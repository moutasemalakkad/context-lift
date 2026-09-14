# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Turn a customer's own data-product documentation into a spec the arm reads.

    python scripts/read_catalog_docs.py MANIFEST.yaml TABLES.txt OUT_DIR

Free, no cloud call. The gap this closes, and it is embarrassing. One estate
had 34 data products, each with a human-written documentation file -- 300 KB
of prose, contacts, business definitions and fully-qualified table names --
and this framework measured five paid rounds against prose a model had mined
out of query logs instead. Nobody had checked whether the real thing existed.

So this is a READER, in the sense `docs/` uses the word: it takes context a
person wrote in their own system and puts it where an arm can carry it,
without editing the meaning. What it keeps:

- the Overview paragraph, which says what the data product is for
- the per-table business definitions, which name a fully-qualified table and
  say what it holds. This is the part that matters most: the mined rules
  named 8 of 10 bare columns that were on no table in the arm at all, while
  these name tables that exist.
- the source systems, because "sourced from S4 via HANA" is exactly the kind
  of thing an analyst knows and a schema does not say.

What it drops: HTML table scaffolding, contact emails, publisher names,
console URLs, and the fetched_at bookkeeping. None of that helps an agent
write SQL, and an irrelevant sentence is not free -- distractor context is
measurably harmful (Death of Schema Linking, arXiv 2408.07702).

Only data products whose dataset appears in TABLES.txt are written. A document
about a dataset no question touches cannot help, and shipping it anyway is the
dilution this project has already measured once.
"""
from __future__ import annotations

import pathlib
import re
import sys

FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
HTML = re.compile(r"<[^>]+>")
LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
# The 34 documents on one estate name their tables four different ways --
# inside a markdown link, inside a <td>, after a <br>, and bare. An earlier
# version matched one of those and carried 8 names out of 34 documents. So
# find every fully-qualified name and read the prose that follows it.
FQ_TABLE = re.compile(r"\b([a-z0-9][a-z0-9\-]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)\b")

OVERVIEW = re.compile(r"\*\*Overview\*\*\s*\n-+\s*\n(.*?)(?:\n\s*\n|\n#)", re.S)
SOURCE_BLOCK = re.compile(r"Source of the Data.*?<td>(.*?)</td>", re.S | re.I)


def clean(text: str) -> str:
    """Prose, with the markup taken out and the words left alone."""
    text = LINK.sub(r"\1", text or "")
    text = HTML.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split())


# A console URL sits BETWEEN a table name and its definition in these
# documents, and its host (console.cloud.google) matches the table pattern
# itself. So every URL goes before anything is matched: otherwise the host is
# read as a table and the real definition is cut off at its own link. That is
# how an earlier pass produced 198 names and not one definition.
URL = re.compile(r"https?://\S+|\bwww\.\S+")


def read(path: pathlib.Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    body = URL.sub(" ", FRONT.sub("", raw))
    overview = OVERVIEW.search(body)

    # Every fully-qualified name, with the prose that follows it. Where two
    # names sit next to each other with no words between -- a <br>-separated
    # list -- the second has no definition of its own and says so, rather
    # than borrowing the next paragraph and claiming it.
    hits = list(FQ_TABLE.finditer(body))
    tables, seen = [], set()
    for index, hit in enumerate(hits):
        name = hit.group(1).lower()
        # The middle part of a real name is a dataset, and every dataset on
        # this estate carries an underscore. A hostname does not.
        if name in seen or "_" not in name.split(".")[1]:
            continue
        seen.add(name)
        stop = hits[index + 1].start() if index + 1 < len(hits) else len(body)
        tail = clean(body[hit.end():min(stop, hit.end() + 800)])
        # A definition ends where the next section starts. Without this the
        # last table in a product borrows the source-systems paragraph.
        for edge in ("**BQ Table", "Source of the Data", "## ", "Q View"):
            if edge in tail:
                tail = tail.split(edge)[0]
        tail = tail.lstrip("](").strip(" -:|,.\u2013\u2014")
        tables.append({"table": name,
                       "definition": tail[:600] if len(tail) > 40 else ""})
    sources = SOURCE_BLOCK.search(body)
    return {"overview": clean(overview.group(1))[:900] if overview else "",
            "tables": tables,
            "sources": clean(sources.group(1))[:500] if sources else ""}


def main(manifest: str, tables_file: str, out_dir: str) -> None:
    import yaml
    root = pathlib.Path(manifest).parent.parent
    products = yaml.safe_load(open(manifest, encoding="utf-8"))["data_products"]
    wanted = {line.strip().split(".")[-2]
              for line in open(tables_file, encoding="utf-8") if line.strip()}

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = skipped = tables_named = 0
    lines = ["# What this company's own documentation says about its data",
             "",
             "Not written for this experiment. These are the data-product",
             "descriptions the data teams publish in their own catalog,",
             "copied here so an arm can carry them. Every table name below",
             "is theirs, fully qualified, from their own document.",
             ""]
    for product in products:
        dataset = product.get("dataset")
        doc = product.get("doc")
        if not dataset or not doc:
            continue
        if dataset not in wanted:
            skipped += 1
            continue
        path = root / doc
        if not path.exists():
            continue
        parsed = read(path)
        if not (parsed["overview"] or parsed["tables"]):
            continue
        written += 1
        lines += [f"## {product.get('display_name', dataset)}  "
                  f"(`{dataset}`)", ""]
        if parsed["overview"]:
            lines += [parsed["overview"], ""]
        described = [e for e in parsed["tables"] if e["definition"]]
        listed = [e for e in parsed["tables"] if not e["definition"]]
        for entry in described:
            tables_named += 1
            lines += [f"- `{entry['table']}`: {entry['definition']}"]
        if listed:
            tables_named += len(listed)
            lines += ["- also in this data product: "
                      + ", ".join(f"`{e['table']}`" for e in listed)]
        if parsed["tables"]:
            lines.append("")
        if parsed["sources"]:
            lines += [f"Sourced from: {parsed['sources']}", ""]

    target = out / "00-company-data-product-docs.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{written} data products written, {skipped} skipped (no question "
          f"touches their dataset)")
    print(f"  {tables_named} fully-qualified table names carried over")
    print(f"  {target}  ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: read_catalog_docs.py MANIFEST TABLES.txt OUT_DIR")
    main(sys.argv[1], sys.argv[2], sys.argv[3])
