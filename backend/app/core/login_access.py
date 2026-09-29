"""The Members directory's Allow / Exclude switch for signing in.

`users.can_login` (default true) is an administrator's decision that one
member may not use Monitra at all until they are allowed again. Excluding a
member (see `MemberService.update`) stops their running timer on the server
and revokes every session they hold; from then on:

* every authenticated request they make is refused with **401** and the
  `login_disabled` detail below (`get_current_user`), so both clients take
  their ordinary "session is over" path and sign out -- the desktop within one
  sync-probe interval, the web within its own session check;
* a session refresh is refused the same way (`AuthService.refresh_session`);
* a fresh sign-in is refused with **403** and the same detail
  (`AuthService.login_exchange` and `_issue_token_pair`), so the user is told
  why rather than shown "wrong password".

The detail is a structured object, not a sentence, so each client can tell
this apart from an ordinary expiry and show the administrator's message with
its contact note instead of "your session has expired". Only an explicit False
blocks: a row that predates the column reads as allowed.
"""
from __future__ import annotations

from fastapi import HTTPException

LOGIN_DISABLED_CODE = "login_disabled"

LOGIN_DISABLED_MESSAGE = (
    "You are not allowed to log in yet. Once an administrator allows you, you can log in again."
)

LOGIN_DISABLED_NOTE = (
    "If you are continuously unable to log in, please contact your administrator."
)


def login_disabled(user) -> bool:
    """Whether an administrator has excluded `user` from signing in."""
    return getattr(user, "can_login", None) is False


def login_disabled_detail() -> dict:
    return {
        "code": LOGIN_DISABLED_CODE,
        "message": LOGIN_DISABLED_MESSAGE,
        "note": LOGIN_DISABLED_NOTE,
    }


def refuse_if_login_disabled(user, status_code: int) -> None:
    """Raise the structured refusal when `user` is excluded."""
    if login_disabled(user):
        raise HTTPException(status_code=status_code, detail=login_disabled_detail())
