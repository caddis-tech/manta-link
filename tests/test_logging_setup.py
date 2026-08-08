"""Logging must never be able to stop the reader, so test it as a hazard."""

import logging
import threading

import pytest

from manta_link import logging_setup
from manta_link.logging_setup import Throttle

from .fakes import wait_until


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
