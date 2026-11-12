# Comparison Design Notes

## Compare three plan perspectives, not just two

Although the project begins with a PostgreSQL plan and an LLM-proposed plan, forcing
the LLM plan through `pg_hint_plan` produces a third plan:

1. **PostgreSQL plan** - the plan PostgreSQL normally chooses.
2. **LLM-proposed plan** - the strategy requested by the LLM after its answer has been
   parsed and validated as a `PlanNode`.
3. **Realized LLM plan** - the executable plan PostgreSQL actually produces after the
   LLM proposal is translated into hints.

The proposed and realized LLM plans can differ because hints are requests rather than
absolute commands. Some requested choices may be impossible for PostgreSQL to build or
cannot be expressed precisely using `pg_hint_plan`. For example, an index scan may be
requested when the query has no condition that can use the index. These mismatches are
reported as **realization gaps**.

The current pipeline compares PostgreSQL's normal plan with the realized LLM plan.
This is defensible because both are executable PostgreSQL plans. However, using only
this comparison can hide a poor or impossible LLM decision: PostgreSQL may replace that
decision while constructing the realized plan, making the final plan appear more
similar to PostgreSQL's normal plan than the LLM's original proposal really was.

The final evaluation should therefore distinguish three questions:

- **Proposed-strategy difference:** How different is the LLM-proposed plan from
  PostgreSQL's normal plan?
- **Plan realizability:** How much of the LLM's proposed strategy could PostgreSQL
  actually realize? This should be measured using the realization gaps returned by
  `force_plan()`.
- **Realized-plan difference:** How different is the executable, forced LLM plan from
  PostgreSQL's normal plan?

Reporting these separately preserves accountability for the LLM's original choices
while still allowing a fair comparison between two executable plans. It also prevents
PostgreSQL's corrections during plan forcing from being mistakenly credited to the
LLM.

The complete metric definitions, formulas, interpretation and limitations are in
[`docs/metrics.md`](docs/metrics.md).

## TODO for the comparison component

### 1. Implement a reusable pairwise metric

- Add a function that accepts two `PlanNode` objects.
- Convert both plans with `decisions()`.
- Use the union of their decision keys as the denominator.
- Count a decision as different when it is missing from one plan or its choices do not
  match under the same rule currently used by `differences()`.
- Return raw counts as well as rates: total decisions, matching decisions, different
  decisions, similarity rate and difference rate.
- Handle an empty comparison universe explicitly instead of dividing by zero.

### 2. Calculate the proposed-plan metric

- Compare PostgreSQL's normal plan with the original agent `PlanNode`.
- Treat this as the primary measurement of what the agent actually proposed.
- Preserve the individual `Difference` entries so that the GUI and report can explain
  the result.

### 3. Calculate plan realizability

- Compare the original agent plan with the realized, forced plan.
- Consider only the forceable kinds: `scan`, `parallel`, `join` and `outer`.
- Use the agent's requested forceable decisions as the denominator.
- Count how many requested decisions have the same key and choice in the realized
  plan.
- Return the rate together with the realization gaps from `force_plan()`.
- Report `N/A` if the agent plan has no forceable decisions.

### 4. Calculate the realized-plan metric

- Compare PostgreSQL's normal plan with the realized agent plan.
- Use the same pairwise formula as the proposed-plan metric.
- Present it as an executable-plan comparison, not as the sole measurement of agent
  quality.

### 5. Add an interpretable breakdown

- Group matches and differences by decision kind: scans, parallelism, joins, outer
  sides, aggregates, Gather, sorting and other operators.
- Keep the overall percentage, raw numerator and denominator together.
- Make it possible to distinguish missing decisions from changed choices.
- Add a clearly labelled core structural rate that excludes `parallel` and `gather`,
  while retaining the all-kinds rate and a separate parallelism breakdown.
- Mark realized decisions as agent-attributable only when their kind is forceable and
  they have no realization gap.

### 6. Define a result object for integration

- Return structured data rather than only printing strings.
- Include the three metrics, raw counts, per-kind breakdowns, differences and
  realization gaps.
- Include both all-kinds and core structural views without presenting the core view as
  a replacement for the official overall rate.
- Expose whether each realized difference is forceable, followed and attributable to
  the agent, or an unforced choice made around the hints by PostgreSQL.
- Give the GUI teammate a stable result object that can be rendered without needing
  to understand the comparison internals.
- Update `project.py` to print a concise command-line summary using the same object.

### 7. Add tests

- Identical plans produce a 0% difference rate and 100% similarity rate.
- Completely different choices are counted correctly.
- A decision present in only one plan is included in the union and counted as a
  difference.
- An unspecified agent index matches the same scan method with a named PostgreSQL
  index, following the existing comparison rule.
- Proposed and realized rates differ when PostgreSQL replaces an agent decision.
- Realizability counts only decisions requested by the agent and only forceable kinds.
- A plan with no forceable decisions reports realizability as `N/A`.
- Per-kind totals add up to the overall totals.
- The core structural rate excludes exactly `parallel` and `gather` decisions.
- Moving a Gather can create two all-kinds differences but neither appears in the core
  structural rate.
- An unforced Aggregate, Sort or Gather difference is not labelled agent-attributable.
- A forceable decision with a realization gap is not labelled agent-attributable.
- A PostgreSQL plan that scans one query alias twice is flagged as a representation or
  planner-rewrite case rather than silently treated as an ordinary agent error.

### 8. Produce evidence for the report

- Record the three metrics for every successful query.
- Report agent failures and plans that cannot be forced instead of silently excluding
  them.
- Calculate the macro-average across queries so every query has equal weight.
- Optionally provide a clearly labelled micro-average based on pooled decision counts.
- Save representative examples showing how the three metrics prevent PostgreSQL's
  corrections from being credited to the agent.
- Document the limitations listed in `docs/metrics.md` when interpreting the results.

### 9. Preserve reproducibility data

- Save the original query and the rewritten `AgentResult.query` used for all plans.
- Save `AgentResult.answer`, summary, retry problems and validated agent plan.
- Save PostgreSQL's normal plan, the realized plan, generated hints and realization
  gaps.
- Record the model, effort, PostgreSQL version, database scale, indexes and relevant
  planner settings.
- Store metric numerators and denominators, not only rounded percentages.
- Retain failed agent calls and unforceable plans in the experiment output.

### 10. Classify known sources of misleading differences

- Flag queries where PostgreSQL scans one alias more than once, such as separate index
  scans for `min()` and `max()`.
- Flag queries where PostgreSQL removes a table or join that the query names.
- Distinguish ordinary planning disagreement from an agent-format limitation or a
  PostgreSQL query rewrite.
- Use Q16, Q17 and Q20 as candidate case studies for realization gaps involving
  `NOT IN` semantics and correlated subqueries.
- Include at least one join-order case study showing that one changed ordering strategy
  can create several `join` and `outer` differences.
