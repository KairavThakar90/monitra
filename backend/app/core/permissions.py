# backend/app/core/permissions.py
#
# This table is the login gate: AuthService.login_exchange refuses any role that
# is missing a key here, with 502 "Invalid authentication provider response".
# That makes it the second half of a pair -- `MemberRole` in app/schemas/member.py
# says which roles a member may be *given*, and this says which roles may
# *sign in*. When the two drift apart you can create a member nobody can log in
# as, which is exactly what happened to `leader` and then to `hr`. Every
# MemberRole value must have an entry here; tests/test_auth_flow.py pins that.

# `manual_time_entries:create_for_others` is filing a manual entry *on somebody
# else's behalf*, and it is deliberately narrower than being able to read other
# people's time. `time_entries:view_all` used to double as both, which handed
# every role that could see the organisation's hours the ability to write hours
# onto anyone's timesheet -- including a leader, whose authority over their team
# is supervisory: they see their members' tracked time and approve or reject the
# requests those members file, but the entry itself must originate with the
# person whose day it describes.

ROLE_PERMISSIONS = {
    "employee": {
        "projects:view",
        "tasks:view",
        "time_entries:manage_own",
        "tasks:create",
        "tasks:update",
        # TODO: Confirm with senior if employee role should also get granular permissions
        # for creating/updating tasks they are assigned to, once task assignee checking is unified.
    },
    "manager": {
        "projects:create",
        "projects:update",
        "projects:delete",
        "projects:view",
        "tasks:create",
        "tasks:update",
        "tasks:delete",
        "tasks:view",
        "project_members:manage",
        "task_assignees:manage",
        "time_entries:manage_own",
        "time_entries:view_all",
        "manual_time_entries:approve",
        "manual_time_entries:create_for_others",
        "view_employees",
    },
    "org_admin": {
        "projects:create",
        "projects:update",
        "projects:delete",
        "projects:view",
        "tasks:create",
        "tasks:update",
        "tasks:delete",
        "tasks:view",
        "project_members:manage",
        "task_assignees:manage",
        "time_entries:manage_own",
        "time_entries:view_all",
        "manual_time_entries:approve",
        "manual_time_entries:create_for_others",
        "view_employees",
        "manage_employees",
        "screenshots:delete",
        "manage_desktop_releases",
    },
    "admin": {  # Alias/compatible role name mapping to org_admin permissions
        "projects:create",
        "projects:update",
        "projects:delete",
        "projects:view",
        "tasks:create",
        "tasks:update",
        "tasks:delete",
        "tasks:view",
        "project_members:manage",
        "task_assignees:manage",
        "time_entries:manage_own",
        "time_entries:view_all",
        "manual_time_entries:approve",
        "manual_time_entries:create_for_others",
        "view_employees",
        "manage_employees",
        "screenshots:delete",
        "manage_desktop_releases",
    },
    # Human resources. `hr` is one of the four roles this system offers --
    # MemberRole in app/schemas/member.py accepts it, and the
    # /project-management/metadata endpoint serves it to the UI as a choice --
    # but it had no entry here, so an HR member could be created and then could
    # not sign in. HR's authority is over people rather than projects: they
    # see the whole member directory and the organisation's time, and they
    # approve manual entries, but they do not own or delete projects.
    #
    # HR is deliberately *read-only over the directory*: `view_employees`
    # without `manage_employees`. HR sees every member's details but cannot
    # create, edit or deactivate a member -- app/api/members.py gates exactly
    # those three routes on `manage_employees`. The one thing HR may create is
    # its own manual time entry, which is what `time_entries:manage_own`
    # allows, and that entry still goes through approval like anyone else's.
    #
    # `screenshots:delete` is the one destructive capability HR holds. Deleting
    # a screenshot is a records decision rather than a directory one -- a
    # capture that should never have been taken has to be removable by the
    # people responsible for what the organisation keeps about its staff, which
    # is HR and the administrators. It is deliberately *not* granted to
    # `leader`, `manager` or `employee`: seeing your team's screenshots is a
    # supervisory read, and destroying the evidence of someone's day is not.
    "hr": {
        "screenshots:delete",
        "projects:view",
        "tasks:view",
        "time_entries:manage_own",
        "time_entries:view_all",
        "manual_time_entries:approve",
        "manual_time_entries:create_for_others",
        "view_employees",
    },
    # A team / project leader. `leader` is a role the rest of the application
    # already recognises -- TeamsService.leaders(), TeamsService.summary() and
    # /projects/assignable-leaders all select on
    # `role_name IN ("admin", "leader")`, and ProjectMemberService.LEADER_ROLES
    # is {"leader", "project_leader"} -- but it was never given a permission set
    # here. login_exchange refuses any role missing from this table, so a
    # WordPress user coming back with roles: ["leader"] authenticated
    # successfully and was then rejected with 502 "Invalid authentication
    # provider response". A leader's authority matches a manager's: they lead
    # projects, manage their members, approve manual time and see the team's
    # entries.
    "leader": {
        "projects:create",
        "projects:update",
        "projects:delete",
        "projects:view",
        "tasks:create",
        "tasks:update",
        "tasks:delete",
        "tasks:view",
        "project_members:manage",
        "task_assignees:manage",
        "time_entries:manage_own",
        "time_entries:view_all",
        "manual_time_entries:approve",
        "view_employees",
    },
    "super_admin": {
        "projects:create",
        "projects:update",
        "projects:delete",
        "projects:view",
        "tasks:create",
        "tasks:update",
        "tasks:delete",
        "tasks:view",
        "project_members:manage",
        "task_assignees:manage",
        "time_entries:manage_own",
        "time_entries:view_all",
        "manual_time_entries:approve",
        "manual_time_entries:create_for_others",
        "view_employees",
        "manage_employees",
        "screenshots:delete",
        "manage_desktop_releases",
        # TODO: Define super-admin specific system-wide settings permissions once verified.
    }
}

# `manage_desktop_releases` is deliberately held only by the three
# administrator roles above -- not by `manager`, and not by `hr`, which holds
# `screenshots:delete` beside it. Registering and publishing a desktop release
# decides what every installed client downloads and then executes, so it is a
# strictly larger authority than anything else in this table: the gap between
# a release channel and an arbitrary-code-execution channel is exactly who
# holds this key.


# `project_leader` is the second spelling ProjectMemberService.LEADER_ROLES
# already accepts. It carries exactly a leader's authority; defining it here
# keeps a provider that uses that spelling from hitting the same 502 that
# `leader` did.
ROLE_PERMISSIONS["project_leader"] = set(ROLE_PERMISSIONS["leader"])


# The release pipeline's own identity, and the smallest one this table can
# express: a single permission, and nothing else.
#
# Why it has to be a role rather than a hand-edited permission set: every login
# path re-derives `user.permissions` from this table and overwrites whatever
# was stored (AuthService.dev_login, and the two provider paths in
# AuthService). A user granted `manage_desktop_releases` directly on the row
# would silently lose it at the next sign-in, and the release job would start
# failing with a 403 that nothing in the code explains.
#
# Why it exists at all: without it, CI has to authenticate as `admin` or
# `org_admin` -- eighteen permissions including `manage_employees` and
# `screenshots:delete` -- to perform one action. A credential that sits in a CI
# secret should be able to do exactly the job it is there for, so that a leak
# costs a bad release rather than the whole organization's staff data.
#
# It is deliberately absent from PROVIDER_ROLE_ALIASES below. No human logs in
# as this role, and no provider account may be mapped onto it.
ROLE_PERMISSIONS["release_bot"] = {"manage_desktop_releases"}


# Which roles may authenticate with a service credential (an API key) instead
# of a password or a provider session.
#
# This is the hard cap on what an API key can ever be. `ServiceCredential`
# stores no permissions of its own -- a key's authority is entirely the
# account's -- so without this list, anyone who could insert a row could mint a
# non-expiring key for an administrator and hold it forever. With it, a key
# issued against any account outside this set is refused at authentication
# time, whatever the row says.
#
# Adding a role here is granting a permanent, non-interactive credential to
# everything that role can do. It is not a place to be generous.
SERVICE_ROLE_NAMES = frozenset({"release_bot"})


# Provider role slug -> Monitra role name.
#
# WordPress ships its own role vocabulary, and the provider passes those slugs
# straight through in `user.roles`. Some of them are just a different spelling
# of a role this system already defines: WordPress's built-in super-user is
# `administrator`, which is exactly Monitra's `admin`. An admin account that
# was provisioned as `admin` began coming back from the provider as
# roles: ["administrator"], which matches no key in ROLE_PERMISSIONS, so the
# login was refused with 502 -- the same failure `leader` and `hr` hit, one
# spelling further out.
#
# This table only renames; it never invents authority. An alias must point at
# a role ROLE_PERMISSIONS already defines, and it is applied before the
# membership check, so an aliased role is held to exactly the permission set
# its Monitra target carries. Only unambiguous slugs belong here: the other
# WordPress core roles (editor, author, contributor, subscriber) have no
# clean Monitra equivalent and are deliberately left unmapped so they are
# refused rather than silently granted access.
PROVIDER_ROLE_ALIASES = {
    "administrator": "admin",
}


def resolve_role_alias(role: str) -> str:
    """Map a provider role slug to its Monitra role name, if one is aliased.

    Returns the role unchanged when it is not an alias, so a role that is
    already a Monitra name (or is simply unknown) passes through untouched.
    """
    return PROVIDER_ROLE_ALIASES.get(role, role)
