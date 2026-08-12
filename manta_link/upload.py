"""Draining the spool into caddis-api, and refusing to lie about what landed.

The API is create-only. A resubmitted `client_ref` comes back `duplicate` and
never an update, so an acknowledgement this gets wrong cannot be corrected
later: a reading acked but not stored is gone, and a reading stored but not
acked is a duplicate row nobody asked for. Everything below is arranged around
being wrong in the second direction rather than the first.

Two rules inherited from the prior art, which was contract-probed against the
live API and should not be re-derived. A result whose index cannot be trusted to
name one reading is dropped rather than guessed at. And a 2xx whose body is not
the shape caddis-api sends is not a confirmed store, because a captive portal
answers 200 to everything.

One deliberate departure from it: an empty `results` list against a non-empty
batch acks nothing. `TelemetryBatchView.post` builds that list by comprehension
over the readings it was sent and returns it unconditionally, so an empty one
cannot have come from caddis-api, and the prior art's fail-open there would
delete a whole batch on the strength of a reply from something else.

This never sees a token, never writes a header, and never touches the archive:
`Recorder.capture` archives before it spools, so every entry reachable here is
already on the stick, which is what keeps `Archive` a single-threaded object.
"""

import enum
import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from . import record
from .config import Binding, ConfigWatcher, TokenSession
from .health import Counters
from .logging_setup import Throttle
from .record import Anchor, AnchorState
from .spool import Spool
from .upload_payload import to_reading

log = logging.getLogger(__name__)

QUARANTINE_SUBDIR = "quarantine"

# Long enough that a slow API on a cellular link is not mistaken for a dead one,
# short enough that a pass cannot hold the queue for a minute.
REQUEST_TIMEOUT_S = 30.0

IDLE_SLEEP_S = 1.0

BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0

# Five failures over ten minutes. Both, because five in five seconds is one bad
# minute on a cellular link and not a poisoned reading.
QUARANTINE_AFTER_ATTEMPTS = 5
QUARANTINE_MIN_AGE_S = 600.0

FAILURE_LOG_INTERVAL_S = 300.0
STATE_LOG_INTERVAL_S = 300.0


class Outcome(enum.Enum):
    """What one drain pass established. Only PROGRESS means a reading landed."""

    UNCONFIGURED = "unconfigured"
    IDLE = "idle"
    PROGRESS = "progress"
    # A 2xx that acked nothing. Distinct from FAILED because the API is up and
    # answering, so retrying this same batch harder achieves nothing, and
    # distinct from PROGRESS because nothing moved.
    STUCK = "stuck"
    FAILED = "failed"
    UNAUTHORIZED = "unauthorized"


class Answer:
    """One POST's outcome, and the per-index results if the body was ours."""

    def __init__(
        self, understood: bool, results: "list[Any] | None" = None,
        unauthorized: bool = False,
    ) -> None:
        # `understood` means the body parsed into the shape caddis-api sends. It
        # does not mean the readings were taken: it is True for a batch where
        # every single row came back an error.
        self.understood = understood
        self.results: list[Any] = [] if results is None else results
        self.unauthorized = unauthorized


class Attempt:
    """How many times one spool entry has failed, and since when."""

    __slots__ = ("count", "first_failed_at")

    def __init__(self, count: int, first_failed_at: float) -> None:
        self.count = count
        self.first_failed_at = first_failed_at


class Uploader:
    """A Health worker that moves spooled readings to the API."""

    def __init__(
        self,
        spool: Spool,
        anchor: Anchor,
        tokens: TokenSession,
        watcher: ConfigWatcher,
        counters: Counters,
    ) -> None:
        self._spool = spool
        self._anchor = anchor
        self._tokens = tokens
        self._watcher = watcher
        self._counters = counters
        self._quarantine_dir = spool.directory.parent / QUARANTINE_SUBDIR

        # Keyed to the spool name, never the client_ref. Eviction unlinks the
        # file and drops the index entry together, so ref-keyed state outlives
        # every entry it describes and leaks forever, which is the pruner the
        # prior art needed and this does not.
        self._attempts: dict[str, Attempt] = {}

        # An entry's drainability is a pure function of its own bytes, which
        # never change, and the anchor state it was tested against. Undrainable
        # under one state is undrainable under that state forever, so a wall of
        # unstampable entries costs one read each rather than one per pass: at a
        # 60,000-entry device spool that is the difference between 200 opens a
        # second for two days and 200 opens once.
        self._undrainable_under: AnchorState | None = None
        self._undrainable: set[str] = set()

        # Where to resume after a batch that would not move. Names are
        # fixed-width and sorted, so a plain comparison is a cursor.
        self._scan_from: str | None = None

        self._generation = tokens.current().generation
        self._backoff_s = 0.0
        self._retry_after = 0.0
        self._failure_log = Throttle(FAILURE_LOG_INTERVAL_S)
        self._state_log = Throttle(STATE_LOG_INTERVAL_S)

    def run_forever(self) -> None:
        """Never returns. Supervised, and restarted if it ever does."""
        while True:
            waiting = self._retry_after - time.monotonic()
            if waiting > 0:
                # Slept in slices rather than in one go, so a token dropped in
                # mid-backoff is picked up on the next pass instead of after the
                # full sixty seconds.
                time.sleep(min(IDLE_SLEEP_S, waiting))
                continue

            outcome = self.drain_once()
            if outcome is Outcome.PROGRESS:
                # Straight round again: there is more queue and it just worked.
                continue
            if outcome in (Outcome.IDLE, Outcome.UNCONFIGURED):
                time.sleep(IDLE_SLEEP_S)
                continue
            self._back_off(outcome)

    def drain_once(self) -> Outcome:
        """One batch, at most. Returns what it established."""
        binding = self._tokens.current()
        self._adopt(binding)

        if not binding.configured:
            self._report_state("no API token configured; uploads are off")
            return Outcome.UNCONFIGURED

        limit = self._watcher.config.batch_max
        batch = self._select(limit)
        if not batch and self._scan_from is not None:
            # The cursor reached the end after stepping over stuck batches.
            # Start again at the oldest rather than parking here: those entries
            # have had time to become sendable, and a cursor left at the tail
            # stalls the whole queue permanently the first time a batch sticks.
            self._scan_from = None
            batch = self._select(limit)
        if not batch:
            return Outcome.IDLE

        answer = self._post(binding, [reading for _, reading in batch])
        if answer.unauthorized:
            self._counters.bump("upload_unauthorized")
            return Outcome.UNAUTHORIZED
        if not answer.understood:
            return Outcome.FAILED
        return self._resolve(batch, answer.results)

    def _adopt(self, binding: Binding) -> None:
        """Notice a rotation, on this thread, by pulling rather than being told.

        The health thread never reaches into the uploader. It changes the
        binding, and this sees a new generation next pass and clears the backoff
        a 401 put it in, which is what makes a dropped-in token take effect
        without a restart.
        """
        if binding.generation == self._generation:
            return
        self._generation = binding.generation
        self._backoff_s = 0.0
        self._retry_after = 0.0
        log.info("credential changed (fingerprint %s); resuming uploads",
                 binding.fingerprint)

    def _select(self, limit: int) -> "list[tuple[str, dict[str, Any]]]":
        """The next batch, oldest first, skipping what cannot go yet."""
        state = self._anchor.state
        if self._undrainable_under is not state:
            self._undrainable_under = state
            self._undrainable = set()

        chosen: list[tuple[str, dict[str, Any]]] = []
        for name in self._spool.names():
            if self._scan_from is not None and name <= self._scan_from:
                continue
            if name in self._undrainable:
                continue

            envelope = self._spool.load(name)
            if envelope is None:
                # Gone, or unreadable. The spool counts and handles both, and a
                # read error says nothing about the content, so this is not
                # counted as undrainable: that would hide an I/O fault behind an
                # anchor story.
                continue

            stamped = record.stamp_with_anchor(envelope, state)
            reading = to_reading(stamped)
            if reading is not None:
                chosen.append((name, reading))
                if len(chosen) >= limit:
                    break
                continue

            if record.is_drainable(stamped):
                # It has a usable timestamp and still cannot become a reading,
                # so the payload or the client_ref is wrong. Locally knowable
                # and permanent: no number of retries improves it, and left in
                # place it is the head of the queue forever.
                self._counters.bump("spool_entries_malformed")
                self._quarantine(name, envelope)
            else:
                self._undrainable.add(name)
        self._count_undrainable()
        return chosen

    def _post(self, binding: Binding, readings: list) -> Answer:
        endpoint = self._watcher.config.batch_endpoint
        try:
            response = binding.session.post(
                endpoint, json={"readings": readings}, timeout=REQUEST_TIMEOUT_S
            )
        except requests.RequestException as exc:
            # The type and nothing else. A requests exception can carry the
            # request, and the request carries the Authorization header, so the
            # message is a place a token can appear. This module is handed a
            # session and never a token, deliberately, which also means it has
            # nothing to scrub a message with: dropping it is the only honest
            # option. The endpoint is already known, and the status code below
            # is the diagnostic that survives.
            self._report_failure(type(exc).__name__)
            return Answer(understood=False)

        if response.status_code in (401, 403):
            return Answer(understood=False, unauthorized=True)
        if not response.ok:
            self._report_failure(f"HTTP {response.status_code}")
            return Answer(understood=False)

        try:
            body = response.json()
        except ValueError:
            # A 2xx whose body is not JSON is a captive portal or a proxy
            # interstitial, not a confirmed store. Retain and retry.
            self._report_failure("2xx with a body that is not JSON")
            return Answer(understood=False)

        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            self._report_failure("2xx with a body that is not ours")
            return Answer(understood=False)
        return Answer(understood=True, results=body["results"])

    def _resolve(
        self, batch: "list[tuple[str, dict[str, Any]]]", results: list
    ) -> Outcome:
        status_by_index = validated_status_by_index(results, len(batch))
        acked = 0

        for index, (name, _) in enumerate(batch):
            # No default. An index missing from the results is uncorrelated, and
            # an uncorrelated reading is never assumed created: that assumption
            # is the difference between a retry and silent data loss.
            if status_by_index.get(index) in ("created", "duplicate"):
                # Both are acks. client_ref dedup upstream turns at-least-once
                # delivery into effectively exactly-once, so a re-sent reading
                # comes back duplicate and is safely dropped too.
                self._spool.remove(name)
                self._attempts.pop(name, None)
                acked += 1

        self._counters.bump("readings_uploaded", acked)
        self._note_failures(batch, status_by_index, acked)

        if acked:
            self._scan_from = None
            return Outcome.PROGRESS

        # Nothing moved and the API is up. Step over this batch rather than
        # deleting it: the entries keep their retry state and drain the moment
        # the API recovers, where quarantining them on a timer would turn a
        # seven-hour server-side bug into permanent loss of the oldest readings.
        self._scan_from = batch[-1][0]
        self._counters.bump("upload_batches_stuck")
        return Outcome.STUCK

    def _note_failures(
        self,
        batch: "list[tuple[str, dict[str, Any]]]",
        status_by_index: "dict[int, str | None]",
        acked: int,
    ) -> None:
        # The prior art's second gate. A batch where nothing was acked is a
        # statement about the API, not about any reading in it, so nothing in it
        # earns a strike toward being called poison.
        uniform_failure = acked == 0 and len(batch) >= QUARANTINE_AFTER_ATTEMPTS
        now = time.monotonic()

        for index, (name, _) in enumerate(batch):
            if status_by_index.get(index) in ("created", "duplicate"):
                continue
            attempt = self._attempts.get(name)
            count = 1 if attempt is None else attempt.count + 1
            first = now if attempt is None else attempt.first_failed_at

            sustained = (
                count >= QUARANTINE_AFTER_ATTEMPTS
                and now - first >= QUARANTINE_MIN_AGE_S
            )
            if sustained and not uniform_failure:
                envelope = self._spool.load(name)
                if envelope is not None:
                    self._quarantine(name, envelope)
                self._attempts.pop(name, None)
                continue
            self._attempts[name] = Attempt(count, first)

    def _quarantine(self, name: str, envelope: Any) -> None:
        """Move one entry aside, so the queue behind it can drain.

        Written out before the spool copy is removed. The reverse order loses
        the reading outright if the write fails, and the whole reason a poison
        entry is set aside rather than dropped is that somebody may want to look
        at it.
        """
        try:
            self._quarantine_dir.mkdir(parents=True, exist_ok=True)
            target = self._quarantine_dir / name
            target.write_text(
                json.dumps(envelope, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as exc:
            # Left in the spool. It will be tried again, which is better than
            # deleting a reading because a directory could not be made.
            self._counters.bump("quarantine_failures")
            log.error("could not quarantine %s (%s); leaving it in the spool",
                      name, exc)
            return

        self._spool.remove(name)
        self._counters.bump("readings_quarantined")
        log.error("set %s aside in %s after repeated refusals; it is out of the "
                  "queue and still on disk", name, self._quarantine_dir)

    def _count_undrainable(self) -> None:
        if self._undrainable:
            self._counters.bump("spool_awaiting_an_anchor", len(self._undrainable))

    def _back_off(self, outcome: Outcome) -> None:
        if outcome is Outcome.UNAUTHORIZED:
            # Straight to the ceiling. A rejected credential is not going to
            # start working in one second, and hammering the API with a bad
            # token is how a device gets rate limited. Never fatal and never a
            # discard: the readings stay spooled and go the moment a good token
            # appears, which _adopt notices within a heartbeat.
            self._backoff_s = BACKOFF_MAX_S
        else:
            self._backoff_s = min(
                BACKOFF_MAX_S,
                self._backoff_s * 2 if self._backoff_s else BACKOFF_BASE_S,
            )
        self._retry_after = time.monotonic() + self._backoff_s

    def _report_failure(self, reason: str) -> None:
        self._counters.bump("upload_failures")
        if self._failure_log.should_emit():
            log.warning("upload failed (%s); %d more suppressed since the last "
                        "of these", reason, self._failure_log.take_suppressed())

    def _report_state(self, message: str) -> None:
        if self._state_log.should_emit():
            log.info("%s", message)


def validated_status_by_index(
    results: list, batch_len: int
) -> "dict[int, str | None]":
    """Index to status, dropping any result that cannot name one reading.

    Carried near-verbatim from the prior art, which contract-probed it against
    the live API. Fails closed: a non-object entry, a string index, a missing
    index, an out-of-range index and a bool are all equally untrustworthy and
    are dropped the same way.

    bool is checked BEFORE int on purpose. bool subclasses int and
    hash(True) == hash(1), so an unvalidated {"index": true} would silently
    overwrite, or be overwritten by, the real index-1 entry rather than being
    rejected. A dropped result leaves its reading uncorrelated, and an
    uncorrelated reading is retried rather than acked.
    """
    validated: dict[int, str | None] = {}
    for entry in results:
        if not isinstance(entry, dict):
            log.warning("batch result entry is not an object: %r; ignoring", entry)
            continue
        index = entry.get("index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < batch_len
        ):
            log.warning("batch result has an uncorrelatable index %r; retaining "
                        "that reading for retry rather than acking it", index)
            continue
        status = entry.get("status")
        validated[index] = status if isinstance(status, str) else None
    return validated


def quarantine_directory(spool_directory: Path) -> Path:
    """Beside the spool, never inside it: the index regex would ignore it, but a
    directory that appears in the spool's own scan is a surprise waiting."""
    return spool_directory.parent / QUARANTINE_SUBDIR
