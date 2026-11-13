# Tasks

## 1. Shared comparison model

- [x] 1.1 Add immutable score, pairwise comparison, per-kind, realizability,
  forcing outcome, attribution, and top-level evaluation result types in
  `comparison.py`; distinguish `defined`, `not_applicable`, and `unavailable`, and
  verify type construction and field semantics with focused unit tests.
- [x] 1.2 Refactor choice equivalence into one helper shared by `differences()` and the
  new metrics; verify all existing comparison tests, including unnamed-index matching,
  still pass.

## 2. Pairwise metrics and diagnostic views

- [x] 2.1 Implement pure two-`PlanNode` comparison over the union of semantic decision
  keys; verify identical, changed, missing, and empty-universe scenarios with unit
  tests.
- [x] 2.2 Add all-kinds and core structural scores, excluding exactly `parallel` and
  `gather` from the core view; verify parallel-only and moved-Gather examples produce
  the specified totals.
- [x] 2.3 Add per-kind matching and difference breakdowns; verify their totals reconcile
  exactly with the all-kinds result.
- [x] 2.4 Update `docs/metrics.md` if implementation names or edge-case wording differ
  from the design, and verify every documented formula matches a unit-tested result.

## 3. Realizability and attribution

- [x] 3.1 Implement requested-decision realizability using only agent decisions in
  `FORCED_KINDS`; explicitly describe these as force-eligible rather than necessarily
  hint-targeted, and verify reproduced, unmatched, extra-realized, and zero-denominator
  scenarios, including the `not_applicable` explanation.
- [x] 3.2 Attach realization gaps and display-ready status reasons without using extra
  realized decisions in the denominator; verify gap details remain available to
  consumers.
- [x] 3.3 Add hint-target tracking to the same branches that emit scan, Parallel,
  join-method, and Leading hints; return the selected retry's targets in a rich forcing
  result while preserving the existing `build_hints()` and `force_plan()` APIs through
  compatibility wrappers. Verify skipped hints and a selected `leading=False` retry do
  not claim their corresponding decisions as targeted.
- [x] 3.4 Implement conservative realized-decision attribution; verify only targeted,
  reproduced, non-gapped choices are labelled agent-attributable, coincidental matches
  are not attributed, and unforced operators receive an explanatory reason.

## 4. Composite evaluation and consumers

- [x] 4.1 Implement a pure evaluator over PostgreSQL and agent-proposed plans plus an
  optional realized forcing outcome; verify the three results remain distinct when
  PostgreSQL corrects an agent choice, and verify forcing failure retains the proposed
  comparison while marking the other two metrics `unavailable` with its reason.
- [x] 4.2 Refactor the `project.py` forcing flow to preserve selected hints, gaps,
  targets, and structured failure reasons; print headline counts and rates, core
  context, and gap status without recalculating them or exiting before a partial
  evaluation. Verify output with deterministic success and forcing-failure pipeline
  tests.
- [x] 4.3 Add and document a JSON-safe primitive representation with stable field names,
  ordered primitive decision/difference entries, and string statuses; verify standard
  JSON encoding needs no custom handling and the GUI can render complete and partial
  results, `N/A`/unavailable reasons, details, gaps, and attribution without accessing
  comparison internals.

## 5. Integration verification

- [x] 5.1 Run the complete comparison and agent unit-test suites and verify existing
  `build_hints()`, `decisions()`, `differences()`, `realization_gaps()`, and
  `force_plan()` behaviour has no regressions.
- [x] 5.2 Run the project pipeline against the available PostgreSQL fixture or TPC-H
  database and verify one result contains the three metrics and the detailed difference
  list while preserving current error handling.

## Workflow follow-up

- Review the result API with the GUI owner before treating its field names as stable.
- Archive the change after implementation and review, then begin
  `add-plan-metrics-experiments` against the archived capability.
