"use strict";

// Run production library methods without a browser, server, or network.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

test("search is readonly while idle and editable on focus without changing text", () => {
  const template = fs.readFileSync(path.join(__dirname, "../templates/index.html"), "utf8");
  assert.match(template, /<input id="libraryQuery"[^>]*name="library-query"[^>]*readonly/);
  const listeners = {};
  const field = {readOnly: true, value: "typed query", addEventListener: (event, fn) => {listeners[event] = fn;}};
  const start = source.indexOf('      $("libraryQuery").addEventListener("focus"');
  const end = source.indexOf('      $("libraryQuery").addEventListener("keydown"', start);
  assert.ok(start > 0 && end > start);
  vm.runInNewContext(source.slice(start, end), {$: () => field});
  listeners.focus.call(field);
  assert.equal(field.readOnly, false);
  listeners.blur.call(field);
  assert.equal(field.readOnly, true);
  assert.equal(field.value, "typed query");
});

function harness() {
  const declarationsStart = source.indexOf("  state.library = {");
  const declarationsEnd = source.indexOf("\n  function cjkLatinOk", declarationsStart);
  const viewStart = source.indexOf("  views.library = {");
  const viewEnd = source.indexOf("\n  };", viewStart) + "\n  };".length;
  assert.ok(declarationsStart >= 0 && declarationsEnd > declarationsStart);
  assert.ok(viewStart >= 0 && viewEnd > viewStart);
  const events = [];
  const elements = new Map();
  function element(tagName) {
    return {
      tagName, children: [], attributes: {}, textContent: "", hidden: true,
      appendChild(child) { this.children.push(child); return child; },
      setAttribute(name, value) { this.attributes[name] = String(value); },
    };
  }
  const box = element("div");
  const searchCard = {
    scrollIntoView(options) { events.push(["scroll", options.block, options.behavior]); },
  };
  const query = {
    focus(options) { events.push(["focus", options.preventScroll]); },
    closest(selector) { assert.equal(selector, ".library-search-card"); return searchCard; },
  };
  box.querySelector = selector => {
    if (selector.endsWith(" h3")) return box.querySelector(selector.slice(0, -3)).querySelector("h3");
    if (!elements.has(selector)) {
      const heading = element("h3");
      const section = element("section");
      section.querySelector = child => child === "h3" ? heading : element("div");
      elements.set(selector, section);
    }
    return elements.get(selector);
  };
  const context = {
    state: {}, views: {}, URLSearchParams,
    document: { createElement: element },
    $: id => {
      if (id === "libraryQuery") return query;
      if (id === "libraryRails") return box;
      if (!elements.has(id)) elements.set(id, element("div"));
      return elements.get(id);
    },
    esc: text => String(text).replace(/[&<>"']/g, "_"),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(declarationsStart, declarationsEnd) + "\n" + source.slice(viewStart, viewEnd), context);
  const library = context.views.library;
  context.$("library").hidden = false;
  library.applyToForm = () => events.push("form");
  library.hideSuggest = () => events.push("suggest");
  library.closeFilters = restore => events.push(["filters", restore]);
  library.search = push => events.push(["search", push]);
  library.fetchChannel = () => Promise.resolve(null);
  return { ...context, context, library, events, box };
}

function heroHarness() {
  const h = harness();
  let now = 100000;
  let hovered = false;
  let reduced = false;
  const probes = [];
  function el() { return {dataset: {}, children: [], attributes: {}, hidden: false,
    style: {setProperty(key, value) { this[key] = value; }},
    appendChild(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
    setAttribute(key, value) { this.attributes[key] = value; },
    querySelectorAll() { return this.children; }}; }
  h.context.document = {createElement: el, visibilityState: 'visible', activeElement: null};
  h.context.Date = {now: () => now};
  const timers = new Map();
  h.context.setTimeout = fn => { const key = {}; timers.set(key, fn); return key; };
  h.context.clearTimeout = key => timers.delete(key);
  h.context.window = {matchMedia: () => ({matches: reduced})};
  h.context.MEDIA_TYPE_LABEL = {movie: '电影', tv: '剧集'};
  h.context.Image = function () {
    this.events = {};
    this.addEventListener = (name, callback) => { this.events[name] = callback; };
    probes.push(this);
  };
  const hero = h.$('libraryHero');
  const parts = {};
  hero.querySelector = selector => parts[selector] ||= el();
  hero.matches = () => hovered;
  hero.contains = active => active === hero && active !== null;
  Object.assign(h.$('heroThumbnails'), el());
  h.$('libraryHome').hidden = false;
  h.library.isBrowsing = () => true;
  h.library.openDetail = id => h.events.push(['detail', id]);
  const items = Array.from({length: 5}, (_, n) => ({media_id:n + 1, title:'电影' + (n + 1),
    media_type:'movie', backdrop_url:'https://fixture.test/backdrop-' + n,
    poster_url:'https://fixture.test/poster-' + n, overview_short:'简介' + n}));
  return {...h, hero, parts, probes, items, timers,
    start() { h.library.applyHeroCandidates(items, '2026-09-12'); probes.at(-1).events.load(); },
    advance(ms = 8000) { now += ms; h.library.advanceHero(1, false); },
    hover(value) { hovered = value; }, reduce(value) { reduced = value; }};
}

test('hero rotates five titles after eight seconds and CTA follows the displayed item', () => {
  const h = heroHarness(); h.start();
  assert.equal(h.$('heroThumbnails').children.length, 5);
  h.advance(7999); assert.equal(h.probes.length, 1);
  h.advance(1); h.probes.at(-1).events.load();
  assert.equal(h.state.library.hero.mediaId, 2);
  assert.equal(h.$('heroPosition').textContent, '2 / 5');
  h.parts['.library-hero-cta'].onclick();
  assert.deepEqual(h.events.at(-1), ['detail', 2]);
  for (let n = 0; n < 4; n++) { h.advance(); h.probes.at(-1).events.load(); }
  assert.equal(h.state.library.hero.mediaId, 1);
});

test('hero overlay frame follows the loaded artwork ratio and ignores stale image dimensions', () => {
  const h = heroHarness(); h.start();
  assert.equal(h.parts['.library-hero-frame'].style['--hero-image-ratio'], String(16 / 9));
  h.$('heroNext').onclick(); const stale = h.probes.at(-1);
  stale.naturalWidth = 1000; stale.naturalHeight = 1000;
  h.$('heroThumbnails').children[4].onclick(); const latest = h.probes.at(-1);
  latest.naturalWidth = 2400; latest.naturalHeight = 1000; latest.events.load();
  assert.equal(h.parts['.library-hero-frame'].style['--hero-image-ratio'], '2.4');
  stale.events.load();
  assert.equal(h.parts['.library-hero-frame'].style['--hero-image-ratio'], '2.4');
});

test('hero pauses for hover, focus, manual pause, reduced motion, hidden page and other views', () => {
  const h = heroHarness(); h.start();
  for (const [on, off] of [
    [() => h.hover(true), () => h.hover(false)],
    [() => h.reduce(true), () => h.reduce(false)],
    [() => {h.context.document.activeElement = h.hero;}, () => {h.context.document.activeElement = null;}],
    [() => h.$('heroPause').onclick(), () => h.$('heroPause').onclick()],
    [() => {h.context.document.visibilityState = 'hidden';}, () => {h.context.document.visibilityState = 'visible';}],
    [() => {h.$('library').hidden = true;}, () => {h.$('library').hidden = false;}],
    [() => {h.state.library.media = 9;}, () => {h.state.library.media = null;}],
  ]) { on(); h.advance(); assert.equal(h.probes.length, 1); off(); }
});

test('hero thumbnail selection and previous controls work while automatic playback is paused', () => {
  const h = heroHarness(); h.start(); h.$('heroPause').onclick();
  h.$('heroThumbnails').children[3].onclick(); h.probes.at(-1).events.load();
  assert.equal(h.state.library.hero.mediaId, 4);
  assert.equal(h.$('heroThumbnails').children[3].attributes['aria-pressed'], 'true');
  h.$('heroPrevious').onclick(); h.probes.at(-1).events.load();
  assert.equal(h.state.library.hero.mediaId, 3);
});

test('hero ignores an old image load after newer manual selection or navigation', () => {
  const h = heroHarness(); h.start();
  h.$('heroNext').onclick(); const stale = h.probes.at(-1);
  h.$('heroThumbnails').children[4].onclick(); h.probes.at(-1).events.load();
  stale.events.load(); assert.equal(h.state.library.hero.mediaId, 5);
  h.$('heroNext').onclick(); h.$('library').hidden = true;
  h.probes.at(-1).events.load(); assert.equal(h.state.library.hero.mediaId, 5);
});

test('broken carousel backdrops are skipped and disabled, not retried each rotation', () => {
  const h = heroHarness(); h.start();
  h.advance(); h.probes.at(-1).events.error(); h.probes.at(-1).events.load();
  assert.equal(h.state.library.hero.mediaId, 3);
  assert.equal(h.state.library.hero.items.length, 4);
  assert.equal(h.$('heroThumbnails').children[1].disabled, true);
});

test('same two-day edition keeps current title and empty edition clears stale hero', () => {
  const h = heroHarness(); h.start(); h.advance(); h.probes.at(-1).events.load();
  const thumbnail = h.$('heroThumbnails').children[1];
  h.library.applyHeroCandidates(h.items, '2026-09-12');
  assert.equal(h.$('heroThumbnails').children[1], thumbnail);
  assert.equal(h.probes.length, 2);
  assert.equal(h.state.library.hero.mediaId, 2);
  h.library.applyHeroCandidates([], '2026-09-14');
  assert.equal(h.hero.hidden, true);
  assert.equal(h.state.library.hero.mediaId, null);
});

test('pause during automatic preload prevents a late switch but manual selection still works', () => {
  const h = heroHarness(); h.start(); h.advance();
  h.$('heroPause').onclick(); h.probes.at(-1).events.load();
  assert.equal(h.state.library.hero.mediaId, 1);
  h.$('heroNext').onclick(); h.probes.at(-1).events.load();
  assert.equal(h.state.library.hero.mediaId, 2);
});

test('manual selection during a new edition preload uses the new edition date', () => {
  const h = heroHarness(); h.start();
  h.library.applyHeroCandidates(h.items.slice().reverse(), '2026-09-14');
  h.$('heroNext').onclick(); h.probes.at(-1).events.load();
  assert.equal(h.state.library.hero.date, '2026-09-14');
});

test('a reused thumbnail selects refreshed metadata rather than a stale captured item', () => {
  const h = heroHarness(); h.start();
  const button = h.$('heroThumbnails').children[1];
  const updated = h.items.map(item => ({...item, backdrop_url: item.backdrop_url + '-new'}));
  h.library.applyHeroCandidates(updated, '2026-09-12');
  assert.equal(h.$('heroThumbnails').children[1], button);
  button.onclick();
  assert.equal(h.probes.at(-1).src, updated[1].backdrop_url);
});

test('a stuck image has a bounded timeout and is not restarted every eight seconds', () => {
  const h = heroHarness(); h.start(); h.advance();
  h.advance(); assert.equal(h.probes.length, 2);
  [...h.timers.values()][0](); h.probes.at(-1).events.load();
  assert.equal(h.state.library.hero.mediaId, 3);
  assert.equal(h.timers.size, 0);
});

function searchHarness() {
  const h = harness();
  const requests = [];
  const rendered = [];
  h.state.library.q = "the missing";
  h.library.renderSkeleton = () => {};
  h.library.renderResults = data => rendered.push(data.total);
  h.library.fetchChannel = (channel, url) => new Promise((resolve, reject) => requests.push({channel, url, resolve, reject}));
  h.library.showState = state => rendered.push(state);
  h.library.setLibraryControlsDisabled = () => {};
  return {...h, requests, rendered};
}

test("explicit filters render once even when echoed by effective query", () => {
  const h = harness();
  h.providerLabel = value => value;
  // Globals are resolved in the VM context, not the returned object.
  const start = source.indexOf("    renderChips: function (interpreted)");
  const end = source.indexOf("\n    },", start) + 7;
  const box = {querySelectorAll: () => []};
  const context = {state: {library: {year: "2024", quality: ["2160p"], provider: ["115"]}},
    views: {library: {explicitChips: () => [{label: "2160p"}, {label: "年份 2024"}, {label: "115"}]}},
    $: () => box, esc: String, providerLabel: String};
  vm.runInNewContext("var render = {" + source.slice(start, end) + "}; render.renderChips({year:2024,quality:'2160p',providers:['115']});", context);
  assert.equal((box.innerHTML.match(/data-chip-index=/g) || []).length, 3);
});

test("reload clears transient filters but preserves category, order, page, tab and detail", () => {
  const start = source.indexOf("  var navigation = window.performance");
  const end = source.indexOf("  router.init();", start);
  const url = "https://fixture.test/?tab=settings&media=42&q=test&type=movie&year=2024&quality=2160p&provider=115&hdr=DV&genre=1&source=re0&deleted=1&sort=year&page=2";
  for (const type of ["reload", "navigate", "back_forward"]) {
    let result = url;
    const window = {location: {href: url}, performance: {getEntriesByType: () => [{type}]},
      history: {state: null, replaceState: (_state, _title, value) => {result = value;}}};
    vm.runInNewContext(source.slice(start, end), {window, URL});
    assert.equal(result, type === "reload" ? "https://fixture.test/?tab=settings&media=42&type=movie&sort=year&page=2" : url);
  }
  for (const category of ["movie", "tv", "unknown"]) {
    let result;
    const window = {location: {href: `https://fixture.test/?tab=library&type=${category}&sort=links_desc&page=3`},
      performance: {getEntriesByType: () => [{type:"reload"}]},
      history: {state:null, replaceState: (_, __, value) => {result=value;}}};
    vm.runInNewContext(source.slice(start,end), {window,URL});
    assert.equal(result,window.location.href);
  }
});

function gridHarness(columns) {
  const h = harness();
  h.state.library.type = "movie";
  h.$("library").hidden = false;
  const grid = h.$("libraryResults");
  grid.hidden = false; grid.clientWidth = 1400;
  // Methods are evaluated in the same VM: attach window via the returned view's
  // source in a fresh context rather than silently bypassing layout measurement.
  const start = source.indexOf("    syncPageSize: function");
  const end = source.indexOf("    runSearch: function", start);
  const context = {state:h.state, views:{library:h.library}, $:h.$,
    window:{getComputedStyle: () => ({gridTemplateColumns:Array(columns).fill("150px").join(" ")})}};
  vm.runInNewContext("Object.assign(views.library,{"+source.slice(start,end)+"});",context);
  return {...h,grid,context};
}

test("responsive page size is full rows within the 50-item API bound", () => {
  for (const columns of [2,3,5,7,8,10,11,12,14,16]) {
    const h = gridHarness(columns);
    h.state.library.page = 3;
    h.library.syncPageSize(false);
    assert.equal(h.state.library.page,3,"initial/reload keeps requested page");
    assert.equal(h.state.library.pageSize, columns*Math.min(5,Math.floor(50/columns)));
    assert.equal(h.state.library.pageSize%columns,0);
    assert.ok(h.state.library.pageSize<=50);
    assert.equal(Number(new URLSearchParams(h.library.buildQuery()).get("page_size")),h.state.library.pageSize);
  }
});

test("resize only reloads when full-row size changes and keeps first item in view", () => {
  const h = gridHarness(8);
  h.state.library.page = 3;
  h.library.resizeResults();
  assert.equal(h.state.library.pageSize,40);
  assert.equal(h.state.library.page,2); // old offset50 is in new page2 (40..79)
  assert.deepEqual(h.events,[["search",false]]);
  h.library.resizeResults();
  assert.equal(h.events.length,1);
  h.context.window.getComputedStyle = () => ({gridTemplateColumns:"150px 150px"});
  h.$("library").hidden=true;
  h.library.resizeResults();
  assert.equal(h.events.length,1);
  h.$("library").hidden=false; h.state.library.media="42";
  h.library.resizeResults();
  assert.equal(h.events.length,1);
});

test("search measures revealed grid before building either local or remote query", async () => {
  const h = searchHarness();
  h.library.renderSkeleton = () => {h.$("libraryResults").hidden=false;};
  h.library.syncPageSize = () => {
    assert.equal(h.$("libraryResults").hidden,false);
    h.state.library.pageSize=40;
  };
  const done=h.library.runSearch();
  assert.equal(h.requests.length,2);
  for(const request of h.requests) assert.equal(new URL(request.url,"https://fixture.test").searchParams.get("page_size"),"40");
  h.requests[0].resolve({total:1}); h.requests[1].resolve({catalog:{total:1},remote:{status:"fresh"}});
  await done;
});

test("out-of-range page moves to real last page without clearing category", () => {
  const h=harness();
  h.library.setLibraryControlsDisabled=()=>{};
  Object.assign(h.state.library,{type:"tv",page:10,pageSize:40});
  h.library.renderResults({total:81,page:10,page_size:40,items:[]});
  assert.equal(h.state.library.page,3);
  assert.equal(h.state.library.type,"tv");
  assert.deepEqual(h.events,[["search",false]]);
});

test("provisional local count cannot demote a valid merged page or navigate away", async () => {
  const h=searchHarness();
  h.state.library.page=5;
  h.state.library.pageSize=25;
  h.library.renderResults=(data,provisional)=>{if(!provisional)h.library.correctResultPage(data);};
  const done=h.library.runSearch();
  h.requests[0].resolve({total:30,page:5,page_size:25,items:[]});
  await Promise.resolve();
  assert.equal(h.state.library.page,5);
  assert.equal(h.events.length,0);
  h.requests[1].resolve({catalog:{total:200,page:5,page_size:25,items:[]},remote:{status:"fresh"}});
  await done;
  assert.equal(h.state.library.page,5);
  assert.equal(h.events.length,0);
  for(const hidden of [true,false]) {
    h.$("library").hidden=hidden;
    h.state.library.media=hidden?null:"42";
    assert.equal(h.library.correctResultPage({total:30,page:5,page_size:25}),false);
    assert.equal(h.events.length,0);
  }
});

test("failed remote lookup can correct page only after final local fallback", async () => {
  const h=searchHarness();
  h.state.library.page=5;
  h.library.renderResults=(data,provisional)=>{if(!provisional)h.library.correctResultPage(data);};
  const done=h.library.runSearch();
  h.requests[0].resolve({total:30,page:5,page_size:25,items:[]});
  await Promise.resolve();
  assert.equal(h.state.library.page,5);
  h.requests[1].reject(new Error("fixture unavailable"));
  await done;
  assert.equal(h.state.library.page,2);
  assert.deepEqual(h.events,[["search",false]]);
});

test("stable scrollbar gutter keeps skeleton and full-page column capacity equal", () => {
  const css=fs.readFileSync(path.join(__dirname,"../static/app.css"),"utf8");
  assert.match(css,/:root\s*\{[^}]*scrollbar-gutter:\s*stable/);
});

test("starts local and remote concurrently; final page replaces provisional local results", async () => {
  const h = searchHarness();
  const done = h.library.runSearch();
  assert.equal(h.requests.length, 2, "both requests start without waiting for either result");
  assert.equal(new URL(h.requests[1].url, "https://fixture.test").searchParams.get("catalog"), "1");
  h.requests[0].resolve({total: 1});
  await Promise.resolve();
  assert.deepEqual(h.rendered, [1]);
  h.requests[1].resolve({catalog: {total: 3}, remote: {status: "fresh"}});
  await done;
  assert.deepEqual(h.rendered, [1, 3]);
});

test("late local response cannot overwrite merged results", async () => {
  const h = searchHarness();
  const done = h.library.runSearch();
  h.requests[1].resolve({catalog: {total: 3}, remote: {status: "fresh"}});
  await Promise.resolve();
  h.requests[0].resolve({total: 1});
  await done;
  assert.deepEqual(h.rendered, [3]);
});

test("remote failure retains local results and exposes partial-result status", async () => {
  const h = searchHarness();
  const done = h.library.runSearch();
  h.requests[1].reject(new Error("fixture timeout"));
  h.requests[0].resolve({total: 1});
  await done;
  assert.deepEqual(h.rendered, [1]);
  assert.match(h.$("libraryRemoteStatus").textContent, /暂时无法查询/);
});

test("old query responses cannot render after a new query or browse navigation", async () => {
  const h = searchHarness();
  const old = h.library.runSearch();
  h.state.library.q = "new query";
  const current = h.library.runSearch();
  h.requests[2].resolve({total: 2});
  h.requests[3].resolve({catalog: {total: 4}, remote: {status: "fresh"}});
  await current;
  h.requests[0].resolve({total: 100});
  h.requests[1].resolve({catalog: {total: 101}, remote: {status: "fresh"}});
  await old;
  assert.deepEqual(h.rendered, [2, 4]);
  const pending = h.library.runSearch();
  h.state.library.searchGeneration++;
  h.requests[4].resolve({total: 102});
  h.requests[5].resolve({catalog: {total: 103}, remote: {status: "fresh"}});
  await pending;
  assert.deepEqual(h.rendered, [2, 4]);
});

test("unmeasured default stays 25 while rails and hero retain independent limits", () => {
  const { state, library, LIBRARY_RAILS } = harness();
  assert.equal(state.library.pageSize, 25);
  assert.equal(new URLSearchParams(library.buildQuery()).get("page_size"), "25");
  assert.equal(LIBRARY_RAILS.length, 5);
  for (const rail of LIBRARY_RAILS) {
    assert.match(rail.endpoint, /(?:page_size|limit)=12$/);
  }
  assert.match(source, /placement=hero&limit=5/);
});

for (const [key, mediaType, sort] of [
  ["year_desc", "all", "year_desc"],
  ["movie", "movie", "links_desc"],
  ["tv", "tv", "links_desc"],
]) {
  test(`${key} heading is a native button and opens an unfiltered first result page`, () => {
    const { state, library, box, events } = harness();
    Object.assign(state.library, {
      q: "old query", type: "unknown", year: "1999", provider: ["115"],
      quality: ["1080p"], hdr: ["sdr"], genre: ["动画"], source: ["re0"],
      includeDeleted: true, sort: "year_asc", page: 3, media: "42",
    });
    library.loadRails();
    const heading = box.querySelector(`.rail[data-rail="${key}"]`).querySelector("h3");
    const button = heading.children[0];
    assert.ok(button, "rail heading must have its browse button");
    assert.equal(button.tagName, "button");
    assert.equal(button.type, "button");
    assert.equal(button.className, "rail-heading-button");
    assert.equal(button.children[0].className, "rail-heading-more");
    assert.equal(button.children[0].attributes["aria-hidden"], "true");
    button.onclick();
    const query = new URLSearchParams(library.buildQuery());
    assert.equal(query.get("type"), mediaType);
    assert.equal(query.get("sort"), sort);
    assert.equal(query.get("page"), "1");
    assert.equal(query.get("page_size"), "25");
    assert.equal(query.get("include_deleted"), "0");
    for (const name of ["q", "year", "provider", "quality", "hdr", "genre", "source"]) {
      assert.equal(query.has(name), false, `${name} must not leak into rail browsing`);
    }
    assert.equal(state.library.media, null);
    assert.ok(events.includes("form"));
    assert.deepEqual(events.slice(-3), [["search", true], ["focus", true], ["scroll", "start", "auto"]]);
  });
}

test("daily recommendations and RE0 discoveries do not gain invented browse destinations", () => {
  const { library, box } = harness();
  library.loadRails();
  for (const key of ["today", "re0"]) {
    assert.equal(box.querySelector(`.rail[data-rail="${key}"]`).querySelector("h3").children.length, 0);
  }
});
