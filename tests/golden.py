"""The Pico lines every test measures against.

One copy, in a file, rather than a literal repeated per suite. The mapping tests
assert an exact payload against it, so a firmware change should move this file
and break those tests loudly, which is the only way a silent read-layer
mismatch ever becomes visible.

Both lines came out of the firmware's own record_json_reading()
(AquadronePicoFirmware quentin, cda532a), not off a boat and not typed here. The
hand-written literal they replace disagreed with that writer in four places at
once: an uppercase hex temp_code, a fractional uv_index, an integer
uv_saturated and a one-decimal temperature are none of them shapes the firmware
can emit, and no test in the suite could see the difference.
"""

from pathlib import Path

DATA = Path(__file__).parent / "data"

# Exactly as it comes off the wire, minus the terminator the framing strips.
READING = (DATA / "pico_reading.json").read_bytes().strip()

# The same cycle with the UV reading off the top of the vendor ladder. It is the
# only shape in the record where a mapped field is a JSON boolean and another is
# an explicit null, which is what makes it worth keeping a second file for.
SATURATED_READING = (DATA / "pico_reading_uv_saturated.json").read_bytes().strip()
