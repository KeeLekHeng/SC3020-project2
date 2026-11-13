# Spec Delta

## Purpose

Defines deterministic, interpretable metrics for comparing PostgreSQL, agent-proposed,
and realized query execution plans without conflating plan similarity, feasibility, or
agent attribution.

## ADDED Requirements

### Requirement: Pairwise plan comparison
The system SHALL compare any two valid plan trees through their semantic planner
decisions and return matching, different, and total decision counts with similarity
and difference rates.

#### Scenario: Identical decision mappings
- **WHEN** two plans contain the same decision keys and choices
- **THEN** the result reports zero different decisions, a 0% difference rate, and a
  100% similarity rate

#### Scenario: Changed choice
- **WHEN** the plans contain the same decision key with different choices
- **THEN** that key is counted once as a different decision

#### Scenario: Decision present on only one side
- **WHEN** a decision key exists in only one plan
- **THEN** the union includes that key and counts it as a difference

#### Scenario: Empty comparison universe
- **WHEN** neither plan produces a comparable decision
- **THEN** the result reports zero differences, a 0% difference rate, a 100% similarity
  rate, and explanatory context for the empty universe

### Requirement: Established choice equality
The metric SHALL use the same choice-equivalence semantics as the detailed difference
list so rates and explanations cannot disagree.

#### Scenario: Agent omits an index name
- **WHEN** one scan choice names only a scan method and the other names the same method
  with a specific index
- **THEN** the choices are treated as matching

### Requirement: Transparent metric values
Every metric score SHALL expose its numerator, denominator, rate, status, and a
human-readable explanation when the rate is unavailable or requires context. Status
SHALL distinguish `defined`, `not_applicable`, and `unavailable`.

#### Scenario: Defined score
- **WHEN** a metric has a non-zero applicable denominator
- **THEN** the result exposes the raw counts, a numeric rate, and status `defined`

#### Scenario: Not-applicable score
- **WHEN** a metric has no applicable decisions
- **THEN** its rate is absent, its status is `not_applicable`, and its explanation states
  why no rate can be calculated

#### Scenario: Unavailable score
- **WHEN** a required plan or forcing outcome could not be obtained
- **THEN** the dependent metric has no numeric rate, has status `unavailable`, and
  retains a human-readable failure reason
- **AND** counts that depend on the missing outcome are absent rather than reported as
  zero, while any independently known denominator may remain available

### Requirement: Proposed-plan difference
The system SHALL calculate proposed-plan difference by comparing PostgreSQL's normal
plan with the original validated agent plan.

#### Scenario: Unrealizable proposal later matches PostgreSQL
- **WHEN** the agent proposes a choice different from PostgreSQL and the realized plan
  later falls back to PostgreSQL's choice
- **THEN** the proposed-plan result still counts the original agent choice as different

### Requirement: Plan realizability
The system SHALL measure the share of the agent's requested force-eligible decisions
whose choices are reproduced in the realized plan. Force eligibility SHALL mean that
the decision kind is in `FORCED_KINDS`; it SHALL NOT by itself claim that the selected
forcing attempt emitted a hint for that individual decision.

#### Scenario: All requested force-eligible decisions reproduced
- **WHEN** every agent decision of kind `scan`, `parallel`, `join`, or `outer` is matched
  in the realized plan
- **THEN** realizability is 100%

#### Scenario: Some requested force-eligible decisions not reproduced
- **WHEN** one or more requested force-eligible decisions are missing or changed in the
  realized plan
- **THEN** realizability excludes them from the reproduced numerator and preserves their
  realization gaps for explanation

#### Scenario: Agent requests no force-eligible decisions
- **WHEN** the agent plan has no decision of kind `scan`, `parallel`, `join`, or `outer`
- **THEN** realizability is `not_applicable` with no numeric rate and explains that no
  force-eligible agent decisions were available

#### Scenario: Realized plan adds an unrequested decision
- **WHEN** the realized plan contains a force-eligible decision key not requested by the
  agent
- **THEN** that extra key is retained as diagnostic evidence but does not increase the
  denominator of requested agent decisions and is not classified as a realization gap

#### Scenario: Force-eligible decision was not individually targeted
- **WHEN** a requested decision kind is in `FORCED_KINDS` but the selected forcing
  attempt emitted no hint that targets that decision
- **THEN** the decision remains in the realizability denominator and its target status
  is retained as diagnostic context rather than being presented as proof that
  PostgreSQL refused a hint

### Requirement: Realized-plan difference
The system SHALL calculate realized-plan difference by comparing PostgreSQL's normal
plan with the executable plan produced from the agent's hints.

#### Scenario: PostgreSQL corrects an agent choice
- **WHEN** the proposed agent choice differs but the realized choice matches the normal
  PostgreSQL plan
- **THEN** the realized-plan result reports that decision as matching while the
  proposed-plan and realizability results retain the original disagreement and gap

### Requirement: Core and per-kind diagnostic views
Each pairwise result SHALL provide the official all-kinds result, a core structural
view excluding exactly `parallel` and `gather`, and a breakdown by decision kind.

#### Scenario: Parallel-only disagreement
- **WHEN** two plans differ only through `parallel` and `gather` decisions
- **THEN** the all-kinds rate reports the differences and the core structural rate is
  0%

#### Scenario: Gather moves between subtrees
- **WHEN** one Gather location disappears and another appears
- **THEN** both keyed Gather differences remain in the all-kinds result and neither is
  included in the core structural result

#### Scenario: Per-kind reconciliation
- **WHEN** a pairwise result contains multiple decision kinds
- **THEN** the per-kind totals reconcile exactly with the all-kinds totals

### Requirement: Conservative agent attribution
The system SHALL label a realized decision as agent-attributable only when the selected
forcing attempt explicitly targeted that decision, PostgreSQL reproduced the requested
choice, and no realization gap applies.

#### Scenario: Reproduced targeted decision
- **WHEN** the selected forcing attempt targeted an agent-requested decision and its
  choice matches in the realized plan without a gap
- **THEN** the decision is labelled agent-attributable

#### Scenario: Coincidental match without a target
- **WHEN** an agent-requested decision matches in the realized plan but the selected
  forcing attempt emitted no hint that targets it
- **THEN** the decision is not labelled agent-attributable and the result explains that
  the match may be PostgreSQL's own choice

#### Scenario: Retry omits join-order hint
- **WHEN** `force_plan()` selects an attempt with `leading=False`
- **THEN** matching `outer` decisions not covered by an emitted `Leading` hint are not
  labelled agent-attributable

#### Scenario: Unforced operator decision
- **WHEN** a realized difference concerns an Aggregate, Gather, Sort, Limit,
  Materialize, or another unforced decision kind
- **THEN** it is not labelled agent-attributable and the result explains that
  PostgreSQL selected surrounding operators while realizing the hints

#### Scenario: Force-eligible decision with a gap
- **WHEN** a force-eligible agent decision has a realization gap
- **THEN** it is not labelled agent-attributable

### Requirement: Structured evaluation for consumers
The system SHALL return one structured evaluation containing the three primary
metrics, pairwise details, per-kind breakdowns, realization gaps, attribution metadata,
forcing-target metadata, and display-ready status explanations. It SHALL also expose a
JSON-safe representation containing only primitive values, lists, and string-keyed
objects.

#### Scenario: GUI consumes evaluation
- **WHEN** a caller requests an evaluation after all three plans are available
- **THEN** it can render counts, rates, detailed differences, gaps, and explanations
  without recalculating comparison logic

#### Scenario: Forcing is unavailable
- **WHEN** PostgreSQL's normal plan and the validated agent plan are available but plan
  forcing fails before a realized plan is produced
- **THEN** the evaluation retains a defined proposed-plan comparison and marks
  realizability and realized-plan difference `unavailable` with the forcing failure
  reason

#### Scenario: JSON-safe handoff
- **WHEN** a GUI or experiment consumer requests the serializable representation
- **THEN** it can encode the complete evaluation as JSON without custom handling for
  `PlanNode`, `Decision`, `Choice`, enum, tuple-keyed, or other internal objects

#### Scenario: Command-line summary
- **WHEN** the command-line pipeline completes an evaluation
- **THEN** it prints a concise summary derived from the same structured evaluation used
  by other consumers
