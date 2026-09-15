#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Download the pinned BIRD validation estate and make contextlift input files.

This is source preparation, not an evaluation runner.  Every gate, split,
generation, score, and comparison remains an explicit recorded contextlift CLI
command in README.md under Enterprise study.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import shutil
import tempfile
import urllib.parse
import urllib.request


QUERY_REPO = "target-benchmark/bird-queries-validation"
QUERY_REVISION = "d108575b93498f02410b579a89e38488eeb7180d"
CORPUS_REPO = "target-benchmark/bird-corpus-validation"
CORPUS_REVISION = "5ec3b6bb52e85d4ff2f7ad97b373ef8c0d8a096f"
DATABASES = (
    "california_schools", "card_games", "codebase_community",
    "debit_card_specializing", "european_football_2", "financial",
    "formula_1", "student_club", "superhero", "thrombosis_prediction",
    "toxicology",
)


def _url(repo: str, revision: str, path: str) -> str:
    quoted = urllib.parse.quote(path, safe="/")
    return f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{quoted}"


def _download(url: str, destination: pathlib.Path) -> None:
    """Download atomically so an interrupted transfer is never reused."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as tmp:
            shutil.copyfileobj(response, tmp)
            temporary = pathlib.Path(tmp.name)
    temporary.replace(destination)


def _tree(database: str) -> list[str]:
    path = f"validation_database/{database}/database_description"
    quoted = urllib.parse.quote(path, safe="/")
    url = (f"https://huggingface.co/api/datasets/{CORPUS_REPO}/tree/"
           f"{CORPUS_REVISION}/{quoted}?limit=1000")
    with urllib.request.urlopen(url, timeout=120) as response:
        return [item["path"] for item in json.load(response)
                if item.get("path", "").endswith(".csv")]


def _read_description(path: pathlib.Path, table: str) -> str:
    rows = list(csv.DictReader(path.open(
        encoding="utf-8-sig", errors="replace", newline="")))
    lines = [f"Catalog documentation for table {table}:"]
    for row in rows:
        column = (row.get("original_column_name") or "").strip()
        business_name = (row.get("column_name") or "").strip()
        description = (row.get("column_description") or "").strip()
        values = (row.get("value_description") or "").strip().replace("\n", " ")
        details = []
        if business_name and business_name.casefold() != column.casefold():
            details.append(f"business name: {business_name}")
        if description and description.casefold() not in {
                column.casefold(), business_name.casefold()}:
            details.append(description)
        if values:
            details.append(f"values: {values}")
        if column and details:
            lines.append(f"- {column}: " + "; ".join(details))
    return "\n".join(lines)


def write_catalog_context(rows: list[dict], root: pathlib.Path) -> None:
    """Write BIRD expert evidence plus docs for the annotated relevant tables."""
    by_id = {f"bird_{row['query_id']:04d}": row for row in rows}
    for database in DATABASES:
        docs = root / "source" / "descriptions" / database
        mapping = {}
        evidence = json.loads((root / "work" / database / "evidence.json").read_text())
        for question_id, text in evidence.items():
            tables = by_id[question_id].get("table_id") or []
            if isinstance(tables, str):
                tables = [tables]
            pages = []
            for table in tables:
                path = docs / f"{table}.csv"
                pages.append(_read_description(path, table) if path.exists()
                             else f"Table {table}: no additional catalog page.")
            mapping[question_id] = (f"Expert business evidence:\n{text}\n\n"
                                    + "\n\n".join(pages))
        destination = root / "work" / database / "catalog_context.json"
        destination.write_text(json.dumps(mapping, indent=1) + "\n", encoding="utf-8")


def write_inputs(rows: list[dict], root: pathlib.Path) -> dict[str, int]:
    """Write one gold file and evidence mapping per independently scored DB."""
    counts: dict[str, int] = {}
    for database in DATABASES:
        selected = [row for row in rows if row["database_id"] == database]
        counts[database] = len(selected)
        directory = root / "work" / database
        directory.mkdir(parents=True, exist_ok=True)
        fields = ("id", "question", "gold_sql", "evidence", "difficulty", "source")
        with (directory / "golds.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in selected:
                writer.writerow({
                    "id": f"bird_{row['query_id']:04d}",
                    "question": row["query"],
                    "gold_sql": row["answer"],
                    "evidence": row.get("evidence", ""),
                    "difficulty": row.get("difficulty", ""),
                    "source": "BIRD validation benchmark",
                })
        evidence = {f"bird_{row['query_id']:04d}": row.get("evidence", "")
                    for row in selected}
        (directory / "evidence.json").write_text(
            json.dumps(evidence, indent=1) + "\n", encoding="utf-8")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--questions-only", action="store_true",
                        help="prepare inputs but skip the ~1.4 GB SQLite estate")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    questions_path = args.out / "source" / "questions.json"
    _download(_url(QUERY_REPO, QUERY_REVISION, "bird-queries-validation.json"),
              questions_path)
    rows = json.loads(questions_path.read_text(encoding="utf-8"))
    counts = write_inputs(rows, args.out)
    if sum(counts.values()) != len(rows):
        known = set(DATABASES)
        unknown = sorted({row["database_id"] for row in rows} - known)
        raise SystemExit(f"source contains unrecognized databases: {unknown}")

    if not args.questions_only:
        for database in DATABASES:
            path = f"validation_database/{database}/{database}.sqlite"
            destination = args.out / "databases" / database / f"{database}.sqlite"
            _download(_url(CORPUS_REPO, CORPUS_REVISION, path), destination)
            for description in _tree(database):
                destination = (args.out / "source" / "descriptions" / database
                               / pathlib.Path(description).name)
                _download(_url(CORPUS_REPO, CORPUS_REVISION, description), destination)
        write_catalog_context(rows, args.out)

    receipt = {
        "query_repo": QUERY_REPO,
        "query_revision": QUERY_REVISION,
        "corpus_repo": CORPUS_REPO,
        "corpus_revision": CORPUS_REVISION,
        "questions": len(rows),
        "databases": counts,
        "sqlite_downloaded": not args.questions_only,
    }
    (args.out / "source.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"prepared {len(rows):,} questions across {len(counts)} databases")
    print("SQLite estate: " + ("downloaded" if not args.questions_only else "skipped"))
    print(f"provenance -> {args.out / 'source.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
