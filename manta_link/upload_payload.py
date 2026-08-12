"""A spooled envelope as the reading caddis-api accepts, or nothing at all.

Split from `upload.py` because that module is about the conversation with the
API and this is about the shape of one row, and from `record.py` because that
file is already the largest in the package.

Everything here is total. A spool entry is whatever was on disk: `Spool.load`
promises a dict and nothing about what is in it, so a bit-flipped but still
parseable file reaches this. Raising on one would crash-loop the uploader at the
supervisor's 60 second ceiling and the boat would upload nothing at all, forever,
because of one bad file. So an entry that cannot become a reading is reported as
`None` and set aside by the caller.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

# The same window the firmware and clock.py enforce. An entry outside it is
# corrupt rather than early: nothing in the pipeline can produce one, because
# resolve_stamp only ever writes a Pico stamp or an anchor plus an uptime.
MIN_PLAUSIBLE_EPOCH_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z
MAX_PLAUSIBLE_EPOCH_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z


def iso_from_epoch_ms(epoch_ms: int) -> str:
    """Millisecond ISO-8601 with a Z suffix, exactly.

    divmod on the integer, never `epoch_ms / 1000` into strftime("%f")[:-3].
    Milliseconds are not representable in binary floating point, so that route
    renders ...072123 as .122999 and truncates it to .122: one millisecond lost,
    silently, on some values and not others.
    """
    seconds, milliseconds = divmod(epoch_ms, 1000)
    moment = datetime.fromtimestamp(seconds, tz=UTC)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{milliseconds:03d}Z"


def to_reading(envelope: Any) -> "dict[str, Any] | None":
    """The API's row, or None if this entry cannot honestly become one."""
    if not isinstance(envelope, dict):
        return None

    timestamp_ms = envelope.get("timestamp_ms")
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        return None
    if not MIN_PLAUSIBLE_EPOCH_MS <= timestamp_ms < MAX_PLAUSIBLE_EPOCH_MS:
        return None

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None

    client_ref = envelope.get("client_ref")
    if not is_a_uuid(client_ref):
        # Refused rather than sent as null. The unique constraint is partial on
        # non-null (devices/models/telemetry.py), so a null ref is `created` on
        # every retry and the whole idempotency story evaporates, silently, one
        # duplicated row at a time.
        return None

    return {
        "payload": payload,
        "timestamp": iso_from_epoch_ms(timestamp_ms),
        "client_ref": client_ref,
    }


def is_a_uuid(value: Any) -> bool:
    """Probe-confirmed: a non-UUID is refused before payload validation."""
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
