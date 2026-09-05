#!/usr/bin/env python3
"""
mailjet_webhook.py — receives Mailjet's transactional-email event
webhooks (sent, open, click, bounce, blocked, spam, unsub, etc) and
writes each one onto the matching contacted_leads row, same as
brevo_webhook.py does for Brevo. Reuses the same
db_storage.update_delivery_status(message_id, event, detail) function —
that call is provider-agnostic (it just matches on message_id), so no
db_storage.py changes were needed for this.

WHY THIS EXISTS: email_sender.py's module docstring notes (as of this
writing) that Mailjet sends have no delivery-status visibility past
"Mailjet's API accepted this at send time" — this file closes that gap
for Mailjet the same way brevo_webhook.py already does for Brevo.

SETUP: in app.mailjet.com -> Account Settings -> REST API -> Event
Tracking, add a webhook pointing at:
    https://<your-render-app>.onrender.com/mailjet-webhook?key=<WEBHOOK_SECRET>
and select every event type you want tracked (recommended: sent, open,
bounce, blocked, spam at minimum; click and unsub are optional extras).
Reuses the SAME WEBHOOK_SECRET env var already set for brevo_webhook.py
— this is a shared "does the caller know our secret" check, not a
per-provider one, same idea as inbox.py's INBOX_SECRET. If WEBHOOK_SECRET
is unset, the route fails closed (401 for every request) rather than open.

INTEGRATION: registered into chat_agent.py's existing Flask `app` as a
blueprint, same pattern as brevo_webhook.brevo_webhook_bp — add:
    import mailjet_webhook
    app.register_blueprint(mailjet_webhook.mailjet_webhook_bp)
next to the existing brevo_webhook registration line. Nothing new to
deploy separately; this rides the same process/port.

EVENT SHAPE: Mailjet can POST either a single JSON object per event, OR
a JSON array of multiple event objects in one request (batch mode,
depending on Mailjet's own dashboard settings) — this reads defensively
and handles both. Each event object's key fields are "event" (sent,
open, click, bounce, blocked, spam, unsub, ...), "MessageID" (numeric,
matches the message_id captured at send time in
email_sender._send_via_mailjet), and for bounce/blocked events an
"error_related_to" / "error" pair with the human-readable reason.
See https://dev.mailjet.com/email/guides/webhooks/ for the full
reference if a new field needs handling later.
"""

from __future__ import annotations

import os

from flask import Blueprint, request, jsonify

import db_storage

mailjet_webhook_bp = Blueprint("mailjet_webhook", __name__)

# Same shared secret as brevo_webhook.py — set once, used by every
# provider's webhook route.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _check_auth() -> bool:
    if not WEBHOOK_SECRET:
        return False
    supplied = request.args.get("key") or request.headers.get("X-Webhook-Key", "")
    return supplied == WEBHOOK_SECRET


def _extract_detail(event: dict) -> str:
    # Mailjet's bounce/blocked events carry the human-readable reason
    # under a couple of different possible keys depending on event type
    # and API version — checked in order, first one present wins, same
    # defensive-fallback approach as brevo_webhook.py's detail extraction.
    return (
        event.get("error_related_to")
        or event.get("error")
        or event.get("comment")
        or ""
    )


def _process_one_event(event: dict) -> tuple[str, str, bool]:
    """Returns (message_id, event_type, updated) for a single Mailjet
    event dict. Does not raise on a malformed/unexpected shape — just
    returns empty/False so the caller can log and move on, same
    "log and 200 back" philosophy as brevo_webhook.py (Mailjet shouldn't
    retry indefinitely on a payload shape that will never parse
    differently)."""
    message_id = str(event.get("MessageID", "") or "")
    event_type = event.get("event", "")

    if not message_id or not event_type:
        print(f"[mailjet-webhook] Ignored event missing MessageID or event: {event}")
        return message_id, event_type, False

    detail = _extract_detail(event)
    updated = db_storage.update_delivery_status(message_id, event_type, detail)
    if not updated:
        # A real event for a message this app has no record of — most
        # likely a send from before Mailjet was added, or a message_id
        # mismatch. Not an error worth failing the webhook over.
        print(f"[mailjet-webhook] No contacted_leads row found for message_id={message_id} (event={event_type})")
    return message_id, event_type, updated


@mailjet_webhook_bp.route("/mailjet-webhook", methods=["POST"])
def receive_mailjet_event():
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        print("[mailjet-webhook] Ignored request with no parseable JSON body")
        return jsonify({"status": "ignored"}), 200

    # Mailjet may send one event object, or a batch array of several —
    # normalize to a list either way so the same loop handles both.
    events = payload if isinstance(payload, list) else [payload]

    results = [_process_one_event(event) for event in events if isinstance(event, dict)]
    updated_count = sum(1 for _, _, updated in results if updated)

    return jsonify({"status": "ok", "received": len(results), "updated": updated_count}), 200
