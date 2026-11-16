"""Repository-wide test hooks.

`@pytest.mark.td("H14", ...)` names the TD test-scenario IDs a test discharges (SPEC REQ-60).
Each ID is recorded as a `td` property in the JUnit report, which `tools/ci/td_test_ids.py`
reads: an ID counts only if a test carrying it ran and passed.
"""

from __future__ import annotations

import re

import pytest

TD_ID_RE = re.compile(r"^[A-Z]{1,4}(?:-[A-Z]{1,6})?[0-9]{1,3}[a-z]?$")


def td_ids(item: pytest.Item) -> list[str]:
    """The IDs an item's td markers name. A malformed marker is a usage error."""
    ids: list[str] = []
    for marker in item.iter_markers("td"):
        if marker.kwargs or not marker.args:
            raise pytest.UsageError(f"{item.nodeid}: use @pytest.mark.td('ID', ...)")
        for tid in marker.args:
            if not isinstance(tid, str) or not TD_ID_RE.fullmatch(tid):
                raise pytest.UsageError(f"{item.nodeid}: {tid!r} is not a TD test ID")
            ids.append(tid)
    return list(dict.fromkeys(ids))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        for tid in td_ids(item):
            item.user_properties.append(("td", tid))
