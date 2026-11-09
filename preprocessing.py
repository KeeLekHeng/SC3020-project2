"""
Read inputs and convert query plans into the project's PlanNode form.

Plans arrive in different formats: PostgreSQL's EXPLAIN JSON (with or
without ANALYZE) and, later, the AI agent's JSON. Each format is parsed into
a tree of PlanNode objects, so the comparison, hint building and drawing code
work on one representation only.
"""

from typing import Iterator

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class PlanNode(BaseModel):
    """One operator in a query execution plan, independent of its source.

    Children follow PostgreSQL's order: for a join, the outer input comes
    first and the inner input second. Fields that do not apply to an
    operator, or that the source does not provide, are None.

    Build a node from field names (PlanNode(node_type="Seq Scan")) or validate
    a PostgreSQL EXPLAIN JSON node, whose keys map to fields through the
    validation aliases below; with several aliases, the first key present
    wins. Unknown keys are ignored, so newer PostgreSQL
    versions that add keys still parse.
    """

    model_config = ConfigDict(frozen=True, validate_by_name=True, validate_by_alias=True)

    node_type: str = Field(
        validation_alias=AliasChoices("node_type", "Node Type"),
        description='Operator name in PostgreSQL\'s vocabulary, e.g. "Hash Join".',
    )
    children: tuple["PlanNode", ...] = Field(
        default=(),
        validation_alias=AliasChoices("children", "Plans"),
        description="Input operators, outer first.",
    )
    parent_relationship: str | None = Field(
        default=None,
        validation_alias=AliasChoices("parent_relationship", "Parent Relationship"),
        description='Role under the parent, e.g. "Outer", "Inner", "Member" '
        '(of an Append), "Subquery", "InitPlan" or "SubPlan".',
    )
    subplan_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("subplan_name", "Subplan Name"),
        description='Name of the subquery or CTE this node computes, e.g. "CTE big".',
    )

    relation: str | None = Field(
        default=None,
        validation_alias=AliasChoices("relation", "Relation Name"),
        description="Table read by a scan.",
    )
    alias: str | None = Field(
        default=None,
        validation_alias=AliasChoices("alias", "Alias"),
        description="Name the query uses for the scanned table, CTE or subquery.",
    )
    index_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("index_name", "Index Name"),
        description="Index used by an index or bitmap scan.",
    )
    cte_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("cte_name", "CTE Name"),
        description='CTE read by a "CTE Scan".',
    )
    function_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("function_name", "Function Name"),
        description='Function read by a "Function Scan".',
    )

    operation: str | None = Field(
        default=None,
        validation_alias=AliasChoices("operation", "Command", "Operation"),
        description='Set operation ("Intersect", "Except", ...) or table '
        'change ("Insert", ...).',
    )
    join_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices("join_type", "Join Type"),
        description='"Inner", "Left", "Full", "Right", "Semi" or "Anti".',
    )
    strategy: str | None = Field(
        default=None,
        validation_alias=AliasChoices("strategy", "Strategy"),
        description='Aggregate or set operation strategy: "Plain", "Sorted", '
        '"Hashed" or "Mixed".',
    )
    partial_mode: str | None = Field(
        default=None,
        validation_alias=AliasChoices("partial_mode", "Partial Mode"),
        description='Aggregate stage: "Simple", "Partial" or "Finalize".',
    )
    parallel_aware: bool = Field(
        default=False,
        validation_alias=AliasChoices("parallel_aware", "Parallel Aware"),
        description="True when the operator itself runs in parallel workers.",
    )
    workers_planned: int | None = Field(
        default=None,
        validation_alias=AliasChoices("workers_planned", "Workers Planned"),
        description="Workers a Gather or Gather Merge node plans to start.",
    )

    join_condition: str | None = Field(
        default=None,
        validation_alias=AliasChoices("join_condition", "Hash Cond", "Merge Cond"),
        description="Join condition of a hash or merge join.",
    )
    index_condition: str | None = Field(
        default=None,
        validation_alias=AliasChoices("index_condition", "Index Cond", "Recheck Cond"),
        description="Condition evaluated through an index, or rechecked by a "
        "bitmap heap scan.",
    )
    join_filter: str | None = Field(
        default=None,
        validation_alias=AliasChoices("join_filter", "Join Filter"),
        description="Extra condition a join checks on each matching row pair.",
    )
    filter: str | None = Field(
        default=None,
        validation_alias=AliasChoices("filter", "Filter"),
        description="Condition that removes rows after they are produced.",
    )
    sort_keys: tuple[str, ...] = Field(
        default=(),
        validation_alias=AliasChoices("sort_keys", "Sort Key"),
        description="Sort columns.",
    )
    group_keys: tuple[str, ...] = Field(
        default=(),
        validation_alias=AliasChoices("group_keys", "Group Key"),
        description="Grouping columns.",
    )

    estimated_rows: float | None = Field(
        default=None,
        validation_alias=AliasChoices("estimated_rows", "Plan Rows"),
        description="Rows the planner expects the operator to return.",
    )
    startup_cost: float | None = Field(
        default=None,
        validation_alias=AliasChoices("startup_cost", "Startup Cost"),
        description="Planner cost before the first row is returned.",
    )
    total_cost: float | None = Field(
        default=None,
        validation_alias=AliasChoices("total_cost", "Total Cost"),
        description="Planner cost to return all rows.",
    )

    actual_rows: float | None = Field(
        default=None,
        validation_alias=AliasChoices("actual_rows", "Actual Rows"),
        description="EXPLAIN ANALYZE only: rows returned, averaged per loop.",
    )
    actual_loops: int | None = Field(
        default=None,
        validation_alias=AliasChoices("actual_loops", "Actual Loops"),
        description="EXPLAIN ANALYZE only: times the operator ran.",
    )
    actual_total_time_ms: float | None = Field(
        default=None,
        validation_alias=AliasChoices("actual_total_time_ms", "Actual Total Time"),
        description="EXPLAIN ANALYZE only: milliseconds, averaged per loop.",
    )

    reason: str | None = Field(
        default=None,
        description="The AI agent's stated reason for choosing this operator.",
    )

    def walk(self) -> Iterator["PlanNode"]:
        """Yield this node and every node below it, parents before children."""
        yield self
        for child in self.children:
            yield from child.walk()


def parse_pg_plan(node: dict) -> PlanNode:
    """Convert one node of PostgreSQL's EXPLAIN (FORMAT JSON) output.

    Pass the object under the "Plan" key; its "Plans" children are converted
    recursively. Works for EXPLAIN and EXPLAIN ANALYZE output.
    """
    return PlanNode.model_validate(node)


def get_postgres_plan(connection, query, analyze=False):
    """
    Return PostgreSQL's execution plan for a query as a PlanNode tree.

    With analyze=True, PostgreSQL runs the query and adds measured rows and
    times. Use a read-only connection then, because the query really
    executes.
    """
    options = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN ({options}) {query}")
        result = cursor.fetchone()[0]

    return parse_pg_plan(result[0]["Plan"])
