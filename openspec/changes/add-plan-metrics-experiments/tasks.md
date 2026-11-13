# Tasks

## 1. Versioned experiment records

- [ ] 1.1 Define versioned workload, run, stage-status, analyst, and human-annotation
  record models that embed the structured metric result; verify JSON round trips
  preserve optional rates, raw counts, plans, and failure reasons.
- [ ] 1.2 Implement one immutable run directory per attempt under the ignored
  experiment output area; verify interrupted and failed writes cannot overwrite another
  run and raw evidence remains retrievable.
- [ ] 1.3 Document the on-disk schema, version fields, and reproducibility metadata; verify
  a documented sample record validates with the record models.

## 2. Failure-preserving workload runner

- [ ] 2.1 Implement a runner for named SQL queries and configurable sample counts using
  the existing plan pipeline and deterministic evaluator; verify every attempted sample
  receives a distinct stored status.
- [ ] 2.2 Persist original and rewritten SQL, model and PostgreSQL settings, raw answers,
  retry problems, all available plans, hints, gaps, and metric results at their
  completed stages; verify a successful fixture contains every required field.
- [ ] 2.3 Preserve agent-plan, forcing, and evaluation failures with readable reasons
  and all earlier evidence; verify a proposed-plan result survives a later forcing
  failure and unavailable later metrics are explicit.

## 3. Workload aggregation

- [ ] 3.1 Aggregate repeated samples into one defined value per query before calculating
  macro-averages; verify oversampling one query does not increase its macro weight.
- [ ] 3.2 Add clearly labelled micro-averages from pooled raw numerators and denominators;
  verify their inclusion counts and values against hand-calculated fixtures.
- [ ] 3.3 Report defined, undefined, failed, and excluded query and sample counts beside
  every aggregate; verify no failed attempt silently disappears.
- [ ] 3.4 Document macro/micro interpretation and repeated-sample handling; verify the
  documented examples match aggregation tests.

## 4. Advisory Haiku analyst

- [ ] 4.1 Define a separate analyst model setting, cause vocabulary, prompt version, and
  validated response schema; verify `unknown`, confidence, evidence, alternatives, and
  human-review fields are accepted while unknown decisions and causes are rejected.
- [ ] 4.2 Build one bounded analyst request per completed run from rewritten SQL,
  relevant database facts, structural plans, stable difference identifiers, objective
  metadata, hints, gaps, and forcing limitations; verify planner reasons, raw answer,
  hidden reasoning, and distorted hinted costs are absent from the rendered prompt.
- [ ] 4.3 Run the analyst in a fresh conversation and validate or retry its response
  without mutating deterministic evaluation objects; verify a fake client cannot change
  counts, rates, gaps, forceability, or attribution.
- [ ] 4.4 Record `completed`, `partial`, `unavailable`, `invalid_response`, and
  `not_requested` analyst outcomes with human-readable reasons; verify refusal,
  timeout, malformed output, contradictory claims, and disabled-analysis fixtures leave
  all metrics valid.
- [ ] 4.5 Document that analyst output is an AI-generated second opinion, including
  same-model-family bias and qualitative confidence; verify consumer examples display
  the advisory label and evidence.

## 5. Human review and case studies

- [ ] 5.1 Add separate append-safe human annotations keyed by run and difference ID;
  verify they preserve the original analyst suggestion and allow `unknown` with notes.
- [ ] 5.2 Add retrieval of all evidence for selected run and difference identifiers;
  verify a case-study fixture returns query, plans, metrics, hints, gaps, analyst
  suggestion, and human annotation together.
- [ ] 5.3 Document a review workflow and candidate Q16, Q17, Q20, and join-order case
  studies; verify the workflow does not require rerunning the planner or analyst.

## 6. Integration verification

- [ ] 6.1 Run all experiment, comparison, and agent tests and verify the optional analyst
  introduces no regression or nondeterminism into deterministic metric tests.
- [ ] 6.2 Run a small multi-query, repeated-sample experiment with a fake analyst and
  verify records, failure counts, macro/micro summaries, and case-study retrieval agree.
- [ ] 6.3 When database and API credentials are available, run one opt-in live analyst
  sample and verify the stored request has sufficient context, the response is labelled
  advisory, and rerendering results makes no additional model call.

## Workflow follow-up

- Apply this change only after `add-plan-comparison-metrics` is implemented and its
  result API is reviewed.
- Archive the change after the experiment schema, report outputs, and analyst boundary
  have been reviewed.
