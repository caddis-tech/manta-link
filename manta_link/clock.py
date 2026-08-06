"""Whether this Pi's clock is worth handing to the Pico, and what time it says.

Lifted from the deployed time_responder.py, which proved on a real boat. Two
changes: the libc handle is resolved once at import instead of per request, and
a failed adjtimex now reports errno instead of failing silently.
"""

import ctypes
import ctypes.util
import logging
import os
import time

log = logging.getLogger(__name__)

# Kernel clock states from adjtimex(2). TIME_ERROR means STA_UNSYNC is set: the
# clock is running free and has not been disciplined. TIME_BAD is an alias for
# the same value, so there is nothing separate to test for.
TIME_ERROR = 5

# Mirrors BOOT_TIME_MIN_EPOCH_MS / BOOT_TIME_MAX_EPOCH_MS in the firmware's
# boot_time.h. Both sides enforce the range and neither relies on the other.
#
# The upper bound matters as much as the lower: a clock set past 2100 passes any
# "is it after 2025" check, and the Pico then rejects the reply with no
# diagnostic on either side, costing the run its timestamps for a reason nobody
# can see. Refusing here at least says why.
MIN_PLAUSIBLE_EPOCH_S = 1735689600  # 2025-01-01T00:00:00Z
MAX_PLAUSIBLE_EPOCH_S = 4102444800  # 2100-01-01T00:00:00Z

# Resolved once. ctypes.util.find_library shells out to ldconfig on Linux, which
# is a fork per call, and the deployed version paid that on every request.
_libc: "ctypes.CDLL | None"
try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
except (OSError, TypeError) as exc:  # pragma: no cover - platform dependent
    log.warning("could not load libc (%s); clock will never be trusted", exc)
    _libc = None


def clock_is_trustworthy() -> bool:
    """True only if this Pi's clock has actually been disciplined by NTP.

    The Pi has no battery-backed RTC. It learns the time from NTP over the
    cellular link, so a Pi that boots without a connection reports a time near
    the epoch, or a stale one carried over from the last shutdown.

    Refusing to answer is the correct behaviour there, and it is why the Pico
    polls rather than asking once: staying silent until this returns true is
    what lets the Pico keep asking until the answer is worth having. A record
    with no timestamp is visibly missing one. A record stamped 1970, or stamped
    with yesterday's date, is wrong in a way that gets used downstream before
    anybody notices.

    Asked of the kernel via adjtimex rather than of systemd via timedatectl,
    because this runs in a container where there is no systemd to ask. The
    kernel's sync flag is set by whichever daemon disciplines the clock, so this
    works the same under timesyncd, chrony or ntpd.
    """
    if _libc is None:
        return False

    try:
        # A zeroed struct timex means modes == 0, which is a read-only query.
        # Oversized on purpose so the exact layout does not have to be declared:
        # only the return value is read.
        buf = ctypes.create_string_buffer(512)
        ctypes.set_errno(0)
        state = _libc.adjtimex(buf)
    except (OSError, AttributeError) as exc:
        log.warning("could not query kernel clock state (%s); refusing", exc)
        return False

    if state < 0:
        err = ctypes.get_errno()
        log.warning("adjtimex failed (errno %d: %s); refusing to answer",
                    err, os.strerror(err) if err else "no errno set")
        return False
    if state == TIME_ERROR:
        return False

    # Belt and braces: a daemon that never clears STA_UNSYNC would pass the
    # check above with a 1970 clock.
    now = time.time()
    if not MIN_PLAUSIBLE_EPOCH_S <= now < MAX_PLAUSIBLE_EPOCH_S:
        log.warning("kernel reports synced but wall clock reads %.0f, outside "
                    "the plausible range; refusing to answer", now)
        return False
    return True


def epoch_ms_now() -> int:
    """Wall clock in milliseconds, in the form the Pico's parser expects."""
    return time.time_ns() // 1_000_000
