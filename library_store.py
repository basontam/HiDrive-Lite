"""SQLite index store for the personal media-resource library.

Owns the on-disk schema (see docs/architecture.md
§3), the ``schema_meta`` key/value table, and the write/upsert API shared by
the offline importer and the production install step.  Query, search,
install-time encryption and the TMDB cache/budget tables are out of scope
here: the two TMDB tables are created by ``library_tmdb.ensure_tables``,
which integrates through ``EXTRA_SCHEMA_HOOKS`` rather than being defined in
``SCHEMA_SQL``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

import library_normalize

if TYPE_CHECKING:
    import library_search

LOG = logging.getLogger("HiDrive-Lite.library_store")

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- key: schema_version, normalize_version, built_at, source_hashes(json), installed_at, encrypted(0|1)

CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY,
  media_identity TEXT NOT NULL UNIQUE,
  media_type TEXT NOT NULL CHECK (media_type IN ('movie','tv','unknown')),
  title_zh TEXT NOT NULL,
  title_original TEXT,
  title_alt_json TEXT NOT NULL DEFAULT '[]',
  year INTEGER,
  search_key TEXT NOT NULL,
  tmdb_id INTEGER,
  overview TEXT, poster_path TEXT, backdrop_path TEXT,
  genres_json TEXT NOT NULL DEFAULT '[]',
  match_status TEXT NOT NULL DEFAULT 'unmatched'
    CHECK (match_status IN ('exact','candidate','unmatched','needs_review')),
  match_score REAL, match_candidates_json TEXT,
  link_count INTEGER NOT NULL DEFAULT 0, has_115 INTEGER NOT NULL DEFAULT 0,
  -- T15 addendum (master work order v1.4 §4.1): metadata-completion state,
  -- independent of match_status above (metadata_status:
  -- pending|complete|partial|manual_review|no_source|error; metadata_source:
  -- tmdb|tvmaze|manual|none). Owned by scripts/media_metadata_backfill.py;
  -- enrich_batch/library_tmdb never advances metadata_status itself.
  metadata_status TEXT NOT NULL DEFAULT 'pending',
  metadata_source TEXT,
  tvmaze_id INTEGER,
  imdb_id TEXT,
  metadata_fetched_at TEXT,
  metadata_error TEXT,
  -- T15 addendum §14.2: ratings, separate from match_status/metadata_status
  -- (ratings_status: pending|complete|partial|none|error). ratings_json
  -- holds one key per source ({"tmdb": {...}, "imdb": {...}, "tvmaze": {...}}),
  -- never averaged/merged across sources.
  ratings_json TEXT NOT NULL DEFAULT '{}',
  ratings_status TEXT NOT NULL DEFAULT 'pending',
  ratings_fetched_at TEXT,
  ratings_error TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_search ON media(search_key);
CREATE INDEX IF NOT EXISTS idx_media_year_type ON media(year, media_type);
CREATE INDEX IF NOT EXISTS idx_media_rank ON media(has_115 DESC, link_count DESC);

-- T15 (z1-hints §design item 1): Codex's offline IMDb candidate hints,
-- shipped inside the plaintext bundle and keyed by media_identity (not
-- media_id -- identities are stable across a rebuild, ids are not).
-- candidates_json is a JSON array of at most 3 {imdb_id, imdb_type,
-- start_year, end_year, primary_title, original_title, score, reasons}
-- objects -- never an evidence blob or a URL. install_bundle's plain
-- file-copy naturally carries this table across; _inherit_from_existing
-- deliberately never touches it (a fresh --hints import always replaces it
-- wholesale, so there is nothing to "inherit" from the previous production
-- library).
CREATE TABLE IF NOT EXISTS tmdb_hints (
  media_identity TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  decision TEXT NOT NULL,
  candidate_count INTEGER NOT NULL,
  candidates_json TEXT NOT NULL,
  generated_at INTEGER NOT NULL
);

-- T15 addendum §14.3: IMDb's own official ratings dataset
-- (title.ratings.tsv.gz), imported offline by
-- scripts/import_media_library.py --imdb-ratings into only the ids
-- referenced by tmdb_hints (never the whole dataset) -- joined by imdb_id
-- once a media row's identity is confirmed. Never network-refreshed by the
-- enricher; imported alongside the bundle like tmdb_hints above.
CREATE TABLE IF NOT EXISTS imdb_ratings (
  imdb_id TEXT PRIMARY KEY,
  rating REAL,
  votes INTEGER,
  as_of TEXT
);

CREATE TABLE IF NOT EXISTS resource_group (
  id INTEGER PRIMARY KEY,
  media_id INTEGER NOT NULL REFERENCES media(id),
  edition_fingerprint TEXT NOT NULL,
  season_from INTEGER, season_to INTEGER, episode_from INTEGER, episode_to INTEGER,
  complete_season INTEGER NOT NULL DEFAULT 0,
  quality TEXT,
  source_type TEXT,
  hdr TEXT,
  video_codec TEXT,
  audio_summary TEXT, subtitle_summary TEXT, tags_json TEXT NOT NULL DEFAULT '[]',
  display_title TEXT NOT NULL,
  needs_review INTEGER NOT NULL DEFAULT 0, review_reason TEXT,
  link_count INTEGER NOT NULL DEFAULT 0, has_115 INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  UNIQUE (media_id, edition_fingerprint)
);

CREATE TABLE IF NOT EXISTS resource_link (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  group_id INTEGER NOT NULL REFERENCES resource_group(id),
  provider TEXT NOT NULL,
  canonical_url_hash TEXT NOT NULL,
  url_plain TEXT, access_code_plain TEXT,
  url_ciphertext BLOB, access_code_ciphertext BLOB,
  url_label TEXT NOT NULL,
  has_access_code INTEGER NOT NULL DEFAULT 0,
  title_raw TEXT, remark TEXT,
  created_at_source INTEGER, deleted_at_source INTEGER,
  imported_at INTEGER NOT NULL,
  UNIQUE (provider, canonical_url_hash)
);
CREATE INDEX IF NOT EXISTS idx_link_group ON resource_link(group_id);

CREATE TABLE IF NOT EXISTS link_provenance (
  link_id INTEGER NOT NULL REFERENCES resource_link(id),
  source_file TEXT NOT NULL, sheet TEXT NOT NULL, row_number INTEGER NOT NULL,
  record_id INTEGER, slug TEXT, owner_uid INTEGER, owner_tag TEXT,
  PRIMARY KEY (source_file, sheet, row_number)
);

-- 检索索引（导入时由 library_search.build_index() 生成；§7.0）
CREATE TABLE IF NOT EXISTS search_doc (
  media_id INTEGER PRIMARY KEY REFERENCES media(id),
  title_key TEXT NOT NULL,
  alias_keys_json TEXT NOT NULL DEFAULT '[]',
  pinyin_full TEXT, pinyin_initials TEXT,
  doc_len INTEGER NOT NULL, static_boost REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS search_term (
  term TEXT NOT NULL, media_id INTEGER NOT NULL, field TEXT NOT NULL,
  weight REAL NOT NULL,
  PRIMARY KEY (term, media_id, field)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_search_term_media ON search_term(media_id);
CREATE TABLE IF NOT EXISTS search_vocab (term TEXT PRIMARY KEY, kind TEXT NOT NULL, df INTEGER NOT NULL, idf REAL NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS search_charmap (src TEXT PRIMARY KEY, dst TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_search_pinyin_full ON search_doc(pinyin_full);
CREATE INDEX IF NOT EXISTS idx_search_pinyin_init ON search_doc(pinyin_initials);

CREATE TABLE IF NOT EXISTS import_run (
  id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL, finished_at INTEGER,
  normalize_version TEXT NOT NULL, source_hashes_json TEXT NOT NULL,
  row_counts_json TEXT, duplicate_counts_json TEXT, conflict_counts_json TEXT, status TEXT NOT NULL
);

-- w6: anonymous link-validity checker (docs/architecture.md).
-- Keyed by resource_link's own stable (provider, canonical_url_hash) pair
-- rather than link_id, so a re-import that reassigns a link's row id (or
-- moves it to a different group) never loses its check history. Deliberately
-- part of this module's own core SCHEMA_SQL (unlike tmdb_cache/tmdb_budget,
-- which are only added via EXTRA_SCHEMA_HOOKS at app.py's "integration
-- time") because library_store.py's OWN query methods (recount/filters/
-- _group_summary_payload, via live_link_sql()) read link_check directly --
-- scripts/import_media_library.py builds a bundle through this module alone
-- (never importing library_tmdb/app), so the table must exist the moment
-- create_schema() runs, not only once a checker/app process has touched the
-- database. Never stores a URL, access code or response body -- only
-- hashes, statuses, reason codes, http classes, timestamps and counts.
CREATE TABLE IF NOT EXISTS link_check (
  provider TEXT NOT NULL, canonical_url_hash TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT, http_class TEXT, checked_at INTEGER, next_check_at INTEGER,
  consecutive_unknown INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (provider, canonical_url_hash)
);
CREATE INDEX IF NOT EXISTS idx_link_check_due ON link_check(provider, next_check_at);

-- BackgroundLinkChecker's persisted heartbeat/round/budget/pause state
-- (mirrors tmdb_enricher_state's role for the TMDB enricher) -- plain
-- key/value pairs only, never a URL or response body.
CREATE TABLE IF NOT EXISTS link_check_state (
  key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER
);
"""

# Populated at integration time with e.g. ``library_tmdb.ensure_tables`` so the
# two TMDB tables (tmdb_cache, tmdb_budget) get created without this module
# depending on library_tmdb.  create_schema() calls each hook with the open
# writable connection.
EXTRA_SCHEMA_HOOKS: list[Callable[[sqlite3.Connection], None]] = []

# T15 addendum §4.1/§14.2: the media columns a pre-T15 already-installed
# library predates. Kept in sync with the CREATE TABLE media(...) block in
# SCHEMA_SQL above -- these ``ALTER TABLE ADD COLUMN`` statements are only
# ever the *migration* path for an existing installed DB; a freshly built
# bundle (``create_schema()``) already has every column via SCHEMA_SQL
# itself, so ``_ensure_media_metadata_columns`` is a same-columns no-op for
# it (kept schema_version at 1: these are additive-only columns with
# defaults, so an old *and* a new binary can both open either an old or a
# migrated database without any version-gated behaviour change).
_MEDIA_METADATA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("metadata_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("metadata_source", "TEXT"),
    ("tvmaze_id", "INTEGER"),
    ("imdb_id", "TEXT"),
    ("metadata_fetched_at", "TEXT"),
    ("metadata_error", "TEXT"),
    ("ratings_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("ratings_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("ratings_fetched_at", "TEXT"),
    ("ratings_error", "TEXT"),
)


def _ensure_media_metadata_columns(conn: sqlite3.Connection) -> None:
    """Lazily, idempotently ``ALTER TABLE media ADD COLUMN`` for every T15
    metadata/ratings column an older installed library predates. A no-op
    once every column already exists (freshly-built bundles: SCHEMA_SQL
    already declares them) and a no-op when ``media`` itself doesn't exist
    yet (``create_schema()``'s first call, before ``SCHEMA_SQL`` runs) --
    safe to call on every write connection. Called from both
    ``LibraryStore.connect()`` (every write connection self-heals) and
    ``open_installed()``/``_inherit_from_existing()`` (so a subsequent
    *readonly* connection, which can never ALTER TABLE itself, already sees
    the full column set)."""
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(media)").fetchall()}
    except sqlite3.OperationalError:
        return
    if not existing:
        return
    changed = False
    for name, ddl in _MEDIA_METADATA_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE media ADD COLUMN {name} {ddl}")
            changed = True
    if changed:
        conn.commit()


def _ensure_link_check_tables(conn: sqlite3.Connection) -> None:
    """w6-checker-fix C1: self-heal ``link_check``/``link_check_state`` (and
    their index) on every write connection, the same way
    ``_ensure_media_metadata_columns`` self-heals a pre-T15 library's
    missing ``media`` columns above. These two tables ARE part of this
    module's own core ``SCHEMA_SQL`` (see its comment), so a bundle built
    by the current binary already has them and this is a same-schema
    no-op -- it only matters for a production library that was installed
    by a pre-w6 binary and has never been re-installed since. Every read
    path in this module and in ``library_search`` queries ``link_check``
    unconditionally via ``live_link_sql()``, and every one of those reads
    uses a *readonly* connection that can never ``CREATE TABLE`` itself --
    so the table must exist before the first read, not just once a checker
    thread or CLI invocation happens to open a write connection. Called
    from both ``LibraryStore.connect(readonly=False)`` (every write
    connection self-heals) and ``open_installed()`` (a short write
    connection at open, so a pre-w6 library works immediately without any
    thread running)."""
    conn.executescript(_LINK_CHECK_TABLES_DDL)
    conn.commit()


# w6-checker-fix C2: recount_groups()'s bulk-recompute cutoff -- above this
# many distinct groups, a full recount() is cheaper than that many
# per-group UPDATEs.
RECOUNT_GROUPS_FALLBACK_THRESHOLD = 500


def live_link_sql(alias: str = "rl") -> str:
    """SQL boolean expression (references ``resource_link`` under ``alias``,
    which may be the table's own bare name when it isn't aliased) for "this
    link is live": not deleted at the source AND not flagged ``invalid`` by
    the w6 link-validity checker (a `link_check` row for this
    provider/canonical_url_hash whose ``status='invalid'``).

    The single source of truth for the live-link rule (w6-contract): every
    ``deleted_at_source IS NULL`` site in this module and in
    ``library_search`` uses this instead, so facets, provider counts,
    group/media link_count, search filters and recommendations all agree
    with the detail page's `invalid` flag -- a checker-confirmed-dead link
    counts exactly like a source-deleted one, everywhere.
    """
    return (
        f"{alias}.deleted_at_source IS NULL AND NOT EXISTS ("
        f"SELECT 1 FROM link_check lc WHERE lc.provider = {alias}.provider "
        f"AND lc.canonical_url_hash = {alias}.canonical_url_hash AND lc.status = 'invalid')"
    )


class LibraryNotInstalled(RuntimeError):
    """Raised when the library index database file does not exist yet."""


class LibraryIndexUnreadable(RuntimeError):
    """Raised when the library index database file exists but is not a
    readable SQLite database (truncated, zero-byte, corrupt) -- distinct
    from LibraryNotInstalled (the file simply isn't there)."""


class LibraryNotEncrypted(RuntimeError):
    """Raised when an operation requires an encrypted (production) library."""


class LibrarySchemaMismatch(RuntimeError):
    """Raised when the on-disk schema version is newer than this code supports."""


class LibraryKeyUnavailable(RuntimeError):
    """Raised by :meth:`LibraryStore.reveal` when the master key is missing or
    cannot decrypt the stored ciphertext (wrong/rotated key)."""


class LibraryBundleInvalid(RuntimeError):
    """Raised by :func:`install_bundle` when the bundle file fails schema_meta
    validation (wrong schema_version, already encrypted, or missing
    normalize_version) -- distinct from :class:`LibrarySchemaMismatch`, which
    is about an already-installed *production* library, not a bundle file
    about to be installed."""


@dataclass
class MediaRecord:
    media_identity: str
    media_type: str
    title_zh: str
    search_key: str
    title_original: str | None = None
    title_alt_json: str = "[]"
    year: int | None = None
    tmdb_id: int | None = None
    overview: str | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    genres_json: str = "[]"
    match_status: str = "unmatched"
    match_score: float | None = None
    match_candidates_json: str | None = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class GroupRecord:
    media_id: int
    edition_fingerprint: str
    display_title: str
    season_from: int | None = None
    season_to: int | None = None
    episode_from: int | None = None
    episode_to: int | None = None
    complete_season: int = 0
    quality: str | None = None
    source_type: str | None = None
    hdr: str | None = None
    video_codec: str | None = None
    audio_summary: str | None = None
    subtitle_summary: str | None = None
    tags_json: str = "[]"
    needs_review: int = 0
    review_reason: str | None = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class LinkRecord:
    public_id: str
    group_id: int
    provider: str
    canonical_url_hash: str
    url_label: str
    url_plain: str | None = None
    access_code_plain: str | None = None
    url_ciphertext: bytes | None = None
    access_code_ciphertext: bytes | None = None
    has_access_code: int = 0
    title_raw: str | None = None
    remark: str | None = None
    created_at_source: int | None = None
    deleted_at_source: int | None = None
    imported_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class ProvenanceRecord:
    link_id: int
    source_file: str
    sheet: str
    row_number: int
    record_id: int | None = None
    slug: str | None = None
    owner_uid: int | None = None
    owner_tag: str | None = None


class LibraryStore:
    def __init__(self, db_path: Path, fernet: Fernet | None = None) -> None:
        self.db_path = Path(db_path)
        self.fernet = fernet

    def connect(self, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            if not self.db_path.exists():
                raise LibraryNotInstalled(f"library index not installed: {self.db_path}")
            uri_path = quote(str(self.db_path), safe="/")
            conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            return conn

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        _ensure_media_metadata_columns(conn)
        _ensure_link_check_tables(conn)
        return conn

    def create_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA_SQL)
            for hook in EXTRA_SCHEMA_HOOKS:
                hook(conn)
            conn.commit()

            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                    ("encrypted", "0"),
                )
                conn.commit()
        finally:
            conn.close()

    def check_schema(self) -> None:
        version_str = self.meta_get("schema_version", "0")
        if int(version_str) > SCHEMA_VERSION:
            raise LibrarySchemaMismatch(
                f"library schema version {version_str} is newer than supported {SCHEMA_VERSION}"
            )

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        conn = self.connect(readonly=True)
        try:
            row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
        return row["value"] if row is not None else default

    def meta_set(self, key: str, value: str) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """
                INSERT INTO schema_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def is_encrypted(self) -> bool:
        return self.meta_get("encrypted", "0") == "1"

    def require_encrypted(self) -> None:
        if not self.is_encrypted():
            raise LibraryNotEncrypted("library index is not encrypted")

    def upsert_media(self, rec: MediaRecord) -> int:
        conn = self.connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO media (
                    media_identity, media_type, title_zh, title_original, title_alt_json,
                    year, search_key, tmdb_id, overview, poster_path, backdrop_path,
                    genres_json, match_status, match_score, match_candidates_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_identity) DO UPDATE SET
                    media_type = excluded.media_type,
                    title_zh = excluded.title_zh,
                    title_original = excluded.title_original,
                    title_alt_json = excluded.title_alt_json,
                    year = excluded.year,
                    search_key = excluded.search_key,
                    tmdb_id = excluded.tmdb_id,
                    overview = excluded.overview,
                    poster_path = excluded.poster_path,
                    backdrop_path = excluded.backdrop_path,
                    genres_json = excluded.genres_json,
                    match_status = excluded.match_status,
                    match_score = excluded.match_score,
                    match_candidates_json = excluded.match_candidates_json,
                    updated_at = excluded.updated_at
                RETURNING id
                """,
                (
                    rec.media_identity, rec.media_type, rec.title_zh, rec.title_original,
                    rec.title_alt_json, rec.year, rec.search_key, rec.tmdb_id, rec.overview,
                    rec.poster_path, rec.backdrop_path, rec.genres_json, rec.match_status,
                    rec.match_score, rec.match_candidates_json, rec.created_at, rec.updated_at,
                ),
            )
            media_id = cur.fetchone()[0]
            conn.commit()
            return media_id
        finally:
            conn.close()

    def upsert_group(self, rec: GroupRecord) -> int:
        conn = self.connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO resource_group (
                    media_id, edition_fingerprint, season_from, season_to, episode_from, episode_to,
                    complete_season, quality, source_type, hdr, video_codec, audio_summary,
                    subtitle_summary, tags_json, display_title, needs_review, review_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_id, edition_fingerprint) DO UPDATE SET
                    season_from = excluded.season_from,
                    season_to = excluded.season_to,
                    episode_from = excluded.episode_from,
                    episode_to = excluded.episode_to,
                    complete_season = excluded.complete_season,
                    quality = excluded.quality,
                    source_type = excluded.source_type,
                    hdr = excluded.hdr,
                    video_codec = excluded.video_codec,
                    audio_summary = excluded.audio_summary,
                    subtitle_summary = excluded.subtitle_summary,
                    tags_json = excluded.tags_json,
                    display_title = excluded.display_title,
                    needs_review = excluded.needs_review,
                    review_reason = excluded.review_reason,
                    updated_at = excluded.updated_at
                RETURNING id
                """,
                (
                    rec.media_id, rec.edition_fingerprint, rec.season_from, rec.season_to,
                    rec.episode_from, rec.episode_to, rec.complete_season, rec.quality,
                    rec.source_type, rec.hdr, rec.video_codec, rec.audio_summary,
                    rec.subtitle_summary, rec.tags_json, rec.display_title, rec.needs_review,
                    rec.review_reason, rec.created_at, rec.updated_at,
                ),
            )
            group_id = cur.fetchone()[0]
            conn.commit()
            return group_id
        finally:
            conn.close()

    def upsert_link(self, rec: LinkRecord) -> tuple[int, bool]:
        """Insert or update a resource_link row keyed on (provider, canonical_url_hash).

        The importer aggregates everything in memory and decides the final
        values before writing, so on conflict this refreshes EVERY column
        except the conflict keys themselves (``provider``,
        ``canonical_url_hash``) and ``id``/``public_id`` -- including
        ``group_id``, so a link that has moved to a different resource group
        is repointed rather than left under its old group.  Returns
        ``(id, created)`` with ``created=False`` on conflict.
        """
        conn = self.connect()
        try:
            existing = conn.execute(
                "SELECT id FROM resource_link WHERE provider=? AND canonical_url_hash=?",
                (rec.provider, rec.canonical_url_hash),
            ).fetchone()
            created = existing is None

            cur = conn.execute(
                """
                INSERT INTO resource_link (
                    public_id, group_id, provider, canonical_url_hash, url_plain, access_code_plain,
                    url_ciphertext, access_code_ciphertext, url_label, has_access_code, title_raw,
                    remark, created_at_source, deleted_at_source, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, canonical_url_hash) DO UPDATE SET
                    group_id = excluded.group_id,
                    url_plain = excluded.url_plain,
                    access_code_plain = excluded.access_code_plain,
                    url_ciphertext = excluded.url_ciphertext,
                    access_code_ciphertext = excluded.access_code_ciphertext,
                    url_label = excluded.url_label,
                    has_access_code = excluded.has_access_code,
                    title_raw = excluded.title_raw,
                    remark = excluded.remark,
                    created_at_source = excluded.created_at_source,
                    deleted_at_source = excluded.deleted_at_source,
                    imported_at = excluded.imported_at
                RETURNING id
                """,
                (
                    rec.public_id, rec.group_id, rec.provider, rec.canonical_url_hash,
                    rec.url_plain, rec.access_code_plain, rec.url_ciphertext,
                    rec.access_code_ciphertext, rec.url_label, rec.has_access_code,
                    rec.title_raw, rec.remark, rec.created_at_source, rec.deleted_at_source,
                    rec.imported_at,
                ),
            )
            link_id = cur.fetchone()[0]
            conn.commit()
            return link_id, created
        finally:
            conn.close()

    def add_provenance(self, rec: ProvenanceRecord) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO link_provenance (
                    link_id, source_file, sheet, row_number, record_id, slug, owner_uid, owner_tag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.link_id, rec.source_file, rec.sheet, rec.row_number,
                    rec.record_id, rec.slug, rec.owner_uid, rec.owner_tag,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def recount(self) -> None:
        """Refresh ``resource_group``/``media``'s precomputed ``link_count``/
        ``has_115`` columns from LIVE (non-deleted-at-source) resource_link
        rows only -- a deleted link is still a row in ``resource_link`` but
        never counted here, the same live-link rule ``library_search``'s own
        per-item/provider-scoped counts and ``_group_summary_payload``'s
        provider-filtered branch already use. Keeping this one rule
        everywhere means an unfiltered media-detail badge, a search card and
        a provider facet for the same media never disagree over whether a
        deleted link still counts."""
        conn = self.connect()
        try:
            conn.execute(
                f"""
                UPDATE resource_group SET
                    link_count = (
                        SELECT COUNT(*) FROM resource_link
                        WHERE resource_link.group_id = resource_group.id
                          AND {live_link_sql('resource_link')}
                    ),
                    has_115 = (
                        SELECT CASE WHEN EXISTS (
                            SELECT 1 FROM resource_link
                            WHERE resource_link.group_id = resource_group.id
                              AND resource_link.provider = '115'
                              AND {live_link_sql('resource_link')}
                        ) THEN 1 ELSE 0 END
                    )
                """
            )
            conn.execute(
                """
                UPDATE media SET
                    link_count = (
                        SELECT COALESCE(SUM(resource_group.link_count), 0)
                        FROM resource_group
                        WHERE resource_group.media_id = media.id
                    ),
                    has_115 = (
                        SELECT CASE WHEN EXISTS (
                            SELECT 1 FROM resource_group
                            WHERE resource_group.media_id = media.id
                              AND resource_group.has_115 = 1
                        ) THEN 1 ELSE 0 END
                    )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def recount_groups(self, group_ids: list[int]) -> None:
        """w6-checker-fix C2: incremental counterpart to ``recount()`` --
        recomputes ``link_count``/``has_115`` for exactly the given
        ``resource_group`` rows (and the ``media`` rows they belong to)
        using the same live-link rule, instead of a full-table sweep.

        Nothing currently recounts after ``run_link_check_round`` writes a
        verdict, so a checker-confirmed-invalid (or restored-valid) link
        never moved ``media.link_count``/``resource_group.link_count``/
        ``has_115`` -- and therefore ``all_links_invalid``, search cards
        and group summaries -- until some unrelated write happened to
        trigger a full ``recount()``. The caller (``run_link_check_round``)
        calls this only for the groups whose links actually changed live
        status this round (a verdict that doesn't flip ``invalid`` either
        way needs no recount at all).

        Falls back to a full ``recount()`` when more than
        ``RECOUNT_GROUPS_FALLBACK_THRESHOLD`` distinct groups are given --
        a bulk pass (e.g. draining a large recheck queue at once) is
        cheaper as one full sweep than as that many tiny per-row UPDATEs.
        A no-op for an empty ``group_ids``."""
        ids = sorted({int(g) for g in group_ids})
        if not ids:
            return
        if len(ids) > RECOUNT_GROUPS_FALLBACK_THRESHOLD:
            self.recount()
            return
        conn = self.connect()
        try:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE resource_group SET
                    link_count = (
                        SELECT COUNT(*) FROM resource_link
                        WHERE resource_link.group_id = resource_group.id
                          AND {live_link_sql('resource_link')}
                    ),
                    has_115 = (
                        SELECT CASE WHEN EXISTS (
                            SELECT 1 FROM resource_link
                            WHERE resource_link.group_id = resource_group.id
                              AND resource_link.provider = '115'
                              AND {live_link_sql('resource_link')}
                        ) THEN 1 ELSE 0 END
                    )
                WHERE resource_group.id IN ({placeholders})
                """,
                ids,
            )
            media_rows = conn.execute(
                f"SELECT DISTINCT media_id FROM resource_group WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            media_ids = [row[0] for row in media_rows]
            if media_ids:
                media_placeholders = ",".join("?" for _ in media_ids)
                conn.execute(
                    f"""
                    UPDATE media SET
                        link_count = (
                            SELECT COALESCE(SUM(resource_group.link_count), 0)
                            FROM resource_group
                            WHERE resource_group.media_id = media.id
                        ),
                        has_115 = (
                            SELECT CASE WHEN EXISTS (
                                SELECT 1 FROM resource_group
                                WHERE resource_group.media_id = media.id
                                  AND resource_group.has_115 = 1
                            ) THEN 1 ELSE 0 END
                        )
                    WHERE media.id IN ({media_placeholders})
                    """,
                    media_ids,
                )
            conn.commit()
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self.connect(readonly=True)
        try:
            media_total = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
            groups_total = conn.execute("SELECT COUNT(*) FROM resource_group").fetchone()[0]
            links_total = conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0]
            match_counts = {"exact": 0, "candidate": 0, "unmatched": 0, "needs_review": 0}
            for status, count in conn.execute("SELECT match_status, COUNT(*) FROM media GROUP BY match_status"):
                match_counts[status] = count
            # T14/§3.1, fix wave 1 (finding #4): split needs_review into
            # three buckets rather than two -- a requeued row that comes
            # back with zero candidates gets match_score=0.0 and
            # match_candidates_json='[]' (see library_tmdb._write_needs_review/
            # judge()'s "unmatched" verdict), which is neither "never
            # queried" nor "has candidates to confirm"; counting it as
            # review_scored (the old review_scored = needs_review -
            # review_pending_unqueried subtraction) mislabeled it as having
            # a candidate to confirm when there is none.
            #   review_pending_unqueried: score NULL and no candidates yet.
            #   review_scored: has an actual (non-empty) candidate list.
            #   review_no_candidate: queried, but came back with none.
            review_pending_unqueried = conn.execute(
                "SELECT COUNT(*) FROM media WHERE match_status = 'needs_review' AND match_score IS NULL "
                "AND (match_candidates_json IS NULL OR match_candidates_json = '')"
            ).fetchone()[0]
            review_scored = conn.execute(
                "SELECT COUNT(*) FROM media WHERE match_status = 'needs_review' "
                "AND match_candidates_json IS NOT NULL AND match_candidates_json != '' "
                "AND match_candidates_json != '[]'"
            ).fetchone()[0]
            review_no_candidate = match_counts["needs_review"] - review_pending_unqueried - review_scored
            cache_counts = {"ok": 0, "empty": 0, "failed_retryable": 0, "failed_permanent": 0}
            try:
                rows = conn.execute("SELECT status, COUNT(*) FROM tmdb_cache GROUP BY status").fetchall()
            except sqlite3.OperationalError:
                # tmdb_cache is created by library_tmdb.ensure_tables via
                # EXTRA_SCHEMA_HOOKS (see module docstring) -- a store built
                # without that hook registered (e.g. library_store.py's own
                # unit tests, which stay decoupled from library_tmdb on
                # purpose) simply reports all-zero cache counts.
                rows = []
            for status, count in rows:
                cache_counts[status] = count
        finally:
            conn.close()
        return {
            "media_total": media_total,
            "groups_total": groups_total,
            "links_total": links_total,
            "schema_version": self.meta_get("schema_version"),
            "built_at": self.meta_get("built_at"),
            "installed_at": self.meta_get("installed_at"),
            "encrypted": self.is_encrypted(),
            "match": match_counts,
            "review_pending_unqueried": review_pending_unqueried,
            "review_scored": review_scored,
            "review_no_candidate": review_no_candidate,
            "needs_review_total": match_counts["needs_review"],
            "cache": cache_counts,
        }

    def hint_stats(self) -> dict:
        """T15 design item 4: counts backing ``/api/library/tmdb-status``'s
        and ``--library-status``'s ``hints_total``/``hints_by_decision``/
        ``hints_pending``, and the read-only ``--library-hints-report`` CLI.
        ``hints_pending`` counts hint rows whose media is still unmatched or
        an unqueried needs_review row (§stats()'s ``review_pending_unqueried``
        predicate) -- i.e. a hint the enricher hasn't had a chance to
        consume yet. Read-only; tolerates a library installed before T15
        (no ``tmdb_hints`` table yet) the same way ``stats()``'s
        ``tmdb_cache`` read tolerates a missing table -- reports all-zero
        rather than raising.

        w5-confirm-import addendum: also reports counts for Codex's manual
        ``confirmed`` hints (``source='codex_manual_confirmation'``,
        ``decision='confirmed'``, see scripts/import_media_library.py's
        ``write_confirmations``) plus the §5.6 five-way metadata-completion
        breakdown (``identity_confirmed``/``metadata_complete``/``partial``/
        ``manual_review``/``no_source``). All of these fold into the same
        pre-T15 "missing tmdb_hints table" all-zero fallback above.
        """
        conn = self.connect(readonly=True)
        try:
            try:
                total = conn.execute("SELECT COUNT(*) FROM tmdb_hints").fetchone()[0]
                by_decision_rows = conn.execute("SELECT decision, COUNT(*) FROM tmdb_hints GROUP BY decision").fetchall()
                pending = conn.execute(
                    """
                    SELECT COUNT(*) FROM tmdb_hints h
                    JOIN media m ON m.media_identity = h.media_identity
                    WHERE m.match_status = 'unmatched'
                       OR (m.match_status = 'needs_review' AND m.match_score IS NULL
                           AND (m.match_candidates_json IS NULL OR m.match_candidates_json = ''))
                    """
                ).fetchone()[0]

                confirmed_total = conn.execute(
                    "SELECT COUNT(*) FROM tmdb_hints WHERE decision = 'confirmed'"
                ).fetchone()[0]
                confirmed_pending = conn.execute(
                    """
                    SELECT COUNT(*) FROM tmdb_hints h
                    JOIN media m ON m.media_identity = h.media_identity
                    WHERE h.decision = 'confirmed' AND m.match_status != 'exact'
                    """
                ).fetchone()[0]
                confirmed_exact_rows = conn.execute(
                    """
                    SELECT h.candidates_json, m.tmdb_id, m.media_type
                    FROM tmdb_hints h
                    JOIN media m ON m.media_identity = h.media_identity
                    WHERE h.decision = 'confirmed' AND m.match_status = 'exact'
                    """
                ).fetchall()
                confirmed_conflicts = 0
                for candidates_json, media_tmdb_id, media_type in confirmed_exact_rows:
                    try:
                        candidates = json.loads(candidates_json or "[]")
                        candidate = candidates[0] if candidates else {}
                    except (TypeError, ValueError):
                        # A malformed candidates_json can't be verified against
                        # the media's own tmdb identity -- count it as a
                        # conflict (visible in the stat) rather than raising
                        # and turning the status endpoint into a 500.
                        confirmed_conflicts += 1
                        continue
                    if candidate.get("tmdb_id") != media_tmdb_id or candidate.get("tmdb_type") != media_type:
                        confirmed_conflicts += 1

                metadata_complete = conn.execute(
                    """
                    SELECT COUNT(*) FROM media
                    WHERE match_status = 'exact'
                      AND poster_path IS NOT NULL AND poster_path != ''
                      AND overview IS NOT NULL AND overview != ''
                    """
                ).fetchone()[0]
                exact_total = conn.execute(
                    "SELECT COUNT(*) FROM media WHERE match_status = 'exact'"
                ).fetchone()[0]
                partial = exact_total - metadata_complete
                manual_review = conn.execute(
                    "SELECT COUNT(*) FROM media WHERE match_status IN ('needs_review', 'candidate')"
                ).fetchone()[0]
                no_source = conn.execute(
                    """
                    SELECT COUNT(*) FROM media m
                    WHERE m.match_status = 'unmatched'
                      AND (
                        NOT EXISTS (SELECT 1 FROM tmdb_hints h WHERE h.media_identity = m.media_identity)
                        OR EXISTS (
                            SELECT 1 FROM tmdb_hints h
                            WHERE h.media_identity = m.media_identity AND h.decision = 'no_imdb_candidate'
                        )
                      )
                    """
                ).fetchone()[0]
            except sqlite3.OperationalError:
                total, by_decision_rows, pending = 0, [], 0
                confirmed_total = confirmed_pending = confirmed_conflicts = 0
                metadata_complete = partial = manual_review = no_source = 0
        finally:
            conn.close()
        return {
            "hints_total": total,
            "hints_by_decision": {decision: count for decision, count in by_decision_rows},
            "hints_pending": pending,
            "confirmed_total": confirmed_total,
            "confirmed_pending": confirmed_pending,
            "confirmed_conflicts": confirmed_conflicts,
            "identity_confirmed": confirmed_total,
            "metadata_complete": metadata_complete,
            "partial": partial,
            "manual_review": manual_review,
            "no_source": no_source,
        }

    # ------------------------------------------------------------------
    # T4.1 query service (read-only, all SQL parameterised)
    # ------------------------------------------------------------------

    def browse(self, filters: "library_search.Filters", *, sort: str, page: int, page_size: int) -> "library_search.SearchPage":
        """Pure-filter browsing (``q`` empty) -- a thin, documented alias for
        ``library_search.search(self, "", filters, ...)``; see that
        function's own "q 为空" branch for the actual SQL."""
        import library_search

        return library_search.search(self, "", filters, sort=sort, page=page, page_size=page_size)

    def eligible_recommendation_media_ids(self) -> list[int]:
        """T16 §2.2: the candidate pool for daily recommendations -- media
        with a trusted (``exact``) match, a displayable poster, and at
        least one live (non-deleted) resource link. Ordered by ``id``
        (stable) so a caller's own deterministic re-sort (e.g. a
        day-keyed tie-break) is reproducible run to run. Read-only; never
        touches TMDB/HDHive."""
        conn = self.connect(readonly=True)
        try:
            rows = conn.execute(
                f"""
                SELECT m.id AS id FROM media m
                WHERE m.match_status = 'exact'
                  AND m.poster_path IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM resource_group rg
                    JOIN resource_link rl ON rl.group_id = rg.id
                    WHERE rg.media_id = m.id AND {live_link_sql('rl')}
                  )
                ORDER BY m.id
                """
            ).fetchall()
        finally:
            conn.close()
        return [row["id"] for row in rows]

    def fallback_recommendation_media_ids(self, limit: int) -> list[int]:
        """T16 §2.1 fallback -- used only when
        ``eligible_recommendation_media_ids()`` is empty: the most
        recently-dated media (any match status) with a displayable poster
        and at least one live link, deterministically ordered (year desc,
        id asc, mirroring ``library_search._SORT_SQL["year_desc"]``)."""
        conn = self.connect(readonly=True)
        try:
            rows = conn.execute(
                f"""
                SELECT m.id AS id FROM media m
                WHERE m.poster_path IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM resource_group rg
                    JOIN resource_link rl ON rl.group_id = rg.id
                    WHERE rg.media_id = m.id AND {live_link_sql('rl')}
                  )
                ORDER BY (m.year IS NULL) ASC, m.year DESC, m.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [row["id"] for row in rows]

    def filters(self, *, clock: Callable[[], float] = time.time) -> dict:
        """§7.2 filter facets, process-wide cached for 300s (keyed by
        db_path so distinct stores/tests never share a cache entry); pass
        ``clock`` to control/advance time in tests."""
        cache_key = str(self.db_path)
        now = clock()
        cached = _FILTERS_CACHE.get(cache_key)
        if cached is not None and now - cached[0] < FILTERS_CACHE_TTL_SECONDS:
            return cached[1]
        result = self._compute_filters()
        _FILTERS_CACHE[cache_key] = (now, result)
        return result

    def _compute_filters(self) -> dict:
        conn = self.connect(readonly=True)
        try:
            types = [
                {"value": row[0], "label": _MEDIA_TYPE_LABELS.get(row[0], row[0]), "count": row[1]}
                for row in conn.execute("SELECT media_type, COUNT(*) FROM media GROUP BY media_type ORDER BY media_type")
            ]
            years = [
                {"value": row[0], "label": str(row[0]), "count": row[1]}
                for row in conn.execute(
                    "SELECT year, COUNT(*) FROM media WHERE year IS NOT NULL GROUP BY year ORDER BY year DESC"
                )
            ]
            providers = [
                {"value": row[0], "label": library_normalize.PROVIDERS.get(row[0], row[0]), "count": row[1]}
                for row in conn.execute(
                    f"SELECT provider, COUNT(*) FROM resource_link WHERE {live_link_sql('resource_link')} "
                    "GROUP BY provider ORDER BY COUNT(*) DESC, provider"
                )
            ]
            qualities = [
                {"value": row[0], "label": _QUALITY_LABELS.get(row[0], row[0]), "count": row[1]}
                for row in conn.execute(
                    "SELECT quality, COUNT(*) FROM resource_group WHERE quality IS NOT NULL "
                    "GROUP BY quality ORDER BY COUNT(*) DESC, quality"
                )
            ]
            hdr = [
                {"value": row[0], "label": _HDR_LABELS.get(row[0], row[0]), "count": row[1]}
                for row in conn.execute(
                    "SELECT hdr, COUNT(*) FROM resource_group WHERE hdr IS NOT NULL "
                    "GROUP BY hdr ORDER BY COUNT(*) DESC, hdr"
                )
            ]
            genre_counts: dict[str, int] = {}
            for (genres_json,) in conn.execute("SELECT genres_json FROM media WHERE genres_json IS NOT NULL"):
                for genre in json.loads(genres_json or "[]"):
                    genre_counts[genre] = genre_counts.get(genre, 0) + 1
            sources = [
                {"value": row[0], "count": row[1]}
                for row in conn.execute(
                    "SELECT source_type, COUNT(*) FROM resource_group WHERE source_type IS NOT NULL "
                    "GROUP BY source_type ORDER BY COUNT(*) DESC, source_type"
                )
            ]
        finally:
            conn.close()
        genres = [
            {"value": name, "label": name, "count": count}
            for name, count in sorted(genre_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return {
            "types": types, "years": years, "providers": providers, "qualities": qualities,
            "hdr": hdr, "genres": genres, "sources": sources,
        }

    def media_detail(
        self, media_id: int, provider: str | None = None, *, include_deleted: bool = False,
    ) -> dict | None:
        """The full media-detail payload (§13.2/§14.1).

        Round 16: ``include_deleted=True`` (the detail page reached from a
        "包含已失效" search result, or a ``deleted=1`` deep link) keeps a
        group whose links are ALL non-live -- checker-invalid and/or
        deleted at source -- instead of dropping it, so the page can show
        those rows marked 已失效. Every count (``link_count``,
        ``providers``, ``provider_facets``) stays live-only regardless;
        only the drop below is skipped.

        Every group carries its inline safe link summaries (§13.2 -- no
        separate ``/api/library/resource/<id>`` round trip needed) and
        normalised spec chips (§12.3). When ``provider`` is given, it is
        applied as a SQL-level filter on ``resource_link.provider`` inside
        each group (not a post-hoc hide): a group left with zero matching
        link ROWS at all (live or deleted) is dropped entirely rather than
        returned empty, and ``group_count``/``provider_facets``/
        ``provider_count`` are all recomputed from the filtered groups --
        no other provider's link, count or label survives anywhere in the
        payload. ``provider=None`` (the default) is the full, unfiltered
        result exactly as before.

        T17 fix wave 1 item 4: a per-group ``providers``/``link_count``/
        ``has_115`` (and the top-level ``provider_facets`` summed from
        them) count only LIVE links, the same way ``library_search``'s own
        card counts already do -- a deleted link still appears as a row in
        ``links`` (flagged ``deleted: True``) but is never counted, so a
        detail-page provider badge and a search-card's link/group count for
        the same media agree.

        Follow-up (post-T17): a group whose only matching link(s) are ALL
        deleted -- so its LIVE ``link_count`` is 0, whether or not
        ``provider`` is given -- is dropped from ``groups`` entirely and
        excluded from ``group_count``, matching the "excluded" rule
        ``library_search``'s own ``group_count_sql`` already applies
        (see library_search.py's ``_fetch_items``). This is a stricter cut
        than the "any matching row at all" rule above: a group can still
        have a matching ROW (kept a moment, above, by the provider filter)
        yet be dropped here because that row happens to be deleted.
        """
        conn = self.connect(readonly=True)
        try:
            media_row = conn.execute(
                "SELECT id, media_type, title_zh, title_original, year, overview, poster_path, "
                "backdrop_path, genres_json, match_status, tmdb_id, imdb_id, tvmaze_id, "
                "ratings_json, ratings_status FROM media WHERE id=?",
                (media_id,),
            ).fetchone()
            if media_row is None:
                return None
            group_rows = conn.execute(
                "SELECT id, media_id, edition_fingerprint, season_from, season_to, episode_from, episode_to, "
                "complete_season, quality, source_type, hdr, video_codec, audio_summary, subtitle_summary, "
                "tags_json, display_title, needs_review, review_reason, link_count, has_115 "
                "FROM resource_group WHERE media_id=?",
                (media_id,),
            ).fetchall()
            groups = [self._group_summary_payload(conn, row, provider=provider) for row in group_rows]
        finally:
            conn.close()
        if provider is not None:
            groups = [g for g in groups if g["links"]]
        # Follow-up (post-T17): a group with zero LIVE links (all deleted,
        # whether or not `provider` filtered it down to one link) is
        # dropped too -- matches search's group_count_sql "excluded" rule.
        # Round 16: unless the caller asked to include them (see docstring).
        if not include_deleted:
            groups = [g for g in groups if g["link_count"] > 0]
        groups.sort(key=lambda g: (0 if g["has_115"] else 1, -_QUALITY_RANK.get(g["quality"], 0)))
        provider_facets = self._provider_facets(groups)
        ratings = library_normalize.format_ratings(
            media_row["ratings_json"],
            media_type=media_row["media_type"],
            tmdb_id=media_row["tmdb_id"],
            imdb_id=media_row["imdb_id"],
            tvmaze_id=media_row["tvmaze_id"],
        )
        return {
            "media_id": media_row["id"],
            "media_type": media_row["media_type"],
            "title": media_row["title_zh"],
            "original_title": media_row["title_original"],
            "year": media_row["year"],
            "overview": media_row["overview"],
            "poster_path": media_row["poster_path"],
            "backdrop_path": media_row["backdrop_path"],
            "genres": json.loads(media_row["genres_json"] or "[]"),
            "match_status": media_row["match_status"],
            "ratings": ratings,
            "ratings_status": media_row["ratings_status"],
            "groups": groups,
            "group_count": len(groups),
            "provider_facets": provider_facets,
            "provider_count": len(provider_facets),
        }

    @staticmethod
    def _provider_facets(groups: list[dict]) -> list[dict]:
        """x1-provider-ui §2.2/§4.2: sum each provider's live link count
        across every resource-group summary, ordered by ``PROVIDER_ORDER``
        (any unexpected code -- there should never be one -- sorts after,
        alphabetically, rather than being silently dropped)."""
        totals: dict[str, int] = {}
        for group in groups:
            for code, count in (group.get("providers") or {}).items():
                totals[code] = totals.get(code, 0) + count
        ordered_codes = [code for code in PROVIDER_ORDER if code in totals]
        ordered_codes += sorted(code for code in totals if code not in PROVIDER_ORDER)
        return [
            {"provider": code, "label": library_normalize.PROVIDERS.get(code, code), "link_count": totals[code]}
            for code in ordered_codes
        ]

    def group_detail(self, group_id: int) -> dict | None:
        conn = self.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT id, media_id, edition_fingerprint, season_from, season_to, episode_from, episode_to, "
                "complete_season, quality, source_type, hdr, video_codec, audio_summary, subtitle_summary, "
                "tags_json, display_title, needs_review, review_reason, link_count, has_115 "
                "FROM resource_group WHERE id=?",
                (group_id,),
            ).fetchone()
            if row is None:
                return None
            payload = self._group_summary_payload(conn, row)
        finally:
            conn.close()
        payload["media_id"] = row["media_id"]
        return payload

    def _group_summary_payload(
        self, conn: sqlite3.Connection, group_row: sqlite3.Row, provider: str | None = None,
    ) -> dict:
        """A resource group's safe API payload: spec/summary fields, inline
        safe link summaries (§13.2 -- no separate ``group_detail`` round
        trip needed) and normalised spec chips (§12.3). Used by both
        ``media_detail`` and ``group_detail``/``/api/library/resource/<id>``.

        When ``provider`` is given, ``providers``/``link_count``/``links``
        are all computed from a SQL-level ``WHERE ... AND provider=?``
        filter (§14.1) rather than the unfiltered baseline: a group with no
        matching link ends up with an empty ``links`` list (the caller,
        ``media_detail``, drops it entirely), and ``has_115`` reflects only
        whether the filtered provider itself is ``"115"`` -- never leaking
        that a *different*, hidden 115 link exists in the same group.

        Without ``provider``, ``link_count``/``has_115`` come straight from
        ``resource_group``'s own precomputed columns -- live-only since
        ``recount()`` excludes deleted-at-source links from them (see its
        docstring), so this baseline already agrees with the filtered
        branch above without any extra query here. Either way, a group
        whose resulting ``link_count`` is 0 (every link deleted) is dropped
        by the caller too, not just an all-empty ``links`` list.
        """
        link_params: list = [group_row["id"]]
        provider_clause = ""
        if provider is not None:
            provider_clause = " AND resource_link.provider=?"
            link_params.append(provider)

        # T17 fix wave 1 item 4: live links only -- the same way
        # library_search's own per-item provider counts already do -- so a
        # deleted link never inflates `providers`/`link_count`/`has_115`
        # (or the `provider_facets` totals summed from them) even though it
        # still appears, as a row, in `links` below (flagged `deleted:
        # True`). Detail badges and search-card counts now agree.
        provider_rows = conn.execute(
            f"SELECT provider, COUNT(*) FROM resource_link WHERE group_id=?{provider_clause} "
            f"AND {live_link_sql('resource_link')} GROUP BY provider",
            link_params,
        ).fetchall()
        providers = {row[0]: row[1] for row in provider_rows}

        # w6: LEFT JOIN link_check so each link row carries its own checker
        # verdict (never a URL/access code -- just status/reason/checked_at)
        # alongside `deleted` -- see _link_payload().
        link_rows = conn.execute(
            f"SELECT resource_link.public_id, resource_link.provider, resource_link.url_label, "
            f"resource_link.has_access_code, resource_link.remark, resource_link.created_at_source, "
            f"resource_link.deleted_at_source, lc.status AS check_status, lc.reason AS check_reason, "
            f"lc.checked_at AS check_checked_at "
            f"FROM resource_link LEFT JOIN link_check lc ON lc.provider = resource_link.provider "
            f"AND lc.canonical_url_hash = resource_link.canonical_url_hash "
            f"WHERE resource_link.group_id=?{provider_clause} "
            f"ORDER BY (resource_link.provider='115') DESC, (resource_link.created_at_source IS NULL) ASC, "
            f"resource_link.created_at_source DESC",
            link_params,
        ).fetchall()
        links = [_link_payload(row) for row in link_rows]

        if provider is not None:
            link_count = providers.get(provider, 0)
            has_115 = "115" in providers
        else:
            link_count = group_row["link_count"]
            has_115 = bool(group_row["has_115"])

        return {
            "group_id": group_row["id"],
            "display_title": group_row["display_title"],
            "quality": group_row["quality"],
            "hdr": group_row["hdr"],
            "season_from": group_row["season_from"],
            "season_to": group_row["season_to"],
            "episode_from": group_row["episode_from"],
            "episode_to": group_row["episode_to"],
            "complete_season": bool(group_row["complete_season"]),
            "source_type": group_row["source_type"],
            "video_codec": group_row["video_codec"],
            "audio_summary": group_row["audio_summary"],
            "subtitle_summary": group_row["subtitle_summary"],
            "tags": json.loads(group_row["tags_json"] or "[]"),
            "review_reason": _sanitize_review_reason(group_row["review_reason"]),
            "specs": library_normalize.resource_specs(group_row["quality"], group_row["hdr"], group_row["source_type"]),
            "link_count": link_count,
            "has_115": has_115,
            "needs_review": bool(group_row["needs_review"]),
            "providers": providers,
            "links": links,
        }

    def link_by_public_id(self, public_id: str) -> sqlite3.Row | None:
        conn = self.connect(readonly=True)
        try:
            return conn.execute("SELECT * FROM resource_link WHERE public_id=?", (public_id,)).fetchone()
        finally:
            conn.close()

    def reveal(self, public_id: str) -> tuple[str, str | None]:
        """Decrypt and return ``(url, access_code)`` for ``public_id``.

        Raises :class:`LibraryKeyUnavailable` when no key is configured or
        the configured key cannot decrypt the stored ciphertext (missing or
        rotated master key) -- mirrors ``secret_get``'s error-handling style
        per docs §2.4. Raises ``KeyError`` if the link doesn't exist (the
        HTTP layer is expected to have already checked existence/provider
        via ``link_by_public_id`` before calling this).
        """
        if self.fernet is None:
            raise LibraryKeyUnavailable("no decryption key configured")
        row = self.link_by_public_id(public_id)
        if row is None:
            raise KeyError(public_id)
        try:
            url = self.fernet.decrypt(bytes(row["url_ciphertext"])).decode("utf-8")
        except (InvalidToken, TypeError, ValueError) as exc:
            raise LibraryKeyUnavailable("failed to decrypt link url") from exc
        access_code = None
        if row["access_code_ciphertext"] is not None:
            try:
                access_code = self.fernet.decrypt(bytes(row["access_code_ciphertext"])).decode("utf-8")
            except (InvalidToken, TypeError, ValueError) as exc:
                raise LibraryKeyUnavailable("failed to decrypt access code") from exc
        return url, access_code

    # -----------------------------------------------------------------
    # w6: link validity checker query/write helpers. Never select or return
    # url_plain/url_ciphertext/access_code_* here -- the checker decrypts a
    # due link's URL just-in-time via reveal(public_id) right before
    # probing it, and never persists or logs it.
    # -----------------------------------------------------------------

    def due_link_checks(self, provider: str, limit: int, *, now: int) -> list[sqlite3.Row]:
        """Live (``deleted_at_source IS NULL``) links of ``provider`` that
        are due for a check: never checked, or ``next_check_at <= now``.
        Ordered by ``priority`` DESC first (a group queued via the
        ``recheck`` endpoint jumps the queue), then never-checked links,
        then soonest-due, then by id for a stable tie-break. ``group_id``
        and ``prior_status`` (the row's own status before this check, or
        ``None`` when it has never been checked) let
        ``run_link_check_round`` (w6-checker-fix C2) tell whether a fresh
        verdict actually flips the link's LIVE status, so it knows which
        groups need an incremental recount."""
        conn = self.connect(readonly=True)
        try:
            return conn.execute(
                "SELECT rl.public_id AS public_id, rl.canonical_url_hash AS canonical_url_hash, "
                "rl.group_id AS group_id, lc.status AS prior_status, "
                "COALESCE(lc.consecutive_unknown, 0) AS consecutive_unknown "
                "FROM resource_link rl LEFT JOIN link_check lc "
                "ON lc.provider = rl.provider AND lc.canonical_url_hash = rl.canonical_url_hash "
                "WHERE rl.provider = ? AND rl.deleted_at_source IS NULL "
                "AND (lc.next_check_at IS NULL OR lc.next_check_at <= ?) "
                "ORDER BY COALESCE(lc.priority, 0) DESC, (lc.next_check_at IS NULL) DESC, "
                "lc.next_check_at ASC, rl.id ASC LIMIT ?",
                (provider, now, limit),
            ).fetchall()
        finally:
            conn.close()

    def sample_live_links(self, provider: str, limit: int) -> list[sqlite3.Row]:
        """``limit`` random LIVE links of ``provider``, regardless of due
        status -- the CLI's ``--sample N`` (calibration: a representative
        slice of real links, not just whatever happens to be due today).
        Same row shape as ``due_link_checks`` (including ``group_id``,
        ``prior_status`` and ``consecutive_unknown``) so both feed
        ``run_link_check_round`` identically."""
        conn = self.connect(readonly=True)
        try:
            return conn.execute(
                "SELECT rl.public_id AS public_id, rl.canonical_url_hash AS canonical_url_hash, "
                "rl.group_id AS group_id, lc.status AS prior_status, "
                "COALESCE(lc.consecutive_unknown, 0) AS consecutive_unknown "
                "FROM resource_link rl LEFT JOIN link_check lc "
                "ON lc.provider = rl.provider AND lc.canonical_url_hash = rl.canonical_url_hash "
                "WHERE rl.provider = ? AND rl.deleted_at_source IS NULL "
                "ORDER BY RANDOM() LIMIT ?",
                (provider, limit),
            ).fetchall()
        finally:
            conn.close()

    def record_link_check(
        self,
        provider: str,
        canonical_url_hash: str,
        *,
        status: str,
        reason: str | None,
        http_class: str | None,
        checked_at: int,
        next_check_at: int,
        consecutive_unknown: int,
    ) -> None:
        """Upsert one link's check verdict. Clears ``priority`` back to 0 --
        a priority queued via ``queue_group_for_recheck`` has now been
        served."""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO link_check (provider, canonical_url_hash, status, reason, http_class, "
                "checked_at, next_check_at, consecutive_unknown, priority) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(provider, canonical_url_hash) DO UPDATE SET "
                "status=excluded.status, reason=excluded.reason, http_class=excluded.http_class, "
                "checked_at=excluded.checked_at, next_check_at=excluded.next_check_at, "
                "consecutive_unknown=excluded.consecutive_unknown, priority=0",
                (provider, canonical_url_hash, status, reason, http_class, checked_at, next_check_at, consecutive_unknown),
            )
            conn.commit()
        finally:
            conn.close()

    def queue_group_for_recheck(
        self, group_id: int, enabled_providers: list[str], supported_providers: "list[str] | None" = None,
    ) -> tuple[int, int, int]:
        """Queue every LIVE link of ``group_id`` whose provider is in
        ``enabled_providers`` for an immediate, highest-priority check
        (``priority=1, next_check_at=0``) -- a link never checked before
        gets a stub ``'queued'`` row (M3: a distinct marker, not
        ``'unknown'`` -- see ``link_check_counts``/``live_link_sql``) so it
        can carry that priority too (it would otherwise already be "due"
        anyway, just without priority over the rest of the queue). Returns
        ``(queued, skipped_disabled, skipped_unsupported)``.

        w6-checker-fix M4: a link whose provider is a phase-1 code (in
        ``supported_providers``) but not currently enabled counts toward
        ``skipped_disabled``; a link whose provider has no checker adapter
        at all (baidu/ed2k/139cloud/... -- never in ``supported_providers``
        regardless of settings) counts toward ``skipped_unsupported``
        instead -- recheck could never queue it no matter what the user
        toggles. When ``supported_providers`` is omitted, every non-enabled
        provider counts as ``skipped_disabled`` (never ``skipped_
        unsupported``) -- a caller that doesn't know the full supported set
        has no way to tell the two apart, matching this method's pre-M4
        behaviour exactly."""
        enabled = set(enabled_providers)
        supported = set(supported_providers) if supported_providers is not None else None
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT provider, canonical_url_hash FROM resource_link WHERE group_id=? AND deleted_at_source IS NULL",
                (group_id,),
            ).fetchall()
            queued = 0
            skipped_disabled = 0
            skipped_unsupported = 0
            for row in rows:
                provider, url_hash = row["provider"], row["canonical_url_hash"]
                if provider not in enabled:
                    if supported is None or provider in supported:
                        skipped_disabled += 1
                    else:
                        skipped_unsupported += 1
                    continue
                conn.execute(
                    "INSERT INTO link_check (provider, canonical_url_hash, status, reason, http_class, "
                    "checked_at, next_check_at, consecutive_unknown, priority) "
                    "VALUES (?, ?, 'queued', NULL, NULL, NULL, 0, 0, 1) "
                    "ON CONFLICT(provider, canonical_url_hash) DO UPDATE SET priority=1, next_check_at=0",
                    (provider, url_hash),
                )
                queued += 1
            conn.commit()
            return queued, skipped_disabled, skipped_unsupported
        finally:
            conn.close()

    def link_check_counts(self, providers: list[str], *, now: int) -> dict[str, dict]:
        """Per-``provider`` (restricted to ``providers``, the phase-1 codes)
        counts of LIVE links by checker status -- ``valid``/``invalid``/
        ``unknown``/``unchecked`` (no ``link_check`` row at all) plus
        ``due`` (currently eligible for a check), for
        ``/api/library/linkcheck-status`` and the CLI's summary."""
        conn = self.connect(readonly=True)
        try:
            result: dict[str, dict] = {}
            for code in providers:
                counts = {"valid": 0, "invalid": 0, "unknown": 0, "unchecked": 0}
                for status, n in conn.execute(
                    "SELECT lc.status AS status, COUNT(*) AS n FROM resource_link rl "
                    "LEFT JOIN link_check lc ON lc.provider = rl.provider AND lc.canonical_url_hash = rl.canonical_url_hash "
                    "WHERE rl.provider = ? AND rl.deleted_at_source IS NULL GROUP BY lc.status",
                    (code,),
                ):
                    # M3: a row with no link_check at all (status IS NULL)
                    # and a "queued" stub row (queue_group_for_recheck's
                    # priority marker for a never-checked link) are both
                    # "not actually checked yet" -- distinct GROUP BY
                    # buckets that both fold into "unchecked", never into
                    # "unknown".
                    if status is None or status == "queued":
                        counts["unchecked"] += n
                    elif status in counts:
                        counts[status] = n
                due_row = conn.execute(
                    "SELECT COUNT(*) FROM resource_link rl LEFT JOIN link_check lc "
                    "ON lc.provider = rl.provider AND lc.canonical_url_hash = rl.canonical_url_hash "
                    "WHERE rl.provider = ? AND rl.deleted_at_source IS NULL "
                    "AND (lc.next_check_at IS NULL OR lc.next_check_at <= ?)",
                    (code, now),
                ).fetchone()
                counts["due"] = due_row[0] if due_row else 0
                result[code] = counts
            return result
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# T4.1 helpers: filters() cache, labels, link actions
# ---------------------------------------------------------------------------

_FILTERS_CACHE: dict[str, tuple[float, dict]] = {}
FILTERS_CACHE_TTL_SECONDS = 300.0

_MEDIA_TYPE_LABELS = {"movie": "电影", "tv": "剧集", "unknown": "待定"}
_QUALITY_LABELS = {"2160p": "2160p", "1080p": "1080p", "720p": "720p", "other": "其他画质"}
_HDR_LABELS = {"dv": "杜比视界", "dv_hdr": "DV/HDR", "hdr10plus": "HDR10+", "hdr10": "HDR10", "hlg": "HLG", "sdr": "SDR"}
_QUALITY_RANK = {"2160p": 3, "1080p": 2, "720p": 1}

# x1-provider-ui §2.2: fixed display priority for media_detail's provider
# facets -- mirrors the work order's tab order, independent of link counts.
PROVIDER_ORDER: tuple[str, ...] = (
    "115", "tianyicloud", "quark", "alipan", "baidu", "guangya", "139cloud", "123", "ed2k", "unknown",
)

# 115 -> transfer only (never reveal); ed2k -> copy only (no browser can
# open it); every other real URL provider -> open+copy. "unknown" is a
# provider switch for everything EXCEPT itself: plan §4.4 says an
# "unknown" link with a real URL (of a host outside the allowlist) still
# gets open+copy, and only non-URL text -- identified by the label
# ``library_normalize`` builds for it, INVALID_LINK_LABEL -- gets none.
_LINK_ACTIONS: dict[str, tuple[str, ...]] = {"115": ("transfer",), "ed2k": ("copy",)}

# Allowlist of review_reason codes scripts/import_media_library.py writes
# (comma-joined, see its `reasons_base`/`agg.conflicts` keys). resource_group
# rows may predate this allowlist or (in theory) be written by another path,
# so _sanitize_review_reason() below always filters the stored value against
# this set rather than trusting it -- unknown tokens (and definitely raw
# spreadsheet cell text) are dropped, never returned to a client.
REVIEW_REASON_CODES = {
    "title_alias_conflict",
    "year_missing",
    "edition_unparsed",
    "link_shared_across_groups",
    "row_shifted",
    "access_code_conflict",
    "access_code_divergence",
    "timestamp_unparsed",
}

_REVIEW_REASON_SPLIT_RE = re.compile(r"[,;\s]+")


def _sanitize_review_reason(raw: str | None) -> list[str]:
    if not raw:
        return []
    tokens = _REVIEW_REASON_SPLIT_RE.split(raw.strip())
    return [token for token in tokens if token in REVIEW_REASON_CODES]


def _link_actions(provider: str, label: str | None = None) -> list[str]:
    if provider in _LINK_ACTIONS:
        return list(_LINK_ACTIONS[provider])
    if provider == "unknown" and label == library_normalize.INVALID_LINK_LABEL:
        return []
    return ["open", "copy"]


def _iso_date(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()


def _iso_datetime(epoch: int | None) -> str | None:
    """Full ISO-8601 UTC timestamp (w6-contract: link_check's ``checked_at``),
    unlike ``_iso_date``'s date-only string used for ``created_at``. Matches
    app.py's own ``iso()`` helper's ``+00:00``-suffixed format (used
    elsewhere across this same API, e.g. token/cookie timestamps) rather
    than a ``Z`` suffix, so every ISO timestamp in the library API looks
    the same."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _link_payload(row: sqlite3.Row) -> dict:
    # w6-contract: check_status/check_reason/checked_at/invalid come from a
    # LEFT JOIN against link_check (see _group_summary_payload's link_rows
    # query) -- absent for a link never checked (None/None/None/False), the
    # same as `deleted` was already absent for a link never removed at the
    # source.
    row_keys = row.keys()
    check_status = row["check_status"] if "check_status" in row_keys else None
    check_reason = row["check_reason"] if "check_reason" in row_keys else None
    checked_at = row["check_checked_at"] if "check_checked_at" in row_keys else None
    return {
        "link_id": row["public_id"],
        "provider": row["provider"],
        "label": row["url_label"],
        "has_access_code": bool(row["has_access_code"]),
        "deleted": row["deleted_at_source"] is not None,
        "check_status": check_status,
        "check_reason": check_reason,
        "checked_at": _iso_datetime(checked_at),
        "invalid": check_status == "invalid",
        "remark": row["remark"],
        "created_at": _iso_date(row["created_at_source"]),
        "actions": _link_actions(row["provider"], row["url_label"]),
    }


# Library paths whose on-open migration already succeeded in this process
# (see open_installed). Tests reset it via `_MIGRATED_LIBRARY_PATHS.clear()`.
_MIGRATED_LIBRARY_PATHS: set[str] = set()


def open_installed(db_path: Path, fernet: Fernet | None) -> LibraryStore:
    """Open an already-installed production library for read access.

    Order matches the T4 brief exactly: missing file -> LibraryNotInstalled;
    not a readable SQLite database (truncated/corrupt) ->
    LibraryIndexUnreadable; schema newer than this code supports ->
    LibrarySchemaMismatch; ``encrypted != 1`` -> LibraryNotEncrypted (a
    plaintext bundle must never be served, docs §2.4/§9).

    T15 fix wave 1 (item 4): this function used to run
    ``store.connect().close()`` unconditionally here (to lazily run
    ``_ensure_media_metadata_columns`` on a pre-T15 library) on every single
    call, which (a) needlessly opened a write connection for what is almost
    always a same-columns no-op, and (b) made ``open_installed`` -- and
    therefore every read route -- fail outright against library storage
    that is genuinely read-only (a write connection there raises
    ``sqlite3.OperationalError``, a ``DatabaseError`` subclass, which this
    function's own ``except`` clause used to misreport as
    ``LibraryIndexUnreadable``, "the file is corrupt"). The media-table
    migration happens at install time (``install_bundle``'s freshly-copied
    bundle already has every column via ``SCHEMA_SQL``, and
    ``_inherit_from_existing`` migrates the OLD library it reads from
    before that read) and lazily at the first WRITE connection thereafter
    (``LibraryStore.connect(readonly=False)`` self-heals via
    ``_ensure_media_metadata_columns`` on every call).

    w6-checker-fix (C1) reintroduces exactly ONE short write connection
    here, below -- unlike the media-column migration above, ``link_check``/
    ``link_check_state`` have no "install time" migration path to rely on:
    a production library installed by a pre-w6 binary is never re-run
    through ``install_bundle``/``_inherit_from_existing`` just because the
    app code was upgraded, so without a write attempt HERE those two
    tables would never come to exist on such a library, and every read
    below (a readonly connection, which can never ``CREATE TABLE``) would
    raise ``OperationalError: no such table: link_check`` forever. The
    attempt is best-effort: genuinely read-only storage (this function's
    own read-only-storage guarantee, tested by
    ``TestOpenInstalledIsReadOnly``) still has to open, so a write failure
    here is swallowed -- a table still missing at that point means the
    storage was never migrated by an install/inherit pass either, which is
    a pre-existing ops problem no amount of retrying here can fix.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise LibraryNotInstalled(f"library index not installed: {db_path}")
    store = LibraryStore(db_path, fernet)
    try:
        store.check_schema()
        encrypted = store.is_encrypted()
    except sqlite3.DatabaseError as exc:
        raise LibraryIndexUnreadable(f"library index unreadable: {db_path}") from exc
    if not encrypted:
        raise LibraryNotEncrypted("library index is not encrypted")
    # w6 review follow-up: run the write-side migration (T15 columns +
    # link_check tables) once per library path per process instead of on
    # every request, and make a failed attempt observable (class name only)
    # rather than silently leaving every later read to fail with
    # "no such table".
    key = str(Path(db_path).resolve())
    if key not in _MIGRATED_LIBRARY_PATHS:
        try:
            store.connect(readonly=False).close()
            _MIGRATED_LIBRARY_PATHS.add(key)
        except sqlite3.OperationalError as exc:
            LOG.warning("library migration on open failed path_key=%s error=%s", key[-16:], type(exc).__name__)
    return store


# ---------------------------------------------------------------------------
# T4.2 install / rollback (production-key encryption)
# ---------------------------------------------------------------------------

# Minimal, duplicated DDL for tmdb_cache/tmdb_budget (matches
# library_tmdb.ensure_tables exactly -- see docs §3). Duplicated rather than
# imported so library_store.py stays decoupled from library_tmdb (the whole
# point of EXTRA_SCHEMA_HOOKS, see this module's own docstring); used only
# defensively here so install_bundle/inherit never crash on a bundle or an
# existing production library that happens not to have these tables yet
# (they are otherwise self-healing -- every real read/write path in
# library_tmdb.py already calls ensure_tables() before touching them).
_TMDB_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS tmdb_cache (
  cache_key TEXT PRIMARY KEY,
  tmdb_id INTEGER, media_type TEXT, language TEXT,
  payload_json TEXT, status TEXT NOT NULL,
  fetched_at INTEGER NOT NULL, retry_after INTEGER, error_class TEXT
);
CREATE TABLE IF NOT EXISTS tmdb_budget (
  day TEXT PRIMARY KEY, used INTEGER NOT NULL, budget INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
"""

# w6: same defensive-duplication role as _TMDB_TABLES_DDL above, for a
# bundle or existing production library built before link_check/
# link_check_state existed (unlike tmdb_cache/tmdb_budget, these two ARE
# also in this module's own SCHEMA_SQL -- see its comment -- so a bundle
# built by the current binary already has them; this only matters for an
# `old_path` production library from before this change).
_LINK_CHECK_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS link_check (
  provider TEXT NOT NULL, canonical_url_hash TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT, http_class TEXT, checked_at INTEGER, next_check_at INTEGER,
  consecutive_unknown INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (provider, canonical_url_hash)
);
CREATE INDEX IF NOT EXISTS idx_link_check_due ON link_check(provider, next_check_at);
CREATE TABLE IF NOT EXISTS link_check_state (
  key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER
);
"""


def _validate_bundle(bundle: Path) -> None:
    if not bundle.exists():
        raise LibraryBundleInvalid(f"bundle not found: {bundle}")
    store = LibraryStore(bundle)
    version = store.meta_get("schema_version")
    if version != str(SCHEMA_VERSION):
        raise LibraryBundleInvalid(f"unsupported bundle schema_version: {version!r}")
    if store.meta_get("encrypted") != "0":
        raise LibraryBundleInvalid("bundle must be unencrypted (schema_meta.encrypted == '0')")
    if not store.meta_get("normalize_version"):
        raise LibraryBundleInvalid("bundle is missing schema_meta.normalize_version")


def _title_key(title_zh: str, year: int | None) -> str:
    """Secondary inherit-lookup key: ``search_key(title_zh)`` + year, with
    ``media_type`` deliberately excluded -- see ``_inherit_from_existing``."""
    return f"{library_normalize.search_key(title_zh)}:{year or 0}"


_BACKUP_STATUS_RANK = {"exact": 3, "candidate": 2, "needs_review": 1}


def _status_rank(match_status: str) -> int:
    """Recovery-precedence rank for a ``match_status`` value, used by the
    ``.bak`` recovery pass in ``_inherit_from_existing`` (``backup_pass``):
    ``'unmatched'`` and anything unrecognised rank 0 (lowest)."""
    return _BACKUP_STATUS_RANK.get(match_status, 0)


def _inherit_from_existing(old_path: Path, new_path: Path, *, backup_pass: bool = False) -> int:
    """Carry TMDB match state forward from ``old_path`` into the freshly-
    copied bundle (``new_path``).

    Primary lookup is by ``media_identity``. ``library_tmdb.merge_by_tmdb``
    rewrites an exact-matched row's identity from ``title:<key>:<year>:
    <type>`` to ``tmdb:<type>:<id>``, while a freshly re-imported bundle
    row always starts with a title identity -- so the primary lookup
    misses every such row. When it misses, a secondary lookup by
    ``_title_key`` (title/year, not media_type -- the exact write can
    overwrite media_type with TMDB's type, which may differ from the
    freshly re-inferred type) is used, but ONLY when unambiguous: after
    identity matches are set aside, exactly one remaining old row and
    exactly one remaining new row must share that key. When a key group is
    NOT 1:1, ONE deterministic tiebreak is tried before giving up: pair the
    remaining old/new rows within that group by media_type (the old row's
    stored type -- TMDB's type for an exact row -- vs the new row's freshly
    inferred type); only the pairs that become 1:1 within the group by type
    are applied. Anything still ambiguous leaves the new row untouched
    (never guess). Rows folded away by ``_merge_media_rows`` are simply
    absent from ``old_path`` and are not found -- no crash.

    When ``backup_pass`` is set (the ``.bak`` recovery pass -- see
    ``install_bundle``), a match is applied only when the old (backup) row's
    match_status ranks strictly higher (``_status_rank``: exact=3,
    candidate=2, needs_review=1, other/unmatched=0) than the new row's
    current match_status -- so a `.bak` exact row can recover a
    needs_review/candidate row the enricher produced after a match was
    lost, while an equal-or-lower-ranked backup row (including an
    'unmatched' one) never overwrites or downgrades the new row.
    tmdb_cache/tmdb_budget are not carried over on this pass (they already
    came from the current library's own call).

    Returns the number of new rows THIS call actually replaced with a
    meaningful (non-'unmatched') match.
    """
    # T15 addendum: `_inherit_from_existing` must carry the new metadata/
    # ratings columns forward too -- migrate a pre-T15 `old_path` in place
    # (one short write connection) before reading it, so the SELECT below
    # can rely on every column existing regardless of when `old_path` was
    # last installed.
    _migrate_conn = sqlite3.connect(old_path)
    try:
        _ensure_media_metadata_columns(_migrate_conn)
    finally:
        _migrate_conn.close()

    old_store = LibraryStore(old_path)
    old_conn = old_store.connect(readonly=True)
    new_conn = sqlite3.connect(new_path)
    new_conn.row_factory = sqlite3.Row
    try:
        new_conn.executescript(_TMDB_TABLES_DDL)
        new_conn.executescript(_LINK_CHECK_TABLES_DDL)

        old_media = {
            row["media_identity"]: row
            for row in old_conn.execute(
                "SELECT media_identity, tmdb_id, media_type, title_zh, year, title_original, overview, poster_path, "
                "backdrop_path, genres_json, match_status, match_score, match_candidates_json, "
                "metadata_status, metadata_source, tvmaze_id, imdb_id, metadata_fetched_at, metadata_error, "
                "ratings_json, ratings_status, ratings_fetched_at, ratings_error FROM media"
            ).fetchall()
        }
        new_media = new_conn.execute(
            "SELECT id, media_identity, title_zh, year, match_status, media_type FROM media"
        ).fetchall()

        # Primary: identity match.
        matched_old_row: dict[int, sqlite3.Row] = {}
        used_old_identities: set[str] = set()
        for row in new_media:
            old_row = old_media.get(row["media_identity"])
            if old_row is not None:
                matched_old_row[row["id"]] = old_row
                used_old_identities.add(row["media_identity"])

        # Secondary: unambiguous title_key match among whatever identity
        # matching left untouched on both sides.
        remaining_old_by_key: dict[str, list] = {}
        for identity, old_row in old_media.items():
            if identity in used_old_identities:
                continue
            remaining_old_by_key.setdefault(_title_key(old_row["title_zh"], old_row["year"]), []).append(old_row)

        remaining_new_by_key: dict[str, list] = {}
        for row in new_media:
            if row["id"] in matched_old_row:
                continue
            remaining_new_by_key.setdefault(_title_key(row["title_zh"], row["year"]), []).append(row)

        for key, new_rows in remaining_new_by_key.items():
            old_rows = remaining_old_by_key.get(key)
            if not old_rows:
                continue
            if len(old_rows) == 1 and len(new_rows) == 1:
                matched_old_row[new_rows[0]["id"]] = old_rows[0]
                continue
            # Not 1:1 within this title/year key -- try ONE deterministic
            # tiebreak before giving up: pair the remaining old/new rows
            # within the group by media_type. Only pairs that become 1:1
            # within the group by type are applied; anything still
            # ambiguous (e.g. two rows sharing the same type) is left
            # untouched.
            old_by_type: dict[str, list] = {}
            for old_row in old_rows:
                old_by_type.setdefault(old_row["media_type"], []).append(old_row)
            new_by_type: dict[str, list] = {}
            for new_row in new_rows:
                new_by_type.setdefault(new_row["media_type"], []).append(new_row)
            for media_type, typed_new_rows in new_by_type.items():
                typed_old_rows = old_by_type.get(media_type)
                if typed_old_rows and len(typed_old_rows) == 1 and len(typed_new_rows) == 1:
                    matched_old_row[typed_new_rows[0]["id"]] = typed_old_rows[0]

        inherited = 0
        for row in new_media:
            old_row = matched_old_row.get(row["id"])
            if old_row is None:
                continue
            if backup_pass and _status_rank(old_row["match_status"]) <= _status_rank(row["match_status"]):
                continue
            if old_row["match_status"] != "unmatched":
                inherited += 1
            inherited_media_type = old_row["media_type"] if old_row["match_status"] == "exact" else None
            new_conn.execute(
                """
                UPDATE media SET
                    tmdb_id = ?,
                    media_type = COALESCE(?, media_type),
                    title_original = ?, overview = ?, poster_path = ?, backdrop_path = ?,
                    genres_json = ?, match_status = ?, match_score = ?, match_candidates_json = ?,
                    metadata_status = ?, metadata_source = ?, tvmaze_id = ?, imdb_id = ?,
                    metadata_fetched_at = ?, metadata_error = ?,
                    ratings_json = ?, ratings_status = ?, ratings_fetched_at = ?, ratings_error = ?
                WHERE id = ?
                """,
                (
                    old_row["tmdb_id"], inherited_media_type, old_row["title_original"], old_row["overview"],
                    old_row["poster_path"], old_row["backdrop_path"], old_row["genres_json"],
                    old_row["match_status"], old_row["match_score"], old_row["match_candidates_json"],
                    old_row["metadata_status"], old_row["metadata_source"], old_row["tvmaze_id"], old_row["imdb_id"],
                    old_row["metadata_fetched_at"], old_row["metadata_error"],
                    old_row["ratings_json"], old_row["ratings_status"], old_row["ratings_fetched_at"], old_row["ratings_error"],
                    row["id"],
                ),
            )

        if not backup_pass:
            try:
                cache_rows = old_conn.execute(
                    "SELECT cache_key, tmdb_id, media_type, language, payload_json, status, fetched_at, "
                    "retry_after, error_class FROM tmdb_cache"
                ).fetchall()
            except sqlite3.OperationalError:
                cache_rows = []
            for row in cache_rows:
                new_conn.execute(
                    "INSERT OR IGNORE INTO tmdb_cache (cache_key, tmdb_id, media_type, language, payload_json, "
                    "status, fetched_at, retry_after, error_class) VALUES (?,?,?,?,?,?,?,?,?)",
                    tuple(row),
                )

            try:
                budget_rows = old_conn.execute("SELECT day, used, budget, updated_at FROM tmdb_budget").fetchall()
            except sqlite3.OperationalError:
                budget_rows = []
            for row in budget_rows:
                new_conn.execute(
                    "INSERT OR IGNORE INTO tmdb_budget (day, used, budget, updated_at) VALUES (?,?,?,?)",
                    tuple(row),
                )

            # w6-contract: link_check carried forward like tmdb_cache --
            # current library only (no .bak recovery pass), by primary key,
            # INSERT OR IGNORE.
            try:
                link_check_rows = old_conn.execute(
                    "SELECT provider, canonical_url_hash, status, reason, http_class, checked_at, "
                    "next_check_at, consecutive_unknown, priority FROM link_check"
                ).fetchall()
            except sqlite3.OperationalError:
                link_check_rows = []
            for row in link_check_rows:
                new_conn.execute(
                    "INSERT OR IGNORE INTO link_check (provider, canonical_url_hash, status, reason, "
                    "http_class, checked_at, next_check_at, consecutive_unknown, priority) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    tuple(row),
                )

            # w6-checker-fix I4: link_check_state (per-provider daily
            # budget counters, paused_until, last_error_class, heartbeat)
            # IS inherited too -- unlike tmdb_enricher_state, which really
            # is pure ephemeral leader-thread status, these values gate
            # real anonymous probing against a real remote host: losing a
            # pause or a today's-budget counter across a bundle re-install
            # would let the checker immediately re-probe a provider that
            # just got rate-limited, or blow well past its daily cap the
            # moment a new bundle is installed the same day. INSERT OR
            # IGNORE by key, current library only (no .bak recovery pass),
            # same as every other w6 table above -- a key the fresh install
            # already wrote (there shouldn't be one yet) is never
            # overwritten.
            try:
                state_rows = old_conn.execute(
                    "SELECT key, value, updated_at FROM link_check_state"
                ).fetchall()
            except sqlite3.OperationalError:
                state_rows = []
            for row in state_rows:
                new_conn.execute(
                    "INSERT OR IGNORE INTO link_check_state (key, value, updated_at) VALUES (?,?,?)",
                    tuple(row),
                )

        new_conn.commit()
    finally:
        old_conn.close()
        new_conn.close()
    return inherited


def _encrypt_plaintext_columns(db_path: Path, fernet: Fernet) -> tuple[int, int, int]:
    """Batch-encrypt url_plain/access_code_plain into the ciphertext columns
    (1000 rows at a time, keyset-paginated by id) and null the plaintext
    columns out. Returns (media_total, groups_total, links_total)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        media_total = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        groups_total = conn.execute("SELECT COUNT(*) FROM resource_group").fetchone()[0]
        links_total = conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0]

        last_id = 0
        while True:
            rows = conn.execute(
                "SELECT id, url_plain, access_code_plain FROM resource_link WHERE id > ? ORDER BY id LIMIT 1000",
                (last_id,),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                url_cipher = fernet.encrypt(row["url_plain"].encode("utf-8")) if row["url_plain"] is not None else None
                code_cipher = (
                    fernet.encrypt(row["access_code_plain"].encode("utf-8"))
                    if row["access_code_plain"] is not None
                    else None
                )
                conn.execute(
                    "UPDATE resource_link SET url_ciphertext=?, access_code_ciphertext=?, "
                    "url_plain=NULL, access_code_plain=NULL WHERE id=?",
                    (url_cipher, code_cipher, row["id"]),
                )
            conn.commit()
            last_id = rows[-1]["id"]
    finally:
        conn.close()
    return media_total, groups_total, links_total


def _use_temp_dir(directory: Path) -> None:
    """Point SQLite's temporary files (VACUUM's working copy, spilled sorts
    and statement journals) at ``directory`` for the rest of this process.

    The production install runs outside the app's systemd unit, possibly in
    a sandbox whose /tmp and /var/tmp are read-only or too small for a copy
    of the whole library; the library's own directory is the one place that
    is guaranteed writable. The pragma is consulted on every temp-file
    lookup; the environment variable covers SQLite builds compiled without
    deprecated pragmas (it is read the first time a temp file is needed).
    """
    os.environ.setdefault("SQLITE_TMPDIR", str(directory))
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA temp_store_directory = '%s'" % str(directory).replace("'", "''"))
    finally:
        conn.close()


def _vacuum(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


def _checkpoint_wal(db_path: Path) -> None:
    """Force every committed WAL frame into ``db_path`` itself (and
    truncate the ``-wal`` sidecar), then close.

    The production library is opened in WAL mode for every write (see
    ``LibraryStore.connect``), including the background TMDB enricher's
    own writes -- those commits can sit in ``<db_path>-wal`` rather than
    the main file for a while. A bare file-level operation (renaming
    ``db_path`` to a ``.bak-<ts>``, or reading it via ``_inherit_from_existing``)
    only touches the main file, so without this checkpoint a backup taken
    that way -- opened on its own, elsewhere -- could silently be missing
    the most recent commits.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _remove_sidecars(db_path: Path) -> None:
    """Remove any stale ``-wal``/``-shm`` sidecar files next to ``db_path``.

    Only the exact-named ``.db`` file gets renamed by ``os.replace`` during
    install/rollback; a leftover sidecar from the library that used to
    live at this path does not follow that rename and would otherwise sit
    next to (and, if ever opened in WAL mode, be wrongly applied against)
    a completely unrelated library.
    """
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def _bak_timestamp(path: Path) -> int:
    try:
        return int(path.name.rsplit(".bak-", 1)[-1])
    except ValueError:
        return -1


def install_bundle(bundle: Path, target: Path, fernet: Fernet, *, now: int | None = None) -> dict:
    """Install a plaintext bundle as the (encrypted) production library.

    Validates the bundle's schema_meta, copies it to ``target`` (mode 0640),
    inherits TMDB match state from any existing production library at
    ``target`` (keyed by media_identity), batch-encrypts url_plain/
    access_code_plain into the ciphertext columns, VACUUMs, rotates the old
    library to a single ``.bak-<ts>`` backup, then atomically replaces
    ``target``. On ANY exception the ``.tmp`` file is removed and the
    existing library at ``target`` is left completely untouched (the
    original exception is re-raised, with the failing stage name attached
    as ``install_stage`` so the CLI can report it).
    """
    bundle = Path(bundle)
    target = Path(target)
    tmp_path = target.with_suffix(".db.tmp")
    now_val = int(time.time()) if now is None else now

    stage = "validate"
    try:
        _validate_bundle(bundle)
        stage = "copy"
        target.parent.mkdir(parents=True, exist_ok=True)
        _use_temp_dir(target.parent)
        shutil.copy2(bundle, tmp_path)
        os.chmod(tmp_path, 0o640)

        inherited_matches = 0
        inherited_from_backup = 0
        if target.exists():
            stage = "inherit"
            _checkpoint_wal(target)
            inherited_matches = _inherit_from_existing(target, tmp_path)

            # Recovery source: the single .bak-<ts> kept from the PREVIOUS
            # install (read here, before this install's own rotation below
            # replaces it) still holds whatever the current library lost --
            # e.g. exact rows the primary/secondary lookup above still
            # couldn't place. Only overrides a new row when the backup
            # row's match_status ranks strictly higher (_status_rank) than
            # the new row's current status; never downgrades, and never
            # fails the install.
            existing_backups = sorted(
                target.parent.glob(target.name + ".bak-*"), key=_bak_timestamp
            )
            if existing_backups:
                try:
                    inherited_from_backup = _inherit_from_existing(
                        existing_backups[-1], tmp_path, backup_pass=True
                    )
                except Exception as exc:
                    LOG.warning(
                        "library install: .bak recovery skipped, error=%s", type(exc).__name__
                    )
                    inherited_from_backup = 0
                else:
                    inherited_matches += inherited_from_backup

        stage = "encrypt"
        media_total, groups_total, links_total = _encrypt_plaintext_columns(tmp_path, fernet)

        stage = "finalize"
        tmp_store = LibraryStore(tmp_path)
        tmp_store.meta_set("encrypted", "1")
        tmp_store.meta_set("installed_at", str(now_val))
        built_at = tmp_store.meta_get("built_at")

        # LibraryStore.connect()'s write path sets journal_mode=WAL, a
        # *persisted*, file-header setting -- every meta_set() call above
        # re-enabled it. Force it back to a journal mode with no lingering
        # sidecar (-wal/-shm) files as the last write, so the installed
        # library (and any .bak-<ts> rotated from it) is ever only a
        # single file; VACUUM then runs (and completes) under that mode.
        _conn = sqlite3.connect(tmp_path)
        try:
            _conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            _conn.close()

        stage = "vacuum"
        _vacuum(tmp_path)

        stage = "replace"
        if target.exists():
            backup_path = target.with_name(target.name + f".bak-{now_val}")
            # Checkpoint + drop target's own -wal/-shm sidecars while
            # `target` still unambiguously names the OLD library, right
            # before it becomes the backup. Doing this AFTER
            # os.replace(tmp_path, target) below (as this used to) is
            # unsafe: by then the same filenames belong to the newly-live
            # library, and a concurrent request's freshly-created -wal
            # could be deleted out from under it. Checkpointing here also
            # makes the resulting .bak-<ts> self-contained (see
            # _checkpoint_wal's docstring).
            _checkpoint_wal(target)
            _remove_sidecars(target)
            os.replace(target, backup_path)
            try:
                os.replace(tmp_path, target)
            except Exception:
                os.replace(backup_path, target)
                raise
            # Best-effort: the install has already fully succeeded at this
            # point (the new library is live at `target`), so a failure to
            # delete an old backup (e.g. a read-only file, a permissions
            # blip) must not be reported as a failed install.
            for old_bak in target.parent.glob(target.name + ".bak-*"):
                if old_bak != backup_path:
                    try:
                        old_bak.unlink()
                    except OSError:
                        continue
        else:
            os.replace(tmp_path, target)
    except Exception as exc:
        exc.install_stage = stage
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    return {
        "media_total": media_total,
        "groups_total": groups_total,
        "links_total": links_total,
        "inherited_matches": inherited_matches,
        "inherited_from_backup": inherited_from_backup,
        "built_at": built_at,
        "installed_at": str(now_val),
    }


def rollback_install(target: Path) -> dict:
    """Restore the single most recent ``.bak-<ts>`` backup over ``target``.

    Raises LibraryNotInstalled when no backup exists; LibraryNotEncrypted
    if the backup itself is somehow not a valid encrypted library.
    """
    target = Path(target)
    candidates = sorted(target.parent.glob(target.name + ".bak-*"), key=_bak_timestamp)
    if not candidates:
        raise LibraryNotInstalled("no backup available to roll back to")
    backup_path = candidates[-1]

    if LibraryStore(backup_path).meta_get("encrypted") != "1":
        raise LibraryNotEncrypted("backup library is not encrypted")

    if target.exists():
        # Same reasoning as install_bundle's rotation step: checkpoint the
        # current target's WAL and drop its sidecars before it's replaced,
        # so no stale -wal/-shm (whose frames belong to the library being
        # rolled BACK from) is left sitting next to the restored backup.
        _checkpoint_wal(target)
        _remove_sidecars(target)

    os.replace(backup_path, target)
    return LibraryStore(target).stats()
