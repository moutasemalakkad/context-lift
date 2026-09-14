# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One round: two arms, the same questions, one verdict.

This is the loop the whole framework exists for. Everything else feeds it or
reads its output.

    split      the questions into a curate set and a held-out set, by hash
    gate       the free checks, BEFORE any paid call
    ask        every question to both arms, K times each
    score      run the gold and the prediction, compare rows
    compare    the paired difference, McNemar, the clustered line
    verdict    one word, in the order that cannot print a flattering lie

The order of the first two steps is the part worth defending. The gates run
before the model is called, not after, because a round that was going to be
INVALID should cost nothing. A leak gate that fires after 156 chats has told
you the same thing and charged you for it.

The model call is injected. A test drives the whole round with a local
function and no network, which is what lets the arithmetic be checked without
paying for it, and the real run passes `CaApiRunner.answer`.

A held-out question is scored once per round. That is not a convention here,
it is the only reason the number means anything: read them twice and the
second read was chosen knowing the first.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field

from .ablate import (AblationReport, Safety, Verdict, ablate, safety,
                     verdict)
from .cost import RoundCost
from .power import DEFAULT_TARGET_POINTS
from .gates import (Gate, answers_with_no_sql, holdout_reads, pasted_sql,
                    stored_example_lookups, template_twins, resolvable_lift)
from .harness import EvalReport, evaluate
from .leakage import SPLIT_SEED, check_leakage, split_golds
from .runners import Arm

# The repeat marker in a scored id. `ablate` already reads this shape to tell
# repetitions of one question from different questions.
REPEAT_MARKER = "#rep"


@dataclass
class Gold:
    """One question with a reviewed answer that runs."""

    id: str
    question: str
    gold_sql: str


@dataclass
class RoundResult:
    """Everything a page needs, and nothing a page has to re-derive."""

    verdict: Verdict
    gates: list[Gate] = field(default_factory=list)
    curate: AblationReport | None = None
    holdout: AblationReport | None = None
    curate_ids: list[str] = field(default_factory=list)
    holdout_ids: list[str] = field(default_factory=list)
    control_arm_id: str = ""
    treatment_arm_id: str = ""
    k_repeats: int = 1
    # Non-empty when every gold was reserved on purpose, holding the REASON.
    # Reserving everything changes what the numbers mean -- no working half,
    # so no fit estimate and no OVERFIT check -- and whoever reads the run
    # later needs to know who claimed the context was external and why.
    external_context: str = ""
    calls: int = 0
    # What this round's own SQL cost, read from the warehouse's job ledger.
    # None when it was not asked for or nothing could be read, and it prints
    # as "not recovered" rather than as zero. Per-command `spent:` lines count
    # MODEL calls only and totalled about $74 while the ledger held $149.61,
    # so a cost line that could quietly read $0.0000 would repeat the exact
    # mistake this field exists to end.
    cost: "RoundCost | None" = None
    # The treatment arm's failures, by shape, from the curate set. Read from
    # curate rather than holdout because that is the set a person is allowed
    # to look at while deciding what to write next; reading the holdout to
    # choose a lever is how a holdout becomes training data.
    failure_shapes: dict[str, int] = field(default_factory=dict)
    # One row per (question, arm): what was asked, what came back, whether it
    # was right. This is what a committed run is made of, so a number in a
    # document can be recomputed later with no warehouse and no model.
    rows: list[dict] = field(default_factory=list)
    # Per split, the scored ids answered by running a stored example verbatim
    # instead of writing SQL. Kept out of every report above, and printed,
    # because "20 of 23 answers were lookups" changes what the lift means
    # even when the lift is real.
    lookups: dict[str, list[str]] = field(default_factory=dict)
    # Only the working set may guide the next context revision. Keeping its
    # treatment score preserves comparator details for offline triage.
    treatment_report: EvalReport | None = None

    def gap(self) -> tuple[float, float, float] | None:
        """The generalization gap in points, with its interval: point, low, high.

        Working-set lift minus held-out lift. It answers the question a lift
        alone cannot: did the context help the questions it was written from
        more than the ones it was not? A gap whose interval excludes zero is
        the shape of the customer page that read REAL GAIN on +60.6 working
        points beside a flat held-out set.

        Printed on every round, whichever word came out, because a reader who
        only sees it when it is bad learns to read its absence as good news.
        None only when a side is missing entirely.
        """
        if self.curate is None or self.holdout is None:
            return None
        from .metrics import independent_difference_interval
        working = self.curate.defended
        held_out = self.holdout.defended
        low, high = independent_difference_interval(
            (working.b, working.c, self.curate.n_defended),
            (held_out.b, held_out.c, self.holdout.n_defended))
        point = (self.curate.lift - self.holdout.lift) * 100
        return point, low * 100, high * 100

    def _scored(self, split: str, ids: list[str], report) -> str:
        """How many questions the number rests on, not how many were asked.

        The two differ as soon as an answer is a stored-example lookup: on
        2026-09-10 the working set printed "23 questions" for a number that
        rested on one, because 22 of the 23 had been answered by lookup. A
        count of what was asked, printed beside a lift, reads as the lift's
        sample size, and that is the number a person quotes later.
        """
        asked = len(ids)
        scored = getattr(report, "n_questions", None) or report.n_paired
        if scored == asked:
            return f"{asked} questions"
        # There are two reasons the numbers differ and a reader deserves to
        # know which. A lookup is an answer the agent did not write. An
        # unpaired question is one arm scoring it and the other excluding it,
        # usually a prediction that hit the row cap. Printing "18 questions"
        # for a lift that rests on 16 is the mistake this helper exists to
        # prevent, and it survived once because the condition also required
        # a lookup.
        why = []
        if self.lookups.get(split):
            why.append(f"{len(self.lookups[split])} answered by a stored example")
        unpaired = len(getattr(report, "unpaired_ids", ()) or ())
        if unpaired:
            why.append(f"{unpaired} scored by only one arm")
        note = f" ({'; '.join(why)})" if why else ""
        return f"{scored} of {asked} questions{note}"

    @property
    def safety(self) -> Safety | None:
        """Did the context break any held-out question. A count, not a test.

        The held-out half, because that is the claim worth making. Breaking
        nothing among the questions the rules were written from is a weaker
        statement, and `safety_curate` carries it separately so the two are
        never read as one.
        """
        if self.holdout is None:
            return None
        return safety(self.holdout, half="held-out")

    @property
    def safety_curate(self) -> Safety | None:
        if self.curate is None:
            return None
        return safety(self.curate, half="working")

    def lines(self) -> list[str]:
        """The round as a person reads it: the verdict, the safety, then why."""
        out = [f"verdict: {self.verdict.word}", f"  {self.verdict.reason}"]
        # The safety line prints even when the word is INVALID, because a gate
        # failing says the LIFT cannot be trusted and says nothing about
        # whether answers that were right became wrong. Those are different
        # facts and a reader needs the second one either way.
        if self.safety is not None:
            out += [f"safety: {self.safety.word}", f"  {self.safety.reason}"]
            if self.safety.broke_ids:
                out.append("  broke: " + ", ".join(self.safety.broke_ids))
            if self.safety_curate is not None:
                s = self.safety_curate
                out.append(f"  on the working questions: {s.fixed} fixed, "
                           f"{s.broke} broke, of {s.n}. The rules were written "
                           "from these, so this number is weaker evidence")
        # Cost is the THIRD decision, beside the lift and the safety word.
        # The lift says whether the context is worth keeping, safety says
        # whether keeping it costs a working answer, and this says what
        # running it costs in money. Printed here rather than at the end
        # because a number a reader has to scroll for is a number nobody
        # quotes.
        if self.cost is not None:
            out.extend(self.cost.text().splitlines())
        # No fixed or broke counts here. The safety block above carries them,
        # and this line used to quote the POOLED ones while the HTML page
        # quoted the clustered ones, so a single round printed two different
        # "broke" numbers on two surfaces. Said once, in one place.
        if self.holdout is not None:
            out.append("  held out: "
                       + self._scored("holdout", self.holdout_ids, self.holdout)
                       + f", {self.holdout.lift * 100:+.1f} points")
        if self.external_context:
            out.append("  every gold is reserved, so there is no working "
                       f"half: {self.external_context}")
            out.append("  no fit estimate and no gap. OVERFIT cannot be "
                       "checked here, because there is nothing to compare "
                       "the reserved half against. That is the cost of the "
                       "choice, not an oversight")
        if self.curate is not None:
            out.append("  fit on the working questions: "
                       + self._scored("curate", self.curate_ids, self.curate)
                       + f", {self.curate.lift * 100:+.1f} points")
        gap = self.gap()
        if gap is not None:
            point, low, high = gap
            # A zero-width interval is not a precise zero. It is what the
            # formula returns when the two arms never disagreed on any
            # question, and printing "[+0.0, +0.0]" alone reads as the most
            # certain measurement on the page instead of the emptiest.
            if low == high == 0.0:
                why = (". The two arms never disagreed on any question, in "
                       "either set, so there is nothing to compare rather "
                       "than a gap measured at zero")
            elif low > 0:
                why = (". It excludes zero, so the context fitted the "
                       "questions it was written from and did not carry")
            else:
                why = (". It includes zero, so there is no evidence the "
                       "context fitted the questions it was written from")
            out.append(f"  gap: {point:+.1f} points [{low:+.1f}, {high:+.1f}], "
                       "the working-set lift minus the held-out one" + why)
        if self.verdict.questions_needed:
            out.append(f"  on a {len(self.holdout_ids)}-question holdout, about "
                       f"{self.verdict.questions_needed} questions would resolve "
                       "the target")
        for split in ("holdout", "curate"):
            found = self.lookups.get(split) or []
            if found:
                where = "held-out" if split == "holdout" else "working"
                out.append(f"  {len(found)} {where} answer(s) ran a stored "
                           "example instead of writing SQL, and are left out "
                           "of the numbers above")
        # "answers", not "model calls". They are the same number when a model
        # answered, and they are not when the answers came from a file: a CSV
        # round printed "62 model calls" having made none. The cost sheet is
        # where a call count belongs, and it is printed before the run.
        out.append(f"  arms {self.control_arm_id} against {self.treatment_arm_id}, "
                   f"K={self.k_repeats}, {self.calls} answers scored")
        if self.failure_shapes:
            from .lever import suggest
            out.append("")
            out.append("what to write next, from the working questions:")
            out += [f"  {line}" for line in suggest(self.failure_shapes)]
        return out


def free_gates(golds: list[Gold], treatment: Arm,
               allow_sql: bool = False,
               protect: list[Gold] | None = None) -> list[Gate]:
    """The checks that cost nothing, so they run before anything is paid for.

    A round that was going to be INVALID should cost nothing. A leak gate that
    fires after every question has been asked has told you the same thing and
    charged you for it.

    `protect` is the set the leak checks defend, and it is the HELD-OUT
    questions, not every gold. A curate question in the context is what
    curation means: the whole point of `curate-examples` is to turn working
    questions into worked examples. A held-out question in the context is the
    leak, because then its answer was handed over rather than written.

    Checking against every gold made the tool refuse its own workflow. Five
    examples built from curate golds failed the template-twin gate against all
    31 golds and passed against the 8 held out.

    Without `protect` every gold is defended, which is the right default for a
    caller that has not split anything yet.
    """
    protect = golds if protect is None else protect
    scored = [(g.id, g.gold_sql) for g in protect]
    items = [(f"ex{i}", e.sql) for i, e in enumerate(treatment.examples)]
    # And the SQL carried as prose. The served arm keeps its worked examples
    # inside the system instruction rather than in `example_queries`, so
    # `treatment.examples` is empty and the shape check printed "none of 0
    # items" while the arm carried 22 queries. A gate that cannot see the
    # thing it guards is worse than no gate, because it prints a pass.
    from .gates import sql_in_text
    items += [(f"instruction{i}", sql) for i, sql
              in enumerate(sql_in_text(treatment.system_instruction))]

    # Everything the agent is handed, as one string, which is what the leak
    # check reads. The rules, the terms and the examples all travel to the
    # model, so all three are searched.
    context = "\n".join(
        [treatment.system_instruction]
        + [f"{t.name}: {t.definition}" for t in treatment.terms]
        + [f"{e.question}\n{e.sql}" for e in treatment.examples])
    leak = check_leakage([(g.id, g.question, g.gold_sql) for g in protect], context)

    # The cheapest gate, and the one that would have saved the most: can
    # the agent reach the data each question is about? Checked over EVERY
    # gold, not only the protected ones, because a question the agent cannot
    # reach is wasted whichever half it lands in.
    from .gates import table_coverage
    coverage = table_coverage(
        {g.id: _tables_of(g.gold_sql, treatment) for g in golds},
        {_arm_table_key(t) for t in treatment.tables})

    # The project half of coverage, which the key above cannot see. A gold
    # whose answer key reads one project while the arm carries another is not
    # covered, however well the dataset and table names line up.
    mismatched = project_mismatches(golds, treatment)
    project_gate = Gate(
        "project match",
        not mismatched,
        (f"{len(mismatched)} of {len(golds)} golds name a project the arm does "
         f"not carry: "
         + "; ".join(f"{gid} reads {names[0]}, the arm has {names[1]}"
                     for gid, names in sorted(mismatched.items())[:3])
         + ("" if len(mismatched) <= 3 else f" and {len(mismatched) - 3} more"))
        if mismatched else
        f"every gold that names a project names one the arm carries "
        f"({len(golds)} golds)")

    return [
        coverage,
        project_gate,
        pasted_sql(treatment.system_instruction, allow_sql=allow_sql),
        template_twins(scored, items),
        Gate("leak", not leak.findings,
             f"{len(leak.findings)} of {len(protect)} held-out questions appear "
             "in what the agent is handed"),
    ]


def _arm_table_key(table: str) -> str:
    """`dataset.table`, lower case, from whatever form the arm carries.

    The arm names tables as `project.dataset.table`; a gold's SQL often
    names them as `dataset.table`, and on one estate as both. Comparing the
    two forms directly is how a gate that looks right passes everything.

    THE PROJECT IS DELIBERATELY DROPPED HERE and checked separately by
    `project_mismatches` below. Keeping it would make the common case -- an
    arm with three parts against a gold with two -- fail for a reason that
    is not a real mismatch, and the gate would stop being usable at all.
    """
    parts = str(table).lower().strip("`").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else parts[-1]


def _project_of(table: str) -> str:
    """The project a table name carries, or "" when it names none."""
    parts = str(table).lower().strip("`").split(".")
    return parts[0] if len(parts) >= 3 else ""


# A three-part table name in SQL, backticked or bare. `tables_named` cannot
# be reused here: it returns `dataset.table` and DROPS the project, which is
# the very thing this check needs. That is not a bug in it -- every other
# caller wants the two-part key -- but it does mean the project has to be
# extracted separately rather than compared later.
_QUALIFIED = re.compile(
    r"`([A-Za-z][\w-]*)\.([\w-]+)\.([\w-]+)`"        # `project.dataset.table`
    r"|(?<![\w.`])([A-Za-z][\w-]*)\.([\w-]+)\.([\w-]+)(?![\w.`])")


def projects_named(sql: str) -> set[str]:
    """Every project a SQL statement names explicitly, lower case.

    Empty when the SQL names none, which is the ordinary case: a gold
    written `dataset.table` has said nothing about which project.
    """
    found = set()
    for match in _QUALIFIED.finditer(sql or ""):
        project = match.group(1) or match.group(4)
        if project:
            found.add(project.lower())
    return found


def project_mismatches(golds, arm) -> dict[str, tuple[str, str]]:
    """Golds whose SQL names a project the arm does not carry.

    THE DEFECT THIS EXISTS FOR. `_arm_table_key` drops the project, so the
    coverage gate certified "43 of 43 golds have every table they need
    (100%)" while one gold's answer key read project `-p` and the arm
    carried only `-pp`. That gold was one of three published breaks, and its
    control MATCH meant only that two physical copies of the data agreed at
    score time -- which is not the same as the agent having been handed the
    table the key reads.

    ONLY WHERE BOTH SIDES NAME A PROJECT. A gold written `dataset.table` has
    said nothing about which project, so there is nothing to disagree with,
    and refusing it would refuse the ordinary case. This fires exactly when
    both are explicit and they differ.

    Returns `{gold_id: (project the gold names, projects the arm carries)}`.
    """
    arm_projects = {_project_of(t) for t in arm.tables} - {""}
    if not arm_projects:
        return {}
    found = {}
    for gold in golds:
        for project in sorted(projects_named(gold.gold_sql)):
            if project not in arm_projects:
                found[gold.id] = (project, ", ".join(sorted(arm_projects)))
                break
    return found


def _tables_of(gold_sql: str, arm: Arm) -> set[str]:
    """The tables one gold's SQL reads, in the arm's own key form.

    A name with one part is a CTE alias, not a table: mining one estate's
    SQL returned `base_cte` beside real tables, and passing that to the API
    is a 400. Dropped rather than counted as missing, because a CTE is not
    something a spec could ever carry.
    """
    from .mine import tables_named
    keys = set()
    for name in tables_named(gold_sql):
        key = _arm_table_key(name)
        if "." in key:
            keys.add(key)
    return keys


def _read_checkpoint(path) -> dict[tuple[str, str], dict]:
    """Answers already paid for, keyed by (arm, scored id).

    A round is dozens of serial calls over several minutes. The first live
    round died nine minutes in and lost every answer it had already been given.
    An answer is worth money and a wait, so it is written down as it arrives
    and reused on the next attempt.
    """
    if path is None or not pathlib.Path(path).is_file():
        return {}
    done: dict[tuple[str, str], dict] = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        done[(row["arm"], row["id"])] = row
    return done


def _ask_arm(golds: list[Gold], arm: Arm, ask, k: int,
             progress=None, checkpoint=None,
             done: dict[tuple[str, str], dict] | None = None,
             answers: dict[tuple[str, str], dict] | None = None) -> list[tuple]:
    """Every question to one arm, K times, as scoring tuples.

    The id carries the repetition, because `ablate` collapses repetitions of
    one question to a majority vote and needs to know which is which. A
    question the model refused keeps its row with an empty prediction: a
    refusal is a wrong answer, not a missing one, and dropping it would move
    the denominator.
    """
    done = done or {}
    answers = answers if answers is not None else {}
    rows = []
    total = len(golds) * k
    for index, gold in enumerate(golds):
        for repeat in range(k):
            scored_id = f"{gold.id}{REPEAT_MARKER}{repeat}"
            key = (arm.arm_id, scored_id)
            if key in done:
                record = done[key]
                if progress is not None:
                    progress(arm.label, index * k + repeat + 1, total,
                             scored_id, True)
            else:
                prediction = ask(scored_id, gold.question, arm)
                record = {
                    "arm": arm.arm_id, "id": scored_id,
                    "pred_sql": getattr(prediction, "pred_sql", None) or "",
                    # Everything the answer carried, not only its SQL. The
                    # worklist is built from these: which items were cited,
                    # which answers were a stored example run as written
                    # rather than text-to-SQL, and how much the model thought.
                    # Discarding them meant paying for an answer and keeping a
                    # tenth of it.
                    "citations": list(getattr(prediction, "citations", []) or []),
                    "matched_example": getattr(prediction, "matched_example", None),
                    "job_id": getattr(prediction, "job_id", None),
                    "thought_chars": getattr(prediction, "thought_chars", 0),
                    "retries": getattr(prediction, "retries", 0),
                    "error_class": getattr(prediction, "error_class", None),
                }
                if checkpoint is not None:
                    with open(checkpoint, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record) + "\n")
                if progress is not None:
                    progress(arm.label, index * k + repeat + 1, total,
                             scored_id, False)
            # Keyed by arm as well as question: both arms answer the same
            # scored id, and keying by id alone lets the second overwrite the
            # first.
            answers[(arm.arm_id, scored_id)] = record
            rows.append((scored_id, gold.question, gold.gold_sql,
                         record.get("pred_sql") or ""))
    return rows


def _lookups(control_rows: list[tuple], treatment_rows: list[tuple],
             answers: dict, control_id: str, treatment_id: str) -> set[str]:
    """The scored ids either arm answered by running a stored example.

    The API can match a worked example and execute it verbatim rather than
    write SQL. That answer says nothing about writing SQL, so it is not an
    observation in a text-to-SQL comparison.

    Dropped from BOTH arms, and for every repeat of that question. The
    comparison is paired: keeping the control's written SQL beside the
    treatment's lookup would compare two different activities and call the
    difference a lift. On the run of 2026-09-10 that mistake read as
    "-24.6 points, 17 broke" when nothing had broken at all.
    """
    found = set()
    for rows, arm_id in ((control_rows, control_id), (treatment_rows, treatment_id)):
        for scored_id, *_rest in rows:
            record = answers.get((arm_id, scored_id)) or {}
            if str(record.get("matched_example") or "").strip():
                found.add(scored_id)
    return found


def _score(executor, rows: list[tuple], gold_cache: dict,
           extra_columns: str) -> EvalReport:
    return evaluate(executor, rows, gold_cache=gold_cache,
                    extra_columns=extra_columns)


def run(golds: list[Gold], control: Arm, treatment: Arm, ask, executor,
        k: int = 1, holdout_fraction: float = 0.3, seed: str = SPLIT_SEED,
        target_pp: float = 10.0, allow_sql: bool = False,
        holdout_reads_so_far: int = 0,
    target_lift: float = DEFAULT_TARGET_POINTS,
    force_underpowered: bool = False, extra_gates: list[Gate] | None = None,
        progress=None, checkpoint=None,
        split: tuple[list[str], list[str]] | None = None,
        extra_columns: str = "reject",
        external_context: str = "",
        gold_cache: dict | None = None) -> RoundResult:
    """Run one round and return its verdict.

    `ask(question_id, question, arm)` returns anything with a `pred_sql`. The
    signature is the runner's own, so the real run passes
    `CaApiRunner.answer` with no adapter in between, and a test drives the
    same round with a local function and no network.

    The curate set and the held-out set are scored as two separate paired
    comparisons over the same two arms. They are not pooled: the whole point
    of the split is that one of them was allowed to be looked at while the
    context was written and the other was not.
    """
    if not golds:
        raise SystemExit("no golds: a round with no questions has no verdict")

    # An explicit split wins, because `contextlift split` writes its two id
    # files for a person to use and a round that quietly made its own split
    # instead is how a worked example built from a "working" question turns
    # out to be a reserved answer.
    #
    # Measured on the run that found it: `split` wrote 19 working and 20
    # reserved; this function's own split of the same golds gave 21 and 18,
    # and the two working halves overlapped on 10 ids of 19. Nine golds a
    # person treated as working were reserved here, so nine reserved answers
    # went into the example bank. The leak gate caught it -- "9 of 18
    # held-out questions appear in what the agent is handed" -- but anybody
    # following the documented order would have hit it, and a leak that
    # manufactures a FALSE POSITIVE is the worst failure this tool has.
    if split is not None:
        curate_ids, holdout_ids = split
        known = {g.id for g in golds}
        unknown = sorted((set(curate_ids) | set(holdout_ids)) - known)
        if unknown:
            raise SystemExit(
                f"the split names {len(unknown)} id(s) that are not in the "
                f"gold set: {unknown[:5]}. A split from a different gold set "
                "would score the wrong questions")
        missing = sorted(known - set(curate_ids) - set(holdout_ids))
        if missing:
            raise SystemExit(
                f"{len(missing)} gold(s) are in neither half of the split: "
                f"{missing[:5]}. Re-run `split` on this gold set")
        both = sorted(set(curate_ids) & set(holdout_ids))
        if both:
            raise SystemExit(
                f"{len(both)} gold(s) are in BOTH halves: {both[:5]}. A "
                "question cannot be practised on and reserved at once")
    else:
        curate_ids, holdout_ids = split_golds([g.id for g in golds],
                                              holdout_fraction, seed,
                                              external_context=external_context)
    by_id = {g.id: g for g in golds}
    curate = [by_id[i] for i in curate_ids]
    holdout = [by_id[i] for i in holdout_ids]

    # The gates defend the held-out questions. The curate set is allowed to be
    # in the context, and is where worked examples come from.
    gates = free_gates(golds, treatment, allow_sql=allow_sql, protect=holdout)
    gates.append(holdout_reads(holdout_reads_so_far + 1))
    # Arithmetic about the design, so it is checked before anything is bought.
    # Five paid rounds on one estate could not have produced a verdict and
    # nothing said so.
    gates.append(resolvable_lift(len(holdout_ids), target_lift,
                                 forced=force_underpowered))
    gates.extend(extra_gates or [])

    # INVALID before anything is paid for. The scores are not computed at all,
    # rather than computed and withheld, because a number that exists gets
    # quoted.
    failed = [g for g in gates if not g.passed]
    if failed:
        return RoundResult(
            verdict=verdict(_empty_report(), gates=[g.as_tuple() for g in gates]),
            gates=gates, curate_ids=curate_ids, holdout_ids=holdout_ids,
            control_arm_id=control.arm_id, treatment_arm_id=treatment.arm_id,
            k_repeats=k)

    rows: list[dict] = []
    failure_shapes: dict[str, int] = {}
    # Every answer, keyed by (arm, scored id). The worklist reads these; the
    # scoring path only needs the SQL.
    answers: dict[tuple[str, str], dict] = {}
    done = _read_checkpoint(checkpoint)
    if done and progress is not None:
        progress("resume", 0, 0, f"{len(done)} answers already on disk", True)
    gold_cache = gold_cache if gold_cache is not None else {}
    calls = 0
    lookups: dict[str, list[str]] = {}
    pairs: dict[str, tuple[AblationReport | None, int]] = {}
    working_treatment_report: EvalReport | None = None
    for name, subset in (("curate", curate), ("holdout", holdout)):
        if not subset:
            pairs[name] = (None, 0)
            continue
        control_rows = _ask_arm(subset, control, ask, k, progress,
                                checkpoint, done, answers)
        treatment_rows = _ask_arm(subset, treatment, ask, k, progress,
                                  checkpoint, done, answers)
        calls += len(control_rows) + len(treatment_rows)

        # A stored-example lookup is not a text-to-SQL answer, so it leaves
        # the comparison before anything is scored. The rows still go to the
        # run file, marked, because a person has to be able to see how many
        # answers were lookups and which ones.
        looked_up = _lookups(control_rows, treatment_rows, answers,
                             control.arm_id, treatment.arm_id)
        lookups[name] = sorted(looked_up)
        scored_control = [r for r in control_rows if r[0] not in looked_up]
        scored_treatment = [r for r in treatment_rows if r[0] not in looked_up]

        control_report = _score(executor, scored_control, gold_cache,
                                extra_columns)
        treatment_report = _score(executor, scored_treatment, gold_cache,
                                  extra_columns)
        rows.extend(_run_rows(name, "control", control.arm_id,
                              control_rows, control_report, answers))
        rows.extend(_run_rows(name, "treatment", treatment.arm_id,
                              treatment_rows, treatment_report, answers))
        if name == "curate":
            working_treatment_report = treatment_report
            from .triage import triage
            failure_shapes = dict(triage(treatment_report).counts)
        # Both arms answered the same golds in this one process, so a
        # difference in what they SCORED can only be a per-arm exclusion --
        # a prediction that hit the row cap, say. Pair what both scored and
        # let the report say how many were dropped, rather than throwing away
        # the whole round. See ablate's own comment for why the cross-run
        # callers keep refusing instead.
        report = ablate(control_report, treatment_report, pair_common=True)
        pairs[name] = (report, len(subset))

    # After the calls, because whether an answer was a lookup is only known
    # once the answer is back. A held-out lookup makes the verdict INVALID,
    # which is why it is a gate and not a footnote.
    gates.append(stored_example_lookups(lookups.get("holdout", []),
                                        lookups.get("curate", [])))

    # And whether the agent wrote anything at all. Also only knowable after
    # the calls, and also a gate rather than a footnote: a round where most
    # answers are empty measured nothing, and saying UNDERPOWERED there
    # names the sample size as the problem when the sample size was fine.
    empty = sum(1 for record in answers.values()
                if not (record.get("pred_sql") or "").strip())
    gates.append(answers_with_no_sql(empty, len(answers)))

    curate_report = pairs["curate"][0]
    holdout_report = pairs["holdout"][0]
    if holdout_report is None:
        raise SystemExit(
            f"{len(golds)} golds all landed in the curate set at a holdout "
            f"share of {holdout_fraction}. There is nothing held out, so there "
            "is no verdict to give")

    return RoundResult(
        verdict=verdict(holdout_report, curate_report,
                        [g.as_tuple() for g in gates], target_pp),
        gates=gates, curate=curate_report, holdout=holdout_report,
        curate_ids=curate_ids, holdout_ids=holdout_ids,
        external_context=external_context,
        control_arm_id=control.arm_id, treatment_arm_id=treatment.arm_id,
        k_repeats=k, calls=calls, rows=rows, failure_shapes=failure_shapes,
        lookups=lookups, treatment_report=working_treatment_report)


def _run_rows(split: str, arm: str, arm_id: str, asked: list[tuple],
              report: EvalReport,
              answers: dict[tuple[str, str], dict] | None = None) -> list[dict]:
    """One row per scored question, joined back to what was asked.

    `correct` is None for an excluded question, which is not the same as
    False. A gold that did not run and a row cut by the row cap were never
    scored, and counting either as wrong would move the denominator.
    """
    verdicts = {r.id: r for r in report.results}
    out = []
    for scored_id, question, gold_sql, pred_sql in asked:
        result = verdicts.get(scored_id)
        answer = (answers or {}).get((arm_id, scored_id), {})
        out.append({
            "id": scored_id, "split": split, "arm": arm, "arm_id": arm_id,
            "question": question, "gold_sql": gold_sql, "pred_sql": pred_sql,
            "verdict": None if result is None else result.verdict,
            "correct": None if result is None else result.execution_correct,
            # What the answer carried besides its SQL. `citations` is a
            # semicolon-joined list because this row becomes a CSV column, and
            # a list inside a cell is a list nobody can read.
            "citations": ";".join(answer.get("citations") or []),
            "matched_example": answer.get("matched_example") or "",
            "job_id": answer.get("job_id") or "",
            "thought_chars": answer.get("thought_chars") or 0,
            "retries": answer.get("retries") or 0,
            # WHY an answer carried no SQL. The runner computes this and it
            # used to be dropped on write, so a PRED_ERROR row said only
            # that something failed. One round had 55 empty answers and
            # finding out why meant making a fresh live API call; the reason
            # had been in memory the whole time. `no_sql_in_answer` is the
            # runner's own word for "the stream came back with nothing".
            "error_class": answer.get("error_class") or "",
        })
    return out


def _empty_report() -> AblationReport:
    """A report with nothing in it, for the INVALID path.

    The verdict function reads a holdout report even when a gate failed, and
    building an empty one is honest: no question was scored, so every count is
    zero. The alternative, making the argument optional, would let a caller
    ask for a verdict with no measurement at all.
    """
    from .metrics import mcnemar_exact, proportion
    return AblationReport(
        without_context=proportion(0, 0), with_context=proportion(0, 0),
        lift=0.0, mcnemar=mcnemar_exact(0, 0), n_paired=0, adjusted=False,
        n_questions=0)


def examples_from_curate(curate: list[Gold], holdout: list[Gold]) -> tuple[list, list]:
    """Worked examples built from working questions, and what was refused.

    Returns (kept, refused) as (question, sql) pairs. Two refusals, and the
    second one is the subtle one.

    A curate gold that compiles to the same query as a held-out gold, once its
    literals are masked, would let the model answer the held-out question by
    substitution rather than by writing SQL.

    A curate gold whose WORDING contains a held-out question is worse and
    harder to see: the shapes can differ completely. On thelook, "Which are the
    top 10 states by number of customers, and what is the customer_count for
    each?" is a curate question that contains the held-out "top 10 states by
    number of customers" word for word. Different SQL, same question handed
    over.

    Doing this here rather than leaving it to the gate is the difference
    between a bank that is clean and a bank that fails a check somebody then
    has to fix by hand.
    """
    from .leakage import check_leakage, normalize_sql

    holdout_shapes = {normalize_sql(g.gold_sql) for g in holdout}
    kept, refused = [], []
    for gold in curate:
        if normalize_sql(gold.gold_sql) in holdout_shapes:
            refused.append((gold.id, "same query shape as a held-out question"))
            continue
        context = f"{gold.question}\n{gold.gold_sql}"
        leak = check_leakage(
            [(g.id, g.question, g.gold_sql) for g in holdout], context)
        if leak.findings:
            names = ", ".join(sorted({f.gold_id for f in leak.findings}))
            refused.append((gold.id, f"its wording carries held-out {names}"))
            continue
        kept.append((gold.question, gold.gold_sql))
    return kept, refused
