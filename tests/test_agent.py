"""The AI agent's plan: validation and parsing into PlanNode, without a database."""

import json
from pathlib import Path
from typing import Any

import pytest

from preprocessing import (
    AGENT_NODE_TYPES,
    GATHER,
    JOIN_HINTS,
    SCAN_HINTS,
    AgentPlanError,
    PlanNode,
    build_hints,
    parse_agent_plan,
    relation_mismatches,
    validate_agent_plan,
)

FIXTURES = Path(__file__).parent / "fixtures"
QUERY = (Path(__file__).parent.parent / "query.sql").read_text(encoding="utf-8")


def answer() -> dict[str, Any]:
    """A fresh copy of the sample agent answer for query.sql."""
    return json.loads((FIXTURES / "agent_customer_orders.json").read_text(encoding="utf-8"))


def postgres_plan() -> PlanNode:
    """PostgreSQL's plan for query.sql, from the hand-written EXPLAIN fixture."""
    explain = json.loads((FIXTURES / "pg_customer_orders.json").read_text(encoding="utf-8"))
    return PlanNode.model_validate(explain[0]["Plan"])


def errors_of(output: Any) -> list[str]:
    """The problems validate_agent_plan() reports for output."""
    with pytest.raises(AgentPlanError) as caught:
        validate_agent_plan(output)

    return caught.value.errors


def join_of(data: dict[str, Any]) -> dict[str, Any]:
    """The Hash Join under the sample answer's Aggregate."""
    return data["plan"]["children"][0]


def test_vocabulary_covers_every_forceable_operator() -> None:
    assert set(SCAN_HINTS) | set(JOIN_HINTS) | set(GATHER) <= set(AGENT_NODE_TYPES)


def test_sample_answer_becomes_a_plan_node_tree() -> None:
    plan = parse_agent_plan(json.dumps(answer()))

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


def test_text_that_is_not_json_is_rejected() -> None:
    assert errors_of("I would scan customer first.")[0].startswith("The answer is not valid JSON")


def test_parsed_plan_builds_hints() -> None:
    hints = build_hints(parse_agent_plan(answer()), QUERY)

    assert "HashJoin(c o)" in hints
    assert "Leading((c o))" in hints
    assert "Set(max_parallel_workers_per_gather 0)" in hints


def test_unknown_operator_and_field_are_reported() -> None:
    data = answer()
    data["plan"]["node_type"] = "Hash Aggregate"
    data["plan"]["cost"] = 12.5

    errors = errors_of(data)

    assert any(e.startswith("plan.node_type: Input should be") for e in errors)
    assert "plan.cost: unknown field." in errors


def test_bad_join_type_is_reported() -> None:
    data = answer()
    join_of(data)["join_type"] = "Cross"

    assert any(e.startswith("plan.children[0].join_type:") for e in errors_of(data))


def test_join_with_one_input_is_reported() -> None:
    data = answer()
    join_of(data)["children"].pop()

    assert "plan.children[0]: a Hash Join takes 2 input(s), but has 1." in errors_of(data)


def test_unmarked_subquery_child_is_reported_as_an_extra_input() -> None:
    data = answer()
    subquery = {"node_type": "Seq Scan", "relation": "orders", "alias": "orders2"}
    join_of(data)["children"].append(subquery)

    errors = errors_of(data)

    assert len(errors) == 1
    assert "takes 2 input(s), but has 3" in errors[0]
    assert 'parent_relationship "SubPlan"' in errors[0]


def test_marked_subquery_child_is_not_an_input() -> None:
    data = answer()
    subquery = {
        "node_type": "Aggregate",
        "strategy": "Plain",
        "parent_relationship": "SubPlan",
        "subplan_name": "SubPlan 1",
        "children": [{"node_type": "Seq Scan", "relation": "orders", "alias": "orders2"}],
    }
    join_of(data)["children"].append(subquery)

    plan = parse_agent_plan(data)

    join = plan.children[0]
    assert len(join.inputs) == 2
    assert [s.subplan_name for s in join.subplans] == ["SubPlan 1"]
    assert plan.relations() == {"c", "o"}


def test_scan_without_relation_or_alias_is_reported() -> None:
    data = answer()
    scan = join_of(data)["children"][0]
    del scan["relation"], scan["alias"]

    errors = errors_of(data)

    assert "plan.children[0].children[0]: a Seq Scan needs relation." in errors
    assert "plan.children[0].children[0]: a Seq Scan needs alias." in errors


def test_reused_alias_is_reported() -> None:
    data = answer()
    join_of(data)["children"][1]["children"][0]["alias"] = "c"

    errors = errors_of(data)

    assert errors == [
        "plan.children[0].children[1].children[0]: alias 'c' is already used at "
        "plan.children[0].children[0]."
    ]


def test_misplaced_fields_are_reported() -> None:
    data = answer()
    join_of(data)["children"][0]["index_name"] = "customer_pkey"
    data["plan"]["join_type"] = "Inner"

    errors = errors_of(data)

    assert any("index_name only belongs on" in e and "not on a Seq Scan" in e for e in errors)
    assert any("join_type only belongs on" in e and "not on a Aggregate" in e for e in errors)


def test_parallel_operator_needs_a_gather_above() -> None:
    data = answer()
    join_of(data)["children"][0]["parallel_aware"] = True

    assert errors_of(data) == [
        "plan.children[0].children[0]: parallel_aware is only valid below a Gather or "
        "Gather Merge."
    ]


def test_parallel_operator_below_a_gather_is_accepted() -> None:
    data = answer()
    join = join_of(data)
    join["parallel_aware"] = True
    join["children"][0]["parallel_aware"] = True
    data["plan"]["children"] = [
        {"node_type": "Gather", "workers_planned": 2, "children": [join]}
    ]

    plan = parse_agent_plan(data)

    assert "Parallel(c 2 hard)" in build_hints(plan, QUERY)


def test_root_cannot_be_a_subquery() -> None:
    data = answer()
    data["plan"]["parent_relationship"] = "InitPlan"

    assert "plan: the root operator cannot be a subquery (parent_relationship)." in errors_of(data)


def test_all_problems_are_reported_at_once() -> None:
    data = answer()
    data["extra"] = True
    join = join_of(data)
    join["join_type"] = "Cross"
    join["children"][0]["alias"] = "o"

    errors = errors_of(data)

    assert len(errors) == 3


def test_relation_mismatches() -> None:
    agent = parse_agent_plan(answer())

    assert relation_mismatches(agent, postgres_plan()) == (set(), set())

    data = answer()
    join_of(data)["children"][0]["alias"] = "customer"
    missing, extra = relation_mismatches(parse_agent_plan(data), postgres_plan())

    assert (missing, extra) == ({"c"}, {"customer"})
