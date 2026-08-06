"""A scriptable stand-in for a serial port, with the faults that matter.

Real hardware failures on this link are not exotic: a Pico that stops draining,
a device that vanishes mid-read, and a termios error that is not an OSError.
Each of those has cost us something, so each is injectable here.
"""

import serial


class FakeSerial:
    """Plays a byte program back in chunks, then behaves as scripted.

    Instantiate through `factory()` so it can be substituted for serial.Serial.
    """

    def __init__(
        self,
        chunks=None,
        raise_on_read=None,
        write_raises=None,
        stall_forever_after=None,
    ) -> None:
        self._chunks = list(chunks or [])
        self._raise_on_read = raise_on_read
        self._write_raises = write_raises
        self._stall_forever_after = stall_forever_after
        self.written = []
        self.reads = 0
        self.closed = False
        self.flush_calls = 0
        self.exclusive_claimed = False

    @classmethod
    def factory(cls, **kwargs):
        """A callable with serial.Serial's signature that yields one instance."""
        instance = cls(**kwargs)

        def make(*_args, **_kwargs):
            instance.open_kwargs = _kwargs
            return instance

        make.instance = instance
        return make

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True
        return False

    def fileno(self) -> int:
        return 0

    def read(self, _size: int) -> bytes:
        self.reads += 1

        if self._raise_on_read is not None and self.reads >= self._raise_on_read:
            raise serial.SerialException("device reports readiness but no data")

        if self._chunks:
            return self._chunks.pop(0)

        if self._stall_forever_after is not None:
            # An open, permanently quiet port: the ordinary select timeout.
            return b""

        # Nothing left to play. Ending the run keeps tests from spinning.
        raise StopPlayback()

    def write(self, payload: bytes) -> int:
        if self._write_raises is not None:
            raise self._write_raises
        self.written.append(payload)
        return len(payload)

    def flush(self) -> None:
        # Tracked so a test can assert nobody calls it: pyserial's flush is a
        # bare tcdrain with no timeout, which write_timeout does not govern.
        self.flush_calls += 1


class StopPlayback(Exception):
    """Raised when a scripted port runs out, to end a test's serve loop."""
