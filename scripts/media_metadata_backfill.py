#!/usr/bin/env python3
"""Resumable metadata-completion CLI (see docs/metadata-enrichment.md).

    TMDB_API_KEY=... .venv/bin/python scripts/media_metadata_backfill.py \\
        --phase verify|enrich|retry|report \\
        --db <staging.sqlite> \\
        [--limit N] [--ids 1,2,3] [--daily-budget N] [--concurrency N] \\
        [--dry-run] [--no-network] [--resume]

Built entirely on the same ``library_tmdb`` primitives as
``enrich_batch``/``BackgroundEnricher`` (``TmdbClient``, the hint/search/
TVmaze judge rules, ``Budget``/``RateLimiter``, ``EnrichStats``) -- this
script never re-implements TMDB request/retry/cache/scoring logic, only
the row selection and metadata-field writing that ``verify``/``enrich``/
``retry`` need on top of identity matching (§2: "身份匹配" is
``enrich_batch``'s job; this script's ``enrich``/``retry`` phases are
"元数据补全", §4).

Phases:
  verify  -- identity-confirm rows (``--ids`` explicit, via
             ``library_tmdb.verify_media_ids``, or the default queue via
             ``library_tmdb.enrich_batch``). Never fetches details/images.
  enrich  -- for ``match_status='exact'`` rows missing an overview or
             poster, fetch ``/<kind>/<tmdb_id>?append_to_response=
             external_ids`` (zh-CN, one en-US retry only if the zh
             overview is empty) and write overview/poster/backdrop/
             genres/imdb_id/metadata_status; ``ratings_json`` is MERGED
             (never replaced) with ``tmdb`` plus, when an imdb_id is now
             known, an ``imdb_ratings`` join into ``ratings_json.imdb``
             -- ``ratings_status`` reflects every applicable source, and a
             failed fetch sets it to ``error`` (fixed ``ratings_error``
             class) only when nothing already-valid would be lost.
  retry   -- same as ``enrich``, restricted to rows whose last attempt
             recorded a *retryable* failure (``metadata_error`` prefixed
             ``"retryable:"``) -- a permanent empty/4xx result is never
             retried.
  report  -- read-only JSON counts (match/metadata/ratings status,
             ``tmdb_cache`` status counts, latest ``tmdb_budget`` row,
             ``media_metadata_attempts`` counts by phase/status). No
             network, no ``TmdbClient`` construction.

``--no-network`` skips constructing a ``TmdbClient`` entirely for verify/
enrich/retry and instead reports how many rows WOULD be selected (a safe,
side-effect-free preview over a staging copy) -- this is the flag
combination this task's report is required to run with (``--no-network
--dry-run``).  T15 fix wave 1: ``--dry-run`` alone (without ``--no-network``)
is now treated EXACTLY the same as ``--no-network`` for every phase,
including ``enrich``/``retry`` -- a dry run must never spend TMDB quota or
write cache/budget rows, so it never constructs a ``TmdbClient`` or issues
an HTTP request either; it is purely a selection-count preview built from
the already-installed ``media``/``tmdb_cache`` state. (Previously
``enrich``/``retry`` still made the real request under ``--dry-run``, only
skipping the final ``media``/``media_metadata_attempts`` write -- that let
a "dry run" burn through the daily budget and populate the response cache,
which is exactly what ``--dry-run`` must never do.) ``process_enrich_row``
itself still accepts its own ``dry_run`` parameter (make the request,
skip the write) as a lower-level primitive for direct/programmatic use;
``run()`` simply never reaches it with ``dry_run=True`` from the CLI flag
any more.

No file this script touches ever contains a Cookie, share link, access
code or TMDB API key: ``TMDB_API_KEY`` is read only from the environment,
``media_metadata_attempts.query_key`` is a short SHA-256 digest (never the
raw title), and every JSON output is counts/ids/status strings only.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_store as ls  # noqa: E402
import library_tmdb as tmdb  # noqa: E402

PHASES = ("verify", "enrich", "retry", "report")
DEFAULT_METADATA_STATUSES = ("pending", "error")
_RETRYABLE_ERROR_PREFIX = "retryable:"
_PERMANENT_ERROR_PREFIX = "permanent:"


def build_query_key(*parts: object) -> str:
    """A short, non-reversible digest for ``media_metadata_attempts.
    query_key`` (§5.2: "不要把敏感查询参数写入日志") -- never the raw
    title, a URL or an access code."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_attempt(
    store: "ls.LibraryStore", *, media_id: int, phase: str, source: str, query_key: str, status: str,
    error_class: str | None = None, response_cache_key: str | None = None,
) -> None:
    """Append one row to the §5.2 audit table. Never stores a title, URL
    or credential -- only ids, phase/source/status strings and a
    pre-hashed ``query_key``."""
    conn = store.connect()
    try:
        tmdb.ensure_tables(conn)
        conn.execute(
            "INSERT INTO media_metadata_attempts "
            "(media_id, phase, source, query_key, status, http_status, error_class, response_cache_key, attempted_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (media_id, phase, source, query_key, status, error_class, response_cache_key, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# row selection
# ---------------------------------------------------------------------------


def select_verify_ids(conn: sqlite3.Connection, ids: list[int]) -> list[int]:
    """``--ids`` filtered to rows not already ``match_status='exact'`` --
    there is nothing to verify for an already-confirmed identity."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT id FROM media WHERE id IN ({placeholders}) AND match_status != 'exact'", ids).fetchall()
    return [row[0] for row in rows]


def select_metadata_rows(
    conn: sqlite3.Connection, *, ids: list[int], limit: int, statuses: tuple[str, ...],
    resume_after: int | None, retryable_only: bool = False,
) -> list[sqlite3.Row]:
    """Rows eligible for ``enrich``/``retry``: identity already confirmed
    (``match_status='exact'``) and metadata incomplete (missing overview
    or poster) -- §5.1: "enrich 只处理 match_status=exact 且元数据不完整
    的条目". Restricted to explicit ``ids`` (ignoring ``statuses``/
    ``resume_after``) or the default ``metadata_status`` filter (+ the
    ``retry`` phase's retryable-only predicate + an optional resume
    cursor)."""
    base = (
        "SELECT id, media_type, tmdb_id, overview, poster_path, imdb_id, tvmaze_id, ratings_json, ratings_status "
        "FROM media "
        "WHERE match_status='exact' AND (overview IS NULL OR overview='' OR poster_path IS NULL)"
    )
    if retryable_only:
        base += f" AND metadata_error LIKE '{_RETRYABLE_ERROR_PREFIX}%'"
    if ids:
        placeholders = ",".join("?" for _ in ids)
        return conn.execute(base + f" AND id IN ({placeholders}) ORDER BY id", ids).fetchall()
    status_placeholders = ",".join("?" for _ in statuses)
    query = base + f" AND metadata_status IN ({status_placeholders})"
    params: list = list(statuses)
    if resume_after is not None:
        query += " AND id > ?"
        params.append(resume_after)
    query += " ORDER BY id LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


def _resume_cursor_key(phase: str) -> str:
    return f"backfill_{phase}_last_id"


def _read_resume_cursor(store: "ls.LibraryStore", phase: str) -> int | None:
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT value FROM tmdb_enricher_state WHERE key=?", (_resume_cursor_key(phase),)).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _write_resume_cursor(store: "ls.LibraryStore", phase: str, last_id: int) -> None:
    conn = store.connect()
    try:
        tmdb.ensure_tables(conn)
        conn.execute(
            "INSERT INTO tmdb_enricher_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (_resume_cursor_key(phase), str(last_id), int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_resume_cursor(store: "ls.LibraryStore", phase: str) -> None:
    """Fix wave 1: drop ``phase``'s resume cursor entirely -- called when a
    round's selection came back with fewer rows than ``--limit`` (the
    ``id > cursor`` queue is exhausted for now). Leaving a stale cursor in
    place would permanently hide any row whose id is at or below it that
    later becomes pending again (a retry reset, a fresh import, ...); the
    next invocation with no cursor re-scans from the very start and picks
    such rows back up."""
    conn = store.connect()
    try:
        tmdb.ensure_tables(conn)
        conn.execute("DELETE FROM tmdb_enricher_state WHERE key=?", (_resume_cursor_key(phase),))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# enrich/retry: one row
# ---------------------------------------------------------------------------


def process_enrich_row(store: "ls.LibraryStore", client: "tmdb.TmdbClient", row: sqlite3.Row, *, phase: str, dry_run: bool) -> dict:
    """Fetch details for one ``exact`` media row and write the §4.2
    metadata fields. Never clears a previously-confirmed field on failure
    (§4.2: "失败只更新 metadata_error、重试信息和状态，不清空此前已经确认
    的字段") -- a failed fetch only ever sets ``metadata_status='error'``/
    ``metadata_error``."""
    media_id, kind, tmdb_id = row["id"], row["media_type"], row["tmdb_id"]
    if kind not in ("movie", "tv") or not tmdb_id:
        if not dry_run:
            record_attempt(store, media_id=media_id, phase=phase, source="tmdb", query_key=build_query_key(phase, media_id), status="skipped")
        return {"media_id": media_id, "status": "skipped", "reason": "unsupported_media_type_or_missing_tmdb_id"}

    query_key = build_query_key(phase, kind, tmdb_id)
    try:
        entry = client.details(kind, tmdb_id, language="zh-CN", append_to_response="external_ids")
    except (tmdb.BudgetExhausted, tmdb.InvalidApiKey) as exc:
        if not dry_run:
            record_attempt(
                store, media_id=media_id, phase=phase, source="tmdb", query_key=query_key,
                status="stopped", error_class=type(exc).__name__,
            )
        return {"media_id": media_id, "status": "stopped", "error_class": type(exc).__name__}

    if entry.status not in ("ok", "empty"):
        error_class = entry.error_class or "Unknown"
        prefix = _RETRYABLE_ERROR_PREFIX if entry.status == "failed_retryable" else _PERMANENT_ERROR_PREFIX
        if not dry_run:
            conn = store.connect()
            try:
                # T15 fix wave 1 (item 3b): a failed fetch marks
                # ratings_status='error' too -- but ONLY when there is
                # nothing already-valid there to protect (never downgrade
                # an existing complete/partial rating set just because a
                # later re-fetch attempt failed).
                if row["ratings_status"] in ("complete", "partial"):
                    conn.execute(
                        "UPDATE media SET metadata_status='error', metadata_error=?, metadata_fetched_at=? WHERE id=?",
                        (f"{prefix}{error_class}", _now_iso(), media_id),
                    )
                else:
                    conn.execute(
                        "UPDATE media SET metadata_status='error', metadata_error=?, metadata_fetched_at=?, "
                        "ratings_status='error', ratings_error=? WHERE id=?",
                        (f"{prefix}{error_class}", _now_iso(), tmdb.RATINGS_FETCH_ERROR_CLASS, media_id),
                    )
                conn.commit()
            finally:
                conn.close()
            record_attempt(store, media_id=media_id, phase=phase, source="tmdb", query_key=query_key, status=entry.status, error_class=error_class)
        return {"media_id": media_id, "status": "error", "error_class": error_class}

    detail = entry.payload[0] if entry.payload else {}
    overview = (detail.get("overview") or "").strip()
    if not overview:
        # §3.3/§4.2: "中文简介为空的条目补一次 en-US" -- only the overview
        # is ever taken from this second request, never poster/genres/ids.
        try:
            en_entry = client.details(kind, tmdb_id, language="en-US", append_to_response="external_ids")
        except (tmdb.BudgetExhausted, tmdb.InvalidApiKey) as exc:
            if not dry_run:
                record_attempt(
                    store, media_id=media_id, phase=phase, source="tmdb", query_key=query_key,
                    status="stopped", error_class=type(exc).__name__,
                )
            return {"media_id": media_id, "status": "stopped", "error_class": type(exc).__name__}
        if en_entry.status == "ok" and en_entry.payload:
            overview = (en_entry.payload[0].get("overview") or "").strip()

    updates: dict = {}
    if overview:
        updates["overview"] = overview
    if detail.get("poster_path"):
        updates["poster_path"] = detail["poster_path"]
    if detail.get("backdrop_path"):
        updates["backdrop_path"] = detail["backdrop_path"]
    genres = detail.get("genres") or []
    if genres:
        updates["genres_json"] = json.dumps([g["name"] for g in genres if "name" in g], ensure_ascii=False)
    external_ids = detail.get("external_ids") or {}
    if external_ids.get("imdb_id"):
        updates["imdb_id"] = external_ids["imdb_id"]

    # T15 fix wave 1 (item 3a/3b): merge into the row's existing
    # ratings_json (never replace it wholesale -- a prior _write_exact may
    # already have written e.g. an imdb/tvmaze entry here) and compute
    # ratings_status from every APPLICABLE source, not just tmdb.
    imdb_id_for_ratings = updates.get("imdb_id") or row["imdb_id"]
    new_ratings: dict = {}
    vote_average, vote_count = detail.get("vote_average"), detail.get("vote_count")
    tmdb_rating = tmdb._tmdb_rating_entry(vote_average, vote_count)
    if tmdb_rating is not None:
        new_ratings["tmdb"] = tmdb_rating

    imdb_row_present = False
    if imdb_id_for_ratings:
        conn = store.connect(readonly=True)
        try:
            imdb_rating = tmdb.lookup_imdb_rating(conn, imdb_id_for_ratings)
        finally:
            conn.close()
        imdb_row_present = imdb_rating is not None
        if imdb_rating is not None:
            new_ratings["imdb"] = imdb_rating

    # Fix wave 2 (item 2): compute ratings_status_for UNCONDITIONALLY on a
    # successful fetch, even when new_ratings is empty -- 'none' must be
    # reachable when nothing applicable came back, not silently skipped
    # (which left a stale ratings_status/ratings_error in place forever).
    # merge_ratings_json only ever overwrites the given keys, so an
    # already-valid entry from a prior fetch is preserved either way.
    updates["ratings_json"] = tmdb.merge_ratings_json(row["ratings_json"], new_ratings)
    merged_ratings = json.loads(updates["ratings_json"])
    updates["ratings_status"] = tmdb.ratings_status_for(
        merged_ratings, imdb_id=imdb_id_for_ratings, imdb_row_present=imdb_row_present, tvmaze_id=row["tvmaze_id"],
    )
    updates["ratings_fetched_at"] = datetime.now(timezone.utc).date().isoformat()
    updates["ratings_error"] = None  # a successful fetch clears a stale prior ratings_error

    final_overview = updates.get("overview") or row["overview"]
    final_poster = updates.get("poster_path") or row["poster_path"]
    metadata_status = "complete" if (final_overview and final_poster) else "partial"

    if not dry_run:
        set_clauses = ", ".join(f"{key}=?" for key in updates)
        query = "UPDATE media SET metadata_status=?, metadata_source='tmdb', metadata_fetched_at=?, metadata_error=NULL"
        if set_clauses:
            query += ", " + set_clauses
        query += " WHERE id=?"
        params = [metadata_status, _now_iso(), *updates.values(), media_id]
        conn = store.connect()
        try:
            conn.execute(query, params)
            conn.commit()
        finally:
            conn.close()
        record_attempt(store, media_id=media_id, phase=phase, source="tmdb", query_key=query_key, status=entry.status, response_cache_key=f"details:{kind}:zh-CN:{tmdb_id}:external_ids")

    return {"media_id": media_id, "status": metadata_status}


# ---------------------------------------------------------------------------
# report (read-only, no client)
# ---------------------------------------------------------------------------


def run_report(store: "ls.LibraryStore") -> dict:
    conn = store.connect(readonly=True)
    try:
        match_counts = dict(conn.execute("SELECT match_status, COUNT(*) FROM media GROUP BY match_status").fetchall())
        metadata_counts = dict(conn.execute("SELECT metadata_status, COUNT(*) FROM media GROUP BY metadata_status").fetchall())
        ratings_counts = dict(conn.execute("SELECT ratings_status, COUNT(*) FROM media GROUP BY ratings_status").fetchall())
        try:
            cache_counts = dict(conn.execute("SELECT status, COUNT(*) FROM tmdb_cache GROUP BY status").fetchall())
        except sqlite3.OperationalError:
            cache_counts = {}
        budget = None
        try:
            budget_row = conn.execute("SELECT day, used, budget FROM tmdb_budget ORDER BY day DESC LIMIT 1").fetchone()
            if budget_row is not None:
                budget = {"day": budget_row[0], "used": budget_row[1], "budget": budget_row[2]}
        except sqlite3.OperationalError:
            pass
        attempts: dict = {}
        try:
            for phase, status, count in conn.execute("SELECT phase, status, COUNT(*) FROM media_metadata_attempts GROUP BY phase, status").fetchall():
                attempts[f"{phase}:{status}"] = count
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return {
        "phase": "report",
        "match_status": match_counts,
        "metadata_status": metadata_counts,
        "ratings_status": ratings_counts,
        "cache": cache_counts,
        "budget": budget,
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# no-network preview
# ---------------------------------------------------------------------------


def run_no_network_preview(store: "ls.LibraryStore", *, phase: str, ids: list[int], limit: int) -> dict:
    """A side-effect-free row-count preview for ``--no-network`` (and, for
    every phase now -- see the module docstring, T15 fix wave 1 -- also
    plain ``--dry-run``) -- reads the same selection queries the real run
    would use, makes no HTTP request and no write.

    ``verify`` with no explicit ``--ids`` delegates straight to
    ``enrich_batch`` (see ``run()``), so the preview must count exactly what
    THAT queue would take, not a naive ``match_status IN ('unmatched',
    'needs_review')`` count (fix wave 1: the naive count over-counted --
    ``enrich_batch`` skips an ``unmatched`` row whose primary search cache
    entry is already resolved, per ``_select_fresh_candidates``, and only
    ever pulls in the never-queried subset of ``needs_review`` rows, per
    ``_select_review_pending_unqueried``, and only once the ``unmatched``
    queue itself is exhausted). Reusing those two private selectors directly
    keeps this preview byte-for-byte in sync with the real queue without
    duplicating its logic."""
    conn = store.connect(readonly=True)
    try:
        if phase == "verify":
            if ids:
                would = len(select_verify_ids(conn, ids))
            else:
                fresh, exhausted = tmdb._select_fresh_candidates(conn, limit)
                review_rows: list = []
                if exhausted and len(fresh) < limit:
                    review_rows, _ = tmdb._select_review_pending_unqueried(conn, limit - len(fresh))
                would = len(fresh) + len(review_rows)
        else:
            statuses = ("error",) if phase == "retry" else DEFAULT_METADATA_STATUSES
            rows = select_metadata_rows(conn, ids=ids, limit=limit, statuses=statuses, resume_after=None, retryable_only=(phase == "retry"))
            would = len(rows)
    finally:
        conn.close()
    return {"phase": phase, "no_network": True, "dry_run": True, "would_process": would}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media_metadata_backfill.py",
        description="Resumable identity-verify/metadata-enrich/retry/report batch tool for a media-library index.",
    )
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--ids", type=str, default=None, help="comma-separated media ids")
    parser.add_argument("--daily-budget", dest="daily_budget", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=False)
    parser.add_argument("--no-network", dest="no_network", action="store_true", default=False)
    parser.add_argument("--resume", action="store_true", default=False)
    return parser


def _parse_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(part) for part in raw.split(",") if part.strip()]


def _build_client(db_path: Path, args: argparse.Namespace) -> "tmdb.TmdbClient":
    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        raise RuntimeError("TMDB_API_KEY is not set")
    limits = tmdb.Limits(
        max_concurrency=max(1, args.concurrency),
        min_interval_ms=tmdb.DEFAULT_LIMITS.min_interval_ms,
        daily_budget=args.daily_budget if args.daily_budget is not None else tmdb.DEFAULT_LIMITS.daily_budget,
    )
    lock_path = db_path.parent / "media-metadata-backfill.lock"
    return tmdb.TmdbClient(api_key, conn_factory=lambda: sqlite3.connect(db_path), lock_path=lock_path, limits=limits)


def run(args: argparse.Namespace) -> dict:
    db_path = Path(args.db)
    store = ls.LibraryStore(db_path)
    ids = _parse_ids(args.ids)

    if args.phase == "report":
        return run_report(store)

    # T15 fix wave 1: --dry-run must never spend TMDB quota or write cache/
    # budget for ANY phase, so it is now always treated exactly like
    # --no-network -- a side-effect-free selection preview, never a
    # TmdbClient/HTTP request (see module docstring).
    if args.no_network or args.dry_run:
        return run_no_network_preview(store, phase=args.phase, ids=ids, limit=args.limit)

    client = _build_client(db_path, args)

    if args.phase == "verify":
        if ids:
            stats = tmdb.verify_media_ids(store, client, ids)
        else:
            stats = tmdb.enrich_batch(store, client, limit=args.limit)
        return {"phase": "verify", "stats": dataclasses.asdict(stats)}

    # enrich / retry
    statuses = ("error",) if args.phase == "retry" else DEFAULT_METADATA_STATUSES
    resume_after = _read_resume_cursor(store, args.phase) if (args.resume and not ids) else None
    conn = store.connect(readonly=True)
    try:
        rows = select_metadata_rows(
            conn, ids=ids, limit=args.limit, statuses=statuses, resume_after=resume_after,
            retryable_only=(args.phase == "retry"),
        )
    finally:
        conn.close()

    # T15 fix wave 1: dispatch in bounded chunks (width = --concurrency)
    # rather than submitting the whole selection at once, so a chunk that
    # comes back with a "stopped" outcome (BudgetExhausted/InvalidApiKey --
    # see process_enrich_row) stops the NEXT chunk from ever being
    # dispatched ("stop the batch on the first stopped"). A little
    # unavoidable overshoot is possible within the chunk that contains the
    # stop (rows already in flight when it happens run to completion), but
    # nothing beyond that chunk is ever attempted. ``args.dry_run`` is
    # always False by this point (see the early-return above), so every row
    # here does a real write.
    outcomes: list[dict] = []
    if rows:
        chunk_size = max(1, args.concurrency)
        with ThreadPoolExecutor(max_workers=chunk_size) as executor:
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start:start + chunk_size]
                futures = [
                    executor.submit(process_enrich_row, store, client, row, phase=args.phase, dry_run=False)
                    for row in chunk
                ]
                chunk_outcomes = [future.result() for future in futures]
                outcomes.extend(chunk_outcomes)
                if any(outcome["status"] == "stopped" for outcome in chunk_outcomes):
                    break

    # T15 fix wave 1: the resume cursor must advance only past rows that
    # ACTUALLY completed -- never past a "stopped" row (or anything after
    # it, which this chunked dispatch never even attempts) -- so an
    # interrupted round is retried from the right place next time, not
    # skipped. Separately: when this round's selection came back with
    # fewer rows than --limit, the id>cursor queue is exhausted for now --
    # clear the cursor entirely (rather than leaving it pointing at the
    # last id ever advanced to) so a row at or below it that later becomes
    # pending again (a retry reset, a fresh import, ...) is not hidden from
    # every future --resume run forever.
    #
    # Fix wave 2 (item 1): a chunk can contain BOTH a stopped row and a
    # higher-id row that completed concurrently in that same chunk (e.g.
    # --concurrency 2: row 1 stops, row 2 finishes right alongside it).
    # ``max(completed_ids)`` alone would then advance the cursor PAST the
    # stopped row, permanently skipping it on the next --resume. Cap the
    # cursor at the first stop: only a completed row with id < the lowest
    # stopped id counts.
    if not ids:
        if len(rows) < args.limit:
            # Fix wave 2 (item 4): only a --resume run should reset
            # persisted resume state -- a bare run never consulted the
            # cursor to select `rows` in the first place (resume_after is
            # always None for it), so it has no business clearing out a
            # --resume cron job's progress marker just because ITS OWN,
            # unrelated full-table view happened to come up short.
            if args.resume:
                _clear_resume_cursor(store, args.phase)
        elif rows:
            stopped_ids = [row["id"] for row, outcome in zip(rows, outcomes) if outcome["status"] == "stopped"]
            completed_ids = [row["id"] for row, outcome in zip(rows, outcomes) if outcome["status"] != "stopped"]
            if stopped_ids:
                stop_at = min(stopped_ids)
                completed_ids = [rid for rid in completed_ids if rid < stop_at]
            if completed_ids:
                _write_resume_cursor(store, args.phase, max(completed_ids))

    return {
        "phase": args.phase,
        "processed": len(outcomes),
        "by_status": dict(Counter(outcome["status"] for outcome in outcomes)),
        "budget": client.budget_status(),
    }


def main(argv: list | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:  # noqa: BLE001 -- CLI top level: report, never crash with a traceback
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps({**result, "ok": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
