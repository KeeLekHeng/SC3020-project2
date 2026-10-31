# SC3020-project2

## Prepare the table data

Install Python 3, then keep one set of the eight generated TPC-H `.tbl` files
inside the project folder (for example, in `data/`): `customer.tbl`,
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

Run `schema.sql` in your database's SQL client to create the eight TPC-H tables,
then import each CSV from `data_clean/` into its matching table using your
database's CSV import tool. Configure the import for comma-separated UTF-8 data
without a header row.

`py prepare_data.py` prepares the data files; `schema.sql` creates the database
tables.
