#!/usr/bin/env python3
"""
send_proposals.py — sends the real proposal_text from proposals.csv to
each lead's WhatsApp, via a self-hosted WAHA instance (uses your own
linked number, no message-volume cap unlike Whapi.Cloud's free tier).

WHY PACED, NOT ALL AT ONCE: sending 10 messages back-to-back looks
robotic and is exactly the pattern WhatsApp's spam detection watches
for. This script waits a random 60-180 seconds between sends by
default — for 10 messages that's a few minutes total, not something
you need to babysit, but it looks like a person sending messages, not
a bot blasting a list.

WHY IT WON'T SEND TWICE: every send is logged to sent_log.csv as it
happens. Before sending to any lead, the script checks that log first
and skips anyone already marked "sent" — so if this crashes halfway
through, or you run it again by mistake, nobody gets double-messaged.

Usage:
    python send_proposals.py --in proposals.csv --waha-url http://localhost:3000

    (or set WAHA_BASE_URL as an environment variable instead of --waha-url)

Options:
    --waha-url                  base URL of your WAHA instance (default
                                 http://localhost:3000, or WAHA_BASE_URL env var)
    --waha-session               WAHA session name (default "default", or
                                 WAHA_SESSION env var)
    --waha-key                  WAHA X-Api-Key, if set (or WAHA_API_KEY env var)
    --min-delay / --max-delay   seconds between sends (default 60-180)
    --dry-run                   print what would be sent, without sending
    --log                       path to the send log (default sent_log.csv)
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time

import requests

import db_storage


def normalize_phone(raw: str) -> str | None:
    """WAHA (like Whapi before it) wants digits only, country code
    included, no '+' or spaces — the '@c.us' suffix is added separately
    when building the chatId for the API call."""
    if not raw or not raw.strip():
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    if len(digits) == 10:
        digits = "91" + digits
    return digits


def load_already_sent(log_path: str) -> set[str]:
    """Returns the set of phone numbers already marked 'sent' in a prior run,
    so re-running this script never double-messages anyone."""
    if not os.path.exists(log_path):
        return set()
    sent = set()
    with open(log_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") == "sent":
                sent.add(row.get("phone", ""))
    return sent


def send_message(waha_url: str, session: str, api_key: str, to: str, body: str) -> tuple[bool, str]:
    try:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-Api-Key"] = api_key
        resp = requests.post(
            f"{waha_url.rstrip('/')}/api/sendText",
            headers=headers,
            json={"session": session, "chatId": f"{to}@c.us", "text": body},
            timeout=20,
        )
        return resp.ok, f"{resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as e:
        return False, str(e)


def run(in_csv: str, waha_url: str, session: str, api_key: str, min_delay: float, max_delay: float,
        log_path: str, dry_run: bool) -> None:
    with open(in_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Cross-run/cross-day dedup lives in Postgres, not the local log file —
    # see db_storage.contacted_leads for why (ephemeral cron filesystem).
    db_storage.init_db()
    already_sent = db_storage.load_all_contacted_phones()
    log_exists = os.path.exists(log_path)
    log_file = open(log_path, "a", newline="", encoding="utf-8")
    log_writer = csv.DictWriter(log_file, fieldnames=["name", "phone", "status", "detail"])
    if not log_exists:
        log_writer.writeheader()

    sent, skipped_no_phone, skipped_already, failed = 0, 0, 0, 0

    for i, row in enumerate(rows, 1):
        name = row.get("name", "").strip()
        proposal_text = row.get("proposal_text", "").strip()
        raw_phone = row.get("phone", "")
        to = normalize_phone(raw_phone)

        if not to:
            print(f"  [{i}/{len(rows)}] {name:40s} -> SKIPPED (no phone on file)")
            log_writer.writerow({"name": name, "phone": raw_phone, "status": "skipped_no_phone", "detail": ""})
            skipped_no_phone += 1
            continue

        if to in already_sent:
            print(f"  [{i}/{len(rows)}] {name:40s} -> already sent previously, skipping")
            skipped_already += 1
            continue

        if dry_run:
            print(f"  [{i}/{len(rows)}] {name:40s} -> WOULD SEND to {to}: {proposal_text[:80]}...")
            continue

        ok, detail = send_message(waha_url, session, api_key, to, proposal_text)
        if ok:
            print(f"  [{i}/{len(rows)}] {name:40s} -> sent to {to}")
            log_writer.writerow({"name": name, "phone": to, "status": "sent", "detail": detail})
            db_storage.mark_contacted(to, name, detail)
            sent += 1
        else:
            print(f"  [{i}/{len(rows)}] {name:40s} -> FAILED: {detail}", file=sys.stderr)
            log_writer.writerow({"name": name, "phone": to, "status": "failed", "detail": detail})
            failed += 1

        log_file.flush()

        # Pace the sends — random delay, not fixed, so it doesn't look robotic.
        # Skip the wait after the very last message.
        if i < len(rows):
            wait = random.uniform(min_delay, max_delay)
            print(f"      waiting {wait:.0f}s before next send...")
            time.sleep(wait)

    log_file.close()

    if dry_run:
        print(f"\nDry run complete. Nothing was actually sent.")
    else:
        print(f"\nDone. Sent: {sent}, already-sent (skipped): {skipped_already}, "
              f"no phone (skipped): {skipped_no_phone}, failed: {failed}")
        print(f"Full log at {log_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send real proposal_text to leads in proposals.csv via a self-hosted WAHA instance.")
    parser.add_argument("--in", dest="in_csv", default="proposals.csv")
    parser.add_argument("--waha-url", dest="waha_url", default=os.environ.get("WAHA_BASE_URL", "http://localhost:3000"),
                         help="Base URL of your WAHA instance (or set WAHA_BASE_URL env var)")
    parser.add_argument("--waha-session", dest="waha_session", default=os.environ.get("WAHA_SESSION", "default"),
                         help="WAHA session name (or set WAHA_SESSION env var)")
    parser.add_argument("--waha-key", dest="waha_key", default=os.environ.get("WAHA_API_KEY", ""),
                         help="WAHA X-Api-Key, if set (or set WAHA_API_KEY env var)")
    parser.add_argument("--min-delay", type=float, default=60, help="Minimum seconds between sends")
    parser.add_argument("--max-delay", type=float, default=180, help="Maximum seconds between sends")
    parser.add_argument("--log", default="sent_log.csv")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent without sending")
    args = parser.parse_args()

    if not args.dry_run and not args.waha_url:
        parser.error("Missing --waha-url (or set the WAHA_BASE_URL environment variable).")

    run(args.in_csv, args.waha_url, args.waha_session, args.waha_key,
        args.min_delay, args.max_delay, args.log, args.dry_run)


if __name__ == "__main__":
    main()
