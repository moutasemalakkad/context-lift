# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Read an Apache Ossie file: a semantic model somebody else already wrote.

Apache Ossie is an interchange format, meaning a YAML or JSON file shape any
tool can read and write, for semantic models. One file holds a model, its
datasets, their fields, the relationships between datasets, and metrics that
span them. Every level may carry an `ai_context` block. It was called Open
Semantic Interchange until July 2026, when it entered Apache incubation.

Why read it at all. A customer already on dbt MetricFlow, Sigma, Omni or
Databricks can export one Ossie file instead of this package growing four
readers. And a model the customer wrote is context nobody on this side
authored, which is the independence a worked example otherwise lacks.

Read only. Nothing here writes Ossie, and Ossie is never the source of truth:
the released spec is 0.1.1 with 0.2.0.dev0 in development, and the project
says field names and mappings may still change. `KNOWN_VERSIONS` is the list
this reader was tested against; anything else stops with a message rather than
being parsed on a guess.

What Ossie does NOT give. There is no worked example in it. `ai_context` has
an `examples` list, and the schema calls it "Sample questions or use cases":
plain strings, no SQL. A Conversational Analytics `example_queries` row needs
a question and its SQL, so those strings become candidate questions a person
writes SQL for, and the gold gate decides. Turning them into verified examples
would mean inventing the SQL and calling it reviewed.

Also out of scope: Ossie's other sub-format, an ontology document keyed on
`ontology:` with `concept:` entries. It models concepts and value types, not
tables and metrics, and nothing downstream corresponds to it. A file carrying
one is reported, not half-read.

Needs the [ossie] extra for `pyyaml`; JSON files need nothing. The import is
lazy so the core stays stdlib-only.
"""
from __future__ import annotations

import json
import pathlib
import re

from .semantic import Dimension, JoinPath, Metric, SemanticModel

# Versions this reader has been run against. 0.1.1 is the released spec and
# 0.2.0.dev0 is what `core-spec/ossie-schema.json` pins `version` to as a
# const, so both appear in the wild.
KNOWN_VERSIONS = ("0.1.1", "0.2.0.dev0")

# One metric may carry a different expression per dialect, and nothing in the
# spec checks that they agree: `SUM(amount)` for one and `AVG(amount)` for
# another both validate. So the choice is stated, not silent.
DIALECT_ORDER = ("BIGQUERY", "ANSI_SQL")


def _ai_context(node: dict) -> tuple[str, list[str], list[str]]:
    """The instructions, synonyms and sample questions on one node.

    `ai_context` is either a bare string or an object. The schema allows both,
    so a file using the short form is not an error and is read as instructions.
    """
    block = node.get("ai_context")
    if isinstance(block, str):
        return block.strip(), [], []
    if not isinstance(block, dict):
        return "", [], []
    instructions = str(block.get("instructions") or "").strip()
    synonyms = [str(word).strip() for word in (block.get("synonyms") or [])
                if str(word).strip()]
    examples = [str(text).strip() for text in (block.get("examples") or [])
                if str(text).strip()]
    return instructions, synonyms, examples


def _expression(node: dict) -> tuple[str, str]:
    """The SQL of one field or metric, and the dialect it came from.

    BIGQUERY wins when present because that is the warehouse under test, then
    ANSI_SQL, then whatever is listed first. The dialect is returned so a
    reader of the output can see which one was picked.
    """
    expression = node.get("expression")
    if isinstance(expression, str):
        return expression.strip(), ""
    if not isinstance(expression, dict):
        return "", ""
    dialects = [d for d in (expression.get("dialects") or []) if isinstance(d, dict)]
    if not dialects:
        return "", ""
    by_name = {str(d.get("dialect") or ""): d for d in dialects}
    for wanted in DIALECT_ORDER:
        if wanted in by_name:
            chosen = by_name[wanted]
            return str(chosen.get("expression") or "").strip(), wanted
    chosen = dialects[0]
    return (str(chosen.get("expression") or "").strip(),
            str(chosen.get("dialect") or ""))


def _load(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError as missing:  # pragma: no cover - depends on the extra
        raise SystemExit(
            "reading an Ossie YAML file needs pyyaml: "
            "`uv pip install -e '.[yaml]'`. A .json file needs nothing."
        ) from missing
    return yaml.safe_load(text)


def read_ossie(path: str | pathlib.Path,
               allow_untested_version: bool = False) -> SemanticModel:
    """One Ossie document as the same `SemanticModel` the LookML reader returns.

    Every model in the file is folded into one result. A dataset's `source` is
    its physical table, so that is what a metric, a field and a join path name,
    which keeps the output in the same vocabulary as everything downstream.
    """
    path = pathlib.Path(path)
    document = _load(path)
    if not isinstance(document, dict):
        raise SystemExit(f"{path} is not an Ossie document: expected a mapping")

    if "ontology" in document and "semantic_model" not in document:
        raise SystemExit(
            f"{path} is an Ossie ontology document, not a semantic model. It "
            "describes concepts and value types, which have no counterpart "
            "here. Nothing was read.")

    version = str(document.get("version") or "").strip()
    if version not in KNOWN_VERSIONS and not allow_untested_version:
        known = " or ".join(KNOWN_VERSIONS)
        raise SystemExit(
            f"{path} declares Ossie version {version or '(none)'}; this reader "
            f"was tested against {known}. The spec is before 1.0 and field "
            "names still move, so nothing was read. Pass "
            "--allow-untested-version to read it anyway.")

    models = document.get("semantic_model")
    if not isinstance(models, list) or not models:
        raise SystemExit(f"{path} has no `semantic_model` list")

    model = SemanticModel(source=f"Apache Ossie {version}: {path.name}",
                          spec_version=version)

    for one in models:
        if not isinstance(one, dict):
            continue
        _read_model(one, model)
    return model


def _read_model(one: dict, model: SemanticModel) -> None:
    """Fold one `semantic_model` entry into the result."""
    model_name = str(one.get("name") or "")
    instructions, synonyms, questions = _ai_context(one)
    if instructions:
        model.instructions.append(instructions)
    if synonyms and model_name:
        model.synonyms[model_name] = synonyms
    for question in questions:
        model.stated_questions.append((question, model_name))

    # A dataset's `source` is the physical table. Everything below refers to
    # tables by that name, never by the dataset's logical name, so two models
    # naming the same table land on the same string.
    table_of: dict[str, str] = {}
    for dataset in (one.get("datasets") or []):
        if isinstance(dataset, dict):
            _read_dataset(dataset, model, table_of)

    for relationship in (one.get("relationships") or []):
        if isinstance(relationship, dict):
            _read_relationship(relationship, model, table_of)

    for metric in (one.get("metrics") or []):
        if isinstance(metric, dict):
            _read_metric(metric, model, table_of)


def _read_dataset(dataset: dict, model: SemanticModel,
                  table_of: dict[str, str]) -> None:
    name = str(dataset.get("name") or "")
    table = str(dataset.get("source") or name)
    table_of[name] = table

    instructions, synonyms, questions = _ai_context(dataset)
    if instructions:
        model.instructions.append(f"{table}: {instructions}")
    if synonyms:
        model.synonyms[table] = synonyms
    for question in questions:
        model.stated_questions.append((question, table))

    # A composite primary key names several columns; each of them identifies a
    # row, so grouping by any one of them asks nothing.
    keys = {str(column) for column in (dataset.get("primary_key") or [])}
    for group in (dataset.get("unique_keys") or []):
        if isinstance(group, list):
            keys.update(str(column) for column in group)

    for field_node in (dataset.get("fields") or []):
        if not isinstance(field_node, dict):
            continue
        field_name = str(field_node.get("name") or "")
        sql, _ = _expression(field_node)
        instructions, synonyms, questions = _ai_context(field_node)
        if instructions:
            model.instructions.append(f"{table}.{field_name}: {instructions}")
        if synonyms:
            model.synonyms[field_name] = synonyms
        for question in questions:
            model.stated_questions.append((question, field_name))
        model.dimensions.append(Dimension(
            name=field_name,
            kind=str(field_node.get("datatype") or ""),
            description=str(field_node.get("description") or "").strip(),
            sql=sql,
            source_table=table,
            is_key=field_name in keys))


def _read_relationship(relationship: dict, model: SemanticModel,
                       table_of: dict[str, str]) -> None:
    left = table_of.get(str(relationship.get("from") or ""),
                        str(relationship.get("from") or ""))
    right = table_of.get(str(relationship.get("to") or ""),
                         str(relationship.get("to") or ""))
    from_columns = [str(c) for c in (relationship.get("from_columns") or [])]
    to_columns = [str(c) for c in (relationship.get("to_columns") or [])]
    # A composite key joins on several columns at once, so every pair is kept
    # and joined with AND. Dropping the extra pairs would state a join that
    # returns more rows than the real one.
    pairs = [f"{left}.{a} = {right}.{b}"
             for a, b in zip(from_columns, to_columns)]
    instructions, synonyms, _ = _ai_context(relationship)
    name = str(relationship.get("name") or "")
    if instructions:
        model.instructions.append(f"{name}: {instructions}")
    if synonyms and name:
        model.synonyms[name] = synonyms
    model.joins.append(JoinPath(left=left, right=right,
                               on=" AND ".join(pairs), relationship=name))


def _one_table(sql: str, table_of: dict[str, str]) -> str:
    """The one dataset a metric's expression names, or "" when it is not one.

    An Ossie metric belongs to the model, not to a dataset, so the format does
    not say which table it sits on. The expression does say, by naming the
    dataset: `SUM(store_sales.ss_ext_sales_price)`. Reading that is not a guess.

    Exactly one wins. `SUM(a.x) / COUNT(DISTINCT b.y)` spans two, and crossing
    it with either table's fields would produce a question whose answer needs
    the join, so it stays unattached and only its plain question is offered.
    """
    named = {name for name in table_of
             if re.search(rf"\b{re.escape(name)}\b", sql)}
    return table_of[named.pop()] if len(named) == 1 else ""


def _read_metric(metric: dict, model: SemanticModel,
                 table_of: dict[str, str]) -> None:
    name = str(metric.get("name") or "")
    sql, dialect = _expression(metric)
    instructions, synonyms, questions = _ai_context(metric)
    if instructions:
        model.instructions.append(f"{name}: {instructions}")
    if synonyms:
        model.synonyms[name] = synonyms
    for question in questions:
        model.stated_questions.append((question, name))
    model.metrics.append(Metric(
        name=name,
        kind=dialect,
        description=str(metric.get("description") or "").strip(),
        sql=sql,
        source_table=_one_table(sql, table_of)))
