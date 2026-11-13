"""Command-line consumption of structured comparison results."""

from types import SimpleNamespace

import pytest

import project
from comparison import Decision, ForcePlanResult, ForcingFailure, realization_gaps
from preprocessing import PlanNode


def scan(alias: str, node_type: str = "Seq Scan") -> PlanNode:
    return PlanNode(node_type=node_type, alias=alias, relation=alias)


def outcome(plan: PlanNode, requested: PlanNode | None = None) -> ForcePlanResult:
    requested = requested or plan
    return ForcePlanResult(
        hints="SeqScan(o)",
        plan=plan,
        gaps=tuple(realization_gaps(requested, plan)),
        targets=frozenset({Decision("scan", ("o",))}),
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
    monkeypatch.setattr(project, "force_plan_detailed", lambda *_args: result)

    shown = project.show_forced(FakeConnection(), "SELECT 1", scan("o"))  # type: ignore[arg-type]

    assert shown is result
    assert isinstance(shown, ForcePlanResult)
    assert shown.targets == frozenset({Decision("scan", ("o",))})
    assert "SeqScan(o)" in capsys.readouterr().out


def test_show_forced_preserves_failure_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_args: object) -> ForcePlanResult:
        raise ValueError("Cannot force this plan")

    monkeypatch.setattr(project, "force_plan_detailed", fail)

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
