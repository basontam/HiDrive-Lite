"""§4.3 of the resource-composition work order: the deterministic parser that
turns a share's remark / title / file names into "what is actually in here"
(整季 / 合集 / 单集 / 部分集数), plus the §4.1 display fields that feed it.

Every case here is a fixture: no RE0 call, no slug, no link."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import re0_sync  # noqa: E402


def parse(**kw):
    return re0_sync.parse_composition(**kw)


class TestDeclaredSeasonsAndEpisodes:
    @pytest.mark.parametrize("text,seasons,display", [
        ("S03 全季", [3], "S03"),
        ("s01", [1], "S01"),
        ("S01-S03 合集", [1, 2, 3], "S01-S03"),
        ("第 3 季 完结", [3], "S03"),
        ("第三季", [3], "S03"),
    ])
    def test_season_forms(self, text, seasons, display):
        out = parse(remark=text)
        assert out["season_numbers"] == seasons
        assert out["display"].startswith(display)

    @pytest.mark.parametrize("text,start,end,kind", [
        ("S01E01", 1, 1, "episode_single"),
        ("S01E01-E10", 1, 10, "episode_range"),
        ("第 1 集", 1, 1, "episode_single"),
        ("第 1-10 集", 1, 10, "episode_range"),
        ("第 1–3 集", 1, 3, "episode_range"),
        ("EP05", 5, 5, "episode_single"),
    ])
    def test_episode_forms(self, text, start, end, kind):
        out = parse(remark=text)
        assert (out["episode_start"], out["episode_end"]) == (start, end)
        assert out["kind"] == kind and out["confidence"] == "declared"

    def test_a_declared_count_is_not_a_range(self):
        out = parse(remark="4K高码，24集首更完结")
        assert out["episode_count"] == 24 and out["episode_start"] is None and out["episode_end"] is None
        assert out["completion"] == "complete" and out["confidence"] == "declared"
        assert out["display"] == "24集"

    def test_season_plus_count_reads_as_a_complete_season(self):
        out = parse(remark="S03 全24集 完结")
        assert out["kind"] == "season_complete" and out["season_numbers"] == [3]
        assert out["episode_count"] == 24 and out["display"] == "S03 · 24集"


class TestCompletionKeywords:
    @pytest.mark.parametrize("text,completion", [
        ("完结", "complete"),
        ("全集", "complete"),
        ("更新中", "updating"),
        ("连载中", "updating"),
        ("更新至第 5 集", "updating"),
        ("首更", "updating"),
        ("24集首更完结", "complete"),   # 完结 wins over 首更
        ("4K 高码", "unknown"),
    ])
    def test_completion(self, text, completion):
        assert parse(remark=text)["completion"] == completion

    def test_collection_without_season_or_episode(self):
        out = parse(remark="系列合集")
        assert out["kind"] == "collection" and out["display"] == "合集"

    def test_special_and_extra_markers_do_not_become_episodes(self):
        out = parse(remark="SP 特辑")
        assert out["kind"] == "unknown" and out["episode_start"] is None and out["display"] == "构成未说明"


class TestUnknownAndSafety:
    def test_nothing_parseable_says_so(self):
        for remark in (None, "", "   ", "高清版本", "4K 高码率"):
            out = parse(remark=remark)
            assert out["kind"] == "unknown" and out["confidence"] == "unknown"
            assert out["display"] == "构成未说明" and out["season_numbers"] == []

    def test_a_season_alone_is_shown_but_not_classified(self):
        # We know the season, not whether it is complete -- say the season, claim nothing else.
        out = parse(remark="S03")
        assert out["season_numbers"] == [3] and out["display"] == "S03"
        assert out["kind"] == "unknown" and out["completion"] == "unknown"

    def test_title_and_share_title_are_fallbacks_after_remark(self):
        assert parse(remark="S02E03", title="S05 全集")["season_numbers"] == [2]
        assert parse(remark=None, title="末日地堡 S05 全集")["season_numbers"] == [5]
        assert parse(remark=None, title=None, share_title="S07 完结")["season_numbers"] == [7]

    def test_absurd_numbers_are_ignored(self):
        out = parse(remark="S99999E123456")
        assert out["season_numbers"] == [] and out["episode_start"] is None and out["kind"] == "unknown"

    def test_parser_is_pure_and_deterministic(self):
        first = parse(remark="S01E01-E10 更新中")
        assert parse(remark="S01E01-E10 更新中") == first


class TestFileInference:
    def test_contiguous_episode_numbers_infer_a_range(self):
        names = [f"Silo.S03E{i:02d}.2160p.WEB-DL.mkv" for i in range(1, 11)]
        out = parse(remark=None, file_names=names, file_count=10)
        assert out["kind"] == "episode_range" and (out["episode_start"], out["episode_end"]) == (1, 10)
        assert out["season_numbers"] == [3] and out["episode_count"] == 10
        assert out["confidence"] == "file_inferred" and out["display"] == "S03 · 第 1–10 集"

    def test_a_file_count_alone_never_claims_a_full_season(self):
        out = parse(remark=None, file_names=["a.mkv", "b.mkv"], file_count=10)
        assert out["kind"] == "unknown" and out["episode_count"] is None and out["completion"] == "unknown"
        assert out["display"] == "构成未说明"

    def test_a_single_file_reads_as_one_episode_only_with_an_episode_number(self):
        assert parse(remark=None, file_names=["Silo.S03E07.mkv"], file_count=1)["kind"] == "episode_single"
        assert parse(remark=None, file_names=["Silo.2023.2160p.mkv"], file_count=1)["kind"] == "unknown"

    def test_a_declared_remark_beats_file_inference(self):
        names = [f"Silo.S03E{i:02d}.mkv" for i in range(1, 4)]
        out = parse(remark="S03 全24集 完结", file_names=names, file_count=3)
        assert out["confidence"] == "declared" and out["episode_count"] == 24 and out["kind"] == "season_complete"

    def test_gaps_in_file_numbering_stay_partial(self):
        out = parse(remark=None, file_names=["S01E01.mkv", "S01E02.mkv", "S01E09.mkv"], file_count=3)
        assert out["kind"] == "episode_range" and (out["episode_start"], out["episode_end"]) == (1, 9)
        assert out["episode_count"] == 3 and out["completion"] == "unknown"

    def test_non_video_files_are_ignored_for_inference(self):
        out = parse(remark=None, file_names=["cover.jpg", "S01E01.mkv", "S01E02.mkv", "readme.txt"], file_count=4)
        assert (out["episode_start"], out["episode_end"]) == (1, 2) and out["episode_count"] == 2


# ---------------------------------------------------------------------------
# 4.1: the display fields the parser and the detail row feed on. Everything
# is whitelisted, length-capped, control-character-cleaned and link-stripped.
# ---------------------------------------------------------------------------

FULL_ITEM = {
    "slug": "fixture-slug-comp", "pan_type": "115", "title": "末日地堡 (2023)", "share_size": "79.52GB",
    "video_resolution": ["4K"], "source": ["WEB-DL/WEBRip"], "subtitle_language": ["简中"], "subtitle_type": ["内封"],
    "remark": "4K高码，24集首更完结", "created_at": "2025-04-30T00:00:00+08:00", "unlock_points": 4,
    "is_unlocked": False, "validate_status": "valid", "validate_message": "", "last_validated_at": "2026-09-10T00:00:00+08:00",
    "is_official": False, "unlocked_users_count": 3, "user": {"nickname": "C", "id": 4242, "email": "x@y.z", "avatar": "https://re0.me/a.png"},
}


def _raw(item=None):
    return re0_sync.normalize_item(dict(FULL_ITEM, **(item or {})), salt="s")["spec"]["raw"]


class TestDisplayFieldCapture:
    def test_every_whitelisted_field_lands_in_the_spec(self):
        raw = _raw()
        assert raw["remark"] == "4K高码，24集首更完结"
        assert raw["created_at"] == "2025-04-29T16:00:00+00:00"   # normalised to a sortable UTC ISO string
        assert raw["last_validated_at"] == "2026-09-09T16:00:00+00:00"
        assert raw["publisher"] == {"nickname": "C", "avatar_url": None}   # never the id/email
        assert raw["is_official"] is False and raw["unlocked_users_count"] == 3
        assert raw["subtitle_language"] == ["简中"] and raw["subtitle_type"] == ["内封"]
        assert raw["title"] == "末日地堡 (2023)" and raw["share_size"] == "79.52GB"

    def test_the_publisher_never_carries_identity_or_an_avatar_link(self):
        blob = str(_raw())
        for needle in ("4242", "x@y.z", "re0.me/a.png"):
            assert needle not in blob, needle

    def test_a_remark_is_cleaned_capped_and_link_stripped(self):
        assert _raw({"remark": "第1集 \x1b[31m 有效"})["remark"] == "第1集 [31m 有效"
        assert _raw({"remark": "看这里 https://re0.me/r/leak 提取码 abcd"})["remark"] == "看这里 提取码 abcd"
        assert len(_raw({"remark": "长" * 5000})["remark"]) == 2000
        for bad in (None, "", "   ", 12, [], {}):
            assert _raw({"remark": bad})["remark"] is None

    def test_html_in_a_remark_is_kept_verbatim_for_the_renderer_to_escape(self):
        # Storage must not half-escape; static/app.js escapes at render time.
        assert _raw({"remark": "<b>4K</b> & 24集"})["remark"] == "<b>4K</b> & 24集"

    def test_missing_fields_stay_null_and_are_never_guessed(self):
        bare = re0_sync.normalize_item({"slug": "s-bare", "pan_type": "115", "is_unlocked": False}, salt="s")["spec"]["raw"]
        for key in ("remark", "created_at", "publisher", "last_validated_at", "is_official", "unlocked_users_count", "title"):
            assert bare[key] is None, key
        assert bare["subtitle_language"] == [] and bare["subtitle_type"] == []

    def test_an_unparseable_timestamp_becomes_null(self):
        assert _raw({"created_at": "昨天"})["created_at"] is None
        assert _raw({"created_at": 17_000_000})["created_at"] is None

    def test_a_publisher_given_as_a_plain_string_still_works(self):
        assert _raw({"user": "老王"})["publisher"] == {"nickname": "老王", "avatar_url": None}
        assert _raw({"user": {"name": "老李"}})["publisher"] == {"nickname": "老李", "avatar_url": None}
        assert _raw({"user": {}})["publisher"] is None

    def test_validate_message_is_captured_and_link_stripped(self):
        assert _raw({"validate_message": "分享已失效 https://re0.me/x"})["validate_message"] == "分享已失效"
        assert _raw({"validate_message": ""})["validate_message"] is None

    def test_the_unlocked_payload_still_never_reaches_the_spec(self):
        item = dict(FULL_ITEM, is_unlocked=True, url="https://115.com/s/swleak", access_code="ab12")
        out = re0_sync.normalize_item(item, salt="s")
        assert out["payload_url"] == "https://115.com/s/swleak" and out["payload_access_code"] == "ab12"
        blob = str(out["spec"])
        assert "115.com" not in blob and "ab12" not in blob and "fixture-slug-comp" not in blob


def test_guangya_is_labelled_guangya_wangpan():
    """Round 32: the pan is 光鸭网盘 (a Xunlei product); 广亚 was wrong."""
    import library_normalize, library_search
    assert library_normalize.PROVIDERS["guangya"] == "光鸭网盘"
    # Searching either spelling still finds it -- the old name stays an alias.
    aliases = dict(library_search._PROVIDER_ALIASES) if hasattr(library_search, "_PROVIDER_ALIASES") else None
    text = (ROOT / "library_search.py").read_text(encoding="utf-8")
    assert '("光鸭", "guangya")' in text and '("广亚", "guangya")' in text
