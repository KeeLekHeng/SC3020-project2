# Design

## Context

`comparison.py` already converts a `PlanNode` into semantic `Decision -> Choice`
mappings and lists differences between two plans. `project.py` currently compares
PostgreSQL's normal plan only with the plan PostgreSQL produces from the agent's hints.
The original agent plan and realization gaps are available but are not combined into a
quantitative evaluation. See `proposal.md` for motivation and the capability spec for
observable behaviour.

The agent and PostgreSQL plans share `PlanNode`, but their populated fields differ.
Costs, estimates, conditions, worker counts, and agent reasons are therefore not valid
inputs to the structural rate. Aliases must come from the same rewritten query.

## Goals / Non-Goals

**Goals:**

- Keep the primitive comparison pure and reusable for any two `PlanNode` trees.
- Produce immutable, typed results with raw counts and display context, plus a
  JSON-safe representation for GUI and experiment handoff.
- Calculate the three plan perspectives without hiding an unrealizable proposal.
- Share one equality rule between detailed differences and metric counts.
- Make attribution conservative and mechanically verifiable.
- Preserve compatibility with existing `decisions()`, `differences()`, and
  `force_plan()` callers.
- Preserve the proposed-plan comparison when forcing cannot produce a realized plan.

**Non-Goals:**

- Execute queries or judge runtime performance.
- Infer the semantic cause of a difference.
- Persist experiments or aggregate a workload.
- Call an LLM or database from the metric functions.
- Treat PostgreSQL's plan as proof of global optimality.

## Decisions

### Use one pure pairwise comparison primitive

Introduce a function that accepts two plans and returns a `PlanComparison`. It builds
both decision mappings, takes their key union, and partitions the keys into matches and
differences using the existing choice-equivalence rule.

The function has no connection, SQL, or source-specific arguments. This keeps tests
deterministic and lets the GUI compare any two plans without invoking PostgreSQL.

**Alternative considered:** one function specialized to PostgreSQL versus agent plans.
Rejected because it couples a general structural operation to plan provenance and
would duplicate logic for the realized-plan comparison.

### Return typed immutable result objects

Use small immutable typed records consistent with the existing `Decision`, `Choice`,
and `Difference` records. The result graph will conceptually contain:

- a score with optional numerator and denominator, optional rate, status, and
  explanation; unavailable counts are absent unless independently knowable;
- all-kinds and core pairwise summaries;
- per-kind summaries;
- ordered matches and `Difference` entries;
- a realizability result with requested, reproduced, unmatched, extra, and gaps;
- target and attribution entries for realized decisions; and
- a top-level evaluation containing proposed, realizability, and realized results.

Rates remain numeric and unrounded. Formatting percentages belongs to CLI or GUI
presentation. Each top-level result also exposes a `to_dict()`-style JSON-safe view.
That view represents decisions and differences as ordered lists of primitive records,
not dictionaries keyed by `Decision`, and represents statuses as stable strings.

**Alternative considered:** return only dictionaries. Rejected because named fields
and type checking provide a more stable internal API. A primitive projection is still
required because GUI and later experiment consumers should not need custom encoders
for named tuples, enums, or decision-keyed mappings.

### Reuse one public choice-equivalence helper

Refactor the current private equality logic into one helper used by both
`differences()` and the new comparison. It retains the rule that an unnamed agent index
matches the same scan method with a named PostgreSQL index.

**Alternative considered:** derive counts from `len(differences())` only. Rejected
because the metric also needs matches, per-kind totals, filtered universes, and explicit
choices for GUI consumers.

### Define the pairwise denominator as the decision-key union

The all-kinds denominator is `keys(decisions(a)) union keys(decisions(b))`. A key that
exists on only one side is a difference. This exposes added or removed operations
instead of restricting the metric to their intersection.

For an empty union, pairwise difference is defined as 0 and similarity as 1, with an
explanation that no comparable decisions existed. This is an identity convention, not
an unavailable measurement.

**Alternative considered:** intersection-only denominator. Rejected because it would
make missing Sorts, Gathers, joins, or scans disappear from the score.

### Compute the core view from the same comparison

The core view filters out exactly `parallel` and `gather` decision keys before counting.
It is returned alongside, never instead of, the official all-kinds result. Per-kind
summaries are calculated from the same partitions and must reconcile with all-kinds
totals.

**Alternative considered:** weight parallelism or join decisions. Rejected because
weights would introduce subjective tuning that is harder to justify than transparent
raw and filtered views.

### Measure realizability against requested force-eligible decisions

The denominator is the set of agent decision keys whose kind is in `FORCED_KINDS`.
The numerator contains requested keys with an equivalent choice in the realized plan.
Extra force-eligible keys appearing only in the realized plan are diagnostic evidence,
not agent requests, so they do not increase the denominator or count as realization
gaps. Retry selection therefore minimizes unmatched requested force-eligible decisions,
not extras introduced by PostgreSQL.

`FORCED_KINDS` defines the stable comparison universe, not proof that a hint was
emitted for every member. A missing alias can suppress a scan hint, an unnamed join
input can suppress join and Leading hints, a parallel scan without a known worker count
can suppress its Parallel hint, and the chosen retry can set `leading=False`.
Realizability therefore answers whether requested force-eligible choices appeared in
the result. Separate target metadata answers whether the selected attempt actually
asked PostgreSQL for each choice.

When the denominator is zero, the rate is `None`, status is `not_applicable`, and the
explanation states that the agent requested no force-eligible decisions. This differs from
the empty pairwise identity convention because no realization claim can be made.

### Compose the three perspectives above the pairwise primitive

A pure evaluator always accepts PostgreSQL's normal plan and the validated agent plan.
It also accepts either a successful realized outcome or an unavailable outcome with a
failure reason. It calculates:

1. proposed comparison: normal PostgreSQL versus agent proposal;
2. realizability: agent proposal versus realized plan for force-eligible kinds; and
3. realized comparison: normal PostgreSQL versus realized plan.

The proposed comparison is always calculated. If the realized outcome is unavailable,
realizability and realized comparison carry status `unavailable` and the failure
reason instead of causing the evaluation to be discarded. The evaluator does not call
`force_plan()`. Orchestration remains in `project.py`, which owns connections, errors,
and output sequencing.

### Track the targets of the selected forcing attempt

Hint generation must return internal metadata alongside the hint text that records
which semantic decisions its emitted hints target. The tracking follows the same
branches that emit hints rather than reparsing the final hint string:

- a scan decision is targeted only when its scan hint is emitted;
- a parallel decision is targeted when its per-scan `Parallel` hint or the applicable
  query-wide serial setting targets it;
- a join decision is targeted only when its join-method hint is emitted; and
- an outer decision is targeted only when an emitted `Leading` hint covers it.

Introduce a richer hint-build/forcing result for internal orchestration, including the
selected attempt's hint text, realized plan, gaps, and target decisions. Keep
`build_hints()` returning a string and keep `force_plan()` returning its current tuple
through compatibility wrappers so existing callers do not break. The rich forcing
result must describe the attempt selected after retries, not the first attempt.

### Attribute only targeted and reproduced choices

A realized decision is agent-attributable only when the selected attempt targeted the
same requested decision, the realized choice is equivalent, and no realization gap
applies. A matching force-eligible decision without target evidence is explicitly
non-attributable because PostgreSQL may have selected it independently. Unforced
surrounding operators and gapped requests are also labelled non-attributable with a
reason suitable for display.

This metadata is factual; it does not classify a difference as an agent error,
PostgreSQL rewrite, or forcing limitation.

### Keep CLI output as a consumer of the result object

`project.py` will print the three headline scores, all-kinds/core context, and gap
summary from the structured evaluation. Its forcing helper must preserve the rich
success result or a structured failure reason instead of returning only `PlanNode` or
`None`. The CLI will not exit before reporting the proposed comparison when forcing
fails, and it will not recalculate rates or attribution. The JSON-safe projection is
the contract for GUI and experiment handoff.

## Risks / Trade-offs

- **Join-order cascades inflate counts** -> Return per-kind counts and preserve raw
  differences so the report can explain that one strategy may create several keys.
- **The core view could be mistaken for the official metric** -> Label it diagnostic
  and always return it beside the all-kinds result.
- **Union denominators make large structural edits prominent** -> Preserve numerator
  and denominator and document the interpretation rather than hiding missing keys.
- **Two operations of the same kind and alias set can collapse in `decisions()`** ->
  Document the existing limitation; do not claim node-level edit distance.
- **GUI consumers may rely on presentation strings** -> Keep stable statuses and
  machine-readable fields; treat explanations as display context rather than keys.
- **A force-eligible match may be mistaken for a caused result** -> Report selected
  attempt targets separately and require target evidence for attribution.
- **Partial results may be mistaken for zero scores** -> Use distinct `unavailable`
  and `not_applicable` statuses and never substitute either with 0%.
- **Typed records may not serialize consistently across consumers** -> Publish one
  tested primitive representation with stable string field names.
- **Existing callers could break during refactoring** -> Retain current public
  functions and cover their behaviour with regression tests.

## Migration Plan

1. Add result types, statuses, JSON-safe projections, and the shared equality helper
   without changing current callers.
2. Add hint-target tracking and a rich forcing result behind compatibility wrappers.
3. Add pairwise, realizability, attribution, and partial composite evaluation functions.
4. Add unit tests for old and new comparison and forcing-result behaviour.
5. Switch `project.py` to preserve forcing evidence and print the structured evaluation,
   including partial results after forcing failure.
6. Leave existing detailed difference and forcing APIs available for compatibility and
   debugging.

Rollback consists of reverting the new result types and `project.py` integration; the
existing difference and forcing APIs remain independently usable.
