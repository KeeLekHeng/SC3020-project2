def get_postgres_plan(connection, query):
    """Retrieve a PostgreSQL execution plan without running the query."""
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (FORMAT JSON) " + query)
        result = cursor.fetchone()[0]

    return result[0]["Plan"]
