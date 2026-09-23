"""Finding the USB device behind a port, for the one thing that resets it."""

from types import SimpleNamespace

from manta_link import portfinder

AUTOPILOT_VID = 0x1209


def port(device, usb_device_path=None, vid=portfinder.PICO_VID):
    """A stand-in for one entry of pyserial's port listing."""
    return SimpleNamespace(device=device, usb_device_path=usb_device_path, vid=vid)


def test_the_node_comes_from_that_ports_usb_device(monkeypatch, tmp_path):
    pico = tmp_path / "1-1.3"
    pico.mkdir()
    (pico / "busnum").write_text("1\n")
    (pico / "devnum").write_text("5\n")
    monkeypatch.setattr(
        portfinder.list_ports,
        "comports",
        lambda: [
            # The autopilot is the same CDC class and is listed first here.
            port("/dev/ttyACM0", str(tmp_path / "1-1.2"), vid=AUTOPILOT_VID),
            port("/dev/ttyACM1", str(pico)),
        ],
    )
    assert portfinder.usb_device_node("/dev/ttyACM1") == "/dev/bus/usb/001/005"


def test_a_port_that_is_not_listed_has_no_node(monkeypatch):
    monkeypatch.setattr(portfinder.list_ports, "comports", lambda: [])
    assert portfinder.usb_device_node("/dev/ttyACM0") is None


def test_a_port_with_no_usb_device_has_no_node(monkeypatch):
    monkeypatch.setattr(
        portfinder.list_ports, "comports", lambda: [port("/dev/ttyACM0")]
    )
    assert portfinder.usb_device_node("/dev/ttyACM0") is None
