#!/usr/bin/env python3
"""Security-scanner exceptions (SPEC REQ-63). Standard library only.

    security_ignore.py lint                     check the ignore file and every `# nosec`
    security_ignore.py exec <tool> -- <cmd...>  run a scanner with its ignore arguments added
    security_ignore.py bandit-report <json>     pass or fail on Bandit's JSON output

**The ignore file** is `.security-ignore.toml`. Each exception is an `[[ignore]]` table:

    [[ignore]]
    tool = "pip-audit"          # bandit, pip-audit or trivy
    id = "GHSA-xxxx-xxxx-xxxx"  # the finding's id, as the tool reports it
    reason = "why it doesn't apply here, or what is being done about it"
    expires = 2027-01-31        # a TOML date; the entry fails CI from the day after

An entry with a missing, empty or unknown field, an unknown tool, a malformed id, an expiry
that isn't a TOML date, or an expiry in the past fails the check. So does any other key.

**Inline suppressions** need known test ids and a reason on the same line, after a second `#`:

    call()  # nosec B603 # the arguments are a fixed list
    call()  # nosec B603, B607 # the same, for two tests

Bandit reads the test ids up to the second `#`, so the reason never reaches it as a test
name. The lint finds suppressions exactly as Bandit 1.9.4 does (its regexes are copied below),
so a form Bandit accepts, such as `nosecB602`, or `nosec` inside a longer word, can't slip
past. It then fails on anything but the form above, and on any test id Bandit doesn't know:
Bandit drops unknown ids, and a suppression left with no known id silences every finding on
its line. Only real comments count, read with `tokenize`; a string that contains the text
doesn't.

**exec** checks the ignore file first, so an expired exception fails every scanner job too.
Then it runs the command with the tool's exceptions added: Bandit `--skip`, pip-audit
`--ignore-vuln`, and Trivy `--ignorefile` (a file written for the run).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tokenize
import tomllib
from pathlib import Path

IGNORE_FILE = Path(".security-ignore.toml")
TOOLS = ("bandit", "pip-audit", "trivy")
ID_RE = {
    "bandit": re.compile(r"^B[0-9]{3}$"),
    "pip-audit": re.compile(
        r"^(PYSEC-[0-9]{4}-[0-9]+|GHSA(-[23456789cfghjmpqrvwx]{4}){3}|CVE-[0-9]{4}-[0-9]{4,})$"
    ),
    "trivy": re.compile(r"^[A-Z][A-Z0-9]*(-[A-Za-z0-9]+)+$"),
}
FIELDS = {"tool", "id", "reason", "expires"}
# Bandit 1.9.4, bandit/core/manager.py, copied verbatim: how Bandit finds a suppression
# and reads its test ids (only the last id of a comma run without spaces is captured).
BANDIT_NOSEC = re.compile(r"#\s*nosec:?\s*(?P<tests>[^#]+)?#?")
BANDIT_NOSEC_TESTS = re.compile(r"(?:(B\d+|[a-z\d_]+),?)+", re.IGNORECASE)
# The one form the lint accepts, from where Bandit's match starts.
NOSEC_FORM = re.compile(r"#\s*nosec(?P<ids>(?:\s+B[0-9]{3}(?:\s*,\s*B[0-9]{3})*)?)\s*#\s*\S")
NOSEC_FORM_HINT = "'# nosec B603 # <reason>'"
# Every test id Bandit 1.9.4 knows (plugins, blacklist, builtin). A test compares this with
# the installed Bandit, so a Bandit upgrade fails CI until this list is updated.
BANDIT_TEST_IDS = frozenset(
    "B001 B101 B102 B103 B104 B105 B106 B107 B108 B110 B112 B113 B201 B202 B301 B302 B303 "
    "B304 B305 B306 B307 B308 B310 B311 B312 B313 B314 B315 B316 B317 B318 B319 B321 B323 "
    "B324 B401 B402 B403 B404 B405 B406 B407 B408 B409 B411 B412 B413 B415 B501 B502 B503 "
    "B504 B505 B506 B507 B508 B509 B601 B602 B603 B604 B605 B606 B607 B608 B609 B610 B611 "
    "B612 B613 B614 B615 B701 B702 B703 B704".split()
)


class IgnoreError(Exception):
    """The ignore file or a suppression is invalid; the message says why."""


def today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def load(path: Path = IGNORE_FILE, on: dt.date | None = None) -> list[dict]:
    """Return the valid entries, or raise IgnoreError listing every problem."""
    on = on or today()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise IgnoreError(f"{path} is missing") from None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise IgnoreError(f"{path}: {e}") from None
    if set(data) - {"ignore"}:
        raise IgnoreError(f"{path}: unknown top-level keys {sorted(set(data) - {'ignore'})}")
    entries = data.get("ignore", [])
    if not isinstance(entries, list):
        raise IgnoreError(f"{path}: 'ignore' must be an array of tables ([[ignore]])")
    problems = []
    for n, e in enumerate(entries, 1):
        where = f"{path} entry {n}"
        if not isinstance(e, dict):
            problems.append(f"{where}: not a table")
            continue
        if set(e) != FIELDS:
            missing, extra = sorted(FIELDS - set(e)), sorted(set(e) - FIELDS)
            problems.append(f"{where}: missing {missing}, unknown {extra}")
            continue
        tool, tid, reason, expires = e["tool"], e["id"], e["reason"], e["expires"]
        if tool not in TOOLS:
            problems.append(f"{where}: tool must be one of {list(TOOLS)}, not {tool!r}")
        elif not isinstance(tid, str) or not ID_RE[tool].fullmatch(tid):
            problems.append(f"{where}: {tid!r} is not a {tool} finding id")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{where}: reason must be a non-empty string")
        if not isinstance(expires, dt.date) or isinstance(expires, dt.datetime):
            problems.append(f"{where}: expires must be a TOML date, such as 2027-01-31")
        elif expires < on:
            problems.append(f"{where}: {tool} {tid} expired on {expires.isoformat()}")
    if problems:
        raise IgnoreError("; ".join(problems))
    return entries


def nosec_comment_problem(comment: str) -> str | None:
    """Why this comment is not an acceptable suppression, or None if Bandit sees none in it."""
    m = BANDIT_NOSEC.search(comment)
    if not m:
        return None
    form = NOSEC_FORM.match(comment, m.start())
    if not form:
        return f"'# nosec' must name known test ids and give a reason, as {NOSEC_FORM_HINT}"
    written = re.findall(r"B[0-9]{3}", form.group("ids"))
    unknown = sorted(set(written) - BANDIT_TEST_IDS)
    if unknown:
        return (
            f"'# nosec' names {', '.join(unknown)}, which Bandit 1.9.4 doesn't know; Bandit "
            "ignores unknown ids, so it could suppress every finding on the line"
        )
    read = {t.group(1) for t in BANDIT_NOSEC_TESTS.finditer(m.group("tests") or "")}
    if read != set(written):
        return (
            f"Bandit reads this '# nosec' as {sorted(read)}, not {sorted(set(written))}; "
            "separate test ids with ', '"
        )
    return None


def nosec_problems(path: str, source: str) -> list[str]:
    """Every comment Bandit would treat as a suppression that isn't in the accepted form."""
    problems = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError) as e:
        return [f"{path}: cannot tokenize: {e}"]
    for tok in tokens:
        if tok.type == tokenize.COMMENT and (why := nosec_comment_problem(tok.string)):
            problems.append(f"{path}:{tok.start[0]}: {why}")
    return problems


def tracked_python() -> list[str]:
    r = subprocess.run(["git", "ls-files", "-z", "--", "*.py"], capture_output=True, check=True)
    return [p.decode() for p in r.stdout.split(b"\0") if p]


def lint() -> list[str]:
    problems = []
    try:
        entries = load()
    except IgnoreError as e:
        problems.append(str(e))
        entries = []
    for path in tracked_python():
        problems += nosec_problems(path, Path(path).read_text(encoding="utf-8"))
    if not problems:
        print(f"{IGNORE_FILE}: {len(entries)} exception(s), every '# nosec' is well formed")
    return problems


def tool_args(tool: str, entries: list[dict], workdir: str) -> list[str]:
    ids = sorted(e["id"] for e in entries if e["tool"] == tool)
    if tool == "bandit":
        return ["--skip", ",".join(ids)] if ids else []
    if tool == "pip-audit":
        return [arg for i in ids for arg in ("--ignore-vuln", i)]
    path = os.path.join(workdir, "trivyignore")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(f"{i}\n" for i in ids))
    return ["--ignorefile", path]


def bandit_report(path: Path) -> list[str]:
    """Bandit's JSON output: any result or any error fails. A missing file fails too."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        results, errors = data["results"], data["errors"]
        if not isinstance(results, list) or not isinstance(errors, list):
            raise TypeError("results and errors must be lists")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"Bandit output {path} is unusable ({type(e).__name__}: {e})"]
    problems = [f"Bandit could not scan {e.get('filename')}: {e.get('reason')}" for e in errors]
    for r in results:
        problems.append(
            f"{r.get('filename')}:{r.get('line_number')}: {r.get('test_id')} "
            f"{r.get('issue_severity')}: {r.get('issue_text')}"
        )
    if not problems:
        loc = data.get("metrics", {}).get("_totals", {}).get("loc", "?")
        print(f"Bandit: no medium or high findings, no errors ({loc} lines scanned)")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["lint"]:
            problems = lint()
        elif len(args) == 2 and args[0] == "bandit-report":
            problems = bandit_report(Path(args[1]))
        elif len(args) >= 4 and args[0] == "exec" and args[1] in TOOLS and args[2] == "--":
            entries = load()
            with tempfile.TemporaryDirectory() as tmp:
                cmd = args[3:] + tool_args(args[1], entries, tmp)
                print(
                    f"running {args[1]} with {sum(e['tool'] == args[1] for e in entries)} "
                    "exception(s) from .security-ignore.toml",
                    flush=True,
                )
                return subprocess.run(cmd, check=False).returncode
        else:
            print(__doc__.split("\n\n")[1], file=sys.stderr)
            return 2
    except IgnoreError as e:
        problems = [str(e)]
    for p in problems:
        print(f"::error::security exceptions: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
