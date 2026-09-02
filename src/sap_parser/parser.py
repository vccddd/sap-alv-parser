"""Parser for SAP ALV fixed-width `|`-delimited reports, yielding multiple wide tables.

The export is a sequence of *blocks*, each following the same pattern::

    block := sep_line  header_line  sep_line  data_line+

    sep_line := '-'+ | '|' '-'+ | '|' '-'+ '|'
    header   := '|' label ('|' label)*
    data     := '|' field ('|' field)*        # fixed-width field, may itself contain '|'

A file may contain several blocks (sort criteria, data statistics, and the
paginated main table). Each block is parsed independently; blocks sharing the
same header (pagination) are merged into a single table.

Cells may contain newlines (e.g. multi-line remarks): a record then spans several
physical lines, and a continuation line may or may not start with `|`. Record
boundaries are therefore detected by *column-boundary completeness* — a record is
complete once a `|` appears at every boundary position — rather than by whether a
line starts with `|`. Control characters (newlines etc.) count as zero-width.

Column alignment is auto-detected per block: some exports align by *display width*
(full-width CJK counts as 2 columns), others by *code points* (CJK counts as 1).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    import pandas as pd

DEFAULT_THRESHOLD: float = 0.95  # coverage threshold for boundary inference


class Table:
    """A lightweight wide table: column names + rows; exports to CSV or pandas.

    DataFrame-like interface: ``t.columns`` / ``t.shape`` / ``len(t)`` / ``t['col']`` /
    ``t[['a','b']]`` / ``t.head()`` / ``t.to_dict()`` / ``t.to_csv()`` / ``t.to_pandas()``.
    """

    def __init__(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[str]],
        meta: dict[str, object] | None = None,
    ) -> None:
        self.columns: list[str] = list(columns)
        self.rows: list[list[str]] = [list(r) for r in rows]
        self.meta: dict[str, object] = meta or {}

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:
        return f"Table(shape={self.shape}, columns={self.columns})"

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.rows), len(self.columns))

    def head(self, n: int = 5) -> Table:
        return Table(self.columns, self.rows[:n], self.meta)

    @overload
    def __getitem__(self, key: str) -> list[str]: ...

    @overload
    def __getitem__(self, key: list[str] | tuple[str, ...]) -> Table: ...

    def __getitem__(self, key: str | list[str] | tuple[str, ...]) -> list[str] | Table:
        """Select by column name: ``t['col']`` -> values, ``t[['a','b']]`` -> sub-table."""
        if isinstance(key, str):
            return [r[self.columns.index(key)] for r in self.rows]
        if isinstance(key, (list, tuple)):
            idx = [self.columns.index(k) for k in key]
            return Table(
                [self.columns[i] for i in idx],
                [[r[i] for i in idx] for r in self.rows],
                self.meta,
            )
        raise TypeError("key must be a column name or a list of column names")

    def to_dict(self) -> list[dict[str, str]]:
        """Return rows as ``list[dict]`` (records orientation)."""
        return [dict(zip(self.columns, r)) for r in self.rows]

    def to_csv(self, path: str | Path) -> str:
        import csv

        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(self.columns)
            w.writerows(self.rows)
        return str(path)

    def to_pandas(self) -> pd.DataFrame:
        """Convert to a pandas DataFrame."""
        import pandas as pd

        return pd.DataFrame(self.rows, columns=self.columns)

    def to_prettytable(self, replace_pipe: str | None = None, **kwargs):
        """Convert to a prettytable.PrettyTable.

        ``replace_pipe``, when given, substitutes cell-internal ``|`` (used to
        separate multi-segment values) so it does not visually collide with the
        table's own column separators. Extra ``**kwargs`` go to ``PrettyTable``.
        """
        import prettytable

        pt = prettytable.PrettyTable(field_names=self.columns, **kwargs)
        for row in self.rows:
            if replace_pipe is not None:
                row = [c.replace("|", replace_pipe) for c in row]
            pt.add_row(row)
        return pt



def _char_width(c: str, mode: str = "display") -> int:
    if ord(c) < 32:  # control characters (\n \r \t) are zero-width
        return 0
    if mode == "display" and unicodedata.east_asian_width(c) in ("F", "W"):
        return 2
    return 1


def display_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def is_separator(line: str) -> bool:
    """A separator line consists only of '-'/'|' and contains at least one '-'."""
    s = line.strip()
    return bool(s) and ("-" in s) and all(c in "-|" for c in s)


def header_labels(header_line: str) -> list[str]:
    return [t.strip() for t in header_line.split("|") if t.strip()]


def _pipe_positions(line: str, mode: str = "display") -> list[int]:
    """Positions of every '|' in a line, in the given width mode (ascending)."""
    positions: list[int] = []
    disp = 0
    for ch in line:
        if ch == "|":
            positions.append(disp)
        disp += _char_width(ch, mode)
    return positions



def _display_to_code(
    line: str, positions: Sequence[int], mode: str = "display"
) -> dict[int, int]:
    """Map boundary positions to the nearest '|' code index (±1 tolerance)."""
    mapping: dict[int, int] = {}
    pipes: list[tuple[int, int]] = []  # (position, code index)
    disp = 0
    for j, ch in enumerate(line):
        if ch == "|":
            pipes.append((disp, j))
        disp += _char_width(ch, mode)
    for b in positions:
        best = min(pipes, key=lambda pj: abs(pj[0] - b), default=None)
        if best is not None and abs(best[0] - b) <= 1:
            mapping[b] = best[1]
    return mapping


def split_row(line: str, boundaries: Sequence[int], mode: str = "display") -> list[str]:
    """Split one line into cells by boundary positions (stripped)."""
    code = _display_to_code(line, boundaries, mode)
    cols: list[str] = []
    for k in range(len(boundaries) - 1):
        cols.append(line[code[boundaries[k]] + 1 : code[boundaries[k + 1]]].strip())
    cols.append(line[code[boundaries[-1]] + 1 :].strip())
    return cols


def _reassemble(
    data_lines: Sequence[str], boundaries: Sequence[int], mode: str = "display"
) -> list[str]:
    """Reassemble physical lines into complete records.

    A record is complete once a `|` appears near every boundary position.
    A multi-line cell makes a record span several physical lines, and the
    continuation line may or may not start with `|`, so we cannot rely on "line
    starts with `|`" — we rely on boundary completeness instead.
    """
    boundaries_sorted = sorted(set(boundaries))
    records: list[str] = []
    current: str | None = None
    for line in data_lines:
        current = line if current is None else current + "\n" + line
        positions = set(_pipe_positions(current, mode))
        # every boundary position must have a `|` nearby (±1), tolerating padding drift
        if all(any(abs(b - p) <= 1 for p in positions) for b in boundaries_sorted):
            records.append(current)
            current = None
    if current is not None:  # trailing incomplete record (kept as-is)
        records.append(current)
    return records


def _detect_mode(header_line: str, data_lines: Sequence[str]) -> str:
    """Detect the alignment convention by comparing header vs a data record start.

    Some exports align columns by display width (CJK counts as 2), others by code
    points (CJK counts as 1). The header and data agree in the correct convention,
    so compare their leading `|` positions in each convention and keep the one
    with more matches. Uses the header (single-line, clean) so it works even when
    every row is a multi-line cell.
    """
    starts = [l for l in data_lines if l.startswith("|")]
    if not starts:
        return "display"
    ref = starts[0]
    best_mode, best_score = "display", -1
    for mode in ("code", "display"):
        hp = _pipe_positions(header_line, mode)
        dp = _pipe_positions(ref, mode)
        m = min(len(hp), len(dp))
        score = sum(1 for k in range(m) if hp[k] == dp[k])
        if score > best_score:
            best_mode, best_score = mode, score
    return best_mode


def _parse_single_block(
    header_line: str, data_lines: Sequence[str], threshold: float
) -> Table | None:
    """Parse a single block (header + its data lines) into a Table."""
    if not data_lines:
        return None

    mode = _detect_mode(header_line, data_lines)
    boundaries = _pipe_positions(header_line, mode)
    if not boundaries:
        return None

    records = _reassemble(data_lines, boundaries, mode)
    rows = [split_row(r, boundaries, mode) for r in records]

    # trailing edge `|` creates a phantom column: drop it if empty in every row
    if rows and all(r[-1] == "" for r in rows):
        boundaries = boundaries[:-1]
        rows = [r[:-1] for r in rows]

    ncols = len(boundaries)
    labels = header_labels(header_line)
    if len(labels) != ncols:
        labels = [f"col_{i}" for i in range(ncols)]
    return Table(labels, rows)


def split_into_blocks(
    lines: Sequence[str], sep: Sequence[bool]
) -> list[tuple[str, list[str]]]:
    """Split a file into blocks, each ``(header_line, data_lines)``."""
    n = len(lines)

    # A header is a `|` line sandwiched between two separators whose closing
    # separator is immediately followed by a data line. The last condition
    # distinguishes the header from the lone data row of a single-row block.
    header_idx: list[int] = []
    for i in range(1, n - 2):
        l = lines[i]
        if (
            l.startswith("|")
            and not sep[i]
            and sep[i - 1]
            and sep[i + 1]
            and lines[i + 2].startswith("|")
            and not sep[i + 2]
        ):
            header_idx.append(i)

    header_set = set(header_idx)
    blocks: list[tuple[str, list[str]]] = []
    for k, hi in enumerate(header_idx):
        end = header_idx[k + 1] if k + 1 < len(header_idx) else n
        # collect all non-separator, non-blank lines (including multi-line continuations)
        data = [
            lines[j]
            for j in range(hi + 1, end)
            if not sep[j] and lines[j].strip() and j not in header_set
        ]
        blocks.append((lines[hi], data))
    return blocks


def parse_blocks(path: str | Path, threshold: float = DEFAULT_THRESHOLD) -> list[Table]:
    """Parse all blocks in a file, returning one Table per distinct header (pages merged)."""
    with open(path, "rb") as f:
        raw = f.read().decode("utf-8")
    lines = raw.split("\r\n") if "\r\n" in raw else raw.split("\n")
    sep = [is_separator(l) for l in lines]

    blocks = split_into_blocks(lines, sep)

    grouped: dict[tuple[str, ...], Table] = {}  # column-name signature -> Table
    for header_line, data_lines in blocks:
        t = _parse_single_block(header_line, data_lines, threshold)
        if t is None:
            continue
        key = tuple(t.columns)
        if key in grouped:
            grouped[key].rows.extend(t.rows)
        else:
            grouped[key] = t
    return list(grouped.values())


def parse_table(path: str | Path, threshold: float = DEFAULT_THRESHOLD) -> Table:
    """Return the table with the most rows (usually the main data table)."""
    tables = parse_blocks(path, threshold)
    if not tables:
        raise ValueError(f"no data blocks parsed from {path}")
    return max(tables, key=len)
