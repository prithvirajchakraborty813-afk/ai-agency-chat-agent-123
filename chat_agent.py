#!/usr/bin/env python3
"""
chat_agent.py — Agent #7, the Chat Agent. This is the piece that was
missing before: webhook_receiver.py proved replies can be RECEIVED
reliably; this module is what actually THINKS about a reply and SENDS
one back.

WHAT THIS DOES, end to end:
  1. An incoming WhatsApp message arrives (via webhook_receiver.py).
  2. Route it: is this from the business owner (Billo), or from a lead
     currently being negotiated with? Same-shaped event, different
     handling entirely.
  3a. If it's a LEAD: load that lead's conversation history, ask
      Gemini what to say next (discover needs / answer questions /
      propose price from tiers_agency.json), send the reply, save the
      updated history.
  3b. If it's the OWNER: check if they're replying to a pending
      approval request. If their message means "approve", lock in the
      order via order_contract.create_order() and send the customer
      the final contract + payment link. If not recognized as an
      approval, don't guess — just log it, no action.
  4. When Gemini decides a lead conversation has enough information to
     propose firm terms, it does NOT send those terms to the lead
     directly — it sends a summary to the OWNER and waits. This is the
     non-negotiable human-approval checkpoint from the project design;
     nothing here bypasses it.

WHAT THIS DOES NOT DO YET:
  - Auto-detect an actual bank/UPI payment landing — there's no API
    for that on a personal UPI account. For now, telling the system
    "payment of ₹X came in for order Y" is still a manual step (see
    the __main__ block at the bottom for how to do that by hand).
  - Handle voice or email — WhatsApp only, per the project's channel
    decision.

HOW TO RUN THIS: this REPLACES running webhook_receiver.py directly.
Same setup — point your WAHA session's webhook at this app's /webhook
(WAHA session config, "webhooks" -> "url", event "message") — just run
this file instead. It does everything webhook_receiver.py did
(logging every raw payload, so nothing is ever silently lost) plus
the actual conversation logic.

Needs the same Vertex AI setup as the other agents:
    1. gcloud auth application-default login
    2. gcloud config set project YOUR_PROJECT_ID
    3. pip install flask requests google-auth

Set these before running (or edit the constants below):
    OWNER_PHONE      — Billo's own WhatsApp number, digits only, no +
    WAHA_BASE_URL     — your self-hosted WAHA instance URL, e.g.
                        http://localhost:3000 or your Render URL for it
    WAHA_SESSION      — WAHA session name (default "default")
    WAHA_API_KEY      — WAHA's X-Api-Key, if you set one (optional locally,
                        strongly recommended once WAHA is exposed on Render)
    GCP_PROJECT       — same project the other Gemini agents use
    OWNER_UPI_ID      — the UPI ID customers will pay to

WHY WAHA INSTEAD OF WHAPI.CLOUD: Whapi.Cloud's free tier caps message
volume; once that cap is hit, sends start failing. WAHA is a
self-hosted, open-source WhatsApp HTTP API (runs as a Docker
container you deploy yourself, e.g. as its own Render service) with
no message-volume cap — WhatsApp's own per-number sending limits
still apply, but there is no vendor tier to run out of. It talks to
WhatsApp Web under the hood (same idea as Whapi.Cloud), so the same
personal-number/ban-risk tradeoff from before still applies.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
import google.auth
import google.auth.transport.requests
from flask import Flask, request, jsonify

import order_contract
import db_storage
import email_sender

# ---------------------------------------------------------------------------
# Vertex AI auth on Render — Render has no gcloud CLI and no attached
# service account (unlike Cloud Run), so google.auth.default() finds
# nothing by default. The org's policy blocking service-account KEY
# creation does NOT block the ADC file gcloud already generated locally
# (it's a refresh-token "authorized_user" credential, not a service
# account key) — so we just hand that same file to Render as a secret
# env var and write it to disk before any Gemini call happens. This must
# run before google.auth.default() is ever called, so it lives at import
# time, above everything else that might trigger it.
# ---------------------------------------------------------------------------
_ADC_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON", "")
if _ADC_JSON and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    _adc_path = "/tmp/adc.json"
    with open(_adc_path, "w", encoding="utf-8") as _f:
        _f.write(_ADC_JSON)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _adc_path
    print("[info] Wrote ADC credentials from GOOGLE_APPLICATION_CREDENTIALS_JSON to /tmp/adc.json")

# ---------------------------------------------------------------------------
# Configuration — same pattern as send_proposals.py / gemini_vertex_qualifier.py
# ---------------------------------------------------------------------------

OWNER_PHONE = os.environ.get("OWNER_PHONE", "919433066933")  # digits only, no +
WAHA_BASE_URL = os.environ.get("WAHA_BASE_URL", "http://localhost:3000").rstrip("/")
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
OWNER_UPI_ID = os.environ.get("OWNER_UPI_ID", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "")  # where owner alerts go if OWNER_NOTIFY_CHANNEL=email
# "whatsapp" (default) or "email" — a manual switch for when WhatsApp itself
# is restricted/down and owner alerts need a channel that still works. Does
# NOT change how leads are messaged, only where the *owner's* pings
# ("Ready to propose", "Sent. Order X — awaiting payment", etc.) go.
OWNER_NOTIFY_CHANNEL = os.environ.get("OWNER_NOTIFY_CHANNEL", "whatsapp").strip().lower()
POLL_EMAIL_SECRET = os.environ.get("POLL_EMAIL_SECRET", "")  # shared secret so /poll-email can't be hit by randoms
EMAIL_REPLY_SUBJECT = "Re: your message"
# Base URL of this very service on Render, e.g. https://ai-agency-chat-agent-123.onrender.com
# (no trailing slash) — same value already used as a GitHub Actions secret for
# /poll-email, just also set here as a Render env var so this app can build a
# link back to itself for the /pay/<order_id> fallback page. Not required for
# anything else, so a missing value only degrades the payment-page link, it
# never blocks WhatsApp/email sending.
RENDER_APP_URL = os.environ.get("RENDER_APP_URL", "").rstrip("/")


def _to_chat_id(digits_only_phone: str) -> str:
    """WAHA identifies chats as '<digits>@c.us' (no '+', no spaces) rather
    than Whapi's bare digit-string 'to' field. Every send_* function below
    goes through this so callers can keep passing the same plain digit
    strings used everywhere else in this codebase (db_storage, order_contract,
    etc.) without having to know about WAHA's chatId format."""
    phone = digits_only_phone.strip()
    if phone.endswith("@c.us"):
        return phone
    return f"{phone}@c.us"


def _waha_headers(extra: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    if extra:
        headers.update(extra)
    return headers

# Loop-limit guard for the "package_selected" stage: if the lead has sent
# this many messages without Gemini ever returning ready_to_propose, the
# owner gets a one-time heads-up that the conversation looks stuck. The
# bot keeps replying to the lead as normal either way — this only adds
# visibility for the owner, it never changes what the lead sees.
STALLED_CONVERSATION_TURN_LIMIT = 8

LOCATION = "us-central1"
VERTEX_ENDPOINT_TEMPLATE = (
    "https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
    "/locations/{location}/publishers/google/models/{model}:generateContent"
)
DEFAULT_MODEL = "gemini-2.5-flash-lite"
AUTH_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

CONVERSATIONS_PATH = "conversations.json"  # unused now — kept only so old references don't NameError; state lives in Postgres via db_storage
LOG_PATH = "incoming_log.json"  # unused now — see above
_state_lock = threading.Lock()

app = Flask(__name__)


def _startup_checks() -> None:
    """Runs once at import time — this fires both when the file is run
    directly (python chat_agent.py) AND when gunicorn imports it as
    chat_agent:app (the Procfile's actual production path). The old
    version of these checks, plus the db_storage.init_db() call, lived
    only inside `if __name__ == "__main__":`, which gunicorn never
    executes — so on Render the Postgres tables were never being
    created, and the very first real webhook hit would fail querying
    tables that don't exist yet."""
    print("Chat agent starting...")
    if not WAHA_BASE_URL:
        print("[warning] WAHA_BASE_URL is not set — sending will fail until you export it.")
    if not GCP_PROJECT:
        print("[warning] GCP_PROJECT is not set — Gemini calls will fail until you export it.")
    if not _ADC_JSON and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        print("[warning] GOOGLE_APPLICATION_CREDENTIALS_JSON is not set — Gemini/Vertex AI calls will fail with an ADC error until you export it (see the paste-your-local-ADC-file instructions).")
    if not OWNER_UPI_ID:
        print("[warning] OWNER_UPI_ID is not set — contract messages will have a broken payment link until you export it.")
    if not RENDER_APP_URL:
        print("[warning] RENDER_APP_URL is not set — contract messages will skip the /pay/<order_id> fallback link until you export it (should be this service's own Render URL, e.g. https://ai-agency-chat-agent-123.onrender.com).")
    if not db_storage.DATABASE_URL:
        print("[warning] DATABASE_URL is not set — conversation/order state cannot be saved until you export it (see console.neon.tech or Render's Postgres add-on).")
    else:
        db_storage.init_db()
        print("Database tables ready (created if they didn't already exist).")


_startup_checks()


# ---------------------------------------------------------------------------
# Gemini client — identical pattern to gemini_vertex_qualifier.py, reused
# so auth/retry behavior is proven, not reinvented.
# ---------------------------------------------------------------------------

class VertexGeminiClient:
    def __init__(self, project: str, model: str = DEFAULT_MODEL, location: str = LOCATION):
        self.project = project
        self.model = model
        self.location = location
        self.endpoint = VERTEX_ENDPOINT_TEMPLATE.format(location=location, project=project, model=model)
        try:
            self.credentials, _ = google.auth.default(scopes=AUTH_SCOPES)
        except Exception as e:
            raise ValueError(
                "Could not find Application Default Credentials. Run "
                "'gcloud auth application-default login' first, then retry.\n"
                f"(underlying error: {e})"
            )

    def _access_token(self) -> str:
        req = google.auth.transport.requests.Request()
        self.credentials.refresh(req)
        return self.credentials.token

    def generate_json(self, system: str, user: str, schema: dict,
                       temperature: float = 0.4, max_retries: int = 3) -> dict:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 700,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        last_err = None
        for attempt in range(max_retries):
            try:
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._access_token()}",
                }
                resp = requests.post(self.endpoint, headers=headers, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except Exception as e:
                last_err = e
                wait = 2 ** attempt
                print(f"  [warn] Vertex AI call failed ({e}); retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
        raise RuntimeError(f"Vertex AI call failed after {max_retries} attempts: {last_err}")


_gemini_client: Optional[VertexGeminiClient] = None


def get_gemini_client() -> VertexGeminiClient:
    global _gemini_client
    if _gemini_client is None:
        if not GCP_PROJECT:
            raise RuntimeError("GCP_PROJECT is not set — export it or edit the constant at the top of this file.")
        _gemini_client = VertexGeminiClient(project=GCP_PROJECT)
    return _gemini_client


# ---------------------------------------------------------------------------
# Sending messages — via a self-hosted WAHA instance for WhatsApp leads
# (see module docstring for why WAHA over Whapi.Cloud), or via Gmail SMTP
# for email leads. Every call site in this file calls send_whatsapp(id,
# text) with a single `id` string; the "email:" prefix (same convention
# send_proposals.py already uses for dedup keys) is what tells this
# function — and nothing else in the file — which channel to use. This
# means the whole stage machine, owner alerts, contracts, escalation
# handling, etc. all work for email leads with no changes anywhere else.
# ---------------------------------------------------------------------------

def _is_email_id(identifier: str) -> bool:
    return identifier.startswith("email:")


def send_whatsapp(to_phone: str, text: str) -> bool:
    # Owner-notify redirect: only fires for messages TO the owner, and only
    # when OWNER_NOTIFY_CHANNEL=email is explicitly set (e.g. while
    # WhatsApp itself is restricted). Lead-facing sends are never affected
    # by this — a lead's own channel (WhatsApp vs email:<domain>) is
    # decided separately, below, by their own identifier.
    if to_phone == OWNER_PHONE and OWNER_NOTIFY_CHANNEL == "email":
        if not OWNER_EMAIL:
            print("[error] OWNER_NOTIFY_CHANNEL=email but OWNER_EMAIL is not set — cannot alert owner.", file=sys.stderr)
            return False
        ok, detail = email_sender.send_email(
            GMAIL_ADDRESS, GMAIL_APP_PASSWORD, OWNER_EMAIL, "Agency update", text
        )
        if not ok:
            print(f"[error] Failed to email owner alert: {detail}", file=sys.stderr)
        return ok

    if _is_email_id(to_phone):
        convo = db_storage.load_conversation(to_phone)
        reply_to = (convo or {}).get("email_address", "")
        if not reply_to:
            print(f"[error] No email_address stored on conversation {to_phone} — cannot send.", file=sys.stderr)
            return False
        ok, detail = email_sender.send_email(
            GMAIL_ADDRESS, GMAIL_APP_PASSWORD, reply_to, EMAIL_REPLY_SUBJECT, text
        )
        if not ok:
            print(f"[error] Failed to send email to {reply_to}: {detail}", file=sys.stderr)
        return ok

    if not WAHA_BASE_URL:
        print("[error] WAHA_BASE_URL not set — cannot send message. Set it as an env var.", file=sys.stderr)
        return False
    try:
        resp = requests.post(
            f"{WAHA_BASE_URL}/api/sendText",
            headers=_waha_headers(),
            json={
                "session": WAHA_SESSION,
                "chatId": _to_chat_id(to_phone),
                "text": text,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[error] Failed to send WhatsApp message to {to_phone}: {e}", file=sys.stderr)
        return False


def send_whatsapp_image(to_phone: str, image_bytes: bytes, caption: str = "") -> bool:
    """Sends a raw image (e.g. a UPI QR code) as a WhatsApp image message,
    same failure-handling pattern as send_whatsapp above: never raises,
    logs and returns False on failure so a broken send never crashes the
    webhook handler mid-conversation.

    WHY THIS EXISTS: the upi://pay deep link in the text contract message
    fails to open on some phones/UPI apps ("no app found for this link")
    even though the same underlying pa/am/tn data is completely valid —
    confirmed by a real customer screenshot. A QR code encoding the exact
    same UPI string is far more universally scannable (any UPI app's
    built-in scanner reads it, no deep-link handling required), so it's
    sent as a companion to the text link, not a replacement — the text
    link still works for phones that DO support it, and typing the UPI ID
    manually is always still possible as a last resort.

    WAHA's /api/sendImage wants the image as base64 in file.data (or a
    public URL) — not multipart form data like Whapi's endpoint — since a
    QR code is generated in-memory here, base64 is the simpler path.
    """
    if _is_email_id(to_phone):
        # No image channel for email leads — the text contract's upi://
        # link plus the raw UPI ID (already in the text message) is the
        # fallback; this just needs to not crash the caller, since
        # _send_contract_and_qr treats image failure as non-fatal already.
        print(f"[info] Skipping QR image for {to_phone} — no image channel over email.")
        return False

    if not WAHA_BASE_URL:
        print("[error] WAHA_BASE_URL not set — cannot send image. Set it as an env var.", file=sys.stderr)
        return False
    try:
        import base64
        resp = requests.post(
            f"{WAHA_BASE_URL}/api/sendImage",
            headers=_waha_headers(),
            json={
                "session": WAHA_SESSION,
                "chatId": _to_chat_id(to_phone),
                "file": {
                    "mimetype": "image/png",
                    "filename": "payment_qr.png",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                },
                "caption": caption,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[error] Failed to send WhatsApp image to {to_phone}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Escalation detection — explicit keyword check, NOT left to Gemini.
# If a lead wants a human/call, the owner must be pinged immediately,
# regardless of what stage the conversation is in. Voice calling is
# dropped from this project, so the reply to the lead is always honest
# about WhatsApp-only, never a promise of a callback.
# ---------------------------------------------------------------------------

ESCALATION_PATTERNS = [
    "call me", "call back", "give me a call", "phone call",
    "talk to owner", "talk to the owner", "speak to owner",
    "real person", "actual person", "human", "not a bot",
    "speak to someone", "talk to someone",
]


def _format_phone_for_display(digits_with_country_code: str) -> str:
    """'919433066933' -> '+91 94330 66933'. Falls back to a '+'-prefixed
    version of whatever was configured if it isn't a 12-digit +91 number,
    so this never crashes on an unexpected OWNER_PHONE format."""
    d = digits_with_country_code.strip()
    if d.startswith("91") and len(d) == 12:
        national = d[2:]
        return f"+91 {national[:5]} {national[5:]}"
    return f"+{d}"


def build_escalation_reply() -> str:
    return (
        "No problem — you can reach the owner directly at "
        f"{_format_phone_for_display(OWNER_PHONE)}. Feel free to call or "
        "WhatsApp that number."
    )


def detect_escalation_request(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in ESCALATION_PATTERNS)


def handle_escalation(phone: str, text: str) -> None:
    convo = get_or_create_conversation(phone)
    already_flagged = convo.get("escalation_flagged", False)

    if not already_flagged:
        send_whatsapp(
            OWNER_PHONE,
            f"⚠️ Lead {phone} asked for a human / a call. Their message:\n"
            f"\"{text}\"\n\nThe agent just gave them your number directly, so "
            f"expect they may call or WhatsApp you at {phone} directly — "
            f"heads up in case they reach out before you see this chat.",
        )
        set_stage(phone, convo["stage"], escalation_flagged=True)
    # else: already told the owner once for this lead. Still answer the
    # lead below every time (repeating "here's the number" isn't spam the
    # same way re-pinging the owner is), just stop re-notifying the owner.

    reply = build_escalation_reply()
    send_whatsapp(phone, reply)
    append_history(phone, "agent", reply)


# ---------------------------------------------------------------------------
# Conversation state — per-lead-phone-number history + stage tracking
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Conversation state — per-lead-phone-number history + stage tracking
#
# Backed by Postgres (see db_storage.py) instead of a local JSON file, so
# state survives redeploys/restarts on ephemeral hosting (e.g. Render's
# free tier). Function names/signatures are unchanged from the old
# JSON-file version on purpose — every call site elsewhere in this file
# still works exactly as before; only what's behind these functions changed.
# ---------------------------------------------------------------------------

def _load_conversations() -> dict:
    """Kept for backward compatibility with any call site that still wants
    the full {phone: convo} mapping — now pulls every row from Postgres
    instead of reading one JSON file. Prefer get_or_create_conversation()
    for single-lead lookups; that hits one row, not the whole table."""
    return db_storage.load_all_conversations()


def _save_conversations(conversations: dict) -> None:
    """Kept for backward compatibility — writes every conversation in the
    dict back to its own row. Only used by call sites that still build a
    full dict in memory first; new code should prefer
    db_storage.save_conversation(phone, convo) for a single lead."""
    for phone, convo in conversations.items():
        db_storage.save_conversation(phone, convo)


def get_or_create_conversation(phone: str, email_address: str = "") -> dict:
    """`phone` is the conversation's identifier — a plain digit string for
    WhatsApp leads, or "email:<domain>" for email leads (see send_whatsapp
    above). email_address is only used the first time an email:<domain>
    conversation is created, to remember the real reply-to address (the
    domain alone isn't enough to send a reply to)."""
    with _state_lock:
        convo = db_storage.load_conversation(phone)
        if convo is None:
            convo = {
                "phone": phone,
                # Stage machine:
                #   warming_up -> catalog_shown -> package_selected
                #   -> awaiting_owner_approval -> awaiting_payment -> closed
                # A single owner APPROVE (for Starter/Growth, fixed price
                # already known) or SETPRICE (Custom only, no fixed price)
                # sends the contract immediately — no further confirmation
                # step after that.
                "stage": "warming_up",
                "history": [],  # list of {"role": "lead"|"agent", "text": ..., "at": ...}
                "pending_order_id": None,
                "selected_tier": None,       # set once customer picks from catalog
                "fixed_price": None,         # set once customer picks from catalog (None for custom)
                "created_at": _now(),
            }
            if _is_email_id(phone) and email_address:
                convo["email_address"] = email_address
            db_storage.save_conversation(phone, convo)
        elif _is_email_id(phone) and email_address and not convo.get("email_address"):
            # Backfill for a conversation row created before this field
            # existed, or created oddly without it — never overwrite a
            # real stored address with a new one silently.
            convo["email_address"] = email_address
            db_storage.save_conversation(phone, convo)
        return convo


def append_history(phone: str, role: str, text: str) -> dict:
    with _state_lock:
        convo = db_storage.load_conversation(phone)
        if convo is None:
            convo = {
                "phone": phone, "stage": "warming_up", "history": [],
                "pending_order_id": None, "selected_tier": None,
                "fixed_price": None, "created_at": _now(),
            }
        convo["history"].append({"role": role, "text": text, "at": _now()})
        db_storage.save_conversation(phone, convo)
        return convo


def set_stage(phone: str, stage: str, pending_order_id: Optional[str] = None,
              recommended_tier: Optional[str] = None, owner_summary: Optional[str] = None,
              selected_tier: Optional[str] = None, fixed_price: Optional[int] = None,
              stall_flagged: Optional[bool] = None, cancellation_flagged: Optional[bool] = None,
              escalation_flagged: Optional[bool] = None) -> None:
    with _state_lock:
        convo = db_storage.load_conversation(phone)
        if convo is None:
            return
        convo["stage"] = stage
        if pending_order_id is not None:
            convo["pending_order_id"] = pending_order_id
        if recommended_tier is not None:
            convo["recommended_tier"] = recommended_tier
        if owner_summary is not None:
            convo["owner_summary"] = owner_summary
        if selected_tier is not None:
            convo["selected_tier"] = selected_tier
        if fixed_price is not None:
            convo["fixed_price"] = fixed_price
        if stall_flagged is not None:
            convo["stall_flagged"] = stall_flagged
        if cancellation_flagged is not None:
            convo["cancellation_flagged"] = cancellation_flagged
        if escalation_flagged is not None:
            convo["escalation_flagged"] = escalation_flagged
        db_storage.save_conversation(phone, convo)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_to_log(entry: dict) -> None:
    with _state_lock:
        db_storage.append_log_entry(entry)


# ---------------------------------------------------------------------------
# Lead conversation logic
# ---------------------------------------------------------------------------

CHAT_SYSTEM_PROMPT = """You are a WhatsApp sales assistant for a solo AI \
automation agency in India. You are chatting with a small-business owner \
who was sent an initial outreach message and has replied. This message is \
used during the WARM-UP phase, before the pricing catalog has been shown. \
Your job right now:

1. Discover what they actually need (what kind of business, what problem \
they want automated — WhatsApp bot, website bot, booking, FAQs, etc).
2. Answer questions about the service in plain, warm, non-salesy language.
3. Once there has been genuine back-and-forth and you understand their \
business well enough that showing them a price catalog would make sense, \
set ready_for_catalog to true. Do NOT show a catalog on the very first \
reply — warm them up first with real conversation.
4. If not ready yet, just write the next reply to send back to the lead.

Important: if the lead asks for a phone call or to speak to a person, \
you don't need to handle it — a separate system detects that automatically \
and sends them the owner's direct number right away, before you're even \
asked to reply. You will not normally see those messages. Just keep \
focusing on discovering their needs; don't bring up calling yourself.

Be concise — this is WhatsApp, not email. Never state that a deal is \
confirmed or that payment has been arranged."""

# Used during the warming_up stage only.
WARMUP_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply_to_lead": {
            "type": "STRING",
            "description": "What to send back to the lead now. Empty string if ready_for_catalog is true (the catalog message is sent by code, not this reply).",
        },
        "ready_for_catalog": {"type": "BOOLEAN"},
    },
    "required": ["reply_to_lead", "ready_for_catalog"],
}

# Used during the package_selected stage — the lead has already picked a
# tier from the fixed-price catalog. The price itself is NOT up for
# discussion here (it's fixed and already locked in convo["fixed_price"]);
# this stage is only about nailing down scope details (business specifics,
# what exactly gets built) so the owner has enough to approve confidently.
SCOPE_SYSTEM_PROMPT = """You are a WhatsApp sales assistant for a solo AI \
automation agency in India. The lead has already picked a pricing package \
from the catalog — the price is fixed and NOT open for negotiation, so \
never discuss or imply a different price. Your job now is to gather enough \
concrete detail about their business (what exactly they want the bot/agent \
to handle, their business type, channel specifics) that the owner can \
review a clear summary and approve confidently.

Once you have enough detail to write a clear, specific owner_summary, set \
ready_to_propose to true and do NOT send anything further to the lead \
yet (leave reply_to_lead empty) — the owner needs to approve first.

Important: if the lead asks for a phone call or to speak to a person, \
you don't need to handle it — a separate system detects that automatically. \
You will not normally see those messages.

Be concise — this is WhatsApp, not email. Never state that a deal is \
confirmed, that a price has changed, or that payment has been arranged."""

CHAT_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply_to_lead": {
            "type": "STRING",
            "description": "What to send back to the lead now. Empty string if ready_to_propose is true and nothing should be sent to the lead yet.",
        },
        "ready_to_propose": {"type": "BOOLEAN"},
        "recommended_tier": {"type": "STRING", "description": "starter or growth — only meaningful if ready_to_propose is true"},
        "owner_summary": {
            "type": "STRING",
            "description": "Summary for the business owner to review, only meaningful if ready_to_propose is true: what the customer wants, recommended tier, and why.",
        },
    },
    "required": ["reply_to_lead", "ready_to_propose", "recommended_tier", "owner_summary"],
}

# ---------------------------------------------------------------------------
# Post-lock conversation (awaiting_owner_approval / awaiting_payment stages).
#
# Before this existed, any lead message received once terms were locked got
# the exact same canned string back, verbatim, no matter what they'd said —
# "hello", "I want to talk more", and "I don't want it" all produced
# identical text. That's not a graceful holding pattern, it's the bot going
# deaf right at the moment the money conversation matters most.
#
# This lets Gemini read and respond to what the lead actually said, but
# under a hard constraint: it can talk about anything EXCEPT the locked
# terms themselves. It cannot restate a different price, promise a
# timeline, or imply the deal changed — those stay exactly where the human-
# approval design already put them: with the owner, not the bot.
#
# Cancellation-style intent is still caught by an explicit keyword check
# (same pattern as ESCALATION_PATTERNS above) rather than trusted to the
# model, because a wrong read there — treating "I don't want it" as
# small talk — is the failure this whole rewrite exists to fix.
# ---------------------------------------------------------------------------

CANCELLATION_PATTERNS = [
    "don't want it", "dont want it", "no longer want", "not interested anymore",
    "cancel", "changed my mind", "never mind", "nevermind",
    "don't want to proceed", "dont want to proceed", "want a refund",
    "stop", "not interested", "won't be paying", "wont be paying",
    "don't want to buy", "dont want to buy", "don't wish to buy",
    "dont wish to buy", "not going to buy", "lost interest",
    "reject", "rejected", "don't need it", "dont need it",
    "don't need this", "dont need this", "not buying",
    "won't be buying", "wont be buying", "no longer interested",
    "not doing this", "not going ahead", "back out", "backing out",
    "withdraw", "decline",
]


def detect_cancellation_request(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in CANCELLATION_PATTERNS)


# ---------------------------------------------------------------------------
# Reconsideration — the flip side of cancellation. A lead who said "I don't
# want it" and then comes back with "actually I do want it" shouldn't be
# stuck: the cancellation_flagged dedupe means the owner won't hear about it
# again unless something explicitly clears the flag. This lets the lead
# un-cancel themselves and tells the owner they came back, instead of the
# owner having to notice it by rereading the chat.
#
# Checked BEFORE the cancellation check, since phrases like "changed my
# mind, I'll pay" contain "changed my mind" (a cancellation pattern) but
# clearly mean the opposite — reconsideration wins that overlap.
# ---------------------------------------------------------------------------

RECONSIDERATION_PATTERNS = [
    "i do want it", "i want it after all", "changed my mind, i'll",
    "changed my mind i'll", "changed my mind, i will", "actually i want",
    "actually, i want", "actually i do want", "i'll buy", "i will buy",
    "i'm in", "im in", "let's do it", "lets do it", "i want to buy",
    "i want to proceed", "never mind, i want", "never mind i want",
    "ok i'll pay", "okay i'll pay", "i'll pay", "i will pay",
    "reconsidered", "on second thought", "back in", "i'm back",
    "im back", "let's proceed", "lets proceed", "go ahead with it",
    "still want it", "still interested",
]


def detect_reconsideration(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in RECONSIDERATION_PATTERNS)


# ---------------------------------------------------------------------------
# Resend requests — a lead asking to have the order summary/payment link
# sent again. This used to fall through to Gemini, which (per the system
# prompt's own wording) would SAY "I've let the owner know" without any
# code actually doing that — no ping ever sent, nothing ever resent, same
# canned sentence back every time no matter how the lead re-phrased it.
#
# Resending isn't a business/pricing decision — it's just re-delivering
# something already approved and already sent once. So this handles it
# directly: look up the already-locked order by pending_order_id and
# resend the exact same contract message, no new order, no new amount,
# nothing for the owner to approve. If a phone message like a screenshot
# claims something was sent but pending_order_id is missing (shouldn't
# happen if the lead reached this stage the normal way), this pings the
# owner instead of pretending to send something that doesn't exist.
# ---------------------------------------------------------------------------

RESEND_PATTERNS = [
    "resend", "re-send", "resent", "re-sent", "send it again",
    "send again", "send the link again", "share the link again",
    "didn't get", "didnt get", "i missed", "can't find", "cant find",
    "send me the link", "share the link", "give me the link",
]


def detect_resend_request(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in RESEND_PATTERNS)


POST_LOCK_SYSTEM_PROMPT = """You are a WhatsApp sales assistant for a solo \
AI automation agency in India. This lead already has a locked order: exact \
price, scope, and terms were already sent to them as a formal contract \
message, and (depending on stage) is either awaiting the business owner's \
final approval or awaiting payment. Those terms are FINAL and were set by \
a human, not you.

Your job here is narrow: have a normal, warm, human-sounding reply to \
whatever they just said, WITHOUT touching the deal itself. Specifically:

- NEVER restate, imply, or negotiate a price — not even to repeat the \
correct one. If they ask about price/scope/timeline specifics, say that's \
already covered in the order summary/contract already sent.
- Resend requests are already handled separately before you ever see this \
message, so you will not normally get one — but if something ambiguous \
still reads that way, don't promise to resend or notify anyone yourself; \
just say the order summary is above and they're welcome to ask again if \
they can't find it.
- NEVER say or imply the deal, price, or status has changed in any way.
- NEVER promise a delivery date or say work has started.
- It's fine to answer general questions (how the service works, what \
happens after payment in general terms, small talk) normally and briefly.
- If anything in their message reads like they might want to cancel, \
back out, or are unhappy with the deal, don't try to talk them out of it \
or handle it yourself — just acknowledge it warmly and say you're \
flagging it for the owner to reach out personally. (A separate system \
also escalates this automatically — you don't need to do anything else \
about it.)

Be concise — this is WhatsApp, not email."""

POST_LOCK_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply_to_lead": {
            "type": "STRING",
            "description": "Natural reply to send back to the lead now. Must never restate or imply changed price/scope/terms.",
        },
    },
    "required": ["reply_to_lead"],
}


def handle_post_lock_message(phone: str, text: str, stage: str) -> None:
    """Replaces the old static holding_replies lookup. Still never touches
    locked terms, but now actually reads what the lead said instead of
    echoing an identical string regardless of content."""
    convo = get_or_create_conversation(phone)

    if detect_reconsideration(text) and convo.get("cancellation_flagged", False):
        send_whatsapp(
            OWNER_PHONE,
            f"↩️ Lead {phone} previously flagged as possibly cancelling "
            f"(stage: {stage}) now sounds like they've reconsidered:\n"
            f"\"{text}\"\n\nOrder was never actually cancelled in the system "
            f"— the link/terms already sent are still valid — but worth a "
            f"quick look before assuming it's back on.",
        )
        set_stage(phone, stage, cancellation_flagged=False)
        reply = (
            "Great to hear! Nothing's changed on our end — the order "
            "summary and payment link already sent are still valid, so "
            "you're all set to go ahead whenever you're ready."
        )
        send_whatsapp(phone, reply)
        append_history(phone, "agent", reply)
        return

    if detect_cancellation_request(text):
        if not convo.get("cancellation_flagged", False):
            send_whatsapp(
                OWNER_PHONE,
                f"⚠️ Lead {phone} may want to cancel/back out (stage: {stage}). "
                f"Their message:\n\"{text}\"\n\nWorth reaching out personally — "
                f"the bot only sent them a brief acknowledgement, nothing else.",
            )
            set_stage(phone, stage, cancellation_flagged=True)
        # else: owner already told once for this lead — don't re-ping on
        # every follow-up cancellation-flavored message, just keep
        # acknowledging the lead below.
        reply = (
            "Totally understand — I've flagged this for the owner to reach "
            "out to you directly about it."
        )
        send_whatsapp(phone, reply)
        append_history(phone, "agent", reply)
        return

    if detect_resend_request(text):
        pending_order_id = convo.get("pending_order_id")
        order = order_contract.get_order(pending_order_id) if pending_order_id else None
        if order is None:
            # Shouldn't normally happen at this stage, but never claim to
            # have resent something that doesn't exist — flag it for a
            # human to sort out instead of fabricating a confirmation.
            send_whatsapp(
                OWNER_PHONE,
                f"⚠️ Lead {phone} asked to resend their order link, but no "
                f"order is on record for them (pending_order_id={pending_order_id!r}). "
                f"Needs a manual look.",
            )
            reply = (
                "I'm having trouble pulling up your order right now — "
                "I've flagged this for the owner to sort out and get back "
                "to you."
            )
            send_whatsapp(phone, reply)
            append_history(phone, "agent", reply)
        else:
            contract_msg = order_contract.build_contract_message(order)
            _send_contract_and_qr(phone, order, contract_msg)
            append_history(phone, "agent", "[resent order summary + payment link + QR]")
        return

    convo = get_or_create_conversation(phone)
    history_text = "\n".join(f"{h['role']}: {h['text']}" for h in convo["history"])
    user_prompt = (
        f"Conversation so far (current stage: {stage}):\n{history_text}\n"
    )
    try:
        client = get_gemini_client()
        result = client.generate_json(POST_LOCK_SYSTEM_PROMPT, user_prompt, POST_LOCK_RESPONSE_SCHEMA)
        reply = result.get("reply_to_lead", "")
    except Exception as e:
        print(f"[warn] Post-lock Gemini call failed ({e}); falling back to a generic holding reply.", file=sys.stderr)
        reply = ""

    if not reply:
        # Fallback only if Gemini is unreachable/misconfigured — not the
        # default path anymore, just a safety net so a lead is never met
        # with total silence.
        reply = (
            "Thanks for the message — your order details are above, and "
            "we'll follow up if anything needs your input."
        )
    send_whatsapp(phone, reply)
    append_history(phone, "agent", reply)


def load_tiers() -> dict:
    with open("tiers_agency.json", encoding="utf-8") as f:
        return json.load(f)


def build_catalog_message(tiers: dict) -> str:
    """Fixed-price catalog shown to the lead after warm-up chat. All tiers
    carry a fixed setup_price_fixed value. Not every tier has a recurring
    monthly_price (e.g. Website is one-time only) — that's fine, it's
    simply omitted from the price line when absent/empty."""
    lines = ["Here's what we offer — pick the one that fits:\n"]
    for i, (key, tier) in enumerate(tiers.items(), start=1):
        fixed = tier.get("setup_price_fixed")
        price_line = f"₹{fixed:,} one-time setup" if fixed is not None else tier.get("setup_price", "Quote-based")
        monthly_line = tier.get("monthly_price", "")
        if monthly_line:
            price_line = f"{price_line} + {monthly_line}"
        lines.append(f"{i}. *{tier['label']}* — {price_line}\n   {tier['description']}\n")
    lines.append('Reply with the number or name of the one you\'d like (e.g. "2" or "Growth").')
    return "\n".join(lines)


# Maps a lead's free-text catalog reply to a tier key. Explicit, code-level
# matching — deliberately NOT left to Gemini, since a wrong tier match here
# would misquote a price to the customer.
def match_selected_tier(text: str, tiers: dict) -> Optional[str]:
    lowered = text.strip().lower()
    tier_keys = list(tiers.keys())  # preserves JSON order: starter, growth
    # Numeric pick ("1", "2.", "option 3")
    for i, key in enumerate(tier_keys, start=1):
        if lowered == str(i) or lowered.startswith(f"{i}.") or lowered.startswith(f"{i})"):
            return key
    # Name pick (tier key or label appearing in the message)
    for key in tier_keys:
        if key in lowered or tiers[key]["label"].lower() in lowered:
            return key
    return None


def handle_lead_message(phone: str, text: str) -> None:
    append_history(phone, "lead", text)
    convo = get_or_create_conversation(phone)
    stage = convo["stage"]
    tiers = load_tiers()

    if stage == "warming_up":
        history_text = "\n".join(f"{h['role']}: {h['text']}" for h in convo["history"])
        user_prompt = f"Conversation so far with this lead:\n{history_text}\n"
        client = get_gemini_client()
        result = client.generate_json(CHAT_SYSTEM_PROMPT, user_prompt, WARMUP_RESPONSE_SCHEMA)

        if result.get("ready_for_catalog"):
            catalog_msg = build_catalog_message(tiers)
            send_whatsapp(phone, catalog_msg)
            append_history(phone, "agent", catalog_msg)
            set_stage(phone, "catalog_shown")
        else:
            reply = result.get("reply_to_lead", "")
            if reply:
                send_whatsapp(phone, reply)
                append_history(phone, "agent", reply)

    elif stage == "catalog_shown":
        picked = match_selected_tier(text, tiers)
        if picked is None:
            tier_labels = [t["label"] for t in tiers.values()]
            number_range = f"1-{len(tier_labels)}" if len(tier_labels) > 1 else "1"
            reply = (f"Didn't catch which one — reply with the number "
                      f"({number_range}) or the plan name ({', '.join(tier_labels)}).")
            send_whatsapp(phone, reply)
            append_history(phone, "agent", reply)
            return
        tier_info = tiers[picked]
        fixed_price = tier_info.get("setup_price_fixed")
        confirm_msg = (
            f"Great — *{tier_info['label']}* it is "
            f"({f'₹{fixed_price:,} setup' if fixed_price is not None else tier_info.get('setup_price', 'quote-based')}). "
            f"Let's nail down a couple of details about your business so we can finalize scope."
        )
        send_whatsapp(phone, confirm_msg)
        append_history(phone, "agent", confirm_msg)
        set_stage(phone, "package_selected", selected_tier=picked, fixed_price=fixed_price)

    elif stage == "package_selected":
        selected_key = convo.get("selected_tier")
        tier_info = tiers.get(selected_key, {})
        history_text = "\n".join(f"{h['role']}: {h['text']}" for h in convo["history"])
        user_prompt = (
            f"The lead has already picked the *{tier_info.get('label', selected_key)}* "
            f"package. Conversation so far:\n{history_text}\n"
        )
        client = get_gemini_client()
        result = client.generate_json(SCOPE_SYSTEM_PROMPT, user_prompt, CHAT_RESPONSE_SCHEMA)

        if result.get("ready_to_propose"):
            owner_summary = result.get("owner_summary", "")
            fixed_price = convo.get("fixed_price")
            price_line = (f"Fixed price already selected: ₹{fixed_price:,}"
                           if fixed_price is not None
                           else "No fixed price on record — needs your quote.")
            send_whatsapp(
                OWNER_PHONE,
                f"📋 Ready to propose — lead {phone}\n\n"
                f"Package: {tier_info.get('label', selected_key)}\n"
                f"{price_line}\n\n"
                f"Summary: {owner_summary}\n\n"
                + (f"Reply APPROVE {phone} to send the contract + payment link now."
                   if fixed_price is not None
                   else f"Reply SETPRICE {phone} <amount> to set the quote and send the contract.")
            )
            set_stage(phone, "awaiting_owner_approval",
                      recommended_tier=selected_key, owner_summary=owner_summary)
        else:
            # Loop-limit / stalled-conversation guard: if the lead has sent
            # several messages in this stage without Gemini ever deciding
            # ready_to_propose, the conversation is likely stuck (going in
            # circles, or off-topic) rather than genuinely still gathering
            # info. Cap it and hand off to the owner rather than replying
            # forever with no end in sight.
            lead_turns = sum(1 for h in convo["history"] if h["role"] == "lead")
            if lead_turns >= STALLED_CONVERSATION_TURN_LIMIT and not convo.get("stall_flagged"):
                send_whatsapp(
                    OWNER_PHONE,
                    f"⚠️ Lead {phone} has sent {lead_turns} messages in the "
                    f"*{tier_info.get('label', selected_key)}* stage without reaching "
                    f"a proposal — looks stuck. Worth checking in manually. "
                    f"(The bot will keep replying to them normally in the meantime.)"
                )
                set_stage(phone, stage, stall_flagged=True)

            reply = result.get("reply_to_lead", "")
            if reply:
                send_whatsapp(phone, reply)
                append_history(phone, "agent", reply)

    elif stage == "closed":
        # Order already complete — nothing left to negotiate or flag, a
        # short fixed reply is genuinely appropriate here (unlike the
        # awaiting_* stages below, there's no live deal for a cancellation
        # check or Gemini reply to be "about").
        reply = (
            "This order is already complete on our end. If you need "
            "something else, just let us know!"
        )
        send_whatsapp(phone, reply)
        append_history(phone, "agent", reply)

    else:
        # awaiting_owner_approval / awaiting_payment — terms are frozen
        # (never auto-send unapproved/changed terms), but the lead still
        # gets a real, content-aware reply — see handle_post_lock_message.
        handle_post_lock_message(phone, text, stage)
        print(f"[info] Message from {phone} received while stage={stage} — sent post-lock reply, needs manual handling if action required.")


# ---------------------------------------------------------------------------
# Owner message logic — approvals only, nothing else auto-handled
# ---------------------------------------------------------------------------

def _send_contract_and_qr(lead_phone: str, order: dict, contract_msg: str) -> None:
    """Sends the text contract (with the upi:// link, which works fine on
    many phones) immediately, then follows with a QR-code image encoding
    the identical payment details — so a phone that can't open the deep
    link still has a working way to pay. QR generation/sending failure
    (e.g. qrcode not installed) is logged but never blocks the text
    message, which already has everything needed to pay manually."""
    send_whatsapp(lead_phone, contract_msg)
    try:
        qr_png = order_contract.build_upi_qr_png(order)
        send_whatsapp_image(
            lead_phone, qr_png,
            caption="If the payment link above doesn't open, scan this QR code with any UPI app instead.",
        )
    except Exception as e:
        print(f"[warn] Could not send payment QR to {lead_phone}: {e}", file=sys.stderr)


def _build_pay_page_url(order_id: str) -> Optional[str]:
    """Link to this same service's /pay/<order_id> fallback page — a plain
    web page (no WhatsApp/UPI-app handoff required) showing the QR, the
    upi:// link, and the raw UPI ID, for cases where the deep link fails to
    open and/or the customer isn't on a phone at all (desktop, QR rendered
    poorly in-chat, etc). Returns None if RENDER_APP_URL isn't set, so
    callers can skip the line entirely rather than send a broken link."""
    if not RENDER_APP_URL:
        return None
    return f"{RENDER_APP_URL}/pay/{order_id}"


def _create_and_send_contract(lead_phone: str, convo: dict, tier_info: dict,
                               tier_key: str, amount: int) -> None:
    """Shared by both approval paths below: lock the order, send the
    contract + payment link to the customer immediately, notify the owner
    it went out. No further confirmation step after this point — this is
    the final, automatic action once a price is known and approval given."""
    order = order_contract.create_order(
        customer_name=lead_phone,  # name not reliably captured yet — phone as identifier
        customer_phone=lead_phone,
        tier_name=tier_info.get("label", tier_key),
        scope_summary=convo.get("owner_summary", "See conversation history."),
        amount_rupees=amount,
        upi_id=OWNER_UPI_ID,
    )
    contract_msg = order_contract.build_contract_message(order)
    pay_page_url = _build_pay_page_url(order["order_id"])
    if pay_page_url:
        # Third fallback option, not a replacement for the upi:// link or
        # the QR image — some phones fail the app handoff from inside
        # WhatsApp/email but succeed from a real browser tab, and this is
        # the only option at all for anyone on desktop.
        contract_msg += (
            f"\n\nHaving trouble with the link or QR above? Open this page "
            f"instead: {pay_page_url}"
        )
    _send_contract_and_qr(lead_phone, order, contract_msg)
    set_stage(lead_phone, "awaiting_payment", pending_order_id=order["order_id"])
    send_whatsapp(
        OWNER_PHONE,
        f"Sent. Order {order['order_id']} — awaiting payment of ₹{order['amount_rupees']:,}.\n"
        f"Once you see the payment land in your own UPI app, confirm it with:\n"
        f"PAID {order['order_id']} {order['amount_rupees']}"
    )


def handle_owner_message(text: str) -> None:
    """
    Recognizes three command shapes:
      - "APPROVE <lead_phone>" — the normal path. Used when the lead
        already picked a fixed-price tier (Starter/Growth) at the catalog
        stage, so the exact amount is already known (convo["fixed_price"]).
        On approve, the contract + payment link are sent to the customer
        IMMEDIATELY, automatically — no further confirmation step. This is
        deliberate: fixing prices in the catalog removed the need for a
        separate price-locking round-trip, since there's no range left for
        a human to pick a number from.
      - "SETPRICE <lead_phone> <amount>" — fallback path for the rare
        case where a lead's selected tier has no fixed price on record
        (e.g. a malformed/edited tiers file). Not expected in normal
        operation since every catalog tier now carries a fixed price.
        Sends immediately once the owner names a number.
      - "PAID <order_id> <amount>" — confirms a payment. This is the ONLY
        way an order's status ever moves off "awaiting_payment": personal
        UPI has no API this app can poll, so there is no automatic way to
        detect that money landed — you check your own UPI app/bank
        notification, then tell the bot the amount with this command. It
        calls order_contract.check_payment(), which does an exact-match
        check (never "any amount > 0") and returns one of three outcomes
        (paid / underpaid / overpaid) — see that function's docstring for
        why underpayment (e.g. a ₹1 test) is never treated as paid.
    Anything else from the owner is logged but not acted on — this
    deliberately does NOT try to guess intent from free-form text, since a
    wrong guess here could send a customer an unapproved offer or falsely
    mark an order paid.
    """
    parts = text.strip().split()

    if len(parts) == 2 and parts[0].upper() == "APPROVE":
        lead_phone = parts[1]
        conversations = _load_conversations()
        convo = conversations.get(lead_phone)
        if convo is None or convo.get("stage") != "awaiting_owner_approval":
            send_whatsapp(OWNER_PHONE, f"No pending approval found for {lead_phone}.")
            return

        tiers = load_tiers()
        tier_key = convo.get("recommended_tier") or next(iter(tiers))
        tier_info = tiers.get(tier_key, {})
        fixed_price = convo.get("fixed_price")

        if fixed_price is None:
            # No fixed price on record — can't auto-charge without a
            # number. Ask for it via SETPRICE instead of guessing.
            send_whatsapp(
                OWNER_PHONE,
                f"{tier_info.get('label', tier_key)} has no fixed price on record for "
                f"{lead_phone}. Reply with the exact amount to charge, e.g.:\n"
                f"SETPRICE {lead_phone} 12000"
            )
            return

        _create_and_send_contract(lead_phone, convo, tier_info, tier_key, fixed_price)
        return

    if len(parts) == 3 and parts[0].upper() == "SETPRICE":
        lead_phone, amount_str = parts[1], parts[2]
        try:
            amount = int(amount_str)
        except ValueError:
            send_whatsapp(OWNER_PHONE, f"Couldn't read '{amount_str}' as a whole-rupee amount. Try again, e.g. SETPRICE {lead_phone} 7000")
            return

        conversations = _load_conversations()
        convo = conversations.get(lead_phone)
        if convo is None or convo.get("stage") != "awaiting_owner_approval":
            send_whatsapp(OWNER_PHONE, f"No pending approval found for {lead_phone}.")
            return

        tiers = load_tiers()
        tier_key = convo.get("recommended_tier") or next(iter(tiers))
        tier_info = tiers.get(tier_key, {})

        _create_and_send_contract(lead_phone, convo, tier_info, tier_key, amount)
        return

    if len(parts) == 3 and parts[0].upper() == "PAID":
        order_id, amount_str = parts[1], parts[2]
        try:
            received_rupees = float(amount_str)
        except ValueError:
            send_whatsapp(OWNER_PHONE, f"Couldn't read '{amount_str}' as an amount. Try again, e.g. PAID {order_id} 7000")
            return

        result = order_contract.check_payment(order_id, received_rupees)

        # Always tell the owner the outcome — paid, underpaid, overpaid, or
        # unknown order. This is the one message that matters most: it's
        # the only confirmation the owner gets that the exact-match check
        # ran and what it decided.
        send_whatsapp(OWNER_PHONE, f"💰 {result.message_for_user}")

        # For underpayment only, check_payment() may also have prepared a
        # one-time nudge to send the customer directly (see its docstring
        # for the nudged_once guard against spamming the same customer on
        # every partial-payment notification). Look up the order to find
        # who to send it to and on which channel.
        if result.message_for_customer:
            order = order_contract.get_order(order_id)
            if order and order.get("customer_phone"):
                send_whatsapp(order["customer_phone"], result.message_for_customer)
            else:
                print(f"[warning] PAID {order_id}: had a customer nudge to send but no customer_phone on the order record.", file=sys.stderr)
        return

    print(f"[info] Owner sent a message not recognized as a command: {text!r} — no action taken.")


# ---------------------------------------------------------------------------
# /pay/<order_id> — a plain web fallback for when the upi://pay?... deep
# link fails to open inside WhatsApp's/email's in-app browsers (a known,
# common mobile issue), or the customer is on desktop, or the QR image
# rendered poorly in the chat client. Shows the exact same order/QR/UPI-ID
# data _send_contract_and_qr() already sent — this is a third way to reach
# it, not a new source of truth. No new domain, no templates/ folder: this
# lives on the same Render service and renders one inline HTML string.
# ---------------------------------------------------------------------------

_PAY_PAGE_NOT_FOUND_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Order not found</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
         background: #f5f5f5; margin: 0; padding: 40px 20px; text-align: center; }}
  .card {{ max-width: 420px; margin: 0 auto; background: #fff; border-radius: 12px;
          padding: 32px 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
  h1 {{ font-size: 18px; color: #222; margin: 0 0 8px; }}
  p {{ color: #666; font-size: 14px; line-height: 1.5; }}
</style></head>
<body><div class="card">
  <h1>Order not found</h1>
  <p>We couldn't find an order matching <code>{order_id}</code>. Double-check the
  link, or reply to your conversation and ask for it to be resent.</p>
</div></body></html>"""

_PAY_PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pay order {order_id}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
         background: #f5f5f5; margin: 0; padding: 32px 16px; }}
  .card {{ max-width: 420px; margin: 0 auto; background: #fff; border-radius: 12px;
          padding: 28px 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
  h1 {{ font-size: 18px; color: #222; margin: 0 0 4px; }}
  .sub {{ color: #888; font-size: 13px; margin: 0 0 20px; }}
  .row {{ display: flex; justify-content: space-between; padding: 8px 0;
         border-bottom: 1px solid #eee; font-size: 14px; }}
  .row:last-of-type {{ border-bottom: none; }}
  .row span:first-child {{ color: #666; }}
  .row span:last-child {{ color: #222; font-weight: 600; text-align: right; }}
  .qr-wrap {{ text-align: center; margin: 24px 0; }}
  .qr-wrap img {{ width: 220px; height: 220px; border: 1px solid #eee; border-radius: 8px; }}
  .qr-caption {{ color: #888; font-size: 12px; margin-top: 8px; }}
  .pay-btn {{ display: block; text-align: center; background: #128C7E; color: #fff;
             text-decoration: none; font-weight: 600; font-size: 15px;
             padding: 14px; border-radius: 8px; margin: 20px 0 8px; }}
  .upi-id {{ text-align: center; color: #666; font-size: 13px; margin: 4px 0 0; }}
  .upi-id code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 4px; }}
  .note {{ color: #999; font-size: 12px; line-height: 1.5; margin-top: 24px;
          padding-top: 16px; border-top: 1px solid #eee; }}
</style></head>
<body><div class="card">
  <h1>Order {order_id}</h1>
  <p class="sub">{tier_name}</p>

  <div class="row"><span>Scope</span><span>{scope_summary}</span></div>
  <div class="row"><span>Amount due</span><span>&#8377;{amount:,}</span></div>

  {qr_block}

  <a class="pay-btn" href="{upi_link}">Pay Now via UPI app</a>
  <p class="upi-id">Or enter manually — UPI ID: <code>{upi_id}</code></p>

  <p class="note">Please keep the order ID ({order_id}) in the payment note if
  your UPI app allows it — this helps confirm your payment quickly. This is a
  personal UPI ID, not a registered business account, so your UPI app may not
  show a verified merchant badge — that's expected.</p>
</div></body></html>"""


@app.route("/pay/<order_id>", methods=["GET"])
def pay_page(order_id: str):
    order = order_contract.get_order(order_id)
    if order is None:
        return _PAY_PAGE_NOT_FOUND_HTML.format(order_id=order_id), 404

    upi_link = order_contract.build_upi_link(order)

    qr_block = ""
    try:
        import base64
        qr_png = order_contract.build_upi_qr_png(order)
        qr_b64 = base64.b64encode(qr_png).decode("ascii")
        qr_block = (
            '<div class="qr-wrap">'
            f'<img src="data:image/png;base64,{qr_b64}" alt="UPI payment QR code">'
            '<p class="qr-caption">Scan with any UPI app</p>'
            '</div>'
        )
    except Exception as e:
        # Same non-fatal handling as _send_contract_and_qr — the Pay Now
        # button and raw UPI ID below still work without the QR image.
        print(f"[warn] Could not render QR for /pay/{order_id}: {e}", file=sys.stderr)

    html = _PAY_PAGE_HTML.format(
        order_id=order["order_id"],
        tier_name=order.get("tier_name", ""),
        scope_summary=order.get("scope_summary", ""),
        amount=order["amount_rupees"],
        qr_block=qr_block,
        upi_link=upi_link,
        upi_id=order["upi_id"],
    )
    return html, 200


# ---------------------------------------------------------------------------
# Webhook endpoint — same shape as webhook_receiver.py, extended with routing
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}

    print("\n" + "=" * 60)
    print(f"[{datetime.now(timezone.utc).isoformat()}] Incoming webhook:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("=" * 60)

    # Same safety net as before: log everything raw, unconditionally,
    # regardless of whether the parsing below understands this event.
    append_to_log({
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": payload,
    })

    # WAHA's shape differs from Whapi's: one event per webhook POST, under
    # payload.event == "message" (not a "messages" array), with the phone
    # in payload.from as "<digits>@c.us" and the text directly on
    # payload.body (not nested under text.body). We normalize both here so
    # the rest of this function keeps working with plain digit strings,
    # matching what db_storage/order_contract/send_whatsapp all expect.
    event = payload.get("event")
    msg_payload = payload.get("payload", {}) if event else {}
    messages = [msg_payload] if event == "message" else []

    for msg in messages:
        raw_from = msg.get("from", "unknown")
        from_number = raw_from.split("@")[0] if raw_from else "unknown"
        if msg.get("fromMe") and from_number != OWNER_PHONE:
            # Skip echoes of the bot's own outgoing sends. Note: when the
            # owner's own WhatsApp channel IS the OWNER_PHONE number, WAHA
            # (like Whapi before it) tags the owner's real replies as
            # fromMe: true too (multi-device echo), with "from" correctly
            # set to OWNER_PHONE. So fromMe alone is NOT a safe filter — it
            # was silently swallowing real APPROVE/SETPRICE replies.
            # Only skip when fromMe AND it's not the owner's number.
            continue
        text = msg.get("body", "")
        if not text:
            continue  # non-text event (reaction, etc) — nothing to act on yet

        print(f"\n>>> REAL REPLY from {from_number}: {text}\n")

        if from_number == OWNER_PHONE:
            handle_owner_message(text)
        else:
            # Escalation requests ("talk to owner", "call me", etc.) still
            # get routed specially, but stage-aware: once terms are locked
            # (awaiting_owner_approval / awaiting_payment), handing out the
            # owner's personal number is exactly the kind of unscoped
            # decision that stage is meant to keep away from the bot — the
            # lead should get a normal post-lock reply instead, which
            # itself still pings the owner (once) that a human is wanted,
            # without leaking a phone number mid-negotiation.
            convo = get_or_create_conversation(from_number)
            stage = convo.get("stage", "warming_up")
            if detect_escalation_request(text) and stage in ("awaiting_owner_approval", "awaiting_payment"):
                append_history(from_number, "lead", text)
                handle_post_lock_message(from_number, text, stage)
            elif detect_escalation_request(text):
                append_history(from_number, "lead", text)
                handle_escalation(from_number, text)
            else:
                handle_lead_message(from_number, text)

    return jsonify({"status": "received"}), 200


def run_email_poll_once() -> dict:
    """Checks the Gmail inbox once for new replies and feeds each one into
    the same handle_lead_message() the WhatsApp webhook uses — a lead who
    replies over email gets the identical stage machine, Gemini prompts,
    catalog, escalation detection, and owner-approval flow as a WhatsApp
    lead.

    This is the actual polling logic, extracted out of the old /poll-email
    route so it can be called two ways: (1) from the background thread
    below, on a timer, so replies go out within a couple minutes without
    anyone triggering anything by hand, and (2) still from /poll-email
    itself, kept as a manual/debug trigger — hitting it by hand (or from
    GitHub Actions) still works exactly as before, it just isn't the
    primary way replies get sent anymore.
    """
    replies = email_sender.fetch_new_replies(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    if replies:
        print(f"[info] email poll: {len(replies)} new email reply(ies) found.")

    processed = 0
    errors = 0
    for sender_addr, domain, body in replies:
        try:
            append_to_log({
                "received_at": datetime.now(timezone.utc).isoformat(),
                "raw_payload": {"channel": "email", "from": sender_addr, "domain": domain, "body": body},
            })

            # If OWNER_EMAIL replied, this is the owner sending APPROVE/SETPRICE
            # by email (see OWNER_NOTIFY_CHANNEL) — route to the owner-command
            # handler, not the lead-chat engine. Case-insensitive since email
            # addresses are compared case-insensitively by convention.
            if OWNER_EMAIL and sender_addr.strip().lower() == OWNER_EMAIL.strip().lower():
                handle_owner_message(body)
                processed += 1
                continue

            convo_id = f"email:{domain}"
            # Matches a reply back to its lead by DOMAIN, not the exact sender
            # address — same limitation as the original send: this pipeline
            # never captured a real per-lead email, only a guessed info@/
            # contact@ address, so "someone at this domain replied" is the
            # best available match. If two different people at the same
            # domain email in, they share one conversation — an accepted
            # tradeoff given the alternative is no email channel at all.
            get_or_create_conversation(convo_id, email_address=sender_addr)

            convo = get_or_create_conversation(convo_id)
            stage = convo.get("stage", "warming_up")
            if detect_escalation_request(body) and stage in ("awaiting_owner_approval", "awaiting_payment"):
                append_history(convo_id, "lead", body)
                handle_post_lock_message(convo_id, body, stage)
            elif detect_escalation_request(body):
                append_history(convo_id, "lead", body)
                handle_escalation(convo_id, body)
            else:
                handle_lead_message(convo_id, body)
            processed += 1
        except Exception as e:
            # One bad reply (Gemini hiccup, malformed body, etc.) used to
            # crash the whole batch as an unhandled 500 with no info in
            # the caller's log. Now it's logged with a full traceback in
            # Render's own logs and the loop moves on to the next reply.
            errors += 1
            print(f"[error] email poll: failed processing reply from {sender_addr!r}: {e}", file=sys.stderr)
            traceback.print_exc()

    return {"replies_found": len(replies), "processed": processed, "errors": errors}


# ---------------------------------------------------------------------------
# Background email poll loop — replaces relying on an external cron hitting
# /poll-email as the ONLY thing that checks Gmail. That meant replies could
# sit for up to 3 hours (the old GitHub Actions schedule) unless someone
# manually triggered the workflow. This runs inside the same process as the
# Flask app and calls run_email_poll_once() on its own timer, so a reply
# gets picked up within EMAIL_POLL_INTERVAL_SECONDS of arriving — no
# external trigger needed at all.
#
# This only helps while the Render process is actually awake. Render's free
# tier spins a service down after ~15 min with no inbound HTTP traffic, and
# a spun-down process runs no background threads, including this one. The
# GitHub Actions workflow (poll-email.yml) has been repurposed to ping "/"
# every few minutes — cheap HTTP traffic whose only job is keeping this
# process alive so this loop keeps running. If you move to a paid Render
# plan (no spin-down), that keep-alive ping becomes unnecessary, but it's
# harmless to leave in either way.
# ---------------------------------------------------------------------------
EMAIL_POLL_INTERVAL_SECONDS = int(os.environ.get("EMAIL_POLL_INTERVAL_SECONDS", "120"))
_email_poll_thread_started = False
_email_poll_thread_lock = threading.Lock()


def _email_poll_loop() -> None:
    print(f"[info] Background email poll loop started — checking every {EMAIL_POLL_INTERVAL_SECONDS}s.")
    while True:
        try:
            run_email_poll_once()
        except Exception as e:
            # Never let one bad poll cycle kill the loop — log and try
            # again next interval rather than silently going dark forever.
            print(f"[error] Background email poll loop: unexpected error: {e}", file=sys.stderr)
            traceback.print_exc()
        time.sleep(EMAIL_POLL_INTERVAL_SECONDS)


def start_email_poll_loop_once() -> None:
    """Starts the background poll thread exactly once per process, even if
    called from multiple places (e.g. both _startup_checks() and a WSGI
    server that imports the module more than once). Guarded by a lock since
    gunicorn can technically import the module from more than one thread
    during worker boot."""
    global _email_poll_thread_started
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("[warning] GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — background email poll loop not started.")
        return
    with _email_poll_thread_lock:
        if _email_poll_thread_started:
            return
        thread = threading.Thread(target=_email_poll_loop, daemon=True)
        thread.start()
        _email_poll_thread_started = True


@app.route("/poll-email", methods=["POST"])
def poll_email():
    """Manual/debug trigger — checks Gmail once, right now, on demand.
    Kept for testing and as a fallback, but this is no longer the only
    thing that checks Gmail: the background loop above (started at app
    boot) already does this automatically every EMAIL_POLL_INTERVAL_SECONDS.

    Protected by a shared secret (POLL_EMAIL_SECRET) in the X-Poll-Secret
    header, since this route triggers real outbound Gemini calls and
    replies — it shouldn't be callable by anyone who finds the URL.
    """
    if POLL_EMAIL_SECRET:
        if request.headers.get("X-Poll-Secret", "") != POLL_EMAIL_SECRET:
            return jsonify({"status": "error", "detail": "bad or missing X-Poll-Secret"}), 401
    else:
        print("[warning] POLL_EMAIL_SECRET not set — /poll-email is callable by anyone with the URL.")

    result = run_email_poll_once()
    print(f"[info] /poll-email (manual): {result['replies_found']} new email reply(ies) found.")
    return jsonify({"status": "ok", **result}), 200


@app.route("/create-test-order", methods=["GET"])
def create_test_order_route():
    """Free-tier-friendly stand-in for running create_test_order.py at a
    shell prompt — Render's Shell tab and One-Off Jobs are both PAID-ONLY
    features, unavailable on the free plan this service runs on, so
    there's no terminal to run that script from directly. This route does
    the same thing (order_contract.create_order() with a fake customer
    identifier, never a real lead) but is triggered by a plain browser
    visit/GET request instead.

    Query params (all optional):
      ?amount=100        rupees to lock in (default 100 — small on
                          purpose, cheap to actually pay if testing a
                          real transfer)
      ?secret=...         required if POLL_EMAIL_SECRET is set (reuses
                          that same secret rather than introducing a
                          second one to configure) — pass it in the URL
                          as a query param since this is meant to be
                          opened directly in a browser, where a custom
                          header isn't practical.

    Returns the order_id and the exact PAID command to send — same
    output as the standalone script, just delivered as a webpage instead
    of shell/stdout. This is NOT a real order and is never linked to any
    real WhatsApp/email conversation — sending PAID for it never
    messages a real customer.
    """
    if POLL_EMAIL_SECRET:
        if request.args.get("secret", "") != POLL_EMAIL_SECRET:
            return jsonify({"status": "error", "detail": "bad or missing ?secret="}), 401
    else:
        print("[warning] POLL_EMAIL_SECRET not set — /create-test-order is callable by anyone with the URL.")

    try:
        amount = int(request.args.get("amount", "100"))
    except ValueError:
        return jsonify({"status": "error", "detail": "?amount= must be a whole number"}), 400

    order = order_contract.create_order(
        customer_name="TEST ORDER (safe to ignore)",
        customer_phone="test:dummy-order",
        tier_name="TEST",
        scope_summary="Dummy order created via /create-test-order — not a real customer.",
        amount_rupees=amount,
        upi_id=OWNER_UPI_ID,
    )
    paid_command = f"PAID {order['order_id']} {amount}"
    return jsonify({
        "status": "ok",
        "order_id": order["order_id"],
        "amount_rupees": amount,
        "send_this_to_the_bot_as_owner": paid_command,
        "note": "This is a fake test order — sending the PAID command above will never message a real customer.",
    }), 200


@app.route("/", methods=["GET"])
def health():
    # Doubles as the keep-alive target for poll-email.yml on Render's free
    # tier — see the background email poll loop comment above for why this
    # route being hit regularly matters now, not just as a status check.
    return "Chat agent is running.", 200


# Starts the background email poll loop at import time, unconditionally —
# this runs both when gunicorn imports chat_agent:app (the actual Render
# production path) and when this file is run directly below. Deliberately
# placed here at module bottom (rather than inside _startup_checks(), which
# runs near the top of the file) because start_email_poll_loop_once() is
# defined further up but after _startup_checks() already executes — putting
# the call here, after every name in the module is defined, avoids a
# NameError while still guaranteeing it runs exactly once per process
# regardless of which path (gunicorn or direct run) started it.
start_email_poll_loop_once()

if __name__ == "__main__":
    # All startup checks + init_db() already ran above at import time
    # (_startup_checks()), so this block only needs to start the local
    # dev server — gunicorn (Render) never reaches this branch at all,
    # it just imports chat_agent:app directly.
    # host/port: 0.0.0.0 and the PORT env var (Render sets this automatically;
    # defaults to 5000 for local/dev use, matching the old hardcoded value).
    port = int(os.environ.get("PORT", 5000))
    print(f"Listening on 0.0.0.0:{port}")
    print("Point your WAHA session's webhook URL (ngrok locally, or your Render URL once deployed) + /webhook at this app, with event 'message' enabled.")
    app.run(host="0.0.0.0", port=port)
