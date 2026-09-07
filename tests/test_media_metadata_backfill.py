"""Unit tests for scripts/media_metadata_backfill.py (T15 addendum §5).

FakeSession-only, mirroring tests/test_library_tmdb_hints.py's plumbing --
the shared tests/conftest.py network guard additionally blocks any
accidental real HTTP call.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import library_store as ls  # noqa: E402
import library_tmdb as tmdb  # noqa: E402
import media_metadata_backfill as backfill  # noqa: E402


# --- fake HTTP plumbing (mirrors tests/test_library_tmdb_hints.py) -----------


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


def _details_url(kind: str, tmdb_id: int) -> str:
    return tmdb.TMDB_BASE + f"/{kind}/{tmdb_id}"


def _client(store, session, **kwargs):
    clock = FakeClock()
    factory = lambda: sqlite3.connect(str(store.db_path))  # noqa: E731
    return tmdb.TmdbClient(
        "tmdb-key-for-tests", conn_factory=factory, lock_path=store.db_path.parent / "client.lock",
        session=session, clock=clock.time, sleep=clock.sleep, **kwargs,
    )


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


def _add_exact_media(store, *, media_type="movie", tmdb_id=100, overview=None, poster_path=None, identity=None) -> int:
    identity = identity or f"tmdb:{media_type}:{tmdb_id}"
    rec = ls.MediaRecord(
        media_identity=identity, media_type=media_type, title_zh="标题", search_key="标题",
        tmdb_id=tmdb_id, overview=overview, poster_path=poster_path, match_status="exact",
    )
    return store.upsert_media(rec)


def _add_unmatched_media(store, *, title_zh, identity, media_type="movie") -> int:
    rec = ls.MediaRecord(media_identity=identity, media_type=media_type, title_zh=title_zh, search_key=title_zh, match_status="unmatched")
    return store.upsert_media(rec)


def _get_media(store, media_id) -> dict:
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    finally:
        conn.close()
    return dict(row)


def _detail_payload(**overrides) -> dict:
    payload = {
        "overview": "中文简介", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
        "genres": [{"id": 1, "name": "剧情"}], "vote_average": 8.1, "vote_count": 500,
        "external_ids": {"imdb_id": "tt0000321"},
    }
    payload.update(overrides)
    return payload


# --- select_metadata_rows ----------------------------------------------------


def test_select_metadata_rows_only_exact_and_incomplete(store):
    complete_id = _add_exact_media(store, tmdb_id=1, overview="有简介", poster_path="/p.jpg", identity="tmdb:movie:1")
    missing_poster_id = _add_exact_media(store, tmdb_id=2, overview="有简介", poster_path=None, identity="tmdb:movie:2")
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET match_status='candidate' WHERE id=?", (complete_id,))
        conn.commit()
    finally:
        conn.close()

    conn = store.connect(readonly=True)
    try:
        rows = backfill.select_metadata_rows(conn, ids=[], limit=10, statuses=("pending", "error"), resume_after=None)
    finally:
        conn.close()
    ids = [row["id"] for row in rows]
    assert missing_poster_id in ids
    assert complete_id not in ids  # not exact anymore


def test_select_metadata_rows_by_explicit_ids_ignores_status_filter(store):
    media_id = _add_exact_media(store, tmdb_id=3, overview=None, poster_path=None, identity="tmdb:movie:3")
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET metadata_status='complete' WHERE id=?", (media_id,))
        conn.commit()
    finally:
        conn.close()
    conn = store.connect(readonly=True)
    try:
        rows = backfill.select_metadata_rows(conn, ids=[media_id], limit=10, statuses=("pending", "error"), resume_after=None)
    finally:
        conn.close()
    assert [row["id"] for row in rows] == [media_id]


def test_select_metadata_rows_retryable_only(store):
    retryable_id = _add_exact_media(store, tmdb_id=4, identity="tmdb:movie:4")
    permanent_id = _add_exact_media(store, tmdb_id=5, identity="tmdb:movie:5")
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET metadata_status='error', metadata_error='retryable:HTTP503' WHERE id=?", (retryable_id,))
        conn.execute("UPDATE media SET metadata_status='error', metadata_error='permanent:HTTP404' WHERE id=?", (permanent_id,))
        conn.commit()
    finally:
        conn.close()
    conn = store.connect(readonly=True)
    try:
        rows = backfill.select_metadata_rows(conn, ids=[], limit=10, statuses=("error",), resume_after=None, retryable_only=True)
    finally:
        conn.close()
    assert [row["id"] for row in rows] == [retryable_id]


# --- process_enrich_row -------------------------------------------------------


def test_process_enrich_row_writes_complete_metadata_and_ratings(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=100)
    session = FakeSession().route(_details_url("movie", 100), FakeResponse(200, _detail_payload()))
    client = _client(store, session)

    outcome = backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    assert outcome["status"] == "complete"
    row = _get_media(store, media_id)
    assert row["metadata_status"] == "complete"
    assert row["metadata_source"] == "tmdb"
    assert row["overview"] == "中文简介"
    assert row["poster_path"] == "/p.jpg"
    assert row["imdb_id"] == "tt0000321"
    ratings = json.loads(row["ratings_json"])
    assert ratings["tmdb"]["score"] == 8.1
    assert ratings["tmdb"]["votes"] == 500
    assert row["ratings_status"] == "complete"

    conn = store.connect(readonly=True)
    try:
        attempt = conn.execute("SELECT phase, source, status FROM media_metadata_attempts WHERE media_id=?", (media_id,)).fetchone()
    finally:
        conn.close()
    assert tuple(attempt) == ("enrich", "tmdb", "ok")


def _add_imdb_rating(store, imdb_id, *, rating, votes, as_of="2026-07-01"):
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO imdb_ratings (imdb_id, rating, votes, as_of) VALUES (?, ?, ?, ?)",
            (imdb_id, rating, votes, as_of),
        )
        conn.commit()
    finally:
        conn.close()


def test_process_enrich_row_joins_imdb_ratings_and_merges_with_tmdb(store):
    # T15 fix wave 1 (item 3a/3b): the enrich phase also joins imdb_ratings
    # by the imdb_id discovered from external_ids -- merged alongside the
    # tmdb entry, never replacing it, and ratings_status accounts for both
    # applicable sources.
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=110, identity="tmdb:movie:110")
    _add_imdb_rating(store, "tt0000321", rating=8.7, votes=99999, as_of="2026-07-01")
    session = FakeSession().route(_details_url("movie", 110), FakeResponse(200, _detail_payload()))
    client = _client(store, session)

    backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    row = _get_media(store, media_id)
    ratings = json.loads(row["ratings_json"])
    assert ratings["tmdb"]["score"] == 8.1
    assert ratings["imdb"] == {"score": 8.7, "scale": 10, "votes": 99999, "as_of": "2026-07-01"}
    assert row["ratings_status"] == "complete"


def test_process_enrich_row_merges_ratings_without_clobbering_existing_key(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=111, identity="tmdb:movie:111")
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE media SET ratings_json=? WHERE id=?",
            (json.dumps({"manual": {"score": 9.0, "scale": 10, "votes": None, "as_of": "2026-01-01"}}), media_id),
        )
        conn.commit()
    finally:
        conn.close()
    session = FakeSession().route(_details_url("movie", 111), FakeResponse(200, _detail_payload(external_ids={})))
    client = _client(store, session)

    backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    row = _get_media(store, media_id)
    ratings = json.loads(row["ratings_json"])
    assert ratings["manual"] == {"score": 9.0, "scale": 10, "votes": None, "as_of": "2026-01-01"}
    assert ratings["tmdb"]["score"] == 8.1


def test_process_enrich_row_failure_sets_ratings_error_when_nothing_valid_yet(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=112, identity="tmdb:movie:112")
    session = FakeSession().route(_details_url("movie", 112), FakeResponse(404, {}))
    client = _client(store, session)

    backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    row = _get_media(store, media_id)
    assert row["ratings_status"] == "error"
    assert row["ratings_error"] == tmdb.RATINGS_FETCH_ERROR_CLASS


def test_process_enrich_row_failure_never_downgrades_existing_valid_ratings(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=113, identity="tmdb:movie:113")
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE media SET ratings_json=?, ratings_status='complete' WHERE id=?",
            (json.dumps({"tmdb": {"score": 7.0, "scale": 10, "votes": 10, "as_of": "2026-01-01"}}), media_id),
        )
        conn.commit()
    finally:
        conn.close()
    session = FakeSession().route(_details_url("movie", 113), FakeResponse(404, {}))
    client = _client(store, session)

    backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    row = _get_media(store, media_id)
    assert row["ratings_status"] == "complete"  # never downgraded to error
    assert json.loads(row["ratings_json"]) == {"tmdb": {"score": 7.0, "scale": 10, "votes": 10, "as_of": "2026-01-01"}}
    assert row["ratings_error"] is None


def test_process_enrich_row_falls_back_to_english_overview(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=101)
    session = (
        FakeSession()
        .route(_details_url("movie", 101), FakeResponse(200, _detail_payload(overview="")))
    )
    # en-US retry is a SEPARATE cache key (language folded in) but the same
    # URL -- FakeSession routes by URL only, so use a list to serve zh then en.
    session._routes[_details_url("movie", 101)] = [
        FakeResponse(200, _detail_payload(overview="")),
        FakeResponse(200, _detail_payload(overview="English overview")),
    ]
    client = _client(store, session)

    outcome = backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    assert outcome["status"] == "complete"
    row = _get_media(store, media_id)
    assert row["overview"] == "English overview"
    assert len(session.calls_to(_details_url("movie", 101))) == 2


def test_process_enrich_row_partial_when_both_overviews_empty(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=102)
    session = FakeSession().route(_details_url("movie", 102), FakeResponse(200, _detail_payload(overview="")))
    client = _client(store, session)

    outcome = backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    assert outcome["status"] == "partial"
    row = _get_media(store, media_id)
    assert row["metadata_status"] == "partial"
    assert row["poster_path"] == "/p.jpg"  # poster still saved


def test_process_enrich_row_partial_when_poster_missing(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=103)
    session = FakeSession().route(_details_url("movie", 103), FakeResponse(200, _detail_payload(poster_path=None)))
    client = _client(store, session)

    outcome = backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    assert outcome["status"] == "partial"


def test_process_enrich_row_retryable_failure_marks_error(store):
    media_id = _add_exact_media(store, media_type="tv", tmdb_id=104)
    session = FakeSession().route(_details_url("tv", 104), [FakeResponse(503, {})] * 4)
    client = _client(store, session)

    outcome = backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    assert outcome["status"] == "error"
    row = _get_media(store, media_id)
    assert row["metadata_status"] == "error"
    assert row["metadata_error"].startswith("retryable:")


def test_process_enrich_row_permanent_failure_marks_error(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=105)
    session = FakeSession().route(_details_url("movie", 105), FakeResponse(404, {}))
    client = _client(store, session)

    outcome = backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    assert outcome["status"] == "error"
    row = _get_media(store, media_id)
    assert row["metadata_error"].startswith("permanent:")


def test_process_enrich_row_dry_run_makes_no_write(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=106)
    session = FakeSession().route(_details_url("movie", 106), FakeResponse(200, _detail_payload()))
    client = _client(store, session)

    outcome = backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=True)

    assert outcome["status"] == "complete"
    row = _get_media(store, media_id)
    assert row["metadata_status"] == "pending"
    assert row["overview"] is None
    conn = store.connect(readonly=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM media_metadata_attempts").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_process_enrich_row_never_clears_confirmed_fields_on_failure(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=107, overview="旧简介", poster_path="/old.jpg")
    session = FakeSession().route(_details_url("movie", 107), FakeResponse(404, {}))
    client = _client(store, session)

    backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)

    row = _get_media(store, media_id)
    assert row["overview"] == "旧简介"
    assert row["poster_path"] == "/old.jpg"


def test_process_enrich_row_skips_non_movie_tv_type(store):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=108)
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET media_type='unknown' WHERE id=?", (media_id,))
        conn.commit()
    finally:
        conn.close()
    session = FakeSession()
    client = _client(store, session)

    outcome = backfill.process_enrich_row(store, client, _get_media_row(store, media_id), phase="enrich", dry_run=False)
    assert outcome["status"] == "skipped"
    assert session.calls == []


def _get_media_row(store, media_id) -> sqlite3.Row:
    conn = store.connect(readonly=True)
    try:
        return conn.execute(
            "SELECT id, media_type, tmdb_id, overview, poster_path, imdb_id, tvmaze_id, ratings_json, ratings_status "
            "FROM media WHERE id=?", (media_id,)
        ).fetchone()
    finally:
        conn.close()


# --- run_report ---------------------------------------------------------------


def test_run_report_counts_and_no_network(store):
    _add_exact_media(store, tmdb_id=200, overview="有", poster_path="/p.jpg", identity="tmdb:movie:200")
    report = backfill.run_report(store)
    assert report["phase"] == "report"
    assert report["match_status"]["exact"] == 1
    assert "metadata_status" in report
    assert "ratings_status" in report
    assert "cache" in report


# --- run_no_network_preview ---------------------------------------------------


def test_no_network_preview_counts_enrich_rows(store):
    _add_exact_media(store, tmdb_id=300, identity="tmdb:movie:300")
    result = backfill.run_no_network_preview(store, phase="enrich", ids=[], limit=10)
    assert result == {"phase": "enrich", "no_network": True, "dry_run": True, "would_process": 1}


def test_no_network_preview_counts_verify_rows(store):
    rec = ls.MediaRecord(media_identity="title:x:0:movie", media_type="movie", title_zh="x", search_key="x", match_status="unmatched")
    store.upsert_media(rec)
    result = backfill.run_no_network_preview(store, phase="verify", ids=[], limit=10)
    assert result["would_process"] == 1


def test_no_network_preview_verify_matches_real_queue_selection(store):
    # T15 fix wave 1 (item 6a): the preview must count exactly what
    # enrich_batch's own queue would take -- an ``unmatched`` row whose
    # primary search cache entry is already resolved is skipped by
    # _select_fresh_candidates and must not be counted here either.
    _add_unmatched_media(store, title_zh="已解析", identity="title:resolved:0:movie")
    _add_unmatched_media(store, title_zh="待处理", identity="title:pending2:0:movie")
    factory = lambda: sqlite3.connect(str(store.db_path))  # noqa: E731
    cache = tmdb.TmdbCache(factory)
    key = tmdb.search_cache_key("movie", "zh-CN", tmdb._fold("已解析"), None)
    cache.put(tmdb.CacheEntry(key, "failed_permanent", None, None, None, 0, None, "HTTP404"))

    result = backfill.run_no_network_preview(store, phase="verify", ids=[], limit=10)
    assert result["would_process"] == 1


def test_dry_run_enrich_makes_no_http_requests(store, monkeypatch):
    # T15 fix wave 1 (item 2): --dry-run must never spend quota or write
    # cache/budget -- not even for enrich/retry, which previously still
    # made the real request.
    _add_exact_media(store, media_type="movie", tmdb_id=800, identity="tmdb:movie:800")

    def _boom(db_path, args):
        raise AssertionError("--dry-run must not construct a TmdbClient")

    monkeypatch.setattr(backfill, "_build_client", _boom)
    args = backfill.build_arg_parser().parse_args([
        "--phase", "enrich", "--db", str(store.db_path), "--dry-run",
    ])
    result = backfill.run(args)
    assert result == {"phase": "enrich", "no_network": True, "dry_run": True, "would_process": 1}


def test_dry_run_retry_makes_no_http_requests(store, monkeypatch):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=801, identity="tmdb:movie:801")
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET metadata_status='error', metadata_error='retryable:HTTP503' WHERE id=?", (media_id,))
        conn.commit()
    finally:
        conn.close()

    def _boom(db_path, args):
        raise AssertionError("--dry-run must not construct a TmdbClient")

    monkeypatch.setattr(backfill, "_build_client", _boom)
    args = backfill.build_arg_parser().parse_args([
        "--phase", "retry", "--db", str(store.db_path), "--dry-run",
    ])
    result = backfill.run(args)
    assert result == {"phase": "retry", "no_network": True, "dry_run": True, "would_process": 1}


# --- resume cursor -------------------------------------------------------------


def test_resume_cursor_round_trips(store):
    assert backfill._read_resume_cursor(store, "enrich") is None
    backfill._write_resume_cursor(store, "enrich", 42)
    assert backfill._read_resume_cursor(store, "enrich") == 42


def test_clear_resume_cursor(store):
    backfill._write_resume_cursor(store, "enrich", 42)
    backfill._clear_resume_cursor(store, "enrich")
    assert backfill._read_resume_cursor(store, "enrich") is None


def test_run_stops_batch_on_first_stopped_and_cursor_advances_only_past_completed(store, monkeypatch):
    id_a = _add_exact_media(store, media_type="movie", tmdb_id=700, identity="tmdb:movie:700")
    id_b = _add_exact_media(store, media_type="movie", tmdb_id=701, identity="tmdb:movie:701")
    id_c = _add_exact_media(store, media_type="movie", tmdb_id=702, identity="tmdb:movie:702")
    session = (
        FakeSession()
        .route(_details_url("movie", 700), FakeResponse(200, _detail_payload()))
        .route(_details_url("movie", 701), FakeResponse(200, _detail_payload()))
        .route(_details_url("movie", 702), FakeResponse(200, _detail_payload()))
    )
    limits = tmdb.Limits(max_concurrency=1, min_interval_ms=0, daily_budget=1)
    client = _client(store, session, limits=limits)
    monkeypatch.setattr(backfill, "_build_client", lambda db_path, args: client)

    args = backfill.build_arg_parser().parse_args([
        "--phase", "enrich", "--db", str(store.db_path), "--limit", "3", "--concurrency", "1", "--resume",
    ])
    result = backfill.run(args)

    assert result["processed"] == 2
    assert result["by_status"] == {"complete": 1, "stopped": 1}
    assert session.calls_to(_details_url("movie", 702)) == []  # third row never attempted
    row_c = _get_media(store, id_c)
    assert row_c["metadata_status"] == "pending"

    assert backfill._read_resume_cursor(store, "enrich") == id_a

    conn = store.connect(readonly=True)
    try:
        stopped_attempt = conn.execute(
            "SELECT phase, source, status, error_class FROM media_metadata_attempts WHERE media_id=?", (id_b,)
        ).fetchone()
    finally:
        conn.close()
    assert tuple(stopped_attempt) == ("enrich", "tmdb", "stopped", "BudgetExhausted")


def test_run_resume_cursor_cleared_when_queue_exhausted_recovers_lower_pending_rows(store, monkeypatch):
    id_a = _add_exact_media(store, media_type="movie", tmdb_id=900, identity="tmdb:movie:900")
    id_b = _add_exact_media(store, media_type="movie", tmdb_id=901, identity="tmdb:movie:901")
    session = (
        FakeSession()
        .route(_details_url("movie", 900), FakeResponse(200, _detail_payload()))
        .route(_details_url("movie", 901), FakeResponse(200, _detail_payload()))
    )
    client = _client(store, session)
    monkeypatch.setattr(backfill, "_build_client", lambda db_path, args: client)

    args1 = backfill.build_arg_parser().parse_args([
        "--phase", "enrich", "--db", str(store.db_path), "--limit", "1", "--resume", "--concurrency", "1",
    ])
    first = backfill.run(args1)
    assert first["processed"] == 1
    assert backfill._read_resume_cursor(store, "enrich") == id_a

    # id_a becomes pending again below the cursor (e.g. a manual reset) --
    # it must not be hidden forever just because its id is <= the cursor.
    conn = store.connect()
    try:
        conn.execute("UPDATE media SET metadata_status='pending', overview=NULL, poster_path=NULL WHERE id=?", (id_a,))
        conn.commit()
    finally:
        conn.close()

    args2 = backfill.build_arg_parser().parse_args([
        "--phase", "enrich", "--db", str(store.db_path), "--limit", "10", "--resume", "--concurrency", "1",
    ])
    second = backfill.run(args2)
    assert second["processed"] == 1  # only id_b is visible above the stale cursor
    assert backfill._read_resume_cursor(store, "enrich") is None  # queue exhausted -> cursor cleared

    third = backfill.run(args2)
    assert third["processed"] == 1  # id_a resurfaces now that the cursor was cleared


def test_run_concurrency_two_caps_cursor_at_first_stop_within_a_chunk(store, monkeypatch):
    # Fix wave 2 (item 1): with --concurrency 2, both rows land in the SAME
    # dispatch chunk. id_a (lower id) hits a "stopped" outcome (an invalid
    # API key here -- same status/handling as BudgetExhausted, but
    # deterministic regardless of thread scheduling since it depends only
    # on which URL each row's own request hits, not a raced budget
    # counter) while id_b (higher id) completes concurrently in that same
    # chunk. The resume cursor must never advance past id_b just because it
    # happened to finish -- id_a was never actually completed.
    id_a = _add_exact_media(store, media_type="movie", tmdb_id=800, identity="tmdb:movie:800")
    id_b = _add_exact_media(store, media_type="movie", tmdb_id=801, identity="tmdb:movie:801")
    session = (
        FakeSession()
        .route(_details_url("movie", 800), FakeResponse(401, {"status_message": "invalid key"}))
        .route(_details_url("movie", 801), FakeResponse(200, _detail_payload()))
    )
    client = _client(store, session)
    monkeypatch.setattr(backfill, "_build_client", lambda db_path, args: client)

    args = backfill.build_arg_parser().parse_args([
        "--phase", "enrich", "--db", str(store.db_path), "--limit", "2", "--concurrency", "2", "--resume",
    ])
    result = backfill.run(args)

    assert result["by_status"] == {"stopped": 1, "complete": 1}
    row_b = _get_media(store, id_b)
    assert row_b["metadata_status"] == "complete"  # id_b really did complete

    cursor = backfill._read_resume_cursor(store, "enrich")
    assert cursor is None or cursor < id_a  # never advanced up to/past the stopped row

    # A subsequent --resume run must reselect id_a -- it was never actually
    # completed, regardless of sharing a chunk with a completed higher id.
    conn = store.connect(readonly=True)
    try:
        pending = backfill.select_metadata_rows(
            conn, ids=[], limit=10, statuses=("pending", "error"), resume_after=cursor,
        )
    finally:
        conn.close()
    assert id_a in [row["id"] for row in pending]


def test_run_without_resume_never_clears_an_existing_cursor(store, monkeypatch):
    # Fix wave 2 (item 4): a bare (non --resume) run never consulted the
    # cursor to select its rows, so it has no business resetting it out
    # from under a --resume cron job -- clearing it here would force that
    # job's next run to rescan the whole table instead of skipping past
    # the stored cursor.
    backfill._write_resume_cursor(store, "enrich", 999)
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=810, identity="tmdb:movie:810")
    session = FakeSession().route(_details_url("movie", 810), FakeResponse(200, _detail_payload()))
    client = _client(store, session)
    monkeypatch.setattr(backfill, "_build_client", lambda db_path, args: client)

    args = backfill.build_arg_parser().parse_args([
        "--phase", "enrich", "--db", str(store.db_path), "--limit", "10", "--concurrency", "1",
    ])
    result = backfill.run(args)

    assert result["processed"] == 1
    row = _get_media(store, media_id)
    assert row["metadata_status"] == "complete"
    assert backfill._read_resume_cursor(store, "enrich") == 999


def test_process_enrich_row_computes_ratings_status_none_and_clears_stale_ratings_error(store):
    # Fix wave 2 (item 2): ratings_status_for must be computed even when
    # this fetch found no applicable rating value at all ('none' must be
    # reachable) -- and a successful fetch always clears a stale
    # ratings_error, never leaves it dangling from a prior failed attempt.
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=930, identity="tmdb:movie:930")
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE media SET metadata_status='error', metadata_error='retryable:HTTP500', "
            "ratings_status='error', ratings_error=? WHERE id=?",
            (tmdb.RATINGS_FETCH_ERROR_CLASS, media_id),
        )
        conn.commit()
    finally:
        conn.close()
    payload = _detail_payload(vote_average=None, external_ids={})
    session = FakeSession().route(_details_url("movie", 930), FakeResponse(200, payload))
    client = _client(store, session)

    conn = store.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT id, media_type, tmdb_id, overview, poster_path, imdb_id, tvmaze_id, ratings_json, ratings_status "
            "FROM media WHERE id=?", (media_id,),
        ).fetchone()
    finally:
        conn.close()

    outcome = backfill.process_enrich_row(store, client, row, phase="retry", dry_run=False)

    assert outcome["status"] == "complete"
    row_after = _get_media(store, media_id)
    assert row_after["metadata_status"] == "complete"
    assert row_after["ratings_status"] == "none"
    assert row_after["ratings_error"] is None


# --- run()/main() end-to-end (monkeypatched client) ---------------------------


def test_run_enrich_end_to_end_via_run(store, monkeypatch, tmp_path):
    media_id = _add_exact_media(store, media_type="movie", tmdb_id=400, identity="tmdb:movie:400")
    session = FakeSession().route(_details_url("movie", 400), FakeResponse(200, _detail_payload()))
    client = _client(store, session)
    monkeypatch.setattr(backfill, "_build_client", lambda db_path, args: client)

    args = backfill.build_arg_parser().parse_args([
        "--phase", "enrich", "--db", str(store.db_path), "--limit", "10",
    ])
    result = backfill.run(args)

    assert result["phase"] == "enrich"
    assert result["processed"] == 1
    assert result["by_status"] == {"complete": 1}
    row = _get_media(store, media_id)
    assert row["metadata_status"] == "complete"


def test_run_report_via_main_prints_json(store, capsys):
    exit_code = backfill.main(["--phase", "report", "--db", str(store.db_path)])
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["ok"] is True
    assert printed["phase"] == "report"


def test_run_no_network_via_main_makes_no_client(store, monkeypatch, capsys):
    def _boom(db_path, args):
        raise AssertionError("must not build a client under --no-network")

    monkeypatch.setattr(backfill, "_build_client", _boom)
    exit_code = backfill.main(["--phase", "enrich", "--db", str(store.db_path), "--no-network"])
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["no_network"] is True


def test_run_resume_skips_already_advanced_cursor(store, monkeypatch):
    id_a = _add_exact_media(store, media_type="movie", tmdb_id=500, identity="tmdb:movie:500")
    id_b = _add_exact_media(store, media_type="movie", tmdb_id=501, identity="tmdb:movie:501")
    session = (
        FakeSession()
        .route(_details_url("movie", 500), FakeResponse(200, _detail_payload()))
        .route(_details_url("movie", 501), FakeResponse(200, _detail_payload()))
    )
    client = _client(store, session)
    monkeypatch.setattr(backfill, "_build_client", lambda db_path, args: client)

    args = backfill.build_arg_parser().parse_args([
        "--phase", "enrich", "--db", str(store.db_path), "--limit", "1", "--resume", "--concurrency", "1",
    ])
    first = backfill.run(args)
    assert first["processed"] == 1

    second = backfill.run(args)
    assert second["processed"] == 1
    processed_ids = {id_a, id_b}
    conn = store.connect(readonly=True)
    try:
        completed = {row[0] for row in conn.execute("SELECT id FROM media WHERE metadata_status='complete'").fetchall()}
    finally:
        conn.close()
    assert completed == processed_ids


def test_run_verify_with_ids_via_run(store, monkeypatch):
    rec = ls.MediaRecord(media_identity="title:verify:0:movie", media_type="movie", title_zh="校验剧", search_key="校验剧", match_status="unmatched")
    media_id = store.upsert_media(rec)
    session = FakeSession().route(tmdb.TMDB_BASE + "/search/movie", FakeResponse(200, {"results": []}))
    client = _client(store, session)
    monkeypatch.setattr(backfill, "_build_client", lambda db_path, args: client)

    args = backfill.build_arg_parser().parse_args([
        "--phase", "verify", "--db", str(store.db_path), "--ids", str(media_id),
    ])
    result = backfill.run(args)
    assert result["phase"] == "verify"
    assert result["stats"]["candidates_considered"] == 1


def test_run_verify_dry_run_is_a_preview(store, monkeypatch):
    def _boom(db_path, args):
        raise AssertionError("verify --dry-run must not build a client")

    monkeypatch.setattr(backfill, "_build_client", _boom)
    args = backfill.build_arg_parser().parse_args([
        "--phase", "verify", "--db", str(store.db_path), "--dry-run",
    ])
    result = backfill.run(args)
    assert result["dry_run"] is True
