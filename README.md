# SC3020-project2

Prepare TPC-H data and inspect PostgreSQL query plans as JSON.

## Requirements

- Python 3 with the Windows `py` launcher.
- A running PostgreSQL server and a database named `tpch`. The `Dockerfile`
  provides PostgreSQL 16 with `pg_hint_plan`:

  ```bash
  docker build -t pg-tpch .
  docker run -d --name tpch -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=tpch \
    -p 127.0.0.1:5432:5432 -v tpch-db:/var/lib/postgresql/data pg-tpch
  ```
- The packages in `requirements.txt` (Psycopg 3 and the Anthropic SDK):

  ```powershell
  py -m pip install -r requirements.txt
  ```

  With `uv`: `uv venv`, then `uv pip install -r requirements.txt`.
- An Anthropic API key in the `ANTHROPIC_API_KEY` environment variable.

## Load the database

`data/` holds the eight generated TPC-H `.tbl` files. Create an empty `tpch`
database, then from the project root run:

```powershell
py prepare_data.py
```

Enter your PostgreSQL password when prompted. The script requires exactly eight
`.tbl` files in the project folder and its subfolders. It strips each row's
trailing `|` and writes headerless CSV files to `data_clean/`, overwriting old
ones. It then runs `schema.sql`, loads the tables in foreign key order and runs
`ANALYZE`, using the connection settings in `project.py`.

The load is one transaction, so a failure leaves no tables behind. If the tables
already exist, the script stops; drop them or recreate the database first.

## Explain a query

Save one SQL query in `query.sql`, without an `EXPLAIN` prefix. The current one
counts each customer's orders from `1995-01-01` onward. For example:

```sql
SELECT *
FROM orders
WHERE o_orderdate >= DATE '1995-01-01';
```

From the project root, run:

```powershell
py project.py
```

Enter your PostgreSQL password when prompted. Change these settings in
`project.py` if your setup differs:

| Setting | Value |
| --- | --- |
| Host | `localhost` |
| Port | `5432` |
| Database | `tpch` |
| User | `postgres` |
| Connection timeout | 10 seconds |

`project.py` reads `query.sql` on each run over a read-only connection, then
prints `Connected to PostgreSQL!` and the formatted JSON plan tree. It uses
`EXPLAIN` without `ANALYZE`, so the plan shows estimates only; the query does
not run.

## Project files

| File | Purpose |
| --- | --- |
| `data/*.tbl` | Source TPC-H table data. |
| `prepare_data.py` | Converts the source data to CSV files in `data_clean/` and loads the database. |
| `schema.sql` | Creates the eight database tables. |
| `query.sql` | Query to explain; edit this file to change the input. |
| `project.py` | Prompts for the password, connects to PostgreSQL, and prints the plan. |
| `preprocessing.py` | `get_postgres_plan()` runs `EXPLAIN (FORMAT JSON)` and returns the root `Plan` object. |
