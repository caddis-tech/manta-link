"""Moving a token MANTA Link was installed with into its own .env.

An installer delivers the token in BlueOS's install settings, which become this
container's environment. Kraken's API hands those settings to anyone on the
boat's network who asks, with no login, so a token left there is one request
away from the whole network. No API serves the data volume's .env, but it is no
secret from a shell on the Pi, and BlueOS's web interface offers a terminal: the
move narrows who can read the token, it does not lock it away. MANTA Link saves
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
    API_URL_ENV,
    DEFAULT_API_URL,
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

_TEMP_SUFFIX = ".tmp"


def save_install_token(data_dir: Path) -> None:
    """Write the environment's token into the .env unless the file holds it.

    A token changes only through the installer, so a different one already in
    the file is a hand edit, and the install settings' token replaces it.
    """
    path = data_dir / ENV_FILENAME
    _remove_abandoned_copies(path)

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        return

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

    assigned = _token_assignments(text)
    if assigned == [token]:
        return

    if _names_another_api(text):
        log.warning("%s names another API than the install settings; not saving "
                    "the API token from the environment into it", path)
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
    _announce_saved(path, token, assigned[-1] if assigned else "")


def _remove_abandoned_copies(path: Path) -> None:
    """Delete the temporary copies a save killed before its rename left behind.

    Each holds a token, and would otherwise outlive that token's retirement.
    """
    try:
        for leftover in path.parent.glob(f"{path.name}.*{_TEMP_SUFFIX}"):
            leftover.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove a temporary copy of %s (%s)", path, exc)


def _token_assignments(text: str) -> list[str]:
    """Every token the file assigns, in order, overridden ones included."""
    assignments = (parse_env_line(line) for line in text.splitlines())
    return [value.strip() for name, value in filter(None, assignments)
            if name == TOKEN_ENV]


def _names_another_api(text: str) -> bool:
    """Whether the file sends uploads to an API the install settings do not.

    A bench boat's file pairs its own API with its own token, and the install
    token saved there would go to an API it was never issued for.
    """
    named = parse_env(text).get(API_URL_ENV, "").strip()
    if not named:
        return False
    installed = os.environ.get(API_URL_ENV, "").strip() or DEFAULT_API_URL
    return named.rstrip("/") != installed.rstrip("/")


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
