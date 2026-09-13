"""Phase 3: who may reach what.

Plan §6/§13/§16.2. Every route is exercised directly with a member's session
-- no snapshot test would catch a route that merely stops being drawn -- and
the map that decides is itself pinned, so a new route cannot slip in
unclassified.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service as auth  # noqa: E402

MEMBER_EMAIL = "member@example.test"
MEMBER_PASSWORD = "Correct1Horse"
SESSION_COOKIE = "__Host-hidrive_session"


@pytest.fixture
def member(client, hidrive):
    """An approved member, signed in on this client."""
    client.post("/api/auth/register", json={
        "email": MEMBER_EMAIL, "password": MEMBER_PASSWORD, "confirm_password": MEMBER_PASSWORD})
    with hidrive.connect_db() as db:
        admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        row = db.execute("SELECT id FROM auth_user WHERE email_norm=?", (MEMBER_EMAIL,)).fetchone()
        auth.approve_user(db, int(row["id"]), approver_id=admin_id, now=2)
    response = client.post("/api/auth/login", json={"email": MEMBER_EMAIL, "password": MEMBER_PASSWORD})
    assert response.status_code == 200
    return int(row["id"])


def _rules(hidrive):
    for rule in hidrive.app.url_map.iter_rules():
        methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
        yield rule.endpoint, methods[0] if methods else "GET", str(rule)


def _call(client, method: str, url: str):
    url = (url.replace("<int:media_id>", "1").replace("<int:group_id>", "1")
              .replace("<int:candidate_id>", "1").replace("<int:resource_id>", "1")
              .replace("<int:user_id>", "1").replace("<info_hash>", "abc")
              .replace("<public_id>", "pub-1").replace("<ref>", "r1")
              .replace("<media_type>", "movie").replace("<int:tmdb_id>", "1")
              .replace("<path:filename>", "app.css"))
    return client.open(url, method=method, json={} if method in {"POST", "PATCH", "PUT"} else None)


# ---------------------------------------------------------------------------
# the map itself
# ---------------------------------------------------------------------------


class TestTheMap:
    def test_every_registered_route_is_classified(self, hidrive):
        """A route with no entry is refused as an administrator's, which is
        the safe default -- but it must still be a decision, so this fails
        until someone records it."""
        classified = (hidrive.PUBLIC_ENDPOINTS | hidrive.MEMBER_ENDPOINTS | hidrive.ADMIN_ENDPOINTS)
        registered = {endpoint for endpoint, _method, _url in _rules(hidrive)}
        assert registered - classified == set(), "unclassified route(s)"

    def test_no_route_is_in_two_buckets(self, hidrive):
        assert not hidrive.PUBLIC_ENDPOINTS & hidrive.MEMBER_ENDPOINTS
        assert not hidrive.PUBLIC_ENDPOINTS & hidrive.ADMIN_ENDPOINTS
        assert not hidrive.MEMBER_ENDPOINTS & hidrive.ADMIN_ENDPOINTS

    def test_the_map_names_only_routes_that_exist(self, hidrive):
        registered = {endpoint for endpoint, _method, _url in _rules(hidrive)}
        classified = (hidrive.PUBLIC_ENDPOINTS | hidrive.MEMBER_ENDPOINTS | hidrive.ADMIN_ENDPOINTS)
        assert classified - registered == set(), "map names a route that is gone"

    def test_the_operator_surface_is_administrator_only(self, hidrive):
        for endpoint in ("api_openlist_list", "api_strm_list", "api_settings", "api_status",
                         "api_library_linkcheck_status", "api_library_tmdb_status",
                         "api_library_re0_status", "api_admin_users", "api_admin_policy_re0"):
            assert endpoint in hidrive.ADMIN_ENDPOINTS, endpoint

    def test_the_library_is_open_to_any_signed_in_user(self, hidrive):
        for endpoint in ("api_library_search", "api_library_media", "api_library_reveal",
                         "api_library_search_re0", "api_library_re0_file_preview"):
            assert endpoint in hidrive.MEMBER_ENDPOINTS, endpoint

    def test_the_paths_that_spend_points_are_reachable_but_governed(self, hidrive):
        """Phase 7: a member may reach them -- reusing an already materialised
        link costs nothing and must work -- and what they may do beyond that
        is decided inside, by `allow_member_re0_unlock`."""
        for endpoint in ("api_library_re0_unlock_and_action", "api_library_re0_follow_unlock",
                         "api_hdhive_unlock"):
            assert endpoint in hidrive.MEMBER_ENDPOINTS, endpoint

    def test_folders_and_cloud_download_are_member_reachable(self, hidrive):
        """Phase 6 (R05): folder browsing and cloud download belong to
        whoever authorised their own 115 -- the token scopes the call, so
        these are MEMBER endpoints, not ADMIN ones. Step A (web cookie and
        the transfer it enables) opened in Phase 4."""
        for endpoint in ("api_115_folders", "api_cloud_download_submit",
                         "api_cloud_download_tasks", "api_cloud_download_status",
                         "api_cloud_download_quota", "api_cloud_download_delete",
                         "api_cloud_download_clear"):
            assert endpoint in hidrive.MEMBER_ENDPOINTS, endpoint
            assert endpoint not in hidrive.ADMIN_ENDPOINTS, endpoint
        for endpoint in ("api_115_save", "api_library_transfer", "api_115_reauth_start"):
            assert endpoint in hidrive.MEMBER_ENDPOINTS, endpoint


# ---------------------------------------------------------------------------
# a member, calling everything directly
# ---------------------------------------------------------------------------


class TestMemberReach:
    def test_a_member_is_refused_every_administrator_route(self, client, hidrive, member):
        refused = {}
        for endpoint, method, url in _rules(hidrive):
            if endpoint not in hidrive.ADMIN_ENDPOINTS:
                continue
            response = _call(client, method, url)
            refused[endpoint] = response.status_code
        wrong = {name: code for name, code in refused.items() if code != 403}
        assert wrong == {}, f"these did not refuse a member: {wrong}"

    def test_every_refusal_says_the_same_thing(self, client, hidrive, member):
        response = client.get("/api/openlist/list")
        assert response.status_code == 403
        assert response.get_json()["code"] == "FORBIDDEN"

    def test_a_member_still_reaches_the_library(self, client, hidrive, member):
        for endpoint, method, url in _rules(hidrive):
            if endpoint not in hidrive.MEMBER_ENDPOINTS:
                continue
            try:
                response = _call(client, method, url)
            except RuntimeError as exc:
                # The handler ran and tried to reach 115, which the harness
                # blocks. Authorisation is what is under test.
                assert "network access blocked" in str(exc), f"{endpoint}: {exc}"
                continue
            assert response.status_code not in {401, 403}, f"{endpoint} refused a member ({response.status_code})"

    def test_a_member_sees_their_own_capabilities(self, client, member):
        caps = client.get("/api/me").get_json()["capabilities"]
        assert caps["library"] is True
        for name in ("openlist", "strm", "global_settings"):
            assert caps[name] is False, name

    def test_a_forged_role_in_the_body_changes_nothing(self, client, member):
        response = client.post("/api/auth/login", json={
            "email": MEMBER_EMAIL, "password": MEMBER_PASSWORD, "role": "admin", "capabilities": {"openlist": True}})
        assert response.get_json()["role"] == "member"
        assert client.get("/api/openlist/list").status_code == 403

    def test_a_forged_user_id_in_the_query_changes_nothing(self, client, member):
        assert client.get("/api/openlist/list?user_id=1").status_code == 403
        assert client.get("/api/me?user_id=1").get_json()["role"] == "member"

    def test_a_disabled_member_loses_everything_at_once(self, client, hidrive, member, monkeypatch):
        """Checked in `app` mode, where the session is the only way in. In
        today's `access` mode Cloudflare is still the gate and the
        administrator must not be locked out by a session expiring."""
        monkeypatch.setattr(hidrive, "AUTH_MODE", "app")
        assert client.get("/api/me").status_code == 200
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            auth.disable_user(db, member, approver_id=admin_id, now=3)
        assert client.get("/api/me").status_code == 401
        assert client.get("/api/library/search?q=x").status_code == 401


# ---------------------------------------------------------------------------
# the administrator keeps everything
# ---------------------------------------------------------------------------


class TestAdminUnchanged:
    def test_the_administrator_reaches_every_route_the_member_cannot(self, client, hidrive):
        for endpoint, method, url in _rules(hidrive):
            if endpoint not in hidrive.ADMIN_ENDPOINTS:
                continue
            try:
                response = _call(client, method, url)
            except RuntimeError as exc:
                # The handler ran and tried to reach 115/TMDB, which the test
                # harness blocks. Authorisation is what is under test here,
                # and it let the request through.
                assert "network access blocked" in str(exc), f"{endpoint}: {exc}"
                continue
            assert response.status_code not in {401, 403}, f"{endpoint} refused the administrator"

    def test_the_five_tabs_are_all_still_there_for_the_administrator(self, client):
        page = client.get("/").get_data(as_text=True)
        for tab in ("library", "openlist", "strm", "cloud", "settings"):
            assert f'data-tab="{tab}"' in page, tab


# ---------------------------------------------------------------------------
# what a member's page is even made of
# ---------------------------------------------------------------------------


class TestMemberPage:
    def test_the_page_a_member_gets_has_no_operator_tabs_in_it_at_all(self, client, member):
        page = client.get("/").get_data(as_text=True)
        for tab in ("openlist", "strm"):
            assert f'data-tab="{tab}"' not in page, f"{tab} tab was sent to a member"
        assert 'data-tab="library"' in page
        assert 'data-tab="settings"' in page
        # 115 cloud download arrives with the member's own authorisation
        # (Phase 5/6); until then the workspace is not theirs either.
        assert 'data-tab="cloud"' not in page

    def test_it_is_absent_from_the_markup_not_merely_hidden(self, client, member):
        page = client.get("/").get_data(as_text=True)
        # The panels themselves, not just their buttons.
        assert 'id="openlist"' not in page
        assert 'id="strm"' not in page
        # Not even in the tagline: a member cannot use either, so the page
        # should not tell them the product is about them.
        assert "OpenList" not in page
        assert "STRM" not in page

    def test_no_global_setting_control_reaches_a_member(self, client, member):
        page = client.get("/").get_data(as_text=True)
        for control in ("cookie115", "tmdbKey", "linkcheckCard", "settingPid",
                        "re0SyncCard", "re0OAuthCard", "policyMemberUnlock", "usersCard"):
            assert control not in page, control

    def test_a_member_gets_their_own_account_section(self, client, member):
        page = client.get("/").get_data(as_text=True)
        assert 'id="accountCard"' in page
        assert "退出登录" in page

    def test_the_administrators_page_still_carries_every_control(self, client):
        page = client.get("/").get_data(as_text=True)
        for control in ("cookie115", "tmdbKey", "linkcheckCard", "settingPid",
                        "re0SyncCard", "re0OAuthCard"):
            assert control in page, control

    def test_the_administrator_gets_the_approval_surface(self, client):
        page = client.get("/").get_data(as_text=True)
        assert 'id="usersCard"' in page
        assert 'id="policyMemberUnlock"' in page
