"""Admin-only OpenList reuse, independent of the step-A credential slot."""
import auth_service as auth
import user_115


def setup_admin(hidrive):
    with hidrive.connect_db() as db:
        uid = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        user_115.secret_set(db, uid, user_115.COOKIE_SECRET, 'cookie-admin-fixture',
                            fernet=hidrive.load_fernet(), now=1)
    hidrive.secret_set('115_open_access_token', 'legacy-access-fixture')
    hidrive.secret_set('115_open_refresh_token', 'legacy-refresh-fixture')
    return auth.CurrentUser(id=uid, email=hidrive.ADMIN_EMAIL, role='admin', status='active')


def test_cookie_only_admin_reuses_openlist_for_status_and_capabilities(client, hidrive):
    admin = setup_admin(hidrive)
    assert hidrive._user_115_state(admin.id, admin.role) == (True, True)
    status = hidrive._open115_status(admin)
    assert status['browse']['state'] == 'connected'
    assert status['browse']['source'] == 'openlist'
    assert hidrive._me_payload(admin)['capabilities']['own_115_cloud_download'] is True
    assert hidrive._open115_credential(admin)[0] == 'legacy-access-fixture'


def test_member_cannot_borrow_admin_openlist_pair(client, hidrive):
    setup_admin(hidrive)
    member = auth.CurrentUser(id=99999, email='member@example.test', role='member', status='active')
    assert hidrive._user_115_state(member.id, member.role) == (False, False)
    assert hidrive._open115_status(member)['browse']['state'] != 'connected'
    assert hidrive._open115_credential(member)[0] is None


def test_disconnect_still_blocks_admin_legacy_fallback(client, hidrive):
    admin = setup_admin(hidrive)
    with hidrive.connect_db() as db:
        user_115.remember_open_state(db, admin.id, state=user_115.STATE_DISCONNECTED,
            expires_at=None, error_code=None, now=2)
    assert hidrive._user_115_state(admin.id, admin.role) == (True, False)
    assert hidrive._open115_status(admin)['browse']['state'] != 'connected'
    assert hidrive._open115_credential(admin)[0] is None


def test_scanned_admin_pair_takes_priority_without_changing_openlist(client, hidrive):
    admin = setup_admin(hidrive)
    with hidrive.connect_db() as db:
        user_115.store_open_pair(db, admin.id, 'scanned-access-fixture', 'scanned-refresh-fixture',
            fernet=hidrive.load_fernet(), now=2, origin=user_115.ORIGIN_OWN_APP)
    assert hidrive._open115_status(admin)['browse']['source'] == 'scan'
    assert hidrive._open115_credential(admin)[0] == 'scanned-access-fixture'
    assert hidrive.secret_get('115_open_access_token') == 'legacy-access-fixture'


def test_old_admin_pair_without_origin_is_labelled_like_recovery(client, hidrive):
    admin = setup_admin(hidrive)
    with hidrive.connect_db() as db:
        user_115.store_open_pair(db, admin.id, 'old-access-fixture', 'old-refresh-fixture',
            fernet=hidrive.load_fernet(), now=2, origin=user_115.ORIGIN_OPENLIST_LEGACY)
        db.execute('UPDATE user_115_profile SET open_token_origin=NULL WHERE user_id=?', (admin.id,))
    assert hidrive._open115_status(admin)['browse']['source'] == 'openlist'
