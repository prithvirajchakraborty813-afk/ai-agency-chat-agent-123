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
       code, NOT your normal Gmail password. This is used for IMAP
       polling of replies (fetch_new_replies below) — NOT for sending;
       see next step.
    2. Sending goes through Brevo's HTTPS API, not Gmail SMTP directly —
       Render's free tier blocks outbound SMTP ports (25/465/587), which
       silently broke every send before this was caught (see the
       BREVO_API_URL note further down for the full story). Sign up free
       at app.brevo.com, verify a sender, then get an API key from
       Settings -> SMTP & API -> API Keys.
    3. Set as env vars (or pass via CLI flags):
           GMAIL_ADDRESS=youraddress@gmail.com
           GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
           BREVO_API_KEY=xkeysib-xxxxxxxxxxxxxxxx

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
import requests
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import parseaddr

# Senders whose mail is never a genuine lead reply — automated
# notifications, digests, job-invite platforms, and no-reply addresses.
# Module-level (not just used inside run_email_poll_once) so inbox.py can
# reuse the exact same list to hide this traffic from the inbox view too.
NOREPLY_LOCAL_PARTS = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notification", "notifications", "mailer-daemon",
    "postmaster", "messages", "message", "alert", "alerts",
    "update", "updates", "notify",
)
# Domains of consumer platforms / job boards / tooling whose automated
# mail (likes, comments, notifications, digests, job invites, security
# alerts) can land in any inbox and is never a genuine business lead
# reply for this agency.
KNOWN_NOTIFICATION_DOMAINS = (
    "youtube.com", "facebookmail.com", "facebook.com",
    "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "google.com", "accounts.google.com", "github.com",
    "slack.com", "notion.so", "calendly.com", "render.com",
    "naukri.com", "jobhai.com", "brevo.com", "zapier.com",
    "whapi.cloud", "cloudflare.com", "descript.com",
)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993

# Render's free tier blocks outbound traffic on SMTP ports 25/465/587 as of
# Sep 26 2025 (https://render.com/changelog/free-web-services-will-no-longer-
# allow-outbound-traffic-to-smtp-ports) — confirmed here as the actual cause
# of every "sent" reply silently failing with [Errno 101] Network is
# unreachable, while IMAP (a different port, not blocked) kept working fine
# for polling. Sending now goes over Brevo's HTTPS API instead, which isn't
# port-restricted. IMAP polling above is unaffected and still uses Gmail
# directly — only outbound sending moved.
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
# Optional — the address replies are sent FROM. Separate from GMAIL_ADDRESS
# on purpose: GMAIL_ADDRESS still does the IMAP polling (reading incoming
# replies) and is a real Gmail inbox; SEND_FROM_EMAIL is for a properly
# authenticated domain sender (e.g. support@yourdomain.com) that Brevo
# sends AS, which deliverability-wise beats sending "as" a free Gmail
# address through a third-party API. Falls back to GMAIL_ADDRESS if unset,
# so this is a drop-in upgrade, not a required change.
SEND_FROM_EMAIL = os.environ.get("SEND_FROM_EMAIL", "")

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


def _plain_text_to_html(body: str) -> str:
    """Turns a plain-text message into simple, professional-looking HTML:
    paragraphs from blank-line-separated blocks, a real clickable button
    for any upi:// or http(s):// link found on its own line (instead of a
    long raw URL sitting in plain text, which reads as low-effort/spammy),
    and a small branded header/footer. Deliberately simple — no external
    images, no tracking pixels, no marketing-template flourishes — since
    those are themselves common spam signals; the goal here is "looks
    like a real small business sent this," not "looks like a campaign."
    """
    import html as _html

    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    blocks = []
    for para in paragraphs:
        lines = para.split("\n")
        # A paragraph that's ONLY a link (the payment link line) becomes a
        # button instead of plain wrapped text.
        if len(lines) == 1 and re.match(r"^(https?://|upi://)\S+$", lines[0].strip()):
            url = lines[0].strip()
            blocks.append(
                f'<p style="text-align:center;margin:24px 0;">'
                f'<a href="{_html.escape(url)}" '
                f'style="background:#4F46E5;color:#ffffff;text-decoration:none;'
                f'padding:12px 28px;border-radius:6px;font-weight:600;'
                f'display:inline-block;">Pay via UPI</a></p>'
            )
        else:
            escaped = _html.escape(para).replace("\n", "<br>")
            blocks.append(f'<p style="margin:0 0 16px;line-height:1.5;">{escaped}</p>')

    body_html = "\n".join(blocks)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:32px 0;">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">
<tr><td style="background:#111827;padding:20px 32px;">
<span style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:0.3px;">Vortex AI</span>
</td></tr>
<tr><td style="padding:32px;color:#1f2937;font-size:15px;">
{body_html}
</td></tr>
<tr><td style="padding:20px 32px;background:#f9fafb;color:#9ca3af;font-size:12px;">
This email was sent by Vortex AI. If you weren't expecting this, you can safely ignore it.
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def send_email(gmail_address: str, gmail_app_password: str, to_addr: str,
                subject: str, body: str) -> tuple[bool, str]:
    """Sends one email via Brevo's HTTPS transactional API. Returns
    (ok, detail) — same shape as send_proposals.py's send_message(), so
    callers can handle both channels identically.

    The "from" address is SEND_FROM_EMAIL if set (a domain address you've
    verified + authenticated in Brevo — better deliverability than a free
    Gmail address), otherwise falls back to gmail_address so this works
    even before SEND_FROM_EMAIL is configured. gmail_address/
    gmail_app_password are kept as required parameters for backward
    compatibility with every existing caller; gmail_app_password itself
    is unused here (still needed elsewhere in this module for IMAP
    polling, which is unaffected by any of this).

    Every send sets Reply-To back to gmail_address (your Gmail — the one
    fetch_new_replies() above actually polls). This matters specifically
    because SEND_FROM_EMAIL points at a domain mailbox (Zoho, in this
    project's case) whose free plan has no IMAP/forwarding — mail sent
    there would be invisible to this bot forever with no error, the same
    silent-loss failure mode as the Gmail read/unread bug this module
    already fixed once. Reply-To redirects a lead's reply to Gmail at the
    email-client level (every major client honors it) without needing
    forwarding, IMAP, or any paid plan on the domain mailbox's end.

    Requires BREVO_API_KEY to be set as an env var — get one free at
    app.brevo.com (Settings -> SMTP & API -> API Keys). Brevo's free tier
    covers 300 emails/day, comfortably above this pipeline's volume.
    """
    if not BREVO_API_KEY:
        return False, "BREVO_API_KEY not set — cannot send (see email_sender.py module notes)"
    from_addr = SEND_FROM_EMAIL or gmail_address
    if not from_addr:
        return False, "Neither SEND_FROM_EMAIL nor GMAIL_ADDRESS set — no from-address available"
    payload = {
        "sender": {"email": from_addr, "name": "Vortex AI"},
        "to": [{"email": to_addr}],
        "subject": subject,
        "textContent": body,
        "htmlContent": _plain_text_to_html(body),
        "headers": {"Content-Type": "text/html; charset=utf-8"},
    }
    # Only set Reply-To when it differs from the from-address — if
    # SEND_FROM_EMAIL isn't configured, from_addr IS gmail_address already,
    # and an identical Reply-To is redundant (harmless, but noise).
    if gmail_address and gmail_address != from_addr:
        payload["replyTo"] = {"email": gmail_address}
    try:
        resp = requests.post(
            BREVO_API_URL,
            headers={
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            return True, f"accepted by Brevo for delivery to {to_addr}"
        return False, f"Brevo API returned {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, str(e)


def send_email_with_fallback_guesses(gmail_address: str, gmail_app_password: str,
                                       domain: str, subject: str, body: str) -> tuple[bool, str]:
    """Tries each guessed address in turn, stopping at the first one
    Brevo's API accepts. Note this does NOT confirm delivery or that the
    address is real — see module docstring."""
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
                       max_messages: int = 5,
                       lookback_days: int = 7) -> list[tuple[str, str, str]]:
    """Connects over IMAP, finds recent messages in the inbox, and returns
    the ones not yet processed as (sender_email, sender_domain, body)
    tuples — recording each one's Message-ID in the DB (via db_storage) as
    it's read, so a message is never returned twice across runs.

    Deliberately does NOT rely on Gmail's \\Seen flag / UNSEEN search.
    \\Seen is a shared, mutable flag — anything else that touches this
    inbox (a phone's mail app opening a message, a second app also polling
    over IMAP, Gmail's own preview) can silently mark a message Seen before
    this poll ever runs, making it permanently invisible to a UNSEEN
    search with zero error signal. Instead this searches SINCE a lookback
    window (recent-but-not-huge) and filters against a DB table of
    already-processed Message-IDs, which only this app writes to — so
    "have I handled this" is a fact this app owns, not something another
    process can quietly invalidate.

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

    try:
        import db_storage
    except Exception as e:
        print(f"[error] db_storage unavailable — cannot track processed emails safely: {e}", file=sys.stderr)
        return []

    results: list[tuple[str, str, str]] = []
    try:
        # timeout=30: without this, imaplib's underlying socket blocks
        # INDEFINITELY if Gmail's IMAP server is slow to respond or the
        # connection stalls partway through — no exception, no timeout,
        # just silence forever. Since fetch_new_replies() runs inside a
        # single background thread on a while-True timer
        # (chat_agent.py's _email_poll_loop), one hung connection here
        # doesn't just fail this one poll — it freezes the ENTIRE loop
        # permanently: no more polls, ever, until the process itself is
        # restarted (a redeploy). This was the actual cause of email
        # replies going completely silent for 25+ minutes with zero log
        # output, confirmed by comparing Render logs against a diagnosis
        # of this function. 30s is generous for a single IMAP handshake
        # over normal networking but still finite, so a genuinely stuck
        # connection surfaces as a caught, logged, recoverable error
        # instead of an invisible permanent hang.
        with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT, timeout=30) as imap:
            imap.login(gmail_address, gmail_app_password)
            imap.select("INBOX")
            since_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
            status, data = imap.search(None, f'(SINCE "{since_date}")')
            if status != "OK":
                print(f"[error] IMAP search failed: {status}", file=sys.stderr)
                return []
            # Newest first, so a burst of old backlog doesn't starve out
            # today's replies when max_messages caps how many we take.
            msg_ids = list(reversed(data[0].split()))
            processed_count = 0
            for msg_id in msg_ids:
                if processed_count >= max_messages:
                    break
                status, msg_data = imap.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                message_id = (_decode_str(msg.get("Message-ID", "")) or "").strip()
                if not message_id:
                    # No Message-ID header at all is rare and slightly
                    # suspicious (most legitimate mail servers always set
                    # one) — skip rather than risk re-processing the same
                    # mail every run with no way to dedupe it.
                    continue
                if db_storage.is_email_processed(message_id):
                    continue

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
                is_noreply_local = local_part in NOREPLY_LOCAL_PARTS
                is_known_notification_domain = any(
                    domain == d or domain.endswith("." + d) for d in KNOWN_NOTIFICATION_DOMAINS
                )
                if is_noreply_local or is_known_notification_domain:
                    print(f"[info] Skipping likely-automated sender: {sender_addr}", file=sys.stderr)
                    db_storage.mark_email_processed(message_id, sender_addr)
                    continue

                body = _extract_body(msg)
                body = _strip_quoted_reply(body)
                if not body:
                    # Some mobile Gmail clients let a user hit Send with a
                    # blank message body — the real intent ends up sitting
                    # in the Subject line instead (e.g. "Hello can you help
                    # me automate my clinic via your services" typed as the
                    # subject, body left empty). Rather than silently
                    # discarding what's very likely a real lead's real
                    # message, fall back to the subject as the body. Only
                    # do this when the body is genuinely empty — a real
                    # body, however short, is always preferred untouched.
                    subject = _decode_str(msg.get("Subject", "")).strip()
                    if subject:
                        print(f"[info] Empty body from {sender_addr} — using Subject as fallback body: {subject!r}", file=sys.stderr)
                        body = subject
                if body:
                    results.append((sender_addr, domain, body))
                    processed_count += 1
                # Record as processed in our own DB regardless of Gmail's
                # \Seen flag — this is the source of truth now, not IMAP
                # state, so it survives anything else touching this inbox.
                db_storage.mark_email_processed(message_id, sender_addr)
        return results
    except imaplib.IMAP4.error as e:
        print(f"[error] IMAP login/search failed: {e}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[error] Unexpected error polling Gmail: {e}", file=sys.stderr)
        return []
