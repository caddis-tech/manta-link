#!/usr/bin/env python3
"""Check the API's live certificate against the CA bundle boats actually ship.

A boat trusts exactly the certifi pinned in pyproject.toml and the Dockerfile,
frozen into its image. api.caddistech.com's certificate is Google-managed and
renews about every 60 days, and a renewal can arrive on a chain that bundle does
not know. Every boat on that image would then stop uploading at once, while the
certificate alerts in caddis-terraform stayed green: they validate against
Google's trust store, not the boats' (#29).

Run by .github/workflows/ca-bundle.yml, daily and whenever the pin changes:

    pip install "certifi==$(python tools/check_ca_bundle.py --pin)"
    python tools/check_ca_bundle.py --report report.md

Exits 1 when any host fails, after writing the report the workflow files as an
issue. Needs no credentials: it only makes outbound TLS connections.
"""

from __future__ import annotations

import argparse
import re
import socket
import ssl
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# api is the host boats post to. squadron is on the same load balancer with a
# certificate of its own that renews on a different date, so it can show a chain
# change before api's does. employee.caddistech.com joins once a certificate
# covers it: on 2026-10-21 it resolved there but was answered for other names.
HOSTS = ("api.caddistech.com", "squadron.caddistech.com")

PORT = 443
TIMEOUT_S = 15.0

# A dropped connection from a CI runner is not news about the certificate, so a
# connection failure is retried. A verification failure is never retried: it
# is the answer.
CONNECT_ATTEMPTS = 3
RETRY_DELAY_S = 10.0

_CHAIN_LINE_RE = re.compile(r"^\s*(\d+\s+s:|i:)")


@dataclass(frozen=True)
class Result:
    host: str
    ok: bool
    detail: str
    chain: str = ""


def pinned_certifi(pyproject: Path = PYPROJECT) -> str:
    """The certifi version boats ship, from the project's exact pin."""
    dependencies = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    for requirement in dependencies:
        name, separator, version = requirement.partition("==")
        if name.strip().lower() == "certifi" and separator:
            return version.strip()
    raise ValueError(f"no exact certifi pin (certifi==...) in {pyproject}")


def same_version(installed: str, pinned: str) -> bool:
    """certifi reports 2026.07.22 for the release pinned as 2026.7.22."""
    try:
        return [int(p) for p in installed.split(".")] == [
            int(p) for p in pinned.split(".")
        ]
    except ValueError:
        return installed == pinned


def verified_handshake(host: str, context: ssl.SSLContext) -> dict:
    """One verified handshake. Returns the leaf certificate as ssl decodes it."""
    with socket.create_connection((host, PORT), timeout=TIMEOUT_S) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert() or {}


def offered_chain(host: str) -> str:
    """The chain the server sent, read without verifying it, so a failure can
    say which issuer the bundle did not know."""
    try:
        listing = subprocess.run(
            ["openssl", "s_client", "-connect", f"{host}:{PORT}",
             "-servername", host, "-showcerts"],
            input=b"", capture_output=True, timeout=TIMEOUT_S, check=False,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(could not read the chain: {type(exc).__name__})"
    return chain_from_s_client(listing) or "(openssl printed no chain)"


def chain_from_s_client(listing: str) -> str:
    """The subject and issuer lines of `openssl s_client -showcerts` output."""
    return "\n".join(
        line.rstrip() for line in listing.splitlines() if _CHAIN_LINE_RE.match(line)
    )


def check_host(
    host: str,
    cafile: str,
    handshake: Callable[[str, ssl.SSLContext], dict] = verified_handshake,
    read_chain: Callable[[str], str] = offered_chain,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    context = ssl.create_default_context(cafile=cafile)
    failure = ""
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            leaf = handshake(host, context)
        except ssl.SSLCertVerificationError as exc:
            reason = exc.verify_message or str(exc)
            return Result(host, False, f"does not verify: {reason}", read_chain(host))
        except OSError as exc:
            failure = f"could not connect: {type(exc).__name__}: {exc}"
            if attempt < CONNECT_ATTEMPTS:
                sleep(RETRY_DELAY_S)
            continue
        return Result(host, True, f"verifies; issued by {issuer_of(leaf)}")
    return Result(host, False, failure)


def issuer_of(leaf: dict) -> str:
    fields = {key: value for rdn in leaf.get("issuer", ()) for key, value in rdn}
    issuer = ", ".join(
        fields[key] for key in ("organizationName", "commonName") if key in fields
    )
    issuer = issuer or "an unnamed issuer"
    expires = leaf.get("notAfter")
    return f"{issuer}, expires {expires}" if expires else issuer


def report(results: list[Result], pin: str) -> str:
    lines = [
        f"Checked against `certifi=={pin}`, the bundle pinned in `pyproject.toml` "
        "and the `Dockerfile`, which is what boats on the current image trust.",
        "",
        "| Host | Result |",
        "|---|---|",
    ]
    lines += [f"| `{r.host}` | {r.detail} |" for r in results]
    for result in results:
        if result.chain:
            lines += ["", f"Chain `{result.host}` offered:", ""]
            lines += ["```", result.chain, "```"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pin", action="store_true",
                        help="print the pinned certifi version and exit")
    parser.add_argument("--report", type=Path,
                        help="write a Markdown report here")
    args = parser.parse_args(argv)

    pin = pinned_certifi()
    if args.pin:
        print(pin)
        return 0

    # Imported here so --pin runs before certifi is installed.
    import certifi

    if not same_version(certifi.__version__, pin):
        # Checking against any other bundle would prove nothing about the boats.
        print(f"certifi {certifi.__version__} is installed but boats ship {pin}; "
              f'install exactly that first: pip install "certifi=={pin}"',
              file=sys.stderr)
        return 2

    results = [check_host(host, certifi.where()) for host in HOSTS]
    text = report(results, pin)
    print(text)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
