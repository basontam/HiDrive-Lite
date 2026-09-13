"""Phase 2: the login page's TMDB artwork, cached server-side.

Plan §11.2. Nothing here reaches the network -- every test injects a fake
session -- and no test asserts on an API key, because the key never leaves
the server and never enters a payload.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_tmdb as tmdb  # noqa: E402

NOW = 1_800_000_000


def test_wall_collects_24_distinct_posters_from_two_trending_pages(cache):
    first = _results(20)
    first['total_pages'] = 2
    second = _results(20)
    for index, item in enumerate(second['results']):
        item['poster_path'] = f'/nextposter{index}abcd.jpg'
    session = FakeSession(FakeResponse(payload=first), FakeResponse(payload=second))
    urls, source = tmdb.login_artwork(api_key='fake-key', read_cache=cache,
        write_cache=cache.write, session=session, now=NOW)
    assert source == 'fresh'
    assert len(urls) == len(set(urls)) == 24
    assert len(session.calls) == 2
    assert session.calls[1]['params']['page'] == 2
    assert all(c['url'].endswith('/trending/all/day') for c in session.calls)


def test_second_trending_page_failure_keeps_first_page(cache):
    first = _results(20)
    first['total_pages'] = 2
    session = FakeSession(FakeResponse(payload=first), OSError('fixture offline'))
    urls, source = tmdb.login_artwork(api_key='fake-key', read_cache=cache,
        write_cache=cache.write, session=session, now=NOW)
    assert source == 'fresh' and len(urls) == 20


@pytest.mark.parametrize('second', [503, 'malformed'])
def test_partial_refresh_preserves_a_complete_cached_wall(cache, second):
    old = [f'https://image.tmdb.org/t/p/w342/oldposter{i}abcd.jpg' for i in range(24)]
    cache.write(old, NOW - tmdb.LOGIN_ARTWORK_FRESH_SECONDS - 1)
    first = _results(20)
    first['total_pages'] = 2
    response = FakeResponse(status_code=503) if second == 503 else FakeResponse(payload={'error': 'fixture'})
    session = FakeSession(FakeResponse(payload=first), response)
    urls, source = tmdb.login_artwork(api_key='fake-key', read_cache=cache,
        write_cache=cache.write, session=session, now=NOW)
    assert source == 'stale' and urls == old
    assert cache()[0] == old


@pytest.fixture
def cache():
    """The caller-supplied store: `login_artwork` never chooses where its
    copy lives, which is what lets the login page work with no media-library
    bundle and no extra table."""
    box: dict = {"urls": None, "fetched_at": None}

    def read():
        return (box["urls"], box["fetched_at"]) if box["urls"] is not None else None

    def write(urls, now):
        box["urls"], box["fetched_at"] = list(urls), now

    read.write = write  # type: ignore[attr-defined]
    read.box = box  # type: ignore[attr-defined]
    return read


class FakeResponse:
    def __init__(self, status_code=200, payload=None, exc=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._exc = exc

    def json(self):
        if self._exc:
            raise self._exc
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        response = self._responses.pop(0) if self._responses else FakeResponse(500)
        if isinstance(response, Exception):
            raise response
        return response


def _results(count=6, *, media_type="movie", poster=True, backdrop=True):
    return {
        "results": [
            {
                "id": index,
                "media_type": media_type,
                "poster_path": f"/poster{index}abcd.jpg" if poster else None,
                "backdrop_path": f"/backdrop{index}abcd.jpg" if backdrop else None,
                "title": f"Fictional {index}",
            }
            for index in range(count)
        ]
    }


# ---------------------------------------------------------------------------
# image URLs
# ---------------------------------------------------------------------------


class TestImageUrl:
    def test_it_builds_an_official_url_for_an_allowlisted_size(self):
        assert tmdb.image_url("/abc123de.jpg", "w342") == "https://image.tmdb.org/t/p/w342/abc123de.jpg"

    @pytest.mark.parametrize("size", ["w9999", "original", "", "w342/../.."])
    def test_it_refuses_a_size_that_is_not_on_the_list(self, size):
        assert tmdb.image_url("/abc123de.jpg", size) is None

    @pytest.mark.parametrize("path", [
        "https://evil.test/x.jpg",
        "//evil.test/x.jpg",
        "/../../etc/passwd",
        "/abc.svg",
        "/a.jpg",
        "abc123de.jpg",
        "/abc 123.jpg",
        None,
        123,
    ])
    def test_it_refuses_anything_that_is_not_a_tmdb_path(self, path):
        assert tmdb.image_url(path, "w342") is None

    def test_every_url_it_builds_is_on_the_image_host(self):
        url = tmdb.image_url("/abc123de.jpg", "w500")
        assert url.startswith(tmdb.IMAGE_BASE_URL)
        assert "image.tmdb.org" in url


# ---------------------------------------------------------------------------
# turning a payload into collage items
# ---------------------------------------------------------------------------


class TestArtworkFromPayload:
    def test_it_keeps_films_and_series_that_have_a_poster(self):
        urls = tmdb.artwork_from_payload(_results(4)["results"], limit=10)
        assert len(urls) == 4
        assert all(url.startswith("https://image.tmdb.org/t/p/") for url in urls)

    def test_it_drops_anything_that_is_not_a_film_or_a_series(self):
        payload = _results(2)["results"] + _results(2, media_type="person")["results"]
        assert len(tmdb.artwork_from_payload(payload, limit=10)) == 2

    def test_it_drops_an_entry_with_no_usable_image(self):
        payload = _results(2)["results"] + _results(2, poster=False)["results"]
        assert len(tmdb.artwork_from_payload(payload, limit=10)) == 2

    def test_it_honours_the_limit(self):
        assert len(tmdb.artwork_from_payload(_results(30)["results"], limit=8)) == 8

    def test_it_survives_a_payload_that_is_not_what_was_promised(self):
        for payload in (None, [], [{}], [{"media_type": "movie"}], ["nonsense"], [None]):
            assert tmdb.artwork_from_payload(payload, limit=5) == []

    def test_it_returns_urls_only_never_titles_or_ids(self):
        urls = tmdb.artwork_from_payload(_results(3)["results"], limit=10)
        assert all(isinstance(url, str) for url in urls)
        assert not any("Fictional" in url for url in urls)


# ---------------------------------------------------------------------------
# fetching and caching
# ---------------------------------------------------------------------------


class TestLoginArtwork:
    def test_a_first_call_fetches_and_caches(self, cache):
        session = FakeSession(FakeResponse(200, _results(5)))
        urls, source = tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW)
        assert source == "fresh" and len(urls) == 5
        assert session.calls[0]["url"] == "https://api.themoviedb.org/3/trending/all/day"
        # The key travels as a parameter to TMDB and nowhere else.
        assert session.calls[0]["params"]["api_key"] == "fake-key"

    def test_a_second_call_inside_the_window_does_not_ask_again(self, cache):
        session = FakeSession(FakeResponse(200, _results(5)))
        tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW)
        urls, source = tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW + 3600)
        assert source == "cache" and len(urls) == 5
        assert len(session.calls) == 1

    def test_it_refreshes_once_the_window_has_passed(self, cache):
        session = FakeSession(FakeResponse(200, _results(5)), FakeResponse(200, _results(7)))
        tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW)
        urls, source = tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session,
                                          now=NOW + tmdb.LOGIN_ARTWORK_FRESH_SECONDS + 1)
        assert source == "fresh" and len(urls) == 7
        assert len(session.calls) == 2

    @pytest.mark.parametrize("failure", [
        FakeResponse(500),
        FakeResponse(429),
        FakeResponse(401),
        FakeResponse(200, exc=ValueError("not json")),
        RuntimeError("connection reset"),
    ])
    def test_a_failure_falls_back_to_the_stale_copy(self, cache, failure):
        session = FakeSession(FakeResponse(200, _results(5)), failure)
        tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW)
        later = NOW + tmdb.LOGIN_ARTWORK_FRESH_SECONDS + 10
        urls, source = tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=later)
        assert source == "stale" and len(urls) == 5

    def test_a_copy_older_than_a_week_is_not_used(self, cache):
        session = FakeSession(FakeResponse(200, _results(5)), FakeResponse(500))
        tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW)
        far_later = NOW + tmdb.LOGIN_ARTWORK_STALE_SECONDS + 10
        urls, source = tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=far_later)
        assert (urls, source) == ([], "unavailable")

    def test_with_no_cache_and_no_answer_it_simply_has_nothing(self, cache):
        session = FakeSession(FakeResponse(500))
        assert tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW) == ([], "unavailable")

    def test_without_a_key_it_never_calls_out_at_all(self, cache):
        session = FakeSession(FakeResponse(200, _results(5)))
        assert tmdb.login_artwork(api_key="", read_cache=cache, write_cache=cache.write, session=session, now=NOW) == ([], "unavailable")
        assert session.calls == []

    def test_an_empty_result_set_is_not_cached_as_if_it_were_good(self, cache):
        session = FakeSession(FakeResponse(200, {"results": []}), FakeResponse(200, _results(3)))
        first = tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW)
        assert first == ([], "unavailable")
        # The next call tries again rather than serving an empty collage for
        # six hours.
        urls, source = tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW + 60)
        assert source == "fresh" and len(urls) == 3

    def test_it_asks_for_the_language_it_was_given_and_times_out(self, cache):
        session = FakeSession(FakeResponse(200, _results(2)))
        tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write, session=session, now=NOW, language="en-US", timeout=4.0)
        assert session.calls[0]["params"]["language"] == "en-US"
        assert session.calls[0]["timeout"] == 4.0

    def test_what_gets_stored_is_urls_and_nothing_else(self, cache):
        session = FakeSession(FakeResponse(200, _results(3)))
        tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write,
                           session=session, now=NOW)
        stored = json.dumps(cache.box["urls"])
        assert "fake-key" not in stored
        assert "Fictional" not in stored
        assert stored.count("image.tmdb.org") == 3


# ---------------------------------------------------------------------------
# Review 2026-09-12 R20: an outage must not turn every visit into a request
# ---------------------------------------------------------------------------


@pytest.fixture
def cooldown():
    box = {"failed_at": None}

    def read():
        return box["failed_at"]

    def write(now):
        box["failed_at"] = now or None

    read.write = write  # type: ignore[attr-defined]
    read.box = box  # type: ignore[attr-defined]
    return read


def _call(cache, cooldown, session, now, api_key="fake-key"):
    return tmdb.login_artwork(api_key=api_key, read_cache=cache, write_cache=cache.write,
                              read_failure=cooldown, write_failure=cooldown.write,
                              session=session, now=now)


class TestFailureCooldown:
    def test_a_failure_stops_the_next_visit_asking_again(self, cache, cooldown):
        session = FakeSession(FakeResponse(500), FakeResponse(200, _results(3)))
        assert _call(cache, cooldown, session, NOW) == ([], "unavailable")
        assert _call(cache, cooldown, session, NOW + 60) == ([], "unavailable")
        assert len(session.calls) == 1, "the second visit is inside the cooldown"

    def test_it_asks_again_once_the_cooldown_is_over(self, cache, cooldown):
        session = FakeSession(FakeResponse(500), FakeResponse(200, _results(3)))
        _call(cache, cooldown, session, NOW)
        later = NOW + tmdb.LOGIN_ARTWORK_FAILURE_COOLDOWN_SECONDS + 1
        urls, source = _call(cache, cooldown, session, later)
        assert source == "fresh" and len(urls) == 3
        assert len(session.calls) == 2

    def test_during_the_cooldown_an_old_copy_is_still_served(self, cache, cooldown):
        session = FakeSession(FakeResponse(200, _results(4)), FakeResponse(500))
        _call(cache, cooldown, session, NOW)
        stale_time = NOW + tmdb.LOGIN_ARTWORK_FRESH_SECONDS + 10
        assert _call(cache, cooldown, session, stale_time)[1] == "stale"
        # Still stale, still no third request.
        assert _call(cache, cooldown, session, stale_time + 60)[1] == "stale"
        assert len(session.calls) == 2

    def test_a_success_clears_the_cooldown(self, cache, cooldown):
        session = FakeSession(FakeResponse(500), FakeResponse(200, _results(2)), FakeResponse(500))
        _call(cache, cooldown, session, NOW)
        recovered = NOW + tmdb.LOGIN_ARTWORK_FAILURE_COOLDOWN_SECONDS + 1
        assert _call(cache, cooldown, session, recovered)[1] == "fresh"
        assert cooldown.box["failed_at"] is None

    @pytest.mark.parametrize("failure", [
        FakeResponse(429), FakeResponse(500), RuntimeError("timeout"),
        FakeResponse(200, exc=ValueError("not json")),
    ])
    def test_every_kind_of_failure_starts_the_cooldown(self, cache, cooldown, failure):
        session = FakeSession(failure)
        _call(cache, cooldown, session, NOW)
        assert cooldown.box["failed_at"] == NOW

    def test_no_key_is_not_a_failure_to_cool_down_from(self, cache, cooldown):
        """Nothing was asked, so there is nothing to back off from."""
        session = FakeSession(FakeResponse(200, _results(2)))
        assert _call(cache, cooldown, session, NOW, api_key="") == ([], "unavailable")
        assert cooldown.box["failed_at"] is None
        assert session.calls == []

    def test_without_a_cooldown_store_the_behaviour_is_unchanged(self, cache):
        session = FakeSession(FakeResponse(500), FakeResponse(500))
        assert tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write,
                                  session=session, now=NOW) == ([], "unavailable")
        assert tmdb.login_artwork(api_key="fake-key", read_cache=cache, write_cache=cache.write,
                                  session=session, now=NOW + 1) == ([], "unavailable")
        assert len(session.calls) == 2


# ---------------------------------------------------------------------------
# Review 2026-09-12 F10: overlapping cold requests, and one staleness rule
# ---------------------------------------------------------------------------


class SlowSession:
    """Counts calls and holds each one open until released, so overlap is real
    rather than assumed."""

    def __init__(self, payload, *, gate: threading.Event, status=200):
        self._payload = payload
        self._status = status
        self._gate = gate
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def get(self, url, params=None, timeout=None):
        with self._lock:
            self.calls.append({"url": url})
        self._gate.wait(timeout=20)
        return FakeResponse(self._status, self._payload)


@pytest.fixture
def unique_key(request):
    """login_artwork's merge is keyed, and the locks are module-level: give
    every test its own key so one test's lock never serialises another's."""
    return f"test-{request.node.name}"


def _call_with_key(cache, cooldown, session, now, key, api_key="fake-key"):
    return tmdb.login_artwork(api_key=api_key, read_cache=cache, write_cache=cache.write,
                              read_failure=cooldown, write_failure=cooldown.write,
                              session=session, now=now, coalesce_key=key)


class TestOverlappingColdRequests:
    def test_six_at_once_make_one_upstream_request(self, cache, cooldown, unique_key):
        """F10: six genuinely concurrent visitors on a cold cache produced six
        requests. The first fetches; the rest wait and read what it wrote."""
        gate = threading.Event()
        session = SlowSession(_results(4), gate=gate)
        results: list[tuple] = []
        guard = threading.Lock()
        start = threading.Barrier(6, timeout=20)

        def visitor():
            start.wait()
            outcome = _call_with_key(cache, cooldown, session, NOW, unique_key)
            with guard:
                results.append(outcome)

        threads = [threading.Thread(target=visitor) for _ in range(6)]
        for thread in threads:
            thread.start()
        # Let them pile up on the lock, then let the one inside finish.
        time.sleep(0.4)
        gate.set()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)

        assert len(session.calls) == 1, f"six visitors, {len(session.calls)} upstream requests"
        assert len(results) == 6
        sources = {source for _urls, source in results}
        assert sources <= {"fresh", "cache"}, sources
        assert sum(1 for _urls, source in results if source == "fresh") == 1
        for urls, _source in results:
            assert urls and all(url.startswith("https://image.tmdb.org/") for url in urls)

    def test_six_at_once_during_an_outage_make_one_failed_request(self, cache, cooldown, unique_key):
        """The same merge on the failure path: one attempt, one cooldown, and
        everybody gets the same answer."""
        gate = threading.Event()
        session = SlowSession(None, gate=gate, status=500)
        results: list[tuple] = []
        guard = threading.Lock()
        start = threading.Barrier(6, timeout=20)

        def visitor():
            start.wait()
            outcome = _call_with_key(cache, cooldown, session, NOW, unique_key)
            with guard:
                results.append(outcome)

        threads = [threading.Thread(target=visitor) for _ in range(6)]
        for thread in threads:
            thread.start()
        time.sleep(0.4)
        gate.set()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)

        assert len(session.calls) == 1, f"one outage, {len(session.calls)} upstream requests"
        assert results == [([], "unavailable")] * 6, results
        assert cooldown.box["failed_at"] == NOW

    def test_a_later_visitor_inside_the_cooldown_still_asks_nothing(self, cache, cooldown, unique_key):
        session = FakeSession(FakeResponse(500))
        assert _call_with_key(cache, cooldown, session, NOW, unique_key) == ([], "unavailable")
        assert _call_with_key(cache, cooldown, session, NOW + 5, unique_key) == ([], "unavailable")
        assert len(session.calls) == 1

    def test_the_merge_does_not_serialise_two_different_caches(self, cache, cooldown):
        """Two independent keys must not queue behind one another."""
        gate = threading.Event()
        gate.set()
        first = SlowSession(_results(2), gate=gate)
        second = SlowSession(_results(2), gate=gate)
        assert _call_with_key(cache, cooldown, first, NOW, "key-one")[1] == "fresh"
        other_box: dict = {"urls": None, "fetched_at": None}

        def read():
            return (other_box["urls"], other_box["fetched_at"]) if other_box["urls"] is not None else None

        def write(urls, now):
            other_box["urls"], other_box["fetched_at"] = list(urls), now
        read.write = write  # type: ignore[attr-defined]
        assert tmdb.login_artwork(api_key="fake-key", read_cache=read, write_cache=write,
                                  read_failure=cooldown, write_failure=cooldown.write,
                                  session=second, now=NOW, coalesce_key="key-two")[1] == "fresh"
        assert len(first.calls) == 1 and len(second.calls) == 1


class TestOneStalenessRule:
    """F10: the cooldown branch served any copy at all, while the just-failed
    branch only served one under a week old. The same cache therefore answered
    `unavailable` on the first failure and `stale` a minute later."""

    def test_a_copy_older_than_a_week_is_never_served(self, cache, cooldown, unique_key):
        ancient = NOW - tmdb.LOGIN_ARTWORK_STALE_SECONDS - 1
        cache.write(["https://image.tmdb.org/t/p/w500/old.jpg"], ancient)
        session = FakeSession(FakeResponse(500), FakeResponse(500))
        first = _call_with_key(cache, cooldown, session, NOW, unique_key)
        assert first == ([], "unavailable"), first
        # A minute later, inside the cooldown: the same answer, not "stale".
        second = _call_with_key(cache, cooldown, session, NOW + 60, unique_key)
        assert second == first, (first, second)
        assert len(session.calls) == 1

    def test_a_copy_under_a_week_old_is_served_by_both_branches(self, cache, cooldown, unique_key):
        recent = NOW - tmdb.LOGIN_ARTWORK_STALE_SECONDS + 3600
        cache.write(["https://image.tmdb.org/t/p/w500/recent.jpg"], recent)
        session = FakeSession(FakeResponse(500))
        first = _call_with_key(cache, cooldown, session, NOW, unique_key)
        assert first[1] == "stale"
        second = _call_with_key(cache, cooldown, session, NOW + 60, unique_key)
        assert second == first
        assert len(session.calls) == 1

    def test_the_boundary_is_the_same_on_both_sides(self, cache, cooldown, unique_key):
        """Exactly at the limit, both branches agree -- whichever one answers."""
        at_limit = NOW - tmdb.LOGIN_ARTWORK_STALE_SECONDS
        cache.write(["https://image.tmdb.org/t/p/w500/edge.jpg"], at_limit)
        session = FakeSession(FakeResponse(500))
        assert _call_with_key(cache, cooldown, session, NOW, unique_key) == ([], "unavailable")
        assert _call_with_key(cache, cooldown, session, NOW + 1, unique_key) == ([], "unavailable")

    def test_a_429_behaves_like_any_other_failure(self, cache, cooldown, unique_key):
        # Past the fresh window, well inside the stale one.
        cache.write(["https://image.tmdb.org/t/p/w500/kept.jpg"],
                    NOW - tmdb.LOGIN_ARTWORK_FRESH_SECONDS - 60)
        session = FakeSession(FakeResponse(429))
        assert _call_with_key(cache, cooldown, session, NOW, unique_key)[1] == "stale"
        assert _call_with_key(cache, cooldown, session, NOW + 30, unique_key)[1] == "stale"
        assert len(session.calls) == 1

    def test_a_success_after_the_cooldown_clears_it(self, cache, cooldown, unique_key):
        session = FakeSession(FakeResponse(500), FakeResponse(200, _results(3)))
        assert _call_with_key(cache, cooldown, session, NOW, unique_key) == ([], "unavailable")
        later = NOW + tmdb.LOGIN_ARTWORK_FAILURE_COOLDOWN_SECONDS + 1
        urls, source = _call_with_key(cache, cooldown, session, later, unique_key)
        assert source == "fresh" and urls
        assert cooldown.box["failed_at"] is None
        assert len(session.calls) == 2
