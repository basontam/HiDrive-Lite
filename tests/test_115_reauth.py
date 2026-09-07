"""T19: the explainable 115 cookie state machine, QR re-authorisation
challenge lifecycle, and transfer-flow failure semantics. All upstreams
(115's session check, share/snap, share/receive and the QR login
endpoints) are faked via the ``http`` fixture -- nothing here ever touches
a real 115 account. Every cookie/uid/QR value below is an obviously fake
placeholder (see the shared ``*-fixture-not-real`` convention used across
this test suite)."""

from __future__ import annotations

import json
import threading
import time

import pytest
import requests as requests_lib
from cryptography.fernet import Fernet, InvalidToken

from conftest import FakeResponse

USER_URL = "https://my.115.com/?ct=ajax&ac=get_user_aq"
SNAP_URL = "https://webapi.115.com/share/snap"
RECEIVE_URL = "https://webapi.115.com/share/receive"
QR_TOKEN_URL = "https://qrcodeapi.115.com/api/1.0/web/1.0/token/"
QR_IMAGE_URL = "https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode"
QR_STATUS_URL = "https://qrcodeapi.115.com/get/status/"
QR_RESULT_URL = "https://qrcodeapi.115.com/app/1.0/web/1.0/login/qrcode/"

COOKIE_FIXTURE = "session=fixture-cookie-not-real"
SHARE_URL = "https://115.com/s/swabc123?password=k9x8"


def _route_115_session(http, *, uid="42"):
    http.route("GET", USER_URL, {"state": True, "data": {"uid": uid}})


def _route_qr_token(http, *, uid="qr-uid-fixture-not-real", sign="qr-sign-fixture-not-real", time_=111, qrcode="qr-content-fixture-not-real"):
    http.route("GET", QR_TOKEN_URL, {"state": True, "data": {"uid": uid, "time": time_, "sign": sign, "qrcode": qrcode}})


def _const(response: FakeResponse):
    """FakeHTTP.route()'s positional ``payload`` always builds its own
    FakeResponse; a pre-built one (custom status/headers/raw bytes) has to
    go through ``handler=``."""
    return lambda **kwargs: response


def _start_challenge(client, http) -> str:
    _route_qr_token(http)
    response = client.post("/api/115/reauth/start")
    assert response.status_code == 200
    return response.get_json()["challenge_id"]


@pytest.fixture
def actor_mode(hidrive, monkeypatch):
    """Two distinct Cloudflare Access actors ("alice"/"bob"), for the
    cross-actor isolation tests -- mirrors test_access_guard.py's
    access_mode fixture."""
    monkeypatch.setattr(hidrive, "AUTH_MODE", "access")
    principals = {"alice-assertion": "alice", "bob-assertion": "bob"}
    monkeypatch.setattr(
        hidrive,
        "verify_access_jwt",
        lambda token: {"sub": principals[token]} if token in principals else (_ for _ in ()).throw(PermissionError("bad")),
    )
    return hidrive


def _write_headers(client, assertion: str) -> dict:
    auth = {"Cf-Access-Jwt-Assertion": assertion}
    token = client.get("/api/csrf", headers=auth).get_json()["token"]
    return {**auth, "Origin": "https://hidrive.test", "X-CSRF-Token": token}


# ===========================================================================
# verify_115_session: every classification branch (§4.1 / §8.1)
# ===========================================================================


def test_verify_returns_unconfigured_without_any_network_call(hidrive, http):
    result = hidrive.verify_115_session("")
    assert result["state"] == "unconfigured"
    assert result["error_code"] is None
    assert http.calls == []


def test_verify_classifies_valid_session(hidrive, http):
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})
    result = hidrive.verify_115_session(COOKIE_FIXTURE)
    assert result["state"] == "valid"
    assert result["error_code"] is None
    assert result["retry_after"] == 0


@pytest.mark.parametrize("status_code", [401, 403, 405])
def test_verify_classifies_rejected_http_status(hidrive, http, status_code):
    http.route("GET", USER_URL, handler=_const(FakeResponse({}, status=status_code)))
    result = hidrive.verify_115_session(COOKIE_FIXTURE)
    assert result["state"] == "reauth_required"
    assert result["error_code"] == f"HTTP_{status_code}"


def test_verify_classifies_rate_limited_and_caps_retry_after(hidrive, http):
    http.route("GET", USER_URL, handler=_const(FakeResponse({}, status=429, headers={"Retry-After": "999999"})))
    result = hidrive.verify_115_session(COOKIE_FIXTURE)
    assert result["state"] == "rate_limited"
    assert result["error_code"] == "HTTP_429"
    assert result["retry_after"] == 900


def test_verify_classifies_timeout(hidrive, http):
    http.route("GET", USER_URL, error=requests_lib.Timeout("slow"))
    result = hidrive.verify_115_session(COOKIE_FIXTURE)
    assert result["state"] == "network_error"
    assert result["error_code"] == "TIMEOUT"


def test_verify_classifies_connection_error(hidrive, http):
    http.route("GET", USER_URL, error=requests_lib.ConnectionError("down"))
    result = hidrive.verify_115_session(COOKIE_FIXTURE)
    assert result["state"] == "network_error"
    assert result["error_code"] == "CONNECTION"


def test_verify_classifies_invalid_json(hidrive, http):
    http.route("GET", USER_URL, handler=_const(FakeResponse(raw=b"not json", status=200)))
    result = hidrive.verify_115_session(COOKIE_FIXTURE)
    assert result["state"] == "unknown"
    assert result["error_code"] == "INVALID_JSON"


def test_verify_classifies_missing_state_key_as_unknown(hidrive, http):
    http.route("GET", USER_URL, {"unexpected": "shape"})
    result = hidrive.verify_115_session(COOKIE_FIXTURE)
    assert result["state"] == "unknown"
    assert result["error_code"] == "INVALID_JSON"


def test_verify_classifies_missing_uid_as_reauth_required(hidrive, http):
    http.route("GET", USER_URL, {"state": True, "data": {}})
    result = hidrive.verify_115_session(COOKIE_FIXTURE)
    assert result["state"] == "reauth_required"
    assert result["error_code"] == "AUTH_REJECTED"


def test_verify_never_returns_cookie_or_uid(hidrive, http):
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})
    result = hidrive.verify_115_session(COOKIE_FIXTURE)
    assert "uid" not in result
    assert set(result) == {"state", "error_code", "checked_at", "retry_after"}


# ===========================================================================
# state persistence + transitions + backoff (§3, §4.2, §8.1)
# ===========================================================================


def test_backoff_interval_caps_at_900_seconds(hidrive):
    assert hidrive._115_next_check_interval(0) == 300
    assert hidrive._115_next_check_interval(1) == 600
    assert hidrive._115_next_check_interval(2) == 900
    assert hidrive._115_next_check_interval(10) == 900


def test_state_transitions_valid_then_reauth_required_then_valid(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    _route_115_session(http)
    first = client.get("/api/status?verify_115=1").get_json()["115"]
    assert first["cookie_state"] == "valid"
    assert first["cookie_last_success_at"] is not None

    hidrive.setting_set("115_cookie_checked_at", str(hidrive.utc_now() - 3600))
    http.route("GET", USER_URL, {"state": False})
    second = client.get("/api/status?verify_115=1").get_json()["115"]
    assert second["cookie_state"] == "reauth_required"
    assert second["cookie_error_code"] == "AUTH_REJECTED"

    hidrive.setting_set("115_cookie_checked_at", str(hidrive.utc_now() - 3600))
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})
    third = client.get("/api/status?verify_115=1").get_json()["115"]
    assert third["cookie_state"] == "valid"
    assert third["cookie_error_code"] is None


def test_status_never_exposes_uid_or_cookie(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})
    blob = json.dumps(client.get("/api/status?verify_115=1").get_json(), ensure_ascii=False)
    assert COOKIE_FIXTURE not in blob
    assert '"42"' not in blob and "uid" not in blob.lower().replace("uid_", "")


def test_open_api_token_never_marks_cookie_valid(client, hidrive):
    hidrive.secret_set("115_open_access_token", "open-access-fixture-not-real")
    hidrive.secret_set("115_open_refresh_token", "open-refresh-fixture-not-real")
    status = client.get("/api/status").get_json()["115"]
    assert status["cookie_configured"] is False
    assert status["cookie_state"] == "unconfigured"
    assert status["open_platform_configured"] is True


# ===========================================================================
# transfer flow failure semantics (§6, §8.1)
# ===========================================================================


def test_transfer_maps_rate_limited(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    http.route("GET", USER_URL, handler=_const(FakeResponse({}, status=429, headers={"Retry-After": "5000"})))
    response = client.post("/api/115/save", json={"share_url": SHARE_URL})
    assert response.status_code == 429
    body = response.get_json()
    assert body["code"] == "115_RATE_LIMITED"
    assert body["retry_after"] == 900


def test_transfer_maps_unknown_shape_to_provider_error(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    http.route("GET", USER_URL, {"unexpected": "shape"})
    response = client.post("/api/115/save", json={"share_url": SHARE_URL})
    assert response.status_code == 502
    assert response.get_json()["code"] == "115_PROVIDER_ERROR"


def test_transfer_retries_snap_once_on_network_error(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    _route_115_session(http)
    calls = {"n": 0}

    def snap_handler(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests_lib.ConnectionError("flaky")
        return FakeResponse({"state": True, "data": {"list": [{"fid": "f1"}]}})

    http.route("GET", SNAP_URL, handler=snap_handler)
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post("/api/115/save", json={"share_url": SHARE_URL})

    assert response.status_code == 200
    assert calls["n"] == 2


def test_transfer_receive_timeout_returns_transfer_unknown_without_retry(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, error=requests_lib.Timeout("slow"))

    response = client.post("/api/115/save", json={"share_url": SHARE_URL})

    assert response.status_code == 202
    assert response.get_json()["code"] == "TRANSFER_UNKNOWN"
    assert len(http.calls_to(RECEIVE_URL)) == 1


def test_transfer_receive_connection_error_also_returns_transfer_unknown(client, hidrive, http):
    # Ambiguous outcomes aren't limited to timeouts -- any transport error
    # after the request left this process could mean 115 already
    # processed it, so it must never be blindly replayed either.
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, error=requests_lib.ConnectionError("dropped"))

    response = client.post("/api/115/save", json={"share_url": SHARE_URL})

    assert response.status_code == 202
    assert response.get_json()["code"] == "TRANSFER_UNKNOWN"
    assert len(http.calls_to(RECEIVE_URL)) == 1


def test_transfer_fast_fails_from_cached_reauth_required_without_network(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.setting_set("115_cookie_state", "reauth_required")
    hidrive.setting_set("115_cookie_checked_at", str(hidrive.utc_now()))
    hidrive.setting_set("115_cookie_fail_streak", "1")

    response = client.post("/api/115/save", json={"share_url": SHARE_URL})

    assert response.status_code == 409
    assert response.get_json()["code"] == "115_REAUTH_REQUIRED"
    assert http.calls == []


def test_transfer_deadline_skips_snap_retry_when_remaining_budget_is_below_threshold(client, hidrive, http, monkeypatch):
    # T19 wave 3 item 1: the two independent per-call budgets this test used
    # to patch (_115_TRANSFER_DEADLINE_SECONDS, measured from
    # save_115_link's own start, and _115_SNAP_RETRY_DEADLINE_SECONDS,
    # measured from that same start) are gone -- there is now exactly one
    # request-scoped deadline (_115_REQUEST_DEADLINE_SECONDS), and
    # share/snap's one read-only retry only runs when
    # >=_115_SNAP_RETRY_MIN_REMAINING seconds of it remain. A fake monotonic
    # clock (rather than a real sleep) makes this deterministic.
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    _route_115_session(http)
    calls = {"n": 0}

    def snap_handler(**kwargs):
        calls["n"] += 1
        # Leaves just under _115_SNAP_RETRY_MIN_REMAINING of the budget.
        clock.advance(hidrive._115_REQUEST_DEADLINE_SECONDS - hidrive._115_SNAP_RETRY_MIN_REMAINING + 1)
        raise requests_lib.ConnectionError("flaky")

    http.route("GET", SNAP_URL, handler=snap_handler)

    response = client.post("/api/115/save", json={"share_url": SHARE_URL})

    assert calls["n"] == 1
    assert response.status_code == 503


def test_transfer_deadline_shortens_receive_timeout_to_remaining_budget(client, hidrive, http, monkeypatch):
    # T19 wave 3 item 1: share/receive must never be the call gunicorn's
    # worker timeout cuts off -- its own timeout is shortened to whatever
    # budget remains. Exercises the actual 3-8s middle branch (a deadline
    # that leaves ~5s), not an extreme, via a deterministic fake clock.
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)

    def slow_verify(**kwargs):
        clock.advance(hidrive._115_REQUEST_DEADLINE_SECONDS - 5)  # leaves ~5s
        return FakeResponse({"state": True, "data": {"uid": "42"}})

    http.route("GET", USER_URL, handler=slow_verify)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post("/api/115/save", json={"share_url": SHARE_URL})

    assert response.status_code == 200
    receive_call = http.calls_to(RECEIVE_URL)[0]
    # T19 wave 4 item 3: timeout is now a (connect, read) tuple.
    assert receive_call["timeout"][0] <= hidrive._115_UPSTREAM_CONNECT_TIMEOUT
    assert receive_call["timeout"][1] < hidrive._115_UPSTREAM_TIMEOUT
    assert receive_call["timeout"][1] > hidrive._115_RECEIVE_MIN_REMAINING
    assert 4.5 < receive_call["timeout"][1] < 5.5


def test_transfer_deadline_skips_retry_and_receive_when_verify_and_snap_exhaust_budget(client, hidrive, http, monkeypatch):
    # T19 wave 3 item 1: previously this asserted a real wall-clock bound
    # (elapsed < 0.16s vs ~0.12s of real sleeps, flagged flaky under CI load
    # in wave 2 N3) -- now fully deterministic via a fake monotonic clock:
    # verify + a failing share/snap attempt eating the rest of the budget
    # between them must skip both the snap retry and share/receive, not
    # just usually finish fast enough.
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)

    verify_calls = {"n": 0}
    snap_calls = {"n": 0}

    def slow_verify(**kwargs):
        verify_calls["n"] += 1
        clock.advance(20)
        return FakeResponse({"state": True, "data": {"uid": "42"}})

    def slow_snap_failure(**kwargs):
        snap_calls["n"] += 1
        clock.advance(3)  # leaves 1s -- below _115_SNAP_RETRY_MIN_REMAINING
        raise requests_lib.ConnectionError("flaky")

    http.route("GET", USER_URL, handler=slow_verify)
    http.route("GET", SNAP_URL, handler=slow_snap_failure)

    response = client.post("/api/115/save", json={"share_url": SHARE_URL})

    assert verify_calls["n"] == 1
    assert snap_calls["n"] == 1  # the retry must be skipped, not attempted
    assert http.calls_to(RECEIVE_URL) == []
    assert response.status_code == 503


def test_snap_never_attempted_due_to_exhausted_budget_returns_transfer_not_attempted(client, hidrive, http, monkeypatch):
    # T19 wave 4 item 4: budget exhaustion before the FIRST share/snap
    # attempt must return the fixed 504 TRANSFER_NOT_ATTEMPTED -- not the
    # generic 502 115_PROVIDER_ERROR a real "share/snap returned a bad
    # response" produces. Previously, when verify alone consumed the whole
    # budget, the retry loop broke on attempt 0 (remaining<=0) leaving both
    # snap and snap_exc None, which fell through to the generic "115 分享
    # 读取失败" 502 branch instead of ever noticing share/snap was never
    # actually called.
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)

    def slow_verify(**kwargs):
        clock.advance(hidrive._115_REQUEST_DEADLINE_SECONDS)  # burns the whole budget
        return FakeResponse({"state": True, "data": {"uid": "42"}})

    http.route("GET", USER_URL, handler=slow_verify)
    # SNAP_URL/RECEIVE_URL deliberately left unrouted -- neither must ever be called.

    response = client.post("/api/115/save", json={"share_url": SHARE_URL})

    assert response.status_code == 504
    assert response.get_json()["code"] == "TRANSFER_NOT_ATTEMPTED"
    assert http.calls_to(SNAP_URL) == []
    assert http.calls_to(RECEIVE_URL) == []


def test_status_reports_retry_after_for_rate_limited_state(client, hidrive, http):
    # Item 8: the settings UI shows a countdown for a rate-limited session.
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    http.route("GET", USER_URL, handler=_const(FakeResponse({}, status=429, headers={"Retry-After": "120"})))
    status = client.get("/api/status?verify_115=1").get_json()["115"]
    assert status["cookie_state"] == "rate_limited"
    assert status["retry_after"] > 0


# ===========================================================================
# Schema migration (T19 wave 3 item 2)
# ===========================================================================


def test_init_db_migrates_legacy_reauth_challenges_table_missing_claim_columns(hidrive, workspace):
    # CREATE TABLE IF NOT EXISTS alone can't add columns to a table that
    # already exists from an earlier deploy (wave 1, before
    # claimed_at/claimed_from existed) -- init_db() must migrate it via
    # PRAGMA table_info + ALTER TABLE, repeatably (run twice below).
    with hidrive.connect_db() as db:
        db.execute("DROP TABLE reauth_challenges")
        db.executescript(
            """
            CREATE TABLE reauth_challenges (
                id_hash TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER,
                error_code TEXT,
                qr_uid_cipher BLOB,
                qr_time INTEGER,
                qr_sign_cipher BLOB
            );
            """
        )

    hidrive.init_db()
    hidrive.init_db()  # repeatable -- must not raise on a second run

    with hidrive.connect_db() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(reauth_challenges)")}
        assert {"claimed_at", "claimed_from"} <= columns
        now = hidrive.utc_now()
        db.execute(
            "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at) VALUES(?,?,?,?,?)",
            ("legacy-hash", "local", "pending", now, now + 300),
        )
        claim = db.execute(
            "UPDATE reauth_challenges SET state='consuming', claimed_at=?, claimed_from=state "
            "WHERE id_hash=? AND consumed_at IS NULL AND state IN ('pending','scanned')",
            (now, "legacy-hash"),
        )
        assert claim.rowcount == 1
        row = db.execute("SELECT * FROM reauth_challenges WHERE id_hash=?", ("legacy-hash",)).fetchone()
        assert row["claimed_from"] == "pending"
        assert row["claimed_at"] == now


def test_reauth_sweep_treats_legacy_null_claimed_at_consuming_row_as_stale(hidrive, workspace):
    # A 'consuming' row can only ever get a NULL claimed_at from a
    # pre-migration leftover (the claim UPDATE always sets it) -- the sweep
    # must treat that the same as a stale claim, not a fresh one.
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        db.execute(
            "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,claimed_at) VALUES(?,?,?,?,?,?)",
            ("legacy-consuming", "local", "consuming", now - 10, now + 290, None),
        )

    hidrive._reauth_expire_abandoned("local", now)

    with hidrive.connect_db() as db:
        row = db.execute("SELECT state, consumed_at, error_code FROM reauth_challenges WHERE id_hash=?", ("legacy-consuming",)).fetchone()
    assert row["state"] == "expired"
    assert row["consumed_at"] is not None
    assert row["error_code"] == "EXPIRED"


# ===========================================================================
# QR re-authorisation challenge lifecycle (§5, §8.1)
# ===========================================================================


def test_reauth_start_issues_challenge_with_only_the_hash_persisted(client, hidrive, http):
    challenge_id = _start_challenge(client, http)
    with hidrive.connect_db() as db:
        row = db.execute("SELECT * FROM reauth_challenges").fetchone()
    assert row["state"] == "pending"
    assert row["actor"] == "local"
    assert row["id_hash"] != challenge_id
    assert hidrive.load_fernet().decrypt(bytes(row["qr_uid_cipher"])).decode() == "qr-uid-fixture-not-real"
    assert hidrive.load_fernet().decrypt(bytes(row["qr_sign_cipher"])).decode() == "qr-sign-fixture-not-real"


def test_reauth_challenge_qr_fields_are_encrypted_at_rest(client, hidrive, http):
    _start_challenge(client, http)
    with hidrive.connect_db() as db:
        row = db.execute("SELECT * FROM reauth_challenges").fetchone()
    blob = bytes(row["qr_uid_cipher"]) + bytes(row["qr_sign_cipher"])
    assert b"qr-uid-fixture-not-real" not in blob
    assert b"qr-sign-fixture-not-real" not in blob
    wrong_key = Fernet(Fernet.generate_key())
    with pytest.raises(InvalidToken):
        wrong_key.decrypt(bytes(row["qr_uid_cipher"]))


def test_reauth_start_rejects_second_concurrent_challenge(client, hidrive, http):
    _start_challenge(client, http)
    second = client.post("/api/115/reauth/start")
    assert second.status_code == 409
    assert second.get_json()["code"] == "REAUTH_IN_PROGRESS"


def test_reauth_start_cooldown_after_repeated_failures(client, hidrive):
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        for i in range(3):
            db.execute(
                "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,consumed_at,error_code) VALUES(?,?,?,?,?,?,?)",
                (f"hash-{i}", "local", "failed", now, now + 1, now, "VERIFY_FAILED"),
            )
    response = client.post("/api/115/reauth/start")
    assert response.status_code == 429
    assert response.get_json()["code"] == "REAUTH_COOLDOWN"


def test_status_reauth_available_false_during_cooldown(client, hidrive):
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        for i in range(3):
            db.execute(
                "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,consumed_at,error_code) VALUES(?,?,?,?,?,?,?)",
                (f"hash-{i}", "local", "failed", now, now + 1, now, "VERIFY_FAILED"),
            )
    assert client.get("/api/status").get_json()["115"]["reauth_available"] is False


def test_reauth_cancel_does_not_count_toward_failure_cooldown(client, hidrive, http):
    # N4 (wave 2): a user-initiated cancel (closing the dialog, refreshing
    # the tab -- the frontend fires a best-effort cancel on close) must not
    # count toward the 3-strike/30-minute failure cooldown, or three
    # ordinary tab closes would lock out the recovery path. This reverses
    # wave 1 item 4's "cancelled counts too" choice for this specific
    # cooldown -- REAUTH_MAX_STARTS_PER_WINDOW (below) is what actually
    # bounds repeated start+cancel spam now.
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        for i in range(3):
            db.execute(
                "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,consumed_at,error_code) VALUES(?,?,?,?,?,?,?)",
                (f"hash-{i}", "local", "cancelled", now, now + 1, now, "CANCELLED_BY_USER"),
            )
    _route_qr_token(http)
    response = client.post("/api/115/reauth/start")
    assert response.status_code != 429


def test_reauth_refresh_qr_headroom_raised_to_eight_starts_per_window(client, hidrive, http):
    # T19 wave 3 item 5: "刷新二维码" (refresh) cancels the previous
    # challenge and immediately starts a new one, so each refresh still
    # spends one /reauth/start -- the simple fix chosen (see
    # REAUTH_MAX_STARTS_PER_WINDOW's docstring for why the simpler, safer
    # option was picked over tracking a per-challenge "reached pending for
    # >=10s" duration) is raising the flat cap from 5 to 8, giving a few
    # ordinary refreshes in one sitting headroom the old cap didn't have.
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        for i in range(6):
            db.execute(
                "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,consumed_at,error_code) VALUES(?,?,?,?,?,?,?)",
                (f"refresh-{i}", "local", "cancelled", now, now + 1, now, "CANCELLED_BY_USER"),
            )
    _route_qr_token(http)
    response = client.post("/api/115/reauth/start")
    assert response.status_code != 429


def test_reauth_cancel_still_bounded_by_starts_per_window_cap(client, hidrive):
    # N4: cancels are excluded from the failure cooldown above, but they
    # still count toward REAUTH_MAX_STARTS_PER_WINDOW (every created row
    # counts, regardless of outcome) -- so spamming start+cancel is still
    # bounded, just by a higher, all-outcomes cap instead of the 3-strike
    # failure one. T19 wave 3 item 5: the cap itself was raised from 5 to 8
    # rows (see REAUTH_MAX_STARTS_PER_WINDOW's docstring).
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        for i in range(hidrive.REAUTH_MAX_STARTS_PER_WINDOW):
            db.execute(
                "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,consumed_at,error_code) VALUES(?,?,?,?,?,?,?)",
                (f"hash-{i}", "local", "cancelled", now, now + 1, now, "CANCELLED_BY_USER"),
            )
    response = client.post("/api/115/reauth/start")
    assert response.status_code == 429
    assert response.get_json()["code"] == "REAUTH_COOLDOWN"


def test_reauth_start_caps_total_starts_per_window_regardless_of_outcome(client, hidrive):
    # Item 4: REAUTH_MAX_STARTS_PER_WINDOW caps *all* /reauth/start attempts
    # per actor per window, even ones that never failed/expired/cancelled --
    # otherwise an actor could hammer the upstream QR token endpoint by
    # completing (or abandoning) challenges quickly enough to never trip the
    # failure-based cooldown above.
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        for i in range(hidrive.REAUTH_MAX_STARTS_PER_WINDOW):
            db.execute(
                "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,consumed_at,error_code) VALUES(?,?,?,?,?,?,?)",
                (f"hash-{i}", "local", "authenticated", now, now + 1, now, None),
            )
    response = client.post("/api/115/reauth/start")
    assert response.status_code == 429
    assert response.get_json()["code"] == "REAUTH_COOLDOWN"
    assert client.get("/api/status").get_json()["115"]["reauth_available"] is False


def test_reauth_start_lazily_expires_abandoned_pending_so_it_counts(client, hidrive, http):
    # Item 4: a challenge nobody ever polls to completion must not be a free
    # pass around the cooldown just because it's still nominally "pending".
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        db.execute(
            "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,consumed_at,error_code) VALUES(?,?,?,?,?,?,?)",
            ("abandoned", "local", "pending", now - 400, now - 100, None, None),
        )
        for i in range(2):
            db.execute(
                "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,consumed_at,error_code) VALUES(?,?,?,?,?,?,?)",
                (f"hash-{i}", "local", "failed", now, now + 1, now, "VERIFY_FAILED"),
            )

    response = client.post("/api/115/reauth/start")

    assert response.status_code == 429
    assert response.get_json()["code"] == "REAUTH_COOLDOWN"
    with hidrive.connect_db() as db:
        abandoned = db.execute("SELECT state, consumed_at FROM reauth_challenges WHERE id_hash='abandoned'").fetchone()
    assert abandoned["state"] == "expired"
    assert abandoned["consumed_at"] is not None


def test_reauth_start_purges_terminal_rows_older_than_24h(client, hidrive, http):
    now = hidrive.utc_now()
    old = now - hidrive.REAUTH_PURGE_AGE_SECONDS - 10
    with hidrive.connect_db() as db:
        db.execute(
            "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,consumed_at,error_code) VALUES(?,?,?,?,?,?,?)",
            ("stale", "local", "failed", old, old + 1, old, "VERIFY_FAILED"),
        )

    _start_challenge(client, http)

    with hidrive.connect_db() as db:
        remaining = db.execute("SELECT 1 FROM reauth_challenges WHERE id_hash='stale'").fetchone()
    assert remaining is None


def test_reauth_qr_proxies_png_with_no_store(client, hidrive, http):
    challenge_id = _start_challenge(client, http)
    http.route("GET", QR_IMAGE_URL, handler=_const(FakeResponse(raw=b"\x89PNG-fixture-not-real", headers={"Content-Type": "image/png"})))
    response = client.get(f"/api/115/reauth/qr?challenge_id={challenge_id}")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "no-store"
    # N5 (wave 2): nosniff -- this response is proxied straight from an
    # upstream we don't fully control.
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.data == b"\x89PNG-fixture-not-real"


def test_reauth_qr_rejects_non_image_content_type(client, hidrive, http):
    # Item 7: this proxies bytes straight to the browser -- an upstream
    # returning e.g. an HTML error page must never be forwarded as-is.
    challenge_id = _start_challenge(client, http)
    http.route("GET", QR_IMAGE_URL, handler=_const(FakeResponse(raw=b"<html>nope</html>", headers={"Content-Type": "text/html"})))
    response = client.get(f"/api/115/reauth/qr?challenge_id={challenge_id}")
    assert response.status_code == 503
    assert response.get_json()["code"] == "115_TEMPORARILY_UNAVAILABLE"


def test_reauth_qr_rejects_svg_content_type(client, hidrive, http):
    # N5 (wave 2): only an *exact* image/png Content-Type is accepted --
    # the old ``startswith("image/")`` check would also have proxied
    # image/svg+xml, whose body can carry embedded script.
    challenge_id = _start_challenge(client, http)
    http.route("GET", QR_IMAGE_URL, handler=_const(FakeResponse(raw=b"<svg onload=alert(1)></svg>", headers={"Content-Type": "image/svg+xml"})))
    response = client.get(f"/api/115/reauth/qr?challenge_id={challenge_id}")
    assert response.status_code == 503
    assert response.get_json()["code"] == "115_TEMPORARILY_UNAVAILABLE"


class _TrackedResponse(FakeResponse):
    """N5 (wave 2): proves the streamed upstream response is closed on
    every exit path, not just the success path."""

    closed_count = {"n": 0}

    def close(self):
        _TrackedResponse.closed_count["n"] += 1


@pytest.mark.parametrize(
    "content_type,raw",
    [("image/png", b"\x89PNG-fixture-not-real"), ("text/html", b"<html>nope</html>")],
    ids=["accepted", "rejected"],
)
def test_reauth_qr_always_closes_the_upstream_response(client, hidrive, http, content_type, raw):
    _TrackedResponse.closed_count["n"] = 0
    challenge_id = _start_challenge(client, http)
    http.route("GET", QR_IMAGE_URL, handler=_const(_TrackedResponse(raw=raw, headers={"Content-Type": content_type})))
    client.get(f"/api/115/reauth/qr?challenge_id={challenge_id}")
    assert _TrackedResponse.closed_count["n"] == 1


def test_reauth_qr_rejects_body_over_the_cap(client, hidrive, http, monkeypatch):
    # Item 7: bound the proxied read regardless of what Content-Length says.
    monkeypatch.setattr(hidrive, "QR_IMAGE_MAX_BYTES", 16)
    challenge_id = _start_challenge(client, http)
    http.route("GET", QR_IMAGE_URL, handler=_const(FakeResponse(raw=b"x" * 1000, headers={"Content-Type": "image/png"})))
    response = client.get(f"/api/115/reauth/qr?challenge_id={challenge_id}")
    assert response.status_code == 503
    assert response.get_json()["code"] == "115_TEMPORARILY_UNAVAILABLE"


def test_reauth_qr_404_when_not_pending(client, hidrive, http):
    challenge_id = _start_challenge(client, http)
    client.post("/api/115/reauth/cancel", json={"challenge_id": challenge_id})
    response = client.get(f"/api/115/reauth/qr?challenge_id={challenge_id}")
    assert response.status_code == 404
    assert response.get_json()["code"] == "REAUTH_NOT_FOUND"


def test_reauth_status_pending_then_scanned_then_confirmed_then_authenticated(client, hidrive, http):
    challenge_id = _start_challenge(client, http)

    http.route("GET", QR_STATUS_URL, {"state": True, "data": {"status": 0}})
    assert client.get(f"/api/115/reauth/status?challenge_id={challenge_id}").get_json()["state"] == "pending"

    http.route("GET", QR_STATUS_URL, {"state": True, "data": {"status": 1}})
    assert client.get(f"/api/115/reauth/status?challenge_id={challenge_id}").get_json()["state"] == "scanned"

    # w6-reauth-longpoll-fix: observing status 2 alone only flips the row to
    # the non-terminal 'confirmed' state and returns immediately -- the
    # cookie exchange itself is a separate follow-up request.
    http.route("GET", QR_STATUS_URL, {"state": True, "data": {"status": 2}})
    confirmed = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
    assert confirmed.get_json()["state"] == "confirmed"

    http.route("POST", QR_RESULT_URL, {"state": True, "data": {"cookie": {"UID": "uid-value-fixture-not-real", "CID": "cid-value-fixture-not-real"}}})
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})

    done = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
    assert done.get_json()["state"] == "authenticated"
    assert hidrive.secret_get("115_cookie") == "UID=uid-value-fixture-not-real; CID=cid-value-fixture-not-real"
    assert hidrive.setting_get("115_cookie_state") == "valid"

    # single consumption: a second read of a terminal outcome is refused.
    again = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
    assert again.status_code == 410
    assert again.get_json()["code"] == "REAUTH_CONSUMED"


def test_reauth_authenticated_but_verify_failed_keeps_old_cookie(client, hidrive, http):
    hidrive.secret_set("115_cookie", "old-cookie-fixture-not-real")
    hidrive.setting_set("115_cookie_state", "valid")
    challenge_id = _start_challenge(client, http)

    http.route("GET", QR_STATUS_URL, {"state": True, "data": {"status": 2}})
    confirmed = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
    assert confirmed.get_json()["state"] == "confirmed"

    http.route("POST", QR_RESULT_URL, {"state": True, "data": {"cookie": {"UID": "new-uid-fixture-not-real"}}})
    http.route("GET", USER_URL, {"state": False})  # the freshly exchanged cookie doesn't verify

    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")

    assert response.get_json()["state"] == "failed"
    assert hidrive.secret_get("115_cookie") == "old-cookie-fixture-not-real"
    assert hidrive.setting_get("115_cookie_state") == "valid"


def test_reauth_status_concurrent_polls_complete_exactly_once(client, hidrive, http, audit_rows):
    # Item 1: two concurrent /status polls claiming the same 'confirmed' row
    # (mirroring gunicorn's 2 sync workers racing the browser's own re-poll
    # for the confirm->complete follow-up request) must never both reach the
    # terminal-completion branch -- exactly one exchange call, one
    # secret_set, one audit row. The exchange handler blocks until a
    # second, concurrent poll has actually observed the claimed
    # ('consuming') row, proving the two requests genuinely overlapped
    # rather than just running in order.
    challenge_id = _start_challenge(client, http)
    id_hash = hidrive._reauth_hash(challenge_id)
    with hidrive.connect_db() as db:
        db.execute("UPDATE reauth_challenges SET state='confirmed' WHERE id_hash=?", (id_hash,))
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})

    exchange_calls = {"n": 0}
    ready = threading.Event()
    proceed = threading.Event()

    def exchange_handler(**kwargs):
        exchange_calls["n"] += 1
        ready.set()
        assert proceed.wait(timeout=5), "second poll never arrived"
        return FakeResponse({"state": True, "data": {"cookie": {"UID": "uid-value-fixture-not-real"}}})

    http.route("POST", QR_RESULT_URL, handler=exchange_handler)

    results: list[dict] = []

    def worker():
        with hidrive.app.test_client() as bg_client:
            results.append(bg_client.get(f"/api/115/reauth/status?challenge_id={challenge_id}").get_json())

    first = threading.Thread(target=worker)
    first.start()
    assert ready.wait(timeout=5), "first poll never reached the exchange call"

    second = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}").get_json()
    proceed.set()
    first.join(timeout=5)

    assert exchange_calls["n"] == 1
    # N1 (wave 2): the internal 'consuming' state must never reach the
    # client -- the losing poll reports the pre-claim state instead
    # (this challenge was 'confirmed' when the winner claimed it).
    assert second["state"] == "confirmed"
    assert results[0]["state"] == "authenticated"
    assert hidrive.secret_get("115_cookie") == "UID=uid-value-fixture-not-real"
    assert len(audit_rows("115.reauth.success")) == 1


def test_reauth_expire_abandoned_sweeps_stale_consuming_claim(client, hidrive, http):
    # N1: a challenge claimed into 'consuming' whose worker never reached a
    # terminal state (killed / uncaught exception right after the claim)
    # must not sit there forever -- the same lazy sweep that expires
    # abandoned pending/scanned rows also expires a 'consuming' row once it
    # has been claimed for longer than REAUTH_CLAIM_STALE_SECONDS.
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        db.execute(
            "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,claimed_at,claimed_from) VALUES(?,?,?,?,?,?,?)",
            ("stuck", "local", "consuming", now - 200, now + 1000, now - hidrive.REAUTH_CLAIM_STALE_SECONDS - 1, "scanned"),
        )

    _start_challenge(client, http)

    with hidrive.connect_db() as db:
        stuck = db.execute("SELECT state, consumed_at, error_code FROM reauth_challenges WHERE id_hash='stuck'").fetchone()
    assert stuck["state"] == "expired"
    assert stuck["consumed_at"] is not None
    assert stuck["error_code"] == "EXPIRED"


def test_reauth_expire_abandoned_leaves_a_fresh_consuming_claim_alone(client, hidrive, http):
    # The stale sweep must not clobber a claim that is still genuinely
    # in-flight (well under REAUTH_CLAIM_STALE_SECONDS old, not yet past
    # its own expires_at either). T19 wave 3 item 3: a fresh 'consuming'
    # claim now also counts as "in progress" for the very next
    # /reauth/start, so this call itself returns 409 -- the sweep (the
    # route's first step, run regardless of that outcome) must still have
    # left this fresh claim's row untouched.
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        db.execute(
            "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at,claimed_at,claimed_from) VALUES(?,?,?,?,?,?,?)",
            ("fresh", "local", "consuming", now - 5, now + 1000, now - 1, "scanned"),
        )

    response = client.post("/api/115/reauth/start")

    assert response.status_code == 409
    assert response.get_json()["code"] == "REAUTH_IN_PROGRESS"
    with hidrive.connect_db() as db:
        fresh = db.execute("SELECT state, consumed_at FROM reauth_challenges WHERE id_hash='fresh'").fetchone()
    assert fresh["state"] == "consuming"
    assert fresh["consumed_at"] is None


def test_reauth_cancel_can_clear_a_stuck_consuming_row(client, hidrive, http):
    # N1: a challenge stuck in 'consuming' must have a way out for the user
    # too, not just the lazy expiry sweep on the next /reauth/start.
    challenge_id = _start_challenge(client, http)
    id_hash = hidrive._reauth_hash(challenge_id)
    with hidrive.connect_db() as db:
        db.execute(
            "UPDATE reauth_challenges SET state='consuming', claimed_at=?, claimed_from='scanned' WHERE id_hash=?",
            (hidrive.utc_now() - 61, id_hash),
        )

    response = client.post("/api/115/reauth/cancel", json={"challenge_id": challenge_id})

    assert response.status_code == 200
    assert response.get_json()["state"] == "cancelled"


def test_reauth_status_guards_against_row_disappearing_between_claim_and_reread(client, hidrive, http, monkeypatch):
    # N8: a losing poll's re-read (after failing to claim the row) must not
    # crash with a TypeError if the row is gone by the time it re-reads --
    # treat it the same as "not found" instead.
    challenge_id = _start_challenge(client, http)
    id_hash = hidrive._reauth_hash(challenge_id)
    with hidrive.connect_db() as db:
        db.execute("UPDATE reauth_challenges SET state='consuming' WHERE id_hash=?", (id_hash,))

    original_row = hidrive._reauth_row
    calls = {"n": 0}

    def flaky_row(h):
        calls["n"] += 1
        if calls["n"] == 1:
            return original_row(h)
        return None

    monkeypatch.setattr(hidrive, "_reauth_row", flaky_row)

    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")

    assert response.status_code == 404
    assert response.get_json()["code"] == "REAUTH_NOT_FOUND"


def test_reauth_status_marks_row_failed_when_post_claim_work_raises(client, hidrive, http, monkeypatch, audit_rows):
    # N1: an uncaught exception after the claim (e.g. a worker crash) must
    # not leave the row stuck in 'consuming' waiting for the lazy sweep --
    # it is marked 'failed' immediately, finally-style, and the original
    # exception still propagates (production: Flask's 500 handler; here,
    # with TESTING=True, it propagates to the test directly).
    challenge_id = _start_challenge(client, http)

    def boom(row, deadline):
        raise RuntimeError("simulated crash after claim")

    monkeypatch.setattr(hidrive, "_reauth_poll_upstream", boom)

    with pytest.raises(RuntimeError):
        client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")

    with hidrive.connect_db() as db:
        row = db.execute("SELECT * FROM reauth_challenges WHERE id_hash=?", (hidrive._reauth_hash(challenge_id),)).fetchone()
    assert row["state"] == "failed"
    assert row["consumed_at"] is not None
    assert len(audit_rows("115.reauth.failed")) == 1


def test_reauth_status_authenticated_terminal_update_and_audit_are_atomic(client, hidrive, http, monkeypatch):
    # N1: the terminal state UPDATE and its audit row are written in the
    # same transaction -- if writing the audit fails, the state change is
    # rolled back with it (never left as a committed 'authenticated' with
    # no matching audit row), and the outer exception guard then marks the
    # row 'failed' instead.
    challenge_id = _start_challenge(client, http)
    id_hash = hidrive._reauth_hash(challenge_id)
    with hidrive.connect_db() as db:
        db.execute("UPDATE reauth_challenges SET state='confirmed' WHERE id_hash=?", (id_hash,))
    http.route("POST", QR_RESULT_URL, {"state": True, "data": {"cookie": {"UID": "uid-value-fixture-not-real"}}})
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})

    real_audit = hidrive.audit

    def flaky_audit(action, status, detail="", actor="", db=None):
        if action == "115.reauth.success":
            raise RuntimeError("simulated audit failure")
        return real_audit(action, status, detail, actor, db=db)

    monkeypatch.setattr(hidrive, "audit", flaky_audit)

    with pytest.raises(RuntimeError):
        client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")

    # the cookie exchange (secret_set) already happened, inside
    # _reauth_complete, before the transactional terminal update/audit --
    # that part isn't rolled back.
    assert hidrive.secret_get("115_cookie") == "UID=uid-value-fixture-not-real"
    with hidrive.connect_db() as db:
        row = db.execute("SELECT * FROM reauth_challenges WHERE id_hash=?", (hidrive._reauth_hash(challenge_id),)).fetchone()
    # never left as 'authenticated' without its audit row -- the outer
    # guard's own cleanup takes over and marks it 'failed' instead.
    assert row["state"] == "failed"


def test_reauth_status_terminal_update_loses_to_a_concurrent_cancel(client, hidrive, http, monkeypatch, audit_rows):
    # T19 wave 3 item 4: if a concurrent /reauth/cancel commits its
    # consumed_at first (e.g. the user closed the dialog while this poll's
    # exchange was still in flight), the terminal UPDATE here must not
    # clobber it -- the row's actual stored state (cancelled) is reported
    # instead of the one this request just computed, and a distinct audit
    # action records the mismatch instead of a fabricated success row.
    challenge_id = _start_challenge(client, http)
    id_hash = hidrive._reauth_hash(challenge_id)
    with hidrive.connect_db() as db:
        db.execute("UPDATE reauth_challenges SET state='confirmed' WHERE id_hash=?", (id_hash,))
    http.route("POST", QR_RESULT_URL, {"state": True, "data": {"cookie": {"UID": "uid-value-fixture-not-real"}}})
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})

    real_complete = hidrive._reauth_complete

    def complete_after_racing_cancel(row, deadline):
        # Simulates /reauth/cancel committing in the gap between this
        # request's claim and its own terminal UPDATE.
        with hidrive.connect_db() as db:
            db.execute(
                "UPDATE reauth_challenges SET state='cancelled', error_code='CANCELLED_BY_USER', consumed_at=? WHERE id_hash=?",
                (hidrive.utc_now(), id_hash),
            )
        return real_complete(row, deadline)

    monkeypatch.setattr(hidrive, "_reauth_complete", complete_after_racing_cancel)

    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")

    assert response.status_code == 200
    assert response.get_json()["state"] == "cancelled"
    with hidrive.connect_db() as db:
        row = db.execute("SELECT * FROM reauth_challenges WHERE id_hash=?", (id_hash,)).fetchone()
    assert row["state"] == "cancelled"
    assert len(audit_rows("115.reauth.completed_after_cancel")) == 1
    assert audit_rows("115.reauth.success") == []


def test_reauth_status_exception_guard_skips_audit_when_cancel_already_won(client, hidrive, http, monkeypatch, audit_rows):
    # T19 wave 3 item 4: the exception-guard's own terminal UPDATE is
    # already conditional on consumed_at IS NULL (wave 2 N1) -- its audit
    # must likewise only fire when that UPDATE actually matched a row, not
    # when a concurrent cancel already committed the terminal state first.
    challenge_id = _start_challenge(client, http)
    id_hash = hidrive._reauth_hash(challenge_id)

    def boom(row, deadline):
        with hidrive.connect_db() as db:
            db.execute(
                "UPDATE reauth_challenges SET state='cancelled', error_code='CANCELLED_BY_USER', consumed_at=? WHERE id_hash=?",
                (hidrive.utc_now(), id_hash),
            )
        raise RuntimeError("simulated crash after a concurrent cancel already won")

    monkeypatch.setattr(hidrive, "_reauth_poll_upstream", boom)

    with pytest.raises(RuntimeError):
        client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")

    with hidrive.connect_db() as db:
        row = db.execute("SELECT * FROM reauth_challenges WHERE id_hash=?", (id_hash,)).fetchone()
    assert row["state"] == "cancelled"  # untouched by the exception guard
    assert audit_rows("115.reauth.failed") == []


def test_reauth_status_timeout_stays_pending(client, hidrive, http):
    challenge_id = _start_challenge(client, http)
    http.route("GET", QR_STATUS_URL, error=requests_lib.Timeout("slow"))
    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
    assert response.get_json()["state"] == "pending"


def test_reauth_status_expires_after_ttl(client, hidrive, http):
    challenge_id = _start_challenge(client, http)
    with hidrive.connect_db() as db:
        db.execute("UPDATE reauth_challenges SET expires_at=?", (hidrive.utc_now() - 1,))
    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
    assert response.get_json()["state"] == "expired"
    # terminal + consumed in the same read
    assert client.get(f"/api/115/reauth/status?challenge_id={challenge_id}").status_code == 410


def test_reauth_cancel_marks_cancelled_and_is_single_use(client, hidrive, http):
    challenge_id = _start_challenge(client, http)
    response = client.post("/api/115/reauth/cancel", json={"challenge_id": challenge_id})
    assert response.status_code == 200
    assert response.get_json()["state"] == "cancelled"
    again = client.post("/api/115/reauth/cancel", json={"challenge_id": challenge_id})
    assert again.status_code == 410
    assert again.get_json()["code"] == "REAUTH_CONSUMED"
    assert client.get(f"/api/115/reauth/status?challenge_id={challenge_id}").status_code == 410


def test_reauth_unknown_challenge_id_returns_404(client, hidrive):
    assert client.get("/api/115/reauth/status?challenge_id=does-not-exist").status_code == 404
    assert client.post("/api/115/reauth/cancel", json={"challenge_id": "does-not-exist"}).status_code == 404


def test_reauth_status_rejects_other_actor(client, hidrive, http, actor_mode):
    alice = _write_headers(client, "alice-assertion")
    bob = {"Cf-Access-Jwt-Assertion": "bob-assertion"}
    _route_qr_token(http)
    challenge_id = client.post("/api/115/reauth/start", headers=alice).get_json()["challenge_id"]

    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}", headers=bob)
    assert response.status_code == 404
    assert response.get_json()["code"] == "REAUTH_NOT_FOUND"

    qr_response = client.get(f"/api/115/reauth/qr?challenge_id={challenge_id}", headers=bob)
    assert qr_response.status_code == 404

    cancel_response = client.post("/api/115/reauth/cancel", json={"challenge_id": challenge_id}, headers=_write_headers(client, "bob-assertion"))
    assert cancel_response.status_code == 404


def test_reauth_flow_never_leaks_qr_or_cookie_values(client, hidrive, http, audit_rows):
    _route_qr_token(http)
    start = client.post("/api/115/reauth/start")
    challenge_id = start.get_json()["challenge_id"]

    http.route("GET", QR_IMAGE_URL, handler=_const(FakeResponse(raw=b"\x89PNG-fixture-not-real")))
    client.get(f"/api/115/reauth/qr?challenge_id={challenge_id}")

    http.route("GET", QR_STATUS_URL, {"state": True, "data": {"status": 2}})
    http.route("POST", QR_RESULT_URL, {"state": True, "data": {"cookie": {"UID": "uid-value-fixture-not-real"}}})
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})
    status_response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")

    response_blob = json.dumps(start.get_json(), ensure_ascii=False) + json.dumps(status_response.get_json(), ensure_ascii=False)
    for marker in ("qr-uid-fixture-not-real", "qr-sign-fixture-not-real", "qr-content-fixture-not-real", "uid-value-fixture-not-real"):
        assert marker not in response_blob

    with hidrive.connect_db() as db:
        audit_blob = " ".join(f"{r['action']} {r['detail']}" for r in db.execute("SELECT action, detail FROM audit_log").fetchall())
    for marker in ("qr-uid-fixture-not-real", "qr-sign-fixture-not-real", "qr-content-fixture-not-real", "uid-value-fixture-not-real", challenge_id):
        assert marker not in audit_blob


# ===========================================================================
# w6-reauth-longpoll: 115's get/status is a long-poll endpoint -- it holds
# the connection until the QR status changes or ~30s elapse, so the old flat
# 8s read timeout abandoned every poll before 115 could ever answer.
# ===========================================================================


@pytest.mark.parametrize(
    "remaining_budget, expected_read",
    [
        (100, 15),  # far from the deadline: capped at REAUTH_STATUS_POLL_READ_SECONDS
        (10, 10),   # near the deadline: bounded by whatever budget remains
        (1, 2),     # almost exhausted: never below the 2s floor
        (-5, 2),    # already past the deadline: still never below the 2s floor
    ],
)
def test_reauth_poll_upstream_read_timeout_bounded_by_deadline(client, hidrive, http, monkeypatch, remaining_budget, expected_read):
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    challenge_id = _start_challenge(client, http)
    row = hidrive._reauth_row(hidrive._reauth_hash(challenge_id))
    http.route("GET", QR_STATUS_URL, {"state": True, "data": {"status": 0}})

    # read = max(2, min(REAUTH_STATUS_POLL_READ_SECONDS,
    # deadline - now - _115_UPSTREAM_CONNECT_TIMEOUT - 1)) -- the clock is
    # frozen, so this arithmetic is exact, not epsilon-prone.
    deadline = clock() + remaining_budget + hidrive._115_UPSTREAM_CONNECT_TIMEOUT + 1
    hidrive._reauth_poll_upstream(row, deadline)

    call = http.calls_to(QR_STATUS_URL)[-1]
    assert call["timeout"] == (hidrive._115_UPSTREAM_CONNECT_TIMEOUT, expected_read)


def test_reauth_poll_upstream_sends_cache_buster_param(client, hidrive, http):
    challenge_id = _start_challenge(client, http)
    row = hidrive._reauth_row(hidrive._reauth_hash(challenge_id))
    http.route("GET", QR_STATUS_URL, {"state": True, "data": {"status": 0}})

    hidrive._reauth_poll_upstream(row, hidrive.time.monotonic() + hidrive._115_REQUEST_DEADLINE_SECONDS)

    call = http.calls_to(QR_STATUS_URL)[-1]
    assert isinstance(call["params"]["_"], int) and call["params"]["_"] > 0


def test_reauth_status_long_poll_observes_confirmation_in_a_single_call(client, hidrive, http, monkeypatch):
    # The core regression: 115 can legitimately hold the get/status
    # connection open for several seconds before answering "confirmed" --
    # that must be observed by the SAME poll, not dropped as a timeout.
    # w6-reauth-longpoll-fix: this one poll only reaches the non-terminal
    # 'confirmed' state now -- QR_RESULT_URL/USER_URL are deliberately left
    # unrouted, so FakeHTTP would raise if the exchange were (wrongly)
    # attempted in this same request.
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    challenge_id = _start_challenge(client, http)

    status_calls = {"n": 0}

    def slow_status(**kwargs):
        status_calls["n"] += 1
        # The read timeout must be long enough to actually cover a 12s hold.
        assert kwargs["timeout"][1] >= 12
        clock.advance(12)
        return FakeResponse({"state": True, "data": {"status": 2}})

    http.route("GET", QR_STATUS_URL, handler=slow_status)

    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")

    assert status_calls["n"] == 1
    assert response.get_json()["state"] == "confirmed"


def test_reauth_complete_sends_app_web(client, hidrive, http):
    # Brief item 2: the reference client sends both account and app=web.
    challenge_id = _start_challenge(client, http)
    http.route("GET", QR_STATUS_URL, {"state": True, "data": {"status": 2}})
    confirmed = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
    assert confirmed.get_json()["state"] == "confirmed"

    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})

    captured = {}

    def exchange_handler(**kwargs):
        captured.update(kwargs)
        return FakeResponse({"state": True, "data": {"cookie": {"UID": "uid-value-fixture-not-real"}}})

    http.route("POST", QR_RESULT_URL, handler=exchange_handler)

    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")

    assert response.get_json()["state"] == "authenticated"
    assert captured["data"] == {"account": "qr-uid-fixture-not-real", "app": "web"}
    # The confirm->complete split (item 1): the follow-up request must skip
    # the upstream poll entirely, not re-poll get/status before exchanging.
    assert len(http.calls_to(QR_STATUS_URL)) == 1


# ===========================================================================
# w6-reauth-longpoll-fix: the confirm/complete split (finding 1) -- get/
# status observing status 2 writes the new, non-terminal 'confirmed' state
# and returns immediately instead of running the cookie exchange in the same
# request; the browser's very next status request claims 'confirmed' and
# runs the exchange with its own fresh deadline budget.
# ===========================================================================


def test_reauth_complete_defers_exchange_when_budget_nearly_exhausted(client, hidrive, http, monkeypatch):
    # Finding 2: _reauth_complete must never fail a confirmed challenge just
    # because this particular request is almost out of budget -- it leaves
    # the row exactly where it was ('confirmed', no error) for the next
    # poll to retry with a fresh deadline, and never even attempts the
    # exchange call.
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    challenge_id = _start_challenge(client, http)
    row = hidrive._reauth_row(hidrive._reauth_hash(challenge_id))

    deadline = clock() + 1  # less than _115_UPSTREAM_CONNECT_TIMEOUT
    state, error_code = hidrive._reauth_complete(row, deadline)

    assert state == "confirmed"
    assert error_code is None
    assert http.calls_to(QR_RESULT_URL) == []  # the exchange must never even be attempted


def test_reauth_expire_abandoned_sweeps_a_confirmed_row_past_its_ttl(hidrive, workspace):
    # A challenge 115 already confirmed, but whose browser tab never came
    # back for the follow-up exchange request, must still expire by its own
    # TTL -- exactly like an abandoned 'pending'/'scanned' row.
    now = hidrive.utc_now()
    with hidrive.connect_db() as db:
        db.execute(
            "INSERT INTO reauth_challenges(id_hash,actor,state,created_at,expires_at) VALUES(?,?,?,?,?)",
            ("abandoned-confirmed", "local", "confirmed", now - 400, now - 10),
        )

    hidrive._reauth_expire_abandoned("local", now)

    with hidrive.connect_db() as db:
        row = db.execute("SELECT state, consumed_at, error_code FROM reauth_challenges WHERE id_hash=?", ("abandoned-confirmed",)).fetchone()
    assert row["state"] == "expired"
    assert row["consumed_at"] is not None
    assert row["error_code"] == "EXPIRED"


def test_reauth_start_rejects_new_challenge_while_one_is_confirmed(client, hidrive, http):
    # A 'confirmed' challenge (115 approved it; the exchange is only
    # deferred, not abandoned) must still count as "in progress" -- starting
    # a fresh one would orphan a login the user already approved.
    challenge_id = _start_challenge(client, http)
    id_hash = hidrive._reauth_hash(challenge_id)
    with hidrive.connect_db() as db:
        db.execute("UPDATE reauth_challenges SET state='confirmed' WHERE id_hash=?", (id_hash,))

    response = client.post("/api/115/reauth/start")

    assert response.status_code == 409
    assert response.get_json()["code"] == "REAUTH_IN_PROGRESS"


def test_reauth_cancel_can_clear_a_confirmed_row(client, hidrive, http):
    # The user must still be able to back out of a 'confirmed' challenge
    # before the follow-up exchange request runs.
    challenge_id = _start_challenge(client, http)
    id_hash = hidrive._reauth_hash(challenge_id)
    with hidrive.connect_db() as db:
        db.execute("UPDATE reauth_challenges SET state='confirmed' WHERE id_hash=?", (id_hash,))

    response = client.post("/api/115/reauth/cancel", json={"challenge_id": challenge_id})

    assert response.status_code == 200
    assert response.get_json()["state"] == "cancelled"


# ===========================================================================
# w6-reauth-longpoll-fix finding 2: end-to-end worst-case timing, asserted
# against a REAL request (not vacuous constant arithmetic) using the
# fixtures' FakeMonotonic to simulate each upstream call taking its full
# allotted timeout, without a genuinely slow test.
# ===========================================================================


def test_reauth_status_poll_worst_case_stays_under_26s_of_simulated_time(client, hidrive, http, monkeypatch):
    # (a) A status request whose get/status call holds the full (connect +
    # read) timeout before answering -- worst case
    # _115_UPSTREAM_CONNECT_TIMEOUT(3) + REAUTH_STATUS_POLL_READ_SECONDS(15)
    # = 18s, comfortably under gunicorn's 30s worker timeout.
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    challenge_id = _start_challenge(client, http)

    def slow_status(**kwargs):
        connect_t, read_t = kwargs["timeout"]
        clock.advance(connect_t + read_t)
        return FakeResponse({"state": True, "data": {"status": 0}})

    http.route("GET", QR_STATUS_URL, handler=slow_status)

    start = clock()
    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
    elapsed = clock() - start

    assert response.get_json()["state"] == "pending"
    assert elapsed == hidrive._115_UPSTREAM_CONNECT_TIMEOUT + hidrive.REAUTH_STATUS_POLL_READ_SECONDS
    assert elapsed < 26


def test_reauth_status_complete_worst_case_stays_under_26s_of_simulated_time(client, hidrive, http, monkeypatch):
    # (b) A status request that claims a 'confirmed' row and runs the
    # cookie exchange + verify, each call taking its own full (connect,
    # read) timeout -- worst case 2 * (3 + 8) = 22s, still comfortably
    # under gunicorn's 30s worker timeout even stacked with (a)'s poll
    # having already run in an earlier request.
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    challenge_id = _start_challenge(client, http)
    id_hash = hidrive._reauth_hash(challenge_id)
    with hidrive.connect_db() as db:
        db.execute("UPDATE reauth_challenges SET state='confirmed' WHERE id_hash=?", (id_hash,))

    def slow_exchange(**kwargs):
        connect_t, read_t = kwargs["timeout"]
        clock.advance(connect_t + read_t)
        return FakeResponse({"state": True, "data": {"cookie": {"UID": "uid-value-fixture-not-real"}}})

    def slow_verify(**kwargs):
        connect_t, read_t = kwargs["timeout"]
        clock.advance(connect_t + read_t)
        return FakeResponse({"state": True, "data": {"uid": "42"}})

    http.route("POST", QR_RESULT_URL, handler=slow_exchange)
    http.route("GET", USER_URL, handler=slow_verify)

    start = clock()
    response = client.get(f"/api/115/reauth/status?challenge_id={challenge_id}")
    elapsed = clock() - start

    assert response.get_json()["state"] == "authenticated"
    expected = 2 * (hidrive._115_UPSTREAM_CONNECT_TIMEOUT + hidrive._115_UPSTREAM_TIMEOUT)
    assert elapsed == expected
    assert elapsed < 26
