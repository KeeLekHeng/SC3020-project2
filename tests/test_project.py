"""The project.py entry point and its timing helper."""

import psycopg
import pytest

import project
from preprocessing import PlanNode
from tests.test_experiments import record


def scan(alias: str, node_type: str = "Seq Scan") -> PlanNode:
    return PlanNode(node_type=node_type, alias=alias, relation=alias)


class FakeConnection:
    read_only = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        pass


class TimedConnection:
    def __init__(self) -> None:
        self.statements: list[bytes] = []

    def execute(self, statement: bytes) -> None:
        self.statements.append(statement)

    def rollback(self) -> None:
        self.statements.append(b"ROLLBACK")


def test_execution_time_is_the_mean_after_a_warm_up(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([999.0, 10.0, 20.0, 30.0])
    monkeypatch.setattr(
        project,
        "get_postgres_plan",
        lambda *_args, **_kwargs: PlanNode(node_type="Result", execution_time_ms=next(times)),
    )
    connection = TimedConnection()

    assert project.execution_time(connection, "SELECT 1") == 20.0  # type: ignore[arg-type]
    assert connection.statements[-1] == b"RESET statement_timeout"


def test_execution_time_without_a_reported_time_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        project, "get_postgres_plan", lambda *_args, **_kwargs: PlanNode(node_type="Result")
    )

    with pytest.raises(RuntimeError, match="no execution time"):
        project.execution_time(TimedConnection(), "SELECT 1")  # type: ignore[arg-type]


def test_execution_time_is_none_when_a_later_run_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = iter([False, False, True])

    def plan(*_args: object, **_kwargs: object) -> PlanNode:
        if next(runs):
            raise psycopg.errors.QueryCanceled()
        return PlanNode(node_type="Result", execution_time_ms=10.0)

    monkeypatch.setattr(project, "get_postgres_plan", plan)
    connection = TimedConnection()

    assert project.execution_time(connection, "SELECT 1") is None  # type: ignore[arg-type]
    assert b"ROLLBACK" in connection.statements


def test_execution_time_runs_the_query(connection: psycopg.Connection) -> None:
    ms = project.execution_time(connection, "SELECT count(*) FROM nation")

    assert ms is not None and ms >= 0


def test_execution_time_is_none_past_the_timeout(connection: psycopg.Connection) -> None:
    assert project.execution_time(connection, "SELECT pg_sleep(1)", timeout_ms=10) is None


def run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[str]:
    """Run main() with a fake database, key and pipeline; return the queries it ran."""
    ran: list[str] = []

    def collect_run(*_args: object, query_name: str, **_kwargs: object) -> object:
        ran.append(query_name)
        return record(query_name)

    monkeypatch.setattr(project, "getpass", lambda *_args: "password")
    monkeypatch.setattr(project.psycopg, "connect", lambda **_kwargs: FakeConnection())
    monkeypatch.setattr(project, "api_key", lambda: "test-key")
    monkeypatch.setattr(project, "collect_run", collect_run)
    project.main(argv)
    return ran


def test_experiments_flag_runs_every_workload_query(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = run_main(monkeypatch, ["--experiments"])

    assert ran[:3] == ["Q1", "Q2", "Q3"] and len(ran) == 22
    lines = capsys.readouterr().out.splitlines()
    # The stored run: an Index Scan proposed, a Seq Scan realized, one answer.
    q1 = next(line for line in lines if line.startswith("Q1 "))
    assert q1.split() == ["Q1", "completed", "1", "1/1", "0/2", "0/1", "0/2", "no/no/no", "-"]
    assert "Usable agent plans: 22/22, 22 on the first answer" in lines
    assert "Same plan as PostgreSQL, apart from parallelism: proposed 0/22, realized 22/22" in lines
    assert "Plans forced: 22/22" in lines


def test_queries_flag_runs_only_the_named_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_main(monkeypatch, ["--experiments", "-q", "Q6", "Q1"]) == ["Q6", "Q1"]


def test_unknown_query_stops_before_connecting() -> None:
    with pytest.raises(SystemExit):
        project.main(["--experiments", "-q", "Q99"])
