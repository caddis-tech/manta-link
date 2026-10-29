"""Entry point. Builds the buffers, starts the workers, and reads.

The reader stays on the main thread deliberately. A worker dying, or failing to
start at all, cannot take the port owner with it, and the main thread is the one
a signal is delivered to. It is also the only context guaranteed to be running,
which is why the health worker's own liveness is checked from inside its loop.

Every worker is registered before `Health.start()`. `_restart_dead_workers`
iterates the worker dict on health's own thread with no lock, so registering one
afterwards raises "dictionary changed size during iteration" and takes the
watchdog down with it. `Health.register` still allows it, for the case it was
written for; nothing here uses that.
"""

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from . import (
    __version__,
    archive,
    capture,
    config,
    gps,
    heartbeat,
    install_token,
    logging_setup,
    mavlink2rest,
    record,
    spool,
    supervisor,
    upload,
)
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

# Where the autopilot's position comes from. The default is the loopback address
# MAVLink2Rest answers on under host networking; a bench run points it at a rig.
MAVLINK2REST_URL_ENV = "AQUADRONE_MAVLINK2REST_URL"

# How long start-up waits for the token save before opening the port anyway. The
# save fsyncs, a failing card can hold an fsync for minutes, and the Pico asks
# TIME? only in its first 180 seconds, so the wait is a small slice of those.
INSTALL_TOKEN_SAVE_TIMEOUT_S = 5.0

# Every variable that configures this process, and what leaving it unset means.
# --help is built from this and the test suite reads the same tuple, so a knob
# added without a line here fails rather than shipping undocumented.
CONFIG_ENV_HELP: tuple[tuple[str, str], ...] = (
    (VOLUME_ENV, f"the persistent volume, default {DEFAULT_VOLUME}"),
    (DATA_DEVICE_ENV, "the removable device; unset leaves the archive off"),
    (MAVLINK2REST_URL_ENV, f"MAVLink2Rest, default {mavlink2rest.DEFAULT_URL}"),
    (config.TOKEN_ENV, f"the API token; saved at start into ${VOLUME_ENV}/"
                       f"{config.ENV_FILENAME}, which is read first"),
    (config.API_URL_ENV, f"the API, default {config.DEFAULT_API_URL}"),
)

CONFIG_ENV_NAMES: tuple[str, ...] = tuple(name for name, _ in CONFIG_ENV_HELP)


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


def data_volume() -> Path:
    """The persistent volume. The token's .env lives here, the spool below it."""
    return Path(os.environ.get(VOLUME_ENV, DEFAULT_VOLUME))


def build_durables(
    counters: Counters, positions: "gps.PositionCache | None" = None
) -> "tuple[record.Recorder, spool.Spool]":
    """The durable half: where a reading is put, and what it is put as.

    Returns the spool as well as the recorder, because the uploader drains the
    same object the recorder fills and there is only ever one of it.

    Nothing here is allowed to end the process. A spool that cannot be opened
    costs records, which is bad; a process that will not start costs the port
    its owner, which is worse and is the one failure this design exists to
    prevent.
    """
    device = spool.find_data_device(os.environ.get(DATA_DEVICE_ENV))

    directory, max_entries = spool.choose_directory(device, data_volume())
    store = spool.Spool(directory, counters, max_entries)
    ring = archive.Archive(archive.choose_directory(device), counters)

    open_or_carry_on("spool", store.open)
    open_or_carry_on("archive", ring.open)
    return record.Recorder(store, ring, counters, position=positions), store


def register_gps(
    health: Health, positions: "gps.PositionCache", counters: Counters
) -> None:
    """Start the position poller, or say why readings will carry none.

    A URL this cannot dial is a configuration mistake, not a reason to stop. The
    port owner is the job that cannot be given up, and a boat with no positions
    still records water quality onto its card and into the spool.
    """
    url = os.environ.get(MAVLINK2REST_URL_ENV, mavlink2rest.DEFAULT_URL)
    try:
        link = mavlink2rest.Mavlink2Rest(url)
    except mavlink2rest.BadUrl as exc:
        counters.bump("gps_disabled")
        log.error("%s is unusable (%s); readings will carry no position",
                  MAVLINK2REST_URL_ENV, exc)
        return

    log.info("polling MAVLink2Rest at %s for position", link.url)
    health.register("gps", gps.GpsPoller(link, positions, counters).run_forever)


def open_or_carry_on(name: str, opener: Callable[[], None]) -> None:
    """Start one store, or say why it will not be storing anything."""
    try:
        opener()
    except Exception:
        # Broad for the reason the supervisor gives: a capability that cannot
        # start must degrade to itself, never to the process.
        log.exception("the %s could not be opened; what would have gone into "
                      "it is counted and lost until it can be", name)


def load_config(tokens: config.TokenSession) -> config.ConfigWatcher:
    """Save the install token into the .env, then read the config once.

    In that order, so the first read already finds the token in the file. The
    save runs on its own thread so a stuck write cannot hold up the port.
    """
    volume = data_volume()
    saver = threading.Thread(target=save_install_token_or_carry_on, args=(volume,),
                             name="install-token", daemon=True)
    saver.start()
    saver.join(INSTALL_TOKEN_SAVE_TIMEOUT_S)
    if saver.is_alive():
        # A save that lands later is picked up by the next reload, as any change
        # to the file is.
        log.warning("saving the API token into %s has taken over %g s; starting "
                    "without waiting for it", volume, INSTALL_TOKEN_SAVE_TIMEOUT_S)

    watcher = config.ConfigWatcher(volume, tokens)
    # Read once here rather than waiting for the first beat. Health credits
    # itself a full interval at start, so a boat provisioned through Kraken's
    # Env would otherwise sit unconfigured for a minute after every install.
    # main calls this on the main thread, which becomes the reader thread; no
    # reload runs on it once the port is open.
    watcher.reload()
    return watcher


def save_install_token_or_carry_on(volume: Path) -> None:
    try:
        install_token.save_install_token(volume)
    except Exception as exc:
        # Broad for open_or_carry_on's reason: every expected failure is already
        # a warning in there, and nothing else may cost the port its owner. The
        # type only, as upload.py logs it, since the token is in play here.
        log.error("saving the API token into %s failed (%s); carrying on",
                  volume, type(exc).__name__)


def parse_args(argv: list[str] | None = None) -> None:
    """Refuse anything that is not a bare start, before the port is touched.

    There are no options; configuration is environment only. Without a parser an
    unrecognised flag was ignored and startup continued, so `--help` seized the
    Pico's port on a live boat instead of printing usage.
    """
    parser = argparse.ArgumentParser(
        prog="manta_link",
        description="Own the Pico's serial port, answer its time request, and "
                    "make every reading durable.",
        epilog="Configuration is environment only: "
               + "; ".join(f"{name} is {what}" for name, what in CONFIG_ENV_HELP)
               + ".",
    )
    parser.add_argument("--version", action="version",
                        version=f"MANTA Link {__version__}")
    parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parse_args(argv)
    sink = logging_setup.configure()
    install_signal_handlers()
    log.info("MANTA Link %s starting", __version__)

    counters = Counters()
    records = capture.new_record_buffer()
    logs = capture.new_log_buffer()
    positions = gps.PositionCache()
    recorder, store = build_durables(counters, positions)

    tokens = config.TokenSession()
    watcher = load_config(tokens)

    worker = capture.CaptureWorker(records, logs, counters, sink=recorder.capture)
    health = Health(counters, on_beat=watcher.reload)
    health.register("capture", worker.run_forever)
    register_gps(health, positions, counters)
    uploader = upload.Uploader(store, recorder.anchor, tokens, watcher, counters)
    health.register("uploader", uploader.run_forever)
    health.register(
        "heartbeat", heartbeat.Heartbeat(tokens, watcher, counters).run_forever
    )
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
    finally:
        # The drain is a daemon and QueueHandler.flush is a no-op, so nothing
        # else empties the queue: without this the line above dies with us, and
        # a clean stop is indistinguishable from a SIGKILL in the docker log.
        sink.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
