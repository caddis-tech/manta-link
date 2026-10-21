"""The CA bundle check says when boats would stop trusting the API, and why.

Nothing here opens a connection: the handshake and the chain read are passed in.
A test suite never reaches a real service, and the live check is the
workflow's job, not the suite's.
"""

import ssl
import sys
from pathlib import Path

import certifi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import check_ca_bundle  # noqa: E402
from tools.check_ca_bundle import Result, check_host, pinned_certifi  # noqa: E402

# A real bundle, because ssl reads it; nothing here connects with it.
BUNDLE = certifi.where()

LEAF = {
    "issuer": (
        (("countryName", "US"),),
        (("organizationName", "Google Trust Services"),),
        (("commonName", "WR3"),),
    ),
    "notAfter": "Dec 20 19:49:43 2026 GMT",
}

# `openssl s_client -showcerts` against api.caddistech.com on 2026-10-21, with
# the PEM blocks and the handshake summary cut down to a line each.
S_CLIENT = """\
CONNECTED(00000003)
---
Certificate chain
 0 s:CN=api.caddistech.com
   i:C=US, O=Google Trust Services, CN=WR3
   a:PKEY: EC, (prime256v1); sigalg: ecdsa-with-SHA256
-----BEGIN CERTIFICATE-----
MIIE...
-----END CERTIFICATE-----
 1 s:C=US, O=Google Trust Services, CN=WR3
   i:C=US, O=Google Trust Services LLC, CN=GTS Root R1
-----BEGIN CERTIFICATE-----
MIIF...
-----END CERTIFICATE-----
---
Server certificate
subject=CN=api.caddistech.com
issuer=C=US, O=Google Trust Services, CN=WR3
"""


def untrusted(_host, _context):
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error.verify_message = "unable to get local issuer certificate"
    raise error


def never_reads_a_chain(_host):
    raise AssertionError("a host that verified has no chain to report")


class TestThePin:
    def test_it_is_read_from_the_exact_requirement(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["requests==2.34.2", "certifi==2026.7.22"]\n'
        )

        assert pinned_certifi(pyproject) == "2026.7.22"

    def test_a_missing_pin_is_an_error_not_a_guess(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["certifi>=2026.1.1"]\n')

        with pytest.raises(ValueError, match="no exact certifi pin"):
            pinned_certifi(pyproject)

    def test_the_repos_own_pin_is_readable(self):
        assert pinned_certifi()

    @pytest.mark.parametrize(("installed", "pinned", "same"), [
        ("2026.07.22", "2026.7.22", True),
        ("2026.7.22", "2026.7.22", True),
        ("2026.08.30", "2026.7.22", False),
    ])
    def test_certifis_zero_padded_version_still_matches_the_pin(
        self, installed, pinned, same
    ):
        assert check_ca_bundle.same_version(installed, pinned) is same


class TestOneHost:
    def test_a_host_that_verifies_names_its_issuer(self):
        result = check_host("api.test", BUNDLE,
                            handshake=lambda *_: LEAF, read_chain=never_reads_a_chain)

        assert result.ok
        assert "Google Trust Services, WR3" in result.detail
        assert "Dec 20 19:49:43 2026 GMT" in result.detail

    def test_an_untrusted_chain_fails_with_the_reason_and_the_chain(self):
        result = check_host("api.test", BUNDLE,
                            handshake=untrusted, read_chain=lambda _h: "0 s:CN=x")

        assert not result.ok
        assert "unable to get local issuer certificate" in result.detail
        assert result.chain == "0 s:CN=x"

    def test_an_untrusted_chain_is_not_retried(self):
        calls = []

        def counted(host, context):
            calls.append(host)
            untrusted(host, context)

        check_host("api.test", BUNDLE, handshake=counted,
                   read_chain=lambda _h: "", sleep=lambda _s: None)

        assert len(calls) == 1

    def test_a_dropped_connection_is_retried_before_it_fails(self):
        calls = []

        def drops(host, _context):
            calls.append(host)
            raise ConnectionResetError("reset by peer")

        result = check_host("api.test", BUNDLE, handshake=drops,
                            read_chain=never_reads_a_chain, sleep=lambda _s: None)

        assert len(calls) == check_ca_bundle.CONNECT_ATTEMPTS
        assert not result.ok
        assert result.detail.startswith("could not connect: ConnectionResetError")

    def test_a_connection_that_recovers_is_a_pass(self):
        attempts = iter([ConnectionResetError("reset"), None])

        def flaky(_host, _context):
            failure = next(attempts)
            if failure:
                raise failure
            return LEAF

        result = check_host("api.test", BUNDLE, handshake=flaky,
                            read_chain=never_reads_a_chain, sleep=lambda _s: None)

        assert result.ok


class TestTheReport:
    def test_the_offered_chain_is_read_out_of_s_client(self):
        assert check_ca_bundle.chain_from_s_client(S_CLIENT).splitlines() == [
            " 0 s:CN=api.caddistech.com",
            "   i:C=US, O=Google Trust Services, CN=WR3",
            " 1 s:C=US, O=Google Trust Services, CN=WR3",
            "   i:C=US, O=Google Trust Services LLC, CN=GTS Root R1",
        ]

    def test_a_failure_names_the_host_the_chain_and_the_pin(self):
        text = check_ca_bundle.report([
            Result("api.caddistech.com", False, "does not verify: no issuer",
                   " 0 s:CN=api.caddistech.com\n   i:CN=Some New CA"),
            Result("squadron.caddistech.com", True, "verifies; issued by GTS"),
        ], pin="2026.7.22")

        assert "certifi==2026.7.22" in text
        assert "| `api.caddistech.com` | does not verify: no issuer |" in text
        assert "i:CN=Some New CA" in text
        assert "| `squadron.caddistech.com` | verifies; issued by GTS |" in text


class TestTheRun:
    @pytest.fixture
    def pinned(self, monkeypatch):
        """certifi as the pin names it, wherever this suite happens to run."""
        monkeypatch.setattr(certifi, "__version__", pinned_certifi())

    def test_any_failing_host_fails_the_run_and_writes_the_report(
        self, monkeypatch, tmp_path, pinned
    ):
        monkeypatch.setattr(check_ca_bundle, "check_host", lambda host, _ca: Result(
            host, host != "api.caddistech.com", "detail"))
        written = tmp_path / "report.md"

        assert check_ca_bundle.main(["--report", str(written)]) == 1
        assert "`api.caddistech.com`" in written.read_text()

    def test_every_host_verifying_passes(self, monkeypatch, pinned):
        monkeypatch.setattr(check_ca_bundle, "check_host",
                            lambda host, _ca: Result(host, True, "verifies"))

        assert check_ca_bundle.main([]) == 0

    def test_it_refuses_to_check_against_any_bundle_but_the_pin(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(certifi, "__version__", "1999.1.1")
        monkeypatch.setattr(check_ca_bundle, "check_host", never_reads_a_chain)

        assert check_ca_bundle.main([]) == 2
        assert "boats ship" in capsys.readouterr().err

    def test_pin_prints_the_version_for_the_workflows_install(self, capsys):
        assert check_ca_bundle.main(["--pin"]) == 0
        assert capsys.readouterr().out.strip() == pinned_certifi()
