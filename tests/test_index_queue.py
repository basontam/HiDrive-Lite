"""Bounded durable indexing; no upstream calls or production data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import sqlite3
import threading

import pytest

import library_search
import library_store
import library_tmdb
import re0_sync


@pytest.fixture
def store(tmp_path):
    store = library_store.LibraryStore(tmp_path / "library.db")
    store.create_schema()
    conn = store.connect()
    re0_sync.ensure_tables(conn)
    conn.commit()
    conn.close()
    return store


def seed(store, count=1):
    return [store.upsert_media(library_store.MediaRecord(
        media_identity=f"index-queue-fixture-{n}", media_type="movie",
        title_zh=f"测试影片{n}", search_key=f"测试影片{n}",
    )) for n in range(count)]


def state(store, key, value=None):
    conn = store.connect()
    try:
        if value is not None:
            re0_sync.state_set(conn, key, str(value), 1)
            conn.commit()
        return re0_sync.state_get(conn, key)
    finally:
        conn.close()


def count(store, pattern):
    conn = store.connect(readonly=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM re0_sync_state WHERE key GLOB ?", (pattern,)).fetchone()[0]
    finally:
        conn.close()


def test_pending_deduplicates_consumers_and_ack_is_atomic(store):
    mid = seed(store)[0]
    for _ in range(3):
        state(store, f"index_pending:{mid}", "1")
    assert count(store, "index_pending:*") == 1
    barrier = threading.Barrier(2)
    def run():
        barrier.wait()
        return library_search.build_index(store, media_ids=[mid], pending_only=True).term_count
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(value > 0 for value in results) == 1
    assert count(store, "index_pending:*") == 0
    # New work after a successful ACK remains queued and gets indexed.
    state(store, f"index_pending:{mid}", "1")
    assert re0_sync.rebuild_index_if_dirty(store, now=1000)
    assert state(store, f"index_pending:{mid}") is None


@pytest.mark.parametrize("status", ["exact", "candidate", "needs_review"])
def test_tmdb_writer_queues_existing_search_document(store, status):
    mid = seed(store)[0]
    library_search.build_index(store, media_ids=[mid])
    candidate = library_tmdb.Candidate(42, "movie", "测试影片0", "New Original", 2024,
        1.0, None, None, "New overview", ())
    judgement = library_tmdb.Judgement(status, 42, "movie", 1.0, (candidate,))
    if status == "exact":
        class Client:
            def genres(self, media_type):
                return {}
        library_tmdb._write_exact(store, Client(), mid, judgement, with_details=False)
    else:
        getattr(library_tmdb, "_write_" + status)(store, mid, judgement)
    assert state(store, f"index_pending:{mid}") == "1"
    assert re0_sync.rebuild_index_if_dirty(store, now=1000)
    assert state(store, f"index_pending:{mid}") is None


def test_weight_maintenance_preserves_alias_pinyin_but_pending_drops_removed_alias(store):
    mid = seed(store)[0]
    conn = store.connect()
    conn.execute("UPDATE media SET title_alt_json='[\"别名\"]' WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    library_search.build_index(store, media_ids=[mid], pinyin=lambda _: ("bieming", "bm"))
    state(store, f"index_reweight:{mid}", "1")
    library_search.build_index(store, media_ids=[mid], pending_only=True)
    conn = store.connect()
    assert "bieming" in conn.execute("SELECT alias_keys_json FROM search_doc WHERE media_id=?", (mid,)).fetchone()[0]
    conn.execute("UPDATE media SET title_alt_json='[]' WHERE id=?", (mid,))
    library_store.enqueue_index(conn, [mid], now=1)
    conn.commit()
    conn.close()
    library_search.build_index(store, media_ids=[mid], pending_only=True)
    conn = store.connect(readonly=True)
    assert "bieming" not in conn.execute("SELECT alias_keys_json FROM search_doc WHERE media_id=?", (mid,)).fetchone()[0]
    conn.close()


def test_exception_rolls_back_ack_and_background_backs_off(store, monkeypatch):
    mid = seed(store)[0]
    state(store, f"index_pending:{mid}", "1")
    original = library_search.tokenize
    monkeypatch.setattr(library_search, "tokenize", lambda *_: (_ for _ in ()).throw(RuntimeError("index-fixture")))
    with pytest.raises(RuntimeError):
        re0_sync.rebuild_index_if_dirty(store, now=1000)
    assert state(store, f"index_pending:{mid}") == "1"
    assert state(store, "index_last_error") == "RuntimeError"
    monkeypatch.setattr(library_search, "tokenize", original)
    assert not re0_sync.rebuild_index_if_dirty(store, now=1029)
    restarted = library_store.LibraryStore(store.db_path)
    assert re0_sync.rebuild_index_if_dirty(restarted, now=1030)
    assert state(store, f"index_pending:{mid}") is None


def test_legacy_flag_becomes_bounded_queue_not_full_rewrite(store, monkeypatch):
    seed(store, 25)
    state(store, "index_dirty", "1")
    original = library_search.build_index
    calls = []
    def bounded(*args, **kwargs):
        calls.append(kwargs["media_ids"])
        assert len(kwargs["media_ids"]) <= re0_sync.INDEX_BATCH_SIZE
        assert kwargs["pending_only"] is True
        return original(*args, **kwargs)
    monkeypatch.setattr(library_search, "build_index", bounded)
    assert re0_sync.rebuild_index_if_dirty(store, now=1000)
    assert count(store, "index_pending:*") == 15
    assert state(store, "index_dirty") == "0"
    assert re0_sync.rebuild_index_if_dirty(store, now=1005)
    assert re0_sync.rebuild_index_if_dirty(store, now=1010)
    assert not re0_sync.rebuild_index_if_dirty(store, now=1015)
    # An old worker can write another legacy flag during a rolling release.
    state(store, "index_dirty", "1")
    assert re0_sync.rebuild_index_if_dirty(store, now=1020)
    assert count(store, "index_pending:*") == 15


def test_missing_document_recovered_and_deleted_pending_removed(store):
    mid = seed(store)[0]
    state(store, "index_pending:999999", "1")
    assert re0_sync.rebuild_index_if_dirty(store, now=1000)
    assert count(store, "index_pending:*") == 0
    conn = store.connect(readonly=True)
    assert conn.execute("SELECT 1 FROM search_doc WHERE media_id=?", (mid,)).fetchone()
    conn.close()


def test_weekly_reweight_is_low_peak_bounded_and_pending_first(store):
    ids = seed(store, 25)
    library_search.build_index(store)
    day = int(datetime(2026, 9, 14, 12, tzinfo=re0_sync.SHANGHAI).timestamp())
    night = int(datetime(2026, 9, 15, 3, tzinfo=re0_sync.SHANGHAI).timestamp())
    state(store, "index_reweight_next_at", day - 1)
    assert not re0_sync.rebuild_index_if_dirty(store, now=day)
    assert re0_sync.rebuild_index_if_dirty(store, now=night)
    assert count(store, "index_reweight:*") == 15
    remaining_before = count(store, "index_reweight:*")
    # Pending work runs outside the low-peak window, calibration does not.
    state(store, f"index_pending:{ids[0]}", "1")
    assert re0_sync.rebuild_index_if_dirty(store, now=night + 10 * 3600)
    assert count(store, "index_reweight:*") <= remaining_before
    assert not re0_sync.rebuild_index_if_dirty(store, now=night + 10 * 3600 + 5)
    assert re0_sync.rebuild_index_if_dirty(store, now=night + 86400)
    assert re0_sync.rebuild_index_if_dirty(store, now=night + 86405)
    assert count(store, "index_reweight:*") == 0
    assert not re0_sync.rebuild_index_if_dirty(store, now=night + 86410)


@pytest.mark.parametrize("enabled", [False, True])
def test_local_index_round_runs_without_tmdb_client(store, tmp_path, enabled):
    mid = seed(store)[0]
    state(store, f"index_pending:{mid}", "1")
    worker = library_tmdb.BackgroundEnricher(
        lambda: store, lambda: None,
        leader_lock_path=tmp_path / "leader.lock", run_lock_path=tmp_path / "run.lock",
        conn_factory=lambda: None, enabled_check=lambda: enabled,
        local_round=lambda s: re0_sync.rebuild_index_if_dirty(s, now=1000),
    )
    waits = []
    def stop(seconds):
        waits.append(seconds)
        worker._stop_flag.set()
    worker._wait = stop
    worker._loop()
    assert waits == [5]
    assert state(store, f"index_pending:{mid}") is None
