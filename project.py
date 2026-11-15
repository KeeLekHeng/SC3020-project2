"""
Entry point. python project.py opens the GUI in interface.py; python project.py
--experiments runs every query in queries/ and saves the runs to
agent_runs/<experiment>.jsonl. Run records, their summary and the advisory analyst live
here too.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from getpass import getpass
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from statistics import fmean
from typing import Any, Literal, NamedTuple, get_args

import anthropic
import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from comparison import (
    ForcePlanResult,
    ForcingFailure,
    MetricScore,
    MetricStatus,
    PlanEvaluation,
    evaluate_plans,
    is_parallel,
    realization_gaps,
    try_force_plan,
)
from preprocessing import (
    AGENT_MAX_ATTEMPTS,
    AGENT_MODEL,
    AgentAttempt,
    AgentError,
    AgentPlanError,
    PlanNode,
    agent_system_prompt,
    api_key,
    ask_agent,
    describe_database,
    get_postgres_plan,
    unique_aliases,
)

PROJECT_FOLDER = Path(__file__).resolve().parent

CONNECTION_SETTINGS: dict[str, Any] = {
    "host": "localhost",
    "port": 5432,
    "dbname": "tpch",
    "user": "postgres",
    "connect_timeout": 10,
}


# The analyst is the same model as the agent; docs/experiments.md notes the bias.
ANALYST_MODEL = AGENT_MODEL
ANALYST_MAX_TOKENS = 4_096
TIMING_RUNS = 3
TIMING_TIMEOUT_MS = 60_000
# An experiment's file name: the UTC time it started.
EXPERIMENT_ID_FORMAT = "%Y-%m-%d_%H-%M-%SZ"
AGENT_RUNS = PROJECT_FOLDER / "agent_runs"


class RecordModel(BaseModel):
    """Strict immutable JSON boundary used by all experiment records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# Where a run stopped: PostgreSQL's plan, no usable agent plan, a failed API request to
# the agent, forcing, or timing.
FailureStage = Literal["postgres_plan", "agent_plan", "agent_request", "forcing", "timing"]


class AnalystAssessment(RecordModel):
    """The analyst's suggested cause for one realization gap."""

    gap_id: str = Field(min_length=1)
    cause: Literal["forcing_limitation", "infeasible_request", "postgresql_rewrite", "unknown"]
    explanation: str = Field(min_length=1)
    evidence: tuple[str, ...] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]


class AnalystResponse(RecordModel):
    assessments: tuple[AnalystAssessment, ...]


class AnalystRequest(NamedTuple):
    system: str
    user: str
    gap_ids: tuple[str, ...]


class AnalystResult(RecordModel):
    """
    The analyst's advice for a run. It never changes the metrics. unavailable and
    invalid_response carry the reason.
    """

    status: Literal["not_requested", "completed", "unavailable", "invalid_response"] = (
        "not_requested"
    )
    assessments: tuple[AnalystAssessment, ...] = ()
    reason: str | None = None


class Provenance(RecordModel):
    """What produced a run, so runs from different commits or prompts are not mixed."""

    # None outside a git checkout, such as an unpacked submission.
    git_commit: str | None
    agent_model: str
    prompt_sha256: str


class Forcing(RecordModel):
    """The forcing try force_plan() selected: the plan PostgreSQL chose with its hints."""

    realized_plan: PlanNode
    hints: str
    switch_offs: bool
    leading: bool


class RunRecord(RecordModel):
    """
    One query's run, saved as one line of agent_runs/<experiment>.jsonl. Each stage of
    collect_run() fills in its fields, so a failed run keeps everything before the
    failure. Unlike the other records it is not frozen, so collect_run() can fill it in
    place; every assignment is validated.
    """

    model_config = ConfigDict(frozen=False, validate_assignment=True)

    schema_version: Literal[3] = 3
    query_name: str
    created_at: datetime
    provenance: Provenance
    original_sql: str
    rewritten_sql: str
    # What the agent was told about the database, from describe_database().
    database_description: str | None = None
    postgres_plan: PlanNode | None = None
    agent_plan: PlanNode | None = None
    agent_attempts: tuple[AgentAttempt, ...] = ()
    # None unless forcing succeeded.
    forcing: Forcing | None = None
    # Mean milliseconds of each plan; None if it was not timed or passed the timeout.
    postgres_ms: float | None = None
    realized_ms: float | None = None
    # Both None for a completed run; otherwise the stage that failed and why.
    failure_stage: FailureStage | None = None
    failure_reason: str | None = None
    analyst: AnalystResult = AnalystResult()


def build_analyst_request(run: RunRecord) -> AnalystRequest | None:
    """One request covering every realization gap in the run; None if there is none."""

    if run.agent_plan is None or run.forcing is None:
        return None
    # Every forcing gap, parallel ones included: the rates leave those out, but the
    # analyst explains what forcing did not reproduce.
    gaps = realization_gaps(run.agent_plan, run.forcing.realized_plan)
    if not gaps:
        return None
    assert run.postgres_plan is not None

    # Costs, row counts and times are left out of the plans the analyst sees: hints
    # distort them, and they would only invite the model to compare numbers.
    measured = {
        "estimated_rows", "startup_cost", "total_cost", "actual_rows", "actual_loops",
        "actual_total_time_ms", "planning_time_ms", "execution_time_ms",
    }

    def structure(value: object) -> object:
        if isinstance(value, dict):
            return {key: structure(item) for key, item in value.items() if key not in measured}
        if isinstance(value, (list, tuple)):
            return [structure(item) for item in value]
        return value

    def plan_structure(plan: PlanNode) -> object:
        return structure(plan.model_dump(exclude_none=True, exclude_defaults=True))

    payload = {
        "query": run.rewritten_sql,
        "database_description": run.database_description,
        "plans": {
            "P_postgresql": plan_structure(run.postgres_plan),
            "A_agent": plan_structure(run.agent_plan),
            "R_realized": plan_structure(run.forcing.realized_plan),
        },
        "realization_gaps": [
            {
                "gap_id": str(gap.decision),
                "requested": gap.a.value if gap.a else None,
                "realized": gap.b.value if gap.b else None,
            }
            for gap in gaps
        ],
        "selected_forcing": run.forcing.model_dump(include={"hints", "switch_offs", "leading"}),
        "known_limits": [
            "pg_hint_plan cannot force every PostgreSQL operator or rewrite",
            "hints may not reach repeated or generated aliases",
            "Leading hints cannot express every join-side arrangement",
            "of up to three forcing tries, the selected one missed the fewest requests",
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
        gap_ids=tuple(str(gap.decision) for gap in gaps),
    )


def run_analyst(client: anthropic.Anthropic, run: RunRecord) -> AnalystResult:
    """
    Make at most one advisory model call for all gaps in a run. Failures become a
    status, so the run and its metrics are never lost.
    """

    try:
        request = build_analyst_request(run)
        if request is None:
            return AnalystResult(status="completed")
        response = client.messages.create(
            model=ANALYST_MODEL,
            max_tokens=ANALYST_MAX_TOKENS,
            system=request.system,
            messages=[{"role": "user", "content": request.user}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("The analyst declined the request.")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("The analyst response was cut off at max_tokens.")
        text = "".join(
            block.text
            for block in response.content
            if isinstance(block, anthropic.types.TextBlock)
        )
        assessments = AnalystResponse.model_validate_json(text).assessments
        assessed = sorted(assessment.gap_id for assessment in assessments)
        if assessed != sorted(request.gap_ids):
            raise ValueError(
                f"The analyst must assess each gap exactly once: asked about "
                f"{sorted(request.gap_ids)}, answered {assessed}."
            )
    except (ValueError, ValidationError) as error:
        return AnalystResult(status="invalid_response", reason=str(error))
    except (anthropic.AnthropicError, RuntimeError) as error:
        return AnalystResult(status="unavailable", reason=str(error))
    return AnalystResult(status="completed", assessments=assessments)


def write_run(run: RunRecord, experiment_id: str, root: Path = AGENT_RUNS) -> None:
    """Append run as one line of the experiment's file, agent_runs/<experiment_id>.jsonl."""

    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{experiment_id}.jsonl").open("a", encoding="utf-8") as experiment:
        experiment.write(run.model_dump_json() + "\n")


def load_experiment(experiment_id: str, root: Path = AGENT_RUNS) -> list[RunRecord]:
    """Load an experiment's runs in the order they were saved."""

    lines = (root / f"{experiment_id}.jsonl").read_text(encoding="utf-8").splitlines()
    return [RunRecord.model_validate_json(line) for line in lines]


def delete_run(experiment_id: str, run: RunRecord, root: Path = AGENT_RUNS) -> None:
    """
    Remove run from the experiment's file, and the file once no run is left. The run is
    found by its creation time, not its position, so a page opened before another run
    was deleted still removes its own run.
    """

    path = root / f"{experiment_id}.jsonl"
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
        if RunRecord.model_validate_json(line).created_at != run.created_at
    ]
    if lines:
        path.write_text("".join(lines), encoding="utf-8")
    else:
        path.unlink()


def evaluation_from_run(run: RunRecord) -> PlanEvaluation | None:
    """
    Recompute the metrics from the stored plans, or None if the run stopped before the
    agent's plan was forced.
    """

    if run.postgres_plan is None or run.agent_plan is None:
        return None
    if run.forcing is None:
        # A run with an agent plan was always forced, so no forcing means it failed, and
        # failure_reason says why.
        outcome: ForcePlanResult | ForcingFailure = ForcingFailure(
            run.failure_reason or "Forcing failed without a recorded reason."
        )
    else:
        outcome = ForcePlanResult(
            hints=run.forcing.hints,
            plan=run.forcing.realized_plan,
            gaps=tuple(realization_gaps(run.agent_plan, run.forcing.realized_plan)),
            switch_offs=run.forcing.switch_offs,
            leading=run.forcing.leading,
        )
    return evaluate_plans(run.postgres_plan, run.agent_plan, outcome)


MetricName = Literal[
    "proposed_difference",
    "realizability",
    "realized_difference",
    "intended_realization",
]


class ParallelFlags(NamedTuple):
    """Whether each plan of a query runs in parallel; None when the run has no such plan."""

    postgres: bool | None
    agent: bool | None
    realized: bool | None


class QuerySummary(NamedTuple):
    """One query's row in the workload summary."""

    # None for a completed run.
    failure_stage: FailureStage | None
    # Answers the agent gave, out of AGENT_MAX_ATTEMPTS; None when the agent was never
    # reached or its API request failed.
    answers: int | None
    # The four rates; empty when the run stopped before the agent's plan was forced.
    scores: dict[MetricName, MetricScore]
    parallel: ParallelFlags
    # Mean realized-plan time over mean PostgreSQL-plan time; None unless both were timed.
    # The realized plan is the agent's plan as PostgreSQL ran it after forcing.
    time_ratio: float | None
    # Whether either plan passed the statement timeout.
    timed_out: bool


def summarise_run(run: RunRecord) -> QuerySummary:
    """A run's row in the workload summary, recomputed from its stored plans."""

    evaluation = evaluation_from_run(run)
    return QuerySummary(
        failure_stage=run.failure_stage,
        answers=(
            len(run.agent_attempts)
            if run.agent_attempts and run.failure_stage != "agent_request"
            else None
        ),
        scores={} if evaluation is None else {
            "proposed_difference": evaluation.proposed.difference,
            "realizability": evaluation.realizability.score,
            "realized_difference": evaluation.realized.difference,
            "intended_realization": evaluation.intended,
        },
        parallel=ParallelFlags(*(
            None if plan is None else is_parallel(plan)
            for plan in (
                run.postgres_plan,
                run.agent_plan,
                run.forcing.realized_plan if run.forcing else None,
            )
        )),
        time_ratio=(
            run.realized_ms / run.postgres_ms
            if run.postgres_ms and run.realized_ms is not None
            else None
        ),
        # A completed run times both plans, so a missing time means a timeout.
        timed_out=run.failure_stage is None
        and (run.postgres_ms is None or run.realized_ms is None),
    )


def summarise_workload(runs: list[RunRecord]) -> dict[str, QuerySummary]:
    """One row per query, recomputed from the stored runs."""

    return {run.query_name: summarise_run(run) for run in runs}


def execution_time(
    connection: psycopg.Connection,
    query: str,
    hints: str = "",
    timeout_ms: int = TIMING_TIMEOUT_MS,
) -> float | None:
    """
    Mean milliseconds of TIMING_RUNS runs after one warm-up run, or None when any run
    passes timeout_ms, since a bad forced plan can run for hours.
    """
    connection.execute(f"SET statement_timeout = {int(timeout_ms)}".encode())
    try:
        get_postgres_plan(connection, query, analyze=True, hints=hints)
        times = [
            get_postgres_plan(connection, query, analyze=True, hints=hints).execution_time_ms
            for _ in range(TIMING_RUNS)
        ]
    except psycopg.errors.QueryCanceled:
        connection.rollback()
        return None
    finally:
        connection.execute(b"RESET statement_timeout")
    if None in times:
        raise RuntimeError("EXPLAIN ANALYZE reported no execution time.")
    return fmean(time for time in times if time is not None)


def collect_run(
    connection: psycopg.Connection,
    *,
    query_name: str,
    sql: str,
    client: anthropic.Anthropic | None = None,
    analyst_client: anthropic.Anthropic | None = None,
) -> RunRecord:
    """
    Run the pipeline on one query: PostgreSQL's plan, the agent's plan, forcing, timing
    and, with analyst_client, the analyst. The run is returned however far it got; the
    caller decides whether to save it with write_run().
    """

    try:
        git_commit: str | None = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_FOLDER, check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        git_commit = None
    rewritten = unique_aliases(sql)
    run = RunRecord(
        query_name=query_name,
        created_at=datetime.now(UTC),
        provenance=Provenance(
            git_commit=git_commit,
            agent_model=AGENT_MODEL,
            prompt_sha256=sha256(agent_system_prompt().encode("utf-8")).hexdigest(),
        ),
        original_sql=sql,
        rewritten_sql=rewritten,
    )

    def finish(stage: FailureStage | None = None, reason: str | None = None) -> RunRecord:
        run.failure_stage, run.failure_reason = stage, reason
        return run

    try:
        description = describe_database(connection, rewritten)
        run.database_description = description.text
        run.postgres_plan = get_postgres_plan(connection, rewritten)
    except Exception as error:
        if isinstance(error, psycopg.Error):
            connection.rollback()
        return finish("postgres_plan", str(error))

    try:
        if client is None:
            client = anthropic.Anthropic(api_key=api_key())
        result = ask_agent(client, rewritten, description)
        run.agent_plan, run.agent_attempts = result.plan, result.attempts
    except AgentPlanError as error:
        run.agent_attempts = error.attempts
        return finish("agent_plan", str(error))
    except AgentError as error:
        run.agent_attempts = error.attempts
        return finish("agent_request", str(error))
    except (anthropic.AnthropicError, RuntimeError) as error:
        return finish("agent_request", str(error))

    outcome = try_force_plan(connection, rewritten, result.plan)
    if isinstance(outcome, ForcingFailure):
        return finish("forcing", outcome.reason)
    run.forcing = Forcing(
        realized_plan=outcome.plan,
        hints=outcome.hints,
        switch_offs=outcome.switch_offs,
        leading=outcome.leading,
    )

    try:
        run.postgres_ms = execution_time(connection, rewritten)
        run.realized_ms = execution_time(connection, rewritten, outcome.hints)
    except psycopg.Error as error:
        connection.rollback()
        return finish("timing", str(error))
    except RuntimeError as error:
        return finish("timing", str(error))

    if analyst_client is not None:
        run.analyst = run_analyst(analyst_client, run)
    return finish()


def run_experiments(connection: psycopg.Connection, queries: dict[str, str]) -> None:
    """
    Run each query once, save the runs to one experiment file, and print one row per query
    and the workload counts. The analyst explains any realization gaps.
    """
    client = anthropic.Anthropic(api_key=api_key())
    experiment_id = datetime.now(UTC).strftime(EXPERIMENT_ID_FORMAT)
    runs = []
    for query_name, sql in queries.items():
        run = collect_run(
            connection, query_name=query_name, sql=sql, client=client, analyst_client=client
        )
        # Saved one by one, so a crash part-way keeps the runs before it.
        write_run(run, experiment_id)
        runs.append(run)
    summary = summarise_workload(runs)

    def fraction(score: MetricScore | None) -> str:
        if score is None or score.status is not MetricStatus.DEFINED:
            return "N/A"
        return f"{score.numerator}/{score.denominator}"

    def flag(value: bool | None) -> str:
        return "-" if value is None else "yes" if value else "no"

    print(
        f"{'Query':<8}{'Status':<24}{'Answers':<9}{'Proposed':<10}{'Realizable':<12}"
        f"{'Realized':<10}{'Intended':<10}{'Parallel P/A/R':<16}Time ratio"
    )
    for query, row in summary.items():
        status = "completed" if row.failure_stage is None else f"failed: {row.failure_stage}"
        rates = [fraction(row.scores.get(name)) for name in get_args(MetricName)]
        ratio = (
            "timeout" if row.timed_out
            else "-" if row.time_ratio is None
            else f"{row.time_ratio:.2f}x"
        )
        print(
            f"{query:<8}{status:<24}{row.answers or '-'!s:<9}{rates[0]:<10}{rates[1]:<12}"
            f"{rates[2]:<10}{rates[3]:<10}{'/'.join(map(flag, row.parallel)):<16}{ratio}"
        )

    rows = summary.values()
    answered = [row for row in rows if row.answers is not None]
    usable = [row for row in answered if row.failure_stage != "agent_plan"]
    unusable = [query for query, row in summary.items() if row.failure_stage == "agent_plan"]
    print(f"\nSaved {len(runs)} run(s) to agent_runs/{experiment_id}.jsonl")
    print(
        f"Usable agent plans: {len(usable)}/{len(answered)}, "
        f"{sum(row.answers == 1 for row in usable)} on the first answer"
        + (f"; none for {', '.join(unusable)}" if unusable else "")
    )
    same = []
    for name, label in (("proposed_difference", "proposed"), ("realized_difference", "realized")):
        defined = [
            score
            for row in rows
            if (score := row.scores.get(name)) and score.status is MetricStatus.DEFINED
        ]
        same.append(f"{label} {sum(score.numerator == 0 for score in defined)}/{len(defined)}")
    print(f"Same plan as PostgreSQL, apart from parallelism: {', '.join(same)}")
    # Forcing ran for every run that got past the agent, and failed only at "forcing".
    tried = [
        row for row in rows
        if row.failure_stage not in ("postgres_plan", "agent_plan", "agent_request")
    ]
    print(f"Plans forced: {sum(row.failure_stage != 'forcing' for row in tried)}/{len(tried)}")
    counts = [
        f"{label} {sum(bool(value) for value in column)}/"
        f"{sum(value is not None for value in column)}"
        for label, column in zip(
            ("PostgreSQL", "agent", "realized"), zip(*(row.parallel for row in rows))
        )
    ]
    ignored = sum(
        row.parallel.realized is not None and row.parallel.agent != row.parallel.realized
        for row in rows
    )
    print(
        f"Parallel plans: {', '.join(counts)}; realized plan ignored the agent's choice "
        f"for {ignored}"
    )
    print(
        f"Time ratio: realized agent plan over PostgreSQL's, each the mean of {TIMING_RUNS} "
        f"runs after a warm-up; {sum(row.timed_out for row in rows)} timed out."
    )


def load_queries() -> dict[str, str]:
    """The workload in queries/, by file name, shorter names first so Q2 precedes Q10."""

    return {
        path.stem: path.read_text(encoding="utf-8").strip()
        for path in sorted(
            (PROJECT_FOLDER / "queries").glob("*.sql"),
            key=lambda path: (len(path.stem), path.stem),
        )
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiments",
        action="store_true",
        help="run every query in queries/ and save the runs to agent_runs/<start time>.jsonl",
    )
    parser.add_argument(
        "-q",
        "--queries",
        nargs="+",
        metavar="NAME",
        help="run only these queries, named by their file in queries/, such as Q1 Q6",
    )
    args = parser.parse_args(argv)
    if args.queries and not args.experiments:
        parser.error("-q/--queries needs --experiments")
    if not args.experiments:
        # Streamlit apps run only under its own runner, not as an imported module. Bound
        # to this machine: the GUI spends the API key and can delete runs.
        try:
            started = subprocess.run(
                [
                    sys.executable, "-m", "streamlit", "run",
                    str(PROJECT_FOLDER / "interface.py"), "--server.address", "localhost",
                ]
            )
        except KeyboardInterrupt:
            return
        sys.exit(started.returncode)

    queries = load_queries()
    if args.queries:
        unknown = [name for name in args.queries if name not in queries]
        if unknown:
            parser.error(f"no such query in queries/: {', '.join(unknown)}")
        queries = {name: queries[name] for name in args.queries}
    if api_key() is None:
        print("No ANTHROPIC_API_KEY is set (see the README), so experiments cannot run.")
        return

    password = getpass("PostgreSQL password: ")
    with psycopg.connect(**CONNECTION_SETTINGS, password=password) as connection:
        connection.read_only = True
        print("Connected to PostgreSQL!")
        run_experiments(connection, queries)


if __name__ == "__main__":
    main()
