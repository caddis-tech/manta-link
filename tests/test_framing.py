"""Framing is where the subtle failures live, so this is the heaviest suite.

Every case here is expressible as bytes in and lines out, with no serial port
and no clock involved.
"""

import pytest

from manta_link.framing import MAX_LINE, Kind, LineAssembler, classify, parse_banner

# Lines the firmware actually emits, copied from my_project.c rather than
# paraphrased. If any of these ever classifies as a record or a time request,
# the reader either tries to parse a log line or answers a request nobody made.
FIRMWARE_LOG_LINES = [
    b"SD card initialized",
    b"Filesystem mounted",
    b"File opened: data3.txt",
    b"ERROR: Could not initialize SD card",
    b"ERROR: Could not mount filesystem (3)",
    b"ERROR: no unused dataN.txt below 1000",
    b"ERROR: Could not create data3.txt error (4), attempt 2/3",
    b"ERROR: giving up on the card; continuing without it",
    b"ERROR: write/flush failed (2, err=1); 5 failures, retry in 8 cycles",
    b"Boot time synced: epoch 1754400000000 ms at 4231 ms uptime",
    b"WARN: no time from the Pi in 180000 ms; records will carry uptime only",
]

BANNER = b"=== Aquadrone firmware 2.0.0: SD LOGGING ENABLED ==="
DEBUG_BANNER = b"=== Aquadrone firmware 2.0.0: SD LOGGING DISABLED ==="

READING = (
    b'{"type":"reading","time":"0:01:12","epoch_ms":1754400072000,"sd_ready":1,'
    b'"sd_writes_failed":0,"sd_mounts_failed":0,"cond_tds_sal":"210,105,0.10",'
    b'"ph":"7.234","temp_code":"0x1A2B","temperature":18.4,"uv_counts":812,'
    b'"uv_mv":655,"uv_index":3.1,"uv_saturated":0}'
)


def collect(assembler: LineAssembler, chunks) -> list:
    out = []
    for chunk in chunks:
        out.extend(assembler.feed(chunk))
    return out


def split_every(data: bytes, size: int) -> list:
    return [data[i:i + size] for i in range(0, len(data), size)]


class TestLineAssembly:
    def test_splits_on_lf(self):
        assert collect(LineAssembler(), [b"one\ntwo\n"]) == [b"one", b"two"]

    def test_strips_crlf(self):
        # The firmware mixes terminators: TIME? and records end in bare LF,
        # every status line ends in CRLF.
        assert collect(LineAssembler(), [b"a\r\nb\n"]) == [b"a", b"b"]

    def test_holds_a_partial_line_across_feeds(self):
        asm = LineAssembler()
        assert asm.feed(b"par") == []
        assert asm.feed(b"tial\n") == [b"partial"]

    def test_reassembles_a_full_size_record_from_tiny_chunks(self):
        # A 2 KB record arrives in 64-byte USB packets in reality; 7 is worse.
        asm = LineAssembler()
        assert collect(asm, split_every(READING + b"\n", 7)) == [READING]

    @pytest.mark.parametrize("size", [1, 2, 3, 7, 64, 256, 4096])
    def test_output_is_identical_at_every_chunk_size(self, size):
        """The decisive property: framing must not depend on read size.

        The deployed responder's over-long handling silently did depend on it,
        which is what made the bug invisible.
        """
        program = (
            BANNER + b"\r\n"
            + b"TIME?\n"
            + READING + b"\n"
            + b"SD card initialized\r\n"
            + b"TIME?\n"
        )
        expected = [
            BANNER,
            b"TIME?",
            READING,
            b"SD card initialized",
            b"TIME?",
        ]
        assert collect(LineAssembler(), split_every(program, size)) == expected

    def test_every_split_offset_gives_the_same_lines(self):
        program = b"TIME?\n" + READING + b"\n" + b"Filesystem mounted\r\n"
        expected = [b"TIME?", READING, b"Filesystem mounted"]
        for offset in range(1, len(program)):
            asm = LineAssembler()
            chunks = [program[:offset], program[offset:]]
            assert collect(asm, chunks) == expected, f"split at {offset}"


class TestOverLongLines:
    def test_discards_the_over_long_line_only(self):
        asm = LineAssembler(max_line=64)
        junk = b"x" * 200
        lines = collect(asm, [junk + b"\nTIME?\n"])
        assert lines == [b"TIME?"]

    def test_discards_an_over_long_line_arriving_in_one_chunk(self):
        # This case never reaches the no-newline branch, so it needs its own
        # length check. It regressed once during development.
        asm = LineAssembler(max_line=64)
        assert collect(asm, [b"y" * 200 + b"\n"]) == []

    def test_a_request_immediately_after_a_discard_survives(self):
        asm = LineAssembler(max_line=64)
        lines = collect(asm, [b"z" * 100, b"more", b"\n", b"TIME?\n"])
        assert lines == [b"TIME?"]

    def test_never_emits_a_spliced_line(self):
        """Losing a line is acceptable. Inventing one is not."""
        asm = LineAssembler(max_line=64)
        lines = collect(asm, split_every(b"a" * 300 + b"\n" + b"TIME?\n", 13))
        assert all(line == b"TIME?" for line in lines)


class TestReadSizeRegression:
    """Why the tail-trim had to go, expressed as a test.

    The deployed responder keeps the tail of an over-long line. That is safe
    only because it reads 256 bytes against a 512-byte cap. This reader reads
    4096, where the same trim destroys complete requests.
    """

    PROGRAM = b"X" * 600 + b"\nTIME?\n" + b"Z" * 600 + b"\n"

    @staticmethod
    def old_tail_trim(chunks, line_max=512) -> list:
        """The deployed algorithm, reproduced from time_responder.py:126-133."""
        out = []
        buf = bytearray()
        for chunk in chunks:
            buf.extend(chunk)
            if len(buf) > line_max:
                del buf[:-line_max]
            while b"\n" in buf:
                raw, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                if raw.decode("ascii", errors="replace").strip() == "TIME?":
                    out.append(b"TIME?")
        return out

    def test_old_algorithm_survives_at_the_deployed_read_size(self):
        chunks = split_every(self.PROGRAM, 256)
        assert self.old_tail_trim(chunks) == [b"TIME?"]

    def test_old_algorithm_loses_the_request_at_the_new_read_size(self):
        chunks = split_every(self.PROGRAM, 4096)
        assert self.old_tail_trim(chunks) == []

    def test_new_assembler_survives_at_both(self):
        for size in (256, 4096):
            asm = LineAssembler()
            lines = collect(asm, split_every(self.PROGRAM, size))
            assert b"TIME?" in lines, f"lost the request at read({size})"


class TestReset:
    def test_reset_drops_a_partial_line(self):
        asm = LineAssembler()
        asm.feed(b"half a rec")
        asm.reset()
        assert asm.feed(b"ord\n") == [b"ord"]

    def test_reset_clears_the_discard_state(self):
        asm = LineAssembler(max_line=16)
        asm.feed(b"q" * 40)
        asm.reset()
        # Without the reset the next newline would be eaten as the terminator
        # of the line we gave up on, taking a real line with it.
        assert asm.feed(b"TIME?\n") == [b"TIME?"]


class TestClassify:
    def test_the_request(self):
        assert classify(b"TIME?") is Kind.TIME_REQUEST

    @pytest.mark.parametrize("raw", [b"TIME?\r", b"  TIME?  ", b"TIME?\r\n"])
    def test_the_request_with_surrounding_whitespace(self, raw):
        assert classify(raw) is Kind.TIME_REQUEST

    @pytest.mark.parametrize("raw", [
        b"WARN: no time from the Pi in 180000 ms; records will carry uptime only",
        b"the Pico sends TIME? every five seconds",
        b"TIME? TIME?",
        b"TIME",
        b"TIME 1754400000000",
    ])
    def test_a_request_quoted_inside_another_line_does_not_count(self, raw):
        assert classify(raw) is not Kind.TIME_REQUEST

    def test_records(self):
        assert classify(READING) is Kind.RECORD
        assert classify(b'  {"type":"boot"}') is Kind.RECORD

    def test_banner(self):
        assert classify(BANNER) is Kind.BANNER

    @pytest.mark.parametrize("line", FIRMWARE_LOG_LINES)
    def test_real_firmware_log_lines_are_logs(self, line):
        assert classify(line) is Kind.LOG

    @pytest.mark.parametrize("line", FIRMWARE_LOG_LINES)
    def test_no_firmware_log_line_is_ever_a_request_or_record(self, line):
        assert classify(line) not in (Kind.TIME_REQUEST, Kind.RECORD)

    def test_empty_and_binary_are_garbage(self):
        assert classify(b"") is Kind.GARBAGE
        assert classify(b"   ") is Kind.GARBAGE
        assert classify(b"\x00\x01\xff\xfe") is Kind.GARBAGE


class TestParseBanner:
    def test_extracts_version_and_state(self):
        assert parse_banner(BANNER) == ("2.0.0", "SD LOGGING ENABLED")

    def test_flags_a_debug_image(self):
        parsed = parse_banner(DEBUG_BANNER)
        assert parsed is not None
        assert "DISABLED" in parsed[1]

    def test_returns_none_for_anything_else(self):
        assert parse_banner(b"SD card initialized") is None


def test_max_line_clears_a_full_size_record_with_room_to_spare():
    """RECORD_JSON_MAX is 2048; the cap must never truncate a real record."""
    assert MAX_LINE >= 2 * 2048
