# Stored plan-metric experiments

`python project.py --experiments` runs each query in the workload once and saves one
JSON `RunRecord` per query as `agent_runs/<experiment>/<UTC start time>.json`. A run is
saved however far it got. The stored plans are the source of truth: the metrics are
recomputed from them when a record is loaded, never stored. `RunRecord` in `project.py`
defines the fields:

- `identity`: experiment, run, query and start time;
- `provenance`: record format version, git commit (none outside a git checkout), agent
  model and a hash of its system prompt, so runs from different code or prompts are
  not mixed;
- `evidence`: the SQL, the database description the agent saw, PostgreSQL's plan, the
  agent's plan and every answer it gave, the forcing outcome, and the timings;
- `failure_stage` and `failure_reason`: both empty for a completed run; otherwise
  `postgres_plan`, `agent_plan` (no usable plan), `agent_request` (the API request
  failed), `forcing` or `timing`, and why; and
- `analyst`: the optional advice below.

An existing file is never overwritten.

## Advisory analyst

When enabled, one Haiku call receives every realization gap of the run, parallel ones
included, with the three plans stripped of costs and times. Its suggested cause for each
gap is stored in the run's `analyst` section as an AI-generated second opinion.
`not_requested` means analysis was off; `completed` holds one assessment per gap, or
none when there was no gap and no call was made; `unavailable` and `invalid_response`
give the reason.

The same Haiku model proposed the plan, so its explanation is not independent ground
truth. It never feeds the metrics.

## Workload summary

`summarise_workload()` returns one row per query, recomputed from the stored runs:

- **Rates:** the four metrics as fractions. A rate that is undefined or unavailable
  is shown as N/A, never as zero. There is no average across queries;
  [docs/metrics.md](metrics.md#reporting-a-workload) gives the workload counts printed
  instead.
- **Agent answers:** for each query that reached the agent, how many answers it gave,
  out of three. A query whose third answer is still unusable fails at `agent_plan` and
  is listed as having no usable plan; a refusal or a cut-off answer counts as unusable
  and ends the attempts. A failed API request fails at `agent_request` and counts
  towards neither.
- **Execution time:** each plan runs once to warm the cache and then three times; the
  stored time is the mean of the three. Each row gives the agent-plan time over
  PostgreSQL's, or none when either timed out.
- **Parallelism:** the rates leave out parallel and gather decisions. Each query's
  PostgreSQL, agent and realized plans get a parallel flag instead, true when the plan
  has a Gather or Gather Merge; a plan the run never produced has none.
  [docs/metrics.md](metrics.md#parallelism-is-reported-separately) gives the reasons.

A GUI or report reader can load the records and recompute the summary without
connecting to PostgreSQL or calling either model.
