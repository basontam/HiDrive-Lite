"""Tests for scripts/import_media_library.py's T15 additions: --hints
(Codex's offline IMDb candidate hints) and --imdb-ratings.

Uses the same synthetic ``library_sources`` fixture as
tests/test_import_media_library.py; hint rows are built FROM the
just-aggregated ``agg.media`` dict (never hand-guessed titles) so a hint
row's computed ``media_identity`` is guaranteed to match one already in the
bundle.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import library_store as ls  # noqa: E402
import import_media_library as importer  # noqa: E402
from test_import_media_library import _read_all_sources  # noqa: E402


def _write_jsonl_gz(path: Path, rows: list) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_jsonl(path: Path, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _aggregate(library_sources):
    rows_by_source, _meta = _read_all_sources(library_sources)
    return importer.aggregate(rows_by_source)


def _hint_row_for(agg, identity: str, *, decision: str, media_id: int = 1, candidates=None) -> dict:
    m = agg.media[identity]
    return {
        "media_id": media_id,
        "title_zh": m["title_zh"],
        "title_original": m["title_original"],
        "year": m["year"],
        "media_type": m["media_type"],
        "decision": decision,
        "candidate_count": len(candidates or []),
        "candidates": candidates or [],
    }


def _one_candidate(imdb_id="tt0000123") -> dict:
    return {
        "imdb_id": imdb_id, "imdb_type": "movie", "start_year": 2024,
        "primary_title": "片名", "original_title": "Title", "score": 0.99, "reasons": ["title_alias_exact"],
    }


# --- load_hints ------------------------------------------------------------


def test_load_hints_matches_by_identity_and_counts_by_decision(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    identities = sorted(agg.media)
    assert len(identities) >= 2, "fixture should have at least 2 media rows"
    matched_identity = identities[0]

    hints_path = tmp_path / "hints.jsonl.gz"
    _write_jsonl_gz(hints_path, [
        _hint_row_for(agg, matched_identity, decision="proposed_exact", candidates=[_one_candidate()]),
        # an identity that cannot possibly exist in this bundle
        {
            "media_id": 999, "title_zh": "绝对不存在的片名12345", "title_original": None, "year": 1901,
            "media_type": "movie", "decision": "no_imdb_candidate", "candidate_count": 0, "candidates": [],
        },
    ])

    result = importer.load_hints(hints_path, set(agg.media))

    assert result.rows_total == 2
    assert result.by_decision == {"proposed_exact": 1, "no_imdb_candidate": 1}
    assert result.unmatched_identity == 1
    assert len(result.matched) == 1
    identity, source, decision, candidates = result.matched[0]
    assert identity == matched_identity
    assert source == "imdb_offline"
    assert decision == "proposed_exact"
    assert candidates == [_one_candidate()]
    assert result.imdb_ids == {"tt0000123"}


def test_load_hints_caps_at_three_candidates_and_strips_unknown_fields(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    identity = sorted(agg.media)[0]
    candidates = [
        {**_one_candidate(f"tt000000{i}"), "evidence_blob": "should be dropped", "url": "https://example.test/x"}
        for i in range(5)
    ]
    hints_path = tmp_path / "hints.jsonl"
    _write_jsonl(hints_path, [_hint_row_for(agg, identity, decision="proposed_candidate", candidates=candidates)])

    result = importer.load_hints(hints_path, set(agg.media))

    assert len(result.matched) == 1
    _identity, _source, _decision, kept = result.matched[0]
    assert len(kept) == 3
    for candidate in kept:
        assert "evidence_blob" not in candidate
        assert "url" not in candidate
        assert set(candidate) <= set(importer._HINT_CANDIDATE_FIELDS)


def test_load_hints_supports_plain_jsonl_not_only_gz(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    identity = sorted(agg.media)[0]
    hints_path = tmp_path / "hints.jsonl"
    _write_jsonl(hints_path, [_hint_row_for(agg, identity, decision="proposed_exact", candidates=[_one_candidate()])])

    result = importer.load_hints(hints_path, set(agg.media))
    assert result.rows_total == 1
    assert len(result.matched) == 1


# --- write_bundle(hints=...) -------------------------------------------------


def test_write_bundle_writes_hints_table(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    identity = sorted(agg.media)[0]
    hints_path = tmp_path / "hints.jsonl.gz"
    _write_jsonl_gz(hints_path, [_hint_row_for(agg, identity, decision="proposed_exact", candidates=[_one_candidate()])])
    hints_result = importer.load_hints(hints_path, set(agg.media))

    bundle_path = importer.write_bundle(agg, tmp_path / "out", source_hashes={"x": "y"}, hints=hints_result)

    conn = sqlite3.connect(str(bundle_path))
    try:
        row = conn.execute(
            "SELECT source, decision, candidate_count, candidates_json FROM tmdb_hints WHERE media_identity=?",
            (identity,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    source, decision, candidate_count, candidates_json = row
    assert source == "imdb_offline"
    assert decision == "proposed_exact"
    assert candidate_count == 1
    assert json.loads(candidates_json) == [_one_candidate()]


def test_write_bundle_without_hints_leaves_table_empty(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    bundle_path = importer.write_bundle(agg, tmp_path / "out", source_hashes={"x": "y"})
    conn = sqlite3.connect(str(bundle_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM tmdb_hints").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# --- imdb ratings ------------------------------------------------------------


def test_load_imdb_ratings_skips_gracefully_when_missing(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl.gz"
    rows, result = importer.load_imdb_ratings(missing, {"tt0000123"}, as_of="2026-09-06")
    assert rows == []
    assert result.file_found is False
    assert result.rows_loaded == 0


def test_load_imdb_ratings_keeps_only_relevant_ids(tmp_path):
    ratings_path = tmp_path / "imdb-ratings.jsonl.gz"
    _write_jsonl_gz(ratings_path, [
        {"imdb_id": "tt0000123", "rating": 8.7, "votes": 31198},
        {"imdb_id": "tt9999999", "rating": 5.0, "votes": 10},
    ])
    rows, result = importer.load_imdb_ratings(ratings_path, {"tt0000123"}, as_of="2026-09-06")
    assert result.file_found is True
    assert result.rows_loaded == 1
    assert rows == [("tt0000123", 8.7, 31198, "2026-09-06")]


def test_write_bundle_with_imdb_ratings(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    bundle_path = importer.write_bundle(
        agg, tmp_path / "out", source_hashes={"x": "y"},
        imdb_ratings_rows=[("tt0000123", 8.7, 31198, "2026-09-06")],
    )
    conn = sqlite3.connect(str(bundle_path))
    try:
        row = conn.execute("SELECT rating, votes, as_of FROM imdb_ratings WHERE imdb_id=?", ("tt0000123",)).fetchone()
    finally:
        conn.close()
    assert row == (8.7, 31198, "2026-09-06")


# --- run()/main() CLI wiring --------------------------------------------------


def test_dry_run_with_hints_reports_counts_without_writing_bundle(library_sources, tmp_path):
    rows_by_source, _meta = _read_all_sources(library_sources)
    agg_preview = importer.aggregate(rows_by_source)
    identity = sorted(agg_preview.media)[0]
    hints_path = tmp_path / "hints.jsonl.gz"
    _write_jsonl_gz(hints_path, [
        _hint_row_for(agg_preview, identity, decision="proposed_exact", candidates=[_one_candidate()]),
        {
            "media_id": 999, "title_zh": "绝对不存在的片名12345", "title_original": None, "year": 1901,
            "media_type": "movie", "decision": "no_imdb_candidate", "candidate_count": 0, "candidates": [],
        },
    ])

    exit_code = importer.main([
        "--source-dir", str(library_sources),
        "--output", str(tmp_path / "out"),
        "--hints", str(hints_path),
        "--report", str(tmp_path / "report.json"),
    ])
    assert exit_code == 0
    assert not (tmp_path / "out" / "library-bundle.sqlite").exists()

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["hints"]["rows_total"] == 2
    assert report["hints"]["by_decision"] == {"proposed_exact": 1, "no_imdb_candidate": 1}
    assert report["hints"]["matched"] == 1
    assert report["hints"]["unmatched_identity"] == 1


def test_write_with_hints_and_imdb_ratings_populates_both_tables(library_sources, tmp_path):
    rows_by_source, _meta = _read_all_sources(library_sources)
    agg_preview = importer.aggregate(rows_by_source)
    identity = sorted(agg_preview.media)[0]
    hints_path = tmp_path / "hints.jsonl.gz"
    _write_jsonl_gz(hints_path, [_hint_row_for(agg_preview, identity, decision="proposed_exact", candidates=[_one_candidate()])])
    ratings_path = tmp_path / "imdb-ratings.jsonl.gz"
    _write_jsonl_gz(ratings_path, [{"imdb_id": "tt0000123", "rating": 8.7, "votes": 31198}])

    exit_code = importer.main([
        "--source-dir", str(library_sources),
        "--output", str(tmp_path / "out"),
        "--write",
        "--hints", str(hints_path),
        "--imdb-ratings", str(ratings_path),
        "--report", str(tmp_path / "report.json"),
    ])
    assert exit_code == 0

    bundle_path = tmp_path / "out" / "library-bundle.sqlite"
    conn = sqlite3.connect(str(bundle_path))
    try:
        hint_count = conn.execute("SELECT COUNT(*) FROM tmdb_hints").fetchone()[0]
        rating_row = conn.execute("SELECT rating, votes FROM imdb_ratings WHERE imdb_id='tt0000123'").fetchone()
    finally:
        conn.close()
    assert hint_count == 1
    assert rating_row == (8.7, 31198)

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["imdb_ratings"] == {"file_found": True, "rows_loaded": 1}


def test_missing_imdb_ratings_file_does_not_fail_a_write_run(library_sources, tmp_path):
    rows_by_source, _meta = _read_all_sources(library_sources)
    agg_preview = importer.aggregate(rows_by_source)
    identity = sorted(agg_preview.media)[0]
    hints_path = tmp_path / "hints.jsonl.gz"
    _write_jsonl_gz(hints_path, [_hint_row_for(agg_preview, identity, decision="proposed_exact", candidates=[_one_candidate()])])

    exit_code = importer.main([
        "--source-dir", str(library_sources),
        "--output", str(tmp_path / "out"),
        "--write",
        "--hints", str(hints_path),
        "--imdb-ratings", str(tmp_path / "does-not-exist-yet.jsonl.gz"),
        "--report", str(tmp_path / "report.json"),
    ])
    assert exit_code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["imdb_ratings"] == {"file_found": False, "rows_loaded": 0}


def test_hints_report_never_contains_a_url(library_sources, tmp_path):
    rows_by_source, _meta = _read_all_sources(library_sources)
    agg_preview = importer.aggregate(rows_by_source)
    identity = sorted(agg_preview.media)[0]
    candidate = {**_one_candidate(), "url": "https://example.test/should-never-appear"}
    hints_path = tmp_path / "hints.jsonl.gz"
    _write_jsonl_gz(hints_path, [_hint_row_for(agg_preview, identity, decision="proposed_exact", candidates=[candidate])])

    importer.main([
        "--source-dir", str(library_sources),
        "--output", str(tmp_path / "out"),
        "--hints", str(hints_path),
        "--report", str(tmp_path / "report.json"),
    ])
    text = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert "http" not in text
