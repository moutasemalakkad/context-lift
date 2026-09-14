# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The probe that decides which arm the page calls the product arm.

Every test injects the answers, so the decision is checked with no call.
"""
from context_lift.sentinel import (Observation, materials, plan, probe, report,
                               verdict, where_seen)

TABLE = "p.eval.orders"


def _mats():
    return materials(TABLE, join_partner="p.eval.customers")


def _sql(text):
    return [{"systemMessage": {"data": {"generatedSql": text}}}]


def _answer(text, kind="FINAL_RESPONSE"):
    return [{"systemMessage": {"text": {"parts": [text], "textType": kind}}}]


def test_every_material_carries_a_marker_no_real_data_holds():
    """A plausible fact could be answered from the model's own training and
    would prove nothing. The marker is what makes the check exact."""
    mats = _mats()

    assert [m.key for m in mats] == ["bq_column_description", "kc_overview_aspect",
                                     "kc_queries_aspect", "kc_glossary_term",
                                     "kc_join_link"]
    assert len({m.marker for m in mats}) == len(mats)
    for material in mats:
        assert TABLE in material.question
    # every written material names its marker in the fact it writes
    for material in mats[:-1]:
        assert material.marker in material.fact
    # the join material writes nothing: its marker is the partner's own name,
    # and the proof is that name in SQL the agent was never told to join
    assert mats[-1].marker == "customers"


def test_the_join_material_is_left_out_when_no_partner_is_named():
    """A check that can never fire reads as a channel that was tested and
    found closed."""
    assert [m.key for m in materials(TABLE)] == [
        "bq_column_description", "kc_overview_aspect", "kc_queries_aspect",
        "kc_glossary_term"]


def test_the_marker_is_found_where_it_appeared_strongest_first():
    messages = (_sql("SELECT * FROM t WHERE note = 'ZQX8802'")
                + _answer("thinking about ZQX8802", kind="THOUGHT")
                + _answer("The fiscal year starts on March 3 (ZQX8802)."))

    assert where_seen(messages, "ZQX8802") == ["sql", "answer", "thinking"]


def test_a_citation_counts_as_seen():
    messages = [{"systemMessage": {"citation": {"sources": [
        {"glossaryTerm": {"displayName": "ZQX9134"}}]}}}]

    assert where_seen(messages, "ZQX9134") == ["citation"]


def test_an_answer_without_the_marker_is_not_evidence():
    """The model can invent a plausible date. Only the marker proves the
    fact came from what was written."""
    assert where_seen(_answer("The fiscal year starts on January 1."), "ZQX8802") == []


def test_nothing_read_means_the_harness_delivered_arm_is_the_product_arm():
    observations = [Observation(m, []) for m in _mats()]

    text = verdict(observations)

    assert "harness-delivered context" in text
    assert "not of the catalog serving it" in text


def test_a_catalog_material_being_read_changes_which_arm_is_the_product_arm():
    """This is the outcome the whole design branches on."""
    mats = _mats()
    observations = [Observation(m, ["sql"] if m.key == "kc_overview_aspect" else [])
                    for m in mats]

    text = verdict(observations)

    assert "reads the catalog on its own" in text
    assert "kc_overview_aspect" in text
    assert "a bare request over the same tables" in text


def test_a_native_read_outside_the_catalog_moves_the_material_into_the_control():
    """The service reading BigQuery descriptions on its own is why the
    control arm carries them: otherwise the lift credits the estate's own
    documentation to the catalog."""
    mats = _mats()
    observations = [Observation(m, ["sql"] if m.key == "bq_column_description" else [])
                    for m in mats]

    text = verdict(observations)

    assert "bq_column_description" in text
    assert "control arm must carry that material too" in text


def test_probe_asks_each_question_once_and_keeps_a_failure_as_a_result():
    asked = []

    def ask(question):
        asked.append(question)
        if "fiscal year" in question:
            raise RuntimeError("429 quota exhausted")
        return _sql("SELECT 1 -- ZQX7413")

    observations = probe(_mats(), ask)

    assert len(asked) == 5
    assert observations[0].places == ["sql"]
    failed = next(o for o in observations if o.material.key == "kc_overview_aspect")
    assert failed.places == [] and "429 quota exhausted" in failed.note


def test_the_report_shows_every_material_including_the_ones_not_seen():
    """A material left out of the table reads as untested, which is the one
    thing the probe exists to prevent."""
    observations = probe(_mats(), lambda q: _sql("SELECT 1"))

    text = report(observations)

    for material in _mats():
        assert material.key in text
    assert text.count("nowhere") == 5
    assert "verdict:" in text


def test_the_plan_writes_nothing_and_names_every_write_and_question():
    text = plan(_mats(), TABLE)

    assert "writes (each carries a marker" in text
    assert "then asks, against a bare arm" in text
    for material in _mats():
        assert material.question in text
    assert "once after the gold gate has executed the answer key" in text


def test_the_plan_runs_from_the_command_line_and_costs_nothing(capsys):
    """The default is the plan: it writes nothing, asks nothing, and names
    every write so a person without the grant can do them by hand."""
    from context_lift.cli import main

    assert main(["sentinel", "--table", TABLE, "--join-partner", "p.eval.customers"]) == 0
    out = capsys.readouterr().out

    assert "nothing was written and nothing was asked" in out
    assert "kc_overview_aspect" in out
    assert "Cost: one chat call per question" in out


def test_a_live_probe_without_a_project_refuses_before_it_bills_anything(capsys):
    from context_lift.cli import main

    assert main(["sentinel", "--table", TABLE, "--run"]) == 2
    assert "needs --project" in capsys.readouterr().out


def test_a_live_probe_refuses_a_forbidden_project(monkeypatch):
    """The chat calls are billed; the same guard every paid path uses."""
    import pytest

    from context_lift.cli import main
    monkeypatch.setenv("CONTEXTLIFT_FORBIDDEN_PROJECTS", "acme-prod")

    with pytest.raises(SystemExit, match="acme-prod"):
        main(["sentinel", "--table", TABLE, "--run", "--project", "acme-prod-payments"])
