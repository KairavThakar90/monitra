"""Everything that connects Monitra to WFPM lives in this folder.

Two directions, and nothing about either is implemented anywhere else:

**WFPM -> Monitra** (WFPM pushes its projects and tasks into Monitra)

``router.py``      Every ``/WFPM/...`` route. Thin: authenticate, check the
                   permission, call the service.
``schemas.py``     The request and response shapes of those routes.
``service.py``     ``WfpmSyncService`` -- resolves a WFPM id to the Monitra row
                   it is linked to, then delegates to the same
                   ``ProjectManagementService`` / ``ProjectMemberService`` the
                   Monitra frontend uses. No project or task rule is
                   re-implemented here.

**Monitra -> WFPM** (a timer started or stopped in Monitra starts or stops
the WFPM timer)

``timer_sync.py``  ``WfpmTimerSync`` -- queues one start and one stop event per
                   timer and delivers them, with retry, backoff and jitter.
``client.py``      The only code that makes an HTTP request to WFPM.
``models.py``      ``wfpm_timer_events``, the durable queue behind it.

``repository.py``  Data access for both directions.

The link between the two systems is two columns: ``projects.wfpm_project_id``
and ``tasks.wfpm_task_id``. ``docs/WFPM_INTEGRATION.md`` is the contract --
read it before changing a route, a payload or a status code here, because the
WFPM side is written against it.

Every log line this folder writes starts with ``WFPM_``, so one ``grep WFPM_``
over the backend log is the whole story of what the integration did.

This ``__init__`` deliberately imports nothing: ``app.models`` imports
``app.WFPM.models``, and pulling the router in here would make that a cycle.
"""
