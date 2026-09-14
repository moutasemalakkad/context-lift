# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Every `python -m NAME` in the Makefile names a module that imports.

The command is `contextlift` and the package is `context_lift`. They differ by
one character and only one of them is importable, which is how
`python -m contextlift` survived in the Makefile after the 2026-09-13 rename.

Three of the four `make example*` targets were broken by it, including
`make example`, which README's quick start tells a first-time reader to run.
Nothing caught it: the test suite imports the package directly, and CI runs
`make test`, not `make example`.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"

_DASH_M = re.compile(r"python\s+-m\s+([A-Za-z_][\w.]*)")


def _modules() -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(MAKEFILE.read_text().splitlines(), 1):
        for match in _DASH_M.finditer(line):
            found.append((number, match.group(1)))
    return found


def test_the_makefile_runs_a_module_at_all():
    """If this fails the check below is passing on an empty list."""
    assert _modules(), "no `python -m` in the Makefile, so nothing is checked"


@pytest.mark.parametrize("number,name", _modules(),
                         ids=lambda v: str(v))
def test_every_dash_m_module_in_the_makefile_imports(number, name):
    assert importlib.util.find_spec(name) is not None, (
        f"Makefile:{number} runs `python -m {name}` and there is no such "
        f"module. The console command is `contextlift`; the importable "
        f"package is `context_lift`."
    )


def test_the_console_command_and_the_package_are_both_what_they_claim():
    """Pins the pair, so a future rename has to change this test and notice.

    Read with a regex rather than `tomllib`, which is Python 3.11 and up.
    This project supports 3.10, CI runs the minimum, and the tomllib version
    of this test passed locally and failed there.
    """
    config = (ROOT / "pyproject.toml").read_text()
    entry = re.search(r'^contextlift\s*=\s*"([^"]+)"', config, re.M)
    assert entry, "pyproject.toml declares no `contextlift` console script"
    target = entry.group(1)
    assert target.startswith("context_lift"), (
        f"the entry point is {target}; the package name moved")
    assert importlib.util.find_spec("context_lift") is not None
    assert importlib.util.find_spec("contextlift") is None, (
        "a module named `contextlift` now exists, so the confusion this test "
        "guards is gone -- delete it")
