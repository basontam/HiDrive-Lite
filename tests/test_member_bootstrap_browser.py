"""F01: a real browser runs the real bundle against the real templates.

The bug this file exists for was a `TypeError: Cannot set properties of null`
in `views.reauth.init()` -- an initialiser binding an *administrator's*
settings button on a member's page. Nothing that inspects HTML strings or
stubs the DOM could see it, because the symptom is that every later
initialiser (account, my115, library, router) silently never ran.

So: start a real `app.py` in a temp directory (`HIDRIVE_AUTH_MODE=local`,
its own master key, its own databases -- see scripts/ui_screenshots.py),
load the page in Chromium for each of the five credential states, and fail
on any uncaught exception.

No upstream is ever contacted. The browser's own network is restricted to
this app (ui_screenshots.install_network_guard), and the three routes whose
handlers would make the *server* call 115 are fulfilled in the browser, so
the request never reaches it: this file tests the page, and the server side
of those routes has its own tests with faked upstreams.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import auth_service as auth  # noqa: E402
import ui_screenshots as uis  # noqa: E402
import user_115  # noqa: E402

MEMBER_EMAIL = "member@example.test"
MEMBER_PASSWORD = "Correct1Horse"
COOKIE_FIXTURE = "UID=fixture-not-real; CID=fixture-not-real; SEID=fixture-not-real"
TOKEN_FIXTURE = "open-token-fixture-not-real"

# A 1x1 PNG, for the QR image route.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e"
    "44ae426082"
)


def _db(data_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(data_dir / "hidrive.db", timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _fernet(env: dict):
    from cryptography.fernet import Fernet

    return Fernet(Path(env["HIDRIVE_MASTER_KEY_FILE"]).read_bytes())


def _make_member(base_url: str, data_dir: Path, request_context) -> int:
    response = request_context.post(f"{base_url}/api/auth/register", data={
        "email": MEMBER_EMAIL, "password": MEMBER_PASSWORD, "confirm_password": MEMBER_PASSWORD})
    assert response.status in (200, 201, 202), (response.status, response.text())
    conn = _db(data_dir)
    try:
        admin_id = auth.ensure_admin_user(conn, email="admin@example.invalid", now=1)
        row = conn.execute("SELECT id FROM auth_user WHERE email_norm=?", (MEMBER_EMAIL,)).fetchone()
        auth.approve_user(conn, int(row["id"]), approver_id=admin_id, now=2)
        conn.commit()
    finally:
        conn.close()
    return int(row["id"])


def _set_credentials(env: dict, data_dir: Path, member_id: int, *, step_a: bool, step_b: bool) -> None:
    conn = _db(data_dir)
    fernet = _fernet(env)
    try:
        for name, wanted, value in (
            (user_115.COOKIE_SECRET, step_a, COOKIE_FIXTURE),
            (user_115.OPEN_ACCESS_SECRET, step_b, TOKEN_FIXTURE),
            (user_115.OPEN_REFRESH_SECRET, step_b, TOKEN_FIXTURE + "-refresh"),
        ):
            if wanted:
                user_115.secret_set(conn, member_id, name, value, fernet=fernet, now=1)
            else:
                user_115.secret_clear(conn, member_id, name)
        conn.execute("INSERT INTO settings(name, value, updated_at) VALUES('cloud_download_enabled','1',1) "
                     "ON CONFLICT(name) DO UPDATE SET value='1'")
        conn.commit()
    finally:
        conn.close()


def _media_id_with(data_dir: Path, provider: str) -> int:
    """The synthetic library's media carrying a live link of this provider --
    `115` renders 转存到 115, `ed2k` renders 云下载."""
    conn = sqlite3.connect(data_dir / "media-library.db", timeout=10)
    try:
        row = conn.execute(
            "SELECT m.id FROM media m JOIN resource_group g ON g.media_id = m.id "
            "JOIN resource_link l ON l.group_id = g.id "
            "WHERE l.provider=? AND l.deleted_at_source IS NULL LIMIT 1", (provider,)
        ).fetchone()
    finally:
        conn.close()
    assert row, f"the synthetic library is expected to carry one {provider} link"
    return int(row[0])


def _stub_115_routes(page) -> None:
    """The three routes whose *server* handler would call 115. Fulfilled in
    the browser, so no request leaves this machine and no 115 endpoint is
    ever touched -- the page's own behaviour is what is under test here."""
    page.route("**/api/115/reauth/start", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"success": True, "challenge_id": "challenge-fixture", "expires_in": 180})))
    page.route("**/api/115/reauth/qr*", lambda route: route.fulfill(
        status=200, content_type="image/png", body=TINY_PNG))
    page.route("**/api/115/reauth/status*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"success": True, "state": "pending", "expires_in": 170})))
    page.route("**/api/115/folders*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"success": True, "mode": "115", "cid": "0",
                         "items": [{"cid": "a1", "name": "我的收藏"}]})))
    page.route("**/api/library/cloud-download/quota*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"success": True, "count": 100, "used": 1, "surplus": 99,
                         "package": [], "today_submitted": 0, "daily_cap": 0})))


STATES = (
    ("administrator", None),
    ("nothing-connected", (False, False)),
    ("step-a-only", (True, False)),
    ("step-b-only", (False, True)),
    ("both-steps", (True, True)),
)


@pytest.mark.slow
def test_every_credential_state_boots_without_an_uncaught_exception(tmp_path):
    """F01's core acceptance: the real page, the real bundle, zero uncaught
    exceptions, and the later initialisers demonstrably reached."""
    ok, message = uis.ensure_chromium()
    assert ok, f"chromium unavailable in this environment: {message}"

    from playwright.sync_api import sync_playwright

    results: dict[str, dict] = {}
    with uis.running_app() as base_url:
        env = uis._app_env
        data_dir = Path(env["HIDRIVE_DATA_DIR"])
        media_id = _media_id_with(data_dir, "ed2k")
        with sync_playwright() as p:
            browser = uis.launch_chromium(p)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = context.new_page()
                uis.install_network_guard(page, base_url)
                _stub_115_routes(page)
                member_id = _make_member(base_url, data_dir, context.request)

                # One listener for the whole run, cleared between states --
                # re-adding it per state would report one exception several
                # times over and hide which state produced it.
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                for label, steps in STATES:
                    del errors[:]
                    context.clear_cookies()
                    if steps is None:
                        # local mode: an anonymous request is the administrator
                        _set_credentials(env, data_dir, member_id, step_a=False, step_b=False)
                    else:
                        _set_credentials(env, data_dir, member_id, step_a=steps[0], step_b=steps[1])
                        login = context.request.post(f"{base_url}/api/auth/login", data={
                            "email": MEMBER_EMAIL, "password": MEMBER_PASSWORD})
                        assert login.status == 200, (label, login.status, login.text())

                    page.goto(f"{base_url}/?tab=library", wait_until="load")
                    # The last initialisers in the bundle: account.load()
                    # fills #accountList and library.init() draws the rails.
                    # Neither runs if an earlier initialiser threw.
                    page.wait_for_selector("#accountList .status-row", state="attached")
                    uis._wait_for_home_rails(page)
                    role = page.eval_on_selector_all(
                        "#accountList .status-row", "rows => rows.map(r => r.textContent).join('|')")
                    results[label] = {
                        "errors": list(errors),
                        "role_row": role,
                        "has_transfer_dialog": page.locator("#transferDialog").count() == 1,
                        "has_reauth_dialog": page.locator("#reauthDialog").count() == 1,
                        "has_settings_trigger": page.locator("#reauth115Btn").count() == 1,
                        "has_my115": page.locator("#my115Card").count() == 1,
                        "operator_tabs": page.locator('[data-tab="openlist"], [data-tab="strm"]').count(),
                        "cloud_tab": page.locator('[data-tab="cloud"]').count(),
                    }
                context.close()
            finally:
                browser.close()

    for label, found in results.items():
        assert found["errors"] == [], f"{label}: uncaught exception during bootstrap: {found['errors']}"
        assert found["role_row"], f"{label}: account never rendered, so bootstrap did not finish"
        assert found["has_reauth_dialog"], f"{label}: the scan dialog is how step A gets connected"

    admin, nothing, only_a, only_b, both = (results[name] for name, _ in STATES)

    # The administrator keeps the settings trigger; no member page has it,
    # which is exactly why binding it unconditionally threw.
    assert admin["has_settings_trigger"] is True
    for member in (nothing, only_a, only_b, both):
        assert member["has_settings_trigger"] is False
        assert member["operator_tabs"] == 0
        assert member["has_my115"] is True

    # F01: the shared dialog follows *either* capability.
    assert nothing["has_transfer_dialog"] is False
    assert only_a["has_transfer_dialog"] is True
    assert only_b["has_transfer_dialog"] is True, "a step-B member needs the dialog cloud download opens"
    assert both["has_transfer_dialog"] is True
    assert only_b["cloud_tab"] == 1 and nothing["cloud_tab"] == 0

    assert media_id  # used by the click test below


@pytest.mark.slow
def test_a_member_can_actually_click_scan_transfer_and_cloud_download(tmp_path):
    """F01: the bindings, exercised. A member clicks 扫码连接 in 我的 115,
    then the resource action their own authorisation allows."""
    ok, message = uis.ensure_chromium()
    assert ok, f"chromium unavailable in this environment: {message}"

    from playwright.sync_api import sync_playwright

    outcome: dict[str, object] = {}
    with uis.running_app() as base_url:
        env = uis._app_env
        data_dir = Path(env["HIDRIVE_DATA_DIR"])
        transfer_media = _media_id_with(data_dir, "115")
        cloud_media = _media_id_with(data_dir, "ed2k")
        with sync_playwright() as p:
            browser = uis.launch_chromium(p)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                uis.install_network_guard(page, base_url)
                _stub_115_routes(page)
                member_id = _make_member(base_url, data_dir, context.request)

                # --- step A: the scan dialog opens from the member's own card
                _set_credentials(env, data_dir, member_id, step_a=False, step_b=False)
                assert context.request.post(f"{base_url}/api/auth/login", data={
                    "email": MEMBER_EMAIL, "password": MEMBER_PASSWORD}).status == 200
                page.goto(f"{base_url}/?tab=settings", wait_until="load")
                page.wait_for_selector("#my115Card")
                page.click("#my115TransferConnect")
                page.wait_for_selector("#reauthDialog:not([hidden])")
                page.wait_for_selector("#reauthQrImage[src]")
                outcome["scan_dialog_opened"] = True
                page.click("#reauthCancel")
                page.wait_for_selector("#reauthDialog[hidden]", state="attached")

                # --- step A only: the transfer dialog opens, and the folder
                # picker says which authorisation would allow choosing one.
                _set_credentials(env, data_dir, member_id, step_a=True, step_b=False)
                page.unroute("**/api/115/folders*")
                page.goto(f"{base_url}/?tab=library&media={transfer_media}", wait_until="load")
                page.wait_for_selector("#libraryDetail:not([hidden]) .link-row")
                page.click(".link-transfer")
                page.wait_for_selector("#transferDialog:not([hidden])")
                page.wait_for_selector("#folderResult .empty")
                outcome["transfer_guidance"] = page.inner_text("#folderResult")
                outcome["save_enabled"] = page.is_enabled("#saveBtn")
                page.click("#transferCancel")

                # --- step B only: the same dialog, opened by 云下载
                _set_credentials(env, data_dir, member_id, step_a=False, step_b=True)
                _stub_115_routes(page)
                page.goto(f"{base_url}/?tab=library&media={cloud_media}", wait_until="load")
                page.wait_for_selector("#libraryDetail:not([hidden]) .link-row")
                page.wait_for_selector(".link-cloud")
                page.click(".link-cloud")
                page.wait_for_selector("#transferDialog:not([hidden])")
                page.wait_for_selector("#folderResult .folder")
                outcome["cloud_dialog_title"] = page.inner_text("#transferTitle")
                outcome["cloud_folder_names"] = page.eval_on_selector_all(
                    "#folderResult .folder .folder-name", "els => els.map(e => e.textContent)")
                outcome["cloud_target"] = page.inner_text("#targetPath")
                outcome["errors"] = list(errors)
                context.close()
            finally:
                browser.close()

    assert outcome["errors"] == [], f"uncaught exception while clicking: {outcome['errors']}"
    assert outcome["scan_dialog_opened"] is True
    # Step A without step B: no folder choice, but the transfer itself stays
    # available (it goes to this member's own 115 default inbox).
    assert "授权" in outcome["transfer_guidance"], outcome["transfer_guidance"]
    assert "默认接收" in outcome["transfer_guidance"], outcome["transfer_guidance"]
    assert outcome["save_enabled"] is True
    # Step B: the local ed2k link opens the cloud-download dialog, browsing
    # this member's own 115 by cid.
    assert "云下载" in outcome["cloud_dialog_title"], outcome["cloud_dialog_title"]
    assert outcome["cloud_folder_names"] == ["我的收藏"]
    assert "115" in outcome["cloud_target"]


# ---------------------------------------------------------------------------
# G06: a first scan makes the page usable without the user reloading it
# ---------------------------------------------------------------------------


def _second_member(base_url, data_dir, request_context, email="other@example.test") -> int:
    response = request_context.post(f"{base_url}/api/auth/register", data={
        "email": email, "password": MEMBER_PASSWORD, "confirm_password": MEMBER_PASSWORD})
    assert response.status in (200, 201, 202), (response.status, response.text())
    conn = _db(data_dir)
    try:
        admin_id = auth.ensure_admin_user(conn, email="admin@example.invalid", now=1)
        row = conn.execute("SELECT id FROM auth_user WHERE email_norm=?", (email,)).fetchone()
        auth.approve_user(conn, int(row["id"]), approver_id=admin_id, now=2)
        conn.commit()
    finally:
        conn.close()
    return int(row["id"])


def _cookie_of(env, data_dir, user_id: int):
    conn = _db(data_dir)
    try:
        return user_115.secret_get(conn, user_id, user_115.COOKIE_SECRET, fernet=_fernet(env))
    finally:
        conn.close()


def _global_cookie(data_dir) -> bytes | None:
    conn = _db(data_dir)
    try:
        row = conn.execute("SELECT value FROM secrets WHERE name='115_cookie'").fetchone()
    finally:
        conn.close()
    return bytes(row["value"]) if row is not None else None


@pytest.mark.slow
def test_a_first_scan_leaves_the_member_able_to_transfer_immediately(tmp_path):
    """G06: the scan callback used to refresh `/api/status` -- the
    administrator's endpoint -- so a member's capabilities, their 我的 115 card
    and the capability-rendered DOM all stayed as they were. Clicking 转存
    still said "scan first" until the user reloaded the page by hand.

    The scan is completed entirely through routes fulfilled in the browser, so
    no 115 endpoint is contacted; the member's synthetic cookie is written into
    the isolated server's own database by the status handler, which is what
    makes the next server-rendered page carry the dialog.
    """
    ok, message = uis.ensure_chromium()
    assert ok, f"chromium unavailable in this environment: {message}"

    from playwright.sync_api import sync_playwright

    outcome: dict[str, object] = {}
    with uis.running_app() as base_url:
        env = uis._app_env
        data_dir = Path(env["HIDRIVE_DATA_DIR"])
        transfer_media = _media_id_with(data_dir, "115")
        with sync_playwright() as p:
            browser = uis.launch_chromium(p)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                loads: list[str] = []
                page.on("load", lambda _page: loads.append("load"))
                statuses: list[tuple[str, int]] = []
                page.on("response", lambda response: statuses.append((response.url, response.status)))
                uis.install_network_guard(page, base_url)

                member_id = _make_member(base_url, data_dir, context.request)
                other_id = _second_member(base_url, data_dir, context.request)
                _set_credentials(env, data_dir, other_id, step_a=True, step_b=False)
                other_before = _cookie_of(env, data_dir, other_id)
                # The administrator's own legacy slot, which a member's scan
                # must never touch.
                conn = _db(data_dir)
                try:
                    conn.execute("INSERT OR REPLACE INTO secrets(name, value, updated_at) VALUES(?,?,?)",
                                 ("115_cookie", _fernet(env).encrypt(b"the-administrators-cookie"), 1))
                    conn.commit()
                finally:
                    conn.close()
                global_before = _global_cookie(data_dir)

                # This member has nothing connected, and logs in on this page.
                _set_credentials(env, data_dir, member_id, step_a=False, step_b=False)
                assert context.request.post(f"{base_url}/api/auth/login", data={
                    "email": MEMBER_EMAIL, "password": MEMBER_PASSWORD}).status == 200

                polls = {"n": 0}

                def status_route(route):
                    polls["n"] += 1
                    if polls["n"] == 1:
                        route.fulfill(status=200, content_type="application/json",
                                      body=json.dumps({"success": True, "state": "pending", "expires_in": 170}))
                        return
                    # The scan completed: the server-side effect a real
                    # exchange would have had, on this isolated database only.
                    _set_credentials(env, data_dir, member_id, step_a=True, step_b=False)
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"success": True, "state": "authenticated", "expires_in": 0}))

                page.route("**/api/115/reauth/start", lambda route: route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"success": True, "challenge_id": "challenge-g06", "expires_in": 180})))
                page.route("**/api/115/reauth/qr*", lambda route: route.fulfill(
                    status=200, content_type="image/png", body=TINY_PNG))
                page.route("**/api/115/reauth/status*", status_route)

                page.goto(f"{base_url}/?tab=settings", wait_until="load")
                page.wait_for_selector("#my115Card")
                outcome["dialog_before"] = page.locator("#transferDialog").count()
                loads_before = len(loads)

                page.click("#my115TransferConnect")
                page.wait_for_selector("#reauthDialog:not([hidden])")
                page.wait_for_selector("#reauthQrImage[src]")

                # From here the test does nothing: no goto, no reload, no
                # click. Whatever makes the page usable has to come from the
                # application itself.
                page.wait_for_selector("#transferDialog", state="attached", timeout=45000)
                outcome["loads_after"] = len(loads) - loads_before
                outcome["dialog_after"] = page.locator("#transferDialog").count()
                outcome["polls"] = polls["n"]

                # And the member can act on it straight away.
                page.goto(f"{base_url}/?tab=library&media={transfer_media}", wait_until="load")
                page.wait_for_selector("#libraryDetail:not([hidden]) .link-row")
                page.click(".link-transfer")
                page.wait_for_selector("#transferDialog:not([hidden])")
                page.wait_for_selector("#folderResult .empty, #folderResult .folder")
                outcome["transfer_opened"] = True
                outcome["folder_text"] = page.inner_text("#folderResult")
                outcome["save_enabled"] = page.is_enabled("#saveBtn")
                outcome["errors"] = list(errors)
                outcome["admin_status_calls"] = [
                    (url, code) for url, code in statuses if "/api/status" in url]
                context.close()
            finally:
                browser.close()

        outcome["other_cookie_unchanged"] = _cookie_of(env, data_dir, other_id) == other_before
        outcome["global_cookie_unchanged"] = _global_cookie(data_dir) == global_before

    assert outcome["errors"] == [], f"uncaught exception during the transition: {outcome['errors']}"
    assert outcome["dialog_before"] == 0, "this test starts from a member who has no transfer dialog"
    assert outcome["polls"] >= 2, "the status poll should have run at least twice"
    assert outcome["dialog_after"] == 1, "the page never became usable without a manual reload"
    assert outcome["loads_after"] >= 1, "the application did not refresh the page itself"
    assert outcome["transfer_opened"] is True
    assert "授权" in outcome["folder_text"], outcome["folder_text"]
    assert outcome["save_enabled"] is True
    assert outcome["admin_status_calls"] == [], (
        "a member's page asked for the administrator's status", outcome["admin_status_calls"])
    assert outcome["other_cookie_unchanged"] is True, "another member's cookie changed"
    assert outcome["global_cookie_unchanged"] is True, "the administrator's legacy cookie changed"
