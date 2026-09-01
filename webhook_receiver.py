#!/usr/bin/env python3
"""
webhook_receiver.py — Step 1 of Agent #7: just prove that incoming
WhatsApp replies actually reach your machine in real time.

This does NOT reply to anyone yet, and does NOT call Gemini yet. It
only listens for what Whapi.Cloud sends, prints it, and saves it to
incoming_log.json — so you can see with your own eyes that a real
message from a real phone landed here, before we build anything on
top of it.

HOW TO RUN THIS:
    1. In one terminal:  python webhook_receiver.py
       (leaves it listening on http://localhost:5000)
    2. In a second terminal:  ngrok http 5000
       (gives you a public URL like https://abc123.ngrok-free.app)
    3. In the Whapi.Cloud dashboard, under your channel's Webhooks
       section, set the URL to:
           https://abc123.ngrok-free.app/webhook
       (use YOUR actual ngrok URL, and keep the /webhook path)
    4. From a different phone, send a WhatsApp message to your number.
    5. Watch this terminal — it should print the incoming message
       within a second or two.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from flask import Flask, request, jsonify

app = Flask(__name__)
LOG_PATH = "incoming_log.json"

# Whapi can send several webhook POSTs in a tight burst (e.g. multiple
# messages arriving seconds apart, or a message + its status update).
# Flask's dev server can handle overlapping requests, and without a
# lock, two requests can both read the same "old" version of the log
# file, then each write their own copy back — the second write wins
# and silently erases whatever the first one added. This lock forces
# every read-modify-write of the log file to happen one at a time.
_log_lock = threading.Lock()


def append_to_log(entry: dict) -> None:
    with _log_lock:
        log = []
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, encoding="utf-8") as f:
                try:
                    log = json.load(f)
                except json.JSONDecodeError:
                    log = []
        log.append(entry)
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}

    print("\n" + "=" * 60)
    print(f"[{datetime.now(timezone.utc).isoformat()}] Incoming webhook:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("=" * 60)

    # Always log the raw payload first, no matter its shape. This is our
    # safety net: even if the parsing below misses a new event shape, we
    # still have the exact JSON on disk to look at afterward.
    append_to_log({
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": payload,
    })

    # Whapi sends different event shapes. Handle both known ones:
    # 1) "messages" — a direct incoming-message event
    # 2) "chats_updates" — a chat-updated event that WRAPS the message
    #    inside after_update.last_message
    messages = payload.get("messages", [])
    for msg in messages:
        if msg.get("from_me"):
            continue  # skip our own outgoing messages echoed back
        from_number = msg.get("from", "unknown")
        text = msg.get("text", {}).get("body", "")
        print(f"\n>>> REAL REPLY (messages) from {from_number}: {text}\n")

    for update in payload.get("chats_updates", []):
        last_msg = (update.get("after_update") or {}).get("last_message") or {}
        if not last_msg or last_msg.get("from_me"):
            continue  # skip empty updates or our own outgoing echoes
        from_number = last_msg.get("from", "unknown")
        text = (last_msg.get("text") or {}).get("body", "")
        print(f"\n>>> REAL REPLY (chats_updates) from {from_number}: {text}\n")

    return jsonify({"status": "received"}), 200


@app.route("/", methods=["GET"])
def health():
    return "Webhook receiver is running.", 200


if __name__ == "__main__":
    print("Webhook receiver starting on http://localhost:5000")
    print("Point ngrok at this port, then set the ngrok URL + /webhook in Whapi's dashboard.")
    app.run(host="0.0.0.0", port=5000)
