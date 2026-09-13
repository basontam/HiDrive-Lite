"""HDHive resource lookup, TMDB-backed search and unlock, all upstreams faked."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import requests as requests_lib

from conftest import FakeResponse
from test_re0_search_api import re0_env, _unlock_payload  # noqa: F401

TMDB_MULTI = "https://api.themoviedb.org/3/search/multi"
RESOURCES_URL = "https://re0.me/api/open/resources/movie/603"
UNLOCK_URL = "https://re0.me/api/open/resources/unlock"
CHECKIN_URL = "https://re0.me/api/open/checkin"
TOKEN_URL = "https://re0.me/api/public/openapi/oauth/token"
REFRESH_URL = "https://re0.me/api/public/openapi/oauth/refresh"


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
    assert response.get_json() == {**upstream, "resource_count": 1}
    (call,) = http.calls_to(RESOURCES_URL)
    assert call["headers"]["Authorization"] == "Bearer access-1"
    assert call["headers"]["X-API-Key"] == "app-secret-for-tests"


def test_resources_filters_share_urls_and_access_codes_from_browser(client, hidrive, http):
    _authorize(hidrive)
    http.route("GET", RESOURCES_URL, {"success": True, "data": [{
        "slug": "fixture-movie", "title": "Fixture Movie",
        "description": "下载 https://115.com/s/swfakeprivate?password=ab12，访问码：cd34",
        "share_url": "https://115.com/s/swfakeprivate", "access_code": "cd34",
        "download_token": "private-token-fixture",
    }]})
    response = client.get("/api/hdhive/resources?media_type=movie&tmdb_id=603")
    assert response.status_code == 200
    assert response.get_json()["data"] == [{
        "slug": "fixture-movie", "title": "Fixture Movie", "description": "下载 [redacted-url]",
    }]
    for private in ("115.com", "access_code", "swfakeprivate", "cd34", "private-token-fixture"):
        assert private not in response.get_data(as_text=True)


def test_resources_redacts_bare_provider_urls_and_spaced_access_codes(client, hidrive, http):
    _authorize(hidrive)
    http.route("GET", RESOURCES_URL, {"success": True, "data": [{
        "slug": "fixture-movie", "description": "备用 115cdn.com/s/swfakebare access code: ab12",
    }]})
    response = client.get("/api/hdhive/resources?media_type=movie&tmdb_id=603")
    assert response.status_code == 200
    assert response.get_json()["data"] == [{
        "slug": "fixture-movie", "description": "备用 [redacted-url] access code=[redacted]",
    }]
    assert "115cdn.com" not in response.get_data(as_text=True)
    assert "ab12" not in response.get_data(as_text=True)


def test_checkin_response_and_saved_message_are_anonymized(client, hidrive, http):
    _authorize(hidrive)
    http.route("POST", CHECKIN_URL, {
        "success": True, "message": "ok token=private-token-fixture",
        "share_url": "https://115.com/s/swfakeprivate", "token": "private-token-fixture",
    })
    response = client.post("/api/hdhive/checkin")
    assert response.status_code == 200
    assert response.get_json() == {"success": True, "message": "ok token=[redacted]"}
    with hidrive.connect_db() as db:
        assert db.execute("SELECT message FROM checkins ORDER BY id DESC LIMIT 1").fetchone()[0] == "ok token=[redacted]"


def test_oauth_rejection_does_not_echo_upstream_private_values(client, hidrive, http):
    hidrive.secret_set("hdhive_client_id", "client-id-for-tests")
    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    url = client.post("/api/hdhive/oauth/start").get_json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    http.route("POST", TOKEN_URL, {
        "success": False, "code": "INVALID_GRANT",
        "message": "token=private-token-fixture https://115.com/s/swfakeprivate",
    }, status=400)
    response = client.get(f"/api/oauth/hdhive/callback?code=fixture-code&state={state}")
    assert response.status_code == 400
    assert response.get_json() == {
        "success": False, "code": "INVALID_GRANT", "message": "token=[redacted] [redacted-url]",
    }
    assert hidrive.get_tokens() is None


@pytest.mark.parametrize("success", [True, False])
def test_coordinated_legacy_unlock_keeps_private_payload_server_side(
        client, re0_env, hidrive, http, audit_rows, success):
    assert client.get("/api/library/search/re0?q=本地片&type=all").status_code == 200
    upstream = _unlock_payload("https://115.com/s/swfakeprivate", "cd34") if success else {
        "success": False, "code": "INSUFFICIENT_POINTS",
        "message": "token=private-token-fixture https://115.com/s/swfakeprivate",
    }
    upstream["token"] = "private-token-fixture"
    http.route("POST", UNLOCK_URL, upstream, status=200 if success else 402)
    response = client.post("/api/hdhive/unlock", json={"slug": "fixture-slug-a", "allow_points": True})
    assert response.status_code == (200 if success else 402), response.get_json()
    body = response.get_json()
    for private in ("115.com", "swfakeprivate", "cd34", "private-token-fixture"):
        assert private not in response.get_data(as_text=True)
        assert all(private not in row["detail"] for row in audit_rows("hdhive.unlock"))
    if success:
        # Sanitise only the browser boundary: the encrypted local result and
        # its public id must survive so the next click never spends twice.
        assert body["success"] is True and body["link_public_id"]
        assert body["data"]["links_available"] is True
        assert body["data"]["link_count"] == 1
        row = re0_env["store"].link_by_public_id(body["link_public_id"])
        assert row is not None
        assert re0_env["store"].fernet.decrypt(bytes(row["url_ciphertext"])).decode() == "https://115.com/s/swfakeprivate"
        assert re0_env["store"].fernet.decrypt(bytes(row["access_code_ciphertext"])).decode() == "cd34"
        replay = client.post("/api/hdhive/unlock", json={"slug": "fixture-slug-a"})
        assert replay.get_json()["link_public_id"] == body["link_public_id"]
        assert replay.get_json()["replayed"] is True
        assert len(http.calls_to(UNLOCK_URL)) == 1
    else:
        assert body == {"success": False, "code": "INSUFFICIENT_POINTS", "message": "token=[redacted] [redacted-url]"}


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


# --- unlock -----------------------------------------------------------------


def test_unlock_validates_slug(client, hidrive):
    _authorize(hidrive)
    assert client.post("/api/hdhive/unlock", json={"slug": "bad slug/../x"}).status_code == 400
    assert client.post("/api/hdhive/unlock", json={}).status_code == 400


def test_unlock_refuses_when_there_is_no_library_to_coordinate_through(client, hidrive, http, audit_rows):
    """G03.2: this endpoint used to fall through to the upstream whenever the
    resource library could not be opened. That was safe while there was one
    caller; with several users it is exactly how two purchases happen. It now
    refuses, and nothing goes upstream."""
    _authorize(hidrive)
    http.route("POST", UNLOCK_URL, {"success": True, "data": {}})

    response = client.post("/api/hdhive/unlock", json={"slug": "matrix-1999"})

    assert response.status_code == 503
    assert response.get_json()["code"] == "RE0_COORDINATION_UNAVAILABLE"
    assert http.calls_to(UNLOCK_URL) == [], "a refusal must not reach RE0"
    assert audit_rows("hdhive.unlock") == []


def test_unlock_refuses_a_slug_with_no_local_candidate(client, hidrive, http, workspace, audit_rows):
    """G03.4: with a library but no candidate row, there is nowhere to record
    the result -- so there is no way to make the next click free. Refuse before
    spending."""
    import library_store as ls

    _authorize(hidrive)
    store = ls.LibraryStore(hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())
    store.create_schema()
    store.meta_set("encrypted", "1")
    http.route("POST", UNLOCK_URL, {"success": True, "data": {}})

    response = client.post("/api/hdhive/unlock", json={"slug": "nobody-knows-this"})

    assert response.status_code == 409
    assert response.get_json()["code"] == "RE0_CANDIDATE_UNKNOWN"
    assert http.calls_to(UNLOCK_URL) == []
    assert audit_rows("hdhive.unlock")[-1]["status"] == "refused"
