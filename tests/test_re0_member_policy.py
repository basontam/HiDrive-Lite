"""Phase 7: who may spend RE0 points, and never twice for the same share.

Plan §10, §16.4. Two things are pinned here:

* a member reuses what is already materialised for free, and is refused a
  new unlock while the switch is off -- with zero upstream calls;
* two people clicking the same share at the same moment produce exactly one
  upstream unlock.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auth_service as auth  # noqa: E402
import re0_sync  # noqa: E402
# The RE0 environment (a seeded library, a fake RE0 HTTP layer and a TMDB
# session) already exists for the search-API suite; reuse it rather than
# building a second one that could drift.
from test_re0_search_api import UNLOCK_URL, re0_env, _resource_id, _unlock_payload  # noqa: E402,F401

MEMBER_PASSWORD = "Correct1Horse"
RE0_SEARCH = "/api/library/search/re0"


def _seed_resource(client, re0_env):
    """Discover one RE0 candidate and return its local id."""
    client.get(RE0_SEARCH + "?q=本地片&type=all")
    return _resource_id(re0_env["store"], "115")


def _unlock_calls(http):
    return [c for c in http.calls if c["url"] == UNLOCK_URL]


@pytest.fixture
def member(client, hidrive):
    client.post("/api/auth/register", json={
        "email": "member@example.test", "password": MEMBER_PASSWORD, "confirm_password": MEMBER_PASSWORD})
    with hidrive.connect_db() as db:
        admin_id = auth.ensure_admin_user(db, email=hidrive.ADMIN_EMAIL, now=1)
        row = db.execute("SELECT id FROM auth_user WHERE email_norm='member@example.test'").fetchone()
        auth.approve_user(db, int(row["id"]), approver_id=admin_id, now=2)
    assert client.post("/api/auth/login", json={
        "email": "member@example.test", "password": MEMBER_PASSWORD}).status_code == 200
    return int(row["id"])


# ---------------------------------------------------------------------------
# the lease, on its own
# ---------------------------------------------------------------------------


@pytest.fixture
def lease_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "media.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE re0_unlock_lease (
          lease_key TEXT PRIMARY KEY, holder TEXT NOT NULL,
          acquired_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
    """)
    return conn


class TestLease:
    def test_the_first_caller_takes_it_and_the_second_does_not(self, lease_db):
        assert re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="a", now=100) is True
        assert re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="b", now=100) is False

    def test_a_different_resource_is_never_blocked(self, lease_db):
        re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="a", now=100)
        assert re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(6), holder="b", now=100) is True

    def test_releasing_it_lets_the_next_caller_in(self, lease_db):
        re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="a", now=100)
        re0_sync.release_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="a")
        assert re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="b", now=100) is True

    def test_a_holder_cannot_release_somebody_elses(self, lease_db):
        re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="a", now=100)
        re0_sync.release_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="b")
        assert re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="c", now=100) is False

    def test_an_expired_lease_is_free_to_take(self, lease_db):
        """A worker killed mid-unlock must not block the resource forever."""
        re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="a", now=100)
        later = 100 + re0_sync.UNLOCK_LEASE_SECONDS + 1
        assert re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="b", now=later) is True

    def test_taking_over_an_expired_lease_makes_the_new_holder_the_owner(self, lease_db):
        re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="a", now=100)
        later = 100 + re0_sync.UNLOCK_LEASE_SECONDS + 1
        re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(5), holder="b", now=later)
        row = lease_db.execute("SELECT holder FROM re0_unlock_lease WHERE lease_key=?",
                               (re0_sync.resource_lease_key(5),)).fetchone()
        assert row["holder"] == "b"

    def test_only_one_of_many_simultaneous_callers_wins(self, lease_db):
        won = [re0_sync.acquire_unlock_lease(lease_db, re0_sync.resource_lease_key(9), holder=f"h{i}", now=100) for i in range(6)]
        assert won.count(True) == 1


# ---------------------------------------------------------------------------
# the policy, through the app
# ---------------------------------------------------------------------------


class TestMemberPolicy:
    def test_the_switch_is_off_by_default(self, hidrive):
        assert hidrive.allow_member_re0_unlock() is False

    def test_a_member_is_refused_a_new_unlock_while_it_is_off(self, client, hidrive, re0_env, http, member):
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        response = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                               json={"action": "transfer", "request_id": "req-p7-1"})
        assert response.status_code == 403
        assert response.get_json()["code"] == "MEMBER_RE0_UNLOCK_DISABLED"

    def test_that_refusal_costs_no_upstream_call_at_all(self, client, hidrive, re0_env, http, member):
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                    json={"action": "transfer", "request_id": "req-p7-2"})
        assert _unlock_calls(http) == []

    def test_the_administrator_is_never_governed_by_it(self, client, hidrive, re0_env, http):
        """The switch is about members spending the administrator's points."""
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        response = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                               json={"action": "transfer", "request_id": "req-p7-3"})
        assert response.status_code == 200
        assert len(_unlock_calls(http)) == 1

    def test_turning_it_on_lets_a_member_through(self, client, hidrive, re0_env, http, member):
        hidrive.setting_set("allow_member_re0_unlock", "1")
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        response = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                               json={"action": "transfer", "request_id": "req-p7-4"})
        assert response.status_code == 200, response.get_json()
        assert len(_unlock_calls(http)) == 1

    def test_a_member_reuses_what_is_already_here_for_free(self, client, hidrive, re0_env, http, member):
        """Plan §10.1 steps 2-3: an already materialised link costs nothing
        and is open to everybody, switch or no switch."""
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        # The administrator unlocks it once...
        hidrive.setting_set("allow_member_re0_unlock", "1")
        assert client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-p7-5"}).status_code == 200
        before = len(_unlock_calls(http))
        # ...and with the switch off again, a member still gets to use it.
        hidrive.setting_set("allow_member_re0_unlock", "0")
        response = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                               json={"action": "transfer", "request_id": "req-p7-6"})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["already_owned"] is True and body["unlock_points"] == 0
        assert len(_unlock_calls(http)) == before, "reuse must not call RE0 again"

    def test_two_requests_for_one_share_unlock_it_once(self, client, hidrive, re0_env, http, member):
        """§10.2: the lease, seen from the outside. The second request finds
        the link the first produced instead of buying it again."""
        hidrive.setting_set("allow_member_re0_unlock", "1")
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        first = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-p7-7"})
        second = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                             json={"action": "transfer", "request_id": "req-p7-8"})
        assert first.status_code == 200 and second.status_code == 200
        assert len(_unlock_calls(http)) == 1

    def test_a_held_lease_makes_the_next_caller_wait_rather_than_pay(self, client, hidrive, re0_env, http, member):
        hidrive.setting_set("allow_member_re0_unlock", "1")
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        # Somebody else is holding it and has produced nothing yet.
        conn = re0_env["store"].connect()
        try:
            re0_sync.acquire_unlock_lease(conn, re0_sync.resource_lease_key(resource_id),
                                          holder="someone-else", now=hidrive.utc_now())
            conn.commit()
        finally:
            conn.close()
        response = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                               json={"action": "transfer", "request_id": "req-p7-9"})
        assert response.status_code == 409
        assert response.get_json()["code"] == "RE0_UNLOCK_IN_PROGRESS"
        assert _unlock_calls(http) == []

    def test_the_lease_is_given_back_when_the_unlock_finishes(self, client, hidrive, re0_env, http, member):
        hidrive.setting_set("allow_member_re0_unlock", "1")
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                    json={"action": "transfer", "request_id": "req-p7-10"})
        conn = re0_env["store"].connect(readonly=True)
        try:
            held = conn.execute("SELECT COUNT(*) AS c FROM re0_unlock_lease").fetchone()["c"]
        finally:
            conn.close()
        assert held == 0

    def test_the_refusal_is_recorded_without_a_slug(self, client, hidrive, re0_env, http, member, audit_rows):
        resource_id = _seed_resource(client, re0_env)
        client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                    json={"action": "transfer", "request_id": "req-p7-11"})
        rows = audit_rows("re0.unlock")
        assert rows and rows[-1]["status"] == "refused"
        assert "member_policy" in rows[-1]["detail"]
        assert "fixture-slug" not in rows[-1]["detail"]


class TestEveryConsumingPath:
    """R07: the client's own flag is not an authorisation."""

    HDHIVE_UNLOCK = "https://re0.me/api/open/resources/unlock"

    def _hdhive_calls(self, http):
        return [c for c in http.calls if c["url"] == self.HDHIVE_UNLOCK]

    @pytest.mark.parametrize("body", [
        {"slug": "fixture-slug-a"},
        {"slug": "fixture-slug-a", "allow_points": False},
        {"slug": "fixture-slug-a", "allow_points": True},
        {"slug": "fixture-slug-a", "allow_points": "yes"},
    ])
    def test_no_shape_of_the_flag_gets_a_member_past_the_switch(self, client, hidrive, re0_env, http,
                                                                member, body):
        """Omitting the flag used to skip the gate entirely and still call
        upstream with the slug."""
        before = len(self._hdhive_calls(http))
        response = client.post("/api/hdhive/unlock", json=body)
        assert response.status_code == 403, body
        assert response.get_json()["code"] == "MEMBER_RE0_UNLOCK_DISABLED"
        assert len(self._hdhive_calls(http)) == before, "a refusal costs no upstream call"

    def test_an_already_materialised_slug_is_free_for_anybody(self, client, hidrive, re0_env, http, member):
        """§10.1 steps 2-3: what is already here costs nothing, switch or no
        switch -- and is answered without asking RE0."""
        hidrive.setting_set("allow_member_re0_unlock", "1")
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        assert client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-p7-legacy"}).status_code == 200
        hidrive.setting_set("allow_member_re0_unlock", "0")
        before = len(self._hdhive_calls(http))
        response = client.post("/api/hdhive/unlock", json={"slug": "fixture-slug-a"})
        assert response.status_code == 200
        body = response.get_json()
        assert body["already_owned"] is True and body["unlock_points"] == 0
        assert len(self._hdhive_calls(http)) == before

    def test_the_administrator_is_not_governed_by_the_switch_here_either(self, client, hidrive, re0_env, http):
        http.route("POST", self.HDHIVE_UNLOCK, {"success": True, "data": {}})
        response = client.post("/api/hdhive/unlock", json={"slug": "fixture-slug-z"})
        assert response.status_code != 403

    def test_the_follow_pack_answers_to_the_same_switch(self, client, hidrive, re0_env, member):
        response = client.post("/api/library/re0-follow/0123456789abcdef/unlock",
                               json={"request_id": "req-p7-follow"})
        # Either the pack is missing or the policy refused; what must never
        # happen is an unlock going through for a member with the switch off.
        assert response.status_code in {403, 404}


class TestPolicyValueSurvivesARefresh:
    """R09: the switch showed the administrator's own capability, which is
    always true, so it read false for the only person who can see it."""

    def test_me_reports_the_policy_itself(self, client, hidrive):
        assert client.get("/api/me").get_json()["allow_member_re0_unlock"] is False
        client.patch("/api/admin/policies/re0", json={"allow_member_re0_unlock": True})
        assert client.get("/api/me").get_json()["allow_member_re0_unlock"] is True

    def test_a_member_is_not_told_the_policy_value(self, client, hidrive, re0_env, member):
        assert "allow_member_re0_unlock" not in client.get("/api/me").get_json()

    def test_the_page_reads_the_policy_not_the_capability(self):
        import re

        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        account = re.search(r"views\.account = \{([\s\S]*?)\n  \};", js)
        assert account, "expected the views.account controller"
        assert "state.me.allow_member_re0_unlock" in account.group(1)
        assert 'capability("re0_unlock") && state.me.role' not in js

    def test_turning_it_on_then_reading_back_keeps_it_on(self, client, hidrive, re0_env, http, member):
        """The end of R09's acceptance: enable, refresh, a member can unlock."""
        client_admin = client
        # (the fixture client is the administrator until `member` logs in)
        hidrive.setting_set("allow_member_re0_unlock", "1")
        assert hidrive.allow_member_re0_unlock() is True
        resource_id = _seed_resource(client_admin, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        assert client_admin.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                                 json={"action": "transfer", "request_id": "req-p9-1"}).status_code == 200


class TestRealConcurrency:
    """R08: the previous tests called one after the other. These overlap.

    The scheduling Codex reproduced: both requests read "not materialised",
    the first takes the lease and unlocks, and the second then takes the
    freed lease -- and must find the link the first produced rather than buy
    a second one.
    """

    def test_the_second_holder_finds_what_the_first_produced(self, client, hidrive, re0_env, http):
        hidrive.setting_set("allow_member_re0_unlock", "1")
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        store = re0_env["store"]

        # Both read the world before either acts -- the state Codex's
        # scheduling starts from.
        conn = store.connect(readonly=True)
        try:
            assert re0_sync.materialized_link(conn, resource_id) is None
        finally:
            conn.close()

        first = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-race-1"})
        assert first.status_code == 200

        # The lease is free again by now; the second request takes it and
        # must re-read before spending.
        conn = store.connect(readonly=True)
        try:
            held = conn.execute("SELECT COUNT(*) AS c FROM re0_unlock_lease").fetchone()["c"]
        finally:
            conn.close()
        assert held == 0, "the first holder gave it back"

        second = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                             json={"action": "transfer", "request_id": "req-race-2"})
        assert second.status_code == 200
        assert second.get_json()["already_owned"] is True
        assert len(_unlock_calls(http)) == 1, "one share, one unlock, one charge"

    def test_overlapping_holders_produce_one_upstream_unlock(self, client, hidrive, re0_env, http):
        """Genuinely concurrent, driven through the real lease and re-read.

        The Flask test client is not safe to share across threads, so this
        exercises the decision itself: several workers racing for the same
        resource, each doing exactly what the route does -- take the lease,
        re-read the local result, and only then call upstream.
        """
        import threading

        hidrive.setting_set("allow_member_re0_unlock", "1")
        resource_id = _seed_resource(client, re0_env)
        store = re0_env["store"]
        lease_key = re0_sync.resource_lease_key(resource_id)
        upstream_calls = []
        materialised: list[str] = []
        seen_materialised = threading.Lock()

        def worker(index):
            conn = store.connect()
            try:
                got = re0_sync.acquire_unlock_lease(conn, lease_key, holder=f"w{index}",
                                                    now=hidrive.utc_now())
                conn.commit()
            finally:
                conn.close()
            if not got:
                return
            try:
                # The re-read R08 asks for: the previous holder may have just
                # finished.
                with seen_materialised:
                    already = bool(materialised)
                if already:
                    return
                upstream_calls.append(index)
                with seen_materialised:
                    materialised.append(f"link-{index}")
            finally:
                conn = store.connect()
                try:
                    re0_sync.release_unlock_lease(conn, lease_key, holder=f"w{index}")
                    conn.commit()
                finally:
                    conn.close()

        barrier = threading.Barrier(4)

        def run(index):
            barrier.wait()
            for _attempt in range(6):
                worker(index)
                with seen_materialised:
                    if materialised:
                        return
                time.sleep(0.01)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive(), "a worker did not finish"
        assert len(upstream_calls) == 1, f"one share, one unlock -- got {upstream_calls}"

    def test_the_lease_is_always_given_back(self, client, hidrive, re0_env, http):
        hidrive.setting_set("allow_member_re0_unlock", "1")
        resource_id = _seed_resource(client, re0_env)
        # An upstream failure must not leave the resource locked for a minute.
        http.route("POST", UNLOCK_URL, {"success": False, "message": "nope"}, status=502)
        client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                    json={"action": "transfer", "request_id": "req-fail-1"})
        conn = re0_env["store"].connect(readonly=True)
        try:
            assert conn.execute("SELECT COUNT(*) AS c FROM re0_unlock_lease").fetchone()["c"] == 0
        finally:
            conn.close()

    def test_a_pack_and_a_resource_never_share_a_lease(self):
        assert re0_sync.resource_lease_key(5) != re0_sync.pack_lease_key("5")
        assert re0_sync.pack_lease_key("abc") != re0_sync.pack_lease_key("abd")
