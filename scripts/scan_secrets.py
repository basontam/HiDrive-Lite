#!/usr/bin/env python3
"""Repeatable secret scan for a HiDrive-Lite source tree.

Scans committed files, untracked-but-not-ignored files, and test artifacts for
credential material, private deployment markers, forbidden file types
(databases, env files, keys, cookies, logs) and symlinks. ``--git-history``
also scans every reachable blob in the local repository. Exit status 1 means at
least one finding; 2 means a usage error. Targets must live inside the
repository root: the scanner never reads production paths.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = DEFAULT_ROOT / "scripts" / "secret-scan-allowlist.txt"
_PRIVATE_DATA_ROOT = "/" + "data-disk/HiDrive-Lite"
_PRIVATE_CONFIG_ROOT = "/" + "etc/hidrive-lite"
_PRIVATE_WORKSPACE_ROOT = "/" + "opt/claude-code"
FORBIDDEN_ROOTS = (Path(_PRIVATE_DATA_ROOT), Path(_PRIVATE_CONFIG_ROOT), Path("/etc/openlist"))
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest-tmp", ".local-index"}
ARTIFACT_DIRS = ("build",)
ARTIFACT_GLOBS = ("release-manifest*.json",)
MAX_CONTENT_BYTES = 8 * 1024 * 1024

FORBIDDEN_NAMES = [
    re.compile(r"^\.env$"),
    re.compile(r"^\.env\..+$"),
    re.compile(r".+\.env$"),
    re.compile(r".+\.(db|db-wal|db-shm|db-journal|sqlite|sqlite3|sqlite-wal|sqlite-shm)$"),
    re.compile(r".+\.(key|pem|p12|pfx|jks|keystore)$"),
    re.compile(r".+\.(cookie|cookies|token)$"),
    re.compile(r".+\.log$"),
    re.compile(r"^master\.key$"),
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r".+\.xlsx?$"),
]
FORBIDDEN_NAME_EXCEPTIONS = [re.compile(r"^(.+\.)?\.?env\.(template|example|sample)$")]

CREDENTIAL_KEYWORDS = r"api[_-]?key|apikey|secret|token|passwd|password|pwd|cookie|authorization|auth[_-]?key|private[_-]?key|access[_-]?key|master[_-]?key"
CONTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("cookie-115", re.compile(r"\bUID=\d{3,}_[A-Z]\d*_\d{6,}|\bSEID=[0-9a-f]{60,}|\bCID=[0-9a-f]{32}\b")),
    ("cloudflare-access-cookie", re.compile(r"\bCF_Authorization=[A-Za-z0-9_\-.]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer-token", re.compile(r"\bBearer\s+([A-Za-z0-9_\-.=+/]{20,})")),
    ("cookie-header", re.compile(r"(?i)\bcookie:\s*(\S{30,})")),
    ("fernet-key", re.compile(r"(?<![A-Za-z0-9_\-+/=])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_\-=])")),
    (
        "credential-assignment",
        re.compile(
            r"(?i)\b[A-Za-z0-9_.-]*(?:" + CREDENTIAL_KEYWORDS + r")[A-Za-z0-9_.-]*[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-./+=]{12,})(?=[\"'\s,;})\]]|$)"
        ),
    ),
    ("private-domain", re.compile(r"(?i)\b(?:[a-z0-9-]+\.)*seasonai\.cloud\b")),
    (
        "private-path",
        re.compile(
            r"/(?:"
            + re.escape(_PRIVATE_DATA_ROOT.lstrip("/"))
            + "|"
            + re.escape(_PRIVATE_CONFIG_ROOT.lstrip("/"))
            + "|"
            + re.escape(_PRIVATE_WORKSPACE_ROOT.lstrip("/"))
            + r")(?:/|\b)"
        ),
    ),
    ("private-access-tenant", re.compile(r"(?i)\b[a-z0-9-]+\.cloudflareaccess\.com\b")),
]
PLACEHOLDER_VALUE = re.compile(
    r"^(?:\.{3,}|x{4,}|\*{4,}|your[-_ ].*|.*[-_]here|change[-_]?me|app-secret|placeholder|redacted|none|null|true|false|/.*|\./.*|https?://.*)$"
    r"|fixture|dummy|fake|placeholder|example|sample|for-tests|not-real|marker",
    re.I,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    snippet: str


@dataclass(frozen=True)
class AllowRule:
    glob: str | None
    pattern: re.Pattern[str]

    def applies(self, path: str) -> bool:
        return self.glob is None or fnmatch.fnmatch(path, self.glob)


def parse_allowlist(lines) -> list[AllowRule]:
    rules: list[AllowRule] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " :: " in line:
            glob, pattern = line.split(" :: ", 1)
            rules.append(AllowRule(glob.strip(), re.compile(pattern.strip())))
        else:
            rules.append(AllowRule(None, re.compile(line)))
    return rules


def load_allowlist(path: Path) -> list[AllowRule]:
    if not path.exists():
        return []
    return parse_allowlist(path.read_text(encoding="utf-8").splitlines())


def _mask(value: str) -> str:
    return f"{value[:4]}…({len(value)} chars)" if len(value) > 6 else "…"


IDENTIFIER_LIKE = re.compile(r"^[a-z_][a-z_.]*$")


def _is_placeholder(value: str) -> bool:
    # Lower-case dotted identifiers (SQL ``excluded.col``, attribute paths)
    # carry no entropy and are code, not credentials.
    return PLACEHOLDER_VALUE.search(value) is not None or IDENTIFIER_LIKE.match(value) is not None


def scan_text(text: str, path: str, allow: list[AllowRule]) -> list[Finding]:
    findings: list[Finding] = []
    active = [rule for rule in allow if rule.applies(path)]
    for number, line in enumerate(text.splitlines(), start=1):
        if any(rule.pattern.search(line) for rule in active):
            continue
        for name, pattern in CONTENT_RULES:
            match = pattern.search(line)
            if not match:
                continue
            value = match.group(1) if match.groups() else match.group(0)
            if name in {"credential-assignment", "bearer-token", "cookie-header"} and _is_placeholder(value):
                continue
            findings.append(Finding(path, number, name, _mask(value)))
    return findings


def _forbidden_name(name: str) -> bool:
    if any(pattern.match(name) for pattern in FORBIDDEN_NAME_EXCEPTIONS):
        return False
    return any(pattern.match(name) for pattern in FORBIDDEN_NAMES)


def scan_path(path: Path, root: Path, allow: list[AllowRule]) -> list[Finding]:
    rel = path.relative_to(root).as_posix()
    if path.is_symlink():
        return [Finding(rel, 0, "symlink", "symbolic links are not allowed in the release tree")]
    findings: list[Finding] = []
    if _forbidden_name(path.name):
        findings.append(Finding(rel, 0, "forbidden-file", "file type must never be committed or shipped"))
    if not path.is_file() or path.stat().st_size > MAX_CONTENT_BYTES:
        return findings
    data = path.read_bytes()
    if b"\x00" in data[:8000]:
        return findings
    findings.extend(scan_text(data.decode("utf-8", errors="replace"), rel, allow))
    return findings


def _walk(directory: Path):
    for item in sorted(directory.rglob("*")):
        if any(part in SKIP_DIRS for part in item.relative_to(directory).parts):
            continue
        if item.is_symlink() or item.is_file():
            yield item


def collect_targets(root: Path) -> list[Path]:
    targets: set[Path] = set()
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    ).stdout
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        candidate = root / raw.decode("utf-8", errors="surrogateescape")
        if any(part in SKIP_DIRS for part in candidate.relative_to(root).parts):
            continue
        if candidate.is_symlink() or candidate.is_file():
            targets.add(candidate)
    for name in ARTIFACT_DIRS:
        directory = root / name
        if directory.is_dir():
            targets.update(_walk(directory))
    for pattern in ARTIFACT_GLOBS:
        targets.update(p for p in root.glob(pattern) if p.is_file())
    return sorted(targets)


def scan_git_history(root: Path, allow: list[AllowRule]) -> list[Finding]:
    """Scan reachable Git blobs without checking out or writing any object."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--objects", "--all"],
        check=True,
        capture_output=True,
        text=True,
    )
    findings: list[Finding] = []
    seen: set[str] = set()
    for raw in result.stdout.splitlines():
        parts = raw.split(" ", 1)
        oid = parts[0]
        path = parts[1] if len(parts) == 2 else oid
        if oid in seen:
            continue
        seen.add(oid)
        kind = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-t", oid],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if kind != "blob":
            continue
        size = int(subprocess.run(
            ["git", "-C", str(root), "cat-file", "-s", oid],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip())
        if size > MAX_CONTENT_BYTES:
            continue
        data = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", oid],
            check=True,
            capture_output=True,
        ).stdout
        if b"\x00" in data[:8000]:
            continue
        history_path = f".git-history/{path}@{oid[:12]}"
        findings.extend(scan_text(data.decode("utf-8", errors="replace"), history_path, allow))
    return findings


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="repository root (default: this working copy)")
    parser.add_argument("--allowlist", default=None, help="allowlist file (default: <root>/scripts/secret-scan-allowlist.txt)")
    parser.add_argument("--report", default=None, help="write a JSON report to this path")
    parser.add_argument("--git-history", action="store_true", help="also scan every reachable Git blob")
    parser.add_argument("paths", nargs="*", help="explicit files/directories inside the root (default: tracked, untracked and artifact files)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    for forbidden in FORBIDDEN_ROOTS:
        if root == forbidden or forbidden in root.parents:
            print(f"error: refusing to scan production path {root}", file=sys.stderr)
            return 2
    allowlist_path = Path(args.allowlist) if args.allowlist else root / "scripts" / "secret-scan-allowlist.txt"
    allow = load_allowlist(allowlist_path)

    if args.paths:
        targets: list[Path] = []
        for raw in args.paths:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = root / candidate
            if not _inside(candidate, root):
                print(f"error: target {candidate} is outside the repository root {root}", file=sys.stderr)
                return 2
            if candidate.is_dir() and not candidate.is_symlink():
                targets.extend(_walk(candidate))
            else:
                targets.append(candidate)
    else:
        targets = collect_targets(root)

    findings: list[Finding] = []
    for target in targets:
        findings.extend(scan_path(target, root, allow))
    if args.git_history:
        findings.extend(scan_git_history(root, allow))

    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.rule}: {finding.snippet}")
    status = "failed" if findings else "passed"
    print(f"secret scan: {status} ({len(findings)} findings, {len(targets)} files scanned)")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "status": status,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "root": str(root),
            "allowlist": str(allowlist_path) if allowlist_path.exists() else None,
            "scanned_files": len(targets),
            "findings": [asdict(finding) for finding in findings],
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
