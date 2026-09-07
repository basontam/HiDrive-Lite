"""Read-only OpenList browsing (faked) and STRM library browsing (temp dir)."""

from __future__ import annotations

import os

import requests as requests_lib

OPENLIST_LIST_URL = "http://openlist.test/api/fs/list"


def test_openlist_list_forwards_page_and_raw_token(client, hidrive, http):
    hidrive.secret_set("openlist_token", "openlist-token-fixture")
    upstream = {"code": 200, "message": "success", "data": {"content": [{"name": "A.strm", "is_dir": False}], "total": 1}}
    http.route("POST", OPENLIST_LIST_URL, upstream)

    response = client.get("/api/openlist/list?path=/115strm/Movies&page=2")

    assert response.status_code == 200
    assert response.get_json() == upstream
    (call,) = http.calls_to(OPENLIST_LIST_URL)
    assert call["json"] == {"path": "/115strm/Movies", "password": "", "page": 2, "per_page": 200, "refresh": False}
    assert call["headers"]["Authorization"] == "openlist-token-fixture", "OpenList tokens are sent verbatim, not as Bearer"


def test_openlist_list_defaults_and_tolerates_bad_page(client, http):
    http.route("POST", OPENLIST_LIST_URL, {"code": 200, "data": {"content": []}})

    client.get("/api/openlist/list?page=abc")

    (call,) = http.calls_to(OPENLIST_LIST_URL)
    assert call["json"]["path"] == "/"
    assert call["json"]["page"] == 1
    assert "Authorization" not in call["headers"]


def test_openlist_list_passes_upstream_status_through(client, http):
    http.route("POST", OPENLIST_LIST_URL, {"code": 403, "message": "guest browsing disabled"}, status=403)

    response = client.get("/api/openlist/list?path=/115pan")

    assert response.status_code == 403
    assert response.get_json()["message"] == "guest browsing disabled"


def test_openlist_list_reports_outage_and_bad_json(client, http):
    http.route("POST", OPENLIST_LIST_URL, error=requests_lib.ConnectionError("down"))
    outage = client.get("/api/openlist/list")
    assert outage.status_code == 502
    assert outage.get_json()["code"] == "OPENLIST_UNAVAILABLE"

    http.route("POST", OPENLIST_LIST_URL, raw=b"<html>gateway</html>")
    bad_json = client.get("/api/openlist/list")
    assert bad_json.status_code == 502
    assert bad_json.get_json()["code"] == "OPENLIST_INVALID_JSON"


# --- STRM -------------------------------------------------------------------


def _seed_strm(hidrive):
    root = hidrive.STRM_ROOT
    (root / "Movies").mkdir()
    (root / "Movies" / "A.strm").write_text("http://example/a")
    (root / "b.strm").write_text("http://example/b")
    (root / "Anime").mkdir()
    return root


def test_strm_root_listing_puts_directories_first(client, hidrive):
    _seed_strm(hidrive)

    response = client.get("/api/strm/list")

    assert response.status_code == 200
    body = response.get_json()
    assert body["root"] == str(hidrive.STRM_ROOT)
    assert body["items"] == [
        {"name": "Anime", "directory": True, "path": "Anime"},
        {"name": "Movies", "directory": True, "path": "Movies"},
        {"name": "b.strm", "directory": False, "path": "b.strm"},
    ]


def test_strm_subdirectory_listing_uses_relative_paths(client, hidrive):
    _seed_strm(hidrive)

    response = client.get("/api/strm/list?path=Movies")

    assert response.get_json()["items"] == [{"name": "A.strm", "directory": False, "path": "Movies/A.strm"}]


def test_strm_rejects_traversal_and_missing_paths(client, hidrive):
    _seed_strm(hidrive)
    for path in ("../", "Movies/../../", "/etc", "does-not-exist"):
        response = client.get(f"/api/strm/list?path={path}")
        assert response.status_code == 404, path
        assert response.get_json()["code"] == "STRM_PATH_INVALID"


def test_strm_rejects_symlink_escaping_root(client, hidrive, workspace):
    _seed_strm(hidrive)
    outside = workspace / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    os.symlink(outside, hidrive.STRM_ROOT / "escape")

    response = client.get("/api/strm/list?path=escape")

    assert response.status_code == 404
    assert response.get_json()["code"] == "STRM_PATH_INVALID"
