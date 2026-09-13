"""re0_sync core: pan_type mapping, item whitelisting/normalisation, slug
hashing, projection tables and the Re0Client's rate-limit/refresh/backoff
behaviour. Every slug/URL/token here is a fixture, never real."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import re0_sync  # noqa: E402


def _conn_factory(tmp_path):
    path = tmp_path / "re0.db"
    def factory():
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn
    conn = factory(); re0_sync.ensure_tables(conn); conn.commit(); conn.close()
    return factory


class TestMappingAndNormalisation:
    @pytest.mark.parametrize("raw,expected", [
        ("115", "115"), ("189", "tianyicloud"), ("quark", "quark"), ("aliPan", "alipan"), ("baiDu", "baidu"),
        ("139", "139cloud"), ("guangYa", "guangya"), ("ed2k", "ed2k"), ("magnet", "ed2k"),
    ])
    def test_pan_type_explicit_map(self, raw, expected):
        assert re0_sync.map_pan_type(raw) == expected

    @pytest.mark.parametrize("raw", ["xunLei", "pikPak", "uc", "QUARK", "Quark", "", None, 123])
    def test_pan_type_unmapped_is_unknown_never_guessed(self, raw):
        assert re0_sync.map_pan_type(raw) == "unknown"

    def test_normalize_item_whitelists_and_normalises_specs(self):
        item = {
            "slug": "fixture-slug-abc", "media_slug": "fixture-media", "media_url": "https://re0.example/r/fixture-slug-abc",
            "pan_type": "189", "source": ["WEB-DL/WEBRip"], "video_resolution": ["4K"], "subtitle_language": ["中文"],
            "subtitle_type": ["内封"], "unlock_points": "5", "is_unlocked": False, "validate_status": "valid",
            "url": "https://cloud.189.cn/t/leak", "full_url": "https://cloud.189.cn/t/leak?pwd=1234", "access_code": "1234",
            "title": "示例 4K", "updated_at": "2026-09-01T00:00:00Z",
        }
        out = re0_sync.normalize_item(item, salt="salt-for-tests")
        assert out["slug"] == "fixture-slug-abc"
        assert out["slug_hash"] == re0_sync.slug_hash("fixture-slug-abc", "salt-for-tests")
        assert out["provider_code"] == "tianyicloud" and out["upstream_pan_type"] == "189"
        assert out["spec"] == {"quality": "2160p", "source_type": "webdl", "hdr": None,
                               "raw": {"video_resolution": ["4K"], "source": ["WEB-DL/WEBRip"], "subtitle_language": ["中文"], "subtitle_type": ["内封"],
                                       "title": "示例 4K", "share_size": None,
                                       # Round 30 display fields: absent upstream -> null, never guessed.
                                       "remark": None, "created_at": None, "publisher": None, "is_official": None,
                                       "unlocked_users_count": None, "last_validated_at": None, "validate_message": None}}
        assert out["unlock_points"] == 5 and out["is_unlocked"] is False and out["validate_status"] == "valid"
        assert out["title"] == "示例 4K"
        for forbidden in ("media_url", "url", "full_url", "access_code", "media_slug"):
            assert forbidden not in out
        assert out["payload_url"] is None and out["payload_access_code"] is None

    def test_normalize_item_keeps_unlocked_payload_separately(self):
        item = {"slug": "s1", "pan_type": "115", "is_unlocked": True, "url": "https://115.com/s/swfake", "access_code": "ab12"}
        out = re0_sync.normalize_item(item, salt="x")
        assert out["is_unlocked"] is True
        assert out["payload_url"] == "https://115.com/s/swfake" and out["payload_access_code"] == "ab12"
        assert out["spec"]["quality"] is None

    @pytest.mark.parametrize("item", [{}, {"slug": ""}, {"slug": None, "pan_type": "115"}, "not-a-dict", {"slug": "x" * 300}])
    def test_normalize_item_rejects_invalid(self, item):
        assert re0_sync.normalize_item(item, salt="x") is None

    def test_slug_hash_is_irreversible_and_salted(self):
        a = re0_sync.slug_hash("fixture-slug", "salt-a")
        b = re0_sync.slug_hash("fixture-slug", "salt-b")
        assert a != b and len(a) == 64 and "fixture" not in a
        assert re0_sync.slug_hash("fixture-slug", "salt-a") == a


class TestTables:
    def test_ensure_tables_idempotent_and_named(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        re0_sync.ensure_tables(conn); re0_sync.ensure_tables(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"re0_sync_state", "re0_search_cache", "re0_media_projection", "re0_resource", "re0_resource_link", "re0_action"} <= names
        cols = {r[1] for r in conn.execute("PRAGMA table_info(re0_media_projection)")}
        assert {"ratings_status", "metadata_fetched_at", "ratings_fetched_at", "local_media_id"} <= cols

    def test_ensure_tables_adds_missing_projection_columns_without_dropping(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        conn.execute("CREATE TABLE re0_media_projection (media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL, title TEXT NOT NULL, last_seen_at INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY (media_type, tmdb_id))")
        conn.execute("INSERT INTO re0_media_projection VALUES ('movie', 1, '旧投影', 1, 1, 1)")
        conn.commit()
        re0_sync.ensure_tables(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(re0_media_projection)")}
        assert {"ratings_status", "metadata_fetched_at", "ratings_fetched_at", "poster_path", "backdrop_path"} <= cols
        assert conn.execute("SELECT title FROM re0_media_projection").fetchone()[0] == "旧投影"


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, raw=None):
        self.status_code = status; self._payload = payload; self.headers = headers or {}
        self.content = raw if raw is not None else (b"{}" if payload is not None else b"")
    def json(self):
        if self._payload is None: raise ValueError("no json")
        return self._payload


class FakeRequester:
    def __init__(self):
        self.routes = {}; self.calls = []
    def route(self, method, url, outcomes):
        self.routes[(method, url)] = list(outcomes) if isinstance(outcomes, list) else [outcomes]
        return self
    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        queue = self.routes.get((method, url))
        if not queue: raise AssertionError(f"unscripted {method} {url}")
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception): raise outcome
        return outcome


def _client(tmp_path, requester, **over):
    state = {"token": "access-fixture", "refreshes": 0, "refresh_ok": True, "sleeps": [], "now": 1_700_000_000.0}
    def refresh():
        state["refreshes"] += 1
        if state["refresh_ok"]:
            state["token"] = "access-renewed"; return state["token"]
        return None
    kwargs = dict(
        request=requester, api_key_provider=lambda: "app-secret-fixture", token_provider=lambda: state["token"],
        refresh_token=refresh, conn_factory=_conn_factory(tmp_path), now=lambda: state["now"],
        sleep=lambda s: state["sleeps"].append(s), min_interval_ms=1000, daily_cap=3, base="https://re0.test",
    )
    kwargs.update(over)
    return re0_sync.Re0Client(**kwargs), state


PING = "https://re0.test/api/open/ping"


class TestRe0Client:
    def test_success_carries_headers_and_counts_budget(self, tmp_path):
        req = FakeRequester().route("GET", PING, FakeResponse(200, {"success": True, "data": {"ok": 1}}))
        client, state = _client(tmp_path, req)
        result = client.get("/api/open/ping")
        assert result.ok and result.status == 200 and result.data == {"ok": 1} and result.error_class is None
        assert req.calls[0]["headers"] == {"X-API-Key": "app-secret-fixture", "Authorization": "Bearer access-fixture", "Accept": "application/json"}
        assert client.budget_status()["used_today"] == 1 and client.budget_status()["daily_cap"] == 3

    def test_min_interval_between_requests(self, tmp_path):
        req = FakeRequester().route("GET", PING, FakeResponse(200, {"success": True, "data": {}}))
        client, state = _client(tmp_path, req)
        client.get("/api/open/ping"); client.get("/api/open/ping")
        assert state["sleeps"] and abs(state["sleeps"][0] - 1.0) < 0.01

    def test_daily_cap_blocks_without_network(self, tmp_path):
        req = FakeRequester().route("GET", PING, FakeResponse(200, {"success": True, "data": {}}))
        client, state = _client(tmp_path, req, daily_cap=2)
        client.get("/api/open/ping"); client.get("/api/open/ping")
        blocked = client.get("/api/open/ping")
        assert not blocked.ok and blocked.error_class == "quota_exhausted" and len(req.calls) == 2

    def test_401_refreshes_once_then_retries(self, tmp_path):
        req = FakeRequester().route("GET", PING, [
            FakeResponse(401, {"success": False, "code": "OPENAPI_REFRESH_REQUIRED"}), FakeResponse(200, {"success": True, "data": {"ok": 1}}),
        ])
        client, state = _client(tmp_path, req)
        result = client.get("/api/open/ping")
        assert result.ok and state["refreshes"] == 1 and req.calls[1]["headers"]["Authorization"] == "Bearer access-renewed"

    def test_401_refresh_failure_is_reauth_required_and_stops(self, tmp_path):
        req = FakeRequester().route("GET", PING, FakeResponse(401, {"success": False, "code": "OPENAPI_REFRESH_REQUIRED"}))
        client, state = _client(tmp_path, req); state["refresh_ok"] = False
        result = client.get("/api/open/ping")
        assert not result.ok and result.error_class == "reauth_required" and len(req.calls) == 1
        again = client.get("/api/open/ping")
        assert again.error_class == "reauth_required" and len(req.calls) == 1  # sticky until a token change

    def test_403_scope_denied_without_retry(self, tmp_path):
        req = FakeRequester().route("GET", PING, FakeResponse(403, {"success": False, "code": "SCOPE_NOT_ALLOWED"}))
        client, _ = _client(tmp_path, req)
        result = client.get("/api/open/ping")
        assert result.error_class == "scope_denied" and len(req.calls) == 1
        assert client.get("/api/open/ping").error_class == "scope_denied" and len(req.calls) == 1

    def test_403_user_level_denied(self, tmp_path):
        req = FakeRequester().route("GET", PING, FakeResponse(403, {"success": False, "code": "USER_LEVEL_NOT_ALLOWED"}))
        client, _ = _client(tmp_path, req)
        assert client.get("/api/open/ping").error_class == "user_level_denied"

    def test_429_reads_retry_after_persists_cooldown_and_does_not_retry(self, tmp_path):
        req = FakeRequester().route("GET", PING, FakeResponse(429, {"success": False}, headers={"Retry-After": "120"}))
        client, state = _client(tmp_path, req)
        result = client.get("/api/open/ping")
        assert result.error_class == "rate_limited" and result.retry_after == 120 and len(req.calls) == 1
        conn = client._conn_factory()
        until = int(conn.execute("SELECT value FROM re0_sync_state WHERE key='cooldown_until'").fetchone()[0])
        assert until == int(state["now"]) + 120
        blocked = client.get("/api/open/ping")
        assert blocked.error_class == "rate_limited" and blocked.retry_after == 120 and len(req.calls) == 1
        state["now"] += 121
        req.route("GET", PING, FakeResponse(200, {"success": True, "data": {}}))
        assert client.get("/api/open/ping").ok

    def test_5xx_bounded_backoff_then_retryable(self, tmp_path):
        req = FakeRequester().route("GET", PING, FakeResponse(503, {"success": False}))
        client, state = _client(tmp_path, req, daily_cap=10)
        result = client.get("/api/open/ping")
        assert result.error_class == "upstream_5xx" and result.retryable is True
        assert len(req.calls) == 3 and len([s for s in state["sleeps"] if s >= 1.0]) >= 2

    def test_network_error_and_invalid_json(self, tmp_path):
        import requests as requests_lib
        req = FakeRequester().route("GET", PING, requests_lib.ConnectionError("boom"))
        client, _ = _client(tmp_path, req, daily_cap=10)
        assert client.get("/api/open/ping").error_class == "network_error"
        req2 = FakeRequester().route("GET", PING, FakeResponse(200, None, raw=b"<html>"))
        client2, _ = _client(tmp_path, req2, daily_cap=10)
        assert client2.get("/api/open/ping").error_class == "invalid_json"

    def test_missing_credentials_never_hits_network(self, tmp_path):
        req = FakeRequester()
        client, _ = _client(tmp_path, req, api_key_provider=lambda: "")
        assert client.get("/api/open/ping").error_class == "missing_credentials"
        client2, _ = _client(tmp_path, req, token_provider=lambda: None)
        assert client2.get("/api/open/ping").error_class == "reauth_required"
        assert req.calls == []

    def test_upstream_4xx_and_success_false(self, tmp_path):
        req = FakeRequester().route("GET", PING, [FakeResponse(404, {"success": False, "code": "NOT_FOUND"}), FakeResponse(200, {"success": False, "code": "X", "message": "bad https://leak.example/x"})])
        client, _ = _client(tmp_path, req, daily_cap=10)
        first = client.get("/api/open/ping"); second = client.get("/api/open/ping")
        assert first.error_class == "upstream_4xx" and first.code == "NOT_FOUND"
        assert second.error_class == "upstream_4xx" and "leak.example" not in (second.message or "")

    def test_post_sends_json_and_is_not_budget_exempt(self, tmp_path):
        url = "https://re0.test/api/open/resources/unlock"
        req = FakeRequester().route("POST", url, FakeResponse(200, {"success": True, "data": {"already_owned": True}}))
        client, _ = _client(tmp_path, req)
        result = client.post("/api/open/resources/unlock", json={"slug": "fixture-slug"})
        assert result.ok and result.data["already_owned"] is True
        assert req.calls[0]["json"] == {"slug": "fixture-slug"} and client.budget_status()["used_today"] == 1


# ---------------------------------------------------------------------------
# Store integration: re0 tables on the library index, insert-only links,
# idempotent candidate upserts, materialisation of unlocked payloads.
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
import library_store as ls  # noqa: E402


@pytest.fixture
def store(tmp_path):
    st = ls.LibraryStore(tmp_path / "lib.db", Fernet(Fernet.generate_key()))
    st.create_schema()
    return st


def _media(store, tmdb_id=555, title="本地片", media_type="movie"):
    return store.upsert_media(ls.MediaRecord(media_identity=f"tmdb:{media_type}:{tmdb_id}", media_type=media_type, title_zh=title,
                                             search_key=title, tmdb_id=tmdb_id, match_status="exact", poster_path="/p.jpg"))


def _local_link(store, group_id, url="https://cloud.189.cn/t/fixtureLocal1", provider="tianyicloud"):
    info = library_normalize.parse_link(url, None)
    rec = ls.LinkRecord(public_id=f"pub-{hashlib.sha1(url.encode()).hexdigest()[:12]}", group_id=group_id, provider=info.provider,
                        canonical_url_hash=library_normalize.canonical_hash(info.canonical), url_label=info.label,
                        url_ciphertext=store.fernet.encrypt(url.encode("utf-8")), has_access_code=0)
    link_id, _ = store.upsert_link(rec)
    return link_id, rec


import library_normalize  # noqa: E402


class TestStoreIntegration:
    def test_re0_tables_appear_on_write_connect(self, store):
        conn = store.connect(readonly=True)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert {"re0_resource", "re0_media_projection", "re0_search_cache", "re0_sync_state"} <= names

    def test_insert_link_preserving_existing_never_touches_old_row(self, store):
        media_id = _media(store)
        old_group = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-old", display_title="旧组"))
        new_group = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-new", display_title="新组"))
        link_id, rec = _local_link(store, old_group)
        conn = store.connect(readonly=True)
        before = dict(conn.execute("SELECT * FROM resource_link WHERE id=?", (link_id,)).fetchone()); conn.close()
        clash = ls.LinkRecord(public_id="pub-clash", group_id=new_group, provider=rec.provider, canonical_url_hash=rec.canonical_url_hash,
                              url_label="RE0 · 新标签", url_ciphertext=store.fernet.encrypt(b"https://cloud.189.cn/t/fixtureLocal1?x=1"),
                              has_access_code=1, deleted_at_source=None)
        got_id, created = store.insert_link_preserving_existing(clash)
        assert got_id == link_id and created is False
        conn = store.connect(readonly=True)
        after = dict(conn.execute("SELECT * FROM resource_link WHERE id=?", (link_id,)).fetchone())
        assert conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0] == 1
        conn.close()
        assert after == before
        fresh = ls.LinkRecord(public_id="pub-fresh", group_id=new_group, provider="115", canonical_url_hash="h-fresh", url_label="115 分享",
                              url_ciphertext=store.fernet.encrypt(b"https://115.com/s/swfakefresh"))
        fresh_id, created = store.insert_link_preserving_existing(fresh)
        assert created is True and fresh_id != link_id

    def test_upsert_candidate_is_idempotent_per_slug(self, store):
        media_id = _media(store)
        item = re0_sync.normalize_item({"slug": "fixture-slug-1", "pan_type": "quark", "video_resolution": ["1080p"], "unlock_points": 3, "is_unlocked": False}, salt="s")
        rid, created = re0_sync.upsert_resource(store, "movie", 555, item, media_id=media_id, media_title="本地片", now=100)
        rid2, created2 = re0_sync.upsert_resource(store, "movie", 555, dict(item, unlock_points=4), media_id=media_id, media_title="本地片", now=200)
        assert rid == rid2 and created is True and created2 is False
        conn = store.connect(readonly=True)
        rows = conn.execute("SELECT * FROM re0_resource").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["state"] == "candidate" and row["unlock_points"] == 4 and row["last_seen_at"] == 200 and row["created_at"] == 100
        assert row["provider_code"] == "quark" and row["resource_url_ciphertext"] is None
        assert store.fernet.decrypt(row["slug_ciphertext"]).decode() == "fixture-slug-1"
        assert "fixture-slug-1" not in row["slug_hash"]
        assert json.loads(row["spec_json"])["quality"] == "1080p"
        conn.close()

    def test_unlocked_item_with_payload_is_materialised_once(self, store, tmp_path):
        media_id = _media(store)
        item = re0_sync.normalize_item({"slug": "fixture-slug-2", "pan_type": "115", "video_resolution": ["4K"], "is_unlocked": True,
                                        "url": "https://115.com/s/swfakeunlocked1", "access_code": "ab12"}, salt="s")
        rid, _ = re0_sync.upsert_resource(store, "movie", 555, item, media_id=media_id, media_title="本地片", now=100)
        outcome = re0_sync.materialize(store, rid, item["payload_url"], item["payload_access_code"], media_id=media_id, media_title="本地片", now=101)
        assert outcome["relation"] == "materialized_unlocked" and outcome["created"] is True
        again = re0_sync.materialize(store, rid, item["payload_url"], item["payload_access_code"], media_id=media_id, media_title="本地片", now=102)
        assert again["resource_link_id"] == outcome["resource_link_id"] and again["created"] is False
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0] == 1
        link = dict(conn.execute("SELECT * FROM resource_link").fetchone())
        assert link["provider"] == "115" and link["url_plain"] is None and link["access_code_plain"] is None
        assert store.fernet.decrypt(link["url_ciphertext"]).decode() == "https://115.com/s/swfakeunlocked1"
        assert store.fernet.decrypt(link["access_code_ciphertext"]).decode() == "ab12" and link["has_access_code"] == 1
        res = dict(conn.execute("SELECT * FROM re0_resource WHERE id=?", (rid,)).fetchone())
        assert res["state"] == "unlocked" and res["group_id"] == link["group_id"] and res["unlocked_at"] == 101
        assert conn.execute("SELECT COUNT(*) FROM re0_resource_link WHERE re0_resource_id=? AND relation='materialized_unlocked'", (rid,)).fetchone()[0] == 1
        group = dict(conn.execute("SELECT * FROM resource_group WHERE id=?", (link["group_id"],)).fetchone())
        assert group["media_id"] == media_id and group["quality"] == "2160p"
        conn.close()

    def test_materialize_same_url_as_local_only_associates(self, store):
        media_id = _media(store)
        group = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-old", display_title="旧组"))
        link_id, rec = _local_link(store, group, url="https://cloud.189.cn/t/fixtureSame1")
        conn = store.connect(readonly=True); before = dict(conn.execute("SELECT * FROM resource_link").fetchone()); conn.close()
        item = re0_sync.normalize_item({"slug": "fixture-slug-3", "pan_type": "189", "is_unlocked": True, "url": "https://cloud.189.cn/t/fixtureSame1"}, salt="s")
        rid, _ = re0_sync.upsert_resource(store, "movie", 555, item, media_id=media_id, media_title="本地片", now=100)
        outcome = re0_sync.materialize(store, rid, item["payload_url"], None, media_id=media_id, media_title="本地片", now=101)
        assert outcome["relation"] == "same_local" and outcome["resource_link_id"] == link_id and outcome["created"] is False
        conn = store.connect(readonly=True)
        assert dict(conn.execute("SELECT * FROM resource_link").fetchone()) == before
        assert conn.execute("SELECT COUNT(*) FROM resource_group").fetchone()[0] == 1
        res = dict(conn.execute("SELECT state, group_id FROM re0_resource WHERE id=?", (rid,)).fetchone())
        assert res["state"] == "linked_local" and res["group_id"] == group
        conn.close()

    def test_record_items_marks_unlocked_without_payload_and_never_unlocks(self, store):
        media_id = _media(store)
        items = [
            {"slug": "fixture-a", "pan_type": "115", "is_unlocked": True},
            {"slug": "fixture-b", "pan_type": "quark", "is_unlocked": False, "unlock_points": 0},
            {"slug": "", "pan_type": "quark"},
            {"slug": "fixture-c", "pan_type": "pikPak", "is_unlocked": False, "unlock_points": 9},
        ]
        report = re0_sync.record_items(store, "movie", 555, items, media_id=media_id, media_title="本地片", salt="s", now=100)
        assert report == {"remote_items": 4, "valid_items": 3, "invalid_items": 1, "new": 3, "seen": 0, "materialized": 0,
                          "already_unlocked_no_payload": 1, "provider_unmapped": 1, "linked_local": 0}
        conn = store.connect(readonly=True)
        states = {store.fernet.decrypt(r["slug_ciphertext"]).decode(): (r["state"], r["last_error_class"], r["provider_code"]) for r in conn.execute("SELECT * FROM re0_resource")}
        conn.close()
        assert states["fixture-a"] == ("already_unlocked", "already_unlocked_no_payload", "115")
        assert states["fixture-b"] == ("candidate", None, "quark")
        assert states["fixture-c"] == ("candidate", "provider_unmapped", "unknown")

    def test_projection_upsert_keeps_single_row_and_merges_metadata(self, store):
        re0_sync.upsert_projection(store, "tv", 777, title="远端剧", year=2026, overview="", poster_path="/p1.jpg", backdrop_path=None,
                                   ratings={"tmdb": {"score": 7.1, "votes": 10}}, now=100)
        re0_sync.upsert_projection(store, "tv", 777, title="远端剧", year=2026, overview="简介来了", poster_path=None, backdrop_path="/b.jpg",
                                   ratings={}, now=200)
        conn = store.connect(readonly=True)
        rows = [dict(r) for r in conn.execute("SELECT * FROM re0_media_projection")]
        conn.close()
        assert len(rows) == 1
        row = rows[0]
        assert row["poster_path"] == "/p1.jpg" and row["backdrop_path"] == "/b.jpg" and row["overview"] == "简介来了"
        assert json.loads(row["ratings_json"])["tmdb"]["score"] == 7.1
        assert row["metadata_status"] in ("pending", "partial") and row["created_at"] == 100 and row["last_seen_at"] == 200


# ---------------------------------------------------------------------------
# Projection metadata enrichment (TMDB details via the app's own client),
# run as an extra step of the existing BackgroundEnricher round.
# ---------------------------------------------------------------------------

import library_tmdb  # noqa: E402


class _Entry:
    def __init__(self, status, payload=None, error_class=None, retry_after=None):
        self.status = status; self.payload = payload; self.error_class = error_class; self.retry_after = retry_after


class FakeTmdbClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes; self.calls = []
    def details(self, kind, tmdb_id, *, language="zh-CN", append_to_response=None):
        self.calls.append((kind, tmdb_id, append_to_response))
        outcome = self.outcomes.get((kind, tmdb_id))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _detail(title="远端片", overview="完整简介", poster="/p.jpg", backdrop="/b.jpg", score=8.1, votes=321, imdb="tt0000001", date="2024-05-01"):
    return _Entry("ok", [{"title": title, "original_title": "Remote Film", "overview": overview, "poster_path": poster, "backdrop_path": backdrop,
                          "vote_average": score, "vote_count": votes, "release_date": date, "external_ids": {"imdb_id": imdb}}])


class TestProjectionEnrichment:
    def _seed(self, store, tmdb_id=999, **over):
        kwargs = dict(title="远端片", year=2024, overview=None, poster_path="/p.jpg", backdrop_path=None, ratings={}, now=100)
        kwargs.update(over)
        re0_sync.upsert_projection(store, "movie", tmdb_id, **kwargs)

    def _row(self, store, tmdb_id=999):
        conn = store.connect(readonly=True)
        try:
            return dict(conn.execute("SELECT * FROM re0_media_projection WHERE tmdb_id=?", (tmdb_id,)).fetchone())
        finally:
            conn.close()

    def test_pending_projection_is_completed_from_details(self, store):
        self._seed(store)
        client = FakeTmdbClient({("movie", 999): _detail()})
        report = re0_sync.enrich_projections(store, client, limit=5, now=1000)
        assert report == {"selected": 1, "completed": 1, "retryable": 0, "failed": 0, "budget_exhausted": False}
        row = self._row(store)
        assert row["metadata_status"] == "complete" and row["ratings_status"] == "complete"
        assert row["overview"] == "完整简介" and row["backdrop_path"] == "/b.jpg" and row["original_title"] == "Remote Film"
        assert json.loads(row["ratings_json"])["tmdb"] == {"score": 8.1, "votes": 321}
        assert row["metadata_fetched_at"] == 1000 and row["ratings_fetched_at"] == 1000
        assert client.calls == [("movie", 999, "external_ids")]

    def test_complete_and_fresh_projection_is_not_refetched(self, store):
        self._seed(store)
        client = FakeTmdbClient({("movie", 999): _detail()})
        re0_sync.enrich_projections(store, client, limit=5, now=1000)
        report = re0_sync.enrich_projections(store, client, limit=5, now=2000)
        assert report["selected"] == 0 and len(client.calls) == 1
        stale = re0_sync.enrich_projections(store, client, limit=5, now=1000 + 31 * 86400)
        assert stale["selected"] == 1 and len(client.calls) == 2

    def test_retryable_failure_keeps_old_data_and_backs_off(self, store):
        self._seed(store, overview="旧简介")
        client = FakeTmdbClient({("movie", 999): _Entry("failed_retryable", None, error_class="Timeout", retry_after=120)})
        report = re0_sync.enrich_projections(store, client, limit=5, now=1000)
        assert report["retryable"] == 1
        row = self._row(store)
        assert row["metadata_status"] == "retryable" and row["overview"] == "旧简介" and row["last_error_class"] == "Timeout"
        again = re0_sync.enrich_projections(store, client, limit=5, now=1010)
        assert again["selected"] == 0  # next_retry_at not reached
        later = re0_sync.enrich_projections(store, client, limit=5, now=1000 + 121)
        assert later["selected"] == 1

    def test_permanent_failure_marks_failed_without_loop(self, store):
        self._seed(store)
        client = FakeTmdbClient({("movie", 999): _Entry("failed_permanent", None, error_class="HTTP404")})
        re0_sync.enrich_projections(store, client, limit=5, now=1000)
        assert self._row(store)["metadata_status"] == "failed"
        assert re0_sync.enrich_projections(store, client, limit=5, now=5000)["selected"] == 0

    def test_budget_exhausted_stops_batch_and_keeps_queue(self, store):
        self._seed(store, tmdb_id=1); self._seed(store, tmdb_id=2)
        client = FakeTmdbClient({("movie", 1): library_tmdb.BudgetExhausted("budget"), ("movie", 2): _detail()})
        report = re0_sync.enrich_projections(store, client, limit=5, now=1000)
        assert report["budget_exhausted"] is True and report["completed"] == 0 and len(client.calls) == 1
        row = self._row(store, 1)
        assert row["metadata_status"] in ("pending", "partial") and row["metadata_fetched_at"] is None

    def test_imdb_rating_merged_from_local_table_when_available(self, store):
        self._seed(store)
        conn = store.connect()
        conn.execute("INSERT OR IGNORE INTO imdb_ratings(imdb_id, rating, votes, as_of) VALUES('tt0000001', 7.9, 5000, '2026-09-01')")
        conn.commit(); conn.close()
        client = FakeTmdbClient({("movie", 999): _detail()})
        re0_sync.enrich_projections(store, client, limit=5, now=1000)
        ratings = json.loads(self._row(store)["ratings_json"])
        assert ratings["imdb"]["score"] == 7.9 and ratings["imdb"]["votes"] == 5000

    def test_background_enricher_runs_extra_round_hook(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(library_tmdb, "enrich_batch", lambda *a, **k: library_tmdb.EnrichStats())
        enricher = library_tmdb.BackgroundEnricher(
            lambda: "store", lambda: "client", leader_lock_path=tmp_path / "leader.lock", run_lock_path=tmp_path / "run.lock",
            conn_factory=lambda: None, sleep=lambda s: None, round_seconds=0, idle_seconds=0,
            extra_round=lambda store, client: (calls.append((store, client)), enricher.stop()),
        )
        enricher._loop()
        assert calls == [("store", "client")]


# ---------------------------------------------------------------------------
# §5 refresh-existing backfill queue + §7 reconcile report + §12.2 run records
# ---------------------------------------------------------------------------


class FakeRe0:
    """Scripted Re0Client stand-in: ``outcomes[path]`` is a Re0Result or a
    list consumed per call; records every path."""
    def __init__(self, outcomes=None, used=0, cap=100):
        self.outcomes = outcomes or {}; self.calls = []; self.used = used; self.cap = cap
    def get(self, path, params=None):
        self.calls.append(path)
        outcome = self.outcomes.get(path)
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if outcome is None:
            outcome = re0_sync.Re0Result(True, 200, data=[])
        self.used += 1
        return outcome
    def post(self, path, json=None):
        raise AssertionError("refresh must never POST")
    def budget_status(self):
        return {"used_today": self.used, "daily_cap": self.cap, "cooldown_until": None}


def _ok(items):
    return re0_sync.Re0Result(True, 200, data=items)


def _seed_media(store, *, tmdb_id, title, media_type="movie", match_status="exact", links=1, invalid=0, seen_at=None):
    media_id = store.upsert_media(ls.MediaRecord(media_identity=f"tmdb:{media_type}:{tmdb_id}", media_type=media_type, title_zh=title,
                                                 search_key=title, tmdb_id=tmdb_id, match_status=match_status, poster_path="/p.jpg", overview="简介"))
    group_id = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-{tmdb_id}", display_title="1080p"))
    for i in range(links):
        url = f"https://pan.quark.cn/s/swfake{tmdb_id}x{i}"
        info = library_normalize.parse_link(url, None)
        h = library_normalize.canonical_hash(info.canonical)
        store.upsert_link(ls.LinkRecord(public_id=f"pub-{tmdb_id}-{i}", group_id=group_id, provider="quark", canonical_url_hash=h, url_label=info.label,
                                        url_ciphertext=store.fernet.encrypt(url.encode())))
        if i < invalid:
            store.record_link_check("quark", h, status="invalid", reason="share_cancelled", http_class="200", checked_at=1, next_check_at=2, consecutive_unknown=0)
    store.recount()
    if seen_at is not None:
        re0_sync.upsert_projection(store, media_type, tmdb_id, title=title, year=None, overview=None, poster_path=None, backdrop_path=None, ratings={}, now=seen_at, local_media_id=media_id)
        conn = store.connect(); conn.execute("UPDATE re0_media_projection SET last_fetched_at=? WHERE tmdb_id=?", (seen_at, tmdb_id)); conn.commit(); conn.close()
    return media_id


class TestRefreshExisting:
    def test_queue_priority_and_cursor(self, store):
        a = _seed_media(store, tmdb_id=1, title="甲", links=2, invalid=2)         # exact + mostly invalid -> first
        b = _seed_media(store, tmdb_id=2, title="乙", links=1)                    # exact, never synced
        c = _seed_media(store, tmdb_id=3, title="丙", match_status="needs_review")  # confirmed-looking review row -> after exact
        d = _seed_media(store, tmdb_id=4, title="丁", links=1, seen_at=990)       # synced recently -> skipped
        _seed_media(store, tmdb_id=None, title="戊")                                # no tmdb id -> never
        conn = store.connect(readonly=True)
        queue = [r["id"] for r in re0_sync.select_refresh_queue(conn, now=1000, limit=10, ttl=3600, cursor=0)]
        conn.close()
        assert queue == [a, b, c]
        conn = store.connect(readonly=True)
        resumed = [r["id"] for r in re0_sync.select_refresh_queue(conn, now=1000, limit=10, ttl=3600, cursor=b)]
        conn.close()
        assert resumed == [c]

    def test_refresh_existing_projects_and_records_run(self, store):
        a = _seed_media(store, tmdb_id=1, title="甲", links=2, invalid=2)
        b = _seed_media(store, tmdb_id=2, title="乙")
        client = FakeRe0({"/api/open/resources/movie/1": _ok([{"slug": "s-1", "pan_type": "115", "video_resolution": ["4K"]}]),
                          "/api/open/resources/movie/2": _ok([])})
        report = re0_sync.refresh_existing(store, client, limit=10, max_requests=10, salt="s", now=1000, ttl=3600)
        assert report["phase"] == "refresh-existing" and report["status"] == "completed"
        assert report["requested"] == 2 and report["succeeded"] == 2 and report["failed"] == 0 and report["skipped"] == 0
        assert report["candidates_new"] == 1 and report["unlocked"] == 0 and report["error_class"] is None
        assert client.calls == ["/api/open/resources/movie/1", "/api/open/resources/movie/2"]
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_resource").fetchone()[0] == 1
        assert conn.execute("SELECT media_id FROM re0_resource").fetchone()[0] == a
        proj = conn.execute("SELECT local_media_id, last_fetched_at FROM re0_media_projection WHERE tmdb_id=2").fetchone()
        assert proj["local_media_id"] == b and proj["last_fetched_at"] == 1000
        run = dict(conn.execute("SELECT * FROM re0_sync_run ORDER BY id DESC LIMIT 1").fetchone())
        assert run["phase"] == "refresh-existing" and run["status"] == "completed" and run["succeeded"] == 2 and run["unlocked"] == 0
        assert re0_sync.state_get(conn, "refresh_cursor") == "0"  # pass completed -> cursor reset
        conn.close()
        again = re0_sync.refresh_existing(store, client, limit=10, max_requests=10, salt="s", now=1100, ttl=3600)
        assert again["requested"] == 0 and len(client.calls) == 2  # within ttl nothing is re-queried

    def test_refresh_stops_on_rate_limit_and_resumes_from_cursor(self, store):
        a = _seed_media(store, tmdb_id=1, title="甲")
        b = _seed_media(store, tmdb_id=2, title="乙")
        c = _seed_media(store, tmdb_id=3, title="丙")
        client = FakeRe0({"/api/open/resources/movie/2": re0_sync.Re0Result(False, 429, error_class="rate_limited", retry_after=60, retryable=True)})
        report = re0_sync.refresh_existing(store, client, limit=10, max_requests=10, salt="s", now=1000, ttl=3600)
        assert report["status"] == "stopped" and report["error_class"] == "rate_limited" and report["succeeded"] == 1 and report["failed"] == 1
        assert client.calls == ["/api/open/resources/movie/1", "/api/open/resources/movie/2"]
        conn = store.connect(readonly=True)
        assert int(re0_sync.state_get(conn, "refresh_cursor")) == a
        conn.close()
        client2 = FakeRe0()
        resumed = re0_sync.refresh_existing(store, client2, limit=10, max_requests=10, salt="s", now=2000, ttl=3600)
        assert client2.calls == ["/api/open/resources/movie/2", "/api/open/resources/movie/3"] and resumed["status"] == "completed"

    def test_refresh_honours_max_requests_and_explicit_ids(self, store):
        for i in (1, 2, 3):
            _seed_media(store, tmdb_id=i, title=f"片{i}")
        client = FakeRe0()
        report = re0_sync.refresh_existing(store, client, limit=10, max_requests=2, salt="s", now=1000, ttl=3600)
        assert report["requested"] == 2 and report["status"] == "budget_reached"
        client2 = FakeRe0()
        report2 = re0_sync.refresh_existing(store, client2, limit=10, max_requests=10, salt="s", now=1000, ttl=3600, tmdb_ids=[3], resume=False)
        assert client2.calls == ["/api/open/resources/movie/3"] and report2["requested"] == 1

    def test_dry_run_selects_without_network_or_writes(self, store):
        _seed_media(store, tmdb_id=1, title="甲")
        report = re0_sync.refresh_existing(store, None, limit=10, max_requests=10, salt="s", now=1000, ttl=3600, dry_run=True)
        assert report["dry_run"] is True and report["would_request"] == 1 and report["requested"] == 0
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_sync_run").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM re0_media_projection").fetchone()[0] == 0
        conn.close()

    def test_reconcile_report_counts_without_touching_match_status(self, store):
        _seed_media(store, tmdb_id=7, title="待复核", match_status="needs_review")
        _seed_media(store, tmdb_id=None, title="无ID", match_status="unmatched")
        _seed_media(store, tmdb_id=8, title="候选", match_status="candidate")
        dry = re0_sync.reconcile_report(store, None, limit=100, salt="s", now=1000, dry_run=True)
        assert dry == {"phase": "reconcile", "dry_run": True, "known_tmdb_ids": 2, "no_tmdb_id": 1, "queried": 0, "remote_items": 0, "new_slugs": 0,
                       "already_owned": 0, "ambiguous": 0, "rate_limited": 0}
        client = FakeRe0({"/api/open/resources/movie/7": _ok([{"slug": "s-7", "pan_type": "115", "is_unlocked": True}, {"slug": "s-7b", "pan_type": "quark"}])})
        live = re0_sync.reconcile_report(store, client, limit=100, salt="s", now=1000, dry_run=False)
        assert live["queried"] == 2 and live["remote_items"] == 2 and live["new_slugs"] == 2 and live["already_owned"] == 1
        conn = store.connect(readonly=True)
        statuses = {r[0] for r in conn.execute("SELECT match_status FROM media")}
        assert statuses == {"needs_review", "unmatched", "candidate"}  # never promoted
        conn.close()

    def test_run_status_summary(self, store):
        _seed_media(store, tmdb_id=1, title="甲")
        re0_sync.refresh_existing(store, FakeRe0(), limit=10, max_requests=10, salt="s", now=1000, ttl=3600)
        conn = store.connect(readonly=True)
        status = re0_sync.run_status(conn, now=1000)
        conn.close()
        assert status["last_run"]["phase"] == "refresh-existing" and status["last_run"]["status"] == "completed"
        assert status["cursor"] == 0 and status["queue_remaining"] == 0 and status["last_pass_completed_at"] == 1000


class TestIndexDirty:
    def test_media_created_from_projection_marks_index_dirty_and_rebuild_makes_it_searchable(self, store):
        import library_search
        library_search.build_index(store)
        re0_sync.upsert_projection(store, "movie", 999, title="远端片", year=2024, overview="简介", poster_path="/p.jpg", backdrop_path=None, ratings={}, now=100)
        media_id = re0_sync.create_media_from_projection(store, "movie", 999, now=101)
        conn = store.connect(readonly=True)
        assert re0_sync.state_get(conn, f"index_pending:{media_id}") == "1"
        assert re0_sync.state_get(conn, "index_dirty") != "1"
        conn.close()
        # In the real flow the unlock materialises a link right after the
        # media row exists; search only lists media with a live link.
        group = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-re0", display_title="4K"))
        _local_link(store, group, url="https://pan.quark.cn/s/swfakeidx999", provider="quark")
        store.recount()
        assert re0_sync.rebuild_index_if_dirty(store, charmap={}) is True
        assert re0_sync.rebuild_index_if_dirty(store, charmap={}) is False
        page = library_search.search(store, "远端片", library_search.Filters(), page=1, page_size=10)
        assert [i["media_id"] for i in page.items] == [media_id]


# ---------------------------------------------------------------------------
# §6.1 calendar projection + §9.1 next-episode CTA + §6.2 streaming-top +
# bounded discovery (RE0 resources for projections without local media)
# ---------------------------------------------------------------------------


def _cal_tv(tmdb, season, number, first_aired, title="远端剧", ep_title="第一集"):
    return {"first_aired": first_aired, "show": {"title": title, "year": 2026, "ids": {"tmdb": tmdb, "imdb": "tt0000002", "tvdb": 5},
                                                  "metadata": {"overview": "剧集简介", "poster_path": "/tv.jpg"}},
            "episode": {"season": season, "number": number, "title": ep_title, "first_aired": first_aired}}


def _cal_movie(tmdb, released, title="远端电影"):
    return {"first_aired": released, "released": released, "movie": {"title": title, "year": 2026, "ids": {"tmdb": tmdb}}}


class TestCalendar:
    def test_record_calendar_dedupes_links_local_and_projects_unknown(self, store):
        local_tv = store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv", title_zh="本地剧", search_key="本地剧", tmdb_id=70, match_status="exact"))
        items = [
            _cal_tv(70, 6, 1, "2026-09-16T03:00:00Z"),
            _cal_tv(70, 6, 1, "2026-09-16T03:00:00Z"),          # duplicate
            _cal_tv(71, 1, 3, "2026-09-18T12:00:00Z"),           # unknown show -> projection only
            _cal_movie(80, "2026-09-20T00:00:00Z"),
            {"first_aired": "2026-09-21T00:00:00Z", "show": {"title": "无ID剧", "ids": {}}, "episode": {"season": 1, "number": 1}},
        ]
        report = re0_sync.record_calendar(store, items, now=1_757_000_000)
        assert report == {"items": 5, "recorded": 3, "duplicates": 1, "no_tmdb_id": 1, "linked_local": 1, "new_projections": 2}
        conn = store.connect(readonly=True)
        rows = [dict(r) for r in conn.execute("SELECT * FROM re0_calendar_event ORDER BY first_aired")]
        assert [(r["media_type"], r["tmdb_id"], r["season"], r["episode"], r["media_id"]) for r in rows] == [("tv", 70, 6, 1, local_tv), ("tv", 71, 1, 3, None), ("movie", 80, None, None, None)]
        assert rows[0]["episode_title"] == "第一集" and json.loads(rows[0]["event_json"])["show_title"] == "远端剧"
        projections = {(r["media_type"], r["tmdb_id"]): r["title"] for r in conn.execute("SELECT media_type, tmdb_id, title FROM re0_media_projection")}
        assert projections == {("tv", 71): "远端剧", ("movie", 80): "远端电影"}
        assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1  # never creates media rows
        conn.close()
        again = re0_sync.record_calendar(store, items, now=1_757_000_100)
        assert again["recorded"] == 0 and again["duplicates"] == 4

    def test_next_event_for_uses_shanghai_day_and_detects_new_season(self, store):
        store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv", title_zh="本地剧", search_key="本地剧", tmdb_id=70, match_status="exact"))
        re0_sync.record_calendar(store, [_cal_tv(70, 6, 1, "2026-09-16T03:00:00Z"), _cal_tv(70, 6, 2, "2026-09-23T03:00:00Z"), _cal_tv(70, 5, 9, "2026-09-01T03:00:00Z")], now=1_757_000_000)
        conn = store.connect(readonly=True)
        now = 1_789_430_400  # 2026-09-15T00:00Z
        event = re0_sync.next_event_for(conn, "tv", 70, now=now, local_max_season=5)
        assert event["season"] == 6 and event["episode"] == 1 and event["is_new_season"] is True
        assert event["label"] == "新一季 · S06" and event["display"] == "9月16日周三 11:00" and event["first_aired"] == "2026-09-16T03:00:00Z"
        same_season = re0_sync.next_event_for(conn, "tv", 70, now=now, local_max_season=6)
        assert same_season["label"] == "下一集 S06E01" and same_season["is_new_season"] is False
        assert re0_sync.next_event_for(conn, "tv", 70, now=1_800_000_000, local_max_season=6) is None  # all past
        assert re0_sync.next_event_for(conn, "tv", 999, now=now, local_max_season=None) is None
        conn.close()

    def test_discover_calendar_once_per_day(self, store):
        client = FakeRe0({"/api/open/calendar": _ok({"items": [_cal_tv(71, 1, 1, "2026-09-18T12:00:00Z")], "days": 31})})
        first = re0_sync.discover_calendar(store, client, days=31, now=1_757_000_000)
        second = re0_sync.discover_calendar(store, client, days=31, now=1_757_000_500)
        assert first["fetched"] is True and first["recorded"] == 1 and second["fetched"] is False and second["skipped_reason"] == "already_today"
        assert client.calls == ["/api/open/calendar"]
        failing = FakeRe0({"/api/open/calendar": re0_sync.Re0Result(False, 503, error_class="upstream_5xx", retryable=True)})
        report = re0_sync.discover_calendar(store, failing, days=31, now=1_757_000_000 + 86400)
        assert report["fetched"] is False and report["error_class"] == "upstream_5xx"
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_calendar_event").fetchone()[0] == 1  # previous data kept
        conn.close()


class TestStreamingTop:
    def test_record_streaming_top_projects_only_tmdb_matched(self, store):
        local = store.upsert_media(ls.MediaRecord(media_identity="tmdb:movie:90", media_type="movie", title_zh="本地榜单片", search_key="本地榜单片", tmdb_id=90, match_status="exact"))
        items = [
            {"rank": 1, "source_title": "Local Hit", "tmdb_id": 90, "media_type": "movie", "title": "本地榜单片", "year": 2026, "local_media": {"id": 1, "slug": "x", "href": "/m/x"}},
            {"rank": 2, "source_title": "Remote Hit", "tmdb_id": 91, "media_type": "movie", "title": "远端榜单片", "year": 2025, "overview": "榜单简介", "poster_url": "https://image.tmdb.org/t/p/w500/poster91.jpg"},
            {"rank": 3, "source_title": "No Match", "tmdb_id": None, "media_type": "movie", "title": "未匹配"},
        ]
        report = re0_sync.record_streaming_top(store, items, now=100)
        assert report == {"items": 3, "projected": 2, "linked_local": 1, "no_tmdb_id": 1}
        conn = store.connect(readonly=True)
        rows = {r["tmdb_id"]: dict(r) for r in conn.execute("SELECT * FROM re0_media_projection")}
        assert rows[90]["local_media_id"] == local and rows[91]["local_media_id"] is None and rows[91]["poster_path"] == "/poster91.jpg"
        assert rows[91]["overview"] == "榜单简介"
        conn.close()

    def test_discover_top_uses_whitelist_once_per_day(self, store):
        client = FakeRe0({"/api/open/streaming-top": _ok({"items": [{"rank": 1, "source_title": "A", "tmdb_id": 91, "media_type": "tv", "title": "榜单剧", "year": 2026}]})})
        report = re0_sync.discover_top(store, client, sources=["netflix:US:tv", "bad-source"], now=100)
        assert report["sources"] == 1 and report["invalid_sources"] == 1 and report["projected"] == 1 and client.calls == ["/api/open/streaming-top"]
        again = re0_sync.discover_top(store, client, sources=["netflix:US:tv"], now=200)
        assert again["sources"] == 0 and again["skipped_today"] == 1 and len(client.calls) == 1


class TestDiscoverBounded:
    def test_bounded_discovery_queries_projections_without_local_media(self, store):
        store.upsert_media(ls.MediaRecord(media_identity="tmdb:movie:90", media_type="movie", title_zh="本地", search_key="本地", tmdb_id=90, match_status="exact"))
        re0_sync.upsert_projection(store, "movie", 90, title="本地", year=None, overview=None, poster_path=None, backdrop_path=None, ratings={}, now=50, local_media_id=1)
        re0_sync.upsert_projection(store, "movie", 91, title="远端一", year=None, overview=None, poster_path=None, backdrop_path=None, ratings={}, now=60)
        re0_sync.upsert_projection(store, "tv", 92, title="远端二", year=None, overview=None, poster_path=None, backdrop_path=None, ratings={}, now=70)
        client = FakeRe0({"/api/open/resources/movie/91": _ok([{"slug": "s-91", "pan_type": "quark"}]), "/api/open/resources/tv/92": _ok([])})
        report = re0_sync.discover_bounded(store, client, limit=10, max_requests=10, salt="s", now=100)
        assert report["requested"] == 2 and report["candidates_new"] == 1 and report["status"] == "completed"
        assert sorted(client.calls) == ["/api/open/resources/movie/91", "/api/open/resources/tv/92"]
        again = re0_sync.discover_bounded(store, client, limit=10, max_requests=10, salt="s", now=200)
        assert again["requested"] == 0
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1 and conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0] == 0
        conn.close()


# ---------------------------------------------------------------------------
# §9.2 tv-follow packs: read-only projection, locked items, explicit unlock
# ---------------------------------------------------------------------------


def _pack(slug, *, tv_id="tv-internal-9", tmdb_id=None, unlocked=False, completed=False, points=30, title="追更包甲"):
    pack = {"slug": slug, "title": title, "tv_id": tv_id, "is_unlocked": unlocked, "is_owner": False, "is_completed": completed,
            "unlock_points": points, "preview_items": [{"episode_label": "S02E10", "season": 2, "episode_start": 10, "episode_end": 10, "resolution": "1080p"},
                                                       {"episode_label": "S02E09", "season": 2, "episode_start": 9, "episode_end": 9}],
            "latest_label": "S02E10", "item_count": 2, "url": "https://re0.me/follow/leak"}
    if tmdb_id is not None:
        pack["tmdb_id"] = tmdb_id
    return pack


class TestTvFollow:
    def test_record_packs_keeps_preview_only_and_maps_tv_id(self, store):
        media_id = store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv", title_zh="本地剧", search_key="本地剧", tmdb_id=70, match_status="exact"))
        report = re0_sync.record_packs(store, [_pack("pack-a", tmdb_id=70), _pack("pack-b", tv_id="tv-internal-8"), "junk", {"slug": ""}], salt="s", tmdb_id=70, now=100)
        assert report == {"packs": 4, "recorded": 2, "invalid": 2, "mapped": 1, "mapping_unknown": 1}
        conn = store.connect(readonly=True)
        rows = {store.fernet.decrypt(r["slug_ciphertext"]).decode(): dict(r) for r in conn.execute("SELECT * FROM re0_tv_follow_pack")}
        assert rows["pack-a"]["tmdb_id"] == 70 and rows["pack-a"]["media_id"] == media_id and rows["pack-a"]["re0_tv_id"] == "tv-internal-9"
        assert rows["pack-a"]["is_unlocked"] == 0 and rows["pack-a"]["unlock_points"] == 30
        preview = json.loads(rows["pack-a"]["preview_json"])
        assert preview["latest_label"] == "S02E10" and preview["item_count"] == 2 and [i["episode_label"] for i in preview["items"]] == ["S02E10", "S02E09"]
        assert "url" not in json.dumps(preview) and "leak" not in rows["pack-a"]["preview_json"]
        # pack-b carries a different tv_id and no tmdb_id -> never guessed from the query context
        assert rows["pack-b"]["tmdb_id"] is None and rows["pack-b"]["media_id"] is None
        conn.close()
        again = re0_sync.record_packs(store, [_pack("pack-a", tmdb_id=70)], salt="s", tmdb_id=70, now=200)
        assert again["recorded"] == 1 and conn is not None
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_tv_follow_pack").fetchone()[0] == 2
        conn.close()

    def test_query_packs_uses_tmdb_as_tv_id_and_records_mapping_evidence(self, store):
        store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv", title_zh="本地剧", search_key="本地剧", tmdb_id=70, match_status="exact"))
        client = FakeRe0({"/api/open/tv-follow/packs": _ok({"items": [_pack("pack-a", tv_id="70")]})})
        report = re0_sync.query_packs_for_tmdb(store, client, tmdb_id=70, salt="s", now=100)
        assert report["queried"] is True and report["recorded"] == 1 and report["mapped"] == 1
        conn = store.connect(readonly=True)
        assert re0_sync.state_get(conn, "tv_follow_checked:70") == "100"
        conn.close()
        report2 = re0_sync.query_packs_for_tmdb(store, client, tmdb_id=70, salt="s", now=200)
        assert report2["queried"] is False and len(client.calls) == 1  # per-tv cooldown

    def test_locked_items_are_marked_and_not_retried(self, store):
        store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv", title_zh="本地剧", search_key="本地剧", tmdb_id=70, match_status="exact"))
        re0_sync.record_packs(store, [_pack("pack-a", tmdb_id=70)], salt="s", tmdb_id=70, now=100)
        client = FakeRe0({"/api/open/tv-follow/packs/pack-a/items": re0_sync.Re0Result(False, 403, error_class="scope_denied", code="not_unlocked")})
        first = re0_sync.fetch_pack_items(store, client, slug="pack-a", salt="s", now=100)
        assert first["status"] == "locked" and len(client.calls) == 1
        second = re0_sync.fetch_pack_items(store, client, slug="pack-a", salt="s", now=101)
        assert second["status"] == "locked" and len(client.calls) == 1

    def test_unlocked_pack_items_materialise_insert_only(self, store):
        media_id = store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv", title_zh="本地剧", search_key="本地剧", tmdb_id=70, match_status="exact"))
        re0_sync.record_packs(store, [_pack("pack-a", tmdb_id=70, unlocked=True)], salt="s", tmdb_id=70, now=100)
        items = [
            {"id": 501, "episode_label": "S02E10", "season": 2, "episode_start": 10, "episode_end": 10, "url": "https://pan.quark.cn/s/swfakepack10", "access_code": "", "resolution": "1080p", "source": "WEB-DL"},
            {"id": 502, "episode_label": "S02E09", "season": 2, "episode_start": 9, "episode_end": 9, "url": "ed2k://|file|Ep09.mkv|1|" + "CD" * 16 + "|/"},
            {"id": 503, "episode_label": "S02E08", "season": 2, "url": "not a url"},
        ]
        client = FakeRe0({"/api/open/tv-follow/packs/pack-a/items": _ok({"items": items})})
        report = re0_sync.fetch_pack_items(store, client, slug="pack-a", salt="s", now=100)
        assert report["status"] == "ok" and report["items"] == 3 and report["materialized"] == 2 and report["invalid"] == 1
        conn = store.connect(readonly=True)
        links = conn.execute("SELECT provider, url_plain FROM resource_link ORDER BY id").fetchall()
        assert [l["provider"] for l in links] == ["quark", "ed2k"] and all(l["url_plain"] is None for l in links)
        rows = {r["re0_item_id"]: dict(r) for r in conn.execute("SELECT * FROM re0_tv_follow_item")}
        assert rows["501"]["unlocked"] == 1 and rows["501"]["resource_link_id"] and rows["501"]["label"] == "S02E10"
        assert "url" not in rows["501"]["item_json"]
        assert conn.execute("SELECT COUNT(*) FROM resource_group WHERE media_id=?", (media_id,)).fetchone()[0] >= 1
        conn.close()
        again = re0_sync.fetch_pack_items(store, client, slug="pack-a", salt="s", now=200)
        assert again["materialized"] == 0 and again["seen"] == 2
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0] == 2
        conn.close()

    def test_sync_my_packs_records_unlocked_packs(self, store):
        client = FakeRe0({"/api/open/tv-follow/my": _ok({"items": [_pack("pack-m", tmdb_id=70, unlocked=True)], "unread_count": 3})})
        report = re0_sync.sync_my_packs(store, client, salt="s", now=100)
        assert report["recorded"] == 1 and report["unread_count"] == 3
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT is_unlocked FROM re0_tv_follow_pack").fetchone()[0] == 1
        conn.close()

    def test_follow_rows_for_detail_have_no_slug(self, store):
        media_id = store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv", title_zh="本地剧", search_key="本地剧", tmdb_id=70, match_status="exact"))
        re0_sync.record_packs(store, [_pack("pack-a", tmdb_id=70)], salt="s", tmdb_id=70, now=100)
        conn = store.connect(readonly=True)
        rows = re0_sync.follow_rows(conn, tmdb_id=70)
        conn.close()
        assert len(rows) == 1
        row = rows[0]
        assert row["title"] == "追更包甲" and row["latest_label"] == "S02E10" and row["is_unlocked"] is False and row["unlock_points"] == 30
        assert row["item_count"] == 2 and row["ref"] == re0_sync.slug_hash("pack-a", "s")[:16]
        assert "pack-a" not in json.dumps(row, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Round 26: read-only file-list preview (`/api/open/resources/file-list/:slug`)
# -- tells a 合集包 from a single episode without unlocking or spending points.
# ---------------------------------------------------------------------------

FILE_LIST_URL = "https://re0.test/api/open/resources/file-list/fixture-slug-fl"


def _seed_resource(store, *, slug="fixture-slug-fl", provider="115", tmdb_id=555):
    media_id = _media(store, tmdb_id=tmdb_id)
    item = re0_sync.normalize_item({"slug": slug, "pan_type": provider, "video_resolution": ["4K"], "source": ["WEB-DL"],
                                    "is_unlocked": False, "unlock_points": 5}, salt="s")
    rid, _ = re0_sync.upsert_resource(store, "movie", tmdb_id, item, media_id=media_id, media_title="本地片", now=100)
    return rid


class TestFileListPreview:
    def test_preview_returns_share_title_count_and_files_without_any_link(self, store, tmp_path):
        rid = _seed_resource(store)
        req = FakeRequester().route("GET", FILE_LIST_URL, FakeResponse(200, {"success": True, "data": {
            "provider": "115", "list_type": "folder", "share_title": "本地片 全4集 合集 https://re0.me/r/leak",
            "file_count": 4, "result_type": "listing", "resource_validate_status": "valid", "resource_validate_message": "",
            "files": [{"name": f"E0{i}.mkv", "path": f"/本地片/E0{i}.mkv", "size": 1073741824 * i, "extension": "mkv"} for i in range(1, 5)],
        }}))
        client, _ = _client(tmp_path, req)
        out = re0_sync.fetch_file_list(store, client, resource_id=rid, now=200)
        assert out["ok"] is True and out["resource_id"] == rid and out["provider"] == "115"
        assert out["file_count"] == 4 and out["list_type"] == "folder" and out["result_type"] == "listing"
        assert out["validate_status"] == "valid" and out["truncated"] is False
        assert [f["name"] for f in out["files"]] == ["E01.mkv", "E02.mkv", "E03.mkv", "E04.mkv"]
        assert out["files"][1]["size"] == 2147483648 and out["files"][0]["path"] == "/本地片/E01.mkv"
        # The share title keeps its words but never carries a link.
        assert "合集" in out["share_title"] and "re0.me" not in out["share_title"] and "http" not in out["share_title"]
        blob = json.dumps(out, ensure_ascii=False)
        for needle in ("fixture-slug-fl", "re0.me/r/", "access-fixture", "app-secret-fixture"):
            assert needle not in blob, needle
        assert [c["url"] for c in req.calls] == [FILE_LIST_URL] and req.calls[0]["method"] == "GET"

    def test_preview_caps_the_file_list_and_marks_it_truncated(self, store, tmp_path):
        rid = _seed_resource(store)
        files = [{"name": f"E{i:03d}.mkv", "path": f"/x/E{i:03d}.mkv", "size": 1, "extension": "mkv"} for i in range(300)]
        req = FakeRequester().route("GET", FILE_LIST_URL, FakeResponse(200, {"success": True, "data": {
            "provider": "115", "share_title": "长剧", "file_count": 300, "files": files}}))
        client, _ = _client(tmp_path, req)
        out = re0_sync.fetch_file_list(store, client, resource_id=rid, now=200)
        assert out["ok"] is True and out["file_count"] == 300
        assert len(out["files"]) == re0_sync.FILE_LIST_MAX_FILES and out["truncated"] is True

    def test_preview_reports_an_invalid_resource_and_an_upstream_refusal(self, store, tmp_path):
        rid = _seed_resource(store)
        req = FakeRequester().route("GET", FILE_LIST_URL, FakeResponse(200, {"success": True, "data": {
            "provider": "115", "result_type": "validation", "files": [], "resource_validate_status": "invalid",
            "resource_validate_message": "分享已失效"}}))
        client, _ = _client(tmp_path, req)
        out = re0_sync.fetch_file_list(store, client, resource_id=rid, now=200)
        assert out["ok"] is True and out["files"] == [] and out["result_type"] == "validation"
        assert out["validate_status"] == "invalid" and out["validate_message"] == "分享已失效"

        req2 = FakeRequester().route("GET", FILE_LIST_URL, FakeResponse(403, {"success": False, "code": "USER_LEVEL_REQUIRED",
                                                                             "message": "需要有效 V 用户"}))
        client2, _ = _client(tmp_path, req2)
        out2 = re0_sync.fetch_file_list(store, client2, resource_id=rid, now=200)
        assert out2["ok"] is False and out2["error_class"] in ("user_level_denied", "scope_denied")
        assert out2["code"] == "USER_LEVEL_REQUIRED" and "V" in out2["message"]

    def test_preview_refuses_an_unknown_resource_and_never_calls_re0(self, store, tmp_path):
        req = FakeRequester()
        client, _ = _client(tmp_path, req)
        out = re0_sync.fetch_file_list(store, client, resource_id=424242, now=200)
        assert out["ok"] is False and out["error_class"] == "unknown_resource" and req.calls == []


# ---------------------------------------------------------------------------
# Round 27: a pan_type that arrives as a JSON number must map like the string
# form (RE0's 115 / 189 / 139 codes are digits), and a read-only probe that
# compares the raw upstream items against what we stored.
# ---------------------------------------------------------------------------


class TestPanTypeNumbers:
    def test_numeric_pan_type_maps_like_its_string_form(self):
        assert re0_sync.map_pan_type(115) == "115"
        assert re0_sync.map_pan_type(189) == "tianyicloud"
        assert re0_sync.map_pan_type(139) == "139cloud"
        assert re0_sync.map_pan_type("115") == "115"

    def test_unmapped_or_nonsensical_pan_types_stay_unknown(self):
        for raw in (None, True, False, 1.5, [], {}, "", "xunLei", 999, "115 "):
            assert re0_sync.map_pan_type(raw) == "unknown", raw

    def test_normalize_item_keeps_the_numeric_code_verbatim(self):
        item = re0_sync.normalize_item({"slug": "s-num", "pan_type": 115, "is_unlocked": False}, salt="s")
        assert item["provider_code"] == "115" and item["upstream_pan_type"] == "115"


PROBE_URL = "https://re0.test/api/open/resources/movie/555"


class TestProbeResources:
    def test_probe_reports_raw_item_shape_next_to_what_was_stored(self, store, tmp_path):
        media_id = _media(store, tmdb_id=555)
        req = FakeRequester().route("GET", PROBE_URL, FakeResponse(200, {"success": True, "data": [
            {"slug": "s-115", "pan_type": 115, "title": "初恋 (2006) 蓝光原盘", "share_size": "9.3GB",
             "video_resolution": ["1080p"], "source": ["BluRay"], "is_unlocked": False, "validate_status": "valid"},
            {"slug": "s-uc", "pan_type": "uc", "is_unlocked": False},
            {"pan_type": "quark"},  # no slug -> dropped as an invalid item
        ]}))
        client, _ = _client(tmp_path, req)
        out = re0_sync.probe_resources(store, client, media_type="movie", tmdb_id=555, salt="s", now=100)
        assert out["ok"] is True and out["media_type"] == "movie" and out["tmdb_id"] == 555
        assert out["local_media_id"] == media_id and out["raw_items"] == 3 and out["invalid_items"] == 1
        first = out["items"][0]
        assert first["pan_type_raw"] == "115" and first["pan_type_json_type"] == "int" and first["provider_code"] == "115"
        assert first["title"] == "初恋 (2006) 蓝光原盘" and first["share_size"] == "9.3GB" and first["is_unlocked"] is False
        # The probe reports our own normalised codes, so a mis-normalisation is visible too.
        assert first["validate_status"] == "valid" and first["specs"] == {"resolution": "1080p", "source": "bluray"}
        second = out["items"][1]
        assert second["pan_type_raw"] == "uc" and second["pan_type_json_type"] == "str" and second["provider_code"] == "unknown"
        assert out["unmapped_pan_types"] == ["uc"]
        # Nothing was stored by the probe itself; `stored` reflects the DB as it is.
        assert out["stored"] == [] and out["stored_missing_from_upstream"] == 0
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_resource").fetchone()[0] == 0
        conn.close()
        blob = json.dumps(out, ensure_ascii=False)
        for needle in ("s-115", "s-uc", "access-fixture", "app-secret-fixture"):
            assert needle not in blob, needle

    def test_probe_shows_what_is_stored_and_what_upstream_no_longer_has(self, store, tmp_path):
        media_id = _media(store, tmdb_id=555)
        gone = re0_sync.normalize_item({"slug": "s-old", "pan_type": "quark", "is_unlocked": False}, salt="s")
        re0_sync.upsert_resource(store, "movie", 555, gone, media_id=media_id, media_title="本地片", now=50)
        req = FakeRequester().route("GET", PROBE_URL, FakeResponse(200, {"success": True, "data": [
            {"slug": "s-115", "pan_type": 115, "is_unlocked": False}]}))
        client, _ = _client(tmp_path, req)
        out = re0_sync.probe_resources(store, client, media_type="movie", tmdb_id=555, salt="s", now=100)
        assert [s["provider_code"] for s in out["stored"]] == ["quark"]
        assert out["stored"][0]["in_upstream"] is False and out["stored"][0]["has_title"] is False
        assert out["stored_missing_from_upstream"] == 1
        assert out["items"][0]["already_stored"] is False  # the 115 share we never captured

    def test_probe_passes_an_upstream_failure_through_without_writing(self, store, tmp_path):
        _media(store, tmdb_id=555)
        req = FakeRequester().route("GET", PROBE_URL, FakeResponse(429, {"success": False}, headers={"Retry-After": "30"}))
        client, _ = _client(tmp_path, req)
        out = re0_sync.probe_resources(store, client, media_type="movie", tmdb_id=555, salt="s", now=100)
        assert out["ok"] is False and out["error_class"] == "rate_limited" and out["retry_after"] == 30
        assert "items" not in out


# ---------------------------------------------------------------------------
# Round 28: old candidates written before the display fields (name/size) were
# captured must heal by themselves, and an explicitly targeted media must be
# refreshable regardless of the queue's TTL.
# ---------------------------------------------------------------------------


def _seed_stale_display_row(store, *, tmdb_id, last_seen_at, title=None, media_type="movie"):
    """One candidate for a local media, with its projection marked as fetched
    just now -- i.e. well inside the 7-day refresh TTL."""
    media_id = _media(store, tmdb_id=tmdb_id, media_type=media_type)
    item = re0_sync.normalize_item({"slug": f"s-{tmdb_id}", "pan_type": "115", "is_unlocked": False,
                                    **({"title": title} if title else {})}, salt="s")
    rid, _ = re0_sync.upsert_resource(store, media_type, tmdb_id, item, media_id=media_id, media_title="片", now=last_seen_at)
    re0_sync.upsert_projection(store, media_type, tmdb_id, title="片", year=None, overview=None, poster_path=None,
                               backdrop_path=None, ratings={}, now=last_seen_at, local_media_id=media_id)
    conn = store.connect()
    conn.execute("UPDATE re0_media_projection SET last_fetched_at=? WHERE media_type=? AND tmdb_id=?", (NOW_28, media_type, tmdb_id))
    conn.execute("UPDATE re0_resource SET last_seen_at=? WHERE id=?", (last_seen_at, rid))
    conn.commit(); conn.close()
    return media_id, rid


NOW_28 = re0_sync.DISPLAY_FIELDS_EPOCH + 3600  # an hour after the release that started storing name/size


class TestDisplayFieldBackfill:
    def test_candidates_recorded_before_the_epoch_are_queued_despite_the_ttl(self, store):
        old_media, _ = _seed_stale_display_row(store, tmdb_id=6600, last_seen_at=re0_sync.DISPLAY_FIELDS_EPOCH - 3600)
        conn = store.connect(readonly=True)
        try:
            rows = re0_sync.select_refresh_queue(conn, now=NOW_28, limit=10, ttl=re0_sync.REFRESH_TTL_SECONDS, cursor=0)
        finally:
            conn.close()
        assert [r["id"] for r in rows] == [old_media]  # inside the TTL, but its candidate predates the capture

    def test_a_candidate_seen_after_the_epoch_is_left_alone_even_without_a_name(self, store):
        # Upstream genuinely has no title for this share: it was re-fetched
        # after the capture landed, so the backfill must not loop on it.
        _seed_stale_display_row(store, tmdb_id=6601, last_seen_at=re0_sync.DISPLAY_FIELDS_EPOCH + 60)
        conn = store.connect(readonly=True)
        try:
            rows = re0_sync.select_refresh_queue(conn, now=NOW_28, limit=10, ttl=re0_sync.REFRESH_TTL_SECONDS, cursor=0)
        finally:
            conn.close()
        assert rows == []

    def test_the_backfill_terminates_once_the_media_has_been_refetched(self, store, tmp_path):
        media_id, _ = _seed_stale_display_row(store, tmdb_id=6600, last_seen_at=re0_sync.DISPLAY_FIELDS_EPOCH - 3600)
        req = FakeRequester().route("GET", "https://re0.test/api/open/resources/movie/6600", FakeResponse(200, {"success": True, "data": [
            {"slug": "s-6600", "pan_type": "115", "title": "末日地堡 (2023)", "share_size": "9GB/集", "is_unlocked": False}]}))
        client, _ = _client(tmp_path, req)
        report = re0_sync.refresh_existing(store, client, limit=10, max_requests=5, salt="s", now=NOW_28)
        assert report["requested"] == 1 and report["succeeded"] == 1 and report["candidates_new"] == 0  # same slug, updated in place
        conn = store.connect(readonly=True)
        try:
            rows = re0_sync.candidate_rows(conn, "movie", 6600)
            again = re0_sync.select_refresh_queue(conn, now=NOW_28, limit=10, ttl=re0_sync.REFRESH_TTL_SECONDS, cursor=0)
        finally:
            conn.close()
        assert [r["title"] for r in rows] == ["末日地堡 (2023)"] and [r["size"] for r in rows] == ["9GB/集"]
        assert again == []  # healed: never queued again

    def test_explicit_media_ids_ignore_the_queue_ttl(self, store):
        media_id, _ = _seed_stale_display_row(store, tmdb_id=6602, last_seen_at=NOW_28, title="有名字")
        conn = store.connect(readonly=True)
        try:
            # Nothing is due on its own -- fresh projection, candidate seen after the epoch.
            assert re0_sync.select_refresh_queue(conn, now=NOW_28, limit=10, ttl=re0_sync.REFRESH_TTL_SECONDS, cursor=0) == []
            by_media = re0_sync.select_refresh_queue(conn, now=NOW_28, limit=10, ttl=re0_sync.REFRESH_TTL_SECONDS, cursor=0,
                                                     media_ids=[media_id])
            by_tmdb = re0_sync.select_refresh_queue(conn, now=NOW_28, limit=10, ttl=re0_sync.REFRESH_TTL_SECONDS, cursor=0,
                                                    tmdb_ids=[6602])
        finally:
            conn.close()
        assert [r["id"] for r in by_media] == [media_id] and [r["id"] for r in by_tmdb] == [media_id]

    def test_the_backfill_touches_no_link_and_never_unlocks(self, store, tmp_path):
        """Codex's constraint 3: a name/size backfill updates the candidate's
        display fields only -- existing links stay exactly as they are, and
        the unlock endpoint is never called."""
        media_id, rid = _seed_stale_display_row(store, tmdb_id=6603, last_seen_at=re0_sync.DISPLAY_FIELDS_EPOCH - 3600)
        group_id = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint="fp", display_title="1080p"))
        link_id, _ = _local_link(store, group_id)
        conn = store.connect()
        conn.execute("INSERT INTO re0_resource_link(re0_resource_id, resource_link_id, relation, linked_at) VALUES(?,?,?,?)",
                     (rid, link_id, "materialized_unlocked", 100))
        conn.execute("UPDATE re0_resource SET state='unlocked' WHERE id=?", (rid,))
        conn.commit(); conn.close()
        before = _links_snapshot(store)
        req = FakeRequester().route("GET", "https://re0.test/api/open/resources/movie/6603", FakeResponse(200, {"success": True, "data": [
            {"slug": "s-6603", "pan_type": "115", "title": "补上的名称", "share_size": "9GB", "is_unlocked": False}]}))
        client, _ = _client(tmp_path, req)
        report = re0_sync.refresh_existing(store, client, limit=10, max_requests=5, salt="s", now=NOW_28)
        assert report["succeeded"] == 1 and report["materialized"] == 0
        assert _links_snapshot(store) == before  # not one link row changed
        conn = store.connect(readonly=True)
        try:
            row = conn.execute("SELECT state FROM re0_resource WHERE id=?", (rid,)).fetchone()
            rows = re0_sync.candidate_rows(conn, "movie", 6603)
        finally:
            conn.close()
        assert row["state"] == "unlocked"  # an unlocked candidate is never downgraded
        assert [r["title"] for r in rows] == ["补上的名称"] and [r["size"] for r in rows] == ["9GB"]
        assert all("unlock" not in c["url"] for c in req.calls)


def _links_snapshot(store):
    conn = store.connect(readonly=True)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT id, group_id, provider, canonical_url_hash, url_label, url_ciphertext, deleted_at_source FROM resource_link ORDER BY id")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4.2 / 5.2: the on-demand file preview. One upstream call per candidate per
# user action, cached; never an unlock, never a link, never a slug in the result.
# ---------------------------------------------------------------------------

PREVIEW_URL = "https://re0.test/api/open/resources/file-list/s-prev"


def _preview_resource(store, *, slug="s-prev", provider="115", tmdb_id=555, remark=None):
    media_id = _media(store, tmdb_id=tmdb_id)
    item = re0_sync.normalize_item({"slug": slug, "pan_type": provider, "is_unlocked": False,
                                    **({"remark": remark} if remark else {})}, salt="s")
    rid, _ = re0_sync.upsert_resource(store, "movie", tmdb_id, item, media_id=media_id, media_title="片", now=100)
    return rid


def _preview_ok(files=None, **over):
    if files is None:
        files = [{"name": f"Silo.S03E{i:02d}.mkv", "path": f"/Silo/S03/E{i:02d}.mkv", "size": 1073741824, "extension": "mkv"}
                 for i in range(1, 11)]
    body = {"provider": "115", "list_type": "folder", "share_title": "末日地堡 S03", "file_count": len(files),
            "result_type": "listing", "resource_validate_status": "valid", "files": files}
    body.update(over)
    return FakeResponse(200, {"success": True, "data": body})


class TestFilePreview:
    def test_a_preview_is_fetched_once_then_served_from_cache(self, store, tmp_path):
        rid = _preview_resource(store)
        req = FakeRequester().route("GET", PREVIEW_URL, _preview_ok())
        client, _ = _client(tmp_path, req)
        first = re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)
        assert first["status"] == "ready" and first["cached"] is False and first["file_count"] == 10
        assert [f["name"] for f in first["files"]][:2] == ["Silo.S03E01.mkv", "Silo.S03E02.mkv"]
        assert first["files"][0]["size"] == 1073741824 and first["files"][0]["path"] == "/Silo/S03/E01.mkv"
        # The composition is inferred from the real file names.
        assert first["composition"]["kind"] == "episode_range" and first["composition"]["display"] == "S03 · 第 1–10 集"
        assert first["composition"]["confidence"] == "file_inferred"
        second = re0_sync.file_preview(store, client, resource_id=rid, now=1_000_060)
        assert second["status"] == "ready" and second["cached"] is True and second["file_count"] == 10
        assert len(req.calls) == 1  # 12h cache
        later = re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000 + 12 * 3600 + 1)
        assert later["cached"] is False and len(req.calls) == 2
        blob = json.dumps(first, ensure_ascii=False)
        for needle in ("s-prev", "access-fixture", "app-secret-fixture", "re0.test"):
            assert needle not in blob, needle

    def test_a_declared_remark_still_beats_the_file_names(self, store, tmp_path):
        rid = _preview_resource(store, remark="S03 全24集 完结")
        req = FakeRequester().route("GET", PREVIEW_URL, _preview_ok())
        client, _ = _client(tmp_path, req)
        out = re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)
        assert out["composition"]["confidence"] == "declared" and out["composition"]["episode_count"] == 24

    def test_an_invalid_share_returns_the_validation_result_and_a_short_cache(self, store, tmp_path):
        rid = _preview_resource(store)
        req = FakeRequester().route("GET", PREVIEW_URL, _preview_ok(files=[], result_type="validation",
                                                                   resource_validate_status="invalid",
                                                                   resource_validate_message="分享已失效"))
        client, _ = _client(tmp_path, req)
        out = re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)
        assert out["status"] == "invalid" and out["files"] == [] and out["file_count"] == 0
        assert out["validate_status"] == "invalid" and out["validate_message"] == "分享已失效"
        assert re0_sync.file_preview(store, client, resource_id=rid, now=1_000_060)["cached"] is True
        assert re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000 + 301)["cached"] is False  # 5 min
        assert len(req.calls) == 2

    def test_upstream_refusals_map_to_stable_local_statuses(self, store, tmp_path):
        cases = {
            403: ("forbidden", {"success": False, "code": "USER_LEVEL_REQUIRED", "message": "需要有效 V 用户"}),
            400: ("unsupported", {"success": False, "message": "文件列表获取失败"}),
            503: ("error", {"success": False, "message": "服务不可用"}),
        }
        for status, (expected, body) in cases.items():
            rid = _preview_resource(store, slug=f"s-prev-{status}", tmdb_id=600 + status)
            req = FakeRequester().route("GET", f"https://re0.test/api/open/resources/file-list/s-prev-{status}",
                                        FakeResponse(status, body))
            client, _ = _client(tmp_path, req)
            out = re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)
            assert out["status"] == expected, (status, out)
            assert out["files"] == [] and "slug" not in out
            assert out["error_class"] and out["message"]

    def test_a_rate_limit_reports_retry_after_and_is_not_cached(self, store, tmp_path):
        rid = _preview_resource(store)
        req = FakeRequester().route("GET", PREVIEW_URL, [FakeResponse(429, {"success": False}, headers={"Retry-After": "45"}),
                                                         _preview_ok()])
        client, state = _client(tmp_path, req)
        out = re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)
        assert out["status"] == "error" and out["error_class"] == "rate_limited" and out["retry_after"] == 45
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_file_preview WHERE status='ready'").fetchone()[0] == 0
        conn.close()

    def test_forbidden_and_unsupported_are_cached_so_we_stop_asking(self, store, tmp_path):
        rid = _preview_resource(store)
        req = FakeRequester().route("GET", PREVIEW_URL, FakeResponse(400, {"success": False, "message": "文件列表获取失败"}))
        client, _ = _client(tmp_path, req)
        assert re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)["status"] == "unsupported"
        again = re0_sync.file_preview(store, client, resource_id=rid, now=1_000_600)
        assert again["status"] == "unsupported" and again["cached"] is True and len(req.calls) == 1

    def test_the_file_list_is_capped_and_never_carries_a_link(self, store, tmp_path):
        rid = _preview_resource(store)
        files = [{"name": f"E{i:03d}.mkv", "path": f"/x/E{i:03d}.mkv", "size": 1, "extension": "mkv"} for i in range(400)]
        files.append({"name": "看这里 https://115.com/s/swleak.mkv", "path": "/x/https://115.com/s/swleak", "size": 1, "extension": "mkv"})
        req = FakeRequester().route("GET", PREVIEW_URL, _preview_ok(files=files, file_count=401))
        client, _ = _client(tmp_path, req)
        out = re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)
        assert out["file_count"] == 401 and len(out["files"]) == re0_sync.FILE_LIST_MAX_FILES and out["truncated"] is True
        assert "115.com" not in json.dumps(out, ensure_ascii=False)

    def test_a_preview_never_unlocks_and_never_writes_a_link(self, store, tmp_path):
        rid = _preview_resource(store)
        before = [tuple(r) for r in store.connect(readonly=True).execute("SELECT * FROM resource_link")]
        req = FakeRequester().route("GET", PREVIEW_URL, _preview_ok())
        client, _ = _client(tmp_path, req)
        re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)
        conn = store.connect(readonly=True)
        try:
            assert [tuple(r) for r in conn.execute("SELECT * FROM resource_link")] == before
            assert conn.execute("SELECT COUNT(*) FROM re0_resource_link").fetchone()[0] == 0
            assert conn.execute("SELECT state FROM re0_resource WHERE id=?", (rid,)).fetchone()[0] == "candidate"
        finally:
            conn.close()
        assert all("unlock" not in c["url"] for c in req.calls)

    def test_an_unknown_candidate_never_reaches_re0(self, store, tmp_path):
        req = FakeRequester()
        client, _ = _client(tmp_path, req)
        out = re0_sync.file_preview(store, client, resource_id=987654, now=1_000_000)
        assert out["status"] == "error" and out["error_class"] == "unknown_resource" and req.calls == []

    def test_the_preview_table_survives_being_created_twice(self, store):
        conn = store.connect()
        re0_sync.ensure_tables(conn)
        re0_sync.ensure_tables(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "re0_file_preview" in names


class TestPreviewSummaryForRows:
    def test_candidate_rows_carry_the_composition_and_preview_state(self, store, tmp_path):
        rid = _preview_resource(store, remark="4K高码，24集首更完结")
        conn = store.connect(readonly=True)
        try:
            row = re0_sync.candidate_rows(conn, "movie", 555)[0]
        finally:
            conn.close()
        assert row["remark"] == "4K高码，24集首更完结"
        assert row["composition"]["display"] == "24集" and row["composition"]["confidence"] == "declared"
        assert row["file_preview"] == {"available": True, "status": "not_loaded", "file_count": None, "fetched_at": None}
        assert row["published_at"] is None and row["publisher"] is None and row["is_official"] is None

    def test_a_cached_preview_shows_up_in_the_row_without_any_call(self, store, tmp_path):
        rid = _preview_resource(store)
        req = FakeRequester().route("GET", PREVIEW_URL, _preview_ok())
        client, _ = _client(tmp_path, req)
        re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)
        conn = store.connect(readonly=True)
        try:
            row = re0_sync.candidate_rows(conn, "movie", 555)[0]
        finally:
            conn.close()
        assert row["file_preview"]["status"] == "ready" and row["file_preview"]["file_count"] == 10
        assert row["file_preview"]["available"] is True and row["file_preview"]["fetched_at"]
        assert row["composition"]["display"] == "S03 · 第 1–10 集"  # the preview refines the row's composition
        assert len(req.calls) == 1

    def test_an_unsupported_pan_stops_offering_the_preview(self, store, tmp_path):
        rid = _preview_resource(store)
        req = FakeRequester().route("GET", PREVIEW_URL, FakeResponse(400, {"success": False, "message": "文件列表获取失败"}))
        client, _ = _client(tmp_path, req)
        re0_sync.file_preview(store, client, resource_id=rid, now=1_000_000)
        conn = store.connect(readonly=True)
        try:
            row = re0_sync.candidate_rows(conn, "movie", 555)[0]
        finally:
            conn.close()
        assert row["file_preview"]["available"] is False and row["file_preview"]["status"] == "unsupported"


class TestDisplayFieldEpochCoversTheRemarkFields:
    def test_the_epoch_is_at_or_after_the_release_that_started_storing_remarks(self):
        """Round 31: the backfill marker must move whenever a NEW display
        field lands, or candidates refreshed between the two releases keep
        their gap forever. remark/publisher/created_at shipped in release Z
        (2026-09-10T11:33:28Z)."""
        assert re0_sync.DISPLAY_FIELDS_EPOCH >= 1789040008

    def test_a_candidate_refreshed_before_that_release_is_queued_again(self, store):
        # Seen after the OLD marker (release U) but before the new one: it has
        # a name but no remark, and must still be picked up.
        media_id, _ = _seed_stale_display_row(store, tmdb_id=6610, last_seen_at=1789004893 + 3600)
        conn = store.connect(readonly=True)
        try:
            rows = re0_sync.select_refresh_queue(conn, now=re0_sync.DISPLAY_FIELDS_EPOCH + 3600, limit=10,
                                                 ttl=re0_sync.REFRESH_TTL_SECONDS, cursor=0)
        finally:
            conn.close()
        assert [r["id"] for r in rows] == [media_id]


# ---------------------------------------------------------------------------
# Invalid-candidate work order §3.1/§5.1: one derived display status per
# candidate, from fields that already exist. A share RE0 confirmed dead must
# never read as merely "unchecked", and a preview we could not run must never
# read as dead.
# ---------------------------------------------------------------------------

NOW_EFF = 1_789_100_000


def _eff_resource(store, *, tmdb_id, validate_status=None, validate_message=None, provider="189",
                  last_validated_at=None, slug=None):
    # `provider` is the UPSTREAM pan_type ("189" -> tianyicloud), not our code.
    media_id = _media(store, tmdb_id=tmdb_id)
    item = re0_sync.normalize_item({
        "slug": slug or f"s-eff-{tmdb_id}", "pan_type": provider, "is_unlocked": False,
        "validate_status": validate_status, "validate_message": validate_message,
        "last_validated_at": last_validated_at,
    }, salt="s")
    rid, _ = re0_sync.upsert_resource(store, "movie", tmdb_id, item, media_id=media_id, media_title="片", now=100)
    return media_id, rid


def _eff(store, tmdb_id, rid):
    conn = store.connect(readonly=True)
    try:
        return {r["id"]: r for r in re0_sync.candidate_rows(conn, "movie", tmdb_id, now=NOW_EFF)}[rid]
    finally:
        conn.close()


class TestEffectiveStatus:
    def test_upstream_invalid_reads_as_invalid_with_its_own_reason(self, store):
        _, rid = _eff_resource(store, tmdb_id=7001, validate_status="invalid",
                               validate_message="链接状态异常，需人工复核", last_validated_at="2026-09-10T00:00:00+08:00")
        row = _eff(store, 7001, rid)
        assert row["effective_status"] == "invalid" and row["effective_status_label"] == "RE0 已失效"
        assert row["effective_status_reason"] == "链接状态异常，需人工复核"
        assert row["effective_checked_at"] == "2026-09-09T16:00:00+00:00"
        assert row["has_local_link"] is False

    def test_a_live_invalid_preview_overrides_an_upstream_valid(self, store, tmp_path):
        _, rid = _eff_resource(store, tmdb_id=7002, validate_status="valid")
        assert _eff(store, 7002, rid)["effective_status"] == "valid"
        re0_sync._preview_store(store, rid, status="invalid", now=NOW_EFF, ttl=re0_sync.PREVIEW_INVALID_TTL_SECONDS,
                                file_count=0, files=[], validate_status="invalid", validate_message="分享已失效")
        row = _eff(store, 7002, rid)
        assert row["effective_status"] == "invalid" and row["effective_status_reason"] == "分享已失效"

    def test_an_expired_invalid_preview_falls_back_to_the_upstream_status(self, store):
        _, rid = _eff_resource(store, tmdb_id=7003, validate_status="valid")
        re0_sync._preview_store(store, rid, status="invalid", now=NOW_EFF - 10_000,
                                ttl=re0_sync.PREVIEW_INVALID_TTL_SECONDS, file_count=0, files=[])
        assert _eff(store, 7003, rid)["effective_status"] == "valid"

    @pytest.mark.parametrize("upstream,expected", [
        ("valid", "valid"), ("invalid", "invalid"), ("checking", "checking"),
        ("pending", "checking"), ("error", "unknown"), (None, "unchecked"), ("", "unchecked"),
    ])
    def test_upstream_status_mapping(self, store, upstream, expected):
        tmdb = 7100 + abs(hash(str(upstream))) % 500
        _, rid = _eff_resource(store, tmdb_id=tmdb, validate_status=upstream, slug=f"s-map-{upstream}")
        assert _eff(store, tmdb, rid)["effective_status"] == expected

    def test_checking_is_never_shown_as_dead(self, store):
        _, rid = _eff_resource(store, tmdb_id=7004, validate_status="checking")
        row = _eff(store, 7004, rid)
        assert row["effective_status"] == "checking" and row["effective_status_label"] == "RE0 校验中"
        assert row["effective_status"] != "invalid"

    @pytest.mark.parametrize("preview_status", ["unsupported", "forbidden"])
    def test_a_preview_we_could_not_run_is_not_a_dead_share(self, store, preview_status):
        tmdb = 7010 if preview_status == "unsupported" else 7011
        _, rid = _eff_resource(store, tmdb_id=tmdb, slug=f"s-pv-{preview_status}")
        re0_sync._preview_store(store, rid, status=preview_status, now=NOW_EFF,
                                ttl=re0_sync.PREVIEW_STABLE_TTL_SECONDS, error_class="upstream_4xx")
        row = _eff(store, tmdb, rid)
        assert row["effective_status"] == "preview_unavailable"
        assert row["effective_status"] != "invalid"

    def test_a_rate_limited_or_5xx_preview_leaves_no_cache_and_no_verdict(self, store, tmp_path):
        _, rid = _eff_resource(store, tmdb_id=7012, slug="s-pv-429")
        req = FakeRequester().route("GET", "https://re0.test/api/open/resources/file-list/s-pv-429",
                                    FakeResponse(429, {"success": False}, headers={"Retry-After": "30"}))
        client, _ = _client(tmp_path, req)
        out = re0_sync.file_preview(store, client, resource_id=rid, now=NOW_EFF)
        assert out["status"] == "error" and out["error_class"] == "rate_limited"
        assert _eff(store, 7012, rid)["effective_status"] == "unchecked"

    def test_an_invalid_candidate_that_already_has_a_local_link_is_kept(self, store):
        media_id, rid = _eff_resource(store, tmdb_id=7005, validate_status="invalid")
        group_id = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-eff", display_title="1080p"))
        link_id, _ = _local_link(store, group_id)
        conn = store.connect()
        conn.execute("INSERT INTO re0_resource_link(re0_resource_id, resource_link_id, relation, linked_at) VALUES(?,?,?,?)",
                     (rid, link_id, "materialized_unlocked", 100))
        conn.commit(); conn.close()
        row = _eff(store, 7005, rid)
        assert row["effective_status"] == "invalid" and row["has_local_link"] is True
        assert row["resource_link_id"]
        visible, hidden = re0_sync.split_invalid([row], include_invalid=False)
        assert visible == [row] and hidden == 0   # a materialised candidate is never hidden

    def test_split_invalid_hides_only_unmaterialised_dead_candidates(self, store):
        rows = [
            {"id": 1, "effective_status": "invalid", "has_local_link": False},
            {"id": 2, "effective_status": "invalid", "has_local_link": True},
            {"id": 3, "effective_status": "checking", "has_local_link": False},
            {"id": 4, "effective_status": "valid", "has_local_link": False},
            {"id": 5, "effective_status": "preview_unavailable", "has_local_link": False},
        ]
        visible, hidden = re0_sync.split_invalid(rows, include_invalid=False)
        assert [r["id"] for r in visible] == [2, 3, 4, 5] and hidden == 1
        audit, hidden_audit = re0_sync.split_invalid(rows, include_invalid=True)
        assert [r["id"] for r in audit] == [1, 2, 3, 4, 5] and hidden_audit == 0

    def test_a_protocol_link_has_no_file_preview_capability(self, store):
        assert re0_sync.supports_file_preview("ed2k") is False
        assert re0_sync.supports_file_preview("unknown") is False
        for pan in ("115", "quark", "alipan", "baidu", "tianyicloud", "guangya", "139cloud", "123"):
            assert re0_sync.supports_file_preview(pan) is True, pan
        _, rid = _eff_resource(store, tmdb_id=7006, provider="ed2k", slug="s-eff-ed2k")
        row = _eff(store, 7006, rid)
        assert row["file_preview"]["available"] is False and row["file_preview"]["status"] == "not_applicable"
        assert row["action"] == "cloud"   # copy/cloud actions are untouched

    def test_an_ed2k_preview_request_never_reaches_re0_and_caches_nothing(self, store, tmp_path):
        _, rid = _eff_resource(store, tmdb_id=7007, provider="ed2k", slug="s-eff-ed2k-2")
        req = FakeRequester()
        client, _ = _client(tmp_path, req)
        out = re0_sync.file_preview(store, client, resource_id=rid, now=NOW_EFF)
        assert out["status"] == "unsupported" and out["error_class"] == "preview_not_applicable"
        assert "协议链接" in out["message"] and req.calls == []
        conn = store.connect(readonly=True)
        assert conn.execute("SELECT COUNT(*) FROM re0_file_preview").fetchone()[0] == 0
        conn.close()
        assert _eff(store, 7007, rid)["effective_status"] != "invalid"
