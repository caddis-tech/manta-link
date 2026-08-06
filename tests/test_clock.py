"""The clock gate: refusing to answer is a feature, so test the refusals."""

import pytest

from manta_link import clock


class FakeLibc:
    """Stands in for libc, returning a chosen adjtimex state."""

    def __init__(self, state: int, errno: int = 0) -> None:
        self.state = state
        self.errno = errno
        self.calls = 0

    def adjtimex(self, _buf) -> int:
        self.calls += 1
        import ctypes

        ctypes.set_errno(self.errno)
        return self.state


SYNCED = 0  # TIME_OK
GOOD_TIME = clock.MIN_PLAUSIBLE_EPOCH_S + 86_400


@pytest.fixture
def libc(monkeypatch):
    def install(state: int, errno: int = 0) -> FakeLibc:
        fake = FakeLibc(state, errno)
        monkeypatch.setattr(clock, "_libc", fake)
        return fake

    return install


@pytest.fixture
def wall(monkeypatch):
    def install(seconds: float) -> None:
        monkeypatch.setattr(clock.time, "time", lambda: seconds)

    return install


class TestTrustworthy:
    def test_synced_and_plausible_is_trusted(self, libc, wall):
        libc(SYNCED)
        wall(GOOD_TIME)
        assert clock.clock_is_trustworthy() is True

    def test_unsynced_kernel_refuses(self, libc, wall):
        libc(clock.TIME_ERROR)
        wall(GOOD_TIME)
        assert clock.clock_is_trustworthy() is False

    def test_adjtimex_failure_refuses(self, libc, wall):
        libc(-1, errno=1)
        wall(GOOD_TIME)
        assert clock.clock_is_trustworthy() is False

    def test_no_libc_refuses(self, monkeypatch):
        monkeypatch.setattr(clock, "_libc", None)
        assert clock.clock_is_trustworthy() is False


class TestPlausibilityBounds:
    """Belt and braces for a daemon that never clears STA_UNSYNC."""

    def test_a_1970_clock_is_refused_even_when_the_kernel_says_synced(
        self, libc, wall
    ):
        libc(SYNCED)
        wall(0)
        assert clock.clock_is_trustworthy() is False

    def test_the_lower_bound_is_inclusive(self, libc, wall):
        libc(SYNCED)
        wall(clock.MIN_PLAUSIBLE_EPOCH_S)
        assert clock.clock_is_trustworthy() is True

    def test_a_clock_past_2100_is_refused(self, libc, wall):
        # The firmware's BOOT_TIME_MAX_EPOCH_MS rejects this silently, costing
        # the run its timestamps with no diagnostic. Refusing here says why.
        libc(SYNCED)
        wall(clock.MAX_PLAUSIBLE_EPOCH_S + 1)
        assert clock.clock_is_trustworthy() is False


class TestEpochMs:
    def test_is_milliseconds_not_seconds(self, monkeypatch):
        monkeypatch.setattr(clock.time, "time_ns", lambda: 1_754_400_000_123_456_789)
        assert clock.epoch_ms_now() == 1_754_400_000_123

    def test_lands_inside_the_firmware_accepted_range(self, monkeypatch):
        monkeypatch.setattr(
            clock.time, "time_ns", lambda: GOOD_TIME * 1_000_000_000
        )
        value = clock.epoch_ms_now()
        assert clock.MIN_PLAUSIBLE_EPOCH_S * 1000 <= value
        assert value < clock.MAX_PLAUSIBLE_EPOCH_S * 1000


def test_libc_handle_is_resolved_once_at_import(libc, wall):
    """The deployed version called find_library per request, forking ldconfig."""
    fake = libc(SYNCED)
    wall(GOOD_TIME)
    for _ in range(5):
        clock.clock_is_trustworthy()
    assert fake.calls == 5  # five adjtimex calls, one handle
