# Stored plan-metric experiments

Each query sample produces one versioned JSON `RunRecord`. Raw plans and the selected
forcing outcome are the source of truth: metric percentages are recomputed when the
record is loaded. The main sections are:

- `identity`: experiment, run, query, sample and timestamp;
- `provenance`: schema version, Git commit and dirty-worktree flag;
- `planner` and `measurement`: material request and timing configuration;
- `database`: PostgreSQL version, exact planner-visible description, and validation
  index map;
- `evidence`: SQL, textual planner attempts, available P/A/R plans, selected forcing
  outcome and measured times;
- `analyst`: optional advisory status and assessments; and
- top-level status plus failure stage and reason for incomplete attempts.

Credentials are never fields in the schema. A dirty Git flag makes clear that the
commit alone does not identify every local code change.

By default, each run uses its UTC start time as its identifier and filename, for
example `agent_runs/report-workload/2026-11-14_15-20-30.123456Z.json`.
Microseconds reduce collisions; if a timestamp already exists, a numeric suffix such
as `-2` is added without overwriting the earlier file. Callers may supply an explicit
run identifier instead; duplicates of explicit identifiers raise an error. Existing records
with older identifiers remain loadable.

## Advisory analyst boundary

When enabled, one fresh Haiku conversation receives every realization gap for the run.
The response is stored inside that run's `analyst` section as an AI-generated second
opinion. Requested analysis records its model, prompt version and token limit.
`not_requested` means analysis was disabled; `completed` holds zero or more assessments;
`unavailable` and `invalid_response` include a readable failure reason. Zero-gap runs
complete with no assessments and make no model call.

The same Haiku model family may have proposed and analysed the plan, so its explanation
is not independent ground truth. Analyst fields are never inputs to deterministic
metric recomputation.

## Minimal failed-run example

```json
{
  "identity": {
    "experiment_id": "report-workload",
    "run_id": "q1-s1",
    "query_name": "Q1",
    "sample": 1,
    "created_at": "2026-11-14T12:00:00Z"
  },
  "provenance": {
    "schema_version": 1,
    "git_commit": "abc123",
    "git_dirty": false
  },
  "planner": {
    "model": "claude-haiku-5-5",
    "prompt_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "max_tokens": 64000,
    "first_effort": "low",
    "retry_effort": "medium",
    "max_attempts": 3
  },
  "measurement": {
    "timeout_ms": 60000,
    "warmup_runs": 1,
    "measured_runs": 1
  },
  "database": null,
  "evidence": {
    "original_sql": "SELECT * FROM orders",
    "rewritten_sql": "SELECT * FROM orders",
    "postgres_plan": null,
    "agent_plan": null,
    "agent_attempts": [],
    "forcing": null,
    "timing": null
  },
  "status": "failed",
  "failure_stage": "postgres_plan",
  "failure_reason": "PostgreSQL was unavailable",
  "analyst": {
    "status": "not_requested",
    "assessments": [],
    "reason": null
  }
}
```

## Aggregation

Defined sample rates are averaged within each query before the workload macro-average.
The separately labelled micro-average pools raw numerators and denominators, so runs
with more decisions carry more weight. Undefined and failed runs are counted beside
both results. Execution time uses `realized_ms / postgres_ms` per run and the median
ratio for repeated samples of one query; it has no pooled workload average.

## JSON-compatible consumer contract

The public boundary consists of Pydantic-validated JSON rather than manual serializers
on in-memory plan or comparison classes:

- `RunRecord.model_dump_json()` and `RunRecord.model_validate_json()` exchange raw run
  evidence and the separately labelled `analyst` section;
- `evaluation_json()` and `load_evaluation_json()` exchange a freshly recomputed
  `PlanEvaluation`; and
- `WorkloadSummary.model_dump_json()` and `model_validate_json()` exchange macro/micro
  values, inclusion counts, exclusion reasons, and per-query timing medians.

A GUI or report reader can load these records without connecting to PostgreSQL or
calling either model. Derived evaluation and summary JSON can always be regenerated
from compatible raw runs.
