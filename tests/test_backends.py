# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The forbidden-projects rule stops a model call before the SDK is even
imported. Offline: the SDK is replaced by an import that fails, so a
refusal that came too late would show as ImportError, not SystemExit."""
import sys

import pytest

from context_lift.backends import (forbidden_project_prefixes, gemini_backend,
                               refuse_forbidden_project)


def test_a_forbidden_project_is_refused_before_the_sdk_is_imported(monkeypatch):
    monkeypatch.setenv("CONTEXTLIFT_FORBIDDEN_PROJECTS", "acme-prod,acme-")
    # an import of google.* now raises; the refusal must come first
    monkeypatch.setitem(sys.modules, "google", None)

    with pytest.raises(SystemExit) as refused:
        gemini_backend(model="gemini-2.5-flash", project="acme-analytics")

    assert str(refused.value) == ("refusing: project 'acme-analytics' matches "
                                  "CONTEXTLIFT_FORBIDDEN_PROJECTS ('acme-'); model calls would "
                                  "be billed there. Use a project you own.")


def test_the_sdk_fallback_google_cloud_project_is_checked_too(monkeypatch):
    monkeypatch.setenv("CONTEXTLIFT_FORBIDDEN_PROJECTS", "acme-")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "acme-prod")
    monkeypatch.setitem(sys.modules, "google", None)

    with pytest.raises(SystemExit) as refused:
        gemini_backend(project=None)

    assert "'acme-prod'" in str(refused.value)


def test_an_empty_variable_forbids_nothing(monkeypatch):
    monkeypatch.setenv("CONTEXTLIFT_FORBIDDEN_PROJECTS", "")
    assert forbidden_project_prefixes() == []
    assert refuse_forbidden_project("acme-prod", "BigQuery jobs") == "acme-prod"
    monkeypatch.delenv("CONTEXTLIFT_FORBIDDEN_PROJECTS")
    assert refuse_forbidden_project("acme-prod", "BigQuery jobs") == "acme-prod"
    assert refuse_forbidden_project(None, "BigQuery jobs") is None


def test_prefixes_are_split_on_commas_and_trimmed(monkeypatch):
    monkeypatch.setenv("CONTEXTLIFT_FORBIDDEN_PROJECTS", " acme-prod , , beta")
    assert forbidden_project_prefixes() == ["acme-prod", "beta"]
    assert refuse_forbidden_project("acme-staging", "x") == "acme-staging"


def _fake_sdk(monkeypatch):
    """A stand-in google.genai, so no network and no real SDK is needed."""
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.models = object()

    class HttpOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake = type("GenAI", (), {
        "Client": Client,
        "errors": type("Errors", (), {}),
        "types": type("Types", (), {"HttpOptions": HttpOptions}),
    })
    google = type(sys)("google")
    google.genai = fake
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", fake)
    return seen


def test_the_api_key_env_var_alone_does_not_switch_endpoint(monkeypatch):
    """GEMINI_API_KEY being present is not consent to use it.

    The danger this pins. An AI Studio key with no billing account is an
    Unpaid Service, and Google's Gemini API terms say Google uses that
    content to improve its products, that human reviewers may read it, and
    "do not submit sensitive, confidential, or personal information". What
    this tool sends a model is a customer's business questions and their
    real SQL. So an ambient environment variable must not quietly move that
    traffic off Vertex, and the operator has to say so out loud.
    """
    seen = _fake_sdk(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "present-but-not-consented")
    monkeypatch.delenv("CONTEXTLIFT_ALLOW_AI_STUDIO", raising=False)

    gemini_backend(model="gemini-2.5-flash", project="p")

    assert "api_key" not in seen
    assert seen["vertexai"] is True


def test_the_explicit_opt_in_uses_the_developer_api_and_says_so(monkeypatch,
                                                                capsys):
    """With consent given, the key is used and the terms are printed."""
    seen = _fake_sdk(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "secret-not-on-argv")
    monkeypatch.setenv("CONTEXTLIFT_ALLOW_AI_STUDIO", "1")

    gemini_backend(model="gemini-2.5-flash", project="ignored-for-developer-api")

    assert seen["api_key"] == "secret-not-on-argv"
    assert "vertexai" not in seen
    assert "project" not in seen
    assert "location" not in seen
    warning = capsys.readouterr().err
    assert "NOT Vertex" in warning
    assert "confidential" in warning


def test_a_key_passed_in_code_needs_no_env_opt_in(monkeypatch, capsys):
    """A caller writing `api_key=` has already made the choice explicitly."""
    seen = _fake_sdk(monkeypatch)
    monkeypatch.delenv("CONTEXTLIFT_ALLOW_AI_STUDIO", raising=False)

    gemini_backend(model="gemini-2.5-flash", api_key="passed-in-code")

    assert seen["api_key"] == "passed-in-code"
    assert "NOT Vertex" in capsys.readouterr().err


def test_a_forbidden_project_is_refused_even_with_a_key(monkeypatch):
    """The guard is about INTENT, not about which endpoint gets billed.

    Making it conditional on a key is what broke two committed tests the
    moment GEMINI_API_KEY happened to be set in a shell.
    """
    _fake_sdk(monkeypatch)
    monkeypatch.setenv("CONTEXTLIFT_FORBIDDEN_PROJECTS", "acme-prod")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("CONTEXTLIFT_ALLOW_AI_STUDIO", "1")

    with pytest.raises(SystemExit):
        gemini_backend(model="gemini-2.5-flash", project="acme-prod-eu")


def test_the_spend_line_reports_cached_tokens_without_claiming_a_discount():
    """Implicit caching decides whether a big shared-context arm is actually
    more expensive than a small per-question one, so the count has to be on
    the spend line. But the reference gives no multiplier -- it says only that
    cost savings are passed on -- so the printed dollar figure must stay at
    the full rate. A tool that guesses a discount under-states a real bill.
    """
    from context_lift.cli import spend_line

    usage = {"calls": 10, "prompt_tokens": 60_000, "output_tokens": 800,
             "thought_tokens": 2_000, "cached_tokens": 45_000}
    line = spend_line(usage, 0.0245)
    assert "60,000 prompt" in line
    assert "45,000 prompt tokens (75%)" in line
    assert "implicit cache" in line
    assert "$0.0245" in line, "the dollar figure must be the full-rate one"


def test_the_cached_line_is_absent_when_nothing_was_cached():
    """A zero would read as a finding. Most arms are below the 2,048-token
    minimum and will never cache, and their spend line should say nothing."""
    from context_lift.cli import spend_line

    usage = {"calls": 3, "prompt_tokens": 900, "output_tokens": 60,
             "thought_tokens": 0, "cached_tokens": 0}
    line = spend_line(usage, 0.001)
    assert "cache" not in line
    assert "\n" not in line, "one line when there is nothing extra to say"


def test_usage_carries_a_cached_token_field_from_the_start():
    """If the field is missing, every `usage.get('cached_tokens')` reads zero
    and the whole measurement silently reports 'no caching'."""
    from context_lift.backends import _new_usage

    assert _new_usage()["cached_tokens"] == 0


def test_the_model_estimate_names_the_line_it_does_not_price():
    """A cost sheet that omits its largest line is worse than no cost sheet.

    Found 2026-09-12 by reading the job ledger instead of this output:
    per-command totals came to about $74 for a day the ledger put at
    $149.61, and the gap was the SQL the agent wrote. Nothing priced those
    queries and nothing mentioned them.

    They cannot be priced in advance -- that SQL does not exist until the run
    does -- so the requirement is that the estimate SAYS SO and names the
    command that recovers the number afterwards.
    """
    from context_lift.cli import _format_estimate

    text = _format_estimate({
        "questions": 25, "reps": 1, "calls": 25,
        "prompt_tokens_per_call": 410, "output_tokens_per_call": 80,
        "output_tokens_source": "default", "estimated_cost_usd": 0.01,
        "price_table": {"as_of": "2026-09-01"}, "prompts_estimated": 0,
    }, "gemini-2.5-flash")

    assert "model calls only" in text
    assert "answer-cost" in text, "it must name the command that recovers it"
    assert "cannot be" in text, (
        "it must say the predicted SQL cannot be priced in advance, not imply "
        "somebody forgot to")


def test_the_command_the_estimate_names_exists():
    """The estimate points at `make answer-cost`. A pointer to a target that
    does not exist is worse than no pointer."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    assert "answer-cost:" in (root / "Makefile").read_text()
    assert (root / "scripts" / "answer_cost.py").is_file()
