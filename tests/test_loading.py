"""python project.py --load-data, run against a throwaway database."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

import project
from preprocessing import TABLES_QUERY
from project import CONNECTION_SETTINGS

DATABASE = "load_data_test"


@pytest.fixture
def database() -> Iterator[dict[str, Any]]:
    """An empty database, dropped afterwards. Needs PGPASSWORD and the right to create one."""
    try:
        admin = psycopg.connect(**CONNECTION_SETTINGS, autocommit=True)
    except psycopg.OperationalError as error:
        pytest.skip(f"PostgreSQL is not reachable: {error}")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DATABASE}")
        admin.execute(f"CREATE DATABASE {DATABASE}")
        settings: dict[str, Any] = CONNECTION_SETTINGS | {"dbname": DATABASE}
        yield settings
        admin.execute(f"DROP DATABASE {DATABASE} WITH (FORCE)")


def write_schema(folder: Path) -> tuple[Path, Path]:
    # a_book sorts before b_author but references it, so the load order must follow the key.
    schema = folder / "library.sql"
    schema.write_text(
        "CREATE TABLE b_author (id int PRIMARY KEY, name text);"
        "CREATE TABLE a_book (id int PRIMARY KEY, author int REFERENCES b_author, title text);"
    )
    data = folder / "data"
    data.mkdir()
    (data / "b_author.csv").write_text('1,"Lee, Ann"\n2,\n')
    (data / "a_book.tbl").write_text("10|1|Plans|\n11|2||\n")
    return schema, data


def test_load_data_creates_the_tables_and_loads_them_in_key_order(
    database: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema, data = write_schema(tmp_path)
    monkeypatch.setattr(project, "PROJECT_FOLDER", tmp_path)

    project.main(["--load-data", "--schema", str(schema), "--data", str(data), "--database", DATABASE])

    with psycopg.connect(**database) as connection:
        rows = connection.execute(
            "SELECT b.id, a.name, b.title FROM a_book b JOIN b_author a ON a.id = b.author ORDER BY b.id"
        ).fetchall()
    # An empty field loads as NULL; a quoted comma stays in its field.
    assert rows == [(10, "Lee, Ann", "Plans"), (11, None, None)]


def test_load_data_stops_and_leaves_no_tables_when_files_and_tables_differ(
    database: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema, data = write_schema(tmp_path)
    (data / "c_extra.csv").write_text("1\n")
    monkeypatch.setattr(project, "PROJECT_FOLDER", tmp_path)

    with pytest.raises(SystemExit, match="Data files with no table: c_extra.csv"):
        project.main(["--load-data", "--schema", str(schema), "--data", str(data), "--database", DATABASE])

    with psycopg.connect(**database) as connection:
        assert connection.execute(TABLES_QUERY).fetchall() == []
