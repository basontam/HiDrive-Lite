"""Tests for u1-backend T2: richer/safe /api/library/* payloads and filters
(brief .superpowers/sdd/briefs/u1-backend.md).

Covers: search/browse items carrying genres/overview_short/poster_url/
backdrop_url/group_count/link_count/has_115/match_status; the media-detail
(summary, no links) vs resource-group-detail (full, with links) split; the
new genre/source/has_backdrop/complete_season filters (both valid values and
400 validation); the /api/library/filters "genres"/"sources" facets; and
that every pre-existing search param still works.

Uses the shared ``installed_library`` fixture (tests/fixtures/library/
build_installed.py) -- media #3 (虚构剧集一, fp:tv:900003, group_id 3) and
media #4 (虚构剧集二, fp:tv:900004, group_id 4) carry resource-group spec
fields (source_type/complete_season/tags/review_reason/...) added for this
task; media #6 (虚构电影四, fp:movie:900006) carries a second real genre
("动作") distinct from media #1's ("剧情"). See that fixture file for the
full synthetic dataset.

All URLs/access codes are fixtures, not real shares, per the project's
test-data rule (see .superpowers/sdd/briefs/common-u.md).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_search as lse  # noqa: E402
import library_store as ls  # noqa: E402


class TestSearchItemFields:
    def test_items_carry_the_required_keys(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影一")
        item = response.get_json()["items"][0]
        for key in (
            "media_id", "title", "original_title", "year", "media_type", "genres",
            "overview_short", "poster_url", "backdrop_url", "group_count",
            "link_count", "has_115", "match_status",
        ):
            assert key in item, key

    def test_backdrop_url_present_when_backdrop_path_set(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影一")
        item = response.get_json()["items"][0]
        assert item["backdrop_url"] is not None
        assert item["backdrop_url"].endswith("/w1280/backdrop1.jpg")
        assert "backdrop_path" not in item

    def test_backdrop_url_is_null_when_absent(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影二")
        item = response.get_json()["items"][0]
        assert item["backdrop_url"] is None

    def test_overview_short_not_cut_matches_source_overview(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影一")
        item = response.get_json()["items"][0]
        assert item["overview_short"] == "一部虚构的电影，用于测试。"
        assert not item["overview_short"].endswith("…")

    def test_overview_short_absent_is_empty_string(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影二")
        item = response.get_json()["items"][0]
        assert item["overview_short"] == ""

    def test_overview_short_length_never_exceeds_121_chars(self, client, installed_library):
        response = client.get("/api/library/search")
        for item in response.get_json()["items"]:
            assert len(item["overview_short"]) <= 121
            if len(item["overview_short"]) == 121:
                assert item["overview_short"].endswith("…")

    def test_group_count_matches_media_group_count(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影一")
        item = response.get_json()["items"][0]
        assert item["group_count"] == 1


@pytest.fixture
def providers_field_library(hidrive, workspace):
    """A standalone bundle (not the shared ``installed_library`` fixture)
    covering the search-item `providers` list: a media with two live
    providers, a media with no resource groups at all, and a media whose
    only deleted-at-source provider must be excluded while its live one
    stays. Installed at ``hidrive.LIBRARY_DB_PATH`` the same way
    ``installed_library`` is, so the ``client`` fixture can hit it."""
    bundle_path = workspace / "providers-field-bundle.sqlite"
    bundle_store = ls.LibraryStore(bundle_path)
    bundle_store.create_schema()
    now = int(time.time())

    multi_id = bundle_store.upsert_media(ls.MediaRecord(
        media_identity="fp:movie:pf0001", media_type="movie", title_zh="供应商甲",
        search_key="供应商甲", match_status="unmatched",
    ))
    multi_group = bundle_store.upsert_group(ls.GroupRecord(
        media_id=multi_id, edition_fingerprint="fp-pf0001", display_title="g",
    ))
    bundle_store.upsert_link(ls.LinkRecord(
        public_id="pub-pf-multi-115", group_id=multi_group, provider="115",
        canonical_url_hash="hash-pf-multi-115", url_label="115 分享 · swf…1",
        created_at_source=now,
    ))
    bundle_store.upsert_link(ls.LinkRecord(
        public_id="pub-pf-multi-quark", group_id=multi_group, provider="quark",
        canonical_url_hash="hash-pf-multi-quark", url_label="夸克 分享 · swf…2",
        created_at_source=now,
    ))

    no_links_id = bundle_store.upsert_media(ls.MediaRecord(
        media_identity="fp:movie:pf0002", media_type="movie", title_zh="供应商乙",
        search_key="供应商乙", match_status="unmatched",
    ))

    deleted_only_id = bundle_store.upsert_media(ls.MediaRecord(
        media_identity="fp:movie:pf0003", media_type="movie", title_zh="供应商丙",
        search_key="供应商丙", match_status="unmatched",
    ))
    deleted_group = bundle_store.upsert_group(ls.GroupRecord(
        media_id=deleted_only_id, edition_fingerprint="fp-pf0003", display_title="g",
    ))
    bundle_store.upsert_link(ls.LinkRecord(
        public_id="pub-pf-live-alipan", group_id=deleted_group, provider="alipan",
        canonical_url_hash="hash-pf-live-alipan", url_label="alipan 分享 · swf…3",
        created_at_source=now,
    ))
    bundle_store.upsert_link(ls.LinkRecord(
        public_id="pub-pf-deleted-baidu", group_id=deleted_group, provider="baidu",
        canonical_url_hash="hash-pf-deleted-baidu", url_label="baidu 分享 · swf…4",
        created_at_source=now, deleted_at_source=now,
    ))

    bundle_store.recount()
    lse.build_index(bundle_store)
    bundle_store.meta_set("normalize_version", "test-1")
    bundle_store.meta_set("built_at", str(now))
    bundle_store.meta_set("source_hashes", "{}")
    bundle_store.meta_set("encrypted", "0")

    ls.install_bundle(bundle_path, hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())


class TestSearchItemProvidersField:
    """u1-backend follow-up: `/api/library/search` items' `providers` list
    (distinct live resource_link.provider codes across all of a media's
    resource groups, "115" first then alphabetical) -- see
    library_search.py's shared `_fetch_items` hydration used by both
    `library_search.search` and `LibraryStore.browse`."""

    def test_multiple_live_providers_sorted_with_115_first(self, client, providers_field_library):
        response = client.get("/api/library/search?q=" + "供应商甲")
        item = response.get_json()["items"][0]
        assert item["providers"] == ["115", "quark"]

    def test_media_with_no_links_has_empty_providers(self, client, providers_field_library):
        # excluded from results by default (no live link at all), so
        # include_deleted=1 is needed just to make it show up.
        response = client.get("/api/library/search?q=" + "供应商乙" + "&include_deleted=1")
        item = response.get_json()["items"][0]
        assert item["providers"] == []

    def test_deleted_only_provider_is_excluded(self, client, providers_field_library):
        response = client.get("/api/library/search?q=" + "供应商丙")
        item = response.get_json()["items"][0]
        assert item["providers"] == ["alipan"]


class TestDetailSummaryVsResourceFullSplit:
    def test_media_detail_group_has_inline_links_and_specs(self, client, installed_library):
        # T17 §13.2: media_detail's groups now carry inline safe link
        # summaries -- no separate /api/library/resource/<id> round trip.
        response = client.get("/api/library/media/3")
        assert response.status_code == 200
        body = response.get_json()
        assert body["group_count"] == 1
        group = body["groups"][0]
        assert "links" in group
        assert len(group["links"]) >= 1
        for key in (
            "group_id", "display_title", "complete_season", "source_type", "video_codec",
            "audio_summary", "subtitle_summary", "tags", "review_reason",
            "specs", "link_count", "has_115", "needs_review", "providers", "links",
        ):
            assert key in group, key
        assert group["source_type"] == "webdl"
        assert group["complete_season"] is True
        assert group["video_codec"] == "H.265"
        assert group["audio_summary"] == "DTS-HD 5.1"
        assert group["subtitle_summary"] == "中英双语"
        assert group["tags"] == ["国语配音", "内封字幕"]
        assert group["specs"]["resolution"]["value"] == "4K"
        assert group["specs"]["source"]["value"] == "WEB-DL"

    def test_media_detail_review_reason_is_sanitized(self, client, installed_library):
        response = client.get("/api/library/media/3")
        group = response.get_json()["groups"][0]
        assert set(group["review_reason"]) == {"title_alias_conflict", "year_missing"}
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        assert "bogus_unknown_code" not in blob

    def test_resource_route_has_full_payload_with_links(self, client, installed_library):
        response = client.get("/api/library/resource/3")
        assert response.status_code == 200
        group = response.get_json()
        assert "links" in group
        assert group["source_type"] == "webdl"
        assert group["complete_season"] is True
        assert set(group["review_reason"]) == {"title_alias_conflict", "year_missing"}

    def test_media_detail_poster_and_backdrop_urls(self, client, installed_library):
        response = client.get("/api/library/media/1")
        body = response.get_json()
        assert body["poster_url"].endswith("/w342/poster1.jpg")
        assert body["poster_large_url"].endswith("/w500/poster1.jpg")
        assert body["backdrop_url"].endswith("/w1280/backdrop1.jpg")
        assert "poster_path" not in body
        assert "backdrop_path" not in body


class TestMediaDetailProviderFacets:
    """x1-provider-ui §2.2/§4.2: the detail-page provider-logo tablist reads
    ``provider_facets``/``provider_count`` straight from ``/api/library/
    media/<id>`` -- media #1 (虚构电影一) has one group with a live 115 link
    and a live quark link (see build_installed.py)."""

    def test_media_1_has_115_and_quark_facets_in_priority_order(self, client, installed_library):
        response = client.get("/api/library/media/1")
        body = response.get_json()
        assert body["provider_count"] == 2
        assert body["provider_facets"] == [
            {"provider": "115", "label": "115 网盘", "link_count": 1},
            {"provider": "quark", "label": "夸克网盘", "link_count": 1},
        ]

    def test_media_6_has_a_single_115_facet_summed_across_its_two_links(self, client, installed_library):
        # fp:movie:900006's one group carries two separate 115 links.
        response = client.get("/api/library/media/6")
        body = response.get_json()
        assert body["provider_facets"] == [{"provider": "115", "label": "115 网盘", "link_count": 2}]
        assert body["provider_count"] == 1

    def test_media_5_orders_ed2k_before_unknown(self, client, installed_library):
        response = client.get("/api/library/media/5")
        body = response.get_json()
        assert [f["provider"] for f in body["provider_facets"]] == ["ed2k", "unknown"]
        assert body["provider_facets"][1]["link_count"] == 2

    def test_provider_facets_never_carry_urls_or_access_codes(self, client, installed_library, library_markers):
        # Extends the existing media_detail sanitisation coverage (T2's
        # test_media_detail_never_leaks_urls_or_codes) to the new
        # provider_facets field specifically -- every media/group in the
        # fixture, not just #1, checked against every real marker.
        for media_id in range(1, 7):
            response = client.get(f"/api/library/media/{media_id}")
            blob = json.dumps(response.get_json(), ensure_ascii=False)
            for marker in library_markers:
                assert marker not in blob, (media_id, marker)
            for forbidden in ('"url":', '"access_code":'):
                assert forbidden not in blob


# ---------------------------------------------------------------------------
# T17 §14.1: /api/library/media/<id>?provider=... -- SQL-level provider
# isolation, not a client-side hide. Media #1 (虚构电影一) has ONE group with
# both a live 115 link and a live quark link (build_installed.py) -- the
# case where an unfiltered response legitimately shows both.
# ---------------------------------------------------------------------------


class TestMediaDetailProviderIsolation:
    def test_invalid_provider_is_400(self, client, installed_library):
        response = client.get("/api/library/media/1?provider=not-a-real-provider")
        assert response.status_code == 400
        assert response.get_json()["code"] == "LIBRARY_PROVIDER_INVALID"

    def test_provider_115_hides_every_quark_trace(self, client, installed_library):
        response = client.get("/api/library/media/1?provider=115")
        assert response.status_code == 200
        body = response.get_json()
        assert body["provider_facets"] == [{"provider": "115", "label": "115 网盘", "link_count": 1}]
        (group,) = body["groups"]
        assert [link["provider"] for link in group["links"]] == ["115"]
        blob = json.dumps(body, ensure_ascii=False)
        assert "quark" not in blob
        assert "夸克" not in blob

    def test_provider_quark_hides_115(self, client, installed_library):
        response = client.get("/api/library/media/1?provider=quark")
        assert response.status_code == 200
        body = response.get_json()
        assert body["provider_facets"] == [{"provider": "quark", "label": "夸克网盘", "link_count": 1}]
        (group,) = body["groups"]
        assert [link["provider"] for link in group["links"]] == ["quark"]
        assert group["has_115"] is False
        blob = json.dumps(body, ensure_ascii=False)
        assert "\"provider\": \"115\"" not in blob
        assert "115 网盘" not in blob

    def test_omitting_provider_restores_both(self, client, installed_library):
        client.get("/api/library/media/1?provider=115")
        response = client.get("/api/library/media/1")
        body = response.get_json()
        assert body["provider_count"] == 2
        all_providers = {link["provider"] for g in body["groups"] for link in g["links"]}
        assert all_providers == {"115", "quark"}


# ---------------------------------------------------------------------------
# T17 §14.1: /api/library/search?provider=... -- recomputed link_count/
# group_count/providers/has_115, and 400 on an invalid code.
# ---------------------------------------------------------------------------


class TestSearchProviderIsolation:
    def test_invalid_provider_is_400(self, client, installed_library):
        response = client.get("/api/library/search?provider=not-a-real-provider")
        assert response.status_code == 400
        assert response.get_json()["code"] == "LIBRARY_PROVIDER_INVALID"

    def test_invalid_provider_in_csv_list_is_400(self, client, installed_library):
        response = client.get("/api/library/search?provider=115,not-a-real-provider")
        assert response.status_code == 400
        assert response.get_json()["code"] == "LIBRARY_PROVIDER_INVALID"

    def test_provider_115_recomputes_counts_for_mixed_group(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影一" + "&provider=115")
        body = response.get_json()
        item = body["items"][0]
        assert item["link_count"] == 1
        assert item["group_count"] == 1
        assert item["providers"] == ["115"]
        assert item["has_115"] is True
        # Follow-up (post-T17): scan the WHOLE response body (not just the
        # one item) for the hidden provider's code AND its display label --
        # mirrors TestMediaDetailProviderIsolation's own blob checks.
        blob = json.dumps(body, ensure_ascii=False)
        assert "quark" not in blob
        assert "夸克" not in blob

    def test_provider_quark_recomputes_counts_and_hides_115(self, client, installed_library):
        # Mirror image of test_provider_115_recomputes_counts_for_mixed_group
        # above -- same media/group, filtered to the OTHER provider instead.
        response = client.get("/api/library/search?q=" + "虚构电影一" + "&provider=quark")
        body = response.get_json()
        item = body["items"][0]
        assert item["link_count"] == 1
        assert item["group_count"] == 1
        assert item["providers"] == ["quark"]
        assert item["has_115"] is False
        blob = json.dumps(body, ensure_ascii=False)
        # A bare "115" substring would also match the ever-present
        # "has_115" key, so check for the quoted-exact provider code
        # instead (as it would appear in a `providers` list or similar).
        assert "\"115\"" not in blob
        assert "115 网盘" not in blob

    def test_omitting_provider_restores_full_counts(self, client, installed_library):
        response = client.get("/api/library/search?q=" + "虚构电影一")
        item = response.get_json()["items"][0]
        assert item["link_count"] == 2
        assert item["providers"] == ["115", "quark"]


class TestNewFilterParams:
    def test_genre_filter_selects_exact_genre(self, client, installed_library):
        response = client.get("/api/library/search?genre=" + "剧情")
        titles = {item["title"] for item in response.get_json()["items"]}
        assert "虚构电影一" in titles
        assert "虚构电影四" not in titles

        response2 = client.get("/api/library/search?genre=" + "动作")
        titles2 = {item["title"] for item in response2.get_json()["items"]}
        assert "虚构电影四" in titles2
        assert "虚构电影一" not in titles2

    def test_source_filter_selects_matching_group(self, client, installed_library):
        response = client.get("/api/library/search?source=webdl")
        titles = {item["title"] for item in response.get_json()["items"]}
        assert "虚构剧集一" in titles
        assert "虚构剧集二" not in titles

    def test_complete_season_filter_selects_matching_group(self, client, installed_library):
        response = client.get("/api/library/search?complete_season=1")
        titles = {item["title"] for item in response.get_json()["items"]}
        assert "虚构剧集一" in titles
        assert "虚构剧集二" not in titles

    def test_has_backdrop_filter_selects_only_media_with_backdrop_and_overview(self, client, installed_library):
        response = client.get("/api/library/search?has_backdrop=1")
        titles = {item["title"] for item in response.get_json()["items"]}
        assert titles == {"虚构电影一"}

    def test_old_params_still_work_alongside_new_ones(self, client, installed_library):
        response = client.get("/api/library/search?type=tv")
        assert response.status_code == 200
        assert response.get_json()["total"] == 2

        response2 = client.get("/api/library/search?type=tv&source=webdl")
        titles2 = {item["title"] for item in response2.get_json()["items"]}
        assert titles2 == {"虚构剧集一"}

        response3 = client.get("/api/library/search?provider=ed2k")
        assert response3.get_json()["total"] == 1


class TestNewFilterParamValidation:
    def test_genre_too_long_is_400(self, client, installed_library):
        response = client.get("/api/library/search?genre=" + "x" * 41)
        assert response.status_code == 400
        assert response.get_json()["code"] == "LIBRARY_SEARCH_GENRE_INVALID"

    def test_source_too_long_is_400(self, client, installed_library):
        response = client.get("/api/library/search?source=" + "x" * 41)
        assert response.status_code == 400
        assert response.get_json()["code"] == "LIBRARY_SEARCH_SOURCE_INVALID"

    def test_has_backdrop_invalid_value_is_400(self, client, installed_library):
        response = client.get("/api/library/search?has_backdrop=2")
        assert response.status_code == 400
        assert response.get_json()["code"] == "LIBRARY_SEARCH_HAS_BACKDROP_INVALID"

    def test_complete_season_invalid_value_is_400(self, client, installed_library):
        response = client.get("/api/library/search?complete_season=abc")
        assert response.status_code == 400
        assert response.get_json()["code"] == "LIBRARY_SEARCH_COMPLETE_SEASON_INVALID"


class TestFiltersFacets:
    def test_filters_has_genres_and_sources(self, client, installed_library):
        response = client.get("/api/library/filters")
        assert response.status_code == 200
        body = response.get_json()
        genre_values = {row["value"] for row in body["genres"]}
        assert {"剧情", "动作"} <= genre_values

        source_values = {row["value"]: row["count"] for row in body["sources"]}
        assert source_values == {"webdl": 1, "hdtv": 1}

    def test_response_never_contains_markers(self, client, installed_library, library_markers):
        response = client.get("/api/library/filters")
        blob = json.dumps(response.get_json(), ensure_ascii=False)
        for marker in library_markers:
            assert marker not in blob


# ---------------------------------------------------------------------------
# T17 §14.5: ratings/ratings_status/primary_rating passthrough on both
# /api/library/search and /api/library/media/<id>.
# ---------------------------------------------------------------------------


class TestRatingsPassthroughAPI:
    def _set_ratings(self, installed_library, media_id, *, imdb_id=None, ratings=None, status="complete"):
        conn = installed_library.connect()
        try:
            conn.execute(
                "UPDATE media SET imdb_id=?, ratings_json=?, ratings_status=? WHERE id=?",
                (imdb_id, json.dumps(ratings or {}), status, media_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_media_detail_ratings_passthrough(self, client, installed_library):
        # media #1 (虚构电影一) has tmdb_id=900001 from build_installed.py.
        self._set_ratings(
            installed_library, 1, imdb_id="tt0137523",
            ratings={"tmdb": {"score": 8.7, "votes": 31198}, "imdb": {"score": 9.3, "votes": 3200000}},
        )
        response = client.get("/api/library/media/1")
        body = response.get_json()
        assert body["ratings_status"] == "complete"
        assert body["ratings"]["tmdb"] == {"score": 8.7, "votes": 31198, "url": "https://www.themoviedb.org/movie/900001"}
        assert body["ratings"]["imdb"] == {"score": 9.3, "votes": 3200000, "url": "https://www.imdb.com/title/tt0137523/"}

    def test_media_detail_ratings_default_pending_empty(self, client, installed_library):
        response = client.get("/api/library/media/2")
        body = response.get_json()
        assert body["ratings"] == {}
        assert body["ratings_status"] == "pending"

    def test_search_item_ratings_and_primary_rating(self, client, installed_library):
        self._set_ratings(
            installed_library, 1, imdb_id="tt0137523",
            ratings={"tmdb": {"score": 8.7, "votes": 31198}, "imdb": {"score": 9.3, "votes": 3200000}},
        )
        response = client.get("/api/library/search?q=" + "虚构电影一")
        item = response.get_json()["items"][0]
        assert item["ratings_status"] == "complete"
        assert item["primary_rating"] == {
            "source": "tmdb", "score": 8.7, "votes": 31198, "url": "https://www.themoviedb.org/movie/900001",
        }

    def test_ratings_survive_provider_filter(self, client, installed_library):
        # T17 §14.6 item 6: ratings must keep showing under a provider
        # filter, even though every non-matching provider disappears.
        self._set_ratings(installed_library, 1, ratings={"tmdb": {"score": 7.0, "votes": 10}})
        response = client.get("/api/library/media/1?provider=quark")
        body = response.get_json()
        assert body["ratings"]["tmdb"]["score"] == 7.0
        blob = json.dumps(body, ensure_ascii=False)
        assert "\"provider\": \"115\"" not in blob

    def test_never_shows_a_fake_zero_zero_score(self, client, installed_library):
        self._set_ratings(installed_library, 1, ratings={"tmdb": {"score": 0, "votes": 0}}, status="none")
        response = client.get("/api/library/media/1")
        body = response.get_json()
        assert body["ratings"] == {}
        assert "0.0" not in json.dumps(body)
