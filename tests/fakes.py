"""Scriptable stand-ins for the two things outside this process.

Real hardware failures on the serial link are not exotic: a Pico that stops
draining, a device that vanishes mid-read, and a termios error that is not an
OSError. Each of those has cost us something, so each is injectable here.

The MAVLink2Rest stand-in is scripted per message name rather than per call,
because every question the GPS poller has to answer is about one message
changing while another does not, and its counter is settable for the same
reason: a test that cannot freeze a counter cannot test a dead autopilot.

Plus the one timing helper the threaded tests need, so waiting on a worker is a
bounded poll rather than a sleep long enough to be safe on a bad day.
"""

import time
from typing import Any

import serial

from manta_link.mavlink2rest import Answer, Observation, Outcome


def wait_until(predicate, timeout_s: float = 2.0, poll_s: float = 0.005) -> bool:
    """Poll until predicate holds. Returns whether it ever did."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


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
        # When each write landed, so a test can say how long the reply took
        # rather than only that it happened.
        self.written_at = []
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
        self.written_at.append(time.monotonic())
        return len(payload)

    def flush(self) -> None:
        # Tracked so a test can assert nobody calls it: pyserial's flush is a
        # bare tcdrain with no timeout, which write_timeout does not govern.
        self.flush_calls += 1


class StopPlayback(Exception):
    """Raised when a scripted port runs out, to end a test's serve loop."""


class FakeMavlink2Rest:
    """One scripted answer per message name, with the counter under test control."""

    def __init__(self) -> None:
        self._answers: dict[str, Answer] = {}
        self.asked: list[str] = []

    def observe(
        self, name: str, fields: "dict[str, Any]", counter: "int | None" = 1
    ) -> None:
        """Script a message as present, at a counter the test chooses."""
        self._answers[name] = Answer(Outcome.OBSERVED, Observation(fields, counter))

    def fail(self, name: str, outcome: Outcome) -> None:
        self._answers[name] = Answer(outcome)

    def advance(self, name: str, fields: "dict[str, Any] | None" = None) -> None:
        """Move a message's counter on, as a live autopilot would."""
        previous = self._answers[name].observation
        assert previous is not None, f"{name} was never observed"
        counter = 1 if previous.counter is None else previous.counter + 1
        self._answers[name] = Answer(
            Outcome.OBSERVED,
            Observation(previous.fields if fields is None else fields, counter),
        )

    def message(self, name: str) -> Answer:
        self.asked.append(name)
        # An unscripted message is one the autopilot has never sent, which is
        # the state a real service reports for most of the MAVLink dialect.
        return self._answers.get(name, Answer(Outcome.ABSENT))
