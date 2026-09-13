"""Idle OpenList owns the refresh; copying an unchanged token is not recovery."""
import pytest

from conftest import FakeResponse
from test_open115_recovery import _admin, _migrate_admin_token, LEGACY_TOKEN, LEGACY_REFRESH, QUOTA_URL
import user_115


def setup_legacy(hidrive, monkeypatch, *, migrated=False):
    admin = _admin(hidrive)
    hidrive.secret_set('115_open_access_token', LEGACY_TOKEN)
    hidrive.secret_set('115_open_refresh_token', LEGACY_REFRESH)
    hidrive.secret_set('openlist_token', 'openlist-admin-fixture')
    if migrated:
        _migrate_admin_token(hidrive, admin.id)
    monkeypatch.setattr(hidrive, 'sync_openlist_credentials_from_source', lambda: True)
    return admin


@pytest.mark.parametrize('migrated', [False, True])
def test_idle_owner_is_asked_once_then_changed_pair_is_adopted(
        client, hidrive, http, monkeypatch, workspace, migrated):
    admin = setup_legacy(hidrive, monkeypatch, migrated=migrated)
    calls = []

    def owner(**kwargs):
        calls.append(kwargs)
        hidrive.secret_set('115_open_access_token', 'owner-rotated-access-fixture')
        hidrive.secret_set('115_open_refresh_token', 'owner-rotated-refresh-fixture')
        return FakeResponse({'code': 200, 'data': {'content': []}}, 200)

    http.route('POST', hidrive.OPENLIST_URL + '/api/fs/list', handler=owner)
    seen = []

    def quota(**kwargs):
        token = kwargs['headers']['Authorization']
        seen.append(token)
        if token == 'Bearer ' + LEGACY_TOKEN:
            return FakeResponse({'state': False, 'code': 40140125}, 200)
        return FakeResponse({'state': True, 'data': {'count': 3000}}, 200)

    http.route('GET', QUOTA_URL, handler=quota)
    with hidrive.app.test_request_context('/'):
        hidrive.g.current_user = admin
        body, status, error = hidrive._open115_offline('GET', '/open/offline/get_quota_info')
        assert (status, error) == (200, '')
        # A second request that failed on the previous generation reuses it.
        assert hidrive._open115_recover(admin, 1 if migrated else 0)
    assert body['data']['count'] == 3000
    assert len(seen) == 2
    assert len(calls) == 1
    assert calls[0]['json'] == {'path': hidrive.OPENLIST_115PAN_PATH,
                              'password': '', 'page': 1, 'per_page': 1, 'refresh': True}
    assert calls[0]['allow_redirects'] is False
    assert calls[0]['timeout'] == (2, 4)
    assert calls[0]['headers']['Authorization'] == 'openlist-admin-fixture'
    with hidrive.connect_db() as db:
        snap = user_115.open_snapshot(db, admin.id, fernet=hidrive.load_fernet())
    assert snap.access_token == 'owner-rotated-access-fixture'
    assert snap.origin == user_115.ORIGIN_OPENLIST_LEGACY


@pytest.mark.parametrize('response', [
    FakeResponse({'code': 200, 'data': {}}, 200),
    FakeResponse({'code': 403}, 200),
    FakeResponse({'code': 200}, 302),
    FakeResponse([], 200),
])
def test_unchanged_pair_never_counts_as_recovery_and_cooldown_survives_calls(
        client, hidrive, http, monkeypatch, workspace, response):
    admin = setup_legacy(hidrive, monkeypatch, migrated=True)
    calls = []
    http.route('POST', hidrive.OPENLIST_URL + '/api/fs/list',
               handler=lambda **kw: (calls.append(kw), response)[1])
    with hidrive.app.test_request_context('/'):
        hidrive.g.current_user = admin
        assert hidrive._open115_recover(admin, 1) is False
        assert hidrive._open115_recover(admin, 1) is False
    assert len(calls) == 1
    with hidrive.connect_db() as db:
        snap = user_115.open_snapshot(db, admin.id, fernet=hidrive.load_fernet())
    assert snap.version == 1
    assert snap.access_token == LEGACY_TOKEN


def test_sync_with_new_pair_does_not_list_owner(client, hidrive, monkeypatch, workspace):
    admin = setup_legacy(hidrive, monkeypatch)
    def sync():
        hidrive.secret_set('115_open_access_token', 'new-access-fixture')
        return True
    monkeypatch.setattr(hidrive, 'sync_openlist_credentials_from_source', sync)
    with hidrive.app.test_request_context('/'):
        hidrive.g.current_user = admin
        assert hidrive._open115_recover(admin, 0)


def test_disconnected_admin_does_not_list_owner(client, hidrive, http, monkeypatch, workspace):
    admin = setup_legacy(hidrive, monkeypatch, migrated=True)
    with hidrive.connect_db() as db:
        db.execute("UPDATE user_115_profile SET open_state='disconnected' WHERE user_id=?", (admin.id,))
        db.commit()
    with hidrive.app.test_request_context('/'):
        hidrive.g.current_user = admin
        assert hidrive._open115_recover(admin, 1) is False


def test_owner_timeout_keeps_pair_and_returns_failure(client, hidrive, http, monkeypatch, workspace):
    import requests
    admin = setup_legacy(hidrive, monkeypatch, migrated=True)
    def timeout(**kw):
        raise requests.Timeout('upstream-timeout-fixture')
    http.route('POST', hidrive.OPENLIST_URL + '/api/fs/list', handler=timeout)
    with hidrive.app.test_request_context('/'):
        hidrive.g.current_user = admin
        assert hidrive._open115_recover(admin, 1) is False


def test_owner_rotation_is_used_even_if_listing_then_times_out(client, hidrive, http, monkeypatch, workspace):
    import requests
    admin = setup_legacy(hidrive, monkeypatch, migrated=True)
    def owner(**kw):
        hidrive.secret_set('115_open_access_token', 'before-timeout-access-fixture')
        hidrive.secret_set('115_open_refresh_token', 'before-timeout-refresh-fixture')
        raise requests.Timeout('listing-timeout-fixture')
    http.route('POST', hidrive.OPENLIST_URL + '/api/fs/list', handler=owner)
    with hidrive.app.test_request_context('/'):
        hidrive.g.current_user = admin
        assert hidrive._open115_recover(admin, 1)
    with hidrive.connect_db() as db:
        snap = user_115.open_snapshot(db, admin.id, fernet=hidrive.load_fernet())
    assert snap.access_token == 'before-timeout-access-fixture'


def test_cooldown_expiry_permits_one_new_owner_attempt(client, hidrive, http, monkeypatch, workspace):
    admin = setup_legacy(hidrive, monkeypatch, migrated=True)
    clock = [hidrive.utc_now()]
    monkeypatch.setattr(hidrive, 'utc_now', lambda: clock[0])
    calls = []
    http.route('POST', hidrive.OPENLIST_URL + '/api/fs/list',
               handler=lambda **kw: (calls.append(kw), FakeResponse({'code': 403}, 200))[1])
    with hidrive.app.test_request_context('/'):
        hidrive.g.current_user = admin
        assert not hidrive._open115_recover(admin, 1)
        clock[0] += 59
        assert not hidrive._open115_recover(admin, 1)
        assert len(calls) == 1
        clock[0] += 2
        assert not hidrive._open115_recover(admin, 1)
    assert len(calls) == 2


def test_disconnect_during_owner_call_is_not_undone(client, hidrive, http, monkeypatch, workspace):
    admin = setup_legacy(hidrive, monkeypatch, migrated=True)
    def owner(**kw):
        with hidrive.connect_db() as db:
            user_115.secret_clear(db, admin.id, user_115.OPEN_ACCESS_SECRET, user_115.OPEN_REFRESH_SECRET)
            db.execute("UPDATE user_115_profile SET open_state='disconnected' WHERE user_id=?", (admin.id,))
            db.commit()
        hidrive.secret_set('115_open_access_token', 'late-access-fixture')
        return FakeResponse({'code': 200}, 200)
    http.route('POST', hidrive.OPENLIST_URL + '/api/fs/list', handler=owner)
    with hidrive.app.test_request_context('/'):
        hidrive.g.current_user = admin
        assert not hidrive._open115_recover(admin, 1)
    with hidrive.connect_db() as db:
        snap = user_115.open_snapshot(db, admin.id, fernet=hidrive.load_fernet())
    assert snap.open_state == 'disconnected'
    assert snap.access_token is None


def test_unmigrated_parallel_requests_share_new_generation(client, hidrive, http, monkeypatch, workspace):
    import threading
    import time
    admin = setup_legacy(hidrive, monkeypatch)
    calls = []
    def owner(**kw):
        calls.append(kw)
        time.sleep(.05)
        hidrive.secret_set('115_open_access_token', 'parallel-access-fixture')
        return FakeResponse({'code': 200}, 200)
    http.route('POST', hidrive.OPENLIST_URL + '/api/fs/list', handler=owner)
    start = threading.Barrier(4)
    outcomes = []
    def worker():
        start.wait(timeout=5)
        with hidrive.app.test_request_context('/'):
            hidrive.g.current_user = admin
            outcomes.append(hidrive._open115_recover(admin, 0))
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)
    assert outcomes == [True] * 4
    assert len(calls) == 1


def test_adoption_holds_write_lock_before_checking_disconnect(client, hidrive, monkeypatch, workspace):
    import sqlite3
    admin = setup_legacy(hidrive, monkeypatch)
    def sync():
        hidrive.secret_set('115_open_access_token', 'locked-access-fixture')
        return True
    monkeypatch.setattr(hidrive, 'sync_openlist_credentials_from_source', sync)
    original = hidrive.user_115.store_open_pair
    checked = []
    def store(db, *args, **kwargs):
        assert db.in_transaction
        other = sqlite3.connect(hidrive.DB_PATH, timeout=.01)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
        finally:
            other.close()
        checked.append(True)
        return original(db, *args, **kwargs)
    monkeypatch.setattr(hidrive.user_115, 'store_open_pair', store)
    with hidrive.app.test_request_context('/'):
        hidrive.g.current_user = admin
        assert hidrive._open115_recover(admin, 0)
    assert checked == [True]
