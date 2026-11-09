"""Hint building, query rewriting and plan comparison, without a database."""

import pytest

from comparison import decisions, differences
from preprocessing import PlanNode, build_hints, unique_aliases


def scan(alias: str, relation: str, role: str | None = None) -> PlanNode:
    """A Seq Scan of relation under alias; role is its parent_relationship."""
    return PlanNode(
        node_type="Seq Scan", alias=alias, relation=relation, parent_relationship=role
    )


def hash_join(outer: PlanNode, inner: PlanNode) -> PlanNode:
    """
    A Hash Join with outer and inner inputs, the inner under a Hash node.

    As in EXPLAIN, the Hash node's own input is labelled Outer.
    """
    return PlanNode(
        node_type="Hash Join",
        children=(
            outer.model_copy(update={"parent_relationship": "Outer"}),
            PlanNode(node_type="Hash", parent_relationship="Inner", children=(inner,)),
        ),
    )


def chain(count: int) -> PlanNode:
    """A left-deep Hash Join chain over count tables t1, t2, ..."""
    plan = scan("t1", "t1")
    for number in range(2, count + 1):
        plan = hash_join(plan, scan(f"t{number}", f"t{number}", "Outer"))

    return plan


def test_hints_fix_scans_joins_and_order() -> None:
    plan = hash_join(scan("o", "orders"), scan("c", "customer", "Outer"))

    hints = build_hints(plan, "SELECT * FROM orders o JOIN customer c ON true")

    assert "HashJoin(o c)" in hints
    assert "Leading((o c))" in hints
    assert "SeqScan(o)" in hints and "SeqScan(c)" in hints


def test_joins_below_a_full_join_get_their_own_leading_hint() -> None:
    inner = hash_join(scan("s", "supplier"), scan("n", "nation", "Outer"))
    full = hash_join(scan("l", "lineitem"), inner)
    full = full.model_copy(update={"join_type": "Full"})
    query = "SELECT 1 FROM supplier s JOIN nation n ON true FULL JOIN lineitem l ON true"

    hints = build_hints(full, query)

    assert "Leading((l (s n)))" in hints
    assert "Leading((s n))" in hints


def test_switch_offs_cover_only_unused_operators() -> None:
    plan = hash_join(scan("o", "orders"), scan("c", "customer", "Outer"))
    query = "SELECT * FROM orders o JOIN customer c ON true"

    hints = build_hints(plan, query)

    assert "Set(enable_mergejoin off)" in hints
    assert "Set(enable_hashjoin off)" not in hints
    assert "enable_" not in build_hints(plan, query, switch_offs=False)
    assert "Leading" not in build_hints(plan, query, leading=False)


@pytest.mark.parametrize(("tables", "lifted"), [(8, False), (9, True)])
def test_joins_past_the_collapse_limit_lift_the_limits(tables: int, lifted: bool) -> None:
    query = "SELECT 1 FROM " + ", ".join(f"t{n}" for n in range(1, tables + 1))

    hints = build_hints(chain(tables), query)

    assert (f"Set(join_collapse_limit {tables})" in hints) is lifted
    assert (f"Set(from_collapse_limit {tables})" in hints) is lifted
    assert ("Set(geqo off)" in hints) is lifted


def test_alias_written_in_query_is_not_taken_for_generated() -> None:
    plan = hash_join(scan("c_1", "customer"), scan("c", "customer", "Outer"))

    hints = build_hints(plan, "SELECT 1 FROM customer c_1 JOIN customer c ON true")

    assert "HashJoin(c_1 c)" in hints


def test_generated_alias_is_refused() -> None:
    plan = hash_join(scan("lineitem", "lineitem"), scan("lineitem_1", "lineitem", "Outer"))
    query = "SELECT 1 FROM lineitem WHERE EXISTS (SELECT 1 FROM lineitem)"

    with pytest.raises(ValueError, match="lineitem_1"):
        build_hints(plan, query)


def test_misnamed_alias_is_refused() -> None:
    with pytest.raises(ValueError, match="cust is not an alias"):
        build_hints(scan("cust", "customer"), "SELECT * FROM customer c")


def test_join_over_from_subquery_uses_its_alias() -> None:
    aggregate = PlanNode(node_type="Aggregate", children=(scan("lineitem", "lineitem", "Outer"),))
    plan = hash_join(aggregate, scan("p", "part", "Outer"))
    query = (
        "SELECT * FROM part p JOIN (SELECT l_partkey, sum(l_quantity) q FROM lineitem "
        "GROUP BY l_partkey) s ON s.l_partkey = p.p_partkey"
    )

    hints = build_hints(plan, query)

    assert "HashJoin(s p)" in hints
    assert "Leading((s p))" in hints


def test_tables_of_an_in_subquery_do_not_count_toward_the_from_subquery() -> None:
    # y reads only orders at its own level; lineitem belongs to the IN subquery.
    aggregate = PlanNode(node_type="Aggregate", children=(scan("orders", "orders", "Outer"),))
    plan = hash_join(aggregate, scan("c", "customer", "Outer"))
    query = (
        "SELECT 1 FROM (SELECT o_custkey FROM orders WHERE o_orderkey IN "
        "(SELECT l_orderkey FROM lineitem) GROUP BY o_custkey) y JOIN customer c ON true"
    )

    assert "HashJoin(y c)" in build_hints(plan, query)


def test_names_are_quoted_and_unsafe_names_refused() -> None:
    quoted = build_hints(scan("Cust", "customer"), 'SELECT 1 FROM customer "Cust"')
    assert 'SeqScan("Cust")' in quoted

    with pytest.raises(ValueError, match="hint comment"):
        build_hints(scan("a*/b", "customer"), 'SELECT 1 FROM customer "a*/b"')


def test_unique_aliases_leaves_queries_without_reused_names_alone() -> None:
    query = "SELECT * FROM orders o JOIN customer c ON c.c_custkey = o.o_custkey"

    assert unique_aliases(query) == query


def test_unique_aliases_renames_later_uses_in_their_own_scope() -> None:
    query = (
        "SELECT lineitem.l_partkey FROM lineitem WHERE l_quantity < "
        "(SELECT avg(lineitem.l_quantity) FROM lineitem WHERE lineitem.l_partkey = 5)"
    )

    assert unique_aliases(query) == (
        "SELECT lineitem.l_partkey FROM lineitem WHERE l_quantity < "
        "(SELECT avg(lineitem2.l_quantity) FROM lineitem lineitem2 "
        "WHERE lineitem2.l_partkey = 5)"
    )


def test_unique_aliases_keeps_the_rest_of_the_text() -> None:
    # sqlglot would regenerate this interval as invalid SQL.
    query = (
        "SELECT 1 FROM lineitem WHERE l_shipdate <= date '1998-12-01' - interval '90' day (3)"
        " AND EXISTS (SELECT 1 FROM lineitem)"
    )

    assert unique_aliases(query) == query.replace(
        "(SELECT 1 FROM lineitem)", "(SELECT 1 FROM lineitem lineitem2)"
    )


def test_unique_aliases_keeps_column_lists_and_avoids_taken_names() -> None:
    query = "SELECT 1 FROM t AS x(a, b) JOIN (SELECT * FROM t AS x(a, b)) x2 ON true"

    assert unique_aliases(query) == (
        "SELECT 1 FROM t AS x(a, b) JOIN (SELECT * FROM t AS x3(a, b)) x2 ON true"
    )


def test_unique_aliases_avoids_names_of_functions_and_subqueries() -> None:
    query = (
        "SELECT 1 FROM nation, unnest(ARRAY[1]) AS nation2(k) "
        "WHERE EXISTS (SELECT 1 FROM nation WHERE n_regionkey = nation2.k)"
    )

    assert "FROM nation nation3 WHERE" in unique_aliases(query)


def test_unique_aliases_drops_the_schema_of_renamed_columns() -> None:
    query = (
        "SELECT 1 FROM public.nation WHERE EXISTS "
        "(SELECT public.nation.n_name FROM public.nation)"
    )

    assert unique_aliases(query).endswith("(SELECT nation2.n_name FROM public.nation nation2)")


def test_lateral_subquery_uses_its_alias() -> None:
    aggregate = PlanNode(node_type="Aggregate", children=(scan("o", "orders", "Outer"),))
    plan = PlanNode(
        node_type="Nested Loop",
        children=(
            scan("c", "customer", "Outer"),
            aggregate.model_copy(update={"parent_relationship": "Inner"}),
        ),
    )
    query = (
        "SELECT 1 FROM customer c, LATERAL "
        "(SELECT count(*) FROM orders o WHERE o.o_custkey = c.c_custkey) t"
    )

    assert "NestLoop(c t)" in build_hints(plan, query)


def test_decisions_ignore_a_subquery_scan_postgresql_may_drop() -> None:
    aggregate = PlanNode(node_type="Aggregate", children=(scan("lineitem", "lineitem", "Outer"),))
    with_scan = hash_join(
        PlanNode(node_type="Subquery Scan", alias="s", children=(aggregate,)),
        scan("p", "part", "Outer"),
    )
    without_scan = hash_join(aggregate, scan("p", "part", "Outer"))

    assert decisions(with_scan).keys() == decisions(without_scan).keys()
    assert differences(with_scan, without_scan) == []


def test_moving_the_gather_is_a_difference() -> None:
    lineitem = scan("l", "lineitem", "Outer")
    lineitem = lineitem.model_copy(update={"parallel_aware": True})
    orders = scan("o", "orders", "Outer")
    early = hash_join(PlanNode(node_type="Gather", children=(lineitem,)), orders)
    late = PlanNode(node_type="Gather", children=(hash_join(lineitem, orders),))

    moved = [str(d.decision) for d in differences(early, late)]

    assert moved == ["gather(l)", "gather(l, o)"]


def parallel_aggregate(partial: str, final: str) -> PlanNode:
    """A two-stage parallel aggregate over lineitem with the given strategies."""
    stage = PlanNode(
        node_type="Aggregate",
        strategy=partial,
        partial_mode="Partial",
        parent_relationship="Outer",
        children=(scan("l", "lineitem", "Outer"),),
    )
    return PlanNode(
        node_type="Aggregate",
        strategy=final,
        partial_mode="Finalize",
        children=(PlanNode(node_type="Gather Merge", children=(stage,)),),
    )


def test_parallel_and_serial_aggregates_are_one_decision() -> None:
    # TPC-H Q1's shapes: the workers hash, and the Finalize stage sorts only because
    # Gather Merge delivers sorted rows.
    lineitem = scan("l", "lineitem", "Outer")
    serial = PlanNode(node_type="Aggregate", strategy="Hashed", children=(lineitem,))
    parallel = parallel_aggregate("Hashed", "Sorted")

    assert {d.decision.kind for d in differences(serial, parallel)} == {"gather"}


def test_partial_aggregate_strategy_is_compared() -> None:
    hashed = parallel_aggregate("Hashed", "Sorted")
    grouped = parallel_aggregate("Sorted", "Sorted")

    assert [str(d.decision) for d in differences(hashed, grouped)] == ["aggregate(l)"]


def test_differences_name_the_decision_and_both_choices() -> None:
    hashed = hash_join(scan("o", "orders"), scan("c", "customer", "Outer"))
    merged = hashed.model_copy(update={"node_type": "Merge Join"})

    [difference] = differences(hashed, merged)

    assert str(difference.decision) == "join(c, o)"
    assert (str(difference.a), str(difference.b)) == ("Hash Join", "Merge Join")
