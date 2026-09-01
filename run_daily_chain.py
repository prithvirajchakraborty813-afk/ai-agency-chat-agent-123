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
    WHAPI_TOKEN        Whapi.Cloud token (also used by send_proposals.py)
    OWNER_PHONE        your own WhatsApp number, digits only, country code
                        included — where failure notifications go
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

WHAPI_ENDPOINT = "https://gate.whapi.cloud/messages/text"


def notify_owner(message: str) -> None:
    token = os.environ.get("WHAPI_TOKEN")
    owner = os.environ.get("OWNER_PHONE")
    if not token or not owner:
        print(f"[owner notify skipped — WHAPI_TOKEN/OWNER_PHONE not set] {message}", file=sys.stderr)
        return
    try:
        requests.post(
            WHAPI_ENDPOINT,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": owner, "body": message},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"[owner notify FAILED: {e}] {message}", file=sys.stderr)


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
    require_env("GCP_PROJECT", "LEAD_DESCRIPTION", "PRODUCT_NAME", "PRODUCT_PITCH", "WHAPI_TOKEN")

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
