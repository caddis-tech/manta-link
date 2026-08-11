"""Reading one MAVLink message out of MAVLink2Rest, and nothing else.

Transport only: this module knows about HTTP and about the three shapes
mavlink2rest wraps a value in. It knows nothing about fixes, staleness, or what
any field means. `gps.py` owns all of that.

`http.client` rather than `urllib.request`, and rather than `requests`.
`urllib.request.build_opener` installs HTTPRedirectHandler, FTPHandler,
FileHandler and DataHandler whatever you pass it, so a 302 from a service
answering on the host's own network namespace would send this privileged
container's poll wherever the response said. `http.client` follows nothing,
consults no proxy environment, and speaks no scheme but the one it dialled.
Declining `requests` here is also what keeps the isolation in the module graph
rather than in a review comment: a client that has never been handed a Session
cannot leak the credential on it.

Nothing here raises. A poll that failed and a message the autopilot has never
sent are different answers, not exceptions, and telling them apart is the whole
reason this returns an Answer rather than a dict or None.
"""

import enum
import http.client
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .logging_setup import Throttle

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:6040"

# The boat has one autopilot and we read its own component. Both are 1 on the
# bench Navigator; nothing has ever presented a second vehicle on this link.
VEHICLE_ID = 1
COMPONENT_ID = 1
MESSAGE_PATH = f"/v1/mavlink/vehicles/{VEHICLE_ID}/components/{COMPONENT_ID}/messages"

# A loopback service on the same host answers in single-digit milliseconds, so
# this is two orders of magnitude of headroom rather than a tuned value. It
# bounds one GET: a caller fetching four messages can spend four times this,
# which is why a position's age is derived when it is read rather than from how
# often this is polled.
HTTP_TIMEOUT_S = 1.0

# A GPS message is a few hundred bytes. This is not a tuned bound either; it is
# the point past which the answer is not mavlink2rest and reading the rest of it
# buys nothing.
MAX_BODY_BYTES = 64 * 1024

FAILURE_LOG_INTERVAL_S = 300.0

# Message names are module constants here, never anything off the wire. Checked
# anyway, because the one thing that must never happen to a URL built by string
# join is that something else gets to choose part of the path.
_MESSAGE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class Outcome(enum.Enum):
    """What a poll came back with. Only OBSERVED carries a message."""

    OBSERVED = "observed"
    # The service answered, and said it has never seen this message. A real
    # state: an ArduPilot build with no airspeed source never sends VFR_HUD.
    # Reporting it as unreachable, or as unreadable, sends a reader to debug a
    # service that is perfectly healthy, once a log interval, forever.
    #
    # Measured against the bench rig on 2026-08-11: for a message it has never
    # received, mavlink2rest answers HTTP 200, Content-Type application/json,
    # and a four-byte body reading `None`. That is Python's repr rather than
    # JSON's `null`, so it does not parse, and reading it as garbage is how a
    # perfectly healthy service ends up reported as broken. It does not 404,
    # which was the first guess and was wrong.
    ABSENT = "absent"
    UNREACHABLE = "unreachable"
    MALFORMED = "malformed"
    OVERSIZE = "oversize"


@dataclass(frozen=True)
class Observation:
    """One message's fields, and mavlink2rest's own receive count for it.

    The counter is the part that says whether the autopilot is still talking.
    mavlink2rest serves the last message it received forever, so a 200 proves
    the service is up and proves nothing at all about the vehicle.
    """

    fields: Mapping[str, Any]
    counter: int | None


@dataclass(frozen=True)
class Answer:
    """One poll's result. `observation` is set only when outcome is OBSERVED."""

    outcome: Outcome
    observation: Observation | None = None


class BadUrl(ValueError):
    """The configured base URL is not one this will dial."""


class Mavlink2Rest:
    """A parsed base URL and the one call that uses it."""

    def __init__(self, base_url: str = DEFAULT_URL, timeout_s: float = HTTP_TIMEOUT_S):
        self._host, self._port, self._prefix, self.url = _split_base_url(base_url)
        self._timeout_s = timeout_s
        self._unreachable_log = Throttle(FAILURE_LOG_INTERVAL_S)
        self._body_log = Throttle(FAILURE_LOG_INTERVAL_S)

    def message(self, name: str) -> Answer:
        """Fetch one message by name. Never raises."""
        if _MESSAGE_NAME_RE.match(name) is None:
            raise ValueError(f"not a MAVLink message name: {name!r}")

        path = f"{self._prefix}{MESSAGE_PATH}/{name}"
        try:
            status, body = self._get(path)
        except (OSError, http.client.HTTPException) as exc:
            # OSError covers the socket and the timeout. HTTPException is here
            # for BadStatusLine, which getresponse() raises on a garbage status
            # line and which is not an OSError, so naming only OSError would let
            # it escape into the supervisor as a restart.
            #
            # A body cut off mid-answer does not arrive here: read(amt) returns
            # what it got rather than raising, so a truncated response comes
            # back as MALFORMED below. That is the honest outcome for it.
            self._report_unreachable(name, exc)
            return Answer(Outcome.UNREACHABLE)

        # Kept although the rig never sends it, because a 404 is the other
        # obvious way to say the same thing and costs one comparison to accept.
        if status == 404:
            return Answer(Outcome.ABSENT)
        if status != 200:
            self._report_body(name, f"HTTP {status}")
            return Answer(Outcome.MALFORMED)
        if len(body) > MAX_BODY_BYTES:
            self._report_body(name, f"body over {MAX_BODY_BYTES} bytes")
            return Answer(Outcome.OVERSIZE)

        if _says_nothing_yet(body):
            return Answer(Outcome.ABSENT)

        try:
            parsed = json.loads(body)
        except ValueError:
            self._report_body(name, "body is not JSON")
            return Answer(Outcome.MALFORMED)

        observation = _observation_from(parsed)
        if observation is None:
            self._report_body(name, "body is not a mavlink2rest message")
            return Answer(Outcome.MALFORMED)
        return Answer(Outcome.OBSERVED, observation)

    def _get(self, path: str) -> "tuple[int, bytes]":
        connection = http.client.HTTPConnection(
            self._host, self._port, timeout=self._timeout_s
        )
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            # One byte past the cap, so oversize is detectable rather than
            # silently indistinguishable from a body that fits exactly.
            return response.status, response.read(MAX_BODY_BYTES + 1)
        finally:
            connection.close()

    def _report_unreachable(self, name: str, exc: Exception) -> None:
        if self._unreachable_log.should_emit():
            log.warning("could not reach MAVLink2Rest at %s for %s (%s: %s); %d "
                        "more suppressed since the last of these",
                        self.url, name, type(exc).__name__, exc,
                        self._unreachable_log.take_suppressed())

    def _report_body(self, name: str, reason: str) -> None:
        if self._body_log.should_emit():
            log.warning("MAVLink2Rest at %s answered %s with something "
                        "unreadable (%s); %d more suppressed since the last of "
                        "these", self.url, name, reason,
                        self._body_log.take_suppressed())


def unwrap(value: Any) -> Any:
    """The value inside whichever shape mavlink2rest wrapped it in.

    Three shapes are in the wild: a bare scalar, `{"type": "GPS_FIX_TYPE_3D_FIX"}`
    for an enum, and `{"type": "int32_t", "value": 473978234}` for a scalar an
    older build wrapped. `base_mode`'s `{"bits": 65}` fits none of them and has
    its own reader below.
    """
    if not isinstance(value, dict):
        return value
    if "value" in value:
        return value["value"]
    return value.get("type")


def as_int(value: Any) -> int | None:
    """An int, through whatever wrapper, or None. bool is not an int here."""
    unwrapped = unwrap(value)
    if isinstance(unwrapped, bool) or not isinstance(unwrapped, int):
        return None
    return unwrapped


def as_float(value: Any) -> float | None:
    unwrapped = unwrap(value)
    if isinstance(unwrapped, bool) or not isinstance(unwrapped, int | float):
        return None
    return float(unwrapped)


def as_name(value: Any) -> str | None:
    """An enum member's name, for the fields sent as {"type": NAME}."""
    unwrapped = unwrap(value)
    return unwrapped if isinstance(unwrapped, str) else None


def base_mode_bits(value: Any) -> int | None:
    """base_mode arrives as {"bits": 65}, which unwrap cannot read."""
    if isinstance(value, dict) and "bits" in value:
        return as_int(value["bits"])
    return as_int(value)


def _split_base_url(base_url: str) -> "tuple[str, int, str, str]":
    """Host, port, path prefix, and the form safe to put in a log line."""
    parsed = urlsplit(base_url)

    if parsed.scheme != "http":
        # Deliberately not https. This dials a service in the host's own network
        # namespace, so TLS protects nothing here, and supporting it would add a
        # certificate path that no test covers and that fails before NTP for the
        # same reason the uploader's does.
        raise BadUrl(f"MAVLink2Rest URL must be http, not {parsed.scheme!r}")
    if parsed.username or parsed.password:
        # Refused rather than stripped: this string reaches a log line, and a
        # credential nobody meant to put here should stop the process rather
        # than be quietly dropped and forgotten about.
        raise BadUrl("MAVLink2Rest URL must carry no credentials")
    if not parsed.hostname:
        raise BadUrl(f"MAVLink2Rest URL names no host: {base_url!r}")

    port = parsed.port or 80
    prefix = parsed.path.rstrip("/")
    return parsed.hostname, port, prefix, f"http://{parsed.hostname}:{port}{prefix}"


def _says_nothing_yet(body: bytes) -> bool:
    """Whether this is the service saying it has never seen that message.

    `None` is what the bench rig sends and is not valid JSON, so it has to be
    matched before the parser rather than after it. `null` is what a service
    that meant it would send, and costs nothing to accept alongside.
    """
    return body.strip() in (b"None", b"null")


def _observation_from(parsed: Any) -> Observation | None:
    if not isinstance(parsed, dict):
        return None

    fields = parsed.get("message")
    if not isinstance(fields, dict):
        return None
    return Observation(fields, _counter_of(parsed))


def _counter_of(parsed: Mapping[str, Any]) -> int | None:
    """mavlink2rest's receive count for this message, if it reported one."""
    status = parsed.get("status")
    if not isinstance(status, dict):
        return None
    when = status.get("time")
    if not isinstance(when, dict):
        return None
    counter = when.get("counter")
    if isinstance(counter, bool) or not isinstance(counter, int):
        return None
    return counter
