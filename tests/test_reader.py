"""The reader's one obligation, and the ways it used to be dodgeable."""

import time
from collections import deque

import pytest
import serial

from manta_link import clock
from manta_link import reader as reader_mod
from manta_link.reader import SerialReader

from .fakes import FakeSerial, StopPlayback
from .golden import READING
from .test_framing import BANNER, DEBUG_BANNER

EPOCH_MS = 1_754_400_000_000


@pytest.fixture
def trusted_clock(monkeypatch):
    monkeypatch.setattr(clock, "clock_is_trustworthy", lambda: True)
    monkeypatch.setattr(clock, "epoch_ms_now", lambda: EPOCH_MS)


@pytest.fixture
def untrusted_clock(monkeypatch):
    monkeypatch.setattr(clock, "clock_is_trustworthy", lambda: False)


def serve_once(monkeypatch, fake_factory) -> SerialReader:
    """Run one _serve() against a scripted port until the script runs out."""
    monkeypatch.setattr(reader_mod.serial, "Serial", fake_factory)
    rdr = SerialReader()
    with pytest.raises(StopPlayback):
        rdr._serve("/dev/fake")
    return rdr


class TestAnswering:
    def test_answers_a_request(self, monkeypatch, trusted_clock):
        factory = FakeSerial.factory(chunks=[b"TIME?\n"])
        rdr = serve_once(monkeypatch, factory)
        assert factory.instance.written == [f"TIME {EPOCH_MS}\n".encode()]
        assert rdr.answered_count == 1

    def test_reply_matches_the_format_the_firmware_parses(
        self, monkeypatch, trusted_clock
    ):
        # boot_time_parse_reply requires the exact prefix "TIME ", then digits,
        # then only blanks or a terminator.
        factory = FakeSerial.factory(chunks=[b"TIME?\n"])
        serve_once(monkeypatch, factory)
        payload = factory.instance.written[0]
        assert payload.startswith(b"TIME ")
        assert payload.endswith(b"\n")
        assert payload[5:-1].isdigit()

    def test_stays_silent_when_the_clock_is_not_synced(
        self, monkeypatch, untrusted_clock
    ):
        factory = FakeSerial.factory(chunks=[b"TIME?\n"])
        rdr = serve_once(monkeypatch, factory)
        assert factory.instance.written == []
        assert rdr.answered_count == 0

    def test_answers_a_request_split_across_reads(self, monkeypatch, trusted_clock):
        factory = FakeSerial.factory(chunks=[b"TI", b"ME", b"?", b"\n"])
        rdr = serve_once(monkeypatch, factory)
        assert rdr.answered_count == 1

    def test_answers_a_request_that_follows_a_full_size_record(
        self, monkeypatch, trusted_clock
    ):
        factory = FakeSerial.factory(chunks=[READING + b"\n" + b"TIME?\n"])
        rdr = serve_once(monkeypatch, factory)
        assert rdr.answered_count == 1

    def test_ignores_a_request_quoted_in_a_log_line(self, monkeypatch, trusted_clock):
        factory = FakeSerial.factory(
            chunks=[b"WARN: no time from the Pi in 180000 ms; TIME? unanswered\r\n"]
        )
        rdr = serve_once(monkeypatch, factory)
        assert rdr.answered_count == 0


class TestWriteHang:
    def test_a_write_timeout_is_survivable(self, monkeypatch, trusted_clock):
        factory = FakeSerial.factory(
            chunks=[b"TIME?\n", b"TIME?\n"],
            write_raises=serial.SerialTimeoutException("no drain"),
        )
        rdr = serve_once(monkeypatch, factory)
        # Both requests were seen; neither killed the loop and neither counted.
        assert rdr.answered_count == 0

    def test_never_calls_flush(self, monkeypatch, trusted_clock):
        """flush() is a bare tcdrain with no timeout of its own.

        Bounding write() and then flushing moves the hang rather than fixing
        it, so the reply path must not flush at all.
        """
        factory = FakeSerial.factory(chunks=[b"TIME?\n"])
        serve_once(monkeypatch, factory)
        assert factory.instance.flush_calls == 0

    def test_opens_with_a_bounded_write_timeout_and_exclusivity(
        self, monkeypatch, trusted_clock
    ):
        factory = FakeSerial.factory(chunks=[b"TIME?\n"])
        serve_once(monkeypatch, factory)
        opened = factory.instance.open_kwargs
        assert opened["write_timeout"] == reader_mod.WRITE_TIMEOUT_S
        assert opened["exclusive"] is True
        assert opened["timeout"] == reader_mod.READ_TIMEOUT_S


class TestAbsentPico:
    def test_the_first_absence_is_logged_however_long_the_host_has_been_up(
        self, monkeypatch, caplog
    ):
        """monotonic counts from host boot, not from this process starting.

        Kraken starts the extension around 45s of uptime, and the bench window
        where somebody is replugging the Pico is the five minutes after that.
        """
        monkeypatch.setattr(time, "monotonic", lambda: 45.0)
        rdr = SerialReader()

        with caplog.at_level("INFO"):
            rdr._log_absent()

        assert "no Pico" in caplog.text

    def test_an_absence_after_a_connection_is_logged_at_once(
        self, monkeypatch, caplog
    ):
        """Connect, then vanish, which is what unplugging the cable looks like.

        The interval has to start again from the connection, so the second
        absence is audible without waiting five minutes for it.
        """
        monkeypatch.setattr(time, "monotonic", lambda: 45.0)
        rdr = SerialReader()
        rdr._log_absent()

        monkeypatch.setattr(reader_mod.serial, "Serial",
                            FakeSerial.factory(chunks=[BANNER + b"\r\n"]))
        with pytest.raises(StopPlayback):
            rdr._serve("/dev/fake")

        with caplog.at_level("INFO"):
            rdr._log_absent()

        assert "no Pico" in caplog.text

    def test_a_second_absence_inside_the_interval_stays_quiet(
        self, monkeypatch, caplog
    ):
        """At the reconnect delay this line is 43,200 a day into a capped history."""
        monkeypatch.setattr(time, "monotonic", lambda: 45.0)
        rdr = SerialReader()
        rdr._log_absent()

        with caplog.at_level("INFO"):
            rdr._log_absent()

        assert "no Pico" not in caplog.text


class TestDispatch:
    def test_records_go_to_the_buffer_with_a_receipt_time(
        self, monkeypatch, trusted_clock
    ):
        seen = deque(maxlen=8)
        monkeypatch.setattr(reader_mod.serial, "Serial",
                            FakeSerial.factory(chunks=[READING + b"\n"]))
        rdr = SerialReader(records=seen)
        with pytest.raises(StopPlayback):
            rdr._serve("/dev/fake")

        assert len(seen) == 1
        raw, received = seen[0]
        assert raw == READING
        # The monotonic receipt is what a late-derived anchor is built from, so
        # it must be captured at the reader rather than at parse time.
        assert isinstance(received, float) and received > 0

    def test_a_debug_banner_is_reported_loudly(self, monkeypatch, caplog):
        monkeypatch.setattr(reader_mod.serial, "Serial",
                            FakeSerial.factory(chunks=[DEBUG_BANNER + b"\r\n"]))
        rdr = SerialReader()
        with caplog.at_level("WARNING"):
            with pytest.raises(StopPlayback):
                rdr._serve("/dev/fake")
        assert "recording NOTHING" in caplog.text

    def test_a_flight_banner_is_not_a_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(reader_mod.serial, "Serial",
                            FakeSerial.factory(chunks=[BANNER + b"\r\n"]))
        rdr = SerialReader()
        with caplog.at_level("WARNING"):
            with pytest.raises(StopPlayback):
                rdr._serve("/dev/fake")
        assert "recording NOTHING" not in caplog.text

    def test_a_release_stream_buffers_no_records(self, monkeypatch, trusted_clock):
        """The current fleet state: banner, requests and log lines only.

        A flight image routes records to the card, so the record path must stay
        dormant rather than merely correct.
        """
        records: deque = deque(maxlen=8)
        logs: deque = deque(maxlen=8)
        program = (
            BANNER + b"\r\n"
            + b"TIME?\n"
            + b"Boot time synced: epoch 1754400000000 ms at 4231 ms uptime\r\n"
            + b"SD card initialized\r\n"
            + b"File opened: data3.txt\r\n"
        )
        monkeypatch.setattr(reader_mod.serial, "Serial",
                            FakeSerial.factory(chunks=[program]))
        rdr = SerialReader(records=records, logs=logs)
        with pytest.raises(StopPlayback):
            rdr._serve("/dev/fake")

        assert list(records) == []
        assert rdr.answered_count == 1
        # The Pico's own log lines still land somewhere a worker can drain.
        assert b"SD card initialized" in logs


class _Clock:
    """time for run_forever: a second passes per reading, and sleeps are free."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        self.now += 1.0
        return self.now

    @staticmethod
    def sleep(_seconds: float) -> None:
        return None


class TestSilence:
    """A port that has carried bytes and then stops is reset, once.

    On 2026-09-22 a Pico's stream stopped mid-run with the port still open and
    no error anywhere, and nothing noticed for the last 45 minutes of the run.
    """

    @staticmethod
    def run_one_serve(monkeypatch, factory):
        """One serve, then one reconnect attempt that ends the test.

        Returns the resets requested, each with whether the port was already
        closed at the time, and how many reconnect notices were sent.
        """
        ports = iter(["/dev/fake"])

        def next_port():
            try:
                return next(ports)
            except StopIteration:
                raise StopPlayback from None

        resets = []
        reconnects = []
        monkeypatch.setattr(reader_mod, "time", _Clock())
        monkeypatch.setattr(reader_mod, "find_pico_port", next_port)
        monkeypatch.setattr(reader_mod.serial, "Serial", factory)
        monkeypatch.setattr(
            reader_mod,
            "reset_usb_device",
            lambda port: resets.append((port, factory.instance.closed)),
        )

        rdr = SerialReader(on_reconnect=lambda: reconnects.append(True))
        with pytest.raises(StopPlayback):
            rdr.run_forever()
        return resets, len(reconnects)

    def test_a_port_that_falls_silent_is_reset_after_it_closes(
        self, monkeypatch, caplog
    ):
        factory = FakeSerial.factory(
            chunks=[BANNER + b"\r\n"], stall_forever_after=True
        )
        with caplog.at_level("WARNING"):
            resets, reconnects = self.run_one_serve(monkeypatch, factory)

        assert resets == [("/dev/fake", True)]
        # The reset re-enumerates the Pico, which is what the notice is for.
        assert reconnects == 1
        assert "no bytes from /dev/fake for 60s; resetting the USB device" in (
            caplog.text
        )

    def test_a_port_that_never_talked_is_not_reset(self, monkeypatch):
        # Four minutes of nothing, then the device goes away on its own.
        factory = FakeSerial.factory(stall_forever_after=True, raise_on_read=240)
        resets, _ = self.run_one_serve(monkeypatch, factory)
        assert resets == []

    def test_a_steady_stream_is_not_reset(self, monkeypatch):
        # Four minutes of a line a second, well past the silence limit in total.
        factory = FakeSerial.factory(chunks=[b"SD card initialized\r\n"] * 240)
        resets, _ = self.run_one_serve(monkeypatch, factory)
        assert resets == []


class TestResetUsbDevice:
    """Whatever goes wrong, the reset falls back to the plain reopen."""

    def test_a_device_that_cannot_be_found_is_left_alone(self, monkeypatch, caplog):
        monkeypatch.setattr(reader_mod, "usb_device_node", lambda _port: None)
        with caplog.at_level("WARNING"):
            reader_mod.reset_usb_device("/dev/ttyACM0")
        assert "could not find the USB device behind /dev/ttyACM0" in caplog.text

    def test_a_reset_that_fails_does_not_raise(self, monkeypatch, caplog, tmp_path):
        missing = str(tmp_path / "no-such-node")
        monkeypatch.setattr(reader_mod, "usb_device_node", lambda _port: missing)
        with caplog.at_level("WARNING"):
            reader_mod.reset_usb_device("/dev/ttyACM0")
        assert f"could not reset USB device {missing}" in caplog.text
