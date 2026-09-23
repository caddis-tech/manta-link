"""Locating the Pico among the USB serial devices on the boat."""

import logging
from pathlib import Path

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


def usb_device_node(port_path: str) -> "str | None":
    """The /dev/bus/usb node of the USB device behind a port, or None.

    pyserial finds each port's USB device directory in sysfs as it lists them,
    the same listing find_pico_port reads, and busnum and devnum there name the
    node. devnum changes on every enumeration, so this is looked up when needed
    and never kept.
    """
    for port in list_ports.comports():
        if port.device != port_path:
            continue
        # Only pyserial's Linux listing carries this attribute.
        usb_path = getattr(port, "usb_device_path", None)
        if not usb_path:
            return None
        try:
            bus = int(Path(usb_path, "busnum").read_text())
            dev = int(Path(usb_path, "devnum").read_text())
        except (OSError, ValueError):
            return None
        return f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    return None
