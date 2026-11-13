# Spec Delta

## Purpose

Defines reproducible workload experiments and optional advisory analysis around the
deterministic plan-comparison metrics, while preserving failures, evidence, and human
control over causal interpretation.

## ADDED Requirements

### Requirement: Configurable workload evaluation
The system SHALL evaluate the deterministic plan metrics for every query and requested
sample in a configured workload.

#### Scenario: Multiple queries
- **WHEN** a workload contains multiple SQL queries
- **THEN** the system produces an individual run result for each attempted query

#### Scenario: Repeated samples
- **WHEN** a query requests more than one agent sample
- **THEN** each sample is stored as a distinct run with its own agent answer, plans,
  gaps, and metrics

### Requirement: Reproducible run records
Each experiment run SHALL preserve the inputs, generated evidence, environment context,
and unrounded metric values needed to inspect or reproduce the reported result.

#### Scenario: Successful run record
- **WHEN** a query produces an accepted agent plan and a realized plan
- **THEN** its record includes the original and rewritten SQL, model settings, raw agent
  answer, retry problems, all three plans, hints, gaps, database context, and complete
  metric outputs

#### Scenario: Metric precision
- **WHEN** a metric is stored
- **THEN** its numerator, denominator, status, and unrounded rate are retained instead
  of storing only a formatted percentage

### Requirement: Failures remain visible
The experiment SHALL retain failed or incomplete attempts with structured status and a
human-readable explanation rather than silently excluding them.

#### Scenario: Agent produces no usable plan
- **WHEN** all allowed agent answers are rejected
- **THEN** the run is stored as an agent-plan failure with the raw answer and validation
  problems

#### Scenario: Plan cannot be forced
- **WHEN** forcing is unavailable or refuses the requested plan
- **THEN** the run preserves the proposed-plan result and records why realizability and
  realized-plan results are unavailable

#### Scenario: Undefined metric
- **WHEN** a metric is not applicable
- **THEN** the run stores its absent rate, status, and human-readable reason

### Requirement: Workload aggregation
The system SHALL calculate workload summaries without hiding undefined results or
failures.

#### Scenario: Macro-average
- **WHEN** at least one query has a defined value for a metric
- **THEN** the summary reports the arithmetic mean of defined per-query rates and the
  number of included and excluded runs

#### Scenario: Micro-average
- **WHEN** raw numerators and denominators are available across runs
- **THEN** the summary may report their pooled ratio labelled explicitly as a
  micro-average

#### Scenario: Failed runs in summary
- **WHEN** one or more runs fail before producing a metric
- **THEN** the summary reports their count and status separately from the metric average

### Requirement: Advisory analyst input context
The optional analyst SHALL receive enough bounded evidence to suggest explanations for
ambiguous differences without recalculating or altering the metrics.

#### Scenario: Analyst request
- **WHEN** advisory analysis is enabled for a completed query run
- **THEN** one separate analyst request receives the rewritten SQL, relevant database
  and index context, the three plans, detailed differences, forceability and attribution
  metadata, hints, gaps, and known forcing limitations

#### Scenario: Planner reasoning isolation
- **WHEN** the analyst evaluates a run produced by the same model family
- **THEN** it runs in a separate conversation and is not given hidden planner reasoning

### Requirement: Structured analyst suggestion
The analyst SHALL return validated, advisory assessments that allow uncertainty and do
not present a suggested cause as confirmed fact.

#### Scenario: Ambiguous difference assessment
- **WHEN** the analyst assesses an ambiguous difference
- **THEN** it returns the decision identifier, suggested cause, confidence, concise
  explanation, evidence, at least one alternative or `none`, and a human-review flag

#### Scenario: Unknown cause
- **WHEN** the supplied evidence is insufficient
- **THEN** the analyst can return `unknown` and recommend human review

#### Scenario: Allowed cause vocabulary
- **WHEN** the analyst suggests a cause
- **THEN** it uses one of `planning_disagreement`, `postgresql_rewrite`,
  `agent_format_limitation`, `forcing_limitation`, `impossible_request`,
  `cardinality_estimation_difference`, or `unknown`

#### Scenario: Analyst references unknown decision
- **WHEN** an analyst response names a decision that was not supplied
- **THEN** the response is rejected or marked partial without changing the experiment's
  metrics

### Requirement: Analyst isolation from metrics
Analyst suggestions SHALL never change decision counts, rates, realization gaps,
forceability, or deterministic agent attribution.

#### Scenario: Analyst disagrees with objective metadata
- **WHEN** the analyst claims a non-forceable or gapped decision was followed and
  agent-attributable
- **THEN** the deterministic metadata remains authoritative and the conflicting
  suggestion is flagged for review

#### Scenario: Analyst unavailable
- **WHEN** the analyst request fails, is refused, times out, or returns unusable output
- **THEN** all deterministic metrics remain valid and the analyst result reports an
  unavailable or invalid-response status with a readable reason

#### Scenario: Analyst disabled
- **WHEN** analysis is not requested
- **THEN** the experiment completes with analyst status `not_requested`

### Requirement: Analyst output is explicitly advisory
Every consumer-facing analyst result SHALL be labelled as an AI-generated second
opinion rather than an independent or confirmed cause.

#### Scenario: GUI or report displays suggestion
- **WHEN** an analyst assessment is presented
- **THEN** it displays its advisory label, confidence, supporting evidence, and
  human-review recommendation

### Requirement: Optional human annotations
The experiment data SHALL permit a human analyst to add a cause and notes without
overwriting deterministic metrics or the original AI suggestion.

#### Scenario: Human records a different cause
- **WHEN** a human reviewer disagrees with the advisory suggestion
- **THEN** both the original suggestion and the separate human annotation remain
  available

#### Scenario: No human conclusion
- **WHEN** a reviewer cannot establish a cause
- **THEN** the human annotation can remain `unknown` with explanatory notes

### Requirement: Case-study evidence
The experiment output SHALL support selecting representative runs and differences for
report case studies without rerunning the model.

#### Scenario: Case-study selection
- **WHEN** a run is selected for discussion
- **THEN** its preserved query, plans, differences, hints, gaps, analyst suggestion, and
  human annotation can be retrieved together
