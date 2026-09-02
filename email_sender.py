#!/usr/bin/env python3
"""
email_sender.py — Gmail-based email fallback for send_proposals.py.

WHAT THIS IS FOR: leads with no phone number on file, or whose WhatsApp
send failed, but who DO have a `domain` value from the finder scripts
(gemini_maps_finder.py / gemini_search_finder.py). No PAN, no business
verification, no KYC — this is a personal Gmail account sending through
Gmail's own SMTP server with an App Password.

THE HONEST LIMITATION, READ THIS FIRST: the pipeline has never captured
a real email address for any lead — only a website `domain`. There is no
info@/contact@ address on file anywhere; this module GUESSES one from the
domain (info@, contact@, in that order) because that's the best available
signal, not because it's reliable. Expect a real chunk of these to bounce
or go nowhere, especially for small businesses (salons, clinics, local
shops) who may have a domain but never set up a matching inbox. This is
meaningfully weaker than the phone numbers the Maps finder actually
extracts — treat it as a genuine fallback, not a channel you'd rely on
for a majority of leads. Leads with no `domain` at all (common — many
real leads have no website, per earlier project notes) get no email
attempt whatsoever; there's nothing to guess from.

SETUP (no PAN, free):
    1. On the Gmail account you want to send from: turn on 2-Step
       Verification (myaccount.google.com/security), then generate an
       App Password (myaccount.google.com/apppasswords) — a 16-character
       code, NOT your normal Gmail password.
    2. Set as env vars (or pass via CLI flags):
           GMAIL_ADDRESS=youraddress@gmail.com
           GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx

REPLIES: this module only sends. A reply lands in the Gmail inbox itself
— nothing here reads it or feeds it back into chat_agent.py's
conversation engine (that would need IMAP polling and email-thread
parsing, a separate build). Check that inbox manually, same as any
normal outreach email.

Gmail sending limits: ~500 emails/day on a free/personal Gmail account
(2,000/day on Google Workspace) — nowhere near a concern at this
pipeline's current lead volume, but worth knowing if volume grows.
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.mime.text import MIMEText

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

# Tried in this order; first one that doesn't bounce at SMTP-accept time
# "wins" for logging purposes — but SMTP accepting the message only means
# the address was well-formed and the mail server took it for delivery,
# NOT that the address exists or a human will see it. A real bounce (if
# any) arrives later, asynchronously, to the Gmail inbox — this script
# has no way to wait for or detect that.
EMAIL_GUESS_PREFIXES = ["info", "contact"]


def guess_email_addresses(domain: str) -> list[str]:
    """Best-effort guesses only — see module docstring. Returns [] for a
    blank/missing domain (nothing to guess from)."""
    d = (domain or "").strip().lower()
    if not d or "." not in d:
        return []
    # Finder scripts sometimes store a bare domain, sometimes a full URL —
    # normalize either shape down to just the domain part.
    d = d.replace("https://", "").replace("http://", "").split("/")[0]
    return [f"{prefix}@{d}" for prefix in EMAIL_GUESS_PREFIXES]


def send_email(gmail_address: str, gmail_app_password: str, to_addr: str,
                subject: str, body: str) -> tuple[bool, str]:
    """Sends one email via Gmail's SMTP server. Returns (ok, detail) —
    same shape as send_proposals.py's send_message(), so callers can
    handle both channels identically."""
    if not gmail_address or not gmail_app_password:
        return False, "GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set"
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = gmail_address
        msg["To"] = to_addr

        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(gmail_address, gmail_app_password)
            server.sendmail(gmail_address, [to_addr], msg.as_string())
        return True, f"accepted by {GMAIL_SMTP_HOST} for delivery to {to_addr}"
    except Exception as e:
        return False, str(e)


def send_email_with_fallback_guesses(gmail_address: str, gmail_app_password: str,
                                       domain: str, subject: str, body: str) -> tuple[bool, str]:
    """Tries each guessed address in turn, stopping at the first one SMTP
    accepts. Note this does NOT confirm delivery or that the address is
    real — see module docstring."""
    candidates = guess_email_addresses(domain)
    if not candidates:
        return False, "no domain on file — nothing to guess an address from"

    last_detail = ""
    for addr in candidates:
        ok, detail = send_email(gmail_address, gmail_app_password, addr, subject, body)
        if ok:
            return True, f"sent to guessed address {addr} ({detail})"
        last_detail = f"{addr}: {detail}"
    return False, f"all guessed addresses failed — last error: {last_detail}"
