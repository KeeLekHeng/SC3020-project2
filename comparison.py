"""
Compare query execution plans.

Each plan is reduced to its decisions, one per planner choice, such as the scan method
for a table or the join method for a set of tables. Comparing two plans means comparing
their decisions; see decisions() for what they leave out. force_plan() forces a plan
on PostgreSQL and checks which forced decisions it followed.
"""

from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple

import psycopg

from preprocessing import (
    GATHER,
    INDEXED,
    JOIN_HINTS,
    PASS_THROUGH,
    PlanNode,
    build_hints,
    get_postgres_plan,
)

# Operators that make no decision of their own: a Hash belongs to its Hash Join, a
# Bitmap Index Scan to its heap scan, and a Subquery Scan only passes rows through.
# PostgreSQL drops a Subquery Scan when nothing above needs it, so counting it would
# make two equal plans differ.
FOLDED = {"Hash", "Bitmap Index Scan", "Subquery Scan"}

# Decision kinds that build_hints() can force; realization_gaps() checks only these.
FORCED_KINDS = {"scan", "index", "parallel", "join", "outer"}

# Decision kinds the rates leave out. One choice to run in parallel or not changes the
# parallel decision of every scan, and hints cannot fix where a Gather goes, so these
# would swamp the rates; is_parallel() reports the choice once per plan instead.
PARALLEL_KINDS = {"parallel", "gather"}

# The index choice of an index scan that names no index, as an agent plan may. A hint
# cannot force it, so it is never a request, but it differs from any named index.
UNSPECIFIED = "unspecified"


class Decision(NamedTuple):
    """A planner decision: its kind, such as "join", and the tables it covers."""

    # "scan", "index", "parallel", "join", "outer", "aggregate", "gather", or any other
    # operator's lowercased node_type, such as "sort", "limit" or "materialize".
    kind: str
    # Sorted aliases, such as ("c", "o"), and <Result>-style names for unnamed leaves.
    tables: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.kind}({', '.join(self.tables)})"


class Choice(NamedTuple):
    """What a plan chose for a decision, such as "Hash Join", and its node."""

    # By decision kind:
    # - scan: node_type, such as "Index Scan"
    # - index: the index name, such as "orders_pkey", or "unspecified"
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
class PlanComparison:
    """
    How two plans differ, outside PARALLEL_KINDS: the difference rate, and the decisions
    behind it.
    """

    difference: MetricScore
    differences: tuple[Difference, ...]


@dataclass(frozen=True)
class ForcePlanResult:
    """The selected forcing attempt and all evidence needed to evaluate it."""

    hints: str
    plan: PlanNode
    gaps: tuple[Difference, ...]
    switch_offs: bool
    leading: bool


@dataclass(frozen=True)
class ForcingFailure:
    """A forcing attempt that produced no realized plan."""

    reason: str


@dataclass(frozen=True)
class RealizabilityResult:
    """How many force-eligible agent requests outside PARALLEL_KINDS the realized plan kept."""

    score: MetricScore
    # The requested force-eligible decisions not reproduced, from the forcing outcome.
    gaps: tuple[Difference, ...]


@dataclass(frozen=True)
class PlanEvaluation:
    """
    The four metrics for one query, all leaving out PARALLEL_KINDS. P is PostgreSQL's
    plan, A the agent's, and R the plan PostgreSQL chose when forced with A's hints.

    - proposed.difference: decisions where A differs from P, over the decisions in
      either plan. Lower means A is closer to P.
    - realizability.score: A's force-eligible requests that R kept, over A's requests.
      Higher means forcing reproduced more of A.
    - realized.difference: decisions where R differs from P, over the decisions in
      either plan. Lower means R is closer to P.
    - intended: of A's force-eligible requests that differ from P, those R kept.
      Higher means more of the agent's deliberate changes took effect.
    """

    proposed: PlanComparison
    realizability: RealizabilityResult
    realized: PlanComparison
    # Of the force-eligible decisions where the agent chose differently from PostgreSQL,
    # how many the realized plan kept.
    intended: MetricScore
    forcing: ForcePlanResult | ForcingFailure



def decisions(plan: PlanNode) -> dict[Decision, Choice]:
    """
    Return the planner's decisions in plan.

    A decision names the tables it covers by alias, so the same decision in two plans is
    equal. An unnamed operator with no inputs, such as a Result, counts as a table
    named <Result>. Written as decision: choice:

    - scan(o): the operator reading alias o, such as "Index Scan", and parallel(o):
      "parallel" or "serial"
    - index(o), for an index or bitmap scan only: the index it reads, such as
      "orders_pkey", or "unspecified" if the plan names none, as an agent plan may
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
            found[Decision("scan", (node.alias,))] = Choice(node.node_type, node)
            if node.node_type in INDEXED:
                index = node.index_name or ", ".join(
                    n.index_name
                    for n in node.walk(subplans=False)
                    if n.node_type == "Bitmap Index Scan" and n.index_name
                )
                found[Decision("index", (node.alias,))] = Choice(index or UNSPECIFIED, node)
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
    """Compare two plans over the union of their decisions outside PARALLEL_KINDS."""
    found_a, found_b = decisions(a), decisions(b)
    same = _rated(_compared(found_a, found_b))
    found_differences = tuple(
        Difference(decision, found_a.get(decision), found_b.get(decision))
        for decision, equal in same.items()
        if not equal
    )
    difference = _rate(
        len(found_differences), len(same), "The plans have no decisions to compare."
    )
    return PlanComparison(difference, found_differences)


def is_parallel(plan: PlanNode) -> bool:
    """
    Whether any part of plan, subqueries included, runs in parallel. PostgreSQL runs
    parallel workers only below a Gather or Gather Merge, and the agent's plans are
    validated to do the same.
    """
    return any(node.node_type in GATHER for node in plan.walk())


def realizability(
    requested_plan: PlanNode,
    outcome: ForcePlanResult | ForcingFailure,
) -> RealizabilityResult:
    """
    Measure reproduced force-eligible requests without claiming hint causality.

    outcome must come from forcing requested_plan: its gaps are the unreproduced requests.
    Decisions in PARALLEL_KINDS are left out.
    """
    requested = _rated(_requests(requested_plan))

    if isinstance(outcome, ForcingFailure):
        score = MetricScore(None, len(requested), None, MetricStatus.UNAVAILABLE, outcome.reason)
        return RealizabilityResult(score, ())

    gaps = tuple(gap for gap in outcome.gaps if gap.decision.kind not in PARALLEL_KINDS)
    score = _rate(
        len(requested) - len(gaps),
        len(requested),
        "The agent requested no force-eligible decisions.",
    )
    return RealizabilityResult(score, gaps)


def evaluate_plans(
    postgres_plan: PlanNode,
    agent_plan: PlanNode,
    outcome: ForcePlanResult | ForcingFailure,
) -> PlanEvaluation:
    """Compose proposed, realizability, and realized metrics without database access."""
    proposed = compare_plans(postgres_plan, agent_plan)
    result = realizability(agent_plan, outcome)
    postgres = decisions(postgres_plan)
    requested = _rated(_requests(agent_plan))
    # Unlike the general pairwise metric, count every explicit forceable request here,
    # including a named index that PostgreSQL's plan does not have. This metric asks
    # whether the agent's intended requests survived, not how many structural differences to
    # display without double-counting one access-method change.
    disagreements = [
        decision
        for decision, choice in requested.items()
        if not choices_equal(choice, postgres.get(decision))
    ]

    if isinstance(outcome, ForcingFailure):
        realized = PlanComparison(
            MetricScore(None, None, None, MetricStatus.UNAVAILABLE, outcome.reason), ()
        )
        intended = MetricScore(
            None, len(disagreements), None, MetricStatus.UNAVAILABLE, outcome.reason
        )
    else:
        realized = compare_plans(postgres_plan, outcome.plan)
        missed = {gap.decision for gap in result.gaps}
        intended = _rate(
            sum(decision not in missed for decision in disagreements),
            len(disagreements),
            "The agent chose like PostgreSQL on every force-eligible decision.",
        )

    return PlanEvaluation(proposed, result, realized, intended, outcome)


def differences(a: PlanNode, b: PlanNode) -> list[Difference]:
    """
    Return every decision plans a and b made differently, sorted by decision, including
    the PARALLEL_KINDS that compare_plans() leaves out.
    """
    found_a, found_b = decisions(a), decisions(b)
    return [
        Difference(decision, found_a.get(decision), found_b.get(decision))
        for decision, equal in _compared(found_a, found_b).items()
        if not equal
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
    requested_decisions = _requests(requested)
    forced_decisions = decisions(forced)
    return [
        Difference(decision, requested_decisions[decision], forced_decisions.get(decision))
        for decision in sorted(requested_decisions)
        if not choices_equal(requested_decisions[decision], forced_decisions.get(decision))
    ]


def force_plan(
    connection: psycopg.Connection, query: str, plan: PlanNode
) -> ForcePlanResult:
    """
    Force plan on PostgreSQL and return the selected try: its hints, the plan chosen,
    the realization gaps and its retry settings.

    Two kinds of hint can backfire: switching off an operator that the plan needs
    without showing it, such as a Materialize, and a Leading hint over an anti join
    made from a LEFT JOIN, which build_hints() cannot tell apart (see its limitations).
    So while the forced plan has gaps, this tries again without switch-offs, then with
    switch-offs but without Leading hints, and
    returns the result with the fewest gaps outside PARALLEL_KINDS, which the rates
    count; fewer gaps overall, then the earlier try, break a tie.

    Raises ValueError when build_hints() refuses the plan, and
    psycopg.errors.UndefinedFile or InsufficientPrivilege when pg_hint_plan cannot be
    loaded (see get_postgres_plan()).
    """
    best: ForcePlanResult | None = None
    best_rank = (0, 0)

    for switch_offs, leading in ((True, True), (False, True), (True, False)):
        hints = build_hints(plan, query, switch_offs, leading)
        forced = get_postgres_plan(connection, query, hints=hints)
        gaps = realization_gaps(plan, forced)
        candidate = ForcePlanResult(
            hints=hints,
            plan=forced,
            gaps=tuple(gaps),
            switch_offs=switch_offs,
            leading=leading,
        )
        rank = (sum(gap.decision.kind not in PARALLEL_KINDS for gap in gaps), len(gaps))
        if best is None or rank < best_rank:
            best, best_rank = candidate, rank

        if not gaps:
            break

    assert best is not None
    return best


def try_force_plan(
    connection: psycopg.Connection, query: str, plan: PlanNode
) -> ForcePlanResult | ForcingFailure:
    """force_plan(), with a refused plan or a database error returned as ForcingFailure."""
    try:
        return force_plan(connection, query, plan)
    except ValueError as error:
        return ForcingFailure(str(error))
    except (psycopg.errors.UndefinedFile, psycopg.errors.InsufficientPrivilege) as error:
        connection.rollback()
        return ForcingFailure(f"pg_hint_plan cannot be loaded, so plans cannot be forced: {error}")
    except psycopg.Error as error:
        connection.rollback()
        return ForcingFailure(f"PostgreSQL forcing failed: {error}")


def choices_equal(a: Choice | None, b: Choice | None) -> bool:
    """Whether two plans chose the same; a decision only one plan has never matches."""
    if a is None or b is None:
        return a is b

    return a.value == b.value


def _requests(plan: PlanNode) -> dict[Decision, Choice]:
    """The plan's force-eligible decisions, which hints ask PostgreSQL to follow."""
    return {
        decision: choice
        for decision, choice in decisions(plan).items()
        if decision.kind in FORCED_KINDS and choice.value != UNSPECIFIED
    }


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


def _rate(numerator: int, denominator: int, empty: str) -> MetricScore:
    """A defined fraction, or not_applicable with the reason empty when nothing applies."""
    if not denominator:
        return MetricScore(0, 0, None, MetricStatus.NOT_APPLICABLE, empty)

    return MetricScore(numerator, denominator, numerator / denominator, MetricStatus.DEFINED)


def _compared(a: dict[Decision, Choice], b: dict[Decision, Choice]) -> dict[Decision, bool]:
    """Whether two plans' decisions match, for every decision either plan has."""
    # An index is compared only when both plans scan the table through one, so a
    # different scan method counts once, as scan, and not again as index.
    return {
        decision: choices_equal(a.get(decision), b.get(decision))
        for decision in sorted(a.keys() | b.keys())
        if decision.kind != "index" or (decision in a and decision in b)
    }


def _rated[T](found: dict[Decision, T]) -> dict[Decision, T]:
    """found without the decisions in PARALLEL_KINDS, which the rates leave out."""
    return {
        decision: value
        for decision, value in found.items()
        if decision.kind not in PARALLEL_KINDS
    }
