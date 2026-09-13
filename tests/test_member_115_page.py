"""R04: the page a member actually gets, in each of their four 115 states.

Before this round the shell was rendered with a hardcoded "nobody has 115",
so a member's own scan and transfer dialogs were never in the markup while
the buttons that drive them were. These tests walk the four states -- nothing
connected, step A only, step B only, both -- and assert the page contains
exactly the dialogs its own buttons reach for, and still nothing operator.

Every cookie and token below is invented and lives in the test's own
temporary database.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service as auth  # noqa: E402
import user_115  # noqa: E402

MEMBER_PASSWORD = "Correct1Horse"
MEMBER_EMAIL = "member@example.test"


@pytest.fixture
def member(client, hidrive):
    client.post("/api/auth/register", json={
        "email": MEMBER_EMAIL, "password": MEMBER_PASSWORD, "confirm_password": MEMBER_PASSWORD})
    with hidrive.connect_db() as db:
        admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        row = db.execute("SELECT id FROM auth_user WHERE email_norm=?", (MEMBER_EMAIL,)).fetchone()
        auth.approve_user(db, int(row["id"]), approver_id=admin_id, now=2)
    assert client.post("/api/auth/login", json={
        "email": MEMBER_EMAIL, "password": MEMBER_PASSWORD}).status_code == 200
    return int(row["id"])


def _connect(hidrive, user_id, *, step_a=False, step_b=False):
    with hidrive.connect_db() as db:
        if step_a:
            user_115.secret_set(db, user_id, user_115.COOKIE_SECRET,
                                "UID=fixture-not-real; CID=fixture-not-real; SEID=fixture-not-real",
                                fernet=hidrive.load_fernet(), now=1)
        if step_b:
            user_115.secret_set(db, user_id, user_115.OPEN_ACCESS_SECRET, "token-fixture-not-real",
                                fernet=hidrive.load_fernet(), now=1)


STATES = [
    pytest.param(False, False, id="nothing-connected"),
    pytest.param(True, False, id="step-a-only"),
    pytest.param(False, True, id="step-b-only"),
    pytest.param(True, True, id="both"),
]


class TestTheFourStates:
    @pytest.mark.parametrize("step_a,step_b", STATES)
    def test_the_scan_dialog_is_always_there(self, client, hidrive, member, step_a, step_b):
        """It is how step A gets connected, so it cannot be conditional on
        already having step A."""
        _connect(hidrive, member, step_a=step_a, step_b=step_b)
        page = client.get("/").get_data(as_text=True)
        assert 'id="reauthDialog"' in page
        assert 'id="my115Card"' in page or "我的 115" in page

    @pytest.mark.parametrize("step_a,step_b", STATES)
    def test_every_element_the_page_reaches_for_exists(self, client, hidrive, member, step_a, step_b):
        """R04's actual failure was a null: a button shipped without the
        dialog it opens. Whatever the page renders, the ids its own inline
        wiring names have to be in it."""
        _connect(hidrive, member, step_a=step_a, step_b=step_b)
        page = client.get("/").get_data(as_text=True)
        present = set(re.findall(r'id="([A-Za-z0-9_-]+)"', page))
        # The transfer dialog is all-or-nothing: either every part of it is
        # in the page or none of it is.
        transfer_parts = {"transferDialog", "transferTitle", "transferSubtitle", "folderResult",
                          "targetPath", "saveBtn", "transferResult", "folderCurrent"}
        overlap = transfer_parts & present
        assert overlap in (set(), transfer_parts), sorted(transfer_parts - overlap)
        reauth_parts = {"reauthDialog", "reauthQrImage", "reauthCountdown", "reauthResult",
                        "reauthCancel", "reauthRefresh", "reauthClose"}
        overlap = reauth_parts & present
        assert overlap in (set(), reauth_parts), sorted(reauth_parts - overlap)

    @pytest.mark.parametrize("step_a,step_b", STATES)
    def test_no_operator_surface_ever_appears(self, client, hidrive, member, step_a, step_b):
        _connect(hidrive, member, step_a=step_a, step_b=step_b)
        page = client.get("/").get_data(as_text=True)
        for absent in ('data-tab="openlist"', 'data-tab="strm"', 'id="cookie115"',
                       'id="tmdbKey"', 'id="usersCard"', 'id="policyMemberUnlock"',
                       # F01: this one's absence is what made views.reauth.init()
                       # throw. It was missing from this list, which is why the
                       # crash shipped; tests/test_member_bootstrap_browser.py
                       # now also runs the real bootstrap.
                       'id="reauth115Btn"', 'id="cloudStatusLine"'):
            assert absent not in page, absent

    def test_step_a_brings_the_transfer_dialog(self, client, hidrive, member):
        _connect(hidrive, member, step_a=True)
        page = client.get("/").get_data(as_text=True)
        assert 'id="transferDialog"' in page

    def test_step_b_brings_the_cloud_workspace_and_the_shared_dialog(self, client, hidrive, member):
        """F01: cloud download opens the same #transferDialog the transfer
        flow uses, so step B has to ship it even without a cookie."""
        _connect(hidrive, member, step_b=True)
        page = client.get("/").get_data(as_text=True)
        assert 'data-tab="cloud"' in page
        assert 'id="transferDialog"' in page

    def test_with_nothing_connected_neither_arrives(self, client, hidrive, member):
        page = client.get("/").get_data(as_text=True)
        assert 'id="transferDialog"' not in page
        assert 'data-tab="cloud"' not in page

    def test_the_capabilities_match_what_was_rendered(self, client, hidrive, member):
        _connect(hidrive, member, step_a=True)
        body = client.get("/api/me").get_json()
        assert body["capabilities"]["own_115_transfer"] is True
        assert body["capabilities"]["own_115_cloud_download"] is False


class TestAnUnlockThatSucceeded:
    """R04: what happens after an unlock is a separate step with its own
    failure. Being told "RE0 解锁失败" after paying would invite paying
    again for something already owned."""

    def _harness(self, body):
        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        match = re.search(r"re0Action:\s*function\s*\(candidate, btn\)\s*\{([\s\S]*?)\n    \},", js)
        assert match, "expected an re0Action function"
        return """
var toasts = [], unlocks = 0, opened = 0;
function $(id) { return { querySelector: function () { return { textContent: "片名" }; } }; }
function toast(message, kind) { toasts.push({ message: message, kind: kind }); }
function re0PointsLabel() { return "（10 分）"; }
var RE0_DONE_ACTION_LABEL = { transfer: "转存" };
var state = { detail: { media: { re0_candidates: [] }, provider: "p" }, library: { media: "1" } };
var views = { detail: { selectProvider: function () {}, reveal: function () {}, openDetail: function () {} },
              transfer: { openLibrary: function () { opened++; throw new TypeError("Cannot set properties of null"); },
                          openCloud: function () { opened++; } } };
var api = { request: function () { unlocks++; return Promise.resolve(
  { link_public_id: "pub-1", media_id: "1", already_owned: false }); } };
window = { confirm: function () { return true; }, crypto: null };
var btn = { disabled: false };
views.detail.re0Action = function (candidate, btn) {""" + match.group(1) + """};
""" + body

    def test_a_failed_next_step_is_not_reported_as_a_failed_unlock(self):
        harness = self._harness("""
views.detail.re0Action({ id: 7, action: "transfer", provider: "115" }, btn);
setTimeout(function () {
  console.log(JSON.stringify({ unlocks: unlocks, opened: opened, toasts: toasts }));
}, 10);
""")
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out["unlocks"] == 1, "exactly one unlock request"
        assert out["opened"] == 1, "the next step was attempted"
        messages = [t["message"] for t in out["toasts"]]
        assert any("解锁成功" in m for m in messages), messages
        assert not any("解锁失败" in m for m in messages), messages
        assert any("不需要再次解锁" in m for m in messages), messages

    def test_an_already_materialised_candidate_never_unlocks_again(self):
        harness = self._harness("""
views.detail.re0Action({ id: 7, action: "transfer", provider: "115", resource_link_id: "pub-1" }, btn);
setTimeout(function () {
  console.log(JSON.stringify({ unlocks: unlocks, opened: opened, toasts: toasts }));
}, 10);
""")
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out["unlocks"] == 0, "an owned resource is never unlocked a second time"
        assert out["opened"] == 1
