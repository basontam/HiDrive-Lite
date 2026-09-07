"""Page smoke tests: the four-tab shell (资源库/OpenList/STRM/设置) keeps its
navigation, static asset wiring, transfer dialog and settings elements, and
must never render stored secrets or leftover HDHive/OAuth/check-in UI."""

from __future__ import annotations

import json
import re
import subprocess

import pytest


@pytest.fixture
def page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    return response.get_data(as_text=True)


@pytest.fixture
def access_mode(hidrive, monkeypatch):
    monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
    monkeypatch.setattr(
        hidrive,
        "verify_access_jwt",
        lambda token: {"sub": "owner", "email": "owner@example.test"} if token == "valid-assertion" else (_ for _ in ()).throw(PermissionError("bad")),
    )
    return hidrive


def _has_id(html: str, element_id: str) -> bool:
    return re.search(rf'id="{re.escape(element_id)}"', html) is not None


def _static_js(client, page_html: str) -> str:
    match = re.search(r'(/static/app\.js\?v=[0-9a-f]{8})', page_html)
    assert match, "page must reference a versioned /static/app.js"
    response = client.get(match.group(1))
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_page_title_and_tabs(page):
    assert "<title>HiDrive-Lite</title>" in page
    assert 'role="tablist"' in page
    assert page.count(' role="tab"') == 4
    assert page.count("aria-selected=") == 4
    assert page.count(' role="tabpanel"') == 4
    for tab in ("library", "openlist", "strm", "settings"):
        assert f'data-tab="{tab}"' in page, tab


def test_page_references_versioned_static_assets(page):
    css = re.search(r'/static/app\.css\?v=([0-9a-f]{8})', page)
    js = re.search(r'/static/app\.js\?v=([0-9a-f]{8})', page)
    assert css and js
    assert css.group(1) == js.group(1)


def test_static_assets_are_served_in_local_mode(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_static_assets_are_behind_access_in_access_mode(client, access_mode):
    assert client.get("/static/app.js").status_code == 401


def _hdhive_field_access_ok(text: str) -> bool:
    """`hdhive` may appear only as an API field access (`.hdhive.` or
    `["hdhive"]`) -- e.g. `s.hdhive.authorized` for the settings-page
    background-authorisation status row -- never as a UI label or any other
    bare mention. HTML stays fully forbidden; this relaxation is JS-only."""
    total = re.findall(r"hdhive", text, re.I)
    field_access = re.findall(r'\.hdhive\.|\["hdhive"\]', text, re.I)
    return len(total) == len(field_access)


def test_no_leftover_hdhive_search_or_oauth_ui(page, client):
    js = _static_js(client, page)
    assert "hdhive" not in page.lower()
    assert "oauth" not in page.lower()
    assert "oauth" not in js.lower()
    for forbidden in ("签到", "解锁"):
        assert forbidden not in page, forbidden
        assert forbidden not in js, forbidden
    assert _hdhive_field_access_ok(js), "hdhive must appear only as .hdhive. / [\"hdhive\"] field access"
    for element_id in ("hdSecret", "hdClient", "oauthBtn", "tmdbQuery"):
        assert not _has_id(page, element_id), element_id
        assert element_id not in js, element_id


def test_page_and_status_never_expose_stored_secrets(client, hidrive):
    for name, value in (("hdhive_app_secret", "SECRET-MARKER-1"), ("tmdb_api_key", "SECRET-MARKER-2"), ("115_cookie", "SECRET-MARKER-3"), ("openlist_token", "SECRET-MARKER-4")):
        hidrive.secret_set(name, value)

    html = client.get("/").get_data(as_text=True)
    status = client.get("/api/status").get_data(as_text=True)

    for marker in ("SECRET-MARKER-1", "SECRET-MARKER-2", "SECRET-MARKER-3", "SECRET-MARKER-4"):
        assert marker not in html and marker not in status, marker


def test_page_transfer_dialog_and_browser_ids_preserved(page):
    for element_id in (
        "transferDialog", "transferShareUrl", "targetPath", "folderResult",
        "folderUp", "folderRefresh", "saveBtn", "transferCancel", "transferClose", "transferResult",
        "openCurrent", "openResult", "openUp", "openRefresh",
        "strmCurrent", "strmResult", "strmUp", "strmRefresh",
        "settingsSave", "settingsResult", "cookie115", "settingPid", "tmdbKey",
    ):
        assert _has_id(page, element_id), element_id
    # T18 §12.5: "选择此目录" (and its folderChoose button) is gone -- the
    # browsed directory is always the transfer target now.
    assert "folderChoose" not in page
    assert "选择此目录" not in page
    assert "转存到 115" in page
    assert "保存设置" in page


def test_settings_tmdb_key_field_wired_into_save(page, client):
    assert 'id="tmdbKey"' in page
    assert 'type="password"' in re.search(r'<input id="tmdbKey"[^>]*>', page).group(0)
    js = _static_js(client, page)
    assert "tmdb_api_key" in js


def test_settings_115_status_never_prefills_cookie_value(page):
    assert _has_id(page, "cookie115Status")
    assert _has_id(page, "reauth115Btn")
    input_tag = re.search(r'<input id="cookie115"[^>]*>', page).group(0)
    assert 'type="password"' in input_tag
    assert "value=" not in input_tag


def test_cookie115_status_five_state_texts_and_classes_present(client, page):
    # T19 fix wave 1 item 3 (brief §8.2): the settings status line's five
    # user-facing texts/colour classes, keyed off the sanitised cookie_state.
    # N7 (wave 2): scope this to the COOKIE_115_STATE_TEXT table and the
    # renderCookie115Status() function that reads it -- asserting these
    # strings appear *anywhere* in app.js would still pass even if the
    # function were deleted (or stopped reading the table) entirely, as
    # long as the strings existed somewhere else (e.g. dead code/comments).
    js = _static_js(client, page)
    match = re.search(r"var COOKIE_115_STATE_TEXT = \{.*?function renderCookie115Status\(n\) \{.*?\n  \}", js, re.S)
    assert match, "COOKIE_115_STATE_TEXT / renderCookie115Status not found"
    region = match.group(0)
    for text in ("可用", "需要重新授权", "网络暂时不可用", "受到限流", "未配置"):
        assert text in region, text
    for cls in ('"success"', '"error"', '"warn"'):
        assert cls in region, cls


def test_reauth_dialog_markup_ids_role_and_focus_trap(page, client):
    dialog_tag = re.search(r'<div id="reauthDialog"[^>]*>', page)
    assert dialog_tag, "reauthDialog not found"
    assert 'role="dialog"' in dialog_tag.group(0)
    assert 'aria-modal="true"' in dialog_tag.group(0)
    for element_id in ("reauthTitle", "reauthQrImage", "reauthCountdown", "reauthResult", "reauthCancel", "reauthRefresh", "reauthClose"):
        assert _has_id(page, element_id), element_id
    assert "data-close-reauth" in page
    js = _static_js(client, page)
    assert "#reauthDialog .modal-card" in js, "focus-trap wiring not found"
    assert "views.reauth" in js


def test_reauth_poll_uses_settimeout_chain_not_setinterval(client, page):
    # w6-reauth-longpoll-fix item 3: driven by a single setTimeout chain,
    # not a 1.5s setInterval -- start() must never install an interval-based
    # poller, and stopPolling() must clear a timer with clearTimeout.
    region = _reauth_region(_static_js(client, page))
    assert "setInterval(views.reauth.poll" not in region, "must not poll via setInterval"
    stop_match = re.search(r"stopPolling: function \(\) \{(.*?)\n    \},", region, re.S)
    assert stop_match, "views.reauth.stopPolling not found"
    assert "clearTimeout(state.reauth.pollTimer)" in stop_match.group(1)
    start_match = re.search(r"start: function \(\) \{(.*?)\n    \},", region, re.S)
    assert start_match, "views.reauth.start not found"
    assert "views.reauth.poll();" in start_match.group(1), "start() must kick off the poll chain directly"
    for state_name in ("authenticated", "expired", "cancelled", "failed", "confirmed"):
        assert '"' + state_name + '"' in region, state_name


def test_reauth_poll_has_in_flight_guard(client, page):
    # Item 1: a slow response must never let two status polls overlap.
    js = _static_js(client, page)
    assert "state.reauth.polling" in js


def _reauth_poll_body(client, page):
    region = _reauth_region(_static_js(client, page))
    match = re.search(r"poll: function \(\) \{(.*?)\n    \},", region, re.S)
    assert match, "views.reauth.poll not found"
    return match.group(1)


def test_reauth_poll_reschedules_after_300ms_on_settled_success(client, page):
    # w6-reauth-longpoll-fix item 3: a settled 2xx response reschedules the
    # next poll at ~300ms, not the old flat 200ms/1.5s-interval cadence,
    # while still going through the in-flight guard (only one request open)
    # and still stopping on a terminal state (challengeId cleared already).
    body = _reauth_poll_body(client, page)
    success = re.search(r"\.then\(function \(d\) \{(.*?)\n      \}\)\.catch", body, re.S)
    assert success, "the success .then(function (d) {...}) handler not found"
    success_body = success.group(1)
    assert "state.reauth.polling = false;" in success_body
    assert re.search(r"if \(state\.reauth\.challengeId\)\s*state\.reauth\.pollTimer\s*=\s*setTimeout\(views\.reauth\.poll,\s*300\)", success_body), \
        "must reschedule via setTimeout(...,300) only while a challenge is still active"


def test_reauth_poll_backs_off_to_1500ms_on_error(client, page):
    # w6-reauth-longpoll-fix item 3: any error other than the challenge
    # being gone (404/410) backs off to the old 1.5s cadence instead of
    # hammering a struggling upstream.
    body = _reauth_poll_body(client, page)
    error_handler = re.search(r"\.catch\(function \(e\) \{(.*?)\n      \}\);", body, re.S)
    assert error_handler, "the poll's .catch(function (e) {...}) handler not found"
    error_body = error_handler.group(1)
    assert "state.reauth.polling = false;" in error_body
    assert re.search(r"if \(state\.reauth\.challengeId\)\s*state\.reauth\.pollTimer\s*=\s*setTimeout\(views\.reauth\.poll,\s*1500\)", error_body), \
        "must back off to a 1.5s setTimeout on a non-404/410 error"


def test_reauth_poll_stops_and_shows_message_when_challenge_gone(client, page):
    # w6-reauth-longpoll-fix item 3: a 404 (REAUTH_NOT_FOUND) or 410
    # (REAUTH_CONSUMED) means the challenge is gone server-side -- polling
    # further is pointless, so this must stop the chain entirely and tell
    # the user to refresh the QR code, instead of backing off and retrying.
    body = _reauth_poll_body(client, page)
    error_handler = re.search(r"\.catch\(function \(e\) \{(.*?)\n      \}\);", body, re.S)
    assert error_handler, "the poll's .catch(function (e) {...}) handler not found"
    error_body = error_handler.group(1)
    gone_branch = re.search(r'if \(e && \(e\.code === "REAUTH_NOT_FOUND" \|\| e\.code === "REAUTH_CONSUMED"\)\) \{(.*?)\n\s*\}', error_body, re.S)
    assert gone_branch, "the 404/410 'challenge gone' branch not found"
    gone_body = gone_branch.group(1)
    assert "views.reauth.stopPolling();" in gone_body
    assert "state.reauth.challengeId = null;" in gone_body
    assert "该授权流程已结束，请刷新二维码" in gone_body
    assert "setTimeout" not in gone_body, "must stop, not reschedule, when the challenge is gone"


def test_reauth_poll_shows_confirmed_message_and_keeps_polling(client, page):
    # w6-reauth-longpoll-fix item 1: 'confirmed' (115 confirmed the QR, the
    # cookie exchange is a deferred follow-up request) must show its own
    # message and keep polling, same as "scanned".
    body = _reauth_poll_body(client, page)
    confirmed = re.search(r'if \(d\.state === "confirmed"\) \{(.*?)\n        \}', body, re.S)
    assert confirmed, "the 'confirmed' branch not found"
    assert "已确认，正在完成登录…" in confirmed.group(1)
    # keeps polling: must not clear challengeId or stop the chain.
    assert "state.reauth.challengeId = null" not in confirmed.group(1)
    assert "stopPolling" not in confirmed.group(1)


def test_reauth_button_honours_availability_and_retry_after(client, page):
    # Item 8: reauth_available disables the button with a fixed text; a
    # rate-limited session shows retry_after.
    js = _static_js(client, page)
    assert "reauth_available" in js
    assert "请稍后再试" in js
    assert "retry_after" in js


def test_reauth_button_is_offered_in_every_non_valid_115_state():
    # Production after T19 showed cookie_state "unknown" (115 answered the
    # verification with a non-JSON login page) and no way to re-authorise:
    # the button used to appear only for reauth_required/unconfigured.
    from pathlib import Path
    js = Path(__file__).resolve().parent.parent.joinpath("static", "app.js").read_text(encoding="utf-8")
    start = js.index("function renderCookie115Status(")
    body = js[start:js.index("\n  }\n", start)]
    assert 'var showBtn = n.cookie_state !== "valid";' in body
    assert 'n.cookie_state === "reauth_required" || n.cookie_state === "unconfigured"' not in body
    assert "btn.hidden = !showBtn;" in body


def test_transfer_reauth_required_shows_settings_link_without_auto_resubmit(client, page):
    js = _static_js(client, page)
    match = re.search(r"function renderTransferError\(e\) \{(.*?)\n  \}", js, re.S)
    assert match, "renderTransferError not found"
    body = match.group(1)
    assert "115_REAUTH_REQUIRED" in body
    assert "transferGotoSettings" in body
    assert 'router.go("settings")' in body
    assert "api.request(" not in body, "must never resubmit automatically"


def test_transfer_error_fallback_renders_message_for_transfer_unknown(client, page):
    # TRANSFER_UNKNOWN's fixed check-records message comes from the backend
    # response body -- the generic error fallback just renders it as-is.
    js = _static_js(client, page)
    assert 'feedback("transferResult", e.message, "error")' in js


def _reauth_region(js: str) -> str:
    match = re.search(r"views\.reauth = \{(.*?)\n  \};", js, re.S)
    assert match, "views.reauth object not found"
    return match.group(1)


def test_transfer_goto_settings_records_return_to_for_reauth(client, page):
    # Brief w5-reauth-return §2: reaching the QR flow via the transfer
    # dialog's "前往设置页" button must remember where to come back to.
    js = _static_js(client, page)
    match = re.search(r"function renderTransferError\(e\) \{(.*?)\n  \}", js, re.S)
    assert match, "renderTransferError not found"
    body = match.group(1)
    assert re.search(r"state\.reauth\.returnTo\s*=\s*\{[^}]*tab:[^}]*linkId:[^}]*title:[^}]*\}", body), \
        "transferGotoSettings must record {tab, linkId, title} before leaving"
    assert 'router.go("settings")' in body


def test_reauth_close_clears_return_to(client, page):
    region = _reauth_region(_static_js(client, page))
    match = re.search(r"close: function \(\) \{(.*?)\n    \},", region, re.S)
    assert match, "views.reauth.close not found"
    assert "state.reauth.returnTo = null;" in match.group(1)


def test_reauth_success_always_closes_even_if_refresh_status_rejects(client, page):
    # Brief w5-reauth-return §1: the dialog must close ~900ms after
    # "authenticated" whether refreshStatus() resolves or rejects -- it
    # must never depend on that promise settling successfully.
    region = _reauth_region(_static_js(client, page))
    poll_match = re.search(r"poll: function \(\) \{(.*?)\n    \},", region, re.S)
    assert poll_match, "views.reauth.poll not found"
    authed = re.search(r'if \(d\.state === "authenticated"\) \{(.*?)\n        \}', poll_match.group(1), re.S)
    assert authed, "authenticated branch not found"
    branch = authed.group(1)

    then_call = re.search(r"refreshStatus\(\)\.then\((.*?)\);\s*\n\s*setTimeout", branch, re.S)
    assert then_call, "refreshStatus().then(...) must run before the close timeout is scheduled"
    assert then_call.group(1).count("function (") >= 2, \
        "refreshStatus().then needs a rejection handler, not just a success one"
    assert "setTimeout" not in then_call.group(1), \
        "the close must not be scheduled from inside refreshStatus()'s callback"
    assert re.search(r"setTimeout\(function \(\) \{\s*views\.reauth\.close\(\);", branch), \
        "close must be scheduled unconditionally"


def test_reauth_success_with_return_to_reopens_transfer_without_resubmitting(client, page):
    # Brief w5-reauth-return §2/§3/§5: with returnTo set, success must
    # navigate back and reopen the SAME transfer dialog with folders
    # reloaded -- and must never auto-resubmit the transfer.
    region = _reauth_region(_static_js(client, page))
    poll_match = re.search(r"poll: function \(\) \{(.*?)\n    \},", region, re.S)
    assert poll_match, "views.reauth.poll not found"
    authed = re.search(r'if \(d\.state === "authenticated"\) \{(.*?)\n        \}', poll_match.group(1), re.S)
    assert authed, "authenticated branch not found"
    branch = authed.group(1)

    assert "var returnTo = state.reauth.returnTo;" in branch
    guarded = re.search(r"if \(returnTo\) \{(.*?)\n\s*\}", branch, re.S)
    assert guarded, "router.go/reopen must be gated on returnTo (absent -> stay on settings)"
    guard_body = guarded.group(1)
    assert "router.go(returnTo.tab)" in guard_body
    assert "views.transfer.openLibrary(returnTo.linkId, returnTo.title)" in guard_body
    assert "state.transferFoldersLoaded = false;" in guard_body, "folder list must be reloaded on reopen"
    assert "/api/library/transfer" not in branch, "must never auto-resubmit the transfer"
    assert "saveBtn" not in branch, "must never auto-click/resubmit the save button"


def test_dynamic_responses_are_not_cached(client):
    assert client.get("/").headers["Cache-Control"] == "no-store"
    assert client.get("/api/status").headers["Cache-Control"] == "no-store"
    assert "Cache-Control" not in client.get("/healthz").headers


def test_unknown_api_route_returns_json_error(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.get_json()["success"] is False


@pytest.fixture
def app_css():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent.joinpath("static", "app.css").read_text(encoding="utf-8")


def test_light_theme_adopts_re0_neutral_tokens(app_css):
    for token in ("#f5f6f9", "#181822", "#dcdeea"):
        assert token in app_css, token
    assert "#007aff" in app_css


def test_460px_breakpoint_hides_tagline_keeps_compact_brand(app_css):
    # T7 §6: the brand wordmark must stay visible (just smaller) at narrow
    # widths -- only the tagline paragraph hides. A fully-hidden brand left
    # the header with no wordmark at all on real phones.
    marker = "@media (max-width:460px)"
    assert marker in app_css, marker
    open_brace = app_css.index("{", app_css.index(marker))
    depth = 0
    end = open_brace
    for i, ch in enumerate(app_css[open_brace:], start=open_brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    block = app_css[open_brace:end + 1]
    assert ".brand p" in block
    assert re.search(r'\.brand p\s*\{[^}]*display\s*:\s*none', block), "tagline should stay hidden"
    assert ".brand h1" in block
    assert not re.search(r'\.brand h1\s*\{[^}]*display\s*:\s*none', block), \
        "brand wordmark must stay visible (compact), not hidden"


def _relative_luminance(hex_color: str) -> float:
    """WCAG 2.x relative luminance of a ``#rrggbb`` colour."""
    hex_color = hex_color.lstrip("#")
    channels = (int(hex_color[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def linearize(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linearize(c) for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(hex_a: str, hex_b: str) -> float:
    l1, l2 = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _brace_block(css: str, open_brace: int) -> str:
    depth = 0
    end = open_brace
    for i, ch in enumerate(css[open_brace:], start=open_brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    return css[open_brace:end + 1]


def _root_block(css: str, *, dark: bool) -> str:
    start = css.index("@media (prefers-color-scheme: dark)") if dark else 0
    root_at = css.index(":root", start)
    return _brace_block(css, css.index("{", root_at))


def _token_hex(css_block: str, token: str) -> str | None:
    match = re.search(re.escape(token) + r":\s*(#[0-9a-fA-F]{6})", css_block)
    return match.group(1) if match else None


def test_primary_button_background_meets_wcag_aa_contrast_with_white_text(app_css):
    """The bare ``button{}`` rule is HiDrive-Lite's primary/default button
    style (forms, dialogs, etc. all fall back to it unless .secondary/
    .tertiary applies). Its white (#fff) text must read at >=4.5:1 against
    whatever custom property backs its background, in both the light
    ``:root`` tokens and the dark theme's `@media (prefers-color-scheme:
    dark)` override (if that property is itself overridden there)."""
    button_match = re.search(r"(?:^|\})\s*button\{([^}]*)\}", app_css)
    assert button_match, "primary `button` rule not found"
    button_rule = button_match.group(1)
    assert "color:#fff" in button_rule

    bg_match = re.search(r"background:var\((--[a-zA-Z-]+)\)", button_rule)
    assert bg_match, "button background does not reference a CSS custom property"
    token = bg_match.group(1)

    light_hex = _token_hex(_root_block(app_css, dark=False), token)
    assert light_hex, f"{token} not defined in the light :root"
    assert _contrast_ratio("#ffffff", light_hex) >= 4.5, (
        f"white on {token}={light_hex} (light theme) is below WCAG AA 4.5:1"
    )

    dark_hex = _token_hex(_root_block(app_css, dark=True), token)
    if dark_hex is not None:
        assert _contrast_ratio("#ffffff", dark_hex) >= 4.5, (
            f"white on {token}={dark_hex} (dark theme override) is below WCAG AA 4.5:1"
        )


# ----------------------------------------------------------------------
# w5-csrf-refresh: api.request must retry a non-GET request exactly once
# after refreshing an expired/rejected CSRF token, and a page left open
# must proactively refresh the token every 20 minutes.
# ----------------------------------------------------------------------

def _api_object_source(js: str) -> str:
    match = re.search(r"(var api = \{[\s\S]*?\n  \};)", js)
    assert match, "expected the `var api = { ... };` object literal"
    return match.group(1)


def _run_node(harness: str) -> dict:
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _fake_fetch_harness(api_src: str, target_responses, csrf_token_prefix="fake-fresh-token-") -> str:
    """Builds a node harness with a fake `fetch`: `target_responses` is a
    list of (status, body) tuples returned for the non-csrf URL in order
    (the last entry repeats for any further calls); /api/csrf always
    succeeds with an incrementing token."""
    return api_src + """
var targetResponses = """ + json.dumps(target_responses) + """;
var calls = [];
var csrfCallCount = 0;
var targetCallCount = 0;
function makeResp(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    url: "https://example.test/resp",
    headers: { get: function () { return "application/json"; } },
    json: function () { return Promise.resolve(body); }
  };
}
function fetch(url, opt) {
  calls.push({
    url: url,
    method: (opt && opt.method) || "GET",
    body: opt && opt.body,
    csrfHeader: opt && opt.headers && opt.headers["X-CSRF-Token"]
  });
  if (url === "/api/csrf") {
    csrfCallCount += 1;
    return Promise.resolve(makeResp(200, { success: true, token: \"""" + csrf_token_prefix + """\" + csrfCallCount }));
  }
  var idx = Math.min(targetCallCount, targetResponses.length - 1);
  targetCallCount += 1;
  return Promise.resolve(makeResp(targetResponses[idx][0], targetResponses[idx][1]));
}
"""


def test_csrf_rejected_post_refreshes_token_and_retries_once(client, page):
    js = _static_js(client, page)
    api_src = _api_object_source(js)
    harness = _fake_fetch_harness(api_src, [
        [403, {"success": False, "message": "invalid CSRF token"}],
        [200, {"success": True, "done": True}],
    ]) + """
api.csrf = "stale-token";
api.csrfFetchedAt = 0;
api.request("/api/library/transfer", { method: "POST", body: JSON.stringify({ x: 1 }) }).then(function (d) {
  console.log(JSON.stringify({ result: d, finalCsrf: api.csrf, calls: calls, csrfCallCount: csrfCallCount, targetCallCount: targetCallCount }));
}).catch(function (e) {
  console.log(JSON.stringify({ error: e.message }));
});
"""
    out = _run_node(harness)
    assert "error" not in out, out
    assert out["result"] == {"success": True, "done": True}
    assert out["targetCallCount"] == 2, "expected exactly one retry of the same request"
    assert out["csrfCallCount"] == 1, "expected exactly one /api/csrf refetch"
    assert out["finalCsrf"] == "fake-fresh-token-1"

    target_calls = [c for c in out["calls"] if c["url"] == "/api/library/transfer"]
    assert len(target_calls) == 2
    assert target_calls[0]["csrfHeader"] == "stale-token"
    assert target_calls[1]["csrfHeader"] == "fake-fresh-token-1"
    # same method/body on the retry
    assert target_calls[0]["method"] == target_calls[1]["method"] == "POST"
    assert target_calls[0]["body"] == target_calls[1]["body"]
    assert json.loads(target_calls[0]["body"]) == {"x": 1}

    # /api/csrf must be fetched BEFORE the retry, not after
    csrf_index = next(i for i, c in enumerate(out["calls"]) if c["url"] == "/api/csrf")
    retry_index = next(i for i, c in enumerate(out["calls"]) if c["url"] == "/api/library/transfer" and c["csrfHeader"] == "fake-fresh-token-1")
    assert csrf_index < retry_index


def test_csrf_retry_never_fires_for_other_403_messages(client, page):
    js = _static_js(client, page)
    api_src = _api_object_source(js)
    harness = _fake_fetch_harness(api_src, [
        [403, {"success": False, "message": "forbidden"}],
    ]) + """
api.csrf = "stale-token";
api.request("/api/library/transfer", { method: "POST", body: "{}" }).then(function (d) {
  console.log(JSON.stringify({ result: d, csrfCallCount: csrfCallCount, targetCallCount: targetCallCount }));
}).catch(function (e) {
  console.log(JSON.stringify({ error: e.message, csrfCallCount: csrfCallCount, targetCallCount: targetCallCount }));
});
"""
    out = _run_node(harness)
    assert out.get("error") == "forbidden"
    assert out["targetCallCount"] == 1, "must not retry a non-CSRF 403"
    assert out["csrfCallCount"] == 0, "must not refetch /api/csrf for a non-CSRF 403"


def test_csrf_retry_never_fires_for_get_requests(client, page):
    js = _static_js(client, page)
    api_src = _api_object_source(js)
    harness = _fake_fetch_harness(api_src, [
        [403, {"success": False, "message": "invalid CSRF token"}],
    ]) + """
api.csrf = "stale-token";
api.request("/api/status", { method: "GET" }).then(function (d) {
  console.log(JSON.stringify({ result: d, csrfCallCount: csrfCallCount, targetCallCount: targetCallCount }));
}).catch(function (e) {
  console.log(JSON.stringify({ error: e.message, csrfCallCount: csrfCallCount, targetCallCount: targetCallCount }));
});
"""
    out = _run_node(harness)
    assert out.get("error") == "invalid CSRF token"
    assert out["targetCallCount"] == 1, "must never retry a GET"
    assert out["csrfCallCount"] == 0, "must never refetch /api/csrf for a GET"


def test_csrf_retry_fires_at_most_once_even_if_still_rejected(client, page):
    js = _static_js(client, page)
    api_src = _api_object_source(js)
    harness = _fake_fetch_harness(api_src, [
        [403, {"success": False, "message": "invalid CSRF token"}],
        [403, {"success": False, "message": "invalid CSRF token"}],
        [403, {"success": False, "message": "invalid CSRF token"}],
    ]) + """
api.csrf = "stale-token";
api.request("/api/library/transfer", { method: "POST", body: "{}" }).then(function (d) {
  console.log(JSON.stringify({ result: d, csrfCallCount: csrfCallCount, targetCallCount: targetCallCount }));
}).catch(function (e) {
  console.log(JSON.stringify({ error: e.message, csrfCallCount: csrfCallCount, targetCallCount: targetCallCount }));
});
"""
    out = _run_node(harness)
    assert out.get("error") == "invalid CSRF token"
    assert out["targetCallCount"] == 2, "expected the initial attempt plus exactly one retry, no more"
    assert out["csrfCallCount"] == 1, "expected exactly one /api/csrf refetch, not one per attempt"


def test_proactive_csrf_refresh_interval_and_visibility_hook(client, page):
    js = _static_js(client, page)

    const_match = re.search(r"var (\w+)\s*=\s*20\s*\*\s*60\s*\*\s*1000;", js)
    assert const_match, "expected a 20-minute (20 * 60 * 1000 ms) CSRF refresh constant"
    const_name = const_match.group(1)

    interval_match = re.search(
        r"setInterval\(function\s*\(\)\s*\{([\s\S]*?)\},\s*" + re.escape(const_name) + r"\)",
        js,
    )
    assert interval_match, f"expected setInterval(..., {const_name}) refreshing the CSRF token"
    assert "csrf" in interval_match.group(1).lower()

    vis_match = re.search(
        r'addEventListener\("visibilitychange",\s*function\s*\(\)\s*\{([\s\S]*?)\n\s*\}\);',
        js,
    )
    assert vis_match, "expected a visibilitychange listener"
    vis_body = vis_match.group(1)
    assert 'document.visibilityState === "visible"' in vis_body
    assert "api.csrfFetchedAt" in vis_body
    assert const_name in vis_body
