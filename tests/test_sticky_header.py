"""Sticky-header work order: the top block hides on scroll down and returns on
scroll up, without leaving the document flow, the accessibility tree, or the
existing filter/modal behaviour any different.

The controller's real functions are extracted from static/app.js and run in
node against a stubbed window, so the thresholds and idempotence are tested
rather than described."""

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


@pytest.fixture
def page(client):
    return client.get("/").get_data(as_text=True)


def _extract(js: str, pattern: str) -> str:
    match = re.search(pattern, js)
    assert match, f"pattern not found: {pattern}"
    return match.group(1)


def _run_node(harness: str) -> str:
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _controller_harness(js: str, *, modal_open: str = "false") -> str:
    body = _extract(js, r"var stickyHeader = (\{[\s\S]*?\n  \});")
    return """
var classes = {};
var el = {
  offsetHeight: 120,
  classList: {
    add: function (c) { classes[c] = (classes[c] || 0) + 1; el._on = true; },
    remove: function (c) { classes[c + ":removed"] = (classes[c + ":removed"] || 0) + 1; el._on = false; },
    contains: function (c) { return !!el._on; }
  },
  style: { setProperty: function (k, v) { el._var = v; } },
  addEventListener: function () {}
};
var scrollY = 0;
var window = { scrollY: 0, pageYOffset: 0, addEventListener: function () {},
  requestAnimationFrame: function (fn) { fn(); return 1; },
  getComputedStyle: function () { return { marginBottom: "24px" }; } };
var document = { body: { classList: { contains: function (c) { return """ + modal_open + """; } } },
  querySelector: function () { return el; }, addEventListener: function () {} };
var stickyHeader = """ + body + """;
stickyHeader.el = el;
stickyHeader.currentY = function () { return scrollY; };
function at(y) { scrollY = y; stickyHeader.onFrame(); return !!el._on; }
"""


def test_the_controller_hides_on_the_way_down_and_returns_on_the_way_up(js):
    out = json.loads(_run_node(_controller_harness(js) + """
stickyHeader.lastY = 0;
var trace = [];
[0, 40, 70, 200, 400].forEach(function (y) { trace.push([y, at(y)]); });      // going down
[380, 300].forEach(function (y) { trace.push([y, at(y)]); });                  // coming back up
trace.push([4, at(4)]);                                                        // back at the top
console.log(JSON.stringify(trace));
"""))
    hidden = dict((y, on) for y, on in out)
    assert hidden[0] is False and hidden[40] is False        # inside the top guard
    assert hidden[200] is True and hidden[400] is True       # scrolled down, hidden
    assert hidden[300] is False                              # one upward move brings it back
    assert hidden[4] is False                                # the top always shows it


def test_a_small_jitter_never_toggles(js):
    out = json.loads(_run_node(_controller_harness(js) + """
stickyHeader.lastY = 400;
el._on = true;
var states = [];
[403, 400, 404, 399].forEach(function (y) { states.push(at(y)); });   // all under the dead zone
console.log(JSON.stringify({ states: states, writes: classes }));
"""))
    assert out["states"] == [True, True, True, True]
    assert not out["writes"].get("is-scroll-hidden:removed")


def test_reveal_and_hide_are_idempotent(js):
    out = json.loads(_run_node(_controller_harness(js) + """
stickyHeader.reveal(); stickyHeader.reveal();
stickyHeader.hide(); stickyHeader.hide(); stickyHeader.hide();
stickyHeader.reveal(); stickyHeader.reveal();
console.log(JSON.stringify(classes));
"""))
    assert out.get("is-scroll-hidden") == 1
    assert out.get("is-scroll-hidden:removed") == 1


def test_an_open_modal_freezes_the_header_visible(js):
    out = json.loads(_run_node(_controller_harness(js, modal_open="true") + """
stickyHeader.lastY = 0;
var states = [at(200), at(600)];
console.log(JSON.stringify(states));
"""))
    assert out == [False, False]


def test_the_hide_offset_is_measured_not_assumed(js):
    out = _run_node(_controller_harness(js) + """
stickyHeader.measure();
process.stdout.write(String(el._var));
""")
    # 120px tall + 24px bottom margin -> the whole block clears the viewport.
    assert out == "-144px"


def test_the_controller_reads_the_window_and_coalesces_with_a_frame(js):
    body = _extract(js, r"var stickyHeader = (\{[\s\S]*?\n  \});")
    assert "requestAnimationFrame" in body and "passive" in body
    assert "window.scrollY" in body and "pageYOffset" in body
    # Focus must reveal: a keyboard user may never see a hidden button.
    assert "focusin" in body and "reveal" in body


def test_the_header_hides_by_transform_and_opacity_only(css):
    rule = re.search(r"\.site-sticky\.is-scroll-hidden\{([^}]*)\}", css)
    assert rule, "expected an .is-scroll-hidden rule"
    body = rule.group(1)
    assert "translate3d" in body and "--site-sticky-hide-offset" in body
    assert "opacity:0" in body and "pointer-events:none" in body
    # §3.5: the block must keep its place in the flow and stay in the a11y tree.
    for forbidden in ("display:none", "visibility:hidden", "max-height", "position:"):
        assert forbidden not in body, forbidden
    base = re.search(r"\n\.site-sticky\{([^}]*)\}", css)
    assert base and "position:sticky" in base.group(1)
    assert "transition:" in base.group(1) and "will-change:" in base.group(1)


def test_reduced_motion_still_toggles_the_state(css):
    # The global reduce block already collapses every transition to ~0ms, so
    # the header still hides/shows -- it just does not animate.
    block = re.search(r"@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\n\}", css)
    assert block and "transition-duration: .001ms !important" in block.group(1)
    assert ".site-sticky{display:none" not in css


def test_the_markup_and_the_existing_behaviour_are_untouched(page, js):
    assert '<div class="site-sticky">' in page
    assert 'role="tablist"' in page and 'aria-selected="true"' in page
    # §3.6: the block itself must stay in the accessibility tree. Decorative
    # icons inside it keep their own aria-hidden, which is correct.
    block = page.split('<div class="site-sticky">')[1].split("</nav>")[0]
    assert '<div class="site-sticky" aria-hidden' not in page
    assert 'aria-hidden' not in block.split("<header")[0]
    assert '<nav class="workspace-nav" role="tablist" aria-label="工作区">' in block
    # The filter popover still closes on scroll -- a separate listener with a
    # separate job, not replaced by the header controller.
    assert 'if (!$("libraryFilters").hidden) views.library.closeFilters(false);' in js
    # Switching tabs reveals the header without moving the page.
    activate = _extract(js, r"activate: function \(tab\) \{([\s\S]*?)\n    \},")
    assert "stickyHeader.reveal()" in activate and "scrollTo" not in activate


def test_the_site_footer_is_gone_from_every_page(page, css):
    assert 'class="app-footer"' not in page and "<footer" not in page
    assert "只读浏览 OpenList 与 Infuse STRM 目录" not in page
    # No dangling selector, and none of its spacing left behind.
    assert ".app-footer" not in css


def test_the_settings_page_keeps_its_own_explanation(page):
    # §4.3: only the footer goes -- the settings help text stays.
    assert 'id="settingsHelp"' in page
