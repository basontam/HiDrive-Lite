"""Static page/JS/CSS assertions for the media-library views (T5.3-T5.7).

The backend `/api/library/*` routes are being built in a sibling worktree and
are not importable here, so every check in this file is a *static* assertion
against the rendered HTML, the `static/app.js` source text and `static/app.css`
source text -- exactly as the brief's "测试" section prescribes. No test in
this file performs a live request to a `/api/library/*` endpoint.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import library_normalize  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@pytest.fixture
def page(client):
    response = client.get("/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _static_js(client, page_html: str) -> str:
    match = re.search(r'(/static/app\.js\?v=[0-9a-f]{8})', page_html)
    assert match, "page must reference a versioned /static/app.js"
    response = client.get(match.group(1))
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.fixture
def js(client, page):
    return _static_js(client, page)


@pytest.fixture
def css():
    return STATIC_DIR.joinpath("app.css").read_text(encoding="utf-8")


def _has_id(html: str, element_id: str) -> bool:
    return re.search(rf'id="{re.escape(element_id)}"', html) is not None


def _tag_block(html: str, element_id: str) -> str:
    """Return the opening tag for the element with this id (attrs only)."""
    match = re.search(rf'<[a-zA-Z0-9]+[^>]*\bid="{re.escape(element_id)}"[^>]*>', html)
    assert match, f"no element with id={element_id!r}"
    return match.group(0)


# ---------------------------------------------------------------------------
# T5.3 library home: search, suggest, chips, filters, results, pagination
# ---------------------------------------------------------------------------

LIBRARY_HOME_IDS = (
    "libraryQuery", "libraryType", "librarySearch", "librarySuggest", "libraryChips",
    "libraryFilters", "filterYear", "filterProviders", "filterQuality", "filterHdr",
    "filterDeleted", "filterSort", "libraryStatus", "libraryResults", "libraryPagination",
    "libraryEmpty", "libraryError", "libraryNotInstalled",
)


def test_library_home_element_ids_present(page):
    for element_id in LIBRARY_HOME_IDS:
        assert _has_id(page, element_id), element_id


def test_library_type_segmented_has_four_values(page):
    block_match = re.search(r'<[^>]*\bid="libraryType"[^>]*>.*?</div>\s*(?=<)', page, re.S)
    assert block_match, "libraryType block not found"
    block = block_match.group(0)
    for value in ("all", "movie", "tv", "unknown"):
        assert f'data-value="{value}"' in block, value


def test_library_type_unknown_option_matches_card_and_detail_wording(page):
    # Cards/detail localise media_type "unknown" as 未分类 (MEDIA_TYPE_LABEL);
    # the #libraryType segmented control must use the same word, not 待定.
    block_match = re.search(r'<[^>]*\bid="libraryType"[^>]*>.*?</div>\s*(?=<)', page, re.S)
    assert block_match, "libraryType block not found"
    block = block_match.group(0)
    assert re.search(r'data-value="unknown"[^>]*>未分类<', block)
    assert "待定" not in block


def test_library_suggest_is_listbox(page):
    tag = _tag_block(page, "librarySuggest")
    assert 'role="listbox"' in tag


def test_library_manual_transfer_disclosure_removed(page, js):
    for element_id in ("libraryManual", "manualShareUrl", "manualOpenTransfer", "libraryManualResult"):
        assert not _has_id(page, element_id), element_id
        assert element_id not in js, element_id


def test_library_result_state_containers_default_hidden(page):
    for element_id in ("libraryResults", "libraryEmpty", "libraryError", "libraryNotInstalled"):
        tag = _tag_block(page, element_id)
        assert "hidden" in tag, element_id


# ---------------------------------------------------------------------------
# JS behaviour: search/suggest wiring, debounce, URL state, never calls TMDB
# ---------------------------------------------------------------------------

def test_library_js_calls_search_and_suggest_endpoints(js):
    assert '/api/library/search' in js
    assert '/api/library/suggest' in js
    assert '/api/library/filters' in js


def test_library_suggest_never_calls_tmdb(js):
    suggest_region_match = re.search(r'suggest[\s\S]{0,1200}', js, re.I)
    assert suggest_region_match
    region = suggest_region_match.group(0)
    assert "themoviedb" not in region.lower()
    assert "api.tmdb" not in region.lower()


def test_library_suggest_is_debounced_200ms(js):
    assert re.search(r'setTimeout\([^)]*,\s*200\)', js), "expected a 200ms debounce timer"


def test_library_suggest_skips_short_queries(js):
    # Requires >=1 CJK char or >=2 latin letters before firing a request.
    assert re.search(r'cjk', js, re.I), "expected a CJK character count guard"
    assert re.search(r'latin\s*>=\s*2', js), "expected a >=2 latin-letter guard"


def test_library_url_state_keys_present(js):
    for key in ("q", "type", "year", "provider", "quality", "hdr", "sort", "page"):
        assert re.search(rf'["\']({key})["\']', js), key
    assert "popstate" in js


def test_library_card_keyboard_activation(js):
    assert 'class="media-card' in js or "class=\\'media-card" in js
    region_match = re.search(r'onkeydown[\s\S]{0,400}', js)
    assert region_match, "expected an onkeydown handler on the media card"
    region = region_match.group(0)
    assert '"Enter"' in region or "'Enter'" in region
    assert '" "' in region or "' '" in region or "Space" in region


def test_library_uses_esc_for_dynamic_card_titles(js):
    region_match = re.search(r'renderResults[\s\S]{0,2000}', js)
    assert region_match, "expected a renderResults function"
    assert "esc(" in region_match.group(0)


def test_library_no_console_log(js):
    assert "console.log(" not in js


# ---------------------------------------------------------------------------
# T5.4 detail view: containers, reveal/copy actions, no leaked secrets
# ---------------------------------------------------------------------------

DETAIL_STATIC_IDS = (
    "libraryDetail", "detailBack", "detailContent", "revealPanel",
    "revealCode", "revealCodeToggle", "revealFallbackRow", "revealFallback",
)


def test_detail_static_ids_present(page):
    for element_id in DETAIL_STATIC_IDS:
        assert _has_id(page, element_id), element_id


def test_detail_container_mutually_exclusive_with_results(page):
    detail_tag = _tag_block(page, "libraryDetail")
    assert "hidden" in detail_tag
    home_tag = _tag_block(page, "libraryHome")
    assert "hidden" not in home_tag  # visible by default; JS toggles it


def test_detail_access_code_placeholder_is_masked(page):
    tag_region = re.search(r'<[^>]*\bid="revealCode"[^>]*>([^<]*)</code>', page)
    assert tag_region, "revealCode element not found"
    assert tag_region.group(1).strip() == "••••"


def test_detail_js_calls_media_endpoint(js):
    assert "/api/library/media/" in js


def test_detail_js_calls_reveal_endpoint_and_uses_noopener(js):
    reveal_match = re.search(r'/api/library/link/[\s\S]{0,600}', js)
    assert reveal_match, "expected a call to the link reveal endpoint"
    assert "/reveal" in reveal_match.group(0)
    window_open_match = re.search(r'window\.open\([^)]*\)', js)
    assert window_open_match, "expected a window.open(...) call"
    assert "noopener" in window_open_match.group(0)


def test_detail_js_window_open_guarded_by_http_scheme_regex(js):
    # C1 fix: window.open must never fire for a non-http(s) URL (ed2k/
    # magnet -- and never javascript:/vbscript:/file: -- only ever reach
    # the "copy" action, never "open"). Belt-and-suspenders scheme check
    # right at the point window.open is called.
    match = re.search(r'[\s\S]{0,160}window\.open\(', js)
    assert match, "expected a window.open( call"
    snippet = match.group(0)
    assert re.search(r'\^https\?:\\/\\/', snippet), (
        "window.open must be guarded by an http(s)-only scheme regex test"
    )
    assert ".test(" in snippet


def test_detail_group_and_link_row_markup(js):
    # T18 §13: groups are ".detail-group[data-group-id]" rows whose
    # ".link-row"s render immediately -- no expand/collapse control at all.
    assert "detail-group" in js
    assert "data-group-id" in js
    assert "link-row" in js
    assert "data-link-id" in js


def test_detail_action_labels_present(js):
    for label in ("转存到 115", "打开", "复制", "复制 ed2k", "链接不可用"):
        assert label in js, label


def test_detail_no_secret_data_attributes(js):
    assert "data-url=" not in js
    assert "data-access-code=" not in js
    assert "data-access_code=" not in js


def test_detail_no_console_log_with_url_or_access_code(js):
    for match in re.finditer(r'console\.log\(([^)]*)\)', js):
        arg = match.group(1).lower()
        assert "url" not in arg and "access_code" not in arg and "accesscode" not in arg


def test_no_group_expand_toggle_or_cache_anywhere(js, css):
    # T18 §13.1/§13.5 "must-delete" list: the old expand/collapse
    # mechanism is gone entirely -- no button/class/state/wording for it,
    # and no lazy per-group resource fetch.
    for needle in ("toggleGroup", "renderGroupPanel", "state.detail.groupCache", "group-expand"):
        assert needle not in js, needle
    assert ".group-expand" not in css
    # The old lazy per-group fetch call is gone (a comment may still
    # mention the route name as historical context, so this checks the
    # actual call pattern, not a bare substring) -- "展开"/"收起" still
    # exist for the UNRELATED overview toggle (detailOverviewToggle), so
    # those two words are not asserted absent here. w6-contract added a
    # legitimate NEW call to this same URL prefix (POST .../recheck, an
    # explicit user-triggered action button -- not a lazy per-group GET
    # fetch on expand), so every occurrence of the prefix must belong to
    # that recheck call, never a bare whole-group GET.
    resource_call_lines = [line for line in js.splitlines() if '.request("/api/library/resource/' in line]
    assert resource_call_lines, "expected the w6-contract recheck POST call"
    for line in resource_call_lines:
        assert "recheck" in line and '"POST"' in line, f"unexpected /api/library/resource/ call: {line}"


def test_all_link_rows_render_without_a_prior_expand_click(js):
    # T18 §13.1/§13.6: every group's links come straight from
    # `group.links` in the initial media response -- groupRowHtml builds
    # every link row unconditionally, not behind any expanded/collapsed
    # state.
    match = re.search(r'groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a groupRowHtml function"
    body = match.group(1)
    assert "group.links" in body
    assert "links.map(views.detail.linkRowHtml)" in body
    assert "暂无可用链接" in body


# ---------------------------------------------------------------------------
# T5.5 transfer dialog: library-link mode vs. manual mode
# ---------------------------------------------------------------------------

def test_transfer_dialog_library_mode_ids_present(page):
    for element_id in ("transferLinkId", "transferServerHeld", "transferServerHeldField", "transferShareField"):
        assert _has_id(page, element_id), element_id
    block = _tag_block(page, "transferServerHeldField")
    assert "hidden" in block


def test_transfer_server_held_text(page):
    match = re.search(r'<[^>]*\bid="transferServerHeld"[^>]*>([^<]*)</div>', page)
    assert match, "transferServerHeld content not found"
    assert "115" in match.group(1)
    assert "访问码" in match.group(1)


def test_transfer_library_request_body_has_resource_link_id_target_path_and_target_pid(js):
    # T18 §12.5: the library-link flow now submits its currently-known
    # (possibly empty) target_pid alongside target_path in the same
    # request -- an empty string is indistinguishable from an absent field
    # to _resolve_115_target_pid (a falsy target_pid is treated as not
    # given), so always including the key is exactly equivalent to the old
    # "only send it when known" behaviour, never a fabricated pid.
    match = re.search(r'"/api/library/transfer"[\s\S]{0,400}?JSON\.stringify\(\{([^}]*)\}\)', js)
    assert match, "expected a JSON.stringify({...}) body near /api/library/transfer"
    body = match.group(1)
    assert "resource_link_id" in body
    assert "target_path" in body
    assert "target_pid" in body
    assert "currentTransferPid" in body
    for forbidden in ("access_code", "url:", "url ", "share_url"):
        assert forbidden not in body, forbidden


def test_transfer_dialog_no_longer_sends_share_url(js):
    # u4/w4-home-manual: the manual share-link flow (and its
    # /api/115/save share_url submission) is gone -- the transfer dialog
    # only ever submits the library-link flow now.
    assert "share_url" not in js
    assert '"/api/115/save"' not in js


def test_transfer_dialog_has_no_folder_choose_step_or_second_primary_button(page, js):
    # T18 §12.5/§12.6: "选择此目录" is gone -- the browsed directory IS the
    # target, so the dialog footer has exactly 取消 + one 转存到 115 button.
    assert "folderChoose" not in page
    assert "选择此目录" not in page
    assert "folderChoose" not in js
    modal_actions = re.search(r'<div class="modal-actions">([\s\S]*?)</div>', page)
    assert modal_actions, "expected the transfer dialog's .modal-actions footer"
    footer = modal_actions.group(1)
    assert footer.count("<button") == 2
    assert "transferCancel" in footer
    assert 'id="saveBtn"' in footer
    assert "转存到 115" in footer


def test_load_folders_keeps_current_transfer_path_and_pid_in_lockstep(js):
    # T18 §12.5: currentTransferPid is only ever set to a pid this client
    # can prove (the OpenList mount root's own 115_open_root_cid) when the
    # browsed path is exactly that root, and cleared for any subfolder --
    # never invented for an arbitrary path.
    match = re.search(r'loadFolders:\s*function\s*\(path\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a loadFolders function"
    body = match.group(1)
    assert "state.currentTransferPath = path" in body
    assert 'state.currentTransferPid = path === state.transferRoot ? state.transferRootPid : ""' in body
    assert '$("folderCurrent").textContent = path' in body
    assert '$("targetPath").textContent = path' in body


def test_transfer_dialog_guards_close_while_busy(js):
    # Both dialog-close triggers share views.transfer.close, which is
    # where the actual busy guard lives -- the previous version of this
    # test only checked that "transferClose"/"transferCancel" appeared
    # somewhere in the next 300 characters of source, which an empty
    # (zero-width) match always satisfies regardless of what's there, so
    # it could never actually fail.
    transfer_module = re.search(r"views\.transfer\s*=\s*\{([\s\S]*?)\n  \};", js)
    assert transfer_module, "expected the views.transfer module"
    module_body = transfer_module.group(1)
    for handler in ("transferClose", "transferCancel"):
        assert f'$("{handler}").onclick = views.transfer.close' in module_body, handler
    close_match = re.search(r"close:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},", module_body)
    assert close_match, "expected a views.transfer.close function"
    body = close_match.group(1)
    assert re.search(r"if\s*\(state\.transferBusy\)\s*\{[^}]*return", body), (
        "close() must bail out while a transfer is in flight"
    )
    assert '$("transferDialog").hidden = true' in body


def test_transfer_dialog_focus_management(js):
    assert ".focus()" in js
    assert "activeElement" in js


# ---------------------------------------------------------------------------
# T5.6 settings: three status cards, TMDB budget/enrich fields, statusList
# ---------------------------------------------------------------------------

SETTINGS_IDS = (
    "cookie115", "tmdbKey", "tmdbBudget", "tmdbEnrichEnabled", "settingPid",
    "settingsSave", "statusList", "settingsHelp",
    "tmdbScrapeStatus", "tmdbCheck", "tmdbEnrichNow", "tmdbScrapeResult",
)


def test_settings_element_ids_present(page):
    for element_id in SETTINGS_IDS:
        assert _has_id(page, element_id), element_id


def test_settings_no_hdhive_input_ids(page, js):
    for element_id in ("hdSecret", "hdClient", "oauthBtn"):
        assert not _has_id(page, element_id), element_id
        assert element_id not in js, element_id


def test_settings_tmdb_budget_is_a_bounded_number_input(page):
    tag = re.search(r'<input id="tmdbBudget"[^>]*>', page)
    assert tag, "tmdbBudget input not found"
    attrs = tag.group(0)
    assert 'type="number"' in attrs
    assert 'min="1"' in attrs
    assert 'max="5000"' in attrs


def test_settings_tmdb_enrich_is_a_checkbox(page):
    tag = re.search(r'<input id="tmdbEnrichEnabled"[^>]*>', page)
    assert tag, "tmdbEnrichEnabled input not found"
    assert 'type="checkbox"' in tag.group(0)


def test_refresh_library_status_seeds_enrich_checkbox_from_server(js):
    # I4: #tmdbEnrichEnabled defaults to unchecked in the HTML and was
    # never initialised from server state -- saving settings (which
    # always sends the checkbox's current .checked) would silently
    # disable enrichment on the very first save. refreshLibraryStatus
    # must seed it from the tmdb-status payload's enrich_enabled field.
    match = re.search(r'function refreshLibraryStatus\(\)[\s\S]{0,2600}?\n  \}', js)
    assert match, "expected refreshLibraryStatus function"
    body = match.group(0)
    assert re.search(r'\$\("tmdbEnrichEnabled"\)\.checked = .*enrich_enabled', body)


def test_refresh_library_status_seeds_budget_input_from_configured_not_effective(js):
    # T1 P0 fix: the input must be seeded from the *configured* budget
    # (what the operator asked for), never the effective/env-capped one --
    # seeding from the old d.budget.budget field showed a value the
    # operator never actually set.
    match = re.search(r'function refreshLibraryStatus\(\)[\s\S]{0,2600}?\n  \}', js)
    assert match, "expected refreshLibraryStatus function"
    body = match.group(0)
    assert re.search(r'\$\("tmdbBudget"\)\.value = .*configured_budget', body)
    assert "d.budget.budget" not in body


def test_settings_save_writes_configured_budget_back_into_input(js):
    # After a successful settings save, the response's tmdb.configured_budget
    # (when present) must be written into #tmdbBudget. Scoped to the save
    # handler's own source region (like its sibling test above) so this
    # fails if the writeback inside $("settingsSave").onclick is removed --
    # an unscoped search of the whole file would still pass on
    # refreshLibraryStatus's unrelated seed of the same input.
    match = re.search(r'\$\("settingsSave"\)\.onclick = function \(\)[\s\S]{0,1600}?\n      \};', js)
    assert match, "expected settingsSave onclick handler"
    body = match.group(0)
    assert re.search(r'\$\("tmdbBudget"\)\.value = .*configured_budget', body)


def test_refresh_library_status_lamp_uses_error_code_not_generic_pending(js):
    # Minor fix: the .catch() branch always showed a generic "待检查"
    # regardless of *why* the request failed -- distinguish 未安装 /
    # 未加密 / 索引不可读 via e.code so the settings lamp is informative.
    match = re.search(r'function refreshLibraryStatus\(\)[\s\S]{0,2600}?\n  \}', js)
    assert match, "expected refreshLibraryStatus function"
    body = match.group(0)
    catch_match = re.search(r'\.catch\(function \((\w*)\)[\s\S]*$', body)
    assert catch_match, "expected a .catch(function (e) {...}) handler"
    catch_body = catch_match.group(0)
    err_param = catch_match.group(1)
    assert err_param, "the catch handler must receive the error to read .code"
    assert (err_param + ".code") in catch_body
    assert "未加密" in catch_body
    assert "未安装" in catch_body


def test_settings_three_column_grid_at_desktop_and_two_at_1199(css):
    assert re.search(r'\.settings-grid\{[^}]*grid-template-columns:repeat\(3', css)
    block = re.search(r'@media \(max-width:1199px\)\{([\s\S]*?)\n\}', css)
    assert block, "expected a max-width:1199px media block"
    assert re.search(r'\.settings-grid\{[^}]*grid-template-columns:repeat\(2', block.group(1))


def test_settings_one_column_at_650(css):
    block = re.search(r'@media \(max-width:650px\)\{([\s\S]*?)\n\}', css)
    assert block, "expected a max-width:650px media block"
    assert re.search(r'\.settings-grid\{[^}]*grid-template-columns:1fr', block.group(1))


def test_settings_grid_children_equal_height(css):
    assert ".settings-grid>.card{height:100%" in css


def test_settings_save_sends_tmdb_budget_and_enrich_fields(js):
    region = re.search(r'settingsSave[\s\S]{0,900}', js)
    assert region, "expected the settingsSave click handler"
    assert "tmdb_daily_budget" in region.group(0)
    assert "tmdb_enrich_enabled" in region.group(0)


def test_settings_save_clears_password_inputs_after_save(js):
    region = re.search(r'settingsSave[\s\S]{0,900}', js)
    assert region
    assert '$("cookie115").value = ""' in region.group(0)
    assert '$("tmdbKey").value = ""' in region.group(0)


# ---------------------------------------------------------------------------
# T10 TMDB 刮削 card: self-diagnosis status block + check/enrich-now buttons
# + a dirty flag so a settings save can never silently disable enrichment.
# ---------------------------------------------------------------------------


def test_tmdb_scrape_card_heading_present(page):
    assert "TMDB 刮削" in page


def test_tmdb_check_and_enrich_now_are_buttons(page):
    for element_id in ("tmdbCheck", "tmdbEnrichNow"):
        tag = re.search(rf'<button[^>]*\bid="{element_id}"[^>]*>', page)
        assert tag, f"{element_id} must be a <button>"


def test_paused_reason_label_map_present(js):
    reason_labels = {
        "disabled": "未启用开关",
        "key_missing": "未填写 API Key",
        "not_installed": "索引未安装",
        "no_heartbeat": "后台线程尚未上报心跳",
        "stale": "心跳超时",
    }
    for reason, label in reason_labels.items():
        assert re.search(rf'{reason}:\s*"{label}"', js), reason


def test_tmdb_error_hint_map_present(js):
    assert "InvalidApiKey" in js
    assert "v3 auth key" in js
    assert "BudgetExhausted" in js
    assert "今日额度已用完" in js
    assert "无法连接 TMDB" in js


def test_tmdb_error_hint_maps_proxy_and_ssl_errors_to_network_hint(js):
    # T10 fix wave 1 #2: tmdbErrorHint must mirror app.py's _tmdb_error_hint
    # -- a ProxyError/SSLError (or any class name containing "Proxy"/"SSL")
    # is a network/proxy problem, not a generic "TMDB 请求失败：<class>".
    match = re.search(r"function tmdbErrorHint\(errorClass\)[\s\S]{0,400}?\n  \}", js)
    assert match, "expected tmdbErrorHint function"
    body = match.group(0)
    assert "Proxy" in body
    assert "SSL" in body


def test_enrich_switch_change_sets_a_dirty_flag(js):
    # T10: the switch must have a "change" listener that records the user
    # actually touched it in this page session -- the guard below only
    # works if something sets this flag.
    match = re.search(r'\$\("tmdbEnrichEnabled"\)\.addEventListener\("change",\s*function\s*\(\)\s*\{\s*state\.(\w+)\s*=\s*true;', js)
    assert match, "expected a change listener on #tmdbEnrichEnabled setting a state flag"
    flag_name = match.group(1)

    # And the settingsSave handler must only include tmdb_enrich_enabled in
    # the request body when that same flag is set -- an unconditional send
    # would silently disable enrichment on a save fired before
    # refreshLibraryStatus has ever seeded the checkbox from server state.
    region = re.search(r'\$\("settingsSave"\)\.onclick = function \(\)[\s\S]{0,1600}?\n      \};', js)
    assert region, "expected settingsSave onclick handler"
    assert re.search(r'if \(state\.' + flag_name + r'\) payload\["tmdb_enrich_enabled"\] = ', region.group(0))


def test_settings_save_resets_the_dirty_flag_on_success(js):
    # T10 fix wave 1 #4: once a save actually succeeds, the switch's dirty
    # flag must be cleared -- otherwise a stale "dirty" from an earlier page
    # visit would keep re-sending tmdb_enrich_enabled on every later save.
    match = re.search(r'\$\("tmdbEnrichEnabled"\)\.addEventListener\("change",\s*function\s*\(\)\s*\{\s*state\.(\w+)\s*=\s*true;', js)
    assert match, "expected a change listener on #tmdbEnrichEnabled setting a state flag"
    flag_name = match.group(1)

    region = re.search(r'\$\("settingsSave"\)\.onclick = function \(\)[\s\S]{0,1600}?\n      \};', js)
    assert region, "expected settingsSave onclick handler"
    assert re.search(r"state\." + flag_name + r"\s*=\s*false;", region.group(0))


def test_tmdb_check_button_posts_to_check_endpoint(js):
    region = re.search(r'\$\("tmdbCheck"\)\.onclick = function \(\)[\s\S]{0,900}?\n      \};', js)
    assert region, "expected tmdbCheck onclick handler"
    body = region.group(0)
    assert '"/api/library/tmdb-check"' in body
    assert '"POST"' in body


def test_tmdb_enrich_now_button_posts_to_enrich_now_endpoint(js):
    region = re.search(r'\$\("tmdbEnrichNow"\)\.onclick = function \(\)[\s\S]{0,900}?\n      \};', js)
    assert region, "expected tmdbEnrichNow onclick handler"
    body = region.group(0)
    assert '"/api/library/tmdb-enrich-now"' in body
    assert '"POST"' in body


def test_tmdb_check_and_enrich_now_disable_while_pending(js):
    for element_id in ("tmdbCheck", "tmdbEnrichNow"):
        region = re.search(rf'\$\("{element_id}"\)\.onclick = function \(\)[\s\S]{{0,900}}?\n      \}};', js)
        assert region, f"expected {element_id} onclick handler"
        body = region.group(0)
        assert f'$("{element_id}").disabled = true' in body
        assert f'$("{element_id}").disabled = false' in body


def test_refresh_library_status_renders_tmdb_scrape_status(js):
    match = re.search(r'function refreshLibraryStatus\(\)[\s\S]{0,2600}?\n  \}', js)
    assert match, "expected refreshLibraryStatus function"
    assert "renderTmdbScrapeStatus(d)" in match.group(0)


def test_render_tmdb_scrape_status_reads_progress_and_error_fields(js):
    match = re.search(r'function renderTmdbScrapeStatus\(d\)[\s\S]{0,1600}?\n  \}', js)
    assert match, "expected renderTmdbScrapeStatus function"
    body = match.group(0)
    for field in (
        "worker_state", "paused_reason", "matched_total", "total_media", "needs_review_count", "last_round_at",
        "last_round_processed", "last_error", "cap_source", "configured_budget", "effective_budget",
        # T14/§3.1 & §4, fix wave 1 (finding #4): the three review-backlog
        # buckets, their total, and today's 429 count.
        "review_pending_unqueried", "review_scored", "review_no_candidate", "needs_review_total",
        "tmdb_requests_429",
    ):
        assert f"d.{field}" in body, field


STATUS_LABELS = (
    "Cloudflare Access", "115 Cookie（转存）", "115 开放平台令牌", "后台授权",
    "OpenList API Token", "Infuse STRM 目录", "资源库索引", "TMDB 补全",
)


def test_status_list_has_eight_rows(js):
    for label in STATUS_LABELS:
        assert f'"{label}"' in js, label


def test_status_list_no_longer_shows_substituted_115_target_dir_row(js):
    assert '"115 默认目标目录"' not in js


def _hdhive_field_access_ok(text: str) -> bool:
    """`hdhive` may appear only as an API field access (`.hdhive.` or
    `["hdhive"]`) -- e.g. `s.hdhive.authorized` -- never as a UI label,
    an OAuth/check-in word, or any other bare mention."""
    total = re.findall(r"hdhive", text, re.I)
    field_access = re.findall(r'\.hdhive\.|\["hdhive"\]', text, re.I)
    return len(total) == len(field_access)


def test_status_list_labels_avoid_forbidden_words(js, page):
    # HTML stays fully forbidden -- no field-access excuse applies there.
    assert "hdhive" not in page.lower()
    assert "oauth" not in page.lower()
    for forbidden in ("签到", "解锁"):
        assert forbidden not in page, forbidden
        assert forbidden not in js, forbidden
    # JS: relaxed ONLY for `hdhive` as a plain API field access; oauth and
    # the check-in/unlock words stay fully forbidden.
    assert "oauth" not in js.lower()
    assert _hdhive_field_access_ok(js), "hdhive must appear only as .hdhive. / [\"hdhive\"] field access"


def test_background_auth_status_row_reads_hdhive_authorized_and_checkin(js):
    region = re.search(r'"后台授权"[\s\S]{0,50}|后台授权[\s\S]{0,400}', js)
    assert region, "expected the 后台授权 status row"
    # the row (and its immediate construction) must read the authorized flag
    # and the last background task's result/time straight from the API.
    assert re.search(r'\.hdhive\.authorized', js)
    assert re.search(r'\.hdhive\.checkin\.last_success', js)
    assert re.search(r'\.hdhive\.checkin\.last_at', js)
    assert "已授权" in js and "未授权" in js
    assert "最近自动任务" in js
    assert "暂无记录" in js


def test_status_uses_status_mark_dots_not_json(js):
    assert "status-mark" in js
    assert "JSON.stringify(s)" not in js
    assert "JSON.stringify(d)" not in js


# ---------------------------------------------------------------------------
# T5.7 OpenList / STRM visual rework: icons, .strm marker, retry, path scroll
# ---------------------------------------------------------------------------

def test_openlist_and_strm_rows_use_folder_and_file_icons(js):
    assert "#icon-folder" in js
    assert "#icon-file" in js


def test_strm_files_get_a_type_marker(js):
    assert re.search(r'\.strm', js, re.I)
    assert "badge-strm" in js


def test_openlist_and_strm_errors_offer_retry(js):
    assert "retry-btn" in js or "setError" in js
    for box_id in ("openResult", "strmResult"):
        region = re.search(re.escape(box_id) + r'[\s\S]{0,600}', js)
        assert region, box_id
    assert re.search(r'catch\(function \(e\) \{\s*setError\("openResult"', js) or "setError(\"openResult\"" in js


def test_browser_head_path_scrolls_horizontally_when_long(css):
    block = re.search(r'\.browser-head code\{([^}]*)\}', css)
    assert block, "expected a .browser-head code rule"
    body = block.group(1)
    assert "overflow-x:auto" in body
    assert "white-space:nowrap" in body


def test_openlist_and_strm_empty_directory_text(js):
    assert "当前目录为空" in js


# ---------------------------------------------------------------------------
# Cross-cutting: the shared api.request() error must carry the backend's
# `code` field so LIBRARY_NOT_INSTALLED (503) can switch views.library into
# the libraryNotInstalled empty state instead of the generic error state.
# ---------------------------------------------------------------------------

def test_api_request_propagates_error_code_and_library_search_checks_it(js):
    assert "err.code = d.code" in js or "e.code = d.code" in js or ".code = d.code" in js
    assert 'LIBRARY_NOT_INSTALLED' in js


def test_year_chip_removal_resets_the_filter_year_select(js):
    region = re.search(r'"年份 " \+ interpreted\.year[\s\S]{0,600}', js)
    assert region, "expected the year-chip clear callback"
    body = region.group(0)
    # explicit-filter fallback (spans absent for this dimension): clears the
    # filter select directly, same as before.
    assert re.search(r'spans\.year[\s\S]{0,250}\$\("filterYear"\)\.value = ""', body) or \
        re.search(r'spans\.year[\s\S]{0,250}:\s*function[\s\S]{0,150}\$\("filterYear"\)\.value = ""', body)
    # interpreted (text-derived) branch: the year text must be stripped out
    # of state.library.q and the query input updated -- not just the select.
    # (clearInterpretedSpan itself is exercised by a dedicated test below.)
    assert "clearInterpretedSpan(" in body
    assert "stripSpans(" in js and '$("libraryQuery").value' in js


# ---------------------------------------------------------------------------
# Review fix: interpreted chip removal must strip the originating query text
# (interpreted.spans), not silently toggle an explicit filter the user never
# set (the old behaviour, which made removal a no-op from the user's POV).
# ---------------------------------------------------------------------------

def test_quality_and_hdr_toggle_groups_are_single_select(js):
    # I2: the backend reads quality/hdr as a scalar (library_search.py's
    # Filters.quality/hdr, app.py's request.args.get("quality") or None),
    # but the multi-select chip UI sent "quality=2160p,1080p" -- always
    # zero results. Only provider is comma-separated per contract §7.1.
    assert re.search(r'renderToggleGroup\(\s*"filterQuality",\s*d\.qualities \|\| \[\],\s*"quality",\s*true\s*\)', js)
    assert re.search(r'renderToggleGroup\(\s*"filterHdr",\s*d\.hdr \|\| \[\],\s*"hdr",\s*true\s*\)', js)
    provider_call = re.search(r'renderToggleGroup\(\s*"filterProviders"[^;]*;', js)
    assert provider_call, "expected the filterProviders renderToggleGroup call"
    assert "true" not in provider_call.group(0).split(",")[-1]


def test_render_toggle_group_single_select_clears_other_selections(js):
    match = re.search(r'renderToggleGroup:\s*function[\s\S]{0,1200}?\n    \},', js)
    assert match, "expected renderToggleGroup function"
    body = match.group(0)
    assert "singleSelect" in body
    assert "list.length = 0" in body


def test_year_option_textcontent_is_not_double_escaped(js):
    # Minor fix: opt.textContent = esc(y.label) HTML-escapes a value that
    # the DOM will already render literally via textContent, showing
    # "&amp;" etc. instead of "&" for any year label containing an
    # HTML-special character. The trailing (y.count === 0 ? "" : "") is
    # also a no-op ternary (both branches are "") left over from a prior
    # edit.
    match = re.search(r'loadFilters:\s*function[\s\S]{0,700}', js)
    assert match, "expected loadFilters function"
    body = match.group(0)
    assert "opt.textContent = y.label" in body
    assert "esc(y.label)" not in body
    assert 'y.count === 0 ? "" : ""' not in body


def test_interpreted_spans_referenced_in_js(js):
    assert "interpreted.spans" in js


def test_interpreted_provider_chips_read_providers_key(js):
    # I3: the backend emits interpreted.providers (plural) --
    # library_search.py's QueryPlan.to_dict uses "providers" -- so reading
    # interpreted.provider (singular) means the chips never render/clear.
    region = re.search(r'renderChips:\s*function[\s\S]{0,2600}', js)
    assert region, "expected renderChips function"
    body = region.group(0)
    assert "interpreted.providers" in body
    assert "interpreted.provider ||" not in body
    assert "interpreted.provider)" not in body and "interpreted.provider " not in body.replace("interpreted.providers", "")


def test_interpreted_correction_chip_uses_corrected_term(js):
    # Minor fix: interpreted.corrections is a list of [original, corrected]
    # pairs (library_search.py) -- rendering the whole pair `c` (which
    # stringifies as "original,corrected") instead of `c[1]` is wrong.
    match = re.search(r'interpreted\.corrections[\s\S]{0,200}forEach\(function \(c\) \{([\s\S]{0,200}?)\}\)', js)
    assert match, "expected interpreted.corrections.forEach(function (c) {...})"
    body = match.group(1)
    assert "c[1]" in body
    assert '"已按 " + c +' not in body


def test_strip_spans_helper_removes_whole_tokens_case_insensitively(js):
    match = re.search(r'function stripSpans\(([^)]*)\)\s*\{([\s\S]*?)\n  \}', js)
    assert match, "expected a stripSpans(q, spans) helper function"
    params = match.group(1)
    assert "q" in params and "spans" in params
    body = match.group(2)
    assert "toLowerCase()" in body, "must compare case-insensitively"
    assert re.search(r'split\(/\\s\+/\)', body), "must split into whole tokens on whitespace"
    assert "indexOf(" in body, "must reject tokens found in the span list"
    assert 'join(" ")' in body, "must collapse whitespace back into a single-spaced string"


def test_interpreted_chip_removal_prefers_span_stripping_over_explicit_filter(js):
    region = re.search(r'renderChips:\s*function[\s\S]{0,2600}', js)
    assert region, "expected renderChips function"
    body = region.group(0)
    # each dimension currently rendered as a chip must branch on whether the
    # backend echoed spans for it -- interpreted (text) vs explicit filter.
    assert re.search(r'spans\.year\s*\?', body)
    assert re.search(r'spans\.quality\s*\?', body)
    assert re.search(r'spans\.providers\s*\?', body)
    # the explicit-filter fallback (old behaviour) must only run in the
    # spans-absent branch, not unconditionally.
    assert re.search(r'spans\.quality\s*\?[\s\S]{0,300}:\s*function[\s\S]{0,200}toggleChip\("filterQuality"', body)
    assert re.search(r'spans\.providers\s*\?[\s\S]{0,300}:\s*function[\s\S]{0,200}toggleChip\("filterProviders"', body)


def test_clear_interpreted_span_updates_query_state_and_input(js):
    match = re.search(r'clearInterpretedSpan:\s*function\s*\(([^)]*)\)\s*\{([\s\S]{0,400}?)\n    \},', js)
    assert match, "expected a clearInterpretedSpan(dimension, spans) method"
    body = match.group(2)
    assert "stripSpans(" in body
    assert "state.library.q" in body
    assert '$("libraryQuery").value' in body


# ---------------------------------------------------------------------------
# Review fix: when the index is not installed (503 LIBRARY_NOT_INSTALLED),
# search/segmented/filter controls must be disabled and re-enabled once a
# later request succeeds; the manual-transfer disclosure stays usable.
# ---------------------------------------------------------------------------

def test_library_not_installed_disables_search_and_filter_controls(js):
    region = re.search(r'LIBRARY_NOT_INSTALLED[\s\S]{0,400}', js)
    assert region, "expected the LIBRARY_NOT_INSTALLED branch in search()'s catch"
    assert "setLibraryControlsDisabled(true)" in region.group(0)
    assert re.search(r'setLibraryControlsDisabled\s*:\s*function', js), \
        "expected a setLibraryControlsDisabled(disabled) method"


def test_library_not_installed_controls_include_search_segmented_and_filters(js):
    match = re.search(r'setLibraryControlsDisabled:\s*function[\s\S]{0,700}', js)
    assert match, "expected the setLibraryControlsDisabled function body"
    body = match.group(0)
    for control_id in ("libraryQuery", "librarySearch", "libraryType", "libraryFilters"):
        assert control_id in body, control_id
    assert "aria-disabled" in body


def test_library_controls_re_enabled_on_a_later_successful_search(js):
    region = re.search(r'renderResults:\s*function[\s\S]{0,200}', js)
    assert region, "expected renderResults function"
    assert "setLibraryControlsDisabled(false)" in region.group(0)


# ---------------------------------------------------------------------------
# Review fix: a shared fileRowHtml(kind, name, path, meta) helper used by
# both openlist.render and strm.render (behaviour unchanged).
# ---------------------------------------------------------------------------

def test_file_row_html_helper_exists_and_is_shared(js):
    assert re.search(r'function fileRowHtml\(kind, name, path, meta\)', js), \
        "expected a fileRowHtml(kind, name, path, meta) helper"
    assert js.count('fileRowHtml("dir"') >= 2, "expected openlist and strm to both build directory rows via the helper"
    assert js.count('fileRowHtml("file"') >= 2, "expected openlist and strm to both build file rows via the helper"


def test_file_row_html_still_marks_strm_files_and_uses_folder_file_icons(js):
    match = re.search(r'function fileRowHtml\([^)]*\)\s*\{([\s\S]{0,900}?)\n  \}', js)
    assert match, "expected the fileRowHtml function body"
    body = match.group(1)
    assert "#icon-folder" in body
    assert "#icon-file" in body
    assert "badge-strm" in body


# ---------------------------------------------------------------------------
# T4: RE0-style rebuild -- home hero/rails/filter-drawer, request-sequencing
# and history fixes, accessibility (tabs/dialog), localisation maps, new
# icons/CSS states, and static-content hygiene (no external URLs, no emoji).
# ---------------------------------------------------------------------------

# The exact DOM id list from the brief (existing ids that must survive the
# rebuild, plus the three new T4 ids).
FULL_DOM_ID_LIST = (
    "tab-library", "tab-openlist", "tab-strm", "tab-settings", "globalDot", "globalStatusText",
    "globalError", "library", "libraryStats", "libraryNotInstalled", "libraryHome", "libraryQuery",
    "librarySuggest", "libraryType", "librarySearch", "libraryChips", "libraryFilters", "filterYear",
    "filterProvidersLabel", "filterProviders", "filterQualityLabel", "filterQuality", "filterHdrLabel",
    "filterHdr", "filterDeleted", "filterSort", "libraryStatus", "libraryResults", "libraryEmpty",
    "libraryClearSearch", "libraryError", "libraryErrorText", "libraryRetry", "libraryPagination",
    "libraryDetail",
    "detailBack", "detailContent", "revealPanel", "revealCode", "revealCodeToggle", "revealFallbackRow",
    "revealFallback", "openlist", "openPreset", "openCurrent", "openUp", "openRefresh", "openResult",
    "strm", "strmCurrent", "strmUp", "strmRefresh", "strmResult", "settings", "cookie115", "tmdbKey",
    "tmdbBudget", "tmdbEnrichEnabled", "settingPid", "settingsSave", "settingsResult", "statusList",
    "settingsHelp", "toast", "transferDialog", "transferTitle", "transferSubtitle", "transferClose",
    "transferLinkId", "transferShareField", "transferShareUrl", "copyTransferLink",
    "transferServerHeldField", "transferServerHeld", "folderCurrent", "folderUp", "folderRefresh",
    "folderResult", "targetPath", "transferResult", "transferCancel", "saveBtn",
    "libraryHero", "libraryRails", "libraryFiltersToggle",
)


def test_full_dom_id_list_present(page):
    for element_id in FULL_DOM_ID_LIST:
        assert _has_id(page, element_id), element_id


def test_hero_rails_drawer_toggle_ids_present(page):
    hero_tag = _tag_block(page, "libraryHero")
    assert "hidden" in hero_tag, "hero must be hidden until a qualifying record is found"
    toggle_tag = _tag_block(page, "libraryFiltersToggle")
    assert 'aria-controls="libraryFilters"' in toggle_tag
    filters_tag = _tag_block(page, "libraryFilters")
    assert "hidden" in filters_tag


RAIL_TITLES = ("今日推荐", "最近年份", "电影", "剧集")


def test_rail_headings_exact_and_ordered_no_trending_words(js, page):
    rails_match = re.search(r'LIBRARY_RAILS\s*=\s*\[([\s\S]{0,700}?)\];', js)
    assert rails_match, "expected a LIBRARY_RAILS array"
    body = rails_match.group(1)
    for title in RAIL_TITLES:
        assert title in body, title
    positions = [body.index(title) for title in RAIL_TITLES]
    assert positions == sorted(positions), "rail titles must appear in the required order"
    assert "热门" not in js and "趋势" not in js
    assert "热门" not in page and "趋势" not in page


def test_rails_use_page_size_12_and_hero_uses_has_backdrop(js):
    assert "page_size=12" in js
    assert "has_backdrop=1" in js


# ---------------------------------------------------------------------------
# T16 §1/§4: the first home rail is now the deterministic daily-
# recommendations endpoint (never a `sort=` browse query), "资源最丰富" is
# retired (decision: removed outright, not moved to last -- its ranking
# duplicated "链接数量" sort and added a fifth rail for no new information),
# and the home page no longer prints internal totals.
# ---------------------------------------------------------------------------


def test_first_rail_reads_the_recommendations_endpoint_not_a_search_sort(js):
    rails_match = re.search(r'LIBRARY_RAILS\s*=\s*\[([\s\S]{0,700}?)\];', js)
    assert rails_match, "expected a LIBRARY_RAILS array"
    body = rails_match.group(1)
    assert "/api/library/recommendations" in body
    assert "资源最丰富" not in js


def test_home_hides_internal_totals(js, page):
    # #libraryStats keeps its static, non-numeric helper copy -- no code
    # path may write a media/link count (or any other internal number)
    # into it any more; loadStats() -- the function that used to do this
    # from /api/library/tmdb-status -- is gone outright.
    assert re.search(r'id="libraryStats"[^>]*>[^<]*搜索', page)
    assert "libraryStats" not in js
    assert not re.search(r'loadStats\s*:\s*function', js)
    assert "views.library.loadStats" not in js


# ---------------------------------------------------------------------------
# T16 §3: activating the library tab (even when already active) always
# resets to the home view -- one router entry point, not per-button logic.
# ---------------------------------------------------------------------------


def test_router_go_sends_the_library_tab_through_a_dedicated_reset(js):
    match = re.search(r'go:\s*function\s*\(tab\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a router.go function"
    body = match.group(1)
    assert re.search(r'tab\s*===\s*"library"', body), \
        "expected router.go to special-case the library tab"
    assert "goHome" in body


def test_go_home_replaces_url_closes_filters_and_scrolls_to_top(js):
    match = re.search(r'goHome:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a views.library.goHome function"
    body = match.group(1)
    assert "replaceState" in body, "expected ?tab=library via replaceState, never pushState"
    assert "pushState" not in body
    assert "closeFilters" in body
    assert re.search(r"scrollTo\(", body), "expected the reset to scroll back to the top"


def test_activate_closes_filters_on_every_tab_switch(js):
    # #libraryFilters is a body-level popover (T16 §5), no longer a
    # descendant of the #library panel -- leaving/entering any tab (not
    # just re-selecting library) must close it explicitly or it would keep
    # floating over whichever tab is now shown.
    match = re.search(r'activate:\s*function\s*\(tab\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a router.activate function"
    assert "closeFilters" in match.group(1)


def test_app_js_has_abort_controller_request_protection(js):
    assert "AbortController" in js
    assert "AbortError" in js
    assert ".abort()" in js


def test_app_js_restores_url_with_replace_state_only_never_push(js):
    assert "replaceState" in js
    match = re.search(r'onEnter:\s*function\s*\(\)\s*\{([\s\S]{0,800}?)\n    \},', js)
    assert match, "expected an onEnter function"
    body = match.group(1)
    assert "pushUrl(false)" in body
    assert "pushUrl(true)" not in body


def test_open_detail_pushes_and_close_detail_goes_back_not_a_third_entry(js):
    # Regression: closeDetail() used to call pushUrl(true) unconditionally,
    # which -- since state.library.media had just changed -- pushed a THIRD
    # history entry (Home -> Detail -> duplicate Home), so the browser back
    # button from there returned to Detail instead of leaving the app.
    # openDetail() must record that *we* pushed the entry (state.detailPushed)
    # and closeDetail() must call history.back() in that case, relying on the
    # existing popstate handler to restore the results view; only when the
    # detail was NOT opened via our own push should it fall back to
    # pushUrl()/replaceState.
    open_match = re.search(r'openDetail:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert open_match, "expected an openDetail function"
    open_body = open_match.group(1)
    assert "state.detailPushed = true" in open_body
    assert "pushUrl(true)" in open_body

    close_match = re.search(r'closeDetail:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert close_match, "expected a closeDetail function"
    close_body = close_match.group(1)
    assert "state.detailPushed" in close_body
    assert "history.back()" in close_body


def test_tabs_have_roving_tabindex_and_arrow_key_navigation(js):
    assert "tabIndex" in js
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert key in js, key


def test_transfer_dialog_has_a_real_tab_cycling_focus_trap(js):
    match = re.search(r'keydown[\s\S]{0,1600}?shiftKey[\s\S]{0,500}', js)
    assert match, "expected a Tab-cycling keydown handler using shiftKey"
    body = match.group(0)
    assert "shiftKey" in body
    assert "preventDefault" in body


def test_media_card_html_no_longer_reads_item_providers_or_counts(js):
    # y1-cards §1.1: the "N 个版本 · M 条链接 · K 个来源" stats line (and the
    # older has_115 provider-badge fallback) are gone from cards entirely
    # -- detail keeps full provider/version/link info -- so mediaCardHtml
    # must not read any of these fields any more.
    match = re.search(r'mediaCardHtml:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a mediaCardHtml function"
    body = match.group(1)
    assert "item.providers" not in body
    assert "item.group_count" not in body
    assert "item.link_count" not in body
    assert "has_115" not in body
    assert "Object.keys(item.providers)" not in js


def test_provider_localisation_map_has_all_ten_keys(js):
    # T8 §3: one shared provider vocabulary with library_normalize.PROVIDERS
    # -- the real codes emitted there are tianyicloud/139cloud (magnet
    # links are stored as provider "ed2k"), so the old "tianyi"/"139"/
    # "magnet" keys are dead and must be gone.
    match = re.search(r'PROVIDER_LABEL\s*=\s*\{([\s\S]{0,700}?)\};', js)
    assert match, "expected a PROVIDER_LABEL map"
    body = match.group(1)
    for key in ("115", "tianyicloud", "quark", "alipan", "baidu", "guangya", "139cloud", "123", "ed2k", "unknown"):
        assert f'"{key}"' in body or f"{key}:" in body, key
    for label in ("115 网盘", "天翼云盘", "夸克网盘", "阿里云盘", "百度网盘", "广亚", "移动云盘", "123 云盘", "ED2K", "其他"):
        assert label in body, label
    for dead_key in ("tianyi:", '"tianyi"', "139:", '"139"', "magnet:", '"magnet"', "光雅"):
        assert dead_key not in body, dead_key


def test_worker_state_localisation_map_present(js):
    match = re.search(r'WORKER_STATE_LABEL\s*=\s*\{([\s\S]{0,220}?)\};', js)
    assert match, "expected a WORKER_STATE_LABEL map"
    body = match.group(1)
    for state, label in (("running", "运行中"), ("paused", "已暂停"), ("stale", "无心跳"), ("not_started", "未启动")):
        assert state in body, state
        assert label in body, label


def test_review_reason_localisation_map_present(js):
    match = re.search(r'REVIEW_REASON_LABEL\s*=\s*\{([\s\S]{0,700}?)\};', js)
    assert match, "expected a REVIEW_REASON_LABEL map"
    body = match.group(1)
    for code in (
        "title_alias_conflict", "year_missing", "edition_unparsed", "link_shared_across_groups",
        "row_shifted", "access_code_conflict", "access_code_divergence", "timestamp_unparsed",
    ):
        assert code in body, code


def test_tmdb_budget_seeded_from_configured_budget_never_effective(js):
    match = re.search(r'function refreshLibraryStatus\(\)[\s\S]{0,2600}?\n  \}', js)
    assert match, "expected refreshLibraryStatus function"
    body = match.group(0)
    assert "configured_budget" in body
    assert re.search(r'\$\("tmdbBudget"\)\.value = d\.configured_budget', body)


def test_settings_save_backfills_budget_from_response_tmdb_configured_budget(js):
    region = re.search(r'settingsSave[\s\S]{0,1100}', js)
    assert region, "expected the settingsSave click handler"
    assert "d.tmdb.configured_budget" in region.group(0)


def test_worker_state_row_shows_used_over_effective_budget(js):
    match = re.search(r'function refreshLibraryStatus\(\)[\s\S]{0,2600}?\n  \}', js)
    assert match, "expected refreshLibraryStatus function"
    body = match.group(0)
    assert "effective_budget" in body
    assert "workerLabel" in body


NEW_ICON_IDS = (
    "icon-search", "icon-filter", "icon-sort", "icon-back", "icon-star", "icon-calendar",
    "icon-quality", "icon-audio", "icon-subtitle", "icon-provider", "icon-film", "icon-tv",
    "icon-chevron-down", "icon-chevron-right", "icon-close", "icon-external", "icon-copy", "icon-transfer",
)


def test_icons_svg_has_all_required_symbols():
    svg = STATIC_DIR.joinpath("icons.svg").read_text(encoding="utf-8")
    for icon_id in NEW_ICON_IDS:
        assert f'id="{icon_id}"' in svg, icon_id


def test_css_has_both_reduced_motion_and_reduced_transparency_blocks(css):
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (prefers-reduced-transparency: reduce)" in css


def test_css_media_grid_uses_fixed_columns_not_auto_fill(css):
    # x1-provider-ui §3.1: home/results grid columns are 桌面 5 / 平板 3 /
    # 窄屏 2 (down from the earlier graduated 6/5/4/3/2). Fix wave 1, item 5:
    # 1024/1280/1440px used to each carry their own identical repeat(5,1fr)
    # rule -- collapsed into the one min-width:1024px rule (already the
    # narrowest of the three, so it covers every wider viewport too).
    assert "auto-fill" not in css
    expectations = {1024: 5, 768: 3}
    for width, columns in expectations.items():
        block = re.search(rf'@media \(min-width:{width}px\)\{{([\s\S]*?)\n\}}', css)
        assert block, f"expected a min-width:{width}px media block"
        assert f"repeat({columns}," in block.group(1)
    assert not re.search(r'@media \(min-width:1280px\)\{\.media-grid\{', css)
    assert not re.search(r'@media \(min-width:1440px\)\{\.media-grid\{', css)
    assert re.search(r'\.media-grid\{[^}]*repeat\(2,', css), "expected a 2-column base (<768px) rule"


def test_css_defines_hover_and_dialog_shadow_tokens(css):
    assert "--shadow-hover" in css
    assert "--shadow-dialog" in css
    # The dialog-level token must actually be wired to the one modal surface
    # (.modal-card), not just declared and left unused.
    match = re.search(r'\.modal-card\{([^}]*)\}', css)
    assert match, "expected a .modal-card rule"
    assert "var(--shadow-dialog)" in match.group(1)


def test_page_title_has_no_sub_clamp_override_at_narrow_widths(css):
    # The page-title clamp(40px, ..., 48px) rule already handles narrow
    # screens down to a 40px floor; a later @media override that shrinks
    # `.page-intro h2` below that floor would defeat the clamp everywhere
    # under that breakpoint (e.g. 390x844 / 375x812).
    clamp_rule = re.search(r'\.page-intro h2\{[^}]*font-size:clamp\([^}]*\}', css)
    assert clamp_rule, "expected the .page-intro h2 clamp(40px, ..., 48px) rule"
    rest = css[clamp_rule.end():]
    assert not re.search(r'\.page-intro h2\{font-size:(?!clamp\()', rest), \
        "found a non-clamp font-size override for .page-intro h2 after the clamp rule"


EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def test_no_emoji_code_points_in_html_css_js(page, css, js):
    for label, text in (("html", page), ("css", css), ("js", js)):
        match = EMOJI_RE.search(text)
        assert not match, f"unexpected emoji {match.group(0)!r} in {label}"


def test_no_external_resource_urls_in_html_or_css(page, css):
    assert not re.search(r'(?:src|href)\s*=\s*"https://', page)
    assert "@import" not in css
    assert not re.search(r'url\(\s*[\'"]?https://', css)


def test_js_https_literals_limited_to_tmdb_images(js):
    for url in re.findall(r'https://[^\s"\'\)]+', js):
        assert "image.tmdb.org" in url, url


# ---------------------------------------------------------------------------
# T7 u4-fix: visual/behaviour fix wave after the first real screenshots
# ---------------------------------------------------------------------------

def test_global_hidden_attribute_always_hides_regardless_of_display_override(css):
    # .content-rails{display:flex} and .field{display:grid} (used by
    # #transferShareField) each declare an author `display` value, which
    # beats the browser's default `[hidden]{display:none}` UA-stylesheet
    # rule regardless of selector specificity (author origin always wins
    # over user-agent origin) -- so setting the `hidden` attribute on
    # #libraryRails/#transferShareField had no visual effect. Only a
    # global, `!important` author rule can force every hideable container
    # to actually hide.
    assert re.search(r'\[hidden\]\s*\{\s*display\s*:\s*none\s*!important\s*;?\s*\}', css), \
        "expected a global [hidden]{display:none !important} rule"


def test_show_results_mode_hides_hero_and_rails(js):
    match = re.search(r'showResultsMode:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a showResultsMode function"
    body = match.group(1)
    assert '$("libraryRails").hidden = true' in body
    assert '$("libraryHero").hidden = true' in body


def test_open_library_transfer_hides_share_field_shows_server_held_field(js):
    match = re.search(r'openLibrary:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected an openLibrary function"
    body = match.group(1)
    assert '$("transferShareField").hidden = true' in body
    assert '$("transferServerHeldField").hidden = false' in body


def test_detail_info_never_overlaps_the_dark_backdrop(css):
    # The title/meta/overview/CTA block must always render in normal flow
    # below the hero (on the plain page background) -- only the poster is
    # allowed a negative margin to overlap up into the backdrop's dark
    # bottom edge.
    hero_rule = re.search(r'\.detail-hero\{([^}]*)\}', css)
    assert hero_rule, "expected a .detail-hero rule"
    assert "margin-bottom" not in hero_rule.group(1), (
        "a negative margin-bottom on .detail-hero pulls the whole "
        ".detail-main row (including the text block) up over the dark "
        "backdrop -- it must not carry one any more"
    )
    poster_rule = re.search(r'\.detail-poster\{([^}]*)\}', css)
    assert poster_rule, "expected a .detail-poster rule"
    assert re.search(r'margin-top\s*:\s*-\d', poster_rule.group(1)), \
        "expected .detail-poster to carry its own negative margin-top to overlap the backdrop"


def test_detail_backdrop_gradient_never_becomes_an_opaque_block(css):
    # T21 §8.2: "渐变只服务于边缘可读性，不能用不透明黑块盖住人物" -- the
    # gradient layer (renamed from .detail-hero-overlay to
    # .detail-backdrop__gradient alongside the new layered hero) must stay
    # low-opacity at every stop so it can never obscure the subject, and
    # must not intercept pointer events (it sits over the real image).
    rule = re.search(r'\.detail-backdrop__gradient\{([^}]*)\}', css)
    assert rule, "expected a .detail-backdrop__gradient rule"
    body = rule.group(1)
    assert "pointer-events:none" in body
    alphas = [float(a) for a in re.findall(r'rgba\([^)]*,\s*([\d.]+)\s*\)', body)]
    assert alphas, "expected the gradient to be built from rgba(...) stops"
    assert max(alphas) <= 0.3, f"gradient stop too opaque to stay a legibility aid, not an opaque block: {alphas}"


def test_detail_backdrop_reserves_height_before_image_loads(css):
    # T21 §8.2/§8.4: no CLS -- the container's box must come from
    # aspect-ratio/min-height (resolved from the CSS alone, before any
    # image has a chance to load), not from the <img> children's own
    # intrinsic size.
    rule = re.search(r'\.detail-backdrop\{([^}]*)\}', css)
    assert rule, "expected a .detail-backdrop rule"
    body = rule.group(1)
    assert "aspect-ratio" in body
    assert "min-height" in body


def test_detail_backdrop_desktop_height_is_capped(css):
    # Follow-up: the un-capped aspect-ratio height (774px at 1440x900)
    # dominated the first screen -- max-height keeps the desktop hero
    # inside the §8.3 clamp(...,...,520px) envelope; aspect-ratio/
    # min-height still let narrower viewports shrink naturally, and the
    # mobile media-query override (210px) is well under the cap already.
    rule = re.search(r'\.detail-backdrop\{([^}]*)\}', css)
    assert rule, "expected a .detail-backdrop rule"
    assert re.search(r'max-height\s*:\s*520px', rule.group(1))


def test_detail_backdrop_empty_state_uses_a_subtle_gradient(css):
    # §8.2 allows "poster/纯色渐变" for the no-backdrop-and-no-poster
    # fallback -- a flat single colour reads as a rendering glitch;
    # .detail-backdrop--empty (set by render()/bind() below) must paint a
    # gradient instead, while the base rule keeps its own solid fallback
    # fill for browsers where the class never applies.
    rule = re.search(r'\.detail-backdrop--empty\{([^}]*)\}', css)
    assert rule, "expected a .detail-backdrop--empty rule"
    assert "gradient" in rule.group(1)


def test_detail_backdrop_image_is_never_object_fit_cover(css):
    # T21 §8.1/§8.2: object-fit:cover is exactly the bug this brief fixes
    # (crops the subject) -- the foreground layer must stay `contain`.
    rule = re.search(r'(?<!,)\.detail-backdrop__image\{([^}]*)\}', css)
    assert rule, "expected a standalone .detail-backdrop__image rule"
    assert "object-fit:contain" in rule.group(1)
    assert "object-fit:cover" not in rule.group(1)


def _detail_render_body(js):
    match = re.search(r'render:\s*function\s*\(media[^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a detail render function"
    return match.group(1)


def test_detail_render_builds_layered_backdrop_dom(js):
    # T21 §8.2: container (.detail-backdrop) > blur copy (aria-hidden) +
    # foreground image (real alt) + gradient (aria-hidden) -- never a bare
    # <img class="detail-backdrop"> any more (that class now names the
    # container, not an <img>).
    body = _detail_render_body(js)
    assert '<div class="detail-backdrop' in body
    assert 'detail-backdrop__blur' in body
    assert 'detail-backdrop__image' in body
    assert 'detail-backdrop__gradient" aria-hidden="true"' in body
    assert 'detail-backdrop__blur" src="' in body and 'aria-hidden="true"' in body
    assert 'loading="eager"' in body
    assert 'fetchpriority="high"' in body
    # Follow-up: no backdrop AND no poster -> detail-backdrop--empty (a
    # subtle gradient instead of the flat fill), same reserved box.
    assert re.search(r'heroEmptyClass\s*=\s*heroSrc\s*\?\s*""\s*:\s*"\s*detail-backdrop--empty"', body)
    assert "detail-backdrop' + heroEmptyClass" in body
    # No more detail-hero-compact height-collapse branch -- the container
    # keeps the same geometry (aspect-ratio/min-height) regardless of
    # whether a backdrop/poster is present (§8.4: no CLS, fallback ==
    # loaded-state height). Checks the actual class-attribute string, not
    # prose mentioning the old name for context.
    assert 'detail-hero-compact"' not in body
    assert 'detail-hero-overlay"' not in body


def test_detail_render_foreground_alt_and_poster_alt(js):
    # T21 §8.2/§8.4 accessibility: foreground alt is "<title>背景图",
    # poster alt is "<title>海报"; the decorative blur/gradient layers
    # carry alt="" / aria-hidden so they never surface to assistive tech.
    body = _detail_render_body(js)
    assert '"背景图"' in body or "'背景图'" in body or "背景图" in body
    assert "海报" in body
    assert re.search(r'heroAlt\s*=\s*esc\(media\.title\)\s*\+\s*"背景图"', body)
    assert re.search(r"esc\(media\.title\)\s*\+\s*'海报", body) or re.search(r'esc\(media\.title\)\s*\+\s*"海报', body)


def test_detail_render_falls_back_to_poster_when_backdrop_missing(js):
    # T21 §8.2/§8.4: no backdrop_url -> the foreground/blur layers use the
    # poster instead (still contain, never stretched) so the hero keeps
    # its reserved geometry instead of collapsing; there is still no
    # image at all only when neither backdrop nor poster exists.
    body = _detail_render_body(js)
    assert re.search(r'heroSrc\s*=\s*media\.backdrop_url\s*\|\|\s*posterUrl\s*\|\|\s*""', body)


def test_detail_bind_hero_image_load_sets_ratio_and_error_falls_back(js):
    # T21 §8.2/§8.4: bind() (called right after the innerHTML insertion,
    # never an inline onload=/onerror= attribute -- T8 #9) sets the
    # informational data-backdrop-ratio on load, and on error swaps to the
    # poster fallback once before giving up and removing both image
    # layers (dark fill + gradient only, geometry unchanged).
    match = re.search(r'bind:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a detail bind function"
    body = match.group(1)
    assert "detail-backdrop__image" in body
    assert 'addEventListener("load"' in body
    assert 'addEventListener("error"' in body
    assert "data-backdrop-ratio" in body
    assert "data-fallback-src" in body
    assert not re.search(r'onerror\s*=\s*[\'"]', body)
    assert not re.search(r'onload\s*=\s*[\'"]', body)
    # Follow-up: once the fallback also fails and both image layers are
    # removed, the container gets detail-backdrop--empty (subtle gradient
    # instead of a flat fill) -- same reserved geometry either way.
    assert 'classList.add("detail-backdrop--empty")' in body


def test_media_card_html_has_no_status_badge(js):
    # y1-cards §1.1: the 待复核 status badge is gone from cards entirely now
    # (detail keeps it) -- mediaCardHtml must not branch on match_status or
    # render badge-match at all any more.
    match = re.search(r'mediaCardHtml:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a mediaCardHtml function"
    body = match.group(1)
    assert "match_status" not in body
    assert "badge-match" not in body


def test_detail_match_status_uses_grey_text_except_needs_review(js):
    match = re.search(r'render:\s*function\s*\(media[^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a detail render function"
    body = match.group(1)
    assert "match-note" in body
    assert re.search(r'media\.match_status\s*===\s*"needs_review"', body)


def test_match_note_style_is_subtle_grey(css):
    rule = re.search(r'\.match-note\{([^}]*)\}', css)
    assert rule, "expected a .match-note rule"
    assert "var(--muted)" in rule.group(1)


def test_group_count_separated_from_title(js, css):
    # T18 §12.4: "资源组标题 N 条链接" on one line, the count in a
    # low-contrast (--muted) style, separated from the title by a real
    # layout gap (flex `gap`), not just adjacent text.
    match = re.search(r'groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a groupRowHtml function"
    body = match.group(1)
    assert '"group-count">' in body
    assert "条链接" in body
    rule = re.search(r'\.group-title-row\{([^}]*)\}', css)
    assert rule, "expected a .group-title-row rule"
    assert "gap" in rule.group(1)
    count_rule = re.search(r'\.group-count\{([^}]*)\}', css)
    assert count_rule, "expected a .group-count rule"
    assert "var(--muted)" in count_rule.group(1)


def test_provider_localisation_map_has_tianyicloud_and_139cloud_labels(js):
    # tianyicloud/139cloud are the real codes library_normalize.PROVIDERS
    # emits (T8 §3) -- not aliases of some other production code.
    match = re.search(r'PROVIDER_LABEL\s*=\s*\{([\s\S]{0,700}?)\};', js)
    assert match, "expected a PROVIDER_LABEL map"
    body = match.group(1)
    assert re.search(r'tianyicloud\s*:\s*"天翼云盘"', body), "expected a tianyicloud label"
    assert re.search(r'"139cloud"\s*:\s*"移动云盘"', body), "expected a 139cloud label"


def test_router_activate_scrolls_selected_tab_into_view(js):
    match = re.search(r'activate:\s*function\s*\(tab\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a router.activate function"
    body = match.group(1)
    assert re.search(r'scrollIntoView\(\{\s*block:\s*"nearest"\s*,\s*inline:\s*"nearest"\s*\}\)', body)


def test_detail_pushed_reset_when_leaving_library_tab(js):
    match = re.search(r'activate:\s*function\s*\(tab\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a router.activate function"
    body = match.group(1)
    assert re.search(r'tab\s*!==\s*"library"[\s\S]{0,40}state\.detailPushed\s*=\s*false', body), (
        "expected router.activate to reset state.detailPushed when the "
        "library tab is left, so a later closeDetail() can't history.back() "
        "past an intervening tab-switch history entry"
    )


def test_on_enter_resets_detail_pushed_flag(js):
    match = re.search(r'onEnter:\s*function\s*\(\)\s*\{([\s\S]{0,800}?)\n    \},', js)
    assert match, "expected an onEnter function"
    body = match.group(1)
    assert "state.detailPushed = false" in body, (
        "restoring a detail from the URL (initial load / popstate / "
        "returning to the library tab) is not a push -- closeDetail() must "
        "not later try to history.back() out of it"
    )


def test_transfer_folder_list_error_never_shows_raw_upstream_text(js):
    assert "目录读取失败" in js, "expected a friendly Chinese fallback message"
    match = re.search(r'loadFolders:\s*function[\s\S]{0,2100}?\n    \},', js)
    assert match, "expected a loadFolders function"
    body = match.group(0)
    assert not re.search(r'setEmpty\("folderResult",\s*e\.message\)', body), (
        "must not pass the raw upstream/exception-class-name message "
        "through unchecked -- map it to a friendly Chinese fallback first"
    )
    assert re.search(r'setEmpty\("folderResult",\s*friendlyFolderError\(e\.message\)\)', body)


def test_filter_drawer_has_genre_and_source_chip_groups(page):
    # T8 #14: consume the genres/sources facets from /api/library/filters
    # as two single-select chip groups, next to provider/quality/hdr.
    for element_id, label_id in (("filterGenre", "filterGenreLabel"), ("filterSource", "filterSourceLabel")):
        assert f'id="{element_id}"' in page, element_id
        assert f'id="{label_id}"' in page, label_id
        assert f'aria-labelledby="{label_id}"' in page, label_id


def test_state_library_has_genre_and_source_fields(js):
    match = re.search(r"state\.library\s*=\s*\{([\s\S]*?)\n  \};", js)
    assert match, "expected the state.library initial object"
    body = match.group(1)
    assert "genre: []" in body
    assert "source: []" in body


def test_parse_url_reads_genre_and_source_params(js):
    match = re.search(r"parseUrl:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected a parseUrl function"
    body = match.group(1)
    assert 'p.get("genre")' in body
    assert 'p.get("source")' in body


def test_push_url_and_build_query_round_trip_genre_and_source(js):
    push_match = re.search(r"pushUrl:\s*function\s*\(push\)\s*\{([\s\S]*?)\n    \},", js)
    assert push_match, "expected a pushUrl function"
    assert 'p.set("genre"' in push_match.group(1)
    assert 'p.set("source"' in push_match.group(1)

    build_match = re.search(r"buildQuery:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},", js)
    assert build_match, "expected a buildQuery function"
    assert 'p.set("genre"' in build_match.group(1)
    assert 'p.set("source"' in build_match.group(1)


def test_load_filters_renders_genre_and_source_toggle_groups(js):
    match = re.search(r"loadFilters:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected a loadFilters function"
    body = match.group(1)
    assert 'renderToggleGroup("filterGenre", d.genres || [], "genre", true)' in body
    assert 'renderToggleGroup("filterSource"' in body
    assert "SOURCE_LABEL" in body


def test_explicit_chips_include_genre_and_source(js):
    match = re.search(r"explicitChips:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected an explicitChips function"
    body = match.group(1)
    assert "l.genre.forEach" in body
    assert "l.source.forEach" in body
    assert "SOURCE_LABEL" in body


def test_is_browsing_treats_genre_and_source_as_active_filters(js):
    match = re.search(r"isBrowsing:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected an isBrowsing function"
    body = match.group(1)
    assert "l.genre.length" in body
    assert "l.source.length" in body


def test_apply_to_form_syncs_genre_and_source_chips(js):
    match = re.search(r"applyToForm:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected an applyToForm function"
    body = match.group(1)
    assert 'syncToggleChips("filterGenre", l.genre)' in body
    assert 'syncToggleChips("filterSource", l.source)' in body


def test_transfer_folders_load_lazily_on_first_dialog_open_not_at_page_load(js):
    # T8 #12: views.transfer.loadFolders(state.transferRoot) used to run
    # unconditionally in the page-load bootstrap init() -- same lazy
    # treatment as OpenList/STRM (state.openlistLoaded/state.strmLoaded):
    # only fetch the folder list the first time the dialog opens, then
    # leave it cached for the rest of the session.
    init_match = re.search(r"function init\(\)\s*\{([\s\S]*?)\n  \}", js)
    assert init_match, "expected the bootstrap init() function"
    assert "views.transfer.loadFolders" not in init_match.group(1), (
        "the bootstrap must not eagerly fetch the 115 folder listing "
        "before the transfer dialog is ever opened"
    )

    open_common_match = re.search(r"openCommon:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},", js)
    assert open_common_match, "expected an openCommon function"
    body = open_common_match.group(1)
    assert "transferFoldersLoaded" in body
    assert "views.transfer.loadFolders" in body
    # Review fix: the guard also retries on a later dialog open if the
    # one-per-session load previously failed (state.transferLoadFailed) --
    # otherwise a single failed attempt left saveBtn disabled for the rest
    # of the page session, since transferFoldersLoaded alone never resets.
    guard = re.search(r"if\s*\(!state\.transferFoldersLoaded \|\| state\.transferLoadFailed\)\s*\{([^}]*)\}", body)
    assert guard, "expected an `if (!state.transferFoldersLoaded || state.transferLoadFailed) { ... }` guard"
    assert "state.transferFoldersLoaded = true" in guard.group(1)
    assert "views.transfer.loadFolders" in guard.group(1)

    # The manual "刷新" button and re-navigating folders must keep working
    # unconditionally -- only the dialog-open call is gated.
    assert "$(\"folderRefresh\").onclick" in js


def test_transfer_load_failed_flag_tracks_success_and_failure(js):
    match = re.search(r"loadFolders:\s*function\s*\(path\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected a loadFolders function"
    body = match.group(1)
    then_match = re.search(r"\.then\(function\s*\(d\)\s*\{([\s\S]*?)\n      \}\)", body)
    assert then_match, "expected loadFolders' .then() success handler"
    assert "state.transferLoadFailed = false" in then_match.group(1)
    catch_match = re.search(r"\.catch\(function\s*\(e\)\s*\{([\s\S]*?)\n      \}\);", body)
    assert catch_match, "expected loadFolders' .catch() failure handler"
    assert "state.transferLoadFailed = true" in catch_match.group(1)
    assert '$("saveBtn").disabled = true' in catch_match.group(1)

    # Item 1: an empty directory is still a *successful* load (the user
    # may still transfer into it) -- clearing transferLoadFailed and
    # re-enabling saveBtn must happen before the empty-directory early
    # return, not after it (where an empty directory would skip them).
    success_body = then_match.group(1)
    reset_pos = success_body.index("state.transferLoadFailed = false")
    return_pos = success_body.index("当前目录没有子文件夹")
    assert reset_pos < return_pos, (
        "state.transferLoadFailed reset must come before the empty-"
        "directory early return"
    )


def test_load_folders_reopen_after_failure_enables_save_btn_on_empty_directory(js):
    # Item 1: a failed load disables saveBtn and sets transferLoadFailed;
    # reopening the dialog and loading a directory that turns out to be
    # empty is still a *successful* load, so it must re-enable saveBtn and
    # clear transferLoadFailed exactly like a non-empty directory would.
    match = re.search(r"loadFolders:\s*function\s*\(path\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected a loadFolders function"
    harness = """
var els = {};
function $(id) { return els[id] || (els[id] = { textContent: "", disabled: false, innerHTML: "" }); }
function setEmpty(id, message) { $(id).innerHTML = message; }
function esc(s) { return String(s == null ? "" : s); }
function friendlyFolderError(message) { return message; }
var state = { currentTransferPath: "/", transferRoot: "/", transferRootPid: "0", transferBusy: false, transferLoadFailed: false };
var callCount = 0;
var api = { request: function () {
  callCount += 1;
  if (callCount === 1) return Promise.reject(new Error("boom"));
  return Promise.resolve({ items: [] });
} };
var views = { transfer: {} };
views.transfer.loadFolders = function (path) {""" + match.group(1) + """};
views.transfer.loadFolders("/").then(function () {
  return views.transfer.loadFolders("/");
}).then(function () {
  console.log(JSON.stringify({ saveBtnDisabled: $("saveBtn").disabled, transferLoadFailed: state.transferLoadFailed }));
});
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["transferLoadFailed"] is False
    assert out["saveBtnDisabled"] is False


def test_transfer_duplicate_error_renders_as_info_notice_not_red_error(js):
    # Review fix: TRANSFER_DUPLICATE (409) is the backend's own benign
    # "already in flight" rejection of a double-submit, not a real
    # failure -- it must never render with the red .error style.
    match = re.search(r"function renderTransferError\(e\) \{([\s\S]*?)\n  \}", js)
    assert match, "expected a renderTransferError function"
    harness = """
var els = {};
function $(id) { return els[id] || (els[id] = { textContent: "", className: "", hidden: true }); }
function feedback(id, message, kind) {
  var el = $(id);
  el.textContent = message || "";
  el.className = "feedback" + (kind ? " " + kind : "");
  el.hidden = !message;
}
function esc(s) { return String(s == null ? "" : s); }
function renderTransferError(e) {""" + match.group(1) + """}
renderTransferError({ code: "TRANSFER_DUPLICATE", message: "请勿重复提交转存请求" });
console.log(JSON.stringify($("transferResult")));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    box = json.loads(result.stdout)
    assert box["className"] == "feedback"
    assert "error" not in box["className"]
    assert box["hidden"] is False
    assert "转存中" in box["textContent"]


def test_library_url_keys_dead_constant_removed(js):
    # T8 #11: LIBRARY_URL_KEYS was never read anywhere.
    assert "LIBRARY_URL_KEYS" not in js


def test_link_row_class_is_not_redefined_by_the_transfer_dialog(page, css):
    # T8 #10: .link-row was defined twice -- the detail group's link rows
    # (app.py.css §"Media detail") and the transfer dialog's share-URL row
    # -- and the later rule silently overrode the detail row's `gap`. The
    # dialog row must use its own class (.transfer-row).
    assert css.count(".link-row{") == 1, "the detail .link-row rule must be the only one"
    transfer_rule = re.search(r"\.transfer-row\{([^}]*)\}", css)
    assert transfer_rule, "expected a .transfer-row rule for the transfer dialog's share-URL row"
    assert 'class="link-row"' not in page
    assert 'class="transfer-row"' in page



def test_media_card_html_poster_image_error_is_bound_after_insertion(js):
    # T8 #9: no inline onerror= attribute (CSP-friendly) -- bindMediaCards
    # (called right after every mediaCardHtml innerHTML insertion, both in
    # renderResults and loadRails) must bind the poster <img>'s error
    # handling instead.
    card_match = re.search(r'mediaCardHtml:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert card_match, "expected a mediaCardHtml function"
    assert "onerror=" not in card_match.group(1)

    bind_match = re.search(r'bindMediaCards:\s*function\s*\(root\)\s*\{([\s\S]*?)\n    \},', js)
    assert bind_match, "expected a bindMediaCards function"
    bind_body = bind_match.group(1)
    assert 'addEventListener("error"' in bind_body
    assert "poster-broken" in bind_body


def test_hero_backdrop_hides_hero_on_image_load_error(js):
    # T8 #6: a stale backdrop_url must not leave the 340px black
    # .library-hero block showing a broken image -- bind an "error"
    # listener (never an inline onerror= attribute, T8 #9) that hides
    # #libraryHero.
    match = re.search(r"loadHero:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected a loadHero function"
    body = match.group(1)
    assert 'addEventListener("error"' in body
    assert "onerror=" not in body
    error_handler = re.search(r'addEventListener\("error",\s*function\s*\(\)\s*\{([^}]*)\}', body)
    assert error_handler, "expected an error listener callback"
    assert "hero.hidden = true" in error_handler.group(1)


def test_old_hdr_codec_audio_subtitle_label_machinery_is_gone(js):
    # T18 §12.1: codec/audio/subtitle/tags/provider chips are removed from
    # the resource summary entirely -- the maps/functions that only ever
    # fed that old rendering (HDR_LABEL/CODEC_LABEL/AUDIO_TOKEN_LABEL/
    # audioTokenLabel/subtitleTokenLabel/summaryLabel) are gone too, not
    # just unused. SOURCE_LABEL survives -- it still labels the "sources"
    # filter-popover chips (a different, still-live call site).
    for needle in (
        "HDR_LABEL", "CODEC_LABEL", "AUDIO_TOKEN_LABEL", "SUBTITLE_LEAF_LABEL",
        "SUBTITLE_PREFIX_LABEL", "function audioTokenLabel", "function subtitleTokenLabel",
        "function summaryLabel",
    ):
        assert needle not in js, needle
    assert "var SOURCE_LABEL" in js


def test_spec_symbol_picks_vendored_glyph_by_category_and_value(js):
    # T18 §12.2/§12.3: resolution always uses the generic fa-photo-film
    # glyph (Font Awesome Free has no per-resolution icon); dynamic_range
    # uses fa-circle-half-stroke, except the Dolby Vision brand mark
    # (si-dolby) for that one exact value; source distinguishes physical
    # media (fa-compact-disc) from streaming/broadcast (fa-film). Runs the
    # real function with node -- a source-text regex can't catch a mapping
    # regression here.
    match = re.search(r"var SPEC_SOURCE_SYMBOL[\s\S]*?function specSymbol\(key, value\) \{[\s\S]*?\n  \}", js)
    assert match, "expected the specSymbol helper section"
    harness = match.group(0) + """
console.log(JSON.stringify([
  specSymbol("resolution", "4K"),
  specSymbol("resolution", "SD"),
  specSymbol("dynamic_range", "Dolby Vision"),
  specSymbol("dynamic_range", "HDR10"),
  specSymbol("dynamic_range", "DV/HDR"),
  specSymbol("source", "WEB-DL"),
  specSymbol("source", "BluRay"),
  specSymbol("source", "BDRemux"),
  specSymbol("source", "HDTV")
]));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "fa-photo-film", "fa-photo-film",
        "si-dolby", "fa-circle-half-stroke", "fa-circle-half-stroke",
        "fa-film", "fa-compact-disc", "fa-compact-disc", "fa-film",
    ]


def test_spec_row_html_reads_backend_specs_object_only(js):
    # T18 §12.1/§12.3: the resource summary renders ONLY the three
    # backend-normalised classes from `group.specs` -- never raw
    # quality/hdr/source_type/video_codec/audio_summary/subtitle_summary/
    # tags/providers fields, and never an empty placeholder for a missing
    # class.
    match = re.search(r"specRowHtml:\s*function\s*\(specs\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected a specRowHtml function"
    body = match.group(1)
    assert '["resolution", "dynamic_range", "source"]' in body
    for forbidden in ("g.quality", "g.hdr", "g.source_type", "g.video_codec", "g.audio_summary", "g.subtitle_summary", "g.tags", "g.providers"):
        assert forbidden not in body, forbidden
    assert "aria-label" in body
    assert 'aria-hidden="true"' in body


def test_group_row_html_never_renders_codec_audio_subtitle_tags_or_provider_name_chips(js):
    match = re.search(r"groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected a groupRowHtml function"
    body = match.group(1)
    # I2: `group.providers[code]` is a legitimate live-only count lookup
    # (the link-count fix), not a provider-name chip -- `providerLabel(`
    # below is what actually guards against a provider-name chip re-
    # appearing here.
    for forbidden in ("video_codec", "audio_summary", "subtitle_summary", "group.tags", "providerLabel("):
        assert forbidden not in body, forbidden


def test_interpreted_provider_chip_uses_localised_label_not_raw_code(js):
    # T8 #4: interpreted.providers carries raw provider codes (same as the
    # explicit filter chips built a few lines above by explicitChips());
    # renderChips() must localise them through providerLabel() too instead
    # of pushing the raw code straight into the chip.
    match = re.search(r'renderChips:\s*function\s*\(interpreted\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a renderChips function"
    body = match.group(1)
    providers_block = re.search(r'\(interpreted\.providers \|\| \[\]\)\.forEach\(function \(pv\) \{([\s\S]*?)\n      \}\);', body)
    assert providers_block, "expected an interpreted.providers forEach block"
    assert "label: providerLabel(pv)" in providers_block.group(1)
    assert "label: pv," not in providers_block.group(1)


def test_search_query_escape_key_closes_suggest_without_clearing_input(js):
    # T8 #2: #libraryQuery is type="search" -- Chromium (and other
    # browsers) clear a search input's value on Escape by default. The
    # handler must preventDefault() so only the suggest dropdown closes
    # and state.library.q / the visible value survive.
    match = re.search(r'libraryQuery"\)\.addEventListener\("keydown",\s*function\s*\(e\)\s*\{([\s\S]*?)\n      \}\);', js)
    assert match, "expected the #libraryQuery keydown handler"
    body = match.group(1)
    escape_branch = re.search(r'if\s*\(e\.key === "Escape"\)\s*\{([^}]*)\}', body)
    assert escape_branch, "expected an Escape branch"
    assert "e.preventDefault()" in escape_branch.group(1)
    assert "hideSuggest" in escape_branch.group(1)


def test_bare_button_hover_does_not_paint_media_card_chip_toggle_group_expand_or_link_action(css):
    # T8 #1: the bare `button:hover{background:var(--accent-strong)}` rule
    # (an element+pseudo-class selector) used to beat these class-only
    # buttons' own backgrounds, painting them accent-blue with their normal
    # dark/muted text on hover (~3.2:1 contrast). Each must now carry its
    # own explicit `:hover` rule -- two class-level selectors always
    # outrank the bare `button:hover`'s one class-level + one element-level
    # selector, regardless of source order -- using a neutral
    # `--surface-hover` background, never accent-strong. `.modal-close` is
    # the same bare-button-hover case (a round icon button, same as the
    # other four) and must get the same explicit fix.
    for selector in (
        ".media-card:hover", ".chip-toggle:hover",
        ".link-action:hover", ".modal-close:hover",
    ):
        rule = re.search(re.escape(selector) + r"\{([^}]*)\}", css)
        assert rule, f"expected a {selector} rule"
        assert "var(--surface-hover)" in rule.group(1), selector
        assert "var(--accent-strong)" not in rule.group(1), selector

    # The primary link-action variant is a real call-to-action button and
    # must keep its accent-strong hover.
    primary = re.search(r"\.link-action\.primary:hover\{([^}]*)\}", css)
    assert primary, "expected .link-action.primary:hover to still exist"
    assert "var(--accent-strong)" in primary.group(1)

    # The segmented control's transparent/muted buttons rely on
    # `.segmented button{...}` (no :hover) tying the bare `button:hover`
    # rule's specificity and winning by appearing later in the
    # stylesheet -- that ordering must not regress.
    button_hover_pos = css.index("button:hover{background:var(--accent-strong)}")
    segmented_pos = css.index(".segmented button{")
    assert segmented_pos > button_hover_pos, (
        ".segmented button must stay defined after the bare button:hover "
        "rule so its own (tied-specificity) background wins on hover"
    )


# ---------------------------------------------------------------------------
# T12 x1-provider-ui: no provider name/logo pills on home/search cards; an
# RE0-style provider-logo tablist filters the detail resource list.
# ---------------------------------------------------------------------------

def test_media_card_html_has_no_provider_pill_rendering(js):
    # y1-cards §1.1 supersedes x1-provider-ui §2.1: cards no longer keep
    # even the neutral "N 个版本 · M 条链接[ · K 个来源]" stat -- no
    # .badge-provider pill, no providerLabel() call, no version/link/source
    # counts anywhere in the card renderer.
    match = re.search(r'mediaCardHtml:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a mediaCardHtml function"
    body = match.group(1)
    assert "badge-provider" not in body
    assert "providerLabel(" not in body
    assert "个来源" not in body
    assert "个版本" not in body
    assert "条链接" not in body


def test_provider_badge_css_classes_removed_as_dead_code(css):
    # The pill classes mediaCardHtml used to render are gone now that
    # nothing constructs them any more.
    assert ".badge-provider{" not in css
    assert ".provider-badges{" not in css


def test_provider_meta_covers_all_ten_codes_with_symbols_that_exist_in_icons_svg(js):
    match = re.search(r'var PROVIDER_ORDER = \[([^\]]*)\];', js)
    assert match, "expected a PROVIDER_ORDER array"
    codes = [c.strip().strip('"') for c in match.group(1).split(",")]
    assert codes == ["115", "tianyicloud", "quark", "alipan", "baidu", "guangya", "139cloud", "123", "ed2k", "unknown"]

    symbol_match = re.search(r'var PROVIDER_SYMBOL = \{([\s\S]*?)\};', js)
    assert symbol_match, "expected a PROVIDER_SYMBOL map"
    accent_match = re.search(r'var PROVIDER_ACCENT = \{([\s\S]*?)\};', js)
    assert accent_match, "expected a PROVIDER_ACCENT map"
    assert "providerMeta" in js
    assert "PROVIDER_META_BY_CODE" in js

    svg = STATIC_DIR.joinpath("icons.svg").read_text(encoding="utf-8")
    for code in codes:
        symbol = re.search(rf'["\']?{re.escape(code)}["\']?\s*:\s*"([^"]+)"', symbol_match.group(1))
        assert symbol, f"no PROVIDER_SYMBOL entry for {code!r}"
        assert f'id="{symbol.group(1)}"' in svg, symbol.group(1)
        accent = re.search(rf'["\']?{re.escape(code)}["\']?\s*:\s*"(#[0-9a-f]{{6}})"', accent_match.group(1))
        assert accent, f"no PROVIDER_ACCENT entry for {code!r}"


def test_detail_render_defines_provider_tablist_and_panel_containers(js):
    match = re.search(r'render:\s*function\s*\(media[^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a detail render function"
    body = match.group(1)
    assert 'id="libraryProviderTabs"' in body
    assert 'role="tablist"' in body
    assert 'aria-label="网盘来源"' in body
    assert 'id="libraryProviderPanel"' in body
    assert 'role="tabpanel"' in body


def test_detail_render_only_shows_all_tab_when_more_than_one_provider_or_scoped_fetch(js):
    # I1: a genuinely scoped fetch (`scopedFetch`) must still offer a 全部
    # tab to get back to the unfiltered view, even though its response's
    # `facets` array only ever contains the one requested provider --
    # `facets.length` alone can't tell that case apart from a genuinely
    # single-provider media (§3.3, where no `scopedFetch` means no 全部).
    match = re.search(r'renderProviderTabs:\s*function[\s\S]{0,400}', js)
    assert match, "expected a renderProviderTabs function"
    body = match.group(0)
    assert "facets.length > 1 || scopedFetch" in body
    assert '"全部"' in body


def test_provider_tabs_have_roving_tabindex_and_aria_selected(js):
    match = re.search(r'renderProviderTabs:\s*function[\s\S]{0,1400}?\n    \},', js)
    assert match, "expected a renderProviderTabs function"
    body = match.group(0)
    assert 'role="tab"' in body
    assert "aria-selected" in body
    assert 'aria-controls="libraryProviderPanel"' in body
    assert "isSelected ? \"0\" : \"-1\"" in body


def test_provider_tabs_keyboard_navigation_arrows_home_end(js):
    match = re.search(r'bindProviderTabs:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a bindProviderTabs function"
    body = match.group(1)
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert key in body, key
    assert "preventDefault" in body
    assert "selectProvider(tabs[nextIdx].dataset.provider)" in body
    assert "selectProvider(btn.dataset.provider)" in body


def test_select_provider_refetches_media_scoped_to_the_new_provider(js):
    # I1 (§14.1): the in-page tab switch must be backend-scoped, the same
    # as open() -- re-filtering `state.detail.media` client-side (the old
    # behaviour) left every other provider's tab/logo/count sitting in the
    # DOM/a11y tree, and inflated the summary row's counts. No more direct
    # aria-selected/tabindex twiddling or a renderGroupList() call here --
    # render() (invoked from the fetch's .then below) rebuilds the whole
    # tablist and group list from the scoped response instead.
    match = re.search(r'selectProvider:\s*function\s*\(code\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a selectProvider function"
    body = match.group(1)
    assert 'state.library.provider = normalized ? [normalized] : []' in body
    assert "pushUrl(false)" in body
    # Round 16: the URL is built by detailRequestUrl (provider scoping +
    # the include_deleted flag) -- see test_detail_request_url_scopes_to_provider.
    assert "detailRequestUrl(state.library.media, normalized)" in body
    assert 'fetchChannel("detail", url)' in body
    assert 'views.detail.render(d, !!normalized)' in body
    assert 'focusProviderTab(state.detail.provider)' in body
    # no leftover client-side residue from the old, buggy implementation
    assert "renderGroupList()" not in body
    assert 'setAttribute("aria-selected"' not in body


def test_close_detail_resets_provider_filter_so_it_cannot_leak_into_search(js):
    match = re.search(r'closeDetail:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a closeDetail function"
    assert "state.library.provider = []" in match.group(1)


def test_open_fetches_media_scoped_to_the_current_provider_filter(js):
    # T18 §14.1: entering detail from a filtered search/rail card, a
    # direct `?provider=115&media=<id>` URL, or a refresh/back/forward
    # onto one, must never load the full, unfiltered media first -- this
    # is the one place provider selection actually triggers a request.
    match = re.search(r'\n    open:\s*function\s*\(mediaId\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a detail open function"
    body = match.group(1)
    assert "state.library.provider && state.library.provider[0]" in body
    assert "detailRequestUrl(mediaId, provider)" in body
    assert 'fetchChannel("detail", url)' in body


def test_detail_request_url_scopes_to_provider(js):
    # Round 16: the shared URL builder keeps §14.1's backend scoping (a
    # `provider=` query parameter whenever a provider is selected) and
    # adds include_deleted=1 only while the "包含已失效" filter is on.
    match = re.search(r'function detailRequestUrl\(mediaId, provider\) \{([\s\S]*?)\n  \}', js)
    assert match, "expected a detailRequestUrl helper"
    harness = """
function encodeURIComponentSafe(s) { return encodeURIComponent(s); }
var state = { library: { includeDeleted: false }, detail: {} };
function detailRequestUrl(mediaId, provider) {""" + match.group(1) + """}
var out = [detailRequestUrl(7, ""), detailRequestUrl(7, "115")];
state.library.includeDeleted = true;
out.push(detailRequestUrl(7, ""), detailRequestUrl(7, "quark"), state.detail.includeDeleted);
console.log(JSON.stringify(out));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "/api/library/media/7", "/api/library/media/7?provider=115",
        "/api/library/media/7?include_deleted=1", "/api/library/media/7?provider=quark&include_deleted=1", True,
    ]


def test_detail_render_computes_selected_provider_from_url_and_facets(js):
    match = re.search(r'render:\s*function\s*\(media[^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a detail render function"
    body = match.group(1)
    assert "media.provider_facets" in body
    assert "codes.indexOf(requested)" in body
    assert "state.detail.provider = selected" in body
    assert "state.library.provider = selected ? [selected] : []" in body
    assert "pushUrl(false)" in body


def test_provider_filter_empty_state_text_is_plain_chinese_not_json(js):
    assert "该来源下暂无资源，请切换其他网盘来源" in js
    match = re.search(r'renderGroupList:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a renderGroupList function"
    assert "该来源下暂无资源" in match.group(1)
    assert "JSON.stringify" not in match.group(1)


def test_group_row_html_filters_a_mixed_provider_groups_links_by_selected_code(js):
    # §14.1: a title's own mixed-provider group (media #1's fixture group
    # has both a 115 and a quark link) must show only the matching
    # provider's link rows -- and its own displayed link count -- once a
    # specific tab is selected, not the group's full, unfiltered links.
    match = re.search(r'groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a groupRowHtml(group, code) function"
    body = match.group(1)
    assert "l.provider === code" in body
    assert 'code ? ((group.providers && group.providers[code]) || 0) : groupLiveLinkCount(group)' in body


def _extract_group_row_functions(js):
    def extract(pattern):
        m = re.search(pattern, js)
        assert m, pattern
        return m.group(1)

    season = extract(r'seasonLabel:\s*function\s*\(g\)\s*\{([\s\S]*?)\n    \},')
    specs = extract(r'specRowHtml:\s*function\s*\(specs\)\s*\{([\s\S]*?)\n    \},')
    actions = extract(r'actionButtons:\s*function\s*\(link\)\s*\{([\s\S]*?)\n    \},')
    link_row = extract(r'linkRowHtml:\s*function\s*\(link\)\s*\{([\s\S]*?)\n    \},')
    group_row = extract(r'groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},')
    live_count = extract(r'function groupLiveLinkCount\(group\) \{([\s\S]*?)\n  \}')
    # w6-contract: linkRowHtml now calls these two link-check helpers.
    reason_label = extract(r'var LINKCHECK_REASON_LABEL = (\{[\s\S]*?\});')
    relative_time = extract(r'function formatRelativeTime\(iso\) \{([\s\S]*?)\n  \}')
    tooltip = extract(r'function linkInvalidTooltip\(link\) \{([\s\S]*?)\n  \}')
    # Round 16: linkRowHtml renders its status word via these two.
    error_label = extract(r'var LINKCHECK_ERROR_CLASS_LABEL = (\{[\s\S]*?\});')
    error_label_fn = extract(r'function linkcheckErrorClassLabel\(code\) \{([\s\S]*?)\n  \}')
    status_tooltip = extract(r'function linkStatusTooltip\(link\) \{([\s\S]*?)\n  \}')
    status_badge = extract(r'function linkStatusBadgeHtml\(link\) \{([\s\S]*?)\n  \}')
    return """
var ICONS_URL = "/static/icons.svg?v=test";
var PROVIDER_LABEL = { "115": "115网盘" };
function providerLabel(code) { return PROVIDER_LABEL[code] || code; }
var PROVIDER_META_BY_CODE = { "115": { code: "115", symbol: "provider-115" } };
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
  return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
}); }
var LINKCHECK_REASON_LABEL = """ + reason_label + """;
function formatRelativeTime(iso) {""" + relative_time + """}
function linkInvalidTooltip(link) {""" + tooltip + """}
var LINKCHECK_ERROR_CLASS_LABEL = """ + error_label + """;
function linkcheckErrorClassLabel(code) {""" + error_label_fn + """}
function linkStatusTooltip(link) {""" + status_tooltip + """}
function linkStatusBadgeHtml(link) {""" + status_badge + """}
function groupLiveLinkCount(group) {""" + live_count + """}
function seasonLabel(g) {""" + season + """}
function specRowHtml(specs) {""" + specs + """}
function actionButtons(link) {""" + actions + """}
function linkRowHtml(link) {""" + link_row + """}
var views = { detail: {
  seasonLabel: seasonLabel, specRowHtml: specRowHtml,
  actionButtons: actionButtons, linkRowHtml: linkRowHtml
} };
function groupRowHtml(group, code) {""" + group_row + """}
"""


def test_group_row_html_link_count_excludes_deleted_links_of_the_selected_provider(js):
    # I2: a deleted link still renders (disabled, for audit -- §13.3), so
    # counting `links.length` after filtering by provider double-counts a
    # deleted+live pair of the same provider as 2. The displayed count
    # must match the server's live-only `group.providers[code]` total.
    harness = _extract_group_row_functions(js) + """
console.log(JSON.stringify(groupRowHtml({
  group_id: 1,
  display_title: "测试资源",
  providers: { "115": 1 },
  links: [
    { link_id: "live", provider: "115", label: "live", deleted: false, actions: [] },
    { link_id: "gone", provider: "115", label: "gone", deleted: true, actions: [] }
  ]
}, "115")));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    html = json.loads(result.stdout)
    assert "1 条链接" in html
    assert "2 条链接" not in html
    # both rows still render (deleted ones stay visible, disabled, per §13.3)
    assert html.count("link-row") >= 2


def test_group_row_html_unfiltered_branch_counts_live_links_only(js):
    # I2: LibraryStore.recount() counts every resource_link row -- deleted
    # ones included -- into `group.link_count`, so it isn't live-only. A
    # group with providers {115: 1, quark: 1} but link_count 3 (one
    # deleted link inflating it) must show "2 条链接" in the unfiltered
    # (全部) view, summing `group.providers`' own live-only counts instead
    # of trusting `link_count` directly.
    harness = _extract_group_row_functions(js) + """
console.log(JSON.stringify(groupRowHtml({
  group_id: 1,
  display_title: "测试资源",
  providers: { "115": 1, "quark": 1 },
  link_count: 3,
  links: [
    { link_id: "a", provider: "115", label: "a", deleted: false, actions: [] },
    { link_id: "b", provider: "quark", label: "b", deleted: false, actions: [] },
    { link_id: "gone", provider: "115", label: "gone", deleted: true, actions: [] }
  ]
})));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    html = json.loads(result.stdout)
    assert "2 条链接" in html
    assert "3 条链接" not in html


def test_detail_summary_link_count_sums_per_group_live_counts(js):
    # I2: the summary row's "N 条链接" must match what the groups actually
    # display below it -- the sum of each group's own live-only count --
    # rather than trusting the backend's `media.link_count`, which sums
    # each group's non-live-only `link_count` and so isn't live-only
    # either.
    render_match = re.search(r"render:\s*function\s*\(media[^)]*\)\s*\{([\s\S]*?)\n    \},", js)
    assert render_match, "expected a detail render function"
    body = render_match.group(1)
    linkcount_match = re.search(
        r"var linkCount = \(media\.groups \|\| \[\]\)\.reduce\(function \(sum, g\) \{ return sum \+ groupLiveLinkCount\(g\); \}, 0\);",
        body,
    )
    assert linkcount_match, "expected linkCount to sum groupLiveLinkCount(g) across every group"

    live_count_match = re.search(r"function groupLiveLinkCount\(group\) \{([\s\S]*?)\n  \}", js)
    assert live_count_match, "expected a groupLiveLinkCount helper"
    harness = "function groupLiveLinkCount(group) {" + live_count_match.group(1) + """}
var media = { groups: [
  { providers: { "115": 1, "quark": 1 }, link_count: 3 },
  { providers: { "115": 1 }, link_count: 1 }
] };
""" + linkcount_match.group(0) + """
console.log(JSON.stringify({ linkCount: linkCount }));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["linkCount"] == 3


def test_group_links_grid_column_cannot_blow_out_from_an_unbreakable_remark(css):
    # I3: `.group-links{display:grid}` with no `grid-template-columns` at
    # all defaults to one *content-sized* ("auto") column -- an
    # unbreakable (no-space) `.link-remark` (white-space:nowrap) that's
    # long enough contributes its full, un-ellipsized min-content width to
    # that column, stretching every `.link-row` (and the "转存到 115"
    # button inside it) far past the viewport even though `.link-remark`
    # itself still clips correctly. `minmax(0,1fr)` is the standard fix:
    # it lets the column (and everything in it) shrink to the container's
    # actual width instead of the widest child's unclipped content.
    rule = re.search(r'\.group-links\{([^}]*)\}', css)
    assert rule, "expected a .group-links rule"
    assert "grid-template-columns:minmax(0,1fr)" in rule.group(1)


def test_link_row_uses_provider_specific_logo_symbol(js):
    match = re.search(r'linkRowHtml:\s*function\s*\(link\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a linkRowHtml function"
    body = match.group(1)
    assert "PROVIDER_META_BY_CODE[link.provider]" in body
    assert '"provider-link"' in body
    assert "provider-logo" in body


def test_provider_tablist_css_uses_theme_tokens_not_hardcoded_light_grey(css):
    tab_rule = re.search(r'\.provider-tab\{([^}]*)\}', css)
    assert tab_rule, "expected a .provider-tab rule"
    assert "var(--surface-hover)" in tab_rule.group(1)
    assert "#f3f4f6" not in tab_rule.group(1)
    selected_rule = re.search(r'\.provider-tab\[aria-selected="true"\]\{([^}]*)\}', css)
    assert selected_rule, "expected a .provider-tab[aria-selected=\"true\"] rule"
    assert "var(--accent" in selected_rule.group(1)


def test_provider_tablist_scrolls_horizontally_and_hides_scrollbar(css):
    rule = re.search(r'\.provider-tablist\{([^}]*)\}', css)
    assert rule, "expected a .provider-tablist rule"
    assert "overflow-x:auto" in rule.group(1)


def test_provider_tab_meets_44px_touch_target(css):
    rule = re.search(r'\.provider-tab\{([^}]*)\}', css)
    assert rule, "expected a .provider-tab rule"
    assert re.search(r'min-height:44px', rule.group(1))


# ---------------------------------------------------------------------------
# x1-provider-ui fix wave 1: fixed-height card footers, 44px mobile resource
# buttons, provider tablist id escaping, and one collapsed grid breakpoint.
# ---------------------------------------------------------------------------

def test_media_card_footer_is_a_fixed_height_block(css):
    # Item 1 (work order §2.1/§3.1): the footer below the poster must be a
    # fixed-height block -- min-height==height plus overflow:hidden -- so a
    # card's total height never depends on whether it has an original
    # title, a genre/待复核 chip row, or how many providers it has.
    rule = re.search(r'\.media-card \.card-footer\{([^}]*)\}', css)
    assert rule, "expected a .media-card .card-footer rule"
    body = rule.group(1)
    height = re.search(r'(?<!min-)height:(\d+)px', body)
    min_height = re.search(r'min-height:(\d+)px', body)
    assert height and min_height, "expected both height and min-height on .card-footer"
    assert height.group(1) == min_height.group(1), "min-height must equal height to fully fix the footer's size"
    assert "overflow:hidden" in body


def test_media_card_footer_rows_each_have_fixed_heights_summing_to_the_footer(css):
    footer_rule = re.search(r'\.media-card \.card-footer\{([^}]*)\}', css)
    assert footer_rule, "expected a .media-card .card-footer rule"
    footer_body = footer_rule.group(1)
    footer_height = int(re.search(r'(?<!min-)height:(\d+)px', footer_body).group(1))
    gap_match = re.search(r'gap:(\d+)px', footer_body)
    gap = int(gap_match.group(1)) if gap_match else 0

    row_patterns = [
        r'\.media-card \.card-title\{([^}]*)\}',
        r'\.media-card \.card-meta\{([^}]*)\}',
    ]
    row_heights = []
    for pattern in row_patterns:
        match = re.search(pattern, css)
        assert match, f"expected a rule matching {pattern!r}"
        height_match = re.search(r'height:(\d+)px', match.group(1))
        assert height_match, f"expected a fixed height in {pattern!r}"
        row_heights.append(int(height_match.group(1)))

    assert sum(row_heights) + gap * (len(row_heights) - 1) == footer_height, (
        "the footer rows' fixed heights (plus gaps) must add up to exactly "
        "the footer's own fixed height, or content could still overflow it"
    )


def test_media_card_html_footer_rows_are_never_conditionally_omitted(js):
    # A conditionally-omitted row (the old `cond ? '<span ...>' : ""` shape)
    # would shift the fixed-height rows below it inside .card-footer,
    # defeating the point of fixing its height -- card-meta must always
    # render its wrapper span, even when formatCardMeta() returns "".
    match = re.search(r'mediaCardHtml:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a mediaCardHtml function"
    body = match.group(1)
    assert 'class="card-footer"' in body
    assert re.search(r"""\?\s*'<span class="card-meta">""", body) is None, \
        "card-meta must always render its span (even empty), never be conditionally omitted"
    assert '\'<span class="card-meta">\' + esc(meta) + \'</span>\'' in body


def test_link_action_and_transfer_meet_44px_and_full_width_on_narrow_screens(css):
    # Item 2 (work order §3.4): on screens <=480px, every resource action
    # button (open/copy/转存到 115) is at least 44px tall, and the 115
    # "转存到 115" button -- the only action a 115 link row ever renders --
    # spans the row's full width at the bottom of the row.
    block = re.search(r'@media \(max-width:480px\)\{([\s\S]*?)\n\}', css)
    assert block, "expected a max-width:480px media block"
    body = block.group(1)
    action_rule = re.search(r'\.link-action\{([^}]*)\}', body)
    assert action_rule, "expected a .link-action rule inside the 480px block"
    assert 'min-height:44px' in action_rule.group(1)
    transfer_rule = re.search(r'\.link-transfer\{([^}]*)\}', body)
    assert transfer_rule, "expected a .link-transfer rule inside the 480px block"
    assert 'width:100%' in transfer_rule.group(1)


def test_provider_tab_id_escapes_the_code(js):
    # Item 4: defence in depth -- providerTabId() interpolates `code`
    # straight into an HTML id attribute; esc() it like every other
    # dynamic value, even though real provider codes never contain HTML
    # metacharacters.
    match = re.search(r'providerTabId:\s*function\s*\(code\)\s*\{([^}]*)\}', js)
    assert match, "expected a providerTabId function"
    assert 'esc(code' in match.group(1)


def test_media_grid_five_column_breakpoint_is_not_duplicated(css):
    # Item 5: 1024/1280/1440px used to each carry their own identical
    # repeat(5,1fr) rule -- collapse that redundancy into the one
    # min-width:1024px rule that already covers every wider viewport.
    assert not re.search(r'@media \(min-width:1280px\)\{\.media-grid\{', css)
    assert not re.search(r'@media \(min-width:1440px\)\{\.media-grid\{', css)
    rule = re.search(r'@media \(min-width:1024px\)\{\.media-grid\{([^}]*)\}\}', css)
    assert rule, "expected a single min-width:1024px .media-grid rule"
    assert 'repeat(5,1fr)' in rule.group(1)


# ---------------------------------------------------------------------------
# T13 y1-cards: cards show only title + one "年份 · 类型" line; single 2:3
# poster box shared by image/fallback/skeleton with explicit rail/grid
# widths (docs/architecture.md §1, §2).
# ---------------------------------------------------------------------------

def test_format_card_meta_function_defined(js):
    assert re.search(r'function formatCardMeta\(year, mediaType\)\s*\{', js)


def test_format_card_meta_cases(js):
    # Runs the real helper with node (a source-text regex can't exercise
    # its actual branch logic) -- both / year-only / type-only / neither.
    match = re.search(
        r"var MEDIA_TYPE_LABEL[\s\S]*?function formatCardMeta\(year, mediaType\) \{[\s\S]*?\n  \}",
        js,
    )
    assert match, "expected a formatCardMeta function"
    harness = match.group(0) + """
console.log(JSON.stringify([
  formatCardMeta(2026, "tv"),
  formatCardMeta(2018, ""),
  formatCardMeta(2018, "unknown"),
  formatCardMeta(null, "unknown"),
  formatCardMeta("", "movie"),
  formatCardMeta("", "")
]));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    # "unknown" is the real unclassified value the API emits: year only.
    assert json.loads(result.stdout) == ["2026 · 剧集", "2018", "2018", "", "电影", ""]


def test_media_card_html_uses_format_card_meta_and_has_no_removed_markup(js):
    # §1.1: a card renders ONLY the title and formatCardMeta()'s one meta
    # line -- no genre chips, status badges, counts or provider text.
    match = re.search(r'mediaCardHtml:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a mediaCardHtml function"
    body = match.group(1)
    assert "formatCardMeta(" in body
    assert "card-chips" not in body
    assert "card-original" not in body
    assert "groups-summary" not in body
    assert "badge-genre" not in body
    assert "badge-match" not in body
    assert "个版本" not in body
    assert "个来源" not in body
    assert "providerLabel(" not in body


def test_media_card_html_has_exactly_one_poster_box(js):
    # §2.1: exactly one poster box element per card -- the image and its
    # fallback both live inside the same `.poster` span, never a second
    # wrapper.
    match = re.search(r'mediaCardHtml:\s*function\s*\([^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a mediaCardHtml function"
    body = match.group(1)
    assert body.count('class="poster"') == 1


def test_render_skeleton_reuses_the_same_poster_box(js):
    # §2.1/§2.2: the loading skeleton (shown before any card has data) must
    # reuse the same nested .poster box as a real card, not size the whole
    # card element directly -- otherwise the geometry acceptance check
    # (scripts/ui_screenshots.py) can't compare loading vs. loaded states
    # with the same ".media-card .poster" selector.
    match = re.search(r'renderSkeleton:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a renderSkeleton function"
    body = match.group(1)
    assert 'class="media-card skeleton-card"' in body
    assert '<span class="poster">' in body


def test_css_poster_box_is_explicit_ratio_and_width(css):
    # §2.1: the poster box's ratio and rail width are explicit CSS custom
    # properties, not a bare literal buried in one selector.
    assert re.search(r':root\s*\{[^}]*--poster-ratio:\s*2\s*/\s*3', css)
    assert re.search(r':root\s*\{[^}]*--poster-rail-width:\s*150px', css)
    poster_rule = re.search(r'\.media-card \.poster\{([^}]*)\}', css)
    assert poster_rule, "expected a .media-card .poster rule"
    assert "var(--poster-ratio)" in poster_rule.group(1)
    assert "width:100%" in poster_rule.group(1)
    rail_rule = re.search(r'\.rail-track \.media-card\{([^}]*)\}', css)
    assert rail_rule, "expected a .rail-track .media-card rule"
    assert "var(--poster-rail-width)" in rail_rule.group(1)


def test_css_poster_img_and_skeleton_absolutely_fill_the_same_box(css):
    # §2.1: img, fallback and skeleton fill the poster box the same way
    # (position:absolute;inset:0-equivalent) -- no width/height on the
    # <img> deciding the box's own size, and the skeleton shimmer is
    # painted on the nested .poster, not a second differently-sized box.
    img_rule = re.search(r'\.media-card \.poster img\{([^}]*)\}', css)
    assert img_rule, "expected a .media-card .poster img rule"
    assert "position:absolute" in img_rule.group(1)
    assert "inset:0" in img_rule.group(1)
    assert re.search(r'\.skeleton-card \.poster\{[^}]*animation:skeleton-shimmer', css), \
        "expected the skeleton shimmer painted on the nested .poster box"
    assert not re.search(r'(?<!\S)\.skeleton-card\{[^}]*aspect-ratio', css), \
        "the outer .skeleton-card element must not carry its own aspect-ratio any more"


# ---------------------------------------------------------------------------
# T16 §4: provider icon fallback, driven from the backend enum
# (library_normalize.PROVIDERS) rather than a hardcoded JS-side list -- so
# a future provider added only on the backend is caught here instead of
# silently falling through to a missing icon.
# ---------------------------------------------------------------------------


def test_provider_icon_map_has_fallback_for_every_provider(js):
    symbol_match = re.search(r'var PROVIDER_SYMBOL = \{([\s\S]*?)\};', js)
    assert symbol_match, "expected a PROVIDER_SYMBOL map"
    symbol_body = symbol_match.group(1)
    svg = STATIC_DIR.joinpath("icons.svg").read_text(encoding="utf-8")

    for code in library_normalize.PROVIDERS:
        match = re.search(rf'["\']?{re.escape(code)}["\']?\s*:\s*"([^"]+)"', symbol_body)
        assert match, f"backend provider {code!r} has no PROVIDER_SYMBOL entry"
        assert f'id="{match.group(1)}"' in svg, f"{code!r} maps to a missing icons.svg symbol"

    # Every lookup that resolves a code to a symbol falls back to the
    # neutral "provider-link" mark when the code isn't in the map at all
    # (e.g. a provider the backend starts emitting before app.js is
    # redeployed) -- never renders no icon.
    fallback_sites = re.findall(r'meta\s*\?\s*meta\.symbol\s*:\s*"provider-link"', js)
    assert len(fallback_sites) >= 2, "expected linkRowHtml and renderProviderTabs to both fall back to provider-link"
    assert 'id="provider-link"' in svg


def test_provider_icons_are_aria_hidden_with_a_visible_text_label(js):
    # §5.2: the icon carries no accessible name of its own -- the parent
    # element's visible text (provider label) is what a screen reader
    # announces.
    link_row_match = re.search(r'linkRowHtml:\s*function\s*\(link\)\s*\{([\s\S]*?)\n    \},', js)
    assert link_row_match, "expected a linkRowHtml function"
    assert 'class="link-provider-icon provider-logo" aria-hidden="true"' in link_row_match.group(1)
    tabs_match = re.search(r'renderProviderTabs:\s*function[\s\S]{0,1400}?\n    \},', js)
    assert tabs_match, "expected a renderProviderTabs function"
    assert 'class="provider-logo" aria-hidden="true"' in tabs_match.group(0)


# ---------------------------------------------------------------------------
# T18 §12.2/§14.4: vendored icon assets (Font Awesome Free / Simple Icons,
# no emoji/CDN) and the RE0-style ratings row.
# ---------------------------------------------------------------------------

ICONS_SVG_PATH = STATIC_DIR / "icons.svg"


def test_vendored_icon_symbols_are_in_icons_svg_with_no_cdn_reference():
    assert ICONS_SVG_PATH.is_file(), "expected static/icons.svg"
    svg = ICONS_SVG_PATH.read_text(encoding="utf-8")
    for symbol_id in ("fa-film", "fa-compact-disc", "fa-photo-film", "fa-circle-half-stroke", "si-dolby", "si-tmdb", "si-imdb"):
        assert f'id="{symbol_id}"' in svg, symbol_id
    # Vendored means vendored: no runtime CDN URL reachable from the
    # rendered symbols themselves (the header comment documents where each
    # one was fetched from ONCE during implementation -- that provenance
    # note is expected, a live reference inside a <symbol>/<path> would not
    # be, which is what the src/href scan below actually rules out).
    assert "cdn.jsdelivr.net" in svg  # the documented, one-time source note
    for attr in ("src=", "href=", "xlink:href="):
        assert attr not in svg, attr


def test_app_js_references_icons_svg_only_not_a_separate_vendor_sprite_or_cdn(js):
    assert "vendor/icons-vendored" not in js
    assert "VENDOR_ICONS_URL" not in js
    for cdn_host in ("cdn.jsdelivr.net", "unpkg.com", "fontawesome.com", "simpleicons.org"):
        assert cdn_host not in js, cdn_host


def test_icon_assets_doc_records_source_version_and_licence():
    doc = (STATIC_DIR.parent / "docs" / "icon-assets.md").read_text(encoding="utf-8")
    assert "6.7.2" in doc  # Font Awesome Free pinned version
    assert "16.29.0" in doc  # Simple Icons pinned version
    assert "CC BY 4.0" in doc
    assert "CC0" in doc
    assert "Dolby" in doc and "trademark" in doc.lower()


def test_no_emoji_anywhere_in_app_js(js):
    # §12.2: "不得使用 emoji" -- scan the whole shipped source, not just the
    # new spec/rating icon helpers (CJK text/punctuation and arrows fall
    # outside both ranges, so this cannot false-positive on existing
    # Chinese copy).
    for ch in js:
        cp = ord(ch)
        assert not (0x1F300 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF), (ch, hex(cp))


def test_format_votes_compact_formatting(js):
    match = re.search(r"function formatVotes\(votes\) \{[\s\S]*?\n  \}", js)
    assert match, "expected a formatVotes function"
    harness = match.group(0) + """
console.log(JSON.stringify([
  formatVotes(31198), formatVotes(3200000), formatVotes(999), formatVotes(0), formatVotes(null)
]));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["31,198", "3.2M", "999", None, None]


RATINGS_ROW_HARNESS_HEADER = """
var ICONS_URL = "/static/icons.svg?v=test";
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
  return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
}); }
"""


def _extract_ratings_functions(js):
    votes = re.search(r"function formatVotes\(votes\) \{[\s\S]*?\n  \}", js).group(0)
    maps = re.search(r"var RATING_SOURCE_LABEL[\s\S]*?var RATING_SOURCE_ORDER = \[[^\]]*\];", js).group(0)
    unit = re.search(r"ratingUnitHtml:\s*function\s*\(source, entry\)\s*\{([\s\S]*?)\n    \},", js).group(1)
    row = re.search(r"ratingsRowHtml:\s*function\s*\(media\)\s*\{([\s\S]*?)\n    \},", js).group(1)
    return (
        RATINGS_ROW_HARNESS_HEADER + maps + "\n" + votes +
        "\nfunction ratingUnitHtml(source, entry) {" + unit + "}\n" +
        "var views = { detail: { ratingUnitHtml: ratingUnitHtml } };\n" +
        "function ratingsRowHtml(media) {" + row + "}\n"
    )


def test_ratings_row_shows_one_unit_per_available_source_in_tmdb_imdb_tvmaze_order(js):
    harness = _extract_ratings_functions(js) + """
console.log(JSON.stringify(ratingsRowHtml({ ratings: {
  tmdb: { score: 8.7, votes: 31198, url: "https://www.themoviedb.org/movie/1" },
  imdb: { score: 9.3, votes: 3200000, url: "https://www.imdb.com/title/tt1/" },
  tvmaze: { score: 8.1, votes: null, url: "https://www.tvmaze.com/shows/1" }
} })));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    html = json.loads(result.stdout)
    assert html.index("TMDB") < html.index("IMDb") < html.index("TVmaze")
    assert "8.7/10" in html and "31,198" in html
    assert "9.3/10" in html and "3.2M" in html
    assert "8.1/10" in html
    assert "0.0/10" not in html
    assert "detail-ratings-empty" not in html


def test_ratings_row_one_source_only_shows_exactly_one_unit_no_placeholders(js):
    harness = _extract_ratings_functions(js) + """
console.log(JSON.stringify(ratingsRowHtml({ ratings: {
  tmdb: { score: 6.4, votes: 812, url: "https://www.themoviedb.org/movie/2" }
} })));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    html = json.loads(result.stdout)
    assert html.count("rating-unit") == 1
    assert "IMDb" not in html
    assert "TVmaze" not in html


def test_ratings_row_all_missing_shows_plain_chinese_not_a_row_of_dashes(js):
    harness = _extract_ratings_functions(js) + """
console.log(JSON.stringify(ratingsRowHtml({ ratings: {} })));
console.log(JSON.stringify(ratingsRowHtml({})));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    for html in json.loads("[" + ",".join(result.stdout.strip().splitlines()) + "]"):
        assert "暂无评分数据" in html
        assert "—" not in html
        assert "rating-unit" not in html


def test_ratings_row_skips_a_source_with_a_non_numeric_score_never_renders_nan(js):
    # Review fix: a missing/null/NaN/string score must never render the
    # literal text "NaN" -- that source's unit is dropped entirely, same
    # as an absent source, and "暂无评分数据" only shows once every source
    # is invalid/missing.
    harness = _extract_ratings_functions(js) + """
console.log(JSON.stringify(ratingsRowHtml({ ratings: {
  tmdb: { score: null, votes: 100, url: "https://www.themoviedb.org/movie/3" },
  imdb: { score: "N/A", votes: 200, url: "https://www.imdb.com/title/tt3/" },
  tvmaze: { score: NaN, votes: 300, url: "https://www.tvmaze.com/shows/3" }
} })));
console.log(JSON.stringify(ratingsRowHtml({ ratings: {
  tmdb: { score: 7.2, votes: 100, url: "https://www.themoviedb.org/movie/4" },
  imdb: { score: undefined, votes: 200, url: "https://www.imdb.com/title/tt4/" }
} })));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    all_invalid, one_valid_one_invalid = [json.loads(line) for line in result.stdout.strip().splitlines()]
    assert "NaN" not in all_invalid
    assert "detail-ratings-empty" in all_invalid and "暂无评分数据" in all_invalid
    assert "rating-unit" not in all_invalid

    assert "NaN" not in one_valid_one_invalid
    assert one_valid_one_invalid.count("rating-unit") == 1
    assert "7.2/10" in one_valid_one_invalid
    assert "IMDb" not in one_valid_one_invalid


def test_rating_unit_is_a_button_only_when_a_safe_https_url_is_present(js):
    harness = _extract_ratings_functions(js) + """
console.log(JSON.stringify([
  ratingUnitHtml("tmdb", { score: 8.7, votes: 31198, url: "https://www.themoviedb.org/movie/1" }),
  ratingUnitHtml("tvmaze", { score: 8.1, votes: null, url: null }),
  ratingUnitHtml("imdb", { score: 9.3, votes: 100, url: "http://www.imdb.com/title/tt1/" })
]));
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    with_url, without_url, plain_http_url = json.loads(result.stdout)
    assert with_url.startswith("<button")
    assert "data-rating-url=" in with_url
    assert not without_url.startswith("<button")
    assert "data-rating-url=" not in without_url
    # Review fix: https only -- a plain http:// rating URL renders the
    # same non-interactive badge as no URL at all, never a button.
    assert not plain_http_url.startswith("<button")
    assert "data-rating-url=" not in plain_http_url


def test_bind_rating_units_opens_via_guarded_window_open(js):
    match = re.search(r"bindRatingUnits:\s*function\s*\(root\)\s*\{([\s\S]*?)\n    \},", js)
    assert match, "expected a bindRatingUnits function"
    body = match.group(1)
    assert "data-rating-url" in body
    # Review fix: https only (not http(s) like reveal's window.open) --
    # every real rating source is always https.
    assert re.search(r'\^https:\\/\\/', body), "expected an https-only scheme guard"
    assert not re.search(r'\^https\?:\\/\\/', body), "must not accept plain http"
    assert 'window.open(url, "_blank", "noopener")' in body


def test_ratings_row_placed_after_title_original_before_overview(js):
    match = re.search(r'\n    render:\s*function\s*\(media[^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a detail render function"
    body = match.group(0)
    assert body.index("detail-title") < body.index("ratingsRowHtml(media)") < body.index('id="detailOverview"')


def test_primary_rating_badge_used_on_cards_inside_the_poster_box(js):
    match = re.search(r'mediaCardHtml:\s*function\s*\(item\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a mediaCardHtml function"
    body = match.group(1)
    assert "primaryRatingBadgeHtml(item.primary_rating)" in body
    # The badge is appended inside the same `.poster` span as the image/
    # fallback, not the card footer -- placing a card's one rating in the
    # poster box corner per §14.4.
    poster_span = re.search(r'\'<span class="poster">\'([^;]*?)\'</span>\'', body)
    assert poster_span, "expected the .poster span assembly"
    assert "primaryRatingBadgeHtml" in poster_span.group(1)


def test_primary_rating_badge_html_never_renders_when_absent(js):
    match = re.search(r"function primaryRatingBadgeHtml\(primary\) \{([\s\S]*?)\n  \}", js)
    assert match, "expected a primaryRatingBadgeHtml function"
    body = match.group(1)
    assert 'if (!primary) return ""' in body


def test_primary_rating_badge_html_never_renders_nan_for_a_non_numeric_score(js):
    # Review fix: same guard as ratingUnitHtml, for the card-poster badge.
    match = re.search(r"function primaryRatingBadgeHtml\(primary\) \{([\s\S]*?)\n  \}", js)
    assert match, "expected a primaryRatingBadgeHtml function"
    maps = re.search(r"var RATING_SOURCE_LABEL[\s\S]*?var RATING_SOURCE_ORDER = \[[^\]]*\];", js).group(0)
    harness = (
        'var ICONS_URL = "/static/icons.svg?v=test";\n'
        'function esc(s) { return String(s == null ? "" : s).replace(/[&<>"\']/g, function (c) {\n'
        '  return { "&": "&amp;", "<": "&lt;", ">": "&gt;", \'"\': "&quot;", "\'": "&#39;" }[c];\n'
        "}); }\n"
        + maps + "\n"
        + "function primaryRatingBadgeHtml(primary) {" + match.group(1) + "}\n"
        + """
console.log(JSON.stringify([
  primaryRatingBadgeHtml({ source: "tmdb", score: null }),
  primaryRatingBadgeHtml({ source: "tmdb", score: "N/A" }),
  primaryRatingBadgeHtml({ source: "tmdb", score: NaN }),
  primaryRatingBadgeHtml({ source: "tmdb", score: 8.7 })
]));
"""
    )
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    null_score, string_score, nan_score, valid_score = json.loads(result.stdout)
    assert null_score == "" and string_score == "" and nan_score == ""
    assert "8.7" in valid_score and "NaN" not in valid_score


def test_card_rating_badge_has_an_accessible_name_able_role(js):
    # Review fix: aria-label on a bare <span> (role "generic") isn't
    # reliably exposed as an accessible name by every screen reader --
    # role="img" gives the badge a role that actually supports naming.
    match = re.search(r"function primaryRatingBadgeHtml\(primary\) \{([\s\S]*?)\n  \}", js)
    assert match, "expected a primaryRatingBadgeHtml function"
    body = match.group(1)
    assert re.search(r'card-rating-badge"\s*role="img"\s*aria-label=', body), (
        "expected role=\"img\" on .card-rating-badge, next to its aria-label"
    )


# ---------------------------------------------------------------------------
# T18 §14.1: provider isolation is a data-level fetch contract, not a
# visual filter -- see also test_open_fetches_media_scoped_to_the_current_
# provider_filter and test_select_provider_refetches_media_scoped_to_the_
# new_provider above. The real DOM/accessibility-tree scan against a
# synthetic multi-provider fixture lives in scripts/ui_screenshots.py's
# walkthrough (real Chromium), since a static source-text check cannot
# prove what ends up in a *rendered* DOM.
# ---------------------------------------------------------------------------

def test_open_replaces_detail_media_wholesale_never_merged(js):
    # §14.1: "不在旧 DOM 上拼接被隐藏的来源" -- render() assigns
    # state.detail.media to the fresh response outright; nothing merges
    # object keys from a previous fetch into it (a fresh open() always
    # replaces the whole detail view's content from scratch, never patches
    # a still-showing one -- see #detailContent's "加载详情…" placeholder).
    match = re.search(r'render:\s*function\s*\(media[^)]*\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a detail render function"
    body = match.group(1)
    assert "state.detail.media = media" in body
    assert "Object.assign" not in body
    assert "extend(" not in body


# ---------------------------------------------------------------------------
# T16 §5/§6: the filter drawer rebuilt as a body-portal popover / mobile
# bottom sheet -- static markup/JS contract checks. Interactive behaviour
# (focus trap, Escape, scrim, positioning within the viewport, mobile
# scroll-lock) is covered by scripts/ui_screenshots.py's Playwright
# walkthrough (real Chromium), not here.
# ---------------------------------------------------------------------------


def test_library_filters_is_a_body_level_dialog_not_nested_in_the_toolbar(page):
    # Portal-to-body (T16 §6.1): #libraryFilters must not be a descendant
    # of .filter-anchor/.library-toolbar-shell any more -- it lives as a
    # sibling at the end of <body>, alongside #transferDialog, so no
    # ancestor's overflow/stacking context can clip or bury it.
    anchor_match = re.search(r'<div class="filter-anchor">([\s\S]*?)</div>\s*(?=<)', page)
    assert anchor_match, "expected a .filter-anchor block"
    assert 'id="libraryFilters"' not in anchor_match.group(1)

    filters_tag = _tag_block(page, "libraryFilters")
    assert 'role="dialog"' in filters_tag
    assert 'aria-modal="true"' in filters_tag
    assert 'aria-labelledby="libraryFiltersTitle"' in filters_tag
    assert 'id="libraryFiltersTitle"' in page
    assert 'id="libraryFiltersScrim"' in page
    assert 'id="libraryFiltersClose"' in page


def test_filters_toggle_has_a_selected_count_badge(page):
    toggle_tag = _tag_block(page, "libraryFiltersToggle")
    assert 'aria-controls="libraryFilters"' in toggle_tag
    assert 'id="libraryFiltersBadge"' in page
    badge_tag = _tag_block(page, "libraryFiltersBadge")
    assert "hidden" in badge_tag, "badge starts hidden with no filters selected"
    assert 'aria-hidden="true"' in badge_tag


def test_update_filters_badge_counts_selections_not_totals(js):
    match = re.search(r'updateFiltersBadge:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected an updateFiltersBadge function"
    body = match.group(1)
    for field in ("l.provider.length", "l.quality.length", "l.hdr.length", "l.genre.length", "l.source.length"):
        assert field in body, field
    assert "media_total" not in body and "links_total" not in body


def test_open_filters_moves_focus_to_first_control_and_positions_panel(js):
    match = re.search(r'openFilters:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected an openFilters function"
    body = match.group(1)
    assert "positionFilters()" in body
    assert "filtersFocusable()" in body
    assert ".focus()" in body


def test_close_filters_returns_focus_only_when_asked(js):
    match = re.search(r'closeFilters:\s*function\s*\(returnFocus\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a closeFilters(returnFocus) function"
    body = match.group(1)
    assert "if (returnFocus" in body


def test_filters_keydown_traps_tab_and_closes_on_escape(js):
    assert re.search(r'if \(e\.key === "Escape"\) \{ e\.preventDefault\(\); views\.library\.closeFilters\(true\)', js)
    assert "filtersFocusable()" in js
    assert re.search(r'shiftKey && document\.activeElement === first', js)
    assert re.search(r'!e\.shiftKey && document\.activeElement === last', js)


def test_filters_close_on_search_scrim_and_scroll(js):
    search_match = re.search(r'\$\("librarySearch"\)\.onclick = function \(\) \{([\s\S]*?)\n      \};', js)
    assert search_match, "expected the #librarySearch onclick handler"
    assert "closeFilters(false)" in search_match.group(1)
    assert re.search(r'\$\("libraryFiltersScrim"\)\.onclick = function \(\) \{ views\.library\.closeFilters\(true\); \};', js)
    assert re.search(r'window\.addEventListener\("scroll", function \(\) \{[\s\S]{0,80}closeFilters\(false\)', js)
    assert re.search(r'window\.addEventListener\("resize", function \(\) \{[\s\S]{0,80}positionFilters\(\)', js)


def test_css_popover_and_mobile_sheet_breakpoint_present(css):
    popover_rule = re.search(r'\.library-filters-popover\{([^}]*)\}', css)
    assert popover_rule, "expected a .library-filters-popover rule"
    assert "position:fixed" in popover_rule.group(1)
    assert re.search(r'z-index:\s*1000', popover_rule.group(1))
    scrim_rule = re.search(r'\.popover-scrim\{([^}]*)\}', css)
    assert scrim_rule, "expected a .popover-scrim rule"
    assert "position:fixed" in scrim_rule.group(1)
    assert "inset:0" in scrim_rule.group(1)

    mobile_match = re.search(r'@media \(max-width:650px\)\{([\s\S]*?)\n\}', css)
    assert mobile_match, "expected the 650px breakpoint block"
    mobile_body = mobile_match.group(1)
    assert ".library-filters-popover{" in mobile_body
    assert "bottom:0" in mobile_body
    assert "max-height:82vh" in mobile_body


def test_filter_groups_use_fieldset_legend(page):
    for element_id, label_id in (
        ("filterProviders", "filterProvidersLabel"),
        ("filterQuality", "filterQualityLabel"),
        ("filterHdr", "filterHdrLabel"),
        ("filterGenre", "filterGenreLabel"),
        ("filterSource", "filterSourceLabel"),
    ):
        block_match = re.search(
            rf'<fieldset class="filter-group">\s*<legend[^>]*id="{label_id}"[^>]*>.*?</legend>\s*'
            rf'<div id="{element_id}"[\s\S]*?</fieldset>',
            page,
        )
        assert block_match, f"expected {element_id} inside a <fieldset><legend id={label_id}>"


def test_filter_group_order_matches_re0_hierarchy(page):
    # §6.1: 年份、网盘、画质、HDR、类型、来源、排序.
    order = ["filterYear", "filterProviders", "filterQuality", "filterHdr", "filterGenre", "filterSource", "filterSort"]
    filters_tag_start = page.index('id="libraryFilters"')
    positions = [page.index(f'id="{element_id}"', filters_tag_start) for element_id in order]
    assert positions == sorted(positions), "filter groups must appear in the RE0-derived order"
