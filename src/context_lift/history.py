# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One question across every round: what fixed it, what broke it, when.

The question a person could not answer before this module. Every round
reports per-question status, so "5 fixed, 0 broke" is on the page. Nothing
said whether question `lv014` had been broken once, three rounds running, or
never. At thirty questions a person can hold that in their head. At a
thousand they cannot, and that is the size this tool is aimed at.

Software testing settled the same problem, and the shape of the answer is
borrowed rather than invented. A gold question is a test; a round is a
commit. Per-test history, not per-build. Rank by how often a test fails, not
alphabetically. A test that fails every run is a real bug; one that fails
sometimes on unchanged code is flaky, and the two need different work.

Nothing new is recorded. Every round already saved a report carrying one
entry per question with its status, so this is a pivot over files that are
already on disk.

The output is a CSV rather than a page, because the person who has to read
it sorts and filters for a living, and a spreadsheet does that better than
anything this tool would build.
"""
from __future__ import annotations

import csv
import pathlib

# A question whose latest round broke it, then one broken before, then one
# that only wobbles, then the ones that carry no work. A human reads down
# this list until the rows stop mattering, and at a thousand questions that
# is the only way the file is usable.
_URGENCY = {"broke": 0, "unstable": 1, "fixed": 2, "wrong": 3, "right": 4, "": 5}

STATUS_MEANING = {
    "broke": "was right without the context, wrong with it",
    "fixed": "was wrong without the context, right with it",
    "unstable": "flipped between repetitions of the same arm; instability, not damage",
    "right": "right both ways; the context changed nothing here",
    "wrong": "wrong both ways; no written context reached it",
    "": "not scored in this round",
}


def build(workspace: pathlib.Path) -> dict:
    """Every scored question, every round, and what moved.

    Imported inside the function rather than at module scope because
    `loop` imports `bundle` which imports `ablate`, and a top-level import
    here would close that circle.
    """
    from .loop import api_rounds
    from .bundle import collect

    ledger = api_rounds(workspace)
    if not ledger.get("baseline"):
        return {"rounds": [], "rows": [], "baseline": None,
                "note": ledger.get("error") or
                "no baseline is scored yet, so no round has anything to compare"}

    baseline_id = ledger["baseline"]["arm_id"]
    rounds = [r for r in ledger["rounds"] if not r["error"]]

    # {question id: {round n: status}} plus the per-question notes that do
    # not change between rounds, taken from the LAST round that carried them
    # so a rule blamed in the newest round is the one printed.
    seen: dict[str, dict] = {}
    for entry in rounds:
        # `api_rounds` already called `collect` for its own numbers, so this
        # is the second call per round. Kept deliberately: a private walk
        # over the manifests here would be a second reader of the same files
        # and the two would drift. These are local reads.
        verdict = collect(workspace, baseline_id, entry["arm_id"])
        for question in verdict["questions"]:
            row = seen.setdefault(question["id"], {
                "id": question["id"], "question": question.get("question", ""),
                "status_by_round": {}, "blamed_rule": "", "credited_rule": "",
                "key_flagged": False, "key_detail": ""})
            row["status_by_round"][entry["n"]] = question["status"]
            if question.get("suspect_rules"):
                row["blamed_rule"] = "; ".join(question["suspect_rules"])
            if question.get("credited_rules"):
                row["credited_rule"] = "; ".join(
                    c["rule"] for c in question["credited_rules"])
            # A flagged answer key makes every round's status for this
            # question unreadable, not just the latest one, so it is sticky.
            if question.get("key_flagged"):
                row["key_flagged"] = True
                row["key_detail"] = question.get("key_detail", "")

    numbers = [r["n"] for r in rounds]
    rows = []
    for row in seen.values():
        by_round = row["status_by_round"]
        broken_in = [n for n in numbers if by_round.get(n) == "broke"]
        unstable_in = [n for n in numbers if by_round.get(n) == "unstable"]
        latest = by_round.get(numbers[-1], "") if numbers else ""
        rows.append({**row,
                     "latest": latest,
                     "first_broken_in": broken_in[0] if broken_in else None,
                     "times_broken": len(broken_in),
                     "times_unstable": len(unstable_in),
                     # A question broken in the newest round is work now. One
                     # broken earlier and right since is history. Sorting them
                     # together is how a real regression gets buried.
                     "open": latest == "broke"})
    rows.sort(key=lambda r: (_URGENCY.get(r["latest"], 5),
                             -r["times_broken"], -r["times_unstable"], r["id"]))
    return {"rounds": rounds, "rows": rows, "baseline": ledger["baseline"],
            "note": ""}


FIELDS = ["id", "question", "latest", "first_broken_in", "times_broken",
          "times_unstable", "blamed_rule", "credited_rule", "key_flagged",
          "key_detail"]


def write_csv(history: dict, path: pathlib.Path) -> int:
    """One row per question, one column per round. Returns the row count."""
    numbers = [r["n"] for r in history["rounds"]]
    columns = FIELDS + [f"round_{n}" for n in numbers]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in history["rows"]:
            out = {field: row.get(field, "") for field in FIELDS}
            out["key_flagged"] = "yes" if row["key_flagged"] else ""
            for n in numbers:
                out[f"round_{n}"] = row["status_by_round"].get(n, "")
            writer.writerow(out)
    return len(history["rows"])


def lines(history: dict, show: int = 12) -> list[str]:
    """What a person reads in the terminal before opening the file.

    Deliberately short. The whole point is that nobody reads a thousand
    rows: a round over a thousand questions might move thirty, and thirty is
    reviewable. The counts say how many rows are worth opening the file for,
    and the list shows the ones at the top.
    """
    if history["note"]:
        return [history["note"]]
    rounds, rows = history["rounds"], history["rows"]
    if not rounds:
        return ["no round is fully scored yet, so there is no history to show"]

    out = [f"{len(rows)} questions over {len(rounds)} round(s): "
           + ", ".join(f"{r['n']}. {r['label']}" for r in rounds)]

    # Grouped by the LATEST status, so these four are exclusive and sum to
    # the question count. An earlier version counted a fixed question under
    # "never moved", which read as though the context had done nothing.
    by_latest: dict[str, list[dict]] = {}
    for row in rows:
        by_latest.setdefault(row["latest"], []).append(row)
    open_now = by_latest.get("broke", [])
    wobbling = by_latest.get("unstable", [])
    holding = by_latest.get("fixed", [])
    quiet = len(by_latest.get("right", [])) + len(by_latest.get("wrong", []))

    out.append(f"  broken in the newest round : {len(open_now)}"
               + ("   <- read these first" if open_now else ""))
    out.append(f"  fixed and still fixed      : {len(holding)}")
    out.append(f"  unstable in the newest     : {len(wobbling)}"
               + ("   <- instability, not damage" if wobbling else ""))
    out.append(f"  never moved                : {quiet}")

    # These two cut across the four above rather than adding to them: a
    # question can be broken now AND have been broken before, and a flagged
    # answer key makes any of the four unreadable.
    repeat = [r for r in rows if r["times_broken"] > 1]
    flagged = [r for r in rows if r["key_flagged"]]
    if repeat:
        out.append(f"    of those, broken before  : {len(repeat)}"
                   "   <- a rule, not luck")
    if flagged:
        out.append(f"    answer key flagged       : {len(flagged)}"
                   "   <- these rows say nothing until the key is fixed")

    worth_reading = [r for r in rows
                     if r["open"] or r["times_broken"] or r["times_unstable"]]
    if worth_reading:
        out.append("")
        for row in worth_reading[:show]:
            trail = " ".join(f"{n}:{row['status_by_round'].get(n, '-')}"
                             for n in [r["n"] for r in rounds])
            note = ""
            if row["key_flagged"]:
                note = "  answer key flagged"
            elif row["blamed_rule"]:
                note = f"  blamed: {row['blamed_rule']}"
            out.append(f"  {row['id']:12s} {trail}{note}")
        if len(worth_reading) > show:
            out.append(f"  ... and {len(worth_reading) - show} more in the file")
    else:
        out.append("")
        out.append("  no question has been broken or gone unstable in any round")
    return out
