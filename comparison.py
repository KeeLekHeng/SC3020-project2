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
# Gather to the parallel scans below it, a Bitmap Index Scan to its heap scan, and a
# Subquery Scan only passes rows through. PostgreSQL drops a Subquery Scan when nothing
# above needs it, so counting it would make two equal plans differ.
FOLDED = {"Hash", "Bitmap Index Scan", "Subquery Scan", *GATHER}

# Decision kinds that build_hints() can force; realization_gaps() checks only these.
FORCED_KINDS = {"scan", "parallel", "join", "outer"}


class Decision(NamedTuple):
    """A planner decision: its kind, such as "join", and the tables it covers."""

    # "scan", "parallel", "join", "outer", "aggregate", "partial aggregate",
    # "finalize aggregate", or any other operator's lowercased node_type, such as
    # "sort", "limit" or "materialize".
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
    # - outer: the outer side's aliases, such as "c, o"
    # - aggregates: "Plain", "Sorted", "Hashed" or "Mixed"
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
    - aggregate(c, o): its strategy; parallel stages are partial aggregate(...) and
      finalize aggregate(...)
    - any other operator, by its lowercased type: its operation, strategy or sort keys,
      or "used" if it has none, such as sort(o): "o.o_orderdate"

    A decision present in only one plan means only that plan made the choice, for
    example a sort.

    Limitations:

    - Decisions use EXPLAIN's aliases, so two plans compare correctly only when they use
      the same aliases.
    - Two operators of the same type over the same tables make one decision, so only the
      last one is kept.
    - Not compared: Gather placement and worker counts (parallelism is recorded per
      scan), conditions, filters, costs and row estimates.
    """
    found: dict[Decision, Choice] = {}

    for node in plan.walk():
        if node.node_type in FOLDED:
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
            mode = node.partial_mode or "Simple"
            kind = "aggregate" if mode == "Simple" else f"{mode.lower()} aggregate"
            value = node.strategy or "Plain"
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
    Return the forced decisions PostgreSQL did not follow.

    requested is the plan given to build_hints(), as a; forced is the plan PostgreSQL
    chose with those hints, as b. Only the kinds in FORCED_KINDS are checked. An empty
    result means every forced decision was followed.

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

    A switch-off in build_hints() backfires when plan needs the operator but does not
    show it. So when the forced plan has gaps, this forces again without switch-offs and
    returns the result with fewer gaps; a tie keeps the switch-offs.
    """
    hints = build_hints(plan)
    forced = get_postgres_plan(connection, query, hints=hints)
    gaps = realization_gaps(plan, forced)
    if not gaps:
        return hints, forced, gaps

    fallback_hints = build_hints(plan, switch_offs=False)
    fallback = get_postgres_plan(connection, query, hints=fallback_hints)
    fallback_gaps = realization_gaps(plan, fallback)
    if len(fallback_gaps) < len(gaps):
        return fallback_hints, fallback, fallback_gaps

    return hints, forced, gaps


def _tables(node: PlanNode) -> tuple[str, ...]:
    """Sorted aliases of the tables read below node in its own query level."""
    return tuple(
        sorted(
            {
                n.alias
                for n in node.walk(subplans=False)
                if n.alias and n.node_type not in FOLDED
            }
        )
    )


def _value(choice: Choice | None) -> str | None:
    return choice.value if choice else None
