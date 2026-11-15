"""The project.py entry point and its timing helper."""

import psycopg
import pytest

import project
from preprocessing import PlanNode
from tests.test_experiments import evaluable_record


def scan(alias: str, node_type: str = "Seq Scan") -> PlanNode:
    return PlanNode(node_type=node_type, alias=alias, relation=alias)


class FakeConnection:
    read_only = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        pass


def test_execution_time_runs_the_query(connection: psycopg.Connection) -> None:
    ms = project.execution_time(connection, "SELECT count(*) FROM nation")

    assert ms is not None and ms >= 0


def test_execution_time_is_none_past_the_timeout(connection: psycopg.Connection) -> None:
    assert project.execution_time(connection, "SELECT pg_sleep(1)", timeout_ms=10) is None


def test_experiments_flag_runs_every_workload_query(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = evaluable_record("Q1", "q1-1", scan("o"), scan("o", "Index Scan"))
    collected: dict[str, object] = {}

    def collect(*_args: object, **kwargs: object) -> list[object]:
        collected.update(kwargs)
        return [run]

    monkeypatch.setattr(project, "getpass", lambda *_args: "password")
    monkeypatch.setattr(project.psycopg, "connect", lambda **_kwargs: FakeConnection())
    monkeypatch.setattr(project, "api_key", lambda: "test-key")
    monkeypatch.setattr(project, "collect_experiment", collect)

    project.main(["--experiments", "--samples", "2"])

    queries = collected["queries"]
    assert isinstance(queries, dict)
    assert list(queries)[:3] == ["Q1", "Q2", "Q3"] and len(queries) == 22
    assert collected["samples"] == 2
    output = capsys.readouterr().out
    assert "Q1 sample 1: completed" in output
    assert "- proposed_difference: macro 50.0%, micro 50.0% (1/1 runs defined)" in output


def test_queries_flag_runs_only_the_named_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    collected: dict[str, object] = {}

    def collect(*_args: object, **kwargs: object) -> list[object]:
        collected.update(kwargs)
        return []

    monkeypatch.setattr(project, "getpass", lambda *_args: "password")
    monkeypatch.setattr(project.psycopg, "connect", lambda **_kwargs: FakeConnection())
    monkeypatch.setattr(project, "api_key", lambda: "test-key")
    monkeypatch.setattr(project, "collect_experiment", collect)

    project.main(["--experiments", "-q", "Q6", "Q1"])

    queries = collected["queries"]
    assert isinstance(queries, dict) and list(queries) == ["Q6", "Q1"]


@pytest.mark.parametrize("argv", [["-q", "Q99"], ["-s", "0"]])
def test_bad_experiment_arguments_stop_before_connecting(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        project.main(["--experiments", *argv])
