"""The AI agent's plan: validation, parsing, the prompt and the retry loop, without an API key."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, get_args

import anthropic
import psycopg
import pytest

from comparison import force_plan
from preprocessing import (
    AGENT_EXAMPLE,
    GATHER,
    JOIN_HINTS,
    SCAN_HINTS,
    AgentNodeType,
    AgentPlanError,
    DatabaseDescription,
    agent_system_prompt,
    agent_user_prompt,
    ask_agent,
    build_hints,
    describe_database,
    get_agent_plan,
    _column_stats,
    _parse,
    _range_columns,
    query_relations,
    unique_aliases,
    validate_agent_plan,
)
from tests.queries import TPCH

FIXTURES = Path(__file__).parent / "fixtures"
QUERY = (Path(__file__).parent.parent / "query.sql").read_text(encoding="utf-8")
DATABASE = DatabaseDescription(
    "customer: ~15,000 rows, 361 pages\norders: ~150,000 rows, 2,648 pages",
    {"customer": {"customer_pkey"}, "orders": {"orders_pkey"}},
)


def answer() -> dict[str, Any]:
    """A fresh copy of the sample agent answer for query.sql."""
    return json.loads((FIXTURES / "agent_customer_orders.json").read_text(encoding="utf-8"))


def errors_of(output: Any) -> list[str]:
    """The problems validate_agent_plan() reports for output."""
    with pytest.raises(AgentPlanError) as caught:
        validate_agent_plan(output)

    return caught.value.errors


def node(data: dict[str, Any], number: int) -> dict[str, Any]:
    """
    The sample answer's operator with that id: 1 Aggregate, 2 Hash Join, 3 Seq Scan of
    c, 4 Hash, 5 Seq Scan of o.
    """
    return data["nodes"][number - 1]


def add(data: dict[str, Any], parent: int, **fields: Any) -> None:
    """Append an operator under parent, with the next id."""
    data["nodes"].append({"id": len(data["nodes"]) + 1, "parent": parent, **fields})


def test_vocabulary_covers_every_forceable_operator() -> None:
    assert set(SCAN_HINTS) | set(JOIN_HINTS) | set(GATHER) <= set(get_args(AgentNodeType))


def test_sample_answer_becomes_a_plan_node_tree() -> None:
    plan = validate_agent_plan(json.dumps(answer())).to_plan_node()

    assert [n.node_type for n in plan.walk()] == [
        "Aggregate", "Hash Join", "Seq Scan", "Hash", "Seq Scan"
    ]
    assert plan.strategy == "Hashed"
    assert plan.group_keys == ("c.c_custkey", "c.c_name")
    assert plan.reason == "Counts each customer's orders in a hash table."
    assert plan.relations() == {"c", "o"}


def test_answer_inside_a_markdown_fence_is_accepted() -> None:
    text = f"Here is the plan:\n```json\n{json.dumps(answer())}\n```"

    assert validate_agent_plan(text).summary.startswith("Hash the filtered orders")


def test_text_after_the_answer_is_ignored() -> None:
    text = json.dumps(answer()) + "\n}\nThe plan above uses {braces}."

    assert len(validate_agent_plan(text).nodes) == 5


def test_text_that_is_not_json_is_rejected() -> None:
    assert errors_of("I would scan customer first.")[0].startswith("The answer is not valid JSON")


def test_node_type_that_is_not_a_string_is_reported() -> None:
    data = answer()
    node(data, 1)["node_type"] = ["Hash Join"]

    assert any(e.startswith("node 1.node_type: Input should be") for e in errors_of(data))


def test_parsed_plan_builds_hints() -> None:
    hints = build_hints(validate_agent_plan(answer()).to_plan_node(), QUERY)

    assert "HashJoin(c o)" in hints
    assert "Leading((c o))" in hints
    assert "Set(max_parallel_workers_per_gather 0)" in hints


def test_unknown_operator_and_field_are_reported() -> None:
    data = answer()
    node(data, 1)["node_type"] = "Hash Aggregate"
    node(data, 1)["cost"] = 12.5
    node(data, 2)["children"] = []

    errors = errors_of(data)

    assert any(e.startswith("node 1.node_type: Input should be") for e in errors)
    assert "node 1.cost: unknown field." in errors
    assert "node 2.children: unknown field." in errors


def test_bad_join_type_is_reported() -> None:
    data = answer()
    node(data, 2)["join_type"] = "Cross"

    assert any(e.startswith("node 2.join_type:") for e in errors_of(data))


def test_join_with_one_input_is_reported() -> None:
    data = answer()
    del data["nodes"][3:]

    assert errors_of(data) == ["node 2: a Hash Join takes 2 input(s), but has 1."]


def test_unmarked_subquery_child_is_reported_as_an_extra_input() -> None:
    data = answer()
    add(data, 2, node_type="Seq Scan", relation="orders", alias="orders2")

    errors = errors_of(data)

    assert len(errors) == 1
    assert "takes 2 input(s), but has 3" in errors[0]
    assert 'parent_relationship "SubPlan"' in errors[0]


def test_marked_subquery_child_is_not_an_input() -> None:
    data = answer()
    add(
        data, 2, node_type="Aggregate", strategy="Plain",
        parent_relationship="SubPlan", subplan_name="SubPlan 1",
    )
    add(data, 6, node_type="Seq Scan", relation="orders", alias="orders2")

    plan = validate_agent_plan(data).to_plan_node()

    join = plan.children[0]
    assert len(join.inputs) == 2
    assert [s.subplan_name for s in join.subplans] == ["SubPlan 1"]
    assert plan.relations() == {"c", "o"}


def test_scan_without_relation_or_alias_is_reported() -> None:
    data = answer()
    del node(data, 3)["relation"], node(data, 3)["alias"]

    errors = errors_of(data)

    assert "node 3: a Seq Scan needs relation." in errors
    assert "node 3: a Seq Scan needs alias." in errors


def test_reused_alias_is_reported() -> None:
    data = answer()
    node(data, 5)["alias"] = "c"

    assert errors_of(data) == ["node 5: alias 'c' is already used at node 3."]


def test_misplaced_fields_are_reported() -> None:
    data = answer()
    node(data, 3)["index_name"] = "customer_pkey"
    node(data, 1)["join_type"] = "Inner"

    errors = errors_of(data)

    assert any("index_name only belongs on" in e and "not on a Seq Scan" in e for e in errors)
    assert any("join_type only belongs on" in e and "not on a Aggregate" in e for e in errors)


def test_parallel_operator_needs_a_gather_above() -> None:
    data = answer()
    node(data, 3)["parallel_aware"] = True

    assert errors_of(data) == [
        "node 3: parallel_aware is only valid below a Gather or Gather Merge."
    ]


def test_parallel_operator_below_a_gather_is_accepted() -> None:
    data = answer()
    node(data, 2)["parallel_aware"] = True
    node(data, 3)["parallel_aware"] = True
    # Put a Gather between the Aggregate and the Hash Join.
    for below in data["nodes"][1:]:
        below["id"] += 1
        below["parent"] += 1
    data["nodes"].insert(1, {"id": 2, "parent": 1, "node_type": "Gather", "workers_planned": 2})

    plan = validate_agent_plan(data).to_plan_node()

    assert "Parallel(c 2 hard)" in build_hints(plan, QUERY)


def test_root_cannot_be_a_subquery() -> None:
    data = answer()
    node(data, 1)["parent_relationship"] = "InitPlan"

    assert "node 1: the root operator cannot be a subquery (parent_relationship)." in errors_of(data)


def test_ids_must_count_from_one_in_list_order() -> None:
    data = answer()
    for entry in data["nodes"]:
        entry["id"] -= 1

    assert errors_of(data)[0] == (
        "node 0: ids count 1, 2, 3, ... in list order, so this operator's id must be 1."
    )


def test_parent_must_be_an_earlier_operator() -> None:
    data = answer()
    node(data, 3)["parent"] = 4
    node(data, 5)["parent"] = 5

    assert errors_of(data) == [
        "node 3: parent 4 is not the id of an earlier operator.",
        "node 5: parent 5 is not the id of an earlier operator.",
    ]


def test_plan_has_exactly_one_root() -> None:
    data = answer()
    node(data, 1)["parent"] = 2
    node(data, 4)["parent"] = None

    assert errors_of(data) == [
        "node 1: the first operator is the root, so its parent must be null.",
        "node 4: only node 1 is the root, so this operator needs a parent.",
    ]


def test_answer_without_operators_is_reported() -> None:
    assert errors_of({"summary": "Nothing to do.", "nodes": []})[0].startswith("nodes: ")


def test_answer_with_too_many_operators_is_reported() -> None:
    chain = [{"id": n, "parent": n - 1 or None, "node_type": "Limit"} for n in range(1, 5001)]

    assert errors_of({"nodes": chain})[0].startswith("nodes: ")


def test_children_keep_list_order_under_their_parent() -> None:
    data = answer()
    data["nodes"] = [
        {"id": 1, "parent": None, "node_type": "Append"},
        {"id": 2, "parent": 1, "node_type": "Result", "reason": "first"},
        {"id": 3, "parent": 1, "node_type": "Limit", "reason": "second"},
        {"id": 4, "parent": 3, "node_type": "Result", "reason": "inside"},
        {"id": 5, "parent": 1, "node_type": "Result", "reason": "third"},
    ]

    plan = validate_agent_plan(data).to_plan_node()

    assert [child.reason for child in plan.children] == ["first", "second", "third"]
    assert plan.children[1].children[0].reason == "inside"


def test_problems_are_reported_one_stage_at_a_time() -> None:
    data = answer()
    data["extra"] = True
    node(data, 2)["join_type"] = "Cross"
    node(data, 3)["alias"] = "o"
    node(data, 5)["parent"] = 9

    schema = errors_of(data)
    del data["extra"]
    node(data, 2)["join_type"] = "Inner"
    tree = errors_of(data)
    node(data, 5)["parent"] = 4
    rules = errors_of(data)

    assert len(schema) == 2
    assert schema[0].startswith("node 2.join_type: ")
    assert schema[1] == "extra: unknown field."
    assert tree == ["node 5: parent 9 is not the id of an earlier operator."]
    assert rules == ["node 5: alias 'o' is already used at node 3."]


def test_system_prompt_is_static_and_free_of_schema_noise() -> None:
    prompt = agent_system_prompt()

    assert prompt == agent_system_prompt()
    assert '"AgentPlanNode"' in prompt
    assert '"parent":{"description":"The id of the operator this one is a child of; null on the root.","type":["integer","null"]}' in prompt
    assert '"children"' not in prompt
    assert '"title"' not in prompt
    assert '"anyOf"' not in prompt


def test_prompt_example_reads_no_table_of_the_database(connection: psycopg.Connection) -> None:
    example = validate_agent_plan(AGENT_EXAMPLE).to_plan_node()

    assert not example.relations() & set(describe_database(connection).indexes)


def test_prompt_example_is_a_valid_answer_without_aliases() -> None:
    plan = validate_agent_plan(AGENT_EXAMPLE).to_plan_node()

    assert plan.relations() == {"department", "employee"}


def test_query_relations_lists_tables_ctes_and_subqueries() -> None:
    q15 = unique_aliases(TPCH["Q15"])
    q7 = unique_aliases(TPCH["Q7"])

    assert query_relations(q15) == [
        ("supplier", "supplier", "table"),
        ("revenue0", "revenue0", "cte"),
        ("lineitem", "lineitem", "table"),
        ("revenue02", "revenue0", "cte"),
    ]
    q7_relations = query_relations(q7)
    assert q7_relations is not None
    assert ("n2", "nation", "table") in q7_relations
    assert ("shipping", "", "subquery") in q7_relations
    assert query_relations("not sql at all (") is None
    assert query_relations("SELECT 1 FROM generate_series(1, 3) g JOIN nation n ON n_nationkey = g") == [
        ("n", "nation", "table")
    ]


def test_query_relations_lists_tables_inside_parenthesised_joins() -> None:
    query = (
        "SELECT count(*) FROM (nation n LEFT JOIN supplier s ON s.s_nationkey = n.n_nationkey) "
        "JOIN ((region r)) ON n.n_regionkey = r.r_regionkey"
    )

    assert query_relations(query) == [
        ("n", "nation", "table"), ("r", "region", "table"), ("s", "supplier", "table")
    ]


def stats(**overrides: Any) -> list[str]:
    """_column_stats() for a plain 1,000-row column with 100 distinct values."""
    values: dict[str, Any] = dict(
        rows=1000, n_distinct=100, null_frac=0, category="N", common_values=None,
        common_freqs=None, histogram=None, correlation=0.0, show_range=True,
    )
    return _column_stats(**{**values, **overrides})


def test_column_stats_give_distinct_count_and_null_fraction() -> None:
    assert stats() == ["~100 distinct"]
    assert stats(n_distinct=-1) == ["unique"]
    assert stats(n_distinct=-0.5) == ["~500 distinct"]
    assert stats(null_frac=0.25) == ["~100 distinct", "25% null"]
    assert stats(n_distinct=None) == []


def test_column_stats_show_common_values_only_when_skewed() -> None:
    skewed = stats(common_values=["A", "B"], common_freqs=[0.4, 0.1])
    even = stats(common_values=["A", "B"], common_freqs=[0.011, 0.01])
    sampling_noise = stats(n_distinct=2500, common_values=["A"], common_freqs=[0.0009])

    assert skewed == ["~100 distinct", "most common: 'A' 40%, 'B' 10%"]
    assert even == ["~100 distinct"]
    assert sampling_noise == ["~2,500 distinct"]


def test_column_stats_give_quartiles_of_numeric_and_date_ranges() -> None:
    histogram = [str(n) for n in range(0, 101)]

    assert stats(histogram=histogram) == [
        "~100 distinct", "range 0 to 100, quartiles 25, 50, 75"
    ]
    assert stats(histogram=histogram, show_range=False) == ["~100 distinct"]
    assert stats(histogram=histogram, category="S") == ["~100 distinct"]


def test_column_stats_mention_strong_correlation() -> None:
    assert stats(correlation=0.99) == ["~100 distinct", "table stored in this column's order"]
    assert stats(correlation=-0.9)[-1] == "table stored in reverse this column's order"
    assert stats(correlation=0.3) == ["~100 distinct"]
    assert stats(n_distinct=1, correlation=1.0) == ["~1 distinct"]


def test_range_columns_are_those_compared_with_inequalities() -> None:
    parsed = _parse(unique_aliases(TPCH["Q17"]) + " and l_extendedprice between 1 and 2")
    assert parsed is not None

    assert _range_columns(parsed) == {"l_quantity", "l_extendedprice"}


def test_user_prompt_lists_the_aliases_and_the_database() -> None:
    prompt = agent_user_prompt(QUERY, DATABASE)

    assert "Aliases (use exactly these): c (table customer), o (table orders)" in prompt
    assert "customer: ~15,000 rows" in prompt
    assert prompt.rstrip().endswith("```")


class FakeClient:
    """Stands in for anthropic.Anthropic: answers each request with the next reply."""

    def __init__(self, *replies: str, stop_reason: str = "end_turn") -> None:
        self.replies = list(replies)
        self.stop_reason = stop_reason
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    def stream(self, **request: Any) -> "FakeClient":
        self.requests.append(copy.deepcopy(request))
        return self

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def get_final_message(self) -> SimpleNamespace:
        content = [
            anthropic.types.ThinkingBlock(type="thinking", thinking="", signature=""),
            anthropic.types.TextBlock(type="text", text=self.replies.pop(0)),
        ]
        return SimpleNamespace(
            content=content, stop_reason=self.stop_reason, stop_details=None
        )


def test_good_first_answer_is_accepted() -> None:
    client = FakeClient(json.dumps(answer()))

    result = ask_agent(cast(anthropic.Anthropic, client), QUERY, DATABASE)

    assert result.problems == []
    assert result.summary.startswith("Hash the filtered orders")
    assert result.plan.relations() == {"c", "o"}

    request = client.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "c.c_custkey = o.o_custkey" in request["messages"][0]["content"]


def test_problems_go_back_to_the_agent_and_its_fix_is_accepted() -> None:
    bad = answer()
    node(bad, 3)["alias"] = "customer"
    client = FakeClient(json.dumps(bad), json.dumps(answer()))

    result = ask_agent(cast(anthropic.Anthropic, client), QUERY, DATABASE)

    assert len(result.problems) == 1
    assert [r["output_config"]["effort"] for r in client.requests] == ["low", "medium"]
    retry = client.requests[1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert retry[1]["content"][0].type == "thinking"
    assert "The query reads c, but no scan in the plan has that alias." in retry[2]["content"]
    assert "The plan scans customer, which is not an alias of a table the query reads." in retry[2]["content"]


def test_plan_is_checked_against_the_query_and_not_postgresqls_plan() -> None:
    # PostgreSQL answers this with two index scans, lineitem and lineitem_1.
    query = "SELECT min(l_orderkey), max(l_orderkey) FROM lineitem"
    plan = {"nodes": [
        {"id": 1, "parent": None, "node_type": "Aggregate", "strategy": "Plain"},
        {"id": 2, "parent": 1, "node_type": "Seq Scan", "relation": "lineitem", "alias": "lineitem"},
    ]}
    client = FakeClient(json.dumps(plan))

    result = ask_agent(cast(anthropic.Anthropic, client), query, DATABASE)

    assert result.problems == []


def test_unknown_index_goes_back_to_the_agent() -> None:
    bad = answer()
    node(bad, 3).update(node_type="Index Scan", index_name="customer_idx")
    client = FakeClient(json.dumps(bad), json.dumps(answer()))

    ask_agent(cast(anthropic.Anthropic, client), QUERY, DATABASE)

    retry = client.requests[1]["messages"][2]["content"]
    assert "'customer_idx' is not an index on customer. Its indexes are: customer_pkey." in retry


def test_relation_that_is_not_the_aliased_table_goes_back_to_the_agent() -> None:
    bad = answer()
    node(bad, 3).update(node_type="Index Scan", relation="c", index_name="customer_pkey")
    client = FakeClient(json.dumps(bad), json.dumps(answer()))

    ask_agent(cast(anthropic.Anthropic, client), QUERY, DATABASE)

    retry = client.requests[1]["messages"][2]["content"]
    assert "Scan of c: c reads the table customer, so relation must be 'customer', not 'c'." in retry
    assert "not an index" not in retry


def test_agent_gives_up_after_max_attempts() -> None:
    client = FakeClient("no plan", "still no plan")

    with pytest.raises(AgentPlanError) as caught:
        ask_agent(cast(anthropic.Anthropic, client), QUERY, DATABASE, max_attempts=2)

    assert caught.value.answer == "still no plan"
    assert len(client.requests) == 2


def test_cut_off_answer_raises() -> None:
    client = FakeClient("{", stop_reason="max_tokens")

    with pytest.raises(RuntimeError, match="max_tokens"):
        ask_agent(cast(anthropic.Anthropic, client), QUERY, DATABASE)


def test_database_description_reads_the_catalogs(connection: psycopg.Connection) -> None:
    database = describe_database(connection)

    assert database.indexes["customer"] >= {"customer_pkey"}
    assert "lineitem: ~" in database.text
    assert "customer_pkey unique btree (c_custkey)" in database.text
    assert database.text.startswith("Settings: max_parallel_workers_per_gather=")
    assert "random_page_cost" not in database.text


def test_database_description_keeps_only_what_the_query_reads(
    connection: psycopg.Connection,
) -> None:
    database = describe_database(connection, QUERY)

    assert set(database.indexes) == {"customer", "orders"}
    assert "lineitem" not in database.text
    assert "c_custkey integer (unique; table stored in this column's order)" in database.text
    assert "o_orderdate date (~" in database.text and "range 1992-" in database.text
    assert "c_comment" not in database.text
    assert "o_custkey references customer(c_custkey)" in database.text
    # customer's key to nation is left out, since the query does not read nation.
    assert "references nation" not in database.text


def test_agent_plan_is_forced_on_postgres(connection: psycopg.Connection) -> None:
    result = get_agent_plan(connection, QUERY, client=cast(anthropic.Anthropic, FakeClient(json.dumps(answer()))))

    hints, forced, gaps = force_plan(connection, result.query, result.plan)

    assert "HashJoin(c o)" in hints
    assert gaps == []
