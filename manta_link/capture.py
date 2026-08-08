"""Everything the reader refuses to do on its own thread.

The reader appends raw bytes and a monotonic receipt time to a deque and goes
straight back to read(). This worker does the parsing, the counting and the
talking, and hands each record to the `sink`, which is where the envelope is
built and spooled. The upload attaches to the same seam.
"""

import json
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .health import Counters
from .logging_setup import Throttle

log = logging.getLogger(__name__)

# 256 records is roughly 11 minutes at the current cycle: long enough to ride
# out a stalled consumer, short enough that the memory is irrelevant. At maxlen
# a deque.append drops the oldest without blocking, which is the whole reason
# this is a deque and not a queue.Queue.
RECORD_BUFFER_MAX = 256
LOG_BUFFER_MAX = 256

IDLE_POLL_S = 0.2

SUMMARY_INTERVAL_S = 60.0
BAD_RECORD_LOG_INTERVAL_S = 60.0
BACKLOG_LOG_INTERVAL_S = 60.0

# The raw line travels with the parsed record because client_ref is a uuid5 of
# the bytes the Pico sent. Re-serialising the parsed dict would not reproduce
# them, and every copy of a reading has to hash to the same row.
RecordSink = Callable[[bytes, dict[str, Any], float], None]


def new_record_buffer() -> "deque[tuple[bytes, float]]":
    return deque(maxlen=RECORD_BUFFER_MAX)


def new_log_buffer() -> "deque[bytes]":
    return deque(maxlen=LOG_BUFFER_MAX)


class CaptureWorker:
    """Drains the reader's buffers and does the slow work off its thread."""

    def __init__(
        self,
        records: "deque[tuple[bytes, float]]",
        logs: "deque[bytes]",
        counters: Counters,
        sink: RecordSink | None = None,
    ) -> None:
        self._records = records
        self._logs = logs
        self._counters = counters
        self._sink = sink
        self._bad_record_log = Throttle(BAD_RECORD_LOG_INTERVAL_S)
        self._backlog_log = Throttle(BACKLOG_LOG_INTERVAL_S)
        self._last_summary = 0.0
        self._summarised_total = 0

    def run_forever(self) -> None:
        """Never returns. Supervised, and restarted if it ever does."""
        self._last_summary = time.monotonic()
        while True:
            if self.drain_once() == 0:
                # Polled rather than woken by an Event, because Event.set()
                # takes a lock and the thread that would call it is the one
                # thread that must never wait on anything.
                time.sleep(IDLE_POLL_S)

            now = time.monotonic()
            if now - self._last_summary >= SUMMARY_INTERVAL_S:
                self._summarise(now)

    def drain_once(self) -> int:
        """Consume everything buffered right now. Returns how many items."""
        self._warn_if_backlogged()
        handled = 0

        while True:
            try:
                raw, received = self._records.popleft()
            except IndexError:
                break
            self._capture(raw, received)
            handled += 1

        while True:
            try:
                line = self._logs.popleft()
            except IndexError:
                break
            self._counters.bump("pico_log_lines")
            log.debug("pico: %s", line.decode("ascii", "replace"))
            handled += 1

        return handled

    def _warn_if_backlogged(self) -> None:
        """A full buffer means the reader has already dropped older records."""
        capacity = self._records.maxlen
        if capacity is None or len(self._records) < capacity:
            return
        self._counters.bump("record_buffer_full")
        if self._backlog_log.should_emit():
            log.warning("record buffer is full at %d; the oldest records are "
                        "being dropped", capacity)

    def _capture(self, raw: bytes, received: float) -> None:
        try:
            record = json.loads(raw)
        except ValueError as exc:
            self._report_bad_record(str(exc))
            return

        if not isinstance(record, dict):
            self._report_bad_record(f"top level is {type(record).__name__}")
            return

        self._counters.bump("records_captured")
        if self._sink is not None:
            self._sink(raw, record, received)

    def _report_bad_record(self, reason: str) -> None:
        """Loud once, then counted.

        A firmware that emits one malformed line emits it every cycle, and at a
        line per cycle the reason scrolls out of the log history before anyone
        reads it.
        """
        self._counters.bump("records_malformed")
        if self._bad_record_log.should_emit():
            log.warning("unparseable record (%s); %d more suppressed since the "
                        "last of these", reason,
                        self._bad_record_log.take_suppressed())

    def _summarise(self, now: float) -> None:
        """Say how much arrived, not what was in it.

        Nothing is logged when nothing arrived: on a Release boat zero records
        is the correct steady state, and the heartbeat is what proves this
        worker is alive.
        """
        self._last_summary = now
        total = self._counters.get("records_captured")
        fresh = total - self._summarised_total
        self._summarised_total = total
        if fresh:
            log.info("captured %d record(s) in the last %.0fs (%d total)",
                     fresh, SUMMARY_INTERVAL_S, total)
