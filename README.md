# sap-parser

Parser for SAP ALV fixed-width `|`-delimited reports. It turns "unconverted" /
text-format SAP exports into one or more wide tables, merges pagination
automatically, and hard-codes no column names, column counts, or column widths.

## Features

- **Zero configuration** — column boundaries, headers, and pagination are all
  inferred automatically; no column names, widths, or counts need to be supplied.
- **Multiple blocks** — a file's sort-criteria block, data-statistics block, and
  main data table are each parsed into their own table.
- **CJK friendly** — full-width characters (CJK) are normalized by display width
  (2 columns), so CJK names do not shift column positions.
- **`|` inside cells** — multi-value fields (e.g. shifts) that use `|` internally
  are not confused with column separators.
- **Multi-line cells** — cells containing newlines (e.g. multi-line remarks) are
  reassembled correctly, whether the newline falls at the start, middle, or end
  of the cell.
- **DataFrame-style interface** — every block is a `Table` that can be converted
  to a pandas DataFrame via `to_pandas()`.

## Install

```bash
uv add sap-parser       # as a dependency
# or, for local development
uv sync                 # installs dependencies (pandas / numpy / pytest)
```

Requirements: Python ≥ 3.13, `pandas`, `numpy`.

## Quick start

```python
from sap_parser import parse_blocks, parse_table

# parse all blocks (each becomes a wide table)
tables = parse_blocks("report.txt")
for t in tables:
    print(t.shape, t.columns)

# get the main table (the one with the most rows)
main = parse_table("report.txt")
df = main.to_pandas()          # pandas.DataFrame
```

Command line:

```bash
uv run sap-parse report.txt -o output.csv --all
```

## Recognized grammar

These exports are a sequence of *blocks*, each following the same pattern:

```
report := stats_block? page+
page   := sep_line header_line sep_line data_line+
sep    := '-'+ | '|' '-'+ | '|' '-'+ '|'
header := '|' label ('|' label)*
data   := '|' field ('|' field)*      # fixed-width field, may itself contain '|'
```

## How it works

1. **Single-block parsing** — normalize by display width → infer column boundaries
   from `|` coverage → drop the phantom column created by a trailing edge `|`.
2. **Multi-block recognition** — a header is the `|` line sandwiched between two
   separator lines and immediately followed by a data line (content-independent);
   blocks sharing the same header (pagination) are merged into one table.
3. **Record reassembly** — records whose cells contain newlines span several
   physical lines; a record is complete once a `|` appears at every column
   boundary position.

## `Table` interface

| Capability | Usage | Description |
|---|---|---|
| Columns | `t.columns` | `list[str]` |
| Shape | `t.shape` | `(rows, cols)` |
| Length | `len(t)` | `int` |
| Select column | `t['Amount']` | `list[str]` |
| Select columns | `t[['A','B']]` | sub-`Table` |
| Head | `t.head(3)` | first 3 rows |
| Records | `t.to_dict()` | `list[dict]` |
| Export | `t.to_csv('x.csv')` | CSV file |
| DataFrame | `t.to_pandas()` | `pandas.DataFrame` |

## Scope and limitations

Supports SAP ALV grid "unconverted" / text exports in the `|`-delimited
fixed-width format.

**Not automatically covered** (requires separate adapters):

- tab-delimited, HTML, or XLSX exports
- fixed-width text with no `|` delimiters (space-aligned)

## Testing

> **Note:** the real SAP export files used as test fixtures have been removed
> from this repository because they contained sensitive production data. All
> tests now build synthetic fixtures inline and run without any external data.

```bash
uv run pytest
```

## Project structure

```
src/sap_parser/
├── __init__.py     # exports Table / parse_blocks / parse_table
├── parser.py       # core parsing logic
└── cli.py          # sap-parse command line
tests/
└── test_parser.py  # tests (synthetic fixtures only)
```
