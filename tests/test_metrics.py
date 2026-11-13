"""Deterministic plan-comparison metrics and their consumer-facing evidence."""

import json
from dataclasses import FrozenInstanceError

import pytest

import comparison
from comparison import (
    Decision,
    ForcePlanResult,
    ForcingFailure,
    MetricStatus,
    choices_equal,
    compare_plans,
    decisions,
    evaluate_plans,
    realizability,
)
from preprocessing import HintBuildResult, PlanNode, build_hints, build_hints_detailed


def scan(
    alias: str,
    node_type: str = "Seq Scan",
    *,
    parallel: bool = False,
    index: str | None = None,
    role: str | None = None,
) -> PlanNode:
    return PlanNode(
        node_type=node_type,
        alias=alias,
        relation=alias,
        index_name=index,
        parallel_aware=parallel,
        parent_relationship=role,
    )


def join(outer: PlanNode, inner: PlanNode, node_type: str = "Hash Join") -> PlanNode:
    return PlanNode(
        node_type=node_type,
        children=(
            outer.model_copy(update={"parent_relationship": "Outer"}),
            inner.model_copy(update={"parent_relationship": "Inner"}),
        ),
    )


def force_result(
    plan: PlanNode,
    *,
    requested: PlanNode | None = None,
    targets: frozenset[Decision] | None = None,
    leading: bool = True,
) -> ForcePlanResult:
    from comparison import realization_gaps

    requested = requested or plan
    return ForcePlanResult(
        hints="hints",
        plan=plan,
        gaps=tuple(realization_gaps(requested, plan)),
        targets=targets or frozenset(),
        switch_offs=True,
        leading=leading,
    )


def test_result_records_are_immutable_and_statuses_are_distinct() -> None:
    result = compare_plans(scan("o"), scan("o"))

    assert result.all_kinds.difference.status is MetricStatus.DEFINED
    with pytest.raises(FrozenInstanceError):
        result.all_kinds.difference.rate = 0.5  # type: ignore[misc]


def test_public_choice_equivalence_matches_an_unnamed_index() -> None:
    unnamed = scan("o", "Index Scan")
    named = scan("o", "Index Scan", index="orders_pkey")
    decision = Decision("scan", ("o",))

    assert choices_equal(decision, decisions(unnamed)[decision], decisions(named)[decision])


def test_pairwise_identical_changed_and_missing_decisions() -> None:
    identical = compare_plans(scan("o"), scan("o"))
    changed = compare_plans(scan("o"), scan("o", "Index Scan"))
    missing = compare_plans(scan("o"), PlanNode(node_type="Result"))

    assert (identical.all_kinds.matching, identical.all_kinds.different) == (2, 0)
    assert identical.all_kinds.difference.rate == 0.0
    assert identical.all_kinds.similarity.rate == 1.0
    assert changed.all_kinds.different == 1
    assert [str(item.decision) for item in missing.differences] == [
        "parallel(o)", "result(<Result>)", "scan(o)"
    ]


def test_empty_pairwise_universe_uses_identity_convention() -> None:
    comparison = compare_plans(PlanNode(node_type="Hash"), PlanNode(node_type="Hash"))

    assert comparison.all_kinds.total == 0
    assert comparison.all_kinds.difference.rate == 0.0
    assert comparison.all_kinds.similarity.rate == 1.0
    assert "empty universe" in (comparison.all_kinds.difference.explanation or "")


def test_core_view_excludes_parallel_and_gather_only() -> None:
    serial = scan("l")
    parallel_scan = scan("l", parallel=True, role="Outer")
    parallel = PlanNode(node_type="Gather", workers_planned=2, children=(parallel_scan,))

    comparison = compare_plans(serial, parallel)

    assert comparison.all_kinds.different == 2
    assert comparison.core.different == 0
    assert {item.decision.kind for item in comparison.differences} == {"parallel", "gather"}


def test_moved_gather_counts_twice_only_in_all_kinds() -> None:
    lineitem = scan("l", parallel=True)
    orders = scan("o")
    early = join(PlanNode(node_type="Gather", children=(lineitem,)), orders)
    late = PlanNode(node_type="Gather", children=(join(lineitem, orders),))

    comparison = compare_plans(early, late)
    gather = [item for item in comparison.differences if item.decision.kind == "gather"]

    assert len(gather) == 2
    assert comparison.all_kinds.different == comparison.core.different + 2


def test_per_kind_totals_reconcile_with_all_kinds() -> None:
    comparison = compare_plans(
        join(scan("c"), scan("o")),
        join(scan("c", "Index Scan"), scan("o"), "Merge Join"),
    )

    assert sum(item.view.total for item in comparison.by_kind) == comparison.all_kinds.total
    assert sum(item.view.different for item in comparison.by_kind) == comparison.all_kinds.different


def test_realizability_counts_requested_reproduced_unmatched_and_extra() -> None:
    requested = scan("o")
    realized = join(scan("o"), scan("c"))
    outcome = force_result(realized, requested=requested)

    result = realizability(requested, outcome)

    assert (result.score.numerator, result.score.denominator, result.score.rate) == (2, 2, 1.0)
    assert [str(item.decision) for item in result.reproduced] == ["parallel(o)", "scan(o)"]
    assert {str(item.decision) for item in result.extra} == {
        "join(c, o)", "outer(c, o)", "parallel(c)", "scan(c)"
    }
    assert result.gaps == ()


def test_realizability_retains_gaps_without_counting_extras_in_denominator() -> None:
    requested = scan("o")
    realized = scan("o", "Index Scan", index="orders_pkey")
    outcome = force_result(realized, requested=requested)

    result = realizability(requested, outcome)

    assert (result.score.numerator, result.score.denominator) == (1, 2)
    assert [str(item.decision) for item in result.unmatched] == ["scan(o)"]
    assert result.gaps == outcome.gaps
    assert str(result.gaps[0].decision) == "scan(o)"


def test_realizability_without_force_eligible_requests_is_not_applicable() -> None:
    requested = PlanNode(node_type="Hash")
    result = realizability(requested, force_result(PlanNode(node_type="Hash")))

    assert result.score.status is MetricStatus.NOT_APPLICABLE
    assert result.score.rate is None
    assert "no force-eligible" in (result.score.explanation or "").lower()


def test_hint_targets_follow_the_exact_emission_branches() -> None:
    plan = join(scan("c"), scan("o"))
    query = "SELECT 1 FROM c JOIN o ON true"

    with_leading = build_hints_detailed(plan, query)
    without_leading = build_hints_detailed(plan, query, leading=False)

    assert build_hints(plan, query) == with_leading.hints
    assert Decision("join", ("c", "o")) in with_leading.targets
    assert Decision("outer", ("c", "o")) in with_leading.targets
    assert Decision("outer", ("c", "o")) not in without_leading.targets
    assert Decision("join", ("c", "o")) in without_leading.targets


def test_unemitted_hints_do_not_create_targets() -> None:
    lone_parallel = scan("l", parallel=True)
    unnamed = join(scan("c"), PlanNode(node_type="Result"))

    parallel_targets = build_hints_detailed(lone_parallel, "SELECT 1 FROM l").targets
    unnamed_targets = build_hints_detailed(unnamed, "SELECT 1 FROM c").targets

    assert Decision("scan", ("l",)) in parallel_targets
    assert Decision("parallel", ("l",)) not in parallel_targets
    assert not {target for target in unnamed_targets if target.kind in {"join", "outer"}}


def test_force_plan_detailed_keeps_the_selected_retry_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = join(scan("c"), scan("o"))
    wrong = requested.model_copy(update={"node_type": "Merge Join"})
    built = iter(
        [
            HintBuildResult("first", frozenset({Decision("outer", ("c", "o"))})),
            HintBuildResult("second", frozenset({Decision("outer", ("c", "o"))})),
            HintBuildResult("third", frozenset({Decision("join", ("c", "o"))})),
        ]
    )
    forced = iter([wrong, wrong, requested])
    monkeypatch.setattr(comparison, "build_hints_detailed", lambda *_args, **_kwargs: next(built))
    monkeypatch.setattr(comparison, "get_postgres_plan", lambda *_args, **_kwargs: next(forced))

    result = comparison.force_plan_detailed(object(), "SELECT 1", requested)  # type: ignore[arg-type]

    assert result.hints == "third"
    assert result.leading is False
    assert result.targets == frozenset({Decision("join", ("c", "o"))})


def test_force_plan_retry_ranking_ignores_extra_realized_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = scan("o")
    wrong_request = scan("o", "Index Scan")
    reproduced_with_extras = join(scan("o"), scan("c"))
    built = iter(
        [
            HintBuildResult("first", frozenset()),
            HintBuildResult("second", frozenset()),
        ]
    )
    forced = iter([wrong_request, reproduced_with_extras])
    monkeypatch.setattr(comparison, "build_hints_detailed", lambda *_args, **_kwargs: next(built))
    monkeypatch.setattr(comparison, "get_postgres_plan", lambda *_args, **_kwargs: next(forced))

    result = comparison.force_plan_detailed(object(), "SELECT 1", requested)  # type: ignore[arg-type]

    assert result.hints == "second"
    assert result.gaps == ()


def test_force_plan_legacy_tuple_api_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = scan("o")
    detailed = force_result(plan, targets=frozenset({Decision("scan", ("o",))}))
    monkeypatch.setattr(comparison, "force_plan_detailed", lambda *_args: detailed)

    hints, forced, gaps = comparison.force_plan(object(), "SELECT 1", plan)  # type: ignore[arg-type]

    assert (hints, forced, gaps) == (detailed.hints, plan, [])


def test_attribution_requires_target_reproduction_and_no_gap() -> None:
    agent = PlanNode(node_type="Aggregate", strategy="Hashed", children=(scan("o"),))
    outcome = force_result(
        agent,
        targets=frozenset({Decision("scan", ("o",))}),
    )

    evaluation = evaluate_plans(agent, agent, outcome)
    entries = {item.decision.kind: item for item in evaluation.attribution}

    assert entries["scan"].attributable
    assert not entries["parallel"].attributable
    assert "no hint" in entries["parallel"].reason
    assert not entries["aggregate"].attributable
    assert "PostgreSQL" in entries["aggregate"].reason


def test_targeted_realization_gap_is_not_attributed() -> None:
    agent = scan("o")
    realized = scan("o", "Index Scan", index="orders_pkey")
    outcome = force_result(
        realized,
        requested=agent,
        targets=frozenset({Decision("scan", ("o",))}),
    )

    evaluation = evaluate_plans(agent, agent, outcome)
    scan_entry = next(item for item in evaluation.attribution if item.decision.kind == "scan")

    assert scan_entry.targeted and scan_entry.has_gap
    assert not scan_entry.reproduced
    assert not scan_entry.attributable


def test_evaluator_keeps_proposed_realizability_and_realized_views_distinct() -> None:
    postgres = scan("o")
    agent = scan("o", "Index Scan", index="orders_pkey")
    outcome = force_result(
        postgres,
        requested=agent,
        targets=frozenset({Decision("scan", ("o",))}),
    )

    evaluation = evaluate_plans(postgres, agent, outcome)

    assert evaluation.proposed.all_kinds.different == 1
    assert evaluation.realizability.score.rate == 0.5
    assert evaluation.realized.all_kinds.different == 0


def test_forcing_failure_preserves_proposal_and_marks_dependent_metrics_unavailable() -> None:
    postgres = scan("o")
    agent = scan("o", "Index Scan")
    evaluation = evaluate_plans(postgres, agent, ForcingFailure("pg_hint_plan unavailable"))

    assert evaluation.proposed.all_kinds.difference.status is MetricStatus.DEFINED
    assert evaluation.proposed.all_kinds.different == 1
    assert evaluation.realizability.score.status is MetricStatus.UNAVAILABLE
    assert evaluation.realizability.score.numerator is None
    assert evaluation.realizability.score.denominator == 2
    assert evaluation.realized.all_kinds.difference.status is MetricStatus.UNAVAILABLE
    assert evaluation.realized.all_kinds.difference.numerator is None
    assert evaluation.realized.all_kinds.difference.denominator is None
    assert evaluation.realized.all_kinds.difference.explanation == "pg_hint_plan unavailable"


@pytest.mark.parametrize("failure", [False, True])
def test_evaluation_to_dict_is_json_safe_for_complete_and_partial_results(failure: bool) -> None:
    postgres = scan("o")
    agent = scan("o", "Index Scan")
    outcome = (
        ForcingFailure("cannot force")
        if failure
        else force_result(
            postgres,
            requested=agent,
            targets=frozenset({Decision("scan", ("o",))}),
        )
    )

    data = evaluate_plans(postgres, agent, outcome).to_dict()
    encoded = json.dumps(data)

    assert encoded
    assert data["proposed"]["differences"][0]["decision"] == {
        "kind": "scan", "tables": ["o"]
    }
    assert data["forcing"]["status"] == ("unavailable" if failure else "defined")
    assert "PlanNode" not in encoded
