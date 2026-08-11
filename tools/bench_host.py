#!/usr/bin/env python3
"""Run MANTA Link on a bench machine whose clock it would otherwise refuse.

`clock.clock_is_trustworthy` asks the kernel through adjtimex, which exists on
Linux and not on Windows. On a dev box it therefore answers false forever, the
reader never replies to `TIME?`, and every record the Pico writes for the rest of
that run carries `epoch_ms: null`. That is exactly right in production and
useless on a bench.

This replaces that one answer and nothing else. It keeps the half of the guard
that is not platform specific, the plausible-epoch range, because a bench box
with a wrong clock can still hand the Pico a wrong year, and it says what it is
doing every time it is asked rather than once at startup.

**The trap this exists to shout about.** The Pico asks only during its boot
window and stops permanently on the first accepted answer, then self-stamps for
the rest of that run. So a run started WITHOUT this, against a Pico that some
earlier bench run already answered, looks completely healthy: records carry real
timestamps, nothing warns, and the guard is fully in force and simply never
consulted. Only a physical power cycle of the Pico resets it. If you are trying
to prove the `TIME?` path works, power-cycle the Pico first or you are proving
nothing.

Not importable by the package, and excluded from the image by .dockerignore.
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manta_link import __main__ as entry  # noqa: E402
from manta_link import clock  # noqa: E402
from manta_link.logging_setup import Throttle  # noqa: E402

log = logging.getLogger("manta_link.bench")

# Short, because this is the one line that says the process is not behaving the
# way it will on a boat, and a bench run is watched rather than left alone.
DISCLAIMER_INTERVAL_S = 60.0

_disclaimer = Throttle(DISCLAIMER_INTERVAL_S)


def trust_this_bench_clock() -> bool:
    """Answer TIME? from this machine's clock, whatever the kernel thinks.

    Logged on every consultation rather than once at startup. A single line at
    startup scrolls away, and the question a reader has later is not "was this
    started in bench mode" but "was the answer the Pico took a real one".
    """
    now = time.time()
    if not clock.MIN_PLAUSIBLE_EPOCH_S <= now < clock.MAX_PLAUSIBLE_EPOCH_S:
        # The Pico enforces the same range and would reject the reply with no
        # diagnostic on either side, costing the run its timestamps for a reason
        # nobody can see.
        log.error("bench mode: this machine's clock reads %.0f, outside the "
                  "range the firmware will accept; staying silent", now)
        return False

    if _disclaimer.should_emit():
        log.warning("bench mode: the kernel sync check is bypassed, so TIME? is "
                    "being answered from a clock nothing has vouched for; %d "
                    "more suppressed since the last of these",
                    _disclaimer.take_suppressed())
    return True


def main(argv: "list[str] | None" = None) -> int:
    # Rebound on the module rather than passed in, because reader.py and
    # record.py both reach it as clock.clock_is_trustworthy and neither should
    # grow a seam that exists only for a bench.
    clock.clock_is_trustworthy = trust_this_bench_clock
    return entry.main([] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
