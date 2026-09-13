"""Phase 5: the 115 OpenAPI authorisation (step B), by contract.

Plan §4.3-§4.5, §16.3. The real flow is blocked until HiDrive-Lite has an
approved 115 application, so these exercise the adapter and the state machine
against injected sessions -- and pin the refusals that keep a blocked step B
from ever showing as connected.

Every id, secret and token below is invented.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import user_115  # noqa: E402

MEMBER_PASSWORD = "Correct1Horse"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, exc=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._exc = exc

    def json(self):
        if self._exc:
            raise self._exc
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, data=None, timeout=None, allow_redirects=None):
        self.calls.append({"url": url, "data": dict(data or {}), "timeout": timeout,
                           "allow_redirects": allow_redirects, "method": "POST"})
        return self._next()

    def get(self, url, params=None, timeout=None, allow_redirects=None, headers=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout,
                           "allow_redirects": allow_redirects, "method": "GET"})
        if headers is not None:
            self.calls[-1]["headers"] = headers
        return self._next()

    def _next(self):
        response = self._responses.pop(0) if self._responses else FakeResponse(500)
        if isinstance(response, Exception):
            raise response
        return response


APPROVED_DEVICE = user_115.OpenAppConfig(client_id="fake-client", device_flow_verified=True)
APPROVED_CODE = user_115.OpenAppConfig(
    client_id="fake-client", client_secret="fake-secret",
    redirect_uri="https://drive.example.test/api/me/115/open/callback")
TOKEN_PAYLOAD = {"data": {"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_in": 7200}}
DEVICE_PAYLOAD = {"uid": "device-uid-fixture", "time": 100, "sign": "device-sign-fixture",
                  "qrcode": "http://115.com/scan/dg-fixture"}


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


class TestBlocking:
    def test_with_no_application_step_b_is_blocked(self):
        assert user_115.OpenAppConfig().blocked_reason() == user_115.BLOCKED_NO_APP

    def test_a_client_id_alone_is_not_enough_to_start(self):
        """Device PKCE is unverified for this application and there is no
        secret or redirect for the fallback -- neither flow can run."""
        config = user_115.OpenAppConfig(client_id="fake-client")
        assert config.blocked_reason() == user_115.BLOCKED_NO_VERIFICATION

    def test_a_verified_device_application_is_not_blocked(self):
        assert APPROVED_DEVICE.blocked_reason() is None
        assert APPROVED_DEVICE.flow() == user_115.FLOW_DEVICE_PKCE

    def test_a_secret_and_redirect_unblock_the_fallback(self):
        assert APPROVED_CODE.blocked_reason() is None
        assert APPROVED_CODE.flow() == user_115.FLOW_AUTHORIZATION_CODE

    def test_an_unverified_application_cannot_start_the_device_flow(self):
        adapter = user_115.Open115Adapter(APPROVED_CODE, session=FakeSession())
        with pytest.raises(user_115.Open115Error) as exc:
            adapter.start_device("challenge")
        assert exc.value.error_class == "flow_not_available"


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


class TestPkce:
    def test_the_verifier_is_high_entropy_and_the_challenge_is_its_s256(self):
        import base64
        import hashlib

        verifier, challenge = user_115.pkce_pair()
        assert len(verifier) >= 64
        expected = base64.b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
        assert challenge == expected
        assert challenge.endswith("=")

    def test_two_pairs_never_repeat(self):
        pairs = {user_115.pkce_pair()[0] for _ in range(5)}
        assert len(pairs) == 5

    def test_standard_base64_alphabet_and_padding_are_preserved(self, monkeypatch):
        # Fixed protocol vector, not a real verifier or credential.
        monkeypatch.setattr(user_115._secrets, "token_urlsafe", lambda size: "abc")
        verifier, challenge = user_115.pkce_pair()
        assert verifier == "abc"
        assert challenge == "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0="

    def test_starting_the_flow_sends_the_challenge_and_never_the_verifier(self):
        session = FakeSession(FakeResponse(200, {"data": {"uid": "fake-uid", "time": 1, "sign": "fake-sign",
                                                          "qrcode": "https://qr.example/x"}}))
        verifier, challenge = user_115.pkce_pair()
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=session)
        started = adapter.start_device(challenge)
        sent = session.calls[0]["data"]
        assert sent["code_challenge"] == challenge
        assert verifier not in str(sent)
        assert started["uid"] == "fake-uid"

    def test_the_exchange_needs_the_verifier_the_browser_never_saw(self):
        session = FakeSession(FakeResponse(200, TOKEN_PAYLOAD))
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=session)
        token = adapter.exchange_device(uid="fake-uid", code_verifier="fake-verifier")
        assert session.calls[0]["url"] == user_115.OPEN_DEVICE_TOKEN_URL
        assert session.calls[0]["data"]["code_verifier"] == "fake-verifier"
        assert token.access_token == "fake-access"


# ---------------------------------------------------------------------------
# what counts as a token
# ---------------------------------------------------------------------------


class TestTokenShape:
    def test_all_three_fields_are_required(self):
        token = user_115.token_from_payload(TOKEN_PAYLOAD)
        assert (token.access_token, token.refresh_token, token.expires_in) == ("fake-access", "fake-refresh", 7200)
        assert token.expires_at(1_000) == 8_200

    @pytest.mark.parametrize("payload", [
        {"data": {"uid": "fake-uid"}},
        {"data": {"access_token": "fake-access"}},
        {"data": {"access_token": "fake-access", "refresh_token": "fake-refresh"}},
        {"data": {"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_in": 0}},
        {"data": {"access_token": "", "refresh_token": "fake-refresh", "expires_in": 10}},
        {"data": {"access_token": "a", "refresh_token": "r", "expires_in": "soon"}},
        "not an object",
        None,
        {"data": {"access_token": ["a"], "refresh_token": "r", "expires_in": 60}},
        {"data": {"access_token": "a", "refresh_token": {"r": 1}, "expires_in": 60}},
        {"data": {"access_token": "a", "refresh_token": "r", "expires_in": True}},
        {"data": {"access_token": "a", "refresh_token": "r", "expires_in": 1.9}},
        {"state": False, **TOKEN_PAYLOAD},
        {"code": 123, **TOKEN_PAYLOAD},
        {"error": "authorization-failed-fixture", **TOKEN_PAYLOAD},
    ])
    def test_anything_less_is_a_failure_not_a_connection(self, payload):
        """§16.3: a response carrying only a uid, or an access token with no
        refresh token, must never be recorded as connected."""
        with pytest.raises(user_115.Open115Error):
            user_115.token_from_payload(payload)

    def test_a_flat_payload_works_as_well_as_a_wrapped_one(self):
        token = user_115.token_from_payload(
            {"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_in": 60})
        assert token.expires_in == 60


# ---------------------------------------------------------------------------
# upstream failures
# ---------------------------------------------------------------------------


class TestUpstreamFailures:
    @pytest.mark.parametrize("status,error_class", [
        (429, "rate_limited"),
        (401, "reauth_required"),
        (403, "reauth_required"),
        (500, "upstream_error"),
    ])
    def test_each_upstream_answer_maps_to_one_class(self, status, error_class):
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=FakeSession(FakeResponse(status)))
        with pytest.raises(user_115.Open115Error) as exc:
            adapter.exchange_device(uid="fake-uid", code_verifier="v")
        assert exc.value.error_class == error_class

    def test_a_transport_failure_is_one_class_too(self):
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=FakeSession(OSError("reset")))
        with pytest.raises(user_115.Open115Error) as exc:
            adapter.exchange_device(uid="fake-uid", code_verifier="v")
        assert exc.value.error_class == "network_error"

    def test_a_body_that_is_not_json_is_not_a_token(self):
        adapter = user_115.Open115Adapter(
            APPROVED_DEVICE, session=FakeSession(FakeResponse(200, exc=ValueError("nope"))))
        with pytest.raises(user_115.Open115Error) as exc:
            adapter.exchange_device(uid="fake-uid", code_verifier="v")
        assert exc.value.error_class == "malformed_response"

    def test_no_failure_message_carries_a_credential(self):
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=FakeSession(FakeResponse(401)))
        with pytest.raises(user_115.Open115Error) as exc:
            adapter.exchange_device(uid="fake-uid", code_verifier="fake-verifier")
        assert "fake-verifier" not in str(exc.value)
        assert "fake-uid" not in str(exc.value)


class TestDevicePolling:
    @pytest.mark.parametrize("upstream,expected", [
        (0, "pending"), (1, "scanned"), (2, "confirmed"),
        (-1, "expired"), (-2, "cancelled"),
    ])
    def test_each_app_scan_state_is_distinct_and_polling_never_exchanges(self, upstream, expected):
        session = FakeSession(FakeResponse(200, {"code": 0, "data": {"status": upstream}}))
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=session)
        assert adapter.poll_device(uid="uid-fixture", time="100", sign="sign-fixture") == expected
        assert session.calls == [{
            "method": "GET", "url": "https://qrcodeapi.115.com/get/status/",
            "params": {"uid": "uid-fixture", "time": "100", "sign": "sign-fixture"},
            "timeout": 10.0, "allow_redirects": False,
        }]

    @pytest.mark.parametrize("status", [None, True, "2", 2.0, 3, {}, []])
    def test_unknown_or_coerced_status_is_not_a_confirmation(self, status):
        session = FakeSession(FakeResponse(200, {"data": {"status": status}}))
        with pytest.raises(user_115.Open115Error, match="malformed_response"):
            user_115.Open115Adapter(APPROVED_DEVICE, session=session).poll_device(
                uid="uid-fixture", time="100", sign="sign-fixture")

    @pytest.mark.parametrize("payload", [None, [], "invalid", {"data": {}},
        {"state": False, "data": {"status": 2}},
        {"code": 401, "data": {"status": 2}},
        {"error": "sign-fixture", "data": {"status": 2}},
    ])
    def test_upstream_failure_cannot_become_confirmed(self, payload):
        session = FakeSession(FakeResponse(200, payload))
        with pytest.raises(user_115.Open115Error) as exc:
            user_115.Open115Adapter(APPROVED_DEVICE, session=session).poll_device(
                uid="uid-fixture", time="100", sign="sign-fixture")
        assert "sign-fixture" not in str(exc.value)

    @pytest.mark.parametrize("http,error", [(302, "upstream_error"), (429, "rate_limited"),
        (401, "reauth_required"), (403, "reauth_required"), (503, "upstream_error")])
    def test_poll_http_errors_are_not_statuses(self, http, error):
        session = FakeSession(FakeResponse(http, {"data": {"status": 2}}))
        with pytest.raises(user_115.Open115Error) as exc:
            user_115.Open115Adapter(APPROVED_DEVICE, session=session).poll_device(
                uid="uid-fixture", time="100", sign="sign-fixture")
        assert exc.value.error_class == error

    def test_no_application_makes_no_poll_request(self):
        session = FakeSession()
        with pytest.raises(user_115.Open115Error):
            user_115.Open115Adapter(user_115.OpenAppConfig(), session=session).poll_device(
                uid="uid-fixture", time="100", sign="sign-fixture")
        assert session.calls == []

    def test_poll_transport_error_does_not_retain_a_secret_bearing_exception(self):
        session = FakeSession(OSError("url includes sign-fixture"))
        with pytest.raises(user_115.Open115Error) as exc:
            user_115.Open115Adapter(APPROVED_DEVICE, session=session).poll_device(
                uid="uid-fixture", time="100", sign="sign-fixture")
        assert exc.value.error_class == "network_error"
        assert exc.value.__suppress_context__ is True
        assert exc.value.__cause__ is None
        assert "sign-fixture" not in str(exc.value)


class TestDeviceChallengeShape:
    @pytest.mark.parametrize("field,value", [("uid", None), ("uid", {}),
        ("time", None), ("time", True), ("time", 0), ("time", "100"),
        ("sign", ""), ("sign", []), ("qrcode", None), ("qrcode", ""),
        pytest.param("qrcode", "x" * 4097, id="oversized-qr")])
    def test_partial_device_response_is_rejected(self, field, value):
        data = {**DEVICE_PAYLOAD, field: value}
        adapter = user_115.Open115Adapter(APPROVED_DEVICE,
            session=FakeSession(FakeResponse(200, {"data": data})))
        with pytest.raises(user_115.Open115Error):
            adapter.start_device("challenge-fixture")


class TestNewTokenProbe:
    def test_only_readonly_root_probe_and_no_refresh(self):
        session = FakeSession(FakeResponse(200, {"state": True, "data": []}))
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=session)
        adapter.verify_token(user_115.OpenToken("access-fixture", "refresh-fixture", 60))
        assert session.calls == [{"url": "https://proapi.115.com/open/ufile/files", "method": "GET",
            "params": {"cid": "0", "limit": 1, "offset": 0, "show_dir": 1},
            "headers": {"Authorization": "Bearer access-fixture"}, "timeout": 10.0, "allow_redirects": False}]

    @pytest.mark.parametrize("response", [FakeResponse(302), FakeResponse(401),
        FakeResponse(200, {"state": False, "data": []}),
        FakeResponse(200, {"state": True, "data": {}}),
        FakeResponse(200, {"data": []}), OSError("access-fixture")])
    def test_probe_failure_never_falls_back_or_refreshes(self, response):
        session = FakeSession(response)
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=session)
        with pytest.raises(user_115.Open115Error) as exc:
            adapter.verify_token(user_115.OpenToken("access-fixture", "refresh-fixture", 60))
        assert len(session.calls) == 1 and session.calls[0]["method"] == "GET"
        assert "access-fixture" not in str(exc.value)

    def test_post_requests_never_follow_redirects_with_verifier_or_tokens(self):
        session = FakeSession(FakeResponse(302, TOKEN_PAYLOAD))
        with pytest.raises(user_115.Open115Error):
            user_115.Open115Adapter(APPROVED_DEVICE, session=session).exchange_device(
                uid="uid-fixture", code_verifier="verifier-fixture")
        assert session.calls[0]["allow_redirects"] is False

    def test_server_failure_is_rejected_even_with_a_device_body(self):
        adapter = user_115.Open115Adapter(APPROVED_DEVICE,
            session=FakeSession(FakeResponse(200, {"code": 401, "data": DEVICE_PAYLOAD})))
        with pytest.raises(user_115.Open115Error):
            adapter.start_device("challenge-fixture")


# ---------------------------------------------------------------------------
# the authorisation-code fallback
# ---------------------------------------------------------------------------


class TestAuthorizationCode:
    def test_the_authorize_url_carries_the_state_and_never_the_secret(self):
        adapter = user_115.Open115Adapter(APPROVED_CODE, session=FakeSession())
        url = adapter.authorize_url(state="fake-state")
        assert url.startswith(user_115.OPEN_AUTHORIZE_URL)
        assert "state=fake-state" in url
        assert "fake-secret" not in url
        assert "client_secret" not in url

    def test_the_exchange_sends_the_secret_server_to_server(self):
        session = FakeSession(FakeResponse(200, TOKEN_PAYLOAD))
        adapter = user_115.Open115Adapter(APPROVED_CODE, session=session)
        adapter.exchange_code(code="fake-code")
        sent = session.calls[0]
        assert sent["url"] == user_115.OPEN_CODE_TOKEN_URL
        assert sent["data"]["client_secret"] == "fake-secret"
        assert sent["data"]["grant_type"] == "authorization_code"

    def test_without_a_secret_the_exchange_refuses_rather_than_guessing(self):
        adapter = user_115.Open115Adapter(
            user_115.OpenAppConfig(client_id="fake-client", redirect_uri="https://x.test/cb"),
            session=FakeSession())
        with pytest.raises(user_115.Open115Error) as exc:
            adapter.exchange_code(code="fake-code")
        assert exc.value.error_class == "flow_not_available"


class TestRefresh:
    def test_a_refresh_returns_a_whole_new_pair(self):
        session = FakeSession(FakeResponse(200, TOKEN_PAYLOAD))
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=session)
        token = adapter.refresh(refresh_token="fake-old-refresh")
        assert session.calls[0]["url"] == user_115.OPEN_REFRESH_URL
        assert token.refresh_token == "fake-refresh"

    def test_a_refresh_that_returns_half_a_pair_raises(self):
        session = FakeSession(FakeResponse(200, {"data": {"access_token": "only-access", "expires_in": 60}}))
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=session)
        with pytest.raises(user_115.Open115Error):
            adapter.refresh(refresh_token="fake-old-refresh")


# ---------------------------------------------------------------------------
# what we never do
# ---------------------------------------------------------------------------


class TestForbiddenShortcuts:
    def test_no_openlist_or_public_broker_endpoint_is_referenced(self):
        """Checked against the string literals the module actually uses --
        the comment that explains why these are forbidden is allowed to name
        them, an address the code could dial is not."""
        import ast

        tree = ast.parse((ROOT / "user_115.py").read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value not in docstrings]
        for forbidden in ("api.oplist.org", "115cloud_qr", "get_qr", "check_status"):
            offenders = [text for text in literals if forbidden in text]
            assert offenders == [], f"{forbidden} appears in {offenders}"

    def test_every_endpoint_used_is_115s_own_passport_api(self):
        for url in (user_115.OPEN_AUTHORIZE_URL, user_115.OPEN_DEVICE_CODE_URL,
                    user_115.OPEN_DEVICE_TOKEN_URL, user_115.OPEN_CODE_TOKEN_URL,
                    user_115.OPEN_REFRESH_URL):
            assert url.startswith("https://passportapi.115.com/open/"), url

    def test_a_uid_can_never_become_an_access_token(self):
        """The one shortcut the plan calls out by name: OpenList's web QR
        returns a uid, and a uid is not a token."""
        with pytest.raises(user_115.Open115Error):
            user_115.token_from_payload({"data": {"uid": "fake-uid", "access_token": "fake-uid"}})


# ---------------------------------------------------------------------------
# through the app, with no approved application
# ---------------------------------------------------------------------------


class TestBlockedThroughTheApp:
    def test_status_reports_step_b_as_blocked_and_never_as_connected(self, client):
        payload = client.get("/api/me/115/status").get_json()
        assert payload["browse"]["state"] == "blocked"
        assert payload["browse"]["blocked_reason"] == user_115.BLOCKED_NO_APP
        assert payload["browse"]["label"] == "待连接"

    def test_starting_step_b_refuses_with_the_reason_and_writes_nothing(self, client, hidrive):
        response = client.post("/api/me/115/open/start", json={})
        assert response.status_code == 409
        assert response.get_json()["blocked_reason"] == user_115.BLOCKED_NO_APP
        with hidrive.connect_db() as db:
            assert db.execute("SELECT COUNT(*) AS c FROM user_115_oauth_state").fetchone()["c"] == 0

    def test_step_a_is_unaffected_by_step_b_being_blocked(self, client, hidrive):
        hidrive.secret_set("115_cookie", "the-administrators-cookie")
        payload = client.get("/api/me/115/status").get_json()
        assert payload["transfer"]["state"] == "connected"
        assert payload["browse"]["state"] == "blocked"

    def test_disconnecting_one_step_leaves_the_other(self, client, hidrive):
        import auth_service as auth

        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            user_115.secret_set(db, admin_id, user_115.COOKIE_SECRET, "fake-cookie",
                                fernet=hidrive.load_fernet(), now=1)
            user_115.secret_set(db, admin_id, user_115.OPEN_ACCESS_SECRET, "fake-access",
                                fernet=hidrive.load_fernet(), now=1)
            user_115.secret_set(db, admin_id, user_115.OPEN_REFRESH_SECRET, "fake-refresh",
                                fernet=hidrive.load_fernet(), now=1)
        assert client.post("/api/me/115/disconnect", json={"step": "browse"}).status_code == 200
        with hidrive.connect_db() as db:
            credentials = user_115.credentials_for(db, admin_id, fernet=hidrive.load_fernet())
        assert credentials.has_cookie is True
        assert credentials.has_open_token is False

    def test_disconnect_refuses_a_step_it_does_not_know(self, client):
        assert client.post("/api/me/115/disconnect", json={"step": "everything"}).status_code == 400

    def test_no_response_carries_a_client_secret(self, client, hidrive):
        hidrive.secret_set("115_open_client_secret", "fake-client-secret")
        for url in ("/api/me/115/status",):
            body = client.get(url).get_data(as_text=True)
            assert "fake-client-secret" not in body


# ---------------------------------------------------------------------------
# Review 2026-09-12 R06: whose token gets refreshed, and what a failure costs
# ---------------------------------------------------------------------------


@pytest.fixture
def token_db(tmp_path):
    import sqlite3

    import auth_service as auth

    path = tmp_path / "hidrive.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT, actor TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE reauth_challenges (id_hash TEXT PRIMARY KEY, actor TEXT NOT NULL,
            state TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
    """)
    auth.ensure_schema(conn)
    admin = auth.ensure_admin_user(conn, email="admin@example.invalid", now=1)
    first = auth.create_pending_user(conn, email="a@example.test", password=MEMBER_PASSWORD,
                                     display_name=None, now=1)
    second = auth.create_pending_user(conn, email="b@example.test", password=MEMBER_PASSWORD,
                                      display_name=None, now=1)
    conn.commit()
    conn.close()

    def factory():
        made = sqlite3.connect(path)
        made.row_factory = sqlite3.Row
        return made

    factory.users = (admin, first, second)
    return factory


@pytest.fixture
def token_fernet():
    import base64
    import os

    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(os.urandom(32)))


def _seed_pair(factory, user_id, fernet, access="old-access", refresh="old-refresh"):
    db = factory()
    try:
        user_115.secret_set(db, user_id, user_115.OPEN_ACCESS_SECRET, access, fernet=fernet, now=1)
        user_115.secret_set(db, user_id, user_115.OPEN_REFRESH_SECRET, refresh, fernet=fernet, now=1)
        db.commit()
    finally:
        db.close()


class TestPerUserRefresh:
    def test_it_rotates_both_halves_together(self, token_db, token_fernet):
        _admin, first, _second = token_db.users
        _seed_pair(token_db, first, token_fernet)
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=FakeSession(FakeResponse(200, TOKEN_PAYLOAD)))
        token = user_115.refresh_open_token(token_db, first, adapter, fernet=token_fernet, now=100)
        assert token is not None
        db = token_db()
        try:
            credentials = user_115.credentials_for(db, first, fernet=token_fernet)
            profile = user_115.profile(db, first)
        finally:
            db.close()
        assert credentials.access_token == "fake-access"
        assert credentials.refresh_token == "fake-refresh"
        assert profile["open_state"] == user_115.STATE_CONNECTED
        assert profile["open_expires_at"] == 100 + 7200

    def test_it_refreshes_only_that_user(self, token_db, token_fernet):
        _admin, first, second = token_db.users
        _seed_pair(token_db, first, token_fernet)
        _seed_pair(token_db, second, token_fernet, access="b-access", refresh="b-refresh")
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=FakeSession(FakeResponse(200, TOKEN_PAYLOAD)))
        user_115.refresh_open_token(token_db, first, adapter, fernet=token_fernet, now=100)
        db = token_db()
        try:
            other = user_115.credentials_for(db, second, fernet=token_fernet)
        finally:
            db.close()
        assert (other.access_token, other.refresh_token) == ("b-access", "b-refresh")

    def test_a_failure_leaves_the_old_pair_exactly_as_it_was(self, token_db, token_fernet):
        _admin, first, _second = token_db.users
        _seed_pair(token_db, first, token_fernet)
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=FakeSession(FakeResponse(401)))
        assert user_115.refresh_open_token(token_db, first, adapter, fernet=token_fernet, now=100) is None
        db = token_db()
        try:
            credentials = user_115.credentials_for(db, first, fernet=token_fernet)
            profile = user_115.profile(db, first)
        finally:
            db.close()
        assert (credentials.access_token, credentials.refresh_token) == ("old-access", "old-refresh")
        assert profile["open_state"] == user_115.STATE_NEEDS_REAUTH
        assert profile["open_error_code"] == "reauth_required"

    def test_half_a_pair_back_from_115_is_not_stored(self, token_db, token_fernet):
        _admin, first, _second = token_db.users
        _seed_pair(token_db, first, token_fernet)
        adapter = user_115.Open115Adapter(
            APPROVED_DEVICE,
            session=FakeSession(FakeResponse(200, {"data": {"access_token": "only", "expires_in": 60}})))
        assert user_115.refresh_open_token(token_db, first, adapter, fernet=token_fernet, now=100) is None
        db = token_db()
        try:
            assert user_115.credentials_for(db, first, fernet=token_fernet).access_token == "old-access"
        finally:
            db.close()

    @pytest.mark.parametrize("failure", [{"code": 401}, {"state": False},
        {"error": "rejected-fixture"}])
    def test_http_200_business_failure_preserves_both_old_secrets(self, token_db, token_fernet, failure):
        from contextlib import closing

        _admin, first, _second = token_db.users
        _seed_pair(token_db, first, token_fernet)
        with closing(token_db()) as db:
            before = tuple(db.execute(
                "SELECT name,ciphertext,version FROM user_secret WHERE user_id=? ORDER BY name", (first,)))
        adapter = user_115.Open115Adapter(APPROVED_DEVICE,
            session=FakeSession(FakeResponse(200, {**TOKEN_PAYLOAD, **failure})))
        assert user_115.refresh_open_token(token_db, first, adapter, fernet=token_fernet, now=100) is None
        with closing(token_db()) as db:
            after = tuple(db.execute(
                "SELECT name,ciphertext,version FROM user_secret WHERE user_id=? ORDER BY name", (first,)))
            credentials = user_115.credentials_for(db, first, fernet=token_fernet)
        assert after == before
        assert (credentials.access_token, credentials.refresh_token) == ("old-access", "old-refresh")

    def test_a_user_with_no_refresh_token_is_not_refreshed(self, token_db, token_fernet):
        _admin, first, _second = token_db.users
        db = token_db()
        try:
            user_115.secret_set(db, first, user_115.OPEN_ACCESS_SECRET, "lonely", fernet=token_fernet, now=1)
            db.commit()
        finally:
            db.close()
        session = FakeSession(FakeResponse(200, TOKEN_PAYLOAD))
        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=session)
        assert user_115.refresh_open_token(token_db, first, adapter, fernet=token_fernet, now=100) is None
        assert session.calls == [], "nothing to present, so nothing is asked"

    def test_concurrent_refreshes_for_one_user_only_spend_the_token_once(self, token_db, token_fernet):
        """115 rotates the refresh token, so a second exchange would present
        a value the first has already replaced."""
        import threading

        _admin, first, _second = token_db.users
        _seed_pair(token_db, first, token_fernet)
        calls = []

        class CountingSession:
            def post(self, url, data=None, timeout=None, allow_redirects=None):
                calls.append(data)
                import time as _t
                _t.sleep(0.05)
                return FakeResponse(200, TOKEN_PAYLOAD)

        adapter = user_115.Open115Adapter(APPROVED_DEVICE, session=CountingSession())
        threads = [threading.Thread(target=user_115.refresh_open_token,
                                    args=(token_db, first, adapter),
                                    kwargs={"fernet": token_fernet, "now": 100}) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # The later arrivals find the rotated pair and present that instead;
        # what must not happen is four exchanges of the same stale token.
        assert len([c for c in calls if c.get("refresh_token") == "old-refresh"]) == 1

    def test_an_expired_member_token_never_falls_back_to_the_administrators(self, client, hidrive):
        """R06 through the app: the cloud client must not re-sync the
        administrator's OpenList on a member's behalf."""
        import auth_service as auth

        hidrive.secret_set("115_open_access_token", "the-administrators-token")
        with hidrive.connect_db() as db:
            member = auth.create_pending_user(db, email="expired@example.test",
                                              password=MEMBER_PASSWORD, display_name=None, now=1)
            user_115.secret_set(db, member, user_115.OPEN_ACCESS_SECRET, "expired-own",
                                fernet=hidrive.load_fernet(), now=1)
        calls = []
        import types
        hidrive.sync_openlist_credentials_from_source  # present
        original = hidrive.sync_openlist_credentials_from_source
        hidrive.sync_openlist_credentials_from_source = lambda *a, **k: calls.append(1) or True
        try:
            with hidrive.app.test_request_context("/"):
                hidrive.g.current_user = auth.CurrentUser(id=member, email="m", role="member", status="active")
                assert hidrive._has_own_open_token(hidrive.current_user()) is True
                assert hidrive._refresh_own_open_token(hidrive.current_user()) is False
        finally:
            hidrive.sync_openlist_credentials_from_source = original
        assert calls == [], "a member never triggers the administrator's sync"
        assert isinstance(types, object)
