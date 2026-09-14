# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Does the agent read the catalog? The one question the design rests on.

No document says whether an agent built through the Conversational Analytics
API reads a table's Knowledge Catalog overview or queries aspect. Its Context
object has no field pointing at the catalog, a Google blog says context comes
from catalog "context bundles" and never defines one, and the console path
imports catalog glossary terms while the API path says its glossary is "only
user authored". Until that is settled, a lift measured after writing to the
catalog cannot be called the catalog's.

So: write a fact no real data carries into one place, ask a question that
cannot be answered without it, and look at what comes back. One material per
place. A marker token makes the check exact, and where the marker appears
says how the fact arrived: in the SQL, in a citation, in the answer, or in
the model's thinking.

The probe is the only place that reads an answer's text. A normal run keeps
the length of the thinking and never its content, because it can quote
customer data. Here the data is invented, in a project the owner chose, and
what the text says is the whole result.

What the outcome decides, and nothing else: which arm the page calls the
product arm. It does not decide whether the framework works; the harness
delivers the context either way.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# One definition of THOUGHT's two spellings, kept with the parser that found
# the bug (runners/ca_api.py). Importing it costs nothing: that module reads
# the SDK only inside `CaApiRunner.answer`.
from .runners.ca_api import is_thought

# Where a marker turned up. Ordered strongest first: SQL means the fact
# changed the query, which is the only place that changes a score.
PLACES = ("sql", "citation", "answer", "thinking")


@dataclass(frozen=True)
class Material:
    """One kind of catalog content, and the question that needs it."""
    key: str
    where: str
    stage: str
    marker: str
    fact: str
    question: str

    def written_value(self) -> str:
        """The text to write, which carries the marker."""
        return self.fact


def materials(table: str, column: str = "amount",
              join_partner: str | None = None) -> list[Material]:
    """The materials to probe, aimed at one table in the evaluation project.

    Markers are invented tokens. A real value would risk a true answer that
    proves nothing: if the fact were plausible, the model could produce it
    from its own training rather than from the catalog.

    The join channel is different and needs `join_partner`. Nothing is
    written for it: the catalog mines join links from query history on its
    own. So its marker is the partner table's own name, and the proof is
    that name appearing in SQL the agent wrote without being told the join.
    Without a partner to name, the material is left out rather than shipped
    as a check that can never fire.
    """
    found = [
        Material(
            key="bq_column_description",
            where=f"the BigQuery column description on {table}.{column}",
            stage="a 2025 blog and a codelab say the API reads these; the API "
                  "reference does not (undocumented)",
            marker="ZQX7413",
            fact=f"ZQX7413: {column} is recorded in tenths of a cent.",
            question=f"What unit is the {column} column of {table} recorded in?",
        ),
        Material(
            key="kc_overview_aspect",
            where="the overview aspect of that table's Knowledge Catalog entry",
            stage="undocumented for the API path; this is the blocking question",
            marker="ZQX8802",
            fact="ZQX8802: the fiscal year of this table starts on March 3.",
            question=f"On what date does the fiscal year start for {table}?",
        ),
        Material(
            key="kc_queries_aspect",
            where="a USER row in that entry's queries aspect",
            stage="undocumented for the API path; the stream carries 'derived and "
                  "authored' example queries and never says what derived means",
            marker="ZQX5561",
            fact="ZQX5561 rows only",
            question=f"Using the stored example, how many ZQX5561 rows are in {table}?",
        ),
        Material(
            key="kc_glossary_term",
            where="a glossary term linked to that table with a definition link",
            stage="the API's glossary field is 'currently, only user authored'; a "
                  "linked term appeared in no served bundle across 13 reads",
            marker="ZQX9134",
            fact="ZQX9134 means a row whose status is neither Cancelled nor Returned.",
            question=f"How many ZQX9134 rows are in {table}?",
        ),
    ]
    if join_partner:
        found.append(Material(
            key="kc_join_link",
            where="a schema-join link the catalog mined from query history",
            stage="Preview; the catalog mines these with no scan, so the harness's "
                  "own gold executions feed them (design doc 4)",
            marker=join_partner.split(".")[-1],
            fact="(nothing is written: the catalog discovers this on its own)",
            question=f"Count the rows of {table} joined to the table it is most "
                     f"often queried with. Do not guess a join key.",
        ))
    return found


def _walk(message: dict, marker: str) -> list[str]:
    """Where the marker appears in one streamed message."""
    places = []
    system = message.get("systemMessage") or message.get("system_message") or {}
    if not isinstance(system, dict):
        return places

    data = system.get("data") or {}
    if isinstance(data, dict):
        sql = data.get("generatedSql") or data.get("generated_sql") or ""
        if marker in str(sql):
            places.append("sql")

    citation = system.get("citation")
    if isinstance(citation, dict) and marker in str(citation):
        places.append("citation")

    text = system.get("text")
    if isinstance(text, dict):
        body = text.get("parts") or text.get("text") or ""
        if isinstance(body, list):
            body = "".join(str(part) for part in body)
        if marker in str(body):
            # A live stream sends the enum's number, not "THOUGHT", so the
            # string compare this replaced could never print "thinking"
            # against a real call; see THOUGHT_FORMS in runners/ca_api.py.
            kind = text.get("textType") or text.get("text_type")
            places.append("thinking" if is_thought(kind) else "answer")
    return places


def where_seen(messages: list[dict], marker: str) -> list[str]:
    """Every place a marker appears in one answer, strongest first.

    Exactness is the point. A question about a fiscal year could be answered
    with a plausible date the model invented; only the marker proves the
    answer came from the fact that was written.
    """
    found = set()
    for message in messages:
        if isinstance(message, dict):
            found.update(_walk(message, marker))
    return [place for place in PLACES if place in found]


@dataclass
class Observation:
    """One material, and where its marker turned up."""
    material: Material
    places: list[str] = field(default_factory=list)
    written: bool = True
    note: str = ""

    @property
    def read_by_the_agent(self) -> bool:
        return bool(self.places)


def probe(mats: list[Material], ask) -> list[Observation]:
    """Ask each material's question and record where its marker appeared.

    `ask(question) -> list[dict]` is the answer's messages. Injected, so the
    whole decision is testable with no call.
    """
    observations = []
    for material in mats:
        try:
            messages = ask(material.question)
        except Exception as error:                  # a refusal is a result
            observations.append(Observation(material, [], note=f"the call failed: {error}"))
            continue
        observations.append(Observation(material, where_seen(messages, material.marker)))
    return observations


def verdict(observations: list[Observation]) -> str:
    """What the result decides: which arm the page calls the product arm."""
    catalog_read = [o for o in observations
                    if o.read_by_the_agent and o.material.key.startswith("kc_")]
    native = [o for o in observations if o.read_by_the_agent]
    if catalog_read:
        keys = ", ".join(o.material.key for o in catalog_read)
        return (f"the agent reads the catalog on its own ({keys}). The served-export "
                f"arm becomes the product arm, and the control needs a twin whose "
                f"catalog entry is never written; a bare request over the same tables "
                f"is not a control.")
    if native:
        keys = ", ".join(o.material.key for o in native)
        return (f"the agent reads {keys} on its own, but nothing from the catalog. "
                f"The control arm must carry that material too, so the lift measures "
                f"the authored context above it and not the material itself.")
    return ("nothing written outside the request reached the agent. The arm the "
            "harness delivers is the product arm, and the page says the measurement "
            "is of harness-delivered context, not of the catalog serving it.")


def report(observations: list[Observation]) -> str:
    """The table the probe prints, and the sentence it decides."""
    width = max(len(o.material.key) for o in observations) if observations else 20
    lines = ["sentinel: what the bare arm reads without being asked", ""]
    lines.append(f"  {'material':<{width}}  {'seen in':<22}  where it was written")
    for observation in observations:
        seen = ", ".join(observation.places) if observation.places else "nowhere"
        lines.append(f"  {observation.material.key:<{width}}  {seen:<22}  "
                     f"{observation.material.where}")
        if observation.note:
            lines.append(f"  {'':<{width}}  {observation.note}")
    lines.append("")
    lines.append(f"verdict: {verdict(observations)}")
    lines.append("")
    lines.append("A marker seen only in the thinking or the answer, and not in the SQL,")
    lines.append("means the fact reached the model but changed no query. That still")
    lines.append("counts as read: the next question it changes could be a scored one.")
    return "\n".join(lines)


def plan(mats: list[Material], table: str) -> str:
    """What a run would write and ask, printed without writing anything.

    The probe writes into a customer's catalog, so it says what it will do
    first. Every line is something a person can do by hand if the harness
    lacks the grant.
    """
    lines = [f"sentinel plan for {table}", "",
             "writes (each carries a marker no real data contains):"]
    for material in mats:
        if material.key == "kc_join_link":
            lines.append(f"  - {material.key}: nothing written; {material.fact}")
            continue
        lines.append(f"  - {material.key}: {material.written_value()!r}")
        lines.append(f"      into {material.where}")
    lines += ["", "then asks, against a bare arm over that table only:"]
    for material in mats:
        lines.append(f"  - {material.question}")
    lines += ["", "and reports where each marker appeared. Run it twice: once on day",
              "zero, and once after the gold gate has executed the answer key, because",
              "the catalog mines join links from query history with no scan.",
              "", "Cost: one chat call per question. Nothing else is billed."]
    return "\n".join(lines)
