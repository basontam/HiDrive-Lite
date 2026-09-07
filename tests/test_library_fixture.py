"""Tests for the synthetic media-library workbook fixtures (T0.4).

The real HDHive / 影巢 workbooks are sensitive and never touch this repo.
``fixtures/library/make_workbooks.py`` generates three fictional workbooks
that reproduce the real layout (banner row, note row, blank row, 12-column
header on row 4, data from row 5) so later importer tests have something
safe and deterministic to run against.
"""

from __future__ import annotations

import re

from openpyxl import load_workbook

from fixtures.library.make_workbooks import EXPECTED, HEADERS, make_workbooks

# The 12-column data tables that share the banner/note/blank/header layout.
# (官组UID / 资源所有者 / 总览 are small reference sheets, not data tables.)
DATA_SHEETS_BY_FILE = {
    "official": ["分享明细", "影视分享", "音乐分享", "追更条目", "磁力ED2K"],
    "supplement_video": ["影视分享"],
    "supplement_ed2k": ["磁力ED2K"],
}

FILE_NAMES = {
    "official": "HDHive official-group-backup.xlsx",
    "supplement_video": "影巢_影视分享_整理.xlsx",
    "supplement_ed2k": "影巢_ED2K_整理.xlsx",
}


def _data_rows(ws):
    """Rows 5..end as a list of (12-cell) tuples, trimmed of fully-empty trailing rows."""
    rows = []
    for row in ws.iter_rows(min_row=5, max_col=12, values_only=True):
        if all(value is None or value == "" for value in row):
            continue
        rows.append(row)
    return rows


def test_creates_three_files_with_expected_names(library_sources):
    for key, name in FILE_NAMES.items():
        path = library_sources / name
        assert path.is_file(), f"missing {key} workbook at {path}"


def test_header_row_matches_layout_on_every_data_table(tmp_path):
    paths = make_workbooks(tmp_path / "sources")
    for key, sheets in DATA_SHEETS_BY_FILE.items():
        wb = load_workbook(paths[key])
        for sheet_name in sheets:
            ws = wb[sheet_name]
            header = [ws.cell(row=4, column=col).value for col in range(1, 13)]
            assert header == HEADERS, f"{key}/{sheet_name} header row mismatch: {header}"
            # row 3 must be entirely empty
            row3 = [ws.cell(row=3, column=col).value for col in range(1, 13)]
            assert all(value in (None, "") for value in row3), f"{key}/{sheet_name} row 3 not blank"


def test_video_sheet_row_count_matches_expected(tmp_path):
    paths = make_workbooks(tmp_path / "sources")
    wb = load_workbook(paths["official"])
    rows = _data_rows(wb["影视分享"])
    assert len(rows) == EXPECTED["sheet_rows"]["official"]["影视分享"]
    assert 40 <= len(rows) <= 60


def test_official_video_sheet_type_is_all_video(tmp_path):
    paths = make_workbooks(tmp_path / "sources")
    wb = load_workbook(paths["official"])
    rows = _data_rows(wb["影视分享"])
    assert rows, "expected at least one 影视分享 row"
    for row in rows:
        assert row[0] == "影视"


def test_ed2k_sheets_start_with_scheme(tmp_path):
    paths = make_workbooks(tmp_path / "sources")
    wb_official = load_workbook(paths["official"])
    wb_ed2k = load_workbook(paths["supplement_ed2k"])
    for ws in (wb_official["磁力ED2K"], wb_ed2k["磁力ED2K"]):
        rows = _data_rows(ws)
        assert rows, f"expected ED2K rows in {ws.title}"
        for row in rows:
            link = row[7]  # 链接
            assert isinstance(link, str) and link.startswith("ed2k://"), link


def test_access_codes_are_short(tmp_path):
    paths = make_workbooks(tmp_path / "sources")
    for key, sheets in DATA_SHEETS_BY_FILE.items():
        wb = load_workbook(paths[key])
        for sheet_name in sheets:
            for row in _data_rows(wb[sheet_name]):
                code = row[8]  # 访问码
                assert code is None or len(str(code)) <= 4, (key, sheet_name, code)


def test_links_never_carry_a_real_share_code_pattern(tmp_path):
    paths = make_workbooks(tmp_path / "sources")
    pattern = re.compile(r"password=([A-Za-z0-9]+)")
    for key, sheets in DATA_SHEETS_BY_FILE.items():
        wb = load_workbook(paths[key])
        for sheet_name in sheets:
            for row in _data_rows(wb[sheet_name]):
                link = row[7]
                if not isinstance(link, str):
                    continue
                match = pattern.search(link)
                if match:
                    assert len(match.group(1)) <= 4, link


def test_generation_is_deterministic(tmp_path):
    paths_a = make_workbooks(tmp_path / "a")
    paths_b = make_workbooks(tmp_path / "b")
    assert paths_a.keys() == paths_b.keys()
    for key in paths_a:
        wb_a = load_workbook(paths_a[key])
        wb_b = load_workbook(paths_b[key])
        assert wb_a.sheetnames == wb_b.sheetnames
        for sheet_name in wb_a.sheetnames:
            ws_a, ws_b = wb_a[sheet_name], wb_b[sheet_name]
            rows_a = list(ws_a.iter_rows(values_only=True))
            rows_b = list(ws_b.iter_rows(values_only=True))
            assert rows_a == rows_b, f"{key}/{sheet_name} differs between runs"
