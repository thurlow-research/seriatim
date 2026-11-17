#!/usr/bin/env python3
"""Release checks for release.yml (SPEC REQ-68, REQ-68a; #10). Standard library only.

    release_checks.py tag    --ref <github.ref> [--github-output <file>]
    release_checks.py source --ref <github.ref> --sha <github.sha> --main-ref <ref> --init <path>
    release_checks.py dist   --ref <github.ref> --sums <file> <dist dir>

Every check fails closed: anything it can't confirm is a failure.

- tag: the ref is a tag named exactly vX.Y.Z or vX.Y.Z-rc.N (ASCII digits, no leading zeros,
  N from 1). It writes tag, version (PEP 440: X.Y.Z or X.Y.ZrcN) and prerelease to the
  step's outputs.
- source: the tag names the commit being built, that commit is reachable from main, and the
  static __version__ in the package's __init__.py equals the tag's PEP 440 version. Nothing
  is rewritten at build time (#10, answer 8(a)).
- dist: the build produced exactly one wheel and one sdist of seriatim-node, both carrying the
  tag's version in their names and their metadata. It then writes SHA256SUMS for them.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from email.parser import HeaderParser
from pathlib import Path

DIST_NAME = "seriatim_node"
TAG_PREFIX = "refs/tags/"
# `uv build --out-dir` writes a .gitignore there. It is not an asset: upload-artifact leaves
# hidden files out, and so does the publish job's assets/* glob.
UV_GITIGNORE = ".gitignore"
# [0-9], not \d, which would also match non-ASCII digits.
NUM = r"(?:0|[1-9][0-9]*)"
TAG_RE = re.compile(rf"v(?P<core>{NUM}\.{NUM}\.{NUM})(?:-rc\.(?P<rc>[1-9][0-9]*))?")
SHA_RE = re.compile(r"[0-9a-f]{40}")


class CheckFailed(Exception):
    """A release check that did not pass."""


@dataclass(frozen=True)
class Tag:
    name: str
    pep440: str
    prerelease: bool


def parse_tag(name: str) -> Tag:
    m = TAG_RE.fullmatch(name)
    if m is None:
        raise CheckFailed(f"tag {name!r} is not vX.Y.Z or vX.Y.Z-rc.N")
    rc = m.group("rc")
    return Tag(name, m.group("core") + (f"rc{rc}" if rc else ""), rc is not None)


def parse_ref(ref: str) -> Tag:
    if not ref.startswith(TAG_PREFIX):
        raise CheckFailed(f"{ref!r} is not a tag ref")
    return parse_tag(ref.removeprefix(TAG_PREFIX))


def static_version(path: Path) -> str:
    """The value of the one top-level `__version__ = "<literal>"` in path."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (OSError, SyntaxError, ValueError) as exc:
        raise CheckFailed(f"cannot read {path}: {exc}") from exc
    found = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == "__version__" for t in targets):
            if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                raise CheckFailed(f"{path}: __version__ is not a string literal")
            found.append(node.value.value)
    if len(found) != 1:
        raise CheckFailed(f"{path}: expected one __version__ assignment, found {len(found)}")
    return found[0]


def check_version(tag: Tag, init: Path) -> None:
    version = static_version(init)
    if version != tag.pep440:
        raise CheckFailed(
            f"__version__ is {version!r} but tag {tag.name} needs {tag.pep440!r}; "
            "bump it by pull request before tagging"
        )


def git(cwd: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def resolve(cwd: Path | None, rev: str) -> str:
    r = git(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}")
    sha = r.stdout.strip()
    if r.returncode or not SHA_RE.fullmatch(sha):
        raise CheckFailed(f"{rev} does not resolve to a commit")
    return sha


def check_reachable(tag: Tag, sha: str, main_ref: str, cwd: Path | None = None) -> None:
    if not SHA_RE.fullmatch(sha):
        raise CheckFailed(f"{sha!r} is not a full commit SHA")
    tagged = resolve(cwd, TAG_PREFIX + tag.name)
    if tagged != sha:
        raise CheckFailed(f"tag {tag.name} names {tagged}, but this run builds {sha}")
    main = resolve(cwd, main_ref)
    r = git(cwd, "merge-base", "--is-ancestor", sha, main)
    if r.returncode == 1:
        raise CheckFailed(f"tag {tag.name} ({sha}) is not reachable from {main_ref}")
    if r.returncode:
        raise CheckFailed(f"git merge-base failed: {r.stderr.strip()}")


def metadata_version(raw: bytes, what: str) -> str:
    version = HeaderParser().parsestr(raw.decode("utf-8", errors="replace")).get("Version")
    if version is None:
        raise CheckFailed(f"{what} has no Version")
    return version.strip()


def wheel_version(path: Path, pep440: str) -> str:
    member = f"{DIST_NAME}-{pep440}.dist-info/METADATA"
    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read(member)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise CheckFailed(f"{path.name}: cannot read {member}: {exc}") from exc
    return metadata_version(raw, path.name)


def sdist_version(path: Path, pep440: str) -> str:
    member = f"{DIST_NAME}-{pep440}/PKG-INFO"
    try:
        with tarfile.open(path, "r:gz") as tf:
            fh = tf.extractfile(member)
            if fh is None:
                raise CheckFailed(f"{path.name}: {member} is not a file")
            raw = fh.read()
    except (OSError, KeyError, tarfile.TarError) as exc:
        raise CheckFailed(f"{path.name}: cannot read {member}: {exc}") from exc
    return metadata_version(raw, path.name)


def check_dist(tag: Tag, dist: Path) -> list[Path]:
    """The wheel and the sdist, after checking that they are all dist holds."""
    if not dist.is_dir():
        raise CheckFailed(f"{dist} is not a directory")
    files = sorted(p for p in dist.iterdir() if p.name != UV_GITIGNORE)
    wheels = [p for p in files if p.name.endswith(".whl")]
    sdists = [p for p in files if p.name.endswith(".tar.gz")]
    others = [p.name for p in files if p not in wheels and p not in sdists]
    if others:
        raise CheckFailed(f"unexpected files in {dist}: {', '.join(others)}")
    if len(wheels) != 1 or len(sdists) != 1:
        raise CheckFailed(
            f"expected one wheel and one sdist, found {len(wheels)} and {len(sdists)}"
        )
    wheel, sdist = wheels[0], sdists[0]
    stem = f"{DIST_NAME}-{tag.pep440}"
    if not wheel.name.startswith(stem + "-"):
        raise CheckFailed(f"wheel {wheel.name} is not {stem}-*.whl")
    if sdist.name != stem + ".tar.gz":
        raise CheckFailed(f"sdist {sdist.name} is not {stem}.tar.gz")
    versions = {wheel: wheel_version(wheel, tag.pep440), sdist: sdist_version(sdist, tag.pep440)}
    for path, version in versions.items():
        if version != tag.pep440:
            raise CheckFailed(f"{path.name} says version {version}, tag needs {tag.pep440}")
    return [wheel, sdist]


def sha256sums(paths: list[Path]) -> str:
    """The `sha256sum` format: "<hex>  <name>" per line, sorted by name."""
    lines = []
    for p in sorted(paths, key=lambda p: p.name):
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {p.name}\n")
    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_tag = sub.add_parser("tag")
    p_tag.add_argument("--ref", required=True)
    p_tag.add_argument("--github-output", type=Path)
    p_src = sub.add_parser("source")
    p_src.add_argument("--ref", required=True)
    p_src.add_argument("--sha", required=True)
    p_src.add_argument("--main-ref", required=True)
    p_src.add_argument("--init", required=True, type=Path)
    p_dist = sub.add_parser("dist")
    p_dist.add_argument("--ref", required=True)
    p_dist.add_argument("--sums", required=True, type=Path)
    p_dist.add_argument("dist", type=Path)
    args = ap.parse_args(argv)
    try:
        tag = parse_ref(args.ref)
        if args.cmd == "tag":
            outputs = (
                f"tag={tag.name}\nversion={tag.pep440}\n"
                f"prerelease={'true' if tag.prerelease else 'false'}\n"
            )
            print(outputs, end="")
            if args.github_output is not None:
                with args.github_output.open("a", encoding="utf-8") as fh:
                    fh.write(outputs)
        elif args.cmd == "source":
            check_reachable(tag, args.sha, args.main_ref)
            check_version(tag, args.init)
            print(f"release: {tag.name} is {args.sha}, on {args.main_ref}, version {tag.pep440}")
        else:
            paths = check_dist(tag, args.dist)
            args.sums.write_text(sha256sums(paths), encoding="utf-8")
            print(f"release: {', '.join(p.name for p in paths)} are version {tag.pep440}")
    except CheckFailed as exc:
        print(f"::error::release check failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
