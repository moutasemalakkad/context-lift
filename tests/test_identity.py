# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One identity, said the same way to every Google service.

These pin two claims the framework makes about itself, both of which a
customer's security review will ask about and both of which are easy to
break by accident later:

- every Google client the framework opens carries the user agent, because a
  count taken from the warehouse calls alone is wrong in a way nobody can
  see from the outside;
- the attribution never fails a run. A missing library or a closed stdin
  gives a worse label, never an exception, because an attribution string is
  worth less than the run it would have stopped.
"""
import pytest

from context_lift import identity


def test_every_user_agent_token_is_name_slash_semver(monkeypatch):
    """The shape is required, not stylistic.

    The software that reads these strings on the other side extracts its
    fields from `NAME/VERSION`, where NAME is lower-case letters, digits and
    hyphens. A token written any other way is filed under no name, so the
    usage is collected and then not attributable to anything. An earlier
    version of this wrote `context-lift-caller/agent`, which has a word
    where a version belongs.
    """
    import re
    monkeypatch.delenv(identity.EXTRA_TOKEN, raising=False)
    tokens = identity.user_agent().split(" ")
    assert len(tokens) == 2
    for token in tokens:
        name, slash, version = token.partition("/")
        assert slash, f"{token!r} is not NAME/VERSION"
        assert re.fullmatch(r"[a-z0-9-]+", name), f"{name!r} is not a legal name"
        assert version, f"{token!r} has no version"
    assert tokens[0] == f"context-lift/{identity.version()}"
    # who started the run lives in the NAME half, where a word is allowed
    assert tokens[1].partition("/")[0].startswith("context-lift-caller-")
    assert tokens[1].partition("/")[0].removeprefix(
        "context-lift-caller-") in identity.CALLERS


def test_somebody_wrapping_this_tool_can_add_their_own_name(monkeypatch):
    """Append, never replace. A tool that hardcodes its identity and offers
    no way to add to it is a nuisance to anyone building on top of it."""
    monkeypatch.setenv(identity.EXTRA_TOKEN, "acme-wrapper/2.1.0")
    agent = identity.user_agent()
    assert agent.endswith(" acme-wrapper/2.1.0")
    assert agent.startswith("context-lift/"), "ours is still first"


def test_client_info_leads_with_our_name():
    """The libraries put their own details after ours, so ours is first."""
    info = identity.client_info()
    assert info is not None, "google-api-core is a dependency; if it is gone, say so loudly"
    full = info.to_user_agent()
    assert full.startswith(f"{identity.NAME}/")
    # and the library still describes itself, which is what Google reads to
    # know which client version made the call
    assert "gl-python/" in full


def test_headers_carry_both_names_google_reads():
    got = identity.headers()
    assert got["user-agent"] == identity.user_agent()
    assert got["x-goog-api-client"] == identity.user_agent()


@pytest.mark.parametrize("env,expected", [
    ({"AI_AGENT": "1"}, "agent"),
    ({"CLAUDECODE": "1"}, "agent"),
    ({"CI": "true"}, "ci"),
    # an agent marker beats CI and beats a terminal: a host agent usually
    # runs commands with a terminal attached, and counting it as a person
    # is the one mistake this field exists to prevent
    ({"CLAUDECODE": "1", "CI": "true"}, "agent"),
    ({"CONTEXT_LIFT_CALLER": "terminal"}, "terminal"),
    # a value nobody recognises falls through rather than being reported
    ({"CONTEXT_LIFT_CALLER": "nonsense"}, None),
])
def test_who_started_the_run(env, expected, monkeypatch):
    for name in identity.AGENT_MARKERS + ("CI", identity.CALLER_OVERRIDE):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    got = identity.caller()
    assert got in identity.CALLERS
    if expected is not None:
        assert got == expected


def test_job_labels_obey_bigquerys_rules(monkeypatch):
    """Lower case, digits, dashes and underscores, 63 characters. A stage
    name that breaks them is cleaned rather than failing the job: a label is
    not worth losing a query over."""
    monkeypatch.setattr(identity, "_stage", "")
    labels = identity.job_labels("Catalog Push!")
    assert labels["stage"] == "catalog_push_"
    assert labels["app"] == identity.NAME
    assert labels["caller"] in identity.CALLERS
    for key, value in labels.items():
        assert len(key) <= 63 and len(value) <= 63
        assert value == value.lower()
        assert all(c.isalnum() or c in "-_" for c in value), value
    # a very long stage name is cut, not rejected
    assert len(identity.job_labels("x" * 200)["stage"]) == 63
    # and no stage at all means no stage key, not an empty one
    assert "stage" not in identity.job_labels("")


def test_the_stage_comes_from_the_command_the_person_ran():
    """Derived from the handler, not from argv: a handler has exactly one
    name and argv has global flags in front of it."""
    from context_lift import cli
    args = cli._build_parser().parse_args(
        ["mine", "--db", "bq://p", "--queries", "/tmp/x.csv", "--out", "/tmp/o.csv"])
    assert args.func.__name__.removeprefix("_cmd_") == "mine"


def test_every_google_client_in_the_tree_is_given_an_identity():
    """The check that stops this quietly going stale.

    A new client opened somewhere without `client_info` or `headers` makes
    the usage count wrong by however much that service is used, and nothing
    else in the suite would notice. So the rule is structural: a line that
    builds a Google client must mention one of them.
    """
    import pathlib
    import re
    root = pathlib.Path(identity.__file__).parent
    builds_a_client = re.compile(
        r"\b(bigquery\.Client|genai\.Client|self\._bq\.Client|"
        r"\w*ServiceClient)\s*\(")
    unnamed = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "identity.py":
            continue
        text = path.read_text()
        for match in builds_a_client.finditer(text):
            # the call can span lines; read to its closing bracket
            start = match.start()
            window = text[start:start + 600]
            call = window[:window.find(")") + 1] if ")" in window else window
            depth, end = 0, len(call)
            for i, ch in enumerate(window):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            call = window[:end]
            if "client_info" not in call and "headers" not in call:
                line = text[:start].count("\n") + 1
                unnamed.append(f"{path.relative_to(root)}:{line} {match.group(1)}")
    assert not unnamed, (
        "these Google clients carry no identity, so their usage is not "
        "attributed to this framework:\n  " + "\n  ".join(unnamed))
