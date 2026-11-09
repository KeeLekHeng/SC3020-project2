import csv
from getpass import getpass
from pathlib import Path

import psycopg

from project import CONNECTION_SETTINGS

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

    with source.open(encoding="utf-8") as input_file, destination.open(
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

# The schema's foreign keys require parent tables to be loaded first.
LOAD_ORDER = [
    "region", "nation", "part", "supplier",
    "partsupp", "customer", "orders", "lineitem",
]

schema = (project_folder / "schema.sql").read_text(encoding="utf-8")
password = getpass("PostgreSQL password: ")

try:
    # One transaction: a failed load leaves no tables behind.
    with psycopg.connect(**CONNECTION_SETTINGS, password=password) as connection:
        with connection.cursor() as cursor:
            cursor.execute(schema)

            for table in LOAD_ORDER:
                csv_path = output_folder / f"{table}.csv"
                with csv_path.open(encoding="utf-8") as data, cursor.copy(
                    f"COPY {table} FROM STDIN (FORMAT csv)"
                ) as copy:
                    while chunk := data.read(1 << 20):
                        copy.write(chunk)
                print(f"{table}: loaded {cursor.rowcount:,} rows")

            cursor.execute("ANALYZE")
except psycopg.errors.DuplicateTable:
    raise SystemExit(
        "The TPC-H tables already exist. Drop them, or recreate the "
        f"{CONNECTION_SETTINGS['dbname']} database, then run this again."
    )
except psycopg.OperationalError as error:
    raise SystemExit(f"Could not connect to PostgreSQL: {error}")

print("Database ready.")
