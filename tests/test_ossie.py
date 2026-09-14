# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The Apache Ossie reader.

Shapes here follow `core-spec/ossie-schema.json` of `apache/ossie`, read on
2026-09-09, and `examples/tpcds_semantic_model.yaml` from the same commit.
"""
from __future__ import annotations

import pytest

from context_lift.ossie import KNOWN_VERSIONS, read_ossie

yaml = pytest.importorskip("yaml")


def write(tmp_path, text, name="model.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


MODEL = """
version: "0.2.0.dev0"
semantic_model:
  - name: retail
    description: Sales and customers
    ai_context:
      instructions: "Exclude test stores."
    datasets:
      - name: store_sales
        source: proj.public.store_sales
        primary_key: [ss_item_sk, ss_ticket_number]
        description: Every sales line
        ai_context:
          synonyms: ["retail sales", "POS data"]
        fields:
          - name: ss_item_sk
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: ss_item_sk
            description: Foreign key to item
            datatype: Integer
          - name: ss_quantity
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: ss_quantity
                - dialect: BIGQUERY
                  expression: SAFE_CAST(ss_quantity AS INT64)
            description: Units sold
            datatype: Integer
            ai_context:
              synonyms: ["units sold"]
      - name: item
        source: proj.public.item
        fields:
          - name: i_item_sk
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: i_item_sk
    relationships:
      - name: sales_to_item
        from: store_sales
        to: item
        from_columns: [ss_item_sk]
        to_columns: [i_item_sk]
    metrics:
      - name: total_sales
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(store_sales.ss_ext_sales_price)
        description: Total sales revenue
        ai_context:
          synonyms: ["gross sales"]
          examples: ["What were total sales last quarter?"]
"""


def test_a_model_becomes_metrics_dimensions_and_joins(tmp_path):
    model = read_ossie(write(tmp_path, MODEL))

    assert [m.name for m in model.metrics] == ["total_sales"]
    assert {d.name for d in model.dimensions} == {
        "ss_item_sk", "ss_quantity", "i_item_sk"}
    assert len(model.joins) == 1
    assert model.spec_version == "0.2.0.dev0"


def test_a_join_names_physical_tables_not_logical_dataset_names(tmp_path):
    """Everything downstream speaks in `project.dataset.table`. A dataset's
    logical name is local to the file, so two files naming the same table
    would otherwise never line up."""
    join = read_ossie(write(tmp_path, MODEL)).joins[0]

    assert join.left == "proj.public.store_sales"
    assert join.right == "proj.public.item"
    assert join.on == "proj.public.store_sales.ss_item_sk = proj.public.item.i_item_sk"


def test_the_bigquery_dialect_wins_and_the_choice_is_recorded(tmp_path):
    """One metric may carry a different expression per dialect and nothing in
    the spec checks they agree, so which one was read has to be visible."""
    model = read_ossie(write(tmp_path, MODEL))
    quantity = next(d for d in model.dimensions if d.name == "ss_quantity")

    assert quantity.sql == "SAFE_CAST(ss_quantity AS INT64)"
    assert next(m for m in model.metrics if m.name == "total_sales").kind == "ANSI_SQL"


def test_a_primary_key_column_is_marked_and_never_becomes_a_question(tmp_path):
    model = read_ossie(write(tmp_path, MODEL))
    item_key = next(d for d in model.dimensions if d.name == "ss_item_sk")

    assert item_key.is_key
    assert not item_key.groupable


def test_examples_become_candidate_questions_and_never_worked_examples(tmp_path):
    """`ai_context.examples` is a list of plain strings the schema calls
    "Sample questions or use cases". There is no SQL in it, so it cannot become
    an `example_queries` row without somebody inventing the query."""
    model = read_ossie(write(tmp_path, MODEL))

    assert model.stated_questions == [
        ("What were total sales last quarter?", "total_sales")]
    assert ("What were total sales last quarter?",
            "total_sales") in model.to_candidates()


def test_a_question_a_person_wrote_comes_before_one_this_code_assembles(tmp_path):
    model = read_ossie(write(tmp_path, MODEL))

    assert model.to_candidates()[0][0] == "What were total sales last quarter?"


def test_instructions_are_collected_with_the_level_they_sat_on(tmp_path):
    model = read_ossie(write(tmp_path, MODEL))

    assert model.instructions == ["Exclude test stores."]
    assert "# Rules" in model.to_context()
    assert "- Exclude test stores." in model.to_context()


def test_synonyms_reach_the_context_as_an_also_called_line(tmp_path):
    context = read_ossie(write(tmp_path, MODEL)).to_context()

    assert "also called: gross sales" in context
    assert "also called: units sold" in context


def test_ai_context_may_be_a_bare_string(tmp_path):
    """The schema's `oneOf` allows a plain string, so the short form is not an
    error and reads as instructions."""
    text = MODEL.replace(
        '    ai_context:\n      instructions: "Exclude test stores."',
        '    ai_context: "Exclude test stores."')
    model = read_ossie(write(tmp_path, text))

    assert model.instructions == ["Exclude test stores."]


def test_an_untested_spec_version_stops_rather_than_being_guessed_at(tmp_path):
    """The spec is before 1.0 and field names still move. Reading one on a
    guess would produce a model that looks fine and is wrong."""
    path = write(tmp_path, MODEL.replace('"0.2.0.dev0"', '"9.9.9"'))

    with pytest.raises(SystemExit) as refused:
        read_ossie(path)

    assert "9.9.9" in str(refused.value)
    assert KNOWN_VERSIONS[0] in str(refused.value)
    assert read_ossie(path, allow_untested_version=True).spec_version == "9.9.9"


def test_an_ontology_document_is_refused_by_name(tmp_path):
    """Ossie's other sub-format models concepts and value types. Nothing here
    corresponds to it, so it is reported rather than half-read."""
    path = write(tmp_path, "version: 0.2.0.dev0\nontology:\n- concept: NrFeet\n")

    with pytest.raises(SystemExit) as refused:
        read_ossie(path)

    assert "ontology" in str(refused.value)


def test_a_composite_key_joins_on_every_column_pair(tmp_path):
    """Dropping the extra pairs would state a join that returns more rows than
    the real one."""
    text = MODEL.replace(
        "        from_columns: [ss_item_sk]\n        to_columns: [i_item_sk]",
        "        from_columns: [ss_item_sk, ss_ticket_number]\n"
        "        to_columns: [i_item_sk, i_ticket]")
    join = read_ossie(write(tmp_path, text)).joins[0]

    assert " AND " in join.on
    assert join.on.endswith("proj.public.store_sales.ss_ticket_number = "
                            "proj.public.item.i_ticket")


def test_a_metric_takes_the_one_table_its_expression_names(tmp_path):
    """Ossie puts metrics on the model, not on a dataset, so the format never
    says which table. The expression does, by naming the dataset."""
    metric = read_ossie(write(tmp_path, MODEL)).metrics[0]

    assert metric.sql == "SUM(store_sales.ss_ext_sales_price)"
    assert metric.source_table == "proj.public.store_sales"


def test_a_metric_spanning_two_tables_stays_unattached(tmp_path):
    """`SUM(a.x) / COUNT(DISTINCT b.y)` needs the join. Crossing it with either
    table's fields would offer a question whose answer is not on that table."""
    text = MODEL.replace(
        "              expression: SUM(store_sales.ss_ext_sales_price)",
        "              expression: SUM(store_sales.ss_ext_sales_price) / "
        "COUNT(DISTINCT item.i_item_sk)")
    model = read_ossie(write(tmp_path, text))

    assert model.metrics[0].source_table == ""
    # The plain question is still offered; only the "by <field>" crossings go,
    # because those would name a table the metric does not sit on.
    questions = [q for q, _ in model.to_candidates()]
    assert "What is total sales revenue?" in questions
    assert not [q for q in questions if " by " in q]


def test_a_json_ossie_file_reads_without_pyyaml(tmp_path):
    import json
    path = tmp_path / "model.json"
    path.write_text(json.dumps(yaml.safe_load(MODEL)), encoding="utf-8")

    assert read_ossie(path).spec_version == "0.2.0.dev0"
