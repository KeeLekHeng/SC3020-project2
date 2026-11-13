# Proposal

## Why

Per-query metrics alone are not enough to evaluate the agent across a diverse workload
or reproduce results from a nondeterministic model. The project needs an experiment
layer that preserves evidence, aggregates results, and offers an explicitly advisory
second opinion on ambiguous differences.

## What Changes

- Run the comparison metrics over a configurable workload without changing their
  deterministic definitions.
- Persist the query, model and database context, raw agent answer, all three plans,
  generated hints, realization gaps, and metric inputs and outputs for each run.
- Calculate per-query results plus macro- and clearly labelled micro-averages.
- Retain failed agent calls, unforceable plans, repeated samples, and undefined results
  instead of silently excluding them.
- Add one separate Haiku analyst call per query for ambiguous differences.
- Require the analyst to return a suggested cause, confidence, evidence, an alternative
  explanation, and whether human review is recommended.
- Keep analyst suggestions outside every metric calculation and allow `unknown` or an
  unavailable analyst result without invalidating the experiment.
- Support optional human cause annotations and report-oriented case studies.
- Build on the structured results introduced by `add-plan-comparison-metrics`; do not
  duplicate the deterministic comparison logic.

## Capabilities

### New Capabilities

- `plan-metrics-experiments`: Reproducible workload evaluation, aggregation, evidence
  persistence, and advisory analysis for plan-comparison metrics.

### Modified Capabilities

None.

## Impact

- Adds an experiment runner and a stable on-disk result format.
- Uses the existing Anthropic integration through a separate analyst prompt and model
  setting.
- Adds validation and tests for analyst responses and experiment aggregation.
- Produces data suitable for the report and later GUI presentation.
- Depends functionally on the plan-comparison metric result API from the first change.
