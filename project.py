from getpass import getpass
from pathlib import Path
from typing import Any

import anthropic
import psycopg

from comparison import (
    ForcePlanResult,
    ForcingFailure,
    MetricScore,
    MetricStatus,
    PlanEvaluation,
    evaluate_plans,
    force_plan,
)
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


def show_forced(
    connection: psycopg.Connection, query: str, plan: PlanNode
) -> ForcePlanResult | ForcingFailure:
    """
    Force plan on PostgreSQL and print the hints, the plan PostgreSQL chose with them
    and every force-eligible decision it did not reproduce. The returned structured
    outcome preserves hints, targets, gaps, selected retry settings, or a failure reason.
    """
    try:
        result = force_plan(connection, query, plan)

    except ValueError as error:
        reason = str(error)
        print(f"\n{reason}")
        return ForcingFailure(reason)

    except (psycopg.errors.UndefinedFile, psycopg.errors.InsufficientPrivilege) as error:
        connection.rollback()
        reason = f"pg_hint_plan cannot be loaded, so plans cannot be forced: {error}"
        print(f"\n{reason}")
        return ForcingFailure(reason)

    print(f"\npg_hint_plan hints:\n{result.hints}")
    print(f"\nPlan forced with the hints:\n{result.plan}")

    if not result.gaps:
        print("\nPostgreSQL followed every forced decision.")

    for gap in result.gaps:
        print(f"\nNot followed: {gap.decision} requested {gap.a}, got {gap.b}")

    return result


def _format_score(score: MetricScore, unit: str) -> str:
    """Format a score without inventing a number for unavailable or N/A results."""
    if score.status is not MetricStatus.DEFINED:
        return f"N/A [{score.status.value}]: {score.explanation or 'No explanation available.'}"

    assert score.numerator is not None and score.denominator is not None and score.rate is not None
    return f"{score.numerator}/{score.denominator} {unit} ({score.rate:.1%})"


def show_evaluation(evaluation: PlanEvaluation) -> None:
    """Print one concise CLI view derived entirely from a structured evaluation."""
    print("\nPlan comparison metrics:")
    print(
        "- Proposed-plan difference: "
        + _format_score(evaluation.proposed.all_kinds.difference, "decisions")
    )
    print(
        "- Plan realizability: "
        + _format_score(evaluation.realizability.score, "force-eligible decisions")
    )
    print(
        "- Realized-plan difference: "
        + _format_score(evaluation.realized.all_kinds.difference, "decisions")
    )
    print(
        "- Proposed core difference: "
        + _format_score(evaluation.proposed.core.difference, "core decisions")
    )
    print(
        "- Realized core difference: "
        + _format_score(evaluation.realized.core.difference, "core decisions")
    )

    print(f"\nProposed-plan differences: {len(evaluation.proposed.differences)}")
    for difference in evaluation.proposed.differences:
        print(
            f"- {difference.decision}: PostgreSQL {difference.a or 'has none'}, "
            f"agent proposed {difference.b or 'has none'}"
        )

    if evaluation.realized.all_kinds.difference.status is MetricStatus.DEFINED:
        print(f"\nRealized-plan differences: {len(evaluation.realized.differences)}")
        for difference in evaluation.realized.differences:
            print(
                f"- {difference.decision}: PostgreSQL {difference.a or 'has none'}, "
                f"realized agent plan {difference.b or 'has none'}"
            )


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

        postgres_round_trip = show_forced(connection, query, postgres)
        if isinstance(postgres_round_trip, ForcingFailure):
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

        outcome = show_forced(connection, result.query, result.plan)
        evaluation = evaluate_plans(postgres, result.plan, outcome)
        show_evaluation(evaluation)


if __name__ == "__main__":
    main()
