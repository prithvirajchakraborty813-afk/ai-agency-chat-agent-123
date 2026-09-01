#!/usr/bin/env python3
"""
merge_prospects.py — Combines two or more prospects.csv-shaped files into
one, deduping on the `domain` column (first occurrence wins). Meant for
merging gemini_search_finder.py output with gemini_maps_finder.py output
before running gemini_vertex_qualifier.py on the combined set.

Usage:
    python merge_prospects.py --out prospects_merged.csv prospects.csv prospects_clinics_salons.csv
"""

from __future__ import annotations

import argparse
import csv
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge and dedupe prospects.csv-shaped files.")
    parser.add_argument("--out", dest="out_csv", default="prospects_merged.csv")
    parser.add_argument("inputs", nargs="+", help="Two or more input CSVs to merge")
    args = parser.parse_args()

    # Union of columns across all input files, in first-seen order — NOT just
    # the first file's header. Locking to the first file's columns silently
    # drops any column (e.g. `phone`) that only appears in a later file.
    header: list[str] = []
    seen_domains = set()
    merged_rows = []
    dupes = 0

    for path in args.inputs:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            file_fields = reader.fieldnames or []
            new_fields = [fn for fn in file_fields if fn not in header]
            if header and new_fields:
                print(f"  [info] {path} adds column(s) not seen in earlier files: "
                      f"{', '.join(new_fields)} — earlier rows will show blank there.",
                      file=sys.stderr)
            header.extend(new_fields)
            for row in reader:
                domain = (row.get("domain") or "").strip().lower()
                if domain and domain in seen_domains:
                    dupes += 1
                    continue
                if domain:
                    seen_domains.add(domain)
                merged_rows.append(row)

    if not header:
        print("No input files produced any rows.", file=sys.stderr)
        sys.exit(1)

    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in merged_rows:
            writer.writerow({k: row.get(k, "") for k in header})

    print(f"Merged {len(args.inputs)} file(s) -> {len(merged_rows)} rows "
          f"({dupes} duplicate domain(s) skipped). Wrote {args.out_csv}")


if __name__ == "__main__":
    main()
