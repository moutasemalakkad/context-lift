# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Build the BLOB arm: every question gets its whole database's context.

    python scripts/blob_arm.py --work DIR [--out-name blob.json]

This exists to answer one question with a number: **does delivering context
per question beat delivering all of it at once?** That is the only mechanism
by which a catalog can beat a file, because for identical content that fits in
a request the two are the same bytes and therefore the same accuracy (measured
2026-09-13: a catalog round trip read back `d07c1f37980d`, the hash it was
given).

So the comparison is between two ways of delivering the SAME words:

    selection arm   evidence.json  -- each question gets its own sentence.
                    Already written by prepare_enterprise.py. This is an
                    ORACLE retriever: perfect selection, the ceiling no real
                    retriever can beat.

    blob arm        this script    -- each question gets every sentence for
                    its database, its own included, in a fixed order.

Both arms contain each question's own evidence, so their leak status is
identical and the gap between them is not an artifact of one arm knowing
more. The blob arm simply also carries what the other questions needed.

If perfect selection does not beat the blob, no retriever will, and a catalog
cannot win on delivery. If it does, a real retriever becomes worth building
and the gap is the ceiling on what it could earn.

Free: no model calls, no network. It rearranges files prepare_enterprise.py
already wrote.
"""
from __future__ import annotations

import argparse
import json
import pathlib

HEADER = "Expert business evidence for this database:"


def blob_for(evidence: dict[str, str]) -> str:
    """One text carrying every sentence, in id order.

    Sorted by id rather than left in dict order, so the blob is the same on
    every machine and two runs of this script build the same arm. An arm that
    changes shape between runs cannot be compared to itself.
    """
    lines = [f"- {evidence[key].strip()}"
             for key in sorted(evidence)
             if (evidence.get(key) or "").strip()]
    return HEADER + "\n" + "\n".join(lines) + "\n"


def build(work: pathlib.Path, out_name: str) -> list[dict]:
    """One blob per database, written beside that database's evidence.json."""
    report = []
    for directory in sorted(p for p in work.iterdir() if p.is_dir()):
        source = directory / "evidence.json"
        if not source.is_file():
            continue
        evidence = json.loads(source.read_text())
        text = blob_for(evidence)
        # every question in this database sees the same blob
        mapping = {key: text for key in evidence}
        destination = directory / out_name
        destination.write_text(json.dumps(mapping, indent=1) + "\n",
                               encoding="utf-8")
        own = sum(len((evidence.get(k) or "")) for k in evidence)
        report.append({
            "database": directory.name,
            "questions": len(evidence),
            "blob_chars": len(text),
            "own_chars_mean": round(own / max(len(evidence), 1)),
            "out": str(destination),
        })
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", required=True, metavar="DIR",
                        help="the work/ directory prepare_enterprise.py wrote")
    parser.add_argument("--out-name", default="blob.json",
                        help="written inside each database's directory")
    args = parser.parse_args()

    work = pathlib.Path(args.work)
    if not work.is_dir():
        raise SystemExit(f"not a directory: {work}")

    report = build(work, args.out_name)
    if not report:
        raise SystemExit(f"no evidence.json under {work}")

    print(f"{'database':26} {'questions':>9} {'blob chars':>11} {'own mean':>9}")
    for row in report:
        print(f"{row['database'][:26]:26} {row['questions']:>9} "
              f"{row['blob_chars']:>11,} {row['own_chars_mean']:>9}")
    questions = sum(r["questions"] for r in report)
    # the ratio is the whole point: how much more text the blob arm carries
    blob_total = sum(r["blob_chars"] * r["questions"] for r in report)
    own_total = sum(r["own_chars_mean"] * r["questions"] for r in report)
    print(f"\n{questions} question(s) across {len(report)} database(s)")
    print(f"the blob arm sends {blob_total / max(own_total, 1):.0f}x more "
          f"context than the selection arm, in total characters")
    print(f"mean blob {blob_total // max(questions, 1):,} chars per call "
          f"(~{blob_total // max(questions, 1) // 4:,} tokens), against "
          f"{own_total // max(questions, 1):,} for selection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
