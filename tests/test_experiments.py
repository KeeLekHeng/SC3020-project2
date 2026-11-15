"""Stored experiment runs, their summary, and the advisory analyst."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import psycopg
import pytest
from pydantic import ValidationError

import project
from comparison import ForcePlanResult, MetricStatus, realization_gaps
from preprocessing import (
    AgentAttempt,
    AgentError,
    AgentPlanError,
    AgentResult,
    DatabaseDescription,
    PlanNode,
)
from project import (
    AnalystAssessment,
    AnalystResponse,
    ForcingEvidence,
    ParallelFlags,
    RunEvidence,
    RunIdentity,
    RunProvenance,
    RunRecord,
    TimingEvidence,
    build_analyst_request,
    collect_run,
    evaluation_from_run,
    load_experiment,
    run_analyst,
    summarise_workload,
    write_run,
)

SEQ = PlanNode(node_type="Seq Scan", relation="orders", alias="orders")
INDEX = PlanNode(
    node_type="Index Scan", relation="orders", alias="orders", index_name="orders_pkey"
)
PARALLEL = PlanNode(
    node_type="Gather",
    children=(
        PlanNode(node_type="Seq Scan", relation="orders", alias="orders", parallel_aware=True),
    ),
)


def record(
    query: str = "Q1",
    *,
    postgres: PlanNode | None = SEQ,
    agent: PlanNode | None = INDEX,
    realized: PlanNode | None = SEQ,
    answers: int = 1,
    timing: TimingEvidence | None = None,
    failure_stage: str | None = None,
) -> RunRecord:
    """A stored run; realized=None with an agent plan means forcing failed."""
    forcing = None
    if agent is not None:
        forcing = (
            ForcingEvidence(failure_reason="cannot force")
            if realized is None
            else ForcingEvidence(
                realized_plan=realized, hints="hints", switch_offs=True, leading=True
            )
        )
    return RunRecord(
        identity=RunIdentity(
            experiment_id="experiment",
            query_name=query,
            created_at=datetime(2026, 11, 15, tzinfo=UTC),
        ),
        provenance=RunProvenance(
            git_commit="abc123", agent_model="claude-haiku-5-5", prompt_sha256="0" * 64
        ),
        evidence=RunEvidence(
            original_sql="SELECT * FROM orders",
            rewritten_sql="SELECT * FROM orders",
            database_description="PostgreSQL 16\norders",
            postgres_plan=postgres,
            agent_plan=agent,
            agent_attempts=tuple(AgentAttempt(response="{}") for _ in range(answers)),
            forcing=forcing,
            timing=timing,
        ),
        failure_stage=failure_stage,
        failure_reason=failure_stage,
    )


class FakeAnalystClient:
    """Stands in for anthropic.Anthropic: answers every request with the given text."""

    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.messages = self
        self.text = text
        self.stop_reason = stop_reason
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> SimpleNamespace:
        self.requests.append(request)
        return SimpleNamespace(
            content=[anthropic.types.TextBlock(type="text", text=self.text)],
            stop_reason=self.stop_reason,
        )


def advice_for(run: RunRecord) -> FakeAnalystClient:
    """An analyst that gives one valid assessment for each of the run's gaps."""
    request = build_analyst_request(run)
    assert request is not None
    advice = tuple(
        AnalystAssessment(
            gap_id=gap_id,
            cause="forcing_limitation",
            explanation="The forced plan did not follow this request.",
            evidence=("A and R differ.",),
            confidence="medium",
        )
        for gap_id in request.gap_ids
    )
    return FakeAnalystClient(AnalystResponse(assessments=advice).model_dump_json())


# Stored runs


def test_runs_are_appended_to_their_experiment_file(tmp_path: Path) -> None:
    runs = [record("Q1"), record("Q2", agent=None, failure_stage="agent_plan")]

    paths = {write_run(run, tmp_path) for run in runs}

    assert paths == {tmp_path / "experiment.jsonl"}
    assert load_experiment("experiment", tmp_path) == runs


def test_a_run_cut_off_mid_write_is_left_out(tmp_path: Path) -> None:
    path = write_run(record("Q1"), tmp_path)
    with path.open("a", encoding="utf-8") as experiment:
        experiment.write(record("Q2").model_dump_json()[:40])

    assert load_experiment("experiment", tmp_path) == [record("Q1")]


def test_a_broken_line_before_the_last_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "experiment.jsonl"
    path.write_text("not json\n" + record().model_dump_json() + "\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_experiment("experiment", tmp_path)


def test_evaluation_is_recomputed_from_the_stored_plans() -> None:
    evaluation = evaluation_from_run(record(agent=INDEX, realized=SEQ))

    assert evaluation is not None
    # The Index Scan and its index were requested; neither was kept.
    assert [str(gap.decision) for gap in evaluation.realizability.gaps] == [
        "index(orders)", "scan(orders)"
    ]


def test_evaluation_keeps_the_proposal_when_forcing_failed() -> None:
    evaluation = evaluation_from_run(record(realized=None, failure_stage="forcing"))

    assert evaluation is not None
    assert evaluation.proposed.difference.rate == 1.0
    assert evaluation.realized.difference.status is MetricStatus.UNAVAILABLE
    assert evaluation.realized.difference.explanation == "cannot force"


def test_a_run_that_stopped_before_forcing_has_no_evaluation() -> None:
    assert evaluation_from_run(record(agent=None, failure_stage="agent_plan")) is None


# Collecting a run


class FakeConnection:
    def __init__(self) -> None:
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1


ANSWERS = (
    AgentAttempt(response="bad", validation_problems=("invalid",)),
    AgentAttempt(response="good"),
)


def arrange(
    monkeypatch: pytest.MonkeyPatch, times: tuple[float | None, ...] = (100.0, 125.0)
) -> None:
    """Fake every database and model call collect_run makes, all succeeding."""
    database = DatabaseDescription("PostgreSQL 16\norders", {"orders": {"orders_pkey"}})
    monkeypatch.setattr(project, "describe_database", lambda *_args: database)
    monkeypatch.setattr(project, "get_postgres_plan", lambda *_args, **_kwargs: SEQ)
    monkeypatch.setattr(
        project, "ask_agent", lambda *_args: AgentResult(INDEX, "Use the index.", "", ANSWERS)
    )
    selected = ForcePlanResult(
        hints="IndexScan(orders orders_pkey)",
        plan=SEQ,
        gaps=tuple(realization_gaps(INDEX, SEQ)),
        switch_offs=False,
        leading=True,
    )
    monkeypatch.setattr("comparison.force_plan", lambda *_args: selected)
    remaining = iter(times)
    monkeypatch.setattr(project, "execution_time", lambda *_args, **_kwargs: next(remaining))


def collect(
    tmp_path: Path,
    connection: FakeConnection | None = None,
    client: object | None = object(),
    analyst_client: object | None = None,
) -> RunRecord:
    return collect_run(
        connection or FakeConnection(),  # type: ignore[arg-type]
        experiment_id="experiment",
        query_name="Q1",
        sql="SELECT * FROM orders",
        output_root=tmp_path,
        client=client,  # type: ignore[arg-type]
        analyst_client=analyst_client,  # type: ignore[arg-type]
    )


def test_a_completed_run_is_saved_with_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange(monkeypatch)

    run = collect(tmp_path)

    assert run.failure_stage is None
    assert run.evidence.database_description == "PostgreSQL 16\norders"
    assert (run.evidence.postgres_plan, run.evidence.agent_plan) == (SEQ, INDEX)
    assert run.evidence.agent_attempts == ANSWERS
    assert run.evidence.forcing is not None
    assert run.evidence.forcing.hints == "IndexScan(orders orders_pkey)"
    assert run.evidence.timing == TimingEvidence(postgres_ms=100.0, realized_ms=125.0)
    assert run.analyst.status == "not_requested"
    assert load_experiment("experiment", tmp_path) == [run]


def test_an_unusable_agent_plan_keeps_its_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange(monkeypatch)

    def no_usable_plan(*_args: object) -> AgentResult:
        raise AgentPlanError(["invalid"], ANSWERS[:1])

    monkeypatch.setattr(project, "ask_agent", no_usable_plan)

    run = collect(tmp_path)

    assert run.failure_stage == "agent_plan"
    assert run.evidence.postgres_plan == SEQ
    assert run.evidence.agent_attempts == ANSWERS[:1]


def test_a_failed_request_keeps_the_answers_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange(monkeypatch)

    def network_failure(*_args: object) -> AgentResult:
        raise AgentError("network failed", ANSWERS[:1])

    monkeypatch.setattr(project, "ask_agent", network_failure)

    run = collect(tmp_path)

    assert run.failure_stage == "agent_request"
    assert run.evidence.agent_attempts == ANSWERS[:1]


def test_a_client_that_cannot_start_is_an_agent_request_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange(monkeypatch)

    def no_api_key(**_kwargs: object) -> None:
        raise anthropic.AnthropicError("no API key")

    monkeypatch.setattr(project.anthropic, "Anthropic", no_api_key)

    run = collect(tmp_path, client=None)

    assert run.failure_stage == "agent_request"
    assert run.evidence.postgres_plan == SEQ


def test_a_forcing_failure_keeps_the_agent_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange(monkeypatch)

    def cannot_force(*_args: object) -> ForcePlanResult:
        raise ValueError("cannot force")

    monkeypatch.setattr("comparison.force_plan", cannot_force)

    run = collect(tmp_path)

    assert run.failure_stage == "forcing"
    assert run.evidence.agent_plan == INDEX
    assert run.evidence.forcing == ForcingEvidence(failure_reason="cannot force")


def test_a_timeout_completes_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arrange(monkeypatch, times=(100.0, None))

    run = collect(tmp_path)

    assert run.failure_stage is None
    assert run.evidence.timing == TimingEvidence(postgres_ms=100.0, realized_ms=None)


def test_a_database_error_while_planning_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange(monkeypatch)

    def database_down(*_args: object, **_kwargs: object) -> PlanNode:
        raise psycopg.OperationalError("database failed")

    monkeypatch.setattr(project, "get_postgres_plan", database_down)
    connection = FakeConnection()

    run = collect(tmp_path, connection)

    assert run.failure_stage == "postgres_plan"
    assert connection.rollbacks == 1


def test_a_database_error_while_timing_keeps_the_first_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange(monkeypatch)
    calls = 0

    def second_call_fails(*_args: object, **_kwargs: object) -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise psycopg.OperationalError("database failed")
        return 100.0

    monkeypatch.setattr(project, "execution_time", second_call_fails)
    connection = FakeConnection()

    run = collect(tmp_path, connection)

    assert run.failure_stage == "timing"
    assert connection.rollbacks == 1
    assert run.evidence.timing == TimingEvidence(postgres_ms=100.0)


def test_provenance_outside_a_git_checkout_has_no_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange(monkeypatch)
    monkeypatch.setattr(project, "PROJECT_FOLDER", tmp_path)

    assert collect(tmp_path).provenance.git_commit is None


def test_the_analyst_is_saved_inside_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange(monkeypatch)

    run = collect(tmp_path, analyst_client=advice_for(record()))

    assert run.analyst.status == "completed"
    assert len(run.analyst.assessments) == 2
    assert load_experiment("experiment", tmp_path) == [run]


# Workload summary


def test_summary_gives_each_query_its_rates() -> None:
    summary = summarise_workload([
        record("Q1", agent=SEQ, realized=SEQ),
        record("Q2", agent=PlanNode(node_type="Result"), realized=SEQ),
        record("Q3", agent=INDEX, realized=SEQ),
    ])

    proposed = {query: row.scores["proposed_difference"] for query, row in summary.items()}
    assert {query: (score.numerator, score.denominator) for query, score in proposed.items()} == {
        "Q1": (0, 1), "Q2": (2, 2), "Q3": (1, 1)
    }


def test_summary_counts_answers_and_marks_missing_rates() -> None:
    summary = summarise_workload([
        record("Q1", agent=SEQ, realized=SEQ, answers=1),
        record("Q2", realized=None, answers=2, failure_stage="forcing"),
        record("Q3", agent=None, answers=3, failure_stage="agent_plan"),
        record("Q4", agent=None, answers=1, failure_stage="agent_request"),
    ])

    assert {query: row.answers for query, row in summary.items()} == {
        "Q1": 1, "Q2": 2, "Q3": 3, "Q4": None
    }
    assert summary["Q2"].scores["realizability"].status is MetricStatus.UNAVAILABLE
    assert summary["Q3"].scores == {} and summary["Q4"].scores == {}
    # Q1's plans match, so there is no disagreement to realize.
    assert summary["Q1"].scores["intended_realization"].status is MetricStatus.NOT_APPLICABLE


def test_summary_flags_each_plan_parallel_or_not() -> None:
    summary = summarise_workload([
        record("Q1", postgres=PARALLEL, agent=SEQ, realized=SEQ),
        record("Q2", postgres=PARALLEL, agent=None, failure_stage="agent_plan"),
    ])

    assert {query: row.parallel for query, row in summary.items()} == {
        "Q1": ParallelFlags(postgres=True, agent=False, realized=False),
        "Q2": ParallelFlags(postgres=True, agent=None, realized=None),
    }


def test_summary_time_ratio_and_timeouts() -> None:
    summary = summarise_workload([
        record("Q1", timing=TimingEvidence(postgres_ms=100, realized_ms=200)),
        record("Q2", timing=TimingEvidence(postgres_ms=100, realized_ms=None)),
        record("Q3", timing=TimingEvidence(postgres_ms=100), failure_stage="timing"),
        record("Q4", timing=None),
    ])

    assert {query: (row.time_ratio, row.timed_out) for query, row in summary.items()} == {
        "Q1": (2.0, False), "Q2": (None, True), "Q3": (None, False), "Q4": (None, False)
    }


# Advisory analyst


def test_one_request_covers_every_gap_without_measured_fields_or_answers() -> None:
    run = record()
    run = run.model_copy(update={"evidence": run.evidence.model_copy(update={
        "agent_attempts": (AgentAttempt(response="private planner prose"),),
        "postgres_plan": SEQ.model_copy(update={"total_cost": 12.5}),
    })})
    client = advice_for(run)

    result = run_analyst(client, run)  # type: ignore[arg-type]

    assert result.status == "completed" and len(result.assessments) == 2
    [sent] = client.requests
    assert "private planner prose" not in str(sent)
    assert "total_cost" not in str(sent)


def test_the_analyst_explains_a_parallel_only_gap() -> None:
    request = build_analyst_request(record(agent=PARALLEL, realized=SEQ))

    assert request is not None and request.gap_ids == ("parallel(orders)",)


def test_no_gap_means_no_analyst_call() -> None:
    client = FakeAnalystClient("")

    result = run_analyst(client, record(agent=SEQ, realized=SEQ))  # type: ignore[arg-type]

    assert (result.status, client.requests) == ("completed", [])


@pytest.mark.parametrize(
    ("client", "status"),
    [
        (FakeAnalystClient("not json"), "invalid_response"),
        (FakeAnalystClient(AnalystResponse(assessments=()).model_dump_json()), "invalid_response"),
        (FakeAnalystClient("", stop_reason="refusal"), "unavailable"),
    ],
    ids=["malformed", "a gap left out", "refusal"],
)
def test_analyst_failures_become_a_status(client: FakeAnalystClient, status: str) -> None:
    run = record()

    result = run_analyst(client, run)  # type: ignore[arg-type]

    assert result.status == status and result.reason
    assert evaluation_from_run(run.model_copy(update={"analyst": result})) == evaluation_from_run(
        run
    )


@pytest.mark.parametrize(
    "fields",
    [{"cause": "bad statistics", "evidence": ["plan"]}, {"cause": "unknown", "evidence": []}],
    ids=["unknown cause", "no evidence"],
)
def test_the_analyst_must_use_a_known_cause_and_give_evidence(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AnalystAssessment.model_validate(
            {"gap_id": "scan(orders)", "explanation": "why", "confidence": "low", **fields}
        )
