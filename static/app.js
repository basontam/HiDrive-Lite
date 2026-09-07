/* HiDrive-Lite shell script.
 * Modules: api (fetch wrapper) / state (shared mutable values) / router
 * (?tab= handling) / views.browser (OpenList + STRM) / views.settings /
 * views.transfer (115 transfer dialog, reusable by later library views).
 */
(function () {
  "use strict";

  var TAB_IDS = ["library", "openlist", "strm", "settings"];

  var $ = function (id) { return document.getElementById(id); };
  var ICONS_URL = "/static/icons.svg?v=" + (document.body.dataset.assetVersion || "");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function feedback(id, message, kind) {
    var el = $(id);
    el.textContent = message || "";
    el.className = "feedback" + (kind ? " " + kind : "");
    el.hidden = !message;
  }
  function setEmpty(id, message) {
    $(id).innerHTML = '<div class="empty">' + esc(message) + "</div>";
  }
  function setError(id, message, retry) {
    var box = $(id);
    box.innerHTML = '<div class="empty error"><span>' + esc(message) + '</span><button type="button" class="tertiary retry-btn">重试</button></div>';
    box.querySelector(".retry-btn").onclick = retry;
  }
  // The 115 folder-picker route can bubble up a raw Python exception class
  // name (e.g. "ConnectionError") or other non-Chinese upstream text as its
  // error message -- never show that in the folder list (T7 §8). Anything
  // that isn't an actual Chinese message falls back to a generic one.
  function friendlyFolderError(message) {
    return message && /[一-鿿]/.test(message) ? message : "目录读取失败，请检查 115 Cookie 或稍后重试";
  }
  function isStrmFile(name) {
    return /\.strm$/i.test(String(name || ""));
  }
  // Shared row markup for OpenList and STRM directory listings. `meta`
  // (size/modified, OpenList only) is only rendered when the caller passes
  // it -- omit the argument (as STRM does) to leave the meta span out.
  function fileRowHtml(kind, name, path, meta) {
    var iconUse = kind === "dir"
      ? '<use href="' + ICONS_URL + '#icon-folder"></use>'
      : '<use href="' + ICONS_URL + '#icon-file"></use>';
    var badge = kind === "file" && isStrmFile(name) ? '<span class="badge-strm">STRM</span>' : "";
    var nameSpan = '<span class="file-row-name"><svg class="file-icon" aria-hidden="true">' + iconUse + '</svg>' + esc(name) + badge + '</span>';
    if (kind === "dir") {
      return '<button type="button" class="file-row directory" data-path="' + esc(path) + '">' + nameSpan + '<span class="folder-action">打开</span></button>';
    }
    var metaSpan = meta === undefined ? "" : '<span class="file-meta">' + esc(meta) + '</span>';
    return '<div class="file-row">' + nameSpan + metaSpan + '</div>';
  }

  // ------------------------------------------------------------------
  // Localisation maps (brief §2): raw backend codes are never shown to the
  // user -- every provider/media-type/review-reason/worker-state code is
  // translated through one of these before it reaches the DOM.
  // ------------------------------------------------------------------
  // Single source of truth shared with the backend's
  // library_normalize.PROVIDERS (T8 §3): the real codes emitted there are
  // tianyicloud/139cloud (magnet links are stored as provider "ed2k") --
  // there is no separate "tianyi"/"139"/"magnet" code in production, so
  // this map carries only the codes that actually occur.
  var PROVIDER_LABEL = {
    "115": "115 网盘", tianyicloud: "天翼云盘", quark: "夸克网盘", alipan: "阿里云盘",
    baidu: "百度网盘", guangya: "广亚", "139cloud": "移动云盘", "123": "123 云盘",
    ed2k: "ED2K", unknown: "其他"
  };
  var MEDIA_TYPE_LABEL = { movie: "电影", tv: "剧集", unknown: "未分类" };
  // y1-cards §1.1: the only metadata line a media-card is allowed to show
  // -- year and type joined by " · ", either half dropped when empty, and
  // no placeholder rendered when both are empty.
  function formatCardMeta(year, mediaType) {
    // §1.1: an unknown/unclassified type shows the year only (the detail
    // page keeps the 未分类 wording); no year and no known type → "".
    var typeLabel = mediaType && mediaType !== "unknown" ? (MEDIA_TYPE_LABEL[mediaType] || "") : "";
    return [year || "", typeLabel].filter(Boolean).join(" · ");
  }
  var MATCH_LABEL = { exact: "", candidate: "候选匹配", needs_review: "待复核", unmatched: "待匹配" };
  var REVIEW_REASON_LABEL = {
    title_alias_conflict: "别名冲突", year_missing: "缺年份", edition_unparsed: "版本未解析",
    link_shared_across_groups: "链接跨组", row_shifted: "源表错位", access_code_conflict: "访问码冲突",
    access_code_divergence: "访问码不一致", timestamp_unparsed: "时间未解析"
  };
  var WORKER_STATE_LABEL = { running: "运行中", paused: "已暂停", stale: "无心跳", not_started: "未启动" };
  // T10: /api/library/tmdb-status's paused_reason -> settings-page 已暂停 text.
  var TMDB_PAUSED_REASON_LABEL = {
    disabled: "未启用开关", key_missing: "未填写 API Key", not_installed: "索引未安装",
    no_heartbeat: "后台线程尚未上报心跳", stale: "心跳超时"
  };
  // Mirrors app.py's _tmdb_error_hint -- used both for tmdb-check's own
  // error_class and for the settings page's "最近错误" (last_error) line.
  var TMDB_ERROR_HINT = {
    InvalidApiKey: "TMDB API Key 无效：请填写 v3 auth key（32 位十六进制），不是 v4 Read Access Token",
    BudgetExhausted: "今日额度已用完，明日 UTC 0 点重置或提高每日预算"
  };
  function tmdbErrorHint(errorClass) {
    if (!errorClass) return "无";
    if (TMDB_ERROR_HINT[errorClass]) return TMDB_ERROR_HINT[errorClass];
    if (/Connection|Timeout|Proxy|SSL/.test(errorClass) || errorClass === "RequestException") return "无法连接 TMDB：请检查服务器网络或代理设置";
    return "TMDB 请求失败：" + errorClass;
  }

  function providerLabel(code) { return PROVIDER_LABEL[code] || code; }

  // I2: a group's own `link_count` (and, transitively, `media.link_count`,
  // summed from it) counts every resource_link row including deleted ones
  // (library_store.recount() has no `deleted_at_source IS NULL` filter) --
  // it is NOT live-only. `group.providers` (per-provider live counts) is
  // the only live-only source available client-side, so the live total for
  // a group is the sum of its values; `link_count` is only a fallback for
  // the (never expected in practice) case where `providers` wasn't sent.
  function groupLiveLinkCount(group) {
    if (group && group.providers && typeof group.providers === "object") {
      return Object.keys(group.providers).reduce(function (sum, k) { return sum + (group.providers[k] || 0); }, 0);
    }
    return (group && group.link_count) || 0;
  }

  // x1-provider-ui §2.2/§4.2: the detail-page provider-logo tablist's fixed
  // display priority -- mirrors library_store.PROVIDER_ORDER -- plus the
  // icons.svg symbol id and brand-accent colour for each code. "unknown"
  // falls back to the neutral "provider-link" mark (spec §4.1: a logo
  // failure/absence falls back to a neutral link icon + the Chinese name).
  var PROVIDER_ORDER = ["115", "tianyicloud", "quark", "alipan", "baidu", "guangya", "139cloud", "123", "ed2k", "unknown"];
  var PROVIDER_SYMBOL = {
    "115": "provider-115", tianyicloud: "provider-tianyicloud", quark: "provider-quark", alipan: "provider-alipan",
    baidu: "provider-baidu", guangya: "provider-guangya", "139cloud": "provider-139cloud", "123": "provider-123",
    ed2k: "provider-ed2k", unknown: "provider-link"
  };
  var PROVIDER_ACCENT = {
    "115": "#2f7df6", tianyicloud: "#f59e0b", quark: "#5b67e8", alipan: "#6d5ce7", baidu: "#2f7df6",
    guangya: "#64748b", "139cloud": "#64748b", "123": "#64748b", ed2k: "#64748b", unknown: "#64748b"
  };
  var providerMeta = PROVIDER_ORDER.map(function (code) {
    return { code: code, label: PROVIDER_LABEL[code], symbol: PROVIDER_SYMBOL[code], accent: PROVIDER_ACCENT[code] };
  });
  var PROVIDER_META_BY_CODE = {};
  providerMeta.forEach(function (m) { PROVIDER_META_BY_CODE[m.code] = m; });

  // T18 §12.1: the "sources" filter facet still needs a Chinese label for
  // each raw source_type code (library_normalize.py's _SOURCE_RULES
  // vocabulary) -- SOURCE_LABEL is used only by the filter-popover chips
  // now (renderToggleGroup("filterSource", ...) below); the old detail-page
  // spec-chip row (raw codec/HDR/source-type/audio/subtitle label maps) is
  // replaced by specRowHtml, which renders straight from the backend's
  // already-normalised `group.specs` (§12.3).
  var SOURCE_LABEL = { remux: "REMUX", bluray: "原盘", bdrip: "BDRip", webdl: "WEB-DL", hdtv: "HDTV" };

  // T18 §12.2/§12.3: resource spec-chip icons. The backend's
  // group.specs[key].icon is always one of three fixed category names
  // ("icon-resolution"/"icon-dynamic-range"/"icon-source") -- Font
  // Awesome's Free tier has no per-resolution glyph (rectangle-4k/
  // high-definition/standard-definition are Pro-only) and no brand mark
  // for every source type, so this module picks the actual vendored
  // symbol from the category plus the spec's own (already-normalised)
  // value instead. See docs/icon-assets.md for why each glyph was chosen.
  var SPEC_CATEGORY_LABEL = { resolution: "分辨率", dynamic_range: "动态范围", source: "片源" };
  var SPEC_SOURCE_SYMBOL = {
    "WEB-DL": "fa-film", WEBRip: "fa-film", HDTV: "fa-film",
    BluRay: "fa-compact-disc", BDRemux: "fa-compact-disc", BDRip: "fa-compact-disc"
  };
  function specSymbol(key, value) {
    if (key === "dynamic_range" && value === "Dolby Vision") return "si-dolby";
    if (key === "source") return SPEC_SOURCE_SYMBOL[value] || "fa-film";
    if (key === "dynamic_range") return "fa-circle-half-stroke";
    return "fa-photo-film"; // resolution, and any unrecognised category
  }

  // T18 §14.4: rating-source brand marks. TVmaze has no vendored icon --
  // it renders as a plain text badge instead (docs/icon-assets.md).
  var RATING_SOURCE_LABEL = { tmdb: "TMDB", imdb: "IMDb", tvmaze: "TVmaze" };
  var RATING_SOURCE_SYMBOL = { tmdb: "si-tmdb", imdb: "si-imdb" };
  var RATING_SOURCE_ORDER = ["tmdb", "imdb", "tvmaze"];

  // Compact vote-count formatting (§14.4): "31,198" below one million,
  // "3.2M" at or above it; the caller is responsible for putting the exact
  // value in a `title`/aria-label for accessibility/hover.
  function formatVotes(votes) {
    if (votes == null || votes <= 0) return null;
    if (votes >= 1e6) return (Math.round(votes / 1e5) / 10) + "M";
    return String(votes).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  // §14.4: "卡片海报墙只显示一个不拥挤的主评分" -- item.primary_rating is
  // already the single TMDB-else-IMDb source the backend picked (never
  // TVmaze here); rendered inside the poster box's own corner so the
  // fixed-height .card-footer geometry (y1-cards §2.1, the screenshot
  // walkthrough's poster-geometry contract) never shifts.
  function primaryRatingBadgeHtml(primary) {
    if (!primary) return "";
    // Review fix: a non-numeric score (missing/null/NaN/a string) must
    // never render as the literal text "NaN" -- render nothing instead.
    if (typeof primary.score !== "number" || !isFinite(primary.score)) return "";
    var symbol = RATING_SOURCE_SYMBOL[primary.source];
    var label = RATING_SOURCE_LABEL[primary.source] || primary.source;
    var scoreText = (Math.round(primary.score * 10) / 10).toFixed(1);
    var icon = symbol
      ? '<svg class="rating-badge-icon" aria-hidden="true"><use href="' + ICONS_URL + '#' + symbol + '"></use></svg>'
      : "";
    // Review fix: aria-label on a bare <span> (role "generic") isn't
    // reliably exposed as an accessible name by every screen reader --
    // role="img" gives it a name-able role so the label is announced as
    // one atomic unit instead of relying on (or leaking) its own text.
    return '<span class="card-rating-badge" role="img" aria-label="' + esc(label + " 评分 " + scoreText) + '">' + icon +
      '<span class="card-rating-score">' + esc(scoreText) + "</span></span>";
  }
  // ------------------------------------------------------------------
  // w6-contract: link validity check -- shared reason/relative-time
  // helpers used by both the link-row red text badge and the card's
  // "全部失效" corner badge below.
  // ------------------------------------------------------------------
  var LINKCHECK_REASON_LABEL = {
    share_not_found: "分享不存在", share_cancelled: "分享已取消", share_expired: "分享已过期",
    file_deleted: "文件已删除", share_audit: "分享受限（审核/封禁）"
  };
  // Coarse "N 分钟/小时/天/个月/年前" relative phrasing for a link-check
  // tooltip -- never more precise than that (no seconds), and never a raw
  // ISO timestamp shown to the user.
  function formatRelativeTime(iso) {
    var t = iso ? Date.parse(iso) : NaN;
    if (isNaN(t)) return "";
    var diffSec = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (diffSec < 60) return "刚刚";
    var diffMin = Math.round(diffSec / 60);
    if (diffMin < 60) return diffMin + " 分钟前";
    var diffHour = Math.round(diffMin / 60);
    if (diffHour < 24) return diffHour + " 小时前";
    var diffDay = Math.round(diffHour / 24);
    if (diffDay < 30) return diffDay + " 天前";
    var diffMonth = Math.round(diffDay / 30);
    if (diffMonth < 12) return diffMonth + " 个月前";
    return Math.round(diffMonth / 12) + " 年前";
  }
  // w6-contract §UI item 1: deleted (来源已删除) and invalid (检测失效 ·
  // <reason> · 检测于 <相对时间>) get distinct tooltips -- `deleted` wins if
  // a link is somehow both (the contract only ever sets one at a time).
  function linkInvalidTooltip(link) {
    if (link.deleted) return "来源已删除";
    var reason = LINKCHECK_REASON_LABEL[link.check_reason] || link.check_reason || "";
    var parts = ["检测失效"];
    if (reason) parts.push(reason);
    if (link.checked_at) parts.push("检测于 " + formatRelativeTime(link.checked_at));
    return parts.join(" · ");
  }
  // Round 16: one status word per link row, right after the provider
  // label -- 已失效 (deleted at source or checker-invalid; "来源已删除"
  // wins the tooltip), 有效, 待确认 (checker could not decide: friendly
  // reason text from LINKCHECK_ERROR_CLASS_LABEL), 未检测 (never checked).
  // Plain text on existing colour tokens; no emoji, no icon.
  function linkStatusTooltip(link) {
    if (link.deleted || link.invalid) return linkInvalidTooltip(link);
    var when = link.checked_at ? "检测于 " + formatRelativeTime(link.checked_at) : "";
    if (link.check_status === "valid") return ["检测有效", when].filter(Boolean).join(" · ");
    if (link.check_status === "unknown") {
      return ["待确认", linkcheckErrorClassLabel(link.check_reason), when].filter(Boolean).join(" · ");
    }
    return "尚未检测";
  }
  function linkStatusBadgeHtml(link) {
    var cls, text;
    if (link.deleted || link.invalid) { cls = "link-invalid-text"; text = "已失效"; }
    else if (link.check_status === "valid") { cls = "link-status-text link-status-valid"; text = "有效"; }
    else if (link.check_status === "unknown") { cls = "link-status-text link-status-unknown"; text = "待确认"; }
    else { cls = "link-status-text link-status-unchecked"; text = "未检测"; }
    return '<span class="' + cls + '" title="' + esc(linkStatusTooltip(link)) + '">' + text + '</span>';
  }
  // w6-contract §UI item 3: a card's own "全部失效" corner badge -- opposite
  // corner from primaryRatingBadgeHtml's top-left placement, so the two
  // never collide; absolutely positioned inside the poster box, never
  // affecting the fixed-height .card-footer geometry below it.
  function cardInvalidBadgeHtml(allInvalid) {
    if (!allInvalid) return "";
    return '<span class="card-invalid-badge" role="img" aria-label="全部链接已失效">全部失效</span>';
  }
  // w6-contract §5: the settings page's "资源有效性检测" card -- fixed
  // phase-1 provider list/order per the brief (天翼云盘/115/夸克/阿里云盘),
  // distinct from the detail page's PROVIDER_ORDER (which also lists
  // baidu/guangya/etc that this check never covers).
  var LINKCHECK_PROVIDERS = ["tianyicloud", "115", "quark", "alipan"];
  var LINKCHECK_ERROR_CLASS_LABEL = {
    network_error: "网络错误", http_4xx: "网盘返回 4xx 错误", http_5xx: "网盘返回 5xx 错误",
    rate_limited: "被限流", anti_bot: "触发反爬虫机制", parse_error: "解析失败",
    unmapped_response: "网盘返回未识别的结果",
    unsupported_url: "链接格式不支持", provider_disabled: "网盘检测未开启", budget_exhausted: "今日检测额度已用完"
  };
  function linkcheckErrorClassLabel(code) {
    if (!code) return "";
    return LINKCHECK_ERROR_CLASS_LABEL[code] || code;
  }
  // One provider row's static skeleton (label/icon/switch/cap input) --
  // rendered once, independent of any status fetch, so the settings card
  // always shows all four rows even if GET linkcheck-status hasn't
  // resolved yet (or fails). `renderLinkcheckStatus` below only ever
  // hydrates values inside this markup, never rebuilds it.
  function linkcheckProviderRowHtml(code) {
    var meta = PROVIDER_META_BY_CODE[code];
    var symbol = meta ? meta.symbol : "provider-link";
    var label = meta ? meta.label : code;
    return '<div class="linkcheck-provider" data-provider="' + esc(code) + '">' +
      '<div class="linkcheck-provider-head">' +
      '<svg class="provider-logo" aria-hidden="true"><use href="' + ICONS_URL + '#' + symbol + '"></use></svg>' +
      '<span class="linkcheck-provider-label">' + esc(label) + '</span>' +
      '<label class="switch-field"><input id="linkcheck-' + esc(code) + '-enabled" type="checkbox"><span>启用</span></label>' +
      '</div>' +
      '<div class="field">' +
      '<label for="linkcheck-' + esc(code) + '-cap">每日检测上限</label>' +
      '<input id="linkcheck-' + esc(code) + '-cap" type="number" min="0" max="20000">' +
      '</div>' +
      '<div id="linkcheck-' + esc(code) + '-status" class="scrape-status"></div>' +
      '</div>';
  }
  // One plain sentence describing what the SERVER currently has stored for
  // the card (from GET linkcheck-status) -- shown under the card so a user
  // who ticked boxes but never saved can see the difference at a glance.
  function linkcheckEnabledSummary(d) {
    if (!d) return "服务器当前：无法读取检测状态";
    var enabledProviders = LINKCHECK_PROVIDERS.filter(function (code) {
      return !!((d.providers || {})[code] || {}).enabled;
    }).map(providerLabel);
    if (!d.enabled) {
      return enabledProviders.length
        ? "服务器当前：检测未启用（已勾选 " + enabledProviders.join("、") + "，但总开关未开）"
        : "服务器当前：检测未启用";
    }
    return enabledProviders.length
      ? "服务器当前：检测已启用（" + enabledProviders.join("、") + "）"
      : "服务器当前：总开关已开，但没有启用任何网盘";
  }
  // Reads the whole card's current DOM state into the two payload keys
  // /api/settings expects -- kept out of the 保存设置 button's own click
  // handler so that handler stays a short, readable sequence of "if
  // dirty, add this key" lines like its tmdb_enrich_enabled sibling above.
  function linkcheckSavePayload() {
    var providers = {};
    LINKCHECK_PROVIDERS.forEach(function (code) {
      providers[code] = {
        enabled: $("linkcheck-" + code + "-enabled").checked,
        daily_cap: parseInt($("linkcheck-" + code + "-cap").value, 10) || 0
      };
    });
    return { linkcheck_enabled: $("linkcheckEnabled").checked, linkcheck_providers: providers };
  }
  // ------------------------------------------------------------------
  // api: thin JSON fetch wrapper shared by every view.
  // ------------------------------------------------------------------
  // W5: a tab left open past CSRF_TTL_SECONDS (backend, 3600s) has a
  // stale api.csrf and every non-GET POST/reveal/transfer/reauth call
  // starts failing with 403 "invalid CSRF token" until reload. A CSRF-
  // rejected request never reached the route handler, so re-fetching
  // /api/csrf and retrying the identical request once is safe even for
  // transfers. CSRF_REFRESH_INTERVAL_MS also drives a proactive refresh
  // (init(), below) so most tabs never hit the reactive path at all.
  var CSRF_REFRESH_INTERVAL_MS = 20 * 60 * 1000;
  var api = {
    csrf: "",
    csrfFetchedAt: 0,
    refreshCsrf: function () {
      return api.request("/api/csrf").then(function (c) {
        api.csrf = c.token;
        api.csrfFetchedAt = Date.now();
        return c;
      });
    },
    request: function (url, opt) {
      opt = opt || {};
      opt.headers = Object.assign({ Accept: "application/json", "Content-Type": "application/json" }, opt.headers || {});
      if (opt.method && opt.method !== "GET" && api.csrf) opt.headers["X-CSRF-Token"] = api.csrf;
      var canRetryCsrf = !!(opt.method && opt.method !== "GET" && !opt._csrfRetried);
      return fetch(url, opt).then(function (r) {
        var contentType = r.headers.get("content-type") || "";
        if (!contentType.includes("application/json")) {
          if (r.url.includes("/cdn-cgi/access/login")) window.location.href = r.url;
          throw new Error("Cloudflare Access 登录已过期，请重新登录");
        }
        return r.json().catch(function () { return { message: r.statusText }; }).then(function (d) {
          if (!r.ok) {
            if (canRetryCsrf && r.status === 403 && (d.message === "invalid CSRF token" || d.message === "CSRF token required")) {
              opt._csrfRetried = true;
              return api.refreshCsrf().then(function () {
                opt.headers["X-CSRF-Token"] = api.csrf;
                return api.request(url, opt);
              });
            }
            var err = new Error(d.message || "HTTP " + r.status);
            err.code = d.code;
            throw err;
          }
          return d;
        });
      });
    }
  };

  // ------------------------------------------------------------------
  // state: mutable values shared across views.
  // ------------------------------------------------------------------
  var state = {
    openPaths: { "115pan": "/115pan", "115strm": "/115strm" },
    currentOpenPath: "/115pan",
    currentOpenPreset: "115pan",
    transferRoot: "/115pan",
    // T18 §12.5: the currently browsed OpenList path IS the transfer
    // target now (no separate "选择此目录" step) -- transferRootPid/
    // currentTransferPid are the 115 pid this client can actually PROVE
    // corresponds to a path (init() below seeds transferRootPid from
    // /api/status's 115.open_root_cid, the only path<->pid mapping ever
    // exposed to the browser); currentTransferPid is kept in lockstep
    // with currentTransferPath by loadFolders() and is never invented for
    // an arbitrary subfolder -- see loadFolders' own comment.
    transferRootPid: "",
    currentTransferPath: "/115pan",
    currentTransferPid: "",
    currentStrmPath: "",
    // Lazy-tab latches (T4): OpenList/STRM only ever fetch once, on the
    // first time their tab is actually activated -- never at page load.
    openlistLoaded: false,
    strmLoaded: false
  };

  // ------------------------------------------------------------------
  // router: reads/writes ?tab= and shows the matching panel. Tabs follow
  // the ARIA "automatic activation" pattern: a roving tabindex (only the
  // selected tab is in the Tab order) plus ArrowLeft/ArrowRight/Home/End
  // move focus *and* activate the newly focused tab.
  // ------------------------------------------------------------------
  var router = {
    readTab: function () {
      var tab = new URLSearchParams(window.location.search).get("tab");
      return TAB_IDS.indexOf(tab) === -1 ? TAB_IDS[0] : tab;
    },
    activate: function (tab) {
      // T16 §5: #libraryFilters is a body-level popover now, no longer a
      // descendant of the #library panel -- close it on every tab switch
      // (including re-selecting library) or it would keep floating over
      // whichever tab is shown next. views.library isn't defined yet the
      // very first time init() calls activate() -- guard it.
      if (views.library) views.library.closeFilters(false);
      document.querySelectorAll("[data-tab]").forEach(function (button) {
        var selected = button.dataset.tab === tab;
        button.setAttribute("aria-selected", selected ? "true" : "false");
        button.tabIndex = selected ? 0 : -1;
        // Keep the active tab visible in the horizontally-scrolling tab bar
        // (T7 §6), e.g. after a direct deep link or arrow-key navigation.
        if (selected) button.scrollIntoView({ block: "nearest", inline: "nearest" });
      });
      TAB_IDS.forEach(function (id) { $(id).hidden = id !== tab; });
      // A detail view pushed while on the library tab is only valid for the
      // *current* library visit -- leaving the tab (T7 §7) means a later
      // history.back() from closeDetail() could land on an intervening
      // tab-switch history entry instead of the results view.
      if (tab !== "library") state.detailPushed = false;
      if (tab === "library" && views.library) views.library.onEnter();
      if (tab === "openlist" && !state.openlistLoaded) { state.openlistLoaded = true; views.browser.openlist.load(state.currentOpenPath); }
      if (tab === "strm" && !state.strmLoaded) { state.strmLoaded = true; views.browser.strm.load(""); }
    },
    go: function (tab) {
      if (TAB_IDS.indexOf(tab) === -1) return;
      // T16 §3: activating "library" -- even when it is already the active
      // tab -- always resets to the home view instead of the generic
      // push-and-activate every other tab uses; see goHome().
      if (tab === "library") { views.library.goHome(); return; }
      var url = new URL(window.location.href);
      url.searchParams.set("tab", tab);
      window.history.pushState({ tab: tab }, "", url);
      router.activate(tab);
    },
    init: function () {
      router.activate(router.readTab());
      document.querySelectorAll("[data-tab]").forEach(function (button) {
        button.addEventListener("click", function () { router.go(button.dataset.tab); });
      });
      document.querySelector(".workspace-nav").addEventListener("keydown", function (e) {
        var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-tab]"));
        var idx = tabs.indexOf(document.activeElement);
        if (idx === -1) return;
        var nextIdx = null;
        if (e.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
        else if (e.key === "ArrowLeft") nextIdx = (idx - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") nextIdx = 0;
        else if (e.key === "End") nextIdx = tabs.length - 1;
        if (nextIdx === null) return;
        e.preventDefault();
        tabs[nextIdx].focus();
        router.go(tabs[nextIdx].dataset.tab);
      });
      window.addEventListener("popstate", function () { router.activate(router.readTab()); });
    }
  };

  // ------------------------------------------------------------------
  // views: OpenList/STRM browsers, settings form, transfer dialog.
  // ------------------------------------------------------------------
  var views = {};

  views.browser = {
    openlist: {
      childPath: function (name) {
        return (state.currentOpenPath === "/" ? "" : state.currentOpenPath.replace(/\/$/, "")) + "/" + name;
      },
      isDir: function (row) {
        return row.is_dir === true || row.is_dir === 1 || row.is_dir === "1" || row.is_dir === "true" || row.is_dir === "True";
      },
      render: function (d) {
        var payload = d.data || {};
        var rows = payload.content || payload.items || [];
        if (!Array.isArray(rows)) { setEmpty("openResult", "OpenList 返回格式异常。"); return; }
        var box = $("openResult");
        box.innerHTML = rows.map(function (r) {
          var name = String(r.name || "未命名");
          if (views.browser.openlist.isDir(r)) {
            var listedPath = String(r.path || "");
            var path = listedPath.indexOf("/") === 0 ? listedPath : views.browser.openlist.childPath(name);
            return fileRowHtml("dir", name, path);
          }
          var meta = [r.size, r.modified].filter(Boolean).join(" · ");
          return fileRowHtml("file", name, "", meta);
        }).join("") || '<div class="empty">当前目录为空。</div>';
        box.querySelectorAll(".directory").forEach(function (b) {
          b.onclick = function () { views.browser.openlist.load(b.dataset.path); };
        });
      },
      load: function (path) {
        path = path === undefined ? state.currentOpenPath : path;
        state.currentOpenPath = path;
        $("openCurrent").textContent = path;
        setEmpty("openResult", "读取 OpenList 目录中…");
        return api.request("/api/openlist/list?path=" + encodeURIComponent(path) + "&page=1")
          .then(views.browser.openlist.render)
          .catch(function (e) { setError("openResult", e.message, function () { views.browser.openlist.load(path); }); });
      },
      init: function () {
        $("openPreset").onchange = function () {
          state.currentOpenPreset = $("openPreset").value;
          views.browser.openlist.load(state.openPaths[state.currentOpenPreset] || "/");
        };
        $("openUp").onclick = function () {
          var root = state.openPaths[state.currentOpenPreset] || "/";
          if (state.currentOpenPath !== root) views.browser.openlist.load(state.currentOpenPath.replace(/\/[^/]+$/, "") || root);
        };
        $("openRefresh").onclick = function () { views.browser.openlist.load(state.currentOpenPath); };
      }
    },
    strm: {
      render: function (d) {
        state.currentStrmPath = d.path || "";
        $("strmCurrent").textContent = "/" + state.currentStrmPath;
        var rows = Array.isArray(d.items) ? d.items : [];
        var box = $("strmResult");
        box.innerHTML = rows.map(function (r) {
          var name = String(r.name || "未命名");
          if (r.directory) return fileRowHtml("dir", name, r.path);
          return fileRowHtml("file", name, "");
        }).join("") || '<div class="empty">当前目录为空。</div>';
        box.querySelectorAll(".directory").forEach(function (b) {
          b.onclick = function () { views.browser.strm.load(b.dataset.path); };
        });
      },
      load: function (path) {
        path = path === undefined ? state.currentStrmPath : path;
        setEmpty("strmResult", "读取 STRM 目录中…");
        return api.request("/api/strm/list?path=" + encodeURIComponent(path))
          .then(views.browser.strm.render)
          .catch(function (e) { setError("strmResult", e.message, function () { views.browser.strm.load(path); }); });
      },
      init: function () {
        $("strmUp").onclick = function () { views.browser.strm.load(state.currentStrmPath.replace(/\/[^/]+$/, "") || ""); };
        $("strmRefresh").onclick = function () { views.browser.strm.load(state.currentStrmPath); };
      }
    }
  };

  // T19: on 115_REAUTH_REQUIRED, point the user at the settings tab's QR
  // flow instead of a dead-end error -- and never resubmit automatically.
  function renderTransferError(e) {
    if (e.code === "115_REAUTH_REQUIRED") {
      var box = $("transferResult");
      box.innerHTML = esc(e.message) + ' <button type="button" class="tertiary" id="transferGotoSettings">前往设置页</button>';
      box.className = "feedback error";
      box.hidden = false;
      $("transferGotoSettings").onclick = function () {
        // Return-to-origin (brief w5-reauth-return §2): remember where the
        // user came from so a successful QR scan can bring them straight
        // back to this same library link's transfer dialog instead of
        // stranding them on settings.
        state.reauth.returnTo = { tab: router.readTab(), linkId: $("transferLinkId").value, title: state.transferTitle };
        views.transfer.close();
        router.go("settings");
      };
      return;
    }
    // Review fix: TRANSFER_DUPLICATE (409) is the backend rejecting a
    // benign double-submit -- the original request is still genuinely in
    // flight, not a failure -- so it gets a plain info notice, never the
    // red error style.
    if (e.code === "TRANSFER_DUPLICATE") {
      feedback("transferResult", "该链接正在转存中，请稍后查看");
      return;
    }
    feedback("transferResult", e.message, "error");
  }

  state.transferBusy = false;
  state.transferTrigger = null;
  // Lazy-loaded once per session, on first dialog open (T8 §12) -- same
  // treatment as OpenList/STRM's state.openlistLoaded/state.strmLoaded.
  state.transferFoldersLoaded = false;
  // Review fix: transferFoldersLoaded alone only ever fires the fetch
  // once per page session, success or failure -- if that one attempt
  // failed, saveBtn stayed disabled for every later dialog open in the
  // same session (loadFolders never ran again unless the user happened
  // to notice and click 刷新). Retry automatically on open whenever the
  // last attempt failed, cleared back to false on any successful load.
  state.transferLoadFailed = false;

  views.transfer = {
    openCommon: function (title, copied) {
      $("transferTitle").textContent = title ? "转存：" + title : "115 转存";
      // T18 §12.5: single-target-path model -- there is no separate
      // "选择此目录" step any more, so the subtitle no longer asks for one.
      $("transferSubtitle").textContent = copied ? "链接已自动复制，当前目录为转存目标。" : "当前目录为转存目标，确认无误后转存。";
      $("folderCurrent").textContent = state.currentTransferPath;
      $("targetPath").textContent = state.currentTransferPath;
      state.transferTrigger = document.activeElement;
      $("transferDialog").hidden = false;
      document.body.classList.add("modal-open");
      feedback("transferResult", copied ? "链接已复制。" : "");
      if (!state.transferFoldersLoaded || state.transferLoadFailed) {
        state.transferFoldersLoaded = true;
        views.transfer.loadFolders(state.currentTransferPath || state.transferRoot);
      }
      views.transfer.focusFirst();
    },
    focusFirst: function () {
      var card = document.querySelector("#transferDialog .modal-card");
      var focusable = card.querySelectorAll('button:not(:disabled), input:not([type="hidden"]):not(:disabled), [tabindex]');
      for (var i = 0; i < focusable.length; i++) {
        if (focusable[i].offsetParent !== null) { focusable[i].focus(); return; }
      }
    },
    openLibrary: function (linkId, title) {
      state.transferMode = "library";
      state.transferTitle = title;
      $("transferShareField").hidden = true;
      $("transferServerHeldField").hidden = false;
      $("transferLinkId").value = linkId;
      $("transferShareUrl").value = "";
      views.transfer.openCommon(title, false);
    },
    close: function () {
      if (state.transferBusy) { toast("转存正在进行，请稍候。", "error"); return; }
      $("transferDialog").hidden = true;
      document.body.classList.remove("modal-open");
      if (state.transferTrigger) state.transferTrigger.focus();
    },
    // T18 §12.5: the browsed path is always the transfer target --
    // folderCurrent and targetPath show the identical path, and
    // currentTransferPid is updated in the same breath: it is only ever
    // set to a pid this client can actually prove (the OpenList "115pan"
    // mount root's own 115_open_root_cid, the one path<->pid mapping
    // /api/status ever exposes -- see init()) when `path` is exactly that
    // mount root, and cleared for any subfolder -- there is no client-
    // visible way to resolve an arbitrary subfolder's real 115 pid (that
    // walk only ever happens server-side, in resolve_115_target_path). A
    // cleared pid is never invented: the submit handler below just sends
    // target_path alone in that case, exactly like the pre-existing
    // manual-link flow, and the server resolves it itself.
    loadFolders: function (path) {
      path = path === undefined ? state.currentTransferPath : path;
      state.currentTransferPath = path;
      state.currentTransferPid = path === state.transferRoot ? state.transferRootPid : "";
      $("folderCurrent").textContent = path;
      $("targetPath").textContent = path;
      return api.request("/api/115/folders?path=" + encodeURIComponent(path)).then(function (d) {
        var box = $("folderResult");
        var items = d.items || [];
        // §12.6: a directory that loaded successfully always has a usable
        // target path -- re-enable the primary button in case an earlier
        // navigation's failure had disabled it. An empty directory is
        // still a successful load (the user may transfer into it), so
        // this must run before the empty-directory early return below.
        state.transferLoadFailed = false;
        if (!state.transferBusy) $("saveBtn").disabled = false;
        if (!items.length) { setEmpty("folderResult", "当前目录没有子文件夹。"); return; }
        box.innerHTML = items.map(function (x) {
          return '<button type="button" class="folder" data-path="' + esc(x.path) + '"><span class="folder-name">' + esc(x.name) + '</span><span class="folder-action">打开</span></button>';
        }).join("");
        box.querySelectorAll(".folder").forEach(function (b) {
          b.onclick = function () { views.transfer.loadFolders(b.dataset.path); };
        });
      }).catch(function (e) {
        setEmpty("folderResult", friendlyFolderError(e.message));
        // §12.5/§12.6: a directory that failed to load must not let the
        // user submit against a stale/unconfirmed path. Flagging the
        // failure here is what lets openCommon's guard above retry on
        // the next dialog open instead of leaving saveBtn disabled for
        // the rest of the page session.
        state.transferLoadFailed = true;
        $("saveBtn").disabled = true;
      });
    },
    init: function () {
      $("folderUp").onclick = function () {
        if (state.currentTransferPath !== state.transferRoot) {
          views.transfer.loadFolders(state.currentTransferPath.replace(/\/[^/]+$/, "") || state.transferRoot);
        }
      };
      $("folderRefresh").onclick = function () { views.transfer.loadFolders(state.currentTransferPath); };
      // T18 §12.5: "选择此目录" is gone -- the browsed directory is always
      // the target, so the only two footer actions left are 取消 and the
      // single primary "转存到 115" button below.
      $("saveBtn").onclick = function () {
        if (state.transferBusy) return;
        var targetPath = state.currentTransferPath;
        var request;
        if (state.transferMode === "library") {
          var linkId = $("transferLinkId").value;
          if (!linkId) { feedback("transferResult", "缺少链接信息。", "error"); return; }
          // currentTransferPid is "" whenever the browsed folder isn't a
          // path this client can prove a pid for (loadFolders' comment
          // above) -- an empty string is indistinguishable from an absent
          // field to the backend (_resolve_115_target_pid treats a falsy
          // target_pid as not given), so it is always safe to send both
          // keys once rather than conditionally omitting one.
          request = api.request("/api/library/transfer", {
            method: "POST",
            body: JSON.stringify({ resource_link_id: linkId, target_path: targetPath, target_pid: state.currentTransferPid }),
          });
        }
        state.transferBusy = true;
        var btn = $("saveBtn");
        var originalLabel = btn.textContent;
        btn.disabled = true;
        btn.classList.add("loading");
        btn.textContent = "转存中…";
        request.then(function (d) {
          feedback("transferResult", d.message || "转存完成", d.success ? "success" : "error");
        }).catch(renderTransferError).then(function () {
          state.transferBusy = false;
          btn.disabled = false;
          btn.classList.remove("loading");
          btn.textContent = originalLabel;
        });
      };
      $("copyTransferLink").onclick = function () {
        navigator.clipboard.writeText($("transferShareUrl").value).then(function () {
          feedback("transferResult", "链接已复制。", "success");
        }).catch(function () {
          feedback("transferResult", "浏览器未允许复制，请手动复制链接。", "error");
        });
      };
      $("transferClose").onclick = views.transfer.close;
      $("transferCancel").onclick = views.transfer.close;
      document.querySelector("[data-close-transfer]").onclick = views.transfer.close;
      document.addEventListener("keydown", function (e) {
        if ($("transferDialog").hidden) return;
        if (e.key === "Escape") { views.transfer.close(); return; }
        // Focus trap: Tab/Shift+Tab cycle inside the dialog instead of
        // escaping to the page behind it.
        if (e.key !== "Tab") return;
        var card = document.querySelector("#transferDialog .modal-card");
        var focusable = Array.prototype.filter.call(
          card.querySelectorAll('button:not(:disabled), input:not([type="hidden"]):not(:disabled), [tabindex]'),
          function (el) { return el.offsetParent !== null; }
        );
        if (!focusable.length) return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      });
    }
  };

  // ------------------------------------------------------------------
  // views.reauth: the "重新授权 115" QR dialog (T19, brief §5.3). Polls
  // /api/115/reauth/status every 1.5s with only the opaque challenge_id
  // (never a cookie or any 115 response content); stops polling once the
  // QR's own countdown expires or a terminal state is reached. Nothing
  // here is ever written to localStorage.
  // ------------------------------------------------------------------
  state.reauth = { challengeId: null, expiresAt: 0, pollTimer: null, countdownTimer: null, trigger: null, polling: false, returnTo: null };

  views.reauth = {
    stopPolling: function () {
      // w6-reauth-longpoll-fix: pollTimer now holds a setTimeout id (the
      // poll chain reschedules itself), not a setInterval id -- close() and
      // refresh() both call this before starting anything new, so a stray
      // chain never outlives the challenge it was polling for.
      if (state.reauth.pollTimer) { clearTimeout(state.reauth.pollTimer); state.reauth.pollTimer = null; }
      if (state.reauth.countdownTimer) { clearInterval(state.reauth.countdownTimer); state.reauth.countdownTimer = null; }
    },
    focusFirst: function () {
      var card = document.querySelector("#reauthDialog .modal-card");
      var focusable = card.querySelectorAll('button:not(:disabled), [tabindex]');
      for (var i = 0; i < focusable.length; i++) {
        if (focusable[i].offsetParent !== null) { focusable[i].focus(); return; }
      }
    },
    updateCountdown: function () {
      var remain = Math.max(0, Math.round((state.reauth.expiresAt - Date.now()) / 1000));
      $("reauthCountdown").textContent = remain > 0 ? "二维码将在 " + remain + " 秒后过期" : "二维码已过期";
      if (remain <= 0 && state.reauth.pollTimer) {
        views.reauth.stopPolling();
        state.reauth.challengeId = null;
        state.reauth.returnTo = null;
        feedback("reauthResult", "二维码已过期，请点击刷新二维码。", "error");
      }
    },
    start: function () {
      views.reauth.stopPolling();
      state.reauth.challengeId = null;
      $("reauthQrImage").removeAttribute("src");
      feedback("reauthResult", "正在获取二维码…");
      return api.request("/api/115/reauth/start", { method: "POST" }).then(function (d) {
        state.reauth.challengeId = d.challenge_id;
        state.reauth.expiresAt = new Date(d.expires_at).getTime();
        // Cache-bust: the browser must never reuse a previous challenge's
        // proxied image for a new one.
        $("reauthQrImage").src = d.qr_url + "&_ts=" + Date.now();
        feedback("reauthResult", "");
        views.reauth.updateCountdown();
        state.reauth.countdownTimer = setInterval(views.reauth.updateCountdown, 1000);
        // w6-reauth-longpoll-fix: no setInterval -- the first poll fires
        // right away, and poll() itself schedules the next one via
        // setTimeout once this one settles.
        views.reauth.poll();
      }).catch(function (e) {
        feedback("reauthResult", e.message, "error");
      });
    },
    poll: function () {
      var challengeId = state.reauth.challengeId;
      // In-flight guard (T19 fix wave 1 item 1): a slow response must never
      // let two status requests for the same challenge overlap in flight.
      if (!challengeId || state.reauth.polling) return;
      state.reauth.polling = true;
      api.request("/api/115/reauth/status?challenge_id=" + encodeURIComponent(challengeId)).then(function (d) {
        if (d.state === "scanned") {
          feedback("reauthResult", "已扫描，请在手机上确认登录。");
        }
        if (d.state === "confirmed") {
          // w6-reauth-longpoll-fix: 115 confirmed the QR, but the cookie
          // exchange itself is a separate follow-up request -- keep
          // polling, same as "scanned".
          feedback("reauthResult", "已确认，正在完成登录…");
        }
        if (d.state === "authenticated") {
          views.reauth.stopPolling();
          state.reauth.challengeId = null;
          feedback("reauthResult", "授权已更新，正在验证…", "success");
          // Return-to-origin (brief w5-reauth-return §1/§2): capture
          // returnTo before close() (which clears it) runs below, and let
          // the dialog close unconditionally -- it must never depend on
          // refreshStatus() settling successfully.
          var returnTo = state.reauth.returnTo;
          refreshStatus().then(function () {
            feedback("reauthResult", "115 已重新授权，转存功能已恢复。", "success");
          }, function () {
            // refreshStatus() rejecting (network hiccup, or the immediate
            // re-check answering non-200) must not strand the dialog on
            // "正在验证…" forever -- the close below is unconditional.
          });
          setTimeout(function () {
            views.reauth.close();
            if (returnTo) {
              router.go(returnTo.tab);
              state.transferFoldersLoaded = false;
              views.transfer.openLibrary(returnTo.linkId, returnTo.title);
            }
          }, 900);
        }
        if (d.state === "expired" || d.state === "cancelled" || d.state === "failed") {
          views.reauth.stopPolling();
          state.reauth.challengeId = null;
          state.reauth.returnTo = null;
          var text = d.state === "expired" ? "二维码已过期，请点击刷新二维码。" : d.state === "cancelled" ? "已取消。" : "授权失败，请刷新二维码重试。";
          feedback("reauthResult", text, "error");
        }
        // "pending": nothing to show yet -- keep polling silently.
        state.reauth.polling = false;
        // w6-reauth-longpoll-fix: drive polling from a single setTimeout
        // chain, not a setInterval -- reschedule ~300ms after a settled 2xx
        // response so a status change is observed almost as soon as this
        // request returns. challengeId is cleared above on every terminal
        // state, so this only reschedules while the challenge is still
        // pending/scanned/confirmed; the in-flight guard above still caps
        // this at one request.
        if (state.reauth.challengeId) state.reauth.pollTimer = setTimeout(views.reauth.poll, 300);
      }).catch(function (e) {
        state.reauth.polling = false;
        if (e && (e.code === "REAUTH_NOT_FOUND" || e.code === "REAUTH_CONSUMED")) {
          // The challenge is gone server-side (404/410, e.g. the QR expired
          // or was cancelled from another tab) -- polling further is
          // pointless.
          views.reauth.stopPolling();
          state.reauth.challengeId = null;
          state.reauth.returnTo = null;
          feedback("reauthResult", "该授权流程已结束，请刷新二维码", "error");
          return;
        }
        // Any other error (network hiccup, transient failure) backs off to
        // the slower 1.5s cadence instead of hammering a struggling
        // upstream; the countdown above is what eventually gives up on
        // this challenge.
        if (state.reauth.challengeId) state.reauth.pollTimer = setTimeout(views.reauth.poll, 1500);
      });
    },
    refresh: function () {
      var previous = state.reauth.challengeId;
      views.reauth.stopPolling();
      state.reauth.challengeId = null;
      var cancelled = previous
        ? api.request("/api/115/reauth/cancel", { method: "POST", body: JSON.stringify({ challenge_id: previous }) }).catch(function () {})
        : Promise.resolve();
      cancelled.then(views.reauth.start);
    },
    open: function () {
      state.reauth.trigger = document.activeElement;
      $("reauthDialog").hidden = false;
      document.body.classList.add("modal-open");
      views.reauth.focusFirst();
      views.reauth.start();
    },
    close: function () {
      views.reauth.stopPolling();
      var challengeId = state.reauth.challengeId;
      state.reauth.challengeId = null;
      state.reauth.returnTo = null;
      $("reauthDialog").hidden = true;
      document.body.classList.remove("modal-open");
      if (state.reauth.trigger) state.reauth.trigger.focus();
      if (challengeId) {
        api.request("/api/115/reauth/cancel", { method: "POST", body: JSON.stringify({ challenge_id: challengeId }) }).catch(function () {});
      }
    },
    init: function () {
      $("reauth115Btn").onclick = views.reauth.open;
      $("reauthClose").onclick = views.reauth.close;
      $("reauthCancel").onclick = views.reauth.close;
      document.querySelector("[data-close-reauth]").onclick = views.reauth.close;
      $("reauthRefresh").onclick = views.reauth.refresh;
      document.addEventListener("keydown", function (e) {
        if ($("reauthDialog").hidden) return;
        if (e.key === "Escape") { views.reauth.close(); return; }
        if (e.key !== "Tab") return;
        var card = document.querySelector("#reauthDialog .modal-card");
        var focusable = Array.prototype.filter.call(
          card.querySelectorAll('button:not(:disabled), [tabindex]'),
          function (el) { return el.offsetParent !== null; }
        );
        if (!focusable.length) return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      });
    }
  };

  // ------------------------------------------------------------------
  // views.library: search home (T5.3). Detail view is added by views.detail
  // (T5.4); this module owns the URL state for both (`media=<id>` route).
  // ------------------------------------------------------------------
  var LIBRARY_RESULT_STATES = ["libraryResults", "libraryEmpty", "libraryError", "libraryNotInstalled"];

  state.library = {
    q: "", type: "all", year: "", provider: [], quality: [], hdr: [], genre: [], source: [], includeDeleted: false,
    sort: "relevance", page: 1, media: null,
    total: 0, pageSize: 24, filtersData: null, suggestTimer: null,
    // Per-channel AbortController + sequence number (T4 request protection):
    // a new request on a channel aborts the previous in-flight one, and a
    // response is only rendered if its sequence is still the latest.
    channels: {}
  };
  // True only right after *we* pushed the history entry that opened the
  // current detail view (openDetail); lets closeDetail() go back to that
  // entry via history.back() instead of pushing a third one.
  state.detailPushed = false;

  // Home content rails (T4 §1, revised T16 §1): exactly these four, in
  // this order -- wording avoids any popularity/trend claim since there is
  // no such data. T4's original first rail (title retired below,
  // `sort=links_desc`) is dropped outright rather than moved to last: its
  // ordering duplicated the existing "链接数量" sort option and a fifth
  // rail added no new information. The first rail is now the server-side
  // deterministic daily recommendations endpoint (never a client-side
  // `sort=` browse query).
  var LIBRARY_RAILS = [
    { key: "today", title: "今日推荐", endpoint: "/api/library/recommendations?limit=12" },
    { key: "year_desc", title: "最近年份", endpoint: "/api/library/search?sort=year_desc&page_size=12" },
    { key: "movie", title: "电影", endpoint: "/api/library/search?type=movie&sort=links_desc&page_size=12" },
    { key: "tv", title: "剧集", endpoint: "/api/library/search?type=tv&sort=links_desc&page_size=12" }
  ];

  function cjkLatinOk(q) {
    var cjk = (q.match(/[一-鿿]/g) || []).length;
    var latin = (q.match(/[A-Za-z]/g) || []).length;
    return cjk >= 1 || latin >= 2;
  }

  // Removes every span (original query substring) for one interpreted
  // dimension from `q`, whole-token and case-insensitively, and collapses
  // whitespace. Used to make interpreted-chip removal actually take the
  // originating text out of the query instead of toggling an unrelated
  // explicit filter.
  function stripSpans(q, spans) {
    if (!spans || !spans.length) return q;
    var lowerSpans = spans.map(function (s) { return String(s).toLowerCase(); });
    var tokens = String(q || "").split(/\s+/).filter(function (t) {
      return t && lowerSpans.indexOf(t.toLowerCase()) === -1;
    });
    return tokens.join(" ");
  }

  views.library = {
    // Fetches `url` on a named channel: any previous in-flight request on
    // the same channel is aborted, and the resolved value is `null` (never
    // rendered) unless this call is still the latest issued on it -- so a
    // slow, superseded response can never clobber a newer one.
    fetchChannel: function (name, url) {
      var ch = state.library.channels[name] || (state.library.channels[name] = { controller: null, seq: 0 });
      if (ch.controller) ch.controller.abort();
      var controller = new AbortController();
      ch.controller = controller;
      var seq = ++ch.seq;
      return api.request(url, { signal: controller.signal }).then(function (d) {
        return seq === ch.seq ? d : null;
      }).catch(function (e) {
        if (e && e.name === "AbortError") return null;
        throw e;
      });
    },
    showState: function (name) {
      LIBRARY_RESULT_STATES.forEach(function (id) { $(id).hidden = id !== name; });
      $("libraryStatus").hidden = name !== "libraryResults";
      $("libraryPagination").hidden = name !== "libraryResults";
    },
    // Disables (or re-enables) the search input, segmented control, search
    // button and filter controls while the index is not installed.
    setLibraryControlsDisabled: function (disabled) {
      [$("libraryQuery"), $("librarySearch")].forEach(function (el) {
        el.disabled = disabled;
        el.setAttribute("aria-disabled", disabled ? "true" : "false");
      });
      document.querySelectorAll('#libraryType [data-value], #libraryFilters input, #libraryFilters select, #libraryFilters button').forEach(function (el) {
        el.disabled = disabled;
        el.setAttribute("aria-disabled", disabled ? "true" : "false");
      });
    },
    parseUrl: function () {
      var p = new URLSearchParams(window.location.search);
      var l = state.library;
      l.q = p.get("q") || "";
      l.type = ["all", "movie", "tv", "unknown"].indexOf(p.get("type")) === -1 ? "all" : p.get("type");
      l.year = p.get("year") || "";
      l.provider = p.get("provider") ? p.get("provider").split(",").filter(Boolean) : [];
      l.quality = p.get("quality") ? p.get("quality").split(",").filter(Boolean) : [];
      l.hdr = p.get("hdr") ? p.get("hdr").split(",").filter(Boolean) : [];
      l.genre = p.get("genre") ? p.get("genre").split(",").filter(Boolean) : [];
      l.source = p.get("source") ? p.get("source").split(",").filter(Boolean) : [];
      l.includeDeleted = p.get("deleted") === "1";
      l.sort = p.get("sort") || "relevance";
      l.page = Math.max(1, parseInt(p.get("page"), 10) || 1);
      l.media = p.get("media") || null;
      return l;
    },
    applyToForm: function () {
      var l = state.library;
      $("libraryQuery").value = l.q;
      document.querySelectorAll('#libraryType [data-value]').forEach(function (b) {
        b.setAttribute("aria-pressed", b.dataset.value === l.type ? "true" : "false");
      });
      if ($("filterYear")) $("filterYear").value = l.year;
      $("filterDeleted").checked = l.includeDeleted;
      $("filterSort").value = l.sort;
      views.library.syncToggleChips("filterProviders", l.provider);
      views.library.syncToggleChips("filterQuality", l.quality);
      views.library.syncToggleChips("filterHdr", l.hdr);
      views.library.syncToggleChips("filterGenre", l.genre);
      views.library.syncToggleChips("filterSource", l.source);
      views.library.updateFiltersBadge();
    },
    syncToggleChips: function (containerId, selected) {
      var box = $(containerId);
      if (!box) return;
      box.querySelectorAll("[data-value]").forEach(function (b) {
        b.setAttribute("aria-pressed", selected.indexOf(b.dataset.value) !== -1 ? "true" : "false");
      });
    },
    pushUrl: function (push) {
      var l = state.library;
      var p = new URLSearchParams();
      p.set("tab", "library");
      if (l.q) p.set("q", l.q);
      if (l.type && l.type !== "all") p.set("type", l.type);
      if (l.year) p.set("year", l.year);
      if (l.provider.length) p.set("provider", l.provider.join(","));
      if (l.quality.length) p.set("quality", l.quality.join(","));
      if (l.hdr.length) p.set("hdr", l.hdr.join(","));
      if (l.genre.length) p.set("genre", l.genre.join(","));
      if (l.source.length) p.set("source", l.source.join(","));
      if (l.includeDeleted) p.set("deleted", "1");
      if (l.sort && l.sort !== "relevance") p.set("sort", l.sort);
      if (l.page && l.page !== 1) p.set("page", String(l.page));
      if (l.media) p.set("media", l.media);
      var url = window.location.pathname + "?" + p.toString();
      // Never push a second history entry for a URL that's already current
      // (e.g. restoring from the URL on load/popstate, or re-entering a
      // state the user is already on) -- replaceState instead.
      if (window.location.origin + url === window.location.href) {
        window.history.replaceState({ tab: "library" }, "", url);
        return;
      }
      if (push) window.history.pushState({ tab: "library" }, "", url);
      else window.history.replaceState({ tab: "library" }, "", url);
    },
    renderSkeleton: function () {
      var box = $("libraryResults");
      var cards = "";
      for (var i = 0; i < 6; i++) cards += '<div class="media-card skeleton-card" aria-hidden="true"><span class="poster"></span></div>';
      box.innerHTML = cards;
      $("libraryStatus").textContent = "正在搜索…";
      views.library.showState("libraryResults");
    },
    // Removes an interpreted dimension's spans from the query text (used
    // when the chip was derived from free text, i.e. `spans[dimension]` is
    // present on the response) rather than toggling an explicit filter.
    clearInterpretedSpan: function (dimension, spans) {
      state.library.q = stripSpans(state.library.q, spans[dimension]);
      $("libraryQuery").value = state.library.q;
    },
    // Common active filters as removable chips (T4 §1: type/year/provider/
    // quality) -- distinct from the free-text "interpreted" chips below,
    // which clear query-text spans instead of an explicit filter.
    explicitChips: function () {
      var l = state.library;
      var chips = [];
      if (l.type !== "all") {
        chips.push({ label: MEDIA_TYPE_LABEL[l.type] || l.type, clear: function () { views.library.setType("all"); } });
      }
      if (l.year) {
        chips.push({ label: "年份 " + l.year, clear: function () { l.year = ""; if ($("filterYear")) $("filterYear").value = ""; } });
      }
      l.provider.forEach(function (value) {
        chips.push({ label: providerLabel(value), clear: function () { views.library.toggleChip("filterProviders", value, "provider"); } });
      });
      l.quality.forEach(function (value) {
        chips.push({ label: value, clear: function () { views.library.toggleChip("filterQuality", value, "quality"); } });
      });
      l.genre.forEach(function (value) {
        chips.push({ label: value, clear: function () { views.library.toggleChip("filterGenre", value, "genre"); } });
      });
      l.source.forEach(function (value) {
        chips.push({
          label: SOURCE_LABEL[value] || String(value).toUpperCase(),
          clear: function () { views.library.toggleChip("filterSource", value, "source"); }
        });
      });
      return chips;
    },
    setType: function (value) {
      state.library.type = value;
      document.querySelectorAll('#libraryType [data-value]').forEach(function (b) {
        b.setAttribute("aria-pressed", b.dataset.value === value ? "true" : "false");
      });
    },
    renderChips: function (interpreted) {
      var box = $("libraryChips");
      interpreted = interpreted || {};
      var spans = interpreted.spans || {};
      var chips = views.library.explicitChips();
      if (interpreted.year) {
        chips.push({
          label: "年份 " + interpreted.year,
          clear: spans.year
            ? function () { views.library.clearInterpretedSpan("year", spans); }
            : function () { state.library.year = ""; $("filterYear").value = ""; }
        });
      }
      if (interpreted.quality) {
        chips.push({
          label: String(interpreted.quality),
          clear: spans.quality
            ? function () { views.library.clearInterpretedSpan("quality", spans); }
            : function () { views.library.toggleChip("filterQuality", interpreted.quality, "quality"); }
        });
      }
      (interpreted.providers || []).forEach(function (pv) {
        chips.push({
          label: providerLabel(pv),
          clear: spans.providers
            ? function () { views.library.clearInterpretedSpan("providers", spans); }
            : function () { views.library.toggleChip("filterProviders", pv, "provider"); }
        });
      });
      (interpreted.corrections || []).forEach(function (c) {
        chips.push({ label: "已按 " + c[1] + " 搜索", clear: null });
      });
      if (!chips.length) { box.hidden = true; box.innerHTML = ""; return; }
      box.hidden = false;
      box.innerHTML = chips.map(function (c, i) {
        return '<span class="chip"><span>' + esc(c.label) + '</span><button type="button" class="chip-remove" data-chip-index="' + i + '" aria-label="移除 ' + esc(c.label) + '">×</button></span>';
      }).join("");
      box.querySelectorAll(".chip-remove").forEach(function (b) {
        b.onclick = function () {
          var chip = chips[parseInt(b.dataset.chipIndex, 10)];
          if (chip && chip.clear) { chip.clear(); views.library.search(true); }
          else { b.closest(".chip").remove(); }
        };
      });
    },
    toggleChip: function (containerId, value, stateKey) {
      var list = state.library[stateKey];
      var idx = list.indexOf(value);
      if (idx === -1) list.push(value); else list.splice(idx, 1);
      views.library.syncToggleChips(containerId, list);
    },
    // Shared poster-card markup for both the results grid and the home
    // rails. y1-cards §1.1: a card shows ONLY the title and one compact
    // "年份 · 类型" line (formatCardMeta) -- genre chips, the 待复核 status
    // badge, the "N 个版本 · M 条链接 · K 个来源" stat and provider text are
    // detail-page-only now, never rendered here.
    mediaCardHtml: function (item) {
      var poster = item.poster_url
        ? '<img src="' + esc(item.poster_url) + '" alt="" loading="lazy">'
        : "";
      var typeIcon = item.media_type === "tv" ? "icon-tv" : item.media_type === "movie" ? "icon-film" : "icon-library";
      var fallback = '<span class="poster-fallback"' + (item.poster_url ? " hidden" : "") + '>' +
        '<svg class="icon" aria-hidden="true"><use href="' + ICONS_URL + '#' + typeIcon + '"></use></svg>' +
        '<span>' + esc(item.title) + '</span></span>';
      var meta = formatCardMeta(item.year, item.media_type);
      // card-meta always renders its wrapper span (even empty) so
      // .card-footer's fixed-height rows (static/app.css) never shift.
      return '<button type="button" class="media-card" data-media-id="' + esc(item.media_id) + '">' +
        '<span class="poster">' + poster + fallback + primaryRatingBadgeHtml(item.primary_rating) + cardInvalidBadgeHtml(item.all_links_invalid) + '</span>' +
        '<span class="card-footer">' +
        '<span class="card-title">' + esc(item.title) + '</span>' +
        '<span class="card-meta">' + esc(meta) + '</span>' +
        '</span>' +
        '</button>';
    },
    bindMediaCards: function (root) {
      root.querySelectorAll(".media-card").forEach(function (el) {
        el.onclick = function () { views.library.openDetail(el.dataset.mediaId); };
        el.onkeydown = function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); views.library.openDetail(el.dataset.mediaId); }
        };
        // CSP-friendly poster broken-image handling (T8 §9): bound here,
        // right after mediaCardHtml's markup is inserted, instead of an
        // inline onerror= attribute.
        var img = el.querySelector(".poster img");
        if (img) {
          img.addEventListener("error", function () {
            img.classList.add("poster-broken");
            var fallback = img.nextElementSibling;
            if (fallback) fallback.hidden = false;
          });
        }
      });
    },
    renderResults: function (d) {
      views.library.setLibraryControlsDisabled(false);
      state.library.total = d.total || 0;
      state.library.pageSize = d.page_size || state.library.pageSize;
      var items = d.items || [];
      var totalPages = Math.max(1, Math.ceil(state.library.total / state.library.pageSize));
      $("libraryStatus").textContent = "找到 " + state.library.total + " 个标题 · 第 " + (d.page || 1) + " / " + totalPages + " 页";
      views.library.renderChips(d.interpreted);
      if (!items.length) { views.library.showState("libraryEmpty"); $("libraryPagination").innerHTML = ""; return; }
      var box = $("libraryResults");
      box.innerHTML = items.map(views.library.mediaCardHtml).join(""); // esc(...) happens inside mediaCardHtml
      views.library.bindMediaCards(box);
      views.library.showState("libraryResults");
      views.library.renderPagination(d.page || 1, totalPages);
    },
    renderPagination: function (page, totalPages) {
      var box = $("libraryPagination");
      function btn(label, target, disabled) {
        return '<button type="button" class="page-btn" data-page="' + target + '"' + (disabled ? " disabled" : "") + '>' + esc(label) + "</button>";
      }
      var html = btn("首页", 1, page <= 1) + btn("上一页", page - 1, page <= 1) +
        '<span class="page-current">' + page + " / " + totalPages + "</span>" +
        btn("下一页", page + 1, page >= totalPages) + btn("末页", totalPages, page >= totalPages);
      box.innerHTML = html;
      box.querySelectorAll(".page-btn:not(:disabled)").forEach(function (b) {
        b.onclick = function () { state.library.page = parseInt(b.dataset.page, 10); views.library.search(true); };
      });
    },
    buildQuery: function () {
      var l = state.library;
      var p = new URLSearchParams();
      if (l.q) p.set("q", l.q);
      p.set("type", l.type);
      if (l.year) p.set("year", l.year);
      if (l.provider.length) p.set("provider", l.provider.join(","));
      if (l.quality.length) p.set("quality", l.quality.join(","));
      if (l.hdr.length) p.set("hdr", l.hdr.join(","));
      if (l.genre.length) p.set("genre", l.genre.join(","));
      if (l.source.length) p.set("source", l.source.join(","));
      p.set("include_deleted", l.includeDeleted ? "1" : "0");
      p.set("sort", l.sort);
      p.set("page", String(l.page));
      p.set("page_size", String(l.pageSize));
      return p.toString();
    },
    // The home view (hero + rails) is shown while browsing with no active
    // query or filter; anything else switches to the results grid.
    isBrowsing: function () {
      var l = state.library;
      return !l.q && l.type === "all" && !l.year && !l.provider.length && !l.quality.length &&
        !l.hdr.length && !l.genre.length && !l.source.length && !l.includeDeleted &&
        l.sort === "relevance" && l.page === 1;
    },
    showBrowseMode: function () {
      $("libraryRails").hidden = false;
      LIBRARY_RESULT_STATES.forEach(function (id) { $(id).hidden = true; });
      $("libraryStatus").hidden = true;
      $("libraryPagination").hidden = true;
      $("libraryChips").hidden = true;
      views.library.loadHero();
      views.library.loadRails();
    },
    showResultsMode: function () {
      $("libraryRails").hidden = true;
      $("libraryHero").hidden = true;
    },
    // Home hero (T4 §1): only ever a record with a real backdrop AND a
    // non-empty short overview -- otherwise stays hidden (the compact
    // gradient search block underneath is always shown regardless).
    loadHero: function () {
      return views.library.fetchChannel("hero", "/api/library/search?has_backdrop=1&sort=links_desc&page_size=6")
        .then(function (d) {
          if (!d) return;
          var items = d.items || [];
          var pick = null;
          for (var i = 0; i < items.length; i++) {
            if (items[i].backdrop_url && items[i].overview_short) { pick = items[i]; break; }
          }
          var hero = $("libraryHero");
          if (!pick) { hero.hidden = true; return; }
          var backdrop = hero.querySelector(".library-hero-backdrop");
          // A stale backdrop_url must never leave the 340px black
          // .library-hero block showing a broken image -- hide the whole
          // hero (the compact gradient search block underneath is always
          // shown regardless). Bound after insertion, not as an inline
          // handler attribute, per the CSP-friendly image-error pattern
          // used throughout this file (T8 §6/§9).
          backdrop.addEventListener("error", function () { hero.hidden = true; });
          backdrop.src = pick.backdrop_url;
          hero.querySelector(".library-hero-type").textContent = MEDIA_TYPE_LABEL[pick.media_type] || "";
          hero.querySelector(".library-hero-title").textContent = pick.title || "";
          hero.querySelector(".library-hero-overview").textContent = pick.overview_short || "";
          hero.querySelector(".library-hero-cta").onclick = function () { views.library.openDetail(pick.media_id); };
          hero.hidden = false;
        }).catch(function () { $("libraryHero").hidden = true; });
    },
    // Content rails (T4 §1): four fixed rails, each `page_size=12`, hidden
    // individually when empty; wording never claims popularity/trend data.
    loadRails: function () {
      var box = $("libraryRails");
      box.innerHTML = LIBRARY_RAILS.map(function (rail) {
        return '<section class="rail" data-rail="' + rail.key + '" hidden>' +
          '<div class="rail-head"><h3>' + esc(rail.title) + '</h3></div>' +
          '<div class="rail-track"></div></section>';
      }).join("");
      LIBRARY_RAILS.forEach(function (rail) {
        views.library.fetchChannel("rail:" + rail.key, rail.endpoint)
          .then(function (d) {
            if (!d) return;
            var items = d.items || [];
            var section = box.querySelector('.rail[data-rail="' + rail.key + '"]');
            if (!section || !items.length) { if (section) section.hidden = true; return; }
            section.querySelector(".rail-track").innerHTML = items.map(views.library.mediaCardHtml).join("");
            views.library.bindMediaCards(section);
            section.hidden = false;
          }).catch(function () { /* rails are optional home decoration */ });
      });
    },
    runSearch: function () {
      views.library.renderSkeleton();
      return views.library.fetchChannel("search", "/api/library/search?" + views.library.buildQuery())
        .then(function (d) {
          if (!d) return;
          views.library.renderResults(d);
        })
        .catch(function (e) {
          if (e && e.code === "LIBRARY_NOT_INSTALLED") {
            views.library.setLibraryControlsDisabled(true);
            views.library.showState("libraryNotInstalled");
            return;
          }
          $("libraryErrorText").textContent = e.message || "搜索失败";
          views.library.showState("libraryError");
        });
    },
    render: function () {
      if (views.library.isBrowsing()) views.library.showBrowseMode();
      else { views.library.showResultsMode(); views.library.runSearch(); }
    },
    search: function (push) {
      views.library.pushUrl(push !== false);
      views.library.render();
    },
    loadFilters: function () {
      return api.request("/api/library/filters").then(function (d) {
        state.library.filtersData = d;
        var yearSel = $("filterYear");
        (d.years || []).forEach(function (y) {
          var opt = document.createElement("option");
          opt.value = String(y.value);
          opt.textContent = y.label;
          if (y.count === 0) opt.disabled = true;
          yearSel.appendChild(opt);
        });
        yearSel.value = state.library.year;
        views.library.renderToggleGroup("filterProviders", d.providers || [], "provider");
        views.library.renderToggleGroup("filterQuality", d.qualities || [], "quality", true);
        views.library.renderToggleGroup("filterHdr", d.hdr || [], "hdr", true);
        views.library.renderToggleGroup("filterGenre", d.genres || [], "genre", true);
        // The "sources" facet carries no label (unlike genres, which are
        // TMDB genre names already) -- localise each value through
        // SOURCE_LABEL before handing it to renderToggleGroup.
        var sourceOptions = (d.sources || []).map(function (s) {
          return { value: s.value, count: s.count, label: SOURCE_LABEL[s.value] || String(s.value).toUpperCase() };
        });
        views.library.renderToggleGroup("filterSource", sourceOptions, "source", true);
      }).catch(function () { /* filters are optional; leave defaults */ });
    },
    // `singleSelect` (I2): quality/hdr are read by the backend as a
    // scalar (only provider is comma-separated, per contract §7.1), so
    // their chip groups behave like a radio group -- selecting one clears
    // any other selection in the same group, and re-clicking the pressed
    // chip clears it entirely.
    renderToggleGroup: function (containerId, options, stateKey, singleSelect) {
      var box = $(containerId);
      box.innerHTML = options.map(function (o) {
        return '<button type="button" class="chip-toggle" data-value="' + esc(o.value) + '" aria-pressed="' +
          (state.library[stateKey].indexOf(String(o.value)) !== -1 ? "true" : "false") + '"' +
          (o.count === 0 ? " disabled" : "") + '>' + esc(o.label) + "</button>";
      }).join("");
      box.querySelectorAll("[data-value]:not(:disabled)").forEach(function (b) {
        b.onclick = function () {
          var pressed = b.getAttribute("aria-pressed") === "true";
          var list = state.library[stateKey];
          if (singleSelect) {
            list.length = 0;
            if (!pressed) list.push(b.dataset.value);
          } else {
            var idx = list.indexOf(b.dataset.value);
            if (pressed && idx !== -1) list.splice(idx, 1);
            else if (!pressed && idx === -1) list.push(b.dataset.value);
          }
          views.library.syncToggleChips(containerId, list);
          views.library.updateFiltersBadge();
          state.library.page = 1;
          views.library.search(true);
        };
      });
    },
    hideSuggest: function () {
      var box = $("librarySuggest");
      box.hidden = true;
      box.innerHTML = "";
      $("libraryQuery").setAttribute("aria-expanded", "false");
    },
    renderSuggest: function (items) {
      var box = $("librarySuggest");
      if (!items.length) { views.library.hideSuggest(); return; }
      box.innerHTML = items.map(function (it, i) {
        return '<li role="option" id="suggestOption' + i + '" data-media-id="' + esc(it.media_id) + '" data-title="' + esc(it.title) + '">' +
          esc(it.title) + (it.year ? '<span class="suggest-year">' + esc(it.year) + "</span>" : "") + "</li>";
      }).join("");
      box.hidden = false;
      $("libraryQuery").setAttribute("aria-expanded", "true");
      box.querySelectorAll("[role=option]").forEach(function (li) {
        li.onclick = function () { views.library.openDetail(li.dataset.mediaId); views.library.hideSuggest(); };
      });
    },
    fetchSuggest: function () {
      var q = $("libraryQuery").value.trim();
      if (!cjkLatinOk(q)) { views.library.hideSuggest(); return; }
      views.library.fetchChannel("suggest", "/api/library/suggest?q=" + encodeURIComponent(q)).then(function (d) {
        if (!d) return;
        views.library.renderSuggest(d.items || []);
      }).catch(function () { views.library.hideSuggest(); });
    },
    // Opening a detail pushes exactly one history entry; closing it (or
    // the browser back button) removes `media` from the URL again.
    openDetail: function (mediaId) {
      state.library.media = mediaId;
      state.detailPushed = true;
      views.library.pushUrl(true);
      $("libraryHome").hidden = true;
      $("libraryDetail").hidden = false;
      if (views.detail) views.detail.open(mediaId);
    },
    // If we're the ones who pushed the history entry for this detail view,
    // go back to it (history.back()) rather than pushing a third entry --
    // the popstate handler (onEnter) restores the results view from the
    // resulting URL. Otherwise (e.g. the detail was reached directly via a
    // shared URL) fall back to replaceState, since there's no entry to
    // return to.
    closeDetail: function () {
      state.library.media = null;
      // The detail view's provider-logo tab selection reuses l.provider's
      // URL slot (x1-provider-ui §2.2) while `media` is set -- clear it so
      // it can never leak into the (unrelated) search provider filter of
      // the same name once we're back in browse/results mode. The
      // history.back() branch below re-derives the correct value from the
      // restored URL via onEnter()'s parseUrl() regardless, but resetting
      // it here keeps state.library internally consistent either way.
      state.library.provider = [];
      if (state.detailPushed) {
        state.detailPushed = false;
        window.history.back();
        return;
      }
      views.library.pushUrl(true);
      $("libraryDetail").hidden = true;
      $("libraryHome").hidden = false;
      views.library.render();
    },
    // Restoring from the URL (initial load or a `popstate`) only ever
    // normalises the URL via `pushUrl(false)` (replaceState) -- it never
    // pushes a new history entry; only an explicit user action (search,
    // filter, pagination, opening a card) does that.
    onEnter: function () {
      // A detail restored from the URL (initial load, popstate, or
      // returning to the library tab with `media=` still in the URL) was
      // not pushed during *this* visit -- closeDetail() must not treat it
      // as one it can history.back() out of (T7 §7).
      state.detailPushed = false;
      views.library.parseUrl();
      views.library.applyToForm();
      views.library.pushUrl(false);
      if (state.library.media) {
        $("libraryHome").hidden = true;
        $("libraryDetail").hidden = false;
        if (views.detail) views.detail.open(state.library.media);
      } else {
        $("libraryDetail").hidden = true;
        $("libraryHome").hidden = false;
        views.library.render();
      }
    },
    // T16 §3: one entry point for "activate the library tab", used by
    // router.go() whether or not library was already the active tab --
    // resets query/filters/detail/provider selection, closes the filters
    // popover, replaces the URL down to the bare `?tab=library` (never
    // pushState -- this is a reset, not a new navigable state) and scrolls
    // back to the top. onEnter() (called next, via router.activate()) then
    // re-derives every state.library field from that now-empty URL and
    // renders the home view -- so there is no separate reset-state copy to
    // keep in sync with parseUrl()'s field list.
    goHome: function () {
      views.library.closeFilters(false);
      var url = window.location.pathname + "?tab=library";
      window.history.replaceState({ tab: "library" }, "", url);
      router.activate("library");
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    },
    filtersFocusable: function () {
      var panel = $("libraryFilters");
      return Array.prototype.filter.call(
        panel.querySelectorAll('button:not(:disabled), input:not([type="hidden"]):not(:disabled), select:not(:disabled), [tabindex]'),
        function (el) { return el.offsetParent !== null; }
      );
    },
    // T16 §6.1: positions the popover from the toggle button's own
    // getBoundingClientRect(), flipping up/left within the viewport --
    // never a fixed top/right pinned to a single breakpoint. A no-op at
    // <=650px, where CSS turns the panel into a full-width bottom sheet.
    positionFilters: function () {
      var panel = $("libraryFilters");
      if (window.matchMedia("(max-width: 650px)").matches) {
        panel.style.top = "";
        panel.style.left = "";
        return;
      }
      var margin = 8;
      var btnRect = $("libraryFiltersToggle").getBoundingClientRect();
      panel.style.left = margin + "px";
      panel.style.top = margin + "px";
      var panelRect = panel.getBoundingClientRect();
      var vw = window.innerWidth, vh = window.innerHeight;
      var left = btnRect.right - panelRect.width;
      if (left < margin) left = btnRect.left;
      if (left + panelRect.width > vw - margin) left = vw - margin - panelRect.width;
      if (left < margin) left = margin;
      var top = btnRect.bottom + margin;
      if (top + panelRect.height > vh - margin) top = btnRect.top - panelRect.height - margin;
      if (top < margin) top = margin;
      panel.style.left = left + "px";
      panel.style.top = top + "px";
    },
    openFilters: function () {
      state.library.filtersTrigger = $("libraryFiltersToggle");
      $("libraryFilters").hidden = false;
      $("libraryFiltersScrim").hidden = false;
      $("libraryFiltersToggle").setAttribute("aria-expanded", "true");
      views.library.positionFilters();
      if (window.matchMedia("(max-width: 650px)").matches) document.body.classList.add("modal-open");
      var focusable = views.library.filtersFocusable();
      if (focusable.length) focusable[0].focus();
    },
    // `returnFocus`: false for programmatic closes (new search, tab
    // switch, browser back) where focus has already moved elsewhere; true
    // for user-initiated closes (Escape, the close button, the scrim) so
    // focus returns to the button that opened the popover.
    closeFilters: function (returnFocus) {
      if ($("libraryFilters").hidden) return;
      $("libraryFilters").hidden = true;
      $("libraryFiltersScrim").hidden = true;
      $("libraryFiltersToggle").setAttribute("aria-expanded", "false");
      document.body.classList.remove("modal-open");
      if (returnFocus && state.library.filtersTrigger) state.library.filtersTrigger.focus();
    },
    // T16 §5: a small selected-count badge on the toggle button -- never a
    // database total, only how many filter values are currently active.
    updateFiltersBadge: function () {
      var l = state.library;
      var count = l.provider.length + l.quality.length + l.hdr.length + l.genre.length +
        l.source.length + (l.year ? 1 : 0) + (l.includeDeleted ? 1 : 0);
      var badge = $("libraryFiltersBadge");
      badge.hidden = count === 0;
      badge.textContent = count === 0 ? "" : String(count);
    },
    init: function () {
      document.querySelectorAll('#libraryType [data-value]').forEach(function (b) {
        b.onclick = function () {
          views.library.setType(b.dataset.value);
          state.library.page = 1;
          views.library.search(true);
        };
      });
      $("libraryFiltersToggle").onclick = function () {
        if ($("libraryFilters").hidden) views.library.openFilters();
        else views.library.closeFilters(true);
      };
      $("libraryFiltersClose").onclick = function () { views.library.closeFilters(true); };
      $("libraryFiltersScrim").onclick = function () { views.library.closeFilters(true); };
      // Escape closes the popover; Tab/Shift+Tab cycle inside it instead of
      // escaping to the page behind it (T16 §6.2 focus trap -- same pattern
      // as views.transfer's modal dialog).
      document.addEventListener("keydown", function (e) {
        if ($("libraryFilters").hidden) return;
        if (e.key === "Escape") { e.preventDefault(); views.library.closeFilters(true); return; }
        if (e.key !== "Tab") return;
        var focusable = views.library.filtersFocusable();
        if (!focusable.length) return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      });
      // position:fixed means the popover never follows the page's own
      // scroll -- close it rather than let it drift away from the button
      // (T16 §6.2). A resize (e.g. rotating a device) repositions instead.
      window.addEventListener("resize", function () {
        if (!$("libraryFilters").hidden) views.library.positionFilters();
      });
      window.addEventListener("scroll", function () {
        if (!$("libraryFilters").hidden) views.library.closeFilters(false);
      });
      $("librarySearch").onclick = function () {
        views.library.closeFilters(false);
        state.library.q = $("libraryQuery").value.trim();
        state.library.page = 1;
        views.library.hideSuggest();
        views.library.search(true);
      };
      $("libraryQuery").addEventListener("keydown", function (e) {
        var box = $("librarySuggest");
        var options = box.querySelectorAll("[role=option]");
        var active = box.querySelector('[aria-selected="true"]');
        if (e.key === "Escape") { e.preventDefault(); views.library.hideSuggest(); return; }
        if (!options.length) {
          if (e.key === "Enter") { $("librarySearch").click(); }
          return;
        }
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          var idx = active ? Array.prototype.indexOf.call(options, active) : -1;
          idx = e.key === "ArrowDown" ? Math.min(idx + 1, options.length - 1) : Math.max(idx - 1, 0);
          options.forEach(function (o) { o.removeAttribute("aria-selected"); });
          options[idx].setAttribute("aria-selected", "true");
          $("libraryQuery").setAttribute("aria-activedescendant", options[idx].id);
        } else if (e.key === "Enter") {
          if (active) { views.library.openDetail(active.dataset.mediaId); views.library.hideSuggest(); }
          else { $("librarySearch").click(); }
        }
      });
      $("libraryQuery").addEventListener("input", function () {
        clearTimeout(state.library.suggestTimer);
        var q = $("libraryQuery").value.trim();
        if (!cjkLatinOk(q)) { views.library.hideSuggest(); return; }
        state.library.suggestTimer = setTimeout(views.library.fetchSuggest, 200);
      });
      document.addEventListener("click", function (e) {
        if (!e.target.closest(".search-field")) views.library.hideSuggest();
      });
      $("filterYear").onchange = function () { state.library.year = $("filterYear").value; views.library.updateFiltersBadge(); state.library.page = 1; views.library.search(true); };
      $("filterSort").onchange = function () { state.library.sort = $("filterSort").value; state.library.page = 1; views.library.search(true); };
      $("filterDeleted").onchange = function () { state.library.includeDeleted = $("filterDeleted").checked; views.library.updateFiltersBadge(); state.library.page = 1; views.library.search(true); };
      $("libraryClearSearch").onclick = function () { $("libraryQuery").value = ""; state.library.q = ""; state.library.page = 1; views.library.search(true); };
      $("libraryRetry").onclick = function () { views.library.search(false); };
    }
    // T16 §3: the home page prints no internal totals any more -- the
    // "N titles / M links" helper text keeps its static template copy
    // (search hints), never a media/link count read from tmdb-status.
    // Settings/diagnostics still expose those numbers via their own
    // status views (#statusList / tmdbScrapeStatus), unaffected.
  };

  // ------------------------------------------------------------------
  // toast: a small ephemeral status message (reveal/copy feedback).
  // ------------------------------------------------------------------
  var toastTimer = null;
  function toast(message, kind) {
    var el = $("toast");
    el.textContent = message;
    el.className = "toast" + (kind ? " " + kind : "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 3200);
  }

  // ------------------------------------------------------------------
  // views.detail: cinematic media detail (T4/T5.4, rebuilt for T18 §13:
  // no expand). `/api/library/media/<id>[?provider=]` now returns every
  // resource group's safe link summary inline -- every group's links
  // render immediately, with no expand/collapse step and no
  // `/api/library/resource/<group_id>` fetch at all. URLs/access codes
  // still only ever reach the DOM after an explicit user click on
  // 打开/复制, and only into revealCode/revealFallback (never into data-*
  // attributes or logs).
  // ------------------------------------------------------------------
  // `provider` is the detail-page provider-logo tablist's current
  // selection ("" == 全部). T18 §14.1 (I1 fix wave): the FETCH is
  // provider-scoped whenever `state.library.provider` carries a value,
  // whether that happened at open() time (entering from a filtered
  // search/rail card, a direct `?provider=115&media=<id>` URL, or a
  // refresh/back/forward onto one) or from an interactive tab switch
  // WITHIN an already-open detail (selectProvider below) -- in every case
  // the server has already dropped every other provider's groups/links/
  // labels/facets, so `state.detail.media` never holds them at all. A tab
  // switch is therefore a real re-fetch + full re-render, never a
  // client-side re-filter of stale data.
  state.detail = { provider: "", includeDeleted: false };
  // Round 16: the detail fetch URL. `include_deleted=1` rides along
  // whenever the library's own "包含已失效" filter is on (a card opened from
  // those results, or a `deleted=1` deep link) so the server keeps groups
  // whose links are all invalid/deleted and the page can show them marked.
  function detailRequestUrl(mediaId, provider) {
    var params = [];
    if (provider) params.push("provider=" + encodeURIComponent(provider));
    state.detail.includeDeleted = !!state.library.includeDeleted;
    if (state.detail.includeDeleted) params.push("include_deleted=1");
    return "/api/library/media/" + encodeURIComponent(mediaId) + (params.length ? "?" + params.join("&") : "");
  }

  views.detail = {
    currentCode: "",
    fallbackTimer: null,
    hideReveal: function () {
      $("revealPanel").hidden = true;
      $("revealCode").textContent = "••••";
      $("revealFallbackRow").hidden = true;
      $("revealFallback").value = "";
    },
    showFallback: function (text) {
      $("revealPanel").hidden = false;
      $("revealFallbackRow").hidden = false;
      $("revealFallback").value = text;
    },
    toggleCode: function () {
      var el = $("revealCode");
      if (!views.detail.currentCode) return;
      var revealing = el.textContent === "••••";
      el.textContent = revealing ? views.detail.currentCode : "••••";
      clearTimeout(views.detail.fallbackTimer);
      if (revealing) {
        views.detail.fallbackTimer = setTimeout(function () { el.textContent = "••••"; }, 5000);
      }
    },
    reveal: function (linkId, mode, btn) {
      var originalDisabled = btn.disabled;
      btn.disabled = true;
      return api.request("/api/library/link/" + encodeURIComponent(linkId) + "/reveal", { method: "POST" }).then(function (d) {
        views.detail.currentCode = d.access_code || "";
        $("revealPanel").hidden = false;
        $("revealCode").textContent = "••••";
        $("revealFallbackRow").hidden = true;
        if (mode === "open" && /^https?:\/\//i.test(d.url)) window.open(d.url, "_blank", "noopener");
        var clipboardText = mode === "copy" ? d.url + (d.access_code ? "\n访问码：" + d.access_code : "") : d.access_code;
        if (!clipboardText) { btn.disabled = originalDisabled; return; }
        return navigator.clipboard.writeText(clipboardText).then(function () {
          toast(mode === "open" ? "访问码已复制" : "已复制到剪贴板", "success");
        }).catch(function () {
          views.detail.showFallback(clipboardText);
        });
      }).catch(function (e) {
        toast(e.message || "获取链接失败", "error");
      }).then(function () { btn.disabled = originalDisabled; });
    },
    // w6-contract §UI item 2: queues a group's links for an immediate
    // (priority) check regardless of when they were last checked. Success
    // disables the button for 60s (the backend's own per-group cooldown --
    // a second click inside that window would just 429 anyway); a 429 (or
    // any other error) re-enables it immediately and surfaces the
    // backend's own message.
    recheck: function (groupId, btn) {
      btn.disabled = true;
      return api.request("/api/library/resource/" + encodeURIComponent(groupId) + "/recheck", { method: "POST" }).then(function (d) {
        var message = "已加入检测队列（" + (d.queued || 0) + " 条）";
        if (d.skipped_disabled) message += "，" + d.skipped_disabled + " 条所属网盘未开启检测";
        toast(message, "success");
        setTimeout(function () { btn.disabled = false; }, 60000);
      }).catch(function (e) {
        btn.disabled = false;
        toast(e.message || "加入检测队列失败", "error");
      });
    },
    // §13.3: a deleted link keeps its row (for audit) but every action
    // button on it is disabled. w6-contract: an invalid (checked, not
    // live) link also keeps its row but only disables 转存到 115 -- the
    // user may still try 打开/复制. A native `disabled` button never fires
    // `click`, so bindLinkActions below needs no extra guard either way.
    actionButtons: function (link) {
      var actions = link.actions || [];
      if (!actions.length) return '<span class="link-unavailable">链接不可用</span>';
      var disabledAttr = link.deleted ? " disabled" : "";
      var transferDisabledAttr = (link.deleted || link.invalid) ? " disabled" : "";
      return actions.map(function (action) {
        if (action === "transfer") {
          return '<button type="button" class="link-action link-transfer primary" data-link-action="transfer" data-link-id="' + esc(link.link_id) + '"' + transferDisabledAttr + '>转存到 115</button>';
        }
        if (action === "open") {
          return '<button type="button" class="link-action" data-link-action="open" data-link-id="' + esc(link.link_id) + '"' + disabledAttr + '>打开</button>';
        }
        if (action === "copy") {
          var label = link.provider === "ed2k" ? "复制 ed2k" : "复制";
          return '<button type="button" class="link-action" data-link-action="copy" data-link-id="' + esc(link.link_id) + '"' + disabledAttr + '>' + label + "</button>";
        }
        return "";
      }).join("");
    },
    // §12.4/§13.3: provider logo fixed 18px (static/app.css), label/remark
    // each single-line-ellipsis with the full text kept in `title` for
    // accessibility/hover -- actions stay right on desktop and wrap onto
    // their own >=44px row on small screens (CSS only, same markup).
    linkRowHtml: function (link) {
      var meta = PROVIDER_META_BY_CODE[link.provider];
      var symbol = meta ? meta.symbol : "provider-link";
      var labelText = providerLabel(link.provider) + " · " + (link.label || "");
      // w6-contract §UI item 1 / round 16: one status word right after the
      // provider label (not after the date like the old pill was) -- red
      // 已失效 for deleted-at-source or checker-invalid, green 有效, orange
      // 待确认, grey 未检测 (linkStatusBadgeHtml above).
      var statusHtml = linkStatusBadgeHtml(link);
      return '<div class="link-row' + (link.deleted ? " link-row-deleted" : "") + '" data-link-id="' + esc(link.link_id) + '">' +
        '<span class="link-provider" title="' + esc(labelText) + '"><svg class="link-provider-icon provider-logo" aria-hidden="true"><use href="' + ICONS_URL + '#' + symbol + '"></use></svg><span class="link-provider-text">' + esc(labelText) + '</span></span>' +
        statusHtml +
        '<span class="link-remark" title="' + esc(link.remark || "") + '">' + esc(link.remark || "") + '</span>' +
        '<span class="link-date">' + esc(link.created_at || "") + '</span>' +
        '<span class="link-actions">' + views.detail.actionButtons(link) + '</span>' +
        '</div>';
    },
    // "S01-S03 E01-E12 · 全季" -- built only from fields that are actually
    // present; never fabricates a season/episode range the backend didn't
    // send.
    seasonLabel: function (g) {
      if (g.season_from == null) return "";
      function pad(n) { return n < 10 ? "0" + n : String(n); }
      var label = (g.season_to != null && g.season_to !== g.season_from)
        ? "S" + pad(g.season_from) + "-S" + pad(g.season_to)
        : "S" + pad(g.season_from);
      if (g.episode_from != null) {
        label += " " + ((g.episode_to != null && g.episode_to !== g.episode_from)
          ? "E" + pad(g.episode_from) + "-E" + pad(g.episode_to)
          : "E" + pad(g.episode_from));
      }
      if (g.complete_season) label += " · 全季";
      return label;
    },
    // T18 §12.1/§12.3: only the three normalised spec classes the backend
    // sends in `group.specs` -- resolution/dynamic_range/source, each
    // rendered at most once, with a vendored decorative icon
    // (aria-hidden) plus the always-readable short text value, and an
    // aria-label on the wrapping element ("分辨率 4K"). A key absent from
    // `specs` (unrecognised/missing on the backend) renders no chip at
    // all -- never an empty placeholder. Codec/audio/subtitle/tags/
    // provider-name chips are gone from this row entirely (§12.1).
    specRowHtml: function (specs) {
      var order = ["resolution", "dynamic_range", "source"];
      return order.filter(function (k) { return specs && specs[k]; }).map(function (k) {
        var spec = specs[k];
        var symbol = specSymbol(k, spec.value);
        var label = (SPEC_CATEGORY_LABEL[k] || k) + " " + spec.value;
        return '<span class="spec-item" aria-label="' + esc(label) + '">' +
          '<svg class="spec-icon" aria-hidden="true"><use href="' + ICONS_URL + '#' + symbol + '"></use></svg>' +
          '<span class="spec-value">' + esc(spec.value) + '</span></span>';
      }).join("");
    },
    // T18 §13: no expand/collapse -- every group's links render inline,
    // right here, from the data already in `group.links` (no separate
    // `/api/library/resource/<group_id>` fetch, ever). An empty group
    // shows "暂无可用链接" instead of blank space. `code` (optional) is
    // the currently-selected provider tab -- when given, a mixed-provider
    // group's link rows and displayed count are restricted to just that
    // provider (the live-only total across every provider when omitted/
    // 全部 -- I2 below).
    groupRowHtml: function (group, code) {
      var reasons = (group.review_reason || []).map(function (reasonCode) { return REVIEW_REASON_LABEL[reasonCode] || reasonCode; }).join(" · ");
      var season = views.detail.seasonLabel(group);
      var specsHtml = views.detail.specRowHtml(group.specs);
      var links = (group.links || []).filter(function (l) { return !code || l.provider === code; });
      // I2: `links` still includes deleted rows (they render, disabled,
      // for audit -- §13.3), so `links.length` would inflate the count by
      // any deleted link of this provider. `group.providers[code]` is the
      // server's live-only count for that provider; the unfiltered (全部)
      // branch can't reuse `group.link_count` for the same reason -- it
      // isn't live-only either -- so it sums `group.providers`' own
      // live-only per-provider counts instead (groupLiveLinkCount above).
      var linkCount = code ? ((group.providers && group.providers[code]) || 0) : groupLiveLinkCount(group);
      var linksHtml = links.length
        ? links.map(views.detail.linkRowHtml).join("")
        : '<div class="group-links-empty">暂无可用链接</div>';
      // Round 16: a group kept only because include_deleted asked for it
      // has rows but a live count of 0 -- say so instead of "0 条链接".
      var countText = linkCount > 0 ? esc(linkCount) + " 条链接" : (links.length ? "全部失效" : "0 条链接");
      var titleText = group.display_title || "";
      return '<div class="detail-group" data-group-id="' + esc(group.group_id) + '">' +
        '<div class="group-summary">' +
        '<div class="group-title-row">' +
        '<span class="group-title" title="' + esc(titleText) + '">' + esc(titleText) + '</span>' +
        '<span class="group-count">' + countText + '</span>' +
        '<button type="button" class="tertiary group-recheck-btn" data-group-id="' + esc(group.group_id) + '">重新检测</button>' +
        '</div>' +
        (season ? '<div class="group-season">' + esc(season) + '</div>' : "") +
        (specsHtml ? '<div class="group-specs">' + specsHtml + '</div>' : "") +
        (group.needs_review ? '<span class="badge-review">待复核' + (reasons ? "：" + esc(reasons) : "") + '</span>' : "") +
        '</div>' +
        '<div class="group-links">' + linksHtml + '</div>' +
        '</div>';
    },
    // x1-provider-ui §2.2/§5.1: the RE0-style provider-logo tablist above
    // the resource list. `code` is "" for the 全部 tab (only rendered when
    // there is more than one real provider on this title -- a single-
    // provider title shows just that one tab, already selected, per §3.3
    // "只有一个来源时可直接选中该来源并隐藏全部，但仍保留来源区域"). I1:
    // `scopedFetch` is true whenever the response this tablist is built
    // from came from a `?provider=` request (the server has already
    // dropped every other provider, so `facets` itself only ever has one
    // entry) -- 全部 must still be offered so the user can get back to the
    // unfiltered view, even though it never comes from `facets.length`.
    providerTabId: function (code) { return "providerTab-" + esc(code || "all"); },
    renderProviderTabs: function (facets, selected, scopedFetch) {
      var tabs = (facets.length > 1 || scopedFetch) ? [{ code: "", label: "全部", symbol: null, count: null }] : [];
      facets.forEach(function (f) {
        var meta = PROVIDER_META_BY_CODE[f.provider];
        tabs.push({ code: f.provider, label: f.label || providerLabel(f.provider), symbol: meta ? meta.symbol : "provider-link", count: f.link_count });
      });
      return tabs.map(function (t) {
        var isSelected = t.code === selected;
        var icon = t.symbol ? '<svg class="provider-logo" aria-hidden="true"><use href="' + ICONS_URL + '#' + t.symbol + '"></use></svg>' : "";
        var count = t.count != null ? '<span class="provider-tab-count">' + esc(t.count) + "</span>" : "";
        return '<button type="button" role="tab" id="' + views.detail.providerTabId(t.code) + '" class="provider-tab" data-provider="' + esc(t.code) + '" ' +
          'aria-selected="' + (isSelected ? "true" : "false") + '" aria-controls="libraryProviderPanel" tabindex="' + (isSelected ? "0" : "-1") + '">' +
          icon + '<span class="provider-tab-label">' + esc(t.label) + '</span>' + count + '</button>';
      }).join("");
    },
    // §14.4: one rating unit -- source mark (vendored brand icon, or a
    // plain text badge when there is none, e.g. TVmaze), "x.x/10", and a
    // compact vote count with the exact value in `title`. A safe https
    // `url` makes the whole unit an (existing external-link-policy)
    // button -- window.open(url, "_blank", "noopener") only. Review fix:
    // https only, not http(s) like reveal's window.open above -- every
    // real rating source (TMDB/IMDb/TVmaze) is always https, so this is
    // strictly narrower defense-in-depth, not a functional restriction.
    // No url (or a non-https one) renders a plain, non-interactive badge
    // instead.
    ratingUnitHtml: function (source, entry) {
      // Review fix: a non-numeric score (missing/null/NaN/a string) must
      // never render as the literal text "NaN/10" -- skip this source's
      // unit entirely (ratingsRowHtml below filters out the empty result).
      if (typeof entry.score !== "number" || !isFinite(entry.score)) return "";
      var label = RATING_SOURCE_LABEL[source] || source;
      var symbol = RATING_SOURCE_SYMBOL[source];
      var icon = symbol
        ? '<svg class="rating-icon" aria-hidden="true"><use href="' + ICONS_URL + '#' + symbol + '"></use></svg>'
        : '<span class="rating-badge-text" aria-hidden="true">' + esc(label) + '</span>';
      var scoreText = (Math.round(entry.score * 10) / 10).toFixed(1) + "/10";
      var votesText = formatVotes(entry.votes);
      var exactVotes = entry.votes != null ? String(entry.votes).replace(/\B(?=(\d{3})+(?!\d))/g, ",") : "";
      var ariaLabel = label + " " + scoreText + (exactVotes ? "，" + exactVotes + " 人评分" : "");
      var body = icon + '<span class="rating-score">' + esc(scoreText) + "</span>" +
        (votesText ? '<span class="rating-votes">(' + esc(votesText) + ")</span>" : "");
      var titleAttr = exactVotes ? ' title="' + esc(exactVotes + " 次评分") + '"' : "";
      if (entry.url && /^https:\/\//i.test(entry.url)) {
        return '<button type="button" class="rating-unit" data-source="' + esc(source) + '" data-rating-url="' + esc(entry.url) + '" aria-label="' + esc(ariaLabel) + '"' + titleAttr + '>' + body + '</button>';
      }
      return '<span class="rating-unit" data-source="' + esc(source) + '" aria-label="' + esc(ariaLabel) + '"' + titleAttr + '>' + body + '</span>';
    },
    // §14.4: one unit per AVAILABLE source only (never a placeholder for a
    // missing one); all missing -> "暂无评分数据", never a row of "—".
    // Ratings are independent of provider filtering/match_status/115
    // transfer -- media.ratings is the same regardless of `?provider=`.
    ratingsRowHtml: function (media) {
      var ratings = media.ratings || {};
      var units = RATING_SOURCE_ORDER.filter(function (s) { return ratings[s]; })
        .map(function (s) { return views.detail.ratingUnitHtml(s, ratings[s]); })
        .filter(Boolean); // a non-numeric score renders "" above -- drop it
      if (!units.length) return '<div class="detail-ratings detail-ratings-empty">暂无评分数据</div>';
      return '<div class="detail-ratings" role="group" aria-label="评分">' + units.join("") + '</div>';
    },
    bindRatingUnits: function (root) {
      root.querySelectorAll("[data-rating-url]").forEach(function (btn) {
        if (btn.disabled) return;
        btn.onclick = function () {
          var url = btn.dataset.ratingUrl;
          if (/^https:\/\//i.test(url)) window.open(url, "_blank", "noopener");
        };
      });
    },
    matchesProvider: function (group, code) {
      if (!code || !!(group.providers && group.providers[code])) return true;
      // Round 16: under include_deleted a group may have only non-live rows
      // of this provider (live `providers` count 0) -- still show it there.
      return !!state.detail.includeDeleted && (group.links || []).some(function (l) { return l.provider === code; });
    },
    // Renders `#detailGroupList` from `state.detail.media.groups`, filtered
    // to the currently-selected provider tab (`state.detail.provider`; ""
    // means 全部, no filtering). Called by render() below, itself called
    // after every fetch (open() and, since I1, selectProvider() too) --
    // this function itself issues no request, it only renders whatever
    // `state.detail.media` currently holds. Because every fetch is now
    // provider-scoped (§14.1) whenever a provider is selected, every
    // group/link here already matches `code` and this filter is a no-op
    // in the common case; it still matters for the unfiltered ("全部")
    // render, where it must exclude a mixed-provider group's OTHER-
    // provider links too, not just whole non-matching groups (groupRowHtml's
    // own `code` argument).
    renderGroupList: function () {
      var code = state.detail.provider;
      var groups = (state.detail.media.groups || []).filter(function (g) { return views.detail.matchesProvider(g, code); });
      var emptyText = code ? "该来源下暂无资源，请切换其他网盘来源" : "暂无资源";
      $("detailGroupList").innerHTML = groups.length
        ? groups.map(function (g) { return views.detail.groupRowHtml(g, code); }).join("")
        : '<div class="empty">' + esc(emptyText) + "</div>";
      views.detail.bindLinkActions($("detailGroupList"));
      views.detail.bindRecheckButtons($("detailGroupList"));
    },
    bindRecheckButtons: function (root) {
      root.querySelectorAll(".group-recheck-btn").forEach(function (btn) {
        if (btn.disabled) return;
        btn.onclick = function () { views.detail.recheck(btn.dataset.groupId, btn); };
      });
    },
    // Moves focus onto the provider tab matching `code` (falls back to
    // 全部) -- used after selectProvider's re-render below, since the old
    // focused tab element no longer exists once the tablist is rebuilt.
    focusProviderTab: function (code) {
      var box = $("libraryProviderTabs");
      if (!box) return;
      var tabs = Array.prototype.slice.call(box.querySelectorAll(".provider-tab"));
      var target = tabs.filter(function (t) { return t.dataset.provider === code; })[0]
        || tabs.filter(function (t) { return t.dataset.provider === ""; })[0];
      if (target) target.focus();
    },
    // I1 (§14.1): a tab click/Enter/Space (native <button> click) or an
    // arrow-key move (bindProviderTabs below) both land here. This is
    // backend-scoped, the same as open() -- updates state + URL ("provider="
    // stored in the URL query via the existing provider= machinery, reused
    // as a single-value slot while a detail view is open -- closeDetail()
    // resets it back to [] so it never leaks into the search filter of the
    // same name), then re-fetches `/api/library/media/<id>[?provider=]`
    // and re-renders wholesale from that response: the server has already
    // dropped every other provider's groups/links/labels/facets, so no
    // other tab/logo/count/link can survive client-side. The old content
    // stays on screen until the new response arrives (no flash of empty);
    // the "加载详情…" placeholder only appears if the fetch is still
    // pending after 150ms. Focus is restored onto the freshly-rendered tab
    // matching the new selection once render() rebuilds the tablist.
    selectProvider: function (code) {
      var normalized = code || "";
      state.library.provider = normalized ? [normalized] : [];
      views.library.pushUrl(false);
      var url = detailRequestUrl(state.library.media, normalized);
      var placeholderTimer = setTimeout(function () {
        $("detailContent").innerHTML = '<div class="empty">加载详情…</div>';
      }, 150);
      return views.library.fetchChannel("detail", url).then(function (d) {
        clearTimeout(placeholderTimer);
        if (!d) return;
        views.detail.render(d, !!normalized);
        views.detail.focusProviderTab(state.detail.provider);
      }).catch(function (e) {
        clearTimeout(placeholderTimer);
        $("detailContent").innerHTML = '<div class="empty error">' + esc(e.message || "加载失败") + "</div>";
      });
    },
    render: function (media, scopedFetch) {
      state.detail.media = media;
      var genres = (media.genres || []).map(function (g) { return '<span class="pill">' + esc(g) + '</span>'; }).join("");
      var matchLabel = MATCH_LABEL[media.match_status] || "";
      // Cards never show a badge for unmatched/candidate; detail keeps a
      // subtle grey match-status text for those, reserving the yellow
      // badge for needs_review only (T7 §3).
      var matchNote = media.match_status === "needs_review"
        ? '<span class="badge-match">' + esc(matchLabel) + '</span>'
        : (matchLabel ? '<span class="match-note">' + esc(matchLabel) + '</span>' : "");
      var hasBackdrop = !!media.backdrop_url;
      var posterUrl = media.poster_large_url || media.poster_url;
      var typeIcon = media.media_type === "tv" ? "icon-tv" : "icon-film";
      // T21 §8.2: the foreground layer shows the real backdrop when there
      // is one; when there isn't (backdrop_url null), it falls back to
      // the poster -- still object-fit:contain, never stretched/cropped
      // -- so the hero keeps the exact same reserved geometry instead of
      // collapsing (no detail-hero-compact any more). bind() below swaps
      // to this same fallback src at runtime if the backdrop URL itself
      // 404s/is blocked (data-fallback-src), and drops the image entirely
      // (dark fill + gradient only -- title/poster stay readable in
      // .detail-info/.detail-poster below, which never sit inside the
      // hero) if that also fails or there is no poster either.
      var heroSrc = media.backdrop_url || posterUrl || "";
      var heroFallbackSrc = hasBackdrop && posterUrl ? posterUrl : "";
      var heroAlt = esc(media.title) + "背景图";
      var heroLayer = heroSrc
        ? '<img class="detail-backdrop__blur" src="' + esc(heroSrc) + '" alt="" aria-hidden="true">' +
          '<img class="detail-backdrop__image" src="' + esc(heroSrc) + '" alt="' + heroAlt + '" loading="eager" fetchpriority="high"' +
          (heroFallbackSrc ? ' data-fallback-src="' + esc(heroFallbackSrc) + '"' : "") + '>'
        : "";
      // T21 §8.2 follow-up: no backdrop AND no poster -- paint a subtle
      // dark gradient instead of the flat fill (bind()'s error handler
      // adds the same class if the fallback src also fails to load).
      var heroEmptyClass = heroSrc ? "" : " detail-backdrop--empty";
      var poster = posterUrl
        ? '<img class="detail-poster-img" src="' + esc(posterUrl) + '" alt="' + esc(media.title) + '海报">'
        : '<span class="poster-fallback"><svg class="icon" aria-hidden="true"><use href="' + ICONS_URL + '#' + typeIcon + '"></use></svg><span>' + esc(media.title) + '</span></span>';
      var groupCount = media.group_count != null ? media.group_count : (media.groups || []).length;
      // I2: `media.link_count` isn't live-only either (it's summed from
      // each group's own non-live-only link_count) -- the summary row
      // must match what the groups below actually display, so it always
      // sums their live counts instead of trusting the backend total.
      var linkCount = (media.groups || []).reduce(function (sum, g) { return sum + groupLiveLinkCount(g); }, 0);
      // x1-provider-ui §2.2: the provider-logo tablist's data (real
      // providers only) and the initial selection -- a `provider=` already
      // in the URL (a shared deep link, or inherited from the search
      // filter at the moment the card was opened) wins if this title
      // actually has that provider; otherwise 全部, unless there is only
      // one provider at all (§3.3: auto-select it, no 全部 tab). When
      // this call's own fetch (open() or selectProvider() above) was
      // itself scoped to a provider (§14.1), `facets` only ever contains
      // that one entry -- `scopedFetch` tells renderProviderTabs to still
      // offer 全部 in that case (it can't infer that from `facets` alone).
      var facets = media.provider_facets || [];
      var codes = facets.map(function (f) { return f.provider; });
      var requested = (state.library.provider && state.library.provider[0]) || "";
      var selected = codes.indexOf(requested) !== -1 ? requested : (codes.length === 1 ? codes[0] : "");
      state.detail.provider = selected;
      state.library.provider = selected ? [selected] : [];
      var sourceCount = media.provider_count != null ? media.provider_count : facets.length;
      var providerSwitcher = facets.length
        ? '<div class="provider-switcher"><h3>网盘</h3><div id="libraryProviderTabs" class="provider-tablist" role="tablist" aria-label="网盘来源">' +
          views.detail.renderProviderTabs(facets, selected, !!scopedFetch) + '</div></div>'
        : "";
      $("detailContent").innerHTML =
        '<div class="detail-hero"><div class="detail-backdrop' + heroEmptyClass + '">' + heroLayer + '<div class="detail-backdrop__gradient" aria-hidden="true"></div></div></div>' +
        '<div class="detail-main">' +
        '<div class="detail-poster">' + poster + '</div>' +
        '<div class="detail-info">' +
        '<h2 class="detail-title">' + esc(media.title) + (media.year ? ' <span class="detail-year">(' + esc(media.year) + ')</span>' : "") + '</h2>' +
        (media.original_title && media.original_title !== media.title ? '<p class="detail-original">' + esc(media.original_title) + '</p>' : "") +
        '<div class="detail-meta-row"><span class="pill">' + esc(MEDIA_TYPE_LABEL[media.media_type] || media.media_type || "") + '</span>' + genres + matchNote + '</div>' +
        // §14.4: after title/original title, before the overview.
        views.detail.ratingsRowHtml(media) +
        '<p class="detail-overview" id="detailOverview">' + esc(media.overview || "暂无简介") + '</p>' +
        '<button type="button" id="detailOverviewToggle" class="tertiary">展开简介</button>' +
        '<div class="detail-summary-row"><span>' + esc(groupCount) + ' 个版本</span><span>' + esc(linkCount) + ' 条链接</span>' + (sourceCount ? "<span>" + esc(sourceCount) + " 个来源</span>" : "") + '</div>' +
        '<button type="button" class="detail-cta">查看资源</button>' +
        '</div>' +
        '</div>' +
        providerSwitcher +
        '<section id="libraryProviderPanel" class="provider-panel" role="tabpanel" aria-label="资源列表" tabindex="0">' +
        '<div class="detail-groups"><h3>资源</h3><div id="detailGroupList" tabindex="-1"></div></div>' +
        '</section>';
      views.library.pushUrl(false);
      views.detail.bind();
      views.detail.renderGroupList();
    },
    bindLinkActions: function (root) {
      root.querySelectorAll("[data-link-action]").forEach(function (btn) {
        if (btn.disabled) return;
        btn.onclick = function () {
          var linkId = btn.dataset.linkId;
          var action = btn.dataset.linkAction;
          if (action === "transfer") {
            var titleEl = $("detailContent").querySelector(".detail-title");
            views.transfer.openLibrary(linkId, titleEl ? titleEl.textContent : "");
            return;
          }
          views.detail.reveal(linkId, action, btn);
        };
      });
    },
    // x1-provider-ui §5.1: roving-tabindex ARIA tablist for the provider
    // switcher -- click/Enter/Space activate via the native <button> click
    // event; ArrowLeft/Right/Home/End move + activate here, mirroring
    // router's workspace-nav tablist keydown handler. Bound once per
    // render() -- I1 (§14.1): every activation is a backend-scoped
    // refetch, and selectProvider's own render() rebuilds the tablist
    // wholesale from that response (never just patching aria-selected/
    // tabindex in place), so this binding itself is thrown away and
    // reattached fresh each time too.
    bindProviderTabs: function () {
      var box = $("libraryProviderTabs");
      if (!box) return;
      box.querySelectorAll(".provider-tab").forEach(function (btn) {
        btn.onclick = function () { views.detail.selectProvider(btn.dataset.provider); };
      });
      box.onkeydown = function (e) {
        var tabs = Array.prototype.slice.call(box.querySelectorAll(".provider-tab"));
        var idx = tabs.indexOf(document.activeElement);
        if (idx === -1) return;
        var nextIdx = null;
        if (e.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
        else if (e.key === "ArrowLeft") nextIdx = (idx - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") nextIdx = 0;
        else if (e.key === "End") nextIdx = tabs.length - 1;
        if (nextIdx === null) return;
        e.preventDefault();
        views.detail.selectProvider(tabs[nextIdx].dataset.provider);
      };
    },
    bind: function () {
      var content = $("detailContent");
      // T21 §8.2/§8.3: no inline onload=/onerror= attributes (T8 #9,
      // CSP-friendly) -- bind the hero foreground image's load (sets the
      // informational data-backdrop-ratio) and error (falls back to the
      // poster once, then removes both image layers entirely, leaving
      // the same-size dark fill + gradient) handlers here instead.
      var heroImg = content.querySelector(".detail-backdrop__image");
      if (heroImg) {
        var heroBlur = content.querySelector(".detail-backdrop__blur");
        var hero = content.querySelector(".detail-hero");
        var heroBackdrop = content.querySelector(".detail-backdrop");
        heroImg.addEventListener("load", function () {
          if (heroImg.naturalWidth && heroImg.naturalHeight) {
            hero.setAttribute("data-backdrop-ratio", String(heroImg.naturalWidth / heroImg.naturalHeight));
          }
        });
        heroImg.addEventListener("error", function () {
          var fallbackSrc = heroImg.getAttribute("data-fallback-src");
          if (fallbackSrc && !heroImg.dataset.triedFallback) {
            heroImg.dataset.triedFallback = "1";
            heroImg.src = fallbackSrc;
            if (heroBlur) heroBlur.src = fallbackSrc;
          } else {
            heroImg.remove();
            if (heroBlur) heroBlur.remove();
            if (heroBackdrop) heroBackdrop.classList.add("detail-backdrop--empty");
          }
        });
      }
      views.detail.bindProviderTabs();
      views.detail.bindRatingUnits(content);
      var toggle = $("detailOverviewToggle");
      if (toggle) {
        toggle.onclick = function () {
          var overview = $("detailOverview");
          var expanded = overview.classList.toggle("expanded");
          toggle.textContent = expanded ? "收起简介" : "展开简介";
        };
      }
      var cta = content.querySelector(".detail-cta");
      if (cta) {
        cta.onclick = function () {
          var list = $("detailGroupList");
          list.scrollIntoView({ behavior: "smooth", block: "start" });
          list.focus();
        };
      }
    },
    // T18 §14.1: the initial fetch is already provider-scoped whenever
    // state.library.provider carries a value -- entering detail from a
    // filtered search/rail card, a direct `?provider=115&media=<id>` URL,
    // or a browser refresh/back/forward onto one, all route through here.
    open: function (mediaId) {
      views.detail.hideReveal();
      $("detailContent").innerHTML = '<div class="empty">加载详情…</div>';
      var provider = (state.library.provider && state.library.provider[0]) || "";
      var url = detailRequestUrl(mediaId, provider);
      return views.library.fetchChannel("detail", url)
        .then(function (d) { if (!d) return; views.detail.render(d, !!provider); })
        .catch(function (e) { $("detailContent").innerHTML = '<div class="empty error">' + esc(e.message || "加载失败") + "</div>"; });
    },
    init: function () {
      $("detailBack").onclick = function () { views.library.closeDetail(); };
      $("revealCodeToggle").onclick = views.detail.toggleCode;
    }
  };

  views.settings = {
    init: function () {
      // T10: only send tmdb_enrich_enabled once the user has actually
      // touched the switch in this page session -- otherwise a save fired
      // before refreshLibraryStatus seeds the checkbox from server state
      // would send its default "unchecked" and silently disable enrichment.
      $("tmdbEnrichEnabled").addEventListener("change", function () { state.tmdbEnrichDirty = true; });
      // w6-contract §5: same "don't clobber an unseen server value" rule as
      // tmdbEnrichDirty above -- one flag covers the whole card (global
      // switch + all four provider rows) since a save always resends the
      // full linkcheck_providers map together, never a per-provider diff.
      $("linkcheckProviders").innerHTML = LINKCHECK_PROVIDERS.map(linkcheckProviderRowHtml).join("");
      $("linkcheckEnabled").addEventListener("change", function () { state.linkcheckDirty = true; });
      LINKCHECK_PROVIDERS.forEach(function (code) {
        $("linkcheck-" + code + "-enabled").addEventListener("change", function () { state.linkcheckDirty = true; });
        $("linkcheck-" + code + "-cap").addEventListener("change", function () { state.linkcheckDirty = true; });
      });
      // The card's own save button: the page-level 保存设置 button sits in
      // another card, and ticking a switch here is not persisted until
      // some save is clicked -- a user who only ticked and reloaded lost
      // everything. Sends ONLY the two linkcheck keys.
      $("linkcheckSave").onclick = function () {
        $("linkcheckSave").disabled = true;
        feedback("linkcheckResult", "正在保存检测设置…");
        api.request("/api/settings", { method: "POST", body: JSON.stringify(linkcheckSavePayload()) }).then(function () {
          state.linkcheckDirty = false;
          return refreshLinkcheckStatus().then(function () {
            feedback("linkcheckResult", "检测设置已保存。" + $("linkcheckHeartbeat").textContent.split(" · ")[0], "success");
          });
        }).catch(function (e) {
          feedback("linkcheckResult", "保存失败：" + e.message, "error");
        }).then(function () { $("linkcheckSave").disabled = false; });
      };
      $("settingsSave").onclick = function () {
        var payload = { "115_target_pid": $("settingPid").value.trim() };
        var cookie = $("cookie115").value.trim();
        if (cookie) payload["115_cookie"] = cookie;
        var tmdbKey = $("tmdbKey").value.trim();
        if (tmdbKey) payload["tmdb_api_key"] = tmdbKey;
        var budget = $("tmdbBudget").value.trim();
        if (budget) payload["tmdb_daily_budget"] = parseInt(budget, 10);
        if (state.tmdbEnrichDirty) payload["tmdb_enrich_enabled"] = $("tmdbEnrichEnabled").checked;
        if (state.linkcheckDirty) Object.assign(payload, linkcheckSavePayload());
        api.request("/api/settings", { method: "POST", body: JSON.stringify(payload) }).then(function (d) {
          $("cookie115").value = "";
          $("tmdbKey").value = "";
          // Backfill #tmdbBudget from the server's configured_budget (the
          // value it actually stored) rather than trusting whatever the
          // input already showed.
          if (d.tmdb && d.tmdb.configured_budget != null) $("tmdbBudget").value = d.tmdb.configured_budget;
          state.tmdbEnrichDirty = false;
          state.linkcheckDirty = false;
          if (d.cookie_check) {
            feedback("settingsResult", d.cookie_check.valid ? "设置已保存，115 Cookie 可用。" : "设置已保存，但 115 Cookie 不可用。", d.cookie_check.valid ? "success" : "error");
          } else {
            feedback("settingsResult", "设置已保存。", "success");
          }
          return refreshStatus().then(refreshLibraryStatus).then(refreshLinkcheckStatus);
        }).catch(function (e) { feedback("settingsResult", e.message, "error"); });
      };
      $("tmdbCheck").onclick = function () {
        $("tmdbCheck").disabled = true;
        $("tmdbEnrichNow").disabled = true;
        feedback("tmdbScrapeResult", "正在测试 TMDB 连接…");
        api.request("/api/library/tmdb-check", { method: "POST" }).then(function (d) {
          feedback("tmdbScrapeResult", d.ok
            ? "连接成功，延迟 " + d.latency_ms + "ms，返回 " + d.genres_count + " 个类型。"
            : d.hint, d.ok ? "success" : "error");
        }).catch(function (e) {
          feedback("tmdbScrapeResult", e.message, "error");
        }).then(function () {
          $("tmdbCheck").disabled = false;
          $("tmdbEnrichNow").disabled = false;
          return refreshLibraryStatus();
        });
      };
      $("tmdbEnrichNow").onclick = function () {
        $("tmdbCheck").disabled = true;
        $("tmdbEnrichNow").disabled = true;
        feedback("tmdbScrapeResult", "正在补全一轮…");
        api.request("/api/library/tmdb-enrich-now", { method: "POST" }).then(function (d) {
          var s = d.stats;
          feedback("tmdbScrapeResult", "处理 " + s.candidates_considered + " 个标题，请求 " + s.requests_made + " 次，精确匹配 " + s.matched_exact + "，待复核 " + s.needs_review + "。", "success");
        }).catch(function (e) {
          feedback("tmdbScrapeResult", e.message, "error");
        }).then(function () {
          $("tmdbCheck").disabled = false;
          $("tmdbEnrichNow").disabled = false;
          return refreshLibraryStatus();
        });
      };
    }
  };

  // ------------------------------------------------------------------
  // status: renders /api/status (+ /api/library/tmdb-status) into the
  // settings "服务状态" card (8 status-dot rows) and the header dot.
  // ------------------------------------------------------------------
  state.statusRowsBase = [];
  state.statusRowsLibrary = [];

  function renderStatusList() {
    var rows = state.statusRowsBase.concat(state.statusRowsLibrary);
    $("statusList").innerHTML = rows.map(function (r) {
      return '<div class="status-row"><span class="status-mark ' + r[0] + '" aria-label="' + esc(r[2]) + '"></span><span class="status-label">' + esc(r[1]) + '</span><span class="status-value">' + esc(r[2]) + "</span></div>";
    }).join("");
  }

  // T19: the 115 cookie state machine's user-facing text/colour, keyed by
  // the sanitised `cookie_state` value the backend returns -- never the
  // raw error_code, never any cookie/uid/response content.
  var COOKIE_115_STATE_TEXT = {
    unconfigured: { text: "未配置", cls: "" },
    valid: { text: "115 转存可用", cls: "success" },
    reauth_required: { text: "需要重新授权", cls: "error" },
    network_error: { text: "网络暂时不可用", cls: "warn" },
    rate_limited: { text: "受到限流，请稍后重试", cls: "warn" },
    provider_error: { text: "115 返回异常，请稍后重试", cls: "warn" },
    unknown: { text: "暂时无法确认状态，请稍后重试", cls: "warn" }
  };

  function renderCookie115Status(n) {
    var info = COOKIE_115_STATE_TEXT[n.cookie_state] || COOKIE_115_STATE_TEXT.unknown;
    var text = info.text;
    if (n.cookie_state === "valid" && n.cookie_checked_at) {
      text += " · 最后检查 " + new Date(n.cookie_checked_at).toLocaleString();
    }
    // Item 8: show how long until 115 stops rate-limiting the session.
    if (n.cookie_state === "rate_limited" && n.retry_after) {
      text += "（约 " + n.retry_after + " 秒后重试）";
    }
    var box = $("cookie115Status");
    box.textContent = text;
    box.className = "feedback" + (info.cls ? " " + info.cls : "");
    box.hidden = false;
    // The QR flow resolves both "used to work, now broken" and "never
    // configured" -- both get the same button (brief §5.3). It is also the
    // only way out of every other non-valid state (unknown/provider_error/
    // network_error/rate_limited: e.g. an expired session that 115 answers
    // with a login page instead of JSON surfaces as "unknown"), so the
    // button is offered whenever the session is not known to be valid;
    // reauth_available below still disables it while 115 rate-limits.
    var showBtn = n.cookie_state !== "valid";
    var btn = $("reauth115Btn");
    btn.hidden = !showBtn;
    if (showBtn) {
      // Item 8: honour reauth_available -- a fixed, disabled state instead
      // of letting the user start a challenge the backend will just 429.
      var available = n.reauth_available !== false;
      btn.disabled = !available;
      btn.textContent = available ? "重新授权 115" : "请稍后再试";
    }
  }

  function renderStatus(s) {
    var n = s["115"] || {};
    var o = s.openlist || {};
    var t = s.strm || {};
    var cookieLabel = !n.cookie_configured ? "未配置" : n.cookie_valid === true ? "可用" : n.cookie_valid === false ? "不可用" : "待检查";
    var lastSuccess = s.hdhive.checkin.last_success;
    var lastAt = s.hdhive.checkin.last_at;
    var lastTaskLabel = lastSuccess === null || lastSuccess === undefined
      ? "暂无记录"
      : "最近自动任务：" + (lastSuccess ? "成功" : "失败") + " · " + (lastAt || "");
    state.statusRowsBase = [
      [s.auth_mode === "access" ? "ok" : "warn", "Cloudflare Access", s.auth_mode === "access" ? "已启用" : "本地测试模式"],
      [n.cookie_valid === true ? "ok" : n.cookie_configured ? "bad" : "off", "115 Cookie（转存）", cookieLabel],
      [n.open_platform_configured ? "ok" : "off", "115 开放平台令牌", n.open_platform_configured ? "已同步" : "未同步"],
      [s.hdhive.authorized ? "ok" : "off", "后台授权", (s.hdhive.authorized ? "已授权" : "未授权") + " · " + lastTaskLabel],
      [o.token_configured ? "ok" : "off", "OpenList API Token", o.token_configured ? "已配置" : "未配置"],
      [t.exists ? "ok" : "bad", "Infuse STRM 目录", t.exists ? "可读取" : "不可读取"]
    ];
    renderStatusList();
    renderCookie115Status(n);
    $("globalDot").className = "status-mark " + (t.exists ? "ok" : "off");
    $("globalStatusText").textContent = t.exists ? "服务可用" : "请检查服务状态";
  }

  function refreshStatus() {
    return api.request("/api/status?verify_115=1").then(renderStatus);
  }


  // T10: renders the settings page's "TMDB 刮削" self-diagnosis block from
  // a GET /api/library/tmdb-status payload -- 状态/今日预算/进度/待复核拆分/
  // 429/最近一轮/最近错误, one line each.
  function renderTmdbScrapeStatus(d) {
    var stateLine = d.worker_state === "running" ? "运行中" : "已暂停：" + (TMDB_PAUSED_REASON_LABEL[d.paused_reason] || d.paused_reason || "未知原因");
    var budgetLine = d.cap_source === "env"
      ? d.used + "/" + d.configured_budget + "（受环境上限 " + d.effective_budget + " 限制）"
      : d.used + "/" + d.effective_budget;
    var progressLine = (d.matched_total || 0) + "/" + (d.total_media || 0) + " 已匹配 · " + (d.needs_review_count || 0) + " 待复核";
    // T14/§3.1, fix wave 1: 待复核拆成三档 -- 从未查询过 TMDB、已有候选待
    // 确认、查询后确认无候选 -- 避免把"查询后确认无候选"误算进"已有候选"。
    var reviewLine = "待 TMDB 首次匹配 " + (d.review_pending_unqueried || 0) + " · 已有候选待确认 " + (d.review_scored || 0)
      + " · 已查询无候选 " + (d.review_no_candidate || 0) + " · 待复核合计 " + (d.needs_review_total || 0);
    var rate429Line = String(d.tmdb_requests_429 || 0);
    var lastRoundLine = d.last_round_at ? new Date(d.last_round_at).toLocaleString() + " · " + (d.last_round_processed || 0) + " 条" : "尚未运行";
    var lastErrorLine = d.last_error ? tmdbErrorHint(d.last_error) : "无";
    $("tmdbScrapeStatus").innerHTML = [
      "状态：" + esc(stateLine),
      "今日：" + esc(budgetLine),
      "进度：" + esc(progressLine),
      "待复核：" + esc(reviewLine),
      "今日 429：" + esc(rate429Line),
      "最近一轮：" + esc(lastRoundLine),
      "最近错误：" + esc(lastErrorLine)
    ].map(function (line) { return "<p>" + line + "</p>"; }).join("");
  }

  function refreshLibraryStatus() {
    return api.request("/api/library/tmdb-status").then(function (d) {
      var indexLabel = d.installed ? "已安装 · " + (d.media_total || 0) + " 个标题" : "未安装";
      // worker_state/effective_budget/used are the top-level status fields;
      // the legacy budget.{budget,used}/enricher.running keys are kept as a
      // fallback so the row still renders against an older backend.
      var legacyBudget = d.budget || {};
      var workerState = d.worker_state || (d.enricher && d.enricher.running ? "running" : (d.installed && d.key_configured ? "paused" : "not_started"));
      var workerLabel = WORKER_STATE_LABEL[workerState] || WORKER_STATE_LABEL.not_started;
      var used = d.used != null ? d.used : (legacyBudget.used || 0);
      var effectiveBudget = d.effective_budget != null ? d.effective_budget : (legacyBudget.budget || 0);
      var enrichLabel = !d.installed ? "索引未安装" : !d.key_configured ? "未配置 Key" : workerLabel + " · 今日 " + used + "/" + effectiveBudget;
      state.statusRowsLibrary = [
        [d.installed ? "ok" : "off", "资源库索引", indexLabel],
        [!d.installed || !d.key_configured ? "off" : (workerState === "running" ? "ok" : "warn"), "TMDB 补全", enrichLabel]
      ];
      renderStatusList();
      renderTmdbScrapeStatus(d);
      // Seed #tmdbBudget from configured_budget -- never from the
      // effective (possibly env-capped) budget.
      if (d.configured_budget != null && !$("tmdbBudget").value) $("tmdbBudget").value = d.configured_budget;
      // I4: seed the checkbox from server state so a save that doesn't
      // touch it (e.g. only changing the 115 target pid) round-trips the
      // existing setting instead of silently sending "unchecked".
      $("tmdbEnrichEnabled").checked = !!d.enrich_enabled;
    }).catch(function (e) {
      var code = e && e.code;
      var indexLabel = code === "LIBRARY_NOT_ENCRYPTED" ? "未加密"
        : code === "LIBRARY_INDEX_UNREADABLE" ? "索引不可读"
        : code === "LIBRARY_SCHEMA_MISMATCH" ? "版本不兼容"
        : "未安装";
      state.statusRowsLibrary = [["off", "资源库索引", indexLabel], ["off", "TMDB 补全", "索引未安装"]];
      setEmpty("tmdbScrapeStatus", "索引未安装，暂无法诊断 TMDB 刮削状态。");
      renderStatusList();
    });
  }

  // w6-contract §4/§UI item 4: renders GET /api/library/linkcheck-status
  // into the "资源有效性检测" settings card -- `d` is null when the fetch
  // itself failed (e.g. the checker isn't installed yet), in which case
  // every row just keeps its skeleton (no counts/heartbeat text). Checkbox/
  // cap values are only ever seeded from the server when the user hasn't
  // touched them yet in this page session (state.linkcheckDirty), exactly
  // like tmdbEnrichEnabled above -- otherwise a save that doesn't touch
  // this card would round-trip a value the user is mid-way through editing.
  function renderLinkcheckStatus(d) {
    if (!state.linkcheckDirty) $("linkcheckEnabled").checked = !!(d && d.enabled);
    var heartbeatBox = $("linkcheckHeartbeat");
    heartbeatBox.textContent = linkcheckEnabledSummary(d) + " · " + (d && d.heartbeat_at
      ? "检测线程：最近心跳 " + new Date(d.heartbeat_at).toLocaleString()
      : "检测线程：未运行");
    heartbeatBox.hidden = false;
    var providers = (d && d.providers) || {};
    LINKCHECK_PROVIDERS.forEach(function (code) {
      var p = providers[code] || {};
      if (!state.linkcheckDirty) {
        $("linkcheck-" + code + "-enabled").checked = !!p.enabled;
        $("linkcheck-" + code + "-cap").value = p.daily_cap != null ? p.daily_cap : "";
      }
      var lines = [
        "今日 " + (p.used_today || 0) + "/" + (p.daily_cap != null ? p.daily_cap : 0),
        "有效 " + (p.valid || 0) + " · 失效 " + (p.invalid || 0) + " · 未知 " + (p.unknown || 0) + " · 未检测 " + (p.unchecked || 0)
      ];
      if (p.paused_until) lines.push("暂停至 " + new Date(p.paused_until).toLocaleString());
      if (p.last_error_class) lines.push("最近错误：" + linkcheckErrorClassLabel(p.last_error_class));
      $("linkcheck-" + code + "-status").innerHTML = lines.map(function (line) { return "<p>" + esc(line) + "</p>"; }).join("");
    });
  }

  function refreshLinkcheckStatus() {
    return api.request("/api/library/linkcheck-status").then(function (d) {
      renderLinkcheckStatus(d);
    }).catch(function () {
      renderLinkcheckStatus(null);
    });
  }

  // ------------------------------------------------------------------
  // bootstrap
  // ------------------------------------------------------------------
  function init() {
    feedback("globalError", "");
    // W5: proactive refresh every 20 minutes, plus a catch-up refresh on
    // tab refocus if it's been that long -- keeps api.csrf from ever
    // reaching the backend's 3600s CSRF_TTL_SECONDS on a long-open tab.
    // Failures are silent: the reactive retry in api.request still covers
    // the next real POST if this hasn't run recently enough.
    setInterval(function () {
      api.refreshCsrf().catch(function () {});
    }, CSRF_REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && Date.now() - api.csrfFetchedAt >= CSRF_REFRESH_INTERVAL_MS) {
        api.refreshCsrf().catch(function () {});
      }
    });
    return api.refreshCsrf().then(function () {
      return api.request("/api/status?verify_115=1");
    }).then(function (s) {
      renderStatus(s);
      state.openPaths = Object.assign(state.openPaths, s.openlist.paths || {});
      state.transferRoot = state.openPaths["115pan"] || "/115pan";
      // T18 §12.5: the only path<->pid mapping ever exposed to the browser
      // -- the OpenList "115pan" mount root's own 115 cid (see the
      // transfer dialog's own folder-loading logic for how this is used).
      state.transferRootPid = (s["115"] && s["115"].open_root_cid) || "";
      state.currentTransferPath = state.transferRoot;
      state.currentTransferPid = state.transferRootPid;
      state.currentOpenPath = state.openPaths[state.currentOpenPreset] || "/";
      $("openCurrent").textContent = state.currentOpenPath;
      $("folderCurrent").textContent = state.transferRoot;
      $("targetPath").textContent = state.transferRoot;
      $("settingPid").value = s["115"].target_pid || "";
      $("actor").textContent = s.auth_mode === "access" ? "Cloudflare Access" : "本地测试模式";
      refreshLibraryStatus();
      refreshLinkcheckStatus();
    }).catch(function (e) { feedback("globalError", e.message, "error"); });
  }

  views.browser.openlist.init();
  views.browser.strm.init();
  views.transfer.init();
  views.reauth.init();
  views.settings.init();
  views.library.init();
  views.library.loadFilters();
  views.detail.init();
  router.init();
  init();
})();
