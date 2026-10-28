"""Moving a token MANTA Link was installed with into its own .env.

An installer delivers the token in BlueOS's install settings, which become this
container's environment. Kraken's API answers anyone on the boat's network with
no login, settings included, so a token left there is readable from the whole
network, while the data volume's .env takes a shell on the Pi. MANTA Link saves
the token into the file, and the installer takes it back out of the settings
(caddis-tech/aquadrone-setup#3).

Runs once, at start, before the first config read. Nothing here may stop the
process: a boat that cannot save its token carries on exactly as it did before
this existed, and keeps reporting.
"""

import logging
import os
import tempfile
from pathlib import Path

from .config import (
    ENV_FILENAME,
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


def save_install_token(data_dir: Path) -> None:
    """Write the environment's token into the .env unless the file holds it.

    A token changes only through the installer, so a different one already in
    the file is a hand edit, and the install settings' token replaces it.
    """
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        return

    path = data_dir / ENV_FILENAME
    try:
        text = read_env_text(path) or ""
    except UnusableEnvFile as exc:
        log.warning("%s %s; not saving the API token from the environment into it",
                    path, exc)
        return

    saved = parse_env(text).get(TOKEN_ENV, "").strip()
    if saved == token:
        return

    line = f"{TOKEN_ENV}={token}"
    if not _reads_back_intact(line, token):
        log.warning("%s in the environment cannot be written as one .env line; "
                    "not saving it into %s", TOKEN_ENV, path)
        return

    try:
        _replace_file(path, _with_token_line(text, line))
    except OSError as exc:
        log.warning("could not save the API token from the environment into %s "
                    "(%s)", path, exc)
        return
    _announce_saved(path, token, saved)


def _reads_back_intact(line: str, token: str) -> bool:
    """Whether `line`, written to a .env, would read back as exactly `token`.

    Not with a control character, a line break, or surrounding quotes in the
    value: the file would then hold another token, or lines nobody wrote.
    """
    return token.isprintable() and parse_env(line) == {TOKEN_ENV: token}


def _with_token_line(text: str, line: str) -> str:
    """`text` with `line` as its only token assignment, all else as it was.

    The new line takes the first assignment's place. Any later ones go, so no
    retired token is left sitting in the file.
    """
    kept: list[str] = []
    placed = False
    for original in text.splitlines(keepends=True):
        assignment = parse_env_line(original)
        if assignment is None or assignment[0] != TOKEN_ENV:
            kept.append(original)
        elif not placed:
            kept.append(line + _line_ending(original))
            placed = True
    if not placed:
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
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
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
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    if not _CAN_FSYNC_DIRECTORY:
        return
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _announce_saved(path: Path, token: str, replaced: str) -> None:
    if replaced:
        log.info("saved the API token from the environment into %s, "
                 "fingerprint %s, replacing %s",
                 path, fingerprint_of(token), fingerprint_of(replaced))
        return
    log.info("saved the API token from the environment into %s, fingerprint %s",
             path, fingerprint_of(token))
