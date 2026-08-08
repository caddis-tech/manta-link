"""Keeping a loop alive through anything that is not a shutdown.

Every worker thread runs its loop through here, and health hands in its own
note_restart as on_restart, so a restart is tallied where the heartbeat can
report it. A callback rather than the health object itself, because health
imports this module to start those threads in the first place.

The reader is supervised by this too, despite running on the main thread rather
than as a worker. It is the component the whole design exists to protect, and it
is reachable by exceptions its own handlers do not name: termios.error is
created with a NULL base in CPython, so it derives from Exception rather than
OSError, and pyserial leaves tcsetattr (inside Serial.__init__) and tcdrain
unwrapped. Unplugging the Pico between port resolution and open reaches it.

Unsupervised, that ends main() and the container restart policy takes over, and
a restart is the one event that can lose an in-flight TIME?.
"""

import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0


def run_forever(
    name: str,
    fn: Callable[[], None],
    on_restart: Callable[[str], None] | None = None,
) -> None:
    """Run fn, restarting it on any failure, backing off as they repeat.

    fn is itself expected to loop, so returning normally is already abnormal and
    is treated as a restart.
    """
    backoff = BACKOFF_START_S

    while True:
        started = time.monotonic()
        try:
            fn()
            log.warning("%s returned unexpectedly; restarting", name)
        except SystemExit:
            # The only intentional exit, from the signal handler.
            raise
        except BaseException:
            # Deliberately broad, including MemoryError and RecursionError. A
            # worker dying must degrade one capability, never the process.
            log.exception("%s failed; restarting in %.1fs", name, backoff)

        if on_restart is not None:
            on_restart(name)

        # A loop that ran for a while and then failed is a different problem
        # from one failing on every attempt, so only the latter backs off.
        if time.monotonic() - started >= BACKOFF_MAX_S:
            backoff = BACKOFF_START_S

        time.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX_S)
