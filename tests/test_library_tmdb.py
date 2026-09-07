"""Unit tests for library_tmdb: rate limiting, daily budget, cache, retries and scoring.

All TMDB access goes through an injected fake session (see FakeSession below);
the shared ``tests/conftest.py`` network guard additionally blocks any
accidental real HTTP call made through ``requests`` directly.  Fake clocks and
sleeps make interval/backoff assertions deterministic and fast -- no test in
this file should take a meaningful amount of wall-clock time.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time as real_time

import pytest
import requests

import library_tmdb as tmdb


# --- helpers ------------------------------------------------------------------


def _conn_factory(db_path):
    def factory():
        return sqlite3.connect(str(db_path))

    return factory


class FakeClock:
    """A controllable monotonic clock paired with a matching fake sleep.

    ``sleep`` advances the same counter ``time`` reads, exactly like real
    time -- this matters when a RateLimiter and a retry backoff share this
    clock: a backoff sleep must be visible to the next pacing check, or the
    limiter would inject spurious extra waits between retries.
    """

    def __init__(self, start: float = 0.0):
        self._now = start
        self._lock = threading.Lock()
        self.sleep_calls: list[float] = []

    def time(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds
            self.sleep_calls.append(seconds)


# --- ensure_tables --------------------------------------------------------------


def test_ensure_tables_creates_cache_and_budget_tables(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    tmdb.ensure_tables(conn)
    tmdb.ensure_tables(conn)  # idempotent, must not raise
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tmdb_cache", "tmdb_budget", "tmdb_enricher_state", "tmdb_requeue_audit"} <= names
    conn.close()


def test_ensure_tables_creates_requeue_audit_table_with_expected_columns(tmp_path):
    # T14/§5.1: the safe-requeue audit trail -- media id + prior status/
    # reason + batch/time only, never a title or link. Fix wave 1: a
    # nullable processed_at, unset (NULL) until the row is actually judged.
    # Fix wave 2/finding #1: a nullable last_error_class, recording a TMDB
    # search failure without marking the row processed.
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    tmdb.ensure_tables(conn)
    conn.execute(
        "INSERT INTO tmdb_requeue_audit (media_id, prev_status, prev_review_reason, batch_id, queued_at) "
        "VALUES (1, 'needs_review', 'review_pending_unqueried', '2026-09-06', 1000)"
    )
    conn.commit()
    row = conn.execute(
        "SELECT media_id, prev_status, prev_review_reason, batch_id, queued_at, processed_at, last_error_class "
        "FROM tmdb_requeue_audit"
    ).fetchone()
    assert row == (1, "needs_review", "review_pending_unqueried", "2026-09-06", 1000, None, None)
    conn.close()


def test_ensure_tables_requeue_audit_has_unique_batch_media_constraint(tmp_path):
    # Fix wave 1/finding #3: INSERT OR IGNORE + UNIQUE(batch_id, media_id)
    # so a repeated per-row queue write (e.g. the same row re-selected by a
    # non-resumed rerun after an aborted batch) never duplicates a row.
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    tmdb.ensure_tables(conn)
    for _ in range(2):
        conn.execute(
            "INSERT OR IGNORE INTO tmdb_requeue_audit (media_id, prev_status, prev_review_reason, batch_id, queued_at) "
            "VALUES (1, 'needs_review', 'review_pending_unqueried', '2026-09-06', 1000)"
        )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM tmdb_requeue_audit").fetchone()[0]
    assert count == 1
    conn.close()


# --- effective_limits -----------------------------------------------------------


def test_effective_limits_defaults_with_no_overrides():
    assert tmdb.effective_limits() == tmdb.DEFAULT_LIMITS
    # T14/user instruction: ~25 req/s pacing (40ms), 3000/day default budget.
    assert tmdb.DEFAULT_LIMITS == tmdb.Limits(max_concurrency=2, min_interval_ms=40, daily_budget=3000)


def test_effective_limits_settings_and_environ_take_the_stricter_value():
    settings = {"tmdb_max_concurrency": 5, "tmdb_min_interval_ms": 200, "tmdb_daily_budget": 500}
    environ = {"TMDB_MAX_CONCURRENCY": "1", "TMDB_MIN_INTERVAL_MS": "900", "TMDB_DAILY_BUDGET": "100"}
    limits = tmdb.effective_limits(settings, environ)
    assert limits == tmdb.Limits(max_concurrency=1, min_interval_ms=900, daily_budget=100)


def test_effective_limits_settings_alone_can_be_stricter_than_default():
    limits = tmdb.effective_limits({"tmdb_max_concurrency": 1})
    assert limits.max_concurrency == 1
    assert limits.min_interval_ms == tmdb.DEFAULT_LIMITS.min_interval_ms
    assert limits.daily_budget == tmdb.DEFAULT_LIMITS.daily_budget


def test_effective_limits_ignores_invalid_values():
    settings = {"tmdb_max_concurrency": "not-a-number"}
    environ = {"TMDB_MIN_INTERVAL_MS": "also-invalid", "TMDB_DAILY_BUDGET": ""}
    limits = tmdb.effective_limits(settings, environ)
    assert limits == tmdb.DEFAULT_LIMITS


def test_effective_limits_rejects_out_of_range_values():
    # max_concurrency < 1, daily_budget < 1 and min_interval_ms < 0 are
    # nonsensical regardless of source -- treated the same as unparsable.
    settings = {"tmdb_max_concurrency": 0, "tmdb_daily_budget": 0, "tmdb_min_interval_ms": -1}
    environ = {"TMDB_MAX_CONCURRENCY": "0", "TMDB_DAILY_BUDGET": "-5", "TMDB_MIN_INTERVAL_MS": "-100"}
    limits = tmdb.effective_limits(settings, environ)
    assert limits == tmdb.DEFAULT_LIMITS


def test_effective_limits_daily_budget_setting_above_default_is_not_clamped_to_default():
    # Regression for the P0 bug: effective_limits() used to always include
    # DEFAULT_LIMITS.daily_budget as a candidate for min(), so a setting of
    # 800 with no env cap was silently cut back to the default.
    limits = tmdb.effective_limits({"tmdb_daily_budget": 800})
    assert limits.daily_budget == 800


# --- resolve_daily_budget ----------------------------------------------------------


def test_resolve_daily_budget_setting_alone():
    result = tmdb.resolve_daily_budget({"tmdb_daily_budget": 800}, {})
    assert result == tmdb.BudgetResolution(configured=800, effective=800, cap_source="setting")


def test_resolve_daily_budget_env_caps_a_higher_setting():
    result = tmdb.resolve_daily_budget({"tmdb_daily_budget": 800}, {"TMDB_DAILY_BUDGET": "300"})
    assert result == tmdb.BudgetResolution(configured=800, effective=300, cap_source="env")


def test_resolve_daily_budget_default_when_nothing_configured():
    result = tmdb.resolve_daily_budget(None, None)
    assert result == tmdb.BudgetResolution(configured=3000, effective=3000, cap_source="default")


def test_resolve_daily_budget_setting_lower_than_env_cap_keeps_setting_as_the_source():
    result = tmdb.resolve_daily_budget({"tmdb_daily_budget": 200}, {"TMDB_DAILY_BUDGET": "300"})
    assert result == tmdb.BudgetResolution(configured=200, effective=200, cap_source="setting")


def test_resolve_daily_budget_invalid_setting_is_treated_as_absent():
    result = tmdb.resolve_daily_budget({"tmdb_daily_budget": "abc"}, {})
    assert result == tmdb.BudgetResolution(configured=3000, effective=3000, cap_source="default")


def test_resolve_daily_budget_setting_out_of_range_is_treated_as_absent():
    result = tmdb.resolve_daily_budget({"tmdb_daily_budget": 5001}, {})
    assert result == tmdb.BudgetResolution(configured=3000, effective=3000, cap_source="default")


def test_resolve_daily_budget_env_cap_equal_to_setting_keeps_setting_as_the_source():
    result = tmdb.resolve_daily_budget({"tmdb_daily_budget": 300}, {"TMDB_DAILY_BUDGET": "300"})
    assert result == tmdb.BudgetResolution(configured=300, effective=300, cap_source="setting")


def test_resolve_daily_budget_env_cap_applies_even_with_no_setting():
    result = tmdb.resolve_daily_budget(None, {"TMDB_DAILY_BUDGET": "100"})
    assert result == tmdb.BudgetResolution(configured=3000, effective=100, cap_source="env")


def test_resolve_daily_budget_invalid_env_is_ignored():
    result = tmdb.resolve_daily_budget({"tmdb_daily_budget": 800}, {"TMDB_DAILY_BUDGET": "not-a-number"})
    assert result == tmdb.BudgetResolution(configured=800, effective=800, cap_source="setting")


# --- LockPaths ----------------------------------------------------------------------


def test_lock_paths_for_data_dir_returns_three_distinct_well_known_paths(tmp_path):
    paths = tmdb.LockPaths.for_data_dir(tmp_path)
    assert paths.leader == tmp_path / "tmdb-enricher-leader.lock"
    assert paths.run == tmp_path / "tmdb-enrich-run.lock"
    assert paths.budget == tmp_path / "tmdb-budget.lock"


def test_lock_paths_rejects_two_equal_members(tmp_path):
    with pytest.raises(ValueError):
        tmdb.LockPaths(leader=tmp_path / "a.lock", run=tmp_path / "a.lock", budget=tmp_path / "b.lock")


# --- RateLimiter ------------------------------------------------------------------


def test_rate_limiter_enforces_minimum_interval_between_dispatches():
    clock = FakeClock()
    limiter = tmdb.RateLimiter(500, clock=clock.time, sleep=clock.sleep)
    dispatch_times = []
    for _ in range(4):
        limiter.wait()
        dispatch_times.append(clock.time())
    gaps = [b - a for a, b in zip(dispatch_times, dispatch_times[1:])]
    assert all(gap >= 0.5 for gap in gaps)
    # Sequential calls with no real work between them should each pay exactly
    # one interval of (fake) sleep -- not more, not less.
    assert dispatch_times == [0.0, 0.5, 1.0, 1.5]


def test_rate_limiter_serialises_concurrent_waiters(request):
    clock = FakeClock()
    limiter = tmdb.RateLimiter(500, clock=clock.time, sleep=clock.sleep)
    dispatch_times: list[float] = []
    record_lock = threading.Lock()

    def worker():
        limiter.wait()
        with record_lock:
            dispatch_times.append(clock.time())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    dispatch_times.sort()
    gaps = [b - a for a, b in zip(dispatch_times, dispatch_times[1:])]
    assert len(dispatch_times) == 6
    assert all(gap >= 0.5 - 1e-9 for gap in gaps)


def test_rate_limiter_slow_down_raises_pacing_then_reverts_after_cooldown():
    # T14/user instruction: a 429 raises pacing to >= 250ms for a cooldown,
    # then reverts to the configured base interval on its own.
    clock = FakeClock()
    limiter = tmdb.RateLimiter(40, clock=clock.time, sleep=clock.sleep)
    limiter.wait()  # dispatch #1 at t=0

    limiter.slow_down(250, 1.0)
    limiter.wait()  # dispatch #2: must pay the raised 250ms interval
    assert clock.time() == pytest.approx(0.25)

    clock.sleep(1.0)  # let the 1.0s cooldown (started at t=0) elapse
    limiter.wait()  # first wait() after cooldown elapsed: gap is already
    # far bigger than any interval, so this dispatches immediately and
    # reverts the interval back to base as a side effect.
    before = clock.time()
    limiter.wait()  # dispatch: now paced at the reverted 40ms base interval
    assert clock.time() - before == pytest.approx(0.04)


def test_rate_limiter_slow_down_does_not_lower_an_already_stricter_interval():
    clock = FakeClock()
    limiter = tmdb.RateLimiter(500, clock=clock.time, sleep=clock.sleep)  # base already stricter than 250ms
    limiter.wait()
    limiter.slow_down(250, 1.0)
    before = clock.time()
    limiter.wait()
    assert clock.time() - before == pytest.approx(0.5)


def test_rate_limiter_slow_down_extends_rather_than_stacks_on_repeat_429s():
    clock = FakeClock()
    limiter = tmdb.RateLimiter(40, clock=clock.time, sleep=clock.sleep)

    limiter.slow_down(250, 1.0)  # at t=0, cooldown until t=1.0
    clock.sleep(0.9)  # t=0.9, still within the first cooldown
    limiter.slow_down(250, 1.0)  # extends the cooldown to t=1.9, not stacked on top

    clock.sleep(0.95)  # t=1.85: past the *first* cooldown deadline but not the extended one
    limiter.wait()  # first-ever wait(): dispatches immediately (no prior dispatch to pace against)
    before = clock.time()
    limiter.wait()  # still within the extended cooldown -> pays the raised interval, not the base one
    assert clock.time() - before == pytest.approx(0.25)


# --- Budget ---------------------------------------------------------------------


def test_budget_reserve_raises_once_daily_budget_is_used_up(tmp_path):
    factory = _conn_factory(tmp_path / "budget.db")
    budget = tmdb.Budget(factory, tmp_path / "budget.lock", daily_budget=3, today=lambda: "2026-09-04")
    budget.reserve()
    budget.reserve()
    budget.reserve()
    with pytest.raises(tmdb.BudgetExhausted):
        budget.reserve()
    assert budget.status() == {
        "day": "2026-09-04", "used": 3, "budget": 3, "remaining": 0, "reset_at": "2026-09-05T00:00:00+00:00",
    }


def test_budget_reserve_is_exact_under_8_concurrent_threads_each_with_own_instance(tmp_path):
    # F.3 (mandatory concurrency test): one shared budget lock file + one
    # SQLite DB, daily_budget=20, 8 threads each with their OWN Budget
    # instance calling reserve() 10 times (80 attempts total) -> exactly 20
    # successes, 60 BudgetExhausted, and the DB row's used == 20. Proves
    # the flock around reserve()'s critical section serializes the
    # read-check-increment across every instance/thread, not just calls on
    # one shared Budget object.
    db_path = tmp_path / "concurrent.db"
    lock_path = tmp_path / "concurrent.lock"
    factory = _conn_factory(db_path)

    successes: list[int] = []
    exhausted: list[int] = []
    results_lock = threading.Lock()

    def worker() -> None:
        budget = tmdb.Budget(factory, lock_path, daily_budget=20, today=lambda: "2026-09-04")
        for _ in range(10):
            try:
                budget.reserve()
            except tmdb.BudgetExhausted:
                with results_lock:
                    exhausted.append(1)
            else:
                with results_lock:
                    successes.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 20
    assert len(exhausted) == 60

    conn = factory()
    row = conn.execute("SELECT used FROM tmdb_budget WHERE day = ?", ("2026-09-04",)).fetchone()
    conn.close()
    assert row[0] == 20


def test_budget_is_shared_across_instances_pointing_at_the_same_db_and_lock(tmp_path):
    factory = _conn_factory(tmp_path / "shared.db")
    lock_path = tmp_path / "shared.lock"
    budget_a = tmdb.Budget(factory, lock_path, daily_budget=2, today=lambda: "2026-09-04")
    budget_b = tmdb.Budget(factory, lock_path, daily_budget=2, today=lambda: "2026-09-04")
    budget_a.reserve()
    budget_b.reserve()
    with pytest.raises(tmdb.BudgetExhausted):
        budget_a.reserve()
    with pytest.raises(tmdb.BudgetExhausted):
        budget_b.reserve()


def test_budget_resets_for_a_new_day(tmp_path):
    factory = _conn_factory(tmp_path / "day.db")
    today = {"value": "2026-09-04"}
    budget = tmdb.Budget(factory, tmp_path / "day.lock", daily_budget=1, today=lambda: today["value"])
    budget.reserve()
    with pytest.raises(tmdb.BudgetExhausted):
        budget.reserve()
    today["value"] = "2026-09-05"
    budget.reserve()  # fresh allowance on the new UTC day


def test_budget_reserve_reconciles_stored_budget_to_the_current_setting(tmp_path):
    # Regression for the P0 fix: a settings-page budget change must take
    # effect on a *different* process's very next reserve(), not just
    # after that process's own explicit sync().
    factory = _conn_factory(tmp_path / "reconcile.db")
    lock_path = tmp_path / "reconcile.lock"
    old = tmdb.Budget(factory, lock_path, daily_budget=300, today=lambda: "2026-09-04")
    old.reserve()
    old.reserve()

    new = tmdb.Budget(factory, lock_path, daily_budget=800, today=lambda: "2026-09-04")
    new.reserve()
    assert new.status()["budget"] == 800
    assert new.status()["used"] == 3


def test_budget_sync_updates_stored_budget_and_keeps_used(tmp_path):
    factory = _conn_factory(tmp_path / "sync.db")
    lock_path = tmp_path / "sync.lock"
    seed = tmdb.Budget(factory, lock_path, daily_budget=300, today=lambda: "2026-09-04")
    for _ in range(50):
        seed.reserve()

    seed.sync(800)
    status = seed.status()
    assert status["used"] == 50
    assert status["budget"] == 800
    assert status["remaining"] == 750

    seed.sync(30)
    status = seed.status()
    assert status["used"] == 50
    assert status["budget"] == 30
    assert status["remaining"] == 0


def test_budget_sync_without_a_row_for_today_is_a_noop(tmp_path):
    # Review fix #4: _sync_locked must perform no write at all -- not even
    # an implicit ensure_tables() schema creation -- when there is no row
    # for today (table missing or just no row yet). ``watch_conn`` is kept
    # open across ``sync()`` so its ``PRAGMA data_version`` reading reflects
    # any write made by *any other* connection (including sync()'s own
    # short-lived ones) since this test started watching -- a data_version
    # comparison across two independently opened-and-closed connections
    # would not catch this (each fresh connection's first read is always
    # relative to its own start, not the file's history).
    db_path = tmp_path / "sync-empty.db"
    factory = _conn_factory(db_path)
    watch_conn = factory()  # only creates the (table-less) file itself
    data_version_before = watch_conn.execute("PRAGMA data_version").fetchone()[0]

    lock_path = tmp_path / "sync-empty.lock"
    budget = tmdb.Budget(factory, lock_path, daily_budget=300, today=lambda: "2026-09-04")
    budget.sync(800)  # no row for today yet (no table at all) -> nothing to update

    data_version_after = watch_conn.execute("PRAGMA data_version").fetchone()[0]
    watch_conn.close()
    assert data_version_after == data_version_before

    conn = factory()
    tmdb.ensure_tables(conn)
    row = conn.execute("SELECT * FROM tmdb_budget WHERE day = ?", ("2026-09-04",)).fetchone()
    conn.close()
    assert row is None


def test_budget_status_on_missing_table_reports_defaults_and_writes_nothing(tmp_path):
    db_path = tmp_path / "no-tables.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE placeholder (id INTEGER)")
    conn.commit()
    data_version_before = conn.execute("PRAGMA data_version").fetchone()[0]
    conn.close()

    budget = tmdb.Budget(_conn_factory(db_path), tmp_path / "no-tables.lock", daily_budget=555, today=lambda: "2026-09-04")
    status = budget.status()
    assert status == {
        "day": "2026-09-04", "used": 0, "budget": 555, "remaining": 555, "reset_at": "2026-09-05T00:00:00+00:00",
    }

    conn = sqlite3.connect(str(db_path))
    data_version_after = conn.execute("PRAGMA data_version").fetchone()[0]
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert data_version_after == data_version_before
    assert "tmdb_budget" not in names


def test_budget_status_reset_at_is_next_utc_midnight(tmp_path):
    factory = _conn_factory(tmp_path / "reset-at.db")
    budget = tmdb.Budget(factory, tmp_path / "reset-at.lock", daily_budget=10, today=lambda: "2026-12-31")
    assert budget.status()["reset_at"] == "2027-01-01T00:00:00+00:00"


# --- derive_worker_state -----------------------------------------------------------
#
# Review fix #5: precedence is not_started > paused > running/stale. A
# missing heartbeat means the enricher has simply never run yet -- that is
# a distinct fact from whether it is currently *allowed* to run, so it
# wins outright regardless of enabled/key_configured/installed. Only once
# a heartbeat exists does "paused" take priority over its freshness.


def _worker_state_kwargs(**overrides):
    kwargs = dict(now=1000.0, enabled=True, key_configured=True, installed=True, idle_seconds=60)
    kwargs.update(overrides)
    return kwargs


def test_derive_worker_state_no_heartbeat_is_not_started_even_when_fully_disabled():
    state = {}
    result = tmdb.derive_worker_state(
        state, **_worker_state_kwargs(enabled=False, key_configured=False, installed=False)
    )
    assert result == "not_started"


def test_derive_worker_state_no_heartbeat_is_not_started_even_when_fully_enabled():
    state = {}
    result = tmdb.derive_worker_state(state, **_worker_state_kwargs())
    assert result == "not_started"


def test_derive_worker_state_paused_when_heartbeat_exists_but_not_enabled():
    state = {"heartbeat_at": "1000"}
    result = tmdb.derive_worker_state(state, **_worker_state_kwargs(enabled=False))
    assert result == "paused"


def test_derive_worker_state_paused_when_heartbeat_exists_but_key_missing():
    state = {"heartbeat_at": "1000"}
    result = tmdb.derive_worker_state(state, **_worker_state_kwargs(key_configured=False))
    assert result == "paused"


def test_derive_worker_state_paused_when_heartbeat_exists_but_not_installed():
    state = {"heartbeat_at": "1000"}
    result = tmdb.derive_worker_state(state, **_worker_state_kwargs(installed=False))
    assert result == "paused"


def test_derive_worker_state_running_when_heartbeat_is_fresh_and_fully_enabled():
    state = {"heartbeat_at": "1000"}
    result = tmdb.derive_worker_state(state, **_worker_state_kwargs(now=1000 + 3 * 60))
    assert result == "running"


def test_derive_worker_state_stale_when_heartbeat_is_older_than_3x_idle_seconds():
    state = {"heartbeat_at": "1000"}
    result = tmdb.derive_worker_state(state, **_worker_state_kwargs(now=1000 + 3 * 60 + 1))
    assert result == "stale"


# --- TmdbCache --------------------------------------------------------------------


def test_cache_round_trips_an_entry(tmp_path):
    factory = _conn_factory(tmp_path / "cache.db")
    cache = tmdb.TmdbCache(factory)
    assert cache.get("search:movie:zh-CN:example:0") is None
    entry = tmdb.CacheEntry(
        cache_key="search:movie:zh-CN:example:0",
        status="ok",
        payload=[{"id": 603, "title": "黑客帝国"}],
        tmdb_id=None,
        media_type=None,
        fetched_at=1000,
        retry_after=None,
        error_class=None,
    )
    cache.put(entry)
    fetched = cache.get(entry.cache_key)
    assert fetched == entry
    assert cache.counts() == {"ok": 1}


def test_cache_put_overwrites_existing_entry(tmp_path):
    factory = _conn_factory(tmp_path / "cache2.db")
    cache = tmdb.TmdbCache(factory)
    key = "search:movie:zh-CN:example:0"
    cache.put(tmdb.CacheEntry(key, "failed_retryable", None, None, None, 1000, 1100, "HTTP503"))
    cache.put(tmdb.CacheEntry(key, "ok", [{"id": 1}], None, None, 2000, None, None))
    fetched = cache.get(key)
    assert fetched.status == "ok"
    assert fetched.payload == [{"id": 1}]


def test_search_cache_key_format():
    assert tmdb.search_cache_key("multi", "zh-CN", "example", None) == "search:multi:zh-CN:example:0"
    assert tmdb.search_cache_key("movie", "zh-CN", "example", 1999) == "search:movie:zh-CN:example:1999"


# --- fakes for TmdbClient tests --------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    """A programmable, thread-safe fake ``requests``-shaped session.

    ``route(url, outcome)`` registers either a single FakeResponse/Exception
    (reused for every call) or a list of them (consumed one per call, in
    call order).  Every call is recorded, and ``hold_seconds`` (if set) makes
    each call sleep briefly for *real* wall-clock time so concurrent callers
    can be observed overlapping -- independent of the fake clock used for
    rate-limit pacing and retry backoff.
    """

    def __init__(self):
        self._routes: dict[str, object] = {}
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
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
        with self._lock:
            self.calls.append({"url": url, "params": dict(params or {})})
            outcome = self._pop_outcome(url)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.hold_seconds:
                real_time.sleep(self.hold_seconds)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            with self._lock:
                self.in_flight -= 1


def _client(tmp_path, session, *, limits=None, today=None, api_key="tmdb-key-for-tests", **extra):
    clock = FakeClock()
    factory = _conn_factory(tmp_path / "client.db")
    kwargs = dict(
        conn_factory=factory,
        lock_path=tmp_path / "client.lock",
        session=session,
        clock=clock.time,
        sleep=clock.sleep,
    )
    if limits is not None:
        kwargs["limits"] = limits
    if today is not None:
        kwargs["today"] = today
    kwargs.update(extra)
    return tmdb.TmdbClient(api_key, **kwargs), clock, factory


SEARCH_MOVIE_URL = tmdb.TMDB_BASE + "/search/movie"
SEARCH_TV_URL = tmdb.TMDB_BASE + "/search/tv"
GENRE_MOVIE_URL = tmdb.TMDB_BASE + "/genre/movie/list"


# --- TmdbClient.search: happy path + cache --------------------------------------


def test_search_success_is_cached_and_not_refetched(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": [{"id": 603, "title": "黑客帝国"}]}))
    client, _, _ = _client(tmp_path, session)
    first = client.search("movie", "黑客帝国", year=1999)
    assert first.status == "ok"
    assert first.payload == [{"id": 603, "title": "黑客帝国"}]
    second = client.search("movie", "黑客帝国", year=1999)
    assert second == first
    assert len(session.calls) == 1


def test_search_empty_results_are_cached_and_not_refetched(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": []}))
    client, _, _ = _client(tmp_path, session)
    first = client.search("movie", "nonexistent movie xyz")
    assert first.status == "empty"
    second = client.search("movie", "nonexistent movie xyz")
    assert second.status == "empty"
    assert len(session.calls) == 1


def test_search_caps_payload_to_first_ten_results(tmp_path):
    results = [{"id": i} for i in range(15)]
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": results}))
    client, _, _ = _client(tmp_path, session)
    entry = client.search("movie", "many results")
    assert len(entry.payload) == 10


def test_search_request_params_are_well_formed(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": []}))
    client, _, _ = _client(tmp_path, session, api_key="tmdb-key-for-tests")
    client.search("movie", "matrix", year=1999)
    (call,) = session.calls
    assert call["params"]["api_key"] == "tmdb-key-for-tests"
    assert call["params"]["query"] == "matrix"
    assert call["params"]["language"] == "zh-CN"
    # T15 fix wave 1 (item 3c): movie search must use primary_release_year,
    # not the bare (unsupported) "year" param.
    assert call["params"]["primary_release_year"] == 1999
    assert "year" not in call["params"]
    assert call["params"]["include_adult"] == "false"


def test_search_tv_uses_first_air_date_year(tmp_path):
    session = FakeSession().route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    client, _, _ = _client(tmp_path, session)
    client.search("tv", "breaking bad", year=2008)
    (call,) = session.calls
    assert call["params"]["first_air_date_year"] == 2008
    assert "year" not in call["params"]


# --- retry policy -------------------------------------------------------------


def test_429_retries_after_retry_after_header_then_succeeds(tmp_path):
    session = FakeSession().route(
        SEARCH_MOVIE_URL,
        [FakeResponse(429, headers={"Retry-After": "2"}), FakeResponse(200, {"results": [{"id": 1}]})],
    )
    client, clock, _ = _client(tmp_path, session)
    entry = client.search("movie", "rate limited")
    assert entry.status == "ok"
    assert len(session.calls) == 2
    assert clock.sleep_calls == [2]
    # Fix wave 1/finding #1: one 429 response then success -> count 1.
    assert client.http_429_responses == 1


def test_429_triggers_adaptive_slowdown_for_a_later_unrelated_request(tmp_path):
    # T14/user instruction: a 429 must slow down every subsequent dispatch
    # through the client's shared RateLimiter, not just the one retry.
    session = FakeSession().route(
        SEARCH_MOVIE_URL,
        [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"results": []})],
    )
    session.route(SEARCH_TV_URL, FakeResponse(200, {"results": []}))
    limits = tmdb.Limits(max_concurrency=1, min_interval_ms=40, daily_budget=100)
    client, clock, _ = _client(tmp_path, session, limits=limits)

    client.search("movie", "rate limited")  # one 429, then succeeds
    clock.sleep_calls.clear()

    client.search("tv", "a different query")  # a fresh cache key -> a real dispatch, paced by the limiter

    assert clock.sleep_calls and max(clock.sleep_calls) >= 0.25


def test_429_four_times_becomes_failed_retryable(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, [FakeResponse(429, headers={"Retry-After": "1"}) for _ in range(4)])
    client, clock, _ = _client(tmp_path, session)
    entry = client.search("movie", "always limited")
    assert entry.status == "failed_retryable"
    assert entry.retry_after is not None and entry.retry_after > int(real_time.time())
    assert len(session.calls) == 4
    # Fix wave 1/finding #1: four real 429 responses -> count 4, not 1 (the
    # old behaviour counted only the retry-exhausted search itself).
    assert client.http_429_responses == 4
    assert clock.sleep_calls == [1, 1, 1]


def test_5xx_backs_off_1_2_4_seconds_then_fails_retryable(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, [FakeResponse(503) for _ in range(4)])
    client, clock, _ = _client(tmp_path, session)
    entry = client.search("movie", "flaky upstream")
    assert entry.status == "failed_retryable"
    assert clock.sleep_calls == [1, 2, 4]
    assert len(session.calls) == 4


def test_connection_error_uses_the_same_backoff_as_5xx(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, [requests.ConnectionError("boom") for _ in range(4)])
    client, clock, _ = _client(tmp_path, session)
    entry = client.search("movie", "network trouble")
    assert entry.status == "failed_retryable"
    assert entry.error_class == "ConnectionError"
    assert clock.sleep_calls == [1, 2, 4]


# --- T10 fix wave 1: request_timeout / max_retries constructor options ---------


def test_max_retries_zero_performs_exactly_one_attempt_and_no_backoff(tmp_path):
    # Same failure script as test_connection_error_uses_the_same_backoff_as_5xx
    # (4 routed ConnectionErrors), but with max_retries=0 -- the "fast"
    # client the settings-page routes now use (T10 fix wave 1) must give up
    # after the very first attempt instead of retrying up to 3 more times
    # with 1s/2s/4s backoff sleeps in between.
    session = FakeSession().route(SEARCH_MOVIE_URL, [requests.ConnectionError("boom") for _ in range(4)])
    client, clock, _ = _client(tmp_path, session, max_retries=0)
    entry = client.search("movie", "no retries allowed")
    assert entry.status == "failed_retryable"
    assert entry.error_class == "ConnectionError"
    assert len(session.calls) == 1
    assert clock.sleep_calls == []


def test_request_timeout_option_is_forwarded_to_session_get(tmp_path):
    captured = {}

    class _TimeoutCapturingSession:
        def get(self, url, params=None, timeout=None):
            captured["timeout"] = timeout
            return FakeResponse(200, {"results": []})

    client, _, _ = _client(tmp_path, _TimeoutCapturingSession(), request_timeout=6.0)
    client.search("movie", "fast timeout probe")
    assert captured["timeout"] == 6.0


def test_404_is_failed_permanent_and_only_requested_once(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(404))
    client, _, _ = _client(tmp_path, session)
    entry = client.search("movie", "does not exist route")
    assert entry.status == "failed_permanent"
    assert entry.error_class == "HTTP404"
    assert len(session.calls) == 1


def test_401_raises_invalid_api_key(tmp_path, caplog):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(401))
    client, _, _ = _client(tmp_path, session, api_key="tmdb-key-for-tests")
    with caplog.at_level("WARNING"):
        with pytest.raises(tmdb.InvalidApiKey):
            client.search("movie", "bad key")
    assert "tmdb-key-for-tests" not in caplog.text


def test_403_also_raises_invalid_api_key(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(403))
    client, _, _ = _client(tmp_path, session)
    with pytest.raises(tmdb.InvalidApiKey):
        client.search("movie", "forbidden")


def test_failed_retryable_cache_entry_blocks_requests_until_retry_after(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": [{"id": 42}]}))
    client, clock, factory = _client(tmp_path, session)
    cache = tmdb.TmdbCache(factory)
    key = tmdb.search_cache_key("movie", "zh-CN", tmdb._fold("still cooling down"), None)
    cache.put(tmdb.CacheEntry(key, "failed_retryable", None, None, None, int(real_time.time()), int(real_time.time()) + 1000, "HTTP503"))

    blocked = client.search("movie", "still cooling down")
    assert blocked.status == "failed_retryable"
    assert session.calls == []

    cache.put(tmdb.CacheEntry(key, "failed_retryable", None, None, None, int(real_time.time()), int(real_time.time()) - 10, "HTTP503"))
    retried = client.search("movie", "still cooling down")
    assert retried.status == "ok"
    assert len(session.calls) == 1


def test_reserve_is_called_before_every_attempt_so_retries_count_against_budget(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, [FakeResponse(429, headers={"Retry-After": "1"}) for _ in range(4)])
    limits = tmdb.Limits(max_concurrency=1, min_interval_ms=0, daily_budget=2)
    client, _, _ = _client(tmp_path, session, limits=limits)
    with pytest.raises(tmdb.BudgetExhausted):
        client.search("movie", "expensive retries")
    assert len(session.calls) == 2  # the 2-request daily budget ran out before a 3rd attempt
    assert client.budget_status()["used"] == 2


# --- concurrency (through the real client + ThreadPoolExecutor) -----------------


def test_map_search_caps_concurrent_in_flight_requests_at_max_concurrency(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": []}))
    session.hold_seconds = 0.03
    limits = tmdb.Limits(max_concurrency=2, min_interval_ms=0, daily_budget=100)
    client, _, _ = _client(tmp_path, session, limits=limits)
    queries = [("movie", f"query {i}", None) for i in range(6)]

    results = client.map_search(queries)

    assert len(results) == 6
    assert all(entry.status == "empty" for entry in results)
    assert session.max_in_flight <= 2
    assert session.max_in_flight >= 2  # proves genuine overlap happened, not accidental serialisation


def test_map_search_cancels_queued_searches_on_invalid_api_key(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(401))
    session.hold_seconds = 0.03
    limits = tmdb.Limits(max_concurrency=2, min_interval_ms=0, daily_budget=100)
    client, _, _ = _client(tmp_path, session, limits=limits)
    queries = [("movie", f"query {i}", None) for i in range(10)]

    with pytest.raises(tmdb.InvalidApiKey):
        client.map_search(queries)

    # Only the requests already dispatched (at most max_concurrency) should
    # have hit the fake session -- the rest of the queue must be cancelled,
    # not spent on a key we already know is doomed.
    assert len(session.calls) <= limits.max_concurrency


# --- genres -----------------------------------------------------------------------


def test_genres_fetches_and_caches(tmp_path):
    session = FakeSession().route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": [{"id": 28, "name": "动作"}, {"id": 12, "name": "冒险"}]}))
    client, _, _ = _client(tmp_path, session)
    first = client.genres("movie")
    assert first == {28: "动作", 12: "冒险"}
    assert len(session.calls) == 1
    second = client.genres("movie")
    assert second == first
    assert len(session.calls) == 1  # cache hit, no second request


def test_genres_cache_expires_after_30_days(tmp_path):
    session = FakeSession().route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": [{"id": 28, "name": "动作"}]}))
    client, _, factory = _client(tmp_path, session)
    cache = tmdb.TmdbCache(factory)
    stale_fetched_at = int(real_time.time()) - 31 * 24 * 3600
    cache.put(tmdb.CacheEntry("genres:movie:zh-CN", "ok", [{"id": 1, "name": "旧数据"}], None, None, stale_fetched_at, None, None))
    fresh = client.genres("movie")
    assert fresh == {28: "动作"}
    assert len(session.calls) == 1


def test_genres_cached_failed_retryable_blocks_requests_until_retry_after(tmp_path):
    session = FakeSession().route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": [{"id": 28, "name": "动作"}]}))
    client, _, factory = _client(tmp_path, session)
    cache = tmdb.TmdbCache(factory)
    key = "genres:movie:zh-CN"
    cache.put(tmdb.CacheEntry(key, "failed_retryable", None, None, None, int(real_time.time()), int(real_time.time()) + 1000, "HTTP503"))

    blocked = client.genres("movie")
    assert blocked == {}
    assert session.calls == []

    cache.put(tmdb.CacheEntry(key, "failed_retryable", None, None, None, int(real_time.time()), int(real_time.time()) - 10, "HTTP503"))
    retried = client.genres("movie")
    assert retried == {28: "动作"}
    assert len(session.calls) == 1


def test_genres_cached_failed_permanent_never_refetches(tmp_path):
    session = FakeSession().route(GENRE_MOVIE_URL, FakeResponse(200, {"genres": [{"id": 28, "name": "动作"}]}))
    client, _, factory = _client(tmp_path, session)
    cache = tmdb.TmdbCache(factory)
    key = "genres:movie:zh-CN"
    cache.put(tmdb.CacheEntry(key, "failed_permanent", None, None, None, int(real_time.time()), None, "HTTP404"))

    result = client.genres("movie")
    assert result == {}
    assert session.calls == []


# --- budget_status ------------------------------------------------------------------


def test_budget_status_reports_usage(tmp_path):
    session = FakeSession().route(SEARCH_MOVIE_URL, FakeResponse(200, {"results": []}))
    limits = tmdb.Limits(max_concurrency=2, min_interval_ms=0, daily_budget=5)
    client, _, _ = _client(tmp_path, session, limits=limits, today=lambda: "2026-09-04")
    client.search("movie", "one")
    assert client.budget_status() == {
        "day": "2026-09-04", "used": 1, "budget": 5, "remaining": 4, "reset_at": "2026-09-05T00:00:00+00:00",
    }


# --- logging never leaks the key --------------------------------------------------


def test_logs_never_contain_the_api_key_or_a_query_string(tmp_path, caplog):
    session = FakeSession().route(
        SEARCH_MOVIE_URL,
        [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, {"results": [{"id": 1}]})],
    )
    client, _, _ = _client(tmp_path, session, api_key="tmdb-key-for-tests")
    with caplog.at_level("INFO"):
        client.search("movie", "logged safely")
    assert "tmdb-key-for-tests" not in caplog.text
    assert "api_key" not in caplog.text
    assert "?" not in caplog.text


# --- score_candidate / judge --------------------------------------------------


def test_score_candidate_chinese_title_and_year_exact_match_is_exact():
    result = {
        "id": 603, "media_type": "movie", "title": "黑客帝国", "original_title": "The Matrix",
        "release_date": "1999-03-31", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
        "overview": "...", "genre_ids": [28, 878],
    }
    judgement = tmdb.judge([result], title_zh="黑客帝国", aliases=[], year=1999, inferred_type="movie")
    assert judgement.status == "exact"
    assert judgement.tmdb_id == 603
    assert judgement.media_type == "movie"
    assert judgement.score == 1.0
    (candidate,) = judgement.candidates
    assert candidate.year == 1999
    assert candidate.poster_path == "/p.jpg"
    assert candidate.backdrop_path == "/b.jpg"
    assert candidate.genre_ids == (28, 878)
    assert candidate.original_title == "The Matrix"


def test_score_candidate_matches_on_original_title_alone():
    # title_zh only equals the *original* (non-localised) title field, not
    # the displayed title/name -- must still score full title credit.
    result = {"id": 42, "media_type": "movie", "title": "某本地化标题", "original_title": "Some English Title"}
    judgement = tmdb.judge([result], title_zh="Some English Title", aliases=[], year=None, inferred_type="movie")
    assert judgement.status == "exact"
    assert judgement.candidates[0].score == 1.0


def test_score_candidate_year_off_by_two_is_penalized_into_needs_review():
    result = {"id": 7, "media_type": "movie", "title": "同名电影", "release_date": "2001-01-01"}
    judgement = tmdb.judge([result], title_zh="同名电影", aliases=[], year=1999, inferred_type="movie")
    assert judgement.candidates[0].score == pytest.approx(0.70)
    assert judgement.status == "needs_review"
    assert judgement.tmdb_id is None


def test_score_candidate_year_off_by_one_gets_a_small_bonus():
    result = {"id": 8, "media_type": "movie", "title": "同名电影二", "release_date": "2000-01-01"}
    judgement = tmdb.judge([result], title_zh="同名电影二", aliases=[], year=1999, inferred_type="movie")
    assert judgement.candidates[0].score == pytest.approx(1.0)  # 1.0 title + 0.05 year, clipped to 1.0


def test_score_candidate_tv_inferred_but_result_is_movie_is_penalized():
    result = {"id": 9, "media_type": "movie", "title": "剧集变电影", "release_date": "1999-01-01"}
    judgement = tmdb.judge([result], title_zh="剧集变电影", aliases=[], year=1999, inferred_type="tv")
    # 1.0 title + 0.15 year - 0.50 type-mismatch = 0.65
    assert judgement.candidates[0].score == pytest.approx(0.65)
    assert judgement.status == "needs_review"


def test_judge_needs_review_keeps_only_the_top_five_candidates():
    title_zh = "无间道风云"
    titles = ["无间道传说", "风云道无间", "江湖无间路", "无间之道行", "风云无间传", "道无间风云传", "无间传风云道", "天下无间风"]
    results = [{"id": index, "media_type": "movie", "title": title} for index, title in enumerate(titles)]
    judgement = tmdb.judge(results, title_zh=title_zh, aliases=[], year=None, inferred_type="movie")
    assert judgement.status == "needs_review"
    assert len(judgement.candidates) == 5
    scores = [c.score for c in judgement.candidates]
    assert scores == sorted(scores, reverse=True)
    assert judgement.score == scores[0]


def test_score_candidate_drops_person_results():
    person = {"id": 100, "media_type": "person", "name": "某演员"}
    assert tmdb.score_candidate(person, title_zh="某演员", aliases=[], year=None, inferred_type="unknown") is None

    movie = {"id": 603, "media_type": "movie", "title": "黑客帝国", "release_date": "1999-03-31"}
    judgement = tmdb.judge([person, movie], title_zh="黑客帝国", aliases=[], year=1999, inferred_type="movie")
    assert judgement.status == "exact"
    assert len(judgement.candidates) == 1
    assert judgement.candidates[0].tmdb_id == 603


def test_judge_with_no_results_is_unmatched():
    judgement = tmdb.judge([], title_zh="不存在的电影", aliases=[], year=None, inferred_type="unknown")
    assert judgement.status == "unmatched"
    assert judgement.tmdb_id is None
    assert judgement.media_type is None
    assert judgement.candidates == ()


def test_judge_candidate_tier_when_score_is_high_but_not_exact():
    # 0.8 (contains-match) title score, no year data -> 0.8: below the 0.90
    # exact bar but at/above the 0.75 candidate bar.
    result = {"id": 55, "media_type": "movie", "title": "长安的荔枝传"}
    judgement = tmdb.judge([result], title_zh="长安的荔枝", aliases=[], year=None, inferred_type="movie")
    assert judgement.candidates[0].score == pytest.approx(0.8)
    assert judgement.status == "candidate"
    assert judgement.tmdb_id == 55


# --- T15 fix wave 1: ratings merge/status helpers (item 3a/3b) -----------------


def test_merge_ratings_json_preserves_other_keys():
    existing = tmdb.merge_ratings_json(
        '{"imdb": {"score": 8.0, "scale": 10, "votes": 100, "as_of": "2026-01-01"}}',
        {"tmdb": {"score": 7.5, "scale": 10, "votes": 200, "as_of": "2026-02-01"}},
    )
    merged = json.loads(existing)
    assert merged["imdb"]["score"] == 8.0  # untouched
    assert merged["tmdb"]["score"] == 7.5  # newly merged in


def test_merge_ratings_json_overwrites_only_the_given_key():
    existing = tmdb.merge_ratings_json(
        '{"tmdb": {"score": 7.0, "scale": 10, "votes": 10, "as_of": "2026-01-01"}}',
        {"tmdb": {"score": 8.0, "scale": 10, "votes": 20, "as_of": "2026-03-01"}},
    )
    assert json.loads(existing) == {"tmdb": {"score": 8.0, "scale": 10, "votes": 20, "as_of": "2026-03-01"}}


def test_merge_ratings_json_tolerates_missing_or_invalid_existing():
    assert json.loads(tmdb.merge_ratings_json(None, {"tmdb": {"score": 1}})) == {"tmdb": {"score": 1}}
    assert json.loads(tmdb.merge_ratings_json("not json", {"tmdb": {"score": 1}})) == {"tmdb": {"score": 1}}


def test_ratings_status_for_none_when_nothing_present():
    status = tmdb.ratings_status_for({}, imdb_id=None, imdb_row_present=False, tvmaze_id=None)
    assert status == "none"


def test_ratings_status_for_complete_when_only_tmdb_applicable_and_present():
    ratings = {"tmdb": {"score": 8.0}}
    status = tmdb.ratings_status_for(ratings, imdb_id=None, imdb_row_present=False, tvmaze_id=None)
    assert status == "complete"


def test_ratings_status_for_imdb_not_applicable_without_a_local_ratings_row():
    # A known imdb_id with no matching imdb_ratings row is not "missing" --
    # it's simply not applicable, so tmdb-only is still "complete".
    ratings = {"tmdb": {"score": 8.0}}
    status = tmdb.ratings_status_for(ratings, imdb_id="tt0000001", imdb_row_present=False, tvmaze_id=None)
    assert status == "complete"


def test_ratings_status_for_partial_when_some_applicable_sources_missing():
    ratings = {"tmdb": {"score": 8.0}}
    status = tmdb.ratings_status_for(ratings, imdb_id="tt0000001", imdb_row_present=True, tvmaze_id=None)
    assert status == "partial"  # imdb is applicable (id known + row exists) but absent from ratings


def test_ratings_status_for_complete_when_every_applicable_source_present():
    ratings = {"tmdb": {"score": 8.0}, "imdb": {"score": 8.4}, "tvmaze": {"score": 7.0}}
    status = tmdb.ratings_status_for(ratings, imdb_id="tt0000001", imdb_row_present=True, tvmaze_id=42)
    assert status == "complete"


def test_ratings_status_for_tvmaze_not_applicable_without_a_rating_value():
    # Fix wave 2 (item 3): mirrors the imdb rule -- a known tvmaze_id with
    # no actual rating value in `ratings` is not applicable (never
    # "missing" forever, same as an imdb_id with no local ratings row).
    ratings = {"tmdb": {"score": 8.0}}
    status = tmdb.ratings_status_for(ratings, imdb_id=None, imdb_row_present=False, tvmaze_id=42)
    assert status == "complete"


def test_lookup_imdb_rating_returns_none_for_unknown_id(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE imdb_ratings (imdb_id TEXT PRIMARY KEY, rating REAL, votes INTEGER, as_of TEXT)")
    assert tmdb.lookup_imdb_rating(conn, "tt-missing") is None
    conn.close()


def test_lookup_imdb_rating_returns_none_when_table_is_missing(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    assert tmdb.lookup_imdb_rating(conn, "tt0000001") is None
    conn.close()


def test_lookup_imdb_rating_returns_the_row(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE imdb_ratings (imdb_id TEXT PRIMARY KEY, rating REAL, votes INTEGER, as_of TEXT)")
    conn.execute("INSERT INTO imdb_ratings VALUES ('tt0000001', 8.4, 50000, '2026-08-01')")
    conn.commit()
    assert tmdb.lookup_imdb_rating(conn, "tt0000001") == {"score": 8.4, "scale": 10, "votes": 50000, "as_of": "2026-08-01"}
    conn.close()
