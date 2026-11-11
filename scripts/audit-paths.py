#!/usr/bin/env python3
"""Refuse absolute paths, routable addresses and contact details in the source.

A framework's source has no business naming a developer's home directory, a
machine on anyone's network, or a person. None of that is a secret in the sense
gitleaks looks for, which is exactly why it needs its own check: a path like
/home/someone/work/thing leaks a name and a working habit and no scanner built
for API keys will say a word about it.

What is allowed, and why, because a check nobody can satisfy gets deleted:

  * Loopback — 127.0.0.0/8 and ::1. A server binding loopback is the
    documented default and names no machine but the one running it.
  * The unspecified address — 0.0.0.0 and ::. Means "every interface", which
    is a configuration choice, not a location.
  * The documentation ranges of RFC 5737 and RFC 3849 — 192.0.2.0/24,
    198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32. Reserved precisely so that
    examples can use them.
  * Version numbers and the like that happen to have four dotted parts, which
    is why this parses candidates as addresses rather than trusting a regex.

Everything else is reported with its file and line, and the line itself is
never printed: if a finding is real, printing it puts the thing being
complained about into a build log that may be more public than the file was.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

# A home directory with a name after it. `/home/` alone is not interesting --
# it appears in prose about Linux -- so a following name is required.
HOME_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")
WINDOWS_PATH = re.compile(r"[A-Za-z]:\\\\?(?:Users|Documents)", re.IGNORECASE)

# Deliberately loose: the point is to catch a real address, and the parse below
# decides whether a match is one.
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")

# An address this project may name in its own source.
DOCUMENTATION_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("0.0.0.0/32"),       # every interface
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("192.0.2.0/24"),     # RFC 5737
    ipaddress.ip_network("198.51.100.0/24"),  # RFC 5737
    ipaddress.ip_network("203.0.113.0/24"),   # RFC 5737
    ipaddress.ip_network("::1/128"),          # loopback
    ipaddress.ip_network("::/128"),           # every interface
    ipaddress.ip_network("2001:db8::/32"),    # RFC 3849
]

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# example.com and friends are reserved for documentation by RFC 2606, and a
# .matrix or .nilhtml suffix in a template is not a top-level domain.
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")


def tracked_files(root: Path) -> list[Path]:
    listed = subprocess.run(["git", "ls-files"], cwd=root,
                            capture_output=True, text=True, check=True)
    return [root / line for line in listed.stdout.splitlines() if line.strip()]


def is_reportable_address(candidate: str) -> bool:
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        # Not an address at all -- a version number, a byte sequence, a time.
        return False
    return not any(address in network for network in DOCUMENTATION_NETWORKS)


def is_reportable_email(candidate: str) -> bool:
    domain = candidate.rsplit("@", 1)[-1].lower()
    if domain in ALLOWED_EMAIL_DOMAINS:
        return False
    # A bare suffix that is not a real TLD: "name@type.matrix" in prose.
    return not domain.endswith((".matrix", ".nilhtml", ".ny", ".nil"))


def audit(root: Path, skip: set[str]) -> list[str]:
    findings: list[str] = []
    for path in tracked_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in skip:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not text; gitleaks covers binary content
        for number, line in enumerate(body.splitlines(), start=1):
            for found in HOME_PATH.findall(line):
                findings.append(f"{relative}:{number}: a home directory path")
                break
            if WINDOWS_PATH.search(line):
                findings.append(f"{relative}:{number}: a Windows user path")
            for found in IPV4.findall(line) + IPV6.findall(line):
                if is_reportable_address(found):
                    findings.append(
                        f"{relative}:{number}: a routable address")
                    break
            for found in EMAIL.findall(line):
                if is_reportable_email(found):
                    findings.append(f"{relative}:{number}: an email address")
                    break
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--skip", action="append", default=[],
                        help="a tracked path to leave out, repeatable")
    args = parser.parse_args()

    root = args.root.resolve(strict=True)
    # This file names the patterns it looks for, so it would always find them.
    skip = set(args.skip) | {"scripts/audit-paths.py"}

    findings = audit(root, skip)
    if findings:
        print("Source audit failures (the matching text is deliberately not shown):")
        for finding in sorted(set(findings)):
            print(f"  - {finding}")
        return 1
    print("Source audit: no absolute paths, routable addresses or contact "
          "details in tracked text.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
