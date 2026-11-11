# SC3020-project2

Compare PostgreSQL's execution plan for a query with a plan proposed by an AI agent. The code so far loads TPC-H data, prints PostgreSQL's plan for a query, forces a plan onto PostgreSQL with `pg_hint_plan` hints, and asks an AI agent (Claude Haiku 5.5) for its own plan as a `PlanNode` tree that can be forced the same way. The GUI and the difference rate are not built yet.

## Requirements

- Python 3 with the Windows `py` launcher.
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
- For the AI agent: an Anthropic API key, either in the `ANTHROPIC_API_KEY` environment variable or as a line `ANTHROPIC_API_KEY=...` in a `.env` file in the project root (`.env` is gitignored). The agent uses Claude Haiku 5.5.

## Load the database

`data/` holds the eight generated TPC-H `.tbl` files. Create an empty `tpch` database (the Docker command above already does), then from the project root run:

```powershell
py prepare_data.py
```

Enter your PostgreSQL password when prompted. The script requires exactly eight `.tbl` files in the project folder and its subfolders. It strips each row's trailing `|` and writes headerless CSV files to `data_clean/`, overwriting old ones. It then runs `schema.sql`, loads the tables in foreign key order and runs `ANALYZE`, using the connection settings in `project.py`.

The load is one transaction, so a failure leaves no tables behind. If the tables already exist, the script stops; drop them or recreate the database first.

## Explain and force a query

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

`project.py` reads `query.sql` over a read-only connection and prints PostgreSQL's plan as an indented tree. If the query reads one table name more than once, it first gives each later use its own alias, such as `lineitem2`, and prints the rewritten query, because hints cannot tell two uses of one name apart. It then forces that plan with `pg_hint_plan` hints, prints the hints and the plan PostgreSQL chose with them, and reports any forced decision PostgreSQL did not follow. If `pg_hint_plan` cannot be loaded, it says so and stops. It uses `EXPLAIN` without `ANALYZE`, so the query does not run.

## Get the AI agent's plan

`preprocessing.get_agent_plan()` asks the agent for its plan for a query and returns it as a `PlanNode` tree, ready for `force_plan()` and `differences()`. `project.py` does not call it yet. From code:

```python
from comparison import differences, force_plan
from preprocessing import get_agent_plan, get_postgres_plan, unique_aliases

query = unique_aliases(original_sql)
postgres = get_postgres_plan(connection, query)
result = get_agent_plan(connection, query, postgres_plan=postgres)
hints, hinted, gaps = force_plan(connection, result.query, result.plan)
diffs = differences(postgres, hinted)
```

Always plan and force with `result.query`, the query after `unique_aliases()`, because the agent's aliases refer to it. `result` is an `AgentResult`:

| Field | Meaning |
| --- | --- |
| `plan` | The agent's plan as a `PlanNode` tree. Each node's `reason` holds the agent's one-line reason for it. |
| `summary` | One or two sentences on the agent's overall strategy. |
| `query` | The query the agent planned. |
| `answer` | The agent's raw JSON answer. |
| `problems` | The errors sent back for each rejected answer, so `len(problems) + 1` answers were needed. |

How it works:

1. `describe_database()` reads from the catalogs only the tables and columns the query uses: row and page counts, distinct counts, the range and quartiles of each column the query compares with `<`, `>` or `BETWEEN`, common values when they are skewed, strong correlation with the table's order, indexes, and foreign keys between those tables. It also gives `max_parallel_workers_per_gather` and `work_mem`. No schema is hardcoded, and the agent only gets raw statistics, never PostgreSQL's estimates or plan.
2. The system prompt gives the task, the answer's JSON Schema (from the pydantic model `AgentAnswer`), the plan conventions and a worked example. The answer is a flat list: one entry per operator, the root first, each with an `id` and the `id` of its `parent`. The agent writes this more reliably than a nested tree, where it lost count of the closing brackets. The prompt is the same for every query, so it is cached. The user message gives the database description, the aliases the plan must use and the query. The rendered prompt is about 6.5K characters of system prompt plus 1–3K of user message.
3. Each answer is validated by `validate_agent_plan()`, which names operators by `id` in its errors (`node 3: a Seq Scan needs alias.`), then checked against the database: the plan must scan the same aliases as PostgreSQL's plan, every index must exist, and `build_hints()` must accept it. All problems go back to the agent in one message, at `low` effort first and `medium` after, up to three answers.

It raises `AgentPlanError` (with `.errors` and the raw `.answer`) when no answer is usable, and `RuntimeError` when the model declines or its answer is cut off. Without an API key, the Anthropic SDK raises an authentication error on the first request. Answers vary from call to call, so save `result.answer` when a run must be repeatable.

### Results on TPC-H Q1–Q22

Measured with the Docker database, one run per prompt version. The first two, on 2026-11-10, asked for the plan as a nested tree; the current one, on 2026-11-11, asks for the flat list:

| | First prompt | Nested tree | Current prompt |
| --- | --- | --- | --- |
| Plans accepted | 22/22 | 22/22 | 22/22 |
| Accepted on the first answer | 9/22 | 19/22 | 21/22 |
| Answers in total | 37 | 25 | 23 |
| Input tokens per request (cached part) | 7,775 (6,386) | 3,421 (2,276) | 3,398 (2,541) |
| Output tokens per request, thinking included | 1,820 | 1,996 | 2,351 |
| Input and output tokens per query | 13,076 and 3,061 | 3,888 and 2,269 | 3,553 and 2,458 |

A run over all 22 queries costs about 3 US cents. Listing the query's aliases in the prompt ended the agent's alias mistakes, which were most rejections in the first run. Across six runs, the retry loop fixed every rejected answer except in one run, with an earlier prompt, where two queries ran out of answers.

### Notes for the comparison and GUI work

- PostgreSQL plans 14 of the 22 queries in parallel; the agent chose parallel plans for 7 in the last run and 2 in an earlier one. `parallel` and `gather` differences will dominate, so consider reporting the difference rate with and without them. The hints force which scans run in parallel and with how many workers, but not where the Gather sits.
- After forcing, PostgreSQL followed every agent decision except in Q16 (`NOT IN` planned as a join, which PostgreSQL cannot do because of NULLs), Q18 (the outer side of one join swapped) and, in one run, Q20 (a correlated subquery planned as a join). `force_plan()` reports these as gaps; count only followed decisions as the agent's.
- The retry loop checks that a plan is well formed and can be forced, not that it is good. Gaps are not sent back to the agent, since they are its own decisions.
- The agent sees row counts, distinct counts, ranges and skewed common values, but not PostgreSQL's full histograms, so it estimates with less detail than the planner.
- When `get_agent_plan()` raises, show it as a failed agent plan; one run with an earlier prompt failed on 2 queries.

## Run the tests

Install the test dependencies, set your PostgreSQL password, then run the tests from the project root:

```powershell
py -m pip install -r requirements-dev.txt
$env:PGPASSWORD = "postgres"
py -m pytest
```

`tests/test_hints.py` needs no database. It covers hint building, the alias rewrite and plan comparison. `tests/test_forcing.py` forces PostgreSQL's own plan for TPC-H Q1–Q22 and for query shapes that broke forcing before, plus a few edited plans such as swapped join sides, and checks that every forced decision is followed. It is skipped when the database or `pg_hint_plan` is unavailable. `tests/test_agent.py` covers the agent's answer validation, the prompt, the database description and the retry loop with a fake client, so it needs no API key; its database tests are skipped like the forcing tests. All use `EXPLAIN` only, so no query runs.

## Project files

| File | Purpose |
| --- | --- |
| `data/*.tbl` | Source TPC-H table data. |
| `prepare_data.py` | Converts the source data to CSV files in `data_clean/` and loads the database. |
| `schema.sql` | Creates the eight database tables. |
| `query.sql` | Query to explain; edit this file to change the input. |
| `project.py` | Prompts for the password, prints PostgreSQL's plan, then forces the same plan with hints, prints it and reports any decision not followed. |
| `preprocessing.py` | Defines `PlanNode`, the plan form shared by every plan source; `get_postgres_plan()` runs `EXPLAIN` (optionally `ANALYZE` or with hints) and returns a `PlanNode` tree; `unique_aliases()` gives each reused table name its own alias; `build_hints()` turns a plan into `pg_hint_plan` hints; `get_agent_plan()` asks the AI agent for its plan, which `AgentPlanNode` and `validate_agent_plan()` check, using the database facts from `describe_database()`. |
| `comparison.py` | `decisions()` lists a plan's planner decisions; `differences()` compares two plans; `force_plan()` forces a plan on PostgreSQL and reports, through `realization_gaps()`, the forced decisions it did not follow. |
| `tests/` | Tests for hint building and comparison, for forcing plans on PostgreSQL, and for the AI agent; `tests/fixtures/` holds a sample PostgreSQL plan and agent answer. |
| `agent_runs/` | Not in git: raw agent answers and rendered prompts from local runs. |
