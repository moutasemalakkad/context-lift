# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The checks a round has to pass before its numbers mean anything.

A score is a number a round tries to move. A gate is pass or fail. The
difference matters: a failed gate is printed INSTEAD of the scores and is never
folded into them, because a number from a run that failed its own checks gets
quoted later without the caveat.

Every check here is a pure function of things already on disk. None of them
calls a cloud service, so `make test` runs the whole set offline and a person
can see why a gate failed without paying for a rerun.

Each returns a `Gate`. The list of them goes to `ablate.verdict`, which prints
the first failure as the headline and withholds the scores. The order of the
list is the order a reader sees, so the cheapest and most damning checks come
first.

Two gates live elsewhere because they own more than a rule. The gold gate is
`gate.py`, twelve defect classes over the answer key. Leak part (a), a scored
question's text or SQL appearing in the context, is `leakage.check_leakage`.
Part (b), the template twin, is here, because it compares two golds to each
other rather than a gold to a document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .leakage import normalize_sql
from .power import (DEFAULT_BREAK_RATE, DEFAULT_TARGET_POINTS,
                    expected_disagreements, floor_on_disagreements, power,
                    rates_for, smallest_resolvable)

# A rules file is for meaning: what a column holds, which rows to exclude, what
# a word means. Pasted SQL in it is an example bank in the wrong place, where
# the leak gate does not look. Measured on one estate: 24 of 29 rule lines
# carried a SELECT, and that round's page was wrong.
PASTED_SQL_SHARE = 0.20

# Below this a "share of lines" number is noise: one SELECT in three lines is
# 33% and means nothing.
PASTED_SQL_MIN_LINES = 5

_SELECT = re.compile(r"\bselect\b", re.I)

# Ten is what a person will actually re-derive by hand in a sitting. The number
# matters less than doing it at all: an unchecked answer key looks like BIRD
# Mini-Dev, where an audit found 52.8% of golds wrong.
AUDIT_SAMPLE = 10


# A round may still be worth running when a few golds name a table the arm
# does not carry: those questions score wrong in both arms and dilute, which
# is bad but measurable. Below this share of golds fully covered, nothing is
# being measured at all. Set from the run that found it: 1 of 39 golds had
# every table it needed, 55 of 78 answers came back with no SQL, and the page
# read UNDERPOWERED as though the answer were more questions.
TABLE_COVERAGE_FLOOR = 0.60


@dataclass(frozen=True)
class Gate:
    """One check. `detail` is shown whether it passed or failed.

    A passing gate still prints its detail, because "leak: 0 of 47 questions
    appear in the context" is evidence and "leak: ok" is not.
    """

    name: str
    passed: bool
    detail: str

    def as_tuple(self) -> tuple[str, bool, str]:
        """The shape `ablate.verdict` reads."""
        return (self.name, self.passed, self.detail)

    def line(self) -> str:
        return f"[{'ok  ' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


def pasted_sql(rules_text: str, allow_sql: bool = False) -> Gate:
    """Refuse a rules file that is mostly pasted queries.

    SQL belongs in the example bank, which the leak gate reads and the gold
    gate executes. SQL pasted into a rules file gets neither check, and on one
    estate that is exactly how a scored question's own query reached the arm.

    `allow_sql` is the escape hatch, and it is recorded in the run manifest so
    a later reader can see the gate was waived rather than passed.
    """
    lines = [line for line in (rules_text or "").splitlines() if line.strip()]
    if len(lines) < PASTED_SQL_MIN_LINES:
        return Gate("pasted SQL", True,
                    f"{len(lines)} rule lines, too few to judge a share")
    with_sql = [line for line in lines if _SELECT.search(line)]
    share = len(with_sql) / len(lines)
    detail = f"{len(with_sql)} of {len(lines)} rule lines contain a SELECT ({share:.0%})"
    if share <= PASTED_SQL_SHARE:
        return Gate("pasted SQL", True, detail)
    if allow_sql:
        return Gate("pasted SQL", True, detail + ", waived by --allow-sql")
    return Gate("pasted SQL", False,
                detail + f", above {PASTED_SQL_SHARE:.0%}. SQL belongs in the "
                         "example bank, where the leak gate reads it and the "
                         "gold gate runs it. Pass --allow-sql to override")


def template_twins(scored: list[tuple[str, str]],
                   items: list[tuple[str, str]]) -> Gate:
    """Leak part (b): an item that is the same query shape as a scored question.

    Part (a) catches a scored question copied into the context. This catches
    the subtler one: an example that is not the same question but compiles to
    the same query once its literals are masked. Answering the scored question
    is then a substitution, not text-to-SQL. The split rule comes from
    Finegan-Dollak's query split.

    `scored` and `items` are both (id, sql). Nothing is moved or dropped here:
    a sealed split must not be reshuffled by a later check, so this reports the
    pairs and lets the caller drop the item.
    """
    by_shape: dict[str, list[str]] = {}
    for identifier, sql in scored:
        shape = normalize_sql(sql)
        if shape:
            by_shape.setdefault(shape, []).append(identifier)

    twins = [(item_id, by_shape[normalize_sql(sql)])
             for item_id, sql in items
             if normalize_sql(sql) in by_shape]
    if not twins:
        return Gate("template twin", True,
                    f"none of {len(items)} items share a query shape with the "
                    f"{len(scored)} scored questions")
    named = ", ".join(f"{item} matches {'/'.join(ids)}" for item, ids in twins[:5])
    more = f" and {len(twins) - 5} more" if len(twins) > 5 else ""
    return Gate("template twin", False,
                f"{len(twins)} items are the same query shape as a scored "
                f"question, so answering it is substitution: {named}{more}")


def holdout_reads(reads: int, forced: bool = False) -> Gate:
    """The held-out questions are read by one arm pair, once, per round.

    Read them twice and they are no longer held out: the second read is chosen
    knowing the first, which is what makes the number worth anything. K repeats
    of one pair count as one read.

    `forced` is `--force`. It passes, and it says so, so nobody later reads the
    page as an untouched holdout.
    """
    if reads == 1:
        return Gate("holdout reads", True, "read once, by one arm pair")
    if reads == 0:
        return Gate("holdout reads", False,
                    "the held-out questions were never read, so there is no "
                    "verdict to give")
    if forced:
        return Gate("holdout reads", True,
                    f"read {reads} times, forced. The later reads were chosen "
                    "knowing the earlier ones, so this is no longer a holdout")
    return Gate("holdout reads", False,
                f"read {reads} times in one round. The second read is chosen "
                "knowing the first. Pass --force to record it as spent anyway")


def model_canary(now, before) -> Gate:
    """The control arm is re-run each round; has the model moved under us?

    `LATEST_GA_MODEL` is an alias and the answer carries no version string, so
    nothing in a response says the model changed. The only detector is the arm
    that should not have changed. Both arguments are `metrics.Proportion`.

    A shift is flagged when the new point sits outside the old interval. This
    detects a move; it cannot name it. That limit is the reason the gate reads
    "the alias may have moved" rather than asserting it did.
    """
    if before is None:
        return Gate("model canary", True,
                    "no earlier control run to compare against; this round "
                    "becomes the baseline")
    inside = before.low <= now.point <= before.high
    shift = (now.point - before.point) * 100
    detail = (f"control arm {now.point:.1%} against {before.point:.1%} "
              f"[{before.low:.1%}, {before.high:.1%}] last round, {shift:+.1f} points")
    if inside:
        return Gate("model canary", True, detail)
    return Gate("model canary", False,
                detail + ". The arm that should not have changed did, so the "
                         "model alias may have moved and this round is not "
                         "comparable to the last one")


def ground_truth_audit(sample: int, disagreements: int,
                       total_golds: int) -> Gate:
    """Did a person re-derive a sample of the answer key by hand?

    The gate is whether the audit happened, not whether it was clean. The rate
    is printed either way and belongs on the page, because an answer key nobody
    checked can be wrong at a rate that dwarfs every lift measured against it:
    an audit of BIRD Mini-Dev found 52.8% of its golds wrong.
    """
    if sample <= 0:
        return Gate("ground-truth audit", False,
                    f"no gold was re-derived by hand out of {total_golds}. "
                    f"Check {min(AUDIT_SAMPLE, total_golds)} and record the "
                    "disagreements; an unchecked answer key can be wrong more "
                    "often than any lift measured against it")
    rate = disagreements / sample
    return Gate("ground-truth audit", True,
                f"{disagreements} of {sample} re-derived golds disagreed "
                f"({rate:.0%}); {total_golds} golds in the set")


def export_round_trip(sentinel: str, answer_text: str) -> Gate:
    """Did the tail of the longest exported field survive to the answer?

    No size limit is documented for any Context field, so the only honest way
    to know a long overview was not cut is to put an invented sentence at its
    end and ask a question that needs it. A silent truncation otherwise looks
    exactly like context that did not help.
    """
    if not sentinel:
        return Gate("export round-trip", False,
                    "no sentinel was placed, so a silently cut field would "
                    "look like context that did not help")
    if sentinel.lower() in (answer_text or "").lower():
        return Gate("export round-trip", True,
                    "the sentence at the end of the longest exported field "
                    "came back in the answer")
    return Gate("export round-trip", False,
                "the sentence at the end of the longest exported field did not "
                "come back. Either the field was cut in transit or the agent "
                "ignored its tail; both make this round's context unknown")


def summary(gates: list[Gate]) -> str:
    """Every gate, failures first, so the reason is the first line read."""
    ordered = [g for g in gates if not g.passed] + [g for g in gates if g.passed]
    lines = [g.line() for g in ordered]
    failed = sum(1 for g in gates if not g.passed)
    lines.append("" if not failed else
                 f"\n{failed} gate(s) failed; the scores are not shown.")
    return "\n".join(lines)


# Above this share of answers carrying no SQL at all, nothing was measured.
# Set from the run that found it: 55 of 78 answers came back empty because
# the agent had been handed the wrong tables, and the page read UNDERPOWERED
# -- which sends a reader off to find more questions when the fix was a
# table list. Half is generous: a working agent writes SQL for nearly
# everything, and a run where a third is empty is already suspect.
EMPTY_ANSWER_LIMIT = 0.50


def answers_with_no_sql(empty: int, total: int,
                        limit: float = EMPTY_ANSWER_LIMIT) -> Gate:
    """An agent that wrote nothing was not measured, whatever the lift says.

    This is the gate that was missing. One round asked 78 questions across
    two arms and **55 of the answers carried no SQL at all** -- no generated
    query, no BigQuery job id, no citations, not a character of reasoning --
    because the context spec named 27 tables and the questions needed 65.
    Both arms failed identically, the lift was +0.0 on both halves, and the
    verdict read UNDERPOWERED.

    That verdict is not merely unhelpful, it is misdirecting: it names the
    sample size as the problem and the sample size was fine. Reading the
    per-question rows took minutes and said the real thing at once. A
    verdict that offers a plausible wrong cause is worse than one that says
    nothing, because the reader acts on it.

    So it is a gate, and a failed gate withholds the scores rather than
    annotating them. An empty answer is counted and printed even when the
    gate passes, because "3 of 78 answers were empty" is evidence and
    "empty: ok" is not.
    """
    if total <= 0:
        return Gate("answers with no SQL", True, "no answers to check")
    share = empty / total
    detail = (f"{empty} of {total} answers carried no SQL at all "
              f"({share:.0%})")
    if share < limit:
        return Gate("answers with no SQL", True, detail)
    return Gate(
        "answers with no SQL", False,
        f"{detail}, so the agent was not measured and the lift is 0.0 for a "
        "reason that has nothing to do with the context. An agent returns "
        "nothing when it cannot reach the tables the question is about, when "
        "the request is refused, or when it asks a clarifying question "
        "instead. Read the per-question rows: `error_class` says which. "
        "Check the table-coverage gate first")


# Below this share of reserved questions sharing ground with the working
# half, no context written from one side can reach the other and a reserved
# lift is not possible. Measured on the run that found it: the halves shared
# 9 tables of 63, and only 9 of 20 reserved questions touched a table the
# working half touched at all.
SHARED_GROUND_FLOOR = 0.50



def resolvable_lift(n_holdout: int, target_points: float = DEFAULT_TARGET_POINTS,
                    break_rate: float = DEFAULT_BREAK_RATE,
                    forced: bool = False) -> Gate:
    """Can this reserved half resolve the effect being paid for?

    The gate that was missing, and its absence cost five paid rounds on one
    enterprise estate. Each reported NOT PROVEN honestly. None of them could
    have reported anything else, and nothing said so.

    A round is scored on the questions that DISAGREE between the two arms.
    A question right in both arms carries no information and neither does one
    wrong in both. With `d` disagreements all falling one way the exact
    two-sided p is `2 * (1/2)^d`, so **six unanimous disagreements is the
    cheapest significant result that exists** and five cannot reach 0.05 in
    any arrangement.

    That is what this refuses on: the EXPECTED number of questions that move
    at all. It is arithmetic about the design rather than a guess about the
    writing, which is why it can be checked before anything is bought. The
    2026-09-12 round on that estate produced 1 fixed and 3 broke -- four
    disagreements, p = 0.6250 -- against a reserved half of 29 whose expected
    count at the target was 3.2.

    The power figure is printed whether the gate passes or fails, because
    "this set resolves a lift above 30 points" is evidence and "ok" is not.

    `target_points` is the effect the operator is paying to see. Its default
    is the largest deployable written-context effect this framework has
    measured on an enterprise warehouse: the worked-example rung of
    `docs/research/README.md`. Declaring a bigger target is a commitment, not a
    loophole -- a round that declares 50 points and finds 6 was wrong about
    its context, not about its set.
    """
    name = "resolvable lift"
    if n_holdout <= 0:
        return Gate(name, True, "no reserved questions to check")
    expected = expected_disagreements(n_holdout, target_points, break_rate)
    floor = floor_on_disagreements()
    smallest = smallest_resolvable(n_holdout, break_rate)
    reached = power(n_holdout, *rates_for(target_points, break_rate))
    resolves = (f"resolves a lift above {smallest:.0f} points"
                if smallest else "resolves no lift at any size")
    detail = (f"{n_holdout} reserved questions, targeting +{target_points:g} "
              f"points: {expected:.1f} questions expected to move, "
              f"{reached:.0%} power; {resolves}")
    if expected < floor and forced:
        return Gate(name, True, detail + ". FORCED with --force: this round "
                    "cannot reach a verdict, and any p it prints is a "
                    "property of the set rather than of the context")
    if expected < floor:
        return Gate(name, False,
                    detail + f". Fewer than {floor} questions are expected to "
                    "move, and " + str(floor) + " unanimous is the cheapest "
                    "significant result that exists, so this round cannot "
                    "reach a verdict however the context is written. This is "
                    "a property of the SET, not the writing: add questions, "
                    "or declare the larger effect you are actually targeting "
                    "with --target-lift")
    return Gate(name, True, detail)

def shared_ground(curate_tables: set[str], holdout_tables: dict[str, set[str]],
                  floor: float = SHARED_GROUND_FLOOR) -> Gate:
    """Can context written from the working half reach the reserved half?

    A rule written from a working question can only help a reserved question
    if the two are about the same data. Where they are not, nothing carries
    across however well the rule is written, and a reserved lift is not
    merely unlikely -- it is unavailable.

    Measured on one estate: the 19 working questions touched 41 tables, the
    20 reserved questions touched 31, and the halves shared 9. **Eleven of
    the twenty reserved questions were about tables the working half never
    touched.** Three rounds reported a reserved +0.0 from that arrangement
    as though it said something about the context.

    This is a property of the SPLIT, not of the context, which is why it is
    checked before anything is paid for and why the message says so. The
    remedy is a split that keeps table families on both sides, or more
    questions, not better writing.
    """
    if not holdout_tables:
        return Gate("shared ground", True, "no reserved questions to check")
    reachable = {gid for gid, tables in holdout_tables.items()
                 if tables & curate_tables}
    share = len(reachable) / len(holdout_tables)
    detail = (f"{len(reachable)} of {len(holdout_tables)} reserved questions "
              f"share a table with the working half ({share:.0%})")
    if share >= floor:
        return Gate("shared ground", True, detail)
    return Gate(
        "shared ground", False,
        f"{detail}, so {len(holdout_tables) - len(reachable)} of them cannot "
        "be reached by ANY context written from the working half, however "
        "good it is. A reserved lift is not unlikely here, it is unavailable. "
        "This is a property of the split, not of the context: re-split so "
        "table families fall on both sides, or mine more questions. Writing "
        "better rules will not change it")


def stored_example_lookups(held_out: list[str], curate: list[str]) -> Gate:
    """A held-out question answered by running a stored example verbatim.

    The Conversational Analytics API can match a worked example and execute it
    as written instead of writing SQL. On a working question that is fine and
    expected: the example was built from that question. On a held-out question
    it means the answer was handed over, and every number in the round is then
    a number about a lookup table.

    So a lookup on the holdout fails the round, and a lookup on the working
    questions is counted and printed. The count matters on its own: a treatment
    arm that answers most working questions by lookup has not been measured as
    a text-to-SQL system at all, whatever its accuracy says.
    """
    if held_out:
        names = ", ".join(sorted(set(held_out))[:5])
        return Gate(
            "stored-example lookups", False,
            f"{len(held_out)} held-out answer(s) ran a stored example instead "
            f"of writing SQL ({names}). The answer was handed over, so the "
            "lift is a lift on a lookup table. Rebuild the example bank: "
            "build-examples refuses an example that carries a held-out "
            "question, and one got through")
    return Gate(
        "stored-example lookups", True,
        f"0 held-out answers ran a stored example; {len(curate)} working "
        "answer(s) did, and those are excluded from the lift rather than "
        "scored as text-to-SQL")


# The link sources the export carries. A USER link is a person's own
# statement about the data; TABLE_CONSTRAINTS is the warehouse's declared
# foreign key. Both are things the estate asserted before any measurement.
EXPORTED_LINK_SOURCES = ("USER", "TABLE_CONSTRAINTS")


def join_links(links: list[dict], golds_ran_at=None) -> Gate:
    """Join relationships the catalog mined for itself, and when.

    Knowledge Catalog mines join links from query history with no scan and no
    asking. Every SQL statement the gold gate executes is query history. So
    the answer key can teach the catalog a join, the catalog can serve that
    join to the treatment arm, and the round then measures the answer key
    against itself.

    The threat is NOT that a mined link exists. On a customer estate those
    come from the business's own analysts and are ordinary context. The
    threat is a link mined AFTER the golds were executed, which is the only
    kind that can carry the answer key. So time is the discriminator, and
    `golds_ran_at` is when the gold gate last ran.

    Each link is `{"source": str, "created_at": datetime | None, "name": str}`.
    Without `golds_ran_at` this reports and does not refuse, because a tool
    that cannot tell the two apart must not accuse.
    """
    mined = [x for x in links
             if str(x.get("source", "")).upper() not in EXPORTED_LINK_SOURCES]
    if not links:
        return Gate("join links", True,
                    "no join relationships on these tables, so none can carry "
                    "the answer key")
    carried = len(links) - len(mined)
    if golds_ran_at is None:
        return Gate("join links", True,
                    f"{len(links)} link(s): {carried} from a person or a "
                    f"declared key, {len(mined)} mined by the catalog. Not "
                    "refused: without the time the golds ran, a link mined "
                    "from the estate's own traffic cannot be told from one "
                    "mined from the answer key")

    after = [x for x in mined
             if x.get("created_at") is not None and x["created_at"] >= golds_ran_at]
    if after:
        names = ", ".join(sorted(x.get("name", "?").split("/")[-1] for x in after)[:3])
        return Gate("join links", False,
                    f"{len(after)} join link(s) were mined after the golds ran "
                    f"({names}). The catalog learns joins from query history "
                    "and the gold gate's own statements are query history, so "
                    "these may carry the answer key. Bill the gold executions "
                    "to a separate project, or delete the links and re-run")
    return Gate("join links", True,
                f"{len(links)} link(s): {carried} exported, {len(mined)} mined "
                "by the catalog and all of them older than the golds, so none "
                "can carry the answer key")


# A SELECT or WITH at the start of a line, taken to the next blank line or to
# the next line that is less indented. Deliberately simple: this feeds a shape
# comparison, and a chunk that grabbed a little too much normalises to a shape
# that matches nothing, which is a missed twin and not a false accusation.
_SQL_START = re.compile(r"^(\s*)(?:sql:\s*)?((?:SELECT|WITH)\b.*)$",
                        re.IGNORECASE | re.MULTILINE)


def sql_in_text(text: str) -> list[str]:
    """Every SQL statement embedded in free text, for the shape check.

    The served arm carries its worked examples as prose inside one system
    instruction rather than in the structured `example_queries` field, so
    `arm.examples` is empty and the template-twin check had nothing to look
    at: it printed "none of 0 items" and passed on an arm that was carrying
    22 queries. A gate that cannot see the thing it guards is worse than no
    gate, because it prints a pass.

    Not a SQL parser, and it does not need to be. The output is fed to
    `normalize_sql`, which masks literals and compares shapes, so taking a
    line too many produces a shape that matches nothing. The failure mode is
    a missed twin, never a false accusation.
    """
    found = []
    lines = (text or "").splitlines()
    for match in _SQL_START.finditer(text or ""):
        indent = len(match.group(1))
        start = text[:match.start()].count("\n")
        block = [match.group(2)]
        for line in lines[start + 1:]:
            if not line.strip():
                break
            # Stop at the next YAML key at the same level or further out:
            # the statement has ended and what follows is the next field.
            outdented = len(line) - len(line.lstrip()) <= indent
            looks_like_a_key = ":" in line.strip()[:40]
            if outdented and looks_like_a_key:
                break
            block.append(line.strip())
        found.append(" ".join(block))
    return found


def table_coverage(golds_tables: dict[str, set[str]],
                   arm_tables: set[str],
                   floor: float = TABLE_COVERAGE_FLOOR) -> Gate:
    """Can the agent even reach the data each question is about?

    This is the cheapest gate there is and it was the last one written,
    which is the lesson. One round on a customer estate spent 37 minutes,
    78 API calls and $2 to produce nothing: the spec named 27 tables, the
    39 golds needed 63, five of them overlapped, and **one** gold of 39 had
    every table it needed. The agent returned no SQL for 55 of 78 questions
    because it was asked about data it had never been handed, both arms
    failed identically, the lift was +0.0, and the verdict read UNDERPOWERED
    -- which tells a reader to go and find more questions.

    Comparing two sets of table names costs nothing and needs no network.
    There is no excuse for learning this after the calls rather than before.

    `golds_tables` maps a gold id to the table names its SQL uses, already
    normalised to the form the arm uses. `arm_tables` is what the arm
    carries. A gold naming no table at all is not counted either way: that
    is a different defect and `gate.py` owns it.

    Failing is deliberately not the same as "every gold covered". A few
    uncovered golds dilute a real measurement and a person may accept that
    knowingly; below `floor` there is nothing to measure.
    """
    scored = {gid: tables for gid, tables in golds_tables.items() if tables}
    if not scored:
        return Gate("table coverage", True,
                    "no gold names a table, so there is nothing to cover")
    covered = {gid for gid, tables in scored.items() if tables <= arm_tables}
    share = len(covered) / len(scored)
    missing: set[str] = set()
    for gid, tables in scored.items():
        missing |= tables - arm_tables
    detail = (f"{len(covered)} of {len(scored)} golds have every table they "
              f"need in the arm ({share:.0%}); the arm carries "
              f"{len(arm_tables)}")
    if share >= floor:
        return Gate("table coverage", True, detail)
    named = ", ".join(sorted(missing)[:5])
    more = len(missing) - 5
    return Gate(
        "table coverage", False,
        f"{detail}. {len(missing)} table(s) the golds need are not in the "
        f"arm: {named}" + (f" and {more} more" if more > 0 else "")
        + ". An agent asked about a table it was not given returns no SQL, "
        "so both arms fail the same way and the lift is 0.0 for a reason "
        "that has nothing to do with the context. Add these tables to the "
        "spec, or keep only the golds the spec covers")


# Whitespace-collapsed, lower case: a prediction that differs from the gold
# only by indentation is the same statement, and an agent copying the answer
# key would not reproduce its formatting.
def _same_statement(left: str, right: str) -> bool:
    """Two SQL strings that differ only in formatting.

    The semicolon comes off BEFORE the final strip, not after. Written the
    other way round, `"select 1 ;"` normalises to `"select 1 "` with a
    trailing space and never matches `"SELECT 1"` -- so the check silently
    reported 0% on exactly the case it exists to catch. Found by trying it.
    """
    import re as _re

    def flat(sql: str) -> str:
        one_line = _re.sub(r"\s+", " ", (sql or "")).lower()
        return one_line.strip().rstrip(";").strip()

    return bool(flat(left)) and flat(left) == flat(right)


def predictions_are_not_the_answer_key(
        pairs: list[tuple[str, str]], allow: bool = False) -> Gate:
    """Refuse an arm whose predictions reproduce the gold SQL.

    `pairs` is (predicted SQL, gold SQL) per scored answer.

    THE HOLE THIS FILLS, found on 2026-09-14 by reading a pull request rather
    than by anything failing. Every other guard here looks at the CONTEXT:
    `pasted_sql` reads the rules text, `template_twins` reads the worked
    examples, and `leak` reads the bundle handed to the agent. NOTHING read
    what the agent returned. So an agent that emits the answer key verbatim
    scored 100%, produced a +75 point lift at p = 1.49e-08, and passed every
    gate including the leak gate -- which was true, because the leak was not
    in the context.

    A LEGITIMATE AGENT SOMETIMES MATCHES, which is why this is not a
    zero-tolerance check. "How many teams are there" has one obvious
    statement, and a gold written by a person and SQL written by a model will
    coincide on it. So the share is reported and a partial match WARNS.

    100% REFUSES, and that threshold needs no measurement to justify: an arm
    that reproduces the answer key on every single question is not an agent
    answering questions, it is a copy of the key. It is the one case that is
    definitionally not a measurement rather than merely suspicious.

    `allow` is for a deliberate positive-control fixture, which is a real
    thing to want -- but it has to be asked for, in writing, by whoever
    built it.
    """
    scored = [(p, g) for p, g in pairs if (p or "").strip()]
    if not scored:
        return Gate("predictions vs answer key", True,
                    "no answer carried SQL, so there is nothing to compare")
    same = [1 for p, g in scored if _same_statement(p, g)]
    share = len(same) / len(scored)

    if allow:
        return Gate(
            "predictions vs answer key", True,
            f"{len(same)} of {len(scored)} predictions ({share:.0%}) are the "
            f"gold statement, ALLOWED on purpose. A positive-control arm "
            f"measures the pipeline and not the agent, and its lift is true "
            f"by construction")
    if share >= 1.0:
        return Gate(
            "predictions vs answer key", False,
            f"every one of {len(scored)} predictions is the gold statement. "
            f"This arm is a copy of the answer key, not an agent answering "
            f"questions, so its accuracy and any lift computed from it are "
            f"true by construction. Pass the positive-control flag if that is "
            f"deliberate")
    return Gate(
        "predictions vs answer key", True,
        f"{len(same)} of {len(scored)} predictions ({share:.0%}) match the "
        f"gold statement"
        + ("" if not same else
           ". Some coincidence is normal -- a simple question has one obvious "
           "statement -- but a rising share is worth a look"))
