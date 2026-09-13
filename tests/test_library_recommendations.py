"""Tests for T16 §2: `GET /api/library/recommendations` -- deterministic
daily recommendations (Asia/Shanghai business day, SHA-256 tie-break).

Uses a dedicated, standalone bundle (not the shared ``installed_library``
fixture) so the exact number of eligible/ineligible candidates is under this
file's control. All URLs/access codes are fixtures, not real shares, per the
project's test-data rule (see .superpowers/sdd/briefs/common-u.md).
"""

from __future__ import annotations

import hashlib
import json
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


# ---------------------------------------------------------------------------
# Round 17: `placement=hero` -- the home banner's own daily pick. Stricter
# pool than the rail (backdrop + non-empty overview on top of exact +
# poster + a live link), its own algorithm version in the tie-break and
# cache key, `section: today_hero`. The default (rail) response is untouched.
# ---------------------------------------------------------------------------


def _add_hero_media(store, identity, *, title, match_status="exact", poster_path="/p.jpg",
                    backdrop_path="/b.jpg", overview="一段简介", year=2020,
                    ratings=None, media_type="movie"):
    media_id = store.upsert_media(
        ls.MediaRecord(
            media_identity=identity, media_type=media_type, title_zh=title, search_key=title, year=year,
            match_status=match_status, poster_path=poster_path, backdrop_path=backdrop_path, overview=overview,
        )
    )
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET ratings_json=? WHERE id=?", (
            json.dumps(ratings if ratings is not None else {"tmdb": {"score": 8.2, "votes": 1000}}), media_id))
        conn.commit()
    finally:
        conn.close()
    return media_id


@pytest.fixture
def hero_library(hidrive, workspace):
    """Six hero-eligible media plus one of every ineligible shape."""
    bundle_path = workspace / "hero-bundle.sqlite"
    store = ls.LibraryStore(bundle_path)
    store.create_schema()
    now = int(time.time())

    eligible_ids = []
    for i in range(1, 7):
        media_id = _add_hero_media(store, f"tmdb:movie:hero{i}", title=f"横幅候选{i}", year=2010 + i)
        _add_live_link(store, media_id)
        eligible_ids.append(media_id)

    ineligible = {}
    ineligible["no_poster"] = _add_hero_media(store, "tmdb:movie:hnp", title="无海报", poster_path=None)
    ineligible["no_backdrop"] = _add_hero_media(store, "tmdb:movie:hnb", title="无背景图", backdrop_path=None)
    ineligible["empty_overview"] = _add_hero_media(store, "tmdb:movie:hno", title="无简介", overview="")
    ineligible["null_overview"] = _add_hero_media(store, "tmdb:movie:hnn", title="简介为空值", overview=None)
    ineligible["needs_review"] = _add_hero_media(store, "fp:movie:hnr", title="待复核", match_status="needs_review")
    ineligible["candidate"] = _add_hero_media(store, "fp:movie:hnc", title="候选匹配", match_status="candidate")
    for name, ratings in {
        "unrated": {}, "low_score": {"tmdb": {"score": 7.4, "votes": 10000}},
        "few_votes": {"tmdb": {"score": 9.9, "votes": 2}},
        "out_of_scale": {"tmdb": {"score": 99, "votes": 10000}},
    }.items():
        ineligible[name] = _add_hero_media(store, "fp:movie:" + name, title=name, ratings=ratings)
    ineligible["collection"] = _add_hero_media(store, "fp:collection:h", title="合集", media_type="unknown")
    for media_id in ineligible.values():
        _add_live_link(store, media_id)
    ineligible["deleted_link"] = _add_hero_media(store, "tmdb:movie:hdl", title="链接已删")
    _add_live_link(store, ineligible["deleted_link"], deleted=True)
    ineligible["invalid_link"] = _add_hero_media(store, "tmdb:movie:hil", title="链接失效")
    _add_live_link(store, ineligible["invalid_link"])
    store.record_link_check(
        "115", _hash(f"https://115.com/s/swfake{ineligible['invalid_link']}"), status="invalid",
        reason="share_cancelled", http_class="200", checked_at=now, next_check_at=now + 1, consecutive_unknown=0,
    )

    _install(hidrive, store, now)
    store.eligible_ids = eligible_ids
    store.ineligible = ineligible
    return store


class TestHeroPlacement:
    def test_hero_response_shape(self, client, hero_library):
        body = client.get("/api/library/recommendations?placement=hero&limit=6").get_json()
        assert body["success"] is True
        assert body["section"] == "today_hero"
        assert body["date"] and body["algorithm_version"]
        assert body["fallback"] is False
        assert len(body["items"]) == 6
        for item in body["items"]:
            assert item["backdrop_url"] and item["poster_url"] and item["overview_short"]
            assert "backdrop_path" not in item and "poster_path" not in item

    def test_hero_only_eligible_media(self, client, hero_library):
        body = client.get("/api/library/recommendations?placement=hero&limit=24&date=2026-01-01").get_json()
        ids = {item["media_id"] for item in body["items"]}
        assert ids == set(hero_library.eligible_ids)
        for shape, media_id in hero_library.ineligible.items():
            assert media_id not in ids, shape

    def test_hero_same_day_20_calls_identical_order(self, client, hero_library):
        first = [i["media_id"] for i in client.get("/api/library/recommendations?placement=hero&limit=6&date=2026-03-14").get_json()["items"]]
        for _ in range(19):
            again = [i["media_id"] for i in client.get("/api/library/recommendations?placement=hero&limit=6&date=2026-03-14").get_json()["items"]]
            assert again == first

    def test_hero_rotates_every_two_days(self, client, hero_library):
        day1 = [i["media_id"] for i in client.get("/api/library/recommendations?placement=hero&limit=6&date=2026-03-14").get_json()["items"]]
        day2 = [i["media_id"] for i in client.get("/api/library/recommendations?placement=hero&limit=6&date=2026-03-16").get_json()["items"]]
        assert set(day1) == set(day2)
        assert day1 != day2

    def test_hero_first_item_changes_across_a_week(self, client, hero_library):
        firsts = {
            client.get(f"/api/library/recommendations?placement=hero&limit=1&date=2026-03-{day:02d}").get_json()["items"][0]["media_id"]
            for day in range(10, 17)
        }
        assert len(firsts) > 1

    def test_hero_cache_cleared_and_restart_simulated_same_result(self, client, hero_library, hidrive):
        first = [i["media_id"] for i in client.get("/api/library/recommendations?placement=hero&limit=6&date=2026-03-14").get_json()["items"]]
        hidrive._RECOMMENDATION_CACHE.clear()
        again = [i["media_id"] for i in client.get("/api/library/recommendations?placement=hero&limit=6&date=2026-03-14").get_json()["items"]]
        assert again == first
        # A fresh process has no cache and recomputes from the index alone.
        hidrive._RECOMMENDATION_CACHE.clear()
        ranked = sorted(hero_library.eligible_ids, key=lambda mid: hidrive._recommendation_tie(hidrive._hero_rotation_date("2026-03-14"), mid, hidrive._HERO_ALGO_VERSION))
        assert ranked == first

    def test_five_items_stay_identical_within_one_edition(self, client, hero_library, hidrive):
        from datetime import datetime, timedelta
        first_date = hidrive._hero_rotation_date("2026-03-14")
        second_date = (datetime.strptime(first_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        first = client.get(f"/api/library/recommendations?placement=hero&limit=5&date={first_date}").get_json()
        hidrive._RECOMMENDATION_CACHE.clear()
        second = client.get(f"/api/library/recommendations?placement=hero&limit=5&date={second_date}").get_json()
        assert len(first["items"]) == 5
        assert first["items"] == second["items"]
        assert first["rotation_date"] == second["rotation_date"] == first_date
        assert second["refresh_interval_days"] == 2

    def test_imdb_fallback_and_threshold_are_supported(self, hidrive, workspace):
        store = ls.LibraryStore(workspace / "rating-boundary.sqlite")
        store.create_schema()
        movie = _add_hero_media(store, "fp:movie:boundary", title="边界", ratings={"tmdb": {"score": 7.5, "votes": 200}})
        tv = _add_hero_media(store, "fp:tv:imdb", title="IMDb剧集", media_type="tv", ratings={"imdb": {"score": 8.5, "votes": 1000}})
        for media_id in (movie, tv):
            _add_live_link(store, media_id)
        assert store.eligible_hero_media_ids() == [movie, tv]

    def test_hero_deduplicates_typed_identity_without_merging_movie_and_tv(self, workspace):
        store = ls.LibraryStore(workspace / "hero-identity.sqlite")
        store.create_schema()
        ids = [_add_hero_media(store, f"fp:hero:{i}", title=f"候选{i}",
                               media_type="tv" if i == 2 else "movie") for i in range(3)]
        for media_id in ids:
            _add_live_link(store, media_id)
        conn = store.connect()
        try:
            conn.executemany("UPDATE media SET tmdb_id=123 WHERE id=?", [(mid,) for mid in ids])
            conn.commit()
        finally:
            conn.close()
        assert store.eligible_hero_media_ids() == [ids[0], ids[2]]
        assert store.eligible_hero_media_ids(ids[:2]) == [ids[0]]

    @pytest.mark.parametrize("votes", [float("inf"), float("nan"), -float("inf")])
    def test_malformed_votes_do_not_break_the_pool(self, workspace, votes):
        store = ls.LibraryStore(workspace / "malformed-rating.sqlite")
        store.create_schema()
        broken = _add_hero_media(store, "fp:movie:broken", title="脏评分", ratings={"tmdb": {"score": 8, "votes": votes}})
        good = _add_hero_media(store, "fp:movie:good", title="正常评分")
        for media_id in (broken, good):
            _add_live_link(store, media_id)
        assert store.eligible_hero_media_ids() == [good]

    @pytest.mark.parametrize("change", ["score", "match", "links"])
    def test_cached_selection_rechecks_changed_eligibility(self, client, hero_library, hidrive, change):
        url = "/api/library/recommendations?placement=hero&limit=5&date=2026-09-12"
        first = client.get(url).get_json()
        removed = first["items"][0]["media_id"]
        store = ls.LibraryStore(hidrive.LIBRARY_DB_PATH)
        conn = store.connect()
        try:
            if change == "score":
                conn.execute("UPDATE media SET ratings_json=? WHERE id=?", (json.dumps({"tmdb": {"score": 7, "votes": 1000}}), removed))
            elif change == "match":
                conn.execute("UPDATE media SET match_status='needs_review' WHERE id=?", (removed,))
            else:
                conn.execute("UPDATE resource_link SET deleted_at_source=1 WHERE group_id IN (SELECT id FROM resource_group WHERE media_id=?)", (removed,))
            conn.commit()
        finally:
            conn.close()
        second = client.get(url).get_json()
        assert removed not in [item["media_id"] for item in second["items"]]
        assert len(second["items"]) == 5

    def test_empty_pool_can_recover_before_the_next_edition(self, client, hero_library, hidrive):
        store = ls.LibraryStore(hidrive.LIBRARY_DB_PATH)
        conn = store.connect()
        conn.execute("UPDATE media SET ratings_json='{}'")
        conn.commit()
        url = "/api/library/recommendations?placement=hero&limit=5&date=2026-09-12"
        assert client.get(url).get_json()["items"] == []
        conn.execute("UPDATE media SET ratings_json=? WHERE id=?", (json.dumps({"tmdb": {"score": 8.1, "votes": 300}}), hero_library.eligible_ids[0]))
        conn.commit()
        conn.close()
        assert len(client.get(url).get_json()["items"]) == 1

    def test_first_calendar_date_never_produces_an_invalid_ordinal(self, client, hero_library):
        response = client.get("/api/library/recommendations?placement=hero&date=0001-01-01")
        assert response.status_code == 200
        assert response.get_json()["rotation_date"] == "0001-01-01"

    def test_hero_uses_its_own_algorithm_version_and_cache_key(self, client, hero_library, hidrive):
        body = client.get("/api/library/recommendations?placement=hero&limit=6&date=2026-03-14").get_json()
        rail = client.get("/api/library/recommendations?limit=6&date=2026-03-14").get_json()
        assert body["algorithm_version"] == hidrive._HERO_ALGO_VERSION
        assert body["algorithm_version"] != rail["algorithm_version"]
        versions = {key[3] for key in hidrive._RECOMMENDATION_CACHE}
        assert hidrive._HERO_ALGO_VERSION in versions

    def test_hero_default_date_is_today_in_asia_shanghai(self, client, hero_library):
        import datetime
        from zoneinfo import ZoneInfo
        expected = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        assert client.get("/api/library/recommendations?placement=hero").get_json()["date"] == expected

    def test_hero_empty_pool_returns_fallback_true_and_no_items(self, client, no_eligible_library):
        body = client.get("/api/library/recommendations?placement=hero&limit=6").get_json()
        assert body["section"] == "today_hero"
        assert body["fallback"] is True and body["items"] == []

    def test_invalid_placement_returns_400(self, client, hero_library):
        assert client.get("/api/library/recommendations?placement=sidebar").status_code == 400

    def test_default_rail_response_unchanged_by_hero_work(self, client, hero_library):
        body = client.get("/api/library/recommendations").get_json()
        assert body["section"] == "today_recommendations"
        assert body["algorithm_version"] == "1"
        # The rail pool is looser (no backdrop/overview requirement): the
        # no-backdrop and no-overview samples still belong to it.
        ids = {i["media_id"] for i in client.get("/api/library/recommendations?limit=24").get_json()["items"]}
        assert hero_library.ineligible["no_backdrop"] in ids and hero_library.ineligible["empty_overview"] in ids

    def test_hero_route_never_touches_tmdb_hdhive_or_production_db(self, client, hero_library, http, hidrive, workspace):
        client.get("/api/library/recommendations?placement=hero&limit=6")
        assert http.calls == []
        assert str(hidrive.LIBRARY_DB_PATH).startswith(str(workspace))
        assert not str(hidrive.LIBRARY_DB_PATH).startswith("/data-disk/")
