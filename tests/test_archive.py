"""The on-boat copy, which ships switched off.

No boat has a stick yet, so the shipping configuration is the disabled one and
it gets tested first. The rest of the suite is about the three properties that
keep the archive from becoming a liability: it is never fsynced, it is never
coupled to an acknowledgement, and it can never raise into a caller.
"""

import json
import os
from pathlib import Path

import pytest

from manta_link import archive
from manta_link.archive import Archive
from manta_link.health import Counters


@pytest.fixture
def counters():
    return Counters()


@pytest.fixture
def ring(tmp_path, counters):
    opened = Archive(tmp_path / "archive", counters, max_files=3, file_max_bytes=200)
    opened.open()
    return opened


def envelope(index: int) -> dict:
    return {
        "client_ref": f"ref-{index}",
        "timestamp_ms": 1_754_400_000_000 + index,
        "position": {"lat": 44.1039, "lon": -70.2148},
        "payload": {"water_temperature": 18.4},
    }


def lines_in(directory: Path) -> list[dict]:
    found = []
    for name in sorted(entry.name for entry in os.scandir(directory)):
        for line in (directory / name).read_text(encoding="utf-8").splitlines():
            found.append(json.loads(line))
    return found


class TestShippedDisabled:
    def test_no_data_device_means_no_archive(self, counters, caplog):
        with caplog.at_level("INFO"):
            off = Archive(archive.choose_directory(None), counters)
            off.open()

        assert not off.enabled
        assert counters.get("archive_disabled") == 1
        assert "the telemetry archive is off" in caplog.text

    def test_a_disabled_archive_still_accepts_everything_handed_to_it(self, counters):
        """Handled like a missing token: off, counted, and never fatal."""
        off = Archive(None, counters)
        off.open()
        off.append(envelope(1))
        assert counters.get("archive_lines") == 0

    def test_a_directory_that_cannot_be_made_disables_it_rather_than_raising(
        self, tmp_path, counters
    ):
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("in the way\n")

        off = Archive(blocked / "archive", counters)
        off.open()

        assert not off.enabled
        assert counters.get("archive_disabled") == 1


class TestAppending:
    def test_a_line_carries_the_position_the_pico_never_had(self, ring, tmp_path):
        """The whole reason this exists beside the Pico's card, which holds
        every other field on the line and not this one."""
        ring.append(envelope(1))
        written = lines_in(tmp_path / "archive")
        assert written == [envelope(1)]

    def test_lines_are_one_per_record(self, ring, tmp_path, counters):
        for index in range(3):
            ring.append(envelope(index))
        assert len(lines_in(tmp_path / "archive")) == 3
        assert counters.get("archive_lines") == 3

    def test_it_is_never_fsynced(self, monkeypatch, ring):
        """A second copy of data the spool has already made durable. Paying for
        durability twice on the capture worker's thread buys nothing."""
        synced: list[int] = []
        monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))

        ring.append(envelope(1))

        assert synced == []

    def test_it_cannot_raise_into_its_caller(self, ring, tmp_path, counters, caplog):
        """On the acknowledgement path this runs on the uploader thread, where
        an unguarded exception unwinds into the supervisor and re-POSTs a record
        the API has already taken."""
        for entry in os.scandir(tmp_path / "archive"):
            os.unlink(entry.path)
        (tmp_path / "archive").rmdir()

        with caplog.at_level("WARNING"):
            ring.append(envelope(1))

        assert counters.get("archive_failures") == 1
        assert "could not append to the archive" in caplog.text

    def test_an_unserialisable_envelope_is_counted_not_raised(self, ring, counters):
        ring.append({"payload": {1, 2, 3}})
        assert counters.get("archive_failures") == 1


class TestTheRing:
    def test_it_rotates_when_a_file_is_full(self, ring, tmp_path, counters):
        for index in range(4):
            ring.append(envelope(index))

        assert counters.get("archive_rotations") >= 1
        assert len(list(os.scandir(tmp_path / "archive"))) >= 2

    def test_the_oldest_file_goes_when_the_ring_is_full(
        self, ring, tmp_path, counters
    ):
        for index in range(40):
            ring.append(envelope(index))

        assert len(list(os.scandir(tmp_path / "archive"))) <= 3
        assert counters.get("archive_evicted") > 0

    def test_eviction_never_walks_the_directory(self, monkeypatch, ring, counters):
        """It rides on rotation, so it is one unlink every full file rather than
        anything per record on the capture worker's thread."""
        scanned: list[str] = []
        real_scandir = os.scandir

        def spy(path):
            scanned.append(str(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", spy)
        for index in range(40):
            ring.append(envelope(index))

        assert scanned == []

    def test_it_picks_up_where_a_previous_run_left_off(self, tmp_path, counters):
        directory = tmp_path / "archive"
        first = Archive(directory, counters, max_files=3, file_max_bytes=200)
        first.open()
        for index in range(4):
            first.append(envelope(index))
        before = sorted(entry.name for entry in os.scandir(directory))

        second = Archive(directory, counters, max_files=3, file_max_bytes=200)
        second.open()
        second.append(envelope(99))

        assert second.enabled
        after = sorted(entry.name for entry in os.scandir(directory))
        assert after[: len(before)] == before
        assert lines_in(directory)[-1] == envelope(99)
