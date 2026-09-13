"""Local accounts, approval, sessions, CSRF keys and role checks.

Phase 1 of docs/claude-multi-user-auth-115-isolation-re0-policy-plan-20260911.md.
This module owns the auth half of ``hidrive.db``: it creates its own tables,
hashes and checks passwords, runs the application/approval state machine,
issues and loads server-side sessions, mints CSRF tokens on a
purpose-separated subkey of the master key, and answers "what may this role
do". It holds no Flask state and no route: ``app.py`` wires it up.

Deliberate boundaries:

* Nothing here reads or writes a 115/RE0/TMDB credential. User-level 115
  material lives in ``user_secret`` rows written by the migration CLI and
  (later) ``user_115.py``; this module only declares the table.
* No function takes ``role``, ``status`` or ``approved_by`` from a caller
  that could be a request body -- an application can only ever create a
  ``pending`` ``member`` (plan §17).
* Failure reasons are returned for the audit trail, never for display: a
  login that fails answers with one message whatever went wrong (plan §5.3).
* The database stores ``SHA-256(session token)``, never the token, and the
  rate limiter stores a hash of its bucket, never the address or the email.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

# The plan's §7 listed nine tables; `user_115_reauth_challenge` is not among
# them here. `reauth_challenges` already exists, already carries the same
# columns under different names, and its routes already refuse a challenge
# that belongs to another actor -- so it gains a nullable `user_id` instead
# of being duplicated (2026-09-11 decision).
NEW_TABLES: tuple[str, ...] = (
    "auth_user",
    "auth_identity",
    "auth_session",
    "auth_rate_limit",
    "user_secret",
    "user_115_profile",
    "user_115_oauth_state",
    "user_cloud_download_task",
    # Review F03: a cross-worker lease. The deployment runs two gunicorn
    # workers, so a lock that lives in one process's memory coordinates
    # nothing -- four overlapping requests refreshed a token four times.
    "user_lock",
)

# Tables that predate this module and gain a nullable `user_id`. Nullable and
# additive on purpose: old rows keep their `actor`, and code that rolls back
# to the previous release still reads and writes them unchanged.
EXTENDED_TABLES: tuple[tuple[str, str, str], ...] = (
    ("audit_log", "user_id", "INTEGER"),
    ("reauth_challenges", "user_id", "INTEGER"),
    # A database created by Phase 1-5 already has user_cloud_download_task
    # without these two, and CREATE TABLE IF NOT EXISTS will not add them.
    # Without the upgrade the task list fails with "no such column"
    # (review R16).
    ("user_cloud_download_task", "group_id", "INTEGER"),
    ("user_cloud_download_task", "link_label", "TEXT"),
    # Review F03: which generation of a credential a caller was holding. A
    # request that failed on version 4 must not overwrite the state a
    # request that succeeded on version 5 just wrote, and the holder of the
    # refresh lease re-reads this to see whether somebody already rotated.
    ("user_secret", "version", "INTEGER NOT NULL DEFAULT 0"),
    # Review F07: when --auth-migrate --apply last copied the administrator's
    # configuration into this row. An explicit marker, because "the default
    # folder is empty" cannot tell "never migrated" apart from "the user
    # cleared it on purpose", and a re-run was restoring the old values over
    # the second.
    ("user_115_profile", "migrated_at", "INTEGER"),
    # Review F02: who is responsible for rotating this token pair.
    # `openlist_legacy` is the administrator's migrated credential, still
    # rotated by OpenList; `own_app` is one this deployment's own 115
    # application issued. Two systems must never rotate the same refresh
    # token, so the answer has to be recorded rather than guessed.
    ("user_115_profile", "open_token_origin", "TEXT"),
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auth_user (
  id INTEGER PRIMARY KEY,
  email_norm TEXT NOT NULL UNIQUE,
  email_display TEXT NOT NULL,
  password_hash TEXT,
  display_name TEXT,
  role TEXT NOT NULL CHECK (role IN ('admin','member')),
  status TEXT NOT NULL CHECK (status IN ('pending','active','rejected','disabled')),
  created_at INTEGER NOT NULL,
  approved_at INTEGER,
  approved_by INTEGER REFERENCES auth_user(id),
  disabled_at INTEGER,
  last_login_at INTEGER
);

CREATE TABLE IF NOT EXISTS auth_identity (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES auth_user(id),
  provider TEXT NOT NULL CHECK (provider IN ('cloudflare_google')),
  subject TEXT NOT NULL,
  email_at_link TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  UNIQUE(provider, subject)
);

CREATE TABLE IF NOT EXISTS auth_session (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES auth_user(id),
  csrf_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  revoked_at INTEGER,
  ip_hash TEXT,
  user_agent_hash TEXT
);
CREATE INDEX IF NOT EXISTS auth_session_user_active_idx
  ON auth_session(user_id, expires_at, revoked_at);

CREATE TABLE IF NOT EXISTS auth_rate_limit (
  bucket_hash TEXT PRIMARY KEY,
  window_started_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL,
  blocked_until INTEGER
);

CREATE TABLE IF NOT EXISTS user_secret (
  user_id INTEGER NOT NULL REFERENCES auth_user(id),
  name TEXT NOT NULL CHECK (name IN (
    '115_web_cookie','115_open_access_token','115_open_refresh_token'
  )),
  ciphertext BLOB NOT NULL,
  updated_at INTEGER NOT NULL,
  -- Bumped on every write (review F03). The caller of an upstream API
  -- carries the version it used, so a failure arriving late cannot
  -- downgrade the state a newer success already recorded.
  version INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, name)
);

CREATE TABLE IF NOT EXISTS user_115_profile (
  user_id INTEGER PRIMARY KEY REFERENCES auth_user(id),
  open_root_cid TEXT,
  default_target_cid TEXT,
  default_target_label TEXT,
  cookie_state TEXT NOT NULL DEFAULT 'unconfigured',
  cookie_checked_at INTEGER,
  cookie_error_code TEXT,
  open_state TEXT NOT NULL DEFAULT 'unconfigured',
  open_expires_at INTEGER,
  open_checked_at INTEGER,
  open_error_code TEXT,
  -- Review F02: 'openlist_legacy' | 'own_app' | NULL (nothing stored yet).
  open_token_origin TEXT,
  -- Review F07: set once by --auth-migrate --apply; a re-run reads it and
  -- copies nothing, so a default the user has since cleared stays cleared.
  migrated_at INTEGER,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_115_oauth_state (
  state_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES auth_user(id),
  provider TEXT NOT NULL DEFAULT '115_open',
  flow TEXT NOT NULL CHECK (flow IN ('device_pkce','authorization_code')),
  code_verifier_ciphertext BLOB,
  upstream_uid_ciphertext BLOB,
  upstream_time_ciphertext BLOB,
  upstream_sign_ciphertext BLOB,
  redirect_uri_hash TEXT,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  last_polled_at INTEGER,
  consumed_at INTEGER
);

CREATE TABLE IF NOT EXISTS user_cloud_download_task (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES auth_user(id),
  info_hash TEXT NOT NULL,
  source_kind TEXT,
  media_id INTEGER,
  display_title TEXT,
  -- Not in the plan's §7 sketch, kept because the cloud-download list shows
  -- them: which resource group a task came from, and the label of the link
  -- that produced it. Dropping them would have regressed that list.
  group_id INTEGER,
  link_label TEXT,
  submitted_at INTEGER NOT NULL,
  last_seen_at INTEGER,
  state TEXT,
  UNIQUE(user_id, info_hash)
);
CREATE INDEX IF NOT EXISTS user_cloud_task_user_time_idx
  ON user_cloud_download_task(user_id, submitted_at DESC);

-- Review F03: one row per thing being serialised, across processes. Same
-- shape and same single-statement take as re0_unlock_lease: a row whose
-- `expires_at` has passed is free, which is what stops a killed worker from
-- holding it forever.
CREATE TABLE IF NOT EXISTS user_lock (
  lock_key TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
"""


USER_LOCK_SECONDS = 30


def acquire_user_lock(db: sqlite3.Connection, lock_key: str, *, holder: str, now: int,
                      ttl: int = USER_LOCK_SECONDS) -> bool:
    """Take the lease on ``lock_key``, or report that somebody holds it.

    Atomic in one statement: the conflicting UPDATE only fires for a lease
    that has already expired, so two workers cannot both believe they hold
    it. Holding it is not by itself permission to act -- the holder must
    re-read whatever state it is protecting, because the previous holder may
    have just changed it (review F03).
    """
    cursor = db.execute(
        "INSERT INTO user_lock(lock_key, holder, acquired_at, expires_at) VALUES(?,?,?,?) "
        "ON CONFLICT(lock_key) DO UPDATE SET holder=excluded.holder, acquired_at=excluded.acquired_at, "
        "expires_at=excluded.expires_at WHERE user_lock.expires_at <= ?",
        (str(lock_key), holder[:160], now, now + ttl, now),
    )
    return bool(cursor.rowcount)


def user_lock_held(db: sqlite3.Connection, lock_key: str, *, holder: str) -> bool:
    """Whether this exact holder still has the lease.

    Review G01.1: a lease that expired and was taken over by somebody else is
    no longer ours, and a result computed under it must not be committed. Read
    inside the transaction that would write, so the answer cannot go stale
    between the check and the commit.
    """
    row = db.execute("SELECT holder FROM user_lock WHERE lock_key=?", (str(lock_key),)).fetchone()
    return row is not None and row["holder"] == holder[:160]


def release_user_lock(db: sqlite3.Connection, lock_key: str, *, holder: str) -> None:
    """Only the holder releases, so a lease taken over after expiry is not
    dropped by the worker it was taken from."""
    db.execute("DELETE FROM user_lock WHERE lock_key=? AND holder=?", (str(lock_key), holder[:160]))


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create the auth tables and add the two nullable columns, idempotently.

    Safe to call on every start: it only ever adds. It moves no data -- the
    administrator's existing credentials are copied by the explicit
    ``--auth-migrate`` command, never by a startup hook (plan §17).
    """
    db.executescript(SCHEMA_SQL)
    # After the CREATE TABLE pass, so a table created just now is present and
    # already has its columns; the loop then only acts on an older one.
    for table, column, column_type in EXTENDED_TABLES:
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _derive(purpose: bytes, raw: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(
        base64.urlsafe_b64decode(raw)
    )


def derive_key(purpose: str, master_key: bytes) -> bytes:
    """A 32-byte subkey of the master key, separated by purpose.

    ``master_key`` is the file's own base64 Fernet key. Deriving keeps one
    use from being usable against another, and keeps this code off
    ``Fernet._signing_key`` -- a private attribute of a third-party class,
    and the very key Fernet signs ciphertext with.
    """
    return _derive(("hidrive-lite/" + purpose).encode("utf-8"), bytes(master_key).strip())


# ---------------------------------------------------------------------------
# passwords and email
# ---------------------------------------------------------------------------

PASSWORD_MIN = 8
PASSWORD_MAX = 128
PASSWORD_HASH_METHOD = "scrypt"
# A hash of a value no one can present, so a login for an address that does
# not exist still pays for one comparison (plan §5.3's timing requirement).
_ABSENT_USER_HASH = generate_password_hash(secrets.token_urlsafe(32), method=PASSWORD_HASH_METHOD)


class AuthError(Exception):
    """Base for the application-level rejections this module raises."""


class PasswordRejected(AuthError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EmailTaken(AuthError):
    pass


class EmailReserved(AuthError):
    pass


def validate_password(password: str) -> None:
    """Plan §5.3. The value is never trimmed or altered on the way through."""
    if len(password) < PASSWORD_MIN:
        raise PasswordRejected("too_short")
    if len(password) > PASSWORD_MAX:
        raise PasswordRejected("too_long")
    if not any("A" <= ch <= "Z" for ch in password):
        raise PasswordRejected("needs_upper")
    if not any("a" <= ch <= "z" for ch in password):
        raise PasswordRejected("needs_lower")
    if not any(ch.isascii() and ch.isdigit() for ch in password):
        raise PasswordRejected("needs_digit")


def hash_password(password: str) -> str:
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD)


def verify_password(password_hash: str | None, password: str) -> bool:
    return bool(password_hash) and check_password_hash(password_hash, password)


def normalize_email(email: str) -> str:
    """NFKC, surrounding whitespace removed, casefolded -- the form the unique
    index is built on. The address as typed is kept separately for display."""
    return unicodedata.normalize("NFKC", email or "").strip().casefold()


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def create_pending_user(db: sqlite3.Connection, *, email: str, password: str,
                        display_name: str | None, now: int, admin_email: str | None = None) -> int:
    """Record an application. The result is always a ``pending`` ``member``:
    no caller can ask for another role or status (plan §17)."""
    validate_password(password)
    email_norm = normalize_email(email)
    if not email_norm or "@" not in email_norm:
        raise PasswordRejected("bad_email")
    if email_norm == normalize_email(admin_email or ADMIN_EMAIL_DEFAULT):
        raise EmailReserved(email_norm)
    if db.execute("SELECT 1 FROM auth_user WHERE email_norm=?", (email_norm,)).fetchone():
        raise EmailTaken(email_norm)
    cursor = db.execute(
        "INSERT INTO auth_user(email_norm, email_display, password_hash, display_name, role, status, created_at) "
        "VALUES(?,?,?,?,'member','pending',?)",
        (email_norm, (email or "").strip(), hash_password(password), display_name, now),
    )
    return int(cursor.lastrowid)


ADMIN_EMAIL_DEFAULT = "admin@example.invalid"


def ensure_admin_user(db: sqlite3.Connection, *, email: str, now: int) -> int:
    """The single administrator row. Idempotent, and never given a password:
    that identity signs in through Cloudflare Access (plan §5.2)."""
    email_norm = normalize_email(email)
    row = db.execute("SELECT id, role, status FROM auth_user WHERE email_norm=?", (email_norm,)).fetchone()
    if row:
        # Only write when something is actually wrong. This runs on every
        # request that resolves the administrator, and a write here would
        # take a lock the request may already be holding elsewhere.
        if row["role"] != "admin" or row["status"] != "active":
            db.execute("UPDATE auth_user SET role='admin', status='active' WHERE id=?", (row["id"],))
        return int(row["id"])
    cursor = db.execute(
        "INSERT INTO auth_user(email_norm, email_display, password_hash, display_name, role, status, created_at) "
        "VALUES(?,?,NULL,NULL,'admin','active',?)",
        (email_norm, (email or "").strip(), now),
    )
    return int(cursor.lastrowid)


def find_user(db: sqlite3.Connection, user_id: int):
    return db.execute("SELECT * FROM auth_user WHERE id=?", (user_id,)).fetchone()


def find_user_by_email(db: sqlite3.Connection, email: str):
    return db.execute("SELECT * FROM auth_user WHERE email_norm=?", (normalize_email(email),)).fetchone()


def _set_status(db: sqlite3.Connection, user_id: int, status: str, *, approver_id: int | None,
                now: int, disabled: bool = False) -> None:
    db.execute(
        "UPDATE auth_user SET status=?, approved_at=?, approved_by=?, disabled_at=? WHERE id=?",
        (status, now if status == "active" else None, approver_id, now if disabled else None, user_id),
    )


def approve_user(db: sqlite3.Connection, user_id: int, *, approver_id: int, now: int) -> None:
    _set_status(db, user_id, "active", approver_id=approver_id, now=now)


def reject_user(db: sqlite3.Connection, user_id: int, *, approver_id: int, now: int) -> None:
    _set_status(db, user_id, "rejected", approver_id=approver_id, now=now)
    revoke_user_sessions(db, user_id, now=now)


def disable_user(db: sqlite3.Connection, user_id: int, *, approver_id: int, now: int) -> None:
    """Disabling takes effect at once: every live session goes with it."""
    _set_status(db, user_id, "disabled", approver_id=approver_id, now=now, disabled=True)
    revoke_user_sessions(db, user_id, now=now)


def bind_identity(db: sqlite3.Connection, *, user_id: int, provider: str, subject: str,
                  email: str, now: int) -> None:
    db.execute(
        "INSERT INTO auth_identity(user_id, provider, subject, email_at_link, created_at, last_seen_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(provider, subject) DO UPDATE SET last_seen_at=excluded.last_seen_at",
        (user_id, provider, subject, email, now, now),
    )


def find_user_by_identity(db: sqlite3.Connection, provider: str, subject: str):
    return db.execute(
        "SELECT u.* FROM auth_user u JOIN auth_identity i ON i.user_id = u.id "
        "WHERE i.provider=? AND i.subject=?",
        (provider, subject),
    ).fetchone()


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------

LOGIN_FAILED_MESSAGE = "邮箱或密码不正确，或账号尚未获批"


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    user_id: int | None = None
    role: str | None = None
    message: str = ""
    # For the audit trail only -- never rendered, or the response would
    # answer "does this account exist, and what state is it in?".
    audit_reason: str = ""


def authenticate(db: sqlite3.Connection, *, email: str, password: str, now: int) -> LoginResult:
    row = find_user_by_email(db, email)
    # Always run a comparison, even with nobody to compare against.
    stored = (row["password_hash"] if row else None) or _ABSENT_USER_HASH
    password_ok = verify_password(stored, password)
    if row is None:
        return LoginResult(False, message=LOGIN_FAILED_MESSAGE, audit_reason="no_such_user")
    if row["status"] != "active":
        return LoginResult(False, message=LOGIN_FAILED_MESSAGE, audit_reason=row["status"])
    if not row["password_hash"]:
        # The administrator identity signs in through Access, not here.
        return LoginResult(False, message=LOGIN_FAILED_MESSAGE, audit_reason="no_password")
    if not password_ok:
        return LoginResult(False, message=LOGIN_FAILED_MESSAGE, audit_reason="bad_password")
    db.execute("UPDATE auth_user SET last_login_at=? WHERE id=?", (now, row["id"]))
    return LoginResult(True, user_id=int(row["id"]), role=row["role"])


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

SESSION_MAX_SECONDS = 24 * 3600
SESSION_IDLE_SECONDS = 8 * 3600
SESSION_TOKEN_BYTES = 32  # 256 bits


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_secret: str
    user_id: int
    expires_at: int


@dataclass(frozen=True)
class LoadedSession:
    user_id: int
    csrf_hash: str
    created_at: int
    expires_at: int


def issue_session(db: sqlite3.Connection, user_id: int, *, now: int,
                  ip_hash: str | None = None, user_agent_hash: str | None = None) -> IssuedSession:
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    csrf_secret = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    expires_at = now + SESSION_MAX_SECONDS
    db.execute(
        "INSERT INTO auth_session(token_hash, user_id, csrf_hash, created_at, expires_at, last_seen_at, ip_hash, user_agent_hash) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (hash_token(token), user_id, hash_token(csrf_secret), now, expires_at, now, ip_hash, user_agent_hash),
    )
    return IssuedSession(token=token, csrf_secret=csrf_secret, user_id=user_id, expires_at=expires_at)


def load_session(db: sqlite3.Connection, token: str, *, now: int, touch: bool = True) -> LoadedSession | None:
    """The live session for this token, or None. Expiry is two-sided: an
    absolute cap no amount of activity extends, and an idle window."""
    if not token:
        return None
    row = db.execute("SELECT * FROM auth_session WHERE token_hash=?", (hash_token(token),)).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    if now >= row["expires_at"] or now - row["last_seen_at"] > SESSION_IDLE_SECONDS:
        return None
    if touch:
        db.execute("UPDATE auth_session SET last_seen_at=? WHERE token_hash=?", (now, row["token_hash"]))
    return LoadedSession(user_id=int(row["user_id"]), csrf_hash=row["csrf_hash"],
                         created_at=int(row["created_at"]), expires_at=int(row["expires_at"]))


def rotate_session(db: sqlite3.Connection, token: str, *, now: int) -> IssuedSession | None:
    """Issue a fresh session for the same user and revoke this one -- after a
    login, a privilege change, or an approval (plan §5.4)."""
    current = load_session(db, token, now=now, touch=False)
    if current is None:
        return None
    revoke_session(db, token, now=now)
    return issue_session(db, current.user_id, now=now)


def revoke_session(db: sqlite3.Connection, token: str, *, now: int) -> None:
    db.execute("UPDATE auth_session SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
               (now, hash_token(token)))


def revoke_user_sessions(db: sqlite3.Connection, user_id: int, *, now: int) -> int:
    cursor = db.execute("UPDATE auth_session SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                        (now, user_id))
    return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

CSRF_TTL_SECONDS = 3600
_CSRF_DIGEST_SIZE = hashlib.sha256().digest_size
_COLON = 0x3A


def issue_csrf(key: bytes, *, subject: str, now: int) -> str:
    payload = f"{subject}:{now}".encode("utf-8")
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b":" + digest).decode("ascii").rstrip("=")


def verify_csrf(key: bytes, token: str, *, subject: str, now: int) -> bool:
    """Constant-time check of a token against this subject and clock.

    Split by length, not by separator: the HMAC is 32 raw bytes that may
    themselves contain a colon, and the subject may contain one too (a
    session subject reads ``user:<id>``). The digest is always the last 32
    bytes, the timestamp is always after the payload's last colon.
    """
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return False
    if len(raw) < _CSRF_DIGEST_SIZE + 2 or raw[-(_CSRF_DIGEST_SIZE + 1)] != _COLON:
        return False
    payload, signature = raw[: -(_CSRF_DIGEST_SIZE + 1)], raw[-_CSRF_DIGEST_SIZE:]
    token_subject, separator, issued = payload.rpartition(b":")
    if not separator:
        return False
    try:
        if token_subject.decode("utf-8") != subject or int(issued) + CSRF_TTL_SECONDS < now:
            return False
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(signature, hmac.new(key, payload, hashlib.sha256).digest())


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------

# Per (source, address-being-tried): how many times one address may be tried
# from one place.
LOGIN_ATTEMPT_LIMIT = 10
LOGIN_ATTEMPT_WINDOW = 600
# Per source, whatever address is tried. Without this, changing the email on
# every attempt buys a fresh budget each time -- and every attempt costs a
# scrypt hash (R13).
SOURCE_ATTEMPT_LIMIT = 30
SOURCE_ATTEMPT_WINDOW = 600
# Per address, whatever source it is tried from. R13 fixed the source side but
# left the second bucket keyed on (source, address), so one account could be
# tried from 50 places for 50 fresh budgets -- and the target of a distributed
# guessing attempt is an *account* (review F06). Deliberately above the
# per-(source, address) limit: a real person retrying from phone, laptop and
# office should not lock themselves out, and this budget expires on its own.
ACCOUNT_ATTEMPT_LIMIT = 20
ACCOUNT_ATTEMPT_WINDOW = 600


def rate_bucket(scope: str, *, ip: str | None, email_norm: str | None) -> str:
    """One opaque bucket per (scope, source, address-being-tried). Hashed, so
    the table never holds an address or an email."""
    material = "|".join((scope, ip or "", email_norm or "")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def source_bucket(scope: str, *, ip: str | None) -> str:
    """The source's own bucket, counted whatever address is being tried."""
    return hashlib.sha256(("source|" + scope + "|" + (ip or "")).encode("utf-8")).hexdigest()


def account_bucket(scope: str, *, email_norm: str | None) -> str:
    """The address's own bucket, counted whatever source is trying it (F06).

    Keyed on the normalised address only, so spreading attempts over many
    source addresses does not buy a fresh budget for the same account. An
    absent address has no account bucket -- there is nothing to protect and
    the source bucket already covers the cost.
    """
    return hashlib.sha256(("account|" + scope + "|" + (email_norm or "")).encode("utf-8")).hexdigest()


def within_attempt_limits(db: sqlite3.Connection, scope: str, *, ip: str | None,
                          email_norm: str | None, now: int) -> bool:
    """Record one attempt against every budget; False once any is spent.

    Three independent dimensions, all recorded on every attempt (F06):

    * the source, whatever address it tries -- changing the email each time
      buys no new budget;
    * the address, whatever source tries it -- coming from 50 places buys no
      new budget for one account;
    * the pair, which is the narrowest and catches ordinary repetition.

    Every one is counted before the answer is returned, so no dimension can
    be starved by another's refusal. Called before any password is hashed and
    outside the transaction that would hold a write lock while it is -- the
    counting transaction is opened and committed here, so it is already over
    by the time the caller starts hashing (G02).
    """
    # G02: all three in one short write transaction, committed before the
    # caller does anything expensive. Each bucket's own decision is already
    # atomic; the transaction is what keeps the three of them from being
    # interleaved with another attempt's three.
    with db:
        source_ok = check_rate_limit(db, source_bucket(scope, ip=ip), now=now,
                                     limit=SOURCE_ATTEMPT_LIMIT, window=SOURCE_ATTEMPT_WINDOW)
        account_ok = True
        if email_norm:
            account_ok = check_rate_limit(db, account_bucket(scope, email_norm=email_norm), now=now,
                                          limit=ACCOUNT_ATTEMPT_LIMIT, window=ACCOUNT_ATTEMPT_WINDOW)
        pair_ok = check_rate_limit(db, rate_bucket(scope, ip=ip, email_norm=email_norm), now=now)
    return source_ok and account_ok and pair_ok


# One statement does the whole decision (review G02). The old shape read the
# row, then in the "no row, or the window has rolled over" branch wrote
# attempts=1 unconditionally -- so a caller that read "no row", paused, and
# resumed after somebody else had created and exhausted the bucket reset the
# count to 1 and doubled the budget for that window. Everything below happens
# inside SQLite's own atomic upsert:
#
#   * the INSERT path is the bucket's first attempt;
#   * ON CONFLICT, `auth_rate_limit.*` are the values *before* this attempt,
#     so the window test, the increment and the block-until stamp all agree on
#     one reading of the row;
#   * RETURNING hands back the post-write count and window, which is what
#     decides allow/refuse -- no second look, nothing to go stale in between.
_RATE_LIMIT_SQL = """
INSERT INTO auth_rate_limit(bucket_hash, window_started_at, attempts, blocked_until)
VALUES(:bucket, :now, 1, NULL)
ON CONFLICT(bucket_hash) DO UPDATE SET
  window_started_at = CASE WHEN :now - auth_rate_limit.window_started_at >= :window
                           THEN :now ELSE auth_rate_limit.window_started_at END,
  attempts          = CASE WHEN :now - auth_rate_limit.window_started_at >= :window
                           THEN 1 ELSE auth_rate_limit.attempts + 1 END,
  blocked_until     = CASE WHEN :now - auth_rate_limit.window_started_at >= :window
                           THEN NULL
                           WHEN auth_rate_limit.attempts + 1 > :limit
                           THEN auth_rate_limit.window_started_at + :window
                           ELSE auth_rate_limit.blocked_until END
RETURNING attempts, window_started_at
"""


def check_rate_limit(db: sqlite3.Connection, bucket_hash: str, *, now: int,
                     limit: int = LOGIN_ATTEMPT_LIMIT, window: int = LOGIN_ATTEMPT_WINDOW) -> bool:
    """Record one attempt; False once this bucket is over its budget.

    Atomic: the window decision, the count and the block stamp are one
    statement, so a concurrent caller holding an older snapshot cannot reset
    the count and hand out a second budget for the same window (G02).
    """
    row = db.execute(_RATE_LIMIT_SQL, {
        "bucket": bucket_hash, "now": now, "window": window, "limit": limit}).fetchone()
    return int(row["attempts"]) <= limit


# ---------------------------------------------------------------------------
# roles and capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    role: str
    status: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def capabilities(*, role: str, allow_member_re0_unlock: bool,
                 has_115_cookie: bool, has_115_open: bool) -> dict:
    """What the front end may show. The server-side checks are the boundary;
    this only keeps the page from offering what would be refused (plan §6).

    The two 115 capabilities are deliberately independent: step A (the web
    cookie) is what saves a share to the default inbox, step B (the OpenAPI
    token) is what lists folders and queues a cloud download. Having one
    never implies the other.
    """
    admin = role == "admin"
    return {
        "library": True,
        "openlist": admin,
        "strm": admin,
        "global_settings": admin,
        "re0_unlock": admin or bool(allow_member_re0_unlock),
        "own_115_transfer": bool(has_115_cookie),
        "own_115_browse": bool(has_115_open),
        "own_115_cloud_download": bool(has_115_open),
    }
