#!/usr/bin/env python3
"""IP-literal check (SPEC REQ-51, REQ-63). Standard library only.

Scans every tracked file for IPv4 and IPv6 address literals. The only addresses allowed are
loopback (127.0.0.0/8 and ::1), 0.0.0.0, and the RFC 5737 documentation ranges
(192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24). Anything else fails the check.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys

ALLOWED_V4 = [
    ipaddress.ip_network(n)
    for n in ("127.0.0.0/8", "0.0.0.0/32", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
]
# Dotted quads, not inside a longer run of digits and dots (a trailing sentence dot is fine).
V4_RE = re.compile(rb"(?<![\d.])(?<!\d\.)(\d{1,3}(?:\.\d{1,3}){3})(?!\d|\.\d)")
# Hex-and-colon runs not touching a word character or another colon; checked by ipaddress.
V6_RE = re.compile(rb"(?<![\w:.])([0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*)(?![\w:])")


def allowed(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(addr, ipaddress.IPv6Address):
        return addr == ipaddress.IPv6Address("::1")
    return any(addr in n for n in ALLOWED_V4)


def find(data: bytes) -> list[tuple[int, str]]:
    """Return (line number, literal) for every disallowed address literal in data."""
    found: list[tuple[int, str]] = []
    for regex in (V4_RE, V6_RE):
        for m in regex.finditer(data):
            text = m.group(1).decode("ascii")
            if regex is V6_RE and not re.search(r"[0-9A-Fa-f]", text):
                continue  # a bare "::" is punctuation, not an address
            try:
                addr = ipaddress.ip_address(text)
            except ValueError:
                continue
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
                continue  # its IPv4 part is checked by the IPv4 pattern
            if not allowed(addr):
                found.append((data.count(b"\n", 0, m.start()) + 1, text))
    return sorted(found)


def tracked_files() -> list[str]:
    r = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
    return [p.decode() for p in r.stdout.split(b"\0") if p]


def main() -> int:
    bad = 0
    for path in tracked_files():
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            print(f"::error file={path}::cannot read: {e.strerror}")
            bad += 1
            continue
        for line, literal in find(data):
            print(f"::error file={path},line={line}::IP literal {literal} is not allowed (REQ-51)")
            bad += 1
    if bad:
        print(
            f"{bad} disallowed IP literal(s). Allowed: loopback, 0.0.0.0 and the RFC 5737 "
            "documentation ranges."
        )
        return 1
    print("no disallowed IP literals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
