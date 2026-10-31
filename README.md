# SC3020-project2

Prepare TPC-H data and inspect PostgreSQL query plans as JSON.

## Requirements

- Python 3 with the Windows `py` launcher.
- A running PostgreSQL server and a database named `tpch`.
- Psycopg 3 for the Python database connection:

  ```powershell
  py -m pip install "psycopg[binary]"
  ```

## Prepare the table data

The repository includes one set of the eight generated TPC-H `.tbl` files
in `data/`: `customer.tbl`,
`lineitem.tbl`, `nation.tbl`, `orders.tbl`, `part.tbl`, `partsupp.tbl`,
`region.tbl`, and `supplier.tbl`.

From the project root, run:

```powershell
py prepare_data.py
```

The script searches the project folder and its subfolders for exactly eight
`.tbl` files. It removes the trailing `|` delimiter from each row and converts
the data into CSV files without headers in `data_clean/`, such as
`data_clean/customer.csv`. It prints the number of rows converted for each file.
Running it again overwrites the corresponding CSV files.

## Create the database tables

Create the `tpch` database if needed, then connect to it in your database's SQL
client. Run `schema.sql` to create the eight TPC-H tables,
then import each CSV from `data_clean/` into its matching table using your
database's CSV import tool. Configure the import for comma-separated UTF-8 data
without a header row.

`py prepare_data.py` prepares the data files; `schema.sql` creates the database
tables.

## Explain a query

Edit `query.sql` and save the SQL query whose plan you want to inspect. The
current example joins `customer` and `orders`, filters orders from
`1995-01-01` onward, and counts orders for each customer.

Keep a single query in the file, without an `EXPLAIN` prefix. For example:

```sql
SELECT *
FROM orders
WHERE o_orderdate >= DATE '1995-01-01';
```

From the project root, run:

```powershell
py project.py
```

Enter your PostgreSQL password when prompted. The connection settings are
currently defined in `project.py`:

| Setting | Value |
| --- | --- |
| Host | `localhost` |
| Port | `5432` |
| Database | `tpch` |
| User | `postgres` |
| Connection timeout | 10 seconds |

Update those settings in `project.py` if your local PostgreSQL setup differs.

`project.py` reads `query.sql` on every run and opens a read-only database
connection. It passes the query to `get_postgres_plan()` in `preprocessing.py`,
which adds `EXPLAIN (FORMAT JSON)` and retrieves the plan. The program prints
`Connected to PostgreSQL!`, followed by the formatted JSON plan tree.

To inspect another query, change `query.sql`, save it, and run `py project.py`
again. This uses `EXPLAIN` without `ANALYZE`, so the output contains the
planner's estimates; it does not execute the query or report actual timings.

## Project files

| File | Purpose |
| --- | --- |
| `data/*.tbl` | Source TPC-H table data. |
| `prepare_data.py` | Converts the source data to CSV files in `data_clean/`. |
| `schema.sql` | Creates the eight database tables. |
| `query.sql` | Query to explain; edit this file to change the input. |
| `project.py` | Prompts for the password, connects to PostgreSQL, and prints the plan. |
| `preprocessing.py` | Runs `EXPLAIN (FORMAT JSON)` and returns the root `Plan` object. |
