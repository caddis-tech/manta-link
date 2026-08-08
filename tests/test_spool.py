"""The queue that has to survive a power cut and never slow the capture path.

Two of these tests exist because of specific prior-art bugs: the wall-clock
filename prefix that let an NTP step backwards invert eviction order, and the
per-reading glob of up to 60,000 files on the capture thread. One exists because
of a credential: .env lives on the extension volume, and the startup scan must
never enumerate the directory holding it.
"""

import json
import os
from pathlib import Path

import pytest

from manta_link import spool
from manta_link.health import Counters
from manta_link.spool import Spool


@pytest.fixture
def counters():
    return Counters()


@pytest.fixture
def store(tmp_path, counters):
    opened = Spool(tmp_path / "spool", counters, max_entries=4)
    opened.open()
    return opened


def envelope(index: int) -> dict:
    return {"client_ref": f"ref-{index}", "timestamp_ms": index, "payload": {}}


class TestPlacement:
    def test_the_extension_volume_gets_a_subdirectory_never_itself(self):
        """.env holds the API token and lives in the volume root. The startup
        index scan must never enumerate a directory holding a credential."""
        volume = Path("/app/data")
        directory, _ = spool.choose_directory(None, volume)
        assert directory == volume / "spool"
        assert directory != volume

    def test_the_volume_cap_is_tight_and_the_device_cap_is_not(self):
        volume_dir, volume_cap = spool.choose_directory(None, Path("/app/data"))
        device_dir, device_cap = spool.choose_directory(
            Path("/media/stick/aquadrone"), Path("/app/data")
        )
        assert volume_cap < device_cap
        assert device_dir == Path("/media/stick/aquadrone/spool")

    def test_no_data_device_is_the_shipping_answer(self, monkeypatch, tmp_path):
        monkeypatch.setattr(spool, "MEDIA_ROOT", tmp_path / "media")
        assert spool.find_data_device() is None

    def test_a_stick_is_only_used_when_it_opts_in(self, monkeypatch, tmp_path):
        """/media holds whatever anyone plugs in. Writing 14 MB a day to a
        stranger's stick, or to a card BlueOS is using, is not ours to decide."""
        media = tmp_path / "media"
        (media / "SOMEONES_PHOTOS").mkdir(parents=True)
        monkeypatch.setattr(spool, "MEDIA_ROOT", media)
        assert spool.find_data_device() is None

        marker = media / "AQUADRONE1" / spool.DEVICE_MARKER
        marker.mkdir(parents=True)
        assert spool.find_data_device() == marker

    def test_a_configured_device_that_is_not_there_falls_back(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            assert spool.find_data_device(str(tmp_path / "nope")) is None
        assert "not a directory" in caplog.text


class TestTheTokenIsNeverEnumerated:
    def test_the_startup_scan_touches_only_the_spool_subdirectory(
        self, monkeypatch, tmp_path, counters
    ):
        volume = tmp_path / "app_data"
        volume.mkdir()
        (volume / ".env").write_text("AQUADRONE_API_TOKEN=not-a-real-token\n")

        directory, cap = spool.choose_directory(None, volume)
        scanned: list[Path] = []
        real_scandir = os.scandir

        def spy(path):
            scanned.append(Path(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", spy)
        opened = Spool(directory, counters, cap)
        opened.open()

        assert scanned == [directory]
        assert volume.resolve() not in [path.resolve() for path in scanned]
        assert ".env" not in opened.names()


class TestPutAndDrain:
    def test_an_entry_survives_a_round_trip(self, store):
        name = store.put(envelope(1))
        assert store.load(name) == envelope(1)

    def test_names_come_back_oldest_first(self, store):
        names = [store.put(envelope(index)) for index in range(3)]
        assert store.names() == names

    def test_removing_an_entry_takes_the_file_with_it(self, store):
        name = store.put(envelope(1))
        store.remove(name)
        assert store.names() == []
        assert not (store.directory / name).exists()

    def test_a_missing_file_is_forgotten_rather_than_retried(self, store):
        name = store.put(envelope(1))
        (store.directory / name).unlink()
        assert store.load(name) is None
        assert store.names() == []

    def test_an_unreadable_entry_is_discarded_and_counted(self, store, counters):
        name = store.put(envelope(1))
        (store.directory / name).write_bytes(b"{truncated")

        assert store.load(name) is None
        assert counters.get("spool_discarded") == 1
        assert store.names() == []

    def test_a_write_that_cannot_happen_is_counted_not_raised(self, store, counters):
        """Nothing in this process may end over a failed write. A record lost is
        bad; a process that will not run costs the port its owner."""
        assert store.put({"payload": {1, 2, 3}}) is None
        assert counters.get("spool_write_failures") == 1
        assert store.names() == []

    def test_a_failed_write_takes_its_partial_file_with_it(
        self, monkeypatch, store, counters
    ):
        """A disk that is merely full must not become a directory of orphans.

        Every attempt burns a fresh sequence number and the cap governs indexed
        entries only, so a failure repeating at the cycle rate leaves a distinct
        file each time, and only the next startup scan prunes them.
        """
        def no_space(_fd):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", no_space)

        for index in range(5):
            assert store.put(envelope(index)) is None

        assert list(os.scandir(store.directory)) == []
        assert counters.get("spool_write_failures") == 5

    def test_nothing_partial_is_ever_left_where_the_index_can_see_it(self, store):
        store.put(envelope(1))
        on_disk = sorted(entry.name for entry in os.scandir(store.directory))
        assert on_disk == store.names()


class TestEviction:
    def test_the_oldest_goes_first_and_is_counted(self, store, counters):
        names = [store.put(envelope(index)) for index in range(6)]

        assert store.names() == names[2:]
        assert counters.get("spool_evicted") == 2
        assert not (store.directory / names[0]).exists()

    def test_it_is_loud_because_it_is_data_the_api_will_never_see(
        self, store, caplog
    ):
        with caplog.at_level("WARNING"):
            for index in range(6):
                store.put(envelope(index))
        assert "evicting the oldest" in caplog.text

    def test_a_spool_over_cap_at_startup_is_trimmed_once(self, tmp_path, counters):
        directory = tmp_path / "spool"
        first = Spool(directory, counters, max_entries=10)
        first.open()
        for index in range(10):
            first.put(envelope(index))

        second = Spool(directory, counters, max_entries=3)
        second.open()

        assert len(second.names()) == 3
        assert counters.get("spool_evicted") == 7


class TestRebuildingTheIndex:
    def test_the_index_rebuilds_identically_from_the_directory(
        self, tmp_path, counters
    ):
        """kill -9 in the middle of a run. Nothing is held anywhere else, so the
        directory is the whole of the state."""
        directory = tmp_path / "spool"
        first = Spool(directory, counters, max_entries=10)
        first.open()
        expected = [first.put(envelope(index)) for index in range(5)]

        second = Spool(directory, counters, max_entries=10)
        second.open()

        assert second.names() == expected

    def test_the_sequence_continues_past_everything_on_disk(self, tmp_path, counters):
        """Monotonic across restarts without a counter file: every live entry is
        below the next number issued, whatever the wall clock does. A wall-clock
        prefix inverts eviction order on a single NTP step backwards, which
        throws away the newest readings first."""
        directory = tmp_path / "spool"
        first = Spool(directory, counters, max_entries=10)
        first.open()
        for index in range(3):
            first.put(envelope(index))

        second = Spool(directory, counters, max_entries=10)
        second.open()
        latest = second.put(envelope(99))

        assert latest is not None
        assert latest > max(second.names()[:-1])
        assert second.names() == sorted(second.names())

    def test_a_half_written_entry_is_pruned(self, tmp_path, counters):
        """A power cut mid-write. It was never indexed and was never complete."""
        directory = tmp_path / "spool"
        directory.mkdir(parents=True)
        partial = directory / ("000000000007.json" + spool.PARTIAL_SUFFIX)
        partial.write_bytes(b'{"client_ref": "hal')

        opened = Spool(directory, counters, max_entries=10)
        opened.open()

        assert not partial.exists()
        assert counters.get("spool_partials_pruned") == 1
        assert opened.names() == []

    def test_files_that_are_not_ours_are_left_alone(self, tmp_path, counters):
        directory = tmp_path / "spool"
        directory.mkdir(parents=True)
        stray = directory / "README.txt"
        stray.write_text("put here by a human\n")

        opened = Spool(directory, counters, max_entries=10)
        opened.open()

        assert opened.names() == []
        assert stray.exists()


class TestDurability:
    def test_an_entry_is_fsynced_before_it_is_indexed(
        self, monkeypatch, tmp_path, counters
    ):
        """The spool protects data in flight, which is the only copy that exists
        before the API has it."""
        synced: list[int] = []
        real_fsync = os.fsync
        monkeypatch.setattr(
            os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1]
        )

        opened = Spool(tmp_path / "spool", counters, max_entries=10)
        opened.open()
        opened.put(envelope(1))

        assert synced

    def test_the_line_on_disk_is_json_the_uploader_can_read(self, store):
        name = store.put(envelope(1))
        assert name is not None
        text = (store.directory / name).read_text(encoding="utf-8")
        assert json.loads(text) == envelope(1)
        assert text.endswith("\n")
