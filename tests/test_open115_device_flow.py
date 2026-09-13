"""Device-flow transactions against an isolated SQLite DB; no real APP grant."""
from contextlib import closing
import sqlite3

import pytest
from cryptography.fernet import Fernet

import auth_service as auth
import user_115 as u


class Adapter:
    def __init__(self):
        self.state = "pending"
        self.exchanges = 0
        self.probes = 0
        self.during_exchange = lambda: None

    def start_device(self, challenge):
        return {"uid": "uid-fixture", "time": 1, "sign": "sign-fixture", "qrcode": "qr-fixture"}

    def poll_device(self, **kwargs):
        return self.state

    def exchange_device(self, **kwargs):
        self.exchanges += 1
        self.during_exchange()
        return u.OpenToken("access-fixture", "refresh-fixture", 7200)

    def verify_token(self, token):
        self.probes += 1


@pytest.fixture
def flow(tmp_path):
    path = tmp_path / "device.sqlite"
    def connect():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return db
    with closing(connect()) as db, db:
        auth.ensure_schema(db)
        admin = auth.ensure_admin_user(db, email="admin@example.test", now=1)
    clock = [100]
    adapter = Adapter()
    config = u.OpenAppConfig(client_id="client-fixture", device_flow_verified=True)
    result = u.DeviceAuthorization(connect, fernet=Fernet(Fernet.generate_key()),
        adapter=adapter, config=config, clock=lambda: clock[0])
    result.test_user = admin
    result.test_clock = clock
    return result


def test_pending_scanned_confirmed_and_replay(flow):
    item = flow.start(flow.test_user)
    assert set(item) == {"challenge_id", "qrcode", "expires_in"}
    cid = item["challenge_id"]
    assert flow.poll(flow.test_user, cid) == "pending"
    flow.test_clock[0] += 3
    flow.adapter.state = "scanned"
    assert flow.poll(flow.test_user, cid) == "scanned"
    assert flow.adapter.exchanges == 0
    flow.test_clock[0] += 3
    flow.adapter.state = "confirmed"
    assert flow.poll(flow.test_user, cid) == "connected"
    assert flow.poll(flow.test_user, cid) == "connected"
    assert flow.adapter.exchanges == flow.adapter.probes == 1
    with closing(flow.connect()) as db:
        pair = u.credentials_for(db, flow.test_user, fernet=flow.fernet)
        row = db.execute("SELECT * FROM user_115_oauth_state").fetchone()
    assert (pair.access_token, pair.refresh_token) == ("access-fixture", "refresh-fixture")
    assert row["consumed_at"] is not None
    assert row["code_verifier_ciphertext"] is None


def test_cross_user_cannot_poll_or_cancel(flow):
    cid = flow.start(flow.test_user)["challenge_id"]
    with pytest.raises(u.Open115Error):
        flow.poll(flow.test_user + 1, cid)
    with pytest.raises(u.Open115Error):
        flow.cancel(flow.test_user + 1, cid)
    assert flow.poll(flow.test_user, cid) == "pending"


@pytest.mark.parametrize("stop", ["cancel", "expire", "disable", "disconnect", "replace"])
def test_late_exchange_cannot_overwrite_a_new_decision(flow, stop):
    cid = flow.start(flow.test_user)["challenge_id"]
    flow.adapter.state = "confirmed"
    def race():
        if stop == "cancel":
            flow.cancel(flow.test_user, cid)
        elif stop == "expire":
            flow.test_clock[0] += 601
        else:
            with closing(flow.connect()) as db, db:
                if stop == "disable":
                    db.execute("UPDATE auth_user SET status='disabled' WHERE id=?", (flow.test_user,))
                elif stop == "disconnect":
                    u.secret_clear(db, flow.test_user, u.OPEN_ACCESS_SECRET, u.OPEN_REFRESH_SECRET)
                else:
                    u.secret_set(db, flow.test_user, u.OPEN_ACCESS_SECRET, "newer-fixture",
                        fernet=flow.fernet, now=101)
    flow.adapter.during_exchange = race
    assert flow.poll(flow.test_user, cid) != "connected"
    with closing(flow.connect()) as db:
        pair = u.credentials_for(db, flow.test_user, fernet=flow.fernet)
    assert pair.access_token == ("newer-fixture" if stop == "replace" else None)
    assert pair.refresh_token is None
    assert flow.adapter.exchanges == 1


def test_cancellation_before_poll_never_exchanges(flow):
    cid = flow.start(flow.test_user)["challenge_id"]
    assert flow.cancel(flow.test_user, cid) == "cancelled"
    assert flow.poll(flow.test_user, cid) == "cancelled"
    assert flow.adapter.exchanges == 0


def test_probe_failure_preserves_pair_and_cannot_repeat_exchange(flow):
    with closing(flow.connect()) as db, db:
        u.secret_set(db, flow.test_user, u.OPEN_ACCESS_SECRET, "old-access-fixture", fernet=flow.fernet, now=1)
        u.secret_set(db, flow.test_user, u.OPEN_REFRESH_SECRET, "old-refresh-fixture", fernet=flow.fernet, now=1)
    cid = flow.start(flow.test_user)["challenge_id"]
    flow.adapter.state = "confirmed"
    def refused(token):
        raise u.Open115Error("probe_failed")
    flow.adapter.verify_token = refused
    assert flow.poll(flow.test_user, cid) == "failed"
    assert flow.poll(flow.test_user, cid) == "failed"
    with closing(flow.connect()) as db:
        pair = u.credentials_for(db, flow.test_user, fernet=flow.fernet)
    assert (pair.access_token, pair.refresh_token) == ("old-access-fixture", "old-refresh-fixture")
    assert flow.adapter.exchanges == 1


def test_new_start_replaces_old_and_start_is_rate_limited(flow):
    cid = flow.start(flow.test_user)["challenge_id"]
    with pytest.raises(u.Open115Error, match="rate_limited"):
        flow.start(flow.test_user)
    flow.test_clock[0] += 11
    newer = flow.start(flow.test_user)["challenge_id"]
    assert newer != cid
    assert flow.poll(flow.test_user, cid) == "cancelled"


def test_poll_interval_is_enforced(flow):
    cid = flow.start(flow.test_user)["challenge_id"]
    assert flow.poll(flow.test_user, cid) == "pending"
    flow.adapter.state = "confirmed"
    assert flow.poll(flow.test_user, cid) == "pending"
    assert flow.adapter.exchanges == 0


def test_lease_takeover_prevents_commit(flow):
    cid = flow.start(flow.test_user)["challenge_id"]
    flow.adapter.state = "confirmed"
    def takeover():
        with closing(flow.connect()) as db, db:
            db.execute("UPDATE user_lock SET holder='new-holder-fixture' WHERE lock_key=?",
                       (f"115_open_refresh:{flow.test_user}",))
    flow.adapter.during_exchange = takeover
    assert flow.poll(flow.test_user, cid) != "connected"
    with closing(flow.connect()) as db:
        assert not u.credentials_for(db, flow.test_user, fernet=flow.fernet).has_open_token
        assert db.execute("SELECT holder FROM user_lock").fetchone()["holder"] == "new-holder-fixture"


def test_concurrent_poll_sees_busy_and_cannot_exchange_twice(flow):
    cid = flow.start(flow.test_user)["challenge_id"]
    flow.adapter.state = "confirmed"
    seen = []
    flow.adapter.during_exchange = lambda: seen.append(flow.poll(flow.test_user, cid))
    assert flow.poll(flow.test_user, cid) == "connected"
    assert seen == ["busy"] and flow.adapter.exchanges == 1


@pytest.mark.parametrize("outcome", ["confirmed", "pending", "error"])
def test_lost_poll_lease_never_consumes_or_terminates_new_holders_challenge(flow, outcome):
    cid = flow.start(flow.test_user)["challenge_id"]
    def takeover(**kwargs):
        with closing(flow.connect()) as db, db:
            db.execute("UPDATE user_lock SET holder='new-holder-fixture'")
        if outcome == "error":
            raise u.Open115Error("network_error")
        return outcome
    flow.adapter.poll_device = takeover
    assert flow.poll(flow.test_user, cid) == "busy"
    with closing(flow.connect()) as db:
        row = db.execute("SELECT status,consumed_at FROM user_115_oauth_state").fetchone()
    assert row["status"] == "pending" and row["consumed_at"] is None
    assert flow.adapter.exchanges == 0


@pytest.fixture
def api_device(client, hidrive, monkeypatch):
    adapter = Adapter()
    config = u.OpenAppConfig(client_id="client-fixture", device_flow_verified=True)
    monkeypatch.setattr(hidrive, "_open115_config", lambda: config)
    monkeypatch.setattr(hidrive.user_115, "Open115Adapter", lambda *a, **kw: adapter)
    return adapter


def test_routes_drive_exchange_without_returning_tokens(client, hidrive, api_device):
    started = client.post("/api/me/115/open/start", json={})
    assert started.status_code == 200
    cid = started.get_json()["challenge_id"]
    api_device.state = "confirmed"
    response = client.post("/api/me/115/open/status", json={"challenge_id": cid})
    assert response.status_code == 200 and response.get_json()["status"] == "connected"
    assert "access-fixture" not in response.get_data(as_text=True)
    assert "refresh-fixture" not in response.get_data(as_text=True)
    assert client.get("/api/me/115/open/status").status_code == 405


def test_routes_cancel_without_exchange(client, api_device):
    cid = client.post("/api/me/115/open/start", json={}).get_json()["challenge_id"]
    assert client.post("/api/me/115/open/cancel", json={"challenge_id": cid}).get_json()["status"] == "cancelled"
    api_device.state = "confirmed"
    assert client.post("/api/me/115/open/status", json={"challenge_id": cid}).get_json()["status"] == "cancelled"
    assert api_device.exchanges == 0


def test_oauth_code_only_config_does_not_offer_unimplemented_app_scan(client, hidrive, monkeypatch):
    config = u.OpenAppConfig(client_id="client-fixture", client_secret="secret-fixture",
                             redirect_uri="https://example.test/callback")
    monkeypatch.setattr(hidrive, "_open115_config", lambda: config)
    status = client.get("/api/me/115/status").get_json()
    assert status["browse"]["state"] == "blocked"
    assert status["browse"]["blocked_reason"] == u.BLOCKED_NO_VERIFICATION
    response = client.post("/api/me/115/open/start", json={})
    assert response.status_code == 409
    assert "authorize_url" not in response.get_json()
    with closing(hidrive.connect_db()) as db:
        assert db.execute("SELECT count(*) FROM user_115_oauth_state").fetchone()[0] == 0


@pytest.mark.parametrize("route", ["status", "cancel"])
@pytest.mark.parametrize("challenge_id", ["\ud800" * 32, "汉" * 32, "!" * 43, None, {}, "short"],
                         ids=["surrogate", "unicode", "punctuation", "null", "object", "short"])
def test_invalid_challenge_input_is_not_a_server_error(client, api_device, route, challenge_id):
    response = client.post("/api/me/115/open/" + route, json={"challenge_id": challenge_id})
    assert response.status_code == 404
    assert api_device.exchanges == 0


@pytest.mark.parametrize("route", ["start", "status", "cancel"])
def test_routes_require_session_in_app_mode(client, hidrive, monkeypatch, route):
    monkeypatch.setattr(hidrive, "AUTH_MODE", "app")
    assert client.post("/api/me/115/open/" + route, json={}).status_code == 401


def _member_session(client, hidrive, email):
    with closing(hidrive.connect_db()) as db, db:
        user_id = auth.ensure_admin_user(db, email=email, now=hidrive.utc_now())
        db.execute("UPDATE auth_user SET role='member' WHERE id=?", (user_id,))
        session = auth.issue_session(db, user_id, now=hidrive.utc_now())
    client.set_cookie(hidrive.SESSION_COOKIE_NAME, session.token)
    csrf = auth.issue_csrf(hidrive.csrf_key(), subject="session:" + auth.hash_token(session.csrf_secret)[:32],
                           now=hidrive.utc_now())
    return user_id, {"X-CSRF-Token": csrf, "Origin": hidrive.PUBLIC_ORIGIN}


@pytest.mark.parametrize("route", ["start", "status", "cancel"])
def test_member_write_routes_require_csrf(client, hidrive, api_device, monkeypatch, route):
    _member_session(client, hidrive, "member@example.test")
    monkeypatch.setattr(hidrive, "AUTH_MODE", "app")
    assert client.post("/api/me/115/open/" + route, json={},
                       headers={"Origin": hidrive.PUBLIC_ORIGIN}).status_code == 403


def test_real_member_session_can_scan_and_other_member_cannot_poll(client, hidrive, api_device, monkeypatch):
    first, headers = _member_session(client, hidrive, "first@example.test")
    monkeypatch.setattr(hidrive, "AUTH_MODE", "app")
    result = client.post("/api/me/115/open/start", json={}, headers=headers)
    assert result.status_code == 200
    cid = result.get_json()["challenge_id"]
    api_device.state = "confirmed"
    assert client.post("/api/me/115/open/status", json={"challenge_id": cid},
                       headers=headers).get_json()["status"] == "connected"
    second, second_headers = _member_session(client, hidrive, "second@example.test")
    assert first != second
    for route in ("status", "cancel"):
        assert client.post("/api/me/115/open/" + route,
            json={"challenge_id": cid}, headers=second_headers).status_code == 404
    assert api_device.exchanges == 1
    with closing(hidrive.connect_db()) as db:
        assert u.credentials_for(db, first, fernet=hidrive.load_fernet()).has_open_token
        assert not u.credentials_for(db, second, fernet=hidrive.load_fernet()).has_open_token
