# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One spec/schema file per table, from INFORMATION_SCHEMA. No cloud here."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from spec_schema import build, write_one  # noqa: E402

# Both projects hold the same dataset, which is the normal shape of a
# production and a pre-production estate and the case that broke the first
# version of this script.
WAREHOUSE = {
    ("acme-analytics-p", "dw_pos"): {"fct_gbl_pos": [("store_id", "STRING")]},
    ("acme-analytics-pp", "dw_pos"): {"fct_gbl_pos": [("store_id", "STRING")]},
}


def reader(project, dataset, tables):
    found = WAREHOUSE.get((project, dataset), {})
    return {t: c for t, c in found.items() if t in tables}


def test_a_two_part_name_lands_in_one_project_not_every_project(tmp_path):
    """The bug this script shipped with: 22 files for 11 tables.

    A two-part `dataset.table` name resolves to the FIRST project in the
    search order that has it. Writing a file per project puts the SAME table
    in the arm twice under two names, and the agent then has to disambiguate
    a table list for no reason.
    """
    written, missing = build(
        ["dw_pos.fct_gbl_pos"],
        ["acme-analytics-p", "acme-analytics-pp"],
        tmp_path, reader)

    assert len(written) == 1, [p.name for p in written]
    assert written[0].name == "acme-analytics-p.dw_pos.fct_gbl_pos.yaml"
    assert missing == set()


def test_the_search_order_decides_which_project_wins(tmp_path):
    written, _ = build(["dw_pos.fct_gbl_pos"],
                       ["acme-analytics-pp", "acme-analytics-p"],
                       tmp_path, reader)

    assert written[0].name == "acme-analytics-pp.dw_pos.fct_gbl_pos.yaml"


def test_a_three_part_name_is_taken_as_written(tmp_path):
    """An explicit project is an instruction, not a hint."""
    written, missing = build(["acme-analytics-pp.dw_pos.fct_gbl_pos"],
                             ["acme-analytics-p"], tmp_path, reader)

    assert [p.name for p in written] == [
        "acme-analytics-pp.dw_pos.fct_gbl_pos.yaml"]
    assert missing == set()


def test_a_table_no_project_has_is_reported_not_silently_skipped(tmp_path):
    """A gold needing it cannot be answered by ANY arm, so the person has to
    hear about it: drop the gold, or get the grant."""
    written, missing = build(["dw_pos.gone_away"], ["acme-analytics-p"],
                             tmp_path, reader)

    assert written == []
    assert missing == {"dw_pos.gone_away"}


def test_column_descriptions_are_written_empty_on_purpose(tmp_path):
    """The file exists so the ARM carries the table.

    The estate's own descriptions belong in `authored/`, which the export
    merges on top. Writing them here would put them in the CONTROL arm too,
    and the measured lift would then credit authored context for work the
    customer's own build already did.
    """
    path = write_one(tmp_path, "p", "d", "t", [("amount", "NUMERIC")])
    text = path.read_text()

    assert "description: ''   # NUMERIC" in text
    assert "so the ARM carries the table" in text


def test_the_filename_is_the_three_part_table_name(tmp_path):
    """`export._read_schema` refuses a schema file whose stem is not
    project.dataset.table, so a name built any other way fails at read."""
    path = write_one(tmp_path, "proj", "ds", "tbl", [("a", "INT64")])

    assert path.stem.count(".") == 2
    assert path.stem == "proj.ds.tbl"
