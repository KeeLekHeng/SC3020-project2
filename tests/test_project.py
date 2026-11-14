"""Command-line consumption of structured comparison results."""

from types import SimpleNamespace

import psycopg
import pytest

import project
from comparison import ForcePlanResult, ForcingFailure, realization_gaps
from preprocessing import PlanNode


def scan(alias: str, node_type: str = "Seq Scan") -> PlanNode:
    return PlanNode(node_type=node_type, alias=alias, relation=alias)


def outcome(plan: PlanNode, requested: PlanNode | None = None) -> ForcePlanResult:
    requested = requested or plan
    return ForcePlanResult(
        hints="SeqScan(o)",
        plan=plan,
        gaps=tuple(realization_gaps(requested, plan)),
        switch_offs=True,
        leading=True,
    )


class FakeConnection:
    read_only = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_show_forced_returns_all_selected_attempt_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = outcome(scan("o"))
    monkeypatch.setattr(project, "force_plan", lambda *_args: result)

    shown = project.show_forced(FakeConnection(), "SELECT 1", scan("o"))  # type: ignore[arg-type]

    assert shown is result
    assert isinstance(shown, ForcePlanResult)
    assert "SeqScan(o)" in capsys.readouterr().out


def test_show_forced_preserves_failure_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_args: object) -> ForcePlanResult:
        raise ValueError("Cannot force this plan")

    monkeypatch.setattr(project, "force_plan", fail)

    shown = project.show_forced(FakeConnection(), "SELECT 1", scan("o"))  # type: ignore[arg-type]

    assert shown == ForcingFailure("Cannot force this plan")
    assert "Cannot force this plan" in capsys.readouterr().out


@pytest.mark.parametrize("forcing_fails", [False, True])
def test_main_prints_complete_or_partial_structured_metrics(
    forcing_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    postgres = scan("o")
    agent = scan("o", "Index Scan")
    round_trip = outcome(postgres)
    agent_outcome = (
        ForcingFailure("pg_hint_plan unavailable")
        if forcing_fails
        else outcome(postgres, requested=agent)
    )
    shown = iter([round_trip, agent_outcome])

    monkeypatch.setattr(project, "getpass", lambda *_args: "password")
    monkeypatch.setattr(project.psycopg, "connect", lambda **_kwargs: FakeConnection())
    monkeypatch.setattr(project, "get_postgres_plan", lambda *_args, **_kwargs: postgres)
    monkeypatch.setattr(project, "api_key", lambda: "test-key")
    monkeypatch.setattr(
        project,
        "get_agent_plan",
        lambda *_args, **_kwargs: SimpleNamespace(
            plan=agent,
            query="SELECT * FROM o",
            problems=[],
            summary="Use the index.",
        ),
    )
    monkeypatch.setattr(project, "show_forced", lambda *_args, **_kwargs: next(shown))
    times = iter([100.0, 250.0])
    monkeypatch.setattr(project, "execution_time", lambda *_args: next(times))

    project.main()

    output = capsys.readouterr().out
    assert "Proposed-plan difference: 1/2 decisions (50.0%)" in output
    assert "scan(o): PostgreSQL Seq Scan, agent proposed Index Scan" in output
    if forcing_fails:
        assert "Plan realizability: N/A [unavailable]: pg_hint_plan unavailable" in output
        assert "Realized-plan difference: N/A [unavailable]" in output
    else:
        assert "Plan realizability: 1/2 force-eligible decisions (50.0%)" in output
        assert "Realized-plan difference: 0/2 decisions (0.0%)" in output
        assert "PostgreSQL plan 100.0 ms, agent plan 250.0 ms" in output
        assert "2.50x" in output


def test_execution_time_runs_the_query(connection: psycopg.Connection) -> None:
    ms = project.execution_time(connection, "SELECT count(*) FROM nation")

    assert ms is not None and ms >= 0


def test_execution_time_is_none_past_the_timeout(connection: psycopg.Connection) -> None:
    assert project.execution_time(connection, "SELECT pg_sleep(1)", timeout_ms=10) is None
