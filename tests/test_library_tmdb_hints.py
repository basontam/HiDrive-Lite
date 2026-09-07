"""Unit tests for T15 (z1-hints): the IMDb-hint /find pathway, TVmaze
fallback, ratings capture on ``exact``, and the new
``tmdb_hints``/``imdb_ratings``/media-metadata schema.

Everything here goes through the same FakeSession pattern as
tests/test_library_tmdb_enrich.py -- the shared tests/conftest.py network
guard additionally blocks any accidental real HTTP call. Test data is
entirely fictional: IMDb ids are ``tt00000NN`` placeholders, titles are
made up.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time as real_time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_store as ls  # noqa: E402
import library_tmdb as tmdb  # noqa: E402


# --- shared fake HTTP plumbing (mirrors tests/test_library_tmdb_enrich.py) ----


class FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class FakeResponse:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    """Route fake responses by URL; a list is consumed one item per call."""

    def __init__(self):
        self._routes: dict[str, object] = {}
        self.calls: list[dict] = []

    def route(self, url, outcome):
        self._routes[url] = outcome
        return self

    def _pop_outcome(self, url):
        routed = self._routes.get(url)
        if routed is None:
            raise AssertionError(f"unscripted request to {url}")
        if isinstance(routed, list):
            if not routed:
                raise AssertionError(f"exhausted script for {url}")
            return routed.pop(0) if len(routed) > 1 else routed[0]
        return routed

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        outcome = self._pop_outcome(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def calls_to(self, url: str) -> list[dict]:
        return [c for c in self.calls if c["url"] == url]


SEARCH_TV_URL = tmdb.TMDB_BASE + "/search/tv"
SEARCH_MOVIE_URL = tmdb.TMDB_BASE + "/search/movie"
GENRE_TV_URL = tmdb.TMDB_BASE + "/genre/tv/list"
GENRE_MOVIE_URL = tmdb.TMDB_BASE + "/genre/movie/list"
TVMAZE_SEARCH_URL = tmdb.TVMAZE_BASE + "/search/shows"


def _find_url(imdb_id: str) -> str:
    return tmdb.TMDB_BASE + f"/find/{imdb_id}"


def _details_url(kind: str, tmdb_id: int) -> str:
    return tmdb.TMDB_BASE + f"/{kind}/{tmdb_id}"


def _client(store, session, *, api_key="tmdb-key-for-tests", **kwargs):
    clock = FakeClock()
    factory = lambda: sqlite3.connect(str(store.db_path))  # noqa: E731
    client = tmdb.TmdbClient(
        api_key,
        conn_factory=factory,
        lock_path=store.db_path.parent / "client.lock",
        session=session,
        clock=clock.time,
        sleep=clock.sleep,
        **kwargs,
    )
    return client, clock


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    conn = library.connect()
    try:
        tmdb.ensure_tables(conn)
        conn.commit()
    finally:
        conn.close()
    return library


def _add_media(
    store, *, title_zh, media_type="movie", year=None, identity, has_115=0, link_count=0,
    match_status="unmatched", tmdb_id=None, poster_path=None, overview=None,
):
    rec = ls.MediaRecord(
        media_identity=identity,
        media_type=media_type,
        title_zh=title_zh,
        search_key=title_zh,
        year=year,
        match_status=match_status,
        tmdb_id=tmdb_id,
        poster_path=poster_path,
        overview=overview,
    )
    media_id = store.upsert_media(rec)
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET has_115=?, link_count=? WHERE id=?", (has_115, link_count, media_id))
        conn.commit()
    finally:
        conn.close()
    return media_id


def _add_imdb_rating(store, imdb_id, *, rating, votes, as_of="2026-09-01"):
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO imdb_ratings (imdb_id, rating, votes, as_of) VALUES (?, ?, ?, ?)",
            (imdb_id, rating, votes, as_of),
        )
        conn.commit()
    finally:
        conn.close()


def _add_hint(store, identity, *, decision, candidates, source="imdb_offline"):
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO tmdb_hints (media_identity, source, decision, candidate_count, candidates_json, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (identity, source, decision, len(candidates), json.dumps(candidates, ensure_ascii=False), 0),
        )
        conn.commit()
    finally:
        conn.close()


def _get_media(store, media_id) -> dict:
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    finally:
        conn.close()
    return dict(row)


def _add_link(store, group_id, *, url, provider="115"):
    """Mirrors tests/test_library_tmdb_enrich.py's own helper -- used only
    by the w5-confirm-enrich merge-keeps-links test below."""
    rec = ls.LinkRecord(
        public_id=f"pub-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}",
        group_id=group_id,
        provider=provider,
        canonical_url_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        url_label=f"{provider} 分享",
    )
    link_id, _created = store.upsert_link(rec)
    return link_id


def _find_movie_result(**overrides) -> dict:
    result = {
        "id": 555, "title": "沙丘", "original_title": "Dune",
        "release_date": "2024-06-01", "poster_path": "/poster.jpg", "backdrop_path": "/backdrop.jpg",
        "overview": "介绍文字", "genre_ids": [], "vote_average": 7.9, "vote_count": 1200,
    }
    result.update(overrides)
    return result


# --- schema -------------------------------------------------------------------


def test_media_table_has_t15_metadata_and_ratings_columns(store):
    conn = store.connect(readonly=True)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(media)").fetchall()}
    finally:
        conn.close()
    for name in (
        "metadata_status", "metadata_source", "tvmaze_id", "imdb_id", "metadata_fetched_at", "metadata_error",
        "ratings_json", "ratings_status", "ratings_fetched_at", "ratings_error",
    ):
        assert name in cols


def test_media_metadata_defaults(store):
    media_id = _add_media(store, title_zh="默认值检查", identity="title:defaults:0:movie")
    row = _get_media(store, media_id)
    assert row["metadata_status"] == "pending"
    assert row["ratings_json"] == "{}"
    assert row["ratings_status"] == "pending"
    assert row["imdb_id"] is None


def test_tmdb_hints_and_imdb_ratings_tables_exist(store):
    conn = store.connect(readonly=True)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    assert "tmdb_hints" in names
    assert "imdb_ratings" in names


# --- hint pathway: proposed_exact -----------------------------------------


def test_proposed_exact_consistent_find_becomes_exact_with_ratings(store):
    media_id = _add_media(store, title_zh="沙丘", media_type="movie", year=2024, identity="title:dune:2024:movie")
    _add_hint(
        store, "title:dune:2024:movie", decision="proposed_exact",
        candidates=[{"imdb_id": "tt0000001", "imdb_type": "movie", "start_year": 2024, "primary_title": "Dune", "original_title": "Dune", "score": 0.99, "reasons": ["title_alias_exact"]}],
    )
    session = (
        FakeSession()
        .route(_find_url("tt0000001"), FakeResponse(200, {"movie_results": [_find_movie_result()], "tv_results": []}))
        .route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.matched_exact == 1
    assert stats.hint_confirmed_exact == 1
    assert stats.hint_find_requests == 1
    assert stats.requests_made == 1

    row = _get_media(store, media_id)
    assert row["match_status"] == "exact"
    assert row["tmdb_id"] == 555
    assert row["poster_path"] == "/poster.jpg"
    assert row["imdb_id"] == "tt0000001"
    ratings = json.loads(row["ratings_json"])
    assert ratings["tmdb"] == {"score": 7.9, "scale": 10, "votes": 1200, "as_of": ratings["tmdb"]["as_of"]}
    assert row["ratings_status"] == "complete"
    explanation = json.loads(row["match_candidates_json"])
    assert explanation["source"] == "imdb_hint"
    assert explanation["imdb_id"] == "tt0000001"
    # never a URL or key in what got written
    dumped = row["match_candidates_json"] + row["ratings_json"]
    assert "api_key" not in dumped and "themoviedb.org" not in dumped


def test_imdb_ratings_joined_and_merged_into_ratings_json(store):
    # T15 fix wave 1 (item 3a/3b): when a matching imdb_ratings row exists,
    # it is joined into ratings_json.imdb and counted as an applicable
    # source (still "complete" here since both tmdb and imdb are present).
    media_id = _add_media(store, title_zh="沙丘二", media_type="movie", year=2024, identity="title:dune2:2024:movie")
    _add_hint(
        store, "title:dune2:2024:movie", decision="proposed_exact",
        candidates=[{"imdb_id": "tt0000009", "imdb_type": "movie", "start_year": 2024, "primary_title": "Dune Two", "score": 0.99, "reasons": []}],
    )
    _add_imdb_rating(store, "tt0000009", rating=8.4, votes=50000, as_of="2026-08-01")
    session = (
        FakeSession()
        .route(_find_url("tt0000009"), FakeResponse(200, {"movie_results": [_find_movie_result()], "tv_results": []}))
        .route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.matched_exact == 1
    row = _get_media(store, media_id)
    ratings = json.loads(row["ratings_json"])
    assert ratings["imdb"] == {"score": 8.4, "scale": 10, "votes": 50000, "as_of": "2026-08-01"}
    assert ratings["tmdb"]["score"] == 7.9  # tmdb entry still present -- merged, not replaced
    assert row["ratings_status"] == "complete"


def test_write_exact_merges_ratings_json_never_replaces_existing_keys(store):
    # A pre-existing ratings_json (e.g. an unrelated source written before
    # this row was ever confirmed exact) must survive a fresh _write_exact
    # untouched -- only the keys _write_exact itself produces are merged
    # in/overwritten.
    media_id = _add_media(store, title_zh="沙丘三", media_type="movie", year=2024, identity="title:dune3:2024:movie")
    _add_hint(
        store, "title:dune3:2024:movie", decision="proposed_exact",
        candidates=[{"imdb_id": "tt0000010", "imdb_type": "movie", "start_year": 2024, "primary_title": "Dune Three", "score": 0.99, "reasons": []}],
    )
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE media SET ratings_json=?, ratings_status='partial' WHERE id=?",
            (json.dumps({"manual": {"score": 9.0, "scale": 10, "votes": None, "as_of": "2026-01-01"}}), media_id),
        )
        conn.commit()
    finally:
        conn.close()
    session = (
        FakeSession()
        .route(_find_url("tt0000010"), FakeResponse(200, {"movie_results": [_find_movie_result()], "tv_results": []}))
        .route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    tmdb.enrich_batch(store, client, limit=1)

    row = _get_media(store, media_id)
    ratings = json.loads(row["ratings_json"])
    assert ratings["manual"] == {"score": 9.0, "scale": 10, "votes": None, "as_of": "2026-01-01"}
    assert ratings["tmdb"]["score"] == 7.9


def test_proposed_review_never_becomes_exact(store):
    media_id = _add_media(store, title_zh="沙丘", media_type="movie", year=2024, identity="title:dune-review:2024:movie")
    _add_hint(
        store, "title:dune-review:2024:movie", decision="proposed_review",
        candidates=[{"imdb_id": "tt0000002", "imdb_type": "movie", "start_year": 2024, "primary_title": "Dune", "score": 0.7, "reasons": ["title_alias_ambiguous"]}],
    )
    session = FakeSession().route(_find_url("tt0000002"), FakeResponse(200, {"movie_results": [_find_movie_result(id=556)], "tv_results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.matched_exact == 0
    assert stats.matched_candidate == 1
    assert stats.hint_candidate == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "candidate"
    explanation = json.loads(row["match_candidates_json"])
    assert explanation["reason"] == "hint_review"
    assert explanation["decision"] == "proposed_review"


def test_year_conflict_forces_needs_review(store):
    media_id = _add_media(store, title_zh="沙丘", media_type="movie", year=2024, identity="title:dune-yr:2024:movie")
    _add_hint(
        store, "title:dune-yr:2024:movie", decision="proposed_exact",
        candidates=[{"imdb_id": "tt0000003", "imdb_type": "movie", "start_year": 2022, "primary_title": "Dune", "score": 0.99, "reasons": []}],
    )
    # TMDB /find confirms the SAME title but a conflicting year (2022 vs
    # the source's 2024, |delta|=2).
    session = FakeSession().route(_find_url("tt0000003"), FakeResponse(200, {"movie_results": [_find_movie_result(id=557, release_date="2022-01-01")], "tv_results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.matched_exact == 0
    assert stats.matched_candidate == 0
    assert stats.matched_needs_review == 1
    assert stats.hint_conflicts == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "needs_review"
    assert row["tmdb_id"] is None


def test_type_conflict_forces_needs_review(store):
    media_id = _add_media(store, title_zh="谜案", media_type="movie", year=2024, identity="title:mystery:2024:movie")
    _add_hint(
        store, "title:mystery:2024:movie", decision="proposed_exact",
        candidates=[{"imdb_id": "tt0000004", "imdb_type": "movie", "start_year": 2024, "primary_title": "Mystery", "score": 0.95, "reasons": []}],
    )
    # /find only has a tv_results hit -- the expected "movie" array is empty.
    session = FakeSession().route(
        _find_url("tt0000004"),
        FakeResponse(200, {"movie_results": [], "tv_results": [_find_movie_result(id=558, title="谜案", name="谜案")]}),
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.matched_needs_review == 1
    assert stats.hint_conflicts == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "needs_review"
    explanation = json.loads(row["match_candidates_json"])
    assert explanation["conflict"] == "type"


def test_empty_find_falls_back_to_normal_search(store):
    media_id = _add_media(store, title_zh="无候选剧", media_type="tv", year=2024, identity="title:nocand:2024:tv")
    _add_hint(
        store, "title:nocand:2024:tv", decision="proposed_candidate",
        candidates=[{"imdb_id": "tt0000005", "imdb_type": "tvSeries", "start_year": 2024, "primary_title": "No Candidate", "score": 0.8, "reasons": []}],
    )
    session = (
        FakeSession()
        .route(_find_url("tt0000005"), FakeResponse(200, {"movie_results": [], "tv_results": []}))
        .route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.hint_find_requests == 1
    # zh-CN + the T15 fix wave 1 en-US retry (still undecidable).
    assert len(session.calls_to(SEARCH_TV_URL)) == 2
    assert stats.matched_exact == 0
    assert stats.matched_candidate == 0
    row = _get_media(store, media_id)
    assert row["match_status"] == "unmatched"


def test_find_transport_failure_falls_back_to_search_not_treated_as_no_candidate(store):
    # T15 fix wave 1 (item 5, §8.1): a /find TRANSPORT failure
    # (failed_retryable/failed_permanent -- as opposed to a genuine empty
    # result) must fall back to the normal search flow exactly like an
    # empty /find, not be silently treated as "hint gave no candidate" and
    # certainly not abandon the row.
    media_id = _add_media(store, title_zh="传输失败剧", media_type="movie", year=2024, identity="title:transportfail:2024:movie")
    _add_hint(
        store, "title:transportfail:2024:movie", decision="proposed_exact",
        candidates=[{"imdb_id": "tt0000099", "imdb_type": "movie", "start_year": 2024, "primary_title": "Transport Fail", "score": 0.9, "reasons": []}],
    )
    session = (
        FakeSession()
        .route(_find_url("tt0000099"), [FakeResponse(503) for _ in range(4)])
        .route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": [{"id": 900, "title": "传输失败剧", "release_date": "2024-01-01"}]}))
        .route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.hint_find_requests == 1
    assert len(session.calls_to(SEARCH_MOVIE_URL)) == 1
    assert stats.matched_exact == 1
    assert stats.search_failed_media_ids == []
    row = _get_media(store, media_id)
    assert row["tmdb_id"] == 900


def test_find_ignores_episode_season_and_person_result_arrays(store):
    # T15 fix wave 1 (item 5, §8.1): tv_episode_results/tv_season_results/
    # person_results in a /find payload must never surface as candidates.
    media_id = _add_media(store, title_zh="分集剧", media_type="tv", year=2024, identity="title:episodes:2024:tv")
    _add_hint(
        store, "title:episodes:2024:tv", decision="proposed_exact",
        candidates=[{"imdb_id": "tt0000098", "imdb_type": "tvSeries", "start_year": 2024, "primary_title": "Episodes", "score": 0.9, "reasons": []}],
    )
    session = (
        FakeSession()
        .route(
            _find_url("tt0000098"),
            FakeResponse(200, {
                "movie_results": [],
                "tv_results": [_find_movie_result(id=561, title="分集剧", name="分集剧")],
                "tv_episode_results": [{"id": 9001, "name": "第一集"}],
                "tv_season_results": [{"id": 9002, "name": "第一季"}],
                "person_results": [{"id": 9003, "name": "某演员"}],
            }),
        )
        .route(GENRE_TV_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.matched_exact == 1
    row = _get_media(store, media_id)
    assert row["tmdb_id"] == 561


def test_find_cache_hit_avoids_second_request(store):
    identity_a = "title:shared-a:2024:movie"
    identity_b = "title:shared-b:2024:movie"
    _add_media(store, title_zh="片名甲", media_type="movie", year=2024, identity=identity_a, has_115=1, link_count=2)
    _add_media(store, title_zh="片名乙", media_type="movie", year=2024, identity=identity_b, has_115=1, link_count=1)
    shared_candidate = [{"imdb_id": "tt0000006", "imdb_type": "movie", "start_year": 2024, "primary_title": "片名甲", "score": 0.9, "reasons": []}]
    _add_hint(store, identity_a, decision="proposed_exact", candidates=shared_candidate)
    _add_hint(store, identity_b, decision="proposed_exact", candidates=shared_candidate)
    session = (
        FakeSession()
        .route(_find_url("tt0000006"), FakeResponse(200, {"movie_results": [_find_movie_result(id=559, title="片名甲")], "tv_results": []}))
        .route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=2)

    assert stats.hint_find_requests == 2
    assert stats.requests_made == 1
    assert stats.cache_hits == 1
    assert len(session.calls_to(_find_url("tt0000006"))) == 1
    # 1 for the shared /find call + 1 for the one exact write's genre()
    # lookup (untracked by stats.requests_made, which only wraps
    # _run_search/_run_find, but still a real budget-reserving request).
    assert client.budget_status()["used"] == 2


def test_decision_not_eligible_skips_hint_pathway(store):
    media_id = _add_media(store, title_zh="无候选", media_type="movie", year=2024, identity="title:noimdb:2024:movie")
    _add_hint(store, "title:noimdb:2024:movie", decision="no_imdb_candidate", candidates=[])
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.hint_find_requests == 0
    # zh-CN + the T15 fix wave 1 en-US retry (still undecidable).
    assert len(session.calls_to(SEARCH_MOVIE_URL)) == 2
    row = _get_media(store, media_id)
    assert row["match_status"] == "unmatched"


# --- TVmaze fallback ------------------------------------------------------


def test_tvmaze_off_by_default(store):
    _add_media(store, title_zh="纯剧集", media_type="tv", year=2024, identity="title:pure-tv:2024:tv")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.tvmaze_requests == 0
    assert len(session.calls_to(TVMAZE_SEARCH_URL)) == 0


def test_tvmaze_enabled_confirms_via_find(store):
    media_id = _add_media(store, title_zh="纯剧集", media_type="tv", year=2024, identity="title:pure-tv2:2024:tv")
    session = (
        FakeSession()
        .route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
        .route(TVMAZE_SEARCH_URL, FakeResponse(200, [{"score": 10.0, "show": {"id": 1, "name": "纯剧集", "externals": {"imdb": "tt0000007"}}}]))
        .route(_find_url("tt0000007"), FakeResponse(200, {"movie_results": [], "tv_results": [_find_movie_result(id=560, title="纯剧集", name="纯剧集")]}))
        .route(GENRE_TV_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1, tvmaze_enabled=True)

    assert stats.tvmaze_requests == 1
    assert len(session.calls_to(TVMAZE_SEARCH_URL)) == 1
    assert stats.matched_exact == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "exact"
    explanation = json.loads(row["match_candidates_json"])
    # T15 fix wave 1 (item 3a): tvmaze_id now carried through so
    # ratings_status_for can treat "tvmaze" as applicable; no
    # tvmaze_rating key here since this fixture's show has no "rating".
    assert explanation == {"source": "tvmaze", "imdb_id": "tt0000007", "tvmaze_id": 1}
    assert row["tvmaze_id"] == 1
    ratings = json.loads(row["ratings_json"])
    assert ratings["tmdb"]["score"] == 7.9
    assert "tvmaze" not in ratings
    # Fix wave 2 (item 3): tvmaze_id alone no longer makes "tvmaze"
    # applicable -- mirroring the imdb rule, it's only applicable when an
    # actual rating value exists. This show has none, so tvmaze is simply
    # not applicable (never "missing" forever) and tmdb-only is complete.
    assert row["ratings_status"] == "complete"


def test_tvmaze_never_called_for_movies(store):
    _add_media(store, title_zh="纯电影", media_type="movie", year=2024, identity="title:pure-movie:2024:movie")
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1, tvmaze_enabled=True)

    assert stats.tvmaze_requests == 0


def test_tvmaze_search_cache_hit_no_second_request(store):
    session = FakeSession().route(TVMAZE_SEARCH_URL, FakeResponse(200, [{"score": 10.0, "show": {"id": 1, "name": "剧名", "externals": {"imdb": "tt0000008"}}}]))
    client, _clock = _client(store, session)

    first = client.tvmaze_search("剧名")
    second = client.tvmaze_search("剧名")

    assert first.status == "ok"
    assert second.status == "ok"
    assert len(session.calls_to(TVMAZE_SEARCH_URL)) == 1


# --- _tvmaze_fetch retry loop (T15 fix wave 1, item 5) -----------------------


def test_tvmaze_fetch_retries_after_429_retry_after_then_succeeds(store):
    session = FakeSession().route(
        TVMAZE_SEARCH_URL,
        [
            FakeResponse(429, headers={"Retry-After": "1"}),
            FakeResponse(200, [{"score": 10.0, "show": {"id": 2, "name": "重试剧", "externals": {"imdb": "tt0000010"}}}]),
        ],
    )
    client, _clock = _client(store, session)

    entry = client.tvmaze_search("重试剧")

    assert entry.status == "ok"
    assert len(session.calls_to(TVMAZE_SEARCH_URL)) == 2


def test_tvmaze_fetch_5xx_retries_then_fails_retryable(store):
    session = FakeSession().route(TVMAZE_SEARCH_URL, [FakeResponse(503) for _ in range(4)])
    client, _clock = _client(store, session)

    entry = client.tvmaze_search("持续故障剧")

    assert entry.status == "failed_retryable"
    assert entry.error_class == "HTTP503"
    assert len(session.calls_to(TVMAZE_SEARCH_URL)) >= 2  # backed off and retried, not a single attempt


def test_tvmaze_fetch_network_exception_retries_then_fails_retryable(store):
    session = FakeSession().route(
        TVMAZE_SEARCH_URL, [requests.exceptions.ConnectionError("boom") for _ in range(4)],
    )
    client, _clock = _client(store, session)

    entry = client.tvmaze_search("网络故障剧")

    assert entry.status == "failed_retryable"
    assert entry.error_class == "ConnectionError"
    assert len(session.calls_to(TVMAZE_SEARCH_URL)) >= 2


# --- hint_stats() -----------------------------------------------------------


def test_hint_stats_counts_by_decision_and_pending(store):
    _add_media(store, title_zh="待定甲", media_type="movie", year=2024, identity="title:pend-a:2024:movie")
    _add_media(store, title_zh="待定乙", media_type="movie", year=2024, identity="title:pend-b:2024:movie")
    _add_hint(store, "title:pend-a:2024:movie", decision="proposed_exact", candidates=[{"imdb_id": "tt1", "imdb_type": "movie"}])
    _add_hint(store, "title:pend-b:2024:movie", decision="proposed_review", candidates=[{"imdb_id": "tt2", "imdb_type": "movie"}])

    stats = store.hint_stats()

    assert stats["hints_total"] == 2
    assert stats["hints_by_decision"] == {"proposed_exact": 1, "proposed_review": 1}
    assert stats["hints_pending"] == 2  # both media rows are still unmatched


def test_hint_stats_tolerates_missing_table(tmp_path):
    db_path = tmp_path / "no-hints.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    conn = library.connect()
    try:
        conn.execute("DROP TABLE tmdb_hints")
        conn.commit()
    finally:
        conn.close()
    assert library.hint_stats() == {
        "hints_total": 0, "hints_by_decision": {}, "hints_pending": 0,
        "confirmed_total": 0, "confirmed_pending": 0, "confirmed_conflicts": 0,
        "identity_confirmed": 0, "metadata_complete": 0, "partial": 0,
        "manual_review": 0, "no_source": 0,
    }


def test_hint_stats_confirmed_counts(store):
    _add_media(
        store, title_zh="已确认甲", media_type="movie", year=2020, identity="title:conf-a:2020:movie",
        match_status="exact", tmdb_id=200,
    )
    _add_hint(
        store, "title:conf-a:2020:movie", decision="confirmed", source="codex_manual_confirmation",
        candidates=[{"tmdb_id": 200, "tmdb_type": "movie"}],
    )
    _add_media(
        store, title_zh="已确认乙", media_type="movie", year=2021, identity="title:conf-b:2021:movie",
        match_status="exact", tmdb_id=201,
    )
    _add_hint(
        store, "title:conf-b:2021:movie", decision="confirmed", source="codex_manual_confirmation",
        candidates=[{"tmdb_id": 999, "tmdb_type": "movie"}],
    )
    _add_media(
        store, title_zh="已确认丙", media_type="movie", year=2022, identity="title:conf-c:2022:movie",
        match_status="candidate",
    )
    _add_hint(
        store, "title:conf-c:2022:movie", decision="confirmed", source="codex_manual_confirmation",
        candidates=[{"tmdb_id": 300, "tmdb_type": "movie"}],
    )

    stats = store.hint_stats()

    assert stats["confirmed_total"] == 3
    assert stats["identity_confirmed"] == 3
    assert stats["confirmed_pending"] == 1  # conf-c: media not exact yet
    assert stats["confirmed_conflicts"] == 1  # conf-b: exact media.tmdb_id (201) != confirmed tmdb_id (999)


def test_hint_stats_confirmed_conflict_same_tmdb_id_different_type(store):
    # Same numeric tmdb_id as the media's own exact match, but the movie/tv
    # namespace half differs -- TMDB ids are only unique within a type, so
    # this must still count as a conflict, not a match.
    _add_media(
        store, title_zh="同id不同类型", media_type="movie", year=2020, identity="title:conf-namespace:2020:movie",
        match_status="exact", tmdb_id=709631,
    )
    _add_hint(
        store, "title:conf-namespace:2020:movie", decision="confirmed", source="codex_manual_confirmation",
        candidates=[{"tmdb_id": 709631, "tmdb_type": "tv"}],
    )

    stats = store.hint_stats()

    assert stats["confirmed_conflicts"] == 1


def test_hint_stats_malformed_candidates_json_does_not_crash(store):
    _add_media(
        store, title_zh="损坏的候选json", media_type="movie", year=2023, identity="title:bad-json:2023:movie",
        match_status="exact", tmdb_id=400,
    )
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO tmdb_hints (media_identity, source, decision, candidate_count, candidates_json, generated_at) "
            "VALUES (?, 'codex_manual_confirmation', 'confirmed', 1, ?, 0)",
            ("title:bad-json:2023:movie", "{not valid json"),
        )
        conn.commit()
    finally:
        conn.close()

    stats = store.hint_stats()  # must not raise

    assert stats["confirmed_total"] == 1
    assert stats["confirmed_conflicts"] == 1


def test_hint_stats_five_way_metadata_breakdown(store):
    _add_media(
        store, title_zh="完整", media_type="movie", year=2020, identity="title:complete:2020:movie",
        match_status="exact", tmdb_id=1, poster_path="/p1.jpg", overview="介绍",
    )
    _add_media(
        store, title_zh="缺海报", media_type="movie", year=2020, identity="title:partial:2020:movie",
        match_status="exact", tmdb_id=2, poster_path=None, overview="介绍",
    )
    _add_media(
        store, title_zh="待复核", media_type="movie", year=2020, identity="title:review:2020:movie",
        match_status="needs_review",
    )
    _add_media(
        store, title_zh="候选中", media_type="movie", year=2020, identity="title:candidate:2020:movie",
        match_status="candidate",
    )
    _add_media(
        store, title_zh="无线索", media_type="movie", year=2020, identity="title:nohint:2020:movie",
        match_status="unmatched",
    )
    _add_media(
        store, title_zh="无候选", media_type="movie", year=2020, identity="title:noimdb:2020:movie",
        match_status="unmatched",
    )
    _add_hint(store, "title:noimdb:2020:movie", decision="no_imdb_candidate", candidates=[])
    _add_media(
        store, title_zh="有候选未确认", media_type="movie", year=2020, identity="title:pending:2020:movie",
        match_status="unmatched",
    )
    _add_hint(store, "title:pending:2020:movie", decision="proposed_exact", candidates=[{"imdb_id": "tt1", "imdb_type": "movie"}])

    stats = store.hint_stats()

    assert stats["metadata_complete"] == 1
    assert stats["partial"] == 1
    assert stats["manual_review"] == 2
    assert stats["no_source"] == 2


# --- w5-confirm-enrich: decision == "confirmed" pathway ----------------------
#
# The hint row shape written by the sibling w5-confirm-import task:
# decision == "confirmed", source == "codex_manual_confirmation",
# candidates_json == [{"imdb_id", "tmdb_id": int, "tmdb_type": "movie"|"tv",
# "confidence", "original_decision", "prior_hint_decision", "evidence": {}}].
# See docs/metadata-enrichment.md §5.2-§5.5, §6.


def _confirmed_candidate(**overrides) -> dict:
    candidate = {
        "imdb_id": "tt9000001", "tmdb_id": 4242, "tmdb_type": "movie",
        "confidence": 0.97, "original_decision": "proposed_exact",
        "prior_hint_decision": "proposed_exact", "evidence": {},
    }
    candidate.update(overrides)
    return candidate


def _detail_payload(**overrides) -> dict:
    payload = {
        "id": 4242, "title": "奇镜", "original_title": "Odd Mirror",
        "release_date": "2024-01-01", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
        "overview": "简介文字", "genres": [{"id": 18, "name": "剧情"}],
        "vote_average": 8.1, "vote_count": 300,
    }
    payload.update(overrides)
    return payload


def test_confirmed_hint_success_writes_exact_with_one_details_request(store):
    media_id = _add_media(store, title_zh="奇镜", media_type="unknown", identity="title:mirror:0:unknown")
    _add_hint(
        store, "title:mirror:0:unknown", decision="confirmed", source="codex_manual_confirmation",
        candidates=[_confirmed_candidate()],
    )
    session = (
        FakeSession()
        .route(_details_url("movie", 4242), FakeResponse(200, _detail_payload()))
        .route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": [{"id": 18, "name": "剧情"}]}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1, with_details=True)

    assert stats.matched_exact == 1
    assert stats.confirmed_exact == 1
    assert stats.confirmed_details_requests == 1
    assert stats.requests_made == 1  # one HTTP request end-to-end (judge + _write_exact reuses the cache)
    assert len(session.calls_to(_details_url("movie", 4242))) == 1

    row = _get_media(store, media_id)
    assert row["match_status"] == "exact"
    assert row["tmdb_id"] == 4242
    assert row["media_type"] == "movie"
    assert row["title_original"] == "Odd Mirror"
    assert row["overview"] == "简介文字"
    assert row["poster_path"] == "/p.jpg"
    assert row["backdrop_path"] == "/b.jpg"
    assert json.loads(row["genres_json"]) == ["剧情"]
    explanation = json.loads(row["match_candidates_json"])
    assert explanation == {
        "source": "codex_confirmation", "imdb_id": "tt9000001",
        "confidence": 0.97, "original_decision": "proposed_exact",
    }


def test_confirmed_hint_transient_failure_leaves_row_untouched_for_retry(store):
    media_id = _add_media(store, title_zh="失联剧集", media_type="movie", identity="title:lost:0:movie")
    _add_hint(
        store, "title:lost:0:movie", decision="confirmed", source="codex_manual_confirmation",
        candidates=[_confirmed_candidate(tmdb_id=5150)],
    )
    session = FakeSession().route(_details_url("movie", 5150), [FakeResponse(503) for _ in range(4)])
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.confirmed_failed == 1
    assert stats.matched_exact == 0
    assert media_id in stats.search_failed_media_ids

    row = _get_media(store, media_id)
    assert row["match_status"] == "unmatched"
    assert row["tmdb_id"] is None


def test_confirmed_hint_not_found_falls_through_to_search(store):
    media_id = _add_media(store, title_zh="边界之光", media_type="movie", year=2022, identity="title:border:2022:movie")
    _add_hint(
        store, "title:border:2022:movie", decision="confirmed", source="codex_manual_confirmation",
        candidates=[_confirmed_candidate(tmdb_id=5001, imdb_id="tt9000002")],
    )
    session = (
        FakeSession()
        .route(_details_url("movie", 5001), FakeResponse(404))
        .route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": [{
            "id": 9999, "title": "边界之光", "original_title": "Border Light",
            "release_date": "2022-05-01", "poster_path": "/p2.jpg", "backdrop_path": "/b2.jpg",
            "overview": "ov", "genre_ids": [], "vote_average": 7.0, "vote_count": 50,
        }]}))
        .route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.confirmed_not_found == 1
    assert stats.matched_exact == 1

    row = _get_media(store, media_id)
    assert row["match_status"] == "exact"
    assert row["tmdb_id"] == 9999  # NOT the stale confirmed id 5001
    explanation = json.loads(row["match_candidates_json"])
    assert explanation["source"] == "codex_confirmation_not_found"
    assert explanation["confirmed_id_not_found"] == 5001
    assert explanation["confirmed_imdb_id"] == "tt9000002"
    # Review follow-up: the search-derived match must not inherit the
    # confirmation's IMDb id (or its rating) -- that id belongs to 5001.
    assert "imdb_id" not in explanation
    assert row["imdb_id"] is None
    ratings = json.loads(row["ratings_json"] or "{}")
    assert "imdb" not in ratings


def test_confirmed_hint_type_conflict_is_needs_review_without_any_request(store):
    media_id = _add_media(store, title_zh="类型冲突剧", media_type="tv", identity="title:conflict:0:tv")
    _add_hint(
        store, "title:conflict:0:tv", decision="confirmed", source="codex_manual_confirmation",
        candidates=[_confirmed_candidate(tmdb_id=321, tmdb_type="movie")],
    )
    session = FakeSession()  # no routes at all -- any request raises AssertionError
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.confirmed_conflicts == 1
    assert stats.matched_needs_review == 1
    assert session.calls == []

    row = _get_media(store, media_id)
    assert row["match_status"] == "needs_review"
    assert row["tmdb_id"] is None
    explanation = json.loads(row["match_candidates_json"])
    assert explanation["source"] == "codex_confirmation"
    assert explanation["conflict"] == "type"
    assert explanation["candidates"][0]["tmdb_id"] == 321
    assert explanation["candidates"][0]["media_type"] == "movie"


def test_confirmed_hint_resolves_via_requeue_pathway(store):
    media_id = _add_media(store, title_zh="待复核剧", media_type="unknown", identity="title:pending:0:unknown")
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET match_status='needs_review' WHERE id=?", (media_id,))
        conn.commit()
    finally:
        conn.close()
    _add_hint(
        store, "title:pending:0:unknown", decision="confirmed", source="codex_manual_confirmation",
        candidates=[_confirmed_candidate(tmdb_id=7777, tmdb_type="tv", imdb_id="tt9000003")],
    )
    session = (
        FakeSession()
        .route(_details_url("tv", 7777), FakeResponse(200, {
            "id": 7777, "name": "待复核剧", "original_name": "Pending Show",
            "first_air_date": "2023-01-01", "poster_path": "/pp.jpg", "backdrop_path": "/bb.jpg",
            "overview": "o", "genres": [], "vote_average": 6.5, "vote_count": 10,
        }))
        .route(GENRE_TV_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.requeue_review_batch(store, client, limit=10, today=lambda: "2026-09-06")

    assert stats.confirmed_exact == 1
    assert stats.matched_exact == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "exact"
    assert row["tmdb_id"] == 7777
    assert row["media_type"] == "tv"


def test_confirmed_hint_budget_exhausted_propagates(store):
    media_id = _add_media(store, title_zh="预算耗尽剧", media_type="unknown", identity="title:nobudget:0:unknown")
    _add_hint(
        store, "title:nobudget:0:unknown", decision="confirmed", source="codex_manual_confirmation",
        candidates=[_confirmed_candidate(tmdb_id=8888)],
    )
    session = FakeSession()  # never reached -- the budget check raises first
    client, _clock = _client(store, session, limits=tmdb.Limits(max_concurrency=1, min_interval_ms=0, daily_budget=0))

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.error_class == "BudgetExhausted"
    row = _get_media(store, media_id)
    assert row["match_status"] == "unmatched"
    assert row["tmdb_id"] is None


def test_confirmed_hints_merge_by_tmdb_keeps_links(store):
    # §6 exception: 蛛网男孩/蜘蛛网 both confirmed to the same movie
    # (709631) -- merge_by_tmdb (unchanged) must fold them into one
    # identity keeping every resource_group/resource_link.
    boy_id = _add_media(store, title_zh="蛛网男孩", media_type="movie", identity="title:boy:0:movie")
    net_id = _add_media(store, title_zh="蜘蛛网", media_type="movie", identity="title:net:0:movie")
    boy_group = store.upsert_group(ls.GroupRecord(media_id=boy_id, edition_fingerprint="fp1", display_title="蛛网男孩-fp1"))
    net_group = store.upsert_group(ls.GroupRecord(media_id=net_id, edition_fingerprint="fp2", display_title="蜘蛛网-fp2"))
    _add_link(store, boy_group, url="https://115.com/s/swfakeboy001")
    _add_link(store, net_group, url="https://115.com/s/swfakenet001")

    _add_hint(
        store, "title:boy:0:movie", decision="confirmed", source="codex_manual_confirmation",
        candidates=[_confirmed_candidate(tmdb_id=709631, imdb_id="tt9100018")],
    )
    _add_hint(
        store, "title:net:0:movie", decision="confirmed", source="codex_manual_confirmation",
        candidates=[_confirmed_candidate(tmdb_id=709631, imdb_id="tt9100018")],
    )
    session = (
        FakeSession()
        .route(_details_url("movie", 709631), FakeResponse(200, _detail_payload(id=709631, title="蛛网男孩")))
        .route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": []}))
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)
    assert stats.confirmed_exact == 2

    merged = tmdb.merge_by_tmdb(store)
    assert merged == 1

    conn = store.connect(readonly=True)
    try:
        media_rows = conn.execute("SELECT id, media_identity, tmdb_id FROM media").fetchall()
        links = conn.execute("SELECT id FROM resource_link").fetchall()
    finally:
        conn.close()
    assert len(media_rows) == 1
    assert media_rows[0]["media_identity"] == "tmdb:movie:709631"
    assert media_rows[0]["tmdb_id"] == 709631
    assert len(links) == 2  # both links survive the merge
