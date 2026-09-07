#!/usr/bin/env python3
"""Optional offline TMDB pre-fill for the personal media-library index
(T3.5; docs/architecture.md §6.2).

    TMDB_API_KEY=... .venv/bin/python scripts/enrich_media_tmdb.py \\
        --index .local-index/library-bundle.sqlite \\
        [--resume] [--retry-failed] [--max-media N] [--with-details] \\
        [--report build/test-artifacts/library-tmdb-report.json]

This is a thin CLI wrapper around ``library_tmdb.enrich_batch`` and
``library_tmdb.merge_by_tmdb`` -- the same functions the production
in-app ``BackgroundEnricher`` calls -- for pre-filling a workspace copy of
the index before a release, when a TMDB key is available.  It is entirely
optional: nothing else in the pipeline depends on it having been run.

The API key is read only from the ``TMDB_API_KEY`` environment variable and
never appears in logs, in the JSON report, or on the command line.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_store as ls  # noqa: E402
import library_tmdb as tmdb  # noqa: E402

DEFAULT_BATCH_SIZE = 20


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Optional offline TMDB pre-fill for a media-library index. "
            "The production background enricher covers this in normal "
            "operation; this script is only for pre-filling a workspace "
            "copy of the index when a TMDB key happens to be available."
        )
    )
    parser.add_argument("--index", required=True, type=Path, help="path to the media-library index (media-library.db)")
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="skip already-cached ok/empty/failed_permanent TMDB responses and already-matched media (default; enrich_batch always does this)",
    )
    parser.add_argument(
        "--retry-failed",
        dest="retry_failed",
        action="store_true",
        default=False,
        help="clear cached failed_retryable responses before running, so this run retries them immediately",
    )
    parser.add_argument("--max-media", dest="max_media", type=int, default=None, help="stop after processing this many media rows in total")
    parser.add_argument(
        "--with-details",
        dest="with_details",
        action="store_true",
        default=False,
        help="also fetch movie|tv/<id> details for exact matches (extra budget; off by default)",
    )
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report (no API key) to this path")
    return parser


def _clear_failed_retryable(store: ls.LibraryStore) -> int:
    conn = store.connect()
    try:
        tmdb.ensure_tables(conn)
        cursor = conn.execute("DELETE FROM tmdb_cache WHERE status = 'failed_retryable'")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


# Fields that describe a snapshot of the *latest* round rather than a
# running total across rounds -- summing them would be meaningless.
_ACCUMULATE_OVERWRITE_FIELDS = {"budget_status", "queue_exhausted"}
# elapsed_seconds is per-round wall time; run() times the whole loop itself
# and reports that instead, so it is deliberately left untouched here.
_ACCUMULATE_SKIP_FIELDS = {"elapsed_seconds"}


def _accumulate(totals: tmdb.EnrichStats, stats: tmdb.EnrichStats) -> None:
    """Fold one round's ``EnrichStats`` into the running ``totals``, field
    by field, so a new numeric field on ``EnrichStats`` is summed
    automatically instead of silently staying at its default until someone
    remembers to list it here by hand."""
    for f in dataclasses.fields(tmdb.EnrichStats):
        name = f.name
        if name in _ACCUMULATE_SKIP_FIELDS:
            continue
        if name == "error_class":
            if stats.error_class is not None:
                totals.error_class = stats.error_class
            continue
        if name in _ACCUMULATE_OVERWRITE_FIELDS:
            setattr(totals, name, getattr(stats, name))
            continue
        setattr(totals, name, getattr(totals, name) + getattr(stats, name))


def _write_report(args: argparse.Namespace, report: dict) -> None:
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace, *, session=None) -> dict:
    """Run one full enrichment pass and return the report dict.

    ``session`` is a test-only injection point (a fake ``requests``-shaped
    object); omitted, ``TmdbClient`` uses a real ``requests.Session()``.

    Uses the same ``LockPaths`` wiring as the production background
    enricher and the ``--library-enrich`` CLI: a non-blocking attempt at
    the shared *run* lock (never waits -- a concurrent manual/background
    enrich just means this invocation reports busy and exits) and its own
    client's *budget* lock, distinct from the run lock so retrying a
    request from inside the run-lock-held section can never deadlock
    against itself (the P0 bug this fix set out to prevent).
    """
    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        raise SystemExit("TMDB_API_KEY environment variable is not set; nothing to do")

    if not args.index.exists():
        raise SystemExit(f"library index not found: {args.index}")

    lock_paths = tmdb.LockPaths.for_data_dir(args.index.parent)
    lock_paths.run.parent.mkdir(parents=True, exist_ok=True)
    run_lock_file = open(lock_paths.run, "a+")
    try:
        fcntl.flock(run_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        run_lock_file.close()
        report = {"ok": False, "error": "TMDB_ENRICH_BUSY"}
        _write_report(args, report)
        return report

    try:
        store = ls.LibraryStore(args.index)

        conn = store.connect()
        try:
            tmdb.ensure_tables(conn)
            conn.commit()
        finally:
            conn.close()

        cleared = _clear_failed_retryable(store) if args.retry_failed else 0

        def conn_factory():
            return sqlite3.connect(str(args.index))

        limits = tmdb.effective_limits({}, os.environ)
        client = tmdb.TmdbClient(
            api_key,
            conn_factory=conn_factory,
            lock_path=lock_paths.budget,
            limits=limits,
            session=session,
        )

        totals = tmdb.EnrichStats()
        remaining = args.max_media
        started = time.monotonic()

        while remaining is None or remaining > 0:
            limit = DEFAULT_BATCH_SIZE if remaining is None else min(DEFAULT_BATCH_SIZE, remaining)
            stats = tmdb.enrich_batch(store, client, limit=limit, with_details=args.with_details)
            _accumulate(totals, stats)
            if remaining is not None:
                remaining -= stats.candidates_considered
            if stats.candidates_considered == 0 or stats.error_class is not None:
                break

        merged = tmdb.merge_by_tmdb(store)
        elapsed = time.monotonic() - started
    finally:
        fcntl.flock(run_lock_file.fileno(), fcntl.LOCK_UN)
        run_lock_file.close()

    report = {
        "candidates_considered": totals.candidates_considered,
        "requests_made": totals.requests_made,
        "cache_hits": totals.cache_hits,
        "budget_status": totals.budget_status,
        "matched_exact": totals.matched_exact,
        "matched_candidate": totals.matched_candidate,
        "matched_needs_review": totals.matched_needs_review,
        "matched_unmatched": totals.matched_unmatched,
        "http_429": totals.http_429,
        "http_5xx": totals.http_5xx,
        "merged_by_tmdb": merged,
        "retry_failed_cleared": cleared,
        "elapsed_seconds": elapsed,
        "error_class": totals.error_class,
    }

    _write_report(args, report)

    return report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    report = run(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report.get("error") == "TMDB_ENRICH_BUSY" else 0


if __name__ == "__main__":
    raise SystemExit(main())
