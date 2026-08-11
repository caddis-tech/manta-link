"""Where the token comes from, and what happens when it changes.

Every failure this guards against is silent. A token bound once at import makes
rotation a no-op while the operator believes a compromised credential is
retired. An empty value in a .env passes any presence check and then 401s
forever while the process reports healthy. A plaintext API URL copied off a
bench puts a live device credential on the wire in clear. None of them raise.
"""

import stat
import threading

import pytest

from manta_link import config
from manta_link.config import Config, ConfigWatcher, TokenSession

TOKEN = "a" * 40
OTHER_TOKEN = "b" * 40


@pytest.fixture(autouse=True)
def no_ambient_configuration(monkeypatch):
    """The real environment must not decide what these tests see."""
    for name in (config.TOKEN_ENV, config.API_URL_ENV, config.BATCH_MAX_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def watcher(tmp_path):
    return ConfigWatcher(tmp_path, TokenSession())


def write_env(directory, text: str) -> None:
    (directory / config.ENV_FILENAME).write_text(text, encoding="utf-8")


def header(watcher: ConfigWatcher) -> "str | None":
    return watcher._tokens.current().session.headers.get("Authorization")


class TestReloadReachesTheLiveSession:
    def test_reload_changes_the_live_sessions_authorization_header(
        self, tmp_path, watcher
    ):
        """The one that would have caught the prior art.

        It bound the token onto a module-global session once at import. A new
        config object changed nothing, so rotation silently did nothing, and the
        operator believed a compromised credential was retired while it was
        still on every request.
        """
        write_env(tmp_path, f"{config.TOKEN_ENV}={TOKEN}")
        watcher.reload()
        assert header(watcher) == f"Token {TOKEN}"

        write_env(tmp_path, f"{config.TOKEN_ENV}={OTHER_TOKEN}")
        watcher.reload()

        assert header(watcher) == f"Token {OTHER_TOKEN}"

    def test_a_rotation_bumps_the_generation_so_the_uploader_can_see_it(
        self, tmp_path, watcher
    ):
        write_env(tmp_path, f"{config.TOKEN_ENV}={TOKEN}")
        watcher.reload()
        first = watcher._tokens.current()

        write_env(tmp_path, f"{config.TOKEN_ENV}={OTHER_TOKEN}")
        watcher.reload()

        assert watcher._tokens.current().generation > first.generation

    def test_an_unchanged_file_does_not_rebuild_the_session(self, tmp_path, watcher):
        # A rebuild per heartbeat would drop the connection pool every minute on
        # a link where the TLS handshake is the expensive part.
        write_env(tmp_path, f"{config.TOKEN_ENV}={TOKEN}")
        watcher.reload()
        session = watcher._tokens.current().session

        assert watcher.reload() is False
        assert watcher._tokens.current().session is session

    def test_the_uploader_is_never_handed_the_token_itself(self, tmp_path, watcher):
        write_env(tmp_path, f"{config.TOKEN_ENV}={TOKEN}")
        watcher.reload()

        binding = watcher._tokens.current()

        assert not hasattr(binding, "token")
        assert TOKEN not in repr(binding)
        assert binding.fingerprint == config.fingerprint_of(TOKEN)

    def test_only_config_ever_writes_an_authorization_header(self):
        """Structural, because the rule is otherwise only a convention."""
        package = config.__file__.rsplit("config.py", 1)[0]
        offenders = []
        for source in __import__("pathlib").Path(package).glob("*.py"):
            if source.name == "config.py":
                continue
            if "Authorization" in source.read_text(encoding="utf-8"):
                offenders.append(source.name)

        assert offenders == []


class TestWhereTheTokenComesFrom:
    def test_the_file_wins_over_the_environment(self, tmp_path, watcher, monkeypatch):
        """File first is what permits rotation without recreating the container,
        and a container restart is the one event that can lose a TIME?."""
        monkeypatch.setenv(config.TOKEN_ENV, OTHER_TOKEN)
        write_env(tmp_path, f"{config.TOKEN_ENV}={TOKEN}")

        watcher.reload()

        assert watcher.config.token == TOKEN
        assert watcher.config.source == config.SOURCE_FILE

    def test_the_environment_is_read_when_there_is_no_file(
        self, watcher, monkeypatch
    ):
        # Boats already in service hold their token in Kraken's Env field, so
        # reading it means MANTA Link replaces the bridge with no re-provisioning.
        monkeypatch.setenv(config.TOKEN_ENV, TOKEN)

        watcher.reload()

        assert watcher.config.token == TOKEN
        assert watcher.config.source == config.SOURCE_ENVIRONMENT

    @pytest.mark.parametrize("value", ["", "   ", '""', "''"])
    def test_an_empty_value_is_not_a_token(self, tmp_path, watcher, value):
        """The prior art's real bug.

        `CADDIS_API_TOKEN=` passes any presence check and then 401s forever
        while the process reports itself healthy.
        """
        write_env(tmp_path, f"{config.TOKEN_ENV}={value}")

        watcher.reload()

        assert watcher.config.token is None
        assert watcher.config.configured is False

    def test_an_empty_file_value_falls_through_to_the_environment(
        self, tmp_path, watcher, monkeypatch
    ):
        monkeypatch.setenv(config.TOKEN_ENV, TOKEN)
        write_env(tmp_path, f"{config.TOKEN_ENV}=")

        watcher.reload()

        assert watcher.config.token == TOKEN

    def test_no_token_anywhere_is_a_normal_state(self, watcher, caplog):
        with caplog.at_level("WARNING"):
            watcher.reload()

        assert watcher.config.configured is False
        assert "no API token configured" in caplog.text
        assert header(watcher) is None


class TestTheFileIsNotAlwaysReadable:
    def test_a_missing_file_is_not_a_refusal(self, watcher, caplog):
        with caplog.at_level("WARNING"):
            watcher.reload()

        # The normal state on every boat before provisioning, and not worth a
        # line of its own.
        assert "falling back to the environment" not in caplog.text

    def test_an_unreadable_path_on_the_first_pass_still_finds_the_environment(
        self, tmp_path, monkeypatch, caplog
    ):
        """The bootstrap case.

        A boat provisioned through Kraken's Env whose .env path is a directory
        would otherwise report no token forever while holding a good one.
        """
        monkeypatch.setenv(config.TOKEN_ENV, TOKEN)
        (tmp_path / config.ENV_FILENAME).mkdir()
        watcher = ConfigWatcher(tmp_path, TokenSession())

        with caplog.at_level("WARNING"):
            watcher.reload()

        assert watcher.config.token == TOKEN
        assert "is not a regular file" in caplog.text

    def test_a_fifo_is_refused_rather_than_blocking_the_heartbeat(
        self, tmp_path, monkeypatch, watcher
    ):
        # A FIFO reports st_size 0, so the size gate does not catch it, and
        # opening one blocks the health thread until something writes.
        monkeypatch.setattr(
            config.Path, "stat", lambda self: _FakeStat(stat.S_IFIFO)
        )

        watcher.reload()

        assert watcher.config.token is None

    def test_a_file_larger_than_a_credential_file_is_refused(
        self, tmp_path, watcher, caplog
    ):
        write_env(tmp_path, "#" * (config.MAX_ENV_BYTES + 1))

        with caplog.at_level("WARNING"):
            watcher.reload()

        assert "larger than" in caplog.text


class _FakeStat:
    def __init__(self, mode: int) -> None:
        self.st_mode = mode
        self.st_size = 0


class TestTheApiUrl:
    def test_https_is_taken_as_given(self, tmp_path, watcher):
        write_env(tmp_path, f"{config.API_URL_ENV}=https://example.test")

        watcher.reload()

        assert watcher.config.api_url == "https://example.test"

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ],
    )
    def test_a_loopback_url_is_allowed_so_a_bench_run_works(
        self, tmp_path, watcher, url
    ):
        write_env(tmp_path, f"{config.API_URL_ENV}={url}")

        watcher.reload()

        assert watcher.config.api_url == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.caddistech.com",
            "http://10.198.95.222:8000",
            "ftp://api.caddistech.com",
            "api.caddistech.com",
        ],
    )
    def test_plaintext_to_anywhere_else_is_refused(self, tmp_path, watcher, url):
        """A bench .env becomes a template, and the template gets copied."""
        write_env(tmp_path, f"{config.API_URL_ENV}={url}")

        watcher.reload()

        assert watcher.config.api_url == config.DEFAULT_API_URL

    def test_a_url_carrying_a_credential_is_refused_without_echoing_it(
        self, tmp_path, watcher, caplog
    ):
        write_env(tmp_path, f"{config.API_URL_ENV}=https://user:hunter2@example.test")

        with caplog.at_level("WARNING"):
            watcher.reload()

        assert watcher.config.api_url == config.DEFAULT_API_URL
        assert "hunter2" not in caplog.text

    def test_the_batch_endpoint_is_built_from_it(self):
        built = Config(api_url="https://example.test/").batch_endpoint
        assert built == "https://example.test/api/v1/devices/telemetry/batch/"


class TestTheBatchSize:
    def test_a_sensible_value_is_taken(self, tmp_path, watcher):
        write_env(tmp_path, f"{config.BATCH_MAX_ENV}=25")

        watcher.reload()

        assert watcher.config.batch_max == 25

    @pytest.mark.parametrize("value", ["500", "0", "-1", "lots", "5.5"])
    def test_a_value_the_server_would_refuse_is_clamped_and_said_out_loud(
        self, tmp_path, watcher, caplog, value
    ):
        # A silent clamp is how an operator spends an afternoon wondering why a
        # setting did nothing.
        write_env(tmp_path, f"{config.BATCH_MAX_ENV}={value}")

        with caplog.at_level("WARNING"):
            watcher.reload()

        assert watcher.config.batch_max == config.DEFAULT_BATCH_MAX
        assert config.BATCH_MAX_ENV in caplog.text


class TestTheFingerprint:
    def test_it_is_a_short_hash_and_never_the_token(self):
        printed = config.fingerprint_of(TOKEN)

        assert printed is not None
        assert len(printed) == config.FINGERPRINT_CHARS
        assert printed not in TOKEN

    def test_two_tokens_do_not_share_one(self):
        assert config.fingerprint_of(TOKEN) != config.fingerprint_of(OTHER_TOKEN)

    def test_no_token_has_none(self):
        assert config.fingerprint_of(None) is None

    def test_a_rotation_is_announced_by_fingerprint_and_not_by_value(
        self, tmp_path, watcher, caplog
    ):
        write_env(tmp_path, f"{config.TOKEN_ENV}={TOKEN}")
        watcher.reload()
        write_env(tmp_path, f"{config.TOKEN_ENV}={OTHER_TOKEN}")

        with caplog.at_level("INFO"):
            watcher.reload()

        assert config.fingerprint_of(OTHER_TOKEN) in caplog.text
        assert TOKEN not in caplog.text
        assert OTHER_TOKEN not in caplog.text

    def test_moving_a_token_between_sources_does_not_claim_it_changed(
        self, tmp_path, watcher, monkeypatch, caplog
    ):
        """The normal shape of a bridge-to-MANTA-Link swap.

        Config carries `source`, so the objects compare unequal while the
        credential is identical, and "changed from a3f91c04 to a3f91c04" is
        confusing at the worst possible moment.
        """
        monkeypatch.setenv(config.TOKEN_ENV, TOKEN)
        watcher.reload()
        write_env(tmp_path, f"{config.TOKEN_ENV}={TOKEN}")

        with caplog.at_level("INFO"):
            watcher.reload()

        assert "the credential did not" in caplog.text
        assert "changed from" not in caplog.text


class TestConcurrentReloads:
    def test_two_health_threads_cannot_leave_the_config_and_session_disagreeing(
        self, tmp_path, watcher
    ):
        """health.py deliberately permits two live health threads.

        A slow read of this very file is the most likely cause of the second
        one, and two racing reloads leaving the watcher's config and the
        session's header describing different credentials is issue #10's exact
        failure, reintroduced by its own fix.
        """
        write_env(tmp_path, f"{config.TOKEN_ENV}={TOKEN}")
        watcher.reload()

        barrier = threading.Barrier(4)

        def rotate(which: int) -> None:
            barrier.wait()
            for _ in range(20):
                write_env(tmp_path, f"{config.TOKEN_ENV}={chr(97 + which) * 40}")
                watcher.reload()

        threads = [threading.Thread(target=rotate, args=(n,)) for n in range(3)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10.0)

        assert header(watcher) == f"Token {watcher.config.token}"

    def test_a_second_caller_skips_rather_than_waiting(self, tmp_path, watcher):
        watcher._lock.acquire()
        try:
            assert watcher.reload() is False
        finally:
            watcher._lock.release()


class TestTheEnvParser:
    def test_it_reads_a_plain_assignment(self):
        assert config.parse_env("A=1") == {"A": "1"}

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("# a comment\nA=1", {"A": "1"}),
            ("\n\n  \nA=1", {"A": "1"}),
            ("export A=1", {"A": "1"}),
            ("A=1\r\nB=2", {"A": "1", "B": "2"}),
            ("  A  =  1  ", {"A": "1"}),
            ('A="1"', {"A": "1"}),
            ("A='1'", {"A": "1"}),
            ("A=", {"A": ""}),
            ("A=b=c", {"A": "b=c"}),
        ],
    )
    def test_the_shapes_it_accepts(self, text, expected):
        assert config.parse_env(text) == expected

    @pytest.mark.parametrize("text", ["novalue", "=novalue", "   ", "#A=1"])
    def test_a_line_it_cannot_read_decides_nothing(self, text):
        # A malformed line in a credential file must not decide what the
        # credential is.
        assert config.parse_env(text) == {}

    def test_a_quote_on_one_side_only_is_kept(self):
        # Not a quoted value, so stripping one quote would change the secret.
        assert config.parse_env('A="1') == {"A": '"1'}
