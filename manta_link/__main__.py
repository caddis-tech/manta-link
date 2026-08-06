"""Entry point.

Runs the reader on the main thread deliberately. Later steps add daemon workers
for capture, GPS, upload and health; putting the reader on the main thread means
a worker dying, or failing to start at all, cannot take the port owner with it,
and it is the thread a signal is delivered to.
"""

import logging
import signal
import sys
from types import FrameType

from . import __version__, logging_setup, supervisor
from .reader import SerialReader

log = logging.getLogger("manta_link")


def _on_terminate(signum: int, frame: FrameType | None) -> None:
    log.info("signal %d received; shutting down", signum)
    raise SystemExit(0)


def install_signal_handlers() -> None:
    """Make SIGTERM actually stop this process.

    The container runs python as PID 1 under an exec-form ENTRYPOINT, and the
    kernel delivers a signal to PID 1 only when a handler is installed for it.
    Python installs one for SIGINT and not for SIGTERM, so without this every
    Kraken stop, restart and uninstall waits the full timeout and then SIGKILLs.
    """
    signal.signal(signal.SIGTERM, _on_terminate)
    signal.signal(signal.SIGINT, _on_terminate)


def main() -> int:
    logging_setup.configure()
    install_signal_handlers()
    log.info("MANTA Link %s starting", __version__)

    reader = SerialReader()
    try:
        supervisor.run_forever("reader", reader.run_forever)
    except SystemExit:
        log.info("stopped after answering %d request(s)", reader.answered_count)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
