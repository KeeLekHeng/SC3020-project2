"""Forcing plans on PostgreSQL: a plan PostgreSQL chose must come back when forced."""

import psycopg
import pytest

from comparison import (
    MetricStatus,
    differences,
    evaluate_plans,
    force_plan,
    force_plan_detailed,
    realization_gaps,
)
from preprocessing import PlanNode, build_hints, get_postgres_plan, unique_aliases
from tests.queries import REFUSED, REMOVED_JOINS, SHAPES, TPCH

QUERIES = {**TPCH, **SHAPES}


def test_live_pipeline_produces_three_metrics_and_details(
    connection: psycopg.Connection,
) -> None:
    """A real PostgreSQL forcing outcome feeds the complete evaluation contract."""
    query = "SELECT * FROM customer c"
    postgres = get_postgres_plan(connection, query)
    outcome = force_plan_detailed(connection, query, postgres)

    evaluation = evaluate_plans(postgres, postgres, outcome)

    assert evaluation.proposed.all_kinds.difference.status is MetricStatus.DEFINED
    assert evaluation.realizability.score.status is MetricStatus.DEFINED
    assert evaluation.realized.all_kinds.difference.status is MetricStatus.DEFINED
    assert isinstance(evaluation.proposed.differences, tuple)
    assert isinstance(evaluation.realized.differences, tuple)


def swap_join_sides(node: PlanNode) -> PlanNode:
    """Copy of node with the outer and inner inputs of every join swapped."""
    children = tuple(swap_join_sides(child) for child in node.children)
    # A join with a subplan among its children has more than two, so leave it alone.
    if node.node_type in ("Hash Join", "Merge Join", "Nested Loop") and not node.subplans:
        outer, inner = children
        children = (
            inner.model_copy(update={"parent_relationship": "Outer"}),
            outer.model_copy(update={"parent_relationship": "Inner"}),
        )

    return node.model_copy(update={"children": children})


def all_hash_joins(node: PlanNode) -> PlanNode:
    """Copy of node with every Nested Loop turned into a Hash Join."""
    children = tuple(all_hash_joins(child) for child in node.children)
    node_type = "Hash Join" if node.node_type == "Nested Loop" else node.node_type

    return node.model_copy(update={"children": children, "node_type": node_type})


@pytest.mark.parametrize("name", QUERIES)
def test_postgresql_plan_is_forced_back(connection: psycopg.Connection, name: str) -> None:
    query = unique_aliases(QUERIES[name])
    plan = get_postgres_plan(connection, query)

    _, _, gaps = force_plan(connection, query, plan)

    assert gaps == []


@pytest.mark.parametrize(
    "name",
    [
        "UNION subquery joined",
        "GROUP BY subquery joined",
        "IN subquery joined",
        "grouped IN subquery joined",
        "CTE read once joined",
        "subquery with EXISTS joined",
        "OFFSET 0 subquery joined",
        "subquery without tables joined",
    ],
)
def test_swapped_join_over_subquery_is_forced(connection: psycopg.Connection, name: str) -> None:
    query = SHAPES[name]
    plan = swap_join_sides(get_postgres_plan(connection, query))

    _, _, gaps = force_plan(connection, query, plan)

    assert gaps == []


@pytest.mark.parametrize(
    "name",
    [
        "join of two outer-join results",
        "nested FULL JOINs",
        "EXISTS inside EXISTS",
        "DISTINCT VALUES subquery joined",
        "8 tables and EXISTS over 2",
        "join over UNION ALL of constants above 8 tables",
    ],
)
def test_first_hints_force_the_plan_without_a_penalty(
    connection: psycopg.Connection, name: str
) -> None:
    # force_plan()'s retries would hide a Leading hint that pg_hint_plan cannot follow.
    query = SHAPES[name]
    plan = get_postgres_plan(connection, query)

    forced = get_postgres_plan(connection, query, hints=build_hints(plan, query))

    assert realization_gaps(plan, forced) == []
    assert (forced.total_cost or 0) < 1e10


def test_swapped_outer_full_join_keeps_its_method(connection: psycopg.Connection) -> None:
    # Its sides cannot be forced, but the hint must not penalize every way to join it.
    query = SHAPES["nested FULL JOINs"]
    plan = swap_join_sides(get_postgres_plan(connection, query))

    _, forced, gaps = force_plan(connection, query, plan)

    assert [str(gap.decision) for gap in gaps] == ["outer(c, n, r, s)"]
    assert (forced.total_cost or 0) < 1e10


def test_hash_joins_are_forced_after_postgresql_removes_joins(
    connection: psycopg.Connection,
) -> None:
    plan = get_postgres_plan(connection, REMOVED_JOINS)
    hashed = all_hash_joins(plan)

    _, _, gaps = force_plan(connection, REMOVED_JOINS, hashed)

    assert gaps == []


def test_lone_parallel_scan_is_forced(connection: psycopg.Connection) -> None:
    query = "SELECT l_orderkey FROM lineitem WHERE l_quantity > 5"
    connection.execute("SET parallel_setup_cost = 0; SET parallel_tuple_cost = 0")
    try:
        plan = get_postgres_plan(connection, query)
    finally:
        connection.execute("RESET parallel_setup_cost; RESET parallel_tuple_cost")

    _, _, gaps = force_plan(connection, query, plan)

    assert plan.node_type == "Gather"
    assert gaps == []


def test_gather_above_a_join_stays_there(connection: psycopg.Connection) -> None:
    query = TPCH["Q10"]
    plan = get_postgres_plan(connection, query)

    _, forced, _ = force_plan(connection, query, plan)

    assert [d for d in differences(plan, forced) if d.decision.kind == "gather"] == []


@pytest.mark.parametrize("name", [n for n in QUERIES if unique_aliases(QUERIES[n]) != QUERIES[n]])
def test_rewritten_query_plans_like_the_original(
    connection: psycopg.Connection, name: str
) -> None:
    original = get_postgres_plan(connection, QUERIES[name])
    rewritten = get_postgres_plan(connection, unique_aliases(QUERIES[name]))

    assert [n.node_type for n in rewritten.walk()] == [n.node_type for n in original.walk()]
    assert rewritten.total_cost == original.total_cost


def test_copied_cte_tables_are_refused(connection: psycopg.Connection) -> None:
    query = unique_aliases(REFUSED)
    plan = get_postgres_plan(connection, query)

    with pytest.raises(ValueError, match="hints cannot reach"):
        force_plan(connection, query, plan)
