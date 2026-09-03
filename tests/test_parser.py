"""Tests for sap_alv_parser, using synthetic SAP ALV export fixtures.

Real export data is sensitive and has been removed from this repository; every
test builds its own fixture inline.
"""

import unicodedata
from pathlib import Path

import pandas as pd
import pytest

from sap_alv_parser import parse_blocks, parse_table


def _display_width(s: str) -> int:
    return sum(
        0 if ord(c) < 32 else (2 if unicodedata.east_asian_width(c) in "FW" else 1)
        for c in s
    )


def _pad(s: str, w: int) -> str:
    return s + " " * (w - _display_width(s))


def _row(values: list[str], widths: list[int]) -> str:
    return "|" + "|".join(_pad(v, w) for v, w in zip(values, widths)) + "|"


# Main-table column widths and header (shared by the report fixtures).
WIDTHS = [7, 8, 10, 25, 25]
HEADER = ["ID", "Name", "Dept", "20260831", "20260830"]


def build_report(path: Path) -> None:
    """Build a report with a statistics block and a two-page main table.

    ``Name`` uses CJK characters to exercise full-width (display-width) handling;
    shift cells use ``|`` internally to exercise multi-segment values.
    """
    sep = "-" * 80
    pipe_sep = "|" + "-" * 79
    stats_h = "|" + "|".join(_pad(h, w) for h, w in zip(["Data statistics", "Number of"], [15, 10])) + "|"
    stats_d = "|" + "|".join(_pad(d, w) for d, w in zip(["Records passed", "3"], [15, 10])) + "|"
    stats_sep = "-" * 28
    stats_pipe_sep = "|" + "-" * 26

    lines = [
        stats_sep, stats_h, stats_pipe_sep, stats_d, stats_sep,
        # page 1
        sep, _row(HEADER, WIDTHS), pipe_sep,
        _row(["1001", "张三", "D01", "07:00-17:00", "07:00-13:30|13:30-19:00"], WIDTHS),
        _row(["1002", "李四", "D02", "", "09:00-18:00"], WIDTHS),
        # page 2
        sep, _row(HEADER, WIDTHS), pipe_sep,
        _row(["1003", "王五", "D03", "07:00-19:00", ""], WIDTHS),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def build_multiline_report(path: Path) -> None:
    """Build a report whose remark column contains newlines at the start, middle,
    and end of a cell."""
    cols = [("ID", 7), ("Name", 8), ("Remark", 24), ("Date", 8)]
    hdr = "|" + "|".join(_pad(n, w) for n, w in cols) + "|"
    sep = "-" * 50
    pipe_sep = "|" + "-" * 49
    rows = [
        _row(["001", "张三", "first line\nsecond line", "20260831"], [7, 8, 24, 8]),  # middle
        _row(["002", "李四", "newline at end\n", "20260830"], [7, 8, 24, 8]),          # end
        _row(["003", "王五", "\nnewline at start", "20260829"], [7, 8, 24, 8]),        # start
    ]
    path.write_text("\n".join([sep, hdr, pipe_sep, *rows, sep]), encoding="utf-8")


# ---- block detection ----

def test_blocks_and_statistics(tmp_path):
    f = tmp_path / "report.txt"
    build_report(f)
    tables = parse_blocks(f)
    by_cols = {tuple(t.columns): t for t in tables}
    # statistics block
    assert ("Data statistics", "Number of") in by_cols
    assert by_cols[("Data statistics", "Number of")].rows == [["Records passed", "3"]]
    # main table (two pages merged into one)
    assert tuple(HEADER) in by_cols
    assert by_cols[tuple(HEADER)].shape == (3, 5)


def test_main_table(tmp_path):
    f = tmp_path / "report.txt"
    build_report(f)
    t = parse_table(f)
    assert t.columns == HEADER
    assert t.shape == (3, 5)
    assert len(set(t["ID"])) == 3  # no duplicate IDs across pages


# ---- CJK names and multi-segment cells ----

def test_cjk_names_and_multi_segment(tmp_path):
    f = tmp_path / "report.txt"
    build_report(f)
    t = parse_table(f)
    assert t["Name"] == ["张三", "李四", "王五"]  # full-width names are not misaligned
    row0 = t.to_dict()[0]
    assert row0["20260830"] == "07:00-13:30|13:30-19:00"  # internal `|` preserved


# ---- multi-line cells ----

def test_multiline_cells(tmp_path):
    f = tmp_path / "multiline.txt"
    build_multiline_report(f)
    t = parse_table(f)
    assert t.columns == ["ID", "Name", "Remark", "Date"]
    assert t["Remark"][0] == "first line\nsecond line"  # newline in the middle
    assert t["Remark"][1] == "newline at end"           # newline at cell end
    assert t["Remark"][2] == "newline at start"         # newline at cell start
    assert t["Date"] == ["20260831", "20260830", "20260829"]


# ---- DataFrame interface ----

def test_to_pandas(tmp_path):
    f = tmp_path / "report.txt"
    build_report(f)
    df = parse_table(f).to_pandas()
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == HEADER
    assert df.shape == (3, 5)


def test_column_access_and_slice(tmp_path):
    f = tmp_path / "report.txt"
    build_report(f)
    t = parse_table(f)
    assert t["ID"] == ["1001", "1002", "1003"]
    sub = t[["ID", "Name"]]
    assert sub.columns == ["ID", "Name"]
    assert sub.rows[0] == ["1001", "张三"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
