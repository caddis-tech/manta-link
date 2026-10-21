"""The heartbeat POST: MANTA Link telling caddis-api that it is running.

The beat is bare. Its arrival is the whole message, so it carries nothing from
the Pico: readings are the Pico's channel, and caddis-api keeps the firmware
version in step from those. The counters stay in health's log line.

It runs as its own worker rather than inside health's tick. A POST can block for
its whole timeout, and name resolution is not covered by a timeout at all, so a
beat on the health thread could stall the watchdog past HEALTH_STALL_S and leave
two of them running.
"""

import logging
import time

import requests

from .config import ConfigWatcher, TokenSession
from .health import Counters
from .logging_setup import Throttle

log = logging.getLogger(__name__)

# Its own number rather than health's log-line interval: Squadron reads presence
# from this cadence, and retuning the log line must not move it. The period is
# this plus however long the last POST took.
BEAT_INTERVAL_S = 60.0

# The upload's timeout, for the upload's reason: a slow API on a cellular link
# is not a dead one. A slow answer delays the next beat; it cannot stack them.
REQUEST_TIMEOUT_S = 30.0

FAILURE_LOG_INTERVAL_S = 300.0

# What HeartbeatView answers. Anything else in a 2xx came from something between
# us and caddis-api, such as a captive portal, which answers 200 to everything.
RECEIVED = "heartbeat received"


class Heartbeat:
    """A Health worker that POSTs a bare beat every minute."""

    def __init__(
        self,
        tokens: TokenSession,
        watcher: ConfigWatcher,
        counters: Counters,
        interval_s: float = BEAT_INTERVAL_S,
    ) -> None:
        self._tokens = tokens
        self._watcher = watcher
        self._counters = counters
        self._interval_s = interval_s
        self._failure_log = Throttle(FAILURE_LOG_INTERVAL_S)

    def run_forever(self) -> None:
        """Never returns. Supervised, and restarted if it ever does.

        Beats before the first sleep, so caddis-api hears of a restart at once
        rather than a minute later.
        """
        while True:
            self.beat_once()
            time.sleep(self._interval_s)

    def beat_once(self) -> bool:
        """One POST. Returns whether caddis-api, and not something in front of
        it, said it was received."""
        binding = self._tokens.current()
        if not binding.configured:
            # The uploader already says once that uploads are off, and a beat
            # with no token is a 401 that proves nothing.
            return False

        # The uploader's session, shared. Its connection pool is thread-safe,
        # and nothing here touches its headers, which is the part that is not.
        try:
            response = binding.session.post(
                self._watcher.config.heartbeat_endpoint, timeout=REQUEST_TIMEOUT_S
            )
        except requests.RequestException as exc:
            # The type and nothing else, as in upload.py: the exception can carry
            # the request, and the request carries the Authorization header.
            return self._report_failure(type(exc).__name__)

        # Not response.ok, which is true of a 3xx as well.
        if not 200 <= response.status_code < 300:
            return self._report_failure(f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            return self._report_failure("2xx with a body that is not JSON")
        if not isinstance(body, dict) or body.get("status") != RECEIVED:
            return self._report_failure("2xx with a body that is not ours")
        self._counters.bump("heartbeats_posted")
        return True

    def _report_failure(self, reason: str) -> bool:
        self._counters.bump("heartbeat_post_failures")
        if self._failure_log.should_emit():
            log.warning("heartbeat POST failed (%s); %d more suppressed since the "
                        "last of these", reason, self._failure_log.take_suppressed())
        return False
