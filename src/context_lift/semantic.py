"""Read a semantic layer: the business rules someone already wrote down.

A query log tells you what people ask. A semantic layer tells you what the
answers MEAN — that revenue is `sale_price` summed, that orders join to users
on `user_id`, that "AUR" is sales divided by units. Somebody wrote those rules
so a BI tool could use them, and they are exactly the curated context the rest
of this package measures the value of.

This matters because mining a query log often does not find them. On one
warehouse, 400 mined query patterns produced 4 usable candidates: the log was
dashboard refreshes, and every metric definition lived in the BI model instead.

Two outputs, feeding opposite ends of the loop:

    to_context()      the rules as text — the `with_context` arm of an ablation
    to_candidates()   a metric crossed with a dimension is a question whose SQL
                      is derivable, so this bootstraps a gold set too

The model in the middle is deliberately neutral — metrics, dimensions, join
paths — following the same core shape as open semantic interchange work, so a
dbt or Cube reader can be added without touching anything downstream. LookML is
the first reader because it is the format most enterprises already have.

Needs the [lookml] extra; `lkml` is imported lazily so the core stays
stdlib-only.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Metric:
    """A measure: a number the business has agreed how to compute.

    `sql` is the COMPLETE expression, with the source's aggregation applied.
    This module used to keep the argument verbatim and drop the operator, on
    the stated grounds that "a half-translated expression is worse than an
    honest one". That reasoning was right and the code got it backwards:
    LookML's `type: sum` with `sql: ${sale_price}` kept verbatim is
    `sale_price`, which is not an honest half, it is a WRONG whole. It tells a
    reader the metric is a bare column. So the operator is applied and the
    argument is left in the source's syntax, which is the honest half the
    docstring was reaching for.

    `restricted_to` carries a measure that counts only some rows. It is in the
    SOURCE tool's filter language, not SQL, and it says so where it is printed:
    Looker's filter language alone has `-NULL`, `>10`, `Complete,Returned`
    meaning OR, `%foo%` and phrases like `last 7 days`, and a reader that
    guessed at those would manufacture exactly the confident falsehood the
    paragraph above exists to stop.
    """

    name: str
    kind: str = ""
    description: str = ""
    sql: str = ""
    source_table: str = ""
    restricted_to: str = ""

    @property
    def is_documented(self) -> bool:
        """True if a human wrote down what this means, not just how to compute it."""
        return bool(self.description.strip())


# A primary key or a foreign key identifies a row; it does not describe one.
# Grouping a metric by it produces one row per record, which is not a question
# anybody asks ("revenue by order id" is just the orders table).
_KEY_LIKE = re.compile(r"(^|_)(id|key|code|pk|fk)$", re.I)


@dataclass(frozen=True)
class Dimension:
    """A field you can group or filter by."""

    name: str
    kind: str = ""
    description: str = ""
    sql: str = ""
    source_table: str = ""
    is_key: bool = False

    @property
    def groupable(self) -> bool:
        """True if grouping a metric by this field asks a real question."""
        return not self.is_key and not _KEY_LIKE.search(self.name or "")


@dataclass(frozen=True)
class JoinPath:
    """How two tables relate — stated, not inferred from a query log."""

    left: str
    right: str
    on: str = ""
    relationship: str = ""


@dataclass
class SemanticModel:
    """Business rules read out of a semantic layer, ready to use as context."""

    metrics: list[Metric] = field(default_factory=list)
    dimensions: list[Dimension] = field(default_factory=list)
    joins: list[JoinPath] = field(default_factory=list)
    source: str = ""

    # The four below come from formats that carry them. LookML fills none of
    # them today, so they stay empty and every LookML output is unchanged.
    #
    # `instructions` are rules a person wrote for a model to follow, in the
    # order they were found: model level first, then dataset, then field.
    # `synonyms` maps an item name to the other words people use for it.
    # `stated_questions` are questions a person already wrote into the model,
    # each with the item it sat on. They carry no SQL, so they are candidates.
    # `spec_version` is the format version the file declared.
    instructions: list[str] = field(default_factory=list)
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    stated_questions: list[tuple[str, str]] = field(default_factory=list)
    spec_version: str = ""

    # `unreadable` is (path, error) per file that failed to parse. Skipping a
    # bad file is right -- one unparseable file must not lose the other 141 --
    # but skipping it SILENTLY is not: a directory where every file fails
    # printed "0 metrics" and exited 0, which reads as an empty project rather
    # than a broken read. Found 2026-09-12 on a fixture with one stray `;;`.
    #
    # `unresolved` counts constructs this reader does not follow, per
    # construct. A view that `extends` another is read WITHOUT the parent's
    # fields, so its context is incomplete, and incomplete context that
    # announces nothing is the same fault as a wrong formula.
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    unresolved: dict[str, int] = field(default_factory=dict)

    @property
    def documented_metrics(self) -> list[Metric]:
        return [m for m in self.metrics if m.is_documented]

    def summary(self) -> str:
        """What was found, and how much of it a human had already explained."""
        documented = len(self.documented_metrics)
        share = documented / len(self.metrics) if self.metrics else 0.0
        described = sum(1 for d in self.dimensions if d.description)
        lines = [
            f"{self.source or 'semantic layer'}:",]
        if self.unreadable:
            lines.append(f"  {len(self.unreadable)} file(s) did NOT parse and "
                         "were skipped; what is below excludes them:")
            for path, error in self.unreadable[:3]:
                lines.append(f"    {path}: {error}")
            if len(self.unreadable) > 3:
                lines.append(f"    ... and {len(self.unreadable) - 3} more")
        lines += [
            f"  {len(self.metrics)} metrics, {documented} with a written "
            f"description ({share:.0%})",
            f"  {len(self.dimensions)} dimensions, "
            f"{described} of them described",
            f"  {len(self.joins)} join paths",
        ]
        # A count of what was READ is not a count of what an agent RECEIVES.
        # `to_context` exports only described dimensions, on the sound ground
        # that a bare column name adds nothing a schema does not already give.
        # But the summary printed the read count alone, so a real Looker
        # project reported "38 dimensions" and shipped none of them, and the
        # person reading that line had no way to tell. Same shape as the round
        # that printed "62 model calls" having made none.
        if self.dimensions and not described:
            lines.append(
                f"  None of the {len(self.dimensions)} dimensions carry a "
                "description, so none reach the context: a\n"
                "  bare column name is already in the schema. Describing them "
                "is what adds anything."
            )
        for construct, count in sorted(self.unresolved.items()):
            lines.append(f"  {count} {construct}: {_UNRESOLVED_MEANING[construct]}")
        if self.metrics and share < 0.5:
            lines.append(
                "  Most metrics have no description. The SQL says how to compute\n"
                "  them; nothing says what they mean. That gap is what a model\n"
                "  cannot guess, so it is the first thing worth curating."
            )
        return "\n".join(lines)

    def to_context(self, include_joins: bool = True) -> str:
        """The rules as a context bundle to hand an agent.

        Metrics first and descriptions before SQL, because the meaning is the
        part a model cannot reconstruct. Undocumented metrics are still listed —
        the expression alone is worth something — but after the documented ones.
        """
        blocks: list[str] = []
        if self.instructions:
            blocks.append("# Rules")
            for instruction in self.instructions:
                blocks.append(f"- {instruction}")
            blocks.append("")
        if self.metrics:
            blocks.append("# Metric definitions")
            if any(m.restricted_to for m in self.metrics):
                blocks.append(
                    "A metric marked `counts only rows where` is RESTRICTED: it "
                    "ignores every other row. The condition is quoted in the "
                    "source tool's filter language, which is not SQL.")
            for metric in self.documented_metrics + [
                m for m in self.metrics if not m.is_documented
            ]:
                line = f"- {metric.name}"
                if metric.source_table:
                    line += f" (on {metric.source_table})"
                if metric.description:
                    line += f": {metric.description}"
                if metric.sql:
                    line += f"\n    computed as: {metric.sql}"
                elif metric.kind:
                    line += f"\n    computed as: {metric.kind}"
                if metric.restricted_to:
                    line += f"\n    counts only rows where {metric.restricted_to}"
                other_names = self.synonyms.get(metric.name)
                if other_names:
                    line += "\n    also called: " + ", ".join(other_names)
                blocks.append(line)
        if include_joins and self.joins:
            blocks.append("\n# How tables join")
            for join in self.joins:
                line = f"- {join.left} to {join.right}"
                if join.on:
                    line += f" on {join.on}"
                if join.relationship:
                    line += f" ({join.relationship})"
                blocks.append(line)
        described = [d for d in self.dimensions if d.description]
        if described:
            blocks.append("\n# Field meanings")
            for dimension in described:
                line = f"- {dimension.name}: {dimension.description}"
                other_names = self.synonyms.get(dimension.name)
                if other_names:
                    line += "\n    also called: " + ", ".join(other_names)
                blocks.append(line)
        return "\n".join(blocks)

    def to_candidates(self, max_per_metric: int = 3) -> list[tuple[str, str]]:
        """(question, metric_name) pairs a human turns into golds.

        A documented metric is already a question — "Number of order line items"
        becomes "how many order line items are there?" — and crossing it with a
        dimension on the same table gives the "by X" variants that make a gold
        set cover more than totals.

        Deliberately returns no SQL. The metric's expression is in that tool's
        dialect and the join path may be needed; writing the query is the human
        step, the same as with mined queries. Candidates, never golds.
        """
        # A question a person already wrote beats one this code assembles, so
        # those come first and are never rephrased.
        out: list[tuple[str, str]] = list(self.stated_questions)
        by_table: dict[str, list[Dimension]] = {}
        for dimension in self.dimensions:
            if not dimension.groupable:
                continue
            by_table.setdefault(dimension.source_table, []).append(dimension)

        for metric in self.documented_metrics:
            # A description often carries a trailing qualifier ("..., in USD");
            # it reads badly once "by region" is appended, so it is dropped for
            # the crossed questions and kept for the plain one.
            phrase = metric.description.rstrip(".").strip()
            head = phrase.split(",")[0].strip()
            out.append((f"What is {phrase.lower()}?", metric.name))
            for dimension in by_table.get(metric.source_table, [])[: max_per_metric - 1]:
                readable = dimension.name.replace("_", " ")
                out.append((f"What is {head.lower()} by {readable}?", metric.name))
        return out


# LookML writes `${TABLE}.column` and `${view.field}`; both are noise in a
# description a human reads during review.
_LOOKML_REF = re.compile(r"\$\{([^}]*)\}")


def _clean_sql(sql: str | None) -> str:
    """LookML expression as readable text.

    `${TABLE}.col` becomes `col` — the `${TABLE}` marker is the tool's way of
    saying "this view's table", which a human reviewing the rule does not need.
    """
    if not sql:
        return ""
    text = _LOOKML_REF.sub(lambda m: m.group(1), sql)
    text = text.replace("TABLE.", "")
    return " ".join(text.replace(";;", "").split())


# A LookML measure's `type` IS the aggregation; `sql` is only its argument.
# So a measure read without its type is not thin, it is WRONG: `type: sum` with
# `sql: ${sale_price}` reaches an agent as the bare column `sale_price`, and an
# agent that trusts that line writes SELECT sale_price where the metric means
# SELECT SUM(sale_price). Wrong context measures worse than none
# (arXiv 2408.07702), so this table is a correctness fix, not a polish one.
#
# Taken from Looker's own measure-types reference, read 2026-09-12:
# docs.cloud.google.com/looker/docs/reference/param-measure-types
_AGGREGATION = {
    "sum": "SUM({sql})",
    "sum_distinct": "SUM(DISTINCT {sql})",
    "average": "AVG({sql})",
    "average_distinct": "AVG(DISTINCT {sql})",
    "min": "MIN({sql})",
    "max": "MAX({sql})",
    "count_distinct": "COUNT(DISTINCT {sql})",
    "median": "PERCENTILE({sql}, 0.5)",
    "median_distinct": "PERCENTILE(DISTINCT {sql}, 0.5)",
    "percentile": "PERCENTILE({sql}, p)",
    "percentile_distinct": "PERCENTILE(DISTINCT {sql}, p)",
}

# Types the same reference lists as NOT aggregates. `number` is the one that
# makes a blanket wrap wrong: its `sql` is the entire expression, so
# `type: number, sql: ${a}/${b}` is already correct printed bare. The three
# ending in a measure reference (percent_of_total, running_total,
# percent_of_previous) take another MEASURE rather than a column, so their
# expression is incomplete on its own; that is said out loud rather than
# guessed at, because inventing the missing half is how a distractor is born.
_NOT_AGGREGATE = {"number", "string", "yesno", "date",
                  "percent_of_total", "running_total", "percent_of_previous"}
_NEEDS_A_MEASURE = {"percent_of_total", "running_total", "percent_of_previous"}


# A file reader sees a Looker project as WRITTEN, not as Looker SERVES it.
# Looker resolves these four before a query runs; `lkml` does not, and neither
# does this module. Each one is therefore named and counted rather than passed
# over, because context that is quietly incomplete is indistinguishable from
# context that is complete.
#
# The Looker API's `lookml_model_explore` returns the RESOLVED model and would
# need none of this. It also needs a live Looker instance, which is why the
# file reader is the supported path and the API reader is not built.
_UNRESOLVED_MEANING = {
    "view(s) extending another view":
        "their parent's fields are NOT included, so those metrics are missing",
    "view refinement(s) (`view: +name`)":
        "read as their own view; the fields they ADD to the base are separate",
    "derived table(s)":
        "built by a query, so the table name below is a placeholder",
    "include statement(s)":
        "not followed; every .lkml under the directory is read instead",
}


def _count(model: "SemanticModel", construct: str) -> None:
    """Tally one unresolved construct. Keys must exist in `_UNRESOLVED_MEANING`.

    Asserted rather than tolerated: a construct counted with no explanation
    would print a bare number, and a bare number in a warning is the thing
    this module spent today removing.
    """
    assert construct in _UNRESOLVED_MEANING, construct
    model.unresolved[construct] = model.unresolved.get(construct, 0) + 1


def measure_filters(measure: dict) -> str:
    """A LookML measure's row restriction, in Looker's own filter language.

    `lkml` puts BOTH of LookML's filter syntaxes under one key, `filters__all`,
    with different shapes inside it, and a reader that handles only one is
    silent on the other:

        filters: { field: is_first_purchase  value: "Yes" }
          -> [{"field": "is_first_purchase", "value": "Yes"}]

        filters: [status: "Complete", x: "-NULL"]          (the legacy form)
          -> [[{"status": "Complete"}, {"x": "-NULL"}]]

    Returned as text and never as SQL. See `Metric.restricted_to`.
    """
    raw = measure.get("filters__all") or measure.get("filters") or []
    pairs: list[tuple[str, str]] = []

    def take(item):
        if isinstance(item, list):
            for inner in item:
                take(inner)
        elif isinstance(item, dict):
            if "field" in item:
                pairs.append((str(item.get("field", "")),
                              str(item.get("value", ""))))
            else:  # the legacy form: one single-key dict per condition
                for field, value in item.items():
                    pairs.append((str(field), str(value)))

    take(raw)
    return " and ".join(f'{field} is "{value}"' for field, value in pairs
                        if field)


def apply_measure_type(kind: str, sql: str, primary_key: str = "") -> str:
    """What the measure actually computes, in SQL, or "" if it cannot be said.

    `primary_key` is used only by `type: count`. Looker's reference is explicit
    that "type: count measures perform table counts that are based on the
    table's primary key, so type: count measures don't support the sql
    parameter". COUNT(*) and COUNT(pk) are the same number until a join fans
    out, and a fanned-out join is exactly when someone asks this tool why the
    total is too high, so the key is named when the view declares one.
    """
    kind = (kind or "").strip().lower()
    sql = (sql or "").strip()

    if kind == "count":
        return f"COUNT({primary_key})" if primary_key else "COUNT(*)"
    if kind == "list":
        # GROUP_CONCAT in Looker's table, but the separator and ordering are
        # instance settings this reader cannot see, so the dialect is not faked.
        return f"a list of every {sql}" if sql else ""
    if kind in _NEEDS_A_MEASURE:
        return f"{kind.replace('_', ' ')} of {sql}" if sql else ""
    if kind in _NOT_AGGREGATE:
        return sql
    template = _AGGREGATION.get(kind)
    if template and sql:
        return template.format(sql=sql)
    # An unknown type is reported as itself. A new LookML type must not be
    # silently rendered as a bare column, which is the bug this whole table
    # exists to fix.
    if sql and kind:
        return f"{sql} (LookML type {kind}, not known to this reader)"
    return sql or kind


def read_lookml(directory: str | pathlib.Path) -> SemanticModel:
    """Read every .lkml file under `directory` into a SemanticModel.

    Reads views for metrics and dimensions, and explores for join paths. Files
    that fail to parse are skipped rather than raising: a real LookML project
    usually has one file using a construct the parser does not know, and losing
    the other 141 files to it helps nobody.
    """
    import lkml  # lazy: keeps the core install stdlib-only

    directory = pathlib.Path(directory)
    paths = sorted(directory.rglob("*.lkml"))
    if not paths:
        raise FileNotFoundError(f"no .lkml files under {directory}")

    model = SemanticModel(source=str(directory))
    for path in paths:
        try:
            parsed = lkml.load(path.read_text())
        except Exception as error:  # noqa: BLE001 -- one bad file must not lose the rest
            # Skipped, but RECORDED. `summary()` prints it. See
            # `SemanticModel.unreadable` for why silence was the bug here.
            model.unreadable.append((
                str(path.relative_to(directory)),
                " ".join(str(error).split())[:120]))
            continue

        if "includes" in parsed or "include" in parsed:
            _count(model, "include statement(s)")

        for view in parsed.get("views", []):
            name = (view.get("name") or "").strip()

            # `view: +orders { ... }` is a REFINEMENT: it adds fields to a view
            # declared elsewhere. `lkml` keeps the `+`, and with no
            # `sql_table_name` the old code used the name as the table, so a
            # refinement produced the table `+orders`, which exists nowhere.
            # The M9 spec predicted this exact fault before it was written.
            if name.startswith("+"):
                _count(model, "view refinement(s) (`view: +name`)")
                name = name.lstrip("+").strip()
            if "extends" in view or "extends__all" in view:
                _count(model, "view(s) extending another view")
            if "derived_table" in view:
                _count(model, "derived table(s)")

            table = view.get("sql_table_name") or name
            table = table.replace(";;", "").strip()

            # Dimensions first, though measures are declared first in the file:
            # `type: count` counts the view's PRIMARY KEY, so the key has to be
            # known before a count measure can be written down honestly.
            keys: list[str] = []
            for dimension in view.get("dimensions", []):
                if dimension.get("hidden") == "yes":
                    continue  # hidden from users, so not a question anyone asks
                if dimension.get("primary_key") == "yes":
                    keys.append(dimension.get("name", ""))
                model.dimensions.append(Dimension(
                    name=dimension.get("name", ""),
                    kind=dimension.get("type", ""),
                    description=(dimension.get("description") or "").strip(),
                    sql=_clean_sql(dimension.get("sql")),
                    source_table=table,
                    is_key=dimension.get("primary_key") == "yes",
                ))

            for measure in view.get("measures", []):
                kind = measure.get("type", "")
                model.metrics.append(Metric(
                    name=measure.get("name", ""),
                    kind=kind,
                    description=(measure.get("description") or "").strip(),
                    sql=apply_measure_type(
                        kind, _clean_sql(measure.get("sql")),
                        keys[0] if keys else ""),
                    source_table=table,
                    restricted_to=measure_filters(measure),
                ))

        for explore in parsed.get("explores", []):
            base = explore.get("from") or explore.get("view_name") or explore.get("name", "")
            for join in explore.get("joins", []):
                model.joins.append(JoinPath(
                    left=base,
                    right=join.get("name", ""),
                    on=_clean_sql(join.get("sql_on")),
                    relationship=join.get("relationship", ""),
                ))
    return model
