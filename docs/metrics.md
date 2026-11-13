# Metrics for comparing PostgreSQL and AI-generated plans

The project evaluates how closely an AI agent's query execution plan agrees with
PostgreSQL's plan. There are three relevant plans for the same rewritten SQL query:

- **PostgreSQL plan (`P`)**: the plan PostgreSQL normally chooses.
- **Agent-proposed plan (`A`)**: the validated `PlanNode` produced from the agent's
  answer.
- **Realized agent plan (`R`)**: the executable plan PostgreSQL produces after the
  agent plan is translated into `pg_hint_plan` hints.

The distinction between `A` and `R` is essential. Hints are requests, not commands,
and PostgreSQL cannot necessarily construct every plan the agent proposes. Comparing
only `P` with `R` could therefore hide a poor or impossible agent choice that
PostgreSQL replaced while planning the hinted query.

This document defines three complementary metrics:

1. proposed-plan difference rate;
2. plan realizability rate; and
3. realized-plan difference rate.

No single metric answers all three questions. They should be reported together.
A supporting fourth metric, the intended-difference realization rate, combines all
three plans to show which of the agent's disagreements with PostgreSQL took effect.
Two diagnostic views are also recommended: a per-kind breakdown and a core structural
difference rate that excludes `parallel` and `gather` decisions. These views explain
whether one broad planning choice is dominating an overall percentage; they do not
replace the three primary metrics.

## The unit of comparison: planner decisions

The metrics compare planner decisions rather than nodes at the same tree position.
Tree positions are unstable: changing the join order can reshape the whole tree even
when several underlying choices remain the same.

`decisions()` in `comparison.py` converts a `PlanNode` tree into a mapping from a
semantic `Decision` to its `Choice`. Examples include:

| Decision | Example choice | Meaning |
| --- | --- | --- |
| `scan(o)` | `Index Scan on orders_pkey` | How alias `o` is read. |
| `parallel(o)` | `parallel` | Whether the scan of `o` is parallel-aware. |
| `join(c, o)` | `Hash Join` | The join method used for aliases `c` and `o`. |
| `outer(c, o)` | `c` | Which input is on the outer side of that join. |
| `aggregate(c, o)` | `Hashed` | The aggregation strategy over those aliases. |
| `gather(c, o)` | `Gather` | How parallel results are collected. |
| `sort(c, o)` | `c.c_name` | The sort operation and its keys. |

A join is identified by the sorted set of aliases below it. This allows corresponding
joins to be matched semantically even when the two plan trees have different shapes.

Let `D(X)` be the decision mapping returned by `decisions(X)`. For two plans `X` and
`Y`, their comparison universe is the union of their decision keys:

```text
U(X, Y) = keys(D(X)) union keys(D(Y))
```

A key is different when:

- it appears in only one plan; or
- it appears in both plans but has different choices.

Choice equality follows the existing `_same()` rule in `comparison.py`. In particular,
an agent scan that specifies a scan method but omits the index name is allowed to match
the same PostgreSQL scan method with a named index.

For transparency, every reported rate should be accompanied by its numerator and
denominator, for example `6/20 decisions (30%)`.

Every metric also has one of three explicit statuses:

- `defined`: the required inputs exist and the rate has an applicable denominator;
- `not_applicable`: the inputs exist, but there are no applicable decisions for this
  metric; or
- `unavailable`: a required input, such as the realized plan, could not be obtained.

`not_applicable` and `unavailable` are both displayed without a numeric rate, but they
mean different things and must not be converted to 0%. Each carries a human-readable
reason. In particular, if forcing fails, the proposed-plan comparison remains defined
because `P` and `A` still exist; only realizability and realized-plan difference are
unavailable. Counts that cannot be observed are represented as `null`, not zero. A
known denominator may remain available—for example, the number of force-eligible agent
requests is known even when forcing fails.

## Metric 1: proposed-plan difference rate

### Question answered

How different is the strategy originally proposed by the agent from PostgreSQL's
normal strategy?

### Plans compared

```text
PostgreSQL plan P  versus  agent-proposed plan A
```

### Definition

Let `M(P, A)` be the decisions in `U(P, A)` that are missing from one plan or have
different choices. The proposed-plan difference rate is:

```text
ProposedDifference(P, A) = |M(P, A)| / |U(P, A)|
```

The corresponding similarity rate is:

```text
ProposedSimilarity(P, A) = 1 - ProposedDifference(P, A)
```

### Reasoning

This is the primary measure of the agent's own planning behaviour. It evaluates the
validated plan before PostgreSQL has an opportunity to repair, replace or ignore any
of its choices. It therefore holds the agent accountable for what it actually
proposed.

For example:

```text
PostgreSQL: Seq Scan on orders
Agent:      Index Scan on orders
```

This is a proposed-plan difference even if PostgreSQL later discovers that the index
scan is impossible and falls back to a sequential scan.

### Implementation mapping

```python
comparison = compare_plans(postgres_plan, agent_plan)
proposed_difference = comparison.all_kinds.difference
```

`proposed_difference.numerator` and `.denominator` are the documented fraction. If the
comparison universe is empty, both the difference and similarity scores are
`not_applicable`, although a valid execution plan always produces at least one decision.

## Metric 2: plan realizability rate

### Question answered

How much of the agent's force-eligible strategy appeared in PostgreSQL's realized
plan?

### Plans compared

```text
Agent-proposed plan A  versus  realized agent plan R
```

Only decision kinds that the forcing layer can potentially target are included:

```text
scan, parallel, join, outer
```

These are the kinds in `FORCED_KINDS` in `comparison.py`. This makes a decision
*force-eligible*; it does not prove that the selected forcing attempt emitted a hint
for that particular decision.

### Definition

Let `F(A)` be the force-eligible decisions requested by the agent. A requested decision
is reproduced when the same decision key exists in `D(R)` and its choice matches under
the same equality rule used by `differences()`.

```text
Realizability(A, R) = reproduced requested force-eligible decisions / |F(A)|
```

Equivalently:

```text
Realizability(A, R)
    = 1 - unmatched requested force-eligible decisions / |F(A)|
```

If the agent plan contains no force-eligible decisions, the rate is `not_applicable`
and should be reported as `N/A`, not as 0% or 100%.

`force_plan()` also returns `realization_gaps()`. The gaps should be listed alongside
this rate because they explain exactly which requested scan, parallelism, join-method
or outer-side decisions were not reproduced. When calculating the rate, count
unmatched decisions requested by `A`; a new decision that appears only in `R` is useful
diagnostic information but is not itself an agent request or realization gap.

Realizability is an outcome metric, not a causality claim. A reproduced choice may
simply be what PostgreSQL would have chosen itself, so an agent that copies
PostgreSQL's plan scores 100%. The intended-difference realization rate below counts
only the choices where the agent disagreed with PostgreSQL.

The implementation exposes this through `realizability(agent_plan, forcing_outcome)`.
Its score is the reproduced count divided by the requested force-eligible count; extra
realized decisions are returned separately and never enter that denominator.

Realizability is measured only on plans the validator accepted. The validator rejects
an answer that `build_hints()` cannot turn into hints and asks the agent again, so plans
the forcing layer cannot express never reach this denominator. Report the retry count
(`len(AgentResult.problems)`) for each query, and across a workload the share of queries
whose first answer was rejected, next to this rate.

### Reasoning

This metric separates plan quality from plan feasibility. A proposed choice may be
reasonable in isolation but impossible for the particular SQL query, index condition
or join type. Conversely, a low realizability rate may also expose a limitation of
`pg_hint_plan`, rather than an error by the agent. The gaps must therefore be
interpreted, not merely counted.

For example:

```text
PostgreSQL normal plan: Seq Scan on orders
Agent proposal:         Index Scan on orders
Realized agent plan:    Seq Scan on orders
```

The requested index scan is a proposed difference and an unrealized decision. It must
not disappear from the evaluation merely because the realized plan matches
PostgreSQL's normal plan.

## Metric 3: realized-plan difference rate

### Question answered

After PostgreSQL realizes as much of the agent strategy as it can, how different is
the resulting executable plan from PostgreSQL's normal plan?

### Plans compared

```text
PostgreSQL plan P  versus  realized agent plan R
```

### Definition

The formula is the same decision-union formula used by the proposed-plan metric:

```text
RealizedDifference(P, R) = |M(P, R)| / |U(P, R)|
```

and:

```text
RealizedSimilarity(P, R) = 1 - RealizedDifference(P, R)
```

### Reasoning

Both `P` and `R` are plans that PostgreSQL can construct, so this is the fairest
structural comparison between two executable plans. It answers what the agent's
strategy becomes in practice after PostgreSQL applies the hints.

This is a secondary metric rather than the sole measure of agent quality. PostgreSQL
may correct an impossible agent decision while producing `R`, making `R` look closer
to `P` than the original proposal `A` was.

The whole realized plan must not be attributed to the agent. Hints directly target
only `scan`, `parallel`, `join` and `outer` decisions. Aggregate strategy, sorting,
Gather placement and other surrounding operators may be PostgreSQL's own choices while
it constructs `R`. A realized-plan difference is therefore a structural difference
between two executable plans, not automatically a difference caused by the agent.

The intended-difference realization rate separates the realized differences the agent
asked for from those PostgreSQL introduced while planning the hinted query.

### Implementation mapping

```python
comparison = compare_plans(postgres_plan, realized_agent_plan)
realized_difference = comparison.all_kinds.difference
```

`evaluate_plans(postgres_plan, agent_plan, forcing_outcome)` composes this comparison
with the proposed comparison and realizability result. When forcing is unavailable it
returns an unavailable realized comparison rather than discarding the proposed one.

## Metric 4 (supporting): intended-difference realization rate

### Question answered

Of the force-eligible choices where the agent disagreed with PostgreSQL, how many did
the realized plan keep?

### Definition

Let `F(A)` be the force-eligible decisions requested by the agent. The agent's intended
differences are those whose choice differs from PostgreSQL's normal plan, including a
decision `P` does not have:

```text
Intended(P, A) = {d in F(A) where A_d differs from P_d}

IntendedRealization(P, A, R)
    = |{d in Intended(P, A) where R_d matches A_d}| / |Intended(P, A)|
```

Choices are compared with the same equality rule as the other metrics. A decision is
realized exactly when it is not a realization gap.

If the agent agrees with PostgreSQL on every force-eligible decision, the rate is
`not_applicable`. If forcing fails, it is `unavailable`, with the number of intended
differences still known as its denominator.

### Reasoning

A realized choice that PostgreSQL would have made anyway says nothing about the agent.
A realized choice that differs from `P` and matches `A` is a change the agent caused,
because forcing the agent's plan is the only thing that differs between `P` and `R`.

The rate needs only the three plans, so it can be recomputed from saved plans. Unlike
the realized-plan difference, it excludes side effects: a realized difference the
agent did not ask for, such as a new aggregate strategy, is not counted. It is limited
to force-eligible kinds because hints cannot request aggregate, Gather, sort or other
operator choices.

### Implementation mapping

`evaluate_plans(postgres_plan, agent_plan, forcing_outcome).intended` is the score.

## Interpreting the three metrics together

The three values should be presented as one result, not treated as competing metrics.
For example:

```text
Proposed-plan difference:  9/21 decisions (42.9%)
Plan realizability:        14/16 force-eligible decisions (87.5%)
Realized-plan difference:  6/20 decisions (30.0%)
Intended realization:      5/7 agent disagreements (71.4%)
```

This result means:

- the agent originally made a substantially different set of choices;
- PostgreSQL could reproduce most, but not all, force-eligible agent choices;
- after the unrealizable choices were replaced, the executable agent plan was closer
  to PostgreSQL's normal plan than the original proposal was; and
- 5 of the 7 force-eligible choices where the agent disagreed with PostgreSQL took
  effect.

A particularly important case is:

```text
Proposed-plan difference: high
Plan realizability:       low
Realized-plan difference: low
```

This does **not** mean that the agent closely agreed with PostgreSQL. It means the
agent proposed a different plan, much of that proposal could not be realized, and
PostgreSQL's corrections made the final plan resemble its normal plan.

## Aggregating results across a workload

Report the three metrics for every query before producing workload-level summaries.
For the overall workload, use the arithmetic mean of the per-query rates:

```text
MacroAverage = sum(per-query rate) / number of queries with a defined rate
```

This gives every query equal importance. A single large query with many joins should
not dominate all smaller queries merely because it produces more decisions.

The report may additionally include a micro-average calculated from the total
numerators and denominators across all queries. If it is included, label it clearly:

```text
MicroAverage = sum(different decisions) / sum(all compared decisions)
```

Always report the number of successful agent plans, failed agent plans and plans that
could not be forced. Excluding failures without reporting them would bias the results
in the agent's favour.

## Recommended result breakdown

In addition to the overall rates, differences should be grouped by decision kind:

- scan method and index;
- parallel versus serial scan;
- join method;
- join order and outer side;
- aggregation strategy;
- Gather placement;
- sorting and other operators.

This breakdown explains *why* two plans differ. An overall percentage alone cannot
distinguish one major join-order change from several smaller scan choices.

### Core structural difference rate

Report the proposed and realized difference rates both with all decision kinds and
with `parallel` and `gather` removed from their comparison universes:

```text
CoreUniverse(X, Y)
    = {d in U(X, Y) where d.kind not in {parallel, gather}}

CoreDifference(X, Y)
    = different decisions in CoreUniverse(X, Y) / |CoreUniverse(X, Y)|
```

The all-kinds rate remains the official overall rate. The core rate is a diagnostic
view motivated by how parallelism is represented:

- one serial-versus-parallel strategy can create one `parallel` difference per scan;
- moving a Gather creates two keyed differences, one at the old location and one at
  the new location; and
- hints can force which scans are parallel, but cannot fix the exact Gather location
  or whether PostgreSQL uses Gather or Gather Merge.

Do not silently remove these decisions. Show the all-kinds rate, core rate and
parallelism category together. In the documented TPC-H runs, PostgreSQL planned 14 of
22 queries in parallel while the agent planned only 2 to 7, depending on the run, so
this distinction may materially affect the results.

## Structured result contract

The typed Python result is `PlanEvaluation`. It holds the proposed comparison,
the realizability result, the realized comparison, the intended-difference
realization score and the forcing outcome. Each difference keeps the `PlanNode` that made each choice, so a
consumer can highlight the node. Rates remain unrounded numbers; formatting belongs to
the CLI or GUI.

## Reproducibility records

Agent answers vary between calls, so every evaluated query should retain enough
information to reconstruct its result. Save at least:

- the original and `unique_aliases()`-rewritten SQL;
- model name, effort setting and relevant prompt/version identifier;
- the raw `AgentResult.answer`, summary and retry problems;
- the validated agent `PlanNode`;
- PostgreSQL's normal plan and the realized hinted plan;
- the generated hints, selected forcing attempt and realization gaps;
- PostgreSQL version, database scale, indexes and relevant planner settings; and
- all metric numerators, denominators, rates and per-kind breakdowns.

Saving only the final percentages is insufficient because another model call may
produce a different plan. Failed agent calls and plans that cannot be forced must also
remain in the experiment record.

## Limitations

### The metric measures structural agreement, not execution quality

A low difference rate means that two plans make similar structural choices. It does
not prove that either plan is fast. Two identical plans can both perform poorly, while
two structurally different plans can have similar execution times.

Agreement with PostgreSQL also says nothing about which plan is better: the agent is
given the same statistics PostgreSQL plans from, so both can share one wrong row
estimate. Execution time is the independent measure. `project.execution_time()` runs a
query with `EXPLAIN ANALYZE`, once to warm the cache and once to time it, and gives up
after a timeout of one minute by default. Report the PostgreSQL plan's and the realized
agent plan's times next to the structural metrics, especially for decisions where the
agent disagreed with PostgreSQL. A single timed run is noisy; across a workload, take
the median of several runs. `EXPLAIN ANALYZE` also fills each node's actual row count,
which shows where a row estimate was wrong.

### PostgreSQL is a baseline, not absolute ground truth

The metric measures agreement with PostgreSQL's optimizer. PostgreSQL's selected plan
is not guaranteed to be globally optimal, especially when its cardinality estimates
are inaccurate. A high proposed difference rate therefore means disagreement, not
automatically poor agent quality.

### Costs and row estimates are not directly comparable

The agent plan intentionally contains choices rather than PostgreSQL-style cost and
cardinality estimates. PostgreSQL supplies `startup_cost`, `total_cost` and
`estimated_rows`; the agent generally does not. These fields are excluded from the
structural metrics.

Costs from the forced plan are also unsuitable for comparison because hints and
planner switch-offs alter PostgreSQL's cost calculations. If performance is measured,
use execution time rather than comparing hinted and unhinted planner costs.

### Conditions and filters are not compared

The current `decisions()` representation does not compare join conditions, index
conditions, filters or group keys in full. This avoids treating formatting differences
as semantic differences, but it can miss an incorrect or incomplete expression in an
agent plan.

### Worker counts are not compared

The metric records whether a scan is parallel or serial and whether a Gather exists,
but it does not compare the exact number of planned workers. Plans with different
degrees of parallelism can therefore appear equal on this dimension.

### Some operators are folded or omitted

`Hash`, `Bitmap Index Scan` and `Subquery Scan` are folded out of the decision mapping
because they either belong to another decision or PostgreSQL may remove them without a
meaningful strategy change. Consequently, the metric is intentionally not a literal
node-by-node tree-edit distance.

### Repeated decisions may collapse

The decision mapping uses one key per decision kind and set of aliases. If two
operators of the same kind cover the same aliases, one entry overwrites the other.
This can undercount differences in plans containing repeated operations over the same
subtree.

### Join-order changes can produce several differences

A join is keyed by the set of aliases underneath it. Changing an early join order can
create or remove several join and outer-side decision keys. The overall rate therefore
reflects the broad structural effect of that change, but it may count one planning
idea as several differences. Reporting results by decision kind helps make this
visible.

### Realizability is partly limited by the forcing mechanism

A realization gap does not always mean the agent proposed an invalid strategy.
`pg_hint_plan` cannot force every PostgreSQL choice, including exact Gather placement,
some non-table scans, individual aggregate strategies and certain outer-join sides.
Some requested plans are also inherently impossible, such as a usable index scan with
no applicable index condition. Realization gaps should therefore be classified where
possible as either an impossible request, a forcing limitation or an unexplained gap.

The gap calculation compares every requested decision in `FORCED_KINDS`, even when
hint generation skipped that individual decision. It excludes decisions that appear
only in the realized plan; those are reported separately as extras. A gap is therefore evidence
that the requested choice was not reproduced, not proof that PostgreSQL rejected a
specific hint. The selected attempt's hint text shows whether a hint was written for
that decision.

## Technical debt

### Forcing failure produces a partial evaluation

Failure to build or execute hints does not invalidate the original agent proposal.
The `P` versus `A` metric should still be reported, while realizability and realized
difference are marked `unavailable` with the exact failure reason. Workload summaries
must count these forcing failures rather than silently dropping the query.

### The agent's plan vocabulary is restricted

The agent can choose only the operators allowed by `AgentNodeType`. For example, it
cannot currently propose every PostgreSQL operator. This makes the comparison more
forceable and easier to validate, but it constrains the agent's planning freedom.

The format also requires the agent to scan every table alias named by the query exactly
once. PostgreSQL is not subject to this rule. It may scan one alias more than once,
such as two index scans for `min(column)` and `max(column)`, or eliminate an unused
table from a `LEFT JOIN`. A direct proposed-plan difference caused by such a rewrite is
partly a representation limitation rather than evidence of a poor agent decision.
Affected results should be flagged and, where possible, classified as a normal
planning disagreement, an agent-format limitation or a PostgreSQL query rewrite.

### Both plans must refer to the same rewritten query

Decisions are matched through table aliases. The PostgreSQL plan, agent plan and
realized plan must all use `AgentResult.query`, after `unique_aliases()` has rewritten
repeated table names. Comparing plans produced from different query texts can create
false missing or extra decisions.

### Workload selection affects the result

Public benchmark queries, and PostgreSQL's plans for them, may have appeared in the
LLM's training data. On TPC-H, agreement with PostgreSQL can then come from recall
rather than planning. Report every metric separately for TPC-H and for original
queries, ideally over renamed tables and columns; markedly closer agreement on TPC-H
points to recall. The final evaluation should therefore combine TPC-H queries with
diverse original queries and
should describe the workload, database scale, indexes, PostgreSQL settings, model and
prompt configuration. Agent failures and retries must be retained as part of the
reported results.

The same query may receive a different agent plan in another run. Workload summaries
should therefore state whether each query was sampled once or repeatedly and must keep
the raw answers needed to reproduce the reported run.
