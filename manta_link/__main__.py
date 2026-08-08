"""Entry point. Builds the buffers, starts the workers, and reads.

The reader stays on the main thread deliberately. A worker dying, or failing to
start at all, cannot take the port owner with it, and the main thread is the one
a signal is delivered to. It is also the only context guaranteed to be running,
which is why the health worker's own liveness is checked from inside its loop.
"""

import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from . import __version__, archive, capture, logging_setup, record, spool, supervisor
from .health import Counters, Health
from .reader import SerialReader

log = logging.getLogger("manta_link")

# Set by the Dockerfile. The API token lives in this directory's .env, which is
# why the spool takes a subdirectory of it and never the directory itself.
VOLUME_ENV = "AQUADRONE_DATA_DIR"
DEFAULT_VOLUME = "/app/data"

# An explicit mount point for the removable device, for whichever boat gets one
# first. Unset is the shipping default and leaves the archive off.
DATA_DEVICE_ENV = "AQUADRONE_DATA_DEVICE"


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


def build_recorder(counters: Counters) -> record.Recorder:
    """The durable half: where a reading is put, and what it is put as.

    Nothing here is allowed to end the process. A spool that cannot be opened
    costs records, which is bad; a process that will not start costs the port
    its owner, which is worse and is the one failure this design exists to
    prevent.
    """
    device = spool.find_data_device(os.environ.get(DATA_DEVICE_ENV))
    volume = Path(os.environ.get(VOLUME_ENV, DEFAULT_VOLUME))

    directory, max_entries = spool.choose_directory(device, volume)
    store = spool.Spool(directory, counters, max_entries)
    ring = archive.Archive(archive.choose_directory(device), counters)

    open_or_carry_on("spool", store.open)
    open_or_carry_on("archive", ring.open)
    return record.Recorder(store, ring, counters)


def open_or_carry_on(name: str, opener: Callable[[], None]) -> None:
    """Start one store, or say why it will not be storing anything."""
    try:
        opener()
    except Exception:
        # Broad for the reason the supervisor gives: a capability that cannot
        # start must degrade to itself, never to the process.
        log.exception("the %s could not be opened; what would have gone into "
                      "it is counted and lost until it can be", name)


def main() -> int:
    logging_setup.configure()
    install_signal_handlers()
    log.info("MANTA Link %s starting", __version__)

    counters = Counters()
    records = capture.new_record_buffer()
    logs = capture.new_log_buffer()
    recorder = build_recorder(counters)

    worker = capture.CaptureWorker(records, logs, counters, sink=recorder.capture)
    health = Health(counters)
    health.register("capture", worker.run_forever)
    health.start()

    reader = SerialReader(
        records=records,
        logs=logs,
        on_tick=health.check_from_main_thread,
        on_reconnect=recorder.anchor.note_serial_reconnect,
        on_banner=recorder.anchor.note_banner,
    )
    try:
        supervisor.run_forever("reader", reader.run_forever, health.note_restart)
    except SystemExit:
        log.info("stopped after answering %d request(s)", reader.answered_count)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
