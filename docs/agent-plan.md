# The agent's plan: format and limits

The project asks an AI agent for a query plan, forces it onto PostgreSQL, and compares the result with PostgreSQL's own plan. Forcing means planning the query with `pg_hint_plan` hints that name the agent's scans, joins and join order. The [README](../README.md) describes the project and how to run it.

This document covers how the agent writes its plan, which other formats were tried, why this one was chosen, and what the format and the comparison with PostgreSQL cannot do. The code is in `preprocessing.py` (`AgentAnswer`, `AgentPlanNode`, `validate_agent_plan()`, `ask_agent()`, `build_hints()`) and `comparison.py` (`force_plan()`, `differences()`).

## The format

The agent answers with one JSON object: a short summary and a list of operators. A plan is a tree of operators, but the agent writes it as a flat list, where each operator names its parent.

```json
{"summary": "Hash the small department table, probe with the filtered employees, group per department.",
 "nodes": [
  {"id": 1, "parent": null, "node_type": "Aggregate", "strategy": "Hashed", "group_keys": ["department.d_id", "department.d_name"], "reason": "A hash table groups without sorting."},
  {"id": 2, "parent": 1, "node_type": "Hash Join", "join_type": "Inner", "join_condition": "employee.e_department = department.d_id", "reason": "The small table fits in a hash table."},
  {"id": 3, "parent": 2, "node_type": "Seq Scan", "relation": "employee", "alias": "employee", "filter": "employee.e_hired >= DATE '2020-01-01'", "reason": "Many employees pass the filter, which has no index."},
  {"id": 4, "parent": 2, "node_type": "Hash"},
  {"id": 5, "parent": 4, "node_type": "Seq Scan", "relation": "department", "alias": "department", "reason": "Every department is read."}
 ]}
```

That list is this tree:

```text
Aggregate
  Hash Join
    Seq Scan (employee)
    Hash
      Seq Scan (department)
```

### Rules of the list

- The root comes first, and every operator comes before its children. This is the order `EXPLAIN` prints.
- `id` counts 1, 2, 3 in list order.
- `parent` is the `id` of the operator this one passes its rows to. Only the root has `null`.
- An operator's children are the entries that name it as parent, in list order. For a join, the outer input comes first, then the inner input.
- A separately planned subquery is an ordinary entry. Its `parent` is the operator that uses its value, and `parent_relationship` is `InitPlan` or `SubPlan`.
- Fields that do not apply are left out. Only `id`, `parent` and `node_type` are required.

### Fields of one operator

| Field | Meaning | Used on |
| --- | --- | --- |
| `id`, `parent` | The operator's place in the tree. | Every operator |
| `node_type` | The operator, by the name `EXPLAIN` prints for it, such as `Seq Scan` or `Hash Join`. It must be one of the 21 names under this table. | Every operator |
| `relation`, `alias` | The table read, and the name the query gives it. Hints refer to tables by alias. | Scans |
| `index_name` | The index used. | Index Scan, Index Only Scan, Bitmap Heap Scan |
| `cte_name` | The `WITH` query read. | CTE Scan |
| `join_type` | Inner, Left, Right, Full, Semi, Anti or Right Anti. | Joins |
| `strategy` | Plain, Sorted, Hashed or Mixed. | Aggregate, SetOp |
| `workers_planned` | How many parallel workers start. | Gather, Gather Merge |
| `parallel_aware` | True when the operator splits its work across the workers. | Operators below a Gather or Gather Merge |
| `parent_relationship`, `subplan_name` | `InitPlan` or `SubPlan`, and the subquery's name, such as `SubPlan 1` or `CTE revenue`. | Separately planned subqueries |
| `join_condition`, `join_filter`, `index_condition`, `filter` | Conditions, written as SQL, under the names `EXPLAIN` uses. | Joins and scans |
| `sort_keys`, `group_keys` | The columns sorted on, and the columns grouped on. | Sort; Aggregate |
| `reason` | One sentence on why the agent chose this operator. Optional. | Any operator |

The 21 operators are: Seq Scan, Index Scan, Index Only Scan, Bitmap Heap Scan, Hash Join, Merge Join, Nested Loop, Gather, Gather Merge, Hash, Sort, Aggregate, Limit, Materialize, Unique, Subquery Scan, CTE Scan, Append, SetOp, WindowAgg and Result. A Bitmap Heap Scan names its index itself and has no Bitmap Index Scan child.

### How an answer is checked

`validate_agent_plan()` checks in three stages. It stops at the first stage that finds problems, and reports all the problems of that stage:

1. The text holds valid JSON with the fields, types and allowed values above.
2. The ids and parents form one tree.
3. The plan rules hold: each operator has the right number of children, such as two for a join and none for a scan; it has its required fields, such as `alias` on a scan; no alias is used twice; and parallel operators sit below a Gather or Gather Merge.

`ask_agent()` then checks the plan against the query and the database. The plan must scan each table alias the query names, use only indexes that exist, and be something `build_hints()` can turn into hints.

A plan that passes all of these is called **usable**. One that does not is sent back to the agent with its problems; the README's [How it works](../README.md#how-it-works) describes the retries.

## What was tried

Four formats were tested on TPC-H Q1–Q22. Each query was asked twice per format, with Claude Haiku 5.5 at low effort, the API setting for how much the model thinks before it answers. That is 44 conversations per format, each allowed up to three answers.

The table counts first answers only. A format failure is a first answer that could not be read, or that used an unknown operator, field or value. The other unusable first answers were plan mistakes, such as a join with one child or a wrong table name, which no format prevents.

| Format | First answer usable | Format failures | Plan mistakes | Answer length in tokens |
| --- | --- | --- | --- | --- |
| Nested JSON tree, the first design | 82% | 5 | 3 | 2,136 |
| XML, one element per operator | 86% | 3 | 3 | 1,972 |
| Flat JSON list, the current design | 95% | 0 | 2 | 2,405 |
| Flat JSON list enforced by the API's structured outputs | 95% | 0 | 2 | 3,804 |

Every conversation reached a usable plan within its three answers. The test was run on 2026-11-11 with a local script that is not in the repository.

**Nested JSON tree.** Each operator held its children inside it. In a deep plan the agent lost count of the closing brackets: all 5 failures were JSON that could not be parsed.

**XML.** Named closing tags fix the bracket problem. But SQL conditions contain `<` and `&`, which XML reserves, so reading the answer needed escaping rules and its own parser. The agent also invented operator tags, such as `<HashRightAnti>`.

**Structured outputs.** This API feature makes the model's answer match a JSON schema. It refused the natural schema with "Schema is too complex", because each operator has 17 optional fields. The schema that worked requires all 17 on every operator, with empty values where they do not apply. Answers grew by more than half and took about 40% longer, and no more first answers were usable. The feature also rejects recursive schemas, so it could not have rescued the nested tree.

## Why the flat list

No single number proves the choice. With 44 conversations per format, a difference of a few answers can be chance, and that applies to the 95% against 86% as much as to the 0 against 3 format failures. The flat list was chosen because it did at least as well on every measure and is the simplest to handle:

- **No format failures.** Of the three formats that do not need structured outputs, it was the only one with none.
- **No brackets to balance.** Each entry closes before the next begins, however deep the plan is.
- **SQL goes in as it is.** A condition is an ordinary JSON string.
- **Little code.** Rebuilding the tree means attaching each entry to its parent. A JSON parser and pydantic, a Python validation library, do the rest.
- **Errors are easy to act on.** They name the operator by id, as in `node 3: a Seq Scan needs alias.`

Its cost: the agent does not see the tree's shape while it writes, and must keep track of ids. A wrong `parent` is a new kind of mistake, which the second check stage catches.

## What the agent cannot express

Hints can only control certain things, and a plan that cannot be forced cannot be compared. So the format allows only plans that hints can describe.

- **Each table alias is read exactly once.** The plan has one scan for each alias in the query; a table joined to itself has two aliases and so two scans. PostgreSQL sometimes breaks this rule in its own plans, and the agent cannot follow it there:
  - It can read one alias twice. For `SELECT min(x), max(x) FROM t` it uses two index scans, one for each end of the index.
  - It can skip an alias. It drops a `LEFT JOIN` whose columns the query never uses.
- **Only the 21 operators.** Others, such as Memoize and Incremental Sort, cannot be chosen. None appears in PostgreSQL's plans for TPC-H Q1–Q22 on the project's database.
- **Tables only.** A function in `FROM`, such as `generate_series()`, and a `VALUES` list have no operator in the list. A view is shown to the agent as if it were a table, but hints for a view have no effect.
- **At most 200 operators.** This keeps the deepest possible tree inside the limits of pydantic and of Python's recursion. The TPC-H plans seen so far have at most 27.

## What forcing cannot guarantee

A usable plan can be turned into hints. That does not mean PostgreSQL builds exactly that plan.

- **What hints control.** How each alias is scanned and with which index, the join methods, the join order, and which scans run in parallel with how many workers.
- **What they do not control.** Where the Gather sits in the tree. The conditions and reasons the agent writes are not forced either: PostgreSQL works out the conditions from the query, and the agent's are for people reading the plan.
- **Gaps.** PostgreSQL ignores a hint when it cannot build that plan. `force_plan()` reports each decision that was not followed as a gap. In each TPC-H run this happened in 1 to 3 of the 22 queries, mostly where the agent planned a subquery as a join: the `NOT IN` of Q16, which `NULL`s prevent, and the correlated subqueries of Q17 and Q20. Gaps are not sent back to the agent, since they are its own decisions.

## How fair the comparison with PostgreSQL is

What is done to keep it fair:

- The agent never sees PostgreSQL's plan, nor the costs and row counts the planner estimated.
- Its answer is checked against the query, not against PostgreSQL's plan.
- The example in the agent's instructions uses tables that are not in the database, so it never gives away the plan of a query being tested.
- When an answer is rejected, the message says what is invalid, never what would be better.

Where it is still uneven:

- **The agent knows less about the data.** It gets a summary of the planner's statistics, which the README lists under [How it works](../README.md#how-it-works). The planner has the full statistics, with up to 100 common values and a 100-step histogram per column, and a cost model to combine them.
- **The agent may know TPC-H.** It is a public benchmark, so the model has probably seen these queries and plans for them. How much that helps is unknown.
- **The agent is a small model thinking briefly.** A larger model or more effort may plan better.
- **Answers vary.** The same query can get a different plan on the next call.
- **The forced plan is partly PostgreSQL's.** When a plan is forced, PostgreSQL still applies its own changes, such as the two index scans for `min()` and `max()`. A decision counts as the agent's only if `force_plan()` does not report it as a gap.
- **Plan quality is not measured yet.** The checks show that a plan is well formed and can be turned into hints. They do not show that it is fast. That needs the run times of both plans from `EXPLAIN ANALYZE`, and this comparison is not built.
