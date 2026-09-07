"""Tests for T4.2 (bundle install / rollback / production-key CLI) and the
T4.2-onward CLI commands in app.py.

All URLs/access codes below are fixtures, not real shares: hostnames use
real domain shapes but share codes are prefixed ``swfake``/``fake`` and
access codes are fixed 4-character placeholders, per the project's
test-data rule (see .superpowers/sdd/briefs/common.md).
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_store as ls  # noqa: E402
import library_tmdb  # noqa: E402


def _hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _build_bundle(path: Path, *, media_specs, normalize_version="test-1", built_at="1700000000", schema_version=None, encrypted="0"):
    """Build a small plaintext bundle: media_specs is a list of
    (media_identity, title, provider, url, access_code) tuples, one
    resource_group + one resource_link per entry."""
    store = ls.LibraryStore(path)
    store.create_schema()
    for identity, title, provider, url, code in media_specs:
        media_id = store.upsert_media(ls.MediaRecord(media_identity=identity, media_type="movie", title_zh=title, search_key=title))
        group_id = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-{identity}", display_title="2160p"))
        store.upsert_link(
            ls.LinkRecord(
                public_id=f"pub-{identity}",
                group_id=group_id,
                provider=provider,
                canonical_url_hash=_hash(url),
                url_label=f"{provider} 分享",
                url_plain=url,
                access_code_plain=code,
                has_access_code=1 if code else 0,
                created_at_source=int(time.time()),
            )
        )
    store.recount()
    store.meta_set("normalize_version", normalize_version)
    store.meta_set("built_at", built_at)
    store.meta_set("source_hashes", "{}")
    if schema_version is not None:
        store.meta_set("schema_version", str(schema_version))
    store.meta_set("encrypted", encrypted)
    return store


@pytest.fixture
def fernet() -> Fernet:
    return Fernet(Fernet.generate_key())


# ---------------------------------------------------------------------------
# install_bundle: fresh install
# ---------------------------------------------------------------------------


class TestInstallFresh:
    def test_fresh_install_encrypts_and_reports_counts(self, tmp_path, fernet):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[
            ("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"),
            ("id-2", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])
        target = tmp_path / "media-library.db"

        result = ls.install_bundle(bundle_path, target, fernet, now=1_700_000_500)

        assert result == {
            "media_total": 2, "groups_total": 2, "links_total": 2,
            "inherited_matches": 0, "inherited_from_backup": 0,
            "built_at": "1700000000", "installed_at": "1700000500",
        }
        assert target.exists()

    def test_installed_library_has_no_plaintext_columns(self, tmp_path, fernet):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        target = tmp_path / "media-library.db"
        ls.install_bundle(bundle_path, target, fernet)

        conn = __import__("sqlite3").connect(target)
        try:
            plain = conn.execute(
                "SELECT COUNT(*) FROM resource_link WHERE url_plain IS NOT NULL OR access_code_plain IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        assert plain == 0

    def test_installed_library_ciphertext_decrypts(self, tmp_path, fernet):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        target = tmp_path / "media-library.db"
        ls.install_bundle(bundle_path, target, fernet)

        store = ls.open_installed(target, fernet)
        url, code = store.reveal("pub-id-1")
        assert url == "https://115.com/s/swfake100001"
        assert code == "ab12"

    def test_installed_library_encrypted_flag_set(self, tmp_path, fernet):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        target = tmp_path / "media-library.db"
        ls.install_bundle(bundle_path, target, fernet)
        assert ls.LibraryStore(target).is_encrypted() is True


# ---------------------------------------------------------------------------
# T15 fix wave 1 (item 4): open_installed must never need a write
# connection -- it must not run the media-table migration on every call,
# and a genuinely read-only library file/directory must still open.
# ---------------------------------------------------------------------------


class TestOpenInstalledIsReadOnly:
    def test_open_installed_does_not_require_write_access(self, tmp_path, fernet):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        target = tmp_path / "media-library.db"
        ls.install_bundle(bundle_path, target, fernet)

        target.chmod(0o444)
        target.parent.chmod(0o555)
        try:
            store = ls.open_installed(target, fernet)
            assert store.is_encrypted() is True
            url, code = store.reveal("pub-id-1")
            assert url == "https://115.com/s/swfake100001"
            assert code == "ab12"
        finally:
            target.parent.chmod(0o755)
            target.chmod(0o644)

    def test_open_installed_opens_exactly_one_write_connection_for_the_w6_migration(
        self, tmp_path, fernet, monkeypatch,
    ):
        """w6-checker-fix C1 supersedes the original guarantee this test
        used to assert here ("open_installed never opens a write
        connection"): an already-installed *pre-w6* library predates
        link_check/link_check_state (see library_store.SCHEMA_SQL's own
        comment), and every read path below uses a readonly connection
        that can never CREATE TABLE itself -- so open_installed() must run
        the write-side migration once, at open, before any read, not
        "never write at all". Still bounded to exactly one write
        connection per call (see test_open_installed_does_not_require_
        write_access above for the genuinely-read-only-storage case this
        used to protect, which still passes unchanged)."""
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        target = tmp_path / "media-library.db"
        ls.install_bundle(bundle_path, target, fernet)

        real_connect = ls.LibraryStore.connect
        write_connections = []

        def _counting_connect(self, readonly=False):
            if not readonly:
                write_connections.append(1)
            return real_connect(self, readonly=readonly)

        monkeypatch.setattr(ls.LibraryStore, "connect", _counting_connect)
        store = ls.open_installed(target, fernet)
        assert store.is_encrypted() is True
        assert len(write_connections) == 1


# ---------------------------------------------------------------------------
# install_bundle: SQLite temp files must not depend on /tmp
# ---------------------------------------------------------------------------


class TestInstallSqliteTempDir:
    """VACUUM copies the whole library through SQLite's temp directory. The
    production install runs outside the app's systemd unit, possibly in a
    sandbox whose /tmp and /var/tmp are read-only or tiny (the first real
    release failed at exactly this point), so the install must point
    SQLite's temp files at the library's own directory -- the one place
    that is guaranteed writable."""

    def test_install_points_sqlite_temp_files_at_library_dir(self, tmp_path, fernet, monkeypatch):
        monkeypatch.delenv("SQLITE_TMPDIR", raising=False)
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        target = tmp_path / "data" / "media-library.db"

        ls.install_bundle(bundle_path, target, fernet)

        probe = sqlite3.connect(":memory:")
        try:
            assert probe.execute("PRAGMA temp_store_directory").fetchone()[0] == str(target.parent)
        finally:
            probe.close()
        assert os.environ["SQLITE_TMPDIR"] == str(target.parent)


# ---------------------------------------------------------------------------
# install_bundle: validation / failure paths
# ---------------------------------------------------------------------------


class TestInstallValidation:
    def test_wrong_schema_version_rejected_and_target_untouched(self, tmp_path, fernet):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")], schema_version=2)
        target = tmp_path / "media-library.db"
        target.write_bytes(b"pre-existing content")

        with pytest.raises(ls.LibraryBundleInvalid):
            ls.install_bundle(bundle_path, target, fernet)

        assert target.read_bytes() == b"pre-existing content"
        assert not (tmp_path / "media-library.db.tmp").exists()

    def test_already_encrypted_bundle_rejected(self, tmp_path, fernet):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")], encrypted="1")
        target = tmp_path / "media-library.db"

        with pytest.raises(ls.LibraryBundleInvalid):
            ls.install_bundle(bundle_path, target, fernet)
        assert not target.exists()

    def test_missing_normalize_version_rejected(self, tmp_path, fernet):
        bundle_path = tmp_path / "bundle.sqlite"
        store = _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        # blank it out after the fact
        conn = store.connect()
        conn.execute("DELETE FROM schema_meta WHERE key='normalize_version'")
        conn.commit()
        conn.close()
        target = tmp_path / "media-library.db"

        with pytest.raises(ls.LibraryBundleInvalid):
            ls.install_bundle(bundle_path, target, fernet)

    def test_missing_bundle_file_rejected(self, tmp_path, fernet):
        target = tmp_path / "media-library.db"
        with pytest.raises(ls.LibraryBundleInvalid):
            ls.install_bundle(tmp_path / "does-not-exist.sqlite", target, fernet)

    def test_no_tmp_file_left_behind_on_failure(self, tmp_path, fernet, monkeypatch):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        target = tmp_path / "media-library.db"

        def _boom(_path):
            raise RuntimeError("simulated failure during vacuum")

        monkeypatch.setattr(ls, "_vacuum", _boom)
        with pytest.raises(RuntimeError):
            ls.install_bundle(bundle_path, target, fernet)
        assert not target.exists()
        assert not target.with_suffix(".db.tmp").exists()


# ---------------------------------------------------------------------------
# install_bundle: re-install over an existing production library
# (inheritance of TMDB matches, backup rotation)
# ---------------------------------------------------------------------------


class TestInstallReinstall:
    def test_inherits_tmdb_fields_and_cache_from_previous_install(self, tmp_path, fernet):
        target = tmp_path / "media-library.db"

        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[
            ("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"),
            ("id-2", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        # Simulate TMDB enrichment having happened against the installed
        # (v1) library: write an exact match onto id-1 plus a cache row.
        # (tmdb_cache only exists here because this test creates it
        # directly -- in production it's created via EXTRA_SCHEMA_HOOKS,
        # which this file's own LibraryStore-direct bundles deliberately
        # don't depend on; see test_library_api.py's stats() coverage for
        # the "hook not registered" case.)
        conn = ls.LibraryStore(target).connect()
        conn.executescript(ls._TMDB_TABLES_DDL)
        conn.execute(
            "UPDATE media SET tmdb_id=555, media_type='movie', title_original='Fake Movie One', "
            "overview='overview', poster_path='/p.jpg', backdrop_path='/b.jpg', "
            "genres_json='[\"Drama\"]', match_status='exact', match_score=0.97 WHERE media_identity='id-1'"
        )
        conn.execute(
            "INSERT INTO tmdb_cache (cache_key, tmdb_id, media_type, language, payload_json, status, fetched_at) "
            "VALUES ('search:movie:zh-CN:x:0', 555, 'movie', 'zh-CN', '{}', 'ok', 1000)"
        )
        conn.commit()
        conn.close()

        # Re-import the SAME media identities (fresh bundle, e.g. a refreshed
        # source export) and install over the existing production library.
        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[
            ("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"),
            ("id-2", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])

        result = ls.install_bundle(bundle_v2, target, fernet, now=2000)

        assert result["inherited_matches"] == 1
        # No .bak exists yet (v1 had no pre-existing target to back up) --
        # the recovery pass must be a clean no-op, not an error.
        assert result["inherited_from_backup"] == 0

        store = ls.open_installed(target, fernet)
        conn = store.connect(readonly=True)
        row = conn.execute("SELECT * FROM media WHERE media_identity='id-1'").fetchone()
        conn.close()
        assert row["tmdb_id"] == 555
        assert row["match_status"] == "exact"
        assert row["title_original"] == "Fake Movie One"
        assert row["overview"] == "overview"

        cache_conn = store.connect(readonly=True)
        cache_row = cache_conn.execute("SELECT * FROM tmdb_cache WHERE cache_key='search:movie:zh-CN:x:0'").fetchone()
        cache_conn.close()
        assert cache_row is not None

    def test_inherits_link_check_state_from_previous_install(self, tmp_path, fernet):
        """w6-checker-fix I4: link_check_state (per-provider daily-budget
        counters, paused_until, last_error_class, heartbeat) must survive a
        bundle re-install too -- losing it would let the checker
        immediately re-probe a provider that was just rate-limited, or
        blow well past its daily cap the moment a new bundle is installed
        the same day."""
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        conn = ls.LibraryStore(target).connect()
        conn.execute(
            "INSERT INTO link_check_state (key, value, updated_at) VALUES (?, ?, ?)",
            ("paused_until:quark", "9999999999", 1000),
        )
        conn.commit()
        conn.close()

        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v2, target, fernet, now=2000)

        store = ls.open_installed(target, fernet)
        conn = store.connect(readonly=True)
        row = conn.execute("SELECT value FROM link_check_state WHERE key='paused_until:quark'").fetchone()
        conn.close()
        assert row is not None
        assert row["value"] == "9999999999"

    def test_media_type_only_inherited_when_old_status_was_exact(self, tmp_path, fernet):
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        conn = ls.LibraryStore(target).connect()
        conn.execute("UPDATE media SET media_type='tv', match_status='candidate' WHERE media_identity='id-1'")
        conn.commit()
        conn.close()

        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v2, target, fernet, now=2000)

        conn = ls.LibraryStore(target).connect(readonly=True)
        row = conn.execute("SELECT media_type, match_status FROM media WHERE media_identity='id-1'").fetchone()
        conn.close()
        # match_status ('candidate') is unconditionally inherited, but
        # media_type is NOT (old match_status wasn't 'exact') -- the fresh
        # bundle's own freshly-inferred 'movie' must survive untouched.
        assert row["match_status"] == "candidate"
        assert row["media_type"] == "movie"

    def test_only_one_backup_is_kept(self, tmp_path, fernet):
        target = tmp_path / "media-library.db"
        for i, now in enumerate((1000, 2000, 3000)):
            bundle = tmp_path / f"bundle-{i}.sqlite"
            _build_bundle(bundle, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
            ls.install_bundle(bundle, target, fernet, now=now)

        # install #1 (now=1000) has no prior target to back up; install #2
        # (now=2000) backs up install #1's content as bak-2000; install #3
        # (now=3000) backs up install #2's content as bak-3000 and deletes
        # bak-2000 -- only the backup from the LATEST install remains.
        backups = sorted(tmp_path.glob("media-library.db.bak-*"))
        assert len(backups) == 1
        assert backups[0].name == "media-library.db.bak-3000"

    def test_exact_matches_survive_reinstall_after_merge_by_tmdb_identity_rewrite(self, tmp_path, fernet):
        """w4-inherit reproduction: `library_tmdb.merge_by_tmdb` (run by the
        background enricher after every round) rewrites an exact-matched
        row's media_identity from a title identity to "tmdb:<type>:<id>".
        A freshly re-imported bundle's row always carries a fresh title
        identity (id-1/id-2 here stand in for that), never the tmdb:...
        form -- so the OLD identity-only lookup missed every such row on
        reinstall and it lost its poster/overview/tmdb_id. Must FAIL on
        unmodified `_inherit_from_existing`."""
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[
            ("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"),
            ("id-2", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        store = ls.LibraryStore(target)
        conn = store.connect()
        conn.execute(
            "UPDATE media SET tmdb_id=555, media_type='movie', title_original='Fake Movie One', "
            "overview='overview-one', poster_path='/p1.jpg', backdrop_path='/b1.jpg', "
            "genres_json='[\"Drama\"]', match_status='exact', match_score=0.97 WHERE media_identity='id-1'"
        )
        conn.execute(
            "UPDATE media SET tmdb_id=777, media_type='movie', title_original='Fake Movie Two', "
            "overview='overview-two', poster_path='/p2.jpg', backdrop_path='/b2.jpg', "
            "genres_json='[\"Comedy\"]', match_status='exact', match_score=0.91 WHERE media_identity='id-2'"
        )
        conn.commit()
        conn.close()

        merged = library_tmdb.merge_by_tmdb(store)
        assert merged == 0  # two distinct tmdb ids -- identity upgrade only, no fold

        conn = store.connect(readonly=True)
        identities = {row["media_identity"] for row in conn.execute("SELECT media_identity FROM media").fetchall()}
        conn.close()
        assert identities == {"tmdb:movie:555", "tmdb:movie:777"}

        # Re-import (production reinstall): the fresh bundle carries the
        # same title_zh values but fresh title identities again.
        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[
            ("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"),
            ("id-2", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])
        result = ls.install_bundle(bundle_v2, target, fernet, now=2000)

        store2 = ls.open_installed(target, fernet)
        conn = store2.connect(readonly=True)
        row1 = conn.execute("SELECT * FROM media WHERE title_zh='虚构电影一'").fetchone()
        row2 = conn.execute("SELECT * FROM media WHERE title_zh='虚构电影二'").fetchone()
        conn.close()

        assert row1["tmdb_id"] == 555
        assert row1["match_status"] == "exact"
        assert row1["poster_path"] == "/p1.jpg"
        assert row1["overview"] == "overview-one"
        assert row2["tmdb_id"] == 777
        assert row2["match_status"] == "exact"
        assert row2["poster_path"] == "/p2.jpg"
        assert row2["overview"] == "overview-two"
        assert result["inherited_matches"] == 2

    def test_recovers_matches_from_backup_when_current_library_lost_them(self, tmp_path, fernet):
        """Recovery from .bak: id-a's match is lost by the current library
        at install #2 (simulating the old identity-only-lookup bug even
        under the fix -- its title also changed, so secondary lookup can't
        rescue it from the current library either) but is still sitting in
        the .bak-<ts> kept from install #1; install #3 recovers it from
        there. id-b already carries a NEWER, non-unmatched status in the
        current library by install #3 -- the (older, different) value
        sitting in the .bak must never downgrade it."""
        target = tmp_path / "media-library.db"

        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[
            ("row-a", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"),
            ("row-b", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        # Seed production match state (as if the background enricher +
        # merge_by_tmdb already ran): row-a upgraded to a tmdb identity,
        # row-b left as a needs_review title-identity match.
        conn = ls.LibraryStore(target).connect()
        conn.execute(
            "UPDATE media SET tmdb_id=555, match_status='exact', poster_path='/p1.jpg', "
            "media_identity='tmdb:movie:555' WHERE media_identity='row-a'"
        )
        conn.execute(
            "UPDATE media SET match_status='needs_review', poster_path='/old-review.jpg' "
            "WHERE media_identity='row-b'"
        )
        conn.commit()
        conn.close()

        # Install #2: row-a is re-imported under a DIFFERENT title (so
        # neither identity nor the secondary title/year lookup can place
        # it against the seeded exact row) -- it comes back unmatched, and
        # this install's rotation captures the seeded state above as the
        # single .bak-2000. row-b keeps its own identity/title, so its
        # needs_review match is inherited normally.
        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[
            ("row-a", "完全不同的新标题", "115", "https://115.com/s/swfake100001", "ab12"),
            ("row-b", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])
        ls.install_bundle(bundle_v2, target, fernet, now=2000)

        backups = sorted(tmp_path.glob("media-library.db.bak-*"))
        assert [p.name for p in backups] == ["media-library.db.bak-2000"]

        conn = ls.LibraryStore(target).connect(readonly=True)
        row_a = conn.execute("SELECT * FROM media WHERE media_identity='row-a'").fetchone()
        conn.close()
        assert row_a["match_status"] == "unmatched"  # confirmed lost, recoverable only from .bak

        # Current library moves on: row-b gets a newer, non-unmatched
        # status since install #2 -- must survive install #3 untouched.
        conn = ls.LibraryStore(target).connect()
        conn.execute(
            "UPDATE media SET match_status='candidate', poster_path='/new-candidate.jpg' "
            "WHERE media_identity='row-b'"
        )
        conn.commit()
        conn.close()

        # Install #3: row-a is re-imported back under its ORIGINAL title
        # (so it can only reconnect via the .bak's secondary title/year
        # match, not the current library's).
        bundle_v3 = tmp_path / "bundle-v3.sqlite"
        _build_bundle(bundle_v3, media_specs=[
            ("row-a", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"),
            ("row-b", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])
        result = ls.install_bundle(bundle_v3, target, fernet, now=3000)

        assert result["inherited_from_backup"] == 1
        assert result["inherited_matches"] == 2

        conn = ls.LibraryStore(target).connect(readonly=True)
        row_a = conn.execute("SELECT * FROM media WHERE media_identity='row-a'").fetchone()
        row_b = conn.execute("SELECT * FROM media WHERE media_identity='row-b'").fetchone()
        conn.close()

        assert row_a["tmdb_id"] == 555
        assert row_a["match_status"] == "exact"
        assert row_a["poster_path"] == "/p1.jpg"

        # row-b: NOT downgraded by the .bak's older needs_review/old-review.jpg.
        assert row_b["match_status"] == "candidate"
        assert row_b["poster_path"] == "/new-candidate.jpg"

    def test_corrupt_backup_does_not_fail_install(self, tmp_path, fernet):
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v2, target, fernet, now=2000)

        backup_path = target.with_name(target.name + ".bak-2000")
        assert backup_path.exists()
        backup_path.write_bytes(b"not a sqlite database at all")

        bundle_v3 = tmp_path / "bundle-v3.sqlite"
        _build_bundle(bundle_v3, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        result = ls.install_bundle(bundle_v3, target, fernet, now=3000)

        assert result["inherited_from_backup"] == 0
        assert target.exists()

    def test_backup_recovery_migrates_a_pre_t15_bak_without_leaving_sidecars(self, tmp_path, fernet):
        """w4-inherit-fix item 3: a `.bak-<ts>` kept from before the T15
        addendum shipped (missing the ten metadata/ratings/tvmaze_id/
        imdb_id columns entirely) must still work as a recovery source --
        `_inherit_from_existing` migrates the OLD library's schema before
        reading it -- and the resulting `.bak-<ts>` from THIS install must
        still end up with no stray -journal/-wal/-shm sidecar."""
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        # A pre-T15 `.bak-1500` sibling holding an exact match for id-1,
        # built against the media table exactly as it looked before the
        # T15 addendum's ten metadata/ratings columns existed.
        backup_path = target.with_name(target.name + ".bak-1500")
        conn = sqlite3.connect(backup_path)
        conn.executescript(
            """
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
        )
        now = 1_600_000_000
        conn.execute(
            "INSERT INTO media (media_identity, media_type, title_zh, search_key, tmdb_id, "
            "title_original, overview, poster_path, backdrop_path, genres_json, match_status, match_score, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "id-1", "movie", "虚构电影一", "虚构电影一", 555,
                "Fake Movie One", "overview-one", "/p1.jpg", "/b1.jpg",
                '["Drama"]', "exact", 0.97, now, now,
            ),
        )
        conn.commit()
        conn.close()

        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        result = ls.install_bundle(bundle_v2, target, fernet, now=2000)

        assert result["inherited_from_backup"] == 1

        store = ls.open_installed(target, fernet)
        conn = store.connect(readonly=True)
        row = conn.execute("SELECT * FROM media WHERE media_identity='id-1'").fetchone()
        conn.close()
        assert row["tmdb_id"] == 555
        assert row["poster_path"] == "/p1.jpg"
        assert row["overview"] == "overview-one"
        assert row["ratings_json"] == "{}"
        assert row["ratings_status"] == "pending"

        new_backup = target.with_name(target.name + ".bak-2000")
        assert new_backup.exists()
        for suffix in ("-journal", "-wal", "-shm"):
            assert not new_backup.with_name(new_backup.name + suffix).exists()


# ---------------------------------------------------------------------------
# install_bundle: WAL-safe rotation (T4 fixes #2 and #7)
# ---------------------------------------------------------------------------


def _commit_uncheckpointed_wal_write(target: Path) -> sqlite3.Connection:
    """Open ``target`` in WAL mode and commit a write WITHOUT checkpointing
    it -- simulating a background TMDB-enrichment write that lands in
    ``<target>-wal`` and would be invisible to anything that only reads
    the main .db file directly (a bare file copy/rename included).

    SQLite checkpoints (and deletes) the WAL when the LAST connection to a
    database closes, so the returned connection must be kept open (by the
    caller, e.g. in a ``try/finally``) for as long as the uncheckpointed
    write needs to still be sitting in ``-wal`` -- closing/GC'ing it early
    undoes this setup."""
    conn = sqlite3.connect(target, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("UPDATE media SET overview='background write' WHERE media_identity='id-1'")
    conn.commit()
    assert target.with_name(target.name + "-wal").exists(), "test setup: expected a live -wal sidecar"
    return conn


class TestInstallWalSafety:
    def test_backup_contains_committed_but_uncheckpointed_wal_write(self, tmp_path, fernet):
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        writer_conn = _commit_uncheckpointed_wal_write(target)
        try:
            bundle_v2 = tmp_path / "bundle-v2.sqlite"
            _build_bundle(bundle_v2, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
            ls.install_bundle(bundle_v2, target, fernet, now=2000)
        finally:
            writer_conn.close()

        backup_path = tmp_path / "media-library.db.bak-2000"
        assert backup_path.exists()
        # Copy the .bak file ALONE (no sidecar) elsewhere and open it in
        # isolation, exactly as an operator inspecting a backup would.
        copied = tmp_path / "bak-copy.db"
        shutil.copy2(backup_path, copied)
        check_conn = sqlite3.connect(copied)
        overview = check_conn.execute("SELECT overview FROM media WHERE media_identity='id-1'").fetchone()[0]
        check_conn.close()
        assert overview == "background write"

    def test_no_stale_sidecars_left_next_to_target_after_install(self, tmp_path, fernet):
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        writer_conn = _commit_uncheckpointed_wal_write(target)
        try:
            bundle_v2 = tmp_path / "bundle-v2.sqlite"
            _build_bundle(bundle_v2, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
            ls.install_bundle(bundle_v2, target, fernet, now=2000)
        finally:
            writer_conn.close()

        assert not target.with_name(target.name + "-wal").exists()
        assert not target.with_name(target.name + "-shm").exists()

    def test_remove_sidecars_runs_before_rotation_not_after(self, tmp_path, fernet, monkeypatch):
        # I6 (final-branch-review): _remove_sidecars(target) must run
        # BEFORE target is rotated to backup_path -- while `target` still
        # unambiguously names the OLD library -- never AFTER
        # os.replace(tmp_path, target), where the same filename would
        # belong to the newly-live library and a concurrent request's
        # freshly-created -wal could be deleted out from under it.
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        events = []
        real_replace = os.replace
        real_remove_sidecars = ls._remove_sidecars

        def _tracked_replace(src, dst, *a, **kw):
            events.append(("replace", str(src), str(dst)))
            return real_replace(src, dst, *a, **kw)

        def _tracked_remove_sidecars(db_path):
            events.append(("remove_sidecars", str(db_path)))
            return real_remove_sidecars(db_path)

        monkeypatch.setattr(ls.os, "replace", _tracked_replace)
        monkeypatch.setattr(ls, "_remove_sidecars", _tracked_remove_sidecars)

        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v2, target, fernet, now=2000)

        replace_indices = [i for i, e in enumerate(events) if e[0] == "replace"]
        sidecar_indices = [i for i, e in enumerate(events) if e[0] == "remove_sidecars"]
        assert replace_indices, "expected os.replace to run"
        assert sidecar_indices, "expected _remove_sidecars to run"
        assert all(si < replace_indices[0] for si in sidecar_indices), (
            "_remove_sidecars must run before the first os.replace (target -> backup_path)"
        )

    def test_stale_backup_cleanup_failure_does_not_fail_install(self, tmp_path, fernet, monkeypatch):
        # T4 fix #7: once os.replace(tmp_path, target) has succeeded, a
        # failure while deleting an OLD .bak-<ts> must not surface as a
        # failed install -- the new library is already live.
        target = tmp_path / "media-library.db"
        for i, now in enumerate((1000, 2000)):
            bundle = tmp_path / f"bundle-{i}.sqlite"
            _build_bundle(bundle, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
            ls.install_bundle(bundle, target, fernet, now=now)
        # media-library.db.bak-2000 now exists (backup of install #1).

        real_unlink = Path.unlink

        def _flaky_unlink(self, *args, **kwargs):
            if self.name == "media-library.db.bak-2000":
                raise OSError("simulated: permission denied deleting stale backup")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _flaky_unlink)

        bundle_v3 = tmp_path / "bundle-2.sqlite"
        _build_bundle(bundle_v3, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"), ("id-2", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None)])

        result = ls.install_bundle(bundle_v3, target, fernet, now=3000)

        assert result["media_total"] == 2
        assert ls.LibraryStore(target).stats()["media_total"] == 2
        assert not target.with_suffix(".db.tmp").exists()


# ---------------------------------------------------------------------------
# rollback_install
# ---------------------------------------------------------------------------


class TestRollback:
    def test_rollback_without_backup_raises_not_installed(self, tmp_path):
        with pytest.raises(ls.LibraryNotInstalled):
            ls.rollback_install(tmp_path / "media-library.db")

    def test_rollback_restores_previous_version(self, tmp_path, fernet):
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[
            ("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"),
            ("id-2", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])
        ls.install_bundle(bundle_v2, target, fernet, now=2000)
        assert ls.LibraryStore(target).stats()["media_total"] == 2

        ls.rollback_install(target)

        assert ls.LibraryStore(target).stats()["media_total"] == 1
        assert not list(tmp_path.glob("media-library.db.bak-*"))

    def test_rollback_removes_current_targets_stale_sidecars(self, tmp_path, fernet):
        # T4 fix #2: a leftover -wal/-shm at the target's path belongs to
        # the library being rolled BACK FROM; left in place, it could
        # later be misapplied against the restored (older) database.
        target = tmp_path / "media-library.db"
        bundle_v1 = tmp_path / "bundle-v1.sqlite"
        _build_bundle(bundle_v1, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        ls.install_bundle(bundle_v1, target, fernet, now=1000)

        bundle_v2 = tmp_path / "bundle-v2.sqlite"
        _build_bundle(bundle_v2, media_specs=[
            ("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12"),
            ("id-2", "虚构电影二", "quark", "https://pan.quark.cn/s/swfake200002", None),
        ])
        ls.install_bundle(bundle_v2, target, fernet, now=2000)

        writer_conn = _commit_uncheckpointed_wal_write(target)
        try:
            ls.rollback_install(target)
        finally:
            writer_conn.close()

        assert not target.with_name(target.name + "-wal").exists()
        assert not target.with_name(target.name + "-shm").exists()


# ---------------------------------------------------------------------------
# CLI: subprocess-based (no network -- only DB operations and the
# TMDB_KEY_MISSING early-exit are exercised via subprocess; enrich's
# actual TMDB traffic is tested in-process further down with FakeHTTP).
# ---------------------------------------------------------------------------


def _cli_env(data_dir: Path, key_file: Path) -> dict:
    env = dict(os.environ)
    for name in (
        "HDHIVE_APP_SECRET", "HDHIVE_CLIENT_ID", "TMDB_API_KEY", "ENV_115_COOKIES",
        "OPENLIST_TOKEN", "115_open_access_token", "115_open_refresh_token", "openlist_token", "115_cookie",
    ):
        env.pop(name, None)
    env.update({
        "HIDRIVE_AUTH_MODE": "local",
        "HIDRIVE_DATA_DIR": str(data_dir),
        "HIDRIVE_MASTER_KEY_FILE": str(key_file),
        "STRM_ROOT": str(data_dir / "strm"),
        "OPENLIST_DB": str(data_dir / "no-openlist.db"),
        "OPENLIST_URL": "http://openlist.test",
        "HIDRIVE_PUBLIC_ORIGIN": "https://hidrive.test",
        "LIBRARY_ENRICH_AUTOSTART": "0",
    })
    return env


@pytest.fixture
def cli_env(tmp_path) -> dict:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "strm").mkdir()
    key_file = tmp_path / "master.key"
    key_file.write_bytes(Fernet.generate_key())
    return _cli_env(data_dir, key_file)


def _run_cli(cli_env, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "app.py"), *args],
        env=cli_env, cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )


class TestCliInstallStatusRollback:
    def test_install_status_and_rollback_round_trip(self, tmp_path, cli_env):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])

        install = _run_cli(cli_env, "--library-install", str(bundle_path))
        assert install.returncode == 0, install.stdout + install.stderr
        install_body = json.loads(install.stdout.strip().splitlines()[-1])
        assert install_body["ok"] is True
        assert install_body["media_total"] == 1
        for forbidden in ("115.com", "swfake", "ab12"):
            assert forbidden not in install.stdout

        status = _run_cli(cli_env, "--library-status")
        assert status.returncode == 0, status.stdout + status.stderr
        status_body = json.loads(status.stdout.strip().splitlines()[-1])
        assert status_body["installed"] is True
        assert status_body["media_total"] == 1
        assert "api_key" not in status.stdout

        # a second install (same bundle) creates a backup we can roll back to
        install2 = _run_cli(cli_env, "--library-install", str(bundle_path))
        assert install2.returncode == 0

        rollback = _run_cli(cli_env, "--library-rollback")
        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        rollback_body = json.loads(rollback.stdout.strip().splitlines()[-1])
        assert rollback_body["ok"] is True

    def test_library_hints_report_cli(self, tmp_path, cli_env):
        # T15 design item 4: --library-hints-report is read-only (no HTTP)
        # and prints the same counts as store.hint_stats().
        bundle_path = tmp_path / "bundle.sqlite"
        store = _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        conn = store.connect()
        try:
            conn.execute(
                "INSERT INTO tmdb_hints (media_identity, source, decision, candidate_count, candidates_json, generated_at) "
                "VALUES ('id-1', 'imdb_offline', 'proposed_exact', 1, '[]', 0)"
            )
            conn.commit()
        finally:
            conn.close()

        install = _run_cli(cli_env, "--library-install", str(bundle_path))
        assert install.returncode == 0, install.stdout + install.stderr

        result = _run_cli(cli_env, "--library-hints-report")
        assert result.returncode == 0, result.stdout + result.stderr
        body = json.loads(result.stdout.strip().splitlines()[-1])
        assert body["ok"] is True
        assert body["hints_total"] == 1
        assert body["hints_by_decision"] == {"proposed_exact": 1}
        assert isinstance(body["hints_pending"], int)

    def test_library_hints_report_missing_library_reports_ok_false(self, cli_env):
        result = _run_cli(cli_env, "--library-hints-report")
        assert result.returncode == 1
        body = json.loads(result.stdout.strip().splitlines()[-1])
        assert body["ok"] is False
        assert body["error"] == "LibraryNotInstalled"

    def test_install_bad_bundle_exits_1_and_leaves_no_library(self, tmp_path, cli_env):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")], schema_version=2)

        result = _run_cli(cli_env, "--library-install", str(bundle_path))
        assert result.returncode == 1
        body = json.loads(result.stdout.strip().splitlines()[-1])
        assert body["ok"] is False
        assert body["error"] == "LibraryBundleInvalid"

        status = _run_cli(cli_env, "--library-status")
        assert status.returncode == 1
        status_body = json.loads(status.stdout.strip().splitlines()[-1])
        assert status_body["ok"] is False
        assert status_body["error"] == "LibraryNotInstalled"

    def test_rollback_without_backup_exits_1(self, cli_env):
        result = _run_cli(cli_env, "--library-rollback")
        assert result.returncode == 1
        body = json.loads(result.stdout.strip().splitlines()[-1])
        assert body["ok"] is False
        assert body["error"] == "LibraryNotInstalled"

    def test_enrich_without_key_exits_1(self, tmp_path, cli_env):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        _run_cli(cli_env, "--library-install", str(bundle_path))

        result = _run_cli(cli_env, "--library-enrich")
        assert result.returncode == 1
        body = json.loads(result.stdout.strip().splitlines()[-1])
        assert body == {"ok": False, "error": "TMDB_KEY_MISSING"}

    def test_requeue_review_without_key_exits_1(self, tmp_path, cli_env):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        _run_cli(cli_env, "--library-install", str(bundle_path))

        result = _run_cli(cli_env, "--library-requeue-review")
        assert result.returncode == 1
        body = json.loads(result.stdout.strip().splitlines()[-1])
        assert body == {"ok": False, "error": "TMDB_KEY_MISSING"}

    def test_requeue_review_dry_run_needs_no_key(self, tmp_path, cli_env):
        # T14/§5.1: --dry-run makes no HTTP calls, so it must work even
        # without a configured TMDB key.
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        _run_cli(cli_env, "--library-install", str(bundle_path))

        result = _run_cli(cli_env, "--library-requeue-review", "--dry-run")
        assert result.returncode == 0
        body = json.loads(result.stdout.strip().splitlines()[-1])
        assert body["ok"] is True
        assert body["dry_run"] is True
        assert "review_pending_unqueried" in body
        assert "audited_today" in body


class TestCliInstallDiagnostics:
    """The bridge only relays ``--library-install``'s exit status and JSON
    line, so that line must say *where* an install failed and which
    Python/SQLite ran it -- never anything beyond counts, stage names and
    version strings."""

    def test_success_line_carries_versions(self, hidrive, tmp_path, capsys):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])

        exit_code = hidrive.run_library_install(str(bundle_path))

        assert exit_code == 0
        body = json.loads(capsys.readouterr().out.strip())
        assert body["ok"] is True
        assert body["media_total"] == 1
        assert body["python"] == platform.python_version()
        assert body["sqlite"] == sqlite3.sqlite_version

    def test_failure_line_names_the_stage(self, hidrive, tmp_path, monkeypatch, capsys):
        bundle_path = tmp_path / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])

        def _boom(path):
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(hidrive.library_store, "_vacuum", _boom)

        exit_code = hidrive.run_library_install(str(bundle_path))

        assert exit_code == 1
        body = json.loads(capsys.readouterr().out.strip())
        assert body == {
            "ok": False,
            "error": "OperationalError",
            "stage": "vacuum",
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        }


class TestCliEnrichInProcess:
    """Real TMDB HTTP interaction (enrich_batch/TmdbClient) is T3's own
    responsibility and is already thoroughly covered by
    test_library_tmdb*.py; this proves run_library_enrich's own CLI wiring
    -- it opens the installed library, builds a TmdbClient from the
    configured key, calls enrich_batch under the shared lock with the
    right limit, and prints exactly its stats as one JSON line."""

    def test_enrich_prints_stats_and_returns_0(self, hidrive, installed_library, monkeypatch, capsys):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        canned = hidrive.library_tmdb.EnrichStats(candidates_considered=2, requests_made=2, matched_exact=1)
        received = {}

        def _fake_enrich_batch(store, client, *, limit=20, with_details=False, tvmaze_enabled=False):
            received["store"] = store
            received["client"] = client
            received["limit"] = limit
            return canned

        monkeypatch.setattr(hidrive.library_tmdb, "enrich_batch", _fake_enrich_batch)

        exit_code = hidrive.run_library_enrich(None)

        assert exit_code == 0
        assert received["limit"] == 20
        assert isinstance(received["client"], hidrive.library_tmdb.TmdbClient)
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed == dataclasses.asdict(canned)

    def test_enrich_respects_max_media(self, hidrive, installed_library, monkeypatch, capsys):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        received = {}

        def _fake_enrich_batch(store, client, *, limit=20, with_details=False, tvmaze_enabled=False):
            received["limit"] = limit
            return hidrive.library_tmdb.EnrichStats()

        monkeypatch.setattr(hidrive.library_tmdb, "enrich_batch", _fake_enrich_batch)
        hidrive.run_library_enrich(5)
        assert received["limit"] == 5

    def test_enrich_batch_failure_reports_ok_false_like_siblings(self, hidrive, installed_library, monkeypatch, capsys):
        # Minor fix: run_library_enrich only wrapped open_installed() in
        # try/except -- an exception from enrich_batch itself propagated
        # unhandled instead of being reported the same
        # {"ok": false, "error": "<ExceptionClass>"} way as
        # run_library_install/run_library_status/run_library_rollback.
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")

        def _boom(store, client, *, limit=20, with_details=False, tvmaze_enabled=False):
            raise RuntimeError("simulated enrich_batch failure")

        monkeypatch.setattr(hidrive.library_tmdb, "enrich_batch", _boom)

        exit_code = hidrive.run_library_enrich(None)

        assert exit_code == 1
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed == {"ok": False, "error": "RuntimeError"}

    def test_enrich_exits_1_immediately_when_run_lock_is_held(self, hidrive, installed_library, capsys):
        # A4: --library-enrich takes the run lock non-blocking -- it must
        # never wait for a concurrent background/manual enrich to finish.
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        lock_paths = hidrive._tmdb_lock_paths()
        lock_paths.run.parent.mkdir(parents=True, exist_ok=True)
        holder = open(lock_paths.run, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            started = time.monotonic()
            exit_code = hidrive.run_library_enrich(None)
            elapsed = time.monotonic() - started
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()

        assert exit_code == 1
        assert elapsed < 1.0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed == {"ok": False, "error": "TMDB_ENRICH_BUSY"}


class TestCliRequeueReviewInProcess:
    """T14/§5.1 -- ``run_library_requeue_review``'s own CLI wiring: it opens
    the installed library, loops ``library_tmdb.requeue_review_batch`` in
    <=100-sized batches under the shared run lock, honours ``--max-media``/
    ``--resume``/``--dry-run``, and prints one aggregated JSON line. The
    review-pending selection/judging rules themselves are covered by
    test_library_tmdb_enrich.py."""

    def test_prints_aggregated_stats_and_returns_0(self, hidrive, installed_library, monkeypatch, capsys):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        canned = hidrive.library_tmdb.EnrichStats(candidates_considered=2, requests_made=2, matched_needs_review=2, queue_exhausted=True)
        received = {}

        def _fake_batch(store, client, *, limit=100, exclude_ids=frozenset(), with_details=False, deadline=None, today=None, tvmaze_enabled=False):
            received["limit"] = limit
            received["exclude_ids"] = exclude_ids
            return canned

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _fake_batch)

        exit_code = hidrive.run_library_requeue_review(max_media=None, resume=False, dry_run=False)

        assert exit_code == 0
        assert received["limit"] == 100
        assert received["exclude_ids"] == frozenset()
        printed = json.loads(capsys.readouterr().out.strip())
        # Fix wave 1/finding #5: elapsed_seconds is now set on the
        # aggregated total (wall time across every internal batch), so it
        # no longer matches the canned per-batch stats' default 0.0.
        elapsed = printed.pop("elapsed_seconds")
        expected = dataclasses.asdict(canned)
        expected.pop("elapsed_seconds")
        assert printed == expected
        assert elapsed >= 0

    def test_aggregated_stats_include_elapsed_seconds_across_internal_batches(
        self, hidrive, installed_library, monkeypatch, capsys
    ):
        # Fix wave 1/finding #5: the aggregated CLI JSON's elapsed_seconds
        # must reflect the whole run, not stay stuck at 0.0 (previously
        # skipped from accumulation and never set on the total afterwards).
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")

        def _fake_batch(store, client, *, limit=100, exclude_ids=frozenset(), with_details=False, deadline=None, today=None, tvmaze_enabled=False):
            time.sleep(0.01)
            return hidrive.library_tmdb.EnrichStats(candidates_considered=1, queue_exhausted=True)

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _fake_batch)

        exit_code = hidrive.run_library_requeue_review(max_media=None, resume=False, dry_run=False)

        assert exit_code == 0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed["elapsed_seconds"] >= 0.01

    def test_max_media_splits_into_capped_internal_batches(self, hidrive, installed_library, monkeypatch, capsys):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        limits_seen = []

        def _fake_batch(store, client, *, limit=100, exclude_ids=frozenset(), with_details=False, deadline=None, today=None, tvmaze_enabled=False):
            limits_seen.append(limit)
            return hidrive.library_tmdb.EnrichStats(candidates_considered=limit, queue_exhausted=False)

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _fake_batch)

        exit_code = hidrive.run_library_requeue_review(max_media=250, resume=False, dry_run=False)

        assert exit_code == 0
        assert limits_seen == [100, 100, 50]
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed["candidates_considered"] == 250

    def test_stops_early_when_queue_is_exhausted(self, hidrive, installed_library, monkeypatch, capsys):
        # Fewer rows remain than max_media -- the loop must not keep
        # calling the batch function once queue_exhausted comes back True.
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        calls = {"n": 0}

        def _fake_batch(store, client, *, limit=100, exclude_ids=frozenset(), with_details=False, deadline=None, today=None, tvmaze_enabled=False):
            calls["n"] += 1
            return hidrive.library_tmdb.EnrichStats(candidates_considered=3, queue_exhausted=True)

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _fake_batch)

        hidrive.run_library_requeue_review(max_media=1000, resume=False, dry_run=False)

        assert calls["n"] == 1

    def test_resume_passes_todays_audited_media_ids_as_exclude_ids(self, hidrive, installed_library, monkeypatch, capsys):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        today = hidrive.datetime.now(hidrive.timezone.utc).date().isoformat()
        conn = installed_library.connect()
        try:
            hidrive.library_tmdb.ensure_tables(conn)
            # Fix wave 1/finding #3: --resume only skips a row that was
            # actually judged (processed_at set), not merely queued.
            conn.execute(
                "INSERT INTO tmdb_requeue_audit "
                "(media_id, prev_status, prev_review_reason, batch_id, queued_at, processed_at) "
                "VALUES (?, 'needs_review', 'review_pending_unqueried', ?, ?, ?)",
                (7, today, 1000, 1001),
            )
            conn.commit()
        finally:
            conn.close()
        received = {}

        def _fake_batch(store, client, *, limit=100, exclude_ids=frozenset(), with_details=False, deadline=None, today=None, tvmaze_enabled=False):
            received["exclude_ids"] = exclude_ids
            return hidrive.library_tmdb.EnrichStats(queue_exhausted=True)

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _fake_batch)

        hidrive.run_library_requeue_review(max_media=None, resume=True, dry_run=False)

        assert received["exclude_ids"] == frozenset({7})

    def test_resume_does_not_exclude_a_media_id_only_queued_but_never_judged(
        self, hidrive, installed_library, monkeypatch, capsys
    ):
        # Fix wave 1/finding #3: an aborted batch leaves a queued-but-
        # unprocessed audit row (processed_at NULL) -- --resume must still
        # pick that media id back up, not treat it as done.
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        today = hidrive.datetime.now(hidrive.timezone.utc).date().isoformat()
        conn = installed_library.connect()
        try:
            hidrive.library_tmdb.ensure_tables(conn)
            conn.execute(
                "INSERT INTO tmdb_requeue_audit (media_id, prev_status, prev_review_reason, batch_id, queued_at) "
                "VALUES (?, 'needs_review', 'review_pending_unqueried', ?, ?)",
                (7, today, 1000),
            )
            conn.commit()
        finally:
            conn.close()
        received = {}

        def _fake_batch(store, client, *, limit=100, exclude_ids=frozenset(), with_details=False, deadline=None, today=None, tvmaze_enabled=False):
            received["exclude_ids"] = exclude_ids
            return hidrive.library_tmdb.EnrichStats(queue_exhausted=True)

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _fake_batch)

        hidrive.run_library_requeue_review(max_media=None, resume=True, dry_run=False)

        assert received["exclude_ids"] == frozenset()

    def test_without_resume_exclude_ids_is_empty_even_with_audit_rows(self, hidrive, installed_library, monkeypatch, capsys):
        today = hidrive.datetime.now(hidrive.timezone.utc).date().isoformat()
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        conn = installed_library.connect()
        try:
            hidrive.library_tmdb.ensure_tables(conn)
            conn.execute(
                "INSERT INTO tmdb_requeue_audit (media_id, prev_status, prev_review_reason, batch_id, queued_at) "
                "VALUES (?, 'needs_review', 'review_pending_unqueried', ?, ?)",
                (7, today, 1000),
            )
            conn.commit()
        finally:
            conn.close()
        received = {}

        def _fake_batch(store, client, *, limit=100, exclude_ids=frozenset(), with_details=False, deadline=None, today=None, tvmaze_enabled=False):
            received["exclude_ids"] = exclude_ids
            return hidrive.library_tmdb.EnrichStats(queue_exhausted=True)

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _fake_batch)

        hidrive.run_library_requeue_review(max_media=None, resume=False, dry_run=False)

        assert received["exclude_ids"] == frozenset()

    def test_search_failed_media_ids_are_folded_into_exclude_ids_for_the_next_internal_batch(
        self, hidrive, installed_library, monkeypatch, capsys
    ):
        # T14 fix wave 2/finding #1: a media id one internal batch reports
        # as search-failed (EnrichStats.search_failed_media_ids) must be
        # excluded from this same invocation's later internal batches too,
        # or the <=100-row loop would keep re-selecting the same failing
        # row until max_media/queue_exhausted stopped it.
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        received_exclude_ids = []

        def _fake_batch(store, client, *, limit=100, exclude_ids=frozenset(), with_details=False, deadline=None, today=None, tvmaze_enabled=False):
            received_exclude_ids.append(exclude_ids)
            if len(received_exclude_ids) == 1:
                return hidrive.library_tmdb.EnrichStats(
                    candidates_considered=1, search_failed_media_ids=[7], queue_exhausted=False,
                )
            return hidrive.library_tmdb.EnrichStats(candidates_considered=0, queue_exhausted=True)

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _fake_batch)

        exit_code = hidrive.run_library_requeue_review(max_media=None, resume=False, dry_run=False)

        assert exit_code == 0
        assert received_exclude_ids == [frozenset(), frozenset({7})]

    def test_dry_run_never_calls_the_batch_function_or_requires_a_key(self, hidrive, installed_library, monkeypatch, capsys):
        def _boom(*args, **kwargs):
            raise AssertionError("requeue_review_batch must not be called in --dry-run")

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _boom)

        exit_code = hidrive.run_library_requeue_review(max_media=None, resume=False, dry_run=True)

        assert exit_code == 0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed["ok"] is True
        assert printed["dry_run"] is True

    def test_batch_failure_reports_ok_false_like_siblings(self, hidrive, installed_library, monkeypatch, capsys):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")

        def _boom(store, client, *, limit=100, exclude_ids=frozenset(), with_details=False, deadline=None, today=None, tvmaze_enabled=False):
            raise RuntimeError("simulated requeue_review_batch failure")

        monkeypatch.setattr(hidrive.library_tmdb, "requeue_review_batch", _boom)

        exit_code = hidrive.run_library_requeue_review(max_media=None, resume=False, dry_run=False)

        assert exit_code == 1
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed == {"ok": False, "error": "RuntimeError"}

    def test_exits_1_immediately_when_run_lock_is_held(self, hidrive, installed_library, capsys):
        hidrive.secret_set("tmdb_api_key", "fake-test-key-not-real")
        lock_paths = hidrive._tmdb_lock_paths()
        lock_paths.run.parent.mkdir(parents=True, exist_ok=True)
        holder = open(lock_paths.run, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            started = time.monotonic()
            exit_code = hidrive.run_library_requeue_review(max_media=None, resume=False, dry_run=False)
            elapsed = time.monotonic() - started
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()

        assert exit_code == 1
        assert elapsed < 1.0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed == {"ok": False, "error": "TMDB_ENRICH_BUSY"}


class TestEnricherNeverCreatesAStubLibrary:
    """Review fix #1 (CRITICAL): BackgroundEnricher used to persist its
    heartbeat/state through ``conn_factory`` -- production:
    ``sqlite3.connect(LIBRARY_DB_PATH)`` -- before ever checking whether a
    library is installed. On a host with no index, that call alone creates
    ``LIBRARY_DB_PATH`` and ``ensure_tables`` fills it with only the tmdb
    tables, so ``open_installed`` sees a file that exists but has none of
    the real schema and raises ``LibraryIndexUnreadable`` instead of
    ``LibraryNotInstalled`` -- and a later ``install_bundle`` then fails at
    the "inherit" stage against that stub. The fix: the production
    ``conn_factory`` must never create the file, and persistence must skip
    silently when it doesn't exist yet."""

    def test_heartbeat_with_no_library_leaves_no_stub_and_install_still_succeeds(self, hidrive, workspace):
        assert not hidrive.LIBRARY_DB_PATH.exists()

        enricher = hidrive._build_library_enricher()
        enricher._idle_seconds = 0.02
        enricher._round_seconds = 0.02
        enricher.start()
        try:
            time.sleep(0.15)  # several idle iterations, each persisting a heartbeat
        finally:
            enricher.stop(timeout=2)

        assert not hidrive.LIBRARY_DB_PATH.exists()
        with pytest.raises(ls.LibraryNotInstalled):
            ls.open_installed(hidrive.LIBRARY_DB_PATH, None)

        bundle_path = workspace / "bundle.sqlite"
        _build_bundle(bundle_path, media_specs=[("id-1", "虚构电影一", "115", "https://115.com/s/swfake100001", "ab12")])
        result = ls.install_bundle(bundle_path, hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())
        assert result["media_total"] == 1
