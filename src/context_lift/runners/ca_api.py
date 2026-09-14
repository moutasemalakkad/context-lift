# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The Conversational Analytics API as a Runner: the agent under test.

Two halves, split so the half that matters is testable with no network.
`build_inline_context` turns an Arm into the request body, and `parse_stream`
turns the answer's messages into a Prediction. Both take and return plain
dicts. Only `CaApiRunner.answer` touches the SDK, which lives behind the
optional [ca] extra.

The request uses inline context (every field in the request, no saved agent),
so a run is reproducible from its run directory alone and two arms cannot
drift apart between calls. The alternative, one agent holding a staging and a
published context, needs a mutable cloud resource with no version string.

Field names were settled by the phase-0 probe on 2026-09-09, against the live
`projects.locations:chat` endpoint. Saved request bodies and answers back
every path read below. The parser still reads camelCase and snake_case,
because the REST reference and the proto disagree on case and only camelCase
was observed.

Three things the probe corrected. Resolved tables live at
`data.query.datasources[].bigqueryTableReference`, not under `schema.result`.
`citation` sits on the message, beside `data`, not inside it. And
`generatedSql` and `matchedQuery` never appear together: an answer is either
generated or a stored example run as written.
"""
from __future__ import annotations

import re
import time

from .. import identity

from . import Arm, CostSheet, Prediction

# The published quota: 30 chat requests per minute per project. A run's
# wall-clock floor comes from this, not from the model's speed.
CHAT_REQUESTS_PER_MINUTE = 30

# https://cloud.google.com/products/data-agents/pricing, read 2026-09-08.
# "Data token" is not defined on that page, so a cost is an estimate until a
# pilot measures one; see cost_sheet.
USD_PER_MILLION_INPUT_DATA_TOKENS = 3.0
USD_PER_MILLION_OUTPUT_DATA_TOKENS = 20.0
PRICES_AS_OF = "2026-09-08"


# `TextMessage.TextType.THOUGHT` in both the forms a reader can be handed.
# proto-plus `to_dict` runs with `use_integers_for_enums=True` at its
# defaults (proto-plus 1.28.4, read off the installed signature 2026-09-12),
# so a live stream carries the NUMBER 2 and only a JSON dump or a
# hand-written dict carries the NAME. Checking the name alone recorded
# thought_chars = 0 on all 86 rows of the paid round, including the 77 that
# produced SQL, and THOUGHT is the only per-call evidence that the service
# does its own context lookup.
THOUGHT_FORMS = ("THOUGHT", 2)


def is_thought(kind) -> bool:
    """True when a streamed `textType` says THOUGHT, whatever form it is in.

    A real enum member passes as well: proto-plus enums subclass `IntEnum`,
    so `TextType.THOUGHT == 2`. Anything else, including None from a message
    with no `textType` at all, is not thought text.
    """
    return kind in THOUGHT_FORMS


def _first(mapping: dict, *names):
    """The first of several spellings present in a dict, or None.

    The REST reference and the proto disagree on case, and nothing has been
    called yet, so both are tried rather than one being guessed.
    """
    for name in names:
        if isinstance(mapping, dict) and name in mapping:
            return mapping[name]
    return None


def build_inline_context(arm: Arm) -> dict:
    """The request body for one arm, as `inlineContext`.

    A bare arm carries table references and nothing else. Everything above
    that is authored context the harness delivers: table and column
    descriptions the estate already had, their synonyms and tags, glossary
    terms, worked examples, join relationships and the rule tiers as one
    system instruction.

    The API rejects a field it does not know with 400 INVALID_ARGUMENT and
    names it, checked on 2026-09-09 with a deliberately wrong field name. So
    a body that is accepted uses only real fields, and a lever that changes
    nothing changed nothing on its own merits rather than by being dropped.

    Not built yet, both real and both documented: `example_queries[].parameters`
    and `user_functions.bqRoutines`. Neither has a source in the spec repo, so
    adding them now would be a field nothing fills.
    """
    references = []
    for table in arm.tables:
        parts = table.split(".")
        if len(parts) != 3:
            raise ValueError(
                f"a table reference needs project.dataset.table, got {table!r}")
        project, dataset, name = parts
        reference = {"projectId": project, "datasetId": dataset, "tableId": name}
        schema: dict = {}
        if table in arm.table_descriptions:
            schema["description"] = arm.table_descriptions[table]
        if arm.table_synonyms.get(table):
            schema["synonyms"] = list(arm.table_synonyms[table])
        if arm.table_tags.get(table):
            schema["tags"] = list(arm.table_tags[table])

        # One entry per column that has anything to say. A column named in
        # only one of the three maps still gets its entry, so a synonyms-only
        # arm is expressible and can be priced on its own.
        descriptions = arm.field_descriptions.get(table) or {}
        synonyms = arm.field_synonyms.get(table) or {}
        tags = arm.field_tags.get(table) or {}
        named = sorted(set(descriptions) | set(synonyms) | set(tags))
        fields = []
        for column in named:
            entry: dict = {"name": column}
            if descriptions.get(column):
                entry["description"] = descriptions[column]
            if synonyms.get(column):
                entry["synonyms"] = list(synonyms[column])
            if tags.get(column):
                entry["tags"] = list(tags[column])
            fields.append(entry)
        if fields:
            schema["fields"] = fields
        if schema:
            reference["schema"] = schema
        references.append(reference)

    context: dict = {
        "datasourceReferences": {"bq": {"tableReferences": references}},
        "options": {"model": arm.model_alias},
    }
    if arm.system_instruction:
        context["systemInstruction"] = arm.system_instruction
    if arm.terms:
        context["glossaryTerms"] = [
            {"displayName": term.name, "description": term.definition}
            for term in arm.terms]
    if arm.examples:
        context["exampleQueries"] = [
            {"naturalLanguageQuestion": example.question, "sqlQuery": example.sql}
            for example in arm.examples]
    if arm.relationships:
        context["schemaRelationships"] = list(arm.relationships)
    return context


# Retried: the service's own faults and the quota. Not retried: anything about
# permissions, a bad argument or a missing resource, none of which a wait fixes.
TRANSIENT = ("InternalServerError", "ServiceUnavailable", "DeadlineExceeded",
             "TooManyRequests", "ResourceExhausted", "Aborted", "Unknown")
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 5.0

# Seconds one chat may take before it is abandoned. There was no bound at
# all, so a single hung stream could block every question behind it, and on
# a 78-question round that is 77 questions waiting on one.
#
# 300 and not 60, because the API is genuinely slow on a large arm and a
# short bound would throw away good answers. Measured on one estate, same
# questions, same model, K=1: with a 27-table arm a question took about 28
# seconds; with an 85-table arm the same questions took 60 to 210 seconds,
# one of them 3m27s, with zero retries -- so the slowness is the service
# thinking about a bigger prompt, not a network fault. 300 leaves headroom
# above the worst measured call and still bounds a stream that never ends.
CHAT_TIMEOUT_SECONDS = 300.0


def _is_transient(problem: BaseException) -> bool:
    """True when waiting and asking again is a reasonable thing to do."""
    return type(problem).__name__ in TRANSIENT


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake_case(value):
    """Every key of a nested structure, from camelCase to snake_case.

    Two spellings, one body. The REST endpoint takes camelCase, which is what
    `build_inline_context` writes and what the saved probe bodies of 2026-09-09
    carry, so that shape stays the one the tests pin and a person reads. The
    Python SDK builds protobuf messages and refuses camelCase outright:
    `ValueError: Unknown field for Message: userMessage`, seen on the first
    live round.

    Keys only. `LATEST_GA_MODEL` is a value and stays exactly as written, and
    so does every table name, column name and SQL string.
    """
    if isinstance(value, dict):
        return {_CAMEL_BOUNDARY.sub("_", key).lower(): to_snake_case(inner)
                for key, inner in value.items()}
    if isinstance(value, list):
        return [to_snake_case(item) for item in value]
    return value


def _table_name(datasource: dict) -> str | None:
    reference = _first(datasource, "bigqueryTableReference", "bigquery_table_reference",
                       "bqTableReference", "tableReference")
    if not isinstance(reference, dict):
        return None
    project = _first(reference, "projectId", "project_id")
    dataset = _first(reference, "datasetId", "dataset_id")
    table = _first(reference, "tableId", "table_id")
    if not table:
        return None
    return ".".join(part for part in (project, dataset, table) if part)


def _citation_names(citation: dict) -> list[str]:
    """The cited items of one answer, keyed by what WE sent.

    The service gives every source an id of its own, and those ids are not
    stable. Over 186 answers on 2026-09-10 one term came back as `g_completed`,
    `g_comp` and `g_completed_order`, and another as `g_deliv` and
    `glossary_delivery_time`. An earlier version of this preferred the id and
    said it was steadier than the text; that was wrong, and a worklist built on
    it joined nothing at all.

    So the name is the `displayName` of a glossary term, or the natural
    language question of an example query, both of which the request carried
    and the reply echoed back. The id is kept only when neither is there.

    The observed shape also carries `anchors[].textMessageAnchor` with byte
    offsets into the answer text. Those are not read: which item was cited is
    what the worklist ranks, not where it was cited.
    """
    names = []
    for source in _first(citation, "sources") or []:
        if not isinstance(source, dict):
            continue
        source_id = _first(source, "id")
        example = _first(source, "exampleQuery", "example_query")
        term = _first(source, "glossaryTerm", "glossary_term")
        uri = _first(source, "uri")
        if isinstance(example, dict):
            names.append("example:" + str(
                _first(example, "naturalLanguageQuestion",
                       "natural_language_question") or source_id or ""))
        elif isinstance(term, dict):
            names.append("term:" + str(
                _first(term, "displayName", "display_name") or source_id or ""))
        elif uri:
            names.append("uri:" + str(uri))
    return names


def parse_stream(messages: list[dict], question_id: str, arm_id: str) -> Prediction:
    """Turn the messages of one answer into a Prediction.

    The last generated SQL wins: the API retries a failed query up to three
    times in one request, and the retry count is kept, because an answer that
    only worked on the third try is fragile.

    Thought text is measured, never stored: it can quote customer data, and
    the length is enough to say whether the agent reasoned at all.
    """
    prediction = Prediction(question_id=question_id, arm_id=arm_id)
    sql_seen = 0
    for message in messages:
        system = _first(message, "systemMessage", "system_message") or {}
        if not isinstance(system, dict):
            continue

        data = _first(system, "data") or {}
        if isinstance(data, dict):
            sql = _first(data, "generatedSql", "generated_sql")
            if sql:
                sql_seen += 1
                prediction.pred_sql = sql
            job = _first(data, "bigQueryJob", "big_query_job")
            if isinstance(job, dict):
                prediction.job_id = _first(job, "jobId", "job_id")
            matched = _first(data, "matchedQuery", "matched_query")
            if isinstance(matched, dict):
                example = _first(matched, "exampleQuery", "example_query") or {}
                prediction.matched_example = str(
                    _first(example, "naturalLanguageQuestion",
                           "natural_language_question") or "matched")

            # The tables the agent actually resolved. Verified 2026-09-09:
            # they arrive as `data.query.datasources`. An earlier draft read
            # `schema.result.datasources`, which never appears.
            query = _first(data, "query") or {}
            if isinstance(query, dict):
                for datasource in (_first(query, "datasources") or []):
                    name = (_table_name(datasource)
                            if isinstance(datasource, dict) else None)
                    if name and name not in prediction.tables_resolved:
                        prediction.tables_resolved.append(name)

        citation = _first(system, "citation")
        if isinstance(citation, dict):
            for name in _citation_names(citation):
                if name not in prediction.citations:
                    prediction.citations.append(name)

        text = _first(system, "text")
        if isinstance(text, dict):
            kind = _first(text, "textType", "text_type")
            body = _first(text, "parts") or _first(text, "text") or ""
            if isinstance(body, list):
                body = "".join(str(part) for part in body)
            if is_thought(kind):
                prediction.thought_chars += len(body)

        error = _first(system, "error")
        if isinstance(error, dict):
            prediction.error_class = str(_first(error, "text") or "error")

    prediction.retries = max(0, sql_seen - 1)
    # A matched example carries no `generatedSql`, so the absence of SQL is
    # normal there and must not be reported as a failure. Scoring reads the
    # rows by job id either way; `is_lookup` keeps the two apart.
    if (prediction.pred_sql is None and prediction.error_class is None
            and not prediction.is_lookup):
        prediction.error_class = "no_sql_in_answer"
    return prediction


class CaApiRunner:
    """Answers through `projects.locations:chat` with inline context.

    Needs the [ca] extra (`google-cloud-geminidataanalytics`). The SDK is
    imported on the first call, so importing this module costs nothing and
    the fast test suite never needs the package.
    """

    name = "ca_api"

    def __init__(self, project: str, location: str = "global",
                 max_billed_bytes: int | None = None,
                 usd_per_call: float | None = None,
                 chat_timeout: float | None = CHAT_TIMEOUT_SECONDS):
        self.project = project
        self.location = location
        self.max_billed_bytes = max_billed_bytes
        self.usd_per_call = usd_per_call
        # None means no bound, for anybody who wants the old behaviour and
        # knows that one hung stream then blocks every question behind it.
        self.chat_timeout = chat_timeout
        # How many calls had to be asked twice. Printed after a round, because
        # a run that needed thirty retries is telling you something about the
        # service that a run of the same length without them is not.
        self.retries = 0
        self._client = None

    def cost_sheet(self, n_questions: int, n_arms: int, k: int) -> CostSheet:
        calls = n_questions * n_arms * k
        minutes = calls / CHAT_REQUESTS_PER_MINUTE
        if self.usd_per_call is None:
            return CostSheet(
                calls, minutes, None,
                "no price yet: the pricing page bills 'data tokens' and does not "
                "define one. Run a 20-question pilot and read the aggregate token "
                "count in Cloud Monitoring, then pass --usd-per-call")
        return CostSheet(calls, minutes, calls * self.usd_per_call,
                         f"measured pilot rate, ${self.usd_per_call:.4f} per call")

    def _chat_client(self):
        if self._client is None:
            try:
                from google.cloud import geminidataanalytics
            except ImportError as exc:      # the extra is not installed
                raise RuntimeError(
                    "the ca_api runner needs the [ca] extra: "
                    "uv pip install -e '.[ca]'") from exc
            self._client = geminidataanalytics.DataChatServiceClient(
                client_info=identity.client_info())
        return self._client

    def _request(self, question: str, arm: Arm) -> dict:
        context = build_inline_context(arm)
        if self.max_billed_bytes is not None:
            context["options"].setdefault("datasource", {})
            context["options"]["datasource"]["bigQueryMaxBilledBytes"] = self.max_billed_bytes
        return to_snake_case({
            "parent": f"projects/{self.project}/locations/{self.location}",
            "messages": [{"userMessage": {"text": question}}],
            "inlineContext": context,
        })

    def ask(self, question: str, arm: Arm) -> list[dict]:
        """One chat call, as the messages it streamed back.

        Separate from `answer` because the sentinel probe needs the whole
        answer, not the parsed fields: it looks for a marker anywhere in it,
        including the model's thinking, which a Prediction keeps only the
        length of.

        Retried on a transient failure. A round is dozens of serial calls over
        several minutes, and the first live round died nine minutes in on a
        single `500 Internal error encountered.` from the service. One
        transient error must not throw away a run, so the transient classes
        are retried with a widening wait and everything else is raised at once:
        a 403 will not fix itself and retrying it just spends time.
        """
        client = self._chat_client()
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return [_as_dict(reply) for reply in
                        client.chat(request=self._request(question, arm),
                                    timeout=self.chat_timeout)]
            except Exception as problem:  # noqa: BLE001 - classified below
                if not _is_transient(problem) or attempt == RETRY_ATTEMPTS - 1:
                    raise
                self.retries += 1
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise AssertionError("unreachable: the loop either returns or raises")

    def answer(self, question_id: str, question: str, arm: Arm) -> Prediction:
        return parse_stream(self.ask(question, arm), question_id, arm.arm_id)


def _as_dict(message) -> dict:
    """One streamed message as a plain dict.

    The SDK returns protobuf objects; `to_dict` is the documented way to read
    one. A message that is already a dict passes through, which is how the
    fake runner and the tests feed the same parser.
    """
    if isinstance(message, dict):
        return message
    to_dict = getattr(type(message), "to_dict", None)
    if to_dict is not None:
        return to_dict(message)
    return {}
