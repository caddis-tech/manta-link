"""Logging must never be able to stop the reader, so test it as a hazard."""

import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from manta_link import __main__, logging_setup
from manta_link.logging_setup import Throttle

from .fakes import wait_until

DROP_REPORT = re.compile(r"dropped (\d+) log line")

# The deadlock is at interpreter exit, which no in-process test can reach, so
# the wedged case runs as a program of its own and is judged by its exit.
WEDGED_EXIT_PROGRAM = """
import logging
import threading

from manta_link import logging_setup


class WedgedStream:
    def __init__(self):
        self.entered = threading.Event()

    def write(self, text):
        self.entered.set()
        threading.Event().wait()
        return len(text)

    def flush(self):
        pass


stream = WedgedStream()
sink = logging_setup.configure(stream=stream)
logging.getLogger("manta_link.test").info("the line the drain is now stuck on")
assert stream.entered.wait(5.0), "the drain never reached the stream"
assert sink.stop(timeout_s=0.2) is False, "a wedged drain cannot stop cleanly"
"""


class StallingStream:
    """A stdout that has stopped draining.

    Which is what Docker's blocking log delivery looks like from in here: the
    write never returns, and Kraken overwrites the manifest's LogConfig, so
    there is no way to ask for non-blocking delivery from outside.
    """

    def __init__(self) -> None:
        self.released = threading.Event()
        self.entered = threading.Event()
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        self.entered.set()
        self.released.wait()
        return len(text)

    def flush(self) -> None:
        pass


class CollectingStream:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> int:
        self.lines.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class StallingThenCollectingStream(StallingStream):
    """Stalls like the one above, and keeps whatever arrives once it is freed."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def write(self, text: str) -> int:
        written = super().write(text)
        self.lines.append(text)
        return written


class SlowStream(CollectingStream):
    """A stdout slow enough that a queue nobody drained is visible.

    In the container the process is gone the moment main returns; in here the
    daemon drain would win that race and hide the missing drain entirely.
    """

    def write(self, text: str) -> int:
        time.sleep(0.02)
        return super().write(text)


def stub_out_the_hardware(monkeypatch, run_forever) -> None:
    """Leave main's logging and shutdown path real, and nothing else.

    Both stubs take *_, **__ rather than the exact signature they replace. A
    stub pinned to today's parameters turns every later argument into a
    TypeError raised from this file, which reports a signature change as a
    logging failure in the one suite whose subject is neither.
    """
    monkeypatch.setattr(__main__, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(
        __main__,
        "build_recorder",
        lambda *_, **__: SimpleNamespace(
            capture=lambda *_, **__: None,
            anchor=SimpleNamespace(
                note_serial_reconnect=lambda: None, note_banner=lambda: None
            ),
        ),
    )
    monkeypatch.setattr(
        __main__,
        "Health",
        lambda *_, **__: SimpleNamespace(
            register=lambda *_, **__: None,
            start=lambda: None,
            check_from_main_thread=lambda: None,
            note_restart=lambda *_, **__: None,
        ),
    )
    monkeypatch.setattr(__main__.supervisor, "run_forever", run_forever)


@pytest.fixture
def restore_root_logging():
    """configure() replaces the root handlers, so put them back afterwards."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


class TestThrottle:
    def test_the_first_message_always_goes_out(self):
        assert Throttle(60.0).should_emit(now=1000.0) is True

    def test_a_repeat_inside_the_interval_is_suppressed(self):
        throttle = Throttle(60.0)
        throttle.should_emit(now=1000.0)
        assert throttle.should_emit(now=1030.0) is False

    def test_a_repeat_after_the_interval_goes_out(self):
        throttle = Throttle(60.0)
        throttle.should_emit(now=1000.0)
        assert throttle.should_emit(now=1060.0) is True

    def test_the_suppressed_count_is_reported_then_reset(self):
        throttle = Throttle(60.0)
        throttle.should_emit(now=1000.0)
        for tick in range(5):
            throttle.should_emit(now=1001.0 + tick)
        assert throttle.take_suppressed() == 5
        assert throttle.take_suppressed() == 0


class TestQueueHandler:
    def test_lines_reach_the_stream_through_the_drain_thread(
        self, restore_root_logging
    ):
        stream = CollectingStream()
        sink = logging_setup.configure(stream=stream)
        logging.getLogger("manta_link.test").info("hello %d", 1)
        assert wait_until(lambda: stream.lines)
        assert "hello 1" in stream.lines[0]
        assert sink.dropped == 0

    def test_a_traceback_survives_the_queue(self, restore_root_logging):
        stream = CollectingStream()
        logging_setup.configure(stream=stream)
        try:
            raise OSError(28, "No space left on device")
        except OSError:
            logging.getLogger("manta_link.test").exception("spool write failed")

        assert wait_until(lambda: stream.lines)
        assert "No space left on device" in stream.lines[0]

    def test_a_full_queue_drops_rather_than_blocking(self, restore_root_logging):
        """The whole reason this exists.

        With the drain wedged on a stalled stdout, a caller must return in
        microseconds and lose the line, not wait for room that never comes.
        """
        stream = StallingStream()
        sink = logging_setup.configure(stream=stream, queue_max=4)
        noisy = logging.getLogger("manta_link.test")
        try:
            noisy.info("the line the drain thread is now stuck on")
            assert stream.entered.wait(2.0)
            for _ in range(100):
                noisy.info("filling")
            assert sink.dropped > 0
        finally:
            stream.released.set()

    def test_the_only_thread_that_writes_the_stream_is_the_drain(
        self, restore_root_logging
    ):
        writers = set()

        class NamingStream(CollectingStream):
            def write(self, text: str) -> int:
                writers.add(threading.current_thread().name)
                return super().write(text)

        stream = NamingStream()
        logging_setup.configure(stream=stream)
        threads = [
            threading.Thread(
                target=logging.getLogger("manta_link.test").info,
                args=("from %s", index),
                name=f"worker-{index}",
            )
            for index in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert wait_until(lambda: len(stream.lines) == 4)
        assert writers == {"logsink"}


class TestDropReport:
    def test_a_report_lost_to_the_full_queue_keeps_its_whole_count(
        self, monkeypatch, restore_root_logging
    ):
        """The report is written the moment the queue is provably full.

        Losing it must not consume the count as though it had been read, or the
        one line that eventually arrives says 1 while hundreds are gone.
        """
        monkeypatch.setattr(logging_setup, "DROP_REPORT_INTERVAL_S", 0.0)
        stream = StallingThenCollectingStream()
        sink = logging_setup.configure(stream=stream, queue_max=4)
        noisy = logging.getLogger("manta_link.test")
        try:
            noisy.info("the line the drain thread is now stuck on")
            assert stream.entered.wait(2.0)
            for _ in range(50):
                noisy.info("filling")
            assert sink.dropped > 1
        finally:
            stream.released.set()

        assert wait_until(
            lambda: any(DROP_REPORT.search(line) for line in stream.lines)
        )
        sink.stop()
        counts = [int(m.group(1)) for m in map(DROP_REPORT.search, stream.lines) if m]
        assert max(counts) > 1
        assert sum(counts) == sink.dropped


class TestShutdown:
    def test_a_clean_stop_puts_its_last_words_on_the_stream(
        self, monkeypatch, restore_root_logging
    ):
        """SIGTERM's own lines are logged after the last drain would have run.

        Only an explicit drain delivers them: the sink thread is a daemon, and
        QueueHandler.flush does nothing, so logging.shutdown cannot either.
        """
        stream = SlowStream()
        monkeypatch.setattr(sys, "stdout", stream)

        def terminate_after_a_backlog(name, run, on_restart):
            backlog = logging.getLogger("manta_link.test")
            for index in range(30):
                backlog.info("line %d from before the signal", index)
            __main__._on_terminate(signal.SIGTERM, None)

        stub_out_the_hardware(monkeypatch, terminate_after_a_backlog)

        # Explicit argv, or main() parses pytest's own command line.
        assert __main__.main([]) == 0
        written = "".join(stream.lines)
        assert f"signal {int(signal.SIGTERM)} received" in written
        assert "stopped after answering 0 request(s)" in written

    def test_a_wedged_stream_does_not_hold_the_process_open(self):
        """logging.shutdown's atexit pass blocks on the lock the drain holds.

        No handler or timeout covers that wait, so Kraken waits out its stop
        timeout and SIGKILLs: exactly what the signal handlers exist to end.
        """
        repo_root = Path(__file__).resolve().parents[1]
        finished = subprocess.run(
            [sys.executable, "-c", WEDGED_EXIT_PROGRAM],
            cwd=repo_root,
            env={**os.environ, "PYTHONPATH": str(repo_root)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert finished.returncode == 0, finished.stderr
