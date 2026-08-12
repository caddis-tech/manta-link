"""Counters, the heartbeat, and the watchdog that restarts a dead worker.

Health is itself a worker, so something outside it has to notice when it dies.
That is the main thread: it sits inside the reader loop, which wakes every 0.2s
for the read timeout and is the only context guaranteed to still be running.
Without that check, health's death silently removes the watchdog from every
other worker and stops the heartbeat, which on a Release boat is the only
outbound signal there is.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import supervisor

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 60.0

WATCHDOG_POLL_S = 1.0

# Thirty missed watchdog passes. Generous because the main thread's ticks are
# 2s apart while no Pico is attached, and a false restart costs more than a
# late one: the wedged thread cannot be killed, so a restart leaves two.
HEALTH_STALL_S = 30.0


@dataclass(frozen=True)
class Worker:
    """A named loop that runs on its own thread under the supervisor."""

    name: str
    run: Callable[[], None]


class Counters:
    """Integer tallies for the heartbeat, safe to bump from any thread.

    The lock is held for an integer add and nothing else. It is never taken
    across I/O, which is the property that makes it safe to leave on a path the
    reader could one day share.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = {}

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0) + amount

    def get(self, name: str) -> int:
        with self._lock:
            return self._values.get(name, 0)

    def snapshot(self) -> dict[str, int]:
        """A copy, so a caller formatting it cannot see it change underneath."""
        with self._lock:
            return dict(self._values)


class Health:
    """Runs the workers, restarts the dead ones, and beats every minute."""

    def __init__(
        self,
        counters: Counters,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._counters = counters
        self._heartbeat_interval_s = heartbeat_interval_s
        self._workers: dict[str, Worker] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._thread: threading.Thread | None = None
        self._last_tick = 0.0
        self._last_beat = 0.0

    def register(self, name: str, run: Callable[[], None]) -> None:
        """Add a worker. Call before start(); start() is what launches them."""
        self._workers[name] = Worker(name, run)

    def start(self) -> None:
        """Launch every registered worker, then health's own thread."""
        for worker in self._workers.values():
            self._start_worker(worker)
        # Credited a full window up front. Otherwise the reader's very first
        # tick, which can arrive before this thread has run at all, reads a
        # last-tick of zero and starts a second health worker.
        self._last_tick = time.monotonic()
        self._last_beat = self._last_tick
        self._start_self()

    def tick(self) -> None:
        """One watchdog pass, and a heartbeat if one is due."""
        now = time.monotonic()
        self._last_tick = now
        self._restart_dead_workers()
        if now - self._last_beat >= self._heartbeat_interval_s:
            self._beat(now)

    def run_forever(self) -> None:
        """Health's own loop. Supervised like any other worker."""
        while True:
            self.tick()
            time.sleep(WATCHDOG_POLL_S)

    def check_from_main_thread(self) -> None:
        """Restart health if it has died or stopped ticking. Called per read.

        Cheap on the happy path: a subtraction and a C-level is_alive(). It has
        to be, because the caller is the reader loop and the reader loop is what
        answers TIME?.
        """
        now = time.monotonic()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and now - self._last_tick < HEALTH_STALL_S
        ):
            return

        log.error("health worker is not ticking; restarting it")
        self._counters.bump("health_thread_restarts")
        # Credit it a full window before the next check, so a health thread that
        # is slow to start is not restarted again on the very next read.
        self._last_tick = now
        self._start_self()

    def note_restart(self, name: str) -> None:
        """Supervisor hook: the named loop failed and is being run again."""
        self._counters.bump(f"{name}_restarts")

    def _start_self(self) -> None:
        # A wedged thread cannot be killed, so this can leave the old one
        # parked forever on whatever blocked it. Two watchdogs is a
        # tolerable outcome; no watchdog is not.
        self._thread = self._spawn("health", self.run_forever)

    def _start_worker(self, worker: Worker) -> None:
        self._threads[worker.name] = self._spawn(worker.name, worker.run)

    def _spawn(self, name: str, run: Callable[[], None]) -> threading.Thread:
        # Daemon threads: SIGTERM raises SystemExit on the main thread, and a
        # worker must not be able to hold the container open past that.
        thread = threading.Thread(
            target=supervisor.run_forever,
            args=(name, run, self.note_restart),
            name=name,
            daemon=True,
        )
        thread.start()
        return thread

    def _restart_dead_workers(self) -> None:
        """A supervised loop only ends by SystemExit, so a dead thread is news."""
        for name, worker in self._workers.items():
            thread = self._threads.get(name)
            if thread is None:
                # Registered after start(). Nothing died, so nothing to report.
                self._start_worker(worker)
                continue
            if thread.is_alive():
                continue
            log.error("worker %s is not alive; restarting it", name)
            self._counters.bump(f"{name}_thread_restarts")
            self._start_worker(worker)

    def _beat(self, now: float) -> None:
        """The only periodic proof of life. A POST replaces this in step 6."""
        self._last_beat = now
        self._counters.bump("heartbeats")
        tallies = self._counters.snapshot()
        log.info("heartbeat: %s",
                 ", ".join(f"{k}={v}" for k, v in sorted(tallies.items())))
