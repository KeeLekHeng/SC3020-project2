"""Deterministic plan-comparison metrics and their consumer-facing evidence."""

from dataclasses import FrozenInstanceError

import pytest

import comparison
from comparison import (
    ForcePlanResult,
    ForcingFailure,
    MetricStatus,
    compare_plans,
    evaluate_plans,
    realizability,
)
from preprocessing import PlanNode


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
    leading: bool = True,
) -> ForcePlanResult:
    from comparison import realization_gaps

    requested = requested or plan
    return ForcePlanResult(
        hints="hints",
        plan=plan,
        gaps=tuple(realization_gaps(requested, plan)),
        switch_offs=True,
        leading=leading,
    )


def test_result_records_are_immutable_and_statuses_are_distinct() -> None:
    result = compare_plans(scan("o"), scan("o"))

    assert result.all_kinds.difference.status is MetricStatus.DEFINED
    with pytest.raises(FrozenInstanceError):
        result.all_kinds.difference.rate = 0.5  # type: ignore[misc]


def test_an_unnamed_index_is_not_a_request() -> None:
    agent = scan("o", "Index Scan")
    realized = scan("o", "Index Scan", index="orders_pkey")

    result = realizability(agent, force_result(realized, requested=agent))

    assert (result.score.numerator, result.score.denominator) == (2, 2)
    assert [str(item.decision) for item in result.extra] == ["index(o)"]


def test_a_named_index_that_falls_back_to_a_seq_scan_is_two_gaps() -> None:
    agent = scan("o", "Index Scan", index="orders_pkey")

    result = realizability(agent, force_result(scan("o"), requested=agent))

    assert [str(gap.decision) for gap in result.gaps] == ["index(o)", "scan(o)"]


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


def test_empty_pairwise_universe_is_not_applicable() -> None:
    comparison = compare_plans(PlanNode(node_type="Hash"), PlanNode(node_type="Hash"))

    assert comparison.all_kinds.total == 0
    assert comparison.all_kinds.difference.status is MetricStatus.NOT_APPLICABLE
    assert comparison.all_kinds.difference.rate is None
    assert comparison.all_kinds.similarity.rate is None


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
    assert (comparison.all_kinds.different, comparison.core.different) == (2, 0)


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
    assert result.gaps == outcome.gaps
    assert str(result.gaps[0].decision) == "scan(o)"


def test_realizability_without_force_eligible_requests_is_not_applicable() -> None:
    requested = PlanNode(node_type="Hash")
    result = realizability(requested, force_result(PlanNode(node_type="Hash")))

    assert result.score.status is MetricStatus.NOT_APPLICABLE
    assert result.score.rate is None
    assert "no force-eligible" in (result.score.explanation or "").lower()


def test_force_plan_keeps_the_selected_retry_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = join(scan("c"), scan("o"))
    wrong = requested.model_copy(update={"node_type": "Merge Join"})
    built = iter(["first", "second", "third"])
    forced = iter([wrong, wrong, requested])
    monkeypatch.setattr(comparison, "build_hints", lambda *_args, **_kwargs: next(built))
    monkeypatch.setattr(comparison, "get_postgres_plan", lambda *_args, **_kwargs: next(forced))

    result = comparison.force_plan(object(), "SELECT 1", requested)  # type: ignore[arg-type]

    assert result.hints == "third"
    assert result.leading is False


def test_force_plan_retry_ranking_ignores_extra_realized_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = scan("o")
    wrong_request = scan("o", "Index Scan")
    reproduced_with_extras = join(scan("o"), scan("c"))
    built = iter(["first", "second"])
    forced = iter([wrong_request, reproduced_with_extras])
    monkeypatch.setattr(comparison, "build_hints", lambda *_args, **_kwargs: next(built))
    monkeypatch.setattr(comparison, "get_postgres_plan", lambda *_args, **_kwargs: next(forced))

    result = comparison.force_plan(object(), "SELECT 1", requested)  # type: ignore[arg-type]

    assert result.hints == "second"
    assert result.gaps == ()


def test_intended_counts_only_force_eligible_disagreements_the_realized_plan_kept() -> None:
    def aggregate(strategy: str, child: PlanNode) -> PlanNode:
        return PlanNode(node_type="Aggregate", strategy=strategy, children=(child,))

    postgres = aggregate("Hashed", join(scan("c"), scan("o")))
    agent = aggregate(
        "Sorted", join(scan("c", "Index Scan"), scan("o", "Index Scan"))
    )
    realized = aggregate("Sorted", join(scan("c"), scan("o", "Index Scan")))

    evaluation = evaluate_plans(postgres, agent, force_result(realized, requested=agent))

    # scan(c) and scan(o) differ from PostgreSQL; only scan(o) was realized. The
    # aggregate also differs but cannot be hinted, and the agreements do not count.
    assert (evaluation.intended.numerator, evaluation.intended.denominator) == (1, 2)


def test_intended_is_not_applicable_when_the_agent_agrees_with_postgres() -> None:
    plan = join(scan("c"), scan("o"))

    evaluation = evaluate_plans(plan, plan, force_result(plan))

    assert evaluation.intended.status is MetricStatus.NOT_APPLICABLE
    assert evaluation.intended.rate is None


def test_evaluator_keeps_proposed_realizability_and_realized_views_distinct() -> None:
    postgres = scan("o")
    agent = scan("o", "Index Scan", index="orders_pkey")
    outcome = force_result(
        postgres,
        requested=agent,
    )

    evaluation = evaluate_plans(postgres, agent, outcome)

    assert evaluation.proposed.all_kinds.different == 1
    # scan, index and parallel were requested; only the serial scan was reproduced.
    score = evaluation.realizability.score
    assert (score.numerator, score.denominator) == (1, 3)
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
    assert evaluation.realized.all_kinds.total is None
    assert evaluation.intended.status is MetricStatus.UNAVAILABLE
    assert evaluation.intended.denominator == 1


def test_an_unnamed_index_is_a_difference_but_not_an_intended_change() -> None:
    postgres = scan("o", "Index Scan", index="orders_pkey")
    agent = scan("o", "Index Scan")

    evaluation = evaluate_plans(postgres, agent, force_result(postgres, requested=agent))

    assert [str(d.decision) for d in evaluation.proposed.differences] == ["index(o)"]
    assert evaluation.intended.status is MetricStatus.NOT_APPLICABLE


def test_intended_rate_counts_a_named_index_postgres_did_not_request() -> None:
    postgres = scan("o")
    agent = scan("o", "Index Scan", index="orders_pkey")
    realized = scan("o", "Index Scan", index="orders_date_idx")

    evaluation = evaluate_plans(postgres, agent, force_result(realized, requested=agent))

    # The access method survived, but the agent's explicit index choice did not.
    assert [str(gap.decision) for gap in evaluation.realizability.gaps] == ["index(o)"]
    assert (
        evaluation.intended.numerator,
        evaluation.intended.denominator,
        evaluation.intended.rate,
    ) == (1, 2, 0.5)
