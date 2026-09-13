"""The response matrix each authentication mode actually produces.

Review 2026-09-12 R14: the Phase 8 work order claimed `app` mode answers 200
on the public root. It answers 302 to /login. A release bridge built on the
wrong claim would roll back a healthy deployment, so the matrix is pinned
here and the documents quote this test rather than an assumption.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HTML = {"Accept": "text/html"}
ANY = {"Accept": "*/*"}


@pytest.fixture
def mode(hidrive, monkeypatch):
    def _set(name):
        monkeypatch.setattr(hidrive, "AUTH_MODE", name)
        monkeypatch.setattr(
            hidrive, "verify_access_jwt",
            lambda token: {"sub": "owner", "email": hidrive.ADMIN_EMAIL} if token == "valid-assertion"
            else (_ for _ in ()).throw(PermissionError("bad")),
        )
        return hidrive
    return _set


class TestAppMode:
    def test_a_browser_at_the_root_is_sent_to_our_own_login_page(self, client, mode):
        mode("app")
        response = client.get("/", headers=HTML)
        assert response.status_code == 302
        assert response.headers["Location"] == "/login", \
            "the redirect target is what distinguishes this from a Cloudflare bounce"

    def test_an_api_client_at_the_root_gets_json_not_a_redirect(self, client, mode):
        mode("app")
        response = client.get("/", headers=ANY)
        assert response.status_code == 401
        assert response.get_json()["code"] == "LOGIN_REQUIRED"

    def test_the_login_page_is_public_and_is_the_login_page(self, client, mode):
        mode("app")
        response = client.get("/login", headers=HTML)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'id="loginForm"' in body and 'data-page="login"' in body, \
            "a 200 is only meaningful if it is actually the sign-in page"

    def test_an_anonymous_api_call_is_refused_in_json(self, client, mode):
        mode("app")
        response = client.get("/api/me")
        assert response.status_code == 401
        assert response.headers["Content-Type"].startswith("application/json")

    def test_an_anonymous_administrator_api_is_still_refused(self, client, mode):
        mode("app")
        for url in ("/api/settings", "/api/openlist/list", "/api/admin/users"):
            assert client.open(url, method="GET").status_code in {401, 403}, url

    def test_healthz_answers_without_an_assertion(self, client, mode):
        mode("app")
        assert client.get("/healthz").status_code == 200


class TestAccessMode:
    def test_the_whole_site_still_needs_an_assertion(self, client, mode):
        mode("access")
        assert client.get("/", headers=HTML).status_code == 401
        assert client.get("/api/me").status_code == 401

    def test_healthz_needs_one_too(self, client, mode):
        mode("access")
        assert client.get("/healthz").status_code == 401
        assert client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"}).status_code == 200

    def test_an_assertion_gets_the_administrator_in(self, client, mode):
        mode("access")
        response = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"})
        assert response.status_code == 200


class TestHybridMode:
    def test_either_way_in_works_for_the_administrator(self, client, mode):
        mode("hybrid")
        assert client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": "valid-assertion"}).status_code == 200

    def test_and_neither_works_for_nobody(self, client, mode):
        mode("hybrid")
        assert client.get("/api/me").status_code == 401

    def test_healthz_answers_without_an_assertion(self, client, mode):
        mode("hybrid")
        assert client.get("/healthz").status_code == 200


class TestTheDocumentsQuoteThis:
    """The matrix lives in three places; they must agree with the test."""

    @pytest.mark.parametrize("path", [
        "DEPLOYMENT.md",
        "docs/release-bridge-contract.md",
        "docs/codex-release-bridge-multiuser-20260911.md",
    ])
    def test_no_document_still_claims_the_root_answers_200_under_app(self, path):
        if not (ROOT / path).exists():
            pytest.skip("Internal deployment document is excluded from the public distribution")
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "302" in text and "/login" in text
        for wrong in ("`app` 模式下应为 `200`（登录页）",
                      "Under `app` it must answer `200` with the\nsign-in page"):
            assert wrong not in text, f"{path} still carries the corrected claim"


# ---------------------------------------------------------------------------
# F09: the bridge's health rules, stated once and executable
# ---------------------------------------------------------------------------

CONTRACT = ROOT / "docs" / "release-bridge-contract.md"


def _health_section() -> str:
    if not CONTRACT.exists():
        pytest.skip("Internal release-bridge contract is excluded from the public distribution")
    text = CONTRACT.read_text(encoding="utf-8")
    start = text.index("## 9. 健康检查")
    return text[start:text.index("## 9A.", start)]


class TestTheBridgeHealthRulesDoNotContradictThemselves:
    """F09: §9 used to call a 200 on `/healthz` a failure and roll back, while
    §4's matrix says 200 is correct under hybrid and app. A bridge built on
    the first would roll back a healthy deployment."""

    def test_the_local_check_is_stated_per_mode(self):
        section = _health_section()
        for mode_name in ("`access`", "`hybrid`", "`app`"):
            assert mode_name in section, mode_name
        assert "都是正确的" in section, "the section must say both values are correct, per mode"

    def test_no_blanket_claim_that_200_on_healthz_fails(self):
        section = _health_section()
        # The old rule, verbatim: a single failure column naming 200 with no
        # mode qualifier, immediately after the /healthz request.
        assert "200（说明 `HIDRIVE_AUTH_MODE` 不是 access）" not in section

    @pytest.mark.parametrize("mode_name,expected", [("access", 401), ("hybrid", 200), ("app", 200)])
    def test_the_documented_local_answer_is_the_real_one(self, client, mode, mode_name, expected):
        """The conditions the document states, executed against the app."""
        mode(mode_name)
        assert client.get("/healthz").status_code == expected

    def test_the_document_states_exactly_those_three_answers(self):
        section = _health_section()
        rows = [line for line in section.splitlines()
                if line.startswith("| `access`") or line.startswith("| `hybrid`") or line.startswith("| `app`")]
        assert len(rows) >= 3, rows
        local = {line.split("|")[1].strip(): line.split("|")[2].strip() for line in rows[:3]}
        assert "401" in local["`access`"]
        assert "200" in local["`hybrid`"] and "200" in local["`app`"]

    def test_the_public_rules_name_the_redirect_target_not_just_the_status(self):
        section = _health_section()
        assert "cloudflareaccess.com" in section
        assert "Location" in section
        assert "/login" in section
        assert "curl -L" in section, "the section must still warn against erasing the target"

    def test_it_separates_the_application_from_the_edge(self):
        section = _health_section()
        assert "Cloudflare 边缘" in section and "应用测试客户端" in section

    @pytest.mark.parametrize("mode_name", ["access", "hybrid", "app"])
    def test_an_anonymous_api_call_is_json_401_in_every_mode(self, client, mode, mode_name):
        """One rule the document states unconditionally, checked unconditionally."""
        mode(mode_name)
        response = client.get("/api/me", headers=ANY)
        assert response.status_code == 401
        assert response.get_json()["success"] is False
        assert response.get_json().get("code"), response.get_json()

    def test_the_app_mode_login_page_is_really_the_login_page(self, client, mode):
        """The decisive `app` check the document names: 200 *and* the content
        is this site's sign-in page, not any 200 error page."""
        mode("app")
        response = client.get("/login", headers=HTML)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'data-page="login"' in body


# ---------------------------------------------------------------------------
# G07: the application table and the edge table are different tables
# ---------------------------------------------------------------------------


def _contract_section(heading: str, until: str) -> str:
    if not CONTRACT.exists():
        pytest.skip("Internal release-bridge contract is excluded from the public distribution")
    text = CONTRACT.read_text(encoding="utf-8")
    start = text.index(heading)
    return text[start:text.index(until, start)]


_APP_MATRIX = {
    # (mode, path, Accept) -> status
    ("access", "/", "text/html"): 401,
    ("access", "/", "*/*"): 401,
    ("access", "/login", "text/html"): 401,
    ("access", "/api/me", "*/*"): 401,
    ("access", "/healthz", "*/*"): 401,
    ("hybrid", "/", "text/html"): 302,
    ("hybrid", "/", "*/*"): 401,
    ("hybrid", "/login", "text/html"): 200,
    ("hybrid", "/api/me", "*/*"): 401,
    ("hybrid", "/healthz", "*/*"): 200,
    ("app", "/", "text/html"): 302,
    ("app", "/", "*/*"): 401,
    ("app", "/login", "text/html"): 200,
    ("app", "/api/me", "*/*"): 401,
    ("app", "/healthz", "*/*"): 200,
}


class TestTheApplicationTableIsTheApplication:
    """G07: §4's single table was labelled "measured application responses"
    while its `access` + HTML cell described the Cloudflare edge. Any bridge
    judgement taken from that cell is wrong: the application answers 401."""

    @pytest.mark.parametrize("key,expected", sorted(_APP_MATRIX.items()))
    def test_every_cell_is_what_the_application_answers(self, client, mode, key, expected):
        mode_name, path, accept = key
        mode(mode_name)
        response = client.get(path, headers={"Accept": accept})
        assert response.status_code == expected, (key, response.status_code)
        if expected == 302:
            assert response.headers["Location"] == "/login", response.headers.get("Location")

    def test_the_application_table_no_longer_claims_a_cloudflare_redirect(self):
        section = _contract_section("### 4.1 应用自身的响应", "### 4.2")
        assert "cloudflareaccess.com" not in section, (
            "the application table must not describe edge behaviour")
        assert "Cloudflare 登录" not in section
        # The corrected `access` row.
        access_row = [line for line in section.splitlines() if line.startswith("| `GET /`（`Accept: text/html`）")]
        assert access_row and "401" in access_row[0].split("|")[2], access_row

    def test_the_edge_table_says_it_is_not_measured(self):
        section = _contract_section("### 4.2 公网经过 Cloudflare 的预期行为", "## 5.")
        assert "待现场验证" in section
        assert "预期" in section and "不是本轮的测量结果" in section
        assert "cloudflareaccess.com" in section, "the edge table is where the Cloudflare host belongs"

    def test_both_tables_name_what_a_judgement_needs(self):
        section = _contract_section("### 4.1 应用自身的响应", "## 5.")
        for needed in ("Accept", "Location", "断言", "跳数", "curl -L"):
            assert needed in section, needed

    @pytest.mark.parametrize("path", ["DEPLOYMENT.md", "docs/codex-release-bridge-multiuser-20260911.md"])
    def test_the_other_documents_no_longer_conflict(self, path):
        if not (ROOT / path).exists():
            pytest.skip("Internal deployment document is excluded from the public distribution")
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "302 to the Cloudflare login | 302 to `/login` or Cloudflare" not in text
        # Each must say, somewhere, that the two measurements are different.
        assert ("Cloudflare edge" in text or "Cloudflare 边缘" in text), path
