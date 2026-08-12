"""The bench override says what it is doing, and keeps the half that still applies.

A development tool, tested because its whole job is to weaken a production guard
and the failure mode of getting that wrong is a bench run that proves the TIME?
path works when it does not.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manta_link import clock  # noqa: E402
from tools import bench_host  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    """Each test gets its own, or the first one silences the rest."""
    monkeypatch.setattr(
        bench_host, "_disclaimer", bench_host.Throttle(bench_host.DISCLAIMER_INTERVAL_S)
    )


def test_a_plausible_bench_clock_is_answered_from(monkeypatch):
    monkeypatch.setattr(bench_host.time, "time", lambda: 1_800_000_000.0)

    assert bench_host.trust_this_bench_clock() is True


@pytest.mark.parametrize("now", [0.0, 1.0, 1_700_000_000.0, 5_000_000_000.0])
def test_a_clock_the_firmware_would_reject_is_still_refused(monkeypatch, now):
    """The one half of the real guard that is not platform specific.

    The Pico enforces the same range and rejects a bad reply with no diagnostic
    on either side, so answering anyway costs the run its timestamps for a
    reason nobody can see.
    """
    monkeypatch.setattr(bench_host.time, "time", lambda: now)

    assert bench_host.trust_this_bench_clock() is False


def test_the_disclaimer_is_logged_every_time_it_is_consulted_not_once(
    monkeypatch, caplog
):
    """A line at startup scrolls away.

    The question a reader has later is not "was this started in bench mode" but
    "was the answer the Pico actually took a real one", and only a line beside
    the answer settles that.
    """
    monkeypatch.setattr(bench_host.time, "time", lambda: 1_800_000_000.0)
    monkeypatch.setattr(bench_host, "_disclaimer", bench_host.Throttle(0.0))

    with caplog.at_level("WARNING"):
        for _ in range(3):
            bench_host.trust_this_bench_clock()

    assert caplog.text.count("bench mode") == 3


def test_the_disclaimer_is_throttled_rather_than_printed_per_read(
    monkeypatch, caplog
):
    monkeypatch.setattr(bench_host.time, "time", lambda: 1_800_000_000.0)

    with caplog.at_level("WARNING"):
        for _ in range(50):
            bench_host.trust_this_bench_clock()

    assert caplog.text.count("bench mode") == 1


def test_main_replaces_the_real_check_and_starts_the_process(monkeypatch):
    real = clock.clock_is_trustworthy
    started: list[object] = []

    def record_start(argv):
        started.append(argv)
        return 0

    monkeypatch.setattr(bench_host.entry, "main", record_start)

    try:
        assert bench_host.main([]) == 0
        assert clock.clock_is_trustworthy is bench_host.trust_this_bench_clock
        assert started == [[]]
    finally:
        clock.clock_is_trustworthy = real
