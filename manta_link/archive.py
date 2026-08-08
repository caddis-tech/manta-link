"""A rotating NDJSON ring on a removable device, for the one field nothing
else on the boat holds.

Not redundant with the Pico's card. It carries GPS position, which the Pico
knows nothing about, and it comes off the boat without opening the enclosure.

Three rules, each of which has a way of going wrong behind it:

Written at spool time, never at acknowledgement time. Ack-coupling would leave
the archive permanently empty on a boat with no token, which is exactly the
configuration where a local copy of the position is worth most, and spool
eviction would then destroy the position outright since the card never had it.

Never fsynced. This is a second copy of data the spool has already made durable,
and paying for durability twice on the capture worker's thread buys nothing.

Never able to raise. On the acknowledgement path in step 5 this runs on the
uploader thread, where an unguarded exception unwinds into the supervisor and
the record is POSTed again.

No stick exists on any boat yet, so the whole path ships disabled: a missing
device is handled like a missing token, logged once, counted, and never fatal.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .health import Counters
from .logging_setup import Throttle

log = logging.getLogger(__name__)

ARCHIVE_SUBDIR = "archive"

FILE_PREFIX = "archive-"
FILE_SUFFIX = ".ndjson"
NAME_WIDTH = 6

_FILE_RE = re.compile(rf"^{FILE_PREFIX}\d{{{NAME_WIDTH}}}\{FILE_SUFFIX}$")

# An archived line measures about 400 bytes, so a file is roughly 40,000
# readings and the ring is a bit over a year at the 2.5s cycle. Sized to be
# carried off on a stick and read, not to be the smallest thing that works.
FILE_MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 32

FAILURE_LOG_INTERVAL_S = 300.0


def choose_directory(data_device: Path | None) -> Path | None:
    """Where the archive lives, or None when there is no device to hold it."""
    if data_device is None:
        return None
    return data_device / ARCHIVE_SUBDIR


class Archive:
    """Append, rotate, evict. Off entirely when there is nowhere to write."""

    def __init__(
        self,
        directory: Path | None,
        counters: Counters,
        max_files: int = MAX_FILES,
        file_max_bytes: int = FILE_MAX_BYTES,
    ) -> None:
        self._directory = directory
        self._counters = counters
        self._max_files = max_files
        self._file_max_bytes = file_max_bytes
        self._files: list[str] = []
        self._current_bytes = 0
        self._enabled = False
        self._failure_log = Throttle(FAILURE_LOG_INTERVAL_S)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def open(self) -> None:
        """One scan to find the ring, or one line to say there is not one."""
        if self._directory is None:
            self._counters.bump("archive_disabled")
            log.info("no data device present; the telemetry archive is off")
            return

        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._files = self._scan()
            self._current_bytes = self._size_of_newest()
        except OSError as exc:
            self._counters.bump("archive_disabled")
            log.warning("could not open the archive at %s (%s); it is off for "
                        "this run", self._directory, exc)
            return

        self._enabled = True
        log.info("archiving to %s (%d file(s) already there)",
                 self._directory, len(self._files))

    def append(self, envelope: dict[str, Any]) -> None:
        """One line. Cannot raise, whichever thread is calling."""
        if not self._enabled or self._directory is None:
            return

        try:
            line = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
            encoded = line.encode("utf-8") + b"\n"
            if not self._files:
                self._files.append(self._name_for(0))
            with open(self._directory / self._files[-1], "ab") as handle:
                handle.write(encoded)
            self._current_bytes += len(encoded)
            self._counters.bump("archive_lines")
            if self._current_bytes >= self._file_max_bytes:
                self._rotate()
        except Exception as exc:
            # Deliberately broad and deliberately swallowed. This is a second
            # copy of data the spool already holds, and on the acknowledgement
            # path a raise here would unwind into the supervisor and re-POST a
            # record the API has already taken.
            self._counters.bump("archive_failures")
            if self._failure_log.should_emit():
                log.warning("could not append to the archive (%s); %d more "
                            "suppressed since the last of these", exc,
                            self._failure_log.take_suppressed())

    def _rotate(self) -> None:
        """A new file, and the oldest one gone if the ring is full.

        Eviction rides on rotation so it never happens per record: one unlink
        every 16 MB, on a thread that has already paid for a file write.
        """
        self._files.append(self._name_for(_sequence_after(self._files)))
        self._current_bytes = 0
        self._counters.bump("archive_rotations")

        while len(self._files) > self._max_files:
            oldest = self._files.pop(0)
            self._unlink(oldest)
            self._counters.bump("archive_evicted")

    def _scan(self) -> list[str]:
        assert self._directory is not None
        found: list[str] = []
        with os.scandir(self._directory) as entries:
            for entry in entries:
                if _FILE_RE.match(entry.name) and entry.is_file():
                    found.append(entry.name)
        found.sort()
        return found

    def _size_of_newest(self) -> int:
        if not self._files or self._directory is None:
            return 0
        return (self._directory / self._files[-1]).stat().st_size

    def _name_for(self, sequence: int) -> str:
        return f"{FILE_PREFIX}{sequence:0{NAME_WIDTH}d}{FILE_SUFFIX}"

    def _unlink(self, name: str) -> None:
        if self._directory is None:
            return
        try:
            (self._directory / name).unlink()
        except OSError as exc:
            log.warning("could not evict archive file %s (%s)", name, exc)


def _sequence_after(names: list[str]) -> int:
    if not names:
        return 0
    digits = names[-1][len(FILE_PREFIX):len(FILE_PREFIX) + NAME_WIDTH]
    return int(digits) + 1
