"""The captured Pico line every test measures against.

One copy, in a file, rather than a literal repeated per suite. The mapping tests
assert an exact payload against it, so a firmware change should move this file
and break those tests loudly, which is the only way a silent read-layer
mismatch ever becomes visible.
"""

from pathlib import Path

DATA = Path(__file__).parent / "data"

# Exactly as it comes off the wire, minus the terminator the framing strips.
READING = (DATA / "pico_reading.json").read_bytes().strip()
