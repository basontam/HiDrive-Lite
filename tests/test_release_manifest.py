"""Tests for scripts/make_release_manifest.py using a throwaway git repo."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import make_release_manifest as mrm  # noqa: E402

JUNIT_OK = '<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="84" failures="0" errors="0" skipped="0"/></testsuites>'
JUNIT_BAD = '<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="84" failures="1" errors="0" skipped="0"/></testsuites>'

APP_SRC = '''"""app"""
import os

DATA_DIR = os.getenv("HIDRIVE_DATA_DIR", "/tmp/x")


def check_csrf():
    return True


def api_status():
    return {"ok": True}
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _write_checks(repo: Path, junit: str = JUNIT_OK, scan_status: str = "passed"):
    artifacts = repo / "build" / "test-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "junit.xml").write_text(junit)
    (artifacts / "secret-scan.json").write_text(json.dumps({"status": scan_status, "findings": [], "scanned_files": 3}))
    (artifacts / "syntax.txt").write_text("py_compile OK\n")


def _write_bridge(bin_dir: Path, status_json: str) -> None:
    bridge = bin_dir / "hidrive-lite-release"
    bridge.write_text("#!/bin/sh\necho '" + status_json + "'\n")
    bridge.chmod(0o755)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.test")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text("build/\nrelease-manifest*.json\n")
    (tmp_path / "app.py").write_text(APP_SRC)
    (tmp_path / "sync_openlist_115.py").write_text("print('sync')\n")
    (tmp_path / "requirements.txt").write_text("Flask==3.1.1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("# notes\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


def _commit_change(repo: Path, path: str, content: str, message: str = "change") -> str:
    (repo / path).write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_lists_allowlisted_files_with_current_and_previous_hashes(repo):
    base = _git(repo, "rev-parse", "HEAD")
    old_app_sha = _sha(repo / "app.py")
    head = _commit_change(repo, "app.py", APP_SRC + "\n\ndef api_extra():\n    return 1\n")
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="definitely-not-installed-xyz")

    assert manifest["manifest_version"] == 1
    assert manifest["commit"] == head and manifest["base_commit"] == base
    assert manifest["tree_clean"] is True
    by_path = {f["path"]: f for f in manifest["files"]}
    assert set(by_path) == {"app.py", "sync_openlist_115.py"}
    assert by_path["app.py"]["sha256"] == _sha(repo / "app.py")
    assert by_path["app.py"]["previous_sha256"] == old_app_sha
    assert by_path["app.py"]["changed"] is True
    assert by_path["sync_openlist_115.py"]["changed"] is False
    assert by_path["app.py"]["size"] == (repo / "app.py").stat().st_size


def test_plain_app_change_with_passing_checks_is_releasable(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "app.py", APP_SRC + "\n\ndef api_extra():\n    return 1\n")
    _commit_change(repo, "docs/notes.md", "# notes\nmore\n")
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="definitely-not-installed-xyz")

    assert manifest["changed_files"] == [{"path": "app.py", "class": "release"}, {"path": "docs/notes.md", "class": "support"}]
    assert manifest["sensitive_hunks"] == []
    assert manifest["checks"]["tests"] == {"status": "passed", "tests": 84, "failures": 0, "errors": 0, "skipped": 0, "report": "build/test-artifacts/junit.xml"}
    assert manifest["checks"]["secret_scan"]["status"] == "passed"
    assert manifest["checks"]["syntax"]["status"] == "passed"
    assert manifest["release_allowed"] is True
    assert manifest["blocking_reasons"] == []
    assert manifest["bridge"] == {"command": "definitely-not-installed-xyz", "status": "unavailable", "action": "manifest_only", "capabilities": []}


def test_blocked_file_classes_stop_auto_release(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "requirements.txt", "Flask==3.1.2\n")
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x")

    assert manifest["changed_files"] == [{"path": "requirements.txt", "class": "blocked"}]
    assert manifest["release_allowed"] is False
    assert any("requirements.txt" in reason for reason in manifest["blocking_reasons"])


@pytest.mark.parametrize("path", ["deploy/hidrive-lite.service", "hidrive-lite.service", "hidrive-lite-checkin.timer", "hidrive-lite.env.template", "cloudflared-hidrive-lite.service"])
def test_deployment_files_are_blocked(path):
    assert mrm.classify(path) == "blocked"


@pytest.mark.parametrize("path", ["docs/x.md", "tests/test_x.py", "scripts/scan_secrets.py", "README.md", "CLAUDE.md", "DEPLOYMENT.md", ".gitignore", "pytest.ini", "requirements-dev.txt", "NOTICE"])
def test_support_files_do_not_affect_release(path):
    assert mrm.classify(path) == "support"


def test_unknown_files_require_review():
    assert mrm.classify("mystery.py") == "review"
    assert mrm.classify("assets/logo.png") == "review"


def test_sensitive_functions_match_the_release_bridge_rule_set():
    # The release bridge re-detects sensitive hunks with its own copy of this
    # list and rejects a manifest whose sensitive_hunks differ ("sensitive
    # hunk classification does not match"). Both sides must change in the
    # same step: on 2026-09-06 the bridge (Codex, host side) adopted the
    # T19 names -- check_115_cookie was replaced by verify_115_session, the
    # reauth adapter's own routes were added, and the stale "main" entry
    # (no such function in app.py) was dropped -- so this list mirrors that.
    #
    # 2026-09-12 (review R15): the multi-user authorisation, session,
    # per-user credential, member-policy and migration entry points joined
    # it. The bridge's own copy must be extended in the same step, or every
    # manifest is rejected for a classification mismatch -- which is the
    # safe direction, but it does mean the two sides move together. See
    # docs/codex-release-bridge-multiuser-20260911.md.
    assert mrm.SENSITIVE_FUNCTIONS == {
        "_115_setting", "_acting_user", "_admin_user_action", "_admin_user_row",
        "_deployment_115", "_load_auth_session", "_open115_config",
        "_open115_device_blocked", "_open115_flow_error", "api_me_115_open_status", "api_me_115_open_cancel",
        "_principal_is_admin", "_reauth_complete", "_require_open115", "_safe_next",
        "_session_cookie", "_user_115_state", "allow_member_re0_unlock",
        "api_115_reauth_cancel", "api_115_reauth_qr", "api_115_reauth_start",
        "api_115_reauth_status", "api_admin_policy_re0", "api_admin_user_approve",
        "api_admin_user_disable", "api_admin_user_reject", "api_admin_users",
        "api_auth_login", "api_auth_logout", "api_auth_register", "api_me",
        "api_me_115_disconnect", "api_me_115_open_start", "api_me_115_status",
        "api_settings", "auth_google", "authorize_request", "before_request",
        "check_csrf", "config_value", "connect_db", "cookie_status", "csrf_key",
        "csrf_subject", "csrf_token", "current_user", "decrypt_token", "get_tokens",
        "handle_error", "hdhive_headers", "init_db", "load_fernet",
        "member_unlock_refused", "oauth_callback", "oauth_start",
        "refresh_hdhive_token", "remember_115_cookie_check", "require_access",
        "require_login", "require_role", "run_auth_migrate", "save_tokens",
        "secret_get", "secret_set", "sync_openlist_credentials_from_source",
        "user_115_cookie", "user_115_open_token", "valid_hdhive_access_token",
        "verify_115_session", "verify_access_jwt",
    }


def test_sensitive_hunk_in_auth_code_stops_auto_release(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "app.py", APP_SRC.replace("def check_csrf():\n    return True", "def check_csrf():\n    return False"))
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x")

    assert manifest["release_allowed"] is False
    assert manifest["sensitive_hunks"] == [{"path": "app.py", "context": "def check_csrf():", "reason": "function is on the sensitive list"}]


def test_sensitive_keyword_in_added_lines_stops_auto_release(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "app.py", APP_SRC + '\n\ndef api_extra():\n    return os.getenv("ENV_115_COOKIES")\n')
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x")

    assert manifest["release_allowed"] is False
    (hunk,) = manifest["sensitive_hunks"]
    assert hunk["path"] == "app.py" and hunk["reason"].startswith("changed lines mention")


def test_dirty_tree_stops_auto_release(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "app.py", APP_SRC + "\n\ndef api_extra():\n    return 1\n")
    (repo / "app.py").write_text(APP_SRC + "\n# uncommitted\n")
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x")

    assert manifest["tree_clean"] is False
    assert manifest["release_allowed"] is False
    assert "uncommitted" in " ".join(manifest["blocking_reasons"])


def test_failed_or_missing_checks_stop_auto_release(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "app.py", APP_SRC + "\n\ndef api_extra():\n    return 1\n")

    missing = mrm.build_manifest(repo, base=base, bridge_command="x")
    assert missing["checks"]["tests"]["status"] == "missing"
    assert missing["release_allowed"] is False

    _write_checks(repo, junit=JUNIT_BAD)
    failed = mrm.build_manifest(repo, base=base, bridge_command="x")
    assert failed["checks"]["tests"]["status"] == "failed"
    assert failed["release_allowed"] is False

    _write_checks(repo, scan_status="failed")
    scan_failed = mrm.build_manifest(repo, base=base, bridge_command="x")
    assert scan_failed["release_allowed"] is False


def test_bridge_detected_on_path(repo, tmp_path, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    _write_checks(repo)
    fake_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    fake_bin.mkdir()
    bridge = fake_bin / "hidrive-lite-release"
    bridge.write_text("#!/bin/sh\nexit 0\n")
    bridge.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))

    manifest = mrm.build_manifest(repo, base=base)

    assert manifest["bridge"] == {"command": "hidrive-lite-release", "status": "available", "action": "submit", "capabilities": []}


def test_main_writes_manifest_named_by_short_commit(repo, capsys):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit_change(repo, "app.py", APP_SRC + "\n\ndef api_extra():\n    return 1\n")
    _write_checks(repo)

    assert mrm.main(["--root", str(repo), "--base", base]) == 0

    out_path = repo / f"release-manifest-{head[:12]}.json"
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert data["commit"] == head and data["release_allowed"] is True
    assert str(out_path) in capsys.readouterr().out


def test_main_returns_nonzero_when_release_is_not_allowed(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "requirements.txt", "Flask==3.1.2\n")
    _write_checks(repo)
    assert mrm.main(["--root", str(repo), "--base", base]) == 1


def test_bridge_detected_in_local_bin_when_not_on_path(repo, tmp_path, monkeypatch):
    """The container installs the client in ~/.local/bin, which is not on the
    default PATH of a non-login shell; detection must still succeed."""
    base = _git(repo, "rev-parse", "HEAD")
    _write_checks(repo)
    fake_home = tmp_path.parent / f"{tmp_path.name}-home"
    local_bin = fake_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    bridge = local_bin / "hidrive-lite-release"
    bridge.write_text("#!/bin/sh\nexit 0\n")
    bridge.chmod(0o755)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    manifest = mrm.build_manifest(repo, base=base)

    assert manifest["bridge"]["status"] == "available"


def test_new_release_files_are_listed(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "library_normalize.py").write_text("def normalize():\n    return None\n")
    (repo / "templates").mkdir()
    (repo / "templates" / "index.html").write_text("<html></html>\n")
    (repo / "static").mkdir()
    (repo / "static" / "app.js").write_text("console.log('ok');\n")
    _git(repo, "add", "library_normalize.py", "templates/index.html", "static/app.js")
    _git(repo, "commit", "-q", "-m", "add media library files")
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x")

    assert {f["path"] for f in manifest["files"]} == {
        "app.py",
        "sync_openlist_115.py",
        "library_normalize.py",
        "templates/index.html",
        "static/app.js",
    }


def test_templates_and_static_are_release_class():
    assert mrm.classify("templates/index.html") == "release"
    assert mrm.classify("static/app.css") == "release"
    assert mrm.classify("static/vendor/x.js") == "release"


def test_new_asset_matching_release_glob_but_not_allowlisted_blocks_release(repo):
    # I5: RELEASE_GLOBS classifies any templates/*|static/* path as
    # "release" (release_allowed could stay true), but files[] only ever
    # iterates the fixed RELEASE_FILES tuple -- so a brand new asset would
    # be silently omitted from what actually ships.
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "static").mkdir()
    (repo / "static" / "new-widget.js").write_text("console.log('new');\n")
    _git(repo, "add", "static/new-widget.js")
    _git(repo, "commit", "-q", "-m", "add new static asset")
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x")

    assert manifest["changed_files"] == [{"path": "static/new-widget.js", "class": "release"}]
    assert manifest["release_allowed"] is False
    assert any(
        "static/new-widget.js" in reason and "RELEASE_FILES" in reason
        for reason in manifest["blocking_reasons"]
    )
    # and, as before the fix, it's still silently absent from files[] --
    # proving that's exactly the gap the new blocking reason must cover.
    assert "static/new-widget.js" not in {f["path"] for f in manifest["files"]}


def test_deleted_allowlisted_file_blocks_release(repo):
    # I5: an allowlisted (RELEASE_FILES) file that existed at the base
    # commit but is missing now must block release, not be silently
    # skipped out of files[].
    (repo / "static").mkdir()
    (repo / "static" / "app.css").write_text("body{}\n")
    _git(repo, "add", "static/app.css")
    _git(repo, "commit", "-q", "-m", "add app.css")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "static" / "app.css").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "remove app.css")
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x")

    assert manifest["release_allowed"] is False
    assert any("static/app.css" in reason for reason in manifest["blocking_reasons"])
    assert "static/app.css" not in {f["path"] for f in manifest["files"]}


def test_approval_overrides_sensitive_hunks_only(repo, tmp_path, monkeypatch):
    # A capable fake bridge is set up here so this test isolates the
    # approval-vs-sensitive-hunks behaviour (T0.2 point 3) from the
    # separate bridge-capability gate (T0.2 point 5, covered by
    # test_bridge_capabilities_from_status below).
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "app.py", APP_SRC.replace("def check_csrf():\n    return True", "def check_csrf():\n    return False"))
    _write_checks(repo)
    fake_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    fake_bin.mkdir()
    _write_bridge(fake_bin, '{"status":"ready","capabilities":["approval"]}')
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))

    manifest = mrm.build_manifest(repo, base=base, approved="ok by owner")

    assert manifest["release_allowed"] is True
    assert manifest["sensitive_hunks"] != []
    assert manifest["approval"]["reason"] == "ok by owner"


def test_approval_does_not_override_failed_checks(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "app.py", APP_SRC.replace("def check_csrf():\n    return True", "def check_csrf():\n    return False"))
    _write_checks(repo, junit=JUNIT_BAD)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x", approved="ok by owner")

    assert manifest["release_allowed"] is False


def test_manifest_without_approval_has_null_field(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit_change(repo, "app.py", APP_SRC + "\n\ndef api_extra():\n    return 1\n")
    _write_checks(repo)

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x")

    assert manifest["approval"] is None
    assert manifest["artifacts"] == []


def test_bundle_artifact_recorded_with_hash(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _write_checks(repo)
    bundle_path = repo / ".local-index" / "library-bundle.sqlite"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(bundle_path))
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')")
    conn.commit()
    conn.close()

    manifest = mrm.build_manifest(repo, base=base, bridge_command="x", bundle=bundle_path)

    (artifact,) = manifest["artifacts"]
    assert artifact["kind"] == "library-bundle"
    assert artifact["path"] == ".local-index/library-bundle.sqlite"
    assert artifact["sha256"] == hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    assert artifact["size"] == bundle_path.stat().st_size
    assert artifact["schema_version"] == 1


def test_bundle_outside_workspace_rejected(repo, tmp_path, capsys):
    base = _git(repo, "rev-parse", "HEAD")
    _write_checks(repo)
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    outside = elsewhere / "library-bundle.sqlite"
    outside.write_text("not a database\n")

    assert mrm.main(["--root", str(repo), "--base", base, "--bundle", str(outside)]) == 2
    assert "bundle" in capsys.readouterr().err


def test_bundle_symlink_rejected(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _write_checks(repo)
    real = repo / "library-bundle.sqlite"
    real.write_text("not a database\n")
    link = repo / "library-bundle-link.sqlite"
    os.symlink(real, link)

    with pytest.raises(ValueError):
        mrm.build_manifest(repo, base=base, bridge_command="x", bundle=link)


def test_bridge_capabilities_from_status(repo, tmp_path, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    _write_checks(repo)
    bundle_path = repo / ".local-index" / "library-bundle.sqlite"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text("not a real database\n")
    fake_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    fake_bin.mkdir()
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))

    _write_bridge(fake_bin, '{"status":"ready","capabilities":["artifacts","approval"]}')
    manifest = mrm.build_manifest(repo, base=base, approved="ok by owner", bundle=bundle_path)
    assert "bridge lacks the artifacts capability" not in manifest["blocking_reasons"]
    assert "bridge lacks the approval capability" not in manifest["blocking_reasons"]

    _write_bridge(fake_bin, '{"status":"ready"}')
    manifest = mrm.build_manifest(repo, base=base, approved="ok by owner", bundle=bundle_path)
    assert "bridge lacks the artifacts capability" in manifest["blocking_reasons"]
    assert "bridge lacks the approval capability" in manifest["blocking_reasons"]


# ---------------------------------------------------------------------------
# Option A for the RE0 module: re0_sync.py joins the allowlist (the host
# bridge mirrors it), deployment files stay out of the manifest but can be
# acknowledged as manually released so they no longer block auto-release.
# ---------------------------------------------------------------------------


def test_re0_sync_is_on_the_release_allowlist_next_to_library_tmdb():
    assert "re0_sync.py" in mrm.RELEASE_FILES
    assert mrm.classify("re0_sync.py") == "release"
    assert mrm.RELEASE_FILES.index("re0_sync.py") == mrm.RELEASE_FILES.index("library_tmdb.py") + 1
    # 11 application files, the nine brand assets the bridge took on earlier
    # on 2026-09-11, and the three multi-user files added with Phase 8 (the
    # bridge's own cap moved 11 -> 20 -> 24 alongside), plus the device QR encoder.
    assert len(mrm.RELEASE_FILES) == 24


def test_device_qr_vendor_is_an_exact_release_path_with_hashes(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "static/vendor").mkdir(parents=True)
    _commit_change(repo, "static/vendor/qrcode.js", "// qr-library-fixture\n")
    _write_checks(repo)
    manifest = mrm.build_manifest(repo, base=base, bridge_command="x")
    entry = next(item for item in manifest["files"] if item["path"] == "static/vendor/qrcode.js")
    assert entry["sha256"] == _sha(repo / "static/vendor/qrcode.js")
    assert entry["previous_sha256"] is None and entry["changed"] is True
    assert {"path": "static/vendor/qrcode.js", "class": "release"} in manifest["changed_files"]
    assert "static/vendor/other.js" not in mrm.RELEASE_FILES


def test_device_routes_remain_sensitive():
    assert {"_open115_device_blocked", "_open115_flow_error", "api_me_115_open_status",
            "api_me_115_open_cancel"} <= mrm.SENSITIVE_FUNCTIONS


def test_every_brand_asset_is_on_the_release_allowlist():
    assets = [path for path in mrm.RELEASE_FILES if path.startswith("static/branding/")]
    assert len(assets) == 9
    for path in assets:
        assert mrm.classify(path) == "release"
        assert path.startswith("static/branding/hidrive-lite/")
    assert len(mrm.RELEASE_FILES) <= 24, "the bridge accepts at most 24 allowlisted paths"


def test_the_multi_user_modules_are_on_the_release_allowlist():
    """app.py imports auth_service and user_115 at module level and renders
    login.html, so a release carrying app.py without them would start a
    worker that cannot import."""
    for path in ("auth_service.py", "user_115.py", "templates/login.html"):
        assert path in mrm.RELEASE_FILES, path
        assert mrm.classify(path) == "release", path


def test_manual_release_ack_records_deploy_files_without_blocking(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "deploy").mkdir(exist_ok=True)
    _commit_change(repo, "deploy/hidrive-lite-re0-sync.service", "[Service]\nExecStart=/bin/true\n")
    _commit_change(repo, "deploy/hidrive-lite-re0-sync.timer", "[Timer]\nOnCalendar=hourly\n")
    _commit_change(repo, "app.py", "print('release')\n")
    _write_checks(repo)

    blocked = mrm.build_manifest(repo, base=base, bridge_command="x")
    assert blocked["release_allowed"] is False
    assert sum("needs a manual release" in r for r in blocked["blocking_reasons"]) == 2

    partial = mrm.build_manifest(repo, base=base, bridge_command="x", manual_release=["deploy/hidrive-lite-re0-sync.service"])
    assert sum("needs a manual release" in r for r in partial["blocking_reasons"]) == 1
    assert [m["path"] for m in partial["manual_release_files"]] == ["deploy/hidrive-lite-re0-sync.service"]

    acked = mrm.build_manifest(repo, base=base, bridge_command="x",
                               manual_release=["deploy/hidrive-lite-re0-sync.service", "deploy/hidrive-lite-re0-sync.timer"])
    assert not any("needs a manual release" in r for r in acked["blocking_reasons"])
    assert [m["path"] for m in acked["manual_release_files"]] == ["deploy/hidrive-lite-re0-sync.service", "deploy/hidrive-lite-re0-sync.timer"]
    assert all(len(m["sha256"]) == 64 for m in acked["manual_release_files"])
    assert all("deploy/" not in f["path"] for f in acked["files"])  # never shipped by the bridge


def test_manual_release_cli_flag_is_repeatable(repo, monkeypatch, capsys):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "deploy").mkdir(exist_ok=True)
    _commit_change(repo, "deploy/hidrive-lite-re0-sync.timer", "[Timer]\nOnCalendar=hourly\n")
    _commit_change(repo, "app.py", "print('release')\n")
    _write_checks(repo)
    out = repo / "m.json"
    mrm.main(["--root", str(repo), "--base", base, "--bridge-command", "x", "--output", str(out),
              "--manual-release", "deploy/hidrive-lite-re0-sync.timer"])
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["manual_release_files"][0]["path"] == "deploy/hidrive-lite-re0-sync.timer"
    assert not any("needs a manual release" in r for r in manifest["blocking_reasons"])


# ---------------------------------------------------------------------------
# Review 2026-09-12 R15: a change to the security core always needs a human
# ---------------------------------------------------------------------------


AUTH_SERVICE_SRC = '''"""Local accounts."""


def verify_password(password_hash, password):
    return bool(password_hash) and check_password_hash(password_hash, password)


def capabilities(*, role, allow_member_re0_unlock, has_115_cookie, has_115_open):
    admin = role == "admin"
    return {"openlist": admin, "re0_unlock": admin or bool(allow_member_re0_unlock)}
'''

USER_115_SRC = '''"""Per-user 115 credentials."""


def credentials_for(db, user_id, *, fernet):
    return db.execute("SELECT 1 FROM user_secret WHERE user_id=?", (user_id,)).fetchone()
'''


@pytest.fixture
def auth_repo(repo):
    """A baseline that already carries the two security-core modules."""
    (repo / "auth_service.py").write_text(AUTH_SERVICE_SRC)
    (repo / "user_115.py").write_text(USER_115_SRC)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "security core")
    return repo


def _hunks_for(repo, base):
    """`build_manifest` reads HEAD itself; the caller only names the base."""
    report = mrm.build_manifest(repo, base=base, bridge_command="definitely-not-installed-xyz")
    return report["sensitive_hunks"], report


class TestSecurityCoreNeedsApproval:
    def test_weakening_a_password_check_is_flagged(self, auth_repo):
        """Codex's counter-example: a one-line edit that makes every password
        verify. It classified as `release` with no sensitive hunk before."""
        base = _git(auth_repo, "rev-parse", "HEAD")
        _commit_change(auth_repo, "auth_service.py",
                              AUTH_SERVICE_SRC.replace(
                                  "return bool(password_hash) and check_password_hash(password_hash, password)",
                                  "return True"),
                              "weaken")
        hunks, report = _hunks_for(auth_repo, base)
        assert [h["path"] for h in hunks] == ["auth_service.py"]
        assert report["release_allowed"] is False
        assert any("auth_service.py" in reason for reason in report["blocking_reasons"])

    def test_a_role_test_change_is_flagged(self, auth_repo):
        base = _git(auth_repo, "rev-parse", "HEAD")
        _commit_change(auth_repo, "auth_service.py",
                              AUTH_SERVICE_SRC.replace('"openlist": admin', '"openlist": True'),
                              "widen")
        hunks, _report = _hunks_for(auth_repo, base)
        assert [h["path"] for h in hunks] == ["auth_service.py"]

    def test_a_credential_ownership_change_is_flagged(self, auth_repo):
        base = _git(auth_repo, "rev-parse", "HEAD")
        _commit_change(auth_repo, "user_115.py",
                              USER_115_SRC.replace("WHERE user_id=?", "WHERE user_id=? OR 1=1"),
                              "leak")
        hunks, _report = _hunks_for(auth_repo, base)
        assert [h["path"] for h in hunks] == ["user_115.py"]

    def test_even_a_comment_in_those_modules_is_flagged(self, auth_repo):
        """The module is the boundary; which lines moved is not the question.
        A reviewer waving through a comment costs one click."""
        base = _git(auth_repo, "rev-parse", "HEAD")
        _commit_change(auth_repo, "auth_service.py",
                              AUTH_SERVICE_SRC + "\n# a note\n", "comment")
        hunks, _report = _hunks_for(auth_repo, base)
        assert len(hunks) == 1 and hunks[0]["context"] == "whole module"

    def test_an_ordinary_style_change_is_not_dragged_in(self, auth_repo):
        """R15 asks not to widen approval indiscriminately: a stylesheet is
        still an ordinary release."""
        (auth_repo / "static").mkdir(exist_ok=True)
        (auth_repo / "static" / "app.css").write_text(".x{color:red}\n")
        _git(auth_repo, "add", ".")
        _git(auth_repo, "commit", "-q", "-m", "css baseline")
        base = _git(auth_repo, "rev-parse", "HEAD")
        _commit_change(auth_repo, "static/app.css", ".x{color:blue}\n", "restyle")
        hunks, _report = _hunks_for(auth_repo, base)
        assert hunks == []

    def test_an_approval_lets_it_through_as_before(self, auth_repo):
        base = _git(auth_repo, "rev-parse", "HEAD")
        _commit_change(auth_repo, "auth_service.py",
                              AUTH_SERVICE_SRC + "\n# reviewed\n", "note")
        report = mrm.build_manifest(auth_repo, base=base, bridge_command="x",
                                    approved="reviewed by the owner")
        assert report["approval"] is not None
        assert not [r for r in report["blocking_reasons"] if "auth_service.py" in r]

    def test_the_modules_named_here_are_the_ones_that_exist(self):
        for module in mrm.SENSITIVE_MODULES:
            assert (ROOT / module).exists(), module
            assert module in mrm.RELEASE_FILES, module
