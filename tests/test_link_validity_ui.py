"""w6-ui: link validity check UI (.superpowers/sdd/briefs/w6-ui.md).

The backend fields this UI consumes (check_status/check_reason/checked_at/
invalid on link objects, all_links_invalid on cards, the recheck endpoint,
GET linkcheck-status, POST /api/settings's linkcheck_* fields) are being
added by the sibling w6-checker worktree per
``.superpowers/sdd/briefs/w6-contract.md`` and are not importable here --
exactly like tests/test_pages_library.py, every check in this file is a
*static* assertion against the rendered HTML, static/app.js source text
run through node, or static/app.css source text. No test performs a live
request to a backend route that doesn't exist in this worktree yet.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@pytest.fixture
def page(client):
    response = client.get("/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.fixture
def js(client, page):
    match = re.search(r'(/static/app\.js\?v=[0-9a-f]{8})', page)
    assert match, "page must reference a versioned /static/app.js"
    response = client.get(match.group(1))
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.fixture
def css():
    return STATIC_DIR.joinpath("app.css").read_text(encoding="utf-8")


def _extract(js: str, pattern: str) -> str:
    match = re.search(pattern, js)
    assert match, f"pattern not found: {pattern}"
    return match.group(1)


def _run_node(harness: str) -> str:
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


# ---------------------------------------------------------------------------
# Shared node harness: the real linkRowHtml/actionButtons/groupRowHtml plus
# their new link-check helpers, evaluated with node exactly like
# test_pages_library.py's own harnesses (a source-text regex can't exercise
# actual branch logic).
# ---------------------------------------------------------------------------


def _link_row_harness(js: str) -> str:
    reason_label = _extract(js, r'var LINKCHECK_REASON_LABEL = (\{[\s\S]*?\});')
    error_label = _extract(js, r'var LINKCHECK_ERROR_CLASS_LABEL = (\{[\s\S]*?\});')
    error_label_fn = _extract(js, r'function linkcheckErrorClassLabel\(code\) \{([\s\S]*?)\n  \}')
    relative_time = _extract(js, r'function formatRelativeTime\(iso\) \{([\s\S]*?)\n  \}')
    tooltip = _extract(js, r'function linkInvalidTooltip\(link\) \{([\s\S]*?)\n  \}')
    status_tooltip = _extract(js, r'function linkStatusTooltip\(link\) \{([\s\S]*?)\n  \}')
    status_badge = _extract(js, r'function linkStatusBadgeHtml\(link\) \{([\s\S]*?)\n  \}')
    actions = _extract(js, r'actionButtons:\s*function\s*\(link\)\s*\{([\s\S]*?)\n    \},')
    link_row = _extract(js, r'linkRowHtml:\s*function\s*\(link\)\s*\{([\s\S]*?)\n    \},')
    return """
var ICONS_URL = "/static/icons.svg?v=test";
var PROVIDER_LABEL = { "115": "115网盘", quark: "夸克网盘", tianyicloud: "天翼云盘" };
function providerLabel(code) { return PROVIDER_LABEL[code] || code; }
var PROVIDER_META_BY_CODE = { "115": { code: "115", symbol: "provider-115" }, quark: { code: "quark", symbol: "provider-quark" } };
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
  return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
}); }
var LINKCHECK_REASON_LABEL = """ + reason_label + """;
var LINKCHECK_ERROR_CLASS_LABEL = """ + error_label + """;
function linkcheckErrorClassLabel(code) {""" + error_label_fn + """}
function formatRelativeTime(iso) {""" + relative_time + """}
function linkInvalidTooltip(link) {""" + tooltip + """}
function linkStatusTooltip(link) {""" + status_tooltip + """}
function linkStatusBadgeHtml(link) {""" + status_badge + """}
var views = { detail: {} };
views.detail.actionButtons = function (link) {""" + actions + """};
views.detail.linkRowHtml = function (link) {""" + link_row + """};
"""


def test_link_invalid_reason_label_map_matches_contract(js):
    reason_label = _extract(js, r'var LINKCHECK_REASON_LABEL = (\{[\s\S]*?\});')
    data = json.loads(re.sub(r'(\w+):', r'"\1":', reason_label))
    assert data == {
        "share_not_found": "分享不存在",
        "share_cancelled": "分享已取消",
        "share_expired": "分享已过期",
        "file_deleted": "文件已删除",
        "share_audit": "分享受限（审核/封禁）",
    }


def test_link_row_invalid_renders_red_text_badge_not_pill(js):
    harness = _link_row_harness(js) + """
console.log(JSON.stringify(views.detail.linkRowHtml({
  link_id: "l1", provider: "quark", label: "test", deleted: false, invalid: true,
  check_reason: "share_expired", checked_at: new Date().toISOString(), actions: ["open", "copy"]
})));
"""
    html = json.loads(_run_node(harness))
    assert 'class="link-invalid-text"' in html
    assert "badge-invalid" not in html
    assert "已失效" in html


def test_link_row_invalid_tooltip_has_reason_and_relative_time(js):
    checked_at = "new Date(Date.now() - 3 * 3600 * 1000).toISOString()"
    harness = _link_row_harness(js) + f"""
console.log(JSON.stringify(views.detail.linkRowHtml({{
  link_id: "l1", provider: "quark", label: "test", deleted: false, invalid: true,
  check_reason: "share_expired", checked_at: {checked_at}, actions: ["open", "copy"]
}})));
"""
    html = json.loads(_run_node(harness))
    assert "检测失效" in html
    assert "分享已过期" in html
    assert "检测于" in html
    assert "小时前" in html


def test_link_row_deleted_tooltip_is_source_deleted_only(js):
    harness = _link_row_harness(js) + """
console.log(JSON.stringify(views.detail.linkRowHtml({
  link_id: "l1", provider: "115", label: "test", deleted: true, invalid: false, actions: []
})));
"""
    html = json.loads(_run_node(harness))
    assert 'title="来源已删除"' in html
    assert "检测失效" not in html


def test_link_row_deleted_wins_over_invalid_for_tooltip(js):
    # Contract: the two states are never expected simultaneously, but if
    # they ever were, "来源已删除" must win -- deleted-at-source is a
    # stronger, unconditional fact than a checker's own signal.
    harness = _link_row_harness(js) + """
console.log(JSON.stringify(views.detail.linkRowHtml({
  link_id: "l1", provider: "115", label: "test", deleted: true, invalid: true,
  check_reason: "share_expired", checked_at: new Date().toISOString(), actions: []
})));
"""
    html = json.loads(_run_node(harness))
    assert 'title="来源已删除"' in html


def test_link_row_invalid_badge_placed_right_after_provider_label(js):
    harness = _link_row_harness(js) + """
console.log(JSON.stringify(views.detail.linkRowHtml({
  link_id: "l1", provider: "quark", label: "test", deleted: false, invalid: true,
  check_reason: "share_expired", checked_at: new Date().toISOString(), actions: ["open"]
})));
"""
    html = json.loads(_run_node(harness))
    provider_pos = html.index("link-provider")
    invalid_pos = html.index("link-invalid-text")
    remark_pos = html.index("link-remark")
    assert provider_pos < invalid_pos < remark_pos


def test_link_row_no_badge_when_not_deleted_and_not_invalid(js):
    harness = _link_row_harness(js) + """
console.log(JSON.stringify(views.detail.linkRowHtml({
  link_id: "l1", provider: "115", label: "test", deleted: false, invalid: false, actions: ["open"]
})));
"""
    html = json.loads(_run_node(harness))
    assert "link-invalid-text" not in html
    assert "已失效" not in html


def test_link_row_invalid_disables_transfer_but_keeps_open_and_copy_enabled(js):
    harness = _link_row_harness(js) + """
console.log(JSON.stringify(views.detail.linkRowHtml({
  link_id: "l1", provider: "115", label: "test", deleted: false, invalid: true,
  check_reason: "share_expired", checked_at: new Date().toISOString(),
  actions: ["transfer", "open", "copy"]
})));
"""
    html = json.loads(_run_node(harness))
    transfer_match = re.search(r'<button[^>]*data-link-action="transfer"[^>]*>', html)
    open_match = re.search(r'<button[^>]*data-link-action="open"[^>]*>', html)
    copy_match = re.search(r'<button[^>]*data-link-action="copy"[^>]*>', html)
    assert "disabled" in transfer_match.group(0)
    assert "disabled" not in open_match.group(0)
    assert "disabled" not in copy_match.group(0)


def test_link_row_deleted_still_disables_every_action(js):
    # Regression: the pre-existing deleted behaviour must be unchanged.
    harness = _link_row_harness(js) + """
console.log(JSON.stringify(views.detail.linkRowHtml({
  link_id: "l1", provider: "115", label: "test", deleted: true, invalid: false,
  actions: ["transfer", "open", "copy"]
})));
"""
    html = json.loads(_run_node(harness))
    for action in ("transfer", "open", "copy"):
        match = re.search(rf'<button[^>]*data-link-action="{action}"[^>]*>', html)
        assert "disabled" in match.group(0), f"{action} should be disabled on a deleted link"


def test_badge_invalid_pill_class_fully_removed(js, css):
    assert "badge-invalid" not in js
    assert "badge-invalid" not in css


def test_link_invalid_text_css_is_a_plain_red_text_no_pill(css):
    rule = re.search(r'\.link-invalid-text\{([^}]*)\}', css)
    assert rule, "expected a .link-invalid-text rule"
    body = rule.group(1)
    assert "color:var(--danger)" in body
    assert "font-weight:600" in body
    assert "background" not in body
    assert "padding" not in body
    assert "border-radius" not in body


# ---------------------------------------------------------------------------
# Group header: 重新检测 button + recheck() handler.
# ---------------------------------------------------------------------------


def test_group_row_has_recheck_button_next_to_link_count(js):
    match = re.search(r'groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a groupRowHtml function"
    body = match.group(1)
    assert "group-recheck-btn" in body
    assert "重新检测" in body
    title_pos = body.index("group-title-row")
    count_pos = body.index("group-count")
    btn_pos = body.index("group-recheck-btn")
    assert title_pos < count_pos < btn_pos


def test_group_row_recheck_button_carries_group_id(js):
    match = re.search(r'groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},', js)
    body = match.group(1)
    assert 'data-group-id="\' + esc(group.group_id) + \'"' in body.replace("'", "'")
    assert "data-group-id" in body


def test_recheck_function_posts_to_resource_recheck_endpoint(js):
    match = re.search(r'recheck:\s*function\s*\(groupId, btn\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a views.detail.recheck function"
    body = match.group(1)
    assert '"/api/library/resource/" + encodeURIComponent(groupId) + "/recheck"' in body
    assert '{ method: "POST" }' in body
    assert "btn.disabled = true" in body
    assert re.search(r'setTimeout\(function \(\) \{ btn\.disabled = false; \}, 60000\)', body)


def test_recheck_toast_message_includes_queued_count_and_skipped_disabled(js):
    match = re.search(r'recheck:\s*function\s*\(groupId, btn\)\s*\{([\s\S]*?)\n    \},', js)
    body = match.group(1)
    assert "已加入检测队列（" in body
    assert "d.queued" in body
    assert "所属网盘未开启检测" in body
    assert "d.skipped_disabled" in body


def test_recheck_error_reenables_button_and_shows_backend_message(js):
    match = re.search(r'recheck:\s*function\s*\(groupId, btn\)\s*\{([\s\S]*?)\n    \},', js)
    body = match.group(1)
    catch_match = re.search(r'\.catch\(function \(e\) \{([\s\S]*?)\}\);', body)
    assert catch_match, "expected a .catch handler"
    catch_body = catch_match.group(1)
    assert "btn.disabled = false" in catch_body
    assert "e.message" in catch_body


def test_bind_recheck_buttons_skips_disabled_and_wires_recheck(js):
    match = re.search(r'bindRecheckButtons:\s*function\s*\(root\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a bindRecheckButtons function"
    body = match.group(1)
    assert ".group-recheck-btn" in body
    assert "if (btn.disabled) return;" in body
    assert "views.detail.recheck(btn.dataset.groupId, btn)" in body


def test_render_group_list_binds_recheck_buttons(js):
    match = re.search(r'renderGroupList:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},', js)
    assert match, "expected a renderGroupList function"
    assert "bindRecheckButtons" in match.group(1)


# ---------------------------------------------------------------------------
# Cards: 全部失效 corner badge.
# ---------------------------------------------------------------------------


def _card_harness(js: str) -> str:
    format_meta = _extract(js, r'function formatCardMeta\(year, mediaType\) \{([\s\S]*?)\n  \}')
    media_type_label = _extract(js, r'var MEDIA_TYPE_LABEL = (\{[^}]*\});')
    rating_symbol = _extract(js, r'var RATING_SOURCE_SYMBOL = (\{[^}]*\});')
    rating_label = _extract(js, r'var RATING_SOURCE_LABEL = (\{[^}]*\});')
    primary_badge = _extract(js, r'function primaryRatingBadgeHtml\(primary\) \{([\s\S]*?)\n  \}')
    invalid_badge = _extract(js, r'function cardInvalidBadgeHtml\(allInvalid\) \{([\s\S]*?)\n  \}')
    card_html = _extract(js, r'mediaCardHtml:\s*function\s*\(item\)\s*\{([\s\S]*?)\n    \},')
    return """
var ICONS_URL = "/static/icons.svg?v=test";
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
  return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
}); }
var MEDIA_TYPE_LABEL = """ + media_type_label + """;
function formatCardMeta(year, mediaType) {""" + format_meta + """}
var RATING_SOURCE_SYMBOL = """ + rating_symbol + """;
var RATING_SOURCE_LABEL = """ + rating_label + """;
function primaryRatingBadgeHtml(primary) {""" + primary_badge + """}
function cardInvalidBadgeHtml(allInvalid) {""" + invalid_badge + """}
var views = { library: {} };
views.library.mediaCardHtml = function (item) {""" + card_html + """};
"""


def test_media_card_renders_card_invalid_badge_when_all_links_invalid(js):
    harness = _card_harness(js) + """
console.log(JSON.stringify(views.library.mediaCardHtml({
  media_id: 1, title: "测试标题", year: 2020, media_type: "movie", all_links_invalid: true
})));
"""
    html = json.loads(_run_node(harness))
    assert "card-invalid-badge" in html
    assert "全部失效" in html
    assert 'role="img"' in html


def test_media_card_no_invalid_badge_when_all_links_invalid_absent_or_false(js):
    harness = _card_harness(js) + """
var a = views.library.mediaCardHtml({ media_id: 1, title: "t", year: 2020, media_type: "movie" });
var b = views.library.mediaCardHtml({ media_id: 2, title: "t", year: 2020, media_type: "movie", all_links_invalid: false });
console.log(JSON.stringify({ a: a, b: b }));
"""
    out = json.loads(_run_node(harness))
    assert "card-invalid-badge" not in out["a"]
    assert "card-invalid-badge" not in out["b"]


def test_media_card_html_passes_all_links_invalid_into_poster_span(js):
    match = re.search(r'mediaCardHtml:\s*function\s*\(item\)\s*\{([\s\S]*?)\n    \},', js)
    body = match.group(1)
    poster_span = re.search(r"'<span class=\"poster\">'[\s\S]*?'</span>'", body)
    assert poster_span, "expected a .poster span assembly line"
    assert "cardInvalidBadgeHtml(item.all_links_invalid)" in poster_span.group(0)


def test_card_invalid_badge_css_is_opposite_corner_from_rating_badge(css):
    rating_rule = re.search(r'\.card-rating-badge\{([^}]*)\}', css)
    invalid_rule = re.search(r'\.card-invalid-badge\{([^}]*)\}', css)
    assert rating_rule and invalid_rule
    assert "left:6px" in rating_rule.group(1) and "top:6px" in rating_rule.group(1)
    assert "right:6px" in invalid_rule.group(1) and "top:6px" in invalid_rule.group(1)
    assert "position:absolute" in invalid_rule.group(1)


def test_card_invalid_badge_uses_muted_danger_color_mix(css):
    rule = re.search(r'\.card-invalid-badge\{([^}]*)\}', css)
    assert rule
    assert "color-mix(in srgb,var(--danger) 70%,var(--text))" in rule.group(1)


# ---------------------------------------------------------------------------
# Settings: "资源有效性检测" card.
# ---------------------------------------------------------------------------


def test_settings_page_has_linkcheck_card_and_ids(page):
    for element_id in ("linkcheckCard", "linkcheckEnabled", "linkcheckProviders", "linkcheckHeartbeat"):
        assert re.search(rf'id="{element_id}"', page), f"missing id={element_id}"


def test_linkcheck_card_appears_after_tmdb_card_and_before_status_card(page):
    tmdb_pos = page.index("TMDB 刮削")
    linkcheck_pos = page.index('id="linkcheckCard"')
    status_pos = page.index("<h3>服务状态</h3>")
    assert tmdb_pos < linkcheck_pos < status_pos


def test_linkcheck_card_help_text_matches_brief(page):
    assert "勾选后请点击下方「保存检测设置」才会生效" in page
    assert "115 为匿名探测，不使用你的登录会话" in page
    assert "失效判定只依据网盘的明确提示，无法确认的不标记" in page


def test_linkcheck_providers_fixed_order(js):
    match = re.search(r'var LINKCHECK_PROVIDERS = (\[[^\]]*\]);', js)
    assert match, "expected a LINKCHECK_PROVIDERS array"
    codes = json.loads(match.group(1).replace("'", '"'))
    assert codes == ["tianyicloud", "115", "quark", "alipan"]


def _settings_row_harness(js: str) -> str:
    provider_meta_block = _extract(js, r'var providerMeta = (PROVIDER_ORDER\.map[\s\S]*?\}\);)')
    provider_order = _extract(js, r'var PROVIDER_ORDER = (\[[^\]]*\]);')
    provider_label = _extract(js, r'var PROVIDER_LABEL = (\{[\s\S]*?\});')
    provider_symbol = _extract(js, r'var PROVIDER_SYMBOL = (\{[\s\S]*?\});')
    provider_accent = _extract(js, r'var PROVIDER_ACCENT = (\{[\s\S]*?\});')
    row_html = _extract(js, r'function linkcheckProviderRowHtml\(code\) \{([\s\S]*?)\n  \}')
    return """
var ICONS_URL = "/static/icons.svg?v=test";
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
  return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
}); }
var PROVIDER_LABEL = """ + provider_label + """;
var PROVIDER_ORDER = """ + provider_order + """;
var PROVIDER_SYMBOL = """ + provider_symbol + """;
var PROVIDER_ACCENT = """ + provider_accent + """;
var providerMeta = """ + provider_meta_block + """
var PROVIDER_META_BY_CODE = {};
providerMeta.forEach(function (m) { PROVIDER_META_BY_CODE[m.code] = m; });
function linkcheckProviderRowHtml(code) {""" + row_html + """}
"""


@pytest.mark.parametrize("code", ["tianyicloud", "115", "quark", "alipan"])
def test_linkcheck_provider_row_html_has_expected_ids_for_each_code(js, code):
    harness = _settings_row_harness(js) + f"""
console.log(linkcheckProviderRowHtml({json.dumps(code)}));
"""
    html = _run_node(harness)
    assert f'id="linkcheck-{code}-enabled"' in html
    assert f'id="linkcheck-{code}-cap"' in html
    assert f'id="linkcheck-{code}-status"' in html
    assert 'type="checkbox"' in html
    assert 'type="number"' in html
    assert 'min="0"' in html and 'max="20000"' in html


def test_linkcheck_provider_row_html_shows_provider_label(js):
    harness = _settings_row_harness(js) + """
console.log(linkcheckProviderRowHtml("tianyicloud"));
"""
    html = _run_node(harness)
    assert "天翼云盘" in html


def test_settings_init_wires_dirty_flag_for_global_switch_and_all_providers(js):
    match = re.search(r'init:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \}\n  \};\n\n  // -+\n  // status', js)
    assert match, "expected views.settings.init function body"
    body = match.group(1)
    assert '$("linkcheckEnabled").addEventListener("change", function () { state.linkcheckDirty = true; });' in body
    assert "LINKCHECK_PROVIDERS.forEach(function (code) {" in body
    assert '"linkcheck-" + code + "-enabled"' in body
    assert '"linkcheck-" + code + "-cap"' in body


def test_settings_save_sends_linkcheck_payload_only_when_dirty(js):
    match = re.search(r'\$\("settingsSave"\)\.onclick = function \(\) \{([\s\S]*?)\n      \};', js)
    assert match, "expected the settingsSave onclick handler"
    body = match.group(1)
    assert "if (state.linkcheckDirty) Object.assign(payload, linkcheckSavePayload());" in body
    assert "state.linkcheckDirty = false;" in body


def test_linkcheck_save_payload_reads_global_switch_and_all_provider_rows(js):
    payload_fn = _extract(js, r'function linkcheckSavePayload\(\) \{([\s\S]*?)\n  \}')
    assert "LINKCHECK_PROVIDERS.forEach" in payload_fn
    assert '"linkcheck-" + code + "-enabled"' in payload_fn
    assert '"linkcheck-" + code + "-cap"' in payload_fn
    assert "linkcheck_enabled:" in payload_fn
    assert "linkcheck_providers:" in payload_fn


def test_render_linkcheck_status_hydrates_provider_status_lines(js):
    checkbox_seed = _extract(js, r'function renderLinkcheckStatus\(d\) \{([\s\S]*?)\n  \}')
    label_fn = _extract(js, r'function linkcheckErrorClassLabel\(code\) \{([\s\S]*?)\n  \}')
    error_label = _extract(js, r'var LINKCHECK_ERROR_CLASS_LABEL = (\{[\s\S]*?\});')
    harness = """
var esc = (s) => String(s == null ? "" : s);
var LINKCHECK_PROVIDERS = ["tianyicloud", "115", "quark", "alipan"];
var LINKCHECK_ERROR_CLASS_LABEL = """ + error_label + """;
function linkcheckErrorClassLabel(code) {""" + label_fn + """}
var PROVIDER_LABEL = { "115": "115网盘", quark: "夸克网盘", tianyicloud: "天翼云盘", alipan: "阿里云盘" };
function providerLabel(code) { return PROVIDER_LABEL[code] || code; }
function linkcheckEnabledSummary(d) { return d ? "服务器当前：summary" : "服务器当前：无法读取检测状态"; }
var state = {};
var elements = {};
function $(id) {
  if (!elements[id]) elements[id] = { checked: false, value: "", innerHTML: "", textContent: "", hidden: true };
  return elements[id];
}
function renderLinkcheckStatus(d) {""" + checkbox_seed + """}
var d = {
  enabled: true,
  heartbeat_at: "2026-01-01T00:00:00Z",
  providers: {
    quark: { enabled: true, daily_cap: 3000, used_today: 45, valid: 30, invalid: 2, unknown: 3, unchecked: 5,
             paused_until: "2026-01-01T01:00:00Z", last_error_class: "rate_limited" }
  }
};
renderLinkcheckStatus(d);
console.log(JSON.stringify({
  enabledChecked: elements["linkcheckEnabled"].checked,
  quarkEnabled: elements["linkcheck-quark-enabled"].checked,
  quarkCap: elements["linkcheck-quark-cap"].value,
  quarkStatus: elements["linkcheck-quark-status"].innerHTML,
  heartbeat: elements["linkcheckHeartbeat"].textContent
}));
"""
    out = json.loads(_run_node(harness))
    assert out["enabledChecked"] is True
    assert out["quarkEnabled"] is True
    assert out["quarkCap"] == 3000
    assert "今日 45/3000" in out["quarkStatus"]
    assert "有效 30" in out["quarkStatus"]
    assert "失效 2" in out["quarkStatus"]
    assert "暂停至" in out["quarkStatus"]
    assert "被限流" in out["quarkStatus"]
    assert "最近心跳" in out["heartbeat"]


def test_render_linkcheck_status_does_not_overwrite_dirty_fields(js):
    checkbox_seed = _extract(js, r'function renderLinkcheckStatus\(d\) \{([\s\S]*?)\n  \}')
    label_fn = _extract(js, r'function linkcheckErrorClassLabel\(code\) \{([\s\S]*?)\n  \}')
    error_label = _extract(js, r'var LINKCHECK_ERROR_CLASS_LABEL = (\{[\s\S]*?\});')
    harness = """
var esc = (s) => String(s == null ? "" : s);
var LINKCHECK_PROVIDERS = ["tianyicloud", "115", "quark", "alipan"];
var LINKCHECK_ERROR_CLASS_LABEL = """ + error_label + """;
function linkcheckErrorClassLabel(code) {""" + label_fn + """}
var PROVIDER_LABEL = { "115": "115网盘", quark: "夸克网盘", tianyicloud: "天翼云盘", alipan: "阿里云盘" };
function providerLabel(code) { return PROVIDER_LABEL[code] || code; }
function linkcheckEnabledSummary(d) { return d ? "服务器当前：summary" : "服务器当前：无法读取检测状态"; }
var state = { linkcheckDirty: true };
var elements = {};
function $(id) {
  if (!elements[id]) elements[id] = { checked: false, value: "", innerHTML: "", textContent: "", hidden: true };
  return elements[id];
}
elements["linkcheckEnabled"] = { checked: true };
elements["linkcheck-quark-enabled"] = { checked: false };
elements["linkcheck-quark-cap"] = { value: "999" };
function renderLinkcheckStatus(d) {""" + checkbox_seed + """}
renderLinkcheckStatus({ enabled: false, providers: { quark: { enabled: true, daily_cap: 3000 } } });
console.log(JSON.stringify({
  enabledChecked: elements["linkcheckEnabled"].checked,
  quarkEnabled: elements["linkcheck-quark-enabled"].checked,
  quarkCap: elements["linkcheck-quark-cap"].value
}));
"""
    out = json.loads(_run_node(harness))
    # A dirty page session must never be clobbered by a background refresh.
    assert out["enabledChecked"] is True
    assert out["quarkEnabled"] is False
    assert out["quarkCap"] == "999"


# ---------------------------------------------------------------------------
# formatRelativeTime bucket boundaries.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds_ago,expected_substring",
    [
        (10, "刚刚"),
        (5 * 60, "分钟前"),
        (3 * 3600, "小时前"),
        (10 * 86400, "天前"),
        (6 * 30 * 86400, "个月前"),
        (2 * 365 * 86400, "年前"),
    ],
)
def test_format_relative_time_buckets(js, seconds_ago, expected_substring):
    relative_time = _extract(js, r'function formatRelativeTime\(iso\) \{([\s\S]*?)\n  \}')
    harness = f"""
function formatRelativeTime(iso) {{{relative_time}}}
var iso = new Date(Date.now() - {seconds_ago} * 1000).toISOString();
console.log(formatRelativeTime(iso));
"""
    out = _run_node(harness).strip()
    assert expected_substring in out


def test_format_relative_time_handles_missing_or_invalid_input(js):
    relative_time = _extract(js, r'function formatRelativeTime\(iso\) \{([\s\S]*?)\n  \}')
    harness = f"""
function formatRelativeTime(iso) {{{relative_time}}}
console.log(JSON.stringify({{ nullVal: formatRelativeTime(null), badVal: formatRelativeTime("not-a-date") }}));
"""
    out = json.loads(_run_node(harness))
    assert out["nullVal"] == ""
    assert out["badVal"] == ""


# ---------------------------------------------------------------------------
# Settings card: its own save button + a "what the server has" summary
# (round 15: a user ticked every switch, reloaded, and lost them all --
# the only save button lived in another card).
# ---------------------------------------------------------------------------


@pytest.fixture
def html():
    return (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")


def test_linkcheck_card_has_its_own_save_button_and_feedback(html):
    card = re.search(r'<article class="card" id="linkcheckCard">([\s\S]*?)</article>', html).group(1)
    assert 'id="linkcheckSave"' in card
    assert 'id="linkcheckResult"' in card
    assert "保存检测设置" in card


def test_linkcheck_save_posts_only_the_two_linkcheck_keys(js):
    handler = _extract(js, r'\$\("linkcheckSave"\)\.onclick = function \(\) \{([\s\S]*?)\n      \};')
    assert 'JSON.stringify(linkcheckSavePayload())' in handler
    assert "115_target_pid" not in handler and "tmdb" not in handler
    assert "state.linkcheckDirty = false" in handler


def _summary_harness(js: str) -> str:
    summary = _extract(js, r'function linkcheckEnabledSummary\(d\) \{([\s\S]*?)\n  \}')
    return """
var LINKCHECK_PROVIDERS = ["tianyicloud", "115", "quark", "alipan"];
var PROVIDER_LABEL = { "115": "115网盘", quark: "夸克网盘", tianyicloud: "天翼云盘", alipan: "阿里云盘" };
function providerLabel(code) { return PROVIDER_LABEL[code] || code; }
function linkcheckEnabledSummary(d) {""" + summary + """}
"""


@pytest.mark.parametrize(
    "status,expected",
    [
        (None, "服务器当前：无法读取检测状态"),
        ({"enabled": False, "providers": {}}, "服务器当前：检测未启用"),
        ({"enabled": False, "providers": {"alipan": {"enabled": True}}}, "服务器当前：检测未启用（已勾选 阿里云盘，但总开关未开）"),
        ({"enabled": True, "providers": {}}, "服务器当前：总开关已开，但没有启用任何网盘"),
        (
            {"enabled": True, "providers": {"tianyicloud": {"enabled": True}, "115": {"enabled": False}, "alipan": {"enabled": True}}},
            "服务器当前：检测已启用（天翼云盘、阿里云盘）",
        ),
    ],
)
def test_linkcheck_enabled_summary(js, status, expected):
    out = _run_node(_summary_harness(js) + "process.stdout.write(linkcheckEnabledSummary(" + json.dumps(status, ensure_ascii=False) + "));")
    assert out == expected


# ---------------------------------------------------------------------------
# Round 16: every link row carries a status word next to the provider label
# -- 有效 (green) / 已失效 (red) / 待确认 (orange) / 未检测 (grey) -- plain
# text, existing colour tokens, no emoji/icons.
# ---------------------------------------------------------------------------


def _row(js, link):
    harness = _link_row_harness(js) + "console.log(JSON.stringify(views.detail.linkRowHtml(" + json.dumps(link, ensure_ascii=False) + ")));"
    return json.loads(_run_node(harness))


def _badge(html):
    match = re.search(r'<span class="(link-invalid-text|link-status-text[^"]*)"[^>]*>([^<]*)</span>', html)
    assert match, html
    return match.group(1), match.group(2), re.search(r'title="([^"]*)"', match.group(0)).group(1)


def test_link_row_valid_shows_green_youxiao_with_checked_time(js):
    html = _row(js, {"link_id": "l1", "provider": "quark", "label": "t", "deleted": False, "invalid": False,
                     "check_status": "valid", "check_reason": "ok",
                     "checked_at": "2026-09-07T00:00:00Z", "actions": ["transfer", "open", "copy"]})
    cls, text, title = _badge(html)
    assert cls == "link-status-text link-status-valid" and text == "有效"
    assert title.startswith("检测有效 · 检测于 ")
    assert 'data-link-action="transfer"' in html and "disabled" not in html


def test_link_row_unknown_shows_orange_daiqueren_with_friendly_reason(js):
    html = _row(js, {"link_id": "l1", "provider": "tianyicloud", "label": "t", "deleted": False, "invalid": False,
                     "check_status": "unknown", "check_reason": "network_error",
                     "checked_at": "2026-09-07T00:00:00Z", "actions": ["transfer", "open"]})
    cls, text, title = _badge(html)
    assert cls == "link-status-text link-status-unknown" and text == "待确认"
    assert title.startswith("待确认 · 网络错误 · 检测于 ")
    assert "disabled" not in html


@pytest.mark.parametrize("reason,label", [
    ("parse_error", "解析失败"), ("http_4xx", "网盘返回 4xx 错误"), ("unmapped_response", "网盘返回未识别的结果"),
])
def test_link_row_unknown_reasons_are_friendly_text(js, reason, label):
    html = _row(js, {"link_id": "l1", "provider": "115", "label": "t", "deleted": False, "invalid": False,
                     "check_status": "unknown", "check_reason": reason, "checked_at": "2026-09-07T00:00:00Z", "actions": ["open"]})
    assert label in _badge(html)[2]


def test_link_row_unchecked_shows_grey_weijiance(js):
    html = _row(js, {"link_id": "l1", "provider": "115", "label": "t", "deleted": False, "invalid": False,
                     "check_status": None, "check_reason": None, "checked_at": None, "actions": ["transfer", "open"]})
    cls, text, title = _badge(html)
    assert cls == "link-status-text link-status-unchecked" and text == "未检测"
    assert title == "尚未检测"
    assert "disabled" not in html


def test_link_row_invalid_keeps_red_yishixiao_and_disables_transfer_only(js):
    html = _row(js, {"link_id": "l1", "provider": "quark", "label": "t", "deleted": False, "invalid": True,
                     "check_status": "invalid", "check_reason": "share_expired",
                     "checked_at": "2026-09-07T00:00:00Z", "actions": ["transfer", "open", "copy"]})
    cls, text, title = _badge(html)
    assert cls == "link-invalid-text" and text == "已失效"
    assert title.startswith("检测失效 · 分享已过期 · 检测于 ")
    assert "disabled" in re.search(r'<button[^>]*data-link-action="transfer"[^>]*>', html).group(0)
    assert "disabled" not in re.search(r'<button[^>]*data-link-action="open"[^>]*>', html).group(0)


def test_link_row_deleted_wins_over_valid_verdict(js):
    html = _row(js, {"link_id": "l1", "provider": "115", "label": "t", "deleted": True, "invalid": False,
                     "check_status": "valid", "check_reason": "ok", "checked_at": "2026-09-07T00:00:00Z",
                     "actions": ["transfer", "open", "copy"]})
    cls, text, title = _badge(html)
    assert cls == "link-invalid-text" and text == "已失效" and title == "来源已删除"
    for action in ("transfer", "open", "copy"):
        assert "disabled" in re.search(rf'<button[^>]*data-link-action="{action}"[^>]*>', html).group(0)


def test_status_words_are_plain_text_without_emoji_or_icons(js):
    body = _extract(js, r'function linkStatusBadgeHtml\(link\) \{([\s\S]*?)\n  \}')
    assert "<svg" not in body and "<img" not in body and "http" not in body
    assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", body)
    for word in ("有效", "已失效", "待确认", "未检测"):
        assert word in body


def _group_row_harness(js: str) -> str:
    group_row = _extract(js, r'groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},')
    live_count = _extract(js, r'function groupLiveLinkCount\(group\) \{([\s\S]*?)\n  \}')
    return _link_row_harness(js) + """
var REVIEW_REASON_LABEL = {};
function groupLiveLinkCount(group) {""" + live_count + """}
views.detail.seasonLabel = function () { return ""; };
views.detail.specRowHtml = function () { return ""; };
views.detail.groupRowHtml = function (group, code) {""" + group_row + """};
"""


def test_mixed_group_shows_valid_and_invalid_status_side_by_side(js):
    group = {
        "group_id": 7, "display_title": "4K", "providers": {"115": 1}, "link_count": 1, "links": [
            {"link_id": "a", "provider": "115", "label": "t", "deleted": False, "invalid": False,
             "check_status": "valid", "check_reason": "ok", "checked_at": "2026-09-07T00:00:00Z", "actions": ["transfer", "open"]},
            {"link_id": "b", "provider": "quark", "label": "t", "deleted": False, "invalid": True,
             "check_status": "invalid", "check_reason": "share_cancelled", "checked_at": "2026-09-07T00:00:00Z", "actions": ["transfer", "open"]},
        ],
    }
    harness = _group_row_harness(js) + "console.log(JSON.stringify(views.detail.groupRowHtml(" + json.dumps(group, ensure_ascii=False) + ", '')));"
    html = json.loads(_run_node(harness))
    assert html.count('class="link-row') == 2
    assert re.search(r'link-status-valid"[^>]*>有效', html) and 'class="link-invalid-text"' in html
    assert "1 条链接" in html


def test_all_invalid_group_says_quanbu_shixiao_instead_of_zero_links(js):
    group = {
        "group_id": 8, "display_title": "1080p", "providers": {}, "link_count": 0, "links": [
            {"link_id": "c", "provider": "tianyicloud", "label": "t", "deleted": False, "invalid": True,
             "check_status": "invalid", "check_reason": "file_deleted", "checked_at": "2026-09-07T00:00:00Z", "actions": ["open"]},
        ],
    }
    harness = _group_row_harness(js) + "console.log(JSON.stringify(views.detail.groupRowHtml(" + json.dumps(group, ensure_ascii=False) + ", '')));"
    html = json.loads(_run_node(harness))
    assert "全部失效" in html and "0 条链接" not in html
    assert "已失效" in html and "暂无可用链接" not in html


def test_detail_fetch_passes_include_deleted_when_filter_active(js):
    helper = _extract(js, r'function detailRequestUrl\(mediaId, provider\) \{([\s\S]*?)\n  \}')
    assert "include_deleted=1" in helper and "state.library.includeDeleted" in helper
    open_fn = _extract(js, r'open:\s*function\s*\(mediaId\)\s*\{([\s\S]*?)\n    \},')
    select_fn = _extract(js, r'selectProvider:\s*function\s*\(code\)\s*\{([\s\S]*?)\n    \},')
    assert "detailRequestUrl(" in open_fn and "detailRequestUrl(" in select_fn


def test_matches_provider_honours_include_deleted_for_all_invalid_groups(js):
    body = _extract(js, r'matchesProvider:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},')
    harness = """
var state = { detail: { includeDeleted: false } };
var views = { detail: {} };
views.detail.matchesProvider = function (group, code) {""" + body + """};
var dead = { providers: {}, links: [{ provider: "quark", invalid: true }] };
var out = [views.detail.matchesProvider(dead, "quark"), views.detail.matchesProvider(dead, "")];
state.detail.includeDeleted = true;
out.push(views.detail.matchesProvider(dead, "quark"), views.detail.matchesProvider(dead, "115"));
console.log(JSON.stringify(out));
"""
    assert json.loads(_run_node(harness)) == [False, True, True, False]
