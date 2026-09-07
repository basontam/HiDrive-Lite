"""Unit tests for the T3.4-T3.5 enrichment flow: library_tmdb.enrich_batch,
merge_by_tmdb, BackgroundEnricher, and the offline script
scripts/enrich_media_tmdb.py.

Everything here goes through a fake TMDB session (see FakeSession below);
the shared ``tests/conftest.py`` network guard additionally blocks any
accidental real HTTP call.  BackgroundEnricher tests use *real* background
threads with a fast, injectable ``sleep`` so lock/round/stop timing is
exercised for real without making the suite slow.  Test data is entirely
fictional: hostnames use real domain shapes but share codes are
``swfake...`` and access codes are fixed 4-character placeholders.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import sqlite3
import sys
import threading
import time as real_time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import library_store as ls  # noqa: E402
import library_tmdb as tmdb  # noqa: E402
import enrich_media_tmdb as enrich_script  # noqa: E402


# --- shared fake HTTP plumbing (mirrors tests/test_library_tmdb.py) ------------


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
        self.hold_seconds = 0.0

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
        if self.hold_seconds:
            real_time.sleep(self.hold_seconds)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


SEARCH_TV_URL = tmdb.TMDB_BASE + "/search/tv"
SEARCH_MOVIE_URL = tmdb.TMDB_BASE + "/search/movie"
SEARCH_MULTI_URL = tmdb.TMDB_BASE + "/search/multi"
GENRE_TV_URL = tmdb.TMDB_BASE + "/genre/tv/list"
GENRE_MOVIE_URL = tmdb.TMDB_BASE + "/genre/movie/list"


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


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = real_time.time() + timeout
    while real_time.time() < deadline:
        if predicate():
            return
        real_time.sleep(0.005)
    raise AssertionError("condition not met within timeout")


def _fast_sleep(seconds: float) -> None:
    real_time.sleep(min(seconds, 0.01))


# --- store fixture and record helpers ------------------------------------------


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


def _add_media(store, *, title_zh="标题", media_type="tv", aliases=(), year=None, has_115=0, link_count=0, identity=None, match_status="unmatched"):
    identity = identity or f"title:{title_zh}:{year or 0}:{media_type}"
    rec = ls.MediaRecord(
        media_identity=identity,
        media_type=media_type,
        title_zh=title_zh,
        search_key=title_zh,
        title_alt_json=json.dumps(list(aliases), ensure_ascii=False),
        year=year,
        match_status=match_status,
    )
    media_id = store.upsert_media(rec)
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET has_115=?, link_count=? WHERE id=?", (has_115, link_count, media_id))
        conn.commit()
    finally:
        conn.close()
    return media_id


def _get_media(store, media_id) -> dict:
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    finally:
        conn.close()
    return dict(row)


def _mark_exact(store, media_id, *, tmdb_id, media_type):
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE media SET tmdb_id=?, media_type=?, match_status='exact' WHERE id=?",
            (tmdb_id, media_type, media_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_review_scored(store, media_id, *, score=0.5, candidates_json="[]"):
    """Give an already-``needs_review`` media a score/candidates -- the §3.1
    "3 already-scored" case, which the T14 requeue must never re-select."""
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE media SET match_status='needs_review', match_score=?, match_candidates_json=? WHERE id=?",
            (score, candidates_json, media_id),
        )
        conn.commit()
    finally:
        conn.close()


def _add_link(store, group_id, *, url, provider="115"):
    rec = ls.LinkRecord(
        public_id=f"pub-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}",
        group_id=group_id,
        provider=provider,
        canonical_url_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        url_label=f"{provider} 分享",
    )
    link_id, _created = store.upsert_link(rec)
    return link_id


# --- enrich_batch: candidate queue ----------------------------------------------


def test_candidate_order_by_has115_then_link_count_then_id(store):
    _add_media(store, title_zh="低优先级", identity="title:low:0:tv", has_115=0, link_count=5)
    _add_media(store, title_zh="中优先级", identity="title:mid:0:tv", has_115=1, link_count=1)
    _add_media(store, title_zh="高优先级", identity="title:high:0:tv", has_115=1, link_count=9)
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 3)
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=3)

    assert stats.candidates_considered == 3
    # T15 fix wave 1: every genuinely undecidable (here: empty) zh-CN search
    # now gets one en-US retry too -- filter to the primary-language calls
    # to check candidate ORDER without hardcoding the retry count.
    queries = [call["params"]["query"] for call in session.calls if call["params"]["language"] == "zh-CN"]
    assert queries == ["高优先级", "中优先级", "低优先级"]


def test_limit_caps_candidates_per_batch(store):
    for i in range(5):
        _add_media(store, title_zh=f"批量剧{i}", identity=f"title:batch{i}:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 5)
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=2)

    assert stats.candidates_considered == 2
    assert len(session.calls) == 4  # 2 candidates x (zh-CN + the en-US retry)


def test_same_cache_key_is_requested_once(store):
    _add_media(store, title_zh="共享剧集", year=2020, has_115=1, link_count=2, identity="title:shared:0:tv:a")
    _add_media(store, title_zh="共享剧集", year=2020, has_115=1, link_count=1, identity="title:shared:0:tv:b")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.candidates_considered == 2
    # The first row makes both its zh-CN and en-US requests for real; the
    # second row (identical title/year -> identical cache keys for both
    # languages) gets both as cache hits.
    assert len(session.calls) == 2
    assert stats.requests_made == 2
    assert stats.cache_hits == 2


def test_second_run_reuses_cache_for_still_unmatched_media(store):
    _add_media(store, title_zh="查无此剧")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    tmdb.enrich_batch(store, client, limit=10)
    tmdb.enrich_batch(store, client, limit=10)

    assert len(session.calls) == 2  # zh-CN + en-US, both on the first round only


def test_second_run_skips_cache_resolved_media_and_processes_lower_ranked_fresh_one(store):
    # A zero-result media is ranked first (has_115=1) but its search comes
    # back empty every time -> it stays match_status='unmatched' forever
    # while its primary TMDB cache entry is 'empty'.  A lower-ranked media
    # that has never been searched must still get its turn.
    resolved = _add_media(store, title_zh="已解析剧", identity="title:resolved:0:tv", has_115=1, link_count=5)
    fresh = _add_media(store, title_zh="待处理剧", identity="title:fresh:0:tv", has_115=0, link_count=0)
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 2)
    client, _clock = _client(store, session)

    first = tmdb.enrich_batch(store, client, limit=1)
    assert first.candidates_considered == 1
    assert _get_media(store, resolved)["match_status"] == "unmatched"

    second = tmdb.enrich_batch(store, client, limit=1)

    assert second.candidates_considered == 1
    queries = [c["params"]["query"] for c in session.calls if c["params"]["language"] == "zh-CN"]
    assert queries == ["已解析剧", "待处理剧"]
    assert _get_media(store, fresh)["match_status"] == "unmatched"


def test_queue_exhausted_true_when_only_cache_resolved_media_remain(store):
    _add_media(store, title_zh="全部已解析", identity="title:onlyresolved:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    first = tmdb.enrich_batch(store, client, limit=10)
    assert first.queue_exhausted is True  # table itself was fully scanned

    second = tmdb.enrich_batch(store, client, limit=10)

    assert second.candidates_considered == 0
    assert second.queue_exhausted is True
    assert len(session.calls) == 2  # zh-CN + en-US, both on the first round only


# --- enrich_batch: deadline (T10 fix wave 1) ------------------------------------


def test_deadline_stops_batch_early_and_records_error_class(store):
    # Four candidates, each a single ~0.3s (real wall-clock) request via
    # hold_seconds -- a short deadline must stop the batch after the first
    # one or two candidates instead of running through the full limit, the
    # same way the synchronous tmdb-enrich-now HTTP route needs it to
    # (T10 fix wave 1) to stay well under gunicorn's 30s worker timeout.
    for i in range(4):
        _add_media(store, title_zh=f"慢速剧{i}", identity=f"title:slow{i}:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 4)
    session.hold_seconds = 0.3
    client, _clock = _client(store, session)

    deadline = real_time.monotonic() + 0.35
    stats = tmdb.enrich_batch(store, client, limit=4, deadline=deadline)

    assert stats.error_class == "DeadlineExceeded"
    assert stats.candidates_considered < 4
    assert stats.matched_unmatched == stats.candidates_considered
    # Each processed candidate now makes 2 requests (zh-CN + the T15 fix
    # wave 1 en-US retry, since every result here is empty).
    assert len(session.calls) == 2 * stats.candidates_considered


def test_no_deadline_runs_the_full_batch(store):
    # deadline=None (the default -- what the background thread still uses)
    # must never stop the batch early.
    for i in range(3):
        _add_media(store, title_zh=f"常规剧{i}", identity=f"title:normal{i}:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 3)
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=3)

    assert stats.error_class is None
    assert stats.candidates_considered == 3
    assert stats.matched_unmatched == 3


# --- enrich_batch: search kind selection ----------------------------------------


def test_tv_media_searches_tv_endpoint_with_year(store):
    _add_media(store, title_zh="电视剧A", year=2019, media_type="tv")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    tmdb.enrich_batch(store, client, limit=10)

    assert len(session.calls) == 2  # zh-CN + the en-US retry (still empty)
    assert session.calls[0]["url"] == SEARCH_TV_URL
    assert session.calls[0]["params"]["first_air_date_year"] == 2019


def test_unknown_media_searches_multi_endpoint(store):
    _add_media(store, title_zh="未知类型", media_type="unknown")
    session = FakeSession().route(SEARCH_MULTI_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    tmdb.enrich_batch(store, client, limit=10)

    assert len(session.calls) == 2  # zh-CN + the en-US retry (still empty)
    assert session.calls[0]["url"] == SEARCH_MULTI_URL


def test_zero_results_retries_with_latin_alias(store):
    _add_media(store, title_zh="外语剧集", aliases=["Foreign Drama", "别名二"])
    session = FakeSession()
    session.route(
        SEARCH_TV_URL,
        [
            FakeResponse(200, {"results": []}),
            FakeResponse(200, {"results": [{"id": 42, "name": "Foreign Drama"}]}),
        ],
    )
    session.route(GENRE_TV_URL, FakeResponse(200, {"genres": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    search_calls = [c for c in session.calls if c["url"] == SEARCH_TV_URL]
    assert len(search_calls) == 2
    assert search_calls[1]["params"]["query"] == "Foreign Drama"
    assert stats.matched_exact == 1


def test_zero_results_and_no_latin_alias_does_not_retry(store):
    # "does not retry" refers to the alias-retry mechanism specifically
    # (no Latin alias to try) -- the separate T15 fix wave 1 en-US retry
    # still fires once (using title_zh, since there's no alias to reuse),
    # since the result is still undecidable (unmatched).
    _add_media(store, title_zh="无别名剧集", aliases=["纯中文别名"])
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert len(session.calls) == 2
    assert session.calls[1]["params"]["language"] == "en-US"
    assert session.calls[1]["params"]["query"] == "无别名剧集"
    assert stats.matched_unmatched == 1


# --- enrich_batch: en-US retry (T15 fix wave 1, item 3c) ------------------------


def test_undecidable_zh_result_is_confirmed_exact_by_en_us_retry(store):
    # The zh-CN result's localized name has nothing in common with title_zh
    # (needs_review, score ~0.15); the en-US result for the SAME tmdb id
    # carries the original title, an exact match -- merging (deduped by
    # tmdb id, the en-US copy winning) and re-judging should now read as
    # exact.
    _add_media(store, title_zh="沙丘", year=2024, media_type="movie")
    session = FakeSession()
    session.route(
        SEARCH_MOVIE_URL,
        [
            FakeResponse(200, {"results": [{"id": 77, "title": "宇宙探险记", "release_date": "2024-01-01"}]}),
            FakeResponse(200, {"results": [{"id": 77, "title": "Dune", "original_title": "沙丘", "release_date": "2024-01-01"}]}),
        ],
    )
    session.route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    search_calls = [c for c in session.calls if c["url"] == SEARCH_MOVIE_URL]
    assert len(search_calls) == 2
    assert search_calls[0]["params"]["language"] == "zh-CN"
    assert search_calls[1]["params"]["language"] == "en-US"
    assert stats.matched_exact == 1


def test_decisive_zh_result_never_triggers_en_us_retry(store):
    _add_media(store, title_zh="精确电影", year=2021, media_type="movie")
    session = FakeSession()
    session.route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": [{"id": 88, "title": "精确电影", "release_date": "2021-01-01"}]}))
    session.route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    movie_calls = [c for c in session.calls if c["url"] == SEARCH_MOVIE_URL]
    assert len(movie_calls) == 1  # no en-US retry for a decisive result
    assert stats.matched_exact == 1


def test_en_us_retry_failure_keeps_the_zh_only_judgement(store):
    # A failed (retryable/permanent) en-US bonus request must never abandon
    # a row that already has a genuine (if undecided) zh-CN judgement --
    # unlike a failed PRIMARY search, which does abandon the row.
    _add_media(store, title_zh="重试失败剧", media_type="movie")
    session = FakeSession()
    session.route(SEARCH_MOVIE_URL, [
        FakeResponse(200, {"results": []}),
        FakeResponse(404, {}),
    ])
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    movie_calls = [c for c in session.calls if c["url"] == SEARCH_MOVIE_URL]
    assert len(movie_calls) == 2
    assert stats.matched_unmatched == 1  # zh-CN's genuine (empty) judgement is kept, row not abandoned
    assert stats.search_failed_media_ids == []


# --- enrich_batch: with_details --------------------------------------------------


def test_exact_match_writes_metadata_and_genres_without_details(store):
    media_id = _add_media(store, title_zh="精确匹配剧", year=2020)
    session = FakeSession()
    session.route(
        SEARCH_TV_URL,
        FakeResponse(
            200,
            {
                "results": [
                    {
                        "id": 900,
                        "name": "精确匹配剧",
                        "first_air_date": "2020-05-01",
                        "poster_path": "/p.jpg",
                        "backdrop_path": "/b.jpg",
                        "overview": "简介",
                        "genre_ids": [18],
                    }
                ]
            },
        ),
    )
    session.route(GENRE_TV_URL, FakeResponse(200, {"genres": [{"id": 18, "name": "剧情"}]}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10, with_details=False)

    assert stats.matched_exact == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "exact"
    assert row["tmdb_id"] == 900
    assert row["media_type"] == "tv"
    assert row["overview"] == "简介"
    assert row["poster_path"] == "/p.jpg"
    assert row["backdrop_path"] == "/b.jpg"
    assert json.loads(row["genres_json"]) == ["剧情"]
    details_url = tmdb.TMDB_BASE + "/tv/900"
    assert not any(c["url"] == details_url for c in session.calls)


def test_with_details_true_requests_details_and_overrides_overview(store):
    _add_media(store, title_zh="精确匹配剧二", year=2020, identity="title:x2:0:tv")
    details_url = tmdb.TMDB_BASE + "/tv/901"
    session = FakeSession()
    session.route(
        SEARCH_TV_URL,
        FakeResponse(200, {"results": [{"id": 901, "name": "精确匹配剧二", "first_air_date": "2020-05-01"}]}),
    )
    session.route(GENRE_TV_URL, FakeResponse(200, {"genres": []}))
    session.route(
        details_url,
        FakeResponse(200, {"overview": "详情简介", "poster_path": "/detail.jpg", "genres": [{"id": 18, "name": "剧情"}]}),
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10, with_details=True)

    assert stats.matched_exact == 1
    assert any(c["url"] == details_url for c in session.calls)


# --- enrich_batch: exact overwrites, candidate never does ------------------------


def test_candidate_match_does_not_overwrite_title_or_metadata(store):
    media_id = _add_media(store, title_zh="候选剧集", identity="title:候选剧集:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": [{"id": 500, "name": "候选剧集完整版"}]}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.matched_candidate == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "candidate"
    assert row["tmdb_id"] == 500
    assert row["title_zh"] == "候选剧集"
    assert row["title_original"] is None
    assert row["overview"] is None
    assert row["media_identity"] == "title:候选剧集:0:tv"
    candidates = json.loads(row["match_candidates_json"])
    assert candidates[0]["tmdb_id"] == 500


def test_needs_review_saves_top_candidates_without_tmdb_id(store):
    media_id = _add_media(store, title_zh="模糊匹配", identity="title:模糊匹配:0:tv")
    # A weak result -> low similarity, below the 0.75 candidate floor.
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": [{"id": 600, "name": "完全不同的名字"}]}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.matched_needs_review == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "needs_review"
    assert row["tmdb_id"] is None
    assert json.loads(row["match_candidates_json"])


# --- T14/§5.1: review-pending-unqueried requeue phase (enrich_batch) -------------


def _audit_rows(store):
    conn = store.connect(readonly=True)
    try:
        tmdb.ensure_tables(conn)
        rows = conn.execute(
            "SELECT media_id, prev_status, prev_review_reason, batch_id, queued_at, processed_at, last_error_class "
            "FROM tmdb_requeue_audit"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_review_pending_processed_after_unmatched_queue_is_exhausted(store):
    unmatched_id = _add_media(store, title_zh="缺省剧", identity="title:default:0:tv")
    pending_id = _add_media(store, title_zh="复核剧", identity="title:pending:0:tv", match_status="needs_review")
    scored_id = _add_media(store, title_zh="已评分剧", identity="title:scored:0:tv", match_status="needs_review")
    _mark_review_scored(store, scored_id, score=0.5, candidates_json=json.dumps([{"tmdb_id": 1}]))

    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 2)
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.candidates_considered == 2  # the 1 unmatched + the 1 review-pending, never the already-scored one
    queries = [c["params"]["query"] for c in session.calls if c["params"]["language"] == "zh-CN"]
    assert queries == ["缺省剧", "复核剧"]  # unmatched queue processed first, per §5.1

    assert _get_media(store, unmatched_id)["match_status"] == "unmatched"
    pending_row = _get_media(store, pending_id)
    assert pending_row["match_status"] == "needs_review"  # never bulk-flipped to unmatched
    assert pending_row["match_score"] == 0.0
    assert json.loads(pending_row["match_candidates_json"]) == []

    scored_row = _get_media(store, scored_id)
    assert scored_row["match_score"] == 0.5  # untouched -- already-scored review rows are never re-selected

    audit = _audit_rows(store)
    assert len(audit) == 1
    assert audit[0]["media_id"] == pending_id
    assert audit[0]["prev_status"] == "needs_review"
    assert audit[0]["prev_review_reason"] == "review_pending_unqueried"


def test_review_pending_not_touched_while_unmatched_queue_still_has_more(store):
    for i in range(3):
        _add_media(store, title_zh=f"未匹配剧{i}", identity=f"title:unmatched{i}:0:tv")
    pending_id = _add_media(store, title_zh="复核剧", identity="title:pending:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 3)
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.candidates_considered == 1
    assert stats.queue_exhausted is False
    assert _audit_rows(store) == []
    assert _get_media(store, pending_id)["match_score"] is None


def test_review_pending_outcomes_exact_candidate_and_still_needs_review(store):
    exact_id = _add_media(store, title_zh="精确复核剧", year=2020, identity="title:rpexact:0:tv", match_status="needs_review")
    candidate_id = _add_media(store, title_zh="候选复核剧", identity="title:rpcandidate:0:tv", match_status="needs_review")
    unmatched_id = _add_media(store, title_zh="无结果复核剧", identity="title:rpunmatched:0:tv", match_status="needs_review")

    session = FakeSession()
    session.route(
        SEARCH_TV_URL,
        [
            FakeResponse(200, {"results": [{"id": 950, "name": "精确复核剧", "first_air_date": "2020-05-01"}]}),
            FakeResponse(200, {"results": [{"id": 600, "name": "候选复核剧完整版"}]}),
            FakeResponse(200, {"results": []}),
        ],
    )
    session.route(GENRE_TV_URL, FakeResponse(200, {"genres": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.matched_exact == 1
    assert stats.matched_candidate == 1
    assert stats.matched_needs_review == 1

    exact_row = _get_media(store, exact_id)
    assert exact_row["match_status"] == "exact"
    assert exact_row["tmdb_id"] == 950

    candidate_row = _get_media(store, candidate_id)
    assert candidate_row["match_status"] == "candidate"
    assert candidate_row["tmdb_id"] == 600

    unmatched_row = _get_media(store, unmatched_id)
    assert unmatched_row["match_status"] == "needs_review"  # provenance kept, not flipped to unmatched
    assert unmatched_row["tmdb_id"] is None
    assert unmatched_row["match_score"] == 0.0

    audit = _audit_rows(store)
    assert {row["media_id"] for row in audit} == {exact_id, candidate_id, unmatched_id}
    batch_ids = {row["batch_id"] for row in audit}
    assert len(batch_ids) == 1  # all written in the same requeue batch


# --- T14 fix wave 2/finding #1: transient search failures must not retire --------
# --- a review-pending row as "queried, no candidate" -----------------------------


def test_review_pending_row_untouched_by_transient_search_failure(store):
    # 4x HTTP 503 exhausts every retry -> the search never produced a
    # genuine ok/empty result. The row must stay exactly as it was (score
    # still NULL) instead of being written as a 0.0/'[]' "no candidate"
    # verdict -- that would permanently retire it from the pending count.
    pending_id = _add_media(store, title_zh="超时复核剧", identity="title:timeout:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(503) for _ in range(4)])
    client, _clock = _client(store, session)

    stats = tmdb.requeue_review_batch(store, client, limit=10, today=lambda: "2026-09-06")

    row = _get_media(store, pending_id)
    assert row["match_status"] == "needs_review"
    assert row["match_score"] is None
    assert row["match_candidates_json"] is None

    audit = _audit_rows(store)
    assert len(audit) == 1
    assert audit[0]["media_id"] == pending_id
    assert audit[0]["processed_at"] is None  # never judged -- --resume must retry it
    assert audit[0]["last_error_class"] == "HTTP503"

    assert stats.matched_needs_review == 0
    assert stats.matched_unmatched == 0
    assert stats.search_failed_media_ids == [pending_id]
    assert stats.http_5xx == 1


def test_review_pending_row_failure_recorded_via_alias_retry_too(store):
    # The primary search is a genuine empty result (triggers the alias
    # retry, same as the non-failure path), but the alias retry itself
    # fails -- that must also count as "search failed", not "no results".
    pending_id = _add_media(
        store, title_zh="别名超时复核剧", aliases=["Foreign Drama"], identity="title:aliastimeout:0:tv",
        match_status="needs_review",
    )
    session = FakeSession()
    session.route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] + [FakeResponse(503) for _ in range(4)])
    client, _clock = _client(store, session)

    stats = tmdb.requeue_review_batch(store, client, limit=10, today=lambda: "2026-09-06")

    row = _get_media(store, pending_id)
    assert row["match_score"] is None
    assert stats.search_failed_media_ids == [pending_id]
    audit = _audit_rows(store)
    assert audit[0]["last_error_class"] == "HTTP503"
    assert audit[0]["processed_at"] is None


def test_review_pending_genuine_empty_result_still_writes_the_verdict(store):
    # Contrast case: a genuine 200/no-results search (not a failure) must
    # still write the 0.0/'[]' verdict exactly as before.
    pending_id = _add_media(store, title_zh="真无结果复核剧", identity="title:genuineempty:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.requeue_review_batch(store, client, limit=10, today=lambda: "2026-09-06")

    row = _get_media(store, pending_id)
    assert row["match_status"] == "needs_review"
    assert row["match_score"] == 0.0
    assert json.loads(row["match_candidates_json"]) == []
    assert stats.search_failed_media_ids == []
    audit = _audit_rows(store)
    assert audit[0]["processed_at"] is not None
    assert audit[0]["last_error_class"] is None


def test_unmatched_row_untouched_by_transient_search_failure(store):
    # The same rule already held for the primary unmatched queue (an
    # unmatched verdict was never written back to begin with), but must
    # keep holding now that the review-pending queue's write path changed:
    # a transient failure never corrupts an ordinary unmatched row either.
    unmatched_id = _add_media(store, title_zh="超时未匹配剧", identity="title:timeoutunmatched:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(503) for _ in range(4)])
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    row = _get_media(store, unmatched_id)
    assert row["match_status"] == "unmatched"
    assert row["match_score"] is None
    assert stats.matched_unmatched == 0
    assert stats.search_failed_media_ids == [unmatched_id]
    assert stats.http_5xx == 1
    assert _audit_rows(store) == []  # the primary queue never writes an audit row


# --- T14 fix wave 3: a failing review-pending row must not be reselected --------
# --- forever, across invocations, once its own cache entry says why -------------


def test_review_pending_row_with_failed_permanent_cache_is_not_reselected(store):
    # A failed_permanent cache entry never expires -- without a cache-based
    # skip (mirroring _select_fresh_candidates's _media_has_resolved_cache),
    # this row would be reselected and re-fail every round forever.
    pending_id = _add_media(store, title_zh="永久失败复核剧", identity="title:permfail:0:tv", match_status="needs_review")
    session = FakeSession()  # no routes: a reselection attempt would raise "unscripted request"
    client, _clock = _client(store, session)
    cache = tmdb.TmdbCache(lambda: sqlite3.connect(str(store.db_path)))
    cache_key = tmdb._primary_cache_key("tv", "永久失败复核剧", None)
    cache.put(tmdb.CacheEntry(cache_key, "failed_permanent", None, None, None, int(real_time.time()), None, "HTTP404"))

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.candidates_considered == 0
    assert session.calls == []
    assert _get_media(store, pending_id)["match_score"] is None
    assert _audit_rows(store) == []


def test_review_pending_row_with_cooling_failed_retryable_is_skipped_then_selectable_after_cooldown(store):
    pending_id = _add_media(store, title_zh="冷却复核剧", identity="title:cooling:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)
    cache = tmdb.TmdbCache(lambda: sqlite3.connect(str(store.db_path)))
    cache_key = tmdb._primary_cache_key("tv", "冷却复核剧", None)

    # Still cooling (retry_after in the future) -> skipped, no request made.
    cache.put(tmdb.CacheEntry(cache_key, "failed_retryable", None, None, None, int(real_time.time()), int(real_time.time()) + 1000, "HTTP503"))
    cooling = tmdb.enrich_batch(store, client, limit=10)
    assert cooling.candidates_considered == 0
    assert session.calls == []
    assert _get_media(store, pending_id)["match_score"] is None

    # Cooldown elapsed (retry_after in the past) -> selectable again, a
    # genuine re-attempt that now writes a real verdict.
    cache.put(tmdb.CacheEntry(cache_key, "failed_retryable", None, None, None, int(real_time.time()), int(real_time.time()) - 10, "HTTP503"))
    retried = tmdb.enrich_batch(store, client, limit=10)
    assert retried.candidates_considered == 1
    assert len(session.calls) == 2  # zh-CN + the en-US retry (still undecidable)
    row = _get_media(store, pending_id)
    assert row["match_status"] == "needs_review"
    assert row["match_score"] == 0.0


def test_search_failed_row_not_reselected_with_caller_supplied_exclude_ids_or_while_still_cooling(store):
    # Mirrors --library-requeue-review's internal <=100-row batch loop
    # (T14 fix wave 2): within one invocation, the caller folds
    # search_failed_media_ids into exclude_ids for its next internal batch
    # so the same failing row is not reselected. A later invocation (a
    # fresh, empty exclude_ids, e.g. a new process) used to reselect it
    # anyway and just re-fail from cache every time (the T14 fix wave 3
    # bug); now the cache-based skip (mirroring _select_fresh_candidates)
    # keeps it out of a fresh invocation too while its failed_retryable
    # entry is still cooling down.
    pending_id = _add_media(store, title_zh="重试复核剧", identity="title:retry:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(503) for _ in range(4)])
    client, _clock = _client(store, session)

    first = tmdb.requeue_review_batch(store, client, limit=10, today=lambda: "2026-09-06")
    assert first.search_failed_media_ids == [pending_id]
    assert len(session.calls) == 4

    second = tmdb.requeue_review_batch(
        store, client, limit=10, exclude_ids=frozenset(first.search_failed_media_ids), today=lambda: "2026-09-06",
    )
    assert second.candidates_considered == 0
    assert len(session.calls) == 4  # no new search attempt while excluded

    # A fresh invocation with no exclude_ids at all: still skipped, this
    # time by the cache-based check (RETRY_AFTER_COOLDOWN is 3600s, so the
    # entry written by `first` is still well within its cooldown here).
    third = tmdb.requeue_review_batch(store, client, limit=10, today=lambda: "2026-09-06")
    assert third.candidates_considered == 0
    assert third.search_failed_media_ids == []
    assert len(session.calls) == 4  # no new attempt, and no cache hit either


# --- T14 fix wave 2/finding #2: http_429 counted once around the whole batch -----


def test_http_429_counts_429s_from_the_genre_endpoint_too(store):
    _add_media(store, title_zh="精确匹配剧限流", year=2020, identity="title:x429:0:tv")
    session = FakeSession()
    session.route(
        SEARCH_TV_URL,
        FakeResponse(200, {"results": [{"id": 902, "name": "精确匹配剧限流", "first_air_date": "2020-05-01"}]}),
    )
    session.route(
        GENRE_TV_URL,
        [
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(200, {"genres": []}),
        ],
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.matched_exact == 1
    assert stats.http_429 == 2


# --- T14/§5.1: requeue_review_batch (the --library-requeue-review CLI) ----------


def test_requeue_review_batch_only_ever_touches_the_review_pending_queue(store):
    _add_media(store, title_zh="未匹配剧", identity="title:unmatched:0:tv")
    pending_id = _add_media(store, title_zh="复核剧", identity="title:pending:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.requeue_review_batch(store, client, limit=10)

    assert stats.candidates_considered == 1
    queries = [c["params"]["query"] for c in session.calls if c["params"]["language"] == "zh-CN"]
    assert queries == ["复核剧"]
    assert _get_media(store, pending_id)["match_score"] == 0.0


def test_requeue_review_batch_exclude_ids_skips_already_audited_media(store):
    skip_id = _add_media(store, title_zh="已排除剧", identity="title:skip:0:tv", match_status="needs_review")
    keep_id = _add_media(store, title_zh="待处理剧", identity="title:keep:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    stats = tmdb.requeue_review_batch(store, client, limit=10, exclude_ids=frozenset({skip_id}))

    assert stats.candidates_considered == 1
    queries = [c["params"]["query"] for c in session.calls if c["params"]["language"] == "zh-CN"]
    assert queries == ["待处理剧"]
    assert _get_media(store, skip_id)["match_score"] is None  # untouched, no audit row either
    assert [row["media_id"] for row in _audit_rows(store)] == [keep_id]


def test_requeue_review_batch_writes_audit_with_the_given_batch_id(store):
    _add_media(store, title_zh="批次剧", identity="title:batch:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session)

    tmdb.requeue_review_batch(store, client, limit=10, today=lambda: "2026-09-06")

    audit = _audit_rows(store)
    assert len(audit) == 1
    assert audit[0]["batch_id"] == "2026-09-06"
    assert audit[0]["processed_at"] is not None  # judged to completion


# --- T14 fix wave 1/finding #3: per-row audit write + processed_at --------------


def test_audit_row_is_written_per_row_before_judging_and_marked_processed_after(store):
    row1_id = _add_media(store, title_zh="逐行剧一", identity="title:row1:0:tv", match_status="needs_review")
    row2_id = _add_media(store, title_zh="逐行剧二", identity="title:row2:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 2)
    client, _clock = _client(store, session)

    tmdb.requeue_review_batch(store, client, limit=10, today=lambda: "2026-09-06")

    audit = {row["media_id"]: row for row in _audit_rows(store)}
    assert set(audit) == {row1_id, row2_id}
    assert audit[row1_id]["processed_at"] is not None
    assert audit[row2_id]["processed_at"] is not None


def test_resume_only_excludes_the_row_actually_judged_when_budget_runs_out_mid_batch(store):
    # Fix wave 1/finding #3: budget 2 (one full row now costs 2 requests --
    # zh-CN + the T15 fix wave 1 en-US retry, since both come back empty),
    # two pending rows -- the audit row for the row that never got judged
    # (BudgetExhausted before its own search) must stay processed_at=NULL,
    # so --resume (processed_media_ids_for_batch) does not skip it on the
    # next attempt.
    row1_id = _add_media(store, title_zh="预算剧一", identity="title:budget1:0:tv", match_status="needs_review")
    row2_id = _add_media(store, title_zh="预算剧二", identity="title:budget2:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session, limits=tmdb.Limits(max_concurrency=1, min_interval_ms=0, daily_budget=2))

    stats = tmdb.requeue_review_batch(store, client, limit=10, today=lambda: "2026-09-06")

    assert stats.error_class == "BudgetExhausted"
    audit = {row["media_id"]: row for row in _audit_rows(store)}
    assert set(audit) == {row1_id, row2_id}  # both queued
    processed_ids = {media_id for media_id, row in audit.items() if row["processed_at"] is not None}
    assert processed_ids == {row1_id}  # only the judged row

    conn = store.connect(readonly=True)
    try:
        assert tmdb.processed_media_ids_for_batch(conn, "2026-09-06") == {row1_id}
        assert tmdb.audited_media_ids_for_batch(conn, "2026-09-06") == {row1_id, row2_id}
    finally:
        conn.close()


# --- T14 fix wave 2/finding #3: batch_id is keyword-required ---------------------


def test_process_candidate_row_requires_batch_id_keyword(store):
    media_id = _add_media(store, title_zh="缺批次剧", identity="title:nobatch:0:tv")
    session = FakeSession()
    client, _clock = _client(store, session)
    row = _get_media(store, media_id)
    with pytest.raises(TypeError):
        tmdb._process_candidate_row(store, client, tmdb.EnrichStats(), row, with_details=False, prev_status="unmatched")


def test_run_candidate_queue_requires_batch_id_keyword(store):
    session = FakeSession()
    client, _clock = _client(store, session)
    with pytest.raises(TypeError):
        tmdb._run_candidate_queue(store, client, tmdb.EnrichStats(), [], with_details=False, deadline=None)


# --- T14/§5.2: conservative judging -- the §5.3 sample titles -------------------
#
# These exercise the SAME fixed judge()/score_candidate() rule every other
# media goes through -- nothing here special-cases a title or tmdb id.


def test_bad_sisters_sample_title_year_type_match_is_exact(store):
    media_id = _add_media(store, title_zh="不良姐妹", media_type="tv", year=2022, aliases=["Bad Sisters"], identity="title:badsisters:2022:tv")
    session = FakeSession()
    session.route(
        SEARCH_TV_URL,
        [
            FakeResponse(200, {"results": []}),  # primary zh-CN search: no hit
            FakeResponse(200, {
                "results": [{
                    "id": 199318, "media_type": "tv", "name": "Bad Sisters", "original_name": "Bad Sisters",
                    "first_air_date": "2022-08-19", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
                    "overview": "...", "genre_ids": [18],
                }]
            }),
        ],
    )
    session.route(GENRE_TV_URL, FakeResponse(200, {"genres": [{"id": 18, "name": "剧情"}]}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.matched_exact == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "exact"
    assert row["tmdb_id"] == 199318


def test_palm_royale_sample_title_year_type_match_is_exact(store):
    media_id = _add_media(store, title_zh="皇家棕榈", media_type="tv", year=2024, aliases=["Palm Royale"], identity="title:palmroyale:2024:tv")
    session = FakeSession()
    session.route(
        SEARCH_TV_URL,
        [
            FakeResponse(200, {"results": []}),
            FakeResponse(200, {
                "results": [{
                    "id": 157367, "media_type": "tv", "name": "Palm Royale", "original_name": "Palm Royale",
                    "first_air_date": "2024-03-20",
                }]
            }),
        ],
    )
    session.route(GENRE_TV_URL, FakeResponse(200, {"genres": []}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.matched_exact == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "exact"
    assert row["tmdb_id"] == 157367


def test_pigsy_sample_year_conflict_between_source_and_candidate_stays_needs_review(store):
    # Source says 2024; the Pigsy candidate's own release year is 2022 --
    # the fixed rule's year-conflict penalty must keep this needs_review,
    # never exact, regardless of how well the title matches.
    media_id = _add_media(store, title_zh="奇幻西游：新世界", media_type="movie", year=2024, aliases=["Pigsy"], identity="title:pigsy:2024:movie")
    session = FakeSession()
    session.route(
        SEARCH_MOVIE_URL,
        [
            FakeResponse(200, {"results": []}),
            FakeResponse(200, {
                "results": [{
                    "id": 1088128, "media_type": "movie", "title": "Pigsy", "original_title": "Pigsy",
                    "release_date": "2022-01-01",
                }]
            }),
        ],
    )
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=1)

    assert stats.matched_exact == 0
    assert stats.matched_needs_review == 1
    row = _get_media(store, media_id)
    assert row["match_status"] == "needs_review"
    assert row["tmdb_id"] is None
    candidates = json.loads(row["match_candidates_json"])
    assert candidates[0]["tmdb_id"] == 1088128


# --- BudgetExhausted / InvalidApiKey stop the batch ------------------------------


def test_budget_exhausted_stops_batch_and_is_recorded(store):
    _add_media(store, title_zh="预算耗尽剧1", identity="title:b1:0:tv")
    _add_media(store, title_zh="预算耗尽剧2", identity="title:b2:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 2)
    client, _clock = _client(store, session, limits=tmdb.Limits(max_concurrency=1, min_interval_ms=0, daily_budget=1))

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.error_class == "BudgetExhausted"
    assert len(session.calls) == 1


def test_invalid_api_key_stops_batch(store):
    _add_media(store, title_zh="密钥失效剧")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(401, {}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.error_class == "InvalidApiKey"


def test_invalid_api_key_request_counts_toward_requests_made(store):
    _add_media(store, title_zh="密钥失效剧二")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(401, {}))
    client, _clock = _client(store, session)

    stats = tmdb.enrich_batch(store, client, limit=10)

    assert stats.error_class == "InvalidApiKey"
    assert stats.requests_made == 1
    assert stats.cache_hits == 0


# --- merge_by_tmdb ---------------------------------------------------------------


def test_merge_by_tmdb_upgrades_identity_for_singleton_exact_match(store):
    media_id = _add_media(store, title_zh="独一无二", media_type="movie", identity="title:c:0:movie")
    _mark_exact(store, media_id, tmdb_id=800, media_type="movie")

    merged = tmdb.merge_by_tmdb(store)

    assert merged == 0
    row = _get_media(store, media_id)
    assert row["media_identity"] == "tmdb:movie:800"


def test_merge_by_tmdb_migrates_groups_and_links_and_is_idempotent(store):
    keeper = _add_media(store, title_zh="正式版名称", media_type="movie", identity="title:a:0:movie")
    dup = _add_media(store, title_zh="别名版本", media_type="movie", identity="title:b:0:movie")
    _mark_exact(store, keeper, tmdb_id=700, media_type="movie")
    _mark_exact(store, dup, tmdb_id=700, media_type="movie")

    keeper_group1 = store.upsert_group(ls.GroupRecord(media_id=keeper, edition_fingerprint="fp1", display_title="正式版-fp1"))
    dup_group_unique = store.upsert_group(ls.GroupRecord(media_id=dup, edition_fingerprint="fp2", display_title="别名版-fp2"))
    dup_group_conflict = store.upsert_group(ls.GroupRecord(media_id=dup, edition_fingerprint="fp1", display_title="别名版-fp1"))

    _add_link(store, dup_group_unique, url="https://115.com/s/swfakeaaa001")
    _add_link(store, dup_group_conflict, url="https://115.com/s/swfakeaaa002")
    _add_link(store, keeper_group1, url="https://115.com/s/swfakeaaa003")

    merged = tmdb.merge_by_tmdb(store)
    assert merged == 1

    conn = store.connect(readonly=True)
    try:
        media_rows = conn.execute("SELECT id, media_identity FROM media ORDER BY id").fetchall()
        assert [dict(r) for r in media_rows] == [{"id": keeper, "media_identity": "tmdb:movie:700"}]

        groups = conn.execute("SELECT id, media_id, edition_fingerprint FROM resource_group ORDER BY id").fetchall()
        assert {(g["media_id"], g["edition_fingerprint"]) for g in groups} == {(keeper, "fp1"), (keeper, "fp2")}

        fp1_group_id = next(g["id"] for g in groups if g["edition_fingerprint"] == "fp1")
        fp2_group_id = next(g["id"] for g in groups if g["edition_fingerprint"] == "fp2")
        links = conn.execute("SELECT group_id FROM resource_link").fetchall()
        assert len(links) == 3
        assert all(l["group_id"] in {fp1_group_id, fp2_group_id} for l in links)
    finally:
        conn.close()

    merged_again = tmdb.merge_by_tmdb(store)
    assert merged_again == 0

    conn = store.connect(readonly=True)
    try:
        media_rows_2 = conn.execute("SELECT id, media_identity FROM media ORDER BY id").fetchall()
        groups_2 = conn.execute("SELECT id, media_id, edition_fingerprint FROM resource_group ORDER BY id").fetchall()
        links_2 = conn.execute("SELECT group_id FROM resource_link").fetchall()
    finally:
        conn.close()
    assert [dict(r) for r in media_rows_2] == [{"id": keeper, "media_identity": "tmdb:movie:700"}]
    assert {(g["media_id"], g["edition_fingerprint"]) for g in groups_2} == {(keeper, "fp1"), (keeper, "fp2")}
    assert len(links_2) == 3


def test_merge_by_tmdb_no_exact_matches_is_a_noop(store):
    _add_media(store, title_zh="未匹配剧")
    assert tmdb.merge_by_tmdb(store) == 0


# --- BackgroundEnricher -----------------------------------------------------------


def _enricher_kwargs(tmp_path, *, conn_factory=None, **overrides):
    """Default leader/run lock paths (three-lock ``LockPaths``, distinct
    from any client's own budget lock) and a state ``conn_factory`` for
    ``BackgroundEnricher`` tests that don't care about the specifics."""
    lock_paths = tmdb.LockPaths.for_data_dir(tmp_path)
    kwargs = dict(
        leader_lock_path=lock_paths.leader,
        run_lock_path=lock_paths.run,
        conn_factory=conn_factory or (lambda: sqlite3.connect(str(tmp_path / "enricher-state.db"))),
    )
    kwargs.update(overrides)
    return kwargs


def test_two_instances_share_lock_file_only_one_runs(tmp_path):
    kwargs = _enricher_kwargs(tmp_path, sleep=_fast_sleep, leader_retry_seconds=0.05)
    a = tmdb.BackgroundEnricher(lambda: None, lambda: None, **kwargs)
    b = tmdb.BackgroundEnricher(lambda: None, lambda: None, **kwargs)
    try:
        a.start()
        _wait_until(lambda: a.status()["running"])
        b.start()
        # B loses the leader-lock race but keeps its thread alive, retrying
        # instead of exiting (T11) -- confirm at least one failed attempt
        # was recorded rather than sleeping a fixed amount and hoping.
        _wait_until(lambda: b.status()["leader_attempts"] >= 1)
        assert a.status()["running"] is True
        assert b.status()["running"] is False
        assert b._thread.is_alive()
    finally:
        a.stop(timeout=2)
        b.stop(timeout=2)
    assert a.status()["running"] is False
    assert b.status()["running"] is False


def test_loser_keeps_retrying_and_takes_over_when_leader_stops(tmp_path):
    # T11: gunicorn HUP reload starts a new worker's enricher thread while
    # an old worker (still holding the leader lock) is alive for the
    # graceful period -- the loser must keep retrying, not exit for good,
    # so it can take over the instant the old leader's stop() releases
    # the lock.
    kwargs = _enricher_kwargs(tmp_path, sleep=_fast_sleep)
    a = tmdb.BackgroundEnricher(lambda: None, lambda: None, **kwargs)
    b = tmdb.BackgroundEnricher(
        lambda: None, lambda: None, **{**kwargs, "leader_retry_seconds": 0.05}
    )
    try:
        a.start()
        _wait_until(lambda: a.status()["running"])
        b.start()
        _wait_until(lambda: b.status()["leader_attempts"] >= 1)
        assert b._thread.is_alive()
        assert b.status()["running"] is False

        a.stop(timeout=2)
        # Leader A already wrote a heartbeat into the shared state DB, so
        # wipe it before B takes over -- the reappearing row then proves
        # B's own post-takeover write path (review fix).
        conn = kwargs["conn_factory"]()
        try:
            conn.execute("DELETE FROM tmdb_enricher_state WHERE key = 'heartbeat_at'")
            conn.commit()
        finally:
            conn.close()
        _wait_until(lambda: b.status()["running"] is True, timeout=1.0)

        def _heartbeat():
            conn = kwargs["conn_factory"]()
            try:
                return tmdb.read_enricher_state(conn).get("heartbeat_at")
            finally:
                conn.close()

        _wait_until(lambda: _heartbeat() not in (None, ""))

        started = real_time.time()
        b.stop(timeout=2)
        elapsed = real_time.time() - started
    finally:
        a.stop(timeout=2)
        b.stop(timeout=2)
    assert elapsed < 2.0


def test_non_leader_performs_no_db_writes_while_waiting(tmp_path):
    conn_calls = []

    def recording_conn_factory():
        conn_calls.append(1)
        return sqlite3.connect(str(tmp_path / "b-state.db"))

    lock_paths = tmdb.LockPaths.for_data_dir(tmp_path)
    a = tmdb.BackgroundEnricher(
        lambda: None, lambda: None,
        leader_lock_path=lock_paths.leader, run_lock_path=lock_paths.run,
        conn_factory=lambda: sqlite3.connect(str(tmp_path / "a-state.db")),
        sleep=_fast_sleep,
    )
    b = tmdb.BackgroundEnricher(
        lambda: None, lambda: None,
        leader_lock_path=lock_paths.leader, run_lock_path=lock_paths.run,
        conn_factory=recording_conn_factory,
        sleep=_fast_sleep, leader_retry_seconds=0.01,
    )
    try:
        a.start()
        _wait_until(lambda: a.status()["running"])
        b.start()
        _wait_until(lambda: b.status()["leader_attempts"] >= 1)
        real_time.sleep(0.05)
        assert conn_calls == []
    finally:
        a.stop(timeout=2)
        b.stop(timeout=2)


def test_stop_while_waiting_for_leadership_is_prompt(tmp_path):
    lock_paths = tmdb.LockPaths.for_data_dir(tmp_path)
    lock_paths.leader.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_paths.leader, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        b = tmdb.BackgroundEnricher(
            lambda: None, lambda: None,
            leader_lock_path=lock_paths.leader, run_lock_path=lock_paths.run,
            conn_factory=lambda: sqlite3.connect(str(tmp_path / "b-state.db")),
            leader_retry_seconds=5,
        )
        b.start()
        _wait_until(lambda: b.status()["leader_attempts"] >= 1)
        started = real_time.time()
        b.stop(timeout=1)
        elapsed = real_time.time() - started
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert elapsed < 1.0


def test_lock_open_failure_is_logged_and_retried(tmp_path, monkeypatch, caplog):
    lock_paths = tmdb.LockPaths.for_data_dir(tmp_path)
    real_open = open
    calls = {"n": 0}

    def flaky_open(path, *args, **kwargs):
        if Path(path) == lock_paths.leader:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("boom")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(tmdb, "open", flaky_open, raising=False)

    enricher = tmdb.BackgroundEnricher(
        lambda: None, lambda: None,
        **_enricher_kwargs(tmp_path, sleep=_fast_sleep, leader_retry_seconds=0.01),
    )
    try:
        with caplog.at_level(logging.WARNING, logger="HiDrive-Lite.tmdb"):
            enricher.start()
            _wait_until(lambda: enricher.status()["running"] is True)
    finally:
        enricher.stop(timeout=2)
    assert calls["n"] >= 2
    assert any("OSError" in rec.getMessage() for rec in caplog.records)


def test_store_factory_none_skips_round_and_reports_idle_60(tmp_path):
    client_factory_calls = []

    def client_factory():
        client_factory_calls.append(1)
        return object()

    enricher = tmdb.BackgroundEnricher(lambda: None, client_factory, **_enricher_kwargs(tmp_path, sleep=_fast_sleep))
    try:
        enricher.start()
        _wait_until(lambda: enricher.status()["next_check_in"] is not None)
        real_time.sleep(0.05)
        status = enricher.status()
    finally:
        enricher.stop(timeout=2)
    assert status["next_check_in"] == 60
    assert client_factory_calls == []


def test_enabled_check_false_skips_round(tmp_path):
    store_factory_calls = []

    def store_factory():
        store_factory_calls.append(1)
        return None

    enricher = tmdb.BackgroundEnricher(
        store_factory, lambda: None, **_enricher_kwargs(tmp_path, sleep=_fast_sleep, enabled_check=lambda: False)
    )
    try:
        enricher.start()
        real_time.sleep(0.05)
    finally:
        enricher.stop(timeout=2)
    assert store_factory_calls == []


def test_round_size_is_bounded_by_batch_size(store, tmp_path):
    for i in range(5):
        _add_media(store, title_zh=f"批量剧{i}", identity=f"title:batch{i}:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 10)

    def client_factory():
        client, _clock = _client(store, session)
        return client

    gate = threading.Event()

    def blocking_sleep(seconds):
        gate.wait(timeout=2)
        gate.clear()

    enricher = tmdb.BackgroundEnricher(
        lambda: store, client_factory,
        **_enricher_kwargs(tmp_path, conn_factory=lambda: sqlite3.connect(str(store.db_path)), sleep=blocking_sleep, batch_size=2),
    )
    try:
        enricher.start()
        _wait_until(lambda: enricher.status()["last_round_at"] is not None)
        status = enricher.status()
        assert status["processed_today"] == 2
    finally:
        enricher.stop(timeout=0.2)
        gate.set()
        _wait_until(lambda: not enricher.status()["running"])


def test_stop_waits_for_in_flight_batch_to_finish(store, tmp_path):
    _add_media(store, title_zh="慢速匹配剧", identity="title:slow:0:tv")
    session = FakeSession()
    session.hold_seconds = 0.15
    session.route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))

    def client_factory():
        client, _clock = _client(store, session)
        return client

    enricher = tmdb.BackgroundEnricher(
        lambda: store, client_factory,
        **_enricher_kwargs(tmp_path, conn_factory=lambda: sqlite3.connect(str(store.db_path)), sleep=_fast_sleep, batch_size=5),
    )
    try:
        enricher.start()
        _wait_until(lambda: enricher.status()["running"] is True)
        started = real_time.time()
        enricher.stop(timeout=2)
        elapsed = real_time.time() - started
        assert elapsed >= 0.1
        assert enricher.status()["last_round_at"] is not None
    finally:
        enricher.stop(timeout=2)


def test_stop_is_prompt_with_real_default_sleep_and_idle_60(tmp_path):
    # store_factory returns None -> every round idles at idle_seconds=60
    # using the *real*, non-injected default sleep (no sleep= kwarg here).
    # stop(timeout=5) must not have to wait out that 60s idle wait.
    enricher = tmdb.BackgroundEnricher(lambda: None, lambda: None, **_enricher_kwargs(tmp_path, idle_seconds=60))
    try:
        enricher.start()
        _wait_until(lambda: enricher.status()["running"] is True)
        started = real_time.time()
        enricher.stop(timeout=5)
        elapsed = real_time.time() - started
    finally:
        enricher.stop(timeout=5)
    assert elapsed < 2.0
    assert enricher.status()["running"] is False


def test_budget_exhausted_backs_off_to_idle(store, tmp_path):
    _add_media(store, title_zh="预算耗尽剧", identity="title:budget:0:tv")

    class ExhaustedClient:
        http_429_responses = 0

        def budget_status(self):
            return {"day": "2026-09-04", "used": 1, "budget": 1, "remaining": 0}

        def search(self, kind, query, *, year=None, language="zh-CN"):
            raise tmdb.BudgetExhausted("no budget left")

        def genres(self, kind, language="zh-CN"):
            return {}

        def details(self, kind, tmdb_id, *, language="zh-CN"):
            raise AssertionError("should not be called")

    enricher = tmdb.BackgroundEnricher(
        lambda: store, lambda: ExhaustedClient(),
        **_enricher_kwargs(
            tmp_path, conn_factory=lambda: sqlite3.connect(str(store.db_path)),
            sleep=_fast_sleep, idle_seconds=42, round_seconds=1,
        ),
    )
    try:
        _wait = enricher
        enricher.start()
        _wait_until(lambda: enricher.status()["last_error_class"] == "BudgetExhausted")
        status = enricher.status()
    finally:
        enricher.stop(timeout=2)
    assert status["next_check_in"] == 42


def test_all_phase2_search_failures_back_off_to_idle(store, tmp_path):
    # T14 fix wave 3: a round where the whole phase-2 (review-pending)
    # selection fails its TMDB search (e.g. an outage) must not be paced
    # like a healthy round -- otherwise every round_seconds it would burn
    # through a fresh slice of the backlog, each failing the same way.
    # error_class itself stays None here (a search failure never sets it),
    # so this exercises a genuinely separate backoff path from
    # test_budget_exhausted_backs_off_to_idle above.
    _add_media(store, title_zh="全失败复核剧一", identity="title:allfail1:0:tv", match_status="needs_review")
    _add_media(store, title_zh="全失败复核剧二", identity="title:allfail2:0:tv", match_status="needs_review")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(503) for _ in range(4)])

    def client_factory():
        client, _clock = _client(store, session)
        return client

    gate = threading.Event()

    def blocking_sleep(seconds):
        gate.wait(timeout=2)
        gate.clear()

    enricher = tmdb.BackgroundEnricher(
        lambda: store, client_factory,
        **_enricher_kwargs(
            tmp_path, conn_factory=lambda: sqlite3.connect(str(store.db_path)),
            sleep=blocking_sleep, idle_seconds=42, round_seconds=1, batch_size=5,
        ),
    )
    try:
        enricher.start()
        _wait_until(lambda: enricher.status()["last_round_at"] is not None)
        status = enricher.status()
        assert status["processed_today"] == 2
        assert status["last_error_class"] is None
        assert status["next_check_in"] == 42
    finally:
        enricher.stop(timeout=0.2)
        gate.set()
        _wait_until(lambda: not enricher.status()["running"])


def test_loop_survives_unexpected_exception_and_continues(store, tmp_path):
    _add_media(store, title_zh="偶发故障剧", identity="title:flaky:0:tv")
    calls = {"n": 0}

    class FlakyClient:
        http_429_responses = 0

        def budget_status(self):
            return {"day": "2026-09-04", "used": 0, "budget": 100, "remaining": 100}

        def search(self, kind, query, *, year=None, language="zh-CN"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return tmdb.CacheEntry("k", "empty", None, None, None, 0, None, None)

        def genres(self, kind, language="zh-CN"):
            return {}

        def details(self, kind, tmdb_id, *, language="zh-CN"):
            raise AssertionError("should not be called")

    enricher = tmdb.BackgroundEnricher(
        lambda: store, lambda: FlakyClient(),
        **_enricher_kwargs(
            tmp_path, conn_factory=lambda: sqlite3.connect(str(store.db_path)),
            sleep=_fast_sleep, idle_seconds=0.01, round_seconds=0.01,
        ),
    )
    try:
        enricher.start()
        _wait_until(lambda: enricher.status()["last_error_class"] == "RuntimeError")
        assert enricher.status()["running"] is True
        _wait_until(lambda: enricher.status()["last_round_at"] is not None)
        status = enricher.status()
    finally:
        enricher.stop(timeout=2)
    assert status["last_round_at"] is not None
    assert calls["n"] >= 2


def test_processed_today_resets_on_utc_day_change(store, tmp_path):
    _add_media(store, title_zh="第一天剧", identity="title:day0:0:tv")
    _add_media(store, title_zh="第二天剧", identity="title:day1:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 2)

    def client_factory():
        client, _clock = _client(store, session)
        return client

    day_box = {"value": "2026-09-04"}
    gate = threading.Event()

    def blocking_sleep(seconds):
        gate.wait(timeout=2)
        gate.clear()

    enricher = tmdb.BackgroundEnricher(
        lambda: store, client_factory,
        **_enricher_kwargs(
            tmp_path, conn_factory=lambda: sqlite3.connect(str(store.db_path)), sleep=blocking_sleep,
            batch_size=1, today=lambda: day_box["value"],
        ),
    )
    try:
        enricher.start()
        _wait_until(lambda: enricher.status()["last_round_at"] is not None)
        assert enricher.status()["processed_today"] == 1

        day_box["value"] = "2026-09-05"
        gate.set()  # let round 2 start on the new day
        _wait_until(lambda: len(session.calls) == 2)
        _wait_until(lambda: enricher.status()["processed_today"] == 1)
    finally:
        enricher.stop(timeout=0.2)
        gate.set()
        _wait_until(lambda: not enricher.status()["running"])
    assert len(session.calls) == 2


def test_requests_429_today_persisted_and_resets_on_utc_day_change(store, tmp_path):
    # T14/§4: persisted like processed_today -- day-scoped, leader-written.
    _add_media(store, title_zh="限流剧", identity="title:ratelimited:0:tv")
    _add_media(store, title_zh="第二天剧", identity="title:day1:0:tv")
    session = FakeSession()
    # Round 1's media exhausts all 4 retries as 429s; round 2's (a new day)
    # media then hits the trailing 200 -- a single route list avoids any
    # re-routing race between the two rounds.
    session.route(
        SEARCH_TV_URL,
        [FakeResponse(429, headers={"Retry-After": "0"}) for _ in range(4)] + [FakeResponse(200, {"results": []})],
    )

    def client_factory():
        client, _clock = _client(store, session, limits=tmdb.Limits(max_concurrency=1, min_interval_ms=0, daily_budget=100))
        return client

    def conn_factory():
        return sqlite3.connect(str(store.db_path))

    day_box = {"value": "2026-09-04"}
    gate = threading.Event()

    def blocking_sleep(seconds):
        gate.wait(timeout=2)
        gate.clear()

    enricher = tmdb.BackgroundEnricher(
        lambda: store, client_factory,
        **_enricher_kwargs(tmp_path, conn_factory=conn_factory, sleep=blocking_sleep, batch_size=1, today=lambda: day_box["value"]),
    )

    def _state() -> dict:
        conn = conn_factory()
        try:
            return tmdb.read_enricher_state(conn)
        finally:
            conn.close()

    try:
        enricher.start()
        _wait_until(lambda: _state().get("requests_429_today") is not None)
        # Fix wave 1/finding #1: 4 real 429 responses (the row's one search
        # exhausts all 4 retries as 429s) -> count 4, not the old "1 per
        # retry-exhausted search" behaviour.
        assert _state()["requests_429_today"] == "4"

        day_box["value"] = "2026-09-05"
        gate.set()  # let round 2 (a new day, no 429) start
        _wait_until(lambda: enricher.status()["processed_today"] == 1)
        _wait_until(lambda: _state().get("requests_429_today") == "0")
    finally:
        enricher.stop(timeout=0.2)
        gate.set()
        _wait_until(lambda: not enricher.status()["running"])


def test_merge_by_tmdb_skipped_when_round_has_no_exact_writes(store, tmp_path, monkeypatch):
    _add_media(store, title_zh="无精确匹配后台剧", identity="title:bg-no-exact:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))

    def client_factory():
        client, _clock = _client(store, session)
        return client

    merge_calls = []
    monkeypatch.setattr(tmdb, "merge_by_tmdb", lambda s: merge_calls.append(s) or 0)

    enricher = tmdb.BackgroundEnricher(
        lambda: store, client_factory,
        **_enricher_kwargs(tmp_path, conn_factory=lambda: sqlite3.connect(str(store.db_path)), sleep=_fast_sleep),
    )
    try:
        enricher.start()
        _wait_until(lambda: enricher.status()["last_round_at"] is not None)
        real_time.sleep(0.05)
    finally:
        enricher.stop(timeout=2)
    assert merge_calls == []


def test_merge_by_tmdb_called_when_round_has_exact_writes(store, tmp_path, monkeypatch):
    _add_media(store, title_zh="精确匹配后台剧", identity="title:bg-exact:0:tv")
    session = FakeSession()
    session.route(SEARCH_TV_URL, FakeResponse(200, {"results": [{"id": 970, "name": "精确匹配后台剧"}]}))
    session.route(GENRE_TV_URL, FakeResponse(200, {"genres": []}))

    def client_factory():
        client, _clock = _client(store, session)
        return client

    merge_calls = []
    monkeypatch.setattr(tmdb, "merge_by_tmdb", lambda s: merge_calls.append(s) or 0)

    enricher = tmdb.BackgroundEnricher(
        lambda: store, client_factory,
        **_enricher_kwargs(tmp_path, conn_factory=lambda: sqlite3.connect(str(store.db_path)), sleep=_fast_sleep),
    )
    try:
        enricher.start()
        _wait_until(lambda: len(merge_calls) >= 1)
    finally:
        enricher.stop(timeout=2)
    assert merge_calls == [store]


def test_status_fields_present_before_start(tmp_path):
    enricher = tmdb.BackgroundEnricher(lambda: None, lambda: None, **_enricher_kwargs(tmp_path))
    status = enricher.status()
    assert set(status) == {
        "running", "leader", "leader_attempts", "last_round_at", "processed_today",
        "next_check_in", "last_error_class",
    }
    assert status["running"] is False
    assert status["leader"] is False
    assert status["leader_attempts"] == 0
    assert status["processed_today"] == 0


# --- three-lock P0 fix: run lock, persisted state, worker-state agreement ---------


def test_run_lock_busy_skips_round_without_error_and_retries_after_release(store, tmp_path):
    _add_media(store, title_zh="占用运行锁剧", identity="title:run-busy:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))

    def client_factory():
        client, _clock = _client(store, session)
        return client

    lock_paths = tmdb.LockPaths.for_data_dir(tmp_path)
    lock_paths.run.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_paths.run, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

    enricher = tmdb.BackgroundEnricher(
        lambda: store, client_factory,
        leader_lock_path=lock_paths.leader, run_lock_path=lock_paths.run,
        conn_factory=lambda: sqlite3.connect(str(store.db_path)),
        sleep=_fast_sleep, round_seconds=0.02, idle_seconds=0.02,
    )
    try:
        enricher.start()
        _wait_until(lambda: enricher.status()["running"] is True)
        real_time.sleep(0.08)
        # The run lock is held by the test -- no round can have completed,
        # and being unable to get the lock must never be recorded as an
        # error (a manual/CLI enrich holding it is an expected condition).
        assert enricher.status()["last_round_at"] is None
        assert enricher.status()["last_error_class"] is None
        assert session.calls == []

        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

        _wait_until(lambda: enricher.status()["last_round_at"] is not None)
    finally:
        enricher.stop(timeout=2)
    assert len(session.calls) >= 1


def test_two_enrichers_sharing_lock_paths_agree_on_running_worker_state(store, tmp_path):
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))

    def client_factory():
        client, _clock = _client(store, session)
        return client

    lock_paths = tmdb.LockPaths.for_data_dir(tmp_path)

    def conn_factory():
        return sqlite3.connect(str(store.db_path))

    kwargs = dict(
        leader_lock_path=lock_paths.leader,
        run_lock_path=lock_paths.run,
        conn_factory=conn_factory,
        sleep=_fast_sleep,
        idle_seconds=0.02,
        round_seconds=0.02,
    )
    a = tmdb.BackgroundEnricher(lambda: store, client_factory, **kwargs)
    b = tmdb.BackgroundEnricher(lambda: store, client_factory, **kwargs)

    def _worker_state() -> str:
        conn = conn_factory()
        try:
            state = tmdb.read_enricher_state(conn)
        finally:
            conn.close()
        return tmdb.derive_worker_state(
            state, now=real_time.time(), enabled=True, key_configured=True, installed=True, idle_seconds=0.02,
        )

    try:
        a.start()
        b.start()
        _wait_until(lambda: _worker_state() == "running")
        # Exactly one of the two won the leader-election flock, but both
        # independently derive "running" from the *same* persisted state
        # row -- that agreement across workers is the whole point of C.3.
        assert sorted([a.status()["running"], b.status()["running"]]) == [False, True]
        assert _worker_state() == "running"
    finally:
        a.stop(timeout=2)
        b.stop(timeout=2)


def test_heartbeat_is_written_while_idle(tmp_path):
    def conn_factory():
        return sqlite3.connect(str(tmp_path / "idle-state.db"))

    lock_paths = tmdb.LockPaths.for_data_dir(tmp_path)
    enricher = tmdb.BackgroundEnricher(
        lambda: None, lambda: None,
        leader_lock_path=lock_paths.leader, run_lock_path=lock_paths.run,
        conn_factory=conn_factory, sleep=_fast_sleep, idle_seconds=0.01,
    )

    def _has_heartbeat() -> bool:
        conn = conn_factory()
        try:
            state = tmdb.read_enricher_state(conn)
        finally:
            conn.close()
        return bool(state.get("heartbeat_at"))

    try:
        enricher.start()
        _wait_until(_has_heartbeat)
    finally:
        enricher.stop(timeout=2)


def test_conn_factory_returning_none_skips_persistence_without_error(tmp_path):
    # Review fix #1: production wiring's conn_factory returns None instead
    # of a connection when the library DB file doesn't exist yet (see
    # tests/test_library_install.py for the end-to-end regression) --
    # library_tmdb itself must treat that as "nothing to persist to", not
    # crash and not conjure up a connection some other way.
    calls = {"n": 0}

    def conn_factory():
        calls["n"] += 1
        return None

    lock_paths = tmdb.LockPaths.for_data_dir(tmp_path)
    enricher = tmdb.BackgroundEnricher(
        lambda: None, lambda: None,
        leader_lock_path=lock_paths.leader, run_lock_path=lock_paths.run,
        conn_factory=conn_factory, sleep=_fast_sleep, idle_seconds=0.01,
    )
    try:
        enricher.start()
        _wait_until(lambda: calls["n"] >= 2)
        assert enricher.status()["running"] is True
    finally:
        enricher.stop(timeout=2)


def test_state_write_failure_is_logged_and_swallowed_round_still_proceeds(store, tmp_path, caplog):
    # Review fix #2: _persist_heartbeat() used to run above the loop's
    # try/except, and _persist_round_state() was only re-tried inside the
    # except handler -- either way, a sqlite3.OperationalError raised while
    # persisting state (DB locked by a concurrent install/merge, read-only
    # dir, ...) escaped _loop entirely and killed the leader thread for
    # good. Persisting state must never be able to kill this thread.
    _add_media(store, title_zh="状态写入失败剧", identity="title:state-write-fail:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))

    def client_factory():
        client, _clock = _client(store, session)
        return client

    calls = {"n": 0}

    def flaky_conn_factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return sqlite3.connect(str(store.db_path))

    enricher = tmdb.BackgroundEnricher(
        lambda: store, client_factory,
        **_enricher_kwargs(tmp_path, conn_factory=flaky_conn_factory, sleep=_fast_sleep),
    )
    with caplog.at_level(logging.WARNING):
        try:
            enricher.start()
            _wait_until(lambda: enricher.status()["last_round_at"] is not None)
            assert enricher.status()["running"] is True
        finally:
            enricher.stop(timeout=2)
    assert calls["n"] >= 2
    assert "OperationalError" in caplog.text
    assert "database is locked" not in caplog.text


def test_idle_seconds_property_exposes_configured_value(tmp_path):
    # Review fix #7: app.py's status endpoint used to reach into the
    # private _idle_seconds attribute -- a small public property is the
    # supported way to read it back.
    enricher = tmdb.BackgroundEnricher(
        lambda: None, lambda: None, **_enricher_kwargs(tmp_path, idle_seconds=42)
    )
    assert enricher.idle_seconds == 42


# --- no API key in logs or reports ------------------------------------------------


def test_no_api_key_in_enrich_batch_logs(store, caplog):
    _add_media(store, title_zh="日志检查剧")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _clock = _client(store, session, api_key="tmdb-fake-key-never-logged")

    with caplog.at_level(logging.DEBUG):
        tmdb.enrich_batch(store, client, limit=10)

    assert "tmdb-fake-key-never-logged" not in caplog.text


# --- offline script: scripts/enrich_media_tmdb.py ----------------------------------


def test_arg_parser_defaults():
    parser = enrich_script.build_arg_parser()
    args = parser.parse_args(["--index", "/tmp/fake.db"])
    assert args.resume is True
    assert args.retry_failed is False
    assert args.max_media is None
    assert args.with_details is False
    assert args.report is None


def test_run_missing_api_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    parser = enrich_script.build_arg_parser()
    args = parser.parse_args(["--index", str(db_path)])
    with pytest.raises(SystemExit):
        enrich_script.run(args)


def test_run_missing_index_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-fake-key-for-script-test")
    parser = enrich_script.build_arg_parser()
    args = parser.parse_args(["--index", str(tmp_path / "does-not-exist.db")])
    with pytest.raises(SystemExit):
        enrich_script.run(args)


def test_run_reports_busy_when_run_lock_is_held(store, tmp_path, monkeypatch):
    # A5: the offline script shares the same run lock as the background
    # enricher/CLI (via LockPaths) -- it must never block waiting for it.
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-fake-key-for-script-test")
    lock_paths = tmdb.LockPaths.for_data_dir(store.db_path.parent)
    lock_paths.run.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_paths.run, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        parser = enrich_script.build_arg_parser()
        args = parser.parse_args(["--index", str(store.db_path)])
        report = enrich_script.run(args, session=FakeSession())
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert report == {"ok": False, "error": "TMDB_ENRICH_BUSY"}


def test_main_exits_1_when_run_reports_busy(monkeypatch):
    monkeypatch.setattr(enrich_script, "run", lambda args, session=None: {"ok": False, "error": "TMDB_ENRICH_BUSY"})
    exit_code = enrich_script.main(["--index", "/does-not-matter"])
    assert exit_code == 1


def test_run_end_to_end_writes_report_without_key(store, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-fake-key-for-script-test")
    _add_media(store, title_zh="脚本测试剧", identity="title:script:0:tv")
    session = FakeSession()
    session.route(SEARCH_TV_URL, FakeResponse(200, {"results": [{"id": 950, "name": "脚本测试剧"}]}))
    session.route(GENRE_TV_URL, FakeResponse(200, {"genres": []}))

    report_path = tmp_path / "report.json"
    parser = enrich_script.build_arg_parser()
    args = parser.parse_args(["--index", str(store.db_path), "--report", str(report_path)])

    with caplog.at_level(logging.DEBUG):
        report = enrich_script.run(args, session=session)

    assert report["matched_exact"] == 1
    assert report["merged_by_tmdb"] == 0
    assert "tmdb-fake-key-for-script-test" not in json.dumps(report)
    assert "tmdb-fake-key-for-script-test" not in caplog.text

    assert report_path.exists()
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk == report
    assert "tmdb-fake-key-for-script-test" not in report_path.read_text(encoding="utf-8")


def test_run_retry_failed_clears_failed_retryable_cache(store, tmp_path, monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-fake-key-for-script-test")
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO tmdb_cache (cache_key, status, fetched_at, retry_after, error_class) VALUES (?, 'failed_retryable', 0, 999999999, 'HTTP429')",
            ("search:tv:zh-CN:stale:0",),
        )
        conn.commit()
    finally:
        conn.close()

    parser = enrich_script.build_arg_parser()
    args = parser.parse_args(["--index", str(store.db_path), "--retry-failed"])
    session = FakeSession()
    report = enrich_script.run(args, session=session)

    assert report["retry_failed_cleared"] == 1
    conn = store.connect(readonly=True)
    try:
        remaining = conn.execute("SELECT COUNT(*) FROM tmdb_cache WHERE status='failed_retryable'").fetchone()[0]
    finally:
        conn.close()
    assert remaining == 0


def test_run_terminates_without_max_media_once_queue_only_has_cache_resolved_media(store, tmp_path, monkeypatch):
    # Regression for the T3 bug: a zero-result media stays match_status =
    # 'unmatched' forever, so without the candidate-queue cache-skip fix,
    # run()'s "while remaining is None or remaining > 0" loop (no
    # --max-media given) would re-select it every pass and never see
    # candidates_considered == 0 -> infinite loop.
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-fake-key-for-script-test")
    _add_media(store, title_zh="脚本已解析剧", identity="title:script-resolved:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))

    parser = enrich_script.build_arg_parser()
    args = parser.parse_args(["--index", str(store.db_path)])

    first_report = enrich_script.run(args, session=session)
    assert first_report["matched_unmatched"] == 1
    calls_after_first_run = len(session.calls)

    second_report = enrich_script.run(args, session=session)

    assert second_report["candidates_considered"] == 0
    assert len(session.calls) == calls_after_first_run


def test_run_max_media_caps_total_processed(store, tmp_path, monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-fake-key-for-script-test")
    for i in range(5):
        _add_media(store, title_zh=f"上限剧{i}", identity=f"title:cap{i}:0:tv")
    session = FakeSession().route(SEARCH_TV_URL, [FakeResponse(200, {"results": []})] * 10)

    parser = enrich_script.build_arg_parser()
    args = parser.parse_args(["--index", str(store.db_path), "--max-media", "2"])
    report = enrich_script.run(args, session=session)

    assert len(session.calls) == 4  # 2 candidates x (zh-CN + the en-US retry)
    assert report["matched_unmatched"] + report["matched_exact"] + report["matched_candidate"] + report["matched_needs_review"] == 2
