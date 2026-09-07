"""HDHive resource lookup, TMDB-backed search and unlock, all upstreams faked."""

from __future__ import annotations

import requests as requests_lib

from conftest import FakeResponse

TMDB_MULTI = "https://api.themoviedb.org/3/search/multi"
RESOURCES_URL = "https://hdhive.com/api/open/resources/movie/603"
UNLOCK_URL = "https://hdhive.com/api/open/resources/unlock"
CHECKIN_URL = "https://hdhive.com/api/open/checkin"
REFRESH_URL = "https://hdhive.com/api/public/openapi/oauth/refresh"


def _authorize(hidrive, *, expires_in: int = 3600):
    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    hidrive.save_tokens({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": expires_in, "refresh_expires_in": 86400})


# --- search -----------------------------------------------------------------


def test_search_requires_tmdb_key(client):
    response = client.get("/api/hdhive/search?q=matrix")
    assert response.status_code == 503
    assert response.get_json()["code"] == "TMDB_KEY_MISSING"


def test_search_validates_query_and_media_type(client, hidrive):
    hidrive.secret_set("tmdb_api_key", "tmdb-key-for-tests")
    assert client.get("/api/hdhive/search?q=").status_code == 400
    assert client.get("/api/hdhive/search?q=matrix&media_type=book").status_code == 400
    assert client.get("/api/hdhive/search?q=" + "x" * 121).status_code == 400


def test_search_normalises_tmdb_results(client, hidrive, http):
    hidrive.secret_set("tmdb_api_key", "tmdb-key-for-tests")
    http.route(
        "GET",
        TMDB_MULTI,
        {
            "results": [
                {"id": 603, "media_type": "movie", "title": "黑客帝国", "original_title": "The Matrix", "release_date": "1999-03-31", "overview": "..."},
                {"id": 603, "media_type": "movie", "title": "duplicate"},
                {"id": 1396, "media_type": "tv", "name": "绝命毒师", "original_name": "Breaking Bad", "first_air_date": "2008-01-20"},
                {"id": 1, "media_type": "person", "name": "someone"},
            ]
        },
    )

    response = client.get("/api/hdhive/search?q=matrix")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["results"] == [
        {"id": 603, "media_type": "movie", "title": "黑客帝国", "original_title": "The Matrix", "date": "1999-03-31", "overview": "..."},
        {"id": 1396, "media_type": "tv", "title": "绝命毒师", "original_title": "Breaking Bad", "date": "2008-01-20", "overview": ""},
    ]
    (call,) = http.calls_to(TMDB_MULTI)
    assert call["params"]["api_key"] == "tmdb-key-for-tests"
    assert call["params"]["language"] == "zh-CN"


def test_search_falls_back_to_english_when_chinese_is_empty(client, hidrive, http):
    hidrive.secret_set("tmdb_api_key", "tmdb-key-for-tests")

    def by_language(**kwargs):
        if kwargs["params"]["language"] == "zh-CN":
            return FakeResponse({"results": []})
        return FakeResponse({"results": [{"id": 7, "media_type": "movie", "title": "Only in English"}]})

    http.route("GET", TMDB_MULTI, handler=by_language)

    response = client.get("/api/hdhive/search?q=obscure")

    assert response.status_code == 200
    assert [row["title"] for row in response.get_json()["results"]] == ["Only in English"]
    assert [call["params"]["language"] for call in http.calls_to(TMDB_MULTI)] == ["zh-CN", "en-US"]


def test_search_reports_tmdb_outage(client, hidrive, http):
    hidrive.secret_set("tmdb_api_key", "tmdb-key-for-tests")
    http.route("GET", TMDB_MULTI, error=requests_lib.ConnectionError("down"))

    response = client.get("/api/hdhive/search?q=matrix")

    assert response.status_code == 502
    assert response.get_json()["code"] == "TMDB_SEARCH_FAILED"


# --- resources --------------------------------------------------------------


def test_resources_validates_parameters(client):
    assert client.get("/api/hdhive/resources?media_type=book&tmdb_id=1").status_code == 400
    assert client.get("/api/hdhive/resources?media_type=movie&tmdb_id=abc").status_code == 400


def test_resources_requires_app_secret(client):
    response = client.get("/api/hdhive/resources?media_type=movie&tmdb_id=603")
    assert response.status_code == 503
    assert response.get_json()["code"] == "HDHIVE_APP_SECRET_MISSING"


def test_resources_requires_authorization(client, hidrive):
    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    response = client.get("/api/hdhive/resources?media_type=movie&tmdb_id=603")
    assert response.status_code == 401
    assert response.get_json()["code"] == "OPENAPI_REAUTH_REQUIRED"


def test_resources_proxies_upstream_with_bearer_and_api_key(client, hidrive, http):
    _authorize(hidrive)
    upstream = {"success": True, "data": [{"slug": "matrix-1999", "title": "The Matrix", "points": 0}]}
    http.route("GET", RESOURCES_URL, upstream)

    response = client.get("/api/hdhive/resources?media_type=movie&tmdb_id=603")

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "data": [{"slug": "matrix-1999", "title": "The Matrix", "points": 0}], "resource_count": 1}
    (call,) = http.calls_to(RESOURCES_URL)
    assert call["headers"]["Authorization"] == "Bearer access-1"
    assert call["headers"]["X-API-Key"] == "app-secret-for-tests"


def test_resources_filters_share_urls_and_access_codes_from_browser(client, hidrive, http):
    _authorize(hidrive)
    http.route(
        "GET",
        RESOURCES_URL,
        {"success": True, "data": [{"slug": "matrix-1999", "title": "The Matrix", "share_url": "https://115.com/s/secret", "access_code": "abcd", "download_token": "token"}]},
    )

    response = client.get("/api/hdhive/resources?media_type=movie&tmdb_id=603")

    assert response.status_code == 200
    body = response.get_json()
    assert body["data"] == [{"slug": "matrix-1999", "title": "The Matrix"}]
    assert "115.com" not in response.get_data(as_text=True)
    assert "access_code" not in response.get_data(as_text=True)


def test_upstream_refresh_demand_retries_once_with_locally_valid_token(client, hidrive, http):
    """Documents current behaviour: on OPENAPI_REFRESH_REQUIRED the app calls
    refresh_hdhive_token(), which returns the stored token unchanged while the
    local expiry still looks valid, so the request is retried once with the
    same bearer and the refresh endpoint is not contacted."""
    _authorize(hidrive)
    attempts: list[str] = []

    def resources(**kwargs):
        attempts.append(kwargs["headers"]["Authorization"])
        if len(attempts) == 1:
            return FakeResponse({"success": False, "code": "OPENAPI_REFRESH_REQUIRED"}, status=401)
        return FakeResponse({"success": True, "data": []})

    http.route("GET", RESOURCES_URL, handler=resources)

    response = client.get("/api/hdhive/resources?media_type=movie&tmdb_id=603")

    assert response.status_code == 200
    assert attempts == ["Bearer access-1", "Bearer access-1"]
    assert http.calls_to(REFRESH_URL) == []


def test_upstream_refresh_demand_rotates_token_inside_skew_window(client, hidrive, http):
    _authorize(hidrive, expires_in=3600)
    with hidrive.connect_db() as db:
        db.execute("UPDATE oauth_tokens SET expires_at=?", (hidrive.utc_now() + 100,))
    http.route("POST", REFRESH_URL, {"success": True, "data": {"access_token": "access-2", "expires_in": 3600}})
    http.route("GET", RESOURCES_URL, {"success": True, "data": []})

    response = client.get("/api/hdhive/resources?media_type=movie&tmdb_id=603")

    assert response.status_code == 200
    assert http.calls_to(RESOURCES_URL)[0]["headers"]["Authorization"] == "Bearer access-2"
    assert hidrive.decrypt_token(hidrive.get_tokens(), "access_token") == "access-2"


def test_resources_reports_upstream_outage(client, hidrive, http):
    _authorize(hidrive)
    http.route("GET", RESOURCES_URL, error=requests_lib.Timeout("slow"))

    response = client.get("/api/hdhive/resources?media_type=movie&tmdb_id=603")

    assert response.status_code == 502
    assert response.get_json()["code"] == "UPSTREAM_UNAVAILABLE"


def test_checkin_response_is_anonymized(client, hidrive, http):
    _authorize(hidrive)
    http.route("POST", CHECKIN_URL, {"success": True, "message": "ok", "share_url": "https://115.com/s/private", "token": "secret"})

    response = client.post("/api/hdhive/checkin")

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "message": "ok"}
    assert "115.com" not in response.get_data(as_text=True)
    assert "secret" not in response.get_data(as_text=True)


# --- unlock -----------------------------------------------------------------


def test_unlock_validates_slug(client, hidrive):
    _authorize(hidrive)
    assert client.post("/api/hdhive/unlock", json={"slug": "bad slug/../x"}).status_code == 400
    assert client.post("/api/hdhive/unlock", json={}).status_code == 400


def test_unlock_posts_slug_without_points_by_default(client, hidrive, http, audit_rows):
    _authorize(hidrive)
    http.route("POST", UNLOCK_URL, {"success": True, "data": {"links": ["https://115.com/s/abc?password=defg"]}})

    response = client.post("/api/hdhive/unlock", json={"slug": "matrix-1999", "allow_points": "yes"})

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "data": {"links_available": True, "link_count": 1}}
    assert "115.com" not in response.get_data(as_text=True)
    assert "password" not in response.get_data(as_text=True)
    (call,) = http.calls_to(UNLOCK_URL)
    assert call["json"] == {"slug": "matrix-1999"}
    (entry,) = audit_rows("hdhive.unlock")
    assert entry["status"] == "success"
    assert "115.com" not in entry["detail"], "share links must never be written to the audit log"


def test_unlock_forwards_explicit_points_consent(client, hidrive, http, audit_rows):
    _authorize(hidrive)
    http.route("POST", UNLOCK_URL, {"success": True, "data": {}})

    response = client.post("/api/hdhive/unlock", json={"slug": "matrix-1999", "allow_points": True})

    assert response.status_code == 200
    assert http.calls_to(UNLOCK_URL)[0]["json"] == {"slug": "matrix-1999", "allow_points": True}
    assert audit_rows("hdhive.unlock")[0]["detail"] == "points unlock requested"


def test_unlock_failure_is_passed_through_and_not_audited_as_success(client, hidrive, http, audit_rows):
    _authorize(hidrive)
    http.route("POST", UNLOCK_URL, {"success": False, "code": "INSUFFICIENT_POINTS", "message": "积分不足"}, status=402)

    response = client.post("/api/hdhive/unlock", json={"slug": "matrix-1999"})

    assert response.status_code == 402
    assert response.get_json()["code"] == "INSUFFICIENT_POINTS"
    assert audit_rows("hdhive.unlock") == []
