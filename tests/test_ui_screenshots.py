"""Tests for scripts/ui_screenshots.py (T5.8).

Two tiers:

* Pure contrast-math and report-structure unit tests -- always run, no
  browser involved.
* Two ``@pytest.mark.slow`` end-to-end tests that drive a real, temp
  (``HIDRIVE_AUTH_MODE=local``) ``app.py`` instance with Playwright: a
  homepage smoke test, and ``test_six_sizes_eight_pages`` which produces the
  full 48-screenshot + report.json deliverable under
  ``build/ui-screenshots/``. Both FAIL (never skip) when Chromium cannot
  actually be launched in this environment -- see
  ``ui_screenshots.chromium_available()``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ui_screenshots as uis  # noqa: E402


class TestParseCssColor:
    def test_rgb(self):
        assert uis.parse_css_color("rgb(24, 24, 34)") == (24, 24, 34)

    def test_rgba_ignores_alpha(self):
        assert uis.parse_css_color("rgba(0, 122, 255, 0.5)") == (0, 122, 255)

    def test_rounds_fractional_channels(self):
        assert uis.parse_css_color("rgb(1.6, 2.4, 255)") == (2, 2, 255)

    def test_rejects_unsupported_format(self):
        with pytest.raises(ValueError):
            uis.parse_css_color("#181822")


class TestContrastRatio:
    def test_black_on_white_is_21(self):
        # The canonical WCAG maximum ratio.
        assert uis.contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0, abs=0.01)

    def test_same_color_is_1(self):
        assert uis.contrast_ratio((100, 100, 100), (100, 100, 100)) == pytest.approx(1.0, abs=0.001)

    def test_symmetric(self):
        a = uis.contrast_ratio((24, 24, 34), (245, 246, 249))
        b = uis.contrast_ratio((245, 246, 249), (24, 24, 34))
        assert a == pytest.approx(b, abs=1e-9)

    def test_app_body_text_on_app_background_passes(self):
        # static/app.css light theme: --text:#181822 on --bg:#f5f6f9.
        ratio = uis.contrast_ratio((0x18, 0x18, 0x22), (0xF5, 0xF6, 0xF9))
        assert ratio == pytest.approx(16.29, abs=0.01)
        assert uis.contrast_pass(ratio) is True

    def test_app_muted_text_on_app_background_passes(self):
        # static/app.css light theme: --muted:#626276 on --bg:#f5f6f9.
        ratio = uis.contrast_ratio((0x62, 0x62, 0x76), (0xF5, 0xF6, 0xF9))
        assert ratio == pytest.approx(5.51, abs=0.01)
        assert uis.contrast_pass(ratio) is True

    def test_app_primary_button_white_text_on_accent_fails(self):
        # static/app.css: button{background:var(--accent) #007aff;color:#fff}.
        # This is a real finding (T5.8 self-review), not a test bug: white on
        # #007aff is ~4.02:1, below the 4.5:1 AA threshold for normal text
        # (it only clears the 3:1 large-text threshold). Logged in the T5.8
        # report for the next UI task.
        ratio = uis.contrast_ratio((255, 255, 255), (0x00, 0x7A, 0xFF))
        assert ratio == pytest.approx(4.02, abs=0.01)
        assert uis.contrast_pass(ratio) is False


class TestContrastPass:
    def test_pass_at_threshold(self):
        assert uis.contrast_pass(4.5) is True

    def test_fail_below_threshold(self):
        assert uis.contrast_pass(4.49) is False

    def test_custom_threshold(self):
        assert uis.contrast_pass(3.0, threshold=3.0) is True


class TestAssembleReport:
    def test_structure(self):
        report = uis.assemble_report(
            sizes=["1440x900"],
            pages=["home"],
            keyboard={"tab_order_reaches_query": True},
            contrast=[{"target": "primary-heading", "ratio": 12.3, "pass": True}],
            overflow=[{"size": "1440x900", "page": "home", "scroll_width": 1440, "inner_width": 1440, "ok": True}],
            card_heights_uniform={"rails": True, "results": True},
            geometry={"per_viewport": {}, "violations": [], "ok": True},
        )
        assert report["sizes"] == ["1440x900"]
        assert report["pages"] == ["home"]
        assert report["keyboard"]["tab_order_reaches_query"] is True
        assert report["contrast"][0]["pass"] is True
        assert report["overflow"][0]["ok"] is True
        assert report["card_heights_uniform"] == {"rails": True, "results": True}
        assert report["geometry"]["ok"] is True

    def test_is_json_serializable(self):
        report = uis.assemble_report(
            sizes=[], pages=[], keyboard={}, contrast=[], overflow=[], card_heights_uniform={},
            geometry={"per_viewport": {}, "violations": [], "ok": True},
        )
        json.dumps(report)  # must not raise

    def test_provider_isolation_defaults_to_a_passing_empty_result(self):
        report = uis.assemble_report(
            sizes=[], pages=[], keyboard={}, contrast=[], overflow=[], card_heights_uniform={},
            geometry={"per_viewport": {}, "violations": [], "ok": True},
        )
        assert report["provider_isolation"]["ok"] is True
        assert report["provider_isolation"]["violations"] == []

    def test_backdrop_defaults_to_a_passing_empty_result(self):
        report = uis.assemble_report(
            sizes=[], pages=[], keyboard={}, contrast=[], overflow=[], card_heights_uniform={},
            geometry={"per_viewport": {}, "violations": [], "ok": True},
        )
        assert report["backdrop"]["ok"] is True
        assert report["backdrop"]["violations"] == []
        assert report["backdrop"]["results"] == []

    def test_linkcheck_defaults_to_a_passing_empty_result(self):
        # w6-contract §UI item 5.
        report = uis.assemble_report(
            sizes=[], pages=[], keyboard={}, contrast=[], overflow=[], card_heights_uniform={},
            geometry={"per_viewport": {}, "violations": [], "ok": True},
        )
        assert report["linkcheck"]["ok"] is True
        assert report["linkcheck"]["violations"] == []
        assert report["linkcheck"]["results"] == []

    def test_linkcheck_section_carries_through_when_provided(self):
        report = uis.assemble_report(
            sizes=[], pages=[], keyboard={}, contrast=[], overflow=[], card_heights_uniform={},
            geometry={"per_viewport": {}, "violations": [], "ok": True},
            linkcheck={"sizes": ["1440x900"], "results": [{"size": "1440x900"}], "violations": ["x"], "ok": False},
        )
        assert report["linkcheck"]["ok"] is False
        assert report["linkcheck"]["violations"] == ["x"]


class TestCardHeightsUniform:
    # Pure-Python grouping/comparison logic, exercised without a browser by
    # stubbing out page.evaluate -- the real DOM measurement is covered by
    # test_six_sizes_eight_pages below.
    class _FakePage:
        def __init__(self, rows):
            self.rows = rows
            self.selector = None

        def evaluate(self, _script, selector):
            self.selector = selector
            return self.rows

    def test_true_when_every_row_is_internally_equal(self):
        page = self._FakePage([[220, 220, 220], [220]])
        assert uis.card_heights_uniform(page, "#libraryRails") is True

    def test_false_when_a_row_has_mismatched_heights(self):
        page = self._FakePage([[220, 226, 220]])
        assert uis.card_heights_uniform(page, "#libraryResults") is False

    def test_false_when_no_cards_matched(self):
        page = self._FakePage([])
        assert uis.card_heights_uniform(page, "#libraryRails") is False


class TestAnalyzePosterGeometry:
    # Pure-Python geometry analysis (y1-cards §2.3), exercised without a
    # browser -- the real DOM measurement is covered by
    # test_six_sizes_eight_pages below.
    def _box(self, width, height, state="image"):
        return {"width": width, "height": height, "state": state}

    def test_ok_when_uniform_2_to_3_ratio_and_states_match(self):
        boxes = [
            self._box(148, 222, "image"),
            self._box(148, 222.4, "fallback"),
            self._box(148, 221.8, "loading"),
        ]
        result = uis.analyze_poster_geometry(boxes)
        assert result["ok"] is True
        assert result["count"] == 3
        assert result["max_width_diff"] <= 1
        assert result["max_height_diff"] <= 1
        assert result["states"] == ["fallback", "image", "loading"]
        assert result["states_uniform"] is True

    def test_fails_when_width_diff_exceeds_1px(self):
        boxes = [self._box(148, 222), self._box(150, 225)]
        result = uis.analyze_poster_geometry(boxes)
        assert result["ok"] is False
        assert result["max_width_diff"] > 1

    def test_fails_when_ratio_is_off(self):
        # height/width == 1.2, not 1.5 -- e.g. a stray width:auto regression.
        boxes = [self._box(150, 180), self._box(150, 180)]
        result = uis.analyze_poster_geometry(boxes)
        assert result["ok"] is False
        assert result["min_ratio"] == pytest.approx(1.2, abs=0.001)

    def test_fails_when_a_box_has_zero_height(self):
        boxes = [self._box(150, 225), self._box(150, 0)]
        result = uis.analyze_poster_geometry(boxes)
        assert result["ok"] is False
        assert result["min_height"] == 0

    def test_fails_when_states_differ_in_size(self):
        # A loading/skeleton box that hasn't picked up the shared box's
        # width yet -- the exact regression y1-cards §2.1 guards against.
        boxes = [self._box(150, 225, "image"), self._box(120, 180, "loading")]
        result = uis.analyze_poster_geometry(boxes)
        assert result["ok"] is False
        assert result["states_uniform"] is False

    def test_empty_boxes_is_a_failure_not_a_crash(self):
        result = uis.analyze_poster_geometry([])
        assert result["ok"] is False
        assert result["count"] == 0


class TestComputeContainBox:
    # T21 §8.4: pure object-fit:contain + object-position:center math,
    # exercised without a browser -- fed real DOM measurements by
    # check_backdrop_hero (real-Chromium coverage in test_six_sizes_eight_pages).
    def test_wider_image_than_container_letterboxes_top_and_bottom(self):
        # 2.39:1 image inside a 16:9 container -- scaled to fit width,
        # leaving equal gaps above and below.
        box = uis.compute_contain_box(1600, 900, 2390, 1000)
        assert box["width"] == pytest.approx(1600, abs=0.01)
        assert box["height"] == pytest.approx(1600 * 1000 / 2390, abs=0.01)
        assert box["x"] == pytest.approx(0, abs=0.01)
        assert box["y"] > 0
        # entirely inside the container
        assert box["y"] + box["height"] <= 900 + 0.01

    def test_narrower_image_than_container_letterboxes_left_and_right(self):
        # 1:1 image inside a 16:9 container -- scaled to fit height.
        box = uis.compute_contain_box(1600, 900, 1000, 1000)
        assert box["height"] == pytest.approx(900, abs=0.01)
        assert box["width"] == pytest.approx(900, abs=0.01)
        assert box["x"] > 0
        assert box["y"] == pytest.approx(0, abs=0.01)

    def test_exact_ratio_match_fills_container_with_no_letterbox(self):
        box = uis.compute_contain_box(1600, 900, 1920, 1080)
        assert box["width"] == pytest.approx(1600, abs=0.01)
        assert box["height"] == pytest.approx(900, abs=0.01)
        assert box["x"] == pytest.approx(0, abs=0.01)
        assert box["y"] == pytest.approx(0, abs=0.01)

    def test_missing_dimensions_return_a_zero_box_not_a_crash(self):
        assert uis.compute_contain_box(0, 900, 1000, 1000) == {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
        assert uis.compute_contain_box(1600, 900, 0, 0) == {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}


class TestRectsOverlap:
    def test_true_for_intersecting_xywh_boxes(self):
        a = {"x": 0, "y": 0, "width": 100, "height": 100}
        b = {"x": 50, "y": 50, "width": 100, "height": 100}
        assert uis.rects_overlap(a, b) is True

    def test_false_for_disjoint_xywh_boxes(self):
        a = {"x": 0, "y": 0, "width": 100, "height": 100}
        b = {"x": 200, "y": 200, "width": 100, "height": 100}
        assert uis.rects_overlap(a, b) is False

    def test_false_for_boxes_only_touching_at_an_edge(self):
        a = {"x": 0, "y": 0, "width": 100, "height": 100}
        b = {"x": 100, "y": 0, "width": 100, "height": 100}
        assert uis.rects_overlap(a, b) is False

    def test_accepts_getboundingclientrect_shaped_boxes(self):
        a = {"left": 0, "top": 0, "right": 100, "bottom": 100}
        b = {"left": 50, "top": 50, "right": 150, "bottom": 150}
        assert uis.rects_overlap(a, b) is True


class TestBackdropHeroViolations:
    def _result(self, size, key, height, violations=None):
        return {"size": size, "key": key, "hero_height": height, "violations": violations or []}

    def test_collects_every_individual_violation(self):
        results = [self._result("1440x900", "16x9", 400, ["object-fit is cover"])]
        assert uis.backdrop_hero_violations(results) == ["object-fit is cover"]

    def test_fallback_height_mismatch_is_flagged(self):
        results = [
            self._result("1440x900", "fallback", 400),
            self._result("1440x900", "16x9", 410),
        ]
        violations = uis.backdrop_hero_violations(results)
        assert len(violations) == 1
        assert "fallback hero height 400" in violations[0]

    def test_fallback_height_match_within_tolerance_is_clean(self):
        results = [
            self._result("1440x900", "fallback", 400.2),
            self._result("1440x900", "16x9", 400.0),
        ]
        assert uis.backdrop_hero_violations(results) == []

    def test_no_fallback_entry_for_a_size_skips_the_comparison(self):
        results = [self._result("1024x768", "16x9", 300)]
        assert uis.backdrop_hero_violations(results) == []


class TestLinkcheckViolations:
    def test_collects_every_individual_violation(self):
        results = [
            {"size": "1440x900", "violations": ["colour mismatch"]},
            {"size": "390x844", "violations": []},
        ]
        assert uis.linkcheck_violations(results) == ["colour mismatch"]

    def test_no_violations_is_empty(self):
        results = [{"size": "1440x900", "violations": []}]
        assert uis.linkcheck_violations(results) == []


class TestSeedLinkcheckFixturesForScreenshots:
    def test_seeds_two_media_and_link_check_rows(self, tmp_path):
        import library_store

        store = library_store.LibraryStore(tmp_path / "media-library.db")
        store.create_schema()
        uis._seed_linkcheck_fixtures_for_screenshots(store)

        assert uis._linkcheck_media_id is not None
        assert uis._linkcheck_all_invalid_media_id is not None
        assert uis._linkcheck_media_id != uis._linkcheck_all_invalid_media_id
        assert set(uis._linkcheck_link_ids) == {"115", "quark", "tianyicloud", "all_invalid"}

        conn = store.connect()
        try:
            rows = conn.execute("SELECT provider, status, reason FROM link_check ORDER BY provider").fetchall()
        finally:
            conn.close()
        by_provider = {r["provider"]: (r["status"], r["reason"]) for r in rows}
        assert by_provider["quark"] == ("invalid", "share_expired")
        assert by_provider["tianyicloud"] == ("unknown", "anti_bot")
        # the all-invalid media's own link also has provider "115" -- same
        # key as the mixed media's plain link, but that one was never
        # written to link_check (status=None), so exactly one "115" row
        # exists here.
        assert by_provider["115"] == ("invalid", "file_deleted")

    def test_never_writes_a_row_for_the_plain_unchecked_link(self, tmp_path):
        import library_store

        store = library_store.LibraryStore(tmp_path / "media-library.db")
        store.create_schema()
        uis._seed_linkcheck_fixtures_for_screenshots(store)
        conn = store.connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM link_check").fetchone()[0]
        finally:
            conn.close()
        # quark (invalid) + tianyicloud (unknown) + the all-invalid media's
        # own link (invalid) == 3; the mixed media's plain "115" link is
        # deliberately left unchecked.
        assert count == 3


class TestGenerateBorderedFixturePng:
    def test_returns_a_valid_png_of_the_requested_size(self):
        data = uis._generate_bordered_fixture_png(64, 48)
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        # IHDR immediately follows the signature -- width/height are its
        # first two 4-byte big-endian fields.
        import struct as _struct
        width, height = _struct.unpack(">II", data[16:24])
        assert (width, height) == (64, 48)

    def test_cache_returns_the_same_bytes_for_a_known_ratio_key(self):
        first = uis._backdrop_fixture_png("16x9")
        second = uis._backdrop_fixture_png("16x9")
        assert first == second
        assert first is not None

    def test_unknown_key_returns_none(self):
        assert uis._backdrop_fixture_png("not-a-real-key") is None


class TestEnsureRoomyProcessTmpdir:
    """A full/tiny ``/tmp`` has been observed in this container to fail
    even plain stdout writes (not just Chromium's shared-memory temp
    files) -- ``_ensure_roomy_process_tmpdir`` (called at ``ui_screenshots``
    import time) redirects ``$TMPDIR`` for the *whole process* in that
    case, not just the spawned browser subprocess (see ``_browser_env``)."""

    def test_redirects_tmpdir_when_default_tempdir_is_small(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setattr(uis.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(uis.shutil, "disk_usage", lambda path: type("U", (), {"total": 16 * 1024 * 1024})())
        uis._ensure_roomy_process_tmpdir()
        assert os.environ["TMPDIR"] == str(uis.PYTEST_TMP / "process-tmp")
        assert (uis.PYTEST_TMP / "process-tmp").is_dir()

    def test_leaves_tmpdir_alone_when_default_tempdir_is_roomy(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setattr(uis.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(uis.shutil, "disk_usage", lambda path: type("U", (), {"total": 50 * 1024 * 1024 * 1024})())
        uis._ensure_roomy_process_tmpdir()
        assert "TMPDIR" not in os.environ

    def test_never_overrides_an_explicit_caller_choice(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", "/some/explicit/choice")
        uis._ensure_roomy_process_tmpdir()
        assert os.environ["TMPDIR"] == "/some/explicit/choice"


class TestChromiumAvailable:
    def test_returns_bool_and_nonempty_message(self):
        ok, message = uis.chromium_available()
        assert isinstance(ok, bool)
        assert isinstance(message, str) and message


@pytest.mark.slow
def test_homepage_screenshot_and_keyboard_walkthrough(tmp_path):
    ok, message = uis.ensure_chromium()
    assert ok, f"chromium unavailable in this environment: {message}"

    from playwright.sync_api import sync_playwright

    size_dir = tmp_path / "ui" / "1440x900"

    with uis.running_app() as base_url:
        with sync_playwright() as p:
            browser = uis.launch_chromium(p)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = context.new_page()
                uis.install_network_guard(page, base_url)
                uis.capture_pages(page, base_url, size_dir, pages=("home",), size_label="1440x900")
                keyboard = uis.run_keyboard_walkthrough(page)
                context.close()
            finally:
                browser.close()

    home_png = size_dir / "home.png"
    assert home_png.exists()
    assert home_png.stat().st_size > 0
    assert keyboard["tab_order_reaches_query"] is True
    assert keyboard["tab_order_reaches_segmented"] is True
    assert keyboard["tab_order_reaches_search_button"] is True
    assert keyboard["tab_order_reaches_result_card"] is True


@pytest.mark.slow
def test_six_sizes_eight_pages():
    """T5.8/T16 deliverable: the real end-to-end run -- exactly the six RE0
    breakpoints x eight page states (T16 §5 adds the filters popover),
    48 PNGs under ``build/ui-screenshots/<WxH>/<page>.png`` plus
    ``report.json`` with no horizontal overflow, a fully-passing keyboard
    walkthrough, passing contrast checks, a fully-passing filters-popover
    acceptance check at every breakpoint, and a fully-passing library-tab
    reset check from all five states. Never skips: fails loudly if
    Chromium cannot launch in this environment."""
    ok, message = uis.ensure_chromium()
    assert ok, f"chromium unavailable in this environment: {message}"

    out_dir = uis.ROOT / "build" / "ui-screenshots"
    report = uis.run_full_capture(out_dir)

    assert report["sizes"] == [f"{width}x{height}" for width, height in uis.SIZES]
    assert report["pages"] == list(uis.ALL_PAGES)

    for width, height in uis.SIZES:
        for page_name in uis.ALL_PAGES:
            png_path = out_dir / f"{width}x{height}" / f"{page_name}.png"
            assert png_path.exists(), f"missing {png_path}"
            assert png_path.stat().st_size > 1024, f"{png_path} is suspiciously small"

    # T19: the reauth QR dialog is captured at only two of the six sizes
    # (brief §8.2) -- not part of the ALL_PAGES/SIZES matrix above.
    assert uis.REAUTH_CAPTURE_SIZES == ("1440x900", "390x844")
    for label in uis.REAUTH_CAPTURE_SIZES:
        png_path = out_dir / label / "reauth.png"
        assert png_path.exists(), f"missing {png_path}"
        assert png_path.stat().st_size > 1024, f"{png_path} is suspiciously small"

    # I3 (§14.6): the overflow-stress detail page (long title/remark,
    # deleted-row, all-deleted-group) is captured at ALL six sizes, unlike
    # the two-size reauth capture above.
    for width, height in uis.SIZES:
        png_path = out_dir / f"{width}x{height}" / "detail-overflow.png"
        assert png_path.exists(), f"missing {png_path}"
        assert png_path.stat().st_size > 1024, f"{png_path} is suspiciously small"

    report_path = out_dir / "report.json"
    assert report_path.exists()
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk == report

    overflow_failures = [item for item in report["overflow"] if not item["ok"]]
    assert not overflow_failures, f"horizontal overflow detected: {overflow_failures}"
    # +len(uis.SIZES): the detail-overflow capture above adds one more
    # overflow check per size, on top of the ALL_PAGES/SIZES matrix.
    # +len(BACKDROP_CAPTURE_SIZES)*(4 ratio fixtures + 1 fallback): T21
    # §8.4's own backdrop captures add one overflow check per key per
    # (smaller) BACKDROP_CAPTURE_SIZES subset of sizes.
    backdrop_keys_count = len(uis.BACKDROP_FIXTURE_RATIOS) + 1
    assert len(report["overflow"]) == (
        len(uis.SIZES) * len(uis.ALL_PAGES) + len(uis.SIZES)
        + len(uis.BACKDROP_CAPTURE_SIZES) * backdrop_keys_count
    )

    keyboard = report["keyboard"]
    assert keyboard["tab_order_reaches_query"] is True
    assert keyboard["tab_order_reaches_segmented"] is True
    assert keyboard["tab_order_reaches_search_button"] is True
    assert keyboard["tab_order_reaches_result_card"] is True
    assert keyboard["tab_order_matches_visual_order"] is True
    assert keyboard["enter_opens_detail"] is True
    assert keyboard["escape_closes_dialog"] is True
    assert keyboard["escape_returns_focus_to_trigger"] is True
    assert keyboard["tab_aria_selected_updates"] is True
    # x1-provider-ui fix wave 1, item 3: the provider-logo tablist's roving
    # tabindex/aria-selected keyboard behaviour and its actual filtering
    # effect on the resource list.
    assert keyboard["provider_tab_has_multiple_providers"] is True
    assert keyboard["provider_tab_arrow_right_moves_selection"] is True
    assert keyboard["provider_tab_arrow_right_moves_focus"] is True
    assert keyboard["provider_tab_end_selects_last_tab"] is True
    assert keyboard["provider_tab_home_selects_first_tab"] is True
    assert keyboard["provider_tab_activation_filters_group_links"] is True

    contrast_failures = [item for item in report["contrast"] if not item["pass"]]
    assert not contrast_failures, f"contrast check(s) failed: {contrast_failures}"

    # x1-provider-ui fix wave 1, item 1: every card sharing a grid row (home
    # rails, search results) must come out exactly the same height.
    assert report["card_heights_uniform"]["rails"] is True
    assert report["card_heights_uniform"]["results"] is True

    # y1-cards §2.3: poster-geometry auto-acceptance across all six
    # viewports x {home rails, search results}.
    geometry = report["geometry"]
    assert not geometry["violations"], f"poster-geometry violations: {geometry['violations']}"
    assert geometry["ok"] is True
    expected_keys = {f"{w}x{h}:{page}" for w, h in uis.SIZES for page in ("home", "search")}
    assert set(geometry["per_viewport"].keys()) == expected_keys
    for key, entry in geometry["per_viewport"].items():
        assert entry["analysis"]["ok"] is True, f"{key}: {entry['analysis']}"
        # Every measurement must include the synthetic "loading" sample
        # (y1-cards §2.3 item 4) alongside the page's real image/fallback
        # cards -- see the module comment above _POSTER_BOX_PROBE.
        assert "loading" in entry["analysis"]["states"], f"{key}: {entry['analysis']}"

    # T16 §6.3: the filters popover, checked at all six breakpoints --
    # fully inside the viewport, above the poster wall, Escape closes it
    # and returns focus, and body scroll is locked only at <=650px.
    filters_popover = report["filters_popover"]
    assert not filters_popover["violations"], f"filters popover violations: {filters_popover['violations']}"
    assert filters_popover["ok"] is True
    assert set(filters_popover["per_viewport"].keys()) == set(report["sizes"])
    mobile_sizes = {f"{w}x{h}" for w, h in uis.SIZES if w <= 650}
    for size_label, entry in filters_popover["per_viewport"].items():
        assert entry["fully_in_viewport"] is True, f"{size_label}: {entry}"
        assert entry["control_hit_by_element_from_point"] is True, f"{size_label}: {entry}"
        assert entry["escape_closes"] is True, f"{size_label}: {entry}"
        assert entry["escape_returns_focus_to_toggle"] is True, f"{size_label}: {entry}"
        assert entry["mobile_body_scroll_locked"] is (size_label in mobile_sizes), f"{size_label}: {entry}"

    # T16 §3: clicking 资源库 from five different states always resets to
    # a clean home view.
    library_tab_reset = report["library_tab_reset"]
    assert not library_tab_reset["violations"], f"library tab reset violations: {library_tab_reset['violations']}"
    assert library_tab_reset["ok"] is True
    assert set(library_tab_reset["scenarios"].keys()) == {
        "from_search_results", "from_detail", "from_filters_open", "from_openlist", "from_strm",
    }
    for scenario, entry in library_tab_reset["scenarios"].items():
        assert all(entry.values()), f"{scenario}: {entry}"

    # T18 §14.1: provider isolation is a data-level fetch contract, proven
    # against the real rendered DOM + accessibility tree.
    provider_isolation = report["provider_isolation"]
    assert not provider_isolation["violations"], f"provider isolation violations: {provider_isolation['violations']}"
    assert provider_isolation["ok"] is True
    assert provider_isolation["filtered_dom_excludes_other_provider"] is True
    assert provider_isolation["filtered_accessibility_tree_excludes_other_provider"] is True
    assert provider_isolation["unfiltered_view_shows_other_provider"] is True
    # I1: the in-page tab switch (selectProvider, not just a URL-driven
    # open()) must be equally isolated in the real rendered DOM/a11y tree.
    assert provider_isolation["tab_switch_dom_excludes_other_provider"] is True
    assert provider_isolation["tab_switch_accessibility_tree_excludes_other_provider"] is True

    # T21 §8.4: layered detail-page backdrop -- 4 different-ratio fixtures
    # (16:9, 4:3, 2.39:1, 1:1) plus the no-backdrop fallback, at the five
    # sizes the brief names, each with its own PNG.
    backdrop = report["backdrop"]
    assert not backdrop["violations"], f"backdrop violations: {backdrop['violations']}"
    assert backdrop["ok"] is True
    assert backdrop["sizes"] == list(uis.BACKDROP_CAPTURE_SIZES)
    expected_keys = set(uis.BACKDROP_FIXTURE_RATIOS) | {"fallback"}
    assert {r["key"] for r in backdrop["results"]} == expected_keys
    assert len(backdrop["results"]) == len(uis.BACKDROP_CAPTURE_SIZES) * len(expected_keys)
    for entry in backdrop["results"]:
        assert entry["no_cls"] is True, entry
        assert entry["info_below_backdrop"] is True, entry
        assert entry["poster_info_overlap"] is False, entry
        assert entry["decorative_layers_hidden"] is True, entry
        assert entry["empty_state_ok"] is True, entry
        if entry["key"] == "fallback":
            assert entry["image_present"] is False, entry
        else:
            assert entry["image_present"] is True, entry
            assert entry["object_fit_contain"] is True, entry
            assert entry["corners_inside_container"] is True, entry
            assert entry["ratio_matches_fixture"] is True, entry
            assert entry["alt_in_accessibility_tree"] is True, entry

    # Follow-up: the un-capped aspect-ratio height dominated the first
    # screen at desktop sizes (774/684/549px) -- max-height:520px caps
    # 1440x900/1280x800/1024x768 at 520px; the mobile min-height (210px)
    # override is well under the cap already, so it is untouched. Fallback
    # height still equals the loaded-state height at every size (already
    # covered by backdrop["violations"] being empty above).
    desktop_capped_sizes = {"1440x900", "1280x800", "1024x768"}
    hero_heights_by_size: dict[str, set[float]] = {}
    for entry in backdrop["results"]:
        hero_heights_by_size.setdefault(entry["size"], set()).add(entry["hero_height"])
    for size_label, heights in hero_heights_by_size.items():
        assert len(heights) == 1, f"{size_label}: hero height differs across keys: {heights}"
        height = next(iter(heights))
        if size_label in desktop_capped_sizes:
            assert height == pytest.approx(520, abs=0.5), f"{size_label}: expected the 520px cap, got {height}"
        else:
            assert height < 520, f"{size_label}: expected an uncapped mobile height, got {height}"
    for size_label in uis.BACKDROP_CAPTURE_SIZES:
        for key in expected_keys:
            png_path = out_dir / size_label / f"detail-backdrop-{key}.png"
            assert png_path.exists(), f"missing {png_path}"
            assert png_path.stat().st_size > 256, f"{png_path} is suspiciously small"

    # w6-contract §UI item 5: link validity check states, at the reduced
    # LINKCHECK_CAPTURE_SIZES subset -- badge colour, no overflow, keyboard
    # reachability of 重新检测, the card corner badge, and the settings
    # card's four provider rows, all against the real app.js with only the
    # seeded fixtures' own network responses patched.
    linkcheck = report["linkcheck"]
    assert not linkcheck["violations"], f"link-check UI violations: {linkcheck['violations']}"
    assert linkcheck["ok"] is True
    assert linkcheck["sizes"] == list(uis.LINKCHECK_CAPTURE_SIZES)
    assert {r["size"] for r in linkcheck["results"]} == set(uis.LINKCHECK_CAPTURE_SIZES)
    for entry in linkcheck["results"]:
        assert entry["badge_color_matches_danger"] is True, entry
        assert entry["no_overflow"] is True, entry
        assert entry["recheck_keyboard_reachable"] is True, entry
        assert entry["card_invalid_badge_present"] is True, entry
        assert entry["settings_provider_rows"] == 4, entry
    for size_label in uis.LINKCHECK_CAPTURE_SIZES:
        for page_name in ("detail-linkcheck", "card-invalid-badge", "settings-linkcheck"):
            png_path = out_dir / size_label / f"{page_name}.png"
            assert png_path.exists(), f"missing {png_path}"
            assert png_path.stat().st_size > 1024, f"{png_path} is suspiciously small"


def test_capture_search_closes_suggestion_dropdown_before_screenshot():
    # The suggest fetch is debounced (200ms) independently of the search
    # click's own hideSuggest() call, so a suggest response that resolves
    # after the click can re-show #librarySuggest right before the
    # screenshot is taken (T7 §9). _capture_search must close it via a
    # click elsewhere + blur -- never Escape any more (T8 #2): #libraryQuery
    # is type="search", and Chromium clears a search input's value on
    # Escape, which used to blank the query text out of search.png.
    import inspect

    source = inspect.getsource(uis._capture_search)
    assert "blur()" in source, (
        "expected _capture_search to blur the query input to close the "
        "suggestion dropdown before taking the screenshot"
    )
    assert "Escape" not in source, (
        "Escape clears a type=\"search\" input's value in Chromium -- "
        "_capture_search must not rely on it any more"
    )
    # The debounced suggest fetch can still be pending when the
    # click+blur runs -- it must wait out the debounce window first, or a
    # response landing afterwards re-shows #librarySuggest right before
    # the screenshot (found by actually running the real capture).
    wait_pos = source.index("wait_for_timeout")
    click_pos = source.index("document.body.click()")
    assert wait_pos < click_pos, (
        "expected _capture_search to wait out the suggest debounce before "
        "clicking away to close the dropdown"
    )


def test_run_keyboard_walkthrough_report_carries_no_link_id():
    # T8 #13: report.json must stay to booleans/ids of DOM elements only
    # (module docstring) -- a resource_link's public id is application
    # data, not a DOM element id, and has no place in it.
    import inspect

    source = inspect.getsource(uis.run_keyboard_walkthrough)
    assert "transfer_trigger_link_id" not in source


def test_ensure_chromium_reports_missing_playwright_module(monkeypatch):
    """A missing playwright package must yield (False, message), never raise
    -- callers (main() and the slow tests above) turn that into a clear
    failure instead of an opaque traceback."""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    ok, message = uis.ensure_chromium()
    assert ok is False
    assert "playwright" in message.lower()
