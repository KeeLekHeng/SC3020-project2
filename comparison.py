"""
Compare query execution plans.

Each plan is reduced to its decisions, one per planner choice, such as the scan method
for a table or the join method for a set of tables. Comparing two plans means comparing
their decisions; see decisions() for what they leave out. force_plan() forces a plan
on PostgreSQL and checks which forced decisions it followed.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, NamedTuple

import psycopg

from preprocessing import (
    GATHER,
    JOIN_HINTS,
    PASS_THROUGH,
    PlanNode,
    build_hints_detailed,
    get_postgres_plan,
)

# Operators that make no decision of their own: a Hash belongs to its Hash Join, a
# Bitmap Index Scan to its heap scan, and a Subquery Scan only passes rows through.
# PostgreSQL drops a Subquery Scan when nothing above needs it, so counting it would
# make two equal plans differ.
FOLDED = {"Hash", "Bitmap Index Scan", "Subquery Scan"}

# Decision kinds that build_hints() can force; realization_gaps() checks only these.
FORCED_KINDS = {"scan", "parallel", "join", "outer"}


class Decision(NamedTuple):
    """A planner decision: its kind, such as "join", and the tables it covers."""

    # "scan", "parallel", "join", "outer", "aggregate", "gather", or any other
    # operator's lowercased node_type, such as "sort", "limit" or "materialize".
    kind: str
    # Sorted aliases, such as ("c", "o"), and <Result>-style names for unnamed leaves.
    tables: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.kind}({', '.join(self.tables)})"


class Choice(NamedTuple):
    """What a plan chose for a decision, such as "Hash Join", and its node."""

    # By decision kind:
    # - scan: node_type plus any index, such as "Index Scan on orders_pkey"
    # - parallel: "parallel" or "serial"
    # - join: "Hash Join", "Merge Join" or "Nested Loop"
    # - outer: the outer side's aliases, such as "o" for join(c, o)
    # - aggregate: "Plain", "Sorted", "Hashed" or "Mixed"
    # - gather: "Gather" or "Gather Merge"
    # - other operators: their operation, strategy or sort keys, else "used"
    value: str
    node: PlanNode

    def __str__(self) -> str:
        return self.value


class Difference(NamedTuple):
    """A decision two plans made differently; a or b is None if that plan lacks it."""

    decision: Decision
    a: Choice | None
    b: Choice | None


class MetricStatus(str, Enum):
    """Whether a metric has a value, does not apply, or could not be produced."""

    DEFINED = "defined"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class MetricScore:
    """One transparent metric value with the raw fraction used to calculate it."""

    numerator: int | None
    denominator: int | None
    rate: float | None
    status: MetricStatus
    explanation: str | None = None


@dataclass(frozen=True)
class ComparisonView:
    """Matching and different counts for one decision universe."""

    matching: int
    different: int
    total: int
    difference: MetricScore
    similarity: MetricScore


@dataclass(frozen=True)
class KindComparison:
    """A pairwise comparison restricted to one decision kind."""

    kind: str
    view: ComparisonView


@dataclass(frozen=True)
class DecisionMatch:
    """A decision present with equivalent choices in both plans."""

    decision: Decision
    a: Choice
    b: Choice


@dataclass(frozen=True)
class DecisionChoice:
    """One decision and choice, used in realizability detail lists."""

    decision: Decision
    choice: Choice


@dataclass(frozen=True)
class PlanComparison:
    """All-kinds, core, per-kind, and detailed comparison of two plans."""

    all_kinds: ComparisonView
    core: ComparisonView
    by_kind: tuple[KindComparison, ...]
    matches: tuple[DecisionMatch, ...]
    differences: tuple[Difference, ...]


@dataclass(frozen=True)
class ForcePlanResult:
    """The selected forcing attempt and all evidence needed to evaluate it."""

    hints: str
    plan: PlanNode
    gaps: tuple[Difference, ...]
    targets: frozenset[Decision]
    switch_offs: bool
    leading: bool


@dataclass(frozen=True)
class ForcingFailure:
    """A forcing attempt that produced no realized plan."""

    reason: str


@dataclass(frozen=True)
class RealizabilityResult:
    """How many force-eligible agent requests appear in the realized plan."""

    score: MetricScore
    requested: tuple[DecisionChoice, ...]
    reproduced: tuple[DecisionChoice, ...]
    unmatched: tuple[Difference, ...]
    extra: tuple[DecisionChoice, ...]
    gaps: tuple[Difference, ...]
    targets: tuple[Decision, ...]


@dataclass(frozen=True)
class Attribution:
    """Whether one realized choice can conservatively be credited to the agent."""

    decision: Decision
    choice: Choice
    attributable: bool
    targeted: bool
    reproduced: bool
    has_gap: bool
    reason: str


@dataclass(frozen=True)
class PlanEvaluation:
    """The three plan perspectives and their display/serialization evidence."""

    proposed: PlanComparison
    realizability: RealizabilityResult
    realized: PlanComparison
    attribution: tuple[Attribution, ...]
    forcing: ForcePlanResult | ForcingFailure

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation with stable primitive field names."""
        return {
            "proposed": _comparison_dict(self.proposed),
            "realizability": _realizability_dict(self.realizability),
            "realized": _comparison_dict(self.realized),
            "attribution": [_attribution_dict(item) for item in self.attribution],
            "forcing": _forcing_dict(self.forcing),
        }


def decisions(plan: PlanNode) -> dict[Decision, Choice]:
    """
    Return the planner's decisions in plan.

    A decision names the tables it covers by alias, so the same decision in two plans is
    equal. An unnamed operator with no inputs, such as a Result, counts as a table
    named <Result>. Written as decision: choice:

    - scan(o): the operator reading alias o, such as "Index Scan on orders_pkey", and
      parallel(o): "parallel" or "serial"; a scan that names no index, as a hand-built
      plan may, equals the same scan on any index
    - join(c, o): "Hash Join", and outer(c, o): "o", the outer side
    - aggregate(c, o): its strategy; a parallel aggregate's Partial and Finalize stages
      are one decision, with the Partial stage's strategy, so serial and parallel
      grouping compare by how the rows are grouped
    - gather(l): "Gather" or "Gather Merge", the operator combining parallel workers'
      rows, keyed by the tables below it, so moving it is a different decision
    - any other operator, by its lowercased type: its operation, strategy or sort keys,
      or "used" if it has none, such as sort(o): "o.o_orderdate"

    A decision present in only one plan means only that plan made the choice, for
    example a sort.

    Limitations:

    - Decisions use EXPLAIN's aliases, so two plans compare correctly only when they use
      the same aliases.
    - Two operators of the same type over the same tables make one decision, so only the
      last one is kept.
    - Not compared: worker counts, conditions, filters, costs and row estimates.
    """
    found: dict[Decision, Choice] = {}

    for node in plan.walk():
        # The Partial stage under a Finalize stage's Gather, where the workers group the
        # rows, stands for the whole parallel aggregate; the Finalize method follows from
        # the Gather kind. A Finalize stage without one, as an AI plan may write, stands
        # for itself.
        below = node.inputs[0] if node.partial_mode == "Finalize" and node.inputs else None
        while below and below.node_type in PASS_THROUGH and below.inputs:
            below = below.inputs[0]
        partial_below = below is not None and below.partial_mode == "Partial"
        if node.node_type in FOLDED or partial_below:
            continue

        if node.alias:
            index = node.index_name or ", ".join(
                n.index_name
                for n in node.walk(subplans=False)
                if n.node_type == "Bitmap Index Scan" and n.index_name
            )
            method = node.node_type + (f" on {index}" if index else "")
            found[Decision("scan", (node.alias,))] = Choice(method, node)
            parallel = "parallel" if node.parallel_aware else "serial"
            found[Decision("parallel", (node.alias,))] = Choice(parallel, node)
            continue

        tables = _tables(node)

        if node.node_type in JOIN_HINTS and len(node.inputs) == 2:
            found[Decision("join", tables)] = Choice(node.node_type, node)
            kind, value = "outer", ", ".join(_tables(node.inputs[0]))
        elif node.node_type == "Aggregate":
            kind, value = "aggregate", node.strategy or "Plain"
        elif node.node_type in GATHER:
            kind, value = "gather", node.node_type
        else:
            kind = node.node_type.lower()
            sort_keys = ", ".join(node.sort_keys)
            value = node.operation or node.strategy or sort_keys or "used"

        found[Decision(kind, tables)] = Choice(value, node)

    return found


def compare_plans(a: PlanNode, b: PlanNode) -> PlanComparison:
    """Compare any two plans over the union of their semantic decision keys."""
    found_a, found_b = decisions(a), decisions(b)
    universe = set(found_a) | set(found_b)
    ordered = sorted(universe)
    matching_keys = [
        decision
        for decision in ordered
        if choices_equal(decision, found_a.get(decision), found_b.get(decision))
    ]
    different_keys = [decision for decision in ordered if decision not in matching_keys]

    matches = tuple(
        DecisionMatch(decision, found_a[decision], found_b[decision])
        for decision in matching_keys
    )
    found_differences = tuple(
        Difference(decision, found_a.get(decision), found_b.get(decision))
        for decision in different_keys
    )
    all_kinds = _comparison_view(ordered, found_a, found_b)
    core_keys = [d for d in ordered if d.kind not in {"parallel", "gather"}]
    core = _comparison_view(core_keys, found_a, found_b)
    by_kind = tuple(
        KindComparison(
            kind,
            _comparison_view([d for d in ordered if d.kind == kind], found_a, found_b),
        )
        for kind in sorted({decision.kind for decision in ordered})
    )

    return PlanComparison(all_kinds, core, by_kind, matches, found_differences)


def realizability(
    requested_plan: PlanNode,
    outcome: ForcePlanResult | ForcingFailure,
) -> RealizabilityResult:
    """Measure reproduced force-eligible requests without claiming hint causality."""
    requested_all = decisions(requested_plan)
    requested = {
        decision: choice
        for decision, choice in requested_all.items()
        if decision.kind in FORCED_KINDS
    }
    requested_items = tuple(
        DecisionChoice(decision, requested[decision]) for decision in sorted(requested)
    )

    if isinstance(outcome, ForcingFailure):
        score = MetricScore(
            None,
            len(requested),
            None,
            MetricStatus.UNAVAILABLE,
            outcome.reason,
        )
        return RealizabilityResult(score, requested_items, (), (), (), (), ())

    realized_all = decisions(outcome.plan)
    reproduced_keys = [
        decision
        for decision in sorted(requested)
        if choices_equal(decision, requested[decision], realized_all.get(decision))
    ]
    unmatched_keys = [d for d in sorted(requested) if d not in reproduced_keys]
    extra_keys = [
        decision
        for decision in sorted(realized_all)
        if decision.kind in FORCED_KINDS and decision not in requested
    ]
    denominator = len(requested)
    numerator = len(reproduced_keys)
    if denominator:
        score = MetricScore(
            numerator,
            denominator,
            numerator / denominator,
            MetricStatus.DEFINED,
        )
    else:
        score = MetricScore(
            0,
            0,
            None,
            MetricStatus.NOT_APPLICABLE,
            "The agent requested no force-eligible decisions.",
        )

    return RealizabilityResult(
        score=score,
        requested=requested_items,
        reproduced=tuple(
            DecisionChoice(decision, requested[decision]) for decision in reproduced_keys
        ),
        unmatched=tuple(
            Difference(decision, requested[decision], realized_all.get(decision))
            for decision in unmatched_keys
        ),
        extra=tuple(
            DecisionChoice(decision, realized_all[decision]) for decision in extra_keys
        ),
        gaps=outcome.gaps,
        targets=tuple(sorted(outcome.targets)),
    )


def evaluate_plans(
    postgres_plan: PlanNode,
    agent_plan: PlanNode,
    outcome: ForcePlanResult | ForcingFailure,
) -> PlanEvaluation:
    """Compose proposed, realizability, and realized metrics without database access."""
    proposed = compare_plans(postgres_plan, agent_plan)
    realized = (
        _unavailable_comparison(outcome.reason)
        if isinstance(outcome, ForcingFailure)
        else compare_plans(postgres_plan, outcome.plan)
    )
    result = realizability(agent_plan, outcome)
    attribution = (
        ()
        if isinstance(outcome, ForcingFailure)
        else _attribution(agent_plan, outcome)
    )
    return PlanEvaluation(proposed, result, realized, attribution, outcome)


def differences(a: PlanNode, b: PlanNode) -> list[Difference]:
    """Return every decision plans a and b made differently, sorted by decision."""
    found_a, found_b = decisions(a), decisions(b)

    return [
        Difference(decision, found_a.get(decision), found_b.get(decision))
        for decision in sorted(found_a.keys() | found_b.keys())
        if not choices_equal(decision, found_a.get(decision), found_b.get(decision))
    ]


def realization_gaps(requested: PlanNode, forced: PlanNode) -> list[Difference]:
    """
    Return the realization gaps: the forced decisions PostgreSQL did not follow.

    requested is the plan given to build_hints(), as each Difference's a; forced is the
    plan PostgreSQL chose with those hints, as b. Only the kinds in FORCED_KINDS are
    checked, and only decisions requested by the original plan can be gaps. Extra
    force-eligible decisions introduced by the realized plan are diagnostics, not
    failed requests. An empty result means every requested force-eligible decision was
    reproduced.

    A gap can be a decision no hint asked for, because build_hints() skips some scans
    and joins (see its limitations). Check it against the hints before concluding that
    PostgreSQL refused it.
    """
    requested_decisions = decisions(requested)
    forced_decisions = decisions(forced)
    return [
        Difference(decision, requested_decisions[decision], forced_decisions.get(decision))
        for decision in sorted(requested_decisions)
        if decision.kind in FORCED_KINDS
        and not choices_equal(
            decision,
            requested_decisions[decision],
            forced_decisions.get(decision),
        )
    ]


def force_plan(
    connection: psycopg.Connection, query: str, plan: PlanNode
) -> tuple[str, PlanNode, list[Difference]]:
    """
    Force plan on PostgreSQL and return (hints used, plan chosen, realization gaps).

    Some hints backfire: a switch-off of an operator plan needs but does not show, and
    a Leading hint over an anti join made from a LEFT JOIN, which build_hints() cannot
    tell apart (see its limitations). So while the forced plan has gaps, this tries
    again without switch-offs, then with switch-offs but without Leading hints, and
    returns the result with the fewest gaps; a tie keeps the earlier try.

    Raises ValueError when build_hints() refuses the plan, and
    psycopg.errors.UndefinedFile or InsufficientPrivilege when pg_hint_plan cannot be
    loaded (see get_postgres_plan()).
    """
    result = force_plan_detailed(connection, query, plan)
    return result.hints, result.plan, list(result.gaps)


def force_plan_detailed(
    connection: psycopg.Connection, query: str, plan: PlanNode
) -> ForcePlanResult:
    """Force a plan and retain the selected retry's hints, targets, and settings."""
    best: ForcePlanResult | None = None

    for switch_offs, leading in ((True, True), (False, True), (True, False)):
        built = build_hints_detailed(plan, query, switch_offs, leading)
        forced = get_postgres_plan(connection, query, hints=built.hints)
        gaps = realization_gaps(plan, forced)
        candidate = ForcePlanResult(
            hints=built.hints,
            plan=forced,
            gaps=tuple(gaps),
            targets=frozenset(Decision(target.kind, target.tables) for target in built.targets),
            switch_offs=switch_offs,
            leading=leading,
        )
        if best is None or len(candidate.gaps) < len(best.gaps):
            best = candidate

        if not gaps:
            break

    assert best is not None
    return best


def _tables(node: PlanNode) -> tuple[str, ...]:
    """
    Sorted aliases of the relations below node, outside InitPlans and SubPlans.

    An unnamed operator without inputs, such as a Result, counts as <Result>, so a
    join over it and the join below get different decisions.
    """
    unnamed = {
        f"<{n.node_type}>"
        for n in node.walk(subplans=False)
        if not n.alias and not n.inputs and n.node_type not in FOLDED
    }
    return tuple(sorted(node.relations() | unnamed))


def choices_equal(decision: Decision, a: Choice | None, b: Choice | None) -> bool:
    """Whether two plans chose the same; a scan that names no index matches any index."""
    if a is None or b is None:
        return a is b

    x, y = a.value, b.value
    return x == y or decision.kind == "scan" and (
        x.startswith(f"{y} on ") or y.startswith(f"{x} on ")
    )


def _comparison_view(
    keys: list[Decision],
    found_a: dict[Decision, Choice],
    found_b: dict[Decision, Choice],
) -> ComparisonView:
    """Build counts and rates for an already-selected decision universe."""
    total = len(keys)
    matching = sum(
        choices_equal(decision, found_a.get(decision), found_b.get(decision))
        for decision in keys
    )
    different = total - matching
    explanation = "No comparable decisions existed; the plans are identical on an empty universe."
    difference = MetricScore(
        different,
        total,
        different / total if total else 0.0,
        MetricStatus.DEFINED,
        explanation if not total else None,
    )
    similarity = MetricScore(
        matching,
        total,
        matching / total if total else 1.0,
        MetricStatus.DEFINED,
        explanation if not total else None,
    )
    return ComparisonView(matching, different, total, difference, similarity)


def _unavailable_comparison(reason: str) -> PlanComparison:
    score = MetricScore(None, None, None, MetricStatus.UNAVAILABLE, reason)
    view = ComparisonView(0, 0, 0, score, score)
    return PlanComparison(view, view, (), (), ())


def _attribution(
    agent_plan: PlanNode,
    outcome: ForcePlanResult,
) -> tuple[Attribution, ...]:
    requested = decisions(agent_plan)
    realized = decisions(outcome.plan)
    gap_keys = {gap.decision for gap in outcome.gaps}
    entries: list[Attribution] = []

    for decision in sorted(realized):
        choice = realized[decision]
        requested_choice = requested.get(decision)
        targeted = decision in outcome.targets
        reproduced = (
            requested_choice is not None
            and choices_equal(decision, requested_choice, choice)
        )
        has_gap = decision in gap_keys
        attributable = targeted and reproduced and not has_gap

        if attributable:
            reason = "The selected forcing attempt targeted and reproduced this choice."
        elif decision.kind not in FORCED_KINDS:
            reason = "This surrounding operator kind is selected by PostgreSQL, not directly hinted."
        elif requested_choice is None:
            reason = "The agent did not request this realized decision."
        elif not targeted:
            reason = "The selected forcing attempt emitted no hint targeting this decision."
        elif has_gap or not reproduced:
            reason = "PostgreSQL did not reproduce the agent's targeted choice."
        else:  # Defensive: the conditions above exhaust current attribution states.
            reason = "The decision cannot be conservatively attributed to the agent."

        entries.append(
            Attribution(
                decision,
                choice,
                attributable,
                targeted,
                reproduced,
                has_gap,
                reason,
            )
        )

    return tuple(entries)


def _score_dict(score: MetricScore) -> dict[str, Any]:
    return {
        "numerator": score.numerator,
        "denominator": score.denominator,
        "rate": score.rate,
        "status": score.status.value,
        "explanation": score.explanation,
    }


def _view_dict(view: ComparisonView) -> dict[str, Any]:
    return {
        "matching": view.matching,
        "different": view.different,
        "total": view.total,
        "difference": _score_dict(view.difference),
        "similarity": _score_dict(view.similarity),
    }


def _decision_dict(decision: Decision) -> dict[str, Any]:
    return {"kind": decision.kind, "tables": list(decision.tables)}


def _choice_dict(choice: Choice | None) -> dict[str, str] | None:
    return None if choice is None else {"value": choice.value}


def _difference_dict(difference: Difference) -> dict[str, Any]:
    return {
        "decision": _decision_dict(difference.decision),
        "a": _choice_dict(difference.a),
        "b": _choice_dict(difference.b),
    }


def _decision_choice_dict(item: DecisionChoice) -> dict[str, Any]:
    return {
        "decision": _decision_dict(item.decision),
        "choice": _choice_dict(item.choice),
    }


def _comparison_dict(comparison: PlanComparison) -> dict[str, Any]:
    return {
        "all_kinds": _view_dict(comparison.all_kinds),
        "core": _view_dict(comparison.core),
        "by_kind": [
            {"kind": item.kind, **_view_dict(item.view)} for item in comparison.by_kind
        ],
        "matches": [
            {
                "decision": _decision_dict(match.decision),
                "a": _choice_dict(match.a),
                "b": _choice_dict(match.b),
            }
            for match in comparison.matches
        ],
        "differences": [_difference_dict(item) for item in comparison.differences],
    }


def _realizability_dict(result: RealizabilityResult) -> dict[str, Any]:
    return {
        "score": _score_dict(result.score),
        "requested": [_decision_choice_dict(item) for item in result.requested],
        "reproduced": [_decision_choice_dict(item) for item in result.reproduced],
        "unmatched": [_difference_dict(item) for item in result.unmatched],
        "extra": [_decision_choice_dict(item) for item in result.extra],
        "gaps": [_difference_dict(item) for item in result.gaps],
        "targets": [_decision_dict(item) for item in result.targets],
    }


def _attribution_dict(item: Attribution) -> dict[str, Any]:
    return {
        "decision": _decision_dict(item.decision),
        "choice": _choice_dict(item.choice),
        "attributable": item.attributable,
        "targeted": item.targeted,
        "reproduced": item.reproduced,
        "has_gap": item.has_gap,
        "reason": item.reason,
    }


def _forcing_dict(outcome: ForcePlanResult | ForcingFailure) -> dict[str, Any]:
    if isinstance(outcome, ForcingFailure):
        return {"status": "unavailable", "reason": outcome.reason}

    return {
        "status": "defined",
        "hints": outcome.hints,
        "switch_offs": outcome.switch_offs,
        "leading": outcome.leading,
        "targets": [_decision_dict(item) for item in sorted(outcome.targets)],
        "gaps": [_difference_dict(item) for item in outcome.gaps],
    }
