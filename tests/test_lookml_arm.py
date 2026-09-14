# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The arm builder refuses more often than it writes, and that is the point.

An arm built from a partial read gets SCORED as though it were the whole
project, and a round cannot tell the difference. So the refusals are the
feature and they are what these tests pin.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("lkml")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "lookml_arm.py"


def run(lookml, out):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--lookml", str(lookml), "--out", str(out)],
        capture_output=True, text=True, timeout=120)


def test_a_partial_read_is_refused_not_shipped(tmp_path):
    """One unparseable file means the arm is missing whatever was in it, and
    nothing downstream could ever notice."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "ok.view.lkml").write_text(
        "view: v {\n  measure: total { type: sum sql: ${x} ;; }\n}\n")
    (project / "bad.view.lkml").write_text("view: b {\n  dimension: d { sql: a ;; ;; }\n")
    result = run(project, tmp_path / "out")
    assert result.returncode == 1
    assert "did not parse" in result.stderr
    assert not (tmp_path / "out").exists()


def test_a_project_with_no_metrics_is_refused(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "v.view.lkml").write_text(
        "view: v {\n  dimension: a { type: string }\n}\n")
    result = run(project, tmp_path / "out")
    assert result.returncode == 1
    assert "no context to test" in result.stderr


def test_the_arm_carries_table_names_or_round_would_refuse_it(tmp_path):
    """`round` stops with "every table the arms name is gone" when the spec
    names none. The empty schema files are what stop that, so their presence
    is part of the contract and not an implementation detail."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "v.view.lkml").write_text(
        "view: v {\n  measure: total { type: sum sql: ${x} ;; }\n}\n")
    out = tmp_path / "out"
    assert run(project, out).returncode == 0
    assert (out / "rules" / "00-from-lookml.md").is_file()
    assert list((out / "schema").glob("*.yaml"))


def test_the_readme_records_where_the_context_came_from(tmp_path):
    """A number nobody can trace to a source is not evidence. The generated
    README carries the source commit so the arm can be rebuilt exactly."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "v.view.lkml").write_text(
        "view: v {\n  measure: total { type: sum sql: ${x} ;; }\n}\n")
    out = tmp_path / "out"
    run(project, out)
    readme = (out / "README.md").read_text()
    assert "Do not edit by hand" in readme
    assert "No LookML source is copied" in readme
    assert "not a git checkout" in readme      # tmp_path is not a repo


def test_the_committed_arm_matches_what_the_script_produces():
    """The committed arm is a shipped artifact, so it must be exactly what the
    committed script writes -- otherwise it drifted and the round measured
    something nobody can rebuild."""
    arm = ROOT / "examples" / "thelook" / "lookml-arm"
    if not arm.is_dir():
        pytest.skip("the committed arm is absent")
    rules = (arm / "rules" / "00-from-lookml.md").read_text()
    # Pins the defects fixed on 2026-09-13: the aggregation is applied, and the
    # restriction is never presented as SQL.
    assert "SUM(sale_price)" in rules
    assert "sale_price\n" not in rules.replace("SUM(sale_price)", "")
    assert (arm / "README.md").is_file()
    assert "88d68c32" in (arm / "README.md").read_text()
