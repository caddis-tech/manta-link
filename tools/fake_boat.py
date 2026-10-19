#!/usr/bin/env python3
"""Stand in for a boat's Pico and autopilot, so the real MANTA Link can be run
against a caddis-api with nothing plugged in.

Everything from the serial line on is MANTA Link's own: framing, the TIME?
answer, record mapping, GPS enrichment, the spool, client_ref, and the batch
upload with its retries. Only its two inputs are made up. The Pico is a
pseudo-terminal speaking the firmware's line format, and MAVLink2Rest is a local
HTTP server answering in the shapes captured off the bench rig, with a position
that works lanes across a pond's traced boundary. So what reaches the API is
what a boat working that pond would send, as of whichever MANTA Link runs it.

Run it in MANTA Link's own image, with this directory mounted where the image
has none, the pond's GeoJSON (the pond page's Download GeoJSON) beside it, and
the token passed through from the shell running docker rather than typed:

    docker run --rm -it -v "$PWD/tools:/app/tools:ro" \\
        -v "$PWD/pond.geojson:/pond.geojson:ro" -e CADDIS_API_TOKEN \\
        --entrypoint python3 <manta-link image> tools/fake_boat.py /pond.geojson \\
        --api-url https://squadron-staging-928515830511.us-central1.run.app

**It never posts to production.** A fake reading there would be filed against a
real pond as if a drone had measured it. Production's hosts are refused by name,
and uploads are confined to the one API named here: MANTA Link's own fallback,
which is production, is replaced with an address that cannot resolve, so a
refused URL or a different one in the data volume's .env goes nowhere.

Not importable by the package, and excluded from the image by .dockerignore.
"""

import argparse
import json
import logging
import math
import os
import random
import re
import select
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manta_link import __main__ as entry  # noqa: E402
from manta_link import clock, config, mavlink2rest, reader  # noqa: E402
from manta_link.logging_setup import Throttle  # noqa: E402
from tools import bench_host  # noqa: E402

log = logging.getLogger("manta_link.fake_boat")

# Production's names: anything under the company domain, and its Cloud Run
# service under every address Cloud Run gives it, which answer without that
# domain: the project-number URL, a revision tag's ("<tag>---<service>-..."),
# and the older hashed form ("<service>-<hash>-uc.a.run.app").
PRODUCTION_DOMAIN = "caddistech.com"
PRODUCTION_PROJECT_NUMBER = "172019847409"
PRODUCTION_SERVICE = "django-backend"

# Where an upload goes when its URL is not the one named on the command line.
# .invalid is reserved never to resolve (RFC 2606), so it fails before a byte
# leaves the machine, and the uploader's own retry and logging take it from there.
REFUSED_API_URL = "https://refused-by-fake-boat.invalid"
REFUSAL_LOG_INTERVAL_S = 60.0

# A record every few seconds, as a streaming Pico sends them, and the window
# after boot in which the firmware asks for the time (see reader.py). Below the
# floor a typo floods whichever API this points at: nothing in a fake Pico
# takes time to measure, and MANTA Link uploads as fast as it is fed.
CADENCE_S = 4.0
MIN_CADENCE_S = 1.0
TIME_ASK_INTERVAL_S = 1.0
TIME_WINDOW_S = 180.0
FIRMWARE_VERSION = "2.1.2+fake-boat"
BANNER = f"=== Aquadrone firmware {FIRMWARE_VERSION}: SD LOGGING ENABLED ==="
_TIME_REPLY_RE = re.compile(rb"^TIME (\d+)$")

# The walk: lanes this far apart, sampled this finely, at a slow survey speed,
# so a waypoint a metre on is one autopilot update.
LANE_SPACING_M = 8.0
STEP_M = 1.0
SPEED_M_S = 1.0
METRES_PER_DEGREE_LAT = 111_320.0


# --- never production ----------------------------------------------------------


def _host(url: str) -> str:
    # A trailing dot is the same host to DNS and a different string to a
    # comparison, so it is dropped before either check sees the name.
    return (urlsplit(url.strip()).hostname or "").lower().rstrip(".")


def is_production(url: str) -> bool:
    host = _host(url)
    if host == PRODUCTION_DOMAIN or host.endswith("." + PRODUCTION_DOMAIN):
        return True
    if not host.endswith(".run.app"):
        return False
    service = host.split("---")[-1]
    return PRODUCTION_PROJECT_NUMBER in host or service.startswith(
        PRODUCTION_SERVICE + "-"
    )


def _origin(url: str) -> tuple[str, str, int | None]:
    return urlsplit(url.strip()).scheme, _host(url), urlsplit(url.strip()).port


def confine_uploads(api_url: str) -> None:
    """Let MANTA Link post to `api_url` and nowhere else.

    Rebound on the module and the class rather than passed in, as bench_host
    rebinds the clock: config.py is the one place an upload's address is
    decided, and the package should not grow a seam that exists only for this.
    """
    allowed = _origin(api_url)
    shipped = config.Config.batch_endpoint.fget
    refusals = Throttle(REFUSAL_LOG_INTERVAL_S)

    def batch_endpoint(self: config.Config) -> str:
        if _origin(self.api_url) == allowed:
            return shipped(self)
        if refusals.should_emit():
            log.error("fake boat: refusing to upload anywhere but %s", api_url)
        return shipped(config.Config(api_url=REFUSED_API_URL))

    config.DEFAULT_API_URL = REFUSED_API_URL
    config.Config.batch_endpoint = property(batch_endpoint)  # type: ignore[assignment]


# --- the pond ------------------------------------------------------------------


def pond_ring(geojson: dict[str, Any]) -> list[tuple[float, float]]:
    """A pond's outer ring as (lat, lng), from a Polygon, Feature or the
    FeatureCollection the pond page's Download GeoJSON serves."""
    shape = geojson
    if isinstance(shape, dict) and shape.get("type") == "FeatureCollection":
        shape = shape["features"][0]
    if isinstance(shape, dict) and shape.get("type") == "Feature":
        shape = shape["geometry"]
    if not isinstance(shape, dict) or shape.get("type") != "Polygon":
        found = shape.get("type") if isinstance(shape, dict) else type(shape).__name__
        raise ValueError(f"expected a GeoJSON Polygon, got {found!r}")
    return [(lat, lng) for lng, lat, *_ in shape["coordinates"][0]]


def _crossings(lat: float, ring: list[tuple[float, float]]) -> list[float]:
    """The longitudes, west to east, where a line of latitude `lat` crosses
    `ring`. Each edge counts on its lower end and not its upper one, so a
    vertex on the line is crossed once and a flat edge not at all."""
    found = []
    for (lat_a, lng_a), (lat_b, lng_b) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (lat_a > lat) != (lat_b > lat):
            found.append(lng_a + (lat - lat_a) * (lng_b - lng_a) / (lat_b - lat_a))
    return sorted(found)


def inside(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    """Whether `point` is inside `ring`: an odd number of crossings east of it."""
    lat, lng = point
    return sum(1 for crossing in _crossings(lat, ring) if lng < crossing) % 2 == 1


def survey_lanes(
    ring: list[tuple[float, float]],
    spacing_m: float = LANE_SPACING_M,
    step_m: float = STEP_M,
) -> list[tuple[float, float]]:
    """Waypoints working east and west across the pond, a lane at a time.

    Every one is inside the ring, because caddis-api files a reading under a
    pond only when its position is, since its strict geofence containment.
    """
    lats = [lat for lat, _ in ring]
    lngs = [lng for _, lng in ring]
    metres_per_degree_lng = METRES_PER_DEGREE_LAT * math.cos(
        math.radians(sum(lats) / len(lats))
    )
    lane_step = spacing_m / METRES_PER_DEGREE_LAT
    sample_step = step_m / metres_per_degree_lng

    path: list[tuple[float, float]] = []
    lat = min(lats) + lane_step / 2
    eastward = True
    while lat < max(lats):
        # Pairs of crossings bound the water along this lane, so samples are
        # taken between them rather than tested one by one against the ring.
        # On the same grid from the west edge, so every lane lines up.
        lane = []
        crossings = _crossings(lat, ring)
        for west, east in zip(crossings[::2], crossings[1::2], strict=True):
            first = math.floor((west - min(lngs)) / sample_step) + 1
            lng = min(lngs) + first * sample_step
            while lng < east:
                lane.append((lat, lng))
                lng += sample_step
        path.extend(lane if eastward else reversed(lane))
        eastward = not eastward
        lat += lane_step
    if not path:
        raise ValueError(
            "no lane crosses the pond; is its outline only a few metres across?"
        )
    return path


# --- the autopilot -------------------------------------------------------------


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat().replace("+00:00", "Z")


class Autopilot:
    """The messages MANTA Link's GPS poller asks MAVLink2Rest for, shaped like
    the ones captured off the bench rig's Navigator (tests/data/mavlink_*.json).

    The receive counter is the field that has to move: mavlink2rest serves its
    last message for ever, so the poller reads an unchanged counter as an
    autopilot that has stopped talking.
    """

    def __init__(
        self,
        path: list[tuple[float, float]],
        now: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._now = now
        self._wall = wall
        self._started = now()
        self._first_update = _iso(wall())

    def position(self) -> tuple[float, float]:
        travelled = (self._now() - self._started) * SPEED_M_S
        return self._path[int(travelled / STEP_M) % len(self._path)]

    def message(self, name: str) -> "dict[str, Any] | None":
        lat, lng = self.position()
        lat_e7, lng_e7 = round(lat * 1e7), round(lng * 1e7)
        fields: dict[str, Any]
        if name == "GLOBAL_POSITION_INT":
            fields = {"alt": 253250, "hdg": 9000, "lat": lat_e7, "lon": lng_e7,
                      "relative_alt": -49, "time_boot_ms": self._uptime_ms(),
                      "vx": 0, "vy": 100, "vz": 0}
        elif name == "GPS_RAW_INT":
            fields = {"alt": 239330, "alt_ellipsoid": 205650, "cog": 9000,
                      "eph": 145, "epv": 253,
                      "fix_type": {"type": "GPS_FIX_TYPE_3D_FIX"},
                      "h_acc": 5937, "hdg_acc": 0, "lat": lat_e7, "lon": lng_e7,
                      "satellites_visible": 12, "time_usec": self._uptime_ms() * 1000,
                      "v_acc": 11002, "vel": round(SPEED_M_S * 100),
                      "vel_acc": 389, "yaw": 0}
        elif name == "HEARTBEAT":
            fields = {"autopilot": {"type": "MAV_AUTOPILOT_ARDUPILOTMEGA"},
                      "base_mode": {"bits": 65}, "custom_mode": 0,
                      "mavlink_version": 3,
                      "mavtype": {"type": "MAV_TYPE_SURFACE_BOAT"},
                      "system_status": {"type": "MAV_STATE_ACTIVE"}}
        elif name == "VFR_HUD":
            fields = {"airspeed": SPEED_M_S, "alt": 253.25, "climb": 0.0,
                      "groundspeed": SPEED_M_S, "heading": 90, "throttle": 30}
        else:
            return None
        fields["type"] = name
        return {"message": fields, "status": {"time": {
            "counter": self._uptime_ms() // 1000,
            "first_update": self._first_update,
            "frequency": 1.0,
            "last_update": _iso(self._wall()),
        }}}

    def _uptime_ms(self) -> int:
        return int((self._now() - self._started) * 1000)


def serve_autopilot(autopilot: Autopilot) -> ThreadingHTTPServer:
    """Answer on a free loopback port, as MAVLink2Rest does on a boat's."""
    prefix = mavlink2rest.MESSAGE_PATH + "/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            name = self.path[len(prefix):] if self.path.startswith(prefix) else ""
            body = autopilot.message(name) if name else None
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_response(404 if body is None else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="autopilot").start()
    return server


# --- the Pico ------------------------------------------------------------------


class Water:
    """Readings that drift the way a calm pond's do, inside every gate
    caddis-api applies, so they are drawn rather than flagged as faults."""

    def __init__(self, seed: "int | None" = None) -> None:
        self._random = random.Random(seed)
        self.ph = 7.3
        self.conductivity = 430.0
        self.salinity = 0.21
        self.temperature = 18.0

    def step(self) -> None:
        self.ph = self._drift(self.ph, 0.02, 6.9, 7.8)
        self.conductivity = self._drift(self.conductivity, 2.0, 380.0, 480.0)
        self.salinity = self._drift(self.salinity, 0.002, 0.18, 0.24)
        self.temperature = self._drift(self.temperature, 0.05, 16.0, 20.0)

    def _drift(self, value: float, step: float, low: float, high: float) -> float:
        return min(high, max(low, value + self._random.uniform(-step, step)))


def reading_line(uptime_ms: int, epoch_ms: "int | None", water: Water) -> bytes:
    """One record as the firmware's record_json_reading() writes it, in its
    field order and number formats (tests/data/pico_reading.json), plus the
    ms_since_boot every 2.1.2 record carries."""
    seconds = uptime_ms // 1000
    clock_text = f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    tds = water.conductivity / 2
    return (
        '{"type":"reading"'
        f',"firmware_version":"{FIRMWARE_VERSION}"'
        f',"time":"{clock_text}","ms_since_boot":{uptime_ms}'
        f',"epoch_ms":{"null" if epoch_ms is None else epoch_ms}'
        ',"sd_ready":1,"sd_writes_failed":0,"sd_mounts_failed":0'
        ',"cond_fault":false'
        f',"cond_tds_sal":"{water.conductivity:.0f},{tds:.0f},{water.salinity:.2f}"'
        f',"ph_fault":false,"ph":"{water.ph:.3f}"'
        f',"temp_fault":false,"temp_code":"0x1a2b","temperature":{water.temperature:.3f}'
        ',"uv_fault":false,"uv_present":true,"uv_counts":812,"uv_mv":654'
        ',"uv_index":5,"uv_saturated":false}'
    ).encode("ascii")


class Pico:
    """A pseudo-terminal that boots, asks for the time, and streams readings.

    Its slave end is the port MANTA Link opens. The slave is held open here too,
    so the terminal outlives MANTA Link closing it to reset a silent link.
    """

    def __init__(
        self, cadence_s: float = CADENCE_S, water: "Water | None" = None
    ) -> None:
        # Unix only, like a pseudo-terminal itself. Imported here so the rest of
        # this file, and its tests, still load on the Windows box this package
        # is developed on.
        import tty

        self._master, self._slave = os.openpty()
        # Raw, so nothing written to the master is echoed back as if MANTA Link
        # had sent it, before MANTA Link's own open sets the line discipline.
        tty.setraw(self._slave)
        self.port = os.ttyname(self._slave)
        self._cadence_s = cadence_s
        self._water = water or Water()
        self._pending = b""
        self.stopped = threading.Event()
        self.answered = threading.Event()

    def run_forever(self) -> None:
        booted = time.monotonic()
        self._send(BANNER.encode("ascii"))
        epoch_at_boot_ms = self._ask_for_time(booted)
        while not self.stopped.wait(self._cadence_s):
            uptime_ms = int((time.monotonic() - booted) * 1000)
            epoch_ms = (
                None if epoch_at_boot_ms is None else epoch_at_boot_ms + uptime_ms
            )
            self._water.step()
            self._send(reading_line(uptime_ms, epoch_ms, self._water))

    def _ask_for_time(self, booted: float) -> "int | None":
        """The epoch at boot, from MANTA Link's answer, or None when it stays
        silent, in which case every record carries a null epoch_ms, as the
        firmware's own would."""
        while time.monotonic() - booted < TIME_WINDOW_S and not self.stopped.is_set():
            self._send(b"TIME?")
            replied = self._reply_within(TIME_ASK_INTERVAL_S)
            if replied is not None:
                self.answered.set()
                return replied - int((time.monotonic() - booted) * 1000)
        return None

    def _reply_within(self, timeout_s: float) -> "int | None":
        deadline = time.monotonic() + timeout_s
        while (left := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([self._master], [], [], left)
            if not ready:
                return None
            self._pending += os.read(self._master, 1024)
            *lines, self._pending = self._pending.split(b"\n")
            for line in lines:
                match = _TIME_REPLY_RE.match(line.strip())
                if match:
                    return int(match.group(1))
        return None

    def _send(self, line: bytes) -> None:
        os.write(self._master, line + b"\r\n")

    def close(self) -> None:
        self.stopped.set()
        os.close(self._master)
        os.close(self._slave)


# --- running it ----------------------------------------------------------------


def _cadence(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and value >= MIN_CADENCE_S):
        raise argparse.ArgumentTypeError(
            f"must be at least {MIN_CADENCE_S:g} s, not {text}"
        )
    return value


def parse_args(argv: "list[str] | None") -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fake_boat",
        description="Run MANTA Link against a fake Pico and autopilot working a pond.",
        epilog=f"The token is {config.TOKEN_ENV}, from the environment or "
               f"${entry.VOLUME_ENV}/{config.ENV_FILENAME}, as on a boat.",
    )
    parser.add_argument("pond", type=Path, help="the pond's boundary, as GeoJSON")
    parser.add_argument(
        "--api-url", default=os.environ.get(config.API_URL_ENV),
        help=f"the caddis-api to post to; default ${config.API_URL_ENV}",
    )
    parser.add_argument(
        "--cadence", type=_cadence, default=CADENCE_S,
        help=f"seconds between readings, default {CADENCE_S:g}, "
             f"at least {MIN_CADENCE_S:g}",
    )
    return parser.parse_args(argv)


def main(argv: "list[str] | None" = None) -> int:
    args = parse_args(argv)
    if not args.api_url:
        print(f"fake boat: name the API with --api-url or {config.API_URL_ENV}; "
              "MANTA Link's own default is production", file=sys.stderr)
        return 2
    if is_production(args.api_url):
        print(f"fake boat: {urlsplit(args.api_url).hostname} is production; "
              "fake readings never go there", file=sys.stderr)
        return 2
    try:
        path = survey_lanes(pond_ring(json.loads(args.pond.read_text())))
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"fake boat: cannot work a pond from {args.pond}: {exc}", file=sys.stderr)
        return 2

    os.environ[config.API_URL_ENV] = args.api_url
    confine_uploads(args.api_url)
    server = serve_autopilot(Autopilot(path))
    os.environ[entry.MAVLINK2REST_URL_ENV] = f"http://127.0.0.1:{server.server_port}"
    pico = Pico(cadence_s=args.cadence)
    threading.Thread(target=pico.run_forever, daemon=True, name="pico").start()

    # Rebound on the modules that read them, as bench_host does: reader.py took
    # find_pico_port by name, and no USB device behind a pseudo-terminal can
    # carry the Pico's VID.
    reader.find_pico_port = lambda: pico.port
    clock.clock_is_trustworthy = bench_host.trust_this_bench_clock
    print(f"fake boat: {len(path)} waypoints across {args.pond.name}, a reading every "
          f"{args.cadence:g} s, uploading to {args.api_url} only", file=sys.stderr)
    return entry.main([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
