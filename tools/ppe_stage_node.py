#!/usr/bin/env python3
"""Stage the sidecar for the PPE's node-a (TD-slice-1 §8.6, S1-14; ruled on #69, question 1).

    python3 -I tools/ppe_stage_node.py

Run it before `ppe/ppe up`: the `ppe` CI job does, and a local pre-check run on the designated
host does the same. Standard library only; it needs `uv` on PATH.

node-a runs a pinned Python image with `ppe/data/node` mounted read-only (ppe/compose.yaml), so
no image is built and the guard's allowlist needs no `build` key. This fills that directory:
- the sidecar's runtime dependencies, exported from `uv.lock` (`uv export --frozen`) and
  installed with every hash required, wheels only and no dependency resolution
  (`--require-hashes --only-binary :all: --no-deps`), as wheels for the container's Python
  (CPython 3.12, x86_64 manylinux);
- this checkout's `node/seriatim_node`.

Before it writes anything it refuses, as the guard does, a `ppe/data` or `ppe/data/node` that
is a symlink or resolves outside ppe/. `ppe/ppe up` checks the mount again.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ppe_guard import DATA_DIR, PPE_DIR, GuardError, check_data_dirs  # noqa: E402

ROOT = PPE_DIR.parent
STAGE = DATA_DIR / "node"
PACKAGE = ROOT / "node" / "seriatim_node"
PYTHON_VERSION = "3.12"  # ppe/compose.yaml's node-a image
PLATFORM = "x86_64-manylinux_2_28"


def commands(requirements: Path, stage: Path) -> list[list[str]]:
    """The two uv commands: the export from the lock, then the hash-checked install."""
    return [
        [
            "uv",
            "export",
            "--frozen",
            "--package",
            "seriatim-node",
            "--no-dev",
            "--no-emit-workspace",
            "--no-header",
            "-o",
            str(requirements),
        ],
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(stage),
            "--python-version",
            PYTHON_VERSION,
            "--python-platform",
            PLATFORM,
            "--only-binary",
            ":all:",
            "--require-hashes",
            "--no-deps",
            "-r",
            str(requirements),
        ],
    ]


def stage(run=subprocess.run) -> Path:
    check_data_dirs(PPE_DIR, (DATA_DIR, STAGE))
    DATA_DIR.mkdir(exist_ok=True)
    shutil.rmtree(STAGE, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="seriatim-stage-") as tmp:
        for cmd in commands(Path(tmp) / "node-requirements.txt", STAGE):
            r = run(cmd, cwd=ROOT, check=False)
            if r.returncode != 0:
                raise GuardError(f"{' '.join(cmd[:3])} ... exited {r.returncode}")
    shutil.copytree(PACKAGE, STAGE / "seriatim_node", ignore=shutil.ignore_patterns("__pycache__"))
    return STAGE


def main() -> int:
    try:
        print(f"ppe: staged the sidecar in {stage()}", flush=True)
    except GuardError as e:
        print(f"ppe: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
