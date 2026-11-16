"""DEMO (never merged): Bandit must fail on this."""

import subprocess


def run(cmd: str) -> int:
    return subprocess.call(cmd, shell=True)
