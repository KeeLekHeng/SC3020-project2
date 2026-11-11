# Forcing a plan: hints and limits

The project compares an AI agent's query plan with PostgreSQL's own. To compare or time the agent's plan, PostgreSQL must first build it. This is called forcing the plan. PostgreSQL has no command for it, so the project uses the `pg_hint_plan` extension, which reads hints from a comment in front of the query. The [README](../README.md) describes the project and how to run it.

This document covers the hints, how a forced plan is checked, which choices were tried, how well forcing works, what cannot be forced, and what forcing changes besides the plan. The code is in `preprocessing.py` (`PlanNode`, the plan tree, `get_postgres_plan()`, `unique_aliases()`, `build_hints()`) and `comparison.py` (`decisions()`, `force_plan()`, `realization_gaps()`). It runs on PostgreSQL 16 with `pg_hint_plan` 1.6. [The agent's plan: format and limits](agent-plan.md) covers the agent's side.

## The hints

`build_hints()` turns a plan into one line of hints. `get_postgres_plan()` puts that line in a `/*+ ... */` comment before the query and runs `EXPLAIN`.

This is PostgreSQL's plan for TPC-H Q3:

```text
Limit
  Sort
    Aggregate
      Gather
        Nested Loop
          Hash Join
            Seq Scan (orders)
            Hash
              Seq Scan (customer)
          Index Scan (lineitem, lineitem_pkey)
```

These are the hints `build_hints()` writes for it. It writes them as one line; they are grouped by purpose here:

```text
SeqScan(orders) SeqScan(customer) IndexScan(lineitem lineitem_pkey)
HashJoin(orders customer) NestLoop(orders customer lineitem) NoMemoize(orders customer lineitem)
Leading(((orders customer) lineitem))
Parallel(orders 1 hard) Parallel(customer 0 hard) Parallel(lineitem 0 hard)
Set(parallel_setup_cost 0) Set(parallel_tuple_cost 0)
Set(join_collapse_limit 100) Set(from_collapse_limit 100) Set(geqo off)
Set(enable_indexonlyscan off) Set(enable_bitmapscan off) Set(enable_tidscan off) Set(enable_mergejoin off)
Set(enable_incremental_sort off) Set(enable_material off) Set(enable_memoize off)
Set(enable_gathermerge off) Set(enable_parallel_hash off) Set(enable_parallel_append off)
```

The plan as printed does not mark parallel scans. In this plan the scan of `orders` is the parallel one, and the Gather starts 1 worker. So `orders` gets `Parallel(orders 1 hard)`, and the other two scans get 0 workers. `hard` tells PostgreSQL to use exactly that number.

### What each group does

| Hints | What they fix |
| --- | --- |
| `SeqScan`, `IndexScan`, `IndexOnlyScan`, `BitmapScan` | How each table is scanned, and with which index. |
| `HashJoin`, `MergeJoin`, `NestLoop` | The method of each join, named by the tables it joins. `NoMemoize` forbids a Memoize under a Nested Loop that has none. |
| `Leading` | The join order, and which side of each join is the outer side. In a pair, the outer side is written first. |
| `Parallel` | Which scans run in parallel, with how many workers. A plan without a Gather gets `Set(max_parallel_workers_per_gather 0)` instead, which makes the whole query serial. |
| `Set(parallel_setup_cost 0)`, `Set(parallel_tuple_cost 0)` | Only for a plan with a Gather. A `Parallel` hint sets these costs to 0 only while its own scan is planned, so a Gather directly above that scan looks free and wins. With both costs at 0 for the whole query, a Gather higher up can win too. This steers the Gather; it does not fix its place. |
| `Set(join_collapse_limit 100)`, `Set(from_collapse_limit 100)`, `Set(geqo off)` | Only for a plan with joins. They make PostgreSQL order all the joins as one problem, so that the `Leading` hint applies. |
| `Set(enable_... off)` | Called the switch-offs in this document: every operator the plan never uses is switched off for the whole query. |

### Rules of the hints

- **Hints name tables by alias.** A hint uses the alias that `EXPLAIN` shows, or the table name when the query gives no alias.
- **A table name used twice needs its own alias.** For a name the query reads twice, `EXPLAIN` shows `nation` and `nation_1`, and no hint can reach `nation_1`. `unique_aliases()` first gives each later use an alias, such as `nation2`, and changes nothing else in the text. Always plan and force the rewritten query.
- **A subquery in a join is named from the SQL.** `EXPLAIN` often drops the name of a subquery in `FROM`, `LATERAL`, `IN` or `EXISTS`. `build_hints()` reads the names from the query, so joins over such subqueries get hints.
- **One `Leading` hint for each group of joins that PostgreSQL orders together.** If one group gets two, `pg_hint_plan` follows only the last. The joins below a `FULL JOIN` are a group of their own, so they get their own hint, and it must come before the hint of the group above.
- **Hints are requests, not commands.** PostgreSQL skips a hint it cannot satisfy, without an error, so every forced plan is checked.

### How a forced plan is checked

`force_plan()` forces a plan: it calls `build_hints()` and then `get_postgres_plan()` with the hints. It returns three things: the hints, the plan PostgreSQL built with them, and the gaps.

1. `decisions()` reduces a plan to its planner decisions, such as `scan(orders): Seq Scan` and `join(customer, orders): Hash Join`.
2. `realization_gaps()` compares the decisions of the requested plan with those of the plan PostgreSQL built. Each forced decision that differs is a **gap**. The forced kinds are the scan method with its index, parallel or serial, the join method, and the outer side. Join order needs no kind of its own: a join is named by the set of tables it joins, so a different order gives different joins. The worker count is not checked.
3. Some hints can backfire. So while there are gaps, `force_plan()` tries again without the switch-offs, then with the switch-offs but without `Leading` hints. It returns the try with the fewest gaps.

A plan with no gaps is **fully followed**. `build_hints()` refuses a plan it cannot write hints for, with a `ValueError`: a table name used twice that the rewrite could not separate, an alias the query does not have, or an alias that contains `/*`, which would open a comment inside the hint.

## What was tried

Each choice below was measured on the project's TPC-H database, with local scripts that are not in the repository.

**Switch-offs.** Scan and join hints leave PostgreSQL free to add operators the plan does not have, such as a Materialize or a hashed Aggregate. The switch-offs remove that freedom. Over 2,419 forcings during development, each was counted by how often it improved the forced plan and how many forced decisions it broke:

| Switch-off | Improved the forced plan | Forced decisions broken |
| --- | --- | --- |
| `enable_material` | 298 | 74 |
| `enable_hashagg` | 199 | 5 |
| `enable_sort` | 92 | 0 |

A switch-off does harm when the plan needs an operator it does not show, such as a Materialize under a Nested Loop whose inner side is a join. No rule predicts every such case, so the switch-offs are still written and `force_plan()` retries without them when a forced plan has gaps.

**Collapse limits.** Past 8 tables in one group of joins, PostgreSQL ignores `Leading` hints, because of `join_collapse_limit` and `from_collapse_limit`. Past 12, its genetic query optimizer, `geqo`, takes over and also ignores them. Limits sized for each query were compared with one fixed value of 100, both with the genetic optimizer off. The results were the same on every benchmark query, so the simpler fixed value was kept.

**`pg_plan_advice`.** This is a newer PostgreSQL module that takes plan advice in place of hints. A prototype on a PostgreSQL 19 release candidate was compared with `pg_hint_plan` on the same server, by forcing PostgreSQL 19's own plan back for the 416 queries of the test below:

| | `pg_hint_plan` | `pg_plan_advice` |
| --- | --- | --- |
| Plans fully followed | 99% | 95% |
| Gather placement followed | 66% | 92% |

`pg_plan_advice` followed scan, join and outer-side decisions in every case, and the forcing code shrank from 666 to 306 lines. But it cannot force parallelism for one scan at a time, and it followed fewer plans in full. The project stayed with `pg_hint_plan` on PostgreSQL 16, where it follows all of PostgreSQL's own plans, as the next section shows.

## How well forcing works

The test used 416 queries: TPC-H Q1–Q22, 144 AI-generated queries built to hit edge cases, and 250 generated joins over the TPC-H foreign keys. `build_hints()` refuses one query, which leaves 415.

Each plan was forced as PostgreSQL chose it. That must give the same plan back, so any gap there is a fault or a known limit. Each plan was then forced again after each of four edits of the kind an agent might make, counted only where the edit changed the plan.

| Plan forced | Plans | Plans fully followed | Forced decisions followed |
| --- | --- | --- | --- |
| PostgreSQL's own plan | 415 | 100% | 100% |
| Join sides swapped | 347 | 91% | 99.0% |
| Every join set to one method, for each of the 3 methods | 799 | 88% | 98.8% |
| Parallelism removed | 157 | 99% | 99.95% |
| Seq Scans set to primary-key Index Scans | 347 | 31% | 91.7% |

The rows add up to 2,065 forcings, where one forcing is one plan forced on one query. The two counts of 347 are a coincidence.

- **PostgreSQL's own plans.** All 415 are fully followed. 401 also match in every decision that is not forced, such as the Aggregate strategy and the place of the Gather.
- **Most plans with a gap are impossible requests.** PostgreSQL adds a cost of 1e10 to a plan that uses a disabled operator, and it picks such a plan only when the hints leave it no other. 319 of the 367 plans with a gap carry that penalty, which means PostgreSQL had no way to build what was asked. This includes all 239 failed Index Scan edits, where the query gives the index no condition to use.
- **The retries are rare.** `force_plan()` needed a retry in 53 of the 2,065 forcings.
- **The refused query** reads a `NOT MATERIALIZED` `WITH` query twice. PostgreSQL copies its tables, and no hint can tell the copies apart.

`tests/test_forcing.py` repeats a part of this on every test run: TPC-H Q1–Q22, query shapes that are hard to force, and a few edited plans.

## What cannot be forced

`pg_hint_plan` has no hint for these, so PostgreSQL chooses them itself around the forced scans and joins:

- **Aggregate strategy, sorting, set operations and Materialize.** A switch-off applies to the whole query. A plan with one hashed and one sorted Aggregate cannot fix which is which.
- **Where the Gather sits**, and whether it is a Gather or a Gather Merge.
- **A Memoize the plan wants.** A hint can forbid one but not demand one.
- **The worker count under an Append.** PostgreSQL sets it and ignores the hint.

These cannot be named in a hint:

- **A scan of something that is not a table**, such as a function or a `VALUES` list, **and a join over an operator without a name**, such as an Append over partitions. Only the switch-offs limit them.
- **The outer side of a join whose inner side contains a `LEFT`, `RIGHT` or `FULL JOIN`**, such as the outer of two nested `FULL JOIN`s. `pg_hint_plan` 1.6 never matches such a pair on PostgreSQL 16. The join keeps its method, and PostgreSQL picks the side.
- **A view.** A hint that names a view has no effect.

And PostgreSQL cannot build these at all:

- an Index Scan or Bitmap Heap Scan with no condition the index can use;
- a Nested Loop for a `FULL JOIN`, or a Hash Join on a condition that is not an equality;
- some swaps of the sides of an outer or anti join;
- a parallel scan on the inner side of a Nested Loop.

## What forcing changes besides the plan

- **Costs are not comparable.** Hints change the planner's cost numbers, even when the plan stays the same. With a `Parallel` hint, the cost of one identical plan went from 15,426 to 1,010. With a scan hint, another went from 445 to 497. Compare the run times from `EXPLAIN ANALYZE`, not the costs.
- **A gap does not always mean PostgreSQL ignored a hint.** `build_hints()` writes no hint for some scans and joins, as listed above. A difference there is also reported as a gap, although PostgreSQL chose freely. Check a gap against the hints before you conclude that a hint was ignored.
- **The forced plan is partly PostgreSQL's.** Count a decision as the agent's only when it is a forced kind and not a gap.
