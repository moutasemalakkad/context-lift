"""Check that the context you added does not contain the answers you score on.

The whole enrichment loop is "add business context, re-measure". If the context
handed to the agent contains the eval questions or their SQL, the re-measure is
testing memory, not understanding, and the lift is fake. This is easy to do by
accident: verified queries, example SQL and golden queries are all things teams
put in a context bundle, and they are also the obvious source of gold answers.

The failure is silent. A leaked eval set makes every number go up, which looks
exactly like success.

Two tools here:

  check_leakage  does the context bundle contain any gold question or gold SQL?
  split_golds    a deterministic train/holdout split, so curation and scoring
                 use different questions in the first place

Prevention beats detection: split first, and only curate against the train
half. check_leakage catches what slips through anyway — a verified query pasted
in later, a document that quotes the question.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Literals and whitespace differ between a stored query and a pasted one, so
# they are masked before comparison — the same normalization mine.fingerprint
# uses, for the same reason.
_STRING_LITERAL = re.compile(r"'[^']*'")
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")
_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")

# Below this many words, a "question" is too generic to be evidence of leakage
# ("total sales" will appear in any context bundle about sales).
_MIN_QUESTION_WORDS = 5


def normalize_sql(sql: str) -> str:
    """SQL reduced to its shape: literals masked, whitespace collapsed, folded."""
    text = _STRING_LITERAL.sub("'?'", sql or "")
    text = _NUMBER_LITERAL.sub("?", text)
    return _WHITESPACE.sub(" ", text).strip().rstrip(";").strip().lower()


def normalize_question(question: str) -> str:
    """Question reduced to comparable words: punctuation dropped, folded."""
    text = _PUNCTUATION.sub(" ", (question or "").lower())
    return _WHITESPACE.sub(" ", text).strip()


@dataclass(frozen=True)
class LeakFinding:
    """One gold whose question or SQL was found inside the context bundle."""

    gold_id: str
    kind: str  # "question" or "sql"
    detail: str


@dataclass(frozen=True)
class LeakageReport:
    """Whether the context under test contains the answers being scored."""

    findings: list[LeakFinding]
    n_golds: int
    n_context_chars: int

    @property
    def clean(self) -> bool:
        return not self.findings

    @property
    def leaked_ids(self) -> set[str]:
        return {f.gold_id for f in self.findings}

    def summary(self) -> str:
        if self.clean:
            return (
                f"no leakage: none of {self.n_golds} golds appear in "
                f"{self.n_context_chars:,} characters of context"
            )
        by_kind: dict[str, int] = {}
        for finding in self.findings:
            by_kind[finding.kind] = by_kind.get(finding.kind, 0) + 1
        lines = [
            f"LEAKAGE: {len(self.leaked_ids)} of {self.n_golds} golds appear in "
            f"the context bundle",
            "  " + ", ".join(f"{k}: {v}" for k, v in sorted(by_kind.items())),
            "",
            "  Any lift measured on these questions is memorization, not",
            "  understanding. Remove them from the context, or score on a",
            "  holdout that was never curated against.",
            "",
        ]
        for finding in self.findings[:10]:
            lines.append(f"  {finding.gold_id:<12} {finding.kind:<9} {finding.detail}")
        if len(self.findings) > 10:
            lines.append(f"  ... and {len(self.findings) - 10} more")
        return "\n".join(lines)


def check_leakage(
    golds: list[tuple[str, str, str]],
    context: str,
    retrieved: dict[str, str] | None = None,
) -> LeakageReport:
    """Look for gold questions and gold SQL inside a context bundle.

    Args:
        golds: (id, question, gold_sql) triples — the questions being scored.
        context: everything the agent is given at runtime, concatenated. Catalog
            descriptions, verified queries, example SQL, glossary, instructions.
        retrieved: when a retriever chose the context per question,
            `{question_id: text}`. Each question is then checked against the
            shared context plus its own retrieved text.

    Detection is normalized-substring, not exact: literals and whitespace are
    masked first, so a verified query stored with different parameters is still
    caught. Questions shorter than five words are skipped, because a short
    phrase appearing in a document is not evidence of anything.

    Two rules, once retrieval is in play, and only the first is obvious. A
    question's own context must not contain its own answer. And the union of
    everything retrieved must not contain a RESERVED question's answer — those
    are never retrieved for, so nobody hands them context, but their answers can
    sit inside a document retrieved for some other question. Checking each blob
    against its own question alone would miss exactly that, which is the leak
    that destroys the number you report.
    """
    retrieved = retrieved or {}
    everything = "\n".join([context] + [retrieved[k] for k in sorted(retrieved)])

    def _seen(gold_id: str) -> tuple[str, str]:
        # a question the retriever served: its own context. One it did not
        # (a reserved question): everything, because any of it could carry the
        # answer and nothing was chosen for it.
        text = f"{context}\n{retrieved[gold_id]}" if gold_id in retrieved else everything
        return normalize_sql(text), normalize_question(text)

    findings: list[LeakFinding] = []
    for gold_id, question, gold_sql in golds:
        sql_seen, question_seen = _seen(gold_id)
        shape = normalize_sql(gold_sql)
        if shape and shape in sql_seen:
            findings.append(
                LeakFinding(gold_id, "sql", f"gold SQL appears verbatim: {shape[:60]}...")
            )
        words = normalize_question(question)
        if len(words.split()) >= _MIN_QUESTION_WORDS and words in question_seen:
            findings.append(
                LeakFinding(gold_id, "question", f'question appears verbatim: "{words[:60]}..."')
            )
    return LeakageReport(
        findings=findings, n_golds=len(golds), n_context_chars=len(everything or "")
    )


# The default split seed. ITS VALUE MUST NEVER CHANGE, whatever the project is
# called. The split is a hash of the gold id plus this string, and the whole
# point is stability: "adding questions never moves an existing one across the
# line, so a holdout score stays comparable across an engagement." Change this
# and every existing curate/holdout split reshuffles, which silently puts every
# committed result on a different split.
#
# It still reads "trueness" because that was the value when the first split was
# made. The 2026-09-13 rename changed it in ten places by find-and-replace and
# would have reshuffled everything; one named constant is how that stops being
# possible.
SPLIT_SEED = "trueness"


def split_golds(
    gold_ids: list[str],
    holdout_fraction: float = 0.3,
    seed: str = SPLIT_SEED,
    family_of: "dict[str, str] | None" = None,
    spent: "set[str] | None" = None,
    external_context: str = "",
) -> tuple[list[str], list[str]]:
    """Split gold ids into (curate_against, hold_out), deterministically.

    `external_context` is a REASON, and giving one reserves every gold: the
    working half comes back empty. It exists for context nobody in the loop
    authored, where a split protects against nothing.

    The case that forced it, on 2026-09-12. Looker's public thelook project
    was read into a context arm and scored. Looker wrote it in February 2022;
    the gold set was built in 2026 from `queries.sql`. The context predates
    the questions by four years and no human here touched it, so there is
    nothing for a reserved half to protect against -- a split exists to stop
    somebody writing rules while looking at the questions.

    Run as `--holdout-share 0.99` instead, that round left a working half of
    TWO questions and the report printed "fit on the working questions: 2
    questions, +0.0 points" and "gap: -2.0 points [-6.7, +2.7]" -- a gap and
    an interval computed from two questions, beside numbers computed from a
    hundred. The workaround did not just read badly; it manufactured a
    comparison.

    The reason is required because reserving everything changes what the
    numbers mean: there is no working half, so OVERFIT cannot be checked and
    no fit estimate exists. Whoever reads the run six months later needs to
    know who claimed the context was external and why.

    Hashing the id rather than shuffling means the split is stable as the gold
    set grows: adding questions never moves an existing one across the line, so
    a holdout score stays comparable across an engagement.

    Curate against the first list. Score the second ONCE, at the end. Every
    time you look at the holdout to decide what to do next, it becomes training
    data and stops answering the question it exists for.

    `family_of` maps a gold id to something it is ABOUT -- its primary
    table's dataset, say -- and makes the split STRATIFIED: each family is
    divided between the two halves rather than landing wholly in one.

    Why, measured on one estate. Hashing the id alone is blind to the
    subject, and on a small set across a wide warehouse that blindness makes
    the halves unrelated: 19 working questions touched 41 tables, 20 reserved
    touched 31, the halves shared 9, and **eleven of the twenty reserved
    questions were about tables the working half never touched**. No context
    written from one side could reach them, however well written, so a
    reserved lift was not unlikely -- it was unavailable.

    The first attempt at this hashed the family, which puts a whole family on
    one side and made it WORSE: shared ground fell from 44% to 14%, because
    then the halves share nothing by construction. Stratifying is the
    opposite operation and the right one.

    The tension this creates is real and is resolved elsewhere. Splitting a
    family across both halves is exactly what raises the risk of a template
    TWIN straddling the line, which turns answering a reserved question into
    substitution. Shared SUBJECT is what makes context able to carry across;
    shared SHAPE is what must be forbidden. The twin gate forbids the second
    and this enables the first, and on one estate that gate caught 9 of 19
    examples, so it is not theoretical.

    Stability survives: a question's position inside its family comes from
    hashing the question, so adding a question to a family never moves an
    existing one.

    `spent` is ids that must NOT be reserved. A question whose failure
    somebody has read is training data whatever the split says, and pretending
    otherwise is how a holdout quietly stops being one. They go to the working
    half and the reason is the caller's to record.
    """
    # 1.0 stays refused as a FRACTION, and is reachable only through
    # `external_context`. A fraction of 1.0 arrived at by arithmetic -- a
    # rounding, a config default, a typo -- would silently empty the working
    # half and with it the OVERFIT check, and the round would still print a
    # verdict. Asking for it in words cannot happen by accident.
    if external_context:
        if not external_context.strip():
            raise ValueError(
                "external_context needs a reason, not an empty string: "
                "reserving every gold changes what the numbers mean and the "
                "claim has to be attributable")
        return [], list(gold_ids)
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be between 0 and 1")

    spent = spent or set()
    curate, holdout = [], []
    if not family_of:
        for gold_id in gold_ids:
            if gold_id in spent:
                curate.append(gold_id)
                continue
            digest = hashlib.sha256(f"{seed}:{gold_id}".encode()).hexdigest()
            # first 8 hex digits as a fraction of the space
            position = int(digest[:8], 16) / 0xFFFFFFFF
            (holdout if position < holdout_fraction else curate).append(gold_id)
        return curate, holdout

    # Stratified: each family divided between the halves, in the family's own
    # stable order, so both sides are about the same subjects.
    families: dict[str, list[str]] = {}
    for gold_id in gold_ids:
        if gold_id in spent:
            curate.append(gold_id)
            continue
        families.setdefault(family_of.get(gold_id) or "", []).append(gold_id)
    for family, members in sorted(families.items()):
        ordered = sorted(
            members,
            key=lambda g: hashlib.sha256(f"{seed}:{g}".encode()).hexdigest())
        # A family of one cannot be on both sides, and putting every
        # singleton in the holdout would fill it with unreachable questions.
        # They go where their own hash puts them, as before.
        if len(ordered) == 1:
            gold_id = ordered[0]
            digest = hashlib.sha256(f"{seed}:{gold_id}".encode()).hexdigest()
            position = int(digest[:8], 16) / 0xFFFFFFFF
            (holdout if position < holdout_fraction else curate).append(gold_id)
            continue
        take = max(1, round(len(ordered) * holdout_fraction))
        take = min(take, len(ordered) - 1)      # never the whole family
        holdout.extend(ordered[:take])
        curate.extend(ordered[take:])
    return curate, holdout
