#!/usr/bin/env python3
"""Private-terms check (SPEC REQ-51, REQ-63). Standard library only.

Run by .github/workflows/private-terms.yml from main's checkout, never from a pull request.
It reads everything from the environment:

    PRIVATE_TERMS                 the term list (a CI secret), one term per line
    PRIVATE_TERMS_AGE_RECIPIENT   the maintainer's age recipient (a repository variable)
    AGE_BIN                       the pinned, checksum-verified age binary
    REPORT_PATH                   where the encrypted report is written
    REPO                          owner/name of the base repository (workflow context)
    EVENT                         pull_request_target or push
    BASE_SHA, HEAD_SHA            pull_request_target: the pull request's base and head
    PUSH_SHA, BEFORE              push: the pushed commit and the previous tip

Pull requests: everything the pull request introduces across its whole commit range: every
added or modified blob (merges compared with each parent), every path, and every commit's
raw object (message, author and committer). Pushes to main: the whole tree, every path, and
the pushed commits' raw objects. Matching ignores ASCII case and works on bytes.

Each non-blank line of PRIVATE_TERMS is one rule (SPEC REQ-63):

    myhost                       a plain substring
    =myhost                      a whole word: no ASCII letter or digit on either side
    myhost !! MyProduct myhost   an occurrence inside a listed phrase is suppressed
    =myhost !! a !! b            the two combine; any number of phrases

A malformed line (an empty term or phrase, or a phrase that can never cover the term) fails
the scan, like any other scan error.

Publicly it prints one fixed line, pass or fail, and nothing else. The match locations go
into a report padded to exactly 64 KiB and encrypted to the maintainer's age recipient. That
report is written on every run, so neither its presence nor its size says anything.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from typing import NamedTuple

GENERIC_FAIL = "Private-terms check failed. The maintainer can decrypt the report artifact."
PASS = "Private-terms check passed."
REPORT_SIZE = 64 * 1024
TRUNCATED = b"\n[report truncated to 64 KiB]\n"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
AGE_RECIPIENT_RE = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")
ZERO = "0" * 40
GITLINK = b"160000"
SEPARATOR = "!!"
MAX_OFFSETS = 5  # byte offsets listed per report line


class ScanError(Exception):
    """The scan could not complete. Never shown publicly."""


def git_env() -> dict[str, str]:
    """A neutral git environment: no system or global config, no prompts."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.gettempdir(),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


def git(gitdir: str, *args: str, stdin: bytes | None = None) -> bytes:
    cmd = ["git", "-c", "core.hooksPath=" + os.devnull, "--git-dir", gitdir, *args]
    r = subprocess.run(cmd, input=stdin, capture_output=True, env=git_env(), check=False)
    if r.returncode:
        raise ScanError(f"git {args[0]} exited {r.returncode}")
    return r.stdout


def fetch(gitdir: str, repo: str, shas: list[str]) -> None:
    """A fresh, complete bare repository holding exactly these commits."""
    git(gitdir, "init", "--bare", "--quiet")
    git(gitdir, "remote", "add", "origin", f"https://github.com/{repo}")
    git(gitdir, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "origin", *shas)
    if git(gitdir, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ScanError("the repository is shallow")
    for sha in shas:
        if git(gitdir, "cat-file", "-t", sha).strip() != b"commit":
            raise ScanError("not a commit")


def commit_range(gitdir: str, head: str, base: str | None) -> list[str]:
    args = ["rev-list", "--topo-order", head] + (["--not", base] if base else [])
    commits = git(gitdir, *args).decode().split()
    if not commits:
        raise ScanError("no commits to scan")
    return commits


def introduced(gitdir: str, commit: str) -> tuple[dict[str, bytes], list[bytes]]:
    """Blobs added or modified by commit (each parent of a merge), and every path listed."""
    out = git(
        gitdir, "diff-tree", "-r", "-z", "-m", "--no-renames", "--no-textconv",
        "--no-ext-diff", "--root", commit,
    )  # fmt: skip
    blobs: dict[str, bytes] = {}
    paths: list[bytes] = []
    fields = out.split(b"\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        if not field.startswith(b":"):  # a commit-id header line, or the final empty field
            i += 1
            continue
        meta = field[1:].split(b" ")
        if len(meta) != 5 or i + 1 >= len(fields):
            raise ScanError("unexpected diff-tree output")
        path = fields[i + 1]
        paths.append(path)
        new_mode, new_sha, status = meta[1], meta[3].decode(), meta[4][:1]
        if status in (b"A", b"M", b"T") and new_mode != GITLINK and new_sha != ZERO:
            blobs.setdefault(new_sha, path)
        i += 2
    return blobs, paths


def whole_tree(gitdir: str, commit: str) -> tuple[dict[str, bytes], list[bytes]]:
    out = git(gitdir, "ls-tree", "-r", "-z", "--full-tree", commit)
    blobs: dict[str, bytes] = {}
    paths: list[bytes] = []
    for entry in filter(None, out.split(b"\0")):
        meta, _, path = entry.partition(b"\t")
        mode, kind, sha = meta.split(b" ")
        paths.append(path)
        if kind == b"blob" and mode != GITLINK:
            blobs.setdefault(sha.decode(), path)
    return blobs, paths


def read_objects(gitdir: str, shas: list[str]) -> dict[str, bytes]:
    """Read objects as bytes through one `cat-file --batch`; anything missing fails."""
    if not shas:
        return {}
    out = git(gitdir, "cat-file", "--batch", stdin="".join(s + "\n" for s in shas).encode())
    objects: dict[str, bytes] = {}
    pos = 0
    for sha in shas:
        nl = out.find(b"\n", pos)
        if nl < 0:
            raise ScanError("short read")
        header = out[pos:nl].split(b" ")
        if len(header) != 3 or header[0].decode() != sha:
            raise ScanError("missing or unexpected object")
        size = int(header[2])
        body = out[nl + 1 : nl + 1 + size]
        if len(body) != size or out[nl + 1 + size : nl + 2 + size] != b"\n":
            raise ScanError("short read")
        objects[sha] = body
        pos = nl + 2 + size
    if pos != len(out):
        raise ScanError("unexpected trailing output")
    return objects


def collect(gitdir: str, commits: list[str], tree_of: str | None) -> list[tuple[str, bytes]]:
    """Everything to scan, as (location label, bytes)."""
    blobs: dict[str, bytes] = {}
    paths: list[bytes] = []
    for c in commits:
        b, p = introduced(gitdir, c)
        for sha, path in b.items():
            blobs.setdefault(sha, path)
        paths += p
    if tree_of:
        b, p = whole_tree(gitdir, tree_of)
        for sha, path in b.items():
            blobs.setdefault(sha, path)
        paths += p
    shas = sorted(blobs)
    contents = read_objects(gitdir, shas)
    raw_commits = read_objects(gitdir, commits)
    items = [(f"blob {s} ({blobs[s].decode(errors='replace')})", contents[s]) for s in shas]
    items += [(f"path {p.decode(errors='replace')}", p) for p in dict.fromkeys(paths)]
    items += [(f"commit {c} (message, author, committer)", raw_commits[c]) for c in commits]
    return items


class Rule(NamedTuple):
    """One line of the term list, lower-cased."""

    term: bytes
    whole_word: bool
    phrases: tuple[bytes, ...]

    def __str__(self) -> str:
        return ("=" if self.whole_word else "") + self.term.decode("utf-8", errors="replace")


def is_word_byte(b: int) -> bool:
    return 48 <= b <= 57 or 65 <= b <= 90 or 97 <= b <= 122  # an ASCII digit or letter


def occurrences(low: bytes, rule: Rule) -> list[int]:
    """Every start offset of rule.term in low (overlapping), whole words only if required."""
    found, n, i = [], len(rule.term), low.find(rule.term)
    while i >= 0:
        if not rule.whole_word or (
            (i == 0 or not is_word_byte(low[i - 1]))
            and (i + n == len(low) or not is_word_byte(low[i + n]))
        ):
            found.append(i)
        i = low.find(rule.term, i + 1)
    return found


def parse_terms(raw: str) -> list[Rule]:
    """The rules, deduplicated and sorted. A malformed line raises ScanError (line number only)."""
    rules = set()
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        head, *rest = (part.strip().encode("utf-8").lower() for part in line.split(SEPARATOR))
        whole_word = head.startswith(b"=")
        term = head[1:].strip() if whole_word else head
        if not term:
            raise ScanError(f"term list line {number}: empty term")
        if term.startswith(b"="):  # a doubled prefix would silently weaken the rule
            raise ScanError(f"term list line {number}: a term starts with '='")
        rule = Rule(term, whole_word, tuple(sorted(set(rest))))
        for phrase in rest:
            if not phrase:
                raise ScanError(f"term list line {number}: empty phrase")
            if phrase.startswith(b"="):
                raise ScanError(f"term list line {number}: a phrase starts with '='")
            if not occurrences(phrase, rule):
                raise ScanError(f"term list line {number}: a phrase does not contain the term")
        rules.add(rule)
    return sorted(rules)


def covered(low: bytes, start: int, rule: Rule) -> bytes | None:
    """The phrase whose occurrence in low contains the term occurrence at start, if any."""
    for phrase in rule.phrases:
        for k in occurrences(phrase, rule):  # where the term sits inside the phrase
            if k <= start and low[start - k : start - k + len(phrase)] == phrase:
                return phrase
    return None


def offsets(at: list[int]) -> str:
    more = f" and {len(at) - MAX_OFFSETS} more" if len(at) > MAX_OFFSETS else ""
    return ", ".join(map(str, at[:MAX_OFFSETS])) + more


def matches(items: Iterable[tuple[str, bytes]], rules: list[Rule]) -> tuple[list[str], list[str]]:
    """(matches, suppressed occurrences): one report line per item and rule (and phrase)."""
    found, suppressed = [], []
    for label, data in items:
        low = data.lower()
        for rule in rules:
            hits: list[int] = []
            by_phrase: dict[bytes, list[int]] = {}
            for i in occurrences(low, rule):
                phrase = covered(low, i, rule)
                if phrase is None:
                    hits.append(i)
                else:
                    by_phrase.setdefault(phrase, []).append(i)
            if hits:
                found.append(f"{label}: matches {str(rule)!r} at byte {offsets(hits)}")
            for phrase, at in by_phrase.items():
                inside = phrase.decode("utf-8", errors="replace")
                line = f"{label}: {str(rule)!r} at byte {offsets(at)}, inside {inside!r}"
                suppressed.append(line)
    return found, suppressed


def build_report(found: list[str], context: str, suppressed: list[str] | None = None) -> bytes:
    """The plaintext report, padded (or cut short, with a note) to exactly REPORT_SIZE.

    Suppressed occurrences are listed separately, so the maintainer can check the exceptions.
    """
    suppressed = suppressed or []
    lines = [f"private-terms report: {context}", f"{len(found)} match(es)", *found]
    lines += [f"{len(suppressed)} suppressed by an exception", *suppressed, ""]
    text = "\n".join(lines).encode("utf-8")
    if len(text) > REPORT_SIZE:
        text = text[: REPORT_SIZE - len(TRUNCATED)] + TRUNCATED
    return text + b" " * (REPORT_SIZE - len(text))


def encrypt(age_bin: str, recipient: str, plaintext: bytes) -> bytes:
    if not AGE_RECIPIENT_RE.fullmatch(recipient):
        raise ScanError("bad age recipient")
    r = subprocess.run(
        [age_bin, "--encrypt", "--recipient", recipient],
        input=plaintext,
        capture_output=True,
        check=False,
    )
    if r.returncode or not r.stdout:
        raise ScanError("encryption failed")
    return r.stdout


def scan(env: dict[str, str], gitdir: str) -> tuple[list[str], list[str], str]:
    """Fetch and scan; return (matches, suppressed, report context). Raises ScanError."""
    repo = env.get("REPO", "")
    if not REPO_RE.fullmatch(repo):
        raise ScanError("bad repository")
    event = env.get("EVENT", "")
    if event == "pull_request_target":
        base, head = env.get("BASE_SHA", ""), env.get("HEAD_SHA", "")
        if not (SHA_RE.fullmatch(base) and SHA_RE.fullmatch(head)):
            raise ScanError("bad base or head SHA")
        fetch(gitdir, repo, [base, head])
        commits, tree_of, context = commit_range(gitdir, head, base), None, f"{base}..{head}"
    elif event == "push":
        head, before = env.get("PUSH_SHA", ""), env.get("BEFORE", "")
        if not SHA_RE.fullmatch(head):
            raise ScanError("bad pushed SHA")
        prior = before if SHA_RE.fullmatch(before) and before != ZERO else None
        fetch(gitdir, repo, [head] + ([prior] if prior else []))
        commits, tree_of, context = commit_range(gitdir, head, prior), head, f"tree {head}"
    else:
        raise ScanError("unexpected event")
    terms = parse_terms(env.get("PRIVATE_TERMS", ""))
    if not terms:
        raise ScanError("the term list is unavailable")
    found, suppressed = matches(collect(gitdir, commits, tree_of), terms)
    return found, suppressed, context


def main() -> int:
    env = dict(os.environ)
    passed = False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                found, suppressed, context = scan(env, os.path.join(tmp, "repo.git"))
                passed = not found
            except ScanError as e:
                found, suppressed, context = [f"the scan did not complete: {e}"], [], "error"
            report = build_report(found, context, suppressed)
            ciphertext = encrypt(
                env.get("AGE_BIN", ""), env.get("PRIVATE_TERMS_AGE_RECIPIENT", ""), report
            )
            with open(env["REPORT_PATH"], "wb") as f:
                f.write(ciphertext)
    except BaseException:  # nothing about the failure may reach the log
        passed = False
    print(PASS if passed else GENERIC_FAIL, flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
