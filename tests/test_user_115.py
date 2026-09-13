"""Phase 4: each user's own 115 web session (step A).

Plan §9/§16.3. Two users never see each other's cookie, challenge or state,
and nobody is ever served the administrator's. Every value here is invented
and lives only in the test's own temporary database.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service as auth  # noqa: E402
import user_115  # noqa: E402

MEMBER_PASSWORD = "Correct1Horse"
COOKIE_A = "UID=fake-a; CID=fake-a; SEID=fake-a"
COOKIE_B = "UID=fake-b; CID=fake-b; SEID=fake-b"


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "hidrive.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT, actor TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE reauth_challenges (id_hash TEXT PRIMARY KEY, actor TEXT NOT NULL,
            state TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            consumed_at INTEGER, error_code TEXT, qr_uid_cipher BLOB, qr_time INTEGER,
            qr_sign_cipher BLOB, claimed_at INTEGER, claimed_from TEXT);
        """
    )
    auth.ensure_schema(conn)
    conn.commit()
    return conn


@pytest.fixture
def fernet(tmp_path):
    import base64
    import os

    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(os.urandom(32)))


def _two_users(db):
    admin = auth.ensure_admin_user(db, email="admin@example.invalid", now=1)
    first = auth.create_pending_user(db, email="a@example.test", password=MEMBER_PASSWORD,
                                     display_name=None, now=1)
    second = auth.create_pending_user(db, email="b@example.test", password=MEMBER_PASSWORD,
                                      display_name=None, now=1)
    auth.approve_user(db, first, approver_id=admin, now=2)
    auth.approve_user(db, second, approver_id=admin, now=2)
    return admin, first, second


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------


class TestSecrets:
    def test_each_user_reads_only_their_own_cookie(self, db, fernet):
        _admin, first, second = _two_users(db)
        user_115.secret_set(db, first, user_115.COOKIE_SECRET, COOKIE_A, fernet=fernet, now=10)
        user_115.secret_set(db, second, user_115.COOKIE_SECRET, COOKIE_B, fernet=fernet, now=10)
        assert user_115.credentials_for(db, first, fernet=fernet).web_cookie == COOKIE_A
        assert user_115.credentials_for(db, second, fernet=fernet).web_cookie == COOKIE_B

    def test_a_user_with_no_cookie_simply_has_none(self, db, fernet):
        _admin, first, _second = _two_users(db)
        assert user_115.credentials_for(db, first, fernet=fernet).web_cookie is None
        assert user_115.credentials_for(db, first, fernet=fernet).has_cookie is False

    def test_what_is_stored_is_not_the_cookie(self, db, fernet):
        _admin, first, _second = _two_users(db)
        user_115.secret_set(db, first, user_115.COOKIE_SECRET, COOKIE_A, fernet=fernet, now=10)
        stored = db.execute("SELECT ciphertext FROM user_secret WHERE user_id=?", (first,)).fetchone()[0]
        assert COOKIE_A.encode() not in bytes(stored)

    def test_an_unknown_secret_name_is_refused(self, db, fernet):
        _admin, first, _second = _two_users(db)
        with pytest.raises(ValueError):
            user_115.secret_set(db, first, "115_admin_cookie", "x", fernet=fernet, now=1)

    def test_a_value_that_will_not_decrypt_reads_as_absent(self, db, fernet):
        _admin, first, _second = _two_users(db)
        db.execute("INSERT INTO user_secret(user_id, name, ciphertext, updated_at) VALUES(?,?,?,?)",
                   (first, user_115.COOKIE_SECRET, b"not-a-token", 1))
        assert user_115.credentials_for(db, first, fernet=fernet).web_cookie is None


class TestStepsAreIndependent:
    def test_step_b_needs_both_halves_to_count_as_connected(self, db, fernet):
        _admin, first, _second = _two_users(db)
        user_115.secret_set(db, first, user_115.OPEN_ACCESS_SECRET, "fake-access", fernet=fernet, now=1)
        assert user_115.credentials_for(db, first, fernet=fernet).has_open_token is False
        user_115.secret_set(db, first, user_115.OPEN_REFRESH_SECRET, "fake-refresh", fernet=fernet, now=1)
        assert user_115.credentials_for(db, first, fernet=fernet).has_open_token is True

    def test_reconnecting_step_a_leaves_step_b_alone(self, db, fernet):
        _admin, first, _second = _two_users(db)
        user_115.secret_set(db, first, user_115.OPEN_ACCESS_SECRET, "fake-access", fernet=fernet, now=1)
        user_115.secret_set(db, first, user_115.OPEN_REFRESH_SECRET, "fake-refresh", fernet=fernet, now=1)
        user_115.secret_clear(db, first, user_115.COOKIE_SECRET)
        user_115.secret_set(db, first, user_115.COOKIE_SECRET, COOKIE_A, fernet=fernet, now=2)
        credentials = user_115.credentials_for(db, first, fernet=fernet)
        assert credentials.has_cookie and credentials.has_open_token

    def test_disconnecting_step_b_leaves_step_a_alone(self, db, fernet):
        _admin, first, _second = _two_users(db)
        user_115.secret_set(db, first, user_115.COOKIE_SECRET, COOKIE_A, fernet=fernet, now=1)
        user_115.secret_set(db, first, user_115.OPEN_ACCESS_SECRET, "fake-access", fernet=fernet, now=1)
        user_115.secret_clear(db, first, user_115.OPEN_ACCESS_SECRET, user_115.OPEN_REFRESH_SECRET)
        assert user_115.credentials_for(db, first, fernet=fernet).has_cookie is True

    def test_the_capability_summary_says_what_can_be_done(self, db, fernet):
        _admin, first, _second = _two_users(db)
        nothing = user_115.capability_state(user_115.credentials_for(db, first, fernet=fernet))
        assert nothing == {"transfer": False, "browse": False, "cloud_download": False, "summary": "待连接"}
        user_115.secret_set(db, first, user_115.COOKIE_SECRET, COOKIE_A, fernet=fernet, now=1)
        cookie_only = user_115.capability_state(user_115.credentials_for(db, first, fernet=fernet))
        assert cookie_only["transfer"] is True and cookie_only["browse"] is False
        assert cookie_only["summary"] == "基础可用"
        user_115.secret_set(db, first, user_115.OPEN_ACCESS_SECRET, "a", fernet=fernet, now=1)
        user_115.secret_set(db, first, user_115.OPEN_REFRESH_SECRET, "r", fernet=fernet, now=1)
        both = user_115.capability_state(user_115.credentials_for(db, first, fernet=fernet))
        assert both["summary"] == "全部可用"


class TestState:
    def test_each_user_has_their_own_cookie_state(self, db):
        _admin, first, second = _two_users(db)
        user_115.remember_cookie_state(db, first, state=user_115.STATE_CONNECTED, error_code=None, now=100)
        user_115.remember_cookie_state(db, second, state=user_115.STATE_NEEDS_REAUTH,
                                       error_code="reauth_required", now=100)
        assert user_115.profile(db, first)["cookie_state"] == user_115.STATE_CONNECTED
        assert user_115.profile(db, second)["cookie_state"] == user_115.STATE_NEEDS_REAUTH

    def test_the_state_row_holds_no_cookie_and_no_uid(self, db):
        _admin, first, _second = _two_users(db)
        user_115.remember_cookie_state(db, first, state=user_115.STATE_NEEDS_REAUTH,
                                       error_code="reauth_required", now=100)
        row = dict(user_115.profile(db, first))
        assert "UID=" not in str(row)
        assert COOKIE_A not in str(row)

    def test_step_a_state_and_step_b_state_are_separate_columns(self, db):
        _admin, first, _second = _two_users(db)
        user_115.remember_cookie_state(db, first, state=user_115.STATE_CONNECTED, error_code=None, now=1)
        user_115.remember_open_state(db, first, state=user_115.STATE_NEEDS_REAUTH,
                                     expires_at=None, error_code="expired", now=2)
        row = user_115.profile(db, first)
        assert row["cookie_state"] == user_115.STATE_CONNECTED
        assert row["open_state"] == user_115.STATE_NEEDS_REAUTH


class TestSettingKeys:
    def test_the_administrator_keeps_the_original_keys(self):
        assert user_115.setting_key("115_cookie_state", 1, admin_user_id=1) == "115_cookie_state"

    def test_everyone_else_gets_their_own_row(self):
        assert user_115.setting_key("115_cookie_state", 7, admin_user_id=1) == "115_cookie_state:u7"

    def test_two_members_never_share_a_key(self):
        first = user_115.setting_key("115_cookie_fail_streak", 7, admin_user_id=1)
        second = user_115.setting_key("115_cookie_fail_streak", 8, admin_user_id=1)
        assert first != second


# ---------------------------------------------------------------------------
# through the app: whose cookie is used, and whose challenge is whose
# ---------------------------------------------------------------------------


@pytest.fixture
def member(client, hidrive):
    client.post("/api/auth/register", json={
        "email": "member@example.test", "password": MEMBER_PASSWORD, "confirm_password": MEMBER_PASSWORD})
    with hidrive.connect_db() as conn:
        admin_id = auth.ensure_admin_user(conn, email=hidrive.ADMIN_EMAIL, now=1)
        row = conn.execute("SELECT id FROM auth_user WHERE email_norm='member@example.test'").fetchone()
        auth.approve_user(conn, int(row["id"]), approver_id=admin_id, now=2)
    assert client.post("/api/auth/login", json={
        "email": "member@example.test", "password": MEMBER_PASSWORD}).status_code == 200
    return int(row["id"])


class TestThroughTheApp:
    def test_a_member_with_no_cookie_of_their_own_gets_none(self, client, hidrive, member):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
            assert hidrive.user_115_cookie(hidrive.current_user()) is None

    def test_the_administrator_still_finds_the_legacy_value(self, client, hidrive):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        with hidrive.app.test_request_context("/"):
            hidrive.g.principal = {"sub": "owner", "email": "owner@example.test"}
            assert hidrive.user_115_cookie(hidrive.current_user()) == "the-administrators-cookie"

    def test_a_member_with_their_own_cookie_uses_it(self, client, hidrive, member):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        with hidrive.connect_db() as conn:
            user_115.secret_set(conn, member, user_115.COOKIE_SECRET, COOKIE_A,
                                fernet=hidrive.load_fernet(), now=1)
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
            assert hidrive.user_115_cookie(hidrive.current_user()) == COOKIE_A

    def test_a_transfer_by_a_member_without_a_cookie_is_refused_before_any_call(self, client, hidrive, member):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        response = client.post("/api/library/transfer", json={"link_id": "pub-1"})
        # Refused for want of a cookie -- and never with the administrator's.
        assert response.status_code in {400, 404, 409, 503}
        assert response.status_code != 200
        body = response.get_data(as_text=True)
        assert "the-administrators-cookie" not in body

    def test_a_members_requests_are_attributed_to_them(self, client, hidrive, member):
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
            assert hidrive.actor_id() == f"user:{member}"

    def test_the_administrator_keeps_their_existing_attribution(self, client, hidrive):
        with hidrive.app.test_request_context("/"):
            hidrive.g.principal = {"sub": "owner", "email": "owner@example.test"}
            assert hidrive.actor_id() == "owner"

    def test_one_members_scan_challenge_is_invisible_to_another(self, client, hidrive, member):
        """A challenge belongs to the actor that started it; the routes have
        checked that since before this work, and Phase 4 makes the actor the
        user rather than the deployment."""
        with hidrive.connect_db() as conn:
            conn.execute(
                "INSERT INTO reauth_challenges(id_hash,actor,user_id,state,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?)",
                (hidrive._reauth_hash("challenge-of-another"), f"user:{member + 1}", member + 1,
                 "pending", hidrive.utc_now(), hidrive.utc_now() + 600),
            )
        response = client.get("/api/115/reauth/status?challenge_id=challenge-of-another")
        assert response.status_code in {403, 404}
        assert response.status_code != 200


# ---------------------------------------------------------------------------
# Phase 6: directories, cloud download and the caches around them
# ---------------------------------------------------------------------------


class TestPhase6Isolation:
    def test_the_same_info_hash_can_belong_to_two_users_at_once(self, db):
        _admin, first, second = _two_users(db)
        for user_id in (first, second):
            db.execute(
                "INSERT INTO user_cloud_download_task(user_id, info_hash, source_kind, submitted_at) "
                "VALUES(?,?,?,?)", (user_id, "shared-hash", "library", 10))
        rows = db.execute("SELECT user_id FROM user_cloud_download_task WHERE info_hash='shared-hash'").fetchall()
        assert sorted(r["user_id"] for r in rows) == sorted([first, second])

    def test_one_user_cannot_submit_the_same_hash_twice(self, db):
        _admin, first, _second = _two_users(db)
        db.execute("INSERT INTO user_cloud_download_task(user_id, info_hash, source_kind, submitted_at) "
                   "VALUES(?,?,?,?)", (first, "same-hash", "library", 10))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO user_cloud_download_task(user_id, info_hash, source_kind, submitted_at) "
                       "VALUES(?,?,?,?)", (first, "same-hash", "library", 11))

    def test_the_task_row_keeps_what_the_list_shows(self, db):
        """The plan's §7 sketch had no group_id or link_label; the
        cloud-download list shows both, so the table carries them."""
        columns = {row["name"] for row in db.execute("PRAGMA table_info(user_cloud_download_task)")}
        assert {"group_id", "link_label", "display_title", "media_id", "state"} <= columns

    def test_two_users_never_share_a_cloud_cache_slot(self, hidrive):
        first = hidrive._cloud_cache_key(auth.CurrentUser(id=7, email="a", role="member", status="active"), "quota")
        second = hidrive._cloud_cache_key(auth.CurrentUser(id=8, email="b", role="member", status="active"), "quota")
        assert first != second
        pages = {hidrive._cloud_cache_key(auth.CurrentUser(id=7, email="a", role="member", status="active"),
                                          "tasks", page) for page in (1, 2)}
        assert len(pages) == 2

    def test_a_member_without_step_b_is_refused_the_folder_list(self, client, hidrive, member):
        hidrive.secret_set("115_open_access_token", "the-administrators-token")
        response = client.get("/api/115/folders")
        assert response.status_code in {403, 409}
        assert "the-administrators-token" not in response.get_data(as_text=True)

    def test_a_member_without_step_b_is_refused_cloud_download(self, client, hidrive, member):
        hidrive.secret_set("115_open_access_token", "the-administrators-token")
        for url in ("/api/library/cloud-download/tasks", "/api/library/cloud-download/quota"):
            response = client.get(url)
            assert response.status_code in {403, 409}, url
            assert "the-administrators-token" not in response.get_data(as_text=True)

    def test_a_members_token_is_their_own_and_never_the_administrators(self, client, hidrive, member):
        hidrive.secret_set("115_open_access_token", "the-administrators-token")
        with hidrive.connect_db() as conn:
            user_115.secret_set(conn, member, user_115.OPEN_ACCESS_SECRET, "fake-member-token",
                                fernet=hidrive.load_fernet(), now=1)
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
            assert hidrive.user_115_open_token(hidrive.current_user()) == "fake-member-token"

    def test_background_work_uses_the_deployments_own_credential(self, hidrive):
        """No request, no user: the enricher and the timers act as the
        deployment. An anonymous request is a different thing and gets
        nothing."""
        hidrive.secret_set("115_open_access_token", "the-deployment-token")
        assert hidrive.user_115_open_token(None) == "the-deployment-token"
        with hidrive.app.test_request_context("/"):
            assert hidrive.user_115_open_token(None) is None

    def test_the_daily_cloud_budget_is_counted_per_user(self, client, hidrive, member):
        with hidrive.connect_db() as conn:
            conn.execute("INSERT INTO user_cloud_download_task(user_id, info_hash, source_kind, submitted_at) "
                         "VALUES(?,?,?,?)", (member + 99, "someone-elses", "library", hidrive.utc_now()))
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
            assert hidrive._cloud_today_submitted() == 0


# ---------------------------------------------------------------------------
# Review 2026-09-12: R01 / R02 / R03 / R18 / R19
# ---------------------------------------------------------------------------


def _challenge_row(hidrive, *, user_id, uid="fake-uid", actor=None):
    """Insert a confirmed challenge the way /reauth/start would, then hand
    back the row `_reauth_complete` is given."""
    fernet = hidrive.load_fernet()
    now = hidrive.utc_now()
    id_hash = hidrive._reauth_hash(f"challenge-{user_id}")
    with hidrive.connect_db() as db:
        db.execute(
            "INSERT INTO reauth_challenges(id_hash,actor,user_id,state,created_at,expires_at,"
            "qr_uid_cipher,qr_time,qr_sign_cipher) VALUES(?,?,?,?,?,?,?,?,?)",
            (id_hash, actor or f"user:{user_id}", user_id, "confirmed", now, now + 600,
             fernet.encrypt(uid.encode()), now, fernet.encrypt(b"fake-sign")),
        )
        return db.execute("SELECT * FROM reauth_challenges WHERE id_hash=?", (id_hash,)).fetchone()


@pytest.fixture
def scan_succeeds(hidrive, monkeypatch):
    """The upstream half of a successful scan, without a network."""
    def _use(cookie_value):
        payload = {"state": 1, "data": {"cookie": dict(
            part.split("=", 1) for part in cookie_value.split("; "))}}

        class _Response:
            status_code = 200
            content = b"{}"

            @staticmethod
            def json():
                return payload

        monkeypatch.setattr(hidrive.requests, "post", lambda *a, **k: _Response())
        monkeypatch.setattr(hidrive, "verify_115_session",
                            lambda cookie=None, deadline=None: {
                                "state": "valid", "error_code": None,
                                "checked_at": hidrive.utc_now(), "retry_after": 0})
    return _use


class TestScanSavesToWhoeverScanned:
    """R01: the real completion function, not a direct insert."""

    def test_a_members_scan_lands_in_their_own_row_only(self, client, hidrive, member, scan_succeeds):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        scan_succeeds(COOKIE_A)
        row = _challenge_row(hidrive, user_id=member)
        state, error = hidrive._reauth_complete(row, deadline=hidrive.time.monotonic() + 30)
        assert (state, error) == ("authenticated", None)
        with hidrive.connect_db() as db:
            mine = user_115.secret_get(db, member, user_115.COOKIE_SECRET, fernet=hidrive.load_fernet())
        assert mine == COOKIE_A
        # And the administrator's global credential is untouched.
        assert hidrive.config_value("115_cookie", "ENV_115_COOKIES") == "the-administrators-cookie"

    def test_one_members_scan_changes_nothing_for_another(self, client, hidrive, member, scan_succeeds):
        with hidrive.connect_db() as db:
            other = auth.create_pending_user(db, email="other@example.test",
                                             password=MEMBER_PASSWORD, display_name=None, now=1)
            user_115.secret_set(db, other, user_115.COOKIE_SECRET, COOKIE_B,
                                fernet=hidrive.load_fernet(), now=1)
        scan_succeeds(COOKIE_A)
        hidrive._reauth_complete(_challenge_row(hidrive, user_id=member),
                                 deadline=hidrive.time.monotonic() + 30)
        with hidrive.connect_db() as db:
            assert user_115.secret_get(db, other, user_115.COOKIE_SECRET,
                                       fernet=hidrive.load_fernet()) == COOKIE_B

    def test_the_administrators_scan_fills_both_their_row_and_the_legacy_slot(self, client, hidrive, scan_succeeds):
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        scan_succeeds(COOKIE_A)
        hidrive._reauth_complete(_challenge_row(hidrive, user_id=admin_id),
                                 deadline=hidrive.time.monotonic() + 30)
        with hidrive.connect_db() as db:
            assert user_115.secret_get(db, admin_id, user_115.COOKIE_SECRET,
                                       fernet=hidrive.load_fernet()) == COOKIE_A
        assert hidrive.config_value("115_cookie", "ENV_115_COOKIES") == COOKIE_A

    def test_a_scan_never_touches_step_b(self, client, hidrive, member, scan_succeeds):
        with hidrive.connect_db() as db:
            user_115.secret_set(db, member, user_115.OPEN_ACCESS_SECRET, "fake-access",
                                fernet=hidrive.load_fernet(), now=1)
            user_115.secret_set(db, member, user_115.OPEN_REFRESH_SECRET, "fake-refresh",
                                fernet=hidrive.load_fernet(), now=1)
        scan_succeeds(COOKIE_A)
        hidrive._reauth_complete(_challenge_row(hidrive, user_id=member),
                                 deadline=hidrive.time.monotonic() + 30)
        with hidrive.connect_db() as db:
            assert user_115.credentials_for(db, member, fernet=hidrive.load_fernet()).has_open_token

    def test_a_failed_verification_writes_nothing(self, client, hidrive, member, monkeypatch, scan_succeeds):
        with hidrive.connect_db() as db:
            user_115.secret_set(db, member, user_115.COOKIE_SECRET, COOKIE_B,
                                fernet=hidrive.load_fernet(), now=1)
        scan_succeeds(COOKIE_A)
        monkeypatch.setattr(hidrive, "verify_115_session",
                            lambda cookie=None, deadline=None: {
                                "state": "reauth_required", "error_code": "reauth_required",
                                "checked_at": hidrive.utc_now(), "retry_after": 0})
        state, error = hidrive._reauth_complete(_challenge_row(hidrive, user_id=member),
                                                deadline=hidrive.time.monotonic() + 30)
        assert (state, error) == ("failed", "VERIFY_FAILED")
        with hidrive.connect_db() as db:
            assert user_115.secret_get(db, member, user_115.COOKIE_SECRET,
                                       fernet=hidrive.load_fernet()) == COOKIE_B

    def test_a_challenge_with_no_user_is_the_administrators_own(self, client, hidrive, scan_succeeds):
        """Rows created before the multi-user work carry no user_id."""
        scan_succeeds(COOKIE_A)
        with hidrive.connect_db() as db:
            db.execute(
                "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,"
                "qr_uid_cipher,qr_time,qr_sign_cipher) VALUES(?,?,?,?,?,?,?,?)",
                (hidrive._reauth_hash("legacy"), "owner", "confirmed", 1, hidrive.utc_now() + 600,
                 hidrive.load_fernet().encrypt(b"uid"), 1, hidrive.load_fernet().encrypt(b"sign")))
            row = db.execute("SELECT * FROM reauth_challenges WHERE actor='owner'").fetchone()
        assert hidrive._reauth_complete(row, deadline=hidrive.time.monotonic() + 30)[0] == "authenticated"
        assert hidrive.config_value("115_cookie", "ENV_115_COOKIES") == COOKIE_A


class TestOneAccountPerTransfer:
    """R02: the verification, the preview and the receive are one account."""

    def test_the_gate_verifies_the_cookie_it_resolved(self, client, hidrive, member, monkeypatch):
        with hidrive.connect_db() as conn:
            user_115.secret_set(conn, member, user_115.COOKIE_SECRET, COOKIE_A,
                                fernet=hidrive.load_fernet(), now=1)
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        seen = []
        monkeypatch.setattr(hidrive, "_115_verify_with_uid",
                            lambda cookie=None, deadline=None: (
                                seen.append(cookie) or ({"state": "valid", "error_code": None,
                                                         "checked_at": hidrive.utc_now(),
                                                         "retry_after": 0}, "fake-uid")))
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
            hidrive._115_transfer_gate(deadline=hidrive.time.monotonic() + 30)
        assert seen == [COOKIE_A], "the gate must verify its own resolved cookie"

    def test_no_in_request_call_falls_back_to_the_deployments_cookie(self, hidrive):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        with hidrive.app.test_request_context("/"):
            result, _uid = hidrive._115_verify_with_uid()
        assert result["state"] == "unconfigured", "an anonymous in-request call gets nothing"


class TestMemberTargetFolder:
    """R03: a member never inherits the administrator's folder."""

    def test_with_only_step_a_a_member_gets_no_cid_at_all(self, client, hidrive, member):
        hidrive.setting_set("115_target_pid", "admin-folder-cid")
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
            pid, error = hidrive._resolve_115_target_pid({})
        assert (pid, error) == ("", None), "no cid means this member's own 115 inbox"

    def test_choosing_a_folder_without_step_b_is_guided_not_guessed(self, client, hidrive, member):
        hidrive.setting_set("115_target_pid", "admin-folder-cid")
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
            pid, error = hidrive._resolve_115_target_pid({"target_pid": "admin-folder-cid"})
        assert pid == ""
        assert error is not None
        body = error[0].get_json() if isinstance(error, tuple) else error.get_json()
        assert body["code"] in {"OPEN115_NOT_AUTHORIZED", "OPEN115_FOLDER_UNAVAILABLE"}

    def test_refusing_never_first_syncs_the_administrators_openlist(self, client, hidrive, member, monkeypatch):
        calls = []
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source",
                            lambda *a, **k: calls.append(1))
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
            hidrive._resolve_115_target_pid({"target_pid": "whatever"})
        assert calls == [], "identity and capability are decided before any upstream work"

    def test_the_administrators_own_default_still_applies_to_them(self, client, hidrive):
        hidrive.setting_set("115_target_pid", "admin-folder-cid")
        with hidrive.app.test_request_context("/"):
            hidrive.g.principal = {"sub": "owner", "email": hidrive.ADMIN_EMAIL}
            pid, error = hidrive._resolve_115_target_pid({})
        assert (pid, error) == ("admin-folder-cid", None)

    @pytest.mark.parametrize("valid", [True, False])
    def test_admin_personal_picker_cid_uses_personal_validation(self, client, hidrive, monkeypatch, valid):
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            with db:
                user_115.secret_set(db, admin_id, user_115.OPEN_ACCESS_SECRET,
                                    "personal-open-fixture", fernet=hidrive.load_fernet(), now=1)
        calls = []
        def validate(user, cid, *, deadline=None):
            calls.append((user.id, cid, deadline))
            return valid
        monkeypatch.setattr(hidrive, "validate_user_target_cid", validate)
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source",
                            lambda: pytest.fail("personal folder must not use legacy allowlist"))
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=admin_id, email="admin", role="admin", status="active")
            pid, error = hidrive._resolve_115_target_pid({"target_pid": "tv-folder", "target_path": ""}, deadline=123)
        assert calls == [(admin_id, "tv-folder", 123)]
        if valid:
            assert (pid, error) == ("tv-folder", None)
        else:
            assert pid == ""
            assert error[1] == 400
            assert error[0].get_json()["code"] == "TARGET_PID_INVALID"

    @pytest.mark.parametrize("body,expected,error_code", [
        ({}, "legacy-default", None),
        ({"target_path": "/115pan/电视剧", "target_pid": "resolved-tv"}, "resolved-tv", None),
        ({"target_path": "/115pan/电视剧", "target_pid": "wrong"}, "", "TARGET_PID_INVALID"),
    ])
    def test_admin_own_token_preserves_legacy_path_and_default(
            self, client, hidrive, monkeypatch, body, expected, error_code):
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            with db:
                user_115.secret_set(db, admin_id, user_115.OPEN_ACCESS_SECRET,
                                    "personal-open-fixture", fernet=hidrive.load_fernet(), now=1)
        hidrive.setting_set("115_target_pid", "legacy-default")
        monkeypatch.setattr(hidrive, "resolve_115_target_path", lambda path, deadline: ("resolved-tv", None))
        monkeypatch.setattr(hidrive, "validate_user_target_cid",
                            lambda *a, **k: pytest.fail("legacy request must use legacy path/default"))
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = auth.CurrentUser(id=admin_id, email="admin", role="admin", status="active")
            pid, error = hidrive._resolve_115_target_pid(body)
        assert pid == expected
        if error_code:
            assert error[1] == 400
            assert error[0].get_json()['code'] == error_code
        else:
            assert error is None

    @pytest.mark.parametrize("endpoint", ["/api/115/save", "/api/library/transfer"])
    @pytest.mark.parametrize("valid", [True, False])
    def test_admin_picked_folder_reaches_transfer_only_after_own_token_proof(
            self, client, hidrive, http, installed_library, endpoint, valid):
        from tests.conftest import FakeResponse
        from tests.test_115_save import USER_URL, SNAP_URL, RECEIVE_URL, OPEN_FILES_URL

        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            with db:
                user_115.secret_set(db, admin_id, user_115.OPEN_ACCESS_SECRET,
                                    "personal-open-fixture", fernet=hidrive.load_fernet(), now=1)
        hidrive.secret_set("115_cookie", COOKIE_A)
        hidrive.setting_set("115_target_pid", "legacy-default")
        def folders(**kwargs):
            assert kwargs['headers']['Authorization'] == 'Bearer personal-open-fixture'
            cid = kwargs['params']['cid']
            if cid == '0':
                return FakeResponse({'state': True, 'data': [{'fid': 'tv-folder', 'fn': '电视剧', 'fc': '0'}]})
            assert cid == 'tv-folder'
            return FakeResponse({'state': valid, 'data': []})
        http.route('GET', OPEN_FILES_URL, handler=folders)
        http.route('GET', USER_URL, {'state': True, 'data': {'uid': '42'}})
        http.route('GET', SNAP_URL, {'state': True, 'data': {'list': [{'fid': 'fixture-file'}]}})
        http.route('POST', RECEIVE_URL, {'state': True})
        picker = client.get('/api/115/folders').get_json()
        assert picker['mode'] == '115'
        selected = picker['items'][0]['cid']
        body = {'target_pid': selected, 'target_path': ''}
        if endpoint == '/api/115/save':
            body['share_url'] = 'https://115.com/s/swabc123?password=k9x8'
        else:
            body['resource_link_id'] = 'pub-fixture-01'
        response = client.post(endpoint, json=body)
        if valid:
            assert response.status_code == 200, response.get_json()
            assert http.calls_to(RECEIVE_URL)[0]['data']['cid'] == selected
        else:
            assert response.status_code == 400
            assert response.get_json()['code'] == 'TARGET_PID_INVALID'
            assert http.calls_to(RECEIVE_URL) == []


class TestDisconnectIsRemembered:
    """R18: an explicit disconnect stops the legacy fallback."""

    def test_the_administrator_stops_using_the_legacy_cookie_after_disconnecting(self, client, hidrive):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        assert client.get("/api/me/115/status").get_json()["transfer"]["state"] == "connected"
        assert client.post("/api/me/115/disconnect", json={"step": "transfer"}).status_code == 200
        assert client.get("/api/me/115/status").get_json()["transfer"]["state"] != "connected"

    def test_but_the_legacy_row_itself_is_kept_for_a_rollback(self, client, hidrive):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        client.post("/api/me/115/disconnect", json={"step": "transfer"})
        assert hidrive.config_value("115_cookie", "ENV_115_COOKIES") == "the-administrators-cookie"

    def test_disconnecting_one_step_leaves_the_other_connected(self, client, hidrive):
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            user_115.secret_set(db, admin_id, user_115.COOKIE_SECRET, COOKIE_A,
                                fernet=hidrive.load_fernet(), now=1)
            user_115.secret_set(db, admin_id, user_115.OPEN_ACCESS_SECRET, "fake-access",
                                fernet=hidrive.load_fernet(), now=1)
            user_115.secret_set(db, admin_id, user_115.OPEN_REFRESH_SECRET, "fake-refresh",
                                fernet=hidrive.load_fernet(), now=1)
        client.post("/api/me/115/disconnect", json={"step": "browse"})
        with hidrive.connect_db() as db:
            credentials = user_115.credentials_for(db, admin_id, fernet=hidrive.load_fernet())
        assert credentials.has_cookie is True and credentials.has_open_token is False

    def test_reconnecting_clears_the_disconnected_state(self, client, hidrive, scan_succeeds):
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        client.post("/api/me/115/disconnect", json={"step": "transfer"})
        scan_succeeds(COOKIE_A)
        hidrive._reauth_complete(_challenge_row(hidrive, user_id=admin_id),
                                 deadline=hidrive.time.monotonic() + 30)
        assert client.get("/api/me/115/status").get_json()["transfer"]["state"] == "connected"


class TestTransferDedupeIsPerUser:
    """R19: two members saving the same share are two transfers."""

    def test_two_users_do_not_collide_in_the_window(self, hidrive):
        assert hidrive._transfer_dedupe_check("pub-1", "", "user:1") is True
        assert hidrive._transfer_dedupe_check("pub-1", "", "user:2") is True, \
            "a second member's own transfer is not a duplicate"

    def test_the_same_user_twice_is_still_a_duplicate(self, hidrive):
        assert hidrive._transfer_dedupe_check("pub-2", "", "user:1") is True
        assert hidrive._transfer_dedupe_check("pub-2", "", "user:1") is False

    def test_the_key_carries_the_user(self, hidrive):
        hidrive._TRANSFER_DEDUPE_SEEN.clear()
        hidrive._transfer_dedupe_check("pub-3", "cid-9", "user:7")
        assert ("user:7", "pub-3", "cid-9") in hidrive._TRANSFER_DEDUPE_SEEN


# ---------------------------------------------------------------------------
# Review 2026-09-12 (round 4): a pasted cookie must be the one that is used
# ---------------------------------------------------------------------------


class TestAPastedCookieIsTheOneUsed:
    """The settings page wrote only the legacy global slot, but once a scan has
    filled the administrator's own slot the read path prefers that. So the
    page said "saved, valid" while every transfer kept using the older
    scanned value."""

    def _admin_with_scanned_cookie(self, hidrive):
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            with db:
                user_115.secret_set(db, admin_id, user_115.COOKIE_SECRET, "scanned-earlier-fixture",
                                    fernet=hidrive.load_fernet(), now=1)
        hidrive.secret_set("115_cookie", "scanned-earlier-fixture")
        return admin_id

    def test_the_pasted_value_replaces_the_scanned_one_everywhere(self, client, hidrive, http, workspace):
        admin_id = self._admin_with_scanned_cookie(hidrive)
        http.route("GET", "https://my.115.com/?ct=ajax&ac=get_user_aq", {"state": True, "data": {"uid": "42"}})
        response = client.post("/api/settings", json={"115_cookie": "pasted-later-fixture"})
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["cookie_check"]["valid"] is True

        with hidrive.app.test_request_context("/"):
            hidrive.g.principal = {"sub": "owner", "email": hidrive.ADMIN_EMAIL}
            used = hidrive.user_115_cookie(hidrive.current_user())
        assert used == "pasted-later-fixture", "the transfer path still used the older scanned cookie"
        with hidrive.connect_db() as db:
            own = user_115.secret_get(db, admin_id, user_115.COOKIE_SECRET, fernet=hidrive.load_fernet())
        assert own == "pasted-later-fixture"
        assert hidrive.secret_get("115_cookie") == "pasted-later-fixture", "the legacy slot stays in step for a rollback"

    def test_the_verification_the_page_reports_is_of_the_value_that_will_be_used(self, client, hidrive, http, workspace):
        self._admin_with_scanned_cookie(hidrive)
        seen: list[str] = []
        from conftest import FakeResponse

        def check(**kwargs):
            seen.append(str(kwargs.get("headers", {}).get("Cookie", "")))
            return FakeResponse({"state": True, "data": {"uid": "42"}}, 200)
        http.route("GET", "https://my.115.com/?ct=ajax&ac=get_user_aq", handler=check)
        client.post("/api/settings", json={"115_cookie": "pasted-later-fixture"})
        assert seen and all("pasted-later-fixture" in cookie for cookie in seen), seen

    def test_pasting_clears_an_explicit_disconnect(self, client, hidrive, http, workspace):
        admin_id = self._admin_with_scanned_cookie(hidrive)
        with hidrive.connect_db() as db:
            with db:
                user_115.secret_clear(db, admin_id, user_115.COOKIE_SECRET)
                user_115.remember_cookie_state(db, admin_id, state=user_115.STATE_DISCONNECTED,
                                               error_code=None, now=2)
        http.route("GET", "https://my.115.com/?ct=ajax&ac=get_user_aq", {"state": True, "data": {"uid": "42"}})
        assert client.post("/api/settings", json={"115_cookie": "pasted-after-disconnect-fixture"}).status_code == 200
        with hidrive.app.test_request_context("/"):
            hidrive.g.principal = {"sub": "owner", "email": hidrive.ADMIN_EMAIL}
            assert hidrive.user_115_cookie(hidrive.current_user()) == "pasted-after-disconnect-fixture"

    def test_step_b_is_untouched_by_a_pasted_cookie(self, client, hidrive, http, workspace):
        admin_id = self._admin_with_scanned_cookie(hidrive)
        with hidrive.connect_db() as db:
            with db:
                user_115.secret_set(db, admin_id, user_115.OPEN_ACCESS_SECRET, "open-token-fixture",
                                    fernet=hidrive.load_fernet(), now=1)
        http.route("GET", "https://my.115.com/?ct=ajax&ac=get_user_aq", {"state": True, "data": {"uid": "42"}})
        client.post("/api/settings", json={"115_cookie": "pasted-later-fixture"})
        with hidrive.connect_db() as db:
            assert user_115.secret_get(db, admin_id, user_115.OPEN_ACCESS_SECRET,
                                       fernet=hidrive.load_fernet()) == "open-token-fixture"
