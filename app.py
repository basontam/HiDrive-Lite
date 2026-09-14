"""HiDrive-Lite: a small, self-hosted media-resource control surface.

The public TgtoDrive repository is used as the source reference for the 115
share-receive flow.  This service deliberately exposes only the requested
features: RE0 OpenAPI, 115 one-click saving, and read-only OpenList/STRM
browsing.  It is intended to run behind Cloudflare Access and a dedicated
Cloudflare Tunnel.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import contextlib
import fcntl
import functools
import hashlib
import hmac
import json
import logging
import os
import platform
import posixpath
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import jwt
import requests
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # Production uses systemd EnvironmentFile.
    def load_dotenv(_path=None):
        return False

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, Response, abort, g, has_request_context, jsonify, redirect, render_template, request

load_dotenv(Path(__file__).resolve().parent / ".env")

import auth_service
import library_normalize
import library_search
import library_store
import library_tmdb
import re0_sync
import user_115

# T4: integrate the TMDB cache/budget tables into every schema this process
# creates. Registered here (not in library_store.py) so library_store.py's
# own unit tests stay decoupled from library_tmdb (they assert a bare
# create_schema() does NOT create tmdb_cache/tmdb_budget); app.py is the
# actual production entrypoint that wires the two modules together, so this
# is the "integration time" the EXTRA_SCHEMA_HOOKS docstring refers to.
library_store.EXTRA_SCHEMA_HOOKS.append(library_tmdb.ensure_tables)


APP_NAME = "HiDrive-Lite"
DEFAULT_HDHIVE_BASE = "https://re0.me"
# Keep the historical HDHIVE_* variable names and route/database identifiers
# for backwards compatibility, while making RE0 the canonical upstream.  A
# deployment may still override the endpoint explicitly for a private mirror.
HDHIVE_BASE = (os.getenv("HDHIVE_BASE_URL", DEFAULT_HDHIVE_BASE).strip().rstrip("/") or DEFAULT_HDHIVE_BASE)
HDHIVE_TOKEN_PATH = "/api/public/openapi/oauth/token"
HDHIVE_REFRESH_PATH = "/api/public/openapi/oauth/refresh"
HDHIVE_CHECKIN_PATH = os.getenv("HDHIVE_CHECKIN_PATH", "/api/open/checkin")
PUBLIC_ORIGIN = os.getenv("HIDRIVE_PUBLIC_ORIGIN", "http://127.0.0.1:12367").rstrip("/")
DEFAULT_STRM_ROOT = "./data/strm"
DEFAULT_OPENLIST_URL = "http://127.0.0.1:5244"
DEFAULT_OPENLIST_115PAN_PATH = "/115pan"
DEFAULT_OPENLIST_115STRM_PATH = "/115strm"
TOKEN_SKEW_SECONDS = 300
STATE_TTL_SECONDS = 600
CSRF_TTL_SECONDS = 3600
LOG = logging.getLogger(APP_NAME)


def utc_now() -> int:
    return int(time.time())


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = Path(os.getenv("HIDRIVE_DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "hidrive.db"
LIBRARY_DB_PATH = DATA_DIR / "media-library.db"
MASTER_KEY_FILE = Path(os.getenv("HIDRIVE_MASTER_KEY_FILE", "./secrets/master.key"))
# Multi-user plan §5.2: the one Google identity allowed to be the
# administrator. Checked server-side on top of Cloudflare's own policy --
# never inferred from a request body.
ADMIN_EMAIL = os.getenv("HIDRIVE_ADMIN_EMAIL", auth_service.ADMIN_EMAIL_DEFAULT).strip().casefold()
# Plan §5.4: the __Host- prefix binds the cookie to this exact origin and
# path, so no sibling host can set or read it.
SESSION_COOKIE_NAME = "__Host-hidrive_session"
STRM_ROOT = Path(os.getenv("STRM_ROOT", DEFAULT_STRM_ROOT)).resolve()
OPENLIST_URL = os.getenv("OPENLIST_URL", DEFAULT_OPENLIST_URL).rstrip("/")
OPENLIST_DB = Path(os.getenv("OPENLIST_DB", "./data/openlist.db"))
OPENLIST_115PAN_PATH = os.getenv("OPENLIST_115PAN_PATH", DEFAULT_OPENLIST_115PAN_PATH).strip() or DEFAULT_OPENLIST_115PAN_PATH
OPENLIST_115STRM_PATH = os.getenv("OPENLIST_115STRM_PATH", DEFAULT_OPENLIST_115STRM_PATH).strip() or DEFAULT_OPENLIST_115STRM_PATH
OPEN115_API_BASE = "https://proapi.115.com"
# 115's web-session QR login endpoints (see docs/115-reauth-adapter.md --
# confirmed by reading p115client's source in a throwaway staging venv,
# not guessed). Not part of the documented Open Platform API; same
# stability/compliance caveat as the existing my.115.com/webapi.115.com
# calls below.
QR_LOGIN_BASE = "https://qrcodeapi.115.com"
REAUTH_TTL_SECONDS = 300
REAUTH_COOLDOWN_WINDOW_SECONDS = 1800
REAUTH_COOLDOWN_THRESHOLD = 3
# T19 fix wave 1 item 4: REAUTH_COOLDOWN_THRESHOLD alone only counts
# failed/expired challenges. REAUTH_MAX_STARTS_PER_WINDOW caps total
# /reauth/start attempts per actor per window regardless of outcome -- this
# is what actually bounds an actor spamming start+cancel forever; see
# _reauth_cooldown_count()'s docstring (wave 2 N4) for why user cancels are
# deliberately excluded from REAUTH_COOLDOWN_THRESHOLD itself.
# T19 wave 3 item 5: raised from 5 to 8 -- "刷新二维码" (refresh) cancels
# the previous challenge and immediately starts a new one, so each refresh
# still spends one /reauth/start; a flat 5-start cap could lock out a user
# who refreshes a few times in one sitting even though nothing failed. A
# per-challenge "reached pending for >=10s before counting" duration
# heuristic was considered and rejected as needless complexity (new
# state/edge cases -- e.g. what a challenge that failed before ever
# reaching pending should count as -- for a cap whose whole job is just a
# blunt spam bound); simply raising the flat cap is the simpler, still-safe
# option, chosen here.
REAUTH_MAX_STARTS_PER_WINDOW = 8
# Terminal challenge rows older than this are deleted lazily on every
# /reauth/start (cheap DELETE, no background sweep needed).
REAUTH_PURGE_AGE_SECONDS = 86400
# T19 fix wave 2 N1: a challenge claimed into 'consuming' (see
# api_115_reauth_status) whose worker was killed or hit an uncaught
# exception before the terminal UPDATE would otherwise sit there forever.
# _reauth_expire_abandoned() also sweeps a 'consuming' row once it has been
# claimed for longer than this -- long enough for the poll/exchange's own
# _115_UPSTREAM_TIMEOUT-bounded upstream calls to have finished either way.
REAUTH_CLAIM_STALE_SECONDS = 60
# w6-reauth-longpoll: 115's get/status is a LONG-POLL endpoint -- it holds
# the connection until the QR status changes or ~30s elapse, rather than
# answering immediately. The old flat _115_UPSTREAM_TIMEOUT=8s read timeout
# abandoned every poll before 115 could ever answer, so a scan/confirm was
# never observed. _reauth_poll_upstream() instead uses a dedicated read
# timeout bounded by the request's own _115_REQUEST_DEADLINE_SECONDS budget
# (see its docstring), capped here at 15s so a single poll plus its
# _115_UPSTREAM_CONNECT_TIMEOUT=3s connect timeout (18s) stays comfortably
# under gunicorn's 30s worker timeout.
#
# w6-reauth-longpoll-fix: observing status 2 no longer runs the cookie
# exchange in the same request -- doing so on top of an ~18s poll hold
# could still blow gunicorn's 30s worker timeout. It instead writes the new
# non-terminal 'confirmed' state and returns immediately; the browser's very
# next status request claims the 'confirmed' row and runs the exchange with
# its own fresh deadline budget. See api_115_reauth_status's docstring for
# the full state machine.
REAUTH_STATUS_POLL_READ_SECONDS = 15
REAUTH_TERMINAL_STATES = {"authenticated", "expired", "cancelled", "failed"}
# Item 7: bound on the QR PNG this app proxies from 115 -- a misbehaving/
# compromised upstream must never make this proxy buffer or forward more.
QR_IMAGE_MAX_BYTES = 512 * 1024
AUTH_MODE = os.getenv("HIDRIVE_AUTH_MODE", "local").strip().lower()
ACCESS_TEAM_DOMAIN = os.getenv("ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")
ACCESS_AUDIENCE = os.getenv("ACCESS_AUDIENCE", "").strip()
JWKS_TTL_SECONDS = 900


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS secrets (
                name TEXT PRIMARY KEY,
                value BLOB NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                provider TEXT PRIMARY KEY,
                access_token BLOB NOT NULL,
                refresh_token BLOB,
                expires_at INTEGER,
                refresh_expires_at INTEGER,
                scope TEXT,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                used_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                actor TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                success INTEGER NOT NULL,
                code TEXT,
                message TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reauth_challenges (
                id_hash TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER,
                error_code TEXT,
                qr_uid_cipher BLOB,
                qr_time INTEGER,
                qr_sign_cipher BLOB,
                claimed_at INTEGER,
                claimed_from TEXT
            );
            CREATE TABLE IF NOT EXISTS cloud_download_task (
                info_hash TEXT PRIMARY KEY,
                link_public_id TEXT NOT NULL,
                media_id INTEGER,
                group_id INTEGER,
                media_title TEXT,
                link_label TEXT,
                wp_path_id TEXT NOT NULL,
                target_path TEXT,
                submitted_at INTEGER NOT NULL,
                submitted_by TEXT,
                last_status INTEGER,
                last_message TEXT,
                last_seen_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_cloud_download_task_submitted ON cloud_download_task(submitted_at);
            """
        )
        # T19 wave 3 item 2: CREATE TABLE IF NOT EXISTS above only builds
        # claimed_at/claimed_from into a brand-new table -- a database that
        # already has reauth_challenges from an earlier deploy (wave 1,
        # before these two columns existed) is left untouched by it. Add
        # the missing columns explicitly so init_db() stays repeatable/safe
        # to run against a live database at any point in its history.
        existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(reauth_challenges)")}
        if "claimed_at" not in existing_columns:
            db.execute("ALTER TABLE reauth_challenges ADD COLUMN claimed_at INTEGER")
        if "claimed_from" not in existing_columns:
            db.execute("ALTER TABLE reauth_challenges ADD COLUMN claimed_from TEXT")
        # Multi-user plan Phase 1: the auth tables and the two nullable
        # `user_id` columns. Additive and idempotent -- it creates empty
        # tables and moves no data. The administrator's existing credentials
        # are copied only by `--auth-migrate`, never by this startup hook
        # (plan §17).
        auth_service.ensure_schema(db)


def load_fernet() -> Fernet:
    raw = MASTER_KEY_FILE.read_bytes().strip()
    if len(raw) != 44:
        raise RuntimeError("HIDRIVE_MASTER_KEY_FILE must contain a Fernet key")
    try:
        return Fernet(raw)
    except Exception as exc:  # pragma: no cover - defensive startup error
        raise RuntimeError("invalid HiDrive-Lite master key") from exc


def secret_get(name: str) -> str | None:
    with connect_db() as db:
        row = db.execute("SELECT value FROM secrets WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    try:
        return load_fernet().decrypt(bytes(row["value"])).decode("utf-8")
    except (InvalidToken, ValueError, OSError) as exc:
        LOG.error("secret decrypt failed for %s: %s", name, type(exc).__name__)
        return None


def secret_set(name: str, value: str) -> None:
    encrypted = load_fernet().encrypt(value.encode("utf-8"))
    with connect_db() as db:
        db.execute(
            "INSERT INTO secrets(name, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (name, encrypted, utc_now()),
        )


def setting_get(name: str, default: str | None = None) -> str | None:
    with connect_db() as db:
        row = db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
    return str(row["value"]) if row else default


def setting_set(name: str, value: str) -> None:
    with connect_db() as db:
        db.execute(
            "INSERT INTO settings(name, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (name, value, utc_now()),
        )


def audit(action: str, status: str, detail: str = "", actor: str = "", *, db: sqlite3.Connection | None = None) -> None:
    """Record an audit row. N1 (wave 2): pass an already-open ``db`` (e.g.
    api_115_reauth_status's terminal UPDATE) to insert the audit row in the
    *same* transaction as a state change -- so a crash between the two can
    never leave one committed without the other."""
    safe_detail = detail[:500]
    if db is not None:
        db.execute(
            "INSERT INTO audit_log(action,status,detail,actor,created_at) VALUES(?,?,?,?,?)",
            (action, status, safe_detail, actor[:160], utc_now()),
        )
        return
    with connect_db() as db:
        db.execute(
            "INSERT INTO audit_log(action,status,detail,actor,created_at) VALUES(?,?,?,?,?)",
            (action, status, safe_detail, actor[:160], utc_now()),
        )


def actor_id() -> str:
    """Who this request is attributed to.

    Phase 4: a local session is identified by its user id, so a scan
    challenge, an audit row and a CSRF subject all belong to that user
    rather than to whatever Cloudflare called them. Without a session it is
    the Access principal exactly as before -- which is what keeps the
    administrator's existing rows readable across the compatibility window.
    """
    user = getattr(g, "current_user", None)
    if user is not None:
        return f"user:{user.id}"
    principal = getattr(g, "principal", None) or {}
    return str(principal.get("sub") or principal.get("email") or "local")


_jwks_cache: dict[str, object] = {"expires_at": 0, "keys": None}


def verify_access_jwt(token: str) -> dict:
    if not ACCESS_TEAM_DOMAIN or not ACCESS_AUDIENCE:
        raise PermissionError("Cloudflare Access is not configured")
    issuer = f"https://{ACCESS_TEAM_DOMAIN}"
    now = utc_now()
    if int(_jwks_cache.get("expires_at", 0)) <= now:
        try:
            response = requests.get(f"{issuer}/cdn-cgi/access/certs", timeout=8)
            response.raise_for_status()
            _jwks_cache["keys"] = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PermissionError("Cloudflare Access JWKS unavailable") from exc
        _jwks_cache["expires_at"] = now + JWKS_TTL_SECONDS
    keys = _jwks_cache.get("keys")
    if not isinstance(keys, dict) or not isinstance(keys.get("keys"), list):
        raise PermissionError("Cloudflare Access JWKS unavailable")
    try:
        header = jwt.get_unverified_header(token)
        key_data = next(
            (item for item in keys["keys"] if isinstance(item, dict) and item.get("kid") == header.get("kid")),
            None,
        )
        if not key_data:
            raise PermissionError("Cloudflare Access signing key not found")
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data))
        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=ACCESS_AUDIENCE,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except PermissionError:
        raise
    except Exception as exc:
        raise PermissionError("invalid Cloudflare Access JWT") from exc


# Plan §5.1's ladder. `access` is production today (Cloudflare guards
# everything); `hybrid` accepts either an Access assertion or a local
# session; `app` is the destination, where only /auth/google still goes
# through Access and the session guards the rest. `local`/`disabled` are the
# developer modes with no authentication at all.
SESSION_AUTH_MODES = frozenset({"hybrid", "app"})
# Modes where a principal that cleared Cloudflare Access *is* the
# administrator without any further check, because Cloudflare's own policy
# over the whole site is what admits them. `hybrid` is deliberately not here
# (R12): once a session is an alternative way in, the site is no longer
# wholly behind that policy, so the address is checked too.
PRINCIPAL_IS_ADMIN_MODES = frozenset({"local", "disabled", "access"})


def require_access() -> None:
    if AUTH_MODE in {"local", "disabled"}:
        g.principal = {"sub": "local", "email": "local"}
        return
    token = request.headers.get("Cf-Access-Jwt-Assertion", "")
    if not token:
        if AUTH_MODE in SESSION_AUTH_MODES:
            # No assertion is not an error here: a local session is the other
            # way in, and authorize_request() decides whether there is one.
            g.principal = None
            return
        abort(401, description="Cloudflare Access authentication required")
    try:
        # An assertion that was sent is always checked, in every mode: an
        # invalid one is an attempt to authenticate, not an absent attempt.
        g.principal = verify_access_jwt(token)
    except PermissionError:
        abort(401, description="invalid Cloudflare Access authentication")


def _load_auth_session() -> None:
    """Resolve the session cookie into ``g.auth_session`` / ``g.current_user``.

    Runs before ``check_csrf()`` because a token issued for a session is
    bound to it. A cookie for a revoked, expired or no-longer-active user
    resolves to nothing at all -- never to a partially trusted state.
    """
    g.auth_session = None
    g.current_user = None
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token:
        return
    now = utc_now()
    with connect_db() as db:
        session = auth_service.load_session(db, token, now=now)
        if session is None:
            return
        row = auth_service.find_user(db, session.user_id)
    if row is None or row["status"] != "active":
        return
    g.auth_session = session
    g.current_user = auth_service.CurrentUser(
        id=int(row["id"]), email=row["email_display"], role=row["role"], status=row["status"]
    )


def _admin_user_row() -> auth_service.CurrentUser:
    """The administrator as a ``CurrentUser``, creating the row on first use.

    Creating it here is not a migration: it is one row with no password and
    no credential, and ``ensure_admin_user`` is idempotent. It exists so the
    administrator's own id can be recorded as the approver of an account
    before ``--auth-migrate`` has ever been run.
    """
    if has_request_context():
        cached = getattr(g, "_admin_current_user", None)
        if cached is not None:
            return cached
    now = utc_now()
    with connect_db() as db:
        user_id = auth_service.ensure_admin_user(db, email=ADMIN_EMAIL, now=now)
    resolved = auth_service.CurrentUser(id=user_id, email=ADMIN_EMAIL, role="admin", status="active")
    if has_request_context():
        # Resolved once per request: this is read on every authorisation
        # check, every credential lookup and every audit row.
        g._admin_current_user = resolved
    return resolved


def current_user() -> "auth_service.CurrentUser | None":
    """Who is making this request, or None.

    A local session wins. Failing that, the compatibility window of plan
    §5.1's ``access`` mode applies: the whole site is still behind Cloudflare
    Access, whose policy admits the administrator alone, so a principal that
    cleared Access *is* the administrator -- exactly today's model, unchanged.
    ``local``/``disabled`` are the developer modes where authentication is
    off entirely and the principal is already synthetic.

    The exact-email check the plan asks for (§5.2) belongs to the identity
    bridge at ``/auth/google`` and to ``hybrid``/``app``, where a session --
    not Cloudflare -- is what grants access. Those modes never fall back to
    a principal here.
    """
    user = getattr(g, "current_user", None)
    if user is not None:
        return user
    principal = getattr(g, "principal", None)
    if not principal:
        return None
    if AUTH_MODE in PRINCIPAL_IS_ADMIN_MODES:
        return _admin_user_row()
    if AUTH_MODE == "hybrid" and _principal_is_admin(principal):
        # Transitional mode: an Access assertion still works, but only for
        # the one address, checked here as well as at the edge (R12).
        return _admin_user_row()
    return None


def _principal_is_admin(principal) -> bool:
    """Whether this Access principal is the administrator, by the same rule
    the identity bridge applies: an address that matches exactly, and that
    the IdP has not marked unverified."""
    email = str((principal or {}).get("email") or "").strip().casefold()
    if not email or email != ADMIN_EMAIL:
        return False
    return (principal or {}).get("email_verified") is not False


# ---------------------------------------------------------------------------
# Multi-user Phase 3: one table saying who may reach what.
#
# Every registered endpoint appears in exactly one set. A route added without
# an entry is refused as if it were an administrator's (and fails the test
# that pins this map) -- the decision has to be made, not defaulted into.
# ---------------------------------------------------------------------------

# Reachable with no session at all: the sign-in surface itself, the health
# probe, and the static files.
PUBLIC_ENDPOINTS = frozenset({
    "static", "healthz", "login_page",
    "api_auth_bootstrap", "api_auth_login", "api_auth_register", "api_auth_logout",
    "api_csrf",
    # The identity bridge is how the administrator gets a session in the
    # first place; Cloudflare Access guards the path, and the route checks
    # the address again itself.
    "auth_google",
})

# Any signed-in user: the shared library, its local links, and the RE0
# candidates already materialised here (plan §6).
MEMBER_ENDPOINTS = frozenset({
    "index", "api_me",
    "api_library_search", "api_library_suggest", "api_library_filters",
    "api_library_media", "api_library_resource", "api_library_recommendations",
    "api_library_reveal", "api_library_search_re0", "api_library_re0_media",
    "api_library_re0_file_preview", "api_library_re0_refresh",
    # Phase 4: each user scans for their own 115 web session and transfers
    # with it. A user who has not done so is refused by the transfer gate,
    # never served somebody else's cookie.
    "api_115_reauth_start", "api_115_reauth_qr", "api_115_reauth_status",
    "api_115_reauth_cancel", "api_115_save", "api_library_transfer",
    # Phase 6: each user's own folders, tasks and quota, resolved through
    # their own step-B token. A user without one is guided to authorise it
    # (R05) -- never shown the administrator's.
    "api_115_folders", "api_cloud_download_submit", "api_cloud_download_tasks",
    "api_cloud_download_status", "api_cloud_download_quota",
    "api_cloud_download_delete", "api_cloud_download_clear",
    # Phase 7: reachable by a member, but what they may do there is decided
    # inside -- reuse of an already materialised link always, a new unlock
    # only while `allow_member_re0_unlock` is on.
    "api_library_re0_unlock_and_action", "api_library_re0_follow_unlock", "api_hdhive_unlock",
    # Phase 5: each user's own step-B authorisation and its status. While the
    # application is unapproved these answer "blocked" and write nothing.
    "api_me_115_status", "api_me_115_open_start", "api_me_115_disconnect",
    "api_me_115_open_status", "api_me_115_open_cancel",
})

# Administrator only. Three groups, and the middle one is temporary:
#
#  * the operator surface -- OpenList, STRM, global settings and status,
#    TMDB, the link checker, RE0 sync, the RE0 OAuth dance, user approval;
#  * 115 transfer/browse/cloud-download and the RE0 unlock paths, which the
#    matrix in plan §6 gives members too -- but only once they have their own
#    115 credentials (Phase 4-6) and the member unlock policy (Phase 7).
#    Until then the honest answer to a member is "no", never "here is the
#    administrator's cookie";
#  * the legacy HDHive endpoints.
ADMIN_ENDPOINTS = frozenset({
    "api_openlist_list", "api_strm_list", "api_settings", "api_status",
    "api_tmdb_search", "api_library_tmdb_check", "api_library_tmdb_enrich_now",
    "api_library_tmdb_status", "api_library_linkcheck_status",
    "api_library_resource_recheck",
    "api_library_re0_status", "api_library_re0_run_small", "api_library_re0_discoveries",
    "oauth_start", "oauth_callback",
    "api_admin_users", "api_admin_user_approve", "api_admin_user_reject",
    "api_admin_user_disable", "api_admin_policy_re0",
    # Awaiting their own phase -- see the note above. Step A (the web cookie)
    # and the transfer it enables moved to MEMBER_ENDPOINTS in Phase 4;
    # folder browsing and cloud download wait for step B (Phase 5/6).

    "api_library_re0_follow_query",
    "api_hdhive_checkin", "api_hdhive_resources", "api_hdhive_search",
})


def _wants_html() -> bool:
    """Whether an anonymous caller should be sent to the login page rather
    than handed a JSON 401 -- a document navigation, not an API call."""
    if request.path.startswith("/api/"):
        return False
    return "text/html" in (request.headers.get("Accept") or "")


def authorize_request():
    """The single authorisation gate, applied to every request.

    Returns a response to send instead of the view, or None to continue.
    Role decorators on individual routes stay as a second, local statement of
    the same rule; this is the one that cannot be forgotten.
    """
    endpoint = request.endpoint or ""
    if endpoint in PUBLIC_ENDPOINTS:
        return None
    user = current_user()
    if user is None:
        if _wants_html():
            return redirect("/login")
        return json_error("请先登录", 401, "LOGIN_REQUIRED")
    if endpoint in MEMBER_ENDPOINTS:
        return None
    # Unlisted endpoints are treated as the administrator's: a new route is
    # closed until someone decides otherwise.
    if user.role != "admin":
        return json_error("没有权限", 403, "FORBIDDEN")
    return None


def require_login(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return json_error("请先登录", 401, "LOGIN_REQUIRED")
        return view(*args, **kwargs)
    return wrapper


def require_role(*roles: str):
    """Server-side role gate. The front end's capabilities only decide what
    to draw; this is the boundary (plan §6)."""
    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return json_error("请先登录", 401, "LOGIN_REQUIRED")
            if user.role not in roles:
                return json_error("没有权限", 403, "FORBIDDEN")
            return view(*args, **kwargs)
        return wrapper
    return decorator


def _re0_lease_take(store, lease_key: str, *, holder: str, now: int) -> bool:
    """Take the unlock lease for one thing. One statement, so two callers
    cannot both believe they hold it (plan §10.2)."""
    conn = store.connect()
    try:
        got = re0_sync.acquire_unlock_lease(conn, lease_key, holder=holder, now=now)
        conn.commit()
    finally:
        conn.close()
    return bool(got)


@contextlib.contextmanager
def _re0_lease_held(store, lease_key: str, *, holder: str):
    """Give the lease back on every path out of the block.

    F05: the follow-pack entry released it on exactly one branch, so an
    upstream failure left it held for its whole TTL and the user's immediate
    retry got 409 RE0_UNLOCK_IN_PROGRESS. Every consuming path now leaves
    through the same finally, whether it succeeded, was refused upstream,
    failed to parse, or raised.
    """
    try:
        yield
    finally:
        conn = store.connect()
        try:
            re0_sync.release_unlock_lease(conn, lease_key, holder=holder)
            conn.commit()
        finally:
            conn.close()


def _re0_resource_for_slug(store, slug: str):
    """The candidate row this slug belongs to, if this deployment has one.

    F05: the legacy endpoint only ever knew a slug, so it coordinated with
    nothing. Resolving the slug to its ``re0_resource`` row is what lets it
    take *the same* lease key the main entry uses for that resource.
    """
    conn = store.connect(readonly=True)
    try:
        salt = _re0_slug_salt(conn)
        row = conn.execute("SELECT * FROM re0_resource WHERE slug_hash=?",
                           (re0_sync.slug_hash(slug, salt),)).fetchone()
        return (dict(row) if row is not None else None), re0_sync.slug_hash(slug, salt)
    except sqlite3.DatabaseError:
        return None, ""
    finally:
        conn.close()


def _materialised_link_for_slug(slug: str) -> str | None:
    """The local link this slug already produced, if any.

    Looked up by the same salted hash the candidate rows are stored under,
    so the legacy entry can answer "you already have this" without
    decrypting anything or asking RE0 (R07).
    """
    try:
        store, error = _library_store_or_error(False)
    except Exception:  # noqa: BLE001 - a missing library is simply no link
        return None
    if error or store is None:
        return None
    conn = store.connect(readonly=True)
    try:
        salt = _re0_slug_salt(conn)
        row = conn.execute(
            "SELECT l.public_id AS public_id FROM re0_resource r "
            "JOIN re0_resource_link rl ON rl.re0_resource_id = r.id "
            "JOIN resource_link l ON l.id = rl.resource_link_id "
            "WHERE r.slug_hash=? ORDER BY rl.linked_at DESC LIMIT 1",
            (re0_sync.slug_hash(slug, salt),),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return row["public_id"] if row else None


def member_unlock_refused(what: str):
    """Plan §10.1 step 5, applied to every path that can spend points.

    Called only after the local reuse branches have had their turn, and
    always before a slug is decrypted or an upstream request is made -- so a
    refusal costs exactly zero RE0 calls and zero points. Returns a response
    to send, or None to carry on.
    """
    user = current_user()
    if user is None or user.role == "admin" or allow_member_re0_unlock():
        return None
    audit("re0.unlock", "refused", f"{what} reason=member_policy", actor_id())
    return json_error("管理员尚未开放普通用户解锁权限", 403, "MEMBER_RE0_UNLOCK_DISABLED")


def allow_member_re0_unlock() -> bool:
    """Plan §10: members may spend the administrator's RE0 points only while
    this is on. Default off."""
    return (setting_get("allow_member_re0_unlock", "0") or "0").strip().lower() in {"1", "true"}


def csrf_key() -> bytes:
    """The HMAC key for CSRF tokens: a purpose-separated subkey of the master
    key (2026-09-11 decision).

    It used to be ``load_fernet()._signing_key`` -- a private attribute of a
    third-party class, and the same key Fernet signs ciphertext with. One
    purpose per key means a token can never be replayed against another use,
    and nothing here depends on an API cryptography never promised.
    """
    return auth_service.derive_key("csrf/v1", MASTER_KEY_FILE.read_bytes())


def csrf_subject() -> str:
    """What a CSRF token is bound to.

    A local session binds to that session's own secret, so rotating the
    session (login, privilege change, approval) retires its tokens with it.
    Before there is a session -- today's Access-only mode, and the login page
    itself -- it binds to the Access principal exactly as before.
    """
    session = getattr(g, "auth_session", None)
    if session is not None:
        return "session:" + session.csrf_hash[:32]
    return actor_id()


def csrf_token() -> str:
    return auth_service.issue_csrf(csrf_key(), subject=csrf_subject(), now=utc_now())


def check_csrf() -> None:
    if AUTH_MODE in {"local", "disabled"}:
        return
    if request.headers.get("Origin", "").rstrip("/") != PUBLIC_ORIGIN:
        abort(403, description="origin check failed")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied:
        abort(403, description="CSRF token required")
    try:
        if not auth_service.verify_csrf(csrf_key(), supplied, subject=csrf_subject(), now=utc_now()):
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        abort(403, description="invalid CSRF token")


def json_error(message: str, status: int = 400, code: str = "BAD_REQUEST"):
    return jsonify({"success": False, "code": code, "message": message}), status


def request_json() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def config_value(name: str, env_name: str | None = None) -> str | None:
    stored = secret_get(name)
    if stored:
        return stored
    # In production, all credentials must come from encrypted SQLite.  The
    # environment fallback remains available only for local/disabled tests.
    if AUTH_MODE == "access":
        return None
    return os.getenv(env_name or name) or None


def hdhive_headers(access_token: str | None = None) -> dict[str, str]:
    api_key = config_value("hdhive_app_secret", "HDHIVE_APP_SECRET")
    headers = {"Accept": "application/json", "X-API-Key": api_key or ""}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def get_tokens() -> sqlite3.Row | None:
    with connect_db() as db:
        return db.execute("SELECT * FROM oauth_tokens WHERE provider='hdhive'").fetchone()


def save_tokens(data: dict, existing_refresh_token: str | None = None, existing_refresh_expires_at: int | None = None) -> None:
    access = str(data.get("access_token") or "")
    refresh = data.get("refresh_token") or existing_refresh_token
    if not access:
        raise RuntimeError("RE0 token response did not include access_token")
    fernet = load_fernet()
    now = utc_now()
    expires_at = now + int(data.get("expires_in") or 0) if data.get("expires_in") else None
    refresh_expires_at = now + int(data.get("refresh_expires_in") or 0) if data.get("refresh_expires_in") else existing_refresh_expires_at
    with connect_db() as db:
        db.execute(
            "INSERT INTO oauth_tokens(provider,access_token,refresh_token,expires_at,refresh_expires_at,scope,updated_at) "
            "VALUES('hdhive',?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
            "access_token=excluded.access_token, refresh_token=excluded.refresh_token, expires_at=excluded.expires_at, "
            "refresh_expires_at=excluded.refresh_expires_at, scope=excluded.scope, updated_at=excluded.updated_at",
            (
                fernet.encrypt(access.encode()),
                fernet.encrypt(str(refresh).encode()) if refresh else None,
                expires_at,
                refresh_expires_at,
                str(data.get("scope") or " ".join(data.get("scopes") or [])),
                now,
            ),
        )


def decrypt_token(row: sqlite3.Row, column: str) -> str | None:
    value = row[column]
    if value is None:
        return None
    try:
        return load_fernet().decrypt(bytes(value)).decode()
    except InvalidToken:
        return None


def refresh_hdhive_token(row: sqlite3.Row | None = None) -> str | None:
    lock_path = DATA_DIR / "hdhive-refresh.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            # Re-read inside the cross-process lock. Another worker/timer may
            # already have rotated the token while this caller was waiting.
            current = get_tokens()
            if current and current["expires_at"] is not None and int(current["expires_at"]) - TOKEN_SKEW_SECONDS > utc_now():
                return decrypt_token(current, "access_token")
            refresh = decrypt_token(current, "refresh_token") if current else None
            api_key = config_value("hdhive_app_secret", "HDHIVE_APP_SECRET")
            if not refresh or not api_key:
                return None
            try:
                response = requests.post(
                    HDHIVE_BASE + HDHIVE_REFRESH_PATH,
                    headers={"X-API-Key": api_key, "Accept": "application/json"},
                    json={"refresh_token": refresh},
                    timeout=15,
                )
                data = response.json() if response.content else {}
            except (requests.RequestException, ValueError):
                audit("hdhive.oauth.refresh", "failed", "upstream unavailable or invalid JSON", "system")
                return None
            if response.status_code >= 400 or not isinstance(data, dict) or not data.get("success", False):
                code = str(data.get("code") or response.status_code) if isinstance(data, dict) else str(response.status_code)
                audit("hdhive.oauth.refresh", "failed", code, "system")
                return None
            token_data = data.get("data") or {}
            if not isinstance(token_data, dict) or not token_data.get("access_token"):
                audit("hdhive.oauth.refresh", "failed", "missing access_token", "system")
                return None
            save_tokens(
                token_data,
                existing_refresh_token=refresh,
                existing_refresh_expires_at=int(current["refresh_expires_at"]) if current and current["refresh_expires_at"] else None,
            )
            audit("hdhive.oauth.refresh", "success", "access token rotated", "system")
            return str(token_data["access_token"])
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def valid_hdhive_access_token() -> str | None:
    row = get_tokens()
    if not row:
        return None
    access = decrypt_token(row, "access_token")
    if access and (row["expires_at"] is None or int(row["expires_at"]) - TOKEN_SKEW_SECONDS > utc_now()):
        return access
    return refresh_hdhive_token(row)


def hdhive_refresh_is_available(row: sqlite3.Row | None) -> bool:
    if not row or not decrypt_token(row, "refresh_token"):
        return False
    return not row["refresh_expires_at"] or int(row["refresh_expires_at"]) > utc_now()


def hdhive_request_once(method: str, path: str, headers: dict[str, str], params: dict | None, payload: dict | None) -> tuple[dict, int]:
    try:
        response = requests.request(method, HDHIVE_BASE + path, headers=headers, params=params, json=payload, timeout=20)
    except requests.RequestException as exc:
        return {"success": False, "code": "UPSTREAM_UNAVAILABLE", "message": f"RE0 请求失败：{type(exc).__name__}"}, 502
    try:
        data = response.json() if response.content else {}
    except (ValueError, TypeError):
        return {"success": False, "code": "UPSTREAM_INVALID_JSON", "message": "RE0 返回了无法解析的响应"}, 502
    if not isinstance(data, dict):
        return {"success": False, "code": "UPSTREAM_INVALID_JSON", "message": "RE0 返回格式异常"}, 502
    return data, response.status_code


def hdhive_request(method: str, path: str, *, params: dict | None = None, payload: dict | None = None, requires_user: bool = True) -> tuple[dict, int]:
    api_key = config_value("hdhive_app_secret", "HDHIVE_APP_SECRET")
    if not api_key:
        return {"success": False, "code": "HDHIVE_APP_SECRET_MISSING", "message": "请先配置 RE0 应用 Secret"}, 503
    token = valid_hdhive_access_token() if requires_user else None
    if requires_user and not token:
        row = get_tokens()
        if hdhive_refresh_is_available(row):
            return {"success": False, "code": "HDHIVE_REFRESH_UNAVAILABLE", "message": "RE0 Token 刷新暂时失败，请稍后重试；若持续失败请重新授权"}, 503
        return {"success": False, "code": "OPENAPI_REAUTH_REQUIRED", "message": "请先完成 RE0 OAuth 授权"}, 401
    data, status = hdhive_request_once(method, path, hdhive_headers(token), params, payload)
    if status in {401, 403} and requires_user and data.get("code") == "OPENAPI_REFRESH_REQUIRED":
        token = refresh_hdhive_token()
        if not token:
            row = get_tokens()
            if hdhive_refresh_is_available(row):
                return {"success": False, "code": "HDHIVE_REFRESH_UNAVAILABLE", "message": "RE0 Token 刷新暂时失败，请稍后重试；若持续失败请重新授权"}, 503
            return {"success": False, "code": "OPENAPI_REAUTH_REQUIRED", "message": "RE0 授权已失效，请重新完成 OAuth 授权"}, 401
        data, status = hdhive_request_once(method, path, hdhive_headers(token), params, payload)
    return data, status


def extract_share_link(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    for candidate in re.findall(r"https?://[^\s<>\"']+", value, re.I):
        candidate = candidate.rstrip(".,;，。；）)")
        if parse_115_link(candidate):
            return candidate
    return None


def parse_115_link(link: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(link.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"115.com", "www.115.com", "115cdn.com", "www.115cdn.com", "share.115.com", "anxia.com", "www.anxia.com"}:
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not parts:
        return None
    if parts[0].lower() == "s":
        parts = parts[1:]
    if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", parts[0]):
        return None
    query = parse_qs(parsed.query, keep_blank_values=False)
    if parsed.fragment:
        query.update(parse_qs(parsed.fragment, keep_blank_values=False))
    password = next((values[0] for name, values in query.items() if name.lower() in {"password", "pwd"} and values), "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", password):
        return None
    return parts[0], password


def normalized_path(value: str) -> str:
    return posixpath.normpath("/" + (value or "").lstrip("/"))


def path_under(root: str, value: str) -> bool:
    root_path = normalized_path(root)
    value_path = normalized_path(value)
    return value_path == root_path or value_path.startswith(root_path.rstrip("/") + "/")


def openlist_folder_items(path: str) -> tuple[list[dict], int, str]:
    rows: list[dict] = []
    for page in range(1, 11):
        data, status = list_openlist(path, page)
        if status >= 400 or not isinstance(data, dict) or data.get("code") not in (None, 200):
            message = str(data.get("message") or data.get("error") or "OpenList 目录读取失败") if isinstance(data, dict) else "OpenList 目录读取失败"
            return [], status if status >= 400 else 502, message
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        page_rows = payload.get("content") or payload.get("items") or []
        if not isinstance(page_rows, list):
            return [], 502, "OpenList 返回的目录格式异常"
        rows.extend(row for row in page_rows if isinstance(row, dict))
        if len(page_rows) < 200:
            break
    root = normalized_path(OPENLIST_115PAN_PATH)
    current = normalized_path(path)
    folders = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        is_dir = row.get("is_dir") is True or row.get("is_dir") in (1, "1", "true", "True")
        name = str(row.get("name") or "").strip()
        if not is_dir or not name or name in {".", ".."}:
            continue
        child = normalized_path(posixpath.join(current, name))
        if path_under(root, child):
            folders.append({"name": name, "path": child, "directory": True})
    return folders, 200, ""


def sync_openlist_credentials_from_source() -> bool:
    """Copy OpenList's current tokens without refreshing them in HiDrive-Lite."""
    if not OPENLIST_DB.exists():
        return False
    lock_path = DATA_DIR / "openlist-sync.lock"
    try:
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                with sqlite3.connect(f"file:{OPENLIST_DB}?mode=ro", uri=True, timeout=5) as source:
                    token_row = source.execute("SELECT value FROM x_setting_items WHERE key='token'").fetchone()
                    storage_row = source.execute(
                        "SELECT addition FROM x_storages WHERE mount_path=? AND driver='115 Open'",
                        (OPENLIST_115PAN_PATH,),
                    ).fetchone()
                if not token_row or not storage_row:
                    return False
                addition = json.loads(storage_row[0])
                access = str(addition.get("access_token") or "").strip()
                refresh = str(addition.get("refresh_token") or "").strip()
                openlist_token = str(token_row[0] or "").strip()
                if not openlist_token or not access or not refresh:
                    return False
                for name, value in (
                    ("openlist_token", openlist_token),
                    ("115_open_access_token", access),
                    ("115_open_refresh_token", refresh),
                ):
                    if secret_get(name) != value:
                        secret_set(name, value)
                root_cid = str(addition.get("root_folder_id") or "0").strip() or "0"
                if setting_get("115_open_root_cid", "0") != root_cid:
                    setting_set("115_open_root_cid", root_cid)
                return True
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except (OSError, sqlite3.Error, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        LOG.warning("OpenList credential sync skipped: %s", type(exc).__name__)
        return False


# T19 fix wave 3 item 1: the ONE request-scoped deadline for the whole
# transfer request -- target-path resolution, _115_transfer_gate/
# _115_verify_with_uid, share/snap (+ its one retry) and share/receive all
# share this single budget (see both routes' docstrings for the full
# arithmetic). Replaces wave 2's separate, resolution-only
# _115_REQUEST_DEADLINE_SECONDS=25s that save_115_link's own (now removed)
# _115_TRANSFER_DEADLINE_SECONDS budget started fresh after.
_115_REQUEST_DEADLINE_SECONDS = 24
# Internal sentinel: resolve_115_target_path()/open115_list() return this
# in place of a user-facing message when the request-scoped deadline ran
# out before they could finish. _resolve_115_target_pid() turns it into a
# fixed 504-style TARGET_RESOLVE_TIMEOUT response instead of the generic
# 115_TARGET_RESOLVE_FAILED one.
_TARGET_RESOLVE_TIMEOUT = object()


def open115_list(cid: str, allow_resync: bool = True, deadline: float | None = None) -> tuple[list[dict], int, object]:
    """List one OpenList/115-Open-Platform folder.

    N2 (wave 2): ``deadline`` (an absolute ``time.monotonic()`` value
    threaded down from a route's request-scoped budget via
    ``resolve_115_target_path``) bounds this call's own (connect, read)
    timeout at ``(min(_115_UPSTREAM_CONNECT_TIMEOUT, remaining budget),
    min(_115_UPSTREAM_TIMEOUT, remaining budget))`` (wave 4 item 3)
    instead of a fixed 20s -- the old fixed 20s-per-segment timeout let
    target resolution alone blow past gunicorn's 30s worker timeout before
    save_115_link's own budget even started. If the budget is already
    exhausted, the call is skipped entirely (returning
    ``_TARGET_RESOLVE_TIMEOUT``) rather than issued with a
    near-zero/negative timeout. ``deadline=None`` (no
    request-scoped budget, e.g. a direct call outside a transfer route)
    keeps the plain ``_115_UPSTREAM_TIMEOUT`` cap."""
    access_token = config_value("115_open_access_token", "115_open_access_token") or ""
    if not access_token:
        return [], 503, "尚未同步 OpenList 中的 115 开放平台令牌"
    remaining = _115_UPSTREAM_TIMEOUT if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        return [], 0, _TARGET_RESOLVE_TIMEOUT
    try:
        response = requests.get(
            OPEN115_API_BASE + "/open/ufile/files",
            headers={"Authorization": "Bearer " + access_token},
            params={"cid": cid or "0", "limit": 1150, "offset": 0, "show_dir": 1, "count_folders": 1},
            timeout=(min(_115_UPSTREAM_CONNECT_TIMEOUT, remaining), min(_115_UPSTREAM_TIMEOUT, remaining)),
        )
        data = response.json() if response.content else {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        return [], 502, "115 开放平台目录读取失败" if isinstance(exc, (ValueError, TypeError)) else f"115 开放平台请求失败：{type(exc).__name__}"
    auth_error = isinstance(data, dict) and (
        response.status_code in {401, 403}
        or str(data.get("errno") or "") in {"40140116", "40140125", "40140126", "40140127"}
    )
    if auth_error and allow_resync and sync_openlist_credentials_from_source():
        return open115_list(cid, allow_resync=False, deadline=deadline)
    if response.status_code >= 400 or not isinstance(data, dict) or not data.get("state", True):
        message = str(data.get("message") or data.get("error") or "115 开放平台令牌可能已失效") if isinstance(data, dict) else "115 开放平台返回格式异常"
        return [], response.status_code if response.status_code >= 400 else 502, message
    rows = data.get("data") or []
    if not isinstance(rows, list):
        return [], 502, "115 开放平台目录格式异常"
    return [row for row in rows if isinstance(row, dict)], 200, ""


def resolve_115_target_path(target_path: str, deadline: float | None = None) -> tuple[str | None, object]:
    """Resolve an OpenList path to the underlying 115 CID without exposing
    IDs.

    N2 (wave 2): ``deadline`` (an absolute ``time.monotonic()`` value) is
    the request-scoped budget shared with ``save_115_link`` -- checked
    before the segment loop and before each per-segment ``open115_list``
    call, so a slow/many-segment resolution aborts (returning
    ``_TARGET_RESOLVE_TIMEOUT``) instead of eating into, or blowing past,
    the budget ``save_115_link`` still needs for share/snap + share/receive.
    """
    root = normalized_path(OPENLIST_115PAN_PATH)
    selected = normalized_path(target_path)
    if not path_under(root, selected):
        return None, "目标目录必须位于 OpenList 的 115pan 存储下"
    if deadline is not None and time.monotonic() >= deadline:
        return None, _TARGET_RESOLVE_TIMEOUT
    sync_openlist_credentials_from_source()
    root_cid = str(setting_get("115_open_root_cid", "0") or "0").strip() or "0"
    relative = selected[len(root):].strip("/")
    if not relative:
        return root_cid, None
    cid = root_cid
    for component in relative.split("/"):
        if deadline is not None and time.monotonic() >= deadline:
            return None, _TARGET_RESOLVE_TIMEOUT
        rows, status, message = open115_list(cid, deadline=deadline)
        if message is _TARGET_RESOLVE_TIMEOUT:
            return None, _TARGET_RESOLVE_TIMEOUT
        if status >= 400:
            return None, message
        match = next(
            (row for row in rows if str(row.get("fn") or "").strip() == component and str(row.get("fc") or "") == "0"),
            None,
        )
        if not match:
            return None, f"115 目录不存在或已变化：{component}"
        cid = str(match.get("fid") or "").strip()
        if not cid:
            return None, f"115 目录缺少有效 ID：{component}"
    return cid, None


_TARGET_PID_INVALID_MESSAGE = "目标 PID 未通过校验，请重新选择目标目录"


def _resolve_115_target_pid(body: dict, deadline: float | None = None) -> tuple[str, object | None]:
    """Shared /api/115/save + /api/library/transfer target resolution.

    An explicit ``target_path`` (an OpenList 115pan path) takes priority
    and is resolved to the underlying 115 CID via ``resolve_115_target_path``;
    otherwise falls back to the request's own ``target_pid``, then the
    stored default ``115_target_pid`` setting. ``deadline`` (N2, wave 2) is
    the caller's request-scoped budget (an absolute ``time.monotonic()``
    value) -- threading it through here means target resolution's own
    upstream calls share the same budget ``save_115_link`` uses afterwards,
    instead of each starting a fresh one (see both routes' docstrings for
    the arithmetic). Returns ``(pid, error_response)`` -- ``error_response``
    is set when a given ``target_path`` fails to resolve: 504
    ``TARGET_RESOLVE_TIMEOUT`` if the budget ran out first, otherwise 503
    ``115_TARGET_RESOLVE_FAILED``; callers should return it as-is when not
    ``None``.

    T17 item 5 (docs §12.5): the new "current path is the target path" UI
    keeps its own resolved PID in memory and submits BOTH ``target_pid``
    and ``target_path`` together -- ``target_path`` is only ever display
    text, never trusted on its own, so when both are given the path is
    still the one resolved into a real 115 CID here, and the client's
    ``target_pid`` is merely compared against that server-resolved value;
    a mismatch is rejected with 400 ``TARGET_PID_INVALID`` (the client-
    displayed path must never become the security boundary) rather than
    silently preferring either side. A request with only ``target_path``
    (the manual-link flow) keeps the exact previous path->pid resolution.

    T17 fix wave 1 item 1 (CRITICAL): a ``target_pid`` given WITHOUT a
    ``target_path`` used to be trusted verbatim as the raw 115 ``cid`` --
    since a "path" shown in the UI is only ever display text, never itself
    checked against anything, a forged/stale ``target_pid`` alone could
    point the transfer at any folder in the whole 115 account, escaping
    the ``/115pan`` boundary entirely. It is now accepted ONLY when the
    server can independently prove it: either (a) a ``target_path`` is
    also given and resolves (above) to this exact pid, or (b) the bare pid
    equals the server's own stored default (``115_target_pid`` setting) or
    the configured ``/115pan`` root cid (``115_open_root_cid`` setting,
    same default ``"0"`` ``resolve_115_target_path`` uses) -- both are
    server-side values the client never supplied. Anything else is
    rejected with 400 ``TARGET_PID_INVALID`` (a single fixed message,
    ``_TARGET_PID_INVALID_MESSAGE``) before any upstream call is made,
    exactly like the target_path-mismatch case above. The displayed path
    is never itself the security boundary -- only a pid the server proved
    is.

    Follow-up (post-T17): the bare-pid branch (b) now mirrors
    ``resolve_115_target_path`` and calls
    ``sync_openlist_credentials_from_source()`` before reading
    ``115_open_root_cid`` -- without it, a root cid OpenList had already
    synced elsewhere could still look unsynced here, and worse, a setting
    that has genuinely NEVER been synced (no row at all) used to fall back
    to the same default ``"0"`` ``resolve_115_target_path`` itself falls
    back to, making that default indistinguishable from a real, synced
    ``"0"`` root cid -- exactly the value a forged/stale ``target_pid``
    could trivially guess. The root-cid exception is refused outright
    (only the stored-default check (a) above can still pass) when the
    setting is absent, rather than silently trusting that guessable
    default.

    Personal step-B tokens: members, and administrators submitting a bare
    CID from their personal folder picker, instead prove that folder with
    their own token. The legacy allowlist above still governs administrators
    without a personal token; explicit OpenList paths keep path resolution.
    """
    target_path = str(body.get("target_path") or "").strip()
    given_pid = str(body.get("target_pid") or "").strip()

    # R03: identity and capability first, before any upstream work. A member
    # never inherits the administrator's stored default folder, and refusing
    # their folder choice must not first drag the administrator's OpenList
    # into it.
    user = _acting_user()
    # The personal folder picker also serves administrators once their
    # step-B token is stored. Its bare CID must use the same user's token,
    # not the legacy OpenList root/default allowlist. Explicit OpenList
    # paths and the administrator's stored default retain their old route.
    if user is not None and (user.role != "admin" or (
            given_pid and not target_path and _has_own_open_token(user))):
        if not target_path and not given_pid:
            # Step A alone: no cid at all, which sends the share to this
            # member's own 115 default inbox (plan §4.2).
            return "", None
        _token, open_error = _require_open115(user)
        if open_error is not None:
            return "", open_error
        # Step B is done: the cid is proved against *their* own 115 by
        # listing it with their own token (R05). A path is display text and
        # is never trusted on its own.
        if target_path and not given_pid:
            return "", (jsonify({
                "success": False, "code": "TARGET_PID_INVALID",
                "message": _TARGET_PID_INVALID_MESSAGE,
            }), 400)
        if not validate_user_target_cid(user, given_pid, deadline=deadline):
            return "", (jsonify({
                "success": False, "code": "TARGET_PID_INVALID",
                "message": _TARGET_PID_INVALID_MESSAGE,
            }), 400)
        return given_pid, None

    default_pid = setting_get("115_target_pid", "") or ""
    pid = given_pid or default_pid
    if target_path:
        resolved_pid, resolve_error = resolve_115_target_path(target_path, deadline)
        if resolve_error is _TARGET_RESOLVE_TIMEOUT:
            return "", json_error("目标目录解析超时，请稍后重试", 504, "TARGET_RESOLVE_TIMEOUT")
        if resolve_error:
            return "", json_error(resolve_error, 503, "115_TARGET_RESOLVE_FAILED")
        resolved_pid = resolved_pid or ""
        if given_pid and given_pid != resolved_pid:
            return "", json_error(_TARGET_PID_INVALID_MESSAGE, 400, "TARGET_PID_INVALID")
        pid = resolved_pid
    elif given_pid:
        sync_openlist_credentials_from_source()
        root_cid_setting = setting_get("115_open_root_cid", None)
        if root_cid_setting is None:
            # Never synced at all -- refuse the root-cid exception rather
            # than trusting the same "0" a forged/stale pid could guess.
            if given_pid != default_pid:
                return "", json_error(_TARGET_PID_INVALID_MESSAGE, 400, "TARGET_PID_INVALID")
        else:
            root_cid = str(root_cid_setting).strip() or "0"
            if given_pid != default_pid and given_pid != root_cid:
                return "", json_error(_TARGET_PID_INVALID_MESSAGE, 400, "TARGET_PID_INVALID")
        pid = given_pid
    return pid, None


# T17 item 5: duplicate-submit guard for /api/library/transfer -- the same
# (resource_link_id, resolved target_pid) submitted twice within this
# window returns 409 TRANSFER_DUPLICATE instead of re-attempting the 115
# call, guarding against a double click or a retry storm. In-memory,
# per-worker (no cross-process coordination) -- consistent with
# HiDrive-Lite's other in-memory, per-worker caches (e.g.
# _RECOMMENDATION_CACHE) and with its personal-use scale.
_TRANSFER_DEDUPE_WINDOW_SECONDS = 10
_TRANSFER_DEDUPE_LOCK = threading.Lock()
_TRANSFER_DEDUPE_SEEN: dict[tuple[str, str, str], float] = {}


def _dedupe_user_key() -> str:
    """Which account an attempt belongs to, for the de-duplication window."""
    user = _acting_user()
    return f"user:{user.id}" if user else "deployment"


def _transfer_dedupe_check(public_id: str, pid: str, user_key: str = "") -> bool:
    """True (and records the attempt) the first time ``(public_id, pid)``
    is seen within the window; False for a duplicate seen again before the
    window elapses. Expired entries are pruned opportunistically on every
    call so the dict never grows past what's active within the window."""
    now = time.monotonic()
    # R19: two members saving the same share to their own default location
    # within the window are two transfers, not a double-click. The user is
    # part of the identity of the attempt; the upstream rate limits that
    # protect 115 are a separate, still-global concern.
    key = (user_key, public_id, pid)
    with _TRANSFER_DEDUPE_LOCK:
        for seen_key, seen_at in list(_TRANSFER_DEDUPE_SEEN.items()):
            if now - seen_at >= _TRANSFER_DEDUPE_WINDOW_SECONDS:
                del _TRANSFER_DEDUPE_SEEN[seen_key]
        if key in _TRANSFER_DEDUPE_SEEN:
            return False
        _TRANSFER_DEDUPE_SEEN[key] = now
        return True


def _transfer_dedupe_clear(public_id: str, pid: str, user_key: str = "") -> None:
    """T17 fix wave 1 item 3: undo ``_transfer_dedupe_check``'s record when
    the attempt it guarded never actually reached 115's share/receive
    endpoint (``TransferResult.receive_attempted`` is False) -- a failure
    that happened before receive was ever issued (bad link format, the
    cookie/rate/provider gate, share/snap, or the shared budget running
    out) is not a real transfer attempt, so an immediate retry with the
    same ``(resource_link_id, pid)`` must not be blocked by the 10s window
    meant for double-submits of a REAL, receive-issuing attempt."""
    with _TRANSFER_DEDUPE_LOCK:
        _TRANSFER_DEDUPE_SEEN.pop((user_key, public_id, pid), None)


GET_USER_AQ_URL = "https://my.115.com/?ct=ajax&ac=get_user_aq"
SHARE_SNAP_URL = "https://webapi.115.com/share/snap"
SHARE_RECEIVE_URL = "https://webapi.115.com/share/receive"
_115_UPSTREAM_TIMEOUT = 8  # brief §"repo facts": any upstream call inside a
# request must use a timeout <= 8s (gunicorn's 30s worker timeout headroom),
# EXCEPT the 115 QR reauth long-poll (_reauth_poll_upstream, capped at
# REAUTH_STATUS_POLL_READ_SECONDS instead) and its confirm->complete cookie
# exchange (_reauth_complete), which are long-poll/deadline-bound
# respectively and use their own, larger deadline-shrunk timeouts instead.
# T19 wave 4 item 3: every upstream call below uses a (connect, read) tuple
# timeout -- (min(_115_UPSTREAM_CONNECT_TIMEOUT, remaining),
# min(_115_UPSTREAM_TIMEOUT, remaining)) -- rather than a single scalar, so
# a stalled TCP handshake can't eat the whole remaining budget by itself.
_115_UPSTREAM_CONNECT_TIMEOUT = 3

# T19 fix wave 3 item 1: waves 1/2 gave save_115_link its own
# _115_TRANSFER_DEADLINE_SECONDS budget measured from its own start, on top
# of the route's separate _115_REQUEST_DEADLINE_SECONDS budget for target
# resolution -- two independent clocks that never actually bounded the
# request as a single whole. There is now exactly ONE request-scoped
# deadline (_115_REQUEST_DEADLINE_SECONDS, created at the very top of both
# /api/115/save and /api/library/transfer) threaded through resolution,
# _115_transfer_gate/_115_verify_with_uid and save_115_link's share/snap
# (+ its one retry) and share/receive. Every upstream call's own timeout is
# min(_115_UPSTREAM_TIMEOUT, deadline-now); share/snap's retry only runs
# with >=_115_SNAP_RETRY_MIN_REMAINING left; share/receive -- which must
# NEVER be the call the worker kills, since an ambiguous cutoff there risks
# a duplicate transfer -- is only issued with
# >=_115_RECEIVE_MIN_REMAINING left, otherwise a fixed 504
# TRANSFER_NOT_ATTEMPTED is returned and share/receive is never called at
# all (see both routes' docstrings for the full arithmetic).
_115_SNAP_RETRY_MIN_REMAINING = 12
_115_RECEIVE_MIN_REMAINING = 3

_115_STATE_LABEL = {
    "unconfigured": "未配置",
    "valid": "可用",
    "reauth_required": "不可用",
    "network_error": "不可用",
    "rate_limited": "不可用",
    "provider_error": "不可用",
    "unknown": "不可用",
}


def _capped_retry_after(raw: str | None, cap: int = 900) -> int:
    """Read a 429 response's Retry-After header defensively -- never trust
    an upstream value large/negative enough to stall a poller."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 60
    return max(0, min(value, cap))


def _115_classify_user_aq(response, checked_at: int) -> tuple[dict, str | None]:
    """Classify a ``get_user_aq`` response into the explainable state
    machine (docs/claude-115-cookie-persistence-and-reauth-20260906.md
    §4.1). Returns ``(result, uid)`` -- ``uid`` is for this process's own
    immediate use building a ``share/receive`` payload and must never be
    logged, audited or returned to a caller outside this module."""
    if response.status_code == 429:
        retry_after = _capped_retry_after(response.headers.get("Retry-After"))
        return {"state": "rate_limited", "error_code": "HTTP_429", "checked_at": checked_at, "retry_after": retry_after}, None
    if response.status_code in (401, 403, 405):
        return {"state": "reauth_required", "error_code": f"HTTP_{response.status_code}", "checked_at": checked_at, "retry_after": 0}, None
    if response.status_code >= 400:
        return {"state": "provider_error", "error_code": f"HTTP_{response.status_code}", "checked_at": checked_at, "retry_after": 0}, None
    try:
        data = response.json() if response.content else {}
    except ValueError:
        return {"state": "unknown", "error_code": "INVALID_JSON", "checked_at": checked_at, "retry_after": 0}, None
    if not isinstance(data, dict) or "state" not in data:
        return {"state": "unknown", "error_code": "INVALID_JSON", "checked_at": checked_at, "retry_after": 0}, None
    if not data.get("state"):
        return {"state": "reauth_required", "error_code": "AUTH_REJECTED", "checked_at": checked_at, "retry_after": 0}, None
    user_data = data.get("data")
    if not isinstance(user_data, dict):
        return {"state": "provider_error", "error_code": "INVALID_JSON", "checked_at": checked_at, "retry_after": 0}, None
    uid = user_data.get("uid")
    if not uid:
        return {"state": "reauth_required", "error_code": "AUTH_REJECTED", "checked_at": checked_at, "retry_after": 0}, None
    return {"state": "valid", "error_code": None, "checked_at": checked_at, "retry_after": 0}, str(uid)


def _115_verify_with_uid(cookie: str | None = None, deadline: float | None = None) -> tuple[dict, str | None]:
    """Do the actual ``get_user_aq`` round trip and classify it. Internal
    -- callers outside this module must go through ``verify_115_session``,
    which drops the uid.

    T19 wave 3 item 1: ``deadline`` (an absolute ``time.monotonic()``
    value), when given by ``_115_transfer_gate`` or (w6-reauth-longpoll-fix)
    ``_reauth_complete``, shrinks this call's own timeout to
    ``(min(_115_UPSTREAM_CONNECT_TIMEOUT, deadline-now),
    min(_115_UPSTREAM_TIMEOUT, deadline-now))`` -- the caller's own
    request-scoped deadline, not a fresh ``_115_UPSTREAM_TIMEOUT`` every
    time. ``deadline=None`` (every other caller: the settings-page poll, the
    periodic backoff check) keeps the plain ``_115_UPSTREAM_TIMEOUT`` cap,
    so the call is never shrunk and never classified ``budget_exhausted``
    for those callers.

    T19 wave 4 item 1: when ``deadline`` is given and the REQUEST's shared
    budget -- not 115 itself -- is what ran out (remaining already <= 0
    before the call could even be issued, or a timeout raised while this
    call's own read timeout was shrunk below the full
    ``_115_UPSTREAM_TIMEOUT``), the result is the distinct ``budget_
    exhausted`` state, never ``network_error``/``TIMEOUT``. This matters
    because ``_115_transfer_gate`` must never persist a budget_exhausted
    result as a real 115 outage (fail streak, fast-fail window, red
    settings state) -- a slow target resolution eating the shared budget
    is not evidence 115 itself is failing."""
    checked_at = utc_now()
    # A caller inside a request always passes the acting user's cookie; the
    # fallback is for background work, which belongs to no user (R02).
    cookies = (cookie if cookie is not None else _deployment_115("115_cookie", "ENV_115_COOKIES") or "").strip()
    if not cookies:
        return {"state": "unconfigured", "error_code": None, "checked_at": checked_at, "retry_after": 0}, None
    remaining = _115_UPSTREAM_TIMEOUT if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        return {"state": "budget_exhausted", "error_code": "BUDGET_EXHAUSTED", "checked_at": checked_at, "retry_after": 0}, None
    read_timeout = min(_115_UPSTREAM_TIMEOUT, remaining)
    headers = {"User-Agent": "Mozilla/5.0 HiDrive-Lite/1.0", "Cookie": cookies}
    try:
        response = requests.get(GET_USER_AQ_URL, headers=headers, timeout=(min(_115_UPSTREAM_CONNECT_TIMEOUT, remaining), read_timeout))
    except requests.Timeout:
        if deadline is not None and read_timeout < _115_UPSTREAM_TIMEOUT:
            return {"state": "budget_exhausted", "error_code": "BUDGET_EXHAUSTED", "checked_at": checked_at, "retry_after": 0}, None
        return {"state": "network_error", "error_code": "TIMEOUT", "checked_at": checked_at, "retry_after": 0}, None
    except requests.RequestException:
        return {"state": "network_error", "error_code": "CONNECTION", "checked_at": checked_at, "retry_after": 0}, None
    return _115_classify_user_aq(response, checked_at)


def verify_115_session(cookie: str | None = None, deadline: float | None = None) -> dict:
    """Classify the 115 web session without ever returning cookie
    material, the raw response, or the account uid (docs
    claude-115-cookie-persistence-and-reauth-20260906.md §4.1). Returns
    ``{"state", "error_code", "checked_at", "retry_after"}``.

    w6-reauth-longpoll-fix: ``deadline`` (an absolute ``time.monotonic()``
    value), when given by ``_reauth_complete``, shrinks the call's own
    timeout the same way ``_115_transfer_gate`` already does -- see
    ``_115_verify_with_uid``'s docstring. Every other caller keeps
    ``deadline=None`` (the plain ``_115_UPSTREAM_TIMEOUT`` cap)."""
    result, _uid = _115_verify_with_uid(cookie, deadline)
    return result


def _115_next_check_interval(fail_streak: int) -> int:
    """300s baseline; exponential backoff on repeated failures, capped at
    15 minutes, so neither the settings-page poll nor the transfer flow
    can turn into a request storm against an already-failing 115."""
    if fail_streak <= 0:
        return 300
    return min(300 * (2 ** min(fail_streak, 4)), 900)


def _acting_user() -> "auth_service.CurrentUser | None":
    """Who this call is for, or None outside a request.

    Background work (the enricher, the link checker, the RE0 timer) runs with
    no request at all; it must not resolve to a user, and must not raise
    trying.
    """
    if not has_request_context():
        return None
    return current_user()


def _admin_user_id() -> int | None:
    with connect_db() as db:
        row = db.execute("SELECT id FROM auth_user WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def _115_setting(name: str, user) -> str:
    """This user's copy of a 115 state key (plan §9.3).

    The administrator keeps the original keys so a rollback still finds its
    state; everyone else gets their own row.
    """
    if user is None:
        return name
    return user_115.setting_key(name, user.id, admin_user_id=_admin_user_id() if user.role != "admin" else user.id)


def user_115_cookie(user) -> str | None:
    """The web-session cookie *this* user authorised (step A).

    The administrator falls back to the legacy global value while the
    migration window is open -- it is the same credential, and
    ``--auth-migrate`` copies it into their own row. No other user has a
    fallback: a member with no cookie of their own simply has none (plan
    §9.1 forbids reaching for ``config_value('115_*')`` on their behalf).
    """
    if user is None:
        return _deployment_115("115_cookie", "ENV_115_COOKIES")
    with connect_db() as db:
        own = user_115.secret_get(db, user.id, user_115.COOKIE_SECRET, fernet=load_fernet())
        profile = user_115.profile(db, user.id)
    if own:
        return own
    if user.role != "admin":
        return None
    if profile is not None and profile["cookie_state"] == user_115.STATE_DISCONNECTED:
        # Asked to forget it. The legacy row stays for a rollback to read,
        # but it is no longer this account's credential (R18).
        return None
    return config_value("115_cookie", "ENV_115_COOKIES")


def _deployment_115(name: str, env_name: str) -> str | None:
    """The deployment's own 115 credential, for work that belongs to nobody.

    Background jobs -- the enricher, the link checker, the RE0 timer, the
    CLI -- run with no request and no user, and legitimately act as the
    deployment. An anonymous *request* is a different thing entirely and
    gets nothing: inside a request, a caller with no user has already been
    refused by authorize_request(), and must never be handed a credential.
    """
    if has_request_context():
        return None
    return config_value(name, env_name)


def user_115_open_token(user) -> str | None:
    """The OpenAPI access token *this* user authorised (step B).

    Same rule as the cookie: the administrator falls back to the legacy
    global value during the migration window because it is the same
    credential, and nobody else has a fallback at all. A member who has not
    completed step B has no token, and every caller must treat that as "not
    authorised" rather than reaching for somebody else's (plan §9.1).
    """
    if user is None:
        return _deployment_115("115_open_access_token", "115_open_access_token")
    with connect_db() as db:
        own = user_115.secret_get(db, user.id, user_115.OPEN_ACCESS_SECRET, fernet=load_fernet())
        profile = user_115.profile(db, user.id)
    if own:
        return own
    if user.role != "admin":
        return None
    if profile is not None and profile["open_state"] == user_115.STATE_DISCONNECTED:
        return None
    return config_value("115_open_access_token", "115_open_access_token")


def _require_open115(user):
    """``(token, error response)``. The error names step B rather than a
    missing setting, so the page can send the user to the right button."""
    token = user_115_open_token(user)
    if token:
        return token, None
    blocked = _open115_config().blocked_reason()
    message = ("115 开放平台授权尚未开通，目录与云下载暂不可用。"
               if blocked else "请先在设置页完成「目录与云下载」授权。")
    payload = {"success": False, "code": "OPEN115_NOT_AUTHORIZED", "message": message}
    if blocked:
        payload["blocked_reason"] = blocked
    return None, (jsonify(payload), 409)


def _has_own_open_token(user) -> bool:
    """Whether this account holds a step-B token of its own, as opposed to
    borrowing the deployment's during the migration window."""
    if user is None:
        return False
    with connect_db() as db:
        return user_115.secret_get(db, user.id, user_115.OPEN_ACCESS_SECRET,
                                   fernet=load_fernet()) is not None


def _open115_fence(user, lock_key: str, holder: str):
    """The condition a recovery result must still satisfy to be committed.

    G01.1: the version check at the start of a recovery says "this work is
    still needed"; it does not say "this result is still the current one" when
    the upstream call took longer than the lease. Evaluated inside the writing
    transaction, so nothing can change between the check and the commit:

    * the lease is still ours -- if it expired and somebody else took it, they
      are the one whose result counts now;
    * the user has not disconnected step B meanwhile, which is an explicit
      decision a late success must not undo.
    """
    def check(db) -> bool:
        if not auth_service.user_lock_held(db, lock_key, holder=holder):
            return False
        row = user_115.profile(db, user.id)
        if row is not None and row["open_state"] == user_115.STATE_DISCONNECTED:
            return False
        return True
    return check


def _refresh_own_open_token(user, seen_version: int | None = None, fence=None) -> bool:
    """Rotate a pair *this deployment's own 115 application* issued.

    False when it cannot be done -- including while that application is
    unapproved, which is where this stands today (B01). This is never the
    path for the administrator's migrated credential: that one is OpenList's
    to rotate (F02), and presenting its refresh token here would have two
    systems rotating one value.
    """
    config = _open115_config()
    if config.blocked_reason():
        return False
    adapter = user_115.Open115Adapter(config, session=requests.Session())
    token = user_115.refresh_open_token(connect_db, user.id, adapter, fernet=load_fernet(),
                                        now=utc_now(), seen_version=seen_version, fence=fence)
    return token is not None


def _open115_token_origin(user) -> str:
    """Who rotates this user's stored pair (F02).

    An administrator whose profile records nothing is the migration case: the
    pair in their slot came from OpenList, which is still rotating it. A
    member cannot be in that case -- nothing ever copies a credential into a
    member's slot.
    """
    with connect_db() as db:
        origin = user_115.open_token_origin(db, user.id)
    if origin:
        return origin
    return user_115.ORIGIN_OPENLIST_LEGACY if user.role == "admin" else user_115.ORIGIN_OWN_APP


def _legacy_open_pair() -> tuple[str | None, str | None]:
    """The deployment's legacy step-B pair, read as one statement.

    G01.1: reading the two halves with two calls could pick up an access token
    from before an OpenList rotation beside a refresh token from after it --
    a pair that is neither of the two real pairs. The environment fallback is
    only reachable in the dev auth modes and is read after, never mixed in.
    """
    fernet = load_fernet()
    with connect_db() as db:
        row = db.execute(
            "SELECT (SELECT value FROM secrets WHERE name='115_open_access_token') AS access_cipher, "
            "       (SELECT value FROM secrets WHERE name='115_open_refresh_token') AS refresh_cipher"
        ).fetchone()

    def plain(value):
        if value is None:
            return None
        try:
            return fernet.decrypt(bytes(value)).decode("utf-8")
        except (InvalidToken, ValueError, OSError):
            return None

    access, refresh = plain(row["access_cipher"]), plain(row["refresh_cipher"])
    if access is None and AUTH_MODE != "access":
        access = os.getenv("115_open_access_token") or None
        refresh = refresh or os.getenv("115_open_refresh_token") or None
    return access, refresh


def _sync_openlist_for_recovery(stale_access: str | None) -> bool:
    """Ask the owner to recover only when copying its saved pair is insufficient.

    Called under the administrator's recovery lease. OpenList alone rotates
    its refresh token; a forced listing bypasses its directory cache. The
    page size only limits the response, not the driver's upstream enumeration.
    The persisted cooldown bounds failed attempts across workers/restarts.
    """
    if not sync_openlist_credentials_from_source():
        return False
    access, refresh = _legacy_open_pair()
    if access and refresh and access != stale_access:
        return True
    token = config_value("openlist_token", "OPENLIST_TOKEN")
    if not token:
        return False
    now = utc_now()
    try:
        retry_after = int(setting_get("openlist_recovery_retry_after", "0"))
    except (ValueError, TypeError):
        retry_after = 0
    if now < retry_after:
        return False
    setting_set("openlist_recovery_retry_after", str(now + 60))
    try:
        requests.post(
            OPENLIST_URL + "/api/fs/list",
            headers={"Authorization": token, "Content-Type": "application/json"},
            json={"path": OPENLIST_115PAN_PATH, "password": "", "page": 1,
                  "per_page": 1, "refresh": True},
            timeout=(2, 4), allow_redirects=False,
        )
    except (requests.RequestException, ValueError, TypeError):
        pass
    # OpenList persists the rotated pair before finishing the listing. Even
    # if that listing times out/fails, adopt a changed pair from its database;
    # the one business retry (not the directory response) verifies usability.
    if not sync_openlist_credentials_from_source():
        return False
    access, refresh = _legacy_open_pair()
    return bool(access and refresh and access != stale_access)


def _adopt_legacy_open_token(user, *, expect_version: int, fence=None) -> bool:
    """Re-sync the administrator's credential from OpenList and put the fresh
    value back in their own slot.

    F02: after ``--auth-migrate --apply`` the read path prefers the user slot,
    so re-syncing the global setting alone fixed nothing -- the retry read the
    same expired copy. OpenList stays the only rotator; this just follows it.

    G01.1: the write is all-or-nothing and fenced. A pair missing its refresh
    half is not a credential, so it is not written at all.
    """
    if user.role != "admin":
        return False
    stale_access, _version = _open115_credential(user)
    if not _sync_openlist_for_recovery(stale_access):
        return False
    access, refresh = _legacy_open_pair()
    if not access or not refresh:
        return False
    with connect_db() as db:
        # Take the write lock before store_open_pair reads version/fence;
        # sqlite's context manager alone does not start a transaction on SELECT.
        db.execute("BEGIN IMMEDIATE")
        return user_115.store_open_pair(
            db, user.id, access, refresh, fernet=load_fernet(), now=utc_now(),
            origin=user_115.ORIGIN_OPENLIST_LEGACY, expect_version=expect_version, fence=fence)


_OPEN115_RECOVERY_WAIT_SECONDS = 8.0
_OPEN115_RECOVERY_POLL_SECONDS = 0.25


def _open115_credential(user) -> tuple[str | None, int]:
    """This caller's step-B access token and the generation it belongs to.

    The version travels with the token so that a 401 can be matched against
    the credential that produced it (F03), and both come from **one** read
    (G01.2): a token from before another worker's rotation beside the version
    from after it would make the recovery think the new version had not been
    tried, and rotate again.

    Every 115 caller -- cloud download, the folder client, target-cid proof --
    goes through here, so they all share that guarantee.
    """
    if user is None:
        return _deployment_115("115_open_access_token", "115_open_access_token"), 0
    with connect_db() as db:
        snapshot = user_115.open_snapshot(db, user.id, fernet=load_fernet())
    if snapshot.access_token:
        return snapshot.access_token, snapshot.version
    if user.role != "admin":
        return None, snapshot.version
    if snapshot.open_state == user_115.STATE_DISCONNECTED:
        return None, snapshot.version
    # The migration window: the administrator's own slot is empty, so the
    # legacy global value is still theirs. Version 0 is correct -- there is no
    # per-user generation yet.
    return config_value("115_open_access_token", "115_open_access_token"), snapshot.version


def _open115_recover(user, seen_version: int) -> bool:
    """Restore this caller's step-B credential once, coordinated across workers.

    The deployment runs several gunicorn workers, so the coordination is a
    row in ``user_lock`` rather than a lock in one process's memory (F03):
    overlapping requests that all noticed the same expiry produce exactly one
    upstream rotation. Whoever holds the lease re-reads the stored version
    first -- if it has already moved past what the caller used, the work is
    done and this returns True without touching 115.
    """
    if user is None:
        # No user at all: the deployment acting on its own behalf (timers,
        # the enricher). Its credential is the legacy global one.
        return bool(sync_openlist_credentials_from_source())
    lock_key = f"115_open_refresh:{user.id}"
    # G01.1: one identity per recovery *operation*, not per process or thread.
    # A thread that recovers twice must not be able to release -- or commit
    # under -- the lease its own earlier attempt took.
    holder = f"{os.getpid()}:{threading.get_ident()}:{secrets.token_hex(8)}"
    deadline = time.monotonic() + _OPEN115_RECOVERY_WAIT_SECONDS
    while True:
        with connect_db() as db:
            taken = auth_service.acquire_user_lock(db, lock_key, holder=holder, now=utc_now())
        if taken:
            break
        with connect_db() as db:
            if user_115.secret_version(db, user.id, user_115.OPEN_ACCESS_SECRET) > seen_version:
                # Another worker finished while we waited. Reuse its result.
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_OPEN115_RECOVERY_POLL_SECONDS)
    fence = _open115_fence(user, lock_key, holder)
    try:
        with connect_db() as db:
            snapshot = user_115.open_snapshot(db, user.id, fernet=load_fernet())
        if snapshot.version > seen_version:
            return True
        if snapshot.open_state == user_115.STATE_DISCONNECTED:
            # They disconnected step B. Recovering it would undo that.
            return False
        if snapshot.access_token is None:
            # Nothing of their own yet: only the administrator has a legacy
            # credential to fall back on, and a member was already refused.
            # Adopt the recovered pair into the admin slot as well, so other
            # workers observing this expiry can reuse the new generation.
            return bool(user.role == "admin" and _adopt_legacy_open_token(
                user, expect_version=snapshot.version, fence=fence))
        origin = snapshot.origin or (user_115.ORIGIN_OPENLIST_LEGACY if user.role == "admin"
                                     else user_115.ORIGIN_OWN_APP)
        if origin == user_115.ORIGIN_OPENLIST_LEGACY:
            return _adopt_legacy_open_token(user, expect_version=snapshot.version, fence=fence)
        return _refresh_own_open_token(user, seen_version=snapshot.version, fence=fence)
    finally:
        with connect_db() as db:
            auth_service.release_user_lock(db, lock_key, holder=holder)


def user_115_folders(user, cid: str, *, deadline: float | None = None,
                     allow_recovery: bool = True) -> tuple[list[dict], int, str]:
    """One folder of *this user's own* 115, through their own step-B token.

    Phase 6 (R05): a member browsing their 115 does not go through
    OpenList -- that is the administrator's operator tooling and holds the
    administrator's credential. The token scopes the call, so a cid this
    token cannot list is simply not theirs.
    """
    # G01.2: one snapshot, shared with every other 115 caller.
    token, token_version = _open115_credential(user)
    if not token:
        _unused, error = _require_open115(user)
        return [], 409, "尚未完成「目录与云下载」授权"
    remaining = _115_UPSTREAM_TIMEOUT if deadline is None else max(0.0, deadline - time.monotonic())
    if remaining <= 0:
        return [], 504, "目录读取超时"
    try:
        response = requests.get(
            OPEN115_API_BASE + "/open/ufile/files",
            headers={"Authorization": "Bearer " + token},
            params={"cid": str(cid or "0"), "limit": 200, "offset": 0, "show_dir": 1, "count_folders": 1},
            timeout=(min(_115_UPSTREAM_CONNECT_TIMEOUT, remaining), min(_115_UPSTREAM_TIMEOUT, remaining)),
        )
        payload = response.json() if response.content else {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        return [], 502, f"115 目录读取失败：{type(exc).__name__}"
    auth_error = response.status_code in {401, 403} or (
        isinstance(payload, dict)
        and str(payload.get("errno") or payload.get("code") or "") in _CLOUD_AUTH_ERRNOS
    )
    if auth_error:
        # F03: the folder picker is often a user's first action after their
        # token expired, so it recovers through the same path the
        # cloud-download client uses -- including HTTP 200 business errors.
        # Recover once, then the answer stands.
        if allow_recovery and _open115_recover(user, token_version):
            return user_115_folders(user, cid, deadline=deadline, allow_recovery=False)
        return [], 401, "「目录与云下载」授权已过期，请重新授权"
    if response.status_code >= 400 or not isinstance(payload, dict) or not payload.get("state", True):
        return [], 502, _cloud_sanitize_message(payload.get("message") if isinstance(payload, dict) else "") or "115 目录读取失败"
    items = []
    for entry in (payload.get("data") or []):
        if not isinstance(entry, dict):
            continue
        # Folders only, and only the two fields the picker needs: no size, no
        # path, nothing that describes the user's own files beyond a name.
        if str(entry.get("fc") or entry.get("file_category") or "") not in {"0", ""}:
            continue
        file_id = str(entry.get("fid") or entry.get("file_id") or entry.get("cid") or "")
        name = str(entry.get("fn") or entry.get("file_name") or entry.get("n") or "")
        if file_id and name:
            items.append({"cid": file_id, "name": name})
    return items, 200, ""


def validate_user_target_cid(user, cid: str, *, deadline: float | None = None) -> bool:
    """Whether ``cid`` is a folder in *this user's* own 115.

    Proved by listing it with their token: a cid belonging to somebody else's
    account cannot be listed with it. A client-supplied cid is a candidate
    until this says otherwise -- it is never itself an authorisation
    (plan §9.2).
    """
    if not str(cid or "").strip():
        return False
    _items, status, _message = user_115_folders(user, cid, deadline=deadline)
    return status == 200


def _cloud_cache_key(user, *parts) -> tuple:
    """A cache key nobody else can hit. The whole point of Phase 6: two users
    ask 115 with different tokens, so their answers must not share a slot
    (plan §17's "do not filter the display and leave the query shared")."""
    return (("user", user.id if user else 0), *parts)


def _cloud_cache_forget(user) -> None:
    """Drop this user's own cached quota and task pages.

    Flushing the whole dictionary would make one user's delete or clear cost
    every other user a fresh round of 115 requests -- and the cache is keyed
    per user precisely so that cannot happen (F04).
    """
    prefix = ("user", user.id if user else 0)
    for key in [k for k in _CLOUD_CACHE if isinstance(k, tuple) and k and k[0] == prefix]:
        _CLOUD_CACHE.pop(key, None)


def _persist_115_verification(result: dict, user=None) -> None:
    """Persist only sanitised state metadata (settings table) -- never the
    cookie, raw response or uid. Keeps writing legacy ``115_cookie_valid``
    for compatibility."""
    state = result["state"]
    checked_at = result["checked_at"]
    user = user if user is not None else _acting_user()
    key = lambda name: _115_setting(name, user)  # noqa: E731 - one short local alias
    setting_set(key("115_cookie_state"), state)
    setting_set(key("115_cookie_checked_at"), str(checked_at))
    setting_set(key("115_cookie_error_code"), result.get("error_code") or "")
    setting_set(key("115_cookie_valid"), "1" if state == "valid" else "0")
    if state == "valid":
        setting_set(key("115_cookie_last_success_at"), str(checked_at))
        setting_set(key("115_cookie_fail_streak"), "0")
    elif state != "unconfigured":
        setting_set(key("115_cookie_error_at"), str(checked_at))
        streak = int(setting_get(key("115_cookie_fail_streak"), "0") or "0") + 1
        setting_set(key("115_cookie_fail_streak"), str(streak))
    if user is not None:
        with connect_db() as db:
            user_115.remember_cookie_state(
                db, user.id,
                state=user_115.STATE_CONNECTED if state == "valid" else (
                    user_115.STATE_UNCONFIGURED if state == "unconfigured" else user_115.STATE_NEEDS_REAUTH),
                error_code=result.get("error_code"), now=checked_at or utc_now(),
            )


def remember_115_cookie_check(cookie: str | None = None, user=None) -> tuple[bool, str]:
    """Used by /api/settings right after a new cookie value is saved: an
    immediate, uncached check (bypassing the interval/backoff, which only
    throttle the periodic /api/status poll and the transfer-flow gate)."""
    user = user if user is not None else _acting_user()
    result = verify_115_session(cookie if cookie is not None else user_115_cookie(user))
    _persist_115_verification(result, user)
    return result["state"] == "valid", _115_STATE_LABEL.get(result["state"], "不可用")


def _115_maybe_refresh(user=None) -> None:
    user = user if user is not None else _acting_user()
    checked_at = int(setting_get(_115_setting("115_cookie_checked_at", user), "0") or "0")
    fail_streak = int(setting_get(_115_setting("115_cookie_fail_streak", user), "0") or "0")
    interval = _115_next_check_interval(fail_streak)
    if not checked_at or checked_at + interval <= utc_now():
        _persist_115_verification(verify_115_session(user_115_cookie(user)), user)


def cookie_status(verify: bool = False, user=None) -> dict[str, object]:
    user = user if user is not None else _acting_user()
    configured = bool(user_115_cookie(user))
    if not configured:
        return {"configured": False, "valid": None, "state": "unconfigured", "checked_at": None, "last_success_at": None, "error_code": None, "retry_after": None}
    if verify:
        _115_maybe_refresh(user)
    checked_at_raw = setting_get(_115_setting("115_cookie_checked_at", user), "") or ""
    try:
        checked_at = int(checked_at_raw)
    except ValueError:
        checked_at = 0
    last_success_raw = setting_get(_115_setting("115_cookie_last_success_at", user), "") or ""
    try:
        last_success_at = int(last_success_raw) or None
    except ValueError:
        last_success_at = None
    valid_raw = setting_get(_115_setting("115_cookie_valid", user))
    valid = None if valid_raw not in {"0", "1"} else valid_raw == "1"
    state = setting_get(_115_setting("115_cookie_state", user), "") or ("valid" if valid else "unknown" if valid is None else "reauth_required")
    # Item 8: how long until the next scheduled check will retry, so the UI
    # can show a countdown instead of a bare "rate limited" -- same backoff
    # formula _115_transfer_gate() already uses for its own cached 429s.
    retry_after = None
    if state == "rate_limited" and checked_at:
        fail_streak = int(setting_get(_115_setting("115_cookie_fail_streak", user), "0") or "0")
        retry_after = max(0, checked_at + _115_next_check_interval(fail_streak) - utc_now())
    return {
        "configured": True,
        "valid": valid,
        "state": state,
        "checked_at": checked_at or None,
        "last_success_at": last_success_at,
        "error_code": setting_get(_115_setting("115_cookie_error_code", user)) or None,
        "retry_after": retry_after,
    }


@dataclasses.dataclass
class TransferResult:
    success: bool
    message: str
    status: int
    code: str | None = None
    retry_after: int | None = None
    # T17 fix wave 1 item 3: True only once share/receive was actually
    # issued (whether it then succeeded, came back ambiguous, or reported
    # an explicit failure) -- every earlier failure (bad link format, the
    # cookie/rate/provider gate, share/snap, or the budget running out)
    # leaves this False. api_library_transfer uses it to decide whether a
    # rejection should keep occupying its 10s dedupe window.
    receive_attempted: bool = False


# docs/claude-115-cookie-persistence-and-reauth-20260906.md §6.2: explicit,
# distinct failure codes so the frontend never has to guess from a generic
# 502 whether a retry, a wait, or a QR re-auth is the right next step.
# "unconfigured" is folded into the same REAUTH_REQUIRED code/message as
# "reauth_required" -- both are resolved by the exact same QR flow, and the
# settings page's "重新授权 115" button covers first-time setup too.
_115_TRANSFER_ERROR_MAP: dict[str, tuple[int, str, str]] = {
    "unconfigured": (409, "115_REAUTH_REQUIRED", "115 需要重新授权，请到设置页扫码"),
    "reauth_required": (409, "115_REAUTH_REQUIRED", "115 需要重新授权，请到设置页扫码"),
    "network_error": (503, "115_TEMPORARILY_UNAVAILABLE", "115 暂时不可用，请稍后重试"),
    "rate_limited": (429, "115_RATE_LIMITED", "115 请求过于频繁，请稍后重试"),
    "provider_error": (502, "115_PROVIDER_ERROR", "115 返回异常，请稍后重试"),
    "unknown": (502, "115_PROVIDER_ERROR", "115 返回异常，请稍后重试"),
}


def _115_transfer_error(state: str, retry_after: int | None) -> TransferResult:
    status, code, message = _115_TRANSFER_ERROR_MAP.get(state, (502, "115_PROVIDER_ERROR", "115 返回异常，请稍后重试"))
    return TransferResult(False, message, status, code, retry_after if code == "115_RATE_LIMITED" else None)


def _115_transfer_gate(deadline: float) -> tuple[TransferResult | None, str | None]:
    """Fast-fail the transfer flow from cached state when a very recent
    check already showed the session is broken/rate-limited -- avoids
    hammering an already-failing 115 endpoint on every transfer attempt.
    Returns ``(error, None)`` to short-circuit the caller, or ``(None,
    uid)`` with a freshly-verified uid ready for ``share/receive``.

    T19 wave 3 item 1: ``deadline`` is the request's single shared budget
    (an absolute ``time.monotonic()`` value) -- threaded into
    ``_115_verify_with_uid`` so the one ``get_user_aq`` call the happy path
    always makes shares it too, instead of a fresh ``_115_UPSTREAM_TIMEOUT``
    of its own. The cached fast-fail path below makes no network call, so
    it needs no deadline.

    T19 wave 4 item 1: a ``budget_exhausted`` result (the shared REQUEST
    budget ran out, not 115 itself) is never passed to
    ``_persist_115_verification`` -- persisting it would misrecord the
    app's own timing pressure as a real 115 outage (network_error/TIMEOUT,
    fail streak, fast-fail window, red settings state). It short-circuits
    with the same fixed 504 ``TRANSFER_NOT_ATTEMPTED`` save_115_link uses
    when share/receive itself can't be attempted, and leaves cookie state
    completely unchanged."""
    # Phase 4: whose transfer this is. There is no fallback to anybody
    # else's cookie -- a user without one is refused here.
    user = _acting_user()
    # Resolved once and used for every call below -- the verification, the
    # share preview and the receive all belong to one account (R02).
    cookie = user_115_cookie(user)
    if not cookie:
        return _115_transfer_error("unconfigured", None), None
    checked_at = int(setting_get(_115_setting("115_cookie_checked_at", user), "0") or "0")
    fail_streak = int(setting_get(_115_setting("115_cookie_fail_streak", user), "0") or "0")
    cached_state = setting_get(_115_setting("115_cookie_state", user), "") or ""
    interval = _115_next_check_interval(fail_streak)
    still_fresh = bool(checked_at) and checked_at + interval > utc_now()
    if still_fresh and cached_state and cached_state != "valid":
        cached_retry = max(0, checked_at + interval - utc_now()) if cached_state == "rate_limited" else None
        return _115_transfer_error(cached_state, cached_retry), None
    # R02: verify the cookie this gate just resolved. Reading the global one
    # here would check the administrator's session and hand back their UID,
    # which the receive call would then use with a member's cookie.
    result, uid = _115_verify_with_uid(cookie, deadline=deadline)
    if result["state"] == "budget_exhausted":
        return TransferResult(False, "转存耗时过长，请稍后重试", 504, "TRANSFER_NOT_ATTEMPTED"), None
    _persist_115_verification(result, user)
    if result["state"] != "valid":
        return _115_transfer_error(result["state"], result.get("retry_after") or None), None
    return None, uid


def save_115_link(link: str, pid: str, deadline: float) -> TransferResult:
    """T19 wave 3 item 1: ``deadline`` (an absolute ``time.monotonic()``
    value) is the caller's single request-scoped budget -- the same one
    already spent on target-path resolution before this is called (see
    both routes' docstrings for the full arithmetic). Every upstream call
    here uses a (connect, read) tuple -- (min(_115_UPSTREAM_CONNECT_
    TIMEOUT, deadline-now), min(_115_UPSTREAM_TIMEOUT, deadline-now))
    (wave 4 item 3) -- as its own timeout: the session check
    (``_115_transfer_gate``), share/snap's one optional read-only retry
    (skipped once fewer than ``_115_SNAP_RETRY_MIN_REMAINING`` seconds
    remain) and share/receive, which is only ever issued with at least
    ``_115_RECEIVE_MIN_REMAINING`` seconds left -- otherwise a fixed 504
    ``TRANSFER_NOT_ATTEMPTED`` is returned and share/receive is never
    called, since an ambiguous cutoff there risks a duplicate transfer.
    share/snap never being actually attempted at all (wave 4 item 4, e.g.
    the session check alone exhausted the budget) also returns this same
    fixed 504, rather than falling through to a generic provider-error
    response."""
    parsed = parse_115_link(link)
    if not parsed:
        return TransferResult(False, "115 分享链接格式不正确", 400, "LINK_FORMAT_INVALID")
    error, uid = _115_transfer_gate(deadline)
    if error is not None:
        return error
    share_code, receive_code = parsed
    # Whoever is asking, with their own step-A cookie: the gate above already
    # refused a user who has none, and nobody is ever served another's.
    cookies = user_115_cookie(_acting_user()) or ""
    headers = {
        "User-Agent": "Mozilla/5.0 HiDrive-Lite/1.0",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookies,
    }
    snap_params = {"share_code": share_code, "offset": 0, "limit": 1000, "receive_code": receive_code, "cid": ""}
    snap: dict | None = None
    snap_exc: Exception | None = None
    snap_attempted = False
    for attempt in range(2):  # share/snap is read-only: one retry allowed
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (attempt > 0 and remaining < _115_SNAP_RETRY_MIN_REMAINING):
            break  # budget already spent, or not enough left for the retry
        snap_attempted = True
        try:
            snap_resp = requests.get(SHARE_SNAP_URL, headers=headers, params=snap_params, timeout=(min(_115_UPSTREAM_CONNECT_TIMEOUT, remaining), min(_115_UPSTREAM_TIMEOUT, remaining)))
            snap = snap_resp.json() if snap_resp.content else {}
            snap_exc = None
            break
        except (requests.RequestException, ValueError, TypeError) as exc:
            snap_exc = exc
            continue
    if not snap_attempted:
        # T19 wave 4 item 4: the budget was already gone before share/snap
        # could even be issued once (e.g. _115_transfer_gate's own verify
        # call consumed what little remained) -- both snap and snap_exc are
        # still None here, which used to fall through to the generic 502
        # 115_PROVIDER_ERROR below as if 115 had returned a bad response.
        return TransferResult(False, "转存耗时过长，请稍后重试", 504, "TRANSFER_NOT_ATTEMPTED")
    if snap_exc is not None:
        if isinstance(snap_exc, (ValueError, TypeError)):
            return TransferResult(False, "115 分享读取失败", 502, "115_PROVIDER_ERROR")
        return TransferResult(False, f"115 请求失败：{type(snap_exc).__name__}", 503, "115_TEMPORARILY_UNAVAILABLE")
    if not isinstance(snap, dict) or not snap.get("state"):
        # Item 5: never forward 115's raw `error` text to the user -- only a
        # fixed, generic message. The upstream text is free-form and could
        # leak internal/account-specific detail.
        return TransferResult(False, "115 分享读取失败，请稍后重试", 502, "115_PROVIDER_ERROR")
    items = (snap.get("data") or {}).get("list") or []
    file_ids = [str(item.get("fid") or item.get("cid")) for item in items if isinstance(item, dict) and (item.get("fid") or item.get("cid"))]
    if not file_ids:
        return TransferResult(False, "115 分享内没有可转存条目", 502, "115_PROVIDER_ERROR")
    payload = {"user_id": uid, "share_code": share_code, "receive_code": receive_code, "file_id": ",".join(file_ids)}
    if pid:
        payload["cid"] = str(pid)
    # share/receive is NEVER retried: an ambiguous outcome (timeout,
    # connection error or a malformed response) might mean 115 already
    # processed the transfer, so replaying it here could duplicate it.
    # TRANSFER_UNKNOWN tells the caller to check their own 115 records
    # instead of guessing. It is only ever issued with
    # >=_115_RECEIVE_MIN_REMAINING seconds of budget left; otherwise it is
    # never called at all -- an ambiguous cutoff there risks a duplicate
    # transfer, so this call must never be the one gunicorn's worker
    # timeout cuts off.
    remaining = deadline - time.monotonic()
    if remaining < _115_RECEIVE_MIN_REMAINING:
        return TransferResult(False, "转存耗时过长，请稍后重试", 504, "TRANSFER_NOT_ATTEMPTED")
    try:
        receive_resp = requests.post(SHARE_RECEIVE_URL, data=payload, headers=headers, timeout=(min(_115_UPSTREAM_CONNECT_TIMEOUT, remaining), min(_115_UPSTREAM_TIMEOUT, remaining)))
        saved = receive_resp.json() if receive_resp.content else {}
    except (requests.RequestException, ValueError, TypeError):
        # T17 fix wave 1 item 3: receive_attempted=True from here on -- the
        # POST was actually issued, so 115 may already have processed it.
        return TransferResult(False, "转存结果未知，请检查 115 转存记录", 202, "TRANSFER_UNKNOWN", receive_attempted=True)
    if not isinstance(saved, dict):
        return TransferResult(False, "转存结果未知，请检查 115 转存记录", 202, "TRANSFER_UNKNOWN", receive_attempted=True)
    if saved.get("state") or "无需重复接收" in str(saved.get("error") or ""):
        return TransferResult(True, "115 转存成功（重复转存也视为成功）", 200, receive_attempted=True)
    # Item 5: same rule as share/snap above -- fixed message only.
    return TransferResult(False, "115 转存失败，请稍后重试", 502, "115_PROVIDER_ERROR", receive_attempted=True)


def _transfer_response(result: TransferResult):
    payload: dict[str, object] = {"success": result.success, "message": result.message}
    if result.code:
        payload["code"] = result.code
    if result.retry_after:
        payload["retry_after"] = result.retry_after
    return jsonify(payload), result.status


# ---------------------------------------------------------------------------
# QR re-authorisation (docs/claude-115-cookie-persistence-and-reauth-
# 20260906.md §5, docs/115-reauth-adapter.md). Challenge state lives in
# ``reauth_challenges`` (not process memory) so start/status/cancel work
# across gunicorn's sync workers. The browser only ever sees a random
# ``challenge_id``; only its SHA-256 is persisted, and the 115 QR
# session's own uid/sign are Fernet-encrypted at rest.
# ---------------------------------------------------------------------------


def _reauth_hash(challenge_id: str) -> str:
    return hashlib.sha256(challenge_id.encode("utf-8")).hexdigest()


def _reauth_row(id_hash: str):
    with connect_db() as db:
        return db.execute("SELECT * FROM reauth_challenges WHERE id_hash=?", (id_hash,)).fetchone()


def _reauth_cooldown_count(actor: str) -> int:
    """N4 (wave 2): only genuine failures/expiries count toward the
    3-strike, 30-minute cooldown -- a user-initiated 'cancelled' (closing
    the dialog, refreshing the tab; the frontend fires a best-effort cancel
    on close) must not lock the recovery path just because it happened
    three times. REAUTH_MAX_STARTS_PER_WINDOW (_reauth_starts_count) still
    caps *all* /reauth/start attempts regardless of outcome, so cancel
    spam remains bounded -- just not by this cooldown."""
    with connect_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM reauth_challenges WHERE actor=? AND state IN ('failed','expired') AND created_at >= ?",
            (actor, utc_now() - REAUTH_COOLDOWN_WINDOW_SECONDS),
        ).fetchone()
    return int(row["c"]) if row else 0


def _reauth_starts_count(actor: str) -> int:
    """Item 4: total /reauth/start attempts per actor per window, regardless
    of outcome -- closes the gap _reauth_cooldown_count() alone leaves for a
    challenge nobody ever resolves to a countable terminal state."""
    with connect_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM reauth_challenges WHERE actor=? AND created_at >= ?",
            (actor, utc_now() - REAUTH_COOLDOWN_WINDOW_SECONDS),
        ).fetchone()
    return int(row["c"]) if row else 0


def _reauth_expire_abandoned(actor: str, now: int) -> None:
    """A challenge nobody ever polls to completion (closed tab, abandoned
    scan) would otherwise sit in 'pending'/'scanned' past its own
    ``expires_at`` forever and never count toward either cooldown above.
    Runs lazily on every /reauth/start so no background sweep is needed.

    N1 (wave 2): also sweeps a 'consuming' row -- claimed by
    api_115_reauth_status (see its comment) but never reaching a terminal
    state, e.g. because the worker handling it was killed or hit an
    uncaught exception right after the claim -- once it is past its own
    ``expires_at`` OR has been claimed for longer than
    REAUTH_CLAIM_STALE_SECONDS, whichever a stuck row hits first.

    T19 wave 3 item 2: a 'consuming' row claimed before init_db()'s
    claimed_at migration ran (or otherwise left with claimed_at NULL) is
    also treated as stale -- it can never be freshly claimed (the claim
    UPDATE always sets claimed_at), so a NULL there only ever means a
    pre-migration leftover, not a genuine in-flight claim.

    w6-reauth-longpoll-fix: 'confirmed' (115 already confirmed the QR, but
    the browser never came back for the follow-up request that runs the
    cookie exchange) is swept the same way 'pending'/'scanned' are -- a
    confirmed-but-abandoned challenge must still expire by its own TTL
    rather than sit there forever."""
    with connect_db() as db:
        db.execute(
            "UPDATE reauth_challenges SET state='expired', error_code='EXPIRED', consumed_at=? "
            "WHERE actor=? AND consumed_at IS NULL AND ("
            "  (state IN ('pending','scanned','confirmed') AND expires_at <= ?)"
            "  OR (state='consuming' AND (expires_at <= ? OR claimed_at IS NULL OR claimed_at <= ?))"
            ")",
            (now, actor, now, now, now - REAUTH_CLAIM_STALE_SECONDS),
        )


def _reauth_purge_old(now: int) -> None:
    """Cheap, lazy cleanup of long-settled challenge rows -- run on every
    /reauth/start rather than a background sweep."""
    with connect_db() as db:
        db.execute(
            "DELETE FROM reauth_challenges WHERE consumed_at IS NOT NULL AND consumed_at < ?",
            (now - REAUTH_PURGE_AGE_SECONDS,),
        )


def _115_reauth_available(actor: str) -> bool:
    return _reauth_cooldown_count(actor) < REAUTH_COOLDOWN_THRESHOLD and _reauth_starts_count(actor) < REAUTH_MAX_STARTS_PER_WINDOW


def _reauth_poll_upstream(row, deadline: float) -> tuple[str, str | None]:
    """Poll 115's QR status endpoint once. A timeout or any transport/parse
    error leaves the challenge exactly where it was -- ``pending``/
    ``scanned`` is never treated as a failure just because one poll had a
    hiccup.

    w6-reauth-longpoll: get/status is a long-poll -- 115 holds the
    connection open until the status changes or ~30s elapse -- so the read
    timeout must be long enough to actually observe that, not the flat
    ``_115_UPSTREAM_TIMEOUT`` every other upstream call uses. ``deadline``
    is the caller's request-scoped budget (``g.request_started +
    _115_REQUEST_DEADLINE_SECONDS``); the read timeout is whatever of it
    remains, capped at ``REAUTH_STATUS_POLL_READ_SECONDS`` and floored at
    2s so a near-exhausted budget still issues a (short) poll rather than
    none at all. The connect timeout is ``_115_UPSTREAM_CONNECT_TIMEOUT``
    (the file's usual convention). ``_`` is the reference client's
    cache-buster query param.

    w6-reauth-longpoll-fix: observing status 2 (115 confirmed the QR)
    returns the new non-terminal ``confirmed`` state, not ``authenticated``
    -- the caller writes it and returns immediately rather than running the
    cookie exchange in this same request. See
    ``api_115_reauth_status``'s docstring for the full state machine."""
    fernet = load_fernet()
    try:
        uid = fernet.decrypt(bytes(row["qr_uid_cipher"])).decode("utf-8")
        sign = fernet.decrypt(bytes(row["qr_sign_cipher"])).decode("utf-8")
    except (InvalidToken, ValueError):
        return "failed", "DECRYPT_FAILED"
    read_timeout = max(2, min(REAUTH_STATUS_POLL_READ_SECONDS, deadline - time.monotonic() - _115_UPSTREAM_CONNECT_TIMEOUT - 1))
    try:
        response = requests.get(
            QR_LOGIN_BASE + "/get/status/",
            params={"uid": uid, "time": row["qr_time"], "sign": sign, "_": int(time.time() * 1000)},
            timeout=(_115_UPSTREAM_CONNECT_TIMEOUT, read_timeout),
        )
        data = response.json() if response.content else {}
    except (requests.RequestException, ValueError, TypeError):
        return row["state"], row["error_code"]
    status_data = data.get("data") if isinstance(data, dict) else None
    qr_status = status_data.get("status") if isinstance(status_data, dict) else None
    if qr_status == 0:
        return "pending", None
    if qr_status == 1:
        return "scanned", None
    if qr_status == 2:
        return "confirmed", None
    if qr_status == -1:
        return "expired", "EXPIRED"
    if qr_status == -2:
        return "cancelled", "CANCELLED_BY_USER"
    return "failed", "QR_ABORTED"


def _reauth_complete(row, deadline: float) -> tuple[str, str | None]:
    """Exchange the confirmed QR session for a cookie, verify it, and only
    then overwrite the encrypted stored cookie. Never overwrites the
    existing (still-working) cookie on any failure here.

    w6-reauth-longpoll-fix: called only from a row this request just
    claimed out of the non-terminal ``confirmed`` state (see
    ``api_115_reauth_status``), so it gets that request's own full
    ``deadline`` budget -- not squeezed alongside a poll's long-poll hold.
    Deadline-aware: the exchange and the verify each use
    ``timeout=(min(_115_UPSTREAM_CONNECT_TIMEOUT, rem),
    min(_115_UPSTREAM_TIMEOUT, rem))`` with ``rem = deadline - now``. If
    ``rem`` is already below one connect timeout's worth before the
    exchange is even attempted, this returns ``("confirmed", None)`` --
    left exactly where it was for the *next* poll to retry with a fresh
    budget, never failing a confirmed challenge just for lack of time. The
    exchange itself is only ever reached this way (a claimed 'confirmed'
    row), and success is terminal, so it is never called twice after one
    already succeeded."""
    fernet = load_fernet()
    try:
        uid = fernet.decrypt(bytes(row["qr_uid_cipher"])).decode("utf-8")
    except (InvalidToken, ValueError):
        return "failed", "DECRYPT_FAILED"
    rem = deadline - time.monotonic()
    if rem < _115_UPSTREAM_CONNECT_TIMEOUT:
        return "confirmed", None
    try:
        response = requests.post(
            QR_LOGIN_BASE + "/app/1.0/web/1.0/login/qrcode/",
            data={"account": uid, "app": "web"},
            timeout=(min(_115_UPSTREAM_CONNECT_TIMEOUT, rem), min(_115_UPSTREAM_TIMEOUT, rem)),
        )
        data = response.json() if response.content else {}
    except (requests.RequestException, ValueError, TypeError):
        return "failed", "EXCHANGE_FAILED"
    result_data = data.get("data") if isinstance(data, dict) else None
    cookie_map = result_data.get("cookie") if isinstance(result_data, dict) else None
    if not isinstance(data, dict) or not data.get("state") or not isinstance(cookie_map, dict) or not cookie_map:
        return "failed", "EXCHANGE_FAILED"
    new_cookie = "; ".join(f"{k}={v}" for k, v in cookie_map.items() if isinstance(k, str) and isinstance(v, str) and k and v)
    if not new_cookie:
        return "failed", "EXCHANGE_FAILED"
    verification = verify_115_session(new_cookie, deadline)
    if verification["state"] != "valid":
        # Nothing is written on any failure: the existing, still-working
        # credential of whoever scanned stays exactly as it was.
        return "failed", "VERIFY_FAILED"
    _store_scanned_cookie(row, new_cookie, verification)
    return "authenticated", None


def _challenge_owner(row) -> "auth_service.CurrentUser | None":
    """The account that started this scan.

    Taken from the challenge itself, never from whoever happens to be
    polling: the row is what was created when the QR was issued, and it is
    consumed once (R01).
    """
    keys = row.keys() if hasattr(row, "keys") else ()
    owner_id = row["user_id"] if "user_id" in keys else None
    if owner_id is None:
        return None
    with connect_db() as db:
        found = auth_service.find_user(db, int(owner_id))
    if found is None:
        return None
    return auth_service.CurrentUser(id=int(found["id"]), email=found["email_display"],
                                    role=found["role"], status=found["status"])


def _store_settings_cookie(cookie: str) -> None:
    """A cookie the administrator pasted into the settings page.

    The legacy global slot is already written by the caller. This puts the
    same value in the administrator's own ``user_secret`` row -- which is
    what ``user_115_cookie()`` reads first once a scan has ever filled it --
    and clears an explicit step-A disconnect, because pasting a new cookie
    is the opposite decision. Only the administrator reaches this route.
    """
    user = _acting_user()
    if user is None or user.role != "admin":
        return
    now = utc_now()
    with connect_db() as db:
        with db:
            user_115.secret_set(db, user.id, user_115.COOKIE_SECRET, cookie, fernet=load_fernet(), now=now)
            row = user_115.profile(db, user.id)
            if row is not None and row["cookie_state"] == user_115.STATE_DISCONNECTED:
                user_115.remember_cookie_state(db, user.id, state="unconfigured", error_code=None, now=now)


def _store_scanned_cookie(row, cookie: str, verification: dict) -> None:
    """Save a freshly scanned 115 session to the account that scanned for it.

    A member's scan lands in their own ``user_secret`` row and nowhere else:
    it must never overwrite the administrator's global credential, and a
    member must never end up using one (R01). The administrator also keeps
    the legacy global slot written while the migration window is open,
    because that is the same credential the previous release reads.

    Step B is untouched here: reconnecting the web session must not cost
    anybody their OpenAPI authorisation (plan §4.2).
    """
    now = utc_now()
    owner = _challenge_owner(row)
    if owner is None:
        # A challenge from before the multi-user work, or a deployment with
        # no user rows yet: that is the administrator's own scan, and the
        # legacy slot is where it lives.
        secret_set("115_cookie", cookie)
        _persist_115_verification(verification, None)
        return
    with connect_db() as db:
        user_115.secret_set(db, owner.id, user_115.COOKIE_SECRET, cookie, fernet=load_fernet(), now=now)
    if owner.role == "admin":
        secret_set("115_cookie", cookie)
    _persist_115_verification(verification, owner)


_REAUTH_AUDIT_ACTION = {"authenticated": "115.reauth.success", "failed": "115.reauth.failed", "expired": "115.reauth.expired"}


def safe_join(root: Path, relative: str) -> Path:
    candidate = (root / relative.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes configured root")
    return candidate


def list_strm(relative: str = "") -> list[dict]:
    directory = safe_join(STRM_ROOT, relative)
    if not directory.exists() or not directory.is_dir():
        raise FileNotFoundError(relative or "/")
    entries = []
    for item in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:500]:
        entries.append({"name": item.name, "directory": item.is_dir(), "path": str(item.relative_to(STRM_ROOT))})
    return entries


def list_openlist(path: str, page: int = 1) -> tuple[dict, int]:
    token = config_value("openlist_token", "OPENLIST_TOKEN") or ""
    headers = {"Content-Type": "application/json"}
    if token:
        # OpenList login tokens are sent verbatim in Authorization.  Prefixing
        # them with Bearer makes the server reject an otherwise valid token.
        headers["Authorization"] = token
    payload = {"path": path or "/", "password": "", "page": max(page, 1), "per_page": 200, "refresh": False}
    try:
        response = requests.post(OPENLIST_URL + "/api/fs/list", headers=headers, json=payload, timeout=15)
        return response.json() if response.content else {"success": False, "message": "empty response"}, response.status_code
    except (requests.RequestException, ValueError, TypeError) as exc:
        if isinstance(exc, (ValueError, TypeError)):
            return {"success": False, "code": "OPENLIST_INVALID_JSON", "message": "OpenList 返回格式异常"}, 502
        return {"success": False, "code": "OPENLIST_UNAVAILABLE", "message": type(exc).__name__}, 502


def _redact_hdhive_text(value: object) -> str:
    """Remove URLs and common access-code forms from provider text fields."""
    text = str(value)
    text = re.sub(r"https?://[^\s<>\"']+", "[redacted-url]", text, flags=re.I)
    # Descriptions may omit the scheme; a bare provider URL is still private.
    text = re.sub(r"(?i)\b(?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|me|cc|tv)(?:/[^\s<>\"']*)?", "[redacted-url]", text)
    text = re.sub(r"(?i)(password|access[\s_-]+code|cookie|token|secret)\s*[:=]\s*[^\s,;，；。]+", r"\1=[redacted]", text)
    text = re.sub(r"(访问码|提取码|密码|口令|密钥)\s*[:：=]\s*[^\s,;，；。]+", r"\1：[redacted]", text)
    return text[:500]


def _safe_hdhive_envelope(data: object) -> dict:
    """Keep diagnostics, never upstream credentials or share payloads.

    The private payload is still available to server-side materialisation;
    only the browser response and persisted diagnostic text use this view.
    """
    if not isinstance(data, dict):
        return {"success": False, "code": "UPSTREAM_INVALID_JSON", "message": "RE0 返回格式异常"}
    result: dict[str, object] = {"success": bool(data.get("success", False))}
    for key in ("code", "message"):
        value = data.get(key)
        if key == "code" and isinstance(value, (str, int, float)):
            code = str(value)
            if re.fullmatch(r"[A-Z0-9_.-]{1,80}", code):
                result[key] = code
        elif key == "message" and isinstance(value, (str, int, float)) and value != "":
            result[key] = _redact_hdhive_text(value)
    return result


_SAFE_HDHIVE_RESOURCE_FIELDS = {
    "id", "resource_id", "slug", "title", "name", "description", "overview",
    "media_type", "tmdb_id", "imdb_id", "tvmaze_id", "year", "release_date",
    "quality", "resolution", "dynamic_range", "source_type", "language",
    "audio", "subtitle", "size", "status", "points", "requires_points",
    "owned", "unlocked", "created_at", "updated_at",
}


def _safe_hdhive_resource_item(item: object) -> dict:
    if not isinstance(item, dict):
        return {}
    return {
        key: _redact_hdhive_text(item[key]) if isinstance(item[key], str) else item[key]
        for key in _SAFE_HDHIVE_RESOURCE_FIELDS
        if key in item and isinstance(item[key], (str, int, float, bool))
    }


def _safe_hdhive_resources_response(data: object) -> dict:
    result = _safe_hdhive_envelope(data)
    if not isinstance(data, dict) or not data.get("success", False):
        return result
    raw = data.get("data")
    if isinstance(raw, list):
        result["data"] = [_safe_hdhive_resource_item(item) for item in raw]
        result["resource_count"] = len(raw)
    elif isinstance(raw, dict):
        items = raw.get("items") if isinstance(raw.get("items"), list) else raw.get("resources")
        if isinstance(items, list):
            result["data"] = [_safe_hdhive_resource_item(item) for item in items]
            result["resource_count"] = len(items)
        else:
            result["data"] = _safe_hdhive_resource_item(raw)
        for key in ("total", "page", "pages", "per_page"):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                result[key] = value
    else:
        result["data"] = []
        result["resource_count"] = 0
    return result


def _safe_hdhive_unlock_response(data: object) -> dict:
    """Return unlock state without returning a share URL or access code."""
    result = _safe_hdhive_envelope(data)
    if not isinstance(data, dict) or not data.get("success", False):
        return result
    raw = data.get("data")
    link_count = 0
    if isinstance(raw, dict):
        for key in ("links", "resources", "items"):
            value = raw.get(key)
            if isinstance(value, list):
                link_count = max(link_count, len(value))
        # Current RE0 also returns one share directly, rather than an array.
        # Count its presence without copying any part of the private value.
        if any(isinstance(raw.get(key), str) and raw[key].strip()
               for key in ("url", "full_url", "share_url")):
            link_count = max(link_count, 1)
        summary = _safe_hdhive_resource_item(raw)
    elif isinstance(raw, list):
        link_count = len(raw)
        summary = {}
    else:
        summary = {}
    summary["links_available"] = link_count > 0
    summary["link_count"] = link_count
    result["data"] = summary
    return result


def status_payload(verify_115: bool = False) -> dict:
    row = get_tokens()
    n115 = cookie_status(verify=verify_115)
    with connect_db() as db:
        checkin_row = db.execute("SELECT success, message, created_at FROM checkins ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "app": APP_NAME,
        "auth_mode": AUTH_MODE,
        "public_origin_configured": bool(PUBLIC_ORIGIN),
        "hdhive": {
            "app_secret_configured": bool(config_value("hdhive_app_secret", "HDHIVE_APP_SECRET")),
            "client_id_configured": bool(config_value("hdhive_client_id", "HDHIVE_CLIENT_ID")),
            "authorized": bool(row and decrypt_token(row, "access_token")),
            "expires_at": iso(row["expires_at"]) if row else None,
            "refresh_expires_at": iso(row["refresh_expires_at"]) if row else None,
            "scope": row["scope"] if row else None,
            "checkin": {
                "last_success": bool(checkin_row["success"]) if checkin_row else None,
                "last_at": iso(int(checkin_row["created_at"])) if checkin_row else None,
            },
        },
        "115": {
            "cookie_configured": bool(n115["configured"]),
            "cookie_valid": n115["valid"],
            "cookie_state": n115["state"],
            "cookie_checked_at": iso(int(n115["checked_at"])) if n115["checked_at"] else None,
            "cookie_last_success_at": iso(int(n115["last_success_at"])) if n115["last_success_at"] else None,
            "cookie_error_code": n115["error_code"],
            "retry_after": n115["retry_after"],
            "reauth_available": _115_reauth_available(actor_id()),
            "target_pid_configured": bool(setting_get("115_target_pid", "")),
            "open_platform_configured": bool(config_value("115_open_access_token", "115_open_access_token") and config_value("115_open_refresh_token", "115_open_refresh_token")),
            "open_root_cid_configured": bool(setting_get("115_open_root_cid", "")),
        },
        "openlist": {"configured": bool(OPENLIST_URL), "token_configured": bool(config_value("openlist_token", "OPENLIST_TOKEN")), "paths": {"115pan": OPENLIST_115PAN_PATH, "115strm": OPENLIST_115STRM_PATH}},
        "strm": {"exists": STRM_ROOT.exists()},
        "library": _library_status_summary(),
    }


def _library_status_summary() -> dict:
    try:
        store = library_store.open_installed(LIBRARY_DB_PATH, None)
    except (
        library_store.LibraryNotInstalled,
        library_store.LibraryNotEncrypted,
        library_store.LibrarySchemaMismatch,
        library_store.LibraryIndexUnreadable,
    ):
        return {"installed": False, "media_total": 0}
    return {"installed": True, "media_total": store.stats()["media_total"]}


def _compute_asset_version() -> str:
    """Cache-busting fingerprint: sha256 of the shipped static assets."""
    static_dir = Path(__file__).resolve().parent / "static"
    digest = hashlib.sha256()
    for name in ("app.js", "app.css", "icons.svg"):
        digest.update((static_dir / name).read_bytes())
    return digest.hexdigest()[:8]


ASSET_VERSION = _compute_asset_version()


# ---------------------------------------------------------------------------
# T4: personal media library -- query routes, install/rollback CLI, reveal
# and server-side 115 transfer bridge (docs §7, §2.4, §9).
# ---------------------------------------------------------------------------

TMDB_IMAGE_BASE = os.getenv("TMDB_IMAGE_BASE", "https://image.tmdb.org/t/p/").rstrip("/") + "/"
LIBRARY_POSTER_SIZE = "w342"
LIBRARY_POSTER_LARGE_SIZE = "w500"
LIBRARY_BACKDROP_SIZE = "w1280"


def _tmdb_image_url(path: str | None, size: str) -> str | None:
    return TMDB_IMAGE_BASE + size + path if path else None


def _tmdb_lock_paths() -> "library_tmdb.LockPaths":
    """The three distinct lock files (leader/run/budget) every TMDB
    enrichment code path -- background thread, ``--library-enrich`` CLI,
    ``scripts/enrich_media_tmdb.py`` -- must share (P0 fix: reusing one
    lock file across the leader lock and the client's own budget lock
    deadlocked the background thread against itself)."""
    return library_tmdb.LockPaths.for_data_dir(DATA_DIR)


def _library_charmap(store: "library_store.LibraryStore") -> dict[str, str]:
    conn = store.connect(readonly=True)
    try:
        rows = conn.execute("SELECT src, dst FROM search_charmap").fetchall()
    finally:
        conn.close()
    return {row["src"]: row["dst"] for row in rows}


def _library_store_or_error(need_key: bool):
    """Open the installed library, or return ``(None, error_response)`` --
    the T4 brief's common precondition mapping every LibraryStore exception
    to a 503 with the matching ``code``.

    ``need_key`` controls whether the master key is required: read-only
    routes (search/filters/suggest/media/resource/tmdb-status) pass
    ``False`` and open the store with ``fernet=None`` -- a missing or
    unreadable master key must never block plain browsing. ``reveal`` and
    ``transfer`` pass ``True`` and get ``LIBRARY_KEY_UNAVAILABLE`` here
    when ``load_fernet()`` itself fails (missing/invalid key file); a key
    that loads fine but fails to decrypt a specific row is a separate,
    later failure -- see their own ``LibraryKeyUnavailable`` handling
    around ``store.reveal()``."""
    fernet = None
    if need_key:
        try:
            fernet = load_fernet()
        except (OSError, RuntimeError):
            return None, json_error("主密钥不可用", 503, "LIBRARY_KEY_UNAVAILABLE")
    try:
        return library_store.open_installed(LIBRARY_DB_PATH, fernet), None
    except library_store.LibraryNotInstalled:
        return None, json_error("资源库索引尚未安装", 503, "LIBRARY_NOT_INSTALLED")
    except library_store.LibraryIndexUnreadable:
        return None, json_error("资源库索引无法读取", 503, "LIBRARY_INDEX_UNREADABLE")
    except library_store.LibraryNotEncrypted:
        return None, json_error("资源库索引未加密，拒绝提供服务", 503, "LIBRARY_NOT_ENCRYPTED")
    except library_store.LibrarySchemaMismatch:
        return None, json_error("资源库索引版本高于当前程序支持的版本", 503, "LIBRARY_SCHEMA_MISMATCH")


def _state_int(state: dict, key: str) -> int | None:
    try:
        return int(state[key])
    except (KeyError, TypeError, ValueError):
        return None


def _library_tmdb_paused_reason(
    *, worker_state: str, enabled: bool, key_configured: bool, has_heartbeat: bool
) -> str | None:
    """Why the enricher isn't currently making progress (T10 self-diagnosis)
    -- ``None`` only when ``worker_state == "running"``. Checked in this
    exact order (matches the brief): a switched-off/unconfigured feature is
    reported before a merely-quiet worker, since that's the more actionable
    fact for a non-technical user reading the settings page. ``installed``
    is always ``True`` here (this function only ever runs against an
    already-opened store), so ``"not_installed"`` never actually fires
    through this path -- it's kept for parity with the documented enum."""
    if worker_state == "running":
        return None
    if not enabled:
        return "disabled"
    if not key_configured:
        return "key_missing"
    if not has_heartbeat:
        return "no_heartbeat"
    return "stale"


def _library_tmdb_status_payload(store: "library_store.LibraryStore") -> dict:
    """Build the ``/api/library/tmdb-status`` payload. Read-only: every DB
    access below uses a readonly connection, and ``Budget.status()``/
    ``read_enricher_state`` themselves never write (§D)."""
    stats = store.stats()
    resolution = library_tmdb.resolve_daily_budget({"tmdb_daily_budget": setting_get("tmdb_daily_budget")}, os.environ)
    budget = library_tmdb.Budget(
        lambda: store.connect(readonly=True), _tmdb_lock_paths().budget, resolution.effective
    ).status()

    state_conn = store.connect(readonly=True)
    try:
        state = library_tmdb.read_enricher_state(state_conn)
    finally:
        state_conn.close()

    enabled = _library_enrich_enabled_check()
    key_configured = bool(config_value("tmdb_api_key", "TMDB_API_KEY"))
    idle_seconds = _library_enricher.idle_seconds if _library_enricher else 60
    worker_state = library_tmdb.derive_worker_state(
        state, now=time.time(), enabled=enabled, key_configured=key_configured, installed=True, idle_seconds=idle_seconds
    )
    paused_reason = _library_tmdb_paused_reason(
        worker_state=worker_state, enabled=enabled, key_configured=key_configured,
        has_heartbeat=_state_int(state, "heartbeat_at") is not None,
    )
    match_counts = stats["match"]
    cache_counts = stats["cache"]

    return {
        "installed": True,
        "schema_version": int(stats["schema_version"]) if stats["schema_version"] else None,
        "built_at": stats["built_at"],
        "installed_at": stats["installed_at"],
        "media_total": stats["media_total"],
        "links_total": stats["links_total"],
        "match": match_counts,
        "budget": budget,
        "cache": cache_counts,
        "key_configured": key_configured,
        "enricher": _library_enricher.status() if _library_enricher else None,
        "enrich_enabled": enabled,
        "configured_budget": resolution.configured,
        "effective_budget": resolution.effective,
        "cap_source": resolution.cap_source,
        "used": budget["used"],
        "remaining": budget["remaining"],
        "reset_at": budget["reset_at"],
        "worker_state": worker_state,
        "paused_reason": paused_reason,
        "total_media": stats["media_total"],
        "matched_total": match_counts.get("exact", 0),
        "last_heartbeat": iso(_state_int(state, "heartbeat_at")),
        "last_round_at": iso(_state_int(state, "last_round_at")),
        "last_round_processed": _state_int(state, "last_round_processed") or 0,
        "last_error": state.get("last_error_class") or None,
        "cache_count": sum(cache_counts.values()),
        "exact_count": match_counts.get("exact", 0),
        "candidate_count": match_counts.get("candidate", 0),
        "needs_review_count": match_counts.get("needs_review", 0),
        "unmatched_count": match_counts.get("unmatched", 0),
        # T14/§3.1, fix wave 1 (finding #4): three needs_review buckets --
        # never queried / has real candidates / queried but empty -- plus
        # their total, for the settings TMDB card.
        "review_pending_unqueried": stats.get("review_pending_unqueried", 0),
        "review_scored": stats.get("review_scored", 0),
        "review_no_candidate": stats.get("review_no_candidate", 0),
        "needs_review_total": stats.get("needs_review_total", match_counts.get("needs_review", 0)),
        # T14/§4-§5.1: persisted budget/429/error accounting, exposed under
        # tmdb_-prefixed names alongside the existing used/remaining/
        # last_error keys above (kept for backward compatibility).
        "tmdb_budget_used": budget["used"],
        "tmdb_budget_remaining": budget["remaining"],
        "tmdb_requests_429": _state_int(state, "requests_429_today") or 0,
        "tmdb_last_error_class": state.get("last_error_class") or None,
        # T15 design item 4: Codex's offline IMDb candidate hints, read-only.
        **store.hint_stats(),
        "tvmaze_hint_enabled": _library_tvmaze_enabled_check(),
    }


def _apply_access_code(url: str, access_code: str | None) -> str:
    """Append ``?password=<code>`` when the URL doesn't already carry a
    non-blank one -- checked in both the query string and the #fragment
    (115 share links sometimes carry it there, see ``parse_115_link``).

    Uses ``keep_blank_values=False`` semantics like ``parse_115_link``: an
    empty ``?password=`` is treated as "no password", not as "already has
    one" -- and, since it's still sitting in the query string, it gets
    replaced by the stored access code rather than duplicated alongside
    it. Reconstructed via proper URL-component handling rather than string
    concatenation."""
    if not access_code:
        return url
    parsed = urlparse(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    fragment_pairs = parse_qsl(parsed.fragment, keep_blank_values=False)
    has_password = any(k.lower() in {"password", "pwd"} and v for k, v in query_pairs) or any(
        k.lower() in {"password", "pwd"} for k, _ in fragment_pairs
    )
    if has_password:
        return url
    # Any password/pwd pair still present here is blank (a non-blank one
    # would have returned above already) -- drop it so the stored code
    # replaces it instead of being appended alongside it.
    kept_pairs = [(k, v) for k, v in query_pairs if k.lower() not in {"password", "pwd"}]
    password_param = "password=" + quote(access_code, safe="")
    new_query = urlencode(kept_pairs, quote_via=quote) + "&" + password_param if kept_pairs else password_param
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


_library_enricher: "library_tmdb.BackgroundEnricher | None" = None
_library_enricher_started = False
_library_enricher_lock = threading.Lock()

# Test-only injection point: tests monkeypatch this to hand the *real*
# production wiring (_build_library_enricher/_library_client_factory) a
# FakeSession instead of a live requests.Session(), without needing a
# separate test-only construction path.
_tmdb_session_factory = lambda: requests.Session()  # noqa: E731

# T14 fix wave 1: one process-wide RateLimiter, lazily created and shared by
# every TmdbClient this worker process builds via _library_client_factory
# (the background enricher's per-round client and the fast tmdb-check/
# tmdb-enrich-now client alike). Before this, TmdbClient.__init__ made its
# own fresh RateLimiter every call, so BackgroundEnricher._loop building a
# brand-new client each round silently discarded the adaptive 429
# slow-down's pacing state (ADAPTIVE_SLOWDOWN_SECONDS) at the end of every
# single round. Reset to None in tests' ``workspace`` fixture so state never
# leaks between tests.
_tmdb_rate_limiter: "library_tmdb.RateLimiter | None" = None
_tmdb_rate_limiter_lock = threading.Lock()


def _shared_tmdb_rate_limiter(min_interval_ms: int) -> "library_tmdb.RateLimiter":
    """Return this process's one shared ``RateLimiter``, creating it on
    first use with ``min_interval_ms`` as its base pacing interval. Later
    calls (even with a different ``min_interval_ms``, e.g. after a settings
    change) keep returning the same already-created instance -- its base
    interval only takes effect again after a process restart, same as every
    other value ``Limits`` bakes into a long-lived object."""
    global _tmdb_rate_limiter
    with _tmdb_rate_limiter_lock:
        if _tmdb_rate_limiter is None:
            _tmdb_rate_limiter = library_tmdb.RateLimiter(min_interval_ms)
        return _tmdb_rate_limiter


def _library_client_factory(*, fast: bool = False) -> "library_tmdb.TmdbClient | None":
    """Build the production ``TmdbClient``. ``fast=True`` (T10 fix wave 1)
    is for the two synchronous settings-page routes: it caps a single HTTP
    attempt at 4s and disables retries (``max_retries=0``) so one flaky
    request can never compound into the ~67s-per-request worst case (4
    attempts x up to 15s each, plus 1+2+4s backoff) that risked exceeding
    gunicorn's default 30s worker timeout. The background enricher thread
    keeps the full default retry policy (it is not subject to a
    request-serving timeout); the ``--library-enrich``/
    ``--library-requeue-review`` CLIs build their own client directly and
    never go through this factory at all.

    Every client this factory builds -- fast or not -- shares the one
    process-wide ``RateLimiter`` from ``_shared_tmdb_rate_limiter`` (T14 fix
    wave 1), so the adaptive 429 slow-down's cooldown survives across the
    background enricher's rounds and is visible to a manual tmdb-check/
    tmdb-enrich-now call made from the same worker process shortly after."""
    key = config_value("tmdb_api_key", "TMDB_API_KEY")
    if not key:
        return None
    limits = library_tmdb.effective_limits({"tmdb_daily_budget": setting_get("tmdb_daily_budget")}, os.environ)
    fast_kwargs = {"request_timeout": 4.0, "max_retries": 0} if fast else {}
    return library_tmdb.TmdbClient(
        key,
        conn_factory=lambda: sqlite3.connect(LIBRARY_DB_PATH),
        lock_path=_tmdb_lock_paths().budget,
        limits=limits,
        session=_tmdb_session_factory(),
        limiter=_shared_tmdb_rate_limiter(limits.min_interval_ms),
        **fast_kwargs,
    )


def _tmdb_error_hint(error_class: str | None) -> str:
    """User-facing Chinese hint for a TMDB error class (T10 §2/§5), shared
    by ``tmdb-check``'s response and the settings page's "最近错误" line.
    Never echoes a key, URL or upstream response body -- only the already
    known-safe exception class name."""
    if error_class == "InvalidApiKey":
        return "TMDB API Key 无效：请填写 v3 auth key（32 位十六进制），不是 v4 Read Access Token"
    if error_class == "BudgetExhausted":
        return "今日额度已用完，明日 UTC 0 点重置或提高每日预算"
    network_markers = ("Connection", "Timeout", "Proxy", "SSL")
    if error_class in {"ConnectionError", "Timeout", "RequestException"} or any(
        marker in (error_class or "") for marker in network_markers
    ):
        return "无法连接 TMDB：请检查服务器网络或代理设置"
    return f"TMDB 请求失败：{error_class}"


def _library_tmdb_precheck(*, fast: bool = False):
    """Shared precondition for ``tmdb-check``/``tmdb-enrich-now``: the
    library must be installed (503 ``LIBRARY_NOT_INSTALLED`` etc.) and a
    TMDB API key configured (400 ``TMDB_KEY_MISSING``). Returns ``(store,
    client, None)`` on success or ``(None, None, error_response)``.
    ``fast`` is forwarded to ``_library_client_factory`` (T10 fix wave 1)."""
    store, error = _library_store_or_error(False)
    if error:
        return None, None, error
    client = _library_client_factory(fast=fast)
    if client is None:
        return None, None, json_error("TMDB API Key 未配置", 400, "TMDB_KEY_MISSING")
    return store, client, None


def _library_store_factory() -> "library_store.LibraryStore | None":
    try:
        return library_store.open_installed(LIBRARY_DB_PATH, load_fernet())
    except (
        library_store.LibraryNotInstalled,
        library_store.LibraryNotEncrypted,
        library_store.LibrarySchemaMismatch,
        library_store.LibraryIndexUnreadable,
    ):
        return None


def _re0_extra_round(store, client) -> None:
    """RE0 projection metadata, using the existing TMDB budget."""
    re0_sync.enrich_projections(store, client, limit=5)


def _library_index_round(store) -> bool:
    """Pure local work: independent of TMDB configuration and availability."""
    return re0_sync.rebuild_index_if_dirty(store, charmap=_library_charmap(store))


def _library_enrich_enabled_check() -> bool:
    return setting_get("tmdb_enrich_enabled", "1") == "1"


def _library_tvmaze_enabled_check() -> bool:
    """T15/z1-hints §3: TVmaze fallback, default off."""
    return setting_get("tvmaze_hint_enabled", "0") == "1"


def _library_enricher_conn_factory() -> "sqlite3.Connection | None":
    """The enricher persists its heartbeat/round state through this
    factory. It must never be what *creates* ``LIBRARY_DB_PATH`` --
    ``sqlite3.connect()`` creates the file if it doesn't exist, and on a
    host with no library installed that leaves behind a stub database
    containing only the tmdb tables, which ``open_installed`` then reports
    as ``LibraryIndexUnreadable`` instead of ``LibraryNotInstalled``
    (review fix #1). Returning ``None`` when the file is absent tells
    ``BackgroundEnricher`` to skip persistence for that write instead."""
    if not LIBRARY_DB_PATH.exists():
        return None
    return sqlite3.connect(LIBRARY_DB_PATH)


def _build_library_enricher() -> "library_tmdb.BackgroundEnricher":
    """Wire the production ``BackgroundEnricher`` exactly: the three
    distinct lock files (``_tmdb_lock_paths()``), a ``conn_factory`` for
    the persisted worker state, and the same store/client factories/
    enabled-check the rest of the app uses."""
    lock_paths = _tmdb_lock_paths()
    return library_tmdb.BackgroundEnricher(
        _library_store_factory,
        _library_client_factory,
        leader_lock_path=lock_paths.leader,
        run_lock_path=lock_paths.run,
        conn_factory=_library_enricher_conn_factory,
        enabled_check=_library_enrich_enabled_check,
        tvmaze_enabled_check=_library_tvmaze_enabled_check,
        extra_round=_re0_extra_round,
        local_round=_library_index_round,
    )


def _ensure_library_enricher_started() -> None:
    """Start the background enricher thread at most once per process
    (double-checked-locked), at worker init (see ``if __name__ !=
    "__main__"`` right after ``app = Flask(...)``/``init_db()`` below) with
    ``before_request`` kept as an idempotent fallback.
    ``LIBRARY_ENRICH_AUTOSTART`` is read lazily here (not at import time)
    so tests can monkeypatch it per-test; ``_library_enricher_started`` is
    itself reset per test by the ``workspace`` fixture so each test gets
    its own fresh evaluation."""
    global _library_enricher, _library_enricher_started
    if _library_enricher_started:
        return
    with _library_enricher_lock:
        if _library_enricher_started:
            return
        _library_enricher_started = True
        if not env_bool("LIBRARY_ENRICH_AUTOSTART", True):
            return
        _library_enricher = _build_library_enricher()
        _library_enricher.start()


# ---------------------------------------------------------------------------
# w6: link validity checker -- production wiring, mirroring the TMDB
# enricher's factory/leader-thread pattern immediately above.
# ---------------------------------------------------------------------------

# Test-only injection point, mirroring _tmdb_session_factory: tests
# monkeypatch this to hand the real production wiring a FakeSession instead
# of a live requests.Session().
_linkcheck_session_factory = lambda: library_tmdb.build_anonymous_session()  # noqa: E731


def _linkcheck_leader_lock_path() -> Path:
    """``linkcheck-leader.lock``, next to the TMDB lock files (same
    ``DATA_DIR``) but a distinct path -- the two background threads must
    never contend on the same fcntl lock."""
    return DATA_DIR / "linkcheck-leader.lock"


# ---------------------------------------------------------------------------
# 115 云下载 (spec docs/superpowers/specs/2026-09-09-115-cloud-download-design.md):
# push ED2K/magnet links to 115's offline downloader through the open
# platform, with the same access_token the directory listing already uses.
# ---------------------------------------------------------------------------

_CLOUD_TIMEZONE = ZoneInfo("Asia/Shanghai")
_CLOUD_PER_SUBMIT_CAP_DEFAULT = 30
_CLOUD_PER_SUBMIT_CAP_MAX = 50
_CLOUD_DAILY_CAP_MAX = 100000


def _cloud_settings_dict() -> dict:
    """``{"enabled", "daily_cap", "per_submit_cap"}`` from the settings
    table; daily_cap 0 means unlimited (the user's own call: 115 has no
    daily rule, only the monthly quota)."""
    def _int(name: str, default: int, lo: int, hi: int) -> int:
        raw = setting_get(name)
        try:
            value = int(raw) if raw is not None else default
        except ValueError:
            value = default
        return min(max(value, lo), hi)
    return {
        "enabled": setting_get("cloud_download_enabled", "0") == "1",
        "daily_cap": _int("cloud_download_daily_cap", 0, 0, _CLOUD_DAILY_CAP_MAX),
        "per_submit_cap": _int("cloud_download_per_submit_cap", _CLOUD_PER_SUBMIT_CAP_DEFAULT, 1, _CLOUD_PER_SUBMIT_CAP_MAX),
    }


_CLOUD_CACHE: dict[object, tuple[float, object]] = {}
_CLOUD_QUOTA_TTL_SECONDS = 60
_CLOUD_TASKS_TTL_SECONDS = 10
_CLOUD_LINK_RE = re.compile(r"(?:ed2k://|magnet:|https?://|ftp://|urn:btih:)\S*", re.I)
_CLOUD_AUTH_ERRNOS = {"40140116", "40140125", "40140126", "40140127"}


def _cloud_sanitize_message(text: object) -> str:
    """115's own ``message`` is shown to the user verbatim -- minus any link
    it might echo back (the same no-URL rule every other response keeps)."""
    if not text:
        return ""
    return re.sub(r"\s{2,}", " ", _CLOUD_LINK_RE.sub("", str(text))).strip()


def _open115_offline(
    method: str, path: str, *, data: dict | None = None, params: dict | None = None,
    deadline: float | None = None, allow_resync: bool = True,
) -> tuple[dict | None, int, str]:
    """One call to a 115 open-platform cloud-download endpoint under the
    synced access_token. Returns ``(json, status, error)``: ``status`` 200
    with ``error == ""`` only when 115 says ``state: true``; an auth
    error re-syncs the token from OpenList once (like ``open115_list``)
    and retries once; a missing token is 503 without any request."""
    # Phase 6: whoever is asking, with their own step-B token. A user without
    # one never falls through to another's -- the route refused them first.
    # The version comes with it so a 401 can be attributed to this exact
    # generation of the credential (F03).
    acting = _acting_user()
    access_token, token_version = _open115_credential(acting)
    if not access_token:
        return None, 503, "尚未完成「目录与云下载」授权"
    remaining = _115_UPSTREAM_TIMEOUT if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        return None, 504, "请求预算已用尽"
    timeout = (min(_115_UPSTREAM_CONNECT_TIMEOUT, remaining), min(_115_UPSTREAM_TIMEOUT, remaining))
    headers = {"Authorization": "Bearer " + access_token}
    try:
        if method.upper() == "POST":
            response = requests.post(OPEN115_API_BASE + path, headers=headers, data=data or {}, timeout=timeout)
        else:
            response = requests.get(OPEN115_API_BASE + path, headers=headers, params=params or {}, timeout=timeout)
        payload = response.json() if response.content else {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        return None, 502, f"115 开放平台请求失败：{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, 502, "115 开放平台返回格式异常"
    auth_error = response.status_code in {401, 403} or str(payload.get("errno") or payload.get("code") or "") in _CLOUD_AUTH_ERRNOS
    if auth_error and allow_resync:
        # F02/F03: one shared recovery, whoever the caller is and wherever
        # their credential came from, and exactly one business retry after
        # it. `allow_resync=False` on the retry is what bounds this -- a
        # credential that is genuinely revoked returns the re-authorisation
        # answer instead of looping.
        if _open115_recover(acting, token_version):
            return _open115_offline(method, path, data=data, params=params,
                                    deadline=deadline, allow_resync=False)
        if acting is not None and _has_own_open_token(acting):
            return payload, 401, "「目录与云下载」授权已过期，请重新授权"
    if response.status_code >= 400 or not payload.get("state", True):
        message = _cloud_sanitize_message(payload.get("message") or payload.get("error")) or "115 开放平台令牌可能已失效"
        return payload, response.status_code if response.status_code >= 400 else 502, message
    return payload, 200, ""


def _cloud_cached(key: object, ttl: int, force: bool, fetch):
    now = time.monotonic()
    cached = _CLOUD_CACHE.get(key)
    if cached is not None and not force and now - cached[0] < ttl:
        return cached[1], ""
    payload, error = fetch()
    if payload is not None:
        _CLOUD_CACHE[key] = (now, payload)
    return payload, error


def _cloud_quota(force: bool = False, deadline: float | None = None) -> tuple[dict | None, str]:
    def fetch():
        body, status, error = _open115_offline("GET", "/open/offline/get_quota_info", deadline=deadline)
        data = body.get("data") if isinstance(body, dict) and status == 200 else None
        return (data if isinstance(data, dict) else None), (error or ("" if isinstance(data, dict) else "115 配额返回格式异常"))
    return _cloud_cached(_cloud_cache_key(_acting_user(), "quota"), _CLOUD_QUOTA_TTL_SECONDS, force, fetch)


def _cloud_task_list(page: int, force: bool = False, deadline: float | None = None) -> tuple[dict | None, str]:
    def fetch():
        body, status, error = _open115_offline("GET", "/open/offline/get_task_list", params={"page": page}, deadline=deadline)
        data = body.get("data") if isinstance(body, dict) and status == 200 else None
        return (data if isinstance(data, dict) else None), (error or ("" if isinstance(data, dict) else "115 任务列表返回格式异常"))
    return _cloud_cached(_cloud_cache_key(_acting_user(), "tasks", page), _CLOUD_TASKS_TTL_SECONDS, force, fetch)


_CLOUD_DOWNLOAD_LOCK = threading.Lock()
_CLOUD_DEDUPE_WINDOW_SECONDS = 60
_CLOUD_DEDUPE_LOCK = threading.Lock()
_CLOUD_DEDUPE_SEEN: dict[tuple[str, str], float] = {}
_CLOUD_CHUNK_SIZE = 10
_CLOUD_CHUNK_GAP_SECONDS = 1.5
_cloud_sleep = time.sleep


def _cloud_dedupe_check(public_ids: list[str], pid: str) -> bool:
    """All-or-nothing: True (recording every id) unless any
    ``(user, id, pid)`` was already submitted inside the window.

    The user is part of the key (R19/R05): two people queueing the same
    release to their own accounts are two submissions. The upstream rate
    limits that protect 115 are a separate, still-global concern.
    """
    now = time.monotonic()
    user_key = _dedupe_user_key()
    with _CLOUD_DEDUPE_LOCK:
        for key, seen_at in list(_CLOUD_DEDUPE_SEEN.items()):
            if now - seen_at >= _CLOUD_DEDUPE_WINDOW_SECONDS:
                del _CLOUD_DEDUPE_SEEN[key]
        if any((user_key, public_id, pid) in _CLOUD_DEDUPE_SEEN for public_id in public_ids):
            return False
        for public_id in public_ids:
            _CLOUD_DEDUPE_SEEN[(user_key, public_id, pid)] = now
        return True


def _cloud_dedupe_clear(public_ids: list[str], pid: str) -> None:
    user_key = _dedupe_user_key()
    with _CLOUD_DEDUPE_LOCK:
        for public_id in public_ids:
            _CLOUD_DEDUPE_SEEN.pop((user_key, public_id, pid), None)


def _cloud_submit_chunks(items: list[dict], pid: str, deadline: float | None) -> tuple[list[dict], str]:
    """Serially submit ``items`` (``{"link_id", "label", "url"}``) to 115 in
    chunks of ``_CLOUD_CHUNK_SIZE`` with ``_CLOUD_CHUNK_GAP_SECONDS``
    between chunks -- never concurrently. A chunk-level failure (HTTP
    error, ``state: false``, rate limit) stops the whole batch: that
    chunk's and every later item come back ``not_submitted`` and the 115
    message is returned as the stop reason."""
    results: list[dict] = []
    chunks = [items[i:i + _CLOUD_CHUNK_SIZE] for i in range(0, len(items), _CLOUD_CHUNK_SIZE)]
    for index, chunk in enumerate(chunks):
        if index:
            _cloud_sleep(_CLOUD_CHUNK_GAP_SECONDS)
        body, status, error = _open115_offline(
            "POST", "/open/offline/add_task_urls",
            data={"urls": "\n".join(item["url"] for item in chunk), "wp_path_id": pid}, deadline=deadline,
        )
        if status != 200:
            for item in chunk:
                results.append({"link_id": item["link_id"], "label": item["label"], "state": "not_submitted"})
            for later in chunks[index + 1:]:
                for item in later:
                    results.append({"link_id": item["link_id"], "label": item["label"], "state": "not_submitted"})
            return results, error or "115 云下载接口失败"
        entries = body.get("data") if isinstance(body.get("data"), list) else []
        by_url = {str(e.get("url") or ""): e for e in entries if isinstance(e, dict)}
        for position, item in enumerate(chunk):
            entry = by_url.get(item["url"]) or (entries[position] if position < len(entries) and isinstance(entries[position], dict) else {})
            if entry.get("state") and entry.get("info_hash"):
                results.append({"link_id": item["link_id"], "label": item["label"], "state": "ok", "info_hash": str(entry["info_hash"])})
            else:
                results.append({
                    "link_id": item["link_id"], "label": item["label"], "state": "failed",
                    "message": _cloud_sanitize_message(entry.get("message")) or "115 未接受该链接",
                })
    return results, ""


def _cloud_today_submitted() -> int:
    """How many tasks *this* user submitted today. The daily cap is a
    per-user budget, not a shared one.

    For the administrator during the migration window the legacy
    ``cloud_download_task`` table still counts: tasks submitted before this
    release -- or before ``--auth-migrate --apply`` copies them -- exist only
    there, and a cap that forgot them would let today's budget be spent
    twice. Counted by info_hash across both tables, so a task the new code
    wrote to both is one task.
    """
    day_start = int(datetime.now(_CLOUD_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    user = _acting_user()
    with connect_db() as db:
        if _writes_legacy_cloud_task():
            row = db.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT info_hash FROM user_cloud_download_task WHERE user_id=? AND submitted_at >= ?"
                "  UNION SELECT info_hash FROM cloud_download_task WHERE submitted_at >= ?)",
                (user.id if user else 0, day_start, day_start),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) FROM user_cloud_download_task WHERE user_id=? AND submitted_at >= ?",
                (user.id if user else 0, day_start),
            ).fetchone()
    return int(row[0]) if row else 0


def _linkcheck_settings_dict() -> dict:
    """Read every ``linkcheck_*`` setting into the flat dict shape
    ``library_tmdb``'s ``_linkcheck_provider_enabled``/``_linkcheck_provider_
    cap``/``run_link_check_round`` expect."""
    settings = {"linkcheck_enabled": setting_get("linkcheck_enabled", "0")}
    for code in library_tmdb.LINK_CHECK_PROVIDERS:
        settings[f"linkcheck_{code}_enabled"] = setting_get(f"linkcheck_{code}_enabled", "0")
        cap = setting_get(f"linkcheck_{code}_daily_cap")
        if cap is not None:
            settings[f"linkcheck_{code}_daily_cap"] = cap
    return settings


def _linkcheck_enabled_check() -> bool:
    return library_tmdb._linkcheck_global_enabled(_linkcheck_settings_dict())


def _linkcheck_conn_factory() -> "sqlite3.Connection | None":
    """Mirrors ``_library_enricher_conn_factory``: never the thing that
    *creates* ``LIBRARY_DB_PATH`` on a host with no library installed."""
    if not LIBRARY_DB_PATH.exists():
        return None
    return sqlite3.connect(LIBRARY_DB_PATH)


def _linkcheck_client_factory() -> "library_tmdb.LinkCheckClient | None":
    return library_tmdb.LinkCheckClient(
        conn_factory=lambda: sqlite3.connect(LIBRARY_DB_PATH),
        settings=_linkcheck_settings_dict(),
        session=_linkcheck_session_factory(),
    )


def _build_library_linkchecker() -> "library_tmdb.BackgroundLinkChecker":
    return library_tmdb.BackgroundLinkChecker(
        _library_store_factory,
        _linkcheck_client_factory,
        leader_lock_path=_linkcheck_leader_lock_path(),
        conn_factory=_linkcheck_conn_factory,
        settings_factory=_linkcheck_settings_dict,
        enabled_check=_linkcheck_enabled_check,
    )


def _ensure_library_linkchecker_started() -> None:
    """Start the background link checker thread at most once per process,
    mirroring ``_ensure_library_enricher_started`` exactly (double-checked
    lock, ``LIBRARY_ENRICH_AUTOSTART`` reused as the shared autostart
    switch for every library background thread, latch reset per test by
    the ``workspace`` fixture)."""
    global _library_linkchecker, _library_linkchecker_started
    if _library_linkchecker_started:
        return
    with _library_linkchecker_lock:
        if _library_linkchecker_started:
            return
        _library_linkchecker_started = True
        if not env_bool("LIBRARY_ENRICH_AUTOSTART", True):
            return
        _library_linkchecker = _build_library_linkchecker()
        _library_linkchecker.start()


_library_linkchecker: "library_tmdb.BackgroundLinkChecker | None" = None
_library_linkchecker_started = False
_library_linkchecker_lock = threading.Lock()


def _read_only_cli() -> bool:
    """Whether this process was started for a command that must not write.

    `init_db()` runs at import and creates tables; a dry run that reports
    "would create 8 tables" after creating them is not a dry run (R10). The
    check is deliberately narrow: only `--auth-migrate` without `--apply`.
    Every other entry point, gunicorn's worker import included, still
    initialises as before.
    """
    argv = sys.argv[1:]
    return bool(argv) and argv[0] == "--auth-migrate" and "--apply" not in argv


app = Flask(__name__)
if not _read_only_cli():
    init_db()
if __name__ != "__main__":
    # gunicorn imports this module as "app" at worker init, so this runs
    # once per worker before it ever serves a request -- the CLI (which
    # runs as __main__) must never start a background thread here.
    # before_request below is kept as an idempotent fallback.
    _ensure_library_enricher_started()
    _ensure_library_linkchecker_started()


@app.before_request
def before_request() -> None:
    # T19 wave 4 item 2: recorded as the FIRST statement, before Access/
    # JWKS work (require_access() below can do a real JWKS fetch on a
    # cache miss) or anything else -- /api/115/save and /api/library/
    # transfer derive their single request-scoped deadline from this
    # (g.request_started + _115_REQUEST_DEADLINE_SECONDS), not from a
    # fresh time.monotonic() call inside the view, so time already spent
    # in before_request counts against that budget too.
    g.request_started = time.monotonic()
    if request.path == "/healthz":
        # A Tunnel connection commonly arrives from loopback, so remote_addr
        # alone cannot distinguish a public request from a local curl.
        #
        # What this means per mode, deliberately (R14): under `access` the
        # probe must present an Access assertion, so an unauthenticated
        # request -- loopback or not -- is 401. Under `hybrid`/`app` the site
        # is no longer wholly behind that policy and the probe answers 200
        # without one: a liveness endpoint that reveals nothing is the point
        # of having it. It carries no user data in any mode.
        if AUTH_MODE in {"local", "disabled"}:
            g.principal = {"sub": "local", "email": "local"}
        else:
            require_access()
        return
    require_access()
    # Before check_csrf() below: a token minted for a session is bound to it.
    _load_auth_session()
    denied = authorize_request()
    if denied is not None:
        return denied
    # Started only after a request has cleared authentication, so an
    # unauthenticated/rejected request never triggers this side effect.
    _ensure_library_enricher_started()
    _ensure_library_linkchecker_started()
    if request.method in {"POST", "PATCH", "PUT", "DELETE"} and not request.path.startswith("/api/oauth/hdhive/callback"):
        check_csrf()


@app.after_request
def no_cache_dynamic_pages(response):
    if request.path == "/" or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def handle_error(error):
    if request.path.startswith("/api/"):
        return json_error(getattr(error, "description", "request failed"), getattr(error, "code", 500))
    return (getattr(error, "description", "request failed"), getattr(error, "code", 500))


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": APP_NAME, "database": DB_PATH.exists(), "strm_root": STRM_ROOT.exists()})


@app.get("/")
def index():
    """The application shell, built for who is asking.

    What a member may not use is not rendered at all -- not hidden with CSS,
    not left in the DOM for a devtools toggle to reveal (plan §13). The
    server-side gate in `authorize_request` is still the boundary; this is
    what keeps the page honest about it.
    """
    user = current_user()
    if user is None:
        caps = auth_service.capabilities(role="member", allow_member_re0_unlock=False,
                                         has_115_cookie=False, has_115_open=False)
    else:
        # R04: the real state, not a placeholder. A page built on "nobody has
        # 115" renders no dialogs, and then the buttons that open them have
        # nothing to open.
        has_cookie, has_open = _user_115_state(user.id, user.role)
        caps = auth_service.capabilities(
            role=user.role, allow_member_re0_unlock=allow_member_re0_unlock(),
            has_115_cookie=has_cookie, has_115_open=has_open)
    return render_template("index.html", asset_version=ASSET_VERSION,
                           public_origin=PUBLIC_ORIGIN, capabilities=caps)


# The artwork's copy lives in two `settings` rows -- no new table, and no
# dependency on the media-library bundle being installed.
_LOGIN_ARTWORK_SETTING = "login_artwork_24_urls_json"
_LOGIN_ARTWORK_AT_SETTING = "login_artwork_24_fetched_at"


def _login_artwork_read():
    raw = setting_get(_LOGIN_ARTWORK_SETTING, "") or ""
    if not raw:
        return None
    try:
        urls = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(urls, list):
        return None
    try:
        fetched_at = int(setting_get(_LOGIN_ARTWORK_AT_SETTING, "0") or "0")
    except ValueError:
        return None
    return urls, fetched_at


def _login_artwork_write(urls, now: int) -> None:
    setting_set(_LOGIN_ARTWORK_SETTING, json.dumps(list(urls)))
    setting_set(_LOGIN_ARTWORK_AT_SETTING, str(int(now)))


_LOGIN_ARTWORK_FAILED_SETTING = "login_artwork_failed_at"


def _login_artwork_read_failure():
    try:
        failed_at = int(setting_get(_LOGIN_ARTWORK_FAILED_SETTING, "0") or "0")
    except ValueError:
        return None
    return failed_at or None


def _login_artwork_write_failure(now: int) -> None:
    setting_set(_LOGIN_ARTWORK_FAILED_SETTING, str(int(now)))


@app.get("/login")
def login_page():
    """The sign-in / apply page.

    It renders in every mode. Today the whole site is still behind
    Cloudflare Access, so only the administrator can reach it; it becomes the
    public entrance when `HIDRIVE_AUTH_MODE` moves to `app` (plan §5.1), and
    that switch is a separate, human step.
    """
    return render_template("login.html", asset_version=ASSET_VERSION, public_origin=PUBLIC_ORIGIN)


# Plan §11.2: the exact wording TMDB asks for. Rendered on the page; the key
# itself never leaves this process.
TMDB_ATTRIBUTION = "This product uses the TMDB API but is not endorsed or certified by TMDB."


@app.get("/api/auth/bootstrap")
def api_auth_bootstrap():
    """Everything the login page needs and nothing else.

    A pre-auth CSRF token, whether applications are open, the administrator
    hint, the attribution line, and cached poster URLs. No setting, no
    credential, no TMDB key, and no reason for the artwork to be missing --
    the page simply draws its own gradient when the list is empty.
    """
    urls, source = ([], "unavailable")
    try:
        urls, source = library_tmdb.login_artwork(
            api_key=config_value("tmdb_api_key", "TMDB_API_KEY"),
            session=_tmdb_session_factory(), now=utc_now(),
            read_cache=_login_artwork_read, write_cache=_login_artwork_write,
            read_failure=_login_artwork_read_failure, write_failure=_login_artwork_write_failure,
        )
    except Exception as exc:  # noqa: BLE001 - artwork must never block signing in
        LOG.warning("login artwork unavailable: %s", type(exc).__name__)
    return jsonify({
        "success": True,
        "csrf": csrf_token(),
        "registration_open": True,
        "admin_email_hint": "管理员请使用 Google 登录",
        "artwork": urls,
        "artwork_source": source,
        "tmdb_attribution": TMDB_ATTRIBUTION,
    })


@app.get("/api/csrf")
def api_csrf():
    return jsonify({"success": True, "token": csrf_token()})


def _readonly_db() -> sqlite3.Connection:
    """A connection that cannot write, for the dry run. Opening the file
    read-only also means no directory is created and no WAL appears."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _auth_migration_plan(db) -> dict:
    """What `ensure_schema()` would add, worked out by reading rather than
    by adding it and counting afterwards."""
    existing = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tables = [name for name in auth_service.NEW_TABLES if name not in existing]
    columns = []
    for table, column, _column_type in auth_service.EXTENDED_TABLES:
        if table not in existing:
            continue
        present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            columns.append(f"{table}.{column}")
    return {"tables_to_create": tables, "columns_to_add": columns}


_PROFILE_MIGRATION_FIELDS = ("open_root_cid", "default_target_cid", "default_target_label")


def _profile_migration_plan(db, admin_id: int | None) -> dict:
    """Whether the administrator's ``user_115_profile`` row would be copied.

    Read-only, and the *same* decision the apply makes, so "dry-run then
    decide" is trustworthy (G05). It also has to work on a database the new
    columns were only just added to (G04): every record in such a database has
    ``migrated_at`` NULL, including rows an older branch's migration already
    filled and the administrator has since edited. Treating "no marker" as "no
    data" put the old global values back over their choices.

    The order of evidence, most reliable first -- never a guess from whether a
    value happens to equal a default:

    1. ``migrated_at`` is set: this branch migrated it. Skip.
    2. The row already carries any of the three fields this migration writes:
       somebody -- an older migration, or the administrator -- put it there.
       Skip and record the marker, so the answer is unambiguous next time.
    3. The administrator already holds a migrated ``user_secret``: a previous
       apply ran (the secrets and the profile are copied by the same command),
       so the empty folder fields are a deliberate clearing. Skip and mark.
    4. Otherwise: nothing has ever been migrated. Copy.
    """
    plan = {"exists": False, "marked": False, "has_fields": False, "secrets_migrated": False,
            "would_copy": True, "reason": "first migration", "backfill_marker": False,
            "readable": True}
    tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if admin_id is None or "user_115_profile" not in tables:
        return plan
    columns = {row[1] for row in db.execute("PRAGMA table_info(user_115_profile)")}
    present = [name for name in _PROFILE_MIGRATION_FIELDS if name in columns]
    selected = ", ".join(present) if present else "user_id"
    has_marker_column = "migrated_at" in columns
    marker = ", migrated_at" if has_marker_column else ""
    row = db.execute(f"SELECT {selected}{marker} FROM user_115_profile WHERE user_id=?", (admin_id,)).fetchone()
    if row is None:
        return plan
    plan["exists"] = True
    plan["marked"] = bool(has_marker_column and row["migrated_at"] is not None)
    plan["has_fields"] = any(row[name] for name in present)
    if "user_secret" in tables:
        plan["secrets_migrated"] = db.execute(
            "SELECT 1 FROM user_secret WHERE user_id=? LIMIT 1", (admin_id,)).fetchone() is not None
    if plan["marked"]:
        plan.update(would_copy=False, reason="already migrated by this branch")
    elif plan["has_fields"]:
        plan.update(would_copy=False, backfill_marker=True,
                    reason="pre-existing configuration from an earlier migration or the administrator")
    elif plan["secrets_migrated"]:
        plan.update(would_copy=False, backfill_marker=True,
                    reason="an earlier apply already ran; the empty folder fields are a deliberate clearing")
    return plan


def run_auth_migrate_dry_run() -> int:
    """Report what an apply would do, touching nothing.

    Reads through a read-only connection and reports presence only -- never
    a value, never a ciphertext, never a hash of a plaintext.
    """
    report: dict = {"ok": True, "dry_run": True, "items": {}}
    try:
        db = _readonly_db()
    except sqlite3.OperationalError:
        report["items"]["database"] = {"present": False}
        print(json.dumps(report, ensure_ascii=False))
        return 0
    try:
        report["items"]["schema"] = _auth_migration_plan(db)
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        admin_present = False
        if "auth_user" in tables:
            admin_present = db.execute(
                "SELECT 1 FROM auth_user WHERE email_norm=?",
                (auth_service.normalize_email(ADMIN_EMAIL),)).fetchone() is not None
        report["items"]["admin_user"] = {"existed": admin_present, "would_create": not admin_present}

        stored = set()
        if "secrets" in tables:
            stored = {row["name"] for row in db.execute("SELECT name FROM secrets")}
        already: set = set()
        if "user_secret" in tables and admin_present:
            already = {row["name"] for row in db.execute(
                "SELECT s.name AS name FROM user_secret s JOIN auth_user u ON u.id = s.user_id "
                "WHERE u.email_norm=?", (auth_service.normalize_email(ADMIN_EMAIL),))}
        for legacy, target in (("115_cookie", user_115.COOKIE_SECRET),
                               ("115_open_access_token", user_115.OPEN_ACCESS_SECRET),
                               ("115_open_refresh_token", user_115.OPEN_REFRESH_SECRET)):
            report["items"][target] = {
                "present": legacy in stored,
                "already_migrated": target in already,
                "would_copy": legacy in stored and target not in already,
            }

        # F08: per task and per administrator. Subtracting one table's total
        # row count from another's counted every *other* user's tasks as
        # already-migrated work, so one outstanding legacy task beside one
        # unrelated member task reported would_copy=0 while the apply copied 1.
        legacy_hashes: set[str] = set()
        if "cloud_download_task" in tables:
            legacy_hashes = {row["info_hash"] for row in db.execute("SELECT info_hash FROM cloud_download_task")}
        admin_hashes: set[str] = set()
        if "user_cloud_download_task" in tables and admin_present:
            admin_hashes = {row["info_hash"] for row in db.execute(
                "SELECT t.info_hash AS info_hash FROM user_cloud_download_task t "
                "JOIN auth_user u ON u.id = t.user_id WHERE u.email_norm=?",
                (auth_service.normalize_email(ADMIN_EMAIL),))}
        outstanding = legacy_hashes - admin_hashes
        report["items"]["cloud_download_task"] = {
            "source_rows": len(legacy_hashes),
            "already_migrated": len(legacy_hashes & admin_hashes),
            "would_copy": len(outstanding),
        }
        # F07: the same marker the apply reads, so dry-run and apply agree on
        # whether the profile would be copied or skipped.
        # G05: never SELECT a column without checking the structure first --
        # an old database has the table and not the column, and the point of a
        # dry run is that it works *before* the apply.
        admin_row = None
        if "auth_user" in tables:
            admin_row = db.execute("SELECT id FROM auth_user WHERE email_norm=?",
                                   (auth_service.normalize_email(ADMIN_EMAIL),)).fetchone()
        try:
            profile_plan = _profile_migration_plan(db, int(admin_row["id"]) if admin_row else None)
        except sqlite3.DatabaseError as exc:
            # An unknown structure gets a structured answer, not a bare SQL
            # traceback, and the run still writes nothing.
            profile_plan = {"readable": False, "would_copy": None,
                            "reason": f"cannot be read on this schema: {type(exc).__name__}"}
        report["items"]["user_115_profile"] = {
            "already_migrated": (profile_plan.get("marked") or profile_plan.get("has_fields")
                                 or profile_plan.get("secrets_migrated") or False),
            "would_copy": profile_plan["would_copy"],
            "reason": profile_plan["reason"],
            "readable": profile_plan.get("readable", True),
        }
        report["items"]["left_global"] = ["oauth_tokens", "tmdb", "openlist", "media-library.db"]
    finally:
        db.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0


def run_auth_migrate(*, apply: bool) -> int:
    """Copy the administrator's existing configuration into the per-user
    tables (plan §8). Explicit, idempotent, and a copy -- never a move.

    What it reports per item is "present/missing, rows copied, and whether the
    value decrypted back equal in this process". It never prints a plaintext,
    a ciphertext, or a hash of a plaintext: re-encryption uses a fresh nonce,
    so identical ciphertext is not a success criterion and identical hashes
    would themselves leak.

    Nothing here runs at startup, and nothing here deletes or rewrites the
    old global rows: a rollback to the previous release still reads them.
    """
    now = utc_now()
    report: dict = {"ok": True, "dry_run": not apply, "items": {}}
    fernet = load_fernet()

    with connect_db() as db:
        auth_service.ensure_schema(db)
        admin_row = db.execute("SELECT id FROM auth_user WHERE email_norm=?",
                               (auth_service.normalize_email(ADMIN_EMAIL),)).fetchone()
        admin_id = int(admin_row["id"]) if admin_row else None
        if admin_id is None and apply:
            admin_id = auth_service.ensure_admin_user(db, email=ADMIN_EMAIL, now=now)
        report["items"]["admin_user"] = {"existed": admin_row is not None, "created": apply and admin_row is None}

        # G04/G05: decided before this run copies anything. One of the pieces
        # of evidence is "the administrator already has migrated secrets", and
        # the secret copy below is part of *this* run -- reading it afterwards
        # would let a run conclude that it had already happened. Computing it
        # here is also what makes the dry run's answer the apply's answer.
        profile_plan = _profile_migration_plan(db, admin_id)

        # Step A + step B credentials, re-encrypted under the same master key.
        secret_map = (
            ("115_web_cookie", config_value("115_cookie", "ENV_115_COOKIES")),
            ("115_open_access_token", config_value("115_open_access_token", "115_open_access_token")),
            ("115_open_refresh_token", config_value("115_open_refresh_token", "115_open_refresh_token")),
        )
        for name, value in secret_map:
            entry = {"present": bool(value), "copied": 0, "skipped": 0, "verified": None}
            if value and apply and admin_id is not None:
                # R11: a first copy, never an overwrite. By the time this runs
                # again the administrator may have re-scanned or refreshed;
                # putting the old global value back over it would undo that
                # silently. The legacy row stays where it is either way, so a
                # rollback still finds it.
                existing = db.execute("SELECT 1 FROM user_secret WHERE user_id=? AND name=?",
                                      (admin_id, name)).fetchone()
                if existing is not None:
                    entry["skipped"] = 1
                    entry["reason"] = "already migrated; refusing to overwrite a newer value"
                else:
                    db.execute(
                        "INSERT INTO user_secret(user_id, name, ciphertext, updated_at) VALUES(?,?,?,?)",
                        (admin_id, name, fernet.encrypt(value.encode("utf-8")), now),
                    )
                    stored = db.execute("SELECT ciphertext FROM user_secret WHERE user_id=? AND name=?",
                                        (admin_id, name)).fetchone()
                    entry["copied"] = 1
                    entry["verified"] = fernet.decrypt(bytes(stored["ciphertext"])).decode("utf-8") == value
                    if name == user_115.OPEN_ACCESS_SECRET:
                        # F02: this pair came from OpenList and OpenList still
                        # rotates it. Recording that is what lets the recovery
                        # path re-sync from there instead of presenting the
                        # refresh token to 115 itself -- two systems rotating
                        # one credential is how you lose it.
                        user_115.remember_open_origin(db, admin_id, user_115.ORIGIN_OPENLIST_LEGACY, now=now)
            report["items"][name] = entry

        root_cid = setting_get("115_open_root_cid", None)
        # R17: the stored default is `115_target_pid` -- the name the rest of
        # the app reads and writes. Looking for `115_target_cid` found
        # nothing, so the administrator's default folder was silently lost.
        target_cid = setting_get("115_target_pid", None)
        target_label = setting_get("115_target_path", None) or setting_get("cloud_download_target_path", None)
        entry = {"root_cid_present": bool(root_cid), "target_present": bool(target_cid),
                 "copied": 0, "skipped": 0}
        if apply and admin_id is not None:
            if not profile_plan["would_copy"]:
                # R11 + F07 + G04: migrated before -- by this branch, by an
                # older one, or evidenced by the secrets an earlier apply
                # copied. Whether the administrator has since chosen a
                # different folder or cleared it, both are their decision.
                entry["skipped"] = 1
                entry["reason"] = profile_plan["reason"]
                if profile_plan["backfill_marker"]:
                    # Make the answer unambiguous from now on, without
                    # touching any of their values.
                    db.execute("UPDATE user_115_profile SET migrated_at=?, updated_at=? WHERE user_id=?",
                               (now, now, admin_id))
                    entry["marker_backfilled"] = 1
            else:
                db.execute(
                    "INSERT INTO user_115_profile(user_id, open_root_cid, default_target_cid, "
                    "default_target_label, migrated_at, updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET open_root_cid=excluded.open_root_cid, "
                    "default_target_cid=excluded.default_target_cid, "
                    "default_target_label=excluded.default_target_label, "
                    "migrated_at=excluded.migrated_at, updated_at=excluded.updated_at",
                    (admin_id, root_cid, target_cid, target_label, now, now),
                )
                entry["copied"] = 1
        report["items"]["user_115_profile"] = entry

        legacy_tasks = db.execute("SELECT * FROM cloud_download_task").fetchall()
        entry = {"source_rows": len(legacy_tasks), "copied": 0}
        if apply and admin_id is not None:
            copied = 0
            for task in legacy_tasks:
                # R17: carry the origin annotation across too -- the list
                # shows which release and which link a task came from, and a
                # migration that drops them loses it for every old task.
                cursor = db.execute(
                    "INSERT INTO user_cloud_download_task(user_id, info_hash, source_kind, media_id, display_title, "
                    "group_id, link_label, submitted_at, last_seen_at, state) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(user_id, info_hash) DO NOTHING",
                    (admin_id, task["info_hash"], "legacy", task["media_id"], task["media_title"],
                     task["group_id"], task["link_label"],
                     task["submitted_at"], task["last_seen_at"], str(task["last_status"] or "")),
                )
                copied += cursor.rowcount or 0
            # R17: what this run actually copied, not how many rows the table
            # happens to hold.
            entry["copied"] = copied
            entry["skipped"] = len(legacy_tasks) - copied
        report["items"]["cloud_download_task"] = entry

        # Left global on purpose (plan §8.6): RE0 OAuth, TMDB, OpenList, the
        # media library. They are not per-user data.
        report["items"]["left_global"] = ["oauth_tokens", "tmdb", "openlist", "media-library.db"]
        if not apply:
            db.rollback()

    report["ok"] = all(
        item.get("verified") is not False for item in report["items"].values() if isinstance(item, dict)
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------
# Multi-user Phase 1: applications, approval, sessions and the member policy.
# Every write here goes through before_request's CSRF check like any other.
# ---------------------------------------------------------------------------


def _user_115_state(user_id: int, role: str) -> tuple[bool, bool]:
    """Whether this user has finished step A (web cookie) and step B (OpenAPI
    token). Read from that user's own rows -- never from another user's.

    The administrator may reuse legacy credentials independently for each
    step, unless that step was explicitly disconnected. A member never takes
    that compatibility path (plan §9.1).
    """
    if role == "admin":
        # A Cookie-only user slot must not hide the administrator's existing
        # OpenList pair. Resolve the two steps independently, including disconnects.
        user = auth_service.CurrentUser(id=user_id, email="", role=role, status="active")
        status = _open115_status(user)
        return status["transfer"]["state"] == "connected", status["browse"]["state"] == "connected"
    with connect_db() as db:
        names = {row["name"] for row in db.execute("SELECT name FROM user_secret WHERE user_id=?", (user_id,))}
    if names:
        return "115_web_cookie" in names, "115_open_access_token" in names
    return False, False


def _me_payload(user) -> dict:
    has_cookie, has_open = _user_115_state(user.id, user.role)
    payload = {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "capabilities": auth_service.capabilities(
            role=user.role,
            allow_member_re0_unlock=allow_member_re0_unlock(),
            has_115_cookie=has_cookie,
            has_115_open=has_open,
        ),
    }
    if user.role == "admin":
        # R09: the policy's own value, distinct from "may I unlock" -- the
        # administrator always may, which is why their capability could not
        # stand in for the switch.
        payload["allow_member_re0_unlock"] = allow_member_re0_unlock()
    return payload


def _session_cookie(response, token: str):
    response.set_cookie(
        SESSION_COOKIE_NAME, token, max_age=auth_service.SESSION_MAX_SECONDS,
        secure=True, httponly=True, samesite="Lax", path="/",
    )
    return response


def _request_ip_hash() -> str:
    return hashlib.sha256((request.headers.get("Cf-Connecting-Ip") or request.remote_addr or "").encode()).hexdigest()


def _open115_config() -> "user_115.OpenAppConfig":
    """HiDrive-Lite's own 115 application, if it has one.

    Deployment configuration, not a user's: the client id may be read by the
    server, the secret only ever leaves it towards 115, and neither reaches a
    browser (plan §9.1). With nothing configured, step B reports itself
    blocked rather than pretending to be one click away.
    """
    return user_115.OpenAppConfig(
        client_id=config_value("115_open_client_id", "HIDRIVE_115_OPEN_CLIENT_ID"),
        client_secret=config_value("115_open_client_secret", "HIDRIVE_115_OPEN_CLIENT_SECRET"),
        redirect_uri=(setting_get("115_open_redirect_uri", "") or "").strip() or None,
        device_flow_verified=(setting_get("115_open_device_flow_verified", "0") or "0").strip() in {"1", "true"},
    )


def _open115_device_blocked(config):
    # The settings UI promises an APP-scanned device flow. An OAuth-code
    # configuration alone does not provide that end-to-end flow.
    return config.blocked_reason() or (
        user_115.BLOCKED_NO_VERIFICATION if config.flow() != user_115.FLOW_DEVICE_PKCE else None)


def _open115_status(user) -> dict:
    """Step A and step B as the settings page words them: by capability, with
    no hint of what is stored (plan §4.2)."""
    with connect_db() as db:
        credentials = user_115.credentials_for(db, user.id, fernet=load_fernet())
        row = user_115.profile(db, user.id)
    # The administrator's legacy global cookie counts as their step A until
    # --auth-migrate copies it across.
    has_cookie = credentials.has_cookie or bool(user_115_cookie(user))
    has_open = credentials.has_open_token
    origin = (row["open_token_origin"] if row is not None else None) or (
        user_115.ORIGIN_OPENLIST_LEGACY if user.role == "admin" else user_115.ORIGIN_OWN_APP)
    source = ("openlist" if origin == user_115.ORIGIN_OPENLIST_LEGACY else "scan") if has_open else None
    if (user.role == "admin" and not credentials.access_token
            and (row is None or row["open_state"] != user_115.STATE_DISCONNECTED)):
        access, refresh = _legacy_open_pair()
        has_open = bool(access and refresh)
        source = "openlist" if has_open else None
    blocked = _open115_device_blocked(_open115_config())
    return {
        "transfer": {
            "state": "connected" if has_cookie else "unconfigured",
            "label": "已连接" if has_cookie else "待连接",
            "checked_at": iso(row["cookie_checked_at"]) if row else None,
            "error_code": (row["cookie_error_code"] if row else None),
        },
        "browse": {
            "state": "connected" if has_open else ("blocked" if blocked else "unconfigured"),
            "label": "已连接" if has_open else "待连接",
            "source": source,
            "blocked_reason": blocked,
            "expires_at": iso(row["open_expires_at"]) if row else None,
            "error_code": (row["open_error_code"] if row else None),
        },
        "summary": "全部可用" if has_cookie and has_open else "基础可用" if has_cookie else "待连接",
        "flow": _open115_config().flow(),
    }


@app.get("/api/me/115/status")
@require_login
def api_me_115_status():
    return jsonify({"success": True, **_open115_status(current_user())})


@app.post("/api/me/115/open/start")
@require_login
def api_me_115_open_start():
    """Begin step B -- or say plainly why it cannot begin.

    While the application is unapproved this is the whole of step B: it
    refuses with the blocking reason and writes nothing. It never falls back
    to OpenList's web-login QR, to a public token broker, or to the
    administrator's token (plan §17).
    """
    config = _open115_config()
    blocked = _open115_device_blocked(config)
    if blocked:
        return jsonify({
            "success": False, "code": blocked.upper(), "blocked_reason": blocked,
            "message": "115 开放平台授权尚未开通，暂时无法授权目录与云下载。",
        }), 409
    user = current_user()
    fernet = load_fernet()
    try:
        with requests.Session() as session:
            flow = user_115.DeviceAuthorization(connect_db, fernet=fernet,
                adapter=user_115.Open115Adapter(config, session=session), config=config, clock=utc_now)
            started = flow.start(user.id)
    except user_115.Open115Error as exc:
        return _open115_flow_error(exc)
    with connect_db() as db:
        audit("115.open.start", "ok", f"user {user.id} flow {config.flow()}", actor_id(), db=db)
    return jsonify({"success": True, "flow": user_115.FLOW_DEVICE_PKCE, **started})


def _open115_flow_error(exc):
    status = {"invalid_challenge": 404, "invalid_user": 403,
              "rate_limited": 429, "flow_not_available": 409}.get(exc.error_class, 502)
    response = json_error("115 授权未完成，请稍后重新扫码。", status, "OPEN115_" + exc.error_class.upper())
    if status == 429:
        response = app.make_response(response)
        response.headers["Retry-After"] = "10"
    return response


@app.post("/api/me/115/open/status")
@require_login
def api_me_115_open_status():
    # This may consume a device code and save credentials: POST + the common
    # CSRF guard, never a GET that a cross-site image could trigger.
    body = request_json()
    config = _open115_config()
    try:
        with requests.Session() as session:
            flow = user_115.DeviceAuthorization(connect_db, fernet=load_fernet(),
                adapter=user_115.Open115Adapter(config, session=session), config=config, clock=utc_now)
            status = flow.poll(current_user().id, body.get("challenge_id"))
    except user_115.Open115Error as exc:
        return _open115_flow_error(exc)
    return jsonify({"success": True, "status": status, "retry_after": 2})


@app.post("/api/me/115/open/cancel")
@require_login
def api_me_115_open_cancel():
    body = request_json()
    config = _open115_config()
    flow = user_115.DeviceAuthorization(connect_db, fernet=load_fernet(),
        adapter=None, config=config, clock=utc_now)
    try:
        status = flow.cancel(current_user().id, body.get("challenge_id"))
    except user_115.Open115Error as exc:
        return _open115_flow_error(exc)
    return jsonify({"success": True, "status": status})


@app.post("/api/me/115/disconnect")
@require_login
def api_me_115_disconnect():
    """Forget one step's credentials. Naming one never touches the other
    (plan §4.2): disconnecting the cookie must not cost somebody their
    OpenAPI authorisation."""
    body = request_json()
    step = str(body.get("step") or "").strip()
    if step not in {"transfer", "browse"}:
        return json_error("step 必须是 transfer 或 browse", 400, "BAD_REQUEST")
    user = current_user()
    names = ((user_115.COOKIE_SECRET,) if step == "transfer"
             else (user_115.OPEN_ACCESS_SECRET, user_115.OPEN_REFRESH_SECRET))
    now = utc_now()
    with connect_db() as db:
        removed = user_115.secret_clear(db, user.id, *names)
        if step == "transfer":
            user_115.remember_cookie_state(db, user.id, state=user_115.STATE_DISCONNECTED,
                                           error_code=None, now=now)
        else:
            user_115.remember_open_state(db, user.id, state=user_115.STATE_DISCONNECTED,
                                         expires_at=None, error_code=None, now=now)
        audit("115.disconnect", "ok", f"user {user.id} step {step} removed {removed}", actor_id(), db=db)
    return jsonify({"success": True, "step": step, "removed": removed})


@app.get("/api/me")
@require_login
def api_me():
    return jsonify({"success": True, **_me_payload(current_user())})


@app.post("/api/auth/register")
def api_auth_register():
    """An application, nothing more: it creates a pending member and no
    session. Role and status are never read from the body (plan §17)."""
    body = request_json()
    email = str(body.get("email") or "")
    password = str(body.get("password") or "")
    confirm = str(body.get("confirm_password") or password)
    display_name = (str(body.get("display_name") or "").strip() or None)
    if password != confirm:
        return json_error("两次输入的密码不一致", 400, "PASSWORD_MISMATCH")
    now = utc_now()
    # Counted and closed before anything expensive happens: hashing a
    # password while holding this write lock would let a flood of attempts
    # serialise behind it (R13).
    with connect_db() as db:
        allowed = auth_service.within_attempt_limits(
            db, "register", ip=_request_ip_hash(),
            email_norm=auth_service.normalize_email(email), now=now)
    if not allowed:
        return json_error("申请过于频繁，请稍后再试", 429, "RATE_LIMITED")
    with connect_db() as db:
        try:
            user_id = auth_service.create_pending_user(
                db, email=email, password=password, display_name=display_name, now=now, admin_email=ADMIN_EMAIL
            )
        except auth_service.PasswordRejected as exc:
            return json_error(_PASSWORD_MESSAGES.get(exc.reason, "密码不符合要求"), 400, "PASSWORD_REJECTED")
        except (auth_service.EmailTaken, auth_service.EmailReserved):
            # One answer either way: whether an address is already registered
            # is not something an anonymous caller may enumerate.
            audit("auth.register", "rejected", "email unavailable", "", db=db)
            return jsonify({"success": True, "status": "pending"}), 202
        audit("auth.register", "ok", f"user {user_id}", "", db=db)
    return jsonify({"success": True, "status": "pending"}), 202


_PASSWORD_MESSAGES = {
    "too_short": "密码至少 8 位",
    "too_long": "密码最多 128 位",
    "needs_upper": "密码需要至少一个大写字母",
    "needs_lower": "密码需要至少一个小写字母",
    "needs_digit": "密码需要至少一个数字",
    "bad_email": "邮箱格式不正确",
}


@app.post("/api/auth/login")
def api_auth_login():
    body = request_json()
    email = str(body.get("email") or "")
    password = str(body.get("password") or "")
    email_norm = auth_service.normalize_email(email)
    now = utc_now()
    with connect_db() as db:
        allowed = auth_service.within_attempt_limits(
            db, "login", ip=_request_ip_hash(), email_norm=email_norm, now=now)
    if not allowed:
        # Refused before the scrypt comparison: the cost of an attempt is
        # exactly what the limit exists to bound.
        return json_error("尝试过于频繁，请稍后再试", 429, "RATE_LIMITED")
    with connect_db() as db:
        result = auth_service.authenticate(db, email=email, password=password, now=now)
        if not result.ok:
            # The precise reason goes to the audit trail only.
            audit("auth.login", "rejected", result.audit_reason, "", db=db)
            return json_error(result.message, 401, "LOGIN_FAILED")
        issued = auth_service.issue_session(db, result.user_id, now=now, ip_hash=_request_ip_hash())
        row = auth_service.find_user(db, result.user_id)
        audit("auth.login", "ok", f"user {result.user_id}", str(result.user_id), db=db)
    user = auth_service.CurrentUser(id=int(row["id"]), email=row["email_display"],
                                    role=row["role"], status=row["status"])
    response = jsonify({"success": True, **_me_payload(user)})
    return _session_cookie(response, issued.token)


def _safe_next(raw: str | None) -> str:
    """A site-relative path, or the home page.

    Anything else -- an absolute URL, a protocol-relative ``//host``, a
    scheme, a backslash -- is discarded rather than corrected. This is the
    only place a redirect target comes from user input (plan §5.2's
    open-redirect rule).
    """
    candidate = (raw or "").strip()
    if not candidate.startswith("/"):
        return "/"
    # `//host` is protocol-relative and `/\host` is treated the same way by
    # several browsers; a backslash or a control character has no business in
    # a path we are about to send someone to.
    if candidate.startswith("//") or "\\" in candidate:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return "/"
    return candidate


@app.get("/auth/google")
def auth_google():
    """The administrator's way in, bridged from Cloudflare Access.

    Access guards this path and its policy admits one address. The same
    address is checked again here, because an edge policy is configuration
    and configuration changes (plan §5.2). Both must agree.

    The stable identity is Cloudflare's ``sub``; the email is used to
    authorise and to display, never as a key -- an address can be reassigned,
    a subject cannot.
    """
    principal = getattr(g, "principal", None) or {}
    email = str(principal.get("email") or "").strip().casefold()
    subject = str(principal.get("sub") or "").strip()
    verified = principal.get("email_verified")
    next_path = _safe_next(request.args.get("next"))

    if not subject or not email:
        audit("auth.google", "rejected", "no verified principal", "")
        return json_error("需要通过 Cloudflare Access 登录", 401, "ACCESS_REQUIRED")
    if verified is False:
        # Google says this address is unverified; an unverified address is
        # not an identity.
        audit("auth.google", "rejected", "email not verified", "")
        return json_error("该 Google 账号邮箱未验证", 403, "EMAIL_NOT_VERIFIED")
    if email != ADMIN_EMAIL:
        audit("auth.google", "rejected", "not the administrator address", "")
        return json_error("该账号无权使用管理员入口", 403, "FORBIDDEN")

    now = utc_now()
    with connect_db() as db:
        user_id = auth_service.ensure_admin_user(db, email=ADMIN_EMAIL, now=now)
        auth_service.bind_identity(db, user_id=user_id, provider="cloudflare_google",
                                   subject=subject, email=email, now=now)
        # Plan §5.4 asks for the session to rotate on sign-in: the session
        # this browser arrived with is replaced. Other devices keep theirs --
        # signing in on a phone is not a reason to sign a desktop out (R21).
        # Revoking everything is what disabling an account does.
        previous = request.cookies.get(SESSION_COOKIE_NAME, "")
        if previous:
            auth_service.revoke_session(db, previous, now=now)
        issued = auth_service.issue_session(db, user_id, now=now, ip_hash=_request_ip_hash())
        audit("auth.google", "ok", f"user {user_id}", str(user_id), db=db)
    response = redirect(next_path)
    return _session_cookie(response, issued.token)


@app.post("/api/auth/logout")
def api_auth_logout():
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if token:
        with connect_db() as db:
            auth_service.revoke_session(db, token, now=utc_now())
    response = jsonify({"success": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/api/admin/users")
@require_role("admin")
def api_admin_users():
    with connect_db() as db:
        pending_count = db.execute("SELECT COUNT(*) FROM auth_user WHERE status='pending'").fetchone()[0]
        if request.args.get("summary") == "1":
            return jsonify({"success": True, "pending_count": pending_count})
        rows = db.execute(
            "SELECT id, email_display, display_name, role, status, created_at, approved_at, last_login_at "
            "FROM auth_user ORDER BY (status='pending') DESC, created_at DESC LIMIT 500"
        ).fetchall()
    return jsonify({"success": True, "users": [dict(row) for row in rows], "pending_count": pending_count})


def _admin_user_action(user_id: int, action: str):
    admin = current_user()
    now = utc_now()
    with connect_db() as db:
        row = auth_service.find_user(db, user_id)
        if row is None:
            return json_error("用户不存在", 404, "USER_NOT_FOUND")
        if row["role"] == "admin":
            return json_error("不能对管理员执行该操作", 400, "ADMIN_IMMUTABLE")
        {"approve": auth_service.approve_user,
         "reject": auth_service.reject_user,
         "disable": auth_service.disable_user}[action](db, user_id, approver_id=admin.id, now=now)
        audit(f"auth.{action}", "ok", f"user {user_id}", str(admin.id), db=db)
        pending_count = db.execute("SELECT COUNT(*) FROM auth_user WHERE status='pending'").fetchone()[0]
    return jsonify({"success": True, "id": user_id, "action": action, "pending_count": pending_count})


@app.post("/api/admin/users/<int:user_id>/approve")
@require_role("admin")
def api_admin_user_approve(user_id: int):
    return _admin_user_action(user_id, "approve")


@app.post("/api/admin/users/<int:user_id>/reject")
@require_role("admin")
def api_admin_user_reject(user_id: int):
    return _admin_user_action(user_id, "reject")


@app.post("/api/admin/users/<int:user_id>/disable")
@require_role("admin")
def api_admin_user_disable(user_id: int):
    return _admin_user_action(user_id, "disable")


@app.patch("/api/admin/policies/re0")
@require_role("admin")
def api_admin_policy_re0():
    """The one switch that lets members spend RE0 points. Default off; the
    audit row keeps who changed it and from what (plan §10.2)."""
    body = request_json()
    value = body.get("allow_member_re0_unlock")
    if not isinstance(value, bool):
        return json_error("allow_member_re0_unlock 必须为布尔值", 400, "BAD_REQUEST")
    before = allow_member_re0_unlock()
    setting_set("allow_member_re0_unlock", "1" if value else "0")
    audit("auth.policy.re0", "ok", f"{before} -> {value}", str(current_user().id))
    return jsonify({"success": True, "allow_member_re0_unlock": value})


@app.get("/api/status")
def api_status():
    verify = request.args.get("verify_115", "").strip().lower() in {"1", "true", "yes"}
    return jsonify({"success": True, **status_payload(verify_115=verify)})


def _parse_bool_setting(raw: object) -> bool | None:
    """Shared boolean-setting parsing for the wire format every
    ``tmdb_enrich_enabled``-shaped setting accepts: a real bool, or the
    literal strings "0"/"1"/"true"/"false" (case-insensitively). Returns
    ``None`` for anything else so the caller can 400. A bare
    ``bool(raw)`` would treat the non-empty string "0" as truthy."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"0", "1", "true", "false"}:
        return raw.strip().lower() in {"1", "true"}
    return None


@app.post("/api/settings")
def api_settings():
    body = request_json()
    allowed = {"hdhive_app_secret", "hdhive_client_id", "tmdb_api_key", "115_cookie"}
    cookie_check: dict[str, object] | None = None
    for key in allowed:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            secret_set(key, value.strip())
            if key == "115_cookie":
                # The read path prefers the administrator's *own* slot once a
                # scan has filled it, so a cookie pasted here must land there
                # too -- the same two-slot rule a scan follows -- or the page
                # reports "saved, valid" while every transfer keeps using the
                # older scanned value.
                _store_settings_cookie(value.strip())
                valid, label = remember_115_cookie_check(value.strip())
                cookie_check = {"valid": valid, "label": label}
    if isinstance(body.get("115_target_pid"), str):
        setting_set("115_target_pid", body["115_target_pid"].strip())
    tmdb_info: dict[str, object] | None = None
    if "tmdb_daily_budget" in body:
        budget_value = body["tmdb_daily_budget"]
        if isinstance(budget_value, bool) or not isinstance(budget_value, int) or not (1 <= budget_value <= 5000):
            return json_error("tmdb_daily_budget 必须是 1-5000 的整数", 400, "TMDB_DAILY_BUDGET_INVALID")
        setting_set("tmdb_daily_budget", str(budget_value))
        resolution = library_tmdb.resolve_daily_budget({"tmdb_daily_budget": setting_get("tmdb_daily_budget")}, os.environ)
        tmdb_info = {
            "configured_budget": resolution.configured,
            "effective_budget": resolution.effective,
            "cap_source": resolution.cap_source,
        }
        try:
            store = library_store.open_installed(LIBRARY_DB_PATH, None)
            library_tmdb.Budget(store.connect, _tmdb_lock_paths().budget, resolution.effective).sync(resolution.effective)
        except (
            library_store.LibraryNotInstalled,
            library_store.LibraryNotEncrypted,
            library_store.LibrarySchemaMismatch,
            library_store.LibraryIndexUnreadable,
        ):
            pass
    if "tmdb_enrich_enabled" in body:
        raw_enrich = body["tmdb_enrich_enabled"]
        # Wire format is the literal strings "0"/"1" (also "true"/"false",
        # case-insensitively); booleans are accepted too. A bare
        # `"1" if raw_enrich else "0"` would treat the non-empty string
        # "0" as truthy and always enable enrichment, so every accepted
        # spelling is enumerated explicitly instead.
        if isinstance(raw_enrich, bool):
            enrich_enabled = raw_enrich
        elif isinstance(raw_enrich, str) and raw_enrich.strip().lower() in {"0", "1", "true", "false"}:
            enrich_enabled = raw_enrich.strip().lower() in {"1", "true"}
        else:
            return json_error("tmdb_enrich_enabled 必须是布尔值或 0/1/true/false", 400, "BAD_REQUEST")
        setting_set("tmdb_enrich_enabled", "1" if enrich_enabled else "0")
    if "tvmaze_hint_enabled" in body:
        tvmaze_enabled = _parse_bool_setting(body["tvmaze_hint_enabled"])
        if tvmaze_enabled is None:
            return json_error("tvmaze_hint_enabled 必须是布尔值或 0/1/true/false", 400, "BAD_REQUEST")
        setting_set("tvmaze_hint_enabled", "1" if tvmaze_enabled else "0")
    if "linkcheck_enabled" in body:
        linkcheck_enabled = _parse_bool_setting(body["linkcheck_enabled"])
        if linkcheck_enabled is None:
            return json_error("linkcheck_enabled 必须是布尔值或 0/1/true/false", 400, "BAD_REQUEST")
        setting_set("linkcheck_enabled", "1" if linkcheck_enabled else "0")
    if "linkcheck_providers" in body:
        # w6-contract §5: the wire shape is nested ({code: {enabled,
        # daily_cap}}); stored internally as flat linkcheck_<code>_enabled/
        # linkcheck_<code>_daily_cap settings keys (matching every other
        # per-feature setting in this table).
        providers_body = body["linkcheck_providers"]
        if not isinstance(providers_body, dict):
            return json_error("linkcheck_providers 必须是对象", 400, "BAD_REQUEST")
        for code, cfg in providers_body.items():
            if code not in library_tmdb.LINK_CHECK_PROVIDERS:
                return json_error(f"linkcheck_providers 不支持的网盘代码：{code}", 400, "LINKCHECK_PROVIDER_INVALID")
            if not isinstance(cfg, dict):
                return json_error("linkcheck_providers 每项必须是对象", 400, "BAD_REQUEST")
            provider_enabled = None
            if "enabled" in cfg:
                provider_enabled = _parse_bool_setting(cfg["enabled"])
                if provider_enabled is None:
                    return json_error(f"linkcheck_providers.{code}.enabled 必须是布尔值或 0/1/true/false", 400, "BAD_REQUEST")
            cap_value = None
            if "daily_cap" in cfg:
                cap_value = cfg["daily_cap"]
                if isinstance(cap_value, bool) or not isinstance(cap_value, int) or not (0 <= cap_value <= 20000):
                    return json_error(f"linkcheck_providers.{code}.daily_cap 必须是 0-20000 的整数", 400, "LINKCHECK_DAILY_CAP_INVALID")
            # M2: a provider that ends up enabled (this request's own
            # "enabled", or -- when this request doesn't touch that field --
            # whatever is already stored) must have a real (>=1) daily cap,
            # this request's own value or whatever is already stored; a
            # disabled provider may keep 0 (it never spends any budget).
            effective_enabled = (
                provider_enabled if provider_enabled is not None
                else str(setting_get(f"linkcheck_{code}_enabled", "0")).strip().lower() in {"1", "true"}
            )
            if effective_enabled:
                if cap_value is not None:
                    effective_cap = cap_value
                else:
                    stored_cap = setting_get(f"linkcheck_{code}_daily_cap")
                    try:
                        effective_cap = int(stored_cap) if stored_cap not in (None, "") else 0
                    except (TypeError, ValueError):
                        effective_cap = 0
                if effective_cap < 1:
                    return json_error(
                        f"linkcheck_providers.{code}.daily_cap 已启用时必须 ≥ 1", 400, "LINKCHECK_DAILY_CAP_INVALID"
                    )
            if provider_enabled is not None:
                setting_set(f"linkcheck_{code}_enabled", "1" if provider_enabled else "0")
            if cap_value is not None:
                setting_set(f"linkcheck_{code}_daily_cap", str(cap_value))
    if "cloud_download_enabled" in body:
        cloud_enabled = _parse_bool_setting(body["cloud_download_enabled"])
        if cloud_enabled is None:
            return json_error("cloud_download_enabled 必须是布尔值或 0/1/true/false", 400, "BAD_REQUEST")
        setting_set("cloud_download_enabled", "1" if cloud_enabled else "0")
    for key, lo, hi in (("cloud_download_daily_cap", 0, _CLOUD_DAILY_CAP_MAX), ("cloud_download_per_submit_cap", 1, _CLOUD_PER_SUBMIT_CAP_MAX)):
        if key in body:
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
                return json_error(f"{key} 必须是 {lo}-{hi} 的整数", 400, "CLOUD_DOWNLOAD_SETTING_INVALID")
            setting_set(key, str(value))
    if "re0_streaming_top_sources" in body:
        raw_sources = body["re0_streaming_top_sources"]
        if not isinstance(raw_sources, str) or len(raw_sources) > 200:
            return json_error("re0_streaming_top_sources 必须是不超过 200 字符的字符串", 400, "RE0_SETTING_INVALID")
        setting_set("re0_streaming_top_sources", ",".join(":".join(t) for t in re0_sync.parse_top_sources(raw_sources)))
    for key, lo, hi in (("re0_daily_request_cap", 1, re0_sync.SAFE_DAILY_CAP_MAX), ("re0_min_interval_ms", re0_sync.SAFE_MIN_INTERVAL_MS, 60000)):
        if key in body:
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
                return json_error(f"{key} 必须是 {lo}-{hi} 的整数", 400, "RE0_SETTING_INVALID")
            setting_set(key, str(value))
            _re0_client_reset()
    library_keys = {
        "115_target_pid", "tmdb_daily_budget", "tmdb_enrich_enabled", "tvmaze_hint_enabled",
        "linkcheck_enabled", "linkcheck_providers",
        "cloud_download_enabled", "cloud_download_daily_cap", "cloud_download_per_submit_cap",
        "re0_daily_request_cap", "re0_min_interval_ms", "re0_streaming_top_sources",
    }
    audit("settings.update", "success", ",".join(sorted(k for k in body if k in allowed or k in library_keys)), actor_id())
    payload: dict[str, object] = {"success": True, "message": "设置已保存"}
    if cookie_check is not None:
        payload["cookie_check"] = cookie_check
    if tmdb_info is not None:
        payload["tmdb"] = tmdb_info
    return jsonify(payload)


_LIBRARY_SORTS = {"relevance", "year_desc", "year_asc", "links_desc"}
_LIBRARY_TYPES = {"all", "movie", "tv", "unknown"}
_LIBRARY_YEAR_RE = re.compile(r"^(\d{4})(?:-(\d{4}))?$")


# ---------------------------------------------------------------------------
# RE0 federated search (spec docs/claude-re0-resource-sync-and-tv-follow-handoff-20260909.md §10.4)
# ---------------------------------------------------------------------------

_RE0_CLIENTS: dict[str, "re0_sync.Re0Client"] = {}
_RE0_CLIENT_LOCK = threading.Lock()
_RE0_DIRECT_PATH_RE = re.compile(r"^/api/open/[A-Za-z0-9_/\-]{1,80}$")


def _re0_settings() -> dict:
    def _int(name: str, env: str, default: int, lo: int, hi: int) -> int:
        raw = setting_get(name) or os.getenv(env) or ""
        try:
            value = int(raw) if raw else default
        except ValueError:
            value = default
        return min(max(value, lo), hi)
    direct = (setting_get("re0_direct_search_path") or os.getenv("RE0_DIRECT_SEARCH_PATH") or "").strip()
    if direct and not _RE0_DIRECT_PATH_RE.match(direct):
        direct = ""
    return {
        "daily_cap": _int("re0_daily_request_cap", "RE0_SYNC_DAILY_REQUEST_CAP", re0_sync.DEFAULT_DAILY_CAP, 1, re0_sync.SAFE_DAILY_CAP_MAX),
        "min_interval_ms": _int("re0_min_interval_ms", "RE0_SYNC_MIN_INTERVAL_MS", re0_sync.DEFAULT_MIN_INTERVAL_MS, re0_sync.SAFE_MIN_INTERVAL_MS, 60000),
        "direct_search_path": direct,
    }


def _re0_conn_factory() -> sqlite3.Connection:
    conn = sqlite3.connect(LIBRARY_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _re0_client() -> "re0_sync.Re0Client":
    """One paced/budgeted client per library path (so a per-test library
    never inherits another's blocked state). Credential callables are
    late-bound to the module's own helpers -- never copied."""
    key = str(LIBRARY_DB_PATH)
    with _RE0_CLIENT_LOCK:
        client = _RE0_CLIENTS.get(key)
        if client is None:
            settings = _re0_settings()
            client = re0_sync.Re0Client(
                request=lambda *a, **k: requests.request(*a, **k),
                api_key_provider=lambda: config_value("hdhive_app_secret", "HDHIVE_APP_SECRET"),
                token_provider=lambda: valid_hdhive_access_token(),
                refresh_token=lambda: refresh_hdhive_token(),
                conn_factory=_re0_conn_factory,
                min_interval_ms=settings["min_interval_ms"],
                daily_cap=settings["daily_cap"],
                base=HDHIVE_BASE,
            )
            _RE0_CLIENTS[key] = client
        return client


def _re0_client_reset() -> None:
    with _RE0_CLIENT_LOCK:
        _RE0_CLIENTS.clear()


def _re0_slug_salt(conn: sqlite3.Connection) -> str:
    salt = re0_sync.state_get(conn, "slug_salt")
    if not salt:
        salt = secrets.token_hex(16)
        re0_sync.state_set(conn, "slug_salt", salt, utc_now())
        conn.commit()
    return salt


def _re0_image_url(path: str | None, kind: str) -> str | None:
    return _tmdb_image_url(path, LIBRARY_BACKDROP_SIZE if kind == "backdrop" else LIBRARY_POSTER_SIZE)


_RE0_STOP_CLASSES = {"rate_limited", "reauth_required", "refresh_unavailable", "scope_denied", "user_level_denied", "quota_exhausted",
                     "missing_credentials", "network_error", "upstream_5xx", "invalid_json"}


def _re0_tmdb_candidates(q: str, kinds: list[str], year: int | None) -> tuple[list[dict], str | None]:
    """Title -> TMDB ids through the app's own TmdbClient (cache + budget +
    shared rate limiter). Returns ``(candidates, error_status)``."""
    client = _library_client_factory(fast=True)
    if client is None:
        return [], "tmdb_unavailable"
    candidates: list[dict] = []
    for kind in kinds:
        try:
            entry = client.search(kind, q, year=year, language="zh-CN")
            if entry.status == "empty" or (entry.status != "ok" and not entry.payload):
                fallback = client.search(kind, q, year=year, language="en-US")
                if fallback.status == "ok" and fallback.payload:
                    entry = fallback
        except library_tmdb.BudgetExhausted:
            return candidates, "tmdb_budget_exhausted"
        except Exception as exc:  # noqa: BLE001 -- never leak the key/URL; class name only
            LOG.warning("re0 search tmdb lookup failed error=%s", type(exc).__name__)
            return candidates, "tmdb_unavailable"
        if entry.status != "ok":
            if entry.error_class and entry.status != "empty":
                return candidates, "tmdb_unavailable"
            continue
        for row in entry.payload or []:
            cand = re0_sync.tmdb_result_to_candidate(kind, row)
            if cand is not None:
                candidates.append(cand)
    return candidates, None


def _re0_refresh_cached_catalog_refs(
    store,
    refs: list[tuple[str, int]],
    *,
    providers: tuple[str, ...],
    salt: str | None,
    now: int,
    max_requests: int = 5,
) -> dict:
    """Backfill resources for cached catalog identities that have no rows.

    The catalog cache stores TMDB identities, while ``re0_resource`` is
    populated separately.  A fresh cache entry can therefore be structurally
    valid but still render no RE0 candidates (for example after a process
    restart or a previous interrupted fetch).  Refresh only those gaps,
    bounded to a handful of upstream calls, before returning the catalog.
    """
    pending: list[tuple[str, int, str | None, int | None]] = []
    conn = store.connect(readonly=True)
    try:
        for media_type, tmdb_id in refs:
            row = conn.execute(
                "SELECT last_fetched_at, resources_retry_at, title, local_media_id FROM re0_media_projection "
                "WHERE media_type=? AND tmdb_id=?",
                (media_type, tmdb_id),
            ).fetchone()
            if row is None:
                pending.append((media_type, tmdb_id, None, None))
                continue
            # Use the unfiltered projection count to decide freshness.  A
            # provider facet with no matching rows is a legitimate empty
            # result and should not cause a refresh loop on every page load.
            summary = re0_sync.resource_summary(conn, media_type, tmdb_id, ())
            fetched_at = int(row["last_fetched_at"] or 0)
            if fetched_at and now - fetched_at < re0_sync.RESOURCES_TTL_SECONDS:
                if summary["candidate_count"]:
                    continue
                if int(row["resources_retry_at"] or 0) > now:
                    continue
            pending.append((media_type, tmdb_id, row["title"], row["local_media_id"]))
    finally:
        conn.close()

    if not pending:
        return {"refreshed": 0, "error_class": None, "message": None, "retry_after": None}

    if salt is None:
        conn = store.connect()
        try:
            salt = _re0_slug_salt(conn)
        finally:
            conn.close()
    re0 = _re0_client()
    refreshed = 0
    for media_type, tmdb_id, title, local_media_id in pending[:max(1, max_requests)]:
        result = re0.get(f"/api/open/resources/{media_type}/{tmdb_id}")
        if result.ok:
            raw_items = result.data if isinstance(result.data, list) else (
                (result.data or {}).get("items") if isinstance(result.data, dict) else []
            )
            re0_sync.record_items(
                store, media_type, tmdb_id, raw_items or [], media_id=local_media_id,
                media_title=title or f"TMDB {tmdb_id}", salt=salt, now=now,
            )
            refreshed += 1
        elif result.error_class == "upstream_4xx":
            # A completed upstream response (including an empty/404 result)
            # still gets a fetch timestamp, matching the normal search path
            # and preventing a hot-loop on every page load.
            pass
        else:
            return {
                "refreshed": refreshed,
                "error_class": result.error_class,
                "message": result.message,
                "retry_after": result.retry_after,
            }
        conn = store.connect(readonly=True)
        try:
            has_candidates = bool(re0_sync.resource_summary(conn, media_type, tmdb_id, ())['candidate_count'])
        finally:
            conn.close()
        conn = store.connect()
        try:
            conn.execute(
                "UPDATE re0_media_projection SET last_fetched_at=?, resources_retry_at=?, last_error_class=NULL, updated_at=? "
                "WHERE media_type=? AND tmdb_id=?",
                (now, None if has_candidates else now + re0_sync.NEGATIVE_TTL_SECONDS, now, media_type, tmdb_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {"refreshed": refreshed, "error_class": None, "message": None, "retry_after": None}


def _re0_refresh_detail_ref(
    media_type: str,
    tmdb_id: int,
    *,
    local_media_id: int | None = None,
    title: str | None = None,
    original_title: str | None = None,
    year: int | None = None,
    overview: str | None = None,
    poster_path: str | None = None,
    backdrop_path: str | None = None,
) -> dict:
    """Ensure one detail view has a populated RE0 projection before render.

    Search results can legitimately arrive from a cached catalog while the
    corresponding ``re0_resource`` rows are still missing (for example after
    an interrupted fetch).  A detail request is the user's explicit intent to
    inspect that identity, so spend at most one normal, budgeted RE0 request to
    repair the gap synchronously.  The helper is deliberately best-effort:
    browsing a local media must remain available when the encryption key or
    upstream service is temporarily unavailable.
    """
    if media_type not in ("movie", "tv") or int(tmdb_id or 0) <= 0:
        return {"refreshed": 0, "error_class": None, "message": None, "retry_after": None}
    write_store, error = _library_store_or_error(True)
    if error:
        LOG.warning("re0 detail refresh skipped type=%s id=%s reason=library_key_unavailable", media_type, tmdb_id)
        return {"refreshed": 0, "error_class": "library_key_unavailable", "message": None, "retry_after": None}
    try:
        # A local media can be opened directly before any federated search has
        # created its projection row.  Seed that identity first so the refresh
        # timestamp written below also participates in the normal TTL gate.
        if local_media_id is not None:
            re0_sync.upsert_projection(
                write_store,
                media_type,
                int(tmdb_id),
                title=title or f"TMDB {tmdb_id}",
                original_title=original_title,
                year=year,
                overview=overview,
                poster_path=poster_path,
                backdrop_path=backdrop_path,
                ratings={},
                now=utc_now(),
                local_media_id=int(local_media_id),
            )
        return _re0_refresh_cached_catalog_refs(
            write_store, [(media_type, int(tmdb_id))], providers=(), salt=None, now=utc_now(), max_requests=1,
        )
    except Exception as exc:  # noqa: BLE001 -- detail browsing is best-effort
        LOG.warning("re0 detail refresh failed type=%s id=%s error=%s", media_type, tmdb_id, type(exc).__name__)
        return {"refreshed": 0, "error_class": type(exc).__name__, "message": None, "retry_after": None}


@app.get("/api/library/search/re0")
def api_library_search_re0():
    store, error = _library_store_or_error(True)
    if error:
        return error
    q = (request.args.get("q") or "").strip()
    if not q or len(q) > 120:
        return json_error("q 必须为 1-120 字符", 400, "LIBRARY_SEARCH_Q_INVALID")
    media_type = request.args.get("type", "all")
    if media_type not in _LIBRARY_TYPES:
        return json_error("type 参数不正确", 400, "LIBRARY_SEARCH_TYPE_INVALID")
    year_raw = request.args.get("year", "")
    year = None
    if year_raw:
        if not _LIBRARY_YEAR_RE.fullmatch(year_raw):
            return json_error("year 参数格式不正确", 400, "LIBRARY_SEARCH_YEAR_INVALID")
        year = int(year_raw) if "-" not in year_raw else None
    provider_raw = request.args.get("provider", "")
    providers = tuple(p for p in provider_raw.split(",") if p) if provider_raw else ()
    if any(code not in library_normalize.PROVIDERS for code in providers):
        return json_error("provider 参数不正确", 400, "LIBRARY_PROVIDER_INVALID")

    settings = _re0_settings()
    mode = "direct" if settings["direct_search_path"] else "tmdb"
    shortcut = re.fullmatch(r"tmdb:(\d{1,10})", q.strip().lower())
    now = utc_now()
    qhash = re0_sync.query_hash(q, media_type, year)
    cache_key = f"{mode}:{qhash}"
    if request.args.get("catalog") == "1":
        cache_key = "catalog-v1:" + cache_key
    filters = {"provider": list(providers)}
    kinds = ["movie", "tv"] if media_type == "all" else [media_type]

    conn = store.connect()
    try:
        salt = _re0_slug_salt(conn)
        cached = re0_sync.cache_get(conn, cache_key)
    finally:
        conn.close()

    def _respond(status: str, refs: list, *, cached_at: int | None, message: str | None = None, retry_after: int | None = None,
                 origin: str | None = None, reason: str | None = None):
        re0_sync.catalog_projections(store, refs, charmap=_library_charmap(store))
        conn = store.connect(readonly=True)
        try:
            items = re0_sync.projection_items(conn, refs, providers, _re0_image_url)
        finally:
            conn.close()
        payload = {
            "success": True,
            "remote": {
                "mode": mode, "status": status, "origin": origin, "reason": reason, "cached_at": iso(cached_at), "message": message,
                "retry_after": retry_after, "budget": _re0_client().budget_status(), "items": items,
            },
        }
        if request.args.get("catalog") == "1":
            # Both lanes finish in the SAME local name index, with one set of
            # filters, relevance tiers, deduplication and pagination.
            response = app.make_response(api_library_search())
            if response.status_code != 200:
                return response
            payload["catalog"] = response.get_json()
        return jsonify(payload)

    if cached is not None:
        refs = [tuple(r) for r in json.loads(cached["candidate_ids_json"] or "[]")]
        try:
            cached_origin = (json.loads(cached["filter_json"] or "{}") or {}).get("origin")
        except ValueError:
            cached_origin = None
        if cached["retry_after_until"] and int(cached["retry_after_until"]) > now:
            return _respond(cached["error_class"] or "rate_limited", refs, cached_at=cached["fetched_at"],
                            message="RE0 限流冷却中", retry_after=int(cached["retry_after_until"]) - now, origin=cached_origin)
        if cached["expires_at"] and int(cached["expires_at"]) > now and cached["status"] in ("fresh", "no_candidates"):
            if request.args.get("catalog") == "1" and refs:
                repair = _re0_refresh_cached_catalog_refs(
                    store, refs, providers=providers, salt=salt, now=now,
                )
                if repair["error_class"]:
                    return _respond(
                        repair["error_class"], refs, cached_at=cached["fetched_at"],
                        message=repair["message"], retry_after=repair["retry_after"], origin=cached_origin,
                    )
            return _respond("cached" if cached["status"] == "fresh" else "no_candidates", refs, cached_at=cached["fetched_at"], origin=cached_origin)

    re0 = _re0_client()
    local_candidates: list[dict] = []
    raw_candidates: list[dict] = []
    source_error = message = None
    if shortcut:
        # Explicit TMDB id (spec §10.4.2 item 4): no TMDB search at all; the
        # projection worker fills the title/metadata later.
        origin = "tmdb_id"
        conn = store.connect(readonly=True)
        try:
            for kind in kinds:
                existing = re0_sync.projection_row(conn, kind, int(shortcut.group(1)))
                raw_candidates.append({"media_type": kind, "tmdb_id": int(shortcut.group(1)), "title": existing["title"] if existing else f"TMDB {shortcut.group(1)}",
                                       "original_title": None, "year": None, "poster_path": None, "backdrop_path": None, "overview": None, "ratings": {}, "votes": 0})
        finally:
            conn.close()
    else:
        # Round 23: a library media that is already matched (exact + tmdb_id)
        # is the candidate -- no TMDB title search for it, so an exhausted
        # TMDB budget cannot hide what the library already knows.
        charmap = _library_charmap(store)
        conn = store.connect(readonly=True)
        try:
            local_candidates = re0_sync.local_exact_candidates(conn, q, kinds=kinds, year=year, charmap=charmap)
        finally:
            conn.close()
        origin = "local" if local_candidates else mode
        if mode == "direct":
            result = re0.get(settings["direct_search_path"], params={"q": q, "type": media_type})
            if result.ok:
                rows = result.data.get("items") if isinstance(result.data, dict) else result.data
                raw_candidates = [c for c in (re0_sync.direct_result_to_candidate(r) for r in (rows or [])) if c and c["media_type"] in kinds]
            elif local_candidates:
                source_error, message = result.error_class or "upstream_4xx", result.message
            else:
                return _respond(result.error_class or "upstream_4xx", [], cached_at=None, message=result.message, retry_after=result.retry_after, origin=origin)
        elif not local_candidates or request.args.get("catalog") == "1":
            raw_candidates, source_error = _re0_tmdb_candidates(q, kinds, year)
    candidates = re0_sync.merge_candidates(local_candidates, re0_sync.select_candidates(raw_candidates, q, year))
    if not candidates:
        if source_error:
            # A TMDB budget/outage answer is not "no results": nothing is
            # cached, so the next search retries once TMDB is back.
            return _respond(source_error, [], cached_at=None, origin=origin)
        conn = store.connect()
        try:
            re0_sync.cache_put(conn, cache_key, qhash, media_type, filters=dict(filters, origin=origin), candidate_refs=[],
                               status="no_candidates", now=now, ttl=re0_sync.NEGATIVE_TTL_SECONDS)
            conn.commit()
        finally:
            conn.close()
        return _respond("no_candidates", [], cached_at=now, origin=origin)

    refs = []
    conn = store.connect()
    try:
        for cand in candidates:
            if cand.get("local_media_id") is None:
                cand["local_media_id"] = re0_sync.local_media_id_for(conn, cand["media_type"], cand["tmdb_id"])
            refs.append((cand["media_type"], cand["tmdb_id"]))
    finally:
        conn.close()
    # Local media first (spec §10.4.2 item 5), then the rest in rank order.
    candidates.sort(key=lambda c: 0 if c["local_media_id"] else 1)
    for cand in candidates:
        re0_sync.upsert_projection(
            store, cand["media_type"], cand["tmdb_id"], title=cand["title"], original_title=cand.get("original_title"), year=cand["year"],
            overview=cand["overview"], poster_path=cand["poster_path"], backdrop_path=cand["backdrop_path"], ratings=cand["ratings"], now=now,
            local_media_id=cand["local_media_id"],
        )

    status, retry_after, stop_class, reason = "fresh", None, None, None
    for cand in candidates:
        conn = store.connect(readonly=True)
        try:
            row = conn.execute("SELECT last_fetched_at, title FROM re0_media_projection WHERE media_type=? AND tmdb_id=?",
                               (cand["media_type"], cand["tmdb_id"])).fetchone()
        finally:
            conn.close()
        if row and row["last_fetched_at"] and now - int(row["last_fetched_at"]) < re0_sync.RESOURCES_TTL_SECONDS:
            continue
        result = re0.get(f"/api/open/resources/{cand['media_type']}/{cand['tmdb_id']}")
        if result.ok:
            raw_items = result.data if isinstance(result.data, list) else ((result.data or {}).get("items") if isinstance(result.data, dict) else [])
            report = re0_sync.record_items(store, cand["media_type"], cand["tmdb_id"], raw_items or [], media_id=cand["local_media_id"],
                                           media_title=cand["title"], salt=salt, now=now)
            LOG.info("re0 search tmdb_id=%s type=%s items=%s new=%s materialized=%s", cand["tmdb_id"], cand["media_type"],
                     report["remote_items"], report["new"], report["materialized"])
        elif result.error_class == "upstream_4xx":
            LOG.info("re0 search tmdb_id=%s type=%s status=%s code=%s", cand["tmdb_id"], cand["media_type"], result.status, result.code)
        else:
            stop_class, message, retry_after = result.error_class, result.message, result.retry_after
            LOG.warning("re0 search stopped error=%s status=%s", stop_class, result.status)
            break
        conn = store.connect()
        try:
            conn.execute("UPDATE re0_media_projection SET last_fetched_at=?, last_error_class=NULL, updated_at=? WHERE media_type=? AND tmdb_id=?",
                         (now, now, cand["media_type"], cand["tmdb_id"]))
            conn.commit()
        finally:
            conn.close()

    conn = store.connect()
    try:
        if stop_class:
            until = now + retry_after if stop_class == "rate_limited" and retry_after else None
            re0_sync.cache_put(conn, cache_key, qhash, media_type, filters=dict(filters, origin=origin), candidate_refs=refs, status="error", now=now,
                               ttl=re0_sync.NEGATIVE_TTL_SECONDS if not until else max(retry_after or 0, 1), retry_after_until=until, error_class=stop_class)
            status = stop_class
        elif source_error:
            # Local matches answered, the remote candidate source did not:
            # say so, and do not cache -- the next search retries the source.
            status, reason = "partial", source_error
        else:
            re0_sync.cache_put(conn, cache_key, qhash, media_type, filters=dict(filters, origin=origin), candidate_refs=refs, status="fresh", now=now,
                               ttl=re0_sync.SEARCH_TTL_SECONDS)
        conn.commit()
    finally:
        conn.close()
    return _respond(status, refs, cached_at=now, message=message, retry_after=retry_after, origin=origin, reason=reason)


@app.get("/api/library/search")
def api_library_search():
    store, error = _library_store_or_error(False)
    if error:
        return error

    q = request.args.get("q", "")
    if len(q) > 120:
        return json_error("q 不能超过 120 字符", 400, "LIBRARY_SEARCH_Q_TOO_LONG")

    media_type = request.args.get("type", "all")
    if media_type not in _LIBRARY_TYPES:
        return json_error("type 参数不正确", 400, "LIBRARY_SEARCH_TYPE_INVALID")

    year_raw = request.args.get("year", "")
    year_from = year_to = None
    if year_raw:
        match = _LIBRARY_YEAR_RE.fullmatch(year_raw)
        if not match:
            return json_error("year 参数格式不正确", 400, "LIBRARY_SEARCH_YEAR_INVALID")
        year_from = int(match.group(1))
        year_to = int(match.group(2)) if match.group(2) else year_from

    provider_raw = request.args.get("provider", "")
    providers = tuple(p for p in provider_raw.split(",") if p) if provider_raw else ()
    # T17 §14.1: an invalid provider code is a 400, never a silent
    # "no matches"/fall-back-to-all -- the client asked to filter and got
    # something it didn't recognise.
    if any(code not in library_normalize.PROVIDERS for code in providers):
        return json_error("provider 参数不正确", 400, "LIBRARY_PROVIDER_INVALID")
    quality = request.args.get("quality") or None
    hdr = request.args.get("hdr") or None

    season_raw = request.args.get("season", "")
    season = None
    if season_raw:
        if not season_raw.isdigit():
            return json_error("season 参数格式不正确", 400, "LIBRARY_SEARCH_SEASON_INVALID")
        season = int(season_raw)

    genre = request.args.get("genre") or None
    if genre and len(genre) > 40:
        return json_error("genre 不能超过 40 字符", 400, "LIBRARY_SEARCH_GENRE_INVALID")

    source = request.args.get("source") or None
    if source and len(source) > 40:
        return json_error("source 不能超过 40 字符", 400, "LIBRARY_SEARCH_SOURCE_INVALID")

    has_backdrop_raw = request.args.get("has_backdrop", "")
    has_backdrop = False
    if has_backdrop_raw:
        if has_backdrop_raw not in {"0", "1"}:
            return json_error("has_backdrop 参数格式不正确", 400, "LIBRARY_SEARCH_HAS_BACKDROP_INVALID")
        has_backdrop = has_backdrop_raw == "1"

    complete_season_raw = request.args.get("complete_season", "")
    complete_season = False
    if complete_season_raw:
        if complete_season_raw not in {"0", "1"}:
            return json_error("complete_season 参数格式不正确", 400, "LIBRARY_SEARCH_COMPLETE_SEASON_INVALID")
        complete_season = complete_season_raw == "1"

    include_deleted = request.args.get("include_deleted", "0") == "1"

    sort = request.args.get("sort", "relevance")
    if sort not in _LIBRARY_SORTS:
        return json_error("sort 参数不正确", 400, "LIBRARY_SEARCH_SORT_INVALID")

    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", "25"))
    except ValueError:
        return json_error("page/page_size 必须为整数", 400, "LIBRARY_SEARCH_PAGE_INVALID")
    if page < 1:
        return json_error("page 超出范围", 400, "LIBRARY_SEARCH_PAGE_INVALID")
    if not (1 <= page_size <= 50):
        return json_error("page_size 超出范围", 400, "LIBRARY_SEARCH_PAGE_SIZE_INVALID")

    filters = library_search.Filters(
        media_type=None if media_type == "all" else media_type,
        year_from=year_from,
        year_to=year_to,
        providers=providers,
        quality=quality,
        hdr=hdr,
        season=season,
        include_deleted=include_deleted,
        genre=genre,
        source=source,
        has_backdrop=has_backdrop,
        complete_season=complete_season,
        include_re0=bool(q.strip()),
    )
    if q:
        page_result = library_search.search(
            store, q, filters, sort=sort, page=page, page_size=page_size, charmap=_library_charmap(store)
        )
    else:
        page_result = store.browse(filters, sort=sort, page=page, page_size=page_size)

    items = []
    for item in page_result.items:
        item = dict(item)
        item["poster_url"] = _tmdb_image_url(item.pop("poster_path", None), LIBRARY_POSTER_SIZE)
        item["backdrop_url"] = _tmdb_image_url(item.pop("backdrop_path", None), LIBRARY_BACKDROP_SIZE)
        items.append(item)

    return jsonify(
        {
            "success": True,
            "total": page_result.total,
            "page": page_result.page,
            "page_size": page_result.page_size,
            "query": {"q": q, "type": media_type},
            "interpreted": page_result.interpreted,
            "items": items,
        }
    )


# ---------------------------------------------------------------------------
# T16 §2: daily recommendations -- a read-only, deterministic "today's
# picks" rail. The business day is always the configured Asia/Shanghai
# timezone (never the browser's local time); ranking is a SHA-256
# tie-break over (day, algorithm version, media_id) -- no
# random.shuffle()/Math.random() anywhere, and no TMDB/HDHive calls. A
# small per-process cache (keyed by (day, version, limit)) makes repeat
# requests cheap; recomputing after the cache is cleared must yield the
# exact same order, since the tie-break is a pure function of its inputs.
# ---------------------------------------------------------------------------

_RECOMMENDATION_TIMEZONE = ZoneInfo("Asia/Shanghai")
_RECOMMENDATION_ALGO_VERSION = "1"
# Round 17: the home banner (`placement=hero`) ranks its own, stricter pool
# (library_store.eligible_hero_media_ids) under its own version string, so
# its tie-break and cache entries never collide with the rail's.
_HERO_ALGO_VERSION = "hero-rated-2day-1"
_RECOMMENDATION_PLACEMENTS = {"": "today_recommendations", "hero": "today_hero"}
_RECOMMENDATION_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RECOMMENDATION_DEFAULT_LIMIT = 12
_RECOMMENDATION_MAX_LIMIT = 24

# (db_path, installed_at, day, algorithm_version, limit) -> {"ids": [...], "fallback": bool}
_RECOMMENDATION_CACHE: dict[tuple, dict] = {}


def _recommendation_tie(day: str, media_id: int, version: str = _RECOMMENDATION_ALGO_VERSION) -> str:
    payload = f"{day}|{version}|{media_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _recommendation_selection(
    store: "library_store.LibraryStore", day: str, limit: int, placement: str = "",
) -> dict:
    # The cache key includes the installed index's own identity (path +
    # install timestamp) so a later ``--library-install``/rollback that
    # replaces the index -- same day, same limit -- can never keep serving
    # a stale selection computed against the previous index's media ids.
    # The algorithm version (distinct per placement) is part of the key too,
    # so bumping it invalidates every cached selection of that placement.
    version = _HERO_ALGO_VERSION if placement == "hero" else _RECOMMENDATION_ALGO_VERSION
    if placement == "hero":
        day = _hero_rotation_date(day)
    cache_key = (str(store.db_path), store.meta_get("installed_at"), day, version, limit)
    cached = _RECOMMENDATION_CACHE.get(cache_key)
    if cached is not None:
        if placement != "hero" or (cached["ids"] and
                set(store.eligible_hero_media_ids(cached["ids"])) == set(cached["ids"])):
            return cached
        # Only re-read the cached IDs normally. Rebuild the pool when a
        # featured title loses its link/score/match, or an empty pool grows.
        _RECOMMENDATION_CACHE.pop(cache_key, None)

    if placement == "hero":
        eligible_ids = store.eligible_hero_media_ids()
    else:
        eligible_ids = store.eligible_recommendation_media_ids()
    if eligible_ids:
        ranked = sorted(eligible_ids, key=lambda media_id: _recommendation_tie(day, media_id, version))
        selection = {"ids": ranked[:limit], "fallback": False}
    elif placement == "hero":
        # The banner needs a backdrop and an overview; nothing outside the
        # strict pool can render there, so the fallback is "no banner".
        selection = {"ids": [], "fallback": True}
    else:
        selection = {"ids": store.fallback_recommendation_media_ids(limit), "fallback": True}

    _RECOMMENDATION_CACHE[cache_key] = selection
    return selection


def _hero_rotation_date(day: str) -> str:
    """Stable two-calendar-day editions, aligned to Asia/Shanghai dates."""
    ordinal = datetime.strptime(day, "%Y-%m-%d").toordinal()
    return datetime.fromordinal(max(1, ordinal - ordinal % 2)).date().isoformat()


@app.post("/api/library/cloud-download")
def api_cloud_download_submit():
    """Push one resource group's ED2K/magnet links to 115 cloud download
    (spec §5). Order of checks: switch → link eligibility → target pid →
    dedupe → daily cap → monthly quota → lock → serial chunked submit."""
    store, error = _library_store_or_error(True)
    if error:
        return error
    settings = _cloud_settings_dict()
    if not settings["enabled"]:
        return json_error("115 云下载未开启，请先在设置中启用", 403, "CLOUD_DOWNLOAD_DISABLED")
    gate = _cloud_download_gate()
    if gate is not None:
        return gate
    deadline = g.request_started + _115_REQUEST_DEADLINE_SECONDS
    body = request_json()
    raw_ids = body.get("resource_link_ids")
    if not isinstance(raw_ids, list) or not raw_ids or not all(isinstance(i, str) and i.strip() for i in raw_ids):
        return json_error("请提供 resource_link_ids", 400, "BAD_REQUEST")
    public_ids = list(dict.fromkeys(i.strip() for i in raw_ids))
    if len(public_ids) > settings["per_submit_cap"]:
        return json_error(f"单次最多提交 {settings['per_submit_cap']} 条链接", 400, "CLOUD_DOWNLOAD_PER_SUBMIT_CAP")

    items: list[dict] = []
    group_ids: set[int] = set()
    for public_id in public_ids:
        row = store.link_by_public_id(public_id)
        if row is None:
            return json_error("未找到该链接", 404, "LINK_NOT_FOUND")
        label = row["url_label"] or ""
        if row["provider"] != "ed2k" or row["deleted_at_source"] is not None:
            return json_error(f"该链接不支持云下载：{label}", 400, "LINK_NOT_CLOUD_DOWNLOADABLE")
        try:
            url, _ = store.reveal(public_id)
        except library_store.LibraryKeyUnavailable:
            return json_error("主密钥不可用", 503, "LIBRARY_KEY_UNAVAILABLE")
        if not (url or "").strip().lower().startswith(("ed2k://", "magnet:")):
            return json_error(f"该链接不支持云下载：{label}", 400, "LINK_NOT_CLOUD_DOWNLOADABLE")
        group_ids.add(int(row["group_id"]))
        items.append({"link_id": public_id, "label": label, "url": url.strip()})
    if len(group_ids) != 1:
        return json_error("一次只能提交同一资源组的链接", 400, "CLOUD_DOWNLOAD_MIXED_GROUPS")
    group_id = group_ids.pop()

    pid, target_error = _resolve_115_target_pid(body, deadline)
    if target_error:
        return target_error
    if not _cloud_dedupe_check(public_ids, pid):
        return json_error("这些链接刚刚已提交过，请稍后再试", 409, "CLOUD_DOWNLOAD_DUPLICATE")

    count = len(items)
    if settings["daily_cap"] and _cloud_today_submitted() + count > settings["daily_cap"]:
        _cloud_dedupe_clear(public_ids, pid)
        return json_error(f"今日云下载提交已达上限 {settings['daily_cap']} 条", 429, "CLOUD_DOWNLOAD_DAILY_CAP")
    quota, quota_error = _cloud_quota(deadline=deadline)
    if quota is None:
        _cloud_dedupe_clear(public_ids, pid)
        return json_error("无法读取 115 云下载配额：" + quota_error, 502, "CLOUD_DOWNLOAD_QUOTA_UNAVAILABLE")
    surplus = int(quota.get("surplus") or 0)
    if surplus < count:
        _cloud_dedupe_clear(public_ids, pid)
        return jsonify({
            "success": False, "code": "CLOUD_DOWNLOAD_QUOTA_EXHAUSTED",
            "message": f"本月云下载配额不足：剩余 {surplus} 条，本次需要 {count} 条", "quota_surplus": surplus,
        }), 409
    if not _CLOUD_DOWNLOAD_LOCK.acquire(blocking=False):
        _cloud_dedupe_clear(public_ids, pid)
        return json_error("另一批云下载正在提交，请稍后再试", 409, "CLOUD_DOWNLOAD_BUSY")
    request_id = secrets.token_hex(8)
    started = time.monotonic()
    try:
        results, stop_error = _cloud_submit_chunks(items, pid, deadline)
    finally:
        _CLOUD_DOWNLOAD_LOCK.release()

    ok = [r for r in results if r["state"] == "ok"]
    failed = sum(1 for r in results if r["state"] == "failed")
    not_submitted = [r["link_id"] for r in results if r["state"] == "not_submitted"]
    if not_submitted:
        _cloud_dedupe_clear(not_submitted, pid)
    if ok:
        legacy_owner = _writes_legacy_cloud_task()
        conn = store.connect(readonly=True)
        try:
            origin = conn.execute(
                "SELECT g.media_id AS media_id, m.title_zh AS title FROM resource_group g JOIN media m ON m.id = g.media_id WHERE g.id = ?",
                (group_id,),
            ).fetchone()
        finally:
            conn.close()
        now = utc_now()
        actor = actor_id()
        with connect_db() as db:
            for result in ok:
                # Phase 6: the note about where a task came from belongs to
                # the user who submitted it. Two users may hold the same
                # info_hash independently -- the unique key is the pair.
                db.execute(
                    "INSERT INTO user_cloud_download_task(user_id, info_hash, source_kind, media_id, display_title, "
                    "group_id, link_label, submitted_at, last_seen_at, state) VALUES(?,?,?,?,?,?,?,?,NULL,NULL) "
                    "ON CONFLICT(user_id, info_hash) DO UPDATE SET submitted_at=excluded.submitted_at, "
                    "media_id=excluded.media_id, display_title=excluded.display_title, "
                    "group_id=excluded.group_id, link_label=excluded.link_label",
                    (
                        (current_user().id if current_user() else 0), result["info_hash"], "library",
                        origin["media_id"] if origin else None,
                        origin["title"] if origin else None, group_id, result["label"], now,
                    ),
                )
                if legacy_owner:
                    # F04: the administrator's own row, in the administrator's
                    # own table. A member submitting the same info_hash leaves
                    # it exactly as it was.
                    db.execute(
                        "INSERT OR REPLACE INTO cloud_download_task(info_hash, link_public_id, media_id, group_id, media_title, link_label, "
                        "wp_path_id, target_path, submitted_at, submitted_by, last_status, last_message, last_seen_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
                        (
                            result["info_hash"], result["link_id"], origin["media_id"] if origin else None, group_id,
                            origin["title"] if origin else None, result["label"], pid,
                            str(body.get("target_path") or "")[:200] or None, now, actor,
                        ),
                    )
        _cloud_cache_forget(_acting_user())
    duration_ms = int((time.monotonic() - started) * 1000)
    audit(
        "cloud_download.submit",
        "failed" if stop_error else "success",
        f"group={group_id} req={request_id} submitted={count} ok={len(ok)} failed={failed} not_submitted={len(not_submitted)} "
        f"pid={pid} duration_ms={duration_ms}" + (f" stop={_cloud_sanitize_message(stop_error)[:80]}" if stop_error else ""),
        actor_id(),
    )
    message = (
        f"已提交 {len(ok)} 条" + (f"，{failed} 条被 115 拒绝" if failed else "")
        if not stop_error else f"已提交 {len(ok)} 条后中止：{stop_error}；{len(not_submitted)} 条未提交"
    )
    return jsonify({
        "success": not stop_error,
        "message": message,
        "submitted": count,
        "ok": len(ok),
        "failed": failed,
        "not_submitted": len(not_submitted),
        "quota_surplus": max(surplus - len(ok), 0),
        "results": results,
    })


def _writes_legacy_cloud_task() -> bool:
    """Whether this caller owns the legacy ``cloud_download_task`` table.

    That table predates users: one row per info_hash, no user column, and the
    administrator's task list was read from it. It stays during the migration
    window because rolling back to the previous release has to find it intact
    -- but it is *the administrator's*, so a member's submit or delete must
    not rewrite the target, origin or status recorded in it (review F04). A
    request with no user at all is the deployment acting on its own behalf,
    which in the dev auth modes is the administrator.
    """
    user = _acting_user()
    return user is None or user.role == "admin"


def _cloud_download_gate():
    """The authorisation a cloud-download route needs before it touches 115.

    Phase 6 (R05): the call runs under the acting user's own step-B token,
    so a user without one is sent to the authorisation button and no
    upstream request is made on anybody else's credential.
    """
    _token, error = _require_open115(_acting_user())
    return error


_CLOUD_STATUS_LABEL = {-2: "已删除", -1: "失败", 0: "分配中", 1: "下载中", 2: "已完成"}
_CLOUD_INFO_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _cloud_error_response(status: int, error: str):
    return json_error("115 云下载接口失败：" + (error or "未知错误"), 502 if status in (200, 0) else status, "CLOUD_DOWNLOAD_UPSTREAM")


@app.get("/api/library/cloud-download/tasks")
def api_cloud_download_tasks():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        return json_error("page 必须为整数", 400, "BAD_REQUEST")
    gate = _cloud_download_gate()
    if gate is not None:
        return gate
    force = request.args.get("refresh") == "1"
    data, error = _cloud_task_list(page, force=force)
    if data is None:
        return _cloud_error_response(502, error)
    raw_tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
    hashes = [str(t.get("info_hash") or "") for t in raw_tasks]
    origins: dict[str, dict] = {}
    if hashes:
        placeholders = ",".join("?" for _ in hashes)
        now = utc_now()
        user_id = current_user().id if current_user() else 0
        with connect_db() as db:
            # Only this user's own notes: another user's row for the same
            # info_hash is theirs, and must not label this list.
            for row in db.execute(
                f"SELECT info_hash, media_id, display_title, group_id, link_label "
                f"FROM user_cloud_download_task WHERE user_id=? AND info_hash IN ({placeholders})",
                (user_id, *hashes),
            ).fetchall():
                origins[row["info_hash"]] = {
                    "media_id": row["media_id"], "group_id": row["group_id"],
                    "media_title": row["display_title"], "link_label": row["link_label"],
                }
            if _writes_legacy_cloud_task():
                # The administrator's tasks from before this release -- or
                # before the migration copies them -- are annotated only in
                # the legacy table. Their notes are still theirs.
                for row in db.execute(
                    f"SELECT info_hash, media_id, media_title, group_id, link_label "
                    f"FROM cloud_download_task WHERE info_hash IN ({placeholders})",
                    tuple(hashes),
                ).fetchall():
                    origins.setdefault(row["info_hash"], {
                        "media_id": row["media_id"], "group_id": row["group_id"],
                        "media_title": row["media_title"], "link_label": row["link_label"],
                    })
            for task in raw_tasks:
                info_hash = str(task.get("info_hash") or "")
                if info_hash in origins:
                    db.execute(
                        "UPDATE user_cloud_download_task SET state=?, last_seen_at=? WHERE user_id=? AND info_hash=?",
                        (str(task.get("status") or ""), now, user_id, info_hash),
                    )
    tasks = []
    for task in raw_tasks:
        info_hash = str(task.get("info_hash") or "")
        status = task.get("status")
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        tasks.append({
            "info_hash": info_hash,
            "name": str(task.get("name") or ""),
            "size": task.get("size"),
            "percent": task.get("percentDone"),
            "status": status,
            "status_label": _CLOUD_STATUS_LABEL.get(status, "未知"),
            "add_time": iso(task.get("add_time")) if isinstance(task.get("add_time"), int) else None,
            "last_update": iso(task.get("last_update")) if isinstance(task.get("last_update"), int) else None,
            "file_id": task.get("file_id"),
            "can_appeal": bool(task.get("can_appeal")),
            "origin": origins.get(info_hash),
        })
    return jsonify({
        "success": True, "page": data.get("page", page), "page_count": data.get("page_count", 1),
        "count": data.get("count", len(tasks)), "tasks": tasks,
    })


@app.get("/api/library/cloud-download/quota")
def api_cloud_download_quota():
    gate = _cloud_download_gate()
    if gate is not None:
        return gate
    force = request.args.get("refresh") == "1"
    data, error = _cloud_quota(force=force)
    if data is None:
        return _cloud_error_response(502, error)
    settings = _cloud_settings_dict()
    packages = [
        {"name": str(p.get("name") or ""), "count": p.get("count"), "used": p.get("used"), "surplus": p.get("surplus")}
        for p in (data.get("package") or []) if isinstance(p, dict)
    ]
    return jsonify({
        "success": True, "count": data.get("count"), "used": data.get("used"), "surplus": data.get("surplus"),
        "package": packages, "today_submitted": _cloud_today_submitted(), "daily_cap": settings["daily_cap"],
    })


@app.post("/api/library/cloud-download/tasks/<info_hash>/delete")
def api_cloud_download_delete(info_hash):
    if not _CLOUD_INFO_HASH_RE.match(info_hash or ""):
        return json_error("任务标识不正确", 400, "BAD_REQUEST")
    gate = _cloud_download_gate()
    if gate is not None:
        return gate
    body = request_json()
    delete_files = body.get("delete_files") is True
    _, status, error = _open115_offline(
        "POST", "/open/offline/del_task", data={"info_hash": info_hash, "del_source_file": "1" if delete_files else "0"},
    )
    audit("cloud_download.delete", "success" if status == 200 else "failed",
          f"hash={info_hash} delete_files={int(delete_files)}" + (f" error={error[:80]}" if error else ""), actor_id())
    if status != 200:
        return _cloud_error_response(status, error)
    now, user_id = utc_now(), (current_user().id if current_user() else 0)
    with connect_db() as db:
        if _writes_legacy_cloud_task():
            # F04: a member deleting their own task must not mark the
            # administrator's legacy row for the same info_hash deleted.
            db.execute("UPDATE cloud_download_task SET last_status=-2, last_seen_at=? WHERE info_hash=?", (now, info_hash))
        # The per-user row is what this user's task list reads (R05); only
        # their own is touched, never another user's row for the same hash.
        db.execute("UPDATE user_cloud_download_task SET state='-2', last_seen_at=? "
                   "WHERE user_id=? AND info_hash=?", (now, user_id, info_hash))
    _cloud_cache_forget(_acting_user())
    return jsonify({"success": True, "message": "任务已删除" + ("（含文件）" if delete_files else "")})


_CLOUD_CLEAR_FLAGS = {"failed": "2", "completed": "0"}


@app.post("/api/library/cloud-download/tasks/clear")
def api_cloud_download_clear():
    body = request_json()
    scope = body.get("scope")
    if scope not in _CLOUD_CLEAR_FLAGS:
        return json_error("scope 只能是 failed 或 completed", 400, "BAD_REQUEST")
    gate = _cloud_download_gate()
    if gate is not None:
        return gate
    _, status, error = _open115_offline("POST", "/open/offline/clear_task", data={"flag": _CLOUD_CLEAR_FLAGS[scope]})
    audit("cloud_download.clear", "success" if status == 200 else "failed",
          f"scope={scope}" + (f" error={error[:80]}" if error else ""), actor_id())
    if status != 200:
        return _cloud_error_response(status, error)
    # F04: clearing upstream is this user's own action on their own account;
    # it neither touches the administrator's legacy rows nor anybody else's
    # cache.
    _cloud_cache_forget(_acting_user())
    return jsonify({"success": True, "message": "已清理" + ("失败任务" if scope == "failed" else "已完成任务")})


@app.get("/api/library/cloud-download/status")
def api_cloud_download_status():
    settings = _cloud_settings_dict()
    return jsonify({
        "success": True,
        "enabled": settings["enabled"],
        "daily_cap": settings["daily_cap"],
        "per_submit_cap": settings["per_submit_cap"],
        "today_submitted": _cloud_today_submitted(),
        # This user's own step-B authorisation (R05) -- reading the global
        # setting here would tell a member about the administrator's.
        "token_available": bool(user_115_open_token(_acting_user())),
    })


@app.get("/api/library/recommendations")
def api_library_recommendations():
    store, error = _library_store_or_error(False)
    if error:
        return error

    date_raw = request.args.get("date", "")
    if date_raw:
        if not _RECOMMENDATION_DATE_RE.match(date_raw):
            return json_error("date 参数格式不正确", 400, "LIBRARY_RECOMMENDATIONS_DATE_INVALID")
        try:
            day = datetime.strptime(date_raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return json_error("date 参数格式不正确", 400, "LIBRARY_RECOMMENDATIONS_DATE_INVALID")
    else:
        day = datetime.now(_RECOMMENDATION_TIMEZONE).strftime("%Y-%m-%d")

    limit_raw = request.args.get("limit", str(_RECOMMENDATION_DEFAULT_LIMIT))
    try:
        limit = int(limit_raw)
    except ValueError:
        return json_error("limit 必须为整数", 400, "LIBRARY_RECOMMENDATIONS_LIMIT_INVALID")
    if not (1 <= limit <= _RECOMMENDATION_MAX_LIMIT):
        return json_error("limit 超出范围", 400, "LIBRARY_RECOMMENDATIONS_LIMIT_INVALID")

    placement = request.args.get("placement", "")
    if placement not in _RECOMMENDATION_PLACEMENTS:
        return json_error("placement 参数不正确", 400, "LIBRARY_RECOMMENDATIONS_PLACEMENT_INVALID")

    selection = _recommendation_selection(store, day, limit, placement)

    conn = store.connect(readonly=True)
    try:
        rows = library_search._fetch_items(conn, selection["ids"])
    finally:
        conn.close()
    items = []
    for row in rows:
        item = dict(row)
        item["poster_url"] = _tmdb_image_url(item.pop("poster_path", None), LIBRARY_POSTER_SIZE)
        item["backdrop_url"] = _tmdb_image_url(item.pop("backdrop_path", None), LIBRARY_BACKDROP_SIZE)
        items.append(item)

    return jsonify(
        {
            "success": True,
            "section": _RECOMMENDATION_PLACEMENTS[placement],
            "date": day,
            **({"rotation_date": _hero_rotation_date(day), "refresh_interval_days": 2} if placement == "hero" else {}),
            "algorithm_version": _HERO_ALGO_VERSION if placement == "hero" else _RECOMMENDATION_ALGO_VERSION,
            "fallback": selection["fallback"],
            "items": items,
        }
    )


@app.get("/api/library/filters")
def api_library_filters():
    store, error = _library_store_or_error(False)
    if error:
        return error
    return jsonify({"success": True, **store.filters()})


@app.get("/api/library/suggest")
def api_library_suggest():
    store, error = _library_store_or_error(False)
    if error:
        return error
    q = request.args.get("q", "")
    if len(q) > 120:
        return json_error("q 不能超过 120 字符", 400, "LIBRARY_SEARCH_Q_TOO_LONG")
    items = library_search.suggest(store, q, charmap=_library_charmap(store))
    return jsonify({"success": True, "items": items})


@app.get("/api/library/media/<int:media_id>")
def api_library_media(media_id):
    store, error = _library_store_or_error(False)
    if error:
        return error
    provider = request.args.get("provider") or None
    if provider is not None and provider not in library_normalize.PROVIDERS:
        return json_error("provider 参数不正确", 400, "LIBRARY_PROVIDER_INVALID")
    # Round 16: same wire spelling as /api/library/search's include_deleted
    # -- only "1"/"true" opt in; anything else is the default (hide groups
    # whose links are all invalid/deleted).
    include_deleted = (request.args.get("include_deleted") or "").strip().lower() in {"1", "true"}
    media = store.media_detail(media_id, provider=provider, include_deleted=include_deleted)
    if media is None:
        return json_error("未找到该媒体", 404, "MEDIA_NOT_FOUND")
    if media.get("media_type") in ("movie", "tv") and media.get("tmdb_id"):
        _re0_refresh_detail_ref(
            media["media_type"],
            int(media["tmdb_id"]),
            local_media_id=int(media["media_id"]),
            title=media.get("title"),
            original_title=media.get("original_title"),
            year=media.get("year"),
            overview=media.get("overview"),
            poster_path=media.get("poster_path"),
            backdrop_path=media.get("backdrop_path"),
        )
        # Re-read after the synchronous projection repair so effective ratings
        # and any projection-backed metadata are included in this response.
        media = store.media_detail(media_id, provider=provider, include_deleted=include_deleted)
        if media is None:
            return json_error("未找到该媒体", 404, "MEDIA_NOT_FOUND")
    media = dict(media)
    poster_path = media.pop("poster_path", None)
    media["poster_url"] = _tmdb_image_url(poster_path, LIBRARY_POSTER_SIZE)
    media["poster_large_url"] = _tmdb_image_url(poster_path, LIBRARY_POSTER_LARGE_SIZE)
    media["backdrop_url"] = _tmdb_image_url(media.pop("backdrop_path", None), LIBRARY_BACKDROP_SIZE)
    # Invalid-candidate work order §3.2: an RE0 share the upstream confirmed
    # dead is hidden unless the caller asks for the audit view. Independent of
    # include_deleted, which is about LOCAL links.
    include_invalid = (request.args.get("include_invalid") or "").strip().lower() in {"1", "true"}
    candidates, hidden_invalid = _re0_candidates_for_media(store, media_id, provider, include_invalid=include_invalid)
    media["re0_candidates"] = candidates
    media["re0_invalid_hidden_count"] = hidden_invalid
    media["provider_facets"] = _merge_re0_facets(media.get("provider_facets") or [], media["re0_candidates"])
    media["provider_count"] = len(media["provider_facets"])
    media["re0_calendar"] = _re0_calendar_for_media(store, media_id)
    media["re0_follow"] = _re0_follow_for_media(store, media_id)
    media["re0_follow_can_subscribe"] = _re0_has_scope("subscription")
    return jsonify({"success": True, **media})


def _re0_has_scope(scope: str) -> bool:
    row = get_tokens()
    return bool(row and row["scope"] and scope in str(row["scope"]).split())


def _re0_follow_for_media(store, media_id: int) -> list[dict]:
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT media_type, tmdb_id FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None or row["media_type"] != "tv" or not row["tmdb_id"]:
            return []
        return re0_sync.follow_rows(conn, tmdb_id=int(row["tmdb_id"]))
    finally:
        conn.close()


@app.post("/api/library/re0-follow/query")
def api_library_re0_follow_query():
    """User-triggered pack lookup for one TV media (spec §9.2, read-only)."""
    store, error = _library_store_or_error(True)
    if error:
        return error
    body = request_json()
    media_id = body.get("media_id")
    if not isinstance(media_id, int) or isinstance(media_id, bool):
        return json_error("media_id 必须为整数", 400, "BAD_REQUEST")
    conn = store.connect()
    try:
        row = conn.execute("SELECT media_type, tmdb_id FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None or row["media_type"] != "tv" or not row["tmdb_id"]:
            return json_error("该媒体不是带 TMDB ID 的剧集", 400, "RE0_FOLLOW_NOT_TV")
        salt = _re0_slug_salt(conn)
    finally:
        conn.close()
    report = re0_sync.query_packs_for_tmdb(store, _re0_client(), tmdb_id=int(row["tmdb_id"]), salt=salt, now=utc_now())
    if report.get("error_class"):
        status = {"rate_limited": 429, "reauth_required": 401, "scope_denied": 403, "user_level_denied": 403, "quota_exhausted": 429}.get(report["error_class"], 502)
        return jsonify({"success": False, "code": "RE0_" + report["error_class"].upper(), "message": "RE0 追更包查询失败", "retry_after": report.get("retry_after")}), status
    return jsonify({"success": True, "report": report, "message": ("已查询，找到 %d 个追更包" % report.get("recorded", 0)) if report["queried"] else "刚刚查询过，请稍后再试"})


@app.post("/api/library/re0-follow/<ref>/unlock")
def api_library_re0_follow_unlock(ref):
    """Explicit user unlock of one tv-follow pack (spec §9.2 items 4-6):
    idempotent per request_id; subscribe_updates only with the
    ``subscription`` scope; items are then materialised insert-only."""
    store, error = _library_store_or_error(True)
    if error:
        return error
    if not re.fullmatch(r"[0-9a-f]{16}", ref or ""):
        return json_error("追更包标识不正确", 400, "BAD_REQUEST")
    body = request_json()
    request_id = str(body.get("request_id") or "")
    if not _RE0_REQUEST_ID_RE.match(request_id):
        return json_error("request_id 格式不正确", 400, "BAD_REQUEST")
    subscribe = body.get("subscribe_updates") is True and _re0_has_scope("subscription")
    now = utc_now()
    conn = store.connect()
    try:
        pack = re0_sync._pack_row(conn, ref=ref)
        if pack is None:
            return json_error("未找到该追更包", 404, "RE0_FOLLOW_NOT_FOUND")
        pack = dict(pack)
        action_key = -int(pack["slug_hash"][:8], 16)  # packs live in the negative id space of re0_action
        prior = re0_sync.action_get(conn, request_id, action_key)
        salt = _re0_slug_salt(conn)
        try:
            slug = store.fernet.decrypt(pack["slug_ciphertext"]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return json_error("主密钥不可用", 503, "LIBRARY_KEY_UNAVAILABLE")
    finally:
        conn.close()
    if prior is not None and prior["status"] == "success":
        return jsonify({"success": True, "replayed": True, "materialized": 0, "subscribe_updates": bool(prior["already_owned"] == 2), "already_owned": True})
    # §10.1: the same rule as a resource unlock, or this would be the way
    # around it. An already-unlocked pack costs nothing and is let through
    # below; buying a new one is what the switch governs.
    pack_holder = f"{actor_id()}:{request_id}"
    if pack["is_unlocked"]:
        # Already bought. Nothing to coordinate and nothing to spend: fetch
        # the items and return.
        return _re0_follow_items_only(store, pack, ref, slug, salt, request_id, action_key, subscribe, now)

    if re0_sync.pack_unlock_state(pack) == re0_sync.STATE_RESULT_UNKNOWN:
        # G03.5: the pack's own "result unknown", honoured before the member
        # gate so an unconfirmed purchase is never repeated.
        return json_error(_RE0_UNCERTAIN_MESSAGE, 409, "RE0_UNLOCK_RESULT_UNKNOWN")
    refused = member_unlock_refused(f"follow={ref}")
    if refused is not None:
        return refused
    # R08: packs need the same protection as resources, under their own
    # key space -- a pack is not a resource id.
    pack_lease = re0_sync.pack_lease_key(pack["slug_hash"])
    if not _re0_lease_take(store, pack_lease, holder=pack_holder, now=now):
        return json_error("该追更包正在解锁中，请稍后重试", 409, "RE0_UNLOCK_IN_PROGRESS")
    with _re0_lease_held(store, pack_lease, holder=pack_holder):
        conn = store.connect(readonly=True)
        try:
            replay = re0_sync.action_get(conn, request_id, action_key)
            # F05: holding the lease is not the same as there being nothing
            # here. A same-request_id record only catches this caller's own
            # retry; the pack itself may have been unlocked by somebody else
            # entirely while this request was reading its first snapshot, and
            # buying it again is what that used to cost.
            current = re0_sync._pack_row(conn, ref=ref)
        finally:
            conn.close()
        if replay is not None and replay["status"] == "success":
            return jsonify({"success": True, "replayed": True, "materialized": 0,
                            "subscribe_updates": bool(replay["already_owned"] == 2), "already_owned": True})
        if current is not None and current["is_unlocked"]:
            return _re0_follow_items_only(store, dict(current), ref, slug, salt, request_id, action_key, subscribe, now)
        return _re0_follow_unlock_locked(store, pack, ref, slug, salt, request_id, action_key, subscribe, now)


def _re0_follow_items_only(store, pack, ref, slug, salt, request_id, action_key, subscribe, now):
    """An already-unlocked pack: fetch what it contains, buy nothing.

    F05: "already unlocked" has to mean "only collect the data", whichever
    request discovers it -- the one that bought it, or the one that arrived a
    moment later.
    """
    client = _re0_client()
    items = re0_sync.fetch_pack_items(store, client, slug=slug, salt=salt, now=now)
    conn = store.connect()
    try:
        re0_sync.action_put(conn, request_id, action_key, "follow-unlock", "success", result_code=items["status"],
                            resource_link_id=None, already_owned=True, unlock_points=0, now=now)
        conn.commit()
    finally:
        conn.close()
    audit("re0.follow.unlock", "success",
          f"pack={ref} items={items['items']} materialized={items['materialized']} already_owned=1 points=0 "
          f"subscribe={int(subscribe)}", actor_id())
    return jsonify({"success": True, "replayed": False, "materialized": items["materialized"],
                    "items": items["items"], "items_status": items["status"], "already_owned": True,
                    "unlock_points": 0, "subscribe_updates": subscribe})


def _re0_follow_unlock_locked(store, pack, ref, slug, salt, request_id, action_key, subscribe, now):
    """The pack unlock itself, with this pack's lease held."""
    client = _re0_client()
    # G03.1: a purchase is never repeated automatically.
    result = client.post(f"/api/open/tv-follow/packs/{slug}/unlock",
                         json={"subscribe_updates": subscribe}, consuming=True)
    if not result.ok:
        uncertain = result.error_class in re0_sync.UNCERTAIN_ERROR_CLASSES
        conn = store.connect()
        try:
            if uncertain:
                # G03.5: the same record the resource entry keeps, so the next
                # click asks the user rather than RE0.
                re0_sync.remember_pack_unlock_unknown(conn, pack["slug_hash"],
                                                      error_class=result.error_class, now=now)
            re0_sync.action_put(conn, request_id, action_key, "follow-unlock",
                                "unknown" if uncertain else "failed", result_code=result.error_class,
                                resource_link_id=None, already_owned=False, unlock_points=None, now=now)
            conn.commit()
        finally:
            conn.close()
        audit("re0.follow.unlock", "unknown" if uncertain else "failed",
              f"pack={ref} error={result.error_class} status={result.status}", actor_id())
        if uncertain:
            return jsonify({"success": False, "code": "RE0_UNLOCK_RESULT_UNKNOWN",
                            "message": _RE0_UNCERTAIN_MESSAGE}), 502
        status = {"rate_limited": 429, "reauth_required": 401, "scope_denied": 403, "user_level_denied": 403, "quota_exhausted": 429}.get(result.error_class, 502)
        payload = {"success": False, "code": "RE0_" + (result.error_class or "upstream_error").upper(), "message": result.message or "RE0 解锁追更包失败"}
        if result.retry_after:
            payload["retry_after"] = result.retry_after
        return jsonify(payload), status
    unlocked = re0_sync.parse_unlock_payload(result.data, slug)
    conn = store.connect()
    try:
        conn.execute("UPDATE re0_tv_follow_pack SET is_unlocked=1, items_status=NULL, updated_at=? WHERE slug_hash=?", (now, pack["slug_hash"]))
        # G03.5: confirmed, so any earlier "unknown" record is settled.
        re0_sync.clear_pack_unlock_state(conn, pack["slug_hash"], now=now)
        conn.commit()
    finally:
        conn.close()
    items = re0_sync.fetch_pack_items(store, client, slug=slug, salt=salt, now=now)
    conn = store.connect()
    try:
        re0_sync.action_put(conn, request_id, action_key, "follow-unlock", "success", result_code=items["status"], resource_link_id=None,
                            already_owned=unlocked["already_owned"], unlock_points=unlocked["points"], now=now)
        conn.commit()
    finally:
        conn.close()
    audit("re0.follow.unlock", "success", f"pack={ref} items={items['items']} materialized={items['materialized']} already_owned={int(bool(unlocked['already_owned']))} "
          f"points={unlocked['points']} subscribe={int(subscribe)}", actor_id())
    return jsonify({"success": True, "replayed": False, "materialized": items["materialized"], "items": items["items"], "items_status": items["status"],
                    "already_owned": bool(unlocked["already_owned"]), "unlock_points": unlocked["points"], "subscribe_updates": subscribe})


def _re0_calendar_for_media(store, media_id: int) -> dict | None:
    """Next future RE0 calendar event for a TV media with a TMDB id (spec
    §9.1); ``local_max_season`` decides 新一季 vs 下一集."""
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT media_type, tmdb_id FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None or row["media_type"] != "tv" or not row["tmdb_id"]:
            return None
        max_row = conn.execute("SELECT MAX(COALESCE(season_to, season_from)) FROM resource_group WHERE media_id=?", (media_id,)).fetchone()
        local_max = int(max_row[0]) if max_row and max_row[0] is not None else None
        return re0_sync.next_event_for(conn, "tv", int(row["tmdb_id"]), now=utc_now(), local_max_season=local_max)
    finally:
        conn.close()


def _re0_effective_status(store, res: dict, now: int) -> str:
    """The derived verdict for one candidate row (§3.1), read from the fields
    already stored -- no RE0 call, no slug."""
    try:
        raw = (json.loads(res.get("spec_json") or "{}") or {}).get("raw") or {}
    except ValueError:
        raw = {}
    conn = store.connect(readonly=True)
    try:
        preview = conn.execute("SELECT * FROM re0_file_preview WHERE re0_resource_id=?", (res["id"],)).fetchone()
    except sqlite3.OperationalError:
        preview = None
    finally:
        conn.close()
    return re0_sync.effective_status(
        upstream_validate_status=res.get("upstream_validate_status"), validate_message=raw.get("validate_message"),
        last_validated_at=raw.get("last_validated_at"), preview=preview, now=now,
    )["status"]


def _merge_re0_facets(facets: list, candidates: list) -> list:
    """Round 25: the detail page organises old and new resources by pan -- a
    pan only RE0 knows still gets a facet (``link_count`` 0), and a facet that
    holds RE0 candidates says how many (``re0_count``, present only when
    > 0 so facets without candidates keep their exact shape)."""
    counts: dict[str, int] = {}
    for cand in candidates:
        counts[cand["provider"]] = counts.get(cand["provider"], 0) + 1
    merged = {f["provider"]: dict(f) for f in facets}
    for code, count in counts.items():
        merged.setdefault(code, {"provider": code, "label": library_normalize.PROVIDERS.get(code, code), "link_count": 0})["re0_count"] = count
    order = list(library_store.PROVIDER_ORDER)
    return [merged[code] for code in sorted(merged, key=lambda c: (order.index(c) if c in order else len(order), c))]


def _re0_candidates_for_media(store, media_id: int, provider: str | None, *, include_invalid: bool = False) -> tuple[list[dict], int]:
    """``(visible candidates, hidden invalid count)`` for a local media (spec
    §10.1): server-side provider isolation, never a slug/URL; empty when the
    media has no TMDB id."""
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT media_type, tmdb_id FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None or not row["tmdb_id"] or row["media_type"] not in ("movie", "tv"):
            return [], 0
        try:
            rows = re0_sync.candidate_rows(conn, row["media_type"], int(row["tmdb_id"]), (provider,) if provider else (), now=utc_now())
        except sqlite3.OperationalError:
            return [], 0
    finally:
        conn.close()
    return re0_sync.split_invalid(rows, include_invalid)


@app.get("/api/library/re0-media/<media_type>/<int:tmdb_id>")
def api_library_re0_media(media_type, tmdb_id):
    """Detail view for a remote-only projection (spec §10.4.4 item 3):
    the same shape the local detail view renders, with no local groups."""
    store, error = _library_store_or_error(False)
    if error:
        return error
    if media_type not in ("movie", "tv"):
        return json_error("media_type 必须为 movie/tv", 400, "LIBRARY_SEARCH_TYPE_INVALID")
    provider = request.args.get("provider") or None
    if provider is not None and provider not in library_normalize.PROVIDERS:
        return json_error("provider 参数不正确", 400, "LIBRARY_PROVIDER_INVALID")
    re0_sync.catalog_projections(store, [(media_type, tmdb_id)], charmap=_library_charmap(store))
    _re0_refresh_detail_ref(media_type, tmdb_id)
    conn = store.connect(readonly=True)
    try:
        proj = re0_sync.projection_row(conn, media_type, tmdb_id)
        if proj is None:
            return json_error("未找到该 RE0 媒体", 404, "RE0_MEDIA_NOT_FOUND")
        local_id = proj["local_media_id"] or re0_sync.local_media_id_for(conn, media_type, tmdb_id)
        candidates = re0_sync.candidate_rows(conn, media_type, tmdb_id, (provider,) if provider else (), now=utc_now())
    finally:
        conn.close()
    try:
        ratings = json.loads(proj["ratings_json"] or "{}")
    except ValueError:
        ratings = {}
    include_invalid = (request.args.get("include_invalid") or "").strip().lower() in {"1", "true"}
    candidates, hidden_invalid = re0_sync.split_invalid(candidates, include_invalid)
    facets = _merge_re0_facets([], candidates)
    return jsonify({
        "success": True, "media_ref": f"re0:{media_type}:{tmdb_id}", "media_id": local_id, "media_type": media_type, "tmdb_id": tmdb_id,
        "title": proj["title"], "original_title": proj["original_title"], "year": proj["year"], "overview": proj["overview"],
        "poster_url": _tmdb_image_url(proj["poster_path"], LIBRARY_POSTER_SIZE), "poster_large_url": _tmdb_image_url(proj["poster_path"], LIBRARY_POSTER_LARGE_SIZE),
        "backdrop_url": _tmdb_image_url(proj["backdrop_path"], LIBRARY_BACKDROP_SIZE), "genres": [], "match_status": "re0",
        "ratings": ratings, "ratings_status": proj["ratings_status"], "metadata_status": proj["metadata_status"],
        "groups": [], "group_count": 0,
        "provider_facets": facets, "provider_count": len(facets), "re0_candidates": candidates,
        "re0_invalid_hidden_count": hidden_invalid,
        "re0_calendar": _re0_calendar_for_projection(store, media_type, tmdb_id),
    })


def _re0_calendar_for_projection(store, media_type: str, tmdb_id: int) -> dict | None:
    if media_type != "tv":
        return None
    conn = store.connect(readonly=True)
    try:
        return re0_sync.next_event_for(conn, "tv", tmdb_id, now=utc_now(), local_max_season=None)
    finally:
        conn.close()


@app.get("/api/library/re0/discoveries")
def api_library_re0_discoveries():
    """Home rail (spec §10.2): remote-only projections whose metadata is
    complete and that carry at least one RE0 candidate. Never touches the
    hero/recommendation selection, which stays local-media only."""
    store, error = _library_store_or_error(False)
    if error:
        return error
    try:
        limit = int(request.args.get("limit", "12"))
    except ValueError:
        return json_error("limit 必须为整数", 400, "BAD_REQUEST")
    if not (1 <= limit <= 24):
        return json_error("limit 超出范围", 400, "BAD_REQUEST")
    conn = store.connect(readonly=True)
    try:
        try:
            refs = [(r["media_type"], int(r["tmdb_id"])) for r in conn.execute(
                "SELECT media_type, tmdb_id FROM re0_media_projection p WHERE metadata_status='complete' "
                "AND NOT EXISTS (SELECT 1 FROM resource_group rg JOIN resource_link rl ON rl.group_id=rg.id "
                f"WHERE rg.media_id=p.local_media_id AND {library_store.live_link_sql('rl')}) "
                "AND poster_path IS NOT NULL AND overview IS NOT NULL AND overview != '' ORDER BY last_seen_at DESC LIMIT ?", (limit * 3,))]
            items = re0_sync.projection_items(conn, refs, (), _re0_image_url)[:limit]
        except sqlite3.OperationalError:
            items = []
    finally:
        conn.close()
    return jsonify({"success": True, "items": items})


@app.get("/api/library/re0/status")
def api_library_re0_status():
    store, error = _library_store_or_error(False)
    if error:
        return error
    now = utc_now()
    conn = store.connect(readonly=True)
    try:
        try:
            summary = re0_sync.status_summary(conn, now)
        except sqlite3.OperationalError:
            summary = {"projections": {}, "resources": {}, "actions_today": 0, "last_error_class": None}
    finally:
        conn.close()
    token_row = get_tokens()
    settings = _re0_settings()
    conn = store.connect(readonly=True)
    try:
        try:
            sync = re0_sync.run_status(conn, now)
        except sqlite3.OperationalError:
            sync = None
    finally:
        conn.close()
    return jsonify({
        "success": True,
        "sync": sync,
        "configured": bool(config_value("hdhive_app_secret", "HDHIVE_APP_SECRET")),
        "client_id_configured": bool(config_value("hdhive_client_id", "HDHIVE_CLIENT_ID")),
        "authorized": bool(token_row and decrypt_token(token_row, "access_token")),
        "budget": _re0_client().budget_status(), "daily_cap": settings["daily_cap"], "min_interval_ms": settings["min_interval_ms"],
        "direct_search": bool(settings["direct_search_path"]), **summary,
    })


def _re0_check_server_quota(client: "re0_sync.Re0Client", store, now: int) -> None:
    """Once per day (spec §4.1/§10.4.6): read /api/open/quota and, when RE0
    reports an integer remaining count, cap today's requests at half of it."""
    day = datetime.now(_CLOUD_TIMEZONE).date().isoformat()
    conn = store.connect()
    try:
        if re0_sync.state_get(conn, f"quota_checked:{day}"):
            return
        re0_sync.state_set(conn, f"quota_checked:{day}", "1", now)
        conn.commit()
    finally:
        conn.close()
    result = client.get("/api/open/quota")
    remaining = None
    if result.ok and isinstance(result.data, dict):
        value = result.data.get("endpoint_remaining")
        remaining = int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    client.apply_server_quota(remaining)


_RE0_REFRESH_THROTTLE_SECONDS = 60
_RE0_OWNERSHIP_TTL_SECONDS = 300


@app.post("/api/library/re0/refresh")
def api_library_re0_refresh():
    """User-triggered refresh of ONE media's RE0 resources (spec §10.4.3):
    bypasses the 24h resource TTL, still budgeted/cooled down, throttled
    per media, read-only."""
    store, error = _library_store_or_error(True)
    if error:
        return error
    body = request_json()
    media_type = str(body.get("media_type") or "")
    tmdb_id = body.get("tmdb_id")
    if media_type not in ("movie", "tv") or not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
        return json_error("media_type 必须为 movie/tv，tmdb_id 必须为正整数", 400, "BAD_REQUEST")
    # Ownership may change on RE0's website without a local action. For
    # unresolved candidates, detail opens revalidate at most every five
    # minutes, shared across users; settled resources retain the 24h TTL.
    if_stale = body.get("if_stale") is True
    now = utc_now()
    key = f"refresh_media:{media_type}:{tmdb_id}"
    conn = store.connect()
    try:
        proj = re0_sync.projection_row(conn, media_type, tmdb_id)
        unresolved = conn.execute(
            "SELECT 1 FROM re0_resource WHERE media_type=? AND tmdb_id=? "
            "AND state IN ('candidate','already_unlocked') LIMIT 1", (media_type, tmdb_id),
        ).fetchone()
        ttl = _RE0_OWNERSHIP_TTL_SECONDS if unresolved else re0_sync.RESOURCES_TTL_SECONDS
        if if_stale and proj and proj["last_fetched_at"] and now - int(proj["last_fetched_at"]) < ttl:
            return jsonify({"success": True, "fetched": False, "skipped": "fresh"})
        last = int(re0_sync.state_get(conn, key) or 0)
        if now - last < _RE0_REFRESH_THROTTLE_SECONDS:
            return json_error(f"刚刚刷新过，请 {_RE0_REFRESH_THROTTLE_SECONDS - (now - last)} 秒后再试", 429, "RE0_REFRESH_TOO_SOON")
        re0_sync.state_set(conn, key, str(now), now)
        salt = _re0_slug_salt(conn)
        media_id = re0_sync.local_media_id_for(conn, media_type, tmdb_id)
        title_row = conn.execute("SELECT title_zh FROM media WHERE id=?", (media_id,)).fetchone() if media_id else None
        conn.commit()
    finally:
        conn.close()
    title = (title_row["title_zh"] if title_row else None) or (proj["title"] if proj else f"TMDB {tmdb_id}")
    result = _re0_client().get(f"/api/open/resources/{media_type}/{tmdb_id}")
    if not result.ok:
        status = {"rate_limited": 429, "reauth_required": 401, "scope_denied": 403, "user_level_denied": 403, "quota_exhausted": 429}.get(result.error_class, 502)
        payload = {"success": False, "code": "RE0_" + (result.error_class or "upstream_error").upper(), "message": result.message or "RE0 查询失败"}
        if result.retry_after:
            payload["retry_after"] = result.retry_after
        return jsonify(payload), status
    raw_items = result.data if isinstance(result.data, list) else ((result.data or {}).get("items") if isinstance(result.data, dict) else [])
    report = re0_sync.record_items(store, media_type, tmdb_id, raw_items or [], media_id=media_id, media_title=title, salt=salt, now=now)
    re0_sync.upsert_projection(store, media_type, tmdb_id, title=title, year=None, overview=None, poster_path=None, backdrop_path=None, ratings={}, now=now, local_media_id=media_id)
    conn = store.connect()
    try:
        conn.execute("UPDATE re0_media_projection SET last_fetched_at=?, updated_at=? WHERE media_type=? AND tmdb_id=?", (now, now, media_type, tmdb_id))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True, "fetched": True, "report": report, "message": f"RE0 返回 {report['remote_items']} 条，新增候选 {report['new']}"})


_RE0_PREVIEW_ERROR_MESSAGES = {
    "rate_limited": "RE0 限流，请稍后再试",
    "reauth_required": "RE0 授权已失效，请到设置页重新授权",
    "quota_exhausted": "今日 RE0 查询额度已用完，请稍后再试",
}


@app.get("/api/library/re0/candidates/<int:candidate_id>/file-preview")
def api_library_re0_file_preview(candidate_id):
    """Work order §5.2: what is inside one RE0 share, on demand.

    The client sends only a local candidate id -- the slug is decrypted
    server-side and never leaves it. Read-only: no unlock, no points, no
    ``resource_link`` write, no change to local link-check. A ready preview is
    cached 12h, a confirmed-invalid share 5 minutes, a pan or account tier
    that cannot preview at all 12h; 429/5xx are not cached."""
    store, error = _library_store_or_error(True)
    if error:
        return error
    started = time.monotonic()
    result = re0_sync.file_preview(store, _re0_client(), resource_id=candidate_id, now=utc_now())
    if result.get("error_class") in ("unknown_resource", "slug_unreadable"):
        return json_error("未找到该 RE0 候选", 404, "RE0_CANDIDATE_NOT_FOUND")
    status = {"rate_limited": 429, "reauth_required": 401, "quota_exhausted": 429}.get(result.get("error_class") or "", 200)
    LOG.info("re0 file-preview candidate=%s status=%s cached=%s files=%s ms=%s", candidate_id, result["status"],
             result.get("cached"), result.get("file_count"), int((time.monotonic() - started) * 1000))
    payload = {"success": status == 200, "preview": result}
    if status != 200:
        # The browser's fetch wrapper reads `message`/`code`/`retry_after` off
        # the body for any non-2xx -- without them the user only sees the
        # status number.
        error_class = result.get("error_class") or "upstream_error"
        payload["code"] = "RE0_" + error_class.upper()
        payload["message"] = _RE0_PREVIEW_ERROR_MESSAGES.get(error_class, "文件预览暂不可用")
        if result.get("retry_after"):
            payload["retry_after"] = result["retry_after"]
            payload["message"] = f"RE0 限流，{result['retry_after']} 秒后再试"
    return jsonify(payload), status


_RE0_SYNC_PHASES = ("status", "refresh-existing", "reconcile", "discover-calendar", "discover-top", "discover-bounded", "tv-follow",
                    "file-list", "probe-resources")


def _re0_sync_lock():
    """The RE0 sync's own run lock (spec §11.1): distinct from the TMDB
    enricher, link-checker, check-in and OpenList-sync locks. Returns the
    open file (caller keeps it until done) or None when another run holds it."""
    lock_path = DATA_DIR / "re0-sync.run.lock"
    lock_file = lock_path.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def re0_sync_cli(argv: list[str]) -> dict:
    """``app.py --re0-sync`` (spec §11.3): status / refresh-existing /
    reconcile. Read-only against RE0 -- there is deliberately no phase or
    flag that spends points. ``--dry-run``/``--no-network`` select without
    building a client or writing anything."""
    import argparse

    parser = argparse.ArgumentParser(prog="app.py --re0-sync", add_help=False)
    parser.add_argument("--phase", default="status")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--max-requests", dest="max_requests", type=int, default=None)
    parser.add_argument("--media-ids", dest="media_ids", default="")
    parser.add_argument("--tmdb-ids", dest="tmdb_ids", default="")
    parser.add_argument("--resource-ids", dest="resource_ids", default="")
    parser.add_argument("--media-type", dest="media_type", default="movie", choices=["movie", "tv"])
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.add_argument("--no-network", dest="no_network", action="store_true")
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", default=None)
    parser.add_argument("--days", type=int, default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return {"ok": False, "error": "RE0_SYNC_ARGS_INVALID"}
    if args.phase not in _RE0_SYNC_PHASES:
        return {"ok": False, "error": "RE0_SYNC_PHASE_INVALID", "phases": list(_RE0_SYNC_PHASES)}
    try:
        store = library_store.open_installed(Path(args.db) if args.db else LIBRARY_DB_PATH, load_fernet())
    except (library_store.LibraryNotInstalled, library_store.LibraryNotEncrypted, library_store.LibrarySchemaMismatch,
            library_store.LibraryIndexUnreadable) as exc:
        return {"ok": False, "error": type(exc).__name__}
    now = utc_now()
    settings = _re0_settings()
    dry_run = args.dry_run or args.no_network
    def _ids(raw: str) -> list[int]:
        return [int(x) for x in raw.split(",") if x.strip().isdigit()]
    if args.phase == "status":
        client = _re0_client()
        _re0_check_server_quota(client, store, now)
        conn = store.connect(readonly=True)
        try:
            sync = re0_sync.run_status(conn, now)
            summary = re0_sync.status_summary(conn, now)
        finally:
            conn.close()
        conn = store.connect(readonly=True)
        try:
            calendar = re0_sync.calendar_status(conn, now)
        finally:
            conn.close()
        return {"ok": True, "phase": "status", "budget": client.budget_status(), "daily_cap": settings["daily_cap"],
                "min_interval_ms": settings["min_interval_ms"], "sync": sync, "calendar": calendar,
                "streaming_top_sources": setting_get("re0_streaming_top_sources") or "", **summary}
    lock_file = _re0_sync_lock()
    if lock_file is None:
        return {"ok": False, "error": "RE0_SYNC_BUSY"}
    try:
        conn = store.connect()
        try:
            salt = _re0_slug_salt(conn)
        finally:
            conn.close()
        client = None if dry_run else _re0_client()
        if client is not None:
            _re0_check_server_quota(client, store, now)
        max_requests = args.max_requests if args.max_requests is not None else settings["daily_cap"]
        if args.phase == "refresh-existing":
            report = re0_sync.refresh_existing(
                store, client, limit=max(1, args.limit), max_requests=max(1, max_requests), salt=salt, now=now,
                media_ids=_ids(args.media_ids) or None, tmdb_ids=_ids(args.tmdb_ids) or None, resume=not args.restart, dry_run=dry_run,
            )
        elif args.phase == "reconcile":
            report = re0_sync.reconcile_report(store, client, limit=max(1, args.limit), salt=salt, now=now, dry_run=dry_run)
        elif args.phase == "discover-calendar":
            days = args.days or int(setting_get("re0_calendar_days") or os.getenv("RE0_SYNC_CALENDAR_DAYS") or 31)
            if dry_run:
                report = {"phase": "discover-calendar", "dry_run": True, "would_fetch": True, "days": days}
            else:
                report = re0_sync.discover_calendar(store, client, days=days, now=now)
        elif args.phase == "discover-top":
            sources = [s for s in (setting_get("re0_streaming_top_sources") or os.getenv("RE0_STREAMING_TOP_SOURCES") or "").split(",") if s]
            if dry_run:
                report = {"phase": "discover-top", "dry_run": True, "sources": [":".join(t) for t in re0_sync.parse_top_sources(",".join(sources))]}
            else:
                report = re0_sync.discover_top(store, client, sources=sources, now=now)
        elif args.phase == "file-list":
            # Round 26: read-only preview of what is inside a share (合集包 vs
            # 单集) -- one GET per candidate, and it spends no points. (The
            # word this guard forbids belongs to the click-only route.)
            ids = _ids(args.resource_ids)[:max(1, args.limit)]
            if not ids:
                return {"ok": False, "error": "RE0_FILE_LIST_NEEDS_RESOURCE_IDS"}
            if dry_run:
                report = {"phase": "file-list", "dry_run": True, "would_request": len(ids), "resource_ids": ids}
            else:
                previews = [re0_sync.fetch_file_list(store, client, resource_id=rid, now=now) for rid in ids]
                report = {"phase": "file-list", "requested": len(previews), "succeeded": sum(1 for p in previews if p.get("ok")),
                          "failed": sum(1 for p in previews if not p.get("ok")), "previews": previews}
        elif args.phase == "probe-resources":
            # Round 27 diagnostic: what the upstream returns for one media,
            # item by item, next to the rows we kept. Writes nothing.
            targets: list[tuple[str, int]] = []
            conn = store.connect(readonly=True)
            try:
                for mid in _ids(args.media_ids):
                    row = conn.execute("SELECT media_type, tmdb_id FROM media WHERE id=? AND tmdb_id IS NOT NULL", (mid,)).fetchone()
                    if row is not None and row["media_type"] in ("movie", "tv"):
                        targets.append((row["media_type"], int(row["tmdb_id"])))
                for tid in _ids(args.tmdb_ids):
                    targets.append((args.media_type, tid))
            finally:
                conn.close()
            seen_targets: list[tuple[str, int]] = []
            for target in targets:
                if target not in seen_targets:
                    seen_targets.append(target)
            seen_targets = seen_targets[:max(1, args.limit)]
            if not seen_targets:
                return {"ok": False, "error": "RE0_PROBE_NEEDS_IDS"}
            if dry_run:
                report = {"phase": "probe-resources", "dry_run": True, "would_request": len(seen_targets),
                          "targets": [f"{t}:{i}" for t, i in seen_targets]}
            else:
                probes = [re0_sync.probe_resources(store, client, media_type=t, tmdb_id=i, salt=salt, now=now) for t, i in seen_targets]
                report = {"phase": "probe-resources", "requested": len(probes), "succeeded": sum(1 for p in probes if p.get("ok")),
                          "failed": sum(1 for p in probes if not p.get("ok")), "probes": probes}
        elif args.phase == "tv-follow":
            cap = int(setting_get("re0_tv_follow_daily_cap") or os.getenv("RE0_SYNC_TV_FOLLOW_DAILY_CAP") or 20)
            if dry_run:
                report = {"phase": "tv-follow", "dry_run": True, "would_query_up_to": min(max(1, args.limit), cap)}
            else:
                report = re0_sync.tv_follow_phase(store, client, limit=min(max(1, args.limit), cap), salt=salt, now=now)
        else:
            if dry_run:
                conn = store.connect(readonly=True)
                try:
                    pending = conn.execute("SELECT COUNT(*) FROM re0_media_projection WHERE local_media_id IS NULL AND (last_fetched_at IS NULL OR last_fetched_at <= ?)",
                                           (now - re0_sync.RESOURCES_TTL_SECONDS,)).fetchone()[0]
                finally:
                    conn.close()
                report = {"phase": "discover-bounded", "dry_run": True, "would_request": min(int(pending), max(1, args.limit), max(1, max_requests))}
            else:
                report = re0_sync.discover_bounded(store, client, limit=max(1, args.limit), max_requests=max(1, max_requests), salt=salt, now=now)
        report.setdefault("dry_run", dry_run)
        if not dry_run:
            audit("re0.sync", "success" if report.get("status", "completed") in ("completed", "budget_reached") else "failed",
                  f"phase={args.phase} requested={report.get('requested', report.get('queried', 0))} succeeded={report.get('succeeded', 0)} "
                  f"failed={report.get('failed', 0)} error={report.get('error_class')}", "system")
        report["ok"] = True
        return report
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def run_re0_sync(argv: list[str]) -> int:
    init_db()
    report = re0_sync_cli(argv)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 1


@app.post("/api/library/re0/run-small")
def api_library_re0_run_small():
    """Settings-card probe (spec §10.3): one media, one read-only RE0
    request, never an unlock."""
    store, error = _library_store_or_error(True)
    if error:
        return error
    lock_file = _re0_sync_lock()
    if lock_file is None:
        return json_error("RE0 同步正在运行，请稍后再试", 409, "RE0_SYNC_BUSY")
    try:
        conn = store.connect()
        try:
            salt = _re0_slug_salt(conn)
        finally:
            conn.close()
        report = re0_sync.refresh_existing(store, _re0_client(), limit=1, max_requests=1, salt=salt, now=utc_now())
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    audit("re0.sync", "success" if report["status"] in ("completed", "budget_reached") else "failed",
          f"phase=run-small requested={report['requested']} succeeded={report['succeeded']} failed={report['failed']} error={report['error_class']}", actor_id())
    return jsonify({"success": report["error_class"] is None, "report": report, "message": "已探测 1 条（只读）" if report["error_class"] is None else ("探测失败：" + str(report["error_class"]))})


_RE0_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_RE0_ACTIONS = {"transfer", "copy", "cloud"}


@app.post("/api/library/re0-resource/<int:resource_id>/unlock-and-action")
def api_library_re0_unlock_and_action(resource_id):
    """The ONLY code path that calls RE0's unlock endpoint: one slug, on an
    explicit user click, idempotent per ``request_id``. On success (or
    ``already_owned``) the payload is encrypted into ``resource_link``
    (insert-only) and the caller continues with the existing transfer /
    reveal-copy / cloud-download flows on ``link_public_id``."""
    store, error = _library_store_or_error(True)
    if error:
        return error
    body = request_json()
    action = str(body.get("action") or "")
    request_id = str(body.get("request_id") or "")
    if action not in _RE0_ACTIONS:
        return json_error("action 只能是 transfer/copy/cloud", 400, "BAD_REQUEST")
    if not _RE0_REQUEST_ID_RE.match(request_id):
        return json_error("request_id 格式不正确", 400, "BAD_REQUEST")
    now = utc_now()
    conn = store.connect(readonly=True)
    try:
        res = conn.execute("SELECT * FROM re0_resource WHERE id=?", (resource_id,)).fetchone()
        if res is None:
            return json_error("未找到该 RE0 资源", 404, "RE0_RESOURCE_NOT_FOUND")
        res = dict(res)
        prior = re0_sync.action_get(conn, request_id, resource_id)
        assoc = conn.execute(
            "SELECT l.public_id, l.id FROM re0_resource_link rl JOIN resource_link l ON l.id = rl.resource_link_id WHERE rl.re0_resource_id=? ORDER BY rl.linked_at DESC LIMIT 1",
            (resource_id,),
        ).fetchone()
    finally:
        conn.close()
    expected = re0_sync.action_for_provider(res["provider_code"])
    if expected == "unavailable":
        return json_error("该网盘类型暂未映射，无法自动解锁", 409, "RE0_PROVIDER_UNMAPPED")
    # §3.3: a share RE0 confirmed dead is refused BEFORE the slug is decrypted
    # and before any unlock request -- but only when nothing of it exists
    # locally yet; an already materialised candidate keeps its idempotent
    # replay path below. checking / unknown / preview_unavailable are not
    # verdicts and never land here.
    if prior is None and assoc is None and _re0_effective_status(store, res, now) == "invalid":
        LOG.info("re0 unlock refused resource=%s reason=invalid", resource_id)
        audit("re0.unlock", "refused", f"resource={resource_id} hash={res['slug_hash'][:12]} reason=invalid", "user")
        return json_error("该 RE0 分享已被标记失效，请选择其他候选", 409, "RE0_RESOURCE_INVALID")
    if action != expected:
        return json_error(f"该资源只支持「{expected}」动作", 400, "RE0_ACTION_NOT_ALLOWED")
    slug_hash_short = res["slug_hash"][:12]

    def _done(link_public_id: str, *, already_owned: bool, points, replayed: bool, media_id):
        return jsonify({
            "success": True, "replayed": replayed, "next_action": action, "provider": res["provider_code"], "link_public_id": link_public_id,
            "media_id": media_id, "already_owned": already_owned, "unlock_points": points,
        })

    if prior is not None and prior["status"] == "success" and prior["resource_link_id"]:
        conn = store.connect(readonly=True)
        try:
            link = conn.execute("SELECT public_id FROM resource_link WHERE id=?", (prior["resource_link_id"],)).fetchone()
        finally:
            conn.close()
        if link is not None:
            return _done(link["public_id"], already_owned=bool(prior["already_owned"]), points=prior["unlock_points"], replayed=True, media_id=res["media_id"])
    if assoc is not None:
        # Already materialised by an earlier action or a search-time unlocked
        # payload: never unlock twice, just continue with the action.
        conn = store.connect()
        try:
            re0_sync.action_put(conn, request_id, resource_id, action, "success", result_code="already_materialized", resource_link_id=int(assoc["id"]),
                                already_owned=True, unlock_points=0, now=now)
            conn.commit()
        finally:
            conn.close()
        return _done(assoc["public_id"], already_owned=True, points=0, replayed=False, media_id=res["media_id"])

    # Nothing local to reuse: this would spend points, so the member policy
    # decides before the slug is even decrypted.
    refused = member_unlock_refused(f"resource={resource_id}")
    if refused is not None:
        return refused

    # §10.2: one unlock per resource at a time, across users and workers.
    holder = f"{actor_id()}:{request_id}"
    lease_key = re0_sync.resource_lease_key(resource_id)
    conn = store.connect()
    try:
        got_lease = re0_sync.acquire_unlock_lease(conn, lease_key, holder=holder, now=now)
        conn.commit()
    finally:
        conn.close()
    if not got_lease:
        # Somebody is unlocking this very share. Look again before paying:
        # by the time they finish there is a local link to use instead.
        conn = store.connect(readonly=True)
        try:
            fresh = re0_sync.materialized_link(conn, resource_id)
        finally:
            conn.close()
        if fresh is not None:
            return _done(fresh["public_id"], already_owned=True, points=0, replayed=False, media_id=res["media_id"])
        return json_error("该资源正在解锁中，请稍后重试", 409, "RE0_UNLOCK_IN_PROGRESS")

    try:
        # R08: holding the lease is not the same as there being nothing here.
        # Whoever held it before may have just finished, in which case there
        # is a local link to use and nothing left to buy.
        conn = store.connect(readonly=True)
        try:
            fresh = re0_sync.materialized_link(conn, resource_id)
        finally:
            conn.close()
        if fresh is not None:
            conn = store.connect()
            try:
                re0_sync.action_put(conn, request_id, resource_id, action, "success",
                                    result_code="already_materialized", resource_link_id=int(fresh["id"]),
                                    already_owned=True, unlock_points=0, now=now)
                conn.commit()
            finally:
                conn.close()
            return _done(fresh["public_id"], already_owned=True, points=0, replayed=False,
                         media_id=res["media_id"])
        # G03.3: two states this resource may be in that mean "do not buy it".
        resumed = _re0_resume_pending(store, res, resource_id, action, request_id, now, _done)
        if resumed is not None:
            return resumed
        if re0_sync.resource_state(store, resource_id) == re0_sync.STATE_RESULT_UNKNOWN:
            return json_error(_RE0_UNCERTAIN_MESSAGE, 409, "RE0_UNLOCK_RESULT_UNKNOWN")
        try:
            slug = store.fernet.decrypt(res["slug_ciphertext"]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return json_error("主密钥不可用", 503, "LIBRARY_KEY_UNAVAILABLE")
        return _unlock_and_action_locked(
            store, res, resource_id, action, request_id, slug, slug_hash_short, now, _done)
    finally:
        conn = store.connect()
        try:
            re0_sync.release_unlock_lease(conn, lease_key, holder=holder)
            conn.commit()
        finally:
            conn.close()


_RE0_UNCERTAIN_MESSAGE = ("RE0 未确认这次解锁的结果。已记录为待确认：再次点击不会重复请求解锁，"
                          "请稍后在 RE0 页面确认后重试。")


def _re0_save_unlocked(store, resource_id: int, payload: dict, *, media_id, media_title, now: int):
    """Save a confirmed unlock, having first recorded that it *is* confirmed.

    G03.3: the order matters. RE0 has already charged for this by the time we
    get here, so the fact of the unlock is persisted before the save is
    attempted -- if the save then fails, the next click recovers from the
    record instead of buying the same thing again. Returns
    ``(outcome, error_class)``; ``outcome`` is None when the save failed and
    the payload is left pending.
    """
    re0_sync.remember_unlock_pending(
        store, resource_id, url=payload["url"], access_code=payload.get("access_code"),
        points=payload.get("points"), already_owned=bool(payload.get("already_owned")), now=now)
    try:
        outcome = re0_sync.materialize(store, resource_id, payload["url"], payload.get("access_code"),
                                       media_id=media_id, media_title=media_title, now=now)
    except (ValueError, sqlite3.DatabaseError) as exc:
        return None, type(exc).__name__
    return outcome, None


def _re0_resume_pending(store, res, resource_id, action, request_id, now, _done):
    """Finish a save that was left pending, spending nothing.

    G03.3: "已解锁但待保存" is a state the next click resumes from, through
    whichever entry it arrives at.
    """
    pending = re0_sync.pending_unlock(store, resource_id)
    if pending is None:
        return None
    media_id = res["media_id"]
    if media_id is None and res["tmdb_id"]:
        media_id = re0_sync.local_media_id_for(store.connect(readonly=True), res["media_type"], int(res["tmdb_id"]))
    if media_id is None:
        media_id = re0_sync.create_media_from_projection(store, res["media_type"], int(res["tmdb_id"]), now)
    if media_id is None:
        return json_error("缺少该媒体的投影，无法落库；已解锁的结果仍在本地待保存，不会重复扣分",
                          409, "RE0_PROJECTION_MISSING")
    outcome, failure = _re0_save_unlocked(store, resource_id, pending, media_id=media_id,
                                         media_title=res["media_title"], now=now)
    if outcome is None:
        audit("re0.unlock", "failed", f"resource={res['slug_hash'][:12]} error=pending_save_failed class={failure}", actor_id())
        return json_error("RE0 已解锁该资源，但本地保存失败；不会重复扣分，请稍后重试", 502,
                          "RE0_UNLOCK_PENDING_SAVE")
    conn = store.connect()
    try:
        link = conn.execute("SELECT public_id FROM resource_link WHERE id=?", (outcome["resource_link_id"],)).fetchone()
        re0_sync.action_put(conn, request_id, resource_id, action, "success", result_code="pending_save_resumed",
                            resource_link_id=outcome["resource_link_id"], already_owned=True,
                            unlock_points=0, now=now)
        conn.commit()
    finally:
        conn.close()
    re0_sync.clear_unlock_pending(store, resource_id, now=now)
    audit("re0.unlock", "success", f"resource={res['slug_hash'][:12]} action={action} relation=pending_save_resumed", actor_id())
    return _done(link["public_id"], already_owned=True, points=0, replayed=False, media_id=media_id)


def _unlock_and_action_locked(store, res, resource_id, action, request_id, slug, slug_hash_short, now, _done):
    """The unlock itself, with this resource's lease held. Split out so the
    lease is released on every path out, including a raised exception."""
    # G03.1: a purchase is never repeated automatically.
    result = _re0_client().post("/api/open/resources/unlock", json={"slug": slug}, consuming=True)
    started = time.monotonic()
    if not result.ok:
        uncertain = result.error_class in re0_sync.UNCERTAIN_ERROR_CLASSES
        if uncertain:
            # G03.3: a 5xx, a timeout or an unparseable body does not say the
            # purchase did not happen. Record it so the next click asks the
            # user instead of asking RE0 again.
            re0_sync.remember_unlock_unknown(store, resource_id, error_class=result.error_class, now=now)
        conn = store.connect()
        try:
            re0_sync.action_put(conn, request_id, resource_id, action,
                                "unknown" if uncertain else "failed", result_code=result.error_class,
                                resource_link_id=None, already_owned=False, unlock_points=None, now=now)
            conn.commit()
        finally:
            conn.close()
        audit("re0.unlock", "unknown" if uncertain else "failed",
              f"resource={slug_hash_short} provider={res['provider_code']} action={action} error={result.error_class} status={result.status}", actor_id())
        if uncertain:
            return jsonify({"success": False, "code": "RE0_UNLOCK_RESULT_UNKNOWN",
                            "message": _RE0_UNCERTAIN_MESSAGE}), 502
        status = {"rate_limited": 429, "reauth_required": 401, "scope_denied": 403, "user_level_denied": 403, "quota_exhausted": 429}.get(result.error_class, 502)
        code = "RE0_" + (result.error_class or "upstream_error").upper()
        payload = {"success": False, "code": code, "message": result.message or "RE0 解锁失败"}
        if result.retry_after:
            payload["retry_after"] = result.retry_after
        return jsonify(payload), status
    unlocked = re0_sync.parse_unlock_payload(result.data, slug)
    if not unlocked["url"]:
        conn = store.connect()
        try:
            re0_sync.action_put(conn, request_id, resource_id, action, "failed", result_code="already_unlocked_no_payload", resource_link_id=None,
                                already_owned=unlocked["already_owned"], unlock_points=unlocked["points"], now=now)
            conn.execute("UPDATE re0_resource SET state='already_unlocked', last_error_class='already_unlocked_no_payload', updated_at=? WHERE id=?", (now, resource_id))
            conn.commit()
        finally:
            conn.close()
        audit("re0.unlock", "failed", f"resource={slug_hash_short} provider={res['provider_code']} action={action} error=already_unlocked_no_payload", actor_id())
        return json_error("RE0 已解锁但未返回链接，请稍后在 RE0 页面查看", 502, "RE0_ALREADY_UNLOCKED_NO_PAYLOAD")
    # RE0 has confirmed this is ours: record that *now*, before anything that
    # can still fail -- resolving the media, creating it from the projection,
    # parsing the link, writing the library. Every one of those used to be a
    # way for a paid unlock to be forgotten and bought again on the next click.
    re0_sync.remember_unlock_pending(
        store, resource_id, url=unlocked["url"], access_code=unlocked["access_code"],
        points=unlocked["points"], already_owned=bool(unlocked["already_owned"]), now=now)
    media_id = res["media_id"] or re0_sync.local_media_id_for(store.connect(readonly=True), res["media_type"], int(res["tmdb_id"]))
    if media_id is None:
        media_id = re0_sync.create_media_from_projection(store, res["media_type"], int(res["tmdb_id"]), now)
        if media_id is None:
            audit("re0.unlock", "failed", f"resource={slug_hash_short} provider={res['provider_code']} action={action} error=projection_missing pending_save=1", actor_id())
            return json_error("缺少该媒体的投影，无法落库；已解锁的结果已在本地待保存，不会重复扣分",
                              409, "RE0_PROJECTION_MISSING")
        audit("re0.media.create", "success", f"tmdb={res['media_type']}:{res['tmdb_id']} reason=re0_public_tmdb_id", actor_id())
    outcome, failure = _re0_save_unlocked(store, resource_id, unlocked, media_id=media_id,
                                          media_title=res["media_title"], now=now)
    if outcome is None:
        # G03.3: the unlock happened; only the local save did not. Saying
        # "unlock failed" here is what made the next click pay again.
        audit("re0.unlock", "failed", f"resource={slug_hash_short} provider={res['provider_code']} action={action} error=materialize_failed class={failure}", actor_id())
        return json_error("RE0 已解锁该资源，但本地保存失败；不会重复扣分，请稍后重试", 502,
                          "RE0_UNLOCK_PENDING_SAVE")
    conn = store.connect()
    try:
        re0_sync.action_put(conn, request_id, resource_id, action, "success", result_code="unlocked", resource_link_id=outcome["resource_link_id"],
                            already_owned=unlocked["already_owned"], unlock_points=unlocked["points"], now=now)
        link = conn.execute("SELECT public_id FROM resource_link WHERE id=?", (outcome["resource_link_id"],)).fetchone()
        conn.commit()
    finally:
        conn.close()
    duration_ms = int((time.monotonic() - started) * 1000)
    audit("re0.unlock", "success",
          f"resource={slug_hash_short} provider={res['provider_code']} action={action} relation={outcome['relation']} "
          f"already_owned={int(unlocked['already_owned'])} points={unlocked['points']} duration_ms={duration_ms}", actor_id())
    return _done(link["public_id"], already_owned=unlocked["already_owned"], points=unlocked["points"], replayed=False, media_id=media_id)


@app.get("/api/library/resource/<int:group_id>")
def api_library_resource(group_id):
    store, error = _library_store_or_error(False)
    if error:
        return error
    group = store.group_detail(group_id)
    if group is None:
        return json_error("未找到该资源组", 404, "RESOURCE_GROUP_NOT_FOUND")
    return jsonify({"success": True, **group})


# w6-contract §3: 60s per-group cooldown, in-memory (per worker process) --
# same pattern as _TRANSFER_DEDUPE_SEEN above, personal-use scale, no
# cross-process coordination needed.
LINKCHECK_RECHECK_COOLDOWN = 60
_RECHECK_COOLDOWN_LOCK = threading.Lock()
_RECHECK_LAST_QUEUED_AT: dict[int, float] = {}


def _recheck_cooldown_check(group_id: int) -> bool:
    """True (and records the attempt) when ``group_id`` was not queued
    within the last ``LINKCHECK_RECHECK_COOLDOWN`` seconds; False (leaves
    the recorded time untouched) for a request arriving during the
    cooldown."""
    now = time.monotonic()
    with _RECHECK_COOLDOWN_LOCK:
        for seen_id, seen_at in list(_RECHECK_LAST_QUEUED_AT.items()):
            if now - seen_at >= LINKCHECK_RECHECK_COOLDOWN:
                del _RECHECK_LAST_QUEUED_AT[seen_id]
        if group_id in _RECHECK_LAST_QUEUED_AT:
            return False
        _RECHECK_LAST_QUEUED_AT[group_id] = now
        return True


@app.post("/api/library/resource/<int:group_id>/recheck")
def api_library_resource_recheck(group_id):
    """w6-contract §3: queue every LIVE link of ``group_id`` (whose
    provider is currently enabled) for an immediate, high-priority link
    check. Links of a disabled/unsupported provider are counted in
    ``skipped_disabled`` and left untouched -- this endpoint only ever
    writes to ``link_check``, never touches TMDB/115/HDHive itself."""
    store, error = _library_store_or_error(False)
    if error:
        return error
    group = store.group_detail(group_id)
    if group is None:
        return json_error("未找到该资源组", 404, "RESOURCE_GROUP_NOT_FOUND")
    if not _recheck_cooldown_check(group_id):
        return json_error("该资源组刚检测过，请稍后再试", 429, "LINKCHECK_RECHECK_COOLDOWN")

    settings = _linkcheck_settings_dict()
    enabled_providers = [
        code for code in library_tmdb.LINK_CHECK_PROVIDERS if library_tmdb._linkcheck_provider_enabled(settings, code)
    ]
    # M4: skipped_disabled (a phase-1 provider that's just toggled off) and
    # skipped_unsupported (a provider with no checker adapter at all --
    # baidu/ed2k/139cloud/... -- recheck could never queue it no matter
    # what's toggled) are reported separately; w6-contract's own
    # "skipped_disabled" field keeps its original disabled-but-supported-
    # only meaning, "skipped_unsupported" is additive (a UI that predates
    # it can ignore the extra field).
    queued, skipped_disabled, skipped_unsupported = store.queue_group_for_recheck(
        group_id, enabled_providers, supported_providers=list(library_tmdb.LINK_CHECK_PROVIDERS),
    )
    audit("library.linkcheck.recheck", "success", f"group={group_id} queued={queued} skipped={skipped_disabled}", actor_id())
    return jsonify({
        "success": True,
        "queued": queued,
        "skipped_disabled": skipped_disabled,
        "skipped_unsupported": skipped_unsupported,
        "message": f"已加入检测队列（{queued} 条）" if queued else "没有可检测的链接",
    })


def _library_linkcheck_status_payload(store: "library_store.LibraryStore") -> dict:
    """Build the ``/api/library/linkcheck-status`` payload (w6-contract
    §4). Read-only: every DB access below uses a readonly connection."""
    settings = _linkcheck_settings_dict()
    now = int(time.time())

    state_conn = store.connect(readonly=True)
    try:
        state = library_tmdb.read_link_check_state(state_conn)
    finally:
        state_conn.close()

    counts = store.link_check_counts(list(library_tmdb.LINK_CHECK_PROVIDERS), now=now)

    providers_payload: dict[str, dict] = {}
    totals = {"checked": 0, "valid": 0, "invalid": 0, "unknown": 0, "unchecked": 0, "queued_priority": 0}
    for code in library_tmdb.LINK_CHECK_PROVIDERS:
        adapter = library_tmdb.LINK_CHECK_ADAPTERS[code]
        daily_cap = library_tmdb._linkcheck_provider_cap(settings, code, adapter.default_daily_cap)
        used_today_raw = state.get(f"budget_used:{code}:{datetime.now(timezone.utc).date().isoformat()}")
        try:
            used_today = int(used_today_raw) if used_today_raw is not None else 0
        except ValueError:
            used_today = 0
        paused_until_raw = _state_int(state, f"paused_until:{code}")
        code_counts = counts.get(code, {"valid": 0, "invalid": 0, "unknown": 0, "unchecked": 0, "due": 0})
        providers_payload[code] = {
            "enabled": library_tmdb._linkcheck_provider_enabled(settings, code),
            "daily_cap": daily_cap,
            "used_today": used_today,
            "interval_seconds": adapter.interval_seconds,
            "paused_until": iso(paused_until_raw),
            "last_error_class": state.get(f"last_error_class:{code}") or None,
            **code_counts,
        }
        for key in ("valid", "invalid", "unknown", "unchecked"):
            totals[key] += code_counts[key]
        totals["checked"] += code_counts["valid"] + code_counts["invalid"] + code_counts["unknown"]

    conn = store.connect(readonly=True)
    try:
        placeholders = ",".join("?" for _ in library_tmdb.LINK_CHECK_PROVIDERS)
        row = conn.execute(
            f"SELECT COUNT(*) FROM link_check WHERE priority > 0 AND provider IN ({placeholders})",
            list(library_tmdb.LINK_CHECK_PROVIDERS),
        ).fetchone()
    finally:
        conn.close()
    totals["queued_priority"] = row[0] if row else 0

    return {
        "enabled": library_tmdb._linkcheck_global_enabled(settings),
        "heartbeat_at": iso(_state_int(state, "heartbeat_at")),
        "leader_pid": _state_int(state, "leader_pid"),
        "providers": providers_payload,
        "totals": totals,
    }


@app.get("/api/library/linkcheck-status")
def api_library_linkcheck_status():
    store, error = _library_store_or_error(False)
    if error:
        return error
    return jsonify({"success": True, **_library_linkcheck_status_payload(store)})


@app.get("/api/library/tmdb-status")
def api_library_tmdb_status():
    store, error = _library_store_or_error(False)
    if error:
        return error
    return jsonify({"success": True, **_library_tmdb_status_payload(store)})


@app.post("/api/library/tmdb-check")
def api_library_tmdb_check():
    """T10: one real TMDB request (``genre/movie/list``) through the
    production client wiring, so a non-technical user can see *why*
    enrichment isn't progressing without anyone shelling into the
    container. Never returns the key, a URL or the upstream body -- only
    an error class name and a canned Chinese hint.

    Uses the ``fast`` client (T10 fix wave 1: ``request_timeout=4``,
    ``max_retries=0``) -- this is a single request, so the worst case is
    one blocked TCP attempt at ~4s plus one rate-limiter wait
    (``min_interval_ms``, default 40ms -- or, if this process's shared
    ``RateLimiter`` is mid-cooldown from a recent 429, up to the T14
    adaptive-slowdown ceiling of 250ms; see ``_shared_tmdb_rate_limiter``),
    well under gunicorn's 30s worker timeout. Without this, the default
    client's retry policy could block for ~67s on this one request alone (4
    attempts x up to 15s each, plus 1+2+4s backoff)."""
    _store, client, error = _library_tmdb_precheck(fast=True)
    if error:
        return error
    started = time.monotonic()
    try:
        entry = client.check_connectivity()
    except library_tmdb.InvalidApiKey:
        audit("library.tmdb_check", "failed", "InvalidApiKey", actor_id())
        return jsonify({"success": True, "ok": False, "error_class": "InvalidApiKey", "hint": _tmdb_error_hint("InvalidApiKey")})
    except library_tmdb.BudgetExhausted:
        audit("library.tmdb_check", "failed", "BudgetExhausted", actor_id())
        return jsonify({"success": True, "ok": False, "error_class": "BudgetExhausted", "hint": _tmdb_error_hint("BudgetExhausted")})
    if entry.status in ("ok", "empty"):
        latency_ms = int((time.monotonic() - started) * 1000)
        genres_count = len(entry.payload) if entry.payload else 0
        audit("library.tmdb_check", "success", f"genres={genres_count}", actor_id())
        return jsonify({"success": True, "ok": True, "latency_ms": latency_ms, "genres_count": genres_count})
    error_class = entry.error_class or "Unknown"
    audit("library.tmdb_check", "failed", error_class, actor_id())
    return jsonify({"success": True, "ok": False, "error_class": error_class, "hint": _tmdb_error_hint(error_class)})


@app.post("/api/library/tmdb-enrich-now")
def api_library_tmdb_enrich_now():
    """T10: one manual enrichment round, sharing ``enrich_batch``/
    ``merge_by_tmdb`` and the same non-blocking run lock as the background
    thread and the ``--library-enrich`` CLI (``run_library_enrich``) --
    allowed even when ``tmdb_enrich_enabled`` is off, since triggering it
    here is itself the explicit manual action.

    Uses the ``fast`` client (``request_timeout=4``, ``max_retries=0``),
    ``limit=5`` and a 6s ``deadline`` (T10 fix waves 1-2) so this request stays
    well under gunicorn's 30s worker timeout: a candidate issues at most 2
    TMDB searches (primary + one Latin-alias retry), so the 6s deadline
    (checked before each candidate starts, never mid-candidate) plus the
    still-in-flight candidate's own worst case (2 x 4s timeouts, or 3 x 4s
    on the rare round where that same candidate is also its first exact
    match and the genre lookup it triggers times out too) bounds the total
    near ~18s (6 + 3 x 4), comfortably under 30s -- vs. the unbounded, ~67s-per-request compounding risk
    (up to 10 candidates x 2 requests x ~67s) the default client/limit had.
    Rate-limiter pacing (``min_interval_ms``, default 40ms across up to 10
    requests -- or, in the worst case, the T14 adaptive-slowdown ceiling of
    250ms if this process's shared ``RateLimiter`` is mid-cooldown from a
    recent 429) adds at most ~2.5s on top, keeping the ~18s bound
    comfortably under 30s even then."""
    store, client, error = _library_tmdb_precheck(fast=True)
    if error:
        return error
    lock_paths = _tmdb_lock_paths()
    lock_paths.run.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_paths.run, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        audit("library.tmdb_enrich_now", "busy", "", actor_id())
        return json_error("后台补全正在运行，请稍后再试", 409, "TMDB_ENRICH_BUSY")
    try:
        stats = library_tmdb.enrich_batch(
            store, client, limit=5, deadline=time.monotonic() + 6, tvmaze_enabled=_library_tvmaze_enabled_check(),
        )
        if stats.matched_exact > 0:
            library_tmdb.merge_by_tmdb(store)
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    audit("library.tmdb_enrich_now", "success", f"matched_exact={stats.matched_exact}", actor_id())
    return jsonify({
        "success": True,
        "stats": {
            "candidates_considered": stats.candidates_considered,
            "requests_made": stats.requests_made,
            "matched_exact": stats.matched_exact,
            "matched_candidate": stats.matched_candidate,
            "needs_review": stats.matched_needs_review,
            "error_class": stats.error_class,
        },
    })


def _link_scheme_allowed(url: str) -> bool:
    """C1 defense in depth: even though library_normalize now only ever
    labels a http(s)/ed2k/magnet URL as revealable at import time, a
    stored row could still predate that fix (or be written by another
    path) -- so reveal/transfer must independently check the actual
    decrypted URL's scheme against the same allowlist before ever handing
    it back to a client or a downstream request."""
    lowered = (url or "").strip().lower()
    return lowered.startswith(("http://", "https://", "ed2k://", "magnet:"))


@app.post("/api/library/link/<public_id>/reveal")
def api_library_reveal(public_id):
    store, error = _library_store_or_error(True)
    if error:
        return error

    row = store.link_by_public_id(public_id)
    if row is None:
        return json_error("未找到该链接", 404, "LINK_NOT_FOUND")

    provider = row["provider"]
    if provider == "115":
        return json_error("115 分享请使用转存", 403, "LINK_PROVIDER_115")
    # plan §4.4: an "unknown" link with a real URL (a host outside the
    # provider allowlist) is still revealable -- only non-URL text, which
    # library_normalize labels INVALID_LINK_LABEL, is not.
    if provider == "unknown" and row["url_label"] == library_normalize.INVALID_LINK_LABEL:
        return json_error("该链接不支持查看", 400, "LINK_NOT_REVEALABLE")

    try:
        url, access_code = store.reveal(public_id)
    except library_store.LibraryKeyUnavailable:
        return json_error("主密钥不可用", 503, "LIBRARY_KEY_UNAVAILABLE")
    if not _link_scheme_allowed(url):
        return json_error("该链接不支持查看", 400, "LINK_NOT_REVEALABLE")

    audit("library.reveal", "success", f"link={public_id} provider={provider}", actor_id())
    return jsonify({"success": True, "provider": provider, "url": url, "access_code": access_code})


@app.post("/api/library/transfer")
def api_library_transfer():
    """T19 wave 3 item 1 / wave 4 items 1-3/5: the single request-scoped
    deadline (g.request_started + _115_REQUEST_DEADLINE_SECONDS=24s) is
    threaded through _resolve_115_target_pid's OpenList listing (the UI
    always sends target_path), _115_transfer_gate/_115_verify_with_uid and
    save_115_link's share/snap (+ its one retry) and share/receive.
    g.request_started is recorded as the FIRST statement of
    before_request -- before Access/JWKS work -- so LESS than the full
    24s may already remain by the time this view body runs; it is not a
    fresh 24s window starting here.

    Every upstream call's own timeout is a (connect, read) tuple --
    (min(_115_UPSTREAM_CONNECT_TIMEOUT=3, remaining),
    min(_115_UPSTREAM_TIMEOUT=8, remaining)); share/snap's retry only runs
    with >=_115_SNAP_RETRY_MIN_REMAINING=12s left; share/receive is only
    issued with >=_115_RECEIVE_MIN_REMAINING=3s left, its own read timeout
    clamped by whatever remains of the shared deadline (min(8,
    remaining)) -- otherwise a fixed 504 TRANSFER_NOT_ATTEMPTED is
    returned and share/receive is never called, since an ambiguous cutoff
    there risks a duplicate transfer. A budget_exhausted verify (the
    shared budget, not 115 itself, ran out) returns this same fixed 504
    without touching cookie state. Either way the request stays
    comfortably under gunicorn's 30s worker timeout, and share/receive --
    the one call that must never be cut off mid-flight -- is never the one
    its timeout kills.

    T17 item 5: after target-pid resolution, ``_transfer_dedupe_check``
    rejects a repeat of the same ``(resource_link_id, resolved pid)`` with
    409 ``TRANSFER_DUPLICATE`` if submitted again within 10s (a double
    click or retry storm), before ``save_115_link`` is ever called again.

    T17 fix wave 1 item 1: the request body's ``target_path`` is display
    text only, never a security boundary -- ``_resolve_115_target_pid``
    always proves ``target_pid`` server-side (against its own path->pid
    resolution, the stored default, or the configured /115pan root cid)
    before it is ever used as the upstream 115 ``cid``; a pid that can't be
    proven is rejected with 400 ``TARGET_PID_INVALID`` and ``save_115_link``
    (hence the actual 115 transfer call) is never reached."""
    store, error = _library_store_or_error(True)
    if error:
        return error

    deadline = g.request_started + _115_REQUEST_DEADLINE_SECONDS
    body = request_json()
    public_id = str(body.get("resource_link_id") or "").strip()
    if not public_id:
        return json_error("请提供 resource_link_id")

    row = store.link_by_public_id(public_id)
    if row is None:
        return json_error("未找到该链接", 404, "LINK_NOT_FOUND")
    if row["provider"] != "115":
        return json_error("该链接不支持转存", 400, "LINK_NOT_TRANSFERABLE")

    try:
        url, access_code = store.reveal(public_id)
    except library_store.LibraryKeyUnavailable:
        return json_error("主密钥不可用", 503, "LIBRARY_KEY_UNAVAILABLE")
    if not _link_scheme_allowed(url):
        return json_error("该链接不支持转存", 400, "LINK_NOT_TRANSFERABLE")

    pid, target_error = _resolve_115_target_pid(body, deadline)
    if target_error:
        return target_error

    if not _transfer_dedupe_check(public_id, pid, _dedupe_user_key()):
        # T17 fix wave 1 item 3: the rejection itself is audited (it never
        # was before), distinct from both "success" and "failed" below.
        audit("library.transfer", "duplicate", f"link={public_id} code=TRANSFER_DUPLICATE", actor_id())
        return json_error("请勿重复提交转存请求", 409, "TRANSFER_DUPLICATE")

    link = _apply_access_code(url, access_code)
    request_id = secrets.token_hex(8)
    started = time.monotonic()
    result = save_115_link(link, pid, deadline)
    if not result.receive_attempted:
        # T17 fix wave 1 item 3: nothing was actually sent to 115 -- don't
        # let this rejection occupy the dedupe window against a legitimate
        # immediate retry.
        _transfer_dedupe_clear(public_id, pid, _dedupe_user_key())
    if row["deleted_at_source"] is not None:
        result.message = "该分享在来源已标记删除，" + result.message
    duration_ms = int((time.monotonic() - started) * 1000)
    audit(
        "library.transfer",
        "success" if result.success else "failed",
        f"link={public_id} req={request_id} code={result.code or 'OK'} status={result.status} duration_ms={duration_ms}",
        actor_id(),
    )
    return _transfer_response(result)


@app.post("/api/hdhive/oauth/start")
def oauth_start():
    client_id = config_value("hdhive_client_id", "HDHIVE_CLIENT_ID")
    if not client_id:
        return json_error("请先配置 RE0 Client ID", 503, "HDHIVE_CLIENT_ID_MISSING")
    state = secrets.token_urlsafe(32)
    created_by = actor_id()
    with connect_db() as db:
        db.execute("DELETE FROM oauth_states WHERE expires_at < ? OR used_at IS NOT NULL", (utc_now(),))
        db.execute("INSERT INTO oauth_states(state,expires_at,created_by) VALUES(?,?,?)", (state, utc_now() + STATE_TTL_SECONDS, created_by))
    redirect_uri = PUBLIC_ORIGIN + "/api/oauth/hdhive/callback"
    # ``meta`` is required by RE0 for the authenticated /api/open/ping
    # connectivity check; keep it alongside the business scopes used by the
    # library and check-in flows.
    query = urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "scope": "meta query unlock write", "state": state})
    audit("hdhive.oauth.start", "success", "authorization URL issued", created_by)
    return jsonify({"success": True, "url": HDHIVE_BASE + "/openapi/authorize?" + query})


@app.get("/api/oauth/hdhive/callback")
def oauth_callback():
    code = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    if not code or not state:
        return json_error("RE0 OAuth 回调缺少 code/state", 400, "OAUTH_CALLBACK_INVALID")
    current_actor = actor_id()
    now = utc_now()
    with connect_db() as db:
        row = db.execute(
            "SELECT * FROM oauth_states WHERE state=? AND created_by=? AND used_at IS NULL AND expires_at>=?",
            (state, current_actor, now),
        ).fetchone()
        if not row:
            return json_error("OAuth state 无效或已过期", 400, "OAUTH_STATE_INVALID")
        consumed_at = utc_now()
        consumed = db.execute(
            "UPDATE oauth_states SET used_at=? WHERE state=? AND created_by=? AND used_at IS NULL AND expires_at>=?",
            (consumed_at, state, current_actor, consumed_at),
        )
        if consumed.rowcount != 1:
            return json_error("OAuth state 无效或已被使用", 400, "OAUTH_STATE_INVALID")
    secret = config_value("hdhive_app_secret", "HDHIVE_APP_SECRET")
    client_id = config_value("hdhive_client_id", "HDHIVE_CLIENT_ID")
    if not secret or not client_id:
        return json_error("RE0 App Secret/Client ID 尚未配置", 503, "HDHIVE_CREDENTIALS_MISSING")
    redirect_uri = PUBLIC_ORIGIN + "/api/oauth/hdhive/callback"
    try:
        response = requests.post(HDHIVE_BASE + HDHIVE_TOKEN_PATH, headers={"X-API-Key": secret, "Accept": "application/json"}, json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}, timeout=20)
        data = response.json() if response.content else {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        code = "HDHIVE_INVALID_JSON" if isinstance(exc, (ValueError, TypeError)) else "HDHIVE_UNAVAILABLE"
        return json_error("RE0 OAuth 返回格式异常" if code == "HDHIVE_INVALID_JSON" else f"RE0 OAuth 换 token 失败：{type(exc).__name__}", 502, code)
    if response.status_code >= 400 or not data.get("success", False):
        safe_error = _safe_hdhive_envelope(data)
        return json_error(str(safe_error.get("message") or "RE0 OAuth 授权失败"), response.status_code if response.status_code >= 400 else 400, str(safe_error.get("code") or "OAUTH_EXCHANGE_FAILED"))
    try:
        save_tokens(data.get("data") or {})
    except RuntimeError as exc:
        return json_error(str(exc), 502, "OAUTH_TOKEN_INVALID")
    audit("hdhive.oauth.callback", "success", "encrypted access/refresh tokens stored", actor_id())
    return redirect("/?tab=settings&oauth=success")


@app.get("/api/hdhive/resources")
def api_hdhive_resources():
    media_type = request.args.get("media_type", "movie").strip().lower()
    tmdb_id = request.args.get("tmdb_id", "").strip()
    if media_type not in {"movie", "tv"} or not tmdb_id.isdigit():
        return json_error("media_type 必须为 movie/tv，tmdb_id 必须为数字")
    data, status = hdhive_request("GET", f"/api/open/resources/{media_type}/{tmdb_id}")
    return jsonify(_safe_hdhive_resources_response(data)), status


@app.post("/api/hdhive/checkin")
def api_hdhive_checkin():
    data, status = hdhive_request("POST", HDHIVE_CHECKIN_PATH, payload={})
    safe = _safe_hdhive_envelope(data)
    success = bool(safe.get("success")) and status < 400
    with connect_db() as db:
        db.execute("INSERT INTO checkins(success,code,message,created_at) VALUES(?,?,?,?)", (int(success), str(safe.get("code") or status), str(safe.get("message") or "")[:500], utc_now()))
    audit("hdhive.checkin", "success" if success else "failed", str(safe.get("code") or status), actor_id())
    return jsonify(safe), status


@app.post("/api/hdhive/unlock")
def api_hdhive_unlock():
    body = request_json()
    slug = str(body.get("slug") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", slug):
        return json_error("slug 格式不正确")
    allow_points = body.get("allow_points") is True
    # R07: the client's own flag is not an authorisation. RE0 documents no
    # promise that omitting `allow_points` cannot spend points -- the main
    # unlock path sends only the slug too -- so the decision is made here,
    # in the same order as every other consuming path (§10.1):
    #
    #   already materialised locally?  -> reuse it, free, for anybody
    #   otherwise                      -> the member policy decides
    materialised = _materialised_link_for_slug(slug)
    if materialised is not None:
        audit("hdhive.unlock", "success", "served from the local materialised link", actor_id())
        return jsonify({"success": True, "already_owned": True, "unlock_points": 0,
                        "link_public_id": materialised, "replayed": True})
    refused = member_unlock_refused("hdhive-unlock")
    if refused is not None:
        return refused

    # F05: this entry used to go straight upstream, so two concurrent callers
    # both spent. It takes the *same* lease key the main entry uses for this
    # resource -- resolved from the slug -- so one resource is one unlock
    # however it is reached.
    #
    # G03.2: and when the coordination store cannot be read, it stops. A
    # single-caller deployment could afford "carry on without coordination";
    # with several users that is exactly how two purchases happen. The master
    # key is needed both to resolve the slug (candidate rows are keyed by a
    # salted hash) and to save a success, so its absence is the same refusal.
    try:
        store, store_error = _library_store_or_error(True)
    except Exception:  # noqa: BLE001 - treated as unavailable, never as permission
        store, store_error = None, True
    if store is None or store_error:
        return json_error("资源库暂时不可读，无法安全协调解锁；请稍后重试", 503,
                          "RE0_COORDINATION_UNAVAILABLE")

    res, slug_hash_value = _re0_resource_for_slug(store, slug)
    if res is None:
        # G03.4: nothing local to attach a result to means nothing local to
        # make the next click free. Refuse before spending rather than buy
        # something this deployment cannot record.
        audit("hdhive.unlock", "refused", "no local candidate for this slug", actor_id())
        return json_error("本地还没有这条资源的候选记录，无法安全解锁；请先在资源页搜索该资源后重试",
                          409, "RE0_CANDIDATE_UNKNOWN")

    resource_id = int(res["id"])
    lease_key = re0_sync.resource_lease_key(resource_id)
    # G01.1-style identity: one per operation, so nothing can commit or
    # release under a lease that is no longer its own.
    holder = f"{actor_id()}:hdhive-unlock:{secrets.token_hex(8)}"
    if not _re0_lease_take(store, lease_key, holder=holder, now=utc_now()):
        again = _materialised_link_for_slug(slug)
        if again is not None:
            return jsonify({"success": True, "already_owned": True, "unlock_points": 0,
                            "link_public_id": again, "replayed": True})
        return json_error("该资源正在解锁中，请稍后重试", 409, "RE0_UNLOCK_IN_PROGRESS")
    with _re0_lease_held(store, lease_key, holder=holder):
        # F05: re-read inside the lease. The previous holder -- possibly the
        # main entry, for the same resource -- may have just materialised it.
        again = _materialised_link_for_slug(slug)
        if again is not None:
            audit("hdhive.unlock", "success", "served from the local materialised link", actor_id())
            return jsonify({"success": True, "already_owned": True, "unlock_points": 0,
                            "link_public_id": again, "replayed": True})
        # G03.3: the same two "do not buy it" states the main entry honours.
        pending = re0_sync.pending_unlock(store, resource_id)
        if pending is not None:
            public_id = _re0_materialise_legacy(store, res, slug, None, payload=pending)
            if public_id:
                return jsonify({"success": True, "already_owned": True, "unlock_points": 0,
                                "link_public_id": public_id, "replayed": True})
            return json_error("RE0 已解锁该资源，但本地保存失败；不会重复扣分，请稍后重试", 502,
                              "RE0_UNLOCK_PENDING_SAVE")
        if re0_sync.resource_state(store, resource_id) == re0_sync.STATE_RESULT_UNKNOWN:
            return json_error(_RE0_UNCERTAIN_MESSAGE, 409, "RE0_UNLOCK_RESULT_UNKNOWN")

        response, status = _hdhive_unlock_upstream(slug, allow_points, raw=True)
        if status >= 400 or not (isinstance(response, dict) and response.get("success")):
            if _hdhive_outcome_uncertain(response, status):
                # G03.3: unknown, not refused. Record it so the next click
                # asks the user rather than RE0.
                re0_sync.remember_unlock_unknown(store, resource_id,
                                                 error_class=str((response or {}).get("code") or status)[:64],
                                                 now=utc_now())
                audit("hdhive.unlock", "unknown", f"status={status} result unconfirmed", actor_id())
                return json_error(_RE0_UNCERTAIN_MESSAGE, 502, "RE0_UNLOCK_RESULT_UNKNOWN")
            return jsonify(_safe_hdhive_unlock_response(response)), status
        # F05/G03.3: a legacy success is recorded as confirmed *before* the
        # save is attempted, so the next caller through either entry resumes
        # instead of paying again.
        public_id = _re0_materialise_legacy(store, res, slug, response)
        safe = _safe_hdhive_unlock_response(response)
        if public_id:
            safe["link_public_id"] = public_id
        return jsonify(safe), status


# The upstream answers whose meaning is "we do not know whether this happened"
# (review G03.3). Everything else this endpoint can see is a refusal, and a
# refusal costs nothing.
_HDHIVE_UNCERTAIN_CODES = frozenset({"UPSTREAM_UNAVAILABLE", "UPSTREAM_INVALID_JSON"})


def _hdhive_outcome_uncertain(response, status: int) -> bool:
    if isinstance(response, dict) and str(response.get("code") or "") in _HDHIVE_UNCERTAIN_CODES:
        return True
    return status >= 500


def _hdhive_unlock_upstream(slug: str, allow_points: bool, *, raw: bool = False):
    """The upstream call itself, audited the same way it always was."""
    payload = {"slug": slug}
    if allow_points:
        payload["allow_points"] = True
    data, status = hdhive_request("POST", "/api/open/resources/unlock", payload=payload)
    if status < 400 and data.get("success"):
        audit("hdhive.unlock", "success", "points unlock requested" if allow_points else "free or already-owned resource; link omitted from audit", actor_id())
    return (data, status) if raw else (jsonify(_safe_hdhive_unlock_response(data)), status)


def _re0_materialise_legacy(store, res: dict, slug: str, response, *, payload: dict | None = None) -> str | None:
    """Save a legacy-entry unlock into the local library, insert-only.

    Returns the local link's public id, or None when the payload carried no
    URL, the media could not be resolved, or the save failed -- and in the
    last case the confirmed result is left **pending** (G03.3), so the next
    click through either entry resumes it for free. Reporting a failure to
    *save* as a failure to unlock is what invites paying twice (F05).
    """
    now = utc_now()
    try:
        unlocked = payload if payload is not None else re0_sync.parse_unlock_payload(
            response.get("data") or response, slug)
        if not unlocked.get("url"):
            return None
        media_id = res.get("media_id")
        if media_id is None and res.get("tmdb_id"):
            conn = store.connect(readonly=True)
            try:
                media_id = re0_sync.local_media_id_for(conn, res["media_type"], int(res["tmdb_id"]))
            finally:
                conn.close()
        if media_id is None:
            media_id = re0_sync.create_media_from_projection(store, res["media_type"], int(res["tmdb_id"]), now)
        if media_id is None:
            # Still confirmed, still unsaved: record it rather than forget it.
            re0_sync.remember_unlock_pending(
                store, int(res["id"]), url=unlocked["url"], access_code=unlocked.get("access_code"),
                points=unlocked.get("points"), already_owned=bool(unlocked.get("already_owned")), now=now)
            return None
        outcome, failure = _re0_save_unlocked(store, int(res["id"]), unlocked, media_id=media_id,
                                              media_title=res.get("media_title"), now=now)
        if outcome is None:
            audit("hdhive.unlock", "failed",
                  f"unlocked upstream but could not be saved locally class={failure}", actor_id())
            return None
        conn = store.connect(readonly=True)
        try:
            link = conn.execute("SELECT public_id FROM resource_link WHERE id=?",
                                (outcome["resource_link_id"],)).fetchone()
        finally:
            conn.close()
        re0_sync.clear_unlock_pending(store, int(res["id"]), now=now)
        return link["public_id"] if link is not None else None
    except (ValueError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
        audit("hdhive.unlock", "failed",
              f"unlocked upstream but could not be saved locally class={type(exc).__name__}", actor_id())
        return None


@app.get("/api/hdhive/search")
def api_hdhive_search():
    """Search the same TMDB catalogue RE0 uses, then let the user open resources."""
    key = config_value("tmdb_api_key", "TMDB_API_KEY")
    query = request.args.get("q", "").strip()
    media_type = request.args.get("media_type", "multi").strip().lower()
    if not key:
        return json_error("请先配置 TMDB API Key，影巢按影片名搜索需要它", 503, "TMDB_KEY_MISSING")
    if not query or len(query) > 120 or media_type not in {"movie", "tv", "multi"}:
        return json_error("请输入影片名，并选择电影、剧集或全部")

    def search_tmdb(language: str) -> tuple[list[dict], str | None]:
        try:
            response = requests.get(
                f"https://api.themoviedb.org/3/search/{media_type}",
                params={"api_key": key, "query": query, "language": language, "page": 1, "include_adult": "false"},
                timeout=15,
            )
            data = response.json() if response.content else {}
        except requests.RequestException as exc:
            return [], f"TMDB 请求失败：{type(exc).__name__}"
        except (ValueError, TypeError):
            return [], "TMDB 返回格式异常"
        if response.status_code >= 400 or not isinstance(data, dict) or not isinstance(data.get("results"), list):
            return [], "TMDB 搜索不可用"
        return [row for row in data["results"] if isinstance(row, dict)], None

    rows, search_error = search_tmdb("zh-CN")
    if not rows:
        rows, fallback_error = search_tmdb("en-US")
        search_error = fallback_error or search_error
    if not rows and search_error:
        return json_error(search_error, 502, "TMDB_SEARCH_FAILED")
    normalized: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows[:30]:
        kind = str(row.get("media_type") or media_type).lower()
        if kind not in {"movie", "tv"} or not str(row.get("id") or "").isdigit():
            continue
        key_tuple = (kind, str(row["id"]))
        if key_tuple in seen:
            continue
        seen.add(key_tuple)
        normalized.append(
            {
                "id": int(row["id"]),
                "media_type": kind,
                "title": str(row.get("title") or row.get("name") or "未命名"),
                "original_title": str(row.get("original_title") or row.get("original_name") or ""),
                "date": str(row.get("release_date") or row.get("first_air_date") or "")[:10],
                "overview": str(row.get("overview") or ""),
            }
        )
    return jsonify({"success": True, "source": "TMDB + RE0 OpenAPI", "results": normalized})


@app.get("/api/tmdb/search")
def api_tmdb_search():
    key = config_value("tmdb_api_key", "TMDB_API_KEY")
    query = request.args.get("q", "").strip()
    media_type = request.args.get("media_type", "movie").strip().lower()
    if not key:
        return json_error("未配置 TMDB API Key；可直接输入 TMDB ID", 503, "TMDB_KEY_MISSING")
    if not query or media_type not in {"movie", "tv"}:
        return json_error("请输入关键词并选择 movie/tv")
    try:
        response = requests.get(f"https://api.themoviedb.org/3/search/{media_type}", params={"api_key": key, "query": query, "language": "zh-CN", "page": 1}, timeout=15)
        return jsonify(response.json()), response.status_code
    except (requests.RequestException, ValueError, TypeError) as exc:
        if isinstance(exc, (ValueError, TypeError)):
            return json_error("TMDB 返回格式异常", 502, "TMDB_INVALID_JSON")
        return json_error(f"TMDB 请求失败：{type(exc).__name__}", 502, "TMDB_UNAVAILABLE")


@app.post("/api/115/save")
def api_115_save():
    """T19 wave 3 item 1 / wave 4 items 1-3/5: the single request-scoped
    deadline (g.request_started + _115_REQUEST_DEADLINE_SECONDS=24s) is
    threaded through _resolve_115_target_pid's OpenList listing (the UI
    always sends target_path), _115_transfer_gate/_115_verify_with_uid and
    save_115_link's share/snap (+ its one retry) and share/receive.
    g.request_started is recorded as the FIRST statement of
    before_request -- before Access/JWKS work -- so LESS than the full
    24s may already remain by the time this view body runs; it is not a
    fresh 24s window starting here.

    Every upstream call's own timeout is a (connect, read) tuple --
    (min(_115_UPSTREAM_CONNECT_TIMEOUT=3, remaining),
    min(_115_UPSTREAM_TIMEOUT=8, remaining)); share/snap's retry only runs
    with >=_115_SNAP_RETRY_MIN_REMAINING=12s left; share/receive is only
    issued with >=_115_RECEIVE_MIN_REMAINING=3s left, its own read timeout
    clamped by whatever remains of the shared deadline (min(8,
    remaining)) -- otherwise a fixed 504 TRANSFER_NOT_ATTEMPTED is
    returned and share/receive is never called, since an ambiguous cutoff
    there risks a duplicate transfer. A budget_exhausted verify (the
    shared budget, not 115 itself, ran out) returns this same fixed 504
    without touching cookie state. Either way the request stays
    comfortably under gunicorn's 30s worker timeout, and share/receive --
    the one call that must never be cut off mid-flight -- is never the one
    its timeout kills.

    T17 fix wave 1 item 1: the request body's ``target_path`` is display
    text only, never a security boundary -- ``_resolve_115_target_pid``
    always proves ``target_pid`` server-side (against its own path->pid
    resolution, the stored default, or the configured /115pan root cid)
    before it is ever used as the upstream 115 ``cid``; a pid that can't be
    proven is rejected with 400 ``TARGET_PID_INVALID`` and ``save_115_link``
    (hence the actual 115 transfer call) is never reached."""
    deadline = g.request_started + _115_REQUEST_DEADLINE_SECONDS
    body = request_json()
    link = extract_share_link(body.get("share_url"))
    pid, target_error = _resolve_115_target_pid(body, deadline)
    if target_error:
        return target_error
    if not link:
        return json_error("请输入带提取码的 115 分享链接")
    request_id = secrets.token_hex(8)
    started = time.monotonic()
    result = save_115_link(link, pid, deadline)
    duration_ms = int((time.monotonic() - started) * 1000)
    audit("115.save", "success" if result.success else "failed", f"req={request_id} code={result.code or 'OK'} status={result.status} duration_ms={duration_ms}", actor_id())
    return _transfer_response(result)


@app.post("/api/115/reauth/start")
def api_115_reauth_start():
    actor = actor_id()
    now = utc_now()
    # Item 4: sweep this actor's abandoned pending/scanned challenges into
    # 'expired' (so they count toward the cooldowns below) and purge old
    # settled rows, before making any cooldown decision.
    #
    # T19 wave 3 item 3: the sweep above also expires a 'consuming' row
    # once its claim is stale (REAUTH_CLAIM_STALE_SECONDS) -- so any
    # 'consuming' row still standing afterwards is a genuinely fresh claim
    # (an /reauth/status poll actively mid-exchange), which must count as
    # "in progress" here too, not just 'pending'/'scanned'.
    #
    # w6-reauth-longpoll-fix: 'confirmed' (115 confirmed the QR; the cookie
    # exchange itself is deferred to the browser's next status request)
    # is likewise still in progress -- starting a brand new challenge on
    # top of it would orphan a login the user already approved on their
    # phone.
    _reauth_expire_abandoned(actor, now)
    _reauth_purge_old(now)
    with connect_db() as db:
        active = db.execute(
            "SELECT 1 FROM reauth_challenges WHERE actor=? AND state IN ('pending','scanned','confirmed','consuming') AND consumed_at IS NULL AND expires_at > ?",
            (actor, now),
        ).fetchone()
    if active:
        return json_error("已有正在进行的重新授权，请先完成或取消", 409, "REAUTH_IN_PROGRESS")
    if _reauth_cooldown_count(actor) >= REAUTH_COOLDOWN_THRESHOLD or _reauth_starts_count(actor) >= REAUTH_MAX_STARTS_PER_WINDOW:
        return json_error("重新授权次数过多，请稍后再试", 429, "REAUTH_COOLDOWN")
    try:
        response = requests.get(QR_LOGIN_BASE + "/api/1.0/web/1.0/token/", timeout=_115_UPSTREAM_TIMEOUT)
        data = response.json() if response.content else {}
    except (requests.RequestException, ValueError, TypeError):
        return json_error("115 暂时不可用，请稍后重试", 503, "115_TEMPORARILY_UNAVAILABLE")
    qr_data = data.get("data") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not data.get("state") or not isinstance(qr_data, dict) or not qr_data.get("uid") or not qr_data.get("sign") or qr_data.get("time") is None:
        return json_error("115 返回异常，请稍后重试", 502, "115_PROVIDER_ERROR")
    challenge_id = secrets.token_urlsafe(32)
    id_hash = _reauth_hash(challenge_id)
    fernet = load_fernet()
    expires_at = now + REAUTH_TTL_SECONDS
    with connect_db() as db:
        db.execute(
            "INSERT INTO reauth_challenges(id_hash,actor,user_id,state,created_at,expires_at,qr_uid_cipher,qr_time,qr_sign_cipher) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                id_hash, actor, (current_user().id if current_user() else None), "pending", now, expires_at,
                fernet.encrypt(str(qr_data["uid"]).encode("utf-8")),
                int(qr_data["time"]),
                fernet.encrypt(str(qr_data["sign"]).encode("utf-8")),
            ),
        )
    setting_set("115_cookie_reauth_started_at", str(now))
    audit("115.reauth.start", "success", f"expires_in={REAUTH_TTL_SECONDS}", actor)
    return jsonify({"success": True, "challenge_id": challenge_id, "expires_at": iso(expires_at), "qr_url": "/api/115/reauth/qr?challenge_id=" + quote(challenge_id)})


def _read_capped(response, max_bytes: int) -> bytes | None:
    """Read a streamed response body, bailing out (returning ``None``) the
    moment it exceeds ``max_bytes`` -- a misbehaving/compromised upstream
    must never make this proxy buffer or forward more than that, regardless
    of what (or whether) Content-Length claims."""
    total = 0
    chunks: list[bytes] = []
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@app.get("/api/115/reauth/qr")
def api_115_reauth_qr():
    challenge_id = request.args.get("challenge_id", "").strip()
    if not challenge_id:
        return json_error("缺少 challenge_id", 400, "REAUTH_CHALLENGE_MISSING")
    row = _reauth_row(_reauth_hash(challenge_id))
    if not row or row["actor"] != actor_id() or row["state"] != "pending" or row["consumed_at"] is not None or row["expires_at"] <= utc_now():
        return json_error("二维码不存在或已失效", 404, "REAUTH_NOT_FOUND")
    try:
        uid = load_fernet().decrypt(bytes(row["qr_uid_cipher"])).decode("utf-8")
    except (InvalidToken, ValueError):
        return json_error("二维码不存在或已失效", 404, "REAUTH_NOT_FOUND")
    try:
        # Item 7 (wave 1) + N5 (wave 2): stream + a bounded read, require
        # the upstream Content-Type to be *exactly* image/png -- not
        # merely image/* (which would also accept e.g. image/svg+xml; an
        # SVG body can carry embedded script and must never be proxied to
        # the browser) -- and always close the streamed upstream response,
        # on every exit path, so this proxy never leaks a connection.
        response = requests.get(QR_LOGIN_BASE + "/api/1.0/web/1.0/qrcode", params={"uid": uid}, timeout=_115_UPSTREAM_TIMEOUT, stream=True)
    except requests.RequestException:
        return json_error("二维码暂时不可用，请稍后重试", 503, "115_TEMPORARILY_UNAVAILABLE")
    try:
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if response.status_code >= 400 or content_type != "image/png":
            return json_error("二维码暂时不可用，请稍后重试", 503, "115_TEMPORARILY_UNAVAILABLE")
        body = _read_capped(response, QR_IMAGE_MAX_BYTES)
        if not body:
            return json_error("二维码暂时不可用，请稍后重试", 503, "115_TEMPORARILY_UNAVAILABLE")
        image = Response(body, mimetype="image/png")
        image.headers["Cache-Control"] = "no-store"
        image.headers["X-Content-Type-Options"] = "nosniff"
        return image
    finally:
        response.close()


@app.get("/api/115/reauth/status")
def api_115_reauth_status():
    """w6-reauth-longpoll: ``deadline`` is this request's own share of the
    existing request-scoped budget (``g.request_started +
    _115_REQUEST_DEADLINE_SECONDS=24s``), threaded into
    ``_reauth_poll_upstream`` so its long-poll read timeout never outlives
    it. Worst case per poll: a ``_115_UPSTREAM_CONNECT_TIMEOUT=3s`` connect
    timeout plus a read timeout capped at
    REAUTH_STATUS_POLL_READ_SECONDS=15s -- comfortably under gunicorn's 30s
    worker timeout.

    w6-reauth-longpoll-fix: state machine ``pending -> scanned -> confirmed
    -> authenticated | failed | expired | cancelled`` (``consuming`` is the
    internal, never-client-visible value a row holds while claimed by one
    in-flight request -- see the claim UPDATE below; a losing poll reports
    ``claimed_from`` instead). The old design ran the upstream poll AND, on
    the same request that first observed status 2, the cookie-exchange
    completion (``_reauth_complete``) -- stacking an up-to-~18s long-poll
    hold with another ~19s of exchange+verify network calls risked
    exceeding gunicorn's 30s worker timeout. This is now split across two
    requests via the non-terminal ``confirmed`` state:

    1. A poll request whose upstream call observes status 2 writes
       ``state='confirmed'`` (claim released, not terminal, no audit yet)
       and returns ``{"state": "confirmed"}`` immediately -- the browser
       shows "已确认，正在完成登录…" and keeps polling.
    2. The browser's very next status request claims the row (the claim
       UPDATE below also matches ``state='confirmed'``); when
       ``claimed_from == 'confirmed'`` this request skips the upstream poll
       entirely and runs ``_reauth_complete`` with its own fresh deadline
       budget, then the usual terminal UPDATE + same-transaction audit.

    A 'confirmed' row is still swept by ``_reauth_expire_abandoned`` once
    past its own TTL (a confirmed-but-abandoned tab must not wait forever),
    still blocks a new ``/reauth/start`` as "in progress", and is still
    cancellable."""
    deadline = g.request_started + _115_REQUEST_DEADLINE_SECONDS
    challenge_id = request.args.get("challenge_id", "").strip()
    if not challenge_id:
        return json_error("缺少 challenge_id", 400, "REAUTH_CHALLENGE_MISSING")
    id_hash = _reauth_hash(challenge_id)
    actor = actor_id()
    row = _reauth_row(id_hash)
    if not row or row["actor"] != actor:
        return json_error("未找到该授权流程", 404, "REAUTH_NOT_FOUND")
    if row["consumed_at"] is not None:
        return json_error("该授权流程已结束", 410, "REAUTH_CONSUMED")
    now = utc_now()
    # Item 1: claim the row before doing any upstream work (poll, or --
    # w6-reauth-longpoll-fix -- the confirmed -> cookie-exchange completion,
    # now only ever attempted from a row THIS request just claimed out of
    # 'confirmed') via a conditional UPDATE. Two concurrent polls
    # (gunicorn's 2 sync workers, the frontend's poll chain racing a
    # cancel/refresh) would otherwise both read the same 'confirmed' row and
    # both call _reauth_complete -- double exchange, double secret_set,
    # double audit. SQLite serialises this UPDATE across
    # connections/threads/processes, so only one caller can ever see
    # rowcount > 0 for a given row; the loser just re-reads and returns
    # whatever the winner (or an earlier request) already left.
    # N1 (wave 2): claimed_at/claimed_from are recorded so (a)
    # _reauth_expire_abandoned can eventually recover a claim whose worker
    # never finishes (killed / uncaught exception) and (b) a losing poll
    # can report the *pre-claim* state instead of the internal-only
    # 'consuming' value -- claimed_from=state captures the old row value
    # (pending/scanned/confirmed) in the same UPDATE, before it is
    # overwritten.
    with connect_db() as db:
        claim = db.execute(
            "UPDATE reauth_challenges SET state='consuming', claimed_at=?, claimed_from=state "
            "WHERE id_hash=? AND consumed_at IS NULL AND state IN ('pending','scanned','confirmed')",
            (now, id_hash),
        )
        claimed = claim.rowcount > 0
    if not claimed:
        row = _reauth_row(id_hash)
        if row is None:
            return json_error("未找到该授权流程", 404, "REAUTH_NOT_FOUND")
        if row["consumed_at"] is not None:
            return json_error("该授权流程已结束", 410, "REAUTH_CONSUMED")
        visible_state = row["claimed_from"] if row["state"] == "consuming" else row["state"]
        return jsonify({"success": True, "state": visible_state, "expires_at": iso(row["expires_at"])})
    # N1 (wave 2): any uncaught exception from here on (poll/exchange
    # raising, a DB error releasing the claim, ...) must not leave the row
    # stuck in 'consuming' -- mark it 'failed' (finally-style) and re-raise
    # so the caller still sees the usual error response.
    try:
        state = row["state"]
        if state not in REAUTH_TERMINAL_STATES and now >= row["expires_at"]:
            state, error_code = "expired", "EXPIRED"
        elif state == "confirmed":
            # w6-reauth-longpoll-fix: second half of the confirm/complete
            # split -- a previous poll already observed status 2 and left
            # this row 'confirmed'; skip the upstream poll entirely and run
            # the cookie exchange with THIS request's own full deadline
            # budget, instead of racing the read timeout that already
            # observed the confirmation.
            state, error_code = _reauth_complete(row, deadline)
        elif state not in REAUTH_TERMINAL_STATES:
            state, error_code = _reauth_poll_upstream(row, deadline)
        else:
            error_code = row["error_code"]
        if state in REAUTH_TERMINAL_STATES:
            # N1 (wave 2): the terminal UPDATE and its audit row are written
            # in the same transaction (same connection, one commit) -- a
            # crash between them (e.g. right after _reauth_complete's own
            # secret_set) can never leave a committed 'authenticated' state
            # without its matching audit row, or vice versa.
            #
            # T19 wave 3 item 4: the UPDATE is conditional on consumed_at
            # IS NULL -- a /reauth/cancel that raced in and committed first
            # (e.g. the user closed the dialog while this poll's exchange
            # was still in flight) must win: this UPDATE then matches 0
            # rows, so the row's *actual* stored terminal state (cancelled)
            # is reported instead of the one this request just computed,
            # and a distinct audit action records the mismatch rather than
            # a fabricated success/failure row for a state that was never
            # actually committed.
            action = _REAUTH_AUDIT_ACTION.get(state, "115.reauth." + state)
            with connect_db() as db:
                terminal_update = db.execute(
                    "UPDATE reauth_challenges SET state=?, error_code=?, consumed_at=? WHERE id_hash=? AND consumed_at IS NULL",
                    (state, error_code, now, id_hash),
                )
                if terminal_update.rowcount > 0:
                    audit(action, "success" if state == "authenticated" else "failed", f"terminal_state={state} error={error_code or ''}", actor, db=db)
                else:
                    audit("115.reauth.completed_after_cancel", "failed", f"terminal_state={state} error={error_code or ''}", actor, db=db)
            if terminal_update.rowcount == 0:
                stored = _reauth_row(id_hash)
                return jsonify({"success": True, "state": stored["state"], "expires_at": iso(row["expires_at"])})
            return jsonify({"success": True, "state": state, "expires_at": iso(row["expires_at"])})
        # Not terminal yet ('pending'/'scanned'/'confirmed') -- release the
        # 'consuming' claim back to a real state so the next poll can
        # proceed normally. Item 4 (minor, w6-reauth-longpoll-fix):
        # conditional on consumed_at IS NULL for parity with the terminal
        # UPDATE above -- a concurrent /reauth/cancel that already
        # committed a terminal consumed_at here must not be clobbered back
        # to a stale non-terminal value.
        with connect_db() as db:
            db.execute("UPDATE reauth_challenges SET state=? WHERE id_hash=? AND consumed_at IS NULL", (state, id_hash))
        return jsonify({"success": True, "state": state, "expires_at": iso(row["expires_at"])})
    except Exception:
        with connect_db() as db:
            failure_update = db.execute(
                "UPDATE reauth_challenges SET state='failed', error_code=?, consumed_at=? WHERE id_hash=? AND consumed_at IS NULL",
                ("INTERNAL_ERROR", now, id_hash),
            )
            # T19 wave 3 item 4: only audit this as our own failure if our
            # UPDATE actually matched a row -- if a concurrent cancel
            # already set consumed_at first, this row's terminal state is
            # already whatever that cancel recorded, and there is nothing
            # for this exception-guard to report.
            if failure_update.rowcount > 0:
                audit("115.reauth.failed", "failed", "terminal_state=failed error=INTERNAL_ERROR", actor, db=db)
        raise


@app.post("/api/115/reauth/cancel")
def api_115_reauth_cancel():
    body = request_json()
    challenge_id = str(body.get("challenge_id") or "").strip()
    if not challenge_id:
        return json_error("缺少 challenge_id", 400, "REAUTH_CHALLENGE_MISSING")
    id_hash = _reauth_hash(challenge_id)
    actor = actor_id()
    row = _reauth_row(id_hash)
    if not row or row["actor"] != actor:
        return json_error("未找到该授权流程", 404, "REAUTH_NOT_FOUND")
    # N1 (wave 2): 'consuming' is also cancellable -- otherwise a challenge
    # stuck there by a killed/crashed worker has no way out except waiting
    # for _reauth_expire_abandoned's REAUTH_CLAIM_STALE_SECONDS sweep.
    # w6-reauth-longpoll-fix: 'confirmed' (115 confirmed the QR; the cookie
    # exchange is deferred to the next status request) is cancellable too --
    # the user can still back out before that follow-up request runs.
    if row["consumed_at"] is not None or row["state"] not in ("pending", "scanned", "confirmed", "consuming"):
        return json_error("该授权流程已结束", 410, "REAUTH_CONSUMED")
    now = utc_now()
    with connect_db() as db:
        db.execute("UPDATE reauth_challenges SET state='cancelled', error_code='CANCELLED_BY_USER', consumed_at=? WHERE id_hash=?", (now, id_hash))
    audit("115.reauth.cancel", "success", "cancelled by actor", actor)
    return jsonify({"success": True, "state": "cancelled"})


@app.get("/api/115/folders")
def api_115_folders():
    """The folder picker.

    A user with their own step-B authorisation browses their own 115 by cid
    (Phase 6). The administrator's legacy OpenList path stays while the
    migration window is open -- it is the same account, reached the way it
    always was. A member with no authorisation is told which button to press,
    never shown somebody else's folders (R05).
    """
    user = current_user()
    is_admin = bool(user and user.role == "admin")
    if user_115_open_token(user) and (_has_own_open_token(user) or not is_admin):
        cid = str(request.args.get("cid") or "0").strip() or "0"
        items, status, message = user_115_folders(user, cid)
        if status != 200:
            return jsonify({"success": False, "code": "OPEN115_FOLDER_FAILED", "message": message}), status
        return jsonify({"success": True, "mode": "115", "cid": cid, "items": items})

    if not is_admin:
        _token, error = _require_open115(user)
        return error if error is not None else json_error("尚未完成「目录与云下载」授权", 409, "OPEN115_NOT_AUTHORIZED")

    root = normalized_path(OPENLIST_115PAN_PATH)
    path = normalized_path(request.args.get("path", root))
    if not path_under(root, path):
        return json_error("目录必须位于 OpenList 的 115pan 存储下", 400, "OPENLIST_PATH_INVALID")
    folders, status, message = openlist_folder_items(path)
    if status >= 400:
        return json_error(message, status, "OPENLIST_LIST_FAILED")
    parent = root if path == root else normalized_path(posixpath.dirname(path))
    return jsonify({"success": True, "mode": "openlist", "root": root, "path": path,
                    "parent": parent, "items": folders})


@app.get("/api/openlist/list")
def api_openlist_list():
    path = request.args.get("path", "/").strip() or "/"
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    data, status = list_openlist(path, page)
    return jsonify(data), status


@app.get("/api/strm/list")
def api_strm_list():
    path = request.args.get("path", "")
    try:
        return jsonify({"success": True, "root": str(STRM_ROOT), "path": path, "items": list_strm(path)})
    except (FileNotFoundError, ValueError) as exc:
        return json_error(str(exc), 404, "STRM_PATH_INVALID")


def run_library_install(bundle_arg: str) -> int:
    """``--library-install <bundle>``. Prints one JSON line (install_bundle's
    counts + ``"ok": true``), or ``{"ok": false, "error": "<ExceptionClassName>",
    "stage": "<install_bundle stage>"}`` on any failure. Both carry the
    Python and SQLite versions that ran the install (the release bridge
    relays nothing else) -- never a link, access code, or any value beyond
    counts/timestamps/stage names/version strings."""
    versions = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version}
    try:
        result = library_store.install_bundle(Path(bundle_arg), LIBRARY_DB_PATH, load_fernet())
    except Exception as exc:
        failure = {"ok": False, "error": type(exc).__name__, "stage": getattr(exc, "install_stage", None)}
        print(json.dumps({**failure, **versions}, ensure_ascii=False))
        return 1
    print(json.dumps({**result, "ok": True, **versions}, ensure_ascii=False))
    return 0


def run_library_status() -> int:
    """``--library-status``. Prints the §7.6 JSON (+ ``"ok"``)."""
    try:
        store = library_store.open_installed(LIBRARY_DB_PATH, load_fernet())
        payload = _library_tmdb_status_payload(store)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps({**payload, "ok": True}, ensure_ascii=False))
    return 0


def run_library_hints_report() -> int:
    """``--library-hints-report`` (T15 design item 4): read-only counts by
    decision for Codex's offline IMDb candidate hints -- no HTTP."""
    try:
        store = library_store.open_installed(LIBRARY_DB_PATH, load_fernet())
        payload = store.hint_stats()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps({**payload, "ok": True}, ensure_ascii=False))
    return 0


def run_library_rollback() -> int:
    """``--library-rollback``."""
    try:
        result = library_store.rollback_install(LIBRARY_DB_PATH)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps({**result, "ok": True}, ensure_ascii=False))
    return 0


def run_library_enrich(max_media: int | None) -> int:
    """``--library-enrich [--max-media N]``: one manual round, sharing the
    same ``enrich_batch`` implementation and run lock as the background
    task (docs §7.7). The run lock is taken non-blocking -- a concurrent
    background/manual enrich means this exits immediately with
    ``TMDB_ENRICH_BUSY`` rather than waiting."""
    key = config_value("tmdb_api_key", "TMDB_API_KEY")
    if not key:
        print(json.dumps({"ok": False, "error": "TMDB_KEY_MISSING"}, ensure_ascii=False))
        return 1
    lock_paths = _tmdb_lock_paths()
    lock_paths.run.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_paths.run, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        print(json.dumps({"ok": False, "error": "TMDB_ENRICH_BUSY"}, ensure_ascii=False))
        return 1
    try:
        store = library_store.open_installed(LIBRARY_DB_PATH, load_fernet())
        limits = library_tmdb.effective_limits({"tmdb_daily_budget": setting_get("tmdb_daily_budget")}, os.environ)
        client = library_tmdb.TmdbClient(
            key, conn_factory=lambda: sqlite3.connect(LIBRARY_DB_PATH), lock_path=lock_paths.budget, limits=limits,
        )
        limit = max_media if max_media is not None else 20
        stats = library_tmdb.enrich_batch(store, client, limit=limit, tvmaze_enabled=_library_tvmaze_enabled_check())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    print(json.dumps(dataclasses.asdict(stats), ensure_ascii=False))
    return 0


_REQUEUE_REVIEW_BATCH_SIZE = 100
# Fields that describe a snapshot of the *latest* internal batch rather
# than a running total across batches -- summing them would be
# meaningless. Mirrors scripts/enrich_media_tmdb.py's own accumulate.
_REQUEUE_ACCUMULATE_OVERWRITE_FIELDS = {"budget_status", "queue_exhausted"}
_REQUEUE_ACCUMULATE_SKIP_FIELDS = {"elapsed_seconds"}


def _accumulate_requeue_stats(totals: "library_tmdb.EnrichStats", stats: "library_tmdb.EnrichStats") -> None:
    """Fold one ``requeue_review_batch`` call's stats into ``totals`` across
    the ``--library-requeue-review`` CLI's internal <=100-sized batches, so
    a new ``EnrichStats`` field is summed automatically instead of silently
    staying at its default."""
    for f in dataclasses.fields(library_tmdb.EnrichStats):
        name = f.name
        if name in _REQUEUE_ACCUMULATE_SKIP_FIELDS:
            continue
        if name == "error_class":
            if stats.error_class is not None:
                totals.error_class = stats.error_class
            continue
        if name in _REQUEUE_ACCUMULATE_OVERWRITE_FIELDS:
            setattr(totals, name, getattr(stats, name))
            continue
        setattr(totals, name, getattr(totals, name) + getattr(stats, name))


def run_library_requeue_review(*, max_media: int | None, resume: bool, dry_run: bool) -> int:
    """``--library-requeue-review [--max-media N] [--resume] [--dry-run]``
    (T14/§5.1): processes ONLY the never-queried ``needs_review`` backlog
    (§3.1: the 1,809-of-1,812 that have no match_score/candidates yet), in
    internal batches of <=100, under the same non-blocking run lock as
    ``--library-enrich``/the background enricher -- a concurrent
    background/manual enrich means this exits immediately with
    ``TMDB_ENRICH_BUSY``. ``--resume`` skips media already *judged* in
    today's ``tmdb_requeue_audit`` batch (fix wave 1/§3:
    ``processed_media_ids_for_batch``, ``processed_at IS NOT NULL`` --
    a row only *queued* by an interrupted previous run, never judged, is
    deliberately retried). A row a TMDB search fails on (transient or
    permanent) is likewise left unjudged (fix wave 2/finding #1) and is
    additionally excluded from this invocation's own remaining internal
    batches via an in-run exclusion set built from each batch's
    ``EnrichStats.search_failed_media_ids`` -- a later invocation (or day)
    starts fresh and retries it. ``--dry-run`` makes no HTTP calls or
    writes -- it only reports counts. Output is JSON: counts, requests,
    429s, error class and ``elapsed_seconds`` (wall time across every
    internal batch) only -- never a title or link."""
    try:
        store = library_store.open_installed(LIBRARY_DB_PATH, load_fernet())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1

    today = datetime.now(timezone.utc).date().isoformat()

    if dry_run:
        conn = store.connect(readonly=True)
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM media WHERE match_status = 'needs_review' AND match_score IS NULL "
                "AND (match_candidates_json IS NULL OR match_candidates_json = '')"
            ).fetchone()[0]
            audited_today = len(library_tmdb.audited_media_ids_for_batch(conn, today))
        finally:
            conn.close()
        print(json.dumps(
            {"ok": True, "dry_run": True, "review_pending_unqueried": pending, "audited_today": audited_today},
            ensure_ascii=False,
        ))
        return 0

    key = config_value("tmdb_api_key", "TMDB_API_KEY")
    if not key:
        print(json.dumps({"ok": False, "error": "TMDB_KEY_MISSING"}, ensure_ascii=False))
        return 1

    lock_paths = _tmdb_lock_paths()
    lock_paths.run.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_paths.run, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        print(json.dumps({"ok": False, "error": "TMDB_ENRICH_BUSY"}, ensure_ascii=False))
        return 1
    try:
        limits = library_tmdb.effective_limits({"tmdb_daily_budget": setting_get("tmdb_daily_budget")}, os.environ)
        client = library_tmdb.TmdbClient(
            key, conn_factory=lambda: sqlite3.connect(LIBRARY_DB_PATH), lock_path=lock_paths.budget, limits=limits,
        )
        exclude_ids: frozenset = frozenset()
        if resume:
            conn = store.connect(readonly=True)
            try:
                # Fix wave 1/§3: only a media id today's audit table marks
                # actually *judged* (processed_at IS NOT NULL) is skipped --
                # one merely queued by an aborted previous run must be
                # retried, not silently treated as done.
                exclude_ids = library_tmdb.processed_media_ids_for_batch(conn, today)
            finally:
                conn.close()

        # T14 fix wave 2/finding #1: media ids this invocation's own
        # internal batches saw fail with a TMDB search failure (score left
        # untouched, never a genuine verdict) are added here as they come
        # in, so the next <=100-row internal batch below does not keep
        # re-selecting the same failing row -- it stays eligible again for
        # a later invocation (or day), which starts with a fresh set.
        in_run_excluded = set(exclude_ids)

        totals = library_tmdb.EnrichStats()
        overall_started = time.monotonic()
        remaining = max_media
        while remaining is None or remaining > 0:
            batch_limit = _REQUEUE_REVIEW_BATCH_SIZE if remaining is None else min(_REQUEUE_REVIEW_BATCH_SIZE, remaining)
            stats = library_tmdb.requeue_review_batch(
                store, client, limit=batch_limit, exclude_ids=frozenset(in_run_excluded), today=lambda: today,
                tvmaze_enabled=_library_tvmaze_enabled_check(),
            )
            in_run_excluded.update(stats.search_failed_media_ids)
            _accumulate_requeue_stats(totals, stats)
            if remaining is not None:
                remaining -= stats.candidates_considered
            if stats.candidates_considered == 0 or stats.error_class is not None or stats.queue_exhausted:
                break
        totals.elapsed_seconds = time.monotonic() - overall_started
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    print(json.dumps(dataclasses.asdict(totals), ensure_ascii=False))
    return 0


def run_library_check_links(*, provider: str | None, sample: int | None, dry_run: bool, as_json: bool) -> int:
    """``--library-check-links [--provider CODE] [--sample N] [--dry-run]
    [--json]`` (w6-contract §6): runs the same LinkCheckClient/adapter/
    pacing/budget machinery as the background thread, for Codex's
    calibration runs and manual inspection. ``--provider`` bypasses the
    settings-page enabled gate ONLY together with ``--dry-run`` (I2: a
    provider is calibrated BEFORE it's ever switched on, but calibration
    never writes or spends budget in the first place); without
    ``--dry-run``, an explicit ``--provider`` still respects
    ``linkcheck_enabled``/that provider's own enabled flag exactly like a
    real background round would -- a disabled provider is reported as
    skipped rather than actually probed. With no ``--provider``, only the
    currently-enabled providers are checked either way.
    ``--sample N`` probes N random live links per provider regardless of
    due status; without it, only currently-due links are selected.
    ``--dry-run`` classifies without writing to ``link_check`` or spending
    any provider's daily budget. Output is counts by status/reason per
    provider, elapsed time and request counts only -- NEVER a URL, access
    code or response body. Always exits 0 (a per-run failure is reported
    inside the JSON body, so a calibration script iterating providers
    never gets aborted by one bad exit code)."""
    if provider is not None and provider not in library_tmdb.LINK_CHECK_PROVIDERS:
        result = {"ok": False, "error": "UnknownProvider", "provider": provider}
        print(json.dumps(result, ensure_ascii=False))
        return 0

    settings = _linkcheck_settings_dict()
    if provider is not None and not dry_run and not library_tmdb._linkcheck_provider_enabled(settings, provider):
        # I2/M6: never bypass the kill switch outside of --dry-run -- report
        # the skip (with the contract's "provider_disabled" reason) instead
        # of probing a provider the settings page has switched off.
        result = {
            "ok": True, "provider": provider, "dry_run": dry_run,
            "skipped": "disabled", "reason": "provider_disabled",
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0

    try:
        store = library_store.open_installed(LIBRARY_DB_PATH, load_fernet())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 0

    providers = [provider] if provider else None
    client = library_tmdb.LinkCheckClient(
        conn_factory=lambda: sqlite3.connect(LIBRARY_DB_PATH),
        settings=settings,
        session=_linkcheck_session_factory(),
    )
    try:
        stats = library_tmdb.run_link_check_round(
            store, client, settings,
            limit=sample or 100, providers=providers, sample=sample, dry_run=dry_run,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 0

    payload = {
        "ok": True,
        "dry_run": dry_run,
        "elapsed_seconds": round(stats.elapsed_seconds, 3),
        "requests": stats.checked,
        "checked": stats.checked,
        "valid": stats.valid,
        "invalid": stats.invalid,
        "unknown": stats.unknown,
        "by_provider": stats.by_provider,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for code, provider_stats in stats.by_provider.items():
            print(
                f"{code}: checked={provider_stats['checked']} valid={provider_stats['valid']} "
                f"invalid={provider_stats['invalid']} unknown={provider_stats['unknown']} "
                f"reasons={provider_stats['reasons']} signals={provider_stats['signals']}"
            )
        print(f"elapsed={payload['elapsed_seconds']}s requests={payload['requests']}")
    return 0


def run_checkin() -> int:
    init_db()
    data, status = hdhive_request("POST", HDHIVE_CHECKIN_PATH, payload={})
    success = bool(data.get("success")) and status < 400
    with connect_db() as db:
        db.execute("INSERT INTO checkins(success,code,message,created_at) VALUES(?,?,?,?)", (int(success), str(data.get("code") or status), str(data.get("message") or "")[:500], utc_now()))
    audit("hdhive.checkin.timer", "success" if success else "failed", str(data.get("code") or status), "system")
    print(json.dumps({"success": success, "code": data.get("code"), "message": data.get("message")}, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    # R10: the read-only command is dispatched before anything creates a
    # table. Every other command still initialises first, as it always has.
    if not _read_only_cli():
        init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "--checkin":
        raise SystemExit(run_checkin())
    if len(sys.argv) > 1 and sys.argv[1] == "--re0-sync":
        raise SystemExit(run_re0_sync(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--library-install":
        if len(sys.argv) < 3:
            print(json.dumps({"ok": False, "error": "MissingBundleArgument"}, ensure_ascii=False))
            raise SystemExit(1)
        raise SystemExit(run_library_install(sys.argv[2]))
    if len(sys.argv) > 1 and sys.argv[1] == "--library-status":
        raise SystemExit(run_library_status())
    if len(sys.argv) > 1 and sys.argv[1] == "--library-hints-report":
        raise SystemExit(run_library_hints_report())
    if len(sys.argv) > 1 and sys.argv[1] == "--library-rollback":
        raise SystemExit(run_library_rollback())
    if len(sys.argv) > 1 and sys.argv[1] == "--library-enrich":
        cli_max_media = None
        if "--max-media" in sys.argv:
            idx = sys.argv.index("--max-media")
            if idx + 1 < len(sys.argv):
                cli_max_media = int(sys.argv[idx + 1])
        raise SystemExit(run_library_enrich(cli_max_media))
    if len(sys.argv) > 1 and sys.argv[1] == "--library-requeue-review":
        cli_max_media = None
        if "--max-media" in sys.argv:
            idx = sys.argv.index("--max-media")
            if idx + 1 < len(sys.argv):
                cli_max_media = int(sys.argv[idx + 1])
        raise SystemExit(run_library_requeue_review(
            max_media=cli_max_media, resume="--resume" in sys.argv, dry_run="--dry-run" in sys.argv,
        ))
    if len(sys.argv) > 1 and sys.argv[1] == "--auth-migrate":
        if "--apply" not in sys.argv and "--dry-run" not in sys.argv:
            print(json.dumps({"ok": False, "error": "NeedDryRunOrApply"}, ensure_ascii=False))
            raise SystemExit(2)
        if "--apply" not in sys.argv:
            raise SystemExit(run_auth_migrate_dry_run())
        raise SystemExit(run_auth_migrate(apply=True))
    if len(sys.argv) > 1 and sys.argv[1] == "--library-check-links":
        cli_provider = None
        if "--provider" in sys.argv:
            idx = sys.argv.index("--provider")
            if idx + 1 < len(sys.argv):
                cli_provider = sys.argv[idx + 1]
        cli_sample = None
        if "--sample" in sys.argv:
            idx = sys.argv.index("--sample")
            if idx + 1 < len(sys.argv):
                raw_sample = sys.argv[idx + 1]
                try:
                    cli_sample = int(raw_sample)
                except ValueError:
                    # M7: a non-numeric --sample must never surface as a raw
                    # traceback -- a JSON error body (documented here) and
                    # exit code 2, matching every other CLI argument error
                    # in this block.
                    print(json.dumps({"ok": False, "error": "InvalidSample", "value": raw_sample}, ensure_ascii=False))
                    raise SystemExit(2)
                if cli_sample < 1:
                    # Review follow-up: SQLite treats a negative LIMIT as "no
                    # limit", so a negative/zero --sample would probe every
                    # live link of the provider (dry-run skips the budget).
                    print(json.dumps({"ok": False, "error": "InvalidSample", "value": raw_sample}, ensure_ascii=False))
                    raise SystemExit(2)
        raise SystemExit(run_library_check_links(
            provider=cli_provider, sample=cli_sample, dry_run="--dry-run" in sys.argv, as_json="--json" in sys.argv,
        ))
    app.run(host=os.getenv("HIDRIVE_BIND", "127.0.0.1"), port=int(os.getenv("HIDRIVE_PORT", "12367")), debug=False)
