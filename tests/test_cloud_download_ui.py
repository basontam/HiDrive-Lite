"""Front-end contract tests for 115 云下载 (spec docs/superpowers/specs/2026-09-09-115-cloud-download-design.md):
settings card, detail-page entry points, dialog cloud mode and the 云下载 tab.
Node harnesses run the real functions extracted from static/app.js."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def page(client):
    return client.get("/").get_data(as_text=True)


@pytest.fixture
def js():
    return (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def _extract(js: str, pattern: str) -> str:
    match = re.search(pattern, js)
    assert match, f"pattern not found: {pattern}"
    return match.group(1)


def _run_node(harness: str) -> str:
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _card(page: str, card_id: str) -> str:
    match = re.search(r'<article class="card" id="' + card_id + r'"[^>]*>([\s\S]*?)</article>', page)
    assert match, f"card {card_id} not found"
    return match.group(1)


# ---------------------------------------------------------------------------
# Settings card
# ---------------------------------------------------------------------------


def test_cloud_card_markup(page):
    card = _card(page, "cloudCard")
    for element_id in ("cloudEnabled", "cloudDailyCap", "cloudPerSubmitCap", "cloudSave", "cloudResult", "cloudStatusLine"):
        assert f'id="{element_id}"' in card, element_id
    assert "115 云下载" in card and "保存云下载设置" in card
    assert "0 表示不限制" in card


def _settings_harness(js: str) -> str:
    payload_fn = _extract(js, r"function cloudSavePayload\(\) \{([\s\S]*?)\n  \}")
    summary_fn = _extract(js, r"function cloudStatusSummary\(d\) \{([\s\S]*?)\n  \}")
    return """
var elements = {};
function $(id) { return elements[id] || (elements[id] = { checked: false, value: "", textContent: "", hidden: true }); }
function cloudSavePayload() {""" + payload_fn + """}
function cloudStatusSummary(d) {""" + summary_fn + """}
"""


def test_cloud_save_payload_only_three_keys(js):
    harness = _settings_harness(js) + """
$("cloudEnabled").checked = true; $("cloudDailyCap").value = ""; $("cloudPerSubmitCap").value = "12";
var a = cloudSavePayload();
$("cloudEnabled").checked = false; $("cloudDailyCap").value = "500"; $("cloudPerSubmitCap").value = "abc";
var b = cloudSavePayload();
console.log(JSON.stringify([a, b]));
"""
    a, b = json.loads(_run_node(harness))
    assert a == {"cloud_download_enabled": True, "cloud_download_daily_cap": 0, "cloud_download_per_submit_cap": 12}
    assert b == {"cloud_download_enabled": False, "cloud_download_daily_cap": 500, "cloud_download_per_submit_cap": 30}


@pytest.mark.parametrize("status,expected", [
    (None, "服务器当前：无法读取云下载状态"),
    ({"enabled": False, "today_submitted": 0, "token_available": True}, "服务器当前：云下载未启用 · 令牌已同步"),
    ({"enabled": True, "today_submitted": 7, "daily_cap": 0, "token_available": True}, "服务器当前：云下载已启用 · 今日已提交 7 条（不限） · 令牌已同步"),
    ({"enabled": True, "today_submitted": 7, "daily_cap": 100, "token_available": False}, "服务器当前：云下载已启用 · 今日已提交 7/100 条 · 令牌未同步，请先在 OpenList 中登录 115"),
])
def test_cloud_status_summary(js, status, expected):
    out = _run_node(_settings_harness(js) + "process.stdout.write(cloudStatusSummary(" + json.dumps(status, ensure_ascii=False) + "));")
    assert out == expected


def test_cloud_card_save_posts_only_cloud_keys_and_refreshes(js):
    handler = _extract(js, r'\$\("cloudSave"\)\.onclick = function \(\) \{([\s\S]*?)\n      \};')
    assert "JSON.stringify(cloudSavePayload())" in handler
    assert 'views.settingsNav.saved("cloud", revision)' in handler
    assert "refreshCloudStatus()" in handler
    assert "linkcheck" not in handler and "tmdb" not in handler


# ---------------------------------------------------------------------------
# Detail page entry points + dialog cloud mode
# ---------------------------------------------------------------------------


def _actions_harness(js: str, cloud_enabled: str) -> str:
    actions = _extract(js, r"actionButtons:\s*function\s*\(link\)\s*\{([\s\S]*?)\n    \},")
    return """
function esc(s) { return String(s == null ? "" : s); }
var state = { cloudEnabled: """ + cloud_enabled + """ };
var views = { detail: {} };
views.detail.actionButtons = function (link) {""" + actions + """};
"""


def _cloud_button(html):
    return re.search(r'<button[^>]*data-link-action="cloud"[^>]*>[^<]*</button>', html)


def test_link_row_cloud_button_conditions(js):
    base = {"link_id": "l1", "provider": "ed2k", "label": "ED2K · S01E01", "deleted": False, "invalid": False, "actions": ["copy"]}
    def render(enabled, **over):
        link = dict(base, **over)
        return _run_node(_actions_harness(js, enabled) + "process.stdout.write(views.detail.actionButtons(" + json.dumps(link, ensure_ascii=False) + "));")
    html = render("true")
    btn = _cloud_button(html)
    assert btn and "云下载" in btn.group(0) and "disabled" not in btn.group(0)
    assert 'data-link-label="ED2K · S01E01"' in btn.group(0)
    assert html.index('data-link-action="cloud"') < html.index('data-link-action="copy"')
    assert "disabled" in _cloud_button(render("true", invalid=True)).group(0)
    assert "disabled" in _cloud_button(render("true", deleted=True)).group(0)
    assert _cloud_button(render("false")) is None
    assert _cloud_button(render("true", provider="115", actions=["transfer"])) is None


def _group_harness(js: str, cloud_enabled: str) -> str:
    group_row = _extract(js, r"groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},")
    # Round 25: groupRowHtml reads its rows through visibleLinks/groupFoldOpen.
    visible_links = _extract(js, r"function visibleLinks\(group, code\) \{([\s\S]*?)\n  \}")
    usable = _extract(js, r"function linkIsUsable\(link\) \{([\s\S]*?)\n  \}")
    fold_open = _extract(js, r"function groupFoldOpen\(key\) \{([\s\S]*?)\n  \}")
    fold_button = _extract(js, r"function foldButtonHtml\(key, open\) \{([\s\S]*?)\n  \}")
    return """
var ICONS_URL = "/static/icons.svg?v=test";
function esc(s) { return String(s == null ? "" : s); }
var REVIEW_REASON_LABEL = {};
var state = { cloudEnabled: """ + cloud_enabled + """, detail: { includeDeleted: false } };
function linkIsUsable(link) {""" + usable + """}
function visibleLinks(group, code) {""" + visible_links + """}
function groupFoldOpen(key) {""" + fold_open + """}
function foldButtonHtml(key, open) {""" + fold_button + """}
function groupLiveLinkCount(g) { return g.link_count || 0; }
var views = { detail: { seasonLabel: function () { return ""; }, specRowHtml: function () { return ""; }, linkRowHtml: function () { return ""; } } };
views.detail.groupRowHtml = function (group, code) {""" + group_row + """};
"""


def test_group_cloud_button_count_excludes_invalid_and_needs_two(js):
    group = {"group_id": 7, "display_title": "4K", "link_count": 3, "links": [
        {"link_id": "a", "provider": "ed2k", "deleted": False, "invalid": False},
        {"link_id": "b", "provider": "ed2k", "deleted": False, "invalid": True},
        {"link_id": "c", "provider": "ed2k", "deleted": False, "invalid": False},
        {"link_id": "d", "provider": "115", "deleted": False, "invalid": False},
    ]}
    html = _run_node(_group_harness(js, "true") + "process.stdout.write(views.detail.groupRowHtml(" + json.dumps(group, ensure_ascii=False) + ", ''));")
    btn = re.search(r'<button[^>]*class="[^"]*group-cloud-btn[^"]*"[^>]*>([^<]*)</button>', html)
    assert btn and btn.group(1) == "整组云下载（2）"
    assert 'data-link-ids="a,c"' in btn.group(0)
    assert html.index("group-cloud-btn") < html.index("group-recheck-btn")
    single = dict(group, links=group["links"][:1])
    html_single = _run_node(_group_harness(js, "true") + "process.stdout.write(views.detail.groupRowHtml(" + json.dumps(single, ensure_ascii=False) + ", ''));")
    assert "group-cloud-btn" not in html_single
    html_off = _run_node(_group_harness(js, "false") + "process.stdout.write(views.detail.groupRowHtml(" + json.dumps(group, ensure_ascii=False) + ", ''));")
    assert "group-cloud-btn" not in html_off


def _open_cloud_harness(js: str, quota: str, per_cap: str = "30") -> str:
    open_cloud = _extract(js, r"openCloud:\s*function\s*\(linkIds, title, labels\)\s*\{([\s\S]*?)\n    \},")
    return """
var elements = {};
function el() { return { hidden: false, value: "", textContent: "", innerHTML: "", disabled: false }; }
function $(id) { return elements[id] || (elements[id] = el()); }
function esc(s) { return String(s == null ? "" : s); }
var state = { transferMode: "library", cloudPerSubmitCap: """ + per_cap + """ };
var opened = [];
var api = { request: function (url) { opened.push(url); return Promise.resolve(""" + quota + """); } };
var views = { transfer: { openCommon: function (title) { $("transferTitle").textContent = "云下载：" + title; } } };
views.transfer.openCloud = function (linkIds, title, labels) {""" + open_cloud + """};
views.transfer.openCloud(["a", "b", "c"], "虚构片", ["ED2K · S01E01", "ED2K · S01E02", "ED2K · S01E03"]);
setTimeout(function () {
  console.log(JSON.stringify({ mode: state.transferMode, ids: state.cloudLinkIds, list: $("transferCloudList").innerHTML,
    listHidden: $("transferCloudField").hidden, shareHidden: $("transferShareField").hidden, heldHidden: $("transferServerHeldField").hidden,
    btn: $("saveBtn").textContent, btnDisabled: $("saveBtn").disabled, subtitle: $("transferSubtitle").textContent, opened: opened }));
}, 10);
"""


def test_open_cloud_sets_mode_list_and_quota_subtitle(js):
    out = json.loads(_run_node(_open_cloud_harness(js, '{ success: true, surplus: 2988 }')))
    assert out["mode"] == "cloud" and out["ids"] == ["a", "b", "c"]
    assert out["list"].count("<li") == 3 and "ED2K · S01E02" in out["list"]
    assert out["listHidden"] is False and out["shareHidden"] is True and out["heldHidden"] is True
    assert out["btn"] == "云下载到 115" and out["btnDisabled"] is False
    assert out["subtitle"] == "3 条链接 · 本月剩余配额 2988"
    assert out["opened"] == ["/api/library/cloud-download/quota"]


def test_open_cloud_over_per_submit_cap_disables_submit(js):
    out = json.loads(_run_node(_open_cloud_harness(js, '{ success: true, surplus: 2988 }', per_cap="2")))
    assert out["btnDisabled"] is True and "单次最多 2 条" in out["subtitle"]


def test_open_cloud_quota_failure_still_opens(js):
    harness = _open_cloud_harness(js, "null").replace("return Promise.resolve(null)", 'return Promise.reject(new Error("boom"))')
    out = json.loads(_run_node(harness))
    assert out["mode"] == "cloud" and "配额未知" in out["subtitle"] and out["btnDisabled"] is False


def test_save_posts_cloud_payload_and_library_mode_unchanged(js):
    handler = _extract(js, r'\$\("saveBtn"\)\.onclick = function \(\) \{([\s\S]*?)\n      \};')
    cloud = re.search(r'if \(state\.transferMode === "cloud"\) \{([\s\S]*?)\n        \}', handler)
    assert cloud, "expected a cloud branch"
    assert '"/api/library/cloud-download"' in cloud.group(1)
    assert "resource_link_ids: state.cloudLinkIds" in cloud.group(1)
    assert "target_path: targetPath, target_pid: state.currentTransferPid" in cloud.group(1)
    assert '"/api/library/transfer"' in handler  # library branch untouched
    assert "cloudResultsHtml(d)" in handler


def _results_harness(js: str) -> str:
    fn = _extract(js, r"function cloudResultsHtml\(d\) \{([\s\S]*?)\n  \}")
    return """
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
function cloudResultsHtml(d) {""" + fn + """}
"""


def test_results_render_ok_failed_and_not_submitted(js):
    d = {"success": False, "message": "已提交 1 条后中止：请求过于频繁；1 条未提交", "ok": 1, "failed": 1, "not_submitted": 1, "quota_surplus": 2987,
         "results": [{"label": "ED2K · S01E01", "state": "ok", "info_hash": "h1"},
                     {"label": "ED2K · S01E02", "state": "failed", "message": "任务已存在"},
                     {"label": "ED2K · <S01E03>", "state": "not_submitted"}]}
    html = _run_node(_results_harness(js) + "process.stdout.write(cloudResultsHtml(" + json.dumps(d, ensure_ascii=False) + "));")
    assert "已提交 1 条后中止" in html and "剩余配额 2987" in html
    assert "cloud-result-ok" in html and "cloud-result-failed" in html and "cloud-result-skipped" in html
    assert "任务已存在" in html and "未提交" in html
    assert "&lt;S01E03&gt;" in html and "<S01E03>" not in html
    assert "h1" not in html.replace("S01E01", "")  # info_hash is not user-facing


def test_bind_link_actions_routes_cloud_to_open_cloud(js):
    body = _extract(js, r"bindLinkActions:\s*function\s*\(root\)\s*\{([\s\S]*?)\n    \},")
    assert 'action === "cloud"' in body and "views.transfer.openCloud([linkId]" in body and "btn.dataset.linkLabel" in body
    group = _extract(js, r"bindCloudGroupButtons:\s*function\s*\(root\)\s*\{([\s\S]*?)\n    \},")
    assert ".group-cloud-btn" in group and 'dataset.linkIds.split(",")' in group and "views.transfer.openCloud(" in group
    render_list = _extract(js, r"renderGroupList:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    assert "bindCloudGroupButtons" in render_list


def test_dialog_markup_has_cloud_list_field(page):
    assert 'id="transferCloudField"' in page and 'id="transferCloudList"' in page


# ---------------------------------------------------------------------------
# 云下载 tab
# ---------------------------------------------------------------------------


def test_cloud_tab_registered(page, js):
    assert 'data-tab="cloud"' in page and 'id="tab-cloud"' in page and 'aria-controls="cloud"' in page
    assert re.search(r'<section id="cloud" hidden role="tabpanel" aria-labelledby="tab-cloud">', page)
    for element_id in ("cloudQuotaSummary", "cloudQuotaPackages", "cloudRefresh", "cloudClearFailed", "cloudClearCompleted",
                       "cloudTabResult", "cloudTasks", "cloudPager", "cloudPrev", "cloudNext", "cloudPageText"):
        assert f'id="{element_id}"' in page, element_id
    tab_ids = json.loads(_extract(js, r'var ALL_TAB_IDS = (\[[^\]]*\]);').replace("'", '"'))
    assert tab_ids == ["library", "openlist", "strm", "cloud", "settings"]
    activate = _extract(js, r"activate:\s*function\s*\(tab\)\s*\{([\s\S]*?)\n    \},")
    assert 'tab === "cloud"' in activate and "views.cloud.enter()" in activate and "views.cloud.leave()" in activate


@pytest.mark.parametrize("status,expected", [(-2, "deleted"), (-1, "failed"), (0, "todo"), (1, "running"), (2, "done"), (7, "unknown"), (None, "unknown")])
def test_cloud_status_class_mapping(js, status, expected):
    fn = _extract(js, r"function cloudStatusClass\(status\) \{([\s\S]*?)\n  \}")
    out = _run_node("function cloudStatusClass(status) {" + fn + "}\nprocess.stdout.write(cloudStatusClass(" + json.dumps(status) + "));")
    assert out == expected


def test_cloud_quota_summary_text(js):
    fn = _extract(js, r"function cloudQuotaSummaryText\(q\) \{([\s\S]*?)\n  \}")
    harness = "function cloudQuotaSummaryText(q) {" + fn + "}\n"
    q = {"count": 3000, "used": 12, "surplus": 2988, "today_submitted": 7, "daily_cap": 0}
    assert _run_node(harness + "process.stdout.write(cloudQuotaSummaryText(" + json.dumps(q) + "));") == "本月配额 3000 · 已用 12 · 剩余 2988 · 今日已提交 7 条"
    q2 = dict(q, daily_cap=100)
    assert _run_node(harness + "process.stdout.write(cloudQuotaSummaryText(" + json.dumps(q2) + "));") == "本月配额 3000 · 已用 12 · 剩余 2988 · 今日已提交 7/100 条"
    assert _run_node(harness + "process.stdout.write(cloudQuotaSummaryText(null));") == "配额未知"


def _task_row_harness(js: str) -> str:
    row = _extract(js, r"taskRowHtml:\s*function\s*\(t\)\s*\{([\s\S]*?)\n    \},")
    status_class = _extract(js, r"function cloudStatusClass\(status\) \{([\s\S]*?)\n  \}")
    size = _extract(js, r"function cloudFormatSize\(bytes\) \{([\s\S]*?)\n  \}")
    return """
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
function cloudStatusClass(status) {""" + status_class + """}
function cloudFormatSize(bytes) {""" + size + """}
var views = { cloud: {} };
views.cloud.taskRowHtml = function (t) {""" + row + """};
"""


def test_task_row_shows_origin_status_and_actions(js):
    task = {"info_hash": "abc123", "name": "Fixture.<S01E01>.mkv", "size": 1572864, "percent": 42, "status": 1, "status_label": "下载中",
            "add_time": "2026-09-09T01:02:03+00:00", "origin": {"media_id": 5, "group_id": 7, "media_title": "虚构片", "link_label": "ED2K · S01E01"}}
    html = _run_node(_task_row_harness(js) + "process.stdout.write(views.cloud.taskRowHtml(" + json.dumps(task, ensure_ascii=False) + "));")
    assert "Fixture.&lt;S01E01&gt;.mkv" in html and "<S01E01>" not in html
    assert "虚构片 · ED2K · S01E01" in html
    assert "1.5 MB" in html and "42%" in html
    assert 'cloud-status cloud-status-running' in html and "下载中" in html
    assert 'data-cloud-delete="abc123"' in html and 'data-cloud-delete-files="abc123"' in html
    assert "ed2k://" not in html
    orphan = dict(task, origin=None, status=2, status_label="已完成", percent=100)
    html2 = _run_node(_task_row_harness(js) + "process.stdout.write(views.cloud.taskRowHtml(" + json.dumps(orphan, ensure_ascii=False) + "));")
    assert "—" in html2 and "cloud-status-done" in html2


def test_polling_starts_on_enter_and_stops_on_leave(js):
    # scoped to the views.cloud object: the reauth dialog has its own
    # start/stopPolling and views.library.onEnter also contains "enter:"
    cloud = _extract(js, r"views\.cloud = \{([\s\S]*?)\n  \};")
    enter = _extract(cloud, r"\n    enter:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    leave = _extract(cloud, r"\n    leave:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    start = _extract(cloud, r"startPolling:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    stop = _extract(cloud, r"stopPolling:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    harness = """
var events = [];
var CLOUD_POLL_INTERVAL_MS = 15000;
var document = { visibilityState: "visible" };
function setInterval(fn, ms) { events.push(["set", ms]); return 42; }
function clearInterval(id) { events.push(["clear", id]); }
var views = { cloud: { timer: null, load: function () { events.push(["load"]); return Promise.resolve(); } } };
views.cloud.startPolling = function () {""" + start + """};
views.cloud.stopPolling = function () {""" + stop + """};
views.cloud.enter = function () {""" + enter + """};
views.cloud.leave = function () {""" + leave + """};
views.cloud.enter();
views.cloud.enter();
views.cloud.leave();
views.cloud.leave();
console.log(JSON.stringify(events));
"""
    events = json.loads(_run_node(harness))
    assert events == [["load"], ["set", 15000], ["load"], ["clear", 42], ["set", 15000], ["clear", 42]]


def test_cloud_tab_delete_and_clear_confirm_and_post(js):
    cloud = _extract(js, r"views\.cloud = \{([\s\S]*?)\n  \};")
    remove = _extract(cloud, r"remove:\s*function\s*\(infoHash, deleteFiles, btn\)\s*\{([\s\S]*?)\n    \},")
    assert "window.confirm(" in remove
    assert '"/api/library/cloud-download/tasks/" + encodeURIComponent(infoHash) + "/delete"' in remove
    assert "delete_files: deleteFiles" in remove
    clear = _extract(cloud, r"clear:\s*function\s*\(scope, btn\)\s*\{([\s\S]*?)\n    \}\s*$")
    assert "window.confirm(" in clear and '"/api/library/cloud-download/tasks/clear"' in clear and "scope: scope" in clear
