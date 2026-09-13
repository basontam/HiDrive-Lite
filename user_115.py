"""Per-user 115 credentials and state.

Phase 4-6 of docs/claude-multi-user-auth-115-isolation-re0-policy-plan-20260911.md.

Two independent authorisations, never merged (plan §4):

* **step A**, a 115 *web session cookie*, obtained by scanning the web login
  QR. It is what receives an external share into the account's default inbox.
* **step B**, a 115 *OpenAPI access/refresh token pair*, obtained by a
  separate authorisation against an approved application. It is what lists
  folders and queues cloud downloads.

Having one never implies the other, reconnecting one never clears the other,
and neither is ever read from another user's row. There is no code path here
that returns the administrator's credential to anyone else: the legacy global
values are resolved in ``app.py``, for the administrator alone, during the
compatibility window.

Step A's HTTP lives in ``app.py``. Step B's injected HTTP adapter and
per-user device authorization service live here alongside credential storage.
"""

from __future__ import annotations

import sqlite3
import json as _json
from contextlib import closing as _closing
from dataclasses import dataclass

# The three names `user_secret.name` accepts (auth_service.SCHEMA_SQL pins
# the same list as a CHECK constraint).
COOKIE_SECRET = "115_web_cookie"
OPEN_ACCESS_SECRET = "115_open_access_token"
OPEN_REFRESH_SECRET = "115_open_refresh_token"
SECRET_NAMES = (COOKIE_SECRET, OPEN_ACCESS_SECRET, OPEN_REFRESH_SECRET)

# `user_115_profile.cookie_state` / `open_state`.
STATE_UNCONFIGURED = "unconfigured"
STATE_CONNECTED = "connected"
STATE_NEEDS_REAUTH = "needs_reauth"
# Told to forget it. Distinct from `unconfigured` on purpose: the
# administrator's legacy global credential is still there for a rollback to
# read, and only an explicit disconnect may stop it being used (R18).
STATE_DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class User115Credentials:
    """What one user has authorised, and nothing about anybody else."""

    user_id: int
    web_cookie: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None

    @property
    def has_cookie(self) -> bool:
        return bool(self.web_cookie)

    @property
    def has_open_token(self) -> bool:
        """Step B needs *both* halves: an access token with no refresh token
        cannot survive its own expiry, and the plan refuses to call that
        connected (§16.3)."""
        return bool(self.access_token and self.refresh_token)


def secret_set(db: sqlite3.Connection, user_id: int, name: str, value: str, *, fernet, now: int) -> None:
    if name not in SECRET_NAMES:
        raise ValueError(f"unknown user secret: {name}")
    # The version is what makes a rotation observable to another worker
    # (review F03): `updated_at` is a clock and two writes in the same second
    # are indistinguishable by it, while this only ever moves forward.
    db.execute(
        "INSERT INTO user_secret(user_id, name, ciphertext, updated_at, version) VALUES(?,?,?,?,1) "
        "ON CONFLICT(user_id, name) DO UPDATE SET ciphertext=excluded.ciphertext, "
        "updated_at=excluded.updated_at, version=user_secret.version + 1",
        (user_id, name, fernet.encrypt(value.encode("utf-8")), now),
    )


def secret_version(db: sqlite3.Connection, user_id: int, name: str) -> int:
    """Which generation of this secret is stored, 0 when there is none.

    Callers carry it alongside the value they read, so a failure can be
    recognised as belonging to a credential that has since been replaced
    (review F03).
    """
    row = db.execute("SELECT version FROM user_secret WHERE user_id=? AND name=?", (user_id, name)).fetchone()
    return int(row["version"]) if row is not None else 0


def secret_get(db: sqlite3.Connection, user_id: int, name: str, *, fernet) -> str | None:
    row = db.execute("SELECT ciphertext FROM user_secret WHERE user_id=? AND name=?", (user_id, name)).fetchone()
    if row is None:
        return None
    try:
        return fernet.decrypt(bytes(row["ciphertext"])).decode("utf-8")
    except Exception:  # noqa: BLE001 - a secret that will not decrypt is simply absent
        return None


@dataclass(frozen=True)
class OpenSnapshot:
    """Both halves of a step-B pair with the version, origin and state they
    were read with -- all from one SQL statement, so one read.

    Review G01.2: reading the token through the resolver and then opening a
    second connection for the version could return an old token beside a new
    version. The old token then 401s, and the recovery concludes the new
    version has not been tried yet and rotates again. No timeout is needed for
    that window; the fix is to stop taking two snapshots.
    """
    access_token: str | None
    refresh_token: str | None
    version: int
    origin: str | None
    open_state: str | None

    @property
    def connected(self) -> bool:
        return bool(self.access_token and self.refresh_token)


_OPEN_SNAPSHOT_SQL = """
SELECT
  (SELECT ciphertext FROM user_secret WHERE user_id = :uid AND name = :access) AS access_cipher,
  (SELECT version    FROM user_secret WHERE user_id = :uid AND name = :access) AS access_version,
  (SELECT ciphertext FROM user_secret WHERE user_id = :uid AND name = :refresh) AS refresh_cipher,
  (SELECT open_token_origin FROM user_115_profile WHERE user_id = :uid) AS origin,
  (SELECT open_state        FROM user_115_profile WHERE user_id = :uid) AS open_state
"""


def open_snapshot(db: sqlite3.Connection, user_id: int, *, fernet) -> OpenSnapshot:
    """This user's stored step-B pair, as one consistent read (G01.2)."""
    row = db.execute(_OPEN_SNAPSHOT_SQL, {
        "uid": user_id, "access": OPEN_ACCESS_SECRET, "refresh": OPEN_REFRESH_SECRET}).fetchone()

    def plain(value):
        if value is None:
            return None
        try:
            return fernet.decrypt(bytes(value)).decode("utf-8")
        except Exception:  # noqa: BLE001 - a secret that will not decrypt is simply absent
            return None

    return OpenSnapshot(
        access_token=plain(row["access_cipher"]),
        refresh_token=plain(row["refresh_cipher"]),
        version=int(row["access_version"] or 0),
        origin=row["origin"],
        open_state=row["open_state"],
    )


def secret_clear(db: sqlite3.Connection, user_id: int, *names: str) -> int:
    """Forget the named secrets for this user. Naming one step's secrets
    never touches the other's -- disconnecting the cookie must not cost
    somebody their OpenAPI authorisation (plan §4.2)."""
    if OPEN_ACCESS_SECRET in names or OPEN_REFRESH_SECRET in names:
        cancel_device_challenges(db, user_id)
    removed = 0
    for name in names or ():
        if name not in SECRET_NAMES:
            raise ValueError(f"unknown user secret: {name}")
        cursor = db.execute("DELETE FROM user_secret WHERE user_id=? AND name=?", (user_id, name))
        removed += cursor.rowcount or 0
    return removed


def credentials_for(db: sqlite3.Connection, user_id: int, *, fernet) -> User115Credentials:
    return User115Credentials(
        user_id=user_id,
        web_cookie=secret_get(db, user_id, COOKIE_SECRET, fernet=fernet),
        access_token=secret_get(db, user_id, OPEN_ACCESS_SECRET, fernet=fernet),
        refresh_token=secret_get(db, user_id, OPEN_REFRESH_SECRET, fernet=fernet),
    )


def ensure_profile(db: sqlite3.Connection, user_id: int, *, now: int) -> None:
    db.execute(
        "INSERT INTO user_115_profile(user_id, updated_at) VALUES(?,?) ON CONFLICT(user_id) DO NOTHING",
        (user_id, now),
    )


def profile(db: sqlite3.Connection, user_id: int):
    return db.execute("SELECT * FROM user_115_profile WHERE user_id=?", (user_id,)).fetchone()


def remember_cookie_state(db: sqlite3.Connection, user_id: int, *, state: str,
                          error_code: str | None, now: int) -> None:
    """Record what the last check said about *this* user's cookie.

    Only sanitised metadata: a state word, a timestamp and an error class.
    Never the cookie, the response, or a uid.
    """
    ensure_profile(db, user_id, now=now)
    db.execute(
        "UPDATE user_115_profile SET cookie_state=?, cookie_checked_at=?, cookie_error_code=?, updated_at=? "
        "WHERE user_id=?",
        (state, now, (error_code or "")[:64] or None, now, user_id),
    )


ORIGIN_OPENLIST_LEGACY = "openlist_legacy"
ORIGIN_OWN_APP = "own_app"


def open_token_origin(db: sqlite3.Connection, user_id: int) -> str | None:
    """Who is responsible for rotating this user's step-B pair (review F02).

    ``openlist_legacy`` means the pair was copied from OpenList by the
    migration and OpenList still rotates it -- this deployment must never
    present that refresh token to 115 itself, or two systems would be
    rotating one credential. ``own_app`` means this deployment's own 115
    application issued it and owns its lifecycle. ``None`` when nothing is
    recorded, which for an administrator during the migration window is read
    as the legacy case.
    """
    row = profile(db, user_id)
    if row is None:
        return None
    keys = row.keys() if hasattr(row, "keys") else ()
    return row["open_token_origin"] if "open_token_origin" in keys else None


def remember_open_origin(db: sqlite3.Connection, user_id: int, origin: str, *, now: int) -> None:
    if origin not in {ORIGIN_OPENLIST_LEGACY, ORIGIN_OWN_APP}:
        raise ValueError(f"unknown token origin: {origin}")
    ensure_profile(db, user_id, now=now)
    db.execute("UPDATE user_115_profile SET open_token_origin=?, updated_at=? WHERE user_id=?",
               (origin, now, user_id))


def remember_open_state(db: sqlite3.Connection, user_id: int, *, state: str, expires_at: int | None,
                        error_code: str | None, now: int) -> None:
    ensure_profile(db, user_id, now=now)
    db.execute(
        "UPDATE user_115_profile SET open_state=?, open_expires_at=?, open_checked_at=?, open_error_code=?, "
        "updated_at=? WHERE user_id=?",
        (state, expires_at, now, (error_code or "")[:64] or None, now, user_id),
    )


def setting_key(name: str, user_id: int, *, admin_user_id: int | None) -> str:
    """Where one user's copy of a per-user setting lives.

    The administrator keeps the original, unsuffixed keys. That is
    deliberate: those exact rows are what the previous release reads, so a
    rollback during the compatibility window finds its state where it left
    it (plan §8). Everybody else gets their own suffixed row.
    """
    if admin_user_id is not None and user_id == admin_user_id:
        return name
    return f"{name}:u{int(user_id)}"


def capability_state(credentials: User115Credentials) -> dict:
    """The two capabilities, described the way the settings page words them
    (plan §4.2) -- by what the user can do, never by what is stored."""
    return {
        "transfer": credentials.has_cookie,
        "browse": credentials.has_open_token,
        "cloud_download": credentials.has_open_token,
        "summary": (
            "全部可用" if credentials.has_cookie and credentials.has_open_token
            else "基础可用" if credentials.has_cookie
            else "待连接"
        ),
    }


# ---------------------------------------------------------------------------
# Step B: the 115 OpenAPI authorisation (plan §4.3-§4.5)
#
# Two flows, one adapter. Which one runs is decided by what the application
# is actually approved for -- never guessed:
#
#   device_pkce          server asks for a device code with a PKCE challenge,
#                        the user confirms by scanning, the server exchanges
#                        uid + code_verifier for the token pair.
#   authorization_code   server sends the user to 115's authorize page with a
#                        one-time state, and exchanges the returned code with
#                        its client secret.
#
# What this module will not do, ever (plan §17):
#
#   * treat OpenList's `115cloud_qr` UID as an access token -- that endpoint
#     returns a web-login UID and no refresh token at all;
#   * route a user through `api.oplist.org`, whose callback lands on its own
#     domain and cannot hand anything back to us;
#   * call a token pair "connected" on anything less than access + refresh +
#     expires_in, verified by a read-only probe.
#
# Every request goes through an injected session, so the contract tests below
# exercise the real code without a network.
# ---------------------------------------------------------------------------

import base64 as _base64
import hashlib as _hashlib
import secrets as _secrets

OPEN_AUTHORIZE_URL = "https://passportapi.115.com/open/authorize"
OPEN_DEVICE_CODE_URL = "https://passportapi.115.com/open/authDeviceCode"
OPEN_DEVICE_STATUS_URL = "https://qrcodeapi.115.com/get/status/"
OPEN_DEVICE_TOKEN_URL = "https://passportapi.115.com/open/deviceCodeToToken"
OPEN_CODE_TOKEN_URL = "https://passportapi.115.com/open/authCodeToToken"
OPEN_REFRESH_URL = "https://passportapi.115.com/open/refreshToken"

FLOW_DEVICE_PKCE = "device_pkce"
FLOW_AUTHORIZATION_CODE = "authorization_code"

# Why step B cannot run for real yet. Reported as-is to the settings page so
# it says "待授权（尚未获批）" rather than pretending to be one click away.
BLOCKED_NO_APP = "blocked_by_115_app_approval"
BLOCKED_NO_VERIFICATION = "blocked_by_115_flow_verification"


class Open115Error(RuntimeError):
    """A step-B failure with a stable class name for the UI and the audit."""

    def __init__(self, error_class: str, message: str = "") -> None:
        super().__init__(message or error_class)
        self.error_class = error_class


@dataclass(frozen=True)
class OpenAppConfig:
    """The application 115 approved, which is deployment configuration -- not
    a user's, and never sent to a browser (plan §9.1)."""

    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None
    device_flow_verified: bool = False

    @property
    def has_client(self) -> bool:
        return bool(self.client_id)

    def blocked_reason(self) -> str | None:
        """None when step B may be attempted for real."""
        if not self.has_client:
            return BLOCKED_NO_APP
        if not self.device_flow_verified and not (self.client_secret and self.redirect_uri):
            # Neither flow is usable: device PKCE is unverified for this app,
            # and the authorisation-code fallback has no secret/redirect.
            return BLOCKED_NO_VERIFICATION
        return None

    def flow(self) -> str:
        """Device PKCE when this application is confirmed to allow it,
        otherwise the authorisation-code fallback OpenList has verified."""
        return FLOW_DEVICE_PKCE if self.device_flow_verified else FLOW_AUTHORIZATION_CODE


@dataclass(frozen=True)
class OpenToken:
    access_token: str
    refresh_token: str
    expires_in: int

    def expires_at(self, now: int) -> int:
        return now + int(self.expires_in)


def pkce_pair() -> tuple[str, str]:
    """A high-entropy verifier and the 115 device protocol's SHA-256 challenge.

    The verifier is returned to the caller to encrypt into its own challenge
    row; it never reaches the browser and never appears in a log (plan §4.4).
    115 uses standard Base64 including padding, as in OpenList's 115 SDK,
    rather than the unpadded Base64URL encoding used by OAuth S256.
    """
    verifier = _secrets.token_urlsafe(64)
    digest = _hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _base64.b64encode(digest).decode("ascii")
    return verifier, challenge


def _open_payload(payload) -> dict:
    """Reject protocol errors even if an upstream body also carries data.

    Do not include the upstream message: it may echo a uid, sign or token.
    Passport replies use code/error; some QR replies also carry state.
    """
    if not isinstance(payload, dict):
        raise Open115Error("malformed_response")
    if "code" in payload and (type(payload["code"]) is not int or payload["code"] != 0):
        raise Open115Error("upstream_rejected")
    if payload.get("error") or payload.get("errno"):
        raise Open115Error("upstream_rejected")
    if "state" in payload and not (payload["state"] is True or
                                   type(payload["state"]) is int and payload["state"] == 1):
        raise Open115Error("upstream_rejected")
    return payload


def token_from_payload(payload) -> OpenToken:
    """The token pair in an upstream response, or a refusal.

    All three fields are required. A response carrying only a uid, or an
    access token with no refresh token, is a failure -- not a connection
    (plan §16.3).
    """
    payload = _open_payload(payload)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    raw_expires = data.get("expires_in")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 16384
           for value in (access, refresh)):
        raise Open115Error("incomplete_token", "response is missing an access or refresh token")
    if type(raw_expires) is not int and not (isinstance(raw_expires, str) and raw_expires.isascii()
                                           and raw_expires.isdecimal()):
        raise Open115Error("incomplete_token", "response is missing expires_in")
    try:
        expires_in = int(raw_expires)
    except (TypeError, ValueError):
        raise Open115Error("incomplete_token", "response is missing expires_in") from None
    if expires_in <= 0:
        raise Open115Error("incomplete_token", "expires_in is not a future lifetime")
    return OpenToken(access_token=access.strip(), refresh_token=refresh.strip(), expires_in=expires_in)


class Open115Adapter:
    """Step B's HTTP, with the session injected.

    Holds no user state: the caller owns the challenge row, the encryption
    and the decision to persist. That separation is what lets a failed
    exchange leave an existing, still-working token untouched (plan §4.4).
    """

    def __init__(self, config: OpenAppConfig, *, session, timeout: float = 10.0):
        self._config = config
        self._session = session
        self._timeout = timeout

    def _post(self, url: str, data: dict) -> dict:
        try:
            response = self._session.post(url, data=data, timeout=self._timeout, allow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - any transport failure is one class
            raise Open115Error("network_error", type(exc).__name__) from None
        return self._response_payload(response)

    def _response_payload(self, response) -> dict:
        status = getattr(response, "status_code", 0)
        if status == 429:
            raise Open115Error("rate_limited", "115 asked us to slow down")
        if status in {401, 403}:
            raise Open115Error("reauth_required", "115 refused this authorisation")
        if status != 200:
            raise Open115Error("upstream_error", f"HTTP {status}")
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise Open115Error("malformed_response", type(exc).__name__) from None
        return _open_payload(payload)

    def start_device(self, code_challenge: str) -> dict:
        """Ask for a device code. Returns only what the page may see -- the
        QR content and how long it lasts; uid and sign stay with the caller
        to encrypt."""
        if self._config.blocked_reason() or self._config.flow() != FLOW_DEVICE_PKCE:
            raise Open115Error("flow_not_available", "this application is not approved for device authorisation")
        payload = self._post(OPEN_DEVICE_CODE_URL, {
            "client_id": self._config.client_id,
            "code_challenge": code_challenge,
            "code_challenge_method": "sha256",
        })
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        for name in ("uid", "sign", "qrcode"):
            value = data.get(name)
            if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                raise Open115Error("malformed_response", "incomplete device challenge")
        if type(data.get("time")) is not int or data["time"] <= 0:
            raise Open115Error("malformed_response", "incomplete device challenge")
        return {"uid": data["uid"], "time": data["time"], "sign": data["sign"],
                "qrcode": data["qrcode"], "expires_in": data.get("expires_in")}

    def poll_device(self, *, uid: str, time: str, sign: str) -> str:
        """Read APP confirmation only; never exchange or persist from here.

        In particular, scanned (1) is not confirmed (2). The caller still
        must enforce owner, expiry, poll interval and one-time consumption.
        """
        if self._config.blocked_reason() or self._config.flow() != FLOW_DEVICE_PKCE:
            raise Open115Error("flow_not_available")
        if any(not isinstance(value, str) or not value or len(value) > 4096
               for value in (uid, time, sign)):
            raise Open115Error("invalid_challenge")
        try:
            response = self._session.get(OPEN_DEVICE_STATUS_URL,
                params={"uid": uid, "time": time, "sign": sign},
                timeout=self._timeout, allow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - do not retain a URL containing sign
            raise Open115Error("network_error", type(exc).__name__) from None
        payload = self._response_payload(response)
        data = payload.get("data")
        status = data.get("status") if isinstance(data, dict) else None
        states = {0: "pending", 1: "scanned", 2: "confirmed", -1: "expired", -2: "cancelled"}
        if type(status) is not int or status not in states:
            raise Open115Error("malformed_response")
        return states[status]

    def exchange_device(self, *, uid: str, code_verifier: str) -> OpenToken:
        """uid + verifier -> the token pair. Never called before the user has
        confirmed, and never with a verifier the browser has seen."""
        return token_from_payload(self._post(OPEN_DEVICE_TOKEN_URL, {
            "uid": uid, "code_verifier": code_verifier,
        }))

    def verify_token(self, token: OpenToken) -> None:
        """Probe the new pair's directory permission without refreshing it."""
        try:
            response = self._session.get("https://proapi.115.com/open/ufile/files",
                headers={"Authorization": "Bearer " + token.access_token},
                params={"cid": "0", "limit": 1, "offset": 0, "show_dir": 1},
                timeout=self._timeout, allow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            raise Open115Error("network_error", type(exc).__name__) from None
        payload = self._response_payload(response)
        if payload.get("state") is not True or not isinstance(payload.get("data"), list):
            raise Open115Error("probe_failed")

    def authorize_url(self, *, state: str) -> str:
        """Where to send the user for the authorisation-code flow. The client
        secret is not in it -- that only ever travels server to server."""
        if not self._config.has_client or not self._config.redirect_uri:
            raise Open115Error("flow_not_available", "no client id or redirect uri configured")
        from urllib.parse import urlencode

        return OPEN_AUTHORIZE_URL + "?" + urlencode({
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "response_type": "code",
            "state": state,
        })

    def exchange_code(self, *, code: str) -> OpenToken:
        if not self._config.client_secret:
            raise Open115Error("flow_not_available", "no client secret configured")
        return token_from_payload(self._post(OPEN_CODE_TOKEN_URL, {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self._config.redirect_uri,
        }))

    def refresh(self, *, refresh_token: str) -> OpenToken:
        """Rotate. The caller replaces both halves in one transaction, or
        neither -- a half-written pair is worse than an expired one."""
        return token_from_payload(self._post(OPEN_REFRESH_URL, {"refresh_token": refresh_token}))


# ---------------------------------------------------------------------------
# Step B device challenge lifecycle. No schema changes: the encrypted verifier
# column holds a versioned context for new challenges; old plain-verifier rows
# are not accepted by this consumer and must be restarted.
# ---------------------------------------------------------------------------

_DEVICE_TERMINAL = ("connected", "cancelled", "expired", "failed")


def cancel_device_challenges(db, user_id: int) -> None:
    db.execute("UPDATE user_115_oauth_state SET status='cancelled', code_verifier_ciphertext=NULL, "
        "upstream_uid_ciphertext=NULL, upstream_time_ciphertext=NULL, upstream_sign_ciphertext=NULL "
        "WHERE user_id=? AND flow=?", (user_id, FLOW_DEVICE_PKCE))


class DeviceAuthorization:
    """One user's APP grant, with short transactions and no network under a DB lock.

    A claim is durably consumed BEFORE exchange. A lost HTTP response cannot
    safely be replayed; it requires a new scan. No caller receives a token.
    Flask callers must authenticate and enforce CSRF on start/poll/cancel.
    """

    def __init__(self, connect, *, fernet, adapter, config, clock):
        self.connect, self.fernet, self.adapter = connect, fernet, adapter
        self.config, self.clock = config, clock

    @staticmethod
    def _active_user(db, user_id):
        row = db.execute("SELECT status FROM auth_user WHERE id=?", (user_id,)).fetchone()
        return row is not None and row["status"] == "active"

    @staticmethod
    def _pair_digest(db, user_id):
        digest = _hashlib.sha256()
        for row in db.execute("SELECT name,ciphertext,version FROM user_secret "
            "WHERE user_id=? AND name IN (?,?) ORDER BY name",
            (user_id, OPEN_ACCESS_SECRET, OPEN_REFRESH_SECRET)):
            digest.update(row["name"].encode() + b"\0" + bytes(row["ciphertext"]) +
                          b"\0" + str(row["version"]).encode() + b"\0")
        return digest.hexdigest()

    @staticmethod
    def _key(challenge_id):
        if (not isinstance(challenge_id, str) or not 32 <= len(challenge_id) <= 128
                or not challenge_id.isascii()
                or any(not (c.isalnum() or c in "-_") for c in challenge_id)):
            raise Open115Error("invalid_challenge")
        return _hashlib.sha256(challenge_id.encode()).hexdigest()

    def _row(self, db, user_id, key):
        row = db.execute("SELECT * FROM user_115_oauth_state WHERE state_hash=? AND user_id=? AND flow=?",
                         (key, user_id, FLOW_DEVICE_PKCE)).fetchone()
        if row is None:
            raise Open115Error("invalid_challenge")
        return row

    @staticmethod
    def _terminal(db, key, state):
        db.execute("UPDATE user_115_oauth_state SET status=?, code_verifier_ciphertext=NULL, "
            "upstream_uid_ciphertext=NULL, upstream_time_ciphertext=NULL, upstream_sign_ciphertext=NULL "
            "WHERE state_hash=? AND status IN ('starting','pending','scanned','exchanging')", (state, key))

    def start(self, user_id):
        if self.config.blocked_reason() or self.config.flow() != FLOW_DEVICE_PKCE:
            raise Open115Error("flow_not_available")
        now = int(self.clock())
        challenge_id = _secrets.token_urlsafe(32)
        key = self._key(challenge_id)
        verifier, challenge = pkce_pair()
        with _closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not self._active_user(db, user_id):
                raise Open115Error("invalid_user")
            latest = db.execute("SELECT MAX(created_at) AS t FROM user_115_oauth_state WHERE user_id=? "
                                "AND flow=?", (user_id, FLOW_DEVICE_PKCE)).fetchone()["t"]
            if latest is not None and now - latest < 10:
                raise Open115Error("rate_limited")
            cancel_device_challenges(db, user_id)
            context = {"v": 1, "verifier": verifier, "pair": self._pair_digest(db, user_id),
                       "client_id": self.config.client_id}
            db.execute("INSERT INTO user_115_oauth_state(state_hash,user_id,flow,code_verifier_ciphertext,"
                "status,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (key, user_id, FLOW_DEVICE_PKCE, self.fernet.encrypt(_json.dumps(context).encode()),
                 "starting", now, now + 600))
        try:
            started = self.adapter.start_device(challenge)
        except Open115Error:
            with _closing(self.connect()) as db, db:
                self._terminal(db, key, "failed")
            raise
        with _closing(self.connect()) as db, db:
            cursor = db.execute("UPDATE user_115_oauth_state SET status='pending', upstream_uid_ciphertext=?, "
                "upstream_time_ciphertext=?, upstream_sign_ciphertext=? WHERE state_hash=? AND status='starting' "
                "AND expires_at>? AND EXISTS (SELECT 1 FROM auth_user WHERE id=? AND status='active')",
                (self.fernet.encrypt(started["uid"].encode()), self.fernet.encrypt(str(started["time"]).encode()),
                 self.fernet.encrypt(started["sign"].encode()), key, int(self.clock()), user_id))
            if not cursor.rowcount:
                raise Open115Error("invalid_challenge")
        return {"challenge_id": challenge_id, "qrcode": started["qrcode"],
                "expires_in": max(0, now + 600 - int(self.clock()))}

    def cancel(self, user_id, challenge_id):
        key = self._key(challenge_id)
        with _closing(self.connect()) as db, db:
            self._row(db, user_id, key)
            self._terminal(db, key, "cancelled")
            return self._row(db, user_id, key)["status"]

    def _lease_valid(self, db, user_id, holder):
        lease = db.execute("SELECT holder,expires_at FROM user_lock WHERE lock_key=?",
                           (f"115_open_refresh:{user_id}",)).fetchone()
        return lease is not None and lease["holder"] == holder and lease["expires_at"] > int(self.clock())

    def _valid_commit(self, db, user_id, key, context, holder):
        row = self._row(db, user_id, key)
        return (row["status"] == "exchanging" and row["expires_at"] > int(self.clock())
            and self._active_user(db, user_id) and self._pair_digest(db, user_id) == context["pair"]
            and self._lease_valid(db, user_id, holder))

    def poll(self, user_id, challenge_id):
        import auth_service

        key = self._key(challenge_id)
        holder = _secrets.token_urlsafe(24)
        lock_key = f"115_open_refresh:{user_id}"
        with _closing(self.connect()) as db, db:
            self._row(db, user_id, key)
            if not self._active_user(db, user_id):
                raise Open115Error("invalid_user")
            if not auth_service.acquire_user_lock(db, lock_key, holder=holder, now=int(self.clock()), ttl=60):
                return "busy"
        try:
            return self._poll_locked(user_id, key, holder)
        finally:
            with _closing(self.connect()) as db, db:
                auth_service.release_user_lock(db, lock_key, holder=holder)

    def _poll_locked(self, user_id, key, holder):
        with _closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not self._lease_valid(db, user_id, holder):
                return "busy"
            row = self._row(db, user_id, key)
            if row["status"] in _DEVICE_TERMINAL:
                return row["status"]
            if row["expires_at"] <= int(self.clock()):
                self._terminal(db, key, "expired")
                return "expired"
            if row["status"] not in ("pending", "scanned"):
                # A crashed exchange is never replayed after its lease ends.
                self._terminal(db, key, "failed")
                return "failed"
            if row["last_polled_at"] is not None and int(self.clock()) - row["last_polled_at"] < 2:
                return row["status"]
            db.execute("UPDATE user_115_oauth_state SET last_polled_at=? WHERE state_hash=?",
                       (int(self.clock()), key))
        try:
            context = _json.loads(self.fernet.decrypt(bytes(row["code_verifier_ciphertext"])))
            if (context.get("v") != 1 or context.get("client_id") != self.config.client_id
                    or self.config.blocked_reason()):
                raise ValueError("invalid context")
            args = {name: self.fernet.decrypt(bytes(row[f"upstream_{name}_ciphertext"])).decode()
                    for name in ("uid", "time", "sign")}
        except Exception:  # noqa: BLE001 - includes retired plain-verifier rows
            with _closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if not self._lease_valid(db, user_id, holder):
                    return "busy"
                self._terminal(db, key, "failed")
            return "failed"
        try:
            state = self.adapter.poll_device(**args)
            with _closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if not self._lease_valid(db, user_id, holder):
                    return "busy"
                fresh = self._row(db, user_id, key)
                if fresh["status"] not in ("pending", "scanned"):
                    return fresh["status"]
                if fresh["expires_at"] <= int(self.clock()):
                    self._terminal(db, key, "expired")
                    return "expired"
                if state != "confirmed":
                    if state in ("expired", "cancelled"):
                        self._terminal(db, key, state)
                    else:
                        db.execute("UPDATE user_115_oauth_state SET status=? WHERE state_hash=?", (state, key))
                    return state
                db.execute("UPDATE user_115_oauth_state SET status='exchanging', consumed_at=? WHERE state_hash=?",
                           (int(self.clock()), key))
                if not self._valid_commit(db, user_id, key, context, holder):
                    self._terminal(db, key, "failed")
                    return "failed"
            token = self.adapter.exchange_device(uid=args["uid"], code_verifier=context["verifier"])
            self.adapter.verify_token(token)
            with _closing(self.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                def finish(transaction):
                    if not self._valid_commit(transaction, user_id, key, context, holder):
                        return False
                    self._terminal(transaction, key, "connected")
                    return True
                saved = store_open_token(db, user_id, token, fernet=self.fernet, now=int(self.clock()), fence=finish)
            if saved:
                return "connected"
        except Open115Error:
            pass  # No token or upstream message crosses the public boundary.
        with _closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not self._lease_valid(db, user_id, holder):
                return "busy"
            self._terminal(db, key, "failed")
            return self._row(db, user_id, key)["status"]


# ---------------------------------------------------------------------------
# Step B token lifecycle (review R06)
# ---------------------------------------------------------------------------

import threading as _threading

# One refresh per user at a time. Several requests noticing the same expiry
# must not each spend the refresh token: 115 rotates it, so the second
# exchange would be presenting a value the first has already replaced.
_REFRESH_LOCKS: dict[int, _threading.Lock] = {}
_REFRESH_LOCKS_GUARD = _threading.Lock()


def _refresh_lock(user_id: int) -> "_threading.Lock":
    with _REFRESH_LOCKS_GUARD:
        return _REFRESH_LOCKS.setdefault(int(user_id), _threading.Lock())


def store_open_token(db: sqlite3.Connection, user_id: int, token: OpenToken, *, fernet, now: int,
                     origin: str = ORIGIN_OWN_APP, expect_version: int | None = None,
                     fence=None) -> bool:
    """Write both halves, or neither. Returns whether anything was written.

    A pair is only a credential together: an access token stored beside a
    stale refresh token cannot survive its own expiry, and that is worse
    than having nothing (plan §4.4, §9.3). ``origin`` records who may rotate
    it from here on (review F02).

    ``expect_version`` and ``fence`` are the commit-time guard (review G01.1).
    A recovery that lost its lease -- because it paused long enough for the
    lease to expire and somebody else to finish -- must not still commit its
    result over the newer one. Both are checked *inside* the same short
    transaction as the write, so nothing can change between the check and the
    commit: ``expect_version`` is the access-token generation this recovery
    started from, and ``fence(db)`` is the caller's own condition (still
    holding the lease, and the user has not disconnected meanwhile).
    """
    with db:
        if expect_version is not None and secret_version(db, user_id, OPEN_ACCESS_SECRET) != expect_version:
            return False
        if fence is not None and not fence(db):
            return False
        secret_set(db, user_id, OPEN_ACCESS_SECRET, token.access_token, fernet=fernet, now=now)
        secret_set(db, user_id, OPEN_REFRESH_SECRET, token.refresh_token, fernet=fernet, now=now)
        remember_open_state(db, user_id, state=STATE_CONNECTED,
                            expires_at=token.expires_at(now), error_code=None, now=now)
        remember_open_origin(db, user_id, origin, now=now)
    return True


def store_open_pair(db: sqlite3.Connection, user_id: int, access: str, refresh: str, *, fernet,
                    now: int, origin: str, expect_version: int | None = None, fence=None) -> bool:
    """``store_open_token`` for a pair that did not come from our own adapter.

    The administrator's migrated credential is rotated by OpenList; following
    it is a write of two values read from one snapshot, not a token exchange,
    so it has no ``OpenToken`` and no expiry of its own -- but it needs the
    same all-or-nothing write and the same commit-time fence (G01.1).
    """
    with db:
        if expect_version is not None and secret_version(db, user_id, OPEN_ACCESS_SECRET) != expect_version:
            return False
        if fence is not None and not fence(db):
            return False
        secret_set(db, user_id, OPEN_ACCESS_SECRET, access, fernet=fernet, now=now)
        secret_set(db, user_id, OPEN_REFRESH_SECRET, refresh, fernet=fernet, now=now)
        remember_open_state(db, user_id, state=STATE_CONNECTED, expires_at=None,
                            error_code=None, now=now)
        remember_open_origin(db, user_id, origin, now=now)
    return True


def refresh_open_token(conn_factory, user_id: int, adapter: "Open115Adapter", *, fernet,
                       now: int, seen_version: int | None = None, fence=None) -> OpenToken | None:
    """Rotate this user's own token pair, once.

    Returns the new pair, or None when it could not be refreshed -- in which
    case the existing pair is left exactly as it was. A failure must never
    clear a credential that might still work, and must never fall back to
    anybody else's (R06).

    ``seen_version`` is the generation the caller's failed request was using.
    When the stored version has moved past it, somebody else already rotated
    and this call does nothing: no second exchange of a refresh token 115 has
    already replaced (F03). It also gates the failure write, so a late
    failure cannot mark a freshly refreshed credential as needing
    re-authorisation.
    """
    with _refresh_lock(user_id):
        db = conn_factory()
        try:
            snapshot = open_snapshot(db, user_id, fernet=fernet)
            stored_version = snapshot.version
            if seen_version is not None and stored_version > seen_version:
                return None
            if not snapshot.refresh_token:
                return None
            try:
                token = adapter.refresh(refresh_token=snapshot.refresh_token)
            except Open115Error as exc:
                # G01.1: a failure arriving after somebody else succeeded, or
                # after the user disconnected, must not write anything. The
                # condition is checked inside the transaction that would write.
                with db:
                    if secret_version(db, user_id, OPEN_ACCESS_SECRET) != stored_version:
                        return None
                    if fence is not None and not fence(db):
                        return None
                    remember_open_state(db, user_id, state=STATE_NEEDS_REAUTH, expires_at=None,
                                        error_code=exc.error_class, now=now)
                return None
            if store_open_token(db, user_id, token, fernet=fernet, now=now,
                                expect_version=stored_version, fence=fence):
                return token
            return None
        finally:
            db.close()
