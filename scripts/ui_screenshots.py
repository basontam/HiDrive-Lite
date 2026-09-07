#!/usr/bin/env python3
"""Six-size screenshots, keyboard walkthrough and a WCAG contrast spot-check
for HiDrive-Lite's media-library UI (T5.8).

Builds a synthetic, installed media library (the same fixture data as
``tests/conftest.py``'s ``installed_library``, via
``tests/fixtures/library/build_installed.py``) inside a throwaway temp
directory, starts ``app.py`` as a subprocess with ``HIDRIVE_AUTH_MODE=local``
pointed entirely at that temp directory (never at production paths), then
uses Playwright (a dev-only dependency -- see ``requirements-dev.txt``) to:

* capture the eight page states (home, search results, the filters popover
  open, detail (every resource group's links visible immediately, no
  expand step), the transfer dialog, settings,
  OpenList, STRM) at the six breakpoints from the RE0 spec (Sec 18), writing
  PNGs to ``build/ui-screenshots/<WxH>/<page>.png`` (``build/`` is
  gitignored -- nothing here is committed);
* run an automated keyboard walkthrough (Tab order, Enter-opens-detail,
  Escape-closes-dialog-and-returns-focus, ``role=tab`` ``aria-selected``);
* spot-check WCAG contrast on primary/secondary text and a primary button;
* record a per-page/size horizontal-overflow check
  (``document.documentElement.scrollWidth <= window.innerWidth``);
* auto-accept poster-box geometry (y1-cards §2.3) on the home rails and
  search results grid at all six breakpoints: every visible poster's
  width/height agree within 1px, its ratio is 2:3 within 0.01, no box has
  zero height, and image/fallback/loading states -- plus a long title and
  keyboard focus -- never change a box's size;
* check the filters popover (T16 §5/§6) at every breakpoint: fully inside
  the viewport, above the poster wall (``elementFromPoint`` on a popover
  control hits the control, not a card underneath), Escape closes it and
  returns focus to the toggle button, and body scroll is locked behind the
  mobile (<=650px) bottom sheet;
* check the detail page's layered backdrop hero (T21 §8.4) against 4 local,
  different-ratio fixture images (16:9, 4:3, 2.39:1, 1:1) plus the
  no-backdrop fallback, at five of the six breakpoints: the foreground
  layer is always ``object-fit:contain`` (never ``cover``), its rendered
  box lies entirely inside the container at the fixture's own ratio (all
  four corners survive -- never cropped), no CLS (container height
  identical before/after the image loads, and fallback == loaded-state
  height at the same viewport), no overlap between the title/info box and
  the backdrop, and the accessibility tree exposes only the foreground's
  alt text;

and writes ``build/ui-screenshots/report.json`` (booleans and element ids
only -- no link text, no access codes; the synthetic fixture data has none
anyway).

Every request the browser makes is intercepted (``page.route``): same-origin
requests to the app subprocess pass through, ``image.tmdb.org`` is fulfilled
with a small generated placeholder PNG (so poster/backdrop layouts render
without any real network access), and everything else is aborted -- this
also keeps CPU/memory pressure down in this container (renderer crashes have
been observed here on real page loads under ``/dev/shm``/memory pressure),
so Chromium is launched with ``--disable-dev-shm-usage --disable-gpu
--no-sandbox`` and only one browser/one context is ever open at a time.

Chromium is a Playwright-managed browser binary, downloaded to Playwright's
own default cache directory (``~/.cache/ms-playwright``) on first use --
never into this repository. ``/tmp`` in this container is a 16 MB tmpfs, so
the (~180 MB) download is staged under ``~/.cache`` instead via ``TMPDIR``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PYTEST_TMP = ROOT / ".pytest-tmp"


def _ensure_roomy_process_tmpdir() -> None:
    """If the process' current default temp directory (``tempfile.
    gettempdir()`` -- ``$TMPDIR``, or ``/tmp`` when unset) is a small
    filesystem (this container's ``/tmp`` is a 16 MB tmpfs, routinely
    driven to 0 bytes free by other concurrent sessions sharing the same
    box -- see the module docstring), redirect ``$TMPDIR`` to a real-disk
    directory under the repo before anything else in this process creates
    a temp file.

    This is deliberately broader than ``_browser_env()`` below (which only
    covers the *spawned Chromium subprocess*'s own env): a full ``/tmp``
    has also been observed to fail plain ``print()``/stdout writes here,
    almost certainly because pytest's own output-capturing machinery (and
    Python's ``tempfile`` module generally) defaults to the same tmpfs.
    Calling this at import time -- before ``pytest`` finishes collecting
    this module's own test file -- gives any *later* temp file the repo's
    own code creates in this process a fighting chance of landing
    somewhere with room, even though it cannot retroactively fix temp
    files pytest's core may have already opened during its own startup.
    Idempotent and silent if ``/tmp`` turns out to be roomy (a normal
    laptop/CI box) -- $TMPDIR is left untouched in that case."""
    if "TMPDIR" in os.environ:
        return  # caller already made an explicit choice -- respect it.
    try:
        total = shutil.disk_usage(tempfile.gettempdir()).total
    except OSError:
        return
    if total > 200 * 1024 * 1024:  # comfortably larger than a 16 MB tmpfs
        return
    process_tmpdir = PYTEST_TMP / "process-tmp"
    process_tmpdir.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(process_tmpdir)
    tempfile.tempdir = None  # force tempfile to re-read $TMPDIR next call


_ensure_roomy_process_tmpdir()

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import library_store  # noqa: E402
from fixtures.library.build_installed import build_synthetic_library  # noqa: E402

SIZES: tuple[tuple[int, int], ...] = (
    (1440, 900),
    (1280, 800),
    (1024, 768),
    (768, 1024),
    (390, 844),
    (375, 812),
)
ALL_PAGES: tuple[str, ...] = ("home", "search", "filters", "detail", "transfer", "settings", "openlist", "strm")

# Environment fallbacks app.py's config_value() honours in local mode -- must
# never leak a developer's real credentials into the subprocess or a
# screenshot (mirrors tests/conftest.py's SECRET_ENV_NAMES).
_SECRET_ENV_NAMES = (
    "HDHIVE_APP_SECRET",
    "HDHIVE_CLIENT_ID",
    "TMDB_API_KEY",
    "ENV_115_COOKIES",
    "OPENLIST_TOKEN",
    "115_open_access_token",
    "115_open_refresh_token",
    "openlist_token",
    "115_cookie",
)


# ---------------------------------------------------------------------------
# Pure helpers: WCAG contrast math and report assembly (unit-tested without a
# browser in tests/test_ui_screenshots.py).
# ---------------------------------------------------------------------------

_RGB_RE = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*[\d.]+\s*)?\)")


def parse_css_color(value: str) -> tuple[int, int, int]:
    """Parse a ``getComputedStyle`` color string (``rgb(...)``/``rgba(...)``)
    into an ``(r, g, b)`` tuple of 0-255 ints. Raises ``ValueError`` on any
    other format (keyword colors, hex, etc. are not expected from
    ``getComputedStyle``)."""
    match = _RGB_RE.match((value or "").strip())
    if not match:
        raise ValueError(f"unsupported CSS color: {value!r}")
    return tuple(min(255, max(0, round(float(match.group(i))))) for i in (1, 2, 3))  # type: ignore[return-value]


def _srgb_channel(c: int) -> float:
    x = c / 255.0
    return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.x relative luminance of an sRGB color."""
    r, g, b = rgb
    return 0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)


def contrast_ratio(rgb_a: tuple[int, int, int], rgb_b: tuple[int, int, int]) -> float:
    """WCAG 2.x contrast ratio between two sRGB colors (1.0-21.0)."""
    la = relative_luminance(rgb_a) + 0.05
    lb = relative_luminance(rgb_b) + 0.05
    return max(la, lb) / min(la, lb)


def contrast_pass(ratio: float, threshold: float = 4.5) -> bool:
    return ratio >= threshold


def assemble_report(
    *,
    sizes: Sequence[str],
    pages: Sequence[str],
    keyboard: dict,
    contrast: list[dict],
    overflow: Sequence[dict],
    card_heights_uniform: dict,
    geometry: dict,
    filters_popover: dict | None = None,
    library_tab_reset: dict | None = None,
    provider_isolation: dict | None = None,
    backdrop: dict | None = None,
    linkcheck: dict | None = None,
    retries: int = 0,
) -> dict:
    """Build the ``build/ui-screenshots/report.json`` structure. Every value
    here must be a bool, a number, an element id/string label, or a nested
    collection of those -- never link text or an access code.

    ``retries`` (follow-up, deterministic-waits round 2): how many
    individual page/size captures needed their one bounded
    close-context/reopen/retry (see ``capture_pages``) after a Playwright
    ``TimeoutError`` -- 0 on a fully clean run."""
    return {
        "sizes": list(sizes),
        "pages": list(pages),
        "keyboard": keyboard,
        "contrast": contrast,
        "overflow": list(overflow),
        "card_heights_uniform": card_heights_uniform,
        "geometry": geometry,
        "filters_popover": filters_popover if filters_popover is not None else {"per_viewport": {}, "violations": [], "ok": True},
        "library_tab_reset": library_tab_reset if library_tab_reset is not None else {"scenarios": {}, "violations": [], "ok": True},
        "provider_isolation": provider_isolation if provider_isolation is not None else {
            "filtered_dom_excludes_other_provider": True,
            "filtered_accessibility_tree_excludes_other_provider": True,
            "tab_switch_dom_excludes_other_provider": True,
            "tab_switch_accessibility_tree_excludes_other_provider": True,
            "unfiltered_view_shows_other_provider": True,
            "violations": [],
            "ok": True,
        },
        "backdrop": backdrop if backdrop is not None else {"sizes": [], "results": [], "violations": [], "ok": True},
        "linkcheck": linkcheck if linkcheck is not None else {"sizes": [], "results": [], "violations": [], "ok": True},
        "retries": retries,
    }


# ---------------------------------------------------------------------------
# Chromium availability (dev-only Playwright browser, cached outside the repo).
# ---------------------------------------------------------------------------

# This container sets HTTP_PROXY/HTTPS_PROXY/ALL_PROXY. Chromium honours
# those by default for every navigation, including our own 127.0.0.1
# subprocess -- launch it with those stripped from its process environment
# (Playwright's launch(env=...) replaces, rather than merges with, the
# current process environment) so it always reaches the app directly.
_PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

# This container's Chromium/chrome-headless-shell only launches with a set of
# system shared libraries that aren't on the default loader path -- see
# ``~/.local/bin/with-chromium-env``. Prepending the same default here makes
# ``_browser_env()`` self-sufficient even when the script isn't invoked
# through that wrapper (the wrapper already exports it, so this is a no-op
# in that case).
_DEFAULT_CHROMIUM_LIB_DIR = Path.home() / ".local" / "chromium-deps" / "root" / "usr" / "lib" / "x86_64-linux-gnu"

# Renderer crashes have been observed in this container under real page
# loads (64 MB /dev/shm, tight overall memory) -- these flags avoid the
# /dev/shm-backed shared memory Chromium otherwise wants and disable GPU
# compositing, which isn't available (or needed) for headless screenshots.
_LAUNCH_ARGS = ["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"]

# --disable-dev-shm-usage makes Chromium fall back to $TMPDIR (default
# /tmp) for the shared-memory-backed temp files it would otherwise put in
# /dev/shm -- but /tmp in this container is an even smaller 16 MB tmpfs, so
# without redirecting TMPDIR that flag trades one out-of-space renderer
# crash for another. Point it at a real-disk directory inside the repo
# instead (never /tmp, mirroring the same reasoning as PYTEST_TMP above).
_CHROMIUM_TMPDIR = ROOT / ".pytest-tmp" / "chromium-tmp"


def _browser_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _PROXY_ENV_NAMES}
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    lib_dir = os.environ.get("HIDRIVE_CHROMIUM_LIB_DIR", str(_DEFAULT_CHROMIUM_LIB_DIR))
    if Path(lib_dir).is_dir():
        existing = env.get("LD_LIBRARY_PATH", "")
        if lib_dir not in existing.split(":"):
            env["LD_LIBRARY_PATH"] = f"{lib_dir}:{existing}" if existing else lib_dir
    _CHROMIUM_TMPDIR.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(_CHROMIUM_TMPDIR)
    return env


def launch_chromium(playwright):
    """``playwright.chromium.launch()`` with the proxy env vars stripped and
    this container's Chromium shared-library dir + crash-avoidance flags
    applied -- every caller that will navigate to the local app subprocess
    must use this instead of calling ``.launch()`` directly."""
    return playwright.chromium.launch(env=_browser_env(), args=_LAUNCH_ARGS)


def chromium_available() -> tuple[bool, str]:
    """Try an actual headless launch (a present-but-unlaunchable binary --
    e.g. missing OS shared libraries -- is exactly as unusable as a missing
    one). Returns ``(True, "ok")`` or ``(False, "<the real error>")``."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return False, f"playwright not importable: {exc}"
    try:
        with sync_playwright() as p:
            browser = launch_chromium(p)
            browser.close()
    except Exception as exc:  # noqa: BLE001 - surface the exact failure verbatim
        detail = str(exc)
        lib_error = re.search(r"error while loading shared libraries:[^\n]*", detail)
        message = lib_error.group(0) if lib_error else detail.splitlines()[0]
        return False, f"{type(exc).__name__}: {message}"
    return True, "ok"


def _chromium_missing() -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return not Path(p.chromium.executable_path).exists()


def ensure_chromium() -> tuple[bool, str]:
    """If the chromium binary is missing, install it (staging the download
    outside the 16 MB ``/tmp`` tmpfs). Either way, return the result of an
    actual launch attempt -- the caller must not proceed on ``False``."""
    try:
        missing = _chromium_missing()
    except ImportError:
        return False, "playwright is not installed in this environment (pip install -r requirements-dev.txt)"
    if missing:
        stage = Path(tempfile.mkdtemp(prefix="pw-install-", dir=str(Path.home() / ".cache")))
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                cwd=str(ROOT),
                env={**os.environ, "TMPDIR": str(stage)},
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    return chromium_available()


# ---------------------------------------------------------------------------
# Network guard: block every non-local request, fulfil image.tmdb.org with a
# generated placeholder so screenshots never depend on real network access.
# ---------------------------------------------------------------------------


def _generate_placeholder_png(width: int = 40, height: int = 60) -> bytes:
    """A tiny in-memory gradient PNG, built with the standard library only
    (no Pillow / other new dependency) -- used to fulfil intercepted
    ``image.tmdb.org`` requests during screenshotting."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # per-scanline filter type: None
        for x in range(width):
            rows.extend((int(255 * x / max(width - 1, 1)), int(255 * y / max(height - 1, 1)), 200))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


_PLACEHOLDER_PNG = _generate_placeholder_png()


# T21 §8.4: local, different-ratio backdrop fixtures (16:9, 4:3, 2.39:1,
# 1:1) so the layered hero's object-fit:contain / no-crop behaviour can
# actually be exercised -- this container has no internet access, so these
# are never fetched from image.tmdb.org for real. app.py's _tmdb_image_url()
# / TMDB_IMAGE_BASE are left untouched (no backend change): the seeded
# fixture media's own backdrop_path (see
# _seed_backdrop_fixtures_for_screenshots below) just embeds a distinctive
# ``fixture-backdrop-<key>.jpg`` suffix, which install_network_guard
# recognises below and fulfils with an in-process, stdlib-only PNG (no
# Pillow / other new dependency, extending _generate_placeholder_png above)
# instead of a second local HTTP server.
BACKDROP_FIXTURE_RATIOS: dict[str, tuple[int, int]] = {
    "16x9": (640, 360),
    "4x3": (640, 480),
    "239x100": (640, 268),
    "1x1": (640, 640),
}
_BACKDROP_FIXTURE_PATH_RE = re.compile(r"/fixture-backdrop-([a-z0-9]+)\.jpg$")


def backdrop_fixture_path(key: str) -> str:
    """The ``library_store.MediaRecord.backdrop_path`` value for fixture
    ``key`` -- app.py's existing, unmodified ``_tmdb_image_url()`` turns
    this into ``https://image.tmdb.org/t/p/w1280/fixture-backdrop-<key>.jpg``."""
    return f"/fixture-backdrop-{key}.jpg"


def _generate_bordered_fixture_png(width: int, height: int) -> bytes:
    """Like ``_generate_placeholder_png`` above, but with a light border
    stripe and four distinctly-coloured corner marker blocks (red/green/
    blue/yellow -- top-left/top-right/bottom-left/bottom-right). Lets a
    human look at the resulting ``detail-backdrop-*.png`` screenshots and
    see whether all four corners of the source image survived
    ``object-fit:contain`` (they must -- ``object-fit:cover`` would crop
    at least one pair, which is exactly the §8.1 bug this brief fixes)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    border = 6
    marker = max(16, min(width, height) // 6)
    corner_colors = ((214, 40, 40), (40, 170, 70), (40, 100, 220), (230, 190, 20))  # TL, TR, BL, BR
    fill = (26, 27, 32)
    border_color = (235, 235, 240)
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # per-scanline filter type: None
        on_horizontal_border = y < border or y >= height - border
        for x in range(width):
            if on_horizontal_border or x < border or x >= width - border:
                color = border_color
            elif x < marker and y < marker:
                color = corner_colors[0]
            elif x >= width - marker and y < marker:
                color = corner_colors[1]
            elif x < marker and y >= height - marker:
                color = corner_colors[2]
            elif x >= width - marker and y >= height - marker:
                color = corner_colors[3]
            else:
                color = fill
            rows.extend(color)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


_backdrop_fixture_png_cache: dict[str, bytes] = {}


def _backdrop_fixture_png(key: str) -> bytes | None:
    """Lazily generate (once per process) and cache the fixture PNG for
    ``key`` -- keeps the (cheap, but non-zero) generation cost out of
    every test module import, paid only when a backdrop capture actually
    runs."""
    dims = BACKDROP_FIXTURE_RATIOS.get(key)
    if dims is None:
        return None
    if key not in _backdrop_fixture_png_cache:
        _backdrop_fixture_png_cache[key] = _generate_bordered_fixture_png(*dims)
    return _backdrop_fixture_png_cache[key]


def install_network_guard(page, base_url: str) -> None:
    """Intercept every request this page makes: same-origin (the app
    subprocess itself) requests pass through untouched, ``image.tmdb.org``
    is fulfilled with ``_PLACEHOLDER_PNG`` (or, for a T21 §8.4 backdrop
    fixture URL, that fixture's own distinctly-bordered/ratioed PNG --
    see ``BACKDROP_FIXTURE_RATIOS`` above), and every other host is
    aborted. Must be called once per page, before the first ``page.goto()``.

    Also raises Playwright's default navigation/action timeout from 30s to
    60s: the single-threaded Werkzeug dev server this drives serves every
    request serially, and the T16 eight-page run (one more full-reload
    navigation per breakpoint than before, each now firing one extra
    recommendations-rail fetch on top of the existing hero/rail fetches)
    has been observed to occasionally exceed 30s under this container's
    documented memory/CPU pressure, well before Chromium itself is
    actually struggling."""
    page.set_default_navigation_timeout(60000)
    page.set_default_timeout(45000)
    local_netloc = urlparse(base_url).netloc

    def handle(route):
        request_url = route.request.url
        parsed = urlparse(request_url)
        if parsed.netloc == local_netloc:
            route.continue_()
        elif parsed.hostname == "image.tmdb.org":
            fixture_match = _BACKDROP_FIXTURE_PATH_RE.search(parsed.path)
            fixture_png = _backdrop_fixture_png(fixture_match.group(1)) if fixture_match else None
            route.fulfill(status=200, content_type="image/png", body=fixture_png or _PLACEHOLDER_PNG)
        else:
            route.abort()

    page.route("**/*", handle)


# ---------------------------------------------------------------------------
# y1-cards §2.3: poster-geometry auto-acceptance. A pure analysis function
# (unit-tested without a browser below) plus the DOM probe that feeds it
# real measurements from a running page.
# ---------------------------------------------------------------------------

_POSTER_RATIO_TARGET = 1.5
_POSTER_RATIO_TOLERANCE = 0.01
_POSTER_DIM_TOLERANCE_PX = 1.0

# This fixture library (fixtures/library/build_installed.py) gives exactly
# one media item a real poster_path -- everything else has no poster_url at
# all, so mediaCardHtml never renders an <img> for it and the "no poster"
# fallback state is already present for free on every page. The "loading"
# state doesn't need a real, timing-sensitive network delay: cloning a real
# card and pointing its poster's <img> at a fresh (mocked) URL gives an
# element whose `img.complete` is deterministically false at the exact
# synchronous instant right after `img.src` is set -- a browser never
# finishes an image load within that same tick -- so the probe below reads
# a genuine "loading" box on every run, no sleeps or route timing races.
_POSTER_BOX_PROBE = """
(sel) => {
  const container = document.querySelector(sel);
  if (!container) return [];
  const cards = Array.from(container.querySelectorAll('.media-card[data-media-id]'));
  const boxes = cards.map((card) => {
    const poster = card.querySelector('.poster') || card.querySelector('.media-card__poster');
    const r = poster.getBoundingClientRect();
    const img = poster.querySelector('img');
    let state = 'skeleton';
    if (img) {
      state = img.classList.contains('poster-broken') ? 'fallback' : (img.complete ? 'image' : 'loading');
    } else if (poster.querySelector('.poster-fallback')) {
      state = 'fallback';
    }
    return { width: r.width, height: r.height, state: state };
  });
  if (cards.length) {
    const clone = cards[0].cloneNode(true);
    clone.removeAttribute('data-media-id');
    const poster = clone.querySelector('.poster') || clone.querySelector('.media-card__poster');
    poster.innerHTML = '';
    const img = document.createElement('img');
    img.src = 'https://image.tmdb.org/t/p/w342/__geometry_probe__.jpg?r=' + Math.random();
    poster.appendChild(img);
    cards[0].parentElement.appendChild(clone);
    const r = poster.getBoundingClientRect();
    boxes.push({ width: r.width, height: r.height, state: img.complete ? 'image' : 'loading' });
    clone.remove();
  }
  return boxes;
}
"""


def collect_poster_boxes(page, container_selector: str) -> list[dict]:
    """Every real card's poster box geometry + current loading state under
    ``container_selector`` (e.g. ``"#libraryRails"``/``"#libraryResults"``),
    plus one synthetic "loading" sample (see module comment above) --
    matches both the legacy ``.poster`` class and the spec's
    ``.media-card__poster`` alias (y1-cards §2.1's single-box contract)."""
    return page.evaluate(_POSTER_BOX_PROBE, container_selector)


def analyze_poster_geometry(boxes: Sequence[dict]) -> dict:
    """Pure geometry analysis over one container's poster boxes -- each
    ``box`` is ``{"width": float, "height": float, "state": str}`` (from
    ``collect_poster_boxes``). Checks, per y1-cards §2.3:

    1) every box's width is within 1px of every other box's width in this
       container (and likewise height);
    2) every box's height/width ratio is 1.5 +/- 0.01;
    3) no box has height <= 0;
    4) boxes are the same size regardless of which loading state
       (image/fallback/loading/skeleton) they're currently in.

    Never raises -- an empty ``boxes`` sequence is reported as a failure
    (``ok: False``) instead of crashing on ``min()``/``max()`` of an empty
    sequence, so a broken selector shows up as a report failure."""
    if not boxes:
        return {
            "count": 0, "max_width_diff": None, "max_height_diff": None,
            "min_ratio": None, "max_ratio": None, "min_height": None,
            "states": [], "states_uniform": False, "ok": False,
        }

    widths = [b["width"] for b in boxes]
    heights = [b["height"] for b in boxes]
    ratios = [(h / w) if w else float("inf") for w, h in zip(widths, heights)]
    max_width_diff = max(widths) - min(widths)
    max_height_diff = max(heights) - min(heights)
    min_height = min(heights)
    ratio_ok = all(abs(r - _POSTER_RATIO_TARGET) <= _POSTER_RATIO_TOLERANCE for r in ratios)

    by_state: dict[str, list[dict]] = {}
    for b in boxes:
        by_state.setdefault(b.get("state", "unknown"), []).append(b)
    state_dims = [
        (sum(x["width"] for x in items) / len(items), sum(x["height"] for x in items) / len(items))
        for items in by_state.values()
    ]
    states_uniform = all(
        abs(state_dims[i][0] - state_dims[j][0]) <= _POSTER_DIM_TOLERANCE_PX
        and abs(state_dims[i][1] - state_dims[j][1]) <= _POSTER_DIM_TOLERANCE_PX
        for i in range(len(state_dims)) for j in range(i + 1, len(state_dims))
    )

    ok = (
        max_width_diff <= _POSTER_DIM_TOLERANCE_PX
        and max_height_diff <= _POSTER_DIM_TOLERANCE_PX
        and ratio_ok
        and min_height > 0
        and states_uniform
    )
    return {
        "count": len(boxes),
        "max_width_diff": round(max_width_diff, 2),
        "max_height_diff": round(max_height_diff, 2),
        "min_ratio": round(min(ratios), 4),
        "max_ratio": round(max(ratios), 4),
        "min_height": round(min_height, 2),
        "states": sorted(by_state.keys()),
        "states_uniform": states_uniform,
        "ok": ok,
    }


def check_title_length_does_not_affect_poster_size(page, container_selector: str) -> dict:
    """y1-cards §2.3 item 5: force the first card's title to a long string
    (clamped to 2 lines by ``.card-title``) and confirm its ``.poster``
    box's width/height are unaffected -- the poster's size comes from CSS
    ``aspect-ratio`` alone, never from sibling content."""
    return page.evaluate(
        """
        (sel) => {
          const card = document.querySelector(sel + ' .media-card');
          const poster = card.querySelector('.poster');
          const title = card.querySelector('.card-title');
          const before = poster.getBoundingClientRect();
          const originalText = title.textContent;
          title.textContent = '一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十';
          const after = poster.getBoundingClientRect();
          title.textContent = originalText;
          return {
            before: { width: before.width, height: before.height },
            after: { width: after.width, height: after.height },
          };
        }
        """,
        container_selector,
    )


def check_focus_does_not_change_poster_size(page) -> dict:
    """y1-cards §2.3 item 6: Tab from ``#libraryQuery`` onto the first
    reachable ``.media-card`` (the same traversal proven in
    ``run_keyboard_walkthrough``) and return that card's ``.poster`` box
    size -- the hover/focus lift (``transform:translateY(-2px)``,
    static/app.css) is a paint-only transform and must never change the
    box's width/height. Caller compares the result against the
    container's own uniform size."""
    page.evaluate("document.getElementById('libraryQuery').focus()")
    for _ in range(60):
        page.keyboard.press("Tab")
        landed = page.evaluate(
            "() => !!(document.activeElement && document.activeElement.classList "
            "&& document.activeElement.classList.contains('media-card'))"
        )
        if landed:
            break
    else:
        raise RuntimeError("Tab traversal never reached a .media-card")
    return page.evaluate(
        "() => { const r = document.activeElement.querySelector('.poster').getBoundingClientRect(); "
        "return { width: r.width, height: r.height }; }"
    )


def geometry_violations_for(key: str, analysis: dict) -> list[str]:
    """``analyze_poster_geometry``'s ``ok``/details, turned into zero or one
    human-readable violation string prefixed with ``key`` (e.g.
    ``"1440x900:home"``) -- shared by every geometry-check call site so
    the report's ``violations`` list always reads the same way."""
    return [] if analysis["ok"] else [f"{key}: geometry failed: {analysis}"]


# ---------------------------------------------------------------------------
# Horizontal-overflow check + renderer-crash guard.
# ---------------------------------------------------------------------------


class RendererCrashed(RuntimeError):
    """Raised when Chromium's renderer process crashes mid-capture."""


def _check_overflow(page, size_label: str, page_name: str) -> dict:
    scroll_width, inner_width = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
    return {
        "size": size_label,
        "page": page_name,
        "scroll_width": scroll_width,
        "inner_width": inner_width,
        "ok": scroll_width <= inner_width,
    }


@contextmanager
def _crash_guard(page, size_label: str, page_name: str) -> Iterator[None]:
    """Around one page/size capture step: if the renderer crashes, raise
    ``RendererCrashed`` naming the exact page/size instead of letting a
    generic Playwright timeout/target-closed error obscure the cause."""
    crashed = {"flag": False}

    def _on_crash(_page) -> None:
        crashed["flag"] = True

    page.on("crash", _on_crash)
    try:
        yield
    except Exception as exc:
        if crashed["flag"]:
            raise RendererCrashed(f"chromium renderer crashed at size={size_label!r} page={page_name!r}") from exc
        raise RuntimeError(f"failed at size={size_label!r} page={page_name!r}: {exc}") from exc
    finally:
        page.remove_listener("crash", _on_crash)


# ---------------------------------------------------------------------------
# Temp environment + app subprocess.
# ---------------------------------------------------------------------------


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_strm_fixtures(strm_root: Path) -> None:
    """A couple of folders/.strm files -- enough to exercise "browse into a
    subfolder and see a .strm file" (RE0 spec Sec 19 item 8). Content is a
    placeholder comment, never a real media path."""
    movies = strm_root / "电影"
    movies.mkdir(parents=True, exist_ok=True)
    (movies / "虚构电影一.2020.2160p.strm").write_text("# UI screenshot fixture, not a real media path\n", encoding="utf-8")
    show = strm_root / "剧集" / "虚构剧集一"
    show.mkdir(parents=True, exist_ok=True)
    (show / "S01E01.strm").write_text("# UI screenshot fixture, not a real media path\n", encoding="utf-8")


def _seed_ratings_for_screenshots(store: library_store.LibraryStore) -> None:
    """T18 §14.4: give this run's OWN synthetic bundle a three-source, a
    one-source, and a no-data (left at the fixture's own default) ratings
    example, so detail.png actually shows the RE0-style ratings row and
    the card primary_rating badge -- never modifies
    tests/fixtures/library/build_installed.py's shared fixture (several
    pytest tests assert media #1's ratings default to empty/pending)."""
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE media SET imdb_id=?, ratings_json=?, ratings_status='complete' WHERE media_identity=?",
            (
                "tt0137523",
                json.dumps({
                    "tmdb": {"score": 8.7, "votes": 31198},
                    "imdb": {"score": 9.3, "votes": 3200000},
                    "tvmaze": {"score": 8.1, "votes": None},
                }),
                "tmdb:movie:900001",
            ),
        )
        conn.execute(
            "UPDATE media SET ratings_json=?, ratings_status='partial' WHERE media_identity=?",
            (json.dumps({"tmdb": {"score": 6.4, "votes": 812}}), "fp:movie:900002"),
        )
        conn.commit()
    finally:
        conn.close()


# I3 (§14.6): overflow coverage the shared build_installed.py fixture never
# exercises -- a title/remark long enough (with no natural break point) to
# prove text wraps/ellipses instead of clipping or forcing horizontal
# scroll, a group with one live + one deleted link of the SAME provider
# (the "deleted row" case), and a group whose links are all deleted (the
# "empty" case). Set by _seed_overflow_media_for_screenshots below, read by
# _capture_detail_overflow -- deterministic within a single run (one
# synthetic bundle is built per process), never a hardcoded row id.
_overflow_media_id: str | None = None

_OVERFLOW_TITLE = "极限测试标题文字" * 10  # 80 CJK chars, no natural break point
_OVERFLOW_REMARK = "超长备注无空格文字用于溢出测试" * 8  # 120 chars, no spaces


def _seed_overflow_media_for_screenshots(store: library_store.LibraryStore) -> None:
    """Adds one media (its own identity, never touching the shared
    build_installed.py fixture's six) with a very long title, a group with
    one live + one deleted link of the same provider, and a group whose
    links are all deleted. Sets module-level ``_overflow_media_id``."""
    global _overflow_media_id
    media_id = store.upsert_media(
        library_store.MediaRecord(
            media_identity="fp:movie:900099", media_type="movie", title_zh=_OVERFLOW_TITLE,
            search_key=_OVERFLOW_TITLE, year=2023, match_status="unmatched",
        )
    )
    mixed_group_id = store.upsert_group(
        library_store.GroupRecord(
            media_id=media_id, edition_fingerprint="fp-overflow-mixed",
            display_title="1080p WEB-DL", quality="1080p",
        )
    )
    all_deleted_group_id = store.upsert_group(
        library_store.GroupRecord(
            media_id=media_id, edition_fingerprint="fp-overflow-all-deleted",
            display_title="720p HDTV", quality="720p",
        )
    )
    now = int(time.time())
    link_specs = [
        # (group_id, provider, url, deleted, remark)
        (mixed_group_id, "115", "https://115.com/s/swfake990001", False, _OVERFLOW_REMARK),
        (mixed_group_id, "115", "https://115.com/s/swfake990002", True, ""),
        # A second, distinct live provider on the mixed group -- without
        # this, the all-deleted group below contributes nothing to
        # provider_facets (its only link is deleted, so its own
        # `providers` dict is empty), leaving media-wide provider_facets
        # with just one entry ("115") and triggering the pre-existing
        # §3.3 single-provider auto-select, which would then hide the
        # all-deleted group entirely from the default (全部) view.
        (mixed_group_id, "quark", "https://pan.quark.cn/s/swfake990003", False, ""),
        (all_deleted_group_id, "alipan", "https://www.alipan.com/s/swfake990004", True, ""),
    ]
    for index, (group_id, provider, url, deleted, remark) in enumerate(link_specs, start=1):
        store.upsert_link(
            library_store.LinkRecord(
                public_id=f"pub-overflow-{index:02d}",
                group_id=group_id,
                provider=provider,
                canonical_url_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                url_label=f"{provider} 分享 · overflow{index}",
                url_plain=url,
                remark=remark,
                created_at_source=now - index * 3600,
                deleted_at_source=now - 60 if deleted else None,
            )
        )
    store.recount()
    _overflow_media_id = str(media_id)


# T21 §8.4: media ids for the 4 different-ratio backdrop fixtures (keyed by
# BACKDROP_FIXTURE_RATIOS' own keys) plus one more entry, "fallback", for
# the no-backdrop-at-all case -- set by
# _seed_backdrop_fixtures_for_screenshots below, read by
# _capture_detail_backdrops.
_backdrop_fixture_media_ids: dict[str, str] = {}

_BACKDROP_FIXTURE_TITLES: dict[str, str] = {
    "16x9": "背景测试 16:9",
    "4x3": "背景测试 4:3",
    "239x100": "背景测试 2.39:1",
    "1x1": "背景测试 1:1",
}


def _seed_backdrop_fixtures_for_screenshots(store: library_store.LibraryStore) -> None:
    """T21 §8.4: one media per ``BACKDROP_FIXTURE_RATIOS`` key, each
    pointed (via ``backdrop_path``, exactly like every other media in this
    fixture bundle -- app.py's ``_tmdb_image_url()`` is untouched) at this
    run's own locally-generated, distinctly-bordered/ratioed PNG (see
    ``install_network_guard``) -- one live "115" link each so the detail
    page's existing ``.link-row`` wait-selector pattern still applies.
    Sets module-level ``_backdrop_fixture_media_ids``, keyed by ratio plus
    one more entry, "fallback", which reuses (idempotent re-upsert with
    identical field values -- never modifies) the shared fixture's own
    ``fp:movie:900002`` (build_installed.py already gives it neither
    ``poster_path`` nor ``backdrop_path``), so the "no backdrop at all"
    case doesn't need a fifth new media row."""
    global _backdrop_fixture_media_ids
    _backdrop_fixture_media_ids = {}
    now = int(time.time())
    for index, (key, title) in enumerate(_BACKDROP_FIXTURE_TITLES.items(), start=1):
        media_id = store.upsert_media(
            library_store.MediaRecord(
                media_identity=f"fp:movie:90030{index}", media_type="movie", title_zh=title,
                search_key=title, year=2024, match_status="unmatched",
                backdrop_path=backdrop_fixture_path(key),
            )
        )
        group_id = store.upsert_group(
            library_store.GroupRecord(
                media_id=media_id, edition_fingerprint=f"fp-backdrop-{key}",
                display_title="1080p WEB-DL", quality="1080p",
            )
        )
        url = f"https://115.com/s/swfake9903{index:02d}"
        store.upsert_link(
            library_store.LinkRecord(
                public_id=f"pub-backdrop-{index:02d}",
                group_id=group_id,
                provider="115",
                canonical_url_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                url_label=f"115 分享 · backdrop{index}",
                url_plain=url,
                created_at_source=now - index * 3600,
            )
        )
        _backdrop_fixture_media_ids[key] = str(media_id)
    fallback_media_id = store.upsert_media(
        library_store.MediaRecord(
            media_identity="fp:movie:900002", media_type="movie", title_zh="虚构电影二",
            search_key="虚构电影二", year=2019, match_status="unmatched",
        )
    )
    _backdrop_fixture_media_ids["fallback"] = str(fallback_media_id)
    store.recount()


# w6-contract §UI item 5: the link-check columns (check_status/check_reason/
# checked_at/invalid on link objects, all_links_invalid on search items) are
# being added by the sibling w6-checker worktree and are NOT present in this
# worktree's own app.py yet (see .superpowers/sdd/briefs/w6-contract.md) --
# these two media are seeded normally through LibraryStore exactly like
# every other screenshot fixture, then _install_linkcheck_route_stub below
# patches only their own JSON responses in the browser's OWN network layer
# (Playwright route interception, never a backend change) so every
# contract-defined link state can be screenshotted against the real,
# unmocked front-end code. Sets the three module-level globals read by
# _install_linkcheck_route_stub and _capture_and_check_linkcheck.
_linkcheck_media_id: str | None = None
_linkcheck_all_invalid_media_id: str | None = None
_linkcheck_link_ids: dict[str, str] = {}
_LINKCHECK_ALL_INVALID_TITLE = "链接检测全部失效样本"


def _seed_linkcheck_fixtures_for_screenshots(store: library_store.LibraryStore) -> None:
    """One mixed-state media (a plain 115 link, a quark link the route stub
    marks ``invalid``, a tianyicloud link it marks ``unknown``) plus one
    media whose only link the stub marks invalid (``all_links_invalid``).
    Writes the verdict rows into the real ``link_check`` table (created by
    the store's own migration) and recounts afterwards, exactly as the
    checker does after a round."""
    global _linkcheck_media_id, _linkcheck_all_invalid_media_id, _linkcheck_link_ids
    now = int(time.time())

    # The store's own write-side migration (LibraryStore.connect(readonly=
    # False) -> _ensure_link_check_tables) creates link_check/link_check_state
    # with the real schema -- no fixture-local DDL, so this seed can never
    # drift from the backend.
    store.connect().close()

    media_id = store.upsert_media(
        library_store.MediaRecord(
            media_identity="fp:movie:900100", media_type="movie", title_zh="链接检测示例",
            search_key="链接检测示例", year=2022, match_status="unmatched",
        )
    )
    group_id = store.upsert_group(
        library_store.GroupRecord(
            media_id=media_id, edition_fingerprint="fp-linkcheck-mixed",
            display_title="1080p WEB-DL", quality="1080p",
        )
    )
    _linkcheck_link_ids = {}
    checked_at_epoch = now - 3 * 3600
    link_check_rows = []
    for index, (provider, url, status, reason) in enumerate(
        [
            ("115", "https://115.com/s/swfake991001", None, None),
            ("quark", "https://pan.quark.cn/s/swfake991002", "invalid", "share_expired"),
            ("tianyicloud", "https://cloud.189.cn/t/swfake991003", "unknown", "anti_bot"),
        ],
        start=1,
    ):
        public_id = f"pub-linkcheck-{index:02d}"
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        store.upsert_link(
            library_store.LinkRecord(
                public_id=public_id, group_id=group_id, provider=provider,
                canonical_url_hash=url_hash,
                url_label=f"{provider} 分享 · linkcheck{index}", url_plain=url,
                created_at_source=now - index * 3600,
            )
        )
        _linkcheck_link_ids[provider] = public_id
        if status:
            link_check_rows.append((provider, url_hash, status, reason, checked_at_epoch))
    _linkcheck_media_id = str(media_id)

    all_invalid_media_id = store.upsert_media(
        library_store.MediaRecord(
            media_identity="fp:movie:900101", media_type="movie", title_zh=_LINKCHECK_ALL_INVALID_TITLE,
            search_key=_LINKCHECK_ALL_INVALID_TITLE, year=2022, match_status="unmatched",
            poster_path="/poster-linkcheck.jpg",
        )
    )
    all_invalid_group_id = store.upsert_group(
        library_store.GroupRecord(
            media_id=all_invalid_media_id, edition_fingerprint="fp-linkcheck-all-invalid",
            display_title="1080p WEB-DL", quality="1080p",
        )
    )
    all_invalid_url = "https://115.com/s/swfake991004"
    all_invalid_public_id = "pub-linkcheck-04"
    all_invalid_hash = hashlib.sha256(all_invalid_url.encode("utf-8")).hexdigest()
    store.upsert_link(
        library_store.LinkRecord(
            public_id=all_invalid_public_id, group_id=all_invalid_group_id, provider="115",
            canonical_url_hash=all_invalid_hash,
            url_label="115 分享 · linkcheck04", url_plain=all_invalid_url,
            created_at_source=now - 3600,
        )
    )
    _linkcheck_link_ids["all_invalid"] = all_invalid_public_id
    link_check_rows.append(("115", all_invalid_hash, "invalid", "file_deleted", checked_at_epoch))
    _linkcheck_all_invalid_media_id = str(all_invalid_media_id)

    conn = store.connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO link_check (provider, canonical_url_hash, status, reason, checked_at) VALUES (?, ?, ?, ?, ?)",
            link_check_rows,
        )
        conn.commit()
    finally:
        conn.close()
    # Live counts (media/group link_count, has_115, all_links_invalid) only
    # move on a recount -- the checker recounts affected groups after each
    # round, and the importer recounts after a build; the seed does the same
    # AFTER the verdict rows exist.
    store.recount()


def build_temp_environment(base_dir: Path) -> tuple[dict[str, str], Path]:
    """Create the temp data dir / master key / STRM root / plaintext bundle
    this run needs, and return (subprocess env, bundle_path). Nothing here
    touches a production path."""
    from cryptography.fernet import Fernet

    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = base_dir / "master.key"
    key_file.write_bytes(Fernet.generate_key())
    strm_root = base_dir / "strm"
    strm_root.mkdir(parents=True, exist_ok=True)
    _write_strm_fixtures(strm_root)

    bundle_path = base_dir / "library-bundle.sqlite"
    bundle_store = library_store.LibraryStore(bundle_path)
    bundle_store.create_schema()
    build_synthetic_library(bundle_store)
    _seed_ratings_for_screenshots(bundle_store)
    _seed_overflow_media_for_screenshots(bundle_store)
    _seed_backdrop_fixtures_for_screenshots(bundle_store)
    _seed_linkcheck_fixtures_for_screenshots(bundle_store)

    env = dict(os.environ)
    for name in _SECRET_ENV_NAMES:
        env.pop(name, None)
    env.update(
        {
            "HIDRIVE_AUTH_MODE": "local",
            "HIDRIVE_DATA_DIR": str(data_dir),
            "HIDRIVE_MASTER_KEY_FILE": str(key_file),
            "STRM_ROOT": str(strm_root),
            "HIDRIVE_BIND": "127.0.0.1",
            "HIDRIVE_PORT": str(find_free_port()),
            "LIBRARY_ENRICH_AUTOSTART": "0",
            "HIDRIVE_PUBLIC_ORIGIN": "https://hidrive.test",
            # A closed local port: the OpenList/STRM browser fetches it
            # unconditionally on load. This keeps that fetch loopback-only
            # (never real network egress) and failing fast, so the OpenList
            # page's real "backend unavailable" error state gets screenshotted
            # -- there is no real OpenList server in this environment.
            "OPENLIST_URL": f"http://127.0.0.1:{find_free_port()}",
            "OPENLIST_DB": str(base_dir / "no-openlist-here.db"),
            # This container sets HTTP_PROXY/HTTPS_PROXY/ALL_PROXY, which
            # `requests` (trust_env=True by default) would otherwise use even
            # for the loopback OPENLIST_URL above -- turning a "fails fast,
            # never leaves the host" local refused-connection into an actual
            # proxied request. Excluding 127.0.0.1/localhost keeps it local.
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return env, bundle_path


def install_library(env: dict[str, str], bundle_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "app.py", "--library-install", str(bundle_path)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"--library-install produced no JSON (exit {proc.returncode}): {proc.stdout!r} {proc.stderr!r}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"--library-install failed: {result}")


# This container sets HTTP_PROXY/HTTPS_PROXY/ALL_PROXY; urllib.request
# honours those by default even for 127.0.0.1, which turns polling our own
# freshly-started subprocess into a proxied request that 503s. Bypass the
# proxy explicitly rather than mutating this process's own os.environ.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_for_healthy(base_url: str, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"app.py exited early (code {proc.returncode}) before becoming healthy:\n{out}")
        try:
            with _NO_PROXY_OPENER.open(f"{base_url}/healthz", timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"app.py did not become healthy within {timeout}s")


@contextmanager
def running_app() -> Iterator[str]:
    """Build the temp environment, install the synthetic library, start
    app.py, yield its base URL, and always tear the subprocess + temp
    directory down again."""
    PYTEST_TMP.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ui-screenshots-", dir=str(PYTEST_TMP)) as base_dir_str:
        base_dir = Path(base_dir_str)
        env, bundle_path = build_temp_environment(base_dir)
        install_library(env, bundle_path)

        # The app's access log goes to a file, never to an unread PIPE: a
        # pipe nobody drains fills after ~64 KiB of Werkzeug request lines
        # (a few dozen navigations), the server then blocks on write and
        # every later page.goto() times out -- always at whichever
        # breakpoint happens to run last. The file also gives a readable
        # server log when a capture does fail.
        app_log = open(base_dir / "app-server.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=str(ROOT),
            env=env,
            stdout=app_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base_url = f"http://127.0.0.1:{env['HIDRIVE_PORT']}"
        try:
            wait_for_healthy(base_url, proc)
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            app_log.close()


# ---------------------------------------------------------------------------
# Screenshot capture (one page.goto()-rooted step per page state, so any
# subset of ALL_PAGES can be captured independently).
# ---------------------------------------------------------------------------


def _full_page_screenshot(page, path: Path) -> None:
    # A prior step (e.g. clicking a below-the-fold link-transfer button)
    # can leave the page scrolled -- Chromium's full-page screenshot then
    # renders the (position:sticky) top bar at its scrolled "stuck" state
    # while the rest of the document is captured from real document
    # coordinates, producing a ghosted double-header. Reset scroll first so
    # every screenshot is taken from a consistent, correct top-of-page state.
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(path), full_page=True)


# Follow-up (deterministic waits, replacing the previous
# wait_until="networkidle" + wait_for_timeout() approach): each of the four
# home rails (static/app.js's LIBRARY_RAILS) fetches and renders
# independently. Waiting for a single "any card visible anywhere under
# #libraryRails" condition can move on while 1-3 other rails are still
# mid-fetch, producing a non-deterministic screenshot (some rails
# populated, others not yet). Wait for each rail individually to reach its
# settled state instead -- populated with cards, or left hidden (a rail's
# query legitimately returning zero items keeps it hidden forever,
# mirroring loadRails()'s own logic) -- before treating the home view as
# ready for capture or interaction. The bounded per-action timeout
# (install_network_guard's set_default_timeout) is the safety net if a
# rail never settles at all.
_HOME_RAIL_KEYS = ("today", "year_desc", "movie", "tv")


def _wait_for_home_rails(page) -> None:
    for key in _HOME_RAIL_KEYS:
        page.wait_for_selector(f'.rail[data-rail="{key}"] .media-card, .rail[data-rail="{key}"][hidden]')


def _capture_home(page, base_url: str, out_dir: Path) -> None:
    # No active query/filter -- views.library.isBrowsing() is true, so the
    # home view is the hero + content rails (#libraryRails), never the
    # #libraryResults grid (that only appears once a search/filter is
    # active -- see _capture_search below).
    page.goto(f"{base_url}/?tab=library", wait_until="load")
    _wait_for_home_rails(page)
    _full_page_screenshot(page, out_dir / "home.png")


def _capture_search(page, base_url: str, out_dir: Path) -> None:
    page.goto(f"{base_url}/?tab=library", wait_until="load")
    _wait_for_home_rails(page)
    page.fill("#libraryQuery", "虚构")
    page.click("#librarySearch")
    page.wait_for_selector("#libraryResults .media-card", state="visible")
    # The suggest fetch is debounced 200ms from the fill() above,
    # independently of the search click's own hideSuggest() call -- wait
    # out the debounce window so that response (which can re-show
    # #librarySuggest) has already landed before we close it, or it could
    # still be pending when the screenshot is taken.
    page.wait_for_timeout(260)
    # Close it explicitly by dispatching a click on <body> (the document
    # click listener hides #librarySuggest for any click outside
    # .search-field -- calling .click() in-page avoids Playwright hit-
    # testing a real coordinate, which could land on a result card instead)
    # and blurring the input. T8 #2: never drive this via a keyboard key
    # press any more -- a type="search" input clears its own value on one
    # particular key in Chromium, which used to blank the query text out
    # of search.png before app.js's #libraryQuery keydown handler was
    # fixed to preventDefault() it.
    page.evaluate("document.body.click()")
    page.evaluate("document.activeElement && document.activeElement.blur()")
    _full_page_screenshot(page, out_dir / "search.png")


def _capture_filters(page, base_url: str, out_dir: Path) -> None:
    page.goto(f"{base_url}/?tab=library", wait_until="load")
    _wait_for_home_rails(page)
    page.click("#libraryFiltersToggle")
    page.wait_for_selector("#libraryFilters:not([hidden])", state="visible")
    page.wait_for_timeout(120)  # let positionFilters()/scrim settle
    # T16 §6: the popover is `position:fixed` -- Chromium's full-page
    # screenshot temporarily resizes the layout viewport to the full
    # scroll height, which mis-places fixed-position content, so this
    # capture (unlike every other page here) is viewport-only -- exactly
    # what a real visitor sees on screen.
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(out_dir / "filters.png"))


def _capture_detail(page, base_url: str, out_dir: Path) -> None:
    # T18 §13: every group's links render immediately -- no expand click.
    page.goto(f"{base_url}/?tab=library&media=1", wait_until="load")
    page.wait_for_selector("#libraryDetail:not([hidden]) .link-row", state="visible")
    _full_page_screenshot(page, out_dir / "detail.png")


def _capture_detail_overflow(page, base_url: str, out_dir: Path) -> None:
    """I3 (§14.6): the same detail page, pointed at the long-title/long-
    remark/deleted-row/all-deleted-group media seeded by
    _seed_overflow_media_for_screenshots -- proves nothing clips and
    nothing forces horizontal scroll under realistic worst-case text/data,
    at every one of the six breakpoints (unlike the two-size reauth
    capture below, this one IS part of the full SIZES matrix)."""
    assert _overflow_media_id, "_seed_overflow_media_for_screenshots must run before this capture"
    page.goto(f"{base_url}/?tab=library&media={_overflow_media_id}", wait_until="load")
    page.wait_for_selector("#libraryDetail:not([hidden]) .link-row", state="visible")
    _full_page_screenshot(page, out_dir / "detail-overflow.png")


def _capture_transfer(page, base_url: str, out_dir: Path) -> None:
    # media=1 ("虚构电影一") always has a live 115 link, so its group always
    # renders a "转存到 115" (class link-transfer, data-link-action="transfer")
    # button -- see tests/fixtures/library/build_installed.py.
    page.goto(f"{base_url}/?tab=library&media=1", wait_until="load")
    page.wait_for_selector("#libraryDetail:not([hidden]) .link-row", state="visible")
    page.click(".link-transfer")
    page.wait_for_selector("#transferDialog:not([hidden])")
    page.wait_for_timeout(300)
    _full_page_screenshot(page, out_dir / "transfer.png")


def _capture_settings(page, base_url: str, out_dir: Path) -> None:
    page.goto(f"{base_url}/?tab=settings", wait_until="load")
    page.wait_for_selector("#settings:not([hidden])")
    # refreshLibraryStatus() (static/app.js bootstrap init()) is the last
    # async step to settle on this page -- it always ends by writing either
    # real <p> lines (renderTmdbScrapeStatus) or a ".empty" diagnostic
    # (its .catch() path) into #tmdbScrapeStatus, so waiting on either is a
    # real-content signal instead of a fixed guess at how long that takes.
    page.wait_for_selector("#tmdbScrapeStatus p, #tmdbScrapeStatus .empty")
    _full_page_screenshot(page, out_dir / "settings.png")


# T19: the "重新授权 115" QR dialog, captured at only two of the six
# breakpoints (brief §8.2) -- deliberately NOT part of ALL_PAGES/SIZES
# (that six-size-by-seven-page matrix is unchanged), so it's driven by its
# own capture step below instead of _CAPTURE_STEPS.
REAUTH_CAPTURE_SIZES: tuple[str, ...] = ("1440x900", "390x844")
_REAUTH_FIXTURE_CHALLENGE_ID = "screenshot-reauth-fixture-not-real"


def _install_reauth_mock(page) -> None:
    """Fulfils the three /api/115/reauth/* endpoints that make a
    server-side outbound call to 115 (start/qr/status) directly at the
    browser level, so opening the QR dialog during a screenshot never
    reaches the real subprocess's own real-115 network calls -- those
    aren't visible to install_network_guard's page.route interception at
    all (they happen in the Python subprocess, not the browser). Never a
    real QR/uid/cookie value -- see docs/115-integration.md."""

    def handle(route):
        url = route.request.url
        if "/api/115/reauth/start" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "success": True,
                        "challenge_id": _REAUTH_FIXTURE_CHALLENGE_ID,
                        "expires_at": "2099-01-01T00:00:00+00:00",
                        "qr_url": f"/api/115/reauth/qr?challenge_id={_REAUTH_FIXTURE_CHALLENGE_ID}",
                    }
                ),
            )
        elif "/api/115/reauth/qr" in url:
            route.fulfill(status=200, content_type="image/png", body=_PLACEHOLDER_PNG)
        elif "/api/115/reauth/status" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "state": "pending", "expires_at": "2099-01-01T00:00:00+00:00"}),
            )
        else:
            # e.g. a cancel call on teardown -- harmless to let the real
            # (mock-data-free) subprocess 404 it; it never calls out to 115.
            route.continue_()

    page.route("**/api/115/reauth/**", handle)


def _capture_reauth(page, base_url: str, out_dir: Path) -> None:
    _install_reauth_mock(page)
    page.goto(f"{base_url}/?tab=settings", wait_until="networkidle")
    page.wait_for_selector("#settings:not([hidden])")
    page.wait_for_selector("#reauth115Btn:not([hidden])", state="visible")
    page.click("#reauth115Btn")
    page.wait_for_selector("#reauthDialog:not([hidden])")
    page.wait_for_selector("#reauthQrImage[src]")
    page.wait_for_timeout(300)
    _full_page_screenshot(page, out_dir / "reauth.png")
    page.unroute("**/api/115/reauth/**")


def _capture_openlist(page, base_url: str, out_dir: Path) -> None:
    page.goto(f"{base_url}/?tab=openlist", wait_until="load")
    page.wait_for_selector("#openlist:not([hidden])")
    # OPENLIST_URL is a closed local port (module docstring) -- the fetch
    # always fails fast, so #openResult always settles into the real
    # "backend unavailable" error state (setError()'s ".error" class,
    # never the template's plain ".empty" placeholder/loading text).
    page.wait_for_selector("#openResult .file-row, #openResult .empty.error")
    _full_page_screenshot(page, out_dir / "openlist.png")


def _capture_strm(page, base_url: str, out_dir: Path) -> None:
    page.goto(f"{base_url}/?tab=strm", wait_until="load")
    page.wait_for_selector("#strm:not([hidden])")
    # The STRM fixture root always has real folders (_write_strm_fixtures)
    # -- #strmResult settles into real .file-row rows, never the
    # template's plain ".empty" placeholder/loading text.
    page.wait_for_selector("#strmResult .file-row, #strmResult .empty.error")
    _full_page_screenshot(page, out_dir / "strm.png")


_CAPTURE_STEPS = {
    "home": _capture_home,
    "search": _capture_search,
    "filters": _capture_filters,
    "detail": _capture_detail,
    "transfer": _capture_transfer,
    "settings": _capture_settings,
    "openlist": _capture_openlist,
    "strm": _capture_strm,
}


# y1-cards §2.3: which container each capture step's page state exposes its
# poster boxes through, so capture_pages can measure geometry on a page it's
# already navigated to for a screenshot -- no extra navigation needed.
_GEOMETRY_CONTAINER_BY_PAGE = {"home": "#libraryRails", "search": "#libraryResults"}

# T16 §6.3: a real control inside the popover, used for the
# elementFromPoint "is the panel actually clickable, not buried under the
# poster wall" check -- #filterYear is present regardless of viewport/
# facet data (unlike the chip-toggle groups, which are only populated once
# /api/library/filters resolves).
_FILTERS_PROBE_CONTROL = "#filterYear"


def check_filters_popover(page) -> dict:
    """T16 §6.3 acceptance, run with ``#libraryFilters`` already open (see
    ``_capture_filters``): the panel is fully inside the viewport, sits
    above the poster wall (a real pointer hit-test on one of its controls
    lands on that control, not a card underneath), Escape closes it and
    returns focus to the toggle button, and -- only meaningful at the
    <=650px mobile breakpoint -- body scroll is locked behind it."""
    rect = page.eval_on_selector(
        "#libraryFilters",
        "el => { const r = el.getBoundingClientRect(); return {left: r.left, top: r.top, right: r.right, bottom: r.bottom}; }",
    )
    viewport = page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")
    fully_in_viewport = (
        rect["left"] >= -0.5 and rect["top"] >= -0.5
        and rect["right"] <= viewport["width"] + 0.5 and rect["bottom"] <= viewport["height"] + 0.5
    )

    control_is_hit = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return !!(hit && hit.closest(sel));
        }""",
        _FILTERS_PROBE_CONTROL,
    )

    body_scroll_locked = page.evaluate("() => getComputedStyle(document.body).overflow === 'hidden'")

    page.keyboard.press("Escape")
    page.wait_for_selector("#libraryFilters[hidden]", state="attached")
    escape_closes = page.eval_on_selector("#libraryFilters", "el => el.hidden")
    focus_returns_to_toggle = page.evaluate(
        "() => document.activeElement && document.activeElement.id === 'libraryFiltersToggle'"
    )

    return {
        "fully_in_viewport": bool(fully_in_viewport),
        "control_hit_by_element_from_point": bool(control_is_hit),
        "mobile_body_scroll_locked": bool(body_scroll_locked),
        "escape_closes": bool(escape_closes),
        "escape_returns_focus_to_toggle": bool(focus_returns_to_toggle),
    }


def filters_popover_violations(results: dict[str, dict]) -> list[str]:
    """T16 §6.3 acceptance across every captured viewport: fully in
    viewport, above the poster wall, Escape closes + returns focus
    everywhere; body scroll locked only at the <=650px mobile breakpoint
    (never on desktop/tablet, where the poster wall stays visible/scrollable
    behind a light scrim)."""
    violations: list[str] = []
    for size_label, entry in results.items():
        width = int(size_label.split("x", 1)[0])
        is_mobile = width <= 650
        for key in ("fully_in_viewport", "control_hit_by_element_from_point", "escape_closes", "escape_returns_focus_to_toggle"):
            if not entry.get(key):
                violations.append(f"{size_label}: {key} failed")
        locked = entry.get("mobile_body_scroll_locked")
        if is_mobile and not locked:
            violations.append(f"{size_label}: expected body scroll locked at mobile breakpoint")
        elif not is_mobile and locked:
            violations.append(f"{size_label}: body scroll unexpectedly locked above the mobile breakpoint")
    return violations


# Follow-up (deterministic waits, round 2): one bounded retry per
# page/size capture. A Playwright TimeoutError out of ``page.goto`` or a
# readiness wait is exactly the documented host-contention failure mode
# (see the report's "known environment flakiness" section) -- not a bug
# in the page under test -- so it is worth exactly one retry against a
# throwaway-fresh context before giving up. Anything else (an assertion,
# a real app error) is never retried.
_CAPTURE_RETRY_DELAY_S = 5.0


def _run_capture_step(
    page,
    base_url: str,
    out_dir: Path,
    name: str,
    size_label: str,
    *,
    browser,
    viewport: dict[str, int] | None,
    retry_counts: dict[str, int] | None,
):
    """Run one capture step; on a Playwright TimeoutError, close the
    current context, wait 5s, open a fresh context at the same viewport,
    and retry the capture exactly once -- recording the outcome in
    ``retry_counts[f"{size_label}:{name}"]`` (0 or 1) for report.json. A
    second failure is never caught here -- it propagates to
    ``capture_pages``'s ``_crash_guard``, which still fails the run
    loudly (never silently skipped). Returns the page actually used for
    this step (unchanged, unless a retry replaced it) so the caller keeps
    using the live context for subsequent steps."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    key = f"{size_label}:{name}"
    try:
        _CAPTURE_STEPS[name](page, base_url, out_dir)
    except PlaywrightTimeoutError:
        if browser is None or viewport is None:
            raise  # caller didn't opt into retries (e.g. the smoke test)
        print(f"retrying {key} after a Playwright TimeoutError...")
        page.context.close()
        time.sleep(_CAPTURE_RETRY_DELAY_S)
        context = browser.new_context(viewport=viewport)
        page = context.new_page()
        install_network_guard(page, base_url)
        _CAPTURE_STEPS[name](page, base_url, out_dir)
        if retry_counts is not None:
            retry_counts[key] = 1
        return page
    if retry_counts is not None:
        retry_counts[key] = 0
    return page


def capture_pages(
    page,
    base_url: str,
    out_dir: Path,
    pages: Sequence[str] = ALL_PAGES,
    *,
    size_label: str = "unspecified",
    overflow_results: list[dict] | None = None,
    geometry_results: dict[str, dict] | None = None,
    filters_results: dict[str, dict] | None = None,
    browser=None,
    viewport: dict[str, int] | None = None,
    retry_counts: dict[str, int] | None = None,
):
    """Returns the page actually used for the last capture step -- pass
    ``browser``/``viewport`` (the per-size browser process and its
    viewport dict) to enable the one-retry-on-timeout behaviour above;
    without them (e.g. the lighter smoke test) a timeout just raises, as
    before."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in pages:
        with _crash_guard(page, size_label, name):
            page = _run_capture_step(
                page, base_url, out_dir, name, size_label,
                browser=browser, viewport=viewport, retry_counts=retry_counts,
            )
            if overflow_results is not None:
                overflow_results.append(_check_overflow(page, size_label, name))
            # y1-cards §2.3: piggyback the poster-geometry acceptance
            # measurement onto the home/search screenshot's own navigation
            # (already loaded, cards already visible) instead of a
            # dedicated extra page load per viewport.
            container = _GEOMETRY_CONTAINER_BY_PAGE.get(name)
            if geometry_results is not None and container is not None:
                boxes = collect_poster_boxes(page, container)
                geometry_results[f"{size_label}:{name}"] = {"analysis": analyze_poster_geometry(boxes)}
            # T16 §6.3: same piggyback pattern -- the filters popover is
            # already open from _capture_filters's own screenshot step.
            if filters_results is not None and name == "filters":
                filters_results[size_label] = check_filters_popover(page)
    return page


# ---------------------------------------------------------------------------
# Keyboard walkthrough + contrast spot-check (run once, on a representative
# desktop viewport -- these check behaviour/color tokens, not per-breakpoint
# layout, which the screenshots themselves cover).
# ---------------------------------------------------------------------------

_FOCUS_PROBE = """
() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  if (el.id) return el.id;
  if (el.closest('#libraryType')) return 'segmented:' + (el.dataset.value || '');
  if (el.classList && el.classList.contains('media-card')) return 'media-card:' + el.dataset.mediaId;
  return el.tagName.toLowerCase();
}
"""


def run_keyboard_walkthrough(page, max_tab_presses: int = 60) -> dict:
    """Caller must already have navigated ``page`` to ``?tab=library`` with
    the browse-mode home view loaded (``#libraryRails .media-card``
    visible) -- this function renavigates to fresh states of its own
    between steps 1-4."""

    # 1. Tab order: search box -> segmented control -> search button -> a
    # result card (relative order, not necessarily adjacent -- other
    # focusable filter controls sit between them in the DOM).
    page.evaluate("document.getElementById('libraryQuery').focus()")
    sequence = ["libraryQuery"]
    index_of = {"libraryQuery": 0}
    for i in range(1, max_tab_presses + 1):
        page.keyboard.press("Tab")
        tag = page.evaluate(_FOCUS_PROBE)
        sequence.append(tag)
        if tag == "librarySearch" and "librarySearch" not in index_of:
            index_of["librarySearch"] = i
        elif tag and tag.startswith("segmented:") and "segmented" not in index_of:
            index_of["segmented"] = i
        elif tag and tag.startswith("media-card:") and "media_card" not in index_of:
            index_of["media_card"] = i

    reaches_query = True
    reaches_segmented = "segmented" in index_of
    reaches_search_button = "librarySearch" in index_of
    reaches_result_card = "media_card" in index_of
    tab_order_matches_visual_order = bool(
        reaches_segmented
        and reaches_search_button
        and reaches_result_card
        and index_of["segmented"] < index_of["librarySearch"] < index_of["media_card"]
    )

    # 2. Enter opens detail.
    page.goto(f"{page.url.split('?')[0]}?tab=library", wait_until="load")
    _wait_for_home_rails(page)
    page.evaluate("document.querySelector('.media-card').focus()")
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    enter_opens_detail = bool(page.evaluate("!document.getElementById('libraryDetail').hidden"))

    # 3. Escape closes the transfer dialog and returns focus to the button
    # that opened it (checked by DOM node identity, not just an id).
    page.goto(f"{page.url.split('?')[0]}?tab=library&media=1", wait_until="load")
    page.wait_for_selector("#libraryDetail:not([hidden]) .link-row", state="visible")
    page.click(".link-transfer")
    page.wait_for_selector("#transferDialog:not([hidden])")
    page.keyboard.press("Escape")
    # The dialog is hidden via the `hidden` attribute (display:none), so it
    # is never "visible" (wait_for_selector's default state) -- wait for
    # the attribute-selector match to simply exist in the DOM instead.
    page.wait_for_selector("#transferDialog[hidden]", state="attached")
    escape_closes_dialog = bool(page.evaluate("document.getElementById('transferDialog').hidden"))
    escape_returns_focus_to_trigger = bool(
        page.evaluate("document.activeElement === document.querySelector('.link-transfer')")
    )

    # 4. role=tab aria-selected follows clicks.
    page.click("#tab-openlist")
    page.wait_for_selector("#openlist:not([hidden])")
    library_deselected = page.eval_on_selector("#tab-library", "el => el.getAttribute('aria-selected')") == "false"
    openlist_selected = page.eval_on_selector("#tab-openlist", "el => el.getAttribute('aria-selected')") == "true"
    tab_aria_selected_updates = bool(library_deselected and openlist_selected)

    # 5. Provider-logo tablist (x1-provider-ui fix wave 1, item 3; T18
    # §13.1: no expand step any more -- every group's links are already
    # visible). Roving tabindex + aria-selected on ArrowRight/Home/End,
    # and activating a provider tab actually narrows the resource group's
    # visible link rows to that provider. media=1 has two providers (115,
    # quark) on its single resource group (see
    # tests/fixtures/library/build_installed.py) -- the default/全部
    # selection shows both links, and switching to one provider must show
    # only its own. I1 (§14.1): every activation is now a real backend-
    # scoped fetch + full re-render, so each step below waits for it to
    # settle (by polling document.activeElement, never a stale id) instead
    # of assuming a synchronous DOM update -- and the tablist itself can
    # shrink: a scoped response only ever offers [全部, <code>], so once
    # ArrowRight lands on "115" the "quark" tab is gone entirely until
    # 全部 is selected again.
    page.goto(f"{page.url.split('?')[0]}?tab=library&media=1", wait_until="load")
    page.wait_for_selector("#libraryProviderTabs .provider-tab", state="visible")
    tab_ids = page.eval_on_selector_all("#libraryProviderTabs .provider-tab", "els => els.map(el => el.id)")
    provider_tab_has_multiple_providers = len(tab_ids) >= 3  # 全部 + >=2 real providers

    page.wait_for_selector(".link-row", state="visible")
    links_before_filter = page.eval_on_selector_all(".link-row", "els => els.length")

    def _wait_for_active_provider_tab(code: str) -> None:
        page.wait_for_function(
            """(code) => {
                var el = document.activeElement;
                return !!el && el.classList && el.classList.contains('provider-tab') &&
                    el.dataset.provider === code && el.getAttribute('aria-selected') === 'true';
            }""",
            arg=code,
        )

    page.evaluate("(id) => document.getElementById(id).focus()", tab_ids[0])
    page.keyboard.press("ArrowRight")
    _wait_for_active_provider_tab("115")
    provider_tab_arrow_right_moves_selection = True
    provider_tab_arrow_right_moves_focus = True

    # Home first, back to 全部 (unscoped fetch, 3 tabs: 全部, 115, quark) --
    # a real transition off "115". Only once focus has genuinely left the
    # last tab can End's own jump-to-last be verified as a real transition
    # too: pressing End while already sitting on the tablist's last tab
    # (as the old ArrowRight-then-End sequence did, on the 2-tab scoped
    # list) makes the wait pass trivially, without End's handler ever
    # having to move anything.
    page.keyboard.press("Home")
    _wait_for_active_provider_tab("")
    provider_tab_home_selects_first_tab = True

    # media=1's two real providers are "115" and "quark", in that
    # PROVIDER_ORDER priority (library_store.py) -- so the unscoped 3-tab
    # list's actual last tab is "quark".
    page.keyboard.press("End")
    _wait_for_active_provider_tab("quark")
    provider_tab_end_selects_last_tab = True

    # Back on 全部 (Home again -- unscoped fetch, 3 tabs) -- ArrowRight
    # lands on the first real provider once more; its link rows must now
    # be limited to that provider's own count, with no reload.
    page.keyboard.press("Home")
    _wait_for_active_provider_tab("")
    page.keyboard.press("ArrowRight")
    _wait_for_active_provider_tab("115")
    target_tab_id = page.evaluate("() => document.activeElement.id")
    target_count = page.eval_on_selector(f"#{target_tab_id} .provider-tab-count", "el => el.textContent")
    links_after_filter = page.eval_on_selector_all(".link-row", "els => els.length")
    provider_tab_activation_filters_group_links = bool(
        links_after_filter > 0
        and str(links_after_filter) == target_count
        and links_after_filter < links_before_filter
    )

    return {
        "tab_order_reaches_query": reaches_query,
        "tab_order_reaches_segmented": reaches_segmented,
        "tab_order_reaches_search_button": reaches_search_button,
        "tab_order_reaches_result_card": reaches_result_card,
        "tab_order_matches_visual_order": tab_order_matches_visual_order,
        "tab_sequence": sequence,
        "enter_opens_detail": enter_opens_detail,
        "escape_closes_dialog": escape_closes_dialog,
        "escape_returns_focus_to_trigger": escape_returns_focus_to_trigger,
        "tab_aria_selected_updates": tab_aria_selected_updates,
        "provider_tab_has_multiple_providers": provider_tab_has_multiple_providers,
        "provider_tab_arrow_right_moves_selection": provider_tab_arrow_right_moves_selection,
        "provider_tab_arrow_right_moves_focus": provider_tab_arrow_right_moves_focus,
        "provider_tab_end_selects_last_tab": provider_tab_end_selects_last_tab,
        "provider_tab_home_selects_first_tab": provider_tab_home_selects_first_tab,
        "provider_tab_activation_filters_group_links": provider_tab_activation_filters_group_links,
    }


def check_library_tab_reset(page, base_url: str) -> dict:
    """T16 §3 acceptance: clicking the 资源库 tab -- from search results,
    an open detail view, the filters popover open, OpenList and STRM --
    always resets to a clean library home view: bare ``?tab=library`` URL,
    the tab itself selected, home shown/detail+filters hidden, and the
    page scrolled back to the top."""

    def _click_library_tab_and_measure():
        page.evaluate("window.scrollTo(0, 300)")
        page.click("#tab-library")
        _wait_for_home_rails(page)
        page.wait_for_timeout(50)
        return {
            "url_is_bare_tab_library": page.evaluate("() => new URL(window.location.href).search") == "?tab=library",
            "library_tab_selected": page.eval_on_selector("#tab-library", "el => el.getAttribute('aria-selected')") == "true",
            "home_visible": bool(page.evaluate("() => !document.getElementById('libraryHome').hidden")),
            "detail_hidden": bool(page.evaluate("() => document.getElementById('libraryDetail').hidden")),
            "filters_closed": bool(page.evaluate("() => document.getElementById('libraryFilters').hidden")),
            "scrolled_to_top": page.evaluate("() => window.scrollY") <= 2,
        }

    results = {}

    page.goto(f"{base_url}/?tab=library", wait_until="load")
    _wait_for_home_rails(page)
    page.fill("#libraryQuery", "虚构")
    page.click("#librarySearch")
    page.wait_for_selector("#libraryResults .media-card", state="visible")
    results["from_search_results"] = _click_library_tab_and_measure()

    page.goto(f"{base_url}/?tab=library&media=1", wait_until="load")
    page.wait_for_selector("#libraryDetail:not([hidden]) .link-row", state="visible")
    results["from_detail"] = _click_library_tab_and_measure()

    page.goto(f"{base_url}/?tab=library", wait_until="load")
    _wait_for_home_rails(page)
    page.click("#libraryFiltersToggle")
    page.wait_for_selector("#libraryFilters:not([hidden])", state="visible")
    results["from_filters_open"] = _click_library_tab_and_measure()

    page.goto(f"{base_url}/?tab=openlist", wait_until="load")
    page.wait_for_selector("#openlist:not([hidden])")
    page.wait_for_selector("#openResult .file-row, #openResult .empty.error")
    results["from_openlist"] = _click_library_tab_and_measure()

    page.goto(f"{base_url}/?tab=strm", wait_until="load")
    page.wait_for_selector("#strm:not([hidden])")
    page.wait_for_selector("#strmResult .file-row, #strmResult .empty.error")
    results["from_strm"] = _click_library_tab_and_measure()

    return results


def library_tab_reset_violations(results: dict[str, dict]) -> list[str]:
    violations: list[str] = []
    for scenario, entry in results.items():
        for key, ok in entry.items():
            if not ok:
                violations.append(f"{scenario}: {key} failed")
    return violations


# ---------------------------------------------------------------------------
# T21 §8.4: detail-page backdrop geometry -- pure object-fit:contain /
# rectangle-overlap math (unit-tested directly, no browser needed) plus the
# Playwright measurement that feeds it real DOM numbers.
# ---------------------------------------------------------------------------


def compute_contain_box(container_w: float, container_h: float, natural_w: float, natural_h: float) -> dict:
    """The CSS ``object-fit:contain`` + ``object-position:center`` geometry:
    the largest box with the image's own aspect ratio that fits entirely
    inside a ``container_w`` x ``container_h`` box, centred. Pure function,
    fed real ``naturalWidth``/``naturalHeight`` + ``getBoundingClientRect()``
    measurements by ``check_backdrop_hero`` below."""
    if not container_w or not container_h or not natural_w or not natural_h:
        return {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
    scale = min(container_w / natural_w, container_h / natural_h)
    width = natural_w * scale
    height = natural_h * scale
    return {"x": (container_w - width) / 2, "y": (container_h - height) / 2, "width": width, "height": height}


def rects_overlap(a: dict, b: dict, tolerance: float = 0.5) -> bool:
    """True if two boxes intersect with more than ``tolerance`` px of
    overlap on both axes. Accepts either a DOMRect-shaped dict
    (``left``/``top``/``right``/``bottom``, what ``getBoundingClientRect()``
    gives) or an ``x``/``y``/``width``/``height`` dict (what
    ``compute_contain_box`` returns)."""

    def _edges(box: dict) -> tuple[float, float, float, float]:
        left = box.get("left", box.get("x", 0))
        top = box.get("top", box.get("y", 0))
        right = box.get("right", left + box.get("width", 0))
        bottom = box.get("bottom", top + box.get("height", 0))
        return left, top, right, bottom

    a_left, a_top, a_right, a_bottom = _edges(a)
    b_left, b_top, b_right, b_bottom = _edges(b)
    x_overlap = min(a_right, b_right) - max(a_left, b_left)
    y_overlap = min(a_bottom, b_bottom) - max(a_top, b_top)
    return x_overlap > tolerance and y_overlap > tolerance


_BACKDROP_HERO_PROBE = """
() => {
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height};
  };
  const backdrop = document.querySelector('.detail-backdrop');
  const img = document.querySelector('.detail-backdrop__image');
  const cs = img ? getComputedStyle(img) : null;
  return {
    backdropRect: rect(backdrop),
    backdropEmptyClass: backdrop ? backdrop.classList.contains('detail-backdrop--empty') : null,
    posterRect: rect(document.querySelector('.detail-poster')),
    infoRect: rect(document.querySelector('.detail-info')),
    imgPresent: !!img,
    imgObjectFit: cs ? cs.objectFit : null,
    imgAlt: img ? img.alt : null,
    naturalWidth: img ? img.naturalWidth : 0,
    naturalHeight: img ? img.naturalHeight : 0,
  };
}
"""


def check_backdrop_hero(page, base_url: str, media_id: str, size_label: str, *, expected_ratio: float | None) -> dict:
    """T21 §8.4 acceptance, for one (media, viewport) pair: navigate to its
    detail page and check -- no CLS (hero height identical measured right
    after ``domcontentloaded``, before the foreground image has
    necessarily finished loading, and again once ``img.complete``);
    ``object-fit`` is never ``cover``; the rendered (``contain``-math)
    image box lies entirely inside the container and matches the
    fixture's own ratio within 1% (so all four corners of the source
    image are visible -- never cropped); ``.detail-info`` never overlaps
    the backdrop container (only the poster is allowed to, by design);
    the poster and title/info boxes never overlap each other; the
    accessibility tree exposes exactly the foreground's alt text and no
    more (the blur/gradient layers stay ``aria-hidden``); and
    ``.detail-backdrop--empty`` (subtle gradient fallback fill) is set
    if-and-only-if there is no image at all."""
    page.goto(f"{base_url}/?tab=library&media={media_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#libraryDetail:not([hidden]) .detail-hero", state="visible")
    height_before = page.eval_on_selector(".detail-hero", "el => el.getBoundingClientRect().height")
    page.wait_for_function(
        "() => { const img = document.querySelector('.detail-backdrop__image'); return !img || img.complete; }"
    )
    height_after = page.eval_on_selector(".detail-hero", "el => el.getBoundingClientRect().height")
    m = page.evaluate(_BACKDROP_HERO_PROBE)
    hero_a11y = page.locator(".detail-hero").aria_snapshot()

    violations: list[str] = []
    no_cls = abs(height_before - height_after) <= 0.5
    if not no_cls:
        violations.append(f"hero height changed after image load: {height_before} -> {height_after}")

    object_fit_ok = True
    contain_box = None
    corners_inside = None
    ratio_ok = None
    if m["imgPresent"]:
        object_fit_ok = m["imgObjectFit"] == "contain"
        if not object_fit_ok:
            violations.append(f"object-fit is {m['imgObjectFit']!r}, expected contain")
        container = m["backdropRect"]
        contain_box = compute_contain_box(container["width"], container["height"], m["naturalWidth"], m["naturalHeight"])
        corners_inside = (
            contain_box["x"] >= -0.5 and contain_box["y"] >= -0.5
            and contain_box["x"] + contain_box["width"] <= container["width"] + 0.5
            and contain_box["y"] + contain_box["height"] <= container["height"] + 0.5
        )
        if not corners_inside:
            violations.append(f"contain box escapes the container: {contain_box} vs {container}")
        if expected_ratio and contain_box["height"]:
            displayed_ratio = contain_box["width"] / contain_box["height"]
            ratio_ok = abs(displayed_ratio - expected_ratio) / expected_ratio <= 0.01
            if not ratio_ok:
                violations.append(f"displayed ratio {displayed_ratio} != expected {expected_ratio} (>1%)")

    info_below_backdrop = True
    if m["infoRect"] and m["backdropRect"]:
        info_below_backdrop = m["infoRect"]["top"] >= m["backdropRect"]["bottom"] - 0.5
        if not info_below_backdrop:
            violations.append("detail-info overlaps the backdrop container")
    poster_info_overlap = bool(m["posterRect"] and m["infoRect"] and rects_overlap(m["posterRect"], m["infoRect"]))
    if poster_info_overlap:
        violations.append("poster box overlaps the title/info box")

    accessible_image_lines = [line for line in hero_a11y.splitlines() if re.search(r"\bimg\b", line)]
    expected_image_lines = 1 if m["imgPresent"] else 0
    decorative_layers_hidden = len(accessible_image_lines) == expected_image_lines
    if not decorative_layers_hidden:
        violations.append(f"expected {expected_image_lines} accessible img node(s), got {accessible_image_lines}")
    alt_in_a11y = True
    if m["imgPresent"]:
        alt_in_a11y = bool(m["imgAlt"]) and m["imgAlt"] in hero_a11y
        if not alt_in_a11y:
            violations.append(f"foreground alt {m['imgAlt']!r} missing from accessibility snapshot")

    empty_state_ok = m["backdropEmptyClass"] == (not m["imgPresent"])
    if not empty_state_ok:
        violations.append(
            f"detail-backdrop--empty is {m['backdropEmptyClass']!r} but image_present is {m['imgPresent']!r}"
        )

    return {
        "size": size_label,
        "media_id": media_id,
        "image_present": m["imgPresent"],
        "empty_state_ok": empty_state_ok,
        "object_fit_contain": object_fit_ok,
        "no_cls": no_cls,
        "hero_height": height_after,
        "contain_box": contain_box,
        "corners_inside_container": corners_inside,
        "ratio_matches_fixture": ratio_ok,
        "info_below_backdrop": info_below_backdrop,
        "poster_info_overlap": poster_info_overlap,
        "alt_in_accessibility_tree": alt_in_a11y,
        "decorative_layers_hidden": decorative_layers_hidden,
        "violations": violations,
        "ok": not violations,
    }


BACKDROP_CAPTURE_SIZES: tuple[str, ...] = ("1440x900", "1280x800", "1024x768", "390x844", "375x812")


def _capture_detail_backdrops(page, base_url: str, out_dir: Path, size_label: str, overflow_results: list[dict]) -> list[dict]:
    """T21 §8.4: screenshot + geometry-check the detail page for each of
    the 4 different-ratio backdrop fixtures plus the no-backdrop fallback
    (``_seed_backdrop_fixtures_for_screenshots``), at this size. Appends
    one horizontal-overflow check per key to ``overflow_results`` (the
    same list every other page/size capture uses) and returns one
    ``check_backdrop_hero()`` result per key."""
    assert _backdrop_fixture_media_ids, "_seed_backdrop_fixtures_for_screenshots must run before this capture"
    results = []
    for key, media_id in _backdrop_fixture_media_ids.items():
        dims = BACKDROP_FIXTURE_RATIOS.get(key)
        expected_ratio = (dims[0] / dims[1]) if dims else None
        result = check_backdrop_hero(page, base_url, media_id, size_label, expected_ratio=expected_ratio)
        result["key"] = key
        results.append(result)
        page_name = f"detail-backdrop-{key}"
        _full_page_screenshot(page, out_dir / f"{page_name}.png")
        overflow_results.append(_check_overflow(page, size_label, page_name))
    return results


def backdrop_hero_violations(results: Sequence[dict]) -> list[str]:
    """Aggregate every individual check_backdrop_hero() violation plus the
    one comparison that needs more than one result at a time (§8.4:
    "fallback state has the same container height as the loaded state at
    the same viewport") -- within 0.5px, per size."""
    violations: list[str] = [v for r in results for v in r["violations"]]
    heights_by_size: dict[str, dict[str, float]] = {}
    for r in results:
        heights_by_size.setdefault(r["size"], {})[r["key"]] = r["hero_height"]
    for size_label, heights in heights_by_size.items():
        fallback_height = heights.get("fallback")
        if fallback_height is None:
            continue
        for key, height in heights.items():
            if key == "fallback":
                continue
            if abs(height - fallback_height) > 0.5:
                violations.append(f"{size_label}: fallback hero height {fallback_height} != {key} hero height {height}")
    return violations


# w6-contract §UI item 5: a reduced two-size subset (like REAUTH_CAPTURE_SIZES
# above) -- desktop + the smallest mobile breakpoint -- rather than the full
# six-size SIZES matrix, since this fixture set needs its own extra
# navigations (detail + a search + settings) on top of everything else
# already captured per size.
LINKCHECK_CAPTURE_SIZES: tuple[str, ...] = ("1440x900", "390x844")

# A representative, internally-consistent GET /api/library/linkcheck-status
# stand-in (§ contract item 4 shape) -- one provider of each interesting
# status/error state (enabled+healthy, disabled, rate-limited+paused,
# untouched) so settings-linkcheck.png actually shows populated counts
# instead of every row's all-zero skeleton. heartbeat_at is filled in with
# a real recent timestamp by _install_linkcheck_route_stub below.
_LINKCHECK_STATUS_STUB = {
    "enabled": True,
    "heartbeat_at": None,
    "leader_pid": 4242,
    "providers": {
        "tianyicloud": {
            "enabled": True, "daily_cap": 3000, "used_today": 812, "interval_seconds": 3,
            "paused_until": None, "last_error_class": None,
            "valid": 120, "invalid": 4, "unknown": 9, "unchecked": 40, "due": 12,
        },
        "115": {
            "enabled": False, "daily_cap": 500, "used_today": 0, "interval_seconds": 10,
            "paused_until": None, "last_error_class": "provider_disabled",
            "valid": 0, "invalid": 0, "unknown": 0, "unchecked": 60, "due": 0,
        },
        "quark": {
            "enabled": True, "daily_cap": 3000, "used_today": 45, "interval_seconds": 3,
            "paused_until": "2026-09-06T12:00:00Z", "last_error_class": "rate_limited",
            "valid": 30, "invalid": 2, "unknown": 3, "unchecked": 5, "due": 0,
        },
        "alipan": {
            "enabled": False, "daily_cap": 3000, "used_today": 0, "interval_seconds": 3,
            "paused_until": None, "last_error_class": None,
            "valid": 0, "invalid": 0, "unknown": 0, "unchecked": 20, "due": 0,
        },
    },
    "totals": {"checked": 213, "valid": 150, "invalid": 6, "unknown": 12, "unchecked": 125, "queued_priority": 0},
}


def _install_linkcheck_route_stub(page) -> None:
    """Screenshot-only: patches just the two seeded linkcheck-fixture
    media's own JSON (adding check_status/check_reason/checked_at/invalid
    on their link objects, all_links_invalid on the matching search item)
    and stubs GET linkcheck-status -- entirely in the browser's network
    layer, never touching a backend file. Must be called AFTER
    install_network_guard on the same page: Playwright runs the most-
    recently-registered matching route handler first, so these more
    specific patterns take over just these three URL shapes while every
    other request still passes through the existing guard untouched."""
    checked_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 3600))
    status_payload = json.loads(json.dumps(_LINKCHECK_STATUS_STUB))
    status_payload["heartbeat_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))

    def patch_media(route):
        response = route.fetch()
        data = response.json()
        for group in data.get("groups", []):
            for link in group.get("links", []):
                link_id = link.get("link_id")
                if link_id == _linkcheck_link_ids.get("quark"):
                    link.update(check_status="invalid", check_reason="share_expired", checked_at=checked_at_iso, invalid=True)
                elif link_id == _linkcheck_link_ids.get("tianyicloud"):
                    link.update(check_status="unknown", check_reason="anti_bot", checked_at=checked_at_iso, invalid=False)
                elif link_id == _linkcheck_link_ids.get("all_invalid"):
                    link.update(check_status="invalid", check_reason="file_deleted", checked_at=checked_at_iso, invalid=True)
        route.fulfill(response=response, json=data)

    def patch_search(route):
        response = route.fetch()
        data = response.json()
        for item in data.get("items", []):
            if str(item.get("media_id")) == str(_linkcheck_all_invalid_media_id):
                item["all_links_invalid"] = True
        route.fulfill(response=response, json=data)

    def patch_linkcheck_status(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(status_payload))

    page.route(re.compile(r"/api/library/media/\d+"), patch_media)
    page.route(re.compile(r"/api/library/search"), patch_search)
    page.route("**/api/library/linkcheck-status", patch_linkcheck_status)


def _tab_focus_reaches(page, selector: str, max_presses: int = 80) -> bool:
    """Bounded native Tab-key walk from a blurred (no active element)
    start -- proves `selector` is really keyboard-reachable, not merely
    present in the DOM (a `disabled` element, or one with tabindex=-1,
    never becomes document.activeElement this way)."""
    page.evaluate("document.activeElement && document.activeElement.blur()")
    for _ in range(max_presses):
        page.keyboard.press("Tab")
        reached = page.evaluate(
            "(sel) => { const el = document.activeElement; return !!(el && el.matches(sel)); }", selector
        )
        if reached:
            return True
    return False


def _capture_and_check_linkcheck(page, base_url: str, out_dir: Path, size_label: str) -> dict:
    """w6-contract §UI item 5 acceptance, against the seeded linkcheck
    fixtures (_seed_linkcheck_fixtures_for_screenshots) with their network
    responses patched (_install_linkcheck_route_stub): the link-invalid
    text badge's computed colour really is --danger, the detail page with
    all three check states rendered never overflows horizontally, 重新检测
    is reachable via the keyboard, the all-invalid title's card shows its
    corner badge, and the settings card renders its four provider rows."""
    assert _linkcheck_media_id and _linkcheck_all_invalid_media_id, \
        "_seed_linkcheck_fixtures_for_screenshots must run before this capture"
    _install_linkcheck_route_stub(page)

    page.goto(f"{base_url}/?tab=library&media={_linkcheck_media_id}", wait_until="load")
    page.wait_for_selector(".link-invalid-text", state="visible")
    _full_page_screenshot(page, out_dir / "detail-linkcheck.png")
    overflow = _check_overflow(page, size_label, "detail-linkcheck")

    badge_color = page.evaluate(
        """() => {
            const badge = document.querySelector('.link-invalid-text');
            const probe = document.createElement('span');
            probe.style.color = 'var(--danger)';
            document.body.appendChild(probe);
            const expected = getComputedStyle(probe).color;
            const actual = badge ? getComputedStyle(badge).color : null;
            probe.remove();
            return { actual: actual, expected: expected, present: !!badge };
        }"""
    )
    keyboard_reaches_recheck = _tab_focus_reaches(page, ".group-recheck-btn")

    # A text `q=` search depends on the search_term/search_doc index that
    # only build_synthetic_library's own fixture-setup step populates --
    # these two freshly-seeded media were never indexed into it. `sort=`
    # anything but the default "relevance" already takes isBrowsing() out
    # of its home/rails branch (static/app.js) into the plain "browse,
    # sorted, no text query" SQL path instead, which lists straight from
    # the `media` table (see library_search.search's `else` branch) -- no
    # indexing dependency, and with well under page_size=24 media seeded
    # in total, guaranteed to land on page 1 regardless of ordering.
    # An all-links-invalid media counts like a source-deleted one (design
    # §6 / w6 contract): the default live-only listing hides it, and it
    # only appears -- carrying the 全部失效 badge -- under the 包含已失效
    # filter (`deleted=1`), which is therefore where the badge is checked.
    page.goto(f"{base_url}/?tab=library&sort=year_desc&deleted=1", wait_until="load")
    page.wait_for_selector("#libraryResults .media-card", state="visible")
    page.wait_for_timeout(260)
    _full_page_screenshot(page, out_dir / "card-invalid-badge.png")
    card_badge_present = page.evaluate("() => !!document.querySelector('.card-invalid-badge')")

    page.goto(f"{base_url}/?tab=settings", wait_until="load")
    page.wait_for_selector(".linkcheck-provider")
    page.wait_for_timeout(150)
    _full_page_screenshot(page, out_dir / "settings-linkcheck.png")
    linkcheck_provider_rows = page.eval_on_selector_all(".linkcheck-provider", "els => els.length")

    violations: list[str] = []
    if not badge_color["present"] or badge_color["actual"] != badge_color["expected"]:
        violations.append(f"{size_label}: .link-invalid-text color {badge_color['actual']!r} != --danger {badge_color['expected']!r}")
    if not overflow["ok"]:
        violations.append(f"{size_label}: detail-linkcheck page overflows horizontally")
    if not keyboard_reaches_recheck:
        violations.append(f"{size_label}: 重新检测 button not reachable via keyboard Tab order")
    if not card_badge_present:
        violations.append(f"{size_label}: .card-invalid-badge missing for an all_links_invalid card")
    if linkcheck_provider_rows != 4:
        violations.append(f"{size_label}: expected 4 .linkcheck-provider rows, found {linkcheck_provider_rows}")

    return {
        "size": size_label,
        "badge_color_matches_danger": bool(badge_color["present"] and badge_color["actual"] == badge_color["expected"]),
        "no_overflow": bool(overflow["ok"]),
        "recheck_keyboard_reachable": bool(keyboard_reaches_recheck),
        "card_invalid_badge_present": bool(card_badge_present),
        "settings_provider_rows": linkcheck_provider_rows,
        "violations": violations,
    }


def linkcheck_violations(results: Sequence[dict]) -> list[str]:
    return [v for r in results for v in r["violations"]]


# T18 §14.1: media #1 in the synthetic fixture (tests/fixtures/library/
# build_installed.py) has one resource group with both a live "115" and a
# live "quark" link -- the ">=2 providers on one media" case the work
# order's own acceptance example names explicitly.
_PROVIDER_ISOLATION_OTHER_PROVIDER_MARKERS = ("quark", "Quark", "夸克")


def check_provider_isolation(page, base_url: str) -> dict:
    """T18 §14.1 acceptance: `?provider=` must be a genuine data-level
    filter, not a visual one -- a filtered detail view must never contain
    another provider's label/logo anywhere reachable: not the rendered
    DOM (``#libraryDetail``'s actual ``innerHTML``, not the JS source), and
    not the accessibility tree Playwright builds from that same DOM
    (``Locator.aria_snapshot()`` -- ``page.accessibility.snapshot()`` was
    removed from this Playwright version). Loading the SAME media
    unfiltered is the positive control proving the fixture (and this
    check) can actually see the other provider at all when it's supposed
    to be there."""
    detail_root = "#libraryDetail"

    def _scan(url: str) -> tuple[str, str]:
        page.goto(url, wait_until="load")
        page.wait_for_selector(f"{detail_root}:not([hidden]) .link-row", state="visible")
        dom_html = page.eval_on_selector(detail_root, "el => el.innerHTML")
        a11y_text = page.locator(detail_root).aria_snapshot()
        return dom_html, a11y_text

    filtered_dom, filtered_a11y = _scan(f"{base_url}/?tab=library&media=1&provider=115")
    full_dom, full_a11y = _scan(f"{base_url}/?tab=library&media=1")

    # I1: the URL-driven load above only proves open() is isolated. The
    # in-page tab switch (selectProvider) must be equally isolated -- open
    # the media unfiltered, click the 115 tab in real Chromium, wait for
    # the resulting backend-scoped re-render (the tablist shrinks to just
    # [全部, 115] once the quark facet is gone from the scoped response),
    # and scan the same way.
    page.goto(f"{base_url}/?tab=library&media=1", wait_until="load")
    page.wait_for_selector(f"{detail_root}:not([hidden]) .link-row", state="visible")
    page.click(f'{detail_root} .provider-tab[data-provider="115"]')
    page.wait_for_selector(f'{detail_root} .provider-tab[data-provider="quark"]', state="detached")
    tab_switch_dom = page.eval_on_selector(detail_root, "el => el.innerHTML")
    tab_switch_a11y = page.locator(detail_root).aria_snapshot()

    violations: list[str] = []
    for marker in _PROVIDER_ISOLATION_OTHER_PROVIDER_MARKERS:
        if marker in filtered_dom:
            violations.append(f"filtered DOM contains {marker!r}")
        if marker in filtered_a11y:
            violations.append(f"filtered accessibility tree contains {marker!r}")
        if marker in tab_switch_dom:
            violations.append(f"in-page tab switch DOM contains {marker!r}")
        if marker in tab_switch_a11y:
            violations.append(f"in-page tab switch accessibility tree contains {marker!r}")
    unfiltered_shows_other_provider = any(
        marker in full_dom or marker in full_a11y for marker in _PROVIDER_ISOLATION_OTHER_PROVIDER_MARKERS
    )
    if not unfiltered_shows_other_provider:
        violations.append("positive control failed: unfiltered view never showed the other provider at all")
    if "115" not in filtered_dom:
        violations.append("filtered view lost the REQUESTED provider's own link too")
    if "115" not in tab_switch_dom:
        violations.append("in-page tab switch lost the SELECTED provider's own link too")

    return {
        "filtered_dom_excludes_other_provider": not any(m in filtered_dom for m in _PROVIDER_ISOLATION_OTHER_PROVIDER_MARKERS),
        "filtered_accessibility_tree_excludes_other_provider": not any(m in filtered_a11y for m in _PROVIDER_ISOLATION_OTHER_PROVIDER_MARKERS),
        "tab_switch_dom_excludes_other_provider": not any(m in tab_switch_dom for m in _PROVIDER_ISOLATION_OTHER_PROVIDER_MARKERS),
        "tab_switch_accessibility_tree_excludes_other_provider": not any(m in tab_switch_a11y for m in _PROVIDER_ISOLATION_OTHER_PROVIDER_MARKERS),
        "unfiltered_view_shows_other_provider": unfiltered_shows_other_provider,
        "violations": violations,
        "ok": not violations,
    }


_CONTRAST_TARGETS = (
    ("h2", "primary-heading"),
    (".page-intro p", "secondary-text"),
    ("#librarySearch", "primary-button-text"),
)

_COLOR_PROBE = """
el => {
  let bgEl = el;
  let bg = getComputedStyle(bgEl).backgroundColor;
  while (bgEl && (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent')) {
    bgEl = bgEl.parentElement;
    if (!bgEl) break;
    bg = getComputedStyle(bgEl).backgroundColor;
  }
  return {color: getComputedStyle(el).color, background: bg || 'rgb(255, 255, 255)', id: el.id || null};
}
"""


def run_contrast_checks(page) -> list[dict]:
    results = []
    for selector, label in _CONTRAST_TARGETS:
        data = page.eval_on_selector(selector, _COLOR_PROBE)
        fg = parse_css_color(data["color"])
        bg = parse_css_color(data["background"])
        ratio = contrast_ratio(fg, bg)
        results.append(
            {
                "target": label,
                "element_id": data["id"],
                "ratio": round(ratio, 2),
                "pass": contrast_pass(ratio),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Card-footer fixed-height check (x1-provider-ui fix wave 1, item 1: work
# order §2.1/§3.1 -- a card's height must never depend on its content).
# ---------------------------------------------------------------------------

_CARD_ROW_HEIGHTS_PROBE = """
(sel) => {
  const cards = Array.from(document.querySelectorAll(sel + ' .media-card'));
  const rows = {};
  cards.forEach((c) => {
    const r = c.getBoundingClientRect();
    const top = Math.round(r.top);
    (rows[top] = rows[top] || []).push(Math.round(r.height));
  });
  return Object.values(rows);
}
"""


def card_heights_uniform(page, container_selector: str) -> bool:
    """Group every ``.media-card`` under ``container_selector`` by its
    rendered row (rounded bounding-rect top) and check every card sharing a
    row comes out exactly the same height -- an original title, a
    genre/待复核 chip row, or a differing provider count must never change a
    card's total height now that ``.card-footer`` is fixed-height
    (static/app.css). Returns ``False`` (never raises) if no card matched,
    so a broken selector shows up as a report failure instead of a crash."""
    rows = page.evaluate(_CARD_ROW_HEIGHTS_PROBE, container_selector)
    return bool(rows) and all(len(set(heights)) == 1 for heights in rows)


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------


def run_full_capture(
    out_dir: Path, *, pages: Sequence[str] = ALL_PAGES, sizes: Sequence[tuple[int, int]] = SIZES
) -> dict:
    """Start the temp app, capture every page at every size (one browser,
    one context at a time -- each closed before the next opens), run the
    keyboard walkthrough + contrast spot-check on a representative desktop
    viewport, write ``out_dir/report.json``, and return the report dict.
    Used by both the CLI entry point (``main``) and
    ``test_six_sizes_eight_pages`` (real Chromium, never skipped)."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    overflow_results: list[dict] = []

    geometry_results: dict[str, dict] = {}
    geometry_violations: list[str] = []

    filters_results: dict[str, dict] = {}
    retry_counts: dict[str, int] = {}
    backdrop_results: list[dict] = []
    linkcheck_results: list[dict] = []

    with running_app() as base_url:
        with sync_playwright() as p:
            # A fresh browser PROCESS per phase below (not just a fresh
            # context) -- this container runs under real, externally-
            # imposed memory pressure (concurrent sibling agents sharing
            # the same box), and Chromium's own process-level memory does
            # not fully return to baseline just from closing contexts.
            # Bounding each browser process to one phase's worth of
            # navigations keeps any single process's footprint small
            # rather than letting ~40+ navigations accumulate in one.
            browser = launch_chromium(p)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = context.new_page()
                install_network_guard(page, base_url)
                with _crash_guard(page, "1440x900", "keyboard+contrast"):
                    page.goto(f"{base_url}/?tab=library", wait_until="load")
                    _wait_for_home_rails(page)
                    keyboard = run_keyboard_walkthrough(page)
                    contrast = run_contrast_checks(page)
                    # y1-cards §2.3 items 5/6: title-length and keyboard-focus
                    # invariance, checked once here rather than at every
                    # viewport -- these two behaviours don't vary by
                    # breakpoint, only per-viewport uniformity/ratio does
                    # (checked below, piggybacked on each screenshot).
                    # run_keyboard_walkthrough's last steps leave the page on
                    # a detail view (no #libraryRails there) -- back to the
                    # rails view first.
                    page.goto(f"{base_url}/?tab=library", wait_until="load")
                    _wait_for_home_rails(page)
                    title_length_invariant = check_title_length_does_not_affect_poster_size(page, "#libraryRails")
                    focus_invariant = check_focus_does_not_change_poster_size(page)
                    before, after = title_length_invariant["before"], title_length_invariant["after"]
                    if (abs(before["width"] - after["width"]) > _POSTER_DIM_TOLERANCE_PX
                            or abs(before["height"] - after["height"]) > _POSTER_DIM_TOLERANCE_PX):
                        geometry_violations.append(f"title length changed poster size: {title_length_invariant}")
                    rails_boxes = collect_poster_boxes(page, "#libraryRails")
                    real_boxes = [b for b in rails_boxes if b["state"] != "loading"] or rails_boxes
                    avg_w = sum(b["width"] for b in real_boxes) / len(real_boxes)
                    avg_h = sum(b["height"] for b in real_boxes) / len(real_boxes)
                    if (abs(focus_invariant["width"] - avg_w) > _POSTER_DIM_TOLERANCE_PX
                            or abs(focus_invariant["height"] - avg_h) > _POSTER_DIM_TOLERANCE_PX):
                        geometry_violations.append(
                            f"focused card poster size {focus_invariant} differs from "
                            f"container average ({avg_w:.2f}, {avg_h:.2f})"
                        )
                # x1-provider-ui fix wave 1, item 1: measure card heights on
                # the home rails and the search results grid at the
                # desktop breakpoint -- the fixed-height .card-footer must
                # make every card in a row come out exactly the same
                # height regardless of content (work order §2.1/§3.1).
                with _crash_guard(page, "1440x900", "card-heights"):
                    page.goto(f"{base_url}/?tab=library", wait_until="load")
                    _wait_for_home_rails(page)
                    rails_card_heights_uniform = card_heights_uniform(page, "#libraryRails")
                    page.fill("#libraryQuery", "虚构")
                    page.click("#librarySearch")
                    page.wait_for_selector("#libraryResults .media-card", state="visible")
                    page.wait_for_timeout(260)
                    results_card_heights_uniform = card_heights_uniform(page, "#libraryResults")
                # T16 §3 acceptance: clicking 资源库 from five different
                # states must always land back on a clean home view.
                with _crash_guard(page, "1440x900", "library-tab-reset"):
                    library_tab_reset = check_library_tab_reset(page, base_url)
                # T18 §14.1: provider isolation is a data-level fetch
                # contract, proven against the real rendered DOM + a11y
                # tree (not just app.js source text).
                with _crash_guard(page, "1440x900", "provider-isolation"):
                    provider_isolation = check_provider_isolation(page, base_url)
                context.close()
            finally:
                browser.close()

            size_labels = []
            for width, height in sizes:
                label = f"{width}x{height}"
                size_labels.append(label)
                viewport = {"width": width, "height": height}
                browser = launch_chromium(p)
                try:
                    context = browser.new_context(viewport=viewport)
                    page = context.new_page()
                    install_network_guard(page, base_url)
                    print(f"capturing {label}...")
                    # y1-cards §2.3: poster-geometry acceptance is
                    # piggybacked onto this same home/search navigation
                    # (geometry_results) -- no extra page loads.
                    page = capture_pages(
                        page, base_url, out_dir / label, pages, size_label=label,
                        overflow_results=overflow_results, geometry_results=geometry_results,
                        filters_results=filters_results, browser=browser, viewport=viewport,
                        retry_counts=retry_counts,
                    )
                    if label in REAUTH_CAPTURE_SIZES:
                        with _crash_guard(page, label, "reauth"):
                            _capture_reauth(page, base_url, out_dir / label)
                    # I3 (§14.6): unlike reauth above, this runs at every
                    # size -- the same overflow check every other page/size
                    # gets is exactly what this fixture exists to stress.
                    with _crash_guard(page, label, "detail-overflow"):
                        _capture_detail_overflow(page, base_url, out_dir / label)
                        overflow_results.append(_check_overflow(page, label, "detail-overflow"))
                    # T21 §8.4: the layered-backdrop geometry/CLS/a11y
                    # checks + their PNGs, at the five sizes the brief
                    # names (a subset of the six-size SIZES matrix above).
                    if label in BACKDROP_CAPTURE_SIZES:
                        with _crash_guard(page, label, "detail-backdrop"):
                            backdrop_results.extend(
                                _capture_detail_backdrops(page, base_url, out_dir / label, label, overflow_results)
                            )
                    # w6-contract §UI item 5: the link-check states, at a
                    # reduced two-size subset (LINKCHECK_CAPTURE_SIZES) --
                    # installed last in this per-size iteration, after
                    # every other page/size check above has already run
                    # against the real (unstubbed) network guard, so its
                    # route patches can never affect them.
                    if label in LINKCHECK_CAPTURE_SIZES:
                        with _crash_guard(page, label, "linkcheck"):
                            linkcheck_results.append(_capture_and_check_linkcheck(page, base_url, out_dir / label, label))
                    # capture_pages may have replaced ``context`` with a
                    # fresh one (a timeout retry) -- close whichever one
                    # is actually live via the returned page.
                    page.context.close()
                finally:
                    browser.close()

    for key, entry in geometry_results.items():
        geometry_violations.extend(geometry_violations_for(key, entry["analysis"]))
    geometry = {
        "per_viewport": geometry_results,
        "title_length_invariant": title_length_invariant,
        "focus_invariant": focus_invariant,
        "violations": geometry_violations,
        "ok": not geometry_violations,
    }

    filters_violations = filters_popover_violations(filters_results)
    filters_popover = {
        "per_viewport": filters_results,
        "violations": filters_violations,
        "ok": not filters_violations,
    }

    reset_violations = library_tab_reset_violations(library_tab_reset)
    library_tab_reset_report = {
        "scenarios": library_tab_reset,
        "violations": reset_violations,
        "ok": not reset_violations,
    }

    backdrop_violations = backdrop_hero_violations(backdrop_results)
    backdrop_report = {
        "sizes": list(BACKDROP_CAPTURE_SIZES),
        "results": backdrop_results,
        "violations": backdrop_violations,
        "ok": not backdrop_violations,
    }

    linkcheck_report_violations = linkcheck_violations(linkcheck_results)
    linkcheck_report = {
        "sizes": list(LINKCHECK_CAPTURE_SIZES),
        "results": linkcheck_results,
        "violations": linkcheck_report_violations,
        "ok": not linkcheck_report_violations,
    }

    report = assemble_report(
        sizes=size_labels,
        pages=list(pages),
        keyboard=keyboard,
        contrast=contrast,
        overflow=overflow_results,
        card_heights_uniform={"rails": rails_card_heights_uniform, "results": results_card_heights_uniform},
        geometry=geometry,
        filters_popover=filters_popover,
        library_tab_reset=library_tab_reset_report,
        provider_isolation=provider_isolation,
        backdrop=backdrop_report,
        linkcheck=linkcheck_report,
        retries=sum(retry_counts.values()),
    )
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {report_path}")
    return report


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "build" / "ui-screenshots",
        help="output directory (default: build/ui-screenshots)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    ok, message = ensure_chromium()
    if not ok:
        print(f"BLOCKED: chromium unavailable in this environment: {message}", file=sys.stderr)
        return 1

    report = run_full_capture(args.out_dir)

    overflow_failures = [item for item in report["overflow"] if not item["ok"]]
    if overflow_failures:
        print(f"FAILED: horizontal overflow on {len(overflow_failures)} page/size combination(s): {overflow_failures}", file=sys.stderr)
        return 1
    if not report["geometry"]["ok"]:
        print(f"FAILED: poster-geometry acceptance violations: {report['geometry']['violations']}", file=sys.stderr)
        return 1
    if not report["filters_popover"]["ok"]:
        print(f"FAILED: filters popover acceptance violations: {report['filters_popover']['violations']}", file=sys.stderr)
        return 1
    if not report["library_tab_reset"]["ok"]:
        print(f"FAILED: library tab reset acceptance violations: {report['library_tab_reset']['violations']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
