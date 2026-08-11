"""What a reading becomes, and whose clock it is stamped by.

The Pico is the source of truth on timestamps. This module fills gaps and never
overrides: the Pico stamps at sampling time, and we can only stamp at receipt,
one to three seconds later.

It also transforms rather than copies. caddis-api stores the payload as a bare
JSONField and keeps whatever it is handed, but persisted is not readable: named
properties over fixed keys are the de-facto schema for the admin, the dashboards
and every consumer. A verbatim copy of the Pico's keys returns `created` and
reads back substantively empty, and nothing in the write path says so. That
failure is silent at every step, which is why the mapping below is the most
heavily tested code in the package.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, TypeGuard

from . import clock
from .archive import Archive
from .health import Counters
from .logging_setup import Throttle
from .spool import Spool

log = logging.getLogger(__name__)

SOURCE_PICO = "pico"
SOURCE_ANCHOR = "link_anchor"

# uuid5 of "manta-link.caddis.tech" under the DNS namespace, frozen as a literal
# so the derivation can never drift. Every copy of one reading has to resolve to
# the same API row: the live upload, a retry after a lost acknowledgement, the
# archive stick and the Pico's card. uuid4 makes each of those a new row, which
# is what leaves the on-boat backups unpromotable.
DEVICE_NAMESPACE = uuid.UUID("72908e53-703b-510b-9c7b-8e51084912e2")

# The firmware samples and then emits one to three seconds later, so an anchor
# derived from the Pi's clock is late by that much. Two seconds is the middle of
# the range: it bounds the error at a second either way instead of leaving it up
# to three seconds one way, and this error is systematic, so an uncorrected one
# skews an entire deployment in the same direction.
EMIT_LAG_MS = 2000

# Uptime is second-truncated, so consecutive records at the 2.5s cycle always
# rise. Anything that does not rise is a reset. Anything this far ahead is not
# the run we anchored to: buffer drops and stalled workers cost minutes, not
# hours. Both are false-positive tolerant, because a drop only costs a
# re-derivation from the very next record.
UPTIME_JUMP_MAX_MS = 3_600_000

# Second-truncated uptime plus the emit-lag estimate makes a few seconds of
# disagreement expected. Past this the two clocks are telling different stories,
# and the difference is reported rather than corrected: the Pico's value stands.
DISAGREEMENT_TOLERANCE_MS = 10_000

ANCHOR_LOG_INTERVAL_S = 60.0
MAPPING_LOG_INTERVAL_S = 60.0

KIND_BOOT = "boot"

# Every key a reading is known to carry. A key outside this set still reaches
# the payload, but it is counted and named in the log, because an unrecognised
# key is the shape a new firmware field arrives in and the mapping below is the
# only thing standing between one and a column nobody can read.
#
# `truncated` is not a measurement: the firmware appends it when a field did not
# fit the record buffer, so it can turn up on either kind of record.
PICO_KEYS = frozenset({
    "type",
    "firmware_version",
    "time",
    "epoch_ms",
    "boot_epoch_ms",
    "sd_ready",
    "sd_writes_failed",
    "sd_mounts_failed",
    "cond_tds_sal",
    "ph",
    "temp_code",
    "temperature",
    "uv_present",
    "uv_counts",
    "uv_mv",
    "uv_index",
    "uv_saturated",
    "truncated",
})

# The startup record's own fields, which are almost none of a reading's. Measured
# against PICO_KEYS it reports six perfectly ordinary boot fields as unknown, and
# a detector that fires on every run is one nobody reads by the time a genuinely
# new field arrives.
BOOT_KEYS = frozenset({
    "type",
    "firmware_version",
    "sd_logging",
    "ph_status",
    "cond_status",
    "temp_probes",
    "uv_probe",
    "boot_epoch_ms",
    "truncated",
})

# Keys that must not survive into the payload under their own name.
# `temperature` becomes `water_temperature`, which is the property the API
# actually reads.
#
# `battery_level` used to be stripped here too, because the API's property
# turned an explicit null into a zero and a boat reporting a flat battery it
# does not have is worse than one reporting nothing. caddis-api#77 fixed that
# property, so stripping now only means a battery reading would be discarded in
# silence if the firmware ever started sending one.
REPLACED_KEYS = frozenset({"temperature"})


@dataclass(frozen=True)
class Stamp:
    """What we believe about a record's absolute time, and on whose authority."""

    timestamp_ms: int | None
    source: str | None
    uptime_ms: int | None


@dataclass(frozen=True)
class AnchorState:
    """An anchor and the run it describes, which are never separately true.

    Frozen and rebound whole, so a reader on another thread gets one or the
    other and never a mixture. Read as two attributes these tear: the drop path
    has to rotate the run identity and clear the epoch, and between those two
    stores the pair reads as this run's id beside the previous run's epoch.
    `stamp_with_anchor` cannot catch that, because the tear is in the input its
    own run gate compares.
    """

    value: int | None
    run_id: str


def client_ref_for(raw_line: bytes) -> str:
    """The row this reading is, in every copy of it that will ever exist.

    Derived from the raw Pico line rather than the envelope, because the
    envelope carries GPS and a Pi timestamp that differ between a live send and
    a backfill from the card, and an envelope-derived ref would defeat the whole
    point. Whitespace is stripped first so a CRLF run and an LF run of the same
    firmware cannot disagree.

    It has to parse as a real UUID: a non-UUID string is rejected with
    {"client_ref": ["Must be a valid UUID."]} before payload validation is even
    reached.
    """
    # backslashreplace rather than replace: it is injective, so two lines that
    # differ only in bytes a lossy decoder would flatten still get two refs.
    text = raw_line.strip().decode("ascii", "backslashreplace")
    return str(uuid.uuid5(DEVICE_NAMESPACE, text))


def to_payload(record: dict[str, Any]) -> dict[str, Any]:
    """The Pico's record in the key names caddis-api can actually read."""
    payload = {
        key: value for key, value in record.items() if key not in REPLACED_KEYS
    }

    conductivity, tds, salinity = _split_cond_tds_sal(record.get("cond_tds_sal"))
    payload["conductivity"] = conductivity
    payload["tds"] = tds
    payload["salinity"] = salinity
    payload["water_temperature"] = _as_number(record.get("temperature"))
    payload["ph"] = _as_number(record.get("ph"))

    # A value that will not coerce is exactly the one worth keeping the original
    # of. Nothing reads it, but it is the only evidence of what arrived.
    if payload["ph"] is None and record.get("ph") is not None:
        payload["ph_raw"] = record["ph"]

    return payload


def unknown_keys(record: dict[str, Any]) -> list[str]:
    """Keys this mapping has never been told about, for the kind of record it is."""
    known = BOOT_KEYS if record.get("type") == KIND_BOOT else PICO_KEYS
    return sorted(set(record) - known)


def uptime_ms_from(record: dict[str, Any]) -> int | None:
    """Milliseconds since the Pico booted, from the h:mm:ss it prints.

    Second-truncated at the source, which is why two records from either side of
    a fast reset can carry the same value. AquadronePicoFirmware#28 adds a raw
    ms_since_boot; this should prefer it once it exists.
    """
    text = record.get("time")
    if not isinstance(text, str):
        return None

    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    if min(hours, minutes, seconds) < 0 or minutes > 59 or seconds > 59:
        return None

    return (hours * 3600 + minutes * 60 + seconds) * 1000


def resolve_stamp(
    record: dict[str, Any], uptime_ms: int | None, anchor_ms: int | None
) -> Stamp:
    """Apply the precedence: the Pico first, an anchor second, nothing third."""
    pico_ms = pico_epoch_ms(record)
    if pico_ms is not None:
        return Stamp(pico_ms, SOURCE_PICO, uptime_ms)
    if anchor_ms is not None and uptime_ms is not None:
        return Stamp(anchor_ms + uptime_ms, SOURCE_ANCHOR, uptime_ms)
    # No stamp we believe. The record is spooled anyway and stamped later if an
    # anchor arrives; what it must never do is go to the API unstamped, because
    # the serializer would fall back to ingest time and a backlog draining after
    # an outage would land every reading at the moment it drained.
    return Stamp(None, None, uptime_ms)


def build_envelope(
    raw_line: bytes,
    record: dict[str, Any],
    stamp: Stamp,
    run_id: str,
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The line as it is spooled, archived, and eventually POSTed."""
    return {
        "client_ref": client_ref_for(raw_line),
        "timestamp_ms": stamp.timestamp_ms,
        "timestamp_source": stamp.source,
        # Kept so an entry spooled without a stamp can be stamped at drain time
        # from an anchor derived after it was written.
        "uptime_ms": stamp.uptime_ms,
        # Which Pico run that uptime counts from. An uptime means nothing
        # outside its own run, and this is the only place the run survives: a
        # restart of this process loses every in-memory way of telling two of
        # them apart.
        "run_id": run_id,
        # Step 4 fills this, and it is the archive's whole reason for existing:
        # the Pico's card holds every other field on this line and not this one.
        "position": position,
        "payload": to_payload(record),
    }


def is_drainable(envelope: dict[str, Any]) -> bool:
    """Whether this entry may be uploaded yet.

    A gate, not a correction. The API is create-only, so a resubmitted
    client_ref returns `duplicate` and never an update: there is no second pass
    that could fix a timestamp sent wrong the first time.

    A type check rather than a null check, because the spool guarantees only
    that a loaded entry is a dict. A corrupted `"1754400072000"` passes "is not
    None" and reaches the uploader's arithmetic as a string.
    """
    return _is_plain_int(envelope.get("timestamp_ms"))


def stamp_with_anchor(
    envelope: dict[str, Any], state: AnchorState
) -> dict[str, Any]:
    """A new envelope carrying the stamp an anchor now makes possible.

    The spooled bytes are left alone. Nothing is overridden either: an entry
    that already has a stamp comes back untouched, and so does one belonging to
    any run but the one this anchor describes. An anchor from the run after a
    reset would shift a whole spool by the gap between the two boots, and the
    result is plausible, drainable and permanent.

    Takes the pair as one value so a caller cannot read a live anchor's epoch
    and run identity separately and compare a torn pair against the run gate.
    """
    uptime_ms = envelope.get("uptime_ms")
    if state.value is None or not _is_plain_int(uptime_ms):
        return envelope
    # Not `is_drainable`: this fills gaps and a corrupt stamp is not a gap.
    # Only an absent one is, so a mistyped value is left for the uploader to set
    # aside rather than quietly replaced with a plausible number.
    if envelope.get("timestamp_ms") is not None:
        return envelope
    if envelope.get("run_id") != state.run_id:
        return envelope
    return {
        **envelope,
        "timestamp_ms": state.value + uptime_ms,
        "timestamp_source": SOURCE_ANCHOR,
    }


def pico_epoch_ms(record: dict[str, Any]) -> int | None:
    """The Pico's own absolute stamp, or None if it never got one."""
    value = record.get("epoch_ms")
    if not _is_plain_int(value):
        return None
    # The firmware writes null rather than zero for an unsynced run, so a zero
    # here is a firmware that changed its mind, not a reading from 1970.
    return value if value > 0 else None


class Anchor:
    """The epoch of the Pico's boot instant, and the rules for distrusting it.

    Held only while it still describes the run in front of us. The two threads
    that touch it do so at different depths: the reader only ever sets a flag,
    which is an attribute store under the GIL, and the capture worker does the
    arithmetic when it next handles a record.
    """

    def __init__(self, counters: Counters) -> None:
        self._counters = counters
        self._state = AnchorState(None, _new_run_id())
        self._last_uptime_ms: int | None = None
        # Stays a plain attribute, deliberately, and does not join AnchorState.
        # The reader thread writes it (note_serial_reconnect) while the capture
        # thread writes the state, and folding it in would turn that write into
        # a read-modify-write of a shared object. A drop notice lost that way
        # leaves a stale anchor stamping the next run.
        self._pending_drop: str | None = None
        # A drop and the re-derivation after it are a pair, so neither may
        # swallow the other's line. Throttled at all because a cycle shorter
        # than the uptime's own second of resolution would log twice a record.
        self._drop_log = Throttle(ANCHOR_LOG_INTERVAL_S)
        self._derive_log = Throttle(ANCHOR_LOG_INTERVAL_S)

    @property
    def state(self) -> AnchorState:
        """The epoch and the run it belongs to, as one indivisible read.

        What anything off this thread should use. The two properties below are
        conveniences for callers that genuinely want one half.
        """
        return self._state

    @property
    def value(self) -> int | None:
        return self._state.value

    @property
    def run_id(self) -> str:
        """Which Pico run the records in front of us belong to.

        A restart of this process mints a new one even where the Pico kept
        running, so entries spooled unstamped before it are evicted rather than
        stamped from an anchor nothing can prove is theirs. That is the give-up
        rule: the Pico's card is the copy that survives.
        """
        return self._state.run_id

    def note_serial_reconnect(self) -> None:
        """Called from the reader thread on every reopen. The primary rule.

        Any Pico reset forces a USB re-enumeration, so read-error-then-reopen is
        the one detector that cannot be missed. The banner is not a reliable one
        on its own: the Pico prints it two seconds after boot without waiting
        for a host, and pico_stdio_usb discards output outright while DTR is
        deasserted, which is the state during the re-enumeration itself.
        """
        self._pending_drop = "the serial port reconnected"

    def note_banner(self) -> None:
        """Called from the reader thread when the Pico announces a boot."""
        self._pending_drop = "the Pico printed its boot banner"

    def for_record(
        self, uptime_ms: int | None, pico_ms: int | None, received: float
    ) -> int | None:
        """The anchor to stamp this record with, re-derived if it had to go."""
        self._take_pending_drop()
        self._drop_if_uptime_is_not_believable(uptime_ms)
        # Only a real uptime replaces the one we compare against. A record with
        # an unreadable `time` would otherwise blind the next comparison, which
        # is the direction that keeps a stale anchor rather than dropping a
        # good one.
        if uptime_ms is not None:
            self._last_uptime_ms = uptime_ms

        # Re-derive immediately rather than waiting for the next banner or sync
        # line. Waiting deadlocks in exactly the case this exists for: a run
        # whose Pi clock was still undisciplined at boot emits neither.
        if self._state.value is None:
            self._derive(uptime_ms, pico_ms, received)
        return self._state.value

    def _take_pending_drop(self) -> None:
        reason = self._pending_drop
        if reason is None:
            return
        self._pending_drop = None
        # Uptimes either side of a reconnect are not comparable, so the
        # monotonic check restarts with the next record rather than firing on
        # the first one after it.
        self._last_uptime_ms = None
        self._drop(reason)

    def _drop_if_uptime_is_not_believable(self, uptime_ms: int | None) -> None:
        # Runs whether or not an anchor is held. A run that never got one still
        # spools records, and they still have to be told apart from the next
        # run's.
        if uptime_ms is None or self._last_uptime_ms is None:
            return

        step_ms = uptime_ms - self._last_uptime_ms
        # Not "strictly lower". Uptime is second-truncated on a deterministic
        # boot sequence, so a reset after exactly one record reproduces the
        # value we already saw, and equal is not lower.
        if 0 < step_ms <= UPTIME_JUMP_MAX_MS:
            return

        if step_ms <= 0:
            # Also how the ~49.7 day uint32_t millisecond wrap looks, with no
            # reset behind it. That is a false drop, and re-deriving absorbs it.
            self._drop(f"uptime went from {self._last_uptime_ms} to {uptime_ms} ms")
        else:
            self._drop(f"uptime jumped {step_ms} ms in one record")

    def _drop(self, reason: str) -> None:
        # The run identity goes even when there is no anchor to lose, because
        # the records already spooled from the run that just ended are exactly
        # the ones the next anchor must not reach.
        #
        # One rebind, not a store to each half. Rotating the run identity before
        # clearing the epoch publishes the new run's id beside the old run's
        # epoch, on every power cycle, and that pair stamps a whole spool with
        # times shifted by the gap between two boots.
        had_anchor = self._state.value is not None
        self._state = AnchorState(None, _new_run_id())
        if not had_anchor:
            return
        self._counters.bump("anchor_dropped")
        if self._drop_log.should_emit():
            log.info("dropped the boot-time anchor: %s; %d more suppressed since "
                     "the last of these", reason, self._drop_log.take_suppressed())

    def _derive(
        self, uptime_ms: int | None, pico_ms: int | None, received: float
    ) -> None:
        if uptime_ms is None:
            return

        if pico_ms is not None:
            # Both halves come from the Pico's own clock at sampling time, so
            # there is no receipt delay and no emit lag to subtract.
            self._set(pico_ms - uptime_ms, "the Pico's own stamp")
            return

        if not clock.clock_is_trustworthy():
            return

        # The reader's receipt time, not the wall clock at parse time. A naive
        # wall_now - uptime is late by the whole buffer backlog, which is
        # deepest exactly when this fires, and by the firmware's own gap between
        # sampling and emitting.
        backlog_ms = int((time.monotonic() - received) * 1000)
        observed_ms = clock.epoch_ms_now() - backlog_ms
        self._set(observed_ms - uptime_ms - EMIT_LAG_MS, "the Pi's clock at receipt")

    def _set(self, value: int, how: str) -> None:
        # Rebound whole, same as _drop, so the pair is never half-written. The
        # run identity is carried across unchanged: deriving an anchor says
        # nothing about which run we are on, only what its epoch is.
        self._state = AnchorState(value, self._state.run_id)
        self._counters.bump("anchor_derived")
        if self._derive_log.should_emit():
            log.info("anchored this run's boot at epoch %d ms, from %s", value, how)


class Recorder:
    """The sink capture calls. Where a reading first becomes durable."""

    def __init__(
        self,
        spool: Spool,
        archive: Archive,
        counters: Counters,
        anchor: Anchor | None = None,
    ) -> None:
        self._spool = spool
        self._archive = archive
        self._counters = counters
        self.anchor = anchor if anchor is not None else Anchor(counters)
        self._mapping_log = Throttle(MAPPING_LOG_INTERVAL_S)
        self._disagreement_log = Throttle(MAPPING_LOG_INTERVAL_S)

    def capture(self, raw: bytes, record: dict[str, Any], received: float) -> None:
        """One parsed record, on the capture thread. Never the reader's."""
        if record.get("type") == KIND_BOOT:
            self._capture_boot()
            return

        uptime_ms = uptime_ms_from(record)
        pico_ms = pico_epoch_ms(record)
        anchor_ms = self.anchor.for_record(uptime_ms, pico_ms, received)

        self._report_disagreement(pico_ms, anchor_ms, uptime_ms)
        stamp = resolve_stamp(record, uptime_ms, anchor_ms)
        self._count_stamp(stamp)

        # The run id is read after for_record, which is what rotates it: a
        # record arriving on the far side of a reset belongs to the new run.
        envelope = build_envelope(raw, record, stamp, self.anchor.run_id)
        self._report_mapping_losses(record, envelope["payload"])

        # Archive first. A spool write that fails still leaves the position on
        # the stick, and the archive is never coupled to an acknowledgement:
        # a boat with no token is exactly when a local copy matters most.
        self._archive.append(envelope)
        self._spool.put(envelope)

    def _capture_boot(self) -> None:
        """The startup line carries no measurement, so nothing durable comes of it.

        Spooling one would hold a ring slot to no purpose: it has no uptime, so
        no anchor could ever stamp it drainable. boot_epoch_ms is left unread on
        purpose; anchoring on it moves the stamp on every later reading in the
        run, and the API is create-only with no way to correct one.
        """
        self._counters.bump("boot_records")

    def _count_stamp(self, stamp: Stamp) -> None:
        if stamp.source == SOURCE_PICO:
            self._counters.bump("timestamps_from_pico")
        elif stamp.source == SOURCE_ANCHOR:
            self._counters.bump("timestamps_from_anchor")
        else:
            self._counters.bump("timestamps_absent")

    def _report_disagreement(
        self, pico_ms: int | None, anchor_ms: int | None, uptime_ms: int | None
    ) -> None:
        """Both clocks answered and they differ. Say so; correct nothing."""
        if pico_ms is None or anchor_ms is None or uptime_ms is None:
            return

        difference_ms = pico_ms - (anchor_ms + uptime_ms)
        if abs(difference_ms) <= DISAGREEMENT_TOLERANCE_MS:
            return

        self._counters.bump("timestamp_disagreements")
        if self._disagreement_log.should_emit():
            log.warning("the Pico's stamp and our anchor disagree by %d ms; "
                        "keeping the Pico's", difference_ms)

    def _report_mapping_losses(
        self, record: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Every silent failure this mapping has, made loud once and counted."""
        # Any of the three, not only the first. An Atlas *ER in the middle
        # position leaves conductivity intact and drops tds, and the record
        # uploads with nothing anywhere to say a value was lost.
        from_cond_tds_sal = ("conductivity", "tds", "salinity")
        if record.get("cond_tds_sal") is not None and any(
            payload[key] is None for key in from_cond_tds_sal
        ):
            self._counters.bump("cond_tds_sal_unparseable")
        if record.get("ph") is not None and payload["ph"] is None:
            self._counters.bump("ph_unparseable")

        unknown = unknown_keys(record)
        if not unknown:
            return
        self._counters.bump("payload_keys_unknown", len(unknown))
        if self._mapping_log.should_emit():
            log.warning("the Pico sent key(s) this mapping does not know about "
                        "(%s); they are stored but nothing can read them",
                        ", ".join(unknown))


def _is_plain_int(value: Any) -> TypeGuard[int]:
    """An int the arithmetic can use.

    bool subclasses int, so an unguarded isinstance accepts True and reads it as
    one millisecond. The same lesson `pico_epoch_ms` learned first.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _new_run_id() -> str:
    """A fresh identity for the Pico run now in front of us.

    Random rather than derived from anything on the wire, because the run this
    has to identify is precisely the one with no epoch_ms and no anchor yet.
    """
    return str(uuid.uuid4())


def _split_cond_tds_sal(raw: Any) -> tuple[float | None, float | None, float | None]:
    """One string into the three numbers the API reads as separate columns.

    All three or none: a field with the wrong number of parts is a firmware that
    changed or a truncated response, and guessing which position is which there
    would put a salinity in the conductivity column.
    """
    if not isinstance(raw, str):
        return (None, None, None)
    parts = raw.split(",")
    if len(parts) != 3:
        return (None, None, None)
    return (_as_number(parts[0]), _as_number(parts[1]), _as_number(parts[2]))


def _as_number(value: Any) -> float | None:
    """A float, or None. Never a string, which is what the Pico sends."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
