"""Shared synthetic, installable media-library builder (T4/T5).

Extracted from ``tests/conftest.py`` so both the pytest fixture
(``installed_library``) and ``scripts/ui_screenshots.py`` (T5.8) build the
exact same fictional library data instead of duplicating the ~13
media/group/link fixture rows in two places.

Callers must have the repository root on ``sys.path`` before importing this
module (``conftest.py`` already does this for the test suite; standalone
scripts must do it themselves) so ``library_normalize``/``library_search``/
``library_store`` resolve.

All URLs/access codes below are fixtures, not real shares: hostnames use
real domain shapes but share codes are prefixed ``swfake``/``fake`` and
access codes are fixed 4-character placeholders, per the project's
test-data rule (see ``.superpowers/sdd/briefs/common.md``).
"""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import urlparse

import library_normalize
import library_search
import library_store


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def build_synthetic_library(store: library_store.LibraryStore) -> list[str]:
    """Populate ``store`` (schema already created) with >=6 media, >=3
    groups, one link per provider (10 providers), a deleted link, a 115
    link with a column access code, a second 115 link whose URL already
    embeds ``?password=``, a third 115 link whose URL embeds a *blank*
    ``?password=`` (T4 fix #5: must be treated as absent, not "already
    has one"), one ``unknown`` non-URL link (label ``INVALID_LINK_LABEL``,
    no actions -- T4 fix #3), and one ``unknown`` link with a real http
    URL of a host not in ``library_normalize.HOST_PROVIDERS`` (label
    ``未知来源 · <host>``, open+copy actions -- T4 fix #3). Returns the
    list of every plaintext URL/access-code value used, for leak
    assertions (``library_markers``)."""
    now = int(time.time())
    markers: list[str] = []

    media_specs = [
        # (identity, title, type, year, match_status, extra tmdb fields)
        ("tmdb:movie:900001", "虚构电影一", "movie", 2020, "exact", {
            "tmdb_id": 900001, "title_original": "Fake Movie One", "overview": "一部虚构的电影，用于测试。",
            "poster_path": "/poster1.jpg", "backdrop_path": "/backdrop1.jpg",
            "genres_json": json.dumps(["剧情"], ensure_ascii=False), "match_score": 0.95,
        }),
        ("fp:movie:900002", "虚构电影二", "movie", 2019, "unmatched", {}),
        ("fp:tv:900003", "虚构剧集一", "tv", 2021, "needs_review", {}),
        ("fp:tv:900004", "虚构剧集二", "tv", 2018, "unmatched", {}),
        ("fp:movie:900005", "虚构电影三", "movie", 2022, "unmatched", {}),
        ("fp:movie:900006", "虚构电影四", "movie", 2017, "candidate", {}),
    ]
    media_ids = {}
    for identity, title, media_type, year, match_status, extra in media_specs:
        media_ids[identity] = store.upsert_media(
            library_store.MediaRecord(
                media_identity=identity, media_type=media_type, title_zh=title,
                search_key=title, year=year, match_status=match_status, **extra,
            )
        )

    group_ids = {}
    for identity, quality, display_title, season in (
        ("tmdb:movie:900001", "2160p", "2160p WEB-DL · DV/HDR", None),
        ("fp:movie:900002", "1080p", "1080p WEB-DL", None),
        ("fp:tv:900003", "2160p", "S01 · 2160p WEB-DL", 1),
        ("fp:tv:900004", "1080p", "S01 · 1080p", 1),
        ("fp:movie:900005", "720p", "720p HDTV", None),
        ("fp:movie:900006", "other", "DVDRip", None),
    ):
        group_ids[identity] = store.upsert_group(
            library_store.GroupRecord(
                media_id=media_ids[identity], edition_fingerprint=f"fp-{identity}",
                display_title=display_title, quality=quality,
                season_from=season, season_to=season,
            )
        )

    # u1-backend T2 §1/§5/§8: a second real genre value (media #6, distinct
    # from media #1's "剧情") for genre-filter tests, and resource-group spec
    # fields (source_type/complete_season/video_codec/audio_summary/
    # subtitle_summary/tags/review_reason) on the two TV groups -- re-upserts
    # (same media_identity / edition_fingerprint) patch the existing rows in
    # place rather than adding new media/group/link rows, so every row-count
    # assertion elsewhere against this fixture (e.g. media_total == 6) stays
    # unchanged.
    store.upsert_media(
        library_store.MediaRecord(
            media_identity="fp:movie:900006", media_type="movie", title_zh="虚构电影四",
            search_key="虚构电影四", year=2017, match_status="candidate",
            genres_json=json.dumps(["动作"], ensure_ascii=False),
        )
    )
    store.upsert_group(
        library_store.GroupRecord(
            media_id=media_ids["fp:tv:900003"], edition_fingerprint="fp-fp:tv:900003",
            display_title="S01 · 2160p WEB-DL", quality="2160p", season_from=1, season_to=1,
            source_type="webdl", complete_season=1, video_codec="H.265",
            audio_summary="DTS-HD 5.1", subtitle_summary="中英双语",
            tags_json=json.dumps(["国语配音", "内封字幕"], ensure_ascii=False),
            needs_review=1, review_reason="title_alias_conflict,year_missing,bogus_unknown_code",
        )
    )
    store.upsert_group(
        library_store.GroupRecord(
            media_id=media_ids["fp:tv:900004"], edition_fingerprint="fp-fp:tv:900004",
            display_title="S01 · 1080p", quality="1080p", season_from=1, season_to=1,
            source_type="hdtv", complete_season=0,
        )
    )

    # (media identity, provider, url, access_code, deleted, remark)
    link_specs = [
        ("tmdb:movie:900001", "115", "https://115.com/s/swfake100001", "ab12", False, "S01 4K WEB-DL DV 内封简繁"),
        ("tmdb:movie:900001", "quark", "https://pan.quark.cn/s/swfake200002", None, False, "备用网盘"),
        ("fp:movie:900002", "tianyicloud", "https://cloud.189.cn/t/swfake300003", None, False, ""),
        ("fp:movie:900002", "alipan", "https://www.alipan.com/s/swfake400004", None, False, ""),
        ("fp:tv:900003", "baidu", "https://pan.baidu.com/s/swfake500005", None, True, "已失效"),
        ("fp:tv:900003", "guangya", "https://www.guangyapan.com/s/swfake600006", None, False, ""),
        ("fp:tv:900004", "139cloud", "https://yun.139.com/s/swfake700007", None, False, ""),
        ("fp:tv:900004", "123", "https://www.123pan.com/s/swfake800008", None, False, ""),
        ("fp:movie:900005", "ed2k", "ed2k://|file|fake.S01E01.2160p.mkv|1234567890|ABCDEF0123456789ABCDEF0123456789|/", None, False, ""),
        ("fp:movie:900005", "unknown", "群公告置顶：资源见钉钉，非直链", None, False, "非 URL 备注"),
        ("fp:movie:900006", "115", "https://115.com/s/swfake900001?password=cd34", "cd34", False, "URL 自带访问码"),
        ("fp:movie:900005", "unknown", "https://example-netdisk.com/s/swfake110011", None, False, "未在预置清单中的网盘"),
        ("fp:movie:900006", "115", "https://115.com/s/swfake120012?password=", "ef56", False, "URL 自带空访问码"),
    ]
    public_ids = []
    for index, (identity, provider, url, code, deleted, remark) in enumerate(link_specs, start=1):
        public_id = f"pub-fixture-{index:02d}"
        public_ids.append(public_id)
        markers.append(url)
        if code:
            markers.append(code)
        if provider == "unknown":
            # Mirror library_normalize._build_label exactly: real-URL
            # unknown links get "未知来源 · <host>" (open+copy actions);
            # non-URL text gets INVALID_LINK_LABEL (no actions) -- see T4
            # fix #3.
            host = urlparse(url).hostname
            label = f"未知来源 · {host}" if host else library_normalize.INVALID_LINK_LABEL
        else:
            label = f"{provider} 分享 · swf…{index}"
        store.upsert_link(
            library_store.LinkRecord(
                public_id=public_id,
                group_id=group_ids[identity],
                provider=provider,
                canonical_url_hash=_url_hash(url),
                url_label=label,
                url_plain=url,
                access_code_plain=code,
                has_access_code=1 if code else 0,
                remark=remark,
                created_at_source=now - index * 3600,
                deleted_at_source=now - 60 if deleted else None,
            )
        )

    store.recount()
    library_search.build_index(store)
    store.meta_set("normalize_version", "test-1")
    store.meta_set("built_at", str(now))
    store.meta_set("source_hashes", "{}")
    store.meta_set("encrypted", "0")
    return markers
