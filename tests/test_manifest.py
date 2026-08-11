"""The manifest is only validated by a real boat install, so validate it here.

Kraken refuses a manifest it cannot parse and gives no reason, which makes a
malformed LABEL an expensive thing to discover.
"""

import json
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import manifest  # noqa: E402


@pytest.fixture(scope="module")
def labels():
    return manifest.load()


def dockerignore_patterns() -> list[str]:
    """The real patterns, with comments and blank lines dropped.

    Parsed rather than substring-matched because the file's own comment explains
    why the token must not reach the builder, and so contains the very string a
    naive check would look for.
    """
    text = (manifest.DOCKERFILE.parent / ".dockerignore").read_text(encoding="utf-8")
    return [
        stripped
        for line in text.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]


class TestRealDockerfile:
    def test_manifest_has_no_problems(self, labels):
        assert manifest.check(labels) == []

    def test_version_matches_the_package(self, labels):
        from manta_link import __version__

        assert labels["version"] == __version__

    def test_version_matches_pyproject(self, labels):
        # The other two copies are both pinned: this test covers the package,
        # and tools/manifest.py compares the LABEL to the git tag on a release.
        # pyproject is the copy nothing reads, so it can drift to a wrong
        # version and every gate including the release still passes.
        pyproject = tomllib.loads(
            (manifest.DOCKERFILE.parent / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert pyproject["project"]["version"] == labels["version"]

    @pytest.mark.parametrize("key", manifest.JSON_LABELS)
    def test_json_labels_parse(self, labels, key):
        json.loads(labels[key])

    def test_identifier_and_links_point_at_manta_link(self, labels):
        assert "manta-link" in labels["links"]
        assert "manta-link" in labels["readme"]

    def test_binds_serial_and_the_data_volume_and_media(self, labels):
        binds = json.loads(labels["permissions"])["HostConfig"]["Binds"]
        assert any(b.startswith("/dev:/dev") for b in binds)
        assert any("/app/data" in b for b in binds)
        # Shipped before any USB stick exists, because a bind established at
        # container start cannot see a device mounted later without it.
        assert any(b.startswith("/media:/media") for b in binds)

    def test_media_bind_carries_slave_propagation(self, labels):
        binds = json.loads(labels["permissions"])["HostConfig"]["Binds"]
        media = next(b for b in binds if b.startswith("/media:"))
        assert "rslave" in media

    def test_host_networking_for_mavlink2rest_and_the_upload_route(self, labels):
        host = json.loads(labels["permissions"])["HostConfig"]
        assert host["NetworkMode"] == "host"

    def test_restart_policy_is_set(self, labels):
        host = json.loads(labels["permissions"])["HostConfig"]
        assert host["RestartPolicy"]["Name"] == "unless-stopped"

    def test_no_extrahosts(self, labels):
        host = json.loads(labels["permissions"])["HostConfig"]
        assert "ExtraHosts" not in host

    def test_no_healthcheck_in_the_dockerfile(self):
        text = manifest.DOCKERFILE.read_text(encoding="utf-8")
        # A freshness-based check restarts the container on a flight boat, where
        # silence is correct, and a restart can lose an in-flight TIME?.
        assert "HEALTHCHECK" not in text.replace("# No HEALTHCHECK", "")

    def test_unbuffered_output(self):
        text = manifest.DOCKERFILE.read_text(encoding="utf-8")
        assert "PYTHONUNBUFFERED=1" in text

    def test_the_build_context_excludes_the_env_file(self):
        # Everything in the context is uploaded to the builder before the first
        # instruction runs, and a bench run leaves a real token in a .env beside
        # this Dockerfile. COPY is narrow enough that it never reaches the
        # image; this keeps it off the builder as well.
        assert ".env" in dockerignore_patterns()

    def test_the_build_context_excludes_the_virtualenv(self):
        assert ".venv/" in dockerignore_patterns()


class TestParser:
    def test_joins_continuations(self):
        text = 'LABEL permissions=\'{\\\n  "a": 1\\\n}\'\n'
        assert json.loads(manifest.parse_labels(text)["permissions"]) == {"a": 1}

    def test_strips_matching_quotes_only(self):
        parsed = manifest.parse_labels('LABEL version="1.2.3"\nLABEL type=other\n')
        assert parsed["version"] == "1.2.3"
        assert parsed["type"] == "other"

    def test_ignores_non_label_lines(self):
        parsed = manifest.parse_labels("FROM python\nRUN pip install x\n")
        assert parsed == {}


class TestChecks:
    BASE = {
        "version": "1.0.0",
        "permissions": json.dumps(
            {"HostConfig": {"Privileged": True, "Binds": ["/dev:/dev:rw"]}}
        ),
    }

    def test_accepts_a_sound_manifest(self):
        assert manifest.check(dict(self.BASE)) == []

    def test_rejects_malformed_json(self):
        broken = dict(self.BASE, permissions='{"HostConfig": {,}}')
        assert any("not valid JSON" in p for p in manifest.check(broken))

    def test_rejects_a_non_semver_version(self):
        problems = manifest.check(dict(self.BASE, version="1.0"))
        assert any("SemVer" in p for p in problems)

    def test_rejects_a_version_that_disagrees_with_the_tag(self):
        problems = manifest.check(dict(self.BASE), expected_version="2.0.0")
        assert any("does not match the release tag" in p for p in problems)

    def test_accepts_a_version_matching_the_tag(self):
        assert manifest.check(dict(self.BASE), expected_version="1.0.0") == []

    def test_rejects_a_missing_dev_bind(self):
        no_dev = dict(self.BASE, permissions=json.dumps(
            {"HostConfig": {"Privileged": True, "Binds": ["/media:/media"]}}
        ))
        assert any("no serial access" in p for p in manifest.check(no_dev))

    def test_rejects_dropping_privileged(self):
        unprivileged = dict(self.BASE, permissions=json.dumps(
            {"HostConfig": {"Privileged": False, "Binds": ["/dev:/dev:rw"]}}
        ))
        assert any("not Privileged" in p for p in manifest.check(unprivileged))

    def test_rejects_a_logconfig_kraken_would_discard(self):
        with_log = dict(self.BASE, permissions=json.dumps({
            "HostConfig": {
                "Privileged": True,
                "Binds": ["/dev:/dev:rw"],
                "LogConfig": {"Type": "json-file", "Config": {"mode": "non-blocking"}},
            }
        }))
        assert any("overwrites" in p for p in manifest.check(with_log))

    def test_reports_missing_required_labels(self):
        assert any("missing required LABEL" in p for p in manifest.check({}))


def test_cli_accepts_a_v_prefixed_tag():
    from manta_link import __version__

    assert manifest.main(["manifest.py", f"v{__version__}"]) == 0


def test_cli_rejects_a_mismatched_tag():
    assert manifest.main(["manifest.py", "v99.0.0"]) == 1


def test_cli_skips_the_version_gate_on_a_manual_dispatch():
    assert manifest.main(["manifest.py", manifest.NO_TAG_GATE]) == 0


def test_cli_still_gates_a_tag_push_while_the_skip_token_exists():
    assert manifest.main(["manifest.py", manifest.NO_TAG_GATE]) == 0
    assert manifest.main(["manifest.py", "v99.0.0"]) == 1


def test_cli_refuses_an_empty_tag_rather_than_skipping_the_gate(capsys):
    assert manifest.main(["manifest.py", ""]) == 1
    assert manifest.NO_TAG_GATE in capsys.readouterr().err
