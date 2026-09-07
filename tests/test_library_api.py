"""Tests for the T4 query service, /api/library/* routes, settings wiring,
reveal/transfer bridge and background-enrichment wiring.

All URLs/access codes below are fixtures, not real shares: hostnames use
real domain shapes but share codes are prefixed ``swfake``/``fake`` and
access codes are fixed 4-character placeholders, per the project's
test-data rule (see .superpowers/sdd/briefs/common.md).

T4.1 (query service) is tested directly against ``LibraryStore`` here,
independent of ``install_bundle`` (a T4.2 deliverable) -- a store is built
with the write API and manually flagged ``encrypted=1`` with its own
ciphertext columns populated, exactly mirroring what ``install_bundle``
would have produced but without depending on it, since the whole point of
building this layer first is that it can be tested in isolation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_store as ls  # noqa: E402
import library_search as lse  # noqa: E402
import library_normalize as ln  # noqa: E402


def _hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


# ---------------------------------------------------------------------------
# A small encrypted store, built directly through the write API (no
# install_bundle dependency -- see module docstring).
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet() -> Fernet:
    return Fernet(Fernet.generate_key())


@pytest.fixture
def query_store(tmp_path, fernet) -> ls.LibraryStore:
    db_path = tmp_path / "media-library.db"
    store = ls.LibraryStore(db_path, fernet)
    store.create_schema()

    now = int(time.time())

    movie_id = store.upsert_media(
        ls.MediaRecord(
            media_identity="tmdb:movie:100001",
            media_type="movie",
            title_zh="虚构电影一",
            title_original="Fake Movie One",
            search_key="虚构电影一",
            year=2020,
            tmdb_id=100001,
            overview="一部虚构的电影，用于测试。",
            poster_path="/poster1.jpg",
            backdrop_path="/backdrop1.jpg",
            genres_json=json.dumps(["剧情"], ensure_ascii=False),
            match_status="exact",
            match_score=0.95,
        )
    )
    tv_id = store.upsert_media(
        ls.MediaRecord(
            media_identity="fp:tv:200002",
            media_type="tv",
            title_zh="虚构剧集一",
            search_key="虚构剧集一",
            year=2021,
            match_status="needs_review",
        )
    )

    movie_group = store.upsert_group(
        ls.GroupRecord(
            media_id=movie_id,
            edition_fingerprint="fp-movie-1",
            display_title="2160p WEB-DL · DV/HDR",
            quality="2160p",
            hdr="dv_hdr",
        )
    )
    tv_group = store.upsert_group(
        ls.GroupRecord(
            media_id=tv_id,
            edition_fingerprint="fp-tv-1",
            display_title="S01 · 1080p",
            quality="1080p",
            season_from=1,
            season_to=1,
        )
    )

    def _encrypt(value: str | None) -> bytes | None:
        return fernet.encrypt(value.encode("utf-8")) if value is not None else None

    links = [
        # (group, provider, url, access_code, deleted, remark)
        (movie_group, "115", "https://115.com/s/swfake100001", "ab12", False, "S01 4K WEB-DL DV 内封简繁"),
        (movie_group, "quark", "https://pan.quark.cn/s/swfake200002", None, False, "备用网盘"),
        (tv_group, "ed2k", "ed2k://|file|fake.S01E01.mkv|123|ABCDEF0123456789ABCDEF0123456789|/", None, False, ""),
        (tv_group, "unknown", "群公告置顶，非直链，见钉钉", None, False, "非 URL 备注"),
        (tv_group, "baidu", "https://pan.baidu.com/s/swfake500005", None, True, "已失效"),
    ]

    public_ids = {}
    for index, (group_id, provider, url, code, deleted, remark) in enumerate(links, start=1):
        public_id = f"pub-fixture-{index:02d}"
        public_ids[(provider, group_id)] = public_id
        if provider == "unknown":
            # Mirror library_normalize._build_label: a real-URL unknown
            # link is labelled "未知来源 · <host>" (open+copy actions);
            # non-URL text gets INVALID_LINK_LABEL (no actions).
            host = urlparse(url).hostname
            label = f"未知来源 · {host}" if host else ln.INVALID_LINK_LABEL
        else:
            label = f"{provider} 分享 · swf…{index}"
        store.upsert_link(
            ls.LinkRecord(
                public_id=public_id,
                group_id=group_id,
                provider=provider,
                canonical_url_hash=_hash(url),
                url_label=label,
                url_ciphertext=_encrypt(url),
                access_code_ciphertext=_encrypt(code),
                has_access_code=1 if code else 0,
                remark=remark,
                created_at_source=now - index * 3600,
                deleted_at_source=now - 60 if deleted else None,
            )
        )

    store.recount()
    lse.build_index(store)
    store.meta_set("normalize_version", "test-1")
    store.meta_set("built_at", str(now))
    store.meta_set("encrypted", "1")
    store.meta_set("installed_at", str(now))

    store.movie_id = movie_id
    store.tv_id = tv_id
    store.movie_group = movie_group
    store.tv_group = tv_group
    store.public_ids = public_ids
    return store


# ---------------------------------------------------------------------------
# open_installed
# ---------------------------------------------------------------------------


class TestOpenInstalled:
    def test_missing_file_raises_not_installed(self, tmp_path, fernet):
        with pytest.raises(ls.LibraryNotInstalled):
            ls.open_installed(tmp_path / "nope.db", fernet)

    def test_schema_newer_than_code_raises_mismatch(self, tmp_path, fernet):
        db_path = tmp_path / "media-library.db"
        store = ls.LibraryStore(db_path)
        store.create_schema()
        store.meta_set("schema_version", "2")
        store.meta_set("encrypted", "1")
        with pytest.raises(ls.LibrarySchemaMismatch):
            ls.open_installed(db_path, fernet)

    def test_unencrypted_library_raises_not_encrypted(self, tmp_path, fernet):
        db_path = tmp_path / "media-library.db"
        store = ls.LibraryStore(db_path)
        store.create_schema()  # encrypted defaults to "0"
        with pytest.raises(ls.LibraryNotEncrypted):
            ls.open_installed(db_path, fernet)

    def test_encrypted_library_opens(self, query_store, fernet):
        store = ls.open_installed(query_store.db_path, fernet)
        assert isinstance(store, ls.LibraryStore)

    def test_truncated_file_raises_index_unreadable(self, tmp_path, fernet):
        # I1: a truncated/corrupt media-library.db (not a valid SQLite
        # file at all) must not let sqlite3.DatabaseError escape -- it's a
        # distinct condition from "not installed" (missing file).
        db_path = tmp_path / "media-library.db"
        db_path.write_bytes(b"not a sqlite database")
        with pytest.raises(ls.LibraryIndexUnreadable):
            ls.open_installed(db_path, fernet)


# ---------------------------------------------------------------------------
# browse (pure filter)
# ---------------------------------------------------------------------------


class TestBrowse:
    def test_browse_returns_all_live_media_by_default(self, query_store):
        page = query_store.browse(lse.Filters(), sort="relevance", page=1, page_size=24)
        titles = {item["title"] for item in page.items}
        assert titles == {"虚构电影一", "虚构剧集一"}
        assert page.total == 2

    def test_browse_filters_by_media_type(self, query_store):
        page = query_store.browse(lse.Filters(media_type="movie"), sort="relevance", page=1, page_size=24)
        assert [item["title"] for item in page.items] == ["虚构电影一"]

    def test_browse_sort_year_asc(self, query_store):
        page = query_store.browse(lse.Filters(), sort="year_asc", page=1, page_size=24)
        assert [item["year"] for item in page.items] == [2020, 2021]

    def test_browse_excludes_media_whose_only_links_are_deleted(self, query_store):
        # tv media has an ed2k + unknown live link, so it stays visible even
        # though its baidu link is deleted -- this only proves browse() is
        # wired to Filters.include_deleted, not the filter's own logic
        # (already covered by T2's library_search tests).
        page = query_store.browse(lse.Filters(include_deleted=False), sort="relevance", page=1, page_size=24)
        assert len(page.items) == 2

    def test_browse_items_match_search_item_shape(self, query_store):
        page = query_store.browse(lse.Filters(media_type="movie"), sort="relevance", page=1, page_size=24)
        item = page.items[0]
        for key in ("media_id", "media_type", "title", "original_title", "year", "poster_path",
                    "match_status", "genres", "link_count", "has_115", "providers", "groups_summary", "needs_review"):
            assert key in item, key


# ---------------------------------------------------------------------------
# filters (with 300s process-local cache)
# ---------------------------------------------------------------------------


class TestFilters:
    def test_filters_returns_all_dimensions(self, query_store):
        result = query_store.filters()
        for key in ("types", "years", "providers", "qualities", "hdr", "genres"):
            assert key in result, key

    def test_filters_provider_entries_have_value_label_count(self, query_store):
        result = query_store.filters()
        providers = {row["value"]: row for row in result["providers"]}
        assert "115" in providers
        assert providers["115"]["count"] == 1
        assert providers["115"]["label"]
        # the deleted baidu link must not count toward the providers filter
        assert "baidu" not in providers

    def test_filters_years_are_descending(self, query_store):
        result = query_store.filters()
        years = [row["value"] for row in result["years"]]
        assert years == sorted(years, reverse=True)

    def test_filters_is_cached_for_300_seconds(self, query_store):
        clock = {"now": 1000.0}
        result1 = query_store.filters(clock=lambda: clock["now"])
        # mutate underlying data directly; a cached call must not see it
        store2 = ls.LibraryStore(query_store.db_path, query_store.fernet)
        extra_media = store2.upsert_media(
            ls.MediaRecord(media_identity="tmdb:movie:999999", media_type="movie", title_zh="新电影", search_key="新电影", year=1999)
        )
        clock["now"] = 1200.0  # +200s, still within the 300s TTL
        result2 = query_store.filters(clock=lambda: clock["now"])
        assert result2 == result1

        clock["now"] = 1301.0  # +301s, past the TTL
        result3 = query_store.filters(clock=lambda: clock["now"])
        years3 = {row["value"] for row in result3["years"]}
        assert 1999 in years3


# ---------------------------------------------------------------------------
# media_detail / group_detail
# ---------------------------------------------------------------------------


class TestMediaDetail:
    def test_unknown_media_id_returns_none(self, query_store):
        assert query_store.media_detail(999999) is None

    def test_media_detail_fields(self, query_store):
        detail = query_store.media_detail(query_store.movie_id)
        assert detail["title"] == "虚构电影一"
        assert detail["original_title"] == "Fake Movie One"
        assert detail["year"] == 2020
        assert detail["match_status"] == "exact"
        assert detail["genres"] == ["剧情"]
        assert detail["overview"]
        assert detail["poster_path"] == "/poster1.jpg"
        assert detail["backdrop_path"] == "/backdrop1.jpg"
        assert len(detail["groups"]) == 1

    def test_media_detail_never_leaks_urls_or_codes(self, query_store):
        detail = query_store.media_detail(query_store.movie_id)
        blob = json.dumps(detail, ensure_ascii=False)
        assert "115.com" not in blob
        assert "swfake" not in blob
        assert "ab12" not in blob
        # exact key names, not substrings: "has_access_code" (a legitimate
        # boolean flag per §7.3) must not be mistaken for the forbidden
        # "access_code" *value* key, nor "original_title" for "record_id" etc.
        for forbidden in ('"url":', '"access_code":', '"record_id":', '"slug":', '"uid":'):
            assert forbidden not in blob.lower()

    def test_media_detail_group_link_shape_and_actions(self, query_store):
        # Links live on group_detail() (the full payload), not media_detail()'s
        # group summaries -- see TestGroupDetail / T2 u1-backend's detail=summary,
        # expand=full split.
        group = query_store.group_detail(query_store.movie_group)
        for key in ("group_id", "display_title", "link_count", "has_115", "needs_review", "providers", "links"):
            assert key in group, key
        links_by_provider = {link["provider"]: link for link in group["links"]}
        assert links_by_provider["115"]["actions"] == ["transfer"]
        assert links_by_provider["115"]["has_access_code"] is True
        assert links_by_provider["quark"]["actions"] == ["open", "copy"]
        for key in ("link_id", "provider", "label", "has_access_code", "deleted", "remark", "created_at", "actions"):
            assert key in links_by_provider["115"], key

    def test_media_detail_link_actions_ed2k_and_unknown(self, query_store):
        group = query_store.group_detail(query_store.tv_group)
        links_by_provider = {link["provider"]: link for link in group["links"]}
        assert links_by_provider["ed2k"]["actions"] == ["copy"]
        assert links_by_provider["unknown"]["actions"] == []

    def test_link_actions_unknown_with_url_is_open_copy(self):
        # plan §4.4: an "unknown" link with a real (unlisted-host) URL is
        # still open/copy-able -- only non-URL text ("无效链接") gets no
        # actions at all. See TestRevealRoute for the matching 200-vs-400
        # /api/library/link/<id>/reveal behaviour, exercised through the
        # (label-driven) real ``_link_actions`` helper directly here.
        assert ls._link_actions("unknown", "未知来源 · example-netdisk.com") == ["open", "copy"]
        assert ls._link_actions("unknown", ln.INVALID_LINK_LABEL) == []

    def test_media_detail_marks_deleted_links(self, query_store):
        group = query_store.group_detail(query_store.tv_group)
        links_by_provider = {link["provider"]: link for link in group["links"]}
        assert links_by_provider["baidu"]["deleted"] is True
        assert links_by_provider["ed2k"]["deleted"] is False

    def test_media_detail_links_sorted_115_first(self, query_store):
        group = query_store.group_detail(query_store.movie_group)
        providers_order = [link["provider"] for link in group["links"]]
        assert providers_order[0] == "115"


class TestGroupDetail:
    def test_unknown_group_id_returns_none(self, query_store):
        assert query_store.group_detail(999999) is None

    def test_group_detail_matches_media_embedded_shape(self, query_store):
        group = query_store.group_detail(query_store.movie_group)
        assert group["media_id"] == query_store.movie_id
        assert group["display_title"] == "2160p WEB-DL · DV/HDR"
        assert len(group["links"]) == 2


# ---------------------------------------------------------------------------
# link_by_public_id / reveal
# ---------------------------------------------------------------------------


class TestLinkByPublicId:
    def test_unknown_public_id_returns_none(self, query_store):
        assert query_store.link_by_public_id("does-not-exist") is None

    def test_known_public_id_returns_row(self, query_store):
        public_id = query_store.public_ids[("115", query_store.movie_group)]
        row = query_store.link_by_public_id(public_id)
        assert row["provider"] == "115"


class TestReveal:
    def test_reveal_decrypts_url_and_code(self, query_store):
        public_id = query_store.public_ids[("115", query_store.movie_group)]
        url, code = query_store.reveal(public_id)
        assert url == "https://115.com/s/swfake100001"
        assert code == "ab12"

    def test_reveal_returns_none_code_when_absent(self, query_store):
        public_id = query_store.public_ids[("quark", query_store.movie_group)]
        url, code = query_store.reveal(public_id)
        assert url == "https://pan.quark.cn/s/swfake200002"
        assert code is None

    def test_reveal_without_fernet_raises_key_unavailable(self, query_store):
        store = ls.LibraryStore(query_store.db_path, fernet=None)
        public_id = query_store.public_ids[("115", query_store.movie_group)]
        with pytest.raises(ls.LibraryKeyUnavailable):
            store.reveal(public_id)

    def test_reveal_with_wrong_key_raises_key_unavailable(self, query_store):
        wrong = ls.LibraryStore(query_store.db_path, fernet=Fernet(Fernet.generate_key()))
        public_id = query_store.public_ids[("115", query_store.movie_group)]
        with pytest.raises(ls.LibraryKeyUnavailable):
            wrong.reveal(public_id)


# ---------------------------------------------------------------------------
# stats() extension
# ---------------------------------------------------------------------------


class TestStatsExtension:
    def test_stats_includes_match_and_cache_counts(self, query_store):
        stats = query_store.stats()
        assert stats["match"]["exact"] == 1
        assert stats["match"]["needs_review"] == 1
        assert stats["match"]["unmatched"] == 0
        assert stats["match"]["candidate"] == 0
        assert stats["cache"] == {"ok": 0, "empty": 0, "failed_retryable": 0, "failed_permanent": 0}
        assert stats["links_total"] == 5

    def test_stats_tolerates_missing_tmdb_tables(self, tmp_path):
        # A LibraryStore built without EXTRA_SCHEMA_HOOKS registered (i.e.
        # without importing app) must not crash stats() just because
        # tmdb_cache/tmdb_budget don't exist.
        import library_store as ls_fresh

        saved_hooks = list(ls_fresh.EXTRA_SCHEMA_HOOKS)
        ls_fresh.EXTRA_SCHEMA_HOOKS.clear()
        try:
            store = ls_fresh.LibraryStore(tmp_path / "bare.db")
            store.create_schema()
            stats = store.stats()
        finally:
            ls_fresh.EXTRA_SCHEMA_HOOKS.extend(saved_hooks)
        assert stats["cache"] == {"ok": 0, "empty": 0, "failed_retryable": 0, "failed_permanent": 0}


# ===========================================================================
# T4.3 HTTP routes, /api/status + /api/settings wiring, background
# enricher autostart (uses the `installed_library`/`library_markers`
# fixtures from conftest.py, and the `client`/`http` fixtures from the
# shared test base).
# ===========================================================================


def _has_marker(blob: str, markers: list[str]) -> str | None:
    for marker in markers:
        if marker and marker in blob:
            return marker
    return None


class TestNotInstalledGuards:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/library/search"),
            ("get", "/api/library/filters"),
            ("get", "/api/library/suggest?q=x"),
            ("get", "/api/library/media/1"),
            ("get", "/api/library/resource/1"),
            ("get", "/api/library/tmdb-status"),
        ],
    )
    def test_not_installed_returns_503(self, client, hidrive, method, path):
        response = getattr(client, method)(path)
        assert response.status_code == 503
        assert response.get_json()["code"] == "LIBRARY_NOT_INSTALLED"

    def test_plaintext_bundle_returns_not_encrypted(self, client, hidrive, workspace):
        store = ls.LibraryStore(hidrive.LIBRARY_DB_PATH)
        store.create_schema()  # encrypted defaults to "0"
        response = client.get("/api/library/search")
        assert response.status_code == 503
        assert response.get_json()["code"] == "LIBRARY_NOT_ENCRYPTED"

    def test_truncated_db_search_returns_index_unreadable(self, client, hidrive, workspace):
        # I1: /api/library/search on a truncated/corrupt index must 503
        # with LIBRARY_INDEX_UNREADABLE, not crash with a 500.
        hidrive.LIBRARY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        hidrive.LIBRARY_DB_PATH.write_bytes(b"not a sqlite database")
        response = client.get("/api/library/search")
        assert response.status_code == 503
        assert response.get_json()["code"] == "LIBRARY_INDEX_UNREADABLE"

    def test_truncated_db_status_stays_200_not_installed(self, client, hidrive, workspace):
        # I1: /api/status must never 500 just because the library index is
        # corrupt -- it degrades to library.installed=false, same as "not
        # installed at all", so the rest of the SPA still renders.
        hidrive.LIBRARY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        hidrive.LIBRARY_DB_PATH.write_bytes(b"not a sqlite database")
        response = client.get("/api/status")
        assert response.status_code == 200
        assert response.get_json()["library"] == {"installed": False, "media_total": 0}

    def test_newer_schema_version_returns_mismatch(self, client, hidrive, workspace):
        store = ls.LibraryStore(hidrive.LIBRARY_DB_PATH)
        store.create_schema()
        store.meta_set("schema_version", "2")
        store.meta_set("encrypted", "1")
        response = client.get("/api/library/search")
        assert response.status_code == 503
        assert response.get_json()["code"] == "LIBRARY_SCHEMA_MISMATCH"


class TestSearchRoute:
    def test_returns_all_by_default_with_no_store_header(self, client, installed_library):
        response = client.get("/api/library/search")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        body = response.get_json()
        assert body["success"] is True
        assert body["total"] == 6
        assert body["page"] == 1
        assert body["page_size"] == 24
        assert body["query"] == {"q": "", "type": "all"}
        assert "interpreted" in body

    def test_response_never_contains_markers(self, client, installed_library, library_markers):
        response = client.get("/api/library/search")
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        assert _has_marker(blob, library_markers) is None

    def test_text_query_finds_exact_title(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影一")
        body = response.get_json()
        titles = [item["title"] for item in body["items"]]
        assert "虚构电影一" in titles

    def test_filters_by_type(self, client, installed_library):
        response = client.get("/api/library/search?type=tv")
        body = response.get_json()
        assert all(item["media_type"] == "tv" for item in body["items"])
        assert body["total"] == 2

    def test_filters_by_provider(self, client, installed_library):
        response = client.get("/api/library/search?provider=ed2k")
        body = response.get_json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "虚构电影三"

    def test_include_deleted_changes_total(self, client, installed_library):
        # tv media #3 has a live guangya link alongside its deleted baidu
        # link, so it's already visible with include_deleted=0; this only
        # proves the query-string flag reaches Filters.include_deleted.
        without = client.get("/api/library/search?include_deleted=0").get_json()["total"]
        withdel = client.get("/api/library/search?include_deleted=1").get_json()["total"]
        assert withdel >= without

    def test_year_range_filter(self, client, installed_library):
        response = client.get("/api/library/search?year=2020-2021")
        body = response.get_json()
        assert all(2020 <= item["year"] <= 2021 for item in body["items"])

    def test_sort_year_asc(self, client, installed_library):
        response = client.get("/api/library/search?sort=year_asc")
        years = [item["year"] for item in response.get_json()["items"]]
        assert years == sorted(years)

    def test_pagination(self, client, installed_library):
        response = client.get("/api/library/search?page=1&page_size=2")
        body = response.get_json()
        assert len(body["items"]) == 2
        assert body["total"] == 6

    def test_poster_url_is_built_from_poster_path(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影一")
        item = response.get_json()["items"][0]
        assert item["poster_url"] == "https://image.tmdb.org/t/p/w342/poster1.jpg"
        assert "poster_path" not in item

    @pytest.mark.parametrize(
        "query",
        ["page=0", "page=201", "page_size=0", "page_size=51", "type=bogus", "year=abcd", "year=2020-abcd", "sort=bogus"],
    )
    def test_invalid_params_return_400(self, client, installed_library, query):
        response = client.get("/api/library/search?" + query)
        assert response.status_code == 400

    def test_sql_injection_like_query_returns_empty_not_everything(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "%25%27%20OR%201%3D1%20--")
        assert response.status_code == 200
        assert response.get_json()["total"] == 0


class TestFiltersRoute:
    def test_returns_dimensions(self, client, installed_library):
        response = client.get("/api/library/filters")
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        for key in ("types", "years", "providers", "qualities", "hdr", "genres"):
            assert key in body

    def test_response_never_contains_markers(self, client, installed_library, library_markers):
        response = client.get("/api/library/filters")
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        assert _has_marker(blob, library_markers) is None


class TestSuggestRoute:
    def test_short_query_returns_empty(self, client, installed_library):
        response = client.get("/api/library/suggest?q=a")
        assert response.status_code == 200
        assert response.get_json()["items"] == []

    def test_matching_query_returns_items(self, client, installed_library):
        response = client.get("/api/library/suggest?q=" + "虚构电影一")
        body = response.get_json()
        assert body["success"] is True
        titles = [item["title"] for item in body["items"]]
        assert "虚构电影一" in titles

    def test_response_never_contains_markers(self, client, installed_library, library_markers):
        response = client.get("/api/library/suggest?q=" + "虚构")
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        assert _has_marker(blob, library_markers) is None


class TestMediaRoute:
    def test_unknown_id_is_404(self, client, installed_library):
        response = client.get("/api/library/media/999999")
        assert response.status_code == 404

    def test_known_id_returns_flattened_media(self, client, installed_library):
        media_id = installed_library.media_detail  # sanity: method exists
        response = client.get("/api/library/media/1")
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert "title" in body
        assert "groups" in body

    def test_response_never_contains_markers(self, client, installed_library, library_markers):
        response = client.get("/api/library/media/1")
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        assert _has_marker(blob, library_markers) is None


class TestResourceRoute:
    def test_unknown_id_is_404(self, client, installed_library):
        response = client.get("/api/library/resource/999999")
        assert response.status_code == 404

    def test_known_id_returns_group(self, client, installed_library):
        response = client.get("/api/library/resource/1")
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert "links" in body

    def test_response_never_contains_markers(self, client, installed_library, library_markers):
        response = client.get("/api/library/resource/1")
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        assert _has_marker(blob, library_markers) is None


class TestTmdbStatusRoute:
    def test_fields_are_complete_and_no_api_key(self, client, installed_library, hidrive):
        hidrive.secret_set("tmdb_api_key", "SECRET-KEY-MARKER")
        response = client.get("/api/library/tmdb-status")
        assert response.status_code == 200
        body = response.get_json()
        for key in (
            "installed", "schema_version", "built_at", "installed_at", "media_total",
            "links_total", "match", "budget", "cache", "key_configured", "enricher", "enrich_enabled",
            "configured_budget", "effective_budget", "cap_source", "used", "remaining", "reset_at",
            "worker_state", "last_heartbeat", "last_round_at", "last_round_processed", "last_error",
            "cache_count", "exact_count", "candidate_count", "needs_review_count", "unmatched_count",
            # T14/§3.1, §4-§5.1, fix wave 1 (finding #4): the three
            # review-backlog buckets + their total, and the persisted
            # 429/budget/error accounting.
            "review_pending_unqueried", "review_scored", "review_no_candidate", "needs_review_total",
            "tmdb_budget_used", "tmdb_budget_remaining", "tmdb_requests_429", "tmdb_last_error_class",
            # T15 design item 4: hint counts (read-only) and the TVmaze
            # fallback setting's current value.
            "hints_total", "hints_by_decision", "hints_pending", "tvmaze_hint_enabled",
        ):
            assert key in body, key
        assert body["key_configured"] is True
        assert body["media_total"] == 6
        blob = json.dumps(body, ensure_ascii=False)
        assert "api_key" not in blob
        assert "SECRET-KEY-MARKER" not in blob
        assert "http" not in blob

    def test_response_never_contains_markers(self, client, installed_library, library_markers):
        response = client.get("/api/library/tmdb-status")
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        assert _has_marker(blob, library_markers) is None

    def test_review_pending_and_scored_counts_split_needs_review(self, client, installed_library):
        # T14/§3.1: the fixture's one needs_review row ("虚构剧集一") has no
        # match_score/candidates yet -- it must count as pending-unqueried,
        # not scored.
        body = client.get("/api/library/tmdb-status").get_json()
        assert body["needs_review_count"] == 1
        assert body["review_pending_unqueried"] == 1
        assert body["review_scored"] == 0
        assert body["review_no_candidate"] == 0
        assert body["needs_review_total"] == 1

    def test_review_no_candidate_counts_a_scored_row_with_empty_candidates(self, client, installed_library):
        # Fix wave 1/finding #4: a requeued row that comes back with zero
        # candidates gets match_score=0.0 and match_candidates_json='[]' --
        # it must be counted separately from rows that have a real
        # candidate to confirm, not lumped into review_scored.
        with installed_library.connect() as conn:
            conn.execute(
                "UPDATE media SET match_score=0.0, match_candidates_json='[]' WHERE media_identity='fp:tv:900003'"
            )
            conn.commit()

        body = client.get("/api/library/tmdb-status").get_json()
        assert body["review_pending_unqueried"] == 0
        assert body["review_scored"] == 0
        assert body["review_no_candidate"] == 1
        assert body["needs_review_total"] == 1

    def test_tmdb_prefixed_accounting_fields_mirror_the_existing_ones(self, client, installed_library):
        # T14/§4: the new tmdb_-prefixed keys must agree with the existing
        # used/remaining/last_error keys they're derived from.
        body = client.get("/api/library/tmdb-status").get_json()
        assert body["tmdb_budget_used"] == body["used"]
        assert body["tmdb_budget_remaining"] == body["remaining"]
        assert body["tmdb_requests_429"] == 0
        assert body["tmdb_last_error_class"] == body["last_error"]

    def test_tvmaze_hint_enabled_defaults_off(self, client, installed_library):
        assert client.get("/api/library/tmdb-status").get_json()["tvmaze_hint_enabled"] is False

    def test_hints_fields_reflect_tmdb_hints_table(self, client, installed_library):
        with installed_library.connect() as conn:
            conn.execute(
                "SELECT media_identity FROM media LIMIT 1"
            )
            identity = conn.execute("SELECT media_identity FROM media LIMIT 1").fetchone()[0]
            conn.execute(
                "INSERT INTO tmdb_hints (media_identity, source, decision, candidate_count, candidates_json, generated_at) "
                "VALUES (?, 'imdb_offline', 'proposed_exact', 1, '[]', 0)",
                (identity,),
            )
            conn.commit()

        body = client.get("/api/library/tmdb-status").get_json()
        assert body["hints_total"] == 1
        assert body["hints_by_decision"] == {"proposed_exact": 1}
        assert isinstance(body["hints_pending"], int)

    def test_enrich_enabled_defaults_true_and_tracks_setting(self, client, installed_library, hidrive):
        # I4: the frontend needs a way to seed its checkbox from the
        # server's actual tmdb_enrich_enabled setting (default: enabled,
        # same default as _library_enrich_enabled_check).
        assert client.get("/api/library/tmdb-status").get_json()["enrich_enabled"] is True
        hidrive.setting_set("tmdb_enrich_enabled", "0")
        assert client.get("/api/library/tmdb-status").get_json()["enrich_enabled"] is False

    def test_worker_state_not_started_wins_over_paused_when_no_heartbeat_exists(self, client, installed_library, hidrive):
        # Review fix #5: not_started takes priority over paused -- with no
        # persisted heartbeat at all, the enricher has simply never run
        # yet, regardless of whether a key is configured.
        assert client.get("/api/library/tmdb-status").get_json()["worker_state"] == "not_started"
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        # Key configured, enabled, installed, but the enricher has still
        # never run (still no persisted heartbeat) -> still not_started.
        assert client.get("/api/library/tmdb-status").get_json()["worker_state"] == "not_started"

    def test_worker_state_is_paused_once_a_heartbeat_exists_but_key_is_missing(self, client, installed_library, hidrive):
        # Once a heartbeat has been persisted (the enricher ran at some
        # point), an administratively-disabled config (here: no TMDB key)
        # reports paused, not running/stale -- see derive_worker_state's
        # docstring for why paused must win once a heartbeat exists.
        conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
        hidrive.library_tmdb.ensure_tables(conn)
        now = int(time.time())
        conn.execute(
            "INSERT INTO tmdb_enricher_state (key, value, updated_at) VALUES ('heartbeat_at', ?, ?)",
            (str(now), now),
        )
        conn.commit()
        conn.close()

        assert client.get("/api/library/tmdb-status").get_json()["worker_state"] == "paused"

    def test_worker_state_honors_local_enrichers_idle_seconds_property(self, client, installed_library, hidrive):
        # Review fix #7: the status endpoint must read idle_seconds back
        # through BackgroundEnricher's public property, not the private
        # _idle_seconds attribute, when a local enricher exists.
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
        hidrive.library_tmdb.ensure_tables(conn)
        old_heartbeat = int(time.time()) - 5
        conn.execute(
            "INSERT INTO tmdb_enricher_state (key, value, updated_at) VALUES ('heartbeat_at', ?, ?)",
            (str(old_heartbeat), old_heartbeat),
        )
        conn.commit()
        conn.close()

        lock_paths = hidrive._tmdb_lock_paths()
        hidrive._library_enricher = hidrive.library_tmdb.BackgroundEnricher(
            lambda: None, lambda: None,
            leader_lock_path=lock_paths.leader, run_lock_path=lock_paths.run,
            conn_factory=lambda: sqlite3.connect(str(hidrive.LIBRARY_DB_PATH)),
            idle_seconds=1,
        )
        try:
            body = client.get("/api/library/tmdb-status").get_json()
            # A 5s-old heartbeat with idle_seconds=1 (3*1=3s threshold) is
            # stale -- it would read as "running" if the endpoint fell back
            # to the hardcoded 60s default instead of this enricher's own
            # configured idle_seconds.
            assert body["worker_state"] == "stale"
        finally:
            hidrive._library_enricher = None

    def test_paused_reason_null_when_running(self, client, installed_library, hidrive):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
        hidrive.library_tmdb.ensure_tables(conn)
        now = int(time.time())
        conn.execute(
            "INSERT INTO tmdb_enricher_state (key, value, updated_at) VALUES ('heartbeat_at', ?, ?)",
            (str(now), now),
        )
        conn.commit()
        conn.close()

        body = client.get("/api/library/tmdb-status").get_json()
        assert body["worker_state"] == "running"
        assert body["paused_reason"] is None

    def test_paused_reason_disabled_when_switch_off(self, client, installed_library, hidrive):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        hidrive.setting_set("tmdb_enrich_enabled", "0")
        assert client.get("/api/library/tmdb-status").get_json()["paused_reason"] == "disabled"

    def test_paused_reason_key_missing_when_no_key_configured(self, client, installed_library):
        assert client.get("/api/library/tmdb-status").get_json()["paused_reason"] == "key_missing"

    def test_paused_reason_no_heartbeat_when_fully_configured_but_never_run(self, client, installed_library, hidrive):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        body = client.get("/api/library/tmdb-status").get_json()
        assert body["worker_state"] == "not_started"
        assert body["paused_reason"] == "no_heartbeat"

    def test_paused_reason_stale_when_heartbeat_is_old(self, client, installed_library, hidrive):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
        hidrive.library_tmdb.ensure_tables(conn)
        old_heartbeat = int(time.time()) - 10_000
        conn.execute(
            "INSERT INTO tmdb_enricher_state (key, value, updated_at) VALUES ('heartbeat_at', ?, ?)",
            (str(old_heartbeat), old_heartbeat),
        )
        conn.commit()
        conn.close()

        body = client.get("/api/library/tmdb-status").get_json()
        assert body["worker_state"] == "stale"
        assert body["paused_reason"] == "stale"

    def test_total_media_and_matched_total_mirror_existing_fields(self, client, installed_library):
        body = client.get("/api/library/tmdb-status").get_json()
        assert body["total_media"] == body["media_total"]
        assert body["matched_total"] == body["exact_count"]

    def test_get_status_performs_no_database_writes(self, client, installed_library, hidrive):
        conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
        try:
            before = conn.execute("PRAGMA data_version").fetchone()[0]
        finally:
            conn.close()

        response = client.get("/api/library/tmdb-status")
        assert response.status_code == 200

        conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
        try:
            after = conn.execute("PRAGMA data_version").fetchone()[0]
        finally:
            conn.close()
        assert after == before


class TestSettingsWiring:
    def test_status_reports_library_installed_and_media_total(self, client, installed_library):
        response = client.get("/api/status")
        body = response.get_json()
        assert body["library"] == {"installed": True, "media_total": 6}

    def test_status_reports_library_not_installed(self, client, hidrive, workspace):
        response = client.get("/api/status")
        body = response.get_json()
        assert body["library"] == {"installed": False, "media_total": 0}

    def test_settings_stores_tmdb_budget_and_enrich_flag(self, client, hidrive):
        response = client.post("/api/settings", json={"tmdb_daily_budget": 500, "tmdb_enrich_enabled": False})
        assert response.status_code == 200
        assert hidrive.setting_get("tmdb_daily_budget") == "500"
        assert hidrive.setting_get("tmdb_enrich_enabled") == "0"

        client.post("/api/settings", json={"tmdb_enrich_enabled": True})
        assert hidrive.setting_get("tmdb_enrich_enabled") == "1"

    def test_settings_budget_response_reports_configured_and_effective(self, client, hidrive, installed_library):
        # B.4/F.5: a setting above the old hard-coded 300 default must not
        # be clamped, and the response must surface it so the frontend can
        # seed its input from the *configured* value, not the effective one.
        response = client.post("/api/settings", json={"tmdb_daily_budget": 800})
        assert response.status_code == 200
        assert response.get_json()["tmdb"] == {"configured_budget": 800, "effective_budget": 800, "cap_source": "setting"}

        status = client.get("/api/library/tmdb-status").get_json()
        assert status["configured_budget"] == 800
        assert status["effective_budget"] == 800
        assert status["cap_source"] == "setting"

    def test_settings_budget_response_reflects_env_cap(self, client, hidrive, installed_library, monkeypatch):
        client.post("/api/settings", json={"tmdb_daily_budget": 800})
        monkeypatch.setenv("TMDB_DAILY_BUDGET", "300")
        status = client.get("/api/library/tmdb-status").get_json()
        assert status["configured_budget"] == 800
        assert status["effective_budget"] == 300
        assert status["cap_source"] == "env"

    def test_settings_budget_response_present_even_when_library_not_installed(self, client, hidrive):
        response = client.post("/api/settings", json={"tmdb_daily_budget": 400})
        assert response.status_code == 200
        assert response.get_json()["tmdb"] == {"configured_budget": 400, "effective_budget": 400, "cap_source": "setting"}

    def test_settings_response_omits_tmdb_when_budget_not_in_request(self, client, hidrive):
        response = client.post("/api/settings", json={"tmdb_enrich_enabled": True})
        assert response.status_code == 200
        assert "tmdb" not in response.get_json()

    @pytest.mark.parametrize("value", [0, 5001, -1, "abc"])
    def test_settings_rejects_out_of_range_budget(self, client, hidrive, value):
        response = client.post("/api/settings", json={"tmdb_daily_budget": value})
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("0", "0"),
            ("1", "1"),
            ("true", "1"),
            ("false", "0"),
            ("TRUE", "1"),
            ("False", "0"),
        ],
    )
    def test_settings_accepts_wire_format_enrich_strings(self, client, hidrive, value, expected):
        # T4 fix #1: the wire format is the literal strings "0"/"1" (also
        # "true"/"false", case-insensitively) -- the old code did
        # `"1" if body[...] else "0"`, which treated the non-empty string
        # "0" as truthy and always stored "1".
        response = client.post("/api/settings", json={"tmdb_enrich_enabled": value})
        assert response.status_code == 200
        assert hidrive.setting_get("tmdb_enrich_enabled") == expected

    def test_settings_rejects_invalid_enrich_value(self, client, hidrive):
        response = client.post("/api/settings", json={"tmdb_enrich_enabled": "maybe"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "BAD_REQUEST"
        assert hidrive.setting_get("tmdb_enrich_enabled") is None

    def test_settings_stores_tvmaze_hint_enabled(self, client, hidrive):
        # T15/z1-hints §3: default off, accepted like tmdb_enrich_enabled.
        assert hidrive.setting_get("tvmaze_hint_enabled") is None
        response = client.post("/api/settings", json={"tvmaze_hint_enabled": True})
        assert response.status_code == 200
        assert hidrive.setting_get("tvmaze_hint_enabled") == "1"

        client.post("/api/settings", json={"tvmaze_hint_enabled": "0"})
        assert hidrive.setting_get("tvmaze_hint_enabled") == "0"

    def test_settings_rejects_invalid_tvmaze_hint_enabled(self, client, hidrive):
        response = client.post("/api/settings", json={"tvmaze_hint_enabled": "maybe"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "BAD_REQUEST"
        assert hidrive.setting_get("tvmaze_hint_enabled") is None


class TestBackgroundEnricherAutostart:
    def test_disabled_by_default_does_not_start(self, client, hidrive, monkeypatch):
        calls = []
        monkeypatch.setattr(hidrive.library_tmdb.BackgroundEnricher, "start", lambda self: calls.append(1))
        client.get("/api/status")
        client.get("/api/status")
        assert calls == []

    def test_enabled_starts_exactly_once(self, client, hidrive, monkeypatch):
        monkeypatch.setenv("LIBRARY_ENRICH_AUTOSTART", "1")
        calls = []
        monkeypatch.setattr(hidrive.library_tmdb.BackgroundEnricher, "start", lambda self: calls.append(1))
        client.get("/api/status")
        client.get("/api/status")
        client.get("/api/status")
        assert len(calls) == 1


class _FakeTmdbResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeTmdbSession:
    """Minimal fake ``requests``-shaped session: one canned response per
    URL, regardless of query params (mirrors the FakeSession in
    tests/test_library_tmdb.py, trimmed to what this integration test
    needs). ``route()`` also accepts a list of outcomes (consumed one per
    call, in order) or an ``Exception`` instance to raise -- needed by the
    T10 tmdb-check network-failure tests below."""

    def __init__(self):
        self._routes: dict[str, object] = {}
        self.calls: list[dict] = []

    def route(self, url, outcome):
        self._routes[url] = outcome
        return self

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        outcome = self._routes.get(url)
        if outcome is None:
            raise AssertionError(f"unscripted TMDB request to {url}")
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestRealEnricherWiring:
    """T1 P0 regression: production wiring (three distinct lock files, a
    real store/client and a real background thread) must complete a round
    without deadlocking -- the original bug had the enricher's leader lock
    and its client's budget lock pointing at the same file."""

    def test_real_enricher_wiring_completes_first_round_without_deadlock(
        self, client, hidrive, installed_library, monkeypatch
    ):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")

        session = _FakeTmdbSession()
        movie_url = hidrive.library_tmdb.TMDB_BASE + "/search/movie"
        tv_url = hidrive.library_tmdb.TMDB_BASE + "/search/tv"
        genre_movie_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        # "虚构电影二" (fp:movie:900002, year 2019) matches exactly; the
        # canned response is reused for "虚构电影三" (900005, year 2022)
        # too, where the year mismatch keeps it out of "exact".
        session.route(movie_url, _FakeTmdbResponse(200, {"results": [
            {"id": 900002, "title": "虚构电影二", "release_date": "2019-01-01"},
        ]}))
        session.route(tv_url, _FakeTmdbResponse(200, {"results": []}))
        session.route(genre_movie_url, _FakeTmdbResponse(200, {"genres": []}))
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        enricher = hidrive._build_library_enricher()
        enricher._round_seconds = 0.05
        enricher._idle_seconds = 0.05
        enricher.start()
        try:
            def _round_completed() -> bool:
                conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
                try:
                    state = hidrive.library_tmdb.read_enricher_state(conn)
                finally:
                    conn.close()
                return bool(state.get("last_round_at"))

            _wait_until(_round_completed, timeout=2.0)
        finally:
            enricher.stop(timeout=2)

        response = client.get("/api/library/tmdb-status")
        assert response.status_code == 200
        body = response.get_json()
        assert body["used"] > 0
        assert body["cache_count"] > 0
        assert body["exact_count"] + body["needs_review_count"] + body["candidate_count"] > 0


class TestSharedTmdbRateLimiter:
    """T14 fix wave 1 (finding #2): every ``TmdbClient`` this process builds
    via ``_library_client_factory`` must share one ``RateLimiter``, or the
    background enricher's per-round client (a new ``TmdbClient`` every
    round) silently discards the adaptive 429 slow-down at the end of every
    round."""

    def test_successive_factory_clients_share_the_limiter_and_revert_after_cooldown(
        self, workspace, hidrive
    ):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")

        client_a = hidrive._library_client_factory()
        client_b = hidrive._library_client_factory()
        client_fast = hidrive._library_client_factory(fast=True)

        assert client_a._limiter is client_b._limiter
        assert client_fast._limiter is client_a._limiter

        limiter = client_a._limiter
        assert limiter._base_interval == 0.04  # DEFAULT_LIMITS.min_interval_ms == 40

        limiter.slow_down(250, 60)
        assert limiter._interval == 0.25
        # Simulate the 60s cooldown having already elapsed, then confirm the
        # very next wait() reverts pacing to the configured base interval.
        limiter._slowdown_until = limiter._clock() - 0.001
        limiter.wait()
        assert limiter._interval == pytest.approx(0.04)


# ===========================================================================
# T10: settings-page self-diagnosis -- tmdb-check (one real request through
# the production client wiring) and tmdb-enrich-now (one manual round,
# sharing enrich_batch/merge_by_tmdb and the run lock with the background
# thread and the --library-enrich CLI).
# ===========================================================================


class TestTmdbCheckRoute:
    def test_ok_path_returns_latency_and_genres_count(self, client, installed_library, hidrive, monkeypatch):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        session = _FakeTmdbSession()
        genre_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        session.route(genre_url, _FakeTmdbResponse(200, {"genres": [{"id": 1, "name": "剧情"}, {"id": 2, "name": "喜剧"}]}))
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        response = client.post("/api/library/tmdb-check")

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["ok"] is True
        assert body["genres_count"] == 2
        assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
        blob = json.dumps(body, ensure_ascii=False)
        assert "fake-test-key-not-real" not in blob
        assert "http" not in blob

    def test_check_bypasses_cache_and_reserves_budget_on_every_call(self, client, installed_library, hidrive, monkeypatch):
        # The check must always hit the network and spend one budget unit
        # -- unlike client.genres(), which is cache-first and would let a
        # warm cache silently report "connected" without talking to TMDB.
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        session = _FakeTmdbSession()
        genre_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        session.route(genre_url, _FakeTmdbResponse(200, {"genres": []}))
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        before_used = client.get("/api/library/tmdb-status").get_json()["used"]
        client.post("/api/library/tmdb-check")
        client.post("/api/library/tmdb-check")
        after_used = client.get("/api/library/tmdb-status").get_json()["used"]

        assert after_used - before_used == 2
        assert len(session.calls) == 2

    def test_connection_error_maps_to_network_hint(self, client, installed_library, hidrive, monkeypatch):
        # T10 fix wave 1: tmdb-check must use the "fast" client
        # (request_timeout=4, max_retries=0) -- without it, the production
        # wiring's real (uninjected) sleep would let the default retry
        # policy's 1s/2s/4s backoff block this one request for ~7s, and a
        # genuinely stuck TCP attempt for up to ~67s (4 attempts x 15s +
        # backoff), risking gunicorn's 30s worker timeout. Only one of the
        # four routed exceptions should ever be consumed.
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        session = _FakeTmdbSession()
        genre_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        session.route(genre_url, [requests.ConnectionError("boom") for _ in range(4)])
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        started = time.monotonic()
        response = client.post("/api/library/tmdb-check")
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["ok"] is False
        assert body["error_class"] == "ConnectionError"
        assert body["hint"] == "无法连接 TMDB：请检查服务器网络或代理设置"
        assert elapsed < 3.0
        assert len(session.calls) == 1

    def test_proxy_error_maps_to_network_hint(self, client, installed_library, hidrive, monkeypatch):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        session = _FakeTmdbSession()
        genre_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        session.route(genre_url, requests.exceptions.ProxyError("boom"))
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        response = client.post("/api/library/tmdb-check")

        body = response.get_json()
        assert body["ok"] is False
        assert body["error_class"] == "ProxyError"
        assert body["hint"] == "无法连接 TMDB：请检查服务器网络或代理设置"

    def test_ssl_error_maps_to_network_hint(self, client, installed_library, hidrive, monkeypatch):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        session = _FakeTmdbSession()
        genre_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        session.route(genre_url, requests.exceptions.SSLError("boom"))
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        response = client.post("/api/library/tmdb-check")

        body = response.get_json()
        assert body["ok"] is False
        assert body["error_class"] == "SSLError"
        assert body["hint"] == "无法连接 TMDB：请检查服务器网络或代理设置"

    def test_uses_fast_client_with_no_retries_and_short_timeout(self, client, installed_library, hidrive, monkeypatch):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        session = _FakeTmdbSession()
        genre_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        session.route(genre_url, _FakeTmdbResponse(200, {"genres": []}))
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        original_factory = hidrive._library_client_factory
        captured = {}

        def _spy(*, fast=False):
            captured["fast"] = fast
            client_obj = original_factory(fast=fast)
            captured["request_timeout"] = client_obj._request_timeout
            captured["max_retries"] = client_obj._max_retries
            return client_obj

        monkeypatch.setattr(hidrive, "_library_client_factory", _spy)

        response = client.post("/api/library/tmdb-check")

        assert response.status_code == 200
        assert captured["fast"] is True
        assert captured["request_timeout"] == 4.0
        assert captured["max_retries"] == 0

    def test_invalid_api_key_maps_to_key_hint(self, client, installed_library, hidrive, monkeypatch):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        session = _FakeTmdbSession()
        genre_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        session.route(genre_url, _FakeTmdbResponse(401))
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        response = client.post("/api/library/tmdb-check")

        body = response.get_json()
        assert body["ok"] is False
        assert body["error_class"] == "InvalidApiKey"
        assert "v3 auth key" in body["hint"]
        blob = json.dumps(body, ensure_ascii=False)
        assert "fake-test-key-not-real" not in blob

    def test_budget_exhausted_maps_to_budget_hint(self, client, installed_library, hidrive):
        client.post("/api/settings", json={"tmdb_daily_budget": 1})
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        budget = hidrive.library_tmdb.Budget(
            lambda: sqlite3.connect(str(hidrive.LIBRARY_DB_PATH)), hidrive._tmdb_lock_paths().budget, 1
        )
        budget.reserve()

        response = client.post("/api/library/tmdb-check")

        body = response.get_json()
        assert body["ok"] is False
        assert body["error_class"] == "BudgetExhausted"
        assert "今日额度已用完" in body["hint"]

    def test_no_key_returns_400(self, client, installed_library):
        response = client.post("/api/library/tmdb-check")
        assert response.status_code == 400
        assert response.get_json()["code"] == "TMDB_KEY_MISSING"

    def test_not_installed_returns_503(self, client, hidrive, workspace):
        response = client.post("/api/library/tmdb-check")
        assert response.status_code == 503
        assert response.get_json()["code"] == "LIBRARY_NOT_INSTALLED"


class TestTmdbEnrichNowRoute:
    def test_ok_path_returns_stats_and_merges(self, client, installed_library, hidrive, monkeypatch):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        session = _FakeTmdbSession()
        movie_url = hidrive.library_tmdb.TMDB_BASE + "/search/movie"
        tv_url = hidrive.library_tmdb.TMDB_BASE + "/search/tv"
        genre_movie_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        session.route(movie_url, _FakeTmdbResponse(200, {"results": [
            {"id": 900002, "title": "虚构电影二", "release_date": "2019-01-01"},
        ]}))
        session.route(tv_url, _FakeTmdbResponse(200, {"results": []}))
        session.route(genre_movie_url, _FakeTmdbResponse(200, {"genres": []}))
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        before_used = client.get("/api/library/tmdb-status").get_json()["used"]
        response = client.post("/api/library/tmdb-enrich-now")

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        stats = body["stats"]
        for key in ("candidates_considered", "requests_made", "matched_exact", "matched_candidate", "needs_review", "error_class"):
            assert key in stats
        assert stats["matched_exact"] >= 1
        assert stats["requests_made"] > 0
        after_used = client.get("/api/library/tmdb-status").get_json()["used"]
        assert after_used > before_used
        blob = json.dumps(body, ensure_ascii=False)
        assert "fake-test-key-not-real" not in blob
        assert "http" not in blob

    def test_uses_fast_client_limit_5_and_a_6s_deadline(self, client, installed_library, hidrive, monkeypatch):
        # T10 fix wave 1: tmdb-enrich-now must go through the "fast" client
        # (request_timeout=4, max_retries=0), a lowered limit=5 and a ~6s
        # deadline -- without this a slow/flaky round could compound past
        # gunicorn's 30s worker timeout (see enrich_batch's/the route's
        # docstrings for the worst-case math). Stubbing out enrich_batch
        # itself keeps this test instant regardless of the deadline's value.
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        captured = {}

        def _fake_enrich_batch(store, tmdb_client, *, limit=20, with_details=False, deadline=None, tvmaze_enabled=False):
            captured["limit"] = limit
            captured["deadline"] = deadline
            captured["request_timeout"] = tmdb_client._request_timeout
            captured["max_retries"] = tmdb_client._max_retries
            return hidrive.library_tmdb.EnrichStats()

        monkeypatch.setattr(hidrive.library_tmdb, "enrich_batch", _fake_enrich_batch)

        before = time.monotonic()
        response = client.post("/api/library/tmdb-enrich-now")
        after = time.monotonic()

        assert response.status_code == 200
        assert captured["limit"] == 5
        assert captured["request_timeout"] == 4.0
        assert captured["max_retries"] == 0
        assert captured["deadline"] is not None
        assert before + 5 <= captured["deadline"] <= after + 7

    def test_busy_returns_409_promptly(self, client, installed_library, hidrive):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        lock_paths = hidrive._tmdb_lock_paths()
        lock_paths.run.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_paths.run, "a+")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            started = time.time()
            response = client.post("/api/library/tmdb-enrich-now")
            elapsed = time.time() - started
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

        assert response.status_code == 409
        assert response.get_json()["code"] == "TMDB_ENRICH_BUSY"
        assert elapsed < 1.0

    def test_no_key_returns_400(self, client, installed_library):
        response = client.post("/api/library/tmdb-enrich-now")
        assert response.status_code == 400
        assert response.get_json()["code"] == "TMDB_KEY_MISSING"

    def test_not_installed_returns_503(self, client, hidrive, workspace):
        response = client.post("/api/library/tmdb-enrich-now")
        assert response.status_code == 503
        assert response.get_json()["code"] == "LIBRARY_NOT_INSTALLED"

    def test_allowed_even_when_enrich_enabled_is_off(self, client, installed_library, hidrive, monkeypatch):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        hidrive.setting_set("tmdb_enrich_enabled", "0")
        session = _FakeTmdbSession()
        movie_url = hidrive.library_tmdb.TMDB_BASE + "/search/movie"
        tv_url = hidrive.library_tmdb.TMDB_BASE + "/search/tv"
        genre_movie_url = hidrive.library_tmdb.TMDB_BASE + "/genre/movie/list"
        session.route(movie_url, _FakeTmdbResponse(200, {"results": []}))
        session.route(tv_url, _FakeTmdbResponse(200, {"results": []}))
        session.route(genre_movie_url, _FakeTmdbResponse(200, {"genres": []}))
        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: session)

        response = client.post("/api/library/tmdb-enrich-now")
        assert response.status_code == 200


@pytest.fixture
def access_mode(hidrive, monkeypatch):
    monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
    monkeypatch.setattr(
        hidrive,
        "verify_access_jwt",
        lambda token: {"sub": "owner", "email": "owner@example.test"} if token == "valid-assertion" else (_ for _ in ()).throw(PermissionError("bad")),
    )
    return hidrive


class TestTmdbRoutesRequireCsrf:
    """Same Access + CSRF guard as every other write route (see
    test_access_guard.py's equivalent reveal/transfer checks) -- checked
    without an installed library, since before_request's CSRF check runs
    before the route handler ever executes."""

    def test_tmdb_check_requires_csrf(self, client, access_mode):
        response = client.post("/api/library/tmdb-check", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"})
        assert response.status_code == 403

    def test_tmdb_enrich_now_requires_csrf(self, client, access_mode):
        response = client.post("/api/library/tmdb-enrich-now", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"})
        assert response.status_code == 403


# ===========================================================================
# T4.4 reveal + server-side 115 transfer
# ===========================================================================

USER_URL = "https://my.115.com/?ct=ajax&ac=get_user_aq"
SNAP_URL = "https://webapi.115.com/share/snap"
RECEIVE_URL = "https://webapi.115.com/share/receive"


def _route_115_session(http, *, uid="42"):
    http.route("GET", USER_URL, {"state": True, "data": {"uid": uid}})


class TestRevealRoute:
    def test_non_115_returns_url_and_code(self, client, installed_library):
        response = client.post("/api/library/link/pub-fixture-01".replace("01", "02") + "/reveal")
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["provider"] == "quark"
        assert body["url"] == "https://pan.quark.cn/s/swfake200002"
        assert body["access_code"] is None

    def test_115_returns_403(self, client, installed_library):
        response = client.post("/api/library/link/pub-fixture-01/reveal")
        assert response.status_code == 403
        assert response.get_json()["code"] == "LINK_PROVIDER_115"

    def test_unknown_non_url_returns_400(self, client, installed_library):
        response = client.post("/api/library/link/pub-fixture-10/reveal")
        assert response.status_code == 400
        assert response.get_json()["code"] == "LINK_NOT_REVEALABLE"

    def test_unknown_with_url_returns_200(self, client, installed_library):
        # T4 fix #3 (plan §4.4): an "unknown" link with a real URL of a
        # host not in the provider allowlist is still revealable -- only
        # non-URL text ("无效链接") is LINK_NOT_REVEALABLE.
        response = client.post("/api/library/link/pub-fixture-12/reveal")
        assert response.status_code == 200
        body = response.get_json()
        assert body["provider"] == "unknown"
        assert body["url"] == "https://example-netdisk.com/s/swfake110011"

    def test_unknown_with_url_actions_are_open_copy(self, client, installed_library):
        response = client.get("/api/library/resource/5")
        assert response.status_code == 200
        link = next(l for l in response.get_json()["links"] if l["link_id"] == "pub-fixture-12")
        assert link["actions"] == ["open", "copy"]

    def test_unknown_non_url_actions_are_empty(self, client, installed_library):
        response = client.get("/api/library/resource/5")
        assert response.status_code == 200
        link = next(l for l in response.get_json()["links"] if l["link_id"] == "pub-fixture-10")
        assert link["actions"] == []

    def test_unknown_id_returns_404(self, client, installed_library):
        response = client.post("/api/library/link/does-not-exist/reveal")
        assert response.status_code == 404

    def test_audit_row_contains_public_id_not_url(self, client, installed_library, audit_rows):
        client.post("/api/library/link/pub-fixture-02/reveal")
        rows = audit_rows("library.reveal")
        assert len(rows) == 1
        assert "pub-fixture-02" in rows[0]["detail"]
        assert "quark.cn" not in rows[0]["detail"]
        assert "swfake" not in rows[0]["detail"]

    def test_response_never_leaks_other_markers(self, client, installed_library, library_markers):
        response = client.post("/api/library/link/pub-fixture-02/reveal")
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        # the revealed link's OWN url is legitimately present; every OTHER
        # marker (other links' urls/codes) must not leak through.
        own_url = "https://pan.quark.cn/s/swfake200002"
        other_markers = [m for m in library_markers if m != own_url]
        assert _has_marker(blob, other_markers) is None

    def test_dangerous_scheme_is_refused_even_when_mislabeled(self, client, hidrive, workspace):
        # C1 defense in depth: api_library_reveal must not trust
        # provider/url_label alone -- a row written before the import-time
        # allowlist fix (or by any other path) could still carry a
        # dangerous-scheme URL under an innocuous provider/label. The
        # actual decrypted URL's scheme must be checked too.
        bundle_path = workspace / "bad-scheme-bundle.sqlite"
        bundle_store = ls.LibraryStore(bundle_path)
        bundle_store.create_schema()
        media_id = bundle_store.upsert_media(ls.MediaRecord(
            media_identity="fp:movie:badscheme1", media_type="movie", title_zh="坏方案测试",
            search_key="坏方案测试", match_status="unmatched",
        ))
        group_id = bundle_store.upsert_group(ls.GroupRecord(
            media_id=media_id, edition_fingerprint="fp-badscheme1", display_title="测试",
        ))
        bad_url = "javascript://evil.example/%0aalert(1)"
        bundle_store.upsert_link(ls.LinkRecord(
            public_id="pub-bad-scheme-01",
            group_id=group_id,
            provider="quark",  # deliberately mislabeled, simulating stale/legacy data
            canonical_url_hash=_hash(bad_url),
            url_label="夸克网盘 分享 · 测试",
            url_plain=bad_url,
            access_code_plain=None,
            has_access_code=0,
            remark="",
            created_at_source=int(time.time()),
        ))
        bundle_store.recount()
        lse.build_index(bundle_store)
        bundle_store.meta_set("normalize_version", "test-1")
        bundle_store.meta_set("built_at", str(int(time.time())))
        bundle_store.meta_set("source_hashes", "{}")
        bundle_store.meta_set("encrypted", "0")

        ls.install_bundle(bundle_path, hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())

        response = client.post("/api/library/link/pub-bad-scheme-01/reveal")
        assert response.status_code == 400
        assert response.get_json()["code"] == "LINK_NOT_REVEALABLE"


class TestTransferRoute:
    @pytest.fixture(autouse=True)
    def _cookie(self, hidrive, workspace):
        # depends on `workspace` explicitly (not just `hidrive`) so this
        # runs AFTER the per-test DB_PATH monkeypatch, not before it.
        hidrive.secret_set("115_cookie", "session=fixture-cookie-not-real")
        # T17 fix wave 1 item 1: most tests below submit a bare target_pid
        # of "1" -- since a client-supplied target_pid without target_path
        # now needs server-side proof (it must match the stored default or
        # the configured /115pan root cid), "1" is established here as that
        # stored default so those tests keep exercising the rest of the
        # transfer flow unchanged. Tests that need a DIFFERENT pid override
        # this setting themselves right before their request.
        hidrive.setting_set("115_target_pid", "1")

    def test_transfer_uses_column_access_code(self, client, installed_library, http, audit_rows, hidrive):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})
        # T17 fix wave 1 item 1: this test's own target_pid differs from the
        # class-wide stored default -- proving it needs its own override.
        hidrive.setting_set("115_target_pid", "999")

        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "999"})

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        snap_call = http.calls_to(SNAP_URL)[0]
        assert snap_call["params"]["share_code"] == "swfake100001"
        assert snap_call["params"]["receive_code"] == "ab12"
        (receive,) = http.calls_to(RECEIVE_URL)
        assert receive["data"]["cid"] == "999"
        assert audit_rows("library.transfer")[0]["status"] == "success"
        assert "pub-fixture-01" in audit_rows("library.transfer")[0]["detail"]

    def test_transfer_does_not_double_apply_password_already_in_url(self, client, installed_library, http):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})

        # link #11 (pub-fixture-11) is the 115 link whose URL already
        # embeds ?password=cd34 AND also has a matching column access code.
        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-11", "target_pid": "1"})

        assert response.status_code == 200
        snap_call = http.calls_to(SNAP_URL)[0]
        assert snap_call["params"]["share_code"] == "swfake900001"
        assert snap_call["params"]["receive_code"] == "cd34"

    def test_transfer_body_leaks_no_url_or_code(self, client, installed_library, http, library_markers):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})
        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        assert _has_marker(blob, library_markers) is None

    def test_audit_row_contains_no_url(self, client, installed_library, http, audit_rows):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})
        client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        detail = audit_rows("library.transfer")[0]["detail"]
        assert "115.com" not in detail
        assert "swfake" not in detail

    def test_non_115_link_returns_400(self, client, installed_library):
        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-02", "target_pid": "1"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "LINK_NOT_TRANSFERABLE"

    def test_dangerous_scheme_is_refused_even_when_mislabeled(self, client, hidrive, workspace):
        # C1 defense in depth (transfer path): a row whose provider is
        # (mis)labeled "115" but whose actual decrypted URL is not
        # http(s) must never reach save_115_link.
        bundle_path = workspace / "bad-scheme-transfer-bundle.sqlite"
        bundle_store = ls.LibraryStore(bundle_path)
        bundle_store.create_schema()
        media_id = bundle_store.upsert_media(ls.MediaRecord(
            media_identity="fp:movie:badscheme2", media_type="movie", title_zh="坏方案测试二",
            search_key="坏方案测试二", match_status="unmatched",
        ))
        group_id = bundle_store.upsert_group(ls.GroupRecord(
            media_id=media_id, edition_fingerprint="fp-badscheme2", display_title="测试",
        ))
        bad_url = "javascript://evil.example/%0aalert(1)"
        bundle_store.upsert_link(ls.LinkRecord(
            public_id="pub-bad-scheme-02",
            group_id=group_id,
            provider="115",  # deliberately mislabeled, simulating stale/legacy data
            canonical_url_hash=_hash(bad_url),
            url_label="115 分享 · 测试",
            url_plain=bad_url,
            access_code_plain=None,
            has_access_code=0,
            remark="",
            created_at_source=int(time.time()),
        ))
        bundle_store.recount()
        lse.build_index(bundle_store)
        bundle_store.meta_set("normalize_version", "test-1")
        bundle_store.meta_set("built_at", str(int(time.time())))
        bundle_store.meta_set("source_hashes", "{}")
        bundle_store.meta_set("encrypted", "0")

        ls.install_bundle(bundle_path, hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())

        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-bad-scheme-02", "target_pid": "1"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "LINK_NOT_TRANSFERABLE"

    def test_unknown_id_returns_404(self, client, installed_library):
        response = client.post("/api/library/transfer", json={"resource_link_id": "does-not-exist", "target_pid": "1"})
        assert response.status_code == 404

    def test_deleted_link_message_prefixed(self, client, installed_library, http):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})
        # pub-fixture-05 is the deleted baidu link -- wait, that's non-115.
        # Use a deleted 115 link instead: none of the synthetic fixtures are
        # both 115 and deleted, so mark one deleted directly for this test.
        with installed_library.connect() as conn:
            conn.execute("UPDATE resource_link SET deleted_at_source=? WHERE public_id='pub-fixture-01'", (1,))
            conn.commit()

        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})

        assert response.status_code == 200
        assert response.get_json()["message"].startswith("该分享在来源已标记删除，")

    def test_cookie_invalid_reports_reauth_required(self, client, installed_library, http):
        # T19: a rejected 115 session now maps to the explicit
        # 115_REAUTH_REQUIRED code (409), not a generic 502, so the
        # frontend can point the user at the QR re-auth flow instead of
        # a dead-end error.
        http.route("GET", USER_URL, {"state": False})
        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        assert response.status_code == 409
        body = response.get_json()
        assert body["code"] == "115_REAUTH_REQUIRED"
        assert body["message"] == "115 需要重新授权，请到设置页扫码"

    # -- T17 fix wave 1 item 3: the dedupe entry is recorded only once a
    # transfer was actually attempted upstream (share/receive issued) --
    # a failure that happened BEFORE that point must not occupy the 10s
    # window, and the 409 rejection itself must be audited.

    def test_reauth_failure_before_receive_clears_dedupe_entry(self, client, installed_library, http, hidrive):
        # A round-trip retry can't prove this directly: _115_transfer_gate
        # has its own, unrelated fast-fail cache (T19) that would keep
        # short-circuiting a second attempt straight to the same cached
        # 115_REAUTH_REQUIRED regardless of the dedupe fix -- so assert the
        # dedupe guard's own state directly instead.
        http.route("GET", USER_URL, {"state": False})
        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        assert response.status_code == 409
        assert response.get_json()["code"] == "115_REAUTH_REQUIRED"
        assert http.calls_to(RECEIVE_URL) == []
        assert hidrive._TRANSFER_DEDUPE_SEEN == {}

    def test_snap_failure_before_receive_does_not_block_immediate_retry(self, client, installed_library, http):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": False, "error": "raw upstream detail marker not-real"})
        first = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        assert first.status_code == 502
        assert http.calls_to(RECEIVE_URL) == []

        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})
        second = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        assert second.status_code == 200

    def test_duplicate_rejection_is_audited(self, client, installed_library, http, audit_rows):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})

        client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})

        assert response.status_code == 409
        rows = audit_rows("library.transfer")
        assert len(rows) == 2
        assert rows[0]["status"] == "success"
        assert rows[1]["status"] == "duplicate"
        assert "pub-fixture-01" in rows[1]["detail"]

    def test_target_path_out_of_bounds_returns_503(self, client, installed_library):
        response = client.post(
            "/api/library/transfer",
            json={"resource_link_id": "pub-fixture-01", "target_path": "/not-115pan/x"},
        )
        assert response.status_code == 503
        assert response.get_json()["code"] == "115_TARGET_RESOLVE_FAILED"

    # -- T17 fix wave 1 item 1: a bare target_pid with no target_path is no
    # longer trusted verbatim -- it must be proven server-side (matching the
    # stored default or the configured /115pan root cid) or the transfer is
    # rejected before any upstream call, including the 115 session check.

    def test_transfer_rejects_target_pid_without_target_path_or_proof(self, client, installed_library, http):
        response = client.post(
            "/api/library/transfer",
            json={"resource_link_id": "pub-fixture-01", "target_pid": "666"},
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == "TARGET_PID_INVALID"
        assert http.calls == []  # no verify, no snap, no receive

    def test_transfer_accepts_target_pid_matching_configured_115pan_root_cid(self, client, installed_library, http, hidrive):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})
        hidrive.setting_set("115_target_pid", "")  # only the root cid should prove "0" here
        hidrive.setting_set("115_open_root_cid", "0")

        response = client.post(
            "/api/library/transfer",
            json={"resource_link_id": "pub-fixture-01", "target_pid": "0"},
        )

        assert response.status_code == 200
        assert http.calls_to(RECEIVE_URL)[0]["data"]["cid"] == "0"

    def test_transfer_deadline_aborts_target_resolution_before_receive_when_budget_exhausted(self, client, installed_library, http, hidrive, monkeypatch):
        # N2: /api/library/transfer shares the same request-scoped deadline
        # mechanism as /api/115/save (see tests/test_115_save.py for the
        # detailed rationale) -- a slow OpenList listing must abort with a
        # fixed 504 TARGET_RESOLVE_TIMEOUT and never reach share/receive,
        # rather than resolving the target on a separate, independent
        # budget from save_115_link's.
        open_files_url = "https://proapi.115.com/open/ufile/files"
        monkeypatch.setattr(hidrive, "_115_REQUEST_DEADLINE_SECONDS", 0.05)
        hidrive.secret_set("115_open_access_token", "open-access-fixture")
        hidrive.setting_set("115_open_root_cid", "10")
        listings = {
            "10": [{"fn": "Movies", "fc": "0", "fid": "20"}],
            "20": [{"fn": "2024", "fc": "0", "fid": "30"}],
        }
        calls = {"n": 0}

        def slow_open_files(**kwargs):
            from conftest import FakeResponse
            import time as time_module

            calls["n"] += 1
            time_module.sleep(0.06)  # alone, already over the whole request budget
            return FakeResponse({"state": True, "data": listings[kwargs["params"]["cid"]]})

        http.route("GET", open_files_url, handler=slow_open_files)

        response = client.post(
            "/api/library/transfer",
            json={"resource_link_id": "pub-fixture-01", "target_path": "/115pan/Movies/2024"},
        )

        assert response.status_code == 504
        assert response.get_json()["code"] == "TARGET_RESOLVE_TIMEOUT"
        assert calls["n"] == 1  # the second segment's listing must never be attempted
        assert http.calls_to(RECEIVE_URL) == []

    def test_transfer_deadline_returns_not_attempted_when_resolution_leaves_too_little_budget(self, client, installed_library, http, hidrive, monkeypatch):
        # T19 wave 4 item 6: mirrors tests/test_115_save.py's equivalent --
        # /api/library/transfer shares the SAME request-scoped deadline
        # mechanism as /api/115/save. If resolution alone eats ~20s of the
        # 24s budget, verify and share/snap must still be attempted (each
        # with a shrunk timeout) but there must be too little budget left
        # for share/receive, which must never be attempted at all: a fixed
        # 504 TRANSFER_NOT_ATTEMPTED is returned instead.
        from conftest import FakeMonotonic, FakeResponse

        open_files_url = "https://proapi.115.com/open/ufile/files"
        clock = FakeMonotonic()
        monkeypatch.setattr(hidrive.time, "monotonic", clock)
        hidrive.secret_set("115_open_access_token", "open-access-fixture")
        hidrive.setting_set("115_open_root_cid", "10")

        def slow_open_files(**kwargs):
            clock.advance(20)  # resolution alone burns 20 of the 24s budget
            return FakeResponse({"state": True, "data": [{"fn": "Movies", "fc": "0", "fid": "20"}]})

        def verify_handler(**kwargs):
            clock.advance(1)
            return FakeResponse({"state": True, "data": {"uid": "42"}})

        def snap_handler(**kwargs):
            clock.advance(1)
            return FakeResponse({"state": True, "data": {"list": [{"fid": "f1"}]}})

        http.route("GET", open_files_url, handler=slow_open_files)
        http.route("GET", USER_URL, handler=verify_handler)
        http.route("GET", SNAP_URL, handler=snap_handler)
        # RECEIVE_URL deliberately left unrouted -- any call to it fails the test.

        response = client.post(
            "/api/library/transfer",
            json={"resource_link_id": "pub-fixture-01", "target_path": "/115pan/Movies"},
        )

        assert response.status_code == 504
        assert response.get_json()["code"] == "TRANSFER_NOT_ATTEMPTED"
        assert http.calls_to(RECEIVE_URL) == []
        verify_call = http.calls_to(USER_URL)[0]
        snap_calls = http.calls_to(SNAP_URL)
        assert verify_call["timeout"] == (3, 4)
        assert len(snap_calls) == 1  # no retry -- remaining budget never reaches 12s
        assert snap_calls[0]["timeout"] == (3, 3)

    def test_transfer_deadline_worst_case_recorded_timeouts_never_exceed_budget(self, client, installed_library, http, hidrive, monkeypatch):
        # T19 wave 4 item 6: mirrors tests/test_115_save.py's equivalent --
        # even if every upstream call in the worst-case chain (target
        # resolution, verify, share/snap's first attempt) actually took its
        # full allotted connect+read time, the whole request still finishes
        # comfortably under gunicorn's 30s worker timeout, whether or not
        # share/receive ends up with enough budget left to be attempted.
        from conftest import FakeMonotonic, FakeResponse

        open_files_url = "https://proapi.115.com/open/ufile/files"
        clock = FakeMonotonic()
        monkeypatch.setattr(hidrive.time, "monotonic", clock)
        hidrive.secret_set("115_open_access_token", "open-access-fixture")
        hidrive.setting_set("115_open_root_cid", "10")

        def make_handler(payload):
            def handler(**kwargs):
                connect, read = kwargs["timeout"]
                clock.advance(connect + read)  # worst case: uses every second of both phases
                return FakeResponse(payload)

            return handler

        http.route("GET", open_files_url, handler=make_handler({"state": True, "data": [{"fn": "Movies", "fc": "0", "fid": "20"}]}))
        http.route("GET", USER_URL, handler=make_handler({"state": True, "data": {"uid": "42"}}))
        http.route("GET", SNAP_URL, handler=make_handler({"state": True, "data": {"list": [{"fid": "f1"}]}}))
        http.route("POST", RECEIVE_URL, handler=make_handler({"state": True}))

        response = client.post(
            "/api/library/transfer",
            json={"resource_link_id": "pub-fixture-01", "target_path": "/115pan/Movies"},
        )

        elapsed_before = 0.0
        for call in http.calls:
            connect, read = call["timeout"]
            remaining_at_call = hidrive._115_REQUEST_DEADLINE_SECONDS - elapsed_before
            assert connect <= hidrive._115_UPSTREAM_CONNECT_TIMEOUT
            assert read <= hidrive._115_UPSTREAM_TIMEOUT
            assert connect + read <= remaining_at_call + hidrive._115_UPSTREAM_CONNECT_TIMEOUT
            elapsed_before += connect + read
        assert elapsed_before < 30
        assert response.status_code == 504
        assert response.get_json()["code"] == "TRANSFER_NOT_ATTEMPTED"
        assert http.calls_to(RECEIVE_URL) == []

    def test_blank_url_password_is_replaced_by_stored_code(self, client, installed_library, http):
        # T4 fix #5: pub-fixture-13's URL embeds an EMPTY ?password= --
        # _apply_access_code must treat that as "no password" (like
        # parse_115_link's keep_blank_values=False) and replace it with
        # the stored column access code, not leave it blank or duplicate it.
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})

        response = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-13", "target_pid": "1"})

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        snap_call = http.calls_to(SNAP_URL)[0]
        assert snap_call["params"]["share_code"] == "swfake120012"
        assert snap_call["params"]["receive_code"] == "ef56"

    # -- T17 item 5: duplicate-submit guard -----------------------------

    def test_repeat_submit_within_window_returns_409(self, client, installed_library, http):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})

        first = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        assert first.status_code == 200

        second = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        assert second.status_code == 409
        assert second.get_json()["code"] == "TRANSFER_DUPLICATE"
        # the duplicate must never re-attempt the actual 115 call.
        assert len(http.calls_to(RECEIVE_URL)) == 1

    def test_different_pid_is_not_a_duplicate(self, client, installed_library, http, hidrive):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})

        first = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        # T17 fix wave 1 item 1: "2" needs its own proof too -- it's a
        # different stored default here, simulating the user having since
        # changed their default target folder between the two submits.
        hidrive.setting_set("115_target_pid", "2")
        second = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "2"})

        assert first.status_code == 200
        assert second.status_code == 200
        assert len(http.calls_to(RECEIVE_URL)) == 2

    def test_different_link_is_not_a_duplicate(self, client, installed_library, http):
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})

        first = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        second = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-11", "target_pid": "1"})

        assert first.status_code == 200
        assert second.status_code == 200

    def test_repeat_submit_after_window_elapses_is_allowed(self, client, hidrive, installed_library, http, monkeypatch):
        from conftest import FakeMonotonic

        clock = FakeMonotonic()
        monkeypatch.setattr(hidrive.time, "monotonic", clock)
        _route_115_session(http)
        http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
        http.route("POST", RECEIVE_URL, {"state": True})

        first = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})
        assert first.status_code == 200

        clock.advance(hidrive._TRANSFER_DEDUPE_WINDOW_SECONDS + 1)
        second = client.post("/api/library/transfer", json={"resource_link_id": "pub-fixture-01", "target_pid": "1"})

        assert second.status_code == 200
        assert len(http.calls_to(RECEIVE_URL)) == 2


class TestLibraryKeyOptionalReads:
    """T4 fix #4: _library_store_or_error(need_key) -- read-only routes
    open the store with fernet=None and must keep working even when the
    master key is completely unavailable; only reveal/transfer need the
    key and map a load_fernet() failure to 503 LIBRARY_KEY_UNAVAILABLE."""

    def test_search_ignores_unavailable_master_key(self, client, installed_library, hidrive, monkeypatch):
        def _boom():
            raise OSError("master key file missing")

        monkeypatch.setattr(hidrive, "load_fernet", _boom)
        response = client.get("/api/library/search")
        assert response.status_code == 200
        assert response.get_json()["success"] is True

    def test_reveal_returns_503_when_master_key_unavailable(self, client, installed_library, hidrive, monkeypatch):
        def _boom():
            raise OSError("master key file missing")

        monkeypatch.setattr(hidrive, "load_fernet", _boom)
        response = client.post("/api/library/link/pub-fixture-02/reveal")
        assert response.status_code == 503
        assert response.get_json()["code"] == "LIBRARY_KEY_UNAVAILABLE"
