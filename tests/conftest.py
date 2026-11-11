"""A read-only connection to the tpch database, for the tests that need PostgreSQL."""

from collections.abc import Iterator

import psycopg
import pytest

from project import CONNECTION_SETTINGS


@pytest.fixture(scope="session")
def connection() -> Iterator[psycopg.Connection]:
    """
    Connect with project.CONNECTION_SETTINGS and the password in PGPASSWORD.

    Tests that use this are skipped when the database or pg_hint_plan is unavailable.
    """
    try:
        connection = psycopg.connect(**CONNECTION_SETTINGS, autocommit=True)
    except psycopg.OperationalError as error:
        pytest.skip(f"PostgreSQL is not reachable: {error}")

    with connection:
        connection.execute("SET default_transaction_read_only = on")
        try:
            connection.execute("LOAD 'pg_hint_plan'")
        except psycopg.Error as error:
            pytest.skip(f"pg_hint_plan cannot be loaded: {error}")

        yield connection
