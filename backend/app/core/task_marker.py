"""The "Non billable" marker on a task's name, and who may create such tasks.

A task made with the desktop's second Add button carries ``" - Non billable"``
on the end of its name. The marker is a naming convention, not a stored field:
every surface that lists a task already shows its name, so nothing else has to
learn about it. The desktop keeps its own copy of the wording
(`desktop/ui/task_marker.py`); `desktop/tests/test_task_marker.py` reads this
file and fails if the two disagree.

The server needs the rule for one reason. An administrator can allow a member
to create *only* Non billable tasks (`users.can_add_nonbillable_tasks`) while
Add Task (`users.can_add_tasks`) is switched off for them. The desktop hides
and shows buttons accordingly, but a hidden button is presentation: the same
member could send an ordinary create with any curl. So the create route lets
them through, and `enforce_marked_only_creation` then refuses any name that does
not carry the marker.
"""
from __future__ import annotations

import re

from fastapi import HTTPException, status

#: What the desktop appends. Two words, no hyphen inside the phrase.
NON_BILLABLE_SUFFIX = " - Non billable"

#: Any spelling of the marker at the end of a name. Wider than what is written
#: today so a name made with an earlier spelling (" - Non-billable") still counts.
_NON_BILLABLE_ENDING = re.compile(r"\s-\s*non[-\s]?billable\s*$", re.IGNORECASE)

#: What a member allowed to create only Non billable tasks is told when they
#: send a name without the marker.
MARKED_ONLY_MESSAGE = (
    "Your account may only create Non billable tasks. "
    f'The task name must end with "{NON_BILLABLE_SUFFIX.strip()}".'
)


def has_non_billable_marker(name: str | None) -> bool:
    """Whether `name` ends in the marker *and* has a real name in front of it.

    A name that is only the marker is not a name, so it does not count.
    """
    text = (name or "").strip()
    found = _NON_BILLABLE_ENDING.search(text)
    return bool(found and text[: found.start()].strip())


def marked_only(user) -> bool:
    """True when `user` may create tasks only through the Non billable route:
    Add Task is switched off for them and Non billable is switched on.

    Only an explicit False on `can_add_tasks` withdraws (an unset value is the
    default, allowed), and only an explicit True on the Non billable switch
    grants it (unset is the default, not allowed), matching how each column is
    read everywhere else.
    """
    return (
        getattr(user, "can_add_tasks", None) is False
        and getattr(user, "can_add_nonbillable_tasks", None) is True
    )


def enforce_marked_only_creation(user, task_name: str | None) -> None:
    """Refuse a create by a marked-only member whose name lacks the marker.

    Everyone else is untouched: a member who may add tasks creates whatever
    they like, with or without the marker.
    """
    if marked_only(user) and not has_non_billable_marker(task_name):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MARKED_ONLY_MESSAGE)
