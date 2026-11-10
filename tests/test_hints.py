"""Hint building, query rewriting and plan comparison, without a database."""

import pytest

from comparison import Decision, decisions, differences
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


GROUPED = "(SELECT o_custkey FROM orders o GROUP BY o_custkey)"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # An IN subquery PostgreSQL pulls up is made unique, then joined directly.
        (
            "SELECT 1 FROM customer c WHERE c_custkey IN (SELECT o_custkey FROM orders o)",
            "HashJoin(o c) Leading((o c))",
        ),
        (f"SELECT 1 FROM customer c WHERE c_custkey IN {GROUPED}", 'HashJoin("ANY_subquery" c)'),
        (f"WITH t AS {GROUPED} SELECT 1 FROM t JOIN customer c ON true", "HashJoin(t c)"),
    ],
)
def test_join_over_aggregated_subquery_is_named(query: str, expected: str) -> None:
    aggregate = PlanNode(node_type="Aggregate", children=(scan("o", "orders", "Outer"),))
    plan = hash_join(aggregate, scan("c", "customer", "Outer"))

    hints = build_hints(plan, query)

    assert all(hint in hints for hint in expected.split())


def test_two_grouped_in_subqueries_get_no_any_subquery_hint() -> None:
    aggregate = PlanNode(node_type="Aggregate", children=(scan("o", "orders", "Outer"),))
    plan = hash_join(aggregate, scan("c", "customer", "Outer"))
    query = (
        f"SELECT 1 FROM customer c WHERE c_custkey IN {GROUPED} "
        f"AND c_nationkey IN {GROUPED.replace(' o ', ' o2 ')}"
    )

    assert "ANY_subquery" not in build_hints(plan, query)


def test_leading_leaves_out_a_join_whose_inner_side_has_an_outer_join() -> None:
    left = hash_join(scan("n", "nation"), scan("s", "supplier", "Outer"))
    left = left.model_copy(update={"join_type": "Left"})
    plan = hash_join(scan("r", "region"), left)
    query = "SELECT 1 FROM region r JOIN (nation n LEFT JOIN supplier s ON true) ON true"

    hints = build_hints(plan, query)

    assert "HashJoin(r n s)" in hints
    assert "Leading((n s))" in hints
    assert "Leading((r (n s)))" not in hints
    # With the outer join on the outer side, pg_hint_plan can order the whole level.
    swapped = hash_join(left, scan("r", "region", "Outer"))
    assert "Leading(((n s) r))" in build_hints(swapped, query)


def test_nested_loop_without_memoize_forbids_one() -> None:
    orders = scan("o", "orders", "Inner")
    plain = PlanNode(node_type="Nested Loop", children=(scan("c", "customer", "Outer"), orders))
    memoize = PlanNode(node_type="Memoize", parent_relationship="Inner", children=(orders,))
    cached = plain.model_copy(update={"children": (plain.children[0], memoize)})
    query = "SELECT 1 FROM customer c JOIN orders o ON true"

    assert "NoMemoize(c o)" in build_hints(plain, query)
    assert "NoMemoize" not in build_hints(cached, query)


def test_plan_with_a_gather_zeroes_the_parallel_costs() -> None:
    lineitem = scan("l", "lineitem", "Outer").model_copy(update={"parallel_aware": True})
    gather = PlanNode(node_type="Gather", workers_planned=2, children=(lineitem,))
    query = "SELECT 1 FROM lineitem l"

    assert "Set(parallel_setup_cost 0) Set(parallel_tuple_cost 0)" in build_hints(gather, query)
    assert "parallel_setup_cost" not in build_hints(lineitem, query)


def test_subquery_postgresql_keeps_whole_goes_by_its_alias() -> None:
    # OFFSET 0 keeps the subquery apart, yet EXPLAIN shows only the scan inside it.
    plan = hash_join(scan("lineitem", "lineitem"), scan("o", "orders", "Outer"))
    query = "SELECT 1 FROM (SELECT l_orderkey FROM lineitem{}) t JOIN orders o ON true"

    assert "HashJoin(t o)" in build_hints(plan, query.format(" OFFSET 0"))
    assert "HashJoin(lineitem o)" in build_hints(plan, query.format(""))


def test_subquery_without_tables_goes_by_its_alias() -> None:
    constants = PlanNode(
        node_type="Append", children=(PlanNode(node_type="Result"), PlanNode(node_type="Result"))
    )
    plan = hash_join(constants, scan("n", "nation", "Outer"))
    query = "SELECT 1 FROM nation n JOIN (SELECT 1 AS k UNION ALL SELECT 2) u ON true"

    assert "HashJoin(u n)" in build_hints(plan, query)


def test_subquery_counts_the_tables_of_an_exists_postgresql_pulls_up() -> None:
    semi = hash_join(scan("c", "customer"), scan("o", "orders", "Outer"))
    aggregate = PlanNode(
        node_type="Aggregate", children=(semi.model_copy(update={"join_type": "Semi"}),)
    )
    plan = hash_join(aggregate, scan("n", "nation", "Outer"))
    query = (
        "SELECT 1 FROM nation n JOIN (SELECT c_nationkey FROM customer c WHERE EXISTS "
        "(SELECT 1 FROM orders o WHERE o.o_custkey = c.c_custkey) GROUP BY 1) a ON true"
    )

    hints = build_hints(plan, query)

    assert "HashJoin(a n)" in hints
    assert "HashJoin(c o)" in hints


def test_subquery_over_values_goes_by_its_alias() -> None:
    values = PlanNode(node_type="Values Scan", alias="*VALUES*", parent_relationship="Outer")
    distinct = PlanNode(node_type="Aggregate", children=(values,))
    plan = hash_join(distinct, scan("n", "nation", "Outer"))
    query = "SELECT 1 FROM nation n JOIN (SELECT DISTINCT x FROM (VALUES (1)) v(x)) u ON true"

    assert "HashJoin(u n)" in build_hints(plan, query)


def test_join_below_an_unhinted_join_gets_no_leading_hint() -> None:
    unnamed = PlanNode(node_type="Append", children=(PlanNode(node_type="Result"),))
    plan = hash_join(hash_join(scan("s", "supplier"), scan("n", "nation", "Outer")), unnamed)

    hints = build_hints(plan, "SELECT 1 FROM supplier s JOIN nation n ON true")

    assert "HashJoin(s n)" in hints
    assert "Leading" not in hints


def test_collapse_limits_count_the_relations_the_query_reads() -> None:
    # PostgreSQL may remove LEFT JOINs from the plan after it has split the join.
    query = "SELECT 1 FROM " + " LEFT JOIN ".join(f"t{n}" for n in range(1, 10))

    assert "Set(join_collapse_limit 9)" in build_hints(chain(5), query)


def test_collapse_limits_ignore_tables_of_other_query_levels() -> None:
    joined = " JOIN ".join(f"t{n}" for n in range(1, 6))
    scalar = "(SELECT count(*) FROM u1, u2, u3, u4)"

    assert "collapse_limit" not in build_hints(chain(5), f"SELECT {scalar} FROM {joined}")


@pytest.mark.parametrize(
    "query",
    [
        # PostgreSQL removed the LEFT JOIN, so only o is left under the Aggregate.
        "SELECT 1 FROM customer c JOIN (SELECT o_custkey, count(*) FROM orders o "
        "LEFT JOIN nation n ON true GROUP BY o_custkey) s ON true",
        "not SQL",
    ],
)
def test_join_over_an_unmatched_aggregate_gets_no_hint(query: str) -> None:
    aggregate = PlanNode(node_type="Aggregate", children=(scan("o", "orders", "Outer"),))

    assert "HashJoin" not in build_hints(hash_join(scan("c", "customer"), aggregate), query)


def test_not_in_subquery_is_not_pulled_up() -> None:
    # The NOT IN stays a SubPlan, so EXPLAIN shows only o inside the kept subquery s.
    plan = hash_join(scan("c", "customer"), scan("o", "orders", "Outer"))
    query = (
        "SELECT 1 FROM customer c JOIN (SELECT * FROM orders o WHERE o_custkey NOT IN "
        "(SELECT n_nationkey FROM nation) OFFSET 0) s ON true"
    )

    assert "HashJoin(c s)" in build_hints(plan, query)


def test_not_in_subquery_is_no_any_subquery() -> None:
    aggregate = PlanNode(node_type="Aggregate", children=(scan("o", "orders", "Outer"),))
    plan = hash_join(aggregate, scan("c", "customer", "Outer"))
    query = (
        f"SELECT 1 FROM customer c WHERE c_custkey IN {GROUPED} "
        f"AND c_nationkey NOT IN {GROUPED.replace(' o ', ' o2 ')}"
    )

    assert 'HashJoin("ANY_subquery" c)' in build_hints(plan, query)


def test_generated_name_with_the_same_scan_is_not_refused() -> None:
    # min() and max() of one column read the table twice through the same index.
    first = PlanNode(
        node_type="Index Only Scan", alias="lineitem", relation="lineitem",
        index_name="lineitem_pkey", parent_relationship="InitPlan",
    )
    second = first.model_copy(update={"alias": "lineitem_1"})
    query = "SELECT min(l_orderkey), max(l_orderkey) FROM lineitem"

    hints = build_hints(PlanNode(node_type="Result", children=(first, second)), query)

    assert hints.count("IndexOnlyScan(lineitem lineitem_pkey)") == 1
    assert "lineitem_1" not in hints
    other = second.model_copy(update={"node_type": "Seq Scan", "index_name": None})
    with pytest.raises(ValueError, match="lineitem_1"):
        build_hints(PlanNode(node_type="Result", children=(first, other)), query)


def test_join_over_a_second_values_list_gets_no_hint() -> None:
    values = PlanNode(node_type="Values Scan", alias="*VALUES*", parent_relationship="Outer")
    second = values.model_copy(update={"alias": "*VALUES*_1"})
    plan = hash_join(hash_join(scan("c", "customer"), values), second)
    query = (
        "SELECT 1 FROM customer c WHERE c_nationkey IN (VALUES (1)) "
        "AND c_custkey IN (VALUES (2))"
    )

    assert "*VALUES*_1" not in build_hints(plan, query)


def test_scan_without_index_name_matches_any_index() -> None:
    named = PlanNode(node_type="Index Scan", alias="o", index_name="orders_pkey")
    unnamed = named.model_copy(update={"index_name": None})

    assert differences(unnamed, named) == []
    assert differences(unnamed.model_copy(update={"node_type": "Seq Scan"}), named) != []


def test_join_over_an_unnamed_input_is_its_own_decision() -> None:
    below = hash_join(scan("s", "supplier"), scan("n", "nation", "Outer"))
    above = hash_join(below, PlanNode(node_type="Result")).model_copy(
        update={"node_type": "Nested Loop"}
    )

    found = decisions(above)

    assert str(found[Decision("join", ("n", "s"))]) == "Hash Join"
    assert str(found[Decision("join", ("<Result>", "n", "s"))]) == "Nested Loop"


def test_unique_aliases_leaves_a_name_also_used_bare() -> None:
    # nation here may be the table's whole row; renaming the table would change it.
    query = (
        "SELECT count(*) FROM nation WHERE n_nationkey IN "
        "(SELECT n_nationkey FROM nation WHERE nation IS NOT NULL)"
    )

    assert unique_aliases(query) == query


def test_unique_aliases_renames_a_name_that_is_also_an_output_column() -> None:
    # As in TPC-H Q9, nation is a column the subquery outputs, never the table's row.
    query = (
        "SELECT nation FROM (SELECT n_name AS nation FROM nation) x "
        "JOIN nation ON n_nationkey = 1"
    )

    assert "FROM nation nation2) x" in unique_aliases(query)


def test_unique_aliases_leaves_columns_of_a_subquery_alias_that_hides_the_table() -> None:
    query = (
        "SELECT 1 FROM nation WHERE n_nationkey IN (SELECT n_nationkey FROM nation "
        "WHERE EXISTS (SELECT 1 FROM (VALUES (1)) nation(k) WHERE nation.k = 1))"
    )

    assert unique_aliases(query) == query.replace(
        "FROM nation WHERE EXISTS", "FROM nation nation2 WHERE EXISTS"
    )


def test_unique_aliases_drops_the_database_of_renamed_columns() -> None:
    query = (
        "SELECT 1 FROM tpch.public.nation WHERE EXISTS "
        "(SELECT tpch.public.nation.n_name FROM tpch.public.nation)"
    )

    assert "(SELECT nation2.n_name FROM" in unique_aliases(query)


def test_names_are_quoted_and_unsafe_names_refused() -> None:
    quoted = build_hints(scan("Cust", "customer"), 'SELECT 1 FROM customer "Cust"')
    assert 'SeqScan("Cust")' in quoted

    # PostgreSQL's comments nest, so an opening mark would swallow the statement.
    for name in ("a*/b", "a/*b"):
        with pytest.raises(ValueError, match="hint comment"):
            build_hints(scan(name, "customer"), f'SELECT 1 FROM customer "{name}"')


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


def test_finalize_aggregate_without_partial_stage_keeps_its_decision() -> None:
    lineitem = scan("l", "lineitem", "Outer")
    finalize = PlanNode(
        node_type="Aggregate",
        strategy="Sorted",
        partial_mode="Finalize",
        children=(lineitem,),
    )

    assert str(decisions(finalize)[Decision("aggregate", ("l",))]) == "Sorted"


def test_finalize_aggregate_ignores_a_partial_stage_of_another_aggregate() -> None:
    join = hash_join(parallel_aggregate("Hashed", "Sorted"), scan("o", "orders", "Outer"))
    finalize = PlanNode(
        node_type="Aggregate", strategy="Hashed", partial_mode="Finalize", children=(join,)
    )

    assert str(decisions(finalize)[Decision("aggregate", ("l", "o"))]) == "Hashed"


def test_only_scans_match_any_index() -> None:
    plain = PlanNode(node_type="Sort", sort_keys=("x",), children=(scan("o", "orders"),))
    longer = plain.model_copy(update={"sort_keys": ("x on y",)})

    assert [str(d.decision) for d in differences(plain, longer)] == ["sort(o)"]


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
