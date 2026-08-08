"""A durable queue with nothing slow on the path that fills it.

The API is the system of record now, so this is a data-loss guard as much as a
decoupling layer: whatever the spool drops is missing from the dataset for good.
Every eviction and every discard is counted, and the heartbeat carries the
counts.

Two things the prior art did that are not repeated here. It globbed the whole
directory, up to 60,000 files, plus a disk_usage call, once per reading and on
the capture thread. And it named files by wall clock, so a single NTP step
backwards inverted eviction order and threw away the newest readings first.

Instead there is one scan, at startup, and an in-memory deque index from then
on. Filenames are a zero-padded sequence continued from the highest name that
scan found, which is monotonic across restarts without a counter file of its
own: every live entry is below the next number to be issued, whatever the clock
does. Fixed width means lexicographic order is numeric order.

Eviction unlinks the file and drops the index entry together, so any retry state
step 5 keeps must be keyed to the spool name rather than to the client_ref.
Otherwise an evicted entry orphans its retry state permanently, which is the
leak the prior art needed a periodic pruner for.
"""

import json
import logging
import os
import re
import threading
from collections import deque
from pathlib import Path
from typing import Any

from .health import Counters
from .logging_setup import Throttle

log = logging.getLogger(__name__)

SPOOL_SUBDIR = "spool"

ENTRY_SUFFIX = ".json"
PARTIAL_SUFFIX = ".part"
NAME_WIDTH = 12

_ENTRY_RE = re.compile(rf"^\d{{{NAME_WIDTH}}}\{ENTRY_SUFFIX}$")

# On the extension volume the spool shares a small partition with BlueOS itself,
# so the cap is tight: at roughly 400 bytes an entry this is under a megabyte,
# and at the 2.5s cycle it still buys about 80 minutes of offline time.
VOLUME_MAX_ENTRIES = 2_000

# A USB stick has room to ride out a long outage. Bounded all the same, because
# an unbounded spool that fills a device stops being a queue and starts being
# the reason nothing else can write.
DEVICE_MAX_ENTRIES = 60_000

WRITE_LOG_INTERVAL_S = 60.0
EVICT_LOG_INTERVAL_S = 60.0

# Only Linux can fsync a directory, and the container is Linux. On the Windows
# box this is developed on the rename is simply not made durable, which costs a
# test nothing and production nothing.
_CAN_FSYNC_DIRECTORY = hasattr(os, "O_DIRECTORY")

# A prepared stick opts in by carrying this directory. /media on the Pi holds
# whatever anyone plugs in, and writing 14 MB a day to a stranger's stick, or to
# a card BlueOS is using, is not ours to decide.
MEDIA_ROOT = Path("/media")
DEVICE_MARKER = "aquadrone"


def find_data_device(override: str | None = None) -> Path | None:
    """The removable device the spool and archive prefer, if a boat has one.

    None is the shipping answer: no boat has a stick yet. The override exists so
    the mount point is configuration rather than a guess, since a wrong guess
    writes the spool somewhere nobody thinks to look.
    """
    if override:
        path = Path(override)
        if path.is_dir():
            return path
        log.warning("the configured data device %s is not a directory; "
                    "falling back to the extension volume", path)
        return None

    if not MEDIA_ROOT.is_dir():
        return None
    try:
        with os.scandir(MEDIA_ROOT) as mounted:
            for entry in sorted(mounted, key=lambda e: e.name):
                marker = Path(entry.path) / DEVICE_MARKER
                if entry.is_dir() and marker.is_dir():
                    return marker
    except OSError as exc:
        log.warning("could not look for a data device under %s (%s)",
                    MEDIA_ROOT, exc)
    return None


def choose_directory(
    data_device: Path | None, extension_volume: Path
) -> "tuple[Path, int]":
    """Where the spool lives, and how many entries it may hold there.

    Never the extension volume itself. The API token lives in its .env, and the
    startup index scan must never enumerate a directory holding a credential.
    """
    if data_device is not None:
        return data_device / SPOOL_SUBDIR, DEVICE_MAX_ENTRIES
    return extension_volume / SPOOL_SUBDIR, VOLUME_MAX_ENTRIES


class Spool:
    """Entries on disk, their order in memory, and a cap on both."""

    def __init__(
        self,
        directory: Path,
        counters: Counters,
        max_entries: int = VOLUME_MAX_ENTRIES,
    ) -> None:
        self._directory = directory
        self._counters = counters
        self._max_entries = max_entries
        # Guards the index and the sequence. It is never held across a write or
        # an fsync, only across the unlink an eviction does. The capture worker
        # puts and the uploader removes; neither is the reader thread, so
        # waiting here can never cost a TIME? reply.
        self._lock = threading.Lock()
        self._index: deque[str] = deque()
        self._next_sequence = 0
        self._write_log = Throttle(WRITE_LOG_INTERVAL_S)
        self._evict_log = Throttle(EVICT_LOG_INTERVAL_S)

    @property
    def directory(self) -> Path:
        return self._directory

    def __len__(self) -> int:
        return len(self._index)

    def open(self) -> None:
        """The one scan. Rebuilds the index and picks up the sequence.

        Raises if the directory cannot be made. The caller logs that and carries
        on: a spool that cannot be opened costs records, and stopping the
        process over it would cost the port its owner.
        """
        self._directory.mkdir(parents=True, exist_ok=True)

        names: list[str] = []
        with os.scandir(self._directory) as entries:
            for entry in entries:
                if entry.name.endswith(PARTIAL_SUFFIX):
                    # A write interrupted by a power cut. It was never indexed
                    # and was never complete, so it is ours to remove.
                    self._unlink(entry.name)
                    self._counters.bump("spool_partials_pruned")
                    continue
                if _ENTRY_RE.match(entry.name) and entry.is_file():
                    names.append(entry.name)

        names.sort()
        with self._lock:
            self._index = deque(names)
            self._next_sequence = _sequence_after(names)
            self._evict_over_cap()
        if names:
            log.info("recovered %d spooled record(s) from %s",
                     len(names), self._directory)

    def put(self, envelope: dict[str, Any]) -> str | None:
        """Write one entry. Returns its name, or None if it could not be kept.

        O(1) and free of globbing, because this runs once per reading on the
        capture worker and everything slow on that thread eventually shows up as
        a backlog in front of the reader.
        """
        with self._lock:
            name = f"{self._next_sequence:0{NAME_WIDTH}d}{ENTRY_SUFFIX}"
            self._next_sequence += 1

        if not self._write(name, envelope):
            return None

        with self._lock:
            self._index.append(name)
            self._evict_over_cap()
        self._counters.bump("records_spooled")
        return name

    def names(self) -> list[str]:
        """A snapshot in oldest-first order, for the uploader to walk."""
        with self._lock:
            return list(self._index)

    def load(self, name: str) -> dict[str, Any] | None:
        """One entry, or None if it is gone or unreadable.

        An unreadable entry is discarded rather than retried forever: it is a
        truncated or corrupted file, and no number of reads will improve it.
        """
        try:
            with open(self._directory / name, "rb") as handle:
                loaded = json.loads(handle.read())
        except FileNotFoundError:
            self._forget(name)
            return None
        except (OSError, ValueError) as exc:
            self._counters.bump("spool_discarded")
            log.warning("discarding unreadable spool entry %s (%s)", name, exc)
            self.remove(name)
            return None

        if not isinstance(loaded, dict):
            self._counters.bump("spool_discarded")
            log.warning("discarding spool entry %s: top level is %s",
                        name, type(loaded).__name__)
            self.remove(name)
            return None
        return loaded

    def remove(self, name: str) -> None:
        """Drop an entry that has been dealt with."""
        self._unlink(name)
        self._forget(name)

    def _write(self, name: str, envelope: dict[str, Any]) -> bool:
        """Whole file or no file, and fsynced: this protects data in flight."""
        partial = self._directory / (name + PARTIAL_SUFFIX)
        try:
            line = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
            with open(partial, "wb") as handle:
                handle.write(line.encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, self._directory / name)
            self._fsync_directory()
        except (OSError, TypeError, ValueError) as exc:
            self._counters.bump("spool_write_failures")
            if self._write_log.should_emit():
                log.error("could not spool a record to %s (%s); %d more "
                          "suppressed since the last of these",
                          self._directory, exc,
                          self._write_log.take_suppressed())
            return False
        return True

    def _fsync_directory(self) -> None:
        if not _CAN_FSYNC_DIRECTORY:
            return
        fd = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _evict_over_cap(self) -> None:
        """Caller holds the lock. Oldest first, and never silently."""
        while len(self._index) > self._max_entries:
            name = self._index.popleft()
            self._unlink(name)
            self._counters.bump("spool_evicted")
            if self._evict_log.should_emit():
                log.warning("the spool is full at %d entries; evicting the "
                            "oldest, which is data the API will never see",
                            self._max_entries)

    def _forget(self, name: str) -> None:
        with self._lock:
            if self._index and self._index[0] == name:
                # The uploader drains oldest first, so this is the usual case
                # and the O(n) scan below is the exception.
                self._index.popleft()
                return
            try:
                self._index.remove(name)
            except ValueError:
                pass

    def _unlink(self, name: str) -> None:
        try:
            (self._directory / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove spool entry %s (%s)", name, exc)


def _sequence_after(names: list[str]) -> int:
    """One past the highest name on disk, so no live entry outranks a new one."""
    if not names:
        return 0
    return int(names[-1][:NAME_WIDTH]) + 1
