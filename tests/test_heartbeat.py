"""The heartbeat POST: a bare beat, on the shared credential, that never stops.

On a Release boat producing no records this is the only outbound traffic, so
each test is about it going out when it should, saying nothing it should not,
believing only caddis-api's answer, and surviving whatever else comes back.

The HTTP double lives here rather than in fakes.py, as test_upload.py's does,
because what was POSTed and what came back is this suite's whole subject.
"""

import logging
from types import SimpleNamespace

import pytest
import requests

from manta_link import __main__, config, heartbeat
from manta_link.config import ConfigWatcher, TokenSession
from manta_link.health import Counters
from manta_link.heartbeat import Heartbeat

from .test_logging_setup import stub_out_the_hardware

TOKEN = "b" * 40
API = "https://api.test"
RECEIVED = {"status": "heartbeat received", "device_id": "frank"}


class FakeResponse:
    def __init__(self, status_code: int, body=RECEIVED, is_json: bool = True) -> None:
        self.status_code = status_code
        self._body = body
        self._is_json = is_json

    @property
    def ok(self) -> bool:
        # As requests has it: anything below 400, a redirect included.
        return self.status_code < 400

    def json(self):
        if not self._is_json:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.posted: list[dict] = []
        self.reply = FakeResponse(200)
        self.raises: Exception | None = None

    # Exactly these parameters, so a body, a header or anything else passed to
    # the POST is a TypeError rather than something a test has to think to check.
    def post(self, url, timeout=None):
        self.posted.append({"url": url, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return self.reply

    def close(self) -> None:
        pass


class FakeTokens(TokenSession):
    """A TokenSession whose sessions are the fake above."""

    def _build(self, cfg):
        session = FakeSession()
        if cfg.token is not None:
            session.headers["Authorization"] = f"Token {cfg.token}"
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
def tokens():
    made = FakeTokens()
    made.rebind(config.Config(token=TOKEN, api_url=API))
    return made


@pytest.fixture
def watcher(tmp_path, monkeypatch, tokens):
    monkeypatch.delenv(config.TOKEN_ENV, raising=False)
    monkeypatch.delenv(config.API_URL_ENV, raising=False)
    made = ConfigWatcher(tmp_path, tokens)
    made._config = config.Config(token=TOKEN, api_url=API)
    return made


@pytest.fixture
def beat(tokens, watcher, counters):
    return Heartbeat(tokens, watcher, counters)


def session(tokens) -> FakeSession:
    return tokens.current().session


class TestWhatGoesOut:
    def test_it_posts_to_the_heartbeat_endpoint(self, beat, tokens):
        beat.beat_once()

        assert [post["url"] for post in session(tokens).posted] == [
            f"{API}/api/v1/devices/heartbeat/"
        ]

    def test_the_beat_is_bare(self, beat, tokens):
        """Its arrival is the whole message: no firmware version, no counters.
        FakeSession.post takes nothing else, so a body would not get this far."""
        assert beat.beat_once() is True

    def test_it_never_waits_without_a_timeout(self, beat, tokens):
        beat.beat_once()

        assert session(tokens).posted[0]["timeout"] == heartbeat.REQUEST_TIMEOUT_S

    def test_nothing_goes_out_without_a_token(self, watcher, counters):
        unconfigured = FakeTokens()
        quiet = Heartbeat(unconfigured, watcher, counters)

        assert quiet.beat_once() is False
        assert session(unconfigured).posted == []
        assert counters.get("heartbeat_post_failures") == 0

    def test_a_rotated_token_is_used_on_the_next_beat(self, beat, tokens):
        beat.beat_once()
        old = session(tokens)

        tokens.rebind(config.Config(token="c" * 40, api_url=API))
        beat.beat_once()

        assert len(old.posted) == 1
        assert len(session(tokens).posted) == 1


class TestWhatComesBack:
    def test_caddis_apis_answer_is_counted_as_posted(self, beat, counters):
        assert beat.beat_once() is True
        assert counters.get("heartbeats_posted") == 1

    @pytest.mark.parametrize("status", [400, 401, 500, 503])
    def test_an_error_is_a_failure_and_says_which(
        self, beat, tokens, counters, caplog, status
    ):
        """A 400 included: caddis-api before #453 answers a bare beat with one."""
        session(tokens).reply = FakeResponse(status, {"detail": "no"})

        with caplog.at_level(logging.WARNING):
            assert beat.beat_once() is False

        assert counters.get("heartbeat_post_failures") == 1
        assert counters.get("heartbeats_posted") == 0
        assert f"HTTP {status}" in caplog.text

    @pytest.mark.parametrize("reply", [
        FakeResponse(200, "<html>Sign in to the harbour Wi-Fi</html>", is_json=False),
        FakeResponse(200, {"status": "ok"}),
        FakeResponse(200, ["heartbeat received"]),
        FakeResponse(302),
    ])
    def test_a_2xx_that_is_not_caddis_apis_is_not_counted_as_posted(
        self, beat, tokens, counters, reply
    ):
        """A captive portal answers 200 to everything, and a redirect is not an
        answer at all."""
        session(tokens).reply = reply

        assert beat.beat_once() is False
        assert counters.get("heartbeats_posted") == 0
        assert counters.get("heartbeat_post_failures") == 1

    def test_a_network_failure_logs_its_type_and_never_its_message(
        self, beat, tokens, counters, caplog
    ):
        """A requests exception can carry the request, and with it the header."""
        session(tokens).raises = requests.ConnectionError(
            f"refused, Authorization: Token {TOKEN}"
        )

        with caplog.at_level(logging.WARNING):
            assert beat.beat_once() is False

        assert counters.get("heartbeat_post_failures") == 1
        assert "ConnectionError" in caplog.text
        assert TOKEN not in caplog.text

    def test_repeated_failures_are_logged_once_per_interval(
        self, beat, tokens, counters, caplog
    ):
        session(tokens).reply = FakeResponse(503)

        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                beat.beat_once()

        assert counters.get("heartbeat_post_failures") == 5
        assert caplog.text.count("heartbeat POST failed") == 1


def patch_sleep(monkeypatch, stop_after: int) -> list[float]:
    """Replace this module's `time`, not the global time.sleep, which other
    tests' daemon threads may still be calling."""
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == stop_after:
            raise SystemExit

    monkeypatch.setattr(heartbeat, "time", SimpleNamespace(sleep=sleep))
    return sleeps


class TestTheLoop:
    def test_it_beats_before_it_first_sleeps(self, beat, tokens, monkeypatch):
        """So caddis-api hears of a restart at once, not a minute later."""
        sleeps = patch_sleep(monkeypatch, stop_after=1)

        with pytest.raises(SystemExit):
            beat.run_forever()

        assert len(session(tokens).posted) == 1
        assert sleeps == [heartbeat.BEAT_INTERVAL_S]

    def test_a_failed_beat_does_not_end_the_loop(self, beat, tokens, monkeypatch):
        session(tokens).raises = requests.Timeout()
        patch_sleep(monkeypatch, stop_after=2)

        with pytest.raises(SystemExit):
            beat.run_forever()

        assert len(session(tokens).posted) == 2


class TestTheWiring:
    def test_main_runs_it_as_a_supervised_worker(self, monkeypatch, tmp_path):
        registered: list[str] = []
        stub_out_the_hardware(monkeypatch, lambda *_, **__: None)
        monkeypatch.setattr(__main__, "Health", lambda *_, **__: SimpleNamespace(
            register=lambda name, _run: registered.append(name),
            start=lambda: None,
            check_from_main_thread=lambda: None,
            note_restart=lambda *_, **__: None,
        ))
        monkeypatch.setattr(__main__.logging_setup, "configure",
                            lambda: SimpleNamespace(stop=lambda: None))
        monkeypatch.setenv(__main__.VOLUME_ENV, str(tmp_path))

        assert __main__.main([]) == 0
        assert "heartbeat" in registered
