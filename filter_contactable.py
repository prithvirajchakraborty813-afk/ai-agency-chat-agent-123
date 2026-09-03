#!/usr/bin/env python3
"""
filter_contactable.py — drops leads that have neither a real captured
`email` nor a real `domain` before they reach gemini_vertex_qualifier.py
/ proposal_generator.py.

WHY THIS EXISTS: gemini_maps_finder.py writes a synthetic placeholder
into the `domain` column for phone-only leads — "no-website--<slug>" —
so merge_prospects.py's domain-based dedup doesn't collapse every
no-website business into one row (see _synthetic_key() there). That
placeholder is a dedupe key, not a real domain: it never contains a
".", so email_sender.py can never guess an address from it.

send_proposals.py runs email-only (PRIMARY_CHANNEL=email, WhatsApp
parked pending a PAN) — a phone number alone is never enough to reach
a lead right now. Without this filter, those phone-only leads still
flow through gemini_vertex_qualifier.py and proposal_generator.py (real
Gemini API calls, real --delay seconds each) only to be skipped or
fail at send_proposals.py anyway. Running this right after merge, before
qualify, cuts that wasted cost and keeps the pipeline's paid API calls
limited to leads that can actually be contacted right now.

This is a fetch-time filter, not a permanent disqualification — a lead
dropped here isn't marked lost anywhere; it simply doesn't appear in
prospects_merged.csv's output for today's run. If WhatsApp comes back
(PRIMARY_CHANNEL=whatsapp), this filter step should be removed from
run_daily_chain.py (or changed to also keep phone-having rows), since
phone-only leads become contactable again at that point.

Usage:
    python filter_contactable.py --in prospects_merged.csv --out prospects_contactable.csv
"""

from __future__ import annotations

import argparse
import csv
import sys

# Same shape-check as gemini_maps_finder.py / gemini_search_finder.py use
# to decide whether a `domain` value is a real domain vs explanatory text
# or a synthetic no-website placeholder. Duplicated here (not imported)
# because those modules import heavy Gemini/Vertex SDKs at module load
# time — pulling one in just for this one regex would slow down and
# complicate what's meant to be a small, fast, dependency-light filter.
def _looks_like_real_domain(value: str) -> bool:
    v = (value or "").strip().lower()
    # A real domain always has a dot (e.g. "example.com"); the
    # "no-website--..." synthetic key never does (built from
    # [a-z0-9-]+ only — see gemini_maps_finder.py's _synthetic_key()).
    return bool(v) and "." in v and not v.startswith("no-website--")


def _looks_like_real_email(value: str) -> bool:
    v = (value or "").strip()
    return bool(v) and "@" in v


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Keep only leads with a real email or a real domain on file.")
    parser.add_argument("--in", dest="in_csv", default="prospects_merged.csv")
    parser.add_argument("--out", dest="out_csv", default="prospects_contactable.csv")
    args = parser.parse_args()

    with open(args.in_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if not fieldnames:
        print(f"No rows/header in {args.in_csv}.", file=sys.stderr)
        sys.exit(1)

    kept, dropped = [], 0
    for row in rows:
        email = row.get("email", "")
        domain = row.get("domain", "")
        if _looks_like_real_email(email) or _looks_like_real_domain(domain):
            kept.append(row)
        else:
            dropped += 1
            name = row.get("name", "").strip()
            print(f"  [skip] {name:40s} -> no real email or domain on file "
                  f"(phone-only — not contactable in email-only mode)", file=sys.stderr)

    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in kept:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    print(f"Kept {len(kept)} contactable lead(s), dropped {dropped} phone-only "
          f"lead(s) (no real email/domain). Wrote {args.out_csv}")


if __name__ == "__main__":
    main()
