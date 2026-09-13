"""Tests for the two-stage name gate and tiered rerank
(docs/claude-search-precision-efficiency-20260910.md §3, §6.1, §6.2).

Stage one recalls only inside the name fields (title / alias / original,
plus the existing pinyin columns) and rejects a candidate whose name
matched nothing; stage two ranks what survived by an explainable
``name_match_tier`` / ``name_coverage`` before the existing BM25 score.

Every title below is invented, and the fixture's links carry placeholder
ids -- no real share, host or access code appears in this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_search as lse  # noqa: E402
import library_store as ls  # noqa: E402

CHARMAP = {"國": "国", "製": "制", "韓": "韩"}


def _pinyin(title: str) -> tuple[str, str]:
    table = {
        "韩国制造": ("hanguozhizao", "hgzz"),
        "美国制造": ("meiguozhizao", "mgzz"),
        "韩国流行音乐偶像": ("hanguoliuxingyinyueouxiang", "hglxyyox"),
    }
    return table.get(title, ("", ""))


@pytest.fixture
def library(tmp_path) -> ls.LibraryStore:
    """The §6.2 fixture library, indexed."""
    store = ls.LibraryStore(tmp_path / "media-library.db")
    store.create_schema()

    rows = [
        # Its overview repeats the title on purpose: §3.3.3's "the overview
        # may raise a gated candidate's score" needs a media matched by both.
        ("hanguo", "韩国制造", None, [], "本剧讲述韩国制造的故事。", 2021, "tv"),
        ("meiguo", "美国制造", None, [], "一部虚构电影。", 2017, "movie"),
        ("tiantang", "天堂制造", None, [], "一部虚构电影。", 2019, "movie"),
        ("idol", "韩国流行音乐偶像", None, [], "一部虚构纪录片。", 2020, "tv"),
        ("alien", "异形2", None, [], "一部虚构续集。", 1986, "movie"),
        ("dune", "沙丘2", None, [], "一部虚构续集。", 2024, "movie"),
        # Name shares nothing with the query; only the overview does.
        ("doc", "无关纪录片", None, [], "本片讲述韩国制造业的历史。", 2015, "movie"),
        # Name evidence lives in the alias and the original title only.
        ("alias", "另一个片名", "Made In Korea", ["韩国制造纪事"], "别名命中的虚构电影。", 2018, "movie"),
        ("numeric", "2012", None, [], "以数字为名的虚构电影。", 2009, "movie"),
        ("latin", "The Matrix", "The Matrix", [], "A fictional film.", 1999, "movie"),
        ("single", "国", None, [], "单字标题的虚构短片。", 2011, "movie"),
    ]
    ids: dict[str, int] = {}
    for key, title, original, aliases, overview, year, media_type in rows:
        media_id = store.upsert_media(
            ls.MediaRecord(
                media_identity=f"gate-fake-{key}",
                media_type=media_type,
                title_zh=title,
                search_key=title,
                title_original=original,
                title_alt_json=json.dumps(aliases, ensure_ascii=False),
                year=year,
                overview=overview,
            )
        )
        ids[key] = media_id
        group_id = store.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint=f"gate-fp-{key}", display_title="g")
        )
        store.upsert_link(
            ls.LinkRecord(
                public_id=f"gate-fake-{key}-1",
                group_id=group_id,
                provider="115",
                canonical_url_hash=f"gate-fake-hash-{key}",
                url_label="115 分享 · fake…1",
            )
        )

    lse.build_index(store, pinyin=_pinyin, charmap=CHARMAP)
    store.fixture_ids = ids  # type: ignore[attr-defined]
    return store


def _titles(store, query, **kwargs) -> list[str]:
    page = lse.search(store, query, lse.Filters(), charmap=CHARMAP, **kwargs)
    return [item["title"] for item in page.items]


def _hits(store, query) -> list[lse.Hit]:
    conn = store.connect(readonly=True)
    try:
        plan = lse.parse_query(query)
        return lse.rerank(conn, lse.recall(conn, plan, CHARMAP), plan, CHARMAP)
    finally:
        conn.close()


def _hit_for(store, query, title_key_id) -> lse.Hit | None:
    return next((h for h in _hits(store, query) if h.media_id == title_key_id), None)


# ---------------------------------------------------------------------------
# §3.2 query-term classification
# ---------------------------------------------------------------------------


class TestClassifyTerms:
    def test_a_trailing_digit_is_numeric_not_name_evidence(self):
        terms = lse.classify_terms("韩国制造2", CHARMAP)
        assert "2" in terms.numeric_terms
        assert "2" not in terms.name_terms
        assert "韩国" in terms.name_terms

    def test_a_pure_numeric_query_falls_back_to_the_number_as_its_name(self):
        terms = lse.classify_terms("2012", CHARMAP)
        assert terms.numeric_terms == ("2012",)
        # Nothing else could ever match a film actually titled "2012".
        assert terms.name_terms == ("2012",)

    def test_units_are_distinct_characters_so_a_repeat_cannot_inflate_coverage(self):
        terms = lse.classify_terms("国国国", CHARMAP)
        assert terms.units == ("国",)

    def test_numbers_stay_out_of_the_coverage_denominator_of_a_mixed_query(self):
        terms = lse.classify_terms("韩国制造2", CHARMAP)
        assert terms.units == ("韩", "国", "制", "造")

    def test_a_latin_stopword_is_not_name_evidence_when_a_real_word_is_present(self):
        terms = lse.classify_terms("the matrix", CHARMAP)
        assert "matrix" in terms.name_terms
        assert "the" not in terms.name_terms

    def test_the_whole_cjk_run_is_the_querys_phrase(self):
        terms = lse.classify_terms("韩国制造2", CHARMAP)
        assert "韩国制造" in terms.runs
        assert "韩国" in terms.bigrams


# ---------------------------------------------------------------------------
# §3.3 the hard name gate
# ---------------------------------------------------------------------------


class TestNameGate:
    def test_a_digit_only_match_is_not_a_name_match(self, library):
        titles = _titles(library, "韩国制造2")
        assert "韩国制造" in titles
        assert "异形2" not in titles
        assert "沙丘2" not in titles

    def test_an_overview_only_match_is_dropped(self, library):
        assert "无关纪录片" not in _titles(library, "韩国制造2")
        assert "无关纪录片" not in _titles(library, "韩国制造")

    def test_a_title_that_also_matches_the_overview_is_kept(self, library):
        # 韩国制造's own overview says nothing about the query; the alias
        # media's does not either -- what matters is that a name hit is
        # never cancelled by the presence of overview text.
        titles = _titles(library, "韩国制造")
        assert titles[0] == "韩国制造"

    def test_an_alias_only_match_is_kept(self, library):
        assert "另一个片名" in _titles(library, "韩国制造纪事")

    def test_an_original_title_only_match_is_kept(self, library):
        assert "另一个片名" in _titles(library, "made in korea")

    def test_a_partial_name_match_survives_but_ranks_below_the_full_one(self, library):
        titles = _titles(library, "韩国制造")
        assert titles[0] == "韩国制造"
        assert "美国制造" in titles
        assert titles.index("美国制造") > 0

    def test_the_overview_still_adds_score_to_a_candidate_that_passed(self, library):
        """§3.3.3: the overview may raise a gated candidate's score, never
        create one. 韩国制造's name qualifies it and its overview repeats the
        title, so its score must exceed the name fields' weight alone."""
        conn = library.connect(readonly=True)
        try:
            plan = lse.parse_query("韩国制造")
            hits = {h.media_id: h for h in lse.recall(conn, plan, CHARMAP)}
            media_id = library.fixture_ids["hanguo"]
            assert media_id in hits
            terms = [t for t, _kind in lse.tokenize("韩国制造", CHARMAP)]
            placeholders = ",".join("?" for _ in terms)
            name_only = conn.execute(
                f"SELECT SUM(weight) AS s FROM search_term WHERE media_id=? "
                f"AND field IN ('title','alias','original') AND term IN ({placeholders})",
                (media_id, *terms),
            ).fetchone()["s"]
            assert hits[media_id].score > name_only
        finally:
            conn.close()

    def test_a_pure_numeric_query_still_finds_a_numeric_title(self, library):
        assert "2012" in _titles(library, "2012")

    def test_a_pure_numeric_query_does_not_drag_in_overview_digits(self, library):
        titles = _titles(library, "2012")
        assert "无关纪录片" not in titles

    def test_a_single_character_query_still_finds_a_single_character_title(self, library):
        assert "国" in _titles(library, "国")

    def test_the_gate_runs_before_the_recall_limit(self, library):
        conn = library.connect(readonly=True)
        try:
            plan = lse.parse_query("韩国制造2")
            stats: dict = {}
            hits = lse.recall(conn, plan, CHARMAP, limit=3, stats=stats)
            # A tiny limit still returns three GATED rows -- the gate is part
            # of the aggregate's HAVING, not a filter applied to its output,
            # so the budget is spent on candidates that passed it.
            assert len(hits) == 3
            assert stats["candidates"] > stats["gated"]
            assert library.fixture_ids["doc"] not in {h.media_id for h in hits}
            # Every row that came back does carry name evidence.
            assert all(h.name_terms for h in lse.rerank(conn, hits, plan, CHARMAP))
        finally:
            conn.close()

    def test_traditional_characters_fold_before_the_gate_is_applied(self, library):
        assert "韩国制造" in _titles(library, "韩國製造")

    def test_case_and_separator_normalisation_reach_the_gate(self, library):
        assert "The Matrix" in _titles(library, "THE-MATRIX")

    def test_a_filter_only_query_keeps_the_browse_behaviour(self, library):
        page = lse.search(library, "", lse.Filters(media_type="movie"), charmap=CHARMAP)
        assert page.total == 9
        assert all(item["media_type"] == "movie" for item in page.items)

    def test_matched_terms_from_the_overview_cannot_bypass_the_gate(self, library):
        hit = _hit_for(library, "韩国制造2", library.fixture_ids["doc"])
        assert hit is None


# ---------------------------------------------------------------------------
# §3.4 tiers and coverage
# ---------------------------------------------------------------------------


class TestTiers:
    def test_an_exact_title_is_tier_zero(self, library):
        hit = _hit_for(library, "韩国制造", library.fixture_ids["hanguo"])
        assert hit.name_match_tier == 0
        assert hit.name_coverage == pytest.approx(1.0)

    def test_an_exact_alias_is_tier_zero(self, library):
        hit = _hit_for(library, "韩国制造纪事", library.fixture_ids["alias"])
        assert hit.name_match_tier == 0

    def test_a_full_phrase_inside_a_longer_query_is_tier_one(self, library):
        hit = _hit_for(library, "韩国制造2", library.fixture_ids["hanguo"])
        assert hit.name_match_tier == 1

    def test_a_shared_bigram_is_tier_two(self, library):
        hit = _hit_for(library, "韩国制造", library.fixture_ids["meiguo"])
        assert hit.name_match_tier == 2
        assert hit.name_coverage == pytest.approx(0.75)

    def test_a_lone_character_of_a_long_query_is_tier_three(self, library):
        hit = _hit_for(library, "韩国流行音乐偶像", library.fixture_ids["single"])
        assert hit.name_match_tier == 3
        assert hit.name_coverage < 0.5

    def test_tier_zero_outranks_every_lower_tier_whatever_the_score(self, library):
        hits = _hits(library, "韩国制造")
        assert hits[0].media_id == library.fixture_ids["hanguo"]
        tiers = [h.name_match_tier for h in hits]
        assert tiers == sorted(tiers)

    def test_coverage_records_its_own_numerator_and_denominator(self, library):
        hit = _hit_for(library, "韩国制造", library.fixture_ids["meiguo"])
        assert hit.name_units_matched == 3
        assert hit.name_units_total == 4

    def test_the_match_source_names_the_evidence(self, library):
        hit = _hit_for(library, "韩国制造", library.fixture_ids["hanguo"])
        assert "title" in hit.match_source
        alias_hit = _hit_for(library, "韩国制造纪事", library.fixture_ids["alias"])
        assert "alias" in alias_hit.match_source

    def test_a_pinyin_query_carries_pinyin_as_its_evidence(self, library):
        hit = _hit_for(library, "hanguozhizao", library.fixture_ids["hanguo"])
        assert "pinyin" in hit.match_source
        assert hit.name_match_tier <= 1

    def test_pinyin_initials_stay_below_a_full_pinyin_match(self, library):
        hit = _hit_for(library, "hgzz", library.fixture_ids["hanguo"])
        assert hit.name_match_tier == 2

    def test_a_number_cannot_outrank_a_full_title_match(self, library):
        hits = _hits(library, "韩国制造2")
        assert hits[0].media_id == library.fixture_ids["hanguo"]

    def test_the_order_is_deterministic_across_repeated_searches(self, library):
        first = [h.media_id for h in _hits(library, "韩国制造2")]
        for _ in range(3):
            assert [h.media_id for h in _hits(library, "韩国制造2")] == first


# ---------------------------------------------------------------------------
# §5 / §6.2 API and paging compatibility
# ---------------------------------------------------------------------------


class TestCompatibility:
    def test_season_parsing_is_unchanged_by_the_gate(self, library):
        for query in ("韩国制造 S02", "韩国制造 第2季"):
            page = lse.search(library, query, lse.Filters(), charmap=CHARMAP)
            assert page.interpreted["season"] == 2
            assert page.interpreted["text"] == "韩国制造"

    def test_a_trailing_digit_is_never_read_as_a_season(self, library):
        page = lse.search(library, "韩国制造2", lse.Filters(), charmap=CHARMAP)
        assert page.interpreted["season"] is None
        assert page.interpreted["text"] == "韩国制造2"

    def test_total_matches_the_gated_set_and_pages_add_up(self, library):
        page = lse.search(library, "韩国制造", lse.Filters(), charmap=CHARMAP, page_size=2)
        assert page.total == len(_titles(library, "韩国制造", page_size=50))
        seen = []
        for number in range(1, 4):
            chunk = lse.search(library, "韩国制造", lse.Filters(), charmap=CHARMAP, page=number, page_size=2)
            seen.extend(item["media_id"] for item in chunk.items)
        assert len(seen) == len(set(seen))

    def test_one_media_yields_one_card(self, library):
        titles = _titles(library, "韩国制造")
        assert len(titles) == len(set(titles))

    def test_the_interpreted_block_keeps_its_shape(self, library):
        page = lse.search(library, "韩国制造 4K 115", lse.Filters(), charmap=CHARMAP)
        assert set(page.interpreted) == {
            "text", "year", "quality", "hdr", "providers", "season",
            "media_type", "corrections", "spans",
        }

    def test_items_carry_no_internal_ranking_fields(self, library):
        page = lse.search(library, "韩国制造", lse.Filters(), charmap=CHARMAP)
        for item in page.items:
            assert "name_match_tier" not in item
            assert "name_coverage" not in item

    def test_include_deleted_still_widens_the_result(self, library):
        page = lse.search(library, "韩国制造", lse.Filters(include_deleted=True), charmap=CHARMAP)
        assert page.total >= 1

    def test_a_latin_typo_still_reaches_its_correction(self, library):
        assert "The Matrix" in _titles(library, "matrx")

    def test_a_query_with_no_name_evidence_anywhere_returns_nothing(self, library):
        assert _titles(library, "紫罗兰永恒花园") == []
