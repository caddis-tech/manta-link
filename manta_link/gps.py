"""Where the boat was, polled off the autopilot and cached for the capture path.

The Pico has no GPS. Every `gps_latitude` that ever reaches caddis-api comes
from here, so a wrong answer here is a reading filed against the wrong water and
a create-only API with no way to correct it.

Two independent staleness questions, which fail in different ways and so are
answered separately:

*Is the autopilot still talking?* mavlink2rest serves the last message it
received forever, so a 200 proves the service is up and proves nothing about the
vehicle. Only `status.time.counter` moves, and it is compared for change rather
than for increase because mavlink2rest zeroes it on restart, which the bench rig
was measured doing. That question is answered here.

*Is this snapshot current?* A poller wedged in a socket read stays `is_alive()`
forever, so the watchdog never restarts it, and a capture worker draining an
eleven-minute backlog reads a snapshot far newer than the record it is stamping.
So nothing here publishes an age. It publishes the monotonic instant a fix was
accepted, and `record.position_payload_fields` works out the age against the
moment its own record was received. That question is answered there.

Nothing on this module's path is reachable from the reader thread, and the
capture thread only ever reads one attribute.
"""

import enum
import logging
import time
from dataclasses import dataclass
from typing import Any

from . import mavlink2rest
from .health import Counters
from .logging_setup import Throttle
from .mavlink2rest import Mavlink2Rest, Outcome

log = logging.getLogger(__name__)

POSITION_MESSAGE = "GLOBAL_POSITION_INT"
QUALITY_MESSAGE = "GPS_RAW_INT"
HEARTBEAT_MESSAGE = "HEARTBEAT"
SPEED_MESSAGE = "VFR_HUD"

# Both position messages stream at 1 Hz on the bench Navigator.
POLL_INTERVAL_S = 1.0

# The vehicle tags are context, not measurement, and cost two more GETs. At a
# 2.5s record cycle this still puts a fresh one on most records.
TAG_INTERVAL_S = 5.0

# Position and quality arrive as two messages whose counters need not advance in
# the same pass. Requiring both in one pass would drop fixes on a beat
# frequency; this is the window in which a quality reading still describes the
# position beside it.
FIX_QUALITY_MAX_AGE_S = 5.0

# A fix good enough to file a water sample against. 2D is deliberately absent:
# the only consumer is a strict ray-cast polygon containment with no
# low-confidence channel, so a 2D fix under poor geometry files a sample in the
# neighbouring pond, permanently.
GPS_FIX_TYPES_USABLE = frozenset({
    "GPS_FIX_TYPE_3D_FIX",
    "GPS_FIX_TYPE_DGPS",
    "GPS_FIX_TYPE_RTK_FLOAT",
    "GPS_FIX_TYPE_RTK_FIXED",
    "GPS_FIX_TYPE_STATIC",
    "GPS_FIX_TYPE_PPP",
})

# Told apart from the set above so "no fix yet", which is every cold start, is
# distinguishable from a name this was never taught. The second is worth a line;
# the first is not.
GPS_FIX_TYPES_REFUSED = frozenset({
    "GPS_FIX_TYPE_NO_GPS",
    "GPS_FIX_TYPE_NO_FIX",
    "GPS_FIX_TYPE_2D_FIX",
})

# MAV_MODE_FLAG_SAFETY_ARMED. The motors-armed bit is the one signal in the
# heartbeat with operational meaning: it is the difference between a boat under
# way and a boat sitting on a trailer with its logger running.
ARMED_BIT = 0b1000_0000

# GPS_RAW_INT reports eph in centimetres of horizontal dilution, and UINT16_MAX
# when it does not know. 655.35 is not a dilution figure, it is the sentinel.
EPH_UNKNOWN = 65535

# Coordinates outside these are not a place. Latitude is checked before
# longitude only so the failure names the one that is wrong.
LATITUDE_LIMIT = 90.0
LONGITUDE_LIMIT = 180.0

STALL_LOG_INTERVAL_S = 300.0
FIX_LOG_INTERVAL_S = 300.0


@dataclass(frozen=True)
class Tags:
    """Vehicle context, carried with no policy applied to it.

    caddis-api has no reader property for any of these, so they ride in the
    archive and in the payload blob and are invisible to every consumer until
    one is added. `mode` is the raw custom_mode integer: its meaning is vehicle
    type dependent, ArduRover and ArduSub disagree on it, and inventing a name
    for it here would be a guess written down as a fact.
    """

    armed: bool | None = None
    mode: int | None = None
    vehicle_type: str | None = None
    groundspeed: float | None = None


@dataclass(frozen=True)
class Position:
    """What the poller last knew, and when it knew it.

    Published by replacement and never mutated, which is what lets the capture
    thread read it with a plain attribute load and no lock.
    """

    published_at: float
    fix_at: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    fix_type: str | None = None
    satellites: int | None = None
    hdop: float | None = None
    # A real coordinate this refused to promote, kept because refusing to file a
    # 2D fix against a waterway is defensible and throwing it away is not:
    # caddis-api's backfill_waterway_assignment can only re-resolve something
    # that was written down.
    refused_latitude: float | None = None
    refused_longitude: float | None = None
    refused_fix_type: str | None = None
    tags: Tags = Tags()
    tags_at: float | None = None

    def as_envelope(self) -> dict[str, Any]:
        """The JSON-safe form the spool and the archive carry."""
        return {
            "published_at": self.published_at,
            "fix_at": self.fix_at,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "fix_type": self.fix_type,
            "satellites": self.satellites,
            "hdop": self.hdop,
            "refused_latitude": self.refused_latitude,
            "refused_longitude": self.refused_longitude,
            "refused_fix_type": self.refused_fix_type,
            "tags_at": self.tags_at,
            "mav_armed": self.tags.armed,
            "mav_mode": self.tags.mode,
            "mav_vehicle_type": self.tags.vehicle_type,
            "mav_groundspeed": self.tags.groundspeed,
        }


class PositionCache:
    """The one attribute the capture thread reads.

    A plain attribute load of a frozen object is atomic under the GIL, so this
    needs no lock, and taking one would put the only lock in the process on the
    capture thread's hot path for a single read.
    """

    def __init__(self) -> None:
        self._latest: Position | None = None

    @property
    def latest(self) -> Position | None:
        return self._latest

    def publish(self, position: Position) -> None:
        self._latest = position


class Change(enum.Enum):
    """What one look at a message's counter established."""

    ADVANCED = "advanced"
    # The first look. Nothing is wrong and nothing is known yet, and telling
    # this apart from UNCHANGED is what keeps a stall counter at zero on a
    # healthy boat: one that reads 1 from the moment it starts is one nobody
    # looks at when it reads 400.
    SEEDED = "seeded"
    UNCHANGED = "unchanged"
    # No counter in the body at all, so nothing can be concluded either way.
    UNKNOWN = "unknown"


class Freshness:
    """Whether one message has changed since this process started watching it.

    Seeded rather than trusted. The first counter this sees records the value
    and establishes nothing, because an autopilot switched off two hours ago
    still has mavlink2rest serving its last message: without the seed, the first
    pass after any restart publishes a two-hour-old coordinate as brand new, and
    does it again on every restart.
    """

    def __init__(self) -> None:
        self._counter: int | None = None
        self._seeded = False

    def look(self, counter: int | None) -> Change:
        if counter is None:
            # No way to tell a live message from a stored one, and guessing
            # "live" is the direction that files bad positions.
            return Change.UNKNOWN
        previous, self._counter = self._counter, counter
        if not self._seeded:
            self._seeded = True
            return Change.SEEDED
        # Changed, not increased. mavlink2rest zeroes its counters on restart,
        # measured on the bench rig across two runs an hour apart, and a
        # greater-than test would freeze the position until the count climbed
        # back past where it was.
        return Change.ADVANCED if counter != previous else Change.UNCHANGED


@dataclass(frozen=True)
class FixVerdict:
    """What GPS_RAW_INT said about the fix, and whether we believe it."""

    name: str | None
    usable: bool
    recognised: bool


def read_fix_type(fields: dict[str, Any]) -> FixVerdict:
    name = mavlink2rest.as_name(fields.get("fix_type"))
    if name is None:
        # Includes an integer fix_type, which is refused rather than mapped: a
        # number-to-name table here is a guess about which enum revision the
        # sender used, and the guess is invisible when it is wrong.
        return FixVerdict(None, False, False)
    if name in GPS_FIX_TYPES_USABLE:
        return FixVerdict(name, True, True)
    return FixVerdict(name, False, name in GPS_FIX_TYPES_REFUSED)


def read_coordinates(fields: dict[str, Any]) -> "tuple[float, float] | None":
    """Degrees from the ten-millionths MAVLink sends, or None if not a place."""
    latitude = mavlink2rest.as_int(fields.get("lat"))
    longitude = mavlink2rest.as_int(fields.get("lon"))
    if latitude is None or longitude is None:
        return None
    if abs(latitude) > LATITUDE_LIMIT * 1e7:
        return None
    if abs(longitude) > LONGITUDE_LIMIT * 1e7:
        return None
    return latitude / 1e7, longitude / 1e7


def is_null_island(latitude: float, longitude: float) -> bool:
    """Exactly zero in both, which ArduPilot publishes before the EKF has an origin.

    Survives the fix gate rather than being made redundant by it, because the
    gate reads GPS_RAW_INT and the coordinates come from GLOBAL_POSITION_INT: on
    the transition into a fix the first can already say 3D while the second
    still carries the pre-fix origin. caddis-api ray-casts (0, 0) like any other
    point and files the reading against the device's office, silently.
    """
    return latitude == 0.0 and longitude == 0.0


def read_hdop(fields: dict[str, Any]) -> float | None:
    eph = mavlink2rest.as_int(fields.get("eph"))
    if eph is None or eph == EPH_UNKNOWN:
        return None
    return eph / 100.0


def read_tags(beat: "dict[str, Any] | None", hud: "dict[str, Any] | None") -> Tags:
    armed = None
    mode = None
    vehicle_type = None
    if beat is not None:
        bits = mavlink2rest.base_mode_bits(beat.get("base_mode"))
        armed = None if bits is None else bool(bits & ARMED_BIT)
        mode = mavlink2rest.as_int(beat.get("custom_mode"))
        vehicle_type = mavlink2rest.as_name(beat.get("mavtype"))

    groundspeed = None
    if hud is not None:
        groundspeed = mavlink2rest.as_float(hud.get("groundspeed"))
    return Tags(armed, mode, vehicle_type, groundspeed)


# Which tally an unusable answer belongs to. Separate names because "the service
# is not there" and "the service said something we cannot read" send an operator
# to different places.
_OUTCOME_COUNTERS = {
    Outcome.ABSENT: "gps_message_absent",
    Outcome.UNREACHABLE: "gps_poll_failures",
    Outcome.MALFORMED: "gps_body_unreadable",
    Outcome.OVERSIZE: "gps_body_oversize",
}


class GpsPoller:
    """A Health worker that keeps the cache current. Never on the reader thread."""

    def __init__(
        self,
        link: Mavlink2Rest,
        cache: PositionCache,
        counters: Counters,
        tag_interval_s: float = TAG_INTERVAL_S,
    ) -> None:
        self._link = link
        self._cache = cache
        self._counters = counters
        self._tag_interval_s = tag_interval_s

        self._position_seen = Freshness()
        self._quality_seen = Freshness()
        self._heartbeat_seen = Freshness()

        self._fix_at: float | None = None
        self._latitude: float | None = None
        self._longitude: float | None = None
        self._fix_type: str | None = None
        self._satellites: int | None = None
        self._hdop: float | None = None
        self._refused_latitude: float | None = None
        self._refused_longitude: float | None = None
        self._refused_fix_type: str | None = None

        self._quality: FixVerdict | None = None
        self._quality_at: float | None = None
        self._tags = Tags()
        self._tags_at: float | None = None
        # Negative infinity rather than zero, so the first pass always fetches
        # whatever monotonic the process or a test happens to start at.
        self._tags_due = float("-inf")

        self._stall_log = Throttle(STALL_LOG_INTERVAL_S)
        self._fix_log = Throttle(FIX_LOG_INTERVAL_S)
        self._had_fix = False

    def run_forever(self) -> None:
        """Never returns. Supervised, and restarted if it ever does."""
        deadline = time.monotonic()
        while True:
            self.poll_once(time.monotonic())
            deadline += POLL_INTERVAL_S
            delay = deadline - time.monotonic()
            if delay <= 0.0:
                # A pass that overran its interval. Re-base rather than chase:
                # catching up only makes the next pass late as well, and the
                # age that matters is derived when a record reads this, not
                # from how regularly this ran.
                deadline = time.monotonic()
                continue
            time.sleep(delay)

    def poll_once(self, now: float) -> Position:
        """One pass. Always publishes, whether or not anything was learned.

        Publishing unconditionally is what makes a stopped poller visible: if a
        failing pass skipped the publish, the last snapshot would sit in the
        cache with its published_at frozen and nothing downstream could tell.
        """
        self._read_quality(now)
        self._read_position(now)
        if now >= self._tags_due:
            self._read_tags(now)
            self._tags_due = now + self._tag_interval_s

        position = Position(
            published_at=now,
            fix_at=self._fix_at,
            latitude=self._latitude,
            longitude=self._longitude,
            fix_type=self._fix_type,
            satellites=self._satellites,
            hdop=self._hdop,
            refused_latitude=self._refused_latitude,
            refused_longitude=self._refused_longitude,
            refused_fix_type=self._refused_fix_type,
            tags=self._tags,
            tags_at=self._tags_at,
        )
        self._cache.publish(position)
        return position

    def _observe(self, name: str) -> "mavlink2rest.Observation | None":
        answer = self._link.message(name)
        self._counters.bump("gps_polls")
        if answer.outcome is Outcome.OBSERVED:
            return answer.observation
        self._counters.bump(_OUTCOME_COUNTERS[answer.outcome])
        return None

    def _read_quality(self, now: float) -> None:
        observation = self._observe(QUALITY_MESSAGE)
        if observation is None:
            return
        if self._quality_seen.look(observation.counter) is not Change.ADVANCED:
            return

        fields = dict(observation.fields)
        self._quality = read_fix_type(fields)
        self._quality_at = now
        self._satellites = mavlink2rest.as_int(fields.get("satellites_visible"))
        self._hdop = read_hdop(fields)

    def _read_position(self, now: float) -> None:
        observation = self._observe(POSITION_MESSAGE)
        if observation is None:
            return
        change = self._position_seen.look(observation.counter)
        if change is Change.SEEDED:
            # The first look. Nothing is wrong and nothing is known: an
            # autopilot that stopped talking an hour ago serves this same
            # message, so accepting it here is the restart trap.
            return
        if change is not Change.ADVANCED:
            # The service answered with a message it already had. On a healthy
            # boat this is the pass that straddles two 1 Hz updates; on a dead
            # autopilot it is every pass from now on, which is the case the
            # counter exists to catch.
            self._counters.bump("gps_stream_stalled")
            return

        pair = read_coordinates(dict(observation.fields))
        if pair is None:
            self._counters.bump("gps_out_of_range")
            return
        latitude, longitude = pair
        if is_null_island(latitude, longitude):
            self._counters.bump("gps_null_island_rejected")
            return
        if not self._quality_is_current(now):
            self._counters.bump("gps_fix_refused_quality")
            return

        verdict = self._quality
        if verdict is None or not verdict.usable:
            self._refuse(latitude, longitude, verdict)
            return

        self._fix_at = now
        self._latitude = latitude
        self._longitude = longitude
        self._fix_type = verdict.name
        self._refused_latitude = None
        self._refused_longitude = None
        self._refused_fix_type = None
        self._counters.bump("gps_fixes")
        self._report_acquired(verdict)

    def _refuse(
        self, latitude: float, longitude: float, verdict: "FixVerdict | None"
    ) -> None:
        """Keep the coordinate, do not promote it.

        A 2D fix is a real measurement taken under geometry too poor to file a
        sample against. It stays in the archive so a later backfill can
        re-resolve it, and out of the payload so nothing files it now.
        """
        self._refused_latitude = latitude
        self._refused_longitude = longitude
        self._refused_fix_type = None if verdict is None else verdict.name
        if verdict is not None and not verdict.recognised:
            self._counters.bump("gps_fix_type_unknown")
            if self._stall_log.should_emit():
                log.warning("the autopilot reports a fix type this does not know "
                            "(%s); treating it as no fix", verdict.name)
            return
        self._counters.bump("gps_no_fix")
        self._report_lost()

    def _quality_is_current(self, now: float) -> bool:
        if self._quality_at is None:
            return False
        return now - self._quality_at <= FIX_QUALITY_MAX_AGE_S

    def _read_tags(self, now: float) -> None:
        beat = self._observe(HEARTBEAT_MESSAGE)
        if beat is None:
            self._counters.bump("gps_tags_stale")
            self._forget_tags()
            return

        change = self._heartbeat_seen.look(beat.counter)
        if change is not Change.ADVANCED:
            # A dead autopilot's last `armed: true` must not be republished
            # every pass until the container restarts. A seed is not that, so it
            # clears the tags without claiming anything went stale.
            if change is not Change.SEEDED:
                self._counters.bump("gps_tags_stale")
            self._forget_tags()
            return

        hud = self._observe(SPEED_MESSAGE)
        self._tags = read_tags(
            dict(beat.fields), None if hud is None else dict(hud.fields)
        )
        self._tags_at = now

    def _forget_tags(self) -> None:
        self._tags = Tags()
        self._tags_at = None

    def _report_acquired(self, verdict: FixVerdict) -> None:
        if self._had_fix:
            return
        self._had_fix = True
        log.info("GPS fix acquired: %s, %s satellite(s)",
                 verdict.name, self._satellites)

    def _report_lost(self) -> None:
        if not self._had_fix:
            return
        self._had_fix = False
        if self._fix_log.should_emit():
            log.warning("the autopilot no longer reports a usable fix; readings "
                        "will carry no position until it does")
