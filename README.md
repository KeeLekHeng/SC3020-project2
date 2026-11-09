# SC3020-project2

Compare PostgreSQL's execution plan for a query with a plan proposed by an AI agent. The code so far loads TPC-H data, prints PostgreSQL's plan for a query and forces that plan back onto PostgreSQL with `pg_hint_plan` hints.

## Requirements

- Python 3 with the Windows `py` launcher.
- A running PostgreSQL server and a database named `tpch`. The `Dockerfile` provides PostgreSQL 16 with `pg_hint_plan`:

  ```bash
  docker build -t pg-tpch .
  docker run -d --name tpch -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=tpch \
    -p 127.0.0.1:5432:5432 -v tpch-db:/var/lib/postgresql/data pg-tpch
  ```
- The packages in `requirements.txt`: Psycopg 3, Pydantic and the Anthropic SDK:

  ```powershell
  py -m pip install -r requirements.txt
  ```

  With `uv`: `uv venv`, then `uv pip install -r requirements.txt`.
- For the AI agent, which is not built yet: an Anthropic API key in the `ANTHROPIC_API_KEY` environment variable.

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

`project.py` reads `query.sql` over a read-only connection and prints PostgreSQL's plan as an indented tree. It then forces that plan with `pg_hint_plan` hints, prints the hints and the plan PostgreSQL chose with them, and reports any forced decision PostgreSQL did not follow. If `pg_hint_plan` cannot be loaded, it says so and stops. It uses `EXPLAIN` without `ANALYZE`, so the query does not run.

## Project files

| File | Purpose |
| --- | --- |
| `data/*.tbl` | Source TPC-H table data. |
| `prepare_data.py` | Converts the source data to CSV files in `data_clean/` and loads the database. |
| `schema.sql` | Creates the eight database tables. |
| `query.sql` | Query to explain; edit this file to change the input. |
| `project.py` | Prompts for the password, prints PostgreSQL's plan, then forces the same plan with hints, prints it and reports any decision not followed. |
| `preprocessing.py` | Defines `PlanNode`, the plan form shared by every plan source; `get_postgres_plan()` runs `EXPLAIN` (optionally `ANALYZE` or with hints) and returns a `PlanNode` tree; `build_hints()` turns a plan into `pg_hint_plan` hints. |
| `comparison.py` | `decisions()` lists a plan's planner decisions; `differences()` compares two plans; `force_plan()` forces a plan on PostgreSQL and reports, through `realization_gaps()`, the forced decisions it did not follow. |
