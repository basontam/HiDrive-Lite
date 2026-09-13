"""Persist RE0 metadata, not paid links; rank one catalog before paging."""
import json
import sqlite3
import pytest

import library_search
import re0_sync
from test_re0_search_api import re0_env, _tmdb_result, _re0_item, RE0_SEARCH


def seed(env, kind, tmdb_id, title, original=None, provider="115", status="valid"):
    re0_sync.upsert_projection(env["store"], kind, tmdb_id, title=title, original_title=original,
                               year=2014, overview="简介", poster_path="/p.jpg", backdrop_path="/b.jpg", ratings={}, now=1)
    re0_sync.record_items(env["store"], kind, tmdb_id,
                         [_re0_item(f"catalog-{kind}-{tmdb_id}-fixture", provider, validate_status=status)],
                         media_id=None, media_title=title, salt="catalog-salt-fixture", now=1)
    re0_sync.catalog_projections(env["store"], [(kind, tmdb_id)])


def test_discovery_is_searchable_in_both_languages_without_unlock(client, re0_env, http):
    env = re0_env
    row = _tmdb_result(777, "失踪", kind="tv", year="2014")
    row["original_name"] = "The Missing"
    env["session"].route(env["tmdb_base"] + "/search/movie", {"results": []})
    env["session"].route(env["tmdb_base"] + "/search/tv", {"results": [row]})
    http.route("GET", "https://re0.me/api/open/resources/tv/777", {
        "success": True, "data": [_re0_item("missing-catalog-fixture", "115")]})
    assert client.get(RE0_SEARCH + "?q=the+missing").status_code == 200
    for query in ("失踪", "the missing"):
        data = client.get("/api/library/search", query_string={"q": query}).get_json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "失踪"
        assert data["items"][0]["sources"] == ["re0"]
        suggestion = client.get("/api/library/suggest", query_string={"q": query}).get_json()
        assert "失踪" in json.dumps(suggestion, ensure_ascii=False)
    conn = env["store"].connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0] == 1
    finally:
        conn.close()
    assert client.get(RE0_SEARCH + "?q=the+missing").status_code == 200
    conn = env["store"].connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == before
    finally:
        conn.close()
    assert not any("/unlock" in call["url"] for call in http.calls)


def test_one_catalog_ranks_before_paging_and_deduplicates_by_typed_identity(client, re0_env):
    env = re0_env
    seed(env, "movie", 555, "本地片")  # attaches to existing local identity
    seed(env, "tv", 777, "本地片外传")
    seed(env, "movie", 777, "本地片续篇")  # same numeric ID is NOT the same title
    seed(env, "movie", 888, "毫无关联")
    pages = [client.get("/api/library/search", query_string={"q": "本地片", "page_size": 1, "page": p}).get_json() for p in (1, 2, 3)]
    assert all(p["total"] == 3 for p in pages)
    assert pages[0]["items"][0]["sources"] == ["local", "re0"]
    assert pages[0]["items"][0]["title"] == "本地片"
    ids = [p["items"][0]["media_id"] for p in pages]
    assert len(set(ids)) == 3
    assert {(p["items"][0]["media_type"], p["items"][0]["tmdb_id"]) for p in pages} == {("movie", 555), ("movie", 777), ("tv", 777)}
    assert client.get("/api/library/search?q=ZZZZZ_UNRELATED").get_json()["total"] == 0


def test_remote_only_candidates_obey_filters_and_invalid_visibility(client, re0_env):
    seed(re0_env, "tv", 777, "失踪", "The Missing", provider="quark")
    seed(re0_env, "movie", 888, "失踪坏链", status="invalid")
    for filters in ({"provider": "115"}, {"quality": "1080p"}, {"type": "movie"}, {"year": "2000"}, {"genre": "动画"}, {"season": "2"}):
        assert client.get("/api/library/search", query_string={"q": "失踪", **filters}).get_json()["total"] == 0
    # Edition normalization defines the canonical quality spelling.
    conn = re0_env["store"].connect()
    try:
        quality = json.loads(conn.execute("SELECT spec_json FROM re0_resource WHERE tmdb_id=777").fetchone()[0])["quality"]
    finally:
        conn.close()
    result = client.get("/api/library/search", query_string={"q": "失踪", "provider": "quark", "quality": quality}).get_json()
    assert result["total"] == 1 and result["items"][0]["sources"] == ["re0"]


def test_catalog_mode_still_discovers_remote_when_local_matches(client, re0_env, http):
    env = re0_env
    env["session"].route(env["tmdb_base"] + "/search/movie", {"results": [_tmdb_result(999, "本地片外传")]})
    body = client.get(RE0_SEARCH, query_string={"q": "本地片", "catalog": "1", "page_size": 1, "page": 2}).get_json()
    assert env["session"].calls, "local exact hit must not suppress remote discovery"
    assert body["catalog"]["total"] == 2
    assert len(body["catalog"]["items"]) == 1
    assert body["catalog"]["items"][0]["title"] == "本地片外传"
    assert body["catalog"]["items"][0]["sources"] == ["re0"]
    assert not any("/unlock" in call["url"] for call in http.calls)


def test_incremental_index_keeps_existing_terms_and_is_idempotent(re0_env):
    store = re0_env["store"]
    conn = store.connect()
    try:
        original = [tuple(r) for r in conn.execute("SELECT * FROM search_term ORDER BY term, media_id, field")]
    finally:
        conn.close()
    library_search.build_index(store, media_ids=[re0_env["media_id"]])
    library_search.build_index(store, media_ids=[re0_env["media_id"]])
    conn = store.connect()
    try:
        assert [tuple(r) for r in conn.execute("SELECT * FROM search_term ORDER BY term, media_id, field")] == original
        assert conn.execute("SELECT MAX(df) FROM search_vocab").fetchone()[0] == 1
    finally:
        conn.close()


def test_corrected_original_title_removes_the_old_index_alias(re0_env):
    store = re0_env["store"]
    mid = re0_env["media_id"]
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET title_original='Wrong Film' WHERE id=?", (mid,))
        conn.commit()
    finally:
        conn.close()
    library_search.build_index(store, media_ids=[mid])
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET title_original='Correct Film' WHERE id=?", (mid,))
        conn.commit()
    finally:
        conn.close()
    library_search.build_index(store, media_ids=[mid])
    assert library_search.search(store, "Wrong", library_search.Filters()).total == 0
    assert library_search.search(store, "Correct", library_search.Filters()).total == 1


def test_index_failure_keeps_alias_retryable(re0_env, monkeypatch):
    store = re0_env["store"]
    seed(re0_env, "movie", 555, "本地片")
    re0_sync.upsert_projection(store, "movie", 555, title="本地片", original_title="New English Alias",
                               year=2019, overview=None, poster_path=None, backdrop_path=None, ratings={}, now=2)
    original = library_search.build_index
    def fail(*args, **kwargs):
        raise RuntimeError("index-failure-fixture")
    monkeypatch.setattr(library_search, "build_index", fail)
    with pytest.raises(RuntimeError):
        re0_sync.catalog_projections(store, [("movie", 555)])
    monkeypatch.setattr(library_search, "build_index", original)
    re0_sync.catalog_projections(store, [("movie", 555)])
    assert library_search.search(store, "New English Alias", library_search.Filters()).total == 1


def test_newly_invalid_candidate_disappears_from_catalog(client, re0_env):
    seed(re0_env, "tv", 777, "失踪")
    assert client.get('/api/library/search?q=失踪').get_json()['total'] == 1
    conn = re0_env['store'].connect()
    try:
        conn.execute("UPDATE re0_resource SET upstream_validate_status=' invalid ' WHERE tmdb_id=777")
        conn.commit()
    finally:
        conn.close()
    assert client.get('/api/library/search?q=失踪').get_json()['total'] == 0


def test_audit_source_does_not_imply_a_usable_candidate(re0_env):
    seed(re0_env, "tv", 777, "失踪正常")
    seed(re0_env, "tv", 778, "失踪坏链")
    conn = re0_env["store"].connect()
    try:
        conn.execute("UPDATE re0_resource SET upstream_validate_status='invalid' WHERE tmdb_id=778")
        conn.commit()
    finally:
        conn.close()
    result = library_search.search(re0_env["store"], "失踪", library_search.Filters(include_deleted=True, include_re0=True))
    by_tmdb = {item["tmdb_id"]: item for item in result.items}
    assert by_tmdb[777]["sources"] == ["re0"]
    assert by_tmdb[778]["sources"] == ["re0"]
    assert by_tmdb[777]["has_usable_re0"] is True
    assert by_tmdb[778]["has_usable_re0"] is False


def test_full_index_computation_allows_writes_and_rejects_stale_snapshot(re0_env, monkeypatch):
    store = re0_env["store"]
    conn = store.connect()
    try:
        re0_sync.state_set(conn, "index_dirty", "1", 1)
        conn.commit()
    finally:
        conn.close()
    original = library_search.tokenize
    changed = False

    def mutate_once(text, charmap):
        nonlocal changed
        if not changed:
            changed = True
            conn = store.connect()
            try:
                conn.execute("PRAGMA busy_timeout=50")
                conn.execute("UPDATE media SET title_original='Concurrent Update' WHERE id=?", (re0_env["media_id"],))
                conn.commit()
            finally:
                conn.close()
            library_search.build_index(store, media_ids=[re0_env["media_id"]])
        return original(text, charmap)

    monkeypatch.setattr(library_search, "tokenize", mutate_once)
    with pytest.raises(sqlite3.OperationalError, match="search index input changed"):
        library_search.build_index(store, clear_re0_dirty=True)
    assert library_search.search(store, "Concurrent Update", library_search.Filters()).total == 1
    conn = store.connect(readonly=True)
    try:
        assert re0_sync.state_get(conn, "index_dirty") == "1"
    finally:
        conn.close()
    monkeypatch.setattr(library_search, "tokenize", original)
    library_search.build_index(store, clear_re0_dirty=True)
    conn = store.connect(readonly=True)
    try:
        assert re0_sync.state_get(conn, "index_dirty") == "0"
    finally:
        conn.close()
    assert library_search.search(store, "Concurrent Update", library_search.Filters()).total == 1
