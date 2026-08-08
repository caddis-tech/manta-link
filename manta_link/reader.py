"""The one thing in this process that touches the Pico's serial port.

Nothing else opens it, and nothing else writes to it. The reader's only hard
obligation is answering TIME?: the Pico asks for its first 180 seconds and then
never asks again, so a missed answer costs that entire run its absolute
timestamps with no second chance.

Everything else is handed off: a line that is not a request is appended to a
bounded deque and forgotten. At maxlen, deque.append drops the oldest under the
GIL without blocking or allocating, so a stalled worker costs old records and
can never cost a reply. No slow or failable work runs on this thread.
"""

import logging
import time
from collections import deque
from collections.abc import Callable

import serial

from . import clock
from .framing import Kind, LineAssembler, classify, parse_banner
from .portfinder import BAUD, find_pico_port

# TIOCEXCL is Linux-only and this package is developed on Windows, so the
# exclusion syscall is optional at import and simply absent off-platform. Only
# the constant is made optional: fcntl is never rebound, so the call below stays
# statically checkable against the platform this actually runs on.
try:
    import fcntl
    import termios

    _TIOCEXCL: "int | None" = termios.TIOCEXCL
except (ImportError, AttributeError):  # pragma: no cover - platform dependent
    _TIOCEXCL = None

log = logging.getLogger(__name__)

REPLY_PREFIX = "TIME "

READ_SIZE = 4096
READ_TIMEOUT_S = 0.2

# A write that cannot complete in this long is a Pico that has stopped draining
# its OUT endpoint. pyserial's default is None, which is an unbounded select.
WRITE_TIMEOUT_S = 2.0

RECONNECT_DELAY_S = 2.0

# "No Pico present" is a normal state on a bench and a real one on a boat, but
# at the reconnect interval it writes 43,200 lines a day into a docker logs
# history Kraken caps at 3 x 20 MB, burying anything that matters.
ABSENT_LOG_INTERVAL_S = 300.0

# An open, quiet port is the correct steady state on a flight image, which makes
# it indistinguishable in the log from a wedged one. Say so periodically rather
# than reopening the port on a timer: a reopen landing inside the Pico's ask
# window can eat a TIME? request.
ALIVE_LOG_INTERVAL_S = 600.0


class SerialReader:
    """Owns the port for the life of the process."""

    def __init__(
        self,
        records: "deque[tuple[bytes, float]] | None" = None,
        logs: "deque[bytes] | None" = None,
        on_tick: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        on_banner: Callable[[], None] | None = None,
    ) -> None:
        self._records = records
        self._logs = logs
        self._on_tick = on_tick
        # Both exist for the boot-time anchor. The reconnect is the reliable
        # half: any Pico reset forces a USB re-enumeration, while a banner can
        # be printed into a deasserted DTR and never arrive at all.
        self._on_reconnect = on_reconnect
        self._on_banner = on_banner
        self._assembler = LineAssembler()
        self._last_absent_log = 0.0
        self.answered_count = 0
        self.connected = False

    def run_forever(self) -> None:
        """Find the Pico, serve it, and reopen when it goes away.

        Never returns. Callers still supervise it, because a serial stack can
        raise things this loop does not name.
        """
        while True:
            self._tick()
            port_path = find_pico_port()
            if port_path is None:
                self._log_absent()
                time.sleep(RECONNECT_DELAY_S)
                continue

            try:
                self._serve(port_path)
            except Exception as exc:
                # Broad on purpose, not a two-class tuple. termios.error is
                # built with a NULL base in CPython, so it derives from
                # Exception rather than OSError, and pyserial leaves tcsetattr
                # (inside Serial.__init__) and tcdrain unwrapped. Unplugging the
                # Pico between resolving the port and opening it lands here.
                log.warning("%s went away (%s); reopening", port_path, exc)
            finally:
                self.connected = False
                # A reconnect means the Pico re-enumerated, so any bytes still
                # buffered belong to a run that has ended, and so does anything
                # derived from that run's uptime.
                self._assembler.reset()
                self._notify(self._on_reconnect, "reconnect notice")

            time.sleep(RECONNECT_DELAY_S)

    def _log_absent(self) -> None:
        now = time.monotonic()
        if now - self._last_absent_log >= ABSENT_LOG_INTERVAL_S:
            log.info("no Pico (USB VID 0x2E8A) present; waiting")
            self._last_absent_log = now

    def _serve(self, port_path: str) -> None:
        """Answer requests on one port until it goes away."""
        with serial.Serial(
            port_path,
            BAUD,
            timeout=READ_TIMEOUT_S,
            write_timeout=WRITE_TIMEOUT_S,
            # Advisory flock. It does not stop `cat /dev/ttyACM0`, and TIOCEXCL
            # below is defeated by CAP_SYS_ADMIN, which this container has. The
            # one-process discipline still carries the weight; these two only
            # make a mistake loud instead of silent.
            exclusive=True,
        ) as link:
            self._claim_exclusive(link)
            self.connected = True
            self._last_absent_log = 0.0
            log.info("listening on %s", port_path)

            last_alive = time.monotonic()
            byte_count = 0

            while True:
                chunk = link.read(READ_SIZE)
                now = time.monotonic()
                self._tick()

                if chunk:
                    byte_count += len(chunk)
                    for line in self._assembler.feed(chunk):
                        self._dispatch(link, line, now)

                if now - last_alive >= ALIVE_LOG_INTERVAL_S:
                    log.info("still listening on %s (%d bytes, %d answered)",
                             port_path, byte_count, self.answered_count)
                    last_alive = now

    def _tick(self) -> None:
        """Give the main thread its one job besides reading: watching health.

        Health is a worker, so nothing inside the worker set can notice its
        death. This loop is the only context guaranteed to still be running,
        and it already wakes every read timeout.
        """
        self._notify(self._on_tick, "health check")

    def _notify(self, callback: "Callable[[], None] | None", what: str) -> None:
        """Tell something off-thread, without letting it cost us the port."""
        if callback is None:
            return
        try:
            callback()
        except Exception:
            # A bug in a listener must not cost the port its owner. Reporting it
            # and reading on is strictly better than reopening the port, which
            # is the one event that can lose an in-flight TIME?.
            log.exception("%s failed; continuing to read", what)

    def _claim_exclusive(self, link: serial.Serial) -> None:
        if _TIOCEXCL is None:
            return
        try:
            fcntl.ioctl(link.fileno(), _TIOCEXCL)
        except OSError as exc:
            # Not fatal. Exclusion is a guard rail, not a requirement.
            log.warning("could not claim exclusive access (%s)", exc)

    def _dispatch(self, link: serial.Serial, line: bytes, received: float) -> None:
        kind = classify(line)

        if kind is Kind.TIME_REQUEST:
            self._answer(link)
            return

        if kind is Kind.RECORD:
            if self._records is not None:
                # The monotonic receipt time travels with the bytes: step 3
                # derives a record's absolute timestamp from an anchor and this
                # offset, and the moment it was parsed is not that moment.
                self._records.append((line, received))
            return

        if kind is Kind.BANNER:
            self._notify(self._on_banner, "banner notice")
            parsed = parse_banner(line)
            if parsed is not None:
                version, state = parsed
                log.info("Pico booted: firmware %s, %s", version, state)
                if "DISABLED" in state:
                    log.warning("this Pico is running a Debug image and is "
                                "recording NOTHING to its card")
            return

        if kind is Kind.LOG and self._logs is not None:
            self._logs.append(line)

    def _answer(self, link: serial.Serial) -> None:
        """Write the reply, or stay silent if the clock is not worth sending."""
        if not clock.clock_is_trustworthy():
            log.info("request received, clock not yet synced; silent")
            return

        epoch_ms = clock.epoch_ms_now()
        try:
            link.write(f"{REPLY_PREFIX}{epoch_ms}\n".encode("ascii"))
        except serial.SerialTimeoutException:
            # The Pico stopped draining. Nothing to do but let the next request
            # try again; the write buffer is the kernel's problem now.
            log.warning("write timed out after %.1fs; Pico is not draining",
                        WRITE_TIMEOUT_S)
            return

        # Deliberately no flush(). pyserial's flush() is a bare termios.tcdrain
        # with no timeout of its own, which write_timeout does not govern, so a
        # bounded write followed by a flush just moves the hang. Twenty bytes
        # the kernel has already accepted do not need draining by hand.
        self.answered_count += 1
        log.info("answered with %d", epoch_ms)
