"""Parser for SAP ALV fixed-width `|`-delimited reports, yielding multiple wide tables.

The export is a sequence of *blocks*, each following the same pattern::

    block := sep+ header sep data+
    sep   := '-'+ | '|' '-'+ | '|' '-'+ '|'
    header:= '|' label ('|' label)*     # one record, possibly spanning several lines
    data  := '|' field ('|' field)*     # fixed-width field, may itself contain '|'

A file may contain several blocks (sort criteria, data statistics, and the
paginated main table). Each block is parsed independently; blocks sharing the
same header (pagination) are merged into a single table.

Wide grids are *line-wrapped into column bands* when the table is wider than
the page: the columns are split into consecutive bands and every record — the
header included — is rendered as one physical line per band, the identity
columns on the first band and further columns on the continuation bands. Such
blocks are detected and the bands are stitched back into single wide rows.

Cells may contain newlines (e.g. multi-line remarks): a record then spans several
physical lines, and a continuation line may or may not start with `|`. Record
boundaries are therefore detected by *column-boundary completeness* — a record is
complete once a `|` appears at every boundary position — rather than by whether a
line starts with `|`. Control characters (newlines etc.) count as zero-width.

Column alignment is auto-detected per block: some exports align by *display width*
(full-width CJK counts as 2 columns), others by *code points* (CJK counts as 1).
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    import pandas as pd

DEFAULT_THRESHOLD: float = 0.95  # coverage threshold for boundary inference

# A record whose physical lines exceed this many is force-flushed: real records
# are a handful of lines, and unbounded accumulation would be quadratic.
_MAX_RECORD_LINES = 64

_SEPARATOR_RE = re.compile(r"[-|]*-[-|]*")

# Number of `|`-starting lines sampled when detecting the alignment mode.
_MODE_SAMPLE = 20


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
    return _SEPARATOR_RE.fullmatch(line.strip()) is not None


def header_labels(header_line: str) -> list[str]:
    return [t.strip() for t in header_line.split("|") if t.strip()]


def _scan_pipes(
    line: str, mode: str
) -> tuple[list[int], list[int]]:
    """Walk a line once, returning ``(display_positions, code_indexes)`` of every '|'`.

    Printable-ASCII runs between pipes are measured in bulk (width == length);
    only runs containing control or non-ASCII characters are measured per char.
    """
    disp_positions: list[int] = []
    code_indexes: list[int] = []
    append_d = disp_positions.append
    append_c = code_indexes.append
    find = line.find
    disp = 0
    start = 0
    while (i := find("|", start)) >= 0:
        seg = line[start:i]
        if seg.isascii() and seg.isprintable():
            disp += i - start
        else:
            disp += sum(_char_width(c, mode) for c in seg)
        append_d(disp)
        append_c(i)
        disp += 1  # the pipe itself
        start = i + 1
    return disp_positions, code_indexes


def _pipe_positions(line: str, mode: str = "display") -> list[int]:
    """Positions of every '|' in a line, in the given width mode (ascending)."""
    return _scan_pipes(line, mode)[0]


def _match_pipes(
    pipe_disp: Sequence[int], pipe_code: Sequence[int], boundaries: Sequence[int]
) -> dict[int, int]:
    """Map each boundary position to the code index of a '|' within ±1 (two-pointer).

    ``pipe_disp`` and ``boundaries`` must be ascending; unmatched boundaries are
    omitted from the mapping.
    """
    mapping: dict[int, int] = {}
    npipes = len(pipe_disp)
    j = 0
    for b in boundaries:
        while j < npipes and pipe_disp[j] < b - 1:
            j += 1
        if j < npipes and pipe_disp[j] <= b + 1:
            mapping[b] = pipe_code[j]
    return mapping


def _nearest_pipe(
    pipe_disp: Sequence[int], pipe_code: Sequence[int], b: int
) -> int | None:
    """Code index of the pipe closest to position ``b`` (fallback for drift)."""
    if not pipe_disp:
        return None
    k = bisect_left(pipe_disp, b)
    best = min(
        (c for c in (k - 1, k) if 0 <= c < len(pipe_disp)),
        key=lambda c: abs(pipe_disp[c] - b),
        default=None,
    )
    return None if best is None else pipe_code[best]


def _display_to_code(
    line: str, positions: Sequence[int], mode: str = "display", strict: bool = False
) -> dict[int, int]:
    """Map boundary positions to the nearest '|' code index (±1 tolerance).

    With ``strict=True`` only boundaries with a pipe within ±1 are mapped, which
    callers use to validate alignment. Otherwise unmatched boundaries fall back
    to the nearest pipe overall so drifted lines still split instead of crashing.
    """
    pipe_disp, pipe_code = _scan_pipes(line, mode)
    boundaries = sorted(positions)
    mapping = _match_pipes(pipe_disp, pipe_code, boundaries)
    if not strict:
        for b in boundaries:
            if b not in mapping:
                near = _nearest_pipe(pipe_disp, pipe_code, b)
                if near is not None:
                    mapping[b] = near
    return mapping


def split_row(line: str, boundaries: Sequence[int], mode: str = "display") -> list[str]:
    """Split one line into cells by boundary positions (stripped).

    Cells whose boundary has no pipe nearby (misaligned input) come out empty
    rather than raising.
    """
    code = _display_to_code(line, boundaries, mode)
    cols: list[str] = []
    for k in range(len(boundaries) - 1):
        a, b = code.get(boundaries[k]), code.get(boundaries[k + 1])
        cols.append(line[a + 1 : b].strip() if a is not None and b is not None else "")
    last = code.get(boundaries[-1])
    cols.append(line[last + 1 :].strip() if last is not None else "")
    return cols


def _covered(boundaries: Sequence[int], positions: Sequence[int]) -> bool:
    """True when every boundary has a pipe within ±1 (both sequences ascending)."""
    j = 0
    n = len(positions)
    for b in boundaries:
        while j < n and positions[j] < b - 1:
            j += 1
        if j >= n or positions[j] > b + 1:
            return False
    return True


def _reassemble(
    data_lines: Sequence[str], boundaries: Sequence[int], mode: str = "display"
) -> list[str]:
    """Reassemble physical lines into complete records.

    A record is complete once a `|` appears near every boundary position.
    A multi-line cell makes a record span several physical lines, and the
    continuation line may or may not start with `|`, so we cannot rely on "line
    starts with `|`" — we rely on boundary completeness instead. Accumulations
    that never complete are force-flushed after ``_MAX_RECORD_LINES`` lines.
    """
    boundaries_sorted = sorted(set(boundaries))
    records: list[str] = []
    current: str | None = None
    nlines = 0
    for line in data_lines:
        current = line if current is None else current + "\n" + line
        nlines += 1
        positions = _pipe_positions(current, mode)
        # every boundary position must have a `|` nearby (±1), tolerating padding drift
        if _covered(boundaries_sorted, positions) or nlines >= _MAX_RECORD_LINES:
            records.append(current)
            current = None
            nlines = 0
    if current is not None:  # trailing incomplete record (kept as-is)
        records.append(current)
    return records


def _detect_mode(header_line: str, data_lines: Sequence[str]) -> str:
    """Detect the alignment convention by comparing header vs data record starts.

    Some exports align columns by display width (CJK counts as 2), others by code
    points (CJK counts as 1). The header and data agree in the correct convention,
    so compare their leading `|` positions in each convention and keep the one
    with more matches. Up to ``_MODE_SAMPLE`` `|`-starting data lines vote, so a
    block whose first record happens to be pure ASCII still detects correctly.
    """
    starts = [l for l in data_lines if l.startswith("|")][:_MODE_SAMPLE]
    if not starts:
        return "display"
    best_mode, best_score = "display", -1
    for mode in ("code", "display"):
        hp = _pipe_positions(header_line, mode)
        score = 0
        for ref in starts:
            dp = _pipe_positions(ref, mode)
            m = min(len(hp), len(dp))
            score += sum(1 for k in range(m) if hp[k] == dp[k])
        if score > best_score:
            best_mode, best_score = mode, score
    return best_mode


def _split_band_line(
    line: str,
    boundaries: Sequence[int],
    has_leading: bool,
    has_trailing: bool,
    mode: str,
) -> list[str] | None:
    """Split one physical line of a column band into its cells.

    ``has_leading``/``has_trailing`` add the cells before the first and after the
    last boundary pipe (continuation bands carry no leading pipe; the last band's
    right edge is open when the grid reaches the page width). Returns ``None``
    when some boundary has no pipe within ±1 — the line does not belong to this
    band layout.
    """
    code = _display_to_code(line, boundaries, mode, strict=True)
    if len(code) != len(boundaries):
        return None
    cells: list[str] = []
    if has_leading:
        cells.append(line[: code[boundaries[0]]].strip())
    for a, b in zip(boundaries, boundaries[1:]):
        cells.append(line[code[a] + 1 : code[b]].strip())
    if has_trailing:
        tail = line[code[boundaries[-1]] + 1 :]
        stripped = tail.rstrip()
        if stripped.endswith("|"):  # right grid edge pipe without a header twin
            tail = stripped[:-1]
        cells.append(tail.strip())
    return cells


def _band_layouts(
    header_lines: Sequence[str], mode: str
) -> list[tuple[list[int], bool, bool]] | None:
    """Per-band column layout: ``(boundaries, has_leading, has_trailing)``."""
    layouts: list[tuple[list[int], bool, bool]] = []
    for h in header_lines:
        boundaries = _pipe_positions(h, mode)
        if not boundaries:
            return None
        last_pipe = h.rfind("|")
        has_trailing = last_pipe >= 0 and bool(h[last_pipe + 1 :].strip())
        layouts.append((boundaries, boundaries[0] > 0, has_trailing))
    return layouts


def _try_parse_banded(
    header_lines: Sequence[str], data_lines: Sequence[str], mode: str
) -> Table | None:
    """Parse a block whose records are line-wrapped into per-column bands.

    Returns ``None`` (falling back to the flat path) unless the header spans
    exactly ``k >= 2`` lines and the data consists of ``k``-line records: every
    record's first band starts with ``|``, continuation bands do not, and every
    line carries a pipe within ±1 of each of its band's header boundaries.
    """
    k = len(header_lines)
    if k < 2 or len(data_lines) < k:
        return None

    layouts = _band_layouts(header_lines, mode)
    if layouts is None:
        return None

    labels: list[str] = []
    for h, (boundaries, has_leading, has_trailing) in zip(header_lines, layouts):
        cells = _split_band_line(h, boundaries, has_leading, has_trailing, mode)
        if cells is None:
            return None
        labels.extend(cells)

    rows: list[list[str]] = []
    n = len(data_lines)
    for start in range(0, n - k + 1, k):
        record = data_lines[start : start + k]
        if not record[0].startswith("|"):
            return None
        if any(l.startswith("|") for l in record[1:]):
            return None
        cells: list[str] = []
        for line, (boundaries, has_leading, has_trailing) in zip(record, layouts):
            parts = _split_band_line(line, boundaries, has_leading, has_trailing, mode)
            if parts is None:
                return None
            cells.extend(parts)
        rows.append(cells)

    # a trailing partial record (truncated page) is dropped; anything else
    # irregular in the remainder means this is not a banded block
    remainder = data_lines[n - n % k :] if n % k else []
    if remainder and not remainder[0].startswith("|"):
        return None

    # No phantom-column drop here: the column structure comes from the header
    # band layout (leading/trailing cells), so a genuinely empty last column on
    # a sparse page must survive for pagination to merge.
    ncols = len(rows[0]) if rows else 0

    if len(labels) != ncols:
        labels = [f"col_{i}" for i in range(ncols)]
    return Table(labels, rows)


def _parse_single_block(
    header_lines: Sequence[str], data_lines: Sequence[str], threshold: float
) -> Table | None:
    """Parse a single block (header lines + data lines) into a Table."""
    if not data_lines:
        return None

    mode = _detect_mode(header_lines[0], data_lines)

    if len(header_lines) >= 2:
        banded = _try_parse_banded(header_lines, data_lines, mode)
        if banded is not None:
            return banded

    header_line = header_lines[0]
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
) -> list[tuple[list[str], list[str]]]:
    """Split a file into blocks, each ``(header_lines, data_lines)``.

    Lines are grouped into segments separated by separator lines; a segment
    directly preceded by a separator and followed by another segment is a header
    (single- or multi-line — line-wrapped headers included), and the segment
    after it is that block's data. Segments therefore alternate header/data,
    which also merges a page's repeated header into the next block boundary.
    """
    n = len(lines)

    # maximal runs of non-separator, non-blank lines
    segments: list[tuple[int, int]] = []  # [start, end) line indexes
    i = 0
    while i < n:
        if sep[i] or not lines[i].strip():
            i += 1
            continue
        j = i + 1
        while j < n and not sep[j] and lines[j].strip():
            j += 1
        segments.append((i, j))
        i = j

    blocks: list[tuple[list[str], list[str]]] = []
    k = 0
    while k < len(segments):
        hs, he = segments[k]
        if hs == 0 or not sep[hs - 1] or k + 1 >= len(segments):
            k += 1  # not preceded by a separator, or no data segment after it
            continue
        ds, de = segments[k + 1]
        blocks.append((list(lines[hs:he]), list(lines[ds:de])))
        k += 2
    return blocks


def parse_blocks(path: str | Path, threshold: float = DEFAULT_THRESHOLD) -> list[Table]:
    """Parse all blocks in a file, returning one Table per distinct header (pages merged)."""
    with open(path, "rb") as f:
        raw = f.read().decode("utf-8")
    lines = raw.split("\r\n") if "\r\n" in raw else raw.split("\n")
    sep = [is_separator(l) for l in lines]

    blocks = split_into_blocks(lines, sep)

    grouped: dict[tuple[str, ...], Table] = {}  # column-name signature -> Table
    for header_lines, data_lines in blocks:
        t = _parse_single_block(header_lines, data_lines, threshold)
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
