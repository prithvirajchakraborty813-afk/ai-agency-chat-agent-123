#!/usr/bin/env python3
"""
brevo_webhook.py — receives Brevo's transactional-email event webhooks
(delivered, deferred, soft_bounce, hard_bounce, blocked, opened, spam,
unsubscribed, etc) and writes each one onto the matching contacted_leads
row so the inbox UI can show real delivery status, not just "Brevo
accepted this at send time."

WHY THIS EXISTS: email_sender.send_email()'s (ok, detail, message_id)
return value only ever meant "Brevo's API accepted this for sending" —
resp.status_code in (200, 201). It says nothing about what actually
happened afterward: whether the mailbox exists, whether the message
bounced, got blocked by a spam filter, or was ever opened. Brevo tracks
all of that itself and pushes it here asynchronously, as a separate
HTTP POST per event, keyed on the messageId from the original send —
which is why send_proposals.py now saves that id via
db_storage.mark_contacted(..., message_id=...) at send time.

SETUP: in app.brevo.com -> Transactional -> Settings -> Webhooks, add a
webhook pointing at:
    https://<your-render-app>.onrender.com/brevo-webhook?key=<WEBHOOK_SECRET>
and select every event type you want tracked (recommended: delivered,
soft bounce, hard bounce, blocked, deferred, opened at minimum — spam
and unsubscribed are also worth including if you want that visibility).
Set WEBHOOK_SECRET as an env var on Render — same idea as inbox.py's
INBOX_SECRET, a long random string only you and Brevo's webhook config
know, so this route can't be spammed with fake events by anyone who
finds the URL. If WEBHOOK_SECRET is unset, the route fails closed (401
for every request) rather than open.

INTEGRATION: registered into chat_agent.py's existing Flask `app` as a
blueprint, same pattern as inbox.inbox_bp — see the
app.register_blueprint(brevo_webhook.brevo_webhook_bp) line added there.
Nothing new to deploy; this rides the same process/port.

EVENT SHAPE: Brevo POSTs one JSON object per event (not a batch) with at
least "event" and "message-id" keys — exact field set varies slightly
by event type, so this reads defensively rather than assuming every key
is always present. See https://developers.brevo.com/docs/transactional-webhooks
for the full reference if a new field needs handling later.
"""

from __future__ import annotations

import os

from flask import Blueprint, request, jsonify

import db_storage

brevo_webhook_bp = Blueprint("brevo_webhook", __name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _check_auth() -> bool:
    if not WEBHOOK_SECRET:
        return False
    supplied = request.args.get("key") or request.headers.get("X-Webhook-Key", "")
    return supplied == WEBHOOK_SECRET


@brevo_webhook_bp.route("/brevo-webhook", methods=["POST"])
def receive_brevo_event():
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    # Brevo's field is "message-id" (hyphenated) on nearly every event
    # type; a couple of older event shapes have used "messageId" with the
    # same meaning, so both are checked here rather than assuming one.
    message_id = payload.get("message-id") or payload.get("messageId") or ""
    event = payload.get("event", "")
    # "reason" (bounce/block explanations) is the most useful free-text
    # field Brevo sends; not every event type includes it, hence the
    # fallback chain rather than a single .get().
    detail = payload.get("reason") or payload.get("tag") or ""

    if not message_id or not event:
        # Malformed or unrecognized payload shape — log and 200 back so
        # Brevo doesn't retry indefinitely on something that will never
        # parse differently, but don't pretend we recorded anything.
        print(f"[brevo-webhook] Ignored payload missing message-id or event: {payload}")
        return jsonify({"status": "ignored"}), 200

    updated = db_storage.update_delivery_status(message_id, event, detail)
    if not updated:
        # A real event for a message this app has no record of — most
        # likely a send from before message_id tracking was added, or a
        # message_id typo somewhere. Not an error worth failing the
        # webhook over; Brevo doesn't need to retry this.
        print(f"[brevo-webhook] No contacted_leads row found for message_id={message_id} (event={event})")

    return jsonify({"status": "ok", "updated": updated}), 200
