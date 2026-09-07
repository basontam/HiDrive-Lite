"""Tests for library_search.py: tokenizer, offline BM25 index build, query
planner, recall/rerank, suggest, corrections.

All titles/URLs below are synthetic fixtures, not real media or shares:
hostnames use real domain shapes but share codes are prefixed ``swfake``/
``fake`` and access codes are fixed 4-character placeholders, per the
project's test-data rule.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_search as lse  # noqa: E402
import library_store as ls  # noqa: E402

K1 = lse.K1
B = lse.B


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_media(**kwargs) -> ls.MediaRecord:
    defaults = dict(
        media_identity=f"tt-fake-{kwargs.get('title_zh', 'x')}",
        media_type="movie",
        title_zh="虚构标题",
        search_key="虚构标题",
    )
    defaults.update(kwargs)
    if "media_identity" not in kwargs:
        defaults["media_identity"] = f"tt-fake-{abs(hash(defaults['title_zh']))}"
    return ls.MediaRecord(**defaults)


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    return library


# ---------------------------------------------------------------------------
# tokenize / fold
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_cjk_run_produces_bigrams_chars_and_run(self):
        tokens = lse.tokenize("黑客帝国", {})
        bigrams = [t for t, k in tokens if k == "cjk_bigram"]
        chars = [t for t, k in tokens if k == "cjk_char"]
        runs = [t for t, k in tokens if k == "cjk_run"]
        assert bigrams == ["黑客", "客帝", "帝国"]
        assert chars == ["黑", "客", "帝", "国"]
        assert runs == ["黑客帝国"]

    def test_latin_run_splits_into_words_and_the_is_a_stopword(self):
        tokens = lse.tokenize("The Matrix", {})
        latin = [t for t, k in tokens if k == "latin"]
        assert latin == ["the", "matrix"]
        assert "the" in lse.STOPWORDS_LATIN
        assert "matrix" not in lse.STOPWORDS_LATIN

    def test_di_bu_roman_and_digit_all_normalise_to_number_2(self):
        for text in ("第二部", "第2部", "II", "2"):
            tokens = lse.tokenize(text, {})
            numbers = [t for t, k in tokens if k == "number"]
            assert numbers == ["2"], f"{text!r} -> {tokens!r}"

    def test_plain_year_number_kept_as_is(self):
        tokens = lse.tokenize("1999", {})
        assert tokens == [("1999", "number")]

    def test_mixed_cjk_and_latin_segments_each_processed(self):
        tokens = lse.tokenize("机动战士高达 GQuuuuuuX", {})
        kinds = {k for _, k in tokens}
        assert "cjk_bigram" in kinds
        assert "cjk_run" in kinds
        latin = [t for t, k in tokens if k == "latin"]
        assert latin == ["gquuuuuux"]

    def test_traditional_chars_fold_through_charmap_before_tokenizing(self):
        charmap = {"國": "国"}
        tokens = lse.tokenize("黑客帝國", charmap)
        bigrams = [t for t, k in tokens if k == "cjk_bigram"]
        assert bigrams == ["黑客", "客帝", "帝国"]

    def test_tokenize_is_deterministic(self):
        a = lse.tokenize("黑客帝国 The Matrix 1999", {})
        b = lse.tokenize("黑客帝国 The Matrix 1999", {})
        assert a == b


# ---------------------------------------------------------------------------
# build_charmap
# ---------------------------------------------------------------------------


class TestBuildCharmap:
    def test_only_keeps_chars_where_convert_differs(self):
        def convert(ch: str) -> str:
            return {"國": "国", "愛": "爱"}.get(ch, ch)

        charmap = lse.build_charmap(["黑客帝國", "愛情"], convert)
        assert charmap == {"國": "国", "愛": "爱"}
        assert "黑" not in charmap  # convert("黑") == "黑", not collected


# ---------------------------------------------------------------------------
# build_index
# ---------------------------------------------------------------------------


class TestBuildIndex:
    def test_bm25_hand_calculation_three_doc_corpus(self, store):
        """Hand-computed BM25 for a 3-doc, title-only, no-stopword corpus.

        Docs (title_zh only, tokenised as plain latin words):
          A: "apple banana"        -> doc_len 2
          B: "apple cherry"        -> doc_len 2
          C: "apple apple banana"  -> doc_len 3 (tf(apple)=2)

        avgdl = (2+2+3)/3 = 7/3
        df(apple)=3, df(banana)=2, df(cherry)=1
        idf(t) = ln(1 + (N-df+0.5)/(df+0.5))
          idf(apple)  = ln(1 + 0.5/3.5)  = 0.133531...
          idf(banana) = ln(1 + 1.5/2.5)  = 0.470004...
          idf(cherry) = ln(1 + 2.5/1.5)  = 0.980829...
        bm25_tf(tf, doc_len) = tf*(k1+1) / (tf + k1*(1-b+b*doc_len/avgdl))
        weight = idf * bm25_tf * field_weight(title=3.0) * kind_weight(latin=1.0)

        Expected (computed with the same formula, k1=1.2 b=0.75):
          A/apple  = 0.425459   A/banana = 1.497529
          B/apple  = 0.425459   B/cherry = 3.125125
          C/apple  = 0.509847   C/banana = 1.262452
        """
        media_a = store.upsert_media(_make_media(title_zh="apple banana", search_key="apple banana"))
        media_b = store.upsert_media(_make_media(title_zh="apple cherry", search_key="apple cherry"))
        media_c = store.upsert_media(_make_media(title_zh="apple apple banana", search_key="apple apple banana"))

        lse.build_index(store)

        conn = store.connect(readonly=True)
        try:
            rows = {
                (r["media_id"], r["term"]): r["weight"]
                for r in conn.execute("SELECT media_id, term, weight FROM search_term WHERE field='title'")
            }
        finally:
            conn.close()

        assert rows[(media_a, "apple")] == pytest.approx(0.425459, rel=1e-4)
        assert rows[(media_a, "banana")] == pytest.approx(1.497529, rel=1e-4)
        assert rows[(media_b, "apple")] == pytest.approx(0.425459, rel=1e-4)
        assert rows[(media_b, "cherry")] == pytest.approx(3.125125, rel=1e-4)
        assert rows[(media_c, "apple")] == pytest.approx(0.509847, rel=1e-4)
        assert rows[(media_c, "banana")] == pytest.approx(1.262452, rel=1e-4)

    def test_field_weights_apply(self, store):
        media_id = store.upsert_media(
            _make_media(
                title_zh="hello",
                search_key="hello",
                overview="hello world there",
            )
        )
        lse.build_index(store)
        conn = store.connect(readonly=True)
        try:
            title_w = conn.execute(
                "SELECT weight FROM search_term WHERE media_id=? AND term='hello' AND field='title'",
                (media_id,),
            ).fetchone()["weight"]
            overview_w = conn.execute(
                "SELECT weight FROM search_term WHERE media_id=? AND term='hello' AND field='overview'",
                (media_id,),
            ).fetchone()["weight"]
        finally:
            conn.close()
        # same term, same doc: only field_weight differs (title 3.0 vs overview 0.3)
        assert title_w > overview_w
        assert title_w == pytest.approx(overview_w * (3.0 / 0.3), rel=1e-6)

    def test_static_boost_is_monotonic(self, store):
        bare_id = store.upsert_media(_make_media(title_zh="裸媒体", search_key="裸媒体"))
        rich_id = store.upsert_media(
            _make_media(title_zh="丰富媒体", search_key="丰富媒体", match_status="exact")
        )
        group_id = store.upsert_group(ls.GroupRecord(media_id=rich_id, edition_fingerprint="fp1", display_title="g1"))
        for i in range(3):
            store.upsert_link(
                ls.LinkRecord(
                    public_id=f"pub-fake-{i}",
                    group_id=group_id,
                    provider="115",
                    canonical_url_hash=f"hash-fake-{i}",
                    url_label="115 分享 · swf…1",
                )
            )
        store.recount()

        lse.build_index(store)

        conn = store.connect(readonly=True)
        try:
            bare_boost = conn.execute(
                "SELECT static_boost FROM search_doc WHERE media_id=?", (bare_id,)
            ).fetchone()["static_boost"]
            rich_boost = conn.execute(
                "SELECT static_boost FROM search_doc WHERE media_id=?", (rich_id,)
            ).fetchone()["static_boost"]
        finally:
            conn.close()
        assert bare_boost == pytest.approx(1.0)
        assert rich_boost > bare_boost

    def test_pinyin_written_when_callable_given(self, store):
        media_id = store.upsert_media(_make_media(title_zh="长安的荔枝", search_key="长安的荔枝"))

        def fake_pinyin(title: str) -> tuple[str, str]:
            mapping = {"长安的荔枝": ("changandelizhi", "cadlz")}
            return mapping.get(title, ("", ""))

        lse.build_index(store, pinyin=fake_pinyin)

        conn = store.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT pinyin_full, pinyin_initials FROM search_doc WHERE media_id=?", (media_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["pinyin_full"] == "changandelizhi"
        assert row["pinyin_initials"] == "cadlz"

    def test_pinyin_fields_strip_leading_punctuation_and_spaces(self, store):
        """T5-polish finding #3: a title starting with a bracket/hash (e.g.
        a franchise-numbering marker) must not leave that punctuation (or
        the space a raw pypinyin-style converter inserts around it) sitting
        at position 0 of pinyin_full/pinyin_initials -- that breaks prefix
        matching from the first real letter."""
        media_id = store.upsert_media(_make_media(title_zh="#长安的荔枝", search_key="#长安的荔枝"))

        def fake_pinyin(title: str) -> tuple[str, str]:
            # A raw pypinyin-style converter passes non-CJK source
            # characters (the leading '#', spaces between syllables)
            # straight through unconverted.
            mapping = {"#长安的荔枝": ("# changandelizhi", "# cadlz")}
            return mapping.get(title, ("", ""))

        lse.build_index(store, pinyin=fake_pinyin)

        conn = store.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT pinyin_full, pinyin_initials FROM search_doc WHERE media_id=?", (media_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["pinyin_full"] == "changandelizhi"
        assert row["pinyin_initials"] == "cadlz"

    def test_two_builds_are_identical(self, store):
        store.upsert_media(_make_media(title_zh="黑客帝国", search_key="黑客帝国", overview="一部科幻电影"))
        store.upsert_media(_make_media(title_zh="The Matrix", search_key="the matrix"))

        stats1 = lse.build_index(store, charmap={"國": "国"})
        conn = store.connect(readonly=True)
        try:
            docs1 = conn.execute("SELECT * FROM search_doc ORDER BY media_id").fetchall()
            terms1 = conn.execute("SELECT * FROM search_term ORDER BY term, media_id, field").fetchall()
            vocab1 = conn.execute("SELECT * FROM search_vocab ORDER BY term").fetchall()
            charmap1 = conn.execute("SELECT * FROM search_charmap ORDER BY src").fetchall()
        finally:
            conn.close()

        stats2 = lse.build_index(store, charmap={"國": "国"})
        conn = store.connect(readonly=True)
        try:
            docs2 = conn.execute("SELECT * FROM search_doc ORDER BY media_id").fetchall()
            terms2 = conn.execute("SELECT * FROM search_term ORDER BY term, media_id, field").fetchall()
            vocab2 = conn.execute("SELECT * FROM search_vocab ORDER BY term").fetchall()
            charmap2 = conn.execute("SELECT * FROM search_charmap ORDER BY src").fetchall()
        finally:
            conn.close()

        assert [tuple(r) for r in docs1] == [tuple(r) for r in docs2]
        assert [tuple(r) for r in terms1] == [tuple(r) for r in terms2]
        assert [tuple(r) for r in vocab1] == [tuple(r) for r in vocab2]
        assert [tuple(r) for r in charmap1] == [tuple(r) for r in charmap2]
        assert stats1 == stats2

    def test_kind_weight_ordering_in_tiny_corpus(self, store):
        """cjk_run (1.5) > cjk_bigram (1.0) > cjk_char (0.25) in a controlled corpus.

        A single doc with a title of 4 distinct, non-repeating CJK chars makes
        every token (each bigram, each char, the run) have tf=1 and df=1 (the
        only doc), so idf/bm25_tf/field_weight are identical across all of
        them -- the only thing that can differ is KIND_WEIGHTS[kind].
        """
        media_id = store.upsert_media(_make_media(title_zh="黑客帝国", search_key="黑客帝国"))
        lse.build_index(store)

        conn = store.connect(readonly=True)
        try:
            def weight(term):
                return conn.execute(
                    "SELECT weight FROM search_term WHERE media_id=? AND term=? AND field='title'",
                    (media_id, term),
                ).fetchone()["weight"]

            run_w = weight("黑客帝国")
            bigram_w = weight("黑客")
            char_w = weight("黑")
        finally:
            conn.close()

        assert run_w > bigram_w > char_w
        assert run_w == pytest.approx(bigram_w * 1.5, rel=1e-6)
        assert bigram_w == pytest.approx(char_w * 4.0, rel=1e-6)

    def test_charmap_only_contains_corpus_characters(self, store):
        store.upsert_media(_make_media(title_zh="黑客帝國", search_key="黑客帝國"))
        # charmap has an entry for a character that never appears in the corpus.
        charmap = {"國": "国", "愛": "爱"}

        lse.build_index(store, charmap=charmap)

        conn = store.connect(readonly=True)
        try:
            rows = {r["src"]: r["dst"] for r in conn.execute("SELECT src, dst FROM search_charmap")}
        finally:
            conn.close()
        assert rows == {"國": "国"}

    def test_build_index_clears_previous_index(self, store):
        store.upsert_media(_make_media(title_zh="第一次", search_key="第一次"))
        lse.build_index(store)
        store.upsert_media(_make_media(title_zh="第二次", search_key="第二次"))
        stats = lse.build_index(store)
        conn = store.connect(readonly=True)
        try:
            doc_count = conn.execute("SELECT COUNT(*) FROM search_doc").fetchone()[0]
        finally:
            conn.close()
        assert doc_count == 2
        assert stats.media_count == 2


# ---------------------------------------------------------------------------
# parse_query
# ---------------------------------------------------------------------------


class TestParseQuery:
    @pytest.mark.parametrize(
        "q, expected",
        [
            ("黑客帝国 1999", {"text": "黑客帝国", "year": 1999}),
            ("全职高手 S02", {"text": "全职高手", "season": 2}),
            ("全职高手 第2季", {"text": "全职高手", "season": 2}),
            ("权力的游戏 E05", {"text": "权力的游戏", "episode": 5}),
            ("沙丘 2160p", {"text": "沙丘", "quality": "2160p"}),
            ("沙丘 4K", {"text": "沙丘", "quality": "2160p"}),
            ("奥本海默 杜比视界", {"text": "奥本海默", "hdr": "dv"}),
            ("盗梦空间 115", {"text": "盗梦空间", "providers": ("115",)}),
            ("盗梦空间 115 夸克", {"text": "盗梦空间", "providers": ("115", "quark")}),
            ("长安的荔枝 天翼", {"text": "长安的荔枝", "providers": ("tianyicloud",)}),
            ("沙丘 电影", {"text": "沙丘", "media_type": "movie"}),
            ("权力的游戏 美剧", {"text": "权力的游戏", "media_type": "tv"}),
            ("间谍过家家 动画", {"text": "间谍过家家", "genre": "动画"}),
            ("黑客帝国 资源 下载", {"text": "黑客帝国"}),
            ("2012", {"text": "2012", "year": None}),
            ("matrix 1999 4k 115", {"text": "matrix", "year": 1999, "quality": "2160p", "providers": ("115",)}),
        ],
    )
    def test_examples(self, q, expected):
        plan = lse.parse_query(q)
        for key, value in expected.items():
            assert getattr(plan, key) == value, f"{q!r}: {key} expected {value!r}, got {getattr(plan, key)!r}"


class TestParseQueryStandaloneTokens:
    """Hotfix: intent words (type/provider/noise/quality/hdr) must only be
    recognised as standalone whitespace/string-boundary-delimited tokens,
    never carved out of the interior of a longer CJK run or alnum run --
    otherwise a title that happens to contain e.g. "剧集"/"夸克" as a
    substring gets mangled into a type/provider filter and the real title
    text is lost (search-quality task finding)."""

    @pytest.mark.parametrize(
        "q, expected",
        [
            ("电影往事", {"text": "电影往事", "media_type": None}),
            ("电影 往事", {"text": "往事", "media_type": "movie"}),
            ("测试剧集名", {"text": "测试剧集名", "media_type": None}),
            ("夸克传说", {"providers": ()}),
            ("传说 夸克", {"providers": ("quark",)}),
            ("S115E01", {"providers": ()}),
            ("1150", {"providers": ()}),
        ],
    )
    def test_examples(self, q, expected):
        plan = lse.parse_query(q)
        for key, value in expected.items():
            assert getattr(plan, key) == value, f"{q!r}: {key} expected {value!r}, got {getattr(plan, key)!r}"


class TestParseQuerySpans:
    """QueryPlan.spans: original (pre-normalisation) substrings consumed per
    dimension, so the frontend can remove exactly that text when a filter
    chip is dismissed."""

    def test_spans_track_original_consumed_substrings(self):
        plan = lse.parse_query("黑客帝国 4K 115 1999")
        assert plan.spans == {"quality": ("4K",), "providers": ("115",), "year": ("1999",)}

    def test_spans_preserve_original_case_and_multiple_providers(self):
        plan = lse.parse_query("盗梦空间 115 夸克")
        assert plan.spans == {"providers": ("115", "夸克")}

    def test_spans_empty_when_nothing_parsed(self):
        plan = lse.parse_query("黑客帝国")
        assert plan.spans == {}

    def test_interpreted_spans_matches_plan_for_store_query(self, store):
        lse.build_index(store)
        page = lse.search(store, "黑客帝国 4K 115 1999", lse.Filters())
        assert page.interpreted["spans"] == {"quality": ["4K"], "providers": ["115"], "year": ["1999"]}

    def test_explicit_filter_override_does_not_change_spans(self, store):
        lse.build_index(store)
        page = lse.search(store, "黑客帝国 4K", lse.Filters(quality="1080p"))
        assert page.interpreted["quality"] == "1080p"
        assert page.interpreted["spans"]["quality"] == ["4K"]


# ---------------------------------------------------------------------------
# search: relevance / recall / rerank behaviour
# ---------------------------------------------------------------------------


def _pinyin_table(title: str) -> tuple[str, str]:
    table = {"长安的荔枝": ("changandelizhi", "cadlz")}
    return table.get(title, ("", ""))


@pytest.fixture
def relevance_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = {}
    ids["matrix"] = library.upsert_media(
        _make_media(title_zh="黑客帝国", search_key="黑客帝国", title_original="The Matrix", year=1999, media_type="movie")
    )
    ids["matrix_decoy"] = library.upsert_media(
        _make_media(title_zh="黑客帝国动画版", search_key="黑客帝国动画版", year=2003, media_type="movie")
    )
    ids["changan"] = library.upsert_media(
        _make_media(title_zh="长安的荔枝", search_key="长安的荔枝", year=2023, media_type="tv")
    )
    ids["inception"] = library.upsert_media(
        _make_media(title_zh="盗梦空间", search_key="盗梦空间", title_original="Inception", year=2010, media_type="movie")
    )
    ids["space_time"] = library.upsert_media(
        _make_media(title_zh="Space Time Voyager", search_key="space time voyager", media_type="movie")
    )
    ids["space_only"] = library.upsert_media(
        _make_media(title_zh="Space Explorer", search_key="space explorer", media_type="movie")
    )
    # Every real media in this library originates from at least one resource
    # link, so `search()`'s default include_deleted=False (which requires at
    # least one non-deleted link) does not filter these fixture rows out.
    for name, media_id in ids.items():
        group_id = library.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-rel-{name}", display_title="g")
        )
        library.upsert_link(
            ls.LinkRecord(
                public_id=f"pub-fake-rel-{name}",
                group_id=group_id,
                provider="115",
                canonical_url_hash=f"hash-fake-rel-{name}",
                url_label="115 分享 · swf…1",
            )
        )
    library.recount()
    lse.build_index(library, pinyin=_pinyin_table)
    return library, ids


class TestSearchRelevance:
    def test_chinese_one_char_typo_still_hits(self, relevance_store):
        store, ids = relevance_store
        page = lse.search(store, "黑客帝园", lse.Filters())
        media_ids = [item["media_id"] for item in page.items]
        assert ids["matrix"] in media_ids

    def test_pinyin_full_and_initials(self, relevance_store):
        store, ids = relevance_store
        for q in ("changandelizhi", "cadlz"):
            page = lse.search(store, q, lse.Filters())
            assert page.items, f"no hits for {q!r}"
            assert page.items[0]["media_id"] == ids["changan"], f"{q!r} top hit"

    def test_english_prefix_hits_original_title(self, relevance_store):
        store, ids = relevance_store
        page = lse.search(store, "ince", lse.Filters())
        media_ids = [item["media_id"] for item in page.items]
        assert ids["inception"] in media_ids

    def test_english_one_char_typo_triggers_corrections(self, relevance_store):
        store, ids = relevance_store
        page = lse.search(store, "incepton", lse.Filters())
        assert page.interpreted["corrections"] == [["incepton", "inception"]]
        assert page.items
        assert page.items[0]["media_id"] == ids["inception"]

    def test_correction_reconstructs_word_level_preserving_cjk(self, relevance_store):
        store, ids = relevance_store
        # 7 CJK chars (>6, so no cjk_run token is emitted for it) that don't
        # overlap with any indexed title -- pure filler so the very first
        # (uncorrected) recall is genuinely empty and the correction fallback
        # actually runs. "incepton" is the same 1-edit typo already covered
        # by TestCorrectTerms, correcting to "inception" (the Inception
        # media's title_original).
        filler = "随便写点东西呀"
        query = f"{filler} incepton"

        page = lse.search(store, query, lse.Filters())

        assert page.interpreted["corrections"] == [["incepton", "inception"]]
        # Only the misspelled Latin word is substituted; the CJK filler is
        # carried over from the original text byte-for-byte (the old code
        # rebuilt this from tokenize()'s full sub-token stream, exploding the
        # CJK run into "随便 便写 写点 ... " and corrupting it).
        assert page.interpreted["text"] == f"{filler} inception"
        assert page.items
        assert page.items[0]["media_id"] == ids["inception"]

    def test_harmony_factor_favours_more_distinct_matched_terms(self, relevance_store):
        store, ids = relevance_store
        page = lse.search(store, "space time", lse.Filters())
        media_ids = [item["media_id"] for item in page.items]
        assert media_ids.index(ids["space_time"]) < media_ids.index(ids["space_only"])

    def test_exact_title_match_ranks_first(self, relevance_store):
        store, ids = relevance_store
        page = lse.search(store, "黑客帝国", lse.Filters())
        assert page.items[0]["media_id"] == ids["matrix"]

    def test_injection_like_query_returns_empty_without_raising_and_db_survives(self, relevance_store):
        store, ids = relevance_store
        page = lse.search(store, "x'; DROP TABLE search_term; --", lse.Filters())
        assert page.total == 0
        assert page.items == []
        # the injection payload must not have harmed the index: a normal
        # query still works afterwards.
        page2 = lse.search(store, "黑客帝国", lse.Filters())
        assert page2.items[0]["media_id"] == ids["matrix"]


class TestSearchEmbeddedIntentWordInTitle:
    """Hotfix: a title that happens to contain an intent word (here "剧集")
    as a substring must be searchable by its full title -- the word must
    not be carved out as a media_type filter, which would (a) corrupt the
    free-text search terms and (b) wrongly constrain media_type, both of
    which can zero out the results (search-quality task finding)."""

    def test_title_containing_intent_word_as_substring_is_found_by_full_title(self, store):
        title = "龙门镖局剧集版"
        media_id = store.upsert_media(
            _make_media(title_zh=title, search_key=title, year=2013, media_type="movie")
        )
        group_id = store.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-embedded", display_title="g")
        )
        store.upsert_link(
            ls.LinkRecord(
                public_id="pub-fake-embedded", group_id=group_id, provider="115",
                canonical_url_hash="hash-fake-embedded", url_label="115 分享 · swf…1",
            )
        )
        store.recount()
        lse.build_index(store)

        page = lse.search(store, title, lse.Filters())

        assert page.interpreted["text"] == title
        assert page.interpreted["media_type"] is None
        media_ids = [item["media_id"] for item in page.items]
        assert media_id in media_ids
        assert page.items[0]["media_id"] == media_id


class TestSearchAliasExactMatch:
    """T5-polish finding #2: a folded query equal to one of a media's alias
    keys (search_doc.alias_keys_json -- which also holds the folded
    title_original, per the module docstring) must get the same x2.0
    rerank boost as an exact title_key match. Before the fix, only
    title_key gets that boost: since the alias field's BM25 weight (2.0)
    is lower than the title field's (3.0), unrelated media whose *titles*
    share most of the query's CJK bigrams/chars can out-score the true
    alias match on raw recall alone."""

    def test_alias_exact_match_ranks_first_over_title_bigram_noise(self, store):
        alias = "云中歌之天下志"  # 7 CJK chars -> no length 3-6 cjk_run token,
        # so this is pure bigram/char overlap (matches the module docstring's
        # own "no cjk_run" boundary), not a special exact-run recall signal.
        target_title = "西山剑传说"
        # Each noise title is `alias` with one extra character spliced into
        # the middle: it shares 5/6 of the query's bigrams and all 7 chars
        # (title field, weight 3.0) but is deliberately *not* a substring of
        # the query (so it earns neither rerank's prefix nor substring
        # boost) -- isolating the raw title-field-weight advantage that lets
        # bigram noise currently out-rank a true alias match.
        noise_titles = ["云中月歌之天下志", "云中歌星之天下志", "云中歌之辰天下志", "云中歌之天海下志"]

        target_id = store.upsert_media(
            _make_media(title_zh=target_title, search_key=target_title, title_alt_json=json.dumps([alias]))
        )
        noise_ids = [
            store.upsert_media(_make_media(title_zh=t, search_key=t, media_identity=f"tt-fake-alias-noise-{i}"))
            for i, t in enumerate(noise_titles)
        ]
        named_ids = [("target", target_id)] + [(f"noise{i}", nid) for i, nid in enumerate(noise_ids)]
        for name, media_id in named_ids:
            group_id = store.upsert_group(
                ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-alias-{name}", display_title="g")
            )
            store.upsert_link(
                ls.LinkRecord(
                    public_id=f"pub-fake-alias-{name}",
                    group_id=group_id,
                    provider="115",
                    canonical_url_hash=f"hash-fake-alias-{name}",
                    url_label="115 分享 · swf…1",
                )
            )
        store.recount()
        lse.build_index(store)

        page = lse.search(store, alias, lse.Filters())

        assert page.items, "no hits for alias query"
        assert page.items[0]["media_id"] == target_id

    def test_title_key_exact_match_still_ranks_first(self, store):
        """Same competing-noise shape, but the query is the target's own
        title (already boosted pre-fix) -- must stay unaffected."""
        title = "海上繁花录之传奇"
        noise_titles = [title + "外传", title + "前传", title + "番外", title + "续"]

        target_id = store.upsert_media(_make_media(title_zh=title, search_key=title))
        noise_ids = [
            store.upsert_media(_make_media(title_zh=t, search_key=t, media_identity=f"tt-fake-title-noise-{i}"))
            for i, t in enumerate(noise_titles)
        ]
        named_ids = [("target", target_id)] + [(f"noise{i}", nid) for i, nid in enumerate(noise_ids)]
        for name, media_id in named_ids:
            group_id = store.upsert_group(
                ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-title-{name}", display_title="g")
            )
            store.upsert_link(
                ls.LinkRecord(
                    public_id=f"pub-fake-title-{name}",
                    group_id=group_id,
                    provider="115",
                    canonical_url_hash=f"hash-fake-title-{name}",
                    url_label="115 分享 · swf…1",
                )
            )
        store.recount()
        lse.build_index(store)

        page = lse.search(store, title, lse.Filters())

        assert page.items[0]["media_id"] == target_id


class TestCorrectTerms:
    def test_short_terms_are_never_corrected(self, relevance_store):
        store, _ids = relevance_store
        conn = store.connect(readonly=True)
        try:
            assert lse.correct_terms(conn, ["ince"]) == []  # length 4 < 5
        finally:
            conn.close()

    def test_finds_single_edit_correction(self, relevance_store):
        store, _ids = relevance_store
        conn = store.connect(readonly=True)
        try:
            assert lse.correct_terms(conn, ["incepton"]) == [("incepton", "inception")]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# search: filters, pagination, empty-query browse
# ---------------------------------------------------------------------------


@pytest.fixture
def filters_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = {}

    ids["quark_media"] = library.upsert_media(
        _make_media(title_zh="资源甲", search_key="资源甲", year=2020, media_type="movie")
    )
    g1 = library.upsert_group(ls.GroupRecord(media_id=ids["quark_media"], edition_fingerprint="fp-a", display_title="g-a"))
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-a1", group_id=g1, provider="quark",
            canonical_url_hash="hash-fake-a1", url_label="夸克 分享 · swf…1",
        )
    )

    ids["media115"] = library.upsert_media(
        _make_media(title_zh="资源乙", search_key="资源乙", year=2021, media_type="movie")
    )
    g2 = library.upsert_group(ls.GroupRecord(media_id=ids["media115"], edition_fingerprint="fp-b", display_title="g-b"))
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-b1", group_id=g2, provider="115",
            canonical_url_hash="hash-fake-b1", url_label="115 分享 · swf…1",
        )
    )

    ids["all_deleted"] = library.upsert_media(
        _make_media(title_zh="资源丙", search_key="资源丙", year=2022, media_type="movie")
    )
    g3 = library.upsert_group(ls.GroupRecord(media_id=ids["all_deleted"], edition_fingerprint="fp-c", display_title="g-c"))
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-c1", group_id=g3, provider="115",
            canonical_url_hash="hash-fake-c1", url_label="115 分享 · swf…1",
            deleted_at_source=int(time.time()),
        )
    )

    library.recount()
    lse.build_index(library)
    return library, ids


class TestSearchFiltersAndBrowse:
    def test_empty_q_with_type_filter_is_pure_browse(self, filters_store):
        store, ids = filters_store
        page = lse.search(store, "", lse.Filters(media_type="movie"))
        assert page.interpreted["text"] == ""
        media_ids = {item["media_id"] for item in page.items}
        assert ids["quark_media"] in media_ids
        assert ids["media115"] in media_ids

    def test_include_deleted_default_excludes_all_deleted_media(self, filters_store):
        store, ids = filters_store
        page = lse.search(store, "", lse.Filters())
        media_ids = {item["media_id"] for item in page.items}
        assert ids["all_deleted"] not in media_ids

        page_incl = lse.search(store, "", lse.Filters(include_deleted=True))
        media_ids_incl = {item["media_id"] for item in page_incl.items}
        assert ids["all_deleted"] in media_ids_incl

    def test_provider_filter_joins_resource_link(self, filters_store):
        store, ids = filters_store
        page = lse.search(store, "", lse.Filters(providers=("quark",)))
        media_ids = {item["media_id"] for item in page.items}
        assert media_ids == {ids["quark_media"]}


@pytest.fixture
def deleted_links_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = {}

    # media "mixed": one deleted quark link + one live 115 link in the same
    # group -- the provider filter must only count the live one.
    ids["mixed"] = library.upsert_media(_make_media(title_zh="资源丁", search_key="资源丁", year=2019, media_type="movie"))
    g_mixed = library.upsert_group(ls.GroupRecord(media_id=ids["mixed"], edition_fingerprint="fp-mixed", display_title="g"))
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-mixed-quark", group_id=g_mixed, provider="quark",
            canonical_url_hash="hash-fake-mixed-quark", url_label="夸克 分享 · swf…1",
            deleted_at_source=int(time.time()),
        )
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-mixed-115", group_id=g_mixed, provider="115",
            canonical_url_hash="hash-fake-mixed-115", url_label="115 分享 · swf…1",
        )
    )

    # media "quality_dead": a 2160p group whose only link is deleted, plus a
    # separate live 1080p group -- so the whole-media "has a live link" rule
    # alone is satisfied and cannot mask the quality-filter bug on its own;
    # the quality filter must independently ignore the dead group.
    ids["quality_dead"] = library.upsert_media(
        _make_media(title_zh="资源戊", search_key="资源戊", year=2018, media_type="movie")
    )
    g_dead = library.upsert_group(
        ls.GroupRecord(media_id=ids["quality_dead"], edition_fingerprint="fp-dead", display_title="g-dead", quality="2160p")
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-dead", group_id=g_dead, provider="115",
            canonical_url_hash="hash-fake-dead", url_label="115 分享 · swf…1",
            deleted_at_source=int(time.time()),
        )
    )
    g_live = library.upsert_group(
        ls.GroupRecord(media_id=ids["quality_dead"], edition_fingerprint="fp-live", display_title="g-live", quality="1080p")
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-live", group_id=g_live, provider="115",
            canonical_url_hash="hash-fake-live", url_label="115 分享 · swf…1",
        )
    )

    library.recount()
    lse.build_index(library)
    return library, ids


class TestSearchDeletedLinkFilters:
    def test_provider_filter_only_counts_live_links_of_that_provider(self, deleted_links_store):
        store, ids = deleted_links_store
        page = lse.search(store, "", lse.Filters(providers=("quark",), include_deleted=False))
        assert ids["mixed"] not in {item["media_id"] for item in page.items}

        page_incl = lse.search(store, "", lse.Filters(providers=("quark",), include_deleted=True))
        assert ids["mixed"] in {item["media_id"] for item in page_incl.items}

    def test_quality_filter_ignores_group_whose_only_link_is_deleted(self, deleted_links_store):
        store, ids = deleted_links_store
        page = lse.search(store, "", lse.Filters(quality="2160p", include_deleted=False))
        assert ids["quality_dead"] not in {item["media_id"] for item in page.items}

        page_incl = lse.search(store, "", lse.Filters(quality="2160p", include_deleted=True))
        assert ids["quality_dead"] in {item["media_id"] for item in page_incl.items}

    def test_group_count_excludes_groups_whose_only_link_is_deleted(self, deleted_links_store):
        # T8 #7: "quality_dead" has one dead 2160p group (its only link is
        # deleted-at-source) and one live 1080p group -- group_count must
        # only count the live one by default, not every resource_group row.
        store, ids = deleted_links_store
        page = lse.search(store, "", lse.Filters(include_deleted=False))
        item = next(i for i in page.items if i["media_id"] == ids["quality_dead"])
        assert item["group_count"] == 1

        # include_deleted=1 keeps the all-groups count.
        page_incl = lse.search(store, "", lse.Filters(include_deleted=True))
        item_incl = next(i for i in page_incl.items if i["media_id"] == ids["quality_dead"])
        assert item_incl["group_count"] == 2


# ---------------------------------------------------------------------------
# w6: a link.tags flagged `invalid` by the link-validity checker (a
# `link_check` row with status='invalid') must count exactly like a
# deleted_at_source link everywhere library_search does -- see
# library_store.live_link_sql() and .superpowers/sdd/briefs/w6-contract.md.
# ---------------------------------------------------------------------------


@pytest.fixture
def checker_invalid_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = {}

    # "mixed": one checker-invalid quark link + one live 115 link in the
    # same group -- the provider filter must only count the live one.
    ids["mixed"] = library.upsert_media(_make_media(title_zh="资源检测甲", search_key="资源检测甲", year=2019, media_type="movie"))
    g_mixed = library.upsert_group(ls.GroupRecord(media_id=ids["mixed"], edition_fingerprint="fp-ci-mixed", display_title="g"))
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-ci-quark", group_id=g_mixed, provider="quark",
            canonical_url_hash="hash-fake-ci-quark", url_label="夸克 分享 · swf…1",
        )
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-ci-115", group_id=g_mixed, provider="115",
            canonical_url_hash="hash-fake-ci-115", url_label="115 分享 · swf…1",
        )
    )
    library.record_link_check(
        "quark", "hash-fake-ci-quark", status="invalid", reason="share_not_found",
        http_class="200", checked_at=1_700_000_000, next_check_at=1_700_600_000, consecutive_unknown=0,
    )

    # "quality_dead": a 2160p group whose only link is checker-invalid, plus
    # a separate live 1080p group.
    ids["quality_dead"] = library.upsert_media(
        _make_media(title_zh="资源检测乙", search_key="资源检测乙", year=2018, media_type="movie")
    )
    g_dead = library.upsert_group(
        ls.GroupRecord(media_id=ids["quality_dead"], edition_fingerprint="fp-ci-dead", display_title="g-dead", quality="2160p")
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-ci-dead", group_id=g_dead, provider="115",
            canonical_url_hash="hash-fake-ci-dead", url_label="115 分享 · swf…1",
        )
    )
    library.record_link_check(
        "115", "hash-fake-ci-dead", status="invalid", reason="share_expired",
        http_class="200", checked_at=1_700_000_000, next_check_at=1_700_600_000, consecutive_unknown=0,
    )
    g_live = library.upsert_group(
        ls.GroupRecord(media_id=ids["quality_dead"], edition_fingerprint="fp-ci-live", display_title="g-live", quality="1080p")
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-ci-live", group_id=g_live, provider="115",
            canonical_url_hash="hash-fake-ci-live", url_label="115 分享 · swf…1",
        )
    )

    # "all_invalid": every link is checker-invalid -- all_links_invalid must
    # be true and the media excluded from a live-only browse.
    ids["all_invalid"] = library.upsert_media(
        _make_media(title_zh="资源检测丙", search_key="资源检测丙", year=2021, media_type="movie")
    )
    g_all_invalid = library.upsert_group(
        ls.GroupRecord(media_id=ids["all_invalid"], edition_fingerprint="fp-ci-allinvalid", display_title="g")
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-ci-allinvalid", group_id=g_all_invalid, provider="115",
            canonical_url_hash="hash-fake-ci-allinvalid", url_label="115 分享 · swf…1",
        )
    )
    library.record_link_check(
        "115", "hash-fake-ci-allinvalid", status="invalid", reason="share_cancelled",
        http_class="200", checked_at=1_700_000_000, next_check_at=1_700_600_000, consecutive_unknown=0,
    )

    library.recount()
    lse.build_index(library)
    return library, ids


class TestSearchCheckerInvalidLinkFilters:
    def test_provider_filter_only_counts_live_links_of_that_provider(self, checker_invalid_store):
        store, ids = checker_invalid_store
        page = lse.search(store, "", lse.Filters(providers=("quark",), include_deleted=False))
        assert ids["mixed"] not in {item["media_id"] for item in page.items}

        page_incl = lse.search(store, "", lse.Filters(providers=("quark",), include_deleted=True))
        assert ids["mixed"] in {item["media_id"] for item in page_incl.items}

    def test_quality_filter_ignores_group_whose_only_link_is_checker_invalid(self, checker_invalid_store):
        store, ids = checker_invalid_store
        page = lse.search(store, "", lse.Filters(quality="2160p", include_deleted=False))
        assert ids["quality_dead"] not in {item["media_id"] for item in page.items}

        page_incl = lse.search(store, "", lse.Filters(quality="2160p", include_deleted=True))
        assert ids["quality_dead"] in {item["media_id"] for item in page_incl.items}

    def test_media_with_only_checker_invalid_links_is_excluded_by_default(self, checker_invalid_store):
        store, ids = checker_invalid_store
        page = lse.search(store, "", lse.Filters(include_deleted=False))
        assert ids["all_invalid"] not in {item["media_id"] for item in page.items}

        page_incl = lse.search(store, "", lse.Filters(include_deleted=True))
        assert ids["all_invalid"] in {item["media_id"] for item in page_incl.items}


class TestSearchAllLinksInvalidField:
    def test_all_links_invalid_true_when_every_link_is_checker_invalid(self, checker_invalid_store):
        store, ids = checker_invalid_store
        page = lse.search(store, "", lse.Filters(include_deleted=True))
        item = next(i for i in page.items if i["media_id"] == ids["all_invalid"])
        assert item["all_links_invalid"] is True

    def test_all_links_invalid_false_when_one_live_link_remains(self, checker_invalid_store):
        store, ids = checker_invalid_store
        page = lse.search(store, "", lse.Filters(include_deleted=True))
        mixed_item = next(i for i in page.items if i["media_id"] == ids["mixed"])
        quality_dead_item = next(i for i in page.items if i["media_id"] == ids["quality_dead"])
        assert mixed_item["all_links_invalid"] is False
        assert quality_dead_item["all_links_invalid"] is False

    def test_all_links_invalid_ignores_the_providers_filter(self, checker_invalid_store):
        # w6-contract: all_links_invalid is a whole-media fact, not scoped
        # to the active provider filter -- "mixed" has a live 115 link, so
        # even filtered down to provider=quark (which hides that live link
        # from the item's own providers/link_count) the badge must stay
        # false.
        store, ids = checker_invalid_store
        page = lse.search(store, "", lse.Filters(providers=("quark",), include_deleted=True))
        item = next(i for i in page.items if i["media_id"] == ids["mixed"])
        assert item["all_links_invalid"] is False

    def test_all_links_invalid_false_for_media_with_no_links_at_all(self, checker_invalid_store):
        store, ids = checker_invalid_store
        media_id = store.upsert_media(_make_media(title_zh="资源检测无链接", search_key="资源检测无链接", year=2020))
        store.recount()
        lse.build_index(store)
        conn = store.connect(readonly=True)
        try:
            items = lse._fetch_items(conn, [media_id])
        finally:
            conn.close()
        assert items[0]["all_links_invalid"] is False


@pytest.fixture
def quality_hdr_season_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = {}

    def add_media(key, title, **group_kwargs):
        media_id = library.upsert_media(
            _make_media(title_zh=title, search_key=title, media_type="movie", media_identity=f"tt-fake-qhs-{key}")
        )
        group_id = library.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-qhs-{key}", display_title="g", **group_kwargs)
        )
        library.upsert_link(
            ls.LinkRecord(
                public_id=f"pub-fake-qhs-{key}", group_id=group_id, provider="115",
                canonical_url_hash=f"hash-fake-qhs-{key}", url_label="115 分享 · swf…1",
            )
        )
        ids[key] = media_id

    add_media("q2160", "资源A2160", quality="2160p")
    add_media("q1080", "资源B1080", quality="1080p")
    add_media("hdr_dv", "资源C杜比", hdr="dv")
    add_media("hdr_none", "资源D无", hdr=None)
    add_media("season2", "资源E第二季", season_from=2, season_to=2)
    add_media("season1", "资源F第一季", season_from=1, season_to=1)

    library.recount()
    lse.build_index(library)
    return library, ids


class TestSearchQualityHdrSeasonFilters:
    """End-to-end filter tests over live (non-deleted) links."""

    def test_quality_filter_selects_matching_media(self, quality_hdr_season_store):
        store, ids = quality_hdr_season_store
        page = lse.search(store, "", lse.Filters(quality="2160p"))
        media_ids = {item["media_id"] for item in page.items}
        assert ids["q2160"] in media_ids
        assert ids["q1080"] not in media_ids

    def test_hdr_filter_selects_matching_media(self, quality_hdr_season_store):
        store, ids = quality_hdr_season_store
        page = lse.search(store, "", lse.Filters(hdr="dv"))
        media_ids = {item["media_id"] for item in page.items}
        assert ids["hdr_dv"] in media_ids
        assert ids["hdr_none"] not in media_ids

    def test_season_filter_selects_matching_media(self, quality_hdr_season_store):
        store, ids = quality_hdr_season_store
        page = lse.search(store, "", lse.Filters(season=2))
        media_ids = {item["media_id"] for item in page.items}
        assert ids["season2"] in media_ids
        assert ids["season1"] not in media_ids


@pytest.fixture
def paginate_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    for i in range(10):
        media_id = library.upsert_media(
            _make_media(
                title_zh=f"分页测试{i:02d}",
                search_key=f"分页测试{i:02d}",
                year=2001 + i,
                media_type="movie",
                media_identity=f"tt-fake-paginate-{i:02d}",
            )
        )
        group_id = library.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-page-{i:02d}", display_title="g")
        )
        library.upsert_link(
            ls.LinkRecord(
                public_id=f"pub-fake-page-{i:02d}",
                group_id=group_id,
                provider="115",
                canonical_url_hash=f"hash-fake-page-{i:02d}",
                url_label="115 分享 · swf…1",
            )
        )
    library.recount()
    lse.build_index(library)
    return library


class TestSearchPagination:
    def test_total_and_page_slicing_year_desc(self, paginate_store):
        store = paginate_store
        page1 = lse.search(store, "", lse.Filters(media_type="movie"), sort="year_desc", page=1, page_size=4)
        assert page1.total == 10
        assert [item["year"] for item in page1.items] == [2010, 2009, 2008, 2007]

        page3 = lse.search(store, "", lse.Filters(media_type="movie"), sort="year_desc", page=3, page_size=4)
        assert page3.total == 10
        assert [item["year"] for item in page3.items] == [2002, 2001]


@pytest.fixture
def free_text_paginate_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = []
    title = "重复标题稳定排序测试"
    for i in range(12):
        media_id = library.upsert_media(
            _make_media(
                title_zh=title, search_key=title, media_type="movie",
                media_identity=f"tt-fake-freepage-{i:02d}",
            )
        )
        ids.append(media_id)
        group_id = library.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-freepage-{i:02d}", display_title="g")
        )
        library.upsert_link(
            ls.LinkRecord(
                public_id=f"pub-fake-freepage-{i:02d}", group_id=group_id, provider="115",
                canonical_url_hash=f"hash-fake-freepage-{i:02d}", url_label="115 分享 · swf…1",
            )
        )
    library.recount()
    lse.build_index(library)
    return library, ids, title


class TestSearchFreeTextPagination:
    def test_page_two_continues_where_page_one_ended_and_total_is_stable(self, free_text_paginate_store):
        store, ids, title = free_text_paginate_store
        # All 12 docs share the exact same title text, so every recall/rerank
        # score ties -- ties break on ascending media_id (see recall()'s and
        # rerank()'s sort keys), giving a deterministic expected order.
        sorted_ids = sorted(ids)

        page1 = lse.search(store, title, lse.Filters(), page=1, page_size=5)
        page2 = lse.search(store, title, lse.Filters(), page=2, page_size=5)

        assert page1.total == 12
        assert page2.total == 12
        page1_ids = [item["media_id"] for item in page1.items]
        page2_ids = [item["media_id"] for item in page2.items]
        assert page1_ids == sorted_ids[0:5]
        assert page2_ids == sorted_ids[5:10]
        assert set(page1_ids).isdisjoint(page2_ids)


@pytest.fixture
def parsed_filter_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = {}

    def add(key, title, media_type="movie", provider="115", **group_kwargs):
        media_id = library.upsert_media(
            _make_media(title_zh=title, search_key=title, media_type=media_type, media_identity=f"tt-fake-pf-{key}")
        )
        group_id = library.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-pf-{key}", display_title="g", **group_kwargs)
        )
        library.upsert_link(
            ls.LinkRecord(
                public_id=f"pub-fake-pf-{key}", group_id=group_id, provider=provider,
                canonical_url_hash=f"hash-fake-pf-{key}", url_label="115 分享 · swf…1",
            )
        )
        ids[key] = media_id

    # Same title on purpose: only the quality/provider filter parsed out of
    # the query text can tell these two apart.
    add("target", "黑客帝国", quality="2160p", provider="115")
    add("decoy", "黑客帝国", quality="1080p", provider="quark")

    add("tv_s2", "示例剧", media_type="tv", season_from=2, season_to=2)
    add("tv_s1", "示例剧", media_type="tv", season_from=1, season_to=1)

    add("movie_kind", "示例剧二", media_type="movie")
    add("tv_kind", "示例剧二", media_type="tv")

    library.recount()
    lse.build_index(library)
    return library, ids


class TestSearchAppliesParsedFilters:
    """parse_query's quality/hdr/providers/season/media_type/year must
    actually constrain results, not just be echoed in `interpreted`."""

    def test_quality_and_provider_from_text_are_applied(self, parsed_filter_store):
        store, ids = parsed_filter_store
        page = lse.search(store, "黑客帝国 4K 115", lse.Filters())
        media_ids = {item["media_id"] for item in page.items}
        assert media_ids == {ids["target"]}

    def test_season_from_text_is_applied(self, parsed_filter_store):
        store, ids = parsed_filter_store
        page = lse.search(store, "示例剧 S02", lse.Filters())
        media_ids = {item["media_id"] for item in page.items}
        assert media_ids == {ids["tv_s2"]}

    def test_media_type_from_text_is_applied(self, parsed_filter_store):
        store, ids = parsed_filter_store
        page = lse.search(store, "示例剧二 电影", lse.Filters())
        media_ids = {item["media_id"] for item in page.items}
        assert media_ids == {ids["movie_kind"]}

    def test_explicit_filter_overrides_text(self, parsed_filter_store):
        store, ids = parsed_filter_store
        page = lse.search(store, "黑客帝国 4K", lse.Filters(quality="1080p"))
        assert page.interpreted["quality"] == "1080p"
        media_ids = {item["media_id"] for item in page.items}
        assert media_ids == {ids["decoy"]}


# ---------------------------------------------------------------------------
# correct_terms edge cases (beyond the commit-2 happy-path coverage)
# ---------------------------------------------------------------------------


class TestCorrectTermsEdgeCases:
    def test_length_9_or_more_allows_two_edits(self, relevance_store):
        store, ids = relevance_store
        conn = store.connect(readonly=True)
        try:
            # "inceptioonn" (11 chars) is exactly 2 insertions away from the
            # indexed term "inception" (9 chars): insert 'o' after "inceptio"
            # and 'n' at the end. The candidate prefilter requires a matching
            # first letter, so the typo must preserve it.
            corrections = lse.correct_terms(conn, ["inceptioonn"])
        finally:
            conn.close()
        assert corrections == [("inceptioonn", "inception")]

    def test_no_candidate_within_edit_budget_yields_no_correction(self, relevance_store):
        store, ids = relevance_store
        conn = store.connect(readonly=True)
        try:
            # "zzzzzzzzzz" shares no letters with any indexed latin term.
            assert lse.correct_terms(conn, ["zzzzzzzzzz"]) == []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# suggest
# ---------------------------------------------------------------------------


@pytest.fixture
def suggest_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids: dict = {}

    # 9 media sharing the "测试媒体" prefix, with strictly decreasing
    # link_count (9 down to 1) so static_boost is strictly decreasing too --
    # makes the sort-by-static_boost and limit=8-excludes-the-9th assertions
    # unambiguous.
    for i in range(9):
        title = f"测试媒体{i:02d}"
        alias_json = json.dumps(["测试媒体00alias"]) if i == 0 else "[]"
        media_id = library.upsert_media(
            ls.MediaRecord(
                media_identity=f"tt-fake-suggest-{i:02d}",
                media_type="movie",
                title_zh=title,
                search_key=title,
                title_alt_json=alias_json,
            )
        )
        ids[i] = media_id
        group_id = library.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-sg-{i:02d}", display_title="g")
        )
        for link_i in range(9 - i):
            library.upsert_link(
                ls.LinkRecord(
                    public_id=f"pub-fake-sg-{i:02d}-{link_i}",
                    group_id=group_id,
                    provider="115",
                    canonical_url_hash=f"hash-fake-sg-{i:02d}-{link_i}",
                    url_label="115 分享 · swf…1",
                )
            )

    ids["zenith"] = library.upsert_media(
        _make_media(title_zh="Zenith Falls", search_key="zenith falls", media_identity="tt-fake-suggest-zenith")
    )
    zg = library.upsert_group(ls.GroupRecord(media_id=ids["zenith"], edition_fingerprint="fp-sg-zenith", display_title="g"))
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-sg-zenith", group_id=zg, provider="115",
            canonical_url_hash="hash-fake-sg-zenith", url_label="115 分享 · swf…1",
        )
    )

    library.recount()
    lse.build_index(library)
    return library, ids


class TestSuggest:
    def test_limit_caps_at_8_sorted_by_static_boost_desc(self, suggest_store):
        store, ids = suggest_store
        results = lse.suggest(store, "测试媒体", limit=8)
        assert len(results) == 8
        result_ids = [r["media_id"] for r in results]
        assert result_ids == [ids[i] for i in range(8)]
        assert ids[8] not in result_ids

    def test_results_are_deduplicated(self, suggest_store):
        store, ids = suggest_store
        # media 0 matches via BOTH title_key and alias_keys_json prefixes.
        results = lse.suggest(store, "测试媒体", limit=8)
        result_ids = [r["media_id"] for r in results]
        assert result_ids.count(ids[0]) == 1

    def test_too_short_query_returns_empty(self, suggest_store):
        store, ids = suggest_store
        assert lse.suggest(store, "") == []
        assert lse.suggest(store, "a") == []

    def test_one_cjk_char_is_not_too_short(self, suggest_store):
        store, ids = suggest_store
        results = lse.suggest(store, "测")
        assert results

    def test_two_latin_letters_is_not_too_short(self, suggest_store):
        store, ids = suggest_store
        results = lse.suggest(store, "ze")
        assert [r["media_id"] for r in results] == [ids["zenith"]]

    def test_item_shape(self, suggest_store):
        store, ids = suggest_store
        results = lse.suggest(store, "测试媒体00")
        assert results
        item = results[0]
        assert set(item.keys()) == {"media_id", "title", "year", "media_type"}


# ---------------------------------------------------------------------------
# performance smoke test
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestSearchPerformance:
    def test_search_p95_under_200ms_over_15000_media(self, tmp_path):
        db_path = tmp_path / "media-library-perf.db"
        library = ls.LibraryStore(db_path)
        library.create_schema()

        # Bulk-insert directly for speed: this is synthetic load generation
        # for the perf smoke test, not a test of upsert_media itself. Titles
        # are built from a large varied CJK character pool (no shared
        # literal prefix/suffix across all 15000 rows) so bigram document
        # frequencies stay realistic instead of every title colliding on a
        # single degenerate high-df term (which would force a huge GROUP BY
        # scan and defeat the point of the perf budget).
        pool = (
            "山水风云龙虎豹狼熊鹰隼鹤鹿马牛羊猴鸡狗猪龟蛇鱼虫鸟花草树叶根茎果实种子"
            "光影声色味触感知觉念心思意志情感恩义礼智信仁勇忠孝廉耻春夏秋冬晨昏晓夜"
            "朝暮晴雨雪雾霜露电磁光热冷暖燥湿金木水火土天地人和平安康宁静动变化生死"
            "存亡兴衰起伏浮沉聚散离合悲欢喜怒哀乐忧愁烦恼欣慰满足饥渴饱暖酸甜苦辣咸"
        )
        pool_len = len(pool)

        def synth_title(i: int) -> str:
            return "".join(pool[(i * 31 + k * 17) % pool_len] for k in range(6))

        titles = [synth_title(i) for i in range(15000)]

        conn = library.connect()
        try:
            now = int(time.time())
            media_rows = []
            for i, title in enumerate(titles):
                media_rows.append(
                    (
                        f"tt-fake-perf-{i:06d}",
                        "movie" if i % 2 == 0 else "tv",
                        title,
                        None,
                        "[]",
                        2000 + (i % 25),
                        title,
                        None, None, None, "[]",
                        "unmatched", None, None,
                        1 if i % 5 == 0 else 0,
                        1 if i % 3 == 0 else 0,
                        now, now,
                    )
                )
            conn.executemany(
                """
                INSERT INTO media (
                    media_identity, media_type, title_zh, title_original, title_alt_json,
                    year, search_key, tmdb_id, overview, poster_path, genres_json,
                    match_status, match_score, match_candidates_json,
                    link_count, has_115, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                media_rows,
            )
            conn.commit()

            media_ids = [row[0] for row in conn.execute("SELECT id FROM media ORDER BY id").fetchall()]
            group_rows = []
            link_rows = []
            for idx, media_id in enumerate(media_ids):
                group_rows.append((media_id, f"fp-perf-{idx:06d}", "2160p", "dv", "g", 0, None, now, now))
            conn.executemany(
                """
                INSERT INTO resource_group (
                    media_id, edition_fingerprint, quality, hdr, display_title,
                    needs_review, review_reason, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                group_rows,
            )
            conn.commit()

            group_ids = [row[0] for row in conn.execute("SELECT id FROM resource_group ORDER BY id").fetchall()]
            for idx, group_id in enumerate(group_ids):
                link_rows.append(
                    (
                        f"pub-fake-perf-{idx:06d}", group_id, "115",
                        f"hash-fake-perf-{idx:06d}", "115 分享 · swf…1", 0, None, None, None, None, now,
                    )
                )
            conn.executemany(
                """
                INSERT INTO resource_link (
                    public_id, group_id, provider, canonical_url_hash, url_label,
                    has_access_code, title_raw, remark, created_at_source, deleted_at_source, imported_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                link_rows,
            )
            conn.commit()
        finally:
            conn.close()

        library.recount()
        lse.build_index(library)

        checkpoint_conn = library.connect()
        try:
            checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            checkpoint_conn.close()

        queries = [titles[123], titles[5000][:4], titles[12345], "2010", titles[777][:3]]
        timings = []
        for i in range(20):
            q = queries[i % len(queries)]
            start = time.perf_counter()
            lse.search(library, q, lse.Filters())
            timings.append(time.perf_counter() - start)

        timings.sort()
        p95 = timings[int(len(timings) * 0.95) - 1]
        assert p95 < 0.2, f"p95={p95:.4f}s over {timings}"


# ---------------------------------------------------------------------------
# u1-backend T2 §5: genre/source/has_backdrop/complete_season Filters
# ---------------------------------------------------------------------------


@pytest.fixture
def genre_source_backdrop_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = {}

    def add_media(key, title, **media_kwargs):
        media_id = library.upsert_media(
            _make_media(
                title_zh=title, search_key=title, media_type="movie",
                media_identity=f"tt-fake-gsb-{key}", **media_kwargs,
            )
        )
        ids[key] = media_id

    add_media("action", "资源动作片", genres_json=json.dumps(["动作"], ensure_ascii=False))
    add_media("drama", "资源剧情片", genres_json=json.dumps(["剧情"], ensure_ascii=False))
    add_media("backdrop", "资源有背景图", backdrop_path="/bg.jpg", overview="一段简介")
    add_media("backdrop_blank_overview", "资源背景图无简介", backdrop_path="/bg2.jpg", overview="")
    add_media("no_backdrop", "资源无背景图")

    def add_group(key, **group_kwargs):
        group_id = library.upsert_group(
            ls.GroupRecord(
                media_id=ids[key], edition_fingerprint=f"fp-gsb-{key}", display_title="g", **group_kwargs,
            )
        )
        library.upsert_link(
            ls.LinkRecord(
                public_id=f"pub-fake-gsb-{key}", group_id=group_id, provider="115",
                canonical_url_hash=f"hash-fake-gsb-{key}", url_label="115 分享 · swf…1",
            )
        )

    add_group("action", source_type="webdl", complete_season=1)
    add_group("drama", source_type="hdtv", complete_season=0)
    add_group("backdrop")
    add_group("backdrop_blank_overview")
    add_group("no_backdrop")

    library.recount()
    lse.build_index(library)
    return library, ids


class TestSearchGenreSourceCompleteSeasonBackdropFilters:
    def test_genre_filter_selects_exact_genre_not_substring(self, genre_source_backdrop_store):
        store, ids = genre_source_backdrop_store
        page = lse.search(store, "", lse.Filters(genre="动作"))
        media_ids = {item["media_id"] for item in page.items}
        assert ids["action"] in media_ids
        assert ids["drama"] not in media_ids

    def test_source_filter_selects_matching_group(self, genre_source_backdrop_store):
        store, ids = genre_source_backdrop_store
        page = lse.search(store, "", lse.Filters(source="webdl"))
        media_ids = {item["media_id"] for item in page.items}
        assert ids["action"] in media_ids
        assert ids["drama"] not in media_ids

    def test_complete_season_filter_selects_matching_group(self, genre_source_backdrop_store):
        store, ids = genre_source_backdrop_store
        page = lse.search(store, "", lse.Filters(complete_season=True))
        media_ids = {item["media_id"] for item in page.items}
        assert ids["action"] in media_ids
        assert ids["drama"] not in media_ids

    def test_has_backdrop_filter_requires_backdrop_and_nonempty_overview(self, genre_source_backdrop_store):
        store, ids = genre_source_backdrop_store
        page = lse.search(store, "", lse.Filters(has_backdrop=True))
        media_ids = {item["media_id"] for item in page.items}
        assert ids["backdrop"] in media_ids
        assert ids["backdrop_blank_overview"] not in media_ids
        assert ids["no_backdrop"] not in media_ids

    def test_filters_also_apply_through_browse(self, genre_source_backdrop_store):
        store, ids = genre_source_backdrop_store
        page = store.browse(lse.Filters(genre="动作"), sort="relevance", page=1, page_size=24)
        media_ids = {item["media_id"] for item in page.items}
        assert media_ids == {ids["action"]}


# ---------------------------------------------------------------------------
# u1-backend T2 §3: search/browse item fields (overview_short, backdrop_path,
# group_count) -- app.py converts backdrop_path -> backdrop_url.
# ---------------------------------------------------------------------------


class TestSearchItemFields:
    def test_items_carry_overview_backdrop_and_group_count(self, genre_source_backdrop_store):
        store, ids = genre_source_backdrop_store
        page = lse.search(store, "", lse.Filters())
        by_id = {item["media_id"]: item for item in page.items}

        backdrop_item = by_id[ids["backdrop"]]
        assert backdrop_item["overview_short"] == "一段简介"
        assert backdrop_item["backdrop_path"] == "/bg.jpg"
        assert backdrop_item["group_count"] == 1

        no_backdrop_item = by_id[ids["no_backdrop"]]
        assert no_backdrop_item["overview_short"] == ""
        assert no_backdrop_item["backdrop_path"] is None

    def test_overview_short_is_trimmed_to_120_chars_with_ellipsis(self, tmp_path):
        db_path = tmp_path / "media-library.db"
        library = ls.LibraryStore(db_path)
        library.create_schema()
        long_overview = "字" * 150
        media_id = library.upsert_media(
            _make_media(title_zh="长简介测试", search_key="长简介测试", overview=long_overview)
        )
        group_id = library.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-long", display_title="g")
        )
        library.upsert_link(
            ls.LinkRecord(
                public_id="pub-fake-long", group_id=group_id, provider="115",
                canonical_url_hash="hash-fake-long", url_label="115 分享 · swf…1",
            )
        )
        library.recount()
        lse.build_index(library)

        page = lse.search(library, "", lse.Filters())
        item = page.items[0]
        assert item["overview_short"][:120] == long_overview[:120]
        assert item["overview_short"].endswith("…")
        assert len(item["overview_short"]) == 121

    def test_overview_short_not_cut_when_under_limit_has_no_ellipsis(self, tmp_path):
        db_path = tmp_path / "media-library.db"
        library = ls.LibraryStore(db_path)
        library.create_schema()
        short_overview = "简短简介"
        media_id = library.upsert_media(
            _make_media(title_zh="短简介测试", search_key="短简介测试", overview=short_overview)
        )
        group_id = library.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-short", display_title="g")
        )
        library.upsert_link(
            ls.LinkRecord(
                public_id="pub-fake-short", group_id=group_id, provider="115",
                canonical_url_hash="hash-fake-short", url_label="115 分享 · swf…1",
            )
        )
        library.recount()
        lse.build_index(library)

        page = lse.search(library, "", lse.Filters())
        item = page.items[0]
        assert item["overview_short"] == short_overview


# ---------------------------------------------------------------------------
# u1-backend follow-up: search-item `providers` list (distinct live
# resource_link.provider codes across all of a media's resource groups,
# "115" first then alphabetical) built by _fetch_items -- shared by
# library_search.search and LibraryStore.browse.
# ---------------------------------------------------------------------------


@pytest.fixture
def providers_field_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = {}

    ids["multi"] = library.upsert_media(_make_media(title_zh="资源供应商甲", search_key="资源供应商甲", media_type="movie"))
    multi_group = library.upsert_group(
        ls.GroupRecord(media_id=ids["multi"], edition_fingerprint="fp-pf-multi", display_title="g")
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-pf-multi-115", group_id=multi_group, provider="115",
            canonical_url_hash="hash-fake-pf-multi-115", url_label="115 分享 · swf…1",
        )
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-pf-multi-quark", group_id=multi_group, provider="quark",
            canonical_url_hash="hash-fake-pf-multi-quark", url_label="夸克 分享 · swf…2",
        )
    )

    ids["no_links"] = library.upsert_media(_make_media(title_zh="资源供应商乙", search_key="资源供应商乙", media_type="movie"))

    ids["deleted_only"] = library.upsert_media(_make_media(title_zh="资源供应商丙", search_key="资源供应商丙", media_type="movie"))
    deleted_group = library.upsert_group(
        ls.GroupRecord(media_id=ids["deleted_only"], edition_fingerprint="fp-pf-deleted", display_title="g")
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-pf-live-alipan", group_id=deleted_group, provider="alipan",
            canonical_url_hash="hash-fake-pf-live-alipan", url_label="alipan 分享 · swf…3",
        )
    )
    library.upsert_link(
        ls.LinkRecord(
            public_id="pub-fake-pf-deleted-baidu", group_id=deleted_group, provider="baidu",
            canonical_url_hash="hash-fake-pf-deleted-baidu", url_label="baidu 分享 · swf…4",
            deleted_at_source=int(time.time()),
        )
    )

    library.recount()
    lse.build_index(library)
    return library, ids


class TestSearchItemProvidersField:
    def test_multiple_live_providers_sorted_with_115_first(self, providers_field_store):
        store, ids = providers_field_store
        page = lse.search(store, "", lse.Filters())
        by_id = {item["media_id"]: item for item in page.items}
        assert by_id[ids["multi"]]["providers"] == ["115", "quark"]

    def test_media_with_no_links_has_empty_providers(self, providers_field_store):
        store, ids = providers_field_store
        page = lse.search(store, "", lse.Filters(include_deleted=True))
        by_id = {item["media_id"]: item for item in page.items}
        assert by_id[ids["no_links"]]["providers"] == []

    def test_deleted_only_provider_is_excluded(self, providers_field_store):
        store, ids = providers_field_store
        page = lse.search(store, "", lse.Filters())
        by_id = {item["media_id"]: item for item in page.items}
        assert by_id[ids["deleted_only"]]["providers"] == ["alipan"]


# ---------------------------------------------------------------------------
# T17 §14.1: _fetch_items recomputes link_count/group_count/has_115/
# providers under a provider filter -- never the media-wide (unfiltered)
# aggregate columns -- for every code path that reaches it (search browse,
# search with text, recommendations pass providers=() and are untouched).
# ---------------------------------------------------------------------------


@pytest.fixture
def provider_recompute_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    ids = {}

    # "mixed": one group with BOTH a 115 and a quark link -- the
    # interesting case where an unfiltered count includes both providers
    # but a provider=115 filter must show only the 115 one.
    ids["mixed"] = library.upsert_media(_make_media(title_zh="资源重算甲", search_key="资源重算甲"))
    g_mixed = library.upsert_group(ls.GroupRecord(media_id=ids["mixed"], edition_fingerprint="fp-pr-mixed", display_title="g"))
    library.upsert_link(ls.LinkRecord(
        public_id="pub-fake-pr-mixed-115", group_id=g_mixed, provider="115",
        canonical_url_hash="hash-fake-pr-mixed-115", url_label="115 分享 · swf…1",
    ))
    library.upsert_link(ls.LinkRecord(
        public_id="pub-fake-pr-mixed-quark", group_id=g_mixed, provider="quark",
        canonical_url_hash="hash-fake-pr-mixed-quark", url_label="夸克 分享 · swf…2",
    ))

    # "quark_only": a second group that has no 115 link at all -- filtering
    # by provider=115 must not count it towards group_count/link_count.
    ids["quark_only"] = library.upsert_media(_make_media(title_zh="资源重算乙", search_key="资源重算乙"))
    g_quark = library.upsert_group(ls.GroupRecord(media_id=ids["quark_only"], edition_fingerprint="fp-pr-quark", display_title="g"))
    library.upsert_link(ls.LinkRecord(
        public_id="pub-fake-pr-quark-only", group_id=g_quark, provider="quark",
        canonical_url_hash="hash-fake-pr-quark-only", url_label="夸克 分享 · swf…3",
    ))

    library.recount()
    lse.build_index(library)
    return library, ids


class TestFetchItemsProviderRecompute:
    def test_no_provider_filter_keeps_full_unfiltered_counts(self, provider_recompute_store):
        store, ids = provider_recompute_store
        page = lse.search(store, "", lse.Filters())
        by_id = {item["media_id"]: item for item in page.items}
        mixed = by_id[ids["mixed"]]
        assert mixed["link_count"] == 2
        assert mixed["group_count"] == 1
        assert mixed["providers"] == ["115", "quark"]
        assert mixed["has_115"] is True

    def test_provider_115_filter_recomputes_to_only_115(self, provider_recompute_store):
        store, ids = provider_recompute_store
        page = lse.search(store, "", lse.Filters(providers=("115",)))
        by_id = {item["media_id"]: item for item in page.items}

        # quark_only never matches the provider EXISTS clause at all.
        assert ids["quark_only"] not in by_id

        mixed = by_id[ids["mixed"]]
        assert mixed["link_count"] == 1
        assert mixed["group_count"] == 1
        assert mixed["providers"] == ["115"]
        assert mixed["has_115"] is True
        blob = json.dumps(mixed, ensure_ascii=False)
        assert "quark" not in blob

    def test_provider_quark_filter_recomputes_and_hides_115(self, provider_recompute_store):
        store, ids = provider_recompute_store
        page = lse.search(store, "", lse.Filters(providers=("quark",)))
        by_id = {item["media_id"]: item for item in page.items}

        mixed = by_id[ids["mixed"]]
        assert mixed["link_count"] == 1
        assert mixed["providers"] == ["quark"]
        assert mixed["has_115"] is False  # the hidden 115 link must not leak through has_115

        quark_only = by_id[ids["quark_only"]]
        assert quark_only["link_count"] == 1
        assert quark_only["group_count"] == 1
        assert quark_only["providers"] == ["quark"]

    def test_omitting_provider_after_filtering_restores_full_counts(self, provider_recompute_store):
        store, ids = provider_recompute_store
        lse.search(store, "", lse.Filters(providers=("115",)))
        page = lse.search(store, "", lse.Filters())
        by_id = {item["media_id"]: item for item in page.items}
        assert by_id[ids["mixed"]]["link_count"] == 2
        assert by_id[ids["mixed"]]["providers"] == ["115", "quark"]


# ---------------------------------------------------------------------------
# T17 fix wave 1 item 2: a media with one 115-only group AND one quark-only
# group -- unlike provider_recompute_store's "mixed" media (one group with
# BOTH providers), this is the case group_count/`groups_summary` used to get
# wrong: group_count was already computed by a separate EXISTS-gated
# subquery, but `groups_summary` (and the sort key ahead of it) was built
# from an UNFILTERED `resource_group` query, so a provider=115 filter still
# listed the quark-only group's display_title alongside the 115 one.
# ---------------------------------------------------------------------------


@pytest.fixture
def two_group_provider_store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    media_id = library.upsert_media(_make_media(title_zh="资源重算丙", search_key="资源重算丙"))
    g_115 = library.upsert_group(ls.GroupRecord(
        media_id=media_id, edition_fingerprint="fp-gs-115", display_title="115 独占版本",
    ))
    library.upsert_link(ls.LinkRecord(
        public_id="pub-fake-gs-115", group_id=g_115, provider="115",
        canonical_url_hash="hash-fake-gs-115", url_label="115 分享 · swf…1",
    ))
    g_quark = library.upsert_group(ls.GroupRecord(
        media_id=media_id, edition_fingerprint="fp-gs-quark", display_title="夸克独占版本",
    ))
    library.upsert_link(ls.LinkRecord(
        public_id="pub-fake-gs-quark", group_id=g_quark, provider="quark",
        canonical_url_hash="hash-fake-gs-quark", url_label="夸克 分享 · swf…2",
    ))
    library.recount()
    lse.build_index(library)
    return library, media_id


class TestGroupsSummaryProviderIsolation:
    def test_provider_115_filter_shows_only_the_115_groups_label(self, two_group_provider_store):
        store, media_id = two_group_provider_store
        page = lse.search(store, "", lse.Filters(providers=("115",)))
        item = next(i for i in page.items if i["media_id"] == media_id)

        assert item["groups_summary"] == ["115 独占版本"]
        assert item["group_count"] == 1

    def test_provider_quark_filter_shows_only_the_quark_groups_label(self, two_group_provider_store):
        store, media_id = two_group_provider_store
        page = lse.search(store, "", lse.Filters(providers=("quark",)))
        item = next(i for i in page.items if i["media_id"] == media_id)

        assert item["groups_summary"] == ["夸克独占版本"]
        assert item["group_count"] == 1

    def test_no_provider_filter_shows_both_groups_labels(self, two_group_provider_store):
        store, media_id = two_group_provider_store
        page = lse.search(store, "", lse.Filters())
        item = next(i for i in page.items if i["media_id"] == media_id)

        assert set(item["groups_summary"]) == {"115 独占版本", "夸克独占版本"}
        assert item["group_count"] == 2


# ---------------------------------------------------------------------------
# T17 §14.5: search items carry ratings/ratings_status/primary_rating from
# the T15 ratings_json/ratings_status columns.
# ---------------------------------------------------------------------------


class TestSearchItemRatings:
    def test_ratings_and_primary_rating_from_stored_json(self, store):
        media_id = store.upsert_media(_make_media(title_zh="评分测试甲", search_key="评分测试甲", tmdb_id=550))
        group_id = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-rating-1", display_title="g"))
        store.upsert_link(ls.LinkRecord(
            public_id="pub-fake-rating-1", group_id=group_id, provider="115",
            canonical_url_hash="hash-fake-rating-1", url_label="115 分享 · swf…1",
        ))
        conn = store.connect()
        try:
            conn.execute(
                "UPDATE media SET imdb_id=?, ratings_json=?, ratings_status='complete' WHERE id=?",
                ("tt0137523", json.dumps({"tmdb": {"score": 8.7, "votes": 31198}, "imdb": {"score": 9.3, "votes": 3200000}}), media_id),
            )
            conn.commit()
        finally:
            conn.close()
        store.recount()
        lse.build_index(store)

        page = lse.search(store, "", lse.Filters())
        item = next(i for i in page.items if i["media_id"] == media_id)
        assert item["ratings_status"] == "complete"
        assert item["ratings"]["tmdb"] == {"score": 8.7, "votes": 31198, "url": "https://www.themoviedb.org/movie/550"}
        assert item["primary_rating"] == {
            "source": "tmdb", "score": 8.7, "votes": 31198, "url": "https://www.themoviedb.org/movie/550",
        }

    def test_missing_ratings_defaults_to_empty_and_pending_primary_none(self, store):
        media_id = store.upsert_media(_make_media(title_zh="评分测试乙", search_key="评分测试乙"))
        group_id = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-rating-2", display_title="g"))
        store.upsert_link(ls.LinkRecord(
            public_id="pub-fake-rating-2", group_id=group_id, provider="115",
            canonical_url_hash="hash-fake-rating-2", url_label="115 分享 · swf…2",
        ))
        store.recount()
        lse.build_index(store)

        page = lse.search(store, "", lse.Filters())
        item = next(i for i in page.items if i["media_id"] == media_id)
        assert item["ratings"] == {}
        assert item["ratings_status"] == "pending"
        assert item["primary_rating"] is None
