"""Tests for scripts/scan_secrets.py.  Secret-looking fixtures are assembled
at runtime so this file itself stays clean under the scanner."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scan_secrets  # noqa: E402

PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow" + "A" * 40 + "\n-----END " + "RSA PRIVATE KEY-----"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcDEF123_-" * 3
COOKIE_115 = "UID=" + "1234567_A1_1700000000" + "; CID=" + "0123456789abcdef" * 2 + "; SEID=" + "f" * 120


def rules(text: str, path: str = "src/sample.py", allow=()) -> list[str]:
    return sorted({finding.rule for finding in scan_secrets.scan_text(text, path, list(allow))})


def test_detects_private_key_block():
    assert rules(PEM) == ["private-key"]


def test_detects_jwt():
    assert "jwt" in rules("token = " + repr(JWT))


def test_detects_115_cookie_material():
    assert "cookie-115" in rules("Cookie: " + COOKIE_115)


def test_detects_bearer_and_cookie_headers():
    assert "bearer-token" in rules("Authorization: Bearer " + "Zx9" * 12)
    assert "cookie-header" in rules("Cookie: " + "session=" + "q" * 40)


def test_detects_credential_assignment_with_real_looking_value():
    assert rules('HDHIVE_APP_SECRET = "' + "k7" * 12 + '"') == ["credential-assignment"]
    assert "credential-assignment" in rules("tmdb_api_key: " + "9f" * 16)
    assert "credential-assignment" in rules("password=" + "Pw" * 10)


@pytest.mark.parametrize(
    "line",
    [
        'api_key = "..."',
        "X-API-Key: app-secret",
        'secret = "<your-app-secret>"',
        "token = ${HDHIVE_TOKEN}",
        'password = "{{ password }}"',
        'api_key = "your-api-key-here"',
        'secret = "changeme"',
        'cookie = os.getenv("ENV_115_COOKIES")',
        'api_key = config_value("hdhive_app_secret", "HDHIVE_APP_SECRET")',
        'headers = {"X-API-Key": api_key or ""}',
        '"token_configured": bool(config_value("openlist_token", "OPENLIST_TOKEN"))',
        "secret_get(name) != value",
    ],
)
def test_ignores_placeholders_and_code_references(line):
    assert rules(line) == []


def test_sha256_digests_are_not_secrets():
    assert rules("sha256: " + "e4" * 32) == []
    assert rules("ACCESS_AUDIENCE=" + "5a" * 32) == []


def test_findings_mask_the_matched_value():
    (finding,) = scan_secrets.scan_text('api_key = "' + "k7" * 12 + '"', "x.py", [])
    assert "k7k7k7k7k7k7k7k7" not in finding.snippet
    assert finding.line == 1
    assert finding.path == "x.py"


def test_glob_scoped_allowlist_only_applies_to_matching_files():
    allow = scan_secrets.parse_allowlist(["docs/*.md :: X-API-Key: \\S+", "# comment", "", 'refresh_token":"\\.\\.\\."'])
    hot = "X-API-Key: " + "Q1" * 12
    assert rules(hot, "docs/api.md", allow) == []
    assert rules(hot, "app.py", allow) == ["credential-assignment"]
    assert rules('{"refresh_token":"..."}', "app.py", allow) == []


@pytest.mark.parametrize(
    "name,flagged",
    [
        (".env", True),
        ("hidrive-lite.env", True),
        (".env.local", True),
        ("hidrive.db", True),
        ("hidrive.db-wal", True),
        ("data.sqlite", True),
        ("master.key", True),
        ("server.pem", True),
        ("115.cookie", True),
        ("hdhive.token", True),
        ("service.log", True),
        ("hidrive-lite.env.template", False),
        (".env.example", False),
        ("app.py", False),
        ("requirements.txt", False),
        ("report.xlsx", True),
        ("sheet.xls", True),
    ],
)
def test_forbidden_filenames(tmp_path, name, flagged):
    target = tmp_path / name
    target.write_text("plain\n")
    found = [f.rule for f in scan_secrets.scan_path(target, tmp_path, [])]
    assert ("forbidden-file" in found) is flagged, name


def test_symlinks_are_reported_and_not_followed(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "real.txt").write_text("api_key = " + '"' + "k7" * 12 + '"')
    link = tmp_path / "link.txt"
    os.symlink(outside / "real.txt", link)
    found = scan_secrets.scan_path(link, tmp_path, [])
    assert [f.rule for f in found] == ["symlink"]


def test_binary_content_is_skipped_but_named_files_still_flagged(tmp_path):
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\x00\x01" + PEM.encode())
    assert scan_secrets.scan_path(blob, tmp_path, []) == []


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.test")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text("build/\n*.db\n.venv/\n")
    (tmp_path / "app.py").write_text("print('ok')\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_collect_targets_covers_tracked_untracked_and_artifacts(repo):
    (repo / "new_untracked.py").write_text("x = 1\n")
    (repo / "ignored.db").write_bytes(b"\x00")
    (repo / "build" / "test-artifacts").mkdir(parents=True)
    (repo / "build" / "test-artifacts" / "junit.xml").write_text("<testsuite/>")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "lib.py").write_text("x = 1\n")
    (repo / "release-manifest-abc.json").write_text("{}")

    targets = {str(p.relative_to(repo)) for p in scan_secrets.collect_targets(repo)}

    assert {"app.py", ".gitignore", "new_untracked.py", "build/test-artifacts/junit.xml", "release-manifest-abc.json"} <= targets
    assert "ignored.db" not in targets
    assert ".venv/lib.py" not in targets


def test_main_exit_codes(repo, capsys):
    assert scan_secrets.main(["--root", str(repo)]) == 0
    (repo / "leak.txt").write_text("Authorization: Bearer " + "Zx9" * 12 + "\n")
    assert scan_secrets.main(["--root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "leak.txt:1" in out and "bearer-token" in out
    assert "Zx9Zx9Zx9Zx9" not in out


def test_main_writes_json_report(repo):
    report = repo / "build" / "secret-scan.json"
    assert scan_secrets.main(["--root", str(repo), "--report", str(report)]) == 0
    import json

    data = json.loads(report.read_text())
    assert data["status"] == "passed" and data["findings"] == [] and data["scanned_files"] >= 2


def test_main_refuses_targets_outside_root(repo, tmp_path, capsys):
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.txt").write_text("x")
    assert scan_secrets.main(["--root", str(repo), str(elsewhere / "x.txt")]) == 2
    assert "outside" in capsys.readouterr().err


def test_project_allowlist_file_parses():
    allow = scan_secrets.load_allowlist(ROOT / "scripts" / "secret-scan-allowlist.txt")
    assert allow, "the project allowlist must contain at least the API-doc placeholders"


def test_ignores_sql_upsert_and_identifier_like_values():
    assert rules("access_token=excluded.access_token, refresh_token=excluded.refresh_token") == []
    assert rules("token = settings.hdhive.access_token") == []


def test_detects_private_deployment_markers():
    private_domain = "drive." + "seasonai." + "cloud"
    private_path = "/data-disk/" + "HiDrive-Lite/data"
    assert "private-domain" in rules(private_domain)
    assert "private-path" in rules(private_path)


def test_git_history_scan_detects_old_blob(repo):
    (repo / "old.txt").write_text("origin=https://" + "seasonai." + "cloud\n")
    _git(repo, "add", "old.txt")
    _git(repo, "commit", "-q", "-m", "old")
    found = scan_secrets.scan_git_history(repo, [])
    assert any(item.rule == "private-domain" for item in found)


def test_collect_targets_skips_local_index(repo):
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + ".local-index/\n")
    (repo / ".local-index").mkdir()
    (repo / ".local-index" / "library-bundle.sqlite").write_text("not a real database\n")

    targets = {str(p.relative_to(repo)) for p in scan_secrets.collect_targets(repo)}

    assert ".local-index/library-bundle.sqlite" not in targets
    assert scan_secrets.main(["--root", str(repo)]) == 0


def test_reference_workbooks_are_ignored_by_git():
    for path in ("private-inputs/source.xlsx", ".local-index/x", "private-matching-artifacts/x.jsonl"):
        result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path])
        assert result.returncode == 0, path
