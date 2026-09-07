"""TMDB client for the HiDrive-Lite media library: rate limiting, a persisted
daily request budget, a response cache, retry policy and candidate scoring.

This module only talks to TMDB through an injected ``session`` object (an
object exposing a ``requests``-shaped ``.get(url, params=..., timeout=...)``);
it never imports or calls ``requests.get`` at module scope, so tests can
supply a fake session and never touch the network.  ``enrich_batch`` (the
per-round search/score/write flow), ``merge_by_tmdb`` (identity upgrade and
duplicate-media merge) and ``BackgroundEnricher`` (the gunicorn-worker
background thread) are the T3.4-T3.5 production/offline-script entry points,
built on top of the client/cache/scoring machinery above.
"""

from __future__ import annotations

import difflib
import fcntl
import http.cookiejar
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from library_store import LibraryKeyUnavailable

if TYPE_CHECKING:
    from library_store import LibraryStore

LOG = logging.getLogger("HiDrive-Lite.tmdb")

TMDB_BASE = "https://api.themoviedb.org/3"
TVMAZE_BASE = "https://api.tvmaze.com"
# T15/z1-hints §3: TVmaze's own pacing, independent of the TMDB
# RateLimiter/Budget -- a free public API with no key and no app-level
# daily quota, just its own "must be >=500ms apart" politeness rule.
TVMAZE_MIN_INTERVAL_MS = 500

GENRE_CACHE_TTL_SECONDS = 30 * 24 * 3600
SEARCH_RESULT_CAP = 10
MAX_RETRIES = 3
FIVE_XX_BACKOFF = (1, 2, 4)
RETRY_AFTER_DEFAULT = 5
RETRY_AFTER_MAX = 60
RETRY_AFTER_COOLDOWN = 3600

# T14/user instruction: adaptive 429 slow-down -- a 429 raises the shared
# RateLimiter's pacing interval to at least this many ms for this many
# seconds (independent of the per-request Retry-After sleep), then it reverts
# to the configured base interval on its own.
ADAPTIVE_SLOWDOWN_MIN_INTERVAL_MS = 250
ADAPTIVE_SLOWDOWN_SECONDS = 60


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the ``tmdb_cache`` and ``tmdb_budget`` tables if missing.

    Matches the DDL in docs/architecture.md §3.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tmdb_cache (
          cache_key TEXT PRIMARY KEY,
          tmdb_id INTEGER, media_type TEXT, language TEXT,
          payload_json TEXT, status TEXT NOT NULL,
          fetched_at INTEGER NOT NULL, retry_after INTEGER, error_class TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tmdb_budget (
          day TEXT PRIMARY KEY, used INTEGER NOT NULL, budget INTEGER NOT NULL, updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tmdb_enricher_state (
          key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tmdb_requeue_audit (
          id INTEGER PRIMARY KEY,
          media_id INTEGER NOT NULL, prev_status TEXT NOT NULL, prev_review_reason TEXT,
          batch_id TEXT NOT NULL, queued_at INTEGER NOT NULL, processed_at INTEGER,
          last_error_class TEXT,
          UNIQUE(batch_id, media_id)
        )
        """
    )
    # T15 addendum §5.2: audit table for scripts/media_metadata_backfill.py.
    # ``query_key`` is a hash/short digest of the normalized query, never a
    # raw title/URL/Cookie -- see build_query_key() in that script.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_metadata_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          media_id INTEGER NOT NULL,
          phase TEXT NOT NULL,
          source TEXT NOT NULL,
          query_key TEXT NOT NULL,
          status TEXT NOT NULL,
          http_status INTEGER,
          error_class TEXT,
          response_cache_key TEXT,
          attempted_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_attempts_media ON media_metadata_attempts(media_id, attempted_at)"
    )
    # w6: link-validity checker tables. These are ALSO in library_store.py's
    # own SCHEMA_SQL (unlike every other table this function creates) --
    # library_store.py's own recount()/filters() query link_check directly
    # via live_link_sql(), so it can't rely on this integration-time hook
    # alone. Duplicated here (self-healing, like every table above) so this
    # module's own tests/CLI can build a bare connection and call only this
    # function, exactly like tmdb_cache/tmdb_budget.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS link_check (
          provider TEXT NOT NULL, canonical_url_hash TEXT NOT NULL, status TEXT NOT NULL,
          reason TEXT, http_class TEXT, checked_at INTEGER, next_check_at INTEGER,
          consecutive_unknown INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (provider, canonical_url_hash)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_link_check_due ON link_check(provider, next_check_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS link_check_state (
          key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER
        )
        """
    )
    conn.commit()


@dataclass(frozen=True)
class Limits:
    max_concurrency: int
    min_interval_ms: int
    daily_budget: int


# T14/user instruction: ~25 req/s global pacing (1000ms / 40ms) with
# concurrency 2, and a 3000/day default budget sized for the review-pending
# backlog (§4/§5.1 of docs/architecture.md;
# the chat instruction overrides that doc's 500ms/day-2000-3000 suggestion).
DEFAULT_LIMITS = Limits(2, 40, 3000)


@dataclass(frozen=True)
class LockPaths:
    """The three distinct fcntl lock files a production process needs for
    TMDB enrichment -- see the module-level docstring and the P0 fix this
    class exists for: reusing one lock file across the leader-election lock
    (held for a thread's whole lifetime), the manual/CLI-vs-background run
    lock and the per-request budget lock caused the background thread to
    deadlock against its own client's budget lock (an ``flock`` is per
    open-file-description, so a second open+lock of the *same path* in the
    same process blocks forever)."""

    leader: Path
    run: Path
    budget: Path

    def __post_init__(self) -> None:
        paths = (self.leader, self.run, self.budget)
        if len({str(p) for p in paths}) != len(paths):
            raise ValueError("LockPaths members must all be distinct paths")

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "LockPaths":
        data_dir = Path(data_dir)
        return cls(
            leader=data_dir / "tmdb-enricher-leader.lock",
            run=data_dir / "tmdb-enrich-run.lock",
            budget=data_dir / "tmdb-budget.lock",
        )


@dataclass(frozen=True)
class BudgetResolution:
    configured: int
    effective: int
    cap_source: str


def _parse_int(value, *, min_value: int | None = None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if min_value is not None and parsed < min_value:
        return None
    return parsed


def resolve_daily_budget(settings: Mapping | None, environ: Mapping[str, str] | None) -> BudgetResolution:
    """Resolve the daily TMDB request budget from the settings-page value
    and an optional environment cap (§B.1 of the P0 fix brief).

    ``configured`` is the valid ``tmdb_daily_budget`` setting (an int in
    1..5000) or ``DEFAULT_LIMITS.daily_budget`` (3000) when the setting is
    absent or fails to parse/validate. ``TMDB_DAILY_BUDGET`` in ``environ``
    is a *cap*, applied only when it is itself a valid int >= 1: the
    effective budget is ``min(configured, env_cap)``. ``cap_source`` is
    ``"env"`` when the env cap is set and strictly lower than
    ``configured``, else ``"setting"`` when a valid setting was given, else
    ``"default"``.
    """
    settings = settings or {}
    environ = environ or {}

    configured = _parse_int(settings.get("tmdb_daily_budget"), min_value=1)
    setting_is_valid = configured is not None and configured <= 5000
    if not setting_is_valid:
        configured = DEFAULT_LIMITS.daily_budget

    env_cap = _parse_int(environ.get("TMDB_DAILY_BUDGET"), min_value=1)

    if env_cap is not None and env_cap < configured:
        return BudgetResolution(configured=configured, effective=env_cap, cap_source="env")
    if setting_is_valid:
        return BudgetResolution(configured=configured, effective=configured, cap_source="setting")
    return BudgetResolution(configured=configured, effective=configured, cap_source="default")


def effective_limits(settings: dict | None = None, environ: Mapping[str, str] | None = None) -> Limits:
    """Combine defaults, settings-page values and environment overrides.

    ``settings`` keys: tmdb_daily_budget / tmdb_max_concurrency / tmdb_min_interval_ms.
    ``environ`` keys: the same names, upper-cased with a ``TMDB_`` prefix.
    ``max_concurrency``/``min_interval_ms``: whichever of the three
    (default, settings, environ) is strictest wins (min for concurrency,
    max for interval); values that fail to parse as ``int``, or are out of
    range (max_concurrency < 1, min_interval_ms < 0), are ignored.
    ``daily_budget`` is computed by ``resolve_daily_budget`` instead: the
    setting (if valid) replaces the default outright, and the environment
    value only ever *caps* it -- see that function's docstring.
    """
    settings = settings or {}
    environ = environ or {}

    def stricter(setting_key: str, env_key: str, default: int, combine, *, min_value: int):
        candidates = [default]
        parsed = _parse_int(settings.get(setting_key), min_value=min_value)
        if parsed is not None:
            candidates.append(parsed)
        parsed = _parse_int(environ.get(env_key), min_value=min_value)
        if parsed is not None:
            candidates.append(parsed)
        return combine(candidates)

    return Limits(
        max_concurrency=stricter(
            "tmdb_max_concurrency", "TMDB_MAX_CONCURRENCY", DEFAULT_LIMITS.max_concurrency, min, min_value=1
        ),
        min_interval_ms=stricter(
            "tmdb_min_interval_ms", "TMDB_MIN_INTERVAL_MS", DEFAULT_LIMITS.min_interval_ms, max, min_value=0
        ),
        daily_budget=resolve_daily_budget(settings, environ).effective,
    )


class RateLimiter:
    """Global pacing gate: at most one dispatch per ``min_interval_ms``,
    shared across every calling thread.

    ``slow_down`` (T14/user instruction) lets a caller that just saw a 429
    temporarily raise the pacing interval above the configured base value --
    an adaptive slow-down layered on top of the per-request ``Retry-After``
    sleep, which only delays the one retried request rather than every
    subsequent dispatch."""

    def __init__(self, min_interval_ms: int, *, clock=time.monotonic, sleep=time.sleep):
        self._base_interval = min_interval_ms / 1000.0
        self._interval = self._base_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_dispatch: float | None = None
        self._slowdown_until: float | None = None

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._slowdown_until is not None and now >= self._slowdown_until:
                self._interval = self._base_interval
                self._slowdown_until = None
            if self._last_dispatch is not None:
                remaining = self._interval - (now - self._last_dispatch)
                if remaining > 0:
                    self._sleep(remaining)
                    now = self._clock()
            self._last_dispatch = now

    def slow_down(self, min_interval_ms: float, cooldown_seconds: float) -> None:
        """Raise the pacing interval to at least ``min_interval_ms`` for the
        next ``cooldown_seconds`` (extending, not stacking, on a repeat
        call), then let ``wait()`` revert it to the configured base interval
        once the cooldown has elapsed."""
        with self._lock:
            interval = min_interval_ms / 1000.0
            if interval > self._interval:
                self._interval = interval
            self._slowdown_until = self._clock() + cooldown_seconds


class BudgetExhausted(RuntimeError):
    """Raised when the daily TMDB request budget has been used up."""


class InvalidApiKey(RuntimeError):
    """Raised when TMDB rejects the configured API key (401/403)."""


def _next_utc_midnight_iso(day: str) -> str:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (start + timedelta(days=1)).isoformat()


class Budget:
    """Cross-process daily request budget, backed by ``tmdb_budget`` and
    guarded by ``lock_path`` -- the *budget* lock (see ``LockPaths``),
    held only inside ``reserve()``/``sync()``'s critical sections and never
    across an HTTP call, so concurrent workers/scripts share one counter
    without blocking each other's requests."""

    def __init__(
        self,
        conn_factory,
        lock_path: Path,
        daily_budget: int,
        *,
        today=lambda: datetime.now(timezone.utc).date().isoformat(),
    ):
        self._conn_factory = conn_factory
        self._lock_path = Path(lock_path)
        self._daily_budget = daily_budget
        self._today = today

    def reserve(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._reserve_locked()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _reserve_locked(self) -> None:
        day = self._today()
        conn = self._conn_factory()
        try:
            ensure_tables(conn)
            row = conn.execute("SELECT used, budget FROM tmdb_budget WHERE day = ?", (day,)).fetchone()
            if row is None:
                used, budget = 0, self._daily_budget
                conn.execute(
                    "INSERT INTO tmdb_budget (day, used, budget, updated_at) VALUES (?, ?, ?, ?)",
                    (day, used, budget, int(time.time())),
                )
            else:
                used, budget = row[0], row[1]
                if budget != self._daily_budget:
                    # Reconcile to the current setting before the
                    # exhaustion check, so another process's changed
                    # tmdb_daily_budget takes effect on this process's very
                    # next reserve() -- not just after its own next sync().
                    budget = self._daily_budget
                    conn.execute(
                        "UPDATE tmdb_budget SET budget = ?, updated_at = ? WHERE day = ?",
                        (budget, int(time.time()), day),
                    )
            if used >= budget:
                conn.commit()
                raise BudgetExhausted(f"daily TMDB budget exhausted ({used}/{budget})")
            conn.execute(
                "UPDATE tmdb_budget SET used = used + 1, updated_at = ? WHERE day = ?",
                (int(time.time()), day),
            )
            conn.commit()
        finally:
            conn.close()

    def sync(self, effective_budget: int) -> None:
        """Update today's row's ``budget`` column to ``effective_budget``
        (never touching ``used``) -- called right after a settings-page
        save so every process sees the new budget immediately rather than
        waiting for its own next ``reserve()`` to reconcile it. A no-op --
        no write of any kind, not even ``ensure_tables`` -- when there is no
        row for today yet (nothing to update, table missing or not), or the
        stored budget already matches."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._sync_locked(effective_budget)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _sync_locked(self, effective_budget: int) -> None:
        day = self._today()
        conn = self._conn_factory()
        try:
            try:
                row = conn.execute("SELECT budget FROM tmdb_budget WHERE day = ?", (day,)).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is not None and row[0] != effective_budget:
                conn.execute(
                    "UPDATE tmdb_budget SET budget = ?, updated_at = ? WHERE day = ?",
                    (effective_budget, int(time.time()), day),
                )
                conn.commit()
        finally:
            conn.close()

    def status(self) -> dict:
        """Read-only snapshot of today's budget row: no ``ensure_tables``,
        no ``INSERT``, no ``commit`` -- a missing table or row simply
        reports ``used=0, budget=self._daily_budget`` rather than creating
        one, so a status read never has a side effect on the database."""
        day = self._today()
        conn = self._conn_factory()
        try:
            try:
                row = conn.execute("SELECT used, budget FROM tmdb_budget WHERE day = ?", (day,)).fetchone()
            except sqlite3.OperationalError:
                row = None
        finally:
            conn.close()
        used, budget = (row[0], row[1]) if row is not None else (0, self._daily_budget)
        return {
            "day": day,
            "used": used,
            "budget": budget,
            "remaining": max(budget - used, 0),
            "reset_at": _next_utc_midnight_iso(day),
        }


@dataclass
class CacheEntry:
    cache_key: str
    status: str
    payload: list | None
    tmdb_id: int | None
    media_type: str | None
    fetched_at: int
    retry_after: int | None
    error_class: str | None


class TmdbCache:
    """Thin wrapper over the ``tmdb_cache`` table."""

    def __init__(self, conn_factory):
        self._conn_factory = conn_factory

    def get(self, key: str) -> CacheEntry | None:
        conn = self._conn_factory()
        try:
            ensure_tables(conn)
            row = conn.execute(
                "SELECT cache_key, status, payload_json, tmdb_id, media_type, fetched_at, retry_after, error_class "
                "FROM tmdb_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        payload = json.loads(row[2]) if row[2] is not None else None
        return CacheEntry(
            cache_key=row[0],
            status=row[1],
            payload=payload,
            tmdb_id=row[3],
            media_type=row[4],
            fetched_at=row[5],
            retry_after=row[6],
            error_class=row[7],
        )

    def put(self, entry: CacheEntry) -> None:
        conn = self._conn_factory()
        try:
            ensure_tables(conn)
            payload_json = json.dumps(entry.payload) if entry.payload is not None else None
            conn.execute(
                "INSERT INTO tmdb_cache (cache_key, tmdb_id, media_type, language, payload_json, status, fetched_at, retry_after, error_class) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "tmdb_id=excluded.tmdb_id, media_type=excluded.media_type, payload_json=excluded.payload_json, "
                "status=excluded.status, fetched_at=excluded.fetched_at, retry_after=excluded.retry_after, error_class=excluded.error_class",
                (
                    entry.cache_key,
                    entry.tmdb_id,
                    entry.media_type,
                    payload_json,
                    entry.status,
                    entry.fetched_at,
                    entry.retry_after,
                    entry.error_class,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def counts(self) -> dict:
        conn = self._conn_factory()
        try:
            ensure_tables(conn)
            rows = conn.execute("SELECT status, COUNT(*) FROM tmdb_cache GROUP BY status").fetchall()
        finally:
            conn.close()
        return {status: count for status, count in rows}


def search_cache_key(kind: str, language: str, query_key: str, year: int | None) -> str:
    return f"search:{kind}:{language}:{query_key}:{year or 0}"


def find_cache_key(source: str, external_id: str, language: str) -> str:
    """Cache key for ``TmdbClient.find_by_imdb`` (T15 addendum §3.2:
    "cache key includes source/id/language")."""
    return f"find:{source}:{language}:{external_id}"


def _epoch_now() -> int:
    return int(time.time())


_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


def _fold(text: str) -> str:
    """NFKC-normalize, casefold and strip punctuation/whitespace for
    tolerant title/query comparison.  Private to this module; a later
    integration step may swap this for ``library_normalize.search_key``."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _PUNCT_RE.sub("", normalized)


def _parse_retry_after(value) -> int:
    if value is None:
        return RETRY_AFTER_DEFAULT
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT
    return max(0, min(seconds, RETRY_AFTER_MAX))


def _cached_entry_is_usable(entry: CacheEntry, now: int, *, ttl_seconds: int | None = None) -> bool:
    """Shared cache-gating rule for ``TmdbClient.search`` and ``.genres``.

    ``ok`` / ``empty`` / ``failed_permanent`` entries are always usable
    without a new request.  A ``failed_retryable`` entry is usable only
    until its ``retry_after`` time passes.  When ``ttl_seconds`` is given
    (``genres``'s 30-day cache), an ``ok`` entry older than that is treated
    as stale and not usable.
    """
    if entry.status == "failed_retryable":
        return entry.retry_after is None or now < entry.retry_after
    if entry.status not in ("ok", "empty", "failed_permanent"):
        return False
    if entry.status == "ok" and ttl_seconds is not None and now - entry.fetched_at >= ttl_seconds:
        return False
    return True


class TmdbClient:
    """TMDB HTTP client: cache-first search/genres with rate limiting, a
    persisted daily budget and the retry policy below.

    All HTTP goes through ``session`` (a ``requests``-shaped object with
    ``.get(url, params=..., timeout=...)``); nothing here calls the
    ``requests`` module's own ``get`` directly, so tests can inject a fake
    session and never touch the network.

    Retry policy (checked for every request, before each attempt re-runs
    ``Budget.reserve()`` and ``RateLimiter.wait()``):
      * 429: read ``Retry-After`` (seconds, default 5, capped at 60), sleep,
        retry up to 3 times; beyond that -> ``failed_retryable`` with
        ``retry_after = now + 3600``.
      * 5xx / ``requests.RequestException``: back off 1s, 2s, 4s, up to 3
        retries; beyond that -> ``failed_retryable``.
      * 401/403 -> raises ``InvalidApiKey``. Other 4xx -> ``failed_permanent``.
      * 200 with empty ``results`` -> ``empty`` (also cached); non-empty ->
        ``ok`` with ``payload`` capped to the first 10 results.

    ``request_timeout`` (default 15s, per HTTP attempt) and ``max_retries``
    (default ``MAX_RETRIES`` = 3) are constructor overrides -- the synchronous
    settings-page routes (T10 fix wave 1) construct a "fast" client with
    ``request_timeout=6, max_retries=0`` so a single stuck request can never
    compound into the ~67s-per-request worst case (4 attempts x up to 15s
    each, plus 1+2+4s backoff) that risked exceeding gunicorn's 30s worker
    timeout; ``max_retries=0`` makes every retry branch above return after
    exactly one attempt, with no backoff sleep.
    """

    def __init__(
        self,
        api_key,
        *,
        conn_factory,
        lock_path,
        limits: Limits = DEFAULT_LIMITS,
        session=None,
        clock=time.monotonic,
        sleep=time.sleep,
        today=None,
        request_timeout: float = 15.0,
        max_retries: int = MAX_RETRIES,
        limiter: "RateLimiter | None" = None,
        tvmaze_limiter: "RateLimiter | None" = None,
    ):
        self._api_key = api_key
        self._conn_factory = conn_factory
        self._limits = limits
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._cache = TmdbCache(conn_factory)
        budget_kwargs = {} if today is None else {"today": today}
        self._budget = Budget(conn_factory, Path(lock_path), limits.daily_budget, **budget_kwargs)
        # T14 fix wave 1: an explicit ``limiter`` lets a caller (app.py's
        # ``_library_client_factory``) hand every client it builds the same
        # process-wide RateLimiter, so the adaptive 429 slow-down's pacing
        # state survives across the background enricher's per-round clients
        # instead of being discarded with each new one -- see
        # ``_shared_tmdb_rate_limiter``.
        self._limiter = limiter if limiter is not None else RateLimiter(limits.min_interval_ms, clock=clock, sleep=sleep)
        # T15: TVmaze's own >=500ms pacing, deliberately a separate
        # RateLimiter instance from ``_limiter`` above (different host, no
        # relation to the TMDB adaptive 429 slow-down) but sharing the same
        # injectable clock/sleep for deterministic tests.
        self._tvmaze_limiter = tvmaze_limiter if tvmaze_limiter is not None else RateLimiter(TVMAZE_MIN_INTERVAL_MS, clock=clock, sleep=sleep)
        self._request_timeout = request_timeout
        self._max_retries = max_retries
        self._http_429_responses = 0

    @property
    def http_429_responses(self) -> int:
        """Count of every 429 HTTP response this client has ever seen (T14
        fix wave 1) -- incremented once per response in ``_fetch``'s 429
        branch, regardless of whether that attempt is later retried or
        exhausts its retries. Unlike ``EnrichStats.http_429`` (which only
        ever counted a *retry-exhausted* search as one 429), this counts
        actual responses, so a search that gets three 429s before finally
        succeeding on the fourth attempt counts as three here, not one."""
        return self._http_429_responses

    def _get(self, path: str, params: dict):
        self._budget.reserve()
        self._limiter.wait()
        request_params = dict(params)
        request_params["api_key"] = self._api_key
        return self._session.get(TMDB_BASE + path, params=request_params, timeout=self._request_timeout)

    def _fetch(
        self,
        path: str,
        params: dict,
        cache_key: str,
        *,
        result_key: str = "results",
        result_keys: Mapping[str, str] | None = None,
        cap: int | None = SEARCH_RESULT_CAP,
        mode: str = "list",
    ) -> CacheEntry:
        retries_429 = 0
        retries_5xx = 0
        while True:
            try:
                response = self._get(path, params)
            except requests.RequestException as exc:
                if retries_5xx >= self._max_retries:
                    LOG.warning("tmdb request key=%s error=%s status=failed_retryable", cache_key, type(exc).__name__)
                    return CacheEntry(
                        cache_key, "failed_retryable", None, None, None,
                        _epoch_now(), _epoch_now() + RETRY_AFTER_COOLDOWN, type(exc).__name__,
                    )
                delay = FIVE_XX_BACKOFF[retries_5xx]
                retries_5xx += 1
                LOG.info("tmdb request key=%s error=%s retry=%s", cache_key, type(exc).__name__, retries_5xx)
                self._sleep(delay)
                continue

            status_code = response.status_code

            if status_code == 200:
                data = response.json()
                if mode == "object":
                    # A details endpoint (movie|tv/<id>) returns one object,
                    # not a {"results": [...]} envelope; wrap it as a
                    # single-element list so it still fits CacheEntry.payload
                    # (list | None) without widening that field's type.
                    payload = [data] if data else None
                    status = "ok" if payload else "empty"
                elif result_keys:
                    # T15 §3.2: TMDB `/find` splits results across
                    # ``movie_results``/``tv_results`` (plus tv_episode/
                    # tv_season/person, which the caller never asks for
                    # here) rather than one ``results`` envelope -- tag
                    # each item with the media_type its own array implies
                    # (``result_keys`` maps envelope key -> tag) so the
                    # merged list slots into ``score_candidate``/``judge``
                    # exactly like a ``search``/``multi`` result.
                    items = []
                    for key, media_type_tag in result_keys.items():
                        for item in data.get(key) or []:
                            item = dict(item)
                            item.setdefault("media_type", media_type_tag)
                            items.append(item)
                    if cap is not None:
                        items = items[:cap]
                    status = "ok" if items else "empty"
                    payload = items if items else None
                else:
                    items = list(data.get(result_key) or [])
                    if cap is not None:
                        items = items[:cap]
                    status = "ok" if items else "empty"
                    payload = items if items else None
                LOG.info("tmdb request key=%s http=%s status=%s", cache_key, status_code, status)
                return CacheEntry(cache_key, status, payload, None, None, _epoch_now(), None, None)

            if status_code in (401, 403):
                LOG.warning("tmdb request key=%s http=%s error=InvalidApiKey", cache_key, status_code)
                raise InvalidApiKey(f"TMDB rejected the API key (HTTP {status_code})")

            if status_code == 429:
                # T14 fix wave 1: count every 429 RESPONSE seen, not just a
                # search that ultimately exhausts its retries -- see
                # ``http_429_responses``.
                self._http_429_responses += 1
                # Adaptive slow-down (T14/user instruction): a 429 raises
                # the shared pacing interval for every subsequent dispatch
                # (not just this retry) until ADAPTIVE_SLOWDOWN_SECONDS of
                # calm passes, on top of the per-request Retry-After sleep
                # below.
                self._limiter.slow_down(ADAPTIVE_SLOWDOWN_MIN_INTERVAL_MS, ADAPTIVE_SLOWDOWN_SECONDS)
                if retries_429 >= self._max_retries:
                    LOG.warning("tmdb request key=%s http=429 status=failed_retryable", cache_key)
                    return CacheEntry(
                        cache_key, "failed_retryable", None, None, None,
                        _epoch_now(), _epoch_now() + RETRY_AFTER_COOLDOWN, "HTTP429",
                    )
                delay = _parse_retry_after(response.headers.get("Retry-After"))
                retries_429 += 1
                LOG.info("tmdb request key=%s http=429 retry=%s", cache_key, retries_429)
                self._sleep(delay)
                continue

            if 500 <= status_code < 600:
                if retries_5xx >= self._max_retries:
                    LOG.warning("tmdb request key=%s http=%s status=failed_retryable", cache_key, status_code)
                    return CacheEntry(
                        cache_key, "failed_retryable", None, None, None,
                        _epoch_now(), _epoch_now() + RETRY_AFTER_COOLDOWN, f"HTTP{status_code}",
                    )
                delay = FIVE_XX_BACKOFF[retries_5xx]
                retries_5xx += 1
                LOG.info("tmdb request key=%s http=%s retry=%s", cache_key, status_code, retries_5xx)
                self._sleep(delay)
                continue

            LOG.warning("tmdb request key=%s http=%s status=failed_permanent", cache_key, status_code)
            return CacheEntry(cache_key, "failed_permanent", None, None, None, _epoch_now(), None, f"HTTP{status_code}")

    def search(self, kind: str, query: str, *, year: int | None = None, language: str = "zh-CN") -> CacheEntry:
        query_key = _fold(query)
        cache_key = search_cache_key(kind, language, query_key, year)
        cached = self._cache.get(cache_key)
        if cached is not None and _cached_entry_is_usable(cached, _epoch_now()):
            return cached

        params = {"query": query, "language": language, "page": 1, "include_adult": "false"}
        if year is not None:
            # T15 fix wave 1 (item 3c, master doc §3.3 item 4): TMDB's search
            # endpoints DO also accept a bare ``year`` filter, but the typed
            # alternative is more precise per kind -- ``primary_release_
            # year`` for /search/movie, ``first_air_date_year`` for
            # /search/tv -- so those are what we send instead.
            if kind == "tv":
                params["first_air_date_year"] = year
            else:
                params["primary_release_year"] = year

        entry = self._fetch(f"/search/{kind}", params, cache_key)
        self._cache.put(entry)
        return entry

    def genres(self, kind: str, language: str = "zh-CN") -> dict[int, str]:
        cache_key = f"genres:{kind}:{language}"
        cached = self._cache.get(cache_key)
        if cached is not None and _cached_entry_is_usable(cached, _epoch_now(), ttl_seconds=GENRE_CACHE_TTL_SECONDS):
            entry = cached
        else:
            entry = self._fetch(f"/genre/{kind}/list", {"language": language}, cache_key, result_key="genres", cap=None)
            self._cache.put(entry)
        if entry.status != "ok":
            return {}
        return {int(item["id"]): item["name"] for item in entry.payload}

    def check_connectivity(self, *, language: str = "zh-CN") -> CacheEntry:
        """Uncached ``genre/movie/list`` probe for the settings-page "测试
        TMDB 连接" button (T10). Deliberately bypasses ``TmdbCache`` (unlike
        ``genres()``) so every call actually reaches the network and
        reserves one ``Budget`` request -- a warm cache must never make a
        connectivity check silently report success without talking to
        TMDB."""
        return self._fetch("/genre/movie/list", {"language": language}, "connectivity-check", result_key="genres", cap=None)

    def details(self, kind: str, tmdb_id: int, *, language: str = "zh-CN", append_to_response: str | None = None) -> CacheEntry:
        """Fetch ``/<kind>/<tmdb_id>`` (``movie`` or ``tv``), cache-first.

        Only called for ``exact`` matches when ``enrich_batch``'s
        ``with_details`` is on (§6.2 step 3) -- search results already carry
        poster/backdrop/overview/genre_ids for the common case, so this is
        an optional, budget-costing extra request.  ``entry.payload`` is the
        single detail object wrapped in a one-element list (see ``_fetch``'s
        ``mode="object"``), matching every other cache entry's ``list |
        None`` shape.

        ``append_to_response`` (T15 addendum §4.2/§5.1: the backfill CLI's
        ``enrich``/``retry`` phases pass ``"external_ids"`` to pick up
        ``imdb_id``/``tvmaze_id`` in the same request) is folded into the
        cache key so it never collides with a plain ``details()`` call for
        the same id/language -- omitting it keeps the exact same cache key
        (and behaviour) as before this parameter existed.
        """
        cache_key = f"details:{kind}:{language}:{tmdb_id}"
        if append_to_response:
            cache_key += f":{append_to_response}"
        cached = self._cache.get(cache_key)
        if cached is not None and _cached_entry_is_usable(cached, _epoch_now()):
            return cached
        params = {"language": language}
        if append_to_response:
            params["append_to_response"] = append_to_response
        entry = self._fetch(f"/{kind}/{tmdb_id}", params, cache_key, mode="object")
        self._cache.put(entry)
        return entry

    def find_by_imdb(self, imdb_id: str, *, language: str = "zh-CN") -> CacheEntry:
        """``GET /find/{imdb_id}?external_source=imdb_id`` -- confirms an
        IMDb-sourced hint's identity by mapping straight to a TMDB id (T15
        design item 2; addendum §3.2). Cache-first like every other client
        call (cache key via ``find_cache_key``, includes source/id/
        language). Only ``movie_results``/``tv_results`` are kept, each
        tagged with its own ``media_type`` (see ``_fetch``'s
        ``result_keys``) so the merged payload slots into
        ``score_candidate``/``judge`` exactly like a search result;
        ``tv_episode_results``/``tv_season_results``/``person_results`` are
        dropped -- §3.2: "忽略剧集分集/季级结果"."""
        cache_key = find_cache_key("imdb_id", imdb_id, language)
        cached = self._cache.get(cache_key)
        if cached is not None and _cached_entry_is_usable(cached, _epoch_now()):
            return cached
        entry = self._fetch(
            f"/find/{imdb_id}",
            {"external_source": "imdb_id", "language": language},
            cache_key,
            result_keys={"movie_results": "movie", "tv_results": "tv"},
        )
        self._cache.put(entry)
        return entry

    def tvmaze_search(self, query: str) -> CacheEntry:
        """``GET https://api.tvmaze.com/search/shows?q=<query>`` -- the T15
        TVmaze fallback for ``tv`` media the normal TMDB search flow
        couldn't find (z1-hints §3; never called for movies). Cache-first
        (key ``tvmaze:<folded query>`` -- ``query`` is the media's own
        ``title_zh``, run through the same ``_fold`` normalization every
        other cache key uses, NOT the ``media.search_key`` column -- in the
        same ``tmdb_cache`` table), which is also how "never more than one
        TVmaze request per media
        per day" is satisfied -- an ``ok``/``empty`` verdict is reused
        indefinitely once cached, a strictly stronger guarantee than a
        once-per-day limit. Paced by its own ``_tvmaze_limiter``
        (>=500ms, independent of the TMDB pacing/daily budget -- TVmaze is
        a free public API with no key, never counted against
        ``Budget``), using the same injected ``session`` as every TMDB
        call. Payload rows are the raw ``{"score":.., "show": {...}}``
        search hits; the caller picks the best name match and reads
        ``show["externals"]["imdb"]`` itself -- this method never calls
        ``/find``."""
        cache_key = f"tvmaze:{_fold(query)}"
        cached = self._cache.get(cache_key)
        if cached is not None and _cached_entry_is_usable(cached, _epoch_now()):
            return cached
        entry = self._tvmaze_fetch(query, cache_key)
        self._cache.put(entry)
        return entry

    def _tvmaze_fetch(self, query: str, cache_key: str) -> CacheEntry:
        """Mirrors ``_fetch``'s retry policy (429 Retry-After, bounded 5xx
        backoff) against the TVmaze host instead of TMDB -- kept separate
        rather than folded into ``_fetch`` since TVmaze has its own base
        URL, no ``api_key`` query param, and its own rate limiter."""
        retries_429 = 0
        retries_5xx = 0
        while True:
            self._tvmaze_limiter.wait()
            try:
                response = self._session.get(TVMAZE_BASE + "/search/shows", params={"q": query}, timeout=self._request_timeout)
            except requests.RequestException as exc:
                if retries_5xx >= self._max_retries:
                    return CacheEntry(cache_key, "failed_retryable", None, None, None, _epoch_now(), _epoch_now() + RETRY_AFTER_COOLDOWN, type(exc).__name__)
                delay = FIVE_XX_BACKOFF[retries_5xx]
                retries_5xx += 1
                self._sleep(delay)
                continue

            status_code = response.status_code
            if status_code == 200:
                items = list(response.json() or [])
                status = "ok" if items else "empty"
                return CacheEntry(cache_key, status, items or None, None, None, _epoch_now(), None, None)
            if status_code == 429:
                if retries_429 >= self._max_retries:
                    return CacheEntry(cache_key, "failed_retryable", None, None, None, _epoch_now(), _epoch_now() + RETRY_AFTER_COOLDOWN, "HTTP429")
                delay = _parse_retry_after(response.headers.get("Retry-After"))
                retries_429 += 1
                self._sleep(delay)
                continue
            if 500 <= status_code < 600:
                if retries_5xx >= self._max_retries:
                    return CacheEntry(cache_key, "failed_retryable", None, None, None, _epoch_now(), _epoch_now() + RETRY_AFTER_COOLDOWN, f"HTTP{status_code}")
                delay = FIVE_XX_BACKOFF[retries_5xx]
                retries_5xx += 1
                self._sleep(delay)
                continue
            return CacheEntry(cache_key, "failed_permanent", None, None, None, _epoch_now(), None, f"HTTP{status_code}")

    def budget_status(self) -> dict:
        return self._budget.status()

    def map_search(self, requests: list[tuple[str, str, int | None]]) -> list[CacheEntry]:
        outcomes: list[CacheEntry | None] = [None] * len(requests)
        invalid_key = threading.Event()

        def run_one(kind: str, query: str, year: int | None) -> CacheEntry:
            # A worker thread reuses itself for the next queued item as soon
            # as it is free, which can race ahead of the exception being
            # observed below -- checking this flag first stops it (and every
            # other worker) from starting further doomed requests.
            if invalid_key.is_set():
                raise InvalidApiKey("a previous request already found the API key invalid")
            try:
                return self.search(kind, query, year=year)
            except InvalidApiKey:
                invalid_key.set()
                raise

        executor = ThreadPoolExecutor(max_workers=self._limits.max_concurrency)
        cancel_pending = False
        try:
            futures = {
                executor.submit(run_one, kind, query, year): index
                for index, (kind, query, year) in enumerate(requests)
            }
            for future in as_completed(futures):
                outcomes[futures[future]] = future.result()
        except InvalidApiKey:
            # Drop whatever is still purely queued (not yet started by any
            # worker) instead of spending more budget on a doomed key.
            cancel_pending = True
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=cancel_pending)
        return outcomes


@dataclass(frozen=True)
class Candidate:
    tmdb_id: int | None
    media_type: str | None
    title: str
    original_title: str | None
    year: int | None
    score: float
    poster_path: str | None
    backdrop_path: str | None
    overview: str | None
    genre_ids: tuple[int, ...]
    # T15 addendum §14.3 item 1: carried straight from the TMDB search/find
    # result so an ``exact`` write can populate ``ratings_json.tmdb``
    # without an extra request.
    vote_average: float | None = None
    vote_count: int | None = None


@dataclass(frozen=True)
class Judgement:
    status: str
    tmdb_id: int | None
    media_type: str | None
    score: float
    candidates: tuple[Candidate, ...]


def _similarity(a: str, b: str) -> float:
    fa, fb = _fold(a), _fold(b)
    if not fa or not fb:
        return 0.0
    if fa == fb:
        return 1.0
    if fa in fb or fb in fa:
        return 0.8
    return difflib.SequenceMatcher(None, fa, fb).ratio()


def _title_score(names: Sequence[str], title_zh: str, aliases: Sequence[str]) -> float:
    best = 0.0
    for query in (title_zh, *aliases):
        if not query:
            continue
        for name in names:
            if not name:
                continue
            best = max(best, _similarity(query, name))
    return best


def _extract_year(result: dict) -> int | None:
    date = result.get("release_date") or result.get("first_air_date")
    if not date or len(date) < 4:
        return None
    try:
        return int(date[:4])
    except ValueError:
        return None


def _year_score(candidate_year: int | None, year: int | None) -> float:
    if candidate_year is None or year is None:
        return 0.0
    diff = abs(candidate_year - year)
    if diff == 0:
        return 0.15
    if diff == 1:
        return 0.05
    return -0.30


def score_candidate(
    result: dict, *, title_zh: str, aliases: Sequence[str], year: int | None, inferred_type: str
) -> Candidate | None:
    """Score one TMDB search result against the known title/aliases/year.

    Returns ``None`` for ``person`` results, which are never candidates.
    """
    if result.get("media_type") == "person":
        return None

    media_type = result.get("media_type") or inferred_type
    title = result.get("title") or result.get("name") or ""
    original_title = result.get("original_title") or result.get("original_name")
    names = [n for n in (result.get("title"), result.get("name"), result.get("original_title"), result.get("original_name")) if n]

    score = _title_score(names, title_zh, aliases)
    candidate_year = _extract_year(result)
    score += _year_score(candidate_year, year)
    if inferred_type == "tv" and media_type == "movie":
        score -= 0.50
    score = max(0.0, min(1.0, score))

    return Candidate(
        tmdb_id=result.get("id"),
        media_type=media_type,
        title=title,
        original_title=original_title,
        year=candidate_year,
        score=score,
        poster_path=result.get("poster_path"),
        backdrop_path=result.get("backdrop_path"),
        overview=result.get("overview"),
        genre_ids=tuple(result.get("genre_ids") or ()),
        vote_average=result.get("vote_average"),
        vote_count=result.get("vote_count"),
    )


def judge(
    results: Sequence[dict], *, title_zh: str, aliases: Sequence[str], year: int | None, inferred_type: str
) -> Judgement:
    """Turn a list of raw TMDB search results into a match decision.

    exact: top score >= 0.90 and beats the runner-up by >= 0.15.
    candidate: top score >= 0.75 (otherwise).
    needs_review: anything else, keeping the top 5 candidates.
    unmatched: no scoreable (non-person) results at all.
    """
    scored = [score_candidate(result, title_zh=title_zh, aliases=aliases, year=year, inferred_type=inferred_type) for result in results]
    candidates = sorted((c for c in scored if c is not None), key=lambda c: c.score, reverse=True)
    if not candidates:
        return Judgement("unmatched", None, None, 0.0, ())

    top = candidates[0]
    second = candidates[1].score if len(candidates) > 1 else 0.0
    top_five = tuple(candidates[:5])

    if top.score >= 0.90 and top.score - second >= 0.15:
        return Judgement("exact", top.tmdb_id, top.media_type, top.score, top_five)
    if top.score >= 0.75:
        return Judgement("candidate", top.tmdb_id, top.media_type, top.score, top_five)
    return Judgement("needs_review", None, None, top.score, top_five)


# ---------------------------------------------------------------------------
# T3.4-T3.5: enrichment flow, identity merge, background enricher (§6.2)
# ---------------------------------------------------------------------------

_ALIAS_LATIN_RE = re.compile(r"[A-Za-z]{3,}")


def _search_kind(media_type: str) -> str:
    """Map a ``media.media_type`` value to the TMDB search endpoint kind.

    ``tv``/``movie`` search their own endpoint; ``unknown`` (a markerless
    title whose text still carries a TV-ish cue -- see
    ``library_normalize.infer_media_type``) uses ``search/multi`` (§6.2
    step 2).
    """
    if media_type in ("movie", "tv"):
        return media_type
    return "multi"


def _load_aliases(title_alt_json: str | None) -> list[str]:
    if not title_alt_json:
        return []
    try:
        aliases = json.loads(title_alt_json)
    except (TypeError, ValueError):
        return []
    return [alias for alias in aliases if isinstance(alias, str)]


def _pick_alias_query(aliases: Sequence[str]) -> str | None:
    """The first alias with a 3+ letter Latin run -- an English/original
    alias worth a second TMDB search when the primary query finds nothing
    (§6.2 step 2; mirrors ``library_normalize.parse_title``'s
    ``original_hint`` rule without depending on that module's private
    regex)."""
    for alias in aliases:
        if _ALIAS_LATIN_RE.search(alias):
            return alias
    return None


def _serialize_candidates(candidates: Sequence[Candidate]) -> str:
    return json.dumps([asdict(c) for c in candidates], ensure_ascii=False)


def _merge_search_results(primary: Sequence[dict], secondary: Sequence[dict]) -> list[dict]:
    """Dedupe two raw TMDB search-result lists by ``(media_type, id)`` for
    ``_judge_media``'s en-US retry (T15 fix wave 1, item 3c) -- a show/movie
    present in both the zh-CN and en-US result sets is judged once, using
    the en-US copy (``secondary`` overwrites a same-key ``primary`` entry),
    since ``original_title``/``original_name`` -- the fields most likely to
    match a Latin alias -- are the same regardless of requested language,
    while ``title``/``name`` differ; keeping only one copy also avoids a
    duplicate candidate quietly shrinking ``judge()``'s top-vs-second-place
    score gap."""
    merged: dict[tuple, dict] = {}
    for item in primary:
        merged[(item.get("media_type"), item.get("id"))] = item
    for item in secondary:
        merged[(item.get("media_type"), item.get("id"))] = item
    return list(merged.values())


@dataclass
class EnrichStats:
    """Summary of one ``enrich_batch`` call -- the numbers the offline
    script and ``/api/library/tmdb-status`` report (§6.2 step 7).  Never
    carries the API key.

    ``candidates_considered`` only counts fresh candidates actually
    processed -- media already resolved by a valid cache verdict (see
    ``_select_fresh_candidates``) are skipped and never counted; it is
    trimmed down further when a ``deadline`` (T10 fix wave 1) stops
    ``enrich_batch`` before every pre-selected candidate was started, in
    which case ``error_class`` is also set to ``"DeadlineExceeded"``.
    ``queue_exhausted`` is True when the candidate-selection scan reached
    the end of the ``match_status='unmatched'`` table without finding
    ``limit`` fresh candidates (so no fresh candidate remains right now);
    it is conservatively False whenever selection stopped early because it
    already had enough -- there may or may not be more.  In
    ``enrich_batch``, ``candidates_considered``/the ``matched_*`` counters
    also include any T14/§5.1 review-pending-unqueried rows processed once
    the ``unmatched`` queue was exhausted; ``queue_exhausted`` itself still
    only reflects that primary queue. ``requeue_review_batch`` reuses this
    same dataclass for its own (review-pending-only) queue.

    ``http_429`` (T14 fix wave 2) is diffed by the caller (``enrich_batch``/
    ``requeue_review_batch``) once around the *whole* call via
    ``client.http_429_responses``, not per search -- so a 429 seen by
    ``client.genres()``/``client.details()`` (both only called from
    ``_write_exact``) is counted too, not just one seen by the search
    itself.

    ``search_failed_media_ids`` (T14 fix wave 2/finding #1) lists the media
    ids whose underlying TMDB search never produced a genuine ``ok``/
    ``empty`` result during this call -- a transient (``failed_retryable``)
    or permanent (``failed_permanent``) HTTP failure, as opposed to a
    genuine zero-result search. Such a row is left completely untouched
    (see ``_process_candidate_row``): no ``match_score``/candidates are
    written, so it must never be retired as "queried, no candidate". The
    ``--library-requeue-review`` CLI folds these ids into its own in-run
    exclusion set so its internal <=100-row batch loop does not keep
    re-selecting the same failing row within one invocation; a later
    invocation (or day) starts with a fresh exclusion set and retries it.
    """

    candidates_considered: int = 0
    queue_exhausted: bool = False
    requests_made: int = 0
    cache_hits: int = 0
    matched_exact: int = 0
    matched_candidate: int = 0
    matched_needs_review: int = 0
    matched_unmatched: int = 0
    http_429: int = 0
    http_5xx: int = 0
    budget_status: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    error_class: str | None = None
    search_failed_media_ids: list = field(default_factory=list)

    # T15 design item 4: IMDb-hint and TVmaze accounting. ``hint_find_requests``
    # counts every ``TmdbClient.find_by_imdb`` attempt the IMDb-hint pathway
    # made (cache hit or not -- ``requests_made``/``cache_hits`` above still
    # separately attribute the underlying HTTP/budget cost); a TVmaze-sourced
    # ``/find`` call (after a TVmaze match) does NOT count here, only the
    # hint-table-driven pathway does. ``hint_confirmed_exact``/
    # ``hint_candidate``/``hint_conflicts`` count a hint-driven judgement's
    # final outcome (exact / candidate incl. the proposed_review cap /
    # needs_review incl. a type conflict); a hint that fell through to the
    # normal search flow (no usable /find result) counts toward none of the
    # three. ``tvmaze_requests`` counts every ``TmdbClient.tvmaze_search``
    # attempt (cache hit or not).
    hint_find_requests: int = 0
    hint_confirmed_exact: int = 0
    hint_candidate: int = 0
    hint_conflicts: int = 0
    tvmaze_requests: int = 0

    # w5-confirm-enrich: the `decision == "confirmed"` pathway (a sibling
    # offline task writes an already human-verified TMDB id straight into
    # `tmdb_hints`, source `codex_manual_confirmation`) -- one details()
    # request settles it instead of a search. ``confirmed_details_requests``
    # counts every attempt (cache hit or not); ``confirmed_exact``/
    # ``confirmed_not_found``/``confirmed_failed``/``confirmed_conflicts``
    # count that attempt's outcome (success; 404/empty/id-mismatch, which
    # falls through to a normal search; a transient 429/5xx/network
    # failure, which leaves the row untouched for retry; a movie/tv type
    # conflict with the row's already-known media_type, detected before any
    # request is made).
    confirmed_details_requests: int = 0
    confirmed_exact: int = 0
    confirmed_not_found: int = 0
    confirmed_failed: int = 0
    confirmed_conflicts: int = 0


def _run_search(
    client: TmdbClient, kind: str, query: str, year: int | None, stats: EnrichStats, *, language: str = "zh-CN"
) -> CacheEntry:
    """Run one ``client.search`` call, attributing it to ``requests_made``
    or ``cache_hits`` by diffing the daily-budget counter around the call
    (a cache hit never reserves budget). The diff is computed in a
    ``finally`` so a request that raised ``InvalidApiKey`` (budget was
    reserved and the HTTP call was made before the 401/403 was seen) still
    counts toward ``requests_made``; a ``BudgetExhausted`` raised before any
    request was attempted leaves the counter untouched, same as before. A
    retry-exhausted non-429 failure (5xx or a network exception) still
    counts once toward ``http_5xx`` (unchanged: per-search, not per-response
    -- unlike ``http_429``, which T14 fix wave 2 moved to a single diff
    around the whole ``enrich_batch``/``requeue_review_batch`` call so 429s
    from ``genres()``/``details()`` are counted too; see ``EnrichStats``).
    ``BudgetExhausted``/``InvalidApiKey`` propagate to the caller
    unhandled. ``language`` (T15 fix wave 1, item 3c) defaults to
    ``search()``'s own default (``zh-CN``); ``_judge_media``'s one-shot
    en-US retry passes ``"en-US"`` explicitly."""
    before = client.budget_status()["used"]
    entry = None
    try:
        entry = client.search(kind, query, year=year, language=language)
    finally:
        _attribute_request(client, before, entry, stats)
    return entry


def _attribute_request(client: TmdbClient, before_used: int, entry: CacheEntry | None, stats: EnrichStats) -> None:
    """Shared cache/budget/5xx attribution for ``_run_search``/``_run_find``/
    ``_run_find_bare`` (T15): a request that actually spent budget counts
    toward ``requests_made``; one that didn't (and got a real entry back)
    was a cache hit. A retry-exhausted non-429 failure counts once toward
    ``http_5xx`` (unchanged from ``_run_search``'s original inline version --
    per-attempt, not per-response, unlike ``http_429`` which is diffed once
    around the whole ``enrich_batch``/``requeue_review_batch`` call)."""
    after = client.budget_status()["used"]
    made = after - before_used
    if made > 0:
        stats.requests_made += made
    elif entry is not None:
        stats.cache_hits += 1
    if entry is not None and entry.status == "failed_retryable" and entry.error_class != "HTTP429":
        stats.http_5xx += 1


def _run_find_bare(client: TmdbClient, imdb_id: str, stats: EnrichStats, *, language: str = "zh-CN") -> CacheEntry:
    """``TmdbClient.find_by_imdb`` with the same cache/budget attribution as
    ``_run_search``, but WITHOUT touching ``stats.hint_find_requests`` --
    used by the TVmaze pathway's own ``/find`` call (T15/z1-hints §3),
    which is not the IMDb-hint pathway that counter is named for. See
    ``_run_find`` for the hint-pathway wrapper that adds that counter."""
    before = client.budget_status()["used"]
    entry = None
    try:
        entry = client.find_by_imdb(imdb_id, language=language)
    finally:
        _attribute_request(client, before, entry, stats)
    return entry


def _run_find(client: TmdbClient, imdb_id: str, stats: EnrichStats, *, language: str = "zh-CN") -> CacheEntry:
    """The IMDb-hint pathway's ``/find`` call (T15 design item 2): same
    attribution as ``_run_find_bare``, plus ``stats.hint_find_requests``
    (every attempt, cache hit or not)."""
    stats.hint_find_requests += 1
    return _run_find_bare(client, imdb_id, stats, language=language)


def _run_tvmaze(client: TmdbClient, query: str, stats: EnrichStats) -> CacheEntry:
    """``TmdbClient.tvmaze_search`` wrapper: counts every attempt (cache hit
    or not) toward ``stats.tvmaze_requests`` -- TVmaze is never counted
    against the TMDB daily ``Budget``, so there is no budget diff to
    attribute here (unlike ``_run_search``/``_run_find``)."""
    stats.tvmaze_requests += 1
    return client.tvmaze_search(query)


def _run_confirmed_details(client: TmdbClient, kind: str, tmdb_id: int, stats: EnrichStats) -> CacheEntry:
    """The w5-confirm-enrich confirmed-hint pathway's ``details()`` call:
    same cache/budget attribution as ``_run_search``/``_run_find``, plus
    ``stats.confirmed_details_requests`` (every attempt, cache hit or not).
    Uses ``client.details``'s own zh-CN default/cache key, so a later
    ``_write_exact`` details call for the same ``(kind, tmdb_id)`` is a
    cache hit, not a second HTTP request."""
    stats.confirmed_details_requests += 1
    before = client.budget_status()["used"]
    entry = None
    try:
        entry = client.details(kind, tmdb_id)
    finally:
        _attribute_request(client, before, entry, stats)
    return entry


# T15 design item 2: only these three hint decisions are eligible for the
# /find confirmation pathway -- `no_imdb_candidate` has no IMDb id to try,
# by construction.
_HINT_ELIGIBLE_DECISIONS = frozenset({"proposed_exact", "proposed_candidate", "proposed_review"})

# Shared empty default for `_process_candidate_row`'s `hints` param -- a
# media_identity -> hint-dict mapping (see `_load_hints_for_queue`).
_EMPTY_HINTS: Mapping[str, dict] = {}

# T15 design item 2: "unknown -> by hint imdb_type; tvSeries/tvMiniSeries ->
# tv, movie/tvMovie -> movie" -- tvSpecial is also tv-shaped (the offline
# matcher supports it too, see docs/metadata-enrichment.md, so it is mapped
# the same way for completeness.
_IMDB_TYPE_TO_KIND = {
    "movie": "movie", "tvMovie": "movie",
    "tvSeries": "tv", "tvMiniSeries": "tv", "tvSpecial": "tv",
}


def _hint_expected_kind(media_type: str, hint_imdb_type: str | None) -> str:
    """Which ``/find`` results array (movie_results/tv_results, both tagged
    with their own ``media_type`` by ``TmdbClient.find_by_imdb``) to judge
    a hint-driven candidate against. A media row whose own type is already
    known (``movie``/``tv``) always wins; only an ``unknown`` row falls
    back to the hint's own ``imdb_type`` (defaulting to ``movie``, same as
    ``library_normalize.infer_media_type``'s own markerless-title default,
    when the hint's type isn't a recognised one)."""
    if media_type in ("movie", "tv"):
        return media_type
    return _IMDB_TYPE_TO_KIND.get(hint_imdb_type or "", "movie")


_TVMAZE_MATCH_THRESHOLD = 0.5


def _pick_tvmaze_match(payload: Sequence[dict], title_zh: str) -> dict | None:
    """Best ``show`` (by title-fold similarity, see ``_similarity``) among a
    ``tvmaze_search`` payload's ``{"score":.., "show": {...}}`` hits, or
    ``None`` when nothing clears ``_TVMAZE_MATCH_THRESHOLD`` -- TVmaze's own
    relevance ``score`` field is not reused here so the "best name match"
    rule (z1-hints §3) stays consistent with every other title comparison
    in this module."""
    best: dict | None = None
    best_score = 0.0
    for item in payload:
        show = item.get("show") or {}
        score = _similarity(title_zh, show.get("name") or "")
        if score > best_score:
            best_score = score
            best = show
    return best if best is not None and best_score >= _TVMAZE_MATCH_THRESHOLD else None


def _judge_media_with_hint(
    client: TmdbClient, stats: EnrichStats, row: sqlite3.Row, hint: dict | None
) -> tuple[Judgement | None, str | None, dict | None]:
    """Try the IMDb-hint ``/find`` pathway first (T15 design item 2);
    returns ``(judgement, error_class, hint_explanation)``.
    ``hint_explanation`` is non-``None`` only when the hint pathway itself
    produced the returned judgement (the caller uses it to annotate
    ``match_candidates_json``/``imdb_id`` and, for a genuine identity
    confirmation, ``ratings_json``). Falls back to ``_judge_media``'s
    normal search flow -- ``hint_explanation=None`` -- whenever there is no
    usable hint, the top hint candidate has no ``imdb_id``, the decision
    isn't one of the three eligible values, ``/find`` fails outright, or
    ``/find`` comes back with nothing in either results array (a genuine
    empty result -- the hint gave no evidence at all, as opposed to the
    "wrong kind" conflict case below).

    Outcome caps (design item 2): a ``proposed_review`` hint can never
    reach ``exact`` (downgraded to ``candidate``, ``reason: "hint_review"``
    in ``hint_explanation``); a genuine year conflict (|Δ|>1) is already
    guaranteed to score below the ``candidate`` threshold by ``judge()``'s
    existing ``_year_score`` penalty (max achievable score with a >1 year
    diff is 0.70, under the 0.75 ``candidate`` floor), so no separate check
    is needed here. A **type** conflict -- ``/find`` has a result in the
    *other* kind's array but nothing in the expected one -- is reported
    directly as ``needs_review`` (using the other array's results as the
    visible candidates) rather than falling through to search, since a
    wrong-type TMDB hit is still positive evidence the identity needs a
    human look, not silence.
    """
    if hint is not None and hint["decision"] in _HINT_ELIGIBLE_DECISIONS and hint["candidates"]:
        top = hint["candidates"][0]
        imdb_id = top.get("imdb_id")
        if imdb_id:
            entry = _run_find(client, imdb_id, stats)
            if entry.status in ("ok", "empty"):
                results = entry.payload or []
                kind = _hint_expected_kind(row["media_type"], top.get("imdb_type"))
                expected = [r for r in results if r.get("media_type") == kind]
                other = [r for r in results if r.get("media_type") != kind]
                explanation = {"source": "imdb_hint", "imdb_id": imdb_id, "decision": hint["decision"]}
                if expected:
                    aliases = list(_load_aliases(row["title_alt_json"]))
                    for extra in (top.get("primary_title"), top.get("original_title")):
                        if extra:
                            aliases.append(extra)
                    judgement = judge(expected, title_zh=row["title_zh"], aliases=aliases, year=row["year"], inferred_type=kind)
                    if judgement.status == "exact" and hint["decision"] == "proposed_review":
                        judgement = Judgement("candidate", judgement.tmdb_id, judgement.media_type, judgement.score, judgement.candidates)
                        explanation["reason"] = "hint_review"
                    return judgement, None, explanation
                if other:
                    explanation["conflict"] = "type"
                    top_five = tuple(
                        c for c in (
                            score_candidate(r, title_zh=row["title_zh"], aliases=_load_aliases(row["title_alt_json"]), year=row["year"], inferred_type=kind)
                            for r in other[:5]
                        ) if c is not None
                    )
                    return Judgement("needs_review", None, None, 0.0, top_five), None, explanation
                # both arrays empty -> genuinely no usable /find result;
                # fall through to the normal search flow below.
    judgement, error_class = _judge_media(client, stats, row)
    return judgement, error_class, None


def _judge_confirmed_hint(
    client: TmdbClient, stats: EnrichStats, row: sqlite3.Row, hint: dict
) -> tuple[Judgement | None, str | None, dict | None] | None:
    """w5-confirm-enrich: the ``decision == "confirmed"`` pathway -- a
    sibling offline task (``codex_manual_confirmation``) writes an
    already human-verified TMDB id straight into ``tmdb_hints`` after a
    human confirmed a match, so one ``details()`` request settles it
    instead of a search (docs/metadata-enrichment.md).

    Returns ``None`` when the top candidate isn't a well-formed confirmed
    hint (no int ``tmdb_id``, or ``tmdb_type`` not in ``{"movie", "tv"}``),
    letting the caller fall back to the normal hint/search flow; only
    called when ``hint["decision"] == "confirmed"``.

    Movie/tv namespaces differ (§6): a row whose own ``media_type`` is
    already known always wins over the hint's claimed kind, and a
    mismatch is reported as ``needs_review`` (the confirmed id as the
    visible candidate) rather than trusted -- before any request is made.
    Otherwise: a successful, id-matching details fetch is written as
    ``exact``; a hard failure (404/other permanent error, empty payload,
    or an id mismatch in the response) is "not found" -- the confirmed id
    is stale, so this falls through to a plain ``_judge_media`` search,
    annotated with ``confirmed_id_not_found``; a transient failure
    (429/5xx/network) is treated exactly like a failed search -- the row
    stays untouched for a later retry. ``BudgetExhausted``/
    ``InvalidApiKey`` propagate uncaught.
    """
    top = hint["candidates"][0]
    tmdb_id = top.get("tmdb_id")
    kind = top.get("tmdb_type")
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or kind not in ("movie", "tv"):
        return None

    explanation = {
        "source": "codex_confirmation",
        "imdb_id": top.get("imdb_id"),
        "confidence": top.get("confidence"),
        "original_decision": top.get("original_decision"),
    }

    if row["media_type"] not in ("unknown", kind):
        stats.confirmed_conflicts += 1
        conflict_explanation = dict(explanation)
        conflict_explanation["conflict"] = "type"
        conflict_candidate = Candidate(
            tmdb_id=tmdb_id, media_type=kind, title=row["title_zh"], original_title=None,
            year=row["year"], score=0.0, poster_path=None, backdrop_path=None, overview=None, genre_ids=(),
        )
        return Judgement("needs_review", None, None, 0.0, (conflict_candidate,)), None, conflict_explanation

    entry = _run_confirmed_details(client, kind, tmdb_id, stats)

    if entry.status == "ok" and entry.payload and entry.payload[0].get("id") == tmdb_id:
        detail = entry.payload[0]
        candidate = Candidate(
            tmdb_id=tmdb_id, media_type=kind,
            title=detail.get("title") or detail.get("name") or "",
            original_title=detail.get("original_title") or detail.get("original_name"),
            year=_extract_year(detail), score=1.0,
            poster_path=detail.get("poster_path"), backdrop_path=detail.get("backdrop_path"),
            overview=detail.get("overview"),
            genre_ids=tuple(g["id"] for g in (detail.get("genres") or []) if "id" in g),
            vote_average=detail.get("vote_average"), vote_count=detail.get("vote_count"),
        )
        stats.confirmed_exact += 1
        return Judgement("exact", tmdb_id, kind, 1.0, (candidate,)), None, explanation

    if entry.status == "failed_retryable":
        stats.confirmed_failed += 1
        return None, entry.error_class, None

    # Hard "not found" (404/other permanent failure, empty payload, or an
    # id mismatch): never write exact from a confirmation TMDB no longer
    # serves -- fall through to a plain search instead.
    stats.confirmed_not_found += 1
    # Review follow-up: the search verdict below is NOT the confirmation's
    # match, so its explanation must not carry the confirmation's
    # ``imdb_id`` -- ``_write_exact`` would otherwise stamp that IMDb id
    # (and its rating) onto whatever different TMDB title the search
    # found. Keep the provenance under distinct keys instead.
    fallthrough_explanation = {
        "source": "codex_confirmation_not_found",
        "confirmed_id_not_found": tmdb_id,
        "confirmed_imdb_id": top.get("imdb_id"),
    }
    judgement, error_class = _judge_media(client, stats, row)
    return judgement, error_class, fallthrough_explanation


def _judge_media_full(
    client: TmdbClient, stats: EnrichStats, row: sqlite3.Row, hint: dict | None, *, tvmaze_enabled: bool
) -> tuple[Judgement | None, str | None, dict | None]:
    """``_judge_media_with_hint`` plus the TVmaze fallback (z1-hints §3):
    when that still leaves a genuine ``unmatched`` verdict for a ``tv``
    media and TVmaze is enabled, try TVmaze's own name search -> best
    match's ``externals.imdb`` -> ``/find`` -> the same judge/score rules,
    tagged ``{"source": "tvmaze", "imdb_id": ...}`` on success. Any failure
    along that chain (empty search, no IMDb id, empty/failed ``/find``, no
    ``tv`` results) silently keeps the original ``unmatched`` judgement --
    TVmaze is only ever a bonus chance, never a reason to fail the row.

    w5-confirm-enrich: a ``decision == "confirmed"`` hint is tried first
    (``_judge_confirmed_hint``), before the ``_HINT_ELIGIBLE_DECISIONS``
    pathway below -- when it produces a result, that result is returned
    directly (never through the TVmaze fallback; a not-found fallthrough
    already resolved via a plain search)."""
    if hint is not None and hint["decision"] == "confirmed" and hint["candidates"]:
        confirmed = _judge_confirmed_hint(client, stats, row, hint)
        if confirmed is not None:
            return confirmed

    judgement, error_class, explanation = _judge_media_with_hint(client, stats, row, hint)
    if judgement is None or judgement.status != "unmatched" or row["media_type"] != "tv" or not tvmaze_enabled:
        return judgement, error_class, explanation

    tvmaze_entry = _run_tvmaze(client, row["title_zh"], stats)
    if tvmaze_entry.status != "ok" or not tvmaze_entry.payload:
        return judgement, error_class, explanation
    match = _pick_tvmaze_match(tvmaze_entry.payload, row["title_zh"])
    imdb_id = (match or {}).get("externals", {}).get("imdb") if match else None
    if not imdb_id:
        return judgement, error_class, explanation

    find_entry = _run_find_bare(client, imdb_id, stats)
    if find_entry.status not in ("ok", "empty") or not find_entry.payload:
        return judgement, error_class, explanation
    tv_results = [r for r in find_entry.payload if r.get("media_type") == "tv"]
    if not tv_results:
        return judgement, error_class, explanation

    aliases = list(_load_aliases(row["title_alt_json"]))
    if match.get("name"):
        aliases.append(match["name"])
    tv_judgement = judge(tv_results, title_zh=row["title_zh"], aliases=aliases, year=row["year"], inferred_type="tv")
    tvmaze_explanation = {"source": "tvmaze", "imdb_id": imdb_id}
    # T15 fix wave 1 (item 3a): carry the confirming show's own tvmaze id
    # and rating.average through to _write_exact (which sets media.
    # tvmaze_id and merges ratings_json.tvmaze from these) -- both omitted
    # when absent, so a TVmaze show with no numeric id or no rating yet
    # never adds a stray key here.
    if match.get("id") is not None:
        tvmaze_explanation["tvmaze_id"] = match["id"]
    tvmaze_rating = (match.get("rating") or {}).get("average")
    if tvmaze_rating is not None:
        tvmaze_explanation["tvmaze_rating"] = tvmaze_rating
    return tv_judgement, None, tvmaze_explanation


def _judge_media(client: TmdbClient, stats: EnrichStats, row: sqlite3.Row) -> tuple[Judgement | None, str | None]:
    """Judge one media row against TMDB search results.

    Returns ``(judgement, error_class)``. ``judgement`` is ``None`` when the
    underlying search (the primary query, or the alias retry when that was
    reached) never produced a genuine ``ok``/``empty`` result -- a transient
    or permanent HTTP failure -- in which case ``error_class`` names it
    (e.g. ``"HTTP503"``, ``"HTTP429"``, a network exception's class name).
    T14 fix wave 2/finding #1: a transient TMDB outage must never be
    mistaken for "TMDB was asked and found nothing" -- the caller
    (``_process_candidate_row``) must leave such a row completely untouched
    rather than writing an empty-candidates verdict. A genuine result (even
    an empty one) always returns a real ``Judgement`` and
    ``error_class=None``.

    T15 fix wave 1 (item 3c, master doc §3.3 item 2): once the zh-CN
    query(ies) above produce a judgement, ONE additional ``en-US`` search
    is tried -- but only when that judgement is still ``unmatched`` or
    ``needs_review`` ("no/undecidable results"; an ``exact``/``candidate``
    verdict never issues it, so a decisive zh-CN result never doubles the
    request count). It reuses the alias query when one exists (same "an
    English/Latin variant" the alias retry already picks), else the plain
    ``title_zh``, and re-judges the ``zh``+``en-US`` results merged (deduped
    by ``_merge_search_results``) together. A failure on this bonus request
    (retryable/permanent) is never fatal -- unlike the primary/alias search
    above, it does not abandon the row; the zh-CN-only judgement already in
    hand (a genuine result, just not yet a strong one) is kept as-is.
    """
    media_type = row["media_type"]
    title_zh = row["title_zh"]
    year = row["year"]
    aliases = _load_aliases(row["title_alt_json"])
    kind = _search_kind(media_type)

    entry = _run_search(client, kind, title_zh, year, stats)
    if entry.status not in ("ok", "empty"):
        return None, entry.error_class
    results = entry.payload or []

    alias_query = _pick_alias_query(aliases)
    if not results and alias_query:
        alias_entry = _run_search(client, kind, alias_query, year, stats)
        if alias_entry.status not in ("ok", "empty"):
            return None, alias_entry.error_class
        results = alias_entry.payload or []

    judgement = judge(results, title_zh=title_zh, aliases=aliases, year=year, inferred_type=media_type)

    if judgement.status in ("unmatched", "needs_review"):
        en_query = alias_query or title_zh
        en_entry = _run_search(client, kind, en_query, year, stats, language="en-US")
        if en_entry.status in ("ok", "empty") and en_entry.payload:
            merged_results = _merge_search_results(results, en_entry.payload)
            judgement = judge(merged_results, title_zh=title_zh, aliases=aliases, year=year, inferred_type=media_type)

    return judgement, None


def _candidates_json_with_explanation(candidates: Sequence[Candidate], hint_explanation: dict | None) -> str:
    """``match_candidates_json`` for ``candidate``/``needs_review``: the
    plain top-5-candidates array (unchanged, T3.4 shape) normally, or --
    when the judgement came from the T15 hint/TVmaze pathway -- that same
    array nested under the hint's explanation dict (``source``/``imdb_id``/
    ``decision``/optionally ``reason``/``conflict``, design item 2), so the
    hint's provenance survives without a ``media`` schema change."""
    if hint_explanation is None:
        return _serialize_candidates(candidates)
    payload = dict(hint_explanation)
    payload["candidates"] = [asdict(c) for c in candidates]
    return json.dumps(payload, ensure_ascii=False)


def _tmdb_rating_entry(vote_average: float | None, vote_count: int | None) -> dict | None:
    """The ``ratings_json.tmdb`` entry from a winning candidate's (or detail
    payload's) ``vote_average``/``vote_count`` (T15 addendum §14.3 item 1),
    or ``None`` when there is nothing genuine to show (§14.2: "评分为
    null、0 票...时不显示虚假的 0.0/10")."""
    if vote_average is None or not vote_count:
        return None
    return {"score": round(vote_average, 1), "scale": 10, "votes": vote_count, "as_of": _default_today()}


# T15 fix wave 1 (item 3b): a fixed, non-descriptive error class recorded on
# ``media.ratings_error`` when a ratings-relevant fetch fails -- unlike
# ``metadata_error`` (which keeps the real ``retryable:``/``permanent:
# <ErrorClass>`` detail for retry-eligibility), the ratings side only ever
# needs to say "a fetch failed", never why, so this stays one constant
# string across every failure kind.
RATINGS_FETCH_ERROR_CLASS = "ratings_fetch_failed"


def merge_ratings_json(existing_json: str | None, new_entries: Mapping[str, dict]) -> str:
    """Merge ``new_entries`` (e.g. ``{"imdb": {...}}``) into an existing
    ``media.ratings_json`` blob, overwriting only the given source keys --
    every other already-recorded source's entry is preserved untouched
    (T15 fix wave 1, item 3a: "MERGE keys, never replace the whole JSON").
    An unparsable/missing ``existing_json`` is treated as ``{}``."""
    try:
        existing = json.loads(existing_json) if existing_json else {}
    except (TypeError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing.update(new_entries)
    return json.dumps(existing, ensure_ascii=False)


def ratings_status_for(
    ratings: Mapping[str, object], *, imdb_id: str | None, imdb_row_present: bool, tvmaze_id: int | None,
) -> str:
    """T15 fix wave 1 (item 3b): ``complete`` only when every APPLICABLE
    source is present in ``ratings`` -- ``tmdb`` is always applicable;
    ``imdb`` only when ``imdb_id`` is known AND a matching ``imdb_ratings``
    row actually exists (an unknown id, or a known id with no local ratings
    row, is simply not applicable -- never counted as "missing"); ``tvmaze``
    -- fix wave 2 (item 3), mirroring the imdb rule -- only when
    ``tvmaze_id`` is set AND ``ratings`` actually carries a ``tvmaze``
    value (a known tvmaze_id whose show has no rating is simply not
    applicable, never counted as permanently "missing"). ``partial`` when
    some but not all applicable sources are present, ``none`` when none
    are. (``error`` is a distinct status this function never returns -- it
    is only ever set directly by a caller on a failed fetch, and only when
    there is nothing already-valid to protect; see ``RATINGS_FETCH_ERROR_
    CLASS``.)"""
    applicable = {"tmdb"}
    if imdb_id and imdb_row_present:
        applicable.add("imdb")
    if tvmaze_id and ratings.get("tvmaze"):
        applicable.add("tvmaze")
    present = {key for key in applicable if ratings.get(key)}
    if not present:
        return "none"
    if present >= applicable:
        return "complete"
    return "partial"


def lookup_imdb_rating(conn: sqlite3.Connection, imdb_id: str) -> dict | None:
    """Read one row from the offline-populated ``imdb_ratings`` table (T15
    addendum §14.3 item 2) -- ``{"score", "scale": 10, "votes", "as_of"}``
    for ``ratings_json.imdb``, or ``None`` when the table doesn't exist yet
    (an old bundle) or has no row for this id."""
    try:
        row = conn.execute("SELECT rating, votes, as_of FROM imdb_ratings WHERE imdb_id=?", (imdb_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row["rating"] is None:
        return None
    return {"score": row["rating"], "scale": 10, "votes": row["votes"], "as_of": row["as_of"]}


def _write_exact(
    store: "LibraryStore", client: TmdbClient, media_id: int, judgement: Judgement, with_details: bool,
    *, hint_explanation: dict | None = None,
) -> None:
    """Write an ``exact`` match: tmdb_id/media_type/title_original/overview/
    poster/backdrop/genres (§6.2 step 5).  ``media_identity`` is deliberately
    left untouched here -- ``merge_by_tmdb`` is the only place that ever
    changes it, so it is the only place that has to reason about the
    UNIQUE(media_identity) constraint when two media rows end up matched to
    the same tmdb id.

    T15 addendum: also captures ``ratings_json``/``ratings_status`` from
    the winning candidate's TMDB vote fields (§14.3 item 1), and -- only
    when this exact match came through the IMDb-hint/TVmaze pathway
    (``hint_explanation`` given) -- records ``imdb_id`` and the pathway's
    explanation into ``match_candidates_json`` (§design item 2: "no schema
    change to media"; normally ``NULL`` for a plain search-flow exact,
    unchanged from before this addendum).

    T15 fix wave 1 (item 3a): when ``hint_explanation`` carries an
    ``imdb_id`` (either pathway), ``imdb_ratings`` is joined and merged into
    ``ratings_json.imdb``; when it is the ``tvmaze`` pathway confirming a
    ``tv`` row, ``media.tvmaze_id`` is set and ``ratings_json.tvmaze`` is
    populated from the confirming show's own ``rating.average`` (``votes``
    always ``null`` -- TVmaze's public search payload has no vote count).
    ``ratings_json`` is MERGED onto whatever was already there (normally
    nothing yet, since this is the row's first ``exact`` write) rather than
    replaced outright, and ``ratings_status`` (item 3b) reflects every
    source ``ratings_status_for`` finds applicable, not just TMDB.
    """
    top = judgement.candidates[0]
    overview, poster_path, backdrop_path = top.overview, top.poster_path, top.backdrop_path
    vote_average, vote_count = top.vote_average, top.vote_count

    genre_map = client.genres(judgement.media_type)
    genres_json = json.dumps([genre_map[g] for g in top.genre_ids if g in genre_map], ensure_ascii=False)

    if with_details:
        detail_entry = client.details(judgement.media_type, judgement.tmdb_id)
        if detail_entry.status == "ok" and detail_entry.payload:
            detail = detail_entry.payload[0]
            overview = detail.get("overview") or overview
            poster_path = detail.get("poster_path") or poster_path
            backdrop_path = detail.get("backdrop_path") or backdrop_path
            vote_average = detail.get("vote_average") if detail.get("vote_average") is not None else vote_average
            vote_count = detail.get("vote_count") if detail.get("vote_count") is not None else vote_count
            detail_genres = detail.get("genres") or []
            if detail_genres:
                genres_json = json.dumps([g["name"] for g in detail_genres if "name" in g], ensure_ascii=False)

    match_candidates_json = json.dumps(hint_explanation, ensure_ascii=False) if hint_explanation is not None else None
    imdb_id = hint_explanation.get("imdb_id") if hint_explanation is not None else None
    is_tvmaze = hint_explanation is not None and hint_explanation.get("source") == "tvmaze"
    tvmaze_id = hint_explanation.get("tvmaze_id") if is_tvmaze else None

    now = int(time.time())
    conn = store.connect()
    try:
        prior = conn.execute("SELECT ratings_json, tvmaze_id FROM media WHERE id=?", (media_id,)).fetchone()
        prior_ratings_json = prior["ratings_json"] if prior else None
        prior_tvmaze_id = prior["tvmaze_id"] if prior else None

        new_ratings: dict = {}
        tmdb_entry = _tmdb_rating_entry(vote_average, vote_count)
        if tmdb_entry is not None:
            new_ratings["tmdb"] = tmdb_entry

        imdb_row_present = False
        if imdb_id:
            imdb_entry = lookup_imdb_rating(conn, imdb_id)
            imdb_row_present = imdb_entry is not None
            if imdb_entry is not None:
                new_ratings["imdb"] = imdb_entry

        if is_tvmaze and judgement.media_type == "tv":
            tvmaze_rating = hint_explanation.get("tvmaze_rating")
            if tvmaze_rating is not None:
                new_ratings["tvmaze"] = {"score": tvmaze_rating, "scale": 10, "votes": None, "as_of": _default_today()}

        ratings_json = merge_ratings_json(prior_ratings_json, new_ratings)
        merged_ratings = json.loads(ratings_json)
        # Fix wave 2 (item 3): consider the row's ALREADY-persisted
        # tvmaze_id too, not just this write's own hint pathway -- a row
        # confirmed via tvmaze earlier keeps its tvmaze_id (COALESCE below)
        # and its ratings_json.tvmaze entry (merge above) through a later,
        # non-tvmaze exact write, so status here must too, rather than
        # silently reverting to tmdb/imdb-only applicability.
        effective_tvmaze_id = tvmaze_id or prior_tvmaze_id
        ratings_status = ratings_status_for(merged_ratings, imdb_id=imdb_id, imdb_row_present=imdb_row_present, tvmaze_id=effective_tvmaze_id)

        conn.execute(
            """
            UPDATE media SET
                tmdb_id = ?, media_type = ?, title_original = ?, overview = ?,
                poster_path = ?, backdrop_path = ?, genres_json = ?,
                match_status = 'exact', match_score = ?, match_candidates_json = ?,
                ratings_json = ?, ratings_status = ?, ratings_fetched_at = ?,
                imdb_id = COALESCE(?, imdb_id), tvmaze_id = COALESCE(?, tvmaze_id),
                updated_at = ?
            WHERE id = ?
            """,
            (
                judgement.tmdb_id, judgement.media_type, top.original_title, overview,
                poster_path, backdrop_path, genres_json, judgement.score, match_candidates_json,
                ratings_json, ratings_status, _default_today(),
                imdb_id, tvmaze_id,
                now, media_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _write_candidate(store: "LibraryStore", media_id: int, judgement: Judgement, *, hint_explanation: dict | None = None) -> None:
    """Write a ``candidate`` match: only tmdb_id/match_score/
    match_candidates_json -- title, identity and every other metadata field
    are left exactly as imported (§6.2 step 5)."""
    now = int(time.time())
    conn = store.connect()
    try:
        conn.execute(
            """
            UPDATE media SET tmdb_id = ?, match_status = 'candidate', match_score = ?,
                match_candidates_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (judgement.tmdb_id, judgement.score, _candidates_json_with_explanation(judgement.candidates, hint_explanation), now, media_id),
        )
        conn.commit()
    finally:
        conn.close()


def _write_needs_review(store: "LibraryStore", media_id: int, judgement: Judgement, *, hint_explanation: dict | None = None) -> None:
    """Write a ``needs_review`` match: match_score/match_candidates_json
    (top 5); no tmdb_id, title or identity change (§6.2 step 5)."""
    now = int(time.time())
    conn = store.connect()
    try:
        conn.execute(
            """
            UPDATE media SET match_status = 'needs_review', match_score = ?,
                match_candidates_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (judgement.score, _candidates_json_with_explanation(judgement.candidates, hint_explanation), now, media_id),
        )
        conn.commit()
    finally:
        conn.close()


_CANDIDATE_SCAN_MULTIPLIER = 5


def _primary_cache_key(media_type: str, title_zh: str, year: int | None) -> str:
    """The cache key ``_run_search``'s primary (non-alias) search call
    would use for this media row -- ``_judge_media`` always searches
    ``title_zh`` first, at ``search``'s default ``language="zh-CN"``."""
    return search_cache_key(_search_kind(media_type), "zh-CN", _fold(title_zh), year)


def _media_has_resolved_cache(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> bool:
    """True when ``row``'s primary TMDB cache entry already holds a valid
    verdict per ``_cached_entry_is_usable`` (``ok``/``empty``/
    ``failed_permanent``, or ``failed_retryable`` still cooling down).
    Such media would just re-hit the cache and get re-judged the same way
    forever, so the candidate queue must stop re-selecting them (they can
    stay ``match_status='unmatched'`` indefinitely -- ``unmatched`` never
    gets written back to the row)."""
    cache_key = _primary_cache_key(row["media_type"], row["title_zh"], row["year"])
    cache_row = conn.execute(
        "SELECT status, retry_after FROM tmdb_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    if cache_row is None:
        return False
    entry = CacheEntry(cache_key, cache_row["status"], None, None, None, 0, cache_row["retry_after"], None)
    return _cached_entry_is_usable(entry, now)


def _media_search_is_failing(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> bool:
    """T14 fix wave 3: True when ``row``'s primary TMDB cache entry is
    currently a search *failure* that a fresh attempt would just repeat
    right now -- ``failed_permanent`` (never expires) or a still-cooling
    ``failed_retryable`` (before its ``retry_after``); a ``failed_retryable``
    whose cooldown has already elapsed is NOT failing here (it's eligible
    for a genuine retry).

    Unlike ``_media_has_resolved_cache`` (used by ``_select_fresh_candidates``
    for the primary ``unmatched`` queue), an ``ok``/``empty`` cache entry
    does not count here: a §5.1 review-pending row always gets its own
    verdict written on its first genuine attempt (see
    ``_process_candidate_row``), even when that verdict is empty, so it
    falls out of ``_select_review_pending_unqueried``'s SQL predicate on
    its own -- no skip is needed (or wanted -- skipping it would leave a
    row that merely shares a cache key with an already-searched title
    stuck ``match_score IS NULL`` forever, since nothing else would ever
    give it its own verdict)."""
    cache_key = _primary_cache_key(row["media_type"], row["title_zh"], row["year"])
    cache_row = conn.execute(
        "SELECT status, retry_after FROM tmdb_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    if cache_row is None or cache_row["status"] not in ("failed_permanent", "failed_retryable"):
        return False
    entry = CacheEntry(cache_key, cache_row["status"], None, None, None, 0, cache_row["retry_after"], None)
    return _cached_entry_is_usable(entry, now)


def _select_fresh_candidates(conn: sqlite3.Connection, limit: int) -> tuple[list[sqlite3.Row], bool]:
    """Page through ``match_status = 'unmatched'`` media in §6.2 step-1
    order (``has_115 DESC, link_count DESC, id``), skipping any row whose
    primary search cache entry is already resolved (see
    ``_media_has_resolved_cache``), and collect up to ``limit`` fresh ones.
    Pages are read in chunks of ``limit * _CANDIDATE_SCAN_MULTIPLIER`` so a
    long run of resolved-but-still-``unmatched`` rows doesn't require one
    SQL round-trip per row.  This is a read-only scan -- nothing is written
    until after it returns -- so paging by ``OFFSET`` is safe even though
    ``match_status`` for earlier rows changes once the caller starts
    processing the returned candidates.

    Returns ``(fresh_rows, exhausted)``; ``exhausted`` is True only when the
    scan reached the end of the table without finding ``limit`` fresh rows
    (i.e. no fresh candidate remains right now) -- it is conservatively
    False when the scan stopped early because ``limit`` was already met.
    """
    chunk_size = max(limit * _CANDIDATE_SCAN_MULTIPLIER, 1)
    offset = 0
    fresh: list[sqlite3.Row] = []
    while len(fresh) < limit:
        page = conn.execute(
            "SELECT id, media_identity, media_type, title_zh, title_alt_json, year FROM media "
            "WHERE match_status = 'unmatched' "
            "ORDER BY has_115 DESC, link_count DESC, id LIMIT ? OFFSET ?",
            (chunk_size, offset),
        ).fetchall()
        if not page:
            return fresh, True
        offset += len(page)
        now = _epoch_now()
        for row in page:
            if _media_has_resolved_cache(conn, row, now):
                continue
            fresh.append(row)
            if len(fresh) == limit:
                return fresh, False
        if len(page) < chunk_size:
            return fresh, True
    return fresh, False


# T14/§5.1: the safe requeue of `needs_review` media that has never been
# queried against TMDB at all (no match_score, no candidates JSON) -- as
# opposed to the 3 already-scored `needs_review` rows in the production
# snapshot, which must never be re-selected here.
REVIEW_PENDING_UNQUERIED = "review_pending_unqueried"


def _select_review_pending_unqueried(
    conn: sqlite3.Connection, limit: int, *, exclude_ids: frozenset[int] = frozenset()
) -> tuple[list[sqlite3.Row], bool]:
    """Page through never-queried ``needs_review`` media (§5.1: ``match_status
    = 'needs_review' AND match_score IS NULL AND (match_candidates_json IS
    NULL OR = '')``), same order and paging strategy as
    ``_select_fresh_candidates``. ``exclude_ids`` skips media already
    recorded in the caller's own resume batch (the
    ``--library-requeue-review --resume`` CLI flag) *and* (T14 fix wave 2/
    finding #1) media the caller's own in-run exclusion set already saw a
    TMDB search fail on earlier in this same invocation.

    A row that gets a genuine verdict (even an empty-candidates one)
    always has its ``match_score`` written before the caller moves on, so
    it falls out of the SQL predicate above on its own -- no cache-based
    skip is needed for that case (unlike ``_select_fresh_candidates``).
    But a row whose search *fails* (transient or permanent) is left with
    ``match_score`` still NULL on purpose (see ``_process_candidate_row``),
    so without ``exclude_ids`` it would keep being selected first (by
    ``has_115 DESC, link_count DESC, id``) forever, or at least for the
    rest of one invocation's internal batch loop -- that's what
    ``exclude_ids`` is for here.

    T14 fix wave 3: ``exclude_ids`` only ever covers *this invocation's*
    own failures -- a later invocation (a fresh, empty ``exclude_ids``,
    e.g. the next ``BackgroundEnricher`` round) used to reselect the same
    still-failing row every time, occupying a phase-2 slot forever, since
    its cache entry (``failed_permanent``, or a ``failed_retryable`` still
    inside its ``retry_after`` cooldown) would just be re-hit and fail the
    same way again. ``_media_search_is_failing`` now skips such a row
    regardless of ``exclude_ids`` -- a ``failed_retryable`` becomes
    selectable again on its own once its cooldown elapses (a genuine
    re-attempt), same as ``failed_permanent`` never becomes selectable
    again short of the cache entry itself changing.

    Returns ``(rows, exhausted)`` with the same conservative-False-on-early-
    stop contract as ``_select_fresh_candidates``.
    """
    if limit <= 0:
        return [], False
    chunk_size = max(limit * _CANDIDATE_SCAN_MULTIPLIER, 1)
    offset = 0
    fresh: list[sqlite3.Row] = []
    while len(fresh) < limit:
        page = conn.execute(
            "SELECT id, media_identity, media_type, title_zh, title_alt_json, year FROM media "
            "WHERE match_status = 'needs_review' AND match_score IS NULL "
            "AND (match_candidates_json IS NULL OR match_candidates_json = '') "
            "ORDER BY has_115 DESC, link_count DESC, id LIMIT ? OFFSET ?",
            (chunk_size, offset),
        ).fetchall()
        if not page:
            return fresh, True
        offset += len(page)
        now = _epoch_now()
        for row in page:
            if row["id"] in exclude_ids:
                continue
            if _media_search_is_failing(conn, row, now):
                continue
            fresh.append(row)
            if len(fresh) == limit:
                return fresh, False
        if len(page) < chunk_size:
            return fresh, True
    return fresh, False


def _write_requeue_audit_row(store: "LibraryStore", media_id: int, batch_id: str) -> None:
    """Record one review-pending-unqueried media id as queued for
    ``batch_id`` -- a sanitized audit trail (media id + prior status/reason
    + batch/time -- never a title or link, §5.1). Called immediately before
    that one row is judged (fix wave 1: previously a whole up-to-100-row
    batch was written up front, so an aborted batch left every
    not-yet-judged row looking "already handled" to ``--resume``).
    ``processed_at`` is left NULL until ``_mark_requeue_audit_processed``
    runs right after this row's verdict is written. ``INSERT OR IGNORE`` +
    the table's ``UNIQUE(batch_id, media_id)`` makes a repeated call for the
    same row in the same batch (e.g. a non-``--resume`` rerun re-selecting a
    row whose previous attempt was queued but never reached a verdict) a
    no-op instead of a duplicate row."""
    conn = store.connect()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT OR IGNORE INTO tmdb_requeue_audit "
            "(media_id, prev_status, prev_review_reason, batch_id, queued_at) "
            "VALUES (?, 'needs_review', ?, ?, ?)",
            (media_id, REVIEW_PENDING_UNQUERIED, batch_id, _epoch_now()),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_requeue_audit_processed(store: "LibraryStore", media_id: int, batch_id: str) -> None:
    """Set ``processed_at`` on the audit row ``_write_requeue_audit_row``
    wrote for ``media_id``/``batch_id``, immediately after that row's
    verdict was written -- the signal ``processed_media_ids_for_batch``
    (``--resume``) relies on to tell "queued" apart from "actually judged"."""
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE tmdb_requeue_audit SET processed_at = ? WHERE batch_id = ? AND media_id = ?",
            (_epoch_now(), batch_id, media_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_requeue_audit_failed(store: "LibraryStore", media_id: int, batch_id: str, error_class: str | None) -> None:
    """Record a transient/permanent TMDB search failure on the audit row
    ``_write_requeue_audit_row`` wrote for ``media_id``/``batch_id`` (T14
    fix wave 2/finding #1). ``processed_at`` is deliberately left untouched
    (stays NULL) -- the row was never judged, so ``--resume``
    (``processed_media_ids_for_batch``) must still retry it; this only
    records why, for observability."""
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE tmdb_requeue_audit SET last_error_class = ? WHERE batch_id = ? AND media_id = ?",
            (error_class, batch_id, media_id),
        )
        conn.commit()
    finally:
        conn.close()


def audited_media_ids_for_batch(conn: sqlite3.Connection, batch_id: str) -> frozenset[int]:
    """The distinct media ids recorded in ``tmdb_requeue_audit`` for
    ``batch_id`` -- queued or processed, i.e. every row a requeue attempt
    ever selected today, regardless of whether it reached a verdict.  Backs
    the ``--library-requeue-review --dry-run`` CLI flag's "audited_today"
    diagnostic count; ``--resume`` itself uses the stricter
    ``processed_media_ids_for_batch`` below. Read-only: unlike most other
    callers in this module it never calls ``ensure_tables`` (a readonly
    connection can't create tables) -- a not-yet-existing table (nothing
    has ever been requeued) simply reports no audited media, same as an
    empty one."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT media_id FROM tmdb_requeue_audit WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return frozenset()
    return frozenset(row[0] for row in rows)


def processed_media_ids_for_batch(conn: sqlite3.Connection, batch_id: str) -> frozenset[int]:
    """The distinct media ids in ``tmdb_requeue_audit`` for ``batch_id``
    that were actually judged (``processed_at IS NOT NULL``) -- backs
    ``--library-requeue-review --resume`` (fix wave 1): a media id that was
    only *queued* before the batch aborted (e.g. ``BudgetExhausted`` mid-row)
    must still be picked up again on the next run, or an interrupted batch
    would silently skip up to one call's worth of never-judged rows
    forever. Read-only, same missing-table tolerance as
    ``audited_media_ids_for_batch``."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT media_id FROM tmdb_requeue_audit WHERE batch_id = ? AND processed_at IS NOT NULL",
            (batch_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return frozenset()
    return frozenset(row[0] for row in rows)


def _default_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _process_candidate_row(
    store: "LibraryStore", client: TmdbClient, stats: "EnrichStats", row: sqlite3.Row, *,
    with_details: bool, prev_status: str, batch_id: str,
    hints: Mapping[str, dict] = _EMPTY_HINTS, tvmaze_enabled: bool = False,
) -> bool:
    """Judge and write one candidate row, updating ``stats``. ``prev_status``
    is ``"unmatched"`` for the normal candidate queue (§6.2: an ``unmatched``
    verdict is left unwritten, same as before) or ``"needs_review"`` for the
    §5.1 review-pending-unqueried queue, where a fresh ``unmatched`` verdict
    must still be written back (as ``needs_review`` with an empty candidate
    list) rather than silently left ``match_score IS NULL`` forever, or
    bulk-flipped to ``unmatched`` and losing provenance.

    Returns ``True`` when the row reached a genuine verdict (written or,
    for the primary ``unmatched`` queue, a real ``unmatched`` left
    unwritten), ``False`` when the underlying TMDB search failed
    (``_judge_media`` returned no ``Judgement``) -- T14 fix wave 2/
    finding #1: such a row is left completely untouched (``match_score``
    stays whatever it already was) rather than retired as "queried, no
    candidate"; for the review-pending queue the failure class is recorded
    on its audit row via ``_mark_requeue_audit_failed`` (``processed_at``
    stays NULL, so ``--resume`` retries it) instead of
    ``_mark_requeue_audit_processed``.

    Fix wave 1: for a review-pending row (``prev_status == "needs_review"``,
    ``batch_id`` always given by the caller in that case), the audit row is
    written immediately before ``_judge_media`` runs -- still before any
    status write -- and marked ``processed_at`` immediately after the
    verdict is written below. If ``_judge_media`` raises
    ``BudgetExhausted``/``InvalidApiKey`` it propagates before that mark
    runs, leaving the audit row queued-but-unprocessed so ``--resume``
    (``processed_media_ids_for_batch``) picks the row up again."""
    is_review_pending = prev_status == "needs_review"
    if is_review_pending:
        _write_requeue_audit_row(store, row["id"], batch_id)
    hint = hints.get(row["media_identity"])
    judgement, error_class, hint_explanation = _judge_media_full(client, stats, row, hint, tvmaze_enabled=tvmaze_enabled)
    if judgement is None:
        if is_review_pending:
            _mark_requeue_audit_failed(store, row["id"], batch_id, error_class)
        return False
    if hint_explanation is not None and hint_explanation.get("source") == "imdb_hint":
        if judgement.status == "exact":
            stats.hint_confirmed_exact += 1
        elif judgement.status == "candidate":
            stats.hint_candidate += 1
        elif judgement.status == "needs_review":
            stats.hint_conflicts += 1
    if judgement.status == "exact":
        _write_exact(store, client, row["id"], judgement, with_details, hint_explanation=hint_explanation)
        stats.matched_exact += 1
    elif judgement.status == "candidate":
        _write_candidate(store, row["id"], judgement, hint_explanation=hint_explanation)
        stats.matched_candidate += 1
    elif judgement.status == "needs_review" or prev_status == "needs_review":
        _write_needs_review(store, row["id"], judgement, hint_explanation=hint_explanation)
        stats.matched_needs_review += 1
    else:
        stats.matched_unmatched += 1
    if is_review_pending:
        _mark_requeue_audit_processed(store, row["id"], batch_id)
    return True


def _load_hints_for_queue(conn: sqlite3.Connection, identities: Sequence[str]) -> dict[str, dict]:
    """Batch-fetch ``tmdb_hints`` rows for a whole selected candidate queue
    in one SQL round-trip (rather than one query per row), keyed by
    ``media_identity``. Tolerates an installed library that predates T15
    (no ``tmdb_hints`` table yet) by reporting no hints at all -- the
    hint/find pathway then simply never engages for this round, degrading
    to the pre-T15 normal search flow, same as a genuinely hint-less media
    row."""
    identities = [identity for identity in identities if identity]
    if not identities:
        return {}
    placeholders = ",".join("?" for _ in identities)
    try:
        rows = conn.execute(
            f"SELECT media_identity, source, decision, candidate_count, candidates_json, generated_at "
            f"FROM tmdb_hints WHERE media_identity IN ({placeholders})",
            identities,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    hints: dict[str, dict] = {}
    for row in rows:
        try:
            candidates = json.loads(row["candidates_json"])
        except (TypeError, ValueError):
            candidates = []
        hints[row["media_identity"]] = {
            "source": row["source"], "decision": row["decision"],
            "candidate_count": row["candidate_count"], "candidates": candidates,
            "generated_at": row["generated_at"],
        }
    return hints


def _run_candidate_queue(
    store: "LibraryStore",
    client: TmdbClient,
    stats: "EnrichStats",
    queue: Sequence[tuple[sqlite3.Row, str]],
    *,
    with_details: bool,
    deadline: float | None,
    batch_id: str,
    hints: Mapping[str, dict] = _EMPTY_HINTS,
    tvmaze_enabled: bool = False,
) -> None:
    """Shared per-row loop for ``enrich_batch`` and ``requeue_review_batch``:
    checks ``deadline`` before starting each row, judges/writes it via
    ``_process_candidate_row`` (passing ``batch_id`` through for the
    per-row audit write/mark), and stops the whole queue (recording
    ``stats.error_class``) on ``BudgetExhausted``/``InvalidApiKey`` or the
    deadline -- see ``enrich_batch``'s own docstring for the exact contract,
    which this preserves unchanged. A row ``_process_candidate_row`` reports
    as not-judged (a TMDB search failure, T14 fix wave 2/finding #1) has its
    media id appended to ``stats.search_failed_media_ids`` instead of being
    silently dropped."""
    for idx, (row, prev_status) in enumerate(queue):
        if deadline is not None and time.monotonic() >= deadline:
            stats.candidates_considered = idx
            stats.error_class = "DeadlineExceeded"
            return
        try:
            judged = _process_candidate_row(
                store, client, stats, row, with_details=with_details, prev_status=prev_status, batch_id=batch_id,
                hints=hints, tvmaze_enabled=tvmaze_enabled,
            )
            if not judged:
                stats.search_failed_media_ids.append(row["id"])
        except (BudgetExhausted, InvalidApiKey) as exc:
            stats.error_class = type(exc).__name__
            return


def enrich_batch(
    store: "LibraryStore", client: TmdbClient, *, limit: int = 20, with_details: bool = False,
    deadline: float | None = None, today: Callable[[], str] = _default_today,
    tvmaze_enabled: bool = False,
) -> EnrichStats:
    """Process up to ``limit`` unmatched media through TMDB search, scoring
    and the §6.2 step-5 write rules.  Shared by the offline script and
    ``BackgroundEnricher``.

    Candidate order: ``match_status = 'unmatched'`` *and no valid cached
    TMDB verdict yet* (see ``_select_fresh_candidates``), ``has_115 DESC,
    link_count DESC, id`` (§6.2 step 1) -- a media whose primary search
    already came back ``ok``/``empty``/``failed_permanent`` (or a still
    cooling-down ``failed_retryable``) is skipped instead of being
    re-selected forever, since ``unmatched`` never gets written back to the
    row.  Requests for the same ``(kind, query, year)`` are deduplicated for
    free by ``TmdbClient.search``'s own cache -- a second media row (or a
    second call to this function, i.e. ``--resume``) with the same key never
    issues a new HTTP request.  A ``BudgetExhausted``/``InvalidApiKey``
    raised at any point (search, genre lookup or detail fetch) stops the
    batch immediately without writing the in-progress row, and is recorded
    on ``EnrichStats.error_class`` rather than propagated -- the row stays
    ``unmatched`` and is retried on the next call.

    T14/§5.1: when that ``unmatched`` queue is exhausted for this round
    (i.e. no fresh unmatched candidate remains right now) and ``limit``
    hasn't been filled yet, the remaining slots are filled from the
    never-queried-``needs_review`` backlog (see
    ``_select_review_pending_unqueried``) -- an audit row is written for
    each such media immediately before *that row* is judged (fix wave 1:
    ``_write_requeue_audit_row``/``_mark_requeue_audit_processed``, inside
    ``_process_candidate_row``, ``batch_id = today()``; not for the whole
    phase-2 selection up front, so an aborted round never leaves
    not-yet-judged rows looking done to ``--resume``), and its outcome
    always keeps ``match_status='needs_review'`` unless the fresh judgement
    is ``exact``/``candidate`` (see ``_process_candidate_row``): provenance
    is never bulk-flipped to ``unmatched``. ``EnrichStats.
    candidates_considered`` counts fresh rows from *both* phases combined;
    ``EnrichStats.queue_exhausted`` only ever reflects the primary
    ``unmatched`` queue (unchanged contract), since that's what gates
    whether this phase runs at all.

    ``deadline`` (``time.monotonic()`` seconds, T10 fix wave 1) is an
    optional wall-clock cutoff checked before *starting* each candidate
    (never mid-candidate): once reached, the batch stops, ``error_class`` is
    set to ``"DeadlineExceeded"`` and ``candidates_considered`` is trimmed
    down to the number of candidates actually started so far (instead of
    the full pre-selected count) so callers can see the round was cut short.
    ``None`` (the default) never stops the batch -- the background thread
    passes no deadline, since it already paces itself between rounds; only
    the synchronous ``tmdb-enrich-now`` HTTP route uses one, to keep a
    request that hits a string of slow/failing candidates from compounding
    past gunicorn's worker timeout.

    ``EnrichStats.http_429`` (T14 fix wave 2) is diffed once around the
    whole call via ``client.http_429_responses``, so a 429 seen by
    ``client.genres()``/``client.details()`` (only reached from
    ``_write_exact``, when an ``exact`` verdict is written) is counted too,
    not just one seen by a search.

    T15: before judging normally, every selected row is checked against
    ``tmdb_hints`` (Codex's offline IMDb candidates, keyed by
    ``media_identity`` -- see ``_load_hints_for_queue``/
    ``_judge_media_with_hint``) for a ``/find``-confirmation shortcut, and
    (``tvmaze_enabled``, default off) a ``tv`` media that still ends up
    genuinely unmatched gets one TVmaze fallback attempt
    (``_judge_media_full``).
    """
    stats = EnrichStats()
    started = time.monotonic()
    before_429 = client.http_429_responses

    conn = store.connect()
    try:
        ensure_tables(conn)
        rows, exhausted = _select_fresh_candidates(conn, limit)
        review_rows: list[sqlite3.Row] = []
        if exhausted and len(rows) < limit:
            review_rows, _review_exhausted = _select_review_pending_unqueried(conn, limit - len(rows))
        hints = _load_hints_for_queue(conn, [row["media_identity"] for row in (*rows, *review_rows)])
    finally:
        conn.close()
    stats.candidates_considered = len(rows) + len(review_rows)
    stats.queue_exhausted = exhausted

    queue = [(row, "unmatched") for row in rows] + [(row, "needs_review") for row in review_rows]
    _run_candidate_queue(
        store, client, stats, queue, with_details=with_details, deadline=deadline, batch_id=today(),
        hints=hints, tvmaze_enabled=tvmaze_enabled,
    )

    stats.http_429 = client.http_429_responses - before_429
    stats.budget_status = client.budget_status()
    stats.elapsed_seconds = time.monotonic() - started
    return stats


def verify_media_ids(
    store: "LibraryStore", client: TmdbClient, media_ids: Sequence[int], *,
    with_details: bool = False, tvmaze_enabled: bool = False, deadline: float | None = None,
    today: Callable[[], str] = _default_today,
) -> EnrichStats:
    """Identity-confirm SPECIFIC media rows by id (T15 addendum §5.1: the
    ``media_metadata_backfill.py --phase verify --ids`` selection) --
    reuses the exact same hint/search/TVmaze judge and write rules as
    ``enrich_batch`` (``_judge_media_full``/``_process_candidate_row`` via
    ``_run_candidate_queue``), only the row *selection* differs (explicit
    ids rather than the has_115/link_count queue order). A row already
    ``match_status='exact'`` is skipped -- there is nothing to verify.
    ``prev_status`` for each row is its own current ``match_status``, so a
    ``needs_review`` row still gets the same audit-trail bookkeeping as
    ``enrich_batch``'s phase-2 queue, and a row that comes back
    ``unmatched`` is left completely untouched either way (never
    downgraded from ``candidate``)."""
    stats = EnrichStats()
    started = time.monotonic()
    before_429 = client.http_429_responses

    conn = store.connect()
    try:
        ensure_tables(conn)
        rows: list[sqlite3.Row] = []
        if media_ids:
            placeholders = ",".join("?" for _ in media_ids)
            rows = [
                row for row in conn.execute(
                    f"SELECT id, media_identity, media_type, title_zh, title_alt_json, year, match_status "
                    f"FROM media WHERE id IN ({placeholders})",
                    list(media_ids),
                ).fetchall()
                if row["match_status"] != "exact"
            ]
        hints = _load_hints_for_queue(conn, [row["media_identity"] for row in rows])
    finally:
        conn.close()
    stats.candidates_considered = len(rows)

    queue = [(row, row["match_status"]) for row in rows]
    _run_candidate_queue(
        store, client, stats, queue, with_details=with_details, deadline=deadline, batch_id=today(),
        hints=hints, tvmaze_enabled=tvmaze_enabled,
    )

    stats.http_429 = client.http_429_responses - before_429
    stats.budget_status = client.budget_status()
    stats.elapsed_seconds = time.monotonic() - started
    return stats


def requeue_review_batch(
    store: "LibraryStore", client: TmdbClient, *, limit: int = 100,
    exclude_ids: frozenset[int] = frozenset(), with_details: bool = False,
    deadline: float | None = None, today: Callable[[], str] = _default_today,
    tvmaze_enabled: bool = False,
) -> EnrichStats:
    """One batch of the T14/§5.1 review-pending-unqueried requeue -- backs
    the ``--library-requeue-review`` CLI. Unlike ``enrich_batch``, this
    processes ONLY that phase (never the ``unmatched`` queue): selects up to
    ``limit`` never-queried ``needs_review`` rows (see
    ``_select_review_pending_unqueried``; the CLI caps ``limit`` at 100 per
    call), then judges each with the same rules and write outcomes as
    ``enrich_batch``'s own phase-2 branch (``_process_candidate_row`` with
    ``prev_status="needs_review"``) -- which writes/marks that row's own
    audit record immediately before/after judging it (fix wave 1), not for
    the whole selected batch up front.
    ``exclude_ids`` -- media already *judged* in today's audit batch (see
    ``processed_media_ids_for_batch``) -- is how the CLI's ``--resume`` flag
    skips media a previous, interrupted invocation already finished today;
    a row that was only queued (not judged) before that invocation aborted
    is deliberately re-selected. It also carries the CLI's in-run exclusion
    set (T14 fix wave 2/finding #1): media ids this same invocation's
    earlier internal batch already saw fail with a TMDB search failure
    (``EnrichStats.search_failed_media_ids``), so this batch doesn't keep
    re-selecting them.

    ``EnrichStats.http_429`` (T14 fix wave 2) is diffed once around the
    whole call, same as ``enrich_batch``.
    """
    stats = EnrichStats()
    started = time.monotonic()
    before_429 = client.http_429_responses

    conn = store.connect()
    try:
        ensure_tables(conn)
        rows, exhausted = _select_review_pending_unqueried(conn, limit, exclude_ids=exclude_ids)
        hints = _load_hints_for_queue(conn, [row["media_identity"] for row in rows])
    finally:
        conn.close()
    stats.candidates_considered = len(rows)
    stats.queue_exhausted = exhausted

    queue = [(row, "needs_review") for row in rows]
    _run_candidate_queue(
        store, client, stats, queue, with_details=with_details, deadline=deadline, batch_id=today(),
        hints=hints, tvmaze_enabled=tvmaze_enabled,
    )

    stats.http_429 = client.http_429_responses - before_429
    stats.budget_status = client.budget_status()
    stats.elapsed_seconds = time.monotonic() - started
    return stats


def _merge_media_rows(conn: sqlite3.Connection, keeper_id: int, dup_id: int, now: int) -> None:
    """Fold ``dup_id`` into ``keeper_id``: repoint each of its resource
    groups (or, on a ``UNIQUE(media_id, edition_fingerprint)`` conflict with
    a group ``keeper_id`` already has, repoint its links onto that existing
    group and drop the now-empty duplicate group), then delete the
    duplicate media row.  Groups are moved before links are merged and the
    media row is deleted last, so every step stays foreign-key-safe."""
    dup_groups = conn.execute(
        "SELECT id, edition_fingerprint FROM resource_group WHERE media_id = ?", (dup_id,)
    ).fetchall()
    for dup_group in dup_groups:
        keeper_group = conn.execute(
            "SELECT id FROM resource_group WHERE media_id = ? AND edition_fingerprint = ?",
            (keeper_id, dup_group["edition_fingerprint"]),
        ).fetchone()
        if keeper_group is None:
            conn.execute(
                "UPDATE resource_group SET media_id = ?, updated_at = ? WHERE id = ?",
                (keeper_id, now, dup_group["id"]),
            )
        else:
            conn.execute("UPDATE resource_link SET group_id = ? WHERE group_id = ?", (keeper_group["id"], dup_group["id"]))
            conn.execute("DELETE FROM resource_group WHERE id = ?", (dup_group["id"],))
    conn.execute("DELETE FROM media WHERE id = ?", (dup_id,))


def merge_by_tmdb(store: "LibraryStore") -> int:
    """Identity merge (§6.2 step 6): group every ``exact``-matched media row
    by ``(media_type, tmdb_id)``, upgrade the earliest row in each group to
    ``media_identity = "tmdb:<type>:<id>"`` (a singleton group is just an
    identity upgrade, no merge), and fold any later rows into it via
    ``_merge_media_rows``.  Returns the number of media rows merged away
    this call.  Idempotent: a second call finds groups of size 1 (their
    identity already upgraded) and returns 0.
    """
    conn = store.connect()
    merged = 0
    try:
        rows = conn.execute(
            "SELECT id, media_type, tmdb_id FROM media "
            "WHERE match_status = 'exact' AND tmdb_id IS NOT NULL ORDER BY id"
        ).fetchall()
        groups: dict[tuple[str, int], list[int]] = {}
        for row in rows:
            groups.setdefault((row["media_type"], row["tmdb_id"]), []).append(row["id"])

        now = int(time.time())
        for (media_type, tmdb_id), ids in groups.items():
            keeper_id = ids[0]
            target_identity = f"tmdb:{media_type}:{tmdb_id}"
            conn.execute(
                "UPDATE media SET media_identity = ?, updated_at = ? WHERE id = ? AND media_identity != ?",
                (target_identity, now, keeper_id, target_identity),
            )
            for dup_id in ids[1:]:
                _merge_media_rows(conn, keeper_id, dup_id, now)
                merged += 1
        conn.commit()
    finally:
        conn.close()
    if merged:
        store.recount()
    return merged


def read_enricher_state(conn: sqlite3.Connection) -> dict:
    """Read every key/value from ``tmdb_enricher_state`` -- the persisted
    global state the leader thread writes (see ``BackgroundEnricher``) so
    every gunicorn worker's ``/api/library/tmdb-status`` reports the same
    thing regardless of which one won the leader election. A missing table
    (enrichment has never run against this database) reports an empty
    dict, same as no rows."""
    try:
        rows = conn.execute("SELECT key, value FROM tmdb_enricher_state").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: row[1] for row in rows}


def derive_worker_state(
    state: dict,
    *,
    now: float,
    enabled: bool,
    key_configured: bool,
    installed: bool,
    idle_seconds: float = 60,
) -> str:
    """Turn the persisted state dict plus the caller's live configuration
    checks into exactly one of ``"not_started"``/``"paused"``/
    ``"running"``/``"stale"``.

    Precedence: ``not_started`` wins outright when there is no heartbeat at
    all, regardless of ``enabled``/``key_configured``/``installed`` -- the
    enricher has simply never run yet, which is a distinct fact from
    whether it is currently allowed to. Only once a heartbeat exists does
    ``paused`` take priority over its freshness: the leader thread keeps
    writing a heartbeat every idle loop iteration even while paused, so
    freshness alone can't distinguish "genuinely idle-but-enabled" from
    "administratively disabled" -- that distinction only exists in the
    caller's own live config (``enabled``/``key_configured``/``installed``),
    which every gunicorn worker can check independently and agree on.
    Otherwise it's ``running`` (heartbeat within ``3 * idle_seconds``) or
    ``stale``.
    """
    try:
        heartbeat = int(state["heartbeat_at"])
    except (KeyError, TypeError, ValueError):
        return "not_started"
    if not (enabled and key_configured and installed):
        return "paused"
    return "running" if now - heartbeat <= 3 * idle_seconds else "stale"


class BackgroundEnricher:
    """Gunicorn-worker background thread (§6.2, D4-C; three-lock P0 fix).

    ``start()`` launches a daemon thread that tries a non-blocking
    ``fcntl.flock`` on ``leader_lock_path``; a worker that loses the race
    keeps the lock file open and retries every ``leader_retry_seconds``
    (via the stop-aware ``_wait``) until it wins or ``stop()`` is called --
    it never exits on its own. This matters across a gunicorn HUP reload:
    new workers' enricher threads start while an old worker (mid graceful
    shutdown) still holds the lock, so without retrying nobody would be
    left enriching once that old worker exits until a full restart. The
    winner then loops: at the top of every iteration it persists a fresh heartbeat
    (via ``conn_factory``, see ``read_enricher_state``/``derive_worker_state``)
    so every gunicorn worker's status view agrees on liveness regardless of
    which one is the leader; then it skips the round and rechecks in
    ``idle_seconds`` when ``enabled_check()`` is false or
    ``store_factory()``/``client_factory()`` return ``None`` (index not
    installed, TMDB key not configured, or ``tmdb_enrich_enabled=0`` --
    whichever of those the caller wires into ``enabled_check``/the
    factories).

    Otherwise it tries a non-blocking ``fcntl.flock`` on ``run_lock_path``
    -- shared with the manual/CLI enrich path (``run_library_enrich``,
    ``scripts/enrich_media_tmdb.py``) -- *around* the round only, released
    immediately after; when that lock is busy (a manual run is in
    progress) the round is skipped without error and retried after
    ``round_seconds``. When the lock is acquired, it calls ``enrich_batch``
    (limited to ``batch_size``) followed by ``merge_by_tmdb`` (only when
    the round wrote at least one ``exact`` match -- otherwise nothing
    could have changed which media share a ``tmdb_id``, so the scan is
    skipped), persists the round's outcome, then sleeps ``round_seconds``
    before the next round, or ``idle_seconds`` if the round stopped early
    on ``BudgetExhausted``/``InvalidApiKey``, or (T14 fix wave 3) if every
    candidate the round selected failed its TMDB search (see ``_loop``).
    Any other unexpected
    exception raised during a round (from either factory, ``enabled_check``
    or the round itself) is caught, logged by exception class name only
    (never the key or a URL), recorded on ``status()["last_error_class"]``
    and persisted state's ``last_error_class``, and backed off like a
    stopped-early round -- it never kills the thread. ``stop()`` sets a
    flag the loop only checks between rounds, so it never aborts an
    in-flight batch -- only the wait *after* it.

    ``lock_path`` is now two paths (``leader_lock_path``/``run_lock_path``,
    see ``LockPaths``) instead of one: reusing a single lock file across
    leader election (held for the thread's whole lifetime) and the round
    guard used to deadlock a background thread against its own client's
    budget-lock acquisition.
    """

    def __init__(
        self,
        store_factory: Callable[[], "LibraryStore | None"],
        client_factory: Callable[[], "TmdbClient | None"],
        *,
        leader_lock_path: Path,
        run_lock_path: Path,
        conn_factory: Callable[[], "sqlite3.Connection | None"],
        sleep: Callable[[float], None] = time.sleep,
        batch_size: int = 20,
        idle_seconds: float = 60,
        round_seconds: float = 5,
        leader_retry_seconds: float = 15.0,
        enabled_check: Callable[[], bool] = lambda: True,
        tvmaze_enabled_check: Callable[[], bool] = lambda: False,
        today: Callable[[], str] = lambda: datetime.now(timezone.utc).date().isoformat(),
    ) -> None:
        self._store_factory = store_factory
        self._client_factory = client_factory
        self._leader_lock_path = Path(leader_lock_path)
        self._run_lock_path = Path(run_lock_path)
        self._conn_factory = conn_factory
        self._sleep = sleep
        self._batch_size = batch_size
        self._idle_seconds = idle_seconds
        self._round_seconds = round_seconds
        self._leader_retry_seconds = leader_retry_seconds
        self._enabled_check = enabled_check
        self._tvmaze_enabled_check = tvmaze_enabled_check
        self._today = today

        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._state_lock = threading.Lock()
        self._running = False
        self._leader_attempts = 0
        self._last_round_at: float | None = None
        self._last_round_processed = 0
        self._processed_today = 0
        self._requests_429_today = 0
        self._today_str: str | None = None
        self._next_check_in: float | None = None
        self._last_error_class: str | None = None

    def start(self) -> None:
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, name="tmdb-enricher", daemon=True)
        self._thread.start()

    def _wait(self, wait_seconds: float) -> None:
        """Wait up to ``wait_seconds`` before the next round, returning as
        soon as ``stop()`` is called.  A caller-injected ``sleep`` (tests
        use one to control round pacing directly, e.g. to speed up or gate
        rounds) is honoured exactly as before; the real, un-injected
        default (``time.sleep``) is never used blockingly -- it is replaced
        by a wait on the stop ``Event`` so ``stop(timeout=...)`` is prompt
        even at the real ``idle_seconds`` default (60s)."""
        if self._sleep is time.sleep:
            self._stop_flag.wait(wait_seconds)
        else:
            self._sleep(wait_seconds)

    def stop(self, timeout: float = 30) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def status(self) -> dict:
        with self._state_lock:
            return {
                "running": self._running,
                "leader": self._running,
                "leader_attempts": self._leader_attempts,
                "last_round_at": self._last_round_at,
                "processed_today": self._processed_today,
                "next_check_in": self._next_check_in,
                "last_error_class": self._last_error_class,
            }

    @property
    def idle_seconds(self) -> float:
        """Public read accessor for the configured idle interval -- lets
        callers (e.g. the status endpoint's ``derive_worker_state`` stale
        threshold) read this back without reaching into the private
        ``_idle_seconds`` attribute."""
        return self._idle_seconds

    def _run(self) -> None:
        lock_file = None
        while not self._stop_flag.is_set():
            try:
                if lock_file is None:
                    self._leader_lock_path.parent.mkdir(parents=True, exist_ok=True)
                    lock_file = open(self._leader_lock_path, "a+")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Expected: another worker currently holds leadership --
                # keep the file open, count the attempt, and retry later.
                # A non-leader must never touch the database, so nothing
                # else happens on this path.
                with self._state_lock:
                    self._leader_attempts += 1
                self._wait(self._leader_retry_seconds)
                continue
            except Exception as exc:  # noqa: BLE001 -- leader acquisition must never kill this thread
                LOG.warning("tmdb enricher leader acquisition failed error=%s", type(exc).__name__)
                with self._state_lock:
                    self._leader_attempts += 1
                if lock_file is not None:
                    try:
                        lock_file.close()
                    except Exception:
                        pass
                    lock_file = None
                self._wait(self._leader_retry_seconds)
                continue
            else:
                break
        else:
            # stop() was called while we were still waiting for leadership.
            if lock_file is not None:
                lock_file.close()
            return

        LOG.info("tmdb enricher leader acquired pid=%s", os.getpid())
        with self._state_lock:
            self._running = True
        try:
            self._loop()
        finally:
            with self._state_lock:
                self._running = False
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def _write_state(self, values: dict[str, object]) -> None:
        """Persist ``values`` into ``tmdb_enricher_state`` via a
        short-lived connection from ``conn_factory`` -- never a key, URL or
        upstream response, only plain numbers/strings (§C.2).

        ``conn_factory`` returns ``None`` when there is nothing to persist
        to (production wiring: the library isn't installed yet, so
        ``LIBRARY_DB_PATH`` doesn't exist) -- persisting must never be what
        *creates* that file, or a host with no library ends up with a stub
        tmdb-only database that ``open_installed`` can't tell apart from a
        corrupt one (review fix #1). And like every other state write, this
        one must never be able to kill the loop (review fix #2): any
        exception raised while persisting -- e.g. ``sqlite3.OperationalError``
        when the DB is locked by a concurrent install/merge -- is logged by
        exception class name only and swallowed here, never propagated."""
        try:
            conn = self._conn_factory()
            if conn is None:
                return
            try:
                ensure_tables(conn)
                now = int(time.time())
                for key, value in values.items():
                    conn.execute(
                        "INSERT INTO tmdb_enricher_state (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (key, str(value), now),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 -- a state write must never kill this thread
            LOG.warning("tmdb background enricher state write failed error=%s", type(exc).__name__)

    def _persist_heartbeat(self) -> None:
        self._write_state({"heartbeat_at": int(time.time())})

    def _persist_round_state(self) -> None:
        with self._state_lock:
            snapshot = {
                "heartbeat_at": int(time.time()),
                "last_round_at": int(self._last_round_at) if self._last_round_at else "",
                "last_round_processed": self._last_round_processed,
                "processed_today": self._processed_today,
                "requests_429_today": self._requests_429_today,
                "today": self._today_str or "",
                "last_error_class": self._last_error_class or "",
                "leader_pid": os.getpid(),
            }
        self._write_state(snapshot)

    def _try_acquire_run_lock(self):
        self._run_lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self._run_lock_path, "a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return None
        return lock_file

    @staticmethod
    def _release_run_lock(lock_file) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    def _loop(self) -> None:
        while not self._stop_flag.is_set():
            self._persist_heartbeat()
            wait_seconds = self._idle_seconds
            try:
                store = self._store_factory() if self._enabled_check() else None
                client = self._client_factory() if store is not None else None

                if store is None or client is None:
                    with self._state_lock:
                        self._next_check_in = wait_seconds
                        self._last_error_class = None
                else:
                    run_lock_file = self._try_acquire_run_lock()
                    if run_lock_file is None:
                        # A manual enrich (CLI or offline script) holds the
                        # run lock -- skip this round without treating it
                        # as an error, and retry after round_seconds.
                        wait_seconds = self._round_seconds
                    else:
                        try:
                            stats = enrich_batch(store, client, limit=self._batch_size, tvmaze_enabled=self._tvmaze_enabled_check())
                            if stats.matched_exact > 0:
                                merge_by_tmdb(store)
                        finally:
                            self._release_run_lock(run_lock_file)
                        # T14 fix wave 3: a round where every candidate this
                        # round selected (phase-2 review-pending rows, once
                        # the primary queue was exhausted -- see
                        # ``enrich_batch``) failed its search is treated like
                        # an error for pacing purposes even though
                        # ``error_class`` itself is None (a search failure
                        # never sets it, see ``_process_candidate_row``): back
                        # off to ``idle_seconds`` instead of hammering a fresh
                        # slice of the backlog every ``round_seconds`` during
                        # e.g. a TMDB outage. ``queue_exhausted`` is required
                        # so a normal round that simply had nothing to select
                        # (``candidates_considered == 0``) doesn't count.
                        round_fully_failed = (
                            stats.queue_exhausted
                            and stats.candidates_considered > 0
                            and len(stats.search_failed_media_ids) >= stats.candidates_considered
                        )
                        wait_seconds = (
                            self._idle_seconds
                            if stats.error_class is not None or round_fully_failed
                            else self._round_seconds
                        )
                        day = self._today()
                        with self._state_lock:
                            if day != self._today_str:
                                self._today_str = day
                                self._processed_today = 0
                                self._requests_429_today = 0
                            self._last_round_at = time.time()
                            self._last_round_processed = stats.candidates_considered
                            self._processed_today += stats.candidates_considered
                            self._requests_429_today += stats.http_429
                            self._last_error_class = stats.error_class
                            self._next_check_in = wait_seconds
                        self._persist_round_state()
            except Exception as exc:  # noqa: BLE001 -- a round must never kill this thread
                LOG.warning("tmdb background enricher round failed error=%s", type(exc).__name__)
                with self._state_lock:
                    self._last_error_class = type(exc).__name__
                    self._next_check_in = wait_seconds
                self._persist_round_state()

            if self._stop_flag.is_set():
                break
            self._wait(wait_seconds)


# ===========================================================================
# Link validity checker (w6)
#
# Anonymous, read-only probing of public share-info endpoints for phase-1
# providers (tianyicloud/quark/alipan/115) to detect a dead share before the
# user clicks it -- see docs/architecture.md
# and .superpowers/sdd/briefs/w6-contract.md (the binding API/storage
# contract shared with the UI task).
#
# Every adapter is split into three small, independently testable pieces
# (so Codex can calibrate `classify` against real samples without touching
# request-building or pacing):
#   * extract_ref(url) -> share code/id, or None if the URL doesn't match
#     this provider's known share-link shapes ("unsupported_url").
#   * build_request(ref, access_code) -> (method, url, params_or_json,
#     headers) -- pure, no I/O.
#   * classify(http_status, body) -> (status, reason) -- pure, no I/O; body
#     is the parsed JSON object for the three JSON adapters or the response
#     text for 115's HTML page.
#
# LinkCheckClient owns the one anonymous ``requests.Session`` (no cookies,
# fixed UA, ``trust_env=False``, capped redirects/timeout/response size) and
# each provider's independent RateLimiter/daily Budget/pause state;
# check_link() dispatches one probe through it. run_link_check_round()
# selects due links from the store, decrypts each just-in-time (reveal(),
# discarded immediately after building the request -- see check_link,
# never logged or persisted), and writes only a status/reason/timestamp
# back. BackgroundLinkChecker is the leader-elected thread that calls it
# once a minute in production, mirroring BackgroundEnricher's leader-lock/
# heartbeat/idle skeleton.
#
# ``calibrated`` in the registry records whether an adapter's request shape
# and classify() mapping have been confirmed against real samples
# (docs/architecture.md: Codex runs
# --library-check-links --dry-run per provider; rounds 1-2 on 2026-09-06).
# Round 1: alipan confirmed (50/50 consistent); tianyicloud needed the JSON
# Accept header (fixed below); quark's numeric placeholder codes were never
# verified and are gone; 115's anonymous HTML page is a JS shell, replaced
# by the anonymous share/snap JSON endpoint. Round 2 (50/50/50/30 samples):
# every invalid code each provider actually returned was cross-checked in
# a browser (see each adapter's comment) -> all four calibrated=True. The
# flag is informational: every provider still defaults OFF in settings and
# is only ever switched on by the user.
# ===========================================================================

LINK_CHECK_STATUSES = ("valid", "invalid", "unknown")

# Reason codes -- see w6-contract.md's table. An "invalid" verdict is only
# ever one of the five explicit-signal reasons below; every ambiguous case
# (network/anti-bot/rate-limit/parse problem, or a URL this adapter can't
# even parse) is "unknown" with one of the reasons in the second tuple.
LINK_CHECK_INVALID_REASONS = (
    "share_not_found", "share_cancelled", "share_expired", "file_deleted", "share_audit",
)
LINK_CHECK_VALID_REASON = "ok"
LINK_CHECK_UNKNOWN_REASONS = (
    "network_error", "http_4xx", "http_5xx", "rate_limited", "anti_bot",
    "parse_error", "unmapped_response", "unsupported_url", "provider_disabled", "budget_exhausted",
)

# w6-contract: intervals are fixed server-side (not settings-configurable),
# daily caps are the *defaults* a fresh install starts with (settings can
# override, see _linkcheck_provider_cap); pause durations are the design
# doc's "429/anti-bot/>=5 consecutive network errors" backoff (115 gets a
# longer one -- it shares the host's outbound IP with OpenList).
LINK_CHECK_DEFAULT_INTERVAL_SECONDS = 3.0
LINK_CHECK_115_INTERVAL_SECONDS = 10.0
LINK_CHECK_DEFAULT_DAILY_CAP = 3000
LINK_CHECK_115_DAILY_CAP = 500
LINK_CHECK_DEFAULT_PAUSE_SECONDS = 3600.0
LINK_CHECK_115_PAUSE_SECONDS = 6 * 3600.0
LINK_CHECK_NETWORK_ERROR_PAUSE_THRESHOLD = 5

# Scheduling (design doc §4): a checked link's next_check_at delay depends
# on its verdict; an "unknown" verdict escalates to the longer interval
# once it has stayed unknown for LINK_CHECK_UNKNOWN_ESCALATION_THRESHOLD
# consecutive checks in a row (reset to 0 the moment a check comes back
# valid or invalid).
LINK_CHECK_INTERVAL_VALID_DAYS = 14
LINK_CHECK_INTERVAL_INVALID_DAYS = 7
LINK_CHECK_INTERVAL_UNKNOWN_DAYS = 1
LINK_CHECK_UNKNOWN_ESCALATION_THRESHOLD = 5
LINK_CHECK_INTERVAL_UNKNOWN_ESCALATED_DAYS = 7

_SECONDS_PER_DAY = 86400


def _next_check_delay_seconds(status: str, consecutive_unknown: int) -> int:
    if status == "valid":
        return LINK_CHECK_INTERVAL_VALID_DAYS * _SECONDS_PER_DAY
    if status == "invalid":
        return LINK_CHECK_INTERVAL_INVALID_DAYS * _SECONDS_PER_DAY
    if consecutive_unknown >= LINK_CHECK_UNKNOWN_ESCALATION_THRESHOLD:
        return LINK_CHECK_INTERVAL_UNKNOWN_ESCALATED_DAYS * _SECONDS_PER_DAY
    return LINK_CHECK_INTERVAL_UNKNOWN_DAYS * _SECONDS_PER_DAY


# --- URL -> share ref extraction (best-effort, documented assumptions) -----

_TIANYI_PATTERNS = (
    re.compile(r"cloud\.189\.cn/t/([A-Za-z0-9]+)"),
    re.compile(r"h5\.cloud\.189\.cn/share/([A-Za-z0-9]+)"),
    re.compile(r"[?&]code=([A-Za-z0-9]+)"),
)


def _extract_tianyicloud(url: str) -> str | None:
    for pattern in _TIANYI_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


_QUARK_PATTERN = re.compile(r"pan\.quark\.cn/s/([A-Za-z0-9]+)")


def _extract_quark(url: str) -> str | None:
    match = _QUARK_PATTERN.search(url)
    return match.group(1) if match else None


_ALIPAN_PATTERNS = (
    re.compile(r"alipan\.com/s/([A-Za-z0-9]+)"),
    re.compile(r"aliyundrive\.com/s/([A-Za-z0-9]+)"),
    re.compile(r"alywp\.net/([A-Za-z0-9]+)"),
)


def _extract_alipan(url: str) -> str | None:
    for pattern in _ALIPAN_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


# 115 share links live on three domains in the real index (calibration
# round 1: 115cdn.com 3,354 / 115.com 2,215 / anxia.com 257 of 5,826 live
# links) -- all three carry the same /s/<share_code> shape.
_115_PATTERN = re.compile(r"(?:^|//|\.)(?:115|115cdn|anxia)\.com/s/([A-Za-z0-9]+)")


def _extract_115(url: str) -> str | None:
    match = _115_PATTERN.search(url)
    return match.group(1) if match else None


# --- build_request: pure (share_ref, access_code) -> (method, url, payload, headers) ---


# Tianyi answers this .action endpoint in XML unless the request asks for
# JSON explicitly (calibration round 1: every probe without the header came
# back "application/xml" and classified as parse_error).
_TIANYI_HEADERS = {"Accept": "application/json;charset=UTF-8"}


def _build_tianyicloud(ref: str, access_code: str | None):
    return (
        "GET", "https://api.cloud.189.cn/open/share/getShareInfoByCodeV2.action",
        {"shareCode": ref}, dict(_TIANYI_HEADERS),
    )


def _build_quark(ref: str, access_code: str | None):
    url = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc"
    return ("POST", url, {"pwd_id": ref, "passcode": access_code or ""}, None)


def _build_alipan(ref: str, access_code: str | None):
    return ("POST", "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous", {"share_id": ref}, None)


# 115: the share page itself (115.com / 115cdn.com / anxia.com /s/<code>)
# is a JavaScript shell -- calibration round 1 got the SAME 21 KiB HTML for
# live and cancelled shares alike, so nothing can be read off it. The
# anonymous share listing endpoint answers in JSON without any cookie/token
# (still the hard rule: this probe never carries the user's 115 session).
def _build_115(ref: str, access_code: str | None):
    params = {"share_code": ref, "receive_code": access_code or "", "offset": 0, "limit": 1}
    return ("GET", "https://webapi.115.com/share/snap", params, None)


# --- classify: pure (http_status, parsed_json_or_text) -> (status, reason) ---
#
# Calibration rule (round 1, 2026-09-06): only a provider code CONFIRMED on
# a real dead share -- the API verdict cross-checked against what a browser
# renders for the same link -- is ever mapped to "invalid". Every other
# well-formed error envelope is "unknown"/"unmapped_response" and shows up,
# code-only, in the dry-run "signals" histogram (see response_signal) so
# the next round can promote it. Nothing here ever inspects, logs or
# persists a provider's free-text message.

# Tianyi: ``res_code`` is the int 0 on success and a string code on
# failure (HTTP 400). ``res_message`` embeds the share/file ids
# ("shareUserRightcheck() - ... shareId=..., fileId=...") and is therefore
# never looked at. Browser-confirmed (calibration rounds 1-2):
# FileNotFound 2/2 and ShareNotFound 2/2 rendered "抱歉，您访问的页面地址有误，
# 或者该页面不存在"; ShareAuditNotPass 1/1 rendered "抱歉，该内容审核不通过".
# Any other string code stays unmapped_response until a round shows it AND
# a browser confirms it.
_TIANYI_INVALID_CODES = {
    "FileNotFound": "file_deleted",
    "ShareNotFound": "share_not_found",
    "ShareAuditNotPass": "share_audit",
}


def _is_int_value(value, expected: int) -> bool:
    """True only for a real int equal to ``expected`` -- never a bool
    (``False == 0``) or a float (``0.0 == 0``): a malformed envelope must
    not be recorded as a 14-day "valid"."""
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _classify_tianyicloud(status_code: int, body) -> tuple[str, str | None]:
    if status_code == 429:
        return "unknown", "rate_limited"
    if status_code >= 500:
        return "unknown", "http_5xx"
    if not isinstance(body, dict) or "res_code" not in body:
        return "unknown", "parse_error"
    code = body.get("res_code")
    if _is_int_value(code, 0):
        return "valid", LINK_CHECK_VALID_REASON
    reason = _TIANYI_INVALID_CODES.get(code) if isinstance(code, str) else None
    if reason:
        return "invalid", reason
    return "unknown", "unmapped_response"


# Quark: ``status``/``code`` envelope (``status`` 200 + ``code`` 0 = live).
# The earlier 41013/41014/41016/41017 placeholders were never verified and
# are gone. Browser-confirmed (calibration rounds 1-2, API message / page
# text): 41011 (HTTP 404, "分享地址已失效" / "分享地址已失效", 3/3), 41012
# (HTTP 404, "好友已取消了分享" / "该分享已被取消，无法访问", 2/2), 41031
# (HTTP 403, "分享者用户封禁链接查看受限" / "该分享已失效，不可访问", 4/4).
# Seen once in round 2 but not yet confirmed (stays unmapped): 41010.
_QUARK_INVALID_CODES: dict[int, str] = {
    41011: "share_expired",
    41012: "share_cancelled",
    41031: "share_audit",
}


def _classify_quark(status_code: int, body) -> tuple[str, str | None]:
    if status_code == 429:
        return "unknown", "rate_limited"
    if status_code >= 500:
        return "unknown", "http_5xx"
    if not isinstance(body, dict) or "code" not in body:
        return "unknown", "parse_error"
    code = body.get("code")
    if _is_int_value(body.get("status"), 200) and _is_int_value(code, 0):
        return "valid", LINK_CHECK_VALID_REASON
    reason = _QUARK_INVALID_CODES.get(code) if _is_int_value(code, code) else None
    if reason:
        return "invalid", reason
    return "unknown", "unmapped_response"


_ALIPAN_CODE_MAP = {
    "ShareLink.Cancelled": "share_cancelled",
    "ShareLink.Expired": "share_expired",
    "ShareLink.Forbidden": "share_audit",
    "NotFound.ShareLink": "share_not_found",
}


def _classify_alipan(status_code: int, body) -> tuple[str, str | None]:
    if status_code == 429:
        return "unknown", "rate_limited"
    if status_code >= 500:
        return "unknown", "http_5xx"
    # M8: inspect the JSON body BEFORE trusting a bare HTTP 200 -- alipan's
    # anonymous share-info endpoint can return 200 with an invalid-family
    # ``code`` in the body instead of a non-200 status; conservative either
    # way -- only a code this module already maps to a reason ever produces
    # "invalid", everything else still falls through to the status-code
    # rules below.
    if isinstance(body, dict):
        reason = _ALIPAN_CODE_MAP.get(body.get("code"))
        if reason:
            return "invalid", reason
    if status_code == 200:
        return "valid", LINK_CHECK_VALID_REASON
    if status_code == 404:
        return "invalid", "share_not_found"
    if 400 <= status_code < 500:
        return "unknown", "http_4xx"
    return "unknown", "parse_error"


# 115 share/snap envelope: ``{"state": true, ...}`` for a live share;
# ``{"state": false, "errno": <int>, "error": "<text>"}`` otherwise.
# errno 4100010 ("分享已取消"): 2/2 API samples, 1 of them rendered "无法加载
# 分享 分享已取消" in a browser. The ``error`` text is only ever matched
# against the anti-bot needles (a throttle/captcha signal must pause the
# provider for 6 h); it is never logged or persisted.
_115_INVALID_ERRNOS = {4100010: "share_cancelled"}
# Deliberately no bare "验证": an access-code error ("请验证访问码") must
# not pause the provider for 6 h. These also cover a captcha/throttle HTML
# shell served instead of the JSON envelope (see the str branch below).
_115_ANTI_BOT_NEEDLES = ("验证码", "人机验证", "频繁", "稍后再试")


def _classify_115(status_code: int, body) -> tuple[str, str | None]:
    if status_code == 429:
        return "unknown", "rate_limited"
    if status_code >= 500:
        return "unknown", "http_5xx"
    if isinstance(body, str):
        # A captcha/throttle page served instead of the JSON envelope must
        # still trip the 6 h pause -- the host's outbound IP is shared with
        # OpenList's own 115 traffic.
        if any(needle in body for needle in _115_ANTI_BOT_NEEDLES):
            return "unknown", "anti_bot"
        return "unknown", "parse_error"
    if not isinstance(body, dict) or "state" not in body:
        return "unknown", "parse_error"
    state = body.get("state")
    if state is True or _is_int_value(state, 1):
        return "valid", LINK_CHECK_VALID_REASON
    if not (state is False or _is_int_value(state, 0)):
        return "unknown", "parse_error"
    error_text = str(body.get("error") or "")
    if any(needle in error_text for needle in _115_ANTI_BOT_NEEDLES):
        return "unknown", "anti_bot"
    errno = body.get("errno")
    reason = _115_INVALID_ERRNOS.get(errno) if _is_int_value(errno, errno) else None
    if reason:
        return "invalid", reason
    return "unknown", "unmapped_response"


# --- response_signal: redacted, code-only fingerprint for calibration ---
#
# The dry-run "signals" histogram (calibration round 2 asks for the actual
# codes each provider returns): HTTP status plus the first envelope code
# field present. The value is VALIDATED, not sanitised -- only an int or a
# str that already is a bare code token ([A-Za-z0-9._-], at most 32 chars)
# is emitted; anything else (a sentence, a path, a nested object, a
# WAF/CDN envelope that happens to call its message "code") becomes the
# literal "unsafe". Never a message, an id, a URL or a body; the histogram
# itself only ever reaches CLI stdout (never the DB/state/API/UI).

_SIGNAL_CODE_FIELDS = ("res_code", "code", "errno")
_SIGNAL_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,32}")


def _signal_token(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return "unsafe"
    text = str(value)
    return text if _SIGNAL_TOKEN.fullmatch(text) else "unsafe"


def response_signal(status_code: int, body) -> str:
    if isinstance(body, dict):
        for field_name in _SIGNAL_CODE_FIELDS:
            if field_name in body:
                return f"http:{status_code} {field_name}:{_signal_token(body.get(field_name))}"
        if "state" in body:
            state = body.get("state")
            if state is True or state is False or _is_int_value(state, state):
                return f"http:{status_code} state:{int(state)}"
            return f"http:{status_code} state:other"
        return f"http:{status_code} json"
    if isinstance(body, str):
        head = body.lstrip()[:5].lower()
        kind = "xml" if head.startswith("<?xml") else "html" if head.startswith("<") else "text"
        return f"http:{status_code} {kind}"
    return f"http:{status_code} other"


@dataclass(frozen=True)
class LinkCheckAdapter:
    code: str
    calibrated: bool
    interval_seconds: float
    default_daily_cap: int
    pause_seconds: float
    extract_ref: Callable[[str], "str | None"]
    build_request: Callable[[str, "str | None"], tuple]
    classify: Callable[[int, object], tuple[str, "str | None"]]


LINK_CHECK_ADAPTERS: dict[str, LinkCheckAdapter] = {
    "tianyicloud": LinkCheckAdapter(
        "tianyicloud", True, LINK_CHECK_DEFAULT_INTERVAL_SECONDS, LINK_CHECK_DEFAULT_DAILY_CAP,
        LINK_CHECK_DEFAULT_PAUSE_SECONDS, _extract_tianyicloud, _build_tianyicloud, _classify_tianyicloud,
    ),
    "quark": LinkCheckAdapter(
        "quark", True, LINK_CHECK_DEFAULT_INTERVAL_SECONDS, LINK_CHECK_DEFAULT_DAILY_CAP,
        LINK_CHECK_DEFAULT_PAUSE_SECONDS, _extract_quark, _build_quark, _classify_quark,
    ),
    "alipan": LinkCheckAdapter(
        "alipan", True, LINK_CHECK_DEFAULT_INTERVAL_SECONDS, LINK_CHECK_DEFAULT_DAILY_CAP,
        LINK_CHECK_DEFAULT_PAUSE_SECONDS, _extract_alipan, _build_alipan, _classify_alipan,
    ),
    "115": LinkCheckAdapter(
        "115", True, LINK_CHECK_115_INTERVAL_SECONDS, LINK_CHECK_115_DAILY_CAP,
        LINK_CHECK_115_PAUSE_SECONDS, _extract_115, _build_115, _classify_115,
    ),
}

LINK_CHECK_PROVIDERS: tuple[str, ...] = ("tianyicloud", "quark", "alipan", "115")


# --- anonymous transport --------------------------------------------------

LINK_CHECK_USER_AGENT = "Mozilla/5.0 HiDrive-Lite/1.0"
LINK_CHECK_CONNECT_TIMEOUT = 5.0
LINK_CHECK_READ_TIMEOUT = 10.0
LINK_CHECK_MAX_REDIRECTS = 3
LINK_CHECK_RESPONSE_SIZE_CAP_BYTES = 512 * 1024
_LINK_CHECK_BASE_HEADERS = {"User-Agent": LINK_CHECK_USER_AGENT}
_LINK_CHECK_FORBIDDEN_HEADERS = ("cookie", "authorization")


def build_anonymous_session() -> "requests.Session":
    """A dedicated, never-authenticated ``requests.Session`` for probing
    share-info endpoints: no cookies (fresh jar, never shared with the 115
    save/transfer session), ``trust_env=False`` (ignores any proxy/netrc
    from the environment), a capped redirect count. Hard rule (w6-checker
    brief): this session -- and this module's checker code in general --
    must never be able to reach the 115 cookie/token store.

    I1: the jar itself also gets a blocking cookie policy
    (``DefaultCookiePolicy(allowed_domains=[])`` refuses every domain, so a
    provider's ``Set-Cookie`` response header is never even stored) --
    independent of, and in addition to, ``LinkCheckClient.dispatch()``
    clearing the jar after every single probe. Either guard alone would
    stop cookies from surviving to the NEXT probe; both together mean a
    provider can never accumulate session state even mid-probe (e.g. a
    redirect chain the session follows internally)."""
    session = requests.Session()
    session.trust_env = False
    session.max_redirects = LINK_CHECK_MAX_REDIRECTS
    session.headers.clear()
    session.cookies = requests.cookies.RequestsCookieJar(
        policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
    )
    return session


def _scrub_headers(headers: dict | None) -> dict:
    merged = dict(_LINK_CHECK_BASE_HEADERS)
    if headers:
        merged.update(headers)
    return {k: v for k, v in merged.items() if k.lower() not in _LINK_CHECK_FORBIDDEN_HEADERS}


def _read_capped_body(response) -> tuple[bytes, bool]:
    """I5: read at most ``LINK_CHECK_RESPONSE_SIZE_CAP_BYTES`` (+1 extra
    byte to detect overflow) off ``response`` via ``iter_content`` -- the
    request was made with ``stream=True`` (see ``LinkCheckClient.dispatch``)
    specifically so this can stop pulling bytes off the wire the instant
    the cap is crossed, rather than letting ``requests`` buffer an
    arbitrarily large body into memory first (the old ``response.content``/
    ``response.json()``-based approach below only checked the size AFTER
    the whole thing was already downloaded). Always closes the response,
    even on an early break. Returns ``(data, oversized)``; ``data`` is
    truncated to the cap when ``oversized`` is True."""
    cap = LINK_CHECK_RESPONSE_SIZE_CAP_BYTES
    chunks: list[bytes] = []
    total = 0
    oversized = False
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                oversized = True
                break
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 -- closing a stream must never mask the real outcome
            pass
    data = b"".join(chunks)
    if oversized:
        data = data[:cap]
    return data, oversized


def _parse_body(data: bytes):
    """Parsed JSON if ``data`` decodes as JSON, else raw text -- lets one
    ``classify(status, body)`` signature serve both the three JSON adapters
    and 115's HTML page. Works off already-capped bytes (see
    ``_read_capped_body``) rather than asking ``requests`` to parse/buffer
    the live response itself."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _linkcheck_state_get(conn_factory, key: str) -> "str | None":
    conn = conn_factory()
    if conn is None:
        return None
    try:
        ensure_tables(conn)
        row = conn.execute("SELECT value FROM link_check_state WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


def _linkcheck_write_state(conn_factory, values: dict) -> None:
    try:
        conn = conn_factory()
        if conn is None:
            return
        try:
            ensure_tables(conn)
            now = int(time.time())
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO link_check_state (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, str(value), now),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- a state write must never kill a round/thread
        LOG.warning("linkcheck state write failed error=%s", type(exc).__name__)


def read_link_check_state(conn: sqlite3.Connection) -> dict:
    """Every key/value in ``link_check_state`` -- the persisted heartbeat/
    round/pause/budget bookkeeping every gunicorn worker reads for
    ``/api/library/linkcheck-status``, regardless of which one is the
    leader. Mirrors ``read_enricher_state``'s role for the TMDB enricher."""
    try:
        rows = conn.execute("SELECT key, value FROM link_check_state").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: row[1] for row in rows}


def _linkcheck_budget_key(provider: str, day: str) -> str:
    return f"budget_used:{provider}:{day}"


def _linkcheck_paused_until_key(provider: str) -> str:
    return f"paused_until:{provider}"


def _linkcheck_last_error_key(provider: str) -> str:
    return f"last_error_class:{provider}"


class LinkCheckBudget:
    """Per-provider daily request budget, tracked as a plain counter in
    ``link_check_state`` (there is no dedicated budget table in the w6
    storage contract -- see that table's own comment). Single-writer by
    construction: only the leader thread's round, or a CLI invocation
    (mutually exclusive with the leader via the same run-lock pattern
    ``BackgroundEnricher`` uses), ever calls ``reserve()`` -- no separate
    fcntl lock needed, unlike TMDB's ``Budget``."""

    def __init__(self, conn_factory, provider: str, daily_cap: int, *, today):
        self._conn_factory = conn_factory
        self._provider = provider
        self._daily_cap = daily_cap
        self._today = today

    def _used_today_locked(self, conn) -> int:
        row = conn.execute(
            "SELECT value FROM link_check_state WHERE key=?",
            (_linkcheck_budget_key(self._provider, self._today()),),
        ).fetchone()
        try:
            return int(row[0]) if row is not None and row[0] is not None else 0
        except (TypeError, ValueError):
            return 0

    def used_today(self) -> int:
        conn = self._conn_factory()
        if conn is None:
            return 0
        try:
            ensure_tables(conn)
            return self._used_today_locked(conn)
        finally:
            conn.close()

    def remaining(self) -> int:
        return max(self._daily_cap - self.used_today(), 0)

    def reserve(self) -> bool:
        """Increment and return True when under the cap; otherwise leave
        the counter untouched and return False."""
        conn = self._conn_factory()
        if conn is None:
            return False
        try:
            ensure_tables(conn)
            used = self._used_today_locked(conn)
            if used >= self._daily_cap:
                return False
            conn.execute(
                "INSERT INTO link_check_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (_linkcheck_budget_key(self._provider, self._today()), str(used + 1), int(time.time())),
            )
            conn.commit()
            return True
        finally:
            conn.close()


@dataclass(frozen=True)
class LinkCheckOutcome:
    status: str
    reason: "str | None"
    http_class: "str | None"
    # Redacted calibration fingerprint (see response_signal); aggregated
    # into the round's per-provider "signals" histogram, never persisted.
    signal: "str | None" = field(default=None, compare=False)


class LinkCheckClient:
    """Anonymous session + per-provider RateLimiter/Budget/pause state.

    ``settings`` supplies each provider's daily cap override
    (``linkcheck_<code>_daily_cap``, see ``_linkcheck_provider_cap``);
    intervals/pause durations are fixed per ``LINK_CHECK_ADAPTERS`` (the
    contract: "intervals fixed server-side"). ``now`` is the injectable
    wall-clock (epoch seconds) used for pause/budget bookkeeping --
    independent of ``clock``/``sleep``, which only drive the per-provider
    ``RateLimiter``'s pacing."""

    def __init__(
        self,
        *,
        conn_factory,
        settings: Mapping | None = None,
        session=None,
        clock=time.monotonic,
        sleep=time.sleep,
        now: Callable[[], int] | None = None,
        today: Callable[[], str] | None = None,
        connect_timeout: float = LINK_CHECK_CONNECT_TIMEOUT,
        read_timeout: float = LINK_CHECK_READ_TIMEOUT,
    ):
        self._conn_factory = conn_factory
        self._session = session if session is not None else build_anonymous_session()
        self._now = now or (lambda: int(time.time()))
        self._today = today or (lambda: datetime.now(timezone.utc).date().isoformat())
        self._timeout = (connect_timeout, read_timeout)

        settings = settings or {}
        self._limiters: dict[str, RateLimiter] = {}
        self._budgets: dict[str, LinkCheckBudget] = {}
        self._paused_until: dict[str, int] = {}
        self._consecutive_network_errors: dict[str, int] = {}
        self._last_error_class: dict[str, "str | None"] = {}
        for code, adapter in LINK_CHECK_ADAPTERS.items():
            self._limiters[code] = RateLimiter(int(adapter.interval_seconds * 1000), clock=clock, sleep=sleep)
            cap = _linkcheck_provider_cap(settings, code, adapter.default_daily_cap)
            self._budgets[code] = LinkCheckBudget(conn_factory, code, cap, today=self._today)
            self._consecutive_network_errors[code] = 0
            persisted_pause = _linkcheck_state_get(conn_factory, _linkcheck_paused_until_key(code))
            try:
                self._paused_until[code] = int(persisted_pause) if persisted_pause else None
            except (TypeError, ValueError):
                self._paused_until[code] = None
            self._last_error_class[code] = _linkcheck_state_get(conn_factory, _linkcheck_last_error_key(code)) or None

    def wait(self, provider: str) -> None:
        limiter = self._limiters.get(provider)
        if limiter is not None:
            limiter.wait()

    def is_paused(self, provider: str) -> bool:
        until = self._paused_until.get(provider)
        return until is not None and self._now() < until

    def paused_until(self, provider: str) -> "int | None":
        return self._paused_until.get(provider)

    def last_error_class(self, provider: str) -> "str | None":
        return self._last_error_class.get(provider)

    def used_today(self, provider: str) -> int:
        budget = self._budgets.get(provider)
        return budget.used_today() if budget else 0

    def budget_remaining(self, provider: str) -> int:
        budget = self._budgets.get(provider)
        return budget.remaining() if budget else 0

    def reserve_budget(self, provider: str) -> bool:
        budget = self._budgets.get(provider)
        return budget.reserve() if budget else False

    def _pause(self, provider: str, pause_seconds: float, reason: "str | None") -> None:
        until = int(self._now() + pause_seconds)
        self._paused_until[provider] = until
        self._last_error_class[provider] = reason
        _linkcheck_write_state(
            self._conn_factory,
            {_linkcheck_paused_until_key(provider): until, _linkcheck_last_error_key(provider): reason or ""},
        )

    def _note_success(self, provider: str) -> None:
        if self._consecutive_network_errors.get(provider):
            self._consecutive_network_errors[provider] = 0
        if self._last_error_class.get(provider) is not None:
            self._last_error_class[provider] = None
            _linkcheck_write_state(self._conn_factory, {_linkcheck_last_error_key(provider): ""})

    def _note_network_error(self, provider: str, error_class: str) -> None:
        """``error_class`` is the raw transport exception class name (e.g.
        ``"ConnectTimeout"``) -- M1: logged here for debugging, but never
        persisted or exposed as-is. The client's own ``last_error_class``
        (and the ``link_check_state``/``/api/library/linkcheck-status``
        value it feeds) always stores the contract reason
        ``"network_error"`` instead, so the settings card never shows a raw
        Python exception class name. The only place the class name does
        appear is ``check_link``'s ``net:<class>`` outcome *signal* -- the
        in-memory dry-run histogram that reaches CLI stdout and nothing
        else."""
        count = self._consecutive_network_errors.get(provider, 0) + 1
        self._consecutive_network_errors[provider] = count
        LOG.warning("linkcheck network error provider=%s error=%s", provider, error_class)
        self._last_error_class[provider] = "network_error"
        _linkcheck_write_state(self._conn_factory, {_linkcheck_last_error_key(provider): "network_error"})
        if count >= LINK_CHECK_NETWORK_ERROR_PAUSE_THRESHOLD:
            adapter = LINK_CHECK_ADAPTERS[provider]
            self._pause(provider, adapter.pause_seconds, "network_error")

    def _note_pause_signal(self, provider: str, reason: str) -> None:
        adapter = LINK_CHECK_ADAPTERS[provider]
        self._pause(provider, adapter.pause_seconds, reason)

    def dispatch(self, method: str, url: str, payload: "dict | None", headers: "dict | None"):
        """Send one request through the anonymous session. Returns
        ``(response, network_error_class)`` -- exactly one is ``None``.
        Never raises -- a transport failure (timeout, connection error, too
        many redirects) is reported as the second element instead.

        I5: ``stream=True`` so the caller (``check_link``, via
        ``_read_capped_body``) can stop reading the body the instant the
        size cap is crossed instead of ``requests`` buffering the whole
        thing first. I1: the session's cookie jar is cleared after every
        single request, win or lose -- a second, independent guard beyond
        ``build_anonymous_session()``'s blocking cookie policy against a
        provider ever accumulating session state across probes."""
        merged_headers = _scrub_headers(headers)
        kwargs: dict = {
            "timeout": self._timeout, "headers": merged_headers, "allow_redirects": True, "stream": True,
        }
        if method == "GET":
            kwargs["params"] = payload
        else:
            kwargs["json"] = payload
        try:
            response = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            return None, type(exc).__name__
        finally:
            # getattr-guarded: a real requests.Session always has a
            # cookie jar here, but tests exercise this code path with
            # lightweight fakes (no cookies attribute at all) that don't
            # need this guard to matter for them.
            cookies = getattr(self._session, "cookies", None)
            if cookies is not None:
                cookies.clear()
        return response, None


def check_link(client: LinkCheckClient, provider: str, url: str, access_code: "str | None") -> LinkCheckOutcome:
    """One anonymous probe of ``url`` (already decrypted by the caller,
    just-in-time -- never logged here). Paces itself via the client's
    per-provider RateLimiter and updates the client's pause/error-tracking
    state as a side effect; never writes to the database itself (the
    caller, ``run_link_check_round``, owns persistence so it can also
    support a dry run)."""
    adapter = LINK_CHECK_ADAPTERS.get(provider)
    if adapter is None:
        return LinkCheckOutcome("unknown", "unsupported_url", None)
    ref = adapter.extract_ref(url)
    if ref is None:
        return LinkCheckOutcome("unknown", "unsupported_url", None)
    method, request_url, payload, headers = adapter.build_request(ref, access_code)

    client.wait(provider)
    response, network_error_class = client.dispatch(method, request_url, payload, headers)
    if network_error_class is not None:
        client._note_network_error(provider, network_error_class)
        return LinkCheckOutcome("unknown", "network_error", None, f"net:{network_error_class}")

    http_class = str(response.status_code)
    body_bytes, oversized = _read_capped_body(response)
    if oversized:
        client._note_success(provider)
        return LinkCheckOutcome("unknown", "parse_error", http_class, f"http:{http_class} oversized")

    body = _parse_body(body_bytes)
    status, reason = adapter.classify(response.status_code, body)
    if reason in ("rate_limited", "anti_bot"):
        client._note_pause_signal(provider, reason)
    elif reason != "parse_error":
        # An unparseable body (e.g. an HTML shell in place of the JSON
        # envelope) is neither a success nor a transport error: it must not
        # reset the consecutive-network-error count or clear a recorded
        # error class the way a real envelope does.
        client._note_success(provider)
    return LinkCheckOutcome(status, reason, http_class, response_signal(response.status_code, body))


def _linkcheck_global_enabled(settings: Mapping) -> bool:
    return str(settings.get("linkcheck_enabled", "0")).strip().lower() in {"1", "true"}


def _linkcheck_provider_enabled(settings: Mapping, code: str) -> bool:
    if not _linkcheck_global_enabled(settings):
        return False
    return str(settings.get(f"linkcheck_{code}_enabled", "0")).strip().lower() in {"1", "true"}


def _linkcheck_provider_cap(settings: Mapping, code: str, default: int) -> int:
    parsed = _parse_int(settings.get(f"linkcheck_{code}_daily_cap"), min_value=0)
    return parsed if parsed is not None else default


@dataclass
class LinkCheckStats:
    checked: int = 0
    valid: int = 0
    invalid: int = 0
    unknown: int = 0
    elapsed_seconds: float = 0.0
    by_provider: dict = field(default_factory=dict)


def _linkcheck_provider_stats(stats: LinkCheckStats, code: str) -> dict:
    return stats.by_provider.setdefault(
        code,
        {
            "checked": 0, "valid": 0, "invalid": 0, "unknown": 0, "reasons": {}, "signals": {},
            "budget_exhausted_skipped": 0, "paused_until": None,
        },
    )


def run_link_check_round(
    store: "LibraryStore",
    client: LinkCheckClient,
    settings: "Mapping | None" = None,
    *,
    limit: int = 20,
    providers: "Sequence[str] | None" = None,
    sample: "int | None" = None,
    dry_run: bool = False,
    now: Callable[[], int] = lambda: int(time.time()),
) -> LinkCheckStats:
    """Select due (or, with ``sample``, random live) links of every enabled
    provider and check them, up to ``limit`` total (ignored when
    ``sample`` is given -- each provider gets its own ``sample`` links).

    ``providers`` overrides the settings-driven enabled set (the CLI's
    ``--provider CODE`` -- deliberately bypasses ``linkcheck_enabled``/
    ``linkcheck_<code>_enabled`` so Codex can calibrate a provider before
    it's ever switched on). ``dry_run`` classifies without writing to
    ``link_check`` or reserving any provider's daily budget (still paces
    itself and still updates the client's pause/error state -- a real
    rate-limit or ban signal seen during calibration is still worth
    recording).

    I3: an active pause is honoured regardless of ``dry_run`` -- calibration
    must never hammer a provider that just told this client to back off; a
    skipped-for-pause provider's stats carry ``paused_until`` instead of any
    probe counts. w6-checker-fix C2: when NOT a dry run, any check whose
    verdict flips a link's LIVE status (``invalid`` <-> anything else --
    ``live_link_sql()``'s only concern) queues that link's group for an
    incremental ``store.recount_groups()`` once the whole round is done, so
    ``media.link_count``/``resource_group.link_count``/``has_115`` (and
    therefore ``all_links_invalid``, search cards and group summaries)
    reflect the new verdict immediately, not just after some unrelated
    write happens to trigger a full ``recount()``.

    M5: without ``sample`` (the shared ``limit`` budget case), links are
    pulled ROUND-ROBIN across providers -- one due link per still-active
    provider per pass, cycling passes until ``limit`` is spent or every
    provider has nothing left to give -- instead of registry order letting
    the FIRST enabled provider's own due backlog exhaust the whole round's
    shared budget while every other provider starves behind it. Each
    provider's due queue is fetched once, up front (bounded by ``limit``);
    per-provider pacing (the client's own ``RateLimiter``/daily budget/
    pause) is unchanged, only the ORDER links are pulled in changes.
    ``--sample`` mode is unaffected -- each provider already gets its own
    fixed ``sample`` count regardless of the others, so there is no shared
    budget for one provider to exhaust.
    """
    settings = settings or {}
    stats = LinkCheckStats()
    started = time.monotonic()
    changed_group_ids: set[int] = set()
    remaining = limit

    codes = list(providers) if providers is not None else [
        code for code in LINK_CHECK_ADAPTERS if _linkcheck_provider_enabled(settings, code)
    ]

    def _process_row(code: str, row, provider_stats: dict) -> None:
        nonlocal remaining
        try:
            url, access_code = store.reveal(row["public_id"])
        except (LibraryKeyUnavailable, KeyError):
            return
        outcome = check_link(client, code, url, access_code)
        url = access_code = None  # never held onto beyond the probe itself

        prior_consecutive_unknown = row["consecutive_unknown"]
        consecutive_unknown = prior_consecutive_unknown + 1 if outcome.status == "unknown" else 0
        if not dry_run:
            checked_at = now()
            next_check_at = checked_at + _next_check_delay_seconds(outcome.status, consecutive_unknown)
            store.record_link_check(
                code, row["canonical_url_hash"],
                status=outcome.status, reason=outcome.reason, http_class=outcome.http_class,
                checked_at=checked_at, next_check_at=next_check_at, consecutive_unknown=consecutive_unknown,
            )
            prior_status = row["prior_status"]
            live_status_changed = (outcome.status == "invalid") != (prior_status == "invalid")
            if live_status_changed:
                changed_group_ids.add(row["group_id"])

        stats.checked += 1
        provider_stats["checked"] += 1
        provider_stats[outcome.status] += 1
        if outcome.reason:
            provider_stats["reasons"][outcome.reason] = provider_stats["reasons"].get(outcome.reason, 0) + 1
        if outcome.signal:
            provider_stats["signals"][outcome.signal] = provider_stats["signals"].get(outcome.signal, 0) + 1
        if outcome.status == "valid":
            stats.valid += 1
        elif outcome.status == "invalid":
            stats.invalid += 1
        else:
            stats.unknown += 1
        if sample is None:
            remaining -= 1

    if sample is not None:
        for code in codes:
            provider_stats = _linkcheck_provider_stats(stats, code)
            if client.is_paused(code):
                provider_stats["paused_until"] = client.paused_until(code)
                continue
            due = store.sample_live_links(code, sample)
            for row in due:
                if client.is_paused(code):
                    provider_stats["paused_until"] = client.paused_until(code)
                    break
                if not dry_run and not client.reserve_budget(code):
                    provider_stats["budget_exhausted_skipped"] += 1
                    provider_stats["reasons"]["budget_exhausted"] = (
                        provider_stats["reasons"].get("budget_exhausted", 0) + 1
                    )
                    break
                _process_row(code, row, provider_stats)
    else:
        # M5: pre-fetch each provider's own due queue exactly once (never
        # re-queried mid-round -- a dry run never writes, so a re-query
        # would keep finding the SAME still-"due" link every pass) and
        # round-robin consume from these in-memory queues.
        queues: dict[str, list] = {}
        for code in codes:
            _linkcheck_provider_stats(stats, code)
            queues[code] = list(store.due_link_checks(code, limit, now=now()))
        active = [code for code in codes if queues[code]]
        while remaining > 0 and active:
            made_progress = False
            for code in list(active):
                if remaining <= 0:
                    break
                provider_stats = _linkcheck_provider_stats(stats, code)
                if client.is_paused(code):
                    provider_stats["paused_until"] = client.paused_until(code)
                    active.remove(code)
                    continue
                queue = queues[code]
                if not queue:
                    active.remove(code)
                    continue
                if not dry_run and not client.reserve_budget(code):
                    provider_stats["budget_exhausted_skipped"] += 1
                    provider_stats["reasons"]["budget_exhausted"] = (
                        provider_stats["reasons"].get("budget_exhausted", 0) + 1
                    )
                    active.remove(code)
                    continue
                row = queue.pop(0)
                _process_row(code, row, provider_stats)
                made_progress = True
                if not queue:
                    active.remove(code)
            if not made_progress:
                break

    if changed_group_ids:
        store.recount_groups(list(changed_group_ids))

    stats.elapsed_seconds = time.monotonic() - started
    return stats


class BackgroundLinkChecker:
    """Leader-elected background thread: one round (``run_link_check_round``)
    per ``idle_seconds`` (default 60, per contract) while enabled, idle
    otherwise. Mirrors ``BackgroundEnricher``'s leader-lock/heartbeat
    skeleton (separate lock file, ``linkcheck-leader.lock``, so the two
    checkers never contend on the same fcntl lock) but is simpler: there is
    no merge-by-identity step, and pacing/budget/pause all live on the
    ``LinkCheckClient`` itself rather than being round-scoped."""

    def __init__(
        self,
        store_factory: Callable[[], "LibraryStore | None"],
        client_factory: Callable[[], "LinkCheckClient | None"],
        *,
        leader_lock_path: Path,
        conn_factory: Callable[[], "sqlite3.Connection | None"],
        settings_factory: Callable[[], dict] = lambda: {},
        sleep: Callable[[float], None] = time.sleep,
        batch_size: int = 20,
        idle_seconds: float = 60,
        leader_retry_seconds: float = 15.0,
        enabled_check: Callable[[], bool] = lambda: True,
    ) -> None:
        self._store_factory = store_factory
        self._client_factory = client_factory
        self._leader_lock_path = Path(leader_lock_path)
        self._conn_factory = conn_factory
        self._settings_factory = settings_factory
        self._sleep = sleep
        self._batch_size = batch_size
        self._idle_seconds = idle_seconds
        self._leader_retry_seconds = leader_retry_seconds
        self._enabled_check = enabled_check

        self._thread: "threading.Thread | None" = None
        self._stop_flag = threading.Event()
        self._state_lock = threading.Lock()
        self._running = False
        self._leader_attempts = 0
        self._last_round_at: "float | None" = None
        self._last_round_checked = 0
        self._last_error_class: "str | None" = None

    def start(self) -> None:
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, name="linkcheck", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _wait(self, seconds: float) -> None:
        if self._sleep is time.sleep:
            self._stop_flag.wait(seconds)
        else:
            self._sleep(seconds)

    def status(self) -> dict:
        with self._state_lock:
            return {
                "running": self._running,
                "leader": self._running,
                "leader_attempts": self._leader_attempts,
                "last_round_at": self._last_round_at,
                "last_round_checked": self._last_round_checked,
                "last_error_class": self._last_error_class,
            }

    @property
    def idle_seconds(self) -> float:
        return self._idle_seconds

    def _run(self) -> None:
        lock_file = None
        while not self._stop_flag.is_set():
            try:
                if lock_file is None:
                    self._leader_lock_path.parent.mkdir(parents=True, exist_ok=True)
                    lock_file = open(self._leader_lock_path, "a+")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                with self._state_lock:
                    self._leader_attempts += 1
                self._wait(self._leader_retry_seconds)
                continue
            except Exception as exc:  # noqa: BLE001 -- leader acquisition must never kill this thread
                LOG.warning("linkcheck leader acquisition failed error=%s", type(exc).__name__)
                with self._state_lock:
                    self._leader_attempts += 1
                if lock_file is not None:
                    try:
                        lock_file.close()
                    except Exception:
                        pass
                    lock_file = None
                self._wait(self._leader_retry_seconds)
                continue
            else:
                break
        else:
            if lock_file is not None:
                lock_file.close()
            return

        LOG.info("linkcheck leader acquired pid=%s", os.getpid())
        with self._state_lock:
            self._running = True
        try:
            self._loop()
        finally:
            with self._state_lock:
                self._running = False
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def _persist_heartbeat(self) -> None:
        _linkcheck_write_state(self._conn_factory, {"heartbeat_at": int(time.time()), "leader_pid": os.getpid()})

    def _loop(self) -> None:
        while not self._stop_flag.is_set():
            self._persist_heartbeat()
            try:
                store = self._store_factory() if self._enabled_check() else None
                client = self._client_factory() if store is not None else None
                if store is None or client is None:
                    with self._state_lock:
                        self._last_error_class = None
                else:
                    settings = self._settings_factory()
                    stats = run_link_check_round(store, client, settings, limit=self._batch_size)
                    with self._state_lock:
                        self._last_round_at = time.time()
                        self._last_round_checked = stats.checked
                        self._last_error_class = None
                    _linkcheck_write_state(
                        self._conn_factory,
                        {"last_round_at": int(self._last_round_at), "last_round_checked": stats.checked},
                    )
            except Exception as exc:  # noqa: BLE001 -- a round must never kill this thread
                LOG.warning("linkcheck round failed error=%s", type(exc).__name__)
                # Review follow-up (M1 residue): persisted/exposed values are
                # contract reason codes only; the Python class name stays in
                # the log line above.
                with self._state_lock:
                    self._last_error_class = "internal_error"
                _linkcheck_write_state(self._conn_factory, {"last_error_class": "internal_error"})

            if self._stop_flag.is_set():
                break
            self._wait(self._idle_seconds)
