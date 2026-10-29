"""Moving a token MANTA Link was installed with into its own .env.

An installer delivers the token in BlueOS's install settings, which become this
container's environment. Kraken's API hands those settings to anyone on the
boat's network who asks, with no login, so a token left there is one request
away from the whole network. No API serves the data volume's .env, but it is no
secret from a shell on the Pi, and BlueOS's web interface offers a terminal: the
move narrows who can read the token, it does not lock it away. MANTA Link saves
the token into the file, with the API address it came with, and the installer
takes it back out of the settings (caddis-tech/aquadrone-setup#3).

Runs once, at start, before the first config read. Nothing here may stop the
process: a boat that cannot save its token carries on exactly as it did before
this existed, and keeps reporting.
"""

import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import (
    API_URL_ENV,
    DEFAULT_API_URL,
    ENV_FILENAME,
    MAX_ENV_BYTES,
    TOKEN_ENV,
    UnusableEnvFile,
    fingerprint_of,
    parse_env,
    parse_env_line,
    read_env_text,
)

log = logging.getLogger(__name__)

# The spool's reason: only Linux can fsync a directory, and the container is
# Linux. Without it a power cut just after the rename can bring the old file back.
_CAN_FSYNC_DIRECTORY = hasattr(os, "O_DIRECTORY")

_TEMP_SUFFIX = ".tmp"

# mkstemp's own shape between prefix and suffix: eight of a-z, 0-9 and _. Only
# names like that are ours to delete; a .env.backup.tmp is someone else's.
_TEMP_RANDOM_PART = "[a-z0-9_]" * 8


def save_install_token(data_dir: Path) -> None:
    """Write the environment's token, with the API address it came with, into
    the .env unless the file holds them.

    A token changes only through the installer, so a different one already in
    the file is a hand edit, and the install settings' token replaces it. A
    token belongs to the API that issued it, so the settings' address replaces
    the file's too, or the file's comes out when the settings name none.
    """
    path = data_dir / ENV_FILENAME
    _remove_abandoned_copies(path)

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        return
    url = os.environ.get(API_URL_ENV, "").strip()

    if path.is_symlink():
        # os.replace would swap the link for a plain file and leave its target
        # holding the old token, so the two would disagree about it.
        log.warning("%s is a symbolic link; not saving the API token from the "
                    "environment into it", path)
        return

    try:
        text = read_env_text(path) or ""
    except UnusableEnvFile as exc:
        log.warning("%s %s; not saving the API token from the environment into it",
                    path, exc)
        return

    assigned = _assignments(text, TOKEN_ENV)
    addresses = _assignments(text, API_URL_ENV)
    same_api = _names_the_same_api(addresses, url)
    if assigned == [token] and same_api:
        log.info("%s already holds the API token from the environment, "
                 "fingerprint %s", path, fingerprint_of(token))
        return

    if not _reads_back_intact(TOKEN_ENV, token):
        log.warning("%s in the environment cannot be written as one .env line; "
                    "not saving it into %s", TOKEN_ENV, path)
        return
    if url and not _reads_back_intact(API_URL_ENV, url):
        log.warning("%s in the environment cannot be written as one .env line; "
                    "not saving it, or the API token that goes only with it, "
                    "into %s", API_URL_ENV, path)
        return

    rewritten = _with_assignment(text, TOKEN_ENV, token)
    if not same_api:
        rewritten = _with_assignment(rewritten, API_URL_ENV, url or None)
    if len(rewritten.encode("utf-8")) > MAX_ENV_BYTES:
        log.warning("%s would be larger than %d bytes with the API token in it; "
                    "not saving it", path, MAX_ENV_BYTES)
        return

    try:
        _replace_file(path, rewritten)
    except OSError as exc:
        log.warning("could not save the API token from the environment into %s "
                    "(%s)", path, exc)
        return
    _announce_saved(path, token, assigned[-1] if assigned else "")
    _announce_move(addresses, url)


def _remove_abandoned_copies(path: Path) -> None:
    """Delete the temporary copies a save killed before its rename left behind.

    Each holds a token, and would otherwise outlive that token's retirement.
    """
    pattern = f"{path.name}.{_TEMP_RANDOM_PART}{_TEMP_SUFFIX}"
    try:
        leftovers = list(path.parent.glob(pattern))
    except OSError as exc:
        log.warning("could not look for temporary copies of %s (%s)", path, exc)
        return
    for leftover in leftovers:
        try:
            leftover.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove %s, a temporary copy of %s (%s)",
                        leftover, path, exc)


def _assignments(text: str, name: str) -> list[str]:
    """Every value the file assigns to `name`, in order, overridden ones
    included."""
    assignments = (parse_env_line(line) for line in text.splitlines())
    return [value.strip() for found, value in filter(None, assignments)
            if found == name]


def _names_the_same_api(addresses: list[str], url: str) -> bool:
    """Whether the file's address lines already say what the install settings
    do: one line naming the same API, or none when the settings name none.

    None in the file is not enough when the settings name one: once the
    installer takes the settings back out, a file naming no API sends to
    production, with a token another API issued.
    """
    wanted = [url] if url else []
    return [found.rstrip("/") for found in addresses] == [
        want.rstrip("/") for want in wanted]


def _reads_back_intact(name: str, value: str) -> bool:
    """Whether `value`, written to a .env as `name`'s line, would read back as
    exactly `value`.

    Not with a control character, a line break, or surrounding quotes in the
    value: the file would then hold another value, or lines nobody wrote.
    """
    return value.isprintable() and parse_env(f"{name}={value}") == {name: value}


def _with_assignment(text: str, name: str, value: "str | None") -> str:
    """`text` with `name` set to `value` on one line, all else as it was, or
    with no line setting `name` when `value` is None.

    The new line takes the first assignment's place. Any later ones go, so no
    retired value is left assigned in the file. A commented-out one is a
    comment, and stays as written.
    """
    line = None if value is None else f"{name}={value}"
    kept: list[str] = []
    placed = False
    for original in text.splitlines(keepends=True):
        assignment = parse_env_line(original)
        if assignment is None or assignment[0] != name:
            kept.append(original)
        elif line is not None and not placed:
            kept.append(line + _line_ending(original))
            placed = True
    if line is not None and not placed:
        if kept and not _line_ending(kept[-1]):
            kept[-1] += "\n"
        kept.append(line + "\n")
    return "".join(kept)


def _line_ending(line: str) -> str:
    """What ends one line from splitlines(keepends=True), or "" at end of file."""
    return line[len(line.splitlines()[0]):]


def _replace_file(path: Path, text: str) -> None:
    """Swap the whole file at once, so a power cut leaves one token or the other."""
    # mkstemp creates the file 0o600 where the platform has modes, so the token
    # is never readable by another user, not even before the rename.
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=_TEMP_SUFFIX
    )
    try:
        with os.fdopen(descriptor, "wb") as temp:
            temp.write(text.encode("utf-8"))
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    except OSError:
        Path(temp_name).unlink(missing_ok=True)
        raise
    try:
        _fsync_directory(path.parent)
    except OSError as exc:
        # The rename has happened, so the file holds the new token. Only whether
        # it survives a power cut in the next few seconds is in doubt.
        log.warning("could not flush the folder holding %s after saving it (%s); "
                    "a power cut now could undo the save", path, exc)


def _fsync_directory(directory: Path) -> None:
    if not _CAN_FSYNC_DIRECTORY:
        return
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _announce_saved(path: Path, token: str, replaced: str) -> None:
    if replaced and replaced != token:
        # A warning, because by the rule above this undoes a hand edit, and a
        # credential changing should stand out from the start-up lines.
        log.warning("saved the API token from the environment into %s, "
                    "fingerprint %s, replacing %s",
                    path, fingerprint_of(token), fingerprint_of(replaced))
        return
    log.info("saved the API token from the environment into %s, fingerprint %s",
             path, fingerprint_of(token))


def _announce_move(addresses: list[str], url: str) -> None:
    # Another API means another Squadron gets the boat's readings, so a move is
    # never quiet. A file that named no address already used the settings' one.
    named = addresses[-1] if addresses else ""
    if not named:
        return
    moved_to = url or DEFAULT_API_URL
    if named.rstrip("/") == moved_to.rstrip("/"):
        return
    log.warning("moved this boat's uploads to the API at %s, from %s, as its "
                "install settings say", _server(moved_to), _server(named))


def _server(url: str) -> str:
    """`url` cut to its scheme and host for a log line, since a credential or a
    path in it could carry a secret."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "an address that does not parse"
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"
