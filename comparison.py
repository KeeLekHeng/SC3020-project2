"""
Compare query execution plans.

Each plan is reduced to its decisions, one per planner choice, such as the scan method
for a table or the join method for a set of tables. Comparing two plans means comparing
their decisions; see decisions() for what they leave out. force_plan() forces a plan
on PostgreSQL and checks which forced decisions it followed.
"""

from typing import NamedTuple

import psycopg

from preprocessing import GATHER, JOIN_HINTS, PlanNode, build_hints, get_postgres_plan

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
    # Sorted aliases, such as ("c", "o").
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


def decisions(plan: PlanNode) -> dict[Decision, Choice]:
    """
    Return the planner's decisions in plan.

    A decision names the tables it covers by alias, so the same decision in two plans is
    equal. Written as decision: choice:

    - scan(o): the operator reading alias o, such as "Index Scan on orders_pkey", and
      parallel(o): "parallel" or "serial"
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
        # The Partial stage below, where the workers group the rows, stands for the
        # whole parallel aggregate; the Finalize method follows from the Gather kind.
        if node.node_type in FOLDED or node.partial_mode == "Finalize":
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


def differences(a: PlanNode, b: PlanNode) -> list[Difference]:
    """Return every decision plans a and b made differently, sorted by decision."""
    found_a, found_b = decisions(a), decisions(b)

    return [
        Difference(decision, found_a.get(decision), found_b.get(decision))
        for decision in sorted(found_a.keys() | found_b.keys())
        if _value(found_a.get(decision)) != _value(found_b.get(decision))
    ]


def realization_gaps(requested: PlanNode, forced: PlanNode) -> list[Difference]:
    """
    Return the realization gaps: the forced decisions PostgreSQL did not follow.

    requested is the plan given to build_hints(), as each Difference's a; forced is the
    plan PostgreSQL chose with those hints, as b. Only the kinds in FORCED_KINDS are
    checked. An empty result means every forced decision was followed.

    A gap can be a decision no hint asked for, because build_hints() skips some scans
    and joins (see its limitations). Check it against the hints before concluding that
    PostgreSQL refused it.
    """
    gaps = differences(requested, forced)
    return [d for d in gaps if d.decision.kind in FORCED_KINDS]


def force_plan(
    connection: psycopg.Connection, query: str, plan: PlanNode
) -> tuple[str, PlanNode, list[Difference]]:
    """
    Force plan on PostgreSQL and return (hints used, plan chosen, realization gaps).

    Some hints backfire: a switch-off when plan needs the operator but does not show
    it, and a Leading hint over a join of two outer-join results (see build_hints()).
    So while the forced plan has gaps, this tries again without switch-offs, then
    without Leading hints, and returns the result with the fewest gaps; a tie keeps the
    fuller hints.

    Raises ValueError when build_hints() refuses the plan, and
    psycopg.errors.UndefinedFile or InsufficientPrivilege when pg_hint_plan cannot be
    loaded (see get_postgres_plan()).
    """
    best: tuple[str, PlanNode, list[Difference]] | None = None

    for switch_offs, leading in ((True, True), (False, True), (True, False)):
        hints = build_hints(plan, query, switch_offs, leading)
        forced = get_postgres_plan(connection, query, hints=hints)
        gaps = realization_gaps(plan, forced)
        if best is None or len(gaps) < len(best[2]):
            best = (hints, forced, gaps)

        if not gaps:
            break

    assert best is not None
    return best


def _tables(node: PlanNode) -> tuple[str, ...]:
    """Sorted aliases of the relations below node, outside InitPlans and SubPlans."""
    return tuple(sorted(node.relations()))


def _value(choice: Choice | None) -> str | None:
    return choice.value if choice else None
