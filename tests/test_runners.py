# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The Runner seam: the Arm hash, the request body, and the answer parser.

No network and no SDK. The parser takes plain dicts, which is the whole
reason it is a separate function: the field names it reads are the part
most likely to be wrong, and a test can pin them the day a live call says
what they are.
"""
import enum

import pytest

from context_lift.runners import Arm, Example, Prediction, Term
from context_lift.runners.ca_api import (THOUGHT_FORMS, build_inline_context,
                                     is_thought, parse_stream)
from context_lift.sentinel import where_seen


def _arm(**kwargs) -> Arm:
    base = dict(label="test", tables=["p.d.orders"])
    base.update(kwargs)
    return Arm(**base)


def test_the_arm_hash_covers_the_context_and_ignores_the_label():
    """Renaming an arm must not make it a different arm; adding a term must."""
    one = _arm(label="round 1", terms=[Term("revenue", "sale price of net lines")])
    same = _arm(label="a different name", terms=[Term("revenue", "sale price of net lines")])
    other = _arm(label="round 1", terms=[Term("revenue", "all sale price")])

    assert one.arm_id == same.arm_id
    assert one.arm_id != other.arm_id
    assert len(one.arm_id) == 12


def test_a_dict_built_in_another_order_hashes_the_same():
    one = _arm(field_descriptions={"p.d.orders": {"a": "one", "b": "two"}})
    other = _arm(field_descriptions={"p.d.orders": {"b": "two", "a": "one"}})

    assert one.arm_id == other.arm_id


def test_bare_means_table_references_and_nothing_else():
    assert _arm().is_bare()
    assert not _arm(table_descriptions={"p.d.orders": "orders"}).is_bare()
    assert not _arm(system_instruction="the fiscal year starts in March").is_bare()


def test_the_bare_request_carries_references_and_the_model_and_no_context():
    body = build_inline_context(_arm())

    assert body["datasourceReferences"]["bq"]["tableReferences"] == [
        {"projectId": "p", "datasetId": "d", "tableId": "orders"}]
    assert body["options"] == {"model": "LATEST_GA_MODEL"}
    assert "glossaryTerms" not in body
    assert "exampleQueries" not in body
    assert "systemInstruction" not in body


def test_the_export_arm_carries_every_kind_of_context():
    arm = _arm(
        table_descriptions={"p.d.orders": "one row per order"},
        field_descriptions={"p.d.orders": {"status": "Complete, Cancelled, Returned"}},
        terms=[Term("revenue", "excludes Cancelled and Returned")],
        examples=[Example("revenue by month", "SELECT 1")],
        system_instruction="the fiscal year starts on March 3",
    )

    body = build_inline_context(arm)
    reference = body["datasourceReferences"]["bq"]["tableReferences"][0]

    assert reference["schema"]["description"] == "one row per order"
    assert reference["schema"]["fields"] == [
        {"name": "status", "description": "Complete, Cancelled, Returned"}]
    assert body["glossaryTerms"] == [
        {"displayName": "revenue", "description": "excludes Cancelled and Returned"}]
    assert body["exampleQueries"] == [
        {"naturalLanguageQuestion": "revenue by month", "sqlQuery": "SELECT 1"}]
    assert body["systemInstruction"] == "the fiscal year starts on March 3"


def test_a_table_reference_must_name_a_project():
    """A two-part name would silently reach whichever project the request
    ran in, and the control arm's whole point is that it names its own."""
    with pytest.raises(ValueError, match="project.dataset.table"):
        build_inline_context(_arm(tables=["d.orders"]))


def test_the_parser_reads_the_sql_the_job_the_tables_and_the_citations():
    """The message shapes here are the ones a live call returned on 2026-09-09.

    An earlier version of this test read `schema.result.datasources`, which was
    a guess and which the service never sends.
    """
    messages = [
        {"systemMessage": {"text": {"parts": ["I will join orders to users"],
                                    "textType": "THOUGHT"}}},
        {"systemMessage": {"data": {"query": {"datasources": [
            {"bigqueryTableReference": {
                "projectId": "p", "datasetId": "d", "tableId": "orders"}}]}}}},
        {"systemMessage": {"data": {"generatedSql": "SELECT COUNT(*) FROM orders"}}},
        {"systemMessage": {"data": {"bigQueryJob": {"jobId": "job-1", "location": "US"}}}},
        {"systemMessage": {"citation": {"sources": [
            {"id": "glossary_revenue", "title": "Glossary: revenue",
             "glossaryTerm": {"displayName": "revenue"}},
            {"id": "ex_q0", "title": "Verified Query: revenue by month",
             "exampleQuery": {"naturalLanguageQuestion": "revenue by month"}}],
            "anchors": [{"textMessageAnchor": {
                "startOffsetBytes": 0, "endOffsetBytes": 7,
                "sourceIds": ["glossary_revenue"]}}]}}},
        {"systemMessage": {"text": {"parts": ["There are 12 orders."],
                                    "textType": "FINAL_RESPONSE"}}},
    ]

    prediction = parse_stream(messages, "q1", "abc123abc123")

    assert prediction.pred_sql == "SELECT COUNT(*) FROM orders"
    assert prediction.job_id == "job-1"
    assert prediction.tables_resolved == ["p.d.orders"]
    # Keyed by what the request sent, not by the id the service invents. Over
    # 186 answers one term came back as g_completed, g_comp and
    # g_completed_order, so an id is not a key anything can join on.
    assert prediction.citations == ["term:revenue", "example:revenue by month"]
    assert prediction.thought_chars == len("I will join orders to users")
    assert prediction.retries == 0
    assert prediction.error_class is None
    assert not prediction.is_lookup


class _TextTypeProxy(enum.IntEnum):
    """Stands in for `TextMessage.TextType` so this file needs no [ca] extra.

    proto-plus enums subclass `IntEnum`, so a member compares equal to its
    number and this proxy behaves the way the real one does. The values are
    the installed SDK's, read on 2026-09-12:
    TEXT_TYPE_UNSPECIFIED 0, FINAL_RESPONSE 1, THOUGHT 2, PROGRESS 3,
    FOLLOWUP_QUESTIONS 4. The names are quoted exactly so a grep for one
    of them finds this line; an earlier draft shortened two of them and
    read as a transcript of something it was not.
    """
    FINAL_RESPONSE = 1
    THOUGHT = 2


def test_thought_text_is_counted_when_the_enum_arrives_as_its_number():
    """The shape a live call actually has, which the name-only test missed.

    proto-plus `to_dict` defaults to `use_integers_for_enums=True` and
    `preserving_proto_field_name=True`, so a real message carries `text_type: 2`
    and never the string. The suite fed the string only, so it passed while a
    paid round recorded thought_chars = 0 on all 86 rows, 77 of them with SQL.
    """
    messages = [
        {"system_message": {"text": {"parts": ["I will join orders to users"],
                                     "text_type": 2}}},
        {"system_message": {"text": {"parts": ["There are 12 orders."],
                                     "text_type": 1}}},
        {"system_message": {"data": {"generated_sql": "SELECT 1"}}},
    ]

    prediction = parse_stream(messages, "q1", "abc123abc123")

    # The final response is text too, so counting every text part would look
    # like a fix while measuring the wrong thing.
    assert prediction.thought_chars == len("I will join orders to users")


def test_every_form_of_the_thought_text_type_is_accepted():
    """Name, number and enum member. A caller hands any of the three."""
    assert is_thought("THOUGHT")
    assert is_thought(2)
    assert is_thought(_TextTypeProxy.THOUGHT)

    assert not is_thought("FINAL_RESPONSE")
    assert not is_thought(1)
    assert not is_thought(_TextTypeProxy.FINAL_RESPONSE)
    assert not is_thought(None)


def test_the_sentinel_calls_a_numbered_thought_thinking():
    """The probe's second reader of the same enum, pinned beside the first.

    `sentinel._walk` shares `is_thought`, and both call sites are checked here
    so the predicate cannot be changed with only one of them still right. The
    string form is pinned in tests/test_sentinel.py.
    """
    numbered = [{"system_message": {"text": {"parts": ["thinking about ZQX8802"],
                                             "text_type": 2}}}]

    assert where_seen(numbered, "ZQX8802") == ["thinking"]


def test_a_matched_example_is_a_lookup_not_a_failure():
    """`generatedSql` and `matchedQuery` never arrive together (probe, 2026-09-09).

    So an answer through a stored example has no SQL of its own. Reporting that
    as `no_sql_in_answer` would count a working answer as a refusal.
    """
    messages = [
        {"systemMessage": {"data": {"matchedQuery": {"exampleQuery": {
            "naturalLanguageQuestion": "How many orders were completed in 2024?",
            "sqlQuery": "SELECT COUNT(*) FROM orders WHERE status = 'Complete'"}}}}},
        {"systemMessage": {"data": {"bigQueryJob": {"jobId": "job-9"}}}},
    ]

    prediction = parse_stream(messages, "q1", "abc123abc123")

    assert prediction.is_lookup
    assert prediction.pred_sql is None
    assert prediction.job_id == "job-9"
    assert prediction.error_class is None


def test_no_sql_and_no_match_is_still_reported_as_a_failure():
    """The refusal case must survive the lookup fix above."""
    messages = [{"systemMessage": {"text": {
        "parts": ["Could you clarify what you mean?"],
        "textType": "FINAL_RESPONSE"}}}]

    prediction = parse_stream(messages, "q1", "abc123abc123")

    assert prediction.error_class == "no_sql_in_answer"
    assert not prediction.is_lookup


def test_a_citation_without_a_display_name_falls_back_to_the_id():
    """A shape change loses precision rather than the whole signal."""
    messages = [{"systemMessage": {"citation": {"sources": [
        {"id": "g_rev", "glossaryTerm": {}}]}}}]

    prediction = parse_stream(messages, "q1", "abc123abc123")

    assert prediction.citations == ["term:g_rev"]


def test_the_service_id_is_not_used_when_the_name_we_sent_is_there():
    """Measured on 2026-09-10: the same term came back as g_completed, g_comp
    and g_completed_order across three answers, so joining on the id joins
    nothing."""
    messages = [{"systemMessage": {"citation": {"sources": [
        {"id": "g_comp", "glossaryTerm": {"displayName": "completed order"}}]}}}]

    prediction = parse_stream(messages, "q1", "abc123abc123")

    assert prediction.citations == ["term:completed order"]


def test_the_parser_reads_snake_case_too():
    """Only camelCase was observed on the wire on 2026-09-09, but the proto
    spells these in snake_case, so the SDK may hand back either."""
    messages = [{"system_message": {"data": {"generated_sql": "SELECT 1",
                                             "big_query_job": {"job_id": "job-2"}}}}]

    prediction = parse_stream(messages, "q1", "abc123abc123")

    assert prediction.pred_sql == "SELECT 1"
    assert prediction.job_id == "job-2"


def test_a_matched_example_is_a_lookup_not_a_written_answer():
    """The agent runs a stored example as written, so that answer says
    nothing about writing SQL and is counted apart."""
    messages = [
        {"systemMessage": {"data": {"generatedSql": "SELECT 1"}}},
        {"systemMessage": {"data": {"matchedQuery": {"exampleQuery": {
            "naturalLanguageQuestion": "revenue by month"}}}}},
    ]

    prediction = parse_stream(messages, "q1", "abc123abc123")

    assert prediction.is_lookup
    assert prediction.matched_example == "revenue by month"


def test_three_generated_statements_are_two_retries():
    """The API retries a failed query up to three times inside one request.
    An answer that only worked on the third try is fragile and says so."""
    messages = [{"systemMessage": {"data": {"generatedSql": sql}}}
                for sql in ("SELECT bad", "SELECT worse", "SELECT good")]

    prediction = parse_stream(messages, "q1", "abc123abc123")

    assert prediction.pred_sql == "SELECT good"
    assert prediction.retries == 2


def test_no_sql_is_an_error_class_not_a_wrong_answer():
    """A refusal and a wrong answer are different outcomes; the scorer needs
    to tell them apart."""
    assert parse_stream([], "q1", "abc").error_class == "no_sql_in_answer"

    refused = parse_stream(
        [{"systemMessage": {"error": {"text": "the question is ambiguous"}}}], "q1", "abc")
    assert refused.error_class == "the question is ambiguous"
    assert refused.pred_sql is None


def test_the_cost_sheet_says_it_cannot_price_a_data_token():
    """The pricing page bills 'data tokens' and never defines one, so a
    figure before a pilot would be invented."""
    from context_lift.runners.ca_api import CaApiRunner

    sheet = CaApiRunner(project="p").cost_sheet(n_questions=50, n_arms=2, k=3)

    assert sheet.calls == 300
    assert sheet.minutes_at_quota == 10
    assert sheet.usd_estimate is None
    assert "data tokens" in sheet.basis
    assert "unknown" in sheet.line()

    measured = CaApiRunner(project="p", usd_per_call=0.01).cost_sheet(50, 2, 3)
    assert measured.usd_estimate == pytest.approx(3.0)


def test_synonyms_and_tags_reach_the_request_on_the_table_and_on_each_field():
    """The Context carries `synonyms` and `tags` beside the descriptions, at
    both levels. This body shape is the one a live call accepted on
    2026-09-09; a wrong field name is refused with 400 INVALID_ARGUMENT, so an
    accepted body is proof the names are real."""
    arm = Arm(
        label="t",
        tables=["p.d.orders"],
        table_descriptions={"p.d.orders": "Orders."},
        table_synonyms={"p.d.orders": ["sales"]},
        table_tags={"p.d.orders": ["retail"]},
        field_descriptions={"p.d.orders": {"num_of_item": "Item count."}},
        field_synonyms={"p.d.orders": {"num_of_item": ["zylbrick"]}},
        field_tags={"p.d.orders": {"num_of_item": ["quantity"]}},
    )

    schema = build_inline_context(arm)["datasourceReferences"]["bq"][
        "tableReferences"][0]["schema"]

    assert schema == {
        "description": "Orders.",
        "synonyms": ["sales"],
        "tags": ["retail"],
        "fields": [{"name": "num_of_item", "description": "Item count.",
                    "synonyms": ["zylbrick"], "tags": ["quantity"]}],
    }


def test_a_column_with_only_synonyms_still_gets_its_entry():
    """A synonyms-only arm has to be expressible, or the class cannot be
    priced on its own with leave-one-class-out."""
    arm = Arm(label="t", tables=["p.d.orders"],
              field_synonyms={"p.d.orders": {"num_of_item": ["zylbrick"]}})

    schema = build_inline_context(arm)["datasourceReferences"]["bq"][
        "tableReferences"][0]["schema"]

    assert schema == {"fields": [{"name": "num_of_item",
                                  "synonyms": ["zylbrick"]}]}


def test_an_arm_carrying_only_synonyms_is_not_bare():
    """The bare arm is the sentinel's arm. Anything the harness delivers,
    including a synonym, makes it something else."""
    assert not Arm(label="t", tables=["p.d.orders"],
                   field_synonyms={"p.d.orders": {"c": ["x"]}}).is_bare()
    assert Arm(label="t", tables=["p.d.orders"]).is_bare()


def test_changing_only_a_synonym_changes_the_arm_id():
    """Two arms that differ in what the agent receives must not claim to be
    the same arm, or a lift gets attributed to the wrong context."""
    without = Arm(label="t", tables=["p.d.orders"])
    with_one = Arm(label="t", tables=["p.d.orders"],
                   field_synonyms={"p.d.orders": {"c": ["x"]}})

    assert without.arm_id != with_one.arm_id


def test_the_chat_call_is_bounded():
    """There was no deadline at all, so one hung stream blocked every
    question behind it -- on a 78-question round, 77 waiting on one.

    300 seconds and not 60, because the service is genuinely slow on a large
    arm and a short bound would throw away good answers. Measured on one
    estate with the same questions, the same model and K=1: a 27-table arm
    answered in about 28 seconds a question; an 85-table arm took 60 to 210
    seconds, one call 3m27s, with ZERO retries -- so that is the service
    thinking about a bigger prompt, not a network fault.
    """
    from context_lift.runners.ca_api import CHAT_TIMEOUT_SECONDS, CaApiRunner
    assert CHAT_TIMEOUT_SECONDS >= 210, (
        "the bound must sit above the slowest call actually measured, or it "
        "throws away answers the service would have given")
    runner = CaApiRunner(project="p")
    assert runner.chat_timeout == CHAT_TIMEOUT_SECONDS
    # and it can be turned off by somebody who means it
    assert CaApiRunner(project="p", chat_timeout=None).chat_timeout is None


def test_a_timeout_is_retried_and_counted():
    """`DeadlineExceeded` is transient, so a bounded call that expires is
    asked again rather than losing the question -- and the retry is counted,
    which is what makes "slow" and "being retried" different words on the
    progress line."""
    from context_lift.runners.ca_api import TRANSIENT, _is_transient

    class DeadlineExceeded(Exception):
        pass

    assert "DeadlineExceeded" in TRANSIENT
    assert _is_transient(DeadlineExceeded())


def test_the_thought_number_still_matches_the_installed_sdk():
    """`THOUGHT_FORMS` hand-copies the integer 2. Pin it to the real enum.

    Without this, every test above passes while `thought_chars` returns to 0,
    because the source and the test proxy both restate the same assumption
    rather than checking it. If the API renumbers its enum, this is the only
    thing that notices.

    Skipped when the [ca] extra is absent, so `make test` stays cloud-free
    (the same shape as tests/test_catalog.py and tests/test_duckdb.py). It
    makes no call and reads no credential: an enum is a constant in the
    installed package.
    """
    gda = pytest.importorskip("google.cloud.geminidataanalytics")
    assert int(gda.TextMessage.TextType.THOUGHT) in THOUGHT_FORMS
    assert gda.TextMessage.TextType.THOUGHT.name in THOUGHT_FORMS
    # And the one it must NOT match, so the pin cannot pass by accepting
    # everything.
    assert int(gda.TextMessage.TextType.FINAL_RESPONSE) not in THOUGHT_FORMS
