# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""Putting a catalog back, which the catalog cannot do itself."""
import pytest

from context_lift.rollback import load, plan, save


def record(table, before, wrote):
    return {"table": table, "entry": f"entries/{table}",
            "before": {"overview": before, "queries": None},
            "wrote": {"overview": wrote, "queries": None}}


def live(table, overview):
    return {table: {"overview": overview, "queries": None}}


def test_an_entry_still_holding_what_we_wrote_is_safe_to_restore():
    decided = plan([record("t", "old", "new")], live("t", "new"))

    assert [table for table, _ in decided.restore] == ["t"]
    assert decided.refused == []


def test_an_entry_somebody_else_edited_is_refused_by_name():
    """Pushing the saved bytes over their edit erases their work, and the
    catalog keeps no record that it happened. There is no undo to appeal to."""
    decided = plan([record("t", "old", "new")], live("t", "a colleague's page"))

    assert decided.restore == []
    assert "erase their work" in decided.refused[0][1]


def test_an_entry_already_back_is_left_alone():
    """Saying so beats writing the same bytes again: a write that changes
    nothing still replaces the aspect and moves its update time."""
    decided = plan([record("t", "old", "new")], live("t", "old"))

    assert decided.restore == [] and decided.refused == []
    assert decided.unchanged == ["t"]


def test_one_edited_table_does_not_stop_the_others():
    """On a seven-table push, the one somebody touched is exactly the one
    worth looking at. Refusing everything makes a person roll back by hand."""
    decided = plan([record("a", "old", "new"), record("b", "old", "new")],
                   {**live("a", "new"), **live("b", "theirs")})

    assert [table for table, _ in decided.restore] == ["a"]
    assert [table for table, _ in decided.refused] == ["b"]


def test_a_table_with_no_entry_now_is_refused_not_skipped():
    decided = plan([record("gone", "old", "new")], {})

    assert decided.refused[0][1] == "it has no catalog entry now"


def test_reindenting_a_page_is_not_somebody_elses_edit():
    """The comparison is whitespace-insensitive, as everywhere else here."""
    decided = plan([record("t", "old", "one two")], live("t", "one\n  two"))

    assert [table for table, _ in decided.restore] == ["t"]


def test_a_missing_directory_says_what_to_do_next_time(tmp_path):
    """A rollback with nothing saved is unrecoverable, so the message has to
    name the flag rather than report a missing path."""
    with pytest.raises(SystemExit) as refused:
        load(tmp_path / "never-written")

    assert "--previous-out" in str(refused.value)


def test_a_saved_record_survives_a_round_trip(tmp_path):
    save(tmp_path, "p.d.t", "entries/p.d.t",
         before={"overview": "old", "queries": None},
         wrote={"overview": "new", "queries": [{"sql": "SELECT 1"}]})

    [back] = load(tmp_path)

    assert back["before"]["overview"] == "old"
    assert back["wrote"]["queries"] == [{"sql": "SELECT 1"}]


def test_a_table_name_becomes_a_filename_without_colliding(tmp_path):
    """Dots and slashes both become underscores, so two different tables
    cannot land on one file and silently lose one of the two records."""
    save(tmp_path, "p.d.t", "e", {}, {})
    save(tmp_path, "p.d.other", "e", {}, {})

    assert len(load(tmp_path)) == 2


# --- the command that actually writes, which no test reached before ---------
#
# plan() decides; _cmd_catalog_rollback writes. Everything above tests the
# deciding. The writing walked straight past the worked examples for as long
# as it existed, so a rollback left every pushed example on the entry for
# good, and nothing failed.

class _FakeTransport:
    def __init__(self):
        self.removed: list[tuple[str, str]] = []
        self.wrote_queries: list[tuple[str, list]] = []

    def remove_aspect(self, entry, key):
        self.removed.append((entry, key))

    def write_queries(self, entry, rows):
        self.wrote_queries.append((entry, rows))


class _FakeStore:
    def __init__(self, held):
        self.held = held                      # {table: {"overview", "queries"}}
        self.transport = _FakeTransport()
        self.wrote_overview: list[tuple[str, str]] = []

    def resolve(self, table):
        return f"entries/{table}"

    def read_entry(self, entry):
        return self.held[entry.rsplit("/", 1)[1]]

    def write_overview(self, entry, text):
        self.wrote_overview.append((entry, text))


def _run_rollback(tmp_path, monkeypatch, before, wrote, held):
    from context_lift import cli
    save(tmp_path, "p.d.t", "entries/p.d.t", before=before, wrote=wrote)
    store = _FakeStore({"p.d.t": held})
    monkeypatch.setattr(cli, "_catalog_store", lambda args: store)
    code = cli.main(["catalog-rollback", "--previous", str(tmp_path),
                     "--project", "p", "--catalog-location", "us"])
    return code, store


def test_rollback_takes_the_worked_examples_off_an_entry_that_had_none(tmp_path, monkeypatch, capsys):
    """A push writes example queries. If the entry carried none before, the
    only way back is taking the aspect OFF: write_queries merges by SQL text,
    so writing an empty list over the pushed rows removes nothing.

    This is the case on an estate whose catalog is empty, which is the
    ordinary case: our worked examples would otherwise live in their catalog
    with no command able to remove them.
    """
    pushed = [{"question": "how many orders?", "sql": "SELECT COUNT(*) FROM o"}]
    code, store = _run_rollback(
        tmp_path, monkeypatch,
        before={"overview": None, "queries": None},
        wrote={"overview": "our page", "queries": pushed},
        held={"overview": "our page", "queries": pushed})

    assert code == 0
    removed_keys = [key for _entry, key in store.transport.removed]
    assert "dataplex-types.global.queries" in removed_keys, \
        "the worked examples must come off, or they stay in their catalog for good"
    assert "dataplex-types.global.overview" in removed_keys, \
        "there was no page before the push either"
    assert store.transport.wrote_queries == [], "nothing to put back"
    assert "taken off 1 of them" in capsys.readouterr().out


def test_rollback_puts_back_the_examples_that_were_there_before(tmp_path, monkeypatch):
    """When rows existed before the push, the aspect still has to come off
    first: write_queries merges, so writing the old rows over the new ones
    leaves BOTH sets on the entry."""
    was = [{"question": "theirs", "sql": "SELECT 1"}]
    now = was + [{"question": "ours", "sql": "SELECT 2"}]
    code, store = _run_rollback(
        tmp_path, monkeypatch,
        before={"overview": "their page", "queries": was},
        wrote={"overview": "our page", "queries": now},
        held={"overview": "our page", "queries": now})

    assert code == 0
    assert ("entries/p.d.t", "dataplex-types.global.queries") in store.transport.removed
    assert store.transport.wrote_queries == [("entries/p.d.t", was)], \
        "exactly the rows that were there, and only those"
    assert store.wrote_overview == [("entries/p.d.t", "their page")]


def test_rollback_writes_nothing_when_somebody_else_edited_the_entry(tmp_path, monkeypatch):
    """The refusal has to reach the writing, not only the plan. A rollback
    that prints REFUSED and writes anyway is worse than no rollback."""
    code, store = _run_rollback(
        tmp_path, monkeypatch,
        before={"overview": None, "queries": None},
        wrote={"overview": "our page", "queries": None},
        held={"overview": "a colleague's page", "queries": None})

    assert code == 1
    assert store.transport.removed == [] and store.wrote_overview == []
    assert store.transport.wrote_queries == []
