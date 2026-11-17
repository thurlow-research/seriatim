#!/usr/bin/env python3
"""Mutation gate (SPEC REQ-61; TD-slice-1 Appendix B). Standard library only.

    mutation.py --base <sha>      run mutmut on the Python files changed since <sha>
    mutation.py --score <file>    only score an existing `mutmut export-cicd-stats` file

For each package directory (node/, and gateway/ from S7) that has changed Python files, it
sets `[tool.mutmut] only_mutate` in that package's pyproject.toml to those files, runs
`mutmut run` and `mutmut export-cicd-stats` there, and scores the export:

    score = (killed + timeout) / (total - skipped)        must be >= 75 %

Per-mutant timeouts count as killed (ruled). Survivors, mutants with no covering test,
suspicious mutants and segfaults count against the score. A missing, malformed or
unknown-format export, or one whose categories don't add up to its total (unchecked
mutants), fails the job.

If the changed files have no mutable code (a version-only change, say), mutmut generates no
mutants, exits non-zero and writes no export. The gate reads that from mutmut's per-file
metadata (an empty `exit_code_by_key` for every changed file) and passes with a notice. A
missing export or a failed run with mutants generated still fails.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

THRESHOLD = 0.75
PACKAGES = ("node", "gateway")
EXPORT = Path("mutants") / "mutmut-cicd-stats.json"
EXPECTED_KEYS = frozenset(
    {
        "killed",
        "survived",
        "total",
        "no_tests",
        "skipped",
        "suspicious",
        "timeout",
        "check_was_interrupted_by_user",
        "segfault",
    }
)
OUTCOMES = ("killed", "survived", "no_tests", "skipped", "suspicious", "timeout", "segfault")
ONLY_MUTATE_RE = re.compile(r"^only_mutate = \[.*\]$", re.M)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NOTHING_CHANGED = "::notice::no changed Python files under node/ or gateway/; nothing to mutate"


class GateError(Exception):
    """The gate fails; the message says why."""


def score(stats: object) -> tuple[float, int]:
    """Return (score, scored mutants) for one export, or raise GateError."""
    if not isinstance(stats, dict):
        raise GateError("the export is not a JSON object")
    keys = set(stats)
    if keys != EXPECTED_KEYS:
        raise GateError(
            "unknown export format: "
            f"missing {sorted(EXPECTED_KEYS - keys)}, unexpected {sorted(keys - EXPECTED_KEYS)}"
        )
    for k, v in stats.items():
        if type(v) is not int or v < 0:
            raise GateError(f"export field {k!r} is not a non-negative integer")
    if stats["check_was_interrupted_by_user"]:
        raise GateError("the mutation run was interrupted")
    if sum(stats[k] for k in OUTCOMES) != stats["total"]:
        raise GateError(
            f"{stats['total'] - sum(stats[k] for k in OUTCOMES)} of {stats['total']} mutants "
            "were not checked"
        )
    scored = stats["total"] - stats["skipped"]
    if scored == 0:
        return 1.0, 0
    return (stats["killed"] + stats["timeout"]) / scored, scored


def score_file(path: Path) -> tuple[float, int]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise GateError(f"no export at {path}: {e.strerror}") from None
    try:
        stats = json.loads(text)
    except json.JSONDecodeError as e:
        raise GateError(f"malformed export {path}: {e.msg}") from None
    return score(stats)


def changed_files(base: str) -> list[str]:
    """Python files under the package directories that changed since base (not deleted)."""
    if not SHA_RE.fullmatch(base):
        raise GateError("--base must be a 40-character commit SHA")
    specs = [f":(glob){p}/**/*.py" for p in PACKAGES]
    r = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=d", "-z", f"{base}...HEAD", "--", *specs],
        capture_output=True,
        check=False,
    )
    if r.returncode:
        raise GateError(f"git diff failed: {r.stderr.decode(errors='replace').strip()}")
    return sorted(p.decode() for p in r.stdout.split(b"\0") if p)


def set_only_mutate(pyproject: str, patterns: list[str]) -> str:
    """Replace the single `only_mutate = [...]` line of a [tool.mutmut] section."""
    if len(ONLY_MUTATE_RE.findall(pyproject)) != 1:
        raise GateError("pyproject.toml must have exactly one `only_mutate = [...]` line")
    line = "only_mutate = " + json.dumps(patterns)
    return ONLY_MUTATE_RE.sub(lambda _: line, pyproject)


def by_package(files: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in files:
        pkg, _, rest = f.partition("/")
        out.setdefault(pkg, []).append(rest)
    return out


@contextmanager
def staged(pdir: Path, root: Path = Path(".")) -> Iterator[None]:
    """Copy tests/ (and contract/) into the package directory for the run, then remove them.

    mutmut runs from the package directory and copies tests/ and contract/ from there into
    mutants/, next to the mutated code. Tests are then collected and re-run for each mutant
    from one root, so their node IDs resolve. Run from outside mutants/, a node ID doesn't
    resolve, pytest exits 4, and mutmut would count every mutant as killed.
    """
    copies = [(root / d, pdir / d) for d in ("tests", "contract") if (root / d).is_dir()]
    for _, dst in copies:
        if dst.exists():
            raise GateError(f"{dst} already exists; remove it before running the gate")
    try:
        for src, dst in copies:
            shutil.copytree(src, dst)
        yield
    finally:
        for _, dst in copies:
            shutil.rmtree(dst, ignore_errors=True)


def generated_mutants(pdir: Path, files: list[str]) -> int:
    """Count the mutants mutmut generated for files (relative to pdir, as by_package gives
    them), from its mutants/<file>.meta records."""
    total = 0
    for f in files:
        meta = pdir / "mutants" / f"{f}.meta"
        try:
            keys = json.loads(meta.read_text(encoding="utf-8"))["exit_code_by_key"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise GateError(f"no usable mutmut record for {f} ({type(e).__name__})") from e
        if not isinstance(keys, dict):
            raise GateError(f"unexpected mutmut record format for {f}")
        total += len(keys)
    return total


def run_package(pkg: str, files: list[str]) -> tuple[float, int]:
    pdir = Path(pkg)
    pyproject = pdir / "pyproject.toml"
    original = pyproject.read_bytes()
    pyproject.write_text(set_only_mutate(original.decode("utf-8"), files), encoding="utf-8")
    try:
        return _mutate(pkg, pdir, files)
    finally:
        pyproject.write_bytes(original)  # a local run leaves the clone as it found it


def _mutate(pkg: str, pdir: Path, files: list[str]) -> tuple[float, int]:
    print(f"mutating in {pkg}/: {', '.join(files)}", flush=True)
    with staged(pdir):
        r = subprocess.run(["uv", "run", "--frozen", "mutmut", "run"], cwd=pdir, check=False)
        if generated_mutants(pdir, files) == 0:
            return 0.0, 0  # nothing mutable: main() prints the notice
        if r.returncode:
            raise GateError(f"`mutmut run` in {pkg}/ exited {r.returncode}")
        r = subprocess.run(
            ["uv", "run", "--frozen", "mutmut", "export-cicd-stats"], cwd=pdir, check=False
        )
        if r.returncode:
            raise GateError(f"`mutmut export-cicd-stats` in {pkg}/ exited {r.returncode}")
    return score_file(pdir / EXPORT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--base", help="the commit to diff against")
    g.add_argument("--score", type=Path, help="score this export file only")
    a = ap.parse_args(argv)
    try:
        if a.score:
            results = {str(a.score): score_file(a.score)}
        else:
            files = changed_files(a.base)
            if not files:
                print(NOTHING_CHANGED)
                return 0
            results = {pkg: run_package(pkg, fs) for pkg, fs in by_package(files).items()}
    except GateError as e:
        print(f"::error::mutation gate: {e}")
        return 1
    ok = True
    for name, (value, scored) in results.items():
        if scored == 0:
            print(f"::notice::{name}: the changed files have no mutable code; nothing to score")
            continue
        verdict = "ok" if value >= THRESHOLD else "below 75 %"
        print(f"{name}: mutation score {value:.1%} of {scored} mutants ({verdict})")
        ok = ok and value >= THRESHOLD
    if not ok:
        print("::error::mutation score below 75 % (SPEC REQ-61)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
