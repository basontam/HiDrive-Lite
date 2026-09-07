"""Search engine for the HiDrive-Lite personal media-resource library.

Implements the design in ``docs/architecture.md``
§7.0 (7.0.1-7.0.5): a CJK-bigram + Okapi BM25 offline index built into the
SQLite bundle by :func:`build_index`, queried at runtime by :func:`search`
(query parsing -> SQL recall -> Python rerank -> filter/paginate), plus
:func:`suggest` (prefix autocomplete) and :func:`correct_terms` (Latin
spelling correction).

Runtime dependency policy: this module imports only the standard library and
``library_normalize`` (``normalize_text`` reused inside :func:`fold`;
``format_ratings``/``primary_rating`` reused by :func:`_fetch_items`, T17
§14.5, so the ratings-passthrough shape is defined in exactly one place).
``pypinyin`` and ``zhconv`` are never imported here -- :func:`build_index`
accepts their *results* as plain callables/mappings supplied by the caller
(the offline importer), never as an import of this module.

Design decisions taken where §7.0 left the exact representation open (each
is exercised by a test in ``tests/test_library_search.py``):

* ``fold()`` is implemented as ``library_normalize.normalize_text()`` (NFKC,
  casefold, zero-width strip, separator-to-space unification, whitespace
  collapse -- covering 7.0.1's "NFKC / casefold / full-half-width unify /
  zero-width strip / punctuation-to-space / whitespace collapse" steps in
  one call) followed by a per-character ``charmap`` lookup for the
  traditional-to-simplified fold. The exact sub-step ordering in the plan's
  prose is not observable in practice (these operations do not interact),
  so reusing the already-tested ``normalize_text`` is the simplest
  deterministic reading.
* ``search_doc.title_key`` = ``fold(title_zh, charmap)`` verbatim, because
  :func:`rerank`'s formula compares it directly against ``fold(query
  text)`` for equality/prefix/substring -- any other representation would
  make those comparisons meaningless.
* ``search_doc.alias_keys_json`` holds one folded key per alias plus a
  folded key for ``title_original`` (if present), plus -- per 7.0.2 point 4
  ("别名同样生成并放入 alias_keys_json") -- the alias's own pinyin full/initials
  strings when a ``pinyin`` callable is supplied and the alias contains a
  CJK character. There is no separate column for this, so alias_keys_json
  is the only place those pinyin strings can live; :func:`suggest` prefix-
  matches against the whole JSON blob (see below).
* CJK ``cjk_run`` tokens are only emitted for runs of length 3-6 (not 1-2):
  a length-1 run is identical to its own single ``cjk_char`` token and a
  length-2 run is identical to its only ``cjk_bigram`` token, so emitting
  ``cjk_run`` there would index the same string twice under a second kind
  label for no benefit. The spec's "<=6" upper bound is preserved.
* Per-kind weight multipliers (``KIND_WEIGHTS`` below) are this module's
  own addition -- the spec's per-kind weight ratios (bigram 1.0 / char 0.25
  / run 1.5) are given in prose only, not in the constants block, so they
  are wired in as a multiplier alongside ``FIELD_WEIGHTS`` in the BM25
  weight formula.
* Latin stopword terms (``STOPWORDS_LATIN``) bypass the BM25 formula
  entirely and get a flat ``weight = 0.2 * FIELD_WEIGHTS[field]`` -- the
  spec's "reads 0.2 not deleted" per-term constant, kept simple and
  deterministic rather than folded into idf/tf.
* BM25 is computed BM25F-style: ``idf``/``df`` are per *term* (shared
  across fields, matching ``search_vocab``'s single row per term) and
  ``doc_len``/``avgdl`` are per *document* (summed across all 4 fields,
  matching ``search_doc.doc_len``'s single column) -- only the ``tf``
  component and the final ``field_weight``/``kind_weight`` multipliers vary
  per field/kind. Rows for the same term/media_id/field are then summed by
  the recall SQL's ``SUM(weight)``.
* ``correct_terms`` uses a small direct Levenshtein DP (not
  ``difflib.SequenceMatcher.ratio``) for the actual "edit distance <=1/2"
  test, because ``ratio()`` is a similarity score, not an edit distance,
  and could give wrong pass/fail decisions; the "first letter + length +-2"
  candidate prefilter from the spec is still applied via SQL before the DP
  runs, keeping the cost bounded.
* ``parse_query``'s year extraction requires whitespace/string-edge
  boundaries AND is suppressed when removing it would leave the residual
  text empty (e.g. a bare query ``"2012"`` -- the movie titled "2012" --
  keeps ``text="2012"``, ``year=None``) per the spec's own worked example.
* ``search()`` owns the recall-empty -> ``correct_terms`` -> retry loop
  (rather than :func:`recall` itself), because :func:`recall`'s signature
  (fixed by the brief) returns only ``list[Hit]`` with no room to also
  report which corrections were applied; :func:`search` rebuilds a new
  ``QueryPlan`` (via ``dataclasses.replace``) carrying the corrections and
  retries :func:`recall` with the corrected text.
* :func:`suggest`'s prefix match against ``alias_keys_json`` uses
  ``LIKE '%"<prefix>%'`` (matching right after a JSON-string-opening quote)
  rather than a JSON1 function, since JSON1 is not guaranteed compiled into
  every stdlib SQLite build; this is a pragmatic approximation, not exact
  JSON-array semantics, documented here for anyone extending it.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Mapping

from library_normalize import format_ratings, normalize_text, primary_rating
from library_store import LibraryStore, live_link_sql

# ---------------------------------------------------------------------------
# 7.0.1 tokenizer constants
# ---------------------------------------------------------------------------

K1 = 1.2
B = 0.75

FIELD_WEIGHTS = {"title": 3.0, "alias": 2.0, "original": 2.0, "overview": 0.3}

# Read as weight 0.2 (not deleted) -- see module docstring.
STOPWORDS_LATIN = {"the", "a", "an", "of"}

# Per-kind multiplier applied alongside FIELD_WEIGHTS -- this module's own
# addition, see module docstring.
KIND_WEIGHTS = {
    "cjk_bigram": 1.0,
    "cjk_char": 0.25,
    "cjk_run": 1.5,
    "latin": 1.0,
    "number": 1.0,
}

_CJK_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_DI_BU_RE = re.compile(r"第\s*([0-9一二三四五六七八九十]+)\s*部")

_ROMAN_MAP = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
}


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _ascii_fold(s: str) -> str:
    """Strip combining diacritics after NFKD decomposition (``e.g. -> e``)."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _clean_pinyin_key(s: str) -> str:
    """Keep only lowercase ASCII letters/digits from a raw ``pinyin()`` result.

    ``pinyin()`` callables (pypinyin-backed) pass source punctuation/spaces
    (e.g. a title's leading ``#`` or ``(``) straight through, which would
    otherwise sit at position 0 of ``pinyin_full``/``pinyin_initials`` and
    break prefix matching from the first real letter.
    """
    return "".join(ch for ch in s.lower() if ch.isascii() and ch.isalnum())


def fold(text: str, charmap: Mapping[str, str]) -> str:
    """NFKC -> casefold -> unify separators/whitespace (``normalize_text``) -> 繁->简 (charmap)."""
    folded = normalize_text(text or "")
    if charmap:
        folded = "".join(charmap.get(ch, ch) for ch in folded)
    return folded


def tokenize(text: str, charmap: Mapping[str, str]) -> list[tuple[str, str]]:
    """Return ``(term, kind)`` pairs, ``kind in {cjk_bigram, cjk_char, cjk_run, latin, number}``.

    See module docstring for the "第N部/roman numeral/digit -> number" and
    "cjk_run only for length 3-6" design decisions.
    """
    folded = fold(text, charmap)

    numbers: list[str] = []

    def _consume_di_bu(match: "re.Match[str]") -> str:
        token = match.group(1)
        if token.isdigit():
            numbers.append(str(int(token)))
        elif len(token) == 1 and token in _CJK_NUM:
            numbers.append(str(_CJK_NUM[token]))
        else:
            numbers.append(token)
        return " "

    folded = _DI_BU_RE.sub(_consume_di_bu, folded)

    tokens: list[tuple[str, str]] = [(n, "number") for n in numbers]

    run_chars: list[str] = []
    run_kind: str | None = None  # "cjk" | "word"

    def _flush() -> None:
        nonlocal run_chars, run_kind
        if not run_chars:
            return
        run = "".join(run_chars)
        if run_kind == "cjk":
            n = len(run)
            for i in range(n - 1):
                tokens.append((run[i:i + 2], "cjk_bigram"))
            for ch in run:
                tokens.append((ch, "cjk_char"))
            if 3 <= n <= 6:
                tokens.append((run, "cjk_run"))
        else:
            if run.isdigit():
                tokens.append((run, "number"))
            else:
                ascii_run = _ascii_fold(run)
                roman = _ROMAN_MAP.get(ascii_run)
                if roman is not None:
                    tokens.append((str(roman), "number"))
                else:
                    tokens.append((ascii_run, "latin"))
        run_chars = []
        run_kind = None

    for ch in folded:
        if _is_cjk(ch):
            if run_kind != "cjk":
                _flush()
                run_kind = "cjk"
            run_chars.append(ch)
        elif ch.isalnum():
            if run_kind != "word":
                _flush()
                run_kind = "word"
            run_chars.append(ch)
        else:
            _flush()
    _flush()
    return tokens


def build_charmap(texts: Iterable[str], convert: Callable[[str], str]) -> dict[str, str]:
    """Map each distinct CJK char in ``texts`` to ``convert(ch)`` when they differ.

    ``convert`` is supplied by the caller (e.g. ``zhconv.convert``); this
    module never imports a conversion library itself.
    """
    chars: set[str] = set()
    for text in texts:
        for ch in text or "":
            if _is_cjk(ch):
                chars.add(ch)
    charmap: dict[str, str] = {}
    for ch in sorted(chars):
        dst = convert(ch)
        if dst != ch:
            charmap[ch] = dst
    return charmap


# ---------------------------------------------------------------------------
# 7.0.2 offline index build
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexStats:
    media_count: int
    term_count: int
    vocab_count: int


def _field_texts(row) -> dict[str, str]:
    aliases = json.loads(row["title_alt_json"] or "[]")
    return {
        "title": row["title_zh"] or "",
        "alias": " ".join(aliases),
        "original": row["title_original"] or "",
        "overview": row["overview"] or "",
    }


def build_index(
    store: LibraryStore,
    *,
    pinyin: Callable[[str], tuple[str, str]] | None = None,
    charmap: Mapping[str, str] | None = None,
) -> IndexStats:
    """Clear and rebuild ``search_doc``/``search_term``/``search_vocab``/``search_charmap``.

    Deterministic: rows are computed from a full in-memory pass over
    ``media`` (sorted by ``media_id``) and written in sorted order, so two
    builds over identical input produce identical rows.
    """
    charmap = charmap or {}

    conn = store.connect()
    try:
        media_rows = conn.execute(
            """
            SELECT id, title_zh, title_original, title_alt_json, overview,
                   year, match_status, link_count, has_115
            FROM media ORDER BY id
            """
        ).fetchall()

        corpus_chars: set[str] = set()
        # media_id -> field -> Counter[(term, kind)]
        field_tokens: dict[int, dict[str, dict[tuple[str, str], int]]] = {}
        doc_len: dict[int, int] = {}
        doc_meta: dict[int, dict] = {}

        for row in media_rows:
            media_id = row["id"]
            texts = _field_texts(row)
            for text in texts.values():
                for ch in text:
                    if _is_cjk(ch):
                        corpus_chars.add(ch)

            per_field: dict[str, dict[tuple[str, str], int]] = {}
            total_tokens = 0
            for field_name, text in texts.items():
                counts: dict[tuple[str, str], int] = {}
                for term, kind in tokenize(text, charmap):
                    key = (term, kind)
                    counts[key] = counts.get(key, 0) + 1
                    total_tokens += 1
                per_field[field_name] = counts
            field_tokens[media_id] = per_field
            doc_len[media_id] = total_tokens
            doc_meta[media_id] = {
                "title_zh": row["title_zh"],
                "title_original": row["title_original"],
                "title_alt_json": row["title_alt_json"],
                "year": row["year"],
                "match_status": row["match_status"],
                "link_count": row["link_count"],
                "has_115": row["has_115"],
            }

        n_docs = len(media_rows)
        avgdl = (sum(doc_len.values()) / n_docs) if n_docs else 1.0

        # global (term -> {kind, df}) across all fields/docs.
        term_kind: dict[str, str] = {}
        term_df: dict[str, int] = {}
        for media_id, per_field in field_tokens.items():
            terms_in_doc: set[str] = set()
            for counts in per_field.values():
                for term, kind in counts:
                    terms_in_doc.add(term)
                    # Prefer the "richest" kind label when a term collides
                    # across kinds (see module docstring: cjk_run/bigram
                    # collisions are avoided by construction, but keep this
                    # deterministic regardless).
                    if term not in term_kind:
                        term_kind[term] = kind
            for term in terms_in_doc:
                term_df[term] = term_df.get(term, 0) + 1

        def idf(term: str) -> float:
            df = term_df[term]
            return math.log(1 + (n_docs - df + 0.5) / (df + 0.5))

        term_rows: list[tuple[str, int, str, float]] = []
        for media_id, per_field in field_tokens.items():
            dl = doc_len[media_id]
            norm = 1 - B + B * (dl / avgdl if avgdl else 0.0)
            for field_name, counts in per_field.items():
                field_weight = FIELD_WEIGHTS[field_name]
                for (term, kind), tf in counts.items():
                    if kind == "latin" and term in STOPWORDS_LATIN:
                        weight = 0.2 * field_weight
                    else:
                        bm25_tf = tf * (K1 + 1) / (tf + K1 * norm) if (tf + K1 * norm) else 0.0
                        weight = idf(term) * bm25_tf * field_weight * KIND_WEIGHTS[kind]
                    term_rows.append((term, media_id, field_name, weight))

        term_rows.sort(key=lambda r: (r[0], r[1], r[2]))
        vocab_rows = sorted(
            (term, term_kind[term], term_df[term], idf(term)) for term in term_df
        )
        filtered_charmap = sorted((src, dst) for src, dst in charmap.items() if src in corpus_chars)

        doc_rows: list[tuple] = []
        for media_id in sorted(doc_meta):
            meta = doc_meta[media_id]
            aliases = json.loads(meta["title_alt_json"] or "[]")
            alias_keys: list[str] = []
            for alias in aliases:
                alias_keys.append(fold(alias, charmap))
                if pinyin is not None and any(_is_cjk(ch) for ch in alias):
                    a_full, a_init = pinyin(alias)
                    if a_full:
                        alias_keys.append(a_full)
                    if a_init:
                        alias_keys.append(a_init)
            if meta["title_original"]:
                alias_keys.append(fold(meta["title_original"], charmap))

            title_key = fold(meta["title_zh"], charmap)
            pinyin_full = pinyin_initials = None
            if pinyin is not None:
                pinyin_full, pinyin_initials = pinyin(meta["title_zh"])
                pinyin_full = _clean_pinyin_key(pinyin_full) or None
                pinyin_initials = _clean_pinyin_key(pinyin_initials) or None

            static_boost = (
                1
                + 0.15 * (1 if meta["has_115"] else 0)
                + 0.10 * math.log1p(meta["link_count"] or 0)
                + 0.10 * (1 if meta["match_status"] in ("exact", "candidate") else 0)
            )

            doc_rows.append(
                (
                    media_id,
                    title_key,
                    json.dumps(alias_keys, ensure_ascii=False),
                    pinyin_full,
                    pinyin_initials,
                    doc_len[media_id],
                    static_boost,
                )
            )

        conn.execute("DELETE FROM search_term")
        conn.execute("DELETE FROM search_vocab")
        conn.execute("DELETE FROM search_charmap")
        conn.execute("DELETE FROM search_doc")

        conn.executemany(
            "INSERT INTO search_doc (media_id, title_key, alias_keys_json, pinyin_full, pinyin_initials, doc_len, static_boost) VALUES (?,?,?,?,?,?,?)",
            doc_rows,
        )
        conn.executemany(
            "INSERT INTO search_term (term, media_id, field, weight) VALUES (?,?,?,?)",
            term_rows,
        )
        conn.executemany(
            "INSERT INTO search_vocab (term, kind, df, idf) VALUES (?,?,?,?)",
            vocab_rows,
        )
        conn.executemany(
            "INSERT INTO search_charmap (src, dst) VALUES (?,?)",
            filtered_charmap,
        )
        conn.commit()

        return IndexStats(media_count=n_docs, term_count=len(term_rows), vocab_count=len(vocab_rows))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7.0.3 query parsing
# ---------------------------------------------------------------------------

_SEASON_RE = re.compile(r"(?<![a-z0-9])s(\d{1,2})(?!\d)|第(\d{1,2})季|(\d{1,2})季")
_EPISODE_RE = re.compile(r"(?<![a-z0-9])e(\d{1,3})(?!\d)")


def _standalone_cjk_re(words: Iterable[str]) -> "re.Pattern[str]":
    """Compile CJK ``words`` into a regex matching only a whole whitespace/
    string-boundary-delimited token equal to one of them -- an intent word
    (type/genre/noise/provider-alias) must never be carved out of the
    interior of a longer CJK run such as a title (hotfix: previously these
    matched as bare substrings anywhere in the query)."""
    alt = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\S)(?:{alt})(?!\S)")


def _standalone_latin_re(words: Iterable[str]) -> "re.Pattern[str]":
    """Compile Latin/digit ``words`` into a regex bounded like the existing
    season/episode/year patterns (not adjacent to another letter or digit),
    so e.g. the "115" provider token is not matched inside "S115"/"1150"/
    "115cdn" (hotfix)."""
    alt = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")


_QUALITY_RE = _standalone_latin_re(["4k", "2160p", "1080p", "720p"])
_QUALITY_MAP = {"4k": "2160p", "2160p": "2160p", "1080p": "1080p", "720p": "720p"}

# Bare "HDR" is mapped to the generic "hdr10" bucket -- see module docstring.
_HDR_CJK_RE = _standalone_cjk_re(["杜比视界"])
_HDR_LATIN_RE = _standalone_latin_re(["dv", "hdr"])
_HDR_MAP = {"dv": "dv", "杜比视界": "dv", "hdr": "hdr10"}

_NOISE_RE = _standalone_cjk_re(["资源", "下载", "网盘", "链接", "高清", "在线"])

# Bounded by non-alnum/string-edge on both sides; suppressed post-hoc below
# when it would leave the residual text empty (see module docstring).
_YEAR_RE = re.compile(r"(?<![0-9a-z])(19\d{2}|20\d{2})(?![0-9a-z])")

# Longer/more specific aliases listed first so e.g. "阿里云盘" is not cut
# short by the shorter "阿里" alternative at the same start position.
_PROVIDER_PATTERNS = [
    ("阿里云盘", "alipan"), ("阿里", "alipan"), ("alipan", "alipan"),
    ("夸克", "quark"), ("quark", "quark"),
    ("百度", "baidu"), ("baidu", "baidu"),
    ("天翼", "tianyicloud"), ("189", "tianyicloud"), ("tianyi", "tianyicloud"),
    ("广亚", "guangya"),
    ("139", "139cloud"),
    ("123", "123"),
    ("115", "115"),
    ("ed2k", "ed2k"),
]
# Split by script: CJK aliases require a whitespace/string-boundary-
# delimited whole token; Latin/digit aliases use the alnum-boundary style
# above (hotfix -- see _standalone_cjk_re/_standalone_latin_re docstrings).
_PROVIDER_CJK_RE = _standalone_cjk_re([p for p, _ in _PROVIDER_PATTERNS if _is_cjk(p[0])])
_PROVIDER_LATIN_RE = _standalone_latin_re([p for p, _ in _PROVIDER_PATTERNS if not _is_cjk(p[0])])
_PROVIDER_LOOKUP = dict(_PROVIDER_PATTERNS)

# "美剧/日剧/韩剧/剧集/电视剧" all resolve to media_type "tv"; nationality is
# not modelled as a separate field (see module docstring).
_TYPE_PATTERNS = [
    ("电影", "movie"),
    ("电视剧", "tv"), ("剧集", "tv"), ("美剧", "tv"), ("日剧", "tv"), ("韩剧", "tv"),
]
_TYPE_RE = _standalone_cjk_re([p for p, _ in _TYPE_PATTERNS])
_TYPE_LOOKUP = dict(_TYPE_PATTERNS)

# "动画"/"动漫" both normalise to the genre value "动画" (matching TMDB's
# Chinese genre name), distinct from the media_type words above.
_GENRE_PATTERNS = [("动画", "动画"), ("动漫", "动画"), ("纪录片", "纪录片")]
_GENRE_RE = _standalone_cjk_re([p for p, _ in _GENRE_PATTERNS])
_GENRE_LOOKUP = dict(_GENRE_PATTERNS)


@dataclass(frozen=True)
class QueryPlan:
    text: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    quality: str | None = None
    hdr: str | None = None
    providers: tuple[str, ...] = ()
    media_type: str | None = None
    genre: str | None = None
    corrections: tuple[tuple[str, str], ...] = ()
    # Original (pre-normalisation) substrings of `q` consumed per dimension
    # -- e.g. {"quality": ("4K",), "providers": ("115",)} -- so the frontend
    # can remove exactly that text from the query box when a filter chip is
    # dismissed. Keys only present for dimensions that were actually parsed.
    spans: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _cut(text: str, match: "re.Match[str]") -> str:
    return text[: match.start()] + " " + text[match.end():]


def _cut_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace each left-to-right, non-overlapping ``(start, end)`` span in
    ``text`` with a single space -- like ``PATTERN.sub(' ', text)`` but
    usable on a second string kept in lockstep with the one the pattern
    actually matched against (see ``parse_query``'s ``cased`` tracking)."""
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(text[last:start])
        out.append(" ")
        last = end
    out.append(text[last:])
    return "".join(out)


# Mirrors library_normalize.normalize_text()'s steps *except* casefold --
# used only to recover the ORIGINAL casing/form of a substring parse_query
# consumed (QueryPlan.spans), while staying offset-aligned with the
# casefolded `remaining` string parse_query actually matches against
# (casefold is 1:1 length-preserving for the ASCII/CJK text this app sees,
# so the two strings stay in lockstep as both are cut the same way).
# Duplicated rather than imported to keep this module's import surface
# limited to normalize_text (see module docstring).
_ZERO_WIDTH_CHARS = ("\u200b", "\u200c", "\ufeff")
_SEPARATOR_TABLE = {
    ord(ch): " "
    for ch in (
        "：", ":", "·", "•", "・", "—", "–", "-", "_", "/", "\\", "|",
        "【", "】", "[", "]", "（", "）", "(", ")", "「", "」", "『", "』",
        ",", "，", "。", "、", "~", "～",
    )
}


def _normalize_keep_case(text: str) -> str:
    result = text or ""
    for ch in _ZERO_WIDTH_CHARS:
        result = result.replace(ch, "")
    result = unicodedata.normalize("NFKC", result)
    result = result.translate(_SEPARATOR_TABLE)
    result = re.sub(r"\s+", " ", result).strip()
    return result


def parse_query(q: str) -> QueryPlan:
    """Split filter keywords (year/season/episode/quality/hdr/provider/type/
    genre/noise) out of ``q``, leaving the residual free-text search string.
    See module docstring for the year-in-title suppression rule.
    """
    remaining = normalize_text(q or "")
    # Kept in lockstep with `remaining` (same cuts, same offsets) so spans
    # can be sliced out with their ORIGINAL casing/form -- see QueryPlan.spans.
    cased = _normalize_keep_case(q or "")
    spans: dict[str, tuple[str, ...]] = {}

    season = None
    match = _SEASON_RE.search(remaining)
    if match:
        value = match.group(1) or match.group(2) or match.group(3)
        season = int(value)
        spans["season"] = (cased[match.start():match.end()],)
        remaining = _cut(remaining, match)
        cased = _cut(cased, match)

    episode = None
    match = _EPISODE_RE.search(remaining)
    if match:
        episode = int(match.group(1))
        spans["episode"] = (cased[match.start():match.end()],)
        remaining = _cut(remaining, match)
        cased = _cut(cased, match)

    quality = None
    match = _QUALITY_RE.search(remaining)
    if match:
        quality = _QUALITY_MAP[match.group(0)]
        spans["quality"] = (cased[match.start():match.end()],)
        remaining = _cut(remaining, match)
        cased = _cut(cased, match)

    hdr = None
    match = None
    for candidate in (_HDR_CJK_RE.search(remaining), _HDR_LATIN_RE.search(remaining)):
        if candidate is not None and (match is None or candidate.start() < match.start()):
            match = candidate
    if match:
        hdr = _HDR_MAP[match.group(0)]
        spans["hdr"] = (cased[match.start():match.end()],)
        remaining = _cut(remaining, match)
        cased = _cut(cased, match)

    providers: list[str] = []
    provider_matches = sorted(
        list(_PROVIDER_CJK_RE.finditer(remaining)) + list(_PROVIDER_LATIN_RE.finditer(remaining)),
        key=lambda m: m.start(),
    )
    for match in provider_matches:
        code = _PROVIDER_LOOKUP[match.group(0)]
        if code not in providers:
            providers.append(code)
    if provider_matches:
        spans["providers"] = tuple(cased[m.start():m.end()] for m in provider_matches)
    provider_spans = [(m.start(), m.end()) for m in provider_matches]
    remaining = _cut_spans(remaining, provider_spans)
    cased = _cut_spans(cased, provider_spans)

    media_type = None
    type_matches = list(_TYPE_RE.finditer(remaining))
    if type_matches:
        media_type = _TYPE_LOOKUP[type_matches[0].group(0)]
        spans["media_type"] = (cased[type_matches[0].start():type_matches[0].end()],)
    type_spans = [(m.start(), m.end()) for m in type_matches]
    remaining = _cut_spans(remaining, type_spans)
    cased = _cut_spans(cased, type_spans)

    genre = None
    genre_matches = list(_GENRE_RE.finditer(remaining))
    if genre_matches:
        genre = _GENRE_LOOKUP[genre_matches[0].group(0)]
        spans["genre"] = (cased[genre_matches[0].start():genre_matches[0].end()],)
    genre_spans = [(m.start(), m.end()) for m in genre_matches]
    remaining = _cut_spans(remaining, genre_spans)
    cased = _cut_spans(cased, genre_spans)

    noise_spans = [(m.start(), m.end()) for m in _NOISE_RE.finditer(remaining)]
    remaining = _cut_spans(remaining, noise_spans)
    cased = _cut_spans(cased, noise_spans)

    year = None
    match = _YEAR_RE.search(remaining)
    if match:
        candidate = re.sub(r"\s+", " ", _cut(remaining, match)).strip()
        if candidate:
            year = int(match.group(1))
            spans["year"] = (cased[match.start():match.end()],)
            remaining = candidate
            cased = re.sub(r"\s+", " ", _cut(cased, match)).strip()
        # else: year would be the entire query -- treat it as the title
        # instead (e.g. the movie literally titled "2012"); leave `remaining`
        # (still containing the year digits) untouched.

    text = re.sub(r"\s+", " ", remaining).strip()

    return QueryPlan(
        text=text,
        year=year,
        season=season,
        episode=episode,
        quality=quality,
        hdr=hdr,
        providers=tuple(providers),
        media_type=media_type,
        genre=genre,
        corrections=(),
        spans=spans,
    )


# ---------------------------------------------------------------------------
# recall / rerank
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    media_id: int
    score: float
    matched_terms: int


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def recall(conn, plan: QueryPlan, charmap, *, limit: int = 300) -> list[Hit]:
    """SQL ``SUM(weight)`` recall over ``search_term`` plus pinyin recall.

    Does not itself attempt spelling correction -- see module docstring
    ("search() owns the recall-empty -> correct_terms -> retry loop").
    """
    text = plan.text
    if not text:
        return []

    tokens = tokenize(text, charmap)
    terms: list[str] = []
    seen: set[str] = set()
    for term, _kind in tokens:
        if term not in seen:
            seen.add(term)
            terms.append(term)

    if tokens and tokens[-1][1] == "latin":
        prefix = tokens[-1][0]
        pattern = _like_escape(prefix) + "%"
        rows = conn.execute(
            "SELECT term FROM search_vocab WHERE kind='latin' AND term LIKE ? ESCAPE '\\' ORDER BY term LIMIT 20",
            (pattern,),
        ).fetchall()
        for row in rows:
            if row["term"] not in seen:
                seen.add(row["term"])
                terms.append(row["term"])

    terms = terms[:64]

    hits: dict[int, Hit] = {}
    if terms:
        placeholders = ",".join("?" for _ in terms)
        rows = conn.execute(
            f"SELECT media_id, SUM(weight) AS s, COUNT(DISTINCT term) AS m "
            f"FROM search_term WHERE term IN ({placeholders}) GROUP BY media_id ORDER BY s DESC LIMIT ?",
            (*terms, limit),
        ).fetchall()
        for row in rows:
            hits[row["media_id"]] = Hit(media_id=row["media_id"], score=float(row["s"]), matched_terms=row["m"])

    stripped = text.strip()
    if stripped and stripped.isascii() and " " not in stripped:
        pattern = _like_escape(fold(text, charmap)) + "%"
        rows = conn.execute(
            "SELECT media_id FROM search_doc WHERE pinyin_full LIKE ? ESCAPE '\\' OR pinyin_initials LIKE ? ESCAPE '\\' LIMIT 100",
            (pattern, pattern),
        ).fetchall()
        top_text_score = max((h.score for h in hits.values()), default=0.0)
        pinyin_score = top_text_score * 0.6 if top_text_score > 0 else 1.0
        for row in rows:
            media_id = row["media_id"]
            if media_id in hits:
                if pinyin_score > hits[media_id].score:
                    hits[media_id] = Hit(media_id=media_id, score=pinyin_score, matched_terms=hits[media_id].matched_terms)
            else:
                hits[media_id] = Hit(media_id=media_id, score=pinyin_score, matched_terms=1)

    ordered = sorted(hits.values(), key=lambda h: (-h.score, h.media_id))
    return ordered[:limit]


def rerank(conn, hits: list[Hit], plan: QueryPlan, charmap) -> list[Hit]:
    if not hits:
        return hits

    media_ids = [h.media_id for h in hits]
    placeholders = ",".join("?" for _ in media_ids)
    rows = conn.execute(
        f"SELECT sd.media_id AS media_id, sd.title_key AS title_key, sd.alias_keys_json AS alias_keys_json, "
        f"sd.static_boost AS static_boost, m.year AS year "
        f"FROM search_doc sd JOIN media m ON m.id = sd.media_id WHERE sd.media_id IN ({placeholders})",
        media_ids,
    ).fetchall()
    doc_map = {row["media_id"]: row for row in rows}

    fold_text = fold(plan.text, charmap) if plan.text else ""

    reranked: list[Hit] = []
    for hit in hits:
        row = doc_map.get(hit.media_id)
        if row is None:
            continue
        title_key = row["title_key"]
        alias_keys = json.loads(row["alias_keys_json"] or "[]")

        harmony = 1 + 0.15 * (hit.matched_terms - 1)
        # An exact match against title_key OR one of the media's alias keys
        # (which also holds the folded title_original -- see module
        # docstring) earns the same boost; prefix/substring stay title-only.
        exact = 2.0 if fold_text and (fold_text == title_key or fold_text in alias_keys) else 1.0
        prefix = 1.5 if fold_text and title_key.startswith(fold_text) else 1.0
        substring = 1.3 if fold_text and fold_text in title_key else 1.0
        if plan.year is not None:
            year_mult = 1.2 if row["year"] == plan.year else 0.8
        else:
            year_mult = 1.0

        score = hit.score * harmony * exact * prefix * substring * year_mult * row["static_boost"]
        reranked.append(Hit(media_id=hit.media_id, score=score, matched_terms=hit.matched_terms))

    reranked.sort(key=lambda h: (-h.score, h.media_id))
    return reranked


# ---------------------------------------------------------------------------
# corrections (needed by search()'s recall-empty fallback -- see module
# docstring; suggest()'s own dedicated tests land in a later commit)
# ---------------------------------------------------------------------------


def _levenshtein(a: str, b: str, max_edits: int) -> int | None:
    """Edit distance, or None as soon as it is certain to exceed max_edits."""
    if abs(len(a) - len(b)) > max_edits:
        return None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[len(b)]


def correct_terms(conn, terms: list[str]) -> list[tuple[str, str]]:
    """Latin spelling correction: length>=5 allows 1 edit, length>=9 allows 2.

    Candidates are prefiltered by first letter and length +-2 (via SQL)
    before the exact edit distance is computed -- see module docstring for
    why a direct Levenshtein DP is used instead of ``difflib``.
    """
    results: list[tuple[str, str]] = []
    for term in terms:
        n = len(term)
        if n < 5:
            continue
        max_edits = 1 if n < 9 else 2
        rows = conn.execute(
            "SELECT term FROM search_vocab WHERE kind='latin' AND substr(term,1,1)=? "
            "AND length(term) BETWEEN ? AND ?",
            (term[0], n - 2, n + 2),
        ).fetchall()
        best_term = None
        best_dist = None
        for row in sorted(rows, key=lambda r: r["term"]):
            candidate = row["term"]
            if candidate == term:
                continue
            dist = _levenshtein(term, candidate, max_edits)
            if dist is not None and dist <= max_edits and (best_dist is None or dist < best_dist):
                best_term, best_dist = candidate, dist
        if best_term is not None:
            results.append((term, best_term))
    return results


# ---------------------------------------------------------------------------
# filter + paginate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    media_type: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    providers: tuple[str, ...] = ()
    quality: str | None = None
    hdr: str | None = None
    season: int | None = None
    include_deleted: bool = False
    # u1-backend T2 §5: media whose genres_json contains this exact name.
    genre: str | None = None
    # at least one resource_group with this source_type.
    source: str | None = None
    # media.backdrop_path IS NOT NULL AND media.overview is non-empty.
    has_backdrop: bool = False
    # at least one resource_group with complete_season=1.
    complete_season: bool = False


@dataclass(frozen=True)
class SearchPage:
    total: int
    items: list[dict]
    interpreted: dict
    page: int
    page_size: int


_SORT_SQL = {
    "year_desc": "(m.year IS NULL) ASC, m.year DESC, m.id ASC",
    "year_asc": "(m.year IS NULL) ASC, m.year ASC, m.id ASC",
    "links_desc": "m.link_count DESC, m.id ASC",
    # Fallback ordering for sort="relevance" with no free text to rank by
    # (pure filter browse): mirrors media's own idx_media_rank index.
    "relevance": "m.has_115 DESC, m.link_count DESC, m.id ASC",
}


def _apply_filters_sql(filters: Filters) -> tuple[list[str], list]:
    """Build parameterised WHERE-clause fragments (against alias ``m``) for ``filters``."""
    clauses: list[str] = []
    params: list = []

    if filters.media_type and filters.media_type != "all":
        clauses.append("m.media_type = ?")
        params.append(filters.media_type)
    if filters.year_from is not None:
        clauses.append("m.year >= ?")
        params.append(filters.year_from)
    if filters.year_to is not None:
        clauses.append("m.year <= ?")
        params.append(filters.year_to)
    if filters.genre:
        # Pragmatic JSON-array membership check (matches a quoted string
        # literally, same approach as suggest()'s alias_keys_json prefix
        # match -- see module docstring): avoids a JSON1 dependency while
        # still requiring the whole genre name, not a substring of a
        # longer one, because the closing quote must follow immediately.
        clauses.append("m.genres_json LIKE ? ESCAPE '\\'")
        params.append('%"' + _like_escape(filters.genre) + '"%')
    if filters.has_backdrop:
        clauses.append("m.backdrop_path IS NOT NULL AND m.overview IS NOT NULL AND m.overview != ''")

    # When deleted links are excluded, every link-level predicate below must
    # only see live links, and a group whose only matching links are all
    # deleted must not satisfy a group-level (quality/hdr/season/source/
    # complete_season) predicate either -- see module docstring / brief for
    # the controller decision.
    link_live_sql = "" if filters.include_deleted else f" AND ({live_link_sql('rl')})"

    group_predicates: list[str] = []
    group_params: list = []
    if filters.quality:
        group_predicates.append("rg.quality = ?")
        group_params.append(filters.quality)
    if filters.hdr:
        group_predicates.append("rg.hdr = ?")
        group_params.append(filters.hdr)
    if filters.season is not None:
        group_predicates.append("(rg.season_from IS NOT NULL AND ? BETWEEN rg.season_from AND rg.season_to)")
        group_params.append(filters.season)
    if filters.source:
        group_predicates.append("rg.source_type = ?")
        group_params.append(filters.source)
    if filters.complete_season:
        group_predicates.append("rg.complete_season = 1")
    if not filters.include_deleted and (
        filters.quality or filters.hdr or filters.season is not None
        or filters.source or filters.complete_season
    ):
        group_predicates.append(
            f"EXISTS (SELECT 1 FROM resource_link rl2 WHERE rl2.group_id = rg.id AND {live_link_sql('rl2')})"
        )
    if filters.providers:
        providers = list(filters.providers[:64])
        placeholders = ",".join("?" for _ in providers)
        group_predicates.append(
            f"EXISTS (SELECT 1 FROM resource_link rl WHERE rl.group_id = rg.id AND rl.provider IN ({placeholders}){link_live_sql})"
        )
        group_params.extend(providers)

    if group_predicates:
        group_where = " AND ".join(group_predicates)
        clauses.append(f"EXISTS (SELECT 1 FROM resource_group rg WHERE rg.media_id = m.id AND {group_where})")
        params.extend(group_params)

    if not filters.include_deleted:
        clauses.append(
            "EXISTS (SELECT 1 FROM resource_group rg3 JOIN resource_link rl3 ON rl3.group_id = rg3.id "
            f"WHERE rg3.media_id = m.id AND {live_link_sql('rl3')})"
        )

    return clauses, params


def _sort_media_ids(conn, media_ids: list[int], sort: str) -> list[int]:
    if not media_ids:
        return []
    placeholders = ",".join("?" for _ in media_ids)
    order_sql = _SORT_SQL.get(sort, _SORT_SQL["relevance"])
    rows = conn.execute(
        f"SELECT m.id AS id FROM media m WHERE m.id IN ({placeholders}) ORDER BY {order_sql}",
        media_ids,
    ).fetchall()
    return [row["id"] for row in rows]


_QUALITY_RANK = {"2160p": 3, "1080p": 2, "720p": 1}

_OVERVIEW_SHORT_LIMIT = 120


def _overview_short(overview: str | None, limit: int = _OVERVIEW_SHORT_LIMIT) -> str:
    """Trim ``overview`` to ``limit`` characters with a trailing "…" when
    cut; "" when absent (u1-backend T2 §3)."""
    if not overview:
        return ""
    if len(overview) <= limit:
        return overview
    return overview[:limit] + "…"


def _order_providers(codes) -> list[str]:
    """Distinct provider codes with ``"115"`` first, the rest alphabetical."""
    rest = sorted(c for c in codes if c != "115")
    return (["115"] if "115" in codes else []) + rest


def _fetch_items(
    conn, media_ids: list[int], include_deleted: bool = False, providers: tuple[str, ...] = (),
) -> list[dict]:
    placeholders = ",".join("?" for _ in media_ids)

    # T17 fix wave 1 item 5: qualified with the link table's own alias at
    # every call site below, never left bare -- each subquery joins
    # resource_link under a different alias, and a bare `deleted_at_source`
    # is fragile even where it happens to resolve unambiguously today.
    def _live_clause(alias: str) -> str:
        return "" if include_deleted else f" AND ({live_link_sql(alias)})"

    # group_count must agree with link_count (media.link_count, live links
    # only -- LibraryStore.recount() excludes deleted-at-source links from
    # both media.link_count and resource_group.link_count/has_115, the same
    # rule this module's own provider-filtered branch below and
    # library_store._group_summary_payload's unfiltered baseline both use)
    # unless the caller asked to include deleted links: otherwise a group
    # whose only links are all deleted-at-source still counted towards
    # group_count, producing cards like "2 个版本 · 0 条链接".
    group_count_sql = (
        "(SELECT COUNT(*) FROM resource_group WHERE resource_group.media_id = media.id)"
        if include_deleted else
        "(SELECT COUNT(*) FROM resource_group rg_gc WHERE rg_gc.media_id = media.id "
        "AND EXISTS (SELECT 1 FROM resource_link rl_gc WHERE rl_gc.group_id = rg_gc.id "
        f"AND {live_link_sql('rl_gc')}))"
    )
    link_count_sql = "media.link_count"
    provider_params: list = []
    if providers:
        # T17 §14.1: when a provider filter is active, both link_count and
        # group_count are recomputed from ONLY that provider's resource_link
        # rows -- never the media-wide (all-provider) precomputed columns --
        # so a card filtered to provider=115 can't show a link/group count
        # padded out by some other, invisible provider's links.
        provider_placeholders = ",".join("?" for _ in providers)
        provider_params = list(providers)
        group_count_sql = (
            f"(SELECT COUNT(*) FROM resource_group rg_pc WHERE rg_pc.media_id = media.id "
            f"AND EXISTS (SELECT 1 FROM resource_link rl_pc WHERE rl_pc.group_id = rg_pc.id "
            f"AND rl_pc.provider IN ({provider_placeholders}){_live_clause('rl_pc')}))"
        )
        link_count_sql = (
            f"(SELECT COUNT(*) FROM resource_link rl_lc JOIN resource_group rg_lc ON rg_lc.id = rl_lc.group_id "
            f"WHERE rg_lc.media_id = media.id AND rl_lc.provider IN ({provider_placeholders}){_live_clause('rl_lc')})"
        )

    media_rows = conn.execute(
        f"SELECT id, media_type, title_zh, title_original, year, poster_path, backdrop_path, "
        f"overview, match_status, genres_json, has_115, tmdb_id, imdb_id, tvmaze_id, "
        f"ratings_json, ratings_status, "
        f"{link_count_sql} AS link_count, {group_count_sql} AS group_count "
        f"FROM media WHERE id IN ({placeholders})",
        (*provider_params, *provider_params, *media_ids),
    ).fetchall()
    media_by_id = {row["id"]: row for row in media_rows}

    # T17 fix wave 1 item 2: gated on the same provider EXISTS filter as
    # group_count_sql above when a filter is active, so a group with no
    # matching live link never contributes its display_title to
    # `groups_summary` (or its has_115 to the sort key just below) even
    # though it still exists, unfiltered, in `resource_group`.
    group_filter_sql = ""
    group_filter_params: list = []
    if providers:
        group_filter_sql = (
            f" AND EXISTS (SELECT 1 FROM resource_link rl_gf WHERE rl_gf.group_id = resource_group.id "
            f"AND rl_gf.provider IN ({provider_placeholders}){_live_clause('rl_gf')})"
        )
        group_filter_params = list(providers)
    group_rows = conn.execute(
        f"SELECT id, media_id, display_title, has_115, quality FROM resource_group "
        f"WHERE media_id IN ({placeholders}){group_filter_sql}",
        (*media_ids, *group_filter_params),
    ).fetchall()
    groups_by_media: dict[int, list] = {}
    for row in group_rows:
        groups_by_media.setdefault(row["media_id"], []).append(row)

    # One parameterised query for the whole page: the distinct live
    # (media_id, provider) pairs across every resource group of each media,
    # used to build each item's `providers` list below -- restricted to the
    # filter's provider set when one is given, so a filtered item's
    # `providers`/`has_115` never reveal a hidden, non-matching provider.
    provider_sql = (
        f"SELECT rg.media_id AS media_id, rl.provider AS provider "
        f"FROM resource_link rl JOIN resource_group rg ON rg.id = rl.group_id "
        f"WHERE rg.media_id IN ({placeholders}) AND ({live_link_sql('rl')})"
    )
    provider_sql_params = list(media_ids)
    if providers:
        provider_placeholders = ",".join("?" for _ in providers)
        provider_sql += f" AND rl.provider IN ({provider_placeholders})"
        provider_sql_params += list(providers)
    provider_sql += " GROUP BY rg.media_id, rl.provider"
    provider_rows = conn.execute(provider_sql, provider_sql_params).fetchall()
    providers_by_media: dict[int, set] = {}
    for row in provider_rows:
        providers_by_media.setdefault(row["media_id"], set()).add(row["provider"])

    # w6-contract: `all_links_invalid` is a whole-media fact (media has >=1
    # link and none of them are live), deliberately ignoring this call's
    # own `providers`/`include_deleted` filter args -- a card must show the
    # same badge regardless of which provider tab is active. `media.
    # link_count` is already the live-only count (recount() excludes both
    # deleted-at-source and checker-invalid links from it, see
    # library_store.live_link_sql), so it's reused as-is here rather than
    # re-deriving it with another live_link_sql subquery.
    all_invalid_rows = conn.execute(
        f"SELECT media.id AS media_id, media.link_count AS live_link_count, "
        f"(SELECT COUNT(*) FROM resource_link rl_ai JOIN resource_group rg_ai ON rg_ai.id = rl_ai.group_id "
        f"WHERE rg_ai.media_id = media.id) AS total_link_count "
        f"FROM media WHERE media.id IN ({placeholders})",
        media_ids,
    ).fetchall()
    all_links_invalid_by_media = {
        row["media_id"]: bool(row["total_link_count"]) and not row["live_link_count"]
        for row in all_invalid_rows
    }

    items: list[dict] = []
    for media_id in media_ids:
        row = media_by_id.get(media_id)
        if row is None:
            continue
        # T17 fix wave 1 item 2: a group's raw has_115 column is unfiltered
        # -- under provider=quark, a group kept only because it ALSO has a
        # (now hidden) 115 link must not still sort as if it has one, the
        # same way `has_115` just below never leaks through a filtered item.
        groups = sorted(
            groups_by_media.get(media_id, []),
            key=lambda g: (
                -int(bool(g["has_115"]) and ("115" in providers if providers else True)),
                -_QUALITY_RANK.get(g["quality"], 0),
            ),
        )
        item_providers = _order_providers(providers_by_media.get(media_id, ()))
        has_115 = ("115" in item_providers) if providers else bool(row["has_115"])
        ratings = format_ratings(
            row["ratings_json"], media_type=row["media_type"],
            tmdb_id=row["tmdb_id"], imdb_id=row["imdb_id"], tvmaze_id=row["tvmaze_id"],
        )
        items.append(
            {
                "media_id": media_id,
                "media_type": row["media_type"],
                "title": row["title_zh"],
                "original_title": row["title_original"],
                "year": row["year"],
                "poster_path": row["poster_path"],
                "backdrop_path": row["backdrop_path"],
                "overview_short": _overview_short(row["overview"]),
                "match_status": row["match_status"],
                "genres": json.loads(row["genres_json"] or "[]"),
                "link_count": row["link_count"],
                "group_count": row["group_count"],
                "has_115": has_115,
                "providers": item_providers,
                "groups_summary": [g["display_title"] for g in groups],
                "needs_review": row["match_status"] == "needs_review",
                "ratings": ratings,
                "ratings_status": row["ratings_status"],
                "primary_rating": primary_rating(ratings),
                "all_links_invalid": all_links_invalid_by_media.get(media_id, False),
            }
        )
    return items


def search(
    store: LibraryStore,
    q: str,
    filters: Filters,
    *,
    sort: str = "relevance",
    page: int = 1,
    page_size: int = 24,
    charmap=None,
) -> SearchPage:
    charmap = charmap or {}
    conn = store.connect(readonly=True)
    try:
        plan = parse_query(q)
        hits = recall(conn, plan, charmap)

        if not hits and plan.text:
            tokens = tokenize(plan.text, charmap)
            latin_terms = [t for t, k in tokens if k == "latin" and t not in STOPWORDS_LATIN]
            corrections = correct_terms(conn, latin_terms) if latin_terms else []
            if corrections:
                corrected_map = dict(corrections)
                # Substitute only the corrected Latin words into the
                # ORIGINAL text at word level -- joining tokenize()'s full
                # sub-token stream instead (bigrams/chars/runs) would
                # corrupt mixed CJK+Latin text (see module docstring).
                words = plan.text.split(" ")
                corrected_words = [corrected_map.get(fold(w, charmap), w) for w in words]
                corrected_text = " ".join(corrected_words)
                plan = replace(plan, text=corrected_text, corrections=tuple(corrections))
                hits = recall(conn, plan, charmap)

        hits = rerank(conn, hits, plan, charmap)

        # Merge the caller's explicit Filters with parse_query's plan:
        # explicit values win when set, otherwise the plan's parsed value is
        # used. `interpreted` below reflects these effective values.
        effective_media_type = filters.media_type if filters.media_type is not None else plan.media_type
        effective_quality = filters.quality if filters.quality is not None else plan.quality
        effective_hdr = filters.hdr if filters.hdr is not None else plan.hdr
        effective_season = filters.season if filters.season is not None else plan.season
        effective_providers = filters.providers if filters.providers else plan.providers
        if filters.year_from is None and filters.year_to is None and plan.year is not None:
            effective_year_from = effective_year_to = plan.year
        else:
            effective_year_from = filters.year_from
            effective_year_to = filters.year_to
        effective_filters = replace(
            filters,
            media_type=effective_media_type,
            quality=effective_quality,
            hdr=effective_hdr,
            season=effective_season,
            providers=effective_providers,
            year_from=effective_year_from,
            year_to=effective_year_to,
        )

        clauses, params = _apply_filters_sql(effective_filters)
        where_sql = " AND ".join(clauses) if clauses else "1=1"

        if plan.text:
            hit_ids = [h.media_id for h in hits]
            if not hit_ids:
                total = 0
                items: list[dict] = []
            else:
                placeholders = ",".join("?" for _ in hit_ids)
                rows = conn.execute(
                    f"SELECT m.id AS id FROM media m WHERE m.id IN ({placeholders}) AND {where_sql}",
                    (*hit_ids, *params),
                ).fetchall()
                eligible = {row["id"] for row in rows}
                if sort == "relevance":
                    ordered_ids = [mid for mid in hit_ids if mid in eligible]
                else:
                    ordered_ids = _sort_media_ids(conn, list(eligible), sort)
                total = len(ordered_ids)
                page_ids = ordered_ids[(page - 1) * page_size : page * page_size]
                items = _fetch_items(conn, page_ids, effective_filters.include_deleted, effective_filters.providers) if page_ids else []
        else:
            count_row = conn.execute(f"SELECT COUNT(*) AS c FROM media m WHERE {where_sql}", params).fetchone()
            total = count_row["c"]
            order_sql = _SORT_SQL.get(sort, _SORT_SQL["relevance"])
            offset = (page - 1) * page_size
            rows = conn.execute(
                f"SELECT m.id AS id FROM media m WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall()
            page_ids = [row["id"] for row in rows]
            items = _fetch_items(conn, page_ids, effective_filters.include_deleted, effective_filters.providers) if page_ids else []

        interpreted = {
            "text": plan.text,
            "year": plan.year,
            "quality": effective_quality,
            "hdr": effective_hdr,
            "providers": list(effective_providers),
            "season": effective_season,
            "media_type": effective_media_type,
            "corrections": [list(c) for c in plan.corrections],
            "spans": {key: list(value) for key, value in plan.spans.items()},
        }
        return SearchPage(total=total, items=items, interpreted=interpreted, page=page, page_size=page_size)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7.0.4 suggest
# ---------------------------------------------------------------------------


def suggest(store: LibraryStore, q: str, *, limit: int = 8, charmap=None) -> list[dict]:
    """Local-only prefix autocomplete over title/alias keys and pinyin.

    Returns ``[]`` for a query shorter than "1 CJK char or 2 Latin letters"
    (server-side mirror of the frontend's debounce guard). Deduplication is
    structural: ``search_doc`` has one row per ``media_id``, so a single
    query with OR'd prefix conditions can return each media at most once
    regardless of how many of the 4 fields matched.
    """
    charmap = charmap or {}
    folded = fold(q, charmap)
    cjk_count = sum(1 for ch in folded if _is_cjk(ch))
    latin_count = sum(1 for ch in folded if ch.isalpha() and not _is_cjk(ch))
    if cjk_count < 1 and latin_count < 2:
        return []

    conn = store.connect(readonly=True)
    try:
        prefix = _like_escape(folded)
        title_pattern = prefix + "%"
        # Approximate "any alias/pinyin entry starts with prefix" by matching
        # right after a JSON-string-opening quote -- see module docstring
        # (no JSON1 dependency).
        alias_pattern = '%"' + prefix + "%"
        rows = conn.execute(
            """
            SELECT sd.media_id AS media_id, sd.static_boost AS static_boost,
                   m.title_zh AS title, m.year AS year, m.media_type AS media_type
            FROM search_doc sd JOIN media m ON m.id = sd.media_id
            WHERE sd.title_key LIKE ? ESCAPE '\\'
               OR sd.alias_keys_json LIKE ? ESCAPE '\\'
               OR sd.pinyin_full LIKE ? ESCAPE '\\'
               OR sd.pinyin_initials LIKE ? ESCAPE '\\'
            ORDER BY sd.static_boost DESC, sd.media_id ASC
            LIMIT ?
            """,
            (title_pattern, alias_pattern, title_pattern, title_pattern, limit),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "media_id": row["media_id"],
            "title": row["title"],
            "year": row["year"],
            "media_type": row["media_type"],
        }
        for row in rows
    ]
