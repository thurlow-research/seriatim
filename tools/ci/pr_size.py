#!/usr/bin/env python3
"""PR-size check (SPEC REQ-63; ADR-001 §10). Standard library only.

    pr_size.py --base <sha> --head <sha>

At most 15 changed files and a diff of at most 150 KB (150,000 bytes), lock files excluded,
so that one reviewer can read the whole change.

The size is the text diff plus, for every binary file, the full size of its new version (or
of the old one, if the file is deleted). Git's binary patches are compressed, so a large but
compressible binary would otherwise count at a fraction of its size.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

MAX_FILES = 15
MAX_BYTES = 150_000
LOCK_FILES = frozenset({"uv.lock"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def is_lock(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in LOCK_FILES


def git(*args: str) -> bytes:
    r = subprocess.run(["git", *args], capture_output=True, check=False)
    if r.returncode:
        raise SystemExit(f"::error::git {args[0]} failed: {r.stderr.decode(errors='replace')}")
    return r.stdout


def binary_paths(rng: str) -> list[str]:
    """Paths whose change git treats as binary: numstat reports them as "-<TAB>-<TAB>path"."""
    out = git("diff", "--numstat", "-z", "--no-renames", "--no-textconv", rng)
    paths = []
    for row in out.split(b"\0"):
        added, _, rest = row.partition(b"\t")
        deleted, _, path = rest.partition(b"\t")
        if added == b"-" and deleted == b"-" and path:
            paths.append(path.decode())
    return paths


def blob_size(rev: str, path: str) -> int:
    r = subprocess.run(["git", "cat-file", "-s", f"{rev}:{path}"], capture_output=True, check=False)
    return int(r.stdout) if r.returncode == 0 else 0


def measure(base: str, head: str) -> tuple[list[str], int]:
    """Return (changed non-lock files, diff size in bytes) for base...head."""
    rng = f"{base}...{head}"
    names = [p.decode() for p in git("diff", "--name-only", "-z", rng).split(b"\0") if p]
    files = [p for p in names if not is_lock(p)]
    excludes = [f":(exclude,glob)**/{n}" for n in sorted(LOCK_FILES)]
    patch = git(
        "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", rng, "--", ".",
        *excludes,
    )  # fmt: skip
    merge_base = git("merge-base", base, head).decode().strip()
    binaries = sum(
        blob_size(head, p) or blob_size(merge_base, p) for p in binary_paths(rng) if not is_lock(p)
    )
    return files, len(patch) + binaries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    a = ap.parse_args(argv)
    if not (SHA_RE.fullmatch(a.base) and SHA_RE.fullmatch(a.head)):
        print("::error::--base and --head must be 40-character commit SHAs")
        return 1
    files, size = measure(a.base, a.head)
    print(f"{len(files)} changed files (limit {MAX_FILES}), diff {size:,} bytes (limit "
          f"{MAX_BYTES:,}); lock files excluded")  # fmt: skip
    ok = True
    if len(files) > MAX_FILES:
        print(f"::error::{len(files)} changed files; split the pull request (at most {MAX_FILES})")
        ok = False
    if size > MAX_BYTES:
        print(f"::error::the diff is {size:,} bytes; split the pull request (at most 150 KB)")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
