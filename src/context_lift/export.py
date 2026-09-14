# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The spec repo as the source of truth: files in, two arms out.

This is file mode, the v1 default. A person authors context as files in their
own repository, reviews it by pull request, and gets diff, history and rollback
from git. The Knowledge Catalog has none of those for an aspect, so it is where
context is published and governed, not where it is versioned.

The layout mirrors what the agent receives, one directory per Context field, so
nobody has to hold a mapping in their head:

    rules/        *.md, concatenated in filename order into one system
                  instruction. A leading two-digit number orders the tier,
                  broadest first: 00-*.md before 10-*.md before 20-*.md. The
                  rest of the name is free text saying what that tier holds --
                  the shipped specs use 00-definitions.md, 00-from-lookml.md
                  and 10-output-conventions.md. A file with no number still
                  gets read, at whatever position its first letter puts it in,
                  which is why `lint` names it.
    schema/       one file per table, named for it. The table's description,
                  synonyms and tags, and the same three per column. This is
                  what the estate already had, and it goes into BOTH arms.
    authored/     the same shape, for what a curator wrote. Treatment only,
                  merged on top of schema/. This is the channel a probe on
                  2026-09-09 showed to be the strongest one: the service reads
                  a column description and obeys instructions written in it.
    vocabulary/   *.yaml, canonical terms with their definitions and the other
                  words people use for them.
    examples/     *.jsonl, one {question, sql} per line.
    relationships.yaml   how tables join.

Two arms come out of one spec, and the control is a strict subset of the
treatment:

    Arm 0d, the control    schema/ only
    Arm 1, the treatment   schema/ plus rules, vocabulary, examples, joins

The control carries the estate's own descriptions because the service reads
them on its own and follows instructions written in them (design doc 3.6). A
control without them would credit authored context for work the customer's own
build already did. Because the control is a subset, the difference between the
two arms is exactly the authored context and nothing else.

YAML needs the [yaml] extra. Every reader here also takes `.json`, which needs
nothing, so the core stays stdlib-only.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field

from .runners import Arm, Example, Term

# The label every object this tool writes to a catalog carries, and the id
# shape it writes them under. Both live here rather than in push.py because
# the READ side needs them too: `write_catalog` tells a stale term of ours
# apart from a colleague's by this label, and matches a spec term to a stored
# one by this id. Two copies of either would disagree eventually, and the
# failure would be a term silently proposed for deletion.
OWNER_LABEL = "managed-by"
OWNER_VALUE = "contextlift"
LABELS = {OWNER_LABEL: OWNER_VALUE}


def slug(text: str) -> str:
    """A catalog id: lowercase, every run of anything but a-z 0-9 becomes one
    hyphen, no hyphen at either end, at most 63 characters (the id cap)."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:63].rstrip("-")

# A rules filename declares its tier with a leading two-digit number, broadest
# first. Only the NUMBER is fixed: it decides the order `sorted()` puts the
# files in, and therefore which rule the precedence sentence below makes win.
# The name after it is free text, and the shipped specs use 00-definitions,
# 00-from-lookml and 10-output-conventions rather than any fixed vocabulary.
TIER_PREFIX = re.compile(r"^(\d\d)-")


def tier_of(name: str) -> int | None:
    """The tier number a rules filename declares, or None if it declares none.

    `None` is not an error and does not stop a read. It means the file's
    position among the other rules is whatever alphabetical order gave it,
    which is nobody's decision -- so `lint` reports it.
    """
    found = TIER_PREFIX.match(name)
    return int(found.group(1)) if found else None


# Said once, at the end of the rules, because the tiers are concatenated and
# nothing else tells the model which one wins.
PRECEDENCE = "When two rules disagree, the more specific one applies."

RULES_DIR = "rules"
SCHEMA_DIR = "schema"
AUTHORED_DIR = "authored"
VOCABULARY_DIR = "vocabulary"
EXAMPLES_DIR = "examples"
RELATIONSHIPS_FILE = "relationships"


def _load_structured(path: pathlib.Path):
    """A `.yaml` or `.json` file as Python data."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError as missing:  # pragma: no cover - depends on the extra
        raise SystemExit(
            f"reading {path.name} needs pyyaml: `uv pip install -e '.[yaml]'`. "
            "A .json file of the same shape needs nothing."
        ) from missing
    return yaml.safe_load(text)


def _structured_files(directory: pathlib.Path) -> list[pathlib.Path]:
    """Every `.yaml` and `.json` file in one directory, in name order."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.suffix.lower() in (".yaml", ".yml", ".json"))


def _count(number: int, noun: str) -> str:
    """`1 table`, `2 tables`. A person reads this line before spending money."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


@dataclass
class TableSchema:
    """What one table already said about itself, before anyone authored a word.

    Keyed by the three-part table name. Column entries are keyed by column
    name. A column may carry any one of the three and still get an entry, so a
    synonyms-only arm is expressible and can be priced on its own.
    """

    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    columns: dict[str, dict] = field(default_factory=dict)


@dataclass
class Spec:
    """One spec repo, read. Nothing here has reached an arm yet."""

    root: pathlib.Path
    rules: list[tuple[str, str]] = field(default_factory=list)
    schemas: dict[str, TableSchema] = field(default_factory=dict)
    authored: dict[str, TableSchema] = field(default_factory=dict)
    terms: list[Term] = field(default_factory=list)
    examples: list[Example] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)

    @property
    def tables(self) -> list[str]:
        """The tables the spec describes, in name order."""
        return sorted(self.schemas)

    def context_text(self) -> str:
        """Everything the agent is handed, as one searchable string.

        For asking "does the written context state this value?" -- which is
        what `gate.missing_literals` needs in order to tell a documented
        business code from a question parameter nobody can guess.

        Not `system_instruction()` alone: a value can be documented in a
        column description, a glossary term, or the SQL of a worked example,
        and a worked example showing `category = 'Jeans'` documents 'Jeans'
        just as a rule sentence would. Everything an arm carries is included
        for that reason, and nothing else is, because a value only counts as
        documented if the agent actually receives it.
        """
        parts = [self.system_instruction()]
        for source in (self.schemas, self.authored):
            for name, table in sorted(source.items()):
                parts += [name, table.description, " ".join(table.synonyms),
                          " ".join(table.tags)]
                for column, meta in sorted(table.columns.items()):
                    parts.append(column)
                    if isinstance(meta, dict):
                        parts += [str(value) for value in meta.values()]
        for term in self.terms:
            parts += [getattr(term, "name", ""), getattr(term, "definition", ""),
                      " ".join(getattr(term, "synonyms", []) or [])]
        for example in self.examples:
            parts += [getattr(example, "question", ""),
                      getattr(example, "sql", "")]
        return "\n".join(part for part in parts if part)

    def system_instruction(self) -> str:
        """Every rules file, in tier order, then the precedence sentence.

        Each file keeps its own heading so a reader of the request body can
        see which tier a rule came from, which is also what makes a tier a
        class the round can price with leave-one-class-out.
        """
        if not self.rules:
            return ""
        blocks = [f"# {name}\n{body.strip()}" for name, body in self.rules if body.strip()]
        if not blocks:
            return ""
        return "\n\n".join(blocks) + "\n\n" + PRECEDENCE

    def summary(self) -> str:
        """What was read, so a person can see it before spending anything."""
        described = sum(1 for s in self.schemas.values() if s.description)
        columns = sum(len(s.columns) for s in self.schemas.values())
        authored_columns = sum(len(s.columns) for s in self.authored.values())
        return "\n".join([
            f"{self.root}:",
            f"  {_count(len(self.rules), 'rules file')}, "
            f"{len(self.system_instruction())} characters",
            f"  {_count(len(self.schemas), 'table')}, {described} with a description, "
            f"{_count(columns, 'column')}",
            f"  {_count(authored_columns, 'authored column description')}",
            f"  {_count(len(self.terms), 'term')}",
            f"  {_count(len(self.examples), 'worked example')}",
            f"  {_count(len(self.relationships), 'join relationship')}",
        ])


def read_spec(root: str | pathlib.Path) -> Spec:
    """Read a spec repo. Every directory is optional; an absent one is empty.

    An absent directory is not an error, because a first round is often rules
    and nothing else. What matters is that whatever is present is read the same
    way every time, so two runs of the same repo produce the same arm hash.
    """
    root = pathlib.Path(root)
    if not root.is_dir():
        raise SystemExit(f"spec repo not found: {root}")
    spec = Spec(root=root)

    rules_dir = root / RULES_DIR
    if rules_dir.is_dir():
        for path in sorted(rules_dir.glob("*.md")):
            spec.rules.append((path.stem, path.read_text(encoding="utf-8")))
    # `sorted` above is the whole tier mechanism. It is not validated here,
    # because reading a spec must not refuse one: a file with no tier number
    # still has content worth sending. `lint.untiered_rule_files` names them.

    for path in _structured_files(root / SCHEMA_DIR):
        _read_schema(path, spec, spec.schemas)

    for path in _structured_files(root / AUTHORED_DIR):
        _read_schema(path, spec, spec.authored)

    for path in _structured_files(root / VOCABULARY_DIR):
        _read_vocabulary(path, spec)

    examples_dir = root / EXAMPLES_DIR
    if examples_dir.is_dir():
        for path in sorted(examples_dir.glob("*.jsonl")):
            _read_examples(path, spec)

    for suffix in (".yaml", ".yml", ".json"):
        path = root / (RELATIONSHIPS_FILE + suffix)
        if path.is_file():
            data = _load_structured(path) or []
            spec.relationships = [r for r in data if isinstance(r, dict)]
            break

    return spec


def _read_schema(path: pathlib.Path, spec: Spec, into: dict) -> None:
    """One `schema/` file. The table name is the filename, not a field inside.

    Putting the name in the filename means two files cannot claim the same
    table without one overwriting the other on disk first, where a person sees
    it, rather than silently at read time.
    """
    data = _load_structured(path) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path} should hold a mapping")
    table = path.stem
    if table.count(".") != 2:
        raise SystemExit(
            f"{path.name}: a schema file is named for its table, as "
            f"project.dataset.table.yaml; got {table!r}")
    entry = TableSchema(
        description=str(data.get("description") or "").strip(),
        synonyms=[str(w) for w in (data.get("synonyms") or [])],
        tags=[str(w) for w in (data.get("tags") or [])],
    )
    for column, body in (data.get("columns") or {}).items():
        if isinstance(body, str):
            body = {"description": body}
        if not isinstance(body, dict):
            continue
        entry.columns[str(column)] = {
            "description": str(body.get("description") or "").strip(),
            "synonyms": [str(w) for w in (body.get("synonyms") or [])],
            "tags": [str(w) for w in (body.get("tags") or [])],
        }
    into[table] = entry


def _read_vocabulary(path: pathlib.Path, spec: Spec) -> None:
    """Canonical terms. Other words for a term join its definition.

    The Context has one field for a term, a name and a description, so a
    synonym has nowhere of its own to go. It is appended as an "Also called"
    line rather than dropped, and the same words are written into the schema
    `synonyms` field too, so the two channels can be priced apart.
    """
    data = _load_structured(path) or []
    if isinstance(data, dict):
        data = data.get("terms") or []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        definition = str(item.get("definition") or "").strip()
        also = _words(item.get("also_called"))
        related = _words(item.get("related"))
        if also:
            definition = f"{definition} Also called: {', '.join(also)}.".strip()
        if related:
            definition = f"{definition} Related: {', '.join(related)}.".strip()
        spec.terms.append(Term(
            name=name, definition=definition,
            tier=str(item.get("tier") or "org").strip() or "org",
            synonyms=tuple(also), related=tuple(related)))


def _words(value) -> list[str]:
    """A list field that may be absent, a string, or a list with blanks in it.

    `also_called: net sales` written without brackets is a string, and
    iterating a string yields characters. That turns one synonym into eleven
    one-letter ones, silently.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(word).strip() for word in value if str(word).strip()]


def _read_examples(path: pathlib.Path, spec: Spec) -> None:
    """One worked example per line. A blank line is skipped, a broken one is not.

    A JSONL file that half-parses is how an example bank silently shrinks, so a
    line that is not JSON stops the read and names the line number.
    """
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as broken:
            raise SystemExit(f"{path.name} line {number}: {broken}") from broken
        question = str(row.get("question") or "").strip()
        sql = str(row.get("sql") or "").strip()
        if question and sql:
            spec.examples.append(Example(question=question, sql=sql))


def build_arms(spec: Spec, tables: list[str] | None = None,
               model_alias: str = "LATEST_GA_MODEL") -> tuple[Arm, Arm]:
    """The control and the treatment, in that order.

    `tables` defaults to every table the spec describes. Naming a subset is how
    a domain arm is built, and it is also how a run stays inside the table
    count the model can hold.

    The control is a strict subset of the treatment by construction, not by
    convention: it is built first and the treatment is built from it. So the
    only difference between the two is authored context, and there is no way
    for a later edit to make the control carry something the treatment lacks.
    """
    named = sorted(tables) if tables else spec.tables
    missing = [t for t in named if t not in spec.schemas]
    if missing:
        raise SystemExit(
            "no schema file for: " + ", ".join(missing) +
            f". Add {SCHEMA_DIR}/<table>.yaml, or the control cannot carry what "
            "the estate already had and the lift would credit authored context "
            "for it.")

    control = Arm(label="0d control", tables=list(named), model_alias=model_alias)
    for table in named:
        entry = spec.schemas[table]
        if entry.description:
            control.table_descriptions[table] = entry.description
        if entry.synonyms:
            control.table_synonyms[table] = list(entry.synonyms)
        if entry.tags:
            control.table_tags[table] = list(entry.tags)
        for column, body in sorted(entry.columns.items()):
            if body["description"]:
                control.field_descriptions.setdefault(table, {})[column] = body["description"]
            if body["synonyms"]:
                control.field_synonyms.setdefault(table, {})[column] = list(body["synonyms"])
            if body["tags"]:
                control.field_tags.setdefault(table, {})[column] = list(body["tags"])

    treatment = Arm(
        label="1 authored",
        tables=list(control.tables),
        table_descriptions=dict(control.table_descriptions),
        field_descriptions={k: dict(v) for k, v in control.field_descriptions.items()},
        table_synonyms={k: list(v) for k, v in control.table_synonyms.items()},
        field_synonyms={k: {c: list(w) for c, w in v.items()}
                        for k, v in control.field_synonyms.items()},
        table_tags={k: list(v) for k, v in control.table_tags.items()},
        field_tags={k: {c: list(w) for c, w in v.items()}
                    for k, v in control.field_tags.items()},
        terms=list(spec.terms),
        examples=list(spec.examples),
        relationships=_relationships_for(spec.relationships, named),
        system_instruction=spec.system_instruction(),
        model_alias=model_alias,
    )

    # What a curator wrote goes on top, into the treatment only. This is the
    # strongest channel measured so far: on 2026-09-09 the service read a
    # column description and obeyed an instruction written in it, while the
    # same words in the catalog changed nothing. An authored line replaces the
    # estate's line for that column rather than joining it, because two
    # descriptions of one column is a contradiction, not more context.
    for table in named:
        entry = spec.authored.get(table)
        if entry is None:
            continue
        if entry.description:
            treatment.table_descriptions[table] = entry.description
        if entry.synonyms:
            treatment.table_synonyms[table] = list(entry.synonyms)
        if entry.tags:
            treatment.table_tags[table] = list(entry.tags)
        for column, body in sorted(entry.columns.items()):
            if body["description"]:
                treatment.field_descriptions.setdefault(table, {})[column] = body["description"]
            if body["synonyms"]:
                treatment.field_synonyms.setdefault(table, {})[column] = list(body["synonyms"])
            if body["tags"]:
                treatment.field_tags.setdefault(table, {})[column] = list(body["tags"])

    return control, treatment


def _relationships_for(relationships: list[dict], tables: list[str]) -> list[dict]:
    """Only joins whose both sides are in the arm.

    A join naming a table the agent cannot see is an instruction to use a table
    that is not there, which is worse than saying nothing.
    """
    named = set(tables)
    kept = []
    for relationship in relationships:
        left = str(relationship.get("left") or relationship.get("from") or "")
        right = str(relationship.get("right") or relationship.get("to") or "")
        if left in named and right in named:
            kept.append(relationship)
    return kept


# The heading that splits an overview page. Above it is what the table is;
# below it is what the rules are. Two different Context fields, and a page
# with no such heading is all description and no rules.
RULES_HEADING = "## Rules"
# The name a rules file gets when it came off a dataset entry rather
# than a table one. It is a name, not a heading in the page, so a
# round trip through the catalog keeps the tier it came from.
ESTATE_RULES = "estate rules"
# The rules file's own name, written into the page so the round trip keeps it.
# A tier is what a reader needs - which rules are estate-wide and which are
# this table's - and the file name is how a spec repo says it.
RULES_NAME_PREFIX = "### "


def split_overview(page: str) -> tuple[str, str]:
    """One overview page as (table description, rules).

    A catalog overview is one markdown page, and the Context wants two things
    from it: a sentence about what the table holds, which goes in
    `Schema.description`, and the business rules, which go in the system
    instruction. Nothing in the page says which is which, so the convention is
    a `## Rules` heading and it is written down here rather than guessed at
    read time.

    A page with no heading is all description. That is the safe way round: a
    rule mistaken for a description still reaches the model, in a field that
    demonstrably works, while a description mistaken for a rule is a sentence
    about a table sitting in a list of rules.
    """
    text = (page or "").strip()
    if not text:
        return "", ""
    lowered = text.lower()
    at = lowered.find(RULES_HEADING.lower())
    if at < 0:
        return text, ""
    description = text[:at].strip()
    rules = text[at + len(RULES_HEADING):].strip()
    return description, rules


def read_catalog(catalog, tables: list[str]) -> Spec:
    """A `Spec` built from Knowledge Catalog instead of from files.

    The same type `read_spec` returns, so `build_arms` and everything after it
    is untouched and a round cannot tell which source it was given. That is
    the point: file mode and catalog mode have to produce the same Context for
    the same content, or a lift measured in one says nothing about the other.

    `catalog` is anything with `resolve(table)` and `read_entry(name)`, which
    is what `catalog.Catalog` provides and what a fake in a test provides.

    A table the catalog does not know is not an error. An estate part way
    through being catalogued is the ordinary case, and refusing the whole run
    for one missing entry would make the tool unusable exactly when it is most
    needed. The table still goes into the arm, carrying nothing.
    """
    spec = Spec(root=pathlib.Path("(knowledge catalog)"))
    rules: list[str] = []
    for table in sorted(tables):
        entry_name = catalog.resolve(table)
        if not entry_name:
            spec.schemas[table] = TableSchema()
            continue
        entry = catalog.read_entry(entry_name)
        description, table_rules = split_overview(entry.get("overview") or "")
        # The estate's own description is what the control arm carries, and
        # the overview is written by a curator, so it belongs in `authored`.
        # Putting it in `schemas` would hand it to both arms and cancel out
        # the very thing the round is measuring.
        spec.schemas[table] = TableSchema()
        # Only when there is something authored. An empty description is not
        # a description, and inventing an entry for it made the catalog spec
        # carry 7 authored tables where the file carried 5.
        if description:
            spec.authored[table] = TableSchema(description=description)
        if table_rules:
            rules.append(f"{table}\n{table_rules}")

        for row in (entry.get("queries") or []):
            if str(row.get("source", "")).upper() != "USER":
                # AGENT rows are the catalog's own scan output, not something
                # a person reviewed, and an unreviewed example in the arm is
                # an unreviewed answer in the key.
                continue
            question = str(row.get("description") or "").strip()
            sql = str(row.get("sql") or "").strip()
            if question and sql:
                spec.examples.append(Example(question=question, sql=sql))

    # Rules above the table live on the dataset entry, because that is the
    # only entry above a table that holds an overview. Read first so they sit
    # first in `system_instruction`, which is the tier order the export
    # promises: estate, then table.
    datasets = sorted({table.rsplit(".", 1)[0] for table in tables})
    for dataset in datasets:
        entry_name = getattr(catalog, "resolve_dataset", lambda _d: None)(dataset)
        if not entry_name:
            continue
        _description, dataset_rules = split_overview(
            catalog.read_entry(entry_name).get("overview") or "")
        for name, text in _named_blocks(dataset_rules):
            spec.rules.append((name, text))

    if rules:
        spec.rules.append(("catalog overviews", "\n\n".join(rules)))

    spec.terms.extend(terms_from_catalog(catalog))
    return spec


def terms_from_catalog(catalog) -> list[Term]:
    """The glossary terms a catalog holds, as `Term` objects.

    Separate from `read_catalog` because it is the one part with no table in
    it: a glossary belongs to the estate, not to a table, so it is read once
    per spec rather than once per entry.

    EVERY term in the glossary is read, not only the ones this tool wrote.
    In catalog mode the catalog IS the context, so a definition a colleague
    curated by hand has to reach the agent -- filtering to our own label
    would mean the catalog arm could never carry anything a person added
    through the console, which is the main way anyone will use this.

    The consequence is deliberate and belongs with the decision: a term
    sitting in the glossary that the spec does not have makes the catalog arm
    genuinely differ from the file arm. `write_catalog` reports that rather
    than hiding it, and only offers to delete the ones carrying our label.

    `tier` comes back from the category the catalog filed the term under, so
    it is read rather than assumed. No category means the estate tier, which
    is `Term`'s own default.

    A catalog with no `read_terms` -- an older transport, or a fake in a test
    that predates it -- returns nothing rather than raising. That is what
    keeps `write_catalog`'s refusal honest: the refusal fires exactly when
    the read is unavailable.
    """
    glossary_id = getattr(catalog, "glossary_id", "") or ""
    reader = getattr(getattr(catalog, "transport", None), "read_terms", None)
    if not reader or not glossary_id:
        return []
    terms = []
    for row in reader(glossary_id) or []:
        name = (row.get("display_name") or row.get("term_id") or "").strip()
        definition = (row.get("description") or "").strip()
        if not name or not definition:
            # A term with no definition is a word, and a word in an arm is a
            # row in a hash with nothing behind it.
            continue
        category = (row.get("category_id") or "").strip()
        terms.append(Term(name=name, definition=definition,
                          tier=category or "org"))
    return sorted(terms, key=lambda t: t.name)


@dataclass
class CatalogWrite:
    """What a spec-to-catalog write did, and what it would not do."""

    written: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    examples_placed: dict[str, int] = field(default_factory=dict)
    # The terms this write will put in the glossary, and the two kinds of
    # term already there that the spec does not have. They are kept apart on
    # purpose: `terms_stale` carries this tool's label, so an earlier push of
    # ours wrote it and a rename or a deletion left it behind -- ours to
    # remove. `terms_not_ours` has no such label, so somebody curated it by
    # hand, it legitimately reaches the catalog arm, and nothing here will
    # ever propose deleting it.
    terms_to_write: list[Term] = field(default_factory=list)
    terms_stale: list[str] = field(default_factory=list)
    terms_not_ours: list[str] = field(default_factory=list)
    # The spec as the catalog can hold it: the input minus everything in
    # `refused`. The read-back is compared against THIS, not against the
    # input, or the check would fail every time something was refused and
    # a person would learn to ignore it.
    stored: "Spec | None" = None

    def text(self) -> str:
        lines = [f"{len(self.written)} entry/entries written"]
        for table in self.written:
            count = self.examples_placed.get(table, 0)
            lines.append(f"  {table}"
                         + (f", {count} worked example(s)" if count else ""))
        if self.terms_to_write:
            tiers = sorted({term.tier for term in self.terms_to_write})
            lines.append(f"  {len(self.terms_to_write)} glossary term(s), "
                         f"tier(s) {', '.join(tiers)}")
        # Both lists print, and each says what to do about it. A count with
        # no instruction is the "(0 linked)" failure this project already hit
        # once: a person reads it, cannot act on it, and stops reading.
        if self.terms_stale:
            lines.append(
                f"  {len(self.terms_stale)} term(s) in the glossary that this "
                f"spec no longer has, written by an earlier push: "
                f"{', '.join(self.terms_stale)}")
            lines.append(
                "    they still reach the catalog arm, so it differs from the "
                "file arm until they go. Remove them with --prune-terms")
        if self.terms_not_ours:
            lines.append(
                f"  {len(self.terms_not_ours)} term(s) in the glossary that "
                f"nobody here wrote: {', '.join(self.terms_not_ours)}")
            lines.append(
                "    they reach the catalog arm too. That is correct - the "
                "catalog is the context in catalog mode - but it is why a "
                "catalog arm can beat the same spec in file mode. Never "
                "deleted by this tool")
        for what, why in self.refused:
            lines.append(f"  refused {what}: {why}")
        return "\n".join(lines)


def table_rules(spec: Spec) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Rules split by table, and the ones that belong above a table.

    `read_catalog` writes per-table rules as one file whose blocks each start
    with a table name, so this reads that shape back. Anything else - the
    estate's definition of revenue, of a completed order - is a tier above the
    table and goes in the second list.

    Those are not homeless. The dataset entry has its own overview aspect, and
    a write to it was accepted and read back byte for byte on 2026-09-10. That
    matters because every rule in the thelook spec is that kind, and refusing
    them would have made the catalog arm strictly weaker than file mode for a
    reason that was never true.
    """
    by_table: dict[str, str] = {}
    above: list[str] = []
    known = set(spec.schemas) | set(spec.authored)
    for _name, body in spec.rules:
        blocks = [block.strip() for block in body.split("\n\n") if block.strip()]
        per_table = all(block.partition("\n")[0].strip() in known
                        and block.partition("\n")[2].strip() for block in blocks)
        if per_table and blocks:
            for block in blocks:
                head, _, rest = block.partition("\n")
                by_table[head.strip()] = rest.strip()
        elif body.strip():
            above.append((_name, body.strip()))
    return by_table, above


def place_examples(spec: Spec, tables: list[str]) -> tuple[dict[str, list], list]:
    """Each worked example on the table its SQL names first, and the rest.

    The Spec holds examples once for the whole arm; the catalog hangs them off
    an entry. So each one needs a table. The SQL names its own tables, and the
    first in name order is deterministic, which is what makes the write
    repeatable.

    An example naming no table in the arm is refused. Writing it to an
    arbitrary entry would mean reading it back on a round whose table list no
    longer includes that entry, and the example would vanish with no error.
    """
    placed: dict[str, list] = {table: [] for table in tables}
    refused = []
    for example in spec.examples:
        named = sorted(t for t in tables if t in example.sql)
        if not named:
            refused.append((example.question[:60],
                            "its SQL names none of the tables being written"))
            continue
        placed[named[0]].append(example)
    return placed, refused


def write_catalog(catalog, spec: Spec, tables: list[str],
                  *, examples: bool = True, dry_run: bool = False,
                  prune_terms: bool = False,
                  previous_out=None) -> CatalogWrite:
    """Put a spec repo into Knowledge Catalog, as the inverse of `read_catalog`.

    Without this, catalog mode has a reader and no writer: a round can read a
    catalog into an arm and there is no way to get a spec repo into a catalog
    to be read. The two together are what make the catalog a measured arm
    rather than a place numbers are copied to afterwards.

    The overview is written in the shape `split_overview` reads: the authored
    description, then a `## Rules` heading, then that table's rules. Examples
    become USER rows in the queries aspect.

    It does not verify. The caller reads back and compares, because a write
    that reports success and stores something else is the failure this whole
    tool exists to catch, and a function that grades its own homework cannot
    catch it.

    `dry_run` builds the same report and writes nothing. Same function, so a
    plan and the write it describes cannot disagree.

    `previous_out` saves what each entry held BEFORE the write, which is the
    only thing that makes a rollback possible: the catalog keeps no version
    of its own and an update replaces an aspect whole.
    """
    report = CatalogWrite()
    rules_by_table, above_table = table_rules(spec)

    # Named here rather than discovered by a hash mismatch. `read_catalog`
    # reads the overview and the queries aspect, and nothing else, so a term
    # written to a glossary would not come back and the read-back check would
    # fail with no reason a person could act on. Terms reach the arm through
    # `lookupEntryLinks`, which is not built.
    # Authored COLUMN descriptions have no home here, and the reason is not
    # that nobody built one. BigQuery owns column descriptions, and writing
    # them onto the evaluation view would hand them to the CONTROL arm too,
    # because the service reads a table's own descriptions by itself. The
    # room exists precisely because a view starts undocumented. So they stay
    # in the spec repo and reach the model in the request, which is what
    # file mode does.
    authored_columns = sum(len(t.columns) for t in spec.authored.values())
    if authored_columns:
        report.refused.append((
            f"{authored_columns} authored column description(s)",
            "BigQuery owns column descriptions and the catalog has no home "
            "for them. Written onto the evaluation view they would reach the "
            "control arm as well, which is the one thing the room exists to "
            "prevent. File mode delivers them in the request instead"))

    # Terms are written when, and only when, they can be read back. The
    # refusal below is not a policy about glossaries; it is the read-back
    # rule applied to one kind of content. `terms_from_catalog` returns
    # nothing when the transport has no `read_terms`, so the two agree by
    # construction rather than by someone remembering to update both.
    reader = getattr(getattr(catalog, "transport", None), "read_terms", None)
    glossary_id = getattr(catalog, "glossary_id", "") or ""
    if spec.terms and not reader:
        report.refused.append((
            f"{len(spec.terms)} glossary term(s)",
            "this catalog transport cannot read terms back, so writing them "
            "would lose them silently. Keep them in the spec repo"))
    elif spec.terms and not glossary_id:
        report.refused.append((
            f"{len(spec.terms)} glossary term(s)",
            "no glossary was named, so there is nowhere to write them and "
            "nothing to read back. Pass --glossary"))
    elif spec.terms:
        report.terms_to_write = list(spec.terms)
        # Terms already in the glossary that this spec does not have. Split
        # by whose they are, because the two need different handling and
        # merging them would make the tool either useless or dangerous:
        # one of ours is stale from an earlier push and is ours to remove,
        # one without our label belongs to somebody else and is reported so
        # a person knows the catalog arm carries it.
        wanted = {slug(term.name) for term in spec.terms}
        for row in (reader(glossary_id) if not dry_run else []) or []:
            if row.get("term_id") in wanted:
                continue
            if dict(row.get("labels") or {}).get(OWNER_LABEL) == OWNER_VALUE:
                report.terms_stale.append(row.get("term_id", ""))
            else:
                report.terms_not_ours.append(row.get("term_id", ""))

        # The write itself. The tier becomes the category, except the estate
        # tier, which is the glossary itself and has no category: a category
        # called "org" inside an org glossary would be a second name for the
        # same thing, and `terms_from_catalog` reads a term with no category
        # back as tier "org", so the round trip needs it absent, not empty.
        # THE DEFINITION IS WRITTEN VERBATIM, and that is a requirement rather
        # than a simplification. `terms_from_catalog` rebuilds
        # `Term.definition` from the stored description, and `Arm.arm_id`
        # hashes (name, definition). Folding the synonym list into the prose
        # here -- which is what the agent's view does -- would mean the term
        # read back is not the term written, so a catalog arm would not hash
        # like the spec it came from and the round trip could never pass.
        for term in spec.terms:
            if dry_run:
                continue
            catalog.transport.upsert_term(
                glossary_id, slug(term.name), term.name,
                term.definition, dict(LABELS),
                category_id="" if term.tier == "org" else slug(term.tier))

        # Which leaves the two structured lists with nowhere to go. A real
        # catalog holds a synonym as a LINK between two term entries, and
        # there is no link read to match `read_terms`, so writing them would
        # be the same silent loss the refusal above exists to prevent. Said
        # out loud instead, because the agent's prose view does include them
        # in file mode: a catalog arm is a smaller arm, and this is one of
        # the ways it is smaller.
        dropped = sum(len(term.synonyms) + len(term.related) for term in spec.terms)
        if dropped:
            report.refused.append((
                f"{dropped} synonym/related link(s) on {len(spec.terms)} term(s)",
                "a catalog holds these as links between term entries and "
                "there is no link read to pair with the term read, so they "
                "would not come back. The definition text is written in full; "
                "only the structured lists are left out"))

        # Pruning is off unless asked for, and only ever touches the stale
        # list. A push that silently deleted from a shared glossary to make
        # its own read-back match would be the worst kind of convenience.
        if prune_terms and not dry_run:
            deleter = getattr(catalog.transport, "delete_term", None)
            for term_id in report.terms_stale:
                if deleter and term_id:
                    deleter(glossary_id, term_id)

    # The estate tier first. One dataset or refuse: writing the same estate
    # rules to two dataset entries means reading them back twice, and an arm
    # carrying a rule twice does not hash like the spec it came from.
    if above_table:
        body = "\n\n".join(f"{RULES_NAME_PREFIX}{name}\n{text}"
                            for name, text in above_table)
        datasets = sorted({table.rsplit(".", 1)[0] for table in tables})
        if len(datasets) != 1:
            report.refused.append((
                "the rules above the table",
                f"they belong on a dataset entry and the tables span "
                f"{len(datasets)} datasets, so there is no single one to put "
                f"them on: {', '.join(datasets)}"))
        else:
            entry_name = catalog.resolve_dataset(datasets[0])
            if not entry_name:
                report.refused.append((
                    "the rules above the table",
                    f"no catalog entry for the dataset {datasets[0]}"))
            else:
                if not dry_run:
                    catalog.write_overview(entry_name, f"{RULES_HEADING}\n" + body)
                report.written.append(datasets[0])

    placed, refused_examples = ({}, [])
    if examples:
        placed, refused_examples = place_examples(spec, tables)
        report.refused.extend(refused_examples)

    for table in sorted(tables):
        entry = catalog.resolve(table)
        if not entry:
            report.refused.append((table, "no catalog entry for it"))
            continue
        description = (spec.authored.get(table) or TableSchema()).description
        rules = rules_by_table.get(table, "")
        page = description
        if rules:
            page = f"{description}\n\n{RULES_HEADING}\n{rules}".strip()
        # Read BEFORE the write. This is the only copy of the previous bytes
        # that will ever exist: the catalog keeps no version and an update
        # replaces an aspect whole.
        before = None
        if previous_out is not None and not dry_run:
            existing = catalog.read_entry(entry)
            before = {"overview": existing.get("overview"),
                      "queries": existing.get("queries")}
        if page and not dry_run:
            catalog.write_overview(entry, page)
        rows = [{"question": e.question, "sql": e.sql}
                for e in placed.get(table, [])]
        if rows:
            if not dry_run:
                catalog.transport.write_queries(entry, rows)
            report.examples_placed[table] = len(rows)
        # And read again AFTER, so `wrote` is what is actually there rather
        # than what this function believed it put there. The first version
        # recorded `queries: None` because the rows are written further down,
        # and every rollback then refused every table with rows, claiming
        # somebody had edited them.
        if before is not None:
            from .rollback import save
            now = catalog.read_entry(entry)
            save(previous_out, table, entry, before=before,
                 wrote={"overview": now.get("overview"),
                        "queries": now.get("queries")})

        if page or rows:
            report.written.append(table)

    # Rebuilt from the same inputs the writes used, so it cannot drift
    # from what actually went in.
    stored = Spec(root=pathlib.Path("(knowledge catalog)"),
                  examples=list(spec.examples), relationships=[])
    stored.rules.extend(above_table)
    for table in sorted(tables):
        stored.schemas[table] = TableSchema()
        described = (spec.authored.get(table) or TableSchema()).description
        if described:
            stored.authored[table] = TableSchema(description=described)
        if rules_by_table.get(table):
            stored.rules.append((table, rules_by_table[table]))
    report.stored = stored
    return report


def rename_tables(spec: Spec, mapping: dict[str, str]) -> Spec:
    """The same spec, written about different table names.

    A spec repo documents the business's own tables, because that is what a
    data team wrote and what they maintain. A round runs against the
    evaluation room's views. Asking a customer to rewrite their documentation
    to match an evaluation room would be asking them to keep two copies of
    the truth, and the second copy would drift.

    So the mapping lives here, applied once, and everything downstream sees
    one set of names. Rules are rewritten too: `read_catalog` writes a rules
    block whose first line is a table name, and a block naming a table the
    arm does not carry is a rule the export cannot place.

    A table with no entry in the mapping keeps its name, so a partial room -
    a few views over the tables that matter - works without listing the rest.
    """
    renamed = Spec(root=spec.root, terms=list(spec.terms),
                   examples=[], relationships=list(spec.relationships))
    for name, body in spec.rules:
        for old, new in mapping.items():
            body = body.replace(old, new)
        renamed.rules.append((name, body))
    for old_name, schema in spec.schemas.items():
        renamed.schemas[mapping.get(old_name, old_name)] = schema
    for old_name, schema in spec.authored.items():
        renamed.authored[mapping.get(old_name, old_name)] = schema
    # An example's SQL names the tables it reads, and it is handed to the
    # model as text. Left alone it would teach the model the production names
    # while the arm carries only the views, and every answer would name a
    # table the agent cannot see.
    for example in spec.examples:
        sql = example.sql
        for old, new in mapping.items():
            sql = sql.replace(old, new)
        renamed.examples.append(Example(question=example.question, sql=sql))
    return renamed


def _named_blocks(text: str) -> list[tuple[str, str]]:
    """Rules blocks and the file each came from, read back off a page.

    `write_catalog` writes `### <file name>` above each block so the round
    trip keeps it. A page with no such marker - one a person wrote in the
    console, or one written before this existed - is one block under the
    generic name, because refusing it would lose a curator's own work.
    """
    text = (text or "").strip()
    if not text:
        return []
    if not text.startswith(RULES_NAME_PREFIX):
        return [(ESTATE_RULES, text)]
    out = []
    for chunk in text.split(RULES_NAME_PREFIX):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, body = chunk.partition("\n")
        if body.strip():
            out.append((name.strip(), body.strip()))
    return out


SERVED_RULES = "served by the catalog"


def read_served(catalog, tables: list[str]) -> Spec:
    """A Spec whose whole context is what `lookupContext` serves, as text.

    This is Arm 1s. Arm 1 delivers the same content in the structured Context
    fields: a worked example goes in `example_queries`, a table description in
    `Schema.description`. Arm 1s delivers whatever the catalog hands a reader,
    as one block of prose in `system_instruction`.

    So `Arm 1s minus Arm 1` is the catalog's own contribution: what serving
    through Knowledge Catalog adds or loses against the same content delivered
    directly. It is the number the catalog team can act on, and nothing else
    in this framework produces it.

    The bundle is taken whole and not parsed. Parsing it would mean deciding
    which parts of a Preview format matter, and being wrong about that is how
    a measurement of the catalog turns into a measurement of our parser. The
    format is Preview and it will change; a change then moves the number,
    which is honest, rather than silently dropping a section.

    Measured on 2026-09-10: one table's bundle was 7,690 characters where its
    own overview was 101, and it carried the table's schema, its
    `sampleQueries`, and the dataset entry's overview under `relatedResources`.
    """
    spec = Spec(root=pathlib.Path("(lookupContext)"))
    blocks = []
    for table in sorted(tables):
        spec.schemas[table] = TableSchema()
        entry = catalog.resolve(table)
        if not entry:
            continue
        served = catalog.transport.lookup(entry, 0)
        if served.strip():
            blocks.append(f"{RULES_NAME_PREFIX}{table}\n{served.strip()}")
    if blocks:
        spec.rules.append((SERVED_RULES, "\n\n".join(blocks)))
    return spec
