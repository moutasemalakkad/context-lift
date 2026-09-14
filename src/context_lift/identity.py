# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Who is calling, said the same way to every Google service.

This module answers two different questions that people keep confusing, and
they need two different mechanisms because only one of them crosses the
boundary between a customer and Google.

**"How much did running this framework cost ME?"** is the customer's
question, and the answer is BigQuery **job labels**. A label is a key-value
pair on a job. It shows up in the customer's own
`INFORMATION_SCHEMA.JOBS_BY_PROJECT.labels` and in their own Cloud Billing
export, so one `WHERE` clause separates this framework's spend from dbt's,
from Looker's, and from a person running queries by hand. It is visible only
inside that customer's projects.

**"Which projects run this framework at all?"** is the publisher's question,
and labels cannot answer it: nobody outside the customer can read them. The
answer is the **user agent**, a short string of text the client library puts
in the header of each request. It travels inside requests the customer is
already sending to Google, so Google's own request records carry it. Google
documents the field for exactly this use: `client_info` is described as the
thing to set "if you're developing your own library or partner tool", and the
Google Cloud Ready - BigQuery partner programme requires partners to set one
so usage is attributed correctly. dbt and the BigQuery Jupyter magics both
set one for the same reason.

Three things this is NOT, because a customer's security review will ask:

- It is not telemetry that phones home. No extra request, no new endpoint,
  nothing sent anywhere that was not already being sent. It is a name on a
  letter that was going to be posted anyway.
- It does not identify a company. It identifies the calling project, and it
  says nothing about who owns that project.
- It is not enforcement. Anyone who forks this can delete the string, and
  then their usage is not counted. That is fine: it is attribution.

One string, from the installed package version, so it cannot drift away from
the release that is running. Every client the framework opens gets it. A
framework that labels only its warehouse calls and not its catalog or agent
calls produces a count that is wrong in a way nobody can see.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _installed_version

# The PUBLISHED name: the string that shows up in Google's request records
# and on every job label. It is deliberately the name people will install,
# not the name of this repository, because attribution to a name nobody can
# find is worth nothing, and a tool that changes its reported name later
# splits its own history in two.
NAME = "context-lift"

# Which installed package to read a version from. Separate from NAME
# because the repository is still called `contextlift` and the rename to
# `context-lift` is a later step; until it lands, `pip` knows the old name
# and Google should already be told the new one. Both are tried, so this
# keeps working through the rename without a flag day.
DISTRIBUTIONS = ("context-lift", "contextlift")

# What a build that was never installed calls itself. A user agent reading
# "contextlift/unknown" is a working checkout, and that is worth telling apart
# from a release: usage counted from a developer's machine is not adoption.
UNKNOWN_VERSION = "unknown"


def version() -> str:
    """The installed version, or `unknown` from a source checkout.

    Tries the published name first, then the old distribution name, so the
    version keeps being reported correctly on both sides of the rename.
    """
    for distribution in DISTRIBUTIONS:
        try:
            return _installed_version(distribution)
        except PackageNotFoundError:
            continue
    return UNKNOWN_VERSION


# A caller token a person can set to add their own name, rather than this
# replacing one. An open-source tool that hardcodes its identity and offers
# no way to add to it is a nuisance to anyone wrapping it in their own SDK.
EXTRA_TOKEN = "CONTEXT_LIFT_USER_AGENT_EXTRA"


def user_agent() -> str:
    """`context-lift/1.2.3 context-lift-caller-agent/1.2.3`.

    Tokens are strictly `NAME/SEMVER`, and a NAME holds only lower-case
    letters, digits and hyphens. That is not a style choice: the parser that
    reads these on the other side extracts its dimensions from that shape,
    and a token shaped any other way is lumped into an unnamed bucket. So
    who started the run goes in the NAME half, where it has room to be a
    word, and the version half stays a real version.

    The second token is the point of the exercise. A count of projects
    running this tool does not say whether a person or a coding agent
    started the run, and those are two different adoption stories that call
    for different work.

    Anything in `CONTEXT_LIFT_USER_AGENT_EXTRA` is appended, so somebody
    wrapping this tool can add their own name instead of losing it.
    """
    import os
    tokens = [f"{NAME}/{version()}", f"{NAME}-caller-{caller()}/{version()}"]
    extra = (os.environ.get(EXTRA_TOKEN) or "").strip()
    if extra:
        tokens.append(extra)
    return " ".join(tokens)


def client_info():
    """For `google-cloud-*` libraries: bigquery, dataplex, the glossary
    service, geminidataanalytics. They all take `client_info=`.

    It has to be the **gapic_v1** ClientInfo, not the plain one, and the
    tests caught this before any call went out. There are two classes with
    the same name. `google.api_core.client_info.ClientInfo` has
    `to_user_agent()`, which is all a REST client like BigQuery needs. The
    gRPC clients -- Dataplex, the glossary service, Conversational Analytics
    -- call `to_grpc_metadata()`, which only
    `google.api_core.gapic_v1.client_info.ClientInfo` has, and the plain one
    raises AttributeError on the first request. The gapic class is a
    subclass of the plain one, so it serves both and there is no reason to
    choose per client.

    Returns None when `google-api-core` is not installed, so nothing here
    can stop a run: an attribution string is worth less than the run it
    would have prevented.
    """
    try:
        from google.api_core.gapic_v1.client_info import ClientInfo
    except ImportError:
        return None
    return ClientInfo(user_agent=user_agent())


def headers() -> dict[str, str]:
    """For `google-genai`, which is a different library with a different
    door: it takes `http_options=` carrying raw headers rather than a
    `ClientInfo`. Returned as a plain dict because that caller already
    builds its own `HttpOptions` for the request timeout, and two objects
    fighting over one field is how a timeout gets dropped.

    Both headers are set. `user-agent` is the general one; `x-goog-api-client`
    is the one Google's own libraries use to describe themselves, and the
    libraries set both.
    """
    agent = user_agent()
    return {"user-agent": agent, "x-goog-api-client": agent}


# Environment variables that mean "an AI agent is driving this, not a
# person". The first two were observed set on a machine running Claude Code
# on 2026-09-11; `AI_AGENT` is the generic one. The rest follow the same
# naming pattern and are NOT verified -- they cost nothing to check and a
# wrong guess only misses a caller, it never misreports one, because a
# missing marker falls through to the terminal check below.
AGENT_MARKERS = (
    "AI_AGENT",                # generic, observed
    "CLAUDECODE",              # Claude Code, observed
    "CLAUDE_CODE_ENTRYPOINT",  # Claude Code, observed
    "GEMINI_CLI",              # not verified
    "CURSOR_AGENT",            # not verified
    "AIDER_MODEL",             # not verified
)

# Any host that is none of the above can say so for itself, which is better
# than this module growing a list of every coding agent that ships.
CALLER_OVERRIDE = "CONTEXT_LIFT_CALLER"

CALLERS = ("agent", "ci", "terminal", "script")


def caller() -> str:
    """Who started this run: `agent`, `ci`, `terminal`, or `script`.

    This is the one number that answers "did making the framework readable
    by coding agents actually bring anybody in". Adoption driven by an agent
    that found the library and used it looks exactly like adoption driven by
    a person reading the README, unless the run says which it was.

    The order matters. An agent marker wins over the terminal check, because
    a host agent often runs commands with a terminal attached and would
    otherwise be counted as a person. `script` is the honest name for no
    terminal and no marker: a cron job, a Makefile, something piped.
    """
    import os
    declared = (os.environ.get(CALLER_OVERRIDE) or "").strip().lower()
    if declared in CALLERS:
        return declared
    if any(os.environ.get(name) for name in AGENT_MARKERS):
        return "agent"
    if os.environ.get("CI"):
        return "ci"
    try:
        import sys
        return "terminal" if sys.stdin.isatty() else "script"
    except Exception:  # noqa: BLE001 - a closed stdin is not an error here
        return "script"


# Which command is running. Process-wide on purpose: one CLI invocation is
# one command, and threading a stage name through every executor call site
# is plumbing that gets forgotten at one of them, leaving some jobs labelled
# and some not. `main` sets it once.
_stage = ""


def set_stage(stage: str) -> None:
    """Called once, by the CLI, with the subcommand name."""
    global _stage
    _stage = stage or ""


def stage() -> str:
    return _stage


def job_labels(stage_name: str | None = None) -> dict[str, str]:
    """BigQuery job labels: the customer-side half.

    The stage is which command is running (`mine`, `gate`, `score`), so a
    customer reading their own bill can see that mining cost them $10.84 and
    the round cost them $0.40, rather than one number for the whole
    framework.

    BigQuery's rules on labels, which are not ours to bend: lower case,
    digits, dashes and underscores only, 63 characters at most, and a key
    cannot be empty. A stage name that breaks them is dropped rather than
    failing the job, because a label is not worth losing a query over.
    """
    labels = {"app": NAME, "caller": caller()}
    name = _stage if stage_name is None else stage_name
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_"
                      for c in (name or "").lower())[:63]
    if cleaned:
        labels["stage"] = cleaned
    return labels
