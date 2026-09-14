# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""One progress line, shared by every stage that makes hundreds of calls.

Four stages each make one network call per item and each used to print
nothing until it finished: `mine` (one model call and one warehouse query per
shape, 437 shapes and 25 minutes on one estate), `round` (two chats per
question, over 400 calls and about three hours), `generate`, and `gate`. The
only way to tell a slow run from a hung one was to watch the process list.

Four rules, each for a reason that was paid for.

**A new line, appended, never a repainted bar.** A long run is read from a
log file with `tail -f`, and a bar drawn with cursor-movement codes writes
one unreadable blob into a file. Every line here stands alone.

**ASCII only.** A log is opened in any editor under any locale. Block
characters turn into mojibake, and a screen reader reads them as nothing.

**The funnel, not only a percentage.** "100 of 437" says the run is alive.
"clean 61, over-budget 14, no-access 8" says whether it is worth finishing,
and that is the number somebody watching actually wants: it is a live answer
to "is my query log any good".

**Fixed-width fields.** Successive lines align, so a person reads a trend
down a column and a program can split on whitespace.

    p = Progress("mine", total=437)
    ...
    for index, item in enumerate(items):
        if line := p.tick(index, clean=n_clean, usd=spent):
            print(line, flush=True)
"""
from __future__ import annotations

import threading
import time

# How often a stage says where it is. Every 25 items is about a line a minute
# on a warehouse behind a proxy: often enough to see it moving, rare enough
# not to bury the summary that follows.
EVERY = 25

# And never less often than this, however slow the items are. Counting items
# alone went silent for 13 minutes on a stage whose items cost 30 seconds
# each -- exactly when a reader most wants a line, because that is when they
# start wondering whether it has hung. Measured on the run that found it: 25
# items took 13 minutes, so the count throttle fired every 13 minutes.
AT_LEAST_EVERY_SECONDS = 120.0

# How often the heartbeat thread speaks while ONE item is taking too long.
# Shorter than the line above, because this is the case where a reader is
# actively wondering whether to kill the run.
HEARTBEAT_SECONDS = 30.0

# An item that has taken longer than this is worth mentioning by itself.
# Below it, a stage with naturally slow items would print a heartbeat for
# every one of them and say nothing.
STUCK_AFTER_SECONDS = 45.0

BAR_WIDTH = 16


def bar(done: int, total: int, width: int = BAR_WIDTH) -> str:
    """`[#####-----------]`, ASCII, fixed width.

    A total of 0 draws an empty bar rather than dividing by zero: a stage with
    nothing to do still prints one line saying so.
    """
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = min(width, round(width * done / total))
    # 425 of 437 rounds to a full bar, which reads as finished, and somebody
    # reading a log walks away from a run with 12 items left. A full bar means
    # done, and nothing else does.
    if filled == width and done < total:
        filled = width - 1
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def waiting_bar(done: int, total: int, width: int = BAR_WIDTH) -> str:
    """The bar with a `?` where the stage is waiting: `[#####?----------]`.

    A `?` rather than a `#`, because the item is not finished, and rather
    than a `-`, because a reader following a column down the log needs to see
    that this line is about one particular item and not about the whole run.
    """
    if total <= 0:
        return "[" + "?" + "-" * (width - 1) + "]"
    at = min(width - 1, int(width * done / total))
    return "[" + "#" * at + "?" + "-" * (width - at - 1) + "]"


def duration(seconds: float) -> str:
    """`4s`, `2m14s`, `1h04m`. Never `0:02:14.318`, which is three decisions
    a reader has to make before learning anything."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def eta(done: int, total: int, elapsed: float) -> str:
    """How long is left, from the rate measured so far.

    "?" until there is a rate to measure. A guess printed before the first
    item is finished is a number somebody plans around, and it is wrong by
    whatever the first call's warm-up cost.
    """
    if total > 0 and done >= total:
        return "done"
    if done <= 0 or elapsed <= 0:
        return "?"
    return duration(elapsed / done * (total - done))


def line(stage: str, done: int, total: int, elapsed: float,
         usd: float | None = None, counts: dict[str, int] | None = None) -> str:
    """The progress line, and the funnel line under it when there is one.

    `counts` is the funnel: whatever the stage wants to show falling out, in
    the order it wants it. Printed under the bar and indented, so the bar
    stays one scannable line however many classes a stage has.
    """
    head = (f"{stage:<8} {duration(elapsed):>7}  {bar(done, total)}  "
            f"{done:>5}/{total:<5} eta {eta(done, total, elapsed):<6}")
    if usd is not None:
        head += f" ${usd:,.2f}"
    head = head.rstrip()
    if not counts:
        return head
    # Nothing is filtered out. A zero is often the whole message: `broke 0` on
    # a round is the good news, and dropping it because the number is zero
    # tells a reader the check did not run. The stage decides what to show.
    shown = "  ".join(f"{name} {value}" for name, value in counts.items())
    return head + "\n" + " " * 18 + shown


# The funnel drawn as one bar, when a person is watching. Each class gets a
# character, and the order is the order of the bar: what you wanted first,
# then what is recoverable, then what is not.
#
# The characters are chosen to read in the right ORDER of density even
# without the key beside them: solid, then light, then a cross, then empty.
FUNNEL_MARKS = (
    ("clean",          "#", "usable answer keys"),
    ("over-budget",    ":", "raise --max-bytes"),
    ("no-access",      ":", "ask whoever owns the policy"),
    ("gone",           "x", "table was dropped, nothing to do"),
    ("bad-sql",        "x", "the SQL itself is wrong"),
    ("no-rows",        "_", "runs and returns nothing to compare"),
    ("not-a-question", "_", "no single right answer"),
)

# The stacked bar's width. Wider than BAR_WIDTH because it carries seven
# classes rather than one position, and a class smaller than a character
# would vanish.
FUNNEL_WIDTH = 48


def funnel_bar(counts: dict[str, int], width: int = FUNNEL_WIDTH) -> str:
    """The funnel as one stacked bar: `[######::::::xxxx____]`.

    Proportional to the counts, so the picture carries the information
    instead of decorating it: one glance says "a quarter of my log is usable
    and most of the loss is permissions", which is the question somebody
    watching actually has.

    A class with a non-zero count always gets at least one character. A
    class rounded down to nothing would say the run had none of it, and on
    this data the small classes are the interesting ones.
    """
    known = [(name, mark) for name, mark, _why in FUNNEL_MARKS
             if counts.get(name)]
    total = sum(counts.get(name, 0) for name, _m in known)
    if not known or total <= 0:
        return "[" + "-" * width + "]"
    # Largest remainder, so the widths always sum to exactly `width`.
    # Rounding each class on its own leaves the bar a character short or
    # long, and a bar that does not fill reads as a run still going when it
    # has finished.
    exact = [(name, mark, width * counts[name] / total) for name, mark in known]
    widths = {name: max(1, int(value)) for name, _m, value in exact}
    spare = width - sum(widths.values())
    # Give the leftovers to whoever was rounded down hardest; take them back
    # the same way when rounding every class up to 1 has overflowed.
    order = sorted(exact, key=lambda item: -(item[2] - int(item[2])))
    while spare > 0 and order:
        for name, _m, _v in order:
            if spare <= 0:
                break
            widths[name] += 1
            spare -= 1
    while spare < 0:
        biggest = max(widths, key=lambda name: widths[name])
        if widths[biggest] <= 1:
            break
        widths[biggest] -= 1
        spare += 1
    marks = "".join(mark * widths[name] for name, mark in known)
    return "[" + marks[:width] + "-" * max(0, width - len(marks)) + "]"


def funnel_key(counts: dict[str, int]) -> list[str]:
    """One line per class present, saying its mark, count and what fixes it.

    The name alone is not enough. `no-access 16` beside a count is a defect
    class; `no-access 16   ask whoever owns the policy` is a next action, and
    that is the difference between a reader who stops the run and one who
    lets it finish.
    """
    lines = []
    for name, mark, why in FUNNEL_MARKS:
        count = counts.get(name)
        if not count:
            continue
        lines.append(f"   {mark * 6}  {name} {count}".ljust(34) + why)
    return lines


def screen(stage: str, done: int, total: int, elapsed: float,
           usd: float | None = None, counts: dict[str, int] | None = None,
           last: str = "") -> str:
    """The whole live display, for a terminal only.

    Never written to a file. This one uses width and blank lines freely
    because a person is looking at it; the appended-line form above is what
    a log gets, and the two must not be confused. See the module docstring.
    """
    head = (f"{stage:<6} {duration(elapsed):>7}   {done:>5}/{total:<5}  "
            f"eta {eta(done, total, elapsed):<7}")
    if usd is not None:
        head += f"  ${usd:,.2f}"
    out = [head.rstrip(), ""]
    if counts:
        out.append("  " + funnel_bar(counts))
        out.append("")
        out += funnel_key(counts)
    if last:
        out += ["", f"  last: {last}"]
    return "\n".join(out)


class Heartbeat:
    """Says where a stage is even when the stage cannot.

    The reason this exists, and it took two ruined runs to see it. `Progress`
    is asked the time only when the loop reaches its next item. Add a clock
    to that and you get a clock that is read only while things are already
    moving: if ONE item hangs, the loop never comes back, the time check is
    never evaluated, and the output is silent for as long as the hang lasts.
    Measured on the run that found it: six minutes fifteen seconds of
    nothing, with `AT_LEAST_EVERY_SECONDS = 120` already in place and
    working exactly as written.

    So the heartbeat runs on its own thread and does not ask the loop for
    permission. It prints the one sentence that diagnoses a hang -- which
    item, and how long it has been on that item -- plus whatever the stage
    hands it, which for a stage making network calls is the retry count.

    A daemon thread, so it can never keep a finished process alive. It says
    nothing until an item has been running longer than STUCK_AFTER_SECONDS,
    because a stage whose items are slow by nature does not need a line per
    item.

        beat = Heartbeat("mine", say=print)
        beat.start()
        for index, item in enumerate(items):
            beat.now_on(index, len(items))
            ...
        beat.stop()
    """

    def __init__(self, progress: "Progress", say,
                 every: float = HEARTBEAT_SECONDS,
                 stuck_after: float = STUCK_AFTER_SECONDS,
                 clock=time.monotonic, extra=None):
        # Shares the stage's Progress so its line lands in the same columns.
        # Successive lines aligning is the whole reason the widths are fixed,
        # and a heartbeat in different columns is the one line a reader most
        # needs to compare with the one above it.
        self.progress = progress
        self.stage = progress.stage
        self.say = say
        self.every = every
        self.stuck_after = stuck_after
        self._clock = clock
        # `extra` is called with no arguments and returns a string to append,
        # or "". The caller owns it because this module must not know what a
        # retry is, or what a dollar is.
        self._extra = extra
        self._lock = threading.Lock()
        self._item = (0, 0)
        self._item_started = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def now_on(self, done: int, total: int) -> None:
        """Called at the top of each item. Resets the per-item clock."""
        with self._lock:
            self._item = (done, total)
            self._item_started = self._clock()

    def line(self) -> str | None:
        """The heartbeat line, or None when the current item is not slow.

        Same columns as a progress line, with two differences a reader sees
        at a glance: the bar carries a `?` at the position being waited on,
        and where the eta would be it says how long this one item has taken.
        An eta is meaningless here -- the rate it would be computed from has
        stopped.
        """
        with self._lock:
            done, total = self._item
            waited = self._clock() - self._item_started
        if waited < self.stuck_after:
            return None
        text = (f"{self.stage:<8} {duration(self.progress.elapsed):>7}  "
                f"{waiting_bar(done, total)}  {done:>5}/{total:<5} "
                f"stuck {duration(waited):<6}")
        if self._extra is not None:
            more = self._extra() or ""
            if more:
                text += f" {more}"
        return text.rstrip()

    def start(self) -> "Heartbeat":
        if self.every <= 0:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"{self.stage}-heartbeat")
        self._thread.start()
        return self

    def _run(self) -> None:
        # `wait` rather than `sleep`, so stop() ends the thread at once
        # instead of after up to `every` seconds.
        while not self._stop.wait(self.every):
            text = self.line()
            if text:
                self.say(text)

    def stop(self) -> None:
        self._stop.set()

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


class Progress:
    """A stage's own progress, throttled, with the clock started on creation.

    `tick` returns the line to print, or None when it is not time yet, so a
    caller never decides how often to speak. It returns the string rather
    than printing it: one stage writes to stdout, another to a recorder, and
    a test wants neither.
    """

    def __init__(self, stage: str, total: int, every: int = EVERY,
                 clock=time.monotonic,
                 at_least_every_seconds: float = AT_LEAST_EVERY_SECONDS):
        self.stage = stage
        self.total = total
        self.every = max(1, every)
        self.at_least_every_seconds = at_least_every_seconds
        self._clock = clock
        self._started = clock()
        self._last_said = self._started

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    def tick(self, done: int, usd: float | None = None,
             counts: dict[str, int] | None = None) -> str | None:
        if done <= 0:
            return None
        # Either enough items have passed, or enough time has. The second
        # clause is what keeps a slow stage from going silent: whichever
        # comes first speaks, and both reset the clock.
        by_count = done % self.every == 0
        by_time = (self.at_least_every_seconds > 0
                   and self._clock() - self._last_said >= self.at_least_every_seconds)
        if not (by_count or by_time):
            return None
        self._last_said = self._clock()
        return line(self.stage, done, self.total, self.elapsed, usd, counts)

    def done(self, done: int, usd: float | None = None,
             counts: dict[str, int] | None = None) -> str:
        self._last_said = self._clock()
        """The last line, printed whatever the count. A run that ends on item
        437 of 437 should not stay silent because 437 is not a multiple of
        25."""
        return line(self.stage, done, self.total, self.elapsed, usd, counts)
