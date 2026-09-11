"""
The local task cache must never show one user another user's tasks.

The desktop paints projects and tasks from a read-through cache before the
network answers, and those tables carry no user column -- they are replaced
wholesale on each refresh. `clear_user_scoped_cache` already runs on a
deliberate logout, which covers the ordinary path. It does not cover the
others: a session that ended without one, a token replaced underneath the
client, a crash between the two. In any of those the next person to sign in is
shown the previous person's tasks until the first response arrives.

`claim_cache_for` closes that by recording whose rows are in the cache, so the
question "are these mine?" has an answer. The backend is still the authority --
`backend/tests/test_task_isolation.py` is where that is proven -- and this is
the client-side half: not a filter over what the server returned, but a refusal
to render what a *previous* session left behind.
"""
from __future__ import annotations

from sync.local_cache import CACHE_OWNER_KEY, LocalCache

USER_A = 101
USER_B = 102

PROJECT = {"id": 1, "project_name": "Beta Launch"}
A_TASKS = [{"id": 10, "name": "A private"}]
B_TASKS = [{"id": 20, "name": "B private"}]


def _signed_in(cache: LocalCache, user_id: int, tasks: list) -> None:
    """Everything a session leaves in the read-through caches."""
    cache.claim_cache_for(user_id)
    cache.cache_projects([PROJECT])
    cache.cache_tasks(PROJECT["id"], tasks)


# ── the owner stamp ──────────────────────────────────────────────────────────

def test_the_first_login_claims_the_cache_without_clearing_anything(cache):
    cache.cache_tasks(PROJECT["id"], A_TASKS)
    assert cache.claim_cache_for(USER_A) is False
    assert cache.get_cached_tasks(PROJECT["id"]) == A_TASKS
    assert cache.load_app_state(CACHE_OWNER_KEY) == USER_A


def test_the_same_user_signing_in_again_keeps_their_cache(cache):
    _signed_in(cache, USER_A, A_TASKS)
    assert cache.claim_cache_for(USER_A) is False
    assert cache.get_cached_tasks(PROJECT["id"]) == A_TASKS


def test_a_different_user_signing_in_drops_the_previous_users_tasks(cache):
    """The leak this exists to stop: user A's tasks rendered into user B's
    session from disk, before any request has been made."""
    _signed_in(cache, USER_A, A_TASKS)

    assert cache.claim_cache_for(USER_B) is True

    assert cache.get_cached_tasks(PROJECT["id"]) is None
    assert cache.get_cached_projects() is None
    assert cache.load_app_state(CACHE_OWNER_KEY) == USER_B


def test_switching_back_does_not_restore_the_first_users_tasks(cache):
    _signed_in(cache, USER_A, A_TASKS)
    _signed_in(cache, USER_B, B_TASKS)

    cache.claim_cache_for(USER_A)

    assert cache.get_cached_tasks(PROJECT["id"]) is None


def test_an_unknown_user_id_claims_nothing_and_clears_nothing(cache):
    """A profile that arrived without an id is not evidence the owner
    changed. Clearing on it would throw away the cache of the very user who
    is about to be shown it."""
    _signed_in(cache, USER_A, A_TASKS)

    assert cache.claim_cache_for(None) is False

    assert cache.get_cached_tasks(PROJECT["id"]) == A_TASKS
    assert cache.load_app_state(CACHE_OWNER_KEY) == USER_A


def test_the_durable_queues_are_left_alone(cache):
    """Clearing is for the read-through caches only. A queued action is
    captured work that still has to be uploaded, and it is already fenced off
    by the session generation and the queue floor."""
    cache.enqueue_action("start_timer", {"task_id": 1}, idempotency_key="k1")
    _signed_in(cache, USER_A, A_TASKS)

    cache.claim_cache_for(USER_B)

    assert cache.get_pending_count() == 1


# ── the runtime transitions that drive it ────────────────────────────────────

def test_logout_then_a_different_login_shows_no_stale_tasks(runtime):
    """The full transition, through the runtime rather than the cache alone."""
    cache = runtime.cache
    _signed_in(cache, USER_A, A_TASKS)

    runtime.on_logout()
    runtime.on_login(USER_B)

    assert cache.get_cached_tasks(PROJECT["id"]) is None
    assert cache.get_cached_projects() is None


def test_a_login_without_an_intervening_logout_still_clears(runtime):
    """The case `clear_user_scoped_cache` on logout cannot reach: the previous
    session ended without one."""
    cache = runtime.cache
    _signed_in(cache, USER_A, A_TASKS)

    runtime.on_login(USER_B)

    assert cache.get_cached_tasks(PROJECT["id"]) is None


def test_the_same_user_signing_in_again_keeps_their_cache_through_the_runtime(runtime):
    cache = runtime.cache
    _signed_in(cache, USER_A, A_TASKS)

    runtime.on_login(USER_A)

    assert cache.get_cached_tasks(PROJECT["id"]) == A_TASKS


def test_login_survives_a_cache_that_cannot_be_read(runtime, monkeypatch):
    """A cache check must never be able to block signing in."""
    def explode(_user_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runtime.cache, "claim_cache_for", explode)
    runtime.on_login(USER_B)  # must not raise
