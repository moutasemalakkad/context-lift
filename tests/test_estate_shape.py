# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The estate-shape numbers, recomputed from the committed thelook golds.

These figures are quoted in the docs as the high-redundancy end of the
comparison, so a silent drift in `tables_named`, the splitter, or the gold
file would make a shipped number wrong with nothing failing. This pins them
to the data rather than to a paragraph.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "estate_shape.py"
GOLDS = REPO / "examples" / "thelook" / "golds.csv"


def _run() -> str:
    done = subprocess.run([sys.executable, str(SCRIPT), str(GOLDS)],
                          capture_output=True, text=True, cwd=REPO, check=True)
    return done.stdout


def test_thelook_is_the_high_redundancy_end():
    out = _run()
    assert "104 questions over 9 tables" in out
    assert "11.56 questions per table" in out
    # above 2.00, so the warning line must NOT print
    assert "under 2.00" not in out


def test_thelook_has_no_unreachable_reserved_questions():
    """Every reserved question shares a table with the working half.

    This is the contrast the docs lean on: the same gate reads 100% here and
    fails on a wide estate, so the gate reads the estate rather than encoding
    one. Both split strategies must reach every reserved question.
    """
    out = _run()
    assert out.count("(100%)") == 2, out
    assert "[FAIL]" not in out
