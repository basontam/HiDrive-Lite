"""Deterministic, fully-fictional media-library workbooks for T0.x tests.

The real HDHive / 影巢 group-backup workbooks are sensitive resource lists and
never touch this repository.  Later importer tests need something with the
*same layout* (banner row, note row, blank row, 12-column header on row 4,
data from row 5, the same sheet names) but entirely invented content, so
tests can run offline and nothing here can leak a real share code.

All hostnames below are the *real* provider domains used by the network
drives HiDrive-Lite indexes (that part of the layout matters for the
importer), but every share code / path segment is an obviously fake string
(``fake...`` / ``swfake...``) and every access code is a fixed 4-character
placeholder.

``make_workbooks(dest)`` writes three ``.xlsx`` files into ``dest`` and
returns their paths.  All data is built once at import time from hardcoded
row literals (no randomness, no system clock), so two calls - even in two
different processes - produce byte-identical spreadsheets.  ``EXPECTED``
is derived from that same data with plain Python (never hand-typed), so it
can never drift from what actually gets written.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import Workbook

# --------------------------------------------------------------------------
# Layout constants (must match the real workbooks exactly - see the T0.4
# brief).
# --------------------------------------------------------------------------

HEADERS = [
    "类型",
    "记录ID",
    "Slug/追更包Slug",
    "用户UID",
    "官组标记",
    "标题",
    "媒体标题",
    "链接",
    "访问码",
    "备注",
    "创建时间",
    "删除时间",
]

BANNER_TEXT = "HDHive 官方资料备份 · 内部使用 · 示例数据请勿外传"
NOTE_TEXT = "本工作簿由脚本生成，仅供导入器测试使用，不含真实分享信息"

OFFICIAL_FILENAME = "HDHive official-group-backup.xlsx"
SUPPLEMENT_VIDEO_FILENAME = "影巢_影视分享_整理.xlsx"
SUPPLEMENT_ED2K_FILENAME = "影巢_ED2K_整理.xlsx"

_BADGES = ["无能", "大佬", "养老", "历史官组标记"]
_SLUG_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")
_EPOCH = datetime(2024, 1, 1)


def _slug(record_id: int, kind: str) -> str:
    """Deterministic UUID string derived from (record_id, kind) - no RNG."""
    return str(uuid.uuid5(_SLUG_NAMESPACE, f"{kind}:{record_id}"))


def _badge(index: int) -> str:
    return _BADGES[index % len(_BADGES)]


def _dt(offset_days: int) -> datetime:
    return _EPOCH + timedelta(days=offset_days)


def _row(
    *,
    type_: str,
    record_id: int,
    uid: int,
    badge_index: int,
    title: str,
    media_title: str,
    link: str,
    code: str = "",
    remark: str = "",
    created_offset: int = 0,
    deleted_offset: int | None = None,
) -> dict:
    return {
        "类型": type_,
        "记录ID": record_id,
        "Slug/追更包Slug": _slug(record_id, type_),
        "用户UID": uid,
        "官组标记": _badge(badge_index),
        "标题": title,
        "媒体标题": media_title,
        "链接": link,
        "访问码": code,
        "备注": remark,
        "创建时间": _dt(created_offset),
        "删除时间": _dt(deleted_offset) if deleted_offset is not None else None,
    }


# --------------------------------------------------------------------------
# 影视分享 (类型 == "影视") - the scenario rows called out by the brief.
# Record IDs 1001-1025.  Kept as one row per named scenario so each
# requirement in the brief maps to exactly one line below.
# --------------------------------------------------------------------------

_VIDEO_SCENARIO_ROWS = [
    # -- same "标题 (年份)", three different 备注 (edition) versions --------
    _row(
        type_="影视", record_id=1001, uid=500001, badge_index=1,
        title="深渊行者 (2024)", media_title="深渊行者",
        link="https://115cdn.com/s/swfakeaa11?password=ab12", code="",
        remark="S01 4K WEB-DL DV 内封简繁 HiveWeb", created_offset=1,
    ),
    _row(
        type_="影视", record_id=1002, uid=500002, badge_index=2,
        title="深渊行者 (2024)", media_title="深渊行者",
        link="https://pan.quark.cn/s/fakeaa12", code="cd12",
        remark="S01 1080p 内封简繁", created_offset=2,
    ),
    _row(
        type_="影视", record_id=1003, uid=500003, badge_index=3,
        title="深渊行者 (2024)", media_title="深渊行者",
        link="https://pan.baidu.com/s/1fakeaa13?pwd=ef34", code="",
        remark="S02 4K WEB-DL DV 内封简繁", created_offset=3,
    ),
    # -- aggregation group 1: same 标题+备注, two different hosts ----------
    _row(
        type_="影视", record_id=1004, uid=500004, badge_index=0,
        title="边界之外 (2023)", media_title="边界之外",
        link="https://anxia.com/s/swfakeg1a1", code="",
        remark="S01 4K WEB-DL DV 内封简繁 NF", created_offset=4,
    ),
    _row(
        type_="影视", record_id=1005, uid=500005, badge_index=1,
        title="边界之外 (2023)", media_title="边界之外",
        link="https://www.aliyundrive.com/s/fakeg1b1", code="",
        remark="S01 4K WEB-DL DV 内封简繁 NF", created_offset=4,
    ),
    # -- aggregation group 2: same 标题+备注, two different hosts ----------
    _row(
        type_="影视", record_id=1006, uid=500006, badge_index=2,
        title="潮汐回声 (2022)", media_title="潮汐回声",
        link="https://cloud.189.cn/t/fakeg2a1", code="cd56",
        remark="4K REMUX DV&HDR 国英音轨 特效字幕", created_offset=5,
    ),
    _row(
        type_="影视", record_id=1007, uid=500007, badge_index=3,
        title="潮汐回声 (2022)", media_title="潮汐回声",
        link="https://www.123912.com/s/fakeg2b1", code="",
        remark="4K REMUX DV&HDR 国英音轨 特效字幕", created_offset=5,
    ),
    # -- 115 code-only-in-column, deleted (1/3) -----------------------------
    _row(
        type_="影视", record_id=1008, uid=500008, badge_index=0,
        title="残响之城 (2021)", media_title="残响之城",
        link="https://115.com/s/swfakerowa1", code="cd34",
        remark="两季全 1080p 内封简繁", created_offset=6, deleted_offset=36,
    ),
    # -- 115 missing "/s/" path segment, title with empty-parens no-year ---
    _row(
        type_="影视", record_id=1009, uid=500009, badge_index=1,
        title="孤星 ()", media_title="孤星",
        link="https://115.com/swfakerowb1", code="ab12",
        remark="S01E01 - E06 1080P 内嵌简中 仅秒传", created_offset=7,
    ),
    # -- alipan, title with no parens/year at all ---------------------------
    _row(
        type_="影视", record_id=1010, uid=500010, badge_index=2,
        title="无名岁月", media_title="无名岁月",
        link="https://www.alipan.com/s/fakerowc1", code="",
        remark="1080p BDRip 内封简英", created_offset=8,
    ),
    # -- 天翼 21cn form (access code embedded via accessCode=), alias title -
    _row(
        type_="影视", record_id=1011, uid=500011, badge_index=3,
        title="旧日回声 / 往昔低语 (2022)", media_title="旧日回声",
        link="https://content.21cn.com/share/page?id=fakerowd1&accessCode=ab12",
        code="", remark="【BluRay.1080P】【内封简英双语特效字幕】【51.4GB】",
        created_offset=9,
    ),
    # -- 广亚: same link, same title, appears twice (dedup case) -----------
    _row(
        type_="影视", record_id=1012, uid=500012, badge_index=0,
        title="深海孤灯 (2020)", media_title="深海孤灯",
        link="https://www.guangyapan.com/s/1234efgh", code="",
        remark="", created_offset=10,
    ),
    _row(
        type_="影视", record_id=1013, uid=500013, badge_index=1,
        title="深海孤灯 (2020)", media_title="深海孤灯",
        link="https://www.guangyapan.com/s/1234efgh", code="",
        remark="", created_offset=10, deleted_offset=40,  # deleted (2/3)
    ),
    # -- 139 caiyun host, traditional-character title -----------------------
    _row(
        type_="影视", record_id=1014, uid=500014, badge_index=2,
        title="還陽 (2021)", media_title="還陽",
        link="https://caiyun.139.com/m/i?fakerowg1", code="ab12",
        remark="随便写的备注", created_offset=11,
    ),
    # -- 139 yun host, English-original-name title ---------------------------
    _row(
        type_="影视", record_id=1015, uid=500015, badge_index=3,
        title="Fake Title (2023)", media_title="Fake Title",
        link="https://yun.139.com/w/i/fakerowh1", code="cd12",
        remark="S01 1080p 内封简英", created_offset=12,
    ),
    # -- 123 (123865), 媒体标题 differs from 标题去年份, deleted (3/3) ------
    _row(
        type_="影视", record_id=1016, uid=500016, badge_index=0,
        title="旧城计划 (2019)", media_title="旧城重制计划",
        link="https://www.123865.com/s/fakerowi1", code="",
        remark="4K WEB-DL 内封简英", created_offset=13, deleted_offset=43,
    ),
    # -- 115cdn, 媒体标题 differs from 标题去年份 (2nd row) ------------------
    _row(
        type_="影视", record_id=1017, uid=500017, badge_index=1,
        title="远行者 (2022)", media_title="远行计划",
        link="https://115cdn.com/s/swfakerowj1", code="ab12",
        remark="1080p WEB-DL 内封简中", created_offset=14,
    ),
    # -- URL 内访问码与 访问码 列不同的冲突行 ---------------------------------
    _row(
        type_="影视", record_id=1018, uid=500018, badge_index=2,
        title="夜航船 (2020)", media_title="夜航船",
        link="https://115.com/s/swfakerowk1?password=ab12", code="zz99",
        remark="S01 1080p WEB-DL 内封简中", created_offset=15,
    ),
    # -- ED2K block: 5 rows, two of them sharing an identical link ----------
    _row(
        type_="影视", record_id=1019, uid=500019, badge_index=3,
        title="深渊蚀刻 (2024)", media_title="深渊蚀刻",
        link="ed2k://|file|Fake.Show.S01E01.2160p.WEB-DL.x265|123456|0123456789ABCDEF0123456789ABCDEF01|/",
        code="", remark="4K WEB-DL DV 内封简繁", created_offset=16,
    ),
    _row(
        type_="影视", record_id=1020, uid=500020, badge_index=0,
        title="深渊蚀刻 (2024)", media_title="深渊蚀刻",
        link="ed2k://|file|Fake.Show.S01E02.2160p.WEB-DL.x265|123456|0123456789ABCDEF0123456789ABCDEF02|/",
        code="", remark="4K WEB-DL DV 内封简繁", created_offset=16,
    ),
    _row(
        type_="影视", record_id=1021, uid=500021, badge_index=1,
        title="深渊蚀刻 (2024)", media_title="深渊蚀刻",
        link="ed2k://|file|Fake.Show.S01E03.2160p.WEB-DL.x265|123456|0123456789ABCDEF0123456789ABCDEF03|/",
        code="", remark="4K WEB-DL DV 内封简繁", created_offset=16,
    ),
    _row(
        type_="影视", record_id=1022, uid=500022, badge_index=2,
        title="深渊蚀刻 (2024)", media_title="深渊蚀刻",
        link="ed2k://|file|Fake.Show.S01E04.2160p.WEB-DL.x265|123456|0123456789ABCDEF0123456789ABCDEF04|/",
        code="", remark="4K WEB-DL DV 内封简繁", created_offset=16,
    ),
    _row(
        # duplicate link of record 1022 (intra-file ED2K dedup case)
        type_="影视", record_id=1023, uid=500023, badge_index=3,
        title="深渊蚀刻 (2024)", media_title="深渊蚀刻",
        link="ed2k://|file|Fake.Show.S01E04.2160p.WEB-DL.x265|123456|0123456789ABCDEF0123456789ABCDEF04|/",
        code="", remark="4K WEB-DL DV 内封简繁 重发", created_offset=17,
    ),
    # -- non-URL free text rows ----------------------------------------------
    _row(
        type_="影视", record_id=1024, uid=500024, badge_index=0,
        title="夜色未眠 (2021)", media_title="夜色未眠",
        link="请私信要资源，站内联系", code="",
        remark="", created_offset=18,
    ),
    _row(
        type_="影视", record_id=1025, uid=500025, badge_index=1,
        title="旧梦重圆 (2023)", media_title="旧梦重圆",
        link="私聊获取，链接已失效", code="",
        remark="", created_offset=19,
    ),
]

# --------------------------------------------------------------------------
# Extra scenario rows for T2.6's search-quality golden set (appended, never
# edited into the block above): the existing scenario rows have no English
# "original title" alias, only one traditional-character title, and only two
# plain-Chinese aliases, so the golden set's 原名/英文前缀/繁体/别名 query
# categories need a little more corpus to draw from.
# --------------------------------------------------------------------------

_EVAL_EXTRA_ROWS = [
    # -- English original-title alias (-> media.title_original) ------------
    _row(
        type_="影视", record_id=1201, uid=500501, badge_index=0,
        title="深藏之影 / Hidden Shadow (2023)", media_title="深藏之影",
        link="https://pan.quark.cn/s/fakerowm1", code="ab12",
        remark="1080p WEB-DL 国英字幕", created_offset=41,
    ),
    # -- plain Chinese alias (no year/parens tension) -----------------------
    _row(
        type_="影视", record_id=1202, uid=500502, badge_index=1,
        title="沉默海岸 / 寂静海岸 (2020)", media_title="沉默海岸",
        link="https://115.com/s/swfakerowm2", code="",
        remark="1080p WEB-DL 内封简中", created_offset=42,
    ),
    # -- second traditional-character title (还阳's 還陽 is the only other
    # one) so the "繁体" golden category has more than one sample -----------
    _row(
        type_="影视", record_id=1203, uid=500503, badge_index=2,
        title="鏡中人 (2020)", media_title="鏡中人",
        link="https://pan.baidu.com/s/1fakerowm3?pwd=cd12", code="",
        remark="1080p BDRip 内封简中", created_offset=43,
    ),
]

# --------------------------------------------------------------------------
# Filler 影视 rows: bulk out 影视分享 to the 40-60 row range required by the
# brief, and add extra 115/quark/alipan/baidu/tianyi coverage (deterministic,
# index-driven - no randomness).
# --------------------------------------------------------------------------

_FILLER_HOST_SPECS = [
    ("115cdn", lambda i: f"https://115cdn.com/s/swfakef{i:03d}"),
    ("quark", lambda i: f"https://pan.quark.cn/s/fakef{i:03d}"),
    ("alipan", lambda i: f"https://www.alipan.com/s/fakef{i:03d}"),
    ("baidu", lambda i: f"https://pan.baidu.com/s/1fakef{i:03d}?pwd=ab12"),
    ("tianyi189", lambda i: f"https://cloud.189.cn/t/fakef{i:03d}"),
]

_FILLER_REMARKS = [
    "1080p WEB-DL 内封简中",
    "4K WEB-DL 国英音轨",
    "1080p BDRip 内封简中",
    "2160p REMUX HDR",
]

_FILLER_COUNT = 20


def _build_filler_rows() -> list[dict]:
    rows = []
    for i in range(_FILLER_COUNT):
        record_id = 1101 + i
        _host_name, link_fn = _FILLER_HOST_SPECS[i % len(_FILLER_HOST_SPECS)]
        year = 2015 + (i % 10)
        title = f"填充剧集{i + 1:02d} ({year})"
        remark = _FILLER_REMARKS[i % len(_FILLER_REMARKS)]
        if i == 2:
            # 填充剧集03: markerless (no season/episode) but the remark
            # carries a TV-ish cue word, so infer_media_type() classifies it
            # "unknown" rather than "movie" -- the only fixture row that
            # exercises that branch through the real importer (§4.6). Chosen
            # because filler rows carry no alias/access-code/year quirks and
            # this index isn't referenced by search_golden.json (unlike
            # 填充剧集02/09, which are).
            remark += " 纪录片"
        rows.append(
            _row(
                type_="影视", record_id=record_id, uid=500100 + i, badge_index=i,
                title=title, media_title=f"填充剧集{i + 1:02d}",
                link=link_fn(i), code="",
                remark=remark,
                created_offset=20 + i,
            )
        )
    return rows


VIDEO_ROWS = _VIDEO_SCENARIO_ROWS + _EVAL_EXTRA_ROWS + _build_filler_rows()

# --------------------------------------------------------------------------
# 音乐分享 (类型 == "音乐")
# --------------------------------------------------------------------------

MUSIC_ROWS = [
    _row(
        type_="音乐", record_id=2001, uid=500201, badge_index=0,
        title="夜之旋律 (2022)", media_title="夜之旋律",
        link="https://pan.quark.cn/s/fakem1", code="ab12",
        remark="", created_offset=50,
    ),
    _row(
        type_="音乐", record_id=2002, uid=500202, badge_index=1,
        title="回声集 (2023)", media_title="回声集",
        link="https://115cdn.com/s/swfakem2", code="",
        remark="FLAC 无损", created_offset=51,
    ),
]

# --------------------------------------------------------------------------
# 追更条目 (类型 == "追更") - first row deliberately reuses record 1001's ID
# to exercise a cross-type 记录ID collision, and carries an ED2K link so the
# 磁力ED2K sheet's "含追更行" rule has something to include.
# --------------------------------------------------------------------------

ZHUIGENG_ROWS = [
    _row(
        type_="追更", record_id=1001, uid=500301, badge_index=2,
        title="深渊行者 追更包 (2024)", media_title="深渊行者",
        link="ed2k://|file|Fake.Zhuigeng.Pack.S01.2160p|999999|0123456789ABCDEF0123456789ABCDEFAA|/",
        code="", remark="追更同步", created_offset=52,
    ),
    _row(
        type_="追更", record_id=3002, uid=500302, badge_index=3,
        title="边界之外 追更包 (2023)", media_title="边界之外",
        link="https://115cdn.com/s/swfaketj2a", code="ab12",
        remark="追更同步", created_offset=53,
    ),
    _row(
        type_="追更", record_id=3003, uid=500303, badge_index=0,
        title="潮汐回声 追更包 (2022)", media_title="潮汐回声",
        link="https://pan.quark.cn/s/faketj3a", code="",
        remark="追更同步 4K", created_offset=54,
    ),
]

# --------------------------------------------------------------------------
# 分享明细 = 影视分享 + 音乐分享 + 追更条目 rows, in that order.
# 磁力ED2K = 分享明细 rows whose 链接 starts with "ed2k://" (含追更行).
# --------------------------------------------------------------------------

DETAIL_ROWS = VIDEO_ROWS + MUSIC_ROWS + ZHUIGENG_ROWS
ED2K_ROWS = [row for row in DETAIL_ROWS if row["链接"].startswith("ed2k://")]

# --------------------------------------------------------------------------
# 影巢_影视分享_整理.xlsx: 12 rows, all 115-provider. 10 copy official 115
# rows exactly (记录ID/Slug/链接/访问码 identical); 2 are new links.
# --------------------------------------------------------------------------


def _classify_provider(link: str) -> str:
    if not isinstance(link, str) or not link:
        return "unknown"
    if link.startswith("ed2k://"):
        return "ed2k"
    parsed = urlparse(link)
    host = parsed.netloc.lower()
    if not host:
        return "unknown"
    if host in {"115.com", "www.115.com", "115cdn.com", "www.115cdn.com", "share.115.com", "anxia.com", "www.anxia.com"}:
        return "115"
    if host in {"cloud.189.cn", "content.21cn.com", "h5.cloud.189.cn", "www.21cn.com"}:
        return "tianyicloud"
    if host in {"pan.quark.cn"}:
        return "quark"
    if host in {"alipan.com", "www.alipan.com", "aliyundrive.com", "www.aliyundrive.com", "alywp.net"}:
        return "alipan"
    if host in {"pan.baidu.com", "yun.baidu.com"}:
        return "baidu"
    if host in {"guangyapan.com", "www.guangyapan.com"}:
        return "guangya"
    if host in {"caiyun.139.com", "yun.139.com"}:
        return "139cloud"
    if host in {"123pan.com", "123684.com", "123865.com", "www.123865.com", "123912.com", "www.123912.com", "123pan.cn"}:
        return "123"
    return "unknown"


_official_115_video_rows = [row for row in VIDEO_ROWS if _classify_provider(row["链接"]) == "115"]
_supplement_video_duplicates = [dict(row) for row in _official_115_video_rows[:10]]
_supplement_video_new = [
    _row(
        type_="影视", record_id=9001, uid=500401, badge_index=0,
        title="孤帆远影 (2021)", media_title="孤帆远影",
        link="https://115cdn.com/s/swfakesupp1", code="ab12",
        remark="1080p WEB-DL 内封简中", created_offset=60,
    ),
    _row(
        type_="影视", record_id=9002, uid=500402, badge_index=1,
        title="灯塔守夜人 (2022)", media_title="灯塔守夜人",
        link="https://115.com/s/swfakesupp2", code="",
        remark="4K WEB-DL DV 内封简繁", created_offset=61,
    ),
]
SUPPLEMENT_VIDEO_ROWS = _supplement_video_duplicates + _supplement_video_new

# --------------------------------------------------------------------------
# 影巢_ED2K_整理.xlsx: 5 rows - 4 copy official 磁力ED2K rows exactly, 1 new.
# --------------------------------------------------------------------------

_supplement_ed2k_duplicates = [dict(row) for row in ED2K_ROWS[:4]]
_supplement_ed2k_new = [
    _row(
        type_="影视", record_id=9003, uid=500403, badge_index=2,
        title="深渊蚀刻 (2024)", media_title="深渊蚀刻",
        link="ed2k://|file|Fake.Extra.Ep99.1080p|777777|FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF|/",
        code="", remark="影巢补充", created_offset=62,
    ),
]
SUPPLEMENT_ED2K_ROWS = _supplement_ed2k_duplicates + _supplement_ed2k_new

# --------------------------------------------------------------------------
# Small reference sheets (official file only).
# --------------------------------------------------------------------------

GROUP_UID_ROWS = [
    ("官组UID", "备注"),
    (910001, "示例官组A"),
    (910002, "示例官组B"),
    (910003, "示例官组C"),
]

OWNER_ROWS = [
    ("用户UID", "昵称"),
    (500001, "示例用户A"),
    (500002, "示例用户B"),
    (500003, "示例用户C"),
]

OVERVIEW_ROWS = [
    ("统计项", "说明", "口径", "备注", "", "", "", ""),
    ("影视分享", "示例统计数据，仅供人工核对参考", "按记录ID计数", "本表为脚本生成的虚构数据", "", "", "", ""),
    ("磁力ED2K", "示例统计数据，仅供人工核对参考", "按记录ID计数", "本表为脚本生成的虚构数据", "", "", "", ""),
]


# --------------------------------------------------------------------------
# EXPECTED - computed from the data above, never hand-typed.
# --------------------------------------------------------------------------


def _provider_counts(rows: list[dict]) -> dict[str, int]:
    return dict(Counter(_classify_provider(row["链接"]) for row in rows))


def _unique_link_count(rows: list[dict]) -> int:
    return len({row["链接"] for row in rows})


def _deleted_count(rows: list[dict]) -> int:
    return sum(1 for row in rows if row.get("删除时间"))


_YEAR_PATTERN = re.compile(r"\(\d{4}\)")


def _no_year_count(rows: list[dict]) -> int:
    return sum(1 for row in rows if not _YEAR_PATTERN.search(row["标题"] or ""))


def _non_url_count(rows: list[dict]) -> int:
    return sum(1 for row in rows if not row["链接"].startswith(("http://", "https://", "ed2k://")))


def _cross_type_collisions(rows: list[dict]) -> int:
    types_by_id: dict[int, set[str]] = {}
    for row in rows:
        types_by_id.setdefault(row["记录ID"], set()).add(row["类型"])
    return sum(1 for types in types_by_id.values() if len(types) > 1)


EXPECTED: dict = {
    "files": {
        "official": OFFICIAL_FILENAME,
        "supplement_video": SUPPLEMENT_VIDEO_FILENAME,
        "supplement_ed2k": SUPPLEMENT_ED2K_FILENAME,
    },
    "sheet_rows": {
        "official": {
            "官组UID": len(GROUP_UID_ROWS) - 1,
            "资源所有者": len(OWNER_ROWS) - 1,
            "分享明细": len(DETAIL_ROWS),
            "影视分享": len(VIDEO_ROWS),
            "音乐分享": len(MUSIC_ROWS),
            "追更条目": len(ZHUIGENG_ROWS),
            "磁力ED2K": len(ED2K_ROWS),
            "总览": len(OVERVIEW_ROWS) - 1,
        },
        "supplement_video": {"影视分享": len(SUPPLEMENT_VIDEO_ROWS)},
        "supplement_ed2k": {"磁力ED2K": len(SUPPLEMENT_ED2K_ROWS)},
    },
    "video_provider_counts": _provider_counts(VIDEO_ROWS),
    "video_unique_links": _unique_link_count(VIDEO_ROWS),
    "video_deleted_rows": _deleted_count(VIDEO_ROWS),
    "video_no_year_rows": _no_year_count(VIDEO_ROWS),
    "video_non_url_rows": _non_url_count(VIDEO_ROWS),
    "supplement_video_duplicate_rows": len(_supplement_video_duplicates),
    "supplement_video_new_rows": len(_supplement_video_new),
    "supplement_ed2k_duplicate_rows": len(_supplement_ed2k_duplicates),
    "supplement_ed2k_new_rows": len(_supplement_ed2k_new),
    "record_id_cross_type_collisions": _cross_type_collisions(DETAIL_ROWS),
}


# --------------------------------------------------------------------------
# Workbook writing.
# --------------------------------------------------------------------------


def _write_data_sheet(ws, rows: list[dict], *, banner_all_columns: bool) -> None:
    if banner_all_columns:
        for col in range(1, 13):
            ws.cell(row=1, column=col, value=BANNER_TEXT)
            ws.cell(row=2, column=col, value=NOTE_TEXT)
    else:
        ws.cell(row=1, column=1, value=BANNER_TEXT)
        ws.cell(row=2, column=1, value=NOTE_TEXT)
    # row 3 stays blank
    for col, header in enumerate(HEADERS, start=1):
        ws.cell(row=4, column=col, value=header)
    for r, row in enumerate(rows, start=5):
        for col, key in enumerate(HEADERS, start=1):
            ws.cell(row=r, column=col, value=row.get(key))


def _write_small_table(ws, rows: list[tuple]) -> None:
    for r, values in enumerate(rows, start=1):
        for col, value in enumerate(values, start=1):
            ws.cell(row=r, column=col, value=value)


def _build_official_workbook() -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)

    _write_small_table(wb.create_sheet("官组UID"), GROUP_UID_ROWS)
    _write_small_table(wb.create_sheet("资源所有者"), OWNER_ROWS)
    _write_data_sheet(wb.create_sheet("分享明细"), DETAIL_ROWS, banner_all_columns=True)
    _write_data_sheet(wb.create_sheet("影视分享"), VIDEO_ROWS, banner_all_columns=True)
    _write_data_sheet(wb.create_sheet("音乐分享"), MUSIC_ROWS, banner_all_columns=True)
    _write_data_sheet(wb.create_sheet("追更条目"), ZHUIGENG_ROWS, banner_all_columns=True)
    _write_data_sheet(wb.create_sheet("磁力ED2K"), ED2K_ROWS, banner_all_columns=True)
    _write_small_table(wb.create_sheet("总览"), OVERVIEW_ROWS)
    return wb


def _build_supplement_video_workbook() -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    _write_data_sheet(wb.create_sheet("影视分享"), SUPPLEMENT_VIDEO_ROWS, banner_all_columns=False)
    return wb


def _build_supplement_ed2k_workbook() -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    _write_data_sheet(wb.create_sheet("磁力ED2K"), SUPPLEMENT_ED2K_ROWS, banner_all_columns=False)
    return wb


def make_workbooks(dest: Path) -> dict[str, Path]:
    """Write the three synthetic workbooks into ``dest`` and return their paths.

    Deterministic: every call (in-process or across processes) produces
    byte-identical spreadsheets, because all row data is hardcoded above -
    nothing here reads the clock or a random source.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    paths = {
        "official": dest / OFFICIAL_FILENAME,
        "supplement_video": dest / SUPPLEMENT_VIDEO_FILENAME,
        "supplement_ed2k": dest / SUPPLEMENT_ED2K_FILENAME,
    }

    _build_official_workbook().save(paths["official"])
    _build_supplement_video_workbook().save(paths["supplement_video"])
    _build_supplement_ed2k_workbook().save(paths["supplement_ed2k"])

    return paths
