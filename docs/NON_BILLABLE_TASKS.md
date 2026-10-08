# Non billable tasks and the Add Non Billable Task switch

The desktop's top bar can carry a second Add button, **Add Non Billable Task**, right
after **Add Task**. A task created with it ends **` - Non billable`** (for
example `Fix the login - Non billable`). An administrator decides, member by
member, who sees the button.

## The switch

| | |
|---|---|
| Column | `users.can_add_nonbillable_tasks` (boolean, **default false**) |
| Where it is set | Members page → **Add Non Billable Task** column (Allowed / Excluded switch), the filter beside Login, and the bulk bar. Same right as the other two switches: `manage_member_access` (administrators and HR). |
| Email | **None**, in either direction. (Login and Add Task email the member; this one does not.) |
| Audit | Recorded in the activity trail as `add_nonbillable_tasks_allowed` / `_excluded`. |
| Default | Off. Every member that existed before keeps exactly what they can do today and sees no new button until allowed. Only an explicit `true` grants it. |
| Profile | `GET /auth/me` returns it; the desktop re-reads the profile on every refresh round, so a change reaches an open window without a restart. |

It is independent of **Add Task** (`can_add_tasks`): an administrator can switch
Add Task off and Add Non Billable Task on, and that member then sees a greyed Add
Task and a working Add Non Billable Task. A member with Add Task on and this off sees
the bar they always had.

## The marker

`" - Non billable"` — two words, no hyphen inside the phrase — on the end of the
task name. It is a naming convention, not a stored field, so every surface that
lists a task already shows it.

* The desktop adds it (`desktop/core/task_marker.py`); the person types only the
  name, and the dialog shows a fixed "Non billable" tag beside it.
* **Edit Task keeps it.** The marker of a task that has one is shown as a fixed tag
  and put back exactly as it was created on save; only the rest of the name can
  change. Earlier spellings (`- Non-billable`) are recognised and preserved as they
  were.
* The backend keeps its own copy of the wording (`backend/app/core/task_marker.py`);
  `desktop/tests/test_add_billable_task_button.py` reads it and fails if the two
  disagree.

## The server decides

The desktop hides the button from members who lack it, but a hidden button is
presentation. `require_task_creation` (the dependency on both task-create routes)
lets a member whose Add Task is off through **only if** their Add Non Billable Task is
on, and the service then refuses any name that does not end in the marker
(`403`, "Your account may only create Non billable tasks…"). A member with both
off is refused as before; a member who may add tasks is never restricted; a name
that is only the marker is not a name.

The WFPM create routes are unchanged (`require_permission("tasks:create")`): a
member with Add Task off is refused there whatever this switch says.

## Migration

`b6d1f8a4c2e9` adds `users.can_add_nonbillable_tasks` (`NOT NULL DEFAULT false`).
Additive and reversible (`alembic downgrade -1`); nothing is backfilled. Apply
with `python -m alembic upgrade head` from `backend/` **before** the new backend
code serves traffic, or every query on `users` fails on the missing column.

## Testing

```bash
cd backend  && python -m pytest tests/test_nonbillable_task_permission.py -q
cd desktop  && python -m pytest tests/test_add_billable_task_button.py tests/test_add_task_non_billable.py -q
cd frontend && npx vitest run src/features/admin/__tests__/membersBillableTask.test.tsx
```
