"""
The GUI: run a query through the pipeline, pick any saved run from the history, and
show PostgreSQL's plan (P), the agent's plan (A) and the realized plan (R) side by side
with the metrics. Start it with python project.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import difflib
from html import escape
from itertools import count
import json
from typing import Any, NamedTuple

import anthropic
import psycopg
import sqlglot
from sqlglot.errors import SqlglotError
import streamlit as st

from comparison import Choice, Decision, MetricScore, MetricStatus, PlanEvaluation, decisions
from preprocessing import SEPARATE_PLANS, PlanNode, api_key
from project import (
    AGENT_RUNS,
    CONNECTION_SETTINGS,
    EXPERIMENT_ID_FORMAT,
    TIMING_RUNS,
    TIMING_TIMEOUT_MS,
    RunRecord,
    collect_run,
    delete_run,
    evaluation_from_run,
    load_experiment,
    load_queries,
    summarise_run,
    write_run,
)

DIFFERS_FILL = "#fdd9a8"
GAP_COLOR = "#c62828"
OUTLINE_COLOR = "#e8902c"


class Mark(NamedTuple):
    """How to draw one node: the numbers of its differing decisions and its styling."""

    numbers: tuple[int, ...] = ()
    # Its decision differs from P's (A and R only).
    filled: bool = False
    # It holds a decision that differs between the plans (P only, the baseline).
    outlined: bool = False
    # No other plan makes any of its differing decisions.
    dashed: bool = False
    # The agent asked for it and R did not keep it (A only).
    gap: bool = False
    # One hover line per differing decision.
    notes: tuple[str, ...] = ()


def mark_plans(
    plans: tuple[PlanNode | None, PlanNode | None, PlanNode | None],
    evaluation: PlanEvaluation,
) -> tuple[dict[Decision, int], list[dict[Decision, Choice]], tuple[dict[int, Mark], ...]]:
    """
    Number the decisions that differ between P, A and R, given as plans, and mark the
    nodes holding them. Decisions over the same tables share a number, so a join and
    its outer side, or a scan and its index, carry one. Numbers run top-down through
    A, then P, then R. Returns each decision's number, each plan's decisions, and per
    plan each marked node's Mark keyed by id(node).
    """
    found = [decisions(plan) if plan else {} for plan in plans]
    proposed = {d.decision for d in evaluation.proposed.differences}
    realized = {d.decision for d in evaluation.realized.differences}
    gaps = {d.decision for d in evaluation.realizability.gaps}
    # Intended realization counts disagreements the other two leave out, such as a
    # named index P's plan does not use, so they are numbered and listed too.
    differing = proposed | realized | gaps | set(evaluation.disagreements)

    numbers: dict[tuple[str, ...], int] = {}
    for index in (1, 0, 2):
        plan = plans[index]
        if plan is None:
            continue
        position = {id(node): place for place, node in enumerate(plan.walk())}
        for decision, _ in sorted(
            found[index].items(), key=lambda item: position[id(item[1].node)]
        ):
            if decision in differing:
                numbers.setdefault(decision.tables, len(numbers) + 1)
    numbered = {decision: numbers[decision.tables] for decision in differing}

    marks = []
    # P is the baseline, so nothing in it is filled; A and R are filled against it.
    for index, (plan_found, changed) in enumerate(zip(found, (set(), proposed, realized))):
        held: dict[int, list[Decision]] = {}
        for decision, choice in plan_found.items():
            if decision in differing:
                held.setdefault(id(choice.node), []).append(decision)
        marks.append({
            node: Mark(
                numbers=tuple(sorted({numbered[d] for d in held_decisions})),
                filled=any(d in changed for d in held_decisions),
                outlined=index == 0,
                dashed=all(
                    sum(d in other for other in found) == 1 for d in held_decisions
                ),
                gap=index == 1 and any(d in gaps for d in held_decisions),
                notes=tuple(
                    f"#{numbered[d]} {d}: "
                    + " · ".join(
                        f"{name} {other.get(d, '—')}" for name, other in zip("PAR", found)
                    )
                    + (" (not kept by R)" if index == 1 and d in gaps else "")
                    for d in sorted(held_decisions)
                ),
            )
            for node, held_decisions in held.items()
        })
    return numbered, found, tuple(marks)


def plans_dot(trees: Sequence[tuple[str, PlanNode | None, dict[int, Mark]]]) -> str:
    """
    Graphviz DOT drawing each (title, plan, marks) tree side by side in one figure, so
    all are drawn at one scale. marks maps a node's id() to its Mark: its numbers go on
    its first line, hovering it shows its notes, and its flags set the fill, an orange
    outline, a dashed border, or a red border with "not kept by R". Subqueries hang
    off dashed edges labelled with their name. A missing plan is drawn as a note.
    """
    lines = [
        "digraph {",
        "pad=0.15; nodesep=0.3; fontname=Helvetica",
        'node [shape=box, style="rounded,filled", fillcolor=white, fontname=Helvetica]',
    ]
    names = count()

    def add(node: PlanNode, marks: dict[int, Mark]) -> str:
        name = f"n{next(names)}"
        mark = marks.get(id(node), Mark())
        # A scan names its table and a join the tables below it, as its decisions do,
        # four to a line so wide joins stay narrow. Operators above the joins cover
        # every table, so naming them would only widen the tree.
        below = sorted(node.relations()) if len(node.inputs) == 2 else []
        table = (
            f"{node.relation} {node.alias}"
            if node.relation and node.alias and node.relation != node.alias
            else node.alias
            or "\n".join(", ".join(below[i : i + 4]) for i in range(0, len(below), 4))
        )
        title = node.node_type + "".join(f" #{n}" for n in mark.numbers)
        parts = [part for part in (title, table, node.index_name, node.strategy) if part]
        text = "\n".join(parts)
        if mark.gap:
            # An HTML label, so the last line can be red. Graphviz also reads backslash
            # escapes such as \N inside HTML labels, so backslashes are escaped too.
            label = (
                "<"
                + "<BR/>".join(
                    escape(part).replace("\\", "&#92;").replace("\n", "<BR/>")
                    for part in parts
                )
                + f'<BR/><FONT COLOR="{GAP_COLOR}"><B>not kept by R</B></FONT>>'
            )
        else:
            # ensure_ascii=False keeps non-ASCII text literal: DOT does not read \u.
            label = json.dumps(text, ensure_ascii=False)
        style = "rounded,filled,dashed" if mark.dashed else "rounded,filled"
        fill = DIFFERS_FILL if mark.filled else "white"
        border = (
            f', color="{GAP_COLOR}", penwidth=2' if mark.gap
            else f', color="{OUTLINE_COLOR}", penwidth=2' if mark.outlined
            else ""
        )
        # Without a tooltip, the browser shows the node's internal name on hover.
        tooltip = "\n".join(mark.notes) or text
        lines.append(
            f'{name} [label={label}, tooltip={json.dumps(tooltip, ensure_ascii=False)}, '
            f'style="{style}", fillcolor="{fill}"{border}]'
        )
        for child in node.children:
            child_name = add(child, marks)
            edge = (
                f" [style=dashed, label={json.dumps(child.subplan_name or '', ensure_ascii=False)}]"
                if child.parent_relationship in SEPARATE_PLANS
                else ""
            )
            lines.append(f"{name} -> {child_name}{edge}")
        return name

    for index, (title, plan, marks) in enumerate(trees):
        lines += [
            f"subgraph cluster{index} {{",
            # Streamlit's CSS fixes every label's font size, so a dark band, not a
            # larger font, makes the title stand out.
            f'label=<<TABLE BORDER="0" CELLPADDING="6" BGCOLOR="#31333f"><TR><TD>'
            f'<FONT COLOR="white"><B>{escape(title)}</B></FONT></TD></TR></TABLE>>; '
            'labeljust=l; margin=8; color="#bbbbbb"',
        ]
        if plan is None:
            lines.append(f'n{next(names)} [label="No plan for this run", shape=note]')
        else:
            add(plan, marks)
        lines.append("}")
    lines.append("}")
    return "\n".join(lines)


def pretty_sql(sql: str) -> str:
    """
    sql laid out over several lines for reading, or unchanged if sqlglot cannot parse
    it. Display only: the output rewrites literals, such as date '1995-03-15' as a CAST,
    and drops comments.
    """
    try:
        return sqlglot.transpile(sql, read="postgres", write="postgres", pretty=True)[0]
    except SqlglotError:
        return sql


def score_text(score: MetricScore) -> str:
    if score.status is MetricStatus.DEFINED:
        return f"{score.numerator}/{score.denominator} ({score.rate:.0%})"
    return "N/A" if score.status is MetricStatus.NOT_APPLICABLE else "unavailable"


def run_page_key(run: RunRecord) -> str:
    """A run's page URL, from its creation time, which deleting other runs never moves."""
    return f"run-{run.created_at:%Y%m%dT%H%M%S%fZ}"


def show_run(run: RunRecord, experiment_id: str) -> None:
    """Draw run, saved in the experiment's file, with a button to delete it."""
    title_column, delete_column = st.columns([5, 1], vertical_alignment="bottom")
    title_column.subheader(f"{run.query_name} · {run.created_at.astimezone():%Y-%m-%d %H:%M}")
    with delete_column.popover("Delete run", icon=":material/delete:", width="stretch"):
        st.write("Delete this run permanently? It cannot be recovered.")
        if st.button("Delete", type="primary"):
            delete_run(experiment_id, run)
            st.session_state.open_new = True
            st.rerun()
    st.markdown("**SQL Query**")
    original = pretty_sql(run.original_sql)
    st.code(original, language="sql", height=200)
    if run.rewritten_sql != run.original_sql:
        with st.expander("Rewritten SQL Query"):
            st.caption(
                "The query names a table more than once, so each repeat gets its own "
                "alias, letting hints and the comparison name every scan. All three "
                "plans were made from this version. Changed lines: - original, + rewrite."
            )
            diff = difflib.unified_diff(
                original.splitlines(), pretty_sql(run.rewritten_sql).splitlines(), n=2,
                lineterm="",
            )
            # The two header lines name no files, so they are dropped.
            st.code("\n".join(list(diff)[2:]), language="diff")
    if run.failure_stage:
        st.error(f"Stopped at {run.failure_stage}: {run.failure_reason}")

    summary = summarise_run(run)
    if summary.scores:
        metrics = (
            (
                "Proposed difference (P vs A)",
                summary.scores["proposed_difference"],
                "Decisions (scan, index, join, outer side, aggregate, sort, …) where A "
                "differs from P, over all decisions in either. Lower: A is closer to P.",
            ),
            (
                "Realizability (A vs R)",
                summary.scores["realizability"],
                "Of A's requested scans, indexes, joins and outer sides, the share R "
                "kept. Higher: forcing reproduced more of A.",
            ),
            (
                "Realized difference (P vs R)",
                summary.scores["realized_difference"],
                "Decisions where R differs from P, over all decisions in either. "
                "Lower: R is closer to P.",
            ),
            (
                "Intended realization",
                summary.scores["intended_realization"],
                "Of A's forceable choices that differ from P, the share R kept. "
                "Higher: more of the agent's changes took effect.",
            ),
        )
        for column, (label, score, definition) in zip(st.columns(4), metrics):
            column.metric(
                label,
                score_text(score),
                help=definition + (f"\n\n{score.explanation}" if score.explanation else ""),
            )

    timeout_s = TIMING_TIMEOUT_MS // 1000
    # A completed run without a time passed execution_time()'s timeout.
    missing = f"timed out (> {timeout_s} s)" if not run.failure_stage else "-"
    timed = (
        f"Mean of {TIMING_RUNS} EXPLAIN ANALYZE runs after a warm-up. Stopped after "
        f"{timeout_s} s."
    )
    timing = (
        (
            "PostgreSQL time (P)",
            missing if run.postgres_ms is None else f"{run.postgres_ms:.0f} ms",
            timed,
        ),
        (
            "Realized time (R)",
            missing if run.realized_ms is None else f"{run.realized_ms:.0f} ms",
            timed,
        ),
        (
            "Time ratio (R/P)",
            f"timed out (> {timeout_s} s)" if summary.timed_out
            else "-" if summary.time_ratio is None
            else f"{summary.time_ratio:.2f}x",
            "R time ÷ P time. Below 1x: R ran faster.",
        ),
        (
            "Parallel P / A / R",
            " / ".join(
                "-" if value is None else "yes" if value else "no"
                for value in summary.parallel
            ),
            "Whether each plan uses parallel workers. Not counted in the rates above.",
        ),
    )
    for column, (label, value, definition) in zip(st.columns(4), timing):
        column.metric(label, value, help=definition)
    if summary.timed_out:
        slow = " and ".join(
            name for name, ms in (("P", run.postgres_ms), ("R", run.realized_ms)) if ms is None
        )
        st.warning(
            f"{slow} ran longer than {timeout_s} s and was stopped, so its time and the "
            "time ratio are unknown."
        )
    flags = [value for value in summary.parallel if value is not None]
    if len(set(flags)) > 1:
        st.info(
            "The plans differ in running in parallel: "
            + ", ".join(
                f"{name} {'is' if value else 'is not'}"
                for name, value in zip("PAR", summary.parallel)
                if value is not None
            )
            + ". The rates and the marks leave parallelism out, but it can explain much "
            "of a difference in time."
        )

    realized = run.forcing.realized_plan if run.forcing else None
    plans = (run.postgres_plan, run.agent_plan, realized)
    evaluation = evaluation_from_run(run)
    numbered, found, marks = (
        mark_plans(plans, evaluation) if evaluation else ({}, [{}, {}, {}], ({}, {}, {}))
    )
    st.expander("How to Read the Trees").markdown(
        "- **Box**: one plan operator with the root at the top.\n"
        "- **Dashed edge**: leads to a subquery.\n"
        "- **#n**: a decision that differs, numbered as in the *Decisions that differ* "
        "table. The same number in two trees marks the nodes being compared.\n"
        "- **Orange fill** (A, R): this decision differs from PostgreSQL's plan (P).\n"
        "- **Orange outline** (P): PostgreSQL's side of a numbered decision.\n"
        "- **Dashed border**: only this plan has the decision, so it has no counterpart.\n"
        "- **Red border, *not kept by R***: the agent asked for this; the realized "
        "plan did not keep it.\n"
        "- **Hover** a box to see its decisions in P, A and R.\n\n"
        "Hints force only scans, indexes, joins and join sides, so an orange aggregate "
        "or sort is never *not kept*. Parallelism is not marked or counted."
    )
    st.graphviz_chart(
        plans_dot(
            tuple(
                zip(
                    ("PostgreSQL plan (P)", "Agent plan (A)", "Realized plan (R)"),
                    plans,
                    marks,
                )
            )
        ),
        width="content",
    )

    if evaluation and numbered:
        gap_decisions = {d.decision for d in evaluation.realizability.gaps}
        st.markdown("**Decisions that differ between P, A and R**")
        st.caption(
            "#: the number on the nodes above. —: the plan has no such decision. "
            "✗: the agent asked for it, R did not keep it."
        )
        st.dataframe(
            [
                {
                    "#": number,
                    "decision": str(decision),
                    **{
                        name: str(plan_found[decision]) if decision in plan_found else "—"
                        for name, plan_found in zip("PAR", found)
                    },
                    "not kept by R": "✗" if decision in gap_decisions else "",
                }
                for decision, number in sorted(
                    numbered.items(), key=lambda item: (item[1], item[0])
                )
            ],
            hide_index=True,
            height="content",
        )

    if run.forcing:
        with st.expander("pg_hint_plan hints"):
            st.caption(
                "The hints that produced R. Forcing tries up to three sets, with and "
                "without join order and planner switch-offs, and keeps the one whose "
                "plan missed the fewest requests, so a request marked *not kept by R* "
                "may have no hint here."
            )
            st.code(run.forcing.hints or "(none)", wrap_lines=True)
    # The analyst is only called when forcing missed a request, so most runs have none.
    if run.analyst.reason:
        st.warning(
            "The analyst's answer was invalid, so no explanations are shown."
            if run.analyst.status == "invalid_response"
            else "The analyst could not be reached, so no explanations are shown."
        )
        st.expander("Analyst error details").code(run.analyst.reason, wrap_lines=True)
    if run.analyst.assessments:
        with st.expander("Analyst: why R missed requests"):
            st.caption(
                "A second model call suggests a cause for each request R did not keep. "
                "It is advice from the agent's own model, not ground truth, and never "
                "changes the metrics."
            )
            for assessment in run.analyst.assessments:
                st.markdown(
                    f"**{assessment.gap_id}**: {assessment.cause.replace('_', ' ')} "
                    f"({assessment.confidence} confidence)\n\n{assessment.explanation}\n\n"
                    + "\n".join(f"- {item}" for item in assessment.evidence)
                )


def new_run_page() -> None:
    st.markdown(
        "Compare PostgreSQL's plan for a query with a plan proposed by an AI agent. "
        "Execute gets PostgreSQL's plan (**P**), asks the agent for its plan (**A**), "
        "forces A onto PostgreSQL with pg_hint_plan hints to get the realized plan "
        "(**R**), times P and R, and asks a second model call why R missed any of A's "
        "requests. It takes up to a few minutes. The run is saved and opens in the "
        "history on the left. The agent's plan and the timings vary from run to run, so "
        "repeated runs of one query can differ."
    )
    queries = load_queries()
    # Streamlit forgets a widget's value once its page is left, so the form keeps its
    # own copy in a plain dict and loads each keyed widget from it on the way back.
    # Mutating the dict, unlike assigning to session state, never yields to a rerun, so
    # the run flags below stay consistent even when a run is cut short.
    kept = st.session_state.setdefault(
        "form",
        {
            "example": "",
            "sql": "",
            "password": "",
            **{key: CONNECTION_SETTINGS[key] for key in ("host", "port", "dbname", "user")},
        },
    )
    for key in ("host", "port", "dbname", "user", "password", "example"):
        st.session_state.setdefault(key, kept[key])

    status = st.session_state.get("connection_status")
    # Any edit or click reruns the page, which cuts a run short after it is saved, so
    # the whole form is locked until the run ends.
    running = kept.get("running", False)
    with st.expander("Database connection", expanded=not (status and status[1])):
        host_column, port_column, database_column, user_column, password_column = (
            st.columns([3, 1, 2, 2, 2])
        )
        host = host_column.text_input("Host", key="host", disabled=running)
        port = port_column.number_input("Port", 1, 65535, key="port", disabled=running)
        dbname = database_column.text_input("Database", key="dbname", disabled=running)
        user = user_column.text_input("User", key="user", disabled=running)
        password = password_column.text_input(
            "Password", type="password", key="password", disabled=running
        )
        kept.update(host=host, port=port, dbname=dbname, user=user, password=password)
        settings: dict[str, Any] = {
            **CONNECTION_SETTINGS,
            "host": host, "port": port, "dbname": dbname, "user": user, "password": password,
        }
        if st.button(
            "Test connection",
            disabled=running or not password,
            help=None if password else "Enter the password first.",
        ):
            try:
                with psycopg.connect(**settings) as connection:
                    version = connection.execute("SHOW server_version").fetchone()
                    connected = (
                        f"Connected to {dbname} on {host}:{port}, PostgreSQL "
                        f"{version[0] if version else '?'}"
                    )
                    try:
                        connection.execute("LOAD 'pg_hint_plan'")
                        status = (settings, True, f"{connected}, with pg_hint_plan.")
                    except psycopg.Error as error:
                        status = (
                            settings,
                            False,
                            f"{connected}, but pg_hint_plan cannot be loaded, so plans "
                            f"cannot be forced: {error}",
                        )
            except psycopg.Error as error:
                status = (settings, False, f"Could not connect: {error}")
            st.session_state.connection_status = status
    # The status belongs to the settings it was tested with, so editing any clears it.
    if status and status[0] == settings:
        _, connected_ok, message = status
        (st.success if connected_ok else st.error)(message)
    else:
        st.caption("Connection not tested. Execute connects with these settings either way.")

    example = st.selectbox("Example query", ("", *queries), key="example", disabled=running)
    # A key per example, so picking one loads its SQL, while edits to it are kept.
    sql_key = f"sql-{example}"
    st.session_state.setdefault(
        sql_key, kept["sql"] if example == kept["example"] else queries.get(example, "")
    )
    sql = st.text_area("Query", height=200, key=sql_key, disabled=running)
    kept.update(example=example, sql=sql)
    if message := kept.pop("error", None):
        st.error(message)
    key = api_key()
    if key is None:
        st.warning("No ANTHROPIC_API_KEY is set (see the README), so the agent cannot run.")
    # The click sets both flags before the page is drawn again, so the form is already
    # locked while the run goes. The one-shot request, not the button, starts the run:
    # a button drawn disabled reports no click, and the busy flag alone would start
    # another run on a later visit if a run were cut short before clearing it.
    st.button(
        "Execute",
        type="primary",
        on_click=lambda: kept.update(running=True, request=True),
        disabled=running or key is None or not password or not sql.strip(),
        help=(
            "Enter the PostgreSQL password first." if not password
            else "Enter a query first." if not sql.strip()
            else None
        ),
    )
    if not kept.pop("request", False):
        kept["running"] = False
        return
    # One experiment file per GUI session, in the same format as --experiments.
    experiment_id = st.session_state.setdefault(
        "experiment_id", datetime.now(UTC).strftime(EXPERIMENT_ID_FORMAT)
    )
    client = anthropic.Anthropic(api_key=key)
    try:
        with st.spinner("Planning, asking the agent, forcing and timing…"):
            with psycopg.connect(**settings) as connection:
                connection.read_only = True
                run = collect_run(
                    connection,
                    # An edited example is a different query, so it is not named
                    # after the example.
                    query_name=(
                        example if sql.strip() == queries.get(example) else "custom"
                    ),
                    sql=sql.strip(),
                    client=client,
                    analyst_client=client,
                )
                # No Streamlit call until the run is filed: a click in the sidebar
                # mid-run reruns the page at the next one, and the rest is lost.
                if run.failure_stage == "postgres_plan":
                    # SQL PostgreSQL cannot plan, such as a typo, is not worth keeping.
                    kept["error"] = f"PostgreSQL could not plan the query: {run.failure_reason}"
                else:
                    write_run(run, experiment_id)
                    # Open the new run once the next script run has added its page.
                    kept["open_run"] = run_page_key(run)
    except psycopg.Error as error:
        kept["error"] = f"PostgreSQL error: {error}"
    finally:
        kept["running"] = False
    # The form was drawn locked, so draw the page again: unlocked, with any error, or
    # on the new run.
    st.rerun()


def main() -> None:
    st.set_page_config(page_title="Plan comparison", layout="wide")

    # Each saved run is a page, so the sidebar lists them as a chat app lists its
    # conversations, newest first, and each has its own URL.
    # Runs and experiment names are in UTC; astimezone() shows them in local time.
    history = []
    for path in AGENT_RUNS.glob("*.jsonl"):
        started = datetime.strptime(path.stem, EXPERIMENT_ID_FORMAT).replace(tzinfo=UTC)
        history += [(path.stem, started.astimezone(), run) for run in load_experiment(path.stem)]
    history.sort(key=lambda item: item[2].created_at, reverse=True)
    # Each label names its experiment by start time, so reruns of a query differ, and
    # leads with ✗ for a failed run, so long labels cut off at the end still show it.
    pages = {
        run_page_key(run): st.Page(
            lambda run=run, experiment=experiment: show_run(run, experiment),
            title=("✗ " if run.failure_stage else "")
            + f"{run.query_name} · {run.created_at.astimezone():%m-%d %H:%M} · "
            + f"session {started:%m-%d %H:%M}",
            url_path=run_page_key(run),
        )
        for experiment, started, run in history
    }
    new_page = st.Page(new_run_page, title="New query", icon=":material/add:", default=True)
    page = st.navigation({"": [new_page], "History": list(pages.values())}, expanded=True)
    if open_run := st.session_state.get("form", {}).pop("open_run", None):
        st.switch_page(pages[open_run])
    if st.session_state.pop("open_new", False):
        st.switch_page(new_page)
    page.run()


if __name__ == "__main__":
    main()
