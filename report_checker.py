import argparse
import concurrent.futures
import gzip
import json
import logging
import tempfile
import time
import zipfile
from pathlib import Path
from typing import List, Tuple, Dict, Iterator

# base path to the archive directory
BASE_PATH = Path("/slgpfs/projects/slc00/slc00474/execution-qc/vault/archive")

def get_file_paths(file_name: str) -> Tuple[Path, Path, Path]:
    # construct paths for json, txt, and zip files based on the file name
    prefix = file_name[:9]  # e.g., egaf00005
    middle = file_name[9:12]  # e.g., 432
    suffix = file_name[12:15]  # e.g., 457
    base_dir = BASE_PATH / prefix / middle / suffix / "execution"
    json_path = base_dir / f"{file_name}_report.json.gz"
    txt_path = base_dir / "input_screen_v2.txt"
    zip_path = base_dir / "stdin_fastqc.zip"
    return json_path, txt_path, zip_path

def check_species(txt_path: Path, warn_missing_file: bool) -> Tuple[str, bool, str]:
    # check if the species file exists
    if not txt_path.exists():
        if warn_missing_file:
            return (
                f"Warning: Species file input_screen_v2.txt not found at {txt_path}\n",
                True,
                "species_file_missing",
            )
        return "", False, ""  # do not warn if file is missing and flag is set

    try:
        # read the species file
        with txt_path.open('r') as file:
            lines = file.readlines()

        if len(lines) >= 3:
            header_line = lines[1].strip()
            headers = header_line.split("\t")
            # find indices of necessary columns
            try:
                species_index = headers.index('Genome')
                one_hit_one_genome_index = headers.index('#One_hit_one_genome')
                percent_one_hit_one_genome_index = headers.index('%One_hit_one_genome')
            except ValueError as e:
                return f"Error: Required columns not found in input_screen_v2.txt: {e}\n", True, "species_error"

            max_value = -1
            max_species = ""
            max_percent = 0.0

            for line in lines[2:]:
                cells = line.strip().split("\t")
                if len(cells) <= max(one_hit_one_genome_index, percent_one_hit_one_genome_index):
                    continue  # skip lines that don't have enough columns
                species = cells[species_index]
                try:
                    one_hit_one_genome = int(cells[one_hit_one_genome_index])
                    percent_one_hit_one_genome = float(cells[percent_one_hit_one_genome_index])
                except ValueError:
                    continue  # skip lines where conversion fails

                if one_hit_one_genome > max_value:
                    max_value = one_hit_one_genome
                    max_species = species
                    max_percent = percent_one_hit_one_genome

            if max_species != "Human":
                return f"Warning: Most reads mapped to {max_species} ({max_value} reads).\n", True, "sp_not_human"
            elif max_percent < 5.0:
                return f"Warning: Species is Unknown ({max_percent:.2f}% reads mapped to Human).\n", True, "species_unknown"
            else:
                return "", False, ""  # no warning

    except Exception as e:
        return f"Error processing input_screen_v2.txt species file: {str(e)}\n", True, "species_error"

    return "", False, ""

def analyze_bam_cram(
    json_path: Path, per_file_counts: Dict
) -> Tuple[str, bool]:
    output = ""
    warnings_found = False

    if not json_path.exists():
        per_file_counts["bam_cram_warnings"]["QC report missing"] += 1
        output += f"Error: BAM/CRAM QC report not found at {json_path}\n"
        warnings_found = True
        return output, warnings_found

    try:
        # open and read the json qc report
        with gzip.open(json_path, 'rt') as file:
            data = json.load(file)

        total_reads = data.get("TotalReads", 0)
        data_section = data.get("Data", {})

        # check for 'MappedReads' key
        if 'MappedReads' in data_section:
            mapped_ratio = data_section["MappedReads"][0]
            reads_unaligned = 100 - (mapped_ratio * 100)

            if reads_unaligned > 40.0:
                output += (
                    f"Warning: Reads unaligned ({reads_unaligned:.2f}%) "
                    f"exceeds 40%.\n"
                )
                per_file_counts["bam_cram_warnings"]["% reads unaligned >40"] += 1
                warnings_found = True
        else:
            output += "Warning: 'MappedReads' data not found in QC report.\n"
            warnings_found = True

        # check for 'MappingQualityDistribution' key
        if 'MappingQualityDistribution' in data_section:
            map_qual_dist = data_section["MappingQualityDistribution"]
            low_mapq_sum = sum(
                count for quality, count in map_qual_dist if quality <= 29
            )

            low_mapq_percentage = (
                (low_mapq_sum / total_reads) * 100 if total_reads > 0 else 0
            )

            if low_mapq_percentage > 20.0:
                output += (
                    f"Warning: Map quality <30 ({low_mapq_percentage:.2f}%) "
                    f"exceeds 20%.\n"
                )
                per_file_counts["bam_cram_warnings"]["% reads map qual <30 >20"] += 1
                warnings_found = True
        else:
            output += "Warning: 'MappingQualityDistribution' data not found in QC report.\n"
            warnings_found = True

        # check for 'Duplicates' key
        if 'Duplicates' in data_section:
            duplicates_percentage = data_section["Duplicates"][0] * 100
            if duplicates_percentage > 20:
                output += (
                    f"Warning: Duplicate reads ({duplicates_percentage:.2f}%) "
                    f"exceed 20%.\n"
                )
                per_file_counts["bam_cram_warnings"]["% duplicate >20"] += 1
                warnings_found = True
        else:
            output += "Warning: 'Duplicates' data not found in QC report.\n"
            warnings_found = True

    except Exception as e:
        output += f"Error processing BAM/CRAM QC report: {str(e)}\n"
        warnings_found = True

    return output, warnings_found

def analyze_fastq(
    zip_path: Path,
    txt_path: Path,
    warn_missing_file: bool,
    per_file_counts: Dict,
) -> Tuple[str, bool]:
    output = ""
    warnings_found = False

    if not zip_path.exists():
        per_file_counts["fastq_warnings"]["QC report missing"] += 1
        output += f"Error: FASTQ QC report not found at {zip_path}\n"
        warnings_found = True
        return output, warnings_found

    try:
        # open the fastqc zip file
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            with tempfile.TemporaryDirectory() as tmpdirname:
                zip_ref.extractall(tmpdirname)
                fastqc_data_path = (
                    Path(tmpdirname) / 'stdin_fastqc' / 'fastqc_data.txt'
                )

                if not fastqc_data_path.exists():
                    output += (
                        f"Error: fastqc_data.txt not found in {zip_path}\n"
                    )
                    warnings_found = True
                    return output, warnings_found

                with fastqc_data_path.open('r') as file:
                    lines = file.readlines()

                # initialize variables
                gc_content_percentage = None
                duplicate_reads_percentage = None

                # parse the fastqc_data.txt file
                lines_iter = iter(lines)
                for line in lines_iter:
                    line = line.strip()
                    if line.startswith(">>Per sequence GC content"):
                        # skip until "#GC Content" line
                        for line in lines_iter:
                            if line.startswith("#GC Content"):
                                break
                        gc_values = []
                        gc_counts = []
                        # read gc content data
                        for line in lines_iter:
                            if line.startswith(">>END_MODULE"):
                                break
                            parts = line.strip().split("\t")
                            if len(parts) == 2:
                                gc_content = float(parts[0])
                                count = float(parts[1])
                                gc_values.append(gc_content)
                                gc_counts.append(count)
                        total_gc_content = sum(gc_counts)
                        if total_gc_content > 0:
                            gc_in_range = sum(
                                count
                                for gc, count in zip(gc_values, gc_counts)
                                if 35 <= gc <= 55
                            )
                            gc_content_percentage = (
                                (gc_in_range / total_gc_content) * 100
                            )
                            if not (35 <= gc_content_percentage <= 55):
                                output += (
                                    f"Warning: GC content "
                                    f"({gc_content_percentage:.2f}%) is out "
                                    f"of acceptable range (35%-55%).\n"
                                )
                                per_file_counts["fastq_warnings"][
                                    "% GC outside 35-55"
                                ] += 1
                                warnings_found = True
                        else:
                            output += (
                                "Warning: Total GC content is zero, unable "
                                "to calculate GC content percentage.\n"
                            )
                            warnings_found = True

                    elif line.startswith(">>Sequence Duplication Levels"):
                        # skip until "#Total Deduplicated Percentage" line
                        for line in lines_iter:
                            if line.startswith(
                                "#Total Deduplicated Percentage"
                            ):
                                parts = line.strip().split("\t")
                                if len(parts) == 2:
                                    dedup_percentage = float(parts[1])
                                    duplicate_reads_percentage = (
                                        100 - dedup_percentage
                                    )
                                    if duplicate_reads_percentage > 20:
                                        output += (
                                            f"Warning: Duplicate reads "
                                            f"({duplicate_reads_percentage:.2f}%) "
                                            "exceed the acceptable "
                                            "threshold (20%).\n"
                                        )
                                        per_file_counts["fastq_warnings"][
                                            "% duplicate >20"
                                        ] += 1
                                        warnings_found = True
                                break

                # check species information
                species_warning, species_flag, species_issue = check_species(
                    txt_path, warn_missing_file
                )
                if species_warning:
                    output += species_warning
                    if species_issue == "sp_not_human":
                        per_file_counts["fastq_warnings"]["sp not-human"] += 1
                    elif species_issue == "species_file_missing":
                        per_file_counts["fastq_warnings"]["species file missing"] += 1
                    elif species_issue == "species_unknown":
                        per_file_counts["fastq_warnings"]["species unknown"] += 1
                    else:
                        per_file_counts["fastq_warnings"]["species error"] += 1
                    warnings_found = True

    except Exception as e:
        output += f"Error processing FASTQ QC report: {str(e)}\n"
        warnings_found = True

    return output, warnings_found

def process_file(
    file_name: str, warn_missing_file: bool
) -> Tuple[str, Dict, str]:
    output = ""
    file_type = ""
    warnings_found = False

    per_file_counts = {
        "warnings_found": False,
        "files_with_warnings": 0,
        "files_no_qc_report": 0,
        "fastq_warnings": {
            "QC report missing": 0,
            "sp not-human": 0,
            "species file missing": 0,
            "species unknown": 0,
            "species error": 0,
            "% duplicate >20": 0,
            "% GC outside 35-55": 0,
        },
        "bam_cram_warnings": {
            "QC report missing": 0,
            "% reads unaligned >40": 0,
            "% reads map qual <30 >20": 0,
            "% duplicate >20": 0,
        },
        "vcf_warnings": {},
    }

    try:
        json_path, txt_path, zip_path = get_file_paths(file_name)

        if json_path.exists():
            # read the json file to determine the file type
            try:
                with gzip.open(json_path, 'rt') as file:
                    data = json.load(file)
            except json.JSONDecodeError as e:
                output += f"Error: Failed to parse JSON QC report at {json_path}: {e}\n"
                per_file_counts["files_no_qc_report"] += 1
                warnings_found = True
                data = {}  # set data to empty to prevent further processing
            if 'VCFVersion' in data:
                file_type = "VCF"
                # no further processing for vcf files
            elif data:
                file_type = "BAM/CRAM"
                result, bam_warnings_found = analyze_bam_cram(
                    json_path, per_file_counts
                )
                output += result
                warnings_found = warnings_found or bam_warnings_found
            else:
                # if data is empty due to JSONDecodeError
                output += f"Error: Invalid or empty QC report for {file_name}.\n"
                warnings_found = True
        elif zip_path.exists():
            file_type = "FASTQ"
            result, fastq_warnings_found = analyze_fastq(
                zip_path, txt_path, warn_missing_file, per_file_counts
            )
            output += result
            warnings_found = warnings_found or fastq_warnings_found
        else:
            output += "Error: No valid QC report found.\n"
            per_file_counts["files_no_qc_report"] += 1
            warnings_found = True

        if output.strip():
            if file_type:
                output = f">> {file_name} | {file_type}\n" + output
            else:
                output = f">> {file_name} | unknown\n" + output

        per_file_counts["warnings_found"] = warnings_found
        if warnings_found:
            per_file_counts["files_with_warnings"] += 1

        return output, per_file_counts, file_type
    except Exception as e:
        output += f"Exception in processing file {file_name}: {e}\n"
        warnings_found = True
        per_file_counts["warnings_found"] = warnings_found
        per_file_counts["files_with_warnings"] += 1
        return output, per_file_counts, file_type

def read_egaf_from_file(file_path: Path) -> List[str]:
    # read egaf ids from a file
    with file_path.open('r') as f:
        return [line.strip() for line in f if line.strip()]

def chunks(iterable: List, size: int) -> Iterator[List]:
    # generator to yield chunks of the iterable
    for i in range(0, len(iterable), size):
        yield iterable[i:i + size]

def main():
    parser = argparse.ArgumentParser(
        description=(
            "analyze BAM/CRAM, FASTQ, or VCF files for quality control issues."
        ),
        epilog=(
            "example usage:\n  python script.py --file egaf_list.txt "
            "--output results.txt"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--egaf', type=str, help="single EGAF ID to analyze")
    parser.add_argument(
        '--file',
        type=str,
        help="file (txt/csv/tsv) containing multiple EGAF IDs",
    )
    parser.add_argument(
        '--output',
        type=str,
        required=True,
        help="output file to append results",
    )
    parser.add_argument(
        '--no-species-warning',
        action='store_true',
        help="do not warn if the species file is missing",
    )
    parser.add_argument(
        '--threads',
        type=int,
        default=4,
        help="number of threads for concurrent processing",
    )
    args = parser.parse_args()

    warn_missing_file = not args.no_species_warning

    # configure logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    start_time = time.time()

    output_file = Path(args.output)

    if args.egaf:
        egaf_list = [args.egaf]
    elif args.file:
        egaf_list = read_egaf_from_file(Path(args.file))
    else:
        parser.error("Provide either --egaf or --file argument.")

    # ensure output file exists and is empty
    output_file.touch()
    output_file.write_text('')

    total_files_checked = 0
    files_with_warnings = 0
    files_no_qc_report = 0
    files_with_qc_report = 0

    fastq_warnings = {
        "QC report missing": 0,
        "sp not-human": 0,
        "species file missing": 0,
        "species unknown": 0,
        "species error": 0,
        "% duplicate >20": 0,
        "% GC outside 35-55": 0,
    }
    bam_cram_warnings = {
        "QC report missing": 0,
        "% reads unaligned >40": 0,
        "% reads map qual <30 >20": 0,
        "% duplicate >20": 0,
    }

    total_fastq_files = 0
    total_bam_cram_files = 0
    total_vcf_files = 0
    files_with_warnings_and_qc = 0

    batch_size = 10000  # adjust the batch size as needed based on system resources

    with output_file.open('a') as f:
        for batch in chunks(egaf_list, batch_size):
            args_list = [(egaf, warn_missing_file) for egaf in batch]
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=args.threads
            ) as executor:
                results = executor.map(
                    process_file,
                    (arg[0] for arg in args_list),
                    (arg[1] for arg in args_list)
                )
                for output, per_file_counts, file_type in results:
                    total_files_checked += 1
                    if per_file_counts["warnings_found"]:
                        files_with_warnings += 1
                    files_no_qc_report += per_file_counts[
                        "files_no_qc_report"
                    ]
                    # count files with qc report, excluding vcf files
                    if per_file_counts["files_no_qc_report"] == 0 and file_type != "VCF":
                        files_with_qc_report += 1
                        if per_file_counts["warnings_found"]:
                            files_with_warnings_and_qc += 1

                    # count file types
                    if file_type == "FASTQ":
                        total_fastq_files += 1
                    elif file_type == "BAM/CRAM":
                        total_bam_cram_files += 1
                    elif file_type == "VCF":
                        total_vcf_files += 1

                    # accumulate warnings
                    for key in fastq_warnings:
                        fastq_warnings[key] += per_file_counts[
                            "fastq_warnings"
                        ][key]

                    for key in bam_cram_warnings:
                        bam_cram_warnings[key] += per_file_counts[
                            "bam_cram_warnings"
                        ][key]

                    # write output to the file
                    f.write(output)

    elapsed_time = time.time() - start_time

    # print final summary
    logging.info(f"\nFinished checking for {total_files_checked} files.")
    if total_files_checked > 0:
        missing_qc_percentage = (
            (files_no_qc_report / total_files_checked) * 100
        )
        logging.info(
            f"{files_no_qc_report} files "
            f"({missing_qc_percentage:.1f}%) have a missing QC report."
        )

        if files_with_qc_report > 0:
            warnings_percentage = (
                (files_with_warnings_and_qc / files_with_qc_report) * 100
            )
            logging.info(
                f"{files_with_warnings_and_qc} files with report identified "
                f"({warnings_percentage:.1f}%) have shown some warning."
            )
        else:
            logging.info("No files with QC report identified.")

    else:
        logging.info("No files were checked.")

    logging.info("\nSummary of warnings:")

    if total_fastq_files > 0:
        logging.info(f"- FASTQ ({total_fastq_files} total files):")
        if fastq_warnings["sp not-human"] > 0:
            logging.info(
                f" {fastq_warnings['sp not-human']} files identified as not human."
            )
        if fastq_warnings["species unknown"] > 0:
            logging.info(
                f" {fastq_warnings['species unknown']} files have species unknown."
            )
        if fastq_warnings["species file missing"] > 0:
            logging.info(
                f" {fastq_warnings['species file missing']} files have missing species file."
            )
        if fastq_warnings["species error"] > 0:
            logging.info(
                f" {fastq_warnings['species error']} files have errors in species file."
            )
        if fastq_warnings["% duplicate >20"] > 0:
            logging.info(
                f" {fastq_warnings['% duplicate >20']} files have a duplicate %>20."
            )
        if fastq_warnings["% GC outside 35-55"] > 0:
            logging.info(
                f" {fastq_warnings['% GC outside 35-55']} files have a GC % outside 35-55 range."
            )
    if total_bam_cram_files > 0:
        logging.info(f"- BAM/CRAM ({total_bam_cram_files} total files):")
        if bam_cram_warnings["% reads unaligned >40"] > 0:
            logging.info(
                f" {bam_cram_warnings['% reads unaligned >40']} files have reads unaligned %>40."
            )
        if bam_cram_warnings["% reads map qual <30 >20"] > 0:
            logging.info(
                f" {bam_cram_warnings['% reads map qual <30 >20']} files have MAPQ under 30 for >20% reads."
            )
        if bam_cram_warnings["% duplicate >20"] > 0:
            logging.info(
                f" {bam_cram_warnings['% duplicate >20']} files have duplicate %>20."
            )
    if total_vcf_files > 0:
        logging.info(f"- VCF ({total_vcf_files} files):")
        # for now, we're just reporting the total number of vcf files

    logging.info(f"\nTime elapsed: {elapsed_time:.2f} seconds.")

if __name__ == "__main__":
    main()