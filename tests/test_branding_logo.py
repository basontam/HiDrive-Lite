"""Brand identity: the tab icon set and the horizontal logo in the topbar
(docs/claude-hidrive-lite-logo-construction-20260911.md).

The assets themselves are design source, copied into the runtime static
directory byte for byte -- these tests pin that they are still identical,
that every icon URL the page declares actually serves, and that the topbar
carries the logo (with its accessible name) instead of the old text title,
without losing the subtitle or restoring the removed authentication badge.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "docs" / "branding" / "hidrive-lite-logo-20260911"
RUNTIME_DIR = ROOT / "static" / "branding" / "hidrive-lite"
ASSET_VERSION_TAG = "v=20260911"

ASSETS = (
    "hidrive-lite-logo.svg",
    "hidrive-lite-logo-on-dark.svg",
    "hidrive-lite-mark.svg",
    "favicon.svg",
    "favicon-16.png",
    "favicon-32.png",
    "favicon-48.png",
    "favicon-512.png",
    "apple-touch-icon.png",
)

EXPECTED_PNG_SIZES = {
    "favicon-16.png": (16, 16),
    "favicon-32.png": (32, 32),
    "favicon-48.png": (48, 48),
    "favicon-512.png": (512, 512),
    "apple-touch-icon.png": (180, 180),
}


@pytest.fixture
def page(client):
    response = client.get("/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _png_size(path: Path) -> tuple[int, int]:
    blob = path.read_bytes()
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", blob[16:24])


# ---------------------------------------------------------------------------
# the installed assets
# ---------------------------------------------------------------------------


class TestAssets:
    @pytest.mark.parametrize("name", ASSETS)
    def test_every_asset_is_installed_byte_for_byte(self, name):
        assert (RUNTIME_DIR / name).read_bytes() == (SOURCE_DIR / name).read_bytes()

    @pytest.mark.parametrize("name,size", sorted(EXPECTED_PNG_SIZES.items()))
    def test_each_png_has_the_size_it_claims(self, name, size):
        assert _png_size(RUNTIME_DIR / name) == size

    @pytest.mark.parametrize("name", [n for n in ASSETS if n.endswith(".svg")])
    def test_each_svg_parses_and_scales(self, name):
        import xml.etree.ElementTree as ET

        root = ET.parse(RUNTIME_DIR / name).getroot()
        assert root.get("viewBox"), f"{name} must keep a viewBox to stay scalable"

    @pytest.mark.parametrize("name", [n for n in ASSETS if n.endswith(".svg")])
    def test_no_svg_reaches_out_to_the_network_or_runs_script(self, name):
        text = (RUNTIME_DIR / name).read_text(encoding="utf-8")
        # The SVG namespace declaration is the only w3.org URL allowed.
        without_ns = text.replace("http://www.w3.org/2000/svg", "")
        for forbidden in ("http://", "https://", "<script", "<foreignObject", "@import", "xlink:href"):
            assert forbidden not in without_ns, f"{name} must not carry {forbidden}"

    @pytest.mark.parametrize("name", [n for n in ASSETS if n.endswith(".svg")])
    def test_no_svg_carries_a_credential_or_a_host_path(self, name):
        text = (RUNTIME_DIR / name).read_text(encoding="utf-8")
        for forbidden in ("/data-disk", "/etc/" + "hidrive-lite", "Cookie", "token", "secret"):
            assert forbidden.lower() not in text.lower(), f"{name} must not carry {forbidden}"


# ---------------------------------------------------------------------------
# what the page declares, and whether it serves
# ---------------------------------------------------------------------------


class TestIconDeclarations:
    def test_the_page_declares_the_svg_icon_and_three_png_sizes(self, page):
        head = page.split("</head>")[0]
        assert f'rel="icon" type="image/svg+xml" href="/static/branding/hidrive-lite/favicon.svg?{ASSET_VERSION_TAG}"' in head
        for size in ("16x16", "32x32", "48x48"):
            pixels = size.split("x")[0]
            assert f'sizes="{size}" href="/static/branding/hidrive-lite/favicon-{pixels}.png?{ASSET_VERSION_TAG}"' in head

    def test_the_page_declares_the_apple_touch_icon(self, page):
        assert ('rel="apple-touch-icon" sizes="180x180" '
                f'href="/static/branding/hidrive-lite/apple-touch-icon.png?{ASSET_VERSION_TAG}"') in page

    def test_only_one_set_of_icons_competes_for_the_tab(self, page):
        head = page.split("</head>")[0]
        # One SVG icon, three PNG icons, one apple-touch-icon -- no second
        # declaration of any of them, and no stale favicon.ico left over.
        assert head.count('rel="icon"') == 4
        assert head.count('rel="apple-touch-icon"') == 1
        assert "favicon.ico" not in page

    def test_the_version_tag_is_the_fixed_one_not_a_random_value(self, page):
        for match in re.findall(r"/static/branding/hidrive-lite/[^\"']+", page):
            assert match.endswith(f"?{ASSET_VERSION_TAG}"), match

    @pytest.mark.parametrize("name,mime", [
        ("favicon.svg", "image/svg+xml"),
        ("favicon-16.png", "image/png"),
        ("favicon-32.png", "image/png"),
        ("favicon-48.png", "image/png"),
        ("apple-touch-icon.png", "image/png"),
        ("hidrive-lite-logo.svg", "image/svg+xml"),
        ("hidrive-lite-logo-on-dark.svg", "image/svg+xml"),
    ])
    def test_every_declared_url_serves_with_the_right_type(self, client, name, mime):
        response = client.get(f"/static/branding/hidrive-lite/{name}?{ASSET_VERSION_TAG}")
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith(mime)

    def test_every_branding_url_the_page_names_actually_serves(self, client, page):
        urls = sorted(set(re.findall(r"/static/branding/hidrive-lite/[^\"']+", page)))
        assert urls, "the page must reference the branding assets"
        for url in urls:
            assert client.get(url).status_code == 200, url


# ---------------------------------------------------------------------------
# the topbar
# ---------------------------------------------------------------------------


class TestTopbarLogo:
    def test_the_brand_shows_the_logo_with_its_accessible_name(self, page):
        brand = page.split('<div class="brand">')[1].split("</div>")[0]
        assert 'alt="HiDrive-Lite"' in brand
        assert "hidrive-lite-logo.svg" in brand

    def test_the_dark_variant_is_offered_by_the_system_colour_scheme(self, page):
        brand = page.split('<div class="brand">')[1].split("</div>")[0]
        assert 'media="(prefers-color-scheme: dark)"' in brand
        assert "hidrive-lite-logo-on-dark.svg" in brand

    def test_the_text_title_is_gone_so_the_name_is_not_shown_twice(self, page):
        assert "<h1>HiDrive-Lite</h1>" not in page

    @pytest.mark.parametrize("administrator", [True, False])
    def test_the_subtitle_is_personal_library_for_both_roles(self, hidrive, administrator):
        page = hidrive.app.jinja_env.get_template("index.html").render(
            capabilities={"global_settings": administrator, "openlist": administrator},
            asset_version="fixture", public_origin="https://hidrive.test",
        )
        brand = page.split('<div class="brand">')[1].split("</div>")[0]
        assert re.search(r'<p>([^<]+)</p>', brand).group(1) == "个人影视资源库"

    def test_the_brand_no_longer_has_an_authentication_badge(self, page):
        topbar = page.split('<header class="topbar">')[1].split("</header>")[0]
        assert 'id="actor"' not in topbar
        assert 'access-badge' not in topbar
        assert 'class="brand"' in topbar

    def test_the_logo_is_an_accessible_home_link(self, page):
        brand = page.split('<div class="brand">')[1].split("</div>")[0]
        link = re.search(r'<a\b[^>]*class="brand-home"[^>]*>', brand)
        assert link, "the project logo must link back to the homepage"
        assert 'href="/"' in link.group(0)
        assert 'aria-label="返回首页"' in link.group(0)
        assert "<button" not in brand
        assert "onclick" not in brand

    def test_the_stylesheet_sizes_the_logo_and_drops_the_old_title_rule(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        assert ".brand-logo-image{" in css
        assert ".brand h1{" not in css, "no rule may target a title that is no longer rendered"

    def test_the_logo_shrinks_at_the_narrow_breakpoints(self):
        """The stylesheet already breaks at 640px and 460px, so the logo
        follows those rather than introducing a third width near them."""
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        for breakpoint in ("@media (max-width:640px){", "@media (max-width:460px){"):
            block = css.split(breakpoint)[1].split("\n}")[0]
            assert ".brand-logo-image" in block, breakpoint
