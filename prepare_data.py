import argparse
import csv
import os
from getpass import getpass
from graphlib import TopologicalSorter
from pathlib import Path

import psycopg
from psycopg import sql

from project import CONNECTION_SETTINGS

project_folder = Path(__file__).resolve().parent

parser = argparse.ArgumentParser(
    description="Create a schema's tables and load one data file into each."
)
parser.add_argument(
    "--schema", type=Path, default=project_folder / "schema.sql",
    help="DDL file to run (default: schema.sql)",
)
parser.add_argument(
    "--data", type=Path, default=project_folder / "data",
    help="folder with one .tbl or headerless .csv file per table, "
    "named after its table (default: data/)",
)
parser.add_argument(
    "--database", default=CONNECTION_SETTINGS["dbname"],
    help="database to load into (default: %(default)s)",
)
args = parser.parse_args()

output_folder = project_folder / "data_clean"
output_folder.mkdir(exist_ok=True)

sources = sorted(
    path for path in args.data.iterdir() if path.suffix in (".tbl", ".csv")
)
files = {source.stem: source for source in sources}

if len(files) != len(sources):
    raise SystemExit(
        f"{args.data} has both a .tbl and a .csv file for the same table. "
        "Keep one data file per table."
    )

for source in sources:
    if source.suffix == ".csv":
        continue

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

# Bytes, because psycopg types accept str only as literal SQL text.
schema = args.schema.read_bytes()
password = os.environ.get("PGPASSWORD") or getpass("PostgreSQL password: ")
# shortcut: sees only tables the DDL creates in the current schema; widen if a
# DDL ever creates its tables in another schema.
tables_query = "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"

try:
    # One transaction: a failed load leaves no tables behind.
    with psycopg.connect(
        **CONNECTION_SETTINGS | {"dbname": args.database}, password=password
    ) as connection:
        with connection.cursor() as cursor:
            # Tables already in the database are not this schema's to load.
            existing = set(cursor.execute(tables_query))
            cursor.execute(schema)
            tables = {name for (name,) in set(cursor.execute(tables_query)) - existing}

            if tables != files.keys():
                no_file = ", ".join(sorted(tables - files.keys())) or "none"
                no_table = ", ".join(files[t].name for t in sorted(files.keys() - tables)) or "none"
                raise SystemExit(
                    f"{args.schema.name} and the data files in {args.data} do not match. "
                    f"Tables with no data file: {no_file}. "
                    f"Data files with no table: {no_table}."
                )

            # The schema's foreign keys require parent tables to be loaded first.
            # A self-reference is left out: one COPY loads the table in any order.
            load_order = TopologicalSorter({table: () for table in sorted(tables)})
            cursor.execute(
                """
                SELECT child.relname, parent.relname
                FROM pg_constraint
                JOIN pg_class child ON child.oid = conrelid
                JOIN pg_class parent ON parent.oid = confrelid
                WHERE contype = 'f' AND conrelid <> confrelid
                  AND connamespace = current_schema()::regnamespace
                  AND parent.relnamespace = connamespace
                  AND child.relname = ANY(%(tables)s)
                  AND parent.relname = ANY(%(tables)s)
                """,
                {"tables": list(tables)},
            )
            for child, parent in cursor:
                load_order.add(child, parent)

            for table in load_order.static_order():
                source = files[table]
                csv_path = source if source.suffix == ".csv" else output_folder / f"{table}.csv"
                with csv_path.open(encoding="utf-8") as data, cursor.copy(
                    sql.SQL("COPY {} FROM STDIN (FORMAT csv)").format(sql.Identifier(table))
                ) as copy:
                    while chunk := data.read(1 << 20):
                        copy.write(chunk)
                print(f"{table}: loaded {cursor.rowcount:,} rows")

            cursor.execute("ANALYZE")

except psycopg.errors.DuplicateTable:
    raise SystemExit(
        f"The tables in {args.schema.name} already exist. Drop them, or recreate "
        f"the {args.database} database, then run this again."
    )

except psycopg.OperationalError as error:
    raise SystemExit(f"Could not connect to PostgreSQL: {error}")

print("Database ready.")
