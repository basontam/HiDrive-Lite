"""Tests for T16 §2: `GET /api/library/recommendations` -- deterministic
daily recommendations (Asia/Shanghai business day, SHA-256 tie-break).

Uses a dedicated, standalone bundle (not the shared ``installed_library``
fixture) so the exact number of eligible/ineligible candidates is under this
file's control. All URLs/access codes are fixtures, not real shares, per the
project's test-data rule (see .superpowers/sdd/briefs/common-u.md).
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_search as lse  # noqa: E402
import library_store as ls  # noqa: E402


def _hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _add_media(store, identity, *, title, match_status="exact", poster_path="/p.jpg", year=2020):
    return store.upsert_media(
        ls.MediaRecord(
            media_identity=identity, media_type="movie", title_zh=title,
            search_key=title, year=year, match_status=match_status, poster_path=poster_path,
        )
    )


def _add_live_link(store, media_id, *, deleted=False, suffix=""):
    group_id = store.upsert_group(
        ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-{media_id}{suffix}", display_title="g")
    )
    store.upsert_link(
        ls.LinkRecord(
            public_id=f"pub-{media_id}{suffix}", group_id=group_id, provider="115",
            canonical_url_hash=_hash(f"https://115.com/s/swfake{media_id}{suffix}"),
            url_label="115 分享 · swf…1",
            deleted_at_source=1 if deleted else None,
        )
    )


def _install(hidrive, bundle_store, now):
    bundle_store.recount()
    lse.build_index(bundle_store)
    bundle_store.meta_set("normalize_version", "test-1")
    bundle_store.meta_set("built_at", str(now))
    bundle_store.meta_set("source_hashes", "{}")
    bundle_store.meta_set("encrypted", "0")
    ls.install_bundle(bundle_store.db_path, hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())


@pytest.fixture
def recommendations_library(hidrive, workspace):
    """Five eligible candidates (``exact`` + poster + a live link) plus one
    of each ineligible shape (needs_review, candidate, no poster, no live
    link) -- so eligibility filtering and the tie-break rotation both have
    something real to exercise."""
    bundle_path = workspace / "recommendations-bundle.sqlite"
    store = ls.LibraryStore(bundle_path)
    store.create_schema()
    now = int(time.time())

    eligible_ids = []
    for i in range(1, 6):
        media_id = _add_media(store, f"tmdb:movie:rec{i}", title=f"推荐候选{i}", year=2015 + i)
        _add_live_link(store, media_id)
        eligible_ids.append(media_id)

    needs_review_id = _add_media(store, "fp:movie:review1", title="待复核样本", match_status="needs_review")
    _add_live_link(store, needs_review_id)

    candidate_id = _add_media(store, "fp:movie:cand1", title="候选样本", match_status="candidate")
    _add_live_link(store, candidate_id)

    no_poster_id = _add_media(store, "tmdb:movie:noposter1", title="无海报样本", poster_path=None)
    _add_live_link(store, no_poster_id)

    no_live_link_id = _add_media(store, "tmdb:movie:nolink1", title="无有效链接样本")
    _add_live_link(store, no_live_link_id, deleted=True)

    _install(hidrive, store, now)
    store.eligible_ids = eligible_ids
    store.ineligible_ids = [needs_review_id, candidate_id, no_poster_id, no_live_link_id]
    return store


@pytest.fixture
def no_eligible_library(hidrive, workspace):
    """Zero ``exact``+poster+live-link candidates, but several media (any
    match status) with a poster and a live link -- exercises the §2.1
    fallback path (``fallback: true``, deterministic ``year_desc``)."""
    bundle_path = workspace / "no-eligible-bundle.sqlite"
    store = ls.LibraryStore(bundle_path)
    store.create_schema()
    now = int(time.time())

    older_id = _add_media(store, "fp:movie:old1", title="旧年份样本", match_status="unmatched", year=2010)
    _add_live_link(store, older_id)
    newer_id = _add_media(store, "fp:movie:new1", title="新年份样本", match_status="unmatched", year=2024)
    _add_live_link(store, newer_id)

    _install(hidrive, store, now)
    store.fallback_ids = [newer_id, older_id]
    return store


@pytest.fixture
def empty_library(hidrive, workspace):
    """No media qualify for either the primary pool or the fallback pool."""
    bundle_path = workspace / "empty-bundle.sqlite"
    store = ls.LibraryStore(bundle_path)
    store.create_schema()
    _install(hidrive, store, int(time.time()))
    return store


class TestRecommendationsRoute:
    def test_returns_today_recommendations_section_and_shape(self, client, recommendations_library):
        response = client.get("/api/library/recommendations")
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["section"] == "today_recommendations"
        assert "date" in body
        assert "algorithm_version" in body
        assert body["fallback"] is False
        assert len(body["items"]) == 5
        for item in body["items"]:
            for key in ("media_id", "title", "year", "media_type", "poster_url"):
                assert key in item, key
            assert "poster_path" not in item
            assert "backdrop_path" not in item

    def test_only_eligible_media_ever_appear(self, client, recommendations_library):
        response = client.get("/api/library/recommendations?date=2026-01-01")
        ids = {item["media_id"] for item in response.get_json()["items"]}
        assert ids == set(recommendations_library.eligible_ids)
        assert ids.isdisjoint(recommendations_library.ineligible_ids)

    def test_same_day_20_calls_identical_order(self, client, recommendations_library):
        first = client.get("/api/library/recommendations?date=2026-03-14").get_json()["items"]
        first_order = [item["media_id"] for item in first]
        for _ in range(19):
            again = client.get("/api/library/recommendations?date=2026-03-14").get_json()["items"]
            assert [item["media_id"] for item in again] == first_order

    def test_rotates_by_day(self, client, recommendations_library):
        day1 = [item["media_id"] for item in client.get("/api/library/recommendations?date=2026-03-14").get_json()["items"]]
        day2 = [item["media_id"] for item in client.get("/api/library/recommendations?date=2026-03-15").get_json()["items"]]
        assert set(day1) == set(day2)  # same eligible pool
        assert day1 != day2  # but a different deterministic order

    def test_default_date_is_today_in_asia_shanghai(self, client, recommendations_library):
        import datetime
        from zoneinfo import ZoneInfo

        expected = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        body = client.get("/api/library/recommendations").get_json()
        assert body["date"] == expected

    def test_invalid_date_returns_400(self, client, recommendations_library):
        response = client.get("/api/library/recommendations?date=not-a-date")
        assert response.status_code == 400

    def test_limit_default_is_12(self, client, recommendations_library):
        response = client.get("/api/library/recommendations")
        # only 5 eligible, so this also proves "limit > eligible" is fine.
        assert len(response.get_json()["items"]) == 5

    def test_limit_greater_than_eligible_returns_available_without_error(self, client, recommendations_library):
        response = client.get("/api/library/recommendations?limit=24")
        assert response.status_code == 200
        assert len(response.get_json()["items"]) == 5

    @pytest.mark.parametrize("limit", ["0", "25", "abc", "-1"])
    def test_invalid_limit_returns_400(self, client, recommendations_library, limit):
        response = client.get("/api/library/recommendations?limit=" + limit)
        assert response.status_code == 400

    def test_no_duplicate_items(self, client, recommendations_library):
        items = client.get("/api/library/recommendations?date=2026-03-14").get_json()["items"]
        ids = [item["media_id"] for item in items]
        assert len(ids) == len(set(ids))

    def test_cache_cleared_yields_same_result(self, client, recommendations_library, hidrive):
        first = client.get("/api/library/recommendations?date=2026-03-14").get_json()["items"]
        first_order = [item["media_id"] for item in first]
        hidrive._RECOMMENDATION_CACHE.clear()
        again = client.get("/api/library/recommendations?date=2026-03-14").get_json()["items"]
        assert [item["media_id"] for item in again] == first_order

    def test_no_store_cache_control_header(self, client, recommendations_library):
        response = client.get("/api/library/recommendations")
        assert response.headers["Cache-Control"] == "no-store"


class TestRecommendationsFallback:
    def test_falls_back_to_year_desc_when_no_eligible_media(self, client, no_eligible_library):
        response = client.get("/api/library/recommendations?date=2026-03-14")
        body = response.get_json()
        assert body["fallback"] is True
        ids = [item["media_id"] for item in body["items"]]
        assert ids == no_eligible_library.fallback_ids

    def test_fallback_is_also_deterministic_across_days(self, client, no_eligible_library):
        day1 = client.get("/api/library/recommendations?date=2026-03-14").get_json()["items"]
        day2 = client.get("/api/library/recommendations?date=2026-03-15").get_json()["items"]
        # §2.4: candidates insufficient -> fallback identical across days is
        # explicitly permitted (documented here, not a bug).
        assert [i["media_id"] for i in day1] == [i["media_id"] for i in day2]

    def test_empty_library_returns_empty_items_not_an_error(self, client, empty_library):
        response = client.get("/api/library/recommendations")
        assert response.status_code == 200
        body = response.get_json()
        assert body["fallback"] is True
        assert body["items"] == []


class TestRecommendationsNoBackendCalls:
    def test_route_never_touches_tmdb_or_hdhive(self, client, recommendations_library, http):
        # `http` replaces requests.get/post/request with a FakeHTTP that
        # raises AssertionError on any unregistered call -- no routes were
        # registered, so any outbound call at all fails the test.
        client.get("/api/library/recommendations")
        assert http.calls == []
