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
"""

import re
import textwrap
from typing import Callable, Iterator

import psycopg
import sqlglot
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
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
# PostgreSQL's default join_collapse_limit and from_collapse_limit. A join of more
# relations is planned in pieces, and from geqo_threshold (12) relations by GEQO; either
# way PostgreSQL ignores Leading hints. Assumes the server's default settings.
COLLAPSE_LIMIT = 8
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

# Operators that pass rows through within one query level. Any other
# operator without an alias, such as an Aggregate under a join, is the top of
# a subquery whose alias EXPLAIN dropped; build_hints() recovers it from the query.
PASS_THROUGH = {"Hash", "Sort", "Incremental Sort", "Materialize", "Memoize", *GATHER}

# A join tree: an alias for one relation, or an (outer, inner) pair of trees.
JoinTree = str | tuple

# A query's FROM subqueries, each as (aliases of the relations inside, its alias).
Subqueries = list[tuple[frozenset[str], str]]

# Where a subquery is a FROM item rather than an IN, EXISTS or scalar subquery.
FROM_ITEM = (exp.From, exp.Join, exp.Lateral)

# PostgreSQL cuts names to 63 bytes (NAMEDATALEN - 1), and EXPLAIN shows them cut.
NAME_LIMIT = 63


def build_hints(
    plan: PlanNode, query: str, switch_offs: bool = True, leading: bool = True
) -> str:
    """
    Return pg_hint_plan hints that ask PostgreSQL to use this plan.

    query is the SQL the plan is for. From it, build_hints learns the aliases of FROM
    subqueries, so joins over them get hints; which aliases the query writes, so c_1 is
    not taken for a generated name; and which aliases exist, so a scan under a misnamed
    alias is refused. If sqlglot cannot parse query, these steps are skipped.

    The hints fix:

    - the method and index of every Seq Scan, Index Scan, Index Only Scan and Bitmap
      Heap Scan
    - the method of every Hash Join, Merge Join and Nested Loop
    - unless leading=False, the join order and outer and inner sides, with one Leading
      hint per query level;
      a level's joins stop at named relations, at a FULL JOIN's inputs and at any
      operator that is not a join or a pass-through operator, and what lies below gets
      its own Leading hint
    - for a join of more than COLLAPSE_LIMIT relations, join_collapse_limit and
      from_collapse_limit high enough to plan it as one join, with GEQO off, so the
      Leading hint applies
    - which scans run in parallel, and with how many workers; a plan without a Gather
      also gets Set(max_parallel_workers_per_gather 0), which turns parallelism off for
      the whole query
    - unless switch_offs=False, every operator in SWITCH_OFFS that the plan never uses
      is switched off for the whole query, such as hashed aggregation in a plan with
      only sorted aggregates

    Hints are requests, not commands: PostgreSQL silently skips any it cannot satisfy,
    so force with comparison.force_plan(), which checks the result.

    Limitations:

    - Not hinted per node, so left to the planner: other scan types (CTE, Function,
      Subquery, Values), aggregate, sort, set operation, Materialize and Memoize
      choices, and where a Gather is placed, such as a two-stage parallel aggregate. A
      switch-off only removes an operator the plan never uses, so a plan with one hashed
      and one sorted aggregate cannot fix which is which.
    - A switched-off operator the plan needs but does not show, such as a Materialize
      under a Nested Loop whose inner side is a join, adds PostgreSQL's cost for a
      disabled operator, 1e10, to every candidate plan. The planner then cannot compare
      their real costs and may break other hints; comparison.force_plan() then retries
      without switch-offs.
    - Hints name tables by alias. A node without an alias gets no scan hint. A join over
      an unnamed operator, such as an Append over partitions or an IN subquery turned
      into a join, gets no join or Leading hint; only the switch-offs limit its method.
    - A name used twice, such as an unaliased lineitem in both the query and a subquery,
      appears in EXPLAIN as lineitem and lineitem_1. pg_hint_plan ignores the generated
      name and applies the lineitem hint to every use, so when a hint would need the
      generated name, build_hints raises ValueError. Planning the query from
      unique_aliases() avoids this, except for a NOT MATERIALIZED CTE used twice, whose
      tables PostgreSQL copies.
    - A parallel scan with no Gather above it gets no Parallel hint, because its worker
      count is unknown.
    - pg_hint_plan honours one Leading hint per join problem. In nested FULL JOINs the
      outer FULL JOINs' sides may not be forced, and a join between two outer-join
      results, such as two LEFT JOINs joined together, falls back to a penalized Nested
      Loop under any Leading hint; comparison.force_plan() then retries without them.
    - PostgreSQL cannot follow a plan it has no path for, for example an index or bitmap
      scan without a usable condition, a nested loop for a FULL JOIN, some outer and
      anti join side swaps, or a parallel scan inside a nested loop.

    Raises ValueError when the plan cannot be forced: a name used twice, as above, a
    name that cannot be written safely in a hint comment, or a scan under an alias the
    query lacks.
    """
    hints: list[str] = []
    leading_hints: list[str] = []
    leading_sizes: list[int] = []
    parsed = _parse(query)
    relations = {t.alias_or_name: t.name for t in _relations(parsed)} if parsed else {}

    # Each FROM or LATERAL subquery, with the relations its own level reads; those in
    # an IN, EXISTS or scalar subquery inside it belong to another level. A LATERAL
    # subquery's alias sits on the Lateral around it.
    subqueries: Subqueries = []
    for sub in parsed.find_all(exp.Subquery) if parsed else ():
        alias = sub.parent.alias if isinstance(sub.parent, exp.Lateral) else sub.alias
        if alias and isinstance(sub.parent, FROM_ITEM):
            inner = {
                id(t)
                for q in sub.find_all(exp.Subquery, exp.Exists)
                if not isinstance(q.parent, FROM_ITEM)
                for t in q.find_all(exp.Table)
            }
            own = (t.alias_or_name for t in _relations(sub) if id(t) not in inner)
            subqueries.append((frozenset(own), alias))
    hinted: set[str] = set()
    nodes = list(plan.walk())
    parallel = any(n.node_type in GATHER for n in nodes)
    if not parallel:
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

        ordered means an ancestor's Leading hint already fixes this node's join order
        within the same query level. workers is the worker count of the nearest
        Gather above node; subqueries start without one.
        """
        if node.node_type in GATHER:
            workers = node.workers_planned

        if node.node_type in SCAN_HINTS and node.alias:
            indexes = [node.index_name] if node.index_name else [
                n.index_name
                for n in node.walk(subplans=False)
                if n.node_type == "Bitmap Index Scan" and n.index_name
            ]
            hinted.add(node.alias)
            names = " ".join(_hint_name(name) for name in [node.alias, *indexes])
            hints.append(f"{SCAN_HINTS[node.node_type]}({names})")

            if node.parallel_aware:
                # Without a Gather above, the worker count is unknown, so the
                # planner decides.
                if workers:
                    hints.append(f"Parallel({_hint_name(node.alias)} {workers} hard)")
            elif parallel:
                # Otherwise PostgreSQL may also run this scan in parallel.
                hints.append(f"Parallel({_hint_name(node.alias)} 0 hard)")

        tree = _join_tree(node, subqueries) if node.node_type in JOIN_HINTS else None
        if tree:
            flat = _flatten(tree)
            hinted.update(flat)
            aliases = " ".join(_hint_name(a) for a in flat)
            hints.append(f"{JOIN_HINTS[node.node_type]}({aliases})")

            if not ordered:
                leading_hints.append(f"Leading({_format_leading(tree)})")
                leading_sizes.append(len(flat))

        # Inputs of a join or pass-through operator stay in the same query
        # level, so a Leading hint above still covers them; anything else
        # starts a new level that needs its own. PostgreSQL never reorders
        # tables across a FULL JOIN, so the joins below one are planned apart.
        same_level = node.node_type in PASS_THROUGH or (
            node.node_type in JOIN_HINTS and node.join_type != "Full"
        )
        for child in node.inputs:
            visit(child, (ordered or tree is not None) and same_level, workers)

        # A subquery is planned on its own, so it starts without an order or
        # a Gather.
        for subplan in node.subplans:
            visit(subplan, False, None)

    visit(plan, ordered=False, workers=None)

    # pg_hint_plan reports all but one Leading hint of a join problem as duplicates,
    # yet the joins below a FULL JOIN keep their order only when their hint comes
    # first, so emit the deepest scope first.
    if leading:
        hints += reversed(leading_hints)

    size = max(leading_sizes, default=0)
    if size > COLLAPSE_LIMIT:
        limits = [f"Set({limit}_collapse_limit {size})" for limit in ("join", "from")]
        hints = [*limits, "Set(geqo off)", *hints]

    # EXPLAIN names the second use of a name lineitem_1, which hints cannot reach. An
    # alias the query writes itself, such as c_1, is not such a name.
    aliases = {n.alias for n in nodes if n.alias}
    written = set(relations) | {alias for _, alias in subqueries}
    generated = {
        alias
        for alias in aliases - written
        if (match := re.fullmatch(r"(.+)_\d+", alias)) and match.group(1) in aliases
    }
    blocked = sorted(hinted & generated)
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
        and n.relation in tables
    )
    if unknown:
        raise ValueError(
            f"Cannot force this plan: {', '.join(unknown)} is not an alias in the "
            "query."
        )

    return " ".join(hints)


def _join_tree(node: PlanNode, subqueries: Subqueries) -> JoinTree | None:
    """Join tree of the relations under node, or None if one is unnamed."""
    if node.alias:
        return node.alias

    inputs = node.inputs

    if node.node_type in JOIN_HINTS and len(inputs) == 2:
        outer, inner = (_join_tree(child, subqueries) for child in inputs)
        return (outer, inner) if outer and inner else None

    if node.node_type in PASS_THROUGH and len(inputs) == 1:
        return _join_tree(inputs[0], subqueries)

    # Any other unnamed operator heads a subquery whose alias EXPLAIN dropped: name it
    # after the one FROM subquery that reads exactly the relations below it.
    below = node.relations()
    names = [alias for inside, alias in subqueries if inside == below]

    return names[0] if below and len(names) == 1 else None



def _flatten(tree: JoinTree) -> list[str]:
    """Aliases in a join tree, outer before inner."""
    if isinstance(tree, str):
        return [tree]

    outer, inner = tree
    return _flatten(outer) + _flatten(inner)


def _format_leading(tree: JoinTree) -> str:
    """Write a join tree as a Leading() argument, e.g. ((a b) c)."""
    if isinstance(tree, str):
        return _hint_name(tree)

    outer, inner = tree
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
    tables = parsed.find_all(exp.Table)
    return [t for t in tables if isinstance(t.parent, (exp.From, exp.Join))]



def _hint_name(name: str) -> str:
    """Quote a relation, alias or index name for use inside a hint."""
    # PostgreSQL's comments nest, so an opening mark is as unsafe as a closing one.
    if "*/" in name or "/*" in name:
        raise ValueError(f"Name cannot appear in a hint comment: {name!r}")

    if re.fullmatch(r"[a-z_][a-z0-9_$]*", name):
        return name

    return '"' + name.replace('"', '""') + '"'
