# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A missing optional extra must fail in words, not in a traceback.

Found on a fresh `uv tool install .` in a directory with no repository, which
is how a new person meets this tool. `contextlift semantic --lookml` printed
`ModuleNotFoundError: No module named 'lkml'` under six frames of traceback.
Two things were wrong with that. A traceback reads as a crash rather than a
setup step, and `lkml` is not a name anyone can act on -- the extra that
carries it is spelled `lookml`, so even guessing the install command took a
step. The mapping already existed in `EXTRAS`; nothing read it when an import
failed.
"""
from __future__ import annotations

import pytest

from context_lift.doctor import EXTRAS, extras_for_module, missing_extra_message


def test_the_module_name_is_translated_to_the_extra_name():
    """The whole point: the module is `lkml`, the extra is `lookml`."""
    assert extras_for_module("lkml") == [("lookml", "semantic --lookml")]


@pytest.mark.parametrize("extra", sorted(EXTRAS))
def test_every_declared_extra_is_reachable_from_its_module(extra):
    """A new extra added to EXTRAS without a resolvable module would fail in a
    traceback again, silently."""
    module = EXTRAS[extra][0]
    # An EXACT path match must resolve to exactly one extra, even though four
    # of the five share the `google` top-level package.
    assert extras_for_module(module) == [(extra, EXTRAS[extra][1])]


def test_a_bare_google_failure_names_every_candidate_rather_than_guessing_one():
    """The bug this test caught, and it would have been worse than the
    traceback it replaced. Four extras import under `google`, so matching on
    the top-level package returned whichever came first in the dict: a missing
    `google.cloud.dataplex_v1` was reported as the `[bigquery]` extra. A
    traceback sends nobody anywhere; a confident wrong install command sends
    them somewhere wrong."""
    found = extras_for_module("google")
    assert len(found) >= 4
    text = missing_extra_message(ModuleNotFoundError("x", name="google"))
    for extra in ("bigquery", "catalog", "ca", "gemini"):
        assert f"[{extra}]" in text
    assert "more than one" in text


def test_an_exact_nested_path_still_resolves_to_one_extra():
    assert extras_for_module("google.cloud.dataplex_v1") == [
        ("catalog", EXTRAS["catalog"][1])]


def test_the_message_says_what_to_type():
    text = missing_extra_message(ModuleNotFoundError("x", name="lkml"))
    assert "uv tool install 'contextlift[lookml]'" in text
    assert "uv pip install -e '.[lookml]'" in text
    assert "contextlift doctor" in text
    assert "Traceback" not in text


def test_the_message_says_what_the_extra_is_for():
    """An install command with no reason attached invites installing all of
    them, which defeats a stdlib-only core."""
    assert "semantic --lookml" in missing_extra_message(
        ModuleNotFoundError("x", name="lkml"))


def test_a_module_that_is_not_an_extra_is_called_a_bug_not_a_setup_step():
    """The failure this must not cause: sending someone to install a package
    that does not exist, when the real fault is a missing import in our code."""
    text = missing_extra_message(ModuleNotFoundError("x", name="numpy"))
    assert "bug in contextlift" in text
    assert "uv tool install" not in text


def test_the_real_verb_fails_in_words(monkeypatch, tmp_path, capsys):
    """End to end through `main`, because the handler is where the lazy import
    lives and a helper tested alone would not prove the wiring."""
    import builtins
    from context_lift import cli

    real_import = builtins.__import__

    def refuse_lkml(name, *args, **kwargs):
        if name == "lkml":
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_lkml)
    project = tmp_path / "p"
    project.mkdir()
    (project / "v.view.lkml").write_text("view: v {}\n")
    code = cli.main(["semantic", "--lookml", str(project)])
    assert code == 2
    assert "[lookml] extra" in capsys.readouterr().err
