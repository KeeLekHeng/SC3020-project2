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
    is_parallel,
    realizability,
    realization_gaps,
)
from comparison import differences
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

    assert result.difference.status is MetricStatus.DEFINED
    with pytest.raises(FrozenInstanceError):
        result.difference.rate = 0.5  # type: ignore[misc]


def test_an_unnamed_index_is_not_a_request() -> None:
    agent = scan("o", "Index Scan")
    realized = scan("o", "Index Scan", index="orders_pkey")

    result = realizability(agent, force_result(realized, requested=agent))

    # The realized plan's index is PostgreSQL's choice, not a reproduced request.
    assert (result.score.numerator, result.score.denominator) == (1, 1)


def test_a_named_index_that_falls_back_to_a_seq_scan_is_two_gaps() -> None:
    agent = scan("o", "Index Scan", index="orders_pkey")

    result = realizability(agent, force_result(scan("o"), requested=agent))

    assert [str(gap.decision) for gap in result.gaps] == ["index(o)", "scan(o)"]


def test_pairwise_identical_changed_and_missing_decisions() -> None:
    identical = compare_plans(scan("o"), scan("o"))
    changed = compare_plans(scan("o"), scan("o", "Index Scan"))
    missing = compare_plans(scan("o"), PlanNode(node_type="Result"))

    assert (identical.difference.numerator, identical.difference.denominator) == (0, 1)
    assert identical.difference.rate == 0.0
    assert changed.difference.numerator == 1
    assert [str(item.decision) for item in missing.differences] == [
        "result(<Result>)", "scan(o)"
    ]


def test_empty_pairwise_universe_is_not_applicable() -> None:
    comparison = compare_plans(PlanNode(node_type="Hash"), PlanNode(node_type="Hash"))

    assert comparison.difference.denominator == 0
    assert comparison.difference.status is MetricStatus.NOT_APPLICABLE
    assert comparison.difference.rate is None


def test_rates_leave_out_parallel_and_gather_but_differences_keeps_them() -> None:
    serial = scan("l")
    parallel_scan = scan("l", parallel=True, role="Outer")
    parallel = PlanNode(node_type="Gather", workers_planned=2, children=(parallel_scan,))

    comparison = compare_plans(serial, parallel)

    assert (comparison.difference.numerator, comparison.difference.denominator) == (0, 1)
    assert comparison.differences == ()
    assert {d.decision.kind for d in differences(serial, parallel)} == {"parallel", "gather"}


def test_a_moved_gather_is_no_difference() -> None:
    lineitem = scan("l", parallel=True)
    orders = scan("o")
    early = join(PlanNode(node_type="Gather", children=(lineitem,)), orders)
    late = PlanNode(node_type="Gather", children=(join(lineitem, orders),))

    assert compare_plans(early, late).difference.numerator == 0
    assert len([d for d in differences(early, late) if d.decision.kind == "gather"]) == 2


@pytest.mark.parametrize(
    ("plan", "parallel"),
    [
        (scan("o"), False),
        (PlanNode(node_type="Gather", children=(scan("o", parallel=True),)), True),
        (PlanNode(node_type="Gather Merge", children=(scan("o", parallel=True),)), True),
        (
            PlanNode(
                node_type="Seq Scan",
                alias="c",
                relation="c",
                children=(
                    PlanNode(
                        node_type="Gather",
                        parent_relationship="InitPlan",
                        children=(scan("o", parallel=True),),
                    ),
                ),
            ),
            True,
        ),
    ],
    ids=["serial", "gather", "gather merge", "gather only in an InitPlan"],
)
def test_a_plan_is_parallel_when_it_has_a_gather(plan: PlanNode, parallel: bool) -> None:
    assert is_parallel(plan) is parallel


def test_realizability_ignores_decisions_the_agent_did_not_request() -> None:
    requested = scan("o")
    realized = join(scan("o"), scan("c"))
    outcome = force_result(realized, requested=requested)

    result = realizability(requested, outcome)

    assert (result.score.numerator, result.score.denominator, result.score.rate) == (1, 1, 1.0)
    assert result.gaps == ()


def test_realizability_retains_gaps_without_counting_extras_in_denominator() -> None:
    requested = scan("o")
    realized = scan("o", "Index Scan", index="orders_pkey")
    outcome = force_result(realized, requested=requested)

    result = realizability(requested, outcome)

    assert (result.score.numerator, result.score.denominator) == (0, 1)
    assert result.gaps == outcome.gaps
    assert str(result.gaps[0].decision) == "scan(o)"


def test_parallel_gaps_stay_in_forcing_but_not_in_realizability() -> None:
    requested = PlanNode(node_type="Gather", children=(scan("o", parallel=True),))
    realized = scan("o")
    outcome = force_result(realized, requested=requested)

    result = realizability(requested, outcome)

    assert [str(gap.decision) for gap in realization_gaps(requested, realized)] == ["parallel(o)"]
    assert (result.score.numerator, result.score.denominator) == (1, 1)
    assert result.gaps == ()


def test_intended_is_not_applicable_when_only_parallelism_differs() -> None:
    postgres = PlanNode(node_type="Gather", children=(scan("o", parallel=True),))
    agent = scan("o")

    evaluation = evaluate_plans(postgres, agent, force_result(agent))

    assert evaluation.proposed.difference.numerator == 0
    assert evaluation.intended.status is MetricStatus.NOT_APPLICABLE


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


def test_force_plan_ranks_tries_by_gaps_outside_parallelism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = join(scan("c"), scan("o"))
    # Try 1 misses the join method; try 2 keeps the join but runs both scans in parallel.
    wrong_join = join(scan("c"), scan("o"), "Merge Join")
    parallel_scans = PlanNode(
        node_type="Gather",
        children=(join(scan("c", parallel=True), scan("o", parallel=True)),),
    )
    built = iter(["first", "second", "third"])
    forced = iter([wrong_join, parallel_scans, wrong_join])
    monkeypatch.setattr(comparison, "build_hints", lambda *_args, **_kwargs: next(built))
    monkeypatch.setattr(comparison, "get_postgres_plan", lambda *_args, **_kwargs: next(forced))

    result = comparison.force_plan(object(), "SELECT 1", requested)  # type: ignore[arg-type]

    assert result.hints == "second"
    assert {gap.decision.kind for gap in result.gaps} == {"parallel"}


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

    assert evaluation.proposed.difference.numerator == 1
    # scan and index were requested; neither was reproduced.
    score = evaluation.realizability.score
    assert (score.numerator, score.denominator) == (0, 2)
    assert evaluation.realized.difference.numerator == 0


def test_forcing_failure_preserves_proposal_and_marks_dependent_metrics_unavailable() -> None:
    postgres = scan("o")
    agent = scan("o", "Index Scan")
    evaluation = evaluate_plans(postgres, agent, ForcingFailure("pg_hint_plan unavailable"))

    assert evaluation.proposed.difference.status is MetricStatus.DEFINED
    assert evaluation.proposed.difference.numerator == 1
    assert evaluation.realizability.score.status is MetricStatus.UNAVAILABLE
    assert evaluation.realizability.score.numerator is None
    assert evaluation.realizability.score.denominator == 1
    assert evaluation.realized.difference.status is MetricStatus.UNAVAILABLE
    assert evaluation.realized.difference.numerator is None
    assert evaluation.realized.difference.denominator is None
    assert evaluation.realized.difference.explanation == "pg_hint_plan unavailable"
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
    # The index is one of the counted disagreements, though proposed leaves it out.
    assert [str(decision) for decision in evaluation.disagreements] == ["scan(o)", "index(o)"]
    assert [str(d.decision) for d in evaluation.proposed.differences] == ["scan(o)"]
