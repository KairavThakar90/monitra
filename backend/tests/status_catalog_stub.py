"""Install a known status catalogue for the duration of a test.

`project_statuses` and `task_statuses` are reference tables that
`app/repositories/status_catalog.py` reads once and caches for the whole
process. The service tests here drive `ProjectManagementService` with a
`MagicMock` session, which has no tables to read: left to itself the catalogue
would build a snapshot out of whatever the mock's scripted `scalars` answer
happened to be, and -- worse -- keep it for every test that ran afterwards,
making results depend on test order.

So tests state their statuses outright. What they were verifying does not
change: the service still has to find the Todo row by its normalised name
whatever id it carries, and still has to reject an id that is not there.
"""
from contextlib import contextmanager
from unittest.mock import patch

from app.repositories.status_catalog import StatusCatalog, StatusRow

#: Any colour; no assertion in these tests depends on it.
_COLOR = "#CBD5E1"


def rows(*pairs: tuple[int, str]) -> dict[int, StatusRow]:
    """`rows((57, "Active"), (61, "Todo"))` -> the catalogue's own shape."""
    return {row_id: StatusRow(id=row_id, name=name, color=_COLOR) for row_id, name in pairs}


@contextmanager
def status_catalog(project_statuses: dict | None = None, task_statuses: dict | None = None):
    """Serve `project_statuses` / `task_statuses` instead of reading a database.

    Both default to empty, which is what an unseeded deployment looks like --
    the case that has to raise a clean 400 rather than a KeyError.
    """
    projects = project_statuses or {}
    tasks = task_statuses or {}
    with patch.object(StatusCatalog, "project_statuses", classmethod(lambda cls, db: projects)), \
         patch.object(StatusCatalog, "task_statuses", classmethod(lambda cls, db: tasks)), \
         patch.object(StatusCatalog, "project_status",
                      classmethod(lambda cls, db, status_id: projects.get(status_id))), \
         patch.object(StatusCatalog, "task_status",
                      classmethod(lambda cls, db, status_id: tasks.get(status_id))):
        yield
