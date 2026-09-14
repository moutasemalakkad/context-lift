# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""LLM backends for the judge and the generator — each is a Callable[[str], str].

The judge and generator depend only on "give a prompt, get text back", so a
backend is just a function. That dependency injection keeps them testable
with no network: echo_backend returns a fixed string for tests;
gemini_backend calls Gemini and lives behind the optional [gemini] extra.

Every backend carries two attributes on the function object, so a caller
can know what a run cost without a second seam: `usage`, a dict of
calls / prompt_tokens / output_tokens / thought_tokens that grows with
each call, and `count_tokens(text) -> int` for an estimate before any
paid call. Setting `budget_usd` makes a backend stop answering (it returns
"" and sets `usage["stopped_at_budget"]`) once its spend crosses the line.
"""
from __future__ import annotations

import os
import sys
import threading
import time

from . import identity
from typing import Callable

# USD per million tokens on Vertex AI, prompts up to 200K tokens.
# Source: https://cloud.google.com/vertex-ai/generative-ai/pricing
# Thinking tokens bill at the output rate, so estimate_cost_usd adds them
# to output. Override per run with generate --input-price/--output-price.
# retries on a quota or overload answer before the error is raised
RETRY_WAITS_SECONDS = 5

PRICES_USD_PER_MILLION = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
}
PRICES_AS_OF = "2026-09-01"


def price_table(model: str, input_price: float | None = None,
                output_price: float | None = None) -> dict | None:
    """The prices a cost figure was computed with, or None when unknown.

    Returned alongside every estimate so a manifest says which numbers it
    used; a price change later does not silently change an old cost.
    """
    known = PRICES_USD_PER_MILLION.get(model, {})
    input_rate = input_price if input_price is not None else known.get("input")
    output_rate = output_price if output_price is not None else known.get("output")
    if input_rate is None or output_rate is None:
        return None
    return {"model": model, "input_per_million_usd": input_rate,
            "output_per_million_usd": output_rate, "as_of": PRICES_AS_OF}


def estimate_cost_usd(prices: dict | None, prompt_tokens: int,
                      output_tokens: int, thought_tokens: int = 0) -> float | None:
    if prices is None:
        return None
    return round(
        prompt_tokens * prices["input_per_million_usd"] / 1e6
        + (output_tokens + thought_tokens) * prices["output_per_million_usd"] / 1e6,
        4,
    )


def _new_usage() -> dict:
    # `retries` and `retry_seconds` are here because a run without them
    # cannot say why it is slow. One stage went quiet for six minutes with
    # an idle CPU, no warehouse query, and 51 open sockets: every symptom of
    # a call being retried, and no way to confirm it, because the retry
    # ladder below slept without saying so. A slow call and a retried call
    # have to be different words on the screen.
    # `cached_tokens` is how many prompt tokens the service served from its
    # implicit cache. It is a SUBSET of prompt_tokens, not an addition, and it
    # is recorded rather than discounted: the reference says only "we
    # automatically pass on cost savings if your request hits caches" and
    # gives no multiplier, so billing them at a guessed rate would let this
    # tool under-report a real invoice. Recording it makes the saving
    # measurable instead of assumed.
    #
    # It matters for what this project measures. Implicit caching needs 2,048
    # prompt tokens on Gemini 2.5 Flash, so a big shared context arm is above
    # the floor and a small per-question arm is below it -- the cache
    # structurally favours the arm that sends more.
    return {"calls": 0, "prompt_tokens": 0, "output_tokens": 0,
            "thought_tokens": 0, "cached_tokens": 0, "stopped_at_budget": False,
            "retries": 0, "retry_seconds": 0.0, "last_retry_code": 0}


def forbidden_project_prefixes() -> list[str]:
    """The project-id prefixes CONTEXTLIFT_FORBIDDEN_PROJECTS names, comma-separated.

    Configuration, not source: a guard that hardcodes the name of the
    project it protects publishes that name to everyone who reads the repo.
    An unset or empty variable forbids nothing.
    """
    return [p.strip() for p in os.environ.get("CONTEXTLIFT_FORBIDDEN_PROJECTS", "").split(",")
            if p.strip()]


def refuse_forbidden_project(project: str | None, spends: str) -> str | None:
    """Stop before anything is billed to a project on the forbidden list.

    Every place that picks a billing project calls this first: the bq://
    executor, the Gemini backend and the chat session. `spends` names what
    would have been billed, so the refusal says what it stopped. The
    project is returned unchanged when it passes, so a caller can write
    `project = refuse_forbidden_project(project, ...)`.
    """
    if project:
        for prefix in forbidden_project_prefixes():
            if project.startswith(prefix):
                raise SystemExit(
                    f"refusing: project {project!r} matches CONTEXTLIFT_FORBIDDEN_PROJECTS "
                    f"({prefix!r}); {spends} would be billed there. Use a project you own.")
    return project


def echo_backend(response: str) -> Callable[[str], str]:
    """Return a backend that ignores the prompt and always returns `response`.

    The seam that keeps the judge and generator testable without a network:
    a test injects a canned reply and asserts how it is parsed. Token counts
    are a characters/4 approximation, so cost paths run offline too.
    """
    usage = _new_usage()
    lock = threading.Lock()

    def _call(prompt: str) -> str:
        with lock:
            usage["calls"] += 1
            usage["prompt_tokens"] += _call.count_tokens(prompt)
            usage["output_tokens"] += _call.count_tokens(response)
        return response

    _call.usage = usage
    _call.count_tokens = lambda text: max(1, len(text) // 4)
    _call.budget_usd = None
    _call.prices = None
    return _call


# Gemini 2.5 spends reasoning tokens before the answer, so a judging call
# needs a non-zero budget. It is also a hard requirement on the stronger
# models: every gemini-2.5-pro call with a budget of 0 comes back
# "400 INVALID_ARGUMENT: The model does not support setting thinking_budget
# to 0". A caller that wants no reasoning must say so on purpose.
DEFAULT_THINKING_BUDGET = 512


def gemini_backend(
    model: str = "gemini-2.5-pro",
    project: str | None = None,
    location: str = "us-central1",
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    max_output_tokens: int = 1024,
    temperature: float = 0.0,
    request_timeout_ms: int = 120_000,
    api_key: str | None = None,
) -> Callable[[str], str]:
    """Return a backend that sends the prompt to Gemini and returns its text.

    Behind the [gemini] extra; google-genai is imported lazily so importing
    this module never requires the extra and the core install stays stdlib-only.

    thinking_budget is kept non-zero on purpose: judging SQL equivalence is
    open-ended reasoning, and Gemini 2.5 spends reasoning tokens *before* the
    answer — a zero budget plus the short output cap would truncate the two-line
    verdict. temperature=0 makes the verdict reproducible. A model at least as
    strong as the one under test reduces self-preference bias (a judge favoring
    output shaped like its own).
    """
    # ALWAYS, and before the SDK import: the SDK falls back to
    # GOOGLE_CLOUD_PROJECT when no project is given, so the fallback is
    # checked against the same list. This ran unconditionally until an
    # earlier draft made it conditional on an API key being present, and two
    # committed tests failed the moment GEMINI_API_KEY happened to be set in
    # a shell. The guard is about INTENT: naming a forbidden project is the
    # signal, whichever endpoint ends up billed.
    refuse_forbidden_project(project or os.environ.get("GOOGLE_CLOUD_PROJECT"),
                             "model calls")

    # An AI Studio developer key is a real convenience -- it needs no GCP
    # project at all, which is the whole point for somebody trying this tool
    # for the first time. It is NOT a drop-in swap, and an environment
    # variable alone must not choose it.
    #
    # Google's Gemini API Additional Terms, read 2026-09-12: on UNPAID
    # services, which includes an AI Studio key with no billing account
    # attached, "Google uses the content you submit and any generated
    # responses to provide, improve, and develop Google products and
    # services and machine learning technologies", "human reviewers may
    # read, annotate, and process your API input and output", and "do not
    # submit sensitive, confidential, or personal information to the Unpaid
    # Services". The flip to Paid is a billing account, not an amount paid.
    #
    # What this tool sends a model is a customer's business questions and
    # their real SQL. That is the confidential content those terms name. So
    # the key path is opt-in per call or by an explicit second variable, and
    # it says what it is doing on the way past. Vertex stays the default.
    # https://ai.google.dev/gemini-api/terms
    if api_key is None and os.environ.get("CONTEXTLIFT_ALLOW_AI_STUDIO") == "1":
        api_key = os.environ.get("GEMINI_API_KEY") or None
    if api_key:
        print("model calls: Google AI Studio Developer API, NOT Vertex. "
              "Without a billing account on the key this is an Unpaid "
              "Service: Google's terms permit human review of your prompts "
              "and use of them to improve its products, and tell you not to "
              "send confidential information. A customer's questions and SQL "
              "are confidential. https://ai.google.dev/gemini-api/terms",
              file=sys.stderr)
    import logging

    from google import genai  # lazy: keeps core install stdlib-only
    from google.genai import errors, types

    # the SDK logs "Direct use of automatic function calling ... is not
    # recommended" on every plain generate_content call; it reads as an
    # error in a run log and is not one
    logging.getLogger("google_genai.models").setLevel(logging.ERROR)

    # a hard timeout per HTTP call: an estimate or a run must fail loudly,
    # never sit silent (seen: a credentials lookup that looked like a hang)
    # google-genai takes raw headers rather than a ClientInfo, so the user
    # agent goes in beside the timeout. identity.py says why it is set.
    if api_key:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=request_timeout_ms,
                                           headers=identity.headers()))
    else:
        client = genai.Client(
            vertexai=True, project=project, location=location,
            http_options=types.HttpOptions(timeout=request_timeout_ms,
                                           headers=identity.headers()))
    usage = _new_usage()
    lock = threading.Lock()

    def _over_budget() -> bool:
        if _call.budget_usd is None:
            return False
        spent = estimate_cost_usd(_call.prices, usage["prompt_tokens"],
                                  usage["output_tokens"], usage["thought_tokens"])
        return spent is not None and spent >= _call.budget_usd

    def _call(prompt: str) -> str:
        with lock:
            if usage["stopped_at_budget"] or _over_budget():
                usage["stopped_at_budget"] = True
                return ""
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
        )
        # a quota or overload answer (429, 503) is a wait, not a failure: the
        # SDK's own retry gives up in seconds, and one 429 used to end a whole
        # mine run with a traceback. Five waits of 2, 4, 8, 16, 32 seconds.
        for attempt in range(RETRY_WAITS_SECONDS + 1):
            try:
                resp = client.models.generate_content(model=model, contents=prompt, config=config)
                break
            except errors.APIError as exc:
                if exc.code not in (429, 500, 503) or attempt == RETRY_WAITS_SECONDS:
                    raise
                wait = 2 ** (attempt + 1)
                # Counted before the sleep, so a caller reading `usage`
                # from another thread sees the retry while it is happening
                # rather than after it finishes.
                with lock:
                    usage["retries"] += 1
                    usage["retry_seconds"] += wait
                    usage["last_retry_code"] = exc.code
                time.sleep(wait)
        meta = resp.usage_metadata
        with lock:
            usage["calls"] += 1
            usage["prompt_tokens"] += getattr(meta, "prompt_token_count", 0) or 0
            usage["output_tokens"] += getattr(meta, "candidates_token_count", 0) or 0
            usage["thought_tokens"] += getattr(meta, "thoughts_token_count", 0) or 0
            # Two names for one number: `usage_metadata` calls it
            # cached_content_token_count and newer SDK surfaces call it
            # total_cached_tokens. Read both and take whichever answered, so
            # an SDK upgrade does not silently zero this.
            usage["cached_tokens"] += (
                getattr(meta, "cached_content_token_count", 0)
                or getattr(meta, "total_cached_tokens", 0) or 0)
        return resp.text or ""

    def count_tokens(text: str) -> int:
        try:
            return client.models.count_tokens(model=model, contents=text).total_tokens
        except Exception as exc:  # noqa: BLE001 - re-raised as one readable line
            name = type(exc).__name__
            if "Credentials" in name or "credentials" in str(exc):
                raise CredentialsMissing(
                    "no Google credentials found. Run `gcloud auth application-default "
                    "login`, or point CLOUDSDK_CONFIG at the gcloud config directory "
                    "that holds them, then retry.") from exc
            raise

    _call.usage = usage
    _call.count_tokens = count_tokens
    _call.budget_usd = None
    _call.prices = price_table(model)
    return _call


class CredentialsMissing(RuntimeError):
    """Raised instead of a 40-line traceback when Vertex has no credentials."""


def command_backend(command: str, timeout: float | None = None) -> Callable[[str], str]:
    """Any program as the generator: the agent under test, in any language.

    A prompt-to-text function cannot hold a router, a catalog lookup and a
    retry, so an agent used to enter through `import` and the code producing
    that CSV was the least examined part of every engagement. This runs the
    program once per (question, repetition) with one JSON object on stdin
    (id, question, rep, schema, context) and reads the SQL from stdout, as
    plain text or as a JSON object with `sql` and optionally `table`, the
    routed table, so routing and writing are graded separately.

    A non-zero exit or a timeout is an empty answer, counted in `usage` and
    with the stderr kept, not an exception: an agent that fails is a
    measurement, and retrying it three times would be three failures. The
    framework holds no state between calls and cannot know whether the
    program does; three repetitions of a deterministic runner are three runs,
    not three samples, and a caller checks that with one question first.
    """
    import subprocess
    import sys

    usage = _new_usage()
    usage.update({"failures": 0, "timeouts": 0})

    def _call(payload: str) -> str:
        usage["calls"] += 1
        try:
            done = subprocess.run(command, shell=True, input=payload, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            usage["timeouts"] += 1
            print(f"runner timed out after {timeout}s: {payload[:120]}", file=sys.stderr)
            return ""
        if done.returncode != 0:
            usage["failures"] += 1
            print(f"runner exit {done.returncode}: {(done.stderr or '').strip()[:400]}",
                  file=sys.stderr)
            return ""
        return done.stdout

    _call.usage = usage
    _call.structured = True                     # generate sends JSON, not a prompt
    _call.count_tokens = lambda text: 0         # the runner's cost is its own
    return _call
