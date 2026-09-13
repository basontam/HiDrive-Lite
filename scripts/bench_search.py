#!/usr/bin/env python3
"""Local-search latency and candidate-shape benchmark
(docs/claude-search-precision-efficiency-20260910.md §4.7).

    .venv/bin/python scripts/bench_search.py --report before.json

Builds a deterministic SYNTHETIC corpus (no production data, no real links
or access codes -- every title, host and share id below is invented), then
runs a fixed query set through ``library_search.search`` and records, per
query:

    p50 / p95 wall time, the recall candidate count BEFORE the name gate,
    the count that passed it, the final ``total`` after business filters,
    and the first ten result titles.

Run it once before a search change and once after, then diff the two JSON
reports -- that comparison is the evidence §6.3.2 asks for. The corpus is
regenerated from the same seed each time, so the two runs see identical
data; only the algorithm differs.
"""

from __future__ import annotations

import argparse
import inspect
import json
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_search as lse  # noqa: E402
import library_store as ls  # noqa: E402

DEFAULT_QUERIES = ROOT / "tests" / "fixtures" / "library" / "search_bench_queries.json"
DEFAULT_SIZE = 4000
DEFAULT_REPS = 15
SEED = 20260910

# One pseudo-syllable per character used by the anchor titles, so a pinyin
# query in the fixed set resolves the way it would in production; every
# other character falls back to a deterministic hash-derived syllable.
_PINYIN = {
    "韩": "han", "国": "guo", "制": "zhi", "造": "zao", "美": "mei", "天": "tian",
    "堂": "tang", "流": "liu", "行": "xing", "音": "yin", "乐": "yue", "偶": "ou",
    "像": "xiang", "异": "yi", "形": "xing", "沙": "sha", "丘": "qiu", "空": "kong",
    "间": "jian", "旅": "lv", "者": "zhe", "纪": "ji", "录": "lu", "片": "pian",
}
_SYLLABLES = ("ba", "chi", "dan", "fen", "gao", "hui", "jin", "kai", "lan", "mo",
              "nan", "pei", "qing", "ran", "shu", "tao", "wen", "xun", "yang", "zhen")

# Title-building pool. 国 / 制 / 造 / 韩 are deliberately frequent so the
# corpus reproduces the "one common character recalls hundreds of media"
# shape the work order's §2 baseline observed on the real library.
_COMMON = "国制造韩"
_POOL = "风云山海城市夜光影传奇少年时代战记录长安飞鸟林海梦境火焰月亮"

_ANCHORS = [
    # (title_zh, title_original, aliases, overview, year, media_type)
    ("韩国制造", None, [], "一部虚构剧集。", 2021, "tv"),
    ("美国制造", None, [], "一部虚构电影。", 2017, "movie"),
    ("天堂制造", None, [], "一部虚构电影。", 2019, "movie"),
    ("韩国流行音乐偶像", None, [], "一部虚构纪录片。", 2020, "tv"),
    ("异形2", None, [], "一部虚构续集。", 1986, "movie"),
    ("沙丘2", None, [], "一部虚构续集。", 2024, "movie"),
    ("无关纪录片", None, [], "本片讲述韩国制造业的历史，与片名无关。", 2015, "movie"),
    ("另一个片名", None, ["空间旅行者"], "只在别名上命中的虚构电影。", 2018, "movie"),
    ("2012", None, [], "以数字为名的虚构电影。", 2009, "movie"),
    ("The Matrix", "The Matrix", [], "A fictional Latin-titled film.", 1999, "movie"),
    ("黑客帝国", "The Matrix Reloaded", [], "一部虚构电影。", 2003, "movie"),
    ("国", None, [], "单字标题的虚构短片。", 2011, "movie"),
]


def _pinyin_of(title: str) -> tuple[str, str]:
    syllables = []
    for ch in title:
        if "一" <= ch <= "鿿":
            syllables.append(_PINYIN.get(ch) or _SYLLABLES[ord(ch) % len(_SYLLABLES)])
    if not syllables:
        return "", ""
    return "".join(syllables), "".join(s[0] for s in syllables)


def build_corpus(db_path: Path, size: int) -> ls.LibraryStore:
    """Write a fresh synthetic bundle to ``db_path`` and index it."""
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = ls.LibraryStore(db_path)
    store.create_schema()

    rng = random.Random(SEED)
    rows: list[tuple[str, str | None, list[str], str, int, str]] = list(_ANCHORS)
    for i in range(size):
        length = rng.choice((2, 2, 3, 4, 4, 5))
        chars = []
        for _ in range(length):
            chars.append(rng.choice(_COMMON) if rng.random() < 0.12 else rng.choice(_POOL))
        title = "".join(chars)
        # Overviews carry digits and common words far more often than
        # titles do -- the exact asymmetry that lets an overview-only or
        # digit-only hit dominate recall when nothing gates on the name.
        overview = "第%d季的虚构简介，%d 年上映，讲述%s的故事。" % (
            rng.randint(1, 5), rng.randint(1990, 2025), rng.choice(("韩国制造", "城市生活", "少年时代", "战地记录")),
        )
        rows.append((title, None, [], overview, rng.randint(1980, 2025), rng.choice(("movie", "tv"))))

    for index, (title, original, aliases, overview, year, media_type) in enumerate(rows):
        media_id = store.upsert_media(
            ls.MediaRecord(
                media_identity=f"bench-fake-{index}",
                media_type=media_type,
                title_zh=title,
                search_key=title,
                title_original=original,
                title_alt_json=json.dumps(aliases, ensure_ascii=False),
                year=year,
                overview=overview,
                match_status="exact" if index % 3 == 0 else "candidate",
            )
        )
        group_id = store.upsert_group(
            ls.GroupRecord(
                media_id=media_id,
                edition_fingerprint=f"bench-fp-{index}",
                display_title="g",
                quality="4K" if index % 4 == 0 else "1080p",
            )
        )
        for n in range(1 + index % 3):
            store.upsert_link(
                ls.LinkRecord(
                    public_id=f"bench-fake-{index}-{n}",
                    group_id=group_id,
                    provider=("115", "quark", "alipan")[n % 3],
                    canonical_url_hash=f"bench-fake-hash-{index}-{n}",
                    url_label="115 分享 · fake…1",
                )
            )

    lse.build_index(store, pinyin=_pinyin_of, charmap={"國": "国", "製": "制", "韓": "韩"})
    return store


def _candidate_shape(store: ls.LibraryStore, query: str) -> dict:
    """Recall candidate counts either side of the name gate, plus how the
    survivors spread over the name-match tiers.

    The tier histogram is the "noise ratio" §3.4 asks the report to carry:
    tier 3 is the low-coverage tail the first round keeps but heavily
    demotes. The pre-gate build of ``recall()`` has no ``stats`` parameter
    and no tiers, so this reports equal counts and an empty histogram there
    -- one script stays usable for both sides of the before/after comparison.
    """
    conn = store.connect(readonly=True)
    try:
        charmap = {row["src"]: row["dst"] for row in conn.execute("SELECT src, dst FROM search_charmap")}
        plan = lse.parse_query(query)
        if not plan.text:
            return {"recall_candidates": 0, "gated_candidates": 0, "tiers": {}}
        if "stats" not in inspect.signature(lse.recall).parameters:
            hits = lse.recall(conn, plan, charmap)
            return {"recall_candidates": len(hits), "gated_candidates": len(hits), "tiers": {}}
        stats: dict = {}
        hits = lse.recall(conn, plan, charmap, stats=stats)
        tiers: dict[str, int] = {}
        for hit in lse.rerank(conn, hits, plan, charmap):
            key = "tier%d" % hit.name_match_tier
            tiers[key] = tiers.get(key, 0) + 1
        return {
            "recall_candidates": stats.get("pre_gate", len(hits)),
            "gated_candidates": len(hits),
            "tiers": dict(sorted(tiers.items())),
        }
    finally:
        conn.close()


def measure(store: ls.LibraryStore, queries: list[dict], *, reps: int) -> list[dict]:
    charmap_conn = store.connect(readonly=True)
    try:
        charmap = {row["src"]: row["dst"] for row in charmap_conn.execute("SELECT src, dst FROM search_charmap")}
    finally:
        charmap_conn.close()

    results = []
    for entry in queries:
        query = entry["query"]
        timings: list[float] = []
        page = None
        for _ in range(reps):
            started = time.perf_counter()
            page = lse.search(store, query, lse.Filters(), charmap=charmap)
            timings.append((time.perf_counter() - started) * 1000.0)
        timings.sort()
        results.append({
            "query": query,
            "note": entry.get("note", ""),
            "p50_ms": round(statistics.median(timings), 3),
            "p95_ms": round(timings[min(len(timings) - 1, int(len(timings) * 0.95))], 3),
            "total": page.total if page else 0,
            "top10": [item["title"] for item in (page.items if page else [])][:10],
            **_candidate_shape(store, query),
        })
    return results


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark local search latency and candidate shape.")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE, help="synthetic filler media (default 4000)")
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS)
    parser.add_argument("--db", default=None, help="where to write the synthetic corpus")
    parser.add_argument("--report", default=None, help="write the JSON report here")
    args = parser.parse_args(argv)

    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    db_path = Path(args.db) if args.db else ROOT / "build" / "bench-search" / "corpus.sqlite"
    store = build_corpus(db_path, args.size)
    rows = measure(store, queries, reps=args.reps)

    report = {
        "corpus": {"filler": args.size, "anchors": len(_ANCHORS), "seed": SEED},
        "reps": args.reps,
        "queries": rows,
        "summary": {
            "p50_ms_median": round(statistics.median([r["p50_ms"] for r in rows]), 3),
            "p95_ms_max": round(max(r["p95_ms"] for r in rows), 3),
            "total_sum": sum(r["total"] for r in rows),
        },
    }
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in rows:
        print("%-18s p50=%7.3fms p95=%7.3fms recall=%4d gated=%4d total=%5d %-28s | %s" % (
            row["query"], row["p50_ms"], row["p95_ms"], row["recall_candidates"],
            row["gated_candidates"], row["total"],
            " ".join("%s=%d" % (k, v) for k, v in row["tiers"].items()),
            " / ".join(row["top10"][:3]),
        ))
    print("summary:", json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
