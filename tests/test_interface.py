"""The GUI's plan drawing and its launch from project.py."""

import sys
from types import SimpleNamespace

import pytest

from comparison import ForcePlanResult, evaluate_plans, realization_gaps
import interface
import project
from preprocessing import PlanNode


def test_plans_dot_draws_each_plan_in_its_own_cluster_with_marks() -> None:
    index_scan = PlanNode(
        node_type="Index Scan", relation="orders", alias="o", index_name="orders_pkey"
    )
    subplan = PlanNode(
        node_type="Seq Scan", alias="n", parent_relationship="InitPlan", subplan_name="InitPlan 1"
    )
    plan = PlanNode(
        node_type="Hash Join",
        children=(index_scan, PlanNode(node_type="Seq Scan", alias="c"), subplan),
    )
    result = PlanNode(node_type="Result")

    def mark(**flags: bool) -> interface.Mark:
        return interface.Mark(numbers=(1,), notes=("#1 note",), **flags)

    dot = interface.plans_dot(
        (
            ("P", result, {id(result): mark(outlined=True)}),
            ("A", plan, {id(plan): mark(filled=True, dashed=True), id(index_scan): mark(gap=True)}),
            ("R", None, {}),
        )
    )

    assert "subgraph cluster0 {\nlabel=<<TABLE" in dot and "<B>P</B>" in dot
    assert (
        'n0 [label="Result #1", tooltip="#1 note", style="rounded,filled", '
        f'fillcolor="white", color="{interface.OUTLINE_COLOR}", penwidth=2]'
    ) in dot
    assert (
        'n1 [label="Hash Join #1\\nc, o", tooltip="#1 note", style="rounded,filled,dashed", '
        f'fillcolor="{interface.DIFFERS_FILL}"]'
    ) in dot
    assert (
        'n2 [label=<Index Scan #1<BR/>orders o<BR/>orders_pkey<BR/>'
        f'<FONT COLOR="{interface.GAP_COLOR}"><B>not kept by R</B></FONT>>, tooltip="#1 note", '
        f'style="rounded,filled", fillcolor="white", color="{interface.GAP_COLOR}", penwidth=2]'
    ) in dot
    assert (
        'n3 [label="Seq Scan\\nc", tooltip="Seq Scan\\nc", style="rounded,filled", '
        'fillcolor="white"]'
    ) in dot
    assert "n1 -> n2\n" in dot
    assert 'n1 -> n4 [style=dashed, label="InitPlan 1"]' in dot
    assert 'n5 [label="No plan for this run", shape=note]' in dot


def test_mark_plans_numbers_decisions_by_tables_and_pairs_nodes_across_plans() -> None:
    def scan(alias: str, node_type: str = "Seq Scan") -> PlanNode:
        return PlanNode(node_type=node_type, alias=alias, relation=alias)

    def join(node_type: str, outer: PlanNode, inner: PlanNode) -> PlanNode:
        return PlanNode(node_type=node_type, children=(outer, inner))

    # P joins c to o by hash and sorts; A asks for a merge join on an index scan of o
    # and no sort; R keeps the merge join but scans o sequentially.
    postgres = PlanNode(node_type="Sort", children=(join("Hash Join", scan("c"), scan("o")),))
    agent = join(
        "Merge Join", scan("c"), scan("o", "Index Scan").model_copy(update={"index_name": "o_pkey"})
    )
    realized = join("Merge Join", scan("c"), scan("o"))
    gaps = tuple(realization_gaps(agent, realized))
    evaluation = evaluate_plans(
        postgres,
        agent,
        ForcePlanResult(hints="", plan=realized, gaps=gaps, switch_offs=False, leading=False),
    )

    numbered, _, (p_marks, a_marks, r_marks) = interface.mark_plans(
        (postgres, agent, realized), evaluation
    )

    # Numbered top-down through A, then P: decisions over the same tables share one.
    # index(o) is numbered though only intended realization counts it.
    assert {str(decision): number for decision, number in numbered.items()} == {
        "join(c, o)": 1,
        "index(o)": 2,
        "scan(o)": 2,
        "sort(c, o)": 1,
    }
    p_join = p_marks[id(postgres.children[0])]
    assert p_join.numbers == (1,) and p_join.outlined and not p_join.dashed
    # Only P sorts, so its Sort node has no counterpart.
    assert p_marks[id(postgres)].dashed
    assert a_marks[id(agent)].filled and not a_marks[id(agent)].gap
    # R kept the agent's merge join, which differs from P's hash join.
    assert r_marks[id(realized)].filled
    o_scan = a_marks[id(agent.children[1])]
    assert o_scan.numbers == (2,) and o_scan.filled and o_scan.gap
    assert not o_scan.dashed
    assert o_scan.notes == (
        "#2 index(o): P — · A o_pkey · R — (not kept by R)",
        "#2 scan(o): P Seq Scan · A Index Scan · R Seq Scan (not kept by R)",
    )


def test_no_arguments_starts_the_gui_on_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []

    def run(command: list[str]) -> SimpleNamespace:
        started.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(project.subprocess, "run", run)

    with pytest.raises(SystemExit) as exit_:
        project.main([])

    assert exit_.value.code == 0
    assert started == [
        [
            sys.executable, "-m", "streamlit", "run",
            str(project.PROJECT_FOLDER / "interface.py"), "--server.address", "localhost",
        ]
    ]


def test_queries_without_experiments_is_an_error() -> None:
    with pytest.raises(SystemExit) as exit_:
        project.main(["-q", "Q1"])

    assert exit_.value.code == 2
