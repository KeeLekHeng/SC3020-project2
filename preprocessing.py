"""
Read query plans and prepare them for comparison.

The project compares PostgreSQL's plan for a query with a plan proposed by an AI agent.
This module defines PlanNode, the single representation both plans are converted into,
and three functions:

- unique_aliases() rewrites a query so that no relation name is read twice, which
  hints cannot tell apart.
- get_postgres_plan() runs EXPLAIN and returns PostgreSQL's plan as a PlanNode tree,
  optionally with measured times (ANALYZE), hints, or both.
- build_hints() turns a PlanNode tree into pg_hint_plan hints that ask PostgreSQL to use
  that plan. comparison.force_plan() plans the query with them, which this project
  calls forcing the plan.

A plan from another source, such as the AI agent, is built into PlanNode from field
names, so comparison.py works on one form for every source.

The AI agent's side:

- get_agent_plan() asks an LLM for its plan for a query and returns it as a PlanNode.
  The answer must match AgentAnswer: a flat list of AgentPlanNode operators, each
  naming its parent, with the subset of PlanNode's fields the agent decides.
  validate_agent_plan() checks an answer, and every problem goes back to the agent in a
  retry message.
- describe_database() reads the schema, statistics and indexes the agent plans with.
"""

import json
import os
import re
import textwrap
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, NamedTuple

import anthropic
import psycopg
import sqlglot
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from pydantic.json_schema import GenerateJsonSchema
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

# Parent relationships of subqueries that PostgreSQL plans separately.
SEPARATE_PLANS = ("InitPlan", "SubPlan")


class PlanNode(BaseModel):
    """
    One operator in a query execution plan, independent of its source.

    Child operators are reached three ways:

    - children: every child, in EXPLAIN order, including separately planned subqueries
    - inputs: only the children that feed rows in, outer first for a join
    - subplans: only the subqueries, marked by parent_relationship "InitPlan" or
      "SubPlan"

    Fields that do not apply, or that the source does not provide, are None, an empty
    tuple or False.

    Build a node from field names, as in PlanNode(node_type="Seq Scan"), or from a
    PostgreSQL EXPLAIN JSON node with PlanNode.model_validate(). Each field's validation
    alias names its EXPLAIN key; where AliasChoices lists several, the first key present
    wins. Unknown keys are ignored, so newer PostgreSQL versions still parse.

    A hand-built plan, such as the AI agent's, must follow the same conventions: outer
    child first, subqueries marked by parent_relationship, and every table read named by
    its alias, because hints and comparisons refer to tables by alias.
    """

    model_config = ConfigDict(frozen=True, validate_by_name=True)

    node_type: str = Field(
        validation_alias="Node Type",
        description='Operator name in PostgreSQL\'s vocabulary, e.g. "Hash Join".',
    )
    children: tuple["PlanNode", ...] = Field(
        (), validation_alias="Plans", description="All child operators in EXPLAIN order, subqueries included."
    )
    parent_relationship: str | None = Field(
        None,
        validation_alias="Parent Relationship",
        description='Role under the parent, e.g. "Outer", "Inner", "Member" '
        '(of an Append), "Subquery", "InitPlan" or "SubPlan".',
    )
    subplan_name: str | None = Field(
        None,
        validation_alias="Subplan Name",
        description='Name of the subquery or CTE this node computes, e.g. "CTE big".',
    )

    relation: str | None = Field(
        None, validation_alias="Relation Name", description="Table read by a scan."
    )
    alias: str | None = Field(
        None,
        validation_alias="Alias",
        description="Name the query uses for the scanned table, CTE or subquery.",
    )
    index_name: str | None = Field(
        None,
        validation_alias="Index Name",
        description="Index used by an index or bitmap scan.",
    )
    cte_name: str | None = Field(
        None, validation_alias="CTE Name", description='CTE read by a "CTE Scan".'
    )
    function_name: str | None = Field(
        None,
        validation_alias="Function Name",
        description='Function read by a "Function Scan".',
    )

    operation: str | None = Field(
        None,
        validation_alias=AliasChoices("Command", "Operation"),
        description='Set operation ("Intersect", "Except", ...) or table '
        'change ("Insert", ...).',
    )
    join_type: str | None = Field(
        None,
        validation_alias="Join Type",
        description='"Inner", "Left", "Full", "Right", "Semi" or "Anti".',
    )
    strategy: str | None = Field(
        None,
        validation_alias="Strategy",
        description='Aggregate or set operation strategy: "Plain", "Sorted", '
        '"Hashed" or "Mixed".',
    )
    partial_mode: str | None = Field(
        None,
        validation_alias="Partial Mode",
        description='Aggregate stage: "Simple", "Partial" or "Finalize".',
    )
    parallel_aware: bool = Field(
        False,
        validation_alias="Parallel Aware",
        description="True when the operator splits its work across parallel workers.",
    )
    workers_planned: int | None = Field(
        None,
        validation_alias="Workers Planned",
        description="Workers a Gather or Gather Merge node plans to start.",
    )

    join_condition: str | None = Field(
        None,
        validation_alias=AliasChoices("Hash Cond", "Merge Cond"),
        description="Join condition of a hash or merge join.",
    )
    index_condition: str | None = Field(
        None,
        validation_alias=AliasChoices("Index Cond", "Recheck Cond"),
        description="Condition evaluated through an index, or rechecked by a "
        "bitmap heap scan.",
    )
    join_filter: str | None = Field(
        None,
        validation_alias="Join Filter",
        description="Extra condition a join checks on each matching row pair.",
    )
    filter: str | None = Field(
        None,
        validation_alias="Filter",
        description="Condition that removes rows after they are produced.",
    )
    sort_keys: tuple[str, ...] = Field(
        (), validation_alias="Sort Key", description="Sort columns."
    )
    group_keys: tuple[str, ...] = Field(
        (), validation_alias="Group Key", description="Grouping columns."
    )

    estimated_rows: float | None = Field(
        None,
        validation_alias="Plan Rows",
        description="Rows the planner expects the operator to return.",
    )
    startup_cost: float | None = Field(
        None,
        validation_alias="Startup Cost",
        description="Planner cost before the first row is returned.",
    )
    total_cost: float | None = Field(
        None,
        validation_alias="Total Cost",
        description="Planner cost to return all rows.",
    )

    actual_rows: float | None = Field(
        None,
        validation_alias="Actual Rows",
        description="EXPLAIN ANALYZE only: rows returned, averaged per loop.",
    )
    actual_loops: int | None = Field(
        None,
        validation_alias="Actual Loops",
        description="EXPLAIN ANALYZE only: times the operator ran.",
    )
    actual_total_time_ms: float | None = Field(
        None,
        validation_alias="Actual Total Time",
        description="EXPLAIN ANALYZE only: milliseconds, averaged per loop.",
    )

    planning_time_ms: float | None = Field(
        None,
        validation_alias="Planning Time",
        description="EXPLAIN ANALYZE only: planning time in ms, set on the root by "
        "get_postgres_plan().",
    )
    execution_time_ms: float | None = Field(
        None,
        validation_alias="Execution Time",
        description="EXPLAIN ANALYZE only: execution time in ms, set on the root by "
        "get_postgres_plan().",
    )

    reason: str | None = Field(
        None, description="The AI agent's stated reason for choosing this operator."
    )

    @property
    def inputs(self) -> tuple["PlanNode", ...]:
        """Children that feed rows into this operator, outer first."""
        return tuple(c for c in self.children if c.parent_relationship not in SEPARATE_PLANS)

    @property
    def subplans(self) -> tuple["PlanNode", ...]:
        """Children that are subqueries planned separately (InitPlan, SubPlan)."""
        return tuple(c for c in self.children if c.parent_relationship in SEPARATE_PLANS)

    def relations(self) -> set[str]:
        """Aliases of the relations read in this subtree, outside InitPlans and SubPlans."""
        return {
            n.alias
            for n in self.walk(subplans=False)
            if n.alias and n.node_type != "Subquery Scan"
        }

    def walk(self, subplans: bool = True) -> Iterator["PlanNode"]:
        """
        Yield this node and every node below it, parents first.

        With subplans=False, separately planned subqueries are skipped.
        """
        yield self

        for child in self.children if subplans else self.inputs:
            yield from child.walk(subplans)

    def __str__(self) -> str:
        """Indented plan tree, one operator per line, with alias and index names."""
        details = ", ".join(name for name in (self.alias, self.index_name) if name)
        line = self.node_type + (f" ({details})" if details else "")
        children = (textwrap.indent(str(child), "  ") for child in self.children)

        return "\n".join([line, *children])


def get_postgres_plan(
    connection: psycopg.Connection,
    query: str,
    analyze: bool = False,
    hints: str = "",
) -> PlanNode:
    """
    Return PostgreSQL's execution plan for a query as a PlanNode tree.

    query must be one SQL statement without its own hint comment. analyze=True runs it
    exactly as given, so use a read-only connection; it fills the actual_* fields and
    sets planning_time_ms and execution_time_ms on the root node.

    hints come from build_hints().

    Non-empty hints first load pg_hint_plan for the rest of the session. Loading raises
    psycopg.errors.UndefinedFile if pg_hint_plan is not installed, and
    psycopg.errors.InsufficientPrivilege if the database user is not a superuser. After
    either, the caller must roll back the connection before using it again.
    """
    if hints:
        connection.execute("LOAD 'pg_hint_plan'")

    options = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
    prefix = f"/*+ {hints} */ " if hints else ""
    statement = f"{prefix}EXPLAIN ({options}) {query}"

    # psycopg's type hints accept str only as literal SQL text. Encoding the
    # user's SQL to bytes satisfies them and sends the statement unchanged.
    row = connection.execute(statement.encode(connection.info.encoding)).fetchone()
    if row is None:
        raise RuntimeError("EXPLAIN returned no rows")

    explain = row[0][0]
    times = {key: explain[key] for key in ("Planning Time", "Execution Time") if key in explain}

    return PlanNode.model_validate({**explain["Plan"], **times})


# PlanNode operator names and the pg_hint_plan hints that force them.
SCAN_HINTS = {
    "Seq Scan": "SeqScan",
    "Index Scan": "IndexScan",
    "Index Only Scan": "IndexOnlyScan",
    "Bitmap Heap Scan": "BitmapScan",
}
JOIN_HINTS = {
    "Hash Join": "HashJoin",
    "Merge Join": "MergeJoin",
    "Nested Loop": "NestLoop",
}
GATHER = ("Gather", "Gather Merge")
# join_collapse_limit and from_collapse_limit for a forced plan, above the relations any
# join here reads. At the default 8, a larger join is planned in pieces, and from
# geqo_threshold (12) relations by GEQO; either way PostgreSQL ignores Leading hints.
# PostgreSQL 18 and later would also need enable_self_join_elimination off, or the
# merged join may lose relations the plan has.
COLLAPSE_LIMIT = 100
SORTED_INPUT = {
    "Sort", "Incremental Sort", "Merge Join", "Merge Append", "Unique", "Group",
    "WindowAgg", "Gather Merge",
}

# Planner settings, each with a test for whether a node uses the operator it controls.
# build_hints() switches off, for the whole query, every operator the plan never uses.
SWITCH_OFFS: dict[str, Callable[[PlanNode], bool]] = {
    "enable_seqscan": lambda n: n.node_type == "Seq Scan",
    # enable_indexscan off also penalizes index-only scans.
    "enable_indexscan": lambda n: n.node_type in ("Index Scan", "Index Only Scan"),
    "enable_indexonlyscan": lambda n: n.node_type == "Index Only Scan",
    "enable_bitmapscan": lambda n: n.node_type == "Bitmap Heap Scan",
    "enable_tidscan": lambda n: n.node_type in ("Tid Scan", "Tid Range Scan"),
    "enable_hashjoin": lambda n: n.node_type == "Hash Join",
    "enable_mergejoin": lambda n: n.node_type == "Merge Join",
    "enable_nestloop": lambda n: n.node_type == "Nested Loop",
    # Hashed aggregates, set operations and grouping sets.
    "enable_hashagg": lambda n: n.strategy in ("Hashed", "Mixed"),
    # Operators that need sorted input keep sorting on, because a plan, especially an
    # AI plan, can leave out the Sort below them.
    "enable_sort": lambda n: n.strategy == "Sorted" or n.node_type in SORTED_INPUT,
    "enable_incremental_sort": lambda n: n.node_type == "Incremental Sort",
    "enable_material": lambda n: n.node_type == "Materialize",
    "enable_memoize": lambda n: n.node_type == "Memoize",
    "enable_gathermerge": lambda n: n.node_type == "Gather Merge",
    "enable_parallel_hash": lambda n: n.node_type == "Hash" and n.parallel_aware,
    "enable_parallel_append": lambda n: n.node_type == "Append" and n.parallel_aware,
}

# Operators that pass rows through within one query level. Any other operator without
# an alias, such as an Aggregate under a join, heads a subquery whose alias EXPLAIN
# dropped, or makes the rows of a pulled-up IN subquery unique; build_hints() tells
# which from the query.
PASS_THROUGH = {"Hash", "Sort", "Incremental Sort", "Materialize", "Memoize", *GATHER}

# A join tree: an alias for one relation, or (outer tree, inner tree, whether the join
# is an outer join).
JoinTree = str | tuple
OUTER_JOINS = ("Left", "Right", "Full")

# A query's subqueries, each as (aliases of the relations inside, its name in hints or
# None for a pulled-up IN or EXISTS, whether PostgreSQL keeps it whole with exactly
# these relations); see _subqueries(). A FROM subquery or CTE is listed twice, without
# and with the tables of the IN and EXISTS subqueries PostgreSQL pulls up into it,
# because EXPLAIN shows those tables inside it; only the second can be kept whole.
Subqueries = list[tuple[frozenset[str], str | None, bool]]

# Where a subquery is a FROM item rather than an IN, EXISTS or scalar subquery.
FROM_ITEM = (exp.From, exp.Join, exp.Lateral)

# Clauses that make PostgreSQL plan a subquery whole instead of pulling it up into the
# query around it.
NOT_PULLED_UP = ("group", "having", "distinct", "order", "limit", "offset", "with_")

# PostgreSQL cuts names to 63 bytes (NAMEDATALEN - 1), and EXPLAIN shows them cut.
NAME_LIMIT = 63


def build_hints(
    plan: PlanNode, query: str, switch_offs: bool = True, leading: bool = True
) -> str:
    """
    Return pg_hint_plan hints that ask PostgreSQL to use this plan.

    query is the SQL the plan is for. From it, build_hints learns the names of
    subqueries (see _subqueries()), so joins over them get hints; which aliases the
    query writes, so c_1 is not taken for a generated name; and which aliases exist, so
    a scan under a misnamed alias is refused. If sqlglot cannot parse query, these steps
    are skipped.

    The hints fix:

    - the method and index of every Seq Scan, Index Scan, Index Only Scan and Bitmap
      Heap Scan
    - the method of every Hash Join, Merge Join and Nested Loop, and NoMemoize for a
      Nested Loop whose inner side has no Memoize
    - unless leading=False, the join order and the outer and inner sides. A query
      level is the joins PostgreSQL orders as one problem: they stop at named
      relations, at a FULL JOIN's inputs and at any operator that is neither a join
      nor a pass-through operator, and what lies below is a level of its own, though
      it still appears nested in the hint above. pg_hint_plan follows only the last
      Leading hint of a level, so each level's hint comes after those of the levels
      below it. A level that pg_hint_plan cannot order whole gets a hint for each part
      it can (see _orderable()), largest last, so the largest part is followed. A
      level whose top join gets no join hint gets no Leading hint, because
      pg_hint_plan disables every method for a join without one above a Leading hint.
    - for a plan with joins, join_collapse_limit and from_collapse_limit of
      COLLAPSE_LIMIT, with GEQO off, so PostgreSQL plans each join as one and the
      Leading hint applies
    - which scans run in parallel, and with how many workers; a plan without a Gather
      also gets Set(max_parallel_workers_per_gather 0), which turns parallelism off for
      the whole query
    - for a plan with a Gather, parallel_setup_cost and parallel_tuple_cost of 0: a
      Parallel hint zeroes them only while its own scan is planned, so a Gather right
      above that scan would otherwise look cheaper than one above a join or aggregate
    - unless switch_offs=False, every operator in SWITCH_OFFS that the plan never uses
      is switched off for the whole query, such as hashed aggregation in a plan with
      only sorted aggregates

    Hints are requests, not commands: PostgreSQL silently skips any it cannot satisfy,
    so force with comparison.force_plan(), which checks the result.

    Limitations:

    - Not hinted per node, so left to the planner: other scan types (CTE, Function,
      Subquery, Values), aggregate, sort, set operation and Materialize choices, a
      Memoize the plan wants, which pg_hint_plan can allow but not demand, and where a
      Gather is placed, such as a two-stage parallel aggregate. pg_hint_plan has no
      hint for these, and a Set hint applies to the whole query: a switch-off only
      removes an operator the plan never uses, so a plan with one hashed and one sorted
      aggregate cannot fix which is which.
    - A switched-off operator the plan needs but does not show, such as a Materialize
      under a Nested Loop whose inner side is a join, adds PostgreSQL's cost for a
      disabled operator, 1e10, to every candidate plan. The planner then cannot compare
      their real costs and may break other hints; comparison.force_plan() then retries
      without switch-offs.
    - Hints name tables by alias. A node without an alias gets no scan hint. A join over
      an unnamed operator, such as an Append over partitions or one of two IN
      subqueries that both group, gets no join or Leading hint; only the switch-offs
      limit its method.
    - A name used twice, such as an unaliased lineitem in both the query and a subquery,
      appears in EXPLAIN as lineitem and lineitem_1. pg_hint_plan ignores the generated
      name and applies the lineitem hint to every use, so when a hint would need the
      generated name, build_hints raises ValueError: when a join reads it, or its scan
      differs from the first use's. Planning the query from unique_aliases() avoids
      this, except for a NOT MATERIALIZED CTE used twice, whose tables PostgreSQL
      copies, and for a view that reads a table under an alias the query also uses.
    - A parallel scan with no Gather above it gets no Parallel hint, because its worker
      count is unknown. Under an Append, PostgreSQL sets the worker count itself and
      ignores the one in the hint.
    - pg_hint_plan follows one Leading hint per query level, and cannot fix the sides
      of a join whose inner side contains a LEFT, RIGHT or FULL JOIN, such as the
      outer of two nested FULL JOINs or a join of two LEFT JOIN results. That join and
      the joins above it keep their method and relations, but the planner picks their
      outer side; of the parts below, only the largest has its sides forced. EXPLAIN
      does not tell an anti join made from a LEFT JOIN from one made from NOT EXISTS,
      so the first is not detected and is penalized under a Leading hint;
      comparison.force_plan() then retries without them.
    - Forced plans' costs are not comparable with unforced ones: scan and Parallel hints
      and the zero parallel costs all change them.
    - PostgreSQL cannot follow a plan it has no path for, for example an index or bitmap
      scan without a usable condition, a nested loop for a FULL JOIN, some outer and
      anti join side swaps, or a parallel scan inside a nested loop.

    Raises ValueError when the plan cannot be forced: a name used twice, as above, a
    name that cannot be written safely in a hint comment, or a scan under an alias the
    query lacks.
    """
    hints: list[str] = []
    levels: list[JoinTree] = []
    parsed = _parse(query)
    relations = {t.alias_or_name: t.name for t in _relations(parsed)} if parsed else {}

    subqueries = _subqueries(parsed) if parsed else []
    hinted: set[str] = set()
    nodes = list(plan.walk())

    # EXPLAIN names the second use of a name lineitem_1, which hints cannot reach: the
    # lineitem hints apply to every use. An alias the query writes itself, such as c_1,
    # is not such a name.
    aliases = {n.alias for n in nodes if n.alias}
    written = set(relations) | {alias for _, alias, _ in subqueries if alias}
    generated = {
        alias: match.group(1)
        for alias in aliases - written
        if (match := re.fullmatch(r"(.+)_\d+", alias)) and match.group(1) in aliases
    }
    copies: list[tuple[str, list[str]]] = []
    # A generated name of something other than a table, such as a second VALUES list,
    # is no second use of anything: a join over it simply goes without hints.
    unnamed = {n.alias for n in nodes if n.alias in generated and not n.relation}
    parallel = any(n.node_type in GATHER for n in nodes)
    if parallel:
        hints += ["Set(parallel_setup_cost 0)", "Set(parallel_tuple_cost 0)"]
    else:
        hints.append("Set(max_parallel_workers_per_gather 0)")

    if switch_offs:
        hints += [
            f"Set({setting} off)"
            for setting, uses in SWITCH_OFFS.items()
            if not any(uses(node) for node in nodes)
        ]

    def visit(node: PlanNode, ordered: bool, workers: int | None) -> None:
        """
        Add hints for node and its subtree.

        ordered is True below the top join of a query level, which alone decides the
        level's Leading hint, even when it gets no join hint and the level gets none.
        workers is the worker count of the nearest Gather above
        node; subqueries start without one.
        """
        if node.node_type in GATHER:
            workers = node.workers_planned

        if node.node_type in SCAN_HINTS and node.alias:
            indexes = [node.index_name] if node.index_name else [
                n.index_name
                for n in node.walk(subplans=False)
                if n.node_type == "Bitmap Index Scan" and n.index_name
            ]
            alias = _hint_name(generated.get(node.alias, node.alias))
            names = " ".join([alias, *map(_hint_name, indexes)])
            scan = [f"{SCAN_HINTS[node.node_type]}({names})"]

            if node.parallel_aware:
                # Without a Gather above, the worker count is unknown, so the
                # planner decides.
                if workers:
                    scan.append(f"Parallel({alias} {workers} hard)")
            elif parallel:
                # Otherwise PostgreSQL may also run this scan in parallel.
                scan.append(f"Parallel({alias} 0 hard)")

            if node.alias in generated:
                copies.append((node.alias, scan))
            else:
                hints.extend(scan)

        tree = _join_tree(node, subqueries) if node.node_type in JOIN_HINTS else None
        if tree and unnamed.isdisjoint(flat := _flatten(tree)):
            hinted.update(flat)
            joined = " ".join(_hint_name(a) for a in flat)
            hints.append(f"{JOIN_HINTS[node.node_type]}({joined})")
            # pg_hint_plan can forbid a Memoize under one join but cannot demand one.
            if node.node_type == "Nested Loop" and node.inputs[1].node_type != "Memoize":
                hints.append(f"NoMemoize({joined})")

            if not ordered:
                levels.append(tree)

        # Inputs of a join or pass-through operator stay in the same query
        # level, so a Leading hint above still covers them; anything else
        # starts a new level that needs its own. PostgreSQL never reorders
        # tables across a FULL JOIN, so the joins below one are planned apart.
        same_level = node.node_type in PASS_THROUGH or (
            node.node_type in JOIN_HINTS and node.join_type != "Full"
        )
        for child in node.inputs:
            visit(child, (ordered or node.node_type in JOIN_HINTS) and same_level, workers)

        # A subquery is planned on its own, so it starts without an order or
        # a Gather.
        for subplan in node.subplans:
            visit(subplan, False, None)

    visit(plan, ordered=False, workers=None)

    # pg_hint_plan reports all but one Leading hint of a join problem as duplicates,
    # yet the joins below a FULL JOIN keep their order only when their hint comes
    # first, so emit the deepest scope first. A level that cannot be ordered whole
    # repeats the hints of the levels below it; keep the deepest copy.
    if leading:
        parts = [f"Leading({_format_leading(t)})" for tree in levels for t in _orderable(tree)]
        hints += dict.fromkeys(reversed(parts))

    if levels:
        limits = [f"Set({limit}_collapse_limit {COLLAPSE_LIMIT})" for limit in ("join", "from")]
        hints = [*limits, "Set(geqo off)", *hints]

    # A generated name is harmless while no join names it and its scan wants the hints
    # the first use already has, as in the two index scans of min() and max().
    blocked = sorted(
        (hinted & generated.keys())
        | {alias for alias, scan in copies if not set(scan) <= set(hints)}
    )
    if blocked:
        raise ValueError(
            f"Cannot force this plan: hints cannot reach {', '.join(blocked)}, a name "
            "PostgreSQL generated for a table read twice under one name."
        )

    # A scan of a query table under another alias would get a hint that matches nothing.
    tables = set(relations.values())
    unknown = sorted(
        n.alias
        for n in nodes
        if n.node_type in SCAN_HINTS
        and n.alias
        and n.alias not in relations
        and n.alias not in generated
        and n.relation in tables
    )
    if unknown:
        raise ValueError(
            f"Cannot force this plan: {', '.join(unknown)} is not an alias in the "
            "query."
        )

    return " ".join(hints)


def _join_tree(node: PlanNode, subqueries: Subqueries, top: bool = True) -> JoinTree | None:
    """
    Join tree of the relations under node, or None if one is unnamed.

    top is True for the join the tree is built for, which is never named after a
    subquery, though it may be all that subquery holds.
    """
    # A subquery PostgreSQL keeps whole is one relation under its own name, even when
    # EXPLAIN shows only the scan or join inside it, as for OFFSET 0.
    if not top and any(kept for _, _, kept in subqueries):
        below = node.relations()
        whole = {name for inside, name, kept in subqueries if kept and inside == below}
        if len(whole) == 1:
            return whole.pop()

    if node.alias:
        return node.alias

    inputs = node.inputs

    if node.node_type in JOIN_HINTS and len(inputs) == 2:
        outer, inner = (_join_tree(child, subqueries, False) for child in inputs)
        return (outer, inner, node.join_type in OUTER_JOINS) if outer and inner else None

    if node.node_type in PASS_THROUGH and len(inputs) == 1:
        return _join_tree(inputs[0], subqueries, False)

    # Any other unnamed operator heads a subquery whose alias EXPLAIN dropped: name it
    # after the one subquery that reads exactly the relations below it. Over an IN or
    # EXISTS subquery that PostgreSQL pulled up, an Aggregate or Unique only makes the
    # rows unique, and the relations below join this level directly.
    below = node.relations()
    names = {name for inside, name, _ in subqueries if inside == below}
    if names == {None} and node.node_type in ("Aggregate", "Unique") and len(inputs) == 1:
        return _join_tree(inputs[0], subqueries, False)

    return names.pop() if below and len(names) == 1 else None


def _subqueries(parsed: exp.Expr) -> Subqueries:
    """
    Return the subqueries of parsed that PostgreSQL may join as one relation.

    A FROM or LATERAL subquery goes by its alias, and a CTE read once by the name it
    is read under, because PostgreSQL plans it in place. An IN or = ANY subquery that
    PostgreSQL keeps whole is named ANY_subquery; two of them share that name, so
    neither is listed. Any other IN or EXISTS subquery that _pulled_up() is pulled up
    into the query around it and has no name.
    """
    found: Subqueries = []
    sublinks: Subqueries = []

    for query in parsed.find_all(exp.Subquery, exp.Exists):
        parent, kept = query.parent, _kept_whole(query.this)
        if isinstance(parent, FROM_ITEM):
            # A LATERAL subquery's alias sits on the Lateral around it.
            alias = parent.alias if isinstance(parent, exp.Lateral) else query.alias
            if alias:
                found += [
                    (_own_relations(query), alias, False),
                    (_own_relations(query, True), alias, kept),
                ]
        elif not _pulled_up(query):
            continue
        elif not kept:
            found.append((_own_relations(query, True), None, False))
        elif isinstance(query, exp.Subquery):
            sublinks.append((_own_relations(query), "ANY_subquery", True))

    for cte in parsed.find_all(exp.CTE):
        uses = [t for t in parsed.find_all(exp.Table) if t.name == cte.alias and not t.db]
        if len(uses) == 1:
            name, body = uses[0].alias_or_name, cte.this
            found += [
                (_own_relations(body), name, False),
                (_own_relations(body, True), name, _kept_whole(body)),
            ]

    return found + sublinks if len(sublinks) == 1 else found


def _kept_whole(query: exp.Expr) -> bool:
    """Whether PostgreSQL plans query as a unit instead of pulling it up."""
    return (
        not isinstance(query, exp.Select)
        or any(query.args.get(clause) for clause in NOT_PULLED_UP)
        or any(f.parent_select is query for f in query.find_all(exp.AggFunc, exp.Window))
    )


def _pulled_up(sublink: exp.Expr) -> bool:
    """
    Whether PostgreSQL may join an IN, = ANY or EXISTS subquery into the query around it.

    It does for an EXISTS, NOT EXISTS, IN or = ANY ANDed into a WHERE or ON condition.
    A NOT IN, or one under OR, stays a SubPlan. So does an IN that refers to the query
    around it, which is not detected here.
    """
    if isinstance(sublink, exp.Exists):
        term = sublink.parent if isinstance(sublink.parent, exp.Not) else sublink
    elif isinstance(sublink.parent, exp.In):
        term = sublink.parent
    elif isinstance(sublink.parent, exp.Any) and isinstance(sublink.parent.parent, exp.EQ):
        term = sublink.parent.parent
    else:
        return False

    while isinstance(term.parent, (exp.And, exp.Paren)):
        term = term.parent

    return isinstance(term.parent, (exp.Where, exp.Join))


def _own_relations(query: exp.Expr, sublinks: bool = False) -> frozenset[str]:
    """
    Aliases of the relations query's own level reads, as EXPLAIN shows them.

    Those in an IN, EXISTS or scalar subquery inside it belong to another level. With
    sublinks=True, those of the IN and EXISTS subqueries that PostgreSQL can pull up
    are counted too. A VALUES list is shown as *VALUES*.
    """
    inner = {
        id(t)
        for q in query.find_all(exp.Subquery, exp.Exists)
        if q is not query
        and not isinstance(q.parent, FROM_ITEM)
        and not (sublinks and _pulled_up(q) and not _kept_whole(q.this))
        for t in q.find_all(exp.Table, exp.Values)
    }
    values = [v for v in query.find_all(exp.Values) if isinstance(v.parent, (exp.From, exp.Join))]
    return frozenset(
        t.alias_or_name if isinstance(t, exp.Table) else "*VALUES*"
        for t in [*_relations(query), *values]
        if id(t) not in inner
    )


def _flatten(tree: JoinTree) -> list[str]:
    """Aliases in a join tree, outer before inner."""
    if isinstance(tree, str):
        return [tree]

    outer, inner, _ = tree
    return _flatten(outer) + _flatten(inner)


def _has_outer_join(tree: JoinTree) -> bool:
    """Whether tree contains a LEFT, RIGHT or FULL JOIN."""
    return not isinstance(tree, str) and (tree[2] or any(map(_has_outer_join, tree[:2])))


def _orderable(tree: JoinTree) -> list[JoinTree]:
    """
    Return the join trees within tree that a Leading hint can order, largest first.

    PostgreSQL 16 counts an outer join among the relations of the side that contains
    it, and pg_hint_plan 1.6 compares a pair's inner side with base relations only. So
    a pair whose inner side contains an outer join never matches, and pg_hint_plan
    penalizes every way to join it. Such a pair, and the joins above it, are left out;
    their join hints still fix which relations they join and how.
    """
    if isinstance(tree, str):
        return []

    outer, inner, _ = tree
    parts = _orderable(outer) + _orderable(inner)
    whole = [side for side in (outer, inner) if not isinstance(side, str)]
    if _has_outer_join(inner) or parts != whole:
        return sorted(parts, key=lambda part: -len(_flatten(part)))

    return [tree]


def _format_leading(tree: JoinTree) -> str:
    """Write a join tree as a Leading() argument, e.g. ((a b) c)."""
    if isinstance(tree, str):
        return _hint_name(tree)

    outer, inner, _ = tree
    return f"({_format_leading(outer)} {_format_leading(inner)})"



def unique_aliases(query: str) -> str:
    """
    Return query with its own alias for every reused relation name.

    EXPLAIN names a table read twice under one name as lineitem and lineitem_1, which
    hints cannot reach (see build_hints()). This keeps the outermost use and gives each
    other use an alias such as lineitem2, then requalifies its columns. It edits only
    those names, so the rest of the text is unchanged. A query without reused names, or
    one sqlglot cannot parse, is returned unchanged. So is a reused name that also
    appears bare, as in count(nation), where it may mean the table's whole row, unless
    the query also names an output column so; a query that uses one name both ways is
    renamed anyway.
    """
    parsed = _parse(query)
    if parsed is None:
        return query

    tables = _relations(parsed)
    taken = {t.alias_or_name for t in tables} | {
        a.name for a in parsed.find_all(exp.TableAlias)
    }
    # A qualified column refers to the nearest enclosing query block that declares its
    # qualifier, so record which blocks declare which names.
    # A subquery or VALUES alias can hide a table's name.
    declared = {(id(t.parent_select), t.alias_or_name) for t in tables} | {
        (id(a.parent_select), a.name) for a in parsed.find_all(exp.TableAlias)
    }
    # Whether a bare name means a table's whole row depends on the columns in scope,
    # which are unknown here. A name given to an output column, as in n_name AS nation,
    # is taken to mean that column.
    outputs = {alias.alias for alias in parsed.find_all(exp.Alias)}
    bare = {c.name for c in parsed.find_all(exp.Column) if not c.table} - outputs
    seen: set[str] = set()
    renames: dict[tuple[int, str], str] = {}
    edits: list[tuple[int, int, str]] = []

    for table in tables:
        name = table.alias_or_name
        if name not in seen or name in bare:
            seen.add(name)
            continue

        number = 2
        while True:
            # Cut the name so that it fits in NAME_LIMIT bytes with its number.
            new = _truncate(name, NAME_LIMIT - len(str(number))) + str(number)
            if new not in taken:
                break

            number += 1

        taken.add(new)
        renames[id(table.parent_select), name] = new
        written = exp.to_identifier(new).sql(dialect="postgres")
        if alias := table.args.get("alias"):
            edits.append((*_span(alias.this), written))
        else:
            end = _span(table.this)[1]
            edits.append((end, end, f" {written}"))

    renamed = {name for _, name in renames}
    for column in parsed.find_all(exp.Column):
        if column.table not in renamed:
            continue

        block = column.parent_select
        while block is not None and (id(block), column.table) not in declared:
            block = block.parent_select

        if block is not None and (new := renames.get((id(block), column.table))):
            # A qualified column, as in tpch.public.lineitem.x, loses its catalog and
            # schema too.
            first = column.args.get("catalog") or column.args.get("db") or column.args["table"]
            start, end = _span(first)[0], _span(column.args["table"])[1]
            edits.append((start, end, exp.to_identifier(new).sql(dialect="postgres")))

    for start, end, text in sorted(edits, reverse=True):
        query = query[:start] + text + query[end:]

    return query


def _span(identifier: exp.Expr) -> tuple[int, int]:
    """Start and end offsets of identifier, quotes included, in the parsed text."""
    return identifier.meta["start"], identifier.meta["end"] + 1


def _parse(query: str) -> exp.Expr | None:
    """
    Parse query with sqlglot, or return None if it is unparsable.

    Names are normalized as PostgreSQL does, lowercased unless quoted and cut to
    NAME_LIMIT bytes, so they match EXPLAIN's aliases.
    """
    try:
        parsed = sqlglot.parse_one(query, read="postgres")
    except SqlglotError:
        return None

    parsed = normalize_identifiers(parsed, "postgres")
    for identifier in parsed.find_all(exp.Identifier):
        identifier.set("this", _truncate(identifier.name))

    return parsed



def _truncate(name: str, limit: int = NAME_LIMIT) -> str:
    """Cut name to limit bytes of UTF-8 without splitting a character."""
    return name.encode()[:limit].decode(errors="ignore")


def _relations(parsed: exp.Expr) -> list[exp.Table]:
    """Relations read in FROM and JOIN clauses, outermost first."""
    found = []
    for table in parsed.find_all(exp.Table):
        # sqlglot wraps a parenthesised join, as in FROM (a JOIN b ON ...), in a
        # Subquery around its first table, once per pair of parentheses.
        parent = table.parent
        while isinstance(parent, exp.Subquery):
            parent = parent.parent

        if isinstance(parent, (exp.From, exp.Join)):
            found.append(table)

    return found



def _hint_name(name: str) -> str:
    """Quote a relation, alias or index name for use inside a hint."""
    # PostgreSQL's comments nest, so an opening mark is as unsafe as a closing one.
    if "*/" in name or "/*" in name:
        raise ValueError(f"Name cannot appear in a hint comment: {name!r}")

    if re.fullmatch(r"[a-z_][a-z0-9_$]*", name):
        return name

    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# The AI agent's plan
# ---------------------------------------------------------------------------

# Model that proposes the agent's plan; its key comes from api_key().
AGENT_MODEL = "claude-haiku-5-5"
AGENT_MAX_TOKENS = 64_000
AGENT_MAX_ATTEMPTS = 3
AGENT_FIRST_EFFORT = "low"
AGENT_RETRY_EFFORT = "medium"

# Operators the agent may use. All are PostgreSQL names, so the agent's plan, the
# PostgreSQL plan and the hinted plan share one vocabulary. Every scan and join here can
# be forced by build_hints(); Gather and Gather Merge let the agent decide parallelism.
AgentNodeType = Literal[
    "Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
    "Hash Join", "Merge Join", "Nested Loop",
    "Gather", "Gather Merge",
    "Hash", "Sort", "Aggregate", "Limit", "Materialize", "Unique",
    "Subquery Scan", "CTE Scan", "Append", "SetOp", "WindowAgg", "Result",
]
AgentJoinType = Literal["Inner", "Left", "Right", "Full", "Semi", "Anti", "Right Anti"]
AgentStrategy = Literal["Plain", "Sorted", "Hashed", "Mixed"]

# Operators allowed in one answer: far above any real plan, and few enough that the
# deepest possible tree stays inside pydantic's nesting limit of 255 levels and
# Python's recursion limit.
AGENT_MAX_NODES = 200

# Scans that name an index.
INDEXED = ("Index Scan", "Index Only Scan", "Bitmap Heap Scan")

# Number of inputs, as (min, max), each agent operator takes; max None means no limit.
# Subqueries marked InitPlan or SubPlan are not inputs, so they are not counted.
AGENT_INPUT_COUNTS: dict[str, tuple[int, int | None]] = {
    **{t: (0, 0) for t in SCAN_HINTS},
    **{t: (2, 2) for t in JOIN_HINTS},
    **{t: (1, 1) for t in GATHER},
    "Hash": (1, 1), "Sort": (1, 1), "Aggregate": (1, 1), "Limit": (1, 1),
    "Materialize": (1, 1), "Unique": (1, 1), "Subquery Scan": (1, 1),
    "WindowAgg": (1, 1), "CTE Scan": (0, 0), "Append": (1, None), "SetOp": (1, None),
    "Result": (0, 1),
}

# Fields only some operators may have, and fields some operators must have. Only
# fields that hints or comparison read are policed.
AGENT_FIELD_OWNERS: dict[str, tuple[str, ...]] = {
    "relation": tuple(SCAN_HINTS),
    "index_name": INDEXED,
    "cte_name": ("CTE Scan",),
    "join_type": tuple(JOIN_HINTS),
    "strategy": ("Aggregate", "SetOp"),
    "workers_planned": GATHER,
}
AGENT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    **{t: ("relation", "alias") for t in SCAN_HINTS},
    **{t: ("join_type",) for t in JOIN_HINTS},
    "CTE Scan": ("cte_name", "alias"),
    "Subquery Scan": ("alias",),
    "Aggregate": ("strategy",),
}

# Planner settings shown to the agent, because they shape which plan is cheapest: what
# memory and workers a plan may use, what a random page costs beside a sequential one,
# and when going parallel pays.
AGENT_SETTINGS = (
    "max_parallel_workers_per_gather", "work_mem", "effective_cache_size",
    "seq_page_cost", "random_page_cost",
    "parallel_setup_cost", "parallel_tuple_cost", "min_parallel_table_scan_size",
)

# Column statistics shown to the agent, beyond row and distinct counts:
# - common values only when skewed: the most common covers at least MIN_COMMON of the
#   rows and is over SKEW times as frequent as an even spread would give. Otherwise the
#   distinct count says it all, and on a column with many values the sampled common
#   values are noise.
# - a range, only for a numeric or date column the query compares with <, >, <=, >= or
#   BETWEEN, since only those conditions need it
# - correlation only when its size is over CORRELATED: the table is stored close to the
#   column's order, so an index range scan reads it almost sequentially
SKEW = 2
MIN_COMMON = 0.01
COMMON_VALUES = 5
CORRELATED = 0.5
RANGE_CATEGORIES = ("N", "D")  # PostgreSQL type categories: numeric, date and time
RANGE_CONDITIONS = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)


class AgentPlanNode(BaseModel):
    """
    One operator in the AI agent's plan: its place in the tree and the subset of
    PlanNode the agent decides.

    The agent writes the tree as a flat list (see AgentAnswer), because in a nested
    tree it loses count of the closing brackets. id and parent place the operator; the
    other field names match PlanNode's. Costs, row counts and timings are left out,
    because the agent is asked for its choices, not for estimates. The field
    descriptions, and the description in model_config instead of this docstring, are
    part of the JSON Schema in the agent's prompt.

    Limitations, each there so that build_hints() can force the plan:

    - Every table alias the query reads is scanned exactly once (see
      _agent_database_errors()). The plan cannot read one alias twice or leave one
      out, as PostgreSQL's own plans sometimes do.
    - Only the operators in AgentNodeType can be chosen. Memoize and Incremental
      Sort, for example, cannot.
    - Only tables can be scanned. A function or a VALUES list in FROM has no operator
      here, and a view is treated as a table although hints for it have no effect.
    - An answer holds at most AGENT_MAX_NODES operators.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"description": "One operator of the plan."},
    )

    id: int = Field(description="This operator's number: 1, 2, 3, ... in list order.")
    parent: int | None = Field(
        description="The id of the operator this one is a child of; null on the root.",
        # _LeanSchema drops the null of optional fields, but this one is required.
        json_schema_extra={"type": ["integer", "null"]},
    )
    node_type: AgentNodeType = Field(description="PostgreSQL operator name.")
    parent_relationship: Literal["InitPlan", "SubPlan"] | None = Field(
        None,
        description='Only on a separately planned subquery: "InitPlan" if it runs once '
        '(uncorrelated), "SubPlan" if it runs again for each row of the operator that '
        "uses it (correlated). Leave it out on inputs.",
    )
    subplan_name: str | None = Field(
        None,
        description='Name of a separately planned subquery, e.g. "SubPlan 1", or '
        '"CTE <name>" for a CTE.',
    )
    relation: str | None = Field(None, description="Table read. Scans only.")
    alias: str | None = Field(
        None,
        description="The table's alias exactly as the query writes it, or the table "
        "name if the query gives none. Scans, CTE Scan and Subquery Scan only.",
    )
    index_name: str | None = Field(
        None,
        description="One of the table's listed indexes. Index Scan, Index Only Scan "
        "and Bitmap Heap Scan only.",
    )
    cte_name: str | None = Field(None, description="CTE read. CTE Scan only.")
    join_type: AgentJoinType | None = Field(None, description="Joins only.")
    strategy: AgentStrategy | None = Field(
        None,
        description="Aggregate (and SetOp) only: Plain without GROUP BY, Sorted for "
        "input sorted by the group keys, Hashed for a hash table, Mixed for grouping "
        "sets.",
    )
    parallel_aware: bool = Field(
        False,
        description="True when this operator splits its work across parallel workers. "
        "Only below a Gather or Gather Merge.",
    )
    workers_planned: int | None = Field(
        None, description="Gather and Gather Merge only: parallel workers to start."
    )
    join_condition: str | None = Field(
        None, description="Hash Join or Merge Join condition."
    )
    index_condition: str | None = Field(
        None, description="Condition the index is searched with."
    )
    join_filter: str | None = Field(
        None, description="Nested Loop condition checked on each row pair."
    )
    filter: str | None = Field(
        None, description="Condition that removes rows after they are read."
    )
    sort_keys: tuple[str, ...] = Field((), description="Sort only: sort columns.")
    group_keys: tuple[str, ...] = Field((), description="Aggregate only: group columns.")
    reason: str | None = Field(
        None, description="One short sentence on why this operator was chosen."
    )


class AgentAnswer(BaseModel):
    """
    The whole answer the agent must give: a summary of its strategy and its plan.

    The plan is a tree written as a list: the root first, every operator before its
    children, each naming its parent by id.
    """

    # A stray field beside summary and nodes, such as a note, carries no plan, so it is
    # ignored. The schema in the prompt still tells the agent not to add one.
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "The answer: a summary and the plan's operators.",
            "additionalProperties": False,
        },
    )

    summary: str = Field("", description="One or two sentences on the overall strategy.")
    nodes: tuple[AgentPlanNode, ...] = Field(
        min_length=1,
        max_length=AGENT_MAX_NODES,
        description="The plan's operators, the root first, each before its children.",
    )

    def to_plan_node(self) -> PlanNode:
        """
        Return the plan as a PlanNode tree, ready for build_hints() or comparison.

        The answer must come from validate_agent_plan(), which checks the ids and
        parents this relies on.
        """
        nodes = {
            node.id: node.model_dump(exclude={"id", "parent"}, exclude_defaults=True)
            for node in self.nodes
        }
        for node in self.nodes:
            if node.parent is not None:
                nodes[node.parent].setdefault("children", []).append(nodes[node.id])

        return PlanNode.model_validate(nodes[self.nodes[0].id])


class AgentPlanError(ValueError):
    """
    The agent's answer is unusable.

    errors lists every problem found, worded so they can go back to the agent in one
    retry message; answer is the agent's raw text.
    """

    def __init__(
        self,
        errors: list[str],
        answer: str = "",
        attempts: tuple["AgentAttempt", ...] = (),
    ) -> None:
        self.errors = errors
        self.answer = answer
        self.attempts = attempts or (
            (AgentAttempt(answer, tuple(errors)),) if answer else ()
        )
        problems = "\n".join(f"- {e}" for e in errors)
        super().__init__(f"The agent's plan is invalid:\n{problems}")


class AgentResponseError(RuntimeError):
    """The model returned a terminal response that cannot produce a plan."""

    def __init__(self, message: str, attempts: tuple["AgentAttempt", ...]) -> None:
        self.attempts = attempts
        super().__init__(message)


class AgentRequestError(RuntimeError):
    """An agent API request failed after zero or more completed response attempts."""

    def __init__(self, message: str, attempts: tuple["AgentAttempt", ...]) -> None:
        self.attempts = attempts
        super().__init__(message)


class DatabaseDescription(NamedTuple):
    """What the agent is told about the database for one query, from describe_database()."""

    text: str
    indexes: dict[str, set[str]]
    """Index names of each table, keyed by table name."""


class AgentAttempt(NamedTuple):
    """One textual model response and the validation problems it produced."""

    response: str
    validation_problems: tuple[str, ...]


class AgentResult(NamedTuple):
    """
    The agent's accepted plan.

    query is the text the agent planned, after unique_aliases(). Plan the PostgreSQL
    side and force the agent's plan with this text, because the plan's aliases refer to
    it. answer is the agent's raw final answer. problems lists, for each rejected answer
    before it, the errors sent back. attempts retains every textual response in order,
    paired with its validation outcome; hidden thinking and SDK response objects are
    deliberately excluded.
    """

    plan: PlanNode
    summary: str
    query: str
    answer: str
    problems: list[list[str]]
    attempts: tuple[AgentAttempt, ...] = ()


def validate_agent_plan(output: str) -> AgentAnswer:
    """
    Check the agent's answer and return it as an AgentAnswer.

    output is the agent's text. Anything around its first JSON object, such as a
    Markdown fence, is ignored, and so is a field beside summary and nodes. Raises
    AgentPlanError listing the problems, one stage at a time: JSON and schema errors,
    then ids and parents that do not form one tree (ids may skip numbers, but not
    repeat), then the structural rules (input counts, required and misplaced fields,
    reused aliases, parallel operators outside a Gather). Operators are named by id, as
    in "node 3".
    """
    try:
        data, _ = json.JSONDecoder().raw_decode(output, max(output.find("{"), 0))
    except json.JSONDecodeError as error:
        raise AgentPlanError(
            [
                f"The answer is not valid JSON: {error}. Write one operator per line, "
                "and check that every { and [ is closed in order."
            ],
            output,
        ) from None

    try:
        answer = AgentAnswer.model_validate(data)
    except ValidationError as error:
        raise AgentPlanError(
            [_format_error(e, data) for e in error.errors(include_url=False)], output
        ) from None

    errors: list[str] = []
    earlier: set[int] = set()
    for node in answer.nodes:
        if node.id in earlier:
            errors.append(f"node {node.id}: an earlier operator already has this id.")
        elif not earlier and node.parent is not None:
            errors.append(
                f"node {node.id}: the first operator is the root, so its parent must be null."
            )
        elif earlier and node.parent is None:
            errors.append(
                f"node {node.id}: only the first operator is the root, so this one needs a parent."
            )
        elif node.parent is not None and node.parent not in earlier:
            errors.append(
                f"node {node.id}: parent {node.parent} is not the id of an earlier operator."
            )
        earlier.add(node.id)

    if errors:
        raise AgentPlanError(errors, output)

    children: dict[int, list[AgentPlanNode]] = {node.id: [] for node in answer.nodes}
    for node in answer.nodes:
        if node.parent is not None:
            children[node.parent].append(node)

    root = answer.nodes[0]
    if root.parent_relationship:
        errors.append(
            f"node {root.id}: the root operator cannot be a subquery (parent_relationship)."
        )

    _check_agent_node(root, children, {}, False, errors)
    if errors:
        raise AgentPlanError(errors, output)

    return answer


def query_relations(query: str) -> list[tuple[str, str, str]] | None:
    """
    Return what the query reads, as (alias, name, kind) per FROM item, outermost first.

    kind is "table" (name is the table), "cte" (name is the CTE) or "subquery" (a FROM
    subquery; name is empty). A function in FROM, such as generate_series(), is left
    out, because the agent has no operator to read it with. Returns None if sqlglot
    cannot parse the query.
    """
    parsed = _parse(query)
    if parsed is None:
        return None

    ctes = {cte.alias for cte in parsed.find_all(exp.CTE)}
    items = [
        (t.alias_or_name, t.name, "cte" if t.name in ctes and not t.db else "table")
        for t in _relations(parsed)
        if t.name
    ]
    for sub in parsed.find_all(exp.Subquery):
        alias = sub.parent.alias if isinstance(sub.parent, exp.Lateral) else sub.alias
        if alias and isinstance(sub.parent, FROM_ITEM):
            items.append((alias, "", "subquery"))

    return items


def describe_database(
    connection: psycopg.Connection, query: str | None = None
) -> DatabaseDescription:
    """
    Describe the database for the agent, read from the catalogs at run time.

    The text gives the PostgreSQL version and AGENT_SETTINGS, then for each table its
    row and page counts and how much of it is all-visible, its columns with types and
    statistics (see _column_stats()), its indexes with their sizes, and its foreign
    keys. No schema is hardcoded.

    With a query, only the tables it reads are described, with only the columns it
    names, and foreign keys only between those tables. A query sqlglot cannot parse
    describes everything.
    """
    parsed = _parse(query) if query else None
    wanted = {t.name for t in _relations(parsed)} if parsed else None
    used = {c.name for c in parsed.find_all(exp.Column)} if parsed else None
    ranged = _range_columns(parsed) if parsed else None

    settings = connection.execute(
        "SELECT name, current_setting(name) FROM unnest(%s::text[]) AS name",
        (list(AGENT_SETTINGS),),
    ).fetchall()
    shown = ", ".join(f"{name}={value}" for name, value in settings)
    # server_version can end with a packaging note, as in "16.4 (Debian 16.4-1)".
    version = (connection.info.parameter_status("server_version") or "").split(" ")[0]
    lines = [f"PostgreSQL {version}", f"Settings: {shown}"]

    tables = [
        row
        for row in connection.execute(
            """
            SELECT c.oid, n.nspname, c.relname, c.reltuples, c.relpages, c.relallvisible
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname !~ '^pg_toast'
            ORDER BY n.nspname, c.relname
            """
        )
        if wanted is None or row[2] in wanted
    ]
    oids = [oid for oid, *_ in tables]
    names = {table for _, _, table, *_ in tables}

    columns: dict[int, list[str]] = {}
    for oid, name, type_name, *stats in connection.execute(
        """
        SELECT a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod),
               c.reltuples, s.n_distinct, s.null_frac, t.typcategory,
               s.most_common_vals::text::text[], s.most_common_freqs,
               s.histogram_bounds::text::text[], s.correlation,
               CASE WHEN t.typlen < 0 THEN s.avg_width END
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        LEFT JOIN pg_stats s
          ON s.schemaname = n.nspname AND s.tablename = c.relname
          AND s.attname = a.attname AND s.inherited = (c.relkind = 'p')
        WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attrelid, a.attnum
        """,
        (oids,),
    ):
        if used is None or name in used:
            details = _column_stats(*stats, show_range=ranged is None or name in ranged)
            suffix = f" ({'; '.join(details)})" if details else ""
            columns.setdefault(oid, []).append(f"{name} {type_name}{suffix}")

    index_text: dict[int, list[str]] = {}
    indexes: dict[str, set[str]] = {}
    for oid, table, index, unique, definition, pages in connection.execute(
        """
        SELECT i.indrelid, t.relname, ic.relname, i.indisunique,
               pg_get_indexdef(i.indexrelid), ic.relpages
        FROM pg_index i
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class t ON t.oid = i.indrelid
        WHERE i.indrelid = ANY(%s)
        ORDER BY i.indrelid, ic.relname
        """,
        (oids,),
    ):
        # "CREATE UNIQUE INDEX x ON t USING btree (a, b)" becomes
        # "x unique btree (a, b), 12 pages".
        method = definition.partition(" USING ")[2]
        index_text.setdefault(oid, []).append(
            f"{index} {'unique ' if unique else ''}{method}, {pages:,} page{'' if pages == 1 else 's'}"
        )
        indexes.setdefault(table, set()).add(index)

    foreign_keys: dict[int, list[str]] = {}
    for oid, target, definition in connection.execute(
        """
        SELECT conrelid, confrelid::regclass::text, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = ANY(%s) AND contype = 'f'
        ORDER BY conrelid, conname
        """,
        (oids,),
    ):
        # Only a key to another described table says anything about this query's joins.
        if target.rpartition(".")[2] in names:
            keys, _, reference = definition.removeprefix("FOREIGN KEY ").partition(" REFERENCES ")
            foreign_keys.setdefault(oid, []).append(f"{keys.strip('()')} references {reference}")

    for oid, schema, table, rows, pages, visible in tables:
        name = table if schema == "public" else f"{schema}.{table}"
        plural = "" if pages == 1 else "s"
        size = f"~{round(rows):,} rows, {pages:,} page{plural}" if rows >= 0 else "size unknown"
        # An Index Only Scan skips the table for rows on all-visible pages.
        if pages:
            size += f", {visible / pages:.0%} all-visible"

        lines.append(f"{name}: {size}")
        for label, items in (
            ("columns used" if used is not None else "columns", columns.get(oid)),
            ("indexes", index_text.get(oid) or ["none"]),
            ("foreign keys", foreign_keys.get(oid)),
        ):
            if items:
                lines.append(f"  {label}: {'; '.join(items)}")

    return DatabaseDescription("\n".join(lines), indexes)


def agent_system_prompt() -> str:
    """
    Return the agent's system prompt: the task, the plan conventions, the answer's JSON
    Schema and a worked example.

    It holds nothing query-specific, so it is identical across queries and cacheable.
    """
    schema = json.dumps(
        AgentAnswer.model_json_schema(schema_generator=_LeanSchema), separators=(",", ":")
    )
    return AGENT_INSTRUCTIONS.format(schema=schema, example=AGENT_EXAMPLE)


def agent_user_prompt(query: str, database: DatabaseDescription) -> str:
    """
    Return the first user message: the database facts for this query, the aliases the
    plan must use (see query_relations()), and the query itself.
    """
    lines = ["Database:", database.text, ""]
    relations = query_relations(query)
    if relations:
        kinds = {"table": "table {}", "cte": "CTE {}", "subquery": "FROM subquery"}
        aliases = ", ".join(f"{a} ({kinds[k].format(n)})" for a, n, k in relations)
        lines += [f"Aliases (use exactly these): {aliases}", ""]

    lines += ["Query:", "```sql", query.strip(), "```"]
    return "\n".join(lines)


def get_agent_plan(
    connection: psycopg.Connection,
    query: str,
    client: anthropic.Anthropic | None = None,
) -> AgentResult:
    """
    Ask the AI agent for its plan for query and return the plan it settles on.

    The query is first rewritten by unique_aliases(); result.query holds the rewritten
    text. The agent is never shown PostgreSQL's plan, and its answer is not checked
    against it. client defaults to an anthropic.Anthropic() with the key from api_key().
    """
    query = unique_aliases(query)
    database = describe_database(connection, query)
    if client is None:
        client = anthropic.Anthropic(api_key=api_key())

    return ask_agent(client, query, database)


def api_key() -> str | None:
    """
    Return the Anthropic API key: ANTHROPIC_API_KEY from the environment, or else from
    the .env file next to this module.

    None lets the SDK look for other credentials, and fail if it finds none.
    """
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key

    env = Path(__file__).resolve().parent / ".env"
    if not env.is_file():
        return None

    for line in env.read_text(encoding="utf-8").splitlines():
        name, _, value = line.strip().removeprefix("export ").partition("=")
        if name.strip() == "ANTHROPIC_API_KEY":
            return value.strip().strip("'\"") or None

    return None


def ask_agent(
    client: anthropic.Anthropic,
    query: str,
    database: DatabaseDescription,
    max_attempts: int = AGENT_MAX_ATTEMPTS,
) -> AgentResult:
    """
    Run the conversation with the agent until it gives a usable plan.

    Each answer is checked by validate_agent_plan(), then against the query and the
    database: the plan must scan exactly the table aliases the query reads
    (query_relations()), every index must exist, and build_hints() must accept the
    plan. All problems go back in one retry message, up to max_attempts answers in all.

    The first answer is asked for at low effort (how hard the model thinks), any retry
    at medium.

    Raises AgentPlanError with the last answer's problems when no answer is usable,
    AgentResponseError when the model declines or is cut off, and AgentRequestError
    with every completed earlier attempt when an API request fails.
    """
    system: list[anthropic.types.TextBlockParam] = [{
        "type": "text",
        "text": agent_system_prompt(),
        "cache_control": {"type": "ephemeral"},
    }]
    messages: list[Any] = [{"role": "user", "content": agent_user_prompt(query, database)}]
    problems: list[list[str]] = []
    attempts: list[AgentAttempt] = []
    errors: list[str] = []
    text = ""

    for attempt in range(1, max_attempts + 1):
        # Streaming avoids HTTP timeouts on long answers.
        try:
            with client.messages.stream(
                model=AGENT_MODEL,
                max_tokens=AGENT_MAX_TOKENS,
                system=system,
                messages=messages,
                output_config={
                    "effort": AGENT_FIRST_EFFORT if attempt == 1 else AGENT_RETRY_EFFORT
                },
            ) as stream:
                response = stream.get_final_message()
        except anthropic.AnthropicError as error:
            raise AgentRequestError(str(error), tuple(attempts)) from error

        text = "".join(
            block.text for block in response.content
            if isinstance(block, anthropic.types.TextBlock)
        )
        if response.stop_reason == "refusal":
            attempts.append(AgentAttempt(text, ()))
            category = response.stop_details.category if response.stop_details else None
            raise AgentResponseError(
                f"The model declined to plan the query ({category}).", tuple(attempts)
            )

        if response.stop_reason == "max_tokens":
            attempts.append(AgentAttempt(text, ()))
            raise AgentResponseError(
                "The agent's answer was cut off at max_tokens.", tuple(attempts)
            )
        try:
            answer = validate_agent_plan(text)
            plan = answer.to_plan_node()
            errors = _agent_database_errors(plan, query, database)
        except AgentPlanError as error:
            errors = error.errors

        attempts.append(AgentAttempt(text, tuple(errors)))

        if not errors:
            return AgentResult(plan, answer.summary, query, text, problems, tuple(attempts))

        problems.append(errors)
        listed = "\n".join(f"- {error}" for error in errors)
        retry = (
            f"Your plan cannot be used yet:\n{listed}\n\n"
            "Fix these and answer again with the complete plan as one JSON object in the "
            "same format."
        )
        # Send the whole reply back unchanged, thinking blocks included.
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": retry},
        ]

    raise AgentPlanError(errors, text, tuple(attempts))


def _agent_database_errors(
    plan: PlanNode, query: str, database: DatabaseDescription
) -> list[str]:
    """
    Problems that only show against the query and database; see ask_agent().

    The aliases come from the query's text, the list the prompt gives the agent, and not
    from PostgreSQL's plan, which can drop a join or read a table twice. So the agent
    cannot write a plan that reads one table twice, as PostgreSQL does for min() and
    max() of one column; hints could not force its second scan anyway (see
    build_hints()). A query sqlglot cannot parse has no such list, so its aliases are
    left to build_hints().
    """
    errors: list[str] = []
    relations = query_relations(query)
    tables = {alias: name for alias, name, kind in relations or () if kind == "table"}
    scanned = {n.alias for n in plan.walk() if n.relation and n.alias}
    missing = tables.keys() - scanned
    extra = scanned - tables.keys() if relations is not None else set()
    if missing:
        errors.append(
            f"The query reads {', '.join(sorted(missing))}, but no scan in the plan has "
            "that alias."
        )

    if extra:
        errors.append(
            f"The plan scans {', '.join(sorted(extra))}, which is not an alias of a "
            "table the query reads."
        )

    for node in plan.walk():
        table = tables.get(node.alias or "")
        if node.relation and table and node.relation != table:
            errors.append(
                f"Scan of {node.alias}: {node.alias} reads the table {table}, so relation "
                f"must be {table!r}, not {node.relation!r}."
            )
            continue

        known = database.indexes.get(node.relation or "", set())
        if node.index_name and node.index_name not in known:
            listed = ", ".join(sorted(known)) or "none"
            errors.append(
                f"Scan of {node.alias}: {node.index_name!r} is not an index on "
                f"{node.relation}. Its indexes are: {listed}."
            )

    if not errors:
        try:
            build_hints(plan, query)
        except ValueError as error:
            errors.append(str(error))

    return errors


def _check_agent_node(
    node: AgentPlanNode,
    children: dict[int, list[AgentPlanNode]],
    aliases: dict[str, str],
    under_gather: bool,
    errors: list[str],
) -> None:
    """
    Add the structural problems of the agent node and its subtree to errors.

    children holds each operator's children by its id; aliases maps each alias seen so
    far to the operator that has it; under_gather means a Gather above node is in the
    same query level.
    """
    path = f"node {node.id}"
    node_type = node.node_type
    inputs = [c for c in children[node.id] if c.parent_relationship not in SEPARATE_PLANS]

    if node_type in AGENT_INPUT_COUNTS:
        low, high = AGENT_INPUT_COUNTS[node_type]
        if len(inputs) < low or (high is not None and len(inputs) > high):
            wanted = (
                str(low) if low == high
                else f"at least {low}" if high is None
                else f"{low} to {high}"
            )
            message = f"{path}: a {node_type} takes {wanted} input(s), but has {len(inputs)}."
            if high is not None and len(inputs) > high:
                message += (
                    ' If a child is a subquery, mark it with parent_relationship "SubPlan" '
                    'or "InitPlan"; it is not an input.'
                )
            errors.append(message)

    for field in AGENT_REQUIRED_FIELDS.get(node_type, ()):
        if not getattr(node, field):
            errors.append(f"{path}: a {node_type} needs {field}.")

    for field, owners in AGENT_FIELD_OWNERS.items():
        if getattr(node, field) is not None and node_type not in owners:
            errors.append(
                f"{path}: {field} only belongs on {', '.join(owners)}, not on a {node_type}."
            )

    alias = node.alias
    if alias:
        if alias in aliases:
            errors.append(f"{path}: alias {alias!r} is already used at {aliases[alias]}.")
        else:
            aliases[alias] = path

    if node.parallel_aware and not under_gather:
        errors.append(f"{path}: parallel_aware is only valid below a Gather or Gather Merge.")

    if node_type in GATHER and under_gather:
        errors.append(f"{path}: a {node_type} cannot sit below another Gather.")

    for child in children[node.id]:
        # A subquery is planned on its own, so it starts outside any Gather.
        subquery = child.parent_relationship in SEPARATE_PLANS
        below = not subquery and (under_gather or node_type in GATHER)
        _check_agent_node(child, children, aliases, below, errors)


def _format_error(error: Any, data: Any) -> str:
    """Word one pydantic error of the answer data with where it is, such as node 3.join_type."""
    place = error["loc"]
    if place[:1] == ("nodes",) and len(place) > 1:
        entry = data["nodes"][place[1]]
        # An entry without a usable id is named by its position in the list.
        number = entry.get("id") if isinstance(entry, dict) else None
        place = (f"node {number if isinstance(number, int) else place[1] + 1}", *place[2:])

    message = "unknown field" if error["type"] == "extra_forbidden" else error["msg"]
    return f"{'.'.join(str(p) for p in place) or 'answer'}: {message}."


def _column_stats(
    rows: float,
    n_distinct: float | None,
    null_frac: float | None,
    category: str,
    common_values: list[str] | None,
    common_freqs: list[float] | None,
    histogram: list[str] | None,
    correlation: float | None,
    width: int | None,
    show_range: bool,
) -> list[str]:
    """
    A column's statistics from pg_stats, worded for the prompt, most important first:
    distinct count, NULL fraction, skewed common values, range (when show_range),
    correlation and, for a type of variable length, the average width. See SKEW for
    which are shown.

    Only raw statistics are given, never an estimate for a condition in the query, so
    the agent reasons from the same inputs as PostgreSQL's planner rather than from its
    conclusions.
    """
    if n_distinct is None:
        return []

    details = []
    distinct = -n_distinct * rows if n_distinct < 0 else n_distinct
    details.append("unique" if n_distinct == -1 else f"~{round(distinct):,} distinct")

    if null_frac:
        details.append(f"{null_frac:.0%} null")

    # Values are cut to 30 characters, so a long text value cannot swamp the prompt.
    top_freq = common_freqs[0] if common_freqs else 0
    if common_values and top_freq >= MIN_COMMON and top_freq > SKEW / max(distinct, 1):
        top = zip(common_values[:COMMON_VALUES], common_freqs or [])
        details.append("most common: " + ", ".join(f"{v[:30]!r} {f:.0%}" for v, f in top))

    if show_range and histogram and category in RANGE_CATEGORIES:
        quartiles = [histogram[round(i * (len(histogram) - 1) / 4)] for i in range(5)]
        details.append(
            f"range {quartiles[0]} to {quartiles[4]}, quartiles {', '.join(quartiles[1:4])}"
        )

    if correlation is not None and abs(correlation) > CORRELATED and distinct >= 2:
        order = "in" if correlation > 0 else "in reverse"
        details.append(f"table stored {order} this column's order")

    if width is not None:
        details.append(f"~{width} bytes")

    return details


def _range_columns(parsed: exp.Expr) -> set[str]:
    """
    Names of the columns the query compares with <, >, <=, >= or BETWEEN.

    A subquery inside a comparison, as in l_quantity < (SELECT ...), is not searched,
    because its columns are compared by its own conditions, if at all.
    """
    return {
        column.name
        for condition in parsed.find_all(*RANGE_CONDITIONS)
        for column in condition.walk(prune=lambda e: isinstance(e, exp.Subquery))
        if isinstance(column, exp.Column)
    }


class _LeanSchema(GenerateJsonSchema):
    """
    JSON Schema without what pydantic adds that does not help the agent: titles,
    defaults, and the null option of optional fields.
    """

    def field_title_should_be_set(self, schema):
        return False

    def nullable_schema(self, schema):
        return self.generate_inner(schema["schema"])

    def default_schema(self, schema):
        return self.generate_inner(schema["schema"])

    def model_schema(self, schema):
        out = super().model_schema(schema)
        out.pop("title", None)
        return out


AGENT_INSTRUCTIONS = """\
You are a query optimizer for a PostgreSQL database. For the query in the user message, \
choose the execution plan you believe runs fastest, using the table sizes, distinct \
counts and indexes given there. Give your own best plan, not a guess at PostgreSQL's.

The plan is forced on PostgreSQL with pg_hint_plan hints, which name tables by alias and \
indexes by name. So use PostgreSQL's operator names, exactly the aliases listed with \
the query, and only listed indexes.

Answer with one JSON object and nothing else, matching this JSON Schema. Leave out \
fields that do not apply.

{schema}

The plan is a tree written as a flat list. "nodes" holds one entry per operator, on one \
line each, the root first and every operator before its children, in the order EXPLAIN \
prints them. Each entry has its own id (1, 2, 3, ...) and parent, the id of the operator \
it is a child of; the root's parent is null. An operator's children are the entries that \
name it as parent, in list order: its inputs first (a join has exactly two, the outer \
input then the inner input), then the subqueries it uses, each marked with \
parent_relationship.

Conventions:
- Scan each listed table alias exactly once, setting relation and alias.
- A join has two inputs, outer first. A Hash Join's inner input is a Hash over the build \
side. A Merge Join needs both inputs sorted on the join keys. A Nested Loop reruns its \
inner input for each outer row, so an inner Index Scan can look up the outer row's value.
- EXISTS or IN can become a Semi join, NOT EXISTS an Anti join. A FROM subquery can be \
merged into the outer joins or kept as a Subquery Scan with its alias.
- A subquery planned separately, such as a scalar subquery, is a child of the operator \
that uses its value, after that operator's inputs, with parent_relationship InitPlan \
(uncorrelated, runs once) or SubPlan (correlated, reruns for each row).
- A CTE used more than once is an InitPlan named "CTE <name>" on the top operator; each \
use is a CTE Scan.
- To run part of the plan in parallel, put it under a Gather or Gather Merge with \
workers_planned, and set parallel_aware on the operators inside that split their work. \
A parallel aggregate is an Aggregate under the Gather and another above it.

Example. Aliases: department (table department), employee (table employee). Query:
SELECT d_name, COUNT(*) FROM department JOIN employee ON d_id = e_department \
WHERE e_hired >= DATE '2020-01-01' GROUP BY d_id, d_name;
Answer:
{example}
"""

# The example reads tables that are not in the TPC-H database, so that no query the
# agent is asked to plan there comes with its answer.
AGENT_EXAMPLE = """\
{"summary": "Hash the small department table, probe with the filtered employees, group per department.",
 "nodes": [
  {"id": 1, "parent": null, "node_type": "Aggregate", "strategy": "Hashed", "group_keys": ["department.d_id", "department.d_name"], "reason": "A hash table groups without sorting."},
  {"id": 2, "parent": 1, "node_type": "Hash Join", "join_type": "Inner", "join_condition": "employee.e_department = department.d_id", "reason": "The small table fits in a hash table."},
  {"id": 3, "parent": 2, "node_type": "Seq Scan", "relation": "employee", "alias": "employee", "filter": "employee.e_hired >= DATE '2020-01-01'", "reason": "Many employees pass the filter, which has no index."},
  {"id": 4, "parent": 2, "node_type": "Hash"},
  {"id": 5, "parent": 4, "node_type": "Seq Scan", "relation": "department", "alias": "department", "reason": "Every department is read."}
 ]}"""
