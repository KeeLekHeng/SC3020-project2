# SC3020-project2

Compare PostgreSQL's execution plan for a query with a plan proposed by an AI agent, Claude Haiku 5.5. For each query, `project.py --experiments` gets PostgreSQL's plan, asks the agent for its own plan, forces that plan onto PostgreSQL with `pg_hint_plan` hints, compares the plans, times them, and saves the run. It prints the comparison metrics for each query and counts across the workload. The data is TPC-H by default, and other schemas can be loaded. `project.py` with no arguments opens a GUI that shows the same for one query.

## Requirements

- Python 3.12 or newer. The commands below use the Windows `py` launcher; on macOS or Linux, use `python3` in its place.
- A running PostgreSQL server with the `pg_hint_plan` extension installed, and a database named `tpch`. The `Dockerfile` provides PostgreSQL 16 with `pg_hint_plan`:

  ```powershell
  docker build -t pg-tpch .
  docker run -d --name tpch -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=tpch -p 127.0.0.1:5432:5432 -v tpch-db:/var/lib/postgresql/data pg-tpch
  ```
- The packages in `requirements.txt`: Psycopg 3, Pydantic, sqlglot, the Anthropic SDK and Streamlit:

  ```powershell
  py -m pip install -r requirements.txt
  ```

  With `uv`: `uv venv`, then `uv pip install -r requirements.txt`, then activate `.venv` or replace `py` with `uv run python` in the commands below.
- For the AI agent: an Anthropic API key, in the `ANTHROPIC_API_KEY` environment variable or in a gitignored `.env` file in the project root. Copy `.env.example` to `.env` and add the key there.

## Load the database

`data/` holds the eight TPC-H `.tbl` files, committed in this repository and generated at scale factor 0.1 (about 100 MB; `lineitem` has 600,572 rows). TPC-H Q1–Q22 in `queries/` are `qgen -d -s 0.1` output: the default parameters, with intervals and Q15's view rewritten for PostgreSQL. The Docker command above creates the empty `tpch` database; without Docker, create it yourself on a server with `pg_hint_plan`. Then from the project root run:

```powershell
py project.py --load-data
```

Enter your PostgreSQL password when prompted (`postgres` with the Docker command above), or set `PGPASSWORD` to skip the prompt. The loader reads every `.tbl` and `.csv` file in `data/` and loads each into the table with the file's name. It strips each `.tbl` row's trailing `|` and writes headerless CSV files to `data_clean/`, overwriting old ones. It then runs `schema.sql`, stops if a table has no file or a file has no table, loads the tables in foreign key order and runs `ANALYZE`, using the [connection settings](#run-the-experiment-workload) in `project.py`. It prints each table's row count and ends with `Database ready.`

The load is one transaction, so a failure leaves no tables behind. If the tables already exist, the load stops; drop them or recreate the database first.

### Use another schema

Create an empty database for it on the same server, such as `docker exec tpch createdb -U postgres library`, so that `pg_hint_plan` is available there too. Then load it by naming its DDL file, data folder and database. Paths are relative to where you run the command, and each option defaults to its TPC-H value:

```powershell
py project.py --load-data --schema library.sql --data library_data --database library
```

- **DDL:** `CREATE TABLE` statements for tables in the database's default schema, `public`. Primary keys, foreign keys and `CREATE INDEX` statements may be included.
- **Data:** one file per table in the data folder, named after the table in lowercase, such as `loan.csv`. A `.csv` file is comma-separated with `"` quoting and no header row, its columns in the table's order, and an empty unquoted field loads as NULL. A `.tbl` file is pipe-separated with a trailing `|`, as TPC-H's `dbgen` writes it; an empty field loads as NULL, and a field cannot contain `|`.

To query it, open the GUI and set **Database** under **Database connection** to that database, then type your queries; the examples in `queries/` are TPC-H. `--experiments` also takes `--database`, but it runs the TPC-H queries in `queries/`, so it runs on TPC-H only.

## Run the GUI

```powershell
py project.py
```

This opens the Streamlit GUI in your browser. Under **Database connection**, check the host, port, database and user (they default to the settings below), enter the password, and optionally press **Test connection**: it reports the PostgreSQL version and whether `pg_hint_plan` loads. Then type a query or pick one from `queries/` and press Execute. The query goes through the same steps as one query of the workload, and the run is appended to `agent_runs/<UTC time of the session's first run>.jsonl`. A query PostgreSQL cannot plan, such as one with a typo, shows its error and is not saved. This needs the Anthropic API key.

The sidebar has New query, which opens the query form above, and lists every run in `agent_runs/`, newest first, each labelled with its query, its time and its session's start time. Click a run to see its results; a new run opens when it finishes. A failed run's label starts with ✗, and **Delete run** on a run's page removes it from its file for good. Viewing a saved run needs neither the database nor the key.

The results panel shows the query's SQL, with a collapsed rewritten version below it when the plans were made from a version with unique aliases, then the four metrics, the timings and parallel flags, and PostgreSQL's plan (P), the agent's plan (A) and the realized plan (R) as three trees. Each decision that differs gets a number, shown on every node that holds it and in the table below, so the same number pairs the nodes being compared. A filled node in the agent's or realized plan made that decision differently from PostgreSQL's plan, which is outlined instead; a dashed border marks a decision no other plan makes; a red border and "not kept by R" mark a request the realized plan did not keep. Hovering a node shows its decisions in all three plans. Each metric's help icon defines it. A table lists every decision that differs, and expanders show the hints and the analyst's advice.

## Run the experiment workload

```powershell
py project.py --experiments
```

Enter your PostgreSQL password when prompted, or set `PGPASSWORD` to skip the prompt. `--database` picks another database; change the other settings in `project.py` if your setup differs:

| Setting | Value |
| --- | --- |
| Host | `localhost` |
| Port | `5432` |
| Database | `tpch` |
| User | `postgres` |
| Connection timeout | 10 seconds |

This runs every query in `queries/` (TPC-H Q1–Q22 and the handwritten H1–H3; add more as `.sql` files), or only those named with `-q`/`--queries`, such as `-q Q1 Q6`, once each, and saves one JSON record per query as a line of `agent_runs/<UTC start time>.jsonl`. For each query, it gets PostgreSQL's plan, asks the agent for its plan (up to three answers), forces that plan, times both plans with `EXPLAIN ANALYZE` as the mean of three runs after a warm-up, and asks the analyst, a second call to the same model, why PostgreSQL did not follow any decision it missed. It prints one row per query, with its status, the agent's answer count, the four metrics as fractions, whether each plan runs in parallel and the time ratio, then the workload counts: usable agent plans, plans the same as PostgreSQL's, plans forced and parallel plans. This step needs the Anthropic API key. [docs/plan-forcing.md](docs/plan-forcing.md) describes the hints and what they cannot force; [docs/experiments.md](docs/experiments.md) describes the records and the summary.

## Get the AI agent's plan

`preprocessing.get_agent_plan()` asks the agent for its plan for a query and returns it as a `PlanNode` tree, ready for `force_plan()` and `differences()`. To use it from your own code:

```python
import psycopg

from comparison import differences, force_plan
from preprocessing import get_agent_plan, get_postgres_plan
from project import CONNECTION_SETTINGS

original_sql = "SELECT count(*) FROM orders o JOIN customer c ON c.c_custkey = o.o_custkey"

connection = psycopg.connect(**CONNECTION_SETTINGS, password="postgres")
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

### Results on TPC-H Q1–Q22 at scale factor 0.1

One run on 2026-11-11 with the Docker database:

| Measure | Value |
| --- | --- |
| Plans accepted | 22/22 |
| Accepted on the first answer | 20/22 |
| Answers in total | 24 |
| Input tokens per request (cached part) | 3,760 (2,523) |
| Output tokens per request, thinking included | 2,448 |
| Input and output tokens per query | 4,102 and 2,671 |

The agent's calls for Q1–Q22 in that run cost 3 to 4 US cents; a workload run adds H1–H3 and the analyst's calls.

### Limits to know

- PostgreSQL plans most TPC-H queries in parallel and the agent few. The metrics leave `parallel` and `gather` decisions out and report one parallel flag per plan instead; [docs/metrics.md](docs/metrics.md#parallelism-is-reported-separately) says why. [Comparing two plans](docs/plan-forcing.md#comparing-two-plans) says how each kind of decision is counted.
- PostgreSQL cannot follow every agent decision. `force_plan()` reports the ones it did not follow as gaps; [docs/plan-forcing.md](docs/plan-forcing.md) says what hints fix, what they cannot, and how gaps are found.
- The agent can only write plans that hints can force, and the comparison is uneven in several ways. [docs/agent-plan.md](docs/agent-plan.md) lists what the agent cannot express and how fair the comparison is.

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
| `tests/test_metrics.py` | The four comparison metrics, parallelism left out of them, and how `force_plan()` chooses between its tries. | Nothing. |
| `tests/test_experiments.py` | Run storage in JSON Lines, each stage of collecting a run, the per-query summary and the advisory analyst, with fake clients. | Nothing. |
| `tests/test_project.py` | The `--experiments` command line and the timing helper. | The database for two timing tests. |
| `tests/test_loading.py` | `--load-data` on a small two-table schema in a throwaway `load_data_test` database: foreign key load order, NULLs, and a mismatched data folder leaving no tables. | The database, and a user that can create databases. |

Tests that need the database are skipped when it is unavailable. Apart from the two timing tests, which run small queries with `EXPLAIN ANALYZE`, all use `EXPLAIN` only.

## Project files

| File | Purpose |
| --- | --- |
| `data/*.tbl` | Source TPC-H table data. |
| `schema.sql` | Creates the eight database tables. |
| `queries/` | The experiment workload, one query per `.sql` file; the file name is the query name. `Q1`–`Q22` are TPC-H; `H1`–`H3` are handwritten, each with a comment on what it exercises. |
| `project.py` | The entry point. With no arguments it opens the GUI; `--load-data` loads a schema and its data, TPC-H from `schema.sql` and `data/` by default; `--experiments` prompts for the password, runs the workload, stores one JSON record per query, and summarises the metrics. It also holds the record format and the advisory analyst. |
| `interface.py` | The Streamlit GUI: runs or loads one query and draws its three plans with Graphviz. |
| `preprocessing.py` | `PlanNode`, the plan form shared by every plan source. `get_postgres_plan()` runs `EXPLAIN`, optionally with `ANALYZE` or hints. `unique_aliases()` gives each reused table name its own alias. `build_hints()` turns a plan into `pg_hint_plan` hints. `get_agent_plan()` asks the AI agent for its plan, described by `describe_database()` and checked by `validate_agent_plan()`. `load_database()` creates a schema's tables and loads their data, converting `.tbl` files to CSV in `data_clean/` first. |
| `comparison.py` | `decisions()` lists a plan's planner decisions; `differences()` compares two plans; `force_plan()` forces a plan on PostgreSQL and reports, through `realization_gaps()`, the forced decisions it did not follow. |
| `docs/plan-forcing.md` | The hints that force a plan, how a forced plan is checked, how well forcing works, and what cannot be forced. |
| `docs/agent-plan.md` | The agent's plan format, the formats that were tried, and the limits of the format and the comparison. |
| `docs/metrics.md` | The four comparison metrics, why parallelism is reported separately, how to report a workload, and the limits of the comparison. |
| `docs/experiments.md` | The run record, the advisory analyst and the per-query summary. |
| `tests/` | The tests above; `tests/fixtures/` holds a sample query and an agent answer for it. |
| `agent_runs/` | Versioned experiment evidence: one JSON Lines file per experiment, one run record per line, with the plans, the agent's answers, the forcing outcome, the timings and the analyst's advice. |
