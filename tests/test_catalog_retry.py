# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The retry that stops one quota error ending a long push.

Exercised against fakes, because waiting for a real 429 is not a test and
sleeping seven real seconds per case is not either.
"""
import pytest


class Quota(Exception):
    pass


class Denied(Exception):
    pass


class Unavailable(Exception):
    pass


class FakeErrors:
    ResourceExhausted = Quota
    ServiceUnavailable = Unavailable
    DeadlineExceeded = TimeoutError
    PermissionDenied = Denied


class Transport:
    """Only the parts `_retry` touches, so no cloud and no imports."""

    BACKOFF = (1.0, 2.0, 4.0)

    def __init__(self):
        self._errors = FakeErrors
        self.waits = []
        self._sleep = self.waits.append

    from context_lift.catalog import DataplexTransport as _real
    _retry = _real._retry


def failing(times, error=Quota("quota exceeded")):
    """A call that fails `times` times and then returns."""
    state = {"n": 0}

    def call():
        state["n"] += 1
        if state["n"] <= times:
            raise error
        return "ok"
    return call


def test_a_quota_error_is_waited_out_not_reported():
    """A per-minute quota clears within the minute. Ending a 300-table push
    on the first 429 leaves half the entries written, so a re-run is not a
    clean re-run."""
    transport = Transport()

    assert transport._retry("writing", failing(2)) == "ok"
    assert transport.waits == [1.0, 2.0]


def test_the_waits_widen_and_stay_short():
    """Short on purpose: a run that waits four minutes to discover a quota
    problem has helped nobody."""
    transport = Transport()

    with pytest.raises(SystemExit) as gave_up:
        transport._retry("writing", failing(99))

    assert transport.waits == [1.0, 2.0, 4.0]
    assert "4 times over 7s" in str(gave_up.value)


def test_it_says_this_is_not_a_permission_problem():
    """The two failures look alike from the outside and need opposite
    answers: wait, or grant a role."""
    transport = Transport()

    with pytest.raises(SystemExit) as gave_up:
        transport._retry("writing", failing(99))

    assert "not a permission problem" in str(gave_up.value)
    assert "safe to repeat" in str(gave_up.value)


def test_a_permission_error_is_raised_at_once():
    """Waiting never fixes a missing role, and retrying turns a clear error
    into a slow one: seven seconds per table instead of none."""
    transport = Transport()

    with pytest.raises(Denied):
        transport._retry("writing", failing(99, Denied("no role")))

    assert transport.waits == []


def test_an_outage_is_retried_like_a_quota_error():
    transport = Transport()

    assert transport._retry("reading", failing(1, Unavailable("503"))) == "ok"
    assert transport.waits == [1.0]
