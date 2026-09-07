"""Tests for the app.py surface of the w6 link-validity checker: settings
validation, the two new endpoints (linkcheck-status, resource/<id>/recheck),
the ``--library-check-links`` CLI, and background-thread autostart wiring.

Uses the shared ``hidrive``/``workspace``/``client`` fixtures from
tests/conftest.py; builds its own minimal library directly at
``hidrive.LIBRARY_DB_PATH`` (same approach as tests/test_library_tmdb_enrich.py's
``store`` fixture) rather than the heavier synthetic-bundle fixture, since
these tests only need one or two links per test. URLs/access codes are
fixtures (``swfake...`` share codes, 4-char placeholder codes), never real.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time as real_time

import pytest

import library_store as ls


class _FakeQuarkValidResponse:
    """A minimal ``requests``-shaped fake for a quark "valid" verdict
    (``{"status": 200, "code": 0}``) -- ``iter_content``/``close`` (I5:
    ``check_link`` reads the body via a streaming, capped loop, never
    ``.json()``/``.text``/``.content`` directly) hand back the same JSON
    bytes in one chunk."""

    status_code = 200

    def json(self):
        return {"status": 200, "code": 0}

    def iter_content(self, chunk_size=8192):
        yield json.dumps(self.json()).encode("utf-8")

    def close(self):
        pass


class _FakeQuarkValidSession:
    def request(self, method, url, **kwargs):
        return _FakeQuarkValidResponse()


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = real_time.time() + timeout
    while real_time.time() < deadline:
        if predicate():
            return
        real_time.sleep(0.005)
    raise AssertionError("condition not met within timeout")


@pytest.fixture
def linkcheck_store(hidrive, workspace):
    store = ls.LibraryStore(hidrive.LIBRARY_DB_PATH, hidrive.load_fernet())
    store.create_schema()
    # Mark it "installed" (like install_bundle's finalize stage) so
    # open_installed()/_library_store_or_error() accept it -- otherwise
    # every read/write route 503s with LIBRARY_NOT_ENCRYPTED.
    store.meta_set("encrypted", "1")
    return store


def _add_link(store, *, provider, url, access_code=None):
    fernet = store.fernet
    media_id = store.upsert_media(
        ls.MediaRecord(media_identity=f"tt-fake-{url}", media_type="movie", title_zh="虚构片", search_key="虚构片")
    )
    group_id = store.upsert_group(ls.GroupRecord(media_id=media_id, edition_fingerprint=f"fp-{url}", display_title="g"))
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
    store.upsert_link(rec)
    store.recount()
    return media_id, group_id, url_hash


# ---------------------------------------------------------------------------
# POST /api/settings
# ---------------------------------------------------------------------------


class TestSettingsLinkcheck:
    def test_stores_global_enable(self, client, hidrive):
        assert hidrive.setting_get("linkcheck_enabled") is None
        response = client.post("/api/settings", json={"linkcheck_enabled": True})
        assert response.status_code == 200
        assert hidrive.setting_get("linkcheck_enabled") == "1"

    def test_rejects_invalid_global_enable(self, client, hidrive):
        response = client.post("/api/settings", json={"linkcheck_enabled": "maybe"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "BAD_REQUEST"

    def test_stores_nested_provider_settings(self, client, hidrive):
        response = client.post(
            "/api/settings",
            json={"linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 1500}}},
        )
        assert response.status_code == 200
        assert hidrive.setting_get("linkcheck_quark_enabled") == "1"
        assert hidrive.setting_get("linkcheck_quark_daily_cap") == "1500"

    def test_rejects_unknown_provider_code(self, client, hidrive):
        response = client.post("/api/settings", json={"linkcheck_providers": {"baidu": {"enabled": True}}})
        assert response.status_code == 400
        assert response.get_json()["code"] == "LINKCHECK_PROVIDER_INVALID"

    @pytest.mark.parametrize("cap", [-1, 20001, "abc", True])
    def test_rejects_out_of_range_daily_cap(self, client, hidrive, cap):
        response = client.post("/api/settings", json={"linkcheck_providers": {"115": {"daily_cap": cap}}})
        assert response.status_code == 400
        assert response.get_json()["code"] == "LINKCHECK_DAILY_CAP_INVALID"

    def test_rejects_non_dict_providers_body(self, client, hidrive):
        response = client.post("/api/settings", json={"linkcheck_providers": "nope"})
        assert response.status_code == 400


class TestSettingsLinkcheckDailyCapRequiredWhenEnabled:
    """M2: a provider that ends up enabled must have a real (>=1) daily
    cap; a disabled provider may keep 0."""

    def test_enabling_with_daily_cap_zero_is_rejected(self, client, hidrive):
        response = client.post(
            "/api/settings",
            json={"linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 0}}},
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == "LINKCHECK_DAILY_CAP_INVALID"
        assert hidrive.setting_get("linkcheck_quark_enabled") is None  # nothing written

    def test_enabling_with_no_daily_cap_at_all_and_none_stored_is_rejected(self, client, hidrive):
        response = client.post(
            "/api/settings",
            json={"linkcheck_providers": {"quark": {"enabled": True}}},
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == "LINKCHECK_DAILY_CAP_INVALID"

    def test_enabling_together_with_a_real_daily_cap_is_accepted(self, client, hidrive):
        response = client.post(
            "/api/settings",
            json={"linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 100}}},
        )
        assert response.status_code == 200
        assert hidrive.setting_get("linkcheck_quark_enabled") == "1"
        assert hidrive.setting_get("linkcheck_quark_daily_cap") == "100"

    def test_disabling_with_daily_cap_zero_is_accepted(self, client, hidrive):
        response = client.post(
            "/api/settings",
            json={"linkcheck_providers": {"quark": {"enabled": False, "daily_cap": 0}}},
        )
        assert response.status_code == 200
        assert hidrive.setting_get("linkcheck_quark_enabled") == "0"
        assert hidrive.setting_get("linkcheck_quark_daily_cap") == "0"

    def test_a_previously_stored_cap_of_zero_blocks_a_later_bare_enable(self, client, hidrive):
        first = client.post(
            "/api/settings",
            json={"linkcheck_providers": {"quark": {"enabled": False, "daily_cap": 0}}},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/settings",
            json={"linkcheck_providers": {"quark": {"enabled": True}}},
        )
        assert second.status_code == 400
        assert second.get_json()["code"] == "LINKCHECK_DAILY_CAP_INVALID"

    def test_a_previously_stored_positive_cap_lets_a_later_bare_enable_through(self, client, hidrive):
        first = client.post(
            "/api/settings",
            json={"linkcheck_providers": {"quark": {"enabled": False, "daily_cap": 500}}},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/settings",
            json={"linkcheck_providers": {"quark": {"enabled": True}}},
        )
        assert second.status_code == 200
        assert hidrive.setting_get("linkcheck_quark_enabled") == "1"


# ---------------------------------------------------------------------------
# GET /api/library/linkcheck-status
# ---------------------------------------------------------------------------


class TestLinkcheckStatusEndpoint:
    def test_not_installed_reports_error(self, client, hidrive):
        response = client.get("/api/library/linkcheck-status")
        assert response.status_code == 503

    def test_default_shape_with_no_links(self, client, hidrive, linkcheck_store):
        response = client.get("/api/library/linkcheck-status")
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["enabled"] is False
        assert body["heartbeat_at"] is None
        assert set(body["providers"].keys()) == {"tianyicloud", "quark", "alipan", "115"}
        for code, provider_payload in body["providers"].items():
            assert provider_payload["enabled"] is False
            assert provider_payload["used_today"] == 0
            assert provider_payload["paused_until"] is None
            assert provider_payload["last_error_class"] is None
            assert provider_payload["valid"] == 0
            assert provider_payload["invalid"] == 0
        assert body["providers"]["115"]["daily_cap"] == 500
        assert body["providers"]["115"]["interval_seconds"] == 10.0
        assert body["providers"]["quark"]["daily_cap"] == 3000
        assert body["totals"] == {
            "checked": 0, "valid": 0, "invalid": 0, "unknown": 0, "unchecked": 0, "queued_priority": 0,
        }

    def test_settings_are_reflected(self, client, hidrive, linkcheck_store):
        client.post("/api/settings", json={
            "linkcheck_enabled": True,
            "linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 42}},
        })
        body = client.get("/api/library/linkcheck-status").get_json()
        assert body["enabled"] is True
        assert body["providers"]["quark"]["enabled"] is True
        assert body["providers"]["quark"]["daily_cap"] == 42
        assert body["providers"]["tianyicloud"]["enabled"] is False

    def test_counts_reflect_recorded_checks(self, client, hidrive, linkcheck_store):
        url_valid = "https://pan.quark.cn/s/swfakestatus1"
        url_invalid = "https://pan.quark.cn/s/swfakestatus2"
        _, _, hash_valid = _add_link(linkcheck_store, provider="quark", url=url_valid)
        _, _, hash_invalid = _add_link(linkcheck_store, provider="quark", url=url_invalid)
        linkcheck_store.record_link_check(
            "quark", hash_valid, status="valid", reason="ok", http_class="200",
            checked_at=1, next_check_at=999_999_999_999, consecutive_unknown=0,
        )
        linkcheck_store.record_link_check(
            "quark", hash_invalid, status="invalid", reason="share_not_found", http_class="200",
            checked_at=1, next_check_at=999_999_999_999, consecutive_unknown=0,
        )
        body = client.get("/api/library/linkcheck-status").get_json()
        assert body["providers"]["quark"]["valid"] == 1
        assert body["providers"]["quark"]["invalid"] == 1
        assert body["totals"]["valid"] == 1
        assert body["totals"]["invalid"] == 1
        assert body["totals"]["checked"] == 2

    def test_queued_priority_counts_recheck_queue(self, client, hidrive, linkcheck_store):
        _, group_id, _ = _add_link(linkcheck_store, provider="quark", url="https://pan.quark.cn/s/swfakestatus3")
        client.post("/api/settings", json={"linkcheck_enabled": True, "linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 10}}})
        response = client.post(f"/api/library/resource/{group_id}/recheck")
        assert response.status_code == 200
        body = client.get("/api/library/linkcheck-status").get_json()
        assert body["totals"]["queued_priority"] == 1


# ---------------------------------------------------------------------------
# POST /api/library/resource/<id>/recheck
# ---------------------------------------------------------------------------


class TestRecheckEndpoint:
    def test_missing_group_is_404(self, client, hidrive, linkcheck_store):
        response = client.post("/api/library/resource/999999/recheck")
        assert response.status_code == 404

    def test_queues_live_links_of_enabled_providers(self, client, hidrive, linkcheck_store):
        _, group_id, _ = _add_link(linkcheck_store, provider="quark", url="https://pan.quark.cn/s/swfakerecheck1")
        client.post("/api/settings", json={"linkcheck_enabled": True, "linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 10}}})

        response = client.post(f"/api/library/resource/{group_id}/recheck")
        assert response.status_code == 200
        body = response.get_json()
        assert body == {
            "success": True, "queued": 1, "skipped_disabled": 0, "skipped_unsupported": 0,
            "message": "已加入检测队列（1 条）",
        }

    def test_disabled_provider_is_counted_as_skipped(self, client, hidrive, linkcheck_store):
        _, group_id, _ = _add_link(linkcheck_store, provider="quark", url="https://pan.quark.cn/s/swfakerecheck2")
        # linkcheck_enabled left off entirely -- every provider counts as disabled.
        response = client.post(f"/api/library/resource/{group_id}/recheck")
        body = response.get_json()
        assert body["queued"] == 0
        assert body["skipped_disabled"] == 1
        assert body["skipped_unsupported"] == 0

    def test_unsupported_provider_is_counted_separately_from_disabled(self, client, hidrive, linkcheck_store):
        # M4: baidu has no checker adapter at all -- it must never be
        # lumped in with a phase-1 provider that's merely toggled off.
        _, group_id, _ = _add_link(linkcheck_store, provider="baidu", url="https://pan.baidu.com/s/swfakerecheck6")
        response = client.post(f"/api/library/resource/{group_id}/recheck")
        body = response.get_json()
        assert body["queued"] == 0
        assert body["skipped_disabled"] == 0
        assert body["skipped_unsupported"] == 1

    def test_second_request_within_cooldown_is_429(self, client, hidrive, linkcheck_store):
        _, group_id, _ = _add_link(linkcheck_store, provider="quark", url="https://pan.quark.cn/s/swfakerecheck3")
        client.post("/api/settings", json={"linkcheck_enabled": True, "linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 10}}})

        first = client.post(f"/api/library/resource/{group_id}/recheck")
        assert first.status_code == 200
        second = client.post(f"/api/library/resource/{group_id}/recheck")
        assert second.status_code == 429
        assert second.get_json()["code"] == "LINKCHECK_RECHECK_COOLDOWN"

    def test_different_groups_each_get_their_own_cooldown(self, client, hidrive, linkcheck_store):
        _, group_a, _ = _add_link(linkcheck_store, provider="quark", url="https://pan.quark.cn/s/swfakerecheck4")
        _, group_b, _ = _add_link(linkcheck_store, provider="quark", url="https://pan.quark.cn/s/swfakerecheck5")
        client.post("/api/settings", json={"linkcheck_enabled": True, "linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 10}}})

        assert client.post(f"/api/library/resource/{group_a}/recheck").status_code == 200
        assert client.post(f"/api/library/resource/{group_b}/recheck").status_code == 200


# ---------------------------------------------------------------------------
# --library-check-links CLI
# ---------------------------------------------------------------------------


class TestLibraryCheckLinksCli:
    def test_unknown_provider_reports_ok_false(self, hidrive, linkcheck_store, capsys):
        exit_code = hidrive.run_library_check_links(provider="baidu", sample=None, dry_run=True, as_json=True)
        assert exit_code == 0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed == {"ok": False, "error": "UnknownProvider", "provider": "baidu"}

    def test_not_installed_reports_ok_false(self, hidrive, workspace, capsys):
        exit_code = hidrive.run_library_check_links(provider=None, sample=None, dry_run=True, as_json=True)
        assert exit_code == 0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed["ok"] is False

    def test_dry_run_json_output_never_writes(self, hidrive, linkcheck_store, monkeypatch, capsys):
        url = "https://pan.quark.cn/s/swfakecli1"
        _add_link(linkcheck_store, provider="quark", url=url)

        _FakeSession = _FakeQuarkValidSession

        monkeypatch.setattr(hidrive, "_linkcheck_session_factory", lambda: _FakeSession())

        exit_code = hidrive.run_library_check_links(provider="quark", sample=5, dry_run=True, as_json=True)
        assert exit_code == 0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed["ok"] is True
        assert printed["dry_run"] is True
        assert printed["checked"] == 1
        assert printed["valid"] == 1
        assert "swfakecli1" not in json.dumps(printed)

        conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
        try:
            count = conn.execute("SELECT COUNT(*) FROM link_check").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_explicit_provider_without_dry_run_respects_the_disabled_kill_switch(
        self, hidrive, linkcheck_store, monkeypatch, capsys,
    ):
        """I2: --provider must NOT bypass linkcheck_enabled/the provider's
        own enabled flag outside of --dry-run -- the old code let an
        explicit --provider probe (and write to link_check, and spend
        budget for) a provider the settings page has switched off."""
        url = "https://pan.quark.cn/s/swfakecliskip1"
        _add_link(linkcheck_store, provider="quark", url=url)

        class _FakeSession:
            def request(self, method, url, **kwargs):
                raise AssertionError("must never probe a disabled provider without --dry-run")

        monkeypatch.setattr(hidrive, "_linkcheck_session_factory", lambda: _FakeSession())

        exit_code = hidrive.run_library_check_links(provider="quark", sample=5, dry_run=False, as_json=True)
        assert exit_code == 0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed == {
            "ok": True, "provider": "quark", "dry_run": False,
            "skipped": "disabled", "reason": "provider_disabled",
        }

        conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
        try:
            count = conn.execute("SELECT COUNT(*) FROM link_check").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_explicit_provider_with_dry_run_still_bypasses_the_kill_switch(
        self, hidrive, linkcheck_store, monkeypatch, capsys,
    ):
        # dry-run remains the one way --provider may calibrate a provider
        # before it's ever switched on (existing behaviour, unaffected by I2).
        url = "https://pan.quark.cn/s/swfakecliskip2"
        _add_link(linkcheck_store, provider="quark", url=url)

        _FakeSession = _FakeQuarkValidSession

        monkeypatch.setattr(hidrive, "_linkcheck_session_factory", lambda: _FakeSession())

        exit_code = hidrive.run_library_check_links(provider="quark", sample=5, dry_run=True, as_json=True)
        assert exit_code == 0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed["ok"] is True
        assert printed.get("skipped") is None
        assert printed["checked"] == 1

    def test_explicit_provider_enabled_via_settings_is_actually_probed(
        self, client, hidrive, linkcheck_store, monkeypatch, capsys,
    ):
        url = "https://pan.quark.cn/s/swfakecliskip3"
        _add_link(linkcheck_store, provider="quark", url=url)
        client.post("/api/settings", json={"linkcheck_enabled": True, "linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 10}}})

        _FakeSession = _FakeQuarkValidSession

        monkeypatch.setattr(hidrive, "_linkcheck_session_factory", lambda: _FakeSession())

        exit_code = hidrive.run_library_check_links(provider="quark", sample=5, dry_run=False, as_json=True)
        assert exit_code == 0
        printed = json.loads(capsys.readouterr().out.strip())
        assert printed.get("skipped") is None
        assert printed["checked"] == 1

    def test_human_readable_output_without_json_flag(self, hidrive, linkcheck_store, monkeypatch, capsys):
        url = "https://pan.quark.cn/s/swfakecli2"
        _add_link(linkcheck_store, provider="quark", url=url)

        _FakeSession = _FakeQuarkValidSession

        monkeypatch.setattr(hidrive, "_linkcheck_session_factory", lambda: _FakeSession())

        exit_code = hidrive.run_library_check_links(provider="quark", sample=5, dry_run=True, as_json=False)
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "quark:" in out
        assert "swfakecli2" not in out


# ---------------------------------------------------------------------------
# Background thread autostart (mirrors TestBackgroundEnricherAutostart)
# ---------------------------------------------------------------------------


class TestBackgroundLinkCheckerAutostart:
    def test_disabled_by_default_does_not_start(self, client, hidrive, monkeypatch):
        calls = []
        monkeypatch.setattr(hidrive.library_tmdb.BackgroundLinkChecker, "start", lambda self: calls.append(1))
        client.get("/api/status")
        client.get("/api/status")
        assert calls == []

    def test_enabled_starts_exactly_once(self, client, hidrive, monkeypatch):
        monkeypatch.setenv("LIBRARY_ENRICH_AUTOSTART", "1")
        calls = []
        monkeypatch.setattr(hidrive.library_tmdb.BackgroundLinkChecker, "start", lambda self: calls.append(1))
        client.get("/api/status")
        client.get("/api/status")
        client.get("/api/status")
        assert len(calls) == 1



# ---------------------------------------------------------------------------
# M7: --library-check-links --sample <non-numeric> must never raise a raw
# traceback -- a real subprocess invocation of app.py's own argv parsing
# (in-process calls to run_library_check_links() bypass that parsing
# entirely, so this exercises the actual __main__ block).
# ---------------------------------------------------------------------------

import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

_ROOT = Path(__file__).resolve().parents[1]


def _sample_arg_cli_env(tmp_path) -> dict:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "strm").mkdir()
    key_file = tmp_path / "master.key"
    key_file.write_bytes(Fernet.generate_key())
    env = dict(os.environ)
    for name in ("HDHIVE_APP_SECRET", "HDHIVE_CLIENT_ID", "TMDB_API_KEY", "ENV_115_COOKIES", "OPENLIST_TOKEN"):
        env.pop(name, None)
    env.update({
        "HIDRIVE_AUTH_MODE": "local",
        "HIDRIVE_DATA_DIR": str(data_dir),
        "HIDRIVE_MASTER_KEY_FILE": str(key_file),
        "STRM_ROOT": str(data_dir / "strm"),
        "OPENLIST_DB": str(data_dir / "no-openlist.db"),
        "OPENLIST_URL": "http://openlist.test",
        "HIDRIVE_PUBLIC_ORIGIN": "https://hidrive.test",
        "LIBRARY_ENRICH_AUTOSTART": "0",
    })
    return env


class TestLibraryCheckLinksCliArgvValidation:
    def test_non_numeric_sample_reports_json_error_and_exits_2(self, tmp_path):
        env = _sample_arg_cli_env(tmp_path)
        result = subprocess.run(
            [sys.executable, str(_ROOT / "app.py"), "--library-check-links", "--sample", "not-a-number", "--json"],
            env=env, cwd=str(_ROOT), capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        body = json.loads(result.stdout.strip().splitlines()[-1])
        assert body["ok"] is False
        assert body["error"] == "InvalidSample"

    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_non_positive_sample_reports_json_error_and_exits_2(self, tmp_path, bad):
        # Review follow-up: SQLite treats a negative LIMIT as "no limit" and
        # the dry-run path skips the daily budget, so --sample 0/-1 must be
        # refused like a non-numeric value -- never probed.
        env = _sample_arg_cli_env(tmp_path)
        result = subprocess.run(
            [sys.executable, str(_ROOT / "app.py"), "--library-check-links", "--provider", "quark", "--sample", bad, "--dry-run", "--json"],
            env=env, cwd=str(_ROOT), capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        body = json.loads(result.stdout.strip().splitlines()[-1])
        assert body["ok"] is False
        assert body["error"] == "InvalidSample"
        assert body["value"] == bad


class TestRealLinkCheckerWiring:
    """Production wiring end-to-end: a real BackgroundLinkChecker built by
    _build_library_linkchecker(), started for real, completes a round
    against the real store/settings/session-factory wiring (with a fake
    session injected) without deadlocking."""

    def test_real_wiring_completes_a_round(self, client, hidrive, linkcheck_store, monkeypatch):
        url = "https://pan.quark.cn/s/swfakewiring1"
        _add_link(linkcheck_store, provider="quark", url=url)
        client.post("/api/settings", json={"linkcheck_enabled": True, "linkcheck_providers": {"quark": {"enabled": True, "daily_cap": 10}}})

        _FakeSession = _FakeQuarkValidSession

        monkeypatch.setattr(hidrive, "_linkcheck_session_factory", lambda: _FakeSession())

        checker = hidrive._build_library_linkchecker()
        checker._idle_seconds = 0.02
        checker.start()
        try:
            def _round_completed():
                conn = sqlite3.connect(str(hidrive.LIBRARY_DB_PATH))
                try:
                    return conn.execute("SELECT COUNT(*) FROM link_check").fetchone()[0] > 0
                finally:
                    conn.close()

            _wait_until(_round_completed, timeout=2.0)
        finally:
            checker.stop(timeout=2)

        response = client.get("/api/library/linkcheck-status")
        body = response.get_json()
        assert body["providers"]["quark"]["valid"] == 1


# ---------------------------------------------------------------------------
# Round 16: GET /api/library/media/<id>?include_deleted=1
# ---------------------------------------------------------------------------

_SAFE_LINK_KEYS = {
    "link_id", "provider", "label", "has_access_code", "deleted", "check_status", "check_reason",
    "checked_at", "invalid", "remark", "created_at", "actions",
}


class TestMediaDetailIncludeDeleted:
    def test_all_invalid_group_visible_only_with_include_deleted(self, client, hidrive, linkcheck_store):
        url = "https://pan.quark.cn/s/swfakeapiincl1"
        media_id, group_id, url_hash = _add_link(linkcheck_store, provider="quark", url=url, access_code="ab12")
        linkcheck_store.record_link_check(
            "quark", url_hash, status="invalid", reason="share_expired", http_class="404",
            checked_at=1_700_000_000, next_check_at=1_700_600_000, consecutive_unknown=0,
        )
        linkcheck_store.recount_groups([group_id])

        default = client.get(f"/api/library/media/{media_id}").get_json()
        assert default["groups"] == [] and default["group_count"] == 0

        shown = client.get(f"/api/library/media/{media_id}?include_deleted=1").get_json()
        (group,) = shown["groups"]
        assert group["group_id"] == group_id
        (link,) = group["links"]
        assert link["check_status"] == "invalid" and link["invalid"] is True
        assert link["check_reason"] == "share_expired"
        assert link["checked_at"].startswith("2023-11-14")
        # Never a URL, share code, access code or response body.
        assert set(link) == _SAFE_LINK_KEYS
        assert "swfakeapiincl1" not in shown.get("groups")[0]["links"][0].__repr__()
        assert "ab12" not in str(shown)

    def test_include_deleted_zero_or_garbage_keeps_default(self, client, hidrive, linkcheck_store):
        url = "https://pan.quark.cn/s/swfakeapiincl2"
        media_id, group_id, url_hash = _add_link(linkcheck_store, provider="quark", url=url)
        linkcheck_store.record_link_check(
            "quark", url_hash, status="invalid", reason="share_cancelled", http_class="404",
            checked_at=1, next_check_at=2, consecutive_unknown=0,
        )
        linkcheck_store.recount_groups([group_id])
        for value in ("0", "false", "yes", ""):
            assert client.get(f"/api/library/media/{media_id}?include_deleted={value}").get_json()["groups"] == []
