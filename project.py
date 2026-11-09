from getpass import getpass
from pathlib import Path
from typing import Any

import psycopg

from comparison import force_plan
from preprocessing import get_postgres_plan, unique_aliases

CONNECTION_SETTINGS: dict[str, Any] = {
    "host": "localhost",
    "port": 5432,
    "dbname": "tpch",
    "user": "postgres",
    "connect_timeout": 10,
}


def main() -> None:
    """Show PostgreSQL's plan for query.sql, then force the same plan with hints."""
    password = getpass("PostgreSQL password: ")

    project_folder = Path(__file__).resolve().parent
    original = (project_folder / "query.sql").read_text(encoding="utf-8")

    # Hints cannot tell apart two uses of one table name, so give each its own alias.
    query = unique_aliases(original)
    if query != original:
        print(f"Query with each reused table given its own alias:\n{query}")

    with psycopg.connect(**CONNECTION_SETTINGS, password=password) as connection:
        connection.read_only = True
        print("Connected to PostgreSQL!")

        plan = get_postgres_plan(connection, query)
        print(f"\nPostgreSQL plan:\n{plan}")

        # force the exact same pg plan as a test
        try:
            hints, forced, gaps = force_plan(connection, query, plan)

        except ValueError as error:
            print(f"\n{error}")
            return

        except (psycopg.errors.UndefinedFile, psycopg.errors.InsufficientPrivilege) as error:
            connection.rollback()
            print(f"\npg_hint_plan cannot be loaded, so plans cannot be forced: {error}")
            return

        print(f"\npg_hint_plan hints:\n{hints}")
        print(f"\nPlan forced with the hints:\n{forced}")

        if not gaps:
            print("\nPostgreSQL followed every forced decision.")

        for gap in gaps:
            print(f"\nNot followed: {gap.decision} requested {gap.a}, got {gap.b}")


if __name__ == "__main__":
    main()
