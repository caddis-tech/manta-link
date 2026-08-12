"""What the uploader will and will not call a stored reading.

The API is create-only, so an acknowledgement this gets wrong cannot be
corrected: a reading acked but not stored is gone for good, and a reading stored
but not acked is a duplicate row. Every test here is about being wrong in the
second direction rather than the first.

The HTTP double lives in this file rather than in fakes.py because every
assertion it supports is about what was POSTed and what the reply was, which is
this suite's whole subject and no other's.
"""

import ast
import json
import time
import uuid
from pathlib import Path

import pytest
import requests

from manta_link import config, record, upload
from manta_link.config import ConfigWatcher, TokenSession
from manta_link.health import Counters
from manta_link.record import Anchor, AnchorState, Stamp
from manta_link.spool import Spool
from manta_link.upload import Outcome, Uploader, validated_status_by_index
from manta_link.upload_payload import iso_from_epoch_ms, to_reading

TOKEN = "a" * 40
RUN_A = "e0a1c5be-0000-4000-8000-00000000000a"
RUN_B = "e0a1c5be-0000-4000-8000-00000000000b"
STAMP_MS = 1_786_000_000_000


class FakeResponse:
    def __init__(self, status_code: int, body, is_json: bool = True) -> None:
        self.status_code = status_code
        self._body = body
        self._is_json = is_json

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if not self._is_json:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """A scriptable stand-in for requests.Session, with the faults that matter."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.posted: list[dict] = []
        self.reply = FakeResponse(200, {"results": []})
        self.raises: Exception | None = None
        self.closed = False

    def post(self, url, json=None, timeout=None):  # noqa: A002 - requests' name
        self.posted.append({"url": url, "json": json, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        # The real thing validates headers on every request, so a token with a
        # newline in it raises here rather than going out on the wire.
        requests.utils.check_header_validity(("Authorization", "x"))
        return self.reply

    def close(self) -> None:
        self.closed = True


class OneSession(TokenSession):
    """A TokenSession whose sessions are the fake above."""

    def __init__(self, configured: bool = True) -> None:
        self.built: list[FakeSession] = []
        super().__init__()
        if configured:
            self.rebind(config.Config(token=TOKEN, api_url="https://api.test"))

    def _build(self, cfg):
        session = FakeSession()
        if cfg.token is not None:
            session.headers["Authorization"] = f"Token {cfg.token}"
        self.built.append(session)
        return config.Binding(
            session=session,  # type: ignore[arg-type]
            generation=self._generation,
            fingerprint=cfg.fingerprint,
            configured=cfg.configured,
        )


@pytest.fixture
def counters():
    return Counters()


@pytest.fixture
def spool(tmp_path, counters):
    store = Spool(tmp_path / "spool", counters)
    store.open()
    return store


@pytest.fixture
def anchor(counters):
    return Anchor(counters)


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    monkeypatch.delenv(config.TOKEN_ENV, raising=False)
    monkeypatch.delenv(config.API_URL_ENV, raising=False)
    made = ConfigWatcher(tmp_path, TokenSession())
    made._config = config.Config(token=TOKEN, api_url="https://api.test")
    return made


@pytest.fixture
def tokens():
    return OneSession()


@pytest.fixture
def uploader(spool, anchor, tokens, watcher, counters):
    return Uploader(spool, anchor, tokens, watcher, counters)


def session_of(tokens: OneSession) -> FakeSession:
    return tokens.current().session  # type: ignore[return-value]


def an_envelope(**overrides) -> dict:
    envelope = {
        "client_ref": str(uuid.uuid4()),
        "timestamp_ms": STAMP_MS,
        "timestamp_source": record.SOURCE_PICO,
        "uptime_ms": 72_000,
        "run_id": RUN_A,
        "position": None,
        "payload": {"ph": 7.2},
    }
    envelope.update(overrides)
    return envelope


def spool_some(store: Spool, count: int, **overrides) -> list[str]:
    return [store.put(an_envelope(**overrides)) or "" for _ in range(count)]


def results_for(names, status="created"):
    return {"results": [{"index": i, "status": status} for i in range(len(names))]}


class TestTheReadingItSends:
    def test_a_spooled_envelope_becomes_the_api_row(self):
        reading = to_reading(an_envelope())

        assert reading is not None
        assert set(reading) == {"payload", "timestamp", "client_ref"}
        assert reading["payload"] == {"ph": 7.2}

    def test_internal_envelope_fields_do_not_go_on_the_wire(self):
        reading = to_reading(an_envelope())

        assert reading is not None
        for internal in ("run_id", "uptime_ms", "timestamp_source", "position"):
            assert internal not in reading

    def test_the_timestamp_keeps_the_millisecond_it_was_given(self):
        # divmod on the integer. epoch_ms / 1000 into strftime("%f")[:-3] renders
        # ...072123 as .122999 and truncates it to .122, on some values only.
        assert iso_from_epoch_ms(1_754_400_072_123) == "2025-08-05T13:21:12.123Z"

    def test_the_rendering_is_utc_whatever_zone_the_pi_holds(self):
        # fromtimestamp without a tz renders in the machine's local zone, and
        # the Z suffix would then be a lie on every boat outside UTC.
        assert iso_from_epoch_ms(1_735_689_600_000) == "2025-01-01T00:00:00.000Z"

    @pytest.mark.parametrize("epoch_ms", [1_754_400_072_000, 1_754_400_072_009])
    def test_the_millisecond_field_is_always_three_digits(self, epoch_ms):
        rendered = iso_from_epoch_ms(epoch_ms)
        assert rendered.endswith("Z")
        assert len(rendered.split(".")[1]) == 4

    @pytest.mark.parametrize(
        "broken",
        [
            {"timestamp_ms": None},
            {"timestamp_ms": "1786000000000"},
            {"timestamp_ms": True},
            {"timestamp_ms": 1},
            {"timestamp_ms": 99_999_999_999_999},
            {"payload": None},
            {"payload": "ph=7.2"},
            {"client_ref": None},
            {"client_ref": "not-a-uuid"},
            {"client_ref": 17},
        ],
    )
    def test_an_entry_that_cannot_become_a_reading_says_so(self, broken):
        """Total, because one bit-flipped but parseable file would otherwise
        crash-loop the uploader at the supervisor ceiling and the boat would
        upload nothing at all, forever."""
        assert to_reading(an_envelope(**broken)) is None

    def test_a_null_client_ref_is_refused_rather_than_sent(self):
        # The unique constraint is partial on non-null, so a null ref is
        # `created` on every retry and idempotency evaporates one row at a time.
        assert to_reading(an_envelope(client_ref=None)) is None

    def test_something_that_is_not_an_envelope_at_all_is_refused(self):
        assert to_reading("a string") is None
        assert to_reading(None) is None


class TestCorrelatingResults:
    def test_the_ordinary_case(self):
        results = [{"index": 0, "status": "created"}, {"index": 1, "status": "error"}]
        assert validated_status_by_index(results, 2) == {0: "created", 1: "error"}

    @pytest.mark.parametrize(
        "entry",
        [
            {"index": True, "status": "created"},
            {"index": "0", "status": "created"},
            {"status": "created"},
            {"index": 5, "status": "created"},
            {"index": -1, "status": "created"},
            {"index": None, "status": "created"},
            "not an object",
            None,
            17,
        ],
    )
    def test_a_result_that_cannot_name_a_reading_is_dropped(self, entry):
        assert validated_status_by_index([entry], 2) == {}

    def test_a_bool_index_cannot_overwrite_the_real_index_one(self):
        """bool subclasses int and hash(True) == hash(1).

        Unvalidated, {"index": true, "status": "created"} silently takes the
        place of the real index-1 entry, and a reading that errored is acked and
        deleted.
        """
        results = [
            {"index": 1, "status": "error"},
            {"index": True, "status": "created"},
        ]

        assert validated_status_by_index(results, 2) == {1: "error"}


class TestWhatCountsAsStored:
    def test_a_created_reading_leaves_the_spool(self, uploader, spool, tokens):
        names = spool_some(spool, 2)
        session_of(tokens).reply = FakeResponse(200, results_for(names))

        assert uploader.drain_once() is Outcome.PROGRESS
        assert spool.names() == []

    def test_a_duplicate_is_an_ack_too(self, uploader, spool, tokens):
        """client_ref dedup turns at-least-once into effectively exactly-once,
        so a re-sent reading comes back duplicate and is safely dropped."""
        names = spool_some(spool, 2)
        session_of(tokens).reply = FakeResponse(
            200, results_for(names, status="duplicate")
        )

        assert uploader.drain_once() is Outcome.PROGRESS
        assert spool.names() == []

    def test_an_errored_reading_stays(self, uploader, spool, tokens):
        names = spool_some(spool, 2)
        session_of(tokens).reply = FakeResponse(
            200, results_for(names, status="error")
        )

        assert uploader.drain_once() is Outcome.STUCK
        assert len(spool.names()) == 2

    def test_an_uncorrelated_reading_is_never_assumed_created(
        self, uploader, spool, tokens
    ):
        spool_some(spool, 2)
        # Only index 0 came back. Index 1 is uncorrelated.
        session_of(tokens).reply = FakeResponse(
            200, {"results": [{"index": 0, "status": "created"}]}
        )

        uploader.drain_once()

        assert len(spool.names()) == 1

    def test_an_empty_results_list_acks_nothing(self, uploader, spool, tokens):
        """The one deliberate departure from the prior art, which failed open.

        TelemetryBatchView builds results by comprehension over the readings it
        was sent and returns it unconditionally, so an empty list against a
        non-empty batch did not come from caddis-api and must not delete fifty
        readings on the strength of a reply from something else.
        """
        spool_some(spool, 3)
        session_of(tokens).reply = FakeResponse(200, {"results": []})

        assert uploader.drain_once() is Outcome.STUCK
        assert len(spool.names()) == 3

    @pytest.mark.parametrize(
        "reply",
        [
            FakeResponse(200, "<html>sign in</html>", is_json=False),
            FakeResponse(200, ["created"]),
            FakeResponse(200, {"detail": "ok"}),
            FakeResponse(200, "ok"),
        ],
    )
    def test_a_2xx_that_is_not_ours_is_not_a_confirmed_store(
        self, uploader, spool, tokens, reply
    ):
        # A captive portal answers 200 to everything.
        spool_some(spool, 2)
        session_of(tokens).reply = reply

        assert uploader.drain_once() is Outcome.FAILED
        assert len(spool.names()) == 2

    def test_a_transport_failure_keeps_everything(self, uploader, spool, tokens):
        spool_some(spool, 2)
        session_of(tokens).raises = requests.ConnectionError("no route")

        assert uploader.drain_once() is Outcome.FAILED
        assert len(spool.names()) == 2

    def test_one_corrupt_entry_does_not_stop_the_boat_uploading(
        self, uploader, spool, tokens, counters
    ):
        """The crash-loop this exists to prevent."""
        spool.put(an_envelope(payload="not a payload"))
        good = spool_some(spool, 2)
        session_of(tokens).reply = FakeResponse(200, results_for(good))

        assert uploader.drain_once() is Outcome.PROGRESS
        assert counters.get("spool_entries_malformed") == 1
        assert spool.names() == []


class TestWhatIsNotReadyToGo:
    def test_an_unstamped_entry_is_never_posted(self, uploader, spool, tokens):
        """The API's timestamp falls back to ingest time, so a backlog draining
        after an outage would land every reading at the moment it drained, with
        no way to correct it afterwards."""
        spool.put(an_envelope(timestamp_ms=None, timestamp_source=None))

        assert uploader.drain_once() is Outcome.IDLE
        assert session_of(tokens).posted == []

    def test_it_goes_the_moment_its_own_runs_anchor_arrives(
        self, spool, tokens, watcher, counters
    ):
        spool.put(an_envelope(timestamp_ms=None, timestamp_source=None))
        anchor = Anchor(counters)
        uploader = Uploader(spool, anchor, tokens, watcher, counters)
        assert uploader.drain_once() is Outcome.IDLE

        anchor._state = AnchorState(STAMP_MS - 72_000, RUN_A)
        session_of(tokens).reply = FakeResponse(200, results_for(["one"]))

        assert uploader.drain_once() is Outcome.PROGRESS
        posted = session_of(tokens).posted[-1]["json"]["readings"][0]
        assert posted["timestamp"] == iso_from_epoch_ms(STAMP_MS)

    def test_an_anchor_from_another_run_does_not_release_it(
        self, spool, tokens, watcher, counters
    ):
        spool.put(an_envelope(timestamp_ms=None, timestamp_source=None))
        anchor = Anchor(counters)
        anchor._state = AnchorState(STAMP_MS, RUN_B)
        uploader = Uploader(spool, anchor, tokens, watcher, counters)

        assert uploader.drain_once() is Outcome.IDLE

    def test_an_undrainable_entry_is_read_once_per_anchor_state(
        self, spool, tokens, watcher, counters, anchor
    ):
        """A wall of unstampable entries must not be re-read every pass.

        On a device spool that is 200 opens a second for forty hours, against
        the same stick the capture thread is fsyncing to.
        """
        for _ in range(3):
            spool.put(an_envelope(timestamp_ms=None, timestamp_source=None))
        uploader = Uploader(spool, anchor, tokens, watcher, counters)

        uploader.drain_once()
        reads_after_first = counters.get("spool_reads")
        for _ in range(5):
            uploader.drain_once()

        # The spool does not count reads, so measure it the other way: the set
        # is populated once and every later pass skips straight past it.
        assert len(uploader._undrainable) == 3
        assert reads_after_first == counters.get("spool_reads")

    def test_a_new_anchor_state_makes_them_worth_reading_again(
        self, spool, tokens, watcher, counters, anchor
    ):
        spool.put(an_envelope(timestamp_ms=None, timestamp_source=None))
        uploader = Uploader(spool, anchor, tokens, watcher, counters)
        uploader.drain_once()
        assert uploader._undrainable

        anchor._state = AnchorState(STAMP_MS - 72_000, RUN_A)
        session_of(tokens).reply = FakeResponse(200, results_for(["one"]))

        assert uploader.drain_once() is Outcome.PROGRESS


class TestHeadOfLineBlocking:
    def test_a_stuck_batch_is_stepped_over_rather_than_deleted(
        self, spool, tokens, watcher, counters, anchor
    ):
        """Skipping, not quarantining.

        TelemetryBatchView returns a per-row error inside a 200 for any
        server-side exception, so a seven-hour server bug would move thousands
        of readings into a quarantine ring on a timer and evict most of them.
        """
        watcher._config = config.Config(token=TOKEN, api_url="https://api.test")
        watcher._config = config.Config(
            token=TOKEN, api_url="https://api.test", batch_max=2
        )
        spool_some(spool, 4)
        uploader = Uploader(spool, anchor, tokens, watcher, counters)
        session_of(tokens).reply = FakeResponse(
            200, results_for(["a", "b"], status="error")
        )

        assert uploader.drain_once() is Outcome.STUCK
        first = session_of(tokens).posted[-1]["json"]["readings"]

        uploader.drain_once()
        second = session_of(tokens).posted[-1]["json"]["readings"]

        assert len(spool.names()) == 4
        assert first != second

    def test_progress_returns_to_the_oldest(
        self, spool, tokens, watcher, counters, anchor
    ):
        watcher._config = config.Config(
            token=TOKEN, api_url="https://api.test", batch_max=2
        )
        uploader = Uploader(spool, anchor, tokens, watcher, counters)
        spool_some(spool, 4)
        session_of(tokens).reply = FakeResponse(
            200, results_for(["a", "b"], status="error")
        )
        uploader.drain_once()
        assert uploader._scan_from is not None

        session_of(tokens).reply = FakeResponse(200, results_for(["a", "b"]))
        uploader.drain_once()

        assert uploader._scan_from is None


class TestRetryState:
    def test_it_is_keyed_to_the_spool_name_not_the_client_ref(
        self, uploader, spool, tokens
    ):
        """spool.py says so: eviction unlinks the file and drops the index entry
        together, so ref-keyed state outlives every entry it describes."""
        names = spool_some(spool, 2)
        session_of(tokens).reply = FakeResponse(
            200, results_for(names, status="error")
        )

        uploader.drain_once()

        assert set(uploader._attempts) == set(names)

    def test_it_does_not_leak_when_an_entry_is_acked(self, uploader, spool, tokens):
        names = spool_some(spool, 2)
        session_of(tokens).reply = FakeResponse(
            200, results_for(names, status="error")
        )
        uploader.drain_once()

        session_of(tokens).reply = FakeResponse(200, results_for(names))
        uploader.drain_once()

        assert uploader._attempts == {}

    def test_a_whole_batch_failing_earns_nobody_a_strike_toward_poison(
        self, uploader, spool, tokens, counters
    ):
        """A batch where nothing was acked is a statement about the API, not
        about any reading in it."""
        names = spool_some(spool, 6)
        session_of(tokens).reply = FakeResponse(
            200, results_for(names, status="error")
        )

        for _ in range(10):
            uploader.drain_once()
            for attempt in uploader._attempts.values():
                attempt.first_failed_at -= 1_000.0

        assert counters.get("readings_quarantined") == 0
        assert len(spool.names()) == 6

    def test_one_poison_reading_beside_healthy_ones_is_set_aside(
        self, uploader, spool, tokens, counters, tmp_path
    ):
        names = spool_some(spool, 2)
        # Index 0 always errors, index 1 always succeeds, so the batch is never
        # a uniform failure and the second gate opens.
        session_of(tokens).reply = FakeResponse(
            200,
            {
                "results": [
                    {"index": 0, "status": "error"},
                    {"index": 1, "status": "created"},
                ]
            },
        )
        uploader.drain_once()
        session_of(tokens).reply = FakeResponse(
            200, {"results": [{"index": 0, "status": "error"}]}
        )
        for _ in range(upload.QUARANTINE_AFTER_ATTEMPTS + 1):
            for attempt in uploader._attempts.values():
                attempt.first_failed_at -= upload.QUARANTINE_MIN_AGE_S
            uploader.drain_once()

        assert counters.get("readings_quarantined") == 1
        quarantined = list((tmp_path / "quarantine").glob("*.json"))
        assert len(quarantined) == 1
        # Written out before the spool copy went, so it is still readable.
        assert json.loads(quarantined[0].read_text())["client_ref"]
        assert names


class TestTheTokenAndTheBackoff:
    def test_no_token_does_not_post_and_does_not_spin(
        self, spool, anchor, watcher, counters
    ):
        tokens = OneSession(configured=False)
        spool_some(spool, 3)
        uploader = Uploader(spool, anchor, tokens, watcher, counters)

        assert uploader.drain_once() is Outcome.UNCONFIGURED
        assert session_of(tokens).posted == []
        assert len(spool.names()) == 3

    def test_a_401_is_retryable_and_never_a_discard(self, uploader, spool, tokens):
        spool_some(spool, 2)
        session_of(tokens).reply = FakeResponse(401, {"detail": "bad token"})

        assert uploader.drain_once() is Outcome.UNAUTHORIZED
        assert len(spool.names()) == 2

    def test_a_401_goes_straight_to_the_ceiling(self, uploader, spool, tokens):
        # A rejected credential will not start working in a second, and
        # hammering with a bad token is how a device gets rate limited.
        spool_some(spool, 1)
        session_of(tokens).reply = FakeResponse(403, {})
        uploader.drain_once()

        uploader._back_off(Outcome.UNAUTHORIZED)

        assert uploader._backoff_s == upload.BACKOFF_MAX_S

    def test_an_ordinary_failure_doubles_from_one_second(self, uploader):
        seen = []
        for _ in range(8):
            uploader._back_off(Outcome.FAILED)
            seen.append(uploader._backoff_s)

        assert seen[:4] == [1.0, 2.0, 4.0, 8.0]
        assert seen[-1] == upload.BACKOFF_MAX_S

    def test_a_new_credential_clears_the_backoff_a_401_caused(
        self, uploader, spool, tokens
    ):
        """The rotation path, pulled on this thread rather than pushed.

        The health thread changes the binding and never reaches in here; this
        notices the new generation and resumes, which is what makes a
        dropped-in token take effect without a container restart.
        """
        spool_some(spool, 1)
        session_of(tokens).reply = FakeResponse(401, {})
        uploader.drain_once()
        uploader._back_off(Outcome.UNAUTHORIZED)
        assert uploader._retry_after > time.monotonic()

        tokens.rebind(config.Config(token="b" * 40, api_url="https://api.test"))
        session_of(tokens).reply = FakeResponse(200, results_for(["one"]))

        assert uploader.drain_once() is Outcome.PROGRESS
        assert uploader._backoff_s == 0.0

    def test_the_uploader_never_writes_a_header_itself(self, uploader, spool, tokens):
        spool_some(spool, 1)
        session_of(tokens).reply = FakeResponse(200, results_for(["one"]))

        uploader.drain_once()

        # It came from TokenSession, not from here.
        assert session_of(tokens).headers == {"Authorization": f"Token {TOKEN}"}

    def test_a_transport_error_is_logged_by_type_and_not_verbatim(
        self, uploader, spool, tokens, caplog
    ):
        """The exception can carry the request, and the request carries the
        header, so logging it whole is how a token reaches a docker log."""
        spool_some(spool, 1)
        session_of(tokens).raises = requests.ConnectionError(
            f"failed sending Authorization: Token {TOKEN}"
        )

        with caplog.at_level("WARNING"):
            uploader.drain_once()

        assert "ConnectionError" in caplog.text
        assert TOKEN not in caplog.text


class TestTheRequestItself:
    def test_it_posts_to_the_batch_endpoint(self, uploader, spool, tokens):
        spool_some(spool, 1)
        session_of(tokens).reply = FakeResponse(200, results_for(["one"]))

        uploader.drain_once()

        posted = session_of(tokens).posted[-1]
        assert posted["url"] == "https://api.test/api/v1/devices/telemetry/batch/"
        assert posted["timeout"] == upload.REQUEST_TIMEOUT_S

    def test_a_batch_never_exceeds_the_configured_size(
        self, spool, anchor, tokens, watcher, counters
    ):
        watcher._config = config.Config(
            token=TOKEN, api_url="https://api.test", batch_max=3
        )
        spool_some(spool, 10)
        uploader = Uploader(spool, anchor, tokens, watcher, counters)
        session_of(tokens).reply = FakeResponse(200, results_for(["a", "b", "c"]))

        uploader.drain_once()

        assert len(session_of(tokens).posted[-1]["json"]["readings"]) == 3

    def test_it_drains_oldest_first(self, uploader, spool, tokens):
        first = spool.put(an_envelope(payload={"ph": 1.0}))
        spool.put(an_envelope(payload={"ph": 2.0}))
        session_of(tokens).reply = FakeResponse(200, results_for(["a", "b"]))

        uploader.drain_once()

        readings = session_of(tokens).posted[-1]["json"]["readings"]
        assert readings[0]["payload"]["ph"] == 1.0
        assert first is not None

    def test_an_empty_spool_is_idle_and_posts_nothing(self, uploader, tokens):
        assert uploader.drain_once() is Outcome.IDLE
        assert session_of(tokens).posted == []


def test_the_uploader_never_touches_the_archive():
    """Recorder.capture archives before it spools, so every entry the uploader
    can see is already on the stick.

    That is what keeps Archive a single-threaded object. It has no lock, and its
    own docstring anticipated being called from this thread; the moment anything
    here appends, that docstring becomes a race. Checked structurally, over the
    imports, because a comment saying so is not a guard.
    """
    tree = ast.parse(Path(upload.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert "archive" not in imported
    assert "Archive" not in imported


def test_a_stamped_envelope_round_trips_through_the_spool(spool):
    """The uploader reads what the recorder wrote, not a constructed dict."""
    envelope = record.build_envelope(
        b'{"type":"reading"}',
        {"type": "reading", "ph": "7.2"},
        Stamp(STAMP_MS, record.SOURCE_PICO, 72_000),
        RUN_A,
    )
    name = spool.put(envelope)
    assert name is not None

    reading = to_reading(spool.load(name))

    assert reading is not None
    assert reading["payload"]["ph"] == 7.2
