"""Tests for library_normalize.py (T1.1-T1.5).

Pure-function tests: no fixtures from tests/conftest.py are needed and no
network/DB access happens.  All share codes/access codes used here are
obviously fake (``swfake...`` style hosts/codes, 4-char access codes) per
.superpowers/sdd/briefs/common.md boundary rule #3.
"""

from __future__ import annotations

import pytest

import library_normalize as ln


# ---------------------------------------------------------------------------
# T1.1 normalize_text / search_key
# ---------------------------------------------------------------------------


def test_normalize_text_fullwidth_to_halfwidth():
    assert ln.normalize_text("Ａ　Ｂ－Ｃ") == "a b c"


def test_normalize_text_nfkc_compatibility_forms():
    # Ligature "ﬃ" (U+FB03) NFKC-decomposes to "ffi".
    assert ln.normalize_text("ﬃx") == "ffix"


def test_normalize_text_removes_zero_width_chars():
    assert ln.normalize_text("剧名​测试‌版﻿") == "剧名测试版"


def test_normalize_text_unifies_separators_to_space():
    assert ln.normalize_text("剧名：测试 · 版") == "剧名 测试 版"
    assert ln.normalize_text("A_B/C\\D|E") == "a b c d e"
    assert ln.normalize_text("【测试】(2024)") == "测试 2024"


def test_normalize_text_collapses_whitespace_and_strips():
    assert ln.normalize_text("  a   b\t\tc  ") == "a b c"


def test_normalize_text_is_idempotent():
    value = "  剧名​：测试 · 版　－　2024  "
    once = ln.normalize_text(value)
    twice = ln.normalize_text(once)
    assert once == twice


def test_search_key_keeps_only_letters_digits_cjk():
    assert ln.search_key("剧名：测试-2024！") == "剧名测试2024"
    assert ln.search_key("Fake Show (2023)") == "fakeshow2023"


# ---------------------------------------------------------------------------
# T1.2 parse_title
# ---------------------------------------------------------------------------


def test_parse_title_year_in_fullwidth_or_ascii_parens_with_space():
    info = ln.parse_title("示例剧 (2024)", "")
    assert info.title_zh == "示例剧"
    assert info.year == 2024


def test_parse_title_year_in_parens_no_space():
    info = ln.parse_title("示例剧(2024)", "")
    assert info.title_zh == "示例剧"
    assert info.year == 2024


def test_parse_title_empty_parens_gives_no_year():
    info = ln.parse_title("三体()", "")
    assert info.title_zh == "三体"
    assert info.year is None


def test_parse_title_alias_from_slash_split_and_media_title_wins():
    info = ln.parse_title("唐诡奇谭 / 唐朝诡事录 (2022)", "唐朝诡事录")
    assert info.title_zh == "唐朝诡事录"
    assert info.aliases == ("唐诡奇谭",)
    assert info.year == 2022


def test_parse_title_original_hint_from_latin_alias():
    info = ln.parse_title("示例剧 / Fake Title (2023)", "示例剧")
    assert info.aliases == ("Fake Title",)
    assert info.original_hint == "Fake Title"
    assert info.year == 2023


def test_parse_title_falls_back_to_title_cell_when_media_title_empty():
    info = ln.parse_title("示例剧 (2024)", "")
    assert info.title_zh == "示例剧"


def test_parse_title_year_out_of_range_is_none():
    # The year regex itself only recognises a 19xx/20xx prefix (per the
    # construction plan's own pattern), so "1899" (18xx) never matches the
    # year-paren pattern at all: the text is left untouched and year is None.
    info = ln.parse_title("示例剧 (1899)", "")
    assert info.title_zh == "示例剧 (1899)"
    assert info.year is None
    # "2099" does match the 20xx prefix but exceeds the 1900-2030 ceiling:
    # the pattern still strips the trailing year token, but year is None.
    info2 = ln.parse_title("示例剧 (2099)", "")
    assert info2.title_zh == "示例剧"
    assert info2.year is None


def test_parse_title_bare_trailing_year_no_parens():
    info = ln.parse_title("示例剧 2024", "")
    assert info.title_zh == "示例剧"
    assert info.year == 2024


def test_parse_title_bare_slash_in_title_is_not_split():
    # §4.2 alias delimiters are " / " (with surrounding whitespace), "／" and
    # "|" -- a bare "/" with no surrounding whitespace (e.g. inside "1/2"
    # style text) must not be treated as an alias separator.
    info = ln.parse_title("生死时速1/2 (2020)", "")
    assert info.title_zh == "生死时速1/2"
    assert info.aliases == ()


# ---------------------------------------------------------------------------
# T1.3 parse_edition
# ---------------------------------------------------------------------------


def test_parse_edition_season_range_and_single():
    assert ln.parse_edition("S01-S04", "").season_from == 1
    assert ln.parse_edition("S01-S04", "").season_to == 4
    single = ln.parse_edition("S01 1080p", "")
    assert (single.season_from, single.season_to) == (1, 1)


def test_parse_edition_season_word_and_di():
    assert (ln.parse_edition("Season 2", "").season_from, ln.parse_edition("Season 2", "").season_to) == (2, 2)
    di = ln.parse_edition("第3季", "")
    assert (di.season_from, di.season_to, di.complete_season) == (3, 3, False)


def test_parse_edition_chinese_numeral_season_complete():
    liang = ln.parse_edition("两季全", "")
    assert (liang.season_from, liang.season_to, liang.complete_season) == (1, 2, True)
    shi = ln.parse_edition("十季全", "")
    assert (shi.season_from, shi.season_to, shi.complete_season) == (1, 10, True)
    ten = ln.parse_edition("10季全", "")
    assert (ten.season_from, ten.season_to, ten.complete_season) == (1, 10, True)


def test_parse_edition_season_quan_without_number():
    quan = ln.parse_edition("全季", "")
    assert (quan.season_from, quan.season_to, quan.complete_season) == (None, None, True)


def test_parse_edition_episode_se_range():
    e = ln.parse_edition("S01E01 - E20", "")
    assert (e.episode_from, e.episode_to) == (1, 20)


def test_parse_edition_episode_di_and_complete_and_single_tag():
    di = ln.parse_edition("第5集", "")
    assert (di.episode_from, di.episode_to) == (5, 5)
    quan_n = ln.parse_edition("20集全", "")
    assert (quan_n.episode_from, quan_n.episode_to, quan_n.complete_season) == (1, 20, True)
    quan = ln.parse_edition("全集", "")
    assert (quan.episode_from, quan.episode_to, quan.complete_season) == (None, None, True)
    single = ln.parse_edition("单集", "")
    assert single.tags == ("single",)


def test_parse_edition_quality_values():
    assert ln.parse_edition("4K", "").quality == "2160p"
    assert ln.parse_edition("UHD", "").quality == "2160p"
    assert ln.parse_edition("1080p", "").quality == "1080p"
    assert ln.parse_edition("1080i", "").quality == "1080p"
    assert ln.parse_edition("720p", "").quality == "720p"
    assert ln.parse_edition("540p", "").quality == "other"


def test_parse_edition_source_type_values():
    assert ln.parse_edition("REMUX", "").source_type == "remux"
    assert ln.parse_edition("原盘", "").source_type == "bluray"
    assert ln.parse_edition("BluRay", "").source_type == "bluray"
    assert ln.parse_edition("BDRip", "").source_type == "bdrip"
    assert ln.parse_edition("WEB-DL", "").source_type == "webdl"
    assert ln.parse_edition("Netflix", "").source_type == "webdl"
    assert ln.parse_edition("HDTV", "").source_type == "hdtv"


def test_parse_edition_source_type_remux_beats_bluray():
    e = ln.parse_edition("REMUX BluRay 2160p", "")
    assert e.source_type == "remux"


def test_parse_edition_hdr_values_and_specificity():
    assert ln.parse_edition("DV&HDR", "").hdr == "dv_hdr"
    assert ln.parse_edition("杜比视界 HDR", "").hdr == "dv_hdr"
    assert ln.parse_edition("DV", "").hdr == "dv"
    assert ln.parse_edition("Dolby Vision", "").hdr == "dv"
    assert ln.parse_edition("HDR10+", "").hdr == "hdr10plus"
    assert ln.parse_edition("HLG", "").hdr == "hlg"
    assert ln.parse_edition("HDR10", "").hdr == "hdr10"
    assert ln.parse_edition("SDR", "").hdr == "sdr"


def test_parse_edition_hdr_dv_and_hdr_combo_is_dv_hdr_not_dv():
    e = ln.parse_edition("DV&HDR", "")
    assert e.hdr == "dv_hdr"
    assert e.hdr != "dv"


def test_parse_edition_codec_values():
    assert ln.parse_edition("HEVC", "").video_codec == "hevc"
    assert ln.parse_edition("x265", "").video_codec == "hevc"
    assert ln.parse_edition("AVC", "").video_codec == "avc"
    assert ln.parse_edition("x264", "").video_codec == "avc"
    assert ln.parse_edition("AV1", "").video_codec == "av1"


def test_parse_edition_audio_normalisation():
    e = ln.parse_edition(
        "DDP5.1 DTS-HD MA 7.1 TrueHD Atmos 杜比音效 AAC FLAC 国英音轨 国粤音轨 国粤英音轨 粤语 英语 双语音轨",
        "",
    )
    assert e.audio == (
        "aac",
        "atmos",
        "cantonese",
        "ddp5.1",
        "dolby_audio",
        "dtshdma7.1",
        "dual",
        "english",
        "flac",
        "mandarin+cantonese",
        "mandarin+cantonese+english",
        "mandarin+english",
        "truehd",
    )
    assert ln.parse_edition("DD5.1", "").audio == ("dd5.1",)
    assert ln.parse_edition("国配", "").audio == ("mandarin",)
    assert ln.parse_edition("国语", "").audio == ("mandarin",)
    assert ln.parse_edition("DTS", "").audio == ("dts",)


def test_parse_edition_subtitle_normalisation():
    assert ln.parse_edition("内封简繁", "").subtitle == ("embedded:zh-hans+zh-hant",)
    assert ln.parse_edition("内封简中", "").subtitle == ("embedded:zh-hans",)
    assert ln.parse_edition("内封繁中", "").subtitle == ("embedded:zh-hant",)
    assert ln.parse_edition("外挂简中", "").subtitle == ("external:zh-hans",)
    assert ln.parse_edition("内封AI简繁", "").subtitle == ("embedded:ai:zh-hans+zh-hant",)
    assert ln.parse_edition("简/繁", "").subtitle == ("zh-hans+zh-hant",)
    assert ln.parse_edition("中字", "").subtitle == ("zh",)


def test_parse_edition_tags():
    e = ln.parse_edition("豆瓣8.5 高码 60帧 50FPS 补 洗版 自购 仅秒传", "")
    assert e.tags == (
        "50fps",
        "60fps",
        "douban:8.5",
        "highbitrate",
        "instant_only",
        "purchased",
        "resend",
        "rewash",
    )
    assert ln.parse_edition("HQ", "").tags == ("hq",)


def test_parse_edition_src_platform_tags_do_not_affect_other_fields():
    assert ln.parse_edition("Netflix", "").tags == ("src:netflix",)
    assert ln.parse_edition("HiveWeb", "").tags == ("src:hive",)
    assert ln.parse_edition("咪咕", "").tags == ("src:migu",)


def test_parse_edition_combined_example():
    e = ln.parse_edition("S01E01 - E20 4K DV&HDR 内封简繁 HiveWeb 仅秒传", "")
    assert (e.season_from, e.season_to) == (1, 1)
    assert (e.episode_from, e.episode_to) == (1, 20)
    assert e.quality == "2160p"
    assert e.hdr == "dv_hdr"
    assert e.subtitle == ("embedded:zh-hans+zh-hant",)
    assert e.source_type == "webdl"
    assert "instant_only" in e.tags
    assert e.complete_season is False


def test_parse_edition_bracket_tokens_split_apart():
    e = ln.parse_edition("【BluRay.1080P】【内封简英双语特效字幕】【51.4GB】", "")
    assert e.quality == "1080p"
    assert e.source_type == "bluray"
    assert e.subtitle == ("dual:zh-hans+en", "effects", "embedded:zh-hans+en")


def test_parse_edition_empty_remark_is_all_none_and_not_unparsed():
    e = ln.parse_edition("", "")
    assert e == ln.EditionInfo(
        season_from=None, season_to=None, episode_from=None, episode_to=None,
        complete_season=False, quality=None, source_type=None, hdr=None,
        video_codec=None, audio=(), subtitle=(), tags=(), unparsed=False,
    )


def test_parse_edition_gibberish_remark_is_unparsed():
    e = ln.parse_edition("随便写的备注", "")
    assert e.unparsed is True
    assert e.season_from is None and e.quality is None and e.tags == ()


def test_parse_edition_merges_ed2k_filename_remark_wins():
    e = ln.parse_edition("", "", ed2k_filename="Fake.Show.S01E05.2160p.WEB-DL.x265")
    assert (e.season_from, e.season_to) == (1, 1)
    assert (e.episode_from, e.episode_to) == (5, 5)
    assert e.quality == "2160p"
    assert e.source_type == "webdl"
    assert e.video_codec == "hevc"
    assert e.unparsed is False

    # Remark takes priority over the ED2K filename when both recognise a
    # value for the same field.
    e2 = ln.parse_edition("1080p", "", ed2k_filename="Fake.Show.S01E05.2160p.WEB-DL.x265")
    assert e2.quality == "1080p"


def test_parse_edition_title_cell_also_contributes():
    e = ln.parse_edition("1080p", "第2季")
    assert (e.season_from, e.season_to) == (2, 2)
    assert e.quality == "1080p"


# ---------------------------------------------------------------------------
# T1.4 parse_link / PROVIDERS / HOST_PROVIDERS / canonical_hash / public_id
# ---------------------------------------------------------------------------


def test_providers_and_host_providers_tables():
    # T8 §3: PROVIDERS is the one shared vocabulary with the frontend's
    # PROVIDER_LABEL map (static/app.js) -- these exact labels must match.
    assert ln.PROVIDERS["115"] == "115 网盘"
    assert ln.PROVIDERS["quark"] == "夸克网盘"
    assert ln.PROVIDERS["alipan"] == "阿里云盘"
    assert ln.PROVIDERS["baidu"] == "百度网盘"
    assert ln.PROVIDERS["tianyicloud"] == "天翼云盘"
    assert ln.PROVIDERS["guangya"] == "广亚"
    assert ln.PROVIDERS["139cloud"] == "移动云盘"
    assert ln.PROVIDERS["123"] == "123 云盘"
    assert ln.PROVIDERS["ed2k"] == "ED2K"
    assert ln.PROVIDERS["unknown"] == "其他"
    assert ln.HOST_PROVIDERS["115.com"] == "115"
    assert ln.HOST_PROVIDERS["pan.baidu.com"] == "baidu"


def test_parse_link_115_host_aliases_and_path_forms():
    # 115 has multiple real host aliases per §4.4; the /s/<code> and bare
    # /<code> path forms on different hosts must canonicalise identically.
    a = ln.parse_link("https://115.com/s/swfake115a", None)
    b = ln.parse_link("https://115cdn.com/swfake115a", None)
    c = ln.parse_link("https://share.115.com/s/swfake115a", None)
    d = ln.parse_link("https://anxia.com/swfake115a", None)
    assert a.provider == b.provider == c.provider == d.provider == "115"
    assert a.canonical == b.canonical == c.canonical == d.canonical == "115:swfake115a"


def test_parse_link_quark_host():
    # §4.4 lists only one quark host; a "www." variant is a distinct alias
    # string that must normalise to the same host/provider/canonical.
    a = ln.parse_link("https://pan.quark.cn/s/swfakeqk1", None)
    b = ln.parse_link("https://www.pan.quark.cn/s/swfakeqk1", None)
    assert a.provider == b.provider == "quark"
    assert a.canonical == b.canonical == "quark:swfakeqk1"


def test_parse_link_alipan_host_aliases():
    a = ln.parse_link("https://alipan.com/s/swfakeal1", None)
    b = ln.parse_link("https://aliyundrive.com/s/swfakeal1", None)
    assert a.provider == b.provider == "alipan"
    assert a.canonical == b.canonical == "alipan:swfakeal1"


def test_parse_link_alipan_folder_makes_a_different_canonical():
    root = ln.parse_link("https://alipan.com/s/swfakeal2", None)
    folder = ln.parse_link("https://alipan.com/s/swfakeal2/folder/fakefid", None)
    assert root.canonical != folder.canonical


def test_parse_link_baidu_host_aliases_and_two_url_forms_same_canonical():
    a = ln.parse_link("https://pan.baidu.com/s/1swfakebd1", None)
    b = ln.parse_link("https://yun.baidu.com/share/init?surl=swfakebd1", None)
    assert a.provider == b.provider == "baidu"
    assert a.canonical == b.canonical == "baidu:swfakebd1"


def test_parse_link_tianyicloud_host_aliases():
    # Unlike 115/quark/baidu, tianyicloud's canonical format
    # ("tianyicloud:<host><path>?<query>") includes the host itself, so two
    # different real hosts for the same share are recognised as the same
    # provider but (correctly, per §4.4) canonicalise differently.
    a = ln.parse_link("https://cloud.189.cn/t/swfakety1", None)
    b = ln.parse_link("https://h5.cloud.189.cn/t/swfakety1", None)
    assert a.provider == b.provider == "tianyicloud"
    assert a.canonical != b.canonical
    same_host = ln.parse_link("https://cloud.189.cn/t/swfakety1", None)
    assert a.canonical == same_host.canonical


def test_parse_link_guangya_host():
    a = ln.parse_link("https://guangyapan.com/s/swfakegy1", None)
    b = ln.parse_link("https://www.guangyapan.com/s/swfakegy1", None)
    assert a.provider == b.provider == "guangya"
    assert a.canonical == b.canonical


def test_parse_link_139cloud_host_aliases():
    a = ln.parse_link("https://caiyun.139.com/s/swfake1391", None)
    b = ln.parse_link("https://yun.139.com/s/swfake1391", None)
    assert a.provider == b.provider == "139cloud"
    assert a.canonical == b.canonical


def test_parse_link_123_host_aliases():
    a = ln.parse_link("https://123pan.com/s/swfake1231", None)
    b = ln.parse_link("https://123684.com/s/swfake1231", None)
    assert a.provider == b.provider == "123"
    assert a.canonical == b.canonical


def test_parse_link_access_code_four_sources():
    # 1) URL query "password"
    a = ln.parse_link("https://115.com/s/swfakeac1?password=ab12", None)
    assert a.access_code == "ab12"
    # 2) URL query "pwd"
    b = ln.parse_link("https://pan.quark.cn/s/swfakeac2?pwd=cd34", None)
    assert b.access_code == "cd34"
    # 3) URL query "accessCode" (case-insensitive)
    c = ln.parse_link("https://cloud.189.cn/t/swfakeac3?ACCESSCODE=ef56", None)
    assert c.access_code == "ef56"
    # 4) column cell value only (no URL param)
    d = ln.parse_link("https://115.com/s/swfakeac4", "gh78")
    assert d.access_code == "gh78"


def test_parse_link_access_code_conflict_prefers_url_value():
    li = ln.parse_link("https://115.com/s/swfakeac5?password=ab12", "cd34")
    assert li.access_code == "ab12"
    assert li.code_conflict is True


def test_parse_link_access_code_no_conflict_when_cell_matches_or_empty():
    same = ln.parse_link("https://115.com/s/swfakeac6?password=ab12", "ab12")
    assert same.code_conflict is False
    empty_cell = ln.parse_link("https://115.com/s/swfakeac7?password=ab12", "NULL")
    assert empty_cell.code_conflict is False
    assert empty_cell.access_code == "ab12"


def test_parse_link_canonical_never_contains_access_code_for_all_url_providers():
    # Every URL-type provider's canonical must never embed an access code
    # passed as a URL query parameter (password/pwd/accessCode) -- including
    # guangya/139cloud/123, whose canonical embeds a sorted query string for
    # other (non-access-code) parameters.
    cases = (
        ("https://115.com/s/swfakeallp1?password=ab12", "ab12"),
        ("https://pan.quark.cn/s/swfakeallp2?pwd=ab12", "ab12"),
        ("https://alipan.com/s/swfakeallp3?password=ab12", "ab12"),
        ("https://pan.baidu.com/s/1swfakeallp4?password=ab12", "ab12"),
        ("https://cloud.189.cn/t/swfakeallp5?accessCode=ab12", "ab12"),
        ("https://guangyapan.com/s/swfakeallp6?password=ab12", "ab12"),
        ("https://caiyun.139.com/s/swfakeallp7?password=ab12", "ab12"),
        ("https://123pan.com/s/swfakeallp8?password=ab12", "ab12"),
    )
    for url, code in cases:
        li = ln.parse_link(url, None)
        assert code not in li.canonical, li.canonical


def test_parse_link_canonical_excludes_access_code_query_for_guangya():
    with_query = ln.parse_link("https://guangyapan.com/s/swfakegyac1?password=ab12", None)
    bare_plus_cell = ln.parse_link("https://guangyapan.com/s/swfakegyac1", "ab12")
    assert with_query.canonical == bare_plus_cell.canonical
    assert ln.canonical_hash(with_query.canonical) == ln.canonical_hash(bare_plus_cell.canonical)
    assert "ab12" not in with_query.canonical


def test_parse_link_canonical_excludes_access_code_query_for_139cloud():
    with_query = ln.parse_link("https://caiyun.139.com/s/swfake139ac1?password=ab12", None)
    bare_plus_cell = ln.parse_link("https://caiyun.139.com/s/swfake139ac1", "ab12")
    assert with_query.canonical == bare_plus_cell.canonical
    assert ln.canonical_hash(with_query.canonical) == ln.canonical_hash(bare_plus_cell.canonical)
    assert "ab12" not in with_query.canonical


def test_parse_link_canonical_excludes_access_code_query_for_123():
    with_query = ln.parse_link("https://123pan.com/s/swfake123ac1?password=ab12", None)
    bare_plus_cell = ln.parse_link("https://123pan.com/s/swfake123ac1", "ab12")
    assert with_query.canonical == bare_plus_cell.canonical
    assert ln.canonical_hash(with_query.canonical) == ln.canonical_hash(bare_plus_cell.canonical)
    assert "ab12" not in with_query.canonical


def test_parse_link_ed2k_hash_lowercased_and_label_uses_filename():
    li = ln.parse_link(
        "ed2k://|file|Fake.Show.S01E01.mkv|123456|DEADBEEFDEADBEEFDEADBEEFDEADBEEF|/",
        None,
    )
    assert li.provider == "ed2k"
    assert li.canonical == "ed2k:deadbeefdeadbeefdeadbeefdeadbeef"
    assert li.label == "ED2K · Fake.Show.S01E01.mkv"


def test_parse_link_ed2k_unparsable_falls_back_to_sha256_of_raw():
    a = ln.parse_link("ed2k://broken-not-the-expected-format", None)
    b = ln.parse_link("ed2k://broken-not-the-expected-format", None)
    assert a.provider == "ed2k"
    assert a.canonical.startswith("ed2k:")
    assert a.canonical == b.canonical  # deterministic


def test_parse_link_magnet_classified_as_ed2k_provider():
    li = ln.parse_link(
        "magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=Fake.Show",
        None,
    )
    assert li.provider == "ed2k"
    assert li.canonical == "ed2k:0123456789abcdef0123456789abcdef01234567"


def test_parse_link_non_url_text_is_unknown_with_invalid_link_label():
    li = ln.parse_link("随便写的文本", None)
    assert li.provider == "unknown"
    assert li.label == "无效链接"


def test_parse_link_unrecognised_host_is_unknown_with_host_label():
    li = ln.parse_link("https://example.com/foo", None)
    assert li.provider == "unknown"
    assert li.label == "未知来源 · example.com"


def test_parse_link_non_allowlisted_scheme_with_real_host_is_invalid():
    # C1 fix: only http/https/ed2k/magnet are allowlisted at import time.
    # A scheme outside that list -- even one with a real host, like ftp://
    # -- must be treated as an invalid, non-revealable link (provider
    # "unknown", INVALID_LINK_LABEL), not given a "未知来源 · <host>" label
    # with open/copy actions.
    li = ln.parse_link("ftp://host.example/x", None)
    assert li.provider == "unknown"
    assert li.label == ln.INVALID_LINK_LABEL


def test_parse_link_javascript_scheme_is_invalid_no_actions():
    li = ln.parse_link("javascript://example.com/%0aalert(1)", None)
    assert li.provider == "unknown"
    assert li.label == ln.INVALID_LINK_LABEL


def test_parse_link_vbscript_scheme_is_invalid():
    li = ln.parse_link("vbscript://h/whatever", None)
    assert li.provider == "unknown"
    assert li.label == ln.INVALID_LINK_LABEL


def test_parse_link_file_scheme_is_invalid():
    li = ln.parse_link("file://localhost/etc/passwd", None)
    assert li.provider == "unknown"
    assert li.label == ln.INVALID_LINK_LABEL


def test_parse_link_label_never_contains_full_share_code_or_access_code():
    li = ln.parse_link("https://115.com/s/swfakelabel1?password=ab12", None)
    assert "swfakelabel1" not in li.label
    assert "ab12" not in li.label
    short = ln.parse_link("https://115.com/s/ab12", None)
    assert "ab12" not in short.label  # 4-char code must not be fully revealed


# ---------------------------------------------------------------------------
# T1.6 hotfix: malformed ED2K cells must never raise (urlparse ValueError)
# ---------------------------------------------------------------------------


def test_parse_link_ed2k_bracketed_filename_does_not_raise():
    # A filename containing "[fake]" makes the raw url's netloc look like an
    # (invalid) IPv6 literal to urlparse -- must not raise.
    li = ln.parse_link(
        "ed2k://|file|Fake.Movie.[fake].mkv|123456|0123456789ABCDEF0123456789ABCDEF|/",
        None,
    )
    assert li.provider == "ed2k"
    assert li.canonical == "ed2k:0123456789abcdef0123456789abcdef"


def test_parse_link_ed2k_fullwidth_colon_and_spaces_in_filename_does_not_raise():
    # A filename containing full-width punctuation makes urlparse's netloc
    # NFKC-normalization check raise -- must not raise.
    li = ln.parse_link(
        "ed2k://|file|Fake Movie ： copy.mkv|123456|0123456789ABCDEF0123456789ABCDEF|/",
        None,
    )
    assert li.provider == "ed2k"
    assert li.canonical == "ed2k:0123456789abcdef0123456789abcdef"


def test_parse_link_ed2k_percent_encoded_filename_does_not_raise():
    li = ln.parse_link(
        "ed2k://|file|Fake%E4%B8%ADMovie.mkv|123456|0123456789ABCDEF0123456789ABCDEF|/",
        None,
    )
    assert li.provider == "ed2k"
    assert li.canonical == "ed2k:0123456789abcdef0123456789abcdef"


def test_parse_link_bracketed_http_url_does_not_raise():
    # A bracket in the *path* (not netloc) of an ordinary http(s) url never
    # raised even before the hotfix -- kept as a regression guard alongside
    # the ED2K cases above.
    li = ln.parse_link("https://115.com/s/[bad", None)
    assert li.provider == "115"
    assert li.canonical == "115:[bad"


def test_parse_link_magnet_with_bracket_and_percent_encoded_dn_does_not_raise():
    li = ln.parse_link(
        "magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=Fake%5Bx%5D%E4%B8%AD",
        None,
    )
    assert li.provider == "ed2k"
    assert li.canonical == "ed2k:0123456789abcdef0123456789abcdef01234567"


def test_canonical_hash_is_sha256_hex():
    digest = ln.canonical_hash("115:swfakehash1")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_public_id_length_and_stability():
    a = ln.public_id("115", "115:swfakepid1")
    b = ln.public_id("115", "115:swfakepid1")
    c = ln.public_id("115", "115:swfakepid2")
    assert len(a) == 22
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# T1.5 infer_media_type / media_identity / edition_fingerprint
# ---------------------------------------------------------------------------


def test_infer_media_type_tv_when_season_present():
    e = ln.parse_edition("S01", "")
    assert ln.infer_media_type(e) == "tv"


def test_infer_media_type_tv_when_only_episode_present():
    e = ln.parse_edition("第5集", "")
    assert e.season_from is None
    assert ln.infer_media_type(e) == "tv"


def test_infer_media_type_tv_when_only_complete_season_present():
    e = ln.parse_edition("全季", "")
    assert ln.infer_media_type(e) == "tv"


def test_infer_media_type_tv_when_season_present_even_with_cue_text():
    e = ln.parse_edition("S01", "")
    assert ln.infer_media_type(e, "热门综艺 第一季") == "tv"


def test_infer_media_type_movie_when_no_season_episode_and_no_cue_text():
    e = ln.parse_edition("4K WEB-DL", "")
    assert ln.infer_media_type(e, "奇异博士2") == "movie"


def test_infer_media_type_movie_when_text_omitted_defaults_to_empty():
    e = ln.parse_edition("4K WEB-DL", "")
    assert ln.infer_media_type(e) == "movie"


@pytest.mark.parametrize(
    "cue_text",
    [
        "综艺 大晚会",
        "真人秀花絮",
        "深夜脱口秀",
        "更新至第10期",
        "已更至完结",
        "长篇连载小说改编",
        "热血番剧",
        "国产动漫合集",
        "纪录片精选",
        "怀旧系列",
        "谍战三部曲",
        "全12部",
        "全 3 部",
    ],
)
def test_infer_media_type_unknown_when_no_season_but_cue_text_present(cue_text):
    e = ln.parse_edition("4K WEB-DL", "")
    assert ln.infer_media_type(e, cue_text) == "unknown"


def test_media_identity_tmdb_exact_match():
    info = ln.parse_title("示例剧 (2024)", "示例剧")
    assert ln.media_identity(info, "unknown", ("tv", 123)) == "tmdb:tv:123"


def test_media_identity_title_based_with_year():
    info = ln.parse_title("示例剧 (2024)", "示例剧")
    assert ln.media_identity(info, "tv", None) == "title:示例剧:2024:tv"


def test_media_identity_no_year_uses_zero():
    info = ln.parse_title("示例剧", "示例剧")
    assert ln.media_identity(info, "unknown", None) == "title:示例剧:0:unknown"


def test_edition_fingerprint_empty_edition_is_unspecified():
    e = ln.parse_edition("", "")
    assert ln.edition_fingerprint(e) == "unspecified"


def test_edition_fingerprint_src_resend_purchased_tags_do_not_affect_fingerprint():
    a = ln.parse_edition("S01 4K WEB-DL 内封简繁 NF", "")
    b = ln.parse_edition("S01 4K WEB-DL 内封简繁 补 自购", "")
    assert a.tags != b.tags
    assert ln.edition_fingerprint(a) == ln.edition_fingerprint(b)


def test_edition_fingerprint_differs_on_quality_season_or_hdr():
    base = ln.parse_edition("S01 4K WEB-DL DV 内封简繁", "")
    diff_quality = ln.parse_edition("S01 1080p WEB-DL DV 内封简繁", "")
    diff_season = ln.parse_edition("S02 4K WEB-DL DV 内封简繁", "")
    diff_hdr = ln.parse_edition("S01 4K WEB-DL HDR10 内封简繁", "")
    base_fp = ln.edition_fingerprint(base)
    assert base_fp != ln.edition_fingerprint(diff_quality)
    assert base_fp != ln.edition_fingerprint(diff_season)
    assert base_fp != ln.edition_fingerprint(diff_hdr)


def test_edition_fingerprint_matches_worked_example_row1():
    # §4.9 row 1 worked example, verbatim.
    e = ln.parse_edition("S01 4K WEB-DL DV 内封简繁 HiveWeb", "示例剧 (2024)")
    assert ln.edition_fingerprint(e) == "s1-1|e?|part|2160p|webdl|dv|?|a=|t=embedded:zh-hans+zh-hant|x="


def test_construction_plan_4_9_worked_example_rows():
    # §4.9's five synthetic rows, exercised end-to-end through parse_title /
    # parse_edition / parse_link, asserting the identity/fingerprint
    # equal/unequal relationships the table describes.
    title_cell, media_title_cell = "示例剧 (2024)", "示例剧"

    row1_title = ln.parse_title(title_cell, media_title_cell)
    row1_edition = ln.parse_edition("S01 4K WEB-DL DV 内封简繁 HiveWeb", title_cell)
    row1_link = ln.parse_link("https://115cdn.com/s/swfakerow1", None)
    row1_type = ln.infer_media_type(row1_edition)
    row1_identity = ln.media_identity(row1_title, row1_type, None)
    row1_fp = ln.edition_fingerprint(row1_edition)

    # Row 2: same media, same group (src:* doesn't affect fingerprint),
    # different link (+1 link on the same group).
    row2_edition = ln.parse_edition("S01 4K WEB-DL DV 内封简繁 NF", title_cell)
    row2_link = ln.parse_link("https://pan.quark.cn/s/swfakerow2", None)
    row2_identity = ln.media_identity(row1_title, ln.infer_media_type(row2_edition), None)
    assert row2_identity == row1_identity
    assert ln.edition_fingerprint(row2_edition) == row1_fp
    assert row2_link.canonical != row1_link.canonical

    # Row 3: same media, new group (quality differs).
    row3_edition = ln.parse_edition("S01 1080p 内封简繁", title_cell)
    row3_identity = ln.media_identity(row1_title, ln.infer_media_type(row3_edition), None)
    assert row3_identity == row1_identity
    assert ln.edition_fingerprint(row3_edition) != row1_fp

    # Row 4: same media, new group (season differs).
    row4_edition = ln.parse_edition("S02 4K WEB-DL DV 内封简繁", title_cell)
    row4_identity = ln.media_identity(row1_title, ln.infer_media_type(row4_edition), None)
    assert row4_identity == row1_identity
    assert ln.edition_fingerprint(row4_edition) != row1_fp

    # Row 5: no year, no season/cue text -> markerless title infers "movie",
    # giving a different media identity (title:...:0:movie).
    row5_title = ln.parse_title("示例剧", "示例剧")
    row5_edition = ln.parse_edition("4K", "示例剧")
    row5_type = ln.infer_media_type(row5_edition, "示例剧 示例剧 4K")
    row5_identity = ln.media_identity(row5_title, row5_type, None)
    assert row5_type == "movie"
    assert row5_identity == "title:示例剧:0:movie"
    assert row5_identity != row1_identity
    # But once TMDB resolves both rows 1 and 5 to the same tv id, they merge.
    row1_tmdb_identity = ln.media_identity(row1_title, row1_type, ("tv", 999))
    row5_tmdb_identity = ln.media_identity(row5_title, row5_type, ("tv", 999))
    assert row1_tmdb_identity == row5_tmdb_identity == "tmdb:tv:999"


# ---------------------------------------------------------------------------
# T17 §12.3: resource_specs() -- resolution/dynamic-range/source spec chips.
# ---------------------------------------------------------------------------


class TestResourceSpecsResolution:
    @pytest.mark.parametrize("value", ["2160p", "2160P", "4K", "4k", "UHD"])
    def test_2160p_and_4k_aliases_map_to_4k(self, value):
        assert ln.resource_specs(value, None, None) == {"resolution": {"value": "4K", "icon": "icon-resolution"}}

    def test_1080p_stays_as_is(self):
        assert ln.resource_specs("1080p", None, None)["resolution"]["value"] == "1080p"

    def test_720p_stays_as_is(self):
        assert ln.resource_specs("720p", None, None)["resolution"]["value"] == "720p"

    def test_sd_stays_as_is(self):
        assert ln.resource_specs("SD", None, None)["resolution"]["value"] == "SD"

    @pytest.mark.parametrize("value", [None, "", "other"])
    def test_missing_or_unrecognised_quality_has_no_resolution_key(self, value):
        assert "resolution" not in ln.resource_specs(value, None, None)


class TestResourceSpecsDynamicRange:
    @pytest.mark.parametrize("value", ["dv_hdr", "DV+HDR10", "DV & HDR10", "dv-hdr"])
    def test_dv_plus_hdr_combo_maps_to_dv_hdr(self, value):
        assert ln.resource_specs(None, value, None) == {"dynamic_range": {"value": "DV/HDR", "icon": "icon-dynamic-range"}}

    @pytest.mark.parametrize("value", ["dv", "DV", "Dolby Vision", "dolby_vision"])
    def test_dv_alone_maps_to_dolby_vision(self, value):
        assert ln.resource_specs(None, value, None)["dynamic_range"]["value"] == "Dolby Vision"

    @pytest.mark.parametrize("value", ["hdr10plus", "HDR10+", "hdr10p"])
    def test_hdr10_plus_variants(self, value):
        assert ln.resource_specs(None, value, None)["dynamic_range"]["value"] == "HDR10+"

    def test_hdr10_alone(self):
        assert ln.resource_specs(None, "hdr10", None)["dynamic_range"]["value"] == "HDR10"

    def test_hlg(self):
        assert ln.resource_specs(None, "hlg", None)["dynamic_range"]["value"] == "HLG"

    def test_sdr(self):
        assert ln.resource_specs(None, "sdr", None)["dynamic_range"]["value"] == "SDR"

    @pytest.mark.parametrize("value", [None, ""])
    def test_missing_hdr_has_no_dynamic_range_key(self, value):
        assert "dynamic_range" not in ln.resource_specs(None, value, None)


class TestResourceSpecsSource:
    @pytest.mark.parametrize("value", ["webdl", "WEB-DL", "web_dl", "WEB.DL"])
    def test_webdl_case_and_separator_variants(self, value):
        assert ln.resource_specs(None, None, value)["source"]["value"] == "WEB-DL"

    @pytest.mark.parametrize("value", ["webrip", "WEBRip", "web-rip"])
    def test_webrip_variants(self, value):
        assert ln.resource_specs(None, None, value)["source"]["value"] == "WEBRip"

    @pytest.mark.parametrize("value", ["bluray", "BluRay", "blu-ray", "Blu_Ray"])
    def test_bluray_variants(self, value):
        assert ln.resource_specs(None, None, value)["source"]["value"] == "BluRay"

    @pytest.mark.parametrize("value", ["remux", "REMUX", "BDRemux"])
    def test_remux_variants_map_to_bdremux(self, value):
        assert ln.resource_specs(None, None, value)["source"]["value"] == "BDRemux"

    def test_hdtv(self):
        assert ln.resource_specs(None, None, "hdtv")["source"]["value"] == "HDTV"

    def test_bdrip(self):
        assert ln.resource_specs(None, None, "BDRip")["source"]["value"] == "BDRip"

    @pytest.mark.parametrize("value", [None, ""])
    def test_missing_source_has_no_source_key(self, value):
        assert "source" not in ln.resource_specs(None, None, value)


class TestResourceSpecsCombinedAndScope:
    def test_all_three_present(self):
        specs = ln.resource_specs("2160p", "dv_hdr", "webdl")
        assert specs == {
            "resolution": {"value": "4K", "icon": "icon-resolution"},
            "dynamic_range": {"value": "DV/HDR", "icon": "icon-dynamic-range"},
            "source": {"value": "WEB-DL", "icon": "icon-source"},
        }

    def test_all_missing_returns_empty_dict(self):
        assert ln.resource_specs(None, None, None) == {}

    def test_output_never_contains_provider_audio_subtitle_or_release_group_text(self):
        # §12.3: provider/audio/subtitle/release-group tokens must never
        # leak into the spec-chip output, even if accidentally passed in.
        specs = ln.resource_specs("2160p WEB-DL 115 DDP5.1 内封简繁 HiveWeb", "dv_hdr", "webdl")
        blob = str(specs)
        for marker in ("115", "DDP5.1", "内封简繁", "HiveWeb"):
            assert marker not in blob


# ---------------------------------------------------------------------------
# T17 §14.5: format_ratings()/primary_rating() -- per-source passthrough,
# no averaging, no fake zeros, safe canonical URLs.
# ---------------------------------------------------------------------------


class TestFormatRatings:
    def test_all_three_sources_present(self):
        ratings = ln.format_ratings(
            '{"tmdb": {"score": 8.7, "votes": 31198}, "imdb": {"score": 9.3, "votes": 3200000}, '
            '"tvmaze": {"score": 8.1, "votes": null}}',
            media_type="movie", tmdb_id=550, imdb_id="tt0137523", tvmaze_id=None,
        )
        assert ratings == {
            "tmdb": {"score": 8.7, "votes": 31198, "url": "https://www.themoviedb.org/movie/550"},
            "imdb": {"score": 9.3, "votes": 3200000, "url": "https://www.imdb.com/title/tt0137523/"},
            "tvmaze": {"score": 8.1, "votes": None, "url": None},
        }

    def test_tv_tmdb_url_uses_tv_segment(self):
        ratings = ln.format_ratings(
            '{"tmdb": {"score": 7.5, "votes": 900}}', media_type="tv", tmdb_id=1399, imdb_id=None, tvmaze_id=None,
        )
        assert ratings["tmdb"]["url"] == "https://www.themoviedb.org/tv/1399"

    def test_tvmaze_url_from_tvmaze_id(self):
        ratings = ln.format_ratings(
            '{"tvmaze": {"score": 8.1, "votes": null}}', media_type="tv", tmdb_id=None, imdb_id=None, tvmaze_id=82,
        )
        assert ratings["tvmaze"]["url"] == "https://www.tvmaze.com/shows/82"

    def test_zero_votes_drops_tmdb_and_imdb_rather_than_showing_fake_score(self):
        ratings = ln.format_ratings(
            '{"tmdb": {"score": 8.7, "votes": 0}, "imdb": {"score": 9.3, "votes": 0}}',
            media_type="movie", tmdb_id=1, imdb_id="tt1", tvmaze_id=None,
        )
        assert ratings == {}

    def test_null_or_zero_score_is_dropped_not_shown_as_0_0(self):
        ratings = ln.format_ratings(
            '{"tmdb": {"score": 0, "votes": 100}, "imdb": {"score": null, "votes": 100}}',
            media_type="movie", tmdb_id=1, imdb_id="tt1", tvmaze_id=None,
        )
        assert ratings == {}

    def test_missing_ratings_json_returns_empty_dict(self):
        assert ln.format_ratings(None, media_type="movie", tmdb_id=1, imdb_id=None, tvmaze_id=None) == {}
        assert ln.format_ratings("", media_type="movie", tmdb_id=1, imdb_id=None, tvmaze_id=None) == {}
        assert ln.format_ratings("{}", media_type="movie", tmdb_id=1, imdb_id=None, tvmaze_id=None) == {}

    def test_malformed_json_returns_empty_dict_not_a_crash(self):
        assert ln.format_ratings("not json", media_type="movie", tmdb_id=1, imdb_id=None, tvmaze_id=None) == {}

    def test_sources_never_merged_or_averaged(self):
        # imdb has no votes -> dropped; tmdb's score must stay exactly its
        # own value, never blended with imdb's.
        ratings = ln.format_ratings(
            '{"tmdb": {"score": 6.0, "votes": 10}, "imdb": {"score": 9.9}}',
            media_type="movie", tmdb_id=1, imdb_id="tt1", tvmaze_id=None,
        )
        assert ratings == {"tmdb": {"score": 6.0, "votes": 10, "url": "https://www.themoviedb.org/movie/1"}}

    def test_url_never_carries_query_params(self):
        ratings = ln.format_ratings(
            '{"tmdb": {"score": 8.0, "votes": 10}}', media_type="movie", tmdb_id=1, imdb_id=None, tvmaze_id=None,
        )
        assert "?" not in ratings["tmdb"]["url"]


class TestPrimaryRating:
    def test_prefers_tmdb(self):
        ratings = {"tmdb": {"score": 8.0, "votes": 1, "url": None}, "imdb": {"score": 9.0, "votes": 1, "url": None}}
        assert ln.primary_rating(ratings) == {"source": "tmdb", "score": 8.0, "votes": 1, "url": None}

    def test_falls_back_to_imdb_when_tmdb_missing(self):
        ratings = {"imdb": {"score": 9.0, "votes": 1, "url": None}}
        assert ln.primary_rating(ratings) == {"source": "imdb", "score": 9.0, "votes": 1, "url": None}

    def test_none_when_neither_present(self):
        assert ln.primary_rating({"tvmaze": {"score": 8.0, "votes": None, "url": None}}) is None
        assert ln.primary_rating({}) is None
