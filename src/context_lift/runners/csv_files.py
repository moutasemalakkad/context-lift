# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Score predictions somebody else's agent produced.

Not every team can point this at their agent. Some run a model this tool has
no runner for, some cannot give it credentials, and some already have the
answers in a spreadsheet from a bake-off last month. All of them still have
the harder problem: knowing whether the number they are about to quote means
anything.

So the CSV runner takes two files, one per arm, and everything else is the
same round. The gold gate still audits the answer key. The split is still
sealed. The paired test, the generalization gap, the lookup exclusion and
the verdict word are the same code, because the guards are the product and
they should not depend on who produced the SQL.

**A question with no row is unanswered, not wrong.** Scoring a missing row as
a failure invents one and moves the denominator, which is the same mistake as
counting an excluded question as wrong. The count is printed so a person can
see how much of the set each file actually covered.

**The arm hash is not invented.** This tool did not build the context, so it
cannot hash it. The manifest records the file's own content hash instead,
which is what is actually known: two runs of the same file are the same
experiment, and that is all this can honestly claim.
"""
from __future__ import annotations

import csv
import hashlib
import pathlib
from dataclasses import dataclass, field

from . import CostSheet, Prediction

# The columns a file must carry. `id` joins to the gold set; `pred_sql` is the
# answer. Anything else in the file is ignored rather than refused, because a
# team's export usually carries their own columns and refusing it would make
# them edit a file to run a check.
REQUIRED = ("id", "pred_sql")


def read(path: str | pathlib.Path) -> dict[str, str]:
    """One arm's predictions, keyed by question id."""
    path = pathlib.Path(path)
    if not path.is_file():
        raise SystemExit(f"no predictions file at {path}")
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in REQUIRED
                   if column not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(
                f"{path} has no {', '.join(missing)} column. A predictions "
                f"file needs {' and '.join(REQUIRED)}; other columns are kept "
                "and ignored")
        rows = {}
        for row in reader:
            identifier = (row.get("id") or "").strip()
            if identifier:
                rows[identifier] = row.get("pred_sql") or ""
    return rows


def content_hash(path: str | pathlib.Path) -> str:
    """The file's own hash, which is what is known about an arm nobody built."""
    return hashlib.sha256(
        pathlib.Path(path).read_bytes()).hexdigest()[:12]


@dataclass
class CsvRunner:
    """A runner that answers from two files instead of from a model."""

    name: str = "csv"
    by_arm: dict[str, dict[str, str]] = field(default_factory=dict)
    missing: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_files(cls, control: str, treatment: str) -> "CsvRunner":
        """One file per arm, keyed by the ARM LABEL the round will use."""
        return cls(by_arm={"control": read(control), "treatment": read(treatment)})

    def cost_sheet(self, n_questions: int, n_arms: int, k: int) -> CostSheet:
        """Free, and the sheet says so rather than printing a zero.

        A zero next to a dollar sign reads as an estimate that came out low.
        """
        return CostSheet(calls=0, minutes_at_quota=0.0, usd_estimate=0.0,
                         basis="the answers are already written; this runs no "
                               "model and makes no call")

    def _side(self, arm) -> dict[str, str]:
        label = (getattr(arm, "label", "") or "").lower()
        # The round names its arms; anything that is not the control is the
        # treatment, so a relabelled arm still finds its file.
        return self.by_arm["control" if "control" in label or label.startswith("0d")
                           else "treatment"]

    def answer(self, question_id: str, question: str, arm) -> Prediction:
        # The round asks for `q1#rep0`; a predictions file is keyed by the
        # question. Repeats of a written answer are the same answer, which is
        # honest: a file has no run-to-run variation to measure.
        base = question_id.split("#")[0]
        rows = self._side(arm)
        sql = rows.get(base)
        if sql is None:
            self.missing.setdefault(arm.label, []).append(base)
            return Prediction(question_id=question_id, arm_id=arm.arm_id,
                              pred_sql=None,
                              error_class="no row in the predictions file")
        return Prediction(question_id=question_id, arm_id=arm.arm_id, pred_sql=sql)

    def coverage(self, question_ids: list[str]) -> str:
        """How much of the gold set each file actually answered."""
        lines = []
        for side in ("control", "treatment"):
            rows = self.by_arm.get(side) or {}
            answered = sum(1 for i in question_ids if i in rows)
            lines.append(f"  {side}: {answered} of {len(question_ids)} questions "
                         "have a row")
        return "\n".join(lines)
