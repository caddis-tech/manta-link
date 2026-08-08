"""The whole arrangement under the load it was arranged for.

Nothing here stubs the thing being tested: a real reader, real deques, a real
capture worker and a real health worker, with the slow parts made slow on
purpose. The claim under test is the one the process exists to keep, which is
that a TIME? is answered no matter what the rest of the process is doing.

The Pico asks only during its boot window and stops on the first accepted
answer, so a late reply is a lost run, not a slow one.
"""

import logging
import threading
import time

import pytest

from manta_link import clock, logging_setup
from manta_link import reader as reader_mod
from manta_link.capture import (
    RECORD_BUFFER_MAX,
    CaptureWorker,
    new_log_buffer,
    new_record_buffer,
)
from manta_link.health import Counters, Health
from manta_link.reader import SerialReader

from .fakes import FakeSerial, StopPlayback, wait_until
from .test_framing import BANNER
from .test_logging_setup import StallingStream

EPOCH_MS = 1_754_400_000_000

# The firmware's poll interval is seconds and the USB round trip is
# sub-millisecond, so this is two orders of magnitude of headroom. It is set
# where a human would notice, not where the hardware would.
REPLY_BUDGET_S = 0.05

# What the spool write and the upload POST cost when they are going badly. Both
# are still ahead of us; the point is that neither can ever be on this path.
SLOW_SPOOL_S = 2.0
SLOW_POST_S = 30.0


@pytest.fixture
def trusted_clock(monkeypatch):
    monkeypatch.setattr(clock, "clock_is_trustworthy", lambda: True)
    monkeypatch.setattr(clock, "epoch_ms_now", lambda: EPOCH_MS)


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def numbered_records(count: int) -> bytes:
    """A back-to-back burst, each line telling you which one it is."""
    return b"".join(b'{"type":"reading","n":%d}\n' % index for index in range(count))


def reply_delay(factory, started: float) -> float:
    """How long after the reader started reading the reply was written."""
    assert factory.instance.written, "no reply was written at all"
    return factory.instance.written_at[0] - started


def serve(monkeypatch, factory, reader: SerialReader) -> None:
    monkeypatch.setattr(reader_mod.serial, "Serial", factory)
    with pytest.raises(StopPlayback):
        reader._serve("/dev/fake")


class TestUnderFullLoad:
    def test_time_request_answered_under_full_load(self, monkeypatch, trusted_clock):
        """300 records land while every consumer of them is asleep.

        The capture worker is stuck in a two-second spool write and the uploader
        in a thirty-second POST, which between them is the worst steady state
        this design has. The reader shares no lock with either: it appends to a
        deque, which drops the oldest at maxlen rather than waiting for room.
        """
        counters = Counters()
        records = new_record_buffer()
        logs = new_log_buffer()

        worker = CaptureWorker(
            records, logs, counters, sink=lambda rec, at: time.sleep(SLOW_SPOOL_S)
        )
        health = Health(counters)
        health.register("capture", worker.run_forever)
        health.register("uploader", lambda: time.sleep(SLOW_POST_S))
        health.start()

        factory = FakeSerial.factory(
            chunks=[numbered_records(300), b"TIME?\n"]
        )
        reader = SerialReader(
            records=records, logs=logs, on_tick=health.check_from_main_thread
        )

        started = time.monotonic()
        serve(monkeypatch, factory, reader)

        assert reader.answered_count == 1
        assert reply_delay(factory, started) < REPLY_BUDGET_S
        # The premise, not the claim: with a two-second sink the worker cannot
        # have drained more than one, so the contention was real.
        assert len(records) >= RECORD_BUFFER_MAX - 1

    def test_time_request_answered_while_a_worker_logs_into_a_stalled_stdout(
        self, monkeypatch, trusted_clock, restore_root_logging
    ):
        """The specific path the QueueHandler exists to break.

        A spool write fails on a full disk, so a worker logs an error every
        cycle. PYTHONUNBUFFERED makes each line an immediate write to the stdout
        pipe, Docker's log delivery is blocking, and Kraken overwrites the
        manifest's LogConfig so there is no way to ask for anything else. Handed
        a plain StreamHandler, that worker blocks inside emit while holding the
        lock every other thread needs to log, and the reader stops reading.
        """
        stream = StallingStream()
        sink = logging_setup.configure(stream=stream, queue_max=8)
        stop = threading.Event()
        noisy = threading.Thread(
            target=flood_with_spool_failures, args=(stop,), daemon=True
        )

        try:
            noisy.start()
            assert stream.entered.wait(2.0), "the drain thread never wrote"
            assert wait_until(lambda: sink.dropped > 0), "the queue never filled"

            factory = FakeSerial.factory(chunks=[b"TIME?\n"])
            reader = SerialReader(records=new_record_buffer())

            started = time.monotonic()
            serve(monkeypatch, factory, reader)

            assert reader.answered_count == 1
            assert reply_delay(factory, started) < REPLY_BUDGET_S
            # Dropping is the mechanism, not a side effect: a blocked worker
            # would have taken the reader down with it.
            assert sink.dropped > 0
        finally:
            stop.set()
            stream.released.set()
            noisy.join(2.0)

    def test_the_buffer_drops_the_oldest_rather_than_blocking(
        self, monkeypatch, trusted_clock
    ):
        """Over capacity, the reader loses history and keeps the port.

        Which is the right way round: an old record is worth less than the
        reply, and the reply cannot be had twice.
        """
        records = new_record_buffer()
        overflow = RECORD_BUFFER_MAX + 44

        factory = FakeSerial.factory(
            chunks=[numbered_records(overflow), b"TIME?\n"]
        )
        reader = SerialReader(records=records)
        serve(monkeypatch, factory, reader)

        assert len(records) == RECORD_BUFFER_MAX
        oldest, _ = records[0]
        newest, _ = records[-1]
        assert oldest.endswith(b'"n":44}'), "the oldest 44 should have gone"
        assert newest.endswith(b'"n":%d}' % (overflow - 1))
        assert reader.answered_count == 1


class TestReleaseBoat:
    def test_release_stream_produces_no_uploads(self, monkeypatch, trusted_clock):
        """The fleet as it stands: banner, one request, and log lines.

        A Release image writes records to the card and puts none on USB, so an
        idle capture path is the correct outcome rather than a suspicious one.
        The heartbeat is what proves the process is alive, which is why it has
        to keep firing when nothing else does.
        """
        counters = Counters()
        records = new_record_buffer()
        logs = new_log_buffer()
        uploaded = []

        worker = CaptureWorker(records, logs, counters, sink=uploaded.append)
        health = Health(counters, heartbeat_interval_s=0.0)

        program = (
            BANNER + b"\r\n"
            + b"TIME?\n"
            + b"Boot time synced: epoch 1754400000000 ms at 4231 ms uptime\r\n"
            + b"SD card initialized\r\n"
            + b"File opened: data3.txt\r\n"
        )
        factory = FakeSerial.factory(chunks=[program])
        reader = SerialReader(records=records, logs=logs)
        serve(monkeypatch, factory, reader)

        worker.drain_once()
        health.tick()

        assert reader.answered_count == 1
        assert factory.instance.written == [f"TIME {EPOCH_MS}\n".encode()]
        assert uploaded == []
        assert counters.get("records_captured") == 0
        assert counters.get("pico_log_lines") == 3
        assert counters.get("heartbeats") == 1

    def test_a_release_boat_still_beats_with_nothing_on_the_port(self):
        """No Pico, no records, no reason to think anything is wrong."""
        counters = Counters()
        health = Health(counters, heartbeat_interval_s=0.0)
        health.tick()
        health.tick()
        assert counters.get("heartbeats") == 2


def flood_with_spool_failures(stop: threading.Event) -> None:
    """A worker in the failure that starts the whole chain: a full disk."""
    worker_log = logging.getLogger("manta_link.fake_worker")
    while not stop.is_set():
        try:
            raise OSError(28, "No space left on device")
        except OSError:
            worker_log.exception("spool write failed")
        # Paced like the real thing, which logs this once per cycle rather
        # than as fast as the CPU allows.
        stop.wait(0.001)
