"""The "Non billable" marker on a task's name.

A task created with the top bar's second Add button carries ``" - Non billable"``
on the end of its name. The marker lives in the name on purpose: that is the one
field every surface that lists a task (this app, the dashboard, reports, WFPM)
already shows, so nothing else has to learn about it. Written "Non billable" --
two words, no hyphen inside the phrase -- as the product asked.

This is a pure module (no Qt) so a test can compare it with the backend's copy,
`backend/app/core/task_marker.py`. The backend needs the same wording because it
refuses an unmarked name from a member who may create *only* Non billable tasks;
`tests/test_add_billable_task_button.py` reads that file and fails if the two disagree.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

#: What the Add Non Billable Task button appends.
NON_BILLABLE_SUFFIX = " - Non billable"

#: Any spelling of the marker at the end of a name. Wider than what is written
#: today so a task named with an earlier spelling (" - Non-billable") is still
#: recognised as marked, and is kept exactly as it was created.
NON_BILLABLE_ENDING_PATTERN = r"\s-\s*non[-\s]?billable\s*$"
_NON_BILLABLE_ENDING = re.compile(NON_BILLABLE_ENDING_PATTERN, re.IGNORECASE)


def split_non_billable(name: str) -> Tuple[str, Optional[str]]:
    """`(base, marker)` for a task name; `marker` is None when it has none.

    `marker` is the text actually found, with its leading space and dash
    (`" - Non billable"`), so a caller that must keep a task's marker as it
    was created can put back exactly that.
    """
    name = (name or "").rstrip()
    found = _NON_BILLABLE_ENDING.search(name)
    if not found:
        return name.strip(), None
    return name[:found.start()].strip(), name[found.start():].rstrip()


def with_non_billable_suffix(name: str) -> str:
    """`name` with ` - Non billable` on the end, exactly once.

    A name that already ends in the marker -- typed by the person, in any
    spelling -- is brought to the one canonical form rather than given a
    second, so ``Fix login - Non-billable`` becomes ``Fix login - Non billable``.
    A name that is only the marker is not a name, and stays empty.
    """
    base, _marker = split_non_billable(name)
    if not base:
        return ""
    return f"{base}{NON_BILLABLE_SUFFIX}"
