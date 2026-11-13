# Proposal

## Why

The project can list structural differences between query execution plans, but it does
not yet quantify them or distinguish the agent's proposal from the executable plan
PostgreSQL realizes from its hints. A deterministic, GUI-ready metric layer is needed
to evaluate the agent fairly without crediting PostgreSQL's corrections to it.

## What Changes

- Add a reusable pairwise comparison for any two `PlanNode` trees using the existing
  semantic planner decisions.
- Calculate proposed-plan difference, plan realizability, and realized-plan difference
  as separate results.
- Report all-kinds, core structural, and per-decision-kind counts and rates.
- Return structured explanations that distinguish defined, unavailable, and
  not-applicable results. If forcing fails, preserve the still-valid proposed-plan
  comparison while marking the forcing-dependent metrics unavailable.
- Track which decisions the selected forcing attempt actually targeted. Mark a
  realized decision as agent-attributable only when that attempt targeted it,
  PostgreSQL reproduced the requested choice, and no realization gap applies.
- Expose one typed evaluation result plus a JSON-safe primitive representation for
  command-line, GUI, and later experiment consumers.
- Add deterministic unit tests and a concise command-line summary.
- Keep batch experiments, persisted run records, workload aggregation, and LLM analyst
  opinions outside this change.

## Capabilities

### New Capabilities

- `plan-comparison-metrics`: Deterministic structural comparison and realizability
  metrics for PostgreSQL, agent-proposed, and realized query plans.

### Modified Capabilities

None.

## Impact

- `comparison.py` gains reusable metric and evaluation result APIs.
- `project.py` consumes the structured result for command-line output.
- Existing plan forcing and difference APIs remain available.
- Unit tests expand around comparison rates, hint-target attribution, partial
  evaluations, breakdowns, and unavailable results.
- GUI code can consume a documented JSON-safe result without reproducing comparison
  logic or serializing internal `PlanNode`, `Decision`, or `Choice` objects itself.
