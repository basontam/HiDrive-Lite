"""Front-end contract tests for the RE0 federated-search lane, the detail
page's RE0 candidates section and the settings sync card (node harnesses run
the real functions extracted from static/app.js)."""

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


ESC = 'function esc(s) { return String(s == null ? "" : s).replace(/[&<>"\']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", \'"\': "&quot;", "\'": "&#39;" }[c]; }); }\n'


def test_markup_has_remote_lane_and_sync_card(page):
    for element_id in ("libraryRemote", "libraryRemoteStatus", "libraryRemoteResults", "re0SyncCard", "re0SyncStatus", "re0SyncRefresh"):
        assert f'id="{element_id}"' in page, element_id
    assert "RE0 资源" in page and "RE0 资源同步" in page
    assert "展开" not in page


@pytest.mark.parametrize("remote,expected", [
    ({"status": "fresh", "items": [1]}, "RE0 找到 1 个媒体"),
    ({"status": "cached", "items": [1, 2]}, "RE0 找到 2 个媒体（缓存）"),
    ({"status": "fresh", "items": []}, "RE0 没有对应资源"),
    ({"status": "no_candidates", "items": []}, "RE0 未找到对应媒体"),
    ({"status": "rate_limited", "items": [], "retry_after": 90}, "RE0 限流，90 秒后再试"),
    ({"status": "reauth_required", "items": []}, "需要重新授权 RE0"),
    ({"status": "scope_denied", "items": []}, "RE0 权限不足"),
    ({"status": "quota_exhausted", "items": []}, "今日 RE0 查询额度已用完"),
    ({"status": "tmdb_budget_exhausted", "items": []}, "TMDB 额度已用完"),
    ({"status": "upstream_5xx", "items": []}, "RE0 暂时不可用"),
    (None, "RE0 查询失败"),
])
def test_remote_status_label(js, remote, expected):
    fn = _extract(js, r"function remoteStatusLabel\(d\) \{([\s\S]*?)\n  \}")
    out = _run_node("function remoteStatusLabel(d) {" + fn + "}\nprocess.stdout.write(remoteStatusLabel(" + json.dumps(remote, ensure_ascii=False) + "));")
    assert out == expected


def _card_harness(js: str) -> str:
    fn = _extract(js, r"remoteCardHtml:\s*function\s*\(item\)\s*\{([\s\S]*?)\n    \},")
    return ESC + """
var ICONS_URL = "/static/icons.svg?v=test";
function formatCardMeta(year, type) { return (year || "") + " · " + (type === "tv" ? "剧集" : "电影"); }
function primaryRatingBadgeHtml() { return ""; }
var views = { library: {} };
views.library.remoteCardHtml = function (item) {""" + fn + """};
"""


def test_remote_card_html_has_ref_badge_and_same_geometry(js):
    item = {"media_ref": "re0:movie:999", "local_media_id": None, "media_type": "movie", "tmdb_id": 999, "title": "远端<片>", "year": 2024,
            "poster_url": "https://img.test/p.jpg", "state": "candidate", "candidate_count": 2, "providers": ["115", "quark"]}
    html = _run_node(_card_harness(js) + "process.stdout.write(views.library.remoteCardHtml(" + json.dumps(item, ensure_ascii=False) + "));")
    assert 'class="media-card media-card-remote"' in html and 'data-media-ref="re0:movie:999"' in html
    assert "远端&lt;片&gt;" in html and "RE0 待解锁" in html and 'class="card-title"' in html and 'class="poster"' in html
    unlocked = dict(item, state="unlocked")
    assert "RE0 已解锁" in _run_node(_card_harness(js) + "process.stdout.write(views.library.remoteCardHtml(" + json.dumps(unlocked, ensure_ascii=False) + "));")


def test_unified_card_keeps_source_rating_and_scopes_invalid_badge(js):
    fn = _extract(js, r"mediaCardHtml:\s*function\s*\(item\)\s*\{([\s\S]*?)\n    \},")
    harness = ESC + """
var ICONS_URL = '/static/icons.svg';
function formatCardMeta() { return '2014 · 剧集'; }
function primaryRatingBadgeHtml() { return '<span class="card-rating-badge">8.8</span>'; }
function cardInvalidBadgeHtml(invalid) { return invalid ? '<span class="card-invalid-badge">失效</span>' : ''; }
function card(item) {""" + fn + """}
console.log(JSON.stringify([
  card({media_id: 7, media_type: 'tv', title: '失踪', sources: ['local', 're0'], all_links_invalid: true, has_usable_re0: true}),
  card({media_id: 8, media_type: 'tv', title: '失踪', sources: ['re0'], all_links_invalid: true, has_usable_re0: true}),
  card({media_id: 9, media_type: 'tv', title: '失踪', all_links_invalid: true}),
  card({media_id: 10, media_type: 'tv', title: '失踪', sources: ['re0'], all_links_invalid: true, has_usable_re0: false})
]));
"""
    combined, remote, dead_local, dead_audit = json.loads(_run_node(harness))
    assert "本地 + RE0" in combined and "本地 + RE0" not in remote
    for html in (combined, remote):
        assert 'card-source-badge' in html and 'card-rating-badge' in html and 'card-invalid-badge' not in html
    assert 'card-invalid-badge' in dead_local
    assert 'card-invalid-badge' in dead_audit and 'card-source-badge' in dead_audit
    css = (ROOT / 'static/app.css').read_text()
    assert '.card-source-badge{top:auto;bottom:6px}' in css


def test_detail_request_url_handles_re0_ref(js):
    fn = _extract(js, r"function detailRequestUrl\(mediaId, provider\) \{([\s\S]*?)\n  \}")
    harness = """
var state = { library: { includeDeleted: false }, detail: {} };
function detailRequestUrl(mediaId, provider) {""" + fn + """}
console.log(JSON.stringify([detailRequestUrl("re0:movie:999", ""), detailRequestUrl("re0:tv:3", "115"), detailRequestUrl("12", "quark")]));
"""
    assert json.loads(_run_node(harness)) == ["/api/library/re0-media/movie/999", "/api/library/re0-media/tv/3?provider=115", "/api/library/media/12?provider=quark"]


def _re0_row_harness(js: str) -> str:
    row = _extract(js, r"re0RowHtml:\s*function\s*\(c\)\s*\{([\s\S]*?)\n    \},")
    points = _extract(js, r"function re0PointsLabel\(c\) \{([\s\S]*?)\n  \}")
    size = _extract(js, r"function re0SizeLabel\(v\) \{([\s\S]*?)\n  \}")
    published = _extract(js, r"function re0PublishedLabel\(iso\) \{([\s\S]*?)\n  \}")
    completion = _extract(js, r"function re0CompletionLabel\(composition\) \{([\s\S]*?)\n  \}")
    completion_labels = _extract(js, r"var RE0_COMPLETION_LABEL = (\{[\s\S]*?\});")
    preview_ctl = _extract(js, r"function re0PreviewControlHtml\(c\) \{([\s\S]*?)\n  \}")
    show_comp = _extract(js, r"function showRe0Composition\(mediaType\) \{([\s\S]*?)\n  \}")
    return ESC + """
function re0PublishedLabel(iso) {""" + published + """}
var RE0_COMPLETION_LABEL = """ + completion_labels + """;
function re0CompletionLabel(composition) {""" + completion + """}
function re0PreviewControlHtml(c) {""" + preview_ctl + """}
function showRe0Composition(mediaType) {""" + show_comp + """}
var state = { detail: { media: { media_type: (typeof MEDIA_TYPE === "undefined" ? "tv" : MEDIA_TYPE) } } };
""" + """
var ICONS_URL = "/static/icons.svg?v=test";
var PROVIDER_META_BY_CODE = { "115": { symbol: "provider-115" }, quark: { symbol: "provider-quark" } };
var RE0_ACTION_LABEL = { transfer: "解锁并转存", copy: "解锁并复制链接", cloud: "解锁并开启云下载" };
var RE0_DONE_ACTION_LABEL = { transfer: "转存到 115", copy: "复制链接", cloud: "云下载到 115" };
function re0PointsLabel(c) {""" + points + """}
function re0SizeLabel(v) {""" + size + """}
var views = { detail: {} };
views.detail.re0RowHtml = function (c) {""" + row + """};
"""


def _cand(**over):
    base = {"id": 11, "provider": "115", "provider_label": "115 网盘", "state": "candidate", "unlock_points": 5, "action": "transfer",
            "specs": {"resolution": {"value": "4K", "icon": "icon-resolution"}, "source": {"value": "WEB-DL", "icon": "icon-source"}},
            "source_status": "valid", "resource_link_id": None, "subtitle_language": ["中文"], "subtitle_type": [],
            "title": "本地片.2019.2160p.WEB-DL.H265", "size": "12.3 GB"}
    base.update(over)
    return base


def test_re0_row_actions_by_state(js):
    def render(c):
        return _run_node(_re0_row_harness(js) + "process.stdout.write(views.detail.re0RowHtml(" + json.dumps(c, ensure_ascii=False) + "));")
    html = render(_cand())
    assert 'data-re0-action="transfer"' in html and 'data-re0-id="11"' in html and ">解锁并转存<" in html
    assert "RE0 待解锁" in html and "需 5 积分" in html and "4K" in html and "WEB-DL" in html
    assert "disabled" not in html and "展开" not in html
    free = render(_cand(unlock_points=0))
    assert "免费解锁" in free
    unknown_points = render(_cand(unlock_points=None))
    assert "以 RE0 返回为准" in unknown_points
    done = render(_cand(state="unlocked", resource_link_id="re0-abc"))
    assert ">转存到 115<" in done and "已解锁" in done and "解锁并" not in done and 'data-link-id="re0-abc"' in done
    copy_row = render(_cand(provider="tianyicloud", provider_label="天翼云盘", action="copy"))
    assert ">解锁并复制链接<" in copy_row
    cloud_row = render(_cand(provider="ed2k", provider_label="ED2K", action="cloud"))
    assert ">解锁并开启云下载<" in cloud_row
    unmapped = render(_cand(provider="unknown", provider_label="其他", action="unavailable"))
    assert "暂不可用" in unmapped and "disabled" in unmapped
    no_payload = render(_cand(state="already_unlocked", last_error_class="already_unlocked_no_payload"))
    assert "等待链接同步" in no_payload
    for candidate in (_cand(state="already_unlocked"), _cand(is_unlocked_upstream=True)):
        owned = render(candidate)
        assert "RE0 已解锁" in owned and "等待链接同步" in owned
        assert "需 5 积分" not in owned and "待解锁" not in owned
    owned_unmapped = render(_cand(is_unlocked_upstream=True, provider="unknown", action="unavailable"))
    assert "已解锁" in owned_unmapped and "disabled" in owned_unmapped
    assert 'data-re0-action="unavailable"' not in owned_unmapped


def test_re0_row_shows_resource_title_size_and_specs(js):
    def render(c):
        return _run_node(_re0_row_harness(js) + "process.stdout.write(views.detail.re0RowHtml(" + json.dumps(c, ensure_ascii=False) + "));")
    html = render(_cand())
    # The pan is the block heading; each row is marked RE0 and carries the resource's own name, size and specs.
    assert 'class="re0-badge"' in html and ">RE0<" in html
    assert 'class="re0-title"' in html and "本地片.2019.2160p.WEB-DL.H265" in html and "12.3 GB" in html
    assert "4K" in html and "WEB-DL" in html and "115 网盘" not in html
    numeric = render(_cand(size=4500000000))
    assert "4.19 GB" in numeric
    nameless = render(_cand(title=None, size=None))
    assert "未提供名称" in nameless and "GB" not in nameless
    escaped = render(_cand(title="<b>x</b>"))
    assert "&lt;b&gt;x&lt;/b&gt;" in escaped and "<b>x</b>" not in escaped


def test_provider_tab_count_includes_re0_candidates(js):
    tabs = _extract(js, r"renderProviderTabs:\s*function\s*\(facets, selected, scopedFetch\)\s*\{([\s\S]*?)\n    \},")
    harness = ESC + """
var ICONS_URL = "/static/icons.svg?v=test";
var PROVIDER_META_BY_CODE = { "115": { symbol: "provider-115" } };
function providerLabel(code) { return code; }
var views = { detail: { providerTabId: function (code) { return "providerTab-" + (code || "all"); } } };
views.detail.renderProviderTabs = function (facets, selected, scopedFetch) {""" + tabs + """};
process.stdout.write(views.detail.renderProviderTabs([
  { provider: "115", label: "115 网盘", link_count: 1, re0_count: 2 },
  { provider: "quark", label: "夸克网盘", link_count: 0, re0_count: 1 },
  { provider: "ed2k", label: "ED2K", link_count: 4 }], "", false));
"""
    html = _run_node(harness)
    counts = re.findall(r'class="provider-tab-count">(\d+)<', html)
    assert counts == ["3", "1", "4"]


def test_detail_render_asks_for_a_stale_re0_refresh_once_and_reloads_on_any_fetch(js):
    render = _extract(js, r"\n    render:\s*function\s*\(media, scopedFetch\)\s*\{([\s\S]*?)\n    \},")
    assert "views.detail.ensureRe0Fresh(media)" in render
    fn = _extract(js, r"ensureRe0Fresh:\s*function\s*\(media\)\s*\{([\s\S]*?)\n    \},")
    harness = """
var posts = []; var reloads = 0; var toasts = 0;
var now = 1000000; Date.now = function () { return now; };
function toast() { toasts++; }
var state = { detail: {}, library: { media: "fixture-media" } };
var api = { request: function (url, opts) { posts.push({ url: url, body: JSON.parse(opts.body) }); return Promise.resolve(api.next); } };
var views = { detail: { reloadRe0: function () { reloads++; return Promise.resolve(); } } };
views.detail.ensureRe0Fresh = function (media) {""" + fn + """};
api.next = { success: true, fetched: true, report: { new: 1, remote_items: 2 } };
Promise.resolve()
  .then(function () { return views.detail.ensureRe0Fresh({ media_type: "tv", tmdb_id: 125988 }); })
  .then(function () { return views.detail.ensureRe0Fresh({ media_type: "tv", tmdb_id: 125988 }); })   // same media: not asked twice
  .then(function () { api.next = { success: true, fetched: false, skipped: "fresh" }; return views.detail.ensureRe0Fresh({ media_type: "movie", tmdb_id: 7 }); })
  .then(function () { api.next = { success: true, fetched: true, report: { new: 0, remote_items: 0 } }; return views.detail.ensureRe0Fresh({ media_type: "movie", tmdb_id: 8 }); })
  .then(function () { return views.detail.ensureRe0Fresh({ media_type: "tv", tmdb_id: null }); })
  .then(function () { api.request = function (url, opts) { posts.push({ url: url, body: JSON.parse(opts.body) }); return Promise.reject(new Error("quota")); }; return views.detail.ensureRe0Fresh({ media_type: "movie", tmdb_id: 9 }); })
  .then(function () { now += 300001; return views.detail.ensureRe0Fresh({ media_type: "movie", tmdb_id: 9 }); })
  .then(function () { console.log(JSON.stringify({ posts: posts, reloads: reloads, toasts: toasts })); });
"""
    out = json.loads(_run_node(harness))
    assert [p["body"] for p in out["posts"]] == [
        {"media_type": "tv", "tmdb_id": 125988, "if_stale": True}, {"media_type": "movie", "tmdb_id": 7, "if_stale": True},
        {"media_type": "movie", "tmdb_id": 8, "if_stale": True}, {"media_type": "movie", "tmdb_id": 9, "if_stale": True},
        {"media_type": "movie", "tmdb_id": 9, "if_stale": True}]
    assert all(p["url"] == "/api/library/re0/refresh" for p in out["posts"])
    # Round 40: both fetches reload -- the one that added a candidate and the
    # one that added none, since the same answer can have taken candidates
    # away (upstream marked a share invalid) and the pan counts must follow.
    # A skipped (fresh) fetch and a failure still reload nothing, silently.
    assert out["reloads"] == 2 and out["toasts"] == 0


def test_old_ownership_refresh_cannot_replace_another_detail(js):
    ensure = _extract(js, r"ensureRe0Fresh:\s*function\s*\(media\)\s*\{([\s\S]*?)\n    \},")
    reload = _extract(js, r"reloadRe0:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    harness = """
var finishPost, finishRead, renders = 0, reads = 0;
var state = { library: { media: 'A' }, detail: { generation: 1, provider: '', media: { re0_candidates: ['A-old'] } } };
var api = { request: function () { return new Promise(function (resolve) { finishPost = resolve; }); } };
function detailRequestUrl(ref) { return ref; }
var views = { library: { fetchChannel: function () { reads++; return new Promise(function (resolve) { finishRead = resolve; }); } },
  detail: { syncProviderTabs: function () {}, renderGroupList: function () { renders++; } } };
views.detail.ensureRe0Fresh = function (media) {""" + ensure + """};
views.detail.reloadRe0 = function () {""" + reload + """};
var oldPost = views.detail.ensureRe0Fresh({media_type:'tv',tmdb_id:111});
state.library.media = 'B'; state.detail.generation = 2;
finishPost({fetched:true});
oldPost.then(function () {
  if (reads !== 0) throw new Error('old POST reloaded the new detail');
  state.library.media = 'A'; state.detail.generation = 3;
  var oldRead = views.detail.reloadRe0();
  state.library.media = 'B'; state.detail.generation = 4;
  state.detail.media = {re0_candidates:['B-current']};
  finishRead({re0_candidates:['A-late']});
  return oldRead;
}).then(function () {
  console.log(JSON.stringify({candidates:state.detail.media.re0_candidates,renders:renders}));
});
"""
    assert json.loads(_run_node(harness)) == {"candidates": ["B-current"], "renders": 0}


def test_re0_action_confirms_posts_and_dispatches(js):
    body = _extract(js, r"re0Action:\s*function\s*\(candidate, btn\)\s*\{([\s\S]*?)\n    \},")
    assert "window.confirm(" in body
    assert '"/api/library/re0-resource/" + encodeURIComponent(candidate.id) + "/unlock-and-action"' in body
    assert "request_id" in body and 'method: "POST"' in body
    for needle in ("views.transfer.openLibrary(", 'views.detail.reveal(', "views.transfer.openCloud("):
        assert needle in body, needle
    assert "candidate.resource_link_id" in body  # already materialised -> no unlock request
    assert "retry_after" in body


def test_search_starts_remote_alongside_local_and_renders_a_unified_catalog(js):
    render = _extract(js, r"renderResults:\s*function\s*\(d, provisional\)\s*\{([\s\S]*?)\n    \},")
    assert "views.library.loadRemoteLane(" not in render
    lane = _extract(js, r"runSearch:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    assert '!!state.library.q.trim() && state.library.type !== "unknown"' in lane
    assert '"/api/library/search/re0?"' in lane
    assert 'fetchChannel("remote"' in lane
    assert "&catalog=1" in lane and "views.library.renderResults(d.catalog)" in lane
    results_mode = _extract(js, r"showBrowseMode:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    assert '$("libraryRemote").hidden = true' in results_mode


def test_open_detail_accepts_re0_ref_and_promotes_after_unlock(js):
    open_fn = _extract(js, r"\n    open:\s*function\s*\(mediaId\)\s*\{([\s\S]*?)\n    \},")
    assert "detailRequestUrl(mediaId, provider)" in open_fn
    action = _extract(js, r"re0Action:\s*function\s*\(candidate, btn\)\s*\{([\s\S]*?)\n    \},")
    assert "d.media_id" in action and "views.library.openDetail(" in action


def test_sync_card_status_line(js):
    fn = _extract(js, r"function re0SyncSummary\(d\) \{([\s\S]*?)\n  \}")
    harness = "function re0SyncSummary(d) {" + fn + "}\n"
    d = {"configured": True, "authorized": True, "budget": {"used_today": 3, "daily_cap": 100, "cooldown_until": None},
         "projections": {"pending": 1, "partial": 2, "complete": 5, "retryable": 0, "failed": 0}, "resources": {"candidate": 4, "unlocked": 1},
         "actions_today": 1, "last_error_class": None, "direct_search": False}
    out = _run_node(harness + "process.stdout.write(re0SyncSummary(" + json.dumps(d, ensure_ascii=False) + "));")
    assert out == "已授权 · 今日请求 3/100 · 投影 8（待补全 3） · 候选 4 · 已解锁 1 · 今日动作 1 · 模式：TMDB→RE0"
    d2 = dict(d, authorized=False, budget={"used_today": 0, "daily_cap": 100, "cooldown_until": 1893456000}, last_error_class="rate_limited")
    out2 = _run_node(harness + "process.stdout.write(re0SyncSummary(" + json.dumps(d2, ensure_ascii=False) + "));")
    assert out2.startswith("未授权 · 今日请求 0/100 · 冷却中") and "最近错误：rate_limited" in out2
    assert _run_node(harness + "process.stdout.write(re0SyncSummary(null));") == "无法读取 RE0 同步状态"


def test_sync_card_has_run_small_button(page, js):
    assert 'id="re0RunSmall"' in page and "只读探测" in page
    handler = _extract(js, r'\$\("re0RunSmall"\)\.onclick = function \(\) \{([\s\S]*?)\n      \};')
    assert '"/api/library/re0/run-small"' in handler and 'method: "POST"' in handler and "refreshRe0SyncStatus()" in handler


def test_sync_card_can_edit_cap_and_interval(page, js):
    for element_id in ("re0DailyCap", "re0MinInterval", "re0SyncSave"):
        assert f'id="{element_id}"' in page, element_id
    payload_fn = _extract(js, r"function re0SettingsPayload\(\) \{([\s\S]*?)\n  \}")
    harness = """
var elements = {};
function $(id) { return elements[id] || (elements[id] = { value: "" }); }
function re0SettingsPayload() {""" + payload_fn + """}
$("re0DailyCap").value = "50"; $("re0MinInterval").value = "abc";
console.log(JSON.stringify(re0SettingsPayload()));
"""
    assert json.loads(_run_node(harness)) == {"re0_daily_request_cap": 50, "re0_min_interval_ms": 1000}
    handler = _extract(js, r'\$\("re0SyncSave"\)\.onclick = function \(\) \{([\s\S]*?)\n      \};')
    assert "JSON.stringify(re0SettingsPayload())" in handler and "refreshRe0SyncStatus()" in handler


def test_detail_has_no_separate_re0_module_and_keeps_the_refresh_button(js):
    render = _extract(js, r"\n    render:\s*function\s*\(media, scopedFetch\)\s*\{([\s\S]*?)\n    \},")
    assert 'id="detailRe0"' not in render and "RE0 新资源" not in render and "renderRe0List" not in render
    # Round 37: the refresh button is gone from the UI; the function stays.
    assert "re0ToolbarHtml" not in js and 'id="re0Refresh"' not in js
    refresh = _extract(js, r"re0Refresh:\s*function\s*\(btn\)\s*\{([\s\S]*?)\n    \},")
    assert '"/api/library/re0/refresh"' in refresh and "tmdb_id" in refresh and "selectProvider(" in refresh
    reload = _extract(js, r"reloadRe0:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    assert "views.detail.renderGroupList()" in reload


def test_calendar_cta_html_and_placement(js):
    fn = _extract(js, r"function calendarCtaHtml\(event\) \{([\s\S]*?)\n  \}")
    harness = ESC + "function calendarCtaHtml(event) {" + fn + "}\n"
    html = _run_node(harness + 'process.stdout.write(calendarCtaHtml({ label: "下一集 S06E01", display: "9月16日周三 11:00", episode_title: "新<集>", season: 6, episode: 1, is_new_season: false }));')
    assert 'class="calendar-cta' in html and "下一集 S06E01" in html and "9月16日周三 11:00" in html and "新&lt;集&gt;" in html
    assert 'data-season="6"' in html and "<a " not in html  # no unverified external link
    assert _run_node(harness + "process.stdout.write(calendarCtaHtml(null));") == ""
    render = _extract(js, r"render:\s*function\s*\(media, scopedFetch\)\s*\{([\s\S]*?)\n    \},")
    assert "calendarCtaHtml(media.re0_calendar)" in render
    assert render.index("ratingsRowHtml(media)") < render.index("calendarCtaHtml(media.re0_calendar)") < render.index("detail-overview")


def test_follow_section_rows_and_unlock_confirm(js):
    row_fn = _extract(js, r"followRowHtml:\s*function\s*\(p\)\s*\{([\s\S]*?)\n    \},")
    harness = ESC + "var views = { detail: {} };\nviews.detail.followRowHtml = function (p) {" + row_fn + "};\n"
    locked = {"ref": "abc123", "title": "追更包<甲>", "latest_label": "S02E10", "is_completed": False, "item_count": 2, "unlock_points": 30, "is_unlocked": False, "unlocked_items": 0}
    html = _run_node(harness + "process.stdout.write(views.detail.followRowHtml(" + json.dumps(locked, ensure_ascii=False) + "));")
    assert "追更包&lt;甲&gt;" in html and "S02E10" in html and "需 30 积分" in html and 'data-follow-ref="abc123"' in html and "解锁追更包" in html
    done = dict(locked, is_unlocked=True, unlocked_items=2)
    html2 = _run_node(harness + "process.stdout.write(views.detail.followRowHtml(" + json.dumps(done, ensure_ascii=False) + "));")
    assert "已解锁" in html2 and "解锁追更包" not in html2
    action = _extract(js, r"followUnlock:\s*function\s*\(pack, btn\)\s*\{([\s\S]*?)\n    \},")
    assert "window.confirm(" in action and '"/api/library/re0-follow/" + encodeURIComponent(pack.ref) + "/unlock"' in action and "request_id" in action
    assert "subscribe_updates" in action


def test_home_has_a_remote_discoveries_rail_rendered_with_remote_cards(js):
    rails = _extract(js, r"var LIBRARY_RAILS = (\[[\s\S]*?\]);")
    assert '"/api/library/re0/discoveries?limit=12"' in rails and "RE0 新发现" in rails and "remote: true" in rails
    load = _extract(js, r"loadRails:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    assert "rail.remote" in load and "remoteCardHtml" in load


# Round 23: local exact TMDB id first -- status words for the local fast
# path and the partial (remote source failed, local matches kept) answer.
@pytest.mark.parametrize("remote,expected", [
    ({"status": "fresh", "origin": "local", "items": [1]}, "本地已匹配 TMDB ID · RE0 找到 1 个媒体"),
    ({"status": "cached", "origin": "local", "items": [1, 2]}, "本地已匹配 TMDB ID · RE0 找到 2 个媒体（缓存）"),
    ({"status": "fresh", "origin": "local", "items": []}, "本地已匹配 TMDB ID · RE0 没有对应资源"),
    ({"status": "fresh", "origin": "tmdb", "items": [1]}, "RE0 找到 1 个媒体"),
    ({"status": "partial", "reason": "tmdb_budget_exhausted", "origin": "tmdb", "items": [1]}, "RE0 找到 1 个媒体（部分结果，TMDB 额度已用完）"),
    ({"status": "partial", "reason": "upstream_4xx", "origin": "local", "items": []}, "本地已匹配 TMDB ID · RE0 没有对应资源（部分结果，RE0 拒绝了请求）"),
    ({"status": "partial", "items": [1]}, "RE0 找到 1 个媒体（部分结果，远端搜索失败）"),
])
def test_remote_status_label_local_and_partial(js, remote, expected):
    fn = _extract(js, r"function remoteStatusLabel\(d\) \{([\s\S]*?)\n  \}")
    out = _run_node("function remoteStatusLabel(d) {" + fn + "}\nprocess.stdout.write(remoteStatusLabel(" + json.dumps(remote, ensure_ascii=False) + "));")
    assert out == expected


def test_remote_lane_forwards_the_provider_filter_to_the_server(js):
    lane = _extract(js, r"runSearch:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    harness = """
var els = {};
function $(id) { return els[id] || (els[id] = { hidden: false, textContent: "", innerHTML: "" }); }
var state = { library: { q: "末日地堡", type: "tv", year: "", provider: ["115", "quark"] } };
var captured = [];
var views = { library: {
  fetchChannel: function (name, url) { captured.push(url); return Promise.resolve(null); },
  renderSkeleton: function () {},
  syncPageSize: function () { state.library.pageSize = 35; },
  buildQuery: function () { return 'q=fixture&type=tv&provider=115%2Cquark&quality=4k&page=2&page_size=' + state.library.pageSize; }
} };
function remoteStatusLabel() { return ""; }
views.library.runSearch = function () {""" + lane + """};
views.library.runSearch().then(function () { process.stdout.write(JSON.stringify(captured)); });
"""
    urls = json.loads(_run_node(harness))
    assert len(urls) == 2 and urls[1].startswith("/api/library/search/re0?")
    assert "provider=115%2Cquark" in urls[1] and "type=tv" in urls[1] and "page=2" in urls[1] and "catalog=1" in urls[1]
    assert all("page_size=35" in url for url in urls)
    # Provider isolation is server-side: the lane never receives resources
    # from other pans, so the cards carry no provider list to leak them.
    card = _extract(js, r"remoteCardHtml:\s*function\s*\(item\)\s*\{([\s\S]*?)\n    \},")
    assert "item.providers" not in card


# ---------------------------------------------------------------------------
# Composition work order §6: a candidate row must show enough to decide which
# resource to save -- publisher, date, remark, composition, subtitles -- and
# a file preview that is its own action, never confused with unlocking.
# ---------------------------------------------------------------------------


def _full_cand(**over):
    base = {
        "id": 592, "provider": "115", "provider_label": "115 网盘", "state": "candidate", "unlock_points": 4,
        "action": "transfer", "title": "末日地堡 (2023)", "size": "79.52GB", "resource_link_id": None,
        "specs": {"resolution": {"value": "4K", "icon": "icon-resolution"}, "source": {"value": "WEB-DL", "icon": "icon-source"}},
        "subtitle_language": ["简中"], "subtitle_type": ["内封"], "source_status": "valid",
        "remark": "4K高码，24集首更完结", "published_at": "2025-04-29T16:00:00+00:00",
        "publisher": {"nickname": "C", "avatar_url": None}, "is_official": False, "unlocked_users_count": 3,
        "composition": {"kind": "season_complete", "display": "S03 · 24集", "confidence": "declared", "completion": "complete"},
        "file_preview": {"available": True, "status": "not_loaded", "file_count": None, "fetched_at": None},
    }
    base.update(over)
    return base


def _render_row(js, cand):
    return _run_node(_re0_row_harness(js) + "process.stdout.write(views.detail.re0RowHtml(" + json.dumps(cand, ensure_ascii=False) + "));")


def test_candidate_row_shows_everything_needed_to_choose(js):
    html = _render_row(js, _full_cand())
    # Publisher and date, secondary; remark verbatim; composition visible, not a tooltip.
    assert "C" in html and "发布于 2025-04-29" in html
    assert 'class="re0-remark"' in html and "4K高码，24集首更完结" in html
    assert 'class="re0-composition"' in html and "S03 · 24集" in html
    # Subtitles are visible chips, not only a title attribute.
    assert 'class="re0-sub"' in html and "简中" in html and "内封" in html
    assert "4K" in html and "WEB-DL" in html and "79.52GB" in html
    assert "需 4 积分" in html and ">解锁并转存<" in html
    # The preview is its own action and never says 展开.
    assert 'data-re0-preview="592"' in html and "文件预览" in html and "展开" not in html
    assert "末日地堡 (2023)" in html


def test_row_says_when_the_publisher_left_the_composition_unstated(js):
    html = _render_row(js, _full_cand(remark=None, publisher=None, published_at=None,
                                      composition={"kind": "unknown", "display": "构成未说明", "confidence": "unknown", "completion": "unknown"}))
    assert "构成未说明" in html and "RE0 分享者未提供" in html
    assert 'class="re0-remark"' not in html   # no empty remark block
    assert "发布于" not in html


def test_row_escapes_publisher_remark_and_title(js):
    html = _render_row(js, _full_cand(remark="<img src=x onerror=alert(1)>", title="<b>t</b>",
                                      publisher={"nickname": "<script>", "avatar_url": None}))
    for raw in ("<img src=x", "<b>t</b>", "<script>"):
        assert raw not in html, raw
    assert "&lt;img src=x" in html and "&lt;script&gt;" in html


def test_row_carries_the_full_remark_for_assistive_tech(js):
    long_remark = "第一部分说明，" * 40
    html = _render_row(js, _full_cand(remark=long_remark))
    assert 'aria-label="' in html and long_remark[:20] in html


def test_preview_button_reflects_a_cached_or_unavailable_preview(js):
    ready = _render_row(js, _full_cand(file_preview={"available": True, "status": "ready", "file_count": 10, "fetched_at": "x"}))
    assert "文件预览（10 个文件）" in ready and 'data-re0-preview="592"' in ready
    unsupported = _render_row(js, _full_cand(file_preview={"available": False, "status": "unsupported", "file_count": None, "fetched_at": None}))
    assert "该来源暂不支持文件预览" in unsupported and 'data-re0-preview=' not in unsupported
    forbidden = _render_row(js, _full_cand(file_preview={"available": False, "status": "forbidden", "file_count": None, "fetched_at": None}))
    assert "账号等级" in forbidden
    # A failed preview never disables the unlock button.
    assert ">解锁并转存<" in unsupported and ">解锁并转存<" in forbidden


def test_a_row_never_carries_a_slug_url_or_access_code(js):
    html = _render_row(js, _full_cand())
    for needle in ("slug", "http://", "https://", "access_code", "ed2k://"):
        assert needle not in html, needle


def test_preview_click_posts_to_the_candidate_route_and_renders_files(js):
    body = _extract(js, r"re0Preview:\s*function\s*\(candidate, btn\)\s*\{([\s\S]*?)\n    \},")
    assert '"/api/library/re0/candidates/" + encodeURIComponent(candidate.id) + "/file-preview"' in body
    assert "btn.disabled = true" in body and "正在读取文件构成" in body
    assert "unlock" not in body   # the preview never touches the unlock route
    binder = _extract(js, r"bindRe0Actions:\s*function\s*\(root\)\s*\{([\s\S]*?)\n    \},")
    assert "data-re0-preview" in binder and "views.detail.re0Preview(" in binder


def test_preview_modal_lists_names_paths_and_sizes(js):
    fn = _extract(js, r"filePreviewHtml:\s*function\s*\(preview, showComposition\)\s*\{([\s\S]*?)\n    \},")
    harness = ESC + """
function re0SizeLabel(v) { return v == null ? "" : String(v); }
var views = { detail: {} };
views.detail.filePreviewHtml = function (preview, showComposition) {""" + fn + """};
var ready = views.detail.filePreviewHtml({ status: "ready", file_count: 2, truncated: false, composition: { display: "S03 · 第 1–2 集" },
  files: [{ name: "S03E01.mkv", path: "/Silo/E01.mkv", size: 1073741824 }, { name: "<b>x</b>.mkv", path: "/x", size: null }] });
var invalid = views.detail.filePreviewHtml({ status: "invalid", file_count: 0, files: [], validate_message: "分享已失效" });
var denied = views.detail.filePreviewHtml({ status: "forbidden", files: [], message: "当前账号等级不支持文件预览" });
console.log(JSON.stringify({ ready: ready, invalid: invalid, denied: denied }));
"""
    out = json.loads(_run_node(harness))
    assert "S03E01.mkv" in out["ready"] and "/Silo/E01.mkv" in out["ready"] and "S03 · 第 1–2 集" in out["ready"]
    assert "&lt;b&gt;x&lt;/b&gt;.mkv" in out["ready"] and "<b>x</b>.mkv" not in out["ready"]
    assert "分享已失效" in out["invalid"]
    assert "当前账号等级不支持文件预览" in out["denied"] and "以 RE0 备注为准" in out["denied"]


# ---------------------------------------------------------------------------
# Invalid-candidate work order §4: a dead RE0 share never offers an unlock
# button, a checking one is not painted red, a protocol link has no preview
# control, and the hidden ones are announced with a way to see them.
# ---------------------------------------------------------------------------


def _inv_cand(**over):
    base = dict(_full_cand())
    base.update({"effective_status": "valid", "effective_status_label": "RE0 校验有效",
                 "effective_status_reason": None, "effective_checked_at": None, "has_local_link": False})
    base.update(over)
    return base


def test_a_dead_candidate_shows_no_unlock_button(js):
    html = _render_row(js, _inv_cand(effective_status="invalid", effective_status_label="RE0 已失效",
                                     effective_status_reason="链接状态异常，需人工复核",
                                     effective_checked_at="2026-09-09T16:00:00+00:00"))
    assert "RE0 已失效" in html and 're0-state invalid' in html
    assert "链接状态异常，需人工复核" in html and "2026-09-09" in html
    for label in ("解锁并转存", "解锁并复制链接", "解锁并开启云下载"):
        assert label not in html, label
    assert "data-re0-action" not in html


def test_a_dead_candidate_that_already_has_a_local_link_keeps_its_action(js):
    # §4.3: "RE0 来源已失效" is not "the local link is dead".
    html = _render_row(js, _inv_cand(effective_status="invalid", effective_status_label="RE0 已失效",
                                     has_local_link=True, resource_link_id="pub-x", state="unlocked"))
    assert "RE0 已失效" in html and ">转存到 115<" in html and 'data-link-id="pub-x"' in html
    assert "解锁并转存" not in html


def test_checking_is_not_painted_as_dead(js):
    html = _render_row(js, _inv_cand(effective_status="checking", effective_status_label="RE0 校验中"))
    assert "RE0 校验中" in html and "已失效" not in html
    assert 're0-state invalid' not in html
    assert ">解锁并转存<" in html   # still actionable


@pytest.mark.parametrize("status", ["valid", "unchecked", "unknown", "preview_unavailable"])
def test_every_non_dead_status_keeps_the_unlock_button(js, status):
    html = _render_row(js, _inv_cand(effective_status=status))
    assert ">解锁并转存<" in html and "已失效" not in html


def test_a_protocol_link_has_no_preview_control_but_keeps_its_action(js):
    html = _render_row(js, _inv_cand(provider="ed2k", provider_label="ED2K", action="cloud",
                                     file_preview={"available": False, "status": "not_applicable",
                                                   "file_count": None, "fetched_at": None}))
    assert "文件预览" not in html and "data-re0-preview" not in html
    # No misleading "this source cannot preview" note either -- there is simply nothing to preview.
    assert "不支持文件预览" not in html
    assert ">解锁并开启云下载<" in html


def test_the_audit_toggle_refetches_the_detail_and_touches_nothing_else(js):
    body = _extract(js, r"toggleInvalid:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    assert "state.detail.includeInvalid" in body and "reloadRe0()" in body
    assert "state.library.provider" not in body and "state.detail.panFilter" not in body
    url = _extract(js, r"function detailRequestUrl\(mediaId, provider\) \{([\s\S]*?)\n  \}")
    assert "include_invalid=1" in url and "state.detail.includeInvalid" in url


# ---------------------------------------------------------------------------
# Movie work order: a film's own remark already says what the release is, so a
# movie candidate drops the composition chip entirely. A series keeps every
# bit of it -- that is how you tell a whole season from three episodes.
# ---------------------------------------------------------------------------


def _render_row_for(js, media_type, cand):
    harness = 'var MEDIA_TYPE = ' + json.dumps(media_type) + ';\n' + _re0_row_harness(js)
    return _run_node(harness + "process.stdout.write(views.detail.re0RowHtml(" + json.dumps(cand, ensure_ascii=False) + "));")


def _comp_cand(**over):
    base = dict(_full_cand())
    base.update({"effective_status": "valid", "effective_status_label": "RE0 校验有效",
                 "effective_status_reason": None, "effective_checked_at": None, "has_local_link": False})
    base.update(over)
    return base


def test_a_movie_candidate_drops_the_composition_chip(js):
    html = _render_row_for(js, "movie", _comp_cand(
        remark="4K HDR 内封简繁 HiveWeb自购",
        composition={"kind": "unknown", "display": "构成未说明", "confidence": "unknown", "completion": "unknown"}))
    assert "构成未说明" not in html and 're0-composition' not in html
    for phrase in ("据文件名推断", "据发布者备注", "已完结", "更新中", "部分集数"):
        assert phrase not in html, phrase
    # Everything else the card carries stays.
    assert "4K HDR 内封简繁 HiveWeb自购" in html and "4K" in html and "WEB-DL" in html
    assert "简中" in html and "内封" in html and "79.52GB" in html
    assert "需 4 积分" in html and ">解锁并转存<" in html and "文件预览" in html
    assert 'class="re0-chips"' in html   # the row keeps its chip strip, just without that one


def test_a_movie_without_a_remark_invents_nothing(js):
    html = _render_row_for(js, "movie", _comp_cand(remark=None, composition=None))
    assert "构成未说明" not in html and "re0-composition" not in html
    assert 're0-remark' not in html          # no fabricated placeholder
    assert 'class="re0-chips"' in html and "4K" in html


def test_a_series_candidate_keeps_every_bit_of_the_composition(js):
    html = _render_row_for(js, "tv", _comp_cand(
        composition={"kind": "season_complete", "display": "S03 · 24集", "confidence": "declared", "completion": "complete"}))
    assert 'class="re0-composition"' in html and "S03 · 24集" in html
    assert "已完结" in html and "据发布者备注" in html
    unstated = _render_row_for(js, "tv", _comp_cand(composition=None))
    assert "构成未说明" in unstated


@pytest.mark.parametrize("media_type", [None, "", "unknown", "anime"])
def test_an_unknown_media_type_keeps_the_composition(js, media_type):
    # §3.3: only an explicit "movie" hides it -- never guess information away.
    html = _render_row_for(js, media_type, _comp_cand())
    assert 're0-composition' in html


def test_the_helper_only_hides_for_an_explicit_movie(js):
    body = _extract(js, r"function showRe0Composition\(mediaType\) \{([\s\S]*?)\n  \}")
    out = json.loads(_run_node("function showRe0Composition(mediaType) {" + body + """}
console.log(JSON.stringify(["movie", "tv", "", null, undefined, "unknown"].map(showRe0Composition)));
"""))
    assert out == [False, True, True, True, True, True]


def _preview_html_harness(js: str) -> str:
    fn = _extract(js, r"filePreviewHtml:\s*function\s*\(preview, showComposition\)\s*\{([\s\S]*?)\n    \},")
    return ESC + """
function re0SizeLabel(v) { return v == null ? "" : String(v); }
var views = { detail: {} };
views.detail.filePreviewHtml = function (preview, showComposition) {""" + fn + """};
"""


def test_a_movie_preview_head_carries_no_composition_suffix(js):
    out = json.loads(_run_node(_preview_html_harness(js) + """
var ready = { status: "ready", file_count: 1, truncated: false, composition: { display: "S01 · 第 1 集" },
              files: [{ name: "Movie.2160p.mkv", path: "/m/Movie.mkv", size: 9 }] };
var denied = { status: "forbidden", files: [], message: "当前账号等级不支持文件预览" };
console.log(JSON.stringify({
  movieReady: views.detail.filePreviewHtml(ready, false),
  seriesReady: views.detail.filePreviewHtml(ready, true),
  movieDenied: views.detail.filePreviewHtml(denied, false),
  seriesDenied: views.detail.filePreviewHtml(denied, true),
  defaultReady: views.detail.filePreviewHtml(ready)
}));
"""))
    # Movie: the count and the files, nothing about composition.
    assert "共 1 个文件" in out["movieReady"] and "S01 · 第 1 集" not in out["movieReady"]
    assert "Movie.2160p.mkv" in out["movieReady"] and "/m/Movie.mkv" in out["movieReady"]
    assert "当前账号等级不支持文件预览" in out["movieDenied"] and "以 RE0 备注为准" not in out["movieDenied"]
    # Series: unchanged, word for word.
    assert "共 1 个文件 · S01 · 第 1 集" in out["seriesReady"]
    assert "当前账号等级不支持文件预览 · 以 RE0 备注为准" in out["seriesDenied"]
    # Omitting the flag must never silently strip a series' composition.
    assert out["defaultReady"] == out["seriesReady"]


def test_the_preview_call_and_its_failure_note_follow_the_media_type(js):
    body = _extract(js, r"re0Preview:\s*function\s*\(candidate, btn\)\s*\{([\s\S]*?)\n    \},")
    assert "showRe0Composition(" in body
    assert "filePreviewHtml(preview, " in body
    # A movie must not look for a chip that was never rendered.
    assert 'querySelector(".re0-composition")' in body
    harness = ESC + """
var boxes = {}; var docCalls = [];
function $(id) { return boxes[id] || (boxes[id] = { hidden: true, innerHTML: "", dataset: {} }); }
var api = { request: function () { return Promise.reject(new Error("RE0 限流，45 秒后再试")); } };
var document = { querySelector: function (sel) { docCalls.push(sel); return null; } };
function showRe0Composition(t) { return t !== "movie"; }
function re0CompletionLabel() { return ""; }
var MEDIA = "movie";
var state = { detail: { media: { media_type: MEDIA } } };
var views = { detail: { filePreviewHtml: function () { return "<p>"; } } };
views.detail.re0Preview = function (candidate, btn) {""" + body + """};
views.detail.re0Preview({ id: 5 }, { disabled: false }).then(function () {
  console.log(JSON.stringify({ note: boxes["re0Preview5"].innerHTML, sel: docCalls }));
});
"""
    out = json.loads(_run_node(harness))
    assert "RE0 限流，45 秒后再试" in out["note"]
    assert "构成未确认" not in out["note"] and "以 RE0 备注为准" not in out["note"]
