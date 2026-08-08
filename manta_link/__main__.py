"""Entry point. Builds the buffers, starts the workers, and reads.

The reader stays on the main thread deliberately. A worker dying, or failing to
start at all, cannot take the port owner with it, and the main thread is the one
a signal is delivered to. It is also the only context guaranteed to be running,
which is why the health worker's own liveness is checked from inside its loop.
"""

import logging
import signal
import sys
from types import FrameType

from . import __version__, capture, logging_setup, supervisor
from .health import Counters, Health
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

    counters = Counters()
    records = capture.new_record_buffer()
    logs = capture.new_log_buffer()

    worker = capture.CaptureWorker(records, logs, counters)
    health = Health(counters)
    health.register("capture", worker.run_forever)
    health.start()

    reader = SerialReader(
        records=records, logs=logs, on_tick=health.check_from_main_thread
    )
    try:
        supervisor.run_forever("reader", reader.run_forever, health.note_restart)
    except SystemExit:
        log.info("stopped after answering %d request(s)", reader.answered_count)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
