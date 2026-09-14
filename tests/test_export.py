# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The spec repo read into two arms."""
from __future__ import annotations

import json

import pytest

from context_lift.export import PRECEDENCE, build_arms, read_spec

yaml = pytest.importorskip("yaml")

TABLE = "p.d.orders"
OTHER = "p.d.users"


def make_spec(tmp_path, *, rules=None, schemas=None, vocabulary=None,
              examples=None, relationships=None):
    root = tmp_path / "spec"
    root.mkdir()
    for name, body in (rules or {}).items():
        (root / "rules").mkdir(exist_ok=True)
        (root / "rules" / f"{name}.md").write_text(body, encoding="utf-8")
    for table, body in (schemas or {}).items():
        (root / "schema").mkdir(exist_ok=True)
        (root / "schema" / f"{table}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    if vocabulary is not None:
        (root / "vocabulary").mkdir(exist_ok=True)
        (root / "vocabulary" / "terms.yaml").write_text(
            yaml.safe_dump(vocabulary), encoding="utf-8")
    if examples is not None:
        (root / "examples").mkdir(exist_ok=True)
        (root / "examples" / "bank.jsonl").write_text(
            "\n".join(json.dumps(row) for row in examples), encoding="utf-8")
    if relationships is not None:
        (root / "relationships.yaml").write_text(
            yaml.safe_dump(relationships), encoding="utf-8")
    return root


SCHEMA = {"description": "Every order.", "synonyms": ["sales"], "tags": ["retail"],
          "columns": {"status": {"description": "Order state.", "synonyms": ["state"]},
                      "num_of_item": "Item count."}}


def test_the_control_carries_only_what_the_estate_already_had(tmp_path):
    """The service reads table and column descriptions on its own and follows
    instructions written in them, so a control without them would credit
    authored context for the customer's own build (design doc 3.6)."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA},
                     rules={"00-org": "Exclude test stores."},
                     vocabulary=[{"name": "revenue", "definition": "Sale price summed."}],
                     examples=[{"question": "how many?", "sql": "SELECT 1"}])

    control, treatment = build_arms(read_spec(root))

    assert control.table_descriptions == {TABLE: "Every order."}
    assert control.field_descriptions == {TABLE: {"status": "Order state.",
                                                  "num_of_item": "Item count."}}
    assert control.terms == [] and control.examples == []
    assert control.system_instruction == ""
    assert treatment.terms and treatment.examples and treatment.system_instruction


def test_the_control_is_a_strict_subset_of_the_treatment(tmp_path):
    """Built from the control rather than beside it, so no later edit can make
    the control carry something the treatment lacks. If it could, the lift
    would not be the authored context alone."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA},
                     rules={"00-org": "Exclude test stores."})

    control, treatment = build_arms(read_spec(root))

    assert treatment.table_descriptions == control.table_descriptions
    assert treatment.field_descriptions == control.field_descriptions
    assert treatment.table_synonyms == control.table_synonyms
    assert treatment.field_tags == control.field_tags
    assert control.arm_id != treatment.arm_id


def test_editing_the_treatment_does_not_reach_back_into_the_control(tmp_path):
    """A shared dict would make the two arms move together, and the pair would
    silently stop being a pair."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA})

    control, treatment = build_arms(read_spec(root))
    treatment.field_descriptions[TABLE]["status"] = "changed"

    assert control.field_descriptions[TABLE]["status"] == "Order state."


def test_rules_are_concatenated_in_filename_order_and_precedence_is_stated(tmp_path):
    """The filename orders the tier. Nothing else tells the model which of two
    disagreeing rules wins, so it is said once at the end."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA}, rules={
        "20-wholesale": "One row per order key.",
        "00-org": "Fiscal year starts in February.",
        "10-sales": "Exclude test stores."})

    instruction = build_arms(read_spec(root))[1].system_instruction

    assert instruction.index("Fiscal year") < instruction.index("Exclude test")
    assert instruction.index("Exclude test") < instruction.index("One row per")
    assert instruction.endswith(PRECEDENCE)
    assert "# 00-org" in instruction


def test_a_terms_other_names_join_its_definition(tmp_path):
    """The Context gives a term a name and a description and nothing else, so a
    synonym has nowhere of its own to go there."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA}, vocabulary=[
        {"name": "net sales", "definition": "Sales after returns.",
         "also_called": ["NSO", "units sold"]}])

    term = build_arms(read_spec(root))[1].terms[0]

    assert term.definition == "Sales after returns. Also called: NSO, units sold."


def test_a_join_naming_a_table_the_arm_cannot_see_is_dropped(tmp_path):
    """Telling the model to use a table that is not there is worse than saying
    nothing about joins at all."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA, OTHER: {"description": "Users."}},
                     relationships=[{"left": TABLE, "right": OTHER, "on": "a = b"},
                                    {"left": TABLE, "right": "p.d.absent", "on": "a = c"}])

    treatment = build_arms(read_spec(root), tables=[TABLE, OTHER])[1]

    assert [r["right"] for r in treatment.relationships] == [OTHER]


def test_a_table_with_no_schema_file_stops_the_run(tmp_path):
    """Without it the control cannot carry what the estate already had, and the
    lift would credit authored context for the customer's own descriptions."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA})

    with pytest.raises(SystemExit) as refused:
        build_arms(read_spec(root), tables=[TABLE, OTHER])

    assert OTHER in str(refused.value)


def test_a_schema_file_is_named_for_its_table(tmp_path):
    root = tmp_path / "spec"
    (root / "schema").mkdir(parents=True)
    (root / "schema" / "orders.yaml").write_text("description: x\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        read_spec(root)

    assert "project.dataset.table" in str(refused.value)


def test_a_broken_example_line_stops_the_read_and_names_the_line(tmp_path):
    """A JSONL file that half-parses is how an example bank silently shrinks."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA},
                     examples=[{"question": "q", "sql": "SELECT 1"}])
    path = root / "examples" / "bank.jsonl"
    path.write_text(path.read_text() + "\n{not json}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        read_spec(root)

    assert "line 2" in str(refused.value)


def test_an_absent_directory_is_empty_not_an_error(tmp_path):
    """A first round is often rules and nothing else."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA})

    spec = read_spec(root)

    assert spec.terms == [] and spec.examples == [] and spec.relationships == []
    assert spec.tables == [TABLE]


def test_reading_the_same_spec_twice_gives_the_same_arm_hash(tmp_path):
    """Two runs that claim to be the same arm have to be provably the same."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA},
                     rules={"00-org": "Exclude test stores."},
                     examples=[{"question": "q", "sql": "SELECT 1"}])

    first = build_arms(read_spec(root))[1]
    second = build_arms(read_spec(root))[1]

    assert first.arm_id == second.arm_id


def test_a_json_spec_file_reads_without_pyyaml(tmp_path):
    root = tmp_path / "spec"
    (root / "schema").mkdir(parents=True)
    (root / "schema" / f"{TABLE}.json").write_text(json.dumps(SCHEMA), encoding="utf-8")

    assert read_spec(root).schemas[TABLE].description == "Every order."


AUTHORED = {"description": "One row per order line, the grain everything else counts.",
            "columns": {"status": {"description": "Line state. Revenue excludes "
                                                  "Cancelled and Returned."}}}


def test_an_authored_description_reaches_the_treatment_and_not_the_control(tmp_path):
    """The channel a probe on 2026-09-09 showed to be the strongest: the
    service reads a column description and obeys instructions written in it,
    while the same words in the catalog changed nothing. It has to be
    expressible on one side of the pair or it cannot be measured."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA})
    (root / "authored").mkdir()
    (root / "authored" / f"{TABLE}.yaml").write_text(
        yaml.safe_dump(AUTHORED), encoding="utf-8")

    control, treatment = build_arms(read_spec(root))

    assert control.table_descriptions[TABLE] == "Every order."
    assert treatment.table_descriptions[TABLE].startswith("One row per order line")
    assert "Revenue excludes" in treatment.field_descriptions[TABLE]["status"]
    assert control.field_descriptions[TABLE]["status"] == "Order state."


def test_an_authored_line_replaces_the_estates_line_rather_than_joining_it(tmp_path):
    """Two descriptions of one column is a contradiction, not more context."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA})
    (root / "authored").mkdir()
    (root / "authored" / f"{TABLE}.yaml").write_text(
        yaml.safe_dump(AUTHORED), encoding="utf-8")

    treatment = build_arms(read_spec(root))[1]

    assert "Order state." not in treatment.field_descriptions[TABLE]["status"]


def test_a_table_with_only_an_authored_file_still_needs_its_schema_file(tmp_path):
    """schema/ is what the estate had. Its absence is not the same as an empty
    file, and the control has to say which of the two it is."""
    root = make_spec(tmp_path, schemas={TABLE: SCHEMA})
    (root / "authored").mkdir()
    (root / "authored" / f"{OTHER}.yaml").write_text(
        yaml.safe_dump(AUTHORED), encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        build_arms(read_spec(root), tables=[TABLE, OTHER])

    assert OTHER in str(refused.value)


class FakeCatalog:
    """A catalog that knows two tables and nothing about a third."""

    def __init__(self, entries):
        self.entries = entries

    def resolve(self, table):
        return f"entries/{table}" if table in self.entries else None

    def resolve_dataset(self, dataset):
        """The live catalog has an entry per dataset, with its own overview."""
        self.entries.setdefault(dataset, {"overview": None, "queries": None})
        return f"entries/{dataset}"

    def read_entry(self, name):
        return self.entries[name.split("/", 1)[1]]

    # The write side, so a round trip can be exercised without a cloud. The
    # shapes match the live transport: an overview is one string, a queries
    # row is {description, sql, source}.
    def write_overview(self, entry_name, text):
        self.entries.setdefault(entry_name.split("/", 1)[1], {})["overview"] = text

    @property
    def transport(self):
        return self

    def write_queries(self, entry_name, rows):
        entry = self.entries.setdefault(entry_name.split("/", 1)[1], {})
        entry["queries"] = [{"description": r["question"], "sql": r["sql"],
                             "source": "USER"} for r in rows]


def test_an_overview_splits_into_a_description_and_rules():
    """A catalog overview is one page and the Context wants two things from
    it. Nothing in the page says which is which, so the convention is written
    down rather than guessed at read time."""
    from context_lift.export import split_overview

    description, rules = split_overview(
        "One row per order line.\n\n## Rules\n- Revenue excludes returns.")

    assert description == "One row per order line."
    assert rules == "- Revenue excludes returns."


def test_a_page_with_no_rules_heading_is_all_description():
    """The safe way round: a rule mistaken for a description still reaches the
    model in a field that demonstrably works, while a description mistaken for
    a rule is a sentence about a table sitting in a list of rules."""
    from context_lift.export import split_overview

    assert split_overview("Just a description.") == ("Just a description.", "")


def test_the_catalog_reader_returns_the_same_spec_type_as_the_file_reader():
    """So `build_arms` and everything after it is untouched, and a round
    cannot tell which source it was given."""
    from context_lift.export import read_catalog

    catalog = FakeCatalog({TABLE: {
        "overview": "Every order.\n\n## Rules\n- Exclude test stores.",
        "queries": [{"description": "how many?", "sql": "SELECT 1",
                     "source": "USER"}]}})

    spec = read_catalog(catalog, [TABLE])
    control, treatment = build_arms(spec)

    assert treatment.table_descriptions[TABLE] == "Every order."
    assert "Exclude test stores." in treatment.system_instruction
    assert [e.question for e in treatment.examples] == ["how many?"]


def test_an_overview_reaches_the_treatment_arm_only():
    """The overview is written by a curator. Putting it in `schemas` would
    hand it to both arms and cancel out the thing the round is measuring."""
    from context_lift.export import read_catalog

    catalog = FakeCatalog({TABLE: {"overview": "Every order.", "queries": []}})

    control, treatment = build_arms(read_catalog(catalog, [TABLE]))

    assert control.table_descriptions == {}
    assert treatment.table_descriptions[TABLE] == "Every order."


def test_a_scan_written_example_is_not_taken_as_a_worked_example():
    """AGENT rows are the catalog's own scan output, not something a person
    reviewed, and an unreviewed example in the arm is an unreviewed answer in
    the key."""
    from context_lift.export import read_catalog

    catalog = FakeCatalog({TABLE: {"overview": "", "queries": [
        {"description": "reviewed", "sql": "SELECT 1", "source": "USER"},
        {"description": "guessed", "sql": "SELECT 2", "source": "AGENT"}]}})

    spec = read_catalog(catalog, [TABLE])

    assert [e.question for e in spec.examples] == ["reviewed"]


def test_a_table_the_catalog_does_not_know_still_joins_the_arm():
    """An estate part way through being catalogued is the ordinary case, and
    refusing the whole run for one missing entry makes the tool unusable
    exactly when it is most needed."""
    from context_lift.export import read_catalog

    catalog = FakeCatalog({TABLE: {"overview": "Every order.", "queries": []}})

    spec = read_catalog(catalog, [TABLE, OTHER])
    control, treatment = build_arms(spec)

    assert set(control.tables) == {TABLE, OTHER}
    assert OTHER not in treatment.table_descriptions


def test_the_same_content_from_files_and_from_the_catalog_builds_the_same_request(tmp_path):
    """The test the whole two-mode design rests on.

    If the same words produce a different request depending on where they were
    read from, a lift measured in file mode says nothing about catalog mode,
    and the catalog arm is measuring the reader rather than the context.
    """
    import json

    from context_lift.export import read_catalog
    from context_lift.runners.ca_api import build_inline_context

    description = "Every order."
    rule = "- Exclude test stores."
    question, sql = "how many?", "SELECT 1"

    # The same content, written as files.
    root = make_spec(tmp_path, schemas={TABLE: {"description": "", "columns": {}}},
                     rules={"catalog overviews": f"{TABLE}\n{rule}"},
                     examples=[{"question": question, "sql": sql}])
    (root / "authored").mkdir()
    (root / "authored" / f"{TABLE}.yaml").write_text(
        yaml.safe_dump({"description": description}), encoding="utf-8")
    from_files = build_arms(read_spec(root))[1]

    # The same content, in a catalog.
    catalog = FakeCatalog({TABLE: {
        "overview": f"{description}\n\n## Rules\n{rule}",
        "queries": [{"description": question, "sql": sql, "source": "USER"}]}})
    from_catalog = build_arms(read_catalog(catalog, [TABLE]))[1]

    assert (json.dumps(build_inline_context(from_files), sort_keys=True)
            == json.dumps(build_inline_context(from_catalog), sort_keys=True))
    assert from_files.arm_id == from_catalog.arm_id, (
        "two arms carrying the same context must hash the same, or the ledger "
        "records them as different experiments")


def test_a_spec_written_to_a_catalog_reads_back_as_the_same_arm(tmp_path):
    """The round trip that makes the catalog an arm and not a filing cabinet.

    Until `write_catalog` existed, catalog mode had a reader and no writer:
    `read_catalog` could turn a catalog into an arm, and nothing could turn a
    spec repo into a catalog for it to read. This writes one and reads it
    back, and the two arms must hash the same.

    They must, because a round in catalog mode and a round in file mode are
    meant to be the same experiment. If the write loses a rule or reorders an
    example, the two hashes differ and the ledger records one experiment as
    two, which is exactly the confusion the hash is there to prevent.
    """
    from context_lift.export import read_catalog, write_catalog

    root = make_spec(
        tmp_path,
        schemas={TABLE: {"description": "", "columns": {}}},
        rules={"catalog overviews": f"{TABLE}\n- Exclude test stores."},
        examples=[{"question": "how many?", "sql": f"SELECT 1 FROM `{TABLE}`"}])
    (root / "authored").mkdir(exist_ok=True)
    (root / "authored" / f"{TABLE}.yaml").write_text(
        yaml.safe_dump({"description": "Every order."}), encoding="utf-8")
    spec = read_spec(root)

    catalog = FakeCatalog({TABLE: {"overview": None, "queries": None}})
    report = write_catalog(catalog, spec, [TABLE])
    assert report.written == [TABLE]
    assert report.refused == []

    assert (build_arms(spec)[1].arm_id
            == build_arms(read_catalog(catalog, [TABLE]))[1].arm_id)


def test_a_rule_above_the_table_round_trips_through_the_dataset_entry(tmp_path):
    """The estate's own definitions have a home, and it is the dataset entry.

    Every rule in the thelook spec is this kind - what revenue means, what a
    completed order is - and they are the ones that bought the lift. If they
    could not be written, the catalog arm would be weaker than file mode for
    a reason that has nothing to do with the context.
    """
    from context_lift.export import read_catalog, write_catalog

    rule = "- The fiscal year starts on 1 February."
    root = make_spec(tmp_path,
                     schemas={TABLE: {"description": "", "columns": {}}},
                     rules={"org": rule})
    catalog = FakeCatalog({TABLE: {"overview": None, "queries": None}})

    report = write_catalog(catalog, read_spec(root), [TABLE])
    assert report.refused == []

    assert rule in read_catalog(catalog, [TABLE]).system_instruction()


def test_estate_rules_are_refused_when_the_tables_span_two_datasets(tmp_path):
    """Written to both, they would read back twice and the arm would not hash
    like the spec it came from. Refused with both dataset names."""
    from context_lift.export import write_catalog

    elsewhere = "p.warehouse.orders"        # OTHER shares p.d with TABLE
    root = make_spec(tmp_path,
                     schemas={TABLE: {"description": "", "columns": {}},
                              elsewhere: {"description": "", "columns": {}}},
                     rules={"org": "- The fiscal year starts on 1 February."})
    catalog = FakeCatalog({TABLE: {"overview": None, "queries": None},
                           elsewhere: {"overview": None, "queries": None}})

    report = write_catalog(catalog, read_spec(root), [TABLE, elsewhere])

    assert any("2 datasets" in why for _, why in report.refused)


def test_an_example_naming_no_written_table_is_refused(tmp_path):
    """It would be written to an entry no later round reads, and vanish."""
    from context_lift.export import write_catalog

    root = make_spec(tmp_path,
                     schemas={TABLE: {"description": "", "columns": {}}},
                     examples=[{"question": "elsewhere?",
                                "sql": "SELECT 1 FROM `other.place.thing`"}])
    catalog = FakeCatalog({TABLE: {"overview": None, "queries": None}})

    report = write_catalog(catalog, read_spec(root), [TABLE])

    assert any("names none of the tables" in why for _, why in report.refused)


def test_the_report_says_what_the_catalog_cannot_hold(tmp_path):
    """Named, not left to show up as a hash that does not match.

    Two things the catalog cannot carry, for different reasons. Terms are
    not read back. Authored column descriptions are owned by BigQuery, and
    writing them onto the evaluation view would hand them to the control arm
    as well, which is the one thing the room exists to prevent.
    """
    from context_lift.export import write_catalog

    root = make_spec(
        tmp_path,
        schemas={TABLE: {"description": "", "columns": {}}},
        vocabulary=[{"name": "revenue", "definition": "sales minus returns"}])
    (root / "authored").mkdir(exist_ok=True)
    (root / "authored" / f"{TABLE}.yaml").write_text(
        yaml.safe_dump({"description": "Every order.",
                        "columns": {"status": {"description": "one of five"}}}),
        encoding="utf-8")
    catalog = FakeCatalog({TABLE: {"overview": None, "queries": None}})

    report = write_catalog(catalog, read_spec(root), [TABLE])

    reasons = " ".join(why for _what, why in report.refused)
    assert "control arm" in reasons, "column descriptions must name the reason"
    assert "not read terms back" in reasons


def test_the_stored_spec_is_what_the_read_back_is_compared_against(tmp_path):
    """Comparing to the input would fail on every refusal.

    A check that fails every time is a check a person learns to ignore, and
    then it catches nothing on the day the write really is wrong.
    """
    from context_lift.export import read_catalog, write_catalog

    root = make_spec(
        tmp_path,
        schemas={TABLE: {"description": "", "columns": {}}},
        rules={"org": "- The fiscal year starts on 1 February."},
        vocabulary=[{"name": "revenue", "definition": "sales minus returns"}])
    spec = read_spec(root)
    catalog = FakeCatalog({TABLE: {"overview": None, "queries": None}})

    report = write_catalog(catalog, spec, [TABLE])

    # The term was refused, so the input arm and the stored arm differ...
    assert build_arms(spec, [TABLE])[1].arm_id != \
        build_arms(report.stored, [TABLE])[1].arm_id
    # ...and the stored arm is exactly what comes back out.
    assert build_arms(report.stored, [TABLE])[1].arm_id == \
        build_arms(read_catalog(catalog, [TABLE]), [TABLE])[1].arm_id


def test_the_served_arm_carries_no_example_queries_field_at_all():
    """Which is why it cannot produce a stored-example lookup.

    In the ordinary catalog arm a worked example goes in `example_queries`,
    and the API can match it and run it verbatim instead of writing SQL. On
    2026-09-10 that happened on 22 of 23 working questions and left the round
    measuring one. The served arm delivers the same examples as prose inside
    one instruction, so there is no field to match against and the model
    writes SQL. Confirmed with one live call: the same question that produced
    a lookup in the ordinary arm returned SQL here, with matched_example
    empty.
    """
    from context_lift.export import read_served
    from context_lift.runners.ca_api import build_inline_context

    class Served:
        transport = None

        def resolve(self, table):
            return f"entries/{table}"

    served = Served()
    served.transport = type("T", (), {
        "lookup": staticmethod(lambda entry, budget:
                               "resources:\n overview: one row per order\n"
                               " sampleQueries:\n  - revenue by month")})()

    spec = read_served(served, [TABLE])
    body = build_inline_context(build_arms(spec, [TABLE])[1])

    assert not body.get("exampleQueries"), (
        "an example_queries field is what the API matches against; the served "
        "arm must carry none, or it can answer by lookup like the other one")
    assert "sampleQueries" in body["systemInstruction"], (
        "the examples still reach the model, as prose")


def test_the_served_bundle_is_taken_whole_and_not_parsed():
    """Parsing a Preview format means deciding which parts matter, and being
    wrong about that turns a measurement of the catalog into a measurement of
    our parser. A format change should move the number, not silently drop a
    section."""
    from context_lift.export import read_served

    bundle = "resources:\n a: 1\n weird_new_key: something\n overview: page"

    class Served:
        transport = type("T", (), {"lookup": staticmethod(lambda e, b: bundle)})()

        def resolve(self, table):
            return f"entries/{table}"

    spec = read_served(Served(), [TABLE])

    assert "weird_new_key" in spec.system_instruction()
