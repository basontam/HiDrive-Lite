"""HiDrive-Lite: a small, self-hosted media-resource control surface.

The 115 share-receive flow is isolated behind explicit provider checks.  The
service exposes the personal media library, optional HDHive metadata, 115
one-click saving, and read-only OpenList/STRM browsing.  Local development is
the safe default; an operator can opt into a reverse proxy or Cloudflare
Access in deployment configuration.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import fcntl
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

from dotenv import load_dotenv
import jwt
import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request

# Load only the repository-local .env for clone-and-run installations. An
# explicit path prevents a parent workspace's credentials or paths from being
# inherited accidentally. Explicit process environment variables still win.
load_dotenv(Path(__file__).resolve().parent / ".env")

import library_normalize
import library_search
import library_store
import library_tmdb

# T4: integrate the TMDB cache/budget tables into every schema this process
# creates. Registered here (not in library_store.py) so library_store.py's
# own unit tests stay decoupled from library_tmdb (they assert a bare
# create_schema() does NOT create tmdb_cache/tmdb_budget); app.py is the
# actual production entrypoint that wires the two modules together, so this
# is the "integration time" the EXTRA_SCHEMA_HOOKS docstring refers to.
library_store.EXTRA_SCHEMA_HOOKS.append(library_tmdb.ensure_tables)


APP_NAME = "HiDrive-Lite"
HDHIVE_BASE = "https://hdhive.com"
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
STRM_ROOT = Path(os.getenv("STRM_ROOT", DEFAULT_STRM_ROOT)).resolve()
OPENLIST_URL = os.getenv("OPENLIST_URL", DEFAULT_OPENLIST_URL).rstrip("/")
OPENLIST_DB = Path(os.getenv("OPENLIST_DB", "./data/openlist.db"))
OPENLIST_115PAN_PATH = os.getenv("OPENLIST_115PAN_PATH", DEFAULT_OPENLIST_115PAN_PATH).strip() or DEFAULT_OPENLIST_115PAN_PATH
OPENLIST_115STRM_PATH = os.getenv("OPENLIST_115STRM_PATH", DEFAULT_OPENLIST_115STRM_PATH).strip() or DEFAULT_OPENLIST_115STRM_PATH
OPEN115_API_BASE = "https://proapi.115.com"
# 115's web-session QR login endpoints (see docs/115-integration.md --
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


def require_access() -> None:
    if AUTH_MODE in {"local", "disabled"}:
        g.principal = {"sub": "local", "email": "local"}
        return
    token = request.headers.get("Cf-Access-Jwt-Assertion", "")
    if not token:
        abort(401, description="Cloudflare Access authentication required")
    try:
        g.principal = verify_access_jwt(token)
    except PermissionError:
        abort(401, description="invalid Cloudflare Access authentication")


def csrf_token() -> str:
    subject = actor_id()
    issued = utc_now()
    secret = load_fernet()._signing_key  # deterministic, local-only HMAC key
    payload = f"{subject}:{issued}".encode("utf-8")
    digest = hmac.new(secret, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b":" + digest).decode("ascii").rstrip("=")


def check_csrf() -> None:
    if AUTH_MODE in {"local", "disabled"}:
        return
    if request.headers.get("Origin", "").rstrip("/") != PUBLIC_ORIGIN:
        abort(403, description="origin check failed")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied:
        abort(403, description="CSRF token required")
    try:
        padded = supplied + "=" * (-len(supplied) % 4)
        raw = base64.urlsafe_b64decode(padded)
        subject, issued, signature = raw.split(b":", 2)
        if int(issued) + CSRF_TTL_SECONDS < utc_now() or subject.decode() != actor_id():
            raise ValueError
        expected = hmac.new(load_fernet()._signing_key, raw[: len(subject) + 1 + len(issued)], hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
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
        raise RuntimeError("HDHive token response did not include access_token")
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
        return {"success": False, "code": "UPSTREAM_UNAVAILABLE", "message": f"HDHive 请求失败：{type(exc).__name__}"}, 502
    try:
        data = response.json() if response.content else {}
    except (ValueError, TypeError):
        return {"success": False, "code": "UPSTREAM_INVALID_JSON", "message": "HDHive 返回了无法解析的响应"}, 502
    if not isinstance(data, dict):
        return {"success": False, "code": "UPSTREAM_INVALID_JSON", "message": "HDHive 返回格式异常"}, 502
    return data, response.status_code


def hdhive_request(method: str, path: str, *, params: dict | None = None, payload: dict | None = None, requires_user: bool = True) -> tuple[dict, int]:
    api_key = config_value("hdhive_app_secret", "HDHIVE_APP_SECRET")
    if not api_key:
        return {"success": False, "code": "HDHIVE_APP_SECRET_MISSING", "message": "请先配置 HDHive 应用 Secret"}, 503
    token = valid_hdhive_access_token() if requires_user else None
    if requires_user and not token:
        row = get_tokens()
        if hdhive_refresh_is_available(row):
            return {"success": False, "code": "HDHIVE_REFRESH_UNAVAILABLE", "message": "HDHive Token 刷新暂时失败，请稍后重试；若持续失败请重新授权"}, 503
        return {"success": False, "code": "OPENAPI_REAUTH_REQUIRED", "message": "请先完成 HDHive OAuth 授权"}, 401
    data, status = hdhive_request_once(method, path, hdhive_headers(token), params, payload)
    if status in {401, 403} and requires_user and data.get("code") == "OPENAPI_REFRESH_REQUIRED":
        token = refresh_hdhive_token()
        if not token:
            row = get_tokens()
            if hdhive_refresh_is_available(row):
                return {"success": False, "code": "HDHIVE_REFRESH_UNAVAILABLE", "message": "HDHive Token 刷新暂时失败，请稍后重试；若持续失败请重新授权"}, 503
            return {"success": False, "code": "OPENAPI_REAUTH_REQUIRED", "message": "HDHive 授权已失效，请重新完成 OAuth 授权"}, 401
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
    """
    target_path = str(body.get("target_path") or "").strip()
    given_pid = str(body.get("target_pid") or "").strip()
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
_TRANSFER_DEDUPE_SEEN: dict[tuple[str, str], float] = {}


def _transfer_dedupe_check(public_id: str, pid: str) -> bool:
    """True (and records the attempt) the first time ``(public_id, pid)``
    is seen within the window; False for a duplicate seen again before the
    window elapses. Expired entries are pruned opportunistically on every
    call so the dict never grows past what's active within the window."""
    now = time.monotonic()
    key = (public_id, pid)
    with _TRANSFER_DEDUPE_LOCK:
        for seen_key, seen_at in list(_TRANSFER_DEDUPE_SEEN.items()):
            if now - seen_at >= _TRANSFER_DEDUPE_WINDOW_SECONDS:
                del _TRANSFER_DEDUPE_SEEN[seen_key]
        if key in _TRANSFER_DEDUPE_SEEN:
            return False
        _TRANSFER_DEDUPE_SEEN[key] = now
        return True


def _transfer_dedupe_clear(public_id: str, pid: str) -> None:
    """T17 fix wave 1 item 3: undo ``_transfer_dedupe_check``'s record when
    the attempt it guarded never actually reached 115's share/receive
    endpoint (``TransferResult.receive_attempted`` is False) -- a failure
    that happened before receive was ever issued (bad link format, the
    cookie/rate/provider gate, share/snap, or the shared budget running
    out) is not a real transfer attempt, so an immediate retry with the
    same ``(resource_link_id, pid)`` must not be blocked by the 10s window
    meant for double-submits of a REAL, receive-issuing attempt."""
    with _TRANSFER_DEDUPE_LOCK:
        _TRANSFER_DEDUPE_SEEN.pop((public_id, pid), None)


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
    machine (docs/115-integration.md
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
    cookies = (cookie if cookie is not None else config_value("115_cookie", "ENV_115_COOKIES") or "").strip()
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
    material, the raw response, or the account uid (docs/115-integration.md).
    Returns
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


def _persist_115_verification(result: dict) -> None:
    """Persist only sanitised state metadata (settings table) -- never the
    cookie, raw response or uid. Keeps writing legacy ``115_cookie_valid``
    for compatibility."""
    state = result["state"]
    checked_at = result["checked_at"]
    setting_set("115_cookie_state", state)
    setting_set("115_cookie_checked_at", str(checked_at))
    setting_set("115_cookie_error_code", result.get("error_code") or "")
    setting_set("115_cookie_valid", "1" if state == "valid" else "0")
    if state == "valid":
        setting_set("115_cookie_last_success_at", str(checked_at))
        setting_set("115_cookie_fail_streak", "0")
    elif state != "unconfigured":
        setting_set("115_cookie_error_at", str(checked_at))
        streak = int(setting_get("115_cookie_fail_streak", "0") or "0") + 1
        setting_set("115_cookie_fail_streak", str(streak))


def remember_115_cookie_check(cookie: str | None = None) -> tuple[bool, str]:
    """Used by /api/settings right after a new cookie value is saved: an
    immediate, uncached check (bypassing the interval/backoff, which only
    throttle the periodic /api/status poll and the transfer-flow gate)."""
    result = verify_115_session(cookie)
    _persist_115_verification(result)
    return result["state"] == "valid", _115_STATE_LABEL.get(result["state"], "不可用")


def _115_maybe_refresh() -> None:
    checked_at = int(setting_get("115_cookie_checked_at", "0") or "0")
    fail_streak = int(setting_get("115_cookie_fail_streak", "0") or "0")
    interval = _115_next_check_interval(fail_streak)
    if not checked_at or checked_at + interval <= utc_now():
        _persist_115_verification(verify_115_session())


def cookie_status(verify: bool = False) -> dict[str, object]:
    configured = bool(config_value("115_cookie", "ENV_115_COOKIES"))
    if not configured:
        return {"configured": False, "valid": None, "state": "unconfigured", "checked_at": None, "last_success_at": None, "error_code": None, "retry_after": None}
    if verify:
        _115_maybe_refresh()
    checked_at_raw = setting_get("115_cookie_checked_at", "") or ""
    try:
        checked_at = int(checked_at_raw)
    except ValueError:
        checked_at = 0
    last_success_raw = setting_get("115_cookie_last_success_at", "") or ""
    try:
        last_success_at = int(last_success_raw) or None
    except ValueError:
        last_success_at = None
    valid_raw = setting_get("115_cookie_valid")
    valid = None if valid_raw not in {"0", "1"} else valid_raw == "1"
    state = setting_get("115_cookie_state", "") or ("valid" if valid else "unknown" if valid is None else "reauth_required")
    # Item 8: how long until the next scheduled check will retry, so the UI
    # can show a countdown instead of a bare "rate limited" -- same backoff
    # formula _115_transfer_gate() already uses for its own cached 429s.
    retry_after = None
    if state == "rate_limited" and checked_at:
        fail_streak = int(setting_get("115_cookie_fail_streak", "0") or "0")
        retry_after = max(0, checked_at + _115_next_check_interval(fail_streak) - utc_now())
    return {
        "configured": True,
        "valid": valid,
        "state": state,
        "checked_at": checked_at or None,
        "last_success_at": last_success_at,
        "error_code": setting_get("115_cookie_error_code") or None,
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


# docs/115-integration.md §6.2: explicit,
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
    if not bool(config_value("115_cookie", "ENV_115_COOKIES")):
        return _115_transfer_error("unconfigured", None), None
    checked_at = int(setting_get("115_cookie_checked_at", "0") or "0")
    fail_streak = int(setting_get("115_cookie_fail_streak", "0") or "0")
    cached_state = setting_get("115_cookie_state", "") or ""
    interval = _115_next_check_interval(fail_streak)
    still_fresh = bool(checked_at) and checked_at + interval > utc_now()
    if still_fresh and cached_state and cached_state != "valid":
        cached_retry = max(0, checked_at + interval - utc_now()) if cached_state == "rate_limited" else None
        return _115_transfer_error(cached_state, cached_retry), None
    result, uid = _115_verify_with_uid(deadline=deadline)
    if result["state"] == "budget_exhausted":
        return TransferResult(False, "转存耗时过长，请稍后重试", 504, "TRANSFER_NOT_ATTEMPTED"), None
    _persist_115_verification(result)
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
    cookies = config_value("115_cookie", "ENV_115_COOKIES") or ""
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
# QR re-authorisation (docs/115-integration.md). Challenge state lives in
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
        return "failed", "VERIFY_FAILED"
    secret_set("115_cookie", new_cookie)
    _persist_115_verification(verification)
    return "authenticated", None


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
    # Upstream descriptions sometimes omit the URL scheme.  Strip domain
    # names with a path as well so a bare 115/115cdn/anxia/share URL cannot
    # reach the browser through an otherwise harmless text field.
    text = re.sub(r"(?i)\b(?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|me|cc|tv)(?:/[^\s<>\"']*)?", "[redacted-url]", text)
    text = re.sub(r"(?i)(password|access[\s_-]+code|cookie|token|secret)\s*[:=]\s*[^\s,;，；。]+", r"\1=[redacted]", text)
    text = re.sub(r"(访问码|提取码|密码|口令|密钥)\s*[:：=]\s*[^\s,;，；。]+", r"\1：[redacted]", text)
    return text[:500]


def _safe_hdhive_envelope(data: object) -> dict:
    """Keep only the non-secret part of an HDHive response envelope.

    HDHive resource and unlock responses may contain provider share URLs,
    access codes, or opaque download tokens. Those values are useful to the
    server-side adapter but must not be reflected into a browser response.
    Error codes/messages remain available for diagnosis, while successful
    payloads are normalized by the endpoint-specific helpers below.
    """
    if not isinstance(data, dict):
        return {"success": False, "code": "UPSTREAM_INVALID_JSON", "message": "HDHive 返回格式异常"}
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
        # Some deployments wrap resources in ``items``/``resources`` and add
        # pagination metadata. Preserve only counters and normalized items.
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
        # The browser already knows its origin from the current URL. Do not
        # echo deployment URLs or filesystem paths through a diagnostics API.
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


app = Flask(__name__)
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
        if AUTH_MODE in {"local", "disabled"}:
            g.principal = {"sub": "local", "email": "local"}
        else:
            require_access()
        return
    require_access()
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
    return render_template("index.html", asset_version=ASSET_VERSION, public_origin=PUBLIC_ORIGIN)


@app.get("/api/csrf")
def api_csrf():
    return jsonify({"success": True, "token": csrf_token()})


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
    library_keys = {
        "115_target_pid", "tmdb_daily_budget", "tmdb_enrich_enabled", "tvmaze_hint_enabled",
        "linkcheck_enabled", "linkcheck_providers",
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
        page_size = int(request.args.get("page_size", "24"))
    except ValueError:
        return json_error("page/page_size 必须为整数", 400, "LIBRARY_SEARCH_PAGE_INVALID")
    if not (1 <= page <= 200):
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
_RECOMMENDATION_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RECOMMENDATION_DEFAULT_LIMIT = 12
_RECOMMENDATION_MAX_LIMIT = 24

# (db_path, installed_at, day, algorithm_version, limit) -> {"ids": [...], "fallback": bool}
_RECOMMENDATION_CACHE: dict[tuple, dict] = {}


def _recommendation_tie(day: str, media_id: int) -> str:
    payload = f"{day}|{_RECOMMENDATION_ALGO_VERSION}|{media_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _recommendation_selection(store: "library_store.LibraryStore", day: str, limit: int) -> dict:
    # The cache key includes the installed index's own identity (path +
    # install timestamp) so a later ``--library-install``/rollback that
    # replaces the index -- same day, same limit -- can never keep serving
    # a stale selection computed against the previous index's media ids.
    cache_key = (str(store.db_path), store.meta_get("installed_at"), day, _RECOMMENDATION_ALGO_VERSION, limit)
    cached = _RECOMMENDATION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    eligible_ids = store.eligible_recommendation_media_ids()
    if eligible_ids:
        ranked = sorted(eligible_ids, key=lambda media_id: _recommendation_tie(day, media_id))
        selection = {"ids": ranked[:limit], "fallback": False}
    else:
        selection = {"ids": store.fallback_recommendation_media_ids(limit), "fallback": True}

    _RECOMMENDATION_CACHE[cache_key] = selection
    return selection


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
            day = datetime.strptime(date_raw, "%Y-%m-%d").strftime("%Y-%m-%d")
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

    selection = _recommendation_selection(store, day, limit)

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
            "section": "today_recommendations",
            "date": day,
            "algorithm_version": _RECOMMENDATION_ALGO_VERSION,
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
    media = dict(media)
    poster_path = media.pop("poster_path", None)
    media["poster_url"] = _tmdb_image_url(poster_path, LIBRARY_POSTER_SIZE)
    media["poster_large_url"] = _tmdb_image_url(poster_path, LIBRARY_POSTER_LARGE_SIZE)
    media["backdrop_url"] = _tmdb_image_url(media.pop("backdrop_path", None), LIBRARY_BACKDROP_SIZE)
    return jsonify({"success": True, **media})


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

    if not _transfer_dedupe_check(public_id, pid):
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
        _transfer_dedupe_clear(public_id, pid)
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
        return json_error("请先配置 HDHive Client ID", 503, "HDHIVE_CLIENT_ID_MISSING")
    state = secrets.token_urlsafe(32)
    created_by = actor_id()
    with connect_db() as db:
        db.execute("DELETE FROM oauth_states WHERE expires_at < ? OR used_at IS NOT NULL", (utc_now(),))
        db.execute("INSERT INTO oauth_states(state,expires_at,created_by) VALUES(?,?,?)", (state, utc_now() + STATE_TTL_SECONDS, created_by))
    redirect_uri = PUBLIC_ORIGIN + "/api/oauth/hdhive/callback"
    query = urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "scope": "query unlock write", "state": state})
    audit("hdhive.oauth.start", "success", "authorization URL issued", created_by)
    return jsonify({"success": True, "url": HDHIVE_BASE + "/openapi/authorize?" + query})


@app.get("/api/oauth/hdhive/callback")
def oauth_callback():
    code = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    if not code or not state:
        return json_error("HDHive OAuth 回调缺少 code/state", 400, "OAUTH_CALLBACK_INVALID")
    current_actor = actor_id()
    with connect_db() as db:
        row = db.execute("SELECT * FROM oauth_states WHERE state=? AND used_at IS NULL AND expires_at>=?", (state, utc_now())).fetchone()
        if not row:
            return json_error("OAuth state 无效或已过期", 400, "OAUTH_STATE_INVALID")
        if str(row["created_by"] or "") != current_actor:
            audit("hdhive.oauth.callback", "failed", "state actor mismatch", current_actor)
            return json_error("OAuth state 不属于当前用户", 403, "OAUTH_STATE_ACTOR_MISMATCH")
        db.execute("UPDATE oauth_states SET used_at=? WHERE state=?", (utc_now(), state))
    secret = config_value("hdhive_app_secret", "HDHIVE_APP_SECRET")
    client_id = config_value("hdhive_client_id", "HDHIVE_CLIENT_ID")
    if not secret or not client_id:
        return json_error("HDHive App Secret/Client ID 尚未配置", 503, "HDHIVE_CREDENTIALS_MISSING")
    redirect_uri = PUBLIC_ORIGIN + "/api/oauth/hdhive/callback"
    try:
        response = requests.post(HDHIVE_BASE + HDHIVE_TOKEN_PATH, headers={"X-API-Key": secret, "Accept": "application/json"}, json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}, timeout=20)
        data = response.json() if response.content else {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        code = "HDHIVE_INVALID_JSON" if isinstance(exc, (ValueError, TypeError)) else "HDHIVE_UNAVAILABLE"
        return json_error("HDHive OAuth 返回格式异常" if code == "HDHIVE_INVALID_JSON" else f"HDHive OAuth 换 token 失败：{type(exc).__name__}", 502, code)
    if response.status_code >= 400 or not data.get("success", False):
        safe_error = _safe_hdhive_envelope(data)
        return json_error(str(safe_error.get("message") or "HDHive OAuth 授权失败"), response.status_code if response.status_code >= 400 else 400, str(safe_error.get("code") or "OAUTH_EXCHANGE_FAILED"))
    try:
        save_tokens(data.get("data") or {})
    except RuntimeError as exc:
        return json_error(str(exc), 502, "OAUTH_TOKEN_INVALID")
    audit("hdhive.oauth.callback", "success", "encrypted access/refresh tokens stored", current_actor)
    return redirect("/?oauth=success")


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
    payload = {"slug": slug}
    if allow_points:
        payload["allow_points"] = True
    data, status = hdhive_request("POST", "/api/open/resources/unlock", payload=payload)
    if status < 400 and data.get("success"):
        audit("hdhive.unlock", "success", "points unlock requested" if allow_points else "free or already-owned resource; link omitted from audit", actor_id())
    return jsonify(_safe_hdhive_unlock_response(data)), status


@app.get("/api/hdhive/search")
def api_hdhive_search():
    """Search the same TMDB catalogue HDHive uses, then let the user open resources."""
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
    return jsonify({"success": True, "source": "TMDB + HDHive OpenAPI", "results": normalized})


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
            "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,qr_uid_cipher,qr_time,qr_sign_cipher) VALUES(?,?,?,?,?,?,?,?)",
            (
                id_hash, actor, "pending", now, expires_at,
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
    root = normalized_path(OPENLIST_115PAN_PATH)
    path = normalized_path(request.args.get("path", root))
    if not path_under(root, path):
        return json_error("目录必须位于 OpenList 的 115pan 存储下", 400, "OPENLIST_PATH_INVALID")
    folders, status, message = openlist_folder_items(path)
    if status >= 400:
        return json_error(message, status, "OPENLIST_LIST_FAILED")
    parent = root if path == root else normalized_path(posixpath.dirname(path))
    return jsonify({"success": True, "root": root, "path": path, "parent": parent, "items": folders})


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
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "--checkin":
        raise SystemExit(run_checkin())
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
