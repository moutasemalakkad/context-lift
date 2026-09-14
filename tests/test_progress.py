# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The one progress line four stages share.

Every assertion here is about a reader. A bar that reads full with 12 items
left makes somebody walk away from a running job; a zero dropped from the
funnel makes them think the check did not run.
"""
import time

from context_lift.progress import Progress, bar, duration, eta, line


def test_a_bar_is_never_full_until_it_is_done():
    """425 of 437 rounds to a full bar. Somebody reading that in a log walks
    away from a run with 12 items left in it."""
    assert bar(425, 437) == "[###############-]"
    assert bar(437, 437) == "[################]"
    assert bar(0, 437) == "[----------------]"
    # and a stage with nothing to do still draws a bar rather than dividing
    # by zero
    assert bar(0, 0) == "[----------------]"


def test_a_duration_is_read_at_a_glance():
    assert duration(4) == "4s"
    assert duration(134) == "2m14s"
    assert duration(3840) == "1h04m"
    assert duration(-1) == "0s"


def test_the_eta_says_nothing_until_it_has_a_rate():
    """A guess printed before the first item finishes is a number somebody
    plans around, wrong by the whole warm-up cost of the first call."""
    assert eta(0, 437, 0) == "?"
    assert eta(0, 437, 5.0) == "?"
    assert eta(100, 437, 134.0) == "7m31s"
    assert eta(437, 437, 1620.0) == "done"


def test_the_funnel_keeps_its_zeros():
    """`broke 0` on a round is the good news. Dropping a count because it is
    zero tells a reader the check did not run."""
    text = line("round", 50, 400, 900.0, counts={"fixed": 6, "broke": 0})
    assert "broke 0" in text
    assert "fixed 6" in text


def test_a_line_is_plain_ascii_and_carries_no_cursor_codes():
    """A long run is read from a log file with `tail -f`. A bar drawn with
    cursor movement writes one unreadable blob; a block character turns into
    mojibake in another locale and is read as nothing by a screen reader."""
    text = line("mine", 100, 437, 134.0, 1.21, {"clean": 61, "over-budget": 14})
    assert text.isascii(), text
    assert "\x1b" not in text and "\r" not in text
    for printed in text.split("\n"):
        assert printed == printed.rstrip(), "no trailing whitespace to read as an edit"


def test_a_line_carries_where_how_much_and_how_long_left():
    text = line("mine", 100, 437, 134.0, 1.21, {"clean": 61})
    assert "mine" in text
    assert "100/437" in text
    assert "2m14s" in text          # how long it has been running
    assert "eta 7m31s" in text      # how long is left
    assert "$1.21" in text          # what it has cost
    assert "clean 61" in text       # whether it is worth finishing


def test_nothing_is_said_between_the_throttle_points():
    """The stage never decides how often to speak, so no stage carries a
    counter or a clock."""
    ticks = iter([0.0] + [float(n) for n in range(1, 200)])
    p = Progress("mine", total=100, every=25, clock=lambda: next(ticks))
    said = [n for n in range(1, 101) if p.tick(n) is not None]
    assert said == [25, 50, 75, 100]


def test_the_last_line_is_always_said():
    """A run ending on item 437 of 437 must not stay silent because 437 is
    not a multiple of 25."""
    p = Progress("mine", total=437, every=25, clock=lambda: 0.0)
    assert p.tick(437) is None
    assert "437/437" in p.done(437)


def test_the_funnel_names_the_fix_not_the_defect():
    """The names a person reads are what recovers the row, because that is
    what they can act on. Checked through the miner's own classifier so the
    two cannot drift apart."""
    from context_lift.mine import Candidate, _funnel

    def candidate(status):
        return Candidate(id="x", question="q", gold_sql="SELECT 1", times_seen=1,
                         gate_status=status, gate_detail="", table="t",
                         accounts=1, out_of_scope="")

    counted = _funnel(
        [candidate("ok")] * 61
        + [candidate("ok (MISSING_LITERAL)")] * 4      # a warning is still usable
        + [candidate("OVER_BUDGET")] * 14
        + [candidate("NO_ACCESS")] * 8
        + [candidate("TABLE_GONE")] * 5
        + [candidate("WHOLE_ROW+CROSS_PROJECT")] * 7
        + [candidate("ZERO_ROWS")] * 2
        + [candidate("EXEC_FAIL")] * 3)

    assert counted == {"clean": 65, "over-budget": 14, "no-access": 8, "gone": 5,
                       "bad-sql": 3, "not-a-question": 7, "no-rows": 2}
    assert sum(counted.values()) == 104, "every candidate lands in exactly one bucket"


def test_the_funnel_shows_clean_even_at_zero_and_hides_what_did_not_happen():
    """`clean 0` is the message: nothing usable yet. `no-access 0` is noise,
    and there are seven of those classes, so a healthy estate would read four
    zeros for every real number. The stage decides, and this is the stage's
    decision; the printer itself keeps whatever it is handed."""
    from context_lift.mine import Candidate, _funnel

    def candidate(status):
        return Candidate(id="x", question="q", gold_sql="SELECT 1", times_seen=1,
                         gate_status=status, gate_detail="", table="t",
                         accounts=1, out_of_scope="")

    assert _funnel([]) == {"clean": 0}
    assert _funnel([candidate("ZERO_ROWS")]) == {"clean": 0, "no-rows": 1}
    assert "no-access" not in _funnel([candidate("ok")])


def test_a_slow_stage_still_says_where_it_is():
    """Counting items alone went silent for 13 minutes on a stage whose items
    cost about 30 seconds each: the throttle was every 25 items, so it spoke
    every 12.5 minutes, and that is exactly when a reader starts wondering
    whether the run has hung. Whichever comes first now speaks."""
    now = [0.0]
    p = Progress("mine", total=500, every=25, clock=lambda: now[0],
                 at_least_every_seconds=120)
    spoke = []
    for item in range(1, 30):
        now[0] = item * 30.0           # 30 seconds an item
        if p.tick(item):
            spoke.append(item)
    # every 2 minutes is every 4 items at this speed, so the first line lands
    # at item 4 rather than item 25
    assert spoke[0] == 4, spoke
    assert len(spoke) > 6, "a 14-minute stretch must not pass in silence"


def test_a_fast_stage_does_not_become_chatty():
    """The time clause must not fire on a stage that is already speaking.
    An extra line every 2 minutes on top of every 25 items is noise."""
    now = [0.0]
    p = Progress("mine", total=500, every=25, clock=lambda: now[0],
                 at_least_every_seconds=120)
    spoke = []
    for item in range(1, 101):
        now[0] = item * 0.5            # half a second an item
        if p.tick(item):
            spoke.append(item)
    assert spoke == [25, 50, 75, 100]


def test_the_time_clause_can_be_turned_off():
    """Zero means count only, so a caller that wants the old behaviour has
    it, and the test above cannot pass by accident."""
    now = [0.0]
    p = Progress("mine", total=500, every=25, clock=lambda: now[0],
                 at_least_every_seconds=0)
    spoke = [item for item in range(1, 30)
             if (now.__setitem__(0, item * 30.0), p.tick(item))[1]]
    assert spoke == [25]


# --- the heartbeat: silence has to be impossible -----------------------------

def test_a_hung_item_still_produces_lines():
    """The failure this exists to prevent, reproduced.

    `Progress` is asked the time only when the loop reaches its next item, so
    adding a time throttle to it produced a clock that is read only while
    things are already moving. One hung item meant six minutes fifteen
    seconds of silence on a real run, with AT_LEAST_EVERY_SECONDS = 120 in
    place and working exactly as written.
    """
    from context_lift.progress import Heartbeat, Progress
    said = []
    tracker = Progress("mine", total=150)
    beat = Heartbeat(tracker, say=said.append, every=0.02, stuck_after=0.0)
    beat.now_on(101, 150)
    beat.start()
    time.sleep(0.2)
    beat.stop()
    assert said, "a hung item produced no output, which is the whole bug"
    assert "101/150" in said[0], "it has to name the item it is stuck on"
    assert "stuck" in said[0]


def test_the_heartbeat_says_nothing_while_items_are_moving():
    """A stage whose items are slow by nature must not get a line each."""
    from context_lift.progress import Heartbeat, Progress
    said = []
    tracker = Progress("mine", total=150)
    beat = Heartbeat(tracker, say=said.append, every=0.02, stuck_after=10.0)
    beat.now_on(5, 150)
    beat.start()
    time.sleep(0.1)
    beat.stop()
    assert said == [], "an item under the stuck threshold is not news"


def test_the_heartbeat_line_lands_in_the_same_columns_as_a_progress_line():
    """Fixed-width fields exist so a reader follows a column down a log, and
    the heartbeat is the line they most need to compare with the one above."""
    from context_lift.progress import Heartbeat, Progress, line
    tracker = Progress("mine", total=150)
    beat = Heartbeat(tracker, say=lambda _: None, stuck_after=0.0)
    beat.now_on(101, 150)
    normal = line("mine", 100, 150, 233.0, usd=0.74)
    stuck = beat.line()
    assert stuck is not None
    # the stage name, the elapsed field and the bar all start in one place
    assert normal.index("[") == stuck.index("[")
    assert normal.index("/150") == stuck.index("/150")
    # and the bar marks WHERE it is waiting, so the line is about one item
    assert "?" in stuck and "?" not in normal


def test_the_heartbeat_thread_cannot_outlive_the_process():
    from context_lift.progress import Heartbeat, Progress
    beat = Heartbeat(Progress("mine", total=3), say=lambda _: None, every=0.01)
    beat.start()
    assert beat._thread is not None and beat._thread.daemon
    beat.stop()
    beat._thread.join(timeout=1.0)
    assert not beat._thread.is_alive(), "stop() must end the thread at once"


def test_the_heartbeat_carries_whatever_the_stage_hands_it():
    """The retry count, for a stage making network calls. This module must
    not know what a retry is, so the stage passes a function."""
    from context_lift.progress import Heartbeat, Progress
    beat = Heartbeat(Progress("mine", total=9), say=lambda _: None,
                     stuck_after=0.0, extra=lambda: "3 model retries (429), 62s waiting")
    beat.now_on(4, 9)
    assert "3 model retries (429), 62s waiting" in beat.line()


def test_waiting_bar_marks_the_position_and_never_reads_as_finished():
    from context_lift.progress import waiting_bar
    assert waiting_bar(0, 10) == "[?---------------]"
    assert waiting_bar(10, 10).count("#") == 15 and waiting_bar(10, 10).endswith("?]")
    assert all("?" in waiting_bar(n, 10) for n in range(11))
    assert waiting_bar(1, 0).startswith("[?")


# --- the live display, for a terminal only -----------------------------------

def test_the_funnel_bar_always_fills_exactly():
    """A bar that does not fill reads as a run still going. Largest-remainder
    allocation, because rounding each class on its own leaves it a character
    short or long."""
    from context_lift.progress import FUNNEL_WIDTH, funnel_bar
    cases = [
        {"clean": 27, "over-budget": 14, "no-access": 16, "gone": 18,
         "bad-sql": 3, "not-a-question": 19},
        {"clean": 1, "gone": 99},          # one class almost everything
        {"clean": 5},                      # one class only
        {},                                # nothing yet
        dict.fromkeys(["clean", "over-budget", "no-access", "gone",
                       "bad-sql", "no-rows", "not-a-question"], 1),
    ]
    for counts in cases:
        drawn = funnel_bar(counts)
        assert drawn.startswith("[") and drawn.endswith("]")
        assert len(drawn) - 2 == FUNNEL_WIDTH, f"{counts}: {drawn}"


def test_a_class_with_any_count_is_never_invisible():
    """A class rounded down to nothing would say the run had none of it, and
    on real data the small classes are the interesting ones."""
    from context_lift.progress import funnel_bar
    drawn = funnel_bar({"clean": 1, "gone": 999})
    assert "#" in drawn, "one clean answer key out of a thousand still shows"
    assert "x" in drawn


def test_the_key_says_what_fixes_each_class_not_just_its_name():
    """`no-access 16` is a defect class. `no-access 16  ask whoever owns the
    policy` is a next action, and that decides whether a reader kills the
    run or lets it finish."""
    from context_lift.progress import funnel_key
    lines = funnel_key({"clean": 27, "no-access": 16, "gone": 18})
    joined = "\n".join(lines)
    assert "ask whoever owns the policy" in joined
    assert "nothing to do" in joined
    # and a class that did not happen is not listed at all
    assert "bad-sql" not in joined
    assert len(lines) == 3


def test_the_live_display_is_never_what_goes_in_a_log():
    """Two renderers on purpose. The appended line is what a file gets,
    because a multi-line repainting display written to a file is one
    unreadable blob, and the log is what a stalled run is debugged from."""
    from context_lift.progress import line, screen
    counts = {"clean": 27, "gone": 18}
    logged = line("mine", 112, 150, 252.0, usd=0.81, counts=counts)
    watched = screen("mine", 112, 150, 252.0, usd=0.81, counts=counts,
                     last="net units by region [clean]")
    assert "\n" in watched and watched.count("\n") > logged.count("\n")
    # the log line stays one bar and one funnel row, as it always was
    assert logged.count("\n") == 1
    # the last-item line is only on the watched form: it is derived from
    # customer SQL, and a log file gets pasted into tickets
    assert "net units by region" in watched
    assert "net units by region" not in logged


def test_every_stage_that_shows_progress_also_starts_a_heartbeat():
    """The guard against the fault that caused this, not the fault itself.

    A 2026-09-11 change added a time floor so a slow stage could not go
    quiet, and a second one that day added a heartbeat thread so a HUNG item
    could not either. Both went into
    `_progress_printer`, which `mine` uses. `round` builds its own `Progress`
    inline, because its callback takes the arm label first, so it had neither
    -- and `round` is the longest and most expensive stage, where silence
    hurts most. Observed on a real run: the treatment arm went five minutes
    without a line and nothing said which question it was on.

    So the rule is structural: a line that builds a `Progress` must have a
    `Heartbeat` beside it. Checked by reading the source, because no
    behavioural test would notice a third stage being added without one.
    """
    import pathlib
    import re
    from context_lift import cli
    text = pathlib.Path(cli.__file__).read_text()
    lines = text.splitlines()
    unwatched = []
    for number, line in enumerate(lines):
        if not re.search(r"\bProgress\(", line):
            continue
        # the same block, generously: the construction and its heartbeat
        window = "\n".join(lines[max(0, number - 12):number + 14])
        if "Heartbeat(" not in window:
            unwatched.append(f"cli.py:{number + 1} {line.strip()[:70]}")
    assert not unwatched, (
        "these stages show progress with no heartbeat, so a hung item would "
        "produce silence:\n  " + "\n  ".join(unwatched))
