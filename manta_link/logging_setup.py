"""Log configuration, and the one thread allowed to touch stdout.

Every other thread hands its records to a bounded queue and returns. That looks
like over-engineering for a process that logs a line every ten minutes, and it
is not: there is a complete chain from a full SD card to a missed TIME?.

A spool write fails, so capture logs an error every cycle. PYTHONUNBUFFERED
makes each line an immediate write to the stdout pipe. Docker's default log
delivery mode is blocking and Kraken overwrites HostConfig.LogConfig
unconditionally, so declaring mode=non-blocking in the manifest does nothing.
logging.Handler.handle wraps emit in a per-handler lock that every thread
shares, and StreamHandler.emit writes and flushes inside it. So capture blocks
on the pipe while holding the lock the reader needs to log its own reply, and
the reader stops reading the port. The remedy has to be in-process, and this is
it: the shared lock is now held only long enough to put an object on a queue.
"""

import atexit
import logging
import logging.handlers
import queue
import sys
import threading
import time
from typing import TextIO

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Roughly a minute of the loudest failure mode we have seen (a per-cycle spool
# error) at a line per cycle, which is long enough for a transient stall to
# drain without loss and short enough that a permanent one is bounded memory.
LOG_QUEUE_MAX = 2048

DROP_REPORT_INTERVAL_S = 60.0

# Long enough to write a full queue to a pipe that is draining, short enough to
# stay well inside the stop timeout Kraken gives us when the pipe is not.
DRAIN_STOP_TIMEOUT_S = 2.0

log = logging.getLogger(__name__)


class Throttle:
    """Lets a repeating message through at most once per interval.

    A per-cycle failure writes tens of thousands of identical lines a day into a
    history Kraken caps at 3 x 20 MB, burying the first occurrence, which is the
    one that says what went wrong.
    """

    def __init__(self, interval_s: float) -> None:
        self._interval_s = interval_s
        self._last = 0.0
        self._suppressed = 0

    def should_emit(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        if self._last and moment - self._last < self._interval_s:
            self._suppressed += 1
            return False
        self._last = moment
        return True

    def take_suppressed(self) -> int:
        """How many were swallowed since the last emitted one, then reset."""
        swallowed = self._suppressed
        self._suppressed = 0
        return swallowed


class DroppingQueueHandler(logging.handlers.QueueHandler):
    """Discards records rather than waiting for room.

    The stock handler routes a full queue to handleError, which writes to stderr
    from the calling thread: the blocking write this whole arrangement exists to
    keep off the reader.
    """

    def __init__(self, log_queue: "queue.Queue[logging.LogRecord | None]") -> None:
        super().__init__(log_queue)
        self.dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            # Handler.handle already holds this handler's lock around emit, so
            # the increment is serialised without a lock of its own.
            self.dropped += 1


class LogSink:
    """The drain thread. The only thread in the process that writes stdout."""

    def __init__(
        self,
        log_queue: "queue.Queue[logging.LogRecord | None]",
        target: logging.Handler,
        source: DroppingQueueHandler,
    ) -> None:
        self._queue = log_queue
        self._target = target
        self._source = source
        self._reported_drops = 0
        self._drop_report = Throttle(DROP_REPORT_INTERVAL_S)
        self._stopping = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def dropped(self) -> int:
        """Lines discarded to keep a worker unblocked. Non-zero is a symptom."""
        return self._source.dropped

    def start(self) -> None:
        """Start draining. Owned here rather than by the health watchdog.

        Health logs, so it cannot also be the thing that keeps logging alive,
        and this loop's one failable step catches for itself.
        """
        # Daemon, so a drain wedged on a stalled pipe cannot keep the process
        # alive after SIGTERM. Losing the tail of the log to a shutdown that
        # already logged its reason is not a loss worth blocking for.
        self.thread = threading.Thread(
            target=self._drain_forever, name="logsink", daemon=True
        )
        self.thread.start()

    def stop(self, timeout_s: float = DRAIN_STOP_TIMEOUT_S) -> bool:
        """Write what is queued, then end the thread. Returns whether it ended.

        Bounded, because the drain may be blocked on a stalled pipe, and an
        unbounded wait there is the stop timeout and SIGKILL we exist to avoid.
        """
        thread = self.thread
        if thread is None:
            return True
        self._stopping.set()
        try:
            # Wakes an idle drain out of its blocking get. If there is no room
            # for the sentinel the loop sees the flag once it has caught up.
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread.join(timeout_s)
        return not thread.is_alive()

    def _drain_forever(self) -> None:
        while True:
            record = self._queue.get()
            if record is None:
                return
            try:
                self._target.handle(record)
            except Exception:
                # There is nowhere to report this: this thread is the log. The
                # line is lost and the drain lives, which is the right trade,
                # because the alternative is a process that logs nothing again.
                pass
            self._report_drops()
            if self._stopping.is_set() and self._queue.empty():
                return

    def _report_drops(self) -> None:
        """Say that lines were lost, so silence is never mistaken for calm."""
        dropped = self._source.dropped
        if dropped == self._reported_drops or not self._drop_report.should_emit():
            return
        # Goes back through the queue rather than straight to the target, so it
        # keeps its place in the ordering of the lines around it.
        log.warning("dropped %d log line(s) to keep the reader unblocked",
                    dropped - self._reported_drops)
        if self._source.dropped == dropped:
            # We report through the queue that is full by definition here, so
            # the warning can be dropped itself. Leaving the watermark where it
            # was keeps the whole count outstanding for the next attempt.
            self._reported_drops = dropped


def configure(
    level: int = logging.INFO,
    stream: TextIO | None = None,
    queue_max: int = LOG_QUEUE_MAX,
) -> LogSink:
    """Point the root logger at the queue and start the drain. Returns the sink.

    Replaces any existing root handler rather than adding to it: basicConfig is
    a no-op once a handler exists, so adding would leave a second, blocking
    path to stdout in place and the whole point of this would be lost.
    """
    target = logging.StreamHandler(stream if stream is not None else sys.stdout)
    target.setFormatter(logging.Formatter(FORMAT))

    # logging.shutdown's atexit pass acquires every handler's lock, and the drain
    # holds the target's for the whole of a blocked write, so a stalled pipe hangs
    # the exit itself with no timeout. LogSink.stop is the bounded replacement.
    atexit.unregister(logging.shutdown)

    log_queue: queue.Queue[logging.LogRecord | None] = queue.Queue(maxsize=queue_max)
    source = DroppingQueueHandler(log_queue)

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(source)
    root.setLevel(level)

    sink = LogSink(log_queue, target, source)
    sink.start()
    return sink
