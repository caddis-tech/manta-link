"""What the poller will and will not call a position.

Every gate here exists because getting it wrong files a water sample against the
wrong water, against an API that is create-only and cannot be corrected. The
expensive failures are all quiet ones: a stale coordinate published as fresh, a
2D fix promoted, an EKF origin of exactly (0, 0) resolving to whatever polygon
happens to contain Null Island.
"""

import json

import pytest

from manta_link import gps
from manta_link.gps import Change, Freshness, GpsPoller, PositionCache, Tags
from manta_link.health import Counters
from manta_link.mavlink2rest import Outcome

from .fakes import FakeMavlink2Rest
from .golden import mavlink

POSITION = gps.POSITION_MESSAGE
QUALITY = gps.QUALITY_MESSAGE
HEARTBEAT = gps.HEARTBEAT_MESSAGE
SPEED = gps.SPEED_MESSAGE

# The bench Navigator's own numbers, so a failure reads as a place.
LATITUDE = 39.9350992
LONGITUDE = -82.9968149


def body(name: str) -> dict:
    """The message half of a captured MAVLink2Rest response."""
    return json.loads(mavlink(name))["message"]


@pytest.fixture
def counters():
    return Counters()


@pytest.fixture
def link():
    service = FakeMavlink2Rest()
    service.observe(POSITION, body("global_position_int"))
    service.observe(QUALITY, body("gps_raw_int_3d"))
    service.observe(HEARTBEAT, body("heartbeat"))
    service.observe(SPEED, body("vfr_hud"))
    return service


@pytest.fixture
def poller(link, counters):
    # Tags every pass, so a test about position does not have to step past the
    # tag interval to see one. The interval has its own tests below, which build
    # their own poller with the shipping value.
    return GpsPoller(link, PositionCache(), counters, tag_interval_s=0.0)


def settle(poller, link, now: float = 100.0):
    """Seed every counter, then take one live pass. Returns that position."""
    poller.poll_once(now)
    for name in (POSITION, QUALITY, HEARTBEAT, SPEED):
        link.advance(name)
    return poller.poll_once(now + 1.0)


class TestSeedThenTrust:
    def test_the_first_counter_ever_seen_is_not_evidence_of_a_fresh_fix(
        self, poller, link, counters
    ):
        """The restart trap, and the single most important test here.

        An autopilot switched off two hours ago still has mavlink2rest serving
        its last message with a 200. Without the seed, the first pass after any
        restart publishes that two-hour-old coordinate with fix_at set to now,
        and does it again on every restart, forever.
        """
        first = poller.poll_once(100.0)

        assert first.fix_at is None
        assert first.latitude is None
        assert first.longitude is None
        assert counters.get("gps_fixes") == 0

    def test_a_position_is_accepted_once_the_counter_has_moved(self, poller, link):
        position = settle(poller, link)

        assert position.fix_at == 101.0
        assert position.latitude == pytest.approx(LATITUDE)
        assert position.longitude == pytest.approx(LONGITUDE)
        assert position.fix_type == "GPS_FIX_TYPE_3D_FIX"
        assert position.satellites == 12

    def test_a_healthy_start_leaves_every_fault_counter_at_zero(
        self, poller, link, counters
    ):
        """A counter that reads 1 from the moment it starts is one nobody looks
        at when it reads 400."""
        settle(poller, link)

        tallies = counters.snapshot()
        for name in (
            "gps_stream_stalled",
            "gps_tags_stale",
            "gps_poll_failures",
            "gps_no_fix",
            "gps_null_island_rejected",
        ):
            assert tallies.get(name, 0) == 0, name

    def test_a_counter_that_resets_to_zero_is_a_new_message(self, poller, link):
        """mavlink2rest zeroes its counters on restart.

        Measured on the bench rig across two runs an hour apart. Under a
        greater-than test the position would freeze until the count climbed back
        past where it was, which on a 1 Hz stream is half an hour of nothing.
        """
        settle(poller, link)
        link.observe(POSITION, body("global_position_int"), counter=0)
        link.observe(QUALITY, body("gps_raw_int_3d"), counter=0)

        position = poller.poll_once(102.0)

        assert position.fix_at == 102.0

    def test_a_message_with_no_counter_is_never_treated_as_fresh(
        self, poller, link, counters
    ):
        link.observe(POSITION, body("global_position_int"), counter=None)
        link.observe(QUALITY, body("gps_raw_int_3d"), counter=None)

        poller.poll_once(100.0)
        position = poller.poll_once(101.0)

        assert position.fix_at is None


class TestAStalledStream:
    def test_a_frozen_counter_stops_refreshing_the_fix(self, poller, link):
        """An advancing counter proves the link is alive, not that the position
        is real. A frozen one is the only signal that separates them."""
        settled = settle(poller, link)

        later = poller.poll_once(160.0)

        assert later.fix_at == settled.fix_at
        assert later.published_at == 160.0

    def test_a_frozen_counter_is_counted(self, poller, link, counters):
        settle(poller, link)

        poller.poll_once(102.0)

        assert counters.get("gps_stream_stalled") == 1

    def test_the_seed_pass_is_not_counted_as_a_stall(self, poller, counters):
        poller.poll_once(100.0)

        assert counters.get("gps_stream_stalled") == 0


class TestTheFixGate:
    @pytest.mark.parametrize("name", sorted(gps.GPS_FIX_TYPES_USABLE))
    def test_a_fix_good_enough_to_file_a_sample_against_is_promoted(
        self, poller, link, name
    ):
        quality = body("gps_raw_int_3d") | {"fix_type": {"type": name}}
        link.observe(QUALITY, quality)

        position = settle(poller, link)

        assert position.fix_type == name
        assert position.latitude is not None

    def test_a_two_d_fix_is_refused_but_not_thrown_away(
        self, poller, link, counters
    ):
        """Refusing to file a 2D fix against a waterway is defensible.

        Throwing it away is not: caddis-api's backfill can only re-resolve a
        coordinate that was written down, and the ray-cast containment it uses
        has no low-confidence channel to hand a doubtful one to.
        """
        quality = body("gps_raw_int_3d") | {
            "fix_type": {"type": "GPS_FIX_TYPE_2D_FIX"}
        }
        link.observe(QUALITY, quality)

        position = settle(poller, link)

        assert position.latitude is None
        assert position.fix_at is None
        assert position.refused_latitude == pytest.approx(LATITUDE)
        assert position.refused_fix_type == "GPS_FIX_TYPE_2D_FIX"
        assert counters.get("gps_no_fix") == 1

    def test_an_unrecognised_fix_type_is_refused_and_named(
        self, poller, link, counters, caplog
    ):
        quality = body("gps_raw_int_3d") | {
            "fix_type": {"type": "GPS_FIX_TYPE_SOMETHING_NEW"}
        }
        link.observe(QUALITY, quality)

        with caplog.at_level("WARNING"):
            position = settle(poller, link)

        assert position.latitude is None
        assert counters.get("gps_fix_type_unknown") == 1
        assert "GPS_FIX_TYPE_SOMETHING_NEW" in caplog.text

    def test_an_integer_fix_type_is_refused_rather_than_mapped(self, poller, link):
        # A number-to-name table here is a guess about which enum revision the
        # sender used, and the guess is invisible when it is wrong.
        link.observe(QUALITY, body("gps_raw_int_3d") | {"fix_type": 3})

        assert settle(poller, link).latitude is None

    def test_a_no_fix_reading_is_refused(self, poller, link, counters):
        link.observe(QUALITY, body("gps_raw_int_no_fix"))
        link.observe(POSITION, body("global_position_int_null_island"))

        position = settle(poller, link)

        assert position.latitude is None
        assert position.fix_at is None

    def test_a_position_with_no_recent_quality_beside_it_is_refused(
        self, poller, link, counters
    ):
        """Position and quality are two messages with two counters.

        A position accepted on the strength of a fix reading from a minute ago
        is a position accepted on no evidence at all.
        """
        settle(poller, link)
        link.fail(QUALITY, Outcome.UNREACHABLE)
        link.advance(POSITION)

        position = poller.poll_once(200.0)

        assert position.fix_at == 101.0
        assert counters.get("gps_fix_refused_quality") == 1


class TestCoordinatesThatAreNotPlaces:
    def test_null_island_is_refused_and_not_even_kept(self, poller, link, counters):
        """ArduPilot publishes lat=lon=0 until the EKF has an origin.

        The fix gate does not make this redundant: the gate reads GPS_RAW_INT
        and the coordinates come from GLOBAL_POSITION_INT, so on the transition
        into a fix the first can already say 3D while the second still carries
        the pre-fix origin. caddis-api ray-casts (0, 0) like any other point.
        """
        link.observe(POSITION, body("global_position_int_null_island"))

        position = settle(poller, link)

        assert position.latitude is None
        assert position.refused_latitude is None
        assert counters.get("gps_null_island_rejected") == 1

    @pytest.mark.parametrize(
        "lat,lon",
        [(910_000_000, 0), (0, 1_810_000_000), (-910_000_000, 0), (0, -1_810_000_000)],
    )
    def test_a_coordinate_off_the_planet_is_refused(
        self, poller, link, counters, lat, lon
    ):
        link.observe(POSITION, body("global_position_int") | {"lat": lat, "lon": lon})

        position = settle(poller, link)

        assert position.latitude is None
        assert counters.get("gps_out_of_range") == 1

    @pytest.mark.parametrize("field", ["lat", "lon"])
    def test_a_missing_coordinate_is_refused(self, poller, link, field):
        broken = body("global_position_int")
        del broken[field]
        link.observe(POSITION, broken)

        assert settle(poller, link).latitude is None

    def test_a_real_coordinate_near_zero_is_not_null_island(self):
        # One ten-millionth of a degree from the origin is a real place, and the
        # guard has to be exact equality rather than a tolerance.
        assert not gps.is_null_island(0.0000001, 0.0)
        assert gps.is_null_island(0.0, 0.0)


class TestPublishing:
    def test_every_pass_publishes_even_when_every_poll_failed(self, poller, link):
        """A pass that skipped the publish would leave the last snapshot in the
        cache with its published_at frozen, and nothing downstream could tell a
        stopped poller from a quiet one."""
        settle(poller, link)
        for name in (POSITION, QUALITY, HEARTBEAT, SPEED):
            link.fail(name, Outcome.UNREACHABLE)

        position = poller.poll_once(200.0)

        assert position.published_at == 200.0
        assert position.fix_at == 101.0

    def test_the_cache_holds_the_last_published_snapshot(self, link, counters):
        cache = PositionCache()
        poller = GpsPoller(link, cache, counters)

        assert cache.latest is None
        published = settle(poller, link)
        assert cache.latest is published

    def test_a_published_snapshot_is_replaced_rather_than_edited(self, poller, link):
        first = settle(poller, link)
        link.advance(POSITION)
        link.advance(QUALITY)

        second = poller.poll_once(102.0)

        assert second is not first
        assert first.published_at == 101.0

    @pytest.mark.parametrize(
        "outcome,counter",
        [
            (Outcome.UNREACHABLE, "gps_poll_failures"),
            (Outcome.MALFORMED, "gps_body_unreadable"),
            (Outcome.OVERSIZE, "gps_body_oversize"),
            (Outcome.ABSENT, "gps_message_absent"),
        ],
    )
    def test_each_way_a_poll_can_fail_has_its_own_tally(
        self, poller, link, counters, outcome, counter
    ):
        # Separate names because "the service is not there" and "the service
        # said something unreadable" send an operator to different places.
        link.fail(POSITION, outcome)

        poller.poll_once(100.0)

        assert counters.get(counter) >= 1


class TestVehicleTags:
    def test_the_tags_are_read_from_the_heartbeat_and_the_hud(self, poller, link):
        position = settle(poller, link)

        assert position.tags.vehicle_type == "MAV_TYPE_SURFACE_BOAT"
        assert position.tags.mode == 0
        assert position.tags.groundspeed == pytest.approx(0.0439258)
        assert position.tags_at == 101.0

    def test_armed_is_read_from_the_safety_bit(self):
        # The rig reports base_mode 65, which is custom-mode-enabled plus
        # manual-input-enabled and not armed. Getting this backwards would
        # label a boat on a trailer as under way.
        assert gps.read_tags({"base_mode": {"bits": 65}}, None).armed is False
        assert gps.read_tags({"base_mode": {"bits": 193}}, None).armed is True

    def test_an_unreadable_base_mode_is_no_answer_rather_than_not_armed(self):
        assert gps.read_tags({"base_mode": "65"}, None).armed is None
        assert gps.read_tags({}, None).armed is None

    def test_a_dead_autopilots_tags_are_dropped_rather_than_republished(
        self, poller, link, counters
    ):
        """armed is the one tag with operational meaning.

        Republishing a stopped autopilot's last `armed: true` every pass until
        the container restarts would put "under way" on every reading taken on a
        trailer.
        """
        assert settle(poller, link).tags.vehicle_type == "MAV_TYPE_SURFACE_BOAT"

        # Position keeps moving, the heartbeat does not.
        for step in range(2, 20):
            link.advance(POSITION)
            link.advance(QUALITY)
            position = poller.poll_once(100.0 + step)

        assert position.tags == Tags()
        assert position.tags_at is None
        assert counters.get("gps_tags_stale") >= 1

    def test_the_first_pass_tries_the_tags_rather_than_waiting_out_the_interval(
        self, link, counters
    ):
        # _tags_due starts at negative infinity for this reason. Starting it at
        # zero makes the behaviour depend on whichever monotonic the process, or
        # a test, happened to begin at.
        paced = GpsPoller(link, PositionCache(), counters)

        paced.poll_once(100.0)

        assert HEARTBEAT in link.asked

    def test_the_tags_are_not_fetched_on_every_pass(self, link, counters):
        paced = GpsPoller(link, PositionCache(), counters)
        paced.poll_once(100.0)
        asked_so_far = link.asked.count(HEARTBEAT)

        paced.poll_once(101.0)

        assert link.asked.count(HEARTBEAT) == asked_so_far


class TestFreshness:
    def test_the_first_look_is_a_seed(self):
        assert Freshness().look(7) is Change.SEEDED

    def test_a_changed_counter_advanced(self):
        watcher = Freshness()
        watcher.look(7)
        assert watcher.look(8) is Change.ADVANCED

    def test_the_same_counter_twice_is_unchanged(self):
        watcher = Freshness()
        watcher.look(7)
        assert watcher.look(7) is Change.UNCHANGED

    def test_a_counter_going_backwards_still_advanced(self):
        watcher = Freshness()
        watcher.look(2156)
        assert watcher.look(0) is Change.ADVANCED

    def test_no_counter_establishes_nothing(self):
        watcher = Freshness()
        assert watcher.look(None) is Change.UNKNOWN
        # And it does not consume the seed, so a body that starts carrying one
        # later is still seeded rather than trusted.
        assert watcher.look(7) is Change.SEEDED


class TestTheHdopReading:
    def test_eph_is_read_as_hundredths(self):
        assert gps.read_hdop({"eph": 145}) == 1.45

    def test_the_unknown_sentinel_is_not_a_dilution_of_six_hundred(self):
        # UINT16_MAX means "no idea", and 655.35 is not a reading.
        assert gps.read_hdop({"eph": gps.EPH_UNKNOWN}) is None

    def test_an_absent_eph_is_no_answer(self):
        assert gps.read_hdop({}) is None


def test_the_envelope_form_is_json_safe(poller, link):
    """The spool and the archive serialise this, so a dataclass will not do."""
    position = settle(poller, link)

    round_tripped = json.loads(json.dumps(position.as_envelope()))

    assert round_tripped["latitude"] == pytest.approx(LATITUDE)
    assert round_tripped["fix_type"] == "GPS_FIX_TYPE_3D_FIX"
    assert round_tripped["mav_vehicle_type"] == "MAV_TYPE_SURFACE_BOAT"


def test_the_poller_sleeps_to_a_deadline_rather_than_by_an_interval(
    poller, link, monkeypatch
):
    """A slow pass must not push every later pass late."""
    slept: list[float] = []
    # Start, the moment the pass begins, and the moment it ends. The fourth read
    # ends the loop, which is how a run_forever gets tested at all.
    ticks = iter([0.0, 0.75, 0.75])

    monkeypatch.setattr(gps.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(gps.time, "sleep", slept.append)
    monkeypatch.setattr(GpsPoller, "poll_once", lambda self, now: None)

    with pytest.raises(StopIteration):
        poller.run_forever()

    # The pass took 0.75s of the 1.0s interval, so the wait is what is left of
    # it. Sleeping a flat interval here would make every later pass late too.
    assert slept == [pytest.approx(0.25)]
