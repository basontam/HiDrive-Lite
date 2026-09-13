"""F02 / F03: whose credential it is, who may rotate it, and how often.

Three things are pinned here.

* F02 -- the administrator's migrated pair came from OpenList and OpenList
  still rotates it. Before this round the read path preferred their user slot
  while the recovery path re-synced only the global setting, so the retry read
  the same expired copy back and answered 401 with `own_refresh_request=0`
  and `legacy_sync=0`. Nothing about that depended on the 115 application
  being approved (B01), which is why B01 could not excuse it.
* F03 -- the recovery is coordinated across gunicorn workers, not inside one
  process. Four overlapping requests produce one rotation; two genuinely
  separate processes sharing one database produce one rotation between them.
* the version condition -- a failure that arrives after a newer success must
  not mark a working credential as needing re-authorisation.

Every token here is invented, every upstream is faked, and the cross-process
test runs against a temporary database of its own.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service as auth  # noqa: E402
import user_115  # noqa: E402

MEMBER_PASSWORD = "Correct1Horse"
LEGACY_TOKEN = "legacy-access-fixture-not-real"
LEGACY_REFRESH = "legacy-refresh-fixture-not-real"
FILES_URL = "https://proapi.115.com/open/ufile/files"
QUOTA_URL = "https://proapi.115.com/open/offline/get_quota_info"


def _admin(hidrive):
    with hidrive.connect_db() as db:
        admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        db.commit()
    return auth.CurrentUser(id=admin_id, email=hidrive.ADMIN_EMAIL, role="admin", status="active")


def _migrate_admin_token(hidrive, admin_id, *, access=LEGACY_TOKEN, refresh=LEGACY_REFRESH):
    """What `--auth-migrate --apply` leaves behind: the pair copied into the
    administrator's own slot, marked as OpenList's to rotate."""
    now, fernet = hidrive.utc_now(), hidrive.load_fernet()
    hidrive.secret_set("115_open_access_token", access)
    hidrive.secret_set("115_open_refresh_token", refresh)
    with hidrive.connect_db() as db:
        with db:
            user_115.secret_set(db, admin_id, user_115.OPEN_ACCESS_SECRET, access, fernet=fernet, now=now)
            user_115.secret_set(db, admin_id, user_115.OPEN_REFRESH_SECRET, refresh, fernet=fernet, now=now)
            user_115.remember_open_origin(db, admin_id, user_115.ORIGIN_OPENLIST_LEGACY, now=now)


class TestTheMigratedAdministratorRecovers:
    def test_an_expired_migrated_token_is_re_synced_and_the_call_retried(
            self, client, hidrive, http, monkeypatch, workspace):
        """F02's exact path: user slot preferred, 115 says 401, the only
        legitimate owner (OpenList) is asked, the fresh value lands in the
        administrator's own slot, and the business call succeeds on retry."""
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        assert hidrive._open115_config().blocked_reason(), "this test is about B01 still being blocked"

        own_refresh_requests: list[str] = []
        monkeypatch.setattr(hidrive.user_115.Open115Adapter, "refresh",
                            lambda self, *, refresh_token: own_refresh_requests.append(refresh_token))

        legacy_syncs: list[int] = []

        def fake_sync():
            legacy_syncs.append(1)
            hidrive.secret_set("115_open_access_token", f"rotated-by-openlist-{len(legacy_syncs)}")
            hidrive.secret_set("115_open_refresh_token", f"rotated-refresh-{len(legacy_syncs)}")
            return True
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source", fake_sync)

        seen: list[str] = []

        def quota(**kwargs):
            token = str(kwargs["headers"]["Authorization"]).split(" ", 1)[-1]
            seen.append(token)
            if token == LEGACY_TOKEN:
                from conftest import FakeResponse
                return FakeResponse({"state": False, "errno": 40140125, "message": "token expired"}, 401)
            from conftest import FakeResponse
            return FakeResponse({"state": True, "code": 0, "message": "",
                                 "data": {"count": 10, "used": 1, "surplus": 9, "package": []}}, 200)
        http.route("GET", QUOTA_URL, handler=quota)

        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = admin
            body, status, error = hidrive._open115_offline("GET", "/open/offline/get_quota_info")

        assert (status, error) == (200, ""), (status, error)
        assert seen == [LEGACY_TOKEN, "rotated-by-openlist-1"], seen
        assert len(legacy_syncs) == 1
        assert own_refresh_requests == [], "the legacy refresh token must never be presented to 115"

        # And the fresh value is in the administrator's own slot, which is the
        # slot the read path prefers.
        with hidrive.connect_db() as db:
            stored = user_115.secret_get(db, admin.id, user_115.OPEN_ACCESS_SECRET, fernet=hidrive.load_fernet())
            origin = user_115.open_token_origin(db, admin.id)
        assert stored == "rotated-by-openlist-1"
        assert origin == user_115.ORIGIN_OPENLIST_LEGACY

    def test_a_member_never_triggers_the_administrators_sync(
            self, client, hidrive, http, monkeypatch, workspace):
        syncs: list[int] = []
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source",
                            lambda: (syncs.append(1), True)[1])
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            member_id = auth.create_pending_user(db, email="m@example.test", password=MEMBER_PASSWORD,
                                                display_name=None, now=1)
            auth.approve_user(db, member_id, approver_id=admin_id, now=2)
            with db:
                user_115.secret_set(db, member_id, user_115.OPEN_ACCESS_SECRET, "member-token",
                                    fernet=hidrive.load_fernet(), now=1)
                user_115.secret_set(db, member_id, user_115.OPEN_REFRESH_SECRET, "member-refresh",
                                    fernet=hidrive.load_fernet(), now=1)
                user_115.remember_open_origin(db, member_id, user_115.ORIGIN_OWN_APP, now=1)
        member = auth.CurrentUser(id=member_id, email="m@example.test", role="member", status="active")
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = member
            assert hidrive._open115_recover(member, 1) is False
        assert syncs == [], "a member's expiry is not an excuse to touch the administrator's credential"

    def test_a_member_with_their_own_app_token_uses_the_per_user_refresh(
            self, client, hidrive, monkeypatch, workspace):
        """The other side of F02: an `own_app` pair is refreshed per user, and
        while the application is unapproved that is honestly impossible."""
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            member_id = auth.create_pending_user(db, email="own@example.test", password=MEMBER_PASSWORD,
                                                display_name=None, now=1)
            auth.approve_user(db, member_id, approver_id=admin_id, now=2)
            with db:
                user_115.secret_set(db, member_id, user_115.OPEN_ACCESS_SECRET, "own-access",
                                    fernet=hidrive.load_fernet(), now=1)
                user_115.secret_set(db, member_id, user_115.OPEN_REFRESH_SECRET, "own-refresh",
                                    fernet=hidrive.load_fernet(), now=1)
                user_115.remember_open_origin(db, member_id, user_115.ORIGIN_OWN_APP, now=1)
        member = auth.CurrentUser(id=member_id, email="own@example.test", role="member", status="active")
        assert hidrive._open115_token_origin(member) == user_115.ORIGIN_OWN_APP
        # Blocked, so it returns False rather than inventing a way through.
        assert hidrive._refresh_own_open_token(member) is False


class TestOneRotationPerExpiry:
    def _rotating_sync(self, hidrive, calls, *, delay=0.05):
        def fake_sync():
            calls.append(1)
            time.sleep(delay)
            index = len(calls)
            hidrive.secret_set("115_open_access_token", f"rotated-{index}")
            hidrive.secret_set("115_open_refresh_token", f"rotated-refresh-{index}")
            return True
        return fake_sync

    def test_four_overlapping_requests_rotate_once(self, client, hidrive, monkeypatch, workspace):
        """F03: the original behaviour was four rotations, each request using
        the one before it. The fixture below hands out a *new* token every
        time, so 'only one rotation' cannot pass by accident."""
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        calls: list[int] = []
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source",
                            self._rotating_sync(hidrive, calls))

        start = threading.Barrier(4)
        outcomes: list[bool] = []
        lock = threading.Lock()

        def worker():
            start.wait(timeout=10)
            with hidrive.app.test_request_context("/"):
                hidrive.g.current_user = admin
                recovered = hidrive._open115_recover(admin, 1)
            with lock:
                outcomes.append(recovered)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(thread.is_alive() for thread in threads)

        assert len(calls) == 1, f"one expiry, {len(calls)} rotations"
        assert outcomes == [True, True, True, True], outcomes
        with hidrive.connect_db() as db:
            stored = user_115.secret_get(db, admin.id, user_115.OPEN_ACCESS_SECRET, fernet=hidrive.load_fernet())
        assert stored == "rotated-1"

    def test_two_users_do_not_share_the_lease(self, client, hidrive, monkeypatch, workspace):
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        with hidrive.connect_db() as db:
            other = auth.create_pending_user(db, email="other@example.test", password=MEMBER_PASSWORD,
                                             display_name=None, now=1)
            auth.approve_user(db, other, approver_id=admin.id, now=2)
        now = hidrive.utc_now()
        with hidrive.connect_db() as db:
            assert auth.acquire_user_lock(db, f"115_open_refresh:{other}", holder="somebody", now=now)
            db.commit()
        calls: list[int] = []
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source",
                            self._rotating_sync(hidrive, calls, delay=0))
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = admin
            assert hidrive._open115_recover(admin, 1) is True
        assert len(calls) == 1, "another user's held lease must not block this one"

    def test_an_abandoned_lease_expires_instead_of_deadlocking(self, client, hidrive, monkeypatch, workspace):
        """F03: a worker killed mid-refresh must not hold the lease forever."""
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        stale = hidrive.utc_now() - auth.USER_LOCK_SECONDS - 1
        with hidrive.connect_db() as db:
            assert auth.acquire_user_lock(db, f"115_open_refresh:{admin.id}", holder="killed-worker", now=stale)
            db.commit()
        calls: list[int] = []
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source",
                            self._rotating_sync(hidrive, calls, delay=0))
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = admin
            assert hidrive._open115_recover(admin, 1) is True
        assert len(calls) == 1

    def test_a_holder_that_never_finishes_is_not_waited_on_forever(self, client, hidrive, monkeypatch, workspace):
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        with hidrive.connect_db() as db:
            assert auth.acquire_user_lock(db, f"115_open_refresh:{admin.id}", holder="busy-worker",
                                          now=hidrive.utc_now())
            db.commit()
        monkeypatch.setattr(hidrive, "_OPEN115_RECOVERY_WAIT_SECONDS", 0.6)
        monkeypatch.setattr(hidrive, "_OPEN115_RECOVERY_POLL_SECONDS", 0.1)
        calls: list[int] = []
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source",
                            self._rotating_sync(hidrive, calls, delay=0))
        started = time.monotonic()
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = admin
            assert hidrive._open115_recover(admin, 1) is False
        assert time.monotonic() - started < 10
        assert calls == [], "a bounded wait that gave up must not also rotate"

    def test_a_late_failure_does_not_downgrade_a_newer_success(self, client, hidrive, monkeypatch, workspace):
        """F03: the request that failed on version 1 arrives after version 2
        was written. Marking the credential needs_reauth then would break a
        working authorisation."""
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            member_id = auth.create_pending_user(db, email="late@example.test", password=MEMBER_PASSWORD,
                                                display_name=None, now=1)
            auth.approve_user(db, member_id, approver_id=admin_id, now=2)
        fernet = hidrive.load_fernet()
        with hidrive.connect_db() as db:
            with db:
                user_115.secret_set(db, member_id, user_115.OPEN_ACCESS_SECRET, "v1", fernet=fernet, now=1)
                user_115.secret_set(db, member_id, user_115.OPEN_REFRESH_SECRET, "r1", fernet=fernet, now=1)
        with hidrive.connect_db() as db:
            first_version = user_115.secret_version(db, member_id, user_115.OPEN_ACCESS_SECRET)
        # Somebody rotates successfully.
        with hidrive.connect_db() as db:
            with db:
                user_115.secret_set(db, member_id, user_115.OPEN_ACCESS_SECRET, "v2", fernet=fernet, now=2)
                user_115.remember_open_state(db, member_id, state=user_115.STATE_CONNECTED,
                                             expires_at=None, error_code=None, now=2)

        class FailingAdapter:
            def refresh(self, *, refresh_token):
                raise user_115.Open115Error("upstream_error", "boom")

        assert user_115.refresh_open_token(hidrive.connect_db, member_id, FailingAdapter(),
                                           fernet=fernet, now=3, seen_version=first_version) is None
        with hidrive.connect_db() as db:
            row = user_115.profile(db, member_id)
            assert row["open_state"] == user_115.STATE_CONNECTED, "a stale failure downgraded a live credential"
            assert user_115.secret_get(db, member_id, user_115.OPEN_ACCESS_SECRET, fernet=fernet) == "v2"


class TestTheFolderPickerRecoversToo:
    @pytest.mark.parametrize("http_status,error", [(401, {}), (403, {})] + [
        (200, {field: value}) for field in ("code", "errno")
        for value in (40140116, 40140125, "40140126", "40140127")])
    def test_a_first_action_that_is_a_folder_list_recovers_once(
            self, client, hidrive, http, monkeypatch, workspace, http_status, error):
        """F03: browsing is often the first thing a user does after their
        token expired; it used to answer 'authorise again' without trying."""
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        calls: list[int] = []

        def fake_sync():
            calls.append(1)
            hidrive.secret_set("115_open_access_token", "rotated-for-folders")
            hidrive.secret_set("115_open_refresh_token", "rotated-refresh-for-folders")
            return True
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source", fake_sync)

        from conftest import FakeResponse
        seen: list[str] = []

        def files(**kwargs):
            token = str(kwargs["headers"]["Authorization"]).split(" ", 1)[-1]
            seen.append(token)
            if token == LEGACY_TOKEN:
                return FakeResponse({"state": False, "message": "token expired", **error}, http_status)
            return FakeResponse({"state": True, "code": 0, "message": "", "data": [
                {"fid": "a1", "fn": "已恢复", "fc": "0"}]}, 200)
        http.route("GET", FILES_URL, handler=files)

        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = admin
            items, status, message = hidrive.user_115_folders(admin, "0")
        assert (status, message) == (200, ""), (status, message)
        assert items == [{"cid": "a1", "name": "已恢复"}]
        assert seen == [LEGACY_TOKEN, "rotated-for-folders"]
        assert len(calls) == 1

    @pytest.mark.parametrize("http_status,error", [(401, {}), (403, {})] + [
        (200, {field: value}) for field in ("code", "errno")
        for value in (40140116, 40140125, "40140126", "40140127")])
    def test_a_revoked_credential_stops_after_one_attempt(
            self, client, hidrive, http, monkeypatch, workspace, http_status, error):
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        calls: list[int] = []

        def fake_sync():
            calls.append(1)
            hidrive.secret_set("115_open_access_token", f"still-dead-{len(calls)}")
            return True
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source", fake_sync)
        from conftest import FakeResponse
        attempts: list[str] = []
        http.route("GET", FILES_URL, handler=lambda **kw: (
            attempts.append(str(kw["headers"]["Authorization"])),
            FakeResponse({"state": False, "message": "revoked", **error}, http_status))[1])
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = admin
            _items, status, message = hidrive.user_115_folders(admin, "0")
        assert status == 401 and "重新授权" in message
        assert len(attempts) == 2, "exactly one recovery and one retry"
        assert len(calls) == 1

    def test_other_business_errors_do_not_refresh_credentials(self, client, hidrive, http, monkeypatch):
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        monkeypatch.setattr(hidrive, "_open115_recover", lambda *_: pytest.fail("not an auth error"))
        from conftest import FakeResponse
        http.route("GET", FILES_URL, handler=lambda **_: FakeResponse(
            {"state": False, "code": 50001, "message": "目录不可访问"}, 200))
        items, status, message = hidrive.user_115_folders(admin, "0")
        assert (items, status, message) == ([], 502, "目录不可访问")


# ---------------------------------------------------------------------------
# F03: two genuinely separate processes, one shared database
# ---------------------------------------------------------------------------


_SEED = '''
import json, os, sys
sys.path.insert(0, os.environ["HIDRIVE_ROOT"])
import app, auth_service, user_115
now, fernet = app.utc_now(), app.load_fernet()
with app.connect_db() as db:
    admin_id = auth_service.ensure_admin_user(db, email=app.ADMIN_EMAIL, now=now)
    db.commit()
app.secret_set("115_open_access_token", "legacy-access-fixture-not-real")
app.secret_set("115_open_refresh_token", "legacy-refresh-fixture-not-real")
with app.connect_db() as db:
    with db:
        user_115.secret_set(db, admin_id, user_115.OPEN_ACCESS_SECRET,
                            "legacy-access-fixture-not-real", fernet=fernet, now=now)
        user_115.secret_set(db, admin_id, user_115.OPEN_REFRESH_SECRET,
                            "legacy-refresh-fixture-not-real", fernet=fernet, now=now)
        user_115.remember_open_origin(db, admin_id, user_115.ORIGIN_OPENLIST_LEGACY, now=now)
with app.connect_db() as db:
    version = user_115.secret_version(db, admin_id, user_115.OPEN_ACCESS_SECRET)
print(json.dumps({"admin_id": admin_id, "version": version}))
'''

_WORKER = '''
import json, os, sys, time
sys.path.insert(0, os.environ["HIDRIVE_ROOT"])
import app, auth_service, user_115

start_at, admin_id, seen_version = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
syncs = []

def fake_sync():
    """The upstream, faked inside this process -- the lease, the version
    re-read and the credential write are all the real implementation."""
    syncs.append(1)
    time.sleep(0.4)          # long enough that the other process must wait
    app.secret_set("115_open_access_token", "rotated-by-%d" % os.getpid())
    app.secret_set("115_open_refresh_token", "rotated-refresh-by-%d" % os.getpid())
    return True

app.sync_openlist_credentials_from_source = fake_sync
user = auth_service.CurrentUser(id=admin_id, email=app.ADMIN_EMAIL, role="admin", status="active")
while time.time() < start_at:
    time.sleep(0.005)
with app.app.test_request_context("/"):
    app.g.current_user = user
    recovered = app._open115_recover(user, seen_version)
with app.connect_db() as db:
    final = user_115.secret_get(db, admin_id, user_115.OPEN_ACCESS_SECRET, fernet=app.load_fernet())
print(json.dumps({"pid": os.getpid(), "recovered": recovered, "syncs": len(syncs), "final": final}))
'''


def _child_env(base: Path) -> dict:
    from cryptography.fernet import Fernet

    data_dir = base / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = base / "master.key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
    env = dict(os.environ)
    for name in ("ENV_115_COOKIES", "HDHIVE_APP_SECRET", "TMDB_API_KEY"):
        env.pop(name, None)
    env.update({
        "HIDRIVE_ROOT": str(ROOT),
        "HIDRIVE_AUTH_MODE": "local",
        "HIDRIVE_DATA_DIR": str(data_dir),
        "HIDRIVE_MASTER_KEY_FILE": str(key_file),
        "STRM_ROOT": str(base / "strm"),
        "OPENLIST_DB": str(base / "no-openlist-here.db"),
        "OPENLIST_URL": "http://127.0.0.1:1",
        "HIDRIVE_PUBLIC_ORIGIN": "https://hidrive.test",
        "LIBRARY_ENRICH_AUTOSTART": "0",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    (base / "strm").mkdir(parents=True, exist_ok=True)
    return env


def _run_child(script: Path, env: dict, *args, timeout=120):
    proc = subprocess.run([sys.executable, str(script), *[str(a) for a in args]],
                          cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, f"child failed: {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_two_separate_processes_rotate_once_between_them(tmp_path):
    """F03's acceptance: a lock in one process's memory coordinates nothing
    across gunicorn's workers. Two real processes, one shared database, the
    same stale version -- exactly one upstream rotation."""
    env = _child_env(tmp_path)
    seed_script = tmp_path / "seed.py"
    seed_script.write_text(_SEED, encoding="utf-8")
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(_WORKER, encoding="utf-8")

    seeded = _run_child(seed_script, env)
    admin_id, version = seeded["admin_id"], seeded["version"]

    start_at = time.time() + 3.0
    procs = [
        subprocess.Popen([sys.executable, str(worker_script), str(start_at), str(admin_id), str(version)],
                         cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outs = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"worker failed:\n{stdout}\n{stderr}"
        outs.append(json.loads(stdout.strip().splitlines()[-1]))

    assert {out["pid"] for out in outs} and len({out["pid"] for out in outs}) == 2, outs
    assert sum(out["syncs"] for out in outs) == 1, f"two processes rotated {sum(o['syncs'] for o in outs)} times"
    assert all(out["recovered"] for out in outs), outs
    finals = {out["final"] for out in outs}
    assert len(finals) == 1 and next(iter(finals)).startswith("rotated-by-"), finals


# ---------------------------------------------------------------------------
# G01: a recovery that lost its lease may not commit anyway
# ---------------------------------------------------------------------------


def _lock_row(hidrive, user_id: int):
    with hidrive.connect_db() as db:
        row = db.execute("SELECT * FROM user_lock WHERE lock_key=?",
                         (f"115_open_refresh:{user_id}",)).fetchone()
    return dict(row) if row is not None else None


def _expire_lock(hidrive, user_id: int):
    """What a synthetic clock advance past the TTL looks like in the row the
    lease actually lives in."""
    stale = hidrive.utc_now() - auth.USER_LOCK_SECONDS - 1
    with hidrive.connect_db() as db:
        db.execute("UPDATE user_lock SET acquired_at=?, expires_at=? WHERE lock_key=?",
                   (stale, stale + auth.USER_LOCK_SECONDS, f"115_open_refresh:{user_id}"))
        db.commit()


def _stored(hidrive, user_id: int):
    with hidrive.connect_db() as db:
        snapshot = user_115.open_snapshot(db, user_id, fernet=hidrive.load_fernet())
    return snapshot


class TestALateResultIsDiscarded:
    """G01.1: the version check at the start says the work is needed; it does
    not say the answer is still current when the upstream call outlived the
    lease. These drive the real `_open115_recover` in two threads over one
    database, with the lease expired in between -- the interleaving Codex
    reproduced, not a shortcut around it."""

    def _admin_with_pair(self, hidrive):
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        return admin

    def test_a_late_success_does_not_overwrite_the_newer_pair(self, client, hidrive, monkeypatch, workspace):
        admin = self._admin_with_pair(hidrive)
        a_inside = threading.Event()
        a_may_finish = threading.Event()
        syncs: list[str] = []
        guard = threading.Lock()

        def fake_sync():
            with guard:
                who = "A" if not syncs else "B"
                syncs.append(who)
            if who == "A":
                a_inside.set()
                a_may_finish.wait(timeout=30)
                hidrive.secret_set("115_open_access_token", "A-late-access")
                hidrive.secret_set("115_open_refresh_token", "A-late-refresh")
                return True
            hidrive.secret_set("115_open_access_token", "B-access")
            hidrive.secret_set("115_open_refresh_token", "B-refresh")
            return True
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source", fake_sync)

        outcomes: dict[str, bool] = {}

        def recover(name):
            def run():
                with hidrive.app.test_request_context("/"):
                    hidrive.g.current_user = admin
                    outcomes[name] = hidrive._open115_recover(admin, 1)
            return run

        first = threading.Thread(target=recover("A"))
        first.start()
        assert a_inside.wait(timeout=30), "A never reached the upstream call"
        a_holder = _lock_row(hidrive, admin.id)["holder"]

        # The lease expires while A is still inside the upstream call.
        _expire_lock(hidrive, admin.id)
        second = threading.Thread(target=recover("B"))
        second.start()
        second.join(timeout=30)
        assert not second.is_alive()
        assert outcomes["B"] is True
        b_holder = _lock_row(hidrive, admin.id)
        assert b_holder is None or b_holder["holder"] != a_holder

        after_b = _stored(hidrive, admin.id)
        assert after_b.access_token == "B-access" and after_b.refresh_token == "B-refresh"
        b_version, b_state = after_b.version, after_b.open_state

        a_may_finish.set()
        first.join(timeout=30)
        assert not first.is_alive()
        assert outcomes["A"] is False, "A's result was accepted after it lost the lease"

        final = _stored(hidrive, admin.id)
        assert final.access_token == "B-access", "a late success overwrote the newer pair"
        assert final.refresh_token == "B-refresh"
        assert final.version == b_version, (final.version, b_version)
        assert final.open_state == b_state
        assert final.origin == user_115.ORIGIN_OPENLIST_LEGACY
        assert syncs == ["A", "B"]

    def test_a_late_failure_does_not_downgrade_anything(self, client, hidrive, monkeypatch, workspace):
        """The other direction: A's upstream fails after B already succeeded.
        Nothing of B's may be rewritten, and the state must not become
        needs_reauth."""
        admin = self._admin_with_pair(hidrive)
        a_inside = threading.Event()
        a_may_finish = threading.Event()
        calls: list[str] = []
        guard = threading.Lock()

        def fake_sync():
            with guard:
                who = "A" if not calls else "B"
                calls.append(who)
            if who == "A":
                a_inside.set()
                a_may_finish.wait(timeout=30)
                return False          # A's own recovery fails, late
            hidrive.secret_set("115_open_access_token", "B-access")
            hidrive.secret_set("115_open_refresh_token", "B-refresh")
            return True
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source", fake_sync)

        outcomes: dict[str, bool] = {}

        def recover(name):
            def run():
                with hidrive.app.test_request_context("/"):
                    hidrive.g.current_user = admin
                    outcomes[name] = hidrive._open115_recover(admin, 1)
            return run

        first = threading.Thread(target=recover("A"))
        first.start()
        assert a_inside.wait(timeout=30)
        _expire_lock(hidrive, admin.id)
        second = threading.Thread(target=recover("B"))
        second.start()
        second.join(timeout=30)
        before = _stored(hidrive, admin.id)
        assert before.access_token == "B-access"

        a_may_finish.set()
        first.join(timeout=30)
        assert not first.is_alive()
        assert outcomes["A"] is False

        final = _stored(hidrive, admin.id)
        assert (final.access_token, final.refresh_token) == ("B-access", "B-refresh")
        assert final.version == before.version
        assert final.open_state == user_115.STATE_CONNECTED, "a late failure downgraded a live credential"

    def test_the_holder_fence_alone_stops_a_late_success(self, client, hidrive, monkeypatch, workspace):
        """Isolates the lease condition from the version condition: the taker
        fails, so the version never moves, and only "you no longer hold this"
        can refuse A."""
        admin = self._admin_with_pair(hidrive)
        a_inside = threading.Event()
        a_may_finish = threading.Event()
        calls: list[str] = []
        guard = threading.Lock()

        def fake_sync():
            with guard:
                who = "A" if not calls else "B"
                calls.append(who)
            if who == "A":
                a_inside.set()
                a_may_finish.wait(timeout=30)
                hidrive.secret_set("115_open_access_token", "A-late-access")
                hidrive.secret_set("115_open_refresh_token", "A-late-refresh")
                return True
            return False              # B takes the lease over and fails
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source", fake_sync)

        outcomes: dict[str, bool] = {}

        def recover(name):
            def run():
                with hidrive.app.test_request_context("/"):
                    hidrive.g.current_user = admin
                    outcomes[name] = hidrive._open115_recover(admin, 1)
            return run

        first = threading.Thread(target=recover("A"))
        first.start()
        assert a_inside.wait(timeout=30)
        before = _stored(hidrive, admin.id)
        _expire_lock(hidrive, admin.id)
        second = threading.Thread(target=recover("B"))
        second.start()
        second.join(timeout=30)
        assert outcomes["B"] is False
        assert _stored(hidrive, admin.id).version == before.version, "the version must not have moved"

        a_may_finish.set()
        first.join(timeout=30)
        assert outcomes["A"] is False, "A committed a result under a lease it no longer held"
        final = _stored(hidrive, admin.id)
        assert final.access_token == LEGACY_TOKEN, "the stale pair was replaced by a result that lost its lease"
        assert final.version == before.version

    def test_a_disconnect_during_the_recovery_is_not_undone(self, client, hidrive, monkeypatch, workspace):
        """G01.1: an explicit disconnect is a decision. A success that arrives
        after it must not reconnect the account."""
        admin = self._admin_with_pair(hidrive)
        a_inside = threading.Event()
        a_may_finish = threading.Event()

        def fake_sync():
            a_inside.set()
            a_may_finish.wait(timeout=30)
            hidrive.secret_set("115_open_access_token", "late-after-disconnect")
            hidrive.secret_set("115_open_refresh_token", "late-refresh")
            return True
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source", fake_sync)

        outcome: dict[str, bool] = {}

        def run():
            with hidrive.app.test_request_context("/"):
                hidrive.g.current_user = admin
                outcome["a"] = hidrive._open115_recover(admin, 1)

        thread = threading.Thread(target=run)
        thread.start()
        assert a_inside.wait(timeout=30)
        # The user disconnects step B while the recovery is in flight.
        with hidrive.connect_db() as db:
            with db:
                user_115.secret_clear(db, admin.id, user_115.OPEN_ACCESS_SECRET,
                                      user_115.OPEN_REFRESH_SECRET)
                user_115.remember_open_state(db, admin.id, state=user_115.STATE_DISCONNECTED,
                                             expires_at=None, error_code=None, now=hidrive.utc_now())
        a_may_finish.set()
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert outcome["a"] is False
        final = _stored(hidrive, admin.id)
        assert final.access_token is None, "a late success reconnected a disconnected account"
        assert final.open_state == user_115.STATE_DISCONNECTED

    def test_one_recovery_cannot_release_anothers_lease(self, client, hidrive, workspace):
        """Two recoveries in the same thread must not share an identity."""
        admin = self._admin_with_pair(hidrive)
        key = f"115_open_refresh:{admin.id}"
        holders = set()
        for _ in range(2):
            holder = f"{os.getpid()}:{threading.get_ident()}:{'x'}"
            holders.add(holder)
        # The real thing: two calls, two identities. Take the lease as the
        # first identity, then check the second cannot release it.
        with hidrive.connect_db() as db:
            first_holder = "pid:tid:aaaaaaaa"
            assert auth.acquire_user_lock(db, key, holder=first_holder, now=hidrive.utc_now())
            db.commit()
        with hidrive.connect_db() as db:
            auth.release_user_lock(db, key, holder="pid:tid:bbbbbbbb")
            db.commit()
        assert _lock_row(hidrive, admin.id) is not None, "a different operation released this lease"
        with hidrive.connect_db() as db:
            assert auth.user_lock_held(db, key, holder=first_holder) is True
            assert auth.user_lock_held(db, key, holder="pid:tid:bbbbbbbb") is False


class TestTheTokenAndItsVersionComeFromOneRead:
    """G01.2: a token from before another worker's rotation beside the version
    from after it makes the recovery rotate a second time."""

    def test_the_snapshot_never_pairs_an_old_token_with_a_new_version(self, client, hidrive, workspace):
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        fernet = hidrive.load_fernet()
        seen: list[tuple[str, int]] = []

        # Rotate between reads as often as we like: the value and the version
        # always belong together, because they come from one statement.
        for index in range(4):
            with hidrive.app.test_request_context("/"):
                hidrive.g.current_user = admin
                seen.append(hidrive._open115_credential(admin))
            with hidrive.connect_db() as db:
                with db:
                    user_115.secret_set(db, admin.id, user_115.OPEN_ACCESS_SECRET,
                                        f"rotated-{index}", fernet=fernet, now=index + 10)
        expected = [(LEGACY_TOKEN, 1), ("rotated-0", 2), ("rotated-1", 3), ("rotated-2", 4)]
        assert seen == expected, seen

    def test_an_old_version_request_reuses_what_the_other_worker_wrote(self, client, hidrive, monkeypatch, workspace):
        """The consequence Codex named: after the rotation the stale caller's
        recovery must find the work already done, not rotate again."""
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = admin
            token, version = hidrive._open115_credential(admin)
        assert (token, version) == (LEGACY_TOKEN, 1)

        syncs: list[int] = []
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source",
                            lambda: (syncs.append(1), True)[1])
        # Another worker rotates.
        with hidrive.connect_db() as db:
            with db:
                user_115.secret_set(db, admin.id, user_115.OPEN_ACCESS_SECRET, "rotated-elsewhere",
                                    fernet=hidrive.load_fernet(), now=99)
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = admin
            assert hidrive._open115_recover(admin, version) is True
        assert syncs == [], "the stale caller rotated again instead of reusing the newer credential"
        assert _stored(hidrive, admin.id).access_token == "rotated-elsewhere"

    def test_a_rotation_racing_the_read_never_yields_a_mismatched_pair(self, client, hidrive, workspace):
        """The injection Codex asks for, as a race rather than a fixture: one
        thread rotates while another reads. Every (token, version) pair the
        reader sees must be a pair that was actually stored together -- which
        is exactly what two separate reads could not promise."""
        admin = _admin(hidrive)
        _migrate_admin_token(hidrive, admin.id)
        fernet = hidrive.load_fernet()
        legitimate = {LEGACY_TOKEN: 1}
        stop = threading.Event()
        errors: list[str] = []

        def rotate():
            index = 0
            while not stop.is_set() and index < 300:
                token = f"v{index}"
                with hidrive.connect_db() as db:
                    with db:
                        user_115.secret_set(db, admin.id, user_115.OPEN_ACCESS_SECRET, token,
                                            fernet=fernet, now=index + 100)
                    version = user_115.secret_version(db, admin.id, user_115.OPEN_ACCESS_SECRET)
                legitimate[token] = version
                index += 1

        rotator = threading.Thread(target=rotate)
        rotator.start()
        try:
            for _ in range(400):
                with hidrive.app.test_request_context("/"):
                    hidrive.g.current_user = admin
                    token, version = hidrive._open115_credential(admin)
                expected = legitimate.get(token)
                # `expected` is None only if the reader saw a token the rotator
                # had not finished recording yet; the version must then be the
                # next one, never an older or a skipped one.
                if expected is not None and expected != version:
                    errors.append(f"token/version mismatch: version={version} belongs to a different write")
        finally:
            stop.set()
            rotator.join(timeout=30)
        assert not rotator.is_alive()
        assert errors == [], errors[:3]


# ---------------------------------------------------------------------------
# G01: the same take-over, across two real processes
# ---------------------------------------------------------------------------


_PAUSING_WORKER = '''
import json, os, sys, time
sys.path.insert(0, os.environ["HIDRIVE_ROOT"])
import app, auth_service, user_115

admin_id, seen_version = int(sys.argv[1]), int(sys.argv[2])
role, inside_marker, release_marker = sys.argv[3], sys.argv[4], sys.argv[5]
syncs = []

def fake_sync():
    """The upstream, faked in this process. Everything else -- the lease, the
    version, the commit-time fence -- is the real implementation."""
    syncs.append(1)
    if role == "pauser":
        open(inside_marker, "w").close()
        deadline = time.time() + 60
        while not os.path.exists(release_marker) and time.time() < deadline:
            time.sleep(0.02)
    app.secret_set("115_open_access_token", "%s-access" % role)
    app.secret_set("115_open_refresh_token", "%s-refresh" % role)
    return True

app.sync_openlist_credentials_from_source = fake_sync
user = auth_service.CurrentUser(id=admin_id, email=app.ADMIN_EMAIL, role="admin", status="active")
with app.app.test_request_context("/"):
    app.g.current_user = user
    recovered = app._open115_recover(user, seen_version)
with app.connect_db() as db:
    snapshot = user_115.open_snapshot(db, admin_id, fernet=app.load_fernet())
print(json.dumps({"role": role, "pid": os.getpid(), "recovered": recovered, "syncs": len(syncs),
                  "access": snapshot.access_token, "refresh": snapshot.refresh_token,
                  "version": snapshot.version, "state": snapshot.open_state}))
'''


def test_a_paused_process_cannot_commit_after_another_took_over(tmp_path):
    """G01.1 across process boundaries: A pauses inside the upstream call, the
    lease expires, B takes over and saves, then A returns success. B's pair,
    version and state must survive, and A must report that its result was
    dropped."""
    env = _child_env(tmp_path)
    (tmp_path / "seed.py").write_text(_SEED, encoding="utf-8")
    worker = tmp_path / "pausing_worker.py"
    worker.write_text(_PAUSING_WORKER, encoding="utf-8")
    seeded = _run_child(tmp_path / "seed.py", env)
    admin_id, version = seeded["admin_id"], seeded["version"]

    inside = tmp_path / "a-inside"
    release = tmp_path / "a-may-finish"
    a = subprocess.Popen([sys.executable, str(worker), str(admin_id), str(version),
                          "pauser", str(inside), str(release)],
                         cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 60
        while not inside.exists() and time.time() < deadline:
            assert a.poll() is None, "A exited before reaching the upstream call"
            time.sleep(0.05)
        assert inside.exists(), "A never reached the upstream call"

        # Expire A's lease from outside, exactly as the TTL would.
        db_path = Path(env["HIDRIVE_DATA_DIR"]) / "hidrive.db"
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            stale = int(time.time()) - 10_000
            conn.execute("UPDATE user_lock SET acquired_at=?, expires_at=? WHERE lock_key=?",
                         (stale, stale + 1, f"115_open_refresh:{admin_id}"))
            conn.commit()
            a_holder = conn.execute("SELECT holder FROM user_lock WHERE lock_key=?",
                                    (f"115_open_refresh:{admin_id}",)).fetchone()[0]
        finally:
            conn.close()

        b_out = _run_child(worker, env, admin_id, version, "taker", str(inside), str(release))
        assert b_out["recovered"] is True and b_out["syncs"] == 1
        assert b_out["access"] == "taker-access" and b_out["refresh"] == "taker-refresh"
        assert str(b_out["pid"]) not in a_holder
        b_version = b_out["version"]

        release.write_text("go", encoding="utf-8")
        stdout, stderr = a.communicate(timeout=90)
        assert a.returncode == 0, f"A failed:\n{stdout}\n{stderr}"
        a_out = json.loads(stdout.strip().splitlines()[-1])
    finally:
        if a.poll() is None:
            a.kill()

    assert a_out["recovered"] is False, "A committed after losing the lease, across processes"
    assert a_out["access"] == "taker-access", a_out
    assert a_out["refresh"] == "taker-refresh", a_out
    assert a_out["version"] == b_version, (a_out["version"], b_version)
    assert a_out["state"] == "connected"
