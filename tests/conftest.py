"""Shared fixtures for HiDrive-Lite tests.

The application module binds its paths and auth mode at import time, so the
session fixture points every path at a temporary directory *before* the first
import.  Nothing here touches the production data directory, master key,
OpenList database or media folders.

All outbound HTTP is blocked at the ``requests`` adapter layer; tests replace
``app.requests.get/post/request`` with a ``FakeHTTP`` router instead.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
import requests.adapters
from cryptography.fernet import Fernet

from fixtures.library.make_workbooks import make_workbooks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_store  # noqa: E402

from fixtures.library.build_installed import build_synthetic_library  # noqa: E402

# Environment fallbacks that app.config_value() honours in local mode.  They
# are removed so a developer shell can never leak credentials into a test.
SECRET_ENV_NAMES = (
    "HDHIVE_APP_SECRET",
    "HDHIVE_CLIENT_ID",
    "TMDB_API_KEY",
    "ENV_115_COOKIES",
    "OPENLIST_TOKEN",
    "115_open_access_token",
    "115_open_refresh_token",
    "openlist_token",
    "115_cookie",
)


def _block_send(self, request, *args, **kwargs):  # pragma: no cover - guard
    raise RuntimeError(f"real network access blocked in tests: {request.method} {request.url}")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _block_send)


@pytest.fixture(scope="session")
def hidrive(tmp_path_factory):
    base = tmp_path_factory.mktemp("hidrive")
    data_dir = base / "data"
    data_dir.mkdir()
    key_file = base / "master.key"
    key_file.write_bytes(Fernet.generate_key())
    strm_root = base / "strm"
    strm_root.mkdir()
    for name in SECRET_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ.update(
        {
            "HIDRIVE_AUTH_MODE": "local",
            "HIDRIVE_DATA_DIR": str(data_dir),
            "HIDRIVE_MASTER_KEY_FILE": str(key_file),
            "STRM_ROOT": str(strm_root),
            "OPENLIST_DB": str(base / "no-openlist-here.db"),
            "OPENLIST_URL": "http://openlist.test",
            "HIDRIVE_PUBLIC_ORIGIN": "https://hidrive.test",
            # T4: never let a plain test request start the real background
            # TMDB enricher thread; tests that want to exercise autostart
            # set this back to "1" themselves via monkeypatch.setenv.
            "LIBRARY_ENRICH_AUTOSTART": "0",
        }
    )
    sys.path.insert(0, str(ROOT))
    module = importlib.import_module("app")
    module.app.config["TESTING"] = True
    return module


@pytest.fixture
def workspace(hidrive, tmp_path, monkeypatch):
    """Fresh database, STRM root and data directory for every test."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    strm_root = tmp_path / "strm"
    strm_root.mkdir()
    monkeypatch.setattr(hidrive, "DATA_DIR", data_dir)
    monkeypatch.setattr(hidrive, "DB_PATH", data_dir / "hidrive.db")
    monkeypatch.setattr(hidrive, "STRM_ROOT", strm_root.resolve())
    monkeypatch.setattr(hidrive, "OPENLIST_DB", tmp_path / "no-openlist-here.db")
    # T4: the library index lives in its own file under the (fresh) data
    # dir, and the background-enricher "started once" latch is reset so
    # every test gets its own fresh before_request autostart evaluation
    # (see app.py's _ensure_library_enricher_started docstring).
    monkeypatch.setattr(hidrive, "LIBRARY_DB_PATH", data_dir / "media-library.db")
    monkeypatch.setattr(hidrive, "_library_enricher_started", False)
    monkeypatch.setattr(hidrive, "_library_enricher", None)
    # w6: the link checker's own "started once" latch/instance, reset the
    # same way as the TMDB enricher's above.
    monkeypatch.setattr(hidrive, "_library_linkchecker_started", False)
    monkeypatch.setattr(hidrive, "_library_linkchecker", None)
    # T14 fix wave 1: _library_client_factory's shared RateLimiter is a
    # module-level singleton in production (see _shared_tmdb_rate_limiter) --
    # reset it per test so pacing/slow-down state never leaks between tests.
    monkeypatch.setattr(hidrive, "_tmdb_rate_limiter", None)
    # T17 item 5: /api/library/transfer's duplicate-submit guard is another
    # module-level singleton (keyed only on (resource_link_id, pid), not on
    # anything test-specific) -- reset it per test so a link_id+pid pair
    # reused verbatim across two unrelated tests never collides.
    monkeypatch.setattr(hidrive, "_TRANSFER_DEDUPE_SEEN", {})
    # w6: /api/library/resource/<id>/recheck's 60s cooldown is the same
    # kind of module-level singleton, keyed only on a group id that
    # restarts from 1 in every test's own fresh database.
    monkeypatch.setattr(hidrive, "_RECHECK_LAST_QUEUED_AT", {})
    hidrive.init_db()
    return tmp_path


@pytest.fixture
def client(hidrive, workspace):
    with hidrive.app.test_client() as test_client:
        yield test_client


class FakeMonotonic:
    """A monotonic clock a test can advance instantly -- lets a FakeHTTP
    handler simulate a slow upstream call (e.g. target-path resolution
    alone consuming most of a request-scoped deadline) without a genuinely
    slow test, and without the wall-clock flakiness of real ``time.sleep``
    calls under CI load. Patch in with
    ``monkeypatch.setattr(hidrive.time, "monotonic", clock)`` -- safe for
    the duration of one test since ``hidrive.time`` is the same ``time``
    module every caller uses, and monkeypatch restores it afterwards."""

    def __init__(self):
        self._value = time.monotonic()

    def __call__(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += seconds


class FakeResponse:
    def __init__(self, payload=None, status=200, *, raw: bytes | None = None, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        if raw is not None:
            self.content = raw
        elif payload is None:
            self.content = b""
        else:
            self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("response is not JSON")
        return self._payload

    def iter_content(self, chunk_size: int = 8192):
        """Minimal ``stream=True`` support: the fake's content is already
        fully in memory, so this just re-chunks it -- enough for code under
        test that reads a streamed body in bounded pieces."""
        content = self.content
        for i in range(0, len(content), chunk_size):
            yield content[i:i + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def close(self):
        """N5 (wave 2): the real ``requests.Response`` is always closed by
        callers using ``stream=True`` -- a no-op here since the fake's body
        is already fully in memory."""


class FakeHTTP:
    """Route fake responses by (method, url) and record every call."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.calls: list[dict] = []

    def route(self, method: str, url: str, payload=None, status: int = 200, *, raw=None, error: Exception | None = None, handler=None):
        """Register a canned response, an exception, or a callable(kwargs) -> FakeResponse."""
        self.routes[(method.upper(), url)] = handler or error or FakeResponse(payload, status, raw=raw)
        return self

    def _dispatch(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method.upper(), "url": url, **kwargs})
        try:
            outcome = self.routes[(method.upper(), url)]
        except KeyError:
            raise AssertionError(f"unexpected outbound request {method.upper()} {url}") from None
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(**kwargs)
        return outcome

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._dispatch(method, url, **kwargs)

    def calls_to(self, url: str) -> list[dict]:
        return [call for call in self.calls if call["url"] == url]


@pytest.fixture
def http(hidrive, monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(hidrive.requests, "get", fake.get)
    monkeypatch.setattr(hidrive.requests, "post", fake.post)
    monkeypatch.setattr(hidrive.requests, "request", fake.request)
    return fake


@pytest.fixture
def library_sources(tmp_path) -> Path:
    """Directory containing three deterministic, fully-fictional media-library
    workbooks (see fixtures/library/make_workbooks.py) for importer tests."""
    dest = tmp_path / "sources"
    make_workbooks(dest)
    return dest


@pytest.fixture
def audit_rows(hidrive):
    def _rows(action: str) -> list[dict]:
        with hidrive.connect_db() as db:
            rows = db.execute("SELECT action, status, detail, actor FROM audit_log WHERE action=? ORDER BY id", (action,)).fetchall()
        return [dict(row) for row in rows]

    return _rows


# ---------------------------------------------------------------------------
# T4: a small synthetic, installed (encrypted) media library for
# /api/library/* route tests. The real offline importer
# (scripts/import_media_library.py) is being built in a sibling worktree and
# is not available here, so this builds directly through LibraryStore +
# library_search.build_index, exactly as the T4 brief describes, then
# installs it through library_store.install_bundle() so the routes exercise
# the real encrypted production path (not a shortcut).
#
# The actual row data lives in fixtures/library/build_installed.py (T5.8
# extracted it there so scripts/ui_screenshots.py can build the same library
# without duplicating it).
# ---------------------------------------------------------------------------


@pytest.fixture
def installed_library(hidrive, workspace, monkeypatch):
    """Build the synthetic library above as a plaintext bundle, then install
    it through ``install_bundle`` (using the app's own ``load_fernet()``, so
    the live HTTP routes -- which also call ``load_fernet()`` -- can decrypt
    what this fixture wrote) at ``hidrive.LIBRARY_DB_PATH`` (already
    monkeypatched into ``workspace`` by the ``workspace`` fixture). Returns
    the opened, installed ``LibraryStore``; its plaintext markers are
    stashed on it for the ``library_markers`` fixture."""
    bundle_path = workspace / "library-bundle.sqlite"
    bundle_store = library_store.LibraryStore(bundle_path)
    bundle_store.create_schema()
    markers = build_synthetic_library(bundle_store)

    library_store.install_bundle(bundle_path, hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())

    store = library_store.open_installed(hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())
    store.markers = markers
    store.public_ids = [f"pub-fixture-{i:02d}" for i in range(1, 14)]
    return store


@pytest.fixture
def library_markers(installed_library) -> list[str]:
    """Every plaintext URL/access-code value used in ``installed_library``,
    for leak assertions against response JSON."""
    return installed_library.markers
