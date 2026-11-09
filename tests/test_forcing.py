"""Forcing plans on PostgreSQL: a plan PostgreSQL chose must come back when forced."""

import psycopg
import pytest

from comparison import force_plan
from preprocessing import PlanNode, get_postgres_plan, unique_aliases
from tests.queries import REFUSED, SHAPES, TPCH

QUERIES = {**TPCH, **SHAPES}


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


@pytest.mark.parametrize("name", QUERIES)
def test_postgresql_plan_is_forced_back(connection: psycopg.Connection, name: str) -> None:
    query = unique_aliases(QUERIES[name])
    plan = get_postgres_plan(connection, query)

    _, _, gaps = force_plan(connection, query, plan)

    assert gaps == []


@pytest.mark.parametrize("name", ["UNION subquery joined", "GROUP BY subquery joined"])
def test_swapped_join_over_subquery_is_forced(connection: psycopg.Connection, name: str) -> None:
    query = SHAPES[name]
    plan = swap_join_sides(get_postgres_plan(connection, query))

    _, _, gaps = force_plan(connection, query, plan)

    assert gaps == []


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
