"""What this actually puts on the socket, and what it makes of the answer.

Every transport test here runs against a real HTTP server on a loopback port. A
stubbed socket would prove what this module meant to send; only a server can say
what it sent, and "does it follow a redirect" and "does it consult a proxy" are
questions about the client library rather than about our code, so answering them
from the docs would be answering the wrong question.
"""

import http.client
import json
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from manta_link import mavlink2rest
from manta_link.mavlink2rest import BadUrl, Mavlink2Rest, Outcome

from .golden import mavlink

POSITION = "GLOBAL_POSITION_INT"


@dataclass
class Reply:
    """What the fake service answers with, and how badly."""

    status: int = 200
    body: bytes = b'{"message": {}}'
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0
    truncate: bool = False


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        service = self.server.service  # type: ignore[attr-defined]
        service.received.append((self.path, dict(self.headers)))
        reply = service.reply

        if reply.delay_s:
            self.connection.settimeout(None)
            threading.Event().wait(reply.delay_s)

        self.send_response(reply.status)
        for key, value in reply.headers.items():
            self.send_header(key, value)
        if reply.truncate:
            # A length we do not honour, then hang up. This is a mavlink2rest
            # restarted while it was answering.
            self.send_header("Content-Length", str(len(reply.body) + 64))
            self.end_headers()
            self.wfile.write(reply.body)
            self.close_connection = True
            return
        self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        self.wfile.write(reply.body)

    def log_message(self, *_args) -> None:
        """Silence. The suite's output is not this server's log."""


class _QuietServer(HTTPServer):
    def handle_error(self, request, client_address) -> None:
        """Say nothing. The truncate case provokes this on purpose.

        The stock handler prints a traceback to stderr, and a suite that prints
        a traceback on a passing run teaches everyone to ignore tracebacks.
        """


class FakeService:
    """A real HTTP server on a loopback port, scriptable per test."""

    def __init__(self) -> None:
        self.reply = Reply()
        self.received: list[tuple[str, dict[str, str]]] = []
        self._server = _QuietServer(("127.0.0.1", 0), _Handler)
        self._server.service = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def address(self) -> "tuple[str, int]":
        host, port = self._server.server_address[:2]
        return host, port

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


@pytest.fixture
def service():
    running = FakeService()
    yield running
    running.close()


@pytest.fixture
def link(service):
    return Mavlink2Rest(service.url)


class TestWhatGoesOnTheWire:
    def test_the_poll_carries_no_authorization_and_no_cookie(self, service, link):
        """The one header rule that matters.

        Asserting the exact header set would fail on day one: http.client always
        sends Host and Accept-Encoding, and adding them to an expected list
        turns this into a test of the standard library.
        """
        service.reply = Reply(body=mavlink("global_position_int"))

        link.message(POSITION)

        _, headers = service.received[0]
        sent = {name.lower() for name in headers}
        assert "authorization" not in sent
        assert "cookie" not in sent

    def test_the_path_is_the_one_mavlink2rest_serves(self, service, link):
        link.message(POSITION)

        path, _ = service.received[0]
        assert path == (
            f"/v1/mavlink/vehicles/1/components/1/messages/{POSITION}"
        )

    def test_a_base_url_with_a_path_prefix_keeps_it(self, service):
        # BlueOS serves this behind its own reverse proxy, so the base URL is
        # not always a bare host and port.
        link = Mavlink2Rest(f"{service.url}/mavlink2rest")

        link.message(POSITION)

        path, _ = service.received[0]
        assert path.startswith("/mavlink2rest/v1/mavlink/")

    def test_no_proxy_is_consulted_even_when_the_environment_names_one(
        self, service, monkeypatch
    ):
        # urllib would honour these and send the poll somewhere else entirely.
        # This container runs on the host's network namespace, so whatever the
        # host has configured is in the environment.
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
        service.reply = Reply(body=mavlink("global_position_int"))

        answer = Mavlink2Rest(service.url).message(POSITION)

        assert answer.outcome is Outcome.OBSERVED
        assert len(service.received) == 1

    def test_a_redirect_is_refused_rather_than_followed(self, service, link):
        """urllib.request installs a redirect handler whatever you pass it.

        A 302 from a service on the host's own network namespace would send this
        privileged container's poll wherever the response named.
        """
        elsewhere = FakeService()
        try:
            service.reply = Reply(
                status=302, headers={"Location": f"{elsewhere.url}/anything"}
            )

            answer = link.message(POSITION)

            assert answer.outcome is Outcome.MALFORMED
            assert elsewhere.received == []
        finally:
            elsewhere.close()


class TestWhatComesBack:
    def test_a_captured_response_reads_as_an_observation(self, service, link):
        service.reply = Reply(body=mavlink("global_position_int"))

        answer = link.message(POSITION)

        assert answer.outcome is Outcome.OBSERVED
        assert answer.observation is not None
        assert answer.observation.fields["lat"] == 399350992
        assert answer.observation.counter == 2156

    def test_a_message_the_autopilot_never_sends_is_not_an_unreachable_service(
        self, service, link
    ):
        """A real state, not a fault.

        An ArduPilot build with no airspeed source never sends VFR_HUD. Reported
        as unreachable, or as unreadable, it sends a reader to debug a service
        that is answering perfectly, once every log interval, forever.

        The body is what the bench rig actually returns, measured 2026-08-11:
        HTTP 200, Content-Type application/json, and four bytes reading `None`.
        That is Python's repr, not JSON's `null`, so it does not parse. Guessing
        got this wrong twice, first as a 404 and then as `null`, which is why it
        asserts against captured bytes rather than a remembered shape.
        """
        service.reply = Reply(status=200, body=b"None")

        assert link.message("VFR_HUD").outcome is Outcome.ABSENT

    @pytest.mark.parametrize("body", [b"null", b"None\n", b"  null  "])
    def test_a_service_that_meant_json_is_read_the_same_way(
        self, service, link, body
    ):
        service.reply = Reply(status=200, body=body)

        assert link.message("VFR_HUD").outcome is Outcome.ABSENT

    def test_a_404_is_read_the_same_way(self, service, link):
        # Not what this mavlink2rest does, but it is the other obvious way to
        # say it and costs one comparison to accept.
        service.reply = Reply(status=404, body=b"")

        assert link.message("VFR_HUD").outcome is Outcome.ABSENT

    def test_an_absent_message_is_not_logged_as_unreadable(self, service, link, caplog):
        """The whole point of telling absent from malformed.

        Logged as unreadable, a boat with no airspeed source reports a fault
        every log interval for as long as it runs.
        """
        service.reply = Reply(status=200, body=b"None")

        with caplog.at_level("WARNING"):
            link.message("VFR_HUD")

        assert caplog.text == ""

    def test_an_unreachable_service_is_not_a_raise(self):
        # Nothing is listening on this port. The poller runs under the
        # supervisor, so a raise here would be a restart loop rather than a
        # nulled position.
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()

        answer = Mavlink2Rest(f"http://127.0.0.1:{port}", timeout_s=1.0).message(
            POSITION
        )

        assert answer.outcome is Outcome.UNREACHABLE
        assert answer.observation is None

    def test_a_slow_server_is_given_up_on_rather_than_waited_for(self, service):
        service.reply = Reply(delay_s=2.0, body=mavlink("global_position_int"))

        answer = Mavlink2Rest(service.url, timeout_s=0.15).message(POSITION)

        assert answer.outcome is Outcome.UNREACHABLE

    def test_a_body_cut_off_mid_answer_is_no_observation_rather_than_a_raise(
        self, service, link
    ):
        service.reply = Reply(body=b'{"message": {"lat": 3993', truncate=True)

        answer = link.message(POSITION)

        assert answer.outcome is Outcome.MALFORMED
        assert answer.observation is None

    def test_an_oversized_body_is_refused_separately_from_garbage(
        self, service, link
    ):
        # Its own outcome rather than folded into MALFORMED, so an operator can
        # tell "this is not our service" from "our service said too much".
        service.reply = Reply(body=b" " * (mavlink2rest.MAX_BODY_BYTES + 1))

        assert link.message(POSITION).outcome is Outcome.OVERSIZE

    @pytest.mark.parametrize(
        "body",
        [
            b"not json at all",
            b"[]",
            b'"a string"',
            b"0",
            b'{"status": {}}',
            b'{"message": []}',
            b'{"message": "GLOBAL_POSITION_INT"}',
        ],
    )
    def test_a_body_that_is_not_a_message_is_malformed(self, service, link, body):
        service.reply = Reply(body=body)

        assert link.message(POSITION).outcome is Outcome.MALFORMED

    @pytest.mark.parametrize(
        "status_block",
        [
            b'{"message": {}}',
            b'{"message": {}, "status": null}',
            b'{"message": {}, "status": {}}',
            b'{"message": {}, "status": {"time": {}}}',
            b'{"message": {}, "status": {"time": {"counter": "2156"}}}',
            b'{"message": {}, "status": {"time": {"counter": true}}}',
        ],
    )
    def test_a_missing_or_unreadable_counter_is_none_rather_than_a_crash(
        self, service, link, status_block
    ):
        service.reply = Reply(body=status_block)

        answer = link.message(POSITION)

        assert answer.outcome is Outcome.OBSERVED
        assert answer.observation is not None
        assert answer.observation.counter is None


class TestTheBaseUrlIsCheckedOnce:
    def test_a_url_carrying_a_credential_is_refused_at_construction(self):
        # Refused rather than stripped: this string reaches a log line, and a
        # credential nobody meant to put here should stop the process rather
        # than be quietly dropped.
        with pytest.raises(BadUrl):
            Mavlink2Rest("http://user:secret@127.0.0.1:6040")

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1:6040",
            "file:///etc/passwd",
            "ftp://127.0.0.1",
            "127.0.0.1:6040",
            "",
        ],
    )
    def test_anything_but_http_is_refused_at_construction(self, url):
        with pytest.raises(BadUrl):
            Mavlink2Rest(url)

    def test_the_logged_url_carries_no_credential_even_if_one_was_offered(self):
        try:
            Mavlink2Rest("http://user:secret@127.0.0.1:6040")
        except BadUrl as exc:
            assert "secret" not in str(exc)

    def test_a_message_name_off_the_wire_cannot_reach_the_path(self, link):
        # Every caller passes a module constant. This is the guard that keeps
        # that true rather than conventional.
        for hostile in ["../../admin", "GLOBAL POSITION", "a/b", "", "lowercase"]:
            with pytest.raises(ValueError):
                link.message(hostile)


class TestTheShapeReaders:
    def test_bare_integers_are_read_as_sent(self):
        assert mavlink2rest.as_int(399350992) == 399350992

    def test_the_older_wrapped_scalar_reads_to_the_same_value(self):
        wrapped = {"type": "int32_t", "value": 399350992}
        assert mavlink2rest.as_int(wrapped) == 399350992

    def test_both_wrappings_of_one_position_agree(self):
        bare = json.loads(mavlink("global_position_int"))["message"]
        wrapped = json.loads(mavlink("global_position_int_wrapped"))["message"]

        assert mavlink2rest.as_int(bare["lat"]) == mavlink2rest.as_int(wrapped["lat"])
        assert mavlink2rest.as_int(bare["lon"]) == mavlink2rest.as_int(wrapped["lon"])

    def test_a_nested_enum_reads_as_its_name(self):
        raw = json.loads(mavlink("gps_raw_int_3d"))["message"]
        assert mavlink2rest.as_name(raw["fix_type"]) == "GPS_FIX_TYPE_3D_FIX"

    def test_an_enum_is_not_a_number(self):
        raw = json.loads(mavlink("gps_raw_int_3d"))["message"]
        assert mavlink2rest.as_int(raw["fix_type"]) is None

    def test_base_mode_bits_are_read_from_their_own_shape(self):
        beat = json.loads(mavlink("heartbeat"))["message"]
        assert mavlink2rest.base_mode_bits(beat["base_mode"]) == 65

    def test_groundspeed_reads_as_a_float(self):
        hud = json.loads(mavlink("vfr_hud"))["message"]
        assert mavlink2rest.as_float(hud["groundspeed"]) == pytest.approx(0.0439258)

    def test_an_integer_reads_as_a_float_too(self):
        assert mavlink2rest.as_float(4) == 4.0

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_is_not_a_number(self, value):
        # bool subclasses int, so an unguarded isinstance reads True as one
        # satellite, or as a latitude of a ten-millionth of a degree.
        assert mavlink2rest.as_int(value) is None
        assert mavlink2rest.as_float(value) is None

    @pytest.mark.parametrize("value", [None, "12", [], {}, {"type": "uint8_t"}])
    def test_anything_else_is_no_number_at_all(self, value):
        assert mavlink2rest.as_int(value) is None

    def test_a_name_is_not_read_from_a_number(self):
        assert mavlink2rest.as_name(12) is None


def test_the_fake_service_is_a_real_http_server(service):
    """The suite above is only worth having if this is true."""
    connection = http.client.HTTPConnection(*service.address)
    try:
        connection.request("GET", "/anything")
        assert connection.getresponse().status == 200
    finally:
        connection.close()
    assert service.received[-1][0] == "/anything"
