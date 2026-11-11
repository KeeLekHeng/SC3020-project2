# SC3020-project2

Compare PostgreSQL's execution plan for a query with a plan proposed by an AI agent, Claude Haiku 5.5. For one query, `project.py` prints PostgreSQL's plan, asks the agent for its own plan, forces that plan onto PostgreSQL with `pg_hint_plan` hints, and lists the decisions the two plans made differently. The data is TPC-H. The GUI and the difference rate are not built yet.

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

## Run the pipeline on a query

Save one SQL query in `query.sql`, without an `EXPLAIN` prefix. The current one counts each customer's orders from `1995-01-01` onward. From the project root, run:

```powershell
py project.py
```

Enter your PostgreSQL password when prompted. Change these settings in `project.py` if your setup differs:

| Setting | Value |
| --- | --- |
| Host | `localhost` |
| Port | `5432` |
| Database | `tpch` |
| User | `postgres` |
| Connection timeout | 10 seconds |

`project.py` reads `query.sql` over a read-only connection and prints PostgreSQL's plan as an indented tree. Hints cannot tell two uses of one table name apart, so if the query reads a name more than once, it first gives each later use its own alias, such as `lineitem2`, and prints the rewritten query. It then runs three steps:

1. **Round-trip check.** It forces PostgreSQL's own plan with `pg_hint_plan` hints, prints the hints and the plan PostgreSQL chose with them, and reports any forced decision PostgreSQL did not follow.
2. **The agent's plan.** It asks the AI agent for its plan, prints the plan and the agent's strategy, and forces that plan the same way. This step needs the Anthropic API key and costs a fraction of a US cent per query.
3. **The differences.** It lists every decision that PostgreSQL's plan and the agent's forced plan made differently, as `differences()` reports them.

If `pg_hint_plan` cannot be loaded, no API key is set, or the agent gives no usable plan, it says so and stops there. It uses `EXPLAIN` without `ANALYZE`, so the query does not run.

## Get the AI agent's plan

`preprocessing.get_agent_plan()` asks the agent for its plan for a query and returns it as a `PlanNode` tree, ready for `force_plan()` and `differences()`. To use it from your own code:

```python
from comparison import differences, force_plan
from preprocessing import get_agent_plan, get_postgres_plan

result = get_agent_plan(connection, original_sql)
postgres = get_postgres_plan(connection, result.query)
hints, hinted, gaps = force_plan(connection, result.query, result.plan)
diffs = differences(postgres, hinted)
```

Plan and force with `result.query`, the query after `unique_aliases()`, because the agent's aliases refer to it. `result` is an `AgentResult`:

| Field | Meaning |
| --- | --- |
| `plan` | The agent's plan as a `PlanNode` tree. Each node's `reason` holds the agent's one-line reason for it. |
| `summary` | One or two sentences on the agent's overall strategy. |
| `query` | The query the agent planned. |
| `answer` | The agent's raw JSON answer. |
| `problems` | The errors sent back for each rejected answer, so `len(problems) + 1` answers were needed. |

It raises `AgentPlanError`, with `.errors` and the raw `.answer`, when no answer is usable, and `RuntimeError` when the model declines or its answer is cut off. Errors from the Anthropic SDK, such as a missing API key or a rate limit, pass through. Answers vary from call to call, so save `result.answer` when a run must be repeatable.

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

- PostgreSQL plans 14 of the 22 queries in parallel; the agent chose parallel plans for 2 to 7, depending on the run. `parallel` and `gather` differences will dominate, so consider reporting the difference rate with and without them.
- PostgreSQL cannot follow every agent decision. `force_plan()` reports the ones it did not follow as gaps; count only followed decisions as the agent's. [What forcing cannot guarantee](docs/agent-plan.md#what-forcing-cannot-guarantee) says what hints control and where gaps come from.
- The agent can only write plans that hints can force, and the comparison is uneven in several ways. [docs/agent-plan.md](docs/agent-plan.md) lists what the agent cannot express and how fair the comparison is.
- When `get_agent_plan()` raises, show it as a failed agent plan, as `project.py` does. The retry loop can run out of answers.

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
| `tests/test_forcing.py` | Forcing PostgreSQL's own plan for TPC-H Q1–Q22, for query shapes that broke forcing before and for a few edited plans, such as swapped join sides; every forced decision must be followed. | The database with `pg_hint_plan`. |
| `tests/test_agent.py` | The agent's answer validation, the prompt, the database description and the retry loop with a fake client. | No API key; the database for the description tests. |

Tests that need the database are skipped when it is unavailable. All use `EXPLAIN` only, so no query runs.

## Project files

| File | Purpose |
| --- | --- |
| `data/*.tbl` | Source TPC-H table data. |
| `prepare_data.py` | Converts the source data to CSV files in `data_clean/` and loads the database. |
| `schema.sql` | Creates the eight database tables. |
| `query.sql` | The query the pipeline runs on; edit this file to change the input. |
| `project.py` | Prompts for the password and runs the pipeline on `query.sql`: PostgreSQL's plan, the same plan forced back as a check, the agent's plan, that plan forced, and the decisions the two made differently. |
| `preprocessing.py` | `PlanNode`, the plan form shared by every plan source. `get_postgres_plan()` runs `EXPLAIN`, optionally with `ANALYZE` or hints. `unique_aliases()` gives each reused table name its own alias. `build_hints()` turns a plan into `pg_hint_plan` hints. `get_agent_plan()` asks the AI agent for its plan, described by `describe_database()` and checked by `validate_agent_plan()`. |
| `comparison.py` | `decisions()` lists a plan's planner decisions; `differences()` compares two plans; `force_plan()` forces a plan on PostgreSQL and reports, through `realization_gaps()`, the forced decisions it did not follow. |
| `docs/agent-plan.md` | The agent's plan format, the formats that were tried, and the limits of the format and the comparison. |
| `tests/` | The tests above; `tests/fixtures/` holds a sample agent answer. |
| `agent_runs/` | Not in git: raw agent answers and rendered prompts from local runs. |
