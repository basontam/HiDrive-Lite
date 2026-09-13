"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const qrcode = require("../static/vendor/qrcode.js");
const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const start = source.indexOf("  state.open115 = {");
const end = source.indexOf("\n  views.my115 = {", start);
assert.ok(start > 0 && end > start);
const settle = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
const generated = id => ({ flow: "device_pkce", challenge_id: id, qrcode: "115-qr-fixture", expires_in: 600 });

function harness(request) {
  const elements = new Map(), calls = [], feedbacks = [], timers = new Map(), intervals = new Map(), rectangles = [];
  let next = 1, now = 100000, refreshed = 0, reloaded = 0;
  const document = { activeElement: null, body: { classList: { add() {}, remove() {} } }, addEventListener() {}, querySelector: () => get("backdrop") };
  function get(id) {
    if (!elements.has(id)) elements.set(id, { hidden: id === "open115Dialog", disabled: false, textContent: "", width: 0, height: 0,
      focus() { document.activeElement = this; }, getContext() { return { fillRect: (...args) => rectangles.push(args) }; } });
    return elements.get(id);
  }
  const context = {
    state: {}, views: { reauth: { afterAuthenticated: () => { refreshed++; return Promise.resolve(); }, needsReload: () => false, reloadForNewCapability: () => { reloaded++; } } },
    $: get, document, qrcode, Date: { now: () => now }, capability: () => false,
    window: { location: { reload: () => { reloaded++; } } },
    setTimeout: (fn, delay) => { const id = next++; timers.set(id, { fn, delay }); return id; }, clearTimeout: id => timers.delete(id),
    setInterval: fn => { const id = next++; intervals.set(id, fn); return id; }, clearInterval: id => intervals.delete(id),
    feedback: (...args) => feedbacks.push(args),
    api: { request: (url, init) => { const body = JSON.parse(init.body); calls.push({ url, body, method: init.method }); return request(url, body); } },
  };
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  return { context, view: context.views.open115, get, calls, feedbacks, timers, intervals, rectangles, clock: time => { now = time; }, refreshed: () => refreshed, reloaded: () => reloaded };
}

test("step B renders a local QR, polls only opaque IDs, and stores no browser tokens", async () => {
  const h = harness(url => Promise.resolve(url.endsWith("/start") ? generated("opaque-fixture") : { status: "pending" }));
  await h.view.open(); await settle();
  assert.equal(h.get("open115Qr").hidden, false);
  assert.ok(h.rectangles.length > 50);
  const size = h.get("open115Qr").width;
  for (const [x, y, w, height] of h.rectangles.slice(1)) {
    assert.ok(x >= 24 && y >= 24 && x + w <= size - 24 && y + height <= size - 24);
  }
  assert.deepEqual(h.calls.map(c => c.method), ["POST", "POST"]);
  assert.deepEqual(h.calls[1].body, { challenge_id: "opaque-fixture" });
  assert.equal(h.timers.size, 1); assert.equal(h.intervals.size, 1);
  assert.doesNotMatch(source.slice(start, end), /localStorage|sessionStorage|authorize_url|location\.href/);
});

test("scanned then connected refreshes own capabilities and stops polling", async () => {
  let status = "scanned";
  const h = harness(url => Promise.resolve(url.endsWith("/start") ? generated("opaque-fixture") : { status }));
  await h.view.open(); await settle();
  assert.match(h.feedbacks.at(-1)[1], /已扫码/);
  status = "connected";
  await h.view.poll(h.context.state.open115.generation);
  assert.equal(h.get("open115Qr").hidden, true);
  assert.equal(h.context.state.open115.challengeId, null);
  assert.equal(h.refreshed(), 1);
  assert.equal(h.timers.size, 0); assert.equal(h.intervals.size, 0);
  h.view.close(); await settle();
  assert.equal(h.calls.filter(c => c.url.endsWith("/cancel")).length, 0);
});

test("close while start is in flight cancels the returned challenge without reopening", async () => {
  const pending = deferred();
  const h = harness(url => url.endsWith("/start") ? pending.promise : Promise.resolve({ status: "cancelled" }));
  const opening = h.view.open(); await settle(); h.view.close();
  pending.resolve(generated("late-fixture")); await opening;
  assert.equal(h.get("open115Dialog").hidden, true);
  assert.equal(h.get("open115Qr").hidden, true);
  assert.equal(h.calls.at(-1).url, "/api/me/115/open/cancel");
  assert.equal(h.calls.at(-1).body.challenge_id, "late-fixture");
  assert.equal(h.timers.size, 0);
});

test("old poll success after close does not refresh or resurrect the dialog", async () => {
  const pending = deferred();
  const h = harness(url => url.endsWith("/start") ? Promise.resolve(generated("old-fixture")) : url.endsWith("/status") ? pending.promise : Promise.resolve({}));
  await h.view.open(); h.view.close(); pending.resolve({ status: "connected" }); await settle();
  assert.equal(h.refreshed(), 0); assert.equal(h.timers.size, 0);
});

test("close reconciles a token commit that won before cancellation without reopening", async () => {
  const pending = deferred();
  const h = harness(url => url.endsWith("/start") ? Promise.resolve(generated("commit-won-fixture")) : url.endsWith("/status") ? pending.promise : Promise.resolve({ status: "connected" }));
  await h.view.open(); h.view.close(); pending.resolve({ status: "connected" }); await settle();
  assert.equal(h.refreshed(), 1); assert.equal(h.get("open115Dialog").hidden, true);
  assert.equal(h.get("open115Qr").hidden, true); assert.equal(h.timers.size, 0);
});

test("refresh cancels old attempt before starting another and discards late old status", async () => {
  const pending = deferred(); let starts = 0;
  const h = harness((url, body) => {
    if (url.endsWith("/start")) return Promise.resolve(generated("attempt-" + ++starts));
    if (url.endsWith("/cancel")) return Promise.resolve({});
    return body.challenge_id === "attempt-1" ? pending.promise : Promise.resolve({ status: "pending" });
  });
  await h.view.open(); await h.view.start(); pending.resolve({ status: "connected" }); await settle();
  assert.equal(h.context.state.open115.challengeId, "attempt-2"); assert.equal(h.refreshed(), 0);
  assert.deepEqual(h.calls.slice(2, 4).map(c => c.url.split("/").at(-1)), ["cancel", "start"]);
});

test("local expiry cancels and fences a delayed response", async () => {
  const pending = deferred();
  const h = harness(url => url.endsWith("/start") ? Promise.resolve(generated("expiry-fixture")) : url.endsWith("/status") ? pending.promise : Promise.resolve({}));
  await h.view.open(); h.clock(701000); h.view.tick(h.context.state.open115.generation);
  pending.resolve({ status: "connected" }); await settle();
  assert.equal(h.refreshed(), 0); assert.equal(h.timers.size, 0); assert.equal(h.intervals.size, 0);
  assert.match(h.feedbacks.at(-1)[1], /过期/);
});

for (const status of ["failed", "expired", "cancelled"]) test("terminal " + status + " clears the QR and polling", async () => {
  const h = harness(url => Promise.resolve(url.endsWith("/start") ? generated("terminal-fixture") : { status }));
  await h.view.open(); await settle();
  assert.equal(h.get("open115Qr").hidden, true); assert.equal(h.timers.size, 0); assert.equal(h.intervals.size, 0);
  assert.equal(h.get("open115Refresh").disabled, false);
});

test("network status failure retries slowly without claiming invalid credentials", async () => {
  const h = harness(url => url.endsWith("/start") ? Promise.resolve(generated("network-fixture")) : Promise.reject(new Error("fixture")));
  await h.view.open(); await settle();
  assert.equal([...h.timers.values()][0].delay, 5000);
  assert.match(h.feedbacks.at(-1)[1], /正在重试/);
  assert.equal(h.get("open115Qr").hidden, false);
});

test("invalid start does not redirect to a token-export page or claim success", async () => {
  const h = harness(() => Promise.resolve({ flow: "authorization_code", authorize_url: "https://example.test/fixture" }));
  await h.view.open();
  assert.equal(h.get("open115Qr").hidden, true);
  assert.match(h.feedbacks.at(-1)[1], /获取失败/);
  assert.equal(h.calls.length, 1);
});

test("missing QR encoder cancels the generated attempt", async () => {
  const h = harness(url => Promise.resolve(url.endsWith("/start") ? generated("encoder-fixture") : {}));
  h.context.qrcode = undefined; await h.view.open(); await settle();
  assert.equal(h.calls.at(-1).url, "/api/me/115/open/cancel");
  assert.equal(h.get("open115Qr").hidden, true); assert.equal(h.timers.size, 0);
});

test("disabling focused Refresh moves focus to an enabled modal control", async () => {
  const h = harness(url => Promise.resolve(url.endsWith("/start") ? generated("focus-fixture") : { status: "pending" }));
  await h.view.open(); h.get("open115Refresh").focus(); const refreshing = h.view.start();
  assert.equal(h.context.document.activeElement, h.get("open115Close"));
  await refreshing; h.get("open115Refresh").focus(); h.view.finish("fixture-success", true);
  assert.equal(h.context.document.activeElement, h.get("open115Cancel"));
});

test("QR encoder handles UTF-8 payloads without HTML or URL navigation", () => {
  const h = harness(() => Promise.resolve({}));
  h.view.drawQr("https://example.test/扫码?fixture=<script>");
  assert.ok(h.get("open115Qr").width > 0); assert.ok(h.rectangles.length > 0);
  assert.equal(h.calls.length, 0);
});

test("Cookie-connected member gains the missing cloud tab even if success dialog closes during refresh", async () => {
  const refresh = deferred();
  const h = harness(url => Promise.resolve(url.endsWith("/start") ? generated("new-capability-fixture") : { status: "connected" }));
  h.context.views.reauth.afterAuthenticated = () => refresh.promise;
  h.context.capability = name => name === "own_115_cloud_download";
  h.context.$ = id => ["cloud", "tab-cloud"].includes(id) ? null : h.get(id);
  await h.view.open(); await settle(); h.view.close(); refresh.resolve(); await settle();
  assert.equal(h.reloaded(), 1);
  h.view.reloadIfNeeded(); assert.equal(h.reloaded(), 1);
});
