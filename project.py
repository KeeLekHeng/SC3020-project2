from getpass import getpass
from pathlib import Path
from typing import Any

import anthropic
import psycopg

from comparison import differences, force_plan
from preprocessing import (
    AgentPlanError,
    PlanNode,
    api_key,
    get_agent_plan,
    get_postgres_plan,
    unique_aliases,
)

CONNECTION_SETTINGS: dict[str, Any] = {
    "host": "localhost",
    "port": 5432,
    "dbname": "tpch",
    "user": "postgres",
    "connect_timeout": 10,
}


def show_forced(connection: psycopg.Connection, query: str, plan: PlanNode) -> PlanNode | None:
    """
    Force plan on PostgreSQL and print the hints, the plan PostgreSQL chose with them
    and every forced decision it did not follow. Returns the chosen plan, or None,
    after printing why, when the plan cannot be forced.
    """
    try:
        hints, forced, gaps = force_plan(connection, query, plan)

    except ValueError as error:
        print(f"\n{error}")
        return None

    except (psycopg.errors.UndefinedFile, psycopg.errors.InsufficientPrivilege) as error:
        connection.rollback()
        print(f"\npg_hint_plan cannot be loaded, so plans cannot be forced: {error}")
        return None

    print(f"\npg_hint_plan hints:\n{hints}")
    print(f"\nPlan forced with the hints:\n{forced}")

    if not gaps:
        print("\nPostgreSQL followed every forced decision.")

    for gap in gaps:
        print(f"\nNot followed: {gap.decision} requested {gap.a}, got {gap.b}")

    return forced


def main() -> None:
    """
    Run the whole pipeline on query.sql: show PostgreSQL's plan and force it back as a
    round-trip check, then ask the AI agent for its plan, force that, and list the
    decisions the two plans made differently.
    """
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

        postgres = get_postgres_plan(connection, query)
        print(f"\nPostgreSQL plan:\n{postgres}")

        if show_forced(connection, query, postgres) is None:
            return

        if api_key() is None:
            print("\nNo ANTHROPIC_API_KEY is set (see the README), so the AI agent is not asked.")
            return

        print("\nAsking the AI agent for its plan...")
        try:
            result = get_agent_plan(connection, query)

        except AgentPlanError as error:
            print(f"\nThe agent gave no usable plan. {error}")
            return

        except (anthropic.AnthropicError, RuntimeError) as error:
            print(f"\nThe agent could not give a plan: {error}")
            return

        print(f"\nAgent plan, after {len(result.problems) + 1} answer(s):\n{result.plan}")
        print(f"\nAgent's strategy: {result.summary}")

        forced = show_forced(connection, result.query, result.plan)
        if forced is None:
            return

        found = differences(postgres, forced)
        print(f"\nDecisions PostgreSQL's plan and the agent's forced plan made differently: {len(found)}")
        for difference in found:
            print(
                f"- {difference.decision}: PostgreSQL {difference.a or 'has none'}, "
                f"agent {difference.b or 'has none'}"
            )


if __name__ == "__main__":
    main()
