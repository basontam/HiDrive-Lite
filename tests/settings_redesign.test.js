"use strict";

// Run with: node --test tests/settings_redesign.test.js
// Execute the production view objects with a small DOM surface. No browser,
// application server, network access, or third-party test package is needed.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const styles = fs.readFileSync(path.join(__dirname, "../static/app.css"), "utf8");

test("folder fallback does not blame the web Cookie for an unknown directory error", () => {
  const body = source.slice(source.indexOf("  function friendlyFolderError("), source.indexOf("  function isStrmFile("));
  const context = vm.createContext({});
  vm.runInContext(body, context);
  for (const message of [undefined, "HTTP 502", "ConnectionError"]) {
    assert.equal(context.friendlyFolderError(message), "目录读取失败，请稍后重试");
  }
  assert.equal(context.friendlyFolderError("「目录与云下载」授权已过期，请重新授权"), "「目录与云下载」授权已过期，请重新授权");
});

test("all hero thumbnails are inside the image-fitted frame on desktop and mobile", () => {
  const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
  assert.match(html, /<div class="library-hero-media">\s*<div class="library-hero-frame">[\s\S]*?id="heroThumbnails"[\s\S]*?<\/div>\s*<\/div>\s*<\/div>/);
  assert.match(styles, /\.library-hero-media\{[^}]*container-type:size/);
  assert.match(styles, /\.library-hero-frame\{[^}]*width:min\(100cqw,calc\(100cqh \* var\(--hero-image-ratio/);
  assert.match(styles, /\.library-hero-thumbs\{position:absolute;[^}]*inset:auto 0 0/);
  assert.doesNotMatch(styles, /\.library-hero-thumbs\{[^}]*grid-row:3/);
});

test("large-screen login card sizes to its form rather than stretching to the poster wall", () => {
  const large = styles.slice(styles.indexOf('@media (min-width:1600px) and (min-height:1100px)'));
  assert.match(large, /\.login-left\{justify-content:center;/);
  assert.match(large, /\.login-panel\{flex:0 0 auto\}/);
  assert.match(large, /\.login-card\{flex:0 0 auto;justify-content:flex-start;/);
});

test("home carousel follows day and night palettes without tinting artwork", () => {
  const rule = selector => {
    const start = styles.indexOf(selector + "{");
    assert.ok(start >= 0, `missing ${selector}`);
    return styles.slice(start, styles.indexOf("}", start) + 1);
  };
  const root = styles.match(/:root \{([^}]+)\}/)[1];
  assert.match(root, /--hero-surface:\s*#fff;/);
  for (const selector of [':root:not([data-theme])', ':root[data-theme="dark"]']) {
    const start = styles.indexOf(selector + " {");
    assert.ok(start >= 0);
    assert.match(styles.slice(start, styles.indexOf("}", start)), /--hero-surface:\s*#0c0d12;/);
  }
  assert.match(rule('.library-hero'), /background:var\(--hero-surface\)/);
  assert.match(rule('.library-hero-content'), /color:var\(--text\)/);
  for (const selector of ['.library-hero-overview', '.library-hero-controls']) {
    assert.match(rule(selector), /color:var\(--muted\)/);
  }
  for (const selector of ['.library-hero .library-hero-type', '.library-hero-controls button']) {
    assert.match(rule(selector), /color:var\(--text\)/);
  }
  assert.match(rule('.library-hero-controls button'), /border:1px solid var\(--line\)/);
  for (const selector of ['.library-hero-controls button:focus-visible', '.library-hero-thumb:focus-visible']) {
    assert.match(rule(selector), /outline:2px solid var\(--accent-ink\)/);
  }
  for (const selector of ['.library-hero-backdrop', '.library-hero-thumb img']) {
    assert.match(rule(selector), /object-fit:contain/);
    assert.doesNotMatch(rule(selector), /filter:|opacity:/);
  }
});

function appearanceHarness(saved, systemDark = false, blocked = false) {
  const listeners = {};
  const control = { value: "", addEventListener: (name, fn) => { listeners[name] = fn; } };
  const logo = { media: "(prefers-color-scheme: dark)" };
  const root = { dataset: {} };
  const media = { matches: systemDark, addEventListener: (_, fn) => { listeners.system = fn; } };
  const storage = {
    getItem: () => { if (blocked) throw Error("blocked"); return saved; },
    setItem: (key, value) => { assert.equal(key, "hidrive.theme"); if (blocked) throw Error("blocked"); saved = value; }
  };
  const context = { document: { documentElement: root, getElementById: () => control, querySelectorAll: () => [logo] },
    window: { matchMedia: () => media, addEventListener: (name, fn) => { listeners[name] = fn; } }, localStorage: storage };
  vm.runInNewContext(functionSource("initAppearance") + "\ninitAppearance();", context);
  return { root, logo, control, media, listeners, saved: () => saved,
    change(value) { control.value = value; listeners.change(); },
    external(value, key = "hidrive.theme") { saved = value; listeners.storage({ key }); } };
}

test("appearance follows system by default and updates the logo", () => {
  const h = appearanceHarness(null, true);
  assert.equal(h.root.dataset.theme, "dark");
  assert.equal(h.logo.media, "all");
  assert.equal(h.control.value, "system");
  h.media.matches = false; h.listeners.system();
  assert.equal(h.root.dataset.theme, "light");
  assert.equal(h.logo.media, "not all");
});

test("manual day/night overrides system, persists on reload, and can return to automatic", () => {
  const h = appearanceHarness("light", true);
  assert.equal(h.root.dataset.theme, "light");
  h.listeners.system();
  assert.equal(h.root.dataset.theme, "light");
  h.change("dark");
  assert.equal(h.saved(), "dark");
  assert.equal(appearanceHarness(h.saved(), false).root.dataset.theme, "dark");
  h.change("system");
  h.media.matches = false; h.listeners.system();
  assert.equal(h.root.dataset.theme, "light");
});

test("appearance tolerates denied storage and invalid values and syncs other tabs", () => {
  const blocked = appearanceHarness(null, false, true);
  blocked.change("dark");
  assert.equal(blocked.root.dataset.theme, "dark");
  const h = appearanceHarness("invalid", true);
  assert.equal(h.control.value, "system");
  h.external("light"); assert.equal(h.root.dataset.theme, "light");
  h.external(null, null); assert.equal(h.root.dataset.theme, "dark");
});

test("both pages apply an identical saved theme before CSS and expose the selector", () => {
  const bootstraps = ["index", "login"].map(name => {
    const html = fs.readFileSync(path.join(__dirname, `../templates/${name}.html`), "utf8");
    assert.match(html, /id="themePreference" aria-label="外观模式"/);
    assert.ok(html.indexOf("hidrive.theme") < html.indexOf('rel="stylesheet"'));
    const logoBootstrap = html.match(/<script data-theme-logo>([\s\S]*?)<\/script>/)[1];
    for (const theme of ["dark", "light"]) {
      const logo = { media: "(prefers-color-scheme: dark)" };
      vm.runInNewContext(logoBootstrap, { document: { documentElement: { dataset: { theme } },
        currentScript: { previousElementSibling: { querySelector: () => logo } } } });
      assert.equal(logo.media, theme === "dark" ? "all" : "not all");
    }
    return html.match(/<script>([\s\S]*?)<\/script>/)[1];
  });
  assert.equal(bootstraps[0], bootstraps[1]);
  for (const saved of ["dark", "light", "invalid", null]) {
    const root = { dataset: {} };
    vm.runInNewContext(bootstraps[0], { document: { documentElement: root },
      localStorage: { getItem: () => saved }, window: { matchMedia: () => ({ matches: true }) } });
    assert.equal(root.dataset.theme, saved === "light" ? "light" : "dark");
  }
  assert.ok(source.indexOf("  initAppearance();") < source.indexOf('document.body.dataset.page === "login"'));
});

test("dark palette has neutral poster fallbacks, readable muted text and no artwork tint", () => {
  const dark = styles.match(/:root\[data-theme="dark"\]\s*\{([^}]+)\}/)[1];
  const automatic = styles.match(/:root:not\(\[data-theme\]\)\s*\{([^}]+)\}/)[1];
  assert.equal(dark.replace(/\s/g, ""), automatic.replace(/\s/g, ""));
  assert.match(dark, /--surface-hover:\s*#353539/);
  assert.match(dark, /--placeholder:\s*#a1a1af/);
  for (const selector of [".login-artwork-grid img", ".media-card .poster img"]) {
    const rule = styles.slice(styles.indexOf(selector + "{" )).split("}")[0];
    assert.doesNotMatch(rule, /(?:filter|opacity|mix-blend-mode):/);
  }
  assert.match(styles, /\.login-artwork-grid img\{[^}]*border-radius:12px/);
  assert.match(styles, /\.open115-qr\{[^}]*background:#fff/);
});

test("login centers equal tracks and fits edge-to-edge 2:3 rounded poster faces", () => {
  assert.match(styles, /\.login-shell\{max-width:none;padding-left:var\(--desktop-gutter\);padding-right:var\(--desktop-gutter\)\}/);
  assert.match(styles, /\.login-columns\{[^}]*grid-template-columns:minmax\(0,1fr\) minmax\(0,1fr\)[^}]*margin:auto/);
  assert.match(styles, /max-width:min\(84vw,2800px,calc\(\(100svh - 160px\)/);
  assert.match(styles, /\.login-artwork\{[^}]*aspect-ratio:8\/9/);
  assert.match(styles, /\.login-artwork-grid img\{[^}]*object-fit:cover;object-position:center/);
  assert.match(styles, /\.login-poster\{[^}]*container-type:size/);
  assert.match(styles, /\.login-poster-turn\{[^}]*margin:auto;width:min\(100cqw,calc\(100cqh \* 2 \/ 3\)\);height:min\(100cqh,calc\(100cqw \* 3 \/ 2\)\)/);
  assert.match(styles, /\.login-poster-turn\{[^}]*transform-style:preserve-3d/);
  assert.match(styles, /max-width:calc\(\(100svh - 144px\) \* 16 \/ 9 \+ 40px\)/);
  assert.doesNotMatch(styles, /min\(40%,520px\)|justify-self:start;justify-content:center/);
  assert.match(styles, /@media \(min-width:1600px\) and \(min-height:1100px\)/);
  assert.match(styles, /\.login-artwork\{display:none\}/);
});

test("wide screens use a fluid shell and the settings pane fills its grid track", () => {
  assert.match(styles, /--content-width:\s*100%/);
  assert.match(styles, /--desktop-gutter:\s*clamp\(32px,4vw,160px\)/);
  const settings = styles.match(/\.settings-content\{([^}]+)\}/)[1];
  assert.match(settings, /min-width:0/);
  assert.doesNotMatch(settings, /max-width/);
});

test("wide sticky header shares shell gutters without changing phone padding", () => {
  assert.match(styles, /\.app-shell\{padding-left:var\(--desktop-gutter\);padding-right:var\(--desktop-gutter\)\}/);
  assert.match(styles, /\.site-sticky\{margin-left:calc\(-1 \* var\(--desktop-gutter\)\);margin-right:calc\(-1 \* var\(--desktop-gutter\)\);padding-left:var\(--desktop-gutter\);padding-right:var\(--desktop-gutter\)\}/);
  assert.match(styles, /\.app-shell\{padding:20px 16px 40px\}/);
});

function viewSource(name) {
  const start = source.indexOf("  views." + name + " = {");
  assert.notEqual(start, -1, "production view exists: " + name);
  const end = source.indexOf("\n  };", start);
  assert.notEqual(end, -1, "production view terminates: " + name);
  return source.slice(start, end + "\n  };".length);
}

function functionSource(name) {
  const start = source.indexOf("  function " + name + "(");
  assert.notEqual(start, -1, "production function exists: " + name);
  const end = source.indexOf("\n  }", start);
  assert.notEqual(end, -1, "production function terminates: " + name);
  return source.slice(start, end + "\n  }".length);
}

class Element {
  constructor(id, dataset = {}) {
    this.id = id;
    this.dataset = dataset;
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.innerHTML = "";
    this.className = "";
    this.tabIndex = 0;
    this.attributes = {};
    this.listeners = {};
    this.children = [];
    this.focused = false;
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name),
      toggle: (name, force) => {
        const enabled = force === undefined ? !classes.has(name) : force;
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      },
    };
  }

  get value() { return this._value; }
  set value(value) { this._value = String(value); }

  addEventListener(type, callback) {
    (this.listeners[type] ||= []).push(callback);
  }

  dispatch(type, properties = {}) {
    const event = { target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...properties };
    const callbacks = [...(this.listeners[type] || [])];
    if (typeof this["on" + type] === "function") callbacks.push(this["on" + type]);
    callbacks.forEach(callback => callback.call(this, event));
  }

  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) {
    const key = dataKey(name);
    return key ? this.dataset[key] ?? null : this.attributes[name] ?? null;
  }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { this.focused = true; }
  scrollIntoView() {}
  querySelectorAll(selector) { return this.children.filter(element => matches(element, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    return matches(this, selector) ? this : this.parentElement ? this.parentElement.closest(selector) : null;
  }
}

function dataKey(name) {
  return name.startsWith("data-") ? name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()) : null;
}

function matches(element, selector) {
  if (selector.startsWith("#")) return element.id === selector.slice(1);
  const attribute = selector.match(/^\[([^=\]]+)(?:=["']?([^"'\]]+)["']?)?\]$/);
  if (!attribute) return false;
  const value = element.getAttribute(attribute[1]);
  return value !== null && (attribute[2] === undefined || value === attribute[2]);
}

function harness(viewNames, options = {}) {
  const elements = new Map();
  const requests = [];
  const feedbacks = [];
  const refreshes = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  const add = (id, dataset) => {
    const element = new Element(id, dataset);
    elements.set(id, element);
    return element;
  };
  const context = {
    views: {},
    state: { me: { role: "admin", email: "admin@example.test", allow_member_re0_unlock: false } },
    $: get,
    document: {
      querySelectorAll: selector => [...elements.values()].filter(element => matches(element, selector)),
      querySelector: selector => [...elements.values()].find(element => matches(element, selector)) || null,
      getElementById: get,
    },
    window: { confirm: () => true, location: { assign() {} } },
    api: { request: (url, init = {}) => {
      requests.push({ url, method: init.method || "GET", body: init.body ? JSON.parse(init.body) : undefined });
      return options.request ? options.request(url, init) : Promise.resolve({});
    } },
    feedback: (...args) => feedbacks.push(args),
    toast() {},
    esc: value => String(value ?? ""),
    capability: () => false,
    ROLE_LABEL: { admin: "管理员", member: "普通用户" },
    USER_STATUS_LABEL: {},
    LINKCHECK_PROVIDERS: ["115", "quark", "aliyun", "tianyicloud"],
    linkcheckProviderRowHtml: () => "",
    linkcheckSavePayload: () => ({ linkcheck_enabled: true, linkcheck_providers: { "115": {} } }),
    cloudSavePayload: () => ({ cloud_enabled: true }),
    re0SettingsPayload: () => ({ re0_daily_cap: 1 }),
    setTimeout,
    clearTimeout,
  };
  for (const name of ["refreshStatus", "refreshLibraryStatus", "refreshLinkcheckStatus", "refreshCloudStatus", "refreshRe0SyncStatus"]) {
    context[name] = () => { refreshes.push(name); return Promise.resolve(); };
  }
  vm.createContext(context);
  vm.runInContext(viewSource("settingsNav"), context);
  vm.runInContext(viewSource("approvals"), context);
  context.views.my115 = { load: () => Promise.resolve() };
  for (const name of viewNames) {
    if (name !== "settingsNav") vm.runInContext(viewSource(name), context);
  }
  return { context, get, add, requests, feedbacks, refreshes };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test("approval badge counts, caps and clears without changing tab selection", () => {
  const h = harness([]);
  h.context.capability = () => true;
  const tab = h.get("tab-settings");
  tab.setAttribute("aria-selected", "false");
  for (const [count, text, hidden] of [[1, "1", false], [102, "99+", false], [0, "0", true]]) {
    h.context.views.approvals.render(count);
    assert.equal(h.get("settingsPendingBadge").textContent, text);
    assert.equal(h.get("settingsPendingBadge").hidden, hidden);
    assert.equal(h.get("usersNavPendingBadge").textContent, text);
    assert.equal(h.get("usersNavPendingBadge").hidden, hidden);
    assert.equal(h.get("usersPendingCount").textContent, "待审批 " + count);
    assert.equal(tab.getAttribute("aria-selected"), "false");
  }
  assert.equal(tab.getAttribute("aria-label"), "设置");
  assert.equal(h.get("settings-nav-users").getAttribute("aria-label"), "用户与权限");
});

test("approval polling is single flight, count-only, hidden/member safe and keeps count on failure", async () => {
  let resolve;
  const h = harness([], { request: () => new Promise(r => { resolve = r; }) });
  const approvals = h.context.views.approvals;
  await approvals.refresh();
  assert.equal(h.requests.length, 0);
  h.context.capability = () => true;
  h.context.document.hidden = true;
  await approvals.refresh();
  assert.equal(h.requests.length, 0);
  h.context.document.hidden = false;
  const first = approvals.refresh();
  assert.equal(approvals.refresh(), first);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, "/api/admin/users?summary=1");
  resolve({ pending_count: 2 });
  await first;
  h.context.api.request = () => Promise.reject(new Error("offline"));
  await approvals.refresh();
  assert.equal(h.get("settingsPendingBadge").textContent, "2");
  assert.equal(h.get("settingsPendingBadge").hidden, false);
});

test("approval success clears badge even if reloading users fails; stale poll cannot resurrect it", async () => {
  let resolve;
  const h = harness(["account"], { request: url => {
    if (url.includes("summary=1")) return new Promise(r => { resolve = r; });
    if (url.endsWith("/approve")) return Promise.resolve({ pending_count: 0 });
    return Promise.reject(new Error("list unavailable"));
  } });
  h.context.capability = () => true;
  h.context.views.approvals.render(1);
  const stale = h.context.views.approvals.refresh();
  await h.context.views.account.act("2", "approve", h.get("approve"));
  assert.equal(h.get("settingsPendingBadge").hidden, true);
  assert.equal(h.get("usersNavPendingBadge").hidden, true);
  resolve({ pending_count: 1 });
  await stale;
  assert.equal(h.get("settingsPendingBadge").hidden, true);
  assert.equal(h.get("usersNavPendingBadge").hidden, true);
});

test("approval timer starts once, pauses off-page and refreshes on return", async () => {
  const h = harness([], { request: () => Promise.resolve({ pending_count: 1 }) });
  const events = {}, intervals = [], cleared = [];
  h.context.capability = () => true;
  h.context.document.addEventListener = (name, callback) => { events[name] = callback; };
  h.context.window.addEventListener = (name, callback) => { events[name] = callback; };
  h.context.setInterval = (callback, ms) => { intervals.push({ callback, ms }); return intervals.length; };
  h.context.clearInterval = id => cleared.push(id);
  const approvals = h.context.views.approvals;
  await approvals.start();
  await approvals.start();
  assert.equal(intervals.length, 1);
  assert.equal(intervals[0].ms, 30000);
  const before = h.requests.length;
  h.context.document.hidden = true;
  intervals[0].callback();
  assert.equal(h.requests.length, before);
  h.context.document.hidden = false;
  events.visibilitychange();
  await settle();
  assert.equal(h.requests.length, before + 1);
  events.pagehide();
  assert.deepEqual(cleared, [1]);
  events.pageshow({ persisted: true });
  await settle();
  assert.equal(intervals.length, 2);
});

test("a slow user list cannot overwrite a newer poll count but still renders the list", async () => {
  const resolvers = {};
  const h = harness(["account"], { request: url => new Promise(resolve => { resolvers[url] = resolve; }) });
  h.context.capability = () => true;
  const list = h.context.views.account.loadUsers();
  const poll = h.context.views.approvals.refresh();
  resolvers["/api/admin/users?summary=1"]({ pending_count: 2 });
  await poll;
  resolvers["/api/admin/users"]({ pending_count: 1, users: [{ id: 2, email_display: "member@example.test", status: "pending", role: "member" }] });
  await list;
  assert.equal(h.get("settingsPendingBadge").textContent, "2");
  assert.match(h.get("usersList").innerHTML, /member@example.test/);
});

test("reauthorization stays clickable without a device app and preserves the existing connection", () => {
  const h = harness(["my115"]);
  h.context.views.my115.render({ transfer: { state: "connected" }, browse: {
    state: "connected", label: "已授权", blocked_reason: "blocked_by_115_flow_verification",
  } });
  assert.equal(h.get("my115Summary").textContent, "两项已连接");
  assert.equal(h.get("my115BrowseConnect").disabled, false);
  assert.equal(h.get("my115BrowseDisconnect").hidden, false);
  assert.equal(h.get("my115TransferConnect").textContent, "重新扫码");
  assert.equal(h.get("my115BrowseConnect").textContent, "重新扫码");
  const template = fs.readFileSync(path.join(__dirname, "../templates/index.html"), "utf8");
  for (const id of ["my115TransferConnect", "my115BrowseConnect"]) {
    const button = template.match(new RegExp('<button[^>]*id="' + id + '"[^>]*>重新扫码</button>'));
    assert.ok(button);
    assert.doesNotMatch(button[0], /secondary/);
  }
});

function fillSettings(h) {
  h.get("settingPid").value = " 115-directory ";
  h.get("cookie115").value = " UID=fixture-cookie ";
  h.get("tmdbKey").value = " tmdb-draft ";
  h.get("tmdbBudget").value = " 250 ";
  h.get("tmdbEnrichEnabled").checked = true;
  h.context.state.tmdbEnrichDirty = true;
  h.context.state.linkcheckDirty = true;
  h.context.views.settings.init();
}

test("115 save only persists its card and preserves unsaved TMDB/link-check edits", async () => {
  const h = harness(["settings"]);
  fillSettings(h);
  h.get("settingPid").dispatch("input");
  h.get("settingsSave").dispatch("click");
  await settle();

  assert.deepEqual(h.requests, [{
    url: "/api/settings", method: "POST",
    body: { "115_target_pid": "115-directory", "115_cookie": "UID=fixture-cookie" },
  }]);
  assert.equal(h.get("cookie115").value, "");
  assert.equal(h.get("tmdbKey").value, " tmdb-draft ");
  assert.equal(h.get("tmdbBudget").value, " 250 ");
  assert.equal(h.context.state.tmdbEnrichDirty, true);
  assert.equal(h.context.state.linkcheckDirty, true);
});

test("Cookie-only save does not clear an undisclosed destination PID", async () => {
  const h = harness(["settings"]);
  h.context.views.settings.init();
  h.get("cookie115").value = "UID=fixture-cookie";
  h.get("settingsSave").dispatch("click");
  await settle();
  assert.deepEqual(h.requests[0].body, { "115_cookie": "UID=fixture-cookie" });
});

test("explicit PID clear is sent, and an in-flight newer edit remains dirty", async () => {
  let resolve;
  const h = harness(["settings"], { request: () => new Promise(r => { resolve = r; }) });
  h.context.views.settings.init();
  h.get("settingPid").value = "";
  h.get("settingPid").dispatch("input");
  h.get("settingsSave").dispatch("click");
  assert.deepEqual(h.requests[0].body, { "115_target_pid": "" });
  h.get("settingPid").value = "new-directory";
  h.get("settingPid").dispatch("input");
  resolve({});
  await settle();
  assert.equal(h.context.state.targetPidDirty, true);
  h.get("settingsSave").dispatch("click");
  assert.equal(h.requests[1].body["115_target_pid"], "new-directory");
  resolve({});
  await settle();
  assert.equal(h.context.state.targetPidDirty, false);
});

test("a failed PID save preserves the edit for retry", async () => {
  const h = harness(["settings"], { request: () => Promise.reject(new Error("fixture-error")) });
  h.context.views.settings.init();
  h.get("settingPid").value = "directory";
  h.get("settingPid").dispatch("input");
  h.get("settingsSave").dispatch("click");
  await settle();
  assert.equal(h.context.state.targetPidDirty, true);
});

test("TMDB save only persists TMDB and leaves the 115 and link-check drafts intact", async () => {
  const h = harness(["settings"], { request: () => Promise.resolve({ tmdb: { configured_budget: 240 } }) });
  fillSettings(h);
  assert.equal(typeof h.get("tmdbSave").onclick, "function", "TMDB has its own save handler");
  h.get("tmdbSave").dispatch("click");
  await settle();

  assert.deepEqual(h.requests, [{
    url: "/api/settings", method: "POST",
    body: { tmdb_api_key: "tmdb-draft", tmdb_daily_budget: 250, tmdb_enrich_enabled: true },
  }]);
  assert.equal(h.get("cookie115").value, " UID=fixture-cookie ");
  assert.equal(h.get("settingPid").value, " 115-directory ");
  assert.equal(h.get("tmdbKey").value, "");
  assert.equal(String(h.get("tmdbBudget").value), "240");
  assert.equal(h.context.state.tmdbEnrichDirty, false);
  assert.equal(h.context.state.linkcheckDirty, true);
});

test("an untouched TMDB switch is not accidentally persisted before status loads", async () => {
  const h = harness(["settings"]);
  h.context.views.settings.init();
  h.get("tmdbBudget").value = "150";
  h.get("tmdbEnrichEnabled").checked = false;
  h.get("tmdbSave").dispatch("click");
  await settle();
  assert.deepEqual(h.requests, [{ url: "/api/settings", method: "POST", body: { tmdb_daily_budget: 150 } }]);
});

test("policy changes remain a draft until save; a failed save preserves the choice for retry", async () => {
  let rejectSave;
  let fail = true;
  const h = harness(["account"], { request: () => fail
    ? new Promise((resolve, reject) => { rejectSave = reject; })
    : Promise.resolve({}) });
  h.context.views.account.init();
  h.context.views.account.render();
  const toggle = h.get("policyMemberUnlock");
  const save = h.get("policySave");
  toggle.checked = true;
  toggle.dispatch("change");
  assert.equal(h.requests.length, 0, "switch changes must not PATCH immediately");

  h.context.views.account.render();
  assert.equal(toggle.checked, true, "an account refresh must retain the unsaved choice");
  save.dispatch("click");
  assert.deepEqual(h.requests, [{
    url: "/api/admin/policies/re0", method: "PATCH", body: { allow_member_re0_unlock: true },
  }]);
  assert.equal(save.disabled, true, "prevent duplicate submissions while saving");
  rejectSave(new Error("network unavailable"));
  await settle();
  assert.equal(toggle.checked, true);
  assert.equal(h.context.state.me.allow_member_re0_unlock, false, "failed save must not claim server success");
  assert.equal(save.disabled, false, "retry remains available");
  assert.ok(h.feedbacks.some(entry => entry[0] === "policyResult" && entry[2] === "error"));

  fail = false;
  save.dispatch("click");
  await settle();
  assert.equal(h.requests.length, 2);
  assert.equal(h.context.state.me.allow_member_re0_unlock, true);
  assert.equal(toggle.checked, true);
  assert.equal(save.disabled, true, "successful save has no remaining draft to submit");
});

function protectedPanel(h, id, dataset) {
  const panel = h.add(id, dataset);
  Object.defineProperty(panel, "innerHTML", {
    get: () => "<input value=\"unsaved\">",
    set: () => assert.fail("navigation must not rebuild existing panel DOM"),
  });
  panel.replaceChildren = () => assert.fail("navigation must not replace panel children");
  panel.remove = () => assert.fail("navigation must not remove panel DOM");
  return panel;
}

test("section and service navigation hide existing panels and retain draft inputs", () => {
  const h = harness(["settingsNav"]);
  const personal = h.add("personalTab", { settingsSection: "personal" });
  const services = h.add("servicesTab", { settingsSection: "services" });
  const service115 = h.add("service115Tab", { settingsService: "115" });
  const serviceTmdb = h.add("serviceTmdbTab", { settingsService: "tmdb" });
  const personalPanel = protectedPanel(h, "personalPanel", { settingsPanel: "personal" });
  const servicesPanel = protectedPanel(h, "servicesPanel", { settingsPanel: "services" });
  const panel115 = protectedPanel(h, "panel115", { servicePanel: "115" });
  const panelTmdb = protectedPanel(h, "panelTmdb", { servicePanel: "tmdb" });
  const cookie = h.get("cookie115");
  const key = h.get("tmdbKey");
  panel115.children.push(cookie);
  panelTmdb.children.push(key);
  cookie.value = "unsaved cookie";
  key.value = "unsaved TMDB key";

  h.context.views.settingsNav.init();
  personal.dispatch("click");
  assert.equal(personalPanel.hidden, false);
  assert.equal(servicesPanel.hidden, true);
  services.dispatch("click");
  assert.equal(personalPanel.hidden, true);
  assert.equal(servicesPanel.hidden, false);
  service115.dispatch("click");
  assert.equal(panel115.hidden, false);
  assert.equal(panelTmdb.hidden, true);
  serviceTmdb.dispatch("click");
  assert.equal(panel115.hidden, true);
  assert.equal(panelTmdb.hidden, false);
  personal.dispatch("click");
  services.dispatch("click");
  service115.dispatch("click");
  assert.equal(panel115.children[0], cookie, "keep the same input DOM node");
  assert.equal(panelTmdb.children[0], key, "keep the same input DOM node");
  assert.equal(cookie.value, "unsaved cookie");
  assert.equal(key.value, "unsaved TMDB key");
  assert.equal(h.requests.length, 0, "navigation has no write requests");
});

test("My 115 summary derives capabilities from step states even if the API summary is misleading", () => {
  const h = harness(["my115"]);
  const summaries = new Map();
  for (const [transfer, browse] of [
    ["connected", "connected"], ["connected", "blocked"],
    ["unconfigured", "connected"], ["unconfigured", "unconfigured"],
  ]) {
    h.context.views.my115.render({
      summary: "MISLEADING_SERVER_SUMMARY",
      transfer: { state: transfer, label: transfer === "connected" ? "已连接" : "待连接" },
      browse: { state: browse, label: browse === "connected" ? "已连接" : "待授权" },
    });
    const summary = h.get("my115Summary").textContent;
    assert.ok(summary);
    assert.doesNotMatch(summary, /MISLEADING_SERVER_SUMMARY/);
    summaries.set(transfer + "/" + browse, summary);
    assert.equal(h.get("my115TransferDisconnect").hidden, transfer !== "connected");
    assert.equal(h.get("my115BrowseDisconnect").hidden, browse !== "connected");
    assert.equal(h.get("my115BrowseConnect").disabled, false);
  }
  assert.equal(new Set(summaries.values()).size, 4, "summary distinguishes both connections and each partial state");
  h.context.views.my115.render(null);
  assert.equal(h.get("my115Summary").textContent, "状态读取失败");
  assert.equal(h.get("my115TransferState").textContent, "状态读取失败");
});

test("the header omits the authentication badge without removing account diagnostics", () => {
  const template = fs.readFileSync(path.join(__dirname, "../templates/index.html"), "utf8");
  assert.doesNotMatch(template, /id="actor"|class="access-badge"/);
  assert.doesNotMatch(source, /\$\("actor"\)/);
  assert.match(viewSource("account"), /站点登录方式/);
});

test("authentication labels distinguish hybrid, account, Access, and local modes", () => {
  const context = {};
  vm.createContext(context);
  vm.runInContext(functionSource("authModeLabel"), context);
  const labels = ["hybrid", "account", "access", "local"].map(mode => context.authModeLabel(mode));
  labels.forEach(label => assert.ok(typeof label === "string" && label.length > 0));
  assert.equal(new Set(labels).size, 4);
  assert.match(labels[0], /Access/);
  assert.match(labels[0], /账号|账户/);
  assert.match(labels[1], /账号|账户/);
  assert.match(labels[2], /Access/);
  assert.match(labels[3], /本地/);
  labels.slice(0, 3).forEach(label => assert.doesNotMatch(label, /本地测试/));
  assert.equal(context.authModeLabel("app"), context.authModeLabel("account"));
});

test("keyboard navigation selects and focuses the next section without writes", () => {
  const h = harness(["settingsNav"]);
  const first = h.add("first", { settingsSection: "my115" });
  const second = h.add("second", { settingsSection: "account" });
  const firstPanel = protectedPanel(h, "firstPanel", { settingsPanel: "my115" });
  const secondPanel = protectedPanel(h, "secondPanel", { settingsPanel: "account" });
  h.context.views.settingsNav.init();
  first.dispatch("keydown", { key: "ArrowDown" });
  assert.equal(second.focused, true);
  assert.equal(secondPanel.hidden, false);
  assert.equal(firstPanel.hidden, true);
  second.dispatch("keydown", { key: "Home" });
  assert.equal(first.focused, true);
  assert.equal(firstPanel.hidden, false);
  assert.equal(secondPanel.hidden, true);
  first.dispatch("keydown", { key: "End" });
  assert.equal(secondPanel.hidden, false);
  assert.equal(h.requests.length, 0);
});

test("input events mark drafts before blur and saves preserve newer in-flight edits", async () => {
  let complete;
  const h = harness(["settings"], { request: () => new Promise(resolve => { complete = resolve; }) });
  const form = h.add("tmdbForm", { settingsForm: "tmdb" });
  h.context.views.settings.init();
  h.context.views.settingsNav.init();
  h.get("tmdbBudget").value = "200";
  h.get("tmdbKey").value = "first-key";
  form.dispatch("input");
  assert.equal(h.context.state.tmdbSettingsDirty, true);
  assert.equal(h.get("settingsDraftNotice").hidden, false);
  h.get("tmdbSave").dispatch("click");
  h.get("tmdbBudget").value = "300";
  h.get("tmdbKey").value = "new-draft-key";
  form.dispatch("input");
  complete({ tmdb: { configured_budget: 200 } });
  await settle();
  assert.equal(h.get("tmdbBudget").value, "300");
  assert.equal(h.get("tmdbKey").value, "new-draft-key");
  assert.equal(h.context.state.tmdbSettingsDirty, true);
  assert.equal(h.get("settingsDraftNotice").hidden, false);
  assert.equal(h.get("tmdbSave").disabled, false);
});

test("background TMDB status updates diagnosis while preserving unsaved checkbox and budget", async () => {
  const h = harness(["settingsNav"], { request: () => Promise.resolve({
    installed: true, configured_budget: 100, enrich_enabled: false,
  }) });
  h.context.WORKER_STATE_LABEL = { not_started: "未启动" };
  h.context.renderStatusList = () => {};
  let rendered;
  h.context.renderTmdbScrapeStatus = result => { rendered = result; };
  h.context.setEmpty = () => {};
  vm.runInContext(functionSource("refreshLibraryStatus"), h.context);
  h.context.views.settingsNav.changed("tmdb");
  h.context.state.tmdbEnrichDirty = true;
  h.get("tmdbBudget").value = "300";
  h.get("tmdbEnrichEnabled").checked = true;
  await h.context.refreshLibraryStatus();
  assert.equal(rendered.configured_budget, 100);
  assert.equal(h.get("tmdbBudget").value, "300");
  assert.equal(h.get("tmdbEnrichEnabled").checked, true);
});

test("invalid numerical fields show an error without persisting or claiming success", () => {
  const h = harness(["settings"]);
  const form = h.add("tmdbForm", { settingsForm: "tmdb" });
  const budget = h.get("tmdbBudget");
  let reported = false;
  budget.checkValidity = () => false;
  budget.reportValidity = () => { reported = true; };
  form.querySelectorAll = selector => selector === "input" ? [budget] : [];
  h.context.views.settings.init();
  h.get("tmdbSave").dispatch("click");
  assert.equal(h.requests.length, 0);
  assert.equal(reported, true);
  assert.equal(h.get("tmdbSave").disabled, false);
  assert.ok(h.feedbacks.some(entry => entry[0] === "tmdbResult" && entry[2] === "error"));
  assert.ok(h.feedbacks.every(entry => entry[2] !== "success"));
});

test("a superseded TMDB GET cannot undo a saved budget or replace current status with an error", async () => {
  for (const staleFails of [false, true]) {
    const pending = [];
    const h = harness(["settings"], { request: (url, init) => {
      if (init.method === "POST") return Promise.resolve({ tmdb: { configured_budget: 200 } });
      return new Promise((resolve, reject) => pending.push({ resolve, reject }));
    } });
    h.context.WORKER_STATE_LABEL = { not_started: "未启动" };
    h.context.renderStatusList = () => {};
    h.context.renderTmdbScrapeStatus = () => {};
    let errors = 0;
    h.context.setEmpty = () => { errors++; };
    vm.runInContext(functionSource("refreshLibraryStatus"), h.context);
    h.context.views.settings.init();
    const stale = h.context.refreshLibraryStatus();
    h.get("tmdbBudget").value = "200";
    h.context.views.settingsNav.changed("tmdb");
    h.get("tmdbSave").dispatch("click");
    await settle();
    pending[1].resolve({ installed: true, configured_budget: 200, enrich_enabled: true });
    await settle();
    if (staleFails) pending[0].reject(new Error("old request failed"));
    else pending[0].resolve({ installed: true, configured_budget: 100, enrich_enabled: false });
    await stale;
    assert.equal(String(h.get("tmdbBudget").value), "200");
    assert.equal(h.get("tmdbEnrichEnabled").checked, true);
    assert.equal(h.context.state.tmdbSettingsDirty, false);
    assert.equal(errors, 0);
    h.get("tmdbKey").value = "new-key";
    h.get("tmdbSave").dispatch("click");
    assert.equal(h.requests.filter(request => request.method === "POST")[1].body.tmdb_daily_budget, 200);
  }
});

test("RE0, cloud and link-check refreshes ignore older success and failure responses", async () => {
  for (const [refresh, render] of [
    ["refreshRe0SyncStatus", "renderRe0SyncStatus"],
    ["refreshCloudStatus", "renderCloudStatus"],
    ["refreshLinkcheckStatus", "renderLinkcheckStatus"],
  ]) {
    for (const staleFails of [false, true]) {
      const pending = [];
      const h = harness([], { request: () => new Promise((resolve, reject) => pending.push({ resolve, reject })) });
      const rendered = [];
      h.context[render] = value => rendered.push(value);
      vm.runInContext(functionSource(refresh), h.context);
      const older = h.context[refresh]();
      const newer = h.context[refresh]();
      pending[1].resolve({ current: true });
      await newer;
      if (staleFails) pending[0].reject(new Error("old request failed"));
      else pending[0].resolve({ current: false });
      await older;
      assert.deepEqual(rendered, [{ current: true }], refresh);
    }
  }
});

test("cloud save persists only its three keys, refreshes status, and retains unrelated drafts", async () => {
  const h = harness(["settings"]);
  vm.runInContext(functionSource("cloudSavePayload"), h.context);
  h.context.views.settings.init();
  h.context.views.settingsNav.changed("cloud");
  h.context.views.settingsNav.changed("tmdb");
  h.context.views.settingsNav.changed("linkcheck");
  h.get("cloudEnabled").checked = true;
  h.get("cloudDailyCap").value = "50";
  h.get("cloudPerSubmitCap").value = "12";
  h.get("tmdbKey").value = "retained-key";
  h.get("cloudSave").dispatch("click");
  await settle();
  assert.deepEqual(h.requests, [{ url: "/api/settings", method: "POST", body: {
    cloud_download_enabled: true, cloud_download_daily_cap: 50, cloud_download_per_submit_cap: 12,
  } }]);
  assert.equal(h.context.state.cloudDirty, false);
  assert.equal(h.context.state.tmdbSettingsDirty, true);
  assert.equal(h.context.state.linkcheckDirty, true);
  assert.equal(h.get("tmdbKey").value, "retained-key");
  assert.deepEqual(h.refreshes, ["refreshCloudStatus"]);
  assert.equal(h.get("cloudSave").disabled, false);
});

test("policy controls wait for initial account load and never save an untouched default", async () => {
  let completeLoad;
  const h = harness(["account"], { request: () => new Promise(resolve => { completeLoad = resolve; }) });
  h.context.state.me = null;
  h.context.views.account.init();
  const load = h.context.views.account.load();
  assert.equal(h.get("policyMemberUnlock").disabled, true);
  assert.equal(h.get("policySave").disabled, true);
  h.get("policySave").dispatch("click");
  await h.context.views.account.savePolicy(false);
  assert.deepEqual(h.requests, [{ url: "/api/me", method: "GET", body: undefined }]);
  completeLoad({ role: "admin", allow_member_re0_unlock: true });
  await load;
  assert.equal(h.get("policyMemberUnlock").checked, true);
  assert.equal(h.get("policyMemberUnlock").disabled, false);
  assert.equal(h.get("policySave").disabled, true);
  h.get("policySave").dispatch("click");
  assert.equal(h.requests.length, 1, "an untouched loaded policy is not a draft");
});

test("initial account-load failure keeps policy controls disabled and sends no PATCH", async () => {
  const h = harness(["account"], { request: () => Promise.reject(new Error("cannot read account")) });
  h.context.state.me = null;
  h.context.views.account.init();
  await h.context.views.account.load();
  h.get("policySave").dispatch("click");
  assert.equal(h.context.state.policyLoaded, false);
  assert.equal(h.get("policySave").disabled, true);
  assert.equal(h.get("policyMemberUnlock").disabled, true);
  assert.equal(h.requests.length, 1);
  assert.ok(h.feedbacks.some(entry => entry[0] === "policyResult" && entry[2] === "error"));
});

test("an older account GET success or failure cannot overwrite an acknowledged policy save", async () => {
  for (const staleFails of [false, true]) {
    let resolveLoad, rejectLoad;
    const h = harness(["account"], { request: url => url === "/api/me"
      ? new Promise((resolve, reject) => { resolveLoad = resolve; rejectLoad = reject; })
      : Promise.resolve({}) });
    h.context.views.account.init();
    h.context.views.account.render();
    const older = h.context.views.account.load();
    h.get("policyMemberUnlock").checked = true;
    h.get("policyMemberUnlock").dispatch("change");
    h.get("policySave").dispatch("click");
    await settle();
    if (staleFails) rejectLoad(new Error("older request failed"));
    else resolveLoad({ role: "admin", allow_member_re0_unlock: false });
    await older;
    assert.equal(h.context.state.me.allow_member_re0_unlock, true);
    assert.equal(h.get("policyMemberUnlock").checked, true);
    assert.equal(h.context.state.policyLoaded, true);
    assert.equal(h.get("policySave").disabled, true);
    assert.equal(h.requests.filter(request => request.method === "PATCH").length, 1);
  }
});

test("a policy save preserves a newer draft even when an input event arrives during the request", async () => {
  let completeSave;
  const h = harness(["account"], { request: () => new Promise(resolve => { completeSave = resolve; }) });
  const form = h.add("policyForm", { settingsForm: "policy" });
  h.context.views.settingsNav.init();
  h.context.views.account.init();
  h.context.views.account.render();
  const toggle = h.get("policyMemberUnlock");
  toggle.checked = true;
  toggle.dispatch("change");
  form.dispatch("input");
  h.get("policySave").dispatch("click");
  assert.equal(toggle.disabled, true);
  toggle.checked = false;
  form.dispatch("input");
  completeSave({});
  await settle();
  assert.equal(h.context.state.me.allow_member_re0_unlock, true);
  assert.equal(toggle.checked, false);
  assert.equal(h.context.state.policyDirty, true);
  assert.equal(h.get("policySave").disabled, false);
  assert.equal(toggle.disabled, false);
});
