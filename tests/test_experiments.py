"""Stored experiment runs, offline aggregation and advisory analysis."""

from datetime import UTC, datetime
from types import SimpleNamespace
import json
from pathlib import Path
from uuid import uuid4

import anthropic
import psycopg
import pytest
from pydantic import ValidationError

from experiments import (
    AnalystConfiguration,
    AnalystAssessment,
    AnalystResult,
    AnalystResponse,
    AnalystStatus,
    DatabaseContext,
    ForcingEvidence,
    MeasurementConfiguration,
    PlannerAttempt,
    PlannerConfiguration,
    RunEvidence,
    RunIdentity,
    RunProvenance,
    RunRecord,
    RunStatus,
    TimingEvidence,
    collect_run,
    collect_experiment,
    aggregate_runs,
    advisory_result,
    build_analyst_request,
    database_context,
    evaluation_from_run,
    evaluation_json,
    forcing_evidence,
    load_run,
    load_experiment,
    load_evaluation_json,
    planner_configuration,
    run_analyst,
    validate_analyst_response,
    write_run,
)
from comparison import ForcePlanResult, ForcingFailure, realization_gaps
from preprocessing import (
    AgentAttempt,
    AgentPlanError,
    AgentRequestError,
    AgentResult,
    DatabaseDescription,
    PlanNode,
)


@pytest.fixture
def run_root() -> Path:
    root = Path("agent_runs") / f"pytest-{uuid4().hex}"
    try:
        yield root
    finally:
        if root.exists():
            for file in root.rglob("*"):
                if file.is_file():
                    file.unlink()
            for directory in sorted(
                (path for path in root.rglob("*") if path.is_dir()), reverse=True
            ):
                directory.rmdir()
            root.rmdir()


def configuration() -> PlannerConfiguration:
    return PlannerConfiguration(
        model="claude-haiku-5-5",
        prompt_sha256="0" * 64,
        max_tokens=64_000,
        first_effort="low",
        retry_effort="medium",
        max_attempts=3,
    )


def record(*, failed: bool = False) -> RunRecord:
    postgres = PlanNode(node_type="Seq Scan", alias="orders")
    agent = PlanNode(
        node_type="Index Scan",
        alias="orders",
        index_name="orders_pkey",
    )
    return RunRecord(
        identity=RunIdentity(
            experiment_id="experiment-1",
            run_id="run-1",
            query_name="Q1",
            sample=1,
            created_at=datetime(2026, 11, 14, tzinfo=UTC),
        ),
        provenance=RunProvenance(git_commit="abc123", git_dirty=False),
        planner=configuration(),
        measurement=MeasurementConfiguration(
            timeout_ms=60_000, warmup_runs=1, measured_runs=1
        ),
        database=DatabaseContext(
            postgres_version="18.0",
            description="PostgreSQL 18.0\nSettings: work_mem=4MB",
            indexes={"orders": ("orders_pkey",)},
        ),
        evidence=RunEvidence(
            original_sql="SELECT * FROM orders",
            rewritten_sql="SELECT * FROM orders",
            postgres_plan=postgres,
            agent_plan=None if failed else agent,
            agent_attempts=() if failed else (PlannerAttempt(response="{}"),),
            forcing=(
                None
                if failed
                else ForcingEvidence(
                    realized_plan=postgres,
                    hints="SeqScan(orders)",
                    switch_offs=False,
                    leading=False,
                )
            ),
            timing=None if failed else TimingEvidence(postgres_ms=1, realized_ms=1),
        ),
        status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
        failure_stage="agent" if failed else None,
        failure_reason="No usable answer" if failed else None,
    )


@pytest.mark.parametrize("failed", [False, True])
def test_successful_and_partial_run_schema_round_trip(failed: bool) -> None:
    original = record(failed=failed)

    loaded = RunRecord.model_validate_json(original.model_dump_json())

    assert loaded == original
    assert loaded.provenance.schema_version == 1


def test_completed_run_rejects_missing_success_evidence() -> None:
    payload = record().model_dump()
    payload["evidence"]["agent_plan"] = None

    with pytest.raises(ValidationError, match="completed run is missing agent plan"):
        RunRecord.model_validate(payload)


def test_run_schema_rejects_secret_fields() -> None:
    data = record().model_dump(mode="json")
    data["database"]["password"] = "admin"  # type: ignore[index]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunRecord.model_validate(data)

    serialized = record().model_dump_json()
    assert "password" not in serialized and "api_key" not in serialized


def test_selected_forcing_outcome_round_trips_into_evaluator() -> None:
    postgres = PlanNode(node_type="Seq Scan", alias="orders", relation="orders")
    agent = PlanNode(
        node_type="Index Scan",
        alias="orders",
        relation="orders",
        index_name="orders_pkey",
    )
    selected = ForcePlanResult(
        hints="IndexScan(orders orders_pkey)",
        plan=postgres,
        gaps=tuple(realization_gaps(agent, postgres)),
        switch_offs=False,
        leading=True,
    )
    original = record().model_copy(
        update={
            "evidence": RunEvidence(
                original_sql="SELECT * FROM orders",
                rewritten_sql="SELECT * FROM orders",
                postgres_plan=postgres,
                agent_plan=agent,
                agent_attempts=(PlannerAttempt(response="{}"),),
                forcing=forcing_evidence(selected),
                timing=TimingEvidence(postgres_ms=1, realized_ms=1),
            )
        }
    )

    loaded = RunRecord.model_validate_json(original.model_dump_json())
    evaluation = evaluation_from_run(loaded)

    assert isinstance(loaded.evidence.forcing, ForcingEvidence)
    assert loaded.evidence.forcing.hints == selected.hints
    assert loaded.evidence.forcing.switch_offs is False
    assert evaluation.proposed.all_kinds.difference.rate == 0.5
    assert evaluation.realized.all_kinds.difference.rate == 0.0
    assert evaluation.realizability.gaps == selected.gaps


def test_offline_evaluation_recomputes_gaps_from_plans() -> None:
    postgres = PlanNode(node_type="Seq Scan", relation="orders", alias="orders")
    agent = PlanNode(
        node_type="Index Scan",
        relation="orders",
        alias="orders",
        index_name="orders_pkey",
    )
    run = record().model_copy(
        update={
            "evidence": RunEvidence(
                original_sql="SELECT * FROM orders",
                rewritten_sql="SELECT * FROM orders",
                postgres_plan=postgres,
                agent_plan=agent,
                forcing=ForcingEvidence(
                    realized_plan=postgres,
                    hints="IndexScan(orders orders_pkey)",
                    # Deliberately stale: current evaluation must ignore this cache.
                    gaps=(),
                    switch_offs=True,
                    leading=True,
                ),
            )
        }
    )

    evaluation = evaluation_from_run(run)

    assert [str(gap.decision) for gap in evaluation.realizability.gaps] == [
        "index(orders)", "scan(orders)"
    ]


def test_offline_evaluation_preserves_recorded_forcing_failure() -> None:
    postgres = PlanNode(node_type="Seq Scan", relation="orders", alias="orders")
    agent = PlanNode(node_type="Index Scan", relation="orders", alias="orders")
    run = record(failed=True).model_copy(
        update={
            "evidence": RunEvidence(
                original_sql="SELECT * FROM orders",
                rewritten_sql="SELECT * FROM orders",
                postgres_plan=postgres,
                agent_plan=agent,
                forcing=ForcingEvidence(failure_reason="pg_hint_plan unavailable"),
            ),
            "failure_stage": "forcing",
            "failure_reason": "pg_hint_plan unavailable",
        }
    )

    evaluation = evaluation_from_run(run)

    assert evaluation.proposed.all_kinds.difference.rate == 0.5
    assert evaluation.realized.all_kinds.difference.rate is None
    assert evaluation.realized.all_kinds.difference.explanation == "pg_hint_plan unavailable"


def test_runtime_configuration_and_database_context_are_explicit() -> None:
    class Info:
        @staticmethod
        def parameter_status(name: str) -> str:
            assert name == "server_version"
            return "18.1 (test build)"

    class Connection:
        info = Info()

    from preprocessing import DatabaseDescription

    planner = planner_configuration()
    database = database_context(
        Connection(),
        DatabaseDescription(
            "PostgreSQL 18.1\nSettings: work_mem=4MB",
            {"orders": {"orders_date_idx", "orders_pkey"}},
        ),
    )

    assert planner.max_tokens == 64_000
    assert len(planner.prompt_sha256) == 64
    assert database.postgres_version == "18.1"
    assert database.description.startswith("PostgreSQL 18.1")
    assert database.indexes["orders"] == ("orders_date_idx", "orders_pkey")


def test_documented_sample_record_validates() -> None:
    document = (Path(__file__).parents[1] / "docs" / "experiments.md").read_text(
        encoding="utf-8"
    )
    sample = document.split("```json\n", 1)[1].split("\n```", 1)[0]

    loaded = RunRecord.model_validate(json.loads(sample))

    assert loaded.identity.run_id == "q1-s1"
    assert loaded.status is RunStatus.FAILED


@pytest.mark.parametrize("failed", [False, True])
def test_run_file_is_created_atomically_and_round_trips(
    run_root: Path, failed: bool
) -> None:
    original = record(failed=failed)

    path = write_run(original, run_root)

    assert path == run_root / "experiment-1" / "run-1.json"
    assert load_run(path) == original
    assert list(path.parent.glob("*.tmp")) == []


def test_existing_run_is_never_replaced(run_root: Path) -> None:
    original = record()
    path = write_run(original, run_root)

    with pytest.raises(FileExistsError):
        write_run(original.model_copy(update={"status": RunStatus.COMPLETED}), run_root)

    assert load_run(path) == original
    assert list(path.parent.glob("*.tmp")) == []
class FakeInfo:
    @staticmethod
    def parameter_status(_name: str) -> str:
        return "18.1"


class FakeConnection:
    info = FakeInfo()

    def __init__(self) -> None:
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1


def arrange_collection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    forcing_failure: bool = False,
    agent_failure: bool = False,
    timed_out: bool = False,
) -> tuple[PlanNode, PlanNode]:
    import experiments

    postgres = PlanNode(node_type="Seq Scan", relation="orders", alias="orders")
    agent = PlanNode(
        node_type="Index Scan",
        relation="orders",
        alias="orders",
        index_name="orders_pkey",
    )
    database = DatabaseDescription("PostgreSQL 18.1\norders", {"orders": {"orders_pkey"}})
    monkeypatch.setattr(experiments, "capture_git_provenance", lambda _repo: RunProvenance(
        git_commit="abc123", git_dirty=False
    ))
    monkeypatch.setattr(experiments, "describe_database", lambda *_args: database)
    monkeypatch.setattr(experiments, "get_postgres_plan", lambda *_args, **_kwargs: postgres)
    attempts = (
        AgentAttempt("bad", ("invalid",)),
        AgentAttempt("good", ()),
    )
    if agent_failure:
        def fail_agent(*_args: object) -> AgentResult:
            raise AgentPlanError(["invalid"], "bad", attempts[:1])
        monkeypatch.setattr(experiments, "ask_agent", fail_agent)
    else:
        monkeypatch.setattr(
            experiments,
            "ask_agent",
            lambda *_args: AgentResult(agent, "Use index", "SELECT", "good", [["invalid"]], attempts),
        )
    selected = ForcePlanResult(
        hints="IndexScan(orders orders_pkey)",
        plan=postgres,
        gaps=tuple(realization_gaps(agent, postgres)),
        switch_offs=False,
        leading=True,
    )
    monkeypatch.setattr(
        experiments,
        "force_plan",
        lambda *_args: ForcingFailure("cannot force") if forcing_failure else selected,
    )
    times = iter([100.0, None if timed_out else 125.0])
    monkeypatch.setattr(experiments, "measure_execution_time", lambda *_args, **_kwargs: next(times))
    return postgres, agent


def test_collector_stores_success_and_only_selected_forcing_evidence(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    postgres, agent = arrange_collection(monkeypatch)

    run = collect_run(
        FakeConnection(),  # type: ignore[arg-type]
        experiment_id="experiment-2",
        run_id="success",
        query_name="Q1",
        sample=1,
        sql="SELECT * FROM orders",
        output_root=run_root,
        client=object(),  # type: ignore[arg-type]
    )

    assert run.status is RunStatus.COMPLETED
    assert run.evidence.postgres_plan == postgres
    assert run.evidence.agent_plan == agent
    assert [attempt.response for attempt in run.evidence.agent_attempts] == ["bad", "good"]
    assert run.evidence.forcing is not None
    assert run.evidence.forcing.hints == "IndexScan(orders orders_pkey)"
    assert run.evidence.forcing.switch_offs is False
    assert run.evidence.timing == run.evidence.timing.model_copy()
    assert load_run(run_root / "experiment-2" / "success.json") == run


@pytest.mark.parametrize(
    ("forcing_failure", "agent_failure", "expected_stage"),
    [(False, True, "agent_plan"), (True, False, "forcing")],
)
def test_collector_preserves_failure_and_prior_evidence(
    run_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    forcing_failure: bool,
    agent_failure: bool,
    expected_stage: str,
) -> None:
    arrange_collection(
        monkeypatch, forcing_failure=forcing_failure, agent_failure=agent_failure
    )

    run = collect_run(
        FakeConnection(),  # type: ignore[arg-type]
        experiment_id="experiment-2",
        run_id=expected_stage,
        query_name="Q1",
        sample=1,
        sql="SELECT * FROM orders",
        output_root=run_root,
        client=object(),  # type: ignore[arg-type]
    )

    assert run.status is RunStatus.FAILED
    assert run.failure_stage == expected_stage
    assert run.evidence.postgres_plan is not None
    assert run.evidence.agent_attempts
    if forcing_failure:
        assert run.evidence.agent_plan is not None
        assert run.evidence.forcing is not None
        assert run.evidence.forcing.failure_reason == "cannot force"


def test_collector_stores_postgres_evidence_when_client_initialization_fails(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import experiments

    arrange_collection(monkeypatch)

    def fail_client(**_kwargs: object) -> None:
        raise anthropic.AnthropicError("client initialization failed")

    monkeypatch.setattr(experiments.anthropic, "Anthropic", fail_client)

    run = collect_run(
        FakeConnection(),  # type: ignore[arg-type]
        experiment_id="experiment-client-failure",
        run_id="client-failure",
        query_name="Q1",
        sample=1,
        sql="SELECT * FROM orders",
        output_root=run_root,
    )

    assert run.status is RunStatus.FAILED
    assert run.failure_stage == "agent_plan"
    assert run.evidence.postgres_plan is not None
    assert load_run(
        run_root / "experiment-client-failure" / "client-failure.json"
    ) == run


def test_collector_stores_attempts_completed_before_request_failure(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import experiments

    arrange_collection(monkeypatch)
    completed = AgentAttempt("invalid response", ("invalid JSON",))

    def fail_after_attempt(*_args: object) -> AgentResult:
        raise AgentRequestError("network failed", (completed,))

    monkeypatch.setattr(experiments, "ask_agent", fail_after_attempt)

    run = collect_run(
        FakeConnection(),  # type: ignore[arg-type]
        experiment_id="experiment-request-failure",
        run_id="request-failure",
        query_name="Q1",
        sample=1,
        sql="SELECT * FROM orders",
        output_root=run_root,
        client=object(),  # type: ignore[arg-type]
    )

    assert run.status is RunStatus.FAILED
    assert run.failure_stage == "agent_plan"
    assert run.evidence.agent_attempts == (
        PlannerAttempt(
            response="invalid response", validation_problems=("invalid JSON",)
        ),
    )


def test_collector_records_timeout_without_discarding_plans(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange_collection(monkeypatch, timed_out=True)

    run = collect_run(
        FakeConnection(),  # type: ignore[arg-type]
        experiment_id="experiment-2",
        run_id="timeout",
        query_name="Q1",
        sample=1,
        sql="SELECT * FROM orders",
        output_root=run_root,
        client=object(),  # type: ignore[arg-type]
    )

    assert run.status is RunStatus.COMPLETED
    assert run.evidence.timing is not None
    assert run.evidence.timing.realized_ms is None
    assert run.evidence.timing.realized_timed_out is True
    assert run.evidence.postgres_plan is not None
    assert run.evidence.agent_plan is not None


def test_collector_preserves_selected_plan_when_timing_fails(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import experiments

    arrange_collection(monkeypatch)

    calls = 0

    def fail_realized_timing(*_args: object, **_kwargs: object) -> float:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 100.0
        raise RuntimeError("timing failed")

    monkeypatch.setattr(experiments, "measure_execution_time", fail_realized_timing)

    run = collect_run(
        FakeConnection(),  # type: ignore[arg-type]
        experiment_id="experiment-2",
        run_id="timing-failure",
        query_name="Q1",
        sample=1,
        sql="SELECT * FROM orders",
        output_root=run_root,
        client=object(),  # type: ignore[arg-type]
    )

    assert run.status is RunStatus.FAILED
    assert run.failure_stage == "timing"
    assert run.failure_reason == "timing failed"
    assert run.evidence.forcing is not None
    assert run.evidence.forcing.realized_plan is not None
    assert run.evidence.timing == TimingEvidence(postgres_ms=100.0)


@pytest.mark.parametrize("stage", ["postgres_plan", "timing"])
def test_collector_rolls_back_database_errors(
    run_root: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    import experiments

    arrange_collection(monkeypatch)

    def fail_database(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("database failed")

    if stage == "postgres_plan":
        monkeypatch.setattr(experiments, "get_postgres_plan", fail_database)
    else:
        monkeypatch.setattr(experiments, "measure_execution_time", fail_database)
    connection = FakeConnection()

    run = collect_run(
        connection,  # type: ignore[arg-type]
        experiment_id="experiment-rollback",
        run_id=stage,
        query_name="Q1",
        sample=1,
        sql="SELECT * FROM orders",
        output_root=run_root,
        client=object(),  # type: ignore[arg-type]
    )

    assert run.status is RunStatus.FAILED
    assert run.failure_stage == stage
    assert connection.rollbacks == 1


def evaluable_record(
    query: str, run_id: str, postgres: PlanNode, agent: PlanNode
) -> RunRecord:
    selected = ForcePlanResult(
        hints="selected",
        plan=agent,
        gaps=tuple(realization_gaps(agent, agent)),
        switch_offs=True,
        leading=True,
    )
    return record().model_copy(
        update={
            "identity": record().identity.model_copy(
                update={"query_name": query, "run_id": run_id}
            ),
            "evidence": RunEvidence(
                original_sql="SELECT 1",
                rewritten_sql="SELECT 1",
                postgres_plan=postgres,
                agent_plan=agent,
                forcing=forcing_evidence(selected),
            ),
        }
    )


def test_macro_and_micro_averages_use_different_documented_weighting() -> None:
    seq = PlanNode(node_type="Seq Scan", relation="orders", alias="orders")
    result = PlanNode(node_type="Result")
    index = PlanNode(node_type="Index Scan", relation="orders", alias="orders")
    runs = [
        evaluable_record("Q1", "q1-1", seq, seq),
        evaluable_record("Q1", "q1-2", seq, result),
        evaluable_record("Q2", "q2-1", seq, index),
    ]

    aggregate = aggregate_runs(runs).metrics["proposed_difference"]

    # Q1 averages 0/2 and 3/3 to 0.5; Q2 is 1/2. Macro gives both
    # queries equal weight, while micro pools the four differences over seven decisions.
    assert aggregate.per_query == {"Q1": 0.5, "Q2": 0.5}
    assert aggregate.macro_rate == 0.5
    assert (aggregate.micro_numerator, aggregate.micro_denominator) == (4, 7)
    assert aggregate.micro_rate == pytest.approx(4 / 7)


def test_aggregate_reports_every_defined_undefined_failed_and_excluded_run() -> None:
    seq = PlanNode(node_type="Seq Scan", relation="orders", alias="orders")
    index = PlanNode(node_type="Index Scan", relation="orders", alias="orders")
    complete = evaluable_record("Q1", "complete", seq, seq)
    forcing_failed = record(failed=True).model_copy(
        update={
            "identity": record().identity.model_copy(
                update={"query_name": "Q2", "run_id": "forcing-failed"}
            ),
            "evidence": RunEvidence(
                original_sql="SELECT 1",
                rewritten_sql="SELECT 1",
                postgres_plan=seq,
                agent_plan=index,
                forcing=ForcingEvidence(failure_reason="cannot force"),
            ),
            "failure_stage": "forcing",
            "failure_reason": "cannot force",
        }
    )
    agent_failed = record(failed=True).model_copy(
        update={
            "identity": record().identity.model_copy(
                update={"query_name": "Q3", "run_id": "agent-failed"}
            ),
            "failure_stage": "agent_plan",
            "failure_reason": "no usable answer",
        }
    )

    summary = aggregate_runs([complete, forcing_failed, agent_failed])
    proposed = summary.metrics["proposed_difference"]
    intended = summary.metrics["intended_realization"]

    assert proposed.total_runs == 3
    assert proposed.defined_runs == 2
    assert proposed.unavailable_runs == 1
    assert proposed.failed_runs == 2
    assert (proposed.included_queries, proposed.excluded_queries) == (2, 1)
    assert sum(proposed.exclusion_reasons.values()) == 1
    assert intended.defined_runs == 0
    assert intended.undefined_runs == 1
    assert intended.unavailable_runs == 2
    assert sum(intended.exclusion_reasons.values()) == 3
    assert (
        intended.defined_runs + intended.undefined_runs + intended.unavailable_runs
        == intended.total_runs
    )


def test_timing_uses_per_query_median_and_keeps_missing_and_timeout_explicit() -> None:
    def timed(run_id: str, timing: TimingEvidence | None) -> RunRecord:
        base = record()
        return base.model_copy(
            update={
                "identity": base.identity.model_copy(
                    update={"query_name": "Q1", "run_id": run_id}
                ),
                "evidence": base.evidence.model_copy(update={"timing": timing}),
            }
        )

    runs = [
        timed("ratio-2", TimingEvidence(postgres_ms=100, realized_ms=200)),
        timed("ratio-4", TimingEvidence(postgres_ms=50, realized_ms=200)),
        timed(
            "timeout",
            TimingEvidence(
                postgres_ms=100, realized_ms=None, realized_timed_out=True
            ),
        ),
        timed("missing", None).model_copy(
            update={
                "identity": timed("unused", None).identity.model_copy(
                    update={"query_name": "Q2", "run_id": "missing"}
                )
            }
        ),
    ]

    timing = aggregate_runs(runs).timing

    assert timing.per_query_median_ratio == {"Q1": 3.0}
    assert timing.defined_runs == 2
    assert timing.timed_out_runs == 1
    assert timing.missing_runs == 1
    assert "workload" not in timing.model_dump()
def test_analyst_schema_accepts_unknown_with_evidence_and_confidence() -> None:
    configuration = AnalystConfiguration()
    response = AnalystResponse.model_validate({
        "assessments": [{
            "gap_id": "scan(orders)",
            "cause": "unknown",
            "explanation": "The supplied plans do not establish the cause.",
            "evidence": ["R uses Seq Scan although A requests Index Scan."],
            "confidence": "low",
        }]
    })

    validated = validate_analyst_response(
        response.model_dump_json(), {"scan(orders)"}
    )

    assert configuration.model == "claude-haiku-5-5"
    assert validated[0].cause == "unknown"


@pytest.mark.parametrize(
    "assessment",
    [
        {
            "gap_id": "scan(orders)",
            "cause": "cardinality_estimation_difference",
            "explanation": "unsupported cause",
            "evidence": ["plan"],
            "confidence": "low",
        },
        {
            "gap_id": "scan(orders)",
            "cause": "unknown",
            "explanation": "missing evidence",
            "evidence": [],
            "confidence": "low",
        },
    ],
)
def test_analyst_schema_rejects_unsupported_causes_and_missing_evidence(
    assessment: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        AnalystResponse.model_validate({"assessments": [assessment]})


def test_analyst_response_rejects_unknown_missing_and_duplicate_gap_ids() -> None:
    def response(*gap_ids: str) -> str:
        return AnalystResponse(
            assessments=tuple(
                {
                    "gap_id": gap_id,
                    "cause": "unknown",
                    "explanation": "Insufficient evidence.",
                    "evidence": ("stored plan",),
                    "confidence": "low",
                }
                for gap_id in gap_ids
            )
        ).model_dump_json()

    with pytest.raises(ValueError, match="unknown gaps"):
        validate_analyst_response(response("scan(other)"), {"scan(orders)"})
    with pytest.raises(ValueError, match="omitted gaps"):
        validate_analyst_response(response(), {"scan(orders)"})
    with pytest.raises(ValueError, match="more than once"):
        validate_analyst_response(
            response("scan(orders)", "scan(orders)"), {"scan(orders)"}
        )


class FakeAnalystClient:
    def __init__(self, assessments: tuple[AnalystAssessment, ...]) -> None:
        self.messages = self
        self.assessments = assessments
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> SimpleNamespace:
        self.requests.append(request)
        text = AnalystResponse(assessments=self.assessments).model_dump_json()
        return SimpleNamespace(
            content=[anthropic.types.TextBlock(type="text", text=text)]
        )


class MalformedAnalystClient:
    messages: "MalformedAnalystClient"

    def __init__(self, *, refusal: bool = False) -> None:
        self.messages = self
        self.refusal = refusal

    def create(self, **_request: object) -> SimpleNamespace:
        return SimpleNamespace(
            content=[anthropic.types.TextBlock(type="text", text="not json")],
            stop_reason="refusal" if self.refusal else "end_turn",
        )


def gapped_run() -> RunRecord:
    postgres = PlanNode(node_type="Seq Scan", relation="orders", alias="orders")
    agent = PlanNode(
        node_type="Index Scan",
        relation="orders",
        alias="orders",
        index_name="orders_pkey",
    )
    selected = ForcePlanResult(
        hints="IndexScan(orders orders_pkey)",
        plan=postgres,
        gaps=tuple(realization_gaps(agent, postgres)),
        switch_offs=True,
        leading=True,
    )
    return record().model_copy(
        update={
            "evidence": RunEvidence(
                original_sql="SELECT * FROM orders",
                rewritten_sql="SELECT * FROM orders",
                postgres_plan=postgres,
                agent_plan=agent,
                agent_attempts=(PlannerAttempt(response="private planner prose",),),
                forcing=forcing_evidence(selected),
                timing=TimingEvidence(postgres_ms=1, realized_ms=1),
            )
        }
    )


def test_one_analyst_request_batches_every_gap_without_planner_prose() -> None:
    run = gapped_run()
    request = build_analyst_request(run)
    assert request is not None
    assessments = tuple(
        AnalystAssessment(
            gap_id=gap_id,
            cause="forcing_limitation",
            explanation="The selected hints did not reproduce this request.",
            evidence=("A and R differ for this decision.",),
            confidence="medium",
        )
        for gap_id in request.gap_ids
    )
    client = FakeAnalystClient(assessments)

    result = run_analyst(client, run)  # type: ignore[arg-type]

    assert len(request.gap_ids) == 2
    assert len(client.requests) == 1
    assert len(result.assessments) == 2
    sent = client.requests[0]
    assert "private planner prose" not in str(sent)
    assert "startup_cost" not in str(sent)


def test_zero_gap_run_completes_without_an_analyst_call() -> None:
    plan = PlanNode(node_type="Seq Scan", relation="orders", alias="orders")
    run = evaluable_record("Q1", "no-gaps", plan, plan)
    client = FakeAnalystClient(())

    result = run_analyst(client, run)  # type: ignore[arg-type]

    assert result.status.value == "completed"
    assert result.assessments == ()
    assert client.requests == []


def test_analyst_failures_and_unknown_advice_never_change_metrics() -> None:
    run = gapped_run()
    request = build_analyst_request(run)
    assert request is not None
    before = evaluation_from_run(run)
    unknown = tuple(
        AnalystAssessment(
            gap_id=gap_id,
            cause="unknown",
            explanation="The evidence is insufficient.",
            evidence=("A differs from R.",),
            confidence="low",
        )
        for gap_id in request.gap_ids
    )

    completed = advisory_result(FakeAnalystClient(unknown), run)  # type: ignore[arg-type]
    malformed = advisory_result(MalformedAnalystClient(), run)  # type: ignore[arg-type]
    refused = advisory_result(MalformedAnalystClient(refusal=True), run)  # type: ignore[arg-type]

    assert completed.status is AnalystStatus.COMPLETED
    assert completed.assessments[0].cause == "unknown"
    assert malformed.status is AnalystStatus.INVALID_RESPONSE
    assert malformed.reason
    assert refused.status is AnalystStatus.UNAVAILABLE
    assert refused.reason
    for analyst in (completed, malformed, refused, AnalystResult()):
        assert evaluation_from_run(run.model_copy(update={"analyst": analyst})) == before


def test_collector_persists_analyst_inside_the_same_run(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrange_collection(monkeypatch)
    gaps = build_analyst_request(gapped_run())
    assert gaps is not None
    assessments = tuple(
        AnalystAssessment(
            gap_id=gap_id,
            cause="forcing_limitation",
            explanation="The selected forcing outcome did not reproduce the request.",
            evidence=("Stored A and R differ.",),
            confidence="medium",
        )
        for gap_id in gaps.gap_ids
    )

    run = collect_run(
        FakeConnection(),  # type: ignore[arg-type]
        experiment_id="experiment-analyst",
        run_id="with-analyst",
        query_name="Q1",
        sample=1,
        sql="SELECT * FROM orders",
        output_root=run_root,
        client=object(),  # type: ignore[arg-type]
        analyst_client=FakeAnalystClient(assessments),  # type: ignore[arg-type]
    )

    stored = load_run(run_root / "experiment-analyst" / "with-analyst.json")
    assert run.analyst.status is AnalystStatus.COMPLETED
    assert stored.analyst == run.analyst
    assert len(stored.analyst.assessments) == 2


def test_public_json_contract_round_trips_run_evaluation_summary_and_advice() -> None:
    run = gapped_run()
    request = build_analyst_request(run)
    assert request is not None
    advice = tuple(
        AnalystAssessment(
            gap_id=gap_id,
            cause="unknown",
            explanation="Insufficient evidence.",
            evidence=("stored plan",),
            confidence="low",
        )
        for gap_id in request.gap_ids
    )
    run = run.model_copy(
        update={
            "analyst": AnalystResult(
                status=AnalystStatus.COMPLETED,
                configuration=AnalystConfiguration(),
                assessments=advice,
            )
        }
    )
    evaluation = evaluation_from_run(run)
    summary = aggregate_runs([run])

    loaded_run = RunRecord.model_validate_json(run.model_dump_json())
    loaded_evaluation = load_evaluation_json(evaluation_json(evaluation))
    loaded_summary = type(summary).model_validate_json(summary.model_dump_json())

    assert loaded_run.analyst.assessments == advice
    assert loaded_evaluation == evaluation
    assert loaded_summary == summary
    assert loaded_summary.metrics["proposed_difference"].defined_runs == 1


def test_small_multi_query_repeated_sample_experiment_end_to_end(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import experiments

    arrange_collection(monkeypatch)
    monkeypatch.setattr(experiments, "measure_execution_time", lambda *_a, **_k: 100.0)
    request = build_analyst_request(gapped_run())
    assert request is not None
    assessments = tuple(
        AnalystAssessment(
            gap_id=gap_id,
            cause="forcing_limitation",
            explanation="The selected outcome did not reproduce the request.",
            evidence=("A and R differ.",),
            confidence="medium",
        )
        for gap_id in request.gap_ids
    )

    runs = collect_experiment(
        FakeConnection(),  # type: ignore[arg-type]
        experiment_id="workload",
        queries={"Q1": "SELECT * FROM orders", "Q2": "SELECT count(*) FROM orders"},
        samples=2,
        output_root=run_root,
        client_factory=lambda: object(),  # type: ignore[return-value]
        analyst_client_factory=lambda: FakeAnalystClient(assessments),  # type: ignore[return-value]
    )
    loaded = load_experiment("workload", run_root)
    summary = aggregate_runs(loaded)

    assert len(runs) == len(loaded) == 4
    for run in loaded:
        timestamp = run.identity.created_at.strftime("%Y-%m-%d_%H-%M-%S.%fZ")
        assert run.identity.run_id == timestamp or (
            run.identity.run_id.startswith(timestamp + "-")
            and run.identity.run_id[len(timestamp) + 1:].isdigit()
        )
    assert {(run.identity.query_name, run.identity.sample) for run in loaded} == {
        ("Q1", 1), ("Q1", 2), ("Q2", 1), ("Q2", 2)
    }
    assert all(run.analyst.status is AnalystStatus.COMPLETED for run in loaded)
    assert summary.metrics["proposed_difference"].total_runs == 4
    assert summary.metrics["proposed_difference"].included_queries == 2
    assert summary.timing.per_query_median_ratio == {"Q1": 1.0, "Q2": 1.0}
    assert all(evaluation_from_run(run).realizability.gaps for run in loaded)
