"""115 share-link parsing, one-click saving and OpenList-backed folder picking."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
import requests as requests_lib

USER_URL = "https://my.115.com/?ct=ajax&ac=get_user_aq"
SNAP_URL = "https://webapi.115.com/share/snap"
RECEIVE_URL = "https://webapi.115.com/share/receive"
OPEN_FILES_URL = "https://proapi.115.com/open/ufile/files"
OPENLIST_LIST_URL = "http://openlist.test/api/fs/list"
COOKIE_FIXTURE = "session=fixture-cookie-not-real"


def _write_openlist_source_db(
    path, *, token="fake-openlist-token", access="fake-open-access", refresh="fake-open-refresh",
    root_folder_id="77", mount_path="/115pan",
) -> None:
    """A minimal stand-in for OpenList's own sqlite database, just enough
    for ``sync_openlist_credentials_from_source`` to read a token and the
    115-Open-Platform storage's ``addition`` JSON blob out of it."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE x_setting_items (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO x_setting_items (key, value) VALUES ('token', ?)", (token,))
        conn.execute("CREATE TABLE x_storages (mount_path TEXT, driver TEXT, addition TEXT)")
        addition = json.dumps({"access_token": access, "refresh_token": refresh, "root_folder_id": root_folder_id})
        conn.execute(
            "INSERT INTO x_storages (mount_path, driver, addition) VALUES (?, '115 Open', ?)",
            (mount_path, addition),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "link,expected",
    [
        ("https://115.com/s/swabc123?password=k9x8", ("swabc123", "k9x8")),
        ("https://115cdn.com/s/swabc123?password=k9x8#", ("swabc123", "k9x8")),
        ("https://anxia.com/s/swabc123?pwd=k9x8", ("swabc123", "k9x8")),
        ("https://115.com/s/swabc123#password=k9x8", ("swabc123", "k9x8")),
        ("https://www.115.com/swabc123?password=k9x8", ("swabc123", "k9x8")),
    ],
    ids=["115.com-password", "115cdn-empty-fragment", "anxia-pwd", "password-in-fragment", "no-s-segment"],
)
def test_parse_115_link_accepts_supported_forms(hidrive, link, expected):
    assert hidrive.parse_115_link(link) == expected


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/s/swabc123?password=k9x8",
        "https://115.com/s/swabc123",
        "https://115.com/s/swabc123/extra?password=k9x8",
        "https://115.com/s/bad%20code?password=k9x8",
        "not a link",
    ],
    ids=["foreign-host", "missing-password", "extra-path", "invalid-share-code", "not-a-link"],
)
def test_parse_115_link_rejects_unsupported_forms(hidrive, link):
    assert hidrive.parse_115_link(link) is None


def test_extract_share_link_pulls_link_out_of_chat_text(hidrive):
    text = "分享给你：https://115.com/s/swabc123?password=k9x8。 另外 https://example.com/x"
    assert hidrive.extract_share_link(text) == "https://115.com/s/swabc123?password=k9x8"
    assert hidrive.extract_share_link("没有链接") is None
    assert hidrive.extract_share_link(123) is None


def _route_115_session(http, *, uid="42"):
    http.route("GET", USER_URL, {"state": True, "data": {"uid": uid}})


def test_save_rejects_text_without_valid_link(client, hidrive):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    response = client.post("/api/115/save", json={"share_url": "https://example.com/nope"})
    assert response.status_code == 400


def test_save_requires_cookie(client, audit_rows):
    # T19: an unconfigured cookie is folded into the same 115_REAUTH_REQUIRED
    # code/message as an expired one -- both are resolved by the same QR
    # re-auth flow (docs/115-integration.md
    # §6.2).
    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})
    assert response.status_code == 409
    assert response.get_json() == {"success": False, "message": "115 需要重新授权，请到设置页扫码", "code": "115_REAUTH_REQUIRED"}
    assert audit_rows("115.save")[0]["status"] == "failed"


def test_save_receives_every_share_item_into_target_folder(client, hidrive, http, audit_rows):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    # T17 fix wave 1 item 1: a bare target_pid now needs server-side proof --
    # here that's simply that it matches the stored default.
    hidrive.setting_set("115_target_pid", "999")
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}, {"cid": "c2"}, {"name": "no-id"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post("/api/115/save", json={"share_url": "看看这个 https://115.com/s/swabc123?password=k9x8。", "target_pid": "999"})

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    snap_call = http.calls_to(SNAP_URL)[0]
    assert snap_call["params"]["share_code"] == "swabc123"
    assert snap_call["params"]["receive_code"] == "k9x8"
    assert snap_call["headers"]["Cookie"] == COOKIE_FIXTURE
    (receive,) = http.calls_to(RECEIVE_URL)
    assert receive["data"] == {"user_id": "42", "share_code": "swabc123", "receive_code": "k9x8", "file_id": "f1,c2", "cid": "999"}
    assert audit_rows("115.save")[0]["status"] == "success"
    assert hidrive.setting_get("115_cookie_valid") == "1"


def test_save_uses_stored_default_pid(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.setting_set("115_target_pid", "555")
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    assert http.calls_to(RECEIVE_URL)[0]["data"]["cid"] == "555"


def test_save_falls_back_to_stored_pid_when_target_pid_is_whitespace_only(client, hidrive, http):
    # Minor fix: `body.get("target_pid") or setting_get(...)` treated a
    # whitespace-only "target_pid" string as a truthy, real value (it only
    # got stripped to "" afterwards), so the stored default was never
    # consulted and the transfer was sent with an empty cid.
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.setting_set("115_target_pid", "555")
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_pid": "   "})

    assert http.calls_to(RECEIVE_URL)[0]["data"]["cid"] == "555"


def test_save_treats_duplicate_receive_as_success(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": False, "error": "无需重复接收"})

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    assert response.status_code == 200
    assert response.get_json()["success"] is True


def test_save_marks_cookie_invalid_when_115_rejects_session(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    http.route("GET", USER_URL, {"state": False})

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    assert response.status_code == 409
    body = response.get_json()
    assert body["code"] == "115_REAUTH_REQUIRED"
    assert body["message"] == "115 需要重新授权，请到设置页扫码"
    assert hidrive.setting_get("115_cookie_valid") == "0"
    assert hidrive.setting_get("115_cookie_state") == "reauth_required"
    assert http.calls_to(RECEIVE_URL) == []


def test_save_reports_empty_share(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": []}})

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    assert response.status_code == 502
    assert response.get_json()["message"] == "115 分享内没有可转存条目"


def test_save_snap_error_never_forwards_raw_upstream_text(client, hidrive, http):
    # Item 5: only a fixed, generic message reaches the user -- 115's raw
    # `error` text (which could contain internal/account-specific detail)
    # must never be forwarded verbatim.
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": False, "error": "raw upstream detail marker not-real"})

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    assert response.status_code == 502
    body = response.get_json()
    assert body["code"] == "115_PROVIDER_ERROR"
    assert "raw upstream detail marker" not in body["message"]


def test_save_receive_error_never_forwards_raw_upstream_text(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": False, "error": "raw upstream detail marker not-real"})

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    assert response.status_code == 502
    body = response.get_json()
    assert body["code"] == "115_PROVIDER_ERROR"
    assert "raw upstream detail marker" not in body["message"]


def test_save_reports_network_failure(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    http.route("GET", USER_URL, error=requests_lib.ConnectionError("down"))

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    assert response.status_code == 503
    body = response.get_json()
    assert body["code"] == "115_TEMPORARILY_UNAVAILABLE"


# --- target path resolution through the 115 Open Platform -------------------


def test_save_resolves_openlist_path_to_115_cid(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.secret_set("115_open_access_token", "open-access-fixture")
    hidrive.setting_set("115_open_root_cid", "10")
    listings = {
        "10": [{"fn": "Movies", "fc": "0", "fid": "20"}, {"fn": "Movies", "fc": "1", "fid": "99"}],
        "20": [{"fn": "2024", "fc": "0", "fid": "30"}],
    }

    def open_files(**kwargs):
        from conftest import FakeResponse

        assert kwargs["headers"]["Authorization"] == "Bearer open-access-fixture"
        return FakeResponse({"state": True, "data": listings[kwargs["params"]["cid"]]})

    http.route("GET", OPEN_FILES_URL, handler=open_files)
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_path": "/115pan/Movies/2024"})

    assert response.status_code == 200
    assert http.calls_to(RECEIVE_URL)[0]["data"]["cid"] == "30"


# --- T17 item 5: target_pid cross-checked against target_path -------------


def test_save_accepts_target_pid_matching_resolved_path(client, hidrive, http):
    # §12.5's "current path is the target path" UI sends BOTH the path (for
    # display) and its own resolved pid together -- when they agree, the
    # transfer proceeds exactly as with target_path alone.
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.secret_set("115_open_access_token", "open-access-fixture")
    hidrive.setting_set("115_open_root_cid", "10")
    listings = {"10": [{"fn": "Movies", "fc": "0", "fid": "20"}]}

    def open_files(**kwargs):
        from conftest import FakeResponse

        return FakeResponse({"state": True, "data": listings[kwargs["params"]["cid"]]})

    http.route("GET", OPEN_FILES_URL, handler=open_files)
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post(
        "/api/115/save",
        json={
            "share_url": "https://115.com/s/swabc123?password=k9x8",
            "target_path": "/115pan/Movies",
            "target_pid": "20",
        },
    )

    assert response.status_code == 200
    assert http.calls_to(RECEIVE_URL)[0]["data"]["cid"] == "20"


def test_save_rejects_target_pid_mismatching_resolved_path(client, hidrive, http):
    # The client-displayed path must never become the security boundary:
    # a target_pid that disagrees with what target_path actually resolves
    # to server-side is rejected outright, and the transfer never runs.
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.secret_set("115_open_access_token", "open-access-fixture")
    hidrive.setting_set("115_open_root_cid", "10")
    listings = {"10": [{"fn": "Movies", "fc": "0", "fid": "20"}]}

    def open_files(**kwargs):
        from conftest import FakeResponse

        return FakeResponse({"state": True, "data": listings[kwargs["params"]["cid"]]})

    http.route("GET", OPEN_FILES_URL, handler=open_files)
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post(
        "/api/115/save",
        json={
            "share_url": "https://115.com/s/swabc123?password=k9x8",
            "target_path": "/115pan/Movies",
            "target_pid": "999999",  # forged/stale -- does not match the real "20"
        },
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "TARGET_PID_INVALID"
    assert http.calls_to(SNAP_URL) == []
    assert http.calls_to(RECEIVE_URL) == []


# --- T17 fix wave 1 item 1: a bare target_pid with no target_path is no
# longer trusted verbatim -- without independent server-side proof (a
# resolved target_path, the stored default, or the configured /115pan root
# cid) it could point anywhere in the account, escaping the /115pan
# boundary entirely. ---------------------------------------------------


def test_save_rejects_target_pid_without_target_path_or_proof(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)

    response = client.post(
        "/api/115/save",
        json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_pid": "666"},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "TARGET_PID_INVALID"
    # rejected before ANY upstream call -- not even the session verify.
    assert http.calls == []


def test_save_accepts_target_pid_matching_stored_default_without_path(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.setting_set("115_target_pid", "555")
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post(
        "/api/115/save",
        json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_pid": "555"},
    )

    assert response.status_code == 200
    assert http.calls_to(RECEIVE_URL)[0]["data"]["cid"] == "555"


def test_save_accepts_target_pid_matching_configured_115pan_root_cid(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.setting_set("115_open_root_cid", "10")
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post(
        "/api/115/save",
        json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_pid": "10"},
    )

    assert response.status_code == 200
    assert http.calls_to(RECEIVE_URL)[0]["data"]["cid"] == "10"


def test_save_rejects_bare_target_pid_zero_when_root_cid_never_synced(client, hidrive):
    # Follow-up (post-T17): the CRITICAL fix in T17 fix wave 1 item 1 let a
    # bare target_pid=="0" through whenever 115_open_root_cid had never
    # been synced -- setting_get(..., "0")'s fallback made an absent
    # setting indistinguishable from a real, synced "0" root cid, exactly
    # the value a forged/stale target_pid could guess. No upstream call is
    # ever reached: rejection happens before save_115_link is called.
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    assert hidrive.setting_get("115_open_root_cid") is None

    response = client.post(
        "/api/115/save",
        json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_pid": "0"},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "TARGET_PID_INVALID"


def test_save_bare_target_pid_syncs_credentials_before_checking_root_cid(client, hidrive, http, tmp_path, monkeypatch):
    # Follow-up (post-T17): the bare-target_pid root-cid exception must
    # mirror resolve_115_target_path's own sync-before-read -- here nothing
    # ever calls hidrive.setting_set("115_open_root_cid", ...) directly;
    # only a fake OpenList source database does, via the very same
    # sync_openlist_credentials_from_source() call resolve_115_target_path
    # already uses.
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    source_db = tmp_path / "openlist-source.db"
    _write_openlist_source_db(source_db, root_folder_id="77")
    monkeypatch.setattr(hidrive, "OPENLIST_DB", source_db)
    assert hidrive.setting_get("115_open_root_cid") is None  # never synced yet
    _route_115_session(http)
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post(
        "/api/115/save",
        json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_pid": "77"},
    )

    assert response.status_code == 200
    assert http.calls_to(RECEIVE_URL)[0]["data"]["cid"] == "77"
    assert hidrive.setting_get("115_open_root_cid") == "77"  # the sync ran and persisted it


def test_transfer_deadline_aborts_target_resolution_before_receive_when_budget_exhausted(client, hidrive, http, monkeypatch):
    # N2: target-path resolution shares the SAME request-scoped deadline
    # (_115_REQUEST_DEADLINE_SECONDS) as save_115_link -- both routes call
    # _resolve_115_target_pid (the UI always sends target_path) BEFORE
    # save_115_link's own budget used to start, so a slow, multi-segment
    # OpenList listing could blow way past gunicorn's 30s worker timeout
    # before save_115_link even began. A slow first segment that alone
    # exhausts the whole budget must abort with a fixed 504
    # TARGET_RESOLVE_TIMEOUT before ever attempting the second segment --
    # and must never reach share/snap or share/receive.
    monkeypatch.setattr(hidrive, "_115_REQUEST_DEADLINE_SECONDS", 0.05)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.secret_set("115_open_access_token", "open-access-fixture")
    hidrive.setting_set("115_open_root_cid", "10")
    listings = {
        "10": [{"fn": "Movies", "fc": "0", "fid": "20"}, {"fn": "Movies", "fc": "1", "fid": "99"}],
        "20": [{"fn": "2024", "fc": "0", "fid": "30"}],
    }
    calls = {"n": 0}

    def slow_open_files(**kwargs):
        from conftest import FakeResponse

        calls["n"] += 1
        time.sleep(0.06)  # alone, already over the whole request budget
        return FakeResponse({"state": True, "data": listings[kwargs["params"]["cid"]]})

    http.route("GET", OPEN_FILES_URL, handler=slow_open_files)

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_path": "/115pan/Movies/2024"})

    assert response.status_code == 504
    assert response.get_json()["code"] == "TARGET_RESOLVE_TIMEOUT"
    assert calls["n"] == 1  # the second segment's listing must never be attempted
    assert http.calls_to(SNAP_URL) == []
    assert http.calls_to(RECEIVE_URL) == []


def test_transfer_deadline_returns_not_attempted_when_resolution_leaves_too_little_budget(client, hidrive, http, monkeypatch):
    # T19 wave 3 item 1: resolution, verify (_115_transfer_gate) and
    # share/snap now all share the SAME single request-scoped deadline
    # (_115_REQUEST_DEADLINE_SECONDS=24s) as share/receive. If resolution
    # alone eats ~20s of it, verify and share/snap must still be attempted
    # (each with a shrunk timeout, since neither is over its own 12s/3s
    # thresholds yet) -- but by the time they're done there is too little
    # budget left for share/receive, which must never be attempted at all:
    # a fixed 504 TRANSFER_NOT_ATTEMPTED is returned instead of risking
    # share/receive being the call cut off mid-flight.
    from conftest import FakeMonotonic, FakeResponse

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.secret_set("115_open_access_token", "open-access-fixture")
    hidrive.setting_set("115_open_root_cid", "10")

    def slow_open_files(**kwargs):
        clock.advance(20)  # resolution alone burns 20 of the 24s budget
        return FakeResponse({"state": True, "data": [{"fn": "Movies", "fc": "0", "fid": "20"}]})

    def verify_handler(**kwargs):
        clock.advance(1)
        return FakeResponse({"state": True, "data": {"uid": "42"}})

    def snap_handler(**kwargs):
        clock.advance(1)
        return FakeResponse({"state": True, "data": {"list": [{"fid": "f1"}]}})

    http.route("GET", OPEN_FILES_URL, handler=slow_open_files)
    http.route("GET", USER_URL, handler=verify_handler)
    http.route("GET", SNAP_URL, handler=snap_handler)
    # RECEIVE_URL deliberately left unrouted -- any call to it fails the test.

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_path": "/115pan/Movies"})

    assert response.status_code == 504
    assert response.get_json()["code"] == "TRANSFER_NOT_ATTEMPTED"
    assert http.calls_to(RECEIVE_URL) == []
    verify_call = http.calls_to(USER_URL)[0]
    snap_calls = http.calls_to(SNAP_URL)
    # T19 wave 4 item 3: every call's timeout is now a (connect, read)
    # tuple -- (min(_115_UPSTREAM_CONNECT_TIMEOUT, remaining),
    # min(_115_UPSTREAM_TIMEOUT, remaining)).
    assert verify_call["timeout"] == (3, 4)
    assert len(snap_calls) == 1  # no retry -- remaining budget never reaches 12s
    assert snap_calls[0]["timeout"] == (3, 3)


def test_transfer_deadline_worst_case_recorded_timeouts_never_exceed_budget(client, hidrive, http, monkeypatch):
    # T19 wave 3 item 1(c) / wave 4 item 3: every upstream call's own
    # timeout is a (connect, read) tuple -- (min(_115_UPSTREAM_CONNECT_
    # TIMEOUT, remaining), min(_115_UPSTREAM_TIMEOUT, remaining)) -- so a
    # single call's own worst-case wall time (connect+read) can never
    # exceed the budget remaining when it was issued by more than
    # _115_UPSTREAM_CONNECT_TIMEOUT. Even if every call in the worst-case
    # chain (target resolution, verify, share/snap's first attempt)
    # actually took its full allotted connect+read time, the whole request
    # still finishes comfortably under gunicorn's 30s worker timeout,
    # whether or not share/receive ends up with enough budget left to be
    # attempted at all.
    from conftest import FakeMonotonic, FakeResponse

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.secret_set("115_open_access_token", "open-access-fixture")
    hidrive.setting_set("115_open_root_cid", "10")

    def make_handler(payload):
        def handler(**kwargs):
            connect, read = kwargs["timeout"]
            clock.advance(connect + read)  # worst case: uses every second of both phases
            return FakeResponse(payload)

        return handler

    http.route("GET", OPEN_FILES_URL, handler=make_handler({"state": True, "data": [{"fn": "Movies", "fc": "0", "fid": "20"}]}))
    http.route("GET", USER_URL, handler=make_handler({"state": True, "data": {"uid": "42"}}))
    http.route("GET", SNAP_URL, handler=make_handler({"state": True, "data": {"list": [{"fid": "f1"}]}}))
    http.route("POST", RECEIVE_URL, handler=make_handler({"state": True}))

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_path": "/115pan/Movies"})

    # Re-derive, call by call, that each one's own worst-case wall time
    # never outran the budget remaining when it was issued by more than
    # _115_UPSTREAM_CONNECT_TIMEOUT -- and that the whole chain still
    # finishes well under gunicorn's 30s worker timeout.
    elapsed_before = 0.0
    for call in http.calls:
        connect, read = call["timeout"]
        remaining_at_call = hidrive._115_REQUEST_DEADLINE_SECONDS - elapsed_before
        assert connect <= hidrive._115_UPSTREAM_CONNECT_TIMEOUT
        assert read <= hidrive._115_UPSTREAM_TIMEOUT
        assert connect + read <= remaining_at_call + hidrive._115_UPSTREAM_CONNECT_TIMEOUT
        elapsed_before += connect + read
    assert elapsed_before < 30
    assert response.status_code == 504
    assert response.get_json()["code"] == "TRANSFER_NOT_ATTEMPTED"
    assert http.calls_to(RECEIVE_URL) == []


def test_budget_already_exhausted_before_verify_is_not_persisted_as_outage(client, hidrive, http, monkeypatch):
    # T19 wave 4 item 1: a NEVER-ISSUED get_user_aq -- the shared request
    # budget was already gone by the time _115_transfer_gate would call it
    # -- must be classified budget_exhausted (the app's own budget running
    # out, not a real 115 failure) and _115_transfer_gate must not persist
    # it as network_error/TIMEOUT (fail streak, fast-fail window, red
    # settings state).
    monkeypatch.setattr(hidrive, "_115_REQUEST_DEADLINE_SECONDS", -1)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    assert response.status_code == 504
    assert response.get_json()["code"] == "TRANSFER_NOT_ATTEMPTED"
    assert http.calls == []  # get_user_aq is never even issued
    assert hidrive.setting_get("115_cookie_state") is None
    assert hidrive.setting_get("115_cookie_fail_streak") is None


def test_budget_shortened_verify_timeout_is_not_persisted_as_outage(client, hidrive, http, monkeypatch):
    # T19 wave 4 item 1: resolution burning most of the shared budget
    # shrinks get_user_aq's own timeout below the full 8s -- if 115 then
    # times out against that SHRUNK timeout, it must be classified
    # budget_exhausted (the app's own budget running out), not persisted
    # as a real network_error/TIMEOUT outage. _115_transfer_gate returns a
    # fixed 504 TRANSFER_NOT_ATTEMPTED instead, with no state change, so
    # the next transfer attempt still performs a real verify rather than
    # fast-failing from a phantom cached outage.
    from conftest import FakeMonotonic, FakeResponse

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.secret_set("115_open_access_token", "open-access-fixture")
    hidrive.setting_set("115_open_root_cid", "10")

    def slow_open_files(**kwargs):
        clock.advance(22)  # resolution alone burns 22 of the 24s budget
        return FakeResponse({"state": True, "data": [{"fn": "Movies", "fc": "0", "fid": "20"}]})

    http.route("GET", OPEN_FILES_URL, handler=slow_open_files)
    http.route("GET", USER_URL, error=requests_lib.Timeout("shrunk timeout exceeded"))

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_path": "/115pan/Movies"})

    assert response.status_code == 504
    assert response.get_json()["code"] == "TRANSFER_NOT_ATTEMPTED"
    assert response.get_json()["message"] == "转存耗时过长，请稍后重试"
    verify_call = http.calls_to(USER_URL)[0]
    assert verify_call["timeout"][1] < hidrive._115_UPSTREAM_TIMEOUT  # shrunk read timeout, not the full 8s
    assert http.calls_to(SNAP_URL) == []
    assert http.calls_to(RECEIVE_URL) == []
    assert hidrive.setting_get("115_cookie_state") is None
    assert hidrive.setting_get("115_cookie_fail_streak") is None

    # A fresh transfer (its own fresh budget, target_pid so resolution is
    # skipped) performs a REAL verify -- proving the budget_exhausted
    # outcome above left no cached fast-fail state behind.
    clock2 = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock2)
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})
    # T17 fix wave 1 item 1: a bare target_pid now needs server-side proof.
    hidrive.setting_set("115_target_pid", "999")

    response2 = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_pid": "999"})

    assert response2.status_code == 200
    assert len(http.calls_to(USER_URL)) == 2


def test_genuine_full_timeout_still_persists_network_error(client, hidrive, http):
    # Control case for the budget_exhausted regression above: when the
    # FULL _115_UPSTREAM_TIMEOUT is actually used (the shared budget never
    # shrinks it) and 115 itself times out, that IS a real outage and must
    # still be persisted as network_error/TIMEOUT (fail streak, fast-fail
    # window, red settings state) exactly as before.
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    http.route("GET", USER_URL, error=requests_lib.Timeout("real upstream timeout"))

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    assert response.status_code == 503
    assert response.get_json()["code"] == "115_TEMPORARILY_UNAVAILABLE"
    assert hidrive.setting_get("115_cookie_state") == "network_error"
    assert hidrive.setting_get("115_cookie_error_code") == "TIMEOUT"
    assert hidrive.setting_get("115_cookie_fail_streak") == "1"


def test_request_budget_starts_at_true_request_start_not_view_entry(client, hidrive, http, monkeypatch):
    # T19 wave 4 item 2: g.request_started is recorded as the FIRST
    # statement of before_request -- before require_access's Access/JWKS
    # work -- and the route derives its deadline from it
    # (g.request_started + _115_REQUEST_DEADLINE_SECONDS), not a fresh
    # time.monotonic() call inside the view. Simulate 20s of Access/JWKS
    # work happening AFTER g.request_started is captured but BEFORE the
    # view runs, by advancing a fake clock from inside require_access
    # (called by before_request right after g.request_started is set) --
    # the shrunk ~4s budget must reach the route's own upstream call.
    from conftest import FakeMonotonic

    clock = FakeMonotonic()
    monkeypatch.setattr(hidrive.time, "monotonic", clock)
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)

    original_require_access = hidrive.require_access

    def slow_require_access():
        original_require_access()
        clock.advance(20)  # simulate slow Access/JWKS validation

    monkeypatch.setattr(hidrive, "require_access", slow_require_access)
    http.route("GET", USER_URL, {"state": True, "data": {"uid": "42"}})
    http.route("GET", SNAP_URL, {"state": True, "data": {"list": [{"fid": "f1"}]}})
    http.route("POST", RECEIVE_URL, {"state": True})

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8"})

    verify_call = http.calls_to(USER_URL)[0]
    assert verify_call["timeout"] == (3, 4)  # ~4s of the 24s budget remains
    assert response.status_code == 200


def test_save_rejects_target_outside_115pan(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_path": "/115strm/x"})

    assert response.status_code == 503
    assert response.get_json()["code"] == "115_TARGET_RESOLVE_FAILED"
    assert http.calls == []


def test_save_reports_missing_open_platform_token(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_path": "/115pan/Movies"})

    assert response.status_code == 503
    assert response.get_json()["message"] == "尚未同步 OpenList 中的 115 开放平台令牌"


def test_save_reports_unknown_115_folder(client, hidrive, http):
    hidrive.secret_set("115_cookie", COOKIE_FIXTURE)
    hidrive.secret_set("115_open_access_token", "open-access-fixture")
    http.route("GET", OPEN_FILES_URL, {"state": True, "data": [{"fn": "Other", "fc": "0", "fid": "1"}]})

    response = client.post("/api/115/save", json={"share_url": "https://115.com/s/swabc123?password=k9x8", "target_path": "/115pan/Movies"})

    assert response.status_code == 503
    assert response.get_json()["message"] == "115 目录不存在或已变化：Movies"


# --- folder picker backed by OpenList ---------------------------------------


def test_folders_lists_only_directories_under_115pan(client, http):
    http.route(
        "POST",
        OPENLIST_LIST_URL,
        {"code": 200, "data": {"content": [{"name": "Movies", "is_dir": True}, {"name": "film.mkv", "is_dir": False}, {"name": "..", "is_dir": True}, {"name": "TV", "is_dir": 1}]}},
    )

    response = client.get("/api/115/folders")

    assert response.status_code == 200
    body = response.get_json()
    assert body["root"] == "/115pan"
    assert body["path"] == "/115pan"
    assert body["parent"] == "/115pan"
    assert body["items"] == [{"name": "Movies", "path": "/115pan/Movies", "directory": True}, {"name": "TV", "path": "/115pan/TV", "directory": True}]
    assert http.calls_to(OPENLIST_LIST_URL)[0]["json"]["path"] == "/115pan"


def test_folders_reports_parent_for_nested_path(client, http):
    http.route("POST", OPENLIST_LIST_URL, {"code": 200, "data": {"content": []}})

    response = client.get("/api/115/folders?path=/115pan/Movies/2024")

    assert response.get_json()["parent"] == "/115pan/Movies"


def test_folders_rejects_paths_outside_115pan(client, http):
    response = client.get("/api/115/folders?path=/115pan/../115strm")
    assert response.status_code == 400
    assert response.get_json()["code"] == "OPENLIST_PATH_INVALID"
    assert http.calls == []


def test_folders_surfaces_openlist_errors(client, http):
    http.route("POST", OPENLIST_LIST_URL, {"code": 401, "message": "token expired"})

    response = client.get("/api/115/folders")

    assert response.status_code == 502
    assert response.get_json() == {"success": False, "code": "OPENLIST_LIST_FAILED", "message": "token expired"}
