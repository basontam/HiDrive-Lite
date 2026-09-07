"""Tests for library_store.py: schema, metadata, write/upsert API, recount, encryption guard.

All URLs/access codes below are fixtures, not real shares: hostnames use real
domain shapes but share codes are prefixed ``swfake``/``fake`` and access
codes are fixed 4-character placeholders, per the project's test-data rule.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_normalize  # noqa: E402
import library_store as ls  # noqa: E402

EXPECTED_TABLES = {
    "schema_meta",
    "media",
    "resource_group",
    "resource_link",
    "link_provenance",
    "search_doc",
    "search_term",
    "search_vocab",
    "search_charmap",
    "import_run",
    "tmdb_hints",
    "imdb_ratings",
    "link_check",
    "link_check_state",
}


def _hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _make_media(identity: str = "tt-fake-001", title: str = "虚构电影一") -> ls.MediaRecord:
    return ls.MediaRecord(
        media_identity=identity,
        media_type="movie",
        title_zh=title,
        search_key=title,
    )


def _make_group(media_id: int, fingerprint: str = "fp-fake-001") -> ls.GroupRecord:
    return ls.GroupRecord(
        media_id=media_id,
        edition_fingerprint=fingerprint,
        display_title="2160p WEB-DL · DV/HDR",
    )


def _make_link(
    group_id: int,
    provider: str = "115",
    url: str = "https://115.com/s/swfake000001",
    access_code: str = "ab12",
    deleted_at_source: int | None = None,
) -> ls.LinkRecord:
    return ls.LinkRecord(
        public_id=f"pub-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}",
        group_id=group_id,
        provider=provider,
        canonical_url_hash=_hash(url),
        url_label=f"{provider} 分享 · swf…1",
        has_access_code=1 if access_code else 0,
        deleted_at_source=deleted_at_source,
    )


# T15 fix wave 1 (item 5): the ``media`` table exactly as it looked before
# the T15 addendum's ten metadata/ratings columns existed -- used to test
# every migration entry point (_ensure_media_metadata_columns directly, a
# write connect() self-heal, and _inherit_from_existing) against a
# genuinely pre-T15 installed library.
_PRE_T15_MEDIA_TABLE_SQL = """
CREATE TABLE media (
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
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
"""


def _make_pre_t15_media_db(path, *, media_identity="tt-fake-pre-t15", title="旧版电影") -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_PRE_T15_MEDIA_TABLE_SQL)
        now = 1_700_000_000
        conn.execute(
            "INSERT INTO media (media_identity, media_type, title_zh, search_key, created_at, updated_at) "
            "VALUES (?, 'movie', ?, ?, ?, ?)",
            (media_identity, title, title, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    return library


class TestSchema:
    def test_create_schema_is_idempotent_and_matches_ddl(self, tmp_path, monkeypatch):
        # This asserts library_store.py's OWN SCHEMA_SQL in isolation, not
        # what an integrator's hook adds -- app.py registers
        # library_tmdb.ensure_tables onto the module-level EXTRA_SCHEMA_HOOKS
        # list at import time (T4), and since that list is process-global,
        # any earlier test in this same pytest session that imported app
        # (via the `hidrive`/`client` fixtures) leaves it registered for
        # every test that runs afterwards. Isolate it explicitly here.
        monkeypatch.setattr(ls, "EXTRA_SCHEMA_HOOKS", [])
        db_path = tmp_path / "media-library.db"
        library = ls.LibraryStore(db_path)
        library.create_schema()
        library.create_schema()  # second call must not raise

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        finally:
            conn.close()
        table_names = {row[0] for row in rows}

        assert table_names == EXPECTED_TABLES
        assert "tmdb_cache" not in table_names
        assert "tmdb_budget" not in table_names
        assert library.meta_get("schema_version") == "1"


class TestConnect:
    def test_readonly_on_missing_file_raises_not_installed(self, tmp_path):
        library = ls.LibraryStore(tmp_path / "missing.db")
        with pytest.raises(ls.LibraryNotInstalled):
            library.connect(readonly=True)

    def test_readonly_can_query_but_insert_fails(self, store):
        conn = store.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            assert row[0] == "1"
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO schema_meta (key, value) VALUES ('probe','probe')")
        finally:
            conn.close()

    def test_readonly_uri_handles_path_containing_space(self, tmp_path):
        db_dir = tmp_path / "has space"
        db_dir.mkdir()
        library = ls.LibraryStore(db_dir / "media-library.db")
        library.create_schema()

        conn = library.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "1"


class TestCheckSchema:
    def test_newer_schema_version_raises_mismatch(self, store):
        conn = sqlite3.connect(store.db_path)
        conn.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
        conn.commit()
        conn.close()

        with pytest.raises(ls.LibrarySchemaMismatch):
            store.check_schema()


class TestMediaMetadataMigration:
    """T15 fix wave 1 (item 5): a media table predating the T15 metadata/
    ratings columns must still be usable through every migration entry
    point -- ``_ensure_media_metadata_columns`` directly, a write
    ``connect()`` (self-heal), and ``_inherit_from_existing`` (migrates the
    OLD library before reading it)."""

    def test_ensure_media_metadata_columns_adds_all_ten_columns(self, tmp_path):
        db_path = tmp_path / "pre-t15.db"
        _make_pre_t15_media_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            before = {row[1] for row in conn.execute("PRAGMA table_info(media)").fetchall()}
            assert "metadata_status" not in before
            ls._ensure_media_metadata_columns(conn)
            after = {row[1] for row in conn.execute("PRAGMA table_info(media)").fetchall()}
        finally:
            conn.close()
        for name, _ddl in ls._MEDIA_METADATA_COLUMNS:
            assert name in after

    def test_ensure_media_metadata_columns_is_idempotent(self, tmp_path):
        db_path = tmp_path / "pre-t15.db"
        _make_pre_t15_media_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            ls._ensure_media_metadata_columns(conn)
            ls._ensure_media_metadata_columns(conn)  # second call must not raise
        finally:
            conn.close()

    def test_write_connect_self_heals_pre_t15_media_table(self, tmp_path):
        db_path = tmp_path / "pre-t15.db"
        _make_pre_t15_media_db(db_path)
        library = ls.LibraryStore(db_path)

        conn = library.connect()  # write connection -- must self-heal
        try:
            row = conn.execute("SELECT metadata_status, ratings_json, ratings_status FROM media").fetchone()
        finally:
            conn.close()
        assert row["metadata_status"] == "pending"
        assert row["ratings_json"] == "{}"
        assert row["ratings_status"] == "pending"

    def test_inherit_from_existing_migrates_pre_t15_old_library(self, tmp_path):
        old_path = tmp_path / "old.db"
        _make_pre_t15_media_db(old_path, media_identity="tt-shared-001", title="旧版电影")

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        new_store.upsert_media(_make_media(identity="tt-shared-001", title="旧版电影"))

        # Must not raise sqlite3.OperationalError ("no such column") reading
        # the pre-T15 old library's media table for the ten new columns.
        ls._inherit_from_existing(old_path, new_path)

        conn = sqlite3.connect(new_path)
        try:
            row = conn.execute(
                "SELECT metadata_status, ratings_json, ratings_status FROM media WHERE media_identity='tt-shared-001'"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("pending", "{}", "pending")

    def test_inherit_from_existing_carries_metadata_and_ratings_values(self, tmp_path):
        old_path = tmp_path / "old.db"
        old_store = ls.LibraryStore(old_path)
        old_store.create_schema()
        old_store.upsert_media(_make_media(identity="tt-shared-002", title="已迁移电影"))
        conn = old_store.connect()
        try:
            conn.execute(
                """
                UPDATE media SET
                    metadata_status='complete', metadata_source='tmdb', tvmaze_id=42, imdb_id='tt7654321',
                    metadata_fetched_at='2026-09-01T00:00:00+00:00', metadata_error=NULL,
                    ratings_json=?, ratings_status='complete', ratings_fetched_at='2026-09-01',
                    ratings_error=NULL
                WHERE media_identity='tt-shared-002'
                """,
                (json.dumps({"tmdb": {"score": 8.1, "scale": 10, "votes": 500, "as_of": "2026-09-01"}}),),
            )
            conn.commit()
        finally:
            conn.close()

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        new_store.upsert_media(_make_media(identity="tt-shared-002", title="已迁移电影"))

        ls._inherit_from_existing(old_path, new_path)

        conn = sqlite3.connect(new_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM media WHERE media_identity='tt-shared-002'").fetchone()
        finally:
            conn.close()
        assert row["metadata_status"] == "complete"
        assert row["metadata_source"] == "tmdb"
        assert row["tvmaze_id"] == 42
        assert row["imdb_id"] == "tt7654321"
        assert json.loads(row["ratings_json"]) == {"tmdb": {"score": 8.1, "scale": 10, "votes": 500, "as_of": "2026-09-01"}}
        assert row["ratings_status"] == "complete"


class TestInheritSecondaryLookup:
    """w4-inherit: `_inherit_from_existing`'s secondary title/year lookup,
    used when `library_tmdb.merge_by_tmdb` has rewritten an exact row's
    identity away from the fresh bundle's own title identity (see
    test_library_install.py's TestInstallReinstall for the full
    install-level reproduction/recovery coverage)."""

    def test_title_key_matches_the_key_embedded_in_a_title_identity(self, tmp_path):
        # w4-inherit-fix item 4: go through the REAL importer entry points
        # (library_normalize.parse_title/parse_edition/infer_media_type/
        # media_identity -- see scripts/import_media_library.py) and a real
        # LibraryStore round trip (write then read back), not a hand-built
        # TitleInfo -- guards against any discrepancy between what's
        # computed and what actually ends up persisted in
        # media_identity/title_zh/year. Asserts for EVERY stored row with a
        # "title:" identity, one with a year and one without.
        db_path = tmp_path / "media-library.db"
        db_store = ls.LibraryStore(db_path)
        db_store.create_schema()

        for title_cell in ("虚构电影三 (2021)", "虚构剧集四"):
            info = library_normalize.parse_title(title_cell, "")
            edition = library_normalize.parse_edition("", title_cell)
            media_type = library_normalize.infer_media_type(edition, title_cell)
            identity = library_normalize.media_identity(info, media_type, None)
            assert identity.startswith("title:")
            db_store.upsert_media(ls.MediaRecord(
                media_identity=identity, media_type=media_type, title_zh=info.title_zh,
                search_key=library_normalize.search_key(info.title_zh), year=info.year,
            ))

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT media_identity, title_zh, year FROM media").fetchall()
        finally:
            conn.close()

        title_rows = [row for row in rows if row["media_identity"].startswith("title:")]
        assert len(title_rows) == 2
        for row in title_rows:
            _prefix, embedded_key, embedded_year, _media_type = row["media_identity"].split(":")
            assert f"{embedded_key}:{embedded_year}" == ls._title_key(row["title_zh"], row["year"])

    def test_ambiguous_secondary_match_is_not_inherited(self, tmp_path):
        # Two old rows share a title/year but differ by type (and neither
        # has an identity match in the new library); the new row's own
        # type ('unknown') matches neither, so even the media_type
        # tiebreak (w4-inherit-fix item 5 -- see TestInheritMediaTypeTiebreak)
        # can't place it -- the secondary lookup must refuse to guess.
        old_path = tmp_path / "old.db"
        old_store = ls.LibraryStore(old_path)
        old_store.create_schema()
        id_movie = old_store.upsert_media(_make_media(identity="old-movie", title="同名资源"))
        id_tv = old_store.upsert_media(_make_media(identity="old-tv", title="同名资源"))
        conn = old_store.connect()
        conn.execute("UPDATE media SET year=2020, tmdb_id=111, media_type='movie', match_status='exact', "
                     "poster_path='/x.jpg' WHERE id=?", (id_movie,))
        conn.execute("UPDATE media SET year=2020, tmdb_id=222, media_type='tv', match_status='exact', "
                     "poster_path='/y.jpg' WHERE id=?", (id_tv,))
        conn.commit()
        conn.close()

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        new_store.upsert_media(_make_media(identity="new-row", title="同名资源"))
        conn = new_store.connect()
        conn.execute("UPDATE media SET year=2020, media_type='unknown' WHERE media_identity='new-row'")
        conn.commit()
        conn.close()

        inherited = ls._inherit_from_existing(old_path, new_path)

        assert inherited == 0
        conn = sqlite3.connect(new_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM media WHERE media_identity='new-row'").fetchone()
        finally:
            conn.close()
        assert row["match_status"] == "unmatched"
        assert row["tmdb_id"] is None
        assert row["poster_path"] is None


class TestInheritBackupPassStatusRank:
    """w4-inherit-fix item 1: the `.bak` recovery pass (`backup_pass=True`)
    applies a backup row only when its match_status ranks strictly higher
    (`_status_rank`: exact=3, candidate=2, needs_review=1, other=0) than
    the new row's CURRENT status -- never downgrades, but lets a `.bak`
    exact row win over a needs_review/candidate the enricher produced
    after the loss (see test_library_install.py's TestInstallReinstall for
    the full install-level recovery scenario)."""

    def test_backup_exact_recovers_a_needs_review_current_row(self, tmp_path):
        old_path = tmp_path / "old.bak"
        old_store = ls.LibraryStore(old_path)
        old_store.create_schema()
        old_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
            match_status="exact", tmdb_id=555, poster_path="/p.jpg", overview="overview-exact",
        ))

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        new_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
            match_status="needs_review",
        ))

        inherited = ls._inherit_from_existing(old_path, new_path, backup_pass=True)

        assert inherited == 1
        conn = sqlite3.connect(new_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM media WHERE media_identity='shared-id'").fetchone()
        conn.close()
        assert row["match_status"] == "exact"
        assert row["tmdb_id"] == 555
        assert row["poster_path"] == "/p.jpg"
        assert row["overview"] == "overview-exact"

    def test_backup_exact_does_not_replace_a_current_exact_row(self, tmp_path):
        old_path = tmp_path / "old.bak"
        old_store = ls.LibraryStore(old_path)
        old_store.create_schema()
        old_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
            match_status="exact", tmdb_id=999, poster_path="/old.jpg",
        ))

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        new_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
            match_status="exact", tmdb_id=555, poster_path="/current.jpg",
        ))

        inherited = ls._inherit_from_existing(old_path, new_path, backup_pass=True)

        assert inherited == 0
        conn = sqlite3.connect(new_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM media WHERE media_identity='shared-id'").fetchone()
        conn.close()
        assert row["tmdb_id"] == 555
        assert row["poster_path"] == "/current.jpg"

    def test_backup_needs_review_does_not_replace_a_current_candidate_row(self, tmp_path):
        old_path = tmp_path / "old.bak"
        old_store = ls.LibraryStore(old_path)
        old_store.create_schema()
        old_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
            match_status="needs_review", poster_path="/old-review.jpg",
        ))

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        new_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
            match_status="candidate", poster_path="/current-candidate.jpg",
        ))

        inherited = ls._inherit_from_existing(old_path, new_path, backup_pass=True)

        assert inherited == 0
        conn = sqlite3.connect(new_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM media WHERE media_identity='shared-id'").fetchone()
        conn.close()
        assert row["match_status"] == "candidate"
        assert row["poster_path"] == "/current-candidate.jpg"

    def test_backup_unmatched_row_is_skipped_even_against_a_current_unmatched_row(self, tmp_path):
        # w4-inherit-fix item 2: a backup row whose own match_status is
        # 'unmatched' has nothing to recover -- must never overwrite the
        # new row with stale unmatched-era metadata, even when the new
        # row is itself still unmatched (rank 0 is never > rank 0).
        old_path = tmp_path / "old.bak"
        old_store = ls.LibraryStore(old_path)
        old_store.create_schema()
        old_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
            match_status="unmatched", poster_path="/stale.jpg",
        ))

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        new_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
        ))

        inherited = ls._inherit_from_existing(old_path, new_path, backup_pass=True)

        assert inherited == 0
        conn = sqlite3.connect(new_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM media WHERE media_identity='shared-id'").fetchone()
        conn.close()
        assert row["match_status"] == "unmatched"
        assert row["poster_path"] is None

    def test_backup_pass_never_carries_tmdb_cache_or_budget(self, tmp_path):
        old_path = tmp_path / "old.bak"
        old_store = ls.LibraryStore(old_path)
        old_store.create_schema()
        old_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
            match_status="exact", tmdb_id=555,
        ))
        conn = old_store.connect()
        conn.executescript(ls._TMDB_TABLES_DDL)
        conn.execute(
            "INSERT INTO tmdb_cache (cache_key, tmdb_id, media_type, language, payload_json, status, fetched_at) "
            "VALUES ('search:movie:zh-CN:x:0', 555, 'movie', 'zh-CN', '{}', 'ok', 1000)"
        )
        conn.execute("INSERT INTO tmdb_budget (day, used, budget, updated_at) VALUES ('2026-09-01', 1, 100, 1000)")
        conn.commit()
        conn.close()

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        new_store.upsert_media(ls.MediaRecord(
            media_identity="shared-id", media_type="movie", title_zh="虚构电影一", search_key="虚构电影一",
        ))

        ls._inherit_from_existing(old_path, new_path, backup_pass=True)

        conn = sqlite3.connect(new_path)
        cache_count = conn.execute("SELECT COUNT(*) FROM tmdb_cache").fetchone()[0]
        budget_count = conn.execute("SELECT COUNT(*) FROM tmdb_budget").fetchone()[0]
        conn.close()
        assert cache_count == 0
        assert budget_count == 0


class TestInheritMediaTypeTiebreak:
    """w4-inherit-fix item 5: inside a title/year key group that is not
    1:1, `_inherit_from_existing` tries ONE deterministic tiebreak by
    media_type (old row's stored type vs new row's inferred type) before
    giving up; only pairs that become 1:1 within the group are applied."""

    def test_group_split_by_media_type_recovers_both_rows(self, tmp_path):
        old_path = tmp_path / "old.db"
        old_store = ls.LibraryStore(old_path)
        old_store.create_schema()
        old_store.upsert_media(ls.MediaRecord(
            media_identity="tmdb:movie:111", media_type="movie", title_zh="同名资源", search_key="同名资源",
            year=2020, match_status="exact", tmdb_id=111, poster_path="/movie.jpg",
        ))
        old_store.upsert_media(ls.MediaRecord(
            media_identity="tmdb:tv:222", media_type="tv", title_zh="同名资源", search_key="同名资源",
            year=2020, match_status="exact", tmdb_id=222, poster_path="/tv.jpg",
        ))

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        new_store.upsert_media(ls.MediaRecord(
            media_identity="title:new-movie:2020:movie", media_type="movie", title_zh="同名资源",
            search_key="同名资源", year=2020,
        ))
        new_store.upsert_media(ls.MediaRecord(
            media_identity="title:new-tv:2020:tv", media_type="tv", title_zh="同名资源",
            search_key="同名资源", year=2020,
        ))

        inherited = ls._inherit_from_existing(old_path, new_path)

        assert inherited == 2
        conn = sqlite3.connect(new_path)
        conn.row_factory = sqlite3.Row
        movie_row = conn.execute("SELECT * FROM media WHERE media_type='movie'").fetchone()
        tv_row = conn.execute("SELECT * FROM media WHERE media_type='tv'").fetchone()
        conn.close()
        assert movie_row["tmdb_id"] == 111
        assert movie_row["poster_path"] == "/movie.jpg"
        assert tv_row["tmdb_id"] == 222
        assert tv_row["poster_path"] == "/tv.jpg"

    def test_group_with_colliding_media_types_is_left_untouched(self, tmp_path):
        old_path = tmp_path / "old.db"
        old_store = ls.LibraryStore(old_path)
        old_store.create_schema()
        old_store.upsert_media(ls.MediaRecord(
            media_identity="tmdb:movie:333", media_type="movie", title_zh="同名资源二", search_key="同名资源二",
            year=2021, match_status="exact", tmdb_id=333, poster_path="/a.jpg",
        ))
        old_store.upsert_media(ls.MediaRecord(
            media_identity="tmdb:tv:444", media_type="tv", title_zh="同名资源二", search_key="同名资源二",
            year=2021, match_status="exact", tmdb_id=444, poster_path="/b.jpg",
        ))

        new_path = tmp_path / "new.db"
        new_store = ls.LibraryStore(new_path)
        new_store.create_schema()
        # Both new rows infer as 'unknown' -- the type bucket collides
        # (size 2), so the tiebreak must refuse to guess for EITHER row.
        new_store.upsert_media(ls.MediaRecord(
            media_identity="title:new-1:2021:unknown", media_type="unknown", title_zh="同名资源二",
            search_key="同名资源二", year=2021,
        ))
        new_store.upsert_media(ls.MediaRecord(
            media_identity="title:new-2:2021:unknown", media_type="unknown", title_zh="同名资源二",
            search_key="同名资源二", year=2021,
        ))

        inherited = ls._inherit_from_existing(old_path, new_path)

        assert inherited == 0
        conn = sqlite3.connect(new_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM media WHERE title_zh='同名资源二'").fetchall()
        conn.close()
        assert len(rows) == 2
        for row in rows:
            assert row["match_status"] == "unmatched"
            assert row["tmdb_id"] is None


class TestUpsertMedia:
    def test_same_identity_returns_same_id_and_updates_title(self, store):
        first_id = store.upsert_media(_make_media(title="虚构电影一"))
        second_id = store.upsert_media(_make_media(title="虚构电影一（改名）"))

        assert second_id == first_id

        conn = store.connect(readonly=True)
        row = conn.execute("SELECT title_zh FROM media WHERE id=?", (first_id,)).fetchone()
        conn.close()
        assert row["title_zh"] == "虚构电影一（改名）"

    def test_different_identity_returns_different_id(self, store):
        id_a = store.upsert_media(_make_media(identity="tt-fake-001"))
        id_b = store.upsert_media(_make_media(identity="tt-fake-002"))
        assert id_a != id_b


class TestUpsertGroup:
    def test_same_media_and_fingerprint_returns_same_id(self, store):
        media_id = store.upsert_media(_make_media())
        first_id = store.upsert_group(_make_group(media_id))
        second_id = store.upsert_group(_make_group(media_id))
        assert first_id == second_id


class TestUpsertLinkAndProvenance:
    def test_second_upsert_returns_created_false_without_new_row(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        link = _make_link(group_id)

        first_id, first_created = store.upsert_link(link)
        second_id, second_created = store.upsert_link(link)

        assert first_created is True
        assert second_created is False
        assert first_id == second_id

        conn = store.connect(readonly=True)
        count = conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0]
        conn.close()
        assert count == 1

    def test_second_upsert_with_different_record_overwrites_all_mutable_columns(self, store):
        media_id = store.upsert_media(_make_media())
        group_id_a = store.upsert_group(_make_group(media_id, fingerprint="fp-fake-001"))
        group_id_b = store.upsert_group(_make_group(media_id, fingerprint="fp-fake-002"))
        url = "https://115.com/s/swfake000099"

        first = ls.LinkRecord(
            public_id="pub-fake-first",
            group_id=group_id_a,
            provider="115",
            canonical_url_hash=_hash(url),
            url_plain=url,
            access_code_plain="ab12",
            url_ciphertext=b"cipher-one",
            access_code_ciphertext=b"code-one",
            url_label="115 分享 · swf…1",
            has_access_code=1,
            title_raw="标题一",
            remark="备注一",
            created_at_source=1000,
            deleted_at_source=None,
            imported_at=2000,
        )
        second = ls.LinkRecord(
            public_id="pub-fake-second",
            group_id=group_id_b,
            provider="115",
            canonical_url_hash=_hash(url),
            url_plain=url,
            access_code_plain=None,
            url_ciphertext=b"cipher-two",
            access_code_ciphertext=None,
            url_label="115 分享 · swf…1（改）",
            has_access_code=0,
            title_raw="标题二",
            remark="备注二",
            created_at_source=1000,
            deleted_at_source=9999,
            imported_at=3000,
        )

        first_id, first_created = store.upsert_link(first)
        second_id, second_created = store.upsert_link(second)

        assert first_created is True
        assert second_created is False
        assert first_id == second_id

        conn = store.connect(readonly=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM resource_link").fetchone()[0]
            row = conn.execute(
                """
                SELECT public_id, group_id, url_plain, access_code_plain, url_ciphertext,
                       access_code_ciphertext, url_label, has_access_code, title_raw, remark,
                       created_at_source, deleted_at_source, imported_at
                FROM resource_link WHERE id=?
                """,
                (first_id,),
            ).fetchone()
        finally:
            conn.close()

        assert count == 1
        # id and public_id are the conflict keys' companions and stay untouched.
        assert row["public_id"] == first.public_id
        # Every other mutable column is refreshed from the second record.
        assert row["group_id"] == group_id_b
        assert row["url_plain"] == second.url_plain
        assert row["access_code_plain"] is None
        assert row["url_ciphertext"] == b"cipher-two"
        assert row["access_code_ciphertext"] is None
        assert row["url_label"] == "115 分享 · swf…1（改）"
        assert row["has_access_code"] == 0
        assert row["title_raw"] == "标题二"
        assert row["remark"] == "备注二"
        assert row["created_at_source"] == 1000
        assert row["deleted_at_source"] == 9999
        assert row["imported_at"] == 3000

    def test_add_provenance_duplicate_primary_key_is_ignored(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        link_id, _ = store.upsert_link(_make_link(group_id))

        prov = ls.ProvenanceRecord(
            link_id=link_id, source_file="fixture.xlsx", sheet="Sheet1", row_number=2
        )
        store.add_provenance(prov)
        store.add_provenance(prov)  # duplicate PK must not raise

        conn = store.connect(readonly=True)
        count = conn.execute("SELECT COUNT(*) FROM link_provenance").fetchone()[0]
        conn.close()
        assert count == 1


class TestRecount:
    def test_recount_updates_link_count_and_has_115(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="115", url="https://115.com/s/swfake000001"))
        store.upsert_link(_make_link(group_id, provider="115", url="https://115.com/s/swfake000002"))
        store.upsert_link(_make_link(group_id, provider="quark", url="https://pan.quark.cn/s/fake000003"))

        store.recount()

        conn = store.connect(readonly=True)
        media_row = conn.execute(
            "SELECT link_count, has_115 FROM media WHERE id=?", (media_id,)
        ).fetchone()
        group_row = conn.execute(
            "SELECT link_count, has_115 FROM resource_group WHERE id=?", (group_id,)
        ).fetchone()
        conn.close()

        assert media_row["link_count"] == 3
        assert media_row["has_115"] == 1
        assert group_row["link_count"] == 3
        assert group_row["has_115"] == 1

    def test_recount_excludes_deleted_links_from_counts(self, store):
        # Follow-up (post-T17): recount() must count only LIVE links --
        # library_search's own per-item/provider-scoped counts and
        # _group_summary_payload's provider-filtered branch are already
        # live-only, so these precomputed columns must agree with them.
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="115", url="https://115.com/s/swfakerclive1"))
        store.upsert_link(_make_link(
            group_id, provider="115", url="https://115.com/s/swfakercdead1", deleted_at_source=9999,
        ))

        store.recount()

        conn = store.connect(readonly=True)
        media_row = conn.execute(
            "SELECT link_count, has_115 FROM media WHERE id=?", (media_id,)
        ).fetchone()
        group_row = conn.execute(
            "SELECT link_count, has_115 FROM resource_group WHERE id=?", (group_id,)
        ).fetchone()
        conn.close()

        assert group_row["link_count"] == 1
        assert group_row["has_115"] == 1
        assert media_row["link_count"] == 1
        assert media_row["has_115"] == 1

    def test_recount_has_115_false_when_only_115_link_is_deleted(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(
            group_id, provider="115", url="https://115.com/s/swfakercdeadonly1", deleted_at_source=9999,
        ))

        store.recount()

        conn = store.connect(readonly=True)
        media_row = conn.execute(
            "SELECT link_count, has_115 FROM media WHERE id=?", (media_id,)
        ).fetchone()
        group_row = conn.execute(
            "SELECT link_count, has_115 FROM resource_group WHERE id=?", (group_id,)
        ).fetchone()
        conn.close()

        assert group_row["link_count"] == 0
        assert group_row["has_115"] == 0
        assert media_row["link_count"] == 0
        assert media_row["has_115"] == 0


class TestEncryptionGuard:
    def test_require_encrypted_raises_by_default(self, store):
        with pytest.raises(ls.LibraryNotEncrypted):
            store.require_encrypted()

    def test_require_encrypted_passes_once_flagged(self, store):
        store.meta_set("encrypted", "1")
        store.require_encrypted()  # must not raise


class TestExtraSchemaHooks:
    def test_hooks_are_called_once_during_create_schema(self, tmp_path, monkeypatch):
        calls = []

        def fake_hook(conn):
            calls.append(conn)

        monkeypatch.setattr(ls, "EXTRA_SCHEMA_HOOKS", [fake_hook])

        library = ls.LibraryStore(tmp_path / "media-library.db")
        library.create_schema()

        assert len(calls) == 1
        assert isinstance(calls[0], sqlite3.Connection)


class TestStats:
    def test_stats_reports_counts_and_meta(self, store):
        media_id = store.upsert_media(_make_media())
        store.upsert_group(_make_group(media_id))

        stats = store.stats()

        assert stats["media_total"] == 1
        assert stats["groups_total"] == 1
        assert stats["links_total"] == 0
        assert stats["schema_version"] == "1"
        assert stats["encrypted"] is False

    def test_stats_splits_needs_review_into_three_buckets(self, store):
        # T14 fix wave 1/finding #4: a requeued row that comes back with
        # zero candidates (match_score=0.0, match_candidates_json='[]') is
        # neither "never queried" nor "has a candidate to confirm" -- it
        # must land in its own review_no_candidate bucket, not be lumped
        # into review_scored by the old needs_review-minus-pending
        # subtraction.
        store.upsert_media(ls.MediaRecord(
            media_identity="pending-1", media_type="movie", title_zh="待查询剧", search_key="待查询剧",
            match_status="needs_review",
        ))
        store.upsert_media(ls.MediaRecord(
            media_identity="scored-1", media_type="movie", title_zh="有候选剧", search_key="有候选剧",
            match_status="needs_review", match_score=0.8, match_candidates_json=json.dumps([{"tmdb_id": 1}]),
        ))
        store.upsert_media(ls.MediaRecord(
            media_identity="no-candidate-1", media_type="movie", title_zh="无候选剧", search_key="无候选剧",
            match_status="needs_review", match_score=0.0, match_candidates_json="[]",
        ))

        stats = store.stats()

        assert stats["review_pending_unqueried"] == 1
        assert stats["review_scored"] == 1
        assert stats["review_no_candidate"] == 1
        assert stats["needs_review_total"] == 3


# ---------------------------------------------------------------------------
# u1-backend T2 §1: resource-group payload spec fields (complete_season,
# source_type, video_codec, audio_summary, subtitle_summary, tags,
# sanitized review_reason).
# ---------------------------------------------------------------------------


class TestGroupPayloadSpecFields:
    def test_group_detail_includes_spec_fields(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(
            ls.GroupRecord(
                media_id=media_id,
                edition_fingerprint="fp-fake-spec",
                display_title="2160p WEB-DL",
                complete_season=1,
                source_type="webdl",
                video_codec="H.265",
                audio_summary="DTS-HD 5.1",
                subtitle_summary="中英双语",
                tags_json=json.dumps(["国语配音", "内封字幕"], ensure_ascii=False),
                needs_review=1,
                review_reason="title_alias_conflict,year_missing",
            )
        )
        group = store.group_detail(group_id)
        assert group["complete_season"] is True
        assert group["source_type"] == "webdl"
        assert group["video_codec"] == "H.265"
        assert group["audio_summary"] == "DTS-HD 5.1"
        assert group["subtitle_summary"] == "中英双语"
        assert group["tags"] == ["国语配音", "内封字幕"]
        assert group["review_reason"] == ["title_alias_conflict", "year_missing"]

    def test_review_reason_drops_unknown_tokens_and_never_returns_raw_text(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(
            ls.GroupRecord(
                media_id=media_id,
                edition_fingerprint="fp-fake-spec2",
                display_title="1080p",
                review_reason="title_alias_conflict, some raw spreadsheet text; year_missing bogus_code",
            )
        )
        group = store.group_detail(group_id)
        assert set(group["review_reason"]) == {"title_alias_conflict", "year_missing"}
        blob = json.dumps(group, ensure_ascii=False)
        assert "raw spreadsheet text" not in blob
        assert "bogus_code" not in blob

    def test_unset_spec_fields_default_to_none_and_empty(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        group = store.group_detail(group_id)
        assert group["complete_season"] is False
        assert group["source_type"] is None
        assert group["video_codec"] is None
        assert group["audio_summary"] is None
        assert group["subtitle_summary"] is None
        assert group["tags"] == []
        assert group["review_reason"] == []

    def test_review_reason_codes_allowlist_matches_importer_codes(self):
        # Mirrors the exact code set scripts/import_media_library.py writes
        # (see its `reasons_base`/`agg.conflicts` keys) -- kept in sync here
        # deliberately rather than imported, per this module's own pattern
        # of not depending on the importer.
        assert ls.REVIEW_REASON_CODES == {
            "title_alias_conflict",
            "year_missing",
            "edition_unparsed",
            "link_shared_across_groups",
            "row_shifted",
            "access_code_conflict",
            "access_code_divergence",
            "timestamp_unparsed",
        }


# ---------------------------------------------------------------------------
# T17 §13.2: media_detail groups now carry inline safe link summaries (no
# separate group_detail round trip needed) plus a top-level group_count;
# group_detail/`_group_payload` is the exact same builder, kept only for
# the compatibility `/api/library/resource/<id>` route.
# ---------------------------------------------------------------------------


class TestMediaDetailSummaryVsGroupDetailFull:
    def test_media_detail_groups_have_inline_links_and_group_count(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id))
        store.recount()

        detail = store.media_detail(media_id)
        assert detail["group_count"] == 1
        assert len(detail["groups"][0]["links"]) == 1
        assert detail["groups"][0]["links"][0]["provider"] == "115"
        for key in (
            "group_id", "display_title", "quality", "hdr", "complete_season", "source_type",
            "video_codec", "audio_summary", "subtitle_summary", "tags", "review_reason",
            "specs", "link_count", "has_115", "needs_review", "providers", "links",
        ):
            assert key in detail["groups"][0], key

    def test_group_detail_has_links(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id))
        store.recount()

        group = store.group_detail(group_id)
        assert "links" in group
        assert len(group["links"]) == 1

    def test_group_payload_includes_normalized_specs(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(
            ls.GroupRecord(
                media_id=media_id, edition_fingerprint="fp-specs-1", display_title="g",
                quality="2160p", hdr="dv_hdr", source_type="webdl",
            )
        )
        group = store.group_detail(group_id)
        assert group["specs"] == {
            "resolution": {"value": "4K", "icon": "icon-resolution"},
            "dynamic_range": {"value": "DV/HDR", "icon": "icon-dynamic-range"},
            "source": {"value": "WEB-DL", "icon": "icon-source"},
        }


# ---------------------------------------------------------------------------
# T17 §14.1: media_detail(media_id, provider=...) -- SQL-level provider
# isolation. A group with no link of the given provider is dropped
# entirely; a group with links from both the filtered provider and others
# keeps ONLY the filtered provider's links/providers/has_115; the top-level
# provider_facets/provider_count are recomputed from the filtered groups.
# ---------------------------------------------------------------------------


class TestMediaDetailProviderIsolation:
    def _build(self, store):
        media_id = store.upsert_media(_make_media())
        # Group A: both 115 and quark links (the interesting mixed case).
        group_a = store.upsert_group(_make_group(media_id, fingerprint="fp-iso-a"))
        store.upsert_link(_make_link(group_a, provider="115", url="https://115.com/s/swfakeisoa1"))
        store.upsert_link(_make_link(group_a, provider="quark", url="https://pan.quark.cn/s/swfakeisoa2"))
        # Group B: quark-only -- must vanish entirely when filtering by 115.
        group_b = store.upsert_group(_make_group(media_id, fingerprint="fp-iso-b"))
        store.upsert_link(_make_link(group_b, provider="quark", url="https://pan.quark.cn/s/swfakeisob1"))
        store.recount()
        return media_id, group_a, group_b

    def test_provider_none_returns_every_group_and_link(self, store):
        media_id, group_a, group_b = self._build(store)
        detail = store.media_detail(media_id)
        assert detail["group_count"] == 2
        all_providers = {link["provider"] for g in detail["groups"] for link in g["links"]}
        assert all_providers == {"115", "quark"}

    def test_provider_115_drops_quark_only_group_and_quark_links(self, store):
        media_id, group_a, group_b = self._build(store)
        detail = store.media_detail(media_id, provider="115")

        assert detail["group_count"] == 1
        (group,) = detail["groups"]
        assert group["group_id"] == group_a
        assert [link["provider"] for link in group["links"]] == ["115"]
        assert group["providers"] == {"115": 1}
        assert group["has_115"] is True
        assert group["link_count"] == 1
        assert detail["provider_facets"] == [{"provider": "115", "label": "115 网盘", "link_count": 1}]
        assert detail["provider_count"] == 1

        blob = json.dumps(detail, ensure_ascii=False)
        assert "quark" not in blob
        assert "夸克" not in blob

    def test_provider_quark_keeps_both_groups_but_only_quark_links(self, store):
        media_id, group_a, group_b = self._build(store)
        detail = store.media_detail(media_id, provider="quark")

        assert detail["group_count"] == 2
        for group in detail["groups"]:
            assert [link["provider"] for link in group["links"]] == ["quark"]
            assert group["has_115"] is False
            assert group["providers"] == {"quark": 1}
        assert detail["provider_facets"] == [{"provider": "quark", "label": "夸克网盘", "link_count": 2}]

        blob = json.dumps(detail, ensure_ascii=False)
        assert "115 网盘" not in blob
        assert "\"provider\": \"115\"" not in blob

    def test_provider_with_no_matching_links_returns_zero_groups(self, store):
        media_id, _group_a, _group_b = self._build(store)
        detail = store.media_detail(media_id, provider="baidu")
        assert detail["groups"] == []
        assert detail["group_count"] == 0
        assert detail["provider_facets"] == []
        assert detail["provider_count"] == 0

    def test_omitting_provider_after_filtering_restores_everything(self, store):
        media_id, _group_a, _group_b = self._build(store)
        store.media_detail(media_id, provider="115")
        full = store.media_detail(media_id)
        assert full["group_count"] == 2
        all_providers = {link["provider"] for g in full["groups"] for link in g["links"]}
        assert all_providers == {"115", "quark"}


# ---------------------------------------------------------------------------
# T17 fix wave 1 item 4: media_detail(provider=...)'s per-group
# providers/link_count/has_115 (and the top-level provider_facets summed
# from them) must count only LIVE links -- the same way library_search
# already does -- so a deleted link never inflates a badge/count even
# though it still appears, as a row, in `links` (flagged `deleted: True`).
#
# Follow-up (post-T17): a group whose live link_count comes out to 0 (every
# matching link deleted) is now dropped from `groups`/`group_count`
# entirely, whether or not `provider` filtered it down -- the same
# "excluded" rule library_search's own group_count_sql already applies --
# rather than kept with a zero count.
# ---------------------------------------------------------------------------


class TestMediaDetailProviderCountsExcludeDeletedLinks:
    def test_deleted_115_link_still_listed_but_not_counted(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="115", url="https://115.com/s/swfakedelctlive"))
        store.upsert_link(_make_link(
            group_id, provider="115", url="https://115.com/s/swfakedelctdead", deleted_at_source=9999,
        ))
        store.recount()

        detail = store.media_detail(media_id, provider="115")
        (group,) = detail["groups"]
        # both links are still listed as rows...
        assert len(group["links"]) == 2
        assert sum(1 for link in group["links"] if link["deleted"]) == 1
        # ...but only the live one is counted.
        assert group["link_count"] == 1
        assert group["providers"] == {"115": 1}
        assert group["has_115"] is True
        assert detail["provider_facets"] == [{"provider": "115", "label": "115 网盘", "link_count": 1}]
        assert detail["provider_count"] == 1

    def test_group_whose_only_matching_link_is_deleted_is_dropped_entirely(self, store):
        # Follow-up (post-T17): matches search's group_count_sql, which
        # EXCLUDES a group whose only matching-provider link is deleted --
        # a matching ROW existing isn't enough to keep the group once its
        # live link_count is 0.
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(
            group_id, provider="115", url="https://115.com/s/swfakedelonly1", deleted_at_source=9999,
        ))
        store.recount()

        detail = store.media_detail(media_id, provider="115")
        assert detail["groups"] == []
        assert detail["group_count"] == 0
        assert detail["provider_facets"] == []
        assert detail["provider_count"] == 0

    def test_unfiltered_group_whose_only_link_is_deleted_is_dropped_entirely(self, store):
        # Follow-up (post-T17): the same "excluded" rule applies with no
        # provider filter at all -- a group whose only (any-provider) link
        # is deleted must not appear, or count towards group_count, in the
        # full/unfiltered media_detail payload either.
        media_id = store.upsert_media(_make_media())
        live_group = store.upsert_group(_make_group(media_id, fingerprint="fp-du-live"))
        store.upsert_link(_make_link(live_group, provider="115", url="https://115.com/s/swfakedulive1"))
        dead_group = store.upsert_group(_make_group(media_id, fingerprint="fp-du-dead"))
        store.upsert_link(_make_link(
            dead_group, provider="quark", url="https://pan.quark.cn/s/swfakedudead1", deleted_at_source=9999,
        ))
        store.recount()

        detail = store.media_detail(media_id)
        assert detail["group_count"] == 1
        (group,) = detail["groups"]
        assert group["group_id"] == live_group


# ---------------------------------------------------------------------------
# T17 §14.5: media_detail's ratings/ratings_status passthrough from the
# T15 `ratings_json`/`ratings_status` columns.
# ---------------------------------------------------------------------------


class TestMediaDetailRatings:
    def test_ratings_passthrough_from_stored_json(self, store):
        media_id = store.upsert_media(
            ls.MediaRecord(
                media_identity="tt-fake-ratings-1", media_type="movie", title_zh="虚构评分电影",
                search_key="虚构评分电影", tmdb_id=550,
            )
        )
        conn = store.connect()
        try:
            conn.execute(
                "UPDATE media SET imdb_id=?, ratings_json=?, ratings_status='complete' WHERE id=?",
                (
                    "tt0137523",
                    json.dumps({"tmdb": {"score": 8.7, "votes": 31198}, "imdb": {"score": 9.3, "votes": 3200000}}),
                    media_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        detail = store.media_detail(media_id)
        assert detail["ratings_status"] == "complete"
        assert detail["ratings"] == {
            "tmdb": {"score": 8.7, "votes": 31198, "url": "https://www.themoviedb.org/movie/550"},
            "imdb": {"score": 9.3, "votes": 3200000, "url": "https://www.imdb.com/title/tt0137523/"},
        }

    def test_missing_ratings_defaults_to_empty_dict_and_pending(self, store):
        media_id = store.upsert_media(_make_media())
        detail = store.media_detail(media_id)
        assert detail["ratings"] == {}
        assert detail["ratings_status"] == "pending"


# ---------------------------------------------------------------------------
# x1-provider-ui §2.2/§4.2: media_detail's provider_facets/provider_count --
# summed across every resource group, ordered by the fixed tab priority
# (PROVIDER_ORDER), with labels from library_normalize.PROVIDERS.
# ---------------------------------------------------------------------------


class TestMediaDetailProviderFacets:
    def test_provider_facets_sum_counts_across_groups_and_order_by_priority(self, store):
        media_id = store.upsert_media(_make_media())
        group_a = store.upsert_group(_make_group(media_id, fingerprint="fp-a"))
        group_b = store.upsert_group(_make_group(media_id, fingerprint="fp-b"))
        # Insert out of priority order (ed2k, then 115 x2, then quark) to
        # prove the facet list is reordered, not left in insertion/count order.
        store.upsert_link(_make_link(group_a, provider="ed2k", url="https://ed2k.example/a"))
        store.upsert_link(_make_link(group_a, provider="115", url="https://115.com/s/swfakea1"))
        store.upsert_link(_make_link(group_b, provider="115", url="https://115.com/s/swfakeb1"))
        store.upsert_link(_make_link(group_b, provider="quark", url="https://pan.quark.cn/s/swfakeb2"))
        store.recount()

        detail = store.media_detail(media_id)
        assert detail["provider_count"] == 3
        assert detail["provider_facets"] == [
            {"provider": "115", "label": "115 网盘", "link_count": 2},
            {"provider": "quark", "label": "夸克网盘", "link_count": 1},
            {"provider": "ed2k", "label": "ED2K", "link_count": 1},
        ]

    def test_provider_facets_excludes_providers_not_present(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="baidu", url="https://pan.baidu.com/s/swfakec1"))
        store.recount()

        detail = store.media_detail(media_id)
        assert detail["provider_facets"] == [{"provider": "baidu", "label": "百度网盘", "link_count": 1}]
        assert detail["provider_count"] == 1

    def test_provider_facets_empty_when_no_links(self, store):
        media_id = store.upsert_media(_make_media())
        store.upsert_group(_make_group(media_id))
        store.recount()

        detail = store.media_detail(media_id)
        assert detail["provider_facets"] == []
        assert detail["provider_count"] == 0


# ---------------------------------------------------------------------------
# u1-backend T2 §6: /api/library/filters gains a "sources" facet.
# ---------------------------------------------------------------------------


class TestFiltersSourcesFacet:
    def test_sources_facet_counts_by_source_type_excluding_null(self, store):
        media_id = store.upsert_media(_make_media())
        store.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-src-1", display_title="g1", source_type="webdl")
        )
        store.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-src-2", display_title="g2", source_type="webdl")
        )
        store.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-src-3", display_title="g3", source_type="hdtv")
        )
        store.upsert_group(
            ls.GroupRecord(media_id=media_id, edition_fingerprint="fp-src-4", display_title="g4")
        )  # source_type=None must not appear

        result = store.filters()
        sources = {row["value"]: row["count"] for row in result["sources"]}
        assert sources == {"webdl": 2, "hdtv": 1}
        assert None not in sources

    def test_existing_facet_keys_are_kept(self, store):
        result = store.filters()
        for key in ("types", "years", "providers", "qualities", "hdr", "genres", "sources"):
            assert key in result, key


# ---------------------------------------------------------------------------
# T16 §2.2: the daily-recommendations candidate pool -- a pure, read-only,
# deterministically-ordered query with no ranking/randomness of its own
# (app.py owns the sha256 tie-break and day/limit slicing).
# ---------------------------------------------------------------------------


def _add_media(store, identity, *, match_status="exact", poster_path="/p.jpg", year=2020):
    return store.upsert_media(
        ls.MediaRecord(
            media_identity=identity, media_type="movie", title_zh=identity,
            search_key=identity, year=year, match_status=match_status, poster_path=poster_path,
        )
    )


def _add_live_link(store, media_id, *, deleted=False, provider="115", suffix=""):
    group_id = store.upsert_group(
        ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-{media_id}{suffix}", display_title="g")
    )
    store.upsert_link(
        ls.LinkRecord(
            public_id=f"pub-{media_id}{suffix}", group_id=group_id, provider=provider,
            canonical_url_hash=_hash(f"https://115.com/s/swfake{media_id}{suffix}"),
            url_label="115 分享 · swf…1",
            deleted_at_source=1 if deleted else None,
        )
    )
    return group_id


class TestRecommendationCandidates:
    def test_eligible_requires_exact_poster_and_live_link(self, store):
        eligible_id = _add_media(store, "tmdb:movie:1")
        _add_live_link(store, eligible_id)

        no_poster_id = _add_media(store, "tmdb:movie:2", poster_path=None)
        _add_live_link(store, no_poster_id)

        needs_review_id = _add_media(store, "fp:movie:3", match_status="needs_review")
        _add_live_link(store, needs_review_id)

        candidate_id = _add_media(store, "fp:movie:4", match_status="candidate")
        _add_live_link(store, candidate_id)

        no_live_link_id = _add_media(store, "tmdb:movie:5")
        _add_live_link(store, no_live_link_id, deleted=True)

        no_link_at_all_id = _add_media(store, "tmdb:movie:6")

        assert store.eligible_recommendation_media_ids() == [eligible_id]

    def test_eligible_is_ordered_by_stable_media_id(self, store):
        ids = []
        for i in range(5):
            media_id = _add_media(store, f"tmdb:movie:{i}")
            _add_live_link(store, media_id)
            ids.append(media_id)
        assert store.eligible_recommendation_media_ids() == sorted(ids)

    def test_fallback_prefers_poster_and_live_link_regardless_of_match_status(self, store):
        eligible_id = _add_media(store, "tmdb:movie:1", match_status="unmatched", year=2019)
        _add_live_link(store, eligible_id)

        no_poster_id = _add_media(store, "tmdb:movie:2", poster_path=None, year=2024)
        _add_live_link(store, no_poster_id)

        deleted_only_id = _add_media(store, "tmdb:movie:3", year=2023)
        _add_live_link(store, deleted_only_id, deleted=True)

        assert store.fallback_recommendation_media_ids(limit=12) == [eligible_id]

    def test_fallback_orders_by_year_desc_then_id_asc_and_respects_limit(self, store):
        older_id = _add_media(store, "tmdb:movie:1", year=2018)
        _add_live_link(store, older_id)
        newer_id = _add_media(store, "tmdb:movie:2", year=2022)
        _add_live_link(store, newer_id)
        no_year_id = _add_media(store, "tmdb:movie:3", year=None)
        _add_live_link(store, no_year_id)

        assert store.fallback_recommendation_media_ids(limit=12) == [newer_id, older_id, no_year_id]
        assert store.fallback_recommendation_media_ids(limit=1) == [newer_id]


# ---------------------------------------------------------------------------
# w6: link validity checker -- live_link_sql() parity and the new
# due_link_checks/record_link_check/queue_group_for_recheck/
# link_check_counts query helpers (docs/architecture.md).
# ---------------------------------------------------------------------------


def _mark_checked(
    store,
    provider: str,
    url: str,
    *,
    status: str,
    reason: str | None = None,
    checked_at: int = 1_700_000_000,
    next_check_at: int = 0,
    consecutive_unknown: int = 0,
    priority: int = 0,
) -> None:
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO link_check (provider, canonical_url_hash, status, reason, http_class, "
            "checked_at, next_check_at, consecutive_unknown, priority) VALUES (?,?,?,?,?,?,?,?,?)",
            (provider, _hash(url), status, reason, None, checked_at, next_check_at, consecutive_unknown, priority),
        )
        conn.commit()
    finally:
        conn.close()


class TestLiveLinkSql:
    def test_default_alias(self):
        assert ls.live_link_sql() == (
            "rl.deleted_at_source IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM link_check lc WHERE lc.provider = rl.provider "
            "AND lc.canonical_url_hash = rl.canonical_url_hash AND lc.status = 'invalid')"
        )

    def test_custom_alias_is_substituted_throughout(self):
        sql = ls.live_link_sql("rl3")
        assert "rl3.deleted_at_source IS NULL" in sql
        assert "lc.provider = rl3.provider" in sql
        assert "lc.canonical_url_hash = rl3.canonical_url_hash" in sql


class TestRecountExcludesCheckerInvalidLinks:
    def test_recount_excludes_checker_invalid_link(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        live_url = "https://115.com/s/swfakelcvalid1"
        invalid_url = "https://115.com/s/swfakelcinvalid1"
        store.upsert_link(_make_link(group_id, provider="115", url=live_url))
        store.upsert_link(_make_link(group_id, provider="115", url=invalid_url))
        _mark_checked(store, "115", invalid_url, status="invalid", reason="share_not_found")

        store.recount()

        conn = store.connect(readonly=True)
        media_row = conn.execute("SELECT link_count, has_115 FROM media WHERE id=?", (media_id,)).fetchone()
        group_row = conn.execute("SELECT link_count, has_115 FROM resource_group WHERE id=?", (group_id,)).fetchone()
        conn.close()

        assert group_row["link_count"] == 1
        assert group_row["has_115"] == 1
        assert media_row["link_count"] == 1
        assert media_row["has_115"] == 1

    def test_recount_has_115_false_when_only_115_link_is_checker_invalid(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://115.com/s/swfakelconly1"
        store.upsert_link(_make_link(group_id, provider="115", url=url))
        _mark_checked(store, "115", url, status="invalid", reason="share_cancelled")

        store.recount()

        conn = store.connect(readonly=True)
        group_row = conn.execute("SELECT link_count, has_115 FROM resource_group WHERE id=?", (group_id,)).fetchone()
        conn.close()
        assert group_row["link_count"] == 0
        assert group_row["has_115"] == 0


class TestFiltersProvidersExcludeCheckerInvalidLinks:
    def test_provider_facet_excludes_invalid_link(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        live_url = "https://pan.quark.cn/s/swfakelcqvalid1"
        invalid_url = "https://pan.quark.cn/s/swfakelcqinvalid1"
        store.upsert_link(_make_link(group_id, provider="quark", url=live_url))
        store.upsert_link(_make_link(group_id, provider="quark", url=invalid_url))
        _mark_checked(store, "quark", invalid_url, status="invalid", reason="share_expired")

        facets = store.filters()["providers"]
        quark_facet = next(f for f in facets if f["value"] == "quark")
        assert quark_facet["count"] == 1


class TestGroupSummaryLinkCheckFields:
    def test_link_payload_carries_check_status_and_invalid_flag(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        live_url = "https://115.com/s/swfakelcpvalid1"
        invalid_url = "https://115.com/s/swfakelcpinvalid1"
        unknown_url = "https://115.com/s/swfakelcpunknown1"
        store.upsert_link(_make_link(group_id, provider="115", url=live_url))
        store.upsert_link(_make_link(group_id, provider="115", url=invalid_url))
        store.upsert_link(_make_link(group_id, provider="115", url=unknown_url))
        _mark_checked(store, "115", live_url, status="valid", reason="ok", checked_at=1_700_000_500)
        _mark_checked(store, "115", invalid_url, status="invalid", reason="share_not_found", checked_at=1_700_000_600)
        _mark_checked(store, "115", unknown_url, status="unknown", reason="network_error", checked_at=1_700_000_700)
        store.recount()

        detail = store.group_detail(group_id)
        assert detail["link_count"] == 2
        assert len(detail["links"]) == 3

        by_status = {l["check_status"]: l for l in detail["links"]}
        assert by_status["valid"]["invalid"] is False
        assert by_status["valid"]["checked_at"] is not None
        assert by_status["invalid"]["invalid"] is True
        assert by_status["invalid"]["check_reason"] == "share_not_found"
        assert by_status["unknown"]["invalid"] is False
        assert by_status["unknown"]["check_reason"] == "network_error"

    def test_never_checked_link_has_null_check_fields(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="115", url="https://115.com/s/swfakelcnone1"))
        store.recount()

        detail = store.group_detail(group_id)
        (link,) = detail["links"]
        assert link["check_status"] is None
        assert link["check_reason"] is None
        assert link["checked_at"] is None
        assert link["invalid"] is False


class TestDueLinkChecks:
    def test_never_checked_link_is_due(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="115", url="https://115.com/s/swfakeduenever1"))
        due = store.due_link_checks("115", 10, now=1_700_000_000)
        assert len(due) == 1
        assert due[0]["consecutive_unknown"] == 0

    def test_future_next_check_at_is_not_due(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://115.com/s/swfakeduefuture1"
        store.upsert_link(_make_link(group_id, provider="115", url=url))
        store.record_link_check(
            "115", _hash(url), status="valid", reason="ok", http_class="200",
            checked_at=1_700_000_000, next_check_at=9_999_999_999, consecutive_unknown=0,
        )
        assert store.due_link_checks("115", 10, now=1_700_000_000) == []

    def test_past_next_check_at_is_due(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://115.com/s/swfakeduepast1"
        store.upsert_link(_make_link(group_id, provider="115", url=url))
        store.record_link_check(
            "115", _hash(url), status="unknown", reason="network_error", http_class=None,
            checked_at=1_700_000_000, next_check_at=1_700_000_001, consecutive_unknown=1,
        )
        due = store.due_link_checks("115", 10, now=1_700_100_000)
        assert len(due) == 1
        assert due[0]["consecutive_unknown"] == 1

    def test_deleted_link_is_never_due(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(
            group_id, provider="115", url="https://115.com/s/swfakeduedel1", deleted_at_source=9999,
        ))
        assert store.due_link_checks("115", 10, now=1_700_000_000) == []

    def test_other_provider_is_not_selected(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="quark", url="https://pan.quark.cn/s/swfakedueother1"))
        assert store.due_link_checks("115", 10, now=1_700_000_000) == []

    def test_priority_row_sorts_before_ordinary_due_rows(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        old_due_url = "https://115.com/s/swfakedueordold1"
        priority_url = "https://115.com/s/swfakedueordnew1"
        store.upsert_link(_make_link(group_id, provider="115", url=old_due_url))
        store.upsert_link(_make_link(group_id, provider="115", url=priority_url))
        store.record_link_check(
            "115", _hash(old_due_url), status="unknown", reason="network_error", http_class=None,
            checked_at=1, next_check_at=2, consecutive_unknown=1,
        )
        _mark_checked(store, "115", priority_url, status="unknown", checked_at=None, next_check_at=0, priority=1)

        due = store.due_link_checks("115", 10, now=1_700_000_000)
        assert [row["canonical_url_hash"] for row in due][0] == _hash(priority_url)


class TestRecordLinkCheck:
    def test_upsert_and_reset_priority(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://115.com/s/swfakercupsert1"
        store.upsert_link(_make_link(group_id, provider="115", url=url))
        store.queue_group_for_recheck(group_id, ["115"])

        store.record_link_check(
            "115", _hash(url), status="valid", reason="ok", http_class="200",
            checked_at=1_700_000_000, next_check_at=1_701_000_000, consecutive_unknown=0,
        )

        conn = store.connect(readonly=True)
        row = conn.execute(
            "SELECT status, reason, http_class, checked_at, next_check_at, consecutive_unknown, priority "
            "FROM link_check WHERE provider='115' AND canonical_url_hash=?",
            (_hash(url),),
        ).fetchone()
        conn.close()
        assert tuple(row) == ("valid", "ok", "200", 1_700_000_000, 1_701_000_000, 0, 0)


class TestQueueGroupForRecheck:
    def test_disabled_provider_is_skipped(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="115", url="https://115.com/s/swfakercqdisabled1"))
        queued, skipped, skipped_unsupported = store.queue_group_for_recheck(group_id, ["quark"])
        assert (queued, skipped, skipped_unsupported) == (0, 1, 0)

    def test_deleted_link_not_selected_or_skipped(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(
            group_id, provider="115", url="https://115.com/s/swfakercqdead1", deleted_at_source=9999,
        ))
        assert store.queue_group_for_recheck(group_id, ["115"]) == (0, 0, 0)

    def test_never_checked_link_gets_stub_priority_row(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://115.com/s/swfakercqstub1"
        store.upsert_link(_make_link(group_id, provider="115", url=url))
        queued, skipped, skipped_unsupported = store.queue_group_for_recheck(group_id, ["115"])
        assert (queued, skipped, skipped_unsupported) == (1, 0, 0)

        conn = store.connect(readonly=True)
        row = conn.execute(
            "SELECT priority, next_check_at, status FROM link_check WHERE provider='115' AND canonical_url_hash=?",
            (_hash(url),),
        ).fetchone()
        conn.close()
        assert row["priority"] == 1
        assert row["next_check_at"] == 0
        # M3: a never-checked link's stub row is a distinct "queued"
        # marker, never "unknown" -- link_check_counts must report it as
        # 未检测 (unchecked), not as an actual unknown verdict.
        assert row["status"] == "queued"

    def test_already_checked_link_priority_bumped_without_losing_status(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://115.com/s/swfakercqbump1"
        store.upsert_link(_make_link(group_id, provider="115", url=url))
        store.record_link_check(
            "115", _hash(url), status="valid", reason="ok", http_class="200",
            checked_at=1, next_check_at=999_999_999_999, consecutive_unknown=0,
        )
        queued, skipped, skipped_unsupported = store.queue_group_for_recheck(group_id, ["115"])
        assert (queued, skipped, skipped_unsupported) == (1, 0, 0)

        conn = store.connect(readonly=True)
        row = conn.execute(
            "SELECT priority, next_check_at, status FROM link_check WHERE provider='115' AND canonical_url_hash=?",
            (_hash(url),),
        ).fetchone()
        conn.close()
        assert row["priority"] == 1
        assert row["next_check_at"] == 0
        assert row["status"] == "valid"

    def test_queued_stub_status_is_reported_as_unchecked_not_unknown(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://115.com/s/swfakercqcounts1"
        store.upsert_link(_make_link(group_id, provider="115", url=url))
        store.queue_group_for_recheck(group_id, ["115"])

        counts = store.link_check_counts(["115"], now=1_700_000_000)
        assert counts["115"]["unchecked"] == 1
        assert counts["115"]["unknown"] == 0

    def test_queued_stub_is_still_counted_live_by_live_link_sql(self, store):
        # A "queued" stub must never read as invalid -- live_link_sql()
        # only excludes status='invalid'.
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://115.com/s/swfakercqlive1"
        store.upsert_link(_make_link(group_id, provider="115", url=url))
        store.queue_group_for_recheck(group_id, ["115"])
        store.recount()

        conn = store.connect(readonly=True)
        group_row = conn.execute("SELECT link_count FROM resource_group WHERE id=?", (group_id,)).fetchone()
        conn.close()
        assert group_row["link_count"] == 1


class TestQueueGroupForRecheckSkipsUnsupportedSeparately:
    """M4: skipped_disabled (a phase-1 provider that's just toggled off)
    and skipped_unsupported (a provider with no checker adapter at all)
    are reported separately when ``supported_providers`` is given."""

    def test_unsupported_provider_counts_separately_from_disabled(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="quark", url="https://pan.quark.cn/s/swfakercqdisabled2"))
        store.upsert_link(_make_link(group_id, provider="baidu", url="https://pan.baidu.com/s/swfakercqunsupported1"))

        queued, skipped_disabled, skipped_unsupported = store.queue_group_for_recheck(
            group_id, ["115"], supported_providers=["115", "quark", "alipan", "tianyicloud"],
        )
        assert queued == 0
        assert skipped_disabled == 1  # quark: supported, just not enabled
        assert skipped_unsupported == 1  # baidu: no adapter at all

    def test_without_supported_providers_everything_not_enabled_is_disabled(self, store):
        # Backward-compatible default: a caller that doesn't know the full
        # supported set can't tell "disabled" from "unsupported" apart, so
        # everything not enabled counts as skipped_disabled (never
        # skipped_unsupported), matching the pre-M4 behaviour exactly.
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(group_id, provider="baidu", url="https://pan.baidu.com/s/swfakercqunsupported2"))

        queued, skipped_disabled, skipped_unsupported = store.queue_group_for_recheck(group_id, ["115"])
        assert (queued, skipped_disabled, skipped_unsupported) == (0, 1, 0)


class TestLinkCheckCounts:
    def test_counts_by_status_and_due(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        valid_url = "https://115.com/s/swfakelccvalid1"
        invalid_url = "https://115.com/s/swfakelccinvalid1"
        unknown_url = "https://115.com/s/swfakelccunknown1"
        never_url = "https://115.com/s/swfakelccnever1"
        for url in (valid_url, invalid_url, unknown_url, never_url):
            store.upsert_link(_make_link(group_id, provider="115", url=url))
        store.record_link_check(
            "115", _hash(valid_url), status="valid", reason="ok", http_class="200",
            checked_at=1, next_check_at=999_999_999_999, consecutive_unknown=0,
        )
        store.record_link_check(
            "115", _hash(invalid_url), status="invalid", reason="share_not_found", http_class="200",
            checked_at=1, next_check_at=999_999_999_999, consecutive_unknown=0,
        )
        store.record_link_check(
            "115", _hash(unknown_url), status="unknown", reason="network_error", http_class=None,
            checked_at=1, next_check_at=2, consecutive_unknown=1,
        )

        counts = store.link_check_counts(["115"], now=1_700_000_000)
        assert counts["115"] == {"valid": 1, "invalid": 1, "unknown": 1, "unchecked": 1, "due": 2}

    def test_only_requested_providers_are_returned(self, store):
        counts = store.link_check_counts(["115", "quark"], now=1_700_000_000)
        assert set(counts.keys()) == {"115", "quark"}
        assert counts["115"] == {"valid": 0, "invalid": 0, "unknown": 0, "unchecked": 0, "due": 0}


class TestRecountGroups:
    """w6-checker-fix C2: recount_groups() -- the incremental counterpart
    to recount() that run_link_check_round calls after a check flips a
    link's live status."""

    def test_recomputes_only_the_given_group_and_its_media(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://115.com/s/swfakercgroup1"
        store.upsert_link(_make_link(group_id, provider="115", url=url))
        _mark_checked(store, "115", url, status="invalid", reason="share_not_found")

        # Untouched groups keep their stale (never-recounted) value.
        other_media_id = store.upsert_media(_make_media(identity="tt-fake-002", title="虚构电影二"))
        other_group_id = store.upsert_group(_make_group(other_media_id, fingerprint="fp-fake-002"))
        store.upsert_link(_make_link(other_group_id, provider="115", url="https://115.com/s/swfakercgroup2"))

        store.recount_groups([group_id])

        conn = store.connect(readonly=True)
        group_row = conn.execute("SELECT link_count, has_115 FROM resource_group WHERE id=?", (group_id,)).fetchone()
        media_row = conn.execute("SELECT link_count, has_115 FROM media WHERE id=?", (media_id,)).fetchone()
        other_group_row = conn.execute(
            "SELECT link_count FROM resource_group WHERE id=?", (other_group_id,)
        ).fetchone()
        conn.close()
        assert group_row["link_count"] == 0
        assert group_row["has_115"] == 0
        assert media_row["link_count"] == 0
        # Never recounted (default 0 until something recomputes it) --
        # recount_groups() must not have touched it.
        assert other_group_row["link_count"] == 0

    def test_empty_group_ids_is_a_no_op(self, store):
        store.recount_groups([])  # must not raise

    def test_falls_back_to_full_recount_above_the_threshold(self, store, monkeypatch):
        monkeypatch.setattr(ls, "RECOUNT_GROUPS_FALLBACK_THRESHOLD", 1)
        media_id = store.upsert_media(_make_media())
        group_a = store.upsert_group(_make_group(media_id, fingerprint="fp-fake-a"))
        group_b = store.upsert_group(_make_group(media_id, fingerprint="fp-fake-b"))
        store.upsert_link(_make_link(group_a, provider="115", url="https://115.com/s/swfakercgroup3"))
        store.upsert_link(_make_link(group_b, provider="115", url="https://115.com/s/swfakercgroup4"))

        called = []
        real_recount = store.recount

        def _spy_recount():
            called.append(1)
            real_recount()

        monkeypatch.setattr(store, "recount", _spy_recount)
        store.recount_groups([group_a, group_b])  # 2 > threshold(1)
        assert called == [1]

        conn = store.connect(readonly=True)
        group_row = conn.execute("SELECT link_count FROM resource_group WHERE id=?", (group_a,)).fetchone()
        conn.close()
        assert group_row["link_count"] == 1


# ---------------------------------------------------------------------------
# w6-checker-fix C1: link_check/link_check_state do not exist on an
# already-installed PRE-w6 production library -- every read path below
# (recount/filters/media_detail/search) queries link_check unconditionally
# via live_link_sql(), so it must be self-healed before the first read, not
# only once some write connection happens to be opened.
# ---------------------------------------------------------------------------


def _make_pre_w6_library(tmp_path, *, media_identity="tt-fake-pre-w6", title="旧版电影2") -> Path:
    """A library built entirely through the CURRENT code (so every other
    table/column already matches production exactly) with link_check/
    link_check_state dropped afterwards. The diff between the pre-w6
    baseline (main@f4911e2) and this module today is ADDITIVE ONLY --
    these two tables plus the query methods that read them (see ``git diff
    f4911e2 -- library_store.py``) -- so dropping them from a freshly
    built library is a faithful, much less brittle fixture for "a
    production library installed by a pre-w6 binary" than reproducing
    ``git show f4911e2:library_store.py``'s SCHEMA_SQL text verbatim."""
    db_path = tmp_path / "pre-w6-media-library.db"
    library = ls.LibraryStore(db_path)
    library.create_schema()
    library.meta_set("encrypted", "1")  # open_installed() requires this
    media_id = library.upsert_media(_make_media(media_identity, title))
    group_id = library.upsert_group(_make_group(media_id))
    library.upsert_link(_make_link(group_id, provider="115", url="https://115.com/s/swfakeprew6001"))
    library.recount()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE link_check")
        conn.execute("DROP TABLE link_check_state")
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestPreW6LibraryMigratesOnOpen:
    def test_library_genuinely_lacks_the_tables_before_open(self, tmp_path):
        db_path = _make_pre_w6_library(tmp_path)
        conn = sqlite3.connect(db_path)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "link_check" not in names
        assert "link_check_state" not in names

    def test_open_installed_migrates_the_tables_before_any_read(self, tmp_path):
        db_path = _make_pre_w6_library(tmp_path)
        ls.open_installed(db_path, None)
        conn = sqlite3.connect(db_path)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert {"link_check", "link_check_state"} <= names

    def test_filters_recount_and_detail_do_not_raise(self, tmp_path):
        db_path = _make_pre_w6_library(tmp_path)
        store = ls.open_installed(db_path, None)

        store.filters()  # must not raise "no such table: link_check"
        store.recount()

        conn = sqlite3.connect(db_path)
        media_id = conn.execute("SELECT id FROM media LIMIT 1").fetchone()[0]
        conn.close()
        detail = store.media_detail(media_id)
        assert detail is not None
        assert detail["groups"][0]["link_count"] == 1

    def test_search_does_not_raise(self, tmp_path):
        import library_search as lse

        db_path = _make_pre_w6_library(tmp_path)
        store = ls.open_installed(db_path, None)
        lse.build_index(store)
        page = lse.search(store, "", lse.Filters())
        assert len(page.items) == 1

    def test_a_readonly_connection_alone_never_migrates(self, tmp_path):
        """The write-side migration (open_installed(), or any write
        connect()) must run first -- a bare readonly connection can never
        CREATE TABLE itself, so opening one directly against an
        unmigrated pre-w6 file must still raise, proving the fix lives in
        the write path and not in some new readonly tolerance."""
        db_path = _make_pre_w6_library(tmp_path)
        store = ls.LibraryStore(db_path)
        conn = store.connect(readonly=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("SELECT * FROM link_check").fetchall()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Round 16: media_detail(include_deleted=True) keeps a group whose links are
# ALL non-live (checker-invalid and/or deleted at source) so the detail page
# reached from a "包含已失效" result can still show it, marked -- the
# default (include_deleted=False) drop is unchanged.
# ---------------------------------------------------------------------------


def _mark_invalid(store, provider, url, *, reason="share_cancelled"):
    store.record_link_check(
        provider, hashlib.sha256(url.encode("utf-8")).hexdigest(), status="invalid", reason=reason,
        http_class="200", checked_at=1_700_000_000, next_check_at=1_700_600_000, consecutive_unknown=0,
    )


class TestMediaDetailIncludeDeleted:
    def test_all_invalid_group_hidden_by_default_but_returned_with_include_deleted(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://pan.quark.cn/s/swfakeinclA1"
        store.upsert_link(_make_link(group_id, provider="quark", url=url))
        store.recount()
        _mark_invalid(store, "quark", url)
        store.recount_groups([group_id])

        assert store.media_detail(media_id)["groups"] == []
        detail = store.media_detail(media_id, include_deleted=True)
        (group,) = detail["groups"]
        assert group["group_id"] == group_id
        assert group["link_count"] == 0          # live count stays live-only
        assert detail["group_count"] == 1
        (link,) = group["links"]
        assert link["invalid"] is True and link["check_status"] == "invalid"
        assert link["check_reason"] == "share_cancelled" and link["checked_at"]
        assert detail["provider_facets"] == []   # facets stay live-only

    def test_deleted_at_source_group_returned_with_include_deleted(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        store.upsert_link(_make_link(
            group_id, provider="115", url="https://115.com/s/swfakeinclB1", deleted_at_source=9999,
        ))
        store.recount()

        assert store.media_detail(media_id)["groups"] == []
        (group,) = store.media_detail(media_id, include_deleted=True)["groups"]
        (link,) = group["links"]
        assert link["deleted"] is True and link["check_status"] is None

    def test_mixed_media_returns_live_and_all_invalid_groups(self, store):
        media_id = store.upsert_media(_make_media())
        live_group = store.upsert_group(_make_group(media_id, fingerprint="fp-incl-live"))
        store.upsert_link(_make_link(live_group, provider="115", url="https://115.com/s/swfakeinclC1"))
        dead_group = store.upsert_group(_make_group(media_id, fingerprint="fp-incl-dead"))
        dead_url = "https://cloud.189.cn/t/swfakeinclC2"
        store.upsert_link(_make_link(dead_group, provider="tianyicloud", url=dead_url))
        store.recount()
        _mark_invalid(store, "tianyicloud", dead_url, reason="file_deleted")
        store.recount_groups([dead_group])

        default = store.media_detail(media_id)
        assert [g["group_id"] for g in default["groups"]] == [live_group]
        detail = store.media_detail(media_id, include_deleted=True)
        assert sorted(g["group_id"] for g in detail["groups"]) == sorted([live_group, dead_group])
        assert detail["group_count"] == 2
        assert detail["provider_facets"] == [{"provider": "115", "label": "115 网盘", "link_count": 1}]

    def test_provider_scoped_include_deleted_keeps_all_invalid_group_of_that_provider(self, store):
        media_id = store.upsert_media(_make_media())
        group_id = store.upsert_group(_make_group(media_id))
        url = "https://pan.quark.cn/s/swfakeinclD1"
        store.upsert_link(_make_link(group_id, provider="quark", url=url))
        store.recount()
        _mark_invalid(store, "quark", url)
        store.recount_groups([group_id])

        assert store.media_detail(media_id, provider="quark")["groups"] == []
        (group,) = store.media_detail(media_id, provider="quark", include_deleted=True)["groups"]
        assert group["providers"] == {}
        assert [l["provider"] for l in group["links"]] == ["quark"]
        assert store.media_detail(media_id, provider="115", include_deleted=True)["groups"] == []
