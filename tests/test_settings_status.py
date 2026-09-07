"""Settings persistence, status reporting and the health endpoint."""

from __future__ import annotations

USER_URL = "https://my.115.com/?ct=ajax&ac=get_user_aq"


def test_healthz_reports_database_and_strm_root(client, hidrive):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "service": "HiDrive-Lite", "database": True, "strm_root": True}


def test_status_on_fresh_install(client, hidrive):
    response = client.get("/api/status")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["auth_mode"] == "local"
    assert body["public_origin"] == "https://hidrive.test"
    assert body["hdhive"]["authorized"] is False
    assert body["hdhive"]["app_secret_configured"] is False
    assert body["hdhive"]["checkin"] == {"last_success": None, "last_at": None}
    assert body["115"]["cookie_configured"] is False
    assert body["115"]["open_platform_configured"] is False
    assert body["openlist"] == {"url": "http://openlist.test", "token_configured": False, "paths": {"115pan": "/115pan", "115strm": "/115strm"}}
    assert body["strm"] == {"root": str(hidrive.STRM_ROOT), "exists": True}


def test_status_115_block_reports_reauth_available_and_retry_after_on_fresh_install(client, hidrive):
    # T19 fix wave 1 item 3/8: the settings page's reauth affordances read
    # off these two fields -- both must be present (and sane) even before
    # any cookie or challenge exists.
    body = client.get("/api/status").get_json()["115"]
    assert body["reauth_available"] is True
    assert body["retry_after"] is None


def test_settings_stores_trimmed_secrets_encrypted(client, hidrive, audit_rows):
    response = client.post(
        "/api/settings",
        json={"hdhive_app_secret": "  secret-value  ", "hdhive_client_id": "client-1", "tmdb_api_key": "tmdb-1", "115_target_pid": " 123 ", "not_allowed": "ignored"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "message": "设置已保存"}
    assert hidrive.secret_get("hdhive_app_secret") == "secret-value"
    assert hidrive.secret_get("hdhive_client_id") == "client-1"
    assert hidrive.secret_get("tmdb_api_key") == "tmdb-1"
    assert hidrive.secret_get("not_allowed") is None
    assert hidrive.setting_get("115_target_pid") == "123"
    with hidrive.connect_db() as db:
        blobs = b"".join(bytes(row["value"]) for row in db.execute("SELECT value FROM secrets"))
    assert b"secret-value" not in blobs and b"tmdb-1" not in blobs
    assert audit_rows("settings.update")[0]["detail"] == "115_target_pid,hdhive_app_secret,hdhive_client_id,tmdb_api_key"


def test_settings_ignores_blank_values(client, hidrive):
    hidrive.secret_set("tmdb_api_key", "keep-me")

    client.post("/api/settings", json={"tmdb_api_key": "   "})

    assert hidrive.secret_get("tmdb_api_key") == "keep-me"


def test_settings_validates_115_cookie_immediately(client, hidrive, http):
    http.route("GET", USER_URL, {"state": True, "data": {"uid": 7}})

    response = client.post("/api/settings", json={"115_cookie": "cookie-fixture"})

    assert response.get_json()["cookie_check"] == {"valid": True, "label": "可用"}
    assert http.calls_to(USER_URL)[0]["headers"]["Cookie"] == "cookie-fixture"
    status = client.get("/api/status").get_json()["115"]
    assert status["cookie_configured"] is True
    assert status["cookie_valid"] is True
    assert status["cookie_checked_at"] is not None


def test_settings_reports_invalid_115_cookie(client, hidrive, http):
    http.route("GET", USER_URL, {"state": False})

    response = client.post("/api/settings", json={"115_cookie": "cookie-fixture"})

    assert response.get_json()["cookie_check"] == {"valid": False, "label": "不可用"}
    assert client.get("/api/status").get_json()["115"]["cookie_valid"] is False


def test_status_verify_flag_rechecks_stale_cookie(client, hidrive, http):
    hidrive.secret_set("115_cookie", "cookie-fixture")
    hidrive.setting_set("115_cookie_valid", "0")
    hidrive.setting_set("115_cookie_checked_at", str(hidrive.utc_now() - 3600))
    http.route("GET", USER_URL, {"state": True, "data": {"uid": 7}})

    assert client.get("/api/status").get_json()["115"]["cookie_valid"] is False
    assert http.calls == []
    assert client.get("/api/status?verify_115=1").get_json()["115"]["cookie_valid"] is True
    assert len(http.calls_to(USER_URL)) == 1


def test_status_reflects_authorization_and_checkin_history(client, hidrive):
    hidrive.secret_set("hdhive_app_secret", "s")
    hidrive.save_tokens({"access_token": "a", "refresh_token": "r", "expires_in": 60, "scope": "query"})
    with hidrive.connect_db() as db:
        db.execute("INSERT INTO checkins(success,code,message,created_at) VALUES(1,'200','ok',?)", (hidrive.utc_now(),))

    body = client.get("/api/status").get_json()["hdhive"]

    assert body["authorized"] is True
    assert body["app_secret_configured"] is True
    assert body["scope"] == "query"
    assert body["expires_at"] is not None
    assert body["checkin"]["last_success"] is True
