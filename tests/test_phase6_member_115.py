"""Phase 6 (R05): folders, cloud download and tasks, each under its own user.

Two members, two synthetic step-B tokens, no shared state. Every call goes
through the real API routes and the FakeHTTP fixture -- nothing here touches
115, and every token, cid and info_hash is invented for the test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service as auth  # noqa: E402
import library_store as ls  # noqa: E402
import user_115  # noqa: E402
from tests.conftest import FakeResponse  # noqa: E402
from tests.test_cloud_download_api import (  # noqa: E402
    ADD_URL, CLEAR_URL, DEL_URL, QUOTA_BODY, QUOTA_URL, TASKS_URL, _add_ed2k_links,
)

FILES_URL = "https://proapi.115.com/open/ufile/files"
FOLDERS_API = "/api/115/folders"
SUBMIT_API = "/api/library/cloud-download"
TASKS_API = "/api/library/cloud-download/tasks"
QUOTA_API = "/api/library/cloud-download/quota"
STATUS_API = "/api/library/cloud-download/status"
MEMBER_PASSWORD = "Correct1Horse"

# Two entirely invented 115 accounts: A owns cid a1, B owns cid b1, and
# neither cid is listable with the other's token.
OWN_FOLDERS = {
    "token-a": {"0": [{"fid": "a1", "fn": "甲的收藏", "fc": "0"}],
                "a1": [{"fid": "a2", "fn": "甲的剧集", "fc": "0"}]},
    "token-b": {"0": [{"fid": "b1", "fn": "乙的收藏", "fc": "0"}], "b1": []},
}


def _files_handler(calls):
    def handler(**kwargs):
        token = str(kwargs["headers"]["Authorization"]).split(" ", 1)[-1]
        cid = str(kwargs["params"]["cid"])
        calls.append((token, cid))
        listing = OWN_FOLDERS.get(token, {})
        if cid not in listing:
            return FakeResponse({"state": False, "code": 20004, "message": "目录不存在"}, 200)
        return FakeResponse({"state": True, "code": 0, "message": "", "data": listing[cid]}, 200)
    return handler


def _sign_up(client, hidrive, email):
    assert client.post("/api/auth/register", json={
        "email": email, "password": MEMBER_PASSWORD, "confirm_password": MEMBER_PASSWORD}).status_code in {200, 201, 202}
    with hidrive.connect_db() as db:
        admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        row = db.execute("SELECT id FROM auth_user WHERE email_norm=?", (email,)).fetchone()
        auth.approve_user(db, int(row["id"]), approver_id=admin_id, now=2)
    assert client.post("/api/auth/login", json={"email": email, "password": MEMBER_PASSWORD}).status_code == 200
    return int(row["id"])


def _authorise(hidrive, user_id, token):
    with hidrive.connect_db() as db:
        user_115.secret_set(db, user_id, user_115.OPEN_ACCESS_SECRET, token,
                            fernet=hidrive.load_fernet(), now=1)


@pytest.fixture
def store(hidrive, workspace):
    library = ls.LibraryStore(hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())
    library.create_schema()
    library.meta_set("encrypted", "1")
    hidrive.setting_set("cloud_download_enabled", "1")
    # The administrator's own settings, which no member may inherit.
    hidrive.secret_set("115_open_access_token", "the-administrators-token")
    hidrive.setting_set("115_target_pid", "the-administrators-folder")
    hidrive._CLOUD_CACHE.clear()
    hidrive._CLOUD_DEDUPE_SEEN.clear()
    return library


@pytest.fixture
def members(client, hidrive, store, http):
    """Two signed-in members, each holding their own step-B token."""
    first = _sign_up(client, hidrive, "a@example.test")
    _authorise(hidrive, first, "token-a")
    second_client = hidrive.app.test_client()
    second = _sign_up(second_client, hidrive, "b@example.test")
    _authorise(hidrive, second, "token-b")
    http.route("GET", QUOTA_URL, QUOTA_BODY)
    return {"a": (client, first), "b": (second_client, second)}


class TestFolders:
    def test_each_member_browses_their_own_115(self, members, http):
        calls: list[tuple[str, str]] = []
        http.route("GET", FILES_URL, handler=_files_handler(calls))
        (client_a, _), (client_b, _) = members["a"], members["b"]

        first = client_a.get(FOLDERS_API)
        assert first.status_code == 200, first.get_json()
        assert first.get_json()["mode"] == "115"
        assert first.get_json()["items"] == [{"cid": "a1", "name": "甲的收藏"}]

        second = client_b.get(FOLDERS_API)
        assert second.get_json()["items"] == [{"cid": "b1", "name": "乙的收藏"}]

        assert calls == [("token-a", "0"), ("token-b", "0")]
        for response in (first, second):
            assert "the-administrators-token" not in response.get_data(as_text=True)
        assert "乙的收藏" not in first.get_data(as_text=True)
        assert "甲的收藏" not in second.get_data(as_text=True)

    def test_a_member_walks_into_their_own_subfolder(self, members, http):
        calls: list[tuple[str, str]] = []
        http.route("GET", FILES_URL, handler=_files_handler(calls))
        client_a, _ = members["a"]
        body = client_a.get(FOLDERS_API + "?cid=a1").get_json()
        assert body["cid"] == "a1" and body["items"] == [{"cid": "a2", "name": "甲的剧集"}]

    def test_one_members_cid_is_not_listable_by_the_other(self, members, http):
        http.route("GET", FILES_URL, handler=_files_handler([]))
        client_a, _ = members["a"]
        response = client_a.get(FOLDERS_API + "?cid=b1")
        assert response.status_code == 502
        assert response.get_json()["success"] is False

    def test_the_member_never_reaches_openlist(self, members, http, monkeypatch, hidrive):
        def forbidden(*args, **kwargs):
            raise AssertionError("a member must never browse the administrator's OpenList")
        monkeypatch.setattr(hidrive, "openlist_folder_items", forbidden)
        http.route("GET", FILES_URL, handler=_files_handler([]))
        assert members["a"][0].get(FOLDERS_API + "?path=/115pan").status_code == 200


class TestWithoutStepB:
    """A member who has not authorised is told which button to press, and no
    request leaves the server on anybody else's credential."""

    @pytest.fixture
    def stranger(self, client, hidrive, store, http):
        http.route("GET", QUOTA_URL, QUOTA_BODY)
        return _sign_up(client, hidrive, "c@example.test"), client

    @pytest.mark.parametrize("method, url, body", [
        ("get", FOLDERS_API, None),
        ("get", TASKS_API, None),
        ("get", QUOTA_API, None),
        ("post", SUBMIT_API, {"resource_link_ids": ["pub-x"]}),
        ("post", TASKS_API + "/hash-1-0/delete", {}),
        ("post", TASKS_API + "/clear", {"scope": "failed"}),
    ])
    def test_refused_with_guidance_and_no_upstream_call(self, stranger, http, method, url, body):
        _user_id, client = stranger
        response = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
        assert response.status_code == 409, (url, response.get_json())
        payload = response.get_json()
        assert payload["code"] == "OPEN115_NOT_AUTHORIZED"
        assert "授权" in payload["message"]
        assert "the-administrators-token" not in response.get_data(as_text=True)
        assert [call for call in http.calls if call["url"] != QUOTA_URL] == []

    def test_status_reports_this_users_own_authorisation(self, stranger, http):
        _user_id, client = stranger
        assert client.get(STATUS_API).get_json()["token_available"] is False


class TestTasks:
    def _submit(self, client, hidrive, store, http, *, title, pid):
        _media, _group, ids = _add_ed2k_links(store, 1, title=title)
        response = client.post(SUBMIT_API, json={"resource_link_ids": ids, "target_pid": pid})
        return response

    def test_each_member_submits_lists_deletes_and_clears_their_own(self, members, http, hidrive, store):
        http.route("GET", FILES_URL, handler=_files_handler([]))
        submitted: list[str] = []

        def add_handler(**kwargs):
            token = str(kwargs["headers"]["Authorization"]).split(" ", 1)[-1]
            submitted.append(token)
            urls = [u for u in kwargs["data"]["urls"].split("\n") if u]
            return FakeResponse({"state": True, "code": 0, "message": "", "data": [
                {"state": True, "code": 0, "message": "", "info_hash": f"hash-{token}", "url": url}
                for url in urls]}, 200)
        http.route("POST", ADD_URL, handler=add_handler)

        first = self._submit(members["a"][0], hidrive, store, http, title="甲片", pid="a1")
        assert first.status_code == 200, first.get_json()
        assert first.get_json()["ok"] == 1
        second = self._submit(members["b"][0], hidrive, store, http, title="乙片", pid="b1")
        assert second.status_code == 200, second.get_json()
        assert submitted == ["token-a", "token-b"]

        # Each row belongs to exactly one user, and the target cid stored is
        # the one that user proved.
        with hidrive.connect_db() as db:
            rows = {row["info_hash"]: row["user_id"] for row in
                    db.execute("SELECT info_hash, user_id FROM user_cloud_download_task")}
        assert rows == {"hash-token-a": members["a"][1], "hash-token-b": members["b"][1]}

        # The list: 115 returns both tasks to either caller (it is one
        # account per token in reality, two here), but only the caller's own
        # row may annotate it.
        def task_list(**kwargs):
            return FakeResponse({"state": True, "code": 0, "message": "", "data": {
                "page": 1, "page_count": 1, "count": 2, "tasks": [
                    {"info_hash": "hash-token-a", "name": "A.mkv", "status": 1},
                    {"info_hash": "hash-token-b", "name": "B.mkv", "status": 1}]}}, 200)
        http.route("GET", TASKS_URL, handler=task_list)
        hidrive._CLOUD_CACHE.clear()
        listed = members["a"][0].get(TASKS_API).get_json()["tasks"]
        origins = {task["info_hash"]: task["origin"] for task in listed}
        assert origins["hash-token-a"] is not None and origins["hash-token-a"]["media_title"] == "甲片"
        assert origins["hash-token-b"] is None

        # Delete and clear act as the caller, with the caller's own token.
        deletes: list[str] = []
        http.route("POST", DEL_URL, handler=lambda **kw: (
            deletes.append(str(kw["headers"]["Authorization"]).split(" ", 1)[-1]),
            FakeResponse({"state": True, "code": 0, "message": ""}, 200))[1])
        assert members["b"][0].post(TASKS_API + "/hash-token-b/delete", json={}).status_code == 200
        assert deletes == ["token-b"]
        with hidrive.connect_db() as db:
            states = {row["info_hash"]: row["state"] for row in
                      db.execute("SELECT info_hash, state FROM user_cloud_download_task")}
        assert states["hash-token-b"] == "-2" and states["hash-token-a"] != "-2"

        cleared: list[str] = []
        http.route("POST", CLEAR_URL, handler=lambda **kw: (
            cleared.append(str(kw["headers"]["Authorization"]).split(" ", 1)[-1]),
            FakeResponse({"state": True, "code": 0, "message": ""}, 200))[1])
        assert members["a"][0].post(TASKS_API + "/clear", json={"scope": "failed"}).status_code == 200
        assert cleared == ["token-a"]

    def test_a_member_may_not_submit_into_another_members_folder(self, members, http, hidrive, store):
        http.route("GET", FILES_URL, handler=_files_handler([]))
        http.route("POST", ADD_URL, handler=lambda **kw: (_ for _ in ()).throw(
            AssertionError("nothing may be submitted against an unproved cid")))
        response = self._submit(members["a"][0], hidrive, store, http, title="越界", pid="b1")
        assert response.status_code == 400
        assert response.get_json()["code"] == "TARGET_PID_INVALID"

    def test_a_member_may_not_submit_into_the_administrators_folder(self, members, http, hidrive, store):
        http.route("GET", FILES_URL, handler=_files_handler([]))
        http.route("POST", ADD_URL, handler=lambda **kw: (_ for _ in ()).throw(
            AssertionError("the administrator's stored default is not a member's default")))
        response = self._submit(members["a"][0], hidrive, store, http,
                                title="管理员目录", pid="the-administrators-folder")
        assert response.status_code == 400
        assert response.get_json()["code"] == "TARGET_PID_INVALID"

    def test_status_and_quota_run_on_the_callers_own_token(self, members, http, hidrive):
        seen: list[str] = []
        http.route("GET", QUOTA_URL, handler=lambda **kw: (
            seen.append(str(kw["headers"]["Authorization"]).split(" ", 1)[-1]),
            FakeResponse(QUOTA_BODY, 200))[1])
        hidrive._CLOUD_CACHE.clear()
        assert members["b"][0].get(QUOTA_API).status_code == 200
        assert seen == ["token-b"]
        assert members["b"][0].get(STATUS_API).get_json()["token_available"] is True


# ---------------------------------------------------------------------------
# F04: the administrator's legacy table belongs to the administrator
# ---------------------------------------------------------------------------


LEGACY_HASH = "shared-hash-fixture"


def _same_hash_handler(seen):
    """115 answers with one info_hash for everybody -- the whole point of
    F04 is that three users submitting the *same* hash must not overwrite
    one another's record of it."""
    def handler(**kwargs):
        token = str(kwargs["headers"]["Authorization"]).split(" ", 1)[-1]
        seen.append(token)
        urls = [u for u in kwargs["data"]["urls"].split("\n") if u]
        return FakeResponse({"state": True, "code": 0, "message": "", "data": [
            {"state": True, "code": 0, "message": "", "info_hash": LEGACY_HASH, "url": url}
            for url in urls]}, 200)
    return handler


def _legacy_row(hidrive):
    with hidrive.connect_db() as db:
        row = db.execute("SELECT * FROM cloud_download_task WHERE info_hash=?", (LEGACY_HASH,)).fetchone()
    return dict(row) if row is not None else None


def _own_rows(hidrive):
    with hidrive.connect_db() as db:
        return {row["user_id"]: dict(row) for row in db.execute(
            "SELECT * FROM user_cloud_download_task WHERE info_hash=?", (LEGACY_HASH,))}


class TestTheLegacyTableIsTheAdministrators:
    @pytest.fixture
    def three_of_them(self, client, hidrive, store, http):
        """The administrator (an anonymous request in this auth mode) plus two
        members, each with their own step-B token."""
        admin_client = hidrive.app.test_client()
        first = _sign_up(client, hidrive, "a@example.test")
        _authorise(hidrive, first, "token-a")
        second_client = hidrive.app.test_client()
        second = _sign_up(second_client, hidrive, "b@example.test")
        _authorise(hidrive, second, "token-b")
        http.route("GET", QUOTA_URL, QUOTA_BODY)
        http.route("GET", FILES_URL, handler=_files_handler([]))
        hidrive.setting_set("115_target_pid", "0")
        with hidrive.connect_db() as db:
            admin_id = int(db.execute("SELECT id FROM auth_user WHERE role='admin'").fetchone()["id"])
        return {"admin": (admin_client, admin_id), "a": (client, first), "b": (second_client, second)}

    def test_only_the_administrator_writes_and_rewrites_the_legacy_row(self, three_of_them, http, hidrive, store):
        seen: list[str] = []
        http.route("POST", ADD_URL, handler=_same_hash_handler(seen))
        (admin, admin_id), (client_a, id_a), (client_b, id_b) = (
            three_of_them["admin"], three_of_them["a"], three_of_them["b"])

        # The administrator submits first: the legacy row is theirs.
        _media, _group, admin_ids = _add_ed2k_links(store, 1, title="管理员片")
        assert admin.post(SUBMIT_API, json={"resource_link_ids": admin_ids, "target_pid": "0"}).status_code == 200
        legacy_before = _legacy_row(hidrive)
        assert legacy_before is not None
        assert legacy_before["media_title"] == "管理员片"
        assert legacy_before["target_path"] is None and legacy_before["last_status"] is None

        # Both members submit the same info_hash, each into their own folder.
        for label, (member_client, pid, title) in (
            ("a", (client_a, "a1", "甲的片")), ("b", (client_b, "b1", "乙的片")),
        ):
            _m, _g, ids = _add_ed2k_links(store, 1, title=title)
            response = member_client.post(SUBMIT_API, json={
                "resource_link_ids": ids, "target_pid": pid, "target_path": ""})
            assert response.status_code == 200, (label, response.get_json())

        assert seen == ["the-administrators-token", "token-a", "token-b"]
        assert _legacy_row(hidrive) == legacy_before, "a member's submit rewrote the administrator's row"

        rows = _own_rows(hidrive)
        assert set(rows) == {admin_id, id_a, id_b}, rows
        assert rows[id_a]["display_title"] == "甲的片"
        assert rows[id_b]["display_title"] == "乙的片"

    def test_a_members_delete_leaves_the_legacy_row_and_the_others_alone(self, three_of_them, http, hidrive, store):
        seen: list[str] = []
        http.route("POST", ADD_URL, handler=_same_hash_handler(seen))
        (admin, admin_id), (client_a, id_a), (client_b, id_b) = (
            three_of_them["admin"], three_of_them["a"], three_of_them["b"])
        _m, _g, admin_ids = _add_ed2k_links(store, 1, title="管理员片")
        admin.post(SUBMIT_API, json={"resource_link_ids": admin_ids, "target_pid": "0"})
        for member_client, pid, title in ((client_a, "a1", "甲的片"), (client_b, "b1", "乙的片")):
            _m, _g, ids = _add_ed2k_links(store, 1, title=title)
            member_client.post(SUBMIT_API, json={"resource_link_ids": ids, "target_pid": pid})
        legacy_before = _legacy_row(hidrive)

        deletes: list[str] = []
        http.route("POST", DEL_URL, handler=lambda **kw: (
            deletes.append(str(kw["headers"]["Authorization"]).split(" ", 1)[-1]),
            FakeResponse({"state": True, "code": 0, "message": ""}, 200))[1])
        assert client_a.post(f"{TASKS_API}/{LEGACY_HASH}/delete", json={}).status_code == 200
        assert deletes == ["token-a"]
        assert _legacy_row(hidrive) == legacy_before, "a member's delete marked the administrator's row deleted"
        rows = _own_rows(hidrive)
        assert rows[id_a]["state"] == "-2"
        assert rows[id_b]["state"] is None and rows[admin_id]["state"] is None

        # And the administrator's own delete still keeps the legacy behaviour.
        assert admin.post(f"{TASKS_API}/{LEGACY_HASH}/delete", json={}).status_code == 200
        assert deletes[-1] == "the-administrators-token"
        assert _legacy_row(hidrive)["last_status"] == -2

    def test_clearing_touches_no_local_row(self, three_of_them, http, hidrive, store):
        seen: list[str] = []
        http.route("POST", ADD_URL, handler=_same_hash_handler(seen))
        (admin, _admin_id), (client_a, _id_a), _b = (
            three_of_them["admin"], three_of_them["a"], three_of_them["b"])
        _m, _g, admin_ids = _add_ed2k_links(store, 1, title="管理员片")
        admin.post(SUBMIT_API, json={"resource_link_ids": admin_ids, "target_pid": "0"})
        _m, _g, ids = _add_ed2k_links(store, 1, title="甲的片")
        client_a.post(SUBMIT_API, json={"resource_link_ids": ids, "target_pid": "a1"})
        legacy_before, rows_before = _legacy_row(hidrive), _own_rows(hidrive)

        cleared: list[str] = []
        http.route("POST", CLEAR_URL, handler=lambda **kw: (
            cleared.append(str(kw["headers"]["Authorization"]).split(" ", 1)[-1]),
            FakeResponse({"state": True, "code": 0, "message": ""}, 200))[1])
        assert client_a.post(f"{TASKS_API}/clear", json={"scope": "failed"}).status_code == 200
        assert cleared == ["token-a"]
        assert _legacy_row(hidrive) == legacy_before
        assert _own_rows(hidrive) == rows_before

    def test_one_users_delete_does_not_evict_anothers_cache(self, three_of_them, http, hidrive):
        """F04: the cache is keyed per user so that a delete costs only the
        caller a fresh round of 115 requests."""
        (_admin, _admin_id), (client_a, id_a), (_client_b, id_b) = (
            three_of_them["admin"], three_of_them["a"], three_of_them["b"])
        other = hidrive._cloud_cache_key(auth.CurrentUser(id=id_b, email="b", role="member", status="active"), "quota")
        hidrive._CLOUD_CACHE[other] = (9e9, {"surplus": 1})
        mine = hidrive._cloud_cache_key(auth.CurrentUser(id=id_a, email="a", role="member", status="active"), "quota")
        hidrive._CLOUD_CACHE[mine] = (9e9, {"surplus": 2})
        http.route("POST", DEL_URL, {"state": True, "code": 0, "message": ""})
        assert client_a.post(f"{TASKS_API}/{LEGACY_HASH}/delete", json={}).status_code == 200
        assert other in hidrive._CLOUD_CACHE, "another user's cache was flushed"
        assert mine not in hidrive._CLOUD_CACHE, "the caller's own cache must be dropped"
