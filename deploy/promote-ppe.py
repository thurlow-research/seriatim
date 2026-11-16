#!/usr/bin/env python3
"""Build a ppe promotion (SPEC REQ-70): promote/ppe-<sha12> plus a pull request into ppe.

The promotion commit P has exactly the source head S's tree, with the ppe tip T as its
first parent and S as its second (the git-data API equivalent of
`git commit-tree S^{tree} -p T -p S`). While T is still the ppe tip it is the merge base,
so merging the pull request gives ppe exactly S's tree, whatever was on ppe before. If ppe
has moved, the promotion is out of date: promotion-source refuses it, and running this
again rebuilds it on the new tip.

Usage:  GH_TOKEN=... promote-ppe.py --repo OWNER/NAME --source BRANCH [--dry-run]

GH_TOKEN must belong to an identity whose pushes trigger workflows (a GitHub App
installation token, not a workflow's GITHUB_TOKEN), so that promotion-source runs. The
token is read from the environment only, and never printed or written anywhere.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class Fail(Exception):
    pass


def call(token: str, method: str, path: str, body: dict | None = None):
    if not API.startswith("https://"):  # urlopen would also accept file: and custom schemes
        raise Fail("GITHUB_API_URL must be an https:// URL")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 # API is checked https
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise Fail(f"{method} {path}: HTTP {e.code}: {detail}") from None


def github_tree(token: str, repo: str, ref: str) -> str | None:
    tree = call(token, "GET", f"/repos/{repo}/git/trees/{urllib.parse.quote(ref, safe='')}")
    if tree.get("truncated"):
        raise Fail(f"tree listing for {ref} is truncated")
    for e in tree["tree"]:
        if e["path"] == ".github" and e["type"] == "tree":
            return e["sha"]
    return None


def ref_sha(token: str, repo: str, branch: str) -> str | None:
    try:
        return call(token, "GET", f"/repos/{repo}/git/ref/heads/{branch}")["object"]["sha"]
    except Fail as e:
        if "HTTP 404" in str(e):
            return None
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--source", required=True, help="a branch of this repository")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not API.startswith("https://"):
        print("GITHUB_API_URL must be an https:// URL", file=sys.stderr)
        return 2
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("GH_TOKEN is empty", file=sys.stderr)
        return 2
    if not REPO_RE.fullmatch(a.repo) or not BRANCH_RE.fullmatch(a.source) or ".." in a.source:
        print("bad --repo or --source", file=sys.stderr)
        return 2
    if a.source in ("ppe", "prod") or a.source.startswith("promote/"):
        print(
            "the source must be a working branch or main, not ppe, prod or promote/*",
            file=sys.stderr,
        )
        return 2

    repo = a.repo
    try:
        src = call(token, "GET", f"/repos/{repo}/branches/{urllib.parse.quote(a.source, safe='')}")
        s_sha = src["commit"]["sha"]
        s_tree = src["commit"]["commit"]["tree"]["sha"]
        t_sha = call(token, "GET", f"/repos/{repo}/branches/ppe")["commit"]["sha"]

        # promotion-source refuses this anyway; fail early with a clear message.
        main_github = github_tree(token, repo, "main")
        if main_github is None:
            raise Fail("main has no .github tree")
        if github_tree(token, repo, s_sha) != main_github:
            raise Fail(".github/ differs from main's; merge workflow changes to main first")

        branch = f"promote/ppe-{s_sha[:12]}"
        print(f"source {a.source} at {s_sha}; ppe at {t_sha}; branch {branch}")
        if a.dry_run:
            return 0

        owner = repo.split("/")[0]
        open_prs = call(
            token,
            "GET",
            f"/repos/{repo}/pulls?state=open&base=ppe"
            f"&head={urllib.parse.quote(owner + ':' + branch, safe='')}",
        )
        existing = ref_sha(token, repo, branch)
        if existing and open_prs:
            parents = [
                x["sha"]
                for x in call(token, "GET", f"/repos/{repo}/git/commits/{existing}")["parents"]
            ]
            if parents and parents[0] == t_sha:
                print(f"already open and current: {open_prs[0]['html_url']}")
                return 0

        p = call(
            token,
            "POST",
            f"/repos/{repo}/git/commits",
            {
                "message": f"Promote {a.source} ({s_sha[:12]}) to ppe\n\nSource: {s_sha}\n",
                "tree": s_tree,
                "parents": [t_sha, s_sha],
            },
        )
        if existing:
            # Rebuild on the current ppe tip (only the App may update promote/**).
            call(
                token,
                "PATCH",
                f"/repos/{repo}/git/refs/heads/{branch}",
                {"sha": p["sha"], "force": True},
            )
        else:
            call(
                token,
                "POST",
                f"/repos/{repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": p["sha"]},
            )
        if open_prs:
            print(f"rebuilt on ppe {t_sha[:12]}: {open_prs[0]['html_url']}")
            return 0
        pr = call(
            token,
            "POST",
            f"/repos/{repo}/pulls",
            {
                "title": f"Promote {a.source} ({s_sha[:12]}) to ppe",
                "head": branch,
                "base": "ppe",
                "body": (
                    f"Promotion built by `deploy/promote-ppe.py` (SPEC REQ-70).\n\n"
                    f"- source branch: `{a.source}`\n- source commit: `{s_sha}`\n"
                    f"- ppe tip: `{t_sha}`\n- promotion commit: `{p['sha']}` "
                    f"(tree equals the source's)\n\n"
                    f"Merging deploys this tree to PPE, after a clean wipe of the PPE "
                    f"project."
                ),
            },
        )
        print(f"opened {pr['html_url']}")
        return 0
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
