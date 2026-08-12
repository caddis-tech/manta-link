#!/usr/bin/env python3
"""Read and validate the BlueOS extension manifest out of the Dockerfile.

Kraken refuses a manifest it cannot parse and does not report why: the install
simply fails, looking like a missing tag or a bad image. That failure is only
discoverable on a boat, so it gets caught here instead.

Usable two ways: imported by the test suite, or run in CI with the release tag
as its argument to assert the version LABEL agrees with it. A run with no tag to
agree with passes NO_TAG_GATE to check everything else.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"

# Labels whose values must be valid JSON. Kraken parses these; a stray comma
# here is an install failure with no message on the boat.
JSON_LABELS = ("permissions", "authors", "company", "tags", "links")

REQUIRED_LABELS = ("version", "permissions")

_LABEL_RE = re.compile(r"^LABEL\s+(?P<key>[a-zA-Z_][\w.-]*)=(?P<value>.*)$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Passed instead of a tag by a workflow_dispatch run, which has no tag for the
# version to agree with. The publish workflow spells this literally; keep them
# in step.
NO_TAG_GATE = "--no-tag"


def join_continuations(text: str) -> str:
    """Collapse Dockerfile backslash line continuations into single lines."""
    return re.sub(r"\\\r?\n", "", text)


def parse_labels(text: str) -> dict[str, str]:
    """Every LABEL in a Dockerfile, as key to raw (unquoted) value."""
    labels: dict[str, str] = {}
    for line in join_continuations(text).splitlines():
        match = _LABEL_RE.match(line.strip())
        if match is None:
            continue
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        labels[match.group("key")] = value
    return labels


def load() -> dict[str, str]:
    return parse_labels(DOCKERFILE.read_text(encoding="utf-8"))


def check(labels: dict[str, str], expected_version: str | None = None) -> list[str]:
    """Problems with the manifest, empty if it is sound."""
    problems: list[str] = []

    for key in REQUIRED_LABELS:
        if key not in labels:
            problems.append(f"missing required LABEL {key}")

    for key in JSON_LABELS:
        if key not in labels:
            continue
        try:
            json.loads(labels[key])
        except json.JSONDecodeError as exc:
            problems.append(f"LABEL {key} is not valid JSON: {exc}")

    version = labels.get("version", "")
    if version and not _SEMVER_RE.match(version):
        problems.append(f"LABEL version {version!r} is not SemVer")

    if expected_version is not None and version != expected_version:
        problems.append(
            f"LABEL version {version!r} does not match the release tag "
            f"{expected_version!r}"
        )

    problems.extend(_check_permissions(labels.get("permissions", "")))
    return problems


def _check_permissions(raw: str) -> list[str]:
    """Shape checks on the one label that decides whether the boat works."""
    if not raw:
        return []
    try:
        permissions = json.loads(raw)
    except json.JSONDecodeError:
        return []  # already reported

    host = permissions.get("HostConfig")
    if not isinstance(host, dict):
        return ["permissions has no HostConfig object"]

    problems = []
    binds = host.get("Binds", [])

    # Losing this bind is the failure that looks healthy: the container starts,
    # reports nothing wrong, and can never see the Pico.
    if not any(b.startswith("/dev:/dev") for b in binds):
        problems.append("permissions does not bind /dev, so no serial access")
    if not host.get("Privileged"):
        problems.append("permissions is not Privileged, so no serial access")

    # Kraken discards any LogConfig here, so one being present means somebody
    # believes it is doing something.
    if "LogConfig" in host:
        problems.append(
            "permissions sets LogConfig, which Kraken overwrites unconditionally"
        )
    return problems


def main(argv: list[str]) -> int:
    expected: str | None = None
    if len(argv) > 1:
        tag = argv[1].strip()
        # Only this exact token skips the gate. An empty argument is a broken
        # caller, and treating it as "no tag" would silently publish a version
        # nothing ever checked.
        if not tag:
            print(
                f"manifest: empty tag argument; pass {NO_TAG_GATE} to skip the "
                "version gate deliberately",
                file=sys.stderr,
            )
            return 1
        expected = None if tag == NO_TAG_GATE else tag.lstrip("v")

    labels = load()
    problems = check(labels, expected)

    if problems:
        for problem in problems:
            print(f"manifest: {problem}", file=sys.stderr)
        return 1

    gate = f"matches tag {expected}" if expected else "no tag to check against"
    print(f"manifest OK: version {labels['version']}, "
          f"{len(labels)} labels, all JSON labels parse, {gate}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
