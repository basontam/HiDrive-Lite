"""Behaviour in the production ``access`` auth mode, with JWT verification
stubbed so no Cloudflare endpoint is contacted."""

from __future__ import annotations

import pytest


@pytest.fixture
def access_mode(hidrive, monkeypatch):
    monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
    monkeypatch.setattr(hidrive, "verify_access_jwt", lambda token: {"sub": "owner", "email": "owner@example.test"} if token == "valid-assertion" else (_ for _ in ()).throw(PermissionError("bad")))
    return hidrive


def test_requests_without_access_assertion_are_rejected(client, access_mode):
    response = client.get("/api/status")
    assert response.status_code == 401
    assert response.get_json()["success"] is False


def test_healthz_is_also_behind_access(client, access_mode):
    """Release health checks must expect 401 on loopback, not 200."""
    assert client.get("/healthz").status_code == 401
    assert client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"}).status_code == 200


def test_invalid_assertion_is_rejected(client, access_mode):
    assert client.get("/api/status", headers={"Cf-Access-Jwt-Assertion": "forged"}).status_code == 401


def test_writes_require_origin_and_csrf_token(client, access_mode):
    auth = {"Cf-Access-Jwt-Assertion": "valid-assertion"}
    assert client.post("/api/settings", json={}, headers=auth).status_code == 403
    token = client.get("/api/csrf", headers=auth).get_json()["token"]
    wrong_origin = client.post("/api/settings", json={}, headers={**auth, "Origin": "https://evil.test", "X-CSRF-Token": token})
    assert wrong_origin.status_code == 403
    ok = client.post("/api/settings", json={}, headers={**auth, "Origin": "https://hidrive.test", "X-CSRF-Token": token})
    assert ok.status_code == 200


def test_csrf_token_is_bound_to_principal(client, access_mode, monkeypatch):
    auth = {"Cf-Access-Jwt-Assertion": "valid-assertion"}
    token = client.get("/api/csrf", headers=auth).get_json()["token"]
    monkeypatch.setattr(access_mode, "verify_access_jwt", lambda _token: {"sub": "someone-else"})
    response = client.post("/api/settings", json={}, headers={**auth, "Origin": "https://hidrive.test", "X-CSRF-Token": token})
    assert response.status_code == 403


def test_env_credentials_are_ignored_in_access_mode(client, access_mode, monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "should-not-be-used")
    response = client.get("/api/hdhive/search?q=matrix", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"})
    assert response.status_code == 503
    assert response.get_json()["code"] == "TMDB_KEY_MISSING"


# ---------------------------------------------------------------------------
# T4.5: the two new /api/library/* POST endpoints (reveal, transfer) must be
# behind the SAME Access + CSRF guard as every other write route -- checked
# without an installed library, since Access/CSRF run in before_request
# before the route handler (and _library_store_or_error()) ever executes.
# ---------------------------------------------------------------------------


def test_library_reveal_requires_access_assertion(client, access_mode):
    response = client.post("/api/library/link/some-public-id/reveal")
    assert response.status_code == 401
    assert response.get_json()["success"] is False


def test_library_transfer_requires_access_assertion(client, access_mode):
    response = client.post("/api/library/transfer", json={"resource_link_id": "some-public-id"})
    assert response.status_code == 401
    assert response.get_json()["success"] is False


def test_library_reveal_requires_csrf(client, access_mode):
    auth = {"Cf-Access-Jwt-Assertion": "valid-assertion"}
    response = client.post("/api/library/link/some-public-id/reveal", headers=auth)
    assert response.status_code == 403


def test_library_transfer_requires_csrf(client, access_mode):
    auth = {"Cf-Access-Jwt-Assertion": "valid-assertion"}
    response = client.post("/api/library/transfer", json={"resource_link_id": "some-public-id"}, headers=auth)
    assert response.status_code == 403


def test_library_transfer_passes_csrf_and_reaches_the_route(client, access_mode):
    """With a valid Access assertion + Origin + CSRF token, the request must
    clear before_request entirely and reach the route handler -- proven by
    getting the route's own 503 LIBRARY_NOT_INSTALLED (not a 401/403)."""
    auth = {"Cf-Access-Jwt-Assertion": "valid-assertion"}
    token = client.get("/api/csrf", headers=auth).get_json()["token"]
    response = client.post(
        "/api/library/transfer",
        json={"resource_link_id": "some-public-id"},
        headers={**auth, "Origin": "https://hidrive.test", "X-CSRF-Token": token},
    )
    assert response.status_code == 503
    assert response.get_json()["code"] == "LIBRARY_NOT_INSTALLED"


def test_unauthenticated_request_never_starts_the_background_enricher(client, access_mode, monkeypatch):
    """Minor fix: _ensure_library_enricher_started() used to run at the very
    top of before_request, before require_access() -- so even a request
    that gets rejected with 401 would still start the background TMDB
    enricher thread (and, once running, its own TMDB calls). It must only
    ever start for an authenticated request."""
    monkeypatch.setenv("LIBRARY_ENRICH_AUTOSTART", "1")
    calls = []
    monkeypatch.setattr(access_mode.library_tmdb.BackgroundEnricher, "start", lambda self: calls.append(1))
    response = client.get("/api/status")
    assert response.status_code == 401
    assert calls == []
