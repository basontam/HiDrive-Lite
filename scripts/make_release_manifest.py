#!/usr/bin/env python3
"""Build the HiDrive-Lite release manifest consumed by the controlled release
bridge (see docs/release-bridge-contract.md).

The manifest records the commit, SHA-256 of every releasable file (plus the
hash at the base commit so the bridge can detect production drift), the
classification of every changed file, sensitive hunks in application code,
the results of the syntax/test/secret-scan gates and whether an automatic
release is allowed.  Exit status 0 means the manifest is releasable, 1 means
it was written but the release gate is closed.  Nothing here touches
production paths or the bridge itself.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE_COMMAND = "hidrive-lite-release"
ARTIFACT_DIR = Path("build") / "test-artifacts"

RELEASE_FILES = (
    "app.py",
    "sync_openlist_115.py",
    "library_normalize.py",
    "library_store.py",
    "library_search.py",
    "library_tmdb.py",
    "re0_sync.py",
    "templates/index.html",
    "static/app.css",
    "static/app.js",
    "static/icons.svg",
    # Brand assets (2026-09-11). The host bridge mirrors these nine paths and
    # creates static/branding/hidrive-lite/ on first release; it reads, backs
    # up, hashes and restores the PNGs as raw bytes (limits: 2 MiB per file,
    # 16 MiB per release -- this set is ~135 KiB).
    "static/branding/hidrive-lite/hidrive-lite-logo.svg",
    "static/branding/hidrive-lite/hidrive-lite-logo-on-dark.svg",
    "static/branding/hidrive-lite/hidrive-lite-mark.svg",
    "static/branding/hidrive-lite/favicon.svg",
    "static/branding/hidrive-lite/favicon-16.png",
    "static/branding/hidrive-lite/favicon-32.png",
    "static/branding/hidrive-lite/favicon-48.png",
    "static/branding/hidrive-lite/favicon-512.png",
    "static/branding/hidrive-lite/apple-touch-icon.png",
    # Multi-user Phase 8. Two modules and the sign-in template; the bridge
    # mirrors these three and its cap moved 20 -> 24 at the same time.
    # app.py imports both at module level, so all three must ship together
    # with app.py in one manifest.
    "auth_service.py",
    "user_115.py",
    "templates/login.html",
    # Step-B device QR is encoded locally; ship with index.html and app.js.
    "static/vendor/qrcode.js",
)
RELEASE_GLOBS = ("templates/*", "static/*")
BLOCKED_GLOBS = ("requirements.txt", "deploy/*", "*.service", "*.timer", "*.env", "*.env.*", "cloudflared*")
SUPPORT_GLOBS = (
    "docs/*",
    "docs/*/*",
    "tests/*",
    "tests/*/*",
    "scripts/*",
    "README.md",
    "CLAUDE.md",
    "DEPLOYMENT.md",
    ".gitignore",
    ".gitattributes",
    "pytest.ini",
    "requirements-dev.txt",
    "NOTICE",
)
# Modules that are the authorisation and credential core. Every change to
# one needs a human, without exception: a hunk-level heuristic cannot be
# relied on to notice a one-character edit to a password comparison or a
# role test (R15, 2026-09-12 review). Being on RELEASE_FILES only means a
# path *can* be shipped; it never means it may ship unattended.
SENSITIVE_MODULES = ("auth_service.py", "user_115.py")

SENSITIVE_FUNCTIONS = {
    "connect_db",
    "init_db",
    "load_fernet",
    "secret_get",
    "secret_set",
    "verify_access_jwt",
    "require_access",
    "csrf_token",
    "check_csrf",
    "config_value",
    "hdhive_headers",
    "get_tokens",
    "save_tokens",
    "decrypt_token",
    "refresh_hdhive_token",
    "valid_hdhive_access_token",
    "sync_openlist_credentials_from_source",
    "verify_115_session",
    "remember_115_cookie_check",
    "cookie_status",
    "_reauth_complete",
    "api_115_reauth_start",
    "api_115_reauth_status",
    "api_115_reauth_cancel",
    "api_115_reauth_qr",
    "before_request",
    "handle_error",
    "oauth_start",
    "oauth_callback",
    "api_settings",
    # Multi-user: authorisation, sessions, per-user credentials, the member
    # policy and the migration entry point.
    "authorize_request",
    "current_user",
    "require_login",
    "require_role",
    "_load_auth_session",
    "_admin_user_row",
    "_principal_is_admin",
    "_acting_user",
    "auth_google",
    "_safe_next",
    "api_auth_login",
    "api_auth_register",
    "api_auth_logout",
    "api_me",
    "api_admin_users",
    "api_admin_user_approve",
    "api_admin_user_reject",
    "api_admin_user_disable",
    "_admin_user_action",
    "api_admin_policy_re0",
    "allow_member_re0_unlock",
    "member_unlock_refused",
    "run_auth_migrate",
    "user_115_cookie",
    "user_115_open_token",
    "_deployment_115",
    "_require_open115",
    "_115_setting",
    "csrf_key",
    "csrf_subject",
    "_open115_config",
    "_open115_device_blocked",
    "_open115_flow_error",
    "api_me_115_open_status",
    "api_me_115_open_cancel",
    "api_me_115_open_start",
    "api_me_115_disconnect",
    "api_me_115_status",
    "_user_115_state",
    "_session_cookie",
}
SENSITIVE_KEYWORDS = re.compile(
    r"secret_get|secret_set|load_fernet|save_tokens|decrypt_token|refresh_hdhive_token|config_value\(|verify_access_jwt|"
    r"check_csrf|csrf_token|require_access|Fernet|\bjwt\.|ENV_115_COOKIES|115_cookie|openlist_token|115_open_access_token|"
    r"115_open_refresh_token|hdhive_app_secret|hdhive_client_id|tmdb_api_key|MASTER_KEY|DATA_DIR|DB_PATH|OPENLIST_DB|"
    r"STRM_ROOT|AUTH_MODE|ACCESS_TEAM_DOMAIN|ACCESS_AUDIENCE|PUBLIC_ORIGIN|os\.getenv|os\.environ|CREATE TABLE|ALTER TABLE|"
    r"DROP TABLE|executescript|\"Cookie\"|Cf-Access|X-CSRF-Token|/data-disk|/etc/|"
    # Multi-user core: anything naming the auth modules, a password or
    # session primitive, the administrator address, or the member policy.
    r"auth_service\.|user_115\.|password_hash|check_password_hash|generate_password_hash|scrypt|"
    r"issue_session|load_session|revoke_session|rotate_session|hash_token|SESSION_COOKIE|"
    r"ADMIN_EMAIL|allow_member_re0_unlock|MEMBER_ENDPOINTS|ADMIN_ENDPOINTS|PUBLIC_ENDPOINTS|"
    r"user_secret|auth_user|auth_session|CurrentUser"
)
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)$")
DEF_NAME = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def classify(path: str) -> str:
    if path in RELEASE_FILES:
        return "release"
    if any(fnmatch.fnmatch(path, glob) for glob in RELEASE_GLOBS):
        return "release"
    if any(fnmatch.fnmatch(path, glob) for glob in BLOCKED_GLOBS):
        return "blocked"
    if any(fnmatch.fnmatch(path, glob) for glob in SUPPORT_GLOBS):
        return "support"
    return "review"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_at(root: Path, commit: str, path: str) -> bytes | None:
    result = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{path}"], capture_output=True)
    return result.stdout if result.returncode == 0 else None


def sensitive_hunks(root: Path, base: str, head: str, path: str) -> list[dict]:
    diff = _git(root, "diff", "-U0", base, head, "--", path)
    hunks: list[dict] = []
    context = ""
    changed: list[str] = []

    def flush() -> None:
        if not context and not changed:
            return
        name_match = DEF_NAME.match(context)
        if name_match and name_match.group(1) in SENSITIVE_FUNCTIONS:
            hunks.append({"path": path, "context": context, "reason": "function is on the sensitive list"})
            return
        for line in changed:
            keyword = SENSITIVE_KEYWORDS.search(line)
            if keyword:
                hunks.append({"path": path, "context": context, "reason": f"changed lines mention {keyword.group(0)}"})
                return

    for line in diff.splitlines():
        header = HUNK_HEADER.match(line)
        if header:
            flush()
            context = header.group(1).strip()
            changed = []
        elif line[:1] in {"+", "-"} and not line.startswith(("+++", "---")):
            changed.append(line[1:])
    flush()
    return hunks


def read_junit(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing"}
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return {"status": "failed", "error": "junit report is not valid XML"}
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    suites = [tree.getroot()] if tree.getroot().tag == "testsuite" else tree.getroot().iter("testsuite")
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, 0))
    passed = totals["tests"] > 0 and totals["failures"] == 0 and totals["errors"] == 0
    return {"status": "passed" if passed else "failed", **totals}


def read_secret_scan(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "failed", "error": "secret scan report unreadable"}
    return {"status": data.get("status", "failed"), "findings": len(data.get("findings", [])), "scanned_files": data.get("scanned_files")}


def read_syntax(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing"}
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"status": "passed" if lines and lines[-1].strip().endswith("OK") else "failed"}


def find_bridge(command: str) -> str | None:
    """Locate the bridge client on PATH, falling back to ~/.local/bin where the
    container installs it (that directory is not on PATH in non-login shells)."""
    return shutil.which(command) or shutil.which(command, path=str(Path.home() / ".local" / "bin"))


def probe_capabilities(bridge_path: str) -> list:
    """Ask the bridge client what it can do.  Any failure (missing binary,
    timeout, non-zero exit, output that is not a JSON object with a
    ``capabilities`` list) is treated as "no capabilities", never an error."""
    try:
        result = subprocess.run([bridge_path, "status", "--json"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    capabilities = data.get("capabilities") if isinstance(data, dict) else None
    return capabilities if isinstance(capabilities, list) else []


def _validate_bundle(root: Path, bundle: Path) -> Path:
    """Resolve and validate a --bundle path: must be inside root, a regular
    file, and not a symlink.  Raises ValueError otherwise."""
    candidate = bundle if bundle.is_absolute() else root / bundle
    if candidate.is_symlink():
        raise ValueError(f"bundle path {candidate} must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"bundle path {candidate} does not exist") from exc
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"bundle path {candidate} is outside the repository root {root}") from None
    if not resolved.is_file():
        raise ValueError(f"bundle path {candidate} is not a regular file")
    return resolved


def _read_schema_version(path: Path):
    """Best-effort read of schema_meta.schema_version from a bundle sqlite
    file.  Any failure (not a database, missing table/row) yields None."""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return row[0]


def build_manifest(
    root: Path,
    base: str,
    bridge_command: str = DEFAULT_BRIDGE_COMMAND,
    approved: str | None = None,
    bundle: Path | None = None,
    manual_release: list[str] | None = None,
) -> dict:
    root = root.resolve()
    head = _git(root, "rev-parse", "HEAD").strip()
    base_commit = _git(root, "rev-parse", base).strip()
    tree_clean = _git(root, "status", "--porcelain").strip() == ""

    files = []
    missing_release_files: list[str] = []
    for path in RELEASE_FILES:
        file_path = root / path
        previous = _blob_at(root, base_commit, path)
        if not file_path.is_file():
            # Only a genuine regression (the file existed at the base
            # commit and is gone now) is worth blocking on -- a RELEASE_FILES
            # entry that was never present in this comparison range at all
            # (e.g. a minimal throwaway repo in a test) isn't something
            # this diff "removed".
            if previous is not None:
                missing_release_files.append(path)
            continue
        data = file_path.read_bytes()
        files.append(
            {
                "path": path,
                "sha256": _sha256(data),
                "size": len(data),
                "previous_sha256": _sha256(previous) if previous is not None else None,
                "changed": previous is None or _sha256(previous) != _sha256(data),
            }
        )

    changed_paths = [line for line in _git(root, "diff", "--name-only", base_commit, head).splitlines() if line]
    changed_files = [{"path": path, "class": classify(path)} for path in changed_paths]
    hunks: list[dict] = []
    for path in changed_paths:
        if classify(path) != "release":
            continue
        if path in SENSITIVE_MODULES:
            # The whole module is the security boundary; which lines moved is
            # not the question (R15).
            hunks.append({
                "path": path, "context": "whole module",
                "reason": "security-core module: every change needs approval",
            })
            continue
        if path.endswith(".py"):
            hunks.extend(sensitive_hunks(root, base_commit, head, path))

    report_dir = root / ARTIFACT_DIR
    checks = {
        "syntax": {**read_syntax(report_dir / "syntax.txt"), "report": (ARTIFACT_DIR / "syntax.txt").as_posix()},
        "tests": {**read_junit(report_dir / "junit.xml"), "report": (ARTIFACT_DIR / "junit.xml").as_posix()},
        "secret_scan": {**read_secret_scan(report_dir / "secret-scan.json"), "report": (ARTIFACT_DIR / "secret-scan.json").as_posix()},
    }

    approval = None
    if approved is not None:
        approval = {"by": "user", "reason": approved, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    artifacts: list[dict] = []
    if bundle is not None:
        bundle_path = _validate_bundle(root, bundle)
        data = bundle_path.read_bytes()
        artifacts.append(
            {
                "kind": "library-bundle",
                "path": bundle_path.relative_to(root).as_posix(),
                "sha256": _sha256(data),
                "size": len(data),
                "schema_version": _read_schema_version(bundle_path),
            }
        )

    bridge_path = find_bridge(bridge_command)
    available = bridge_path is not None
    capabilities = probe_capabilities(bridge_path) if bridge_path else []

    reasons: list[str] = []
    manual_release_files: list[dict] = []
    acknowledged = {p.strip() for p in (manual_release or []) if p and p.strip()}
    if not tree_clean:
        reasons.append("working tree has uncommitted changes")
    for name, check in checks.items():
        if check["status"] != "passed":
            reasons.append(f"{name} check is {check['status']}")
    for entry in changed_files:
        if entry["class"] == "blocked":
            if entry["path"] in acknowledged:
                # Deployment/dependency files never ship through the bridge;
                # an explicit acknowledgement records that they are installed
                # by hand (host side) instead of blocking the app release.
                blob = _blob_at(root, head, entry["path"]) or b""
                manual_release_files.append({"path": entry["path"], "sha256": _sha256(blob), "note": "installed manually on the host"})
            else:
                reasons.append(f"{entry['path']} is a deployment/dependency file and needs a manual release")
        elif entry["class"] == "review":
            reasons.append(f"{entry['path']} is not on the release allowlist and needs review")
        elif entry["class"] == "release" and entry["path"] not in RELEASE_FILES:
            reasons.append(
                f"{entry['path']} matches a release path pattern (templates/static) but is not listed in "
                "RELEASE_FILES -- add it to the allowlist, or the release bridge would not ship it"
            )
    for path in missing_release_files:
        reasons.append(f"{path} is listed in RELEASE_FILES and existed at the base commit, but is missing now")
    if approval is None:
        for hunk in hunks:
            reasons.append(f"{hunk['path']} touches sensitive code ({hunk['context'] or 'module level'}): {hunk['reason']}")
    if not any(entry["class"] == "release" for entry in changed_files):
        reasons.append("no release-class file changed since the base commit")
    if artifacts and "artifacts" not in capabilities:
        reasons.append("bridge lacks the artifacts capability")
    if approval is not None and "approval" not in capabilities:
        reasons.append("bridge lacks the approval capability")

    return {
        "manifest_version": 1,
        "project": "HiDrive-Lite",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workspace": str(root),
        "commit": head,
        "base_commit": base_commit,
        "tree_clean": tree_clean,
        "files": files,
        "changed_files": changed_files,
        "sensitive_hunks": hunks,
        "checks": checks,
        "approval": approval,
        "artifacts": artifacts,
        "manual_release_files": manual_release_files,
        "release_allowed": not reasons,
        "blocking_reasons": reasons,
        "bridge": {
            "command": bridge_command,
            "status": "available" if available else "unavailable",
            "action": "submit" if available else "manifest_only",
            "capabilities": capabilities,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="repository root (default: this working copy)")
    parser.add_argument("--base", default="HEAD~1", help="commit the release is compared against (default: HEAD~1)")
    parser.add_argument("--bridge-command", default=DEFAULT_BRIDGE_COMMAND, help="release bridge executable to look for on PATH")
    parser.add_argument("--output", default=None, help="manifest path (default: <root>/release-manifest-<commit12>.json)")
    parser.add_argument("--approved", default=None, help="record a manual approval reason; suppresses the sensitive-hunks block")
    parser.add_argument("--bundle", default=None, help="library bundle sqlite file to record as a release artifact")
    parser.add_argument("--manual-release", action="append", default=[], metavar="PATH",
                        help="acknowledge a deployment/dependency file (deploy/*, *.service, *.timer) as installed manually; repeatable")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    bundle = Path(args.bundle) if args.bundle else None
    try:
        manifest = build_manifest(root, base=args.base, bridge_command=args.bridge_command, approved=args.approved, bundle=bundle,
                                  manual_release=args.manual_release)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    output = Path(args.output) if args.output else root / f"release-manifest-{manifest['commit'][:12]}.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"release manifest written to {output}")
    print(f"release_allowed: {manifest['release_allowed']}")
    for reason in manifest["blocking_reasons"]:
        print(f"  - {reason}")
    print(f"bridge: {manifest['bridge']['status']} -> {manifest['bridge']['action']}")
    print(f"artifacts: {len(manifest['artifacts'])}")
    print(f"approval: {'yes' if manifest['approval'] else 'no'}")
    return 0 if manifest["release_allowed"] else 1


if __name__ == "__main__":
    sys.exit(main())
