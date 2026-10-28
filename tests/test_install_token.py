"""Saving the token a boat was installed with into its own .env.

The install settings are readable by anything on the boat's network, so the
token has to end up in the file. Every way this can go wrong is quiet: a token
written over the wrong line, another line of the file lost, a credential in a
log line, or a boat that stops reporting because its volume was read-only.
"""

import errno
import os
import stat

import pytest

from manta_link import __main__, config, install_token
from manta_link.config import ConfigWatcher, TokenSession
from manta_link.install_token import save_install_token

# Made up, and different from each other, so a log line or a file holding the
# wrong one is caught.
INSTALLED = "1a" * 20
EARLIER = "2b" * 20
EARLIEST = "3c" * 20


@pytest.fixture(autouse=True)
def no_ambient_configuration(monkeypatch):
    """The real environment must not decide what these tests see."""
    for name in (config.TOKEN_ENV, config.API_URL_ENV, config.BATCH_MAX_ENV,
                 __main__.VOLUME_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def installed_with_a_token(monkeypatch):
    monkeypatch.setenv(config.TOKEN_ENV, INSTALLED)


def env_file(directory):
    return directory / config.ENV_FILENAME


def write_env(directory, text: str) -> None:
    env_file(directory).write_bytes(text.encode("utf-8"))


def read_env(directory) -> str:
    return env_file(directory).read_bytes().decode("utf-8")


def first_read(directory) -> config.Config:
    watcher = ConfigWatcher(directory, TokenSession())
    watcher.reload()
    return watcher.config


@pytest.mark.usefixtures("installed_with_a_token")
class TestAFreshVolume:
    def test_the_install_token_is_saved_into_a_new_env(self, tmp_path):
        save_install_token(tmp_path)

        assert read_env(tmp_path) == f"{config.TOKEN_ENV}={INSTALLED}\n"

    def test_the_first_read_at_start_finds_it_in_the_file(
        self, tmp_path, monkeypatch
    ):
        # The order in load_config is the point: saved after the first read, a
        # boat would announce the environment and then change source a minute on.
        monkeypatch.setenv(__main__.VOLUME_ENV, str(tmp_path))

        watcher = __main__.load_config(TokenSession())

        assert watcher.config.token == INSTALLED
        assert watcher.config.source == config.SOURCE_FILE

    def test_the_save_is_logged_by_fingerprint_with_the_file_named(
        self, tmp_path, caplog
    ):
        with caplog.at_level("INFO"):
            save_install_token(tmp_path)

        assert config.fingerprint_of(INSTALLED) in caplog.text
        assert str(env_file(tmp_path)) in caplog.text
        assert "replacing" not in caplog.text

    def test_no_temporary_file_is_left_beside_it(self, tmp_path):
        save_install_token(tmp_path)

        assert [path.name for path in tmp_path.iterdir()] == [config.ENV_FILENAME]

    @pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX only")
    def test_the_saved_file_is_readable_by_its_owner_only(self, tmp_path):
        save_install_token(tmp_path)

        assert stat.S_IMODE(env_file(tmp_path).stat().st_mode) == 0o600


@pytest.mark.usefixtures("installed_with_a_token")
class TestAFileHoldingTheSameToken:
    @pytest.mark.parametrize("written", [INSTALLED, f'"{INSTALLED}"', f" {INSTALLED} "])
    def test_it_is_left_untouched(self, tmp_path, caplog, written):
        original = f"# provisioned by hand\n{config.TOKEN_ENV}={written}\n"
        write_env(tmp_path, original)
        before = env_file(tmp_path).stat().st_mtime_ns

        with caplog.at_level("INFO"):
            save_install_token(tmp_path)

        assert read_env(tmp_path) == original
        assert env_file(tmp_path).stat().st_mtime_ns == before
        assert "saved the API token" not in caplog.text


@pytest.mark.usefixtures("installed_with_a_token")
class TestAFileHoldingADifferentToken:
    def test_the_install_token_replaces_it(self, tmp_path):
        """The install settings are the newer, deliberate act, so they win."""
        write_env(tmp_path, f"{config.TOKEN_ENV}={EARLIER}\n")

        save_install_token(tmp_path)

        assert first_read(tmp_path).token == INSTALLED

    def test_every_other_line_is_kept_exactly(self, tmp_path):
        # CRLF, comments, blanks, an export and an unquoted URL: none of it is
        # ours to tidy, and a lost API URL sends uploads to production.
        write_env(tmp_path,
                  "# bench boat\r\n"
                  f"{config.API_URL_ENV}=https://staging.example.test\r\n"
                  "\r\n"
                  f"export {config.TOKEN_ENV}='{EARLIER}'\r\n"
                  f"{config.BATCH_MAX_ENV}=25")

        save_install_token(tmp_path)

        assert read_env(tmp_path) == (
            "# bench boat\r\n"
            f"{config.API_URL_ENV}=https://staging.example.test\r\n"
            "\r\n"
            f"{config.TOKEN_ENV}={INSTALLED}\r\n"
            f"{config.BATCH_MAX_ENV}=25")

    def test_the_replacement_is_logged_with_both_fingerprints(
        self, tmp_path, caplog
    ):
        write_env(tmp_path, f"{config.TOKEN_ENV}={EARLIER}\n")

        with caplog.at_level("INFO"):
            save_install_token(tmp_path)

        saved = [r for r in caplog.records if "saved the API token" in r.message]
        assert len(saved) == 1
        assert saved[0].levelname == "INFO"
        assert config.fingerprint_of(INSTALLED) in saved[0].message
        assert f"replacing {config.fingerprint_of(EARLIER)}" in saved[0].message

    def test_a_repeated_assignment_leaves_one_line_where_the_first_was(
        self, tmp_path
    ):
        # The later line is the one that counted, and leaving either would keep
        # a retired credential sitting in the file.
        write_env(tmp_path,
                  f"{config.TOKEN_ENV}={EARLIEST}\n"
                  f"{config.API_URL_ENV}=https://example.test\n"
                  f"{config.TOKEN_ENV}={EARLIER}\n")

        save_install_token(tmp_path)

        assert read_env(tmp_path) == (
            f"{config.TOKEN_ENV}={INSTALLED}\n"
            f"{config.API_URL_ENV}=https://example.test\n")

    def test_a_blank_token_line_is_filled_in_where_it_stands(self, tmp_path):
        write_env(tmp_path, f"{config.TOKEN_ENV}=\n{config.BATCH_MAX_ENV}=25\n")

        save_install_token(tmp_path)

        assert read_env(tmp_path) == (
            f"{config.TOKEN_ENV}={INSTALLED}\n{config.BATCH_MAX_ENV}=25\n")

    def test_a_file_with_no_token_line_gets_one_on_a_line_of_its_own(
        self, tmp_path
    ):
        # No newline at the end of the last line, which is how printf without
        # \n leaves one, and appending straight onto it would corrupt the URL.
        write_env(tmp_path, f"{config.API_URL_ENV}=https://example.test")

        save_install_token(tmp_path)

        assert read_env(tmp_path) == (
            f"{config.API_URL_ENV}=https://example.test\n"
            f"{config.TOKEN_ENV}={INSTALLED}\n")

    def test_a_commented_out_token_is_not_a_token_line(self, tmp_path):
        write_env(tmp_path, f"# {config.TOKEN_ENV}={EARLIER}\n")

        save_install_token(tmp_path)

        assert read_env(tmp_path) == (
            f"# {config.TOKEN_ENV}={EARLIER}\n{config.TOKEN_ENV}={INSTALLED}\n")


class TestNothingToSave:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_no_token_in_the_environment_writes_no_file(
        self, tmp_path, monkeypatch, value
    ):
        if value is not None:
            monkeypatch.setenv(config.TOKEN_ENV, value)

        save_install_token(tmp_path)

        assert not env_file(tmp_path).exists()

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_no_token_in_the_environment_leaves_the_file_alone(
        self, tmp_path, monkeypatch, value
    ):
        # A blank install setting is not an instruction to clear the token.
        if value is not None:
            monkeypatch.setenv(config.TOKEN_ENV, value)
        write_env(tmp_path, f"{config.TOKEN_ENV}={EARLIER}\n")

        save_install_token(tmp_path)

        assert read_env(tmp_path) == f"{config.TOKEN_ENV}={EARLIER}\n"

    @pytest.mark.parametrize(
        "value",
        [
            f"{INSTALLED}\n{config.API_URL_ENV}=https://elsewhere.example.test",
            f"{INSTALLED}\r{config.API_URL_ENV}=https://elsewhere.example.test",
            f'"{INSTALLED}"',
            f"{INSTALLED[:20]}\x1d{INSTALLED[20:]}",
            # An undecodable byte in the environment, which Python hands over as
            # a lone surrogate, and which no UTF-8 file can hold.
            f"{INSTALLED[:20]}\udc80{INSTALLED[20:]}",
        ],
    )
    def test_a_value_that_would_not_read_back_intact_is_not_written(
        self, tmp_path, monkeypatch, caplog, value
    ):
        # Written as is, a line break plants lines nobody wrote, here an API URL
        # that would carry the device token off to another host.
        monkeypatch.setenv(config.TOKEN_ENV, value)
        write_env(tmp_path, f"{config.TOKEN_ENV}={EARLIER}\n")

        with caplog.at_level("WARNING"):
            save_install_token(tmp_path)

        assert read_env(tmp_path) == f"{config.TOKEN_ENV}={EARLIER}\n"
        assert "cannot be written as one .env line" in caplog.text


@pytest.mark.usefixtures("installed_with_a_token")
class TestAFileThatIsNotOursToRewrite:
    def test_a_directory_in_its_place_is_left_alone(self, tmp_path, caplog):
        env_file(tmp_path).mkdir()

        with caplog.at_level("WARNING"):
            save_install_token(tmp_path)

        assert env_file(tmp_path).is_dir()
        assert f"{env_file(tmp_path)} is not a regular file" in caplog.text

    def test_an_oversized_file_is_left_alone(self, tmp_path, caplog):
        oversized = "#" * (config.MAX_ENV_BYTES + 1)
        write_env(tmp_path, oversized)

        with caplog.at_level("WARNING"):
            save_install_token(tmp_path)

        assert read_env(tmp_path) == oversized
        assert "larger than" in caplog.text

    def test_a_file_that_is_not_utf8_is_left_alone(self, tmp_path, caplog):
        original = b"\xff\xfe" + f"{config.TOKEN_ENV}={EARLIER}\n".encode()
        env_file(tmp_path).write_bytes(original)

        with caplog.at_level("WARNING"):
            save_install_token(tmp_path)

        assert env_file(tmp_path).read_bytes() == original
        assert "could not be read" in caplog.text

    def test_the_boat_still_reads_the_token_from_the_environment(self, tmp_path):
        env_file(tmp_path).mkdir()

        save_install_token(tmp_path)

        loaded = first_read(tmp_path)
        assert loaded.token == INSTALLED
        assert loaded.source == config.SOURCE_ENVIRONMENT


def refuse(error: OSError):
    def fail(*args, **kwargs):
        raise error
    return fail


@pytest.mark.usefixtures("installed_with_a_token")
class TestAWriteThatFails:
    def test_a_read_only_volume_is_a_warning_and_the_boat_carries_on(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(install_token.tempfile, "mkstemp",
                            refuse(OSError(errno.EROFS, "Read-only file system")))

        with caplog.at_level("WARNING"):
            save_install_token(tmp_path)

        assert f"into {env_file(tmp_path)}" in caplog.text
        assert "Read-only file system" in caplog.text
        assert first_read(tmp_path).token == INSTALLED

    def test_a_refused_rename_keeps_the_old_file_and_leaves_no_temporary_one(
        self, tmp_path, monkeypatch, caplog
    ):
        write_env(tmp_path, f"{config.TOKEN_ENV}={EARLIER}\n")
        monkeypatch.setattr(install_token.os, "replace",
                            refuse(PermissionError(errno.EACCES, "Permission denied")))

        with caplog.at_level("WARNING"):
            save_install_token(tmp_path)

        assert read_env(tmp_path) == f"{config.TOKEN_ENV}={EARLIER}\n"
        assert [path.name for path in tmp_path.iterdir()] == [config.ENV_FILENAME]
        assert "Permission denied" in caplog.text

    def test_an_unexpected_failure_does_not_stop_the_start(
        self, tmp_path, monkeypatch, caplog
    ):
        # The process owns the Pico's port; a start-up nicety failing in a way
        # nobody planned for must not leave the boat with no TIME? responder.
        monkeypatch.setenv(__main__.VOLUME_ENV, str(tmp_path))
        monkeypatch.setattr(install_token, "save_install_token",
                            refuse(RuntimeError("not planned for")))

        with caplog.at_level("ERROR"):
            watcher = __main__.load_config(TokenSession())

        assert watcher.config.token == INSTALLED
        assert "RuntimeError" in caplog.text


def a_fresh_volume(tmp_path, monkeypatch):
    pass


def a_file_holding_another_token(tmp_path, monkeypatch):
    write_env(tmp_path, f"{config.TOKEN_ENV}={EARLIER}\n")


def a_file_that_cannot_be_read(tmp_path, monkeypatch):
    env_file(tmp_path).write_bytes(b"\xff" + EARLIER.encode())


def a_write_that_fails(tmp_path, monkeypatch):
    write_env(tmp_path, f"{config.TOKEN_ENV}={EARLIER}\n")
    monkeypatch.setattr(install_token.os, "replace",
                        refuse(OSError(errno.EIO, "Input/output error")))


@pytest.mark.usefixtures("installed_with_a_token")
@pytest.mark.parametrize(
    "arrange",
    [a_fresh_volume, a_file_holding_another_token, a_file_that_cannot_be_read,
     a_write_that_fails],
)
def test_no_log_line_carries_either_token(tmp_path, monkeypatch, caplog, arrange):
    arrange(tmp_path, monkeypatch)

    with caplog.at_level("DEBUG"):
        save_install_token(tmp_path)

    assert caplog.records
    assert INSTALLED not in caplog.text
    assert EARLIER not in caplog.text
