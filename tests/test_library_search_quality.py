"""Tests for scripts/eval_library_search.py (T2.6): search-quality golden
set and evaluation script.

Builds a real search index from the synthetic ``library_sources`` fixture
(via ``import_media_library.aggregate``/``write_bundle``, exactly like
``tests/test_import_media_library.py``'s end-to-end tests) and runs the
golden query set (``tests/fixtures/library/search_golden.json``, >= 50
queries covering every category in
docs/architecture.md §7.0.5) through
``eval_library_search.evaluate``, then checks the CI gate
(MRR@10 >= 0.85, P@1 >= 0.80) and the report's no-links/no-codes rule.

All titles/hosts in the golden set and the underlying fixture are synthetic
(see fixtures/library/make_workbooks.py's module docstring) -- nothing here
is a real share link or access code.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import library_store as ls  # noqa: E402
import import_media_library as importer  # noqa: E402
import eval_library_search as evalsearch  # noqa: E402

GOLDEN_PATH = ROOT / "tests" / "fixtures" / "library" / "search_golden.json"
MIN_QUERIES = 50
REQUIRED_CATEGORIES = {
    "中文全称", "中文部分", "别名", "原名", "英文前缀", "拼音全拼",
    "拼音首字母", "繁体", "一个错字", "英文一个字母错", "带年份", "带季",
    "带画质", "带网盘", "噪声词", "空查询+筛选",
}


def _read_all_sources(source_dir: Path):
    rows_by_source = {}
    sources_meta = []
    for filename, sheet, kind in importer.SOURCES:
        header_row, rows = importer.read_sheet(source_dir / filename, sheet)
        rows_by_source[kind] = rows
        sources_meta.append({"file": filename, "sheet": sheet, "kind": kind, "header_row": header_row})
    cc_file, cc_sheet = importer.CROSS_CHECK
    _cc_header, cc_rows = importer.read_sheet(source_dir / cc_file, cc_sheet)
    rows_by_source["cross_check"] = cc_rows
    return rows_by_source, sources_meta


@pytest.fixture
def bundle_store(library_sources, tmp_path) -> ls.LibraryStore:
    rows_by_source, _meta = _read_all_sources(library_sources)
    agg = importer.aggregate(rows_by_source)
    path = importer.write_bundle(agg, tmp_path / "out", source_hashes={"x": "y"})
    return ls.LibraryStore(path)


# ---------------------------------------------------------------------------
# golden set shape
# ---------------------------------------------------------------------------


def test_golden_set_has_at_least_50_queries():
    golden = evalsearch.load_golden(GOLDEN_PATH)
    assert len(golden) >= MIN_QUERIES


def test_golden_set_covers_every_required_category():
    golden = evalsearch.load_golden(GOLDEN_PATH)
    categories = {evalsearch._category(entry["note"]) for entry in golden}
    missing = REQUIRED_CATEGORIES - categories
    assert not missing, f"golden set missing categories: {missing}"


def test_golden_set_file_never_contains_a_link_or_ed2k_scheme():
    text = GOLDEN_PATH.read_text(encoding="utf-8")
    assert "http://" not in text
    assert "https://" not in text
    assert "ed2k://" not in text


# ---------------------------------------------------------------------------
# evaluate() against a real bundle built from the synthetic fixture
# ---------------------------------------------------------------------------


def test_evaluate_meets_ci_gate_thresholds(bundle_store):
    golden = evalsearch.load_golden(GOLDEN_PATH)
    evaluation = evalsearch.evaluate(bundle_store, golden, top=10)
    overall = evaluation["overall"]
    failing = [row for row in evaluation["queries"] if row["rank"] != 1]
    assert overall["mrr"] >= 0.85, f"MRR@10 {overall['mrr']:.4f} below gate; rank!=1 queries: {failing}"
    assert overall["p1"] >= 0.80, f"P@1 {overall['p1']:.4f} below gate; rank!=1 queries: {failing}"


def test_evaluate_breaks_down_by_category(bundle_store):
    golden = evalsearch.load_golden(GOLDEN_PATH)
    evaluation = evalsearch.evaluate(bundle_store, golden, top=10)
    categories = evaluation["categories"]
    assert set(categories) >= REQUIRED_CATEGORIES
    for name, metrics in categories.items():
        assert metrics["count"] > 0, name


def test_report_never_contains_a_link_or_ed2k_scheme(bundle_store):
    golden = evalsearch.load_golden(GOLDEN_PATH)
    evaluation = evalsearch.evaluate(bundle_store, golden, top=10)
    report = evalsearch.build_report(evaluation, min_mrr=0.85, min_p1=0.80)
    text = json.dumps(report, ensure_ascii=False)
    assert "http" not in text
    assert "ed2k://" not in text


def test_report_contains_only_query_expected_rank_and_metrics(bundle_store):
    golden = evalsearch.load_golden(GOLDEN_PATH)
    evaluation = evalsearch.evaluate(bundle_store, golden, top=10)
    report = evalsearch.build_report(evaluation, min_mrr=0.85, min_p1=0.80)
    for row in report["queries"]:
        assert set(row) == {"query", "expected", "category", "rank"}


# ---------------------------------------------------------------------------
# CLI exit codes: 0 pass / 1 below threshold / 2 usage error
# ---------------------------------------------------------------------------


def test_cli_exit_code_0_when_gate_passes(bundle_store, tmp_path):
    report_path = tmp_path / "report.json"
    code = evalsearch.main([
        "--index", str(bundle_store.db_path),
        "--queries", str(GOLDEN_PATH),
        "--report", str(report_path),
    ])
    assert code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["metrics"]["mrr_at_top"] >= 0.85
    assert report["metrics"]["p_at_1"] >= 0.80


def test_cli_exit_code_1_when_threshold_unreachable(bundle_store):
    code = evalsearch.main([
        "--index", str(bundle_store.db_path),
        "--queries", str(GOLDEN_PATH),
        "--min-mrr", "1.5",
        "--min-p1", "1.5",
    ])
    assert code == 1


def test_cli_exit_code_2_when_queries_file_missing(bundle_store, tmp_path):
    code = evalsearch.main([
        "--index", str(bundle_store.db_path),
        "--queries", str(tmp_path / "does-not-exist.json"),
    ])
    assert code == 2


def test_cli_exit_code_2_for_bad_arguments():
    with pytest.raises(SystemExit) as exc_info:
        evalsearch.main(["--index", "x"])  # missing required --queries
    assert exc_info.value.code == 2


def test_cli_as_subprocess_prints_no_link_and_exits_0(bundle_store, tmp_path):
    report_path = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable, str(ROOT / "scripts" / "eval_library_search.py"),
            "--index", str(bundle_store.db_path),
            "--queries", str(GOLDEN_PATH),
            "--report", str(report_path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "http" not in result.stdout
    assert "ed2k://" not in result.stdout
