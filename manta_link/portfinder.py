"""Locating the Pico among the USB serial devices on the boat."""

import logging

from serial.tools import list_ports

log = logging.getLogger(__name__)

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
# The firmware disables that (#69), but this stays pinned regardless, since the
# reset is triggered by the host's choice rather than the firmware's.
BAUD = 115200


def find_pico_port() -> "str | None":
    """The device path of the attached Pico, or None if it is not there."""
    matches = [p.device for p in list_ports.comports() if p.vid == PICO_VID]

    if not matches:
        return None
    if len(matches) > 1:
        # Two Picos on one boat is not a configuration we have, so this is a
        # symptom rather than a choice to make silently.
        log.warning("%d devices with VID 0x%04X (%s); using %s",
                    len(matches), PICO_VID, ", ".join(matches), matches[0])
    return matches[0]
