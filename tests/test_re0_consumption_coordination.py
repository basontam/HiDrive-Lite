"""F05: every path that can spend RE0 points, coordinated the same way.

Three entries can consume: the main resource unlock, the legacy
``/api/hdhive/unlock``, and the tv-follow pack. Before this round only the
first was coordinated. The others were reproducible failures:

* the pack released its lease on exactly one branch, so an upstream refusal
  left it held and the user's immediate retry got 409;
* the pack re-read only its own ``request_id`` record, so a request holding a
  stale snapshot bought a pack somebody else had already unlocked;
* the legacy entry went straight upstream with no lease at all, so two
  concurrent callers both spent.

Nothing here contacts RE0: the whole client is the ``http`` fixture, and no
test asserts that real points were ever charged.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import re0_sync  # noqa: E402
from test_re0_search_api import (  # noqa: E402,F401
    UNLOCK_URL, _resource_id, _tmdb_result, _unlock_payload, re0_env,
)

RE0_SEARCH = "/api/library/search/re0"
PACK_UNLOCK_URL = "https://re0.me/api/open/tv-follow/packs/pack-a/unlock"
PACK_ITEMS_URL = "https://re0.me/api/open/tv-follow/packs/pack-a/items"
PACKS_URL = "https://re0.me/api/open/tv-follow/packs"
HDHIVE_UNLOCK_URL = "https://re0.me/api/open/resources/unlock"


def _seed_resource(client, re0_env):
    client.get(RE0_SEARCH + "?q=本地片&type=all")
    return _resource_id(re0_env["store"], "115")


def _unlock_calls(http):
    return [c for c in http.calls if c["url"] == UNLOCK_URL]


def _pack_unlock_calls(http):
    return [c for c in http.calls if c["url"] == PACK_UNLOCK_URL]


def _leases(store) -> list[str]:
    conn = store.connect(readonly=True)
    try:
        return [row["lease_key"] for row in conn.execute("SELECT lease_key FROM re0_unlock_lease")]
    finally:
        conn.close()


def _slug_of(store, resource_id: int) -> str:
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT slug_ciphertext FROM re0_resource WHERE id=?", (resource_id,)).fetchone()
    finally:
        conn.close()
    return store.fernet.decrypt(row["slug_ciphertext"]).decode("utf-8")


# ---------------------------------------------------------------------------
# the follow pack
# ---------------------------------------------------------------------------


@pytest.fixture
def pack(client, re0_env, http, hidrive):
    """One tv-follow pack, discovered and not yet unlocked."""
    store = re0_env["store"]
    import library_store as ls

    tv_id = store.upsert_media(ls.MediaRecord(media_identity="tmdb:tv:70", media_type="tv",
                                              title_zh="追更剧", search_key="追更剧", year=2020, tmdb_id=70,
                                              match_status="exact"))
    http.route("GET", PACKS_URL, {"success": True, "data": {"items": [{
        "slug": "pack-a", "title": "追更包甲", "tv_id": "70", "tmdb_id": 70, "is_unlocked": False,
        "is_owner": False, "is_completed": False, "unlock_points": 30,
        "preview_items": [{"episode_label": "S02E10", "season": 2, "episode_start": 10, "episode_end": 10}],
        "latest_label": "S02E10", "item_count": 1}]}})
    assert client.post("/api/library/re0-follow/query", json={"media_id": tv_id}).status_code == 200
    body = client.get(f"/api/library/media/{tv_id}").get_json()
    ref = body["re0_follow"][0]["ref"]
    http.route("GET", PACK_ITEMS_URL, {"success": True, "data": {"items": [
        {"id": 501, "episode_label": "S02E10", "season": 2, "episode_start": 10, "episode_end": 10,
         "url": "https://pan.quark.cn/s/swfakepack10"}]}})
    return {"ref": ref, "tv_id": tv_id, "store": store}


def _unlock_pack(client, ref, request_id, **body):
    return client.post(f"/api/library/re0-follow/{ref}/unlock", json={"request_id": request_id, **body})


class TestThePackAlwaysGivesItsLeaseBack:
    def test_an_upstream_refusal_does_not_strand_the_lease(self, client, re0_env, http, pack, hidrive):
        """F05's measured case: the upstream failed, and the immediate retry got
        409 RE0_UNLOCK_IN_PROGRESS because the lease was never released.

        A 402 is used rather than a 502: G03.5 made an uncertain outcome its own
        recorded state, so a 5xx retry is now legitimately refused for a
        *different* reason. A refusal costs nothing and may be retried, which is
        what isolates the lease behaviour here.
        """
        http.route("POST", PACK_UNLOCK_URL, {"success": False, "code": "INSUFFICIENT_POINTS", "message": "积分不足"}, status=402)
        first = _unlock_pack(client, pack["ref"], "req-fail-1")
        assert first.status_code == 502, first.get_json()
        assert _leases(pack["store"]) == [], "the lease was not given back after an upstream refusal"

        second = _unlock_pack(client, pack["ref"], "req-fail-2")
        assert second.get_json().get("code") != "RE0_UNLOCK_IN_PROGRESS", second.get_json()
        assert len(_transport_calls(http, PACK_UNLOCK_URL)) == 2, "the retry never reached the upstream"

    def test_an_uncertain_outcome_also_gives_the_lease_back(self, client, re0_env, http, pack):
        http.route("POST", PACK_UNLOCK_URL, {"success": False, "message": "boom"}, status=502)
        assert _unlock_pack(client, pack["ref"], "req-unc-lease").status_code == 502
        assert _leases(pack["store"]) == [], "the lease was not given back after an uncertain outcome"

    def test_a_success_gives_it_back_too(self, client, re0_env, http, pack):
        http.route("POST", PACK_UNLOCK_URL, {"success": True, "data": {"already_owned": False, "points_cost": 30}})
        assert _unlock_pack(client, pack["ref"], "req-ok-1").status_code == 200
        assert _leases(pack["store"]) == []

    def test_an_exception_while_materialising_gives_it_back(self, client, re0_env, http, pack, monkeypatch, hidrive):
        http.route("POST", PACK_UNLOCK_URL, {"success": True, "data": {"already_owned": False, "points_cost": 30}})

        def explode(*args, **kwargs):
            raise RuntimeError("materialisation blew up")
        monkeypatch.setattr(hidrive.re0_sync, "fetch_pack_items", explode)
        with pytest.raises(RuntimeError):
            _unlock_pack(client, pack["ref"], "req-boom-1")
        assert _leases(pack["store"]) == [], "an exception must not leave the lease held"


class TestThePackRereadsInsideTheLease:
    def test_a_stale_snapshot_does_not_buy_it_twice(self, client, re0_env, http, pack, hidrive, monkeypatch):
        """F05: the entry's first read is a snapshot. Another request may have
        unlocked the pack between that read and this one taking the lease, so
        the in-lease re-read is what decides whether anything is bought.

        The staleness is injected exactly where a concurrent request would put
        it: the *first* `_pack_row` of the second request answers with the
        pre-unlock row, every later read with the truth.
        """
        http.route("POST", PACK_UNLOCK_URL, {"success": True, "data": {"already_owned": False, "points_cost": 30}})
        assert _unlock_pack(client, pack["ref"], "req-stale-a").status_code == 200
        assert len(_pack_unlock_calls(http)) == 1

        real_pack_row = hidrive.re0_sync._pack_row
        conn = pack["store"].connect(readonly=True)
        try:
            stale = dict(real_pack_row(conn, ref=pack["ref"]))
        finally:
            conn.close()
        stale["is_unlocked"] = 0
        reads: list[int] = []

        def snapshotting(conn, *, slug_hash_value=None, ref=None):
            # Only the route's own by-ref reads are snapshots; the by-hash
            # reads inside re0_sync are its own business.
            if ref is None:
                return real_pack_row(conn, slug_hash_value=slug_hash_value)
            reads.append(1)
            if len(reads) == 1:
                return stale
            return real_pack_row(conn, ref=ref)
        monkeypatch.setattr(hidrive.re0_sync, "_pack_row", snapshotting)

        second = _unlock_pack(client, pack["ref"], "req-stale-b")
        assert second.status_code == 200, second.get_json()
        assert len(reads) >= 2, "the in-lease re-read never happened"
        assert len(_pack_unlock_calls(http)) == 1, "a stale snapshot bought the pack a second time"
        body = second.get_json()
        assert body["already_owned"] is True and body["unlock_points"] == 0

    def test_an_already_unlocked_pack_only_syncs_items(self, client, re0_env, http, pack, hidrive):
        """F05: "已解锁但条目同步失败的重试只补同步" -- the pack is bought once;
        a retry after a failed item sync collects the items and buys nothing.

        The first item fetch fails with a 5xx rather than a 403 on purpose: a
        403 is RE0 saying "you may not read this at all", and the client
        blocks itself on it by design. A transient upstream failure is the
        case where a retry is the right thing to do.
        """
        http.route("POST", PACK_UNLOCK_URL, {"success": True, "data": {"already_owned": False, "points_cost": 30}})
        http.route("GET", PACK_ITEMS_URL, {"success": False, "message": "upstream down"}, status=502)
        first = _unlock_pack(client, pack["ref"], "req-items-1")
        assert first.status_code == 200, first.get_json()
        assert first.get_json()["materialized"] == 0
        assert len(_pack_unlock_calls(http)) == 1

        http.route("GET", PACK_ITEMS_URL, {"success": True, "data": {"items": [
            {"id": 501, "episode_label": "S02E10", "season": 2, "episode_start": 10, "episode_end": 10,
             "url": "https://pan.quark.cn/s/swfakepack10"}]}})
        retry = _unlock_pack(client, pack["ref"], "req-items-2")
        assert retry.status_code == 200, retry.get_json()
        body = retry.get_json()
        assert body["materialized"] == 1, body
        assert body["already_owned"] is True and body["unlock_points"] == 0
        assert len(_pack_unlock_calls(http)) == 1, "the retry bought the pack again"
        assert _leases(pack["store"]) == []


# ---------------------------------------------------------------------------
# the legacy entry
# ---------------------------------------------------------------------------


class TestTheLegacyEntryIsCoordinated:
    def test_it_takes_the_same_lease_key_as_the_main_entry(self, client, re0_env, http, hidrive):
        """F05: one resource, one key, whichever entry reaches it. A different
        key per entry would coordinate nothing."""
        resource_id = _seed_resource(client, re0_env)
        store = re0_env["store"]
        slug = _slug_of(store, resource_id)
        res, _slug_hash = hidrive._re0_resource_for_slug(store, slug)
        assert res is not None and int(res["id"]) == resource_id
        # Hold the main entry's key and watch the legacy entry refuse.
        held = re0_sync.resource_lease_key(resource_id)
        assert hidrive._re0_lease_take(store, held, holder="somebody-else", now=hidrive.utc_now())
        response = client.post("/api/hdhive/unlock", json={"slug": slug})
        assert response.status_code == 409, response.get_json()
        assert response.get_json()["code"] == "RE0_UNLOCK_IN_PROGRESS"
        assert not [c for c in http.calls if c["url"] == HDHIVE_UNLOCK_URL], "nothing may go upstream while held"

    def test_it_reuses_what_the_main_entry_materialised(self, client, re0_env, http, hidrive):
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeunlocked777", "cd34"))
        assert client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-main-1"}).status_code == 200
        before = len(_unlock_calls(http))
        response = client.post("/api/hdhive/unlock", json={"slug": slug})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["already_owned"] is True and body["unlock_points"] == 0 and body["link_public_id"]
        assert len(_unlock_calls(http)) == before, "the legacy entry paid for a resource already owned"

    def test_a_legacy_success_is_saved_so_the_next_caller_is_free(self, client, re0_env, http, hidrive):
        """F05: "legacy 成功也接入一致的保存/复用语义"."""
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakelegacy888", "ab12"))
        first = client.post("/api/hdhive/unlock", json={"slug": slug})
        assert first.status_code == 200, first.get_json()
        assert len(_unlock_calls(http)) == 1
        assert first.get_json().get("link_public_id"), first.get_json()

        # The main entry now finds it locally and spends nothing.
        main = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-after-legacy"})
        assert main.status_code == 200, main.get_json()
        assert main.get_json()["already_owned"] is True
        assert len(_unlock_calls(http)) == 1, "the main entry paid again for what legacy already bought"
        assert _leases(re0_env["store"]) == []

    def test_a_failed_save_still_returns_the_upstream_answer(self, client, re0_env, http, hidrive, monkeypatch):
        """The points are already spent. Reporting a local save failure as an
        unlock failure is what invites the user to pay twice."""
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakelegacy999", "ab12"))
        monkeypatch.setattr(hidrive.re0_sync, "materialize",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("cannot parse")))
        response = client.post("/api/hdhive/unlock", json={"slug": slug})
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["success"] is True
        assert _leases(re0_env["store"]) == []

    def test_an_unknown_slug_still_gets_a_lease_of_its_own(self, client, re0_env, http, hidrive):
        store = re0_env["store"]
        http.route("POST", UNLOCK_URL, {"success": True, "data": {"already_owned": False, "points_cost": 5}})
        _res, slug_hash = hidrive._re0_resource_for_slug(store, "slug-nobody-knows")
        assert hidrive._re0_lease_take(store, re0_sync.slug_lease_key(slug_hash),
                                       holder="somebody-else", now=hidrive.utc_now())
        response = client.post("/api/hdhive/unlock", json={"slug": "slug-nobody-knows"})
        assert response.status_code == 409
        assert not [c for c in http.calls if c["url"] == HDHIVE_UNLOCK_URL]

    def test_the_client_flag_still_cannot_buy_a_way_past_the_switch(self, client, re0_env, http, hidrive, monkeypatch):
        """R07 must survive F05: the gate comes before the lease, and no shape
        of the client's own flag reaches upstream."""
        monkeypatch.setattr(hidrive, "allow_member_re0_unlock", lambda: False)
        monkeypatch.setattr(hidrive, "_principal_is_admin", lambda: False)
        import auth_service as auth

        member = auth.CurrentUser(id=99, email="m@example.test", role="member", status="active")
        monkeypatch.setattr(hidrive, "current_user", lambda: member)
        for body in ({"slug": "fixture-slug-a"}, {"slug": "fixture-slug-a", "allow_points": False},
                     {"slug": "fixture-slug-a", "allow_points": "yes"},
                     {"slug": "fixture-slug-a", "allow_points": True}):
            response = client.post("/api/hdhive/unlock", json=body)
            assert response.status_code == 403, (body, response.get_json())
        assert not [c for c in http.calls if c["url"] == HDHIVE_UNLOCK_URL]
        assert _leases(re0_env["store"]) == [], "a refusal must not take a lease"


# ---------------------------------------------------------------------------
# the two entries, genuinely overlapping
# ---------------------------------------------------------------------------


class TestTheEntriesCrossWithoutPayingTwice:
    def test_the_main_and_legacy_entries_race_for_one_resource(self, client, re0_env, http, hidrive):
        """Real threads, real routes, separate clients, genuinely overlapping.

        The winner is held inside the upstream call until the loser has been
        answered, so the two requests are demonstrably in flight together.
        Whoever loses either reuses the winner's link or is told to retry --
        never a second unlock. Which of the two wins is not the point and is
        not asserted.
        """
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)

        from conftest import FakeResponse

        loser_answered = threading.Event()
        upstream: list[str] = []
        guard = threading.Lock()

        def slow_unlock(**kwargs):
            with guard:
                upstream.append("unlock")
            # Stay inside the upstream call until the other request has come
            # and gone. Bounded, so a scheduling surprise fails the assertions
            # rather than hanging the suite.
            loser_answered.wait(timeout=20)
            return FakeResponse(_unlock_payload("https://115.com/s/swfakerace111", "cd34"), 200)
        http.route("POST", UNLOCK_URL, handler=slow_unlock)

        outcomes: dict[str, tuple[int, dict]] = {}

        def call(name, fn):
            def run():
                try:
                    response = fn()
                    outcomes[name] = (response.status_code, response.get_json())
                finally:
                    loser_answered.set()
            return run

        def main_entry():
            own = hidrive.app.test_client()
            return own.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-race-main"})

        def legacy_entry():
            own = hidrive.app.test_client()
            return own.post("/api/hdhive/unlock", json={"slug": slug})

        threads = [threading.Thread(target=call("main", main_entry)),
                   threading.Thread(target=call("legacy", legacy_entry))]
        threads[0].start()
        threads[1].start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(thread.is_alive() for thread in threads), "a request hung"

        assert len(upstream) == 1, f"one resource, {len(upstream)} unlock requests"
        assert set(outcomes) == {"main", "legacy"}, outcomes
        winners = [name for name, (status, _body) in outcomes.items() if status == 200]
        assert winners, outcomes
        for name, (status, body) in outcomes.items():
            assert status in (200, 409), (name, status, body)
            if status == 409:
                assert body["code"] == "RE0_UNLOCK_IN_PROGRESS", (name, body)
            else:
                assert body.get("success") is True, (name, body)
        assert _leases(re0_env["store"]) == [], "a lease outlived the request that took it"

        # And afterwards the resource is materialised exactly once, so the
        # next caller through either entry is free.
        conn = re0_env["store"].connect(readonly=True)
        try:
            links = conn.execute("SELECT COUNT(*) AS c FROM re0_resource_link WHERE re0_resource_id=?",
                                 (resource_id,)).fetchone()["c"]
        finally:
            conn.close()
        assert links == 1, f"{links} local links for one resource"


# ---------------------------------------------------------------------------
# G03.1: a purchase is never repeated automatically
# ---------------------------------------------------------------------------


def _transport_calls(http, url):
    """What reached the mock transport -- not how often a route was called."""
    return [call for call in http.calls if call["url"] == url]


class TestAConsumingRequestIsSentOnce:
    """G03.1: the 5xx backoff did not distinguish a read from a purchase, so
    one unlock reached the transport three times. A 5xx, a timeout or an
    unparseable body says nothing about whether the purchase happened."""

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_a_resource_unlock_is_not_repeated_on_a_5xx(self, client, re0_env, http, hidrive, status):
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, {"success": False, "message": "boom"}, status=status)
        response = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                               json={"action": "transfer", "request_id": "req-5xx-once"})
        assert len(_transport_calls(http, UNLOCK_URL)) == 1, _transport_calls(http, UNLOCK_URL)
        assert response.status_code == 502
        assert response.get_json()["code"] == "RE0_UNLOCK_RESULT_UNKNOWN"

    def test_a_resource_unlock_is_not_repeated_on_a_timeout(self, client, re0_env, http, hidrive):
        import requests as requests_lib

        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, error=requests_lib.ConnectTimeout("slow"))
        response = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                               json={"action": "transfer", "request_id": "req-timeout-once"})
        assert len(_transport_calls(http, UNLOCK_URL)) == 1
        assert response.get_json()["code"] == "RE0_UNLOCK_RESULT_UNKNOWN"

    def test_a_resource_unlock_is_not_repeated_on_a_corrupt_body(self, client, re0_env, http, hidrive):
        from conftest import FakeResponse

        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, handler=lambda **kw: FakeResponse(raw=b"{not json"))
        response = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                               json={"action": "transfer", "request_id": "req-corrupt-once"})
        assert len(_transport_calls(http, UNLOCK_URL)) == 1
        assert response.get_json()["code"] == "RE0_UNLOCK_RESULT_UNKNOWN"

    def test_a_pack_unlock_is_not_repeated_on_a_5xx(self, client, re0_env, http, pack):
        http.route("POST", PACK_UNLOCK_URL, {"success": False, "message": "boom"}, status=502)
        response = _unlock_pack(client, pack["ref"], "req-pack-5xx")
        assert len(_transport_calls(http, PACK_UNLOCK_URL)) == 1, _transport_calls(http, PACK_UNLOCK_URL)
        assert response.status_code == 502
        assert response.get_json()["code"] == "RE0_UNLOCK_RESULT_UNKNOWN"
        assert _leases(pack["store"]) == []

    def test_an_unconfirmed_pack_result_is_not_bought_again(self, client, re0_env, http, pack):
        """G03.5: the pack entry honours "result unknown" exactly as the
        resource entry does -- otherwise the one uncertain outcome the pack
        could have was retried as a fresh purchase."""
        http.route("POST", PACK_UNLOCK_URL, {"success": False, "message": "boom"}, status=502)
        assert _unlock_pack(client, pack["ref"], "req-pack-unknown-1").status_code == 502
        assert len(_transport_calls(http, PACK_UNLOCK_URL)) == 1

        # Even with a working upstream, the next click must not send one.
        http.route("POST", PACK_UNLOCK_URL, {"success": True, "data": {"already_owned": False, "points_cost": 30}})
        again = _unlock_pack(client, pack["ref"], "req-pack-unknown-2")
        assert again.status_code == 409, again.get_json()
        assert again.get_json()["code"] == "RE0_UNLOCK_RESULT_UNKNOWN"
        assert len(_transport_calls(http, PACK_UNLOCK_URL)) == 1, "an unconfirmed pack result was re-sent"
        assert _leases(pack["store"]) == []

    def test_a_pack_refusal_is_not_an_unknown(self, client, re0_env, http, pack):
        http.route("POST", PACK_UNLOCK_URL, {"success": False, "code": "INSUFFICIENT_POINTS"}, status=402)
        first = _unlock_pack(client, pack["ref"], "req-pack-refused-1")
        assert first.status_code == 502 and first.get_json()["code"] != "RE0_UNLOCK_RESULT_UNKNOWN"
        http.route("POST", PACK_UNLOCK_URL, {"success": True, "data": {"already_owned": False, "points_cost": 30}})
        second = _unlock_pack(client, pack["ref"], "req-pack-refused-2")
        assert second.status_code == 200, second.get_json()
        assert len(_transport_calls(http, PACK_UNLOCK_URL)) == 2

    def test_a_read_only_query_keeps_its_backoff(self, client, re0_env, http, hidrive, monkeypatch):
        """The other half of G03.1: a GET is safe to retry and still is."""
        monkeypatch.setattr(hidrive.re0_sync.Re0Client, "_sleep", lambda self, seconds: None, raising=False)
        hidrive._re0_client_reset()
        client_obj = hidrive._re0_client()
        monkeypatch.setattr(client_obj, "_sleep", lambda seconds: None)
        url = "https://re0.me/api/open/resources"
        http.route("GET", url, {"success": False, "message": "later"}, status=503)
        result = client_obj.get("/api/open/resources", params={"tmdb_id": 1})
        assert result.ok is False and result.error_class == "upstream_5xx"
        assert len(_transport_calls(http, url)) == hidrive.re0_sync.MAX_5XX_ATTEMPTS, _transport_calls(http, url)

    def test_a_read_only_query_still_honours_retry_after(self, client, re0_env, http, hidrive):
        from conftest import FakeResponse

        hidrive._re0_client_reset()
        client_obj = hidrive._re0_client()
        url = "https://re0.me/api/open/resources"
        http.route("GET", url, handler=lambda **kw: FakeResponse(
            {"success": False, "message": "slow down"}, 429, headers={"Retry-After": "42"}))
        result = client_obj.get("/api/open/resources", params={"tmdb_id": 1})
        assert result.error_class == "rate_limited" and result.retry_after == 42
        assert len(_transport_calls(http, url)) == 1


# ---------------------------------------------------------------------------
# G03.3: confirmed-but-unsaved, and result-unknown, are states
# ---------------------------------------------------------------------------


def _resource_row(store, resource_id):
    conn = store.connect(readonly=True)
    try:
        return dict(conn.execute("SELECT * FROM re0_resource WHERE id=?", (resource_id,)).fetchone())
    finally:
        conn.close()


class TestASuccessThatCouldNotBeSaved:
    """G03.3: RE0 has already charged by the time the local save runs. When the
    save fails, the fact of the unlock has to survive -- otherwise the next
    click buys it again, which is what the reproduction showed."""

    def _break_materialize(self, hidrive, monkeypatch, failures=(1,)):
        real = hidrive.re0_sync.materialize
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] in failures:
                raise ValueError("cannot parse this link")
            return real(*args, **kwargs)
        monkeypatch.setattr(hidrive.re0_sync, "materialize", flaky)
        return calls

    def test_the_next_click_on_the_same_entry_does_not_buy_it_again(
            self, client, re0_env, http, hidrive, monkeypatch):
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakepending1", "cd34"))
        self._break_materialize(hidrive, monkeypatch)

        first = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-pending-1"})
        assert first.status_code == 502, first.get_json()
        assert first.get_json()["code"] == "RE0_UNLOCK_PENDING_SAVE"
        assert len(_transport_calls(http, UNLOCK_URL)) == 1
        row = _resource_row(re0_env["store"], resource_id)
        assert row["state"] == hidrive.re0_sync.STATE_PENDING_SAVE
        assert row["pending_payload_cipher"] is not None
        raw = re0_env["store"].connect(readonly=True).execute(
            "SELECT pending_payload_cipher FROM re0_resource WHERE id=?", (resource_id,)).fetchone()
        assert b"115.com" not in bytes(raw[0]), "the pending payload must be encrypted at rest"

        second = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                             json={"action": "transfer", "request_id": "req-pending-2"})
        assert second.status_code == 200, second.get_json()
        assert second.get_json()["already_owned"] is True
        assert len(_transport_calls(http, UNLOCK_URL)) == 1, "the second click bought it again"
        after = _resource_row(re0_env["store"], resource_id)
        assert after["pending_payload_cipher"] is None, "the pending copy must go once saved"

    def test_the_other_entry_resumes_it_too(self, client, re0_env, http, hidrive, monkeypatch):
        """G03.5: one resource, one protection, whichever entry arrives."""
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakepending2", "ab12"))
        self._break_materialize(hidrive, monkeypatch)

        assert client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-cross-1"}).status_code == 502
        assert len(_transport_calls(http, UNLOCK_URL)) == 1

        legacy = client.post("/api/hdhive/unlock", json={"slug": slug})
        assert legacy.status_code == 200, legacy.get_json()
        body = legacy.get_json()
        assert body["already_owned"] is True and body["link_public_id"]
        assert len(_transport_calls(http, UNLOCK_URL)) == 1, "the legacy entry bought it again"

    def test_a_legacy_success_that_cannot_be_saved_is_resumed_by_the_main_entry(
            self, client, re0_env, http, hidrive, monkeypatch):
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakepending3", "ef56"))
        self._break_materialize(hidrive, monkeypatch)

        legacy = client.post("/api/hdhive/unlock", json={"slug": slug})
        assert legacy.status_code == 200, legacy.get_json()
        assert "link_public_id" not in legacy.get_json(), legacy.get_json()
        assert len(_transport_calls(http, UNLOCK_URL)) == 1
        assert _resource_row(re0_env["store"], resource_id)["state"] == hidrive.re0_sync.STATE_PENDING_SAVE

        main = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-resume-main"})
        assert main.status_code == 200, main.get_json()
        assert main.get_json()["already_owned"] is True
        assert len(_transport_calls(http, UNLOCK_URL)) == 1

    def test_a_second_user_resuming_it_also_pays_nothing(self, client, re0_env, http, hidrive, monkeypatch):
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakepending4", "cd34"))
        self._break_materialize(hidrive, monkeypatch)
        assert client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-user-a"}).status_code == 502
        other = hidrive.app.test_client()
        second = other.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-user-b"})
        assert second.status_code == 200, second.get_json()
        assert len(_transport_calls(http, UNLOCK_URL)) == 1

    def test_the_same_request_id_replays_without_buying(self, client, re0_env, http, hidrive, monkeypatch):
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakepending5", "cd34"))
        self._break_materialize(hidrive, monkeypatch)
        assert client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-same-id"}).status_code == 502
        again = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-same-id"})
        assert again.status_code == 200, again.get_json()
        assert len(_transport_calls(http, UNLOCK_URL)) == 1


class TestAResultNobodyKnows:
    """G03.3: an unconfirmed outcome becomes a state, not another request."""

    def test_the_next_click_asks_the_user_not_re0(self, client, re0_env, http, hidrive):
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, {"success": False, "message": "gateway"}, status=502)
        first = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-unknown-1"})
        assert first.get_json()["code"] == "RE0_UNLOCK_RESULT_UNKNOWN"
        assert _resource_row(re0_env["store"], resource_id)["state"] == hidrive.re0_sync.STATE_RESULT_UNKNOWN

        # Even with a working upstream, the next click must not send one.
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeafterunknown", "cd34"))
        second = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                             json={"action": "transfer", "request_id": "req-unknown-2"})
        assert second.status_code == 409, second.get_json()
        assert second.get_json()["code"] == "RE0_UNLOCK_RESULT_UNKNOWN"
        assert len(_transport_calls(http, UNLOCK_URL)) == 1, "an unconfirmed result was re-sent"

    def test_the_legacy_entry_agrees(self, client, re0_env, http, hidrive):
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)
        http.route("POST", UNLOCK_URL, {"success": False, "message": "gateway"}, status=502)
        assert client.post("/api/hdhive/unlock", json={"slug": slug}).get_json()["code"] == "RE0_UNLOCK_RESULT_UNKNOWN"
        assert len(_transport_calls(http, UNLOCK_URL)) == 1
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakelegacyunknown", "cd34"))
        again = client.post("/api/hdhive/unlock", json={"slug": slug})
        assert again.status_code == 409 and again.get_json()["code"] == "RE0_UNLOCK_RESULT_UNKNOWN"
        assert len(_transport_calls(http, UNLOCK_URL)) == 1

    def test_a_refusal_is_not_an_unknown(self, client, re0_env, http, hidrive):
        """A 402 is RE0 saying no: nothing was spent, and the resource keeps
        its candidate state so a later click may legitimately try again."""
        resource_id = _seed_resource(client, re0_env)
        http.route("POST", UNLOCK_URL, {"success": False, "code": "INSUFFICIENT_POINTS"}, status=402)
        first = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-refused-1"})
        assert first.status_code == 502 and first.get_json()["code"] == "RE0_UPSTREAM_4XX"
        assert _resource_row(re0_env["store"], resource_id)["state"] != hidrive.re0_sync.STATE_RESULT_UNKNOWN
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeretryok", "cd34"))
        second = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                             json={"action": "transfer", "request_id": "req-refused-2"})
        assert second.status_code == 200, second.get_json()
        assert len(_transport_calls(http, UNLOCK_URL)) == 2


class TestTheLegacyConsentContractIsUnchanged:
    """Kept from the tests G03.2 replaced: the flag is still forwarded exactly
    as it was, and the audit still carries no link."""

    def test_the_default_does_not_forward_the_flag(self, client, re0_env, http, hidrive, audit_rows):
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeconsent1", "cd34"))
        assert client.post("/api/hdhive/unlock", json={"slug": slug, "allow_points": "yes"}).status_code == 200
        (call,) = _transport_calls(http, UNLOCK_URL)
        assert call["json"] == {"slug": slug}
        entry = audit_rows("hdhive.unlock")[-1]
        assert entry["status"] == "success" and "115.com" not in entry["detail"]

    def test_explicit_consent_is_forwarded(self, client, re0_env, http, hidrive, audit_rows):
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakeconsent2", "cd34"))
        assert client.post("/api/hdhive/unlock", json={"slug": slug, "allow_points": True}).status_code == 200
        assert _transport_calls(http, UNLOCK_URL)[0]["json"] == {"slug": slug, "allow_points": True}
        assert audit_rows("hdhive.unlock")[-1]["detail"] == "points unlock requested"

    def test_a_refusal_is_passed_through(self, client, re0_env, http, hidrive, audit_rows):
        resource_id = _seed_resource(client, re0_env)
        slug = _slug_of(re0_env["store"], resource_id)
        http.route("POST", UNLOCK_URL, {"success": False, "code": "INSUFFICIENT_POINTS", "message": "积分不足"}, status=402)
        response = client.post("/api/hdhive/unlock", json={"slug": slug})
        assert response.status_code == 402
        assert response.get_json()["code"] == "INSUFFICIENT_POINTS"
        assert [row for row in audit_rows("hdhive.unlock") if row["status"] == "success"] == []


class TestAMissingProjectionDoesNotForgetThePurchase:
    """Round 4: after an upstream success, a missing media projection returned
    409 *before* the pending record was written. Two calls: two unlock
    requests, zero pending records."""

    def _strip_media(self, store, resource_id):
        conn = store.connect()
        try:
            conn.execute("UPDATE re0_resource SET media_id=NULL, tmdb_id=424242 WHERE id=?", (resource_id,))
            conn.commit()
        finally:
            conn.close()

    def test_the_second_click_resumes_instead_of_buying_again(self, client, re0_env, http, hidrive, monkeypatch):
        resource_id = _seed_resource(client, re0_env)
        self._strip_media(re0_env["store"], resource_id)
        monkeypatch.setattr(hidrive.re0_sync, "create_media_from_projection", lambda *a, **k: None)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakenoproj1", "cd34"))

        first = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-noproj-1"})
        assert first.status_code == 409, first.get_json()
        assert first.get_json()["code"] == "RE0_PROJECTION_MISSING"
        assert len(_transport_calls(http, UNLOCK_URL)) == 1
        row = _resource_row(re0_env["store"], resource_id)
        assert row["state"] == hidrive.re0_sync.STATE_PENDING_SAVE, "the paid unlock was not recorded"
        assert row["pending_payload_cipher"] is not None

        second = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                             json={"action": "transfer", "request_id": "req-noproj-2"})
        assert second.status_code == 409 and second.get_json()["code"] == "RE0_PROJECTION_MISSING"
        assert len(_transport_calls(http, UNLOCK_URL)) == 1, "a missing projection bought the resource again"

    def test_once_the_projection_exists_the_pending_result_is_saved_for_free(self, client, re0_env, http, hidrive, monkeypatch):
        resource_id = _seed_resource(client, re0_env)
        self._strip_media(re0_env["store"], resource_id)
        monkeypatch.setattr(hidrive.re0_sync, "create_media_from_projection", lambda *a, **k: None)
        http.route("POST", UNLOCK_URL, _unlock_payload("https://115.com/s/swfakenoproj2", "cd34"))
        assert client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                           json={"action": "transfer", "request_id": "req-noproj-3"}).status_code == 409
        # The projection turns up (the media is local again).
        conn = re0_env["store"].connect()
        try:
            conn.execute("UPDATE re0_resource SET media_id=? WHERE id=?", (re0_env["media_id"], resource_id))
            conn.commit()
        finally:
            conn.close()
        third = client.post(f"/api/library/re0-resource/{resource_id}/unlock-and-action",
                            json={"action": "transfer", "request_id": "req-noproj-4"})
        assert third.status_code == 200, third.get_json()
        assert third.get_json()["already_owned"] is True
        assert len(_transport_calls(http, UNLOCK_URL)) == 1
        assert _resource_row(re0_env["store"], resource_id)["pending_payload_cipher"] is None
