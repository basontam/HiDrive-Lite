"""Phase 2: the sign-in / apply page and the one endpoint it bootstraps from.

Plan §11. The page must sign people in with or without TMDB, must never
carry a key or a backend setting, and must credit TMDB.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def page(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    return response.get_data(as_text=True)


@pytest.fixture
def js():
    return (ROOT / "static" / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def css():
    return (ROOT / "static" / "app.css").read_text(encoding="utf-8")


def _run_node(harness: str) -> str:
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


class _LoginHTML(HTMLParser):
    """Build an element tree so layout assertions check real ancestry."""

    VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                           "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self, page):
        super().__init__()
        self.root = ET.Element("document")
        self.stack = [self.root]
        self.feed(page)
        self.close()
        assert self.stack == [self.root], "unclosed login markup"

    def handle_starttag(self, tag, attrs):
        node = ET.SubElement(self.stack[-1], tag, {name: value or "" for name, value in attrs})
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        assert len(self.stack) > 1 and self.stack[-1].tag == tag, f"unexpected closing {tag}"
        self.stack.pop()

    def handle_data(self, data):
        node = self.stack[-1]
        if len(node):
            node[-1].tail = (node[-1].tail or "") + data
        else:
            node.text = (node.text or "") + data

    def one(self, attribute, value):
        matches = [node for node in self.root.iter()
                   if (value in node.get(attribute, "").split() if attribute == "class"
                       else node.get(attribute) == value)]
        assert len(matches) == 1, f"expected one {attribute}={value}, found {len(matches)}"
        return matches[0]


# ---------------------------------------------------------------------------
# the page itself
# ---------------------------------------------------------------------------


class TestLoginPage:
    def test_it_carries_the_brand_and_says_what_the_product_is(self, page):
        assert 'alt="HiDrive-Lite"' in page
        assert "hidrive-lite-logo.svg" in page
        assert "搜索、转存、管理你自己的影视资源" in page

    def test_it_borrows_nobody_elses_identity(self, page):
        for foreign in ("Claude", "Anthropic", "claude.ai"):
            assert foreign not in page

    def test_the_google_entry_says_it_is_for_the_administrator(self, page):
        assert "管理员使用 Google 登录" in page
        assert "Google 入口只对管理员开放" in page

    def test_the_redundant_sign_in_lead_is_empty_and_hidden(self, page):
        assert "使用账号密码登录；管理员请走 Google 入口。" not in page
        lead = _LoginHTML(page).one("id", "loginLead")
        assert lead.tag == "p" and "hidden" in lead.attrib
        assert not "".join(lead.itertext()).strip()

    def test_the_logo_is_above_two_columns_and_the_form_follows_the_headline(self, page):
        tree = _LoginHTML(page)
        shell = tree.one("class", "login-shell")
        header = tree.one("class", "login-header")
        columns = tree.one("class", "login-columns")
        left = tree.one("class", "login-left")
        artwork = tree.one("class", "login-artwork")
        intro = tree.one("class", "login-intro")
        panel = tree.one("class", "login-panel")
        assert shell.tag == "main" and header.tag == "header" and artwork.tag == "aside"
        assert header in list(shell) and columns in list(shell)
        assert list(shell).index(header) < list(shell).index(columns)
        assert list(columns) == [left, artwork], "the two columns must be direct siblings"
        assert list(left) == [intro, panel], "the headline and form must share the left column"
        assert tree.one("class", "brand-logo-picture") in list(header.iter())
        assert tree.one("class", "login-headline") in list(intro.iter())
        assert tree.one("id", "loginForm") in list(panel.iter())
        assert "login-points" not in page

    def test_the_intro_and_copyright_use_the_requested_copy_without_losing_credit(self, page):
        assert "一个地方搜遍资源库，把想看的转存到自己的 115和其他网盘，再按你习惯的方式观看。" in page
        assert "HiDrive Lite Copyright©2026" in page
        assert re.search(r'<details class="login-attribution">\s*<summary>数据来源</summary>[\s\S]*?id="loginAttribution"[\s\S]*?</details>', page)

    def test_it_offers_both_signing_in_and_applying(self, page):
        assert 'id="loginForm"' in page
        assert 'id="loginSwitch"' in page and "申请账号" in page
        assert 'id="loginConfirmField"' in page and "确认密码" in page

    def test_the_password_rules_are_on_the_page_not_only_in_the_error(self, page):
        assert "8–128 位" in page
        assert "大写字母" in page and "小写字母" in page and "数字" in page

    def test_every_input_has_a_label_and_an_autocomplete_hint(self, page):
        for field, autocomplete in (("loginEmail", "username"), ("loginPassword", "current-password"),
                                    ("loginConfirm", "new-password")):
            assert f'for="{field}"' in page, field
            assert re.search(rf'id="{field}"[^>]*autocomplete="{autocomplete}"', page), field

    def test_the_message_area_announces_itself(self, page):
        assert re.search(r'id="loginMessage"[^>]*role="status"[^>]*aria-live="polite"', page)

    def test_the_collage_is_decoration_and_says_so(self, page):
        assert re.search(r'id="loginArtwork"[^>]*aria-hidden="true"', page)

    def test_it_credits_tmdb_without_naming_a_key(self, client, page):
        assert 'id="loginAttribution"' in page
        payload = client.get("/api/auth/bootstrap").get_json()
        assert payload["tmdb_attribution"] == (
            "This product uses the TMDB API but is not endorsed or certified by TMDB.")

    def test_no_credential_reaches_the_markup(self, page):
        lowered = page.lower()
        for forbidden in ("api_key", "apikey", "tmdb_api", "secret", "cookie", "access_token"):
            assert forbidden not in lowered, forbidden

    def test_it_wears_the_same_favicon_as_the_app(self, page):
        assert page.count('rel="icon"') == 4
        assert 'rel="apple-touch-icon"' in page

    def test_it_loads_the_shared_bundle_and_marks_itself(self, page):
        assert 'data-page="login"' in page
        assert re.search(r'src="/static/app\.js\?v=[0-9a-f]{8}"', page)


class TestLoginLayout:
    def test_desktop_form_has_its_own_centered_width_limit(self, css):
        assert ".login-intro{text-align:center}" in css
        assert ".login-panel{width:min(100%,clamp(440px,32vw,1040px));align-self:center}" in css

    def test_the_artwork_uses_neutral_framing_without_tinting_the_posters(self, css, page):
        artwork = self._rule(css, ".login-artwork")
        grid = self._rule(css, ".login-artwork-grid")
        assert "background:var(--surface-solid)" in artwork
        assert "gradient" not in artwork
        assert "opacity" not in grid
        assert "login-artwork-veil" not in css + page

    @staticmethod
    def _rule(css, selector):
        match = re.search(re.escape(selector) + r"\{([^}]+)\}", css)
        assert match, selector
        return match.group(1)

    def test_the_poster_grid_cannot_set_the_pages_intrinsic_height(self, css):
        columns = self._rule(css, ".login-columns")
        assert re.findall(r"(?:^|;)\s*display\s*:\s*([^;]+)", columns)[-1:] == ["grid"]
        assert "grid-template-columns:minmax(0,1fr) minmax(0,1fr)" in columns
        assert "align-items:stretch" in columns
        artwork = self._rule(css, ".login-artwork")
        assert re.findall(r"(?:^|;)\s*position\s*:\s*([^;]+)", artwork)[-1:] == ["relative"]
        assert "min-height:0" in artwork
        assert "align-self:stretch" in artwork
        assert not re.search(r"(?:^|;)\s*(?:height|max-height)\s*:", artwork)
        grid = self._rule(css, ".login-artwork-grid")
        assert re.findall(r"(?:^|;)\s*display\s*:\s*([^;]+)", grid)[-1:] == ["grid"]
        assert "position:absolute" in grid and "inset:0" in grid
        assert "grid-template-columns:repeat(4,minmax(0,1fr))" in grid
        assert "grid-template-rows:repeat(3,minmax(0,1fr))" in grid
        image = self._rule(css, ".login-artwork-grid img")
        assert "height:100%" in image and "min-height:0" in image
        assert "object-fit:cover" in image and "object-position:center" in image
        assert "border-radius:12px" in image
        assert "container-type:size" in self._rule(css, ".login-poster")
        turn = self._rule(css, ".login-poster-turn")
        assert "width:min(100cqw,calc(100cqh * 2 / 3))" in turn
        assert "height:min(100cqh,calc(100cqw * 3 / 2))" in turn
        assert "margin:auto" in turn and "transform-style:preserve-3d" in turn
        assert "aspect-ratio:8/9" in artwork

    def test_the_form_keeps_natural_height_and_the_mobile_collage_stays_hidden(self, css):
        for selector in (".login-body", ".login-shell", ".login-columns", ".login-left", ".login-panel", ".login-card"):
            rule = self._rule(css, selector)
            assert not re.search(r"(?:^|;)\s*(?:height|max-height)\s*:", rule), selector
            assert not re.search(r"overflow(?:-[xy])?\s*:\s*(?:hidden|clip)", rule), selector
        shell = self._rule(css, ".login-shell")
        assert "padding:24px 32px" in shell
        assert "max-width:1220px" in shell
        mobile = css.split("@media (max-width:900px)", 1)[1]
        assert ".login-columns{grid-template-columns:minmax(0,1fr)" in mobile
        assert ".login-artwork{display:none}" in mobile
        assert re.search(r"\.login-field input:focus-visible\{[^}]*outline:", css)


# ---------------------------------------------------------------------------
# the bootstrap endpoint
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_it_returns_only_what_the_page_needs(self, client):
        payload = client.get("/api/auth/bootstrap").get_json()
        assert set(payload) == {
            "success", "csrf", "registration_open", "admin_email_hint",
            "artwork", "artwork_source", "tmdb_attribution",
        }

    def test_it_hands_over_a_usable_pre_auth_csrf_token(self, client):
        assert client.get("/api/auth/bootstrap").get_json()["csrf"]

    def test_it_never_returns_a_setting_or_a_credential(self, client, hidrive):
        hidrive.secret_set("tmdb_api_key", "fake-tmdb-key")
        body = json.dumps(client.get("/api/auth/bootstrap").get_json())
        assert "fake-tmdb-key" not in body
        for forbidden in ("115", "openlist", "master", "token"):
            assert forbidden not in body.lower(), forbidden

    def test_without_tmdb_it_still_answers_and_the_page_still_works(self, client):
        payload = client.get("/api/auth/bootstrap").get_json()
        assert payload["success"] is True
        assert payload["artwork"] == []
        assert payload["artwork_source"] == "unavailable"

    def test_a_tmdb_failure_never_breaks_the_endpoint(self, client, hidrive, monkeypatch):
        hidrive.secret_set("tmdb_api_key", "fake-tmdb-key")

        def explode(*args, **kwargs):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(hidrive.library_tmdb, "login_artwork", explode)
        payload = client.get("/api/auth/bootstrap").get_json()
        assert payload["success"] is True and payload["artwork"] == []

    def test_the_artwork_it_returns_is_cached_between_calls(self, client, hidrive, monkeypatch):
        hidrive.secret_set("tmdb_api_key", "fake-tmdb-key")
        calls = []

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"results": [
                    {"media_type": "movie", "poster_path": f"/poster{i}abcd.jpg"} for i in range(4)
                ]}

        class FakeSession:
            def get(self, url, params=None, timeout=None):
                calls.append(url)
                return FakeResponse()

        monkeypatch.setattr(hidrive, "_tmdb_session_factory", lambda: FakeSession())
        first = client.get("/api/auth/bootstrap").get_json()
        second = client.get("/api/auth/bootstrap").get_json()
        assert first["artwork_source"] == "fresh" and second["artwork_source"] == "cache"
        assert len(calls) == 1
        assert all(url.startswith("https://image.tmdb.org/t/p/") for url in second["artwork"])

    def test_the_cache_lives_in_settings_and_adds_no_table(self, client, hidrive, monkeypatch):
        before = _table_names(hidrive)
        hidrive.secret_set("tmdb_api_key", "fake-tmdb-key")
        client.get("/api/auth/bootstrap")
        assert _table_names(hidrive) == before


def _table_names(hidrive):
    with hidrive.connect_db() as db:
        return {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ---------------------------------------------------------------------------
# the controller, run for real
# ---------------------------------------------------------------------------


def _harness(js: str, extra: str) -> str:
    match = re.search(r"\n  views\.login = \{([\s\S]*?)\n  \};", js)
    assert match, "expected a views.login controller"
    api_match = re.search(r"var api = \{([\s\S]*?)\n  \};", js)
    assert api_match, "expected the api wrapper"
    return """
var elements = {};
function el(id) {
  return elements[id] || (elements[id] = { id: id, value: "", textContent: "", innerHTML: "", hidden: false,
    className: "", children: [], focused: false, attrs: {},
    classList: { values: new Set(), add(x) { this.values.add(x); }, remove(x) { this.values.delete(x); },
      toggle(x, enabled) { if (enabled) this.values.add(x); else this.values.delete(x); } },
    style: { setProperty(k, v) { this[k] = v; } },
    setAttribute: function (k, v) { this.attrs[k] = v; }, focus: function () { this.focused = true; },
    appendChild: function (c) { this.children.push(c); c.parentNode = this; },
    removeChild: function (c) { this.children = this.children.filter(function (x) { return x !== c; }); },
    addEventListener: function (name, fn) { this["on" + name] = fn; } });
}
function $(id) { return el(id); }
var created = [];
var document = { body: { dataset: { page: "login" } },
  createElement: function () { var node = el("img-" + created.length); created.push(node); return node; } };
var window = { location: { href: "" } };
var setTimeout = function () { return 1; }, clearTimeout = function () {};
var fetched = [];
var api = {""" + api_match.group(1) + """
};
var views = { login: {""" + match.group(1) + """
} };
""" + extra


class TestLoginController:
    def test_all_twelve_tiles_get_two_distinct_faces_and_animate_only_after_loading(self, js, css):
        out = json.loads(_run_node(_harness(js, """
var urls = Array.from({length:24}, (_, i) => "https://image.tmdb.org/t/p/w342/poster" + i + "abcd.jpg");
views.login.renderArtwork(urls);
var turns = $("loginArtworkGrid").children.map(tile => tile.children[0]);
var before = turns.filter(turn => turn.classList.values.has("is-ready")).length;
turns.forEach(turn => turn.children.forEach(img => img.onload()));
document.hidden = true;
views.login.pauseArtwork();
console.log(JSON.stringify({before, tiles:turns.length,
  faces:turns.map(turn => turn.children.length),
  unique:new Set(turns.flatMap(turn => turn.children.map(img => img.src))).size,
  animated:turns.filter(turn => turn.classList.values.has("is-ready")).length,
  paused:$("loginArtworkGrid").classList.values.has("is-paused")}));
""")))
        assert out == {"before": 0, "tiles": 12, "faces": [2] * 12,
                       "unique": 24, "animated": 12, "paused": True}
        assert "animation:login-poster-flip 12s" in css
        # Quarter-cycle holds alternate with 3-second X-axis half turns.
        assert "0%,25%{transform:rotateX(0deg)}" in css
        assert "50%,75%{transform:rotateX(-180deg)}" in css
        assert "100%{transform:rotateX(-360deg)}" in css
        assert ".login-poster-back{transform:rotateX(180deg)}" in css
        assert "border-radius:12px" in css
        assert "prefers-reduced-motion:reduce" in css

    def test_no_manual_animation_button_or_dangling_handler(self, js, css):
        template = (Path(__file__).resolve().parents[1] / "templates/login.html").read_text()
        assert "loginArtworkToggle" not in template + js + css
        assert "暂停海报动画" not in template + js
        out = _run_node(_harness(js, """
document.hidden = true;
views.login.pauseArtwork();
document.hidden = false;
views.login.pauseArtwork();
console.log($("loginArtworkGrid").classList.values.has("is-paused"));
"""))
        assert out.strip() == "false"

    def test_failed_back_face_keeps_front_static(self, js):
        out = json.loads(_run_node(_harness(js, """
views.login.renderArtwork(Array.from({length:24}, (_, i) => "https://image.tmdb.org/t/p/w342/poster" + i + "abcd.jpg"));
var turn = $("loginArtworkGrid").children[0].children[0];
turn.children[1].onerror();
turn.children[0].onload();
console.log(JSON.stringify({faces:turn.children.length, animated:turn.classList.values.has("is-ready")}));
""")))
        assert out == {"faces": 1, "animated": False}

    def test_trend_refresh_preserves_the_form_and_old_art_on_empty_response(self, js):
        out = json.loads(_run_node(_harness(js, """
var delay = 0;
setTimeout = function(fn, ms) { delay = ms; return 1; };
views.login.renderArtwork(["https://image.tmdb.org/t/p/w342/oldposterabcd.jpg"]);
var tile = $("loginArtworkGrid").children[0];
$("loginEmail").value = "member@example.test";
api.csrf = "original-fixture";
api.request = function() { return Promise.resolve({artwork:[], csrf:"changed-fixture"}); };
views.login.refreshArtwork().then(function() {
  console.log(JSON.stringify({delay, same:$("loginArtworkGrid").children[0] === tile,
    csrf:api.csrf, email:$("loginEmail").value, inflight:views.login.artworkRequest}));
});
""")))
        assert out == {"delay": 6 * 3600000, "same": True, "csrf": "original-fixture",
                       "email": "member@example.test", "inflight": None}

    def test_it_draws_only_tmdb_urls_and_drops_anything_else(self, js):
        out = json.loads(_run_node(_harness(js, """
views.login.renderArtwork([
  "https://image.tmdb.org/t/p/w342/ok1abcd.jpg",
  "https://evil.test/x.jpg",
  "javascript:alert(1)",
  "https://image.tmdb.org/t/p/w342/ok2abcd.jpg"
]);
var grid = $("loginArtworkGrid");
console.log(JSON.stringify({
  drawn: grid.children.map(function (c) { return c.children[0].children[0].src; }),
  alts: grid.children.map(function (c) { return c.children[0].children[0].alt; })
}));
""")))
        assert out["drawn"] == ["https://image.tmdb.org/t/p/w342/ok1abcd.jpg",
                                "https://image.tmdb.org/t/p/w342/ok2abcd.jpg"]
        assert out["alts"] == ["", ""], "the collage is decoration and carries no accessible name"

    def test_a_tile_that_fails_to_load_removes_itself(self, js):
        out = json.loads(_run_node(_harness(js, """
views.login.renderArtwork(["https://image.tmdb.org/t/p/w342/ok1abcd.jpg"]);
var grid = $("loginArtworkGrid");
var tile = grid.children[0].children[0].children[0];
tile.onerror();
console.log(JSON.stringify({ left: grid.children.length }));
""")))
        assert out["left"] == 0

    def test_switching_to_apply_shows_the_second_field_and_the_rules(self, js):
        out = json.loads(_run_node(_harness(js, """
views.login.setMode("register");
var registering = { title: $("loginTitle").textContent, confirmHidden: $("loginConfirmField").hidden,
  submit: $("loginSubmit").textContent, autocomplete: $("loginPassword").attrs.autocomplete,
  lead: $("loginLead").textContent, leadHidden: $("loginLead").hidden };
views.login.setMode("login");
var back = { title: $("loginTitle").textContent, confirmHidden: $("loginConfirmField").hidden,
  submit: $("loginSubmit").textContent, autocomplete: $("loginPassword").attrs.autocomplete,
  lead: $("loginLead").textContent, leadHidden: $("loginLead").hidden };
console.log(JSON.stringify({ registering: registering, back: back, focused: $("loginEmail").focused }));
""")))
        assert out["registering"]["confirmHidden"] is False
        assert out["registering"]["submit"] == "提交申请"
        assert out["registering"]["autocomplete"] == "new-password"
        assert out["registering"]["lead"] == "提交后需要管理员批准才能登录。"
        assert out["registering"]["leadHidden"] is False
        assert out["back"]["confirmHidden"] is True
        assert out["back"]["submit"] == "登录"
        assert out["back"]["autocomplete"] == "current-password"
        assert out["back"]["lead"] == ""
        assert out["back"]["leadHidden"] is True
        assert out["focused"] is True, "the first field takes focus when the form changes shape"

    def test_an_application_ends_in_the_waiting_message_and_clears_the_password(self, js):
        out = json.loads(_run_node(_harness(js, """
fetch = function (url, opt) {
  fetched.push({ url: url, body: JSON.parse(opt.body) });
  return Promise.resolve({ ok: true, status: 200, url: url,
    headers: { get: function () { return "application/json"; } },
    json: function () { return Promise.resolve({ success: true, status: "pending" }); } });
};
views.login.setMode("register");
$("loginEmail").value = "m@example.test";
$("loginPassword").value = "Correct1Horse";
$("loginConfirm").value = "Correct1Horse";
views.login.submit().then(function () {
  console.log(JSON.stringify({ url: fetched[0].url, message: $("loginMessage").textContent,
    kind: $("loginMessage").className, password: $("loginPassword").value, redirected: window.location.href }));
});
""")))
        assert out["url"] == "/api/auth/register"
        assert out["message"] == "申请已提交，等待管理员审批。"
        assert "is-done" in out["kind"]
        assert out["password"] == ""
        assert out["redirected"] == "", "an application never signs anyone in"

    def test_a_failed_sign_in_shows_the_servers_one_message(self, js):
        out = json.loads(_run_node(_harness(js, """
fetch = function (url, opt) {
  return Promise.resolve({ ok: false, status: 401, url: url,
    headers: { get: function () { return "application/json"; } },
    json: function () { return Promise.resolve({ message: "邮箱或密码不正确，或账号尚未获批" }); } });
};
$("loginEmail").value = "m@example.test";
$("loginPassword").value = "Wrong1Password";
views.login.submit().then(function () {
  console.log(JSON.stringify({ message: $("loginMessage").textContent, kind: $("loginMessage").className,
    disabled: $("loginSubmit").disabled, redirected: window.location.href }));
});
""")))
        assert out["message"] == "邮箱或密码不正确，或账号尚未获批"
        assert "is-error" in out["kind"]
        assert out["disabled"] is False, "the button comes back so the user can try again"
        assert out["redirected"] == ""

    def test_a_mismatched_confirmation_never_reaches_the_server(self, js):
        out = json.loads(_run_node(_harness(js, """
fetch = function () { fetched.push("called"); return Promise.reject(new Error("must not be called")); };
views.login.setMode("register");
$("loginEmail").value = "m@example.test";
$("loginPassword").value = "Correct1Horse";
$("loginConfirm").value = "Different1Horse";
views.login.submit().then(function () {
  console.log(JSON.stringify({ calls: fetched.length, message: $("loginMessage").textContent }));
});
""")))
        assert out["calls"] == 0
        assert out["message"] == "两次输入的密码不一致"

    def test_a_bootstrap_failure_leaves_a_usable_page(self, js):
        out = json.loads(_run_node(_harness(js, """
fetch = function (url) {
  return Promise.resolve({ ok: false, status: 500, url: url,
    headers: { get: function () { return "application/json"; } },
    json: function () { return Promise.resolve({ message: "boom" }); } });
};
views.login.bootstrap().then(function () {
  console.log(JSON.stringify({ tiles: $("loginArtworkGrid").children.length,
    message: $("loginMessage").textContent, switchHidden: $("loginSwitch").hidden }));
});
""")))
        assert out["tiles"] == 0
        assert out["message"] == "", "a missing decoration is not an error to show the user"
        assert out["switchHidden"] is False
