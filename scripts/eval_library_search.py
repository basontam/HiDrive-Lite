#!/usr/bin/env python3
"""Search-quality evaluation harness for the personal media-resource library
(T2.6; docs/architecture.md §7.0.5).

    .venv/bin/python scripts/eval_library_search.py
        --index .local-index/library-bundle.sqlite
        --queries tests/fixtures/library/search_golden.json
        [--report build/test-artifacts/library-search-eval-report.json]
        [--top 10] [--min-mrr 0.85] [--min-p1 0.80]

Runs every golden query in ``--queries`` (a JSON array of
``{"query", "expected", "note"}`` objects -- see
``tests/fixtures/library/search_golden.json``) through
``library_search.search`` with default (unfiltered) ``Filters`` and reports
MRR@top, Recall@top and P@1 overall and per category, where a query's
category is the text of its ``note`` before the first ``":"``.

The report (stdout summary and ``--report`` JSON, when given) contains only
query text, expected title, hit rank and the metrics themselves -- never a
link, host or access code, since :func:`evaluate` only ever reads the
``title`` field of ``library_search.search``'s result items.

Exit codes: 0 both thresholds met, 1 a threshold missed, 2 usage/input error
(bad CLI arguments, missing/invalid ``--queries`` file, index not built).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from library_search import Filters, search  # noqa: E402
from library_store import LibraryNotInstalled, LibraryStore  # noqa: E402

DEFAULT_TOP = 10
DEFAULT_MIN_MRR = 0.85
DEFAULT_MIN_P1 = 0.80


class GoldenSetError(ValueError):
    """Raised for a malformed ``--queries`` file (never for a search failure)."""


def load_golden(path: Path) -> list[dict]:
    """Parse and validate a golden-query JSON file into a list of
    ``{"query", "expected", "note"}`` dicts (``note`` optional, defaults to
    ``""``)."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise GoldenSetError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GoldenSetError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise GoldenSetError(f"{path} must be a non-empty JSON array")
    golden: list[dict] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or "query" not in entry or "expected" not in entry:
            raise GoldenSetError(f"{path}[{i}] must have 'query' and 'expected' keys")
        golden.append({
            "query": entry["query"],
            "expected": entry["expected"],
            "note": entry.get("note", ""),
        })
    return golden


def _category(note: str) -> str:
    note = (note or "").strip()
    if ":" in note:
        return note.split(":", 1)[0].strip()
    return note or "uncategorized"


def _rank_of(store: LibraryStore, query: str, expected: str, *, top: int) -> int | None:
    """Return the 1-based rank of ``expected`` (matched by exact ``title``)
    in the first ``top`` results ``library_search.search`` returns for
    ``query``, or ``None`` if it is not among them."""
    page = search(store, query, Filters(), page_size=top)
    for rank, item in enumerate(page.items[:top], start=1):
        if item["title"] == expected:
            return rank
    return None


def _metrics(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"count": 0, "mrr": 0.0, "recall": 0.0, "p1": 0.0}
    mrr_sum = sum((1.0 / row["rank"]) if row["rank"] else 0.0 for row in rows)
    recall_hits = sum(1 for row in rows if row["rank"] is not None)
    p1_hits = sum(1 for row in rows if row["rank"] == 1)
    return {"count": n, "mrr": mrr_sum / n, "recall": recall_hits / n, "p1": p1_hits / n}


def evaluate(store: LibraryStore, golden: list[dict], *, top: int = DEFAULT_TOP) -> dict:
    """Run every golden entry through :func:`library_search.search` and
    return per-query ranks plus overall/per-category MRR@top, Recall@top and
    P@1.  Reads only ``item["title"]`` from each search result -- never a
    link, host or access code."""
    results = []
    for entry in golden:
        query = entry["query"]
        expected = entry["expected"]
        category = _category(entry.get("note", ""))
        rank = _rank_of(store, query, expected, top=top)
        results.append({"query": query, "expected": expected, "category": category, "rank": rank})

    overall = _metrics(results)
    categories = {
        category: _metrics([row for row in results if row["category"] == category])
        for category in sorted({row["category"] for row in results})
    }
    return {"top": top, "overall": overall, "categories": categories, "queries": results}


def build_report(evaluation: dict, *, min_mrr: float, min_p1: float) -> dict:
    """Shape :func:`evaluate`'s output into the report dict written to
    ``--report`` and summarised on stdout: query text, expected title, hit
    rank/category and the metrics -- nothing else."""
    overall = evaluation["overall"]
    passed = overall["mrr"] >= min_mrr and overall["p1"] >= min_p1

    def _fmt(m: dict) -> dict:
        return {
            "count": m["count"],
            "mrr_at_top": m["mrr"],
            "recall_at_top": m["recall"],
            "p_at_1": m["p1"],
        }

    return {
        "top": evaluation["top"],
        "thresholds": {"min_mrr": min_mrr, "min_p1": min_p1},
        "passed": passed,
        "metrics": _fmt(overall),
        "categories": {name: _fmt(m) for name, m in evaluation["categories"].items()},
        "queries": [
            {"query": row["query"], "expected": row["expected"], "category": row["category"], "rank": row["rank"]}
            for row in evaluation["queries"]
        ],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_library_search.py",
        description="Search-quality evaluation for the personal media-resource library.",
    )
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--min-mrr", dest="min_mrr", type=float, default=DEFAULT_MIN_MRR)
    parser.add_argument("--min-p1", dest="min_p1", type=float, default=DEFAULT_MIN_P1)
    return parser


def run(args: argparse.Namespace) -> tuple[dict | None, int]:
    """Run one full evaluation pass. Returns ``(report_dict_or_None, exit_code)``."""
    try:
        golden = load_golden(args.queries)
    except GoldenSetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 2

    store = LibraryStore(Path(args.index))
    try:
        evaluation = evaluate(store, golden, top=args.top)
    except LibraryNotInstalled as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 2

    report = build_report(evaluation, min_mrr=args.min_mrr, min_p1=args.min_p1)

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return report, (0 if report["passed"] else 1)


def main(argv: list | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    report, code = run(args)
    if report is not None:
        print(json.dumps({"passed": report["passed"], "metrics": report["metrics"]}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
