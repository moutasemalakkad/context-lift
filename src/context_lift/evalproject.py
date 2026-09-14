# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The clean room a measurement runs in, and why it has to exist.

No field of the Conversational Analytics API turns the service's own reading
of table metadata on or off. A Google blog and a codelab say it pulls
BigQuery table and column descriptions by itself; the API reference does not
say either way. So on a customer estate a request that carries no context is
not a request without context: it silently carries whatever the estate's own
build tool wrote. Measuring against that control credits the estate's
documentation to the catalog.

A view fixes it. A BigQuery view does not inherit the descriptions of the
tables under it, so a view created here starts undocumented, its catalog
entry starts with only the system aspects, and nothing has been generated for
it. The harness then hands the control arm the estate's own descriptions on
purpose, and the lift measures the authored context above them.

Three more reasons the room is separate, each from the design doc's gaps:
measurement writes never touch the production catalog; the run gets its own
30-chat-per-minute quota instead of sharing production's; and the gold
executions can be billed elsewhere, because the catalog mines join links from
query history with no scan, and a gold executed here would become context.

Everything is planned before anything is done. Authorizing a view is a write
on the dataset holding the customer's data, and that is their call, not ours.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Step:
    """One thing to do, what it needs, and how to do it by hand."""
    what: str
    needs: str
    statement: str
    on_production: bool = False


@dataclass
class Plan:
    """Every step, in order, with the two that touch production marked."""
    eval_project: str
    eval_dataset: str
    steps: list[Step] = field(default_factory=list)

    @property
    def touches_production(self) -> list[Step]:
        return [step for step in self.steps if step.on_production]

    def text(self) -> str:
        lines = [f"evaluation room: {self.eval_project}.{self.eval_dataset}", ""]
        for number, step in enumerate(self.steps, start=1):
            mark = "  (writes on the production dataset)" if step.on_production else ""
            lines.append(f"{number}. {step.what}{mark}")
            lines.append(f"   needs {step.needs}")
            for line in step.statement.splitlines():
                lines.append(f"     {line}")
            lines.append("")
        if self.touches_production:
            lines.append(f"{len(self.touches_production)} of {len(self.steps)} steps write on a "
                         "production dataset. Authorizing a view is how the view reads the")
            lines.append("data without every reader needing access to the tables. Nothing here")
            lines.append("changes a table, a row, or any existing description.")
        lines.append("")
        lines.append("Run each statement, or hand this to whoever holds the grants. Then")
        lines.append("`contextlift doctor --explain` turns any refusal into the role that grants it.")
        return "\n".join(lines)


def _parts(table: str) -> tuple[str, str, str]:
    pieces = table.split(".")
    if len(pieces) != 3:
        raise ValueError(f"a table needs project.dataset.table, got {table!r}")
    return pieces[0], pieces[1], pieces[2]


def view_name(eval_project: str, eval_dataset: str, table: str) -> str:
    """The evaluation view that stands in for one production table.

    Named after the source dataset and table together, because two datasets
    on one estate carry the same table name often enough that a collision
    would silently point two views at one table.
    """
    _, dataset, name = _parts(table)
    return f"{eval_project}.{eval_dataset}.{dataset}__{name}"


def plan(eval_project: str, eval_dataset: str, tables: list[str],
         location: str = "US") -> Plan:
    """Every statement needed to stand the room up, in the order they run.

    `SELECT *` and not a column list: the view must track the table, and a
    frozen column list would quietly drop a column added later, which reads
    as the agent failing to find it.
    """
    if not tables:
        raise ValueError("name at least one production table")
    steps = [Step(
        what=f"create the evaluation dataset in {eval_project}",
        needs="bigquery.datasets.create on the evaluation project",
        statement=f"CREATE SCHEMA IF NOT EXISTS `{eval_project}.{eval_dataset}`\n"
                  f"OPTIONS (location = '{location}');",
    )]

    for table in sorted(tables):
        view = view_name(eval_project, eval_dataset, table)
        steps.append(Step(
            what=f"a view over {table}, with no descriptions of its own",
            needs="bigquery.tables.create on the evaluation dataset, and read on the source",
            statement=f"CREATE OR REPLACE VIEW `{view}` AS\nSELECT * FROM `{table}`;",
        ))

    by_dataset: dict[str, list[str]] = {}
    for table in sorted(tables):
        project, dataset, _ = _parts(table)
        by_dataset.setdefault(f"{project}.{dataset}", []).append(table)
    for source, held in by_dataset.items():
        views = ", ".join(view_name(eval_project, eval_dataset, t) for t in held)
        steps.append(Step(
            what=f"authorize the view(s) on {source} so they may read it"
                 + (" - SKIP THIS if the source dataset is readable by "
                    "allUsers or allAuthenticatedUsers"),
            needs="bigquery.datasets.update and bigquery.datasets.setIamPolicy on "
                  f"{source} (roles/bigquery.dataOwner)",
            statement="-- Authorizing a view exists so the view's READERS do not\n"
                      "-- need access to the source. On a public dataset they already\n"
                      "-- have it, so this step is unnecessary. Checked on 2026-09-10\n"
                      "-- against bigquery-public-data.thelook_ecommerce: an\n"
                      "-- unauthorized view read 124,724 rows. Confirm with:\n"
                      f"--   bq show --format=prettyjson {source.replace('.', ':', 1)} | jq '.access'\n"
                      "-- and look for allUsers or allAuthenticatedUsers.\n"
                      f"-- Otherwise, add each view to the access list of {source}:\n"
                      f"--   {views}\n"
                      f"bq update --source <(bq show --format=prettyjson {source.replace('.', ':', 1)} \\\n"
                      f"  | jq '.access += [{{\"view\":{{\"projectId\":\"{eval_project}\",\n"
                      f"     \"datasetId\":\"{eval_dataset}\",\"tableId\":\"<VIEW>\"}}}}]') \\\n"
                      f"  {source.replace('.', ':', 1)}",
            on_production=True,
        ))

    return Plan(eval_project=eval_project, eval_dataset=eval_dataset, steps=steps)


def arm_tables(eval_project: str, eval_dataset: str, tables: list[str]) -> list[str]:
    """The names an arm references: the views, never the production tables."""
    return [view_name(eval_project, eval_dataset, table) for table in sorted(tables)]


def billing_warning(eval_project: str, harness_project: str | None) -> str | None:
    """Why gold execution is billed somewhere else, when it is not.

    The catalog mines join relationships from query history for tables and
    views, with no scan and nothing to opt out of. Every gold the gate runs
    against the views therefore becomes catalog state on them, and a join
    the treatment arm later exports could have been learned from the answer
    key. Billing the gate elsewhere keeps that history out of this room.
    """
    if harness_project and harness_project != eval_project:
        return None
    return (f"gold execution is billed to {eval_project}, the same project the arms run in. "
            "The catalog mines join links from query history with no scan, so every gold "
            "the gate executes becomes context on these views. Pass a different "
            "--harness-project, or expect the join-link gate to report links it mined "
            "from the answer key.")
