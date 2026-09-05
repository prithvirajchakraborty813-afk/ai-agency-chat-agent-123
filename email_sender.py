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
    2. Sending goes through HTTPS provider APIs, not Gmail SMTP directly —
       Render's free tier blocks outbound SMTP ports (25/465/587), which
       silently broke every send before this was caught (see the
       BREVO_API_URL note further down for the full story). Set up one or
       more of the providers below; you only need one working, more is
       optional redundancy/extra daily volume (see MULTI-PROVIDER note).
    3. Set as env vars (or pass via CLI flags):
           GMAIL_ADDRESS=youraddress@gmail.com
           GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
           BREVO_API_KEY=xkeysib-xxxxxxxxxxxxxxxx

MULTI-PROVIDER SENDING (added later — Brevo was the only option before):
    send_email() now tries a list of providers in priority order and
    falls through to the next one if a provider is unreachable, rejects
    the send, or has already hit its own daily free-tier cap today
    (tracked via db_storage.get_provider_sends_today so this pipeline
    self-limits BEFORE a real bounce/suspension, not just after). This
    buys two things: (1) more combined daily volume than any single free
    tier alone, and (2) the pipeline keeps working if one provider has an
    outage or blocks the account. All of them are free-forever tiers, no
    credit card, similar few-minutes signup to Brevo above. Add ANY
    subset — every one is independently optional; unset ones are just
    skipped.

    Brevo    (existing, 300/day)  — already set up per step 2 above.
    Mailgun  (100/day)            — signup.mailgun.com free plan, verify a
                                     sending domain (Settings -> Sending ->
                                     Domains), get the API key from
                                     Settings -> API Keys.
                                       MAILGUN_API_KEY=xxxxxxxxxxxxxxxx
                                       MAILGUN_DOMAIN=mg.yourdomain.com
    Resend   (3,000/mo, 100/day)  — resend.com signup, verify a domain
                                     under Domains, create a key under
                                     API Keys.
                                       RESEND_API_KEY=re_xxxxxxxxxxxxxxxx
    SMTP2GO  (1,000/mo)           — smtp2go.com free signup, verify a
                                     sender under Settings -> Sender
                                     Domains, create a key under Settings
                                     -> API Keys (grant "Email Send"
                                     permission).
                                       SMTP2GO_API_KEY=api-XXXXXXXXXXXXXXXX
    Elastic Email (100/day)       — elasticemail.com free signup, verify a
                                     sender under Settings -> Domains, get
                                     the key from Settings -> API.
                                       ELASTICEMAIL_API_KEY=xxxxxxxxxxxxxxxx
    MailerSend    (3,000/mo,
                   100/day trial) — mailersend.com free signup, verify a
                                     domain under Domains, create a token
                                     under Integrations -> API tokens
                                     (needs "Email" full access).
                                       MAILERSEND_API_KEY=mlsn.xxxxxxxxxxxx
    SendGrid (100/day,
              60-DAY TRIAL ONLY —   — signup.sendgrid.com. As of 2026 the
              see note)              old permanent-free plan is gone: new
                                     accounts get 100/day for 60 days, then
                                     must move to a paid plan (~$19.95/mo)
                                     to keep sending. Set up if you want a
                                     temporary boost, but don't rely on it
                                     staying free — verify a Single Sender
                                     (Settings -> Sender Authentication, no
                                     domain DNS needed), then create a key
                                     under Settings -> API Keys ("Mail
                                     Send" access).
                                       SENDGRID_API_KEY=SG.xxxxxxxxxxxxxxxx
    Mailjet  (200/day, 6,000/mo,
              free forever)        — mailjet.com free signup, verify a
                                     sender under Senders & Domains, get
                                     both keys from Account Settings ->
                                     API Key Management.
                                       MAILJET_API_KEY=xxxxxxxxxxxxxxxx
                                       MAILJET_SECRET_KEY=xxxxxxxxxxxxxxxx
    Mailtrap (1,000/mo)            — mailtrap.io free signup, under Email
                                     Sending verify a sending domain, then
                                     create a token under Sending Domains
                                     -> API Tokens.
                                       MAILTRAP_API_TOKEN=xxxxxxxxxxxxxxxx
    Postmark (100/mo,
              free forever)        — postmarkapp.com free signup, verify a
                                     Sender Signature (single address, no
                                     domain DNS needed — Sender Signatures
                                     -> Add) or a full domain for higher
                                     trust, then get the Server API Token
                                     from the server's API Tokens tab. Low
                                     monthly cap (100/mo, not /day) — best
                                     used as one more fallback, not a
                                     primary volume source.
                                       POSTMARK_SERVER_TOKEN=xxxxxxxxxxxxxxxx
    Zoho ZeptoMail (transactional-
                    only, free
                    credit trial)  — zeptomail.zoho.com signup, verify a
                                     sending domain under Setup -> Domains,
                                     create a Mail Agent, then get the
                                     token from Setup -> Mail Agents ->
                                     [agent] -> API. ZeptoMail runs on a
                                     credit system after the initial free
                                     trial credits run out (not a fixed
                                     daily/monthly cap like the others) —
                                     check current pricing on their site
                                     before relying on it.
                                       ZEPTOMAIL_TOKEN=xxxxxxxxxxxxxxxx

    Priority order defaults to Brevo -> Mailjet -> Elastic Email -> Mailgun
    -> Resend -> MailerSend -> SendGrid -> SMTP2GO -> Mailtrap -> Postmark
    -> ZeptoMail (roughly highest-to-lowest free cap; the last two are
    added at the end since their free tiers are the smallest/least
    reliable of the set — see their notes above). Override with:
           EMAIL_PROVIDER_ORDER=resend,brevo,mailgun,smtp2go
    (comma-separated provider names from PROVIDERS below; unknown/unset
    ones are ignored, so a typo just falls back to the default order
    rather than erroring).

    Adding a further provider later: add one entry to the PROVIDERS list
    below (name, required env vars, daily_limit, a `send` function with
    the same (from_name, from_addr, reply_to, to_addr, subject, text,
    html) -> (ok, detail, message_id) signature as the others) — nothing
    else needs to change, send_email() picks it up automatically.

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
from typing import Optional

import dns.resolver

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
MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SMTP2GO_API_KEY = os.environ.get("SMTP2GO_API_KEY", "")
ELASTICEMAIL_API_KEY = os.environ.get("ELASTICEMAIL_API_KEY", "")
MAILERSEND_API_KEY = os.environ.get("MAILERSEND_API_KEY", "")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
MAILJET_API_KEY = os.environ.get("MAILJET_API_KEY", "")
MAILJET_SECRET_KEY = os.environ.get("MAILJET_SECRET_KEY", "")
MAILTRAP_API_TOKEN = os.environ.get("MAILTRAP_API_TOKEN", "")
POSTMARK_SERVER_TOKEN = os.environ.get("POSTMARK_SERVER_TOKEN", "")
ZEPTOMAIL_TOKEN = os.environ.get("ZEPTOMAIL_TOKEN", "")
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
    blank/missing domain (nothing to guess from), OR for a domain that
    has no mail-capable DNS at all (see _domain_has_mail_capable_dns) —
    no point guessing addresses at a domain that can't receive mail full
    stop, that's a wasted send + a guaranteed bounce every time."""
    d = (domain or "").strip().lower()
    if not d or "." not in d:
        return []
    # Finder scripts sometimes store a bare domain, sometimes a full URL —
    # normalize either shape down to just the domain part.
    d = d.replace("https://", "").replace("http://", "").split("/")[0]
    if not _domain_has_mail_capable_dns(d):
        return []
    return [f"{prefix}@{d}" for prefix in EMAIL_GUESS_PREFIXES]


# ---------------------------------------------------------------------------
# Deliverability pre-checks — layered, both optional-by-design so a DNS
# hiccup or a missing API key degrades to the OLD behavior (try the send
# anyway) rather than silently blocking every guessed lead.
#
# LAYER 1 — _domain_has_mail_capable_dns(): free, no signup, no API key,
# works from anywhere (plain DNS over port 53 — never blocked, unlike SMTP
# port 25 which Render/GitHub Actions/most cloud hosts block outbound by
# default, which is exactly why real per-mailbox SMTP verification isn't
# viable in this pipeline's infra without a third-party API — see LAYER 2).
# Checks whether the domain has an MX record, or failing that an A/AAAA
# record (RFC 5321 allows mail delivery to fall back to the A record when
# no MX exists). Domain has neither -> it categorically cannot receive
# mail -> skip guessing entirely, guaranteed bounce avoided for free. This
# catches dead/parked/expired domains and misc junk (e.g. some free
# page-builder domains) but will NOT catch "domain has real mail, but
# info@/contact@ specifically doesn't exist there" — that's most of what's
# actually bouncing right now (soft bounces, not hard "no such domain").
#
# LAYER 2 — verify_email_deliverable(): real per-mailbox check via
# AbstractAPI's free tier (100 requests/month, no card required — sign up
# at https://www.abstractapi.com/api/email-verification-validation-api,
# then set ABSTRACT_API_KEY as an env var). This is the layer that
# actually catches "domain's fine, but info@ isn't a real inbox" — the
# dominant failure mode seen in Brevo's bounce log. Only runs if
# ABSTRACT_API_KEY is set; if unset, this layer is a no-op (returns None
# = "unknown, proceed") so nothing breaks before you've signed up. Given
# current send volume (~20-40 guessed sends/day), the 100/month free tier
# will run out fast — treat this as a limited budget: it's applied to
# guessed addresses only (never real captured emails), so spend it there.
# ---------------------------------------------------------------------------

ABSTRACT_API_KEY = os.environ.get("ABSTRACT_API_KEY", "")
ABSTRACT_API_URL = "https://emailvalidation.abstractapi.com/v1/"


def _domain_has_mail_capable_dns(domain: str) -> bool:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        if len(answers) > 0:
            return True
    except Exception:
        pass  # NXDOMAIN, NoAnswer, Timeout, etc. — fall through to A/AAAA check
    for rtype in ("A", "AAAA"):
        try:
            if len(dns.resolver.resolve(domain, rtype, lifetime=5)) > 0:
                return True
        except Exception:
            continue
    return False


def verify_email_deliverable(addr: str) -> Optional[bool]:
    """True = AbstractAPI says this mailbox is deliverable. False =
    confirmed undeliverable (skip sending, save the guaranteed bounce).
    None = unknown — either ABSTRACT_API_KEY isn't set, or the API call
    itself failed (network error, rate limit, bad response) — treated as
    'proceed with the send anyway' so a flaky check never blocks a lead
    that might have gone through fine."""
    if not ABSTRACT_API_KEY:
        return None
    try:
        resp = requests.get(
            ABSTRACT_API_URL,
            params={"api_key": ABSTRACT_API_KEY, "email": addr},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        deliverability = (data.get("deliverability") or "").upper()
        if deliverability == "UNDELIVERABLE":
            return False
        if deliverability == "DELIVERABLE":
            return True
        return None  # "UNKNOWN" from the API — genuinely ambiguous, don't block
    except Exception:
        return None


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


def _send_via_brevo(from_name: str, from_addr: str, reply_to: str,
                     to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    """message_id in the return is Brevo's own id for this send (from the
    API response's "messageId" field) — it's what brevo_webhook.py's
    delivery/bounce events arrive keyed on (see
    db_storage.update_delivery_status()), so callers that want status
    tracking must save it. Other providers below return "" for message_id
    since there's no matching webhook wired up for them yet — status
    tracking currently only works for sends that went out via Brevo."""
    payload = {
        "sender": {"email": from_addr, "name": from_name},
        "to": [{"email": to_addr}],
        "subject": subject,
        "textContent": text,
        "htmlContent": html,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
    }
    if reply_to and reply_to != from_addr:
        payload["replyTo"] = {"email": reply_to}
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
            message_id = ""
            try:
                message_id = resp.json().get("messageId", "")
            except Exception:
                pass
            return True, f"accepted by Brevo for delivery to {to_addr}", message_id
        return False, f"Brevo API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


def _send_via_mailgun(from_name: str, from_addr: str, reply_to: str,
                       to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    if not MAILGUN_DOMAIN:
        return False, "MAILGUN_DOMAIN not set", ""
    data = {
        "from": f"{from_name} <{from_addr}>",
        "to": to_addr,
        "subject": subject,
        "text": text,
        "html": html,
    }
    if reply_to and reply_to != from_addr:
        data["h:Reply-To"] = reply_to
    try:
        resp = requests.post(
            f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
            auth=("api", MAILGUN_API_KEY),
            data=data,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            message_id = ""
            try:
                message_id = resp.json().get("id", "")
            except Exception:
                pass
            return True, f"accepted by Mailgun for delivery to {to_addr}", message_id
        return False, f"Mailgun API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


def _send_via_resend(from_name: str, from_addr: str, reply_to: str,
                      to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    payload = {
        "from": f"{from_name} <{from_addr}>",
        "to": [to_addr],
        "subject": subject,
        "text": text,
        "html": html,
    }
    if reply_to and reply_to != from_addr:
        payload["reply_to"] = reply_to
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            message_id = ""
            try:
                message_id = resp.json().get("id", "")
            except Exception:
                pass
            return True, f"accepted by Resend for delivery to {to_addr}", message_id
        return False, f"Resend API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


def _send_via_smtp2go(from_name: str, from_addr: str, reply_to: str,
                       to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    payload = {
        "api_key": SMTP2GO_API_KEY,
        "to": [f"{to_addr}"],
        "sender": f"{from_name} <{from_addr}>",
        "subject": subject,
        "text_body": text,
        "html_body": html,
    }
    if reply_to and reply_to != from_addr:
        payload["custom_headers"] = [{"header": "Reply-To", "value": reply_to}]
    try:
        resp = requests.post(
            "https://api.smtp2go.com/v3/email/send",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json() or {}
            succeeded = ((data.get("data") or {}).get("succeeded", 0))
            if succeeded:
                message_id = ""
                try:
                    email_ids = (data.get("data") or {}).get("email_id", [])
                    message_id = email_ids[0] if email_ids else ""
                except Exception:
                    pass
                return True, f"accepted by SMTP2GO for delivery to {to_addr}", message_id
            return False, f"SMTP2GO reported 0 succeeded: {resp.text[:300]}", ""
        return False, f"SMTP2GO API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


def _send_via_elasticemail(from_name: str, from_addr: str, reply_to: str,
                            to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    payload = {
        "Recipients": [{"Email": to_addr}],
        "Content": {
            "From": f"{from_name} <{from_addr}>",
            "Subject": subject,
            "Body": [
                {"ContentType": "HTML", "Content": html},
                {"ContentType": "PlainText", "Content": text},
            ],
        },
    }
    if reply_to and reply_to != from_addr:
        payload["Content"]["ReplyTo"] = reply_to
    try:
        resp = requests.post(
            "https://api.elasticemail.com/v4/emails",
            headers={
                "X-ElasticEmail-ApiKey": ELASTICEMAIL_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            message_id = ""
            try:
                message_id = resp.json().get("MessageID") or resp.json().get("TransactionID", "")
            except Exception:
                pass
            return True, f"accepted by Elastic Email for delivery to {to_addr}", message_id
        return False, f"Elastic Email API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


def _send_via_mailersend(from_name: str, from_addr: str, reply_to: str,
                          to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    payload = {
        "from": {"email": from_addr, "name": from_name},
        "to": [{"email": to_addr}],
        "subject": subject,
        "text": text,
        "html": html,
    }
    if reply_to and reply_to != from_addr:
        payload["reply_to"] = {"email": reply_to}
    try:
        resp = requests.post(
            "https://api.mailersend.com/v1/email",
            headers={
                "Authorization": f"Bearer {MAILERSEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201, 202):
            # MailerSend doesn't return a body on success — the message id
            # comes back in the X-Message-Id response header instead.
            message_id = resp.headers.get("X-Message-Id", "")
            return True, f"accepted by MailerSend for delivery to {to_addr}", message_id
        return False, f"MailerSend API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


def _send_via_sendgrid(from_name: str, from_addr: str, reply_to: str,
                        to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    payload = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": from_addr, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text},
            {"type": "text/html", "value": html},
        ],
    }
    if reply_to and reply_to != from_addr:
        payload["reply_to"] = {"email": reply_to}
    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201, 202):
            # SendGrid also returns no JSON body on success — message id
            # comes back in the X-Message-Id response header instead,
            # same pattern as MailerSend above.
            message_id = resp.headers.get("X-Message-Id", "")
            return True, f"accepted by SendGrid for delivery to {to_addr}", message_id
        return False, f"SendGrid API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


def _send_via_mailjet(from_name: str, from_addr: str, reply_to: str,
                       to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    message = {
        "From": {"Email": from_addr, "Name": from_name},
        "To": [{"Email": to_addr}],
        "Subject": subject,
        "TextPart": text,
        "HTMLPart": html,
    }
    if reply_to and reply_to != from_addr:
        message["ReplyTo"] = {"Email": reply_to}
    try:
        resp = requests.post(
            "https://api.mailjet.com/v3.1/send",
            auth=(MAILJET_API_KEY, MAILJET_SECRET_KEY),
            json={"Messages": [message]},
            timeout=20,
        )
        if resp.status_code in (200, 201):
            message_id = ""
            try:
                data = resp.json()
                msgs = data.get("Messages", [])
                if msgs and msgs[0].get("Status") == "success":
                    to_list = msgs[0].get("To", [])
                    message_id = str(to_list[0].get("MessageID", "")) if to_list else ""
                elif msgs and msgs[0].get("Status") != "success":
                    errs = msgs[0].get("Errors", [])
                    err_text = "; ".join(e.get("ErrorMessage", "") for e in errs) or "unknown Mailjet error"
                    return False, f"Mailjet rejected the send: {err_text}", ""
            except Exception:
                pass
            return True, f"accepted by Mailjet for delivery to {to_addr}", message_id
        return False, f"Mailjet API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


# Priority order + per-provider daily_limit (the FREE-TIER cap, used only
# to self-limit BEFORE a real bounce — see MULTI-PROVIDER note in the
# module docstring). env_required lists the env var(s) that must be set
# for this provider to be attempted at all; missing any of them skips it
# silently (not an error — that's the "every provider is optional" design).
def _send_via_mailtrap(from_name: str, from_addr: str, reply_to: str,
                        to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    payload = {
        "from": {"email": from_addr, "name": from_name},
        "to": [{"email": to_addr}],
        "subject": subject,
        "text": text,
        "html": html,
    }
    if reply_to and reply_to != from_addr:
        payload["reply_to"] = {"email": reply_to}
    try:
        resp = requests.post(
            "https://send.api.mailtrap.io/api/send",
            headers={
                "Authorization": f"Bearer {MAILTRAP_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            message_id = ""
            try:
                ids = resp.json().get("message_ids", [])
                message_id = ids[0] if ids else ""
            except Exception:
                pass
            return True, f"accepted by Mailtrap for delivery to {to_addr}", message_id
        return False, f"Mailtrap API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


def _send_via_postmark(from_name: str, from_addr: str, reply_to: str,
                        to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    """Postmark's free tier is 100/MONTH (not /day like most others here)
    — deliberately placed last in PROVIDERS below so it's only used once
    every higher-cap provider is exhausted for the day."""
    payload = {
        "From": f"{from_name} <{from_addr}>" if from_name else from_addr,
        "To": to_addr,
        "Subject": subject,
        "TextBody": text,
        "HtmlBody": html,
        "MessageStream": "outbound",
    }
    if reply_to and reply_to != from_addr:
        payload["ReplyTo"] = reply_to
    try:
        resp = requests.post(
            "https://api.postmarkapp.com/email",
            headers={
                "X-Postmark-Server-Token": POSTMARK_SERVER_TOKEN,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json() if resp.text else {}
            if data.get("ErrorCode", 0) != 0:
                return False, f"Postmark rejected the send: {data.get('Message', resp.text[:300])}", ""
            return True, f"accepted by Postmark for delivery to {to_addr}", data.get("MessageID", "")
        return False, f"Postmark API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


def _send_via_zeptomail(from_name: str, from_addr: str, reply_to: str,
                         to_addr: str, subject: str, text: str, html: str) -> tuple[bool, str, str]:
    """ZeptoMail runs on a credit system after its initial free trial
    credits, not a fixed recurring daily/monthly cap like the others in
    this file — daily_limit below is a conservative placeholder, not a
    number ZeptoMail itself publishes. Check current pricing/credits on
    your own dashboard before relying on this for real volume."""
    payload = {
        "from": {"address": from_addr, "name": from_name},
        "to": [{"email_address": {"address": to_addr}}],
        "subject": subject,
        "htmlbody": html,
        "textbody": text,
    }
    if reply_to and reply_to != from_addr:
        payload["reply_to"] = [{"address": reply_to}]
    try:
        resp = requests.post(
            "https://api.zeptomail.com/v1.1/email",
            headers={
                "Authorization": ZEPTOMAIL_TOKEN,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            data = resp.json() if resp.text else {}
            message_id = ""
            try:
                message_id = data.get("data", [{}])[0].get("additional_info", {}).get("message_id", "")
            except Exception:
                pass
            return True, f"accepted by ZeptoMail for delivery to {to_addr}", message_id
        return False, f"ZeptoMail API returned {resp.status_code}: {resp.text[:300]}", ""
    except Exception as e:
        return False, str(e), ""


PROVIDERS = [
    {"name": "brevo", "env_required": ["BREVO_API_KEY"], "daily_limit": 300, "send": _send_via_brevo},
    {"name": "mailjet", "env_required": ["MAILJET_API_KEY", "MAILJET_SECRET_KEY"], "daily_limit": 200, "send": _send_via_mailjet},
    {"name": "elasticemail", "env_required": ["ELASTICEMAIL_API_KEY"], "daily_limit": 100, "send": _send_via_elasticemail},
    {"name": "mailgun", "env_required": ["MAILGUN_API_KEY", "MAILGUN_DOMAIN"], "daily_limit": 100, "send": _send_via_mailgun},
    {"name": "resend", "env_required": ["RESEND_API_KEY"], "daily_limit": 100, "send": _send_via_resend},
    {"name": "mailersend", "env_required": ["MAILERSEND_API_KEY"], "daily_limit": 100, "send": _send_via_mailersend},
    {"name": "sendgrid", "env_required": ["SENDGRID_API_KEY"], "daily_limit": 100, "send": _send_via_sendgrid},
    {"name": "smtp2go", "env_required": ["SMTP2GO_API_KEY"], "daily_limit": 33, "send": _send_via_smtp2go},
    {"name": "mailtrap", "env_required": ["MAILTRAP_API_TOKEN"], "daily_limit": 33, "send": _send_via_mailtrap},
    {"name": "postmark", "env_required": ["POSTMARK_SERVER_TOKEN"], "daily_limit": 3, "send": _send_via_postmark},
    {"name": "zeptomail", "env_required": ["ZEPTOMAIL_TOKEN"], "daily_limit": 20, "send": _send_via_zeptomail},
]


def _ordered_providers() -> list[dict]:
    """Applies EMAIL_PROVIDER_ORDER if set (comma-separated provider
    names), otherwise the PROVIDERS list's own default order. Unknown
    names in the override are ignored rather than erroring, so a typo
    just falls back to default order instead of breaking sends."""
    order_env = os.environ.get("EMAIL_PROVIDER_ORDER", "").strip()
    if not order_env:
        return PROVIDERS
    by_name = {p["name"]: p for p in PROVIDERS}
    ordered = [by_name[n.strip()] for n in order_env.split(",") if n.strip() in by_name]
    # Anything in PROVIDERS but not mentioned in the override still gets
    # tried, just after everything the override named explicitly — an
    # override is a preference, not meant to silently disable providers.
    remaining = [p for p in PROVIDERS if p not in ordered]
    return ordered + remaining


def send_email(gmail_address: str, gmail_app_password: str, to_addr: str,
                subject: str, body: str) -> tuple[bool, str, str]:
    """Tries each configured provider in priority order (see PROVIDERS /
    EMAIL_PROVIDER_ORDER above), returning on the first one that accepts
    the send. Returns (ok, detail, message_id) — detail says which
    provider succeeded (or, on total failure, what every attempted
    provider said). message_id is only meaningful for delivery-status
    tracking when the send went via Brevo (see _send_via_brevo).

    A provider is skipped, not attempted, when: its required env var(s)
    aren't set (not configured at all), or db_storage reports it's
    already sent >= its daily_limit today (self-imposed cap to avoid
    hitting the real free-tier limit and risking a bounce or account
    flag — see db_storage.get_provider_sends_today). If db_storage isn't
    reachable, the count check fails open (returns 0) so this never
    blocks a send the provider itself would have accepted.

    gmail_address/gmail_app_password are kept as required parameters for
    backward compatibility with every existing caller; gmail_app_password
    itself is unused here (still needed elsewhere in this module for IMAP
    polling, unaffected by any of this). The "from" address for every
    provider is SEND_FROM_EMAIL if set, else gmail_address. Reply-To is
    always set back to gmail_address (your Gmail — the one
    fetch_new_replies() above actually polls) when it differs from the
    from-address, for the same reason as before: SEND_FROM_EMAIL may
    point at a mailbox with no IMAP/forwarding, so replies would be
    invisible to this bot without Reply-To redirecting them to Gmail.
    """
    from_addr = SEND_FROM_EMAIL or gmail_address
    if not from_addr:
        return False, "Neither SEND_FROM_EMAIL nor GMAIL_ADDRESS set — no from-address available", ""

    html = _plain_text_to_html(body)
    attempts: list[str] = []
    any_configured = False

    try:
        import db_storage
    except Exception:
        db_storage = None  # count-check below degrades to "always try" if unavailable

    for provider in _ordered_providers():
        if not all(os.environ.get(v) for v in provider["env_required"]):
            continue  # not configured — silently skip, this is expected/normal
        any_configured = True

        if db_storage is not None:
            sent_today = db_storage.get_provider_sends_today(provider["name"])
            if sent_today >= provider["daily_limit"]:
                attempts.append(f"{provider['name']}: skipped — already at today's self-imposed cap ({sent_today}/{provider['daily_limit']})")
                continue

        ok, detail, message_id = provider["send"]("Vortex AI", from_addr, gmail_address, to_addr, subject, body, html)
        if ok:
            if db_storage is not None:
                db_storage.record_provider_send(provider["name"])
            return True, f"[{provider['name']}] {detail}", message_id
        attempts.append(f"{provider['name']}: {detail}")

    if not any_configured:
        return False, "No email provider configured — set at least one of BREVO_API_KEY, MAILJET_API_KEY+MAILJET_SECRET_KEY, MAILGUN_API_KEY+MAILGUN_DOMAIN, RESEND_API_KEY, SMTP2GO_API_KEY, ELASTICEMAIL_API_KEY, MAILERSEND_API_KEY, SENDGRID_API_KEY, MAILTRAP_API_TOKEN, POSTMARK_SERVER_TOKEN, ZEPTOMAIL_TOKEN (see email_sender.py module notes)", ""
    return False, "All configured providers failed — " + "; ".join(attempts), ""


def send_email_with_fallback_guesses(gmail_address: str, gmail_app_password: str,
                                       domain: str, subject: str, body: str) -> tuple[bool, str, str]:
    """Tries each guessed address in turn, stopping at the first one
    Brevo's API accepts. Note this does NOT confirm delivery or that the
    address is real — see module docstring. Returns (ok, detail,
    message_id) — same third element as send_email() above, from
    whichever guess actually succeeded."""
    candidates = guess_email_addresses(domain)
    if not candidates:
        return False, "no domain on file, or domain has no mail-capable DNS — nothing to guess", ""

    last_detail = ""
    any_attempted = False
    for addr in candidates:
        verified = verify_email_deliverable(addr)
        if verified is False:
            last_detail = f"{addr}: skipped — AbstractAPI confirmed undeliverable"
            continue
        any_attempted = True
        ok, detail, message_id = send_email(gmail_address, gmail_app_password, addr, subject, body)
        if ok:
            return True, f"sent to guessed address {addr} ({detail})", message_id
        last_detail = f"{addr}: {detail}"
    if not any_attempted:
        return False, f"all guessed addresses pre-filtered as undeliverable — {last_detail}", ""
    return False, f"all guessed addresses failed — last error: {last_detail}", ""


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
