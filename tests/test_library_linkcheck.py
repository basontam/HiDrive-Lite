"""Tests for library_tmdb.py's link-validity checker (w6): adapter
extract_ref/build_request/classify tables, LinkCheckClient (pacing/budget/
pause), check_link, run_link_check_round and BackgroundLinkChecker.

All HTTP goes through a fake, ``requests``-shaped session (FakeSession
below); the shared tests/conftest.py network guard additionally blocks any
real call made through ``requests`` directly. URLs below use real domain
shapes with fake share codes (``swfake...``) and 4-character placeholder
access codes, per the project's test-data rule -- none of these are real
shares.
"""

from __future__ import annotations

import json as jsonlib
import sqlite3
import time as real_time

import pytest
import requests

import library_store as ls
import library_tmdb as tmdb


# --- fakes ------------------------------------------------------------------


class FakeResponse:
    """``requests``-shaped fake response. ``iter_content``/``close`` (I5:
    ``check_link`` now reads the body via ``_read_capped_body``'s
    streaming ``iter_content`` loop, never ``.content``/``.json()``/
    ``.text`` directly) simply hand back ``self.content`` in slices --
    every existing test that constructs one of these keeps working
    unchanged against the new streaming read path."""

    def __init__(self, status_code=200, json_payload=None, text=None, headers=None, content=None):
        self.status_code = status_code
        self._json_payload = json_payload
        self._text = text
        self.headers = headers or {}
        if content is not None:
            self.content = content
        elif json_payload is not None:
            self.content = jsonlib.dumps(json_payload).encode("utf-8")
        elif text is not None:
            self.content = text.encode("utf-8")
        else:
            self.content = b""
        self.closed = False

    def json(self):
        if self._json_payload is None:
            raise ValueError("no JSON payload")
        return self._json_payload

    @property
    def text(self):
        if self._text is not None:
            return self._text
        if self._json_payload is not None:
            return jsonlib.dumps(self._json_payload)
        return ""

    def iter_content(self, chunk_size=8192):
        content = self.content
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def close(self):
        self.closed = True


class FakeStreamingResponse:
    """A stricter I5 fake than ``FakeResponse``: it only supports
    ``iter_content``/``close`` (no ``.content``/``.json()``/``.text`` at
    all) so a test using it fails loudly if ``check_link`` ever falls back
    to eagerly materializing the whole body instead of reading it via the
    capped ``iter_content`` loop. ``chunks_yielded`` lets a test prove
    reading stopped early once the size cap was crossed, rather than
    draining every chunk that was "available"."""

    def __init__(self, status_code, chunks):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.closed = False
        self.chunks_yielded = 0

    def iter_content(self, chunk_size=8192):
        for chunk in self._chunks:
            self.chunks_yielded += 1
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    """Programmable fake for ``LinkCheckClient.dispatch()``'s
    ``session.request(method, url, params=, json=, headers=, timeout=,
    allow_redirects=, stream=)`` call."""

    def __init__(self):
        self._routes: dict[str, object] = {}
        self.calls: list[dict] = []

    def route(self, url, outcome):
        self._routes[url] = outcome
        return self

    def request(
        self, method, url, *, params=None, json=None, headers=None, timeout=None,
        allow_redirects=None, stream=None,
    ):
        self.calls.append(
            {
                "method": method, "url": url, "params": params, "json": json,
                "headers": dict(headers or {}), "timeout": timeout, "allow_redirects": allow_redirects,
                "stream": stream,
            }
        )
        outcome = self._routes.get(url)
        if outcome is None:
            raise AssertionError(f"unscripted request to {url}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    """A settable epoch clock for LinkCheckClient's ``now``/``today``."""

    def __init__(self, epoch: int = 1_700_000_000):
        self.epoch = epoch

    def now(self) -> int:
        return self.epoch

    def today(self) -> str:
        import datetime

        return datetime.datetime.fromtimestamp(self.epoch, tz=datetime.timezone.utc).date().isoformat()


def _client(tmp_path, session, *, settings=None, clock=None, db_name="linkcheck.db"):
    db_path = tmp_path / db_name

    def conn_factory():
        return sqlite3.connect(str(db_path))

    clock = clock or Clock()
    client = tmdb.LinkCheckClient(
        conn_factory=conn_factory, settings=settings, session=session,
        clock=real_time.monotonic, sleep=lambda s: None,
        now=clock.now, today=clock.today,
    )
    return client, conn_factory, clock


# ---------------------------------------------------------------------------
# ensure_tables
# ---------------------------------------------------------------------------


def test_ensure_tables_creates_link_check_tables(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    tmdb.ensure_tables(conn)
    tmdb.ensure_tables(conn)  # idempotent
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"link_check", "link_check_state"} <= names
    conn.close()


# ---------------------------------------------------------------------------
# URL -> share ref extraction
# ---------------------------------------------------------------------------


class TestExtractRef:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://cloud.189.cn/t/swfaket1code", "swfaket1code"),
            ("https://cloud.189.cn/web/share?code=swfaket2code", "swfaket2code"),
            ("https://h5.cloud.189.cn/share/swfaket3code", "swfaket3code"),
        ],
    )
    def test_tianyicloud_forms(self, url, expected):
        assert tmdb.LINK_CHECK_ADAPTERS["tianyicloud"].extract_ref(url) == expected

    def test_tianyicloud_unrecognised_url_returns_none(self):
        assert tmdb.LINK_CHECK_ADAPTERS["tianyicloud"].extract_ref("https://example.com/nope") is None

    def test_quark(self):
        assert tmdb.LINK_CHECK_ADAPTERS["quark"].extract_ref("https://pan.quark.cn/s/swfakequark1") == "swfakequark1"

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://alipan.com/s/swfakealipan1", "swfakealipan1"),
            ("https://aliyundrive.com/s/swfakealipan2", "swfakealipan2"),
            ("https://alywp.net/swfakealipan3", "swfakealipan3"),
        ],
    )
    def test_alipan_forms(self, url, expected):
        assert tmdb.LINK_CHECK_ADAPTERS["alipan"].extract_ref(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://115.com/s/swfake115share1", "swfake115share1"),
            # Calibration round 1: 3,354 of 5,826 live 115 links sit on 115cdn.com
            # and 257 on anxia.com -- same /s/<code> shape, same share.
            ("https://115cdn.com/s/swfake115share2?password=ab12", "swfake115share2"),
            ("https://anxia.com/s/swfake115share3?password=ab12&", "swfake115share3"),
        ],
    )
    def test_115_forms(self, url, expected):
        assert tmdb.LINK_CHECK_ADAPTERS["115"].extract_ref(url) == expected

    @pytest.mark.parametrize(
        "url", ["https://example.com/s/nope", "https://not115.com/s/abc", "https://myanxia.com/s/abc"]
    )
    def test_115_unrecognised_url_returns_none(self, url):
        assert tmdb.LINK_CHECK_ADAPTERS["115"].extract_ref(url) is None


# ---------------------------------------------------------------------------
# build_request: pure, no I/O
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_every_phase1_adapter_is_calibrated_after_round_2(self):
        # Informational flag only -- settings still default every provider
        # OFF; the user switches each one on.
        assert {code: a.calibrated for code, a in tmdb.LINK_CHECK_ADAPTERS.items()} == {
            "tianyicloud": True, "quark": True, "alipan": True, "115": True,
        }


class TestBuildRequest:
    def test_tianyicloud(self):
        method, url, payload, headers = tmdb.LINK_CHECK_ADAPTERS["tianyicloud"].build_request("swfaket1", None)
        assert method == "GET"
        assert url == "https://api.cloud.189.cn/open/share/getShareInfoByCodeV2.action"
        assert payload == {"shareCode": "swfaket1"}
        # Calibration round 1: without an explicit JSON Accept the endpoint
        # answers in XML and every probe classified as parse_error.
        assert headers == {"Accept": "application/json;charset=UTF-8"}

    def test_quark_includes_passcode(self):
        method, url, payload, headers = tmdb.LINK_CHECK_ADAPTERS["quark"].build_request("swfakeq1", "ab12")
        assert method == "POST"
        assert "pr=ucpro" in url and "fr=pc" in url
        assert payload == {"pwd_id": "swfakeq1", "passcode": "ab12"}

    def test_quark_no_access_code_sends_empty_passcode(self):
        _, _, payload, _ = tmdb.LINK_CHECK_ADAPTERS["quark"].build_request("swfakeq2", None)
        assert payload["passcode"] == ""

    def test_alipan(self):
        method, url, payload, headers = tmdb.LINK_CHECK_ADAPTERS["alipan"].build_request("swfakea1", None)
        assert method == "POST"
        assert url == "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous"
        assert payload == {"share_id": "swfakea1"}

    def test_115_with_access_code(self):
        # Calibration round 1: the share page is a JS shell (same HTML for
        # live and cancelled shares) -- the anonymous share/snap JSON
        # endpoint is the probe now, still cookie/token-free.
        method, url, payload, headers = tmdb.LINK_CHECK_ADAPTERS["115"].build_request("swfake115a", "cd34")
        assert method == "GET"
        assert url == "https://webapi.115.com/share/snap"
        assert payload == {"share_code": "swfake115a", "receive_code": "cd34", "offset": 0, "limit": 1}

    def test_115_without_access_code_sends_empty_receive_code(self):
        _, _, payload, _ = tmdb.LINK_CHECK_ADAPTERS["115"].build_request("swfake115b", None)
        assert payload["receive_code"] == ""

    @pytest.mark.parametrize("code", ["tianyicloud", "quark", "alipan", "115"])
    def test_no_adapter_ever_sets_a_cookie_or_authorization_header(self, code):
        # Hard rule: an adapter's own build_request must never carry a
        # Cookie/Authorization header (tianyicloud sends an Accept header,
        # nothing else does), and _scrub_headers (tested below) is the
        # last line of defense regardless.
        _, _, _, headers = tmdb.LINK_CHECK_ADAPTERS[code].build_request("ref", "ab12")
        for name in (headers or {}):
            assert name.lower() not in ("cookie", "authorization")


# ---------------------------------------------------------------------------
# classify: pure, no I/O
# ---------------------------------------------------------------------------


class TestClassifyTianyicloud:
    classify = staticmethod(tmdb._classify_tianyicloud)

    def test_ok(self):
        assert self.classify(200, {"res_code": 0, "res_message": "成功"}) == ("valid", "ok")

    @pytest.mark.parametrize(
        "code,reason",
        [
            ("FileNotFound", "file_deleted"),        # rounds 1-2: page "该页面不存在" (2/2)
            ("ShareNotFound", "share_not_found"),    # round 2: page "该页面不存在" (2/2)
            ("ShareAuditNotPass", "share_audit"),    # round 2: page "该内容审核不通过" (1/1)
        ],
    )
    def test_browser_confirmed_codes_are_invalid(self, code, reason):
        # HTTP 400 + string res_code. res_message (which embeds share/file
        # ids) is never consulted -- an empty one changes nothing.
        assert self.classify(400, {"res_code": code, "res_message": ""}) == ("invalid", reason)

    @pytest.mark.parametrize("code", ["ShareInfoNotFound", "ShareExpiredError", "ShareAuditWaiting", -999])
    def test_unconfirmed_codes_are_unknown_unmapped_never_invalid(self, code):
        # Community-known but never seen/browser-confirmed codes stay
        # "unknown" (they surface in the signals histogram for the next round).
        assert self.classify(400, {"res_code": code, "res_message": "x"}) == ("unknown", "unmapped_response")

    def test_invalid_marker_in_message_alone_is_not_trusted(self):
        # The old message-needle matching is gone: only res_code decides.
        assert self.classify(200, {"res_code": -1, "res_message": "分享不存在 FileNotFound"}) == (
            "unknown", "unmapped_response",
        )

    def test_429_is_unknown_rate_limited(self):
        assert self.classify(429, None) == ("unknown", "rate_limited")

    def test_5xx_is_unknown_http_5xx(self):
        assert self.classify(503, None) == ("unknown", "http_5xx")

    def test_envelope_without_res_code_is_parse_error(self):
        assert self.classify(200, {"unexpected": True}) == ("unknown", "parse_error")

    @pytest.mark.parametrize("code", [False, 0.0, "0"])
    def test_only_a_real_int_zero_is_success(self, code):
        # Review: False == 0 and 0.0 == 0 in Python; a malformed envelope
        # must not be recorded as a 14-day "valid".
        assert self.classify(200, {"res_code": code}) == ("unknown", "unmapped_response")

    def test_non_json_body_is_unknown_parse_error_regardless_of_4xx_status(self):
        # This is an envelope-driven JSON API -- a non-JSON (e.g. XML) body
        # is a parse failure first, whatever the outer HTTP status was.
        assert self.classify(403, "not json") == ("unknown", "parse_error")
        assert self.classify(400, '<?xml version="1.0"?><error><code>FileNotFound</code></error>') == (
            "unknown", "parse_error",
        )


class TestClassifyQuark:
    classify = staticmethod(tmdb._classify_quark)

    def test_ok(self):
        assert self.classify(200, {"status": 200, "code": 0, "message": "ok"}) == ("valid", "ok")

    @pytest.mark.parametrize(
        "status,code,reason",
        [(404, 41011, "share_expired"), (404, 41012, "share_cancelled"), (403, 41031, "share_audit")],
    )
    def test_confirmed_codes_are_invalid(self, status, code, reason):
        # Calibration rounds 1-2: API message + the share page rendered in a
        # browser ("分享地址已失效" / "该分享已被取消，无法访问" /
        # "该分享已失效，不可访问").
        assert self.classify(status, {"status": status, "code": code, "message": "x"}) == ("invalid", reason)

    @pytest.mark.parametrize("code", [41010, 41013, 41014, 41016, 41017, -1, 1])
    def test_unconfirmed_code_is_unknown_unmapped_never_invalid(self, code):
        # The retired 41013/41014/41016/41017 placeholders were never
        # verified; 41010 was seen once in round 2 and is not confirmed.
        # None of them may produce "invalid".
        assert self.classify(404, {"status": 404, "code": code}) == ("unknown", "unmapped_response")

    def test_code_zero_with_non_200_status_field_is_unmapped(self):
        assert self.classify(200, {"status": 400, "code": 0}) == ("unknown", "unmapped_response")

    def test_envelope_without_code_is_parse_error(self):
        assert self.classify(200, {"status": 200}) == ("unknown", "parse_error")

    @pytest.mark.parametrize("body", [{"status": 200, "code": False}, {"status": 200, "code": 0.0},
                                      {"status": True, "code": 0}, {"status": 200.0, "code": 0}])
    def test_only_real_ints_are_success(self, body):
        assert self.classify(200, body) == ("unknown", "unmapped_response")

    def test_429_is_rate_limited(self):
        assert self.classify(429, None) == ("unknown", "rate_limited")

    def test_5xx(self):
        assert self.classify(500, None) == ("unknown", "http_5xx")


class TestClassifyAlipan:
    classify = staticmethod(tmdb._classify_alipan)

    def test_http_200_is_valid(self):
        assert self.classify(200, {"anything": True}) == ("valid", "ok")

    def test_404_is_share_not_found(self):
        assert self.classify(404, None) == ("invalid", "share_not_found")

    @pytest.mark.parametrize(
        "code,reason",
        [
            ("ShareLink.Cancelled", "share_cancelled"),
            ("ShareLink.Expired", "share_expired"),
            ("ShareLink.Forbidden", "share_audit"),
            ("NotFound.ShareLink", "share_not_found"),
        ],
    )
    def test_error_code_mapping(self, code, reason):
        assert self.classify(400, {"code": code}) == ("invalid", reason)

    def test_429_is_rate_limited(self):
        assert self.classify(429, None) == ("unknown", "rate_limited")

    def test_5xx(self):
        assert self.classify(502, None) == ("unknown", "http_5xx")

    def test_http_200_with_invalid_family_code_is_invalid(self):
        # M8: a 200 status doesn't automatically mean valid -- the JSON
        # body's own `code` is inspected first, exactly like every other
        # non-200 status already does.
        assert self.classify(200, {"code": "ShareLink.Cancelled"}) == ("invalid", "share_cancelled")

    def test_http_200_with_unmapped_code_is_still_valid(self):
        assert self.classify(200, {"code": "SomethingElse"}) == ("valid", "ok")

    def test_unrecognised_4xx_code_is_unknown(self):
        assert self.classify(400, {"code": "Something.Else"}) == ("unknown", "http_4xx")


class TestClassify115:
    classify = staticmethod(tmdb._classify_115)

    def test_state_true_is_valid(self):
        assert self.classify(200, {"state": True, "error": "", "errno": 0, "data": {}}) == ("valid", "ok")

    def test_errno_4100010_is_share_cancelled(self):
        # Calibration round 1: 2/2 API samples; one rendered "无法加载分享
        # 分享已取消" in a browser.
        assert self.classify(200, {"state": False, "error": "分享已取消", "errno": 4100010}) == (
            "invalid", "share_cancelled",
        )

    @pytest.mark.parametrize("errno", [4100011, 4100012, 990001, 0, "4100010"])
    def test_unconfirmed_errno_is_unknown_unmapped(self, errno):
        assert self.classify(200, {"state": False, "error": "x", "errno": errno}) == ("unknown", "unmapped_response")

    @pytest.mark.parametrize("text", ["操作过于频繁", "请稍后再试", "需要验证码", "人机验证"])
    def test_anti_bot_text_is_unknown_anti_bot(self, text):
        assert self.classify(200, {"state": False, "error": text, "errno": 1}) == ("unknown", "anti_bot")

    def test_access_code_error_text_does_not_pause_the_provider(self):
        # Review: a bare "验证" needle would turn "请验证访问码" into a 6 h pause.
        assert self.classify(200, {"state": False, "error": "请验证访问码", "errno": 1}) == (
            "unknown", "unmapped_response",
        )

    @pytest.mark.parametrize("html", ["<html>操作频繁，请稍后再试</html>", "<html>请输入验证码</html>"])
    def test_captcha_html_instead_of_json_is_anti_bot(self, html):
        # Review: a throttle/captcha shell served in place of the JSON
        # envelope must still trip the pause (shared outbound IP).
        assert self.classify(200, html) == ("unknown", "anti_bot")
        assert self.classify(403, html) == ("unknown", "anti_bot")

    def test_html_shell_page_is_parse_error(self):
        # The share page's own HTML (what the old probe fetched) carries no
        # verdict at all.
        assert self.classify(200, "<!DOCTYPE html><html>115生活</html>") == ("unknown", "parse_error")

    @pytest.mark.parametrize("state", ["true", "1", 1.0, None])
    def test_non_bool_state_is_parse_error(self, state):
        assert self.classify(200, {"state": state, "errno": 0}) == ("unknown", "parse_error")

    def test_envelope_without_state_is_parse_error(self):
        assert self.classify(200, {"errno": 0}) == ("unknown", "parse_error")

    def test_429_is_rate_limited(self):
        assert self.classify(429, None) == ("unknown", "rate_limited")


class TestResponseSignal:
    signal = staticmethod(tmdb.response_signal)

    @pytest.mark.parametrize(
        "status,body,expected",
        [
            (400, {"res_code": "FileNotFound", "res_message": "shareId=123 fileId=456"}, "http:400 res_code:FileNotFound"),
            (200, {"res_code": 0, "res_message": "成功"}, "http:200 res_code:0"),
            (200, {"status": 200, "code": 0, "data": {"stoken": "secret"}}, "http:200 code:0"),
            (200, {"state": False, "errno": 4100010, "error": "分享已取消"}, "http:200 errno:4100010"),
            (200, {"share_name": "片名", "file_infos": []}, "http:200 json"),
            (200, {"state": True}, "http:200 state:1"),
            (400, '<?xml version="1.0"?><error/>', "http:400 xml"),
            (200, "<!DOCTYPE html><html></html>", "http:200 html"),
            (403, "forbidden", "http:403 text"),
            (200, None, "http:200 other"),
        ],
    )
    def test_code_only_fingerprint(self, status, body, expected):
        assert self.signal(status, body) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "https://115.com/s/swfake115share1",      # a URL under a "code" key
            "share_code=abcdefgh not found",          # a sentence
            {"code": "X", "shareId": 987654321},      # nested envelope
            ["FileNotFound"],
            "x" * 33,                                 # too long for a code token
            True,                                     # bool is not a code
            None,
            1.5,
        ],
    )
    def test_non_token_code_values_are_never_emitted(self, value):
        # Review: validate, don't strip -- a value that is not already a bare
        # code token becomes the literal "unsafe", so no path/id/sentence
        # fragment can survive into the histogram.
        assert self.signal(200, {"code": value}) == "http:200 code:unsafe"

    def test_state_fallback_only_for_bool_or_int(self):
        assert self.signal(200, {"state": "weird"}) == "http:200 state:other"
        assert self.signal(200, {"state": 0}) == "http:200 state:0"

# ---------------------------------------------------------------------------
# LinkCheckClient: pacing wiring, budget, pause
# ---------------------------------------------------------------------------


class TestLinkCheckClientWiring:
    def test_default_intervals_per_provider(self, tmp_path):
        client, _, _ = _client(tmp_path, FakeSession())
        assert client._limiters["tianyicloud"]._base_interval == pytest.approx(3.0)
        assert client._limiters["quark"]._base_interval == pytest.approx(3.0)
        assert client._limiters["alipan"]._base_interval == pytest.approx(3.0)
        assert client._limiters["115"]._base_interval == pytest.approx(10.0)

    def test_default_daily_caps(self, tmp_path):
        client, _, _ = _client(tmp_path, FakeSession())
        assert client.budget_remaining("tianyicloud") == 3000
        assert client.budget_remaining("115") == 500

    def test_settings_override_daily_cap(self, tmp_path):
        client, _, _ = _client(tmp_path, FakeSession(), settings={"linkcheck_quark_daily_cap": "5"})
        assert client.budget_remaining("quark") == 5


class TestLinkCheckBudget:
    def test_reserve_decrements_remaining_until_exhausted(self, tmp_path):
        client, _, _ = _client(tmp_path, FakeSession(), settings={"linkcheck_quark_daily_cap": "2"})
        assert client.reserve_budget("quark") is True
        assert client.reserve_budget("quark") is True
        assert client.budget_remaining("quark") == 0
        assert client.reserve_budget("quark") is False

    def test_used_today_is_isolated_per_provider(self, tmp_path):
        client, _, _ = _client(tmp_path, FakeSession(), settings={"linkcheck_quark_daily_cap": "10"})
        client.reserve_budget("quark")
        assert client.used_today("quark") == 1
        assert client.used_today("tianyicloud") == 0

    def test_budget_resets_on_a_new_day(self, tmp_path):
        clock = Clock(epoch=1_700_000_000)
        client, _, _ = _client(tmp_path, FakeSession(), settings={"linkcheck_quark_daily_cap": "1"}, clock=clock)
        assert client.reserve_budget("quark") is True
        assert client.reserve_budget("quark") is False
        clock.epoch += 90000  # into the next UTC day
        assert client.reserve_budget("quark") is True


class TestLinkCheckClientPause:
    def test_rate_limited_reason_pauses_immediately(self, tmp_path):
        clock = Clock()
        session = FakeSession().route(
            "https://pan.quark.cn/s/swfakepause1", None
        )
        client, _, _ = _client(tmp_path, session, clock=clock)
        assert client.is_paused("quark") is False
        client._note_pause_signal("quark", "rate_limited")
        assert client.is_paused("quark") is True
        assert client.last_error_class("quark") == "rate_limited"
        assert client.paused_until("quark") == clock.epoch + tmdb.LINK_CHECK_DEFAULT_PAUSE_SECONDS

    def test_115_gets_the_longer_pause(self, tmp_path):
        clock = Clock()
        client, _, _ = _client(tmp_path, FakeSession(), clock=clock)
        client._note_pause_signal("115", "anti_bot")
        assert client.paused_until("115") == clock.epoch + tmdb.LINK_CHECK_115_PAUSE_SECONDS

    def test_pause_expires(self, tmp_path):
        clock = Clock()
        client, _, _ = _client(tmp_path, FakeSession(), clock=clock)
        client._note_pause_signal("quark", "rate_limited")
        assert client.is_paused("quark") is True
        clock.epoch += int(tmdb.LINK_CHECK_DEFAULT_PAUSE_SECONDS) + 1
        assert client.is_paused("quark") is False

    def test_five_consecutive_network_errors_pause(self, tmp_path):
        client, _, _ = _client(tmp_path, FakeSession())
        for _ in range(4):
            client._note_network_error("quark", "ConnectionError")
            assert client.is_paused("quark") is False
        client._note_network_error("quark", "ConnectionError")
        assert client.is_paused("quark") is True

    def test_success_resets_consecutive_network_errors(self, tmp_path):
        client, _, _ = _client(tmp_path, FakeSession())
        for _ in range(4):
            client._note_network_error("quark", "ConnectionError")
        client._note_success("quark")
        for _ in range(4):
            client._note_network_error("quark", "ConnectionError")
        assert client.is_paused("quark") is False

    def test_paused_state_survives_a_freshly_constructed_client(self, tmp_path):
        clock = Clock()
        client_a, conn_factory, _ = _client(tmp_path, FakeSession(), clock=clock)
        client_a._note_pause_signal("115", "anti_bot")

        client_b = tmdb.LinkCheckClient(
            conn_factory=conn_factory, session=FakeSession(),
            clock=real_time.monotonic, sleep=lambda s: None, now=clock.now, today=clock.today,
        )
        assert client_b.is_paused("115") is True
        assert client_b.last_error_class("115") == "anti_bot"


# ---------------------------------------------------------------------------
# dispatch() / header scrubbing
# ---------------------------------------------------------------------------


class TestDispatchNeverSendsAuthHeaders:
    def test_forbidden_headers_are_stripped_even_if_an_adapter_tried(self, tmp_path):
        session = FakeSession().route("https://example.com/probe", FakeResponse(200, text="ok"))
        client, _, _ = _client(tmp_path, session)
        client.dispatch(
            "GET", "https://example.com/probe", None,
            {"Cookie": "sid=fake", "Authorization": "Bearer fake", "X-Custom": "keep-me"},
        )
        sent_headers = {k.lower(): v for k, v in session.calls[0]["headers"].items()}
        assert "cookie" not in sent_headers
        assert "authorization" not in sent_headers
        assert sent_headers.get("x-custom") == "keep-me"

    def test_base_user_agent_is_always_sent(self, tmp_path):
        session = FakeSession().route("https://example.com/probe", FakeResponse(200, text="ok"))
        client, _, _ = _client(tmp_path, session)
        client.dispatch("GET", "https://example.com/probe", None, None)
        assert session.calls[0]["headers"]["User-Agent"] == tmdb.LINK_CHECK_USER_AGENT

    @pytest.mark.parametrize("code", ["tianyicloud", "quark", "alipan", "115"])
    def test_no_real_adapter_ever_produces_a_cookie_or_auth_header_end_to_end(self, tmp_path, code):
        adapter = tmdb.LINK_CHECK_ADAPTERS[code]
        method, url, payload, headers = adapter.build_request("swfakeref1", "ab12")
        session = FakeSession().route(url, FakeResponse(200, json_payload={}, text="ok"))
        client, _, _ = _client(tmp_path, session)
        client.dispatch(method, url, payload, headers)
        sent_headers = {k.lower() for k in session.calls[0]["headers"]}
        assert "cookie" not in sent_headers
        assert "authorization" not in sent_headers

    def test_build_anonymous_session_has_no_cookies_and_ignores_env(self):
        session = tmdb.build_anonymous_session()
        assert session.trust_env is False
        assert session.max_redirects == tmdb.LINK_CHECK_MAX_REDIRECTS
        assert len(session.cookies) == 0


# ---------------------------------------------------------------------------
# I1: the session must never accumulate a provider's cookies across probes
# ---------------------------------------------------------------------------

import email.message
import types

import requests.adapters


class _SetCookieAdapter(requests.adapters.BaseAdapter):
    """A real transport adapter mounted onto a genuine ``requests.Session``
    that always tries to plant a cookie via ``Set-Cookie`` -- exercises
    requests' OWN cookie-extraction path (``Session.send()`` ->
    ``extract_cookies_to_jar()``) for real, so a passing test proves
    ``build_anonymous_session()``'s jar policy (and
    ``LinkCheckClient.dispatch()``'s per-request jar clear) actually block
    cookie accumulation end to end -- not just "nothing here happens to
    read session.cookies"."""

    def __init__(self, body: bytes = b'{"status": 200, "code": 0}'):
        self.body = body
        self.sent_requests: list = []

    def send(self, request, **kwargs):
        self.sent_requests.append(request)
        msg = email.message.Message()
        msg.add_header("Set-Cookie", "sid=fake-session-token; Path=/")
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.raw = types.SimpleNamespace(_original_response=types.SimpleNamespace(msg=msg))
        response._content = self.body
        response._content_consumed = True
        response.headers["Content-Type"] = "application/json"
        return response

    def close(self):
        pass


class TestCookieJarNeverAccumulates:
    def test_set_cookie_is_never_stored_or_resent_on_a_real_session(self, tmp_path):
        adapter = _SetCookieAdapter()
        session = tmdb.build_anonymous_session()
        session.mount("https://", adapter)
        client, _, _ = _client(tmp_path, session)

        outcome = tmdb.check_link(client, "quark", "https://pan.quark.cn/s/swfakecookie1", None)
        assert outcome == tmdb.LinkCheckOutcome("valid", "ok", "200")
        # I1: the jar's own blocking policy already refused to store the
        # Set-Cookie the fake provider tried to plant.
        assert len(session.cookies) == 0

        tmdb.check_link(client, "quark", "https://pan.quark.cn/s/swfakecookie2", None)
        assert len(adapter.sent_requests) == 2
        second_request_headers = {k.lower() for k in adapter.sent_requests[1].headers}
        assert "cookie" not in second_request_headers


# ---------------------------------------------------------------------------
# check_link: assembly of extract_ref -> build_request -> dispatch -> classify
# ---------------------------------------------------------------------------


class TestCheckLink:
    def test_valid_end_to_end(self, tmp_path):
        url = "https://pan.quark.cn/s/swfakeq9"
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        client, _, _ = _client(tmp_path, session)
        outcome = tmdb.check_link(client, "quark", url, None)
        assert outcome == tmdb.LinkCheckOutcome("valid", "ok", "200")

    def test_invalid_end_to_end(self, tmp_path):
        url = "https://alipan.com/s/swfakea9"
        session = FakeSession().route(
            "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous",
            FakeResponse(404, json_payload=None),
        )
        client, _, _ = _client(tmp_path, session)
        outcome = tmdb.check_link(client, "alipan", url, None)
        assert outcome == tmdb.LinkCheckOutcome("invalid", "share_not_found", "404")

    def test_unsupported_url_never_makes_a_request(self, tmp_path):
        session = FakeSession()
        client, _, _ = _client(tmp_path, session)
        outcome = tmdb.check_link(client, "quark", "https://example.com/not-quark", None)
        assert outcome == tmdb.LinkCheckOutcome("unknown", "unsupported_url", None)
        assert session.calls == []

    def test_unknown_provider_is_unsupported_url(self, tmp_path):
        client, _, _ = _client(tmp_path, FakeSession())
        outcome = tmdb.check_link(client, "baidu", "https://pan.baidu.com/s/swfake1", None)
        assert outcome == tmdb.LinkCheckOutcome("unknown", "unsupported_url", None)

    def test_network_error_is_reported_and_recorded(self, tmp_path):
        url = "https://pan.quark.cn/s/swfakeq10"
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            requests.exceptions.ConnectTimeout("boom"),
        )
        client, _, _ = _client(tmp_path, session)
        outcome = tmdb.check_link(client, "quark", url, None)
        assert outcome == tmdb.LinkCheckOutcome("unknown", "network_error", None)
        # M1: last_error_class is always the contract reason
        # ("network_error"), never the raw Python exception class name --
        # the settings card would otherwise show "ConnectTimeout" verbatim.
        assert client.last_error_class("quark") == "network_error"

    def test_network_error_class_name_is_logged_but_never_persisted(self, tmp_path, caplog):
        """M1: the raw exception class name is only ever visible in the
        log line -- link_check_state (and therefore the API) always sees
        the contract reason."""
        url = "https://pan.quark.cn/s/swfakeq10b"
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            requests.exceptions.ConnectTimeout("boom"),
        )
        client, conn_factory, _ = _client(tmp_path, session)
        with caplog.at_level("WARNING"):
            tmdb.check_link(client, "quark", url, None)
        assert "ConnectTimeout" in caplog.text

        conn = conn_factory()
        state = tmdb.read_link_check_state(conn)
        conn.close()
        assert state.get("last_error_class:quark") == "network_error"
        assert "ConnectTimeout" not in jsonlib.dumps(state)

    def test_parse_error_neither_resets_network_errors_nor_clears_error_class(self, tmp_path):
        # Review: an HTML shell in place of the JSON envelope is not a
        # success -- 3 prior transport errors must still count toward the
        # pause threshold afterwards.
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, text="<html>???</html>"),
        )
        client, _, _ = _client(tmp_path, session)
        client._consecutive_network_errors["quark"] = 3
        client._last_error_class["quark"] = "network_error"
        outcome = tmdb.check_link(client, "quark", "https://pan.quark.cn/s/swfakeq9", None)
        assert outcome == tmdb.LinkCheckOutcome("unknown", "parse_error", "200")
        assert client._consecutive_network_errors["quark"] == 3
        assert client.last_error_class("quark") == "network_error"

    def test_oversized_response_is_unknown_parse_error(self, tmp_path):
        url = "https://pan.quark.cn/s/swfakeq11"
        oversized = b"x" * (tmdb.LINK_CHECK_RESPONSE_SIZE_CAP_BYTES + 1)
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, content=oversized),
        )
        client, _, _ = _client(tmp_path, session)
        outcome = tmdb.check_link(client, "quark", url, None)
        assert outcome == tmdb.LinkCheckOutcome("unknown", "parse_error", "200")

    def test_request_is_made_with_stream_true(self, tmp_path):
        # I5: the request itself must ask for a streamed body so the cap
        # check below can stop reading early instead of requests buffering
        # the whole response first.
        url = "https://pan.quark.cn/s/swfakeq13"
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        client, _, _ = _client(tmp_path, session)
        tmdb.check_link(client, "quark", url, None)
        assert session.calls[0]["stream"] is True

    def test_response_is_closed_after_reading(self, tmp_path):
        url = "https://pan.quark.cn/s/swfakeq14"
        response = FakeResponse(200, json_payload={"status": 200, "code": 0})
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc", response,
        )
        client, _, _ = _client(tmp_path, session)
        tmdb.check_link(client, "quark", url, None)
        assert response.closed is True

    def test_oversized_streaming_response_stops_reading_well_before_the_stream_ends(self, tmp_path):
        # I5: a provider trying to serve an arbitrarily large body must
        # never have it fully buffered/drained first -- this fake only
        # supports iter_content/close (no .content/.json()/.text at all),
        # and offers far more chunks than the cap allows, so a pass here
        # proves check_link() stopped iterating early.
        cap = tmdb.LINK_CHECK_RESPONSE_SIZE_CAP_BYTES
        chunk = b"x" * 8192
        available_chunks = (cap // len(chunk)) + 50
        response = FakeStreamingResponse(200, [chunk] * available_chunks)
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc", response,
        )
        client, _, _ = _client(tmp_path, session)
        outcome = tmdb.check_link(client, "quark", "https://pan.quark.cn/s/swfakeq15", None)
        assert outcome == tmdb.LinkCheckOutcome("unknown", "parse_error", "200")
        assert response.closed is True
        assert response.chunks_yielded < available_chunks

    def test_rate_limited_response_pauses_the_provider(self, tmp_path):
        url = "https://pan.quark.cn/s/swfakeq12"
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(429),
        )
        client, _, _ = _client(tmp_path, session)
        tmdb.check_link(client, "quark", url, None)
        assert client.is_paused("quark") is True


# ---------------------------------------------------------------------------
# scheduling math
# ---------------------------------------------------------------------------


class TestNextCheckDelay:
    def test_valid(self):
        assert tmdb._next_check_delay_seconds("valid", 0) == 14 * 86400

    def test_invalid(self):
        assert tmdb._next_check_delay_seconds("invalid", 0) == 7 * 86400

    def test_unknown_below_escalation_threshold(self):
        assert tmdb._next_check_delay_seconds("unknown", 4) == 1 * 86400

    def test_unknown_at_escalation_threshold(self):
        assert tmdb._next_check_delay_seconds("unknown", 5) == 7 * 86400

    def test_unknown_above_escalation_threshold(self):
        assert tmdb._next_check_delay_seconds("unknown", 9) == 7 * 86400


# ---------------------------------------------------------------------------
# run_link_check_round: store + client integration
# ---------------------------------------------------------------------------

from cryptography.fernet import Fernet


def _round_store(tmp_path):
    fernet = Fernet(Fernet.generate_key())
    db_path = tmp_path / "media-library.db"
    store = ls.LibraryStore(db_path, fernet)
    store.create_schema()
    return store, fernet


def _add_link(store, fernet, *, provider, url, access_code=None, media_identity=None):
    media_identity = media_identity or f"tt-fake-{url}"
    media_id = store.upsert_media(
        ls.MediaRecord(media_identity=media_identity, media_type="movie", title_zh="虚构片", search_key="虚构片")
    )
    group_id = store.upsert_group(
        ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-{url}", display_title="g")
    )
    import hashlib

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    rec = ls.LinkRecord(
        public_id=f"pub-{url_hash[:16]}",
        group_id=group_id,
        provider=provider,
        canonical_url_hash=url_hash,
        url_label=f"{provider} 分享",
        url_ciphertext=fernet.encrypt(url.encode("utf-8")),
        access_code_ciphertext=fernet.encrypt(access_code.encode("utf-8")) if access_code else None,
        has_access_code=1 if access_code else 0,
    )
    link_id, _ = store.upsert_link(rec)
    return media_id, group_id, link_id, url_hash


class TestRunLinkCheckRound:
    def test_checks_due_link_and_records_result(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        url = "https://pan.quark.cn/s/swfakeround1"
        _add_link(store, fernet, provider="quark", url=url)

        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        client, _, _ = _client(tmp_path, session)

        stats = tmdb.run_link_check_round(store, client, providers=["quark"], limit=10)

        assert stats.checked == 1
        assert stats.valid == 1
        assert stats.by_provider["quark"]["valid"] == 1

        conn = store.connect(readonly=True)
        row = conn.execute("SELECT status, reason FROM link_check").fetchone()
        conn.close()
        assert tuple(row) == ("valid", "ok")

    def test_dry_run_never_writes_or_reserves_budget(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        url = "https://pan.quark.cn/s/swfakeround2"
        _add_link(store, fernet, provider="quark", url=url)
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        client, _, _ = _client(tmp_path, session, settings={"linkcheck_quark_daily_cap": "10"})

        stats = tmdb.run_link_check_round(store, client, providers=["quark"], limit=10, dry_run=True)

        assert stats.checked == 1
        assert stats.valid == 1
        conn = store.connect(readonly=True)
        count = conn.execute("SELECT COUNT(*) FROM link_check").fetchone()[0]
        conn.close()
        assert count == 0
        assert client.used_today("quark") == 0

    def test_disabled_provider_is_not_selected_without_explicit_providers_arg(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        _add_link(store, fernet, provider="quark", url="https://pan.quark.cn/s/swfakeround3")
        client, _, _ = _client(tmp_path, FakeSession())

        stats = tmdb.run_link_check_round(store, client, settings={})  # linkcheck_enabled defaults to off

        assert stats.checked == 0

    def test_global_and_per_provider_enable_gate(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        url = "https://pan.quark.cn/s/swfakeround4"
        _add_link(store, fernet, provider="quark", url=url)
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        client, _, _ = _client(tmp_path, session)

        settings = {"linkcheck_enabled": "1", "linkcheck_quark_enabled": "0"}
        assert tmdb.run_link_check_round(store, client, settings).checked == 0

        settings["linkcheck_quark_enabled"] = "1"
        assert tmdb.run_link_check_round(store, client, settings).checked == 1

    def test_budget_exhaustion_stops_the_provider_mid_round(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        url1 = "https://pan.quark.cn/s/swfakeround5"
        url2 = "https://pan.quark.cn/s/swfakeround6"
        _add_link(store, fernet, provider="quark", url=url1)
        _add_link(store, fernet, provider="quark", url=url2)
        session = FakeSession()
        for u in (
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
        ):
            session.route(u, FakeResponse(200, json_payload={"status": 200, "code": 0}))
        client, _, _ = _client(tmp_path, session, settings={"linkcheck_quark_daily_cap": "1"})

        stats = tmdb.run_link_check_round(store, client, providers=["quark"], limit=10)
        assert stats.checked == 1
        assert stats.by_provider["quark"]["budget_exhausted_skipped"] == 1
        # M6: the declared-but-previously-unused "budget_exhausted" reason
        # is actually produced in the round's own skip reporting.
        assert stats.by_provider["quark"]["reasons"]["budget_exhausted"] == 1

    def test_paused_provider_is_skipped_entirely(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        _add_link(store, fernet, provider="quark", url="https://pan.quark.cn/s/swfakeround7")
        client, _, _ = _client(tmp_path, FakeSession())
        client._note_pause_signal("quark", "rate_limited")

        stats = tmdb.run_link_check_round(store, client, providers=["quark"], limit=10)
        assert stats.checked == 0
        assert stats.by_provider["quark"]["paused_until"] == client.paused_until("quark")

    def test_dry_run_still_honours_an_active_pause(self, tmp_path):
        """I3: --dry-run must not hammer a provider that's currently paused
        -- the old code only checked ``client.is_paused(code)`` when NOT a
        dry run, so calibration could keep probing straight through a live
        rate-limit/anti-bot pause instead of skipping and reporting it."""
        store, fernet = _round_store(tmp_path)
        _add_link(store, fernet, provider="quark", url="https://pan.quark.cn/s/swfakeround7b")
        client, _, _ = _client(tmp_path, FakeSession())
        client._note_pause_signal("quark", "rate_limited")

        stats = tmdb.run_link_check_round(store, client, providers=["quark"], sample=5, dry_run=True)
        assert stats.checked == 0
        assert stats.by_provider["quark"]["paused_until"] == client.paused_until("quark")

    def test_sample_mode_ignores_due_status(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        url = "https://pan.quark.cn/s/swfakeround8"
        _add_link(store, fernet, provider="quark", url=url)
        # Mark it already checked with a far-future next_check_at (not due).
        import hashlib

        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        store.record_link_check(
            "quark", url_hash, status="valid", reason="ok", http_class="200",
            checked_at=1, next_check_at=9_999_999_999, consecutive_unknown=0,
        )
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        client, _, _ = _client(tmp_path, session)

        # Without --sample, the ordinary due-only query finds nothing.
        assert tmdb.run_link_check_round(store, client, providers=["quark"], limit=10).checked == 0
        # With --sample, the not-due link is still probed.
        stats = tmdb.run_link_check_round(store, client, providers=["quark"], sample=5)
        assert stats.checked == 1

    def test_key_unavailable_link_is_skipped_not_fatal(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        _add_link(store, fernet, provider="quark", url="https://pan.quark.cn/s/swfakeround9")
        # A store opened with no fernet at all cannot reveal() -- reserve()
        # for that provider is not called for a link that failed to reveal
        # (never counted as "checked"), and the round doesn't blow up.
        store_no_key = ls.LibraryStore(store.db_path, None)
        client, _, _ = _client(tmp_path, FakeSession())

        stats = tmdb.run_link_check_round(store_no_key, client, providers=["quark"], limit=10)
        assert stats.checked == 0

    def test_limit_is_split_round_robin_not_exhausted_by_the_first_provider(self, tmp_path):
        """M5: the old registry-order code gave the FIRST enabled
        provider's own due backlog the whole round's shared `limit`,
        starving every other provider behind it."""
        store, fernet = _round_store(tmp_path)
        for i in range(3):
            _add_link(store, fernet, provider="quark", url=f"https://pan.quark.cn/s/swfakerr{i}")
        for i in range(3):
            _add_link(store, fernet, provider="alipan", url=f"https://alipan.com/s/swfakerr{i}")

        session = FakeSession()
        session.route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        session.route(
            "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous",
            FakeResponse(200, json_payload={}),
        )
        client, _, _ = _client(tmp_path, session)

        stats = tmdb.run_link_check_round(store, client, providers=["quark", "alipan"], limit=2)
        assert stats.checked == 2
        assert stats.by_provider["quark"]["checked"] == 1
        assert stats.by_provider["alipan"]["checked"] == 1


import library_search as lse


class TestRunLinkCheckRoundRecountsAffectedGroups:
    """w6-checker-fix C2 repro: before this fix, nothing recounted after a
    check flipped a link's live status, so ``media.link_count``/
    ``resource_group.link_count``/``has_115`` (and everything derived from
    them: ``media_detail``'s groups, search cards, ``all_links_invalid``)
    kept whatever value the last *unrelated* write left them at. Every test
    below calls ``store.recount()`` exactly once, right after building the
    fixture -- establishing the SAME baseline ``scripts/
    import_media_library.py`` always leaves a freshly-installed library in,
    a step no ``run_link_check_round`` caller can skip -- and then never
    again: the round itself, with no further manual ``recount()``/
    ``recount_groups()`` call anywhere, must be the thing that keeps counts
    in sync from then on."""

    def test_group_and_media_counts_drop_when_a_fresh_check_finds_the_link_invalid(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        url = "https://cloud.189.cn/t/swfakec2invalid1"
        media_id, group_id, _, _ = _add_link(store, fernet, provider="tianyicloud", url=url)
        store.recount()  # baseline only, like import_media_library.py's own post-import call
        assert store.group_detail(group_id)["link_count"] == 1

        session = FakeSession().route(
            "https://api.cloud.189.cn/open/share/getShareInfoByCodeV2.action",
            FakeResponse(400, json_payload={"res_code": "FileNotFound", "res_message": ""}),  # confirmed dead
        )
        client, _, _ = _client(tmp_path, session)
        stats = tmdb.run_link_check_round(store, client, providers=["tianyicloud"], limit=10)
        assert stats.invalid == 1
        assert stats.by_provider["tianyicloud"]["signals"] == {"http:400 res_code:FileNotFound": 1}

        # No recount()/recount_groups() call anywhere above this line --
        # the round itself must have kept the precomputed counts in sync.
        assert store.group_detail(group_id)["link_count"] == 0
        detail = store.media_detail(media_id)
        assert detail["groups"] == []
        assert detail["group_count"] == 0
        assert detail["provider_facets"] == []

        lse.build_index(store)
        page = lse.search(store, "", lse.Filters(include_deleted=True))
        item = next(i for i in page.items if i["media_id"] == media_id)
        assert item["all_links_invalid"] is True
        assert item["link_count"] == 0

    def test_counts_are_restored_when_a_later_check_finds_the_link_valid_again(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        url = "https://pan.quark.cn/s/swfakec2restore1"
        media_id, group_id, _, url_hash = _add_link(store, fernet, provider="quark", url=url)
        # Simulates a PRIOR round (before this test's own round) having
        # already found -- and correctly recounted -- this link invalid;
        # this recount() call establishes that pre-existing baseline, it
        # is not standing in for the fix under test.
        store.record_link_check(
            "quark", url_hash, status="invalid", reason="share_not_found",
            http_class="200", checked_at=1, next_check_at=1, consecutive_unknown=0,
        )
        store.recount()
        assert store.group_detail(group_id)["link_count"] == 0

        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        client, _, _ = _client(tmp_path, session)
        stats = tmdb.run_link_check_round(store, client, providers=["quark"], limit=10)
        assert stats.valid == 1

        # C2: restored without any further manual recount() call.
        assert store.group_detail(group_id)["link_count"] == 1
        detail = store.media_detail(media_id)
        assert len(detail["groups"]) == 1

    def test_a_verdict_that_does_not_flip_live_status_skips_the_recount(self, tmp_path):
        """unknown -> unknown (or valid -> valid) never touches
        live_link_sql()'s result, so run_link_check_round has nothing to
        recount -- this is a cheap sanity check, not a correctness
        requirement on its own, but guards against recount_groups() being
        called (and failing loudly) for every single check regardless of
        whether anything actually changed."""
        store, fernet = _round_store(tmp_path)
        url = "https://pan.quark.cn/s/swfakec2nochange1"
        _add_link(store, fernet, provider="quark", url=url)
        store.recount()

        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        client, _, _ = _client(tmp_path, session)
        stats = tmdb.run_link_check_round(store, client, providers=["quark"], limit=10)
        assert stats.valid == 1  # sanity: the check itself still ran


# ---------------------------------------------------------------------------
# BackgroundLinkChecker: leader election, heartbeat, idle/round behaviour
# (mirrors tests/test_library_tmdb_enrich.py's BackgroundEnricher patterns)
# ---------------------------------------------------------------------------

import fcntl


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = real_time.time() + timeout
    while real_time.time() < deadline:
        if predicate():
            return
        real_time.sleep(0.005)
    raise AssertionError("condition not met within timeout")


def _fast_sleep(seconds: float) -> None:
    real_time.sleep(min(seconds, 0.01))


class TestBackgroundLinkChecker:
    def test_disabled_check_keeps_thread_idle_and_never_touches_the_store(self, tmp_path):
        store_calls = []

        def store_factory():
            store_calls.append(1)
            return None

        checker = tmdb.BackgroundLinkChecker(
            store_factory, lambda: None,
            leader_lock_path=tmp_path / "linkcheck-leader.lock",
            conn_factory=lambda: sqlite3.connect(str(tmp_path / "state.db")),
            enabled_check=lambda: False,
            sleep=_fast_sleep, idle_seconds=0.02,
        )
        try:
            checker.start()
            _wait_until(lambda: checker.status()["running"])
            real_time.sleep(0.05)
        finally:
            checker.stop(timeout=2)
        assert store_calls == []

    def test_heartbeat_is_persisted_while_running(self, tmp_path):
        state_db = tmp_path / "state.db"
        checker = tmdb.BackgroundLinkChecker(
            lambda: None, lambda: None,
            leader_lock_path=tmp_path / "linkcheck-leader.lock",
            conn_factory=lambda: sqlite3.connect(str(state_db)),
            enabled_check=lambda: False,
            sleep=_fast_sleep, idle_seconds=0.02,
        )
        try:
            checker.start()

            def _heartbeat():
                conn = sqlite3.connect(str(state_db))
                try:
                    return tmdb.read_link_check_state(conn).get("heartbeat_at")
                finally:
                    conn.close()

            _wait_until(lambda: _heartbeat() not in (None, ""))
        finally:
            checker.stop(timeout=2)

    def test_a_round_is_run_and_recorded_when_enabled(self, tmp_path):
        store, fernet = _round_store(tmp_path)
        url = "https://pan.quark.cn/s/swfakebg1"
        _add_link(store, fernet, provider="quark", url=url)
        session = FakeSession().route(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
            FakeResponse(200, json_payload={"status": 200, "code": 0}),
        )
        client, _, _ = _client(tmp_path, session)
        state_db = tmp_path / "bg-state.db"

        checker = tmdb.BackgroundLinkChecker(
            lambda: store, lambda: client,
            leader_lock_path=tmp_path / "linkcheck-leader.lock",
            conn_factory=lambda: sqlite3.connect(str(state_db)),
            settings_factory=lambda: {"linkcheck_enabled": "1", "linkcheck_quark_enabled": "1"},
            sleep=_fast_sleep, idle_seconds=0.02,
        )
        try:
            checker.start()
            _wait_until(lambda: checker.status()["last_round_checked"] == 1)
        finally:
            checker.stop(timeout=2)

        conn = store.connect(readonly=True)
        row = conn.execute("SELECT status FROM link_check").fetchone()
        conn.close()
        assert row[0] == "valid"

    def test_second_instance_waits_for_the_leader_lock(self, tmp_path):
        lock_path = tmp_path / "linkcheck-leader.lock"
        a = tmdb.BackgroundLinkChecker(
            lambda: None, lambda: None, leader_lock_path=lock_path,
            conn_factory=lambda: sqlite3.connect(str(tmp_path / "a-state.db")),
            enabled_check=lambda: False, sleep=_fast_sleep, idle_seconds=0.02,
        )
        b = tmdb.BackgroundLinkChecker(
            lambda: None, lambda: None, leader_lock_path=lock_path,
            conn_factory=lambda: sqlite3.connect(str(tmp_path / "b-state.db")),
            enabled_check=lambda: False, sleep=_fast_sleep, idle_seconds=0.02,
            leader_retry_seconds=0.01,
        )
        try:
            a.start()
            _wait_until(lambda: a.status()["running"])
            b.start()
            _wait_until(lambda: b.status()["leader_attempts"] >= 1)
            assert b.status()["running"] is False
        finally:
            a.stop(timeout=2)
            b.stop(timeout=2)

    def test_stop_while_waiting_for_leadership_is_prompt(self, tmp_path):
        lock_path = tmp_path / "linkcheck-leader.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = open(lock_path, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            b = tmdb.BackgroundLinkChecker(
                lambda: None, lambda: None, leader_lock_path=lock_path,
                conn_factory=lambda: sqlite3.connect(str(tmp_path / "b-state.db")),
                leader_retry_seconds=5,
            )
            b.start()
            _wait_until(lambda: b.status()["leader_attempts"] >= 1)
            started = real_time.time()
            b.stop(timeout=1)
            elapsed = real_time.time() - started
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        assert elapsed < 1.0
