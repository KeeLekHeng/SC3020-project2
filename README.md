# SC3020-project2

Compare PostgreSQL's execution plan for a query with a plan proposed by an AI agent, Claude Haiku 5.5. For each query, `project.py --experiments` gets PostgreSQL's plan, asks the agent for its own plan, forces that plan onto PostgreSQL with `pg_hint_plan` hints, compares the plans, saves the run, and aggregates the comparison metrics over the workload. The data is TPC-H. The GUI is separate work.

## Requirements

- Python 3. The commands below use the Windows `py` launcher; on macOS or Linux, use `python3` in its place.
- A running PostgreSQL server and a database named `tpch`. The `Dockerfile` provides PostgreSQL 16 with `pg_hint_plan`:

  ```bash
  docker build -t pg-tpch .
  docker run -d --name tpch -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=tpch \
    -p 127.0.0.1:5432:5432 -v tpch-db:/var/lib/postgresql/data pg-tpch
  ```
- The packages in `requirements.txt`: Psycopg 3, Pydantic, sqlglot and the Anthropic SDK:

  ```powershell
  py -m pip install -r requirements.txt
  ```

  With `uv`: `uv venv`, then `uv pip install -r requirements.txt`.
- For the AI agent: an Anthropic API key, in the `ANTHROPIC_API_KEY` environment variable or in a gitignored `.env` file in the project root. Copy `.env.example` to `.env` and add the key there.

## Load the database

`data/` holds the eight generated TPC-H `.tbl` files. The Docker command above creates the empty `tpch` database; without Docker, create it yourself. Then from the project root run:

```powershell
py prepare_data.py
```

Enter your PostgreSQL password when prompted. The script requires exactly eight `.tbl` files in the project folder and its subfolders. It strips each row's trailing `|` and writes headerless CSV files to `data_clean/`, overwriting old ones. It then runs `schema.sql`, loads the tables in foreign key order and runs `ANALYZE`, using the connection settings in `project.py`.

The load is one transaction, so a failure leaves no tables behind. If the tables already exist, the script stops; drop them or recreate the database first.

## Run the experiment workload

```powershell
py project.py --experiments
```

Enter your PostgreSQL password when prompted. Change these settings in `project.py` if your setup differs:

| Setting | Value |
| --- | --- |
| Host | `localhost` |
| Port | `5432` |
| Database | `tpch` |
| User | `postgres` |
| Connection timeout | 10 seconds |

This runs every query in `queries/` (TPC-H Q1–Q22; add more as `.sql` files), or only those named with `-q`/`--queries`, such as `-q Q1 Q6`, once each, and saves one JSON record per query under `agent_runs/<UTC start time>/`. For each query, it gets PostgreSQL's plan, asks the agent for its plan (up to three answers), forces that plan, times both plans with `EXPLAIN ANALYZE` as the mean of three runs after a warm-up, and asks the Haiku analyst about any decision PostgreSQL did not follow. It prints one row per query, with its status, the agent's answer count, the four metrics as fractions, whether each plan runs in parallel and the time ratio, then the workload counts: usable agent plans, plans the same as PostgreSQL's, plans forced and parallel plans. This step needs the Anthropic API key. [docs/plan-forcing.md](docs/plan-forcing.md) describes the hints and what they cannot force; [docs/experiments.md](docs/experiments.md) describes the records and the summary.

## Get the AI agent's plan

`preprocessing.get_agent_plan()` asks the agent for its plan for a query and returns it as a `PlanNode` tree, ready for `force_plan()` and `differences()`. To use it from your own code:

```python
from comparison import differences, force_plan
from preprocessing import get_agent_plan, get_postgres_plan

result = get_agent_plan(connection, original_sql)
postgres = get_postgres_plan(connection, result.query)
forced = force_plan(connection, result.query, result.plan)
diffs = differences(postgres, forced.plan)
```

Plan and force with `result.query`, the query after `unique_aliases()`, because the agent's aliases refer to it. `result` is an `AgentResult`:

| Field | Meaning |
| --- | --- |
| `plan` | The agent's plan as a `PlanNode` tree. Each node's `reason` holds the agent's one-line reason for it. |
| `summary` | One or two sentences on the agent's overall strategy. |
| `query` | The query the agent planned. |
| `attempts` | Every answer in order, each with the problems sent back for it; the last is the accepted answer. |

It raises `AgentPlanError`, with `.errors`, when no answer is usable, including when the model declines or its answer is cut off, and `AgentError` when an API request fails, such as on a rate limit. Both carry the answers so far in `.attempts`. A missing API key raises the Anthropic SDK's error. Answers vary from call to call, so save `result.attempts` when a run must be repeatable.

### How it works

The agent never sees PostgreSQL's plan or estimates, and its answer is not checked against them.

1. **What the agent is told.** `describe_database()` reads the catalogs for the tables and columns the query uses; no schema is hardcoded.
   - Server: the PostgreSQL version and the planner settings in `AGENT_SETTINGS`: memory, parallel workers, and page and parallel costs.
   - Tables: row and page counts, the share of all-visible pages, and foreign keys between the tables read.
   - Columns: distinct counts, NULL fraction, common values when they are skewed, strong correlation with the table's order, and the average width of variable-length types. A column compared with `<`, `>` or `BETWEEN` also gets its range and quartiles.
   - Indexes: name, uniqueness, method, columns and size.
2. **The prompt.** The system prompt gives the task, the answer's JSON Schema from the pydantic model `AgentAnswer`, the plan conventions and a worked example. It is the same for every query, so it is cached. The example reads tables that are not in the TPC-H database, so no query there comes with its answer. The user message gives the database description, the aliases the plan must use and the query. The system prompt is about 6.6K characters and the user message 1–3.6K.
3. **The answer.** A flat list with one entry per operator, the root first, each with an `id` and the `id` of its `parent`. [docs/agent-plan.md](docs/agent-plan.md) describes the format, the others that were tried and why this one was chosen.
4. **The checks.** `validate_agent_plan()` checks the answer, then `ask_agent()` checks it against the query and the database. [The format document](docs/agent-plan.md#how-an-answer-is-checked) lists the checks.
5. **Retries.** All problems go back to the agent in one message, for up to three answers: the first at `low` effort, the rest at `medium`.

### Results on TPC-H Q1–Q22

One run on 2026-11-11 with the Docker database:

| Measure | Value |
| --- | --- |
| Plans accepted | 22/22 |
| Accepted on the first answer | 20/22 |
| Answers in total | 24 |
| Input tokens per request (cached part) | 3,760 (2,523) |
| Output tokens per request, thinking included | 2,448 |
| Input and output tokens per query | 4,102 and 2,671 |

A run over all 22 queries costs 3 to 4 US cents.

### Notes for the comparison and GUI work

- PostgreSQL plans 14 of the 22 queries in parallel; the agent chose parallel plans for 2 to 7, depending on the run. The metrics leave `parallel` and `gather` decisions out and report one parallel flag per plan instead; [docs/metrics.md](docs/metrics.md#parallelism-is-reported-separately) says why. [Comparing two plans](docs/plan-forcing.md#comparing-two-plans) says how each kind of decision is counted.
- PostgreSQL cannot follow every agent decision. `force_plan()` reports the ones it did not follow as gaps; count only followed decisions as the agent's. [docs/plan-forcing.md](docs/plan-forcing.md) says what hints fix, what they cannot, and how gaps are found.
- The agent can only write plans that hints can force, and the comparison is uneven in several ways. [docs/agent-plan.md](docs/agent-plan.md) lists what the agent cannot express and how fair the comparison is.
- When `get_agent_plan()` raises, show it as a failed agent plan, as `project.py --experiments` records it. The retry loop can run out of answers.

## Run the tests

Install the test dependencies, set your PostgreSQL password, then run the tests from the project root:

```powershell
py -m pip install -r requirements-dev.txt
$env:PGPASSWORD = "postgres"
py -m pytest
```

| File | Covers | Needs |
| --- | --- | --- |
| `tests/test_hints.py` | Hint building, the alias rewrite and plan comparison. | Nothing. |
| `tests/test_forcing.py` | Forcing PostgreSQL's own plan for TPC-H Q1–Q22, for query shapes that are hard to force and for a few edited plans, such as swapped join sides; every forced decision must be followed. | The database with `pg_hint_plan`. |
| `tests/test_agent.py` | The agent's answer validation, the prompt, the database description and the retry loop with a fake client. | No API key; the database for the description tests. |
| `tests/test_experiments.py` | Versioned run storage, offline recomputation, aggregation, timing summaries and advisory analysis with fake clients. | Nothing. |

Tests that need the database are skipped when it is unavailable. All use `EXPLAIN` only, so no query runs.

## Project files

| File | Purpose |
| --- | --- |
| `data/*.tbl` | Source TPC-H table data. |
| `prepare_data.py` | Converts the source data to CSV files in `data_clean/` and loads the database. |
| `schema.sql` | Creates the eight database tables. |
| `queries/` | The experiment workload, one query per `.sql` file; the file name is the query name. |
| `project.py` | The entry point. `--experiments` prompts for the password, runs the workload, stores one JSON record per query, and summarises the metrics. It also holds the record format and the advisory Haiku analyst. |
| `preprocessing.py` | `PlanNode`, the plan form shared by every plan source. `get_postgres_plan()` runs `EXPLAIN`, optionally with `ANALYZE` or hints. `unique_aliases()` gives each reused table name its own alias. `build_hints()` turns a plan into `pg_hint_plan` hints. `get_agent_plan()` asks the AI agent for its plan, described by `describe_database()` and checked by `validate_agent_plan()`. |
| `comparison.py` | `decisions()` lists a plan's planner decisions; `differences()` compares two plans; `force_plan()` forces a plan on PostgreSQL and reports, through `realization_gaps()`, the forced decisions it did not follow. |
| `docs/plan-forcing.md` | The hints that force a plan, how a forced plan is checked, how well forcing works, and what cannot be forced. |
| `docs/agent-plan.md` | The agent's plan format, the formats that were tried, and the limits of the format and the comparison. |
| `docs/experiments.md` | The run schema, analyst boundary, aggregation rules and JSON consumer contract. |
| `tests/` | The tests above; `tests/fixtures/` holds a sample query and an agent answer for it. |
| `agent_runs/` | Not in git: versioned raw run records, including plans, attempts, selected forcing evidence, timings and optional analyst feedback. |
