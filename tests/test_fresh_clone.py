# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""A green suite on this laptop is not a green suite.

Run in a fresh clone on 2026-09-12, `make test` did two things the machine it
was written on could never show.

It failed outright: `error: Failed to spawn: pytest`. The target runs
`uv run --no-project pytest`, which uses the ambient `.venv`, and nothing had
created one. It had worked here since the first day because `.venv` was
already there. The README meanwhile promised "make test runs green from a
fresh clone with no credentials".

And it reported 13 skips against 11 here. The two extra were the DuckDB
backend and the Conversational Analytics runner, skipping on imports that
happened to be installed here from other work. Two real code paths tested on
one laptop and nowhere else, under a suite reporting green.

These tests are about the PACKAGING, so they read the declarations rather than
running anything. The real verification is a fresh clone, and it is not
something a test can do cheaply; what a test can do is stop the declarations
from drifting back.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# Modules a test may skip on WITHOUT being installable by `make install`.
# Every entry needs a reason, because this list is the exception to the rule
# and an unexplained entry is how the rule gets hollowed out.
ALLOWED_TO_SKIP = {
    # Cross-checks this project's exact arithmetic against a reference
    # implementation. It skips identically on every machine, so the suite is
    # not claiming to test something it is not. Adding it would pull in numpy,
    # scipy and pandas to verify an exact integer calculation.
    "statsmodels.stats.proportion": "reference cross-check only",
    "statsmodels.stats.contingency_tables": "reference cross-check only",
}


def dev_dependencies() -> set[str]:
    """The package names in [project.optional-dependencies] dev."""
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"^dev = \[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert block, "no dev extra found in pyproject.toml"
    return {re.split(r"[><=!\[]", name)[0].strip().lower()
            for name in re.findall(r'"([^"]+)"', block.group(1))}


def skipped_modules() -> dict[str, list[str]]:
    """{module: [file, ...]} for every importorskip in the suite."""
    found: dict[str, list[str]] = {}
    for path in sorted(TESTS.glob("test_*.py")):
        for module in re.findall(r'importorskip\(\s*"([^"]+)"',
                                 path.read_text()):
            found.setdefault(module, []).append(path.name)
    return found


def test_the_suite_actually_imports_something():
    """Guards the regex: a rename that silently matched nothing would make
    every test below pass for the wrong reason."""
    assert len(skipped_modules()) >= 3


@pytest.mark.parametrize("module", sorted(skipped_modules()))
def test_every_module_the_suite_skips_on_is_installed_by_make_install(module):
    """A skip on a fresh clone that does not skip here means a code path is
    tested on one machine only, while the suite reports green."""
    if module in ALLOWED_TO_SKIP:
        pytest.skip(f"declared exception: {ALLOWED_TO_SKIP[module]}")
    dev = dev_dependencies()
    # `google.cloud.dataplex_v1` is shipped by `google-cloud-dataplex`, and
    # `lkml` by `lkml`: the import path and the distribution name differ often
    # enough that matching has to be loose on both separators.
    head = module.split(".")[0].replace("_", "-")
    tail = module.split(".")[-1].replace("_", "-")
    tail = re.sub(r"-v\d+$", "", tail)          # dataplex_v1 -> dataplex
    assert any(head in name or tail in name or name.replace("-", "") in module
               for name in dev), (
        f"the suite skips on {module!r} when it is absent, but nothing in the "
        f"dev extra installs it. Either add it to `dev = [...]` in "
        f"pyproject.toml, or add it to ALLOWED_TO_SKIP here with the reason. "
        f"dev currently has: {sorted(dev)}")


def test_make_test_creates_the_venv_it_needs():
    """The fresh-clone failure itself. `make test` must not assume a venv that
    only exists because someone ran `make install` first -- a person who has
    to know the order is a person the tool has failed."""
    makefile = (ROOT / "Makefile").read_text()
    target = re.search(r"^test:(.*)$", makefile, re.MULTILINE)
    assert target, "no test target in the Makefile"
    assert ".venv" in target.group(1), (
        "the `test` target no longer depends on .venv, so `make test` in a "
        "fresh clone will print `Failed to spawn: pytest` again")


def test_the_readme_still_claims_a_green_suite_from_a_fresh_clone():
    """If that promise is ever dropped from the README, the two tests above
    are guarding a claim nobody is making, and should be revisited rather
    than left as decoration."""
    readme = (ROOT / "README.md").read_text()
    assert "fresh clone" in readme
