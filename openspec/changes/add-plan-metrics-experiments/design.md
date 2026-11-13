# Design

## Context

This change follows the deterministic result API planned by
`add-plan-comparison-metrics`. The current pipeline already exposes enough context for
an advisory analyst: rewritten SQL, catalog-derived database and index facts, the
agent's accepted plan and raw answer, PostgreSQL's normal plan, the realized hinted
plan, generated hints, detailed differences, and realization gaps.

The agent is nondeterministic, failures are currently printed rather than preserved,
and no workload-level result format exists. The same Haiku model family may serve as
the planner and analyst, so its interpretation cannot be treated as independent ground
truth. See `proposal.md` and the capability spec for required behaviour.

## Goals / Non-Goals

**Goals:**

- Preserve every attempted run and enough context to reproduce or audit it.
- Aggregate repeated samples without allowing large queries or oversampled queries to
  dominate silently.
- Add one bounded, structured analyst request per query run when enabled.
- Keep deterministic facts authoritative and analyst output explicitly advisory.
- Support later GUI presentation, report tables, case studies, and human annotations.

**Non-Goals:**

- Change any comparison formula or attribution rule.
- Use the analyst to accept, reject, or repair the planner's plan.
- Automatically prove the semantic cause of a difference.
- Treat analyst confidence as a calibrated probability.
- Benchmark execution time unless a later design explicitly adds controlled timing.

## Decisions

### Build the experiment layer on stored deterministic evaluations

The runner invokes the existing plan pipeline and the structured evaluation from the
first change. It stores each stage before proceeding to optional analysis. The analyst
never participates in generating a score.

**Alternative considered:** let the analyst emit classifications during comparison.
Rejected because a model failure or hallucination could then contaminate objective
metrics and make tests nondeterministic.

### Use one immutable run directory per attempt

Store each attempt under a stable experiment/run identifier in the existing ignored
`agent_runs/` area. A run record contains a versioned JSON manifest plus separate raw
text or JSON evidence where preserving the original representation matters. Human
annotations live in a separate file so they never overwrite generated evidence.

The manifest records:

- experiment, query, sample, and timestamp identifiers;
- original and rewritten SQL;
- model names, effort, retry history, and raw answers;
- PostgreSQL version, database scale, indexes, and relevant planner settings;
- normal, proposed, and realized plans;
- hints, gaps, deterministic evaluations, and statuses;
- analyst request/response status and validated suggestions; and
- references to optional human annotations.

**Alternative considered:** one growing CSV file. Rejected because nested plans,
failures, evidence, and versioning do not fit safely in a flat row and interrupted runs
could corrupt the whole workload.

### Persist failures at the stage where they occur

Each run advances through explicit stages such as plan acquisition, forcing,
evaluation, and analysis. A failure finalizes the current status and retains all prior
evidence. Later metrics are represented as unavailable with reasons instead of being
omitted or fabricated.

This supports honest success-rate reporting and lets a proposed-plan metric survive a
forcing failure.

### Aggregate samples per query before macro-averaging

For repeated sampling, first average defined sample rates for each query. The workload
macro-average is then the mean of those per-query values, giving each query equal
weight. Report the number of queries and samples included or excluded at every level.

A separately labelled micro-average pools raw numerators and denominators across all
defined run scores. It is diagnostic because queries with more decisions or more
samples can contribute more weight.

**Alternative considered:** average every sample directly. Rejected because a query
with more requested samples would dominate the macro result.

### Run one analyst conversation per completed query attempt

When enabled, the analyst receives all ambiguous differences for one run in one request
so it can recognize join-order cascades and shared causes. Use a separate
`ANALYST_MODEL` setting and a fresh conversation even when it initially names the same
Haiku model as the planner.

The prompt includes:

- rewritten SQL and relevant catalog-derived schema, statistics, and index facts;
- structural forms of all three plans with conditions needed for interpretation;
- stable identifiers for detailed differences;
- forceability, followed/gapped, and attribution metadata;
- generated hints and documented forcing limitations.

The prompt excludes hidden model reasoning. It also omits the planner's summary,
per-node reasons, and raw answer from the analyst context by default to reduce
self-rationalization; those remain saved for human inspection. Planner and forced-plan
costs are omitted because hints distort cost values. The analyst receives the database
statistics already shared with the planner instead.

**Alternative considered:** one call per difference. Rejected because it costs more,
loses whole-plan context, and can produce contradictory explanations for cascading
differences.

### Validate analyst output against a strict advisory schema

The response contains an overall summary and assessments keyed only by supplied stable
difference identifiers. Each assessment has:

- one cause from the documented vocabulary;
- `low`, `medium`, or `high` confidence;
- concise explanation and evidence references;
- an alternative cause or explicit `none`; and
- a human-review flag.

Unknown identifiers, invalid causes, and missing required evidence are rejected or
produce a partial result. A small retry allowance may return validation problems to the
analyst, but exhaustion records `invalid_response` and leaves metrics untouched.

**Alternative considered:** free-form prose. Rejected because the GUI, report tooling,
and human-review workflow need stable fields and must distinguish unknown from confident
claims.

### Treat objective metadata as immutable prompt facts

The analyst may suggest why a difference exists, but it cannot override whether a kind
is forceable, a choice was followed, a gap exists, or a realized decision is
agent-attributable. Any response contradicting those facts is flagged and the facts
remain unchanged.

Analyst results are always labelled as an AI-generated second opinion. Confidence is
qualitative and displayed with evidence and the human-review flag.

### Keep human annotations separate and append-safe

A human reviewer may record a cause from the same vocabulary plus free-text notes and
review metadata. The annotation references a stable run and difference identifier and
does not replace the original analyst assessment. `unknown` is valid for both human and
AI classifications.

This allows the report to distinguish deterministic facts, AI suggestions, and human
judgement.

### Version the experiment schema and analyst prompt

Every run records a result-schema version and analyst-prompt version. Readers reject or
migrate incompatible records explicitly rather than silently misreading them. Model and
PostgreSQL versions are recorded because both can change plans across runs.

## Risks / Trade-offs

- **The same model family may rationalize its own plan** -> Use a fresh conversation,
  omit planner reasons from the prompt, require alternatives and `unknown`, and label
  output advisory.
- **Analyst explanations may sound more certain than evidence permits** -> Require
  confidence, evidence, and a human-review flag; deterministic facts remain primary.
- **Database descriptions and plans can create large prompts** -> Send only tables and
  statistics relevant to the query and one batch of differences per run.
- **Repeated calls increase cost and latency** -> Make analysis optional, one call per
  run, and preserve results so rerendering does not call the model again.
- **Partial runs can bias averages** -> Report failure and exclusion counts beside every
  aggregate and retain all attempts.
- **Stored records may become incompatible** -> Version schemas and keep raw source
  evidence for migration or reanalysis.
- **Manual annotations can diverge** -> Store reviewer identity/timestamp and preserve
  both AI and human records rather than choosing one silently.

## Migration Plan

1. Complete and consume the structured comparison result from
   `add-plan-comparison-metrics`.
2. Define versioned run, workload, analyst, and annotation schemas.
3. Add the runner with failure-preserving persistence and no analyst enabled.
4. Add per-query, macro, and micro aggregation with inclusion counts.
5. Add the separate analyst prompt, response validation, and failure isolation.
6. Add optional human annotations and case-study retrieval.
7. Run a small workload before the full evaluation to validate record size, cost, and
   analyst usefulness.

Rollback disables the experiment runner and analyst without affecting deterministic
comparison APIs or existing command-line query planning.
