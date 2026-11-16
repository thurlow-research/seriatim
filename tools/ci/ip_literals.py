#!/usr/bin/env python3
"""IP-literal check (SPEC REQ-51, REQ-63). Standard library only.

Scans every tracked file for IPv4 and IPv6 address literals. The only addresses allowed are
loopback (127.0.0.0/8 and ::1), 0.0.0.0, and the RFC 5737 documentation ranges
(192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24). An IPv4-mapped IPv6 address (in ::ffff:0:0/96,
dotted or hex) is judged by the IPv4 address it maps to. Every other IPv6 address fails,
the IPv6 documentation prefix (RFC 3849) included. A bare "::" is not an address.
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
        if addr.ipv4_mapped is not None:  # ::ffff:a.b.c.d, in dotted or hex form
            return allowed(addr.ipv4_mapped)
        return addr == ipaddress.IPv6Address("::1")
    return any(addr in n for n in ALLOWED_V4)


def find(data: bytes) -> list[tuple[int, str]]:
    """Return (line number, literal) for every disallowed address literal in data.

    IPv6 literals are matched first. A dotted quad inside one (::ffff:a.b.c.d) belongs to
    it, and isn't reported again on its own.
    """
    found: list[tuple[int, str]] = []
    v6_spans: list[tuple[int, int]] = []
    for m in V6_RE.finditer(data):
        text = m.group(1).decode("ascii")
        if not re.search(r"[0-9A-Fa-f]", text):
            continue  # a bare "::" is punctuation, not an address
        addr = None
        while text:
            try:
                addr = ipaddress.IPv6Address(text)
                break
            except ValueError:
                if text[-1] not in ".:":
                    break
                text = text[:-1]  # sentence punctuation after an address
        if addr is None:
            continue
        v6_spans.append((m.start(1), m.start(1) + len(text)))
        if not allowed(addr):
            found.append((data.count(b"\n", 0, m.start()) + 1, text))
    for m in V4_RE.finditer(data):
        if any(a <= m.start(1) and m.end(1) <= b for a, b in v6_spans):
            continue
        text = m.group(1).decode("ascii")
        try:
            addr4 = ipaddress.IPv4Address(text)
        except ValueError:
            continue
        if not allowed(addr4):
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
