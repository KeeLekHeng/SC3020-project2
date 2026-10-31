import json
from getpass import getpass
from pathlib import Path
import psycopg

from preprocessing import get_postgres_plan


def main():
    password = getpass("PostgreSQL password: ")

    # This sample query is only for testing the connection.
    project_folder = Path(__file__).resolve().parent
    query = (project_folder / "query.sql").read_text(encoding="utf-8")

    with psycopg.connect(
        host="localhost",
        port=5432,
        dbname="tpch",
        user="postgres",
        password=password,
        connect_timeout=10,
    ) as connection:
        connection.read_only = True
        print("Connected to PostgreSQL!")

        plan = get_postgres_plan(connection, query)
        print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
