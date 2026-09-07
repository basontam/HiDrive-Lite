"""Pure-function normaliser for the HiDrive-Lite personal media library.

This module has no I/O and no dependency on Flask/requests: it is a set of
deterministic, pure functions that turn raw spreadsheet cell values (title,
remark, link, access-code columns) into normalised text, parsed structures,
and stable identity/fingerprint keys used for deduplication.

``NORMALIZE_VERSION`` must be bumped whenever any rule in this module changes
the output for some input, so callers (the importer, ``schema_meta``) can
detect "same input + same version => same output" has broken.

Design decisions taken where the architecture notes (docs/architecture.md)
left the exact representation open are recorded next to the function/constant
they affect, and are exercised by
tests in ``tests/test_library_normalize.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlparse

NORMALIZE_VERSION = "2"

_ZERO_WIDTH_CHARS = ("\u200b", "\u200c", "\ufeff")

# §4.1 separator-unification table: each of these characters becomes a space.
_SEPARATOR_CHARS = (
    "\uff1a", ":", "\u00b7", "\u2022", "\u30fb", "\u2014", "\u2013", "-", "_", "/", "\\", "|",
    "\u3010", "\u3011", "[", "]", "\uff08", "\uff09", "(", ")", "\u300c", "\u300d", "\u300e", "\u300f",
    ",", "\uff0c", "\u3002", "\u3001", "~", "\uff5e",
)
_SEPARATOR_TABLE = {ord(ch): " " for ch in _SEPARATOR_CHARS}


def normalize_text(value: str) -> str:
    """NFKC -> casefold -> unify separators to space -> collapse whitespace -> strip.

    Zero-width characters (U+200B, U+200C, U+FEFF) are removed first.  The
    result is idempotent: ``normalize_text(normalize_text(x)) ==
    normalize_text(x)``.
    """
    text = value or ""
    for ch in _ZERO_WIDTH_CHARS:
        text = text.replace(ch, "")
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = text.translate(_SEPARATOR_TABLE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def search_key(value: str) -> str:
    """``normalize_text`` then keep only letters, digits and CJK characters.

    Used for LIKE/instr-style substring search and for building the
    ``media_identity`` title key.
    """
    text = normalize_text(value)
    return "".join(ch for ch in text if ch.isalnum())


# ---------------------------------------------------------------------------
# §4.2 title and year
# ---------------------------------------------------------------------------

# Trailing "(YYYY)" / "（YYYY）", allowing internal spaces around the digits.
_YEAR_PAREN_RE = re.compile(r"[（(]\s*((?:19|20)\d{2})\s*[）)]\s*$")
# Trailing empty "()" / "（）".
_YEAR_EMPTY_PAREN_RE = re.compile(r"[（(]\s*[）)]\s*$")
# Trailing bare four-digit year with a mandatory preceding space, no brackets.
_YEAR_BARE_RE = re.compile(r"\s((?:19|20)\d{2})\s*$")

_YEAR_MIN, _YEAR_MAX = 1900, 2030

_ALIAS_SPLIT_RE = re.compile(r"\s+/\s+|／|\|")
_LATIN_RUN_RE = re.compile(r"[A-Za-z]{3,}")


@dataclass(frozen=True)
class TitleInfo:
    title_zh: str
    aliases: tuple[str, ...]
    original_hint: str | None
    year: int | None


def _split_year(cell: str) -> tuple[str, int | None]:
    """Return (title without trailing year token, year or None).

    The year patterns (per §4.2) only recognise a 19xx/20xx digit prefix, so
    e.g. "(1899)" never matches at all and is left in the title untouched.
    When a pattern *does* match but the captured year falls outside
    1900-2030 (e.g. "(2099)"), the trailing token is still stripped from the
    returned title -- it is unambiguously a year slot, just an invalid one --
    but the returned year is None.
    """
    stripped = cell.strip()
    match = _YEAR_PAREN_RE.search(stripped)
    if match:
        year = int(match.group(1))
        if not (_YEAR_MIN <= year <= _YEAR_MAX):
            year = None
        return stripped[: match.start()].strip(), year
    match = _YEAR_EMPTY_PAREN_RE.search(stripped)
    if match:
        return stripped[: match.start()].strip(), None
    match = _YEAR_BARE_RE.search(stripped)
    if match:
        year = int(match.group(1))
        if not (_YEAR_MIN <= year <= _YEAR_MAX):
            year = None
        return stripped[: match.start()].strip(), year
    return stripped, None


def parse_title(title_cell: str, media_title_cell: str) -> TitleInfo:
    """Parse the "标题"/"媒体标题" columns into a :class:`TitleInfo`.

    ``title_zh`` prefers ``media_title_cell`` (stripped); when that is empty
    it falls back to ``title_cell`` with its trailing year token removed.
    ``aliases`` are the ``title_cell`` (year removed) parts split on
    ``" / "``, ``"／"`` or ``"|"`` that are not equal to ``title_zh``, kept in
    the order they appear.  ``original_hint`` is the first alias containing
    three or more consecutive Latin letters (a Latin/English alias -- a
    candidate ``original_title`` for TMDB matching), or ``None``.
    """
    title_no_year, year = _split_year(title_cell or "")
    media_title = (media_title_cell or "").strip()
    title_zh = media_title or title_no_year

    aliases: list[str] = []
    for part in _ALIAS_SPLIT_RE.split(title_no_year):
        part = part.strip()
        if part and part != title_zh:
            aliases.append(part)

    original_hint = next((alias for alias in aliases if _LATIN_RUN_RE.search(alias)), None)

    return TitleInfo(title_zh=title_zh, aliases=tuple(aliases), original_hint=original_hint, year=year)


# ---------------------------------------------------------------------------
# §4.3 edition parsing (remark + title + ED2K filename)
# ---------------------------------------------------------------------------

_CHINESE_NUMERALS = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10", "两": "2",
}
_CHINESE_NUMERAL_RE = re.compile("[" + "".join(_CHINESE_NUMERALS) + "]")

_SEASON_RANGE_RE = re.compile(r"S\s*(\d{1,2})(?:\s*[-~–]\s*S?\s*(\d{1,2}))?", re.I)
_SEASON_WORD_RE = re.compile(r"Season\s*(\d+)", re.I)
_SEASON_DI_RE = re.compile(r"第\s*(\d+)\s*季")
_SEASON_N_QUAN_RE = re.compile(r"(\d+)\s*季全|全\s*(\d+)\s*季")
_SEASON_QUAN_RE = re.compile(r"全季|季全")

_EPISODE_SE_RE = re.compile(r"S\d{1,2}\s*E(\d{1,3})(?:\s*[-~–]\s*E?(\d{1,3}))?", re.I)
_EPISODE_RANGE_RE = re.compile(r"\bE(\d{1,3})\s*[-~–]\s*E?(\d{1,3})", re.I)
_EPISODE_DI_RE = re.compile(r"第\s*(\d+)\s*集")
_EPISODE_QUAN_N_RE = re.compile(r"(\d+)\s*集全")
_EPISODE_QUAN_RE = re.compile(r"全集|合集")
_EPISODE_SINGLE_RE = re.compile(r"单集")

_QUALITY_RULES = (
    (re.compile(r"2160p|4K|UHD", re.I), "2160p"),
    (re.compile(r"1080[pi]", re.I), "1080p"),
    (re.compile(r"720p", re.I), "720p"),
)
_QUALITY_OTHER_RE = re.compile(r"\d{3,4}p", re.I)

_SOURCE_RULES = (
    (re.compile(r"REMUX", re.I), "remux"),
    (re.compile(r"原盘|BluRay|Blu-ray|蓝光|ISO", re.I), "bluray"),
    (re.compile(r"BDRip", re.I), "bdrip"),
    (
        re.compile(
            r"WEB-?DL|WEB|HiveWeb|\bNF\b|Netflix|\bHBO\b|\bMax\b|Amazon|\bAMZN\b|Disney|\bDSNP\b|"
            r"iTunes|\bATVP\b|咪咕|\bBBC\b|\biQ\b|Vivid|\bEDR\b|\bHQ\b",
            re.I,
        ),
        "webdl",
    ),
    (re.compile(r"HDTV", re.I), "hdtv"),
)

_HDR_RULES = (
    (re.compile(r"DV\s*[&+]\s*HDR|杜比视界.*HDR", re.I), "dv_hdr"),
    (re.compile(r"\bDV\b|杜比视界|Dolby ?Vision|\bP[58]\b", re.I), "dv"),
    (re.compile(r"HDR10\+|HDR10P", re.I), "hdr10plus"),
    (re.compile(r"HLG", re.I), "hlg"),
    (re.compile(r"HDR10|HDR|\bEDR\b|Vivid", re.I), "hdr10"),
    (re.compile(r"SDR", re.I), "sdr"),
)

_CODEC_RULES = (
    (re.compile(r"HEVC|H\.?265|x265", re.I), "hevc"),
    (re.compile(r"AVC|H\.?264|x264", re.I), "avc"),
    (re.compile(r"AV1", re.I), "av1"),
)

# Audio/subtitle normalised spellings below are this module's own choice
# (the construction plan leaves the exact spelling open) -- see the class
# docstrings of EditionInfo for the full rationale.  They are asserted
# verbatim in tests/test_library_normalize.py.
_DD_RE = re.compile(r"(DDP|DD\+|DD)\.?\s?(\d)\.(\d)", re.I)
_DTS_RE = re.compile(r"DTS(-HD)?(\s*MA)?(?:\s*(\d)\.(\d))?", re.I)
_TRUEHD_RE = re.compile(r"TrueHD", re.I)
_ATMOS_RE = re.compile(r"Atmos|杜比全景声", re.I)
_DOLBY_AUDIO_RE = re.compile(r"杜比音效")
_AAC_RE = re.compile(r"\bAAC\b", re.I)
_FLAC_RE = re.compile(r"\bFLAC\b", re.I)
_LANGUAGE_AUDIO_MAP = {
    "国英音轨": "mandarin+english",
    "国粤音轨": "mandarin+cantonese",
    "国粤英音轨": "mandarin+cantonese+english",
    "国配": "mandarin",
    "国语": "mandarin",
    "粤语": "cantonese",
    "英语": "english",
    "双语音轨": "dual",
}

_SUBTITLE_RULES = (
    (re.compile(r"内封简繁"), "embedded:zh-hans+zh-hant"),
    (re.compile(r"内封简中"), "embedded:zh-hans"),
    (re.compile(r"内封繁中"), "embedded:zh-hant"),
    (re.compile(r"内封简英"), "embedded:zh-hans+en"),
    (re.compile(r"内嵌简中"), "embedded:zh-hans"),
    (re.compile(r"外挂简中"), "external:zh-hans"),
    (re.compile(r"特效字幕"), "effects"),
    (re.compile(r"简英双语"), "dual:zh-hans+en"),
    (re.compile(r"内封AI简繁", re.I), "embedded:ai:zh-hans+zh-hant"),
    (re.compile(r"中字"), "zh"),
    (re.compile(r"简/繁"), "zh-hans+zh-hant"),
)

_TAG_RULES = (
    (re.compile(r"\bHQ\b", re.I), "hq"),
    (re.compile(r"高码"), "highbitrate"),
    (re.compile(r"60FPS|60帧", re.I), "60fps"),
    (re.compile(r"50FPS", re.I), "50fps"),
    (re.compile(r"补"), "resend"),
    (re.compile(r"洗版"), "rewash"),
    (re.compile(r"自购"), "purchased"),
    (re.compile(r"仅秒传"), "instant_only"),
)
_DOUBAN_RE = re.compile(r"豆瓣\s?(\d(?:\.\d)?)")

# Only these nine named platforms produce a src:* tag -- see module/class
# docstring design-decision note (no src:other is ever emitted).
_SRC_PLATFORM_RULES = (
    (re.compile(r"\bNF\b|Netflix", re.I), "netflix"),
    (re.compile(r"\bHBO\b|\bMax\b", re.I), "max"),
    (re.compile(r"Amazon|\bAMZN\b", re.I), "amazon"),
    (re.compile(r"Disney|\bDSNP\b", re.I), "disney"),
    (re.compile(r"iTunes|\bATVP\b", re.I), "itunes"),
    (re.compile(r"HiveWeb", re.I), "hive"),
    (re.compile(r"咪咕"), "migu"),
    (re.compile(r"\bBBC\b", re.I), "bbc"),
    (re.compile(r"\biQ\b", re.I), "iq"),
)

_EDITION_SCALAR_FIELDS = (
    "season_from", "season_to", "episode_from", "episode_to",
    "quality", "source_type", "hdr", "video_codec",
)


@dataclass(frozen=True)
class EditionInfo:
    """A resource's parsed edition/version facts (§4.3).

    ``complete_season`` doubles as a general "complete/full release" signal:
    it is set both by season-level completeness markers (``全季``/``季全``/
    ``N季全``/``全N季``) and by episode-level completeness markers (``全集``/
    ``合集``/``N集全``), because the interface exposes only one completeness
    boolean and there is no separate "complete episodes" field.  ``N集全``
    additionally sets ``episode_from=1, episode_to=N``.

    ``unparsed`` reflects the *remark* text alone (per its one-line
    docstring: "remark non-empty but no field recognised"): it is True only
    when ``remark`` is non-empty after normalisation and parsing that remark
    in isolation recognised nothing at all.  A non-empty ``title_cell``/
    ``ed2k_filename`` contributing fields does not clear an otherwise-True
    ``unparsed``, and an empty ``remark`` never sets it True even if
    ``title_cell``/``ed2k_filename`` supplied real data.
    """

    season_from: int | None
    season_to: int | None
    episode_from: int | None
    episode_to: int | None
    complete_season: bool
    quality: str | None
    source_type: str | None
    hdr: str | None
    video_codec: str | None
    audio: tuple[str, ...]
    subtitle: tuple[str, ...]
    tags: tuple[str, ...]
    unparsed: bool


def _convert_chinese_numerals(text: str) -> str:
    return _CHINESE_NUMERAL_RE.sub(lambda m: _CHINESE_NUMERALS[m.group(0)], text)


def _prep_edition_text(text: str) -> str:
    text = text or ""
    text = unicodedata.normalize("NFKC", text)
    for ch in _ZERO_WIDTH_CHARS:
        text = text.replace(ch, "")
    # Split bracket-group tokens apart so adjacent 【...】/[...] groups don't
    # glom together, per §4.3 preprocessing.
    for ch in "【】[]":
        text = text.replace(ch, " ")
    text = _convert_chinese_numerals(text)
    return text


def _normalize_dd(match: re.Match) -> str:
    prefix = match.group(1).upper().replace("+", "P")
    label = "ddp" if prefix == "DDP" else "dd"
    return f"{label}{match.group(2)}.{match.group(3)}"


def _normalize_dts(match: re.Match) -> str:
    label = "dts"
    if match.group(1):
        label += "hd"
    if match.group(2):
        label += "ma"
    if match.group(3) and match.group(4):
        label += f"{match.group(3)}.{match.group(4)}"
    return label


def _empty_fragment() -> dict:
    return {
        "season_from": None, "season_to": None,
        "episode_from": None, "episode_to": None,
        "complete_season": False,
        "quality": None, "source_type": None, "hdr": None, "video_codec": None,
        "audio": set(), "subtitle": set(), "tags": set(),
    }


def _parse_fragment(text: str) -> dict:
    prepped = _prep_edition_text(text)
    frag = _empty_fragment()

    match = _SEASON_RANGE_RE.search(prepped)
    if match:
        frag["season_from"] = int(match.group(1))
        frag["season_to"] = int(match.group(2)) if match.group(2) else int(match.group(1))
    else:
        match = _SEASON_WORD_RE.search(prepped) or _SEASON_DI_RE.search(prepped)
        if match:
            frag["season_from"] = frag["season_to"] = int(match.group(1))
        else:
            match = _SEASON_N_QUAN_RE.search(prepped)
            if match:
                n = int(match.group(1) or match.group(2))
                frag["season_from"], frag["season_to"] = 1, n
                frag["complete_season"] = True
            elif _SEASON_QUAN_RE.search(prepped):
                frag["complete_season"] = True

    match = _EPISODE_SE_RE.search(prepped)
    if match:
        frag["episode_from"] = int(match.group(1))
        frag["episode_to"] = int(match.group(2)) if match.group(2) else int(match.group(1))
    else:
        match = _EPISODE_RANGE_RE.search(prepped)
        if match:
            frag["episode_from"] = int(match.group(1))
            frag["episode_to"] = int(match.group(2))
        else:
            match = _EPISODE_DI_RE.search(prepped)
            if match:
                frag["episode_from"] = frag["episode_to"] = int(match.group(1))
            else:
                match = _EPISODE_QUAN_N_RE.search(prepped)
                if match:
                    n = int(match.group(1))
                    frag["episode_from"], frag["episode_to"] = 1, n
                    frag["complete_season"] = True
                elif _EPISODE_QUAN_RE.search(prepped):
                    frag["complete_season"] = True
    if _EPISODE_SINGLE_RE.search(prepped):
        frag["tags"].add("single")

    for rx, val in _QUALITY_RULES:
        if rx.search(prepped):
            frag["quality"] = val
            break
    else:
        if _QUALITY_OTHER_RE.search(prepped):
            frag["quality"] = "other"

    for rx, val in _SOURCE_RULES:
        if rx.search(prepped):
            frag["source_type"] = val
            break

    for rx, val in _HDR_RULES:
        if rx.search(prepped):
            frag["hdr"] = val
            break

    for rx, val in _CODEC_RULES:
        if rx.search(prepped):
            frag["video_codec"] = val
            break

    match = _DD_RE.search(prepped)
    if match:
        frag["audio"].add(_normalize_dd(match))
    match = _DTS_RE.search(prepped)
    if match:
        frag["audio"].add(_normalize_dts(match))
    if _TRUEHD_RE.search(prepped):
        frag["audio"].add("truehd")
    if _ATMOS_RE.search(prepped):
        frag["audio"].add("atmos")
    if _DOLBY_AUDIO_RE.search(prepped):
        frag["audio"].add("dolby_audio")
    if _AAC_RE.search(prepped):
        frag["audio"].add("aac")
    if _FLAC_RE.search(prepped):
        frag["audio"].add("flac")
    for literal, val in _LANGUAGE_AUDIO_MAP.items():
        if literal in prepped:
            frag["audio"].add(val)

    for rx, val in _SUBTITLE_RULES:
        if rx.search(prepped):
            frag["subtitle"].add(val)

    for rx, val in _TAG_RULES:
        if rx.search(prepped):
            frag["tags"].add(val)
    match = _DOUBAN_RE.search(prepped)
    if match:
        frag["tags"].add(f"douban:{match.group(1)}")
    for rx, val in _SRC_PLATFORM_RULES:
        if rx.search(prepped):
            frag["tags"].add(f"src:{val}")

    return frag


def _fragment_is_empty(frag: dict) -> bool:
    if frag["complete_season"] or frag["audio"] or frag["subtitle"] or frag["tags"]:
        return False
    return all(frag[field] is None for field in _EDITION_SCALAR_FIELDS)


def parse_edition(remark: str, title_cell: str, ed2k_filename: str | None = None) -> EditionInfo:
    """Parse 备注/标题/ED2K 文件名 into an :class:`EditionInfo` (§4.3).

    Each of the three sources is parsed independently, then merged: for each
    scalar field, the first source (in the priority order remark > title_cell
    > ed2k_filename) that recognised a value wins; ``complete_season`` is
    True if *any* source signalled completeness; ``audio``/``subtitle``/
    ``tags`` are the union across all sources, sorted and de-duplicated.
    ``unparsed`` is computed from the remark alone (see ``EditionInfo``
    docstring).
    """
    remark = remark or ""
    remark_frag = _parse_fragment(remark)
    fragments = [remark_frag, _parse_fragment(title_cell or "")]
    if ed2k_filename:
        fragments.append(_parse_fragment(ed2k_filename))

    merged = _empty_fragment()
    for field in _EDITION_SCALAR_FIELDS:
        for frag in fragments:
            if frag[field] is not None:
                merged[field] = frag[field]
                break
    merged["complete_season"] = any(frag["complete_season"] for frag in fragments)
    for frag in fragments:
        merged["audio"] |= frag["audio"]
        merged["subtitle"] |= frag["subtitle"]
        merged["tags"] |= frag["tags"]

    unparsed = bool(normalize_text(remark)) and _fragment_is_empty(remark_frag)

    return EditionInfo(
        season_from=merged["season_from"],
        season_to=merged["season_to"],
        episode_from=merged["episode_from"],
        episode_to=merged["episode_to"],
        complete_season=merged["complete_season"],
        quality=merged["quality"],
        source_type=merged["source_type"],
        hdr=merged["hdr"],
        video_codec=merged["video_codec"],
        audio=tuple(sorted(merged["audio"])),
        subtitle=tuple(sorted(merged["subtitle"])),
        tags=tuple(sorted(merged["tags"])),
        unparsed=unparsed,
    )


# ---------------------------------------------------------------------------
# §4.4/4.5 provider mapping and canonical links
# ---------------------------------------------------------------------------

# The exact label an "unknown"-provider link gets when its raw text is NOT a
# URL (no scheme/host to point a user at) -- library_store._link_actions
# keys off this constant (rather than the provider alone) to tell that case
# apart from an "unknown" link that DOES have a real URL of a host outside
# the provider allowlist, which still gets open/copy actions (plan §4.4).
INVALID_LINK_LABEL = "无效链接"

PROVIDERS: dict[str, str] = {
    "115": "115 网盘",
    "quark": "夸克网盘",
    "alipan": "阿里云盘",
    "baidu": "百度网盘",
    "tianyicloud": "天翼云盘",
    "guangya": "广亚",
    "139cloud": "移动云盘",
    "123": "123 云盘",
    "ed2k": "ED2K",
    "unknown": "其他",
}

HOST_PROVIDERS: dict[str, str] = {
    "115.com": "115",
    "115cdn.com": "115",
    "share.115.com": "115",
    "anxia.com": "115",
    "cloud.189.cn": "tianyicloud",
    "content.21cn.com": "tianyicloud",
    "h5.cloud.189.cn": "tianyicloud",
    "pan.quark.cn": "quark",
    "alipan.com": "alipan",
    "aliyundrive.com": "alipan",
    "alywp.net": "alipan",
    "pan.baidu.com": "baidu",
    "yun.baidu.com": "baidu",
    "guangyapan.com": "guangya",
    "caiyun.139.com": "139cloud",
    "yun.139.com": "139cloud",
    "123pan.com": "123",
    "123684.com": "123",
    "123865.com": "123",
    "123912.com": "123",
    "123pan.cn": "123",
}

_ED2K_RE = re.compile(r"^ed2k://\|file\|(?P<name>[^|]*)\|(?P<size>[^|]*)\|(?P<hash>[0-9A-Fa-f]{32})\|/?", re.I)
_MAGNET_BTIH_RE = re.compile(r"btih:([A-Za-z0-9]+)", re.I)
_MAGNET_DN_RE = re.compile(r"[?&]dn=([^&]+)", re.I)


@dataclass(frozen=True)
class LinkInfo:
    """A parsed share link, used only for deduplication (§4.5).

    ``url`` is the input, stripped, with no other rewriting.

    Two representations were left open by §4.4/4.5 and are fixed here:

    * ``magnet:`` links are classified as ``provider="ed2k"`` per §4.4.  There
      is no ``tags`` field on ``LinkInfo`` to carry the brief's "tag magnet"
      note, so that note is treated as scoped to a later stage (importer/
      runtime) and is not reproduced here.  ``canonical`` for a magnet link
      is derived from its ``xt=urn:btih:<hash>`` parameter (lower-cased),
      mirroring ed2k-hash canonicalisation; with no ``btih`` present it falls
      back to the same ``ed2k:<sha256 hex of the stripped url>`` scheme as an
      unparsable ``ed2k://`` link.  ``label`` reuses the ed2k
      ``"ED2K · <name>"`` format, using the magnet's ``dn=`` parameter (or
      the extracted hash, or the literal ``"ed2k"``) as the display name.

    * §4.4's ``tianyicloud``/``guangya``/``139cloud``/``123`` canonical forms
      embed a sorted query string; for all four, the access-code query keys
      (``password``/``pwd``/``accessCode``, case-insensitive) are dropped
      before sorting, so ``canonical`` never contains an access code passed
      as a URL query parameter -- this mirrors the "canonical never contains
      an access code" rule that already holds for every other provider.
      ``label`` never contains it either.

    * §4.5 label masking (``label`` for URL-type providers) applies to the
      *share code* embedded in ``canonical`` (not the access code, which
      never appears in ``label`` at all).  For providers whose canonical is
      ``<provider>:<path>?<query>`` rather than a single ``<code>`` token
      (tianyicloud/guangya/139cloud/123), the last non-empty path segment is
      used as the maskable "share code" (falling back to the host/provider
      name if the path is empty).  Masking: a code of 4 characters or fewer
      is replaced entirely with ``"…"`` (splitting it into "first 3 + last
      1" would reveal the whole code); a longer code renders as
      ``code[:3] + "…" + code[-1]``.
    """

    provider: str
    url: str
    canonical: str
    access_code: str | None
    label: str
    code_conflict: bool


def _strip_www(host: str) -> str:
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _path_parts(path: str) -> list[str]:
    return [unquote(part) for part in path.split("/") if part]


def _last_path_segment(path: str) -> str:
    parts = _path_parts(path)
    return parts[-1] if parts else ""


def _sorted_query_string(query: str, *, exclude_keys: frozenset = frozenset()) -> str:
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.lower() not in exclude_keys]
    pairs.sort()
    return "&".join(f"{k}={v}" for k, v in pairs)


# Query keys that carry an access code, recognised case-insensitively. Any
# canonical built from a sorted query string must drop these first, so an
# access code passed as a URL query parameter never ends up in `canonical`
# for any provider.
_ACCESS_CODE_QUERY_KEYS = frozenset({"password", "pwd", "accesscode"})


def _sorted_query_string_without_access_code(query: str) -> str:
    return _sorted_query_string(query, exclude_keys=_ACCESS_CODE_QUERY_KEYS)


def _access_code_from_url(parsed) -> str | None:
    combined: dict[str, str] = {}
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        combined.setdefault(k.lower(), v)
    for k, v in parse_qsl(parsed.fragment, keep_blank_values=True):
        combined[k.lower()] = v
    for key in ("password", "pwd", "accesscode"):
        if combined.get(key):
            return combined[key]
    return None


def _cell_access_code(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value or value.upper() == "NULL":
        return None
    return value


def _mask_share_code(code: str) -> str:
    if not code:
        return "…"
    if len(code) <= 4:
        return "…"
    return f"{code[:3]}…{code[-1]}"


def _canonical_simple_code(prefix: str, parsed) -> tuple[str, str] | None:
    parts = _path_parts(parsed.path)
    if parts and parts[0].lower() == "s":
        parts = parts[1:]
    if len(parts) != 1 or not parts[0]:
        return None
    code = parts[0]
    return f"{prefix}:{code}", code


def _canonical_alipan(parsed) -> tuple[str, str] | None:
    parts = _path_parts(parsed.path)
    if parts and parts[0].lower() == "s":
        parts = parts[1:]
    if not parts:
        return None
    return f"alipan:{'/'.join(parts)}", parts[0]


def _canonical_baidu(parsed) -> tuple[str, str] | None:
    parts = _path_parts(parsed.path)
    if parts and parts[0].lower() == "s":
        rest = parts[1:]
        if len(rest) != 1 or not rest[0]:
            return None
        segment = rest[0]
        code = segment[1:] if segment.startswith("1") and len(segment) > 1 else segment
        return f"baidu:{code}", code
    if parsed.path.rstrip("/").lower().endswith("/share/init"):
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            if k.lower() == "surl" and v:
                return f"baidu:{v}", v
    return None


def _canonical_tianyicloud(host: str, parsed) -> tuple[str, str]:
    query = _sorted_query_string_without_access_code(parsed.query)
    canonical = f"tianyicloud:{host}{parsed.path}"
    if query:
        canonical += f"?{query}"
    share_code = _last_path_segment(parsed.path) or host
    return canonical, share_code


def _canonical_query_path(provider: str, parsed) -> tuple[str, str]:
    query = _sorted_query_string_without_access_code(parsed.query)
    canonical = f"{provider}:{parsed.path}"
    if query:
        canonical += f"?{query}"
    share_code = _last_path_segment(parsed.path) or provider
    return canonical, share_code


def _canonical_unknown(raw: str) -> str:
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"unknown:{digest}"


def _canonical_ed2k(url: str) -> tuple[str, str]:
    match = _ED2K_RE.match(url)
    if match:
        return f"ed2k:{match.group('hash').lower()}", unquote(match.group("name"))[:24]
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"ed2k:{digest}", ""


def _canonical_magnet(url: str) -> tuple[str, str]:
    hash_match = _MAGNET_BTIH_RE.search(url)
    dn_match = _MAGNET_DN_RE.search(url)
    name = unquote(dn_match.group(1))[:24] if dn_match else ""
    if hash_match:
        return f"ed2k:{hash_match.group(1).lower()}", name or hash_match.group(1)[:24]
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"ed2k:{digest}", name


_PROVIDER_CANONICAL_BUILDERS = {
    "115": lambda host, parsed: _canonical_simple_code("115", parsed),
    "quark": lambda host, parsed: _canonical_simple_code("quark", parsed),
    "alipan": lambda host, parsed: _canonical_alipan(parsed),
    "baidu": lambda host, parsed: _canonical_baidu(parsed),
    "tianyicloud": lambda host, parsed: _canonical_tianyicloud(host, parsed),
    "guangya": lambda host, parsed: _canonical_query_path("guangya", parsed),
    "139cloud": lambda host, parsed: _canonical_query_path("139cloud", parsed),
    "123": lambda host, parsed: _canonical_query_path("123", parsed),
}


def _build_label(provider: str, share_code: str, host: str, *, is_url: bool) -> str:
    if provider == "ed2k":
        return f"ED2K · {share_code or 'ed2k'}"
    if provider == "unknown":
        return f"未知来源 · {host}" if is_url else INVALID_LINK_LABEL
    return f"{PROVIDERS[provider]} 分享 · {_mask_share_code(share_code)}"


def _access_code_from_magnet_query(url: str) -> str | None:
    """Same lookup as ``_access_code_from_url`` but on the raw ``magnet:``
    string's query part, found by splitting on ``"?"`` -- never through
    ``urlparse`` (see ``parse_link``'s hotfix note)."""
    query = url.split("?", 1)[1] if "?" in url else ""
    combined: dict[str, str] = {}
    for k, v in parse_qsl(query, keep_blank_values=True):
        combined.setdefault(k.lower(), v)
    for key in ("password", "pwd", "accesscode"):
        if combined.get(key):
            return combined[key]
    return None


def parse_link(raw_url: str, access_code_cell: str | None) -> LinkInfo:
    """Parse a share-link cell (plus its access-code cell) into a :class:`LinkInfo`.

    See the ``LinkInfo`` docstring for the representation choices taken for
    magnet links and for the tianyicloud/guangya/139cloud/123 canonical
    query-string handling.

    Hotfix (first real-data import run): real ED2K file names can contain
    ``[``/``]``, full-width punctuation or percent-encoded bytes that make
    ``urllib.parse.urlparse`` raise ``ValueError`` ("Invalid IPv6 URL",
    "...NFKC normalization", "...IPv4 or IPv6 address") when such a cell is
    parsed as a generic URL -- and that message can carry the file name.  So
    ``ed2k://``/``magnet:`` cells are detected by a case-insensitive prefix
    check and parsed by regex/query-splitting on the raw string, never via
    ``urlparse``; every other cell still goes through ``urlparse``, but the
    call is wrapped so a ``ValueError`` there is treated as non-URL text
    (``provider="unknown"``, label ``"无效链接"``) instead of propagating.
    """
    url = (raw_url or "").strip()
    cell_code = _cell_access_code(access_code_cell)
    lower_url = url.lower()

    provider = "unknown"
    share_code = ""
    host = ""
    url_access_code: str | None = None
    is_url = False

    if lower_url.startswith("ed2k://"):
        is_url = True
        provider = "ed2k"
        canonical, share_code = _canonical_ed2k(url)
    elif lower_url.startswith("magnet:"):
        is_url = True
        provider = "ed2k"
        canonical, share_code = _canonical_magnet(url)
        url_access_code = _access_code_from_magnet_query(url)
    else:
        try:
            parsed = urlparse(url) if url else None
        except ValueError:
            parsed = None
        scheme = (parsed.scheme or "").lower() if parsed is not None else ""

        if scheme in ("http", "https") and parsed is not None:
            is_url = True
            host = _strip_www(parsed.hostname or "")
            provider = HOST_PROVIDERS.get(host, "unknown")
            url_access_code = _access_code_from_url(parsed)
            builder = _PROVIDER_CANONICAL_BUILDERS.get(provider)
            result = builder(host, parsed) if builder else None
            if result is None:
                provider = "unknown"
                canonical = _canonical_unknown(url)
            else:
                canonical, share_code = result
        else:
            # C1 fix: only http/https/ed2k/magnet are allowlisted as
            # revealable/openable link schemes. A scheme outside that list
            # -- even a "real" one with a resolvable host, like ftp://, and
            # especially a script-executing one like javascript:/vbscript:
            # or a local-file one like file:// -- must never get the
            # "未知来源 · <host>" treatment (open/copy actions); it is
            # invalid text as far as this library is concerned.
            provider = "unknown"
            canonical = _canonical_unknown(url)

    access_code = url_access_code if url_access_code else cell_code
    code_conflict = bool(url_access_code and cell_code and url_access_code != cell_code)

    label = _build_label(provider, share_code, host, is_url=is_url)

    return LinkInfo(
        provider=provider,
        url=url,
        canonical=canonical,
        access_code=access_code,
        label=label,
        code_conflict=code_conflict,
    )


def canonical_hash(canonical: str) -> str:
    """``sha256(canonical)`` hex digest, used as the dedup key alongside provider."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def public_id(provider: str, canonical: str) -> str:
    """Stable, non-reversible public id: first 22 chars of a urlsafe-base64 sha256."""
    digest = hashlib.sha256(f"hidrive-library-v1:{provider}:{canonical}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")[:22]


# ---------------------------------------------------------------------------
# §4.6/4.7 movie/tv inference, identity and fingerprint
# ---------------------------------------------------------------------------

# The tags that participate in edition_fingerprint's "x=" segment (§4.7).
# Everything else (provider, src:*, resend/rewash/purchased/instant_only,
# douban:*) is deliberately excluded so it never splits a resource group.
_FINGERPRINT_TAGS = frozenset({"hq", "highbitrate", "60fps", "50fps"})


# TV-ish cues (§4.6): free text mentioning these, with no season/episode
# marker, is still too ambiguous to call "movie" -- stays "unknown" instead.
_TV_CUE_RE = re.compile(
    r"综艺|真人秀|脱口秀|更新|更至|连载|番剧|动漫|动画|纪录|系列|合辑|部曲|全\s*\d+\s*部",
    re.IGNORECASE,
)


def infer_media_type(edition: EditionInfo, text: str = "") -> str:
    """"tv" iff a season or episode field (or complete_season) is present;
    otherwise "unknown" if ``text`` (title cell + media title + remark,
    space-joined) matches a TV-ish cue (``_TV_CUE_RE``, case-insensitive);
    otherwise "movie" (§4.6 -- a markerless title is assumed to be a movie
    since the vast majority of markerless titles in the real data are)."""
    has_season = edition.season_from is not None or edition.season_to is not None
    has_episode = edition.episode_from is not None or edition.episode_to is not None
    if has_season or has_episode or edition.complete_season:
        return "tv"
    if _TV_CUE_RE.search(text):
        return "unknown"
    return "movie"


def media_identity(info: TitleInfo, media_type: str, tmdb: tuple[str, int] | None) -> str:
    """"tmdb:<type>:<id>" for an exact TMDB match, else "title:<search_key>:<year or 0>:<media_type>" (§4.7)."""
    if tmdb is not None:
        tmdb_type, tmdb_id = tmdb
        return f"tmdb:{tmdb_type}:{tmdb_id}"
    year = info.year if info.year is not None else 0
    return f"title:{search_key(info.title_zh)}:{year}:{media_type}"


_EMPTY_EDITION = EditionInfo(
    season_from=None, season_to=None, episode_from=None, episode_to=None,
    complete_season=False, quality=None, source_type=None, hdr=None,
    video_codec=None, audio=(), subtitle=(), tags=(), unparsed=False,
)


def edition_fingerprint(e: EditionInfo) -> str:
    """The §4.7 dedup fingerprint string for an edition.

    Provider, source-platform tags (``src:*``), ``resend``/``rewash``/
    ``purchased``/``instant_only`` and the douban rating never affect the
    fingerprint (they stay in ``tags``/link ``remark`` for display only).
    An edition with nothing recognised at all -- i.e. equal to what
    ``parse_edition("", "")`` returns -- fingerprints as the literal string
    ``"unspecified"``.
    """
    if e == _EMPTY_EDITION:
        return "unspecified"
    season = f"s{e.season_from}-{e.season_to}" if e.season_from is not None else "s?"
    episode = f"e{e.episode_from}-{e.episode_to}" if e.episode_from is not None else "e?"
    completeness = "full" if e.complete_season else "part"
    audio = "a=" + "+".join(sorted(e.audio))
    subtitle = "t=" + "+".join(sorted(e.subtitle))
    tags = "x=" + "+".join(sorted(tag for tag in e.tags if tag in _FINGERPRINT_TAGS))
    return "|".join(
        [
            season,
            episode,
            completeness,
            e.quality or "?",
            e.source_type or "?",
            e.hdr or "?",
            e.video_codec or "?",
            audio,
            subtitle,
            tags,
        ]
    )


# ---------------------------------------------------------------------------
# T17 §12.3: resource-card spec chips (resolution / dynamic range / source).
#
# ``resource_specs`` is deliberately independent of ``_QUALITY_RULES``/
# ``_HDR_RULES``/``_SOURCE_RULES`` above (those scan a whole remark/title
# blob and must not false-positive on surrounding text); its own three rule
# tables instead classify a SINGLE already-isolated quality/hdr/source_type
# value -- either this module's own parsed edition code (``"2160p"``,
# ``"dv_hdr"``, ``"webdl"``, ...) or an equivalent display-style spelling
# (``"4K"``, ``"DV+HDR10"``, ``"WEB-DL"``, ...), case- and separator-
# insensitive (NFKC-normalised first, so full-width punctuation folds to
# its half-width form before matching).
# ---------------------------------------------------------------------------

_SPEC_RESOLUTION_RULES = (
    (re.compile(r"2160p|4k|uhd", re.I), "4K"),
    (re.compile(r"1080p", re.I), "1080p"),
    (re.compile(r"720p", re.I), "720p"),
    (re.compile(r"\bsd\b", re.I), "SD"),
)

# The DV+HDR combo rule must come first: it is a strict superset of the
# bare-"dv" pattern below, and would never be reached if that one were
# tried first.
_SPEC_DYNAMIC_RANGE_RULES = (
    (re.compile(r"dv[\s_&+.-]*hdr(?:10)?\+?", re.I), "DV/HDR"),
    (re.compile(r"\bdv\b|dolby[\s_-]*vision", re.I), "Dolby Vision"),
    (re.compile(r"hdr10\+|hdr10p(?:lus)?", re.I), "HDR10+"),
    (re.compile(r"hdr10", re.I), "HDR10"),
    (re.compile(r"hlg", re.I), "HLG"),
    (re.compile(r"sdr", re.I), "SDR"),
)

_SPEC_SOURCE_RULES = (
    (re.compile(r"web[\s_.-]*rip", re.I), "WEBRip"),
    (re.compile(r"web[\s_.-]*dl", re.I), "WEB-DL"),
    (re.compile(r"blu[\s_.-]*ray", re.I), "BluRay"),
    (re.compile(r"remux", re.I), "BDRemux"),
    (re.compile(r"hdtv", re.I), "HDTV"),
    (re.compile(r"bd[\s_.-]*rip", re.I), "BDRip"),
)


def _match_spec(value: str | None, rules: tuple[tuple[re.Pattern, str], ...]) -> str | None:
    if not value:
        return None
    text = unicodedata.normalize("NFKC", value)
    for pattern, label in rules:
        if pattern.search(text):
            return label
    return None


def resource_specs(quality: str | None, hdr: str | None, source_type: str | None) -> dict:
    """Map raw ``quality``/``hdr``/``source_type`` tokens to the §12.3
    resource-card spec-chip contract:
    ``{"resolution": {"value": ..., "icon": "icon-resolution"},
    "dynamic_range": {"value": ..., "icon": "icon-dynamic-range"},
    "source": {"value": ..., "icon": "icon-source"}}``.

    Each key is present only when its input was recognised; an
    unrecognised or missing token (``None``, ``""``, or a value like
    ``"other"`` that doesn't map to any of the three known buckets)
    contributes no key at all -- never an empty/placeholder chip (§12.1:
    "解析不到某类就不占位、不显示空 chip").
    """
    specs: dict[str, dict[str, str]] = {}
    resolution = _match_spec(quality, _SPEC_RESOLUTION_RULES)
    if resolution:
        specs["resolution"] = {"value": resolution, "icon": "icon-resolution"}
    dynamic_range = _match_spec(hdr, _SPEC_DYNAMIC_RANGE_RULES)
    if dynamic_range:
        specs["dynamic_range"] = {"value": dynamic_range, "icon": "icon-dynamic-range"}
    source = _match_spec(source_type, _SPEC_SOURCE_RULES)
    if source:
        specs["source"] = {"value": source, "icon": "icon-source"}
    return specs


# ---------------------------------------------------------------------------
# T17 §14.5: ratings passthrough -- per-source, never averaged, safe
# canonical URLs built from ids alone (no query params, no keys).
# ---------------------------------------------------------------------------

_RATINGS_SOURCES = ("tmdb", "imdb", "tvmaze")
# TMDB/IMDb vote counts are always meaningful when the source has a score at
# all, so a missing/zero vote count there means the entry isn't trustworthy
# yet; TVmaze's public API commonly has no comparable count, so a TVmaze
# score is kept with ``votes: None`` rather than dropped (§14.2/§14.5).
_RATINGS_VOTES_REQUIRED = frozenset({"tmdb", "imdb"})


def _rating_url(
    source: str, media_type: str | None, tmdb_id: int | None, imdb_id: str | None, tvmaze_id: int | None,
) -> str | None:
    if source == "tmdb":
        return f"https://www.themoviedb.org/{media_type}/{tmdb_id}" if media_type in ("movie", "tv") and tmdb_id else None
    if source == "imdb":
        return f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None
    if source == "tvmaze":
        return f"https://www.tvmaze.com/shows/{tvmaze_id}" if tvmaze_id else None
    return None


def format_ratings(
    ratings_json: str | None,
    *,
    media_type: str | None,
    tmdb_id: int | None,
    imdb_id: str | None,
    tvmaze_id: int | None,
) -> dict:
    """Build the ``ratings`` passthrough dict (§14.5) from a stored
    ``media.ratings_json`` blob plus the identity ids needed for safe
    canonical URLs.

    A source is included only when its ``score`` is a positive number and
    -- for TMDB/IMDb -- its ``votes`` is also a positive number; a source
    with no valid score/votes is dropped entirely rather than shown as a
    fake ``0``/``0.0``. Sources are never averaged or merged into each
    other. A missing/empty/unparsable ``ratings_json`` yields ``{}``.
    """
    try:
        raw = json.loads(ratings_json) if ratings_json else {}
    except (TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    ratings: dict[str, dict] = {}
    for source in _RATINGS_SOURCES:
        entry = raw.get(source)
        if not isinstance(entry, dict):
            continue
        score = entry.get("score")
        if not isinstance(score, (int, float)) or score <= 0:
            continue
        votes = entry.get("votes")
        if not isinstance(votes, (int, float)):
            votes = None
        if source in _RATINGS_VOTES_REQUIRED and (votes is None or votes <= 0):
            continue
        ratings[source] = {
            "score": score,
            "votes": int(votes) if votes is not None else None,
            "url": _rating_url(source, media_type, tmdb_id, imdb_id, tvmaze_id),
        }
    return ratings


def primary_rating(ratings: dict) -> dict | None:
    """The single rating shown on a poster card (§14.4): TMDB, else IMDb,
    else ``None`` when neither source is available."""
    for source in ("tmdb", "imdb"):
        if source in ratings:
            return {"source": source, **ratings[source]}
    return None
