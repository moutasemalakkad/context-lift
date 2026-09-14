# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The human review between mined candidates and golds, as a stage.

`mine` writes candidates and a person turns them into golds. That step is the
one no command can rescue, and on one estate it only happened because someone
asked for it: the drafts carried the SQL's vocabulary, 13 of 22 gate-clean
candidates were a BI tool's filter-dropdown queries, and nothing between mine
and gate would have said so.

Two calls. `prepare` writes the review sheet (every candidate column plus
`wording`, `keep`, `reason`, `source`) and REVIEW.md beside it: the rule
first, one worked example of the trap, the funnel that produced the
worklist. `finish` refuses while any row has no keep decision, writes the
golds from the kept rows, and counts the rejections by reason so "16 dropped"
is reported as what they were.

`draft` sits between the two when nobody is there to review on day one: a
model accepts or rejects each undecided row with a reason from REASONS. It
never writes SQL or question text, the rows it decided carry
`source = model-reviewed:<model>`, and `finish` counts them apart from the
rows a person decided, so a verdict can say how much of its key a model
accepted (`answer_key_caveat`).

Exports: prepare, finish, draft, answer_key_caveat, ReviewNotFinished.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import json
import pathlib
from collections import Counter
from typing import Callable

from .gate import WHOLE_ROW
from .leakage import check_leakage

YES = {"yes", "y", "true", "1", "keep"}
NO = {"no", "n", "false", "0", "drop", "reject"}
EXTRA_COLUMNS = ("wording", "keep", "reason", "comment", "source",
                 "model_keep", "model_reason", "sql_sha256")
# `sql_sha256` is written by prepare and read by finish. It is how finish
# knows the SQL of a row was edited after the gate read it: an edited gold
# is no longer covered by the gate that passed it.

# `model_keep` and `model_reason` hold what a MODEL said. `keep` and `reason`
# stay the person's, always. Before this split `--draft` wrote straight into
# `keep`, so after a model pass nobody could tell a model decision from a
# human one and the two could never be compared. That comparison is what the
# project is blocked on: on one estate gemini-2.5-pro kept 45 of 177 rows
# (25.4%) and gemini-2.5-flash kept 94 (53.1%), and which is right decides
# whether a round can resolve anything at all.
MODEL_COLUMNS = ("model_keep", "model_reason")

# Free text, no schema, no dropdown. An analyst rejecting a question does not
# just filter it -- they explain the business, and that explanation IS the
# written context this framework exists to measure. With no column it lands
# nowhere and is thrown away.
COMMENT_COLUMN = "comment"

# The order a person reads, not the order the file grew in. `gold_sql` used to
# be column 3 of 14, so a reviewer opening a spreadsheet scrolled past a wall
# of SQL to reach the one cell they had to fill in.
COLUMN_ORDER = (
    # what the reviewer decides
    "id", "question", "keep", "comment", "reason", "wording",
    # what the model thought, beside it and never replacing it
    "model_keep", "model_reason",
    # the evidence they judge it on
    "gold_sql", "gate_status", "gate_detail", "table", "times_seen",
    # bookkeeping
    "source", "sql_sha256",
)


def ordered_columns(columns) -> list[str]:
    """`columns` in reading order, with anything unknown kept at the end.

    An unknown column is never dropped. A sheet carries whatever the candidate
    file carried, and quietly losing one because this list had not heard of it
    would destroy a reviewer's own notes.
    """
    known = [c for c in COLUMN_ORDER if c in columns]
    return known + [c for c in columns if c not in COLUMN_ORDER]

# The only reasons a model reviewer may give, each with its meaning as
# REVIEW.md prints it. `matches` is the one reason for a yes; every other
# reason is a no. A reason outside this list leaves the row for a person.
REASONS = {
    "matches": "the question asks for what the SQL returns",
    "does_not_match": "the question asks for something the SQL does not return",
    "dashboard_shaped": "extra grouping columns, HAVING <> 0, or indicator columns "
                        "the question never asked for",
    "dropdown_or_metadata": "a filter value list or a metadata probe, not a business question",
    "duplicate": "the same question as another row",
    "ambiguous_question": "the question can be read more than one way",
    "not_a_question": "the text is not a question a person would ask",
}
# `source` of a row the model decided, followed by the model name; a person
# who checks such a row sets it to PERSON_SOURCE
# A row decided by the gate rather than by a person or a model. Distinct from
# both, so `finish` can report the three separately and nobody reads a
# machine's refusal as a review.
GATE_SOURCE = "gate-refused"

MODEL_SOURCE_PREFIX = "model-reviewed:"
PERSON_SOURCE = "reviewed"

# How close two question texts must be before finish names them as one
# question. difflib's ratio on the normalised text: a field pair worded
# "in each order state" against "by order state" scores 0.88, and two
# unrelated questions from the same warehouse score 0.28. This catches
# rewording, not rethinking: "how many open purchase orders are there"
# against "what is the number of open purchase orders" scores 0.57 and is
# missed, which is why the printed line says a reviewer still has to look.
NEAR_DUPLICATE_RATIO = 0.85


class ReviewNotFinished(SystemExit):
    """Raised by `finish` while rows still lack a keep decision."""


def sql_stamp(sql: str) -> str:
    """A short fingerprint of one SQL statement, for spotting an edit.

    Leading and trailing spaces are stripped first, so re-saving the sheet in
    a spreadsheet does not read as an edit. Everything else counts, including
    a changed project name, which is the case this was written for.
    """
    return hashlib.sha256((sql or "").strip().encode()).hexdigest()[:12]


def _already_refused(row: dict) -> dict:
    """`keep=no` for a row whose SQL the gate refused, else `{}`.

    The sheet used to hand every candidate to the reviewer with an empty
    `keep`, including the ones the gate had already rejected. Measured on one
    estate: of 344 candidates a model reviewer was asked to judge, it kept 24
    whose SQL cannot run at all -- 8 refused by an org policy, 5 naming a
    dropped table, 3 returning no rows, 2 over the byte ceiling, 2 in another
    project -- and left 16 more undecided, every one of them defective.

    Three costs, and the third is the one that bites. Review budget spent on
    rows that can never be golds. A reviewer's attention spent the same way.
    And a kept count that reads as the answer-key size: 63 kept became 39
    after re-gating, and nothing in between said so.

    Asking "does this question match this SQL" about a query that returns a
    403 is not a question anybody can answer. The row stays in the sheet,
    because a reviewer should see what was dropped and why -- it is the
    decision that is made for them, not the information withheld.
    """
    status = (row.get("gate_status") or "").strip()
    if not status or status == "ok" or status.startswith("ok ("):
        return {}
    return {"keep": "no", "reason": "gate_defect",
            "source": f"{GATE_SOURCE}:{status.split('+')[0]}"}


def prepare(candidates_path: str | pathlib.Path, out_dir: str | pathlib.Path) -> dict:
    """Write review.csv and REVIEW.md into out_dir; return the funnel counts."""
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(candidates_path, newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        rows = list(reader)
    for column in EXTRA_COLUMNS:
        if column not in columns:
            columns.append(column)
    columns = ordered_columns(columns)
    sheet = out_dir / "review.csv"
    with sheet.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            decided = _already_refused(row)
            writer.writerow({**{c: "" for c in columns}, **row,
                             **decided,
                             "source": decided.get("source")
                             or row.get("source") or "mined",
                             "sql_sha256": sql_stamp(row.get("gold_sql", ""))})
    counts = _funnel(rows)
    counts["pre_rejected"] = sum(1 for row in rows if _already_refused(row))
    # Said out loud in the document, because deciding rows for a reviewer
    # without telling them is worse than asking about rows they cannot judge.
    (out_dir / "REVIEW.md").write_text(_document(counts, sheet.name))
    return counts


def finish(sheet_path: str | pathlib.Path, out_path: str | pathlib.Path) -> dict:
    """Golds from the kept rows. Refuses while any row is undecided.

    The counts say who accepted each gold: `model_reviewed` rows carry a
    `model-reviewed:<model>` source, every other kept row counts as
    `person_reviewed`, and `reviewer_model` names the model (the most
    frequent one when a sheet was drafted twice).

    Three more counts guard the answer key. `edited_sql` lists the kept ids
    whose SQL no longer matches the fingerprint prepare wrote, so the gate
    that passed them did not read this SQL. `clean_rejected` counts the
    gate-clean rows a reviewer threw out, so nobody sizes a key from the
    gate's pass count. `duplicate_questions` groups the kept ids that ask the
    same question in words while their SQL differs: one question cannot have
    two right answers, and nobody reading one row at a time can see it.
    """
    with open(sheet_path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    undecided = [r for r in rows if (r.get("keep") or "").strip().lower() not in YES | NO]
    if undecided:
        raise ReviewNotFinished(
            f"review not finished: {len(undecided)} of {len(rows)} rows have no keep decision "
            f"(yes or no in the keep column of {sheet_path}). Nothing written.")
    kept = [r for r in rows if (r.get("keep") or "").strip().lower() in YES]
    rejected = Counter((r.get("reason") or "(no reason given)").strip()
                       for r in rows if (r.get("keep") or "").strip().lower() in NO)
    # A row whose SQL was edited in the sheet is no longer the SQL the gate
    # read. One estate rewrote a project name to fix a cross-project gold,
    # which is a fair edit, but the file then carries a gold nothing checked.
    edited_sql = [row["id"] for row in kept
                  if (row.get("sql_sha256") or "").strip()
                  and (row.get("sql_sha256") or "").strip()
                  != sql_stamp(row.get("gold_sql", ""))]
    # How many golds the gate passed and a person still threw out. On one
    # estate that was 9 of 25: the gate's pass count is not the key size.
    clean = [row for row in rows if (row.get("gate_status") or "ok").strip() in ("", "ok")]
    clean_rejected = sum(1 for row in clean
                         if (row.get("keep") or "").strip().lower() in NO)
    # Two kept rows asking the same question in words cannot both be right
    # when their SQL differs. A reviewer who reads one row at a time cannot
    # see this, and a model reviewer judging one row at a time cannot either.
    asked = [(row["id"], _normalised_question(row)) for row in kept]
    duplicate_questions = _same_question_groups([(i, t) for i, t in asked if t])
    reviewer_models: Counter = Counter()
    out_path = pathlib.Path(out_path)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "question", "gold_sql", "table", "source"])
        for row in kept:
            wording = (row.get("wording") or "").strip()
            source = (row.get("source") or "mined").strip()
            if wording and wording != (row.get("question") or "").strip() and source == "mined":
                source = "mined_rewritten"
            if source.startswith(MODEL_SOURCE_PREFIX):
                reviewer_models[source[len(MODEL_SOURCE_PREFIX):]] += 1
            writer.writerow([row["id"], wording or row.get("question", ""),
                             row.get("gold_sql", ""), row.get("table", ""), source])
    model_reviewed = sum(reviewer_models.values())
    return {"kept": len(kept), "rejected": dict(rejected), "total": len(rows),
            "model_reviewed": model_reviewed,
            "person_reviewed": len(kept) - model_reviewed,
            "reviewer_model": (reviewer_models.most_common(1)[0][0]
                               if reviewer_models else None),
            "edited_sql": edited_sql, "duplicate_questions": duplicate_questions,
            "clean": len(clean), "clean_rejected": clean_rejected}


# --- the model draft ---------------------------------------------------------

_PROMPT = """You are checking candidate rows for a text-to-SQL answer key.
Each row is a question and the SQL taken from the query history. Decide, for
every row, whether the question asks for exactly what the SQL returns. You may
only accept or reject a row. Never rewrite the question or the SQL.

Give exactly one reason per row, from this list:
{reasons}

Accept a row only with the reason "matches". Every other reason rejects it.
The gate line is the result of running the SQL; a defect there is usually a
reason to reject.
{accepted}
{rows}
Answer with a JSON array only, one object per row, in this shape:
[{{"id": "<row id>", "keep": "yes" or "no", "reason": "<one reason>"}}]
"""

_ROW = """### {id}
question: {question}
SQL: {sql}
gate: {gate}
"""


def output_token_cap(batch: int) -> int:
    """The output tokens one draft call may use.

    One decision is about 30 tokens of JSON. The old figure, 200 + 60 a row,
    counted only those and a batch of 10 still came back cut off after 9: on a
    thinking model the reasoning is billed as output and spends the same
    budget, and one run used 1,658 thinking tokens against 1,526 of answer.
    So the margin has to cover the thinking too.
    """
    return 1500 + 120 * batch


def build_review_prompt(rows: list[dict], accepted: list[str] | tuple[str, ...] = ()) -> str:
    """One batch of undecided rows as the prompt. The question shown is the
    `wording` when a person wrote one, else the candidate's question.

    `accepted` is every question already kept for this key. A model judging
    one row at a time cannot see that two rows ask the same thing: in a field
    review, 4 of the 13 rows a model kept and a person threw out were
    duplicates. Showing what is already in the key is what makes the
    `duplicate` reason reachable at all.
    """
    reasons = "\n".join(f"- {name}: {meaning}" for name, meaning in REASONS.items())
    shown = []
    for row in rows:
        gate = (row.get("gate_status") or "ok").strip()
        detail = (row.get("gate_detail") or "").strip()
        shown.append(_ROW.format(id=row["id"], question=_question_of(row),
                                 sql=(row.get("gold_sql") or "").strip(),
                                 gate=f"{gate} ({detail})" if detail else gate))
    already = ""
    if accepted:
        listed = "\n".join(f"- {question}" for question in accepted)
        already = ("\nThese questions are already in the answer key:\n" + listed
                   + '\nReject a row that asks the same thing as one of these with the '
                     'reason "duplicate", however differently it is worded.\n')
    return _PROMPT.format(reasons=reasons, accepted=already, rows="\n".join(shown))


def accepted_questions(rows: list[dict]) -> list[str]:
    """The question text of every row already kept, in sheet order."""
    return [text for text in (_question_of(row) for row in rows
                              if (row.get("keep") or "").strip().lower() in YES) if text]


def _complete_objects(text: str) -> list[dict]:
    """Every `{...}` in `text` that parses on its own, in order.

    Used when the array around them never closed. Brace counting rather than a
    regular expression, because a reason string may contain a brace and a
    regular expression would cut the object at the wrong place.
    """
    found: list[dict] = []
    depth = 0
    start = -1
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    item = json.loads(text[start:index + 1])
                except ValueError:
                    continue
                if isinstance(item, dict):
                    found.append(item)
    return found


def parse_decisions(text: str, ids: set[str]) -> dict[str, tuple[str, str]]:
    """id -> (keep, reason) for every row the model decided with a known
    reason. A yes with a rejecting reason, an unknown reason, an id the
    prompt never showed, or text with no JSON array in it is not a
    decision: the row stays for a person. The array may sit in a code fence.

    A cut-off array still counts for the rows that arrived. The model answers
    with one object per row, and an output cap can stop it part way through:
    a batch of 10 came back with 9 complete decisions and a half-written
    tenth, and reading the whole array at once threw all 9 away and reported
    0 of 40 decided across four paid calls. Every complete object is read
    whether or not the array ever closes.
    """
    text = text or ""
    start = text.find("[")
    if start < 0:
        return {}
    end = text.rfind("]")
    items = None
    if end > start:
        try:
            items = json.loads(text[start:end + 1])
        except ValueError:
            items = None
    if items is None:
        items = _complete_objects(text[start:])
    decided: dict[str, tuple[str, str]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        row_id = str(item.get("id", "")).strip()
        keep = str(item.get("keep", "")).strip().lower()
        reason = str(item.get("reason", "")).strip()
        if row_id not in ids or keep not in ("yes", "no") or reason not in REASONS:
            continue
        if (keep == "yes") != (reason == "matches"):
            continue
        decided[row_id] = (keep, reason)
    return decided


def draft_plan(sheet_path: str | pathlib.Path, batch: int = 10) -> tuple[list[str], int, int]:
    """The prompts a draft would send, one per batch of undecided rows, with
    the undecided and total row counts, so a caller can price the calls
    before any is made."""
    _columns, rows = _read_sheet(sheet_path)
    undecided = _undecided(rows)
    if undecided and not any(_question_of(row).strip() for row in undecided):
        # A model cannot judge a row with no question: it is being asked
        # whether to keep something nobody has said yet. Four calls and
        # $0.0099 bought 0 of 40 decisions this way. Mined candidates carry
        # SQL and no question until somebody writes one.
        raise SystemExit(
            f"none of the {len(undecided)} undecided rows has a question, so "
            "there is nothing for a model to judge and the calls would decide "
            "nothing. Draft the questions first with `mine --write-questions`, "
            "or write the wording column by hand, then run this again")
    accepted = accepted_questions(rows)
    prompts = []
    for chunk in _batches(undecided, batch):
        prompts.append(build_review_prompt(chunk, accepted))
        # the priced prompt is the longest one possible: every row of every
        # earlier batch accepted. The real calls can only be shorter.
        accepted = accepted + [text for text in (_question_of(row) for row in chunk) if text]
    return prompts, len(undecided), len(rows)


def draft(sheet_path: str | pathlib.Path, backend: Callable[[str], str], model: str,
          batch: int = 10) -> dict:
    """Let a model decide the undecided rows; rewrite the sheet in place.

    Only `keep`, `reason` and `source` are written, and `wording` when it is
    empty (a copy of the question, so every gold's wording column is
    filled). SQL and question text are never touched. A row a person
    already decided is not shown to the model. The counts come back so the
    caller can print who decided what.

    Every reply is written to `review.raw.txt` beside the sheet, before it is
    parsed. A run that decided 0 of 40 rows cost four calls and left nothing to
    read, so the only way to find out why was to pay again. `draft-rules`
    already keeps its reply for exactly this reason.
    """
    columns, rows = _read_sheet(sheet_path)
    undecided = _undecided(rows)
    accepted = accepted_questions(rows)
    decided = kept = 0
    reasons: Counter = Counter()
    raw_path = pathlib.Path(sheet_path).with_suffix(".raw.txt")
    raw: list[str] = []
    for chunk in _batches(undecided, batch):
        reply = backend(build_review_prompt(chunk, accepted))
        raw.append(f"--- batch of {len(chunk)} rows ---\n{reply}")
        raw_path.write_text("\n\n".join(raw), encoding="utf-8")
        found = parse_decisions(reply, {r["id"] for r in chunk})
        for row in chunk:
            if row["id"] not in found:
                continue
            keep, reason = found[row["id"]]
            # The model's opinion goes in its own two columns, ALWAYS. `keep`
            # and `reason` are filled too, because a sheet nobody reviews by
            # hand still has to produce golds -- but a person editing `keep`
            # afterwards now leaves `model_keep` untouched, so the two can be
            # compared instead of one silently replacing the other.
            row["model_keep"], row["model_reason"] = keep, reason
            row["keep"], row["reason"] = keep, reason
            row["source"] = MODEL_SOURCE_PREFIX + model
            if not (row.get("wording") or "").strip():
                row["wording"] = row.get("question", "")
            decided += 1
            kept += keep == "yes"
            reasons[reason] += 1
            if keep == "yes" and _question_of(row):
                accepted.append(_question_of(row))
    for column in MODEL_COLUMNS:
        if column not in columns:
            columns.append(column)
    columns = ordered_columns(columns)
    with open(sheet_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({**{c: "" for c in columns}, **row} for row in rows)
    return {"total": len(rows), "decided": decided, "kept": kept, "rejected": decided - kept,
            "reasons": dict(reasons.most_common()), "undecided": len(undecided) - decided}


def _normalised_question(row: dict) -> str:
    """The reviewer's wording when they wrote one, else the draft, folded to
    lower case with punctuation and repeated spaces removed."""
    text = (row.get("wording") or "").strip() or (row.get("question") or "").strip()
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    return " ".join(kept.split())


def _same_question_groups(asked: list[tuple[str, str]]) -> list[list[str]]:
    """Groups of ids whose questions read as the same question.

    Exact after normalising, or a difflib ratio at or above
    NEAR_DUPLICATE_RATIO. The pairs are compared in order and joined into
    groups, so three spellings of one question come out as one group. The
    length guard skips the comparison when one text cannot reach the ratio
    on length alone, which keeps a key of several hundred golds quick.
    """
    groups: list[list[tuple[str, str]]] = []
    for question_id, text in asked:
        for group in groups:
            if any(_reads_the_same(text, other) for _, other in group):
                group.append((question_id, text))
                break
        else:
            groups.append([(question_id, text)])
    return [[question_id for question_id, _ in group] for group in groups if len(group) > 1]


def _reads_the_same(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter, longer = sorted((len(left), len(right)))
    if not longer or shorter / longer < NEAR_DUPLICATE_RATIO:
        return False
    return difflib.SequenceMatcher(None, left, right).ratio() >= NEAR_DUPLICATE_RATIO


def _read_sheet(sheet_path: str | pathlib.Path) -> tuple[list[str], list[dict]]:
    with open(sheet_path, newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _undecided(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not (r.get("keep") or "").strip()]


def _batches(rows: list[dict], batch: int) -> list[list[dict]]:
    batch = max(1, batch)
    return [rows[i:i + batch] for i in range(0, len(rows), batch)]


def _question_of(row: dict) -> str:
    return (row.get("wording") or "").strip() or (row.get("question") or "").strip()


# --- the caveat a verdict carries -------------------------------------------

def model_family(model: str) -> str:
    """The first token before the first "-": gemini-2.5-flash and
    gemini-2.5-pro are one family, and a family checking its own answers
    shares its blind spots."""
    return (model or "").split("-", 1)[0].lower()


def answer_key_caveat(summary: dict | None, agent_models: list[str] | None = None) -> str | None:
    """The sentence printed beside a verdict while model-accepted golds are
    in the key; None when a person accepted every gold. `summary` is what
    `finish` returned; `agent_models` answered the arms, so the sentence can
    say when the reviewer and the agent are one family."""
    if not summary or not summary.get("model_reviewed"):
        return None
    n = summary["model_reviewed"]
    k = summary.get("person_reviewed") or 0
    model = summary.get("reviewer_model") or "unknown model"
    text = (f"the answer key: {n} of {n + k} golds accepted by a model reviewer ({model}), "
            f"{k} by a person; have a person re-review a sample before quoting the number")
    same = [m for m in (agent_models or []) if m and model_family(m) == model_family(model)]
    if same:
        text += (f"; the reviewer and the agent are the same model family ({model} / {same[0]}), "
                 "so a shared blind spot passes as correct")
    return text


def _funnel(rows: list[dict]) -> dict:
    status = Counter()
    for row in rows:
        for defect in (row.get("gate_status") or "ok").split("+"):
            status[defect] += 1
    tables = Counter(row.get("table") or "(unknown table)" for row in rows)
    return {"candidates": len(rows), "clean": status.get("ok", 0),
            "defects": {k: v for k, v in status.items() if k != "ok"},
            "tables": len(tables), "largest": tables.most_common(3)}


def _document(counts: dict, sheet_name: str) -> str:
    defects = ", ".join(f"{k} {v}" for k, v in counts["defects"].items()) or "none"
    largest = ", ".join(f"{t} {n}" for t, n in counts["largest"]) or "none"
    reasons = "\n".join(f"- `{name}`: {meaning}" for name, meaning in REASONS.items())
    browse = counts["defects"].get(WHOLE_ROW, 0)
    # said only when the worklist has such rows, so the document stays about
    # what is in front of the reviewer
    browsing = (f"""
## {browse} {"row is" if browse == 1 else "rows are"} a browse, not a question

Their SQL is `SELECT *`, so it hands back every column of the table whatever
was asked. Nobody asks a business question and wants 151 columns back; the
person was reading a table, not asking a question. No answer can match such a
gold unless the agent guesses the same column list, so reject the row, or
write the few columns the question really asks for.
""" if browse else "")
    return f"""# Review these candidates before they become golds

## The rule, before the data

A draft question written from SQL carries the SQL's vocabulary. A context page
that repeats those column names then scores well without helping anyone, and
the measurement is of vocabulary overlap, not of understanding. Read the draft,
look away from the SQL, and write what a person would type who has never seen
the table. If you cannot say what the query is for, reject it and say why.

## One worked example of the trap

| | text |
|---|---|
| draft from the SQL | Total of order_item_sale_price grouped by order_status |
| what a person asks | How much did we sell in each order state? |

Both describe the same query. Only the second one measures whether the context
taught the agent what "sold" and "order state" mean.

## What this worklist is, and is not

- {counts['candidates']} candidates, {counts['clean']} passed the gate, defects: {defects}
- {counts.get('pre_rejected', 0)} already decided for you: their SQL was refused
  by the gate, so `keep` is `no` and `reason` is `gate_defect`. They are still
  listed, with the defect beside them, because you should see what was dropped
  and why. Asking whether a question matches SQL that returns a 403 is not a
  question anybody can answer.
- {counts['tables']} tables; largest: {largest}
- Not in this list: everything the noise rules dropped (BI-tool projections,
  metadata probes, single-KPI tiles, dbt temp tables), one-off queries under
  the times-seen floor, and any dataset outside the scope mining ran with.
{browsing}
## What you own in `{sheet_name}`

- `wording`: the question in your words. Leave empty to keep the draft.
- `keep`: yes or no, for every row. Nothing is deleted, so rejections stay countable.
- `reason`: for every no, a few words (filter dropdown, lookup, not a business question, ...).
- `source`: `mined` unless the question came from somewhere else (`spreadsheet_human`, ...).

Then: `contextlift review --sheet {sheet_name} --out golds.csv`. It refuses while
any `keep` is empty, and it reports the rejections by reason.

## A model may draft this review

When nobody is there to review on day one, `contextlift review --draft --sheet
{sheet_name}` lets a model fill the empty `keep` rows. Four rules hold:

1. The SQL comes from the query history. The model never writes or edits SQL.
2. The model only accepts or rejects a row, with one reason from the list
   below. It never edits `wording` or the question text; when `wording` is
   empty it copies the question as it is.
3. Use a different model family from the agent being scored. Nothing here can
   enforce that; the verdict names both models so a reader can see when they
   are the same family.
4. The verdict carries a caveat ("N of M golds accepted by a model reviewer")
   until a person re-reviews the rows.

Rows the model decided get `source = {MODEL_SOURCE_PREFIX}<model>`. When you
check such a row, set `source` to `{PERSON_SOURCE}`: it then counts as reviewed
by a person. Rows you decided before the draft are never shown to the model,
and a row the model could not decide stays empty for you.

The reasons, the only ones the model may give:

{reasons}
"""


# ---------------------------------------------------------------------------
# The agreement rate, and harvesting an analyst's comments into rule lines.

# Every harvested line carries this, so an ablation can separate context a
# person wrote from context a machine counted. D36 already ranks a declared
# definition above a counted one, and a review comment is the cheapest source
# of declared definitions that exists.
HARVEST_PROVENANCE = "human_review_comment"
HARVEST_MARK = (f'<!-- {HARVEST_PROVENANCE}: drafted from a reviewer comment '
                f'by contextlift review --harvest; not approved -->')


def agreement(rows: list[dict]) -> dict:
    """How often the person and the model agreed, on rows where both decided.

    THE NUMBER THE PROJECT IS BLOCKED ON. On one estate `gemini-2.5-pro` kept
    45 of 177 candidate rows (25.4%) and `gemini-2.5-flash` kept 94 (53.1%).
    Which of those is right decides whether a round can resolve anything at
    all, and nothing settles it except a person disagreeing with one of them
    in writing. A human correcting thirty rows turns a 2x band into a number.

    Only rows where BOTH columns are filled count. A row the model never saw
    is not agreement, and counting it as one would push the rate toward 100%
    exactly as a reviewer does less work.
    """
    both = [r for r in rows
            if (r.get("keep") or "").strip() and (r.get("model_keep") or "").strip()]
    agreed = [r for r in both
              if _yes_no(r.get("keep")) == _yes_no(r.get("model_keep"))]
    return {
        "compared": len(both),
        "agreed": len(agreed),
        "disagreed": len(both) - len(agreed),
        # None, not 0.0: "nobody has checked" and "they never agree" are
        # different facts and a rate of 0% reads as the second.
        "rate": (len(agreed) / len(both)) if both else None,
        "person_kept": sum(1 for r in both if _yes_no(r.get("keep")) == "yes"),
        "model_kept": sum(1 for r in both if _yes_no(r.get("model_keep")) == "yes"),
    }


def agreement_text(found: dict) -> str:
    if not found["compared"]:
        return ("  agreement: no row has both a person's decision and a "
                "model's, so there is nothing to compare. Draft a sheet, then "
                "correct some rows by hand")
    return (f"  agreement: {found['agreed']} of {found['compared']} rows "
            f"({found['rate']:.0%}); the person kept {found['person_kept']}, "
            f"the model kept {found['model_kept']}\n"
            f"      {found['disagreed']} disagreement(s) are the rows worth "
            f"reading: they are where the model's error rate lives")


def _yes_no(value) -> str:
    text = (value or "").strip().lower()
    if text in YES:
        return "yes"
    return "no" if text in NO else ""


def comments_in(rows: list[dict]) -> list[tuple[str, str]]:
    """(question id, comment) for every row a reviewer wrote a comment on."""
    return [(r.get("id", ""), (r.get(COMMENT_COLUMN) or "").strip())
            for r in rows if (r.get(COMMENT_COLUMN) or "").strip()]


def harvest_prompt(comments: list[tuple[str, str]]) -> str:
    """Ask a model to turn reviewer comments into candidate rule lines.

    The comments are the input and the ONLY input. No schema, no questions,
    no SQL: a rule drafted from the answer key is the leak this stage is most
    able to create, and the cheapest guard is not to show it the answer key.
    """
    body = "\n".join(f"[{ident}] {text}" for ident, text in comments)
    return (
        "Below are comments a data analyst wrote while reviewing candidate "
        "questions for a text-to-SQL evaluation. Each explains something "
        "about the business that the schema does not say.\n\n"
        "Turn them into rule lines for a written context file. One line each, "
        "starting with '- '. Keep the analyst's meaning exactly; do not add "
        "anything they did not say. Name the table or column when they named "
        "one. If a comment says nothing about the business, skip it.\n\n"
        "Write nothing except the lines.\n\n"
        f"{body}\n")


def harvest_lines(reply: str) -> list[str]:
    """The `- ` lines out of a model reply, in order, deduplicated."""
    seen, out = set(), []
    for line in (reply or "").splitlines():
        text = line.strip()
        if not text.startswith("- ") or len(text) < 4:
            continue
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def harvest(sheet_path, backend: Callable[[str], str], golds=None) -> dict:
    """Reviewer comments into candidate rule lines, as text a person approves.

    Returns `{"comments": n, "lines": [...], "refused": [(line, why)], "text":
    str}`. It WRITES NOTHING. This is an LLM stage and gets the same treatment
    as every other one here: the output is a draft a person reads, never a
    file the tool applies on its own.

    THE LEAK GATE IS THE POINT OF THE `golds` ARGUMENT. A comment written on a
    scored question can quote that question's answer, and a rule carrying the
    answer would make every later measurement meaningless while looking like
    an improvement. Any drafted line that `check_leakage` flags is refused
    with its reason, and refused lines are returned rather than dropped so a
    person can see what the analyst said and rewrite it themselves.
    """
    _columns, rows = _read_sheet(sheet_path)
    comments = comments_in(rows)
    if not comments:
        return {"comments": 0, "lines": [], "refused": [], "text": ""}

    drafted = harvest_lines(backend(harvest_prompt(comments)))

    kept, refused = [], []
    for line in drafted:
        why = _leaks(line, golds)
        (refused.append((line, why)) if why else kept.append(line))

    header = [
        "# Drafted from reviewer comments. NOT APPROVED.",
        "#",
        "# Each line below came from a comment an analyst wrote while",
        "# reviewing candidate questions. Read every one before using this",
        f"# file: it is a model's paraphrase of a person, from {len(comments)}",
        "# comment(s).",
        HARVEST_MARK,
        "",
    ]
    return {"comments": len(comments), "lines": kept, "refused": refused,
            "text": "\n".join(header + kept) + "\n"}


def _leaks(line: str, golds) -> str:
    """Why this drafted line must not ship, or "" when it is clean.

    `check_leakage` takes (id, question, gold_sql) triples and the context
    text. `clean` is its own word for "nothing found", used here rather than
    a truthiness check on the findings list so this reads the same way the
    gate does.
    """
    if not golds:
        return ""
    report = check_leakage(list(golds), line)
    if report.clean:
        return ""
    return (f"the leak gate flagged it against "
            f"{', '.join(sorted(report.leaked_ids)[:3])}")


# ---------------------------------------------------------------------------
# Refusing self-review.

# What `mine` writes in `author` when a model drafted the question text. It
# records THAT a model wrote it and never WHICH, which is the gap the refusal
# below has to be honest about.
MODEL_DRAFTED = "model draft"


def self_review_refusal(rows: list[dict], review_model: str,
                        question_model: str | None = None) -> str | None:
    """Why this model must not review these questions, or None to go ahead.

    A MODEL CHECKING ITS OWN FAMILY'S WORK SHARES ITS BLIND SPOTS. If
    `gemini-2.5-flash` drafted a question from a SQL statement, asking
    `gemini-2.5-pro` whether the question matches that SQL is not an
    independent check: both read the same vocabulary out of the same SQL, and
    the mistake the drafter made is the one the reviewer is least likely to
    see. The published comparison is stark on its own -- two models in one
    family kept 25.4% and 53.1% of the same candidates -- and neither of those
    numbers is a measurement of correctness.

    THE HONEST PART IS THE THIRD CASE. `mine` records `author` as "model
    draft, reviewed against the live schema": it says a model wrote the
    question and not which model. So when the questions are model-drafted and
    nobody names the drafter, this tool CANNOT tell whether the reviewer is
    the same family, and it says so rather than guessing either way. Pass
    `--question-model` to name it, or accept the risk explicitly.
    """
    drafted = [r for r in rows if MODEL_DRAFTED in (r.get("author") or "").lower()]
    if not drafted:
        return None
    if not review_model:
        return None
    if question_model:
        if model_family(question_model) == model_family(review_model):
            return (f"{len(drafted)} of {len(rows)} question(s) were drafted "
                    f"by {question_model}, and {review_model} is the same "
                    f"family. A family checking its own work shares its blind "
                    f"spots: the mistake the drafter made is the one the "
                    f"reviewer is least likely to see. Use another family, or "
                    f"pass --allow-self-review with a reason you can defend")
        return None
    return (f"{len(drafted)} of {len(rows)} question(s) say they were model "
            f"drafted and do not say by which model, so whether "
            f"{review_model} is reviewing its own family's work cannot be "
            f"determined. Pass --question-model NAME, or --allow-self-review "
            f"if you accept a review that may not be independent")
