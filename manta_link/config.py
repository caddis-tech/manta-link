"""The API token, where it comes from, and how it changes without a restart.

Three objects, split so the dangerous mistake cannot be written. `Config` is
frozen and inert and holds the token. `TokenSession` is the only object in this
process that ever puts an `Authorization` header on anything. `ConfigWatcher` is
the only caller of `TokenSession.rebind`, and it runs on the health thread.

The split exists because of one specific silent failure. Bind a token to a
module-global session once at import, and a later `Config` object changes
nothing: rotation appears to work, the operator believes a compromised token is
retired, and it is still in use. Nothing anywhere surfaces that. So the uploader
is handed a `Binding` and never a token, and there is no spelling of
`session.headers["Authorization"] = ...` anywhere outside `rebind`.

A missing token is a normal state, not an error. Nothing POSTs, nothing spins,
capture and spool and archive carry on, and the reader keeps answering `TIME?`.
Dropping a token in starts uploads within one heartbeat with no restart, and a
401 self-heals the same way, because a container restart is the one event that
can lose an in-flight `TIME?`.
"""

import hashlib
import logging
import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .logging_setup import Throttle

log = logging.getLogger(__name__)

TOKEN_ENV = "CADDIS_API_TOKEN"
API_URL_ENV = "CADDIS_API_URL"
BATCH_MAX_ENV = "CADDIS_BATCH_MAX"

ENV_FILENAME = ".env"

DEFAULT_API_URL = "https://api.caddistech.com"

SOURCE_FILE = "file"
SOURCE_ENVIRONMENT = "environment"

# Enough to prove a rotation took effect and far too little to help anyone
# reverse. Eight hex characters over a 40-character key: an operator can compare
# two of these by eye, which is the whole job.
FINGERPRINT_CHARS = 8

# The server's own cap is 200 and a batch over it is a 400. Well under, because
# a smaller batch loses less to a connection dropped mid-POST on a cellular
# link, and the queue drains at the same rate either way.
DEFAULT_BATCH_MAX = 50
SERVER_BATCH_MAX = 200

# A .env holding one token is a few hundred bytes. Past this it is not our file,
# and reading the rest of it into memory on the health thread buys nothing.
MAX_ENV_BYTES = 64 * 1024

# Hosts where plain http is allowed, because a bench run points this at a
# caddis-api on the same desk and there is no certificate to be had.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

REFUSAL_LOG_INTERVAL_S = 300.0


@dataclass(frozen=True)
class Config:
    """What this process believes about the API, at one moment."""

    api_url: str = DEFAULT_API_URL
    # repr=False because supervisor.run_forever logs an exception with
    # log.exception, and anything reachable from a traceback frame can be
    # rendered into it.
    token: str | None = field(default=None, repr=False)
    source: str | None = None
    batch_max: int = DEFAULT_BATCH_MAX

    @property
    def configured(self) -> bool:
        return self.token is not None

    @property
    def fingerprint(self) -> str | None:
        return fingerprint_of(self.token)

    @property
    def batch_endpoint(self) -> str:
        return f"{self.api_url.rstrip('/')}/api/v1/devices/telemetry/batch/"


@dataclass(frozen=True)
class Binding:
    """A session and the credential generation it carries.

    The uploader compares generations to know a rotation happened. It never sees
    the token, and the fingerprint is all it can put in a log line.
    """

    session: requests.Session
    generation: int
    fingerprint: str | None
    configured: bool


def fingerprint_of(token: str | None) -> str | None:
    if token is None:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


class TokenSession:
    """The only place an Authorization header is ever written."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._binding = self._build(Config())

    def current(self) -> Binding:
        return self._binding

    def rebind(self, config: Config) -> Binding:
        """Point a new session at the new credential, and close the old one.

        A new session rather than a header written onto the live one. The
        uploader may be mid-POST on it, and mutating the headers of a session in
        flight is a request that goes out with neither credential cleanly.
        """
        with self._lock:
            previous = self._binding
            self._generation += 1
            self._binding = self._build(config)
        # Closed after the swap and outside the lock. The uploader adopts the
        # new binding on its next pass, so a request already in flight on the
        # old session finishes against the socket it opened.
        previous.session.close()
        return self._binding

    def _build(self, config: Config) -> Binding:
        session = requests.Session()
        if config.token is not None:
            session.headers["Authorization"] = f"Token {config.token}"
        return Binding(
            session=session,
            generation=self._generation,
            fingerprint=config.fingerprint,
            configured=config.configured,
        )


class ConfigWatcher:
    """Re-reads the environment file on the heartbeat, and says what changed.

    Never on the reader thread once the port is open. The one exception is the
    synchronous read `main` does before the reader starts, which is on the main
    thread by definition: without it a boat provisioned through Kraken's Env
    would wait a full heartbeat for its first upload.
    """

    def __init__(self, data_dir: Path, tokens: TokenSession) -> None:
        self._path = data_dir / ENV_FILENAME
        self._tokens = tokens
        self._config: Config | None = None
        # Non-reentrant and non-blocking. health.py deliberately permits two
        # live health threads, and a slow read of this very file is the most
        # likely cause of the second one. Two threads racing a reload leave the
        # watcher's config and the session's header describing different
        # credentials, which is the exact failure this module exists to prevent,
        # reintroduced by its own fix.
        self._lock = threading.Lock()
        self._refusal_log = Throttle(REFUSAL_LOG_INTERVAL_S)

    @property
    def config(self) -> Config:
        return self._config if self._config is not None else Config()

    @property
    def path(self) -> Path:
        return self._path

    def reload(self) -> bool:
        """Re-read, rebind if anything changed, and report whether it did."""
        if not self._lock.acquire(blocking=False):
            # Another health thread is already in here. Skipping is right: it is
            # doing the same work, and waiting would put this thread to sleep
            # holding nothing anyone needs.
            return False
        try:
            return self._reload_once()
        finally:
            self._lock.release()

    def _reload_once(self) -> bool:
        previous = self._config
        current = self._resolve()
        self._config = current
        if previous == current:
            return False

        self._tokens.rebind(current)
        self._announce(previous, current)
        return True

    def _announce(self, previous: "Config | None", current: Config) -> None:
        if not current.configured:
            log.warning("no API token configured; uploads are off")
            return
        if previous is None or not previous.configured:
            log.info("API token loaded, fingerprint %s (source: %s)",
                     current.fingerprint, current.source)
            return
        if previous.fingerprint == current.fingerprint:
            # Config carries `source`, so moving a token from Kraken's Env into
            # the file compares unequal while the credential is identical. Two
            # matching fingerprints in a "changed from X to X" line is confusing
            # at the worst possible moment.
            log.info("API token source changed to %s; the credential did not",
                     current.source)
            return
        log.warning("API token changed from %s to %s (source: %s)",
                    previous.fingerprint, current.fingerprint, current.source)

    def _resolve(self) -> Config:
        from_file = self._read_file()
        return Config(
            api_url=self._resolve_url(from_file),
            token=self._resolve_token(from_file),
            source=self._resolve_source(from_file),
            batch_max=self._resolve_batch_max(from_file),
        )

    def _resolve_token(self, from_file: "dict[str, str] | None") -> str | None:
        """File first, environment second.

        File first because it is the only ordering that permits rotation without
        recreating the container, and a container restart is the one event that
        can lose an in-flight TIME?. Environment second because boats already in
        service hold their token in Kraken's Env field, so reading it means
        MANTA Link replaces the bridge on those hulls with no re-provisioning.
        """
        for source in (from_file or {}, os.environ):
            token = source.get(TOKEN_ENV, "").strip()
            if token:
                return token
        # An empty value is not a token. `CADDIS_API_TOKEN=` in a file passes any
        # presence check and then 401s forever while the process reports healthy.
        return None

    def _resolve_source(self, from_file: "dict[str, str] | None") -> str | None:
        if (from_file or {}).get(TOKEN_ENV, "").strip():
            return SOURCE_FILE
        if os.environ.get(TOKEN_ENV, "").strip():
            return SOURCE_ENVIRONMENT
        return None

    def _resolve_url(self, from_file: "dict[str, str] | None") -> str:
        raw = (from_file or {}).get(API_URL_ENV) or os.environ.get(API_URL_ENV)
        if not raw:
            return DEFAULT_API_URL

        parsed = urlsplit(raw.strip())
        if parsed.username or parsed.password:
            return self._refuse_url(raw, "it carries a credential")
        if parsed.scheme == "https":
            return raw.strip()
        if parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS:
            # A bench caddis-api on the same desk, where there is no certificate
            # to be had. Anything else in plain http puts a device credential on
            # the wire in clear.
            return raw.strip()
        return self._refuse_url(raw, f"{parsed.scheme or 'no scheme'} is not https")

    def _refuse_url(self, raw: str, why: str) -> str:
        if self._refusal_log.should_emit():
            # The value is named but never echoed: a URL refused for carrying a
            # credential must not have that credential logged in the refusal.
            log.warning("%s is unusable (%s); falling back to %s",
                        API_URL_ENV, why, DEFAULT_API_URL)
        return DEFAULT_API_URL

    def _resolve_batch_max(self, from_file: "dict[str, str] | None") -> int:
        raw = (from_file or {}).get(BATCH_MAX_ENV) or os.environ.get(BATCH_MAX_ENV)
        if not raw:
            return DEFAULT_BATCH_MAX
        try:
            wanted = int(raw)
        except ValueError:
            return self._refuse_batch_max(raw, "not a number")
        if wanted < 1:
            return self._refuse_batch_max(raw, "below one")
        if wanted > SERVER_BATCH_MAX:
            return self._refuse_batch_max(raw, f"over the server's {SERVER_BATCH_MAX}")
        return wanted

    def _refuse_batch_max(self, raw: str, why: str) -> int:
        # Loudly, with the value seen and the value used. A silent clamp is how
        # an operator spends an afternoon wondering why a setting did nothing.
        if self._refusal_log.should_emit():
            log.warning("%s=%s is %s; using %d instead",
                        BATCH_MAX_ENV, raw, why, DEFAULT_BATCH_MAX)
        return DEFAULT_BATCH_MAX

    def _read_file(self) -> "dict[str, str] | None":
        """The file's contents, or None if there is nothing usable to read.

        None means "preserve whatever the environment says" rather than "there
        is no token". The difference matters on the very first pass: a boat
        provisioned through Kraken's Env, whose .env path is a directory or
        raises EIO, would otherwise report no token forever while holding a
        perfectly good one.
        """
        try:
            info = self._path.stat()
        except FileNotFoundError:
            # The normal state on a boat with no token, and on every boat before
            # provisioning. Not a refusal and not worth a line.
            return None
        except OSError as exc:
            return self._refuse_file(f"could not be checked ({exc})")

        if not stat.S_ISREG(info.st_mode):
            # A FIFO reports st_size 0, so the size gate below does not catch
            # one, and opening it blocks the heartbeat for as long as nothing
            # writes to it.
            return self._refuse_file("is not a regular file")
        if info.st_size > MAX_ENV_BYTES:
            return self._refuse_file(f"is larger than {MAX_ENV_BYTES} bytes")

        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return self._refuse_file(f"could not be read ({exc})")
        return parse_env(text)

    def _refuse_file(self, why: str) -> "dict[str, str] | None":
        """Say so once, and answer None, which preserves the environment."""
        if self._refusal_log.should_emit():
            log.warning("%s %s; falling back to the environment", self._path, why)
        return None


def parse_env(text: str) -> dict[str, str]:
    """The subset of .env syntax this needs, and nothing else.

    Written here rather than taking python-dotenv, because the package has one
    runtime dependency and this is fifteen lines. Accepts comments, blank lines,
    an `export ` prefix, CRLF, surrounding whitespace, and one level of matching
    quotes. Refuses anything else by ignoring the line: a malformed line in a
    credential file should not decide what the credential is.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        name, separator, value = stripped.partition("=")
        if not separator:
            continue
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values
