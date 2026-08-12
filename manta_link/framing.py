"""Turning a byte stream into lines, and lines into kinds.

Pure: no I/O, no clock, no serial. Everything here is testable by feeding it
bytes, which matters because framing is where the subtle failures live.
"""

import enum
import re

# Two times RECORD_JSON_MAX (2048) plus slack, so a full-size record always fits
# with escaping headroom and never trips the over-long path.
MAX_LINE = 4096

REQUEST = b"TIME?"

_BANNER_RE = re.compile(rb"^=== Aquadrone firmware (\S+): (.*) ===$")

# Anything outside this is not something the firmware emits on this link.
_PRINTABLE = bytes(range(0x20, 0x7F)) + b"\t"


class Kind(enum.Enum):
    TIME_REQUEST = "time_request"
    RECORD = "record"
    BANNER = "banner"
    LOG = "log"
    GARBAGE = "garbage"


def classify(line: bytes) -> Kind:
    """What a single complete line is.

    Operates on bytes throughout. The deployed responder decoded with
    errors="replace" before comparing, which is harmless in practice but means
    the match depends on how a decoder handles junk rather than on the bytes
    that actually arrived.
    """
    stripped = line.strip()
    if not stripped:
        return Kind.GARBAGE

    # Exact equality, never a substring. Log lines quote this token, and the
    # firmware prints "WARN: no time from the Pi ..." on the same stream.
    if stripped == REQUEST:
        return Kind.TIME_REQUEST

    if stripped[:1] == b"{":
        return Kind.RECORD

    if _BANNER_RE.match(stripped):
        return Kind.BANNER

    if all(b in _PRINTABLE for b in stripped):
        return Kind.LOG

    return Kind.GARBAGE


def parse_banner(line: bytes) -> "tuple[str, str] | None":
    """(version, state) from a boot banner, or None if it is not one.

    The state half carries "SD LOGGING ENABLED" or "SD LOGGING DISABLED", which
    is the direct signal that a Debug image is flashed and recording nothing.
    """
    match = _BANNER_RE.match(line.strip())
    if match is None:
        return None
    version, state = match.groups()
    return version.decode("ascii", "replace"), state.decode("ascii", "replace")


class LineAssembler:
    """Accumulates chunks and yields complete lines.

    Accepts LF or CRLF, because the firmware genuinely mixes them: TIME? and
    records end in bare LF while the status lines end in CRLF.
    """

    def __init__(self, max_line: int = MAX_LINE) -> None:
        self._max_line = max_line
        self._buf = bytearray()
        self._discarding = False

    def reset(self) -> None:
        """Drop any partial line. Call on every reconnect.

        Bytes straddling a disconnect are garbage, and carrying them forward
        splices the tail of one run onto the head of the next into a line that
        was never sent.
        """
        self._buf.clear()
        self._discarding = False

    def feed(self, chunk: bytes) -> "list[bytes]":
        """Complete lines contained in everything fed so far, CR/LF stripped."""
        lines: list[bytes] = []
        self._buf.extend(chunk)

        while True:
            index = self._buf.find(b"\n")

            if index < 0:
                # No terminator yet. An over-long line means framing is already
                # lost, so drop bytes until the next newline rather than keeping
                # a window of them.
                #
                # The deployed responder keeps the TAIL here (del buf[:-512]).
                # That is safe only because it reads 256 bytes at a time against
                # a 512-byte cap, an undocumented coupling. At the 4096-byte
                # reads this reader uses, the same trim discards complete TIME?
                # lines outright. Discarding to the next newline costs exactly
                # one line and can never fabricate or destroy a later one.
                if len(self._buf) > self._max_line:
                    self._discarding = True
                    self._buf.clear()
                return lines

            line = bytes(self._buf[:index])
            del self._buf[:index + 1]

            if self._discarding:
                # This newline terminates the line we gave up on.
                self._discarding = False
                continue

            # The same cap applies to a line that arrived complete inside one
            # chunk, which never reaches the no-newline branch above.
            if len(line) > self._max_line:
                continue

            lines.append(line.rstrip(b"\r\n"))
