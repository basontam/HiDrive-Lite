"""The migration command, exercised through the CLI it actually is.

Review 2026-09-12 R10/R11/R16/R17. These run `python app.py --auth-migrate`
as a subprocess against a throwaway database, because the defects were in
what happens *before* the function runs -- the import-time `init_db()` -- and
no in-process test with an already-initialised fixture could see them.

Every value here is invented and never leaves the test's own tmp_path.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service  # noqa: E402

LEGACY_SCHEMA = """
CREATE TABLE secrets (name TEXT PRIMARY KEY, value BLOB NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
    status TEXT NOT NULL, detail TEXT, actor TEXT, created_at INTEGER NOT NULL);
CREATE TABLE reauth_challenges (id_hash TEXT PRIMARY KEY, actor TEXT NOT NULL, state TEXT NOT NULL,
    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, consumed_at INTEGER, error_code TEXT,
    qr_uid_cipher BLOB, qr_time INTEGER, qr_sign_cipher BLOB, claimed_at INTEGER, claimed_from TEXT);
CREATE TABLE cloud_download_task (info_hash TEXT PRIMARY KEY, link_public_id TEXT NOT NULL,
    media_id INTEGER, group_id INTEGER, media_title TEXT, link_label TEXT, wp_path_id TEXT NOT NULL,
    target_path TEXT, submitted_at INTEGER NOT NULL, submitted_by TEXT, last_status INTEGER,
    last_message TEXT, last_seen_at INTEGER);
"""


@pytest.fixture
def deployment(tmp_path):
    """A database as a Phase-1-5 deployment would have left it."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    key_file = tmp_path / "master.key"
    key_file.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))

    from cryptography.fernet import Fernet

    fernet = Fernet(key_file.read_bytes().strip())
    db_path = data_dir / "hidrive.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO secrets(name, value, updated_at) VALUES(?,?,?)",
                 ("115_cookie", fernet.encrypt(b"fake-legacy-cookie"), 1))
    conn.execute("INSERT INTO settings(name, value, updated_at) VALUES(?,?,?)",
                 ("115_target_pid", "legacy-folder-cid", 1))
    conn.execute("INSERT INTO settings(name, value, updated_at) VALUES(?,?,?)",
                 ("115_target_path", "/115pan/影视", 1))
    conn.execute(
        "INSERT INTO cloud_download_task(info_hash, link_public_id, media_id, group_id, media_title, "
        "link_label, wp_path_id, submitted_at) VALUES(?,?,?,?,?,?,?,?)",
        ("hash-legacy-1", "pub-1", 42, 7, "虚构片", "ED2K · S01E01", "cid-1", 100))
    conn.commit()
    conn.close()

    env = dict(os.environ,
               HIDRIVE_AUTH_MODE="local",
               HIDRIVE_DATA_DIR=str(data_dir),
               HIDRIVE_MASTER_KEY_FILE=str(key_file),
               HIDRIVE_LIBRARY_DB=str(data_dir / "media-library.db"))
    return {"env": env, "db": db_path, "dir": data_dir, "fernet": fernet}


def _run(deployment, *args) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, "app.py", *args], cwd=str(ROOT),
                            env=deployment["env"], capture_output=True, text=True, timeout=120)
    payload = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
    return result.returncode, payload


def _fingerprint(path: Path) -> tuple:
    """Everything a write would disturb: content, schema, and the sidecars."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        schema = sorted(row[0] or "" for row in conn.execute("SELECT sql FROM sqlite_master"))
        counts = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            counts[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    finally:
        conn.close()
    sidecars = sorted(p.name for p in path.parent.iterdir() if p.name.startswith(path.name + "-"))
    return (path.read_bytes(), tuple(schema), tuple(sorted(counts.items())), tuple(sidecars))


# ---------------------------------------------------------------------------
# R10: a dry run writes nothing at all
# ---------------------------------------------------------------------------


class TestDryRunIsReadOnly:
    def test_it_leaves_the_database_byte_for_byte_identical(self, deployment):
        before = _fingerprint(deployment["db"])
        code, report = _run(deployment, "--auth-migrate", "--dry-run")
        assert code == 0 and report["dry_run"] is True
        assert _fingerprint(deployment["db"]) == before

    def test_it_creates_no_table_and_no_column(self, deployment):
        _run(deployment, "--auth-migrate", "--dry-run")
        conn = sqlite3.connect(deployment["db"])
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)")}
        finally:
            conn.close()
        assert not (set(auth_service.NEW_TABLES) & tables), "a dry run that creates the tables is not a dry run"
        assert "user_id" not in columns

    def test_it_reports_what_an_apply_would_add(self, deployment):
        _code, report = _run(deployment, "--auth-migrate", "--dry-run")
        plan = report["items"]["schema"]
        assert set(plan["tables_to_create"]) == set(auth_service.NEW_TABLES)
        assert "audit_log.user_id" in plan["columns_to_add"]
        assert report["items"]["115_web_cookie"] == {
            "present": True, "already_migrated": False, "would_copy": True}
        assert report["items"]["cloud_download_task"]["would_copy"] == 1

    def test_it_leaves_no_write_ahead_log_behind(self, deployment):
        _run(deployment, "--auth-migrate", "--dry-run")
        leftovers = [p.name for p in deployment["dir"].iterdir() if p.name.endswith(("-wal", "-shm"))]
        assert leftovers == []

    def test_a_dry_run_against_a_fully_migrated_database_reports_nothing_to_do(self, deployment):
        _run(deployment, "--auth-migrate", "--apply")
        _code, report = _run(deployment, "--auth-migrate", "--dry-run")
        assert report["items"]["schema"]["tables_to_create"] == []
        assert report["items"]["115_web_cookie"]["would_copy"] is False
        assert report["items"]["cloud_download_task"]["would_copy"] == 0

    def test_neither_flag_is_an_error_rather_than_a_guess(self, deployment):
        code, report = _run(deployment, "--auth-migrate")
        assert code == 2 and report["error"] == "NeedDryRunOrApply"


# ---------------------------------------------------------------------------
# R11 / R17: applying, and applying again
# ---------------------------------------------------------------------------


class TestApply:
    def test_the_first_apply_copies_everything_it_found(self, deployment):
        code, report = _run(deployment, "--auth-migrate", "--apply")
        assert code == 0
        assert report["items"]["115_web_cookie"]["copied"] == 1
        assert report["items"]["115_web_cookie"]["verified"] is True
        assert report["items"]["cloud_download_task"]["copied"] == 1

    def test_the_administrators_default_folder_survives(self, deployment):
        """R17: the stored default is `115_target_pid`; the migration used to
        look for a key that does not exist and quietly lost it."""
        _run(deployment, "--auth-migrate", "--apply")
        conn = sqlite3.connect(deployment["db"])
        try:
            row = conn.execute("SELECT default_target_cid, default_target_label "
                               "FROM user_115_profile").fetchone()
        finally:
            conn.close()
        assert row[0] == "legacy-folder-cid"
        assert row[1] == "/115pan/影视"

    def test_a_historic_tasks_origin_survives(self, deployment):
        """R17: the cloud-download list shows which release and which link a
        task came from."""
        _run(deployment, "--auth-migrate", "--apply")
        conn = sqlite3.connect(deployment["db"])
        try:
            row = conn.execute("SELECT media_id, display_title, group_id, link_label "
                               "FROM user_cloud_download_task").fetchone()
        finally:
            conn.close()
        assert row == (42, "虚构片", 7, "ED2K · S01E01")

    def test_the_legacy_rows_are_copied_not_moved(self, deployment):
        _run(deployment, "--auth-migrate", "--apply")
        conn = sqlite3.connect(deployment["db"])
        try:
            assert conn.execute("SELECT COUNT(*) FROM secrets WHERE name='115_cookie'").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM cloud_download_task").fetchone()[0] == 1
            assert conn.execute("SELECT value FROM settings WHERE name='115_target_pid'").fetchone()[0] \
                == "legacy-folder-cid"
        finally:
            conn.close()

    def test_re_running_never_overwrites_a_newer_credential(self, deployment):
        """R11: by the second run the administrator may have re-scanned. The
        old global value must not be put back over it."""
        _run(deployment, "--auth-migrate", "--apply")
        conn = sqlite3.connect(deployment["db"])
        try:
            conn.execute("UPDATE user_secret SET ciphertext=? WHERE name='115_web_cookie'",
                         (deployment["fernet"].encrypt(b"fake-rescanned-cookie"),))
            conn.commit()
        finally:
            conn.close()
        _code, report = _run(deployment, "--auth-migrate", "--apply")
        assert report["items"]["115_web_cookie"]["copied"] == 0
        assert report["items"]["115_web_cookie"]["skipped"] == 1
        conn = sqlite3.connect(deployment["db"])
        try:
            stored = conn.execute("SELECT ciphertext FROM user_secret WHERE name='115_web_cookie'").fetchone()[0]
        finally:
            conn.close()
        assert deployment["fernet"].decrypt(bytes(stored)) == b"fake-rescanned-cookie"

    def test_re_running_never_restores_the_old_default_folder(self, deployment):
        _run(deployment, "--auth-migrate", "--apply")
        conn = sqlite3.connect(deployment["db"])
        try:
            conn.execute("UPDATE user_115_profile SET default_target_cid='chosen-since'")
            conn.commit()
        finally:
            conn.close()
        _code, report = _run(deployment, "--auth-migrate", "--apply")
        assert report["items"]["user_115_profile"]["skipped"] == 1
        conn = sqlite3.connect(deployment["db"])
        try:
            assert conn.execute("SELECT default_target_cid FROM user_115_profile").fetchone()[0] == "chosen-since"
        finally:
            conn.close()

    def test_the_report_counts_this_run_not_the_table(self, deployment):
        _run(deployment, "--auth-migrate", "--apply")
        _code, report = _run(deployment, "--auth-migrate", "--apply")
        assert report["items"]["cloud_download_task"]["copied"] == 0
        assert report["items"]["cloud_download_task"]["skipped"] == 1

    def test_no_report_ever_prints_a_value_or_its_hash(self, deployment):
        import hashlib

        result = subprocess.run([sys.executable, "app.py", "--auth-migrate", "--apply"], cwd=str(ROOT),
                                env=deployment["env"], capture_output=True, text=True, timeout=120)
        assert "fake-legacy-cookie" not in result.stdout
        assert hashlib.sha256(b"fake-legacy-cookie").hexdigest() not in result.stdout


# ---------------------------------------------------------------------------
# R16: a database that already ran Phase 1-5 gains the newer columns
# ---------------------------------------------------------------------------


class TestSchemaUpgrade:
    def test_an_older_task_table_gains_the_two_columns(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "old.db")
        conn.row_factory = sqlite3.Row
        conn.executescript(LEGACY_SCHEMA)
        # Phase 1-5's version of the table: no group_id, no link_label.
        conn.executescript("""
            CREATE TABLE auth_user (id INTEGER PRIMARY KEY, email_norm TEXT NOT NULL UNIQUE,
              email_display TEXT NOT NULL, password_hash TEXT, display_name TEXT,
              role TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL,
              approved_at INTEGER, approved_by INTEGER, disabled_at INTEGER, last_login_at INTEGER);
            CREATE TABLE user_cloud_download_task (id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL, info_hash TEXT NOT NULL, source_kind TEXT, media_id INTEGER,
              display_title TEXT, submitted_at INTEGER NOT NULL, last_seen_at INTEGER, state TEXT,
              UNIQUE(user_id, info_hash));
        """)
        conn.execute("INSERT INTO user_cloud_download_task(user_id, info_hash, submitted_at) "
                     "VALUES(1,'kept',10)")
        conn.commit()

        auth_service.ensure_schema(conn)
        conn.commit()

        columns = {row[1] for row in conn.execute("PRAGMA table_info(user_cloud_download_task)")}
        assert {"group_id", "link_label"} <= columns
        # The query the task list actually runs now works.
        row = conn.execute("SELECT info_hash, group_id, link_label FROM user_cloud_download_task").fetchone()
        assert row["info_hash"] == "kept", "the upgrade keeps what was there"
        conn.close()

    def test_upgrading_twice_changes_nothing_further(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "twice.db")
        conn.executescript(LEGACY_SCHEMA)
        auth_service.ensure_schema(conn)
        first = sorted(row[0] or "" for row in conn.execute("SELECT sql FROM sqlite_master"))
        auth_service.ensure_schema(conn)
        assert sorted(row[0] or "" for row in conn.execute("SELECT sql FROM sqlite_master")) == first
        conn.close()

    def test_the_upgrade_list_names_only_real_columns(self):
        for table, column, column_type in auth_service.EXTENDED_TABLES:
            # ALTER TABLE ADD COLUMN accepts NOT NULL only with a DEFAULT, so
            # the one non-null addition (user_secret.version) carries one.
            assert column_type in {"INTEGER", "TEXT", "INTEGER NOT NULL DEFAULT 0"}, (table, column, column_type)
            if "NOT NULL" in column_type:
                assert "DEFAULT" in column_type, (table, column)


# ---------------------------------------------------------------------------
# F07: an explicit migration marker, so "the user cleared it" is not read as
# "never migrated"
# ---------------------------------------------------------------------------


def _profile(deployment) -> dict | None:
    conn = sqlite3.connect(f"file:{deployment['db']}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM user_115_profile").fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


class TestTheProfileIsCopiedExactlyOnce:
    def test_the_first_apply_copies_and_marks(self, deployment):
        code, report = _run(deployment, "--auth-migrate", "--apply")
        assert code == 0
        assert report["items"]["user_115_profile"] == {
            "root_cid_present": False, "target_present": True, "copied": 1, "skipped": 0}
        row = _profile(deployment)
        assert row["default_target_cid"] == "legacy-folder-cid"
        assert row["default_target_label"] == "/115pan/影视"
        assert row["migrated_at"] is not None

    def test_a_changed_default_survives_a_re_run(self, deployment):
        _run(deployment, "--auth-migrate", "--apply")
        conn = sqlite3.connect(deployment["db"])
        conn.execute("UPDATE user_115_profile SET open_root_cid='new-root', default_target_label='new-label', "
                     "default_target_cid='chosen-since'")
        conn.commit()
        conn.close()
        _code, report = _run(deployment, "--auth-migrate", "--apply")
        assert report["items"]["user_115_profile"]["skipped"] == 1
        assert report["items"]["user_115_profile"]["copied"] == 0
        row = _profile(deployment)
        assert (row["default_target_cid"], row["open_root_cid"], row["default_target_label"]) == (
            "chosen-since", "new-root", "new-label")

    def test_a_default_the_user_cleared_stays_cleared(self, deployment):
        """F07's counter-example: NULL is a decision, not a gap. Deciding by
        that one field restored root, label *and* default from the old global
        settings on the next run."""
        _run(deployment, "--auth-migrate", "--apply")
        conn = sqlite3.connect(deployment["db"])
        conn.execute("UPDATE user_115_profile SET open_root_cid='new-root', default_target_label='new-label', "
                     "default_target_cid=NULL")
        conn.commit()
        conn.close()
        _code, report = _run(deployment, "--auth-migrate", "--apply")
        assert report["items"]["user_115_profile"]["skipped"] == 1
        row = _profile(deployment)
        assert row["default_target_cid"] is None, "a cleared default was restored from the old global setting"
        assert (row["open_root_cid"], row["default_target_label"]) == ("new-root", "new-label")

    def test_a_third_run_still_changes_nothing(self, deployment):
        _run(deployment, "--auth-migrate", "--apply")
        before = _profile(deployment)
        _run(deployment, "--auth-migrate", "--apply")
        _run(deployment, "--auth-migrate", "--apply")
        assert _profile(deployment) == before

    def test_the_dry_run_agrees_with_the_apply(self, deployment):
        _code, before = _run(deployment, "--auth-migrate", "--dry-run")
        assert before["items"]["user_115_profile"]["would_copy"] is True
        assert before["items"]["user_115_profile"]["already_migrated"] is False
        assert before["items"]["user_115_profile"]["readable"] is True
        _code, applied = _run(deployment, "--auth-migrate", "--apply")
        assert applied["items"]["user_115_profile"]["copied"] == 1
        _code, after = _run(deployment, "--auth-migrate", "--dry-run")
        assert after["items"]["user_115_profile"]["would_copy"] is False
        assert after["items"]["user_115_profile"]["already_migrated"] is True

    def test_the_copied_token_records_who_rotates_it(self, deployment):
        """F02: the pair came from OpenList, so the migration says so and the
        recovery path follows OpenList rather than presenting its refresh
        token to 115."""
        conn = sqlite3.connect(deployment["db"])
        conn.execute("INSERT INTO secrets(name, value, updated_at) VALUES(?,?,?)",
                     ("115_open_access_token", deployment["fernet"].encrypt(b"fake-open-access"), 1))
        conn.execute("INSERT INTO secrets(name, value, updated_at) VALUES(?,?,?)",
                     ("115_open_refresh_token", deployment["fernet"].encrypt(b"fake-open-refresh"), 1))
        conn.commit()
        conn.close()
        _code, report = _run(deployment, "--auth-migrate", "--apply")
        assert report["items"]["115_open_access_token"]["copied"] == 1
        assert _profile(deployment)["open_token_origin"] == "openlist_legacy"


# ---------------------------------------------------------------------------
# F08: the dry run counts this administrator's own outstanding tasks
# ---------------------------------------------------------------------------


class TestTheDryRunTaskCount:
    def _add_member_task(self, deployment, info_hash="hash-legacy-1"):
        """A task belonging to somebody else. Subtracting table totals counted
        it as migration work already done."""
        _run(deployment, "--auth-migrate", "--apply")   # creates the schema + admin
        conn = sqlite3.connect(deployment["db"])
        conn.execute("INSERT INTO auth_user(email_norm, email_display, password_hash, display_name, role, "
                     "status, created_at) VALUES('m@example.test','m@example.test','x',NULL,"
                     "'member','active',1)")
        member_id = conn.execute("SELECT id FROM auth_user WHERE email_norm='m@example.test'").fetchone()[0]
        conn.execute("INSERT INTO user_cloud_download_task(user_id, info_hash, source_kind, submitted_at) "
                     "VALUES(?,?,?,?)", (member_id, info_hash, "library", 200))
        conn.commit()
        conn.close()
        return member_id

    def test_an_unrelated_member_task_is_not_counted_as_migrated(self, deployment):
        # Start over: a fresh legacy task the administrator has not migrated,
        # plus one member task that has nothing to do with it.
        member_id = self._add_member_task(deployment, info_hash="hash-member-only")
        conn = sqlite3.connect(deployment["db"])
        conn.execute("INSERT INTO cloud_download_task(info_hash, link_public_id, wp_path_id, submitted_at) "
                     "VALUES('hash-legacy-2','pub-2','cid-2',300)")
        conn.commit()
        conn.close()
        _code, dry = _run(deployment, "--auth-migrate", "--dry-run")
        assert dry["items"]["cloud_download_task"]["would_copy"] == 1, dry["items"]["cloud_download_task"]
        _code, applied = _run(deployment, "--auth-migrate", "--apply")
        assert applied["items"]["cloud_download_task"]["copied"] == 1
        assert member_id

    def test_a_member_holding_the_same_hash_does_not_hide_the_work(self, deployment):
        conn = sqlite3.connect(deployment["db"])
        conn.execute("INSERT INTO cloud_download_task(info_hash, link_public_id, wp_path_id, submitted_at) "
                     "VALUES('hash-shared','pub-3','cid-3',300)")
        conn.commit()
        conn.close()
        self._add_member_task(deployment, info_hash="hash-shared")
        # The administrator already has hash-legacy-1 and hash-shared from the
        # apply inside the helper, so there is nothing left to copy -- and the
        # member's identical hash must not change that either way.
        _code, dry = _run(deployment, "--auth-migrate", "--dry-run")
        _code, applied = _run(deployment, "--auth-migrate", "--apply")
        assert dry["items"]["cloud_download_task"]["would_copy"] == applied["items"]["cloud_download_task"]["copied"]

    def test_the_estimate_matches_the_apply_at_every_stage(self, deployment):
        conn = sqlite3.connect(deployment["db"])
        for index in range(2, 5):
            conn.execute("INSERT INTO cloud_download_task(info_hash, link_public_id, wp_path_id, submitted_at) "
                         f"VALUES('hash-legacy-{index}','pub-{index}','cid-{index}',{100 * index})")
        conn.commit()
        conn.close()
        for _ in range(3):
            _code, dry = _run(deployment, "--auth-migrate", "--dry-run")
            estimate = dry["items"]["cloud_download_task"]["would_copy"]
            _code, applied = _run(deployment, "--auth-migrate", "--apply")
            assert applied["items"]["cloud_download_task"]["copied"] == estimate, (estimate, applied["items"])

    def test_with_no_new_schema_at_all_the_estimate_is_every_legacy_row(self, deployment):
        _code, dry = _run(deployment, "--auth-migrate", "--dry-run")
        assert dry["items"]["cloud_download_task"] == {
            "source_rows": 1, "already_migrated": 0, "would_copy": 1}
        assert _fingerprint(deployment["db"])  # still readable, still untouched


# ---------------------------------------------------------------------------
# G04: a database the older branch already migrated
# ---------------------------------------------------------------------------

OLD_COMMIT = "ce4d72c"


@pytest.fixture(scope="module")
def old_tree(tmp_path_factory):
    """The working copy exactly as it was at the commit that had no
    `migrated_at` column, extracted read-only.

    G04 is about a database *that branch* produced, so the fixture runs that
    branch's own migration rather than imitating it.
    """
    available = subprocess.run(["git", "cat-file", "-e", OLD_COMMIT + "^{commit}"],
                               cwd=str(ROOT), capture_output=True)
    if available.returncode:
        pytest.skip("Internal historical revision is not part of the sanitized public history")
    target = tmp_path_factory.mktemp("old-branch")
    archive = subprocess.run(["git", "archive", OLD_COMMIT], cwd=str(ROOT),
                             capture_output=True, timeout=120)
    assert archive.returncode == 0, archive.stderr.decode("utf-8", "replace")
    extract = subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout,
                             capture_output=True, timeout=120)
    assert extract.returncode == 0, extract.stderr.decode("utf-8", "replace")
    columns = (target / "auth_service.py").read_text(encoding="utf-8")
    assert "migrated_at" not in columns, "this fixture is only meaningful before that column existed"
    return target


def _run_in(tree: Path, deployment, *args) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, "app.py", *args], cwd=str(tree),
                            env=deployment["env"], capture_output=True, text=True, timeout=180)
    payload = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
    return result.returncode, payload


def _profile_row(deployment) -> dict | None:
    conn = sqlite3.connect(f"file:{deployment['db']}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM user_115_profile").fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _edit_profile(deployment, **values):
    assignments = ", ".join(f"{name}=?" for name in values)
    conn = sqlite3.connect(deployment["db"])
    try:
        conn.execute(f"UPDATE user_115_profile SET {assignments}", tuple(values.values()))
        conn.commit()
    finally:
        conn.close()


class TestAnOlderBranchsMigration:
    """G04: every row in a database upgraded from the old branch has
    `migrated_at` NULL -- including rows that branch's own migration filled and
    the administrator has since edited. Reading "no marker" as "no data" put
    the old global values back over their choices."""

    def test_the_old_branch_really_migrates_without_the_marker(self, deployment, old_tree):
        code, report = _run_in(old_tree, deployment, "--auth-migrate", "--apply")
        assert code == 0, report
        assert report["items"]["user_115_profile"]["copied"] == 1
        columns = {row[1] for row in sqlite3.connect(deployment["db"]).execute(
            "PRAGMA table_info(user_115_profile)")}
        assert "migrated_at" not in columns
        row = _profile_row(deployment)
        assert row["default_target_cid"] == "legacy-folder-cid"

    def test_an_edited_configuration_survives_the_upgrade_and_the_new_apply(self, deployment, old_tree):
        _run_in(old_tree, deployment, "--auth-migrate", "--apply")
        _edit_profile(deployment, open_root_cid="new-root", default_target_label="new-label",
                      default_target_cid="chosen-since")

        code, dry = _run(deployment, "--auth-migrate", "--dry-run")
        assert code == 0, dry
        assert dry["items"]["user_115_profile"]["would_copy"] is False, dry["items"]["user_115_profile"]

        _code, applied = _run(deployment, "--auth-migrate", "--apply")
        assert applied["items"]["user_115_profile"]["skipped"] == 1
        assert applied["items"]["user_115_profile"]["copied"] == 0
        row = _profile_row(deployment)
        assert (row["open_root_cid"], row["default_target_label"], row["default_target_cid"]) == (
            "new-root", "new-label", "chosen-since")
        assert row["migrated_at"] is not None, "the marker should be backfilled so the answer is unambiguous"

        # And a third run still changes nothing.
        before = _profile_row(deployment)
        _run(deployment, "--auth-migrate", "--apply")
        assert _profile_row(deployment) == before

    def test_a_deliberately_cleared_configuration_survives_too(self, deployment, old_tree):
        """The hardest case: all three fields empty is indistinguishable from a
        blank row *by value*. The evidence is the secrets the earlier apply
        copied, which is a record rather than a guess."""
        _run_in(old_tree, deployment, "--auth-migrate", "--apply")
        _edit_profile(deployment, open_root_cid=None, default_target_label=None, default_target_cid=None)

        _code, dry = _run(deployment, "--auth-migrate", "--dry-run")
        assert dry["items"]["user_115_profile"]["would_copy"] is False, dry["items"]["user_115_profile"]
        _code, applied = _run(deployment, "--auth-migrate", "--apply")
        assert applied["items"]["user_115_profile"]["skipped"] == 1
        row = _profile_row(deployment)
        assert row["default_target_cid"] is None, "a cleared default was restored from the old global setting"
        assert row["open_root_cid"] is None and row["default_target_label"] is None
        assert row["migrated_at"] is not None

    def test_only_the_default_cleared_is_also_kept(self, deployment, old_tree):
        _run_in(old_tree, deployment, "--auth-migrate", "--apply")
        _edit_profile(deployment, default_target_cid=None, open_root_cid="kept-root")
        _code, applied = _run(deployment, "--auth-migrate", "--apply")
        assert applied["items"]["user_115_profile"]["skipped"] == 1
        row = _profile_row(deployment)
        assert row["default_target_cid"] is None and row["open_root_cid"] == "kept-root"

    def test_a_genuinely_blank_database_is_still_copied(self, deployment):
        """The upgrade compatibility must not turn into "never copy anything"."""
        _code, applied = _run(deployment, "--auth-migrate", "--apply")
        assert applied["items"]["user_115_profile"]["copied"] == 1
        row = _profile_row(deployment)
        assert row["default_target_cid"] == "legacy-folder-cid"
        assert row["migrated_at"] is not None

    def test_a_profile_row_that_only_holds_cookie_state_is_not_mistaken_for_migrated(self, deployment):
        """A profile row can exist before any migration -- `remember_cookie_state`
        creates one. With no folder fields and no migrated secrets, the first
        apply must still copy."""
        _run(deployment, "--auth-migrate", "--dry-run")
        conn = sqlite3.connect(deployment["db"])
        try:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS auth_user (id INTEGER PRIMARY KEY, email_norm TEXT NOT NULL UNIQUE,"
                " email_display TEXT NOT NULL, password_hash TEXT, display_name TEXT, role TEXT NOT NULL,"
                " status TEXT NOT NULL, created_at INTEGER NOT NULL, approved_at INTEGER, approved_by INTEGER,"
                " disabled_at INTEGER, last_login_at INTEGER);"
                "CREATE TABLE IF NOT EXISTS user_115_profile (user_id INTEGER PRIMARY KEY, open_root_cid TEXT,"
                " default_target_cid TEXT, default_target_label TEXT, cookie_state TEXT NOT NULL DEFAULT 'unconfigured',"
                " cookie_checked_at INTEGER, cookie_error_code TEXT, open_state TEXT NOT NULL DEFAULT 'unconfigured',"
                " open_expires_at INTEGER, open_checked_at INTEGER, open_error_code TEXT, updated_at INTEGER NOT NULL);")
            conn.execute("INSERT INTO auth_user(email_norm, email_display, password_hash, display_name, role, "
                         "status, created_at) VALUES('admin@example.invalid','admin@example.invalid',NULL,NULL,"
                         "'admin','active',1)")
            admin_id = conn.execute("SELECT id FROM auth_user").fetchone()[0]
            conn.execute("INSERT INTO user_115_profile(user_id, cookie_state, updated_at) "
                         "VALUES(?, 'reauth_required', 1)", (admin_id,))
            conn.commit()
        finally:
            conn.close()
        _code, applied = _run(deployment, "--auth-migrate", "--apply")
        assert applied["items"]["user_115_profile"]["copied"] == 1, applied["items"]["user_115_profile"]
        assert _profile_row(deployment)["default_target_cid"] == "legacy-folder-cid"


# ---------------------------------------------------------------------------
# G05: the dry run has to work before the apply, on every structure
# ---------------------------------------------------------------------------


class TestTheDryRunOnEveryStructure:
    def _columns(self, deployment, table):
        conn = sqlite3.connect(f"file:{deployment['db']}?mode=ro", uri=True)
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def test_an_old_profile_table_without_the_column_still_plans(self, deployment, old_tree):
        """G05: the dry run used to SELECT `migrated_at` after checking only
        that the table existed -- `OperationalError: no such column`, so
        "dry-run first, then decide" could not be done at all."""
        _run_in(old_tree, deployment, "--auth-migrate", "--apply")
        assert "migrated_at" not in self._columns(deployment, "user_115_profile")
        before = _fingerprint(deployment["db"])

        code, report = _run(deployment, "--auth-migrate", "--dry-run")
        assert code == 0, report
        assert report["ok"] is True
        item = report["items"]["user_115_profile"]
        assert item["readable"] is True
        assert item["would_copy"] is False
        assert "earlier migration" in item["reason"] or "administrator" in item["reason"], item

        assert _fingerprint(deployment["db"]) == before, "the dry run wrote to the database"
        assert "migrated_at" not in self._columns(deployment, "user_115_profile")

    def test_no_new_tables_at_all(self, deployment):
        before = _fingerprint(deployment["db"])
        code, report = _run(deployment, "--auth-migrate", "--dry-run")
        assert code == 0
        assert report["items"]["user_115_profile"]["would_copy"] is True
        assert report["items"]["user_115_profile"]["readable"] is True
        assert _fingerprint(deployment["db"]) == before

    def test_a_partially_upgraded_profile_table(self, deployment, old_tree):
        """Some of the new columns, not all: still a real plan."""
        _run_in(old_tree, deployment, "--auth-migrate", "--apply")
        conn = sqlite3.connect(deployment["db"])
        try:
            conn.execute("ALTER TABLE user_115_profile ADD COLUMN open_token_origin TEXT")
            conn.commit()
        finally:
            conn.close()
        before = _fingerprint(deployment["db"])
        code, report = _run(deployment, "--auth-migrate", "--dry-run")
        assert code == 0, report
        assert report["items"]["user_115_profile"]["readable"] is True
        assert report["items"]["user_115_profile"]["would_copy"] is False
        assert _fingerprint(deployment["db"]) == before

    def test_a_fully_upgraded_database(self, deployment):
        _run(deployment, "--auth-migrate", "--apply")
        before = _fingerprint(deployment["db"])
        code, report = _run(deployment, "--auth-migrate", "--dry-run")
        assert code == 0
        assert report["items"]["user_115_profile"]["would_copy"] is False
        assert _fingerprint(deployment["db"]) == before

    def test_the_estimate_matches_the_apply_on_an_old_database(self, deployment, old_tree):
        _run_in(old_tree, deployment, "--auth-migrate", "--apply")
        _edit_profile(deployment, default_target_cid="chosen-since")
        _code, dry = _run(deployment, "--auth-migrate", "--dry-run")
        _code, applied = _run(deployment, "--auth-migrate", "--apply")
        assert dry["items"]["user_115_profile"]["would_copy"] is False
        assert applied["items"]["user_115_profile"]["copied"] == 0
        assert dry["items"]["cloud_download_task"]["would_copy"] == applied["items"]["cloud_download_task"]["copied"]


# ---------------------------------------------------------------------------
# G05: a failure in the middle of the copy, and the re-run after it
# ---------------------------------------------------------------------------


class TestAFailureInTheMiddleOfTheCopy:
    """The staged design (schema first, then data) is kept, so its boundary is
    tested rather than asserted: an exception during the copy rolls the copied
    rows back, leaves the empty tables, and the re-run finishes the job."""

    def test_an_interrupted_apply_can_simply_be_re_run(self, deployment, tmp_path):
        crash = tmp_path / "crash.py"
        crash.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import app, user_115\n"
            "real = user_115.remember_open_origin\n"
            "calls = {'n': 0}\n"
            "def flaky(*a, **k):\n"
            "    calls['n'] += 1\n"
            "    raise RuntimeError('interrupted in the middle of the copy')\n"
            "user_115.remember_open_origin = flaky\n"
            "try:\n"
            "    app.run_auth_migrate(apply=True)\n"
            "except RuntimeError as exc:\n"
            "    print('CRASHED', exc)\n",
            encoding="utf-8")
        # Give the deployment an open-token secret so the crashing step is reached.
        conn = sqlite3.connect(deployment["db"])
        try:
            conn.execute("INSERT INTO secrets(name, value, updated_at) VALUES(?,?,?)",
                         ("115_open_access_token", deployment["fernet"].encrypt(b"fake-open-access"), 1))
            conn.commit()
        finally:
            conn.close()

        result = subprocess.run([sys.executable, str(crash)], cwd=str(ROOT), env=deployment["env"],
                                capture_output=True, text=True, timeout=180)
        assert "CRASHED" in result.stdout, f"{result.stdout}\n{result.stderr}"

        conn = sqlite3.connect(f"file:{deployment['db']}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            secrets_copied = conn.execute("SELECT COUNT(*) AS c FROM user_secret").fetchone()["c"]
        finally:
            conn.close()
        # The staged boundary, stated plainly: the tables are there, the data
        # of the interrupted transaction is not.
        assert "user_secret" in tables and "user_115_profile" in tables
        assert secrets_copied == 0, "the interrupted copy was not rolled back"

        # And the re-run completes it.
        code, report = _run(deployment, "--auth-migrate", "--apply")
        assert code == 0, report
        assert report["items"]["115_web_cookie"]["copied"] == 1
        assert report["items"]["115_open_access_token"]["copied"] == 1
        assert report["items"]["user_115_profile"]["copied"] == 1
        conn = sqlite3.connect(f"file:{deployment['db']}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            assert conn.execute("SELECT COUNT(*) AS c FROM user_secret").fetchone()["c"] == 2
        finally:
            conn.close()

        # A second re-run copies nothing more.
        _code, again = _run(deployment, "--auth-migrate", "--apply")
        assert again["items"]["115_web_cookie"]["skipped"] == 1
        assert again["items"]["user_115_profile"]["skipped"] == 1
