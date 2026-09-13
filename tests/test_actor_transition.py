"""V02: what changes for the administrator the moment a local session exists.

Before the bridge, an administrator is attributed by their Cloudflare Access
subject; afterwards by ``user:<id>``. The account, its credentials and its
history are the same -- but a scan challenge that was already in flight was
filed under the old actor, and has to be restarted. These tests pin that
transition so it is a documented step rather than a surprise on the day.

Every assertion, subject and QR value below is invented for the test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service as auth  # noqa: E402
from conftest import FakeResponse  # noqa: E402

QR_TOKEN_URL = "https://qrcodeapi.115.com/api/1.0/web/1.0/token/"
QR_STATUS_URL = "https://qrcodeapi.115.com/get/status/"
SESSION_COOKIE = "__Host-hidrive_session"
ACCESS_SUBJECT = "cf-subject-fixture"
MEMBER_PASSWORD = "Correct1Horse"


@pytest.fixture
def bridge(hidrive, monkeypatch):
    """`access` mode with a stub verifier: the production shape, where the
    administrator arrives with an Access assertion and may then exchange it
    for a local session at /auth/google."""
    monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
    principal = {"sub": ACCESS_SUBJECT, "email": hidrive.ADMIN_EMAIL, "email_verified": True}
    monkeypatch.setattr(
        hidrive, "verify_access_jwt",
        lambda token: dict(principal) if token == "valid-assertion"
        else (_ for _ in ()).throw(PermissionError("bad")),
    )
    return principal


ACCESS_HEADERS = {"Cf-Access-Jwt-Assertion": "valid-assertion"}


def _write_headers(client, extra=None):
    headers = dict(ACCESS_HEADERS, **(extra or {}))
    token = client.get("/api/csrf", headers=headers).get_json()["token"]
    return {**headers, "Origin": "https://hidrive.test", "X-CSRF-Token": token}


def _start_scan(client, http, headers):
    http.route("GET", QR_TOKEN_URL, {"state": True, "data": {
        "uid": "qr-uid-fixture-not-real", "time": 111, "sign": "qr-sign-fixture-not-real"}})
    # Still waiting on the phone: enough for a status poll to answer 200.
    http.route("GET", QR_STATUS_URL, {"state": True, "data": {"status": 0}})
    response = client.post("/api/115/reauth/start", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["challenge_id"]


class TestTheActorItself:
    def test_before_the_bridge_it_is_the_access_subject(self, client, hidrive, bridge):
        with hidrive.app.test_request_context("/", headers=ACCESS_HEADERS):
            hidrive.g.principal = dict(bridge)
            assert hidrive.actor_id() == ACCESS_SUBJECT

    def test_after_the_bridge_it_is_the_user_id(self, client, hidrive, http, bridge):
        assert client.get("/auth/google", headers=ACCESS_HEADERS).status_code == 302
        with hidrive.connect_db() as db:
            admin_id = int(db.execute("SELECT id FROM auth_user WHERE role='admin'").fetchone()["id"])
        body = client.get("/api/me", headers=ACCESS_HEADERS).get_json()
        assert body["id"] == admin_id and body["role"] == "admin"
        # What the next request files under is the actor -- read off a row
        # the app itself wrote, without reading any token.
        _start_scan(client, http, _write_headers(client))
        with hidrive.connect_db() as db:
            actor = db.execute("SELECT actor FROM reauth_challenges ORDER BY rowid DESC LIMIT 1").fetchone()["actor"]
        assert actor == f"user:{admin_id}"

    def test_it_is_the_same_account_and_the_same_credentials(self, client, hidrive, bridge):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        assert client.get("/auth/google", headers=ACCESS_HEADERS).status_code == 302
        with hidrive.app.test_request_context("/"):
            hidrive.g.principal = dict(bridge)
            hidrive.g.current_user = auth.CurrentUser(
                id=1, email=hidrive.ADMIN_EMAIL, role="admin", status="active")
            assert hidrive.user_115_cookie(hidrive.current_user()) == "the-administrators-cookie"


class TestAScanCaughtInTheTransition:
    def test_an_in_flight_challenge_must_be_restarted(self, client, hidrive, http, bridge):
        """The row was filed under the Access subject; after the bridge the
        same person is a different actor, so the old challenge is no longer
        theirs to poll. Restarting is the documented step."""
        challenge_id = _start_scan(client, http, _write_headers(client))
        assert client.get(f"/api/115/reauth/status?challenge_id={challenge_id}",
                          headers=ACCESS_HEADERS).status_code == 200

        assert client.get("/auth/google", headers=ACCESS_HEADERS).status_code == 302
        after = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}", headers=ACCESS_HEADERS)
        assert after.status_code == 404
        assert after.get_json()["code"] == "REAUTH_NOT_FOUND"

    def test_the_stale_row_never_blocks_a_fresh_start(self, client, hidrive, http, bridge):
        """A 409 REAUTH_IN_PROGRESS here would strand the administrator: the
        old row can neither be polled nor cancelled under the new actor."""
        _start_scan(client, http, _write_headers(client))
        assert client.get("/auth/google", headers=ACCESS_HEADERS).status_code == 302
        restarted = client.post("/api/115/reauth/start", headers=_write_headers(client))
        assert restarted.status_code == 200, restarted.get_json()
        with hidrive.connect_db() as db:
            actors = [row["actor"] for row in db.execute("SELECT actor FROM reauth_challenges ORDER BY rowid")]
        assert actors[0] == ACCESS_SUBJECT, "the old row is left exactly as it was"
        assert actors[-1].startswith("user:")

    def test_nobody_else_inherits_the_orphaned_challenge(self, client, hidrive, http, bridge, monkeypatch):
        """The stale row is not up for grabs: a member -- whose actor is
        their own user id -- gets the same 404 as any stranger."""
        challenge_id = _start_scan(client, http, _write_headers(client))
        monkeypatch.setattr(hidrive, "AUTH_MODE", "app")
        member = hidrive.app.test_client()

        def signed(payload):
            token = member.get("/api/csrf").get_json()["token"]
            return {"Origin": hidrive.PUBLIC_ORIGIN, "X-CSRF-Token": token}

        assert member.post("/api/auth/register", headers=signed(None), json={
            "email": "m@example.test", "password": MEMBER_PASSWORD,
            "confirm_password": MEMBER_PASSWORD}).status_code == 202
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            row = db.execute("SELECT id FROM auth_user WHERE email_norm='m@example.test'").fetchone()
            auth.approve_user(db, int(row["id"]), approver_id=admin_id, now=2)
        assert member.post("/api/auth/login", headers=signed(None), json={
            "email": "m@example.test", "password": MEMBER_PASSWORD}).status_code == 200
        response = member.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
        assert response.status_code == 404
        assert response.get_json()["code"] == "REAUTH_NOT_FOUND"


class TestHistoryIsLeftAlone:
    def test_old_audit_rows_keep_the_actor_they_were_written_with(self, client, hidrive, http, bridge):
        """No batch rewrite: the trail says who was attributed at the time."""
        _start_scan(client, http, _write_headers(client))
        with hidrive.connect_db() as db:
            before = [row["actor"] for row in db.execute(
                "SELECT actor FROM audit_log WHERE action='115.reauth.start' ORDER BY id")]
        assert before == [ACCESS_SUBJECT]
        assert client.get("/auth/google", headers=ACCESS_HEADERS).status_code == 302
        with hidrive.connect_db() as db:
            after = [row["actor"] for row in db.execute(
                "SELECT actor FROM audit_log WHERE action='115.reauth.start' ORDER BY id")]
        assert after == before
