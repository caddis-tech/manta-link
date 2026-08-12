"""The reader must survive things its own handlers do not name."""

import pytest

from manta_link import supervisor


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)


def run_until(fn, calls: int):
    """Run the supervisor until fn has been entered `calls` times."""
    counter = {"n": 0}

    def wrapped():
        counter["n"] += 1
        if counter["n"] >= calls:
            raise SystemExit(0)
        fn()

    with pytest.raises(SystemExit):
        supervisor.run_forever("test", wrapped)
    return counter["n"]


class TestRestarts:
    def test_restarts_after_an_ordinary_exception(self):
        def boom():
            raise RuntimeError("nope")

        assert run_until(boom, 3) == 3

    def test_restarts_after_a_bare_exception_subclass(self):
        """termios.error derives from Exception, not OSError.

        CPython builds it with a NULL base, and pyserial leaves tcsetattr and
        tcdrain unwrapped, so unplugging the Pico between resolving the port and
        opening it raises something a two-class except tuple would not catch.
        """

        class TermiosLike(Exception):
            pass

        def boom():
            raise TermiosLike("(5, 'Input/output error')")

        assert run_until(boom, 3) == 3

    def test_restarts_after_memory_error(self):
        def boom():
            raise MemoryError()

        assert run_until(boom, 2) == 2

    def test_restarts_when_the_loop_returns_normally(self):
        # A function that is itself a loop should never return, so returning is
        # already a failure.
        assert run_until(lambda: None, 3) == 3


class TestShutdown:
    def test_system_exit_is_not_caught(self):
        def stop():
            raise SystemExit(0)

        with pytest.raises(SystemExit):
            supervisor.run_forever("test", stop)

    def test_keyboard_interrupt_does_not_wedge_the_loop(self):
        # KeyboardInterrupt is a BaseException and so is caught and restarted;
        # the signal handler converts SIGINT to SystemExit before this matters.
        calls = {"n": 0}

        def once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt()
            raise SystemExit(0)

        with pytest.raises(SystemExit):
            supervisor.run_forever("test", once)
        assert calls["n"] == 2


class TestBackoff:
    def test_backoff_grows_then_caps(self, monkeypatch):
        slept = []
        monkeypatch.setattr(supervisor.time, "sleep", slept.append)

        def boom():
            raise RuntimeError("nope")

        counter = {"n": 0}

        def wrapped():
            counter["n"] += 1
            if counter["n"] > 10:
                raise SystemExit(0)
            boom()

        with pytest.raises(SystemExit):
            supervisor.run_forever("test", wrapped)

        assert slept[0] == supervisor.BACKOFF_START_S
        assert slept[1] > slept[0]
        assert max(slept) <= supervisor.BACKOFF_MAX_S

    def test_on_restart_hook_is_called_with_the_name(self):
        names = []
        counter = {"n": 0}

        def wrapped():
            counter["n"] += 1
            if counter["n"] >= 3:
                raise SystemExit(0)
            raise RuntimeError("nope")

        with pytest.raises(SystemExit):
            supervisor.run_forever("reader", wrapped, on_restart=names.append)
        assert names == ["reader", "reader"]
