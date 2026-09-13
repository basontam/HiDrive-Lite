"""Phase 1 routes: applications, approval, sessions, the member policy
switch, and the CSRF key change that comes with them.

The suite runs against the app's own test client and a throwaway database.
No password, cookie or token value is asserted on; the migration test never
touches a production path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import sqlite3
import subprocess
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service as auth  # noqa: E402

GOOD_PASSWORD = "Correct1Horse"
SESSION_COOKIE = "__Host-hidrive_session"


@pytest.fixture
def access_mode(hidrive, monkeypatch):
    """The production authentication mode, with a stub Access verifier --
    the only mode where check_csrf() actually runs (mirrors the fixture in
    tests/test_access_guard.py)."""
    monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
    monkeypatch.setattr(
        hidrive, "verify_access_jwt",
        lambda token: {"sub": "owner", "email": "owner@example.test"} if token == "valid-assertion"
        else (_ for _ in ()).throw(PermissionError("bad")),
    )
    return hidrive


def _register(client, email=("m@example.test"), password=GOOD_PASSWORD, **extra):
    body = {"email": email, "password": password, "confirm_password": password}
    body.update(extra)
    return client.post("/api/auth/register", json=body)


def _approve_everyone(hidrive):
    """Approve every pending member, the way the administrator would."""
    with hidrive.connect_db() as db:
        admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        for row in db.execute("SELECT id FROM auth_user WHERE status='pending'").fetchall():
            auth.approve_user(db, int(row["id"]), approver_id=admin_id, now=2)


# ---------------------------------------------------------------------------
# applications
# ---------------------------------------------------------------------------


class TestRegister:
    def test_an_application_is_accepted_and_lands_pending(self, client, hidrive):
        response = _register(client)
        assert response.status_code == 202
        assert response.get_json()["status"] == "pending"
        with hidrive.connect_db() as db:
            row = db.execute("SELECT role, status FROM auth_user WHERE email_norm='m@example.test'").fetchone()
        assert (row["role"], row["status"]) == ("member", "pending")

    def test_it_sets_no_session_cookie(self, client):
        response = _register(client)
        assert SESSION_COOKIE not in response.headers.get("Set-Cookie", "")

    def test_a_body_cannot_ask_for_a_role_or_an_approval(self, client, hidrive):
        _register(client, email="sneaky@example.test", role="admin", status="active", approved_by=1)
        with hidrive.connect_db() as db:
            row = db.execute("SELECT role, status, approved_by FROM auth_user WHERE email_norm='sneaky@example.test'").fetchone()
        assert (row["role"], row["status"], row["approved_by"]) == ("member", "pending", None)

    def test_a_weak_password_is_refused_with_the_rule_it_broke(self, client):
        response = _register(client, email="weak@example.test", password="abcdefgh")
        assert response.status_code == 400
        assert response.get_json()["code"] == "PASSWORD_REJECTED"

    def test_mismatched_confirmation_is_refused(self, client):
        response = client.post("/api/auth/register", json={
            "email": "mismatch@example.test", "password": GOOD_PASSWORD, "confirm_password": "Other1Password"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "PASSWORD_MISMATCH"

    def test_an_address_already_taken_answers_exactly_like_a_new_one(self, client):
        first = _register(client, email="dup@example.test")
        second = _register(client, email="DUP@example.test")
        assert first.status_code == second.status_code == 202
        assert first.get_json() == second.get_json()

    def test_the_administrator_address_cannot_be_applied_for(self, client, hidrive):
        response = _register(client, email=hidrive.ADMIN_EMAIL)
        assert response.status_code == 202  # same answer, no account
        with hidrive.connect_db() as db:
            row = db.execute("SELECT password_hash FROM auth_user WHERE email_norm=?",
                             (auth.normalize_email(hidrive.ADMIN_EMAIL),)).fetchone()
        assert row is None or row["password_hash"] is None


# ---------------------------------------------------------------------------
# login and session
# ---------------------------------------------------------------------------


class TestLogin:
    def test_an_approved_member_gets_a_session_cookie_with_the_right_flags(self, client, hidrive):
        _register(client)
        _approve_everyone(hidrive)
        response = client.post("/api/auth/login", json={"email": "m@example.test", "password": GOOD_PASSWORD})
        assert response.status_code == 200
        cookie = response.headers.get("Set-Cookie", "")
        assert cookie.startswith(SESSION_COOKIE + "=")
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
        assert "Path=/" in cookie and "Domain=" not in cookie

    def test_the_cookie_value_is_not_what_the_database_stores(self, client, hidrive):
        _register(client)
        _approve_everyone(hidrive)
        response = client.post("/api/auth/login", json={"email": "m@example.test", "password": GOOD_PASSWORD})
        token = response.headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
        with hidrive.connect_db() as db:
            stored = [r["token_hash"] for r in db.execute("SELECT token_hash FROM auth_session")]
        assert token not in stored
        assert auth.hash_token(token) in stored

    @pytest.mark.parametrize("email,password", [
        ("nobody@example.test", GOOD_PASSWORD),
        ("m@example.test", "Wrong1Password"),
    ])
    def test_every_failure_answers_the_same_way(self, client, hidrive, email, password):
        _register(client)
        _approve_everyone(hidrive)
        response = client.post("/api/auth/login", json={"email": email, "password": password})
        assert response.status_code == 401
        assert response.get_json()["message"] == auth.LOGIN_FAILED_MESSAGE

    def test_a_pending_member_cannot_log_in_until_approved(self, client, hidrive):
        _register(client)
        first = client.post("/api/auth/login", json={"email": "m@example.test", "password": GOOD_PASSWORD})
        assert first.status_code == 401
        _approve_everyone(hidrive)
        assert client.post("/api/auth/login", json={"email": "m@example.test", "password": GOOD_PASSWORD}).status_code == 200

    def test_logging_out_revokes_the_session(self, client, hidrive):
        _register(client)
        _approve_everyone(hidrive)
        client.post("/api/auth/login", json={"email": "m@example.test", "password": GOOD_PASSWORD})
        assert client.post("/api/auth/logout").status_code == 200
        with hidrive.connect_db() as db:
            revoked = db.execute("SELECT COUNT(*) AS c FROM auth_session WHERE revoked_at IS NOT NULL").fetchone()["c"]
        assert revoked == 1

    def test_the_reason_a_login_failed_reaches_the_audit_trail_only(self, client, hidrive, audit_rows):
        _register(client)
        client.post("/api/auth/login", json={"email": "m@example.test", "password": GOOD_PASSWORD})
        rows = audit_rows("auth.login")
        assert rows and rows[-1]["detail"] == "pending"
        assert rows[-1]["status"] == "rejected"


# ---------------------------------------------------------------------------
# /api/me and the admin surface
# ---------------------------------------------------------------------------


class TestMeAndAdmin:
    def test_pending_summary_counts_all_requests_without_returning_users(self, client, hidrive):
        with hidrive.connect_db() as db:
            db.executemany(
                "INSERT INTO auth_user(email_norm,email_display,role,status,created_at) VALUES(?,?,'member',?,1)",
                [(f"pending-{i}@example.test", f"pending-{i}@example.test", "pending") for i in range(501)]
                + [("active@example.test", "active@example.test", "active"),
                   ("rejected@example.test", "rejected@example.test", "rejected"),
                   ("disabled@example.test", "disabled@example.test", "disabled")])
        response = client.get("/api/admin/users?summary=1")
        assert response.get_json() == {"success": True, "pending_count": 501}
        assert response.headers["Cache-Control"] == "no-store"
        listing = client.get("/api/admin/users").get_json()
        assert len(listing["users"]) == 500
        assert listing["pending_count"] == 501

    @pytest.mark.parametrize("action", ["approve", "reject"])
    def test_handled_application_returns_zero_pending(self, client, hidrive, action):
        _register(client)
        user = next(u for u in client.get("/api/admin/users").get_json()["users"] if u["status"] == "pending")
        response = client.post(f"/api/admin/users/{user['id']}/{action}")
        assert response.get_json()["pending_count"] == 0
        assert client.get("/api/admin/users?summary=1").get_json()["pending_count"] == 0

    def test_pending_summary_is_admin_only(self, client, hidrive, monkeypatch):
        _register(client)
        _approve_everyone(hidrive)
        client.post("/api/auth/login", json={"email": "m@example.test", "password": GOOD_PASSWORD})
        assert client.get("/api/admin/users?summary=1").status_code == 403
        assert 'id="settingsPendingBadge"' not in client.get("/").get_data(as_text=True)
        assert 'id="usersNavPendingBadge"' not in client.get("/").get_data(as_text=True)
        client.delete_cookie(SESSION_COOKIE)
        monkeypatch.setattr(hidrive, "AUTH_MODE", "hybrid")
        assert client.get("/api/admin/users?summary=1").status_code == 401

    def test_admin_shell_has_initially_hidden_notification(self, client):
        html = client.get("/").get_data(as_text=True)
        assert 'id="settingsPendingBadge"' in html
        assert 'id="usersNavPendingBadge"' in html
        assert 'class="nav-notification" hidden' in html

    def test_me_reports_the_role_and_its_capabilities(self, client):
        payload = client.get("/api/me").get_json()
        assert payload["role"] == "admin"
        caps = payload["capabilities"]
        assert caps["openlist"] is True and caps["global_settings"] is True

    def test_me_never_returns_a_credential(self, client):
        body = json.dumps(client.get("/api/me").get_json())
        for forbidden in ("cookie", "token", "secret", "password"):
            assert forbidden not in body.lower()

    def test_the_admin_can_list_and_approve_an_application(self, client, hidrive):
        _register(client)
        listing = client.get("/api/admin/users").get_json()["users"]
        pending = [u for u in listing if u["status"] == "pending"]
        assert len(pending) == 1
        response = client.post(f"/api/admin/users/{pending[0]['id']}/approve")
        assert response.status_code == 200
        with hidrive.connect_db() as db:
            row = db.execute("SELECT status, approved_by FROM auth_user WHERE id=?", (pending[0]["id"],)).fetchone()
        assert row["status"] == "active" and row["approved_by"] is not None

    def test_disabling_a_member_ends_their_session_immediately(self, client, hidrive):
        _register(client)
        _approve_everyone(hidrive)
        login = client.post("/api/auth/login", json={"email": "m@example.test", "password": GOOD_PASSWORD})
        token = login.headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
        with hidrive.connect_db() as db:
            member_id = db.execute("SELECT id FROM auth_user WHERE email_norm='m@example.test'").fetchone()["id"]
        # The client still holds the member's cookie from that login; the
        # administrator acts without it (and would otherwise be refused --
        # which is itself the point of the gate).
        assert client.post(f"/api/admin/users/{member_id}/disable").status_code == 403
        client.delete_cookie(SESSION_COOKIE)
        assert client.post(f"/api/admin/users/{member_id}/disable").status_code == 200
        with hidrive.connect_db() as db:
            assert auth.load_session(db, token, now=3) is None

    def test_the_administrator_row_itself_cannot_be_disabled(self, client, hidrive):
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        response = client.post(f"/api/admin/users/{admin_id}/disable")
        assert response.status_code == 400
        assert response.get_json()["code"] == "ADMIN_IMMUTABLE"

    def test_the_re0_member_switch_is_off_until_the_admin_turns_it_on(self, client, hidrive):
        assert hidrive.allow_member_re0_unlock() is False
        response = client.patch("/api/admin/policies/re0", json={"allow_member_re0_unlock": True})
        assert response.status_code == 200
        assert hidrive.allow_member_re0_unlock() is True

    def test_the_switch_refuses_anything_that_is_not_a_boolean(self, client):
        assert client.patch("/api/admin/policies/re0", json={"allow_member_re0_unlock": "yes"}).status_code == 400

    def test_flipping_the_switch_is_audited_with_the_old_and_new_value(self, client, audit_rows):
        client.patch("/api/admin/policies/re0", json={"allow_member_re0_unlock": True})
        rows = audit_rows("auth.policy.re0")
        assert rows and rows[-1]["detail"] == "False -> True"


# ---------------------------------------------------------------------------
# §5.4 / 2026-09-11 decision 3: the CSRF key moved
# ---------------------------------------------------------------------------


class TestCsrfKey:
    def test_the_token_is_signed_with_the_derived_key_not_fernets_own(self, client, hidrive):
        from cryptography.fernet import Fernet

        token = client.get("/api/csrf").get_json()["token"]
        raw = hidrive.MASTER_KEY_FILE.read_bytes().strip()
        derived = auth.derive_key("csrf/v1", raw)
        with hidrive.app.test_request_context("/"):
            hidrive.g.principal = {"sub": "local", "email": "local"}
            subject = hidrive.csrf_subject()
        assert auth.verify_csrf(derived, token, subject=subject, now=hidrive.utc_now())
        assert not auth.verify_csrf(Fernet(raw)._signing_key, token, subject=subject, now=hidrive.utc_now())

    def test_the_source_no_longer_reaches_into_fernets_private_attribute(self):
        """Checked on the parse tree, not the text: the docstring that records
        what the key used to be is allowed to name it, an expression is not."""
        import ast

        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        uses = [node for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and node.attr == "_signing_key"]
        assert uses == []

    def test_a_token_signed_with_the_old_key_is_refused(self, client, hidrive, access_mode):
        """The release that moves the key makes every token minted before it
        invalid. That is the case the front end must survive without a
        re-login, and the next test pins that behaviour."""
        from cryptography.fernet import Fernet

        headers = {"Cf-Access-Jwt-Assertion": "valid-assertion", "Origin": "https://hidrive.test"}
        raw = hidrive.MASTER_KEY_FILE.read_bytes().strip()
        stale = auth.issue_csrf(Fernet(raw)._signing_key, subject="owner", now=hidrive.utc_now())
        refused = client.post("/api/settings", json={}, headers={**headers, "X-CSRF-Token": stale})
        assert refused.status_code == 403
        assert refused.get_json()["message"] == "invalid CSRF token"
        # A token from the new key is accepted for the same request.
        fresh = client.get("/api/csrf", headers=headers).get_json()["token"]
        assert client.post("/api/settings", json={}, headers={**headers, "X-CSRF-Token": fresh}).status_code == 200


def test_the_front_end_retries_a_rejected_csrf_exactly_once():
    """2026-09-11 decision 3: after a 403 the page re-fetches /api/csrf and
    repeats the identical request once -- the write happens exactly once, the
    session is untouched, and nobody is asked to log in again."""
    import re
    import subprocess

    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"var api = \{([\s\S]*?)\n  \};", js)
    assert match, "expected the api wrapper"
    harness = """
var writes = 0, csrfFetches = 0, csrfValid = false;
function fetch(url, opt) {
  opt = opt || {};
  if (url === "/api/csrf") {
    csrfFetches++; csrfValid = true;
    return Promise.resolve({ ok: true, url: url, headers: { get: function () { return "application/json"; } },
      json: function () { return Promise.resolve({ token: "fresh-token" }); } });
  }
  var accepted = csrfValid && opt.headers["X-CSRF-Token"] === "fresh-token";
  if (accepted) writes++;
  return Promise.resolve({
    ok: accepted, status: accepted ? 200 : 403, url: url,
    headers: { get: function () { return "application/json"; } },
    json: function () {
      return Promise.resolve(accepted ? { success: true } : { message: "invalid CSRF token" });
    }
  });
}
var api = {""" + match.group(1) + """
};
api.csrf = "stale-token";
api.request("/api/library/transfer", { method: "POST", body: "{}" })
  .then(function (d) {
    console.log(JSON.stringify({ ok: !!d.success, writes: writes, csrfFetches: csrfFetches, error: null }));
  })
  .catch(function (e) {
    console.log(JSON.stringify({ ok: false, writes: writes, csrfFetches: csrfFetches, error: e.message }));
  });
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ok"] is True, out
    assert out["writes"] == 1, "the write must happen exactly once"
    assert out["csrfFetches"] == 1, "the token must be re-fetched exactly once"


def test_the_front_end_gives_up_after_one_retry():
    """A second 403 is a real failure, not a loop: it surfaces to the caller
    instead of re-fetching the token again."""
    import re
    import subprocess

    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"var api = \{([\s\S]*?)\n  \};", js)
    harness = """
var csrfFetches = 0, attempts = 0;
function fetch(url, opt) {
  if (url === "/api/csrf") {
    csrfFetches++;
    return Promise.resolve({ ok: true, url: url, headers: { get: function () { return "application/json"; } },
      json: function () { return Promise.resolve({ token: "still-wrong" }); } });
  }
  attempts++;
  return Promise.resolve({ ok: false, status: 403, url: url,
    headers: { get: function () { return "application/json"; } },
    json: function () { return Promise.resolve({ message: "invalid CSRF token" }); } });
}
var api = {""" + match.group(1) + """
};
api.csrf = "stale-token";
api.request("/api/library/transfer", { method: "POST", body: "{}" })
  .then(function () { console.log(JSON.stringify({ resolved: true, attempts: attempts, csrfFetches: csrfFetches })); })
  .catch(function (e) { console.log(JSON.stringify({ resolved: false, attempts: attempts, csrfFetches: csrfFetches, message: e.message })); });
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["resolved"] is False
    assert out["attempts"] == 2, "the original request plus exactly one retry"
    assert out["csrfFetches"] == 1


# ---------------------------------------------------------------------------
# §8 the migration command
# ---------------------------------------------------------------------------


class TestAuthMigrate:
    def test_a_dry_run_writes_nothing(self, hidrive, capsys):
        hidrive.secret_set("115_cookie", "fake-cookie-value")
        assert hidrive.run_auth_migrate(apply=False) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["dry_run"] is True
        assert report["items"]["115_web_cookie"]["present"] is True
        assert report["items"]["115_web_cookie"]["copied"] == 0
        with hidrive.connect_db() as db:
            assert db.execute("SELECT COUNT(*) AS c FROM user_secret").fetchone()["c"] == 0

    def test_apply_copies_the_credential_and_verifies_it_decrypts_back(self, hidrive, capsys):
        hidrive.secret_set("115_cookie", "fake-cookie-value")
        assert hidrive.run_auth_migrate(apply=True) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["items"]["115_web_cookie"] == {
            "present": True, "copied": 1, "skipped": 0, "verified": True}
        with hidrive.connect_db() as db:
            assert db.execute("SELECT COUNT(*) AS c FROM user_secret").fetchone()["c"] == 1

    def test_applying_twice_changes_nothing_more(self, hidrive, capsys):
        """R11: the second run reports what it skipped rather than copying
        the old global value over whatever is there now."""
        hidrive.secret_set("115_cookie", "fake-cookie-value")
        hidrive.run_auth_migrate(apply=True)
        capsys.readouterr()
        hidrive.run_auth_migrate(apply=True)
        report = json.loads(capsys.readouterr().out)
        assert report["items"]["admin_user"]["existed"] is True
        assert report["items"]["115_web_cookie"]["copied"] == 0
        assert report["items"]["115_web_cookie"]["skipped"] == 1
        with hidrive.connect_db() as db:
            assert db.execute("SELECT COUNT(*) AS c FROM user_secret").fetchone()["c"] == 1

    def test_it_copies_rather_than_moves_the_old_global_rows(self, hidrive, capsys):
        hidrive.secret_set("115_cookie", "fake-cookie-value")
        hidrive.run_auth_migrate(apply=True)
        capsys.readouterr()
        assert hidrive.config_value("115_cookie", "ENV_115_COOKIES") == "fake-cookie-value"

    def test_the_report_never_prints_a_value_or_a_hash_of_one(self, hidrive, capsys):
        hidrive.secret_set("115_cookie", "fake-cookie-value")
        hidrive.run_auth_migrate(apply=True)
        printed = capsys.readouterr().out
        assert "fake-cookie-value" not in printed
        import hashlib
        assert hashlib.sha256(b"fake-cookie-value").hexdigest() not in printed

    def test_it_leaves_the_shared_services_global(self, hidrive, capsys):
        hidrive.run_auth_migrate(apply=False)
        report = json.loads(capsys.readouterr().out)
        assert "oauth_tokens" in report["items"]["left_global"]
        assert "media-library.db" in report["items"]["left_global"]


# ---------------------------------------------------------------------------
# Phase 8: the Cloudflare Access identity bridge (§5.2)
# ---------------------------------------------------------------------------


@pytest.fixture
def google_mode(hidrive, monkeypatch):
    """`access` mode with a stub verifier whose principal we control."""
    monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
    principal = {"sub": "cf-subject-1", "email": hidrive.ADMIN_EMAIL, "email_verified": True}
    monkeypatch.setattr(
        hidrive, "verify_access_jwt",
        lambda token: dict(principal) if token == "valid-assertion"
        else (_ for _ in ()).throw(PermissionError("bad")),
    )
    return principal


def _google(client, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get("/auth/google" + (f"?{query}" if query else ""),
                      headers={"Cf-Access-Jwt-Assertion": "valid-assertion"})


class TestGoogleBridge:
    def test_the_administrator_arrives_with_a_session(self, client, hidrive, google_mode):
        response = _google(client)
        assert response.status_code == 302
        assert response.headers["Location"] == "/"
        assert SESSION_COOKIE in response.headers.get("Set-Cookie", "")

    def test_the_identity_is_keyed_on_the_subject_not_the_address(self, client, hidrive, google_mode):
        _google(client)
        with hidrive.connect_db() as db:
            row = db.execute("SELECT provider, subject, email_at_link FROM auth_identity").fetchone()
        assert (row["provider"], row["subject"]) == ("cloudflare_google", "cf-subject-1")
        assert row["email_at_link"] == hidrive.ADMIN_EMAIL

    def test_arriving_again_rotates_the_session(self, client, hidrive, google_mode):
        first = _google(client).headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
        second = _google(client).headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
        assert first != second
        with hidrive.connect_db() as db:
            assert hidrive.auth_service.load_session(db, first, now=hidrive.utc_now()) is None
            assert hidrive.auth_service.load_session(db, second, now=hidrive.utc_now()) is not None

    def test_any_other_address_is_refused_however_cloudflare_feels(self, client, hidrive, google_mode, monkeypatch):
        monkeypatch.setattr(hidrive, "verify_access_jwt",
                            lambda token: {"sub": "cf-subject-2", "email": "someone@example.test",
                                           "email_verified": True})
        response = _google(client)
        assert response.status_code == 403
        assert response.get_json()["code"] == "FORBIDDEN"
        with hidrive.connect_db() as db:
            assert db.execute("SELECT COUNT(*) AS c FROM auth_identity").fetchone()["c"] == 0

    def test_an_unverified_address_is_not_an_identity(self, client, hidrive, google_mode, monkeypatch):
        monkeypatch.setattr(hidrive, "verify_access_jwt",
                            lambda token: {"sub": "cf-subject-1", "email": hidrive.ADMIN_EMAIL,
                                           "email_verified": False})
        response = _google(client)
        assert response.status_code == 403
        assert response.get_json()["code"] == "EMAIL_NOT_VERIFIED"

    def test_without_an_assertion_there_is_nothing_to_bridge(self, client, hidrive, google_mode):
        response = client.get("/auth/google")
        assert response.status_code == 401

    def test_a_forged_assertion_never_gets_past_access(self, client, hidrive, google_mode):
        response = client.get("/auth/google", headers={"Cf-Access-Jwt-Assertion": "forged"})
        assert response.status_code == 401

    @pytest.mark.parametrize("target,expected", [
        ("/?tab=settings", "/?tab=settings"),
        ("/library", "/library"),
        ("https://evil.test/", "/"),
        ("//evil.test/", "/"),
        ("evil.test", "/"),
        ("", "/"),
    ])
    def test_it_only_ever_redirects_inside_this_site(self, client, hidrive, google_mode, target, expected):
        response = _google(client, next=target)
        assert response.headers["Location"] == expected

    def test_a_backslash_or_a_control_character_is_not_a_path(self, hidrive):
        for raw in ("/\\evil.test", "/ok\nSet-Cookie: x", "/ok\r\nx"):
            assert hidrive._safe_next(raw) == "/"

    def test_the_refusal_is_audited_without_the_address(self, client, hidrive, google_mode, monkeypatch, audit_rows):
        monkeypatch.setattr(hidrive, "verify_access_jwt",
                            lambda token: {"sub": "cf-subject-2", "email": "someone@example.test",
                                           "email_verified": True})
        _google(client)
        rows = audit_rows("auth.google")
        assert rows and rows[-1]["status"] == "rejected"
        assert "someone@example.test" not in rows[-1]["detail"]


# ---------------------------------------------------------------------------
# Review 2026-09-12: R12 / R13 / R21 / V01
# ---------------------------------------------------------------------------


class TestHybridIsNotAFreePass:
    """R12: once a session is an alternative way in, an Access assertion no
    longer stands on its own -- the address is checked here too."""

    @pytest.fixture
    def hybrid(self, hidrive, monkeypatch):
        monkeypatch.setattr(hidrive, "AUTH_MODE", "hybrid")
        return hidrive

    def _as(self, hidrive, monkeypatch, email, **over):
        principal = {"sub": "cf-sub", "email": email}
        principal.update(over)
        monkeypatch.setattr(hidrive, "verify_access_jwt", lambda token: dict(principal))

    def test_a_non_administrator_assertion_is_nobody(self, client, hybrid, monkeypatch):
        self._as(hybrid, monkeypatch, "someone@example.test")
        response = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"})
        assert response.status_code == 401

    def test_and_reaches_no_administrator_route(self, client, hybrid, monkeypatch):
        self._as(hybrid, monkeypatch, "someone@example.test")
        for url in ("/api/openlist/list", "/api/settings", "/api/status"):
            response = client.open(url, method="GET", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"})
            assert response.status_code in {401, 403}, url

    def test_the_administrator_address_still_works(self, client, hybrid, monkeypatch):
        self._as(hybrid, monkeypatch, hybrid.ADMIN_EMAIL)
        response = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"})
        assert response.status_code == 200
        assert response.get_json()["role"] == "admin"

    def test_an_address_the_idp_calls_unverified_is_not_the_administrator(self, client, hybrid, monkeypatch):
        self._as(hybrid, monkeypatch, hybrid.ADMIN_EMAIL, email_verified=False)
        assert client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"}).status_code == 401

    def test_access_mode_keeps_its_documented_compatibility(self, client, hidrive, monkeypatch):
        """Today's production mode is unchanged: the whole site is behind one
        Access policy, so a principal that cleared it is the administrator."""
        monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
        monkeypatch.setattr(hidrive, "verify_access_jwt",
                            lambda token: {"sub": "cf-sub", "email": "whoever@example.test"})
        response = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"})
        assert response.status_code == 200 and response.get_json()["role"] == "admin"


class TestSourceRateLimit:
    """R13: a budget per source, not only per address."""

    def _attempt(self, client, email):
        return client.post("/api/auth/login", json={"email": email, "password": "Wrong1Password"})

    def test_changing_the_address_does_not_buy_a_fresh_budget(self, client, hidrive):
        import auth_service as svc

        statuses = [self._attempt(client, f"user{i}@example.test").status_code
                    for i in range(svc.SOURCE_ATTEMPT_LIMIT + 3)]
        assert 429 in statuses, "a source must run out whatever addresses it tries"
        assert statuses.index(429) <= svc.SOURCE_ATTEMPT_LIMIT + 1

    def test_one_address_still_runs_out_first(self, client, hidrive):
        import auth_service as svc

        statuses = [self._attempt(client, "same@example.test").status_code
                    for i in range(svc.LOGIN_ATTEMPT_LIMIT + 2)]
        assert statuses.count(429) >= 1
        assert statuses.index(429) <= svc.LOGIN_ATTEMPT_LIMIT + 1

    def test_a_refused_attempt_never_reaches_the_password_hash(self, client, hidrive, monkeypatch):
        import auth_service as svc

        for i in range(svc.SOURCE_ATTEMPT_LIMIT + 1):
            self._attempt(client, f"burn{i}@example.test")
        calls = []
        monkeypatch.setattr(hidrive.auth_service, "authenticate",
                            lambda *a, **k: calls.append(1))
        assert self._attempt(client, "next@example.test").status_code == 429
        assert calls == [], "the limit exists to bound exactly this cost"

    def test_the_three_budgets_are_separate_rows(self, client, hidrive):
        """F06 added the third: source, account, and the pair."""
        import auth_service as svc

        buckets = {
            svc.source_bucket("login", ip="a"),
            svc.account_bucket("login", email_norm="x@y.z"),
            svc.rate_bucket("login", ip="a", email_norm="x@y.z"),
        }
        assert len(buckets) == 3
        self._attempt(client, "one@example.test")
        with hidrive.connect_db() as db:
            assert db.execute("SELECT COUNT(*) AS c FROM auth_rate_limit").fetchone()["c"] == 3

    def test_registering_is_bounded_the_same_way(self, client, hidrive):
        import auth_service as svc

        statuses = []
        for i in range(svc.SOURCE_ATTEMPT_LIMIT + 3):
            statuses.append(client.post("/api/auth/register", json={
                "email": f"applicant{i}@example.test", "password": GOOD_PASSWORD,
                "confirm_password": GOOD_PASSWORD}).status_code)
        assert 429 in statuses


class TestAccountRateLimit:
    """F06: one account, many sources. R13 bounded the source and left the
    second bucket keyed on (source, address), so 50 addresses tried the same
    account 50 times -- every one paying for a scrypt comparison."""

    def _attempt(self, client, email, ip):
        return client.post("/api/auth/login", json={"email": email, "password": "Wrong1Password"},
                           headers={"Cf-Connecting-Ip": ip})

    def test_one_account_tried_from_many_sources_runs_out(self, client, hidrive):
        import auth_service as svc

        statuses = [self._attempt(client, "target@example.test", f"203.0.113.{i}").status_code
                    for i in range(svc.ACCOUNT_ATTEMPT_LIMIT + 5)]
        assert 429 in statuses, "an account must run out whatever sources try it"
        assert statuses.index(429) <= svc.ACCOUNT_ATTEMPT_LIMIT + 1

    def test_a_refused_login_never_reaches_the_password_hash(self, client, hidrive, monkeypatch):
        import auth_service as svc

        for i in range(svc.ACCOUNT_ATTEMPT_LIMIT + 1):
            self._attempt(client, "target@example.test", f"198.51.100.{i}")
        calls = []
        monkeypatch.setattr(hidrive.auth_service, "authenticate", lambda *a, **k: calls.append("login"))
        assert self._attempt(client, "target@example.test", "198.51.100.250").status_code == 429
        assert calls == [], "the limit exists to bound exactly this cost"

    def test_a_refused_registration_never_creates_a_user(self, client, hidrive, monkeypatch):
        """Registration has its own scope, bounded the same way: applying for
        one address from many sources runs that address's budget out."""
        import auth_service as svc

        def apply(index):
            return client.post("/api/auth/register", json={
                "email": "wanted@example.test", "password": GOOD_PASSWORD,
                "confirm_password": GOOD_PASSWORD,
            }, headers={"Cf-Connecting-Ip": f"203.0.114.{index}"}).status_code

        statuses = [apply(i) for i in range(svc.ACCOUNT_ATTEMPT_LIMIT + 3)]
        assert 429 in statuses
        calls = []
        monkeypatch.setattr(hidrive.auth_service, "create_pending_user", lambda *a, **k: calls.append("create"))
        assert apply(250) == 429
        assert calls == []

    def test_another_account_from_the_same_sources_is_unaffected(self, client, hidrive):
        import auth_service as svc

        for i in range(svc.ACCOUNT_ATTEMPT_LIMIT + 2):
            self._attempt(client, "target@example.test", f"192.0.2.{i}")
        # A different address from one of those same sources: its own budget.
        assert self._attempt(client, "bystander@example.test", "192.0.2.1").status_code == 401

    def test_the_window_lets_a_real_person_back_in(self, client, hidrive, monkeypatch):
        import auth_service as svc

        for i in range(svc.ACCOUNT_ATTEMPT_LIMIT + 2):
            self._attempt(client, "target@example.test", f"192.0.2.{i}")
        assert self._attempt(client, "target@example.test", "192.0.2.9").status_code == 429
        later = hidrive.utc_now() + svc.ACCOUNT_ATTEMPT_WINDOW + 1
        monkeypatch.setattr(hidrive, "utc_now", lambda: later)
        assert self._attempt(client, "target@example.test", "192.0.2.9").status_code == 401

    def test_concurrent_attempts_are_all_counted(self, hidrive):
        """The count is a single UPDATE per attempt, so overlapping callers
        cannot both read the same total and each write it back."""
        import threading

        import auth_service as svc

        results: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def worker(index):
            start.wait(timeout=10)
            with hidrive.connect_db() as db:
                allowed = svc.within_attempt_limits(db, "login", ip=f"10.0.0.{index}",
                                                    email_norm="race@example.test", now=1000)
                db.commit()
            with lock:
                results.append(allowed)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)
        with hidrive.connect_db() as db:
            row = db.execute("SELECT attempts FROM auth_rate_limit WHERE bucket_hash=?",
                             (svc.account_bucket("login", email_norm="race@example.test"),)).fetchone()
        assert row["attempts"] == len(results) == 8, (row["attempts"], results)

    def test_an_attempt_with_no_address_has_no_account_bucket(self, hidrive):
        import auth_service as svc

        with hidrive.connect_db() as db:
            assert svc.within_attempt_limits(db, "login", ip="10.1.1.1", email_norm=None, now=1) is True
            rows = {r["bucket_hash"] for r in db.execute("SELECT bucket_hash FROM auth_rate_limit")}
        assert svc.account_bucket("login", email_norm=None) not in rows


class TestBridgeSessionScope:
    """R21: signing in on one device is not a reason to sign the others out."""

    def test_it_replaces_only_the_session_this_browser_arrived_with(self, client, hidrive, google_mode):
        # Another device already has one.
        with hidrive.connect_db() as db:
            user_id = hidrive.auth_service.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            elsewhere = hidrive.auth_service.issue_session(db, user_id, now=hidrive.utc_now())
        first = _google(client).headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
        second = _google(client).headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
        with hidrive.connect_db() as db:
            now = hidrive.utc_now()
            assert hidrive.auth_service.load_session(db, first, now=now) is None, "this browser's old session goes"
            assert hidrive.auth_service.load_session(db, second, now=now) is not None
            assert hidrive.auth_service.load_session(db, elsewhere.token, now=now) is not None, \
                "the other device keeps its session"

    def test_disabling_an_account_is_what_ends_every_session(self, client, hidrive):
        with hidrive.connect_db() as db:
            user_id = hidrive.auth_service.create_pending_user(
                db, email="many@example.test", password=GOOD_PASSWORD, display_name=None, now=1)
            admin_id = hidrive.auth_service.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            hidrive.auth_service.approve_user(db, user_id, approver_id=admin_id, now=2)
            devices = [hidrive.auth_service.issue_session(db, user_id, now=10 + i) for i in range(3)]
            hidrive.auth_service.disable_user(db, user_id, approver_id=admin_id, now=20)
            for device in devices:
                assert hidrive.auth_service.load_session(db, device.token, now=21) is None


class TestStandardCloudflareClaims:
    """V01: a standard Access application token need not carry
    `email_verified`; a missing claim is not a rejection."""

    def test_a_payload_without_email_verified_is_accepted(self, client, hidrive, monkeypatch):
        monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
        monkeypatch.setattr(hidrive, "verify_access_jwt", lambda token: {
            "aud": ["fake-aud"], "email": hidrive.ADMIN_EMAIL, "sub": "cf-sub-standard",
            "iat": 1, "exp": 2, "iss": "https://access.example.invalid",
        })
        response = _google(client)
        assert response.status_code == 302
        assert SESSION_COOKIE in response.headers.get("Set-Cookie", "")

    def test_only_an_explicit_false_is_a_refusal(self, client, hidrive, monkeypatch):
        monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
        monkeypatch.setattr(hidrive, "verify_access_jwt", lambda token: {
            "email": hidrive.ADMIN_EMAIL, "sub": "cf-sub-standard", "email_verified": False})
        assert _google(client).status_code == 403

    def test_a_signature_issuer_or_audience_failure_still_stops_at_the_door(self, client, hidrive, google_mode):
        assert client.get("/auth/google", headers={"Cf-Access-Jwt-Assertion": "forged"}).status_code == 401


# ---------------------------------------------------------------------------
# G02: the budget decision is atomic -- no snapshot can hand out a second one
# ---------------------------------------------------------------------------


class TestTheBudgetCannotBeDoubled:
    """G02: `check_rate_limit` read the row, then in the "no row / window
    rolled over" branch wrote attempts=1 unconditionally. A caller that read
    "no row", paused, and resumed after another had created *and exhausted*
    the bucket reset the count to 1 -- 60 attempts allowed against a budget of
    30 in one window."""

    def _fresh_db(self, tmp_path, name="rate.db"):
        import auth_service as svc

        conn = sqlite3.connect(tmp_path / name, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS auth_rate_limit (bucket_hash TEXT PRIMARY KEY, "
            "window_started_at INTEGER NOT NULL, attempts INTEGER NOT NULL, blocked_until INTEGER);")
        conn.commit()
        assert svc.check_rate_limit is not None
        return conn

    def test_the_creation_race_does_not_reset_the_count(self, tmp_path):
        """Two real connections, the interleaving fixed by events: A reads
        "empty", B fills the bucket to its limit, A then records its own
        attempt. The total allowed in the window must not exceed the budget."""
        import auth_service as svc

        bucket = svc.source_bucket("login", ip="203.0.113.7")
        limit, window, now = 5, 600, 1000

        self._fresh_db(tmp_path).close()

        def own_connection():
            conn = sqlite3.connect(tmp_path / "rate.db", timeout=20)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=20000")
            return conn

        a_read = threading.Event()
        b_done = threading.Event()
        allowed: list[bool] = []
        guard = threading.Lock()

        def first():
            # The old shape's window: observe the empty bucket, pause, then
            # record. With the atomic statement there is nothing to observe
            # first -- the record *is* the observation.
            conn = own_connection()
            try:
                a_read.set()
                assert b_done.wait(timeout=30)
                with conn:
                    ok = svc.check_rate_limit(conn, bucket, now=now, limit=limit, window=window)
                with guard:
                    allowed.append(ok)
            finally:
                conn.close()

        def second():
            conn = own_connection()
            try:
                assert a_read.wait(timeout=30)
                for _ in range(limit):
                    with conn:
                        ok = svc.check_rate_limit(conn, bucket, now=now, limit=limit, window=window)
                    with guard:
                        allowed.append(ok)
            finally:
                conn.close()
                b_done.set()

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(thread.is_alive() for thread in threads)
        reader = own_connection()
        try:
            row = reader.execute("SELECT attempts FROM auth_rate_limit WHERE bucket_hash=?", (bucket,)).fetchone()
        finally:
            reader.close()
        assert sum(1 for ok in allowed if ok) <= limit, (
            f"{sum(1 for ok in allowed if ok)} allowed against a budget of {limit}")
        assert int(row["attempts"]) == limit + 1, row["attempts"]

    def test_many_connections_never_exceed_the_budget(self, tmp_path):
        """Every attempt arrives at once; the number allowed is exactly the
        budget, and the count is exactly the number of attempts."""
        import auth_service as svc

        bucket = svc.account_bucket("login", email_norm="crowd@example.test")
        limit, window, now, attempts = 6, 600, 2000, 24
        self._fresh_db(tmp_path, "crowd.db").close()
        start = threading.Barrier(attempts, timeout=30)
        allowed: list[bool] = []
        guard = threading.Lock()

        def worker():
            conn = sqlite3.connect(tmp_path / "crowd.db", timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=15000")
            try:
                start.wait()
                with conn:
                    ok = svc.check_rate_limit(conn, bucket, now=now, limit=limit, window=window)
                with guard:
                    allowed.append(ok)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(attempts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert not any(thread.is_alive() for thread in threads)
        conn = sqlite3.connect(tmp_path / "crowd.db", timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT attempts FROM auth_rate_limit WHERE bucket_hash=?", (bucket,)).fetchone()
        finally:
            conn.close()
        assert len(allowed) == attempts
        assert sum(1 for ok in allowed if ok) == limit, sum(1 for ok in allowed if ok)
        assert int(row["attempts"]) == attempts, "an attempt was lost"

    def test_the_last_remaining_attempt_is_handed_out_once(self, tmp_path):
        import auth_service as svc

        bucket = svc.rate_bucket("login", ip="198.51.100.9", email_norm="last@example.test")
        limit, window, now = 3, 600, 3000
        conn = self._fresh_db(tmp_path, "last.db")
        try:
            for _ in range(limit - 1):
                with conn:
                    assert svc.check_rate_limit(conn, bucket, now=now, limit=limit, window=window)
        finally:
            conn.close()
        start = threading.Barrier(4, timeout=30)
        allowed: list[bool] = []
        guard = threading.Lock()

        def worker():
            own = sqlite3.connect(tmp_path / "last.db", timeout=15)
            own.row_factory = sqlite3.Row
            own.execute("PRAGMA busy_timeout=15000")
            try:
                start.wait()
                with own:
                    ok = svc.check_rate_limit(own, bucket, now=now, limit=limit, window=window)
                with guard:
                    allowed.append(ok)
            finally:
                own.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert not any(thread.is_alive() for thread in threads)
        assert sum(1 for ok in allowed if ok) == 1, "the last remaining attempt was handed out more than once"

    def test_the_window_boundary_is_not_a_free_budget(self, tmp_path):
        """Rolling over is one reset, whoever observes it first."""
        import auth_service as svc

        bucket = svc.source_bucket("login", ip="192.0.2.44")
        limit, window, now = 4, 600, 4000
        conn = self._fresh_db(tmp_path, "roll.db")
        try:
            for _ in range(limit + 2):
                with conn:
                    svc.check_rate_limit(conn, bucket, now=now, limit=limit, window=window)
            with conn:
                assert svc.check_rate_limit(conn, bucket, now=now, limit=limit, window=window) is False
        finally:
            conn.close()
        later = now + window
        start = threading.Barrier(6, timeout=30)
        allowed: list[bool] = []
        guard = threading.Lock()

        def worker():
            own = sqlite3.connect(tmp_path / "roll.db", timeout=15)
            own.row_factory = sqlite3.Row
            own.execute("PRAGMA busy_timeout=15000")
            try:
                start.wait()
                with own:
                    ok = svc.check_rate_limit(own, bucket, now=later, limit=limit, window=window)
                with guard:
                    allowed.append(ok)
            finally:
                own.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert not any(thread.is_alive() for thread in threads)
        assert sum(1 for ok in allowed if ok) == limit, (
            f"{sum(1 for ok in allowed if ok)} allowed in the new window against a budget of {limit}")

    def test_separate_processes_share_one_budget(self, tmp_path):
        """The deployment runs two workers; the budget is one."""
        import auth_service as svc

        bucket = svc.source_bucket("login", ip="203.0.113.99")
        self._fresh_db(tmp_path, "procs.db").close()
        script = tmp_path / "spend.py"
        script.write_text(
            "import json, sqlite3, sys, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import auth_service as svc\n"
            "path, bucket, limit, window, now, start_at = sys.argv[1], sys.argv[2], int(sys.argv[3]), "
            "int(sys.argv[4]), int(sys.argv[5]), float(sys.argv[6])\n"
            "conn = sqlite3.connect(path, timeout=20)\n"
            "conn.row_factory = sqlite3.Row\n"
            "conn.execute('PRAGMA busy_timeout=20000')\n"
            "while time.time() < start_at:\n"
            "    time.sleep(0.005)\n"
            "allowed = 0\n"
            "for _ in range(limit):\n"
            "    with conn:\n"
            "        if svc.check_rate_limit(conn, bucket, now=now, limit=limit, window=window):\n"
            "            allowed += 1\n"
            "print(json.dumps({'allowed': allowed}))\n",
            encoding="utf-8")
        limit, window, now = 8, 600, 5000
        start_at = time.time() + 2.0
        procs = [subprocess.Popen(
            [sys.executable, str(script), str(tmp_path / "procs.db"), bucket, str(limit),
             str(window), str(now), str(start_at)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        totals = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=120)
            assert proc.returncode == 0, f"{stdout}\n{stderr}"
            totals.append(json.loads(stdout.strip().splitlines()[-1])["allowed"])
        assert sum(totals) == limit, f"two processes allowed {sum(totals)} against a budget of {limit}"


class TestRefusalCostsNothing:
    def test_no_expensive_work_after_any_dimension_refuses(self, client, hidrive, monkeypatch):
        """G02: once refused, neither scrypt nor a user row happens."""
        import auth_service as svc

        for index in range(svc.ACCOUNT_ATTEMPT_LIMIT + 1):
            client.post("/api/auth/login", json={"email": "cost@example.test", "password": "Wrong1Password"},
                        headers={"Cf-Connecting-Ip": f"203.0.115.{index}"})
        calls: list[str] = []
        monkeypatch.setattr(hidrive.auth_service, "authenticate", lambda *a, **k: calls.append("authenticate"))
        monkeypatch.setattr(hidrive.auth_service, "create_pending_user", lambda *a, **k: calls.append("create"))
        monkeypatch.setattr(hidrive.auth_service, "derive_key", lambda *a, **k: calls.append("scrypt"))
        assert client.post("/api/auth/login", json={
            "email": "cost@example.test", "password": "Wrong1Password"},
            headers={"Cf-Connecting-Ip": "203.0.115.250"}).status_code == 429
        assert calls == [], calls
