"""The reader's one obligation, and the ways it used to be dodgeable."""

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
