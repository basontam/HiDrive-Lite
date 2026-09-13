/* HiDrive-Lite shell script.
 * Modules: api (fetch wrapper) / state (shared mutable values) / router
 * (?tab= handling) / views.browser (OpenList + STRM) / views.settings /
 * views.transfer (115 transfer dialog, reusable by later library views).
 */
(function () {
  "use strict";

  function initAppearance() {
    var control = document.getElementById("themePreference");
    if (!control) return;
    var key = "hidrive.theme";
    var system = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    function normalize(value) { return value === "light" || value === "dark" ? value : "system"; }
    function readPreference() {
      try { return normalize(localStorage.getItem(key)); } catch (_) { return "system"; }
    }
    var preference = readPreference();
    function apply() {
      var dark = preference === "dark" || (preference === "system" && system && system.matches);
      document.documentElement.dataset.theme = dark ? "dark" : "light";
      control.value = preference;
      document.querySelectorAll(".brand-logo-picture source").forEach(function (source) {
        source.media = dark ? "all" : "not all";
      });
    }
    control.addEventListener("change", function () {
      preference = normalize(control.value);
      try { localStorage.setItem(key, preference); } catch (_) {}
      apply();
    });
    if (system) {
      if (system.addEventListener) system.addEventListener("change", apply);
      else if (system.addListener) system.addListener(apply);
    }
    window.addEventListener("storage", function (event) {
      if (event.key === key || event.key === null) {
        preference = readPreference();
        apply();
      }
    });
    apply();
  }
  // Login and signed-in pages share this, independently of API initialization.
  initAppearance();

  // Phase 3: the server renders only the workspaces this user may use, so
  // the shell follows the page rather than a fixed list. A member's page has
  // no OpenList or STRM panel in it at all -- nothing here needs to hide
  // them, and nothing here can be made to show them.
  var ALL_TAB_IDS = ["library", "openlist", "strm", "cloud", "settings"];
  var TAB_IDS = ALL_TAB_IDS.filter(function (id) { return !!document.getElementById(id); });
  var CLOUD_POLL_INTERVAL_MS = 15000;

  var $ = function (id) { return document.getElementById(id); };
  var ICONS_URL = "/static/icons.svg?v=" + (document.body.dataset.assetVersion || "");

  // Sticky header: hides while the reader scrolls down, comes back the moment
  // they scroll up. It owns nothing but `.site-sticky`'s own class -- the
  // filter popover keeps its own scroll listener for its own job (closing
  // itself), and the two never read or write each other's state.
  //
  // Thresholds, tuned on the real page: 8px of top guard where the header is
  // always shown, a 6px direction dead zone so a trackpad jitter cannot make
  // it flicker, and 64px of travel before the first hide so a short nudge on a
  // freshly loaded page does not take the navigation away.
  var stickyHeader = {
    TOP_REVEAL: 8,
    DEAD_ZONE: 6,
    HIDE_AFTER: 64,
    HIDDEN_CLASS: "is-scroll-hidden",
    el: null,
    lastY: 0,
    frame: 0,
    currentY: function () {
      return window.scrollY || window.pageYOffset || 0;
    },
    measure: function () {
      if (!this.el) return;
      // Clear the whole block, including the margin that separates it from the
      // content -- a fixed pixel guess would leave a sliver on one breakpoint.
      var margin = parseFloat(window.getComputedStyle(this.el).marginBottom) || 0;
      this.el.style.setProperty("--site-sticky-hide-offset", "-" + Math.ceil(this.el.offsetHeight + margin) + "px");
    },
    reveal: function () {
      if (!this.el || !this.el.classList.contains(this.HIDDEN_CLASS)) return;
      this.el.classList.remove(this.HIDDEN_CLASS);
    },
    hide: function () {
      if (!this.el || this.el.classList.contains(this.HIDDEN_CLASS)) return;
      if (document.body.classList.contains("modal-open")) return;
      this.el.classList.add(this.HIDDEN_CLASS);
    },
    onFrame: function () {
      this.frame = 0;
      var y = this.currentY();
      var delta = y - this.lastY;
      if (y <= this.TOP_REVEAL) {
        this.reveal();
      } else if (Math.abs(delta) >= this.DEAD_ZONE) {
        if (delta > 0 && y > this.HIDE_AFTER) this.hide();
        else if (delta < 0) this.reveal();
      }
      this.lastY = y;
    },
    onScroll: function () {
      if (this.frame) return;
      this.frame = window.requestAnimationFrame(this.onFrame.bind(this));
    },
    init: function () {
      this.el = document.querySelector(".site-sticky");
      if (!this.el) return;
      this.lastY = this.currentY();
      this.measure();
      window.addEventListener("scroll", this.onScroll.bind(this), { passive: true });
      window.addEventListener("resize", this.measure.bind(this), { passive: true });
      window.addEventListener("orientationchange", this.measure.bind(this), { passive: true });
      // A keyboard user must never land on a button they cannot see.
      this.el.addEventListener("focusin", this.reveal.bind(this));
    }
  };

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
    return message && /[一-鿿]/.test(message) ? message : "目录读取失败，请稍后重试";
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
  var MEDIA_TYPE_LABEL = { movie: "电影", tv: "剧集", unknown: "系列合集" };
  // y1-cards §1.1: the only metadata line a media-card is allowed to show
  // -- year and type joined by " · ", either half dropped when empty, and
  // no placeholder rendered when both are empty.
  function formatCardMeta(year, mediaType) {
    // §1.1: an unknown/unclassified type shows the year only (the detail
    // page uses the 系列合集 display label); no year and no known type → "".
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
  // Round 29: a link is usable when the source still has it AND the checker
  // has not looked at it, or looked and confirmed it valid. "Looked at"
  // excludes the `queued` stub row, which only marks a never-checked link as
  // due -- an unchecked link keeps showing, an unconfirmed one does not.
  function linkIsUsable(link) {
    if (!link || link.deleted || link.invalid) return false;
    var status = link.check_status;
    return !status || status === "queued" || status === "valid";
  }
  // Round 25: the link rows a resource group shows -- restricted to the
  // card's pan and, unless include_deleted asked for the audit view, without
  // the rows linkIsUsable rejects. Nothing is fetched here: `group.links` is
  // already in the media response.
  function visibleLinks(group, code) {
    var det = (typeof state !== "undefined" && state && state.detail) || {};
    return ((group && group.links) || []).filter(function (l) {
      if (code && l.provider !== code) return false;
      return !!det.includeDeleted || linkIsUsable(l);
    });
  }
  // Round 25: how many links of this pan the detail actually shows as
  // usable -- live and not checker-invalid. The summary row, the pan tabs
  // and each card header all count these same rows, so no header can
  // claim more links than the card below it lists.
  function panLinkCount(media, code) {
    return ((media && media.groups) || []).reduce(function (sum, g) {
      return sum + ((g.links || []).filter(function (l) {
        return (!code || l.provider === code) && linkIsUsable(l);
      }).length);
    }, 0);
  }
  // Round 40: the RE0 candidates a pan is offering that are not already one
  // of its local links. An unlocked candidate keeps its RE0 row AND gains a
  // link row, so a count that added both would promise two resources where
  // the page is showing one of them twice.
  function re0UnstoredCandidates(media, code) {
    return (((media && media.re0_candidates) || [])).filter(function (c) {
      return (!code || c.provider === code) && !c.has_local_link && !c.resource_link_id;
    });
  }
  // Round 40: what the tab beside a pan's logo counts -- everything this pan
  // can give you right now, local links and RE0 candidates alike, each
  // resource once. Invalid candidates never reach here: the server drops the
  // ones the checker confirmed dead (and that own no local link) before the
  // detail response is built, so a candidate going invalid lowers this count
  // as soon as the page re-reads the detail.
  function panResourceCount(media, code) {
    return panLinkCount(media, code) + re0UnstoredCandidates(media, code).length;
  }
  // Round 25: exactly the pans the resource area shows a card for, in the
  // server's facet order with any pan found only in the rows or only in RE0
  // appended -- so a tab never promises a pan whose card would be empty, and
  // a card never appears without its tab. A pan whose every link is invalid
  // drops out (it keeps its card, count 0, only in the 包含已失效 audit view).
  function visibleFacets(media) {
    media = media || {};
    var labels = {};
    var codes = [];
    (media.provider_facets || []).forEach(function (f) { labels[f.provider] = f.label; codes.push(f.provider); });
    (media.groups || []).forEach(function (g) {
      (g.links || []).forEach(function (l) { if (codes.indexOf(l.provider) === -1) codes.push(l.provider); });
    });
    (media.re0_candidates || []).forEach(function (c) { if (codes.indexOf(c.provider) === -1) codes.push(c.provider); });
    return codes.map(function (code) {
      return {
        provider: code, label: labels[code] || providerLabel(code), link_count: panLinkCount(media, code),
        // `re0_count` is every candidate row this pan's card would list (an
        // already-unlocked one included, since it still renders); the tab's
        // own number is `resource_count`, which counts each resource once.
        re0_count: (media.re0_candidates || []).filter(function (c) { return c.provider === code; }).length,
        resource_count: panResourceCount(media, code),
      };
    }).filter(function (f) {
      return f.link_count > 0 || f.re0_count > 0 ||
        (media.groups || []).some(function (g) { return visibleLinks(g, f.provider).length > 0; });
    });
  }
  // Round 25: the 展开/收起 control is a caret (倒三角) that points down when
  // open and turns when folded -- the words live in aria-label/title only,
  // so the row keeps its space for the resource itself.
  function foldButtonHtml(key, open) {
    var label = open ? "收起" : "展开";
    return '<button type="button" class="group-fold-btn" data-fold="' + esc(key) + '" aria-expanded="' + (open ? "true" : "false") +
      '" aria-label="' + esc(label) + '" title="' + esc(label) + '">' +
      '<svg class="fold-caret" aria-hidden="true"><use href="' + ICONS_URL + '#icon-caret"></use></svg></button>';
  }
  // Round 25: 展开/收起 state of one block ("g:<group_id>" / "re0:<pan>"):
  // the user's own toggle wins, then the default the card decided, then open.
  function groupFoldOpen(key) {
    var det = (typeof state !== "undefined" && state && state.detail) || {};
    var folds = det.folds || {};
    if (Object.prototype.hasOwnProperty.call(folds, key)) return !!folds[key];
    var defaults = det.foldDefaults || {};
    return Object.prototype.hasOwnProperty.call(defaults, key) ? !!defaults[key] : true;
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
  // Round 33: each accent is the dominant colour of that pan's mark in
  // icons.svg (redrawn from the services' own app marks), so the tab
  // underline and the logo agree.
  var PROVIDER_ACCENT = {
    "115": "#2b5795", tianyicloud: "#25b9ec", quark: "#316cf2", alipan: "#7771ee", baidu: "#39c9ec",
    guangya: "#ff7900", "139cloud": "#21a9ed", "123": "#4d7ff1", ed2k: "#64748b", unknown: "#64748b"
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
  // RE0 federated search lane (spec §10.4): status words for the remote
  // lane, never the upstream body.
  function remoteStatusLabel(d) {
    var labels = {
      no_candidates: "RE0 未找到对应媒体", reauth_required: "需要重新授权 RE0", refresh_unavailable: "需要重新授权 RE0",
      scope_denied: "RE0 权限不足", user_level_denied: "RE0 账号等级不足", quota_exhausted: "今日 RE0 查询额度已用完",
      missing_credentials: "RE0 尚未配置", tmdb_unavailable: "TMDB 不可用", tmdb_budget_exhausted: "TMDB 额度已用完",
      upstream_5xx: "RE0 暂时不可用", network_error: "RE0 暂时不可用", invalid_json: "RE0 暂时不可用", upstream_4xx: "RE0 拒绝了请求"
    };
    if (!d) return "RE0 查询失败";
    var count = (d.items || []).length;
    var found = count ? "RE0 找到 " + count + " 个媒体" : "RE0 没有对应资源";
    var prefix = d.origin === "local" ? "本地已匹配 TMDB ID · " : "";
    // partial: candidates were served (library's own TMDB ids and/or the
    // part TMDB did answer) but the remote candidate source stopped early.
    if (d.status === "partial") return prefix + found + "（部分结果，" + (labels[d.reason] || "远端搜索失败") + "）";
    if (d.status === "fresh" || d.status === "cached") {
      if (!count) return prefix + "RE0 没有对应资源";
      return prefix + found + (d.status === "cached" ? "（缓存）" : "");
    }
    if (d.status === "rate_limited") return "RE0 限流，" + (d.retry_after || 60) + " 秒后再试";
    return labels[d.status] || "RE0 查询失败";
  }
  var RE0_ACTION_LABEL = { transfer: "解锁并转存", copy: "解锁并复制链接", cloud: "解锁并开启云下载" };
  var RE0_DONE_ACTION_LABEL = { transfer: "转存到 115", copy: "复制链接", cloud: "云下载到 115" };
  function re0PointsLabel(c) {
    if (c.state === "already_unlocked" || c.is_unlocked_upstream) return "RE0 已解锁";
    if (c.unlock_points === 0) return "免费解锁";
    if (c.unlock_points == null) return "积分以 RE0 返回为准";
    return "需 " + c.unlock_points + " 积分";
  }
  // RE0 reports a share's size either as bytes or as a ready-made string.
  function re0SizeLabel(v) {
    if (v == null || v === "") return "";
    if (typeof v === "number") {
      if (v >= 1073741824) return (v / 1073741824).toFixed(2) + " GB";
      if (v >= 1048576) return (v / 1048576).toFixed(1) + " MB";
      return v + " B";
    }
    return String(v);
  }
  // §6.1: publisher line and composition tooltip. A missing date simply
  // renders nothing -- never a guessed "today".
  function re0PublishedLabel(iso) {
    if (!iso || typeof iso !== "string" || iso.length < 10) return "";
    return "发布于 " + iso.slice(0, 10);
  }
  // A film's own remark already says what the release is, so a movie card
  // drops the composition entirely. A series keeps it -- that is how you tell
  // a whole season from three episodes. Only an explicit "movie" hides it:
  // an absent or unfamiliar type keeps the information rather than guessing
  // it away. The data itself is untouched; this is a rendering choice.
  function showRe0Composition(mediaType) {
    return mediaType !== "movie";
  }
  var RE0_COMPLETION_LABEL = { complete: "已完结", updating: "更新中", partial: "部分集数" };
  function re0CompletionLabel(composition) {
    if (!composition) return "";
    var parts = [RE0_COMPLETION_LABEL[composition.completion] || ""];
    if (composition.confidence === "file_inferred") parts.push("据文件名推断");
    else if (composition.confidence === "declared") parts.push("据发布者备注");
    return parts.filter(Boolean).join(" · ");
  }
  // §6.2: the preview control -- its own action, never called 展开, and never
  // offered for a pan or account tier that already answered "cannot".
  function re0PreviewControlHtml(c) {
    var p = c.file_preview || {};
    // §3.4/§4.6: a protocol link has no directory to list -- no control, and
    // no note pretending something failed.
    if (p.status === "not_applicable") return "";
    if (p.available === false) {
      var why = p.status === "forbidden" ? "当前账号等级不支持文件预览" : "该来源暂不支持文件预览";
      return '<span class="re0-preview-note">' + esc(why) + '</span>';
    }
    var label = p.status === "ready" && p.file_count != null ? "文件预览（" + p.file_count + " 个文件）" : "文件预览";
    return '<button type="button" class="tertiary re0-preview-btn" data-re0-preview="' + esc(c.id) + '">' + esc(label) + '</button>';
  }
  // §9.1 next-episode / new-season line: server-formatted Asia/Shanghai
  // time, no external link; clicking scrolls to the local groups.
  function calendarCtaHtml(event) {
    if (!event || !event.label) return "";
    var extra = event.episode_title ? " · " + esc(event.episode_title) : "";
    return '<button type="button" class="calendar-cta" data-season="' + esc(event.season != null ? event.season : "") + '">' +
      '<span class="calendar-cta-label">' + esc(event.label) + '</span><span class="calendar-cta-time">' + esc(event.display || "") + extra + '</span></button>';
  }
  function re0SettingsPayload() {
    var cap = parseInt($("re0DailyCap").value, 10);
    var interval = parseInt($("re0MinInterval").value, 10);
    return {
      re0_daily_request_cap: isNaN(cap) || cap < 1 ? 100 : cap,
      re0_min_interval_ms: isNaN(interval) || interval < 500 ? 1000 : interval
    };
  }
  function re0SyncSummary(d) {
    if (!d) return "无法读取 RE0 同步状态";
    var b = d.budget || {};
    var p = d.projections || {};
    var total = (p.pending || 0) + (p.partial || 0) + (p.complete || 0) + (p.retryable || 0) + (p.failed || 0);
    var todo = (p.pending || 0) + (p.partial || 0) + (p.retryable || 0);
    var parts = [d.authorized ? "已授权" : (d.configured ? "未授权" : "未配置"), "今日请求 " + (b.used_today || 0) + "/" + (b.daily_cap || 0)];
    if (b.cooldown_until) parts.push("冷却中");
    parts.push("投影 " + total + "（待补全 " + todo + "）", "候选 " + ((d.resources || {}).candidate || 0), "已解锁 " + ((d.resources || {}).unlocked || 0),
      "今日动作 " + (d.actions_today || 0), "模式：" + (d.direct_search ? "RE0 直接搜索" : "TMDB→RE0"));
    if (d.last_error_class) parts.push("最近错误：" + d.last_error_class);
    var sync = d.sync || {};
    if (sync.last_run) {
      parts.push("最近回补：" + (sync.last_run.phase || "") + " " + (sync.last_run.status || "") + "（请求 " + (sync.last_run.requested || 0) + "）" +
        (sync.queue_remaining != null ? " · 待回补 " + sync.queue_remaining : ""));
    }
    return parts.join(" · ");
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
  // 115 云下载 settings card (spec §6): the three keys /api/settings
  // accepts, read from the card's own controls; blank/invalid numbers
  // fall back to the server defaults (0 = unlimited, 30 per submit).
  function cloudSavePayload() {
    var daily = parseInt($("cloudDailyCap").value, 10);
    var per = parseInt($("cloudPerSubmitCap").value, 10);
    return {
      cloud_download_enabled: !!$("cloudEnabled").checked,
      cloud_download_daily_cap: isNaN(daily) || daily < 0 ? 0 : daily,
      cloud_download_per_submit_cap: isNaN(per) || per < 1 ? 30 : per
    };
  }
  function cloudStatusSummary(d) {
    if (!d) return "服务器当前：无法读取云下载状态";
    var parts = [d.enabled ? "云下载已启用" : "云下载未启用"];
    if (d.enabled) {
      parts.push("今日已提交 " + (d.today_submitted || 0) + (d.daily_cap ? "/" + d.daily_cap + " 条" : " 条（不限）"));
    }
    parts.push(d.token_available ? "令牌已同步" : "令牌未同步，请先在 OpenList 中登录 115");
    return "服务器当前：" + parts.join(" · ");
  }
  // 云下载 tab helpers (spec §6): status pill class, quota line, sizes.
  function cloudStatusClass(status) {
    return { "-2": "deleted", "-1": "failed", "0": "todo", "1": "running", "2": "done" }[String(status)] || "unknown";
  }
  function cloudQuotaSummaryText(q) {
    if (!q) return "配额未知";
    var today = "今日已提交 " + (q.today_submitted || 0) + (q.daily_cap ? "/" + q.daily_cap : "") + " 条";
    return "本月配额 " + (q.count != null ? q.count : "?") + " · 已用 " + (q.used != null ? q.used : "?") + " · 剩余 " + (q.surplus != null ? q.surplus : "?") + " · " + today;
  }
  function cloudFormatSize(bytes) {
    var n = Number(bytes);
    if (!n || n < 0) return "—";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? String(n) : n.toFixed(1)) + " " + units[i];
  }
  // Per-link outcome list for the dialog after a cloud-download submit:
  // 已提交 / 失败：原因 / 未提交 (batch stopped before reaching it). Words,
  // not symbols; never the info_hash or a URL.
  function cloudResultsHtml(d) {
    var items = (d.results || []).map(function (r) {
      var cls = r.state === "ok" ? "cloud-result-ok" : (r.state === "failed" ? "cloud-result-failed" : "cloud-result-skipped");
      var text = r.state === "ok" ? "已提交" : (r.state === "failed" ? "失败：" + (r.message || "115 未接受") : "未提交");
      return '<li class="' + cls + '"><span class="cloud-result-label">' + esc(r.label || "") + '</span><span class="cloud-result-text">' + esc(text) + '</span></li>';
    }).join("");
    var summary = esc(d.message || "") + (d.quota_surplus != null ? " · 剩余配额 " + esc(d.quota_surplus) : "");
    return '<div class="cloud-result-summary">' + summary + '</div><ul class="cloud-result-list">' + items + '</ul>';
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
  var HERO_REFRESH_INTERVAL_MS = 60 * 60 * 1000;
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
            if (d.retry_after) err.retry_after = d.retry_after;
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
    // Phase 3: what /api/me said about this session. Display only -- the
    // server refuses what it refuses regardless of anything in here.
    me: null,
    openPaths: { "115pan": "/115pan", "115strm": "/115strm" },
    currentOpenPath: "/115pan",
    currentOpenPreset: "115pan",
    transferRoot: "/115pan",
    // The browsed OpenList path is the transfer target. Legacy paths are
    // resolved server-side; diagnostics never supply a root PID or CID.
    transferRootPid: "",
    // Phase 6 (R05): a user who authorised their own 115 browses their own
    // account by cid, not through the administrator's OpenList mount. The
    // server decides which of the two it is (the `mode` on the folder
    // response); ownCids stays false until it says "115".
    transferOwnCids: false,
    transferOwnStack: [],
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
      // Switching workspace must not strand the reader with a hidden header;
      // this only changes the header's own class, never the scroll position.
      stickyHeader.reveal();
      document.querySelectorAll("[data-tab]").forEach(function (button) {
        var selected = button.dataset.tab === tab;
        button.setAttribute("aria-selected", selected ? "true" : "false");
        button.tabIndex = selected ? 0 : -1;
        // Keep the active tab visible in the horizontally-scrolling tab bar
        // (T7 §6), e.g. after a direct deep link or arrow-key navigation.
        if (selected) button.scrollIntoView({ block: "nearest", inline: "nearest" });
      });
      TAB_IDS.forEach(function (id) { var panel = $(id); if (panel) panel.hidden = id !== tab; });
      // A detail view pushed while on the library tab is only valid for the
      // *current* library visit -- leaving the tab (T7 §7) means a later
      // history.back() from closeDetail() could land on an intervening
      // tab-switch history entry instead of the results view.
      if (tab !== "library") state.detailPushed = false;
      if (tab === "library" && views.library) views.library.onEnter();
      if (tab === "openlist" && $("openlist") && !state.openlistLoaded) { state.openlistLoaded = true; views.browser.openlist.load(state.currentOpenPath); }
      if (tab === "strm" && $("strm") && !state.strmLoaded) { state.strmLoaded = true; views.browser.strm.load(""); }
      if (views.cloud) { if (tab === "cloud") views.cloud.enter(); else views.cloud.leave(); }
      if (tab === "settings" && capability("global_settings")) views.account.loadUsers();
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
      if (!$("transferDialog")) {
        // This page was built without the transfer dialog, which means this
        // user has not connected 115 yet. Say what to do rather than throw.
        toast("请先在设置页完成「一键转存」扫码连接。", "error");
        return;
      }
      state.transferMode = "library";
      state.transferTitle = title;
      $("transferShareField").hidden = true;
      $("transferServerHeldField").hidden = false;
      $("transferCloudField").hidden = true;
      $("saveBtn").textContent = "转存到 115";
      $("saveBtn").disabled = false;
      $("transferLinkId").value = linkId;
      $("transferShareUrl").value = "";
      views.transfer.openCommon(title, false);
    },
    // 115 云下载 (spec §6): the same dialog and folder picker, submitting
    // one resource group's ED2K/magnet links to 115's offline downloader.
    // The subtitle shows the month's remaining quota once fetched; more
    // links than the per-submit cap disables the button outright.
    openCloud: function (linkIds, title, labels) {
      if (!$("transferDialog")) {
        // F01: cloud download needs step B, and the dialog is shipped to
        // whoever has either step. Say which button, rather than throw.
        toast("请先在设置页完成「目录与云下载」授权。", "error");
        return;
      }
      state.transferMode = "cloud";
      state.transferTitle = title;
      state.cloudLinkIds = linkIds;
      $("transferShareField").hidden = true;
      $("transferServerHeldField").hidden = true;
      $("transferCloudField").hidden = false;
      $("transferCloudList").innerHTML = (labels || []).map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("");
      $("transferLinkId").value = "";
      $("transferShareUrl").value = "";
      views.transfer.openCommon(title, false);
      $("transferTitle").textContent = "云下载：" + (title || "");
      var btn = $("saveBtn");
      btn.textContent = "云下载到 115";
      var cap = state.cloudPerSubmitCap || 30;
      if (linkIds.length > cap) {
        btn.disabled = true;
        $("transferSubtitle").textContent = linkIds.length + " 条链接 · 单次最多 " + cap + " 条，请分批提交";
        return;
      }
      btn.disabled = false;
      $("transferSubtitle").textContent = linkIds.length + " 条链接 · 配额读取中…";
      api.request("/api/library/cloud-download/quota").then(function (q) {
        $("transferSubtitle").textContent = linkIds.length + " 条链接 · 本月剩余配额 " + (q && q.surplus != null ? q.surplus : "未知");
      }).catch(function () {
        $("transferSubtitle").textContent = linkIds.length + " 条链接 · 配额未知";
      });
    },
    close: function () {
      if (state.transferBusy) { toast("转存正在进行，请稍候。", "error"); return; }
      $("transferDialog").hidden = true;
      document.body.classList.remove("modal-open");
      if (state.transferTrigger) state.transferTrigger.focus();
    },
    // The browsed legacy OpenList path is submitted with an empty PID;
    // resolve_115_target_path performs the directory lookup server-side.
    // Phase 6 (R05): one folder of this user's own 115, addressed by cid.
    // `stack` is the breadcrumb, and the deepest entry is the target -- a
    // cid the server proves against this user's own token at submit time,
    // so the displayed name is never the boundary.
    loadOwnFolders: function (cid) {
      state.currentTransferPath = "";
      state.currentTransferPid = cid;
      var label = state.transferOwnStack.map(function (n) { return n.name; }).join(" / ");
      label = "我的 115" + (label ? " / " + label : "");
      $("folderCurrent").textContent = label;
      $("targetPath").textContent = label;
      return api.request("/api/115/folders?cid=" + encodeURIComponent(cid)).then(function (d) {
        var box = $("folderResult");
        var items = d.items || [];
        state.transferLoadFailed = false;
        if (!state.transferBusy) $("saveBtn").disabled = false;
        if (!items.length) { setEmpty("folderResult", "当前目录没有子文件夹。"); return; }
        box.innerHTML = items.map(function (x) {
          return '<button type="button" class="folder" data-cid="' + esc(x.cid) + '" data-name="' + esc(x.name) + '"><span class="folder-name">' + esc(x.name) + '</span><span class="folder-action">打开</span></button>';
        }).join("");
        box.querySelectorAll(".folder").forEach(function (b) {
          b.onclick = function () {
            state.transferOwnStack.push({ cid: b.dataset.cid, name: b.dataset.name });
            views.transfer.loadOwnFolders(b.dataset.cid);
          };
        });
      }).catch(views.transfer.folderFailed);
    },
    // A user who has not authorised "目录与云下载" still transfers -- the
    // share lands in their own 115 default inbox (plan §4.2). Say which
    // button grants folder choice instead of showing a bare error.
    folderFailed: function (e) {
      if (e && e.code === "OPEN115_NOT_AUTHORIZED") {
        state.currentTransferPath = "";
        state.currentTransferPid = "";
        $("folderCurrent").textContent = "115 默认接收目录";
        $("targetPath").textContent = "115 默认接收目录";
        setEmpty("folderResult", (e.message || "尚未完成「目录与云下载」授权。") +
          "在设置页完成授权后即可选择目录；现在提交将转存到你自己的 115 默认接收位置。");
        state.transferLoadFailed = false;
        if (!state.transferBusy) $("saveBtn").disabled = false;
        return;
      }
      setEmpty("folderResult", friendlyFolderError(e && e.message));
      // §12.5/§12.6: a directory that failed to load must not let the
      // user submit against a stale/unconfirmed path. Flagging the
      // failure here is what lets openCommon's guard above retry on
      // the next dialog open instead of leaving saveBtn disabled for
      // the rest of the page session.
      state.transferLoadFailed = true;
      $("saveBtn").disabled = true;
    },
    loadFolders: function (path) {
      if (state.transferOwnCids) return views.transfer.loadOwnFolders(state.currentTransferPid || "0");
      path = path === undefined ? state.currentTransferPath : path;
      state.currentTransferPath = path;
      state.currentTransferPid = path === state.transferRoot ? state.transferRootPid : "";
      $("folderCurrent").textContent = path;
      $("targetPath").textContent = path;
      return api.request("/api/115/folders?path=" + encodeURIComponent(path)).then(function (d) {
        if (d.mode === "115") {
          state.transferOwnCids = true;
          state.transferOwnStack = [];
          return views.transfer.loadOwnFolders(d.cid || "0");
        }
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
      }).catch(views.transfer.folderFailed);
    },
    init: function () {
      $("folderUp").onclick = function () {
        if (state.transferOwnCids) {
          if (!state.transferOwnStack.length) return;
          state.transferOwnStack.pop();
          var parent = state.transferOwnStack.length
            ? state.transferOwnStack[state.transferOwnStack.length - 1].cid : "0";
          views.transfer.loadOwnFolders(parent);
          return;
        }
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
        if (state.transferMode === "cloud") {
          if (!state.cloudLinkIds || !state.cloudLinkIds.length) { feedback("transferResult", "缺少链接信息。", "error"); return; }
          request = api.request("/api/library/cloud-download", {
            method: "POST",
            body: JSON.stringify({ resource_link_ids: state.cloudLinkIds, target_path: targetPath, target_pid: state.currentTransferPid }),
          });
        }
        var cloudMode = state.transferMode === "cloud";
        state.transferBusy = true;
        var btn = $("saveBtn");
        var originalLabel = btn.textContent;
        btn.disabled = true;
        btn.classList.add("loading");
        btn.textContent = cloudMode ? "提交中…" : "转存中…";
        request.then(function (d) {
          if (cloudMode) {
            var box = $("transferResult");
            box.innerHTML = cloudResultsHtml(d);
            box.className = "feedback " + (d.success ? "success" : "warn");
            box.hidden = false;
            return;
          }
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
  // `reloaded` is the G06 one-shot: a capability-driven page reload happens at
  // most once per page load, so a scan can never put the browser in a loop.
  state.reauth = { challengeId: null, expiresAt: 0, pollTimer: null, countdownTimer: null, trigger: null, polling: false, returnTo: null, reloaded: false };

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
          // G06: /api/status is the administrator's. A member scanning for
          // the first time needs *their* state -- which of their own 115
          // steps are connected, and what their capabilities now allow. This
          // resolves once either way, so the dialog never strands on
          // "正在验证…".
          views.reauth.afterAuthenticated().then(function () {
            feedback("reauthResult", "115 已重新授权，转存功能已恢复。", "success");
          }, function () {
            // A network hiccup here must not strand the dialog either -- the
            // close below is unconditional.
          });
          setTimeout(function () {
            views.reauth.close();
            // G06: the page is rendered per capability on the server, so a
            // user who had no transfer dialog before this scan has none now
            // either. One controlled reload of the *current in-site* location
            // gives them the page their new capability earns. Guarded so it
            // happens at most once per page, and only when the dialog the
            // next step needs is genuinely absent.
            if (views.reauth.needsReload()) {
              views.reauth.reloadForNewCapability(returnTo);
              return;
            }
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
      if (!$("reauthDialog")) return;
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
    // G06: what to refresh after a successful scan, for whoever scanned.
    // The administrator's deployment-wide status is theirs alone; everybody
    // gets their own account, their own 115 state and the cloud switch.
    afterAuthenticated: function () {
      var jobs = [views.account.load(), views.my115.load()];
      if (capability("global_settings")) jobs.push(refreshStatus());
      else if (capability("own_115_cloud_download")) jobs.push(refreshCloudStatus());
      return Promise.all(jobs.map(function (job) {
        return Promise.resolve(job).catch(function () { return null; });
      }));
    },
    // True when this user's capabilities now allow something the page it is
    // looking at was not built with. The server decides the markup, so the
    // only honest way to get it is to ask for the page again.
    needsReload: function () {
      if (state.reauth.reloaded) return false;
      var wantsTransfer = capability("own_115_transfer") || capability("own_115_cloud_download");
      return !!(wantsTransfer && !$("transferDialog"));
    },
    reloadForNewCapability: function (returnTo) {
      state.reauth.reloaded = true;
      var url = new URL(window.location.href);
      if (returnTo && returnTo.tab) url.searchParams.set("tab", returnTo.tab);
      // Same origin, same path, same query -- never a location from anywhere
      // but the address the user is already on.
      window.location.replace(url.pathname + url.search);
    },
    init: function () {
      // F01: the dialog's own parts are the precondition for this init and
      // are always there; `reauth115Btn` is an *external* trigger that only
      // the administrator's settings card carries. Binding it unconditionally
      // threw on a member's page and took every later initialiser with it.
      // A member reaches the same dialog through the 我的 115 card.
      var settingsTrigger = $("reauth115Btn");
      if (settingsTrigger) settingsTrigger.onclick = views.reauth.open;
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
    // Round 17: what the home hero currently shows (server business day +
    // media id) so a refresh on the same day never re-renders/reloads it.
    hero: { date: null, mediaId: null, fetchedAt: 0, items: [], generation: 0, changedAt: 0, paused: false },
    sort: "relevance", page: 1, media: null,
    total: 0, pageSize: 25, filtersData: null, suggestTimer: null,
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
    { key: "tv", title: "剧集", endpoint: "/api/library/search?type=tv&sort=links_desc&page_size=12" },
    // RE0 discoveries (spec §10.2): complete remote-only projections with
    // candidates -- rendered as remote cards, never part of the hero pick.
    { key: "re0", title: "RE0 新发现", endpoint: "/api/library/re0/discoveries?limit=12", remote: true }
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
      if (interpreted.year && !state.library.year) {
        chips.push({
          label: "年份 " + interpreted.year,
          clear: spans.year
            ? function () { views.library.clearInterpretedSpan("year", spans); }
            : function () { state.library.year = ""; $("filterYear").value = ""; }
        });
      }
      if (interpreted.quality && !state.library.quality.length) {
        chips.push({
          label: String(interpreted.quality),
          clear: spans.quality
            ? function () { views.library.clearInterpretedSpan("quality", spans); }
            : function () { views.library.toggleChip("filterQuality", interpreted.quality, "quality"); }
        });
      }
      (state.library.provider.length ? [] : interpreted.providers || []).forEach(function (pv) {
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
      var sources = item.sources || [];
      var sourceBadge = sources.indexOf("re0") !== -1
        ? '<span class="card-re0-badge card-source-badge">' + (sources.indexOf("local") !== -1 ? "本地 + RE0" : "RE0") + '</span>' : '';
      // card-meta always renders its wrapper span (even empty) so
      // .card-footer's fixed-height rows (static/app.css) never shift.
      return '<button type="button" class="media-card" data-media-id="' + esc(item.media_id) + '">' +
        '<span class="poster">' + poster + fallback + primaryRatingBadgeHtml(item.primary_rating) + (item.has_usable_re0 ? "" : cardInvalidBadgeHtml(item.all_links_invalid)) + sourceBadge + '</span>' +
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
    correctResultPage: function (d) {
      // A provisional local count must never override the merged catalog's
      // page, and a late response must not navigate out of another view.
      if ($("library").hidden || state.library.media) return false;
      var totalPages = Math.max(1, Math.ceil((d.total || 0) / (d.page_size || state.library.pageSize)));
      if (d.total > 0 && (d.page || state.library.page) > totalPages) {
        state.library.page = totalPages;
        views.library.search(false);
        return true;
      }
      return false;
    },
    renderResults: function (d, provisional) {
      if (!provisional && views.library.correctResultPage(d)) return;
      views.library.setLibraryControlsDisabled(false);
      state.library.total = d.total || 0;
      state.library.pageSize = d.page_size || state.library.pageSize;
      var items = d.items || [];
      var totalPages = Math.max(1, Math.ceil(state.library.total / state.library.pageSize));
      $("libraryStatus").textContent = "找到 " + state.library.total + " 个标题 · 第 " + (d.page || 1) + " / " + totalPages + " 页";
      views.library.renderChips(d.interpreted);
      if (!items.length) {
        views.library.showState("libraryEmpty"); $("libraryPagination").innerHTML = "";
        return;
      }
      var box = $("libraryResults");
      box.innerHTML = items.map(views.library.mediaCardHtml).join(""); // esc(...) happens inside mediaCardHtml
      views.library.bindMediaCards(box);
      views.library.showState("libraryResults");
      views.library.renderPagination(d.page || 1, totalPages);
    },
    // Separate card style retained only for the home discovery rail.
    remoteCardHtml: function (item) {
      var poster = item.poster_url ? '<img src="' + esc(item.poster_url) + '" alt="" loading="lazy">' : "";
      var typeIcon = item.media_type === "tv" ? "icon-tv" : "icon-film";
      var fallback = '<span class="poster-fallback"' + (item.poster_url ? " hidden" : "") + '>' +
        '<svg class="icon" aria-hidden="true"><use href="' + ICONS_URL + '#' + typeIcon + '"></use></svg>' +
        '<span>' + esc(item.title) + '</span></span>';
      var unlocked = item.state === "unlocked" || item.state === "partial";
      var badge = '<span class="card-remote-badge' + (unlocked ? " unlocked" : "") + '">' + (unlocked ? "RE0 已解锁" : "RE0 待解锁") + '</span>';
      var target = item.local_media_id != null ? String(item.local_media_id) : item.media_ref;
      return '<button type="button" class="media-card media-card-remote" data-media-ref="' + esc(item.media_ref) + '" data-media-id="' + esc(target) + '">' +
        '<span class="poster">' + poster + fallback + badge + '</span>' +
        '<span class="card-footer">' +
        '<span class="card-title">' + esc(item.title) + '</span>' +
        '<span class="card-meta">' + esc(formatCardMeta(item.year, item.media_type)) + '</span>' +
        '</span>' +
        '</button>';
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
      state.library.searchGeneration = (state.library.searchGeneration || 0) + 1;
      $("libraryRails").hidden = false;
      $("libraryRemote").hidden = true;
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
      state.library.hero.generation++;
      state.library.hero.loading = false;
    },
    // Five high-rated local titles, refreshed in server-side two-day editions.
    // Preload each backdrop before switching all of its copy and its CTA.
    heroOnScreen: function () {
      return !$("library").hidden && !$("libraryHome").hidden && !state.library.media && views.library.isBrowsing();
    },
    loadHero: function () {
      return views.library.fetchChannel("hero", "/api/library/recommendations?placement=hero&limit=5")
        .then(function (d) {
          if (!d || !views.library.heroOnScreen()) return;
          state.library.hero.fetchedAt = Date.now();
          views.library.applyHeroCandidates(d.items || [], d.rotation_date || d.date || "");
        }).catch(function () {
          if (!state.library.hero.mediaId) $("libraryHero").hidden = true;
        });
    },
    applyHeroCandidates: function (items, date) {
      var candidates = items.filter(function (item) { return item.backdrop_url && item.overview_short; }).slice(0, 5);
      var hero = $("libraryHero");
      var current = state.library.hero;
      current.generation = (current.generation || 0) + 1;
      current.items = candidates;
      current.itemsDate = date;
      current.automaticRequest = false;
      current.loading = false;
      var thumbnailKey = JSON.stringify(candidates.map(function (item) { return [item.media_id, item.title, item.poster_url]; }));
      if (current.thumbnailKey !== thumbnailKey) {
        current.thumbnailKey = thumbnailKey;
        views.library.renderHeroThumbnails();
      }
      if (candidates.some(function (item) { return item.media_id === current.mediaId; }) && current.date === date) {
        hero.hidden = false;
        views.library.syncHeroControls();
        return;
      }
      if (!candidates.length) {
        current.mediaId = null;
        hero.hidden = true;
        return;
      }
      views.library.tryHeroCandidate(candidates, 0, date);
    },
    tryHeroCandidate: function (candidates, index, date) {
      var hero = $("libraryHero");
      var current = state.library.hero;
      var generation = current.generation = (current.generation || 0) + 1;
      var automatic = current.automaticRequest;
      if (index >= candidates.length) {
        current.changedAt = Date.now();
        views.library.syncHeroControls();
        if (!current.mediaId) hero.hidden = true;
        return;
      }
      var pick = candidates[index];
      // Preload off-DOM so a stale backdrop_url never leaves the
      // .library-hero block showing a broken image; bound listeners,
      // never inline handler attributes (T8 §6/§9). On error, fall
      // through to the next candidate.
      var probe = new Image();
      current.loading = true;
      var timeout = setTimeout(failed, 12000);
      probe.addEventListener("load", function () {
        clearTimeout(timeout);
        if (current.generation !== generation) return;
        current.loading = false;
        if (!views.library.heroOnScreen() || (automatic && views.library.heroAutoBlocked())) return;
        var imageRatio = probe.naturalWidth / probe.naturalHeight;
        hero.querySelector(".library-hero-frame").style.setProperty("--hero-image-ratio",
          String(Number.isFinite(imageRatio) && imageRatio > 0 ? imageRatio : 16 / 9));
        hero.querySelector(".library-hero-backdrop").src = pick.backdrop_url;
        var rating = pick.primary_rating;
        hero.querySelector(".library-hero-type").textContent = ["高分推荐", MEDIA_TYPE_LABEL[pick.media_type], pick.year,
          rating ? rating.source.toUpperCase() + " " + Number(rating.score).toFixed(1) : ""].filter(Boolean).join(" · ");
        hero.querySelector(".library-hero-title").textContent = pick.title || "";
        hero.querySelector(".library-hero-overview").textContent = pick.overview_short || "";
        hero.querySelector(".library-hero-cta").onclick = function () { views.library.openDetail(pick.media_id); };
        state.library.hero.date = date;
        state.library.hero.mediaId = pick.media_id;
        current.changedAt = Date.now();
        views.library.syncHeroControls();
        hero.hidden = false;
      });
      function failed() {
        clearTimeout(timeout);
        if (current.generation !== generation) return;
        current.loading = false;
        if (!views.library.heroOnScreen()) return;
        current.items = current.items.filter(function (item) { return item.media_id !== pick.media_id; });
        views.library.tryHeroCandidate(candidates, index + 1, date);
      }
      probe.addEventListener("error", failed);
      probe.src = pick.backdrop_url;
    },
    syncHeroControls: function () {
      var current = state.library.hero;
      var count = current.items.length;
      var index = current.items.findIndex(function (item) { return item.media_id === current.mediaId; });
      $("heroControls").hidden = count < 2;
      $("heroPosition").textContent = (index + 1) + " / " + count;
      $("heroPause").textContent = current.paused ? "播放" : "暂停";
      $("heroPause").setAttribute("aria-label", current.paused ? "播放推荐轮播" : "暂停推荐轮播");
      $("heroPause").onclick = function () { current.paused = !current.paused; current.changedAt = Date.now(); views.library.syncHeroControls(); };
      $("heroPrevious").onclick = function () { views.library.advanceHero(-1, true); };
      $("heroNext").onclick = function () { views.library.advanceHero(1, true); };
      $("heroThumbnails").querySelectorAll("button").forEach(function (button) {
        button.setAttribute("aria-pressed", String(Number(button.dataset.mediaId) === current.mediaId));
        button.disabled = !current.items.some(function (item) { return item.media_id === Number(button.dataset.mediaId); });
      });
    },
    renderHeroThumbnails: function () {
      var box = $("heroThumbnails");
      var current = state.library.hero;
      box.replaceChildren();
      current.items.forEach(function (item) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "library-hero-thumb";
        button.dataset.mediaId = String(item.media_id);
        button.setAttribute("aria-label", "切换到" + item.title);
        var image = document.createElement("img");
        image.src = item.poster_url;
        image.alt = "";
        button.appendChild(image);
        button.onclick = function () {
          var latest = current.items.find(function (candidate) { return candidate.media_id === item.media_id; });
          if (!latest) return;
          current.changedAt = Date.now();
          current.automaticRequest = false;
          views.library.tryHeroCandidate([latest], 0, current.itemsDate);
        };
        box.appendChild(button);
      });
      box.hidden = current.items.length < 2;
    },
    advanceHero: function (direction, manual) {
      var current = state.library.hero;
      var items = current.items;
      if (!views.library.heroOnScreen() || document.visibilityState === "hidden" || items.length < 2) return;
      if (!manual && (current.loading || views.library.heroAutoBlocked() || Date.now() - current.changedAt < 8000)) return;
      var index = items.findIndex(function (item) { return item.media_id === current.mediaId; });
      var next = (index + direction + items.length) % items.length;
      current.changedAt = Date.now();
      current.automaticRequest = !manual;
      views.library.tryHeroCandidate(items.slice(next).concat(items.slice(0, next)), 0, current.itemsDate);
    },
    heroAutoBlocked: function () {
      var hero = $("libraryHero");
      return state.library.hero.paused || document.visibilityState === "hidden" || hero.matches(":hover") ||
        hero.contains(document.activeElement) || (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    },
    // Round 17: a long-open tab re-asks the server on focus/visibility (and
    // hourly) -- only while the library home is actually on screen -- and
    // applyHeroCandidates above changes nothing unless the server's
    // business day (and so its pick) moved on.
    maybeRefreshHero: function () {
      if (state.library.media || !views.library.isBrowsing()) return;
      if ($("libraryHome").hidden) return;
      if (Date.now() - state.library.hero.fetchedAt < 60000) return;
      views.library.loadHero();
    },
    browseRail: function (key) {
      if (["year_desc", "movie", "tv"].indexOf(key) === -1) return;
      var l = state.library;
      l.q = "";
      l.type = key === "year_desc" ? "all" : key;
      l.year = "";
      l.provider = [];
      l.quality = [];
      l.hdr = [];
      l.genre = [];
      l.source = [];
      l.includeDeleted = false;
      l.sort = key === "year_desc" ? "year_desc" : "links_desc";
      l.page = 1;
      l.media = null;
      state.detailPushed = false;
      views.library.hideSuggest();
      views.library.closeFilters(false);
      views.library.applyToForm();
      views.library.search(true);
      var query = $("libraryQuery");
      query.focus({ preventScroll: true });
      query.closest(".library-search-card").scrollIntoView({ block: "start", behavior: "auto" });
    },
    // Content rails (T4 §1): fixed rails, each with 12 items, hidden
    // individually when empty; wording never claims popularity/trend data.
    loadRails: function () {
      var box = $("libraryRails");
      box.innerHTML = LIBRARY_RAILS.map(function (rail) {
        return '<section class="rail" data-rail="' + rail.key + '" hidden>' +
          '<div class="rail-head"><h3>' + esc(rail.title) + '</h3></div>' +
          '<div class="rail-track"></div></section>';
      }).join("");
      LIBRARY_RAILS.forEach(function (rail) {
        if (["year_desc", "movie", "tv"].indexOf(rail.key) !== -1) {
          var heading = box.querySelector('.rail[data-rail="' + rail.key + '"] h3');
          var button = document.createElement("button");
          button.type = "button";
          button.className = "rail-heading-button";
          button.textContent = rail.title;
          var more = document.createElement("span");
          more.className = "rail-heading-more";
          more.textContent = "查看更多 →";
          more.setAttribute("aria-hidden", "true");
          button.appendChild(more);
          button.onclick = function () { views.library.browseRail(rail.key); };
          heading.textContent = "";
          heading.appendChild(button);
        }
        views.library.fetchChannel("rail:" + rail.key, rail.endpoint)
          .then(function (d) {
            if (!d) return;
            var items = d.items || [];
            var section = box.querySelector('.rail[data-rail="' + rail.key + '"]');
            if (!section || !items.length) { if (section) section.hidden = true; return; }
            section.querySelector(".rail-track").innerHTML = items.map(rail.remote ? views.library.remoteCardHtml : views.library.mediaCardHtml).join("");
            views.library.bindMediaCards(section);
            section.hidden = false;
          }).catch(function () { /* rails are optional home decoration */ });
      });
    },
    syncPageSize: function (preserveItem) {
      var grid = $("libraryResults");
      if (!grid || grid.hidden || !grid.clientWidth) return false;
      var tracks = window.getComputedStyle(grid).gridTemplateColumns.trim().split(/\s+/);
      var columns = tracks.filter(function (track) { return parseFloat(track) > 0; }).length;
      if (!columns || columns > 50) return false;
      // Up to five full rows, within the search API's 50-item limit.
      var size = columns * Math.min(5, Math.floor(50 / columns));
      var l = state.library;
      if (size === l.pageSize) return false;
      if (preserveItem) l.page = Math.floor((l.page - 1) * l.pageSize / size) + 1;
      l.pageSize = size;
      return true;
    },
    resizeResults: function () {
      if ($("library").hidden || state.library.media || views.library.isBrowsing()) return;
      if (views.library.syncPageSize(true)) views.library.search(false);
    },
    runSearch: function () {
      views.library.renderSkeleton();
      views.library.syncPageSize(false);
      $("libraryRemote").hidden = true;
      var generation = state.library.searchGeneration = (state.library.searchGeneration || 0) + 1;
      var query = views.library.buildQuery();
      var current = function () { return generation === state.library.searchGeneration; };
      var unifiedReady = false;
      var localReady = false;
      var localData = null;
      var federated = !!state.library.q.trim() && state.library.type !== "unknown";
      var localError = null;
      var showError = function (e) {
        if (!current()) return;
        if (e && e.code === "LIBRARY_NOT_INSTALLED") {
          views.library.setLibraryControlsDisabled(true);
          views.library.showState("libraryNotInstalled");
          return;
        }
        $("libraryErrorText").textContent = e.message || "搜索失败";
        views.library.showState("libraryError");
      };
      var status = function (text) {
        if (!current()) return;
        $("libraryRemote").hidden = !text;
        $("libraryRemoteStatus").textContent = text;
        $("libraryRemoteResults").innerHTML = "";
      };
      var local = views.library.fetchChannel("search", "/api/library/search?" + query)
        .then(function (d) {
          if (!d || !current()) return;
          localReady = true;
          localData = d;
          if (!unifiedReady) views.library.renderResults(d, federated);
        })
        .catch(function (e) { localError = e; });
      if (!federated) {
        return local.then(function () { if (localError) showError(localError); });
      }
      status("正在同时搜索本地库与 RE0…");
      // Start both requests together. RE0 persists metadata and returns the
      // same catalog's ranked page, not an independently paginated card lane.
      var remote = views.library.fetchChannel("remote", "/api/library/search/re0?" + query + "&catalog=1")
        .then(function (d) {
          if (!d || !current()) return;
          if (d.catalog) {
            unifiedReady = true;
            views.library.renderResults(d.catalog);
          }
          var s = d.remote && d.remote.status;
          status(["fresh", "cached", "no_candidates"].indexOf(s) !== -1 ? "" : "RE0 暂未完成搜索，当前显示已收录的结果。");
        }).catch(function () {
          status("RE0 暂时无法查询，当前显示已收录的结果。");
        });
      return Promise.all([local, remote]).then(function () {
        if (current() && !unifiedReady && localData) views.library.correctResultPage(localData);
        if (current() && !localReady && !unifiedReady && localError) showError(localError);
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
      var gridResizeTimer = null;
      window.addEventListener("resize", function () {
        if (!$("libraryFilters").hidden) views.library.positionFilters();
        clearTimeout(gridResizeTimer);
        gridResizeTimer = setTimeout(function () { views.library.resizeResults(); }, 180);
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
      // Chrome can ignore autocomplete=off and fill an account email while
      // this search field is idle. Enable editing only while it has focus.
      $("libraryQuery").addEventListener("focus", function () { this.readOnly = false; });
      $("libraryQuery").addEventListener("blur", function () { this.readOnly = true; });
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
    var ref = String(mediaId || "");
    if (ref.indexOf("re0:") === 0) {
      // Remote-only projection (RE0 federated search): same detail view,
      // served from the projection instead of a local media row.
      var parts = ref.split(":");
      return "/api/library/re0-media/" + encodeURIComponent(parts[1] || "") + "/" + encodeURIComponent(parts[2] || "") + (params.length ? "?" + params.join("&") : "");
    }
    state.detail.includeDeleted = !!state.library.includeDeleted;
    if (state.detail.includeDeleted) params.push("include_deleted=1");
    if (state.detail.includeInvalid) params.push("include_invalid=1");
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
        // Round 18: the masked-code row (and the panel) only for a link
        // that actually has an access code -- a code-less share used to
        // show "访问码 ••••" with a 显示访问码 button that did nothing.
        var hasCode = !!views.detail.currentCode;
        $("revealPanel").hidden = !hasCode;
        $("revealCodeRow").hidden = !hasCode;
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
      // 115 云下载 (spec §6): ED2K/magnet rows get a 云下载 button ahead of
      // 复制 whenever the feature is switched on; disabled like 转存 for a
      // deleted or checker-invalid link.
      var cloudHtml = (link.provider === "ed2k" && state.cloudEnabled)
        ? '<button type="button" class="link-action link-cloud" data-link-action="cloud" data-link-id="' + esc(link.link_id) + '" data-link-label="' + esc(link.label || "") + '"' + transferDisabledAttr + '>云下载</button>'
        : "";
      return cloudHtml + actions.map(function (action) {
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
      // Round 25: only the rows a user can act on (visibleLinks -- invalid
      // and deleted links stay hidden unless include_deleted asks for the
      // audit view); the count is the live+valid rows actually shown, and
      // "全部失效" when the audit view left only dead rows. Links fold per
      // group; nothing is fetched on expand (visibleLinks reads
      // `group.links` from the media response).
      var links = visibleLinks(group, code);
      var linkCount = links.filter(linkIsUsable).length;
      var foldKey = "g:" + (code || "") + ":" + group.group_id;
      var open = groupFoldOpen(foldKey);
      var linksHtml = links.length
        ? links.map(views.detail.linkRowHtml).join("")
        : '<div class="group-links-empty">暂无可用链接</div>';
      // Round 16: a group kept only because include_deleted asked for it
      // has rows but a live count of 0 -- say so instead of "0 条链接".
      var countText = linkCount > 0 ? esc(linkCount) + " 条链接" : (links.length ? "全部失效" : "0 条链接");
      // 115 云下载 (spec §6): a whole-group button when at least two of the
      // displayed links can be cloud-downloaded (live, not checker-invalid).
      var cloudable = links.filter(function (l) { return l.provider === "ed2k" && linkIsUsable(l); });
      var groupCloudHtml = (state.cloudEnabled && cloudable.length >= 2)
        ? '<button type="button" class="tertiary group-cloud-btn" data-group-id="' + esc(group.group_id) + '" data-link-ids="' + esc(cloudable.map(function (l) { return l.link_id; }).join(",")) + '">整组云下载（' + cloudable.length + '）</button>'
        : "";
      var titleText = group.display_title || "";
      var foldHtml = foldButtonHtml(foldKey, open);
      return '<div class="detail-group' + (open ? "" : " detail-group-folded") + '" data-group-id="' + esc(group.group_id) + '">' +
        '<div class="group-summary">' +
        '<div class="group-title-row">' +
        '<span class="group-title" title="' + esc(titleText) + '">' + esc(titleText) + '</span>' +
        '<span class="group-count">' + countText + '</span>' +
        groupCloudHtml +
        '<button type="button" class="tertiary group-recheck-btn" data-group-id="' + esc(group.group_id) + '">重新检测</button>' +
        foldHtml +
        '</div>' +
        (season ? '<div class="group-season">' + esc(season) + '</div>' : "") +
        (specsHtml ? '<div class="group-specs">' + specsHtml + '</div>' : "") +
        (group.needs_review ? '<span class="badge-review">待复核' + (reasons ? "：" + esc(reasons) : "") + '</span>' : "") +
        '</div>' +
        (open ? '<div class="group-links">' + linksHtml + '</div>' : "") +
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
    // The 个版本 / 条链接 / 个来源 line. Split out of render() so a later RE0
    // refresh can rebuild it from the same facets the tabs are built from --
    // a new pan must not leave the page saying "3 个来源" above four tabs.
    summaryRowHtml: function (media, facetCount) {
      media = media || {};
      var groupCount = media.group_count != null ? media.group_count : (media.groups || []).length;
      // I2 / round 25: `media.link_count` is neither live-only nor
      // invalid-aware -- this line must match what the cards below actually
      // list, so it counts those same rows (panLinkCount).
      var linkCount = panLinkCount(media, "");
      var sourceCount = media.provider_count != null ? media.provider_count : facetCount;
      return '<span>' + esc(groupCount) + ' 个版本</span><span>' + esc(linkCount) + ' 条链接</span>' +
        (sourceCount ? '<span>' + esc(sourceCount) + ' 个来源</span>' : "");
    },
    // Round 40: rebuild the pan tabs (and the summary line) from the current
    // media. renderGroupList() only owns the cards, so without this a
    // refresh that brought new RE0 candidates in -- or dropped the ones the
    // checker had just failed -- left the counts beside the pan logos at
    // whatever they were when the page opened, and a pan that appeared only
    // in the new candidates got a card with no tab above it.
    syncProviderTabs: function () {
      var media = state.detail.media || {};
      var facets = visibleFacets(media);
      var codes = facets.map(function (f) { return f.provider; });
      var selected = state.detail.panFilter || state.detail.provider || "";
      if (selected && codes.indexOf(selected) === -1) {
        // Its tab is gone; filtering to it would leave an empty resource area.
        selected = "";
        state.detail.panFilter = "";
        if (state.detail.provider && codes.indexOf(state.detail.provider) === -1) state.detail.provider = "";
      }
      var summary = $("detailSummaryRow");
      if (summary) summary.innerHTML = views.detail.summaryRowHtml(media, facets.length);
      var box = $("libraryProviderTabs");
      if (!box) {
        // The detail opened with nothing to show at all and RE0 has since
        // found a share: the switcher has to exist before it can be filled.
        if (!facets.length) return;
        var panel = $("libraryProviderPanel");
        if (!panel || !panel.parentNode) return;
        var wrap = document.createElement("div");
        wrap.className = "provider-switcher";
        wrap.innerHTML = '<h3>网盘</h3><div id="libraryProviderTabs" class="provider-tablist" role="tablist" aria-label="网盘来源"></div>';
        panel.parentNode.insertBefore(wrap, panel);
        box = $("libraryProviderTabs");
        if (!box) return;
      }
      box.innerHTML = views.detail.renderProviderTabs(facets, selected, !!state.detail.scopedFetch);
      views.detail.bindProviderTabs();
    },
    renderProviderTabs: function (facets, selected, scopedFetch) {
      var tabs = (facets.length > 1 || scopedFetch) ? [{ code: "", label: "全部", symbol: null, count: null }] : [];
      facets.forEach(function (f) {
        var meta = PROVIDER_META_BY_CODE[f.provider];
        // Round 40: local links + the RE0 candidates not already stored as
        // one of them. `resource_count` is set by visibleFacets(); a caller
        // handing over raw server facets falls back to the plain sum.
        var count = f.resource_count != null
          ? f.resource_count
          : ((f.link_count != null || f.re0_count != null) ? (f.link_count || 0) + (f.re0_count || 0) : null);
        tabs.push({ code: f.provider, label: f.label || providerLabel(f.provider), symbol: meta ? meta.symbol : "provider-link", count: count });
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
    // Round 25: the resource area is one card per pan (two per row by
    // CSS), each holding that pan's local groups and its RE0 candidates --
    // old and new resources organised the same way, RE0 only marked. A
    // selected pan tab shows just that card.
    renderGroupList: function () {
      var code = state.detail.panFilter || state.detail.provider;
      var emptyText = code ? "该来源下暂无资源，请切换其他网盘来源" : "暂无资源";
      var cards = views.detail.panCards();
      // A list card's count comes from the server, which still counts links
      // this page hides -- so an otherwise blank area says why rather than
      // reading as "this title has nothing".
      var hidden = views.detail.hiddenLinkCount(code);
      // The count still explains an otherwise blank area, but it no longer
      // tells the reader to tick a control that is not on this page.
      var emptyHtml = '<div class="empty">' + esc(emptyText) +
        (hidden ? '<span class="empty-note">' + hidden + ' 条链接未通过检测或已失效，已隐藏。</span>' : "") + "</div>";
      var grid = cards.length
        ? '<div class="pan-grid' + (cards.length === 1 ? " pan-grid-single" : "") + '">' + cards.map(views.detail.panCardHtml).join("") + '</div>'
        : emptyHtml;
      // The resource list ends with the resources. The RE0 toolbar (hidden
      // count, "显示失效候选", "刷新 RE0 候选") is not rendered at all -- the
      // capabilities behind it stay: ensureRe0Fresh() still refreshes on open
      // within its TTL, and re0Refresh()/toggleInvalid() remain callable.
      $("detailGroupList").innerHTML = grid;
      views.detail.bindLinkActions($("detailGroupList"));
      views.detail.bindRecheckButtons($("detailGroupList"));
      views.detail.bindCloudGroupButtons($("detailGroupList"));
      views.detail.bindRe0Actions($("detailGroupList"));
      views.detail.bindGroupFolds($("detailGroupList"));
    },
    // Pans in facet order (a pan only found in links/candidates is appended);
    // a card exists when the pan has at least one visible link or candidate.
    // Round 32: a pan tab narrows the CARDS to that pan. The tab row itself
    // is built from the response's full facet list and never shrinks -- the
    // old behaviour refetched with ?provider=, which deleted the other tab
    // buttons along with their cards. 全部 brings every card straight back,
    // with no request.
    panCards: function () {
      var media = state.detail.media || {};
      var only = state.detail.panFilter || "";
      return visibleFacets(media).filter(function (f) { return !only || f.provider === only; }).map(function (f) {
        return {
          code: f.provider, label: f.label, re0: views.detail.re0RowsFor(f.provider),
          groups: (media.groups || []).filter(function (g) { return visibleLinks(g, f.provider).length > 0; }),
        };
      });
    },
    // A card: pan logo, label, "N 条链接 · M 条 RE0 候选", then a scrolling
    // body. Groups start folded when the card holds several (open when it
    // is the only one); the RE0 block starts open.
    panCardHtml: function (card) {
      var det = state.detail;
      det.foldDefaults = det.foldDefaults || {};
      var solo = card.groups.length === 1;
      card.groups.forEach(function (g) { det.foldDefaults["g:" + card.code + ":" + g.group_id] = solo; });
      det.foldDefaults["re0:" + card.code] = true;
      var linkTotal = panLinkCount(state.detail.media, card.code);
      var meta = [linkTotal ? linkTotal + " 条链接" : (card.groups.length ? "全部失效" : ""),
                  card.re0.length ? card.re0.length + " 条 RE0 候选" : ""].filter(Boolean).join(" · ");
      var metaInfo = PROVIDER_META_BY_CODE[card.code];
      var symbol = metaInfo ? metaInfo.symbol : "provider-link";
      return '<article data-pan="' + esc(card.code) + '" class="pan-card">' +
        '<header class="pan-card-head"><svg class="link-provider-icon provider-logo" aria-hidden="true"><use href="' + ICONS_URL + '#' + symbol + '"></use></svg>' +
        '<span class="pan-card-title">' + esc(card.label) + '</span><span class="pan-card-meta">' + esc(meta) + '</span></header>' +
        '<div class="pan-card-body">' +
        card.groups.map(function (g) { return views.detail.groupRowHtml(g, card.code); }).join("") +
        views.detail.re0BlockHtml(card) +
        '</div></article>';
    },
    re0BlockHtml: function (card) {
      if (!card.re0.length) return "";
      var key = "re0:" + card.code;
      var open = groupFoldOpen(key);
      return '<div class="detail-group detail-group-re0' + (open ? "" : " detail-group-folded") + '" data-re0-group="' + esc(card.code) + '">' +
        '<div class="group-summary"><div class="group-title-row">' +
        '<span class="group-title re0-group-title"><span class="re0-badge">RE0</span><span>RE0 候选</span></span>' +
        '<span class="group-count">' + card.re0.length + ' 条</span>' +
        foldButtonHtml(key, open) +
        '</div></div>' +
        (open ? '<div class="group-links">' + card.re0.map(views.detail.re0RowHtml).join("") + '</div>' : "") +
        '</div>';
    },
    // Links this page is hiding right now (0 in the 含已失效 audit view,
    // which shows them all).
    hiddenLinkCount: function (code) {
      var media = state.detail.media || {};
      if (state.detail.includeDeleted) return 0;
      return ((media.groups) || []).reduce(function (sum, g) {
        return sum + ((g.links || []).filter(function (l) {
          return (!code || l.provider === code) && !linkIsUsable(l);
        }).length);
      }, 0);
    },
    // §3.2: the audit view is a second read of the same detail, nothing more --
    // it must not disturb the pan filter or the local include_deleted view.
    // Round 37 removed its button from the UI; the capability stays.
    toggleInvalid: function () {
      state.detail.includeInvalid = !state.detail.includeInvalid;
      return views.detail.reloadRe0();
    },
    re0RowsFor: function (code) {
      var all = (state.detail.media && state.detail.media.re0_candidates) || [];
      return all.filter(function (c) { return !code || c.provider === code; });
    },
    bindGroupFolds: function (root) {
      root.querySelectorAll("[data-fold]").forEach(function (btn) {
        btn.onclick = function () { views.detail.toggleFold(btn.dataset.fold); };
      });
    },
    toggleFold: function (key) {
      state.detail.folds = state.detail.folds || {};
      state.detail.folds[key] = !groupFoldOpen(key);
      views.detail.renderGroupList();
    },
    // RE0 candidates (composition work order §6.1): one block per share,
    // carrying what a decision needs BEFORE any points are spent -- who
    // published it and when, their own remark, the composition read from it,
    // subtitles and specs -- plus a file preview that is its own action and
    // exactly ONE unlock action. No raw link, no slug, no second unlock.
    re0RowHtml: function (c) {
      var specs = c.specs || {};
      var chips = ["resolution", "dynamic_range", "source"].filter(function (k) { return specs[k]; }).map(function (k) {
        return '<span class="spec-item"><svg class="spec-icon" aria-hidden="true"><use href="' + ICONS_URL + '#' + esc(specs[k].icon) + '"></use></svg><span class="spec-value">' + esc(specs[k].value) + '</span></span>';
      }).join("");
      var done = !!c.resource_link_id;
      var dead = c.effective_status === "invalid";
      var stateHtml, actionHtml;
      if (dead && !done) {
        // §4.4: RE0 confirmed this share dead -- say so, with its reason and
        // check time, and offer no way to spend points on it.
        stateHtml = '<span class="re0-state invalid">' + esc(c.effective_status_label || "RE0 已失效") + '</span>' +
          (c.effective_status_reason ? '<span class="re0-points">' + esc(c.effective_status_reason) + '</span>' : "") +
          (c.effective_checked_at ? '<span class="re0-points">' + esc(re0PublishedLabel(c.effective_checked_at).replace("发布于", "校验于")) + '</span>' : "");
        actionHtml = "";
      } else if (done) {
        stateHtml = dead
          ? '<span class="re0-state invalid">' + esc(c.effective_status_label || "RE0 已失效") + '</span>'
          : '<span class="re0-state done">已解锁</span>';
        actionHtml = c.action === "unavailable" ? "" :
          '<button type="button" class="link-action" data-re0-action="' + esc(c.action) + '" data-re0-id="' + esc(c.id) + '" data-link-id="' + esc(c.resource_link_id) + '">' + esc(RE0_DONE_ACTION_LABEL[c.action] || "打开") + '</button>';
      } else if (c.state === "already_unlocked" || c.is_unlocked_upstream) {
        stateHtml = '<span class="re0-state wait">RE0 已解锁 · 等待链接同步</span>';
        actionHtml = c.action === "unavailable" ? '<button type="button" class="link-action" disabled>暂不可用</button>' :
          '<button type="button" class="link-action" data-re0-action="' + esc(c.action) + '" data-re0-id="' + esc(c.id) + '">重新获取链接</button>';
      } else if (c.action === "unavailable") {
        stateHtml = '<span class="re0-state wait">网盘类型未映射</span>';
        actionHtml = '<button type="button" class="link-action" disabled>暂不可用</button>';
      } else if (c.effective_status === "checking") {
        // §4.5: an in-progress check is not a verdict -- never red, still actionable.
        stateHtml = '<span class="re0-state wait">' + esc(c.effective_status_label || "RE0 校验中") + '</span><span class="re0-points">' + esc(re0PointsLabel(c)) + '</span>';
        actionHtml = '<button type="button" class="link-action primary" data-re0-action="' + esc(c.action) + '" data-re0-id="' + esc(c.id) + '">' + esc(RE0_ACTION_LABEL[c.action] || "解锁") + '</button>';
      } else {
        stateHtml = '<span class="re0-state">RE0 待解锁</span><span class="re0-points">' + esc(re0PointsLabel(c)) + '</span>';
        actionHtml = '<button type="button" class="link-action primary" data-re0-action="' + esc(c.action) + '" data-re0-id="' + esc(c.id) + '">' + esc(RE0_ACTION_LABEL[c.action] || "解锁") + '</button>';
      }
      var publisher = (c.publisher && c.publisher.nickname) ? esc(c.publisher.nickname) : "RE0 分享者未提供";
      var published = re0PublishedLabel(c.published_at);
      var official = c.is_official ? '<span class="re0-official">官组</span>' : "";
      var sizeText = re0SizeLabel(c.size);
      var subs = (c.subtitle_language || []).concat(c.subtitle_type || []);
      var subHtml = subs.map(function (t) { return '<span class="re0-sub">' + esc(t) + '</span>'; }).join("");
      var showComposition = showRe0Composition(state.detail.media && state.detail.media.media_type);
      var compTitle = showComposition ? re0CompletionLabel(c.composition) : "";
      var compHtml = showComposition
        ? '<span class="re0-composition"' + (compTitle ? ' title="' + esc(compTitle) + '"' : "") + '>' +
          esc((c.composition && c.composition.display) || "构成未说明") + '</span>'
        : "";
      var remarkHtml = c.remark
        ? '<p class="re0-remark" aria-label="' + esc("发布者备注：" + c.remark) + '" title="' + esc(c.remark) + '">' + esc(c.remark) + '</p>'
        : "";
      return '<div class="re0-row re0-card" data-re0-row="' + esc(c.id) + '">' +
        '<div class="re0-card-head"><span class="re0-badge">RE0</span>' +
        '<span class="re0-publisher">' + publisher + '</span>' + official +
        (published ? '<span class="re0-published">' + esc(published) + '</span>' : "") +
        '<span class="re0-actions">' + stateHtml + '</span></div>' +
        '<div class="re0-title" title="' + esc(c.title || "") + '">' + esc(c.title || "未提供名称") + '</div>' +
        remarkHtml +
        '<div class="re0-chips">' + compHtml +
        chips + subHtml + (sizeText ? '<span class="re0-size">' + esc(sizeText) + '</span>' : "") + '</div>' +
        '<div class="re0-card-foot">' + re0PreviewControlHtml(c) + '<span class="re0-actions">' + actionHtml + '</span></div>' +
        '<div class="re0-preview" id="re0Preview' + esc(c.id) + '" hidden></div></div>';
    },
    // §6.2: the preview is a separate, on-demand action; a failed or
    // unavailable preview says why and never blocks the unlock button.
    re0Preview: function (candidate, btn) {
      var box = $("re0Preview" + candidate.id);
      if (box && !box.hidden && box.dataset.loaded === "1") { box.hidden = true; return Promise.resolve(); }
      if (box && box.dataset.loaded === "1") { box.hidden = false; return Promise.resolve(); }
      btn.disabled = true;
      if (box) { box.hidden = false; box.innerHTML = '<span class="re0-preview-note">正在读取文件构成…</span>'; }
      var showComposition = showRe0Composition(state.detail.media && state.detail.media.media_type);
      return api.request("/api/library/re0/candidates/" + encodeURIComponent(candidate.id) + "/file-preview").then(function (d) {
        var preview = (d && d.preview) || {};
        if (box) { box.innerHTML = views.detail.filePreviewHtml(preview, showComposition); box.dataset.loaded = "1"; }
        candidate.file_preview = { available: preview.status !== "forbidden" && preview.status !== "unsupported",
                                   status: preview.status, file_count: preview.file_count, fetched_at: preview.fetched_at };
        if (preview.composition && preview.composition.display) {
          // The parsed composition is still kept on the candidate (a series
          // re-render uses it); only a series has a chip to patch in place --
          // re-rendering the row would close the preview just opened.
          candidate.composition = preview.composition;
          if (showComposition) {
            var row = document.querySelector('[data-re0-row="' + candidate.id + '"]');
            var chip = row && row.querySelector(".re0-composition");
            if (chip) {
              chip.textContent = preview.composition.display;
              var why = re0CompletionLabel(preview.composition);
              if (why) chip.setAttribute("title", why);
            }
          }
        }
      }).catch(function (e) {
        var reason = esc((e && e.message) || "文件预览暂不可用");
        if (box) box.innerHTML = '<span class="re0-preview-note">' + reason + (showComposition ? ' · 构成未确认，以 RE0 备注为准' : "") + '</span>';
      }).then(function () { btn.disabled = false; });
    },
    // ``showComposition`` defaults to true so an older one-argument call can
    // never silently strip a series' composition.
    filePreviewHtml: function (preview, showComposition) {
      var withComposition = showComposition !== false;
      var files = preview.files || [];
      if (preview.status === "ready" || (preview.status === "invalid" && files.length)) {
        var head = '<div class="re0-preview-head">共 ' + esc(preview.file_count == null ? files.length : preview.file_count) + ' 个文件' +
          (withComposition && preview.composition && preview.composition.display ? ' · ' + esc(preview.composition.display) : "") +
          (preview.truncated ? ' · 仅显示前 ' + files.length + ' 个' : "") + '</div>';
        var rows = files.map(function (f) {
          var size = re0SizeLabel(f.size);
          return '<li><span class="re0-file-name">' + esc(f.name) + '</span>' +
            (f.path ? '<span class="re0-file-path">' + esc(f.path) + '</span>' : "") +
            (size ? '<span class="re0-file-size">' + esc(size) + '</span>' : "") + '</li>';
        }).join("");
        return head + '<ul class="re0-file-list">' + rows + '</ul>';
      }
      if (preview.status === "invalid") {
        return '<span class="re0-preview-note">RE0 校验：' + esc(preview.validate_message || "该分享已失效") + '</span>';
      }
      return '<span class="re0-preview-note">' + esc(preview.message || "文件预览暂不可用") +
        (withComposition ? ' · 以 RE0 备注为准' : "") + '</span>';
    },
    // Detail renders ask for a server-TTL-respecting ownership refresh.
    // The in-page guard expires too, so returning to the same film later
    // can notice a website unlock without a full browser reload.
    ensureRe0Fresh: function (media) {
      if (!media || !media.tmdb_id || (media.media_type !== "movie" && media.media_type !== "tv")) return Promise.resolve();
      var key = media.media_type + ":" + media.tmdb_id;
      if (state.detail.re0CheckedFor === key && Date.now() - state.detail.re0CheckedAt < 300000) return Promise.resolve();
      state.detail.re0CheckedFor = key;
      state.detail.re0CheckedAt = Date.now();
      var generation = state.detail.generation;
      var mediaRef = state.library.media;
      return api.request("/api/library/re0/refresh", { method: "POST", body: JSON.stringify({ media_type: media.media_type, tmdb_id: media.tmdb_id, if_stale: true }) })
        .then(function (d) {
          if (generation !== state.detail.generation || mediaRef !== state.library.media) return;
          // Round 40: re-read the detail whenever RE0 actually answered, not
          // only when it added something. A refresh that brought no new
          // candidate can still have taken one away -- upstream marking a
          // share invalid hides it -- and the counts beside the pan logos
          // have to follow in both directions. `if_stale` is shared and
          // budgeted server-side, not one upstream call per visitor.
          if (d && d.fetched) return views.detail.reloadRe0();
        })
        .catch(function () { /* the section simply stays as it is */ });
    },
    reloadRe0: function () {
      var generation = state.detail.generation;
      var mediaRef = state.library.media;
      var current = state.detail.media;
      var provider = state.detail.provider;
      var url = detailRequestUrl(state.library.media, state.detail.provider);
      return views.library.fetchChannel("detail-re0", url).then(function (d) {
        if (!d || !current || generation !== state.detail.generation || mediaRef !== state.library.media ||
            current !== state.detail.media || provider !== state.detail.provider) return;
        state.detail.media.re0_candidates = d.re0_candidates || [];
        state.detail.media.re0_invalid_hidden_count = d.re0_invalid_hidden_count || 0;
        state.detail.media.provider_facets = d.provider_facets || state.detail.media.provider_facets;
        if (d.provider_count != null) state.detail.media.provider_count = d.provider_count;
        // The counts beside the pan logos come from this same response, so
        // they are rebuilt here rather than left at their opening values.
        views.detail.syncProviderTabs();
        views.detail.renderGroupList();
      });
    },
    // §9.2 tv-follow packs: preview-only rows, one explicit unlock button.
    followRowHtml: function (p) {
      var meta = [p.latest_label ? "最新 " + esc(p.latest_label) : "", p.is_completed ? "已完结" : (p.is_completed === false ? "更新中" : ""),
        p.item_count != null ? esc(p.item_count) + " 条" : ""].filter(Boolean).join(" · ");
      var stateHtml, actionHtml;
      if (p.is_unlocked) {
        stateHtml = '<span class="re0-state done">已解锁' + (p.unlocked_items ? " · 已落库 " + esc(p.unlocked_items) + " 条" : "") + '</span>';
        actionHtml = "";
      } else {
        stateHtml = '<span class="re0-state">追更包待解锁</span><span class="re0-points">' + esc(p.unlock_points === 0 ? "免费解锁" : (p.unlock_points == null ? "积分以 RE0 返回为准" : "需 " + p.unlock_points + " 积分")) + '</span>';
        actionHtml = '<button type="button" class="link-action primary" data-follow-ref="' + esc(p.ref) + '">解锁追更包</button>';
      }
      return '<div class="re0-row" data-follow-row="' + esc(p.ref) + '"><span class="link-provider"><span class="link-provider-text">' + esc(p.title || "追更包") + '</span></span>' +
        '<span class="re0-points">' + meta + '</span>' + stateHtml + '<span class="re0-actions">' + actionHtml + '</span></div>';
    },
    renderFollowList: function () {
      var packs = (state.detail.media && state.detail.media.re0_follow) || [];
      var box = $("detailFollow");
      if (!box) return;
      if (!packs.length) { box.hidden = true; $("detailFollowList").innerHTML = ""; return; }
      box.hidden = false;
      $("detailFollowList").innerHTML = packs.map(views.detail.followRowHtml).join("");
      $("detailFollowList").querySelectorAll("[data-follow-ref]").forEach(function (btn) {
        btn.onclick = function () {
          var pack = packs.filter(function (p) { return p.ref === btn.dataset.followRef; })[0];
          if (pack) views.detail.followUnlock(pack, btn);
        };
      });
    },
    followUnlock: function (pack, btn) {
      var points = pack.unlock_points === 0 ? "免费解锁" : (pack.unlock_points == null ? "积分以 RE0 返回为准" : "需 " + pack.unlock_points + " 积分");
      if (!window.confirm("解锁追更包「" + (pack.title || "") + "」？" + points + "。解锁后会把已发布的剧集链接加密保存到本地。")) return;
      var subscribe = false;
      if (state.detail.media && state.detail.media.re0_follow_can_subscribe) {
        subscribe = window.confirm("同时接收该追更包的更新通知？（取消则只解锁，不订阅）");
      }
      btn.disabled = true;
      var requestId = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : "req-" + Date.now() + "-" + Math.random().toString(16).slice(2, 10);
      api.request("/api/library/re0-follow/" + encodeURIComponent(pack.ref) + "/unlock", {
        method: "POST", body: JSON.stringify({ request_id: requestId, subscribe_updates: subscribe }),
      }).then(function (d) {
        toast(d.already_owned ? "追更包已拥有，已同步链接 " + (d.materialized || 0) + " 条" : "追更包已解锁，已落库 " + (d.materialized || 0) + " 条", "success");
        return views.detail.selectProvider(state.detail.provider);
      }).catch(function (e) {
        var message = e.message || "解锁追更包失败";
        if (e.retry_after) message += "（" + e.retry_after + " 秒后再试）";
        toast(message, "error");
        btn.disabled = false;
      });
    },
    // Explicit single-media refresh (spec §10.4.3): bypasses the resource
    // TTL, still budgeted and throttled server-side; never unlocks.
    re0Refresh: function (btn) {
      var media = state.detail.media || {};
      var tmdbId = media.tmdb_id;
      if (!tmdbId) { toast("该媒体没有 TMDB ID，无法刷新 RE0", "error"); return; }
      btn.disabled = true;
      api.request("/api/library/re0/refresh", { method: "POST", body: JSON.stringify({ media_type: media.media_type, tmdb_id: tmdbId }) }).then(function (d) {
        toast(d.message || "已刷新", "success");
        return views.detail.selectProvider(state.detail.provider);
      }).catch(function (e) {
        toast(e.message || "刷新失败", "error");
      }).then(function () { btn.disabled = false; });
    },
    bindRe0Actions: function (root) {
      root.querySelectorAll("[data-re0-preview]").forEach(function (btn) {
        btn.onclick = function () {
          var id = btn.dataset.re0Preview;
          var candidate = ((state.detail.media && state.detail.media.re0_candidates) || []).filter(function (c) { return String(c.id) === id; })[0];
          if (candidate) views.detail.re0Preview(candidate, btn);
        };
      });
      root.querySelectorAll("[data-re0-action]").forEach(function (btn) {
        if (btn.disabled) return;
        btn.onclick = function () {
          var id = btn.dataset.re0Id;
          var candidate = ((state.detail.media && state.detail.media.re0_candidates) || []).filter(function (c) { return String(c.id) === id; })[0];
          if (candidate) views.detail.re0Action(candidate, btn);
        };
      });
    },
    // The only place that asks the backend to unlock (spec §8): explicit
    // click, a confirm showing the points, one idempotent request_id; then
    // the existing transfer / reveal-copy / cloud-download flow runs on the
    // materialised local link. An already materialised candidate skips
    // straight to that flow -- never a second unlock.
    re0Action: function (candidate, btn) {
      var titleEl = $("detailContent").querySelector(".detail-title");
      var title = titleEl ? titleEl.textContent : "";
      var label = (candidate.provider_label || candidate.provider || "") + " · RE0";
      // R04: what happens after an unlock is a separate step with its own
      // failure. An unlock that succeeded stays succeeded -- telling the
      // user "RE0 解锁失败" here would invite them to pay again for
      // something they already own.
      function dispatch(linkId) {
        try {
          if (candidate.action === "transfer") views.transfer.openLibrary(linkId, title);
          else if (candidate.action === "cloud") views.transfer.openCloud([linkId], title, [label]);
          else views.detail.reveal(linkId, "copy", btn);
          return true;
        } catch (err) {
          toast("资源已解锁并保存，但这一步没能继续：请在资源列表中重试，不需要再次解锁。", "error");
          return false;
        }
      }
      if (candidate.resource_link_id) { dispatch(candidate.resource_link_id); return; }
      var question = (candidate.state === "already_unlocked" || candidate.is_unlocked_upstream)
        ? "RE0 已标记拥有此资源，是否请求获取链接并" + (RE0_DONE_ACTION_LABEL[candidate.action] || "继续") + "？最终以 RE0 返回为准。"
        : "解锁这条 RE0 资源？" + re0PointsLabel(candidate) + "。解锁后将" + (RE0_DONE_ACTION_LABEL[candidate.action] || "继续") + "。";
      if (!window.confirm(question)) return;
      btn.disabled = true;
      var requestId = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : "req-" + Date.now() + "-" + Math.random().toString(16).slice(2, 10);
      api.request("/api/library/re0-resource/" + encodeURIComponent(candidate.id) + "/unlock-and-action", {
        method: "POST", body: JSON.stringify({ action: candidate.action, request_id: requestId }),
      }).then(function (d) {
        toast(d.already_owned ? "RE0 已拥有该资源，未重复扣分" : "RE0 解锁成功，链接已加密保存", "success");
        if (d.media_id && String(state.library.media) !== String(d.media_id)) views.library.openDetail(d.media_id);
        else views.detail.selectProvider(state.detail.provider);
        dispatch(d.link_public_id);
      }).catch(function (e) {
        var message = e.message || "RE0 解锁失败";
        if (e.retry_after) message += "（" + e.retry_after + " 秒后再试）";
        toast(message, "error");
        btn.disabled = false;
      });
    },
    bindCloudGroupButtons: function (root) {
      root.querySelectorAll(".group-cloud-btn").forEach(function (btn) {
        btn.onclick = function () {
          var ids = btn.dataset.linkIds.split(",");
          var group = (state.detail.media.groups || []).filter(function (g) { return String(g.group_id) === btn.dataset.groupId; })[0];
          var labels = ids.map(function (id) {
            var link = ((group && group.links) || []).filter(function (l) { return l.link_id === id; })[0];
            return link ? (link.label || "") : "";
          });
          var titleEl = $("detailContent").querySelector(".detail-title");
          views.transfer.openCloud(ids, titleEl ? titleEl.textContent : "", labels);
        };
      });
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
      // Round 32: narrow the cards to this pan, keep every tab button on
      // screen, and issue no request -- refetching with ?provider= is what
      // used to delete the other tabs. Server-side isolation is unchanged for
      // a genuinely provider-scoped fetch (a deep link or the search filter).
      var normalized = code || "";
      state.detail.panFilter = normalized;
      views.detail.renderGroupList();
      var box = $("libraryProviderTabs");
      if (box) {
        box.querySelectorAll(".provider-tab").forEach(function (tab) {
          var on = (tab.dataset.provider || "") === normalized;
          tab.setAttribute("aria-selected", on ? "true" : "false");
          tab.setAttribute("tabindex", on ? "0" : "-1");
        });
      }
      views.detail.focusProviderTab(normalized);
      return Promise.resolve();
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
      // Round 25: each tab counts the rows its card actually lists.
      var facets = visibleFacets(media);
      var codes = facets.map(function (f) { return f.provider; });
      var requested = (state.library.provider && state.library.provider[0]) || "";
      var selected = codes.indexOf(requested) !== -1 ? requested : (codes.length === 1 ? codes[0] : "");
      state.detail.provider = selected;
      state.library.provider = selected ? [selected] : [];
      // Remembered so a later syncProviderTabs() can rebuild the same
      // tablist: whether 全部 belongs there is not inferable from `facets`.
      state.detail.scopedFetch = !!scopedFetch;
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
        calendarCtaHtml(media.re0_calendar) +
        '<p class="detail-overview" id="detailOverview">' + esc(media.overview || "暂无简介") + '</p>' +
        '<button type="button" id="detailOverviewToggle" class="tertiary">展开简介</button>' +
        '<div class="detail-summary-row" id="detailSummaryRow">' + views.detail.summaryRowHtml(media, facets.length) + '</div>' +
        '<button type="button" class="detail-cta">查看资源</button>' +
        '</div>' +
        '</div>' +
        providerSwitcher +
        '<section id="libraryProviderPanel" class="provider-panel" role="tabpanel" aria-label="资源列表" tabindex="0">' +
        '<div class="detail-groups"><h3>资源</h3><div id="detailGroupList" tabindex="-1"></div></div>' +
        '<div class="detail-re0" id="detailFollow" hidden><div class="detail-re0-head"><h3>追更包</h3></div><p class="detail-re0-note">未解锁前只显示季集标签与状态；解锁只在你点击后进行，默认不订阅通知。</p><div id="detailFollowList"></div></div>' +
        '</section>';
      views.library.pushUrl(false);
      views.detail.bind();
      views.detail.renderGroupList();
      views.detail.renderFollowList();
      views.detail.ensureRe0Fresh(media);
      var cta = $("detailContent").querySelector(".calendar-cta");
      if (cta) cta.onclick = function () { $("detailGroupList").scrollIntoView({ block: "start", behavior: "smooth" }); };
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
          if (action === "cloud") {
            var cloudTitleEl = $("detailContent").querySelector(".detail-title");
            views.transfer.openCloud([linkId], cloudTitleEl ? cloudTitleEl.textContent : "", [btn.dataset.linkLabel || ""]);
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
      state.detail.generation = (state.detail.generation || 0) + 1;
      views.detail.hideReveal();
      state.detail.folds = {};
      state.detail.panFilter = "";
      state.detail.includeInvalid = false;
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

  // 115 云下载 tab (spec §6): quota card + the account's task list from
  // 115, refreshed every 15s only while the tab is on screen.
  views.cloud = {
    page: 1,
    timer: null,
    init: function () {
      $("cloudRefresh").onclick = function () { views.cloud.load(true); };
      $("cloudClearFailed").onclick = function () { views.cloud.clear("failed", $("cloudClearFailed")); };
      $("cloudClearCompleted").onclick = function () { views.cloud.clear("completed", $("cloudClearCompleted")); };
      $("cloudPrev").onclick = function () { if (views.cloud.page > 1) { views.cloud.page -= 1; views.cloud.load(true); } };
      $("cloudNext").onclick = function () { views.cloud.page += 1; views.cloud.load(true); };
      document.addEventListener("visibilitychange", function () {
        if (!$("cloud").hidden) { if (document.visibilityState === "visible") views.cloud.startPolling(); else views.cloud.stopPolling(); }
      });
    },
    enter: function () {
      views.cloud.load();
      views.cloud.startPolling();
    },
    leave: function () {
      views.cloud.stopPolling();
    },
    startPolling: function () {
      views.cloud.stopPolling();
      if (document.visibilityState !== "visible") return;
      views.cloud.timer = setInterval(function () { views.cloud.load(); }, CLOUD_POLL_INTERVAL_MS);
    },
    stopPolling: function () {
      if (views.cloud.timer) { clearInterval(views.cloud.timer); views.cloud.timer = null; }
    },
    load: function (force) {
      var suffix = force ? "&refresh=1" : "";
      var quota = api.request("/api/library/cloud-download/quota?_=" + views.cloud.page + suffix).then(function (q) {
        views.cloud.renderQuota(q);
      }).catch(function () { views.cloud.renderQuota(null); });
      var tasks = api.request("/api/library/cloud-download/tasks?page=" + views.cloud.page + suffix).then(function (d) {
        views.cloud.renderTasks(d);
      }).catch(function (e) {
        setEmpty("cloudTasks", e.message || "任务读取失败");
      });
      return Promise.all([quota, tasks]);
    },
    renderQuota: function (q) {
      $("cloudQuotaSummary").textContent = cloudQuotaSummaryText(q);
      var packages = (q && q.package) || [];
      $("cloudQuotaPackages").innerHTML = packages.map(function (p) {
        return '<div class="quota-item"><b>' + esc(p.name || "配额") + '</b>剩余 ' + esc(p.surplus != null ? p.surplus : "?") + ' / ' + esc(p.count != null ? p.count : "?") + '</div>';
      }).join("");
    },
    taskRowHtml: function (t) {
      var origin = t.origin ? esc(t.origin.media_title || "") + " · " + esc(t.origin.link_label || "") : "—";
      var percent = t.percent != null ? esc(t.percent) + "%" : "—";
      return '<tr data-info-hash="' + esc(t.info_hash) + '">' +
        '<td class="cloud-name" title="' + esc(t.name) + '">' + esc(t.name) + '</td>' +
        '<td>' + origin + '</td>' +
        '<td class="cloud-num">' + esc(cloudFormatSize(t.size)) + '</td>' +
        '<td class="cloud-num">' + percent + '</td>' +
        '<td><span class="cloud-status cloud-status-' + cloudStatusClass(t.status) + '">' + esc(t.status_label || "未知") + '</span></td>' +
        '<td class="cloud-num">' + esc(t.add_time ? new Date(t.add_time).toLocaleString() : "—") + '</td>' +
        '<td class="cloud-actions">' +
        '<button type="button" class="tertiary" data-cloud-delete="' + esc(t.info_hash) + '">删除</button>' +
        '<button type="button" class="tertiary" data-cloud-delete-files="' + esc(t.info_hash) + '">删除并删文件</button>' +
        '</td></tr>';
    },
    renderTasks: function (d) {
      var tasks = (d && d.tasks) || [];
      var box = $("cloudTasks");
      if (!tasks.length) {
        setEmpty("cloudTasks", "115 账号里目前没有云下载任务。");
      } else {
        box.innerHTML = '<table class="cloud-table"><thead><tr><th>任务</th><th>来自</th><th>大小</th><th>进度</th><th>状态</th><th>添加时间</th><th>操作</th></tr></thead><tbody>' +
          tasks.map(views.cloud.taskRowHtml).join("") + '</tbody></table>';
        box.querySelectorAll("[data-cloud-delete]").forEach(function (btn) {
          btn.onclick = function () { views.cloud.remove(btn.dataset.cloudDelete, false, btn); };
        });
        box.querySelectorAll("[data-cloud-delete-files]").forEach(function (btn) {
          btn.onclick = function () { views.cloud.remove(btn.dataset.cloudDeleteFiles, true, btn); };
        });
      }
      var pageCount = (d && d.page_count) || 1;
      var page = (d && d.page) || views.cloud.page;
      views.cloud.page = page;
      $("cloudPager").hidden = pageCount <= 1;
      $("cloudPageText").textContent = "第 " + page + " / " + pageCount + " 页";
      $("cloudPrev").disabled = page <= 1;
      $("cloudNext").disabled = page >= pageCount;
    },
    remove: function (infoHash, deleteFiles, btn) {
      var row = btn.closest("tr");
      var name = row ? row.querySelector(".cloud-name").textContent : "";
      var question = deleteFiles ? "删除云下载任务「" + name + "」并删除已下载的文件？此操作不可撤销。" : "删除云下载任务「" + name + "」？已下载的文件会保留。";
      if (!window.confirm(question)) return;
      btn.disabled = true;
      api.request("/api/library/cloud-download/tasks/" + encodeURIComponent(infoHash) + "/delete", {
        method: "POST", body: JSON.stringify({ delete_files: deleteFiles }),
      }).then(function (d) {
        feedback("cloudTabResult", d.message || "任务已删除", "success");
        return views.cloud.load(true);
      }).catch(function (e) {
        feedback("cloudTabResult", "删除失败：" + e.message, "error");
        btn.disabled = false;
      });
    },
    clear: function (scope, btn) {
      var question = scope === "failed" ? "清理 115 账号里所有失败的云下载任务？" : "清理 115 账号里所有已完成的云下载任务记录？已下载的文件会保留。";
      if (!window.confirm(question)) return;
      btn.disabled = true;
      api.request("/api/library/cloud-download/tasks/clear", { method: "POST", body: JSON.stringify({ scope: scope }) }).then(function (d) {
        feedback("cloudTabResult", d.message || "已清理", "success");
        return views.cloud.load(true);
      }).catch(function (e) {
        feedback("cloudTabResult", "清理失败：" + e.message, "error");
      }).then(function () { btn.disabled = false; });
    }
  };

  // Panels stay mounted so navigating never discards an unfinished form.
  views.settingsNav = {
    drafts: {},
    revisions: {},
    flags: { legacy115: "legacy115Dirty", tmdb: "tmdbSettingsDirty", linkcheck: "linkcheckDirty", re0: "re0SettingsDirty", cloud: "cloudDirty", policy: "policyDirty" },
    changed: function (group) {
      this.drafts[group] = true;
      this.revisions[group] = (this.revisions[group] || 0) + 1;
      state[this.flags[group]] = true;
      this.notice();
    },
    saved: function (group, revision) {
      if ((this.revisions[group] || 0) !== revision) return false;
      delete this.drafts[group];
      state[this.flags[group]] = false;
      this.notice();
      return true;
    },
    notice: function () {
      var el = $("settingsDraftNotice");
      if (el) el.hidden = !Object.keys(this.drafts).length;
    },
    select: function (kind, key, focus) {
      var navAttribute = kind === "service" ? "data-settings-service" : "data-settings-section";
      var panelAttribute = kind === "service" ? "data-service-panel" : "data-settings-panel";
      var buttons = Array.from(document.querySelectorAll("[" + navAttribute + "]"));
      var active = buttons.filter(function (btn) { return btn.getAttribute(navAttribute) === key; })[0];
      if (!active) return;
      buttons.forEach(function (btn) {
        if (btn === active) btn.setAttribute("aria-current", "page");
        else btn.removeAttribute("aria-current");
      });
      document.querySelectorAll("[" + panelAttribute + "]").forEach(function (panel) {
        panel.hidden = panel.getAttribute(panelAttribute) !== key;
      });
      if (focus) active.focus();
      this.notice();
    },
    init: function () {
      ["section", "service"].forEach(function (kind) {
        var attribute = kind === "service" ? "data-settings-service" : "data-settings-section";
        var buttons = Array.from(document.querySelectorAll("[" + attribute + "]"));
        buttons.forEach(function (btn, index) {
          btn.onclick = function () { views.settingsNav.select(kind, btn.getAttribute(attribute)); };
          btn.onkeydown = function (event) {
            var next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
              : event.key === "ArrowDown" || event.key === "ArrowRight" ? (index + 1) % buttons.length
              : event.key === "ArrowUp" || event.key === "ArrowLeft" ? (index - 1 + buttons.length) % buttons.length : -1;
            if (next < 0) return;
            event.preventDefault();
            views.settingsNav.select(kind, buttons[next].getAttribute(attribute), true);
          };
        });
      });
      document.querySelectorAll("[data-settings-form]").forEach(function (form) {
        function changed() { views.settingsNav.changed(form.dataset.settingsForm); }
        form.addEventListener("input", changed);
        form.addEventListener("change", changed);
      });
      // Existing feedback helpers retain their IDs while all settings save
      // results announce themselves to assistive technology.
      if ($("settings")) $("settings").querySelectorAll(".feedback").forEach(function (el) {
        el.setAttribute("role", "status");
        el.setAttribute("aria-live", "polite");
      });
    }
  };

  views.settings = {
    validate: function (group, resultId) {
      var form = document.querySelector('[data-settings-form="' + group + '"]');
      if (!form) return true;
      var invalid = Array.from(form.querySelectorAll("input")).filter(function (input) { return !input.checkValidity(); })[0];
      if (!invalid) return true;
      invalid.reportValidity();
      feedback(resultId, "请检查输入值，按字段提示填写后再保存。", "error");
      return false;
    },
    init: function () {
      // The public status response never echoes the legacy destination PID.
      // Track this field separately: editing a Cookie must not clear it.
      ["input", "change"].forEach(function (eventName) {
        $("settingPid").addEventListener(eventName, function () {
          state.targetPidDirty = true;
          state.targetPidRevision = (state.targetPidRevision || 0) + 1;
        });
      });
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
      // Each save owns only its section's fields and dirty state.
      $("linkcheckSave").onclick = function () {
        if (!views.settings.validate("linkcheck", "linkcheckResult")) return;
        var revision = views.settingsNav.revisions.linkcheck || 0;
        $("linkcheckSave").disabled = true;
        feedback("linkcheckResult", "正在保存检测设置…");
        api.request("/api/settings", { method: "POST", body: JSON.stringify(linkcheckSavePayload()) }).then(function () {
          views.settingsNav.saved("linkcheck", revision);
          return refreshLinkcheckStatus().then(function () {
            feedback("linkcheckResult", "检测设置已保存。" + $("linkcheckHeartbeat").textContent.split(" · ")[0], "success");
          });
        }).catch(function (e) {
          feedback("linkcheckResult", "保存失败：" + e.message, "error");
        }).then(function () { $("linkcheckSave").disabled = false; });
      };
      ["cloudEnabled", "cloudDailyCap", "cloudPerSubmitCap"].forEach(function (id) {
        $(id).addEventListener("change", function () { state.cloudDirty = true; });
      });
      $("re0SyncRefresh").onclick = function () {
        $("re0SyncRefresh").disabled = true;
        refreshRe0SyncStatus().then(function () { $("re0SyncRefresh").disabled = false; });
      };
      ["re0DailyCap", "re0MinInterval"].forEach(function (id) {
        $(id).addEventListener("change", function () { state.re0SettingsDirty = true; });
      });
      $("re0SyncSave").onclick = function () {
        if (!views.settings.validate("re0", "re0SyncResult")) return;
        var revision = views.settingsNav.revisions.re0 || 0;
        $("re0SyncSave").disabled = true;
        feedback("re0SyncResult", "正在保存 RE0 设置…");
        api.request("/api/settings", { method: "POST", body: JSON.stringify(re0SettingsPayload()) }).then(function () {
          views.settingsNav.saved("re0", revision);
          return refreshRe0SyncStatus().then(function () { feedback("re0SyncResult", "RE0 设置已保存。", "success"); });
        }).catch(function (e) {
          feedback("re0SyncResult", "保存失败：" + e.message, "error");
        }).then(function () { $("re0SyncSave").disabled = false; });
      };
      $("re0RunSmall").onclick = function () {
        $("re0RunSmall").disabled = true;
        feedback("re0SyncResult", "正在只读探测 1 条…");
        api.request("/api/library/re0/run-small", { method: "POST", body: JSON.stringify({}) }).then(function (d) {
          var r = d.report || {};
          feedback("re0SyncResult", (d.message || "已探测") + "：请求 " + (r.requested || 0) + " · 成功 " + (r.succeeded || 0) + " · 新候选 " + (r.candidates_new || 0), d.success ? "success" : "warn");
          return refreshRe0SyncStatus();
        }).catch(function (e) {
          feedback("re0SyncResult", "探测失败：" + e.message, "error");
        }).then(function () { $("re0RunSmall").disabled = false; });
      };
      $("cloudSave").onclick = function () {
        if (!views.settings.validate("cloud", "cloudResult")) return;
        var revision = views.settingsNav.revisions.cloud || 0;
        $("cloudSave").disabled = true;
        feedback("cloudResult", "正在保存云下载设置…");
        api.request("/api/settings", { method: "POST", body: JSON.stringify(cloudSavePayload()) }).then(function () {
          views.settingsNav.saved("cloud", revision);
          return refreshCloudStatus().then(function () {
            feedback("cloudResult", "云下载设置已保存。" + $("cloudStatusLine").textContent, "success");
          });
        }).catch(function (e) {
          feedback("cloudResult", "保存失败：" + e.message, "error");
        }).then(function () { $("cloudSave").disabled = false; });
      };
      $("settingsSave").onclick = function () {
        var revision = views.settingsNav.revisions.legacy115 || 0;
        var originalCookie = $("cookie115").value;
        var pidRevision = state.targetPidRevision || 0;
        var payload = {};
        if (state.targetPidDirty) payload["115_target_pid"] = $("settingPid").value.trim();
        var cookie = $("cookie115").value.trim();
        if (cookie) payload["115_cookie"] = cookie;
        $("settingsSave").disabled = true;
        feedback("settingsResult", "正在保存 115 配置…");
        return api.request("/api/settings", { method: "POST", body: JSON.stringify(payload) }).then(function (d) {
          if ($("cookie115").value === originalCookie) $("cookie115").value = "";
          if ((state.targetPidRevision || 0) === pidRevision) state.targetPidDirty = false;
          views.settingsNav.saved("legacy115", revision);
          if (d.cookie_check) {
            feedback("settingsResult", d.cookie_check.valid ? "115 配置已保存，Cookie 可用。" : "115 配置已保存，但 Cookie 不可用，请重新扫码。", d.cookie_check.valid ? "success" : "error");
          } else {
            feedback("settingsResult", "115 配置已保存。", "success");
          }
          return refreshStatus().then(function () { return views.my115.load(); });
        }).catch(function (e) { feedback("settingsResult", "保存失败：" + e.message, "error"); })
          .then(function () { $("settingsSave").disabled = false; });
      };
      $("tmdbSave").onclick = function () {
        if (!views.settings.validate("tmdb", "tmdbResult")) return;
        var revision = views.settingsNav.revisions.tmdb || 0;
        var originalKey = $("tmdbKey").value;
        var payload = {};
        var tmdbKey = $("tmdbKey").value.trim();
        if (tmdbKey) payload["tmdb_api_key"] = tmdbKey;
        var budget = $("tmdbBudget").value.trim();
        if (budget) payload["tmdb_daily_budget"] = parseInt(budget, 10);
        if (state.tmdbEnrichDirty) payload["tmdb_enrich_enabled"] = $("tmdbEnrichEnabled").checked;
        $("tmdbSave").disabled = true;
        feedback("tmdbResult", "正在保存 TMDB 设置…");
        return api.request("/api/settings", { method: "POST", body: JSON.stringify(payload) }).then(function (d) {
          if ($("tmdbKey").value === originalKey) $("tmdbKey").value = "";
          if (views.settingsNav.saved("tmdb", revision)) {
            state.tmdbEnrichDirty = false;
            if (d.tmdb && d.tmdb.configured_budget != null) $("tmdbBudget").value = d.tmdb.configured_budget;
          }
          feedback("tmdbResult", "TMDB 设置已保存。", "success");
          return refreshLibraryStatus();
        }).catch(function (e) { feedback("tmdbResult", "保存失败：" + e.message, "error"); })
          .then(function () { $("tmdbSave").disabled = false; });
      };
      $("settingsStatusRefresh").onclick = function () {
        var btn = $("settingsStatusRefresh");
        btn.disabled = true;
        return Promise.all([refreshStatus(), refreshLibraryStatus()]).catch(function (e) {
          $("settingsStatusUpdated").textContent = "刷新失败：" + e.message;
        }).then(function () { btn.disabled = false; });
      };
      // RE0 OAuth is intentionally a top-level navigation rather than a
      // popup: the provider can fall back to redirect when window.opener is
      // unavailable, and the Access session is preserved by the browser.
      $("re0OAuthBtn").onclick = function () {
        var btn = $("re0OAuthBtn");
        btn.disabled = true;
        feedback("re0OAuthResult", "正在生成 RE0 授权链接…");
        api.request("/api/hdhive/oauth/start", { method: "POST", body: JSON.stringify({}) }).then(function (d) {
          if (!d.url) throw new Error("RE0 未返回授权地址");
          window.location.assign(d.url);
        }).catch(function (e) {
          feedback("re0OAuthResult", e.message, "error");
          btn.disabled = false;
        });
      };
      $("re0OAuthRefresh").onclick = function () {
        var btn = $("re0OAuthRefresh");
        btn.disabled = true;
        refreshStatus().then(function () {
          feedback("re0OAuthResult", "RE0 授权状态已刷新。", "success");
        }).catch(function (e) {
          feedback("re0OAuthResult", e.message, "error");
        }).then(function () { btn.disabled = false; });
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

  var RE0_SCOPE_LABEL = { meta: "连通性", query: "资源查询", unlock: "资源解锁", write: "资源管理" };
  function formatRe0Scopes(scope) {
    var values = Array.isArray(scope) ? scope : String(scope || "").trim().split(/\s+/);
    var labels = values.filter(function (value) { return RE0_SCOPE_LABEL[value]; }).map(function (value) { return RE0_SCOPE_LABEL[value]; });
    return labels.filter(function (value, index) { return labels.indexOf(value) === index; }).join("、") || "已授权权限";
  }

  function renderRe0OAuthStatus(h) {
    var box = $("re0OAuthStatus");
    var btn = $("re0OAuthBtn");
    if (!box || !btn) return;
    var configured = !!(h && h.client_id_configured && h.app_secret_configured);
    btn.disabled = !configured;
    if (!configured) {
      box.textContent = "RE0 应用尚未完整配置，暂时无法发起授权。";
      box.className = "feedback warn";
      btn.textContent = "暂不可授权";
      return;
    }
    btn.textContent = h.authorized ? "重新授权 RE0" : "授权 RE0";
    if (!h.authorized) {
      box.textContent = "尚未授权。完成授权后才能查询资源、签到与解锁。";
      box.className = "feedback warn";
      return;
    }
    var text = "已授权 · " + formatRe0Scopes(h.scope);
    if (h.expires_at) {
      var expires = new Date(h.expires_at);
      if (!isNaN(expires.getTime())) text += " · Access Token 有效期至 " + expires.toLocaleString();
    }
    box.textContent = text;
    box.className = "feedback success";
  }

  function authModeLabel(mode) {
    return { access: "Cloudflare Access", hybrid: "Cloudflare Access + 账户登录", app: "账户登录", account: "账户登录", local: "本地测试模式", disabled: "身份验证已关闭" }[mode] || "登录方式未提供";
  }

  function renderStatus(s) {
    var n = s["115"] || {};
    var o = s.openlist || {};
    var t = s.strm || {};
    var h = s.hdhive || {};
    var cookieLabel = !n.cookie_configured ? "未配置" : n.cookie_valid === true ? "可用" : n.cookie_valid === false ? "不可用" : "待检查";
    var checkin = h.checkin || {};
    var lastSuccess = checkin.last_success;
    var lastAt = checkin.last_at;
    var lastTaskLabel = lastSuccess === null || lastSuccess === undefined
      ? "暂无记录"
      : "最近自动任务：" + (lastSuccess ? "成功" : "失败") + " · " + (lastAt || "");
    state.statusRowsBase = [
      [s.auth_mode === "local" || s.auth_mode === "disabled" ? "warn" : "ok", "站点登录方式", authModeLabel(s.auth_mode)],
      [n.cookie_valid === true ? "ok" : n.cookie_configured ? "bad" : "off", "115 Cookie（转存）", cookieLabel],
      [n.open_platform_configured ? "ok" : "off", "115 开放平台令牌", n.open_platform_configured ? "已同步" : "未同步"],
      [h.authorized ? "ok" : "off", "RE0 授权", (h.authorized ? "已授权" : "未授权") + " · " + lastTaskLabel],
      [o.token_configured ? "ok" : "off", "OpenList API Token", o.token_configured ? "已配置" : "未配置"],
      [t.exists ? "ok" : "bad", "Infuse STRM 目录", t.exists ? "可读取" : "不可读取"]
    ];
    renderStatusList();
    renderCookie115Status(n);
    renderRe0OAuthStatus(h);
    state.authMode = s.auth_mode;
    views.account.render();
    if ($("settingsStatusUpdated")) $("settingsStatusUpdated").textContent = "状态读取时间：" + new Date().toLocaleString();
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
    var summary = [
      "状态：" + esc(stateLine),
      "今日：" + esc(budgetLine),
      "进度：" + esc(progressLine),
      "最近错误：" + esc(lastErrorLine)
    ];
    var details = [
      "待复核：" + esc(reviewLine),
      "今日 429：" + esc(rate429Line),
      "最近一轮：" + esc(lastRoundLine)
    ];
    var box = $("tmdbScrapeStatus");
    var previousDetails = box.querySelector("details");
    var open = previousDetails && previousDetails.open;
    box.innerHTML = summary.map(function (line) { return "<p>" + line + "</p>"; }).join("") +
      '<details class="settings-details"' + (open ? " open" : "") + '><summary>详细诊断</summary>' +
      details.map(function (line) { return "<p>" + line + "</p>"; }).join("") + '</details>';
  }

  function refreshLibraryStatus() {
    var version = state.tmdbStatusVersion = (state.tmdbStatusVersion || 0) + 1;
    return api.request("/api/library/tmdb-status").then(function (d) {
      if (version !== state.tmdbStatusVersion) return;
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
      if (d.configured_budget != null && !state.tmdbSettingsDirty) $("tmdbBudget").value = d.configured_budget;
      // I4: seed the checkbox from server state so a save that doesn't
      // touch it (e.g. only changing the 115 target pid) round-trips the
      // existing setting instead of silently sending "unchecked".
      if (!state.tmdbEnrichDirty && !state.tmdbSettingsDirty) $("tmdbEnrichEnabled").checked = !!d.enrich_enabled;
    }).catch(function (e) {
      if (version !== state.tmdbStatusVersion) return;
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
  function renderRe0SyncStatus(d) {
    var line = $("re0SyncStatus");
    line.textContent = !d ? "无法读取 RE0 同步状态" : d.authorized ? "已授权" : d.configured ? "未授权" : "未配置";
    line.className = "feedback " + (d && d.authorized ? "success" : "warn");
    var metrics = $("re0SyncMetrics");
    if (metrics) {
      var budget = (d && d.budget) || {};
      var projections = (d && d.projections) || {};
      var pending = (projections.pending || 0) + (projections.partial || 0) + (projections.retryable || 0);
      metrics.innerHTML = d ? [
        ["今日请求", (budget.used_today || 0) + " / " + (budget.daily_cap || 0)],
        ["候选资源", (d.resources || {}).candidate || 0], ["待补全", pending]
      ].map(function (metric) { return '<div class="settings-metric">' + esc(metric[0]) + '<strong>' + esc(metric[1]) + '</strong></div>'; }).join("") : "";
    }
    if ($("re0SyncDetails")) $("re0SyncDetails").textContent = re0SyncSummary(d);
    if (d && !state.re0SettingsDirty) {
      if (d.daily_cap != null) $("re0DailyCap").value = String(d.daily_cap);
      if (d.min_interval_ms != null) $("re0MinInterval").value = String(d.min_interval_ms);
    }
  }
  function refreshRe0SyncStatus() {
    var version = state.re0StatusVersion = (state.re0StatusVersion || 0) + 1;
    return api.request("/api/library/re0/status").then(function (d) {
      if (version === state.re0StatusVersion) renderRe0SyncStatus(d);
    }).catch(function () { if (version === state.re0StatusVersion) renderRe0SyncStatus(null); });
  }
  function renderCloudStatus(d) {
    // F01: two audiences in one payload. The switch and the per-submit cap
    // are page-wide state every user needs (they decide whether a 云下载
    // button is drawn at all); the three inputs and the status line belong
    // to the administrator's settings card, which a member's page does not
    // contain. Writing them unconditionally threw on a member's page.
    if (d && !state.cloudDirty && $("cloudEnabled")) {
      $("cloudEnabled").checked = !!d.enabled;
      $("cloudDailyCap").value = d.daily_cap != null ? String(d.daily_cap) : "";
      $("cloudPerSubmitCap").value = d.per_submit_cap != null ? String(d.per_submit_cap) : "";
    }
    var wasEnabled = state.cloudEnabled;
    state.cloudEnabled = !!(d && d.enabled);
    state.cloudPerSubmitCap = d && d.per_submit_cap ? d.per_submit_cap : 30;
    if (wasEnabled !== state.cloudEnabled && state.library.media && state.detail && state.detail.media) views.detail.renderGroupList();
    var line = $("cloudStatusLine");
    if (!line) return;
    line.textContent = cloudStatusSummary(d);
    line.hidden = false;
  }
  function refreshCloudStatus() {
    var version = state.cloudStatusVersion = (state.cloudStatusVersion || 0) + 1;
    return api.request("/api/library/cloud-download/status").then(function (d) {
      if (version === state.cloudStatusVersion) renderCloudStatus(d);
    }).catch(function () { if (version === state.cloudStatusVersion) renderCloudStatus(null); });
  }
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
    var version = state.linkcheckStatusVersion = (state.linkcheckStatusVersion || 0) + 1;
    return api.request("/api/library/linkcheck-status").then(function (d) {
      if (version === state.linkcheckStatusVersion) renderLinkcheckStatus(d);
    }).catch(function () {
      if (version === state.linkcheckStatusVersion) renderLinkcheckStatus(null);
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
      if (document.visibilityState === "visible") views.library.maybeRefreshHero();
    });
    window.addEventListener("focus", function () { views.library.maybeRefreshHero(); });
    setInterval(function () { views.library.maybeRefreshHero(); }, HERO_REFRESH_INTERVAL_MS);
    setInterval(function () { views.library.advanceHero(1, false); }, 1000);
    return api.refreshCsrf().then(function () {
      return views.account.load();
    }).then(function () {
      // /api/status is the deployment's own state -- 115, OpenList, RE0
      // sync, the checker. A member is refused it by the server, so the
      // shell does not ask: it would be one guaranteed 403 per page load.
      if (!capability("global_settings")) {
        // F01: /api/status is the administrator's, but the cloud-download
        // switch and this user's own authorisation are not -- without this
        // a member with step B never learns cloud download is available, so
        // the buttons that open the dialog are never drawn at all.
        if (capability("own_115_cloud_download")) refreshCloudStatus();
        return null;
      }
      return api.request("/api/status?verify_115=1");
    }).then(function (s) {
      if (!s) return;
      renderStatus(s);
      if (new URLSearchParams(window.location.search).get("oauth") === "success") {
        feedback("re0OAuthResult", "RE0 授权成功，Token 已加密保存。", "success");
        var cleanUrl = new URL(window.location.href);
        cleanUrl.searchParams.delete("oauth");
        window.history.replaceState(window.history.state, "", cleanUrl);
      }
      state.openPaths = Object.assign(state.openPaths, s.openlist.paths || {});
      state.transferRoot = state.openPaths["115pan"] || "/115pan";
      // Legacy OpenList paths are resolved by the server, not diagnostics.
      // The separate personal 115 folder flow still uses its own proven CID.
      state.transferRootPid = "";
      state.currentTransferPath = state.transferRoot;
      state.currentTransferPid = state.transferRootPid;
      state.currentOpenPath = state.openPaths[state.currentOpenPreset] || "/";
      $("openCurrent").textContent = state.currentOpenPath;
      $("folderCurrent").textContent = state.transferRoot;
      $("targetPath").textContent = state.transferRoot;
      refreshLibraryStatus();
      refreshLinkcheckStatus();
      refreshCloudStatus();
      refreshRe0SyncStatus();
    }).catch(function (e) { feedback("globalError", e.message, "error"); });
  }

  // ------------------------------------------------------------------
  // views.account: who is signed in, and -- for an administrator -- the
  // approval queue and the member RE0 switch (multi-user plan §13).
  // Capabilities decide what to draw; the server decides what is allowed.
  // ------------------------------------------------------------------
  var ROLE_LABEL = { admin: "管理员", member: "普通用户" };
  var USER_STATUS_LABEL = { pending: "待审批", active: "已启用", rejected: "已拒绝", disabled: "已停用" };

  function capability(name) {
    return !!(state.me && state.me.capabilities && state.me.capabilities[name]);
  }

  // views.my115: the two-step "我的 115" card (plan §4.2). Each step shows
  // what it lets you do. Configuration errors appear only after an attempt.
  state.open115 = { generation: 0, challengeId: null, timer: null, countdown: null, expiresAt: 0, trigger: null, reloaded: false };

  // Step B owns a separate challenge. Only an opaque ID is sent back to our
  // server; token exchange and verification never run in the browser.
  views.open115 = {
    stop: function () {
      clearTimeout(state.open115.timer);
      clearInterval(state.open115.countdown);
      state.open115.timer = null;
      state.open115.countdown = null;
    },
    current: function (generation) {
      return state.open115.generation === generation && !$("open115Dialog").hidden;
    },
    cancel: function (id) {
      if (!id) return Promise.resolve();
      return api.request("/api/me/115/open/cancel", { method: "POST", body: JSON.stringify({ challenge_id: id }) }).then(function (d) {
        // The token commit may have won just before cancel. Reconcile that
        // server-confirmed success without reopening the closed dialog.
        if (d.status === "connected") {
          state.transferFoldersLoaded = false;
          return views.reauth.afterAuthenticated().then(function () { views.open115.reloadIfNeeded(); });
        }
      });
    },
    clearQr: function () {
      var canvas = $("open115Qr");
      canvas.hidden = true;
      canvas.width = canvas.height = 1;
    },
    drawQr: function (payload) {
      if (typeof payload !== "string" || !payload || payload.length > 4096 || typeof qrcode !== "function") throw new Error("qr_unavailable");
      qrcode.stringToBytes = qrcode.stringToBytesFuncs["UTF-8"];
      var qr = qrcode(0, "M");
      qr.addData(payload, "Byte");
      qr.make();
      var count = qr.getModuleCount();
      var canvas = $("open115Qr");
      // Four-module quiet zone, integer pixels, no external image service.
      var scale = 6;
      canvas.width = canvas.height = (count + 8) * scale;
      var ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("qr_unavailable");
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#000";
      for (var row = 0; row < count; row++) {
        for (var col = 0; col < count; col++) {
          if (qr.isDark(row, col)) ctx.fillRect((col + 4) * scale, (row + 4) * scale, scale, scale);
        }
      }
      canvas.hidden = false;
    },
    finish: function (message, success) {
      views.open115.stop();
      state.open115.challengeId = null;
      views.open115.clearQr();
      $("open115Countdown").textContent = "";
      if (success && document.activeElement === $("open115Refresh")) $("open115Cancel").focus();
      $("open115Refresh").disabled = !!success;
      $("open115Cancel").textContent = success ? "完成" : "关闭";
      feedback("open115Result", message, success ? "success" : "error");
    },
    tick: function (generation) {
      if (!views.open115.current(generation)) return;
      var seconds = Math.max(0, Math.ceil((state.open115.expiresAt - Date.now()) / 1000));
      $("open115Countdown").textContent = "二维码将在 " + seconds + " 秒后过期";
      if (!seconds) {
        var id = state.open115.challengeId;
        views.open115.finish("二维码已过期，请刷新后重新扫码。", false);
        // Invalidate a response still in flight at the local deadline.
        state.open115.generation++;
        views.open115.cancel(id).catch(function () {});
      }
    },
    start: function () {
      views.open115.stop();
      var previous = state.open115.challengeId;
      state.open115.challengeId = null;
      var generation = ++state.open115.generation;
      views.open115.clearQr();
      $("open115Countdown").textContent = "";
      if (document.activeElement === $("open115Refresh")) $("open115Close").focus();
      $("open115Refresh").disabled = true;
      $("open115Cancel").textContent = "取消";
      feedback("open115Result", "正在获取二维码…");
      return views.open115.cancel(previous).then(function () {
        if (!views.open115.current(generation)) return;
        return api.request("/api/me/115/open/start", { method: "POST", body: "{}" });
      }).then(function (d) {
        if (!d) return;
        if (!views.open115.current(generation)) {
          return views.open115.cancel(d.challenge_id).catch(function () {});
        }
        state.open115.challengeId = d.challenge_id;
        if (d.flow !== "device_pkce" || typeof d.challenge_id !== "string" || !d.challenge_id || !Number.isFinite(d.expires_in) || d.expires_in <= 0) throw new Error("invalid_start");
        views.open115.drawQr(d.qrcode);
        state.open115.expiresAt = Date.now() + Math.min(600, d.expires_in) * 1000;
        $("open115Refresh").disabled = false;
        feedback("open115Result", "请使用 115 APP 扫码，并在手机上确认。");
        views.open115.tick(generation);
        state.open115.countdown = setInterval(function () { views.open115.tick(generation); }, 1000);
        views.open115.poll(generation);
      }).catch(function () {
        if (!views.open115.current(generation)) return;
        var id = state.open115.challengeId;
        views.open115.finish("二维码获取失败，请稍后重试。", false);
        views.open115.cancel(id).catch(function () {});
      });
    },
    poll: function (generation) {
      if (!views.open115.current(generation) || !state.open115.challengeId) return Promise.resolve();
      var id = state.open115.challengeId;
      return api.request("/api/me/115/open/status", { method: "POST", body: JSON.stringify({ challenge_id: id }) }).then(function (d) {
        if (!views.open115.current(generation) || state.open115.challengeId !== id) return;
        if (d.status === "connected") {
          views.open115.finish("已授权，目录与云下载连接成功。", true);
          state.transferFoldersLoaded = false;
          return views.reauth.afterAuthenticated().then(function () {
            views.open115.reloadIfNeeded();
          });
        }
        var messages = { expired: "二维码已过期，请刷新后重新扫码。", cancelled: "本次授权已取消。", failed: "授权未完成，原有连接未被替换。请刷新二维码重试。" };
        if (messages[d.status]) { views.open115.finish(messages[d.status], false); return; }
        if (["pending", "scanned", "busy"].indexOf(d.status) === -1) throw new Error("invalid_status");
        feedback("open115Result", d.status === "scanned" ? "已扫码，请在 115 APP 中确认授权。" : "等待 115 APP 扫码确认…");
        state.open115.timer = setTimeout(function () { views.open115.poll(generation); }, 2000);
      }).catch(function () {
        if (!views.open115.current(generation) || state.open115.challengeId !== id) return;
        feedback("open115Result", "暂时无法查询授权状态，正在重试…");
        state.open115.timer = setTimeout(function () { views.open115.poll(generation); }, 5000);
      });
    },
    reloadIfNeeded: function () {
      // Step-A-only members already have a transfer dialog but no cloud tab.
      // Closing this success dialog must not strand newly granted markup.
      if (!state.open115.reloaded && capability("own_115_cloud_download") &&
          (!$("cloud") || !$("tab-cloud") || !$("transferDialog"))) {
        state.open115.reloaded = true;
        window.location.reload();
      }
    },
    open: function () {
      if (!$("open115Dialog").hidden) return Promise.resolve();
      state.open115.trigger = document.activeElement;
      $("open115Dialog").hidden = false;
      document.body.classList.add("modal-open");
      $("open115Close").focus();
      return views.open115.start();
    },
    close: function () {
      var id = state.open115.challengeId;
      state.open115.challengeId = null;
      state.open115.generation++;
      views.open115.stop();
      views.open115.clearQr();
      $("open115Dialog").hidden = true;
      document.body.classList.remove("modal-open");
      if (state.open115.trigger) state.open115.trigger.focus();
      views.open115.cancel(id).catch(function () {
        feedback("my115Result", "未能确认取消结果，请刷新连接状态；本次二维码会自动过期。", "error");
      });
    },
    init: function () {
      $("open115Close").onclick = views.open115.close;
      $("open115Cancel").onclick = views.open115.close;
      $("open115Refresh").onclick = views.open115.start;
      document.querySelector("[data-close-open115]").onclick = views.open115.close;
      document.addEventListener("keydown", function (e) {
        if ($("open115Dialog").hidden) return;
        if (e.key === "Escape") { views.open115.close(); return; }
        if (e.key !== "Tab") return;
        var first = $("open115Close");
        var last = $("open115Refresh").disabled ? $("open115Cancel") : $("open115Refresh");
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      });
    }
  };

  views.my115 = {
    init: function () {
      if (!$("my115Card")) return;
      $("my115TransferConnect").onclick = function () { views.reauth.open(); };
      $("my115TransferDisconnect").onclick = function () { views.my115.disconnect("transfer"); };
      $("my115BrowseConnect").onclick = function () { views.my115.startBrowse(); };
      $("my115BrowseDisconnect").onclick = function () { views.my115.disconnect("browse"); };
      views.my115.load();
    },
    load: function () {
      if (!$("my115Card")) return Promise.resolve();
      return api.request("/api/me/115/status").then(function (d) {
        views.my115.render(d);
      }).catch(function () {
        views.my115.render(null);
      });
    },
    render: function (status) {
      if (!$("my115Card")) return;
      var transfer = (status && status.transfer) || { state: "unknown", label: "状态读取失败" };
      var browse = (status && status.browse) || { state: "unknown", label: "状态读取失败" };
      var summary = !status ? "状态读取失败" : transfer.state === "connected" && browse.state === "connected" ? "两项已连接"
        : transfer.state === "connected" ? "一键转存已连接" : browse.state === "connected" ? "目录与云下载已授权"
        : transfer.state === "needs_reauth" || browse.state === "needs_reauth" ? "需要重新授权" : "待连接";
      $("my115Summary").textContent = summary;
      views.my115.step("my115TransferState", transfer);
      views.my115.step("my115BrowseState", browse);
      $("my115TransferDisconnect").hidden = transfer.state !== "connected";
      $("my115TransferConnect").textContent = "重新扫码";
      $("my115BrowseConnect").disabled = false;
      $("my115BrowseConnect").textContent = "重新扫码";
      $("my115BrowseDisconnect").hidden = browse.state !== "connected";
      var note = $("my115LegacyNote");
      if (note) {
        note.hidden = browse.source !== "openlist";
        note.textContent = browse.source === "openlist" ? "已复用 OpenList 连接，可直接使用目录与云下载，也可重新扫码更新。" : "";
      }
    },
    step: function (id, info) {
      var el = $(id);
      if (!el) return;
      el.textContent = info.label || "";
      el.className = "my115-state" + (info.state === "connected" ? " is-connected"
        : info.state === "blocked" ? " is-blocked"
        : info.state === "needs_reauth" ? " is-broken" : "");
    },
    startBrowse: function () {
      return views.open115.open();
    },
    disconnect: function (step) {
      if (!window.confirm(step === "transfer"
          ? "断开后将无法一键转存，需要重新扫码。目录与云下载不受影响。"
          : "断开后将无法浏览目录与使用云下载。一键转存不受影响。")) return Promise.resolve();
      return api.request("/api/me/115/disconnect", { method: "POST", body: JSON.stringify({ step: step }) })
        .then(function () {
          feedback("my115Result", "已断开。", "success");
          return views.my115.load();
        })
        .catch(function (e) { feedback("my115Result", e.message || "断开失败", "error"); });
    }
  };

  // Only a count crosses this background path, never the user directory.
  views.approvals = {
    timer: null,
    revision: 0,
    request: null,
    started: false,
    start: function () {
      if (!capability("global_settings") || !$("settingsPendingBadge")) return;
      if (!this.started) {
        this.started = true;
        document.addEventListener("visibilitychange", function () {
          if (!document.hidden) views.approvals.refresh();
        });
        window.addEventListener("pageshow", function (event) {
          if (event.persisted) views.approvals.start();
        });
        window.addEventListener("pagehide", function () {
          clearInterval(views.approvals.timer);
          views.approvals.timer = null;
        });
      }
      if (!this.timer) this.timer = setInterval(function () { views.approvals.refresh(); }, 30000);
      return this.refresh();
    },
    render: function (count) {
      if (!capability("global_settings") || !Number.isSafeInteger(count) || count < 0) return;
      [["settingsPendingBadge", "tab-settings", "设置"],
       ["usersNavPendingBadge", "settings-nav-users", "用户与权限"]].forEach(function (item) {
        var badge = $(item[0]), button = $(item[1]);
        if (badge) {
          badge.textContent = count > 99 ? "99+" : String(count);
          badge.hidden = count === 0;
        }
        if (button) button.setAttribute("aria-label", count ? item[2] + "，" + count + " 条待审批申请" : item[2]);
      });
      if ($("usersPendingCount")) $("usersPendingCount").textContent = "待审批 " + count;
    },
    refresh: function () {
      if (!capability("global_settings") || !$("settingsPendingBadge") || document.hidden) return Promise.resolve();
      if (this.request) return this.request;
      var revision = ++this.revision;
      this.request = api.request("/api/admin/users?summary=1").then(function (d) {
        if (revision === views.approvals.revision) views.approvals.render(d.pending_count);
      }).catch(function () {
        // A failed check is not evidence that the queue is empty.
      }).then(function () { views.approvals.request = null; });
      return this.request;
    }
  };

  views.account = {
    init: function () {
      var logout = $("accountLogout");
      if (logout) logout.onclick = function () { views.account.logout(); };
      var refresh = $("usersRefresh");
      if (refresh) refresh.onclick = function () { views.account.loadUsers(); };
      var toggle = $("policyMemberUnlock");
      if (toggle) toggle.onchange = function () {
        if (!state.policyLoaded || state.policySaving) return;
        state.policyDirty = true;
        views.account.updatePolicyControls();
      };
      var save = $("policySave");
      if (save) save.onclick = function () { return views.account.savePolicy(toggle.checked); };
      views.account.updatePolicyControls();
    },
    updatePolicyControls: function () {
      var toggle = $("policyMemberUnlock");
      var save = $("policySave");
      if (toggle) toggle.disabled = !state.policyLoaded || !!state.policySaving;
      if (save) save.disabled = !state.policyLoaded || !state.policyDirty || !!state.policySaving;
    },
    load: function () {
      var version = state.accountLoadVersion = (state.accountLoadVersion || 0) + 1;
      return api.request("/api/me").then(function (me) {
        if (version !== state.accountLoadVersion) return state.me;
        state.me = me;
        views.account.render();
        views.my115.load();
        views.approvals.start();
        if (capability("global_settings") && router.readTab() === "settings") views.account.loadUsers();
        return me;
      }).catch(function () {
        if (version !== state.accountLoadVersion) return;
        // A shell that cannot say who you are still shows the library; the
        // server refuses anything else on its own.
        state.me = null;
        state.policyLoaded = false;
        views.account.updatePolicyControls();
        if ($("policyResult")) feedback("policyResult", "无法读取用户权限，请刷新页面后重试。", "error");
      });
    },
    render: function () {
      var box = $("accountList");
      if (!box || !state.me) return;
      state.policyLoaded = typeof state.me.allow_member_re0_unlock === "boolean";
      var rows = [
        ["邮箱", state.me.email || ""],
        ["身份", ROLE_LABEL[state.me.role] || state.me.role || ""],
        ["登录状态", "已登录"]
      ];
      if (state.authMode) rows.push(["站点登录方式", authModeLabel(state.authMode)]);
      box.innerHTML = rows.map(function (row) {
        return '<div class="status-row"><span class="status-dot ok"></span><span>' + esc(row[0]) +
          '</span><span>' + esc(row[1]) + "</span></div>";
      }).join("");
      var toggle = $("policyMemberUnlock");
      // R09: this is the policy "may members spend points", not "may I
      // unlock". Using the administrator's own capability made the switch
      // read false for the only person who can see it.
      if (toggle && !state.policyDirty) toggle.checked = !!(state.me && state.me.allow_member_re0_unlock);
      views.account.updatePolicyControls();
    },
    logout: function () {
      return api.request("/api/auth/logout", { method: "POST", body: "{}" })
        .then(function () { window.location.href = "/login"; })
        .catch(function (e) { toast(e.message || "退出失败", "error"); });
    },
    loadUsers: function () {
      var box = $("usersList");
      if (!box) return Promise.resolve();
      var revision = ++views.approvals.revision;
      var version = state.usersLoadVersion = (state.usersLoadVersion || 0) + 1;
      return api.request("/api/admin/users").then(function (d) {
        if (version !== state.usersLoadVersion) return;
        var users = d.users || [];
        if (revision === views.approvals.revision) views.approvals.render(d.pending_count);
        users = users.slice().sort(function (a, b) { return (a.status === "pending" ? 0 : 1) - (b.status === "pending" ? 0 : 1); });
        if (!users.length) { box.innerHTML = '<div class="empty">还没有其他用户。</div>'; return; }
        box.innerHTML = users.map(views.account.userRowHtml).join("");
        box.querySelectorAll("[data-user-action]").forEach(function (btn) {
          btn.onclick = function () { views.account.act(btn.dataset.userId, btn.dataset.userAction, btn); };
        });
      }).catch(function (e) { feedback("usersResult", e.message || "读取用户失败", "error"); });
    },
    userRowHtml: function (user) {
      var status = USER_STATUS_LABEL[user.status] || user.status || "";
      var dot = user.status === "active" ? "ok" : (user.status === "pending" ? "warn" : "bad");
      var actions = user.role === "admin" ? "" : [
        user.status === "pending" ? '<button type="button" class="link-action" data-user-action="approve" data-user-id="' + esc(user.id) + '">批准</button>' : "",
        user.status === "pending" ? '<button type="button" class="link-action" data-user-action="reject" data-user-id="' + esc(user.id) + '">拒绝</button>' : "",
        user.status === "active" ? '<button type="button" class="link-action" data-user-action="disable" data-user-id="' + esc(user.id) + '">停用</button>' : ""
      ].filter(Boolean).join("");
      return '<div class="user-row"><span class="status-dot ' + dot + '"></span>' +
        '<span class="user-email">' + esc(user.email_display || "") + "</span>" +
        '<span class="user-state">' + esc(ROLE_LABEL[user.role] || user.role || "") + " · " + esc(status) + "</span>" +
        '<span class="link-actions">' + actions + "</span></div>";
    },
    act: function (userId, action, btn) {
      if ((action === "disable" || action === "reject") && !window.confirm(action === "disable" ? "停用该用户并立即结束其所有登录会话？" : "拒绝该用户的申请？")) return Promise.resolve();
      btn.disabled = true;
      return api.request("/api/admin/users/" + encodeURIComponent(userId) + "/" + action,
                         { method: "POST", body: "{}" })
        .then(function (d) {
          // Invalidate reads started before this acknowledged write.
          views.approvals.revision++;
          views.approvals.render(d.pending_count);
          feedback("usersResult", "已" + ({ approve: "批准", reject: "拒绝", disable: "停用" }[action] || "处理"), "success");
          return views.account.loadUsers();
        })
        .catch(function (e) { btn.disabled = false; feedback("usersResult", e.message || "操作失败", "error"); });
    },
    savePolicy: function (enabled) {
      var btn = $("policySave");
      if (!state.policyLoaded || !state.policyDirty || state.policySaving || (btn && btn.disabled)) return Promise.resolve();
      var revision = views.settingsNav.revisions.policy || 0;
      state.accountLoadVersion = (state.accountLoadVersion || 0) + 1;
      state.policySaving = true;
      views.account.updatePolicyControls();
      feedback("policyResult", "正在保存用户权限…");
      return api.request("/api/admin/policies/re0", {
        method: "PATCH", body: JSON.stringify({ allow_member_re0_unlock: !!enabled })
      }).then(function () {
        // A GET begun before this write completed may carry the old policy.
        // It must not overwrite the acknowledged value or clear a valid actor.
        state.accountLoadVersion = (state.accountLoadVersion || 0) + 1;
        if (state.me) state.me.allow_member_re0_unlock = !!enabled;
        views.settingsNav.saved("policy", revision);
        feedback("policyResult", enabled ? "已允许普通用户消耗积分解锁。" : "已关闭普通用户解锁。", "success");
      }).catch(function (e) {
        feedback("policyResult", e.message || "保存失败", "error");
      }).then(function () {
        state.policySaving = false;
        views.account.updatePolicyControls();
      });
    }
  };

  // ------------------------------------------------------------------
  // views.login: the sign-in / apply page (multi-user plan §11).
  // It shares this bundle -- one release-allowlisted script -- but none of
  // the shell above applies to it, so init() below returns before any of it
  // runs.
  // ------------------------------------------------------------------
  views.login = {
    mode: "login",
    artworkTimer: null,
    artworkFetchedAt: 0,
    artworkRequest: null,
    init: function () {
      var form = $("loginForm");
      if (!form) return;
      form.addEventListener("submit", function (e) { e.preventDefault(); views.login.submit(); });
      $("loginSwitch").onclick = function () { views.login.setMode(views.login.mode === "login" ? "register" : "login"); };
      $("loginGoogle").onclick = function () { window.location.href = "/auth/google"; };
      document.addEventListener("visibilitychange", function () {
        views.login.pauseArtwork();
        if (!document.hidden && Date.now() - views.login.artworkFetchedAt >= 6 * 3600000) views.login.refreshArtwork();
      });
      window.addEventListener("pagehide", function () { clearTimeout(views.login.artworkTimer); });
      window.addEventListener("pageshow", function (event) {
        if (event.persisted) views.login.refreshArtwork();
      });
      views.login.bootstrap();
    },
    bootstrap: function () {
      return api.request("/api/auth/bootstrap").then(function (d) {
        api.csrf = d.csrf || "";
        api.csrfFetchedAt = Date.now();
        $("loginAttribution").textContent = d.tmdb_attribution || "";
        if (d.admin_email_hint) $("loginGoogle").setAttribute("title", d.admin_email_hint);
        views.login.renderArtwork(d.artwork || []);
        if (d.registration_open === false) {
          $("loginSwitch").hidden = true;
          $("loginSwitchLead").textContent = "目前不开放申请。";
        }
      }).catch(function () {
        // The page still signs people in without any of this; only the
        // decoration and the pre-auth token are missing, and the token is
        // re-fetched by api.request's own retry when the first write is
        // refused.
        views.login.renderArtwork([]);
      });
    },
    pauseArtwork: function () {
      $("loginArtworkGrid").classList.toggle("is-paused", document.hidden);
    },
    refreshArtwork: function () {
      if (views.login.artworkRequest) return views.login.artworkRequest;
      clearTimeout(views.login.artworkTimer);
      // Decorations do not refresh credentials or interrupt the login form.
      views.login.artworkRequest = api.request("/api/auth/bootstrap").then(function (d) {
        if (Array.isArray(d.artwork) && d.artwork.length) views.login.renderArtwork(d.artwork);
      }).catch(function () {}).then(function () {
        views.login.artworkFetchedAt = Date.now();
        views.login.artworkRequest = null;
        views.login.scheduleArtwork();
      });
      return views.login.artworkRequest;
    },
    scheduleArtwork: function () {
      clearTimeout(views.login.artworkTimer);
      views.login.artworkTimer = setTimeout(function () {
        if (!document.hidden) views.login.refreshArtwork();
      }, 6 * 3600000);
    },
    // Twelve independent live tiles: 3-second holds and 3-second half-turns.
    // Both images must load before a tile animates; a failed side stays static.
    renderArtwork: function (urls) {
      var box = $("loginArtworkGrid");
      if (!box) return;
      var pool = (urls || []).filter(function (url, index, all) {
        return typeof url === "string" && /^https:\/\/image\.tmdb\.org\/t\/p\/w(?:92|154|185|342|500|780)\/[A-Za-z0-9][A-Za-z0-9._-]{5,63}\.(?:jpg|png|webp)$/.test(url) && all.indexOf(url) === index;
      }).slice(0, 24);
      if (!pool.length && box.children.length) return;
      box.innerHTML = "";
      pool.slice(0, 12).forEach(function (url, index) {
        var tile = document.createElement("div"), turn = document.createElement("div");
        tile.className = "login-poster";
        turn.className = "login-poster-turn";
        turn.style.setProperty("--poster-delay", (-(index * 5 % 12)) + "s");
        tile.appendChild(turn);
        box.appendChild(tile);
        var pair = pool.length > 12 ? [url, pool[index + 12]] : [url];
        var loaded = 0, failed = false;
        pair.filter(Boolean).forEach(function (src, side) {
          var img = document.createElement("img");
          img.alt = "";
          img.className = side ? "login-poster-back" : "login-poster-front";
          img.loading = "lazy";
          img.decoding = "async";
          img.onload = function () {
            loaded++;
            if (!failed && loaded === 2) turn.classList.add("is-ready");
          };
          img.onerror = function () {
            failed = true;
            turn.classList.remove("is-ready");
            if (img.parentNode) turn.removeChild(img);
            if (turn.children.length) turn.children[0].className = "login-poster-front";
            else if (tile.parentNode) box.removeChild(tile);
          };
          turn.appendChild(img);
          img.src = src;
        });
      });
      views.login.artworkFetchedAt = Date.now();
      views.login.pauseArtwork();
      views.login.scheduleArtwork();
    },
    setMode: function (mode) {
      views.login.mode = mode;
      var registering = mode === "register";
      $("loginTitle").textContent = registering ? "申请 HiDrive-Lite 账号" : "登录 HiDrive-Lite";
      $("loginLead").textContent = registering
        ? "提交后需要管理员批准才能登录。"
        : "";
      $("loginLead").hidden = !registering;
      $("loginConfirmField").hidden = !registering;
      $("loginSubmit").textContent = registering ? "提交申请" : "登录";
      $("loginSwitchLead").textContent = registering ? "已经有账号？" : "还没有账号？";
      $("loginSwitch").textContent = registering ? "返回登录" : "申请账号";
      $("loginPassword").setAttribute("autocomplete", registering ? "new-password" : "current-password");
      views.login.say("", "");
      $("loginEmail").focus();
    },
    say: function (text, kind) {
      var box = $("loginMessage");
      box.textContent = text || "";
      box.hidden = !text;
      box.className = "login-message" + (kind ? " is-" + kind : "");
    },
    submit: function () {
      var registering = views.login.mode === "register";
      var email = $("loginEmail").value.trim();
      var password = $("loginPassword").value;
      if (!email || !password) { views.login.say("请填写邮箱和密码", "error"); return Promise.resolve(); }
      if (registering && password !== $("loginConfirm").value) {
        views.login.say("两次输入的密码不一致", "error");
        return Promise.resolve();
      }
      var body = registering
        ? { email: email, password: password, confirm_password: $("loginConfirm").value }
        : { email: email, password: password };
      var url = registering ? "/api/auth/register" : "/api/auth/login";
      $("loginSubmit").disabled = true;
      views.login.say("", "");
      return api.request(url, { method: "POST", body: JSON.stringify(body) }).then(function () {
        if (registering) {
          views.login.say("申请已提交，等待管理员审批。", "done");
          $("loginPassword").value = "";
          $("loginConfirm").value = "";
        } else {
          window.location.href = "/";
        }
      }).catch(function (e) {
        views.login.say(e.message || "请稍后再试", "error");
      }).then(function () {
        $("loginSubmit").disabled = false;
      });
    }
  };

  // The login page renders from the same bundle but shares none of the
  // shell's state: it has no tabs, no session yet, and every API call below
  // would be refused. Run its controller and stop here.
  if (document.body.dataset.page === "login") {
    views.login.init();
    return;
  }

  // Only what this page was built with. A member's page has no OpenList or
  // STRM panel, no global settings card and no 115 dialogs, so those
  // initialisers have nothing to bind to.
  if ($("openlist")) views.browser.openlist.init();
  if ($("strm")) views.browser.strm.init();
  if ($("transferDialog")) views.transfer.init();
  // Always present for a signed-in user: it is how step A gets connected in
  // the first place (R04).
  if ($("reauthDialog")) views.reauth.init();
  if ($("open115Dialog")) views.open115.init();
  if ($("settingsSave")) views.settings.init();
  views.settingsNav.init();
  if ($("cloud")) views.cloud.init();
  views.account.init();
  views.my115.init();
  views.library.init();
  views.library.loadFilters();
  views.detail.init();
  stickyHeader.init();
  // Refresh clears transient search filters, not the current category/page.
  // Fresh shared links and history keep all their state.
  var navigation = window.performance && window.performance.getEntriesByType
    ? window.performance.getEntriesByType("navigation")[0] : null;
  if (navigation && navigation.type === "reload") {
    var freshUrl = new URL(window.location.href);
    ["q", "year", "provider", "quality", "hdr", "genre", "source", "deleted"].forEach(function (key) {
      freshUrl.searchParams.delete(key);
    });
    window.history.replaceState(window.history.state, "", freshUrl.toString());
  }
  router.init();
  init();
})();
