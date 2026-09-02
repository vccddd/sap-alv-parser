"""CLI entry point: parse a SAP export file into multiple wide tables."""

from __future__ import annotations

import argparse
import os

from .parser import DEFAULT_THRESHOLD, parse_blocks


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parse SAP ALV fixed-width `|` reports into multiple blocks"
    )
    ap.add_argument("input", help="input file path")
    ap.add_argument("-o", "--output", default="table.csv", help="CSV path for the largest table")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="coverage threshold for column boundary inference (default %(default)s)")
    ap.add_argument("--all", action="store_true", help="write each block to a separate CSV")
    args = ap.parse_args()

    tables = parse_blocks(args.input, threshold=args.threshold)
    if not tables:
        raise SystemExit("no data blocks parsed.")

    print(f"Found {len(tables)} block group(s):")
    for i, t in enumerate(tables):
        print(f"  [{i}] {t.shape[0]} rows x {t.shape[1]} cols  columns: {t.columns}")

    main = max(tables, key=len)
    main.to_csv(args.output)
    print(f"\nLargest table -> {args.output}: {main.shape[0]} rows x {main.shape[1]} cols")

    if args.all:
        stem, ext = os.path.splitext(args.output)
        for i, t in enumerate(tables):
            p = f"{stem}.block{i}{ext}"
            t.to_csv(p)
            print(f"  block {i} -> {p}")


if __name__ == "__main__":
    main()
