"""Tests for scripts/import_media_library.py's w5-confirm-import addition:
``--confirmations`` (Codex's manual TMDB-identity confirmations, see
docs/metadata-enrichment.md §1-§6).

Uses the same synthetic ``library_sources`` fixture as
tests/test_import_media_library.py. Confirmation rows are built FROM the
just-aggregated ``agg.media`` dict (never hand-guessed titles/years) so a
row's (title_zh, year, media_type) is guaranteed to match one already in the
bundle, exactly like tests/test_import_media_library_hints.py does for
--hints.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import import_media_library as importer  # noqa: E402
from test_import_media_library import _read_all_sources  # noqa: E402
from test_import_media_library_hints import (  # noqa: E402
    _hint_row_for, _one_candidate, _write_jsonl, _write_jsonl_gz,
)


def _aggregate(library_sources):
    rows_by_source, _meta = _read_all_sources(library_sources)
    return importer.aggregate(rows_by_source)


def _confirmation_row_for(agg, identity: str, media_id: int, *, tmdb_id: int, imdb_id="tt0000123",
                           confidence="high_identity", original_decision="proposed_exact", evidence=None) -> dict:
    m = agg.media[identity]
    return {
        "media_id": media_id,
        "title_zh": m["title_zh"],
        "title_original": m["title_original"],
        "year": m["year"],
        "media_type": m["media_type"],
        "imdb_id": imdb_id,
        "tmdb_id": tmdb_id,
        "confidence": confidence,
        "original_decision": original_decision,
        "candidate_rank": 1,
        "evidence": evidence or {"year_exact": True, "media_type_exact": True, "wikidata_tmdb_property": "P4947"},
    }


# --- _media_lookup_from_aggregate -------------------------------------------


def test_media_lookup_from_aggregate_matches_write_bundle_ids(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)

    bundle_path = importer.write_bundle(agg, tmp_path / "out", source_hashes={"x": "y"})
    conn = sqlite3.connect(str(bundle_path))
    try:
        rows = conn.execute("SELECT id, media_identity, title_zh, year, media_type FROM media").fetchall()
    finally:
        conn.close()

    assert len(rows) == len(lookup)
    for media_id, identity, title_zh, year, media_type in rows:
        entry = lookup[media_id]
        assert entry["media_identity"] == identity
        assert entry["title_zh"] == title_zh
        assert entry["year"] == year
        assert entry["media_type"] == media_type


# --- load_confirmations ------------------------------------------------------


def test_load_confirmations_accepts_matching_row(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, [row])

    result = importer.load_confirmations(path, lookup)

    assert result.rows_total == 1
    assert result.written == 1
    assert sum(result.skipped.values()) == 0
    got_identity, candidate = result.accepted[0]
    assert got_identity == identity
    assert candidate == {
        "imdb_id": "tt0000123",
        "tmdb_id": 555,
        "tmdb_type": lookup[media_id]["media_type"],
        "confidence": "high_identity",
        "original_decision": "proposed_exact",
        "evidence": {"year_exact": True, "media_type_exact": True, "wikidata_tmdb_property": "P4947"},
    }


def test_load_confirmations_skip_missing_media_id(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    identity = lookup[sorted(lookup)[0]]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id=999_999, tmdb_id=555)
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, [row])

    result = importer.load_confirmations(path, lookup)

    assert result.written == 0
    assert result.skipped["missing"] == 1


def test_load_confirmations_skip_title_mismatch(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    row["title_zh"] = row["title_zh"] + "___different"
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, [row])

    result = importer.load_confirmations(path, lookup)

    assert result.written == 0
    assert result.skipped["title_mismatch"] == 1


def test_load_confirmations_skip_year_mismatch(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    row["year"] = (row["year"] or 0) + 1234
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, [row])

    result = importer.load_confirmations(path, lookup)

    assert result.written == 0
    assert result.skipped["year_mismatch"] == 1


def test_load_confirmations_skip_type_mismatch(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    row["media_type"] = "tv" if row["media_type"] == "movie" else "movie"
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, [row])

    result = importer.load_confirmations(path, lookup)

    assert result.written == 0
    assert result.skipped["type_mismatch"] == 1


def test_load_confirmations_skip_bad_row_malformed_json(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("not json at all\n", encoding="utf-8")

    result = importer.load_confirmations(path, {})

    assert result.rows_total == 1
    assert result.skipped["bad_row"] == 1
    assert result.written == 0


def test_load_confirmations_skip_bad_row_missing_required_field(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    del row["tmdb_id"]
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, [row])

    result = importer.load_confirmations(path, lookup)

    assert result.skipped["bad_row"] == 1
    assert result.written == 0


def test_load_confirmations_year_none_normalizes_to_zero(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    # find a media row whose year is already None so (year or 0) == 0 for both sides
    media_id = next(mid for mid, entry in lookup.items() if entry["year"] is None)
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    row["year"] = None
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, [row])

    result = importer.load_confirmations(path, lookup)

    assert result.written == 1
    assert sum(result.skipped.values()) == 0


def test_load_confirmations_shared_tmdb_pair_both_written(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    by_type: dict = {}
    for mid, entry in lookup.items():
        by_type.setdefault(entry["media_type"], []).append(mid)
    media_ids = next(ids for ids in by_type.values() if len(ids) >= 2)[:2]
    assert len(media_ids) >= 2
    rows = [
        _confirmation_row_for(agg, lookup[mid]["media_identity"], mid, tmdb_id=709631)
        for mid in media_ids
    ]
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, rows)

    result = importer.load_confirmations(path, lookup)

    assert result.written == 2
    assert result.shared_tmdb_pairs == 1


def test_load_confirmations_dedupes_by_media_id(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, [row, dict(row)])

    result = importer.load_confirmations(path, lookup)

    assert result.rows_total == 2
    assert result.written == 1
    assert result.skipped["duplicate"] == 1
    assert len(result.accepted) == 1


def test_load_confirmations_supports_gzip(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    path = tmp_path / "confirmations.jsonl.gz"
    _write_jsonl_gz(path, [row])

    result = importer.load_confirmations(path, lookup)

    assert result.written == 1
    assert result.accepted[0][0] == identity


def test_load_confirmations_never_leaks_unknown_row_keys(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    row["evidence"]["sources"] = ["wikidata"]
    row["evidence"]["unknown_evidence_field"] = "should be dropped"
    path = tmp_path / "confirmations.jsonl"
    _write_jsonl(path, [row])

    result = importer.load_confirmations(path, lookup)

    _identity, candidate = result.accepted[0]
    dumped = json.dumps(candidate, ensure_ascii=False)
    assert "unknown_evidence_field" not in dumped
    assert "candidate_rank" not in dumped
    assert set(candidate) == {"imdb_id", "tmdb_id", "tmdb_type", "confidence", "original_decision", "evidence"}


# --- write_confirmations ------------------------------------------------------


def test_write_confirmations_writes_confirmed_hint_row(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    confirmations_path = tmp_path / "confirmations.jsonl"
    _write_jsonl(confirmations_path, [row])
    result = importer.load_confirmations(confirmations_path, lookup)

    bundle_path = importer.write_bundle(agg, tmp_path / "out", source_hashes={"x": "y"}, confirmations=result)

    conn = sqlite3.connect(str(bundle_path))
    try:
        db_row = conn.execute(
            "SELECT source, decision, candidate_count, candidates_json FROM tmdb_hints WHERE media_identity=?",
            (identity,),
        ).fetchone()
    finally:
        conn.close()
    assert db_row is not None
    source, decision, candidate_count, candidates_json = db_row
    assert source == "codex_manual_confirmation"
    assert decision == "confirmed"
    assert candidate_count == 1
    candidates = json.loads(candidates_json)
    assert len(candidates) == 1
    assert candidates[0]["tmdb_id"] == 555
    assert candidates[0]["prior_hint_decision"] is None


def test_write_confirmations_replaces_existing_hint_and_records_prior_decision(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]

    hint_row = {
        "media_id": media_id, "title_zh": lookup[media_id]["title_zh"], "title_original": None,
        "year": lookup[media_id]["year"], "media_type": lookup[media_id]["media_type"],
        "decision": "proposed_exact", "candidate_count": 1,
        "candidates": [{"imdb_id": "tt0000999", "imdb_type": lookup[media_id]["media_type"]}],
    }
    hints_path = tmp_path / "hints.jsonl"
    _write_jsonl(hints_path, [hint_row])
    hints_result = importer.load_hints(hints_path, set(agg.media))

    confirm_row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    confirmations_path = tmp_path / "confirmations.jsonl"
    _write_jsonl(confirmations_path, [confirm_row])
    confirmations_result = importer.load_confirmations(confirmations_path, lookup)

    bundle_path = importer.write_bundle(
        agg, tmp_path / "out", source_hashes={"x": "y"},
        hints=hints_result, confirmations=confirmations_result,
    )

    conn = sqlite3.connect(str(bundle_path))
    try:
        db_row = conn.execute(
            "SELECT source, decision, candidates_json FROM tmdb_hints WHERE media_identity=?", (identity,),
        ).fetchone()
    finally:
        conn.close()
    source, decision, candidates_json = db_row
    assert source == "codex_manual_confirmation"
    assert decision == "confirmed"
    candidates = json.loads(candidates_json)
    assert candidates[0]["prior_hint_decision"] == "proposed_exact"


def test_write_confirmations_duplicate_media_id_keeps_original_prior_decision(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]

    hint_row = {
        "media_id": media_id, "title_zh": lookup[media_id]["title_zh"], "title_original": None,
        "year": lookup[media_id]["year"], "media_type": lookup[media_id]["media_type"],
        "decision": "proposed_exact", "candidate_count": 1,
        "candidates": [{"imdb_id": "tt0000999", "imdb_type": lookup[media_id]["media_type"]}],
    }
    hints_path = tmp_path / "hints.jsonl"
    _write_jsonl(hints_path, [hint_row])
    hints_result = importer.load_hints(hints_path, set(agg.media))

    confirm_row = _confirmation_row_for(agg, identity, media_id, tmdb_id=555)
    confirmations_path = tmp_path / "confirmations.jsonl"
    _write_jsonl(confirmations_path, [confirm_row, dict(confirm_row)])
    confirmations_result = importer.load_confirmations(confirmations_path, lookup)
    assert confirmations_result.written == 1
    assert confirmations_result.skipped["duplicate"] == 1

    bundle_path = importer.write_bundle(
        agg, tmp_path / "out", source_hashes={"x": "y"},
        hints=hints_result, confirmations=confirmations_result,
    )

    conn = sqlite3.connect(str(bundle_path))
    try:
        db_row = conn.execute(
            "SELECT candidates_json FROM tmdb_hints WHERE media_identity=?", (identity,),
        ).fetchone()
    finally:
        conn.close()
    candidates = json.loads(db_row[0])
    assert len(candidates) == 1
    assert candidates[0]["prior_hint_decision"] == "proposed_exact"


def test_write_bundle_without_confirmations_leaves_no_confirmed_rows(library_sources, tmp_path):
    agg = _aggregate(library_sources)
    bundle_path = importer.write_bundle(agg, tmp_path / "out", source_hashes={"x": "y"})
    conn = sqlite3.connect(str(bundle_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM tmdb_hints WHERE decision='confirmed'").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# --- run()/main() CLI wiring + report shape -----------------------------------


def test_dry_run_with_confirmations_reports_counts_without_writing_bundle(library_sources, tmp_path):
    agg_preview = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg_preview)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg_preview, identity, media_id, tmdb_id=555)
    confirmations_path = tmp_path / "confirmations.jsonl"
    _write_jsonl(confirmations_path, [row])

    exit_code = importer.main([
        "--source-dir", str(library_sources),
        "--output", str(tmp_path / "out"),
        "--confirmations", str(confirmations_path),
        "--report", str(tmp_path / "report.json"),
    ])
    assert exit_code == 0
    assert not (tmp_path / "out" / "library-bundle.sqlite").exists()

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["confirmations"]["rows"] == 1
    assert report["confirmations"]["written"] == 1
    assert report["confirmations"]["skipped"] == {
        "missing": 0, "title_mismatch": 0, "year_mismatch": 0, "type_mismatch": 0, "bad_row": 0, "duplicate": 0,
    }
    assert report["confirmations"]["shared_tmdb_pairs"] == 0


def test_report_omits_confirmations_key_when_flag_not_given(library_sources, tmp_path):
    exit_code = importer.main([
        "--source-dir", str(library_sources),
        "--output", str(tmp_path / "out"),
        "--report", str(tmp_path / "report.json"),
    ])
    assert exit_code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert "confirmations" not in report


def test_write_with_confirmations_end_to_end_writes_two_rows(library_sources, tmp_path):
    agg_preview = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg_preview)
    media_ids = sorted(lookup)[:2]
    rows = [
        _confirmation_row_for(agg_preview, lookup[mid]["media_identity"], mid, tmdb_id=1000 + i)
        for i, mid in enumerate(media_ids)
    ]
    confirmations_path = tmp_path / "confirmations.jsonl"
    _write_jsonl(confirmations_path, rows)

    exit_code = importer.main([
        "--source-dir", str(library_sources),
        "--output", str(tmp_path / "out"),
        "--write",
        "--confirmations", str(confirmations_path),
        "--report", str(tmp_path / "report.json"),
    ])
    assert exit_code == 0

    bundle_path = tmp_path / "out" / "library-bundle.sqlite"
    conn = sqlite3.connect(str(bundle_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM tmdb_hints WHERE decision='confirmed'").fetchone()[0]
    finally:
        conn.close()
    assert count == 2

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["confirmations"]["written"] == 2


def test_write_with_confirmations_extends_imdb_ratings_relevant_ids(library_sources, tmp_path):
    """A confirmation's own imdb_id must count as "relevant" for
    --imdb-ratings even when no --hints candidate ever mentioned it (the
    important review finding: 659 confirmed rows on the real data carried
    an imdb_id with no imdb_ratings row because confirmations were loaded
    after, and never unioned into, the hints-only relevant_ids set)."""
    agg_preview = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg_preview)
    media_ids = sorted(lookup)[:2]
    hint_identity = lookup[media_ids[0]]["media_identity"]
    confirm_media_id = media_ids[1]
    confirm_identity = lookup[confirm_media_id]["media_identity"]

    # --hints contributes zero imdb_ids (a no-candidate decision) -- the
    # ratings-relevant id below comes only from the confirmation.
    hints_path = tmp_path / "hints.jsonl"
    _write_jsonl(hints_path, [
        _hint_row_for(agg_preview, hint_identity, decision="no_imdb_candidate", candidates=[]),
    ])

    confirm_row = _confirmation_row_for(agg_preview, confirm_identity, confirm_media_id, tmdb_id=555, imdb_id="tt0000456")
    confirmations_path = tmp_path / "confirmations.jsonl"
    _write_jsonl(confirmations_path, [confirm_row])

    ratings_path = tmp_path / "imdb-ratings.jsonl.gz"
    _write_jsonl_gz(ratings_path, [
        {"imdb_id": "tt0000456", "rating": 7.1, "votes": 2000},
        {"imdb_id": "tt9999999", "rating": 5.0, "votes": 10},
    ])

    exit_code = importer.main([
        "--source-dir", str(library_sources),
        "--output", str(tmp_path / "out"),
        "--write",
        "--hints", str(hints_path),
        "--confirmations", str(confirmations_path),
        "--imdb-ratings", str(ratings_path),
        "--report", str(tmp_path / "report.json"),
    ])
    assert exit_code == 0

    bundle_path = tmp_path / "out" / "library-bundle.sqlite"
    conn = sqlite3.connect(str(bundle_path))
    try:
        ids = {row[0] for row in conn.execute("SELECT imdb_id FROM imdb_ratings").fetchall()}
    finally:
        conn.close()
    assert ids == {"tt0000456"}

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["imdb_ratings"] == {"file_found": True, "rows_loaded": 1}


def test_confirmations_report_never_contains_a_url(library_sources, tmp_path):
    agg_preview = _aggregate(library_sources)
    lookup = importer._media_lookup_from_aggregate(agg_preview)
    media_id = sorted(lookup)[0]
    identity = lookup[media_id]["media_identity"]
    row = _confirmation_row_for(agg_preview, identity, media_id, tmdb_id=555)
    row["evidence"]["url"] = "https://example.test/should-never-appear"
    confirmations_path = tmp_path / "confirmations.jsonl"
    _write_jsonl(confirmations_path, [row])

    importer.main([
        "--source-dir", str(library_sources),
        "--output", str(tmp_path / "out"),
        "--confirmations", str(confirmations_path),
        "--report", str(tmp_path / "report.json"),
    ])
    text = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert "http" not in text
