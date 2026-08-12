"""The captured reference data every test measures against.

Two sources, kept in one place: the Pico lines below, and the MAVLink2Rest
bodies at the bottom.

One copy, in a file, rather than a literal repeated per suite. The mapping tests
assert an exact payload against it, so a firmware change should move this file
and break those tests loudly, which is the only way a silent read-layer
mismatch ever becomes visible.

Both lines came out of the firmware's own record_json_reading()
(AquadronePicoFirmware ecdd3dc, branch fix/d1-both-sinks), not off a boat and
not typed here. The hand-written literal they replace disagreed with that writer
in four places at once: an uppercase hex temp_code, a fractional uv_index, an
integer uv_saturated and a one-decimal temperature are none of them shapes the
firmware can emit, and no test in the suite could see the difference.

firmware_version's value is whatever a given build stamped in, so nothing should
assert on it beyond its shape. That every record carries one is the part that
matters: it, and uv_present, predate every firmware that can feed this process.
"""

from pathlib import Path

DATA = Path(__file__).parent / "data"

# Exactly as it comes off the wire, minus the terminator the framing strips.
READING = (DATA / "pico_reading.json").read_bytes().strip()

# The same cycle with the UV reading off the top of the vendor ladder. It is the
# only shape in the record where a mapped field is a JSON boolean and another is
# an explicit null, which is what makes it worth keeping a second file for.
SATURATED_READING = (DATA / "pico_reading_uv_saturated.json").read_bytes().strip()


# --- MAVLink2Rest ------------------------------------------------------------
#
# The response shape is the one thing in this project not usefully guessed at,
# so which of these came off a real autopilot and which were written by hand is
# recorded per file rather than left to be assumed.
#
# Captured from the bench rig's Navigator on 2026-08-11, verbatim including the
# status block mavlink2rest wraps every message in:
#
#   mavlink_global_position_int.json   a real 3D fix, bare integer coordinates
#   mavlink_gps_raw_int_3d.json        the same instant: 3D fix, 12 satellites
#   mavlink_heartbeat.json             ArduRover, MAV_TYPE_SURFACE_BOAT
#   mavlink_vfr_hud.json               groundspeed, and nothing else we read
#
# Written by hand, because the rig had a good fix throughout and none of these
# could be captured without breaking it:
#
#   mavlink_gps_raw_int_no_fix.json    GPS_FIX_TYPE_NO_FIX, 0 satellites, eph
#                                      65535, which is how a cold start reads
#   mavlink_global_position_int_null_island.json
#                                      lat and lon exactly 0, which ArduPilot
#                                      publishes until the EKF has an origin
#   mavlink_*_wrapped.json             the older {"type", "value"} scalar
#                                      wrapping, kept because a mavlink2rest
#                                      that does it is still in the fleet
#
# The hand-written four are modelled on the captured four and carry the same key
# set. That is the part worth distrusting: they prove the parsing, not what an
# autopilot does over time.


def mavlink(name: str) -> bytes:
    """One captured MAVLink2Rest response body, exactly as it came off the wire."""
    return (DATA / f"mavlink_{name}.json").read_bytes()
