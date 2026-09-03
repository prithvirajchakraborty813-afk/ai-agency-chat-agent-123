#!/usr/bin/env python3
"""
run_daily_chain.py — the single command the Render Cron Job runs once a
day. Chains: find leads -> merge -> qualify -> draft proposals -> send
first-touch messages, in order, stopping immediately (and notifying the
owner on WhatsApp) if any step fails, rather than letting broken or empty
data flow into the next step.

All the things you'd otherwise pass as CLI flags come from environment
variables instead, so you edit them in Render's dashboard, never in code:

    GCP_PROJECT        Google Cloud project ID (required)
    LEAD_DESCRIPTION   what kind of business to find, e.g.
                        "small clinics, salons, and local shops in India"
    LEAD_CITIES        comma-separated cities, e.g. "Kolkata,Bengaluru"
    COUNT_PER_CITY     businesses per city anchor (default 8)
    PRODUCT_NAME       your product name, for the qualifier/proposal steps
    PRODUCT_PITCH      one-line pitch of what it does / solves
    GMAIL_ADDRESS      Gmail address used both to send lead outreach
                        (PRIMARY_CHANNEL=email, see send_proposals.py) and
                        owner failure alerts (this file) — required in the
                        current email-only setup
    GMAIL_APP_PASSWORD Gmail App Password for the above (NOT your normal
                        password — see email_sender.py / GMAIL_SETUP.md)
    OWNER_EMAIL        your own email — where failure notifications go
                        while WhatsApp is parked (see notify_owner() below)
    WAHA_BASE_URL      base URL of your self-hosted WAHA instance — PARKED,
                        not required right now (send_proposals.py's
                        PRIMARY_CHANNEL defaults to "email", which never
                        touches WAHA). Needed again once PRIMARY_CHANNEL is
                        switched back to "whatsapp".
    WAHA_SESSION       WAHA session name, default "default" (parked, see above)
    WAHA_API_KEY       WAHA X-Api-Key, if set (optional, parked, see above)
    OWNER_PHONE        your own WhatsApp number, digits only, country code
                        included — PARKED alongside WAHA_BASE_URL above;
                        not used for owner alerts while WhatsApp is parked,
                        kept here so flipping back later is a one-line change
    DATABASE_URL       Postgres connection string (already required by
                        db_storage.py / chat_agent.py)

Run order and files:
    gemini_maps_finder.py   --> prospects_maps.csv
    gemini_search_finder.py --> prospects_search.csv   (fallback source)
    merge_prospects.py      --> prospects_merged.csv
    gemini_vertex_qualifier.py --> qualified.csv
    proposal_generator.py   --> proposals.csv
    send_proposals.py       --> sends + records in Postgres (contacted_leads)
"""

from __future__ import annotations

import os
import subprocess
import sys

import requests

import email_sender

WAHA_BASE_URL = os.environ.get("WAHA_BASE_URL", "http://localhost:3000").rstrip("/")
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "")

# "email" (default) or "whatsapp" — which channel owner failure-alerts go
# out on. Independent of send_proposals.py's PRIMARY_CHANNEL (that one
# controls how LEADS are contacted; this controls how the OWNER is
# notified when a pipeline step fails) but set to match it for now since
# WhatsApp is parked pending a PAN. Flip to "whatsapp" to restore the
# original behavior — the WhatsApp-send code below is untouched, just
# gated behind this same pattern used in send_proposals.py.
OWNER_ALERT_CHANNEL = os.environ.get("OWNER_ALERT_CHANNEL", "email").strip().lower()


def _notify_owner_whatsapp(message: str) -> None:
    """PARKED — not called while OWNER_ALERT_CHANNEL=email, but left fully
    intact so switching back to WhatsApp needs no rewriting, just flipping
    OWNER_ALERT_CHANNEL back to "whatsapp"."""
    owner = os.environ.get("OWNER_PHONE")
    if not WAHA_BASE_URL or not owner:
        print(f"[owner notify skipped — WAHA_BASE_URL/OWNER_PHONE not set] {message}", file=sys.stderr)
        return
    try:
        headers = {"Content-Type": "application/json"}
        if WAHA_API_KEY:
            headers["X-Api-Key"] = WAHA_API_KEY
        requests.post(
            f"{WAHA_BASE_URL}/api/sendText",
            headers=headers,
            json={"session": WAHA_SESSION, "chatId": f"{owner}@c.us", "text": message},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"[owner notify FAILED: {e}] {message}", file=sys.stderr)


def _notify_owner_email(message: str) -> None:
    if not OWNER_EMAIL:
        print(f"[owner notify skipped — OWNER_EMAIL not set] {message}", file=sys.stderr)
        return
    ok, detail = email_sender.send_email(
        GMAIL_ADDRESS, GMAIL_APP_PASSWORD, OWNER_EMAIL,
        subject="Daily lead pipeline alert", body=message,
    )
    if not ok:
        print(f"[owner notify FAILED: {detail}] {message}", file=sys.stderr)


def notify_owner(message: str) -> None:
    if OWNER_ALERT_CHANNEL == "whatsapp":
        _notify_owner_whatsapp(message)
    else:
        _notify_owner_email(message)


def run_step(label: str, cmd: list[str]) -> bool:
    print(f"\n=== {label} ===")
    print(" ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        notify_owner(
            f"Daily lead pipeline stopped: step '{label}' failed "
            f"(exit code {result.returncode}). Nothing downstream ran. "
            f"Check Render logs."
        )
        print(f"STOPPING CHAIN — {label} failed.", file=sys.stderr)
        return False
    return True


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        notify_owner(f"Daily lead pipeline could not start — missing env vars: {', '.join(missing)}")
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    # WAHA_BASE_URL deliberately NOT required here — it's only needed once
    # send_proposals.py's PRIMARY_CHANNEL is switched back to "whatsapp".
    # In the current email-only default, requiring it would block a
    # correctly-configured pipeline for no reason.
    require_env("GCP_PROJECT", "LEAD_DESCRIPTION", "PRODUCT_NAME", "PRODUCT_PITCH")

    project = os.environ["GCP_PROJECT"]
    description = os.environ["LEAD_DESCRIPTION"]
    cities = os.environ.get("LEAD_CITIES", "")
    count_per_city = os.environ.get("COUNT_PER_CITY", "8")
    product = os.environ["PRODUCT_NAME"]
    pitch = os.environ["PRODUCT_PITCH"]

    maps_cmd = [
        sys.executable, "gemini_maps_finder.py",
        "--project", project, "--description", description,
        "--out", "prospects_maps.csv", "--count-per-city", count_per_city,
    ]
    if cities:
        maps_cmd += ["--cities", cities]
    if not run_step("Find leads (Google Maps)", maps_cmd):
        sys.exit(1)

    # Search-grounding fallback — same description, different source. Not
    # fatal if this one fails; the maps results alone are enough to continue.
    search_cmd = [
        sys.executable, "gemini_search_finder.py",
        "--project", project, "--description", description,
        "--out", "prospects_search.csv",
    ]
    have_search_results = run_step("Find leads (Search fallback)", search_cmd)

    merge_inputs = ["prospects_maps.csv"]
    if have_search_results and os.path.exists("prospects_search.csv"):
        merge_inputs.append("prospects_search.csv")
    merge_cmd = [sys.executable, "merge_prospects.py", "--out", "prospects_merged.csv"] + merge_inputs
    if not run_step("Merge and dedupe", merge_cmd):
        sys.exit(1)

    qualify_cmd = [
        sys.executable, "gemini_vertex_qualifier.py",
        "--in", "prospects_merged.csv", "--out", "qualified.csv",
        "--project", project, "--product", product, "--pitch", pitch,
        "--delay", os.environ.get("QUALIFY_DELAY", "4"),
    ]
    if not run_step("Qualify leads", qualify_cmd):
        sys.exit(1)

    proposal_cmd = [
        sys.executable, "proposal_generator.py",
        "--in", "qualified.csv", "--out", "proposals.csv",
        "--project", project, "--delay", os.environ.get("PROPOSAL_DELAY", "4"),
    ]
    if not run_step("Draft proposals", proposal_cmd):
        sys.exit(1)

    send_cmd = [sys.executable, "send_proposals.py", "--in", "proposals.csv"]
    if not run_step("Send first-touch messages", send_cmd):
        sys.exit(1)

    print("\nDaily chain complete.")


if __name__ == "__main__":
    main()
