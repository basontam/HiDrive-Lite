"""115 云下载 (cloud download) API tests -- spec docs/superpowers/specs/2026-09-09-115-cloud-download-design.md.

Every upstream call goes through the FakeHTTP ``http`` fixture (zero real
network). URLs/hashes below are fixtures, never real shares."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_store as ls  # noqa: E402
import user_115  # noqa: E402

STATUS_URL = "/api/library/cloud-download/status"
MEMBER_PASSWORD = "Correct1Horse"


@pytest.fixture
def cloud_store(hidrive, workspace):
    store = ls.LibraryStore(hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())
    store.create_schema()
    store.meta_set("encrypted", "1")
    return store


def _add_ed2k_links(store, count=1, *, provider="ed2k", deleted=False, title="虚构片"):
    """One media + one group with ``count`` ed2k links; returns public ids."""
    fernet = store.fernet
    media_id = store.upsert_media(
        ls.MediaRecord(media_identity=f"tt-fake-{title}-{count}", media_type="movie", title_zh=title, search_key=title)
    )
    group_id = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-{title}-{count}", display_title="4K"))
    public_ids = []
    for i in range(count):
        url = f"ed2k://|file|Fixture.{title}.S01E{i + 1:02d}.mkv|123456|{'AB' * 16}|/" if provider == "ed2k" else f"https://115.com/s/swfake{title}{i}"
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        public_id = f"pub-{title}-{count}-{i}"
        store.upsert_link(ls.LinkRecord(
            public_id=public_id, group_id=group_id, provider=provider, canonical_url_hash=url_hash,
            url_label=f"ED2K · S01E{i + 1:02d}" if provider == "ed2k" else "115 分享",
            url_ciphertext=fernet.encrypt(url.encode("utf-8")), has_access_code=0,
            deleted_at_source=1 if deleted else None,
        ))
        public_ids.append(public_id)
    store.recount()
    return media_id, group_id, public_ids


class TestSettingsAndStatus:
    def test_status_defaults(self, client, cloud_store):
        body = client.get(STATUS_URL).get_json()
        assert body["success"] is True
        assert body["enabled"] is False
        assert body["daily_cap"] == 0
        assert body["per_submit_cap"] == 30
        assert body["today_submitted"] == 0
        assert body["token_available"] is False

    def test_settings_roundtrip(self, client, cloud_store, hidrive):
        hidrive.secret_set("115_open_access_token", "open-access-fixture")
        response = client.post("/api/settings", json={
            "cloud_download_enabled": True, "cloud_download_daily_cap": 500, "cloud_download_per_submit_cap": 12,
        })
        assert response.status_code == 200
        assert hidrive.setting_get("cloud_download_enabled") == "1"
        assert hidrive.setting_get("cloud_download_daily_cap") == "500"
        assert hidrive.setting_get("cloud_download_per_submit_cap") == "12"
        body = client.get(STATUS_URL).get_json()
        assert body["enabled"] is True and body["daily_cap"] == 500 and body["per_submit_cap"] == 12
        assert body["token_available"] is True

    @pytest.mark.parametrize("payload", [
        {"cloud_download_enabled": "maybe"},
        {"cloud_download_daily_cap": -1},
        {"cloud_download_daily_cap": "12"},
        {"cloud_download_per_submit_cap": 0},
        {"cloud_download_per_submit_cap": 51},
    ])
    def test_settings_validation_400(self, client, cloud_store, payload):
        assert client.post("/api/settings", json=payload).status_code == 400

    def test_table_exists(self, hidrive, workspace):
        with hidrive.connect_db() as db:
            cols = [r[1] for r in db.execute("PRAGMA table_info(cloud_download_task)")]
        assert {"info_hash", "link_public_id", "media_id", "group_id", "wp_path_id", "submitted_at", "last_status"} <= set(cols)


QUOTA_URL = "https://proapi.115.com/open/offline/get_quota_info"
TASKS_URL = "https://proapi.115.com/open/offline/get_task_list"
ADD_URL = "https://proapi.115.com/open/offline/add_task_urls"
DEL_URL = "https://proapi.115.com/open/offline/del_task"
CLEAR_URL = "https://proapi.115.com/open/offline/clear_task"

QUOTA_BODY = {"state": True, "code": 0, "message": "", "data": {"count": 3000, "used": 12, "surplus": 2988, "package": [
    {"name": "长期VIP", "count": 3000, "used": 12, "surplus": 2988, "expire_info": []}]}}


@pytest.fixture
def cloud_token(hidrive, workspace):
    hidrive.secret_set("115_open_access_token", "open-access-fixture")
    hidrive._CLOUD_CACHE.clear()
    return "open-access-fixture"


class TestOfflineClient:
    def test_offline_request_resyncs_once_on_auth_error(self, hidrive, http, cloud_token, monkeypatch):
        seen = []
        def handler(**kwargs):
            token = kwargs["headers"]["Authorization"]
            seen.append(token)
            if token == "Bearer open-access-fixture":
                return hidrive_fake_response({"state": False, "errno": 40140125, "message": "token expired"}, 401)
            return hidrive_fake_response(QUOTA_BODY, 200)
        http.route("GET", QUOTA_URL, handler=handler)
        def fake_resync():
            hidrive.secret_set("115_open_access_token", "open-access-renewed")
            return True
        monkeypatch.setattr(hidrive, "sync_openlist_credentials_from_source", fake_resync)
        data, status, error = hidrive._open115_offline("GET", "/open/offline/get_quota_info")
        assert status == 200 and error == "" and data["data"]["surplus"] == 2988
        assert seen == ["Bearer open-access-fixture", "Bearer open-access-renewed"]

    def test_offline_request_state_false_is_an_error_with_message(self, hidrive, http, cloud_token):
        http.route("POST", DEL_URL, {"state": False, "code": 10008, "message": "任务不存在"})
        data, status, error = hidrive._open115_offline("POST", "/open/offline/del_task", data={"info_hash": "x"})
        assert status == 502 and "任务不存在" in error

    def test_offline_request_without_token_is_503(self, hidrive, http, workspace):
        hidrive._CLOUD_CACHE.clear()
        data, status, error = hidrive._open115_offline("GET", "/open/offline/get_quota_info")
        assert status == 503 and data is None and http.calls == []

    def test_quota_cached_60s(self, hidrive, http, cloud_token):
        http.route("GET", QUOTA_URL, QUOTA_BODY)
        first, err = hidrive._cloud_quota()
        again, _ = hidrive._cloud_quota()
        assert err == "" and first["surplus"] == 2988 and again is first
        assert len(http.calls_to(QUOTA_URL)) == 1
        hidrive._cloud_quota(force=True)
        assert len(http.calls_to(QUOTA_URL)) == 2

    def test_task_list_cached_10s_per_page(self, hidrive, http, cloud_token):
        http.route("GET", TASKS_URL, {"state": True, "data": {"page": 1, "page_count": 1, "count": 1, "tasks": [
            {"info_hash": "h1", "name": "Fixture.mkv", "size": 10, "percentDone": 50, "status": 1, "url": "ed2k://secret"}]}})
        hidrive._cloud_task_list(1)
        hidrive._cloud_task_list(1)
        assert len(http.calls_to(TASKS_URL)) == 1
        hidrive._cloud_task_list(2)
        assert len(http.calls_to(TASKS_URL)) == 2
        assert http.calls_to(TASKS_URL)[-1]["params"]["page"] == 2

    def test_sanitize_strips_links(self, hidrive):
        text = "添加失败 ed2k://|file|x.mkv|1|AA|/ 请重试 https://115.com/s/abc magnet:?xt=urn:btih:zz 结束"
        out = hidrive._cloud_sanitize_message(text)
        assert "添加失败" in out and "请重试" in out and "结束" in out
        for needle in ("ed2k://", "http", "magnet:", "btih"):
            assert needle not in out
        assert hidrive._cloud_sanitize_message(None) == ""


def hidrive_fake_response(payload, status):
    from tests.conftest import FakeResponse
    return FakeResponse(payload, status)


SUBMIT_URL = "/api/library/cloud-download"


def _add_handler(results_by_index=None, status=200, fail_call=None, fail_status=429):
    """FakeHTTP handler for add_task_urls: echoes one result per submitted
    url (all ok unless ``results_by_index`` overrides), records the form."""
    calls = []
    def handler(**kwargs):
        calls.append(kwargs)
        if fail_call is not None and len(calls) == fail_call:
            return hidrive_fake_response({"state": False, "code": 20001, "message": "请求过于频繁"}, fail_status)
        urls = [u for u in kwargs["data"]["urls"].split("\n") if u]
        data = []
        for i, url in enumerate(urls):
            override = (results_by_index or {}).get(i)
            if override:
                data.append({"state": False, "code": override[0], "message": override[1], "url": url})
            else:
                data.append({"state": True, "code": 0, "message": "", "info_hash": f"hash-{len(calls)}-{i}", "url": url})
        return hidrive_fake_response({"state": True, "code": 0, "message": "", "data": data}, status)
    handler.calls = calls
    return handler


@pytest.fixture
def cloud_ready(hidrive, workspace, cloud_store, cloud_token, http):
    hidrive.setting_set("cloud_download_enabled", "1")
    hidrive.setting_set("115_target_pid", "1")
    hidrive._CLOUD_DEDUPE_SEEN.clear()
    http.route("GET", QUOTA_URL, QUOTA_BODY)
    return cloud_store


class TestSubmit:
    def test_submit_single_ok(self, client, cloud_ready, http, hidrive, audit_rows):
        media_id, group_id, ids = _add_ed2k_links(cloud_ready, 1)
        handler = _add_handler(); http.route("POST", ADD_URL, handler=handler)
        response = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["success"] is True and body["submitted"] == 1 and body["ok"] == 1 and body["failed"] == 0
        assert body["quota_surplus"] == 2987  # 2988 before this submission, minus the one accepted task
        assert body["results"][0]["state"] == "ok" and body["results"][0]["info_hash"] == "hash-1-0"
        assert body["results"][0]["label"] == "ED2K · S01E01"
        form = handler.calls[0]["data"]
        assert form["wp_path_id"] == "1" and form["urls"].startswith("ed2k://|file|Fixture.虚构片.S01E01.mkv|")
        assert handler.calls[0]["headers"]["Authorization"] == "Bearer open-access-fixture"
        with hidrive.connect_db() as db:
            rows = db.execute("SELECT * FROM cloud_download_task").fetchall()
        assert len(rows) == 1 and rows[0]["info_hash"] == "hash-1-0" and rows[0]["media_id"] == media_id
        assert rows[0]["link_public_id"] == ids[0] and rows[0]["wp_path_id"] == "1" and rows[0]["media_title"] == "虚构片"
        audit = audit_rows("cloud_download.submit")
        assert audit and audit[-1]["status"] == "success" and "ok=1" in audit[-1]["detail"]
        assert "ed2k://" not in audit[-1]["detail"]

    def test_submit_group_chunks_serially_with_gap(self, client, cloud_ready, http, hidrive, monkeypatch):
        _, _, ids = _add_ed2k_links(cloud_ready, 12)
        handler = _add_handler(); http.route("POST", ADD_URL, handler=handler)
        events = []
        monkeypatch.setattr(hidrive, "_cloud_sleep", lambda seconds: events.append(("sleep", seconds)))
        original_post = http.post
        def recording_post(url, **kwargs):
            events.append(("post", url))
            return original_post(url, **kwargs)
        monkeypatch.setattr(hidrive.requests, "post", recording_post)
        body = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"}).get_json()
        assert body["ok"] == 12 and body["failed"] == 0
        assert [len([u for u in c["data"]["urls"].split("\n") if u]) for c in handler.calls] == [10, 2]
        assert events == [("post", ADD_URL), ("sleep", 1.5), ("post", ADD_URL)]

    def test_submit_partial_failure_keeps_going_and_sanitises(self, client, cloud_ready, http):
        _, _, ids = _add_ed2k_links(cloud_ready, 3)
        http.route("POST", ADD_URL, handler=_add_handler({1: (10003, "任务已存在 ed2k://|file|x|1|AA|/")}))
        body = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"}).get_json()
        assert body["success"] is True and body["ok"] == 2 and body["failed"] == 1
        failed = [r for r in body["results"] if r["state"] == "failed"]
        assert failed[0]["message"] == "任务已存在" and failed[0]["link_id"] == ids[1]

    def test_rejects_non_ed2k_and_deleted_links(self, client, cloud_ready, http):
        _, _, ids_115 = _add_ed2k_links(cloud_ready, 1, provider="115", title="网盘片")
        response = client.post(SUBMIT_URL, json={"resource_link_ids": ids_115, "target_pid": "1"})
        assert response.status_code == 400 and response.get_json()["code"] == "LINK_NOT_CLOUD_DOWNLOADABLE"
        _, _, ids_dead = _add_ed2k_links(cloud_ready, 1, deleted=True, title="已删片")
        response = client.post(SUBMIT_URL, json={"resource_link_ids": ids_dead, "target_pid": "1"})
        assert response.status_code == 400 and response.get_json()["code"] == "LINK_NOT_CLOUD_DOWNLOADABLE"
        assert http.calls_to(ADD_URL) == []

    def test_rejects_links_from_different_groups(self, client, cloud_ready, http):
        _, _, a = _add_ed2k_links(cloud_ready, 1, title="甲")
        _, _, b = _add_ed2k_links(cloud_ready, 1, title="乙")
        response = client.post(SUBMIT_URL, json={"resource_link_ids": a + b, "target_pid": "1"})
        assert response.status_code == 400 and response.get_json()["code"] == "CLOUD_DOWNLOAD_MIXED_GROUPS"

    def test_per_submit_cap_400(self, client, cloud_ready, http, hidrive):
        hidrive.setting_set("cloud_download_per_submit_cap", "2")
        _, _, ids = _add_ed2k_links(cloud_ready, 3)
        response = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"})
        assert response.status_code == 400 and response.get_json()["code"] == "CLOUD_DOWNLOAD_PER_SUBMIT_CAP"

    def test_duplicate_409(self, client, cloud_ready, http):
        _, _, ids = _add_ed2k_links(cloud_ready, 1)
        http.route("POST", ADD_URL, handler=_add_handler())
        assert client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"}).status_code == 200
        response = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"})
        assert response.status_code == 409 and response.get_json()["code"] == "CLOUD_DOWNLOAD_DUPLICATE"
        assert len(http.calls_to(ADD_URL)) == 1

    def test_quota_exhausted_409(self, client, cloud_ready, http, hidrive):
        hidrive._CLOUD_CACHE.clear()
        http.route("GET", QUOTA_URL, {"state": True, "data": {"count": 3000, "used": 2999, "surplus": 1, "package": []}})
        _, _, ids = _add_ed2k_links(cloud_ready, 2)
        response = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"})
        assert response.status_code == 409
        body = response.get_json()
        assert body["code"] == "CLOUD_DOWNLOAD_QUOTA_EXHAUSTED" and body["quota_surplus"] == 1
        assert http.calls_to(ADD_URL) == []

    def test_daily_cap_429_and_zero_means_unlimited(self, client, cloud_ready, http, hidrive):
        http.route("POST", ADD_URL, handler=_add_handler())
        hidrive.setting_set("cloud_download_daily_cap", "1")
        _, _, a = _add_ed2k_links(cloud_ready, 1, title="甲")
        _, _, b = _add_ed2k_links(cloud_ready, 1, title="乙")
        assert client.post(SUBMIT_URL, json={"resource_link_ids": a, "target_pid": "1"}).status_code == 200
        response = client.post(SUBMIT_URL, json={"resource_link_ids": b, "target_pid": "1"})
        assert response.status_code == 429 and response.get_json()["code"] == "CLOUD_DOWNLOAD_DAILY_CAP"
        hidrive.setting_set("cloud_download_daily_cap", "0")
        assert client.post(SUBMIT_URL, json={"resource_link_ids": b, "target_pid": "1"}).status_code == 200

    def test_disabled_403(self, client, cloud_ready, http, hidrive):
        hidrive.setting_set("cloud_download_enabled", "0")
        _, _, ids = _add_ed2k_links(cloud_ready, 1)
        response = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"})
        assert response.status_code == 403 and response.get_json()["code"] == "CLOUD_DOWNLOAD_DISABLED"

    def test_busy_409_when_another_submission_holds_the_lock(self, client, cloud_ready, http, hidrive):
        _, _, ids = _add_ed2k_links(cloud_ready, 1)
        assert hidrive._CLOUD_DOWNLOAD_LOCK.acquire(blocking=False)
        try:
            response = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"})
        finally:
            hidrive._CLOUD_DOWNLOAD_LOCK.release()
        assert response.status_code == 409 and response.get_json()["code"] == "CLOUD_DOWNLOAD_BUSY"

    def test_circuit_breaks_on_rate_limit_and_marks_rest_not_submitted(self, client, cloud_ready, http, hidrive, monkeypatch, audit_rows):
        monkeypatch.setattr(hidrive, "_cloud_sleep", lambda seconds: None)
        _, _, ids = _add_ed2k_links(cloud_ready, 12)
        handler = _add_handler(fail_call=2); http.route("POST", ADD_URL, handler=handler)
        response = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"})
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is False and body["ok"] == 10 and body["failed"] == 0 and body["not_submitted"] == 2
        assert "请求过于频繁" in body["message"]
        assert [r["state"] for r in body["results"]].count("not_submitted") == 2
        assert len(handler.calls) == 2
        audit = audit_rows("cloud_download.submit")
        assert audit[-1]["status"] == "failed"

    def test_response_has_no_url_or_token(self, client, cloud_ready, http):
        _, _, ids = _add_ed2k_links(cloud_ready, 2)
        http.route("POST", ADD_URL, handler=_add_handler({0: (1, "失败 ed2k://|file|leak|1|AA|/")}))
        raw = client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"}).get_data(as_text=True)
        for needle in ("ed2k://", "|file|", "open-access-fixture", "magnet:"):
            assert needle not in raw


TASKS_API = "/api/library/cloud-download/tasks"
QUOTA_API = "/api/library/cloud-download/quota"


def _tasks_body(*tasks):
    return {"state": True, "data": {"page": 1, "page_count": 1, "count": len(tasks), "tasks": list(tasks)}}


def _task(info_hash, status, name="Fixture.mkv", percent=100):
    return {"info_hash": info_hash, "name": name, "size": 1024, "percentDone": percent, "status": status,
            "url": "ed2k://|file|secret|1|AA|/", "add_time": 1700000000, "last_update": 1700000100,
            "file_id": "f1", "delete_file_id": "d1", "wp_path_id": "1", "can_appeal": 0}


class TestTaskManagement:
    def test_tasks_attach_origin_and_update_status(self, client, cloud_ready, http, hidrive):
        _, group_id, ids = _add_ed2k_links(cloud_ready, 1)
        http.route("POST", ADD_URL, handler=_add_handler())
        assert client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"}).status_code == 200
        http.route("GET", TASKS_URL, _tasks_body(_task("hash-1-0", -1, percent=0), _task("other-hash", 2, name="Elsewhere.mkv")))
        hidrive._CLOUD_CACHE.clear()
        body = client.get(TASKS_API + "?page=1").get_json()
        assert body["success"] is True and body["page"] == 1 and body["count"] == 2
        mine, other = body["tasks"]
        assert mine["info_hash"] == "hash-1-0" and mine["status"] == -1 and mine["status_label"] == "失败"
        assert mine["origin"] == {"media_id": mine["origin"]["media_id"], "media_title": "虚构片", "link_label": "ED2K · S01E01", "group_id": group_id}
        assert other["origin"] is None and other["status_label"] == "已完成"
        assert "url" not in mine and "url" not in other
        # Phase 6: the per-user row is the one that tracks state. The legacy
        # table keeps its submission record so a rollback still sees where a
        # task came from, but it is no longer written back to.
        with hidrive.connect_db() as db:
            row = db.execute(
                "SELECT state, last_seen_at FROM user_cloud_download_task WHERE info_hash='hash-1-0'").fetchone()
            legacy = db.execute(
                "SELECT info_hash FROM cloud_download_task WHERE info_hash='hash-1-0'").fetchone()
        assert row["state"] == "-1" and row["last_seen_at"]
        assert legacy is not None

    def test_tasks_upstream_error_502(self, client, cloud_ready, http, hidrive):
        hidrive._CLOUD_CACHE.clear()
        http.route("GET", TASKS_URL, {"state": False, "message": "服务繁忙"})
        response = client.get(TASKS_API)
        assert response.status_code == 502 and response.get_json()["code"] == "CLOUD_DOWNLOAD_UPSTREAM"

    def test_quota_endpoint(self, client, cloud_ready, http, hidrive):
        hidrive.setting_set("cloud_download_daily_cap", "50")
        body = client.get(QUOTA_API).get_json()
        assert body["success"] is True and body["surplus"] == 2988 and body["count"] == 3000 and body["used"] == 12
        assert body["package"][0]["name"] == "长期VIP"
        assert body["today_submitted"] == 0 and body["daily_cap"] == 50

    def test_delete_maps_flag_and_marks_record(self, client, cloud_ready, http, hidrive, audit_rows):
        _, _, ids = _add_ed2k_links(cloud_ready, 1)
        http.route("POST", ADD_URL, handler=_add_handler())
        client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"})
        http.route("POST", DEL_URL, {"state": True, "code": 0, "message": ""})
        response = client.post(TASKS_API + "/hash-1-0/delete", json={"delete_files": True})
        assert response.status_code == 200 and response.get_json()["success"] is True
        assert http.calls_to(DEL_URL)[-1]["data"] == {"info_hash": "hash-1-0", "del_source_file": "1"}
        response = client.post(TASKS_API + "/hash-1-0/delete", json={})
        assert http.calls_to(DEL_URL)[-1]["data"] == {"info_hash": "hash-1-0", "del_source_file": "0"}
        with hidrive.connect_db() as db:
            assert db.execute("SELECT last_status FROM cloud_download_task WHERE info_hash='hash-1-0'").fetchone()[0] == -2
        assert audit_rows("cloud_download.delete")[-1]["status"] == "success"

    def test_delete_rejects_bad_hash(self, client, cloud_ready, http):
        assert client.post(TASKS_API + "/not%20a%20hash/delete", json={}).status_code == 400
        assert http.calls_to(DEL_URL) == []

    @pytest.mark.parametrize("scope,flag", [("failed", "2"), ("completed", "0")])
    def test_clear_scope_mapping(self, client, cloud_ready, http, scope, flag, audit_rows):
        http.route("POST", CLEAR_URL, {"state": True, "code": 0, "message": ""})
        response = client.post(TASKS_API + "/clear", json={"scope": scope})
        assert response.status_code == 200
        assert http.calls_to(CLEAR_URL)[-1]["data"] == {"flag": flag}
        assert audit_rows("cloud_download.clear")[-1]["detail"].startswith(f"scope={scope}")

    @pytest.mark.parametrize("scope", ["all", "1", "", None, "delete_files"])
    def test_clear_rejects_other_scopes(self, client, cloud_ready, http, scope):
        response = client.post(TASKS_API + "/clear", json={"scope": scope})
        assert response.status_code == 400
        assert http.calls_to(CLEAR_URL) == []


class TestTheAdministratorsLegacyTasksStillCount:
    """Round 4: the task list and the daily count read only the per-user
    table, so before --auth-migrate --apply the administrator's older tasks
    lost their origin notes and did not count against today's cap."""

    def _legacy_task(self, hidrive, info_hash, submitted_at, title="旧片"):
        with hidrive.connect_db() as db:
            db.execute(
                "INSERT INTO cloud_download_task(info_hash, link_public_id, media_id, group_id, media_title, "
                "link_label, wp_path_id, submitted_at) VALUES(?,?,?,?,?,?,?,?)",
                (info_hash, "pub-legacy", 7, 3, title, "ED2K · 旧集", "cid-1", submitted_at))

    def test_a_pre_migration_task_keeps_its_origin_in_the_list(self, client, cloud_ready, http, hidrive):
        self._legacy_task(hidrive, "hash-legacy-old", 100)
        http.route("GET", TASKS_URL, {"state": True, "data": {"page": 1, "page_count": 1, "count": 1, "tasks": [
            {"info_hash": "hash-legacy-old", "name": "Old.mkv", "size": 10, "percentDone": 100, "status": 2}]}})
        hidrive._CLOUD_CACHE.clear()
        body = client.get(TASKS_API).get_json()
        (task,) = body["tasks"]
        assert task["origin"] is not None, "the legacy task lost its origin"
        assert task["origin"]["media_title"] == "旧片" and task["origin"]["link_label"] == "ED2K · 旧集"

    def test_todays_legacy_submissions_count_against_the_cap(self, client, cloud_ready, http, hidrive):
        import time as _time

        self._legacy_task(hidrive, "hash-legacy-today", int(_time.time()))
        assert client.get(STATUS_URL).get_json()["today_submitted"] == 1

    def test_a_task_in_both_tables_is_counted_once(self, client, cloud_ready, http, hidrive):
        _, _, ids = _add_ed2k_links(cloud_ready, 1)
        http.route("POST", ADD_URL, handler=_add_handler())
        assert client.post(SUBMIT_URL, json={"resource_link_ids": ids, "target_pid": "1"}).status_code == 200
        with hidrive.connect_db() as db:
            both = db.execute("SELECT (SELECT COUNT(*) FROM cloud_download_task) + "
                              "(SELECT COUNT(*) FROM user_cloud_download_task)").fetchone()[0]
        assert both == 2, "the administrator's submit should land in both tables"
        assert client.get(STATUS_URL).get_json()["today_submitted"] == 1

    def test_a_members_view_never_reads_the_legacy_table(self, client, cloud_ready, http, hidrive):
        """The fallback is the administrator's alone.

        The legacy row is dated *today*, so it would count -- and would carry
        an origin -- if the member path read the old table. A row dated
        elsewhere would prove nothing about the count either way.
        """
        import time as _time

        import auth_service as auth

        self._legacy_task(hidrive, "hash-legacy-member", int(_time.time()))
        client.post("/api/auth/register", json={
            "email": "member@example.test", "password": MEMBER_PASSWORD, "confirm_password": MEMBER_PASSWORD})
        with hidrive.connect_db() as db:
            admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
            row = db.execute("SELECT id FROM auth_user WHERE email_norm='member@example.test'").fetchone()
            auth.approve_user(db, int(row["id"]), approver_id=admin_id, now=2)
        assert client.post("/api/auth/login", json={
            "email": "member@example.test", "password": MEMBER_PASSWORD}).status_code == 200
        member = auth.CurrentUser(id=int(row["id"]), email="m", role="member", status="active")
        with hidrive.app.test_request_context("/"):
            hidrive.g.current_user = member
            assert hidrive._cloud_today_submitted() == 0, "a member's daily count read the administrator's legacy table"
        # And the member's task list, given the same hash by 115, shows no
        # origin: the legacy note is the administrator's, not theirs.
        with hidrive.connect_db() as db:
            user_115.secret_set(db, member.id, user_115.OPEN_ACCESS_SECRET, "member-token-fixture",
                                fernet=hidrive.load_fernet(), now=1)
        http.route("GET", TASKS_URL, {"state": True, "data": {"page": 1, "page_count": 1, "count": 1, "tasks": [
            {"info_hash": "hash-legacy-member", "name": "Old.mkv", "size": 10, "percentDone": 100, "status": 2}]}})
        hidrive._CLOUD_CACHE.clear()
        listed = client.get(TASKS_API).get_json()
        assert listed["tasks"][0]["origin"] is None, "a member's list showed the administrator's legacy origin"
