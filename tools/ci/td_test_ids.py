#!/usr/bin/env python3
"""TD test-ID check (SPEC REQ-60, REQ-63; issue #16). Standard library only.

    td_test_ids.py --junit <file> [--junit <file> ...]
    td_test_ids.py --check-needs

Every test scenario a slice's TD lists must be discharged by a test that ran and passed.

- **TD IDs.** Every `docs/design/TD-slice-<N>.md` is parsed strictly. Its test tables are the
  Markdown tables whose header's first cell is `id`; the first cell of every row must be an
  ID such as `C1`, `HL4` or `K-OAS1`. A table without its separator row, a row whose first
  cell isn't an ID, a TD with no test table, and an ID listed twice (in one TD or across TDs)
  all fail.
- **Enforcement.** Each TD carries exactly one line `Enforced: yes` or `Enforced: no` in its
  first 20 lines; a missing line or any other value fails. The TD's slice comes from its file
  name (`TD-slice-1.md` is S1) and must have a row in ADR-001 §10. For an enforced TD, every ID
  needs a passing test; for one that isn't, the job prints a notice.
- **Discharge by execution.** IDs are read from every JUnit file given: in CI, the ubuntu
  `test` leg's (`junit-test-ubuntu`) and the `ppe` job's (`junit-ppe`), from the same run.
  Tests declare IDs with `@pytest.mark.td("H14")`, and `tests/conftest.py` records them as
  `td` properties. Only a test case with no `failure`, `error` or `skipped` element discharges
  its IDs, so a skipped, xfailed or uncollected test leaves them unresolved. A missing, empty
  or malformed JUnit file fails; it never means "nothing to check", and that holds for each
  file, so a missing `ppe` report can't shrink the set of IDs read. A test that names an ID
  absent from every TD fails.
- **Fail closed on the jobs it reads** (`--check-needs`, and again in the main check). With
  `NEEDS` set (the workflow's `toJSON(needs)`), every job it lists must have succeeded: a
  failed, cancelled or skipped `test` or `ppe` fails this check, even while every TD says
  `Enforced: no`.
- **Obligations can't silently shrink** (pull requests only). Against the base commit:
  - every ID that disappears needs a PR-body line `TD-ID-REMOVED: <ID> - <reason>`;
  - a TD that main has as `Enforced: yes` and the PR turns to `no` or deletes needs a line
    `TD-ENFORCEMENT-OFF: TD-slice-<N> - <reason>`.
  The separator may be " - " or " — ". A base TD written before this check existed has no
  'Enforced:' line and counts as 'Enforced: no'. The human-proxy confirms each waiver at
  review. On a push to main both checks are skipped with a notice: main changes only by pull
  request, whose run enforced them, and a merge commit's first-parent diff would show the very
  removal the pull request waived.

Environment: EVENT (`pull_request` or `push`), BASE_SHA (pull requests), PR_BODY, NEEDS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

TD_DIR = Path("docs/design")
ADR = Path("docs/adr/ADR-001-architecture.md")
TD_NAME_RE = re.compile(r"^TD-slice-(?P<slice>[0-9]+[a-z]?)\.md$")
ID_RE = re.compile(r"^[A-Z]{1,4}(?:-[A-Z]{1,6})?[0-9]{1,3}[a-z]?$")
ENFORCED_RE = re.compile(r"^Enforced:(.*)$")
ENFORCED_WITHIN = 20
ADR_ROW_RE = re.compile(r"^\| \*\*(S[0-9]+[a-z]?)\*\*", re.M)
SEPARATOR_RE = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+\s*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
WAIVER_RE = re.compile(r"^(TD-ID-REMOVED|TD-ENFORCEMENT-OFF):\s*(\S+)\s+(?:-|—)\s+(\S.*)$")


class CheckError(Exception):
    """The check cannot run, or an input is malformed; the message says why."""


@dataclass
class TD:
    name: str  # "TD-slice-1"
    slice: str  # "S1"
    enforced: bool
    ids: dict[str, int] = field(default_factory=dict)  # id -> line number


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_td(filename: str, text: str, *, require_flag: bool = True) -> TD:
    """Parse one TD strictly. Raises CheckError naming the file and line.

    require_flag=False is for the base commit only: a TD written before this check existed
    has no 'Enforced:' line, and counts as 'Enforced: no'.
    """
    m = TD_NAME_RE.fullmatch(filename)
    if not m:
        raise CheckError(f"{filename}: not a TD-slice-<N>.md name")
    lines = text.splitlines()
    flags = [(i, ENFORCED_RE.match(ln)) for i, ln in enumerate(lines, 1)]
    flags = [(i, f) for i, f in flags if f]
    if not flags and not require_flag:
        line_no, value = 0, "no"
    elif len(flags) != 1:
        raise CheckError(f"{filename}: needs exactly one 'Enforced: yes|no' line, has {len(flags)}")
    else:
        line_no, value = flags[0][0], flags[0][1].group(1).strip()
    if line_no > ENFORCED_WITHIN or value not in ("yes", "no"):
        raise CheckError(
            f"{filename}:{line_no}: 'Enforced:' must be 'yes' or 'no' within the first "
            f"{ENFORCED_WITHIN} lines"
        )
    td = TD(filename.removesuffix(".md"), f"S{m.group('slice')}", value == "yes")
    i, tables = 0, 0
    while i < len(lines):
        if lines[i].startswith("|") and cells(lines[i])[0].lower() == "id":
            tables += 1
            if i + 1 >= len(lines) or not SEPARATOR_RE.match(lines[i + 1]):
                raise CheckError(f"{filename}:{i + 1}: test table has no separator row")
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                tid = cells(lines[i])[0]
                if not ID_RE.fullmatch(tid):
                    raise CheckError(f"{filename}:{i + 1}: {tid!r} is not a test ID")
                if tid in td.ids:
                    raise CheckError(f"{filename}:{i + 1}: {tid} is listed twice")
                td.ids[tid] = i + 1
                i += 1
            continue
        i += 1
    if not td.ids:
        raise CheckError(f"{filename}: no test table (a header row starting '| id |')")
    return td


def check_unique(tds: list[TD]) -> None:
    seen: dict[str, str] = {}
    for td in tds:
        for tid in td.ids:
            if tid in seen:
                raise CheckError(f"{tid} is listed in both {seen[tid]} and {td.name}")
            seen[tid] = td.name


def load_tds(td_dir: Path, adr: Path) -> list[TD]:
    files = sorted(td_dir.glob("TD-slice-*.md"))
    tds = [parse_td(p.name, p.read_text(encoding="utf-8")) for p in files]
    slices = set(ADR_ROW_RE.findall(adr.read_text(encoding="utf-8")))
    for td in tds:
        if td.slice not in slices:
            raise CheckError(f"{td.name}: slice {td.slice} has no row in ADR-001 §10")
    check_unique(tds)
    return tds


def git(*args: str) -> bytes:
    r = subprocess.run(["git", *args], capture_output=True, check=False)
    if r.returncode:
        raise CheckError(f"git {args[0]} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def load_base_tds(base: str, td_dir: Path) -> list[TD]:
    out = git("ls-tree", "-z", "--name-only", base, "--", f"{td_dir.as_posix()}/")
    names = [Path(p.decode()).name for p in out.split(b"\0") if p]
    tds = []
    for name in sorted(n for n in names if n.startswith("TD-slice-") and n.endswith(".md")):
        text = git("show", f"{base}:{td_dir.as_posix()}/{name}").decode("utf-8")
        try:
            tds.append(parse_td(name, text, require_flag=False))
        except CheckError as e:
            raise CheckError(f"the base commit's {e}") from e
    return tds


def junit_results(path: Path) -> tuple[dict[str, list[str]], dict[str, list[str]], int]:
    """Return ({id: passing tests}, {id: tests that didn't pass}, test-case count)."""
    try:
        # The file is this run's own JUnit output. Python's expat (2.4.1 and later) limits
        # entity expansion, and the job holds a read-only token.
        root = ET.parse(path).getroot()  # nosec B314 # own JUnit output, see above
    except (OSError, ET.ParseError) as e:
        raise CheckError(f"JUnit file {path}: {type(e).__name__}: {e}") from e
    if root.tag not in ("testsuites", "testsuite"):
        raise CheckError(f"JUnit file {path}: unexpected root element <{root.tag}>")
    passed: dict[str, list[str]] = {}
    other: dict[str, list[str]] = {}
    cases = list(root.iter("testcase"))
    if not cases:
        raise CheckError(f"JUnit file {path}: no test cases")
    for case in cases:
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        ok = not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
        for prop in case.iter("property"):
            if prop.get("name") == "td":
                (passed if ok else other).setdefault(prop.get("value", ""), []).append(name)
    return passed, other, len(cases)


def needs_errors(raw: str | None) -> list[str]:
    """Every job in the workflow's `needs` context that did not succeed."""
    if raw is None:
        return []
    try:
        needs = json.loads(raw)
        results = {job: v["result"] for job, v in needs.items()}
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise CheckError(f"NEEDS is not the workflow's needs context: {e}") from e
    if not results:
        raise CheckError("NEEDS lists no jobs")
    return [
        f"job {job} did not succeed ({result}), so its test results can't be read"
        for job, result in sorted(results.items())
        if result != "success"
    ]


def waivers(body: str) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {"TD-ID-REMOVED": {}, "TD-ENFORCEMENT-OFF": {}}
    for line in body.splitlines():
        m = WAIVER_RE.match(line.strip())
        if m:
            found[m.group(1)][m.group(2)] = m.group(3).strip()
    return found


def shrink_errors(base: list[TD], head: list[TD], body: str) -> list[str]:
    """Removed IDs and turned-off enforcement without a PR-body waiver."""
    w = waivers(body)
    errors = []
    head_ids = {tid for td in head for tid in td.ids}
    head_by_name = {td.name: td for td in head}
    for td in base:
        for tid in sorted(set(td.ids) - head_ids):
            if tid not in w["TD-ID-REMOVED"]:
                errors.append(
                    f"{tid} ({td.name}) was removed; add a PR-body line "
                    f"'TD-ID-REMOVED: {tid} - <reason>'"
                )
        now = head_by_name.get(td.name)
        if td.enforced and (now is None or not now.enforced):
            if td.name not in w["TD-ENFORCEMENT-OFF"]:
                errors.append(
                    f"{td.name} was 'Enforced: yes' on the base; add a PR-body line "
                    f"'TD-ENFORCEMENT-OFF: {td.name} - <reason>'"
                )
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--junit", type=Path, action="append", default=[])
    ap.add_argument("--check-needs", action="store_true")
    ap.add_argument("--td-dir", type=Path, default=TD_DIR)
    ap.add_argument("--adr", type=Path, default=ADR)
    a = ap.parse_args(argv)
    env = os.environ
    errors: list[str] = []
    try:
        needs = needs_errors(env.get("NEEDS"))
        if a.check_needs:
            for err in needs:
                print(f"::error::td-test-ids: {err}")
            if not needs:
                print("td-test-ids: every job it reads succeeded")
            return 1 if needs else 0
        if not a.junit:
            raise CheckError("give at least one --junit file")
        errors += needs
        tds = load_tds(a.td_dir, a.adr)
        passed, other, count = {}, {}, 0
        for path in a.junit:
            p, o, n = junit_results(path)
            for tid, tests in p.items():
                passed.setdefault(tid, []).extend(tests)
            for tid, tests in o.items():
                other.setdefault(tid, []).extend(tests)
            count += n
        known = {tid for td in tds for tid in td.ids}
        for tid in sorted((set(passed) | set(other)) - known):
            tests = ", ".join(passed.get(tid, []) + other.get(tid, []))
            errors.append(f"{tid} is named by a test but listed in no TD ({tests})")
        for td in tds:
            missing = sorted(set(td.ids) - set(passed))
            if not td.enforced:
                print(
                    f"::notice::{td.name} ({td.slice}) is 'Enforced: no': {len(td.ids)} IDs, "
                    f"{len(td.ids) - len(missing)} discharged; missing IDs fail only once "
                    "the TD says 'Enforced: yes'"
                )
                continue
            for tid in missing:
                why = f" (did not pass: {', '.join(other[tid])})" if tid in other else ""
                errors.append(f"{tid} ({td.name}) has no passing test{why}")
        event = env.get("EVENT", "")
        if event == "pull_request":
            base = env.get("BASE_SHA", "")
            if not SHA_RE.fullmatch(base):
                raise CheckError("BASE_SHA must be a 40-character commit SHA")
            errors += shrink_errors(load_base_tds(base, a.td_dir), tds, env.get("PR_BODY", ""))
        elif event == "push":
            print(
                "::notice::push to main: the removal and enforcement checks run on pull "
                "requests only (main changes only by pull request)"
            )
        else:
            raise CheckError(f"unexpected EVENT {event!r}")
    except CheckError as e:
        print(f"::error::td-test-ids: {e}")
        return 1
    for err in errors:
        print(f"::error::td-test-ids: {err}")
    if errors:
        return 1
    print(
        f"td-test-ids: {len(tds)} TD(s), {count} test cases read from {len(a.junit)} "
        "JUnit file(s); every check passed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
