"""Capture does the work the reader refuses to do, and says little about it."""

import threading
import time
from collections import deque

import pytest

from manta_link.capture import (
    RECORD_BUFFER_MAX,
    CaptureWorker,
    new_log_buffer,
    new_record_buffer,
)
from manta_link.health import Counters, Health

from .golden import READING


@pytest.fixture
def counters():
    return Counters()


class TestDraining:
    def test_a_record_is_parsed_and_handed_on_with_its_receipt_time(self, counters):
        records = new_record_buffer()
        records.append((READING, 1234.5))
        handed = []
        worker = CaptureWorker(records, new_log_buffer(), counters,
                               sink=lambda raw, rec, at: handed.append((rec, at)))

        assert worker.drain_once() == 1

        parsed, received = handed[0]
        assert parsed["type"] == "reading"
        assert parsed["uv_counts"] == 812
        assert received == 1234.5
        assert counters.get("records_captured") == 1

    def test_the_buffer_is_empty_afterwards(self, counters):
        records = new_record_buffer()
        records.append((READING, 1.0))
        CaptureWorker(records, new_log_buffer(), counters).drain_once()
        assert len(records) == 0

    def test_pico_log_lines_are_drained_and_counted(self, counters):
        logs = new_log_buffer()
        logs.append(b"SD card initialized")
        logs.append(b"File opened: data3.txt")

        assert CaptureWorker(new_record_buffer(), logs, counters).drain_once() == 2
        assert counters.get("pico_log_lines") == 2

    def test_draining_an_empty_buffer_is_not_an_error(self, counters):
        assert CaptureWorker(new_record_buffer(), new_log_buffer(),
                             counters).drain_once() == 0


class TestMalformedRecords:
    def test_truncated_json_is_counted_not_raised(self, counters):
        records = new_record_buffer()
        records.append((b'{"type":"reading","ph":', 1.0))
        CaptureWorker(records, new_log_buffer(), counters).drain_once()

        assert counters.get("records_malformed") == 1
        assert counters.get("records_captured") == 0

    def test_a_json_array_is_not_a_record(self, counters):
        """json.loads is happy with a list. The rest of the pipeline is not."""
        records = new_record_buffer()
        records.append((b"[1, 2, 3]", 1.0))
        CaptureWorker(records, new_log_buffer(), counters).drain_once()
        assert counters.get("records_malformed") == 1

    def test_a_malformed_record_never_reaches_the_sink(self, counters):
        records = new_record_buffer()
        records.append((b"{not json}", 1.0))
        handed = []
        CaptureWorker(records, new_log_buffer(), counters,
                      sink=lambda raw, rec, at: handed.append(rec)).drain_once()
        assert handed == []

    def test_the_reason_is_logged_once_then_suppressed(self, counters, caplog):
        """A firmware that emits one bad line emits it every cycle.

        Unthrottled, the reason scrolls out of a log history Kraken caps at
        3 x 20 MB long before anybody reads it.
        """
        records = new_record_buffer()
        for _ in range(50):
            records.append((b"{not json}", 1.0))

        with caplog.at_level("WARNING"):
            CaptureWorker(records, new_log_buffer(), counters).drain_once()

        assert counters.get("records_malformed") == 50
        assert caplog.text.count("unparseable record") == 1


class TestBacklog:
    def test_a_full_buffer_is_reported(self, counters, caplog):
        records = new_record_buffer()
        for _ in range(RECORD_BUFFER_MAX):
            records.append((READING, 1.0))

        with caplog.at_level("WARNING"):
            CaptureWorker(records, new_log_buffer(), counters).drain_once()

        assert counters.get("record_buffer_full") == 1
        assert "record buffer is full" in caplog.text

    def test_an_unbounded_buffer_is_never_called_full(self, counters):
        # deque(maxlen=None) cannot drop, so there is nothing to report.
        records = deque()
        records.append((READING, 1.0))
        CaptureWorker(records, new_log_buffer(), counters).drain_once()
        assert counters.get("record_buffer_full") == 0


class TestDroppedRecords:
    """Building the worker is what points a buffer at the tallies, as __main__ does."""

    def test_a_stalled_sink_loses_records_and_every_loss_is_counted(self, counters):
        """The failure this counter exists for: a 20 minute fsync retry.

        The worker is inside the sink while the buffer overruns, so the backlog
        hint at the top of drain_once cannot fire and the deque itself reports
        nothing. Without a tally at the append, 144 readings leave no trace.
        """
        fed = RECORD_BUFFER_MAX + 145
        records = new_record_buffer()
        stalled = threading.Event()
        release = threading.Event()

        def stalling_sink(raw, record, at):
            if not stalled.is_set():
                stalled.set()
                assert release.wait(5.0), "the sink was never released"

        worker = CaptureWorker(records, new_log_buffer(), counters,
                               sink=stalling_sink)
        records.append((READING, 1.0))
        drain = threading.Thread(target=worker.drain_once, daemon=True)
        drain.start()
        assert stalled.wait(5.0), "the worker never reached the sink"

        for _ in range(fed - 1):
            records.append((READING, 2.0))
        release.set()
        drain.join(5.0)
        assert not drain.is_alive(), "the drain never finished"

        assert counters.get("records_dropped") == 144
        captured = counters.get("records_captured")
        assert captured + counters.get("records_dropped") == fed

    def test_the_loss_reaches_the_heartbeat(self, counters, caplog):
        """The heartbeat is the only outbound signal a Release boat has."""
        records = new_record_buffer()
        CaptureWorker(records, new_log_buffer(), counters)
        for _ in range(RECORD_BUFFER_MAX + 3):
            records.append((READING, 1.0))

        with caplog.at_level("INFO"):
            Health(counters, heartbeat_interval_s=0.0).tick()

        assert "records_dropped=3" in caplog.text

    def test_a_buffer_that_never_fills_reports_no_drops(self, counters):
        records = new_record_buffer()
        CaptureWorker(records, new_log_buffer(), counters)
        for _ in range(RECORD_BUFFER_MAX):
            records.append((READING, 1.0))
        assert counters.get("records_dropped") == 0


class TestSummary:
    def test_it_says_how_much_arrived(self, counters, caplog):
        records = new_record_buffer()
        for _ in range(3):
            records.append((READING, 1.0))
        worker = CaptureWorker(records, new_log_buffer(), counters)
        worker.drain_once()

        with caplog.at_level("INFO"):
            worker._summarise(time.monotonic())

        assert "captured 3 record(s)" in caplog.text

    def test_it_is_silent_when_nothing_arrived(self, counters, caplog):
        """Zero records is the correct steady state on a Release boat.

        Silence there is the point of the whole extension, so the summary must
        not turn it into a line a minute. The heartbeat proves liveness.
        """
        worker = CaptureWorker(new_record_buffer(), new_log_buffer(), counters)

        with caplog.at_level("INFO"):
            worker._summarise(time.monotonic())

        assert caplog.text == ""

    def test_it_reports_only_what_is_new_since_the_last_one(self, counters, caplog):
        records = new_record_buffer()
        records.append((READING, 1.0))
        worker = CaptureWorker(records, new_log_buffer(), counters)
        worker.drain_once()
        worker._summarise(time.monotonic())

        records.append((READING, 2.0))
        worker.drain_once()
        with caplog.at_level("INFO"):
            worker._summarise(time.monotonic())

        assert "captured 1 record(s)" in caplog.text
        assert "(2 total)" in caplog.text
