"""HDHive OAuth start/callback/refresh, with the token endpoints faked."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

TOKEN_URL = "https://hdhive.com/api/public/openapi/oauth/token"
REFRESH_URL = "https://hdhive.com/api/public/openapi/oauth/refresh"


def _start(client, hidrive) -> str:
    hidrive.secret_set("hdhive_client_id", "client-id-for-tests")
    response = client.post("/api/hdhive/oauth/start")
    assert response.status_code == 200
    query = parse_qs(urlparse(response.get_json()["url"]).query)
    return query["state"][0]


def test_oauth_start_requires_client_id(client):
    response = client.post("/api/hdhive/oauth/start")
    assert response.status_code == 503
    assert response.get_json()["code"] == "HDHIVE_CLIENT_ID_MISSING"


def test_oauth_start_issues_authorize_url_and_persists_state(client, hidrive):
    hidrive.secret_set("hdhive_client_id", "client-id-for-tests")
    response = client.post("/api/hdhive/oauth/start")
    assert response.status_code == 200
    url = response.get_json()["url"]
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://hdhive.com/openapi/authorize"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["client-id-for-tests"]
    assert query["redirect_uri"] == ["https://hidrive.test/api/oauth/hdhive/callback"]
    assert query["scope"] == ["query unlock write"]
    with hidrive.connect_db() as db:
        row = db.execute("SELECT * FROM oauth_states WHERE state=?", (query["state"][0],)).fetchone()
    assert row is not None and row["used_at"] is None


def test_oauth_callback_rejects_missing_parameters(client):
    response = client.get("/api/oauth/hdhive/callback")
    assert response.status_code == 400
    assert response.get_json()["code"] == "OAUTH_CALLBACK_INVALID"


def test_oauth_callback_rejects_unknown_state(client):
    response = client.get("/api/oauth/hdhive/callback?code=abc&state=not-issued")
    assert response.status_code == 400
    assert response.get_json()["code"] == "OAUTH_STATE_INVALID"


def test_oauth_callback_requires_app_secret(client, hidrive):
    state = _start(client, hidrive)
    response = client.get(f"/api/oauth/hdhive/callback?code=abc&state={state}")
    assert response.status_code == 503
    assert response.get_json()["code"] == "HDHIVE_CREDENTIALS_MISSING"


def test_oauth_callback_rejects_state_from_another_actor(client, hidrive, monkeypatch, audit_rows):
    hidrive.secret_set("hdhive_client_id", "client-id-for-tests")
    actors = iter(("owner", "attacker"))
    monkeypatch.setattr(hidrive, "actor_id", lambda: next(actors))
    state = _start(client, hidrive)

    response = client.get(f"/api/oauth/hdhive/callback?code=auth-code&state={state}")

    assert response.status_code == 403
    assert response.get_json()["code"] == "OAUTH_STATE_ACTOR_MISMATCH"
    assert hidrive.get_tokens() is None
    assert audit_rows("hdhive.oauth.callback")[0]["detail"] == "state actor mismatch"


def test_oauth_callback_exchanges_code_and_stores_encrypted_tokens(client, hidrive, http, audit_rows):
    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    state = _start(client, hidrive)
    http.route(
        "POST",
        TOKEN_URL,
        {"success": True, "data": {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600, "refresh_expires_in": 86400, "scope": "query unlock write"}},
    )

    response = client.get(f"/api/oauth/hdhive/callback?code=auth-code&state={state}")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/?oauth=success")
    (call,) = http.calls_to(TOKEN_URL)
    assert call["headers"]["X-API-Key"] == "app-secret-for-tests"
    assert call["json"] == {"grant_type": "authorization_code", "code": "auth-code", "redirect_uri": "https://hidrive.test/api/oauth/hdhive/callback"}
    row = hidrive.get_tokens()
    assert hidrive.decrypt_token(row, "access_token") == "access-1"
    assert hidrive.decrypt_token(row, "refresh_token") == "refresh-1"
    assert b"access-1" not in bytes(row["access_token"])
    assert row["scope"] == "query unlock write"
    assert audit_rows("hdhive.oauth.callback")[0]["status"] == "success"


def test_oauth_state_cannot_be_replayed(client, hidrive, http):
    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    state = _start(client, hidrive)
    http.route("POST", TOKEN_URL, {"success": True, "data": {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}})
    assert client.get(f"/api/oauth/hdhive/callback?code=auth-code&state={state}").status_code == 302

    replay = client.get(f"/api/oauth/hdhive/callback?code=auth-code&state={state}")

    assert replay.status_code == 400
    assert replay.get_json()["code"] == "OAUTH_STATE_INVALID"


def test_oauth_callback_surfaces_upstream_rejection(client, hidrive, http):
    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    state = _start(client, hidrive)
    http.route(
        "POST",
        TOKEN_URL,
        {
            "success": False,
            "code": "INVALID_GRANT",
            "message": "code expired; contact https://hdhive.com/reset with password=secret",
        },
        status=400,
    )

    response = client.get(f"/api/oauth/hdhive/callback?code=stale&state={state}")

    assert response.status_code == 400
    body = response.get_json()
    assert body["success"] is False
    assert body["code"] == "INVALID_GRANT"
    assert "hdhive.com" not in body["message"]
    assert "password=secret" not in body["message"]
    assert hidrive.get_tokens() is None


def test_oauth_callback_handles_upstream_outage(client, hidrive, http):
    import requests as requests_lib

    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    state = _start(client, hidrive)
    http.route("POST", TOKEN_URL, error=requests_lib.ConnectionError("down"))

    response = client.get(f"/api/oauth/hdhive/callback?code=auth-code&state={state}")

    assert response.status_code == 502
    assert response.get_json()["code"] == "HDHIVE_UNAVAILABLE"


def test_expired_access_token_is_refreshed_before_use(hidrive, workspace, http, audit_rows):
    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    hidrive.save_tokens({"access_token": "old-access", "refresh_token": "refresh-1", "expires_in": 1, "refresh_expires_in": 86400})
    with hidrive.connect_db() as db:
        db.execute("UPDATE oauth_tokens SET expires_at=?", (hidrive.utc_now() - 10,))
    http.route("POST", REFRESH_URL, {"success": True, "data": {"access_token": "new-access", "expires_in": 3600}})

    token = hidrive.valid_hdhive_access_token()

    assert token == "new-access"
    (call,) = http.calls_to(REFRESH_URL)
    assert call["json"] == {"refresh_token": "refresh-1"}
    assert call["headers"]["X-API-Key"] == "app-secret-for-tests"
    row = hidrive.get_tokens()
    assert hidrive.decrypt_token(row, "refresh_token") == "refresh-1", "refresh token is kept when the refresh response omits it"
    assert audit_rows("hdhive.oauth.refresh")[-1]["status"] == "success"


def test_refresh_failure_leaves_existing_tokens_untouched(hidrive, workspace, http, audit_rows):
    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    hidrive.save_tokens({"access_token": "old-access", "refresh_token": "refresh-1", "expires_in": 1})
    with hidrive.connect_db() as db:
        db.execute("UPDATE oauth_tokens SET expires_at=?", (hidrive.utc_now() - 10,))
    http.route("POST", REFRESH_URL, {"success": False, "code": "OPENAPI_REFRESH_INVALID"}, status=401)

    assert hidrive.valid_hdhive_access_token() is None
    assert hidrive.decrypt_token(hidrive.get_tokens(), "access_token") == "old-access"
    assert audit_rows("hdhive.oauth.refresh")[-1] == {"action": "hdhive.oauth.refresh", "status": "failed", "detail": "OPENAPI_REFRESH_INVALID", "actor": "system"}
