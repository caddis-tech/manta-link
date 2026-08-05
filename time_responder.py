#!/usr/bin/env python3
"""Answer the Pico's boot-time request with this Pi's clock.

This is the whole Pi side of the boot-time sync. It sits on the Pico's USB
serial port and, when a line arrives reading "TIME?", writes back
"TIME <milliseconds since the epoch>".

It is deliberately NOT the bridge. No MAVLink, no disk queue, no network, no
caddis-api. It reads one token and writes one line.

It is also stateless and silent by default. It never speaks first, so once the
Pico has its anchor and stops asking, this produces no traffic at all for the
rest of the deployment.

The one judgement it makes is refusing to answer when the clock cannot be
trusted. See clock_is_trustworthy().
"""

import ctypes
import ctypes.util
import logging
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is required: pip3 install pyserial")

# The Raspberry Pi Pico's USB vendor ID.
#
# Matched on the VID rather than taking the first ttyACM, because the ArduPilot
# autopilot enumerates as the same CDC ACM class. Guessing by enumeration order
# means that on some boots this opens the flight controller instead, which is a
# port nothing here has any business writing to.
PICO_VID = 0x2E8A

# Never 1200. On a Pico with the stock stdio settings, a host opening the port
# at 1200 baud reboots it into BOOTSEL: the board stops being a serial device,
# comes back as mass storage, and logging stops with nothing written to say why.
# The firmware now disables that (#69), but this stays pinned regardless, since
# the reset is triggered by the host's choice rather than the firmware's.
BAUD = 115200

REQUEST = "TIME?"
REPLY_PREFIX = "TIME "

# A line longer than this is not something this protocol produces. The Pico's
# records share this link and run a few hundred bytes, so the cap exists only so
# a wedged port cannot grow a buffer without bound.
LINE_MAX = 512

RECONNECT_DELAY_S = 2.0

# Kernel clock states from adjtimex(2). TIME_ERROR means STA_UNSYNC is set: the
# clock is running free and has not been disciplined.
TIME_ERROR = 5

# Nothing before this is a real time. Mirrors BOOT_TIME_MIN_EPOCH_MS in
# boot_time.h; the Pico enforces it too, and neither side relies on the other.
MIN_PLAUSIBLE_EPOCH_S = 1735689600  # 2025-01-01T00:00:00Z

log = logging.getLogger("time-responder")


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
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        # A zeroed struct timex means modes == 0, which is a read-only query.
        # Oversized on purpose so the exact layout does not have to be declared:
        # only the return value is read.
        buf = ctypes.create_string_buffer(512)
        state = libc.adjtimex(buf)
    except (OSError, AttributeError) as exc:
        log.warning("could not query kernel clock state (%s); refusing", exc)
        return False

    if state < 0:
        log.warning("adjtimex failed; refusing to answer")
        return False
    if state == TIME_ERROR:
        return False

    # Belt and braces: a daemon that never clears STA_UNSYNC would pass the
    # check above with a 1970 clock.
    return time.time() >= MIN_PLAUSIBLE_EPOCH_S


def find_pico() -> "str | None":
    """The device path of the attached Pico, or None if it is not there."""
    for port in list_ports.comports():
        if port.vid == PICO_VID:
            return port.device
    return None


def serve(port_path: str) -> None:
    """Answer requests on one port until it goes away."""
    with serial.Serial(port_path, BAUD, timeout=1) as link:
        log.info("listening on %s", port_path)
        buf = bytearray()
        while True:
            chunk = link.read(256)
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > LINE_MAX:
                # Keep the tail only. The request is short, so anything this
                # long is a record and cannot contain a request.
                del buf[:-LINE_MAX]

            while b"\n" in buf:
                raw, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                if raw.decode("ascii", errors="replace").strip() != REQUEST:
                    # Everything else here is the Pico's record stream.
                    continue

                if not clock_is_trustworthy():
                    log.info("request received, clock not yet synced; silent")
                    continue

                epoch_ms = time.time_ns() // 1_000_000
                link.write(f"{REPLY_PREFIX}{epoch_ms}\n".encode("ascii"))
                link.flush()
                log.info("answered with %d", epoch_ms)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    while True:
        port_path = find_pico()
        if port_path is None:
            log.info("no Pico (USB VID 0x%04X) present; waiting", PICO_VID)
            time.sleep(RECONNECT_DELAY_S)
            continue
        try:
            serve(port_path)
        except (serial.SerialException, OSError) as exc:
            # Unplugged, reflashed, or the port was taken. None of it is fatal.
            log.warning("%s went away (%s); reopening", port_path, exc)
        time.sleep(RECONNECT_DELAY_S)


if __name__ == "__main__":
    main()
