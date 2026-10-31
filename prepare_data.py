import csv
from pathlib import Path

project_folder = Path(__file__).resolve().parent
output_folder = project_folder / "data_clean"
output_folder.mkdir(exist_ok=True)

files = sorted(project_folder.rglob("*.tbl"))

if len(files) != 8:
    raise SystemExit(
        f"Expected 8 .tbl files, found {len(files)}. "
        "Keep one set of the generated files inside your project folder."
    )

for source in files:
    destination = output_folder / f"{source.stem}.csv"
    row_count = 0

    with source.open(encoding="utf-8") as input_file:
        with destination.open(
            "w", newline="", encoding="utf-8"
        ) as output_file:
            writer = csv.writer(output_file)

            for line in input_file:
                row = line.rstrip("\r\n")

                if not row.endswith("|"):
                    raise ValueError(f"Missing final delimiter in {source.name}")

                writer.writerow(row[:-1].split("|"))
                row_count += 1

    print(f"{source.name}: converted {row_count:,} rows")
