"""The fake boat feeds MANTA Link what a boat would, and none of it reaches production.

A development tool, tested for the two ways it could mislead. A reading or a
position MANTA Link reads differently from a real one would make a run against
staging prove less than it seems to. An upload that escapes to production would
file a made-up reading against a real pond as if a drone had measured it.
"""

import json
import math
import os
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manta_link import clock, config, framing, gps, mavlink2rest, reader, record  # noqa: E402
from manta_link.reader import SerialReader  # noqa: E402
from tools import fake_boat  # noqa: E402

from .golden import READING  # noqa: E402

STAGING = "https://squadron-staging-928515830511.us-central1.run.app"

# About 85 m by 55 m, beside the bench rig's own captured fix, as (lat, lng).
POND = [
    (39.9350, -82.9975), (39.9350, -82.9965), (39.9355, -82.9965), (39.9355, -82.9975)
]


def _download(ring):
    """The pond as caddis-api's Download GeoJSON writes it: lng first, closed."""
    coordinates = [[lng, lat] for lat, lng in ring] + [[ring[0][1], ring[0][0]]]
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": "Bench pond"},
        "geometry": {"type": "Polygon", "coordinates": [coordinates]},
    }]}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def confined(monkeypatch):
    """confine_uploads rebinds module state; put it back after each test."""
    monkeypatch.setattr(config, "DEFAULT_API_URL", config.DEFAULT_API_URL)
    monkeypatch.setattr(config.Config, "batch_endpoint", config.Config.batch_endpoint)
    monkeypatch.delenv(config.API_URL_ENV, raising=False)


# --- the pond ------------------------------------------------------------------


def test_the_pond_pages_download_is_read_as_lat_lng():
    ring = fake_boat.pond_ring(_download(POND))

    assert ring[:-1] == POND
    assert ring[-1] == POND[0]


@pytest.mark.parametrize("geojson", [
    [[-82.9975, 39.9350], [-82.9965, 39.9350], [-82.9965, 39.9355]],
    7,
    {"type": "Feature", "geometry": [1, 2]},
    {"type": "MultiPolygon", "coordinates": []},
])
def test_anything_but_a_polygon_is_refused_with_what_it_was(geojson):
    with pytest.raises(ValueError, match="expected a GeoJSON Polygon"):
        fake_boat.pond_ring(geojson)


def test_every_waypoint_is_inside_the_pond_and_the_lanes_cover_it():
    """caddis-api files a reading under a pond only when it is inside the ring."""
    path = fake_boat.survey_lanes(POND)

    assert all(fake_boat.inside(point, POND) for point in path)
    assert len({lat for lat, _ in path}) >= 5


def test_a_concave_pond_keeps_the_walk_off_the_land_in_its_bay():
    """An L-shaped pond: the lanes stop where the water does."""
    ell = [(39.9350, -82.9975), (39.9350, -82.9965), (39.9352, -82.9965),
           (39.9352, -82.9970), (39.9355, -82.9970), (39.9355, -82.9975)]

    path = fake_boat.survey_lanes(ell)

    assert path
    assert all(fake_boat.inside(point, ell) for point in path)
    assert not any(lat > 39.9352 and lng > -82.9970 for lat, lng in path)


def test_a_large_finely_traced_lake_is_laned_without_testing_every_point():
    """A 2 km lake traced with 500 vertices: each lane meets the ring once, so
    this is lanes times edges, not lanes times metres times edges."""
    lake = [
        (39.93 + 0.0045 * math.sin(2 * math.pi * i / 500),
         -82.99 + 0.0117 * math.cos(2 * math.pi * i / 500))
        for i in range(500)
    ]

    started = time.monotonic()
    path = fake_boat.survey_lanes(lake)

    assert time.monotonic() - started < 5
    assert len(path) > 100_000
    assert all(fake_boat.inside(point, lake) for point in path[::997])


def _metres(a, b):
    degree = fake_boat.METRES_PER_DEGREE_LAT
    north = (a[0] - b[0]) * degree
    east = (a[1] - b[1]) * degree * math.cos(math.radians(a[0]))
    return math.hypot(north, east)


def test_each_pass_lies_between_the_last_and_starts_where_it_ended():
    """A pass that retraces the last lands every new reading on an old ring, so
    a viewer cannot see them arrive, and a wrap to the first lane is a jump
    across the pond that caddis-api scores as travel."""
    walk = fake_boat.Walk(POND)
    first = [walk.at(i) for i in range(walk.first_pass)]
    later = [walk.at(walk.first_pass + i) for i in range(3 * walk.first_pass)]

    assert not set(first) & set(later)
    assert all(fake_boat.inside(point, POND) for point in later)
    track = first + later
    steps = [_metres(a, b) for a, b in zip(track, track[1:], strict=False)]
    assert max(steps) < 2 * fake_boat.LANE_SPACING_M


def test_a_pond_too_small_for_a_lane_is_refused_with_the_reason():
    speck = [(39.93500, -82.99700), (39.93500, -82.99699), (39.93501, -82.99699)]

    with pytest.raises(ValueError, match="no lane crosses the pond"):
        fake_boat.survey_lanes(speck)


# --- the Pico ------------------------------------------------------------------


def _fake_reading():
    return fake_boat.reading_line(72_000, 1_754_400_072_000, fake_boat.Water(seed=1))


def test_a_fake_reading_carries_the_golden_readings_fields_and_no_others():
    """The golden line is the firmware's own writer; a key it lacks is a shape
    MANTA Link would log as unknown on a boat."""
    fake = json.loads(_fake_reading())

    assert set(fake) == set(json.loads(READING)) | {"ms_since_boot"}
    assert record.unknown_keys(fake) == []
    assert framing.classify(_fake_reading()) is framing.Kind.RECORD


def test_every_measurement_on_a_fake_reading_maps_to_a_number():
    payload = record.to_payload(json.loads(_fake_reading()))

    for key in ("conductivity", "tds", "salinity", "ph", "water_temperature"):
        assert isinstance(payload[key], int | float), key
    assert "ph_raw" not in payload


def test_the_water_drifts_inside_what_caddis_api_draws():
    water = fake_boat.Water(seed=7)

    for _ in range(2000):
        water.step()
        assert 6.9 <= water.ph <= 7.8
        assert 380 <= water.conductivity <= 480
        assert 16 <= water.temperature <= 20


# --- the autopilot -------------------------------------------------------------


@pytest.fixture
def autopilot():
    now = Clock()
    walk = fake_boat.Walk(POND)
    server = fake_boat.serve_autopilot(fake_boat.Autopilot(walk, now=now))
    link = mavlink2rest.Mavlink2Rest(f"http://127.0.0.1:{server.server_port}")
    yield link, now
    server.shutdown()
    server.server_close()


def test_the_autopilot_is_read_as_a_3d_fix_inside_the_pond(autopilot):
    link, _ = autopilot

    position = link.message(gps.POSITION_MESSAGE)
    quality = link.message(gps.QUALITY_MESSAGE)

    assert position.outcome is mavlink2rest.Outcome.OBSERVED
    where = gps.read_coordinates(dict(position.observation.fields))
    assert fake_boat.inside(where, POND)
    assert gps.read_fix_type(dict(quality.observation.fields)).usable
    assert gps.read_hdop(dict(quality.observation.fields)) == 1.45


def test_the_autopilot_keeps_talking_and_the_boat_keeps_moving(autopilot):
    """mavlink2rest serves its last message for ever, so an unchanged counter
    reads as an autopilot that has stopped."""
    link, now = autopilot
    before = link.message(gps.POSITION_MESSAGE).observation

    now.now += 5.0
    after = link.message(gps.POSITION_MESSAGE).observation

    assert after.counter != before.counter
    assert gps.read_coordinates(dict(after.fields)) != gps.read_coordinates(
        dict(before.fields)
    )


def test_a_message_the_autopilot_does_not_send_is_absent(autopilot):
    link, _ = autopilot

    assert link.message("SYS_STATUS").outcome is mavlink2rest.Outcome.ABSENT


# --- never production ----------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://api.caddistech.com",
    "https://API.caddistech.com/",
    "https://squadron.caddistech.com",
    "https://django-backend-172019847409.us-central1.run.app",
])
def test_production_is_refused_by_name(url):
    assert fake_boat.is_production(url)


@pytest.mark.parametrize("url", [
    "https://api.caddistech.com.",
    "https://Squadron.CaddisTech.com./pond_status/",
    "https://canary---django-backend-172019847409.us-central1.run.app",
    "https://django-backend-a1b2c3d4e5-uc.a.run.app",
])
def test_production_is_refused_under_every_other_name_dns_answers_to(url):
    """A trailing dot is the same host to DNS, and Cloud Run serves the one
    service at a revision tag's address and at its older hashed one too."""
    assert fake_boat.is_production(url)


def test_uploads_named_with_a_trailing_dot_are_still_confined(confined):
    fake_boat.confine_uploads(STAGING)

    assert config.Config(api_url=STAGING + ".").batch_endpoint.startswith(STAGING)


@pytest.mark.parametrize("url", [STAGING, "http://127.0.0.1:8000", "http://localhost:8000"])
def test_staging_and_a_bench_api_are_not_production(url):
    assert not fake_boat.is_production(url)


def test_uploads_go_to_the_named_api_and_nowhere_else(confined):
    fake_boat.confine_uploads(STAGING)

    named = config.Config(api_url=STAGING).batch_endpoint
    elsewhere = [
        config.Config(api_url="https://api.caddistech.com").batch_endpoint,
        config.Config(api_url="https://example.org").batch_endpoint,
        config.Config().batch_endpoint,
    ]

    assert named == f"{STAGING}/api/v1/devices/telemetry/batch/"
    assert all(url.startswith(fake_boat.REFUSED_API_URL) for url in elsewhere)


@pytest.mark.parametrize("written", [None, "http://10.0.0.5:8000", "https://user:pw@x.org"])
def test_a_missing_or_refused_url_resolves_to_nowhere_not_production(
    confined, tmp_path, written
):
    """MANTA Link's own answer to both is production, which is right for a boat
    and the one place a fake one must never post."""
    if written is not None:
        (tmp_path / config.ENV_FILENAME).write_text(f"{config.API_URL_ENV}={written}\n")
    fake_boat.confine_uploads(STAGING)
    watcher = config.ConfigWatcher(tmp_path, config.TokenSession())

    watcher.reload()

    assert watcher.config.batch_endpoint.startswith(fake_boat.REFUSED_API_URL)


@pytest.mark.parametrize("argv", [
    ["--api-url", "https://api.caddistech.com"],
    [],
])
def test_production_or_no_api_at_all_stops_it_before_the_port_is_touched(
    confined, monkeypatch, tmp_path, argv
):
    pond = tmp_path / "pond.geojson"
    pond.write_text(json.dumps(_download(POND)))
    untouched = reader.find_pico_port

    assert fake_boat.main([str(pond), *argv]) == 2
    assert reader.find_pico_port is untouched


@pytest.mark.parametrize("cadence", ["0", "-4", "0.004", "nan", "inf"])
def test_a_cadence_that_would_flood_the_api_is_refused(confined, tmp_path, cadence):
    """Nothing in a fake Pico takes time to measure, so a zero here would post
    readings as fast as MANTA Link can upload them."""
    pond = tmp_path / "pond.geojson"
    pond.write_text(json.dumps(_download(POND)))
    untouched = reader.find_pico_port

    with pytest.raises(SystemExit):
        fake_boat.main([str(pond), "--api-url", STAGING, "--cadence", cadence])
    assert reader.find_pico_port is untouched


# --- against the real reader ---------------------------------------------------


@contextmanager
def _reading(pico, monkeypatch, *, answering):
    """MANTA Link's SerialReader serving `pico`'s port, on a thread that ends
    with the test. Its one-port _serve rather than run_forever, which never
    returns and would go on reopening ports for the rest of the session: the
    pseudo-terminal closing under _serve is what ends it."""
    monkeypatch.setattr(clock, "clock_is_trustworthy", lambda: answering)
    records: deque = deque()
    port_owner = SerialReader(records=records)

    def serve():
        try:
            port_owner._serve(pico.port)
        except Exception:
            pass

    owner = threading.Thread(target=serve, daemon=True)
    talking = threading.Thread(target=pico.run_forever, daemon=True)
    owner.start()
    talking.start()
    try:
        yield records
    finally:
        pico.stopped.set()
        talking.join(timeout=5)
        pico.close()
        owner.join(timeout=5)
        assert not owner.is_alive(), "the reader outlived its port"


def _until(ready, seconds=10):
    deadline = time.monotonic() + seconds
    while not ready() and time.monotonic() < deadline:
        time.sleep(0.05)


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="pseudo-terminals are Unix only")
def test_the_real_reader_answers_the_fake_pico_and_takes_its_readings(monkeypatch):
    """The fake end to end on MANTA Link's side of the port: the time asked for
    is answered, and what follows arrives as stamped records."""
    pico = fake_boat.Pico(cadence_s=0.05)

    def stamped():
        epochs = [json.loads(line)["epoch_ms"] for line, _ in list(records)]
        return [epoch for epoch in epochs if epoch is not None]

    with _reading(pico, monkeypatch, answering=True) as records:
        _until(lambda: len(stamped()) >= 2)

    assert pico.answered.is_set()
    assert abs(stamped()[-1] - time.time() * 1000) < 60_000


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="pseudo-terminals are Unix only")
def test_readings_flow_unstamped_while_the_time_goes_unanswered(monkeypatch):
    """The firmware records while it asks. A fake that waited out the whole
    window first would hide the unstamped stretch MANTA Link's anchoring is for."""
    pico = fake_boat.Pico(cadence_s=0.05)

    with _reading(pico, monkeypatch, answering=False) as records:
        _until(lambda: len(records) >= 3, seconds=5)

    assert not pico.answered.is_set()
    assert len(records) >= 3
    assert all(json.loads(line)["epoch_ms"] is None for line, _ in records)
