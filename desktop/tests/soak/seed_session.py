"""
Seed a Monitra data directory with a saved sign-in, for the stub-backend rig.

    python seed_session.py --data-dir <dir> [--api-base http://127.0.0.1:8765]
                           [--user-email rig.user@example.test]

Creates ``<dir>/cache.db`` using the product's own ``StorageManager`` and
``LocalCache.save_session`` -- the same calls ``SessionManager.start_session``
makes -- so the next launch (source or packaged) restores the session and goes
straight to the dashboard without a sign-in screen.

If a stub server is already listening at ``--api-base`` the tokens come from
its real sign-in path (``/__portal/login`` -> ``/auth/sso/token`` ->
``/auth/me``), so the saved session is exactly what an interactive sign-in
would have stored. Otherwise the same shapes are built locally. Either way the
sign-in window is far in the future.

This is a TEST RIG. It lives under tests/soak/ and is never imported by product
code. It refuses to write into the real ``~/.monitra`` and refuses a
non-loopback ``--api-base``: nothing here may reach a real host.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
DESKTOP_ROOT = HERE.parent.parent
sys.path.insert(0, str(DESKTOP_ROOT))
sys.path.insert(0, str(HERE))

from stub_backend_server import build_user, iso  # noqa: E402  (rig-only helper)

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _post_json(url: str, payload: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def tokens_from_stub(api_base: str, email: str) -> dict | None:
    """Sign in through the running stub, or None when nothing is listening."""
    try:
        portal = _post_json(f"{api_base}/__portal/login", {"username": email, "password": "rig"})
        pair = _post_json(f"{api_base}/auth/sso/token", {"token": portal["access_token"]})
        pair["user"] = _get_json(f"{api_base}/auth/me", pair["access_token"])
        return pair
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        return None


def local_tokens(email: str) -> dict:
    created = datetime.now(timezone.utc)
    return {
        "access_token": "stub-access-token-seeded",
        "refresh_token": "stub-refresh-token-seeded",
        "user": build_user(email),
        "session_created_at": iso(created),
        "session_expires_at": iso(created + timedelta(days=3650)),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--api-base", default="http://127.0.0.1:8765")
    ap.add_argument("--user-email", default="rig.user@example.test")
    args = ap.parse_args(argv)

    api_base = args.api_base.rstrip("/")
    if (urlsplit(api_base).hostname or "") not in LOOPBACK:
        print(f"refusing: --api-base {api_base} is not a loopback address", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir).expanduser().resolve()
    if data_dir == (Path.home() / ".monitra").resolve():
        print("refusing to seed the real ~/.monitra", file=sys.stderr)
        return 2
    data_dir.mkdir(parents=True, exist_ok=True)

    # Product modules resolve the data dir (logs, db) from this at import time;
    # pin it so nothing can fall back to the real ~/.monitra.
    os.environ["MONITRA_DATA_DIR"] = str(data_dir)
    from storage.manager import StorageManager
    from sync.local_cache import LocalCache

    pair = tokens_from_stub(api_base, args.user_email)
    source = "stub sign-in"
    if pair is None:
        pair, source = local_tokens(args.user_email), "built locally (stub not reachable)"
    # The stub's own window is 90 days; a rig that sits for weeks must not be
    # signed out mid-measurement, so the stored window is pushed far out.
    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=3650)

    storage = StorageManager(str(data_dir / "cache.db"))
    try:
        cache = LocalCache(storage=storage)
        cache.save_session(
            pair["access_token"],
            pair["user"],
            refresh_token=pair["refresh_token"],
            session_created_at=created.isoformat(),
            session_expires_at=expires.isoformat(),
        )
        loaded = cache.load_session()
        assert loaded and loaded["access_token"] == pair["access_token"], "session did not round-trip"
        try:
            storage.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 - cosmetic: leaves a -wal file at worst
            pass
    finally:
        storage.close()

    print(f"seeded {data_dir / 'cache.db'} for {pair['user']['email']} (user id {pair['user']['id']}); tokens: {source}")
    print(f"  session window: {created.isoformat()} .. {expires.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
