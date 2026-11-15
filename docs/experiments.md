# Stored plan-metric experiments

`python project.py --experiments` runs each query in `queries/` once, or those named with `-q`, and saves one JSON `RunRecord` per query as one line of `agent_runs/<UTC start time>.jsonl`, one file per experiment. A run is saved however far it got. The stored plans are the source of truth: the metrics are recomputed from them when a record is loaded, never stored. `RunRecord` in `project.py` defines the fields, all at the top level of each line:

- the query name, start time and record format version;
- `provenance`: the git commit (none outside a git checkout), agent model and a hash of its system prompt, so runs from different commits or prompts are not mixed;
- the original and rewritten SQL, and the database description the agent saw;
- PostgreSQL's plan, the agent's plan and every answer it gave;
- `forcing`: the realized plan and the hints and retry settings that produced it, gaps or not; empty if forcing raised an error, which fails the run at `forcing`, or was never reached;
- the mean time in milliseconds of PostgreSQL's plan and of the realized plan; empty for a plan that passed the one-minute timeout, which still completes the run, while a database error during timing fails it at `timing`;
- `failure_stage` and `failure_reason`: both empty for a completed run; otherwise `postgres_plan`, `agent_plan` (no usable plan), `agent_request` (the API request failed), `forcing` or `timing`, and why; and
- `analyst`: the optional advice below.

Runs are appended, so an existing run is never overwritten. A line cut short by a crash makes the file fail to load; delete that line and rerun the query.

## Advisory analyst

`--experiments` always passes an analyst client to `collect_run()`, so after each completed run one Haiku call receives every realization gap, parallel ones included, with the three plans stripped of costs and times. Its suggested cause for each gap is stored in the run's `analyst` section as an AI-generated second opinion. `not_requested` means the run failed before analysis, or no analyst client was passed; `completed` holds one assessment per gap, or none when there was no gap and no call was made; `unavailable` and `invalid_response` give the reason.

The analyst uses the agent's model, the one that proposed the plan, so its explanation is not independent ground truth. It never feeds the metrics.

## Workload summary

`summarise_workload()` returns one row per query, recomputed from the stored runs:

- **Rates:** the four metrics as fractions. A rate that is `not_applicable` or `unavailable` is shown as N/A, never as zero. There is no average across queries; [docs/metrics.md](metrics.md#reporting-a-workload) gives the workload counts printed instead.
- **Agent answers:** for each query that reached the agent, how many answers it gave, out of three. A query whose third answer is still unusable fails at `agent_plan` and is listed as having no usable plan; a refusal or a cut-off answer counts as unusable and ends the attempts. A failed API request fails at `agent_request` and counts as neither a usable nor an unusable plan.
- **Execution time:** each plan runs once to warm the cache and then three times; the stored time is the mean of the three. Each row gives the realized plan's time over PostgreSQL's, or none when either timed out.
- **Parallelism:** the rates leave out parallel and gather decisions. Each query's PostgreSQL, agent and realized plans get a parallel flag instead, true when the plan has a Gather or Gather Merge; a plan the run never produced has none. [docs/metrics.md](metrics.md#parallelism-is-reported-separately) gives the reasons.

The GUI and any report reader load the records and recompute the summary without connecting to PostgreSQL or calling the agent or the analyst.
