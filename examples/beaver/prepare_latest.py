#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Download a deterministic BEAVER DW question sample without extra packages.

The Hugging Face dataset is gated. Accept its terms in a browser, put a
read-only token in ``HF_TOKEN``, then run this script. The token is sent only in
the Authorization header and is never printed or written to the output.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import urllib.parse
import urllib.request
from collections.abc import Callable


ROWS_URL = "https://datasets-server.huggingface.co/rows"


def fetch_rows(token: str, opener: Callable = urllib.request.urlopen) -> list[dict]:
    """Fetch every public DW row from Hugging Face's paginated rows API."""
    rows: list[dict] = []
    total = None
    while total is None or len(rows) < total:
        query = urllib.parse.urlencode({
            "dataset": "beaverbench/beaver-query",
            "config": "default",
            "split": "dw",
            "offset": len(rows),
            "length": 100,
        })
        request = urllib.request.Request(
            f"{ROWS_URL}?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with opener(request) as response:
            page = json.load(response)
        total = int(page["num_rows_total"])
        batch = [item["row"] for item in page["rows"]]
        if not batch and len(rows) < total:
            raise RuntimeError(f"Hugging Face returned an empty page at offset {len(rows)}")
        rows.extend(batch)
    return rows


def write_sample(rows: list[dict], out: str, sample: int, seed: int) -> None:
    """Write the stable question CSV consumed by contextlift gate."""
    if sample > len(rows):
        raise ValueError(f"asked for {sample} questions, but the release has {len(rows)}")
    chosen = random.Random(seed).sample(rows, sample)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["id", "question", "gold_sql", "author", "origin"])
        writer.writeheader()
        for row in chosen:
            writer.writerow({
                "id": row["id"],
                "question": row["question"],
                "gold_sql": row["sql"],
                "author": "BEAVER",
                "origin": f"beaverbench/beaver-query dw, seed {seed} sample",
            })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="latest/questions_1200.csv")
    parser.add_argument("--sample", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=77)
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN")
    if not token:
        parser.error("set HF_TOKEN to a read-only token after accepting the dataset terms")
    rows = fetch_rows(token)
    write_sample(rows, args.out, args.sample, args.seed)
    print(f"{args.out}: {args.sample} of {len(rows)} DW questions (seed {args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

