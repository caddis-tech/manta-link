"""The mapping, the client_ref, and the anchor's whole life.

Everything in this suite fails silently in production if it is wrong. A payload
in the Pico's key names is accepted by the API and reads back empty. A uuid4
client_ref uploads cleanly and makes the on-boat backups unpromotable. A stale
anchor stamps a run with times that look entirely reasonable. None of the three
raises anything anywhere, which is why they are tested this heavily.
"""

import json
import time
import uuid

import pytest

from manta_link import clock, record
from manta_link import reader as reader_mod
from manta_link.health import Counters
from manta_link.reader import SerialReader
from manta_link.record import (
    EMIT_LAG_MS,
    SOURCE_ANCHOR,
    SOURCE_PICO,
    Anchor,
    Recorder,
    Stamp,
)

from .fakes import FakeSerial, StopPlayback
from .golden import READING, SATURATED_READING

# The golden line's own values, so a test says what it means rather than
# repeating a literal that has to be kept in step with the file.
UPTIME_MS = 72_000
PICO_EPOCH_MS = 1_754_400_072_000
BOOT_EPOCH_MS = PICO_EPOCH_MS - UPTIME_MS

# A wall clock far enough from the Pico's stamp to be unmistakable in a failure.
PI_EPOCH_MS = 1_754_500_000_000

# Two Pico runs, named rather than generated, so a test that crosses the
# boundary says which side it is on.
RUN_A = "e0a1c5be-0000-4000-8000-00000000000a"
RUN_B = "e0a1c5be-0000-4000-8000-00000000000b"


@pytest.fixture
def counters():
    return Counters()


@pytest.fixture
def trusted_clock(monkeypatch):
    monkeypatch.setattr(clock, "clock_is_trustworthy", lambda: True)
    monkeypatch.setattr(clock, "epoch_ms_now", lambda: PI_EPOCH_MS)


@pytest.fixture
def unsynced_clock(monkeypatch):
    monkeypatch.setattr(clock, "clock_is_trustworthy", lambda: False)


def reading(**overrides) -> dict:
    """The golden record, with named fields replaced."""
    parsed = json.loads(READING)
    parsed.update(overrides)
    return parsed


def unstamped_line(sample_ms: int) -> bytes:
    """The wire line for an unsynced sample this far into the run.

    The firmware renders uptime as h:mm:ss and nothing else in an unsynced
    record carries time, so the sub-second part of sample_ms has nowhere to go.
    That is the collision below, modelled here rather than asserted about one
    byte string twice.
    """
    seconds = sample_ms // 1000
    stamp = f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    fields: dict = {"epoch_ms": None, "time": stamp}
    # AquadronePicoFirmware#28. The day the golden line carries raw milliseconds,
    # these samples stop colliding and the test below is what says so.
    if "ms_since_boot" in reading():
        fields["ms_since_boot"] = sample_ms
    return json.dumps(reading(**fields)).encode()


class RecordingSpool:
    """A spool that only remembers the order it was called in."""

    def __init__(self, calls: list) -> None:
        self.calls = calls
        self.entries: list[dict] = []

    def put(self, envelope: dict) -> str:
        self.calls.append("spool")
        self.entries.append(envelope)
        return "000000000000.json"


class RecordingArchive:
    def __init__(self, calls: list) -> None:
        self.calls = calls
        self.lines: list[dict] = []

    def append(self, envelope: dict) -> None:
        self.calls.append("archive")
        self.lines.append(envelope)


class TestFieldMapping:
    def test_the_golden_line_maps_to_exactly_this_payload(self):
        """The one assertion that stands between us and empty rows.

        caddis-api reads named properties over fixed keys, so a payload in the
        Pico's own key names is stored, returns `created`, and reads back with
        nothing in it.
        """
        assert record.to_payload(reading()) == {
            "type": "reading",
            "time": "0:01:12",
            "epoch_ms": PICO_EPOCH_MS,
            "sd_ready": 1,
            "sd_writes_failed": 0,
            "sd_mounts_failed": 0,
            "cond_tds_sal": "210,105,0.10",
            "conductivity": 210.0,
            "tds": 105.0,
            "salinity": 0.1,
            "ph": 7.234,
            "temp_code": "0x1a2b",
            "water_temperature": 18.4,
            "uv_counts": 812,
            "uv_mv": 654,
            "uv_index": 5,
            "uv_saturated": False,
        }

    def test_the_saturated_line_keeps_its_null_and_its_boolean(self):
        """A UV reading off the top of the ladder is the one record where a
        mapped field is a JSON boolean and another is an explicit null. Neither
        goes through _as_number, and a coercion added there later would turn
        the boolean into a null and lose the distinction the firmware is at
        pains to keep."""
        payload = record.to_payload(json.loads(SATURATED_READING))
        assert payload["uv_index"] is None
        assert payload["uv_saturated"] is True
        assert payload["uv_mv"] == 1128

    def test_temperature_is_renamed_rather_than_duplicated(self):
        payload = record.to_payload(reading())
        assert payload["water_temperature"] == 18.4
        assert "temperature" not in payload

    def test_battery_level_is_never_emitted(self):
        """The API's property turns an explicit null into a zero.

        A boat reporting a flat battery it does not have is worse than one
        reporting no battery at all (caddis-api#77).
        """
        assert "battery_level" not in record.to_payload(reading(battery_level=None))

    def test_ph_as_a_string_becomes_a_number(self):
        assert record.to_payload(reading(ph="7.234"))["ph"] == 7.234

    def test_ph_already_numeric_is_left_alone(self):
        assert record.to_payload(reading(ph=6.5))["ph"] == 6.5

    def test_ph_null_stays_null(self):
        assert record.to_payload(reading(ph=None))["ph"] is None

    def test_an_uncoercible_ph_is_null_but_the_original_is_kept(self):
        payload = record.to_payload(reading(ph="*ER"))
        assert payload["ph"] is None
        assert payload["ph_raw"] == "*ER"

    def test_cond_tds_sal_splits_into_three_numbers(self):
        payload = record.to_payload(reading(cond_tds_sal="1413,707,0.70"))
        assert (payload["conductivity"], payload["tds"], payload["salinity"]) == (
            1413.0, 707.0, 0.7
        )

    def test_a_null_cond_tds_sal_gives_three_nulls(self):
        payload = record.to_payload(reading(cond_tds_sal=None))
        assert (payload["conductivity"], payload["tds"], payload["salinity"]) == (
            None, None, None
        )

    def test_a_missing_cond_tds_sal_gives_three_nulls(self):
        parsed = reading()
        del parsed["cond_tds_sal"]
        payload = record.to_payload(parsed)
        assert (payload["conductivity"], payload["tds"], payload["salinity"]) == (
            None, None, None
        )

    def test_a_short_cond_tds_sal_yields_nothing_rather_than_a_guess(self):
        """Two parts could be cond and tds, or tds and sal. Guessing there puts
        a salinity in the conductivity column, which nothing downstream can
        detect."""
        payload = record.to_payload(reading(cond_tds_sal="210,105"))
        assert payload["conductivity"] is None
        assert payload["cond_tds_sal"] == "210,105"

    def test_a_non_numeric_part_is_null_and_the_others_survive(self):
        payload = record.to_payload(reading(cond_tds_sal="210,*ER,0.10"))
        assert payload["conductivity"] == 210.0
        assert payload["tds"] is None
        assert payload["salinity"] == 0.1

    def test_the_raw_string_is_kept_beside_the_split_numbers(self):
        """Nothing reads it, and it is the only evidence of what arrived."""
        assert record.to_payload(reading())["cond_tds_sal"] == "210,105,0.10"

    def test_keys_nothing_can_read_are_still_carried(self):
        payload = record.to_payload(reading())
        for key in ("uv_index", "uv_saturated", "time", "epoch_ms", "temp_code"):
            assert key in payload


class TestMappingRot:
    def test_every_key_in_the_golden_line_is_one_this_mapping_knows(self):
        """The tripwire. A new firmware field arrives as an unknown key here.

        Without this, a field the Pico starts emitting lands in the blob and is
        invisible to every consumer, with nothing anywhere to say so.
        """
        assert record.unknown_keys(json.loads(READING)) == []

    def test_a_new_key_is_named_rather_than_passed_through_quietly(self):
        assert record.unknown_keys(reading(ms_since_boot=72_123)) == ["ms_since_boot"]

    def test_an_unknown_key_is_counted_and_logged(self, counters, caplog):
        calls: list[str] = []
        recorder = Recorder(RecordingSpool(calls), RecordingArchive(calls), counters)

        with caplog.at_level("WARNING"):
            recorder.capture(READING, reading(turbidity=4.2), time.monotonic())

        assert counters.get("payload_keys_unknown") == 1
        assert "turbidity" in caplog.text


class TestClientRef:
    def test_it_is_the_same_across_two_independent_derivations(self):
        """The whole point: the live upload, a retry, the archive stick and the
        card all have to resolve to one row."""
        assert record.client_ref_for(READING) == record.client_ref_for(READING)

    def test_it_parses_as_a_real_uuid(self):
        """Probe-confirmed: a non-UUID string is rejected with
        {"client_ref": ["Must be a valid UUID."]} before payload validation."""
        assert str(uuid.UUID(record.client_ref_for(READING))) == record.client_ref_for(
            READING
        )

    def test_different_lines_get_different_refs(self):
        other = json.dumps(reading(uv_counts=813)).encode()
        assert record.client_ref_for(other) != record.client_ref_for(READING)

    def test_the_line_terminator_cannot_change_it(self):
        assert record.client_ref_for(READING + b"\r\n") == record.client_ref_for(
            READING
        )

    def test_two_unstamped_samples_inside_one_second_collide(self):
        """The known collision, asserted rather than trusted to the cadence.

        Two readings 400 ms apart with no epoch_ms carry the same
        second-truncated uptime and nothing else that tells them apart, so they
        are one row: the second comes back `duplicate` and is lost. The 2.5s
        cycle floor makes that unreachable today, but #44 (re-measure cadence)
        and #28 (raw ms_since_boot) both move it, and this is where that shows
        up.
        """
        assert record.client_ref_for(unstamped_line(72_000)) == record.client_ref_for(
            unstamped_line(72_400)
        )

    def test_unstamped_samples_a_second_apart_stay_two_rows(self):
        assert record.client_ref_for(unstamped_line(72_000)) != record.client_ref_for(
            unstamped_line(73_000)
        )

    def test_an_envelope_field_cannot_change_it(self):
        """Derived from the raw line, never the envelope. GPS and the Pi's
        receipt time differ between a live send and a backfill, and an
        envelope-derived ref would make those two different rows."""
        live = record.build_envelope(
            READING, reading(), Stamp(PICO_EPOCH_MS, SOURCE_PICO, UPTIME_MS), RUN_A
        )
        backfilled = record.build_envelope(
            READING,
            reading(),
            Stamp(PICO_EPOCH_MS, SOURCE_PICO, UPTIME_MS),
            RUN_B,
            position={"lat": 44.1, "lon": -70.2},
        )
        assert live["client_ref"] == backfilled["client_ref"]


class TestTimestampPrecedence:
    def test_the_picos_own_stamp_wins(self):
        stamp = record.resolve_stamp(reading(), UPTIME_MS, anchor_ms=1)
        assert stamp.timestamp_ms == PICO_EPOCH_MS
        assert stamp.source == SOURCE_PICO

    def test_an_anchor_fills_the_gap_when_the_pico_has_none(self):
        stamp = record.resolve_stamp(
            reading(epoch_ms=None), UPTIME_MS, anchor_ms=BOOT_EPOCH_MS
        )
        assert stamp.timestamp_ms == PICO_EPOCH_MS
        assert stamp.source == SOURCE_ANCHOR

    def test_neither_leaves_the_record_unstamped(self):
        stamp = record.resolve_stamp(reading(epoch_ms=None), UPTIME_MS, anchor_ms=None)
        assert stamp.timestamp_ms is None
        assert stamp.source is None

    def test_a_zero_epoch_is_not_a_stamp(self):
        """The firmware writes null for an unsynced run, so a zero is a firmware
        that changed its mind, not a reading from 1970."""
        assert record.pico_epoch_ms(reading(epoch_ms=0)) is None

    def test_disagreement_is_reported_and_not_corrected(self, counters, caplog):
        calls: list[str] = []
        spool = RecordingSpool(calls)
        recorder = Recorder(spool, RecordingArchive(calls), counters)
        # An anchor an hour off, held from the record one cycle earlier.
        recorder.anchor.for_record(
            UPTIME_MS - 2_500, PICO_EPOCH_MS - 3_600_000, time.monotonic()
        )

        with caplog.at_level("WARNING"):
            recorder.capture(READING, reading(), time.monotonic())

        assert counters.get("timestamp_disagreements") == 1
        assert "disagree" in caplog.text
        assert spool.entries[0]["timestamp_ms"] == PICO_EPOCH_MS
        assert spool.entries[0]["timestamp_source"] == SOURCE_PICO


class TestDrainability:
    def test_an_unstamped_entry_is_not_drainable(self):
        envelope = record.build_envelope(
            READING, reading(epoch_ms=None), Stamp(None, None, UPTIME_MS), RUN_A
        )
        assert not record.is_drainable(envelope)

    def test_the_same_entry_becomes_drainable_unchanged_once_an_anchor_arrives(self):
        """A gate, not a correction. The API is create-only, so a resubmitted
        client_ref returns `duplicate` and never an update: a timestamp sent
        wrong the first time stays wrong."""
        spooled = record.build_envelope(
            READING, reading(epoch_ms=None), Stamp(None, None, UPTIME_MS), RUN_A
        )

        stamped = record.stamp_with_anchor(spooled, BOOT_EPOCH_MS, RUN_A)

        assert record.is_drainable(stamped)
        assert stamped["timestamp_ms"] == PICO_EPOCH_MS
        assert stamped["timestamp_source"] == SOURCE_ANCHOR
        assert stamped["client_ref"] == spooled["client_ref"]
        assert stamped["payload"] == spooled["payload"]
        # The spooled copy is untouched, so nothing had to be rewritten on disk.
        assert spooled["timestamp_ms"] is None

    def test_an_anchor_never_overrides_a_stamp_the_pico_gave(self):
        spooled = record.build_envelope(
            READING, reading(), Stamp(PICO_EPOCH_MS, SOURCE_PICO, UPTIME_MS), RUN_A
        )
        assert record.stamp_with_anchor(spooled, 1, RUN_A) is spooled

    def test_an_entry_with_no_uptime_can_never_be_stamped(self):
        envelope = record.build_envelope(
            READING, reading(epoch_ms=None, time="junk"), Stamp(None, None, None), RUN_A
        )
        stamped = record.stamp_with_anchor(envelope, BOOT_EPOCH_MS, RUN_A)
        assert not record.is_drainable(stamped)

    def test_an_anchor_from_a_later_run_leaves_an_older_entry_alone(self):
        """The failure this whole field exists for.

        A run whose Pi clock was undisciplined spools unstamped entries; the
        Pico resets; the clock syncs and the next run derives an anchor. Stamped
        from that one, every entry of the first run shifts forward by the gap
        between the two boots, and the result is plausible, drainable and
        permanent.
        """
        spooled = record.build_envelope(
            READING, reading(epoch_ms=None), Stamp(None, None, UPTIME_MS), RUN_A
        )

        stamped = record.stamp_with_anchor(spooled, BOOT_EPOCH_MS + 86_400_000, RUN_B)

        assert stamped is spooled
        assert not record.is_drainable(stamped)

    def test_an_entry_spooled_before_this_format_is_never_stamped(self):
        """A format without a run id cannot say which run it is from, so the
        only honest answer is the give-up rule: it stays spooled and the Pico's
        card is the copy that survives."""
        older = {"client_ref": "x", "timestamp_ms": None, "uptime_ms": UPTIME_MS}
        assert record.stamp_with_anchor(older, BOOT_EPOCH_MS, RUN_A) is older


class TestAnchorDerivation:
    def test_the_picos_own_stamp_anchors_the_run_exactly(self, counters):
        anchor = Anchor(counters)
        assert anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic()) == (
            BOOT_EPOCH_MS
        )
        assert counters.get("anchor_derived") == 1

    def test_the_pi_clock_derivation_is_the_reply_minus_uptime_and_lag(
        self, counters, trusted_clock
    ):
        anchor = Anchor(counters)
        derived = anchor.for_record(UPTIME_MS, None, time.monotonic())
        assert derived == pytest.approx(
            PI_EPOCH_MS - UPTIME_MS - EMIT_LAG_MS, abs=50
        )

    def test_it_uses_the_receipt_monotonic_rather_than_the_clock_at_parse(
        self, counters, trusted_clock
    ):
        """The backlog is deepest exactly when a late NTP sync fires.

        A naive wall_now - uptime is late by however long the record sat in the
        buffer, and the error is systematic, so it would skew an entire
        deployment the same way rather than averaging out.
        """
        backlog_s = 3.0
        fresh = Anchor(counters).for_record(UPTIME_MS, None, time.monotonic())
        backlogged = Anchor(counters).for_record(
            UPTIME_MS, None, time.monotonic() - backlog_s
        )
        assert fresh is not None and backlogged is not None
        assert fresh - backlogged == pytest.approx(backlog_s * 1000, abs=50)

    def test_an_unsynced_pi_derives_nothing(self, counters, unsynced_clock):
        assert Anchor(counters).for_record(UPTIME_MS, None, time.monotonic()) is None
        assert counters.get("anchor_derived") == 0


class TestAnchorInvalidation:
    def test_a_serial_reconnect_drops_it(self, counters, unsynced_clock):
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())

        anchor.note_serial_reconnect()

        assert anchor.for_record(UPTIME_MS, None, time.monotonic()) is None
        assert counters.get("anchor_dropped") == 1

    def test_the_banner_drops_it_too(self, counters, unsynced_clock):
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())

        anchor.note_banner()

        assert anchor.for_record(UPTIME_MS, None, time.monotonic()) is None

    def test_an_equal_uptime_after_a_reset_still_drops_it(
        self, counters, unsynced_clock
    ):
        """Not "strictly lower". Uptime is second-truncated on a deterministic
        boot sequence, so a reset after exactly one record reproduces the value
        we already saw, and equal is not lower."""
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())

        assert anchor.for_record(UPTIME_MS, None, time.monotonic()) is None
        assert counters.get("anchor_dropped") == 1

    def test_uptime_going_backwards_drops_it(self, counters, unsynced_clock):
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())
        assert anchor.for_record(1_000, None, time.monotonic()) is None

    def test_an_implausible_forward_jump_drops_it(self, counters, unsynced_clock):
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())
        assert anchor.for_record(
            UPTIME_MS + record.UPTIME_JUMP_MAX_MS + 1, None, time.monotonic()
        ) is None

    def test_the_ordinary_cycle_keeps_it(self, counters, unsynced_clock):
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())
        assert anchor.for_record(UPTIME_MS + 2_500, None, time.monotonic()) == (
            BOOT_EPOCH_MS
        )
        assert counters.get("anchor_dropped") == 0

    def test_after_a_drop_it_re_derives_rather_than_waiting(
        self, counters, trusted_clock
    ):
        """Waiting for the next banner or sync line deadlocks in the case that
        matters: a run whose Pi clock was still undisciplined at boot emits
        neither. The 49.7 day uint32_t wrap is the same shape, with no reset
        behind it at all."""
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())

        anchor.note_serial_reconnect()
        re_derived = anchor.for_record(5_000, None, time.monotonic())

        assert re_derived == pytest.approx(PI_EPOCH_MS - 5_000 - EMIT_LAG_MS, abs=50)
        assert counters.get("anchor_dropped") == 1
        assert counters.get("anchor_derived") == 2

    def test_uptimes_either_side_of_a_reconnect_are_not_compared(
        self, counters, trusted_clock
    ):
        """A reconnect already dropped the anchor. The record after it must not
        drop the freshly derived one for regressing against a run that ended."""
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())

        anchor.note_serial_reconnect()
        anchor.for_record(1_000, None, time.monotonic())

        assert counters.get("anchor_dropped") == 1
        assert anchor.value is not None


class TestRunIdentity:
    """Which boot an uptime counts from, which is the only thing that makes a
    deferred stamp safe."""

    def test_the_ordinary_cycle_stays_one_run(self, counters, unsynced_clock):
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())
        before = anchor.run_id

        anchor.for_record(UPTIME_MS + 2_500, None, time.monotonic())

        assert anchor.run_id == before

    def test_a_reconnect_starts_a_new_run(self, counters, unsynced_clock):
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())
        before = anchor.run_id

        anchor.note_serial_reconnect()
        anchor.for_record(1_000, None, time.monotonic())

        assert anchor.run_id != before

    def test_a_run_that_never_anchored_still_ends(self, counters, unsynced_clock):
        """The case the anchor's own drop rule cannot see. A run with an
        undisciplined Pi clock holds no anchor to lose, and its records are
        exactly the ones the next run's anchor must not reach."""
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, None, time.monotonic())
        assert anchor.value is None
        before = anchor.run_id

        anchor.note_serial_reconnect()
        anchor.for_record(1_000, None, time.monotonic())

        assert anchor.run_id != before

    def test_uptime_going_backwards_ends_it_too(self, counters, unsynced_clock):
        """The backstop for a reset the reconnect somehow missed."""
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, None, time.monotonic())
        before = anchor.run_id

        anchor.for_record(1_000, None, time.monotonic())

        assert anchor.run_id != before

    def test_two_processes_never_share_one(self, counters):
        """A restart mints a new identity even where the Pico kept running, so
        entries spooled unstamped before it are given up rather than stamped
        from an anchor nothing can prove is theirs."""
        assert Anchor(counters).run_id != Anchor(counters).run_id


class TestTheReaderDrivesTheAnchor:
    def test_a_reconnect_drops_it_with_no_banner_anywhere(self, monkeypatch, counters):
        """The detector that cannot be missed.

        Any Pico reset forces a USB re-enumeration, so read-error-then-reopen
        catches every reset. The banner does not: the Pico prints it two seconds
        after boot without waiting for a host, and pico_stdio_usb discards
        output outright while DTR is deasserted, which is the state during the
        re-enumeration a reset causes.
        """
        monkeypatch.setattr(clock, "clock_is_trustworthy", lambda: False)
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())

        drive_one_reconnect(monkeypatch, anchor)

        assert anchor.for_record(UPTIME_MS + 2_500, None, time.monotonic()) is None
        assert counters.get("anchor_dropped") == 1

    def test_a_banner_on_the_wire_drops_it(self, monkeypatch, counters):
        monkeypatch.setattr(clock, "clock_is_trustworthy", lambda: False)
        anchor = Anchor(counters)
        anchor.for_record(UPTIME_MS, PICO_EPOCH_MS, time.monotonic())

        factory = FakeSerial.factory(
            chunks=[b"=== Aquadrone firmware 2.0.0: SD LOGGING ENABLED ===\r\n"]
        )
        monkeypatch.setattr(reader_mod.serial, "Serial", factory)
        reader = SerialReader(on_banner=anchor.note_banner)
        with pytest.raises(StopPlayback):
            reader._serve("/dev/fake")

        assert anchor.for_record(UPTIME_MS + 2_500, None, time.monotonic()) is None


class TestRecorder:
    def test_the_archive_is_written_before_the_spool(self, counters):
        """Ordered so a failed spool write still leaves the position on the
        stick. Step 5 keeps the same order on the acknowledgement path, where
        the append has to precede the spool unlink."""
        calls: list[str] = []
        recorder = Recorder(RecordingSpool(calls), RecordingArchive(calls), counters)

        recorder.capture(READING, reading(), time.monotonic())

        assert calls == ["archive", "spool"]

    def test_the_archive_is_written_with_no_token_and_no_uploader_anywhere(
        self, counters
    ):
        """The archive is never ack-coupled. A boat with no token is exactly the
        configuration where a local copy of the position is worth most, and
        coupling would leave the ring permanently empty there."""
        calls: list[str] = []
        ring = RecordingArchive(calls)
        recorder = Recorder(RecordingSpool(calls), ring, counters)

        recorder.capture(READING, reading(), time.monotonic())

        assert len(ring.lines) == 1
        assert ring.lines[0]["payload"]["water_temperature"] == 18.4

    def test_an_unstamped_record_is_spooled_anyway(self, counters, unsynced_clock):
        calls: list[str] = []
        spool = RecordingSpool(calls)
        recorder = Recorder(spool, RecordingArchive(calls), counters)

        recorder.capture(
            json.dumps(reading(epoch_ms=None)).encode(),
            reading(epoch_ms=None),
            time.monotonic(),
        )

        assert spool.entries[0]["timestamp_ms"] is None
        assert spool.entries[0]["uptime_ms"] == UPTIME_MS
        assert counters.get("timestamps_absent") == 1

    def test_an_entry_carries_the_run_its_uptime_counts_from(
        self, counters, unsynced_clock
    ):
        """Read after the anchor has seen the record, not before: a record on
        the far side of a reset belongs to the run that started there."""
        calls: list[str] = []
        spool = RecordingSpool(calls)
        recorder = Recorder(spool, RecordingArchive(calls), counters)
        line = json.dumps(reading(epoch_ms=None)).encode()

        recorder.capture(line, reading(epoch_ms=None), time.monotonic())
        first_run = recorder.anchor.run_id
        recorder.anchor.note_serial_reconnect()
        recorder.capture(line, reading(epoch_ms=None), time.monotonic())

        assert spool.entries[0]["run_id"] == first_run
        assert spool.entries[1]["run_id"] == recorder.anchor.run_id
        assert spool.entries[1]["run_id"] != first_run

    def test_a_partly_unparseable_cond_tds_sal_is_counted(self, counters):
        """An Atlas *ER in the middle position leaves conductivity intact and
        drops tds, so a counter keyed to the first part alone never fires and
        the record uploads with no evidence a value was lost."""
        calls: list[str] = []
        recorder = Recorder(RecordingSpool(calls), RecordingArchive(calls), counters)

        recorder.capture(
            READING, reading(cond_tds_sal="210,*ER,0.10"), time.monotonic()
        )

        assert counters.get("cond_tds_sal_unparseable") == 1

    def test_a_wholly_readable_cond_tds_sal_is_not_counted(self, counters):
        calls: list[str] = []
        recorder = Recorder(RecordingSpool(calls), RecordingArchive(calls), counters)

        recorder.capture(READING, reading(), time.monotonic())

        assert counters.get("cond_tds_sal_unparseable") == 0

    def test_the_stamp_source_is_counted(self, counters):
        calls: list[str] = []
        recorder = Recorder(RecordingSpool(calls), RecordingArchive(calls), counters)
        recorder.capture(READING, reading(), time.monotonic())
        assert counters.get("timestamps_from_pico") == 1


class TestUptime:
    def test_it_is_read_from_the_h_mm_ss_the_pico_prints(self):
        assert record.uptime_ms_from({"time": "1:02:03"}) == 3_723_000

    @pytest.mark.parametrize(
        "value", ["", "junk", "1:02", "1:2:3:4", "a:b:c", "0:99:00", None, 72],
    )
    def test_anything_else_is_no_uptime_at_all(self, value):
        assert record.uptime_ms_from({"time": value}) is None


class _NoSleep:
    """time, minus the reconnect pause, so a test is not two seconds long."""

    monotonic = staticmethod(time.monotonic)

    @staticmethod
    def sleep(_seconds: float) -> None:
        return None


def drive_one_reconnect(monkeypatch, anchor: Anchor) -> None:
    """One serve that dies mid-read, then no port at all. No banner is sent."""
    ports = iter(["/dev/fake"])

    def next_port():
        try:
            return next(ports)
        except StopIteration:
            raise StopPlayback from None

    monkeypatch.setattr(reader_mod, "time", _NoSleep)
    monkeypatch.setattr(reader_mod, "find_pico_port", next_port)
    monkeypatch.setattr(
        reader_mod.serial, "Serial", FakeSerial.factory(raise_on_read=1)
    )

    reader = SerialReader(on_reconnect=anchor.note_serial_reconnect)
    with pytest.raises(StopPlayback):
        reader.run_forever()
