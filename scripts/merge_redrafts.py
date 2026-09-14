# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Put re-drafted questions back in a review sheet, for the SAME judge to score.

    python scripts/merge_redrafts.py review.csv drafts.json out.csv

Free, no cloud call. The comparison this sets up, and why the judge matters
more than the drafter. On one estate `gemini-2.5-pro` rejected 59 of 195
candidates with reason `does_not_match`: the drafted English did not ask what
the SQL answered. Those were re-drafted, and a second reader accepted 41 of
the 59.

That 41 is not the number. The re-drafter and the reader were the same model
family, and a model reading its own family's writing accepts it -- measured on
this project: `gemini-2.5-flash` reviewing its own drafts kept 53%,
`gemini-2.5-pro` reviewing the same rows kept 25% (D80).

So the only honest measurement holds the judge fixed. `gemini-2.5-pro`
rejected 59 of 59 flash drafts. Re-run the same judge on the re-drafts and the
difference is attributable to the drafting, because nothing else moved.

The output sheet carries `keep` and `reason` cleared, so the judge decides
again from scratch rather than seeing an earlier verdict.
"""
from __future__ import annotations

import csv
import json
import sys


def main(sheet: str, result_json: str, out: str) -> None:
    payload = json.load(open(result_json, encoding="utf-8"))
    data = payload.get("result", payload)
    drafts = {d["id"]: d for d in data.get("drafts", [])}
    verdicts = {v["id"]: v for v in data.get("verdicts", [])}

    rows = list(csv.DictReader(open(sheet, encoding="utf-8")))
    replaced = improved = 0
    for row in rows:
        draft = drafts.get(row["id"])
        if not draft:
            continue
        verdict = verdicts.get(row["id"]) or {}
        better = (verdict.get("better_question") or "").strip()
        # Where the checker rejected the draft AND offered its own, take the
        # checker's: it read the SQL again with the draft in front of it, so
        # it is the later and better-informed of the two.
        question = better if (not verdict.get("accept", True) and better) \
            else draft["new_question"]
        if better and not verdict.get("accept", True):
            improved += 1
        row["question"] = question
        row["wording"] = ""
        row["keep"] = ""          # the judge decides again, from scratch
        row["reason"] = ""
        row["source"] = "redrafted:opus-5"
        replaced += 1

    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{replaced} questions replaced ({improved} taken from the checker "
          f"rather than the first draft)")
    print(f"wrote {out}")
    print("  `keep` and `reason` are blank on those rows, so the judge starts "
          "clean.\n  The other rows keep their earlier verdict and are not "
          "re-billed.")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: merge_redrafts.py SHEET.csv RESULT.json OUT.csv")
    main(sys.argv[1], sys.argv[2], sys.argv[3])
