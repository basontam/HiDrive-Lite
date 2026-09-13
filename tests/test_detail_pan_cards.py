"""Round 25 (2026-09-10): the detail page's resource area is a grid of pan
cards (two per row), each holding that pan's local groups (links already in
the response, 展开/收起 per group, invalid/deleted links hidden) and its RE0
candidates. Node harnesses run the real functions extracted from app.js."""

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
def js():
    return (ROOT / "static" / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def css():
    return (ROOT / "static" / "app.css").read_text(encoding="utf-8")


def _extract(js: str, pattern: str) -> str:
    match = re.search(pattern, js)
    assert match, f"pattern not found: {pattern}"
    return match.group(1)


def _run_node(harness: str) -> str:
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


ESC = 'function esc(s) { return String(s == null ? "" : s).replace(/[&<>"\']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", \'"\': "&quot;", "\'": "&#39;" }[c]; }); }\n'


def _cards_harness(js: str) -> str:
    parts = {
        "renderGroupList": _extract(js, r"renderGroupList:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},"),
        "panCards": _extract(js, r"panCards:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},"),
        "panCardHtml": _extract(js, r"panCardHtml:\s*function\s*\(card\)\s*\{([\s\S]*?)\n    \},"),
        "re0BlockHtml": _extract(js, r"re0BlockHtml:\s*function\s*\(card\)\s*\{([\s\S]*?)\n    \},"),
        "re0RowsFor": _extract(js, r"re0RowsFor:\s*function\s*\(code\)\s*\{([\s\S]*?)\n    \},"),
        "hiddenLinkCount": _extract(js, r"hiddenLinkCount:\s*function\s*\(code\)\s*\{([\s\S]*?)\n    \},"),
        "toggleFold": _extract(js, r"toggleFold:\s*function\s*\(key\)\s*\{([\s\S]*?)\n    \},"),
    }
    visible = _extract(js, r"function visibleLinks\(group, code\) \{([\s\S]*?)\n  \}")
    usable = _extract(js, r"function linkIsUsable\(link\) \{([\s\S]*?)\n  \}")
    fold_open = _extract(js, r"function groupFoldOpen\(key\) \{([\s\S]*?)\n  \}")
    fold_button = _extract(js, r"function foldButtonHtml\(key, open\) \{([\s\S]*?)\n  \}")
    pan_count = _extract(js, r"function panLinkCount\(media, code\) \{([\s\S]*?)\n  \}")
    unstored = _extract(js, r"function re0UnstoredCandidates\(media, code\) \{([\s\S]*?)\n  \}")
    resource_count = _extract(js, r"function panResourceCount\(media, code\) \{([\s\S]*?)\n  \}")
    facets_fn = _extract(js, r"function visibleFacets\(media\) \{([\s\S]*?)\n  \}")
    return ESC + """
var ICONS_URL = "/static/icons.svg?v=test";
var PROVIDER_META_BY_CODE = { "115": { symbol: "provider-115" }, quark: { symbol: "provider-quark" } };
function providerLabel(code) { return { "115": "115 网盘", quark: "夸克网盘", tianyicloud: "天翼云盘", ed2k: "ED2K" }[code] || code; }
var elements = {};
function $(id) { return elements[id] || (elements[id] = { innerHTML: "", hidden: false, onclick: null, querySelectorAll: function () { return []; } }); }
function linkIsUsable(link) {""" + usable + """}
function visibleLinks(group, code) {""" + visible + """}
function groupFoldOpen(key) {""" + fold_open + """}
function panLinkCount(media, code) {""" + pan_count + """}
function re0UnstoredCandidates(media, code) {""" + unstored + """}
function panResourceCount(media, code) {""" + resource_count + """}
function visibleFacets(media) {""" + facets_fn + """}
function foldButtonHtml(key, open) {""" + fold_button + """}
var state = { detail: { provider: "", includeDeleted: false, media: {
  tmdb_id: 555,
  provider_facets: [{ provider: "115", label: "115 网盘", link_count: 3, re0_count: 1 }, { provider: "quark", label: "夸克网盘", link_count: 1 }, { provider: "tianyicloud", label: "天翼云盘", link_count: 0, re0_count: 2 }],
  groups: [
    { group_id: 1, display_title: "A 4K", providers: { "115": 2 }, links: [
      { link_id: "a1", provider: "115", label: "a1" }, { link_id: "a2", provider: "115", label: "a2", invalid: true }, { link_id: "a3", provider: "115", label: "a3", deleted: true } ] },
    { group_id: 2, display_title: "B 1080p", providers: { "115": 1, quark: 1 }, links: [
      { link_id: "b1", provider: "115", label: "b1" }, { link_id: "b2", provider: "quark", label: "b2" } ] },
    { group_id: 3, display_title: "C dead", providers: { "115": 0 }, links: [ { link_id: "c1", provider: "115", label: "c1", invalid: true } ] }
  ],
  re0_candidates: [ { id: 11, provider: "115", provider_label: "115 网盘", state: "candidate" },
                    { id: 12, provider: "tianyicloud", provider_label: "天翼云盘", state: "candidate" }, { id: 13, provider: "tianyicloud", provider_label: "天翼云盘", state: "candidate" } ]
} } };
var bound = [];
var views = { detail: {
  groupRowHtml: function (g, code) { var open = groupFoldOpen("g:" + code + ":" + g.group_id); return "<group " + g.group_id + ":" + code + ":" + (open ? "open" : "folded") + ">"; },
  re0RowHtml: function (c) { return "<row " + c.id + ">"; },
  bindLinkActions: function () { bound.push("links"); }, bindRecheckButtons: function () {}, bindCloudGroupButtons: function () {},
  bindRe0Actions: function () { bound.push("re0"); }, bindGroupFolds: function () { bound.push("folds"); }, re0Refresh: function () {}
} };
""" + "".join('views.detail.%s = function (%s) {%s};\n' % (name, {"renderGroupList": "", "panCards": "", "panCardHtml": "card", "re0BlockHtml": "card", "re0RowsFor": "code", "hiddenLinkCount": "code", "toggleFold": "key"}[name], body) for name, body in parts.items())


def test_resource_area_is_one_card_per_pan_with_only_visible_links(js):
    out = json.loads(_run_node(_cards_harness(js) + """
views.detail.renderGroupList();
var all = $("detailGroupList").innerHTML;
state.detail.panFilter = "tianyicloud";
views.detail.renderGroupList();
var tianyi = $("detailGroupList").innerHTML;
state.detail.panFilter = "";
state.detail.media.groups = []; state.detail.media.re0_candidates = []; state.detail.media.provider_facets = [];
views.detail.renderGroupList();
var nothing = $("detailGroupList").innerHTML;
state.detail.media.tmdb_id = null;
views.detail.renderGroupList();
var noId = $("detailGroupList").innerHTML;
console.log(JSON.stringify({ all: all, tianyi: tianyi, nothing: nothing, noId: noId, bound: bound }));
"""))
    html = out["all"]
    # One card per pan, facet order; the grid is what the CSS lays out two per row.
    assert 'class="pan-grid"' in html and html.count('data-pan="') == 3
    assert html.index('data-pan="115"') < html.index('data-pan="quark"') < html.index('data-pan="tianyicloud"')
    # 115: groups 1 and 2 (group 3 has only an invalid link -> not shown), then its RE0 block; the header counts only live+valid links.
    card_115 = html[html.index('data-pan="115"'):html.index('data-pan="quark"')]
    assert "<group 1:115:" in card_115 and "<group 2:115:" in card_115 and "<group 3:" not in card_115
    assert "2 条链接" in card_115 and "1 条 RE0 候选" in card_115 and "<row 11>" in card_115 and "#provider-115" in card_115
    # quark: only group 2's quark side, no RE0 block; tianyicloud: RE0 only, no local group.
    card_quark = html[html.index('data-pan="quark"'):html.index('data-pan="tianyicloud"')]
    assert "<group 2:quark:" in card_quark and "<group 1:" not in card_quark and "detail-group-re0" not in card_quark and "1 条链接" in card_quark
    card_tianyi = html[html.index('data-pan="tianyicloud"'):]
    assert "<group" not in card_tianyi and "<row 12><row 13>" in card_tianyi and "2 条 RE0 候选" in card_tianyi and "需解锁" not in card_tianyi
    # A pan whose links are all dead (audit view only) says so instead of an empty header.
    dead = json.loads(_run_node(_cards_harness(js) + """
state.detail.includeDeleted = true;
state.detail.media.groups = [{ group_id: 9, links: [{ provider: "quark", invalid: true }] }];
state.detail.media.re0_candidates = [];
state.detail.media.provider_facets = [{ provider: "quark", label: "夸克网盘", link_count: 0 }];
views.detail.renderGroupList();
console.log(JSON.stringify({ html: $("detailGroupList").innerHTML }));
"""))
    assert "全部失效" in dead["html"] and 'data-pan="quark"' in dead["html"]
    assert "RE0 新资源" not in html and "re0-toolbar" not in html
    # Round 32: a selected pan tab narrows the cards to that pan (the tab row
    # itself is built elsewhere, from the response's full facet list).
    assert out["tianyi"].count('data-pan="') == 1 and 'data-pan="tianyicloud"' in out["tianyi"]
    # Nothing to show: the empty text, plus the refresh toolbar while the media has a TMDB id.
    assert "暂无资源" in out["nothing"] and "pan-card" not in out["nothing"]
    assert "暂无资源" in out["noId"] and "re0Refresh" not in out["noId"]
    assert set(out["bound"]) >= {"links", "re0", "folds"}


def test_groups_fold_by_default_when_a_card_has_several_and_toggle_re_renders(js):
    out = json.loads(_run_node(_cards_harness(js) + """
views.detail.renderGroupList();
var first = $("detailGroupList").innerHTML;
views.detail.toggleFold("g:115:1");
var afterOpen = $("detailGroupList").innerHTML;
views.detail.toggleFold("re0:tianyicloud");
var re0Folded = $("detailGroupList").innerHTML;
console.log(JSON.stringify({ first: first, afterOpen: afterOpen, re0Folded: re0Folded }));
"""))
    first = out["first"]
    # 115 holds two groups -> both start folded; quark holds one -> open; RE0 blocks start open.
    assert "<group 1:115:folded>" in first and "<group 2:115:folded>" in first and "<group 2:quark:open>" in first
    assert first.count("<row 12><row 13>") == 1 and 'data-fold="re0:tianyicloud"' in first and 'aria-expanded="true"' in first
    assert "<group 1:115:open>" in out["afterOpen"] and "<group 2:115:folded>" in out["afterOpen"]
    assert "<row 12>" not in out["re0Folded"] and 'aria-expanded="false"' in out["re0Folded"] and 'class="group-count">2 条</span>' in out["re0Folded"]


def _group_row_harness(js: str) -> str:
    group_row = _extract(js, r"groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},")
    visible = _extract(js, r"function visibleLinks\(group, code\) \{([\s\S]*?)\n  \}")
    usable = _extract(js, r"function linkIsUsable\(link\) \{([\s\S]*?)\n  \}")
    fold_open = _extract(js, r"function groupFoldOpen\(key\) \{([\s\S]*?)\n  \}")
    fold_button = _extract(js, r"function foldButtonHtml\(key, open\) \{([\s\S]*?)\n  \}")
    return ESC + """
var ICONS_URL = "/static/icons.svg?v=test";
var REVIEW_REASON_LABEL = {};
var state = { cloudEnabled: false, detail: { includeDeleted: false } };
function linkIsUsable(link) {""" + usable + """}
function visibleLinks(group, code) {""" + visible + """}
function groupFoldOpen(key) {""" + fold_open + """}
function groupLiveLinkCount(group) { return 0; }
function foldButtonHtml(key, open) {""" + fold_button + """}
var views = { detail: { seasonLabel: function () { return ""; }, specRowHtml: function () { return ""; },
  linkRowHtml: function (l) { return '<div class="link-row' + (l.deleted ? " link-row-deleted" : "") + '">' + l.label + "</div>"; } } };
views.detail.groupRowHtml = function (group, code) {""" + group_row + """};
var group = { group_id: 7, display_title: "G", providers: { "115": 2 }, links: [
  { link_id: "ok", provider: "115", label: "ok" }, { link_id: "bad", provider: "115", label: "bad", invalid: true },
  { link_id: "gone", provider: "115", label: "gone", deleted: true }, { link_id: "q", provider: "quark", label: "q" } ] };
"""


def test_group_row_hides_invalid_and_deleted_links_unless_include_deleted(js):
    out = json.loads(_run_node(_group_row_harness(js) + """
var plain = views.detail.groupRowHtml(group, "115");
state.detail.includeDeleted = true;
var audit = views.detail.groupRowHtml(group, "115");
state.detail.includeDeleted = false;
var allPans = views.detail.groupRowHtml(group, "");
console.log(JSON.stringify({ plain: plain, audit: audit, allPans: allPans }));
"""))
    plain = out["plain"]
    assert ">ok<" in plain and ">bad<" not in plain and ">gone<" not in plain and ">q<" not in plain and "1 条链接" in plain
    audit = out["audit"]
    assert ">ok<" in audit and ">bad<" in audit and ">gone<" in audit and "1 条链接" in audit  # rows shown for audit, count stays live+valid
    assert ">ok<" in out["allPans"] and ">q<" in out["allPans"] and "2 条链接" in out["allPans"]
    assert 'data-fold="g:115:7"' in plain  # pan-scoped fold key; open by default outside a card


def test_group_row_folded_keeps_summary_and_drops_link_rows(js):
    out = _run_node(_group_row_harness(js) + """
state.detail.folds = { "g:115:7": false };
process.stdout.write(views.detail.groupRowHtml(group, "115"));
""")
    assert ">ok<" not in out and "link-row" not in out and "1 条链接" in out and 'aria-expanded="false"' in out
    assert "detail-group-folded" in out and "重新检测" in out


def test_fold_control_is_a_caret_icon_with_an_accessible_name(js, css):
    """The 展开/收起 affordance is a caret (倒三角), not words: it points down
    when open and rotates when folded, and carries its label for assistive
    tech only."""
    icons = (ROOT / "static" / "icons.svg").read_text(encoding="utf-8")
    assert 'id="icon-caret"' in icons
    button = _extract(js, r"function foldButtonHtml\(key, open\) \{([\s\S]*?)\n  \}")
    assert "#icon-caret" in button and "fold-caret" in button and 'aria-label="' in button
    for body in (_extract(js, r"groupRowHtml:\s*function\s*\(group, code\)\s*\{([\s\S]*?)\n    \},"),
                 _extract(js, r"re0BlockHtml:\s*function\s*\(card\)\s*\{([\s\S]*?)\n    \},")):
        assert "foldButtonHtml(" in body and ">收起<" not in body and ">展开<" not in body
    rule = re.search(r'\.group-fold-btn\[aria-expanded="false"\] \.fold-caret\{([^}]*)\}', css)
    assert rule and "rotate(" in rule.group(1)


def test_pan_grid_css_two_columns_and_scrolling_card_body(css):
    grid = re.search(r"\.pan-grid\{([^}]*)\}", css)
    assert grid and "repeat(2,minmax(0,1fr))" in grid.group(1)
    assert re.search(r"@media[^{]*max-width[^{]*\{[^}]*\.pan-grid\{grid-template-columns:minmax\(0,1fr\)\}", css)
    card = re.search(r"\.pan-card\{([^}]*)\}", css)
    assert card and "min-height" in card.group(1) and "max-height" in card.group(1)
    min_px = int(re.search(r"min-height:(\d+)px", card.group(1)).group(1))
    max_px = int(re.search(r"max-height:(\d+)px", card.group(1)).group(1))
    assert 180 <= min_px <= 260 and 480 <= max_px <= 640  # tall enough for a usable scrollbar, not a wall
    body = re.search(r"\.pan-card-body\{([^}]*)\}", css)
    assert body and "overflow-y:auto" in body.group(1)
    assert ".pan-grid-single{grid-template-columns:minmax(0,1fr)}" in css


def test_header_tabs_and_cards_all_count_the_same_visible_links(js):
    """Round 25: once invalid/deleted links are hidden, every count on the
    page must be the live+valid rows actually shown -- summary row, pan
    tabs and card headers alike, never the server's invalid-inclusive
    totals."""
    count_fn = _extract(js, r"function panLinkCount\(media, code\) \{([\s\S]*?)\n  \}")
    facets_fn = _extract(js, r"function visibleFacets\(media\) \{([\s\S]*?)\n  \}")
    visible = _extract(js, r"function visibleLinks\(group, code\) \{([\s\S]*?)\n  \}")
    usable = _extract(js, r"function linkIsUsable\(link\) \{([\s\S]*?)\n  \}")
    unstored = _extract(js, r"function re0UnstoredCandidates\(media, code\) \{([\s\S]*?)\n  \}")
    resource_count = _extract(js, r"function panResourceCount\(media, code\) \{([\s\S]*?)\n  \}")
    harness = """
function providerLabel(code) { return { ed2k: "ED2K" }[code] || code; }
var state = { detail: { includeDeleted: false } };
function linkIsUsable(link) {""" + usable + """}
function visibleLinks(group, code) {""" + visible + """}
function panLinkCount(media, code) {""" + count_fn + """}
function re0UnstoredCandidates(media, code) {""" + unstored + """}
function panResourceCount(media, code) {""" + resource_count + """}
function visibleFacets(media) {""" + facets_fn + """}
var media = {
  provider_facets: [{ provider: "115", label: "115 网盘", link_count: 3, re0_count: 1 },
                    { provider: "quark", label: "夸克网盘", link_count: 1 },
                    { provider: "baidu", label: "百度网盘", link_count: 1 }],
  groups: [
    { group_id: 1, links: [ { provider: "115" }, { provider: "115", invalid: true }, { provider: "115", deleted: true } ] },
    { group_id: 2, links: [ { provider: "115" }, { provider: "quark" }, { provider: "ed2k" } ] },
    { group_id: 3, links: [ { provider: "baidu", invalid: true } ] } ],
  re0_candidates: [ { provider: "115" }, { provider: "tianyicloud" }, { provider: "tianyicloud" } ]
};
var plain = visibleFacets(media);
state.detail.includeDeleted = true;
console.log(JSON.stringify({ all: panLinkCount(media, ""), p115: panLinkCount(media, "115"), facets: plain, audit: visibleFacets(media) }));
"""
    out = json.loads(_run_node(harness))
    assert out["all"] == 4 and out["p115"] == 2  # one invalid + one deleted 115 link drop out
    # A tab exists for exactly the pans the cards show: server facets first,
    # then pans found only in the rows (ed2k) or only in RE0 (tianyicloud);
    # a pan whose every link is invalid (baidu) drops out of both.
    # Round 40: each facet also carries `resource_count` -- the links plus the
    # RE0 candidates not already stored as one of them, which is the number
    # the tab beside the pan logo shows.
    assert out["facets"] == [
        {"provider": "115", "label": "115 网盘", "link_count": 2, "re0_count": 1, "resource_count": 3},
        {"provider": "quark", "label": "夸克网盘", "link_count": 1, "re0_count": 0, "resource_count": 1},
        {"provider": "ed2k", "label": "ED2K", "link_count": 1, "re0_count": 0, "resource_count": 1},
        {"provider": "tianyicloud", "label": "tianyicloud", "link_count": 0, "re0_count": 2, "resource_count": 2},
    ]
    # The audit view (包含已失效) keeps that pan, with a live count of 0.
    assert {f["provider"]: f["link_count"] for f in out["audit"]}.get("baidu") == 0
    render = _extract(js, r"\n    render:\s*function\s*\(media, scopedFetch\)\s*\{([\s\S]*?)\n    \},")
    assert "var facets = visibleFacets(media);" in render
    # Round 40: the summary row moved into its own builder so a later RE0
    # refresh can rebuild it -- it still counts the rows the cards list.
    summary = _extract(js, r"summaryRowHtml:\s*function\s*\(media, facetCount\)\s*\{([\s\S]*?)\n    \},")
    assert 'var linkCount = panLinkCount(media, "");' in summary
    assert "views.detail.summaryRowHtml(media, facets.length)" in render
    card = _extract(js, r"panCardHtml:\s*function\s*\(card\)\s*\{([\s\S]*?)\n    \},")
    assert "panLinkCount(state.detail.media, card.code)" in card
    cards = _extract(js, r"panCards:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    assert "visibleFacets(media)" in cards  # cards and tabs come from the same list


# ---------------------------------------------------------------------------
# Round 29: a link the checker has looked at but did NOT confirm valid is
# hidden from the detail page and left out of every count. A link that was
# never actually checked (no status, or the "queued" stub) still shows.
# ---------------------------------------------------------------------------


def _link(link_id, **over):
    base = {"link_id": link_id, "provider": "115", "label": link_id, "deleted": False, "invalid": False, "actions": []}
    base.update(over)
    return base


def test_only_unconfirmed_checked_links_are_hidden(js):
    visible = _extract(js, r"function visibleLinks\(group, code\) \{([\s\S]*?)\n  \}")
    usable = _extract(js, r"function linkIsUsable\(link\) \{([\s\S]*?)\n  \}")
    count = _extract(js, r"function panLinkCount\(media, code\) \{([\s\S]*?)\n  \}")
    links = [
        _link("valid", check_status="valid"),
        _link("unchecked"),                                  # never checked -> stays
        _link("queued", check_status="queued"),              # stub row, never actually checked -> stays
        _link("unknown", check_status="unknown"),            # checked, not confirmed -> hidden
        _link("invalid", check_status="invalid", invalid=True),
        _link("gone", check_status="valid", deleted=True),   # deleted at source -> hidden
    ]
    harness = """
var state = { detail: { includeDeleted: false } };
function linkIsUsable(link) {""" + usable + """}
function linkIsUsable(link) {""" + usable + """}
function visibleLinks(group, code) {""" + visible + """}
function panLinkCount(media, code) {""" + count + """}
var group = { group_id: 1, links: """ + json.dumps(links, ensure_ascii=False) + """ };
var media = { groups: [group] };
var plain = visibleLinks(group, "115").map(function (l) { return l.link_id; });
var counted = panLinkCount(media, "115");
state.detail.includeDeleted = true;
var audit = visibleLinks(group, "115").map(function (l) { return l.link_id; });
console.log(JSON.stringify({ plain: plain, counted: counted, audit: audit, auditCount: panLinkCount(media, "115") }));
"""
    out = json.loads(_run_node(harness))
    assert out["plain"] == ["valid", "unchecked", "queued"]
    assert out["counted"] == 3
    # The audit view still lists every row, but the count stays the usable ones.
    assert out["audit"] == ["valid", "unchecked", "queued", "unknown", "invalid", "gone"]
    assert out["auditCount"] == 3


def test_a_group_whose_links_were_all_checked_and_unconfirmed_leaves_the_card(js):
    out = json.loads(_run_node(_cards_harness(js) + """
state.detail.media.groups = [
  { group_id: 1, providers: { "115": 1 }, links: [{ link_id: "ok", provider: "115", check_status: "valid" }] },
  { group_id: 2, providers: { "115": 1 }, links: [{ link_id: "meh", provider: "115", check_status: "unknown" }] }
];
state.detail.media.re0_candidates = [];
state.detail.media.provider_facets = [{ provider: "115", label: "115 网盘", link_count: 2 }];
views.detail.renderGroupList();
console.log(JSON.stringify({ html: $("detailGroupList").innerHTML }));
"""))
    assert "<group 1:115:" in out["html"] and "<group 2:115:" not in out["html"]
    assert "1 条链接" in out["html"] and "2 条链接" not in out["html"]


def test_an_empty_resource_area_explains_that_links_were_hidden(js):
    """A list card's count comes from the server, which still counts links the
    detail page now hides -- so an otherwise blank resource area says why, and
    points at the 含已失效 view. With usable links present it stays silent."""
    out = json.loads(_run_node(_cards_harness(js) + """
state.detail.media.re0_candidates = [];
state.detail.media.provider_facets = [];
state.detail.media.tmdb_id = null;
state.detail.media.groups = [{ group_id: 1, providers: { "115": 1 }, links: [
  { link_id: "a", provider: "115", check_status: "unknown" }, { link_id: "b", provider: "115", deleted: true } ] }];
views.detail.renderGroupList();
var blank = $("detailGroupList").innerHTML;
state.detail.media.groups[0].links.push({ link_id: "c", provider: "115", check_status: "valid" });
views.detail.renderGroupList();
var withUsable = $("detailGroupList").innerHTML;
state.detail.media.groups = [];
views.detail.renderGroupList();
var nothingAtAll = $("detailGroupList").innerHTML;
console.log(JSON.stringify({ blank: blank, withUsable: withUsable, nothingAtAll: nothingAtAll }));
"""))
    # Round 37: the count stays, the pointer at an off-page control does not.
    assert "2 条链接未通过检测或已失效，已隐藏" in out["blank"] and "含已失效" not in out["blank"]
    assert "已隐藏" not in out["withUsable"]   # something to show -> no explanation needed
    assert "已隐藏" not in out["nothingAtAll"]  # nothing was hidden, the media simply has no links


# ---------------------------------------------------------------------------
# Round 31: a pan tab HIGHLIGHTS its pan instead of hiding every other one.
# Server-side isolation is untouched: a response fetched with ?provider= still
# contains only that pan, so a deep link still shows one card.
# ---------------------------------------------------------------------------


def test_the_tab_row_marks_the_highlighted_pan(js):
    tabs = _extract(js, r"renderProviderTabs:\s*function\s*\(facets, selected, scopedFetch\)\s*\{([\s\S]*?)\n    \},")
    harness = ESC + """
var ICONS_URL = "/static/icons.svg?v=test";
var PROVIDER_META_BY_CODE = { "115": { symbol: "provider-115" } };
function providerLabel(code) { return code; }
var views = { detail: { providerTabId: function (code) { return "providerTab-" + (code || "all"); } } };
views.detail.renderProviderTabs = function (facets, selected, scopedFetch) {""" + tabs + """};
process.stdout.write(views.detail.renderProviderTabs([
  { provider: "115", label: "115 网盘", link_count: 1, re0_count: 2 },
  { provider: "quark", label: "夸克网盘", link_count: 3 }], "quark", false));
"""
    html = _run_node(harness)
    assert html.count('aria-selected="true"') == 1
    quark = html[html.index('data-provider="quark"'):]
    assert 'aria-selected="true"' in quark[:200]


def test_a_provider_scoped_response_still_shows_only_that_pan(js):
    # The server already dropped the other pans, so the page renders what it got.
    out = json.loads(_run_node(_cards_harness(js) + """
state.detail.media.provider_facets = [{ provider: "115", label: "115 网盘", link_count: 3, re0_count: 1 }];
state.detail.media.groups = state.detail.media.groups.filter(function (g) { return g.providers["115"]; })
  .map(function (g) { return { group_id: g.group_id, providers: { "115": 1 }, links: g.links.filter(function (l) { return l.provider === "115"; }) }; });
state.detail.media.re0_candidates = state.detail.media.re0_candidates.filter(function (c) { return c.provider === "115"; });
views.detail.renderGroupList();
console.log(JSON.stringify({ html: $("detailGroupList").innerHTML }));
"""))
    assert 'data-pan="115"' in out["html"] and 'data-pan="quark"' not in out["html"]
    assert "pan-grid-single" in out["html"]


# ---------------------------------------------------------------------------
# Round 32 user feedback: the composition chip must adopt what a preview
# discovered; a pan tab filters the cards but never hides the tab buttons;
# every pan is visually distinct; 广亚 is really 光鸭网盘.
# ---------------------------------------------------------------------------


def test_a_finished_preview_updates_the_composition_chip_in_place(js):
    body = _extract(js, r"re0Preview:\s*function\s*\(candidate, btn\)\s*\{([\s\S]*?)\n    \},")
    # The row is not re-rendered (that would close the preview the user just
    # opened), so the chip has to be patched where it stands.
    assert "re0-composition" in body and "candidate.composition = preview.composition" in body
    harness = ESC + """
var chip = { textContent: "构成未说明", title: "", setAttribute: function (k, v) { this[k] = v; } };
var row = { querySelector: function (sel) { return sel === ".re0-composition" ? chip : null; } };
var els = { re0Preview592: { hidden: true, innerHTML: "", dataset: {} } };
function $(id) { return els[id]; }
function esc(s) { return String(s == null ? "" : s); }
var RE0_COMPLETION_LABEL = { complete: "已完结", updating: "更新中", partial: "部分集数" };
function re0CompletionLabel(c) { return c && c.confidence === "file_inferred" ? "据文件名推断" : ""; }
function showRe0Composition(t) { return t !== "movie"; }
var state = { detail: { media: { media_type: "tv" } } };
var document = { querySelector: function (sel) { return sel.indexOf("re0-row") !== -1 ? row : null; } };
var api = { request: function () { return Promise.resolve({ preview: { status: "ready", file_count: 10,
  composition: { display: "S03 · 第 1–10 集", confidence: "file_inferred", completion: "unknown" }, files: [] } }); } };
var views = { detail: { filePreviewHtml: function () { return "<preview>"; } } };
var candidate = { id: 592, composition: { display: "构成未说明" } };
var btn = { disabled: false };
views.detail.re0Preview = function (candidate, btn) {""" + body + """};
views.detail.re0Preview(candidate, btn).then(function () {
  console.log(JSON.stringify({ chip: chip.textContent, title: chip.title || "", comp: candidate.composition.display }));
});
"""
    out = json.loads(_run_node(harness))
    assert out["chip"] == "S03 · 第 1–10 集" and out["comp"] == "S03 · 第 1–10 集"
    assert "构成未说明" not in out["chip"]


def test_a_pan_tab_filters_the_cards_but_keeps_every_tab_button(js):
    out = json.loads(_run_node(_cards_harness(js) + """
views.detail.renderGroupList();
var all = $("detailGroupList").innerHTML;
state.detail.panFilter = "quark";
views.detail.renderGroupList();
var filtered = $("detailGroupList").innerHTML;
state.detail.panFilter = "";
views.detail.renderGroupList();
console.log(JSON.stringify({ all: all, filtered: filtered, cleared: $("detailGroupList").innerHTML }));
"""))
    assert out["all"].count('data-pan="') == 3
    # Only the chosen pan's card survives; 全部 brings the rest straight back.
    assert out["filtered"].count('data-pan="') == 1 and 'data-pan="quark"' in out["filtered"]
    assert "pan-grid-single" in out["filtered"]
    assert out["cleared"].count('data-pan="') == 3


def test_selecting_a_pan_filters_without_refetching_so_the_tab_row_survives(js):
    body = _extract(js, r"selectProvider:\s*function\s*\(code\)\s*\{([\s\S]*?)\n    \},")
    # A refetch with ?provider= is what used to delete the other tab buttons.
    assert "fetchChannel(" not in body and "detailRequestUrl(" not in body
    assert "state.detail.panFilter = normalized" in body and "renderGroupList()" in body
    assert "aria-selected" in body


def test_every_pan_has_its_own_mark(js):
    """The provider sprite carries a local vector reconstruction of each
    service's own app mark (docs/claude-provider-icon-redraw-20260910.md).
    What this guards is that the marks stay distinct, stay local, and stay in
    step with the tab accent colours -- not the exact path data."""
    icons = (ROOT / "static" / "icons.svg").read_text(encoding="utf-8")
    symbols = {}
    for match in re.finditer(r'<symbol id="(provider-[a-z0-9]+)"([^>]*)>([\s\S]*?)</symbol>', icons):
        # Attributes matter: stroke="currentColor" often sits on the symbol itself.
        symbols[match.group(1)] = (match.group(2) + ">" + match.group(3)).strip()
    pans = ("115", "tianyicloud", "quark", "alipan", "baidu", "guangya", "139cloud", "123")
    for code in (*pans, "ed2k", "link"):
        assert f"provider-{code}" in symbols, code
    artwork = [symbols[f"provider-{c}"] for c in pans]
    assert len(set(artwork)) == len(artwork), "two pans share identical artwork"

    # Local only: no remote image, font or CDN request at render time.
    for code, body in symbols.items():
        assert "http://" not in body and "https://" not in body, code
        assert "<image" not in body and "@font-face" not in body, code

    # ED2K and the unknown fallback stay neutral -- a protocol is not a pan.
    for code in ("ed2k", "link"):
        assert 'stroke="currentColor"' in symbols[f"provider-{code}"], code
        assert not re.search(r'(fill|stroke)="#[0-9a-fA-F]{6}"', symbols[f"provider-{code}"]), code

    # Every pan mark is drawn in its own brand colour, and that colour is the
    # tab accent the JS uses, so the underline and the logo cannot drift apart.
    accents = _extract(js, r"var PROVIDER_ACCENT = (\{[\s\S]*?\});").lower()
    for code in pans:
        colours = {c.lower() for c in re.findall(r'(?:fill|stroke|stop-color)="(#[0-9a-fA-F]{6})"', symbols[f"provider-{code}"])}
        assert colours, f"{code} has no brand colour at all"
        accent = re.search(rf'"?{re.escape(code)}"?\s*:\s*"(#[0-9a-fA-F]{{6}})"', accents)
        assert accent, f"{code} has no PROVIDER_ACCENT entry"
        assert accent.group(1) in colours, f"{code}: accent {accent.group(1)} is not a colour of its mark {colours}"
    brand_accents = [re.search(rf'"?{re.escape(c)}"?\s*:\s*"(#[0-9a-fA-F]{{6}})"', accents).group(1) for c in pans]
    assert len(set(brand_accents)) == len(brand_accents), f"two pans share an accent: {brand_accents}"


def test_no_hidden_count_or_audit_toggle_reaches_the_page(js):
    """Round 37 replaced the announcement: the work order asks for the
    resource list to simply end. The count still reaches the client in
    re0_invalid_hidden_count, and toggleInvalid() still works -- neither is
    rendered."""
    out = json.loads(_run_node(_cards_harness(js) + """
state.detail.media.re0_invalid_hidden_count = 2;
views.detail.renderGroupList();
console.log(JSON.stringify({ html: $("detailGroupList").innerHTML }));
"""))
    for phrase in ("已隐藏 2 条失效 RE0 候选", "re0ShowInvalid", "显示失效候选", "隐藏失效候选", "re0-toolbar"):
        assert phrase not in out["html"], phrase


# ---------------------------------------------------------------------------
# Auxiliary-copy work order: the detail page's RE0 toolbar and the site footer
# are gone from the normal UI. The resources, the unlock flow and the backend
# capabilities behind the removed buttons all stay.
# ---------------------------------------------------------------------------


def test_the_resource_area_ends_with_the_resources(js):
    out = json.loads(_run_node(_cards_harness(js) + """
views.detail.renderGroupList();
var withCards = $("detailGroupList").innerHTML;
state.detail.media.re0_invalid_hidden_count = 3;
views.detail.renderGroupList();
var withHidden = $("detailGroupList").innerHTML;
state.detail.media.groups = []; state.detail.media.re0_candidates = []; state.detail.media.provider_facets = [];
views.detail.renderGroupList();
console.log(JSON.stringify({ withCards: withCards, withHidden: withHidden, empty: $("detailGroupList").innerHTML }));
"""))
    for name, html in out.items():
        assert "re0-toolbar" not in html, name
        assert "re0ShowInvalid" not in html and "re0Refresh" not in html, name
        for phrase in ("已隐藏", "刷新 RE0 候选", "显示失效候选", "隐藏失效候选",
                       "RE0 候选未解锁前只显示规格与状态", "暂无 RE0 候选"):
            assert phrase not in html, (name, phrase)
    # The resources themselves are untouched, and the list simply ends.
    assert out["withCards"].count('data-pan="') == 3 and out["withCards"].rstrip().endswith("</div>")
    assert "暂无资源" in out["empty"]


def test_the_empty_state_no_longer_points_at_a_control_that_is_not_here(js):
    out = json.loads(_run_node(_cards_harness(js) + """
state.detail.media.re0_candidates = [];
state.detail.media.provider_facets = [];
state.detail.media.groups = [{ group_id: 1, providers: { "115": 1 }, links: [
  { link_id: "a", provider: "115", check_status: "unknown" }, { link_id: "b", provider: "115", deleted: true } ] }];
views.detail.renderGroupList();
console.log(JSON.stringify({ html: $("detailGroupList").innerHTML }));
"""))
    assert "暂无资源" in out["html"] and "2 条链接未通过检测或已失效，已隐藏" in out["html"]
    assert "勾选" not in out["html"] and "含已失效" not in out["html"]


def test_no_binding_is_left_pointing_at_a_removed_button(js):
    body = _extract(js, r"renderGroupList:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    assert "re0ToolbarHtml" not in body
    assert '$("re0Refresh")' not in body and '$("re0ShowInvalid")' not in body
    assert ".onclick" not in body
    # The helper that only fed those buttons is gone; nothing calls it either.
    assert "re0ToolbarHtml" not in js


def test_the_capabilities_behind_the_removed_buttons_survive(js):
    # §2/§4.2: the UI entry points go, the functions and the API do not.
    for name in ("re0Refresh: function", "toggleInvalid: function", "ensureRe0Fresh: function", "reloadRe0: function"):
        assert name in js, name
    refresh = _extract(js, r"re0Refresh:\s*function\s*\(btn\)\s*\{([\s\S]*?)\n    \},")
    assert '"/api/library/re0/refresh"' in refresh
    toggle = _extract(js, r"toggleInvalid:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},")
    assert "state.detail.includeInvalid" in toggle
    url = _extract(js, r"function detailRequestUrl\(mediaId, provider\) \{([\s\S]*?)\n  \}")
    assert "include_invalid=1" in url          # the contract is untouched
    fresh = _extract(js, r"ensureRe0Fresh:\s*function\s*\(media\)\s*\{([\s\S]*?)\n    \},")
    assert "if_stale: true" in fresh           # the automatic refresh still runs


# ---------------------------------------------------------------------------
# Round 40 (2026-09-11): the count beside each pan logo is local links PLUS
# the RE0 candidates that pan is offering, and it has to follow the data --
# up when a refresh pulls new candidates in, down when the checker takes
# them away.
# ---------------------------------------------------------------------------


def _tabs_harness(js: str) -> str:
    parts = {
        "renderProviderTabs": _extract(js, r"renderProviderTabs:\s*function\s*\(facets, selected, scopedFetch\)\s*\{([\s\S]*?)\n    \},"),
        "syncProviderTabs": _extract(js, r"syncProviderTabs:\s*function\s*\(\)\s*\{([\s\S]*?)\n    \},"),
        "summaryRowHtml": _extract(js, r"summaryRowHtml:\s*function\s*\(media, facetCount\)\s*\{([\s\S]*?)\n    \},"),
    }
    usable = _extract(js, r"function linkIsUsable\(link\) \{([\s\S]*?)\n  \}")
    pan_count = _extract(js, r"function panLinkCount\(media, code\) \{([\s\S]*?)\n  \}")
    unstored = _extract(js, r"function re0UnstoredCandidates\(media, code\) \{([\s\S]*?)\n  \}")
    resource_count = _extract(js, r"function panResourceCount\(media, code\) \{([\s\S]*?)\n  \}")
    facets_fn = _extract(js, r"function visibleFacets\(media\) \{([\s\S]*?)\n  \}")
    return ESC + """
var ICONS_URL = "/static/icons.svg?v=test";
var PROVIDER_META_BY_CODE = { "115": { symbol: "provider-115" }, quark: { symbol: "provider-quark" }, tianyicloud: { symbol: "provider-tianyicloud" } };
function providerLabel(code) { return { "115": "115 网盘", quark: "夸克网盘", tianyicloud: "天翼云盘", alipan: "阿里云盘" }[code] || code; }
function makeEl() { return { innerHTML: "", className: "", parentNode: null, querySelectorAll: function () { return []; } }; }
var elements = { libraryProviderTabs: makeEl(), detailSummaryRow: makeEl(), libraryProviderPanel: makeEl() };
var inserted = [];
elements.libraryProviderPanel.parentNode = { insertBefore: function (node) { inserted.push(node); elements.libraryProviderTabs = makeEl(); } };
function $(id) { return Object.prototype.hasOwnProperty.call(elements, id) ? elements[id] : null; }
var document = { createElement: function () { return makeEl(); } };
function linkIsUsable(link) {""" + usable + """}
function panLinkCount(media, code) {""" + pan_count + """}
function re0UnstoredCandidates(media, code) {""" + unstored + """}
function panResourceCount(media, code) {""" + resource_count + """}
function visibleFacets(media) {""" + facets_fn + """}
var bound = 0;
var state = { detail: { provider: "", panFilter: "", scopedFetch: false, includeDeleted: false, media: null } };
var views = { detail: { providerTabId: function (code) { return "ptab-" + (code || "all"); }, bindProviderTabs: function () { bound++; } } };
""" + "".join('views.detail.%s = function (%s) {%s};\n' % (name, {"renderProviderTabs": "facets, selected, scopedFetch", "syncProviderTabs": "", "summaryRowHtml": "media, facetCount"}[name], body) for name, body in parts.items()) + """
function tabCounts() {
  var out = {};
  var html = elements.libraryProviderTabs.innerHTML;
  var re = /data-provider="([^"]*)"[\\s\\S]*?(?:<span class="provider-tab-count">(\\d+)<\\/span>)?<\\/button>/g;
  var m;
  while ((m = re.exec(html)) !== null) out[m[1] || "all"] = m[2] === undefined ? null : Number(m[2]);
  return out;
}
"""


def _media(**over):
    media = {
        "group_count": 1,
        "provider_facets": [{"provider": "115", "label": "115 网盘"}],
        "groups": [{"group_id": 1, "display_title": "A", "links": [
            {"link_id": "a1", "provider": "115", "label": "a1"},
            {"link_id": "a2", "provider": "115", "label": "a2", "invalid": True},
        ]}],
        "re0_candidates": [{"id": 11, "provider": "115"}, {"id": 12, "provider": "115"}],
    }
    media.update(over)
    return media


def test_the_pan_count_adds_re0_candidates_to_the_local_links(js):
    out = json.loads(_run_node(_tabs_harness(js) + """
state.detail.media = %s;
views.detail.syncProviderTabs();
console.log(JSON.stringify({ counts: tabCounts(), bound: bound }));
""" % json.dumps(_media())))
    # One usable local link (a2 is checker-invalid) plus two RE0 candidates.
    assert out["counts"]["115"] == 3
    assert out["bound"] == 1


def test_a_candidate_already_stored_locally_is_not_counted_twice(js):
    media = _media(re0_candidates=[
        {"id": 11, "provider": "115", "has_local_link": True, "resource_link_id": "pub-a1"},
        {"id": 12, "provider": "115"},
    ])
    out = json.loads(_run_node(_tabs_harness(js) + """
state.detail.media = %s;
views.detail.syncProviderTabs();
console.log(JSON.stringify({ counts: tabCounts() }));
""" % json.dumps(media)))
    # The unlocked candidate IS the local link -- one link + one new candidate.
    assert out["counts"]["115"] == 2


def test_new_candidates_raise_the_count_and_a_new_pan_gains_its_tab(js):
    out = json.loads(_run_node(_tabs_harness(js) + """
state.detail.media = %s;
views.detail.syncProviderTabs();
var before = tabCounts();
state.detail.media.re0_candidates.push({ id: 13, provider: "115" });
state.detail.media.re0_candidates.push({ id: 14, provider: "quark" });
views.detail.syncProviderTabs();
console.log(JSON.stringify({ before: before, after: tabCounts(), summary: elements.detailSummaryRow.innerHTML }));
""" % json.dumps(_media())))
    assert out["before"] == {"115": 3}
    assert out["after"]["115"] == 4
    assert out["after"]["quark"] == 1
    # A second pan means the tablist now offers 全部 as well, and the summary
    # row's 来源 count follows the same facets.
    assert "all" in out["after"]
    assert "2 个来源" in out["summary"]


def test_candidates_lost_to_the_checker_lower_the_count_and_drop_the_tab(js):
    media = _media(
        groups=[{"group_id": 1, "display_title": "A", "links": [{"link_id": "a1", "provider": "115", "label": "a1"}]}],
        re0_candidates=[{"id": 11, "provider": "115"}, {"id": 12, "provider": "tianyicloud"}],
    )
    out = json.loads(_run_node(_tabs_harness(js) + """
state.detail.media = %s;
views.detail.syncProviderTabs();
var before = tabCounts();
// The server hides a candidate the checker confirmed dead: it simply stops
// coming back in re0_candidates.
state.detail.media.re0_candidates = [{ id: 11, provider: "115" }];
views.detail.syncProviderTabs();
console.log(JSON.stringify({ before: before, after: tabCounts(), summary: elements.detailSummaryRow.innerHTML }));
""" % json.dumps(media)))
    assert out["before"] == {"all": None, "115": 2, "tianyicloud": 1}
    # 天翼 had nothing but that candidate, so its tab goes with it, and with
    # one pan left there is no 全部 tab either.
    assert out["after"] == {"115": 2}
    assert "1 个来源" in out["summary"]


def test_a_pan_that_loses_everything_cannot_stay_the_selected_filter(js):
    media = _media(
        groups=[{"group_id": 1, "display_title": "A", "links": [{"link_id": "a1", "provider": "115", "label": "a1"}]}],
        re0_candidates=[{"id": 12, "provider": "tianyicloud"}],
    )
    out = json.loads(_run_node(_tabs_harness(js) + """
state.detail.media = %s;
state.detail.panFilter = "tianyicloud";
views.detail.syncProviderTabs();
var kept = state.detail.panFilter;
state.detail.media.re0_candidates = [];
views.detail.syncProviderTabs();
console.log(JSON.stringify({ kept: kept, after: state.detail.panFilter, counts: tabCounts() }));
""" % json.dumps(media)))
    assert out["kept"] == "tianyicloud"
    # Its tab is gone, so the resource area falls back to showing every pan
    # rather than filtering to one that no longer exists.
    assert out["after"] == ""
    assert "tianyicloud" not in out["counts"]


def test_the_switcher_appears_when_re0_finds_the_first_resource_of_all(js):
    empty = _media(group_count=0, provider_facets=[], groups=[], re0_candidates=[])
    out = json.loads(_run_node(_tabs_harness(js) + """
elements.libraryProviderTabs = null;
delete elements.libraryProviderTabs;
state.detail.media = %s;
views.detail.syncProviderTabs();
var noTabs = inserted.length;
state.detail.media.re0_candidates = [{ id: 21, provider: "alipan" }];
views.detail.syncProviderTabs();
console.log(JSON.stringify({ noTabs: noTabs, inserted: inserted.length, className: (inserted[0] || {}).className, counts: tabCounts() }));
""" % json.dumps(empty)))
    # Nothing to show, nothing inserted; once RE0 finds a share the switcher
    # is created so the card below it never appears without its tab.
    assert out["noTabs"] == 0
    assert out["inserted"] == 1
    assert out["className"] == "provider-switcher"
    assert out["counts"]["alipan"] == 1


def test_the_card_header_still_counts_the_rows_the_card_lists(js):
    """The tab counts distinct resources; the card header describes its own
    rows, where an unlocked candidate legitimately appears twice (its RE0 row
    says 已解锁, and its link row is there too)."""
    media = _media(re0_candidates=[
        {"id": 11, "provider": "115", "has_local_link": True, "resource_link_id": "pub-a1"},
        {"id": 12, "provider": "115"},
    ])
    out = json.loads(_run_node(_cards_harness(js) + """
state.detail.media = %s;
state.detail.panFilter = "";
views.detail.renderGroupList();
console.log(JSON.stringify({ html: $("detailGroupList").innerHTML }));
""" % json.dumps(media)))
    card = out["html"]
    assert "1 条链接" in card
    assert "2 条 RE0 候选" in card
