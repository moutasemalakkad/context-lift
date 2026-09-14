# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Break one property of good context at a time, and measure what it costs.

The committed `examples/thelook/spec` is MEASURED to be worth +50.0 points
[+21.5, +67.4], 8 fixed 0 broke, p = 0.0078. That makes it the one thing this
project has that a defect can be injected into with known ground truth: break
one property, re-run, and the drop is what that kind of bad context costs.

WHY THIS GOES BEFORE MORE PAID WORK. It is immune to the three things blocking
everything else. A customer's reserved set is contaminated and thelook's is
not; adjudicating business intent needs an analyst, but when you inject the
defect yourself you already know the truth; and the linter cannot be validated
on the one file that inspired it, because five real defects is not a test set.

PERTURBATIONS ARE DATA, NOT EDITED COPIES. Each one below is a find-and-replace
over the spec's rule text, applied at run time to a deepcopy. A directory of
hand-edited spec copies drifts the moment the original changes and cannot be
re-run against a new model, which is the whole reason this is a module rather
than eight folders.

THE POSITIVE CONTROL IS THE MOST IMPORTANT ENTRY. `remove-capitalisation`
deletes a rule known to be load-bearing: a lowercase `'shipped'` matches no
row, so the answer comes back empty and reads as a result rather than as a
mistake. If removing it does not register as damage, the harness is broken and
not the context. A perturbation suite with no positive control measures its own
insensitivity and reports it as a finding.

WHAT THIS CANNOT PRODUCE. Generality. A defect costing ten points on a clean
seven-table public schema may cost nothing on a 900-dataset estate. The suite
measures the sensitivity of ONE agent on ONE schema, and any write-up has to
say so.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

# The linter checks each perturbation is meant to trip. Empty means the defect
# is invisible to the linter by design -- which is itself the finding, because
# a defect no free check can see is one only a paid round will ever catch.
NO_CHECK = ""


@dataclass(frozen=True)
class Perturbation:
    """One injected defect, as data.

    `replacements` are (find, replace) pairs applied to every rules file in
    order. A pair whose `find` is absent is an error, not a no-op: a
    perturbation that silently changed nothing would be scored as "this defect
    costs nothing" when in fact it was never injected.
    """

    name: str
    breaks: str
    replacements: tuple[tuple[str, str], ...]
    m18_check: str = NO_CHECK
    note: str = ""

    def apply(self, spec):
        """The spec with this defect injected. The input is untouched."""
        changed = copy.deepcopy(spec)
        rebuilt = []
        hits = 0
        for filename, body in spec.rules:
            text = body
            for find, replace in self.replacements:
                if find in text:
                    text = text.replace(find, replace)
                    hits += 1
            rebuilt.append((filename, text))
        if hits < len(self.replacements):
            raise SystemExit(
                f"perturbation {self.name!r} matched {hits} of "
                f"{len(self.replacements)} replacement(s). The spec has "
                f"changed under it, so this arm would be scored as a defect "
                f"that costs nothing when the defect was never injected.")
        changed.rules = rebuilt
        return changed


REVENUE = "- Revenue is the sale price of items that are neither Cancelled nor Returned."
COMPLETED = "- A completed order has status exactly 'Complete'."
MARGIN = "- Margin is sale price minus product cost."
CAPITALISATION = (
    "Status values are capitalised exactly: Complete, Cancelled, Returned,\n"
    "Processing, Shipped. A query written with 'shipped' in lower case returns "
    "zero\nrows and looks like an empty result rather than a mistake.")

SUITE: tuple[Perturbation, ...] = (
    Perturbation(
        name="strip-subject",
        breaks="the rule commands something and never says what it is about",
        replacements=((REVENUE,
                       "- Compute it as the sale price of items that are "
                       "neither Cancelled nor Returned."),),
        m18_check="no subject",
        note="The defect that produced five real lines on one estate. The "
             "author was told naming the metric was a person's job, and no "
             "person came."),
    Perturbation(
        name="strip-binding",
        breaks="the prose survives and the column name is gone",
        replacements=(("`shipped_at` to `delivered_at`",
                       "the ship date to the delivery date"),),
        m18_check="no identifier",
        note="A rule a person understands perfectly and an agent cannot act "
             "on, because nothing in it names a column."),
    Perturbation(
        name="unknown-column",
        breaks="the rule points at a column no table carries",
        replacements=((COMPLETED,
                       "- A completed order has `order_status_code` exactly "
                       "'Complete'."),),
        m18_check="unknown column",
        note="The check dbt's Semantic Layer runs as its data-platform "
             "validation, in the one place this project can score it."),
    Perturbation(
        name="ambiguous-column",
        breaks="the column is on two tables and the rule does not say which",
        replacements=((COMPLETED,
                       "- A completed order has `status` exactly 'Complete'."),),
        m18_check="ambiguous column",
        note="`status` is on orders AND order_items. Observed once on a real "
             "estate: a rule true of the business moved the agent off the "
             "table that already had the column."),
    Perturbation(
        name="conflicting-definitions",
        breaks="two lines define the same term differently",
        replacements=((MARGIN,
                       "- Margin is sale price minus product cost.\n"
                       "- Margin is sale price minus inventory landed cost."),),
        m18_check=NO_CHECK,
        note="NOT INVENTED: third-party context for this same public dataset "
             "disagrees in exactly this way. No linter check catches it "
             "today, which is why it is here -- a defect no free check sees "
             "is one only a paid round will ever find."),
    Perturbation(
        name="irrelevant-context",
        breaks="five rules name columns no question uses",
        replacements=((REVENUE, REVENUE + "\n"
                       "- Stock keeping units are tracked by `product_sku`.\n"
                       "- Distribution centres are identified by "
                       "`distribution_center_id`.\n"
                       "- Session identifiers use `session_id`.\n"
                       "- Web events carry a `sequence_number`.\n"
                       "- Inventory rows carry a `cost` at creation time."),),
        m18_check="no question would change",
        note="Also a replication attempt: the established distractor result "
             "is -24.3 points for a worked example handed to the wrong "
             "question, and this asks whether inert REFERENCE text behaves "
             "the same way. Practice 15 says it should not."),
    Perturbation(
        name="positive-control-remove-capitalisation",
        breaks="a rule known to be load-bearing is deleted",
        replacements=((CAPITALISATION, ""),),
        m18_check=NO_CHECK,
        note="THE CONTROL. A lowercase 'shipped' matches no row, so the "
             "answer is empty and reads as a result rather than a mistake. "
             "If this does not register as damage, the harness is broken and "
             "not the context, and every other number here is worthless."),
)


def by_name(name: str) -> Perturbation:
    found = [p for p in SUITE if p.name == name]
    if not found:
        raise SystemExit(f"no perturbation {name!r}. Known: "
                         + ", ".join(p.name for p in SUITE))
    return found[0]


@dataclass
class Injected:
    """One perturbation, its arm, and what the free checks said about it.

    `caught_by` holds only checks the CLEAN spec did not already raise. The
    first version reported every finding, so two warnings the clean spec
    produces anyway -- `no identifier` and `examples without routing` -- read
    as the linter detecting the injected defect. A baseline nobody subtracts
    is a baseline that flatters every arm equally.

    `skipped` is kept apart from a miss, and the difference is the whole
    verdict. A check that RAN and stayed quiet is a gap in the linter. A check
    that could not run is a gap in the SPEC, and on `examples/thelook/spec`
    it is the second one: that spec carries 0 column names, so the three
    column checks never execute.
    """

    perturbation: Perturbation
    arm_id: str
    caught_by: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def check_could_run(self) -> bool:
        wanted = self.perturbation.m18_check
        return not any(wanted and wanted in note for note in self.skipped)

    @property
    def verdict(self) -> str:
        wanted = self.perturbation.m18_check
        if not wanted:
            if self.caught_by:
                return f"no check claimed, but caught by {', '.join(self.caught_by)}"
            return "no free check sees it, as designed"
        if not self.check_could_run:
            return (f"{wanted} COULD NOT RUN on this spec, so nothing is "
                    f"proven either way")
        return "CAUGHT FREE" if wanted in self.caught_by else f"{wanted} MISSED it"


def inject(spec, tables=None, model: str = "LATEST_GA_MODEL",
           gold_sql: list[str] | None = None) -> list[Injected]:
    """Every perturbation applied, with its arm hash and the linter's verdict.

    THE LINTER PASS IS FREE AND IS THE POINT OF RUNNING THIS BEFORE PAYING.
    Each perturbation names the M18 check it should trip. Running the linter
    over each injected spec answers, for nothing, which defects a free check
    already catches -- and which ones only a paid round can find.

    The arm hash makes a perturbation reproducible: two runs of the same
    defect are the same experiment, and a defect whose arm hashes like the
    clean spec was never injected at all.
    """
    from .export import build_arms
    from .lint import lint

    _control, clean = build_arms(spec, tables, model)
    # The baseline, subtracted from every arm below. The clean spec already
    # raises `no identifier` and `examples without routing`, and counting
    # those as detections would credit the linter for finding a defect that
    # was there before anything was injected.
    before = lint(spec, gold_sql)
    # COUNTS, not a set of names. `no identifier` fires on the clean spec
    # already, so presence alone cannot tell a new one from the old ones --
    # the first version reported `strip-binding` as a miss for exactly that
    # reason, when the check does fire, on one more line.
    baseline: dict[str, int] = {}
    for finding in before.findings:
        baseline[finding.check] = baseline.get(finding.check, 0) + 1

    out = []
    for item in SUITE:
        damaged = item.apply(spec)
        _c, arm = build_arms(damaged, tables, model)
        if arm.arm_id == clean.arm_id:
            raise SystemExit(
                f"perturbation {item.name!r} produced the same arm as the "
                f"clean spec ({arm.arm_id}). Nothing was injected.")
        report = lint(damaged, gold_sql)
        after: dict[str, int] = {}
        for finding in report.findings:
            after[finding.check] = after.get(finding.check, 0) + 1
        fresh = {check for check, n in after.items()
                 if n > baseline.get(check, 0)}
        out.append(Injected(perturbation=item, arm_id=arm.arm_id,
                            caught_by=tuple(sorted(fresh)),
                            skipped=tuple(report.skipped)))
    return out


def plan_text(injected: list[Injected], clean_arm_id: str,
              questions: int, reps: int = 1) -> str:
    """What the suite would run, and what the free pass already found."""
    lines = [f"perturbation suite: {len(injected)} defect(s) against the clean "
             f"arm {clean_arm_id}",
             ""]
    for item in injected:
        perturbation = item.perturbation
        lines.append(f"  {perturbation.name:<38} arm {item.arm_id}")
        lines.append(f"      breaks: {perturbation.breaks}")
        lines.append(f"      free check: {item.verdict}")
    unrunnable = [i for i in injected if not i.check_could_run]
    if unrunnable:
        lines += ["",
                  f"  {len(unrunnable)} check(s) could not run on this spec at "
                  f"all. That is a gap in the SPEC, not in the linter:",
                  f"      {injected[0].skipped[0] if injected[0].skipped else ''}"]
    calls = len(injected) * questions * reps
    lines += ["",
              f"  to RUN: {len(injected)} arm(s) x {questions} question(s) x "
              f"{reps} rep(s) = {calls} model call(s).",
              "  Price it with `round --dry-run` before spending. Building "
              "and the linter pass above are free.",
              "",
              "  What this cannot produce: generality. A defect costing ten "
              "points on a clean",
              "  seven-table public schema may cost nothing on a "
              "900-dataset estate. This",
              "  measures one agent on one schema."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The BIRD suite: the same idea at a size that can resolve its own control.
#
# WHY A SECOND SET EXISTS. The seven above are find-and-replace pairs written
# against the thelook spec, and that spec's working half is 74 questions,
# which resolves a change above about 14 points
# (`power.smallest_resolvable(74)`). The positive control there measured -4.1.
# So the thelook suite CANNOT resolve its own control, by a factor of about
# three, and every smaller defect is further inside the noise.
#
# BIRD pooled is 450 questions and resolves about 4.7 points, which is where a
# four-point effect starts to be visible. These are written as TRANSFORMS
# rather than fixed strings because one definition has to work on three
# different databases' evidence text.
#
# What BIRD context looks like: one `- ` line per question's evidence, e.g.
#   - non-carcinogenic molecules refers to label = '-'; chlorine atoms ...
# The quoted literals are the decoding a schema cannot give, which is what
# makes the positive control below load-bearing.

import re as _re

_QUOTED = _re.compile(r"'[^']*'")
_REFERS = _re.compile(r"^- ([^;]{3,60}?) refers to ", _re.M)


@dataclass(frozen=True)
class TextPerturbation:
    """One injected defect, as a function over the context text.

    `apply` must change the text. A transform that returns its input is an
    error for the same reason a replacement matching nothing is: the arm
    would be scored as a defect that costs nothing when nothing was injected.
    """

    name: str
    breaks: str
    transform: object
    note: str = ""

    def apply(self, text: str) -> str:
        out = self.transform(text)
        if out == text:
            raise SystemExit(
                f"perturbation {self.name!r} changed nothing. It would be "
                f"scored as a defect that costs nothing when the defect was "
                f"never injected.")
        return out


def _drop_quoted_sentences(text: str) -> str:
    """Delete every evidence line carrying a quoted literal.

    THE POSITIVE CONTROL, and it is load-bearing for a reason already
    measured. A quoted literal is the value decoding a schema cannot supply:
    `label = '+'` means carcinogenic, and no column description says so. The
    over-salience work found 19 toxicology questions breaking when just the
    delivery of these sentences changed. Removing them entirely should cost
    much more than that.

    If this shows no loss, the harness is not sensitive enough to measure
    anything smaller, and that is the finding.
    """
    return "\n".join(line for line in text.splitlines()
                     if not (line.strip().startswith("- ") and _QUOTED.search(line)))


def _rename_a_column(text: str) -> str:
    """Point the evidence at a column no table carries.

    The most-mentioned snake_case identifier gains a `_code` suffix
    everywhere. Every sentence still reads correctly to a person and no
    sentence can be executed, which is dbt's data-platform validation case
    in a place where its cost can be measured.
    """
    names = _re.findall(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", text)
    if not names:
        return text
    top = max(set(names), key=names.count)
    return _re.sub(rf"\b{_re.escape(top)}\b", f"{top}_code", text)


def _add_irrelevant(text: str) -> str:
    """Five sentences about columns no question uses.

    Inert reference text. Practice 15 says sending more reference costs
    nothing, and the established distractor result is -24.3 points for a
    worked EXAMPLE handed to the wrong question. This asks whether reference
    behaves like an example or not, which is the one place those two
    predictions disagree.
    """
    return text.rstrip("\n") + "\n" + "\n".join([
        "- warehouse load batch refers to etl_batch_identifier",
        "- record freshness refers to ingested_at_timestamp",
        "- source system refers to upstream_feed_name",
        "- audit trail refers to change_log_sequence",
        "- partition key refers to physical_partition_id",
    ])


def _contradict_a_literal(text: str) -> str:
    """Define one term twice, with different values.

    Takes the first line carrying a quoted literal and adds a copy whose
    literal is changed. A person reading both cannot tell which is right, and
    neither can an agent. No free check catches it, which is why it is worth
    pricing.
    """
    for line in text.splitlines():
        if line.strip().startswith("- ") and _QUOTED.search(line):
            twisted = _QUOTED.sub("'CONTRADICTORY'", line, count=1)
            return text.replace(line, f"{line}\n{twisted}", 1)
    return text


def _strip_subject(text: str) -> str:
    """Replace the subject of every "X refers to Y" line with a pronoun.

    The meaning is still there for a person who reads the whole file and gone
    for an agent matching a question to a line. This is the defect that
    produced five real rule lines on one customer estate.
    """
    return _REFERS.sub("- it refers to ", text)


BIRD_SUITE: tuple[TextPerturbation, ...] = (
    TextPerturbation(
        name="positive-control-drop-value-decoding",
        breaks="every sentence carrying a quoted literal is deleted",
        transform=_drop_quoted_sentences,
        note="THE CONTROL. A quoted literal is the decoding a schema cannot "
             "give. If deleting all of them does not register, nothing "
             "smaller will, and every other number here is worthless."),
    TextPerturbation(
        name="unknown-column",
        breaks="the most-used column is renamed to one no table carries",
        transform=_rename_a_column,
        note="dbt's data-platform validation case, priced."),
    TextPerturbation(
        name="strip-subject",
        breaks="every 'X refers to Y' loses its X",
        transform=_strip_subject,
        note="Five real lines on one estate looked exactly like this."),
    TextPerturbation(
        name="conflicting-definitions",
        breaks="one term is defined twice with different values",
        transform=_contradict_a_literal,
        note="No free check catches it, which is why it needs a price."),
    TextPerturbation(
        name="irrelevant-context",
        breaks="five sentences name columns no question uses",
        transform=_add_irrelevant,
        note="Practice 15 predicts no cost; the distractor result predicts "
             "-24.3. This is where the two disagree."),
)


def bird_by_name(name: str) -> TextPerturbation:
    found = [p for p in BIRD_SUITE if p.name == name]
    if not found:
        raise SystemExit(f"no BIRD perturbation {name!r}. Known: "
                         + ", ".join(p.name for p in BIRD_SUITE))
    return found[0]
