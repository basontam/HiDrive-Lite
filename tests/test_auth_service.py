"""Phase 1 of docs/claude-multi-user-auth-115-isolation-re0-policy-plan-20260911.md:
local accounts, approval, sessions, CSRF keys and role checks.

Everything here runs against a throwaway SQLite file and a throwaway master
key -- no production database, no real credential, and no password or token
value is ever written to an assertion message.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service as auth  # noqa: E402

ADMIN_EMAIL = "admin@example.invalid"
GOOD_PASSWORD = "Correct1Horse"


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "hidrive.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # The two tables Phase 1 extends rather than replaces.
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
def key_file(tmp_path):
    path = tmp_path / "master.key"
    path.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    return path


def _member(db, email="member@example.test", *, now=1_800_000_000):
    return auth.create_pending_user(db, email=email, password=GOOD_PASSWORD, display_name="M", now=now)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_it_creates_exactly_the_new_tables(self, db):
        names = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert auth.NEW_TABLES == (
            "auth_user", "auth_identity", "auth_session", "auth_rate_limit",
            "user_secret", "user_115_profile", "user_115_oauth_state", "user_cloud_download_task",
            # F03: a lease a second gunicorn worker can see.
            "user_lock",
        )
        assert set(auth.NEW_TABLES) <= names
        # The challenge table is reused, not replaced.
        assert "user_115_reauth_challenge" not in names

    def test_it_adds_a_nullable_user_id_to_the_two_existing_tables(self, db):
        for table in ("audit_log", "reauth_challenges"):
            columns = {r["name"]: r for r in db.execute(f"PRAGMA table_info({table})")}
            assert "user_id" in columns, table
            assert columns["user_id"]["notnull"] == 0, table
        # The old actor column stays for compatibility.
        assert "actor" in {r["name"] for r in db.execute("PRAGMA table_info(reauth_challenges)")}

    def test_running_it_twice_changes_nothing(self, db):
        before = sorted(r["sql"] or "" for r in db.execute("SELECT sql FROM sqlite_master"))
        auth.ensure_schema(db)
        assert sorted(r["sql"] or "" for r in db.execute("SELECT sql FROM sqlite_master")) == before

    def test_every_new_table_that_needs_one_has_its_index(self, db):
        indexes = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"auth_session_user_active_idx", "user_cloud_task_user_time_idx"} <= indexes


# ---------------------------------------------------------------------------
# §5.3 passwords
# ---------------------------------------------------------------------------


class TestPassword:
    @pytest.mark.parametrize("password,reason", [
        ("Ab1cdef", "too_short"),
        ("A" + "b1" * 64, "too_long"),
        ("abcdefg1", "needs_upper"),
        ("ABCDEFG1", "needs_lower"),
        ("Abcdefgh", "needs_digit"),
    ])
    def test_it_rejects_a_password_that_breaks_a_rule(self, password, reason):
        with pytest.raises(auth.PasswordRejected) as exc:
            auth.validate_password(password)
        assert exc.value.reason == reason

    def test_it_accepts_a_password_that_meets_every_rule(self):
        auth.validate_password(GOOD_PASSWORD)
        auth.validate_password("Ab1" + "x" * 125)  # exactly 128

    def test_it_never_trims_what_the_user_typed(self, db):
        spaced = "  Correct1Horse  "
        user_id = auth.create_pending_user(db, email="s@example.test", password=spaced, display_name=None, now=1)
        row = db.execute("SELECT password_hash FROM auth_user WHERE id=?", (user_id,)).fetchone()
        assert auth.verify_password(row["password_hash"], spaced)
        assert not auth.verify_password(row["password_hash"], spaced.strip())

    def test_the_stored_hash_is_scrypt_and_not_the_password(self, db):
        user_id = _member(db)
        stored = db.execute("SELECT password_hash FROM auth_user WHERE id=?", (user_id,)).fetchone()["password_hash"]
        assert stored.startswith("scrypt:")
        assert GOOD_PASSWORD not in stored


# ---------------------------------------------------------------------------
# §5.3 accounts and approval
# ---------------------------------------------------------------------------


class TestAccounts:
    def test_an_application_creates_a_pending_user_and_no_session(self, db):
        user_id = _member(db)
        row = db.execute("SELECT * FROM auth_user WHERE id=?", (user_id,)).fetchone()
        assert (row["role"], row["status"]) == ("member", "pending")
        assert db.execute("SELECT COUNT(*) AS c FROM auth_session").fetchone()["c"] == 0

    def test_an_email_is_unique_after_normalisation(self, db):
        _member(db, email="Member@Example.TEST")
        with pytest.raises(auth.EmailTaken):
            _member(db, email="member@example.test")

    def test_normalisation_folds_case_and_width_but_keeps_the_display_form(self, db):
        user_id = auth.create_pending_user(db, email="  Ｍember@Example.test ", password=GOOD_PASSWORD,
                                           display_name=None, now=1)
        row = db.execute("SELECT email_norm, email_display FROM auth_user WHERE id=?", (user_id,)).fetchone()
        assert row["email_norm"] == "member@example.test"
        assert row["email_display"] == "Ｍember@Example.test"

    def test_the_admin_address_cannot_be_claimed_by_an_application(self, db):
        with pytest.raises(auth.EmailReserved):
            auth.create_pending_user(db, email=ADMIN_EMAIL, password=GOOD_PASSWORD, display_name=None, now=1)

    def test_an_application_cannot_ask_for_a_role_or_a_status(self):
        import inspect
        params = set(inspect.signature(auth.create_pending_user).parameters)
        assert not params & {"role", "status", "approved_by", "approved_at"}

    def test_approval_activates_the_user_and_records_who_did_it(self, db):
        admin_id = auth.ensure_admin_user(db, email=ADMIN_EMAIL, now=1)
        user_id = _member(db)
        auth.approve_user(db, user_id, approver_id=admin_id, now=2_000)
        row = db.execute("SELECT * FROM auth_user WHERE id=?", (user_id,)).fetchone()
        assert (row["status"], row["approved_by"], row["approved_at"]) == ("active", admin_id, 2_000)

    def test_rejecting_and_disabling_move_the_user_out_of_active(self, db):
        admin_id = auth.ensure_admin_user(db, email=ADMIN_EMAIL, now=1)
        rejected = _member(db, email="r@example.test")
        auth.reject_user(db, rejected, approver_id=admin_id, now=5)
        assert db.execute("SELECT status FROM auth_user WHERE id=?", (rejected,)).fetchone()["status"] == "rejected"
        active = _member(db, email="d@example.test")
        auth.approve_user(db, active, approver_id=admin_id, now=6)
        auth.disable_user(db, active, approver_id=admin_id, now=7)
        row = db.execute("SELECT status, disabled_at FROM auth_user WHERE id=?", (active,)).fetchone()
        assert (row["status"], row["disabled_at"]) == ("disabled", 7)

    def test_disabling_revokes_every_live_session_at_once(self, db):
        admin_id = auth.ensure_admin_user(db, email=ADMIN_EMAIL, now=1)
        user_id = _member(db)
        auth.approve_user(db, user_id, approver_id=admin_id, now=2)
        first = auth.issue_session(db, user_id, now=10)
        second = auth.issue_session(db, user_id, now=11)
        auth.disable_user(db, user_id, approver_id=admin_id, now=12)
        assert auth.load_session(db, first.token, now=13) is None
        assert auth.load_session(db, second.token, now=13) is None

    def test_the_admin_user_is_created_once_and_never_carries_a_password(self, db):
        first = auth.ensure_admin_user(db, email=ADMIN_EMAIL, now=1)
        again = auth.ensure_admin_user(db, email=ADMIN_EMAIL, now=2)
        assert first == again
        row = db.execute("SELECT role, status, password_hash FROM auth_user WHERE id=?", (first,)).fetchone()
        assert (row["role"], row["status"], row["password_hash"]) == ("admin", "active", None)


# ---------------------------------------------------------------------------
# §5.3 authenticate: one answer for every failure
# ---------------------------------------------------------------------------


class TestAuthenticate:
    def _cases(self, db):
        admin_id = auth.ensure_admin_user(db, email=ADMIN_EMAIL, now=1)
        pending = _member(db, email="pending@example.test")
        rejected = _member(db, email="rejected@example.test")
        auth.reject_user(db, rejected, approver_id=admin_id, now=2)
        disabled = _member(db, email="disabled@example.test")
        auth.approve_user(db, disabled, approver_id=admin_id, now=3)
        auth.disable_user(db, disabled, approver_id=admin_id, now=4)
        active = _member(db, email="active@example.test")
        auth.approve_user(db, active, approver_id=admin_id, now=5)
        return {"pending": pending, "rejected": rejected, "disabled": disabled, "active": active}

    def test_only_an_approved_user_with_the_right_password_gets_in(self, db):
        ids = self._cases(db)
        ok = auth.authenticate(db, email="active@example.test", password=GOOD_PASSWORD, now=10)
        assert ok.user_id == ids["active"]
        assert ok.ok is True

    @pytest.mark.parametrize("email,password,reason", [
        ("nobody@example.test", GOOD_PASSWORD, "no_such_user"),
        ("pending@example.test", GOOD_PASSWORD, "pending"),
        ("rejected@example.test", GOOD_PASSWORD, "rejected"),
        ("disabled@example.test", GOOD_PASSWORD, "disabled"),
        ("active@example.test", "Wrong1Password", "bad_password"),
    ])
    def test_every_failure_looks_the_same_from_outside(self, db, email, password, reason):
        self._cases(db)
        result = auth.authenticate(db, email=email, password=password, now=10)
        assert result.ok is False
        assert result.message == auth.LOGIN_FAILED_MESSAGE
        # The precise reason exists for the audit trail, never for the caller
        # to render.
        assert result.audit_reason == reason

    def test_an_unknown_address_still_costs_a_password_verification(self, db, monkeypatch):
        """§5.3 asks for comparable timing: the lookup must not return early
        on a missing user, or the response time answers 'does this account
        exist?' on its own."""
        self._cases(db)
        calls = []
        real = auth.verify_password
        monkeypatch.setattr(auth, "verify_password", lambda h, p: calls.append(1) or real(h, p))
        auth.authenticate(db, email="nobody@example.test", password=GOOD_PASSWORD, now=10)
        assert calls, "a missing user must still run the hash comparison"

    def test_a_successful_login_stamps_last_login(self, db):
        ids = self._cases(db)
        auth.authenticate(db, email="active@example.test", password=GOOD_PASSWORD, now=77)
        assert db.execute("SELECT last_login_at FROM auth_user WHERE id=?", (ids["active"],)).fetchone()[0] == 77


# ---------------------------------------------------------------------------
# §5.4 sessions
# ---------------------------------------------------------------------------


class TestSessions:
    def test_the_database_stores_only_a_hash_of_the_token(self, db):
        user_id = _member(db)
        issued = auth.issue_session(db, user_id, now=100)
        rows = [r["token_hash"] for r in db.execute("SELECT token_hash FROM auth_session")]
        assert issued.token not in rows
        assert rows == [auth.hash_token(issued.token)]

    def test_the_token_carries_at_least_256_bits(self, db):
        issued = auth.issue_session(db, _member(db), now=100)
        assert len(base64.urlsafe_b64decode(issued.token + "=" * (-len(issued.token) % 4))) >= 32

    def test_two_sessions_never_share_a_token(self, db):
        user_id = _member(db)
        tokens = {auth.issue_session(db, user_id, now=100 + i).token for i in range(5)}
        assert len(tokens) == 5

    def test_a_session_dies_at_the_absolute_cap_however_active_it_was(self, db):
        user_id = _member(db)
        issued = auth.issue_session(db, user_id, now=1_000)
        assert auth.SESSION_MAX_SECONDS == 24 * 3600
        for step in range(1, 25):  # touched every hour
            assert auth.load_session(db, issued.token, now=1_000 + step * 3_000) is not None
        assert auth.load_session(db, issued.token, now=1_000 + auth.SESSION_MAX_SECONDS + 1) is None

    def test_an_idle_session_expires_before_the_cap(self, db):
        issued = auth.issue_session(db, _member(db), now=1_000)
        assert auth.load_session(db, issued.token, now=1_000 + auth.SESSION_IDLE_SECONDS + 1) is None

    def test_rotation_replaces_the_token_and_kills_the_old_one(self, db):
        user_id = _member(db)
        first = auth.issue_session(db, user_id, now=1_000)
        second = auth.rotate_session(db, first.token, now=1_100)
        assert second.token != first.token
        assert auth.load_session(db, first.token, now=1_101) is None
        assert auth.load_session(db, second.token, now=1_101).user_id == user_id

    def test_revoking_one_session_leaves_the_others_alone(self, db):
        user_id = _member(db)
        first = auth.issue_session(db, user_id, now=1_000)
        second = auth.issue_session(db, user_id, now=1_001)
        auth.revoke_session(db, first.token, now=1_002)
        assert auth.load_session(db, first.token, now=1_003) is None
        assert auth.load_session(db, second.token, now=1_003) is not None

    def test_an_unknown_or_malformed_token_is_simply_no_session(self, db):
        for token in ("", "nonsense", "a" * 64):
            assert auth.load_session(db, token, now=1_000) is None


# ---------------------------------------------------------------------------
# §5.4 CSRF, on a purpose-separated key
# ---------------------------------------------------------------------------


class TestCsrf:
    def test_the_key_is_derived_for_this_purpose_and_is_not_fernets_own(self, key_file):
        from cryptography.fernet import Fernet

        raw = key_file.read_bytes().strip()
        csrf_key = auth.derive_key("csrf/v1", raw)
        other = auth.derive_key("something-else/v1", raw)
        assert csrf_key != other, "two purposes must not share a key"
        assert len(csrf_key) == 32
        # Never the private attribute of a third-party class, and never the
        # key Fernet itself signs ciphertext with.
        assert csrf_key != Fernet(raw)._signing_key
        assert csrf_key != base64.urlsafe_b64decode(raw)

    def test_the_same_master_key_always_derives_the_same_subkey(self, key_file):
        raw = key_file.read_bytes().strip()
        assert auth.derive_key("csrf/v1", raw) == auth.derive_key("csrf/v1", raw)

    def test_a_different_master_key_derives_a_different_subkey(self, key_file, tmp_path):
        other = base64.urlsafe_b64encode(os.urandom(32))
        assert auth.derive_key("csrf/v1", key_file.read_bytes().strip()) != auth.derive_key("csrf/v1", other)

    def test_a_token_verifies_for_its_own_subject_and_no_other(self, key_file):
        key = auth.derive_key("csrf/v1", key_file.read_bytes().strip())
        token = auth.issue_csrf(key, subject="user:7", now=1_000)
        assert auth.verify_csrf(key, token, subject="user:7", now=1_000)
        assert not auth.verify_csrf(key, token, subject="user:8", now=1_000)

    def test_a_token_expires_and_a_tampered_one_never_verifies(self, key_file):
        key = auth.derive_key("csrf/v1", key_file.read_bytes().strip())
        token = auth.issue_csrf(key, subject="user:7", now=1_000)
        assert not auth.verify_csrf(key, token, subject="user:7", now=1_000 + auth.CSRF_TTL_SECONDS + 1)
        assert not auth.verify_csrf(key, token[:-2] + "xy", subject="user:7", now=1_000)
        assert not auth.verify_csrf(key, "", subject="user:7", now=1_000)

    def test_a_token_from_a_different_key_is_rejected(self, key_file):
        good = auth.derive_key("csrf/v1", key_file.read_bytes().strip())
        stale = auth.derive_key("csrf/v1", base64.urlsafe_b64encode(os.urandom(32)))
        token = auth.issue_csrf(stale, subject="user:7", now=1_000)
        assert not auth.verify_csrf(good, token, subject="user:7", now=1_000)


# ---------------------------------------------------------------------------
# §5.4 rate limiting
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_it_blocks_after_the_limit_and_frees_the_bucket_next_window(self, db):
        bucket = auth.rate_bucket("login", ip="203.0.113.7", email_norm="a@example.test")
        for attempt in range(auth.LOGIN_ATTEMPT_LIMIT):
            assert auth.check_rate_limit(db, bucket, now=1_000 + attempt) is True
        assert auth.check_rate_limit(db, bucket, now=1_010) is False
        assert auth.check_rate_limit(db, bucket, now=1_000 + auth.LOGIN_ATTEMPT_WINDOW + 1) is True

    def test_the_bucket_is_a_hash_and_carries_neither_address_nor_email(self, db):
        bucket = auth.rate_bucket("login", ip="203.0.113.7", email_norm="a@example.test")
        auth.check_rate_limit(db, bucket, now=1_000)
        stored = db.execute("SELECT bucket_hash FROM auth_rate_limit").fetchone()["bucket_hash"]
        assert "203.0.113.7" not in stored and "a@example.test" not in stored
        assert len(stored) == 64

    def test_two_addresses_do_not_share_a_budget(self, db):
        first = auth.rate_bucket("login", ip="203.0.113.7", email_norm="a@example.test")
        second = auth.rate_bucket("login", ip="203.0.113.8", email_norm="a@example.test")
        for attempt in range(auth.LOGIN_ATTEMPT_LIMIT):
            auth.check_rate_limit(db, first, now=1_000 + attempt)
        assert auth.check_rate_limit(db, first, now=1_010) is False
        assert auth.check_rate_limit(db, second, now=1_010) is True


# ---------------------------------------------------------------------------
# §6 capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_an_admin_can_reach_everything(self):
        caps = auth.capabilities(role="admin", allow_member_re0_unlock=False,
                                 has_115_cookie=False, has_115_open=False)
        for name in ("library", "openlist", "strm", "global_settings", "re0_unlock"):
            assert caps[name] is True, name

    def test_a_member_never_gets_the_admin_surfaces(self):
        caps = auth.capabilities(role="member", allow_member_re0_unlock=True,
                                 has_115_cookie=True, has_115_open=True)
        for name in ("openlist", "strm", "global_settings"):
            assert caps[name] is False, name
        assert caps["library"] is True

    def test_a_members_unlock_follows_the_global_switch_only(self):
        off = auth.capabilities(role="member", allow_member_re0_unlock=False,
                                has_115_cookie=True, has_115_open=True)
        on = auth.capabilities(role="member", allow_member_re0_unlock=True,
                               has_115_cookie=True, has_115_open=True)
        assert off["re0_unlock"] is False and on["re0_unlock"] is True

    def test_the_115_capabilities_follow_that_users_own_authorisations(self):
        neither = auth.capabilities(role="member", allow_member_re0_unlock=False,
                                    has_115_cookie=False, has_115_open=False)
        assert neither["own_115_transfer"] is False
        assert neither["own_115_browse"] is False
        assert neither["own_115_cloud_download"] is False
        cookie_only = auth.capabilities(role="member", allow_member_re0_unlock=False,
                                        has_115_cookie=True, has_115_open=False)
        # Step A alone is enough to save to the default inbox, and not enough
        # to browse folders or queue a cloud download.
        assert cookie_only["own_115_transfer"] is True
        assert cookie_only["own_115_browse"] is False
        assert cookie_only["own_115_cloud_download"] is False
        open_only = auth.capabilities(role="member", allow_member_re0_unlock=False,
                                      has_115_cookie=False, has_115_open=True)
        assert open_only["own_115_transfer"] is False
        assert open_only["own_115_browse"] is True
        assert open_only["own_115_cloud_download"] is True

    def test_an_admin_keeps_the_same_115_honesty(self):
        caps = auth.capabilities(role="admin", allow_member_re0_unlock=False,
                                 has_115_cookie=False, has_115_open=False)
        assert caps["own_115_browse"] is False

    def test_a_subject_containing_a_colon_round_trips(self, key_file):
        """A session subject reads `user:<id>`, and an HMAC digest is raw
        bytes that may hold a colon of its own -- neither may be parsed by
        splitting on the separator."""
        key = auth.derive_key("csrf/v1", key_file.read_bytes().strip())
        for subject in ("user:7", "user:12345", "cf:sub:with:colons"):
            token = auth.issue_csrf(key, subject=subject, now=1_000)
            assert auth.verify_csrf(key, token, subject=subject, now=1_000)
            assert not auth.verify_csrf(key, token, subject=subject + "8", now=1_000)
