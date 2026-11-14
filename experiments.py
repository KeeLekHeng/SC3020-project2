"""Persist and analyse reproducible plan-comparison experiment runs."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import tempfile
from statistics import fmean, median
from typing import Callable, Literal
from uuid import uuid4

import anthropic
import psycopg
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from comparison import (
    Difference,
    ForcePlanResult,
    ForcingFailure,
    PlanEvaluation,
    MetricScore,
    MetricStatus,
    evaluate_plans,
    force_plan,
    realization_gaps,
)
from preprocessing import (
    AGENT_FIRST_EFFORT,
    AGENT_MAX_ATTEMPTS,
    AGENT_MAX_TOKENS,
    AGENT_MODEL,
    AGENT_RETRY_EFFORT,
    AgentPlanError,
    AgentRequestError,
    AgentResponseError,
    AgentResult,
    DatabaseDescription,
    PlanNode,
    api_key,
    ask_agent,
    agent_system_prompt,
    describe_database,
    get_postgres_plan,
    unique_aliases,
)


RUN_SCHEMA_VERSION = 1
ANALYST_MODEL = AGENT_MODEL
ANALYST_MAX_TOKENS = 4_096
ANALYST_PROMPT_VERSION = 1
_EVALUATION_ADAPTER = TypeAdapter(PlanEvaluation)


class RecordModel(BaseModel):
    """Strict immutable JSON boundary used by all experiment records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class AnalystStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


class RunIdentity(RecordModel):
    experiment_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    query_name: str = Field(min_length=1)
    sample: int = Field(ge=1)
    created_at: datetime


class RunProvenance(RecordModel):
    schema_version: Literal[RUN_SCHEMA_VERSION] = RUN_SCHEMA_VERSION
    git_commit: str = Field(min_length=1)
    git_dirty: bool


class PlannerConfiguration(RecordModel):
    model: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_tokens: int = Field(gt=0)
    first_effort: str = Field(min_length=1)
    retry_effort: str = Field(min_length=1)
    max_attempts: int = Field(gt=0)


class MeasurementConfiguration(RecordModel):
    timeout_ms: int = Field(gt=0)
    warmup_runs: int = Field(ge=0)
    measured_runs: int = Field(gt=0)


class DatabaseContext(RecordModel):
    postgres_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    indexes: dict[str, tuple[str, ...]]


class PlannerAttempt(RecordModel):
    response: str
    validation_problems: tuple[str, ...] = ()


class GapEvidence(RecordModel):
    gap_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    tables: tuple[str, ...]
    requested: str | None
    realized: str | None


class ForcingEvidence(RecordModel):
    realized_plan: PlanNode | None = None
    hints: str | None = None
    gaps: tuple[GapEvidence, ...] = ()
    switch_offs: bool | None = None
    leading: bool | None = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def selected_or_failed(self) -> "ForcingEvidence":
        selected = self.realized_plan is not None
        if selected and (
            self.hints is None
            or self.switch_offs is None
            or self.leading is None
            or self.failure_reason is not None
        ):
            raise ValueError("A selected forcing outcome needs hints and flags but no failure.")
        if not selected and not self.failure_reason:
            raise ValueError("A forcing outcome without a realized plan needs a failure reason.")
        return self


class TimingEvidence(RecordModel):
    postgres_ms: float | None = Field(default=None, ge=0)
    realized_ms: float | None = Field(default=None, ge=0)
    postgres_timed_out: bool = False
    realized_timed_out: bool = False


AnalystCause = Literal[
    "forcing_limitation", "infeasible_request", "postgresql_rewrite", "unknown"
]
AnalystConfidence = Literal["low", "medium", "high"]


class AnalystConfiguration(RecordModel):
    model: str = ANALYST_MODEL
    prompt_version: Literal[ANALYST_PROMPT_VERSION] = ANALYST_PROMPT_VERSION
    max_tokens: int = Field(default=ANALYST_MAX_TOKENS, gt=0)


class AnalystAssessment(RecordModel):
    gap_id: str = Field(min_length=1)
    cause: AnalystCause
    explanation: str = Field(min_length=1)
    evidence: tuple[str, ...] = Field(min_length=1)
    confidence: AnalystConfidence


class AnalystResponse(RecordModel):
    assessments: tuple[AnalystAssessment, ...]


class AnalystRequest(RecordModel):
    system: str
    user: str
    gap_ids: tuple[str, ...]


class AnalystResult(RecordModel):
    status: AnalystStatus = AnalystStatus.NOT_REQUESTED
    configuration: AnalystConfiguration | None = None
    assessments: tuple[AnalystAssessment, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def status_matches_payload(self) -> "AnalystResult":
        failed = self.status in {
            AnalystStatus.UNAVAILABLE,
            AnalystStatus.INVALID_RESPONSE,
        }
        if failed and not self.reason:
            raise ValueError("An unavailable or invalid analyst result needs a reason.")
        if self.status is AnalystStatus.NOT_REQUESTED and (
            self.configuration is not None or self.assessments or self.reason is not None
        ):
            raise ValueError("A not-requested analyst result cannot contain output.")
        if self.status is not AnalystStatus.NOT_REQUESTED and self.configuration is None:
            raise ValueError("A requested analyst result needs its configuration.")
        if failed and self.assessments:
            raise ValueError("A failed analyst result cannot contain assessments.")
        return self


def validate_analyst_response(
    text: str, expected_gap_ids: set[str]
) -> tuple[AnalystAssessment, ...]:
    """Validate structured advice and require exactly one assessment per supplied gap."""

    response = AnalystResponse.model_validate_json(text)
    received = [assessment.gap_id for assessment in response.assessments]
    if len(received) != len(set(received)):
        raise ValueError("The analyst returned a gap more than once.")
    unknown = set(received) - expected_gap_ids
    missing = expected_gap_ids - set(received)
    if unknown:
        raise ValueError(f"The analyst returned unknown gaps: {', '.join(sorted(unknown))}.")
    if missing:
        raise ValueError(f"The analyst omitted gaps: {', '.join(sorted(missing))}.")
    return response.assessments


_NON_STRUCTURAL_PLAN_FIELDS = {
    "estimated_rows",
    "startup_cost",
    "total_cost",
    "actual_rows",
    "actual_loops",
    "actual_total_time_ms",
    "planning_time_ms",
    "execution_time_ms",
}


def _structural_plan(plan: PlanNode) -> dict[str, object]:
    """Return plan structure without costs or measured fields distorted by hints."""

    def strip(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: strip(item)
                for key, item in value.items()
                if key not in _NON_STRUCTURAL_PLAN_FIELDS
            }
        if isinstance(value, list):
            return [strip(item) for item in value]
        if isinstance(value, tuple):
            return [strip(item) for item in value]
        return value

    return strip(plan.model_dump(exclude_none=True, exclude_defaults=True))  # type: ignore[return-value]


def build_analyst_request(run: RunRecord) -> AnalystRequest | None:
    """Render one fresh-conversation request containing every gap in the run."""

    evaluation = evaluation_from_run(run)
    gaps = evaluation.realizability.gaps
    if not gaps:
        return None
    forcing = run.evidence.forcing
    assert run.database is not None
    assert run.evidence.postgres_plan is not None
    assert run.evidence.agent_plan is not None
    assert forcing is not None and forcing.realized_plan is not None

    gap_rows = [
        {
            "gap_id": str(gap.decision),
            "requested": gap.a.value if gap.a else None,
            "realized": gap.b.value if gap.b else None,
        }
        for gap in gaps
    ]
    related = [
        {
            "comparison": label,
            "decision": str(difference.decision),
            "left": difference.a.value if difference.a else None,
            "right": difference.b.value if difference.b else None,
        }
        for label, differences in (
            ("P_vs_A", evaluation.proposed.differences),
            ("P_vs_R", evaluation.realized.differences),
        )
        for difference in differences
    ]
    payload = {
        "query": run.evidence.rewritten_sql,
        "database_description": run.database.description,
        "validation_indexes": run.database.indexes,
        "plans": {
            "P_postgresql": _structural_plan(run.evidence.postgres_plan),
            "A_agent": _structural_plan(run.evidence.agent_plan),
            "R_realized": _structural_plan(forcing.realized_plan),
        },
        "realization_gaps": gap_rows,
        "selected_forcing": {
            "hints": forcing.hints,
            "switch_offs": forcing.switch_offs,
            "leading": forcing.leading,
        },
        "related_differences": related,
        "known_limits": [
            "pg_hint_plan cannot force every PostgreSQL operator or rewrite",
            "hints may not reach repeated or generated aliases",
            "Leading hints cannot express every join-side arrangement",
            "the selected outcome is the candidate with the fewest realization gaps",
        ],
    }
    schema = json.dumps(AnalystResponse.model_json_schema(), separators=(",", ":"))
    system = (
        "You are an advisory PostgreSQL plan analyst. Suggest a likely cause for every "
        "supplied realization gap using only the evidence. Use unknown when evidence is "
        "insufficient. Your suggestions never change deterministic metrics. Return only "
        f"JSON matching this schema: {schema}"
    )
    return AnalystRequest(
        system=system,
        user=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        gap_ids=tuple(row["gap_id"] for row in gap_rows),
    )


def run_analyst(client: anthropic.Anthropic, run: RunRecord) -> AnalystResult:
    """Make at most one advisory model call for all gaps in a run."""

    request = build_analyst_request(run)
    if request is None:
        return AnalystResult(
            status=AnalystStatus.COMPLETED,
            configuration=AnalystConfiguration(),
        )
    response = client.messages.create(
        model=ANALYST_MODEL,
        max_tokens=ANALYST_MAX_TOKENS,
        system=request.system,
        messages=[{"role": "user", "content": request.user}],
    )
    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("The analyst declined the request.")
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise RuntimeError("The analyst response was cut off at max_tokens.")
    text = "".join(
        block.text
        for block in response.content
        if isinstance(block, anthropic.types.TextBlock)
    )
    assessments = validate_analyst_response(text, set(request.gap_ids))
    return AnalystResult(
        status=AnalystStatus.COMPLETED,
        configuration=AnalystConfiguration(),
        assessments=assessments,
    )


def advisory_result(client: anthropic.Anthropic, run: RunRecord) -> AnalystResult:
    """Contain analyst failures so deterministic evidence and metrics remain valid."""

    try:
        return run_analyst(client, run)
    except (ValueError, ValidationError) as error:
        return AnalystResult(
            status=AnalystStatus.INVALID_RESPONSE,
            configuration=AnalystConfiguration(),
            reason=str(error),
        )
    except (anthropic.AnthropicError, RuntimeError) as error:
        return AnalystResult(
            status=AnalystStatus.UNAVAILABLE,
            configuration=AnalystConfiguration(),
            reason=str(error),
        )


MetricName = Literal[
    "proposed_difference",
    "realizability",
    "realized_difference",
    "intended_realization",
]


class MetricAggregate(RecordModel):
    metric: MetricName
    per_query: dict[str, float]
    macro_rate: float | None
    micro_numerator: int
    micro_denominator: int
    micro_rate: float | None
    total_runs: int
    defined_runs: int
    undefined_runs: int
    unavailable_runs: int
    failed_runs: int
    included_queries: int
    excluded_queries: int
    exclusion_reasons: dict[str, int]


class TimingSummary(RecordModel):
    per_query_median_ratio: dict[str, float]
    total_runs: int
    defined_runs: int
    timed_out_runs: int
    missing_runs: int


class WorkloadSummary(RecordModel):
    metrics: dict[MetricName, MetricAggregate]
    timing: TimingSummary


class RunEvidence(RecordModel):
    original_sql: str
    rewritten_sql: str
    postgres_plan: PlanNode | None = None
    agent_plan: PlanNode | None = None
    agent_attempts: tuple[PlannerAttempt, ...] = ()
    forcing: ForcingEvidence | None = None
    timing: TimingEvidence | None = None


class RunRecord(RecordModel):
    identity: RunIdentity
    provenance: RunProvenance
    planner: PlannerConfiguration
    measurement: MeasurementConfiguration
    database: DatabaseContext | None = None
    evidence: RunEvidence
    status: RunStatus
    failure_stage: str | None = None
    failure_reason: str | None = None
    analyst: AnalystResult = AnalystResult()

    @model_validator(mode="after")
    def status_matches_evidence(self) -> "RunRecord":
        if self.status is RunStatus.FAILED and (
            not self.failure_stage or not self.failure_reason
        ):
            raise ValueError("A failed run needs a failure stage and reason.")
        if self.status is RunStatus.COMPLETED and (
            self.failure_stage is not None or self.failure_reason is not None
        ):
            raise ValueError("A completed run cannot carry a pipeline failure.")
        if self.status is RunStatus.COMPLETED:
            forcing = self.evidence.forcing
            required = {
                "database context": self.database,
                "PostgreSQL plan": self.evidence.postgres_plan,
                "agent plan": self.evidence.agent_plan,
                "planner attempts": self.evidence.agent_attempts or None,
                "realized plan": forcing.realized_plan if forcing else None,
                "timing evidence": self.evidence.timing,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(
                    "A completed run is missing " + ", ".join(missing) + "."
                )
        return self


class IncompleteRunError(ValueError):
    """A stored run lacks the raw evidence required for deterministic evaluation."""


def write_run(run: RunRecord, root: Path = Path("agent_runs")) -> Path:
    """Atomically create one JSON run file without replacing an existing run."""

    directory = root / run.identity.experiment_id
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{run.identity.run_id}.json"
    payload = run.model_dump_json(indent=2).encode("utf-8") + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{run.identity.run_id}.", suffix=".tmp", dir=directory,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        # A hard link publishes the complete bytes atomically and fails if another run
        # already owns this identifier. The temporary name is then discarded.
        os.link(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_run(path: Path) -> RunRecord:
    """Load and validate one versioned run record."""

    return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))


def evaluation_json(evaluation: PlanEvaluation) -> str:
    """Serialize recomputed metric detail through Pydantic's public boundary."""

    return _EVALUATION_ADAPTER.dump_json(evaluation).decode("utf-8")


def load_evaluation_json(value: str) -> PlanEvaluation:
    """Validate serialized recomputed metric detail for downstream consumers."""

    return _EVALUATION_ADAPTER.validate_json(value)


def capture_git_provenance(repository: Path) -> RunProvenance:
    """Record the checked-out commit and whether tracked or untracked files differ."""

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    return RunProvenance(
        git_commit=git("rev-parse", "HEAD"),
        git_dirty=bool(git("status", "--porcelain", "--untracked-files=normal")),
    )


def planner_configuration() -> PlannerConfiguration:
    """Snapshot material planner request values, including the exact prompt hash."""

    prompt_hash = sha256(agent_system_prompt().encode("utf-8")).hexdigest()
    return PlannerConfiguration(
        model=AGENT_MODEL,
        prompt_sha256=prompt_hash,
        max_tokens=AGENT_MAX_TOKENS,
        first_effort=AGENT_FIRST_EFFORT,
        retry_effort=AGENT_RETRY_EFFORT,
        max_attempts=AGENT_MAX_ATTEMPTS,
    )


def database_context(
    connection: object, description: DatabaseDescription
) -> DatabaseContext:
    """Preserve exactly what the planner saw plus its validation-only index map."""

    info = getattr(connection, "info")
    version = (info.parameter_status("server_version") or "unknown").split(" ")[0]
    return DatabaseContext(
        postgres_version=version,
        description=description.text,
        indexes={table: tuple(sorted(names)) for table, names in description.indexes.items()},
    )


def gap_evidence(gap: Difference) -> GapEvidence:
    """Copy one realization gap into the persistence boundary."""

    return GapEvidence(
        gap_id=str(gap.decision),
        kind=gap.decision.kind,
        tables=gap.decision.tables,
        requested=gap.a.value if gap.a else None,
        realized=gap.b.value if gap.b else None,
    )


def forcing_evidence(outcome: ForcePlanResult | ForcingFailure) -> ForcingEvidence:
    """Copy only the selected forcing outcome, or its terminal failure."""

    if isinstance(outcome, ForcingFailure):
        return ForcingEvidence(failure_reason=outcome.reason)
    return ForcingEvidence(
        realized_plan=outcome.plan,
        hints=outcome.hints,
        gaps=tuple(gap_evidence(gap) for gap in outcome.gaps),
        switch_offs=outcome.switch_offs,
        leading=outcome.leading,
    )


def evaluation_from_run(run: RunRecord) -> PlanEvaluation:
    """Recompute deterministic metrics from raw stored plans and forcing evidence."""

    postgres = run.evidence.postgres_plan
    agent = run.evidence.agent_plan
    forcing = run.evidence.forcing
    if postgres is None or agent is None or forcing is None:
        raise IncompleteRunError("The run has no complete P/A/forcing evidence.")

    if forcing.realized_plan is None:
        outcome: ForcePlanResult | ForcingFailure = ForcingFailure(
            forcing.failure_reason or "Forcing failed without a recorded reason."
        )
    else:
        outcome = ForcePlanResult(
            hints=forcing.hints or "",
            plan=forcing.realized_plan,
            gaps=tuple(realization_gaps(agent, forcing.realized_plan)),
            switch_offs=bool(forcing.switch_offs),
            leading=bool(forcing.leading),
        )
    return evaluate_plans(postgres, agent, outcome)


def _metric_scores(evaluation: PlanEvaluation) -> dict[MetricName, MetricScore]:
    return {
        "proposed_difference": evaluation.proposed.all_kinds.difference,
        "realizability": evaluation.realizability.score,
        "realized_difference": evaluation.realized.all_kinds.difference,
        "intended_realization": evaluation.intended,
    }


def aggregate_runs(runs: list[RunRecord]) -> WorkloadSummary:
    """Recompute and aggregate count metrics; timing remains a per-query summary."""

    names: tuple[MetricName, ...] = (
        "proposed_difference",
        "realizability",
        "realized_difference",
        "intended_realization",
    )
    scores: dict[MetricName, list[tuple[RunRecord, MetricScore]]] = {
        name: [] for name in names
    }
    incomplete: list[tuple[RunRecord, str]] = []
    for run in runs:
        try:
            evaluation = evaluation_from_run(run)
        except IncompleteRunError as error:
            incomplete.append((run, str(error)))
            continue
        for name, score in _metric_scores(evaluation).items():
            scores[name].append((run, score))

    query_names = {run.identity.query_name for run in runs}
    aggregates: dict[MetricName, MetricAggregate] = {}
    for name in names:
        per_query_samples: dict[str, list[float]] = defaultdict(list)
        numerator = denominator = 0
        undefined = unavailable = 0
        reasons: Counter[str] = Counter(reason for _run, reason in incomplete)
        for run, score in scores[name]:
            if score.status is MetricStatus.DEFINED:
                assert score.rate is not None
                assert score.numerator is not None and score.denominator is not None
                per_query_samples[run.identity.query_name].append(score.rate)
                numerator += score.numerator
                denominator += score.denominator
            elif score.status is MetricStatus.NOT_APPLICABLE:
                undefined += 1
                reasons[score.explanation or "not applicable"] += 1
            else:
                unavailable += 1
                reasons[score.explanation or "unavailable"] += 1

        per_query = {
            query: fmean(values) for query, values in sorted(per_query_samples.items())
        }
        defined = sum(len(values) for values in per_query_samples.values())
        aggregates[name] = MetricAggregate(
            metric=name,
            per_query=per_query,
            macro_rate=fmean(per_query.values()) if per_query else None,
            micro_numerator=numerator,
            micro_denominator=denominator,
            micro_rate=numerator / denominator if denominator else None,
            total_runs=len(runs),
            defined_runs=defined,
            undefined_runs=undefined,
            unavailable_runs=unavailable + len(incomplete),
            failed_runs=sum(run.status is RunStatus.FAILED for run in runs),
            included_queries=len(per_query),
            excluded_queries=len(query_names - per_query.keys()),
            exclusion_reasons=dict(sorted(reasons.items())),
        )

    ratios: dict[str, list[float]] = defaultdict(list)
    timed_out = missing = 0
    for run in runs:
        timing = run.evidence.timing
        if timing is None:
            missing += 1
        elif timing.postgres_timed_out or timing.realized_timed_out:
            timed_out += 1
        elif (
            timing.postgres_ms is None
            or timing.realized_ms is None
            or timing.postgres_ms <= 0
        ):
            missing += 1
        else:
            ratios[run.identity.query_name].append(
                timing.realized_ms / timing.postgres_ms
            )
    timing_summary = TimingSummary(
        per_query_median_ratio={
            query: median(values) for query, values in sorted(ratios.items())
        },
        total_runs=len(runs),
        defined_runs=sum(len(values) for values in ratios.values()),
        timed_out_runs=timed_out,
        missing_runs=missing,
    )
    return WorkloadSummary(metrics=aggregates, timing=timing_summary)


def measure_execution_time(
    connection: psycopg.Connection,
    query: str,
    hints: str = "",
    timeout_ms: int = 60_000,
) -> float | None:
    """Warm once, measure once, and return None when statement timeout is reached."""

    connection.execute(f"SET statement_timeout = {int(timeout_ms)}".encode())
    try:
        for _ in range(2):
            plan = get_postgres_plan(connection, query, analyze=True, hints=hints)
    except psycopg.errors.QueryCanceled:
        connection.rollback()
        return None
    finally:
        connection.execute(b"RESET statement_timeout")
    return plan.execution_time_ms


def collect_run(
    connection: psycopg.Connection,
    *,
    experiment_id: str,
    query_name: str,
    sample: int,
    sql: str,
    repository: Path = Path("."),
    output_root: Path = Path("agent_runs"),
    client: anthropic.Anthropic | None = None,
    analyst_client: anthropic.Anthropic | None = None,
    run_id: str | None = None,
    timeout_ms: int = 60_000,
) -> RunRecord:
    """Execute one query sample, persist all available evidence, and return its run."""

    identity = RunIdentity(
        experiment_id=experiment_id,
        run_id=run_id or uuid4().hex,
        query_name=query_name,
        sample=sample,
        created_at=datetime.now(UTC),
    )
    provenance = capture_git_provenance(repository)
    planner = planner_configuration()
    measurement = MeasurementConfiguration(
        timeout_ms=timeout_ms, warmup_runs=1, measured_runs=1
    )
    rewritten = unique_aliases(sql)
    database: DatabaseContext | None = None
    postgres_plan: PlanNode | None = None
    agent_plan: PlanNode | None = None
    attempts: tuple[PlannerAttempt, ...] = ()
    forcing: ForcingEvidence | None = None
    timing: TimingEvidence | None = None
    analyst = AnalystResult()

    def make_record(
        status: RunStatus,
        stage: str | None = None,
        reason: str | None = None,
    ) -> RunRecord:
        return RunRecord(
            identity=identity,
            provenance=provenance,
            planner=planner,
            measurement=measurement,
            database=database,
            evidence=RunEvidence(
                original_sql=sql,
                rewritten_sql=rewritten,
                postgres_plan=postgres_plan,
                agent_plan=agent_plan,
                agent_attempts=attempts,
                forcing=forcing,
                timing=timing,
            ),
            status=status,
            failure_stage=stage,
            failure_reason=reason,
            analyst=analyst,
        )

    def finish(
        status: RunStatus,
        stage: str | None = None,
        reason: str | None = None,
    ) -> RunRecord:
        run = make_record(status, stage, reason)
        write_run(run, output_root)
        return run

    try:
        description = describe_database(connection, rewritten)
        database = database_context(connection, description)
        postgres_plan = get_postgres_plan(connection, rewritten)
    except psycopg.Error as error:
        connection.rollback()
        return finish(RunStatus.FAILED, "postgres_plan", str(error))
    except Exception as error:
        return finish(RunStatus.FAILED, "postgres_plan", str(error))

    try:
        if client is None:
            client = anthropic.Anthropic(api_key=api_key())
        result: AgentResult = ask_agent(client, rewritten, description)
        agent_plan = result.plan
        attempts = tuple(
            PlannerAttempt(
                response=attempt.response,
                validation_problems=attempt.validation_problems,
            )
            for attempt in result.attempts
        )
    except AgentPlanError as error:
        attempts = tuple(
            PlannerAttempt(
                response=attempt.response,
                validation_problems=attempt.validation_problems,
            )
            for attempt in error.attempts
        )
        return finish(RunStatus.FAILED, "agent_plan", str(error))
    except (AgentRequestError, AgentResponseError) as error:
        attempts = tuple(
            PlannerAttempt(
                response=attempt.response,
                validation_problems=attempt.validation_problems,
            )
            for attempt in error.attempts
        )
        return finish(RunStatus.FAILED, "agent_plan", str(error))
    except (anthropic.AnthropicError, RuntimeError) as error:
        return finish(RunStatus.FAILED, "agent_plan", str(error))

    try:
        outcome: ForcePlanResult | ForcingFailure = force_plan(
            connection, rewritten, agent_plan
        )
    except ValueError as error:
        outcome = ForcingFailure(str(error))
    except (psycopg.errors.UndefinedFile, psycopg.errors.InsufficientPrivilege) as error:
        connection.rollback()
        outcome = ForcingFailure(f"pg_hint_plan cannot be loaded: {error}")
    except psycopg.Error as error:
        connection.rollback()
        outcome = ForcingFailure(f"PostgreSQL forcing failed: {error}")
    forcing = forcing_evidence(outcome)
    if isinstance(outcome, ForcingFailure):
        return finish(RunStatus.FAILED, "forcing", outcome.reason)

    try:
        postgres_ms = measure_execution_time(connection, rewritten, timeout_ms=timeout_ms)
        timing = TimingEvidence(
            postgres_ms=postgres_ms,
            postgres_timed_out=postgres_ms is None,
        )
        realized_ms = measure_execution_time(
            connection, rewritten, outcome.hints, timeout_ms=timeout_ms
        )
    except psycopg.Error as error:
        connection.rollback()
        return finish(RunStatus.FAILED, "timing", str(error))
    except RuntimeError as error:
        return finish(RunStatus.FAILED, "timing", str(error))
    timing = TimingEvidence(
        postgres_ms=postgres_ms,
        realized_ms=realized_ms,
        postgres_timed_out=postgres_ms is None,
        realized_timed_out=realized_ms is None,
    )
    if analyst_client is not None:
        analyst = advisory_result(analyst_client, make_record(RunStatus.COMPLETED))
    return finish(RunStatus.COMPLETED)


def collect_experiment(
    connection: psycopg.Connection,
    *,
    experiment_id: str,
    queries: dict[str, str],
    samples: int,
    repository: Path = Path("."),
    output_root: Path = Path("agent_runs"),
    client_factory: Callable[[], anthropic.Anthropic],
    analyst_client_factory: Callable[[], anthropic.Anthropic] | None = None,
    timeout_ms: int = 60_000,
) -> list[RunRecord]:
    """Collect every named query/sample pair as a distinct run."""

    if samples < 1:
        raise ValueError("samples must be at least 1")
    runs: list[RunRecord] = []
    for query_name, sql in queries.items():
        for sample in range(1, samples + 1):
            runs.append(
                collect_run(
                    connection,
                    experiment_id=experiment_id,
                    query_name=query_name,
                    sample=sample,
                    sql=sql,
                    repository=repository,
                    output_root=output_root,
                    client=client_factory(),
                    analyst_client=(
                        analyst_client_factory() if analyst_client_factory else None
                    ),
                    timeout_ms=timeout_ms,
                )
            )
    return runs


def load_experiment(
    experiment_id: str, root: Path = Path("agent_runs")
) -> list[RunRecord]:
    """Load all stored runs for an experiment in stable path order."""

    directory = root / experiment_id
    return [load_run(path) for path in sorted(directory.glob("*.json"))]
