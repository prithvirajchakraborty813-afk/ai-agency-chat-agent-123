#!/usr/bin/env python3
"""
send_proposals.py — sends the real proposal_text from proposals.csv to
each lead, over email or WhatsApp depending on PRIMARY_CHANNEL (see that
constant below). Currently EMAIL-ONLY by default (PRIMARY_CHANNEL=email):
every lead is emailed at their real captured `email` address when the
finder scripts found one listed on Maps/their website; if not, it falls
back to a guessed info@/contact@ address from their `domain` value — see
email_sender.py's module docstring for the guessed-address limitation
(lower hit rate than a real captured address; expect some bounces).
Leads with neither a real `email` nor a `domain` get no send at all in
this mode — a phone number alone isn't enough, since WhatsApp isn't used.

WHATSAPP (parked, not removed): the original WhatsApp send path — via a
self-hosted WAHA instance, your own linked number, no message-volume cap
unlike Whapi.Cloud's free tier — is still fully implemented below
(send_message(), normalize_phone(), pacing, dedup) but is not called
while PRIMARY_CHANNEL=email. This is intentional: it's a working, tested
pipeline that's waiting on a PAN (business verification) before it can
be primary again. Set PRIMARY_CHANNEL=whatsapp (env var) to switch back
— WhatsApp becomes primary again with email as its fallback (original
behavior), no other code changes needed.

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
    --no-email-fallback         disable the Gmail fallback even if
                                 GMAIL_ADDRESS/GMAIL_APP_PASSWORD are set
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
import email_sender

# ---------------------------------------------------------------------------
# PRIMARY_CHANNEL — single switch controlling which channel actually sends.
#
# "email"    (current default): email-only. WhatsApp is fully wired and
#            tested below (send_message(), normalize_phone(), the WAHA
#            call) but is never invoked — every lead goes out over email,
#            using the same guessed info@/contact@ address logic
#            email_sender.py already has. No phone number is used or
#            required for a send to happen.
# "whatsapp": the original behavior — WhatsApp first, email only as a
#            fallback when there's no phone or the WhatsApp send fails.
#
# WHY A TOGGLE AND NOT A DELETE: WhatsApp send is a working, tested
# pipeline (WAHA integration, phone normalization, pacing) — it's parked,
# not gone, until a PAN is available and WhatsApp becomes primary. When
# that day comes, flip this one line back to "whatsapp" — nothing else
# in this file needs to change, no code needs to be rewritten or
# un-commented line by line.
# ---------------------------------------------------------------------------
PRIMARY_CHANNEL = os.environ.get("PRIMARY_CHANNEL", "email").strip().lower()


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


def _email_key(domain: str) -> str | None:
    """Dedup/contacted-leads key for the email channel — kept distinct from
    the phone key (same "email:<domain>" prefix pattern chat_agent.py uses
    for "tg:<chat_id>") so a lead can be independently tracked as
    WhatsApp-contacted and/or email-contacted without the two colliding."""
    d = (domain or "").strip().lower()
    return f"email:{d}" if d else None


def run(in_csv: str, waha_url: str, session: str, api_key: str, min_delay: float, max_delay: float,
        log_path: str, dry_run: bool, email_fallback: bool,
        gmail_address: str, gmail_app_password: str) -> None:
    with open(in_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Cross-run/cross-day dedup lives in Postgres, not the local log file —
    # see db_storage.contacted_leads for why (ephemeral cron filesystem).
    db_storage.init_db()
    already_sent = db_storage.load_all_contacted_phones()  # holds phone keys AND "email:<domain>" keys
    log_exists = os.path.exists(log_path)
    log_file = open(log_path, "a", newline="", encoding="utf-8")
    log_writer = csv.DictWriter(log_file, fieldnames=["name", "phone", "channel", "status", "detail"])
    if not log_exists:
        log_writer.writeheader()

    sent, skipped_no_channel, skipped_already, failed = 0, 0, 0, 0

    for i, row in enumerate(rows, 1):
        name = row.get("name", "").strip()
        proposal_text = row.get("proposal_text", "").strip()
        raw_phone = row.get("phone", "")
        domain = row.get("domain", "").strip()
        real_email = row.get("email", "").strip()
        to = normalize_phone(raw_phone)
        # Prefer a real, listed email address (from Maps/website, captured by
        # gemini_maps_finder.py) over the guessed info@/contact@ fallback —
        # a real address is far more likely to actually reach someone and
        # not bounce/spam-flag. Falls back to the domain-guess key exactly
        # as before when no real address was found for this lead.
        e_key = f"email:{real_email.lower()}" if real_email else _email_key(domain)

        # PRIMARY_CHANNEL == "email": WhatsApp is never attempted, so
        # can_try_whatsapp stays False regardless of whether a phone
        # number is on file — see the PRIMARY_CHANNEL comment near the
        # top of this file for how to flip this back.
        can_try_whatsapp = (PRIMARY_CHANNEL == "whatsapp") and bool(to) and to not in already_sent
        can_try_email = bool(e_key) and e_key not in already_sent
        if PRIMARY_CHANNEL == "whatsapp":
            can_try_email = email_fallback and can_try_email

        if not can_try_whatsapp and not can_try_email:
            if PRIMARY_CHANNEL == "email":
                reason = "already emailed" if e_key else "no email or domain on file (email-only mode — phone number, if any, isn't used)"
            else:
                reason = "already sent on every available channel" if (to or e_key) else "no phone or domain on file"
            print(f"  [{i}/{len(rows)}] {name:40s} -> SKIPPED ({reason})")
            log_writer.writerow({"name": name, "phone": raw_phone, "channel": "", "status": "skipped", "detail": reason})
            if (PRIMARY_CHANNEL == "email" and e_key) or (PRIMARY_CHANNEL == "whatsapp" and (to or e_key)):
                skipped_already += 1
            else:
                skipped_no_channel += 1
            continue

        if dry_run:
            if can_try_whatsapp:
                print(f"  [{i}/{len(rows)}] {name:40s} -> WOULD SEND (WhatsApp) to {to}: {proposal_text[:80]}...")
            elif can_try_email:
                if real_email:
                    print(f"  [{i}/{len(rows)}] {name:40s} -> WOULD EMAIL (real address {real_email}): {proposal_text[:80]}...")
                else:
                    guesses = email_sender.guess_email_addresses(domain)
                    print(f"  [{i}/{len(rows)}] {name:40s} -> WOULD EMAIL (guessed {guesses}): {proposal_text[:80]}...")
            continue

        sent_ok = False

        # WhatsApp send — parked behind PRIMARY_CHANNEL, not deleted. This
        # block is fully working code (WAHA call, logging, dedup) and only
        # runs at all when PRIMARY_CHANNEL="whatsapp"; can_try_whatsapp is
        # unconditionally False otherwise, so this whole branch is a no-op
        # in email-only mode without needing to comment out its body.
        if can_try_whatsapp:
            ok, detail = send_message(waha_url, session, api_key, to, proposal_text)
            if ok:
                print(f"  [{i}/{len(rows)}] {name:40s} -> sent (WhatsApp) to {to}")
                log_writer.writerow({"name": name, "phone": to, "channel": "whatsapp", "status": "sent", "detail": detail})
                db_storage.mark_contacted(to, name, detail)
                sent_ok = True
            else:
                print(f"  [{i}/{len(rows)}] {name:40s} -> WhatsApp FAILED: {detail}", file=sys.stderr)
                log_writer.writerow({"name": name, "phone": to, "channel": "whatsapp", "status": "failed", "detail": detail})

        if not sent_ok and can_try_email:
            subject = f"AI customer-engagement assistant for {name}" if name else "AI customer-engagement assistant"
            if real_email:
                ok, detail = email_sender.send_email(
                    gmail_address, gmail_app_password, real_email, subject, proposal_text)
                if ok:
                    detail = f"sent to real address {real_email} ({detail})"
            else:
                ok, detail = email_sender.send_email_with_fallback_guesses(
                    gmail_address, gmail_app_password, domain, subject, proposal_text)
            if ok:
                print(f"  [{i}/{len(rows)}] {name:40s} -> sent (email fallback), {detail}")
                log_writer.writerow({"name": name, "phone": e_key, "channel": "email", "status": "sent", "detail": detail})
                db_storage.mark_contacted(e_key, name, detail)
                sent_ok = True
            else:
                print(f"  [{i}/{len(rows)}] {name:40s} -> email fallback FAILED: {detail}", file=sys.stderr)
                log_writer.writerow({"name": name, "phone": e_key, "channel": "email", "status": "failed", "detail": detail})

        if sent_ok:
            sent += 1
        else:
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
              f"no phone/domain (skipped): {skipped_no_channel}, failed: {failed}")
        print(f"Full log at {log_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send real proposal_text to leads in proposals.csv over email (default) or WhatsApp — see PRIMARY_CHANNEL.")
    parser.add_argument("--in", dest="in_csv", default="proposals.csv")
    parser.add_argument("--waha-url", dest="waha_url", default=os.environ.get("WAHA_BASE_URL", "http://localhost:3000"),
                         help="Base URL of your WAHA instance — only needed if PRIMARY_CHANNEL=whatsapp (or set WAHA_BASE_URL env var)")
    parser.add_argument("--waha-session", dest="waha_session", default=os.environ.get("WAHA_SESSION", "default"),
                         help="WAHA session name (or set WAHA_SESSION env var)")
    parser.add_argument("--waha-key", dest="waha_key", default=os.environ.get("WAHA_API_KEY", ""),
                         help="WAHA X-Api-Key, if set (or set WAHA_API_KEY env var)")
    parser.add_argument("--min-delay", type=float, default=60, help="Minimum seconds between sends")
    parser.add_argument("--max-delay", type=float, default=180, help="Maximum seconds between sends")
    parser.add_argument("--log", default="sent_log.csv")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent without sending")
    parser.add_argument("--no-email-fallback", dest="email_fallback", action="store_false",
                         help="When PRIMARY_CHANNEL=whatsapp: disable the Gmail fallback even if GMAIL_ADDRESS/GMAIL_APP_PASSWORD are set. Has no effect in email-only mode (email isn't a 'fallback' there, it's the only channel).")
    parser.add_argument("--gmail-address", default=os.environ.get("GMAIL_ADDRESS", ""),
                         help="Gmail address to send from (or set GMAIL_ADDRESS env var)")
    parser.add_argument("--gmail-app-password", default=os.environ.get("GMAIL_APP_PASSWORD", ""),
                         help="Gmail App Password, NOT your normal password (or set GMAIL_APP_PASSWORD env var)")
    parser.set_defaults(email_fallback=True)
    args = parser.parse_args()

    # --waha-url is only required when WhatsApp is actually going to be
    # used — in email-only mode (the default) nothing in this script ever
    # touches WAHA, so requiring its URL would block a working config for
    # no reason.
    if not args.dry_run and PRIMARY_CHANNEL == "whatsapp" and not args.waha_url:
        parser.error("Missing --waha-url (or set the WAHA_BASE_URL environment variable) — required when PRIMARY_CHANNEL=whatsapp.")

    if PRIMARY_CHANNEL == "email":
        # Email is the only channel in this mode, not a fallback — always
        # attempted (per-lead skips still apply: no domain, or already
        # sent). --no-email-fallback intentionally has no effect here.
        email_fallback = True
        if not args.gmail_address or not args.gmail_app_password:
            print("[warning] GMAIL_ADDRESS/GMAIL_APP_PASSWORD not both set — PRIMARY_CHANNEL=email but sends will fail until these are set.", file=sys.stderr)
    else:
        email_fallback = args.email_fallback and bool(args.gmail_address) and bool(args.gmail_app_password)
        if args.email_fallback and not email_fallback and not args.dry_run:
            print("[info] Gmail fallback disabled — GMAIL_ADDRESS/GMAIL_APP_PASSWORD not both set.", file=sys.stderr)

    run(args.in_csv, args.waha_url, args.waha_session, args.waha_key,
        args.min_delay, args.max_delay, args.log, args.dry_run,
        email_fallback, args.gmail_address, args.gmail_app_password)


if __name__ == "__main__":
    main()
