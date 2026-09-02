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

REPLIES: fetch_new_replies() below polls the inbox over IMAP and
returns unread messages as (sender_address, body) pairs, marking them
Seen so the same reply is never returned twice. chat_agent.py's
/poll-email route calls this once per invocation (triggered by the
daily GitHub Action, not continuous) and feeds each reply into the
same handle_lead_message() used for WhatsApp — see that route for how
a sender address maps back to a conversation.

Gmail IMAP needs the same App Password as SMTP sending above — no
separate setup step. If 2-Step Verification + an App Password are
already on, IMAP access works with the same GMAIL_ADDRESS /
GMAIL_APP_PASSWORD env vars.

Gmail sending limits: ~500 emails/day on a free/personal Gmail account
(2,000/day on Google Workspace) — nowhere near a concern at this
pipeline's current lead volume, but worth knowing if volume grows.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import parseaddr

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993

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


def _decode_str(raw) -> str:
    """Email headers can be plain str or MIME-encoded-word bytes depending
    on the sender's mail client; normalize both to a plain str."""
    if raw is None:
        return ""
    parts = decode_header(raw)
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def _extract_body(msg: "email.message.Message") -> str:
    """Plain-text body only — HTML-only replies (common from webmail
    clients) get their tags stripped as a best effort, since the chat
    engine expects plain text same as WhatsApp messages."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in disp:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace").strip()
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ctype == "text/html" and "attachment" not in disp:
                charset = part.get_content_charset() or "utf-8"
                html = part.get_payload(decode=True).decode(charset, errors="replace")
                return re.sub(r"<[^>]+>", " ", html).strip()
        return ""
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload is None:
            return ""
        text = payload.decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        return text.strip()


def _strip_quoted_reply(body: str) -> str:
    """Best-effort trim of quoted history under a reply, so Gemini sees
    only the new message rather than the whole thread repeated each time.
    Cuts at the first common quote marker; if none found, returns the
    body unchanged rather than guessing wrong and losing real content."""
    markers = [
        r"\nOn .{0,120} wrote:\n",   # Gmail/most clients
        r"\n-{2,} ?Original Message ?-{2,}",
        r"\nFrom: .+\nSent: .+\nTo: .+\nSubject: .+",  # Outlook
    ]
    for pattern in markers:
        m = re.search(pattern, body, flags=re.IGNORECASE)
        if m:
            return body[:m.start()].strip()
    return body.strip()


def fetch_new_replies(gmail_address: str, gmail_app_password: str,
                       max_messages: int = 50) -> list[tuple[str, str, str]]:
    """Connects over IMAP, finds UNSEEN messages in the inbox, and returns
    them as (sender_email, sender_domain, body) tuples — marking each one
    Seen as it's read, so a message is never returned twice across runs.

    sender_domain is the part after '@' in the sender's address, used to
    match a reply back to the lead it belongs to (leads are only known by
    domain in this pipeline, not a captured personal email — see module
    docstring). Returns [] on any connection/auth failure rather than
    raising, so a bad Gmail credential doesn't crash the whole poll route;
    the caller should log the empty result and move on.

    Only reads the inbox this account owns — never sends anything.
    """
    if not gmail_address or not gmail_app_password:
        print("[error] GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — cannot poll for replies.", file=sys.stderr)
        return []

    results: list[tuple[str, str, str]] = []
    try:
        with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT) as imap:
            imap.login(gmail_address, gmail_app_password)
            imap.select("INBOX")
            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                print(f"[error] IMAP search failed: {status}", file=sys.stderr)
                return []
            msg_ids = data[0].split()[:max_messages]
            for msg_id in msg_ids:
                status, msg_data = imap.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                from_header = _decode_str(msg.get("From", ""))
                _, sender_addr = parseaddr(from_header)
                sender_addr = (sender_addr or "").strip().lower()
                if not sender_addr or "@" not in sender_addr:
                    continue
                domain = sender_addr.split("@", 1)[1]

                # Skip obvious automated/notification senders (newsletters,
                # platform notifications, no-reply addresses, etc). Without
                # this, something like a YouTube or Facebook notification
                # landing as UNSEEN in the inbox gets treated as a real lead
                # reply, and the chat engine burns a Gemini call and tries to
                # email a reply back to an address that will never accept
                # mail — which can hang the whole request until the server's
                # worker is killed (this exact failure mode happened first
                # with noreply@youtube.com, then messages@facebookmail.com —
                # a local-part-only check missed the second one, so this now
                # also checks the domain).
                local_part = sender_addr.split("@", 1)[0]
                NOREPLY_LOCAL_PARTS = (
                    "noreply", "no-reply", "donotreply", "do-not-reply",
                    "notifications", "notification", "mailer-daemon",
                    "postmaster", "messages", "alert", "alerts", "updates",
                )
                # Domains of consumer platforms whose automated mail (likes,
                # comments, notifications, digests) can land in any inbox
                # and is never going to be a genuine business lead reply.
                KNOWN_NOTIFICATION_DOMAINS = (
                    "youtube.com", "facebookmail.com", "facebook.com",
                    "instagram.com", "linkedin.com", "twitter.com", "x.com",
                    "google.com", "accounts.google.com", "github.com",
                    "slack.com", "notion.so", "calendly.com",
                )
                is_noreply_local = local_part in NOREPLY_LOCAL_PARTS
                is_known_notification_domain = any(
                    domain == d or domain.endswith("." + d) for d in KNOWN_NOTIFICATION_DOMAINS
                )
                if is_noreply_local or is_known_notification_domain:
                    print(f"[info] Skipping likely-automated sender: {sender_addr}", file=sys.stderr)
                    imap.store(msg_id, "+FLAGS", "\\Seen")
                    continue

                body = _extract_body(msg)
                body = _strip_quoted_reply(body)
                if body:
                    results.append((sender_addr, domain, body))
                # Explicitly mark Seen (should already happen via fetch,
                # but IMAP servers vary — don't rely on the side effect).
                imap.store(msg_id, "+FLAGS", "\\Seen")
        return results
    except imaplib.IMAP4.error as e:
        print(f"[error] IMAP login/search failed: {e}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[error] Unexpected error polling Gmail: {e}", file=sys.stderr)
        return []
