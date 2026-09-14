"""GET /api/library/search/re0 -- the federated (local + RE0) search lane.
TMDB goes through the app's own client (fake session), RE0 through the
FakeHTTP fixture; every slug/URL/token is a fixture."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_normalize  # noqa: E402
import library_store as ls  # noqa: E402

RE0_SEARCH = "/api/library/search/re0"
RES_MOVIE_555 = "https://re0.me/api/open/resources/movie/555"
RES_MOVIE_999 = "https://re0.me/api/open/resources/movie/999"
RES_TV_777 = "https://re0.me/api/open/resources/tv/777"
UNLOCK_URL = "https://re0.me/api/open/resources/unlock"


class _TmdbResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status; self._payload = payload
    def json(self):
        return self._payload


class _TmdbSession:
    def __init__(self):
        self.routes = {}; self.calls = []
    def route(self, url, payload, status=200):
        self.routes[url] = _TmdbResponse(status, payload); return self
    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        if url not in self.routes:
            raise AssertionError(f"unscripted TMDB request to {url}")
        return self.routes[url]


def _tmdb_result(tmdb_id, title, *, kind="movie", year="2019", poster="/p.jpg", backdrop="/b.jpg", overview="简介", votes=100, score=7.5):
    row = {"id": tmdb_id, "poster_path": poster, "backdrop_path": backdrop, "overview": overview, "vote_average": score, "vote_count": votes,
           "genre_ids": [], "original_language": "zh"}
    if kind == "movie":
        row.update({"title": title, "original_title": title, "release_date": f"{year}-01-01"})
    else:
        row.update({"name": title, "original_name": title, "first_air_date": f"{year}-01-01"})
    return row


def _re0_item(slug, pan_type, **over):
    item = {"slug": slug, "media_slug": "m", "media_url": f"https://re0.me/r/{slug}", "pan_type": pan_type, "source": ["WEB-DL"],
            "video_resolution": ["4K"], "unlock_points": 5, "is_unlocked": False, "validate_status": "valid"}
    item.update(over)
    return item


@pytest.fixture
def re0_env(hidrive, workspace, http, monkeypatch):
    store = ls.LibraryStore(hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())
    store.create_schema(); store.meta_set("encrypted", "1")
    media_id = store.upsert_media(ls.MediaRecord(media_identity="tmdb:movie:555", media_type="movie", title_zh="本地片", search_key="本地片",
                                                 year=2019, tmdb_id=555, match_status="exact", poster_path="/local.jpg"))
    group_id = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-local", display_title="1080p"))
    url = "https://115.com/s/swfakelocal555"
    info = library_normalize.parse_link(url, None)
    store.upsert_link(ls.LinkRecord(public_id="pub-local-555", group_id=group_id, provider="115", canonical_url_hash=library_normalize.canonical_hash(info.canonical),
                                    url_label=info.label, url_ciphertext=store.fernet.encrypt(url.encode())))
    store.recount()
    import library_search
    library_search.build_index(store)
    hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
    hidrive.secret_set("hdhive_app_secret", "app-secret-for-tests")
    hidrive.save_tokens({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600, "refresh_expires_in": 86400})
    session = _TmdbSession()
    tmdb_base = hidrive.library_tmdb.TMDB_BASE
    session.route(tmdb_base + "/search/movie", {"results": [_tmdb_result(555, "本地片"), _tmdb_result(999, "远端片", year="2024")]})
    session.route(tmdb_base + "/search/tv", {"results": []})
    monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)
    http.route("GET", RES_MOVIE_555, {"success": True, "data": [_re0_item("fixture-slug-a", "115"), _re0_item("fixture-slug-b", "189")]})
    http.route("GET", RES_MOVIE_999, {"success": True, "data": [_re0_item("fixture-slug-c", "quark", unlock_points=0)]})
    http.route("GET", "https://re0.me/api/open/quota", {"success": True, "data": {"endpoint_limit": None, "endpoint_remaining": None}})
    return {"store": store, "media_id": media_id, "session": session, "tmdb_base": tmdb_base}


def _re0_calls(http):
    """Every RE0 call except the once-a-day read-only quota probe."""
    return [c for c in http.calls if "re0.me" in c["url"] and not c["url"].endswith("/api/open/quota")]


class TestFederatedSearch:
    @pytest.mark.parametrize("query,expected", [("page=201&page_size=10", 200),
        ("page=0", 400), ("page_size=51", 400)])
    def test_catalog_keeps_shared_pagination_bounds(self, client, re0_env, query, expected):
        response = client.get(RE0_SEARCH + "?q=远端片&type=all&catalog=1&" + query)
        assert response.status_code == expected
        if expected == 200:
            catalog = response.get_json()["catalog"]
            assert catalog["page"] == 201 and catalog["page_size"] == 10
            assert catalog["items"] == []

    def test_requires_q_and_validates_params(self, client, re0_env):
        assert client.get(RE0_SEARCH).status_code == 400
        assert client.get(RE0_SEARCH + "?q=" + "x" * 121).status_code == 400
        assert client.get(RE0_SEARCH + "?q=a&type=anime").status_code == 400
        assert client.get(RE0_SEARCH + "?q=a&provider=dropbox").status_code == 400
        assert client.get(RE0_SEARCH + "?q=a&year=20x").status_code == 400

    def test_tmdb_mode_projects_candidates_and_resources(self, client, re0_env, http, hidrive):
        response = client.get(RE0_SEARCH + "?q=远端片&type=all")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        remote = body["remote"]
        assert remote["mode"] == "tmdb" and remote["status"] == "fresh" and remote["origin"] == "tmdb" and remote["cached_at"]
        by_id = {i["tmdb_id"]: i for i in remote["items"]}
        assert set(by_id) == {555, 999}
        local = by_id[555]
        assert local["local_media_id"] == re0_env["media_id"] and local["media_ref"] == "re0:movie:555"
        assert local["candidate_count"] == 2 and local["providers"] == ["115", "tianyicloud"] and local["state"] == "candidate"
        assert local["poster_url"].endswith("/p.jpg") and local["backdrop_url"].endswith("/b.jpg") and local["overview_short"] == "简介"
        assert local["ratings"]["tmdb"]["score"] == 7.5 and local["metadata_status"] in ("pending", "partial")
        remote_only = by_id[999]
        assert remote_only["local_media_id"] and remote_only["media_ref"] == "re0:movie:999" and remote_only["providers"] == ["quark"]
        assert remote_only["title"] == "远端片" and remote_only["year"] == 2024
        assert [c["url"] for c in _re0_calls(http)] == [RES_MOVIE_555, RES_MOVIE_999]
        assert all(c["method"] == "GET" for c in _re0_calls(http)) and not [c for c in http.calls if c["url"] == UNLOCK_URL]
        assert {c["url"] for c in re0_env["session"].calls} == {re0_env["tmdb_base"] + "/search/movie", re0_env["tmdb_base"] + "/search/tv"}
        raw = response.get_data(as_text=True)
        for needle in ("fixture-slug", "re0.me/r/", "access-1", "app-secret", "media_url"):
            assert needle not in raw, needle
        conn = re0_env["store"].connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_resource").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM re0_media_projection").fetchone()[0] == 2
        assert conn.execute("SELECT local_media_id FROM re0_media_projection WHERE tmdb_id=555").fetchone()[0] == re0_env["media_id"]
        assert conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0] == 1  # candidates never become links
        conn.close()

    def test_second_query_within_ttl_uses_cache(self, client, re0_env, http):
        client.get(RE0_SEARCH + "?q=远端片&type=all")
        tmdb_calls, re0_calls = len(re0_env["session"].calls), len(_re0_calls(http))
        body = client.get(RE0_SEARCH + "?q=远端片&type=all").get_json()
        assert body["remote"]["status"] == "cached" and body["remote"]["origin"] == "tmdb" and {i["tmdb_id"] for i in body["remote"]["items"]} == {555, 999}
        assert len(re0_env["session"].calls) == tmdb_calls and len(_re0_calls(http)) == re0_calls

    def test_provider_filter_is_server_side(self, client, re0_env):
        body = client.get(RE0_SEARCH + "?q=本地片&type=all&provider=115").get_json()
        items = body["remote"]["items"]
        assert [i["tmdb_id"] for i in items] == [555]
        assert items[0]["providers"] == ["115"] and items[0]["candidate_count"] == 1
        assert "tianyicloud" not in response_text(body) and "quark" not in response_text(body)

    def test_no_tmdb_candidates_skips_re0(self, client, re0_env, http):
        re0_env["session"].route(re0_env["tmdb_base"] + "/search/movie", {"results": []})
        body = client.get(RE0_SEARCH + "?q=不存在的片&type=all").get_json()
        assert body["remote"]["status"] == "no_candidates" and body["remote"]["items"] == []
        assert _re0_calls(http) == []
        conn = re0_env["store"].connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_resource").fetchone()[0] == 0
        conn.close()

    def test_rate_limited_sets_cooldown_and_local_search_still_works(self, client, re0_env, http):
        from tests.conftest import FakeResponse
        http.route("GET", RES_MOVIE_555, handler=lambda **kw: FakeResponse({"success": False}, 429, headers={"Retry-After": "90"}))
        body = client.get(RE0_SEARCH + "?q=本地片&type=all").get_json()
        assert body["remote"]["status"] == "rate_limited" and body["remote"]["retry_after"] == 90
        assert len(_re0_calls(http)) == 1
        again = client.get(RE0_SEARCH + "?q=本地片&type=all").get_json()
        assert again["remote"]["status"] == "rate_limited" and len(_re0_calls(http)) == 1
        local = client.get("/api/library/search?q=本地片").get_json()
        assert local["total"] == 1 and local["items"][0]["media_id"] == re0_env["media_id"]

    def test_reauth_required_and_scope_denied(self, client, re0_env, http, hidrive, monkeypatch):
        http.route("GET", RES_MOVIE_555, {"success": False, "code": "OPENAPI_REFRESH_REQUIRED"}, 401)
        monkeypatch.setattr(hidrive, "refresh_hdhive_token", lambda *a, **k: None)
        body = client.get(RE0_SEARCH + "?q=本地片&type=all").get_json()
        assert body["remote"]["status"] == "reauth_required" and len(_re0_calls(http)) == 1
        http.route("GET", RES_MOVIE_555, {"success": False, "code": "SCOPE_NOT_ALLOWED"}, 403)
        hidrive._re0_client_reset()
        body = client.get(RE0_SEARCH + "?q=本地片&type=tv").get_json()  # different cache key
        assert body["remote"]["status"] in ("no_candidates", "scope_denied")

    def test_candidate_selection_caps_at_five_and_prefers_exact_title(self, client, re0_env, http):
        results = [_tmdb_result(9000 + i, f"近似片{i}", votes=1000 - i) for i in range(8)] + [_tmdb_result(555, "本地片", votes=1)]
        re0_env["session"].route(re0_env["tmdb_base"] + "/search/movie", {"results": results})
        for i in range(8):
            http.route("GET", f"https://re0.me/api/open/resources/movie/{9000 + i}", {"success": True, "data": []})
        body = client.get(RE0_SEARCH + "?q=本地片&type=movie").get_json()
        calls = [c["url"] for c in _re0_calls(http)]
        assert len(calls) <= 5 and calls[0] == RES_MOVIE_555

    def test_year_and_type_reach_tmdb_only_for_that_kind(self, client, re0_env, http):
        http.route("GET", RES_TV_777, {"success": True, "data": []})
        re0_env["session"].route(re0_env["tmdb_base"] + "/search/tv", {"results": [_tmdb_result(777, "远端剧", kind="tv", year="2020")]})
        client.get(RE0_SEARCH + "?q=远端剧&type=tv&year=2020")
        calls = re0_env["session"].calls
        assert all(c["url"].endswith("/search/tv") for c in calls) and calls[0]["params"]["first_air_date_year"] == 2020
        assert [c["url"] for c in _re0_calls(http)] == [RES_TV_777]

    def test_unlocked_payload_from_search_is_materialised_insert_only(self, client, re0_env, http):
        http.route("GET", RES_MOVIE_555, {"success": True, "data": [
            _re0_item("fixture-slug-u", "115", is_unlocked=True, url="https://115.com/s/swfakelocal555"),  # same as the local link
            _re0_item("fixture-slug-v", "189", is_unlocked=True, url="https://cloud.189.cn/t/fixtureNew1", access_code="ab12"),
        ]})
        body = client.get(RE0_SEARCH + "?q=本地片&type=movie").get_json()
        item = [i for i in body["remote"]["items"] if i["tmdb_id"] == 555][0]
        assert item["unlocked_count"] == 2 and item["state"] == "unlocked"
        conn = re0_env["store"].connect(readonly=True)
        links = conn.execute("SELECT provider, url_label, group_id FROM resource_link ORDER BY id").fetchall()
        assert len(links) == 2 and links[0]["provider"] == "115" and links[1]["provider"] == "tianyicloud"
        assert conn.execute("SELECT COUNT(*) FROM re0_resource_link WHERE relation='same_local'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM re0_resource_link WHERE relation='materialized_unlocked'").fetchone()[0] == 1
        conn.close()
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]

    def test_direct_mode_shares_the_same_pipeline(self, client, re0_env, http, hidrive):
        hidrive.setting_set("re0_direct_search_path", "/api/open/search")
        http.route("GET", "https://re0.me/api/open/search", {"success": True, "data": {"items": [{"media_type": "movie", "tmdb_id": 999, "title": "远端片", "year": 2024}]}})
        body = client.get(RE0_SEARCH + "?q=远端片&type=movie").get_json()
        assert body["remote"]["mode"] == "direct" and [i["tmdb_id"] for i in body["remote"]["items"]] == [999]
        assert re0_env["session"].calls == []  # TMDB search not used in direct mode
        assert [c["url"] for c in _re0_calls(http)] == ["https://re0.me/api/open/search", RES_MOVIE_999]
        assert http.calls[0]["params"]["q"] == "远端片"


def response_text(body) -> str:
    import json
    return json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Detail page projections + remote-only detail + status
# ---------------------------------------------------------------------------

RE0_STATUS = "/api/library/re0/status"


def _resource_id(store, provider):
    conn = store.connect(readonly=True)
    try:
        return int(conn.execute("SELECT id FROM re0_resource WHERE provider_code=? ORDER BY id", (provider,)).fetchone()[0])
    finally:
        conn.close()


def _local_links_snapshot(store):
    conn = store.connect(readonly=True)
    try:
        return [tuple(r) for r in conn.execute("SELECT id, group_id, provider, canonical_url_hash, url_label, deleted_at_source FROM resource_link ORDER BY id")]
    finally:
        conn.close()


class TestDetailProjection:
    def test_detail_gains_re0_candidates_and_keeps_local_groups_untouched(self, client, re0_env):
        before = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        snapshot = _local_links_snapshot(re0_env["store"])
        client.get(RE0_SEARCH + "?q=本地片&type=all")
        after = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        assert after["groups"] == before["groups"] and after["group_count"] == before["group_count"]
        # Local facets keep their counts; a pan only RE0 knows is added with link_count 0 (round 25).
        before_local_facets = [{k: v for k, v in f.items() if k != "re0_count"} for f in before["provider_facets"] if f["link_count"]]
        assert [{k: v for k, v in f.items() if k != "re0_count"} for f in after["provider_facets"] if f["link_count"]] == before_local_facets
        assert [(f["provider"], f["link_count"], f["re0_count"]) for f in after["provider_facets"]] == [("115", 1, 1), ("tianyicloud", 0, 1)]
        assert _local_links_snapshot(re0_env["store"]) == snapshot
        cands = after["re0_candidates"]
        assert [c["provider"] for c in cands] == ["115", "tianyicloud"]
        c115 = cands[0]
        assert c115["state"] == "candidate" and c115["unlock_points"] == 5 and c115["action"] == "transfer"
        assert c115["specs"]["resolution"]["value"] == "4K" and c115["specs"]["source"]["value"] == "WEB-DL"
        assert c115["provider_label"] == "115 网盘" and c115["source_status"] == "valid" and c115["resource_link_id"] is None
        assert cands[1]["action"] == "copy"
        raw = client.get(f"/api/library/media/{re0_env['media_id']}").get_data(as_text=True)
        for needle in ("fixture-slug", "re0.me", "slug_hash"):
            assert needle not in raw

    def test_detail_provider_filter_isolates_candidates(self, client, re0_env):
        client.get(RE0_SEARCH + "?q=本地片&type=all")
        body = client.get(f"/api/library/media/{re0_env['media_id']}?provider=115").get_json()
        assert [c["provider"] for c in body["re0_candidates"]] == ["115"]
        assert "tianyicloud" not in client.get(f"/api/library/media/{re0_env['media_id']}?provider=115").get_data(as_text=True)
        body = client.get(f"/api/library/media/{re0_env['media_id']}?provider=quark").get_json()
        assert body["re0_candidates"] == []

    def test_detail_facets_merge_re0_candidates_per_pan(self, client, re0_env):
        """Round 25: the detail page organises old and new resources by pan,
        so a pan that only RE0 knows still gets a tab, and each facet says
        how many RE0 candidates it holds."""
        client.get(RE0_SEARCH + "?q=本地片&type=all")  # local exact id -> 115 + tianyicloud candidates for 555
        body = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        assert body["provider_facets"] == [
            {"provider": "115", "label": "115 网盘", "link_count": 1, "re0_count": 1},
            {"provider": "tianyicloud", "label": "天翼云盘", "link_count": 0, "re0_count": 1},
        ]
        assert body["provider_count"] == 2
        scoped = client.get(f"/api/library/media/{re0_env['media_id']}?provider=tianyicloud").get_json()
        assert scoped["provider_facets"] == [{"provider": "tianyicloud", "label": "天翼云盘", "link_count": 0, "re0_count": 1}]
        assert [c["provider"] for c in scoped["re0_candidates"]] == ["tianyicloud"] and scoped["groups"] == []

    def test_remote_only_detail_route(self, client, re0_env):
        client.get(RE0_SEARCH + "?q=远端片&type=all")
        response = client.get("/api/library/re0-media/movie/999")
        assert response.status_code == 200
        body = response.get_json()
        assert body["media_ref"] == "re0:movie:999" and body["media_id"] and body["title"] == "远端片" and body["year"] == 2024
        assert body["groups"] == [] and body["group_count"] == 0 and body["provider_facets"] == [{"provider": "quark", "label": "夸克网盘", "link_count": 0, "re0_count": 1}]
        assert body["poster_url"] and body["backdrop_url"] and body["metadata_status"] in ("pending", "partial")
        assert [c["provider"] for c in body["re0_candidates"]] == ["quark"] and body["re0_candidates"][0]["action"] == "copy"
        assert client.get("/api/library/re0-media/movie/999?provider=115").get_json()["re0_candidates"] == []
        assert client.get("/api/library/re0-media/movie/424242").status_code == 404
        assert client.get("/api/library/re0-media/anime/1").status_code == 400
        assert client.get("/api/library/re0-media/movie/555").get_json()["media_id"] == re0_env["media_id"]

    def test_status_endpoint_is_summary_only(self, client, re0_env, hidrive):
        client.get(RE0_SEARCH + "?q=远端片&type=all")
        body = client.get(RE0_STATUS).get_json()
        assert body["success"] is True and body["configured"] is True and body["authorized"] is True
        assert body["budget"]["used_today"] == 2 and body["budget"]["daily_cap"] == 100
        assert body["projections"] == {"pending": 0, "partial": 2, "complete": 0, "retryable": 0, "failed": 0}
        assert body["resources"]["candidate"] == 3 and body["direct_search"] is False
        raw = client.get(RE0_STATUS).get_data(as_text=True)
        for needle in ("access-1", "app-secret", "fixture-slug", "refresh-1"):
            assert needle not in raw


# ---------------------------------------------------------------------------
# User-triggered unlock-and-action
# ---------------------------------------------------------------------------


def _unlock_payload(url, access_code=None, already_owned=False, points=5):
    data = {"url": url, "full_url": url, "already_owned": already_owned, "points_cost": points}
    if access_code:
        data["access_code"] = access_code
    return {"success": True, "data": data}


class TestUnlockAndAction:
    def test_115_candidate_unlocks_materialises_and_returns_transfer_next_action(self, client, re0_env, http, hidrive, audit_rows):
        client.get(RE0_SEARCH + "?q=本地片&type=all")
        rid = _resource_id(re0_env["store"], "115")
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        response = client.post(f"/api/library/re0-resource/{rid}/unlock-and-action", json={"action": "transfer", "request_id": "req-0001"})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["success"] is True and body["next_action"] == "transfer" and body["provider"] == "115"
        assert body["link_public_id"].startswith("re0-") and body["media_id"] == re0_env["media_id"] and body["already_owned"] is False
        assert body["replayed"] is False and body["unlock_points"] == 5
        unlock_calls = [c for c in http.calls if c["url"] == UNLOCK_URL]
        assert len(unlock_calls) == 1 and unlock_calls[0]["json"] == {"slug": "fixture-slug-a"}
        raw = response.get_data(as_text=True)
        for needle in ("swfakeunlocked777", "cd34", "fixture-slug"):
            assert needle not in raw
        conn = re0_env["store"].connect(readonly=True)
        link = conn.execute("SELECT * FROM resource_link WHERE public_id=?", (body["link_public_id"],)).fetchone()
        assert link["provider"] == "115" and link["url_plain"] is None and link["has_access_code"] == 1
        assert re0_env["store"].fernet.decrypt(link["url_ciphertext"]).decode() == "https://115.com/s/swfakeunlocked777"
        res = conn.execute("SELECT state, unlocked_at FROM re0_resource WHERE id=?", (rid,)).fetchone()
        assert res["state"] == "unlocked" and res["unlocked_at"]
        conn.close()
        audit = audit_rows("re0.unlock")
        assert audit and audit[-1]["status"] == "success" and "swfake" not in audit[-1]["detail"] and "fixture-slug" not in audit[-1]["detail"]
        # replay: same request_id -> no second unlock, same link
        replay = client.post(f"/api/library/re0-resource/{rid}/unlock-and-action", json={"action": "transfer", "request_id": "req-0001"}).get_json()
        assert replay["replayed"] is True and replay["link_public_id"] == body["link_public_id"]
        # a new request on an already-materialised resource also never unlocks again
        again = client.post(f"/api/library/re0-resource/{rid}/unlock-and-action", json={"action": "transfer", "request_id": "req-0002"}).get_json()
        assert again["link_public_id"] == body["link_public_id"] and len([c for c in http.calls if c["url"] == UNLOCK_URL]) == 1
        detail = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        cand = [c for c in detail["re0_candidates"] if c["provider"] == "115"][0]
        assert cand["state"] == "unlocked" and cand["resource_link_id"] == body["link_public_id"]
        assert any(l["link_id"] == body["link_public_id"] for g in detail["groups"] for l in g["links"])

    def test_action_must_match_provider(self, client, re0_env, http):
        client.get(RE0_SEARCH + "?q=本地片&type=all")
        rid = _resource_id(re0_env["store"], "tianyicloud")
        response = client.post(f"/api/library/re0-resource/{rid}/unlock-and-action", json={"action": "transfer", "request_id": "req-0003"})
        assert response.status_code == 400 and response.get_json()["code"] == "RE0_ACTION_NOT_ALLOWED"
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]
        assert client.post(f"/api/library/re0-resource/{rid}/unlock-and-action", json={"action": "copy"}).status_code == 400
        assert client.post(f"/api/library/re0-resource/424242/unlock-and-action", json={"action": "copy", "request_id": "req-0004"}).status_code == 404

    def test_rate_limited_unlock_leaves_candidate_untouched(self, client, re0_env, http):
        from tests.conftest import FakeResponse
        client.get(RE0_SEARCH + "?q=本地片&type=all")
        rid = _resource_id(re0_env["store"], "115")
        http.route("POST", UNLOCK_URL, handler=lambda **kw: FakeResponse({"success": False}, 429, headers={"Retry-After": "30"}))
        response = client.post(f"/api/library/re0-resource/{rid}/unlock-and-action", json={"action": "transfer", "request_id": "req-0005"})
        assert response.status_code == 429 and response.get_json()["code"] == "RE0_RATE_LIMITED" and response.get_json()["retry_after"] == 30
        conn = re0_env["store"].connect(readonly=True)
        assert conn.execute("SELECT state FROM re0_resource WHERE id=?", (rid,)).fetchone()[0] == "candidate"
        assert conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM re0_action WHERE request_id='req-0005'").fetchone()[0] == "failed"
        conn.close()

    def test_remote_only_media_is_created_from_projection_on_unlock(self, client, re0_env, http, audit_rows):
        client.get(RE0_SEARCH + "?q=远端片&type=all")
        rid = _resource_id(re0_env["store"], "quark")
        http.route("POST", UNLOCK_URL, _unlock_payload("https://pan.quark.cn/s/swfakequark999", already_owned=True, points=0))
        body = client.post(f"/api/library/re0-resource/{rid}/unlock-and-action", json={"action": "copy", "request_id": "req-0006"}).get_json()
        assert body["success"] is True and body["next_action"] == "copy" and body["already_owned"] is True and body["media_id"]
        conn = re0_env["store"].connect(readonly=True)
        media = conn.execute("SELECT * FROM media WHERE tmdb_id=999").fetchone()
        assert media["match_status"] == "exact" and media["title_zh"] == "远端片" and media["year"] == 2024 and media["poster_path"] == "/p.jpg"
        assert conn.execute("SELECT local_media_id FROM re0_media_projection WHERE tmdb_id=999").fetchone()[0] == media["id"]
        link = conn.execute("SELECT provider, group_id FROM resource_link WHERE public_id=?", (body["link_public_id"],)).fetchone()
        assert link["provider"] == "quark"
        assert conn.execute("SELECT media_id FROM resource_group WHERE id=?", (link["group_id"],)).fetchone()[0] == media["id"]
        conn.close()
        # Discovery already created the metadata identity; unlocking reuses it.
        assert audit_rows("re0.media.create") == []
        detail = client.get(f"/api/library/media/{media['id']}").get_json()
        assert detail["title"] == "远端片" and detail["group_count"] == 1
        reveal = client.post(f"/api/library/link/{body['link_public_id']}/reveal").get_json()
        assert reveal["url"] == "https://pan.quark.cn/s/swfakequark999"

    def test_ed2k_candidate_maps_to_cloud_action(self, client, re0_env, http):
        http.route("GET", RES_MOVIE_555, {"success": True, "data": [_re0_item("fixture-slug-e", "ed2k")]})
        client.get(RE0_SEARCH + "?q=本地片&type=movie")
        rid = _resource_id(re0_env["store"], "ed2k")
        detail = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        assert [c["action"] for c in detail["re0_candidates"]] == ["cloud"]
        http.route("POST", UNLOCK_URL, _unlock_payload("ed2k://|file|Fixture.mkv|123|" + "AB" * 16 + "|/"))
        body = client.post(f"/api/library/re0-resource/{rid}/unlock-and-action", json={"action": "cloud", "request_id": "req-0007"}).get_json()
        assert body["next_action"] == "cloud" and body["provider"] == "ed2k"


# ---------------------------------------------------------------------------
# §11 CLI (app.py --re0-sync), run-small endpoint, status with run records
# ---------------------------------------------------------------------------

RE0_RUN_SMALL = "/api/library/re0/run-small"


class TestCliAndRunSmall:
    def test_cli_status_phase_is_read_only(self, re0_env, hidrive, http):
        report = hidrive.re0_sync_cli(["--phase", "status", "--json"])
        assert report["ok"] is True and report["phase"] == "status"
        assert report["budget"]["daily_cap"] == 100 and report["sync"]["cursor"] == 0 and report["sync"]["queue_remaining"] == 1
        assert _re0_calls(http) == []

    def test_cli_refresh_existing_runs_bounded_and_records(self, re0_env, hidrive, http):
        report = hidrive.re0_sync_cli(["--phase", "refresh-existing", "--limit", "5", "--max-requests", "1", "--json"])
        assert report["ok"] is True and report["requested"] == 1 and report["succeeded"] == 1 and report["unlocked"] == 0
        assert [c["url"] for c in _re0_calls(http)] == [RES_MOVIE_555]
        assert all(c["method"] == "GET" for c in _re0_calls(http))
        status = hidrive.re0_sync_cli(["--phase", "status", "--json"])
        assert status["sync"]["last_run"]["phase"] == "refresh-existing" and status["sync"]["last_run"]["succeeded"] == 1

    def test_cli_file_list_previews_a_candidate_without_unlocking(self, re0_env, hidrive, http):
        """Round 26: a read-only look inside a share, so 合集包 vs 单集 is
        answerable before anyone spends points."""
        client_ = re0_env["store"]
        hidrive.app.test_client().get(RE0_SEARCH + "?q=本地片&type=movie")
        rid = _resource_id(client_, "115")
        http.route("GET", f"https://re0.me/api/open/resources/file-list/fixture-slug-a", {"success": True, "data": {
            "provider": "115", "list_type": "folder", "share_title": "本地片 全4集", "file_count": 4, "result_type": "listing",
            "resource_validate_status": "valid",
            "files": [{"name": f"E0{i}.mkv", "path": f"/本地片/E0{i}.mkv", "size": 1073741824, "extension": "mkv"} for i in range(1, 5)],
        }})
        report = hidrive.re0_sync_cli(["--phase", "file-list", "--resource-ids", str(rid), "--json"])
        assert report["ok"] is True and report["phase"] == "file-list" and report["requested"] == 1
        preview = report["previews"][0]
        assert preview["ok"] is True and preview["share_title"] == "本地片 全4集" and preview["file_count"] == 4
        assert [f["name"] for f in preview["files"]] == ["E01.mkv", "E02.mkv", "E03.mkv", "E04.mkv"]
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]
        blob = __import__("json").dumps(report, ensure_ascii=False)
        for needle in ("fixture-slug", "re0.me/r/", "access-1", "app-secret"):
            assert needle not in blob, needle
        empty = hidrive.re0_sync_cli(["--phase", "file-list", "--json"])
        assert empty["ok"] is False and empty["error"] == "RE0_FILE_LIST_NEEDS_RESOURCE_IDS"

    def test_cli_probe_resources_compares_upstream_against_what_is_stored(self, re0_env, hidrive, http):
        """Round 27: the diagnostic Codex runs on the host -- it shows every
        upstream item's raw pan_type next to the rows we kept, and writes
        nothing."""
        report = hidrive.re0_sync_cli(["--phase", "probe-resources", "--media-ids", str(re0_env["media_id"]), "--json"])
        assert report["ok"] is True and report["phase"] == "probe-resources" and len(report["probes"]) == 1
        probe = report["probes"][0]
        assert probe["ok"] is True and probe["media_type"] == "movie" and probe["tmdb_id"] == 555
        assert probe["raw_items"] == 2 and probe["invalid_items"] == 0 and probe["data_shape"] == "list"
        assert [i["provider_code"] for i in probe["items"]] == ["115", "tianyicloud"]
        assert all(i["already_stored"] is False for i in probe["items"]) and probe["stored"] == []
        assert [c["url"] for c in _re0_calls(http)] == [RES_MOVIE_555]
        conn = re0_env["store"].connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_resource").fetchone()[0] == 0  # a probe never stores
        conn.close()
        blob = __import__("json").dumps(report, ensure_ascii=False)
        for needle in ("fixture-slug", "re0.me/r/", "access-1", "app-secret"):
            assert needle not in blob, needle
        by_tmdb = hidrive.re0_sync_cli(["--phase", "probe-resources", "--tmdb-ids", "555", "--media-type", "movie", "--json"])
        assert by_tmdb["ok"] is True and by_tmdb["probes"][0]["tmdb_id"] == 555
        empty = hidrive.re0_sync_cli(["--phase", "probe-resources", "--json"])
        assert empty["ok"] is False and empty["error"] == "RE0_PROBE_NEEDS_IDS"

    def test_cli_dry_run_and_no_network_do_not_touch_re0(self, re0_env, hidrive, http):
        for flag in ("--dry-run", "--no-network"):
            report = hidrive.re0_sync_cli(["--phase", "refresh-existing", "--limit", "5", flag, "--json"])
            assert report["ok"] is True and report["dry_run"] is True and report["would_request"] == 1
        assert _re0_calls(http) == []
        recon = hidrive.re0_sync_cli(["--phase", "reconcile", "--dry-run", "--limit", "100", "--json"])
        assert recon["ok"] is True and recon["phase"] == "reconcile" and recon["known_tmdb_ids"] == 0

    def test_cli_rejects_unknown_phase_and_never_offers_unlock(self, re0_env, hidrive):
        bad = hidrive.re0_sync_cli(["--phase", "unlock-all", "--json"])
        assert bad["ok"] is False and bad["error"] == "RE0_SYNC_PHASE_INVALID"
        import inspect
        source = inspect.getsource(hidrive.re0_sync_cli)
        assert "unlock" not in source.replace("unlocked", "")

    def test_cli_lock_prevents_overlap(self, re0_env, hidrive, http):
        import fcntl
        lock_path = hidrive.DATA_DIR / "re0-sync.run.lock"
        with lock_path.open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            report = hidrive.re0_sync_cli(["--phase", "refresh-existing", "--limit", "1", "--json"])
        assert report["ok"] is False and report["error"] == "RE0_SYNC_BUSY" and _re0_calls(http) == []

    def test_run_small_endpoint_probes_one_media_only(self, client, re0_env, http):
        response = client.post(RE0_RUN_SMALL, json={})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["success"] is True and body["report"]["requested"] == 1 and body["report"]["unlocked"] == 0
        assert [c["url"] for c in _re0_calls(http)] == [RES_MOVIE_555]
        status = client.get(RE0_STATUS).get_json()
        assert status["sync"]["last_run"]["phase"] == "refresh-existing"

    def test_deploy_units_use_the_cli(self):
        if not (ROOT / "deploy" / "hidrive-lite-re0-sync.service").exists():
            pytest.skip("Private host service units are excluded from the public distribution")
        service = (ROOT / "deploy" / "hidrive-lite-re0-sync.service").read_text(encoding="utf-8")
        timer = (ROOT / "deploy" / "hidrive-lite-re0-sync.timer").read_text(encoding="utf-8")
        assert "app.py --re0-sync --phase refresh-existing" in service and "--limit" in service and "--resume" in service
        assert "unlock" not in service
        assert "Unit=hidrive-lite-re0-sync.service" in timer and "OnCalendar=" in timer


# ---------------------------------------------------------------------------
# tmdb:<id> shortcut, explicit per-media refresh, server quota, settings
# ---------------------------------------------------------------------------

QUOTA_URL_RE0 = "https://re0.me/api/open/quota"
RE0_REFRESH = "/api/library/re0/refresh"


class TestSearchExtras:
    def test_tmdb_id_shortcut_skips_tmdb_search(self, client, re0_env, http):
        body = client.get(RE0_SEARCH + "?q=tmdb:999&type=movie").get_json()
        assert body["remote"]["status"] == "fresh" and [i["tmdb_id"] for i in body["remote"]["items"]] == [999]
        assert re0_env["session"].calls == [] and [c["url"] for c in _re0_calls(http)] == [RES_MOVIE_999]
        assert body["remote"]["items"][0]["title"] == "TMDB 999"

    def test_explicit_refresh_bypasses_ttl_but_is_throttled_per_media(self, client, re0_env, http):
        client.get(RE0_SEARCH + "?q=远端片&type=movie")
        assert len(_re0_calls(http)) == 2
        response = client.post(RE0_REFRESH, json={"media_type": "movie", "tmdb_id": 555})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["success"] is True and body["report"]["remote_items"] == 2 and len(_re0_calls(http)) == 3
        again = client.post(RE0_REFRESH, json={"media_type": "movie", "tmdb_id": 555})
        assert again.status_code == 429 and again.get_json()["code"] == "RE0_REFRESH_TOO_SOON" and len(_re0_calls(http)) == 3
        assert client.post(RE0_REFRESH, json={"media_type": "anime", "tmdb_id": 1}).status_code == 400
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]

    def test_refresh_if_stale_fetches_once_then_skips_within_ttl(self, client, re0_env, http):
        """Opening a detail page repairs a missing projection, then the
        explicit stale refresh endpoint respects the freshly written TTL."""
        media_id = re0_env["media_id"]
        initial = client.get(f"/api/library/media/{media_id}").get_json()
        assert len(initial["re0_candidates"]) == 2
        assert [c["url"] for c in _re0_calls(http)] == [RES_MOVIE_555]
        response = client.post(RE0_REFRESH, json={"media_type": "movie", "tmdb_id": 555, "if_stale": True})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body == {"success": True, "fetched": False, "skipped": "fresh"}
        assert len(client.get(f"/api/library/media/{media_id}").get_json()["re0_candidates"]) == 2
        again = client.post(RE0_REFRESH, json={"media_type": "movie", "tmdb_id": 555, "if_stale": True})
        assert again.status_code == 200 and again.get_json() == {"success": True, "fetched": False, "skipped": "fresh"}
        assert len(_re0_calls(http)) == 1
        # A remote-only projection (no local media) is refreshed the same way.
        assert client.post(RE0_REFRESH, json={"media_type": "movie", "tmdb_id": 999, "if_stale": True}).get_json()["fetched"] is True
        assert [c["url"] for c in _re0_calls(http)] == [RES_MOVIE_555, RES_MOVIE_999]
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]

    def test_detail_empty_result_is_negative_cached(self, client, re0_env, http):
        """A completed empty RE0 response is retried after a short backoff,
        not on every subsequent detail render."""
        http.route("GET", RES_MOVIE_555, {"success": True, "data": []})
        first = client.get(f"/api/library/media/{re0_env['media_id']}")
        assert first.status_code == 200 and first.get_json()["re0_candidates"] == []
        second = client.get(f"/api/library/media/{re0_env['media_id']}")
        assert second.status_code == 200 and second.get_json()["re0_candidates"] == []
        assert [c["url"] for c in _re0_calls(http)] == [RES_MOVIE_555]

    def test_website_unlock_refreshes_after_five_minutes_without_purchase(self, client, re0_env, http, hidrive, monkeypatch):
        now = hidrive.utc_now()
        monkeypatch.setattr(hidrive, "utc_now", lambda: now)
        body = {"media_type": "movie", "tmdb_id": 555, "if_stale": True}
        assert client.post(RE0_REFRESH, json=body).get_json()["fetched"] is True
        http.route("GET", RES_MOVIE_555, {"success": True, "data": [
            _re0_item("fixture-slug-a", "115", is_unlocked=True),
            _re0_item("fixture-slug-b", "189", is_unlocked=False),
        ]})
        monkeypatch.setattr(hidrive, "utc_now", lambda: now + 299)
        assert client.post(RE0_REFRESH, json=body).get_json()["skipped"] == "fresh"
        monkeypatch.setattr(hidrive, "utc_now", lambda: now + 301)
        assert client.post(RE0_REFRESH, json=body).get_json()["fetched"] is True
        rows = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()["re0_candidates"]
        owned = next(r for r in rows if r["provider"] == "115")
        other = next(r for r in rows if r["provider"] != "115")
        assert owned["state"] == "already_unlocked" and owned["is_unlocked_upstream"] is True
        assert owned["last_error_class"] == "already_unlocked_no_payload"
        assert other["state"] == "candidate" and not other["is_unlocked_upstream"]
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]

    @pytest.mark.parametrize("state,should_fetch", [("already_unlocked", True), ("unlocked", False)])
    def test_ownership_ttl_distinguishes_pending_payload_from_settled(self, client, re0_env, hidrive, monkeypatch, state, should_fetch):
        now = hidrive.utc_now()
        monkeypatch.setattr(hidrive, "utc_now", lambda: now)
        body = {"media_type": "movie", "tmdb_id": 555, "if_stale": True}
        assert client.post(RE0_REFRESH, json=body).get_json()["fetched"] is True
        conn = re0_env["store"].connect()
        try:
            conn.execute("UPDATE re0_resource SET state=? WHERE tmdb_id=555", (state,))
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(hidrive, "utc_now", lambda: now + 301)
        assert client.post(RE0_REFRESH, json=body).get_json()["fetched"] is should_fetch
        if not should_fetch:
            conn = re0_env["store"].connect()
            try:
                conn.execute("UPDATE re0_media_projection SET last_fetched_at=? WHERE tmdb_id=555", (now - 86401,))
                conn.commit()
            finally:
                conn.close()
            assert client.post(RE0_REFRESH, json=body).get_json()["fetched"] is True

    def test_candidate_rows_carry_resource_title_and_size(self, client, re0_env, http):
        http.route("GET", RES_MOVIE_555, {"success": True, "data": [
            _re0_item("fixture-slug-t1", "115", title="本地片.2019.2160p.WEB-DL.H265", share_size="12.3 GB"),
            _re0_item("fixture-slug-t2", "115", title="本地片 1080p BluRay", share_size=4500000000, video_resolution=["1080p"], source=["BluRay"]),
            _re0_item("fixture-slug-t3", "quark"),
        ]})
        client.get(RE0_SEARCH + "?q=本地片&type=movie")
        rows = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()["re0_candidates"]
        by_title = {r["title"]: r for r in rows}
        assert set(by_title) == {"本地片.2019.2160p.WEB-DL.H265", "本地片 1080p BluRay", None}
        assert by_title["本地片.2019.2160p.WEB-DL.H265"]["size"] == "12.3 GB" and by_title["本地片.2019.2160p.WEB-DL.H265"]["specs"]["source"]["value"] == "WEB-DL"
        assert by_title["本地片 1080p BluRay"]["size"] == 4500000000 and by_title["本地片 1080p BluRay"]["specs"]["resolution"]["value"] == "1080p"
        assert [r["provider"] for r in rows] == ["115", "115", "quark"]  # grouped order: provider, then id
        raw = client.get(f"/api/library/media/{re0_env['media_id']}").get_data(as_text=True)
        assert "fixture-slug" not in raw and "re0.me/r/" not in raw

    def test_server_quota_lowers_the_effective_cap_once_per_day(self, re0_env, hidrive, http):
        http.route("GET", QUOTA_URL_RE0, {"success": True, "data": {"endpoint_limit": 20, "endpoint_remaining": 6}})
        status = hidrive.re0_sync_cli(["--phase", "status", "--json"])
        assert status["budget"]["effective_cap"] == 3 and status["budget"]["server_remaining"] == 6
        hidrive.re0_sync_cli(["--phase", "status", "--json"])
        assert len([c for c in http.calls if c["url"] == QUOTA_URL_RE0]) == 1
        http.route("GET", QUOTA_URL_RE0, {"success": True, "data": {"endpoint_limit": None, "endpoint_remaining": None}})
        hidrive._RE0_CLIENTS.clear()
        conn = re0_env["store"].connect(); conn.execute("DELETE FROM re0_sync_state WHERE key LIKE 'quota_checked:%'"); conn.commit(); conn.close()
        status = hidrive.re0_sync_cli(["--phase", "status", "--json"])
        assert status["budget"]["effective_cap"] == 100 and status["budget"]["server_remaining"] is None

    def test_settings_edit_cap_and_interval(self, client, re0_env, hidrive):
        assert client.post("/api/settings", json={"re0_daily_request_cap": 50, "re0_min_interval_ms": 2000}).status_code == 200
        status = client.get(RE0_STATUS).get_json()
        assert status["daily_cap"] == 50 and status["min_interval_ms"] == 2000 and status["budget"]["daily_cap"] == 50
        for payload in ({"re0_daily_request_cap": 0}, {"re0_daily_request_cap": 5000}, {"re0_min_interval_ms": 100}, {"re0_min_interval_ms": "x"}):
            assert client.post("/api/settings", json=payload).status_code == 400


class TestCalendarInDetail:
    def test_detail_carries_next_episode_cta_for_tv_with_tmdb_id(self, client, re0_env, hidrive, monkeypatch):
        store = re0_env["store"]
        tv_id = store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv", title_zh="本地剧", search_key="本地剧", tmdb_id=70, match_status="exact"))
        group = store.upsert_group(ls.GroupRecord(media_id=tv_id, edition_fingerprint="fp-s5", display_title="S05", season_from=5, season_to=5))
        url = "https://pan.quark.cn/s/swfaketv70"
        info = library_normalize.parse_link(url, None)
        store.upsert_link(ls.LinkRecord(public_id="pub-tv-70", group_id=group, provider="quark", canonical_url_hash=library_normalize.canonical_hash(info.canonical), url_label=info.label, url_ciphertext=store.fernet.encrypt(url.encode())))
        store.recount()
        import re0_sync
        re0_sync.record_calendar(store, [{"first_aired": "2026-09-16T03:00:00Z", "show": {"title": "本地剧", "ids": {"tmdb": 70}}, "episode": {"season": 6, "number": 1, "title": "新季首集"}}], now=1_757_000_000)
        monkeypatch.setattr(hidrive, "utc_now", lambda: 1_757_900_000)
        body = client.get(f"/api/library/media/{tv_id}").get_json()
        cta = body["re0_calendar"]
        assert cta["label"] == "新一季 · S06" and cta["display"] == "9月16日周三 11:00" and cta["season"] == 6 and cta["episode"] == 1
        assert cta["episode_title"] == "新季首集" and cta["is_new_season"] is True
        movie = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        assert movie["re0_calendar"] is None


class TestDiscoveryCli:
    def test_cli_discover_phases(self, re0_env, hidrive, http):
        http.route("GET", "https://re0.me/api/open/calendar", {"success": True, "data": {"items": [{"first_aired": "2026-09-18T12:00:00Z", "show": {"title": "日历剧", "ids": {"tmdb": 71}}, "episode": {"season": 1, "number": 1}}], "days": 31}})
        http.route("GET", "https://re0.me/api/open/streaming-top", {"success": True, "data": {"items": [{"rank": 1, "source_title": "Top", "tmdb_id": 92, "media_type": "tv", "title": "榜单剧", "year": 2026}]}})
        http.route("GET", "https://re0.me/api/open/resources/tv/71", {"success": True, "data": []})
        http.route("GET", "https://re0.me/api/open/resources/tv/92", {"success": True, "data": [_re0_item("fixture-slug-t", "115")]})
        hidrive.setting_set("re0_streaming_top_sources", "netflix:US:tv")
        dry = hidrive.re0_sync_cli(["--phase", "discover-calendar", "--dry-run", "--json"])
        assert dry["ok"] is True and dry["dry_run"] is True and _re0_calls(http) == []
        cal = hidrive.re0_sync_cli(["--phase", "discover-calendar", "--json"])
        assert cal["ok"] is True and cal["recorded"] == 1
        top = hidrive.re0_sync_cli(["--phase", "discover-top", "--json"])
        assert top["ok"] is True and top["projected"] == 1
        bounded = hidrive.re0_sync_cli(["--phase", "discover-bounded", "--limit", "10", "--json"])
        assert bounded["ok"] is True and bounded["requested"] == 2 and bounded["candidates_new"] == 1
        assert all(c["method"] == "GET" for c in _re0_calls(http)) and not [c for c in http.calls if c["url"] == UNLOCK_URL]
        status = hidrive.re0_sync_cli(["--phase", "status", "--json"])
        assert status["calendar"]["events"] == 1 and status["calendar"]["last_fetched_at"]


class TestTvFollowApi:
    def _tv(self, re0_env):
        store = re0_env["store"]
        tv_id = store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv", title_zh="本地剧", search_key="本地剧", tmdb_id=70, match_status="exact"))
        return tv_id

    def test_detail_lists_packs_and_unlock_requires_click(self, client, re0_env, http, audit_rows):
        tv_id = self._tv(re0_env)
        pack = {"slug": "pack-a", "title": "追更包甲", "tv_id": "70", "tmdb_id": 70, "is_unlocked": False, "is_owner": False, "is_completed": False,
                "unlock_points": 30, "preview_items": [{"episode_label": "S02E10", "season": 2, "episode_start": 10, "episode_end": 10}], "latest_label": "S02E10", "item_count": 1}
        http.route("GET", "https://re0.me/api/open/tv-follow/packs", {"success": True, "data": {"items": [pack]}})
        assert client.post("/api/library/re0-follow/query", json={"media_id": tv_id}).status_code == 200
        body = client.get(f"/api/library/media/{tv_id}").get_json()
        follow = body["re0_follow"]
        assert len(follow) == 1 and follow[0]["title"] == "追更包甲" and follow[0]["is_unlocked"] is False and follow[0]["latest_label"] == "S02E10"
        raw = client.get(f"/api/library/media/{tv_id}").get_data(as_text=True)
        assert "pack-a" not in raw
        assert not [c for c in http.calls if "unlock" in c["url"]]
        http.route("POST", "https://re0.me/api/open/tv-follow/packs/pack-a/unlock", {"success": True, "data": {"already_owned": False, "points_cost": 30}})
        http.route("GET", "https://re0.me/api/open/tv-follow/packs/pack-a/items", {"success": True, "data": {"items": [
            {"id": 501, "episode_label": "S02E10", "season": 2, "episode_start": 10, "episode_end": 10, "url": "https://pan.quark.cn/s/swfakepack10"}]}})
        response = client.post(f"/api/library/re0-follow/{follow[0]['ref']}/unlock", json={"request_id": "req-follow-01", "subscribe_updates": True})
        assert response.status_code == 200, response.get_json()
        result = response.get_json()
        assert result["success"] is True and result["materialized"] == 1 and result["subscribe_updates"] is False  # no subscription scope -> forced false
        unlock_calls = [c for c in http.calls if c["url"].endswith("/pack-a/unlock")]
        assert len(unlock_calls) == 1 and unlock_calls[0]["json"] == {"subscribe_updates": False}
        replay = client.post(f"/api/library/re0-follow/{follow[0]['ref']}/unlock", json={"request_id": "req-follow-01"}).get_json()
        assert replay["replayed"] is True and len([c for c in http.calls if c["url"].endswith("/pack-a/unlock")]) == 1
        after = client.get(f"/api/library/media/{tv_id}").get_json()
        assert after["re0_follow"][0]["is_unlocked"] is True and after["group_count"] == 1
        assert audit_rows("re0.follow.unlock")[-1]["status"] == "success"

    def test_cli_tv_follow_phase_is_bounded(self, re0_env, hidrive, http):
        self._tv(re0_env)
        http.route("GET", "https://re0.me/api/open/tv-follow/my", {"success": True, "data": {"items": [], "unread_count": 0}})
        http.route("GET", "https://re0.me/api/open/tv-follow/packs", {"success": True, "data": {"items": []}})
        report = hidrive.re0_sync_cli(["--phase", "tv-follow", "--limit", "20", "--json"])
        assert report["ok"] is True and report["my_packs"]["recorded"] == 0 and report["queried"] == 1
        assert [c["url"] for c in _re0_calls(http)] == ["https://re0.me/api/open/tv-follow/my", "https://re0.me/api/open/tv-follow/packs"]
        assert not [c for c in http.calls if "unlock" in c["url"]]


class TestDiscoveriesRail:
    def test_discoveries_lists_complete_remote_only_projections_with_candidates(self, client, re0_env, http):
        import re0_sync
        store = re0_env["store"]
        client.get(RE0_SEARCH + "?q=远端片&type=all")  # projections for 555 (local) and 999 (remote-only, partial)
        assert client.get("/api/library/re0/discoveries").get_json()["items"] == []  # 999 is not complete yet
        re0_sync.upsert_projection(store, "movie", 999, title="远端片", year=2024, overview="完整简介", poster_path="/p.jpg", backdrop_path="/b.jpg",
                                   ratings={"tmdb": {"score": 7.5, "votes": 10}}, now=200, metadata_status="complete")
        re0_sync.upsert_projection(store, "tv", 4242, title="无候选剧", year=2024, overview="简介", poster_path="/p2.jpg", backdrop_path="/b2.jpg", ratings={}, now=300, metadata_status="complete")
        body = client.get("/api/library/re0/discoveries?limit=12").get_json()
        assert body["success"] is True and [i["media_ref"] for i in body["items"]] == ["re0:movie:999"]
        item = body["items"][0]
        assert item["title"] == "远端片" and item["poster_url"].endswith("/p.jpg") and item["candidate_count"] == 1 and item["local_media_id"]
        assert item["providers"] == ["quark"] and item["state"] == "candidate"
        raw = client.get("/api/library/re0/discoveries").get_data(as_text=True)
        assert "fixture-slug" not in raw and "slug" not in raw
        assert client.get("/api/library/re0/discoveries?limit=99").status_code == 400


# ---------------------------------------------------------------------------
# Round 23: local exact TMDB id first -- a library media that is already
# matched (match_status=exact, tmdb_id set) reaches RE0 without a TMDB title
# search, so an exhausted TMDB budget can no longer hide it.
# ---------------------------------------------------------------------------

RES_TV_SILO = "https://re0.me/api/open/resources/tv/125988"
DIRECT_SEARCH = "https://re0.me/api/open/search"


def _seed_silo(re0_env, *, match_status="exact", tmdb_id=125988, media_type="tv", title="末日地堡"):
    store = re0_env["store"]
    identity = f"tmdb:{media_type}:{tmdb_id}" if tmdb_id else f"title:{title}"
    media_id = store.upsert_media(ls.MediaRecord(media_identity=identity, media_type=media_type, title_zh=title, title_original="Silo",
                                                 search_key=library_normalize.search_key(title), year=2023, tmdb_id=tmdb_id,
                                                 match_status=match_status, poster_path="/silo.jpg", overview="地堡里的人"))
    store.recount()
    import library_search
    library_search.build_index(store)
    return media_id


def _set_tmdb_budget(hidrive, *, used: int, budget: int = 3000) -> None:
    """Write today's row of the app's own TMDB budget table (what the
    settings page shows as ``used/budget``)."""
    import sqlite3
    from datetime import datetime, timezone
    conn = sqlite3.connect(hidrive.LIBRARY_DB_PATH)
    hidrive.library_tmdb.ensure_tables(conn)
    day = datetime.now(timezone.utc).date().isoformat()
    conn.execute("INSERT INTO tmdb_budget(day, used, budget, updated_at) VALUES(?,?,?,0) "
                 "ON CONFLICT(day) DO UPDATE SET used=excluded.used, budget=excluded.budget", (day, used, budget))
    conn.commit()
    conn.close()


class TestLocalExactFirst:
    def test_local_exact_tmdb_id_reaches_re0_when_tmdb_budget_is_exhausted(self, client, re0_env, hidrive, http):
        silo_id = _seed_silo(re0_env)
        _set_tmdb_budget(hidrive, used=3000, budget=3000)
        http.route("GET", RES_TV_SILO, {"success": True, "data": [_re0_item("fixture-slug-silo", "115"), _re0_item("fixture-slug-silo2", "quark")]})
        # Control: a title the library does not know still needs TMDB, which is exhausted.
        blocked = client.get(RE0_SEARCH + "?q=不存在的片&type=all").get_json()
        assert blocked["remote"]["status"] == "tmdb_budget_exhausted" and blocked["remote"]["items"] == []

        response = client.get(RE0_SEARCH + "?q=末日地堡&type=all")
        assert response.status_code == 200, response.get_json()
        remote = response.get_json()["remote"]
        assert remote["status"] == "fresh" and remote["origin"] == "local", remote
        assert [i["tmdb_id"] for i in remote["items"]] == [125988]
        item = remote["items"][0]
        assert item["local_media_id"] == silo_id and item["media_type"] == "tv" and item["media_ref"] == "re0:tv:125988"
        assert item["title"] == "末日地堡" and item["original_title"] == "Silo" and item["year"] == 2023
        assert item["candidate_count"] == 2 and item["providers"] == ["115", "quark"] and item["state"] == "candidate"
        assert [c["url"] for c in _re0_calls(http)] == [RES_TV_SILO]
        assert re0_env["session"].calls == []  # no TMDB title search at all
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]
        raw = response.get_data(as_text=True)
        for needle in ("fixture-slug", "re0.me/r/", "access-1", "app-secret"):
            assert needle not in raw, needle

    def test_local_fast_path_skips_tmdb_even_with_budget_available(self, client, re0_env, http):
        _seed_silo(re0_env)
        http.route("GET", RES_TV_SILO, {"success": True, "data": [_re0_item("fixture-slug-silo", "115")]})
        remote = client.get(RE0_SEARCH + "?q=末日地堡&type=tv").get_json()["remote"]
        assert remote["status"] == "fresh" and remote["origin"] == "local" and [i["tmdb_id"] for i in remote["items"]] == [125988]
        assert re0_env["session"].calls == [] and [c["url"] for c in _re0_calls(http)] == [RES_TV_SILO]

    def test_local_fast_path_writes_and_reads_the_search_cache(self, client, re0_env, http):
        _seed_silo(re0_env)
        http.route("GET", RES_TV_SILO, {"success": True, "data": [_re0_item("fixture-slug-silo", "115")]})
        first = client.get(RE0_SEARCH + "?q=末日地堡&type=all").get_json()["remote"]
        assert first["status"] == "fresh" and first["origin"] == "local"
        conn = re0_env["store"].connect(readonly=True)
        rows = conn.execute("SELECT status, candidate_ids_json, error_class FROM re0_search_cache").fetchall()
        conn.close()
        assert len(rows) == 1 and rows[0]["status"] == "fresh" and rows[0]["error_class"] is None
        assert [tuple(r) for r in __import__("json").loads(rows[0]["candidate_ids_json"])] == [("tv", 125988)]
        again = client.get(RE0_SEARCH + "?q=末日地堡&type=all").get_json()["remote"]
        assert again["status"] == "cached" and again["origin"] == "local" and [i["tmdb_id"] for i in again["items"]] == [125988]
        assert len(_re0_calls(http)) == 1 and re0_env["session"].calls == []

    def test_tmdb_budget_exhaustion_is_not_cached_as_no_results(self, client, re0_env, hidrive, http):
        _set_tmdb_budget(hidrive, used=3000, budget=3000)
        first = client.get(RE0_SEARCH + "?q=远端片&type=movie").get_json()["remote"]
        assert first["status"] == "tmdb_budget_exhausted" and first["items"] == [] and re0_env["session"].calls == []
        conn = re0_env["store"].connect(readonly=True)
        cached = conn.execute("SELECT status FROM re0_search_cache").fetchall()
        conn.close()
        assert all(r["status"] != "no_candidates" for r in cached)
        _set_tmdb_budget(hidrive, used=0, budget=3000)  # budget is back (next day / raised cap)
        second = client.get(RE0_SEARCH + "?q=远端片&type=movie").get_json()["remote"]
        assert second["status"] == "fresh" and second["origin"] == "tmdb"
        assert 999 in [i["tmdb_id"] for i in second["items"]] and len(re0_env["session"].calls) >= 1

    def test_tmdb_running_out_mid_search_is_partial_and_not_cached(self, client, re0_env, hidrive, http):
        _set_tmdb_budget(hidrive, used=2999, budget=3000)  # one request left: /search/movie answers, /search/tv is refused
        remote = client.get(RE0_SEARCH + "?q=远端片&type=all").get_json()["remote"]
        assert remote["status"] == "partial" and remote["reason"] == "tmdb_budget_exhausted" and remote["origin"] == "tmdb"
        assert 999 in [i["tmdb_id"] for i in remote["items"]]
        assert [c["url"] for c in re0_env["session"].calls] == [re0_env["tmdb_base"] + "/search/movie"]
        conn = re0_env["store"].connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_search_cache").fetchone()[0] == 0
        conn.close()

    def test_needs_review_or_missing_tmdb_id_is_never_used_as_a_local_candidate(self, client, re0_env, http):
        _seed_silo(re0_env, match_status="needs_review", tmdb_id=125988)
        _seed_silo(re0_env, match_status="unmatched", tmdb_id=None, media_type="movie", title="末日地堡电影")
        re0_env["session"].route(re0_env["tmdb_base"] + "/search/tv", {"results": []})
        re0_env["session"].route(re0_env["tmdb_base"] + "/search/movie", {"results": []})
        remote = client.get(RE0_SEARCH + "?q=末日地堡&type=all").get_json()["remote"]
        assert remote["status"] == "no_candidates" and remote["items"] == []
        assert _re0_calls(http) == []  # the needs_review guess is never sent to RE0
        assert {c["url"] for c in re0_env["session"].calls} == {re0_env["tmdb_base"] + "/search/movie", re0_env["tmdb_base"] + "/search/tv"}

    def test_local_fast_path_respects_type_and_year_filters(self, client, re0_env, http):
        _seed_silo(re0_env)  # tv, 2023
        http.route("GET", RES_TV_SILO, {"success": True, "data": [_re0_item("fixture-slug-silo", "115")]})
        re0_env["session"].route(re0_env["tmdb_base"] + "/search/movie", {"results": []})
        re0_env["session"].route(re0_env["tmdb_base"] + "/search/tv", {"results": []})
        assert client.get(RE0_SEARCH + "?q=末日地堡&type=movie").get_json()["remote"]["status"] == "no_candidates"
        assert len(re0_env["session"].calls) >= 1 and _re0_calls(http) == []
        tmdb_calls = len(re0_env["session"].calls)
        assert client.get(RE0_SEARCH + "?q=末日地堡&type=tv&year=2020").get_json()["remote"]["status"] == "no_candidates"
        assert len(re0_env["session"].calls) > tmdb_calls and _re0_calls(http) == []
        tmdb_calls = len(re0_env["session"].calls)
        remote = client.get(RE0_SEARCH + "?q=末日地堡&type=tv&year=2023").get_json()["remote"]
        assert remote["origin"] == "local" and [i["tmdb_id"] for i in remote["items"]] == [125988]
        assert len(re0_env["session"].calls) == tmdb_calls and [c["url"] for c in _re0_calls(http)] == [RES_TV_SILO]

    def test_local_fast_path_provider_filter_is_server_side(self, client, re0_env, http):
        _seed_silo(re0_env)
        http.route("GET", RES_TV_SILO, {"success": True, "data": [_re0_item("fixture-slug-silo", "115"), _re0_item("fixture-slug-silo2", "quark")]})
        body = client.get(RE0_SEARCH + "?q=末日地堡&type=all&provider=115").get_json()
        items = body["remote"]["items"]
        assert [i["tmdb_id"] for i in items] == [125988] and items[0]["providers"] == ["115"] and items[0]["candidate_count"] == 1
        assert "quark" not in response_text(body)
        body = client.get(RE0_SEARCH + "?q=末日地堡&type=all&provider=quark").get_json()
        assert body["remote"]["items"][0]["providers"] == ["quark"] and "\"115\"" not in response_text(body)
        assert client.get(RE0_SEARCH + "?q=末日地堡&type=all&provider=ed2k").get_json()["remote"]["items"] == []
        assert len(_re0_calls(http)) == 1  # the three provider views share one RE0 fetch

    def test_local_and_remote_candidates_dedupe_by_type_and_tmdb_id(self, client, re0_env, hidrive, http):
        hidrive.setting_set("re0_direct_search_path", "/api/open/search")
        silo_id = _seed_silo(re0_env)
        http.route("GET", RES_TV_SILO, {"success": True, "data": [_re0_item("fixture-slug-silo", "115")]})
        http.route("GET", DIRECT_SEARCH, {"success": True, "data": {"items": [
            {"media_type": "tv", "tmdb_id": 125988, "title": "远端标题不应覆盖", "year": 2023},
            {"media_type": "movie", "tmdb_id": 999, "title": "远端片", "year": 2024},
        ]}})
        remote = client.get(RE0_SEARCH + "?q=末日地堡&type=all").get_json()["remote"]
        assert remote["mode"] == "direct" and remote["status"] == "fresh" and remote["origin"] == "local"
        assert [(i["media_type"], i["tmdb_id"]) for i in remote["items"]] == [("tv", 125988), ("movie", 999)]
        assert remote["items"][0]["local_media_id"] == silo_id and remote["items"][0]["title"] == "末日地堡"
        assert [c["url"] for c in _re0_calls(http)] == [DIRECT_SEARCH, RES_TV_SILO, RES_MOVIE_999]
        assert re0_env["session"].calls == []

    def test_direct_search_failure_keeps_local_matches_as_partial(self, client, re0_env, hidrive, http):
        hidrive.setting_set("re0_direct_search_path", "/api/open/search")
        _seed_silo(re0_env)
        http.route("GET", RES_TV_SILO, {"success": True, "data": [_re0_item("fixture-slug-silo", "115")]})
        http.route("GET", DIRECT_SEARCH, {"success": False, "message": "not found https://re0.me/x"}, 404)
        remote = client.get(RE0_SEARCH + "?q=末日地堡&type=all").get_json()["remote"]
        assert remote["status"] == "partial" and remote["reason"] == "upstream_4xx" and remote["origin"] == "local"
        assert [i["tmdb_id"] for i in remote["items"]] == [125988]
        assert "re0.me/x" not in (remote.get("message") or "")
        assert [c["url"] for c in _re0_calls(http)] == [DIRECT_SEARCH, RES_TV_SILO]
        conn = re0_env["store"].connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_search_cache").fetchone()[0] == 0  # partial answers are not cached
        conn.close()


# ---------------------------------------------------------------------------
# Composition work order §5.1/§5.2/§5.3: the detail payload's new decision
# fields, the on-demand file-preview proxy, and provider isolation.
# ---------------------------------------------------------------------------

FILE_PREVIEW_A = "https://re0.me/api/open/resources/file-list/fixture-slug-a"


def _preview_body(files=None, **over):
    if files is None:
        files = [{"name": f"Silo.S03E{i:02d}.mkv", "path": f"/Silo/S03/E{i:02d}.mkv", "size": 1073741824, "extension": "mkv"}
                 for i in range(1, 11)]
    body = {"provider": "115", "list_type": "folder", "share_title": "末日地堡 S03", "file_count": len(files),
            "result_type": "listing", "resource_validate_status": "valid", "files": files}
    body.update(over)
    return {"success": True, "data": body}


class TestCompositionPayloadAndPreview:
    def _seed(self, client, http, re0_env, remark="4K高码，24集首更完结"):
        http.route("GET", RES_MOVIE_555, {"success": True, "data": [
            dict(_re0_item("fixture-slug-a", "115"), title="末日地堡 (2023)", share_size="79.52GB", remark=remark,
                 created_at="2025-04-30T00:00:00+08:00", user={"nickname": "C", "id": 9}, is_official=False,
                 unlocked_users_count=3, subtitle_language=["简中"], subtitle_type=["内封"]),
            _re0_item("fixture-slug-b", "189"),
        ]})
        client.get(RE0_SEARCH + "?q=本地片&type=movie")
        return _resource_id(re0_env["store"], "115")

    def test_detail_payload_carries_remark_publisher_and_composition(self, client, re0_env, http):
        self._seed(client, http, re0_env)
        rows = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()["re0_candidates"]
        row = [r for r in rows if r["provider"] == "115"][0]
        assert row["remark"] == "4K高码，24集首更完结"
        assert row["published_at"] == "2025-04-29T16:00:00+00:00"
        assert row["publisher"] == {"nickname": "C", "avatar_url": None}
        assert row["is_official"] is False and row["unlocked_users_count"] == 3
        assert row["subtitle_language"] == ["简中"] and row["subtitle_type"] == ["内封"]
        assert row["composition"]["display"] == "24集" and row["composition"]["confidence"] == "declared"
        assert row["composition"]["completion"] == "complete"
        assert row["file_preview"] == {"available": True, "status": "not_loaded", "file_count": None, "fetched_at": None}
        raw = client.get(f"/api/library/media/{re0_env['media_id']}").get_data(as_text=True)
        for needle in ("fixture-slug", "re0.me/r/", "access-1", "app-secret", '"id": 9'):
            assert needle not in raw, needle

    def test_a_share_without_a_remark_says_the_composition_is_unstated(self, client, re0_env, http):
        self._seed(client, http, re0_env, remark=None)
        rows = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()["re0_candidates"]
        row = [r for r in rows if r["provider"] == "115"][0]
        assert row["remark"] is None
        assert row["composition"]["display"] == "构成未说明" and row["composition"]["kind"] == "unknown"
        assert row["title"] == "末日地堡 (2023)"  # the media title is shown, never turned into a composition

    def test_file_preview_proxy_fetches_once_then_serves_the_cache(self, client, re0_env, http):
        rid = self._seed(client, http, re0_env, remark=None)  # nothing declared -> the files decide
        http.route("GET", FILE_PREVIEW_A, _preview_body())
        before = len(_re0_calls(http))
        response = client.get(f"/api/library/re0/candidates/{rid}/file-preview")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["success"] is True and body["preview"]["status"] == "ready" and body["preview"]["file_count"] == 10
        assert [f["name"] for f in body["preview"]["files"]][:1] == ["Silo.S03E01.mkv"]
        assert body["preview"]["composition"]["display"] == "S03 · 第 1–10 集"
        assert len(_re0_calls(http)) == before + 1
        again = client.get(f"/api/library/re0/candidates/{rid}/file-preview").get_json()
        assert again["preview"]["cached"] is True and len(_re0_calls(http)) == before + 1
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]
        raw = response.get_data(as_text=True)
        for needle in ("fixture-slug", "re0.me", "access-1", "app-secret"):
            assert needle not in raw, needle
        # The row now reports the cached preview without another call.
        row = [r for r in client.get(f"/api/library/media/{re0_env['media_id']}").get_json()["re0_candidates"] if r["id"] == rid][0]
        assert row["file_preview"]["status"] == "ready" and row["file_preview"]["file_count"] == 10

    def test_preview_maps_upstream_refusals_to_stable_statuses(self, client, re0_env, http):
        from tests.conftest import FakeResponse
        rid = self._seed(client, http, re0_env)
        cases = [(403, {"success": False, "code": "USER_LEVEL_REQUIRED"}, "forbidden", "当前账号等级不支持文件预览"),
                 (400, {"success": False, "message": "文件列表获取失败"}, "unsupported", "该来源暂不支持文件预览")]
        for status, payload, expected, message in cases:
            conn = re0_env["store"].connect()
            conn.execute("DELETE FROM re0_file_preview"); conn.commit(); conn.close()
            _re0_client_reset_for(re0_env)
            http.route("GET", FILE_PREVIEW_A, payload, status)
            body = client.get(f"/api/library/re0/candidates/{rid}/file-preview").get_json()
            assert body["preview"]["status"] == expected and message in body["preview"]["message"]
            assert body["preview"]["files"] == []
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]

    def test_preview_rate_limit_reports_retry_after_and_is_not_cached(self, client, re0_env, http):
        from tests.conftest import FakeResponse
        rid = self._seed(client, http, re0_env)
        http.route("GET", FILE_PREVIEW_A, handler=lambda **kw: FakeResponse({"success": False}, 429, headers={"Retry-After": "45"}))
        response = client.get(f"/api/library/re0/candidates/{rid}/file-preview")
        assert response.status_code == 429
        body = response.get_json()
        assert body["preview"]["error_class"] == "rate_limited" and body["preview"]["retry_after"] == 45
        conn = re0_env["store"].connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_file_preview").fetchone()[0] == 0
        conn.close()

    def test_preview_rejects_an_unknown_candidate_without_calling_re0(self, client, re0_env, http):
        self._seed(client, http, re0_env)
        before = len(_re0_calls(http))
        assert client.get("/api/library/re0/candidates/987654/file-preview").status_code == 404
        assert client.get("/api/library/re0/candidates/0/file-preview").status_code == 404
        assert len(_re0_calls(http)) == before

    def test_provider_filter_keeps_the_preview_and_rows_isolated(self, client, re0_env, http):
        self._seed(client, http, re0_env)
        body = client.get(f"/api/library/media/{re0_env['media_id']}?provider=115").get_json()
        assert [r["provider"] for r in body["re0_candidates"]] == ["115"]
        assert "tianyicloud" not in response_text(body) and "天翼" not in response_text(body)
        other = client.get(f"/api/library/media/{re0_env['media_id']}?provider=tianyicloud").get_json()
        assert [r["provider"] for r in other["re0_candidates"]] == ["tianyicloud"]

    def test_re0_candidates_never_enter_local_link_counts(self, client, re0_env, http):
        before = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        self._seed(client, http, re0_env)
        after = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        # §5.3: RE0 candidates never count as local links -- the groups and
        # their live link totals are byte-for-byte what they were.
        assert after["groups"] == before["groups"] and after["group_count"] == before["group_count"]
        local_facets = [{k: v for k, v in f.items() if k != "re0_count"} for f in after["provider_facets"] if f["link_count"]]
        before_local_facets = [{k: v for k, v in f.items() if k != "re0_count"} for f in before["provider_facets"] if f["link_count"]]
        assert local_facets == before_local_facets
        assert len(before["re0_candidates"]) == 2 and len(after["re0_candidates"]) == 2


    def test_a_refused_preview_carries_a_readable_top_level_message(self, client, re0_env, http):
        """Round 31: the browser's fetch wrapper reads `message` off the body
        for any non-2xx -- without one the user just sees "HTTP 429"."""
        from tests.conftest import FakeResponse
        rid = self._seed(client, http, re0_env)
        http.route("GET", FILE_PREVIEW_A, handler=lambda **kw: FakeResponse({"success": False}, 429, headers={"Retry-After": "45"}))
        response = client.get(f"/api/library/re0/candidates/{rid}/file-preview")
        assert response.status_code == 429
        body = response.get_json()
        assert body["success"] is False
        assert "限流" in body["message"] and "45" in body["message"]
        assert body["code"] == "RE0_RATE_LIMITED" and body["retry_after"] == 45
        assert body["preview"]["error_class"] == "rate_limited"

    def test_a_quota_or_auth_refusal_also_reads_as_a_sentence(self, client, re0_env, http, hidrive, monkeypatch):
        rid = self._seed(client, http, re0_env)
        http.route("GET", FILE_PREVIEW_A, {"success": False, "code": "OPENAPI_REFRESH_REQUIRED"}, 401)
        monkeypatch.setattr(hidrive, "refresh_hdhive_token", lambda *a, **k: None)
        body = client.get(f"/api/library/re0/candidates/{rid}/file-preview").get_json()
        assert body["success"] is False and body["code"] == "RE0_REAUTH_REQUIRED"
        assert "授权" in body["message"]

    def test_a_successful_preview_needs_no_error_message(self, client, re0_env, http):
        rid = self._seed(client, http, re0_env)
        http.route("GET", FILE_PREVIEW_A, _preview_body())
        body = client.get(f"/api/library/re0/candidates/{rid}/file-preview").get_json()
        assert body["success"] is True and body.get("code") is None


def _re0_client_reset_for(re0_env):
    import app as hidrive
    hidrive._re0_client_reset()


# ---------------------------------------------------------------------------
# Invalid-candidate work order §5.2: a share RE0 confirmed dead is hidden by
# default, visible on request, and can never be unlocked; a protocol link has
# no file preview. Nothing here unlocks -- every test asserts that.
# ---------------------------------------------------------------------------

FILE_PREVIEW_B = "https://re0.me/api/open/resources/file-list/fixture-slug-b"


class TestInvalidCandidateVisibility:
    def _seed(self, client, http, re0_env, *, b_status="invalid", b_message="链接状态异常，需人工复核"):
        """115 candidate valid, tianyicloud candidate as given, one ed2k."""
        http.route("GET", RES_MOVIE_555, {"success": True, "data": [
            dict(_re0_item("fixture-slug-a", "115"), title="有效分享", validate_status="valid"),
            dict(_re0_item("fixture-slug-b", "189"), title="失效分享", validate_status=b_status,
                 validate_message=b_message, last_validated_at="2026-09-10T00:00:00+08:00"),
            dict(_re0_item("fixture-slug-e", "ed2k"), title="协议链接", validate_status="valid"),
        ]})
        client.get(RE0_SEARCH + "?q=本地片&type=movie")
        store = re0_env["store"]
        return {p: _resource_id(store, p) for p in ("115", "tianyicloud", "ed2k")}

    def test_a_dead_candidate_is_hidden_by_default_and_counted(self, client, re0_env, http):
        ids = self._seed(client, http, re0_env)
        body = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        shown = {c["id"] for c in body["re0_candidates"]}
        assert ids["tianyicloud"] not in shown and ids["115"] in shown and ids["ed2k"] in shown
        assert body["re0_invalid_hidden_count"] == 1
        # The tab must not advertise a pan whose only candidate is hidden.
        assert "tianyicloud" not in {f["provider"] for f in body["provider_facets"]}

    def test_the_audit_view_returns_it_with_its_reason(self, client, re0_env, http):
        ids = self._seed(client, http, re0_env)
        body = client.get(f"/api/library/media/{re0_env['media_id']}?include_invalid=1").get_json()
        row = [c for c in body["re0_candidates"] if c["id"] == ids["tianyicloud"]][0]
        assert row["effective_status"] == "invalid" and row["effective_status_label"] == "RE0 已失效"
        assert row["effective_status_reason"] == "链接状态异常，需人工复核"
        assert row["effective_checked_at"] == "2026-09-09T16:00:00+00:00" and row["has_local_link"] is False
        assert body["re0_invalid_hidden_count"] == 0
        assert "tianyicloud" in {f["provider"] for f in body["provider_facets"]}
        raw = client.get(f"/api/library/media/{re0_env['media_id']}?include_invalid=1").get_data(as_text=True)
        for needle in ("fixture-slug", "re0.me/r/", "access-1", "app-secret"):
            assert needle not in raw, needle

    def test_include_invalid_is_independent_of_include_deleted(self, client, re0_env, http):
        self._seed(client, http, re0_env)
        base = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        deleted = client.get(f"/api/library/media/{re0_env['media_id']}?include_deleted=1").get_json()
        # include_deleted is about LOCAL links and must not reveal a dead RE0 candidate.
        assert len(deleted["re0_candidates"]) == len(base["re0_candidates"])
        assert deleted["re0_invalid_hidden_count"] == 1
        both = client.get(f"/api/library/media/{re0_env['media_id']}?include_deleted=1&include_invalid=1").get_json()
        assert len(both["re0_candidates"]) == len(base["re0_candidates"]) + 1

    def test_checking_and_unknown_are_never_hidden(self, client, re0_env, http):
        for status in ("checking", "error", None):
            conn = re0_env["store"].connect()
            conn.execute("DELETE FROM re0_resource"); conn.execute("DELETE FROM re0_search_cache")
            # The lane also skips RE0 inside the 24h resource TTL, so the
            # projection has to look unfetched for the reseed to happen.
            conn.execute("UPDATE re0_media_projection SET last_fetched_at=NULL")
            conn.commit(); conn.close()
            ids = self._seed(client, http, re0_env, b_status=status, b_message=None)
            body = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
            assert ids["tianyicloud"] in {c["id"] for c in body["re0_candidates"]}, status
            assert body["re0_invalid_hidden_count"] == 0, status

    def test_a_dead_candidate_that_already_produced_a_local_link_is_never_hidden(self, client, re0_env, http):
        ids = self._seed(client, http, re0_env)
        store = re0_env["store"]
        conn = store.connect()
        link_id = conn.execute("SELECT id FROM resource_link ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO re0_resource_link(re0_resource_id, resource_link_id, relation, linked_at) VALUES(?,?,?,?)",
                     (ids["tianyicloud"], link_id, "materialized_unlocked", 100))
        conn.commit(); conn.close()
        body = client.get(f"/api/library/media/{re0_env['media_id']}").get_json()
        row = [c for c in body["re0_candidates"] if c["id"] == ids["tianyicloud"]][0]
        assert row["effective_status"] == "invalid" and row["has_local_link"] is True and row["resource_link_id"]
        assert body["re0_invalid_hidden_count"] == 0
        # The local link itself is untouched: same groups, same rows.
        assert body["groups"] == client.get(f"/api/library/media/{re0_env['media_id']}").get_json()["groups"]

    def test_unlocking_a_dead_candidate_is_refused_before_re0_is_ever_called(self, client, re0_env, http):
        ids = self._seed(client, http, re0_env)
        before = len([c for c in http.calls if c["url"] == UNLOCK_URL])
        response = client.post(f"/api/library/re0-resource/{ids['tianyicloud']}/unlock-and-action",
                               json={"action": "copy", "request_id": "req-inv-01"})
        assert response.status_code == 409
        body = response.get_json()
        assert body["code"] == "RE0_RESOURCE_INVALID" and "失效" in body["message"]
        assert len([c for c in http.calls if c["url"] == UNLOCK_URL]) == before == 0
        conn = re0_env["store"].connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_resource_link").fetchone()[0] == 0
        conn.close()

    def test_a_valid_candidate_still_unlocks_normally(self, client, re0_env, http, audit_rows):
        ids = self._seed(client, http, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakevalid1"))
        body = client.post(f"/api/library/re0-resource/{ids['115']}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-inv-02"}).get_json()
        assert body["success"] is True and body["next_action"] == "transfer" and body["link_public_id"]

    def test_an_already_materialised_dead_candidate_replays_without_unlocking(self, client, re0_env, http):
        ids = self._seed(client, http, re0_env)
        store = re0_env["store"]
        conn = store.connect()
        link_row = conn.execute("SELECT id, public_id FROM resource_link ORDER BY id LIMIT 1").fetchone()
        conn.execute("INSERT INTO re0_resource_link(re0_resource_id, resource_link_id, relation, linked_at) VALUES(?,?,?,?)",
                     (ids["tianyicloud"], link_row["id"], "materialized_unlocked", 100))
        conn.commit(); conn.close()
        response = client.post(f"/api/library/re0-resource/{ids['tianyicloud']}/unlock-and-action",
                               json={"action": "copy", "request_id": "req-inv-03"})
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["link_public_id"] == link_row["public_id"]
        assert not [c for c in http.calls if c["url"] == UNLOCK_URL]

    def test_an_ed2k_preview_request_answers_unsupported_without_calling_re0(self, client, re0_env, http):
        ids = self._seed(client, http, re0_env)
        before = len(_re0_calls(http))
        body = client.get(f"/api/library/re0/candidates/{ids['ed2k']}/file-preview").get_json()
        assert body["preview"]["status"] == "unsupported"
        assert body["preview"]["error_class"] == "preview_not_applicable" and "协议链接" in body["preview"]["message"]
        assert len(_re0_calls(http)) == before
        conn = re0_env["store"].connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_file_preview").fetchone()[0] == 0
        conn.close()
        row = [c for c in client.get(f"/api/library/media/{re0_env['media_id']}").get_json()["re0_candidates"]
               if c["id"] == ids["ed2k"]][0]
        assert row["file_preview"]["available"] is False and row["file_preview"]["status"] == "not_applicable"
        assert row["effective_status"] != "invalid" and row["action"] == "cloud"

    def test_remote_only_detail_uses_the_same_rules(self, client, re0_env, http):
        http.route("GET", RES_MOVIE_999, {"success": True, "data": [
            dict(_re0_item("fixture-slug-c", "quark"), validate_status="invalid", validate_message="已失效"),
        ]})
        client.get(RE0_SEARCH + "?q=远端片&type=movie")
        default = client.get("/api/library/re0-media/movie/999").get_json()
        assert default["re0_candidates"] == [] and default["re0_invalid_hidden_count"] == 1
        assert default["provider_facets"] == []
        audit = client.get("/api/library/re0-media/movie/999?include_invalid=1").get_json()
        assert len(audit["re0_candidates"]) == 1 and audit["re0_candidates"][0]["effective_status"] == "invalid"
        assert audit["re0_invalid_hidden_count"] == 0
