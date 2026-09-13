"""RE0 (ex-HDHive) resource sync: read-only projections of RE0 resources for
the library, a rate-limited client on top of the app's existing OAuth token
plumbing, and the federated-search cache.

Spec: docs/claude-re0-resource-sync-and-tv-follow-handoff-20260909.md.

Safety rules baked in here (never relaxed by callers):

* the only network entry points are ``Re0Client.get``/``post``; both count
  against the local daily cap, honour ``Retry-After`` cooldowns persisted in
  ``re0_sync_state`` and refresh the access token at most once per call;
* nothing in this module ever calls the unlock endpoint on its own -- the
  unlock POST exists only for the user-triggered action route in app.py;
* unlocked payloads (URL/access code) are kept out of every candidate/spec
  record; only ``normalize_item`` returns them, separately, for the caller
  to encrypt through ``LibraryStore`` -- they are never logged;
* slugs are stored encrypted and deduplicated by a salted SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import requests

import library_normalize

LOG = logging.getLogger("HiDrive-Lite.re0")

DEFAULT_BASE = "https://re0.me"
DEFAULT_MIN_INTERVAL_MS = 1000
DEFAULT_DAILY_CAP = 100
SAFE_MIN_INTERVAL_MS = 500
SAFE_DAILY_CAP_MAX = 1000
MAX_5XX_ATTEMPTS = 3
REQUEST_TIMEOUT = 20

# Explicit upstream pan_type -> local provider map (spec §5.2). Anything
# else -- including different casings -- is "unknown": never guessed.
PAN_TYPE_MAP = {
    "115": "115",
    "189": "tianyicloud",
    "quark": "quark",
    "aliPan": "alipan",
    "baiDu": "baidu",
    "139": "139cloud",
    "guangYa": "guangya",
    "ed2k": "ed2k",
    "magnet": "ed2k",
}

ERROR_CLASSES = (
    "missing_credentials", "reauth_required", "refresh_unavailable", "scope_denied", "user_level_denied",
    "rate_limited", "quota_unknown", "quota_exhausted", "upstream_4xx", "upstream_5xx", "network_error",
    "invalid_json", "invalid_item", "provider_unmapped", "already_unlocked_no_payload", "match_ambiguous",
    "materialize_failed", "transfer_failed", "cloud_download_failed",
)


def map_pan_type(raw: object) -> str:
    """Explicit map only -- an unlisted code is ``unknown``, never guessed.

    Round 27: RE0's numeric codes (115 / 189 / 139) may arrive as JSON
    numbers rather than strings; an integer is the same code in a different
    JSON type, so it is read through the same table. Anything else (float,
    bool, padded string) stays ``unknown``."""
    if isinstance(raw, bool):
        return "unknown"
    if isinstance(raw, int):
        raw = str(raw)
    if not isinstance(raw, str):
        return "unknown"
    return PAN_TYPE_MAP.get(raw, "unknown")


def slug_hash(slug: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}|{slug}".encode("utf-8")).hexdigest()


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v)]
    return []


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


_CONTROL_CHARS = None
REMARK_MAX_CHARS = 2000


def _clean_text(value: object, limit: int) -> str | None:
    """External free text for display: control characters removed, anything
    URL-shaped stripped (a remark must never smuggle a share link), collapsed
    whitespace, hard length cap. HTML is kept verbatim -- the renderer escapes."""
    global _CONTROL_CHARS
    if not isinstance(value, str):
        return None
    if _CONTROL_CHARS is None:
        _CONTROL_CHARS = {c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)}
        _CONTROL_CHARS[0x7F] = None
    text = strip_links(value.translate(_CONTROL_CHARS))
    text = " ".join(text.split())
    return text[:limit] or None


def _iso_utc(value: object) -> str | None:
    """RFC3339-ish upstream timestamp -> sortable UTC ISO string, or None.
    A bare number is not a timestamp we can trust, so it stays None."""
    epoch = _parse_rfc3339(value)
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat() if epoch is not None else None


def _publisher(value: object) -> dict | None:
    """Only the public nickname. Never the upstream user id, email or any
    other profile field; the avatar stays None because this project embeds no
    third-party image hosts."""
    if isinstance(value, str):
        nickname = _clean_text(value, 60)
        return {"nickname": nickname, "avatar_url": None} if nickname else None
    if isinstance(value, dict):
        for key in ("nickname", "name", "username", "display_name"):
            nickname = _clean_text(value.get(key), 60)
            if nickname:
                return {"nickname": nickname, "avatar_url": None}
    return None


def normalize_item(item: object, *, salt: str) -> dict | None:
    """Reduce one ``/api/open/resources`` item to the safe, whitelisted
    candidate record. Returns ``None`` for an invalid item (no usable slug).

    The unlocked payload (``url``/``full_url``/``access_code``), when RE0
    marks the item ``is_unlocked``, is returned under ``payload_url`` /
    ``payload_access_code`` ONLY -- callers encrypt it via the store and
    must never persist it in the candidate/spec fields or log it."""
    if not isinstance(item, dict):
        return None
    slug = item.get("slug")
    if not isinstance(slug, str) or not slug.strip() or len(slug) > 200:
        return None
    slug = slug.strip()
    raw_resolution = _as_str_list(item.get("video_resolution"))
    raw_source = _as_str_list(item.get("source"))
    edition = library_normalize.parse_edition(" ".join(raw_resolution + raw_source), "")
    unlocked = item.get("is_unlocked") is True
    payload_url = None
    payload_code = None
    if unlocked:
        for key in ("url", "full_url"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                payload_url = value.strip()
                break
        code = item.get("access_code")
        if isinstance(code, str) and code.strip():
            payload_code = code.strip()
    title = item.get("title")
    title = title.strip()[:200] if isinstance(title, str) and title.strip() else None
    share_size = item.get("share_size")
    if not isinstance(share_size, (str, int, float)) or isinstance(share_size, bool):
        share_size = None
    elif isinstance(share_size, str):
        share_size = share_size.strip()[:40] or None
    return {
        "slug": slug,
        "slug_hash": slug_hash(slug, salt),
        "provider_code": map_pan_type(item.get("pan_type")),
        "upstream_pan_type": str(item.get("pan_type")) if isinstance(item.get("pan_type"), (str, int)) and not isinstance(item.get("pan_type"), bool) else None,
        "spec": {
            "quality": edition.quality,
            "source_type": edition.source_type,
            "hdr": edition.hdr,
            "raw": {
                "video_resolution": raw_resolution,
                "source": raw_source,
                "subtitle_language": _as_str_list(item.get("subtitle_language")),
                "subtitle_type": _as_str_list(item.get("subtitle_type")),
                # Display-only upstream fields (never a link): the share's own
                # name and size, so same-pan links can be told apart, plus the
                # publisher's own words -- the only reliable way to tell a
                # 合集 from a 单集 before anything is unlocked (work order §4.1).
                "title": title,
                "share_size": share_size,
                "remark": _clean_text(item.get("remark"), REMARK_MAX_CHARS),
                "created_at": _iso_utc(item.get("created_at")),
                "publisher": _publisher(item.get("user")),
                "is_official": item.get("is_official") if isinstance(item.get("is_official"), bool) else None,
                "unlocked_users_count": _as_int(item.get("unlocked_users_count")),
                "last_validated_at": _iso_utc(item.get("last_validated_at")),
                "validate_message": _clean_text(item.get("validate_message"), 300),
            },
        },
        "unlock_points": _as_int(item.get("unlock_points")),
        "is_unlocked": unlocked,
        "validate_status": str(item.get("validate_status")) if isinstance(item.get("validate_status"), (str, int)) else None,
        "title": title,
        "payload_url": payload_url,
        "payload_access_code": payload_code,
    }


# ---------------------------------------------------------------------------
# Composition (work order §4.3): what is actually inside a share -- a whole
# season, a collection, one episode, a range -- read deterministically from the
# publisher's own words first (remark, then title/share_title) and only then
# from file names. Nothing here calls RE0 or touches the database; it never
# claims more than the text supports, and says "构成未说明" when it cannot tell.
# ---------------------------------------------------------------------------

COMPOSITION_UNKNOWN_DISPLAY = "构成未说明"
_MAX_SEASON = 99
_MAX_EPISODE = 9999
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# A range dash in any of the forms publishers actually use.
_DASH = r"[-–—~～]"
_SEASON_RE = None


def _cn_number(text: str) -> int | None:
    """Small Chinese numeral (一 to 九十九) -- enough for a season number."""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        if tens and 0 <= ones <= 9:
            return tens * 10 + ones
    return None


def _compile_composition_patterns() -> dict:
    import re as _re
    return {
        # S03 / s01-S03 / S01E01-E10
        "season_range": _re.compile(rf"\bs(\d{{1,2}})\s*{_DASH}\s*s?(\d{{1,2}})\b", _re.I),
        "season": _re.compile(r"\bs(\d{1,2})(?![\d])", _re.I),
        "season_cn": _re.compile(r"第\s*([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*季"),
        "ep_range": _re.compile(rf"\be?p?(\d{{1,4}})\s*{_DASH}\s*e?p?(\d{{1,4}})\b", _re.I),
        "ep_single": _re.compile(r"\bep?(\d{1,4})(?![\d])", _re.I),
        "ep_cn_range": _re.compile(rf"第\s*(\d{{1,4}})\s*{_DASH}\s*(\d{{1,4}})\s*集"),
        "ep_cn_single": _re.compile(r"第\s*(\d{1,4})\s*集"),
        "ep_count": _re.compile(r"(?:全|共)?\s*(\d{1,4})\s*集"),
        "updated_to": _re.compile(r"更新至"),
        "file_ep": _re.compile(r"s(\d{1,2})e(\d{1,4})", _re.I),
        "file_ep_bare": _re.compile(r"(?:^|[^\d])e(?:p)?(\d{1,4})(?![\d])", _re.I),
    }


def _composition_patterns() -> dict:
    global _SEASON_RE
    if _SEASON_RE is None:
        _SEASON_RE = _compile_composition_patterns()
    return _SEASON_RE


def _valid_season(n) -> bool:
    return isinstance(n, int) and 1 <= n <= _MAX_SEASON


def _valid_episode(n) -> bool:
    return isinstance(n, int) and 1 <= n <= _MAX_EPISODE


def _declared_seasons(text: str) -> list[int]:
    pat = _composition_patterns()
    match = pat["season_range"].search(text)
    if match:
        lo, hi = int(match.group(1)), int(match.group(2))
        if _valid_season(lo) and _valid_season(hi) and lo <= hi:
            return list(range(lo, hi + 1))
    seasons = []
    for raw in pat["season"].findall(text):
        n = int(raw)
        if _valid_season(n) and n not in seasons:
            seasons.append(n)
    for raw in pat["season_cn"].findall(text):
        n = _cn_number(raw)
        if _valid_season(n) and n not in seasons:
            seasons.append(n)
    return sorted(seasons)


def _declared_episodes(text: str) -> tuple[int | None, int | None, int | None]:
    """``(start, end, count)`` -- a bare "24集" is a count, never a range."""
    pat = _composition_patterns()
    for key in ("ep_cn_range",):
        match = pat[key].search(text)
        if match:
            lo, hi = int(match.group(1)), int(match.group(2))
            if _valid_episode(lo) and _valid_episode(hi) and lo <= hi:
                return lo, hi, hi - lo + 1
    # "E01-E10" / "S01E01-E10": only when at least one side carries the E marker.
    import re as _re
    match = _re.search(rf"e(\d{{1,4}})\s*{_DASH}\s*e?(\d{{1,4}})", text, _re.I)
    if match:
        lo, hi = int(match.group(1)), int(match.group(2))
        if _valid_episode(lo) and _valid_episode(hi) and lo <= hi:
            return lo, hi, hi - lo + 1
    # "S01E01": the E is glued to the season number, so no word boundary here.
    match = pat["file_ep"].search(text)
    if match:
        n = int(match.group(2))
        if _valid_episode(n):
            return n, n, 1
    match = pat["ep_cn_single"].search(text)
    if match:
        n = int(match.group(1))
        if _valid_episode(n):
            return n, n, 1
    match = pat["ep_single"].search(text)
    if match:
        n = int(match.group(1))
        if _valid_episode(n):
            return n, n, 1
    match = pat["ep_count"].search(text)
    if match:
        n = int(match.group(1))
        if _valid_episode(n):
            return None, None, n
    return None, None, None


def _declared_completion(text: str) -> str:
    # 完结/全集 win over 首更/更新中 -- "24集首更完结" is a finished run.
    if "完结" in text or "全集" in text or "已完结" in text:
        return "complete"
    if "更新中" in text or "连载" in text or "首更" in text or _composition_patterns()["updated_to"].search(text):
        return "updating"
    return "unknown"


_VIDEO_EXTENSIONS = (".mkv", ".mp4", ".ts", ".avi", ".m2ts", ".iso", ".rmvb", ".wmv", ".mov", ".flv")


def _file_episodes(file_names: list) -> tuple[list[int], list[int]]:
    """Season and episode numbers read off video file names."""
    pat = _composition_patterns()
    seasons, episodes = [], []
    for name in file_names or []:
        if not isinstance(name, str) or not name.strip():
            continue
        text = name.strip()
        if not text.lower().endswith(_VIDEO_EXTENSIONS):
            continue
        match = pat["file_ep"].search(text)
        if match:
            season, episode = int(match.group(1)), int(match.group(2))
            if _valid_season(season) and season not in seasons:
                seasons.append(season)
            if _valid_episode(episode) and episode not in episodes:
                episodes.append(episode)
            continue
        bare = pat["file_ep_bare"].search(text)
        if bare:
            episode = int(bare.group(1))
            if _valid_episode(episode) and episode not in episodes:
                episodes.append(episode)
    return sorted(seasons), sorted(episodes)


def _composition_display(seasons: list, start, end, count, kind: str) -> str:
    parts = []
    if seasons:
        parts.append(f"S{seasons[0]:02d}" if len(seasons) == 1 else f"S{seasons[0]:02d}-S{seasons[-1]:02d}")
    if kind == "collection" and not seasons:
        parts.append("合集")
    elif start is not None and end is not None:
        parts.append(f"第 {start} 集" if start == end else f"第 {start}–{end} 集")
    elif count is not None:
        parts.append(f"{count}集")
    return " · ".join(parts) if parts else COMPOSITION_UNKNOWN_DISPLAY


def parse_composition(*, remark=None, title=None, share_title=None, file_names=None, file_count=None) -> dict:
    """Work order §4.3. The publisher's own words are authoritative
    (``declared``); file names only fill what the words left out
    (``file_inferred``); ``file_count`` alone never asserts anything."""
    declared_text = ""
    for source in (remark, title, share_title):
        if isinstance(source, str) and source.strip():
            declared_text = source.strip()
            break
    seasons = _declared_seasons(declared_text) if declared_text else []
    start, end, count = _declared_episodes(declared_text) if declared_text else (None, None, None)
    completion = _declared_completion(declared_text) if declared_text else "unknown"
    is_collection = bool(declared_text) and ("合集" in declared_text or "全集" in declared_text)
    confidence = "declared" if (seasons or start is not None or count is not None or completion != "unknown" or is_collection) else "unknown"

    if start is None and count is None:
        file_seasons, file_episodes = _file_episodes(list(file_names or []))
        if file_episodes:
            start, end = file_episodes[0], file_episodes[-1]
            count = len(file_episodes)
            if not seasons:
                seasons = file_seasons
            # §4.3 rule 2: a range read off file names is file_inferred even
            # when the season came from the publisher's text.
            confidence = "file_inferred"

    if start is not None and end is None:
        end = start
    kind = "unknown"
    if start is not None and end is not None:
        kind = "episode_single" if start == end else "episode_range"
        if seasons and completion == "complete":
            kind = "season_complete"
    elif count is not None and seasons:
        kind = "season_complete" if completion == "complete" else "season_partial"
    elif seasons:
        # The season is known, its completeness is not -- say the season, claim nothing more.
        kind = "season_complete" if completion == "complete" else ("season_partial" if completion == "updating" else "unknown")
    elif is_collection:
        kind = "collection"
    if kind in ("episode_single", "episode_range") and completion == "updating" and seasons:
        kind = "season_partial"
    return {
        "kind": kind, "season_numbers": seasons, "episode_start": start, "episode_end": end,
        "episode_count": count, "completion": completion,
        "confidence": confidence if kind != "unknown" or seasons or completion != "unknown" else "unknown",
        "display": _composition_display(seasons, start, end, count, kind),
    }


# ---------------------------------------------------------------------------
# Tables (library index DB). Idempotent; existing rows are never touched and
# a projection table created by an earlier version only gains columns.
# ---------------------------------------------------------------------------

_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS re0_sync_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS re0_sync_run (
  id INTEGER PRIMARY KEY,
  phase TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  requested INTEGER NOT NULL DEFAULT 0,
  succeeded INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  unlocked INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  error_class TEXT
);
CREATE TABLE IF NOT EXISTS re0_search_cache (
  cache_key TEXT PRIMARY KEY,
  query_hash TEXT NOT NULL,
  media_type TEXT NOT NULL,
  filter_json TEXT NOT NULL DEFAULT '{}',
  candidate_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  fetched_at INTEGER,
  expires_at INTEGER,
  retry_after_until INTEGER,
  error_class TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS re0_media_projection (
  media_type TEXT NOT NULL CHECK (media_type IN ('movie','tv')),
  tmdb_id INTEGER NOT NULL,
  local_media_id INTEGER,
  title TEXT NOT NULL,
  original_title TEXT,
  year INTEGER,
  overview TEXT,
  poster_path TEXT,
  backdrop_path TEXT,
  ratings_json TEXT NOT NULL DEFAULT '{}',
  metadata_status TEXT NOT NULL DEFAULT 'pending',
  ratings_status TEXT NOT NULL DEFAULT 'pending',
  metadata_fetched_at INTEGER,
  ratings_fetched_at INTEGER,
  last_seen_at INTEGER NOT NULL,
  last_fetched_at INTEGER,
  last_error_class TEXT,
  next_retry_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (media_type, tmdb_id)
);
CREATE TABLE IF NOT EXISTS re0_resource (
  id INTEGER PRIMARY KEY,
  slug_ciphertext BLOB NOT NULL,
  slug_hash TEXT NOT NULL UNIQUE,
  media_type TEXT NOT NULL CHECK (media_type IN ('movie','tv')),
  tmdb_id INTEGER NOT NULL,
  media_id INTEGER,
  group_id INTEGER,
  media_title TEXT,
  provider_code TEXT NOT NULL,
  upstream_pan_type TEXT,
  spec_json TEXT NOT NULL DEFAULT '{}',
  unlock_points INTEGER,
  upstream_is_unlocked INTEGER NOT NULL DEFAULT 0,
  upstream_validate_status TEXT,
  state TEXT NOT NULL DEFAULT 'candidate',
  resource_url_ciphertext BLOB,
  last_seen_at INTEGER NOT NULL,
  unlocked_at INTEGER,
  last_error_class TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_re0_resource_media ON re0_resource(media_id, provider_code, state);
CREATE INDEX IF NOT EXISTS idx_re0_resource_tmdb ON re0_resource(media_type, tmdb_id);
CREATE TABLE IF NOT EXISTS re0_resource_link (
  re0_resource_id INTEGER NOT NULL,
  resource_link_id INTEGER NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('same_local','materialized_unlocked')),
  linked_at INTEGER NOT NULL,
  PRIMARY KEY (re0_resource_id, resource_link_id)
);
CREATE TABLE IF NOT EXISTS re0_file_preview (
  re0_resource_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  file_count INTEGER,
  files_json TEXT NOT NULL DEFAULT '[]',
  composition_json TEXT NOT NULL DEFAULT '{}',
  validate_status TEXT,
  validate_message TEXT,
  truncated INTEGER NOT NULL DEFAULT 0,
  fetched_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  error_class TEXT
);
CREATE TABLE IF NOT EXISTS re0_action (
  request_id TEXT NOT NULL,
  re0_resource_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  result_code TEXT,
  resource_link_id INTEGER,
  already_owned INTEGER NOT NULL DEFAULT 0,
  unlock_points INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (request_id, re0_resource_id)
);

-- Multi-user plan §10.2: one lease per RE0 resource, so two people clicking
-- the same share at the same moment produce one upstream unlock and one
-- charge -- not two. Per-request idempotency cannot cover this: different
-- users legitimately send different request ids for the same resource.
-- A row with an expired `expires_at` is free to take, which is what keeps a
-- crashed holder from blocking the resource forever.
CREATE TABLE IF NOT EXISTS re0_unlock_lease (
  lease_key TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
"""

# How long one unlock may hold its resource. Comfortably longer than an
# unlock round trip, short enough that a worker killed mid-flight frees it
# within one retry.
UNLOCK_LEASE_SECONDS = 60

_PROJECTION_COLUMNS = {
    "local_media_id": "INTEGER", "original_title": "TEXT", "year": "INTEGER", "overview": "TEXT",
    "poster_path": "TEXT", "backdrop_path": "TEXT", "ratings_json": "TEXT NOT NULL DEFAULT '{}'",
    "metadata_status": "TEXT NOT NULL DEFAULT 'pending'", "ratings_status": "TEXT NOT NULL DEFAULT 'pending'",
    "metadata_fetched_at": "INTEGER", "ratings_fetched_at": "INTEGER", "last_fetched_at": "INTEGER",
    "last_error_class": "TEXT", "next_retry_at": "INTEGER",
}


# Review G03.3: where a confirmed-but-not-yet-saved unlock lives. Both are
# nullable and additive, and the cipher column is cleared as soon as the
# payload is materialised, so it holds a link only for the moments between
# "RE0 says it is yours" and "it is in the library".
_RESOURCE_COLUMNS = {
    "pending_payload_cipher": "BLOB",
    "pending_saved_at": "INTEGER",
}


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_TABLES_SQL)
    _ensure_calendar_table(conn)
    _ensure_follow_tables(conn)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(re0_media_projection)")}
    for column, decl in _PROJECTION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE re0_media_projection ADD COLUMN {column} {decl}")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(re0_resource)")}
    for column, decl in _RESOURCE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE re0_resource ADD COLUMN {column} {decl}")
    conn.commit()


# ---------------------------------------------------------------------------
# What happened to a consuming request (review G03.3)
# ---------------------------------------------------------------------------

STATE_PENDING_SAVE = "unlocked_pending_save"
STATE_RESULT_UNKNOWN = "unlock_result_unknown"
# The upstream outcomes that say nothing about whether the purchase happened.
UNCERTAIN_ERROR_CLASSES = frozenset({"network_error", "invalid_json", "upstream_5xx"})


def remember_unlock_pending(store, resource_id: int, *, url: str, access_code: str | None,
                            points, already_owned: bool, now: int) -> None:
    """Record "RE0 confirmed this is ours" *before* trying to save it locally.

    G03.3: the save can fail -- a bad URL, a database error -- and the points
    are already spent by then. Without this the next click unlocked again. The
    payload is encrypted with the library's own key and is deleted the moment
    it is materialised.
    """
    payload = json.dumps({"url": url, "access_code": access_code or "",
                          "points": points, "already_owned": bool(already_owned)}, ensure_ascii=False)
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE re0_resource SET state=?, pending_payload_cipher=?, pending_saved_at=?, "
            "last_error_class=NULL, updated_at=? WHERE id=?",
            (STATE_PENDING_SAVE, store.fernet.encrypt(payload.encode("utf-8")), now, now, resource_id),
        )
        conn.commit()
    finally:
        conn.close()


def pending_unlock(store, resource_id: int) -> dict | None:
    """The confirmed payload waiting to be saved, or None."""
    conn = store.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT state, pending_payload_cipher FROM re0_resource WHERE id=?", (resource_id,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    if row is None or row["pending_payload_cipher"] is None:
        return None
    try:
        data = json.loads(store.fernet.decrypt(bytes(row["pending_payload_cipher"])).decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable payload is simply absent
        return None
    if not isinstance(data, dict) or not data.get("url"):
        return None
    return {"url": data["url"], "access_code": data.get("access_code") or None,
            "points": data.get("points"), "already_owned": bool(data.get("already_owned"))}


def clear_unlock_pending(store, resource_id: int, *, now: int) -> None:
    conn = store.connect()
    try:
        conn.execute("UPDATE re0_resource SET pending_payload_cipher=NULL, pending_saved_at=NULL, "
                     "updated_at=? WHERE id=?", (now, resource_id))
        conn.commit()
    finally:
        conn.close()


def remember_unlock_unknown(store, resource_id: int, *, error_class: str | None, now: int) -> None:
    """Record that a consuming request's outcome is not known.

    G03.3: the next click must not send another one. There is no documented
    read-only endpoint in `docs/re0-openapi-reference-20260909.md` that reports
    a single resource's unlock state, so this is as far as the local record can
    go -- the user is told it is unconfirmed rather than charged again.
    """
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE re0_resource SET state=?, last_error_class=?, updated_at=? WHERE id=?",
            (STATE_RESULT_UNKNOWN, (error_class or "unknown")[:64], now, resource_id),
        )
        conn.commit()
    finally:
        conn.close()


def remember_pack_unlock_unknown(conn: sqlite3.Connection, slug_hash_value: str, *,
                                 error_class: str | None, now: int) -> None:
    """A pack whose unlock outcome is not known (G03.5).

    Same rule as a resource: the next click must ask the user, not RE0.
    """
    _ensure_follow_tables(conn)
    conn.execute("UPDATE re0_tv_follow_pack SET unlock_state=?, items_status=?, updated_at=? WHERE slug_hash=?",
                 (STATE_RESULT_UNKNOWN, (error_class or "unknown")[:64], now, slug_hash_value))


def clear_pack_unlock_state(conn: sqlite3.Connection, slug_hash_value: str, *, now: int) -> None:
    conn.execute("UPDATE re0_tv_follow_pack SET unlock_state=NULL, updated_at=? WHERE slug_hash=?",
                 (now, slug_hash_value))


def pack_unlock_state(row) -> str | None:
    keys = row.keys() if hasattr(row, "keys") else (row or {})
    return row["unlock_state"] if "unlock_state" in keys else None


def resource_state(store, resource_id: int) -> str | None:
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT state FROM re0_resource WHERE id=?", (resource_id,)).fetchone()
    finally:
        conn.close()
    return row["state"] if row is not None else None


def state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM re0_sync_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def state_set(conn: sqlite3.Connection, key: str, value: str, now: int) -> None:
    conn.execute(
        "INSERT INTO re0_sync_state(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now),
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class Re0Result:
    ok: bool
    status: int
    data: object = None
    error_class: str | None = None
    code: str | None = None
    message: str | None = None
    retry_after: int | None = None
    retryable: bool = False
    headers: dict = field(default_factory=dict)


_LINK_TOKEN_RE = None


def strip_links(text: object) -> str:
    """Any upstream free text, minus anything URL-shaped. No length cap: the
    caller decides how much of the cleaned text it keeps."""
    global _LINK_TOKEN_RE
    if not text:
        return ""
    if _LINK_TOKEN_RE is None:
        import re
        _LINK_TOKEN_RE = re.compile(r"(?:ed2k://|magnet:|https?://|ftp://|urn:btih:)\S*", re.I)
    import re
    return re.sub(r"\s{2,}", " ", _LINK_TOKEN_RE.sub("", str(text))).strip()


def sanitize_message(text: object) -> str:
    """Upstream ``message`` for the user -- link-stripped and short."""
    return strip_links(text)[:200]


def _today_key(now: float) -> str:
    return "requests:" + datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()


class Re0Client:
    """One-concurrency, paced, budgeted RE0 open-API client.

    ``request`` is ``requests.request``-shaped (patched in tests);
    ``api_key_provider``/``token_provider``/``refresh_token`` are the app's
    existing credential helpers (never copied here); ``conn_factory``
    opens the library index for ``re0_sync_state`` (budget + cooldown)."""

    def __init__(
        self,
        *,
        request: Callable = requests.request,
        api_key_provider: Callable[[], str | None],
        token_provider: Callable[[], str | None],
        refresh_token: Callable[[], str | None],
        conn_factory: Callable[[], sqlite3.Connection],
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_ms: int = DEFAULT_MIN_INTERVAL_MS,
        daily_cap: int = DEFAULT_DAILY_CAP,
        base: str = DEFAULT_BASE,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self._request = request
        self._api_key_provider = api_key_provider
        self._token_provider = token_provider
        self._refresh_token = refresh_token
        self._conn_factory = conn_factory
        self._now = now
        self._sleep = sleep
        self._min_interval = max(int(min_interval_ms), SAFE_MIN_INTERVAL_MS) / 1000.0
        self._daily_cap = max(1, min(int(daily_cap), SAFE_DAILY_CAP_MAX))
        self._base = base.rstrip("/")
        self._timeout = timeout
        self._last_request_at: float | None = None
        self._blocked: tuple[str, str | None] | None = None  # (error_class, token it was seen with)

    # -- budget / cooldown -------------------------------------------------

    def _server_cap(self, conn: sqlite3.Connection) -> tuple[int | None, int | None]:
        """(server_remaining, derived cap): a conservative half of the
        server's own remaining count, when RE0 reports one (spec §4.1)."""
        raw = state_get(conn, "server_remaining")
        if raw is None or raw == "":
            return None, None
        try:
            remaining = int(raw)
        except ValueError:
            return None, None
        return remaining, max(1, remaining // 2)

    def apply_server_quota(self, remaining: int | None) -> None:
        conn = self._conn_factory()
        try:
            state_set(conn, "server_remaining", "" if remaining is None else str(int(remaining)), int(self._now()))
            conn.commit()
        finally:
            conn.close()

    def budget_status(self) -> dict:
        conn = self._conn_factory()
        try:
            used = int(state_get(conn, _today_key(self._now())) or 0)
            cooldown = int(state_get(conn, "cooldown_until") or 0)
            server_remaining, server_cap = self._server_cap(conn)
        finally:
            conn.close()
        effective = min(self._daily_cap, server_cap) if server_cap is not None else self._daily_cap
        return {"used_today": used, "daily_cap": self._daily_cap, "effective_cap": effective, "server_remaining": server_remaining,
                "cooldown_until": cooldown or None}

    def _cooldown_remaining(self) -> int:
        conn = self._conn_factory()
        try:
            until = int(state_get(conn, "cooldown_until") or 0)
        finally:
            conn.close()
        remaining = until - int(self._now())
        return remaining if remaining > 0 else 0

    def _count_request(self) -> bool:
        """Reserve one request against today's cap; False when exhausted."""
        conn = self._conn_factory()
        try:
            key = _today_key(self._now())
            used = int(state_get(conn, key) or 0)
            _, server_cap = self._server_cap(conn)
            cap = min(self._daily_cap, server_cap) if server_cap is not None else self._daily_cap
            if used >= cap:
                return False
            state_set(conn, key, str(used + 1), int(self._now()))
            conn.commit()
            return True
        finally:
            conn.close()

    def _set_cooldown(self, seconds: int) -> None:
        conn = self._conn_factory()
        try:
            state_set(conn, "cooldown_until", str(int(self._now()) + seconds), int(self._now()))
            conn.commit()
        finally:
            conn.close()

    def _pace(self) -> None:
        if self._last_request_at is not None:
            elapsed = self._now() - self._last_request_at
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request_at = self._now()

    # -- requests ------------------------------------------------------------

    def get(self, path: str, params: dict | None = None) -> Re0Result:
        return self._call("GET", path, params=params, json=None)

    def post(self, path: str, json: dict | None = None, *, consuming: bool = False) -> Re0Result:
        """``consuming=True`` marks a request that may spend points.

        Review G03.1: the 5xx backoff did not distinguish a read from a
        purchase, so one unlock call reached the transport three times. A 5xx,
        a timeout or an unparseable body says nothing about whether the
        purchase happened, and RE0 documents no idempotency key for unlock
        (our ``request_id`` is local and is not sent upstream), so a consuming
        request is never automatically repeated.
        """
        return self._call("POST", path, params=None, json=json, consuming=consuming)

    def _call(self, method: str, path: str, *, params, json, consuming: bool = False) -> Re0Result:
        api_key = self._api_key_provider() or ""
        if not api_key:
            return Re0Result(False, 0, error_class="missing_credentials", message="RE0 应用 Secret 未配置")
        token = self._token_provider()
        if not token:
            return Re0Result(False, 401, error_class="reauth_required", message="请先完成 RE0 OAuth 授权")
        if self._blocked and self._blocked[1] == token:
            return Re0Result(False, 403 if self._blocked[0] != "reauth_required" else 401, error_class=self._blocked[0],
                             message="RE0 请求已被阻断，需重新授权或权限不足")
        remaining = self._cooldown_remaining()
        if remaining:
            return Re0Result(False, 429, error_class="rate_limited", retry_after=remaining, retryable=True,
                             message=f"RE0 限流冷却中，{remaining} 秒后再试")
        if not self._count_request():
            return Re0Result(False, 0, error_class="quota_exhausted", message="今日 RE0 请求额度已用完")

        refreshed = False
        attempts_5xx = 0
        while True:
            self._pace()
            headers = {"X-API-Key": api_key, "Authorization": "Bearer " + token, "Accept": "application/json"}
            try:
                response = self._request(method, self._base + path, headers=headers, params=params, json=json, timeout=self._timeout)
            except requests.RequestException as exc:
                # G03.1: for a consuming request this is "result unknown", not
                # "did not happen" -- `retryable` stays False so no caller
                # treats it as safe to repeat.
                return Re0Result(False, 0, error_class="network_error", retryable=not consuming,
                                 message=f"RE0 请求失败：{type(exc).__name__}")
            status = int(getattr(response, "status_code", 0) or 0)
            resp_headers = dict(getattr(response, "headers", {}) or {})
            try:
                payload = response.json() if getattr(response, "content", b"") else {}
            except (ValueError, TypeError):
                return Re0Result(False, status, error_class="invalid_json", retryable=not consuming,
                                 message="RE0 返回了无法解析的响应", headers=resp_headers)
            if not isinstance(payload, dict):
                return Re0Result(False, status, error_class="invalid_json", retryable=not consuming,
                                 message="RE0 返回格式异常", headers=resp_headers)
            code = str(payload.get("code") or "") or None
            message = sanitize_message(payload.get("message"))

            if status == 429:
                retry_after = _as_int(resp_headers.get("Retry-After")) or 60
                self._set_cooldown(retry_after)
                return Re0Result(False, status, error_class="rate_limited", code=code, retry_after=retry_after, retryable=True,
                                 message=f"RE0 限流，{retry_after} 秒后再试", headers=resp_headers)
            if status == 401 or code == "OPENAPI_REFRESH_REQUIRED":
                if not refreshed and code != "OPENAPI_REAUTH_REQUIRED":
                    refreshed = True
                    new_token = self._refresh_token()
                    if new_token:
                        token = new_token
                        continue
                self._blocked = ("reauth_required", token)
                return Re0Result(False, 401, error_class="reauth_required", code=code, message="RE0 授权已失效，请重新授权", headers=resp_headers)
            if status == 403:
                error_class = "user_level_denied" if "LEVEL" in (code or "").upper() else "scope_denied"
                self._blocked = (error_class, token)
                return Re0Result(False, status, error_class=error_class, code=code, message=message or "RE0 权限不足", headers=resp_headers)
            if status >= 500:
                # G03.1: a read may be retried with backoff; a purchase may
                # not. A 5xx after a consuming POST leaves the outcome unknown,
                # and repeating it could buy the same thing twice.
                attempts_5xx += 1
                if not consuming and attempts_5xx < MAX_5XX_ATTEMPTS:
                    self._sleep(float(2 ** (attempts_5xx - 1)))
                    continue
                return Re0Result(False, status, error_class="upstream_5xx", code=code,
                                 retryable=not consuming,
                                 message=message or "RE0 服务暂时不可用", headers=resp_headers)
            if status >= 400 or not payload.get("success", True):
                return Re0Result(False, status if status >= 400 else 400, error_class="upstream_4xx", code=code, message=message or "RE0 拒绝了请求", headers=resp_headers)
            return Re0Result(True, status, data=payload.get("data"), code=code, message=message, headers=resp_headers)


# ---------------------------------------------------------------------------
# Projections and candidates (library index DB via LibraryStore)
# ---------------------------------------------------------------------------


def _metadata_status(title, overview, poster_path, backdrop_path) -> str:
    if title and overview and poster_path and backdrop_path:
        return "partial"  # search-time data is never "complete": the enricher promotes it
    return "partial" if any((overview, poster_path, backdrop_path)) else "pending"


def upsert_projection(
    store, media_type: str, tmdb_id: int, *, title: str, year: int | None, overview: str | None,
    poster_path: str | None, backdrop_path: str | None, ratings: dict | None, now: int,
    local_media_id: int | None = None, original_title: str | None = None, metadata_status: str | None = None,
    ratings_status: str | None = None,
) -> None:
    """One row per ``(media_type, tmdb_id)``; non-empty new values win,
    empty ones never erase what an earlier pass stored."""
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM re0_media_projection WHERE media_type=? AND tmdb_id=?", (media_type, tmdb_id)
        ).fetchone()
        merged_ratings = {}
        if row is not None:
            try:
                merged_ratings = json.loads(row["ratings_json"] or "{}")
            except ValueError:
                merged_ratings = {}
        for key, value in (ratings or {}).items():
            if value:
                merged_ratings[key] = value
        if row is None:
            values = {
                "title": title, "original_title": original_title, "year": year, "overview": overview or None,
                "poster_path": poster_path or None, "backdrop_path": backdrop_path or None,
                "local_media_id": local_media_id,
            }
        else:
            values = {
                "title": title or row["title"], "original_title": original_title or row["original_title"],
                "year": year if year is not None else row["year"], "overview": overview or row["overview"],
                "poster_path": poster_path or row["poster_path"], "backdrop_path": backdrop_path or row["backdrop_path"],
                "local_media_id": local_media_id if local_media_id is not None else row["local_media_id"],
            }
        status = metadata_status or (
            row["metadata_status"] if row is not None and row["metadata_status"] == "complete"
            else _metadata_status(values["title"], values["overview"], values["poster_path"], values["backdrop_path"])
        )
        rstatus = ratings_status or (row["ratings_status"] if row is not None else ("partial" if merged_ratings else "pending"))
        if row is None:
            conn.execute(
                "INSERT INTO re0_media_projection(media_type, tmdb_id, local_media_id, title, original_title, year, overview, "
                "poster_path, backdrop_path, ratings_json, metadata_status, ratings_status, last_seen_at, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (media_type, tmdb_id, values["local_media_id"], values["title"], values["original_title"], values["year"],
                 values["overview"], values["poster_path"], values["backdrop_path"], json.dumps(merged_ratings, ensure_ascii=False),
                 status, rstatus, now, now, now),
            )
        else:
            conn.execute(
                "UPDATE re0_media_projection SET local_media_id=?, title=?, original_title=?, year=?, overview=?, poster_path=?, "
                "backdrop_path=?, ratings_json=?, metadata_status=?, ratings_status=?, last_seen_at=?, updated_at=? "
                "WHERE media_type=? AND tmdb_id=?",
                (values["local_media_id"], values["title"], values["original_title"], values["year"], values["overview"],
                 values["poster_path"], values["backdrop_path"], json.dumps(merged_ratings, ensure_ascii=False), status, rstatus,
                 now, now, media_type, tmdb_id),
            )
        conn.commit()
    finally:
        conn.close()


def upsert_resource(
    store, media_type: str, tmdb_id: int, item: dict, *, media_id: int | None, media_title: str | None, now: int,
    error_class: str | None = None,
) -> tuple[int, bool]:
    """Idempotent per ``slug_hash``: a re-seen candidate only refreshes its
    safe spec fields and ``last_seen_at``; an unlocked/linked state is never
    downgraded back to ``candidate``. Never stores a URL."""
    spec_json = json.dumps(item["spec"], ensure_ascii=False)
    conn = store.connect()
    try:
        row = conn.execute("SELECT id, state FROM re0_resource WHERE slug_hash=?", (item["slug_hash"],)).fetchone()
        if item["provider_code"] == "unknown" and error_class is None:
            error_class = "provider_unmapped"
        if row is None:
            state = "already_unlocked" if item["is_unlocked"] else "candidate"
            cur = conn.execute(
                "INSERT INTO re0_resource(slug_ciphertext, slug_hash, media_type, tmdb_id, media_id, group_id, media_title, provider_code, "
                "upstream_pan_type, spec_json, unlock_points, upstream_is_unlocked, upstream_validate_status, state, last_seen_at, "
                "last_error_class, created_at, updated_at) VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
                (store.fernet.encrypt(item["slug"].encode("utf-8")), item["slug_hash"], media_type, tmdb_id, media_id, media_title,
                 item["provider_code"], item["upstream_pan_type"], spec_json, item["unlock_points"], int(item["is_unlocked"]),
                 item["validate_status"], state, now, error_class, now, now),
            )
            conn.commit()
            return int(cur.lastrowid), True
        state = row["state"]
        if state == "candidate" and item["is_unlocked"]:
            state = "already_unlocked"
        conn.execute(
            "UPDATE re0_resource SET media_id=COALESCE(?, media_id), media_title=COALESCE(?, media_title), spec_json=?, unlock_points=?, "
            "upstream_is_unlocked=?, upstream_validate_status=?, state=?, last_seen_at=?, last_error_class=?, updated_at=? WHERE id=?",
            (media_id, media_title, spec_json, item["unlock_points"], int(item["is_unlocked"]), item["validate_status"], state, now,
             error_class, now, row["id"]),
        )
        conn.commit()
        return int(row["id"]), False
    finally:
        conn.close()


def _group_for_spec(store, conn, media_id: int, spec: dict, title: str | None, now: int) -> int:
    quality, source_type, hdr = spec.get("quality"), spec.get("source_type"), spec.get("hdr")
    fingerprint = f"re0:{quality or '-'}:{source_type or '-'}:{hdr or '-'}"
    row = conn.execute("SELECT id FROM resource_group WHERE media_id=? AND edition_fingerprint=?", (media_id, fingerprint)).fetchone()
    if row is not None:
        return int(row["id"])
    specs = library_normalize.resource_specs(quality, hdr, source_type)
    labels = [specs[k]["value"] for k in ("resolution", "dynamic_range", "source") if k in specs]
    import library_store  # local import to avoid a module cycle
    return store.upsert_group(library_store.GroupRecord(
        media_id=media_id, edition_fingerprint=fingerprint, display_title=" · ".join(labels) or (title or "RE0 资源"),
        quality=quality, source_type=source_type, hdr=hdr, tags_json=json.dumps(["re0"]), created_at=now, updated_at=now,
    ))


def materialize(store, resource_id: int, url: str, access_code: str | None, *, media_id: int, media_title: str | None, now: int) -> dict:
    """Encrypt an unlocked payload into ``resource_link`` (insert-only) and
    associate it with the RE0 resource. A URL that already exists locally
    only gets a ``same_local`` association -- the old row is never changed."""
    info = library_normalize.parse_link(url, access_code)
    canonical = library_normalize.canonical_hash(info.canonical)
    conn = store.connect()
    try:
        res = conn.execute("SELECT spec_json, media_title FROM re0_resource WHERE id=?", (resource_id,)).fetchone()
        if res is None:
            raise ValueError("re0_resource not found")
        spec = json.loads(res["spec_json"] or "{}")
        existing = conn.execute(
            "SELECT id, group_id FROM resource_link WHERE provider=? AND canonical_url_hash=?", (info.provider, canonical)
        ).fetchone()
    finally:
        conn.close()
    if existing is not None:
        link_id, group_id, relation, created = int(existing["id"]), int(existing["group_id"]), "same_local", False
        new_state = "linked_local"
        conn = store.connect(readonly=True)
        try:
            prior = conn.execute(
                "SELECT relation FROM re0_resource_link WHERE re0_resource_id=? AND resource_link_id=?", (resource_id, link_id)
            ).fetchone()
        finally:
            conn.close()
        if prior is not None:
            # Re-materialising a payload this very resource already produced
            # (a replayed action, a re-seen unlocked item): keep its state.
            return {"resource_link_id": link_id, "group_id": group_id, "relation": prior["relation"], "created": False, "provider": info.provider}
    else:
        conn = store.connect()
        try:
            group_id = _group_for_spec(store, conn, media_id, spec, media_title or res["media_title"], now)
        finally:
            conn.close()
        import library_store
        rec = library_store.LinkRecord(
            public_id=f"re0-{hashlib.sha256(f'{resource_id}|{canonical}'.encode()).hexdigest()[:16]}", group_id=group_id,
            provider=info.provider, canonical_url_hash=canonical, url_label=info.label,
            url_ciphertext=store.fernet.encrypt(info.url.encode("utf-8")),
            access_code_ciphertext=store.fernet.encrypt(info.access_code.encode("utf-8")) if info.access_code else None,
            has_access_code=1 if info.access_code else 0, remark="RE0", imported_at=now,
        )
        link_id, created = store.insert_link_preserving_existing(rec)
        relation = "materialized_unlocked" if created else "same_local"
        new_state = "unlocked" if created else "linked_local"
        store.recount_groups([group_id])
    conn = store.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO re0_resource_link(re0_resource_id, resource_link_id, relation, linked_at) VALUES(?,?,?,?)",
            (resource_id, link_id, relation, now),
        )
        # G03.3: the payload is in the library now, so the pending copy goes.
        conn.execute(
            "UPDATE re0_resource SET state=?, group_id=?, media_id=COALESCE(?, media_id), resource_url_ciphertext=?, unlocked_at=COALESCE(unlocked_at, ?), "
            "pending_payload_cipher=NULL, pending_saved_at=NULL, last_error_class=NULL, updated_at=? WHERE id=?",
            (new_state, group_id, media_id, store.fernet.encrypt(info.url.encode("utf-8")) if created else None, now, now, resource_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"resource_link_id": link_id, "group_id": group_id, "relation": relation, "created": created, "provider": info.provider}


def record_items(store, media_type: str, tmdb_id: int, raw_items: list, *, media_id: int | None, media_title: str | None, salt: str, now: int) -> dict:
    """Project one ``/api/open/resources`` response. Never calls unlock: an
    ``is_unlocked`` item is materialised only when RE0 already returned its
    payload; otherwise it is recorded as ``already_unlocked_no_payload``."""
    report = {"remote_items": len(raw_items), "valid_items": 0, "invalid_items": 0, "new": 0, "seen": 0, "materialized": 0,
              "already_unlocked_no_payload": 0, "provider_unmapped": 0, "linked_local": 0}
    for raw in raw_items:
        item = normalize_item(raw, salt=salt)
        if item is None:
            report["invalid_items"] += 1
            continue
        report["valid_items"] += 1
        error_class = None
        if item["provider_code"] == "unknown":
            error_class = "provider_unmapped"
            report["provider_unmapped"] += 1
        if item["is_unlocked"] and not item["payload_url"]:
            error_class = "already_unlocked_no_payload"
            report["already_unlocked_no_payload"] += 1
        rid, created = upsert_resource(store, media_type, tmdb_id, item, media_id=media_id, media_title=media_title, now=now, error_class=error_class)
        report["new" if created else "seen"] += 1
        if item["is_unlocked"] and item["payload_url"] and media_id is not None:
            try:
                outcome = materialize(store, rid, item["payload_url"], item["payload_access_code"], media_id=media_id, media_title=media_title, now=now)
            except (ValueError, sqlite3.DatabaseError) as exc:
                LOG.warning("re0 materialize failed error=%s", type(exc).__name__)
                conn = store.connect()
                try:
                    conn.execute("UPDATE re0_resource SET last_error_class='materialize_failed', updated_at=? WHERE id=?", (now, rid))
                    conn.commit()
                finally:
                    conn.close()
                continue
            report["linked_local" if outcome["relation"] == "same_local" else "materialized"] += 1
    return report


# ---------------------------------------------------------------------------
# Federated search: query cache, candidate selection, safe item assembly
# ---------------------------------------------------------------------------

import unicodedata  # noqa: E402

SEARCH_TTL_SECONDS = 20 * 60
NEGATIVE_TTL_SECONDS = 10 * 60
RESOURCES_TTL_SECONDS = 24 * 3600
MAX_CANDIDATES = 5
PROVIDER_ORDER = ["115", "tianyicloud", "quark", "alipan", "baidu", "guangya", "139cloud", "123", "ed2k", "unknown"]
UNLOCKED_STATES = ("unlocked", "already_unlocked", "linked_local")


def fold_query(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def query_hash(q: str, media_type: str, year: int | None) -> str:
    return hashlib.sha256(f"{fold_query(q)}|{media_type}|{year or ''}".encode("utf-8")).hexdigest()


def cache_get(conn: sqlite3.Connection, cache_key: str):
    return conn.execute("SELECT * FROM re0_search_cache WHERE cache_key=?", (cache_key,)).fetchone()


def cache_put(
    conn: sqlite3.Connection, cache_key: str, qhash: str, media_type: str, *, filters: dict, candidate_refs: list,
    status: str, now: int, ttl: int, retry_after_until: int | None = None, error_class: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO re0_search_cache(cache_key, query_hash, media_type, filter_json, candidate_ids_json, status, fetched_at, expires_at, "
        "retry_after_until, error_class, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(cache_key) DO UPDATE SET candidate_ids_json=excluded.candidate_ids_json, status=excluded.status, fetched_at=excluded.fetched_at, "
        "expires_at=excluded.expires_at, retry_after_until=excluded.retry_after_until, error_class=excluded.error_class, updated_at=excluded.updated_at",
        (cache_key, qhash, media_type, json.dumps(filters, ensure_ascii=False), json.dumps(candidate_refs), status, now, now + ttl,
         retry_after_until, error_class, now, now),
    )


def tmdb_result_to_candidate(kind: str, row: dict) -> dict | None:
    tmdb_id = _as_int(row.get("id"))
    if tmdb_id is None or tmdb_id <= 0:
        return None
    title = row.get("title") if kind == "movie" else row.get("name")
    original = row.get("original_title") if kind == "movie" else row.get("original_name")
    date = row.get("release_date") if kind == "movie" else row.get("first_air_date")
    year = _as_int(str(date)[:4]) if isinstance(date, str) and len(date) >= 4 else None
    score = row.get("vote_average")
    votes = _as_int(row.get("vote_count"))
    return {
        "media_type": kind, "tmdb_id": tmdb_id, "title": str(title or original or f"TMDB {tmdb_id}")[:200],
        "original_title": str(original)[:200] if isinstance(original, str) else None, "year": year,
        "poster_path": row.get("poster_path") if isinstance(row.get("poster_path"), str) else None,
        "backdrop_path": row.get("backdrop_path") if isinstance(row.get("backdrop_path"), str) else None,
        "overview": str(row.get("overview") or "")[:2000] or None,
        "ratings": {"tmdb": {"score": round(float(score), 1), "votes": votes or 0}} if isinstance(score, (int, float)) and score else {},
        "votes": votes or 0,
    }


def select_candidates(candidates: list[dict], query: str, year: int | None, limit: int = MAX_CANDIDATES) -> list[dict]:
    """Rank by exact folded title match, then year match, then popularity;
    dedupe by (media_type, tmdb_id); keep at most ``limit``."""
    folded = fold_query(query)
    seen = set()
    ranked = []
    for cand in candidates:
        key = (cand["media_type"], cand["tmdb_id"])
        if key in seen:
            continue
        seen.add(key)
        score = 0
        if folded and folded in (fold_query(cand["title"]), fold_query(cand.get("original_title") or "")):
            score += 100
        if year and cand.get("year") == year:
            score += 20
        ranked.append((score, cand["votes"], cand))
    ranked.sort(key=lambda t: (-t[0], -t[1], t[2]["media_type"], t[2]["tmdb_id"]))
    return [c for _, _, c in ranked[:limit]]


def direct_result_to_candidate(row: object) -> dict | None:
    """The disabled-by-default direct-search hook accepts only a minimal,
    documented shape: ``{media_type, tmdb_id, title, year?, ...}``."""
    if not isinstance(row, dict) or row.get("media_type") not in ("movie", "tv"):
        return None
    tmdb_id = _as_int(row.get("tmdb_id"))
    if tmdb_id is None or tmdb_id <= 0:
        return None
    return {
        "media_type": row["media_type"], "tmdb_id": tmdb_id, "title": str(row.get("title") or f"TMDB {tmdb_id}")[:200],
        "original_title": str(row.get("original_title"))[:200] if isinstance(row.get("original_title"), str) else None,
        "year": _as_int(row.get("year")), "poster_path": row.get("poster_path") if isinstance(row.get("poster_path"), str) else None,
        "backdrop_path": row.get("backdrop_path") if isinstance(row.get("backdrop_path"), str) else None,
        "overview": str(row.get("overview") or "")[:2000] or None, "ratings": {}, "votes": 0,
    }


def local_media_id_for(conn: sqlite3.Connection, media_type: str, tmdb_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM media WHERE media_type=? AND tmdb_id=? ORDER BY (match_status='exact') DESC, id ASC LIMIT 1", (media_type, tmdb_id)
    ).fetchone()
    return int(row[0]) if row else None


def local_exact_candidates(conn: sqlite3.Connection, q: str, *, kinds: list, year: int | None, charmap: dict,
                           limit: int = MAX_CANDIDATES) -> list[dict]:
    """Library media already matched to a TMDB identity (``match_status=
    exact`` with a ``tmdb_id``) whose title matches the query, in the
    library's own search-rank order (same recall/rerank as the local lane).
    candidate/needs_review/unmatched rows and rows without a tmdb_id are
    never returned -- nothing is guessed; those titles go through TMDB."""
    import library_search  # local import: shares the lane's tokenizer/ranker
    plan = library_search.parse_query(q)
    if not plan.text or not kinds:
        return []
    hits = library_search.rerank(conn, library_search.recall(conn, plan, charmap), plan, charmap)
    if not hits:
        return []
    # "Exact" means the whole query is the title (or one of its aliases /
    # the original title), the same rule the lane's reranker boosts -- a
    # shared character or word is not enough to skip the TMDB search.
    folded = library_search.fold(plan.text, charmap)
    placeholders = ",".join("?" for _ in hits)
    ids = []
    for row in conn.execute(f"SELECT media_id, title_key, alias_keys_json FROM search_doc WHERE media_id IN ({placeholders})",
                            [h.media_id for h in hits]).fetchall():
        try:
            aliases = json.loads(row["alias_keys_json"] or "[]")
        except ValueError:
            aliases = []
        if folded == row["title_key"] or folded in aliases:
            ids.append(int(row["media_id"]))
    if not ids:
        return []
    order = {h.media_id: i for i, h in enumerate(hits)}
    ids.sort(key=lambda media_id: order[media_id])
    sql = ("SELECT id, media_type, tmdb_id, title_zh, title_original, year, overview, poster_path, backdrop_path, ratings_json FROM media "
           "WHERE id IN (%s) AND match_status='exact' AND tmdb_id IS NOT NULL AND tmdb_id > 0 AND media_type IN (%s)"
           % (",".join("?" for _ in ids), ",".join("?" for _ in kinds)))
    params: list = [*ids, *kinds]
    if year:
        sql += " AND year=?"
        params.append(year)
    by_id = {row["id"]: row for row in conn.execute(sql, params).fetchall()}
    out = []
    for media_id in ids:
        row = by_id.get(media_id)
        if row is None:
            continue
        try:
            ratings = json.loads(row["ratings_json"] or "{}")
        except ValueError:
            ratings = {}
        out.append({
            "media_type": row["media_type"], "tmdb_id": int(row["tmdb_id"]), "title": row["title_zh"], "original_title": row["title_original"],
            "year": row["year"], "poster_path": row["poster_path"], "backdrop_path": row["backdrop_path"], "overview": row["overview"],
            "ratings": ratings if isinstance(ratings, dict) else {}, "votes": 0, "local_media_id": int(row["id"]),
        })
        if len(out) >= limit:
            break
    return out


def merge_candidates(local: list[dict], remote: list[dict], limit: int = MAX_CANDIDATES) -> list[dict]:
    """One candidate per ``(media_type, tmdb_id)``; local matches come first
    and win over a remote duplicate."""
    seen = set()
    merged = []
    for cand in [*local, *remote]:
        key = (cand["media_type"], cand["tmdb_id"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(cand)
    return merged[:limit]


def resource_summary(conn: sqlite3.Connection, media_type: str, tmdb_id: int, providers: tuple[str, ...] = ()) -> dict:
    """Provider-filtered (server side) counts for one projected media."""
    sql = "SELECT provider_code, state FROM re0_resource WHERE media_type=? AND tmdb_id=? AND state != 'permanent_error'"
    params: list = [media_type, tmdb_id]
    if providers:
        sql += " AND provider_code IN (%s)" % ",".join("?" for _ in providers)
        params.extend(providers)
    rows = conn.execute(sql, params).fetchall()
    codes = sorted({r["provider_code"] for r in rows}, key=lambda c: (PROVIDER_ORDER.index(c) if c in PROVIDER_ORDER else 99, c))
    unlocked = sum(1 for r in rows if r["state"] in UNLOCKED_STATES)
    count = len(rows)
    state = "none" if not count else ("unlocked" if unlocked == count else ("partial" if unlocked else "candidate"))
    return {"candidate_count": count, "unlocked_count": unlocked, "providers": codes, "state": state}


def projection_items(conn: sqlite3.Connection, refs: list, providers: tuple[str, ...], image_url) -> list[dict]:
    """Safe search-lane items for the given ``(media_type, tmdb_id)`` refs;
    media with no (provider-filtered) RE0 resource are left out."""
    import library_search  # local import: overview_short helper only
    items = []
    for media_type, tmdb_id in refs:
        row = conn.execute("SELECT * FROM re0_media_projection WHERE media_type=? AND tmdb_id=?", (media_type, tmdb_id)).fetchone()
        if row is None:
            continue
        summary = resource_summary(conn, media_type, tmdb_id, providers)
        if not summary["candidate_count"]:
            continue
        try:
            ratings = json.loads(row["ratings_json"] or "{}")
        except ValueError:
            ratings = {}
        items.append({
            "media_ref": f"re0:{media_type}:{tmdb_id}", "local_media_id": row["local_media_id"], "media_type": media_type, "tmdb_id": tmdb_id,
            "title": row["title"], "original_title": row["original_title"], "year": row["year"],
            "poster_url": image_url(row["poster_path"], "poster"), "backdrop_url": image_url(row["backdrop_path"], "backdrop"),
            "overview_short": library_search._overview_short(row["overview"]), "ratings": ratings,
            "metadata_status": row["metadata_status"], "ratings_status": row["ratings_status"],
            "updated_at": datetime.fromtimestamp(int(row["updated_at"]), tz=timezone.utc).isoformat() if row["updated_at"] else None,
            **summary,
        })
    return items


# ---------------------------------------------------------------------------
# Projection metadata enrichment -- runs inside the existing TMDB
# BackgroundEnricher round (same client, budget, locks); never touches RE0.
# ---------------------------------------------------------------------------

METADATA_TTL_SECONDS = 30 * 86400
RATINGS_TTL_SECONDS = 7 * 86400
DEFAULT_RETRY_SECONDS = 300


def select_projections_to_enrich(conn: sqlite3.Connection, now: int, limit: int) -> list:
    return conn.execute(
        "SELECT * FROM re0_media_projection WHERE metadata_status IN ('pending','partial') "
        "OR (metadata_status='retryable' AND (next_retry_at IS NULL OR next_retry_at <= ?)) "
        "OR (metadata_status='complete' AND metadata_fetched_at IS NOT NULL AND metadata_fetched_at <= ?) "
        "ORDER BY CASE metadata_status WHEN 'pending' THEN 0 WHEN 'partial' THEN 1 WHEN 'retryable' THEN 2 ELSE 3 END, "
        "last_seen_at DESC LIMIT ?",
        (now, now - METADATA_TTL_SECONDS, limit),
    ).fetchall()


def enrich_projections(store, client, *, limit: int = 5, now: int | None = None) -> dict:
    """Fill title/overview/poster/backdrop/TMDB rating (plus the local IMDb
    rating table when the external id is known) for projected media."""
    import library_tmdb  # local import: this module is imported by library_store

    now = int(now if now is not None else time.time())
    report = {"selected": 0, "completed": 0, "retryable": 0, "failed": 0, "budget_exhausted": False}
    conn = store.connect(readonly=True)
    try:
        rows = [dict(r) for r in select_projections_to_enrich(conn, now, limit)]
    finally:
        conn.close()
    for row in rows:
        report["selected"] += 1
        kind, tmdb_id = row["media_type"], int(row["tmdb_id"])
        try:
            entry = client.details(kind, tmdb_id, append_to_response="external_ids")
        except library_tmdb.BudgetExhausted:
            report["budget_exhausted"] = True
            break
        except Exception as exc:  # noqa: BLE001 -- class name only, never a key or URL
            _mark_projection(store, kind, tmdb_id, "retryable", type(exc).__name__, now, now + DEFAULT_RETRY_SECONDS)
            report["retryable"] += 1
            continue
        payload = entry.payload[0] if entry.status == "ok" and isinstance(entry.payload, list) and entry.payload and isinstance(entry.payload[0], dict) else None
        if payload is None:
            if entry.status == "failed_retryable" or entry.retry_after:
                _mark_projection(store, kind, tmdb_id, "retryable", entry.error_class or "retryable", now, now + int(entry.retry_after or DEFAULT_RETRY_SECONDS))
                report["retryable"] += 1
            else:
                _mark_projection(store, kind, tmdb_id, "failed", entry.error_class or entry.status, now, None)
                report["failed"] += 1
            continue
        title = payload.get("title") if kind == "movie" else payload.get("name")
        original = payload.get("original_title") if kind == "movie" else payload.get("original_name")
        date = payload.get("release_date") if kind == "movie" else payload.get("first_air_date")
        year = _as_int(str(date)[:4]) if isinstance(date, str) and len(date) >= 4 else None
        ratings = {}
        score, votes = payload.get("vote_average"), _as_int(payload.get("vote_count"))
        if isinstance(score, (int, float)) and score:
            ratings["tmdb"] = {"score": round(float(score), 1), "votes": votes or 0}
        external = payload.get("external_ids") if isinstance(payload.get("external_ids"), dict) else {}
        imdb_id = external.get("imdb_id") if isinstance(external.get("imdb_id"), str) else None
        if imdb_id:
            conn = store.connect(readonly=True)
            try:
                imdb = library_tmdb.lookup_imdb_rating(conn, imdb_id)
            except sqlite3.OperationalError:
                imdb = None
            finally:
                conn.close()
            if imdb:
                ratings["imdb"] = {"score": imdb.get("score"), "votes": imdb.get("votes")}
        upsert_projection(
            store, kind, tmdb_id, title=str(title or row["title"])[:200], original_title=str(original)[:200] if isinstance(original, str) else None,
            year=year, overview=str(payload.get("overview") or "")[:2000] or None,
            poster_path=payload.get("poster_path") if isinstance(payload.get("poster_path"), str) else None,
            backdrop_path=payload.get("backdrop_path") if isinstance(payload.get("backdrop_path"), str) else None,
            ratings=ratings, now=now, metadata_status="complete", ratings_status="complete",
        )
        conn = store.connect()
        try:
            conn.execute(
                "UPDATE re0_media_projection SET metadata_fetched_at=?, ratings_fetched_at=?, last_error_class=NULL, next_retry_at=NULL WHERE media_type=? AND tmdb_id=?",
                (now, now, kind, tmdb_id),
            )
            conn.commit()
        finally:
            conn.close()
        report["completed"] += 1
    return report


def _mark_projection(store, kind: str, tmdb_id: int, status: str, error_class: str | None, now: int, next_retry_at: int | None) -> None:
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE re0_media_projection SET metadata_status=?, last_error_class=?, next_retry_at=?, updated_at=? WHERE media_type=? AND tmdb_id=?",
            (status, (error_class or "")[:64] or None, next_retry_at, now, kind, tmdb_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Detail-page candidates, remote-only detail, status, user actions
# ---------------------------------------------------------------------------

ACTION_BY_PROVIDER = {"115": "transfer", "ed2k": "cloud", "unknown": "unavailable"}


def action_for_provider(code: str) -> str:
    return ACTION_BY_PROVIDER.get(code, "copy")


def _iso(ts) -> str | None:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else None


# ---------------------------------------------------------------------------
# Effective display status (invalid-candidate work order §3.1). Derived from
# fields that already exist -- the upstream validate status and the file
# preview cache -- so there is exactly one notion of "RE0 says this share is
# dead". A preview we could not run, a rate limit, a 5xx or an in-progress
# check are NEVER a verdict of dead.
# ---------------------------------------------------------------------------

EFFECTIVE_STATUS_LABEL = {
    "valid": "RE0 校验有效",
    "invalid": "RE0 已失效",
    "checking": "RE0 校验中",
    "preview_unavailable": "无法预览文件",
    "unknown": "校验结果未知",
    "unchecked": "未校验",
}
_UPSTREAM_CHECKING = {"checking", "pending", "verifying", "processing"}
# A protocol link is not a pan directory: there is nothing to list, so the
# preview is never offered and its absence is never evidence of anything.
_NO_FILE_PREVIEW_PROVIDERS = {"ed2k", "unknown"}
_STATUS_REASON_MAX = 300


def supports_file_preview(provider_code: str) -> bool:
    return provider_code not in _NO_FILE_PREVIEW_PROVIDERS


def effective_status(*, upstream_validate_status, validate_message=None, last_validated_at=None,
                     preview=None, now: int) -> dict:
    """``{status, label, reason, checked_at}`` for one candidate.

    Priority: a live ``invalid`` preview, then an upstream ``invalid``, then an
    in-progress check, then an upstream ``valid``, then a preview we were not
    allowed or able to run, then any other upstream value, then nothing known.
    An expired preview row carries no weight at all."""
    upstream = str(upstream_validate_status or "").strip().lower()
    live = preview if preview is not None and int(preview["expires_at"] or 0) > now else None

    def _out(status, reason=None, checked_at=None):
        return {"status": status, "label": EFFECTIVE_STATUS_LABEL[status],
                "reason": (reason or None) and str(reason)[:_STATUS_REASON_MAX], "checked_at": checked_at}

    if live is not None and live["status"] == "invalid":
        return _out("invalid", live["validate_message"] or validate_message, _iso(live["fetched_at"]))
    if upstream == "invalid":
        return _out("invalid", validate_message, last_validated_at)
    if upstream in _UPSTREAM_CHECKING:
        return _out("checking", validate_message, last_validated_at)
    if upstream == "valid":
        return _out("valid", None, last_validated_at)
    if live is not None and live["status"] in ("unsupported", "forbidden"):
        return _out("preview_unavailable", None, _iso(live["fetched_at"]))
    if upstream:
        return _out("unknown", validate_message, last_validated_at)
    return _out("unchecked", None, last_validated_at)


def split_invalid(rows: list, include_invalid: bool) -> tuple[list, int]:
    """Hide a candidate only when RE0 confirmed it dead AND nothing of it was
    ever materialised locally -- a candidate that already produced a local link
    stays, so the link can never vanish with it."""
    if include_invalid:
        return list(rows), 0
    visible = [r for r in rows if not (r.get("effective_status") == "invalid" and not r.get("has_local_link"))]
    return visible, len(rows) - len(visible)


def candidate_rows(conn: sqlite3.Connection, media_type: str, tmdb_id: int, providers: tuple[str, ...] = (),
                   *, now: int | None = None) -> list[dict]:
    """Safe detail-page rows for one media's RE0 resources (never slug/URL).

    Every row carries its derived ``effective_status`` -- filtering is the
    caller's job (``split_invalid``), so an audit view can ask for the same
    rows without a second query."""
    now = int(now if now is not None else time.time())
    sql = (
        "SELECT r.*, l.public_id AS link_public_id, rl.relation AS relation FROM re0_resource r "
        "LEFT JOIN re0_resource_link rl ON rl.re0_resource_id = r.id "
        "LEFT JOIN resource_link l ON l.id = rl.resource_link_id "
        "WHERE r.media_type=? AND r.tmdb_id=? AND r.state != 'permanent_error'"
    )
    params: list = [media_type, tmdb_id]
    if providers:
        sql += " AND r.provider_code IN (%s)" % ",".join("?" for _ in providers)
        params.extend(providers)
    sql += " ORDER BY r.id"
    try:
        previews = {int(p["re0_resource_id"]): p for p in conn.execute(
            "SELECT p.* FROM re0_file_preview p JOIN re0_resource r ON r.id = p.re0_resource_id "
            "WHERE r.media_type=? AND r.tmdb_id=?", (media_type, tmdb_id)).fetchall()}
    except sqlite3.OperationalError:  # a library that predates the preview table
        previews = {}
    rows = []
    seen = set()
    for r in conn.execute(sql, params).fetchall():
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        try:
            spec = json.loads(r["spec_json"] or "{}")
        except ValueError:
            spec = {}
        raw = spec.get("raw") or {}
        preview = previews.get(int(r["id"]))
        composition = None
        if preview is not None:
            try:
                composition = json.loads(preview["composition_json"] or "{}") or None
            except ValueError:
                composition = None
        if composition is None:
            composition = parse_composition(remark=raw.get("remark"), title=raw.get("title"))
        previewable = supports_file_preview(r["provider_code"])
        preview_summary = {
            "available": previewable and (preview["status"] not in ("forbidden", "unsupported") if preview is not None else True),
            "status": ("not_applicable" if not previewable
                       else (preview["status"] if preview is not None else "not_loaded")),
            "file_count": preview["file_count"] if preview is not None else None,
            "fetched_at": _iso(preview["fetched_at"]) if preview is not None else None,
        }
        derived = effective_status(
            upstream_validate_status=r["upstream_validate_status"], validate_message=raw.get("validate_message"),
            last_validated_at=raw.get("last_validated_at"), preview=preview, now=now,
        )
        rows.append({
            "id": int(r["id"]), "provider": r["provider_code"], "provider_label": library_normalize.PROVIDERS.get(r["provider_code"], r["provider_code"]),
            "upstream_pan_type": r["upstream_pan_type"], "state": r["state"], "unlock_points": r["unlock_points"],
            "is_unlocked_upstream": bool(r["upstream_is_unlocked"]), "source_status": r["upstream_validate_status"],
            "specs": library_normalize.resource_specs(spec.get("quality"), spec.get("hdr"), spec.get("source_type")),
            "title": raw.get("title") or None, "size": raw.get("share_size") if raw.get("share_size") not in ("", None) else None,
            "subtitle_language": raw.get("subtitle_language") or [], "subtitle_type": raw.get("subtitle_type") or [],
            # Work order §5.1: what a user needs BEFORE spending points --
            # the publisher's own words, who posted it and when, and the
            # composition read from them (refined by a cached file preview).
            "remark": raw.get("remark") or None, "published_at": raw.get("created_at") or None,
            "publisher": raw.get("publisher") or None, "is_official": raw.get("is_official"),
            "unlocked_users_count": raw.get("unlocked_users_count"),
            "source_message": raw.get("validate_message") or None,
            "composition": composition, "file_preview": preview_summary,
            # §3.1: one derived verdict per candidate, plus whether anything of
            # it already exists locally (which pins the row in place).
            "effective_status": derived["status"], "effective_status_label": derived["label"],
            "effective_status_reason": derived["reason"], "effective_checked_at": derived["checked_at"],
            "has_local_link": bool(r["link_public_id"]),
            "last_seen_at": _iso(r["last_seen_at"]), "unlocked_at": _iso(r["unlocked_at"]), "last_error_class": r["last_error_class"],
            "resource_link_id": r["link_public_id"], "relation": r["relation"],
            "action": action_for_provider(r["provider_code"]) if r["state"] not in UNLOCKED_STATES or not r["link_public_id"] else action_for_provider(r["provider_code"]),
        })
    rows.sort(key=lambda c: (PROVIDER_ORDER.index(c["provider"]) if c["provider"] in PROVIDER_ORDER else 99, c["id"]))
    return rows


def projection_row(conn: sqlite3.Connection, media_type: str, tmdb_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM re0_media_projection WHERE media_type=? AND tmdb_id=?", (media_type, tmdb_id)).fetchone()
    return dict(row) if row else None


def status_summary(conn: sqlite3.Connection, now: int) -> dict:
    projections = {k: 0 for k in ("pending", "partial", "complete", "retryable", "failed")}
    for status, count in conn.execute("SELECT metadata_status, COUNT(*) FROM re0_media_projection GROUP BY metadata_status"):
        projections[status] = projections.get(status, 0) + count
    resources: dict[str, int] = {}
    for state, count in conn.execute("SELECT state, COUNT(*) FROM re0_resource GROUP BY state"):
        resources[state] = count
    day_start = now - (now % 86400)
    actions_today = conn.execute("SELECT COUNT(*) FROM re0_action WHERE created_at >= ? AND status='success'", (day_start,)).fetchone()[0]
    last_error = conn.execute(
        "SELECT last_error_class FROM re0_media_projection WHERE last_error_class IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    return {"projections": projections, "resources": resources, "actions_today": int(actions_today), "last_error_class": last_error[0] if last_error else None}


def create_media_from_projection(store, media_type: str, tmdb_id: int, now: int) -> int | None:
    """Promote a remote-only projection to a local ``media`` row (exact
    match: the TMDB id came from RE0's own public payload, not a title
    guess) so an unlocked link has somewhere to live. Idempotent."""
    import library_store
    conn = store.connect()
    try:
        proj = conn.execute("SELECT * FROM re0_media_projection WHERE media_type=? AND tmdb_id=?", (media_type, tmdb_id)).fetchone()
        if proj is None:
            return None
        existing = local_media_id_for(conn, media_type, tmdb_id)
    finally:
        conn.close()
    if existing is None:
        existing = store.upsert_media(library_store.MediaRecord(
            media_identity=f"tmdb:{media_type}:{tmdb_id}", media_type=media_type, title_zh=proj["title"], search_key=proj["title"],
            title_original=proj["original_title"], year=proj["year"], tmdb_id=tmdb_id, overview=proj["overview"],
            poster_path=proj["poster_path"], backdrop_path=proj["backdrop_path"], match_status="exact", match_score=1.0,
            created_at=now, updated_at=now,
        ))
    conn = store.connect()
    try:
        conn.execute("UPDATE re0_media_projection SET local_media_id=?, updated_at=? WHERE media_type=? AND tmdb_id=?", (existing, now, media_type, tmdb_id))
        conn.execute("UPDATE re0_resource SET media_id=?, updated_at=? WHERE media_type=? AND tmdb_id=? AND media_id IS NULL", (existing, now, media_type, tmdb_id))
        state_set(conn, f"index_pending:{existing}", "1", now)
        conn.commit()
    finally:
        conn.close()
    return existing


def catalog_projections(store, refs: list, *, charmap=None) -> int:
    """Keep discovered identities searchable without calling unlock.

    Reuse the canonical media identity and index only changed documents.
    Query text is deliberately NOT an alias (a vague query must not poison
    future exact matches); only the provider's names become aliases.
    """
    import library_search
    changed_ids = []
    now = int(time.time())
    for kind, tmdb_id in sorted(set(map(tuple, refs))):
        conn = store.connect(readonly=True)
        try:
            proj = projection_row(conn, kind, tmdb_id)
            visible, _ = split_invalid(candidate_rows(conn, kind, tmdb_id, now=now), False)
        finally:
            conn.close()
        if not proj or not visible or not proj["title"] or proj["title"].startswith("TMDB "):
            continue
        mid = create_media_from_projection(store, kind, tmdb_id, now) if not proj["local_media_id"] else proj["local_media_id"]
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT title_zh, title_original, title_alt_json FROM media WHERE id=?", (mid,)).fetchone()
            if row is None:
                continue
            aliases = json.loads(row["title_alt_json"] or "[]")
            names = {row["title_zh"], row["title_original"], *aliases}
            additions = [n for n in (proj["title"], proj["original_title"]) if n and n not in names]
            if additions:
                conn.execute("UPDATE media SET title_alt_json=?, updated_at=? WHERE id=?",
                             (json.dumps(list(dict.fromkeys(aliases + additions)), ensure_ascii=False), now, mid))
            indexed = conn.execute("SELECT alias_keys_json FROM search_doc WHERE media_id=?", (mid,)).fetchone()
            expected = {library_search.fold(n, charmap or {}) for n in aliases + additions + [row["title_original"]] if n}
            # A prior request may have committed metadata but failed while
            # indexing. Detect missing keys again rather than losing the retry.
            if additions or not indexed or not expected.issubset(set(json.loads(indexed[0] or "[]"))):
                state_set(conn, f"index_pending:{mid}", "1", now)
                changed_ids.append(mid)
            elif state_get(conn, f"index_pending:{mid}") is not None:
                changed_ids.append(mid)
            conn.commit()
        finally:
            conn.close()
    if changed_ids:
        library_search.build_index(store, media_ids=changed_ids, charmap=charmap or {}, pending_only=True)
    return len(changed_ids)


INDEX_BATCH_SIZE = 10
INDEX_REWEIGHT_INTERVAL = 7 * 86400


def rebuild_index_if_dirty(store, *, charmap=None, now: int | None = None) -> bool:
    """Drain a bounded durable batch; never run a full online index rebuild.

    New/changed titles have priority. Weekly weight calibration is a gradual
    pass in the 03:00–05:00 Shanghai window, not a single corpus snapshot.
    SQLite serializes the actual build and queue ACK across all workers.
    """
    now = int(time.time()) if now is None else int(now)
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if int(state_get(conn, "index_retry_after") or 0) > now:
            return False
        if state_get(conn, "index_dirty") == "1":
            # Consume old flags, including ones from a rolling-release worker.
            # Queue all IDs once so unknown historical dirty sources are safe.
            conn.execute("INSERT OR IGNORE INTO re0_sync_state(key,value,updated_at) "
                         "SELECT 'index_pending:' || id, '1', ? FROM media", (now,))
            state_set(conn, "index_dirty", "0", now)
        # Recover the crash gap between media creation and its queue write.
        conn.execute("INSERT OR IGNORE INTO re0_sync_state(key,value,updated_at) "
                     "SELECT 'index_pending:' || m.id, '1', ? FROM media m "
                     "LEFT JOIN search_doc d ON d.media_id=m.id WHERE d.media_id IS NULL "
                     "ORDER BY m.id LIMIT ?", (now, INDEX_BATCH_SIZE))
        keys = [r[0] for r in conn.execute(
            "SELECT key FROM re0_sync_state WHERE key GLOB 'index_pending:*' ORDER BY updated_at,key LIMIT ?",
            (INDEX_BATCH_SIZE,))]
        next_at = state_get(conn, "index_reweight_next_at")
        if next_at is None:
            # Do not launch a whole-corpus calibration on first deployment.
            state_set(conn, "index_reweight_next_at", str(now + INDEX_REWEIGHT_INTERVAL), now)
        if not keys and 3 <= datetime.fromtimestamp(now, tz=SHANGHAI).hour < 5:
            if next_at is not None and int(next_at) <= now and not conn.execute(
                "SELECT 1 FROM re0_sync_state WHERE key GLOB 'index_reweight:*' LIMIT 1"
            ).fetchone():
                conn.execute("INSERT OR IGNORE INTO re0_sync_state(key,value,updated_at) "
                             "SELECT 'index_reweight:' || id, '1', ? FROM media", (now,))
                state_set(conn, "index_reweight_next_at", str(now + INDEX_REWEIGHT_INTERVAL), now)
            keys = [r[0] for r in conn.execute(
                "SELECT key FROM re0_sync_state WHERE key GLOB 'index_reweight:*' ORDER BY key LIMIT ?",
                (INDEX_BATCH_SIZE,))]
        ids = [int(key.split(":", 1)[1]) for key in keys]
        conn.commit()
    finally:
        conn.close()
    if not ids:
        return False
    import library_search  # local import: library_search imports library_store
    try:
        library_search.build_index(store, media_ids=ids, charmap=charmap or {}, pending_only=True)
    except Exception as exc:
        conn = store.connect()
        try:
            state_set(conn, "index_retry_after", str(now + 30), now)
            state_set(conn, "index_last_error", type(exc).__name__, now)
            conn.commit()
        finally:
            conn.close()
        raise
    conn = store.connect()
    try:
        state_set(conn, "index_last_success", str(now), now)
        state_set(conn, "index_last_error", "", now)
        conn.commit()
    finally:
        conn.close()
    return True


def action_get(conn: sqlite3.Connection, request_id: str, resource_id: int):
    return conn.execute("SELECT * FROM re0_action WHERE request_id=? AND re0_resource_id=?", (request_id, resource_id)).fetchone()


def resource_lease_key(resource_id: int) -> str:
    """One key space per kind of thing being unlocked.

    Resources and follow packs are different objects with their own ids; a
    shared integer column invited a pack's truncated hash to be squeezed into
    it, which is not a collision-free identity (review R08).
    """
    return f"resource:{int(resource_id)}"


def pack_lease_key(slug_hash_value: str) -> str:
    return f"pack:{slug_hash_value}"


def slug_lease_key(slug_hash_value: str) -> str:
    """For a slug this deployment has no candidate row for (review F05).

    A resource we do know is always keyed by ``resource_lease_key`` -- whatever
    entry point reached it -- so two entries never coordinate on two different
    keys for one resource. This one only covers the remaining case: the legacy
    endpoint asked to unlock a slug that is not in ``re0_resource`` at all.
    """
    return f"slug:{slug_hash_value}"


def acquire_unlock_lease(conn: sqlite3.Connection, lease_key: str, *, holder: str, now: int,
                         ttl: int = UNLOCK_LEASE_SECONDS) -> bool:
    """Take the lease on one unlockable thing, or report that somebody has it.

    Atomic in one statement: the conflicting UPDATE only fires when the
    existing lease has expired, so two concurrent callers cannot both come
    away believing they hold it. A database lease rather than a process lock
    because gunicorn runs several workers and restarts them (plan §10.2).

    Holding the lease is not on its own enough: the holder must re-read the
    local result inside it before calling upstream, because the previous
    holder may have just produced one (R08).
    """
    cursor = conn.execute(
        "INSERT INTO re0_unlock_lease(lease_key, holder, acquired_at, expires_at) VALUES(?,?,?,?) "
        "ON CONFLICT(lease_key) DO UPDATE SET holder=excluded.holder, acquired_at=excluded.acquired_at, "
        "expires_at=excluded.expires_at WHERE re0_unlock_lease.expires_at <= ?",
        (str(lease_key), holder[:160], now, now + ttl, now),
    )
    return bool(cursor.rowcount)


def release_unlock_lease(conn: sqlite3.Connection, lease_key: str, *, holder: str) -> None:
    """Give it back. Scoped to the holder so a slow caller whose lease has
    already been taken over cannot release somebody else's."""
    conn.execute("DELETE FROM re0_unlock_lease WHERE lease_key=? AND holder=?",
                 (str(lease_key), holder[:160]))


def materialized_link(conn: sqlite3.Connection, resource_id: int):
    """The local link this RE0 resource already produced, if any. Read again
    after waiting on a lease: the holder may have just created it, and then
    there is nothing left to buy."""
    return conn.execute(
        "SELECT l.public_id, l.id FROM re0_resource_link rl JOIN resource_link l ON l.id = rl.resource_link_id "
        "WHERE rl.re0_resource_id=? ORDER BY rl.linked_at DESC LIMIT 1",
        (resource_id,),
    ).fetchone()


def action_put(conn: sqlite3.Connection, request_id: str, resource_id: int, action: str, status: str, *, result_code: str | None,
               resource_link_id: int | None, already_owned: bool, unlock_points: int | None, now: int) -> None:
    conn.execute(
        "INSERT INTO re0_action(request_id, re0_resource_id, action, status, result_code, resource_link_id, already_owned, unlock_points, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id, re0_resource_id) DO UPDATE SET status=excluded.status, result_code=excluded.result_code, "
        "resource_link_id=excluded.resource_link_id, already_owned=excluded.already_owned, unlock_points=excluded.unlock_points, updated_at=excluded.updated_at",
        (request_id, resource_id, action, status, result_code, resource_link_id, int(already_owned), unlock_points, now, now),
    )


def parse_unlock_payload(data: object, slug: str) -> dict:
    """Pick this slug's entry out of a single or batch unlock response;
    returns ``{url, access_code, already_owned, points}`` (url may be None)."""
    entry = None
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        for it in data["items"]:
            if isinstance(it, dict) and (it.get("slug") == slug or entry is None):
                entry = it
                if it.get("slug") == slug:
                    break
    elif isinstance(data, dict):
        entry = data
    entry = entry or {}
    url = None
    for key in ("url", "full_url", "media_url"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://", "ed2k://", "magnet:")):
            url = value.strip()
            break
    code = entry.get("access_code")
    points = None
    for key in ("points_cost", "unlock_points", "points", "cost"):
        if _as_int(entry.get(key)) is not None:
            points = _as_int(entry.get(key))
            break
    return {"url": url, "access_code": code.strip() if isinstance(code, str) and code.strip() else None,
            "already_owned": entry.get("already_owned") is True, "points": points}


# ---------------------------------------------------------------------------
# §5 refresh-existing: ordered backfill of local media by TMDB id (resumable
# cursor, bounded requests, run records). Read-only against RE0 -- never
# unlocks -- and never touches existing resource_link rows.
# ---------------------------------------------------------------------------

# Round 26 (§ file-list preview): a read-only look inside one share, so a
# 合集包 can be told from a single episode BEFORE anyone spends points. The
# endpoint returns display fields only -- no share link, no access code --
# and this wrapper still strips anything link-shaped out of the free text it
# passes on, and never returns or logs the slug.
FILE_LIST_MAX_FILES = 200


def _file_row(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    size = raw.get("size")
    return {
        "name": sanitize_message(name)[:300],
        "path": sanitize_message(raw.get("path"))[:500] or None,
        "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
        "extension": str(raw.get("extension"))[:20] if isinstance(raw.get("extension"), str) else None,
    }


def fetch_file_list(store, client, *, resource_id: int, now: int) -> dict:
    """One ``/api/open/resources/file-list/<slug>`` preview for a candidate
    already in ``re0_resource``. Read-only: it never unlocks, never writes a
    link, and costs no points -- just one paced, budgeted RE0 request."""
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT id, slug_ciphertext, provider_code, media_type, tmdb_id FROM re0_resource WHERE id=?",
                           (int(resource_id),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "resource_id": int(resource_id), "error_class": "unknown_resource", "message": "候选不存在"}
    try:
        slug = store.fernet.decrypt(row["slug_ciphertext"]).decode("utf-8")
    except Exception:  # noqa: BLE001 -- never leak the ciphertext or the key
        return {"ok": False, "resource_id": int(row["id"]), "error_class": "slug_unreadable", "message": "候选标识无法解密"}
    result = client.get(f"/api/open/resources/file-list/{slug}")
    if not result.ok:
        LOG.info("re0 file-list resource_id=%s status=%s error=%s", row["id"], result.status, result.error_class)
        return {"ok": False, "resource_id": int(row["id"]), "error_class": result.error_class, "code": result.code,
                "message": result.message, "retry_after": result.retry_after}
    data = result.data if isinstance(result.data, dict) else {}
    files = [f for f in (_file_row(r) for r in (data.get("files") or [])) if f is not None]
    count = _as_int(data.get("file_count"))
    LOG.info("re0 file-list resource_id=%s files=%s", row["id"], len(files))
    return {
        "ok": True, "resource_id": int(row["id"]), "media_type": row["media_type"], "tmdb_id": int(row["tmdb_id"]),
        "provider": map_pan_type(data.get("provider")) if data.get("provider") is not None else row["provider_code"],
        "list_type": str(data.get("list_type"))[:40] if isinstance(data.get("list_type"), str) else None,
        "share_title": sanitize_message(data.get("share_title"))[:300] or None,
        "file_count": count if count is not None else len(files),
        "result_type": str(data.get("result_type"))[:40] if isinstance(data.get("result_type"), str) else None,
        "validate_status": str(data.get("resource_validate_status"))[:40] if isinstance(data.get("resource_validate_status"), str) else None,
        "validate_message": sanitize_message(data.get("resource_validate_message"))[:300] or None,
        "files": files[:FILE_LIST_MAX_FILES], "truncated": len(files) > FILE_LIST_MAX_FILES,
    }


def _json_type_name(value: object) -> str:
    return {bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "dict", type(None): "null"}.get(type(value), type(value).__name__)


def probe_resources(store, client, *, media_type: str, tmdb_id: int, salt: str, now: int) -> dict:
    """Round 27 diagnostic: one read-only ``resources/{type}/{tmdb_id}`` call,
    reported item by item next to what ``re0_resource`` already holds -- so a
    share the app never captured (an unmapped ``pan_type``, an item dropped as
    invalid, or one the upstream only added later) is visible without guessing.

    Writes nothing. Slugs, links and credentials never reach the report."""
    conn = store.connect(readonly=True)
    try:
        local_media_id = local_media_id_for(conn, media_type, tmdb_id)
        stored_rows = conn.execute(
            "SELECT slug_hash, provider_code, upstream_pan_type, state, spec_json, last_seen_at FROM re0_resource "
            "WHERE media_type=? AND tmdb_id=? ORDER BY id", (media_type, tmdb_id)
        ).fetchall()
    finally:
        conn.close()
    result = client.get(f"/api/open/resources/{media_type}/{tmdb_id}")
    base = {"phase": "probe-resources", "media_type": media_type, "tmdb_id": int(tmdb_id), "local_media_id": local_media_id}
    if not result.ok:
        LOG.info("re0 probe tmdb_id=%s type=%s status=%s error=%s", tmdb_id, media_type, result.status, result.error_class)
        return {**base, "ok": False, "status": result.status, "error_class": result.error_class, "code": result.code,
                "message": result.message, "retry_after": result.retry_after}
    raw_items = result.data if isinstance(result.data, list) else ((result.data or {}).get("items") if isinstance(result.data, dict) else [])
    raw_items = raw_items or []
    known_hashes = {r["slug_hash"] for r in stored_rows}
    seen_hashes = set()
    items: list[dict] = []
    invalid = 0
    unmapped: list[str] = []
    for raw in raw_items:
        item = normalize_item(raw, salt=salt)
        if item is None:
            invalid += 1
            continue
        seen_hashes.add(item["slug_hash"])
        pan_raw = raw.get("pan_type") if isinstance(raw, dict) else None
        if item["provider_code"] == "unknown" and item["upstream_pan_type"] and item["upstream_pan_type"] not in unmapped:
            unmapped.append(item["upstream_pan_type"])
        spec = item["spec"]
        items.append({
            "pan_type_raw": item["upstream_pan_type"], "pan_type_json_type": _json_type_name(pan_raw),
            "provider_code": item["provider_code"], "title": item["title"], "share_size": spec["raw"].get("share_size"),
            "specs": {k: v for k, v in (("resolution", spec["quality"]), ("source", spec["source_type"]), ("hdr", spec["hdr"])) if v},
            "is_unlocked": item["is_unlocked"], "validate_status": item["validate_status"],
            "unlock_points": item["unlock_points"], "already_stored": item["slug_hash"] in known_hashes,
        })
    stored = []
    for row in stored_rows:
        try:
            raw_spec = (json.loads(row["spec_json"] or "{}") or {}).get("raw") or {}
        except ValueError:
            raw_spec = {}
        stored.append({
            "provider_code": row["provider_code"], "upstream_pan_type": row["upstream_pan_type"], "state": row["state"],
            "has_title": bool(raw_spec.get("title")), "in_upstream": row["slug_hash"] in seen_hashes,
            "last_seen_at": _iso(row["last_seen_at"]),
        })
    LOG.info("re0 probe tmdb_id=%s type=%s raw=%s invalid=%s unmapped=%s", tmdb_id, media_type, len(raw_items), invalid, unmapped)
    return {
        **base, "ok": True, "raw_items": len(raw_items), "invalid_items": invalid, "items": items,
        "unmapped_pan_types": unmapped, "stored": stored,
        "stored_missing_from_upstream": sum(1 for s in stored if not s["in_upstream"]),
        "upstream_not_yet_stored": sum(1 for i in items if not i["already_stored"]),
        "data_shape": "list" if isinstance(result.data, list) else _json_type_name(result.data),
    }


PREVIEW_READY_TTL_SECONDS = 12 * 3600
PREVIEW_INVALID_TTL_SECONDS = 5 * 60
# A pan that cannot preview at all, or an account tier that may not, is a
# stable fact -- cache it so the button stops asking. Transient failures
# (429/5xx/network) are never cached.
PREVIEW_STABLE_TTL_SECONDS = 12 * 3600
_PREVIEW_LOCKS: dict = {}
_PREVIEW_LOCKS_GUARD = threading.Lock()
_PREVIEW_ERROR_STATUS = {"user_level_denied": "forbidden", "scope_denied": "forbidden", "upstream_4xx": "unsupported"}
_PREVIEW_MESSAGES = {
    "forbidden": "当前账号等级不支持文件预览",
    "unsupported": "该来源暂不支持文件预览",
    "error": "文件预览暂不可用",
}


def _preview_lock(resource_id: int):
    with _PREVIEW_LOCKS_GUARD:
        lock = _PREVIEW_LOCKS.get(resource_id)
        if lock is None:
            lock = _PREVIEW_LOCKS[resource_id] = threading.Lock()
        return lock


def _preview_row(conn: sqlite3.Connection, resource_id: int):
    return conn.execute("SELECT * FROM re0_file_preview WHERE re0_resource_id=?", (int(resource_id),)).fetchone()


def _preview_payload(row, *, cached: bool) -> dict:
    try:
        files = json.loads(row["files_json"] or "[]")
    except ValueError:
        files = []
    try:
        composition = json.loads(row["composition_json"] or "{}")
    except ValueError:
        composition = {}
    return {
        "resource_id": int(row["re0_resource_id"]), "status": row["status"], "cached": cached,
        "file_count": row["file_count"], "files": files, "composition": composition or None,
        "truncated": bool(row["truncated"]), "validate_status": row["validate_status"],
        "validate_message": row["validate_message"], "error_class": row["error_class"],
        "message": _PREVIEW_MESSAGES.get(row["status"]) if row["status"] not in ("ready", "invalid") else None,
        "fetched_at": _iso(row["fetched_at"]),
    }


def _preview_store(store, resource_id: int, *, status: str, now: int, ttl: int, file_count=None, files=None,
                   composition=None, validate_status=None, validate_message=None, truncated=False, error_class=None) -> None:
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO re0_file_preview(re0_resource_id, status, file_count, files_json, composition_json, validate_status, "
            "validate_message, truncated, fetched_at, expires_at, error_class) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(re0_resource_id) DO UPDATE SET status=excluded.status, file_count=excluded.file_count, "
            "files_json=excluded.files_json, composition_json=excluded.composition_json, validate_status=excluded.validate_status, "
            "validate_message=excluded.validate_message, truncated=excluded.truncated, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at, error_class=excluded.error_class",
            (int(resource_id), status, file_count, json.dumps(files or [], ensure_ascii=False),
             json.dumps(composition or {}, ensure_ascii=False), validate_status, validate_message, int(bool(truncated)),
             now, now + ttl, error_class),
        )
        conn.commit()
    finally:
        conn.close()


def file_preview(store, client, *, resource_id: int, now: int, force: bool = False) -> dict:
    """Work order §4.2/§5.2: what is inside one share, on demand.

    Read-only against RE0 -- one paced GET, no unlock, no points, no link ever
    written or returned. A ready preview is cached 12h, a confirmed-invalid
    share 5 minutes, a pan/account that simply cannot preview 12h; transient
    failures are not cached. Concurrent callers for the same candidate share
    one upstream request."""
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT id, slug_ciphertext, provider_code, media_type, tmdb_id, spec_json, state FROM re0_resource WHERE id=?",
                           (int(resource_id),)).fetchone()
    finally:
        conn.close()
    if row is None or row["state"] == "permanent_error":
        return {"resource_id": int(resource_id), "status": "error", "cached": False, "files": [], "file_count": None,
                "composition": None, "error_class": "unknown_resource", "message": "候选不存在"}
    if not supports_file_preview(row["provider_code"]):
        # §3.4: a protocol link has no directory to list. Answer without
        # touching RE0 and without writing a cache row -- "no preview" must
        # never become evidence that the share is dead.
        return {"resource_id": int(row["id"]), "status": "unsupported", "cached": False, "files": [], "file_count": None,
                "composition": None, "error_class": "preview_not_applicable", "message": "协议链接无需文件预览"}
    with _preview_lock(int(resource_id)):
        conn = store.connect(readonly=True)
        try:
            cached_row = _preview_row(conn, resource_id)
        finally:
            conn.close()
        if cached_row is not None and not force and int(cached_row["expires_at"]) > now:
            return _preview_payload(cached_row, cached=True)
        try:
            slug = store.fernet.decrypt(row["slug_ciphertext"]).decode("utf-8")
        except Exception:  # noqa: BLE001 -- never leak the ciphertext or the key
            return {"resource_id": int(row["id"]), "status": "error", "cached": False, "files": [], "file_count": None,
                    "composition": None, "error_class": "slug_unreadable", "message": "候选标识无法解密"}
        result = client.get(f"/api/open/resources/file-list/{slug}")
        try:
            spec_raw = (json.loads(row["spec_json"] or "{}") or {}).get("raw") or {}
        except ValueError:
            spec_raw = {}
        if not result.ok:
            status = _PREVIEW_ERROR_STATUS.get(result.error_class or "", "error")
            LOG.info("re0 file-preview resource_id=%s status=%s error=%s", row["id"], result.status, result.error_class)
            if status in ("forbidden", "unsupported"):
                _preview_store(store, row["id"], status=status, now=now, ttl=PREVIEW_STABLE_TTL_SECONDS, error_class=result.error_class)
            return {"resource_id": int(row["id"]), "status": status, "cached": False, "files": [], "file_count": None,
                    "composition": None, "error_class": result.error_class, "retry_after": result.retry_after,
                    "message": _PREVIEW_MESSAGES.get(status, "文件预览暂不可用")}
        data = result.data if isinstance(result.data, dict) else {}
        files = [f for f in (_file_row(r) for r in (data.get("files") or [])) if f is not None]
        truncated = len(files) > FILE_LIST_MAX_FILES
        files = files[:FILE_LIST_MAX_FILES]
        reported = _as_int(data.get("file_count"))
        count = reported if reported is not None else len(files)
        validate_status = str(data.get("resource_validate_status"))[:40] if isinstance(data.get("resource_validate_status"), str) else None
        validate_message = _clean_text(data.get("resource_validate_message"), 300)
        composition = parse_composition(
            remark=spec_raw.get("remark"), title=spec_raw.get("title"),
            share_title=_clean_text(data.get("share_title"), 300),
            file_names=[f["name"] for f in files], file_count=count,
        )
        invalid = not files and str(data.get("result_type") or "").lower() == "validation"
        status = "invalid" if invalid else "ready"
        _preview_store(store, row["id"], status=status, now=now,
                       ttl=PREVIEW_INVALID_TTL_SECONDS if invalid else PREVIEW_READY_TTL_SECONDS,
                       file_count=count, files=files, composition=composition, validate_status=validate_status,
                       validate_message=validate_message, truncated=truncated)
        LOG.info("re0 file-preview resource_id=%s files=%s status=%s", row["id"], len(files), status)
        conn = store.connect(readonly=True)
        try:
            return _preview_payload(_preview_row(conn, row["id"]), cached=False)
        finally:
            conn.close()


REFRESH_TTL_SECONDS = 7 * 86400
# The display-field backfill marker. A candidate last seen before this instant
# predates the newest display field, so its media is due for one refresh even
# inside the TTL; a refresh moves last_seen_at past the marker, so each media
# qualifies at most once and a share whose upstream genuinely has nothing to
# say is never re-requested.
#
# MOVE THIS whenever a new field joins spec_json.raw, or candidates refreshed
# between the two releases keep the gap forever.
#   release U 2026-09-10T01:48:13Z  1789004893  title, share_size
#   release Z 2026-09-10T11:33:28Z  1789040008  remark, created_at, publisher,
#                                               is_official, unlocked_users_count,
#                                               last_validated_at, validate_message
DISPLAY_FIELDS_EPOCH = 1789040008
_STOP_ON = {"rate_limited", "reauth_required", "refresh_unavailable", "scope_denied", "user_level_denied", "quota_exhausted",
            "missing_credentials", "network_error", "upstream_5xx", "invalid_json"}


def select_refresh_queue(conn: sqlite3.Connection, *, now: int, limit: int, ttl: int, cursor: int,
                         media_ids: list[int] | None = None, tmdb_ids: list[int] | None = None) -> list:
    """Spec §5.1 order: exact media with a high invalid/unknown link ratio,
    then exact never/long-unsynced, then candidate/needs_review rows that
    carry a TMDB id (evidence only), then the rest -- each bucket by
    ``media.id`` so a cursor (the last media id handled) resumes a pass."""
    where = ["m.tmdb_id IS NOT NULL", "m.media_type IN ('movie','tv')", "m.id > ?"]
    params: list = [cursor]
    if media_ids:
        where.append("m.id IN (%s)" % ",".join("?" for _ in media_ids)); params.extend(int(i) for i in media_ids)
    if tmdb_ids:
        where.append("m.tmdb_id IN (%s)" % ",".join("?" for _ in tmdb_ids)); params.extend(int(i) for i in tmdb_ids)
    # An explicitly targeted media is what the caller asked for: the TTL is a
    # pacing rule for the automatic pass, not a veto on a direct request.
    if media_ids or tmdb_ids:
        due_clause = "1=1"
        params_tail = [limit]
    elif now >= DISPLAY_FIELDS_EPOCH:
        # Due when the TTL has expired, OR when this media still holds a
        # candidate recorded before the display fields were captured. The
        # clock guard keeps the migration meaningless before its own release
        # instant (and keeps fixed-time fixtures on the plain TTL rule).
        due_clause = ("(p.last_fetched_at IS NULL OR p.last_fetched_at <= ? OR EXISTS ("
                      "SELECT 1 FROM re0_resource r WHERE r.media_type = m.media_type AND r.tmdb_id = m.tmdb_id "
                      "AND r.last_seen_at < ?))")
        params_tail = [now - ttl, DISPLAY_FIELDS_EPOCH, limit]
    else:
        due_clause = "(p.last_fetched_at IS NULL OR p.last_fetched_at <= ?)"
        params_tail = [now - ttl, limit]
    sql = f"""
        SELECT m.id AS id, m.media_type AS media_type, m.tmdb_id AS tmdb_id, m.title_zh AS title, m.match_status AS match_status,
               COALESCE(p.last_fetched_at, 0) AS last_fetched_at,
               (SELECT COUNT(*) FROM resource_group g JOIN resource_link l ON l.group_id = g.id WHERE g.media_id = m.id AND l.deleted_at_source IS NULL) AS live_links,
               (SELECT COUNT(*) FROM resource_group g JOIN resource_link l ON l.group_id = g.id
                  JOIN link_check lc ON lc.provider = l.provider AND lc.canonical_url_hash = l.canonical_url_hash
                  WHERE g.media_id = m.id AND lc.status IN ('invalid','unknown')) AS bad_links
        FROM media m LEFT JOIN re0_media_projection p ON p.media_type = m.media_type AND p.tmdb_id = m.tmdb_id
        WHERE {' AND '.join(where)} AND {due_clause}
        ORDER BY CASE
            WHEN m.match_status = 'exact' AND live_links > 0 AND bad_links * 2 >= live_links THEN 0
            WHEN m.match_status = 'exact' THEN 1
            WHEN m.match_status IN ('candidate', 'needs_review') THEN 2
            ELSE 3 END, m.id ASC
        LIMIT ?
    """
    return conn.execute(sql, params + params_tail).fetchall()


def _run_open(store, phase: str, now: int) -> int:
    conn = store.connect()
    try:
        cur = conn.execute("INSERT INTO re0_sync_run(phase, started_at, status) VALUES(?,?,'running')", (phase, now))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _run_close(store, run_id: int, report: dict, now: int) -> None:
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE re0_sync_run SET finished_at=?, requested=?, succeeded=?, failed=?, skipped=?, unlocked=0, status=?, error_class=? WHERE id=?",
            (now, report["requested"], report["succeeded"], report["failed"], report["skipped"], report["status"], report["error_class"], run_id),
        )
        conn.commit()
    finally:
        conn.close()


def refresh_existing(store, client, *, limit: int, max_requests: int, salt: str, now: int, ttl: int = REFRESH_TTL_SECONDS,
                     media_ids: list[int] | None = None, tmdb_ids: list[int] | None = None, resume: bool = True, dry_run: bool = False) -> dict:
    report = {"phase": "refresh-existing", "dry_run": dry_run, "requested": 0, "succeeded": 0, "failed": 0, "skipped": 0, "unlocked": 0,
              "candidates_new": 0, "candidates_seen": 0, "materialized": 0, "status": "completed", "error_class": None, "cursor": 0}
    conn = store.connect(readonly=True)
    try:
        cursor = int(state_get(conn, "refresh_cursor") or 0) if (resume and not media_ids and not tmdb_ids) else 0
        rows = [dict(r) for r in select_refresh_queue(conn, now=now, limit=limit, ttl=ttl, cursor=cursor, media_ids=media_ids, tmdb_ids=tmdb_ids)]
    finally:
        conn.close()
    if dry_run:
        report.update({"would_request": min(len(rows), max_requests), "cursor": cursor, "queue_sample": [r["id"] for r in rows[:10]]})
        return report
    run_id = _run_open(store, "refresh-existing", now)
    last_ok = cursor
    for row in rows:
        if report["requested"] >= max_requests:
            report["status"] = "budget_reached"
            break
        report["requested"] += 1
        result = client.get(f"/api/open/resources/{row['media_type']}/{row['tmdb_id']}")
        if not result.ok:
            report["failed"] += 1
            if result.error_class in _STOP_ON:
                report["status"], report["error_class"] = "stopped", result.error_class
                break
            report["skipped"] += 1
            continue
        raw_items = result.data if isinstance(result.data, list) else ((result.data or {}).get("items") if isinstance(result.data, dict) else [])
        item_report = record_items(store, row["media_type"], int(row["tmdb_id"]), raw_items or [], media_id=int(row["id"]), media_title=row["title"], salt=salt, now=now)
        upsert_projection(store, row["media_type"], int(row["tmdb_id"]), title=row["title"], year=None, overview=None, poster_path=None,
                          backdrop_path=None, ratings={}, now=now, local_media_id=int(row["id"]))
        conn = store.connect()
        try:
            conn.execute("UPDATE re0_media_projection SET last_fetched_at=?, updated_at=? WHERE media_type=? AND tmdb_id=?",
                         (now, now, row["media_type"], int(row["tmdb_id"])))
            conn.commit()
        finally:
            conn.close()
        report["succeeded"] += 1
        report["candidates_new"] += item_report["new"]
        report["candidates_seen"] += item_report["seen"]
        report["materialized"] += item_report["materialized"]
        last_ok = int(row["id"])
        LOG.info("re0 refresh media_id=%s type=%s items=%s new=%s", row["id"], row["media_type"], item_report["remote_items"], item_report["new"])
    pass_complete = report["status"] == "completed" and len(rows) < limit
    if not media_ids and not tmdb_ids:
        conn = store.connect()
        try:
            state_set(conn, "refresh_cursor", "0" if pass_complete else str(last_ok), now)
            if pass_complete:
                state_set(conn, "refresh_pass_completed_at", str(now), now)
            state_set(conn, "refresh_last_run_at", str(now), now)
            conn.commit()
        finally:
            conn.close()
    report["cursor"] = 0 if pass_complete else last_ok
    _run_close(store, run_id, report, now)
    return report


def reconcile_report(store, client, *, limit: int, salt: str, now: int, dry_run: bool) -> dict:
    """§7: how many review/candidate/unmatched rows could be checked against
    RE0 (evidence only -- match_status is never changed here)."""
    report = {"phase": "reconcile", "dry_run": dry_run, "known_tmdb_ids": 0, "no_tmdb_id": 0, "queried": 0, "remote_items": 0, "new_slugs": 0,
              "already_owned": 0, "ambiguous": 0, "rate_limited": 0}
    conn = store.connect(readonly=True)
    try:
        report["known_tmdb_ids"] = conn.execute(
            "SELECT COUNT(*) FROM media WHERE match_status IN ('candidate','needs_review','unmatched') AND tmdb_id IS NOT NULL").fetchone()[0]
        report["no_tmdb_id"] = conn.execute(
            "SELECT COUNT(*) FROM media WHERE match_status IN ('candidate','needs_review','unmatched') AND tmdb_id IS NULL").fetchone()[0]
        report["ambiguous"] = conn.execute(
            "SELECT COUNT(*) FROM media WHERE match_status IN ('candidate','needs_review') AND match_candidates_json IS NOT NULL "
            "AND match_candidates_json NOT IN ('', '[]') AND tmdb_id IS NULL").fetchone()[0]
        rows = [] if dry_run else [dict(r) for r in conn.execute(
            "SELECT id, media_type, tmdb_id, title_zh FROM media WHERE match_status IN ('candidate','needs_review','unmatched') AND tmdb_id IS NOT NULL "
            "AND media_type IN ('movie','tv') ORDER BY id LIMIT ?", (limit,))]
    finally:
        conn.close()
    for row in rows:
        result = client.get(f"/api/open/resources/{row['media_type']}/{row['tmdb_id']}")
        report["queried"] += 1
        if not result.ok:
            if result.error_class == "rate_limited":
                report["rate_limited"] += 1
                break
            continue
        raw_items = result.data if isinstance(result.data, list) else []
        item_report = record_items(store, row["media_type"], int(row["tmdb_id"]), raw_items, media_id=int(row["id"]), media_title=row["title_zh"], salt=salt, now=now)
        report["remote_items"] += item_report["remote_items"]
        report["new_slugs"] += item_report["new"]
        report["already_owned"] += sum(1 for it in raw_items if isinstance(it, dict) and it.get("is_unlocked") is True)
    return report


def run_status(conn: sqlite3.Connection, now: int | None = None) -> dict:
    now = int(now if now is not None else time.time())
    last = conn.execute("SELECT * FROM re0_sync_run ORDER BY id DESC LIMIT 1").fetchone()
    cursor = int(state_get(conn, "refresh_cursor") or 0)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM media m LEFT JOIN re0_media_projection p ON p.media_type = m.media_type AND p.tmdb_id = m.tmdb_id "
        "WHERE m.tmdb_id IS NOT NULL AND m.media_type IN ('movie','tv') AND m.id > ? AND (p.last_fetched_at IS NULL OR p.last_fetched_at <= ?)",
        (cursor, now - REFRESH_TTL_SECONDS),
    ).fetchone()[0]
    return {
        "last_run": dict(last) if last else None, "cursor": cursor, "queue_remaining": int(remaining),
        "last_pass_completed_at": int(state_get(conn, "refresh_pass_completed_at") or 0) or None,
        "last_run_at": int(state_get(conn, "refresh_last_run_at") or 0) or None,
    }


# ---------------------------------------------------------------------------
# §6.1 calendar projection, §9.1 next-episode CTA, §6.2 streaming-top,
# bounded discovery for projections without a local media row.
# ---------------------------------------------------------------------------

from zoneinfo import ZoneInfo  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")
_WEEKDAYS = "一二三四五六日"
TOP_PROVIDERS = {"netflix", "hbo", "apple", "disney", "crunchyroll", "prime", "amazon", "hulu"}
TOP_REGIONS = {"US", "KR", "GB", "DE"}
_TMDB_IMAGE_RE = None


def _ensure_calendar_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
CREATE TABLE IF NOT EXISTS re0_calendar_event (
  event_key TEXT PRIMARY KEY,
  media_type TEXT NOT NULL,
  tmdb_id INTEGER,
  media_id INTEGER,
  first_aired TEXT NOT NULL,
  first_aired_epoch INTEGER,
  season INTEGER,
  episode INTEGER,
  episode_title TEXT,
  event_json TEXT NOT NULL DEFAULT '{}',
  fetched_at INTEGER NOT NULL,
  UNIQUE (media_type, tmdb_id, first_aired, season, episode)
);
CREATE INDEX IF NOT EXISTS idx_re0_calendar_media ON re0_calendar_event(media_type, tmdb_id, first_aired_epoch);
""")


def _parse_rfc3339(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return int(dt.timestamp())


def _shanghai_day(now: int) -> str:
    return datetime.fromtimestamp(now, tz=SHANGHAI).date().isoformat()


def _image_path(url: object) -> str | None:
    """TMDB image URL -> path (``/abc.jpg``); anything else is dropped so
    the browser only ever gets server-built image addresses."""
    global _TMDB_IMAGE_RE
    if not isinstance(url, str):
        return None
    if _TMDB_IMAGE_RE is None:
        import re
        _TMDB_IMAGE_RE = re.compile(r"^https?://image\.tmdb\.org/t/p/[A-Za-z0-9_]+(/[A-Za-z0-9_.-]+)$")
    if url.startswith("/") and "/" not in url[1:]:
        return url
    m = _TMDB_IMAGE_RE.match(url)
    return m.group(1) if m else None


def record_calendar(store, items: list, now: int) -> dict:
    report = {"items": len(items or []), "recorded": 0, "duplicates": 0, "no_tmdb_id": 0, "linked_local": 0, "new_projections": 0}
    conn = store.connect()
    try:
        _ensure_calendar_table(conn)
        pending_projections = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            show = item.get("show") if isinstance(item.get("show"), dict) else None
            movie = item.get("movie") if isinstance(item.get("movie"), dict) else None
            episode = item.get("episode") if isinstance(item.get("episode"), dict) else {}
            node = show or movie
            media_type = "tv" if show else ("movie" if movie else None)
            ids = node.get("ids") if node and isinstance(node.get("ids"), dict) else {}
            tmdb_id = _as_int(ids.get("tmdb"))
            first_aired = item.get("first_aired") or episode.get("first_aired") or (movie or {}).get("released")
            epoch = _parse_rfc3339(first_aired)
            if media_type is None or tmdb_id is None or tmdb_id <= 0 or epoch is None:
                report["no_tmdb_id"] += 1
                continue
            season = _as_int(episode.get("season")) if media_type == "tv" else None
            number = _as_int(episode.get("number")) if media_type == "tv" else None
            key = hashlib.sha256(f"{media_type}|{tmdb_id}|{first_aired}|{season}|{number}".encode("utf-8")).hexdigest()[:32]
            media_id = local_media_id_for(conn, media_type, tmdb_id)
            title = node.get("title") if isinstance(node.get("title"), str) else None
            metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
            event_json = json.dumps({"show_title": title, "year": _as_int(node.get("year")), "imdb": ids.get("imdb") if isinstance(ids.get("imdb"), str) else None},
                                    ensure_ascii=False)
            cur = conn.execute(
                "INSERT OR IGNORE INTO re0_calendar_event(event_key, media_type, tmdb_id, media_id, first_aired, first_aired_epoch, season, episode, episode_title, event_json, fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, media_type, tmdb_id, media_id, str(first_aired), epoch, season, number,
                 str(episode.get("title"))[:200] if isinstance(episode.get("title"), str) else None, event_json, now),
            )
            if cur.rowcount != 1:
                report["duplicates"] += 1
                continue
            report["recorded"] += 1
            if media_id is not None:
                report["linked_local"] += 1
            else:
                pending_projections.append((media_type, tmdb_id, title or f"TMDB {tmdb_id}", _as_int(node.get("year")),
                                            str(metadata.get("overview") or "")[:2000] or None, _image_path(metadata.get("poster_path") or metadata.get("poster_url")),
                                            _image_path(metadata.get("backdrop_path") or metadata.get("backdrop_url"))))
        conn.commit()
        existing = {(r[0], r[1]) for r in conn.execute("SELECT media_type, tmdb_id FROM re0_media_projection")}
    finally:
        conn.close()
    for media_type, tmdb_id, title, year, overview, poster, backdrop in pending_projections:
        if (media_type, tmdb_id) not in existing:
            report["new_projections"] += 1
            existing.add((media_type, tmdb_id))
        upsert_projection(store, media_type, tmdb_id, title=title, year=year, overview=overview, poster_path=poster, backdrop_path=backdrop, ratings={}, now=now)
    return report


def next_event_for(conn: sqlite3.Connection, media_type: str, tmdb_id: int, *, now: int, local_max_season: int | None) -> dict | None:
    try:
        row = conn.execute(
            "SELECT * FROM re0_calendar_event WHERE media_type=? AND tmdb_id=? AND first_aired_epoch >= ? ORDER BY first_aired_epoch ASC LIMIT 1",
            (media_type, tmdb_id, now),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    when = datetime.fromtimestamp(int(row["first_aired_epoch"]), tz=SHANGHAI)
    display = f"{when.month}月{when.day}日周{_WEEKDAYS[when.weekday()]} {when.strftime('%H:%M')}"
    season, episode = row["season"], row["episode"]
    is_new_season = bool(media_type == "tv" and season is not None and local_max_season is not None and season > local_max_season)
    if media_type == "movie":
        label = "上映"
    elif is_new_season:
        label = f"新一季 · S{season:02d}"
    elif season is not None and episode is not None:
        label = f"下一集 S{season:02d}E{episode:02d}"
    else:
        label = "下一集"
    return {"media_type": media_type, "season": season, "episode": episode, "episode_title": row["episode_title"], "first_aired": row["first_aired"],
            "display": display, "label": label, "is_new_season": is_new_season}


def discover_calendar(store, client, *, days: int, now: int) -> dict:
    day = _shanghai_day(now)
    conn = store.connect()
    try:
        _ensure_calendar_table(conn)
        if state_get(conn, f"calendar_checked:{day}"):
            return {"phase": "discover-calendar", "fetched": False, "skipped_reason": "already_today"}
    finally:
        conn.close()
    result = client.get("/api/open/calendar", params={"days": max(1, min(int(days), 31))})
    if not result.ok:
        return {"phase": "discover-calendar", "fetched": False, "error_class": result.error_class, "retry_after": result.retry_after}
    items = result.data.get("items") if isinstance(result.data, dict) else (result.data if isinstance(result.data, list) else [])
    report = record_calendar(store, items or [], now)
    conn = store.connect()
    try:
        state_set(conn, f"calendar_checked:{day}", "1", now)
        state_set(conn, "calendar_last_fetched_at", str(now), now)
        conn.commit()
    finally:
        conn.close()
    return {"phase": "discover-calendar", "fetched": True, **report}


def calendar_status(conn: sqlite3.Connection, now: int) -> dict:
    try:
        events = conn.execute("SELECT COUNT(*) FROM re0_calendar_event").fetchone()[0]
        upcoming = conn.execute("SELECT COUNT(*) FROM re0_calendar_event WHERE first_aired_epoch >= ?", (now,)).fetchone()[0]
    except sqlite3.OperationalError:
        events = upcoming = 0
    last = state_get(conn, "calendar_last_fetched_at")
    return {"events": int(events), "upcoming": int(upcoming), "last_fetched_at": int(last) if last else None}


def record_streaming_top(store, items: list, now: int) -> dict:
    report = {"items": len(items or []), "projected": 0, "linked_local": 0, "no_tmdb_id": 0}
    conn = store.connect(readonly=True)
    try:
        prepared = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            tmdb_id = _as_int(item.get("tmdb_id"))
            media_type = item.get("media_type")
            if tmdb_id is None or tmdb_id <= 0 or media_type not in ("movie", "tv"):
                report["no_tmdb_id"] += 1
                continue
            local_id = local_media_id_for(conn, media_type, tmdb_id)
            prepared.append((media_type, tmdb_id, local_id, item))
    finally:
        conn.close()
    for media_type, tmdb_id, local_id, item in prepared:
        rating = item.get("rating")
        upsert_projection(
            store, media_type, tmdb_id, title=str(item.get("title") or item.get("source_title") or f"TMDB {tmdb_id}")[:200],
            original_title=str(item.get("original_title"))[:200] if isinstance(item.get("original_title"), str) else None,
            year=_as_int(item.get("year")), overview=str(item.get("overview") or "")[:2000] or None,
            poster_path=_image_path(item.get("poster_url")), backdrop_path=_image_path(item.get("backdrop_url")),
            ratings={"tmdb": {"score": round(float(rating), 1), "votes": 0}} if isinstance(rating, (int, float)) and rating else {},
            now=now, local_media_id=local_id,
        )
        report["projected"] += 1
        if local_id is not None:
            report["linked_local"] += 1
    return report


def parse_top_sources(raw: object) -> list[tuple[str, str, str]]:
    """``"netflix:US:tv,hbo:GB:movie"`` -> validated (provider, region, type) tuples."""
    out = []
    for token in str(raw or "").split(","):
        parts = [p.strip() for p in token.split(":")]
        if len(parts) != 3:
            continue
        provider, region, media_type = parts[0].lower(), parts[1].upper(), parts[2].lower()
        if provider in TOP_PROVIDERS and region in TOP_REGIONS and media_type in ("movie", "tv"):
            out.append((provider, region, media_type))
    return out


def discover_top(store, client, *, sources: list, now: int) -> dict:
    valid = parse_top_sources(",".join(sources) if isinstance(sources, list) else sources)
    report = {"phase": "discover-top", "sources": 0, "invalid_sources": len([s for s in (sources or []) if s]) - len(valid), "skipped_today": 0,
              "projected": 0, "linked_local": 0, "no_tmdb_id": 0, "errors": 0, "error_class": None}
    day = _shanghai_day(now)
    for provider, region, media_type in valid:
        key = f"top_checked:{day}:{provider}:{region}:{media_type}"
        conn = store.connect()
        try:
            if state_get(conn, key):
                report["skipped_today"] += 1
                continue
        finally:
            conn.close()
        result = client.get("/api/open/streaming-top", params={"provider": provider, "region": region, "media_type": media_type})
        if not result.ok:
            report["errors"] += 1
            report["error_class"] = result.error_class
            if result.error_class in _STOP_ON:
                break
            continue
        items = result.data.get("items") if isinstance(result.data, dict) else []
        sub = record_streaming_top(store, items or [], now)
        report["sources"] += 1
        for k in ("projected", "linked_local", "no_tmdb_id"):
            report[k] += sub[k]
        conn = store.connect()
        try:
            state_set(conn, key, "1", now)
            conn.commit()
        finally:
            conn.close()
    return report


def discover_bounded(store, client, *, limit: int, max_requests: int, salt: str, now: int, ttl: int = RESOURCES_TTL_SECONDS) -> dict:
    """RE0 resources for projections that have no local media row yet
    (calendar / streaming-top / user searches) -- candidates only."""
    report = {"phase": "discover-bounded", "requested": 0, "succeeded": 0, "failed": 0, "skipped": 0, "unlocked": 0, "candidates_new": 0,
              "status": "completed", "error_class": None}
    conn = store.connect(readonly=True)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT media_type, tmdb_id, title FROM re0_media_projection WHERE local_media_id IS NULL AND (last_fetched_at IS NULL OR last_fetched_at <= ?) "
            "ORDER BY last_seen_at DESC LIMIT ?", (now - ttl, limit))]
    finally:
        conn.close()
    run_id = _run_open(store, "discover-bounded", now)
    for row in rows:
        if report["requested"] >= max_requests:
            report["status"] = "budget_reached"
            break
        report["requested"] += 1
        result = client.get(f"/api/open/resources/{row['media_type']}/{row['tmdb_id']}")
        if not result.ok:
            report["failed"] += 1
            if result.error_class in _STOP_ON:
                report["status"], report["error_class"] = "stopped", result.error_class
                break
            continue
        raw_items = result.data if isinstance(result.data, list) else []
        sub = record_items(store, row["media_type"], int(row["tmdb_id"]), raw_items, media_id=None, media_title=row["title"], salt=salt, now=now)
        report["succeeded"] += 1
        report["candidates_new"] += sub["new"]
        conn = store.connect()
        try:
            conn.execute("UPDATE re0_media_projection SET last_fetched_at=?, updated_at=? WHERE media_type=? AND tmdb_id=?", (now, now, row["media_type"], int(row["tmdb_id"])))
            conn.commit()
        finally:
            conn.close()
    _run_close(store, run_id, report, now)
    return report


# ---------------------------------------------------------------------------
# §9.2 tv-follow packs: preview-only projection, locked items never retried,
# materialisation only after the user's explicit unlock.
# ---------------------------------------------------------------------------

TV_FOLLOW_COOLDOWN_SECONDS = 86400
_PREVIEW_ITEM_KEYS = ("episode_label", "season", "episode_start", "episode_end", "resolution", "source", "subtitle")


def _ensure_follow_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
CREATE TABLE IF NOT EXISTS re0_tv_follow_pack (
  slug_hash TEXT PRIMARY KEY,
  slug_ciphertext BLOB NOT NULL,
  re0_tv_id TEXT,
  tmdb_id INTEGER,
  media_id INTEGER,
  title TEXT,
  is_unlocked INTEGER NOT NULL DEFAULT 0,
  is_owner INTEGER NOT NULL DEFAULT 0,
  is_completed INTEGER,
  unlock_points INTEGER,
  preview_json TEXT NOT NULL DEFAULT '{}',
  items_status TEXT,
  last_seen_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS re0_tv_follow_item (
  pack_slug_hash TEXT NOT NULL,
  re0_item_id TEXT NOT NULL,
  season INTEGER,
  episode_start INTEGER,
  episode_end INTEGER,
  label TEXT,
  unlocked INTEGER NOT NULL DEFAULT 0,
  resource_link_id INTEGER,
  item_json TEXT NOT NULL DEFAULT '{}',
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (pack_slug_hash, re0_item_id)
);
CREATE INDEX IF NOT EXISTS idx_re0_tv_follow_pack_tmdb ON re0_tv_follow_pack(tmdb_id);
""")
    # G03.5: the pack entry needs the same "result unknown" record the resource
    # entry has, or an uncertain pack unlock is retried as a fresh purchase.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(re0_tv_follow_pack)")}
    if "unlock_state" not in existing:
        conn.execute("ALTER TABLE re0_tv_follow_pack ADD COLUMN unlock_state TEXT")


def _preview_items(raw: object) -> list[dict]:
    out = []
    for item in (raw if isinstance(raw, list) else [])[:50]:
        if isinstance(item, dict):
            out.append({k: item[k] for k in _PREVIEW_ITEM_KEYS if k in item and isinstance(item[k], (str, int)) and not isinstance(item[k], bool)})
    return out


def record_packs(store, packs: list, *, salt: str, tmdb_id: int | None, now: int, force_unlocked: bool = False) -> dict:
    """Project ``tv-follow/packs`` (or ``/my``) rows. ``tmdb_id`` is the query
    context: a pack is mapped to it only when the pack itself says so
    (``tmdb_id`` / ``tv.ids.tmdb``) or its ``tv_id`` literally equals the
    TMDB id we asked for -- never by title."""
    report = {"packs": len(packs or []), "recorded": 0, "invalid": 0, "mapped": 0, "mapping_unknown": 0}
    conn = store.connect()
    try:
        _ensure_follow_tables(conn)
        for pack in packs or []:
            if not isinstance(pack, dict) or not isinstance(pack.get("slug"), str) or not pack["slug"].strip():
                report["invalid"] += 1
                continue
            slug = pack["slug"].strip()
            tv = pack.get("tv") if isinstance(pack.get("tv"), dict) else {}
            ids = tv.get("ids") if isinstance(tv.get("ids"), dict) else {}
            mapped = _as_int(pack.get("tmdb_id")) or _as_int(ids.get("tmdb"))
            re0_tv_id = str(pack.get("tv_id")) if pack.get("tv_id") is not None else None
            if mapped is None and tmdb_id is not None and re0_tv_id is not None and re0_tv_id == str(tmdb_id):
                mapped = tmdb_id
            if mapped is not None:
                report["mapped"] += 1
            else:
                report["mapping_unknown"] += 1
            media_id = local_media_id_for(conn, "tv", mapped) if mapped else None
            preview = {"latest_label": pack.get("latest_label") if isinstance(pack.get("latest_label"), str) else None,
                       "item_count": _as_int(pack.get("item_count")), "items": _preview_items(pack.get("preview_items"))}
            if preview["latest_label"] is None and preview["items"]:
                preview["latest_label"] = preview["items"][0].get("episode_label")
            unlocked = 1 if (force_unlocked or pack.get("is_unlocked") is True) else 0
            conn.execute(
                "INSERT INTO re0_tv_follow_pack(slug_hash, slug_ciphertext, re0_tv_id, tmdb_id, media_id, title, is_unlocked, is_owner, is_completed, unlock_points, "
                "preview_json, last_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(slug_hash) DO UPDATE SET re0_tv_id=excluded.re0_tv_id, tmdb_id=COALESCE(excluded.tmdb_id, tmdb_id), media_id=COALESCE(excluded.media_id, media_id), "
                "title=excluded.title, is_unlocked=MAX(is_unlocked, excluded.is_unlocked), is_owner=excluded.is_owner, is_completed=excluded.is_completed, "
                "unlock_points=excluded.unlock_points, preview_json=excluded.preview_json, last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at",
                (slug_hash(slug, salt), store.fernet.encrypt(slug.encode("utf-8")), re0_tv_id, mapped, media_id,
                 str(pack.get("title") or "")[:200] or None, unlocked, 1 if pack.get("is_owner") is True else 0,
                 None if pack.get("is_completed") is None else int(pack.get("is_completed") is True), _as_int(pack.get("unlock_points")),
                 json.dumps(preview, ensure_ascii=False), now, now),
            )
            report["recorded"] += 1
        conn.commit()
    finally:
        conn.close()
    return report


def query_packs_for_tmdb(store, client, *, tmdb_id: int, salt: str, now: int, cooldown: int = TV_FOLLOW_COOLDOWN_SECONDS) -> dict:
    key = f"tv_follow_checked:{tmdb_id}"
    conn = store.connect()
    try:
        _ensure_follow_tables(conn)
        last = int(state_get(conn, key) or 0)
        if last and now - last < cooldown:
            return {"queried": False, "skipped_reason": "cooldown", "recorded": 0, "mapped": 0}
        state_set(conn, key, str(now), now)
        conn.commit()
    finally:
        conn.close()
    result = client.get("/api/open/tv-follow/packs", params={"tv_id": tmdb_id, "page": 1, "page_size": 50})
    if not result.ok:
        return {"queried": True, "error_class": result.error_class, "recorded": 0, "mapped": 0, "retry_after": result.retry_after}
    items = result.data.get("items") if isinstance(result.data, dict) else (result.data if isinstance(result.data, list) else [])
    report = record_packs(store, items or [], salt=salt, tmdb_id=tmdb_id, now=now)
    return {"queried": True, **report}


def sync_my_packs(store, client, *, salt: str, now: int) -> dict:
    result = client.get("/api/open/tv-follow/my", params={"page": 1, "page_size": 50})
    if not result.ok:
        return {"recorded": 0, "unread_count": None, "error_class": result.error_class}
    data = result.data if isinstance(result.data, dict) else {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    report = record_packs(store, items, salt=salt, tmdb_id=None, now=now, force_unlocked=True)
    return {"recorded": report["recorded"], "unread_count": _as_int(data.get("unread_count"))}


def _pack_row(conn: sqlite3.Connection, *, slug_hash_value: str | None = None, ref: str | None = None):
    if slug_hash_value:
        return conn.execute("SELECT * FROM re0_tv_follow_pack WHERE slug_hash=?", (slug_hash_value,)).fetchone()
    return conn.execute("SELECT * FROM re0_tv_follow_pack WHERE substr(slug_hash, 1, 16)=?", (ref,)).fetchone()


def fetch_pack_items(store, client, *, slug: str, salt: str, now: int) -> dict:
    """Items of an unlocked pack -> insert-only links (one group per season
    under the pack's local media). A 403 marks the pack ``locked`` and is
    never retried by itself."""
    key = slug_hash(slug, salt)
    conn = store.connect()
    try:
        _ensure_follow_tables(conn)
        pack = _pack_row(conn, slug_hash_value=key)
        if pack is None:
            return {"status": "unknown_pack", "items": 0, "materialized": 0, "seen": 0, "invalid": 0}
        if pack["items_status"] == "locked" and not pack["is_unlocked"]:
            return {"status": "locked", "items": 0, "materialized": 0, "seen": 0, "invalid": 0}
        media_id, title = pack["media_id"], pack["title"]
    finally:
        conn.close()
    result = client.get(f"/api/open/tv-follow/packs/{slug}/items", params={"page": 1, "page_size": 50})
    if not result.ok:
        status = "locked" if result.status == 403 or (result.code or "") == "not_unlocked" else (result.error_class or "error")
        conn = store.connect()
        try:
            conn.execute("UPDATE re0_tv_follow_pack SET items_status=?, updated_at=? WHERE slug_hash=?", (status, now, key))
            conn.commit()
        finally:
            conn.close()
        return {"status": status, "items": 0, "materialized": 0, "seen": 0, "invalid": 0, "retry_after": result.retry_after}
    items = result.data.get("items") if isinstance(result.data, dict) else (result.data if isinstance(result.data, list) else [])
    report = {"status": "ok" if media_id else "no_local_media", "items": len(items or []), "materialized": 0, "seen": 0, "invalid": 0}
    import library_store
    for item in items or []:
        if not isinstance(item, dict) or item.get("id") is None:
            report["invalid"] += 1
            continue
        url = item.get("url") if isinstance(item.get("url"), str) else None
        if not url or not url.strip().lower().startswith(("http://", "https://", "ed2k://", "magnet:")):
            report["invalid"] += 1
            continue
        item_id = str(item.get("id"))
        label = str(item.get("episode_label") or "")[:64] or None
        season, ep_start, ep_end = _as_int(item.get("season")), _as_int(item.get("episode_start")), _as_int(item.get("episode_end"))
        safe_json = json.dumps({k: item[k] for k in ("resolution", "source", "subtitle", "remark") if isinstance(item.get(k), (str, int))}, ensure_ascii=False)
        conn = store.connect()
        try:
            existing = conn.execute("SELECT resource_link_id FROM re0_tv_follow_item WHERE pack_slug_hash=? AND re0_item_id=?", (key, item_id)).fetchone()
        finally:
            conn.close()
        link_id = existing["resource_link_id"] if existing else None
        if link_id:
            report["seen"] += 1
        elif media_id:
            info = library_normalize.parse_link(url, item.get("access_code") if isinstance(item.get("access_code"), str) else None)
            canonical = library_normalize.canonical_hash(info.canonical)
            fingerprint = f"re0-follow:S{season if season is not None else 0:02d}"
            conn = store.connect()
            try:
                group = conn.execute("SELECT id FROM resource_group WHERE media_id=? AND edition_fingerprint=?", (media_id, fingerprint)).fetchone()
            finally:
                conn.close()
            group_id = int(group["id"]) if group else store.upsert_group(library_store.GroupRecord(
                media_id=media_id, edition_fingerprint=fingerprint, display_title=f"追更包 · S{season:02d}" if season is not None else "追更包",
                season_from=season, season_to=season, tags_json=json.dumps(["re0", "tv-follow"]), created_at=now, updated_at=now))
            rec = library_store.LinkRecord(
                public_id=f"re0f-{hashlib.sha256(f'{key}|{item_id}|{canonical}'.encode()).hexdigest()[:16]}", group_id=group_id, provider=info.provider,
                canonical_url_hash=canonical, url_label=(label + " · " if label else "") + info.label,
                url_ciphertext=store.fernet.encrypt(info.url.encode("utf-8")),
                access_code_ciphertext=store.fernet.encrypt(info.access_code.encode("utf-8")) if info.access_code else None,
                has_access_code=1 if info.access_code else 0, remark="RE0 追更包", imported_at=now,
            )
            link_id, created = store.insert_link_preserving_existing(rec)
            store.recount_groups([group_id])
            report["materialized" if created else "seen"] += 1
        conn = store.connect()
        try:
            conn.execute(
                "INSERT INTO re0_tv_follow_item(pack_slug_hash, re0_item_id, season, episode_start, episode_end, label, unlocked, resource_link_id, item_json, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pack_slug_hash, re0_item_id) DO UPDATE SET label=excluded.label, unlocked=excluded.unlocked, "
                "resource_link_id=COALESCE(excluded.resource_link_id, resource_link_id), item_json=excluded.item_json, updated_at=excluded.updated_at",
                (key, item_id, season, ep_start, ep_end, label, 1, link_id, safe_json, now),
            )
            conn.commit()
        finally:
            conn.close()
    conn = store.connect()
    try:
        conn.execute("UPDATE re0_tv_follow_pack SET items_status='ok', is_unlocked=1, updated_at=? WHERE slug_hash=?", (now, key))
        conn.commit()
    finally:
        conn.close()
    return report


def follow_rows(conn: sqlite3.Connection, *, tmdb_id: int) -> list[dict]:
    try:
        rows = conn.execute("SELECT * FROM re0_tv_follow_pack WHERE tmdb_id=? ORDER BY is_unlocked DESC, updated_at DESC", (tmdb_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        try:
            preview = json.loads(r["preview_json"] or "{}")
        except ValueError:
            preview = {}
        unlocked_items = conn.execute("SELECT COUNT(*) FROM re0_tv_follow_item WHERE pack_slug_hash=? AND resource_link_id IS NOT NULL", (r["slug_hash"],)).fetchone()[0]
        out.append({
            "ref": r["slug_hash"][:16], "title": r["title"], "latest_label": preview.get("latest_label"), "item_count": preview.get("item_count"),
            "preview_items": preview.get("items") or [], "is_completed": None if r["is_completed"] is None else bool(r["is_completed"]),
            "is_unlocked": bool(r["is_unlocked"]), "is_owner": bool(r["is_owner"]), "unlock_points": r["unlock_points"],
            "items_status": r["items_status"], "unlocked_items": int(unlocked_items), "last_seen_at": _iso(r["last_seen_at"]),
        })
    return out


def tv_follow_phase(store, client, *, limit: int, salt: str, now: int) -> dict:
    report = {"phase": "tv-follow", "my_packs": sync_my_packs(store, client, salt=salt, now=now), "queried": 0, "recorded": 0, "skipped": 0, "error_class": None}
    conn = store.connect(readonly=True)
    try:
        _ensure_calendar_table(conn)
        rows = conn.execute(
            "SELECT m.tmdb_id AS tmdb_id FROM media m WHERE m.media_type='tv' AND m.tmdb_id IS NOT NULL "
            "ORDER BY (SELECT MIN(first_aired_epoch) FROM re0_calendar_event c WHERE c.media_type='tv' AND c.tmdb_id=m.tmdb_id AND c.first_aired_epoch >= ?) IS NULL, m.id LIMIT ?",
            (now, limit),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        sub = query_packs_for_tmdb(store, client, tmdb_id=int(row["tmdb_id"]), salt=salt, now=now)
        if not sub["queried"]:
            report["skipped"] += 1
            continue
        report["queried"] += 1
        report["recorded"] += sub.get("recorded", 0)
        if sub.get("error_class") in _STOP_ON:
            report["error_class"] = sub["error_class"]
            break
    return report
