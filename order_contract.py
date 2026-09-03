#!/usr/bin/env python3
"""
order_contract.py — part of Agent #7 (Chat Agent). Handles the two
things that sit right after the user taps "approve" on a deal summary
and right before any work begins:

  1. Generate a short written contract/terms message — order ID,
     exact scope, exact price, no-refund-once-started policy — sent
     to the customer as the final offer, alongside the payment link/QR.
  2. Verify an incoming payment against that specific order's exact
     expected amount, so a wrong or partial payment can NEVER be
     silently treated as "paid, proceed."

WHY THIS EXISTS: direct UPI (used because the PAN-less workaround
means no gateway KYC) is a peer-to-peer transfer, not an itemized
checkout — nothing on the payment side inherently ties a rupee amount
to a specific order. Without this module, a ₹1 test payment, a
partial payment, or an unrelated payment from the same number could
be mistaken for "the order is paid" if the check only looks for
"did any money arrive" instead of "did the exact right amount arrive
for this exact order."

DESIGN RULE, non-negotiable per the project's payment-risk decision:
an order only ever moves to "proceed with work" when the amount
received exactly matches the amount locked in at approval time.
Underpayment and overpayment are BOTH treated as not-yet-resolved,
never as an automatic green light.

    Chat Agent negotiates + drafts terms
                |
                v
    User approves  -->  create_order() locks order_id + exact amount
                |
                v
    build_contract_message() --> sent to customer via WhatsApp
                |
                v
    Customer pays (direct UPI, note field = order_id)
                |
                v
    check_payment() --> exact match only --> mark_paid() --> proceed
                     --> short  --> nudge once, then flag for user
                     --> over   --> flag for user, never auto-proceed

Orders are stored in orders.json, keyed by order_id, so this can be
called repeatedly (e.g. once per incoming payment notification) without
losing state between runs.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import db_storage

ORDERS_PATH = "orders.json"  # unused now — kept only so old references don't NameError; state lives in Postgres via db_storage

# Kept for structural compatibility with any code that still acquires this
# lock directly, but the real cross-process safety now comes from
# Postgres itself (each save_order call is a single atomic upsert) rather
# than from this in-process lock, which never protected against two
# separate processes anyway.
_orders_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Order storage — backed by Postgres (see db_storage.py) instead of a local
# JSON file, so orders survive redeploys/restarts on ephemeral hosting.
# Function names/signatures are unchanged on purpose so create_order(),
# check_payment(), and get_order() below — including the exact-payment-
# match logic that guards against the ₹1 exploit — don't need to change
# at all; only what's behind these two functions changed.
# ---------------------------------------------------------------------------

def _load_orders() -> dict:
    return db_storage.load_all_orders()


def _save_orders(orders: dict) -> None:
    for order_id, order in orders.items():
        db_storage.save_order(order_id, order)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Order lifecycle
# ---------------------------------------------------------------------------

def create_order(
    customer_name: str,
    customer_phone: str,
    tier_name: str,
    scope_summary: str,
    amount_rupees: int,
    upi_id: str,
) -> dict:
    """
    Called once, right after the user taps "approve" on a deal summary.
    Locks in the order_id and the exact expected amount — nothing
    downstream can change this number; the customer is only ever shown
    this locked amount, never asked to "enter an amount" themselves.
    """
    order_id = "ORD-" + uuid.uuid4().hex[:8].upper()

    order = {
        "order_id": order_id,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "tier_name": tier_name,
        "scope_summary": scope_summary,
        "amount_rupees": amount_rupees,
        "upi_id": upi_id,
        "status": "awaiting_payment",  # awaiting_payment -> paid -> (or) flagged_mismatch
        "created_at": _now(),
        "paid_at": None,
        "payment_history": [],  # every payment attempt/notification we see, even wrong ones
        "nudged_once": False,
    }

    with _orders_lock:
        orders = _load_orders()
        orders[order_id] = order
        _save_orders(orders)

    return order


def build_upi_link(order: dict) -> str:
    """
    Standard UPI deep link. The order_id goes in the transaction note
    (tn) field so it shows up in both the customer's UPI app and your
    own bank/UPI notification — this is what lets a human (you) match
    a real bank notification back to a specific order at a glance,
    since personal UPI has no API to check this automatically.

    IMPORTANT: pn (payee name) is required in practice even though the
    UPI spec marks it optional — GPay and PhonePe will silently fail
    ("no app found") or refuse to open a payment screen without it.
    All free-text values are URL-encoded since UPI apps parse this
    strictly; an unencoded space, &, or other special character in the
    business name or note can break the whole link.

    KNOWN LIMITATION: even with pn set, this upi:// deep link can still
    fail to open at all on some phones/UPI-app combinations — WhatsApp
    tries to hand the URI off to an app that "supports" it and some
    devices don't route it correctly, producing a "no app found" style
    error even though the link itself is well-formed. build_upi_qr_png()
    below exists specifically to route around that: same pa/am/tn data,
    but as a QR code any UPI app's built-in scanner can read directly,
    without relying on the OS resolving a custom URI scheme at all.
    """
    upi_id = order["upi_id"]
    amount = order["amount_rupees"]
    order_id = order["order_id"]
    payee_name = order.get("payee_name") or "Prithviraj"
    note = urllib.parse.quote(order_id)
    pn = urllib.parse.quote(payee_name)
    return f"upi://pay?pa={upi_id}&pn={pn}&am={amount}&tn={note}&cu=INR"


def build_upi_qr_png(order: dict) -> bytes:
    """Encodes the exact same UPI string from build_upi_link() as a QR
    code PNG, in-memory (no temp file). Requires the 'qrcode' package —
    `pip install qrcode[pil]`. Raises ImportError with a clear message if
    it's not installed, rather than a confusing stack trace deep in a
    webhook handler."""
    try:
        import qrcode
    except ImportError as e:
        raise ImportError(
            "The 'qrcode' package is required for QR payment codes. "
            "Install it with: pip install qrcode[pil]"
        ) from e

    import io
    upi_link = build_upi_link(order)
    img = qrcode.make(upi_link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_contract_message(order: dict) -> str:
    """
    The exact message sent to the customer as the final offer, right
    after user approval. Deliberately short and plain — this is a
    WhatsApp message, not a legal document — but states the three
    things that actually matter: what they're getting, the exact
    price, and the no-refund-once-started policy, so there's no
    ambiguity for either side about what payment means.

    Payee name + UPI ID are stated explicitly, in plain text, before the
    link — not just left inside the upi:// URL. This exists because a
    personal UPI ID (used here instead of a registered business account,
    since the no-PAN workaround rules out merchant KYC) will never show
    a verified-merchant badge on the payment screen the way a registered
    business like Blinkit does — as of NPCI's June 2026 verified-name
    rule, apps only display the actual bank-registered name, with no way
    to attach a custom business badge without that registration. Stating
    the expected name/ID here in writing gives the customer something
    concrete to manually cross-check against whatever their UPI app
    shows, since the app itself won't do that reassurance for them.

    An owner contact number is also included, right after the UPI
    details — a customer paying a personal (non-verified-merchant) UPI ID
    has no in-app way to confirm they're paying the right person, and no
    support channel if something looks wrong before they pay. A direct
    phone number gives them somewhere to check first. Read from the
    OWNER_CONTACT_PHONE env var (falls back to a hardcoded default below)
    rather than something only chat_agent.py knows, since this function
    can be called standalone (see the __main__ test block at the bottom
    of this file).
    """
    upi_link = build_upi_link(order)
    payee_name = order.get("payee_name") or "Prithviraj"
    upi_id = order["upi_id"]
    owner_contact_phone = os.environ.get("OWNER_CONTACT_PHONE", "94330 66933")
    return (
        f"Here's the final order summary — order ID {order['order_id']}.\n\n"
        f"Package: {order['tier_name']}\n"
        f"Scope: {order['scope_summary']}\n"
        f"Total: ₹{order['amount_rupees']:,} (full payment upfront, no partial start)\n\n"
        f"Once payment is received in full, work begins. "
        f"Please note: once work has started, this order is non-refundable, "
        f"so let us know now if anything above doesn't match what you expect.\n\n"
        f"Paying: {payee_name}\n"
        f"UPI ID: {upi_id}\n"
        f"Amount: ₹{order['amount_rupees']:,}\n"
        f"(This is a personal UPI ID, not a registered business account, so "
        f"your UPI app may not show a verified merchant badge — that's "
        f"expected, please just check the name/UPI ID/amount above match "
        f"what your app displays before paying.)\n\n"
        f"Questions before paying, or anything doesn't look right? Contact us "
        f"directly: {owner_contact_phone}\n\n"
        f"Pay via UPI: {upi_link}\n"
        f"(Please keep the order ID {order['order_id']} in the payment note if your "
        f"UPI app allows it — this helps us confirm your payment instantly.)"
    )


# ---------------------------------------------------------------------------
# Payment verification — the part that prevents the ₹1 exploit
# ---------------------------------------------------------------------------

@dataclass
class PaymentCheckResult:
    order_id: str
    outcome: str  # "paid" | "underpaid" | "overpaid" | "unknown_order"
    expected_rupees: Optional[int]
    received_rupees: int
    message_for_user: str  # what to tell the business owner (Billo)
    message_for_customer: Optional[str] = None  # only set for underpaid (nudge)


def check_payment(order_id: str, received_rupees: float) -> PaymentCheckResult:
    """
    THE core safety check. Call this every time a payment notification
    comes in (however that notification arrives — manually entered by
    the user for now, automated later if a proper gateway is added).

    Deliberately does exact-match comparison, not "received >= 0" or
    "received > 0" — a ₹1 payment against an expected ₹8,000 order
    must never be treated as anything other than "underpaid."
    """
    with _orders_lock:
        orders = _load_orders()
        order = orders.get(order_id)

        if order is None:
            return PaymentCheckResult(
                order_id=order_id,
                outcome="unknown_order",
                expected_rupees=None,
                received_rupees=received_rupees,
                message_for_user=(
                    f"Received a payment of ₹{received_rupees:,.2f} referencing "
                    f"order {order_id}, but no such order exists. Do not proceed "
                    f"with anything — check manually."
                ),
            )

        expected = order["amount_rupees"]
        order["payment_history"].append({
            "received_at": _now(),
            "received_rupees": received_rupees,
        })

        if order["status"] == "paid":
            # Already fully paid — a later payment against the same
            # order_id (accidental duplicate, or someone re-paying) must
            # never flip a settled order back to an unresolved state.
            orders[order_id] = order
            _save_orders(orders)
            return PaymentCheckResult(
                order_id=order_id,
                outcome="overpaid",
                expected_rupees=expected,
                received_rupees=received_rupees,
                message_for_user=(
                    f"Order {order_id} ({order['customer_name']}) was already marked "
                    f"paid, but received ANOTHER payment of ₹{received_rupees:,.2f} "
                    f"against it. Possible duplicate payment or reused order ID — "
                    f"do not treat as a second order automatically, check manually."
                ),
            )

        if received_rupees == expected:
            order["status"] = "paid"
            order["paid_at"] = _now()
            orders[order_id] = order
            _save_orders(orders)
            return PaymentCheckResult(
                order_id=order_id,
                outcome="paid",
                expected_rupees=expected,
                received_rupees=received_rupees,
                message_for_user=(
                    f"Order {order_id} ({order['customer_name']}) — full payment of "
                    f"₹{expected:,} confirmed. Safe to proceed with work."
                ),
            )

        if received_rupees < expected:
            already_nudged = order["nudged_once"]
            order["status"] = "flagged_mismatch"
            order["nudged_once"] = True
            orders[order_id] = order
            _save_orders(orders)
            shortfall = expected - received_rupees
            customer_msg = None
            user_msg = (
                f"Order {order_id} ({order['customer_name']}) — received "
                f"₹{received_rupees:,.2f}, expected ₹{expected:,}. Short by "
                f"₹{shortfall:,.2f}. DO NOT start work."
            )
            if not already_nudged:
                customer_msg = (
                    f"Thanks — we've received ₹{received_rupees:,.2f} for order "
                    f"{order['order_id']}, but the total for this package is "
                    f"₹{expected:,}. Please send the remaining ₹{shortfall:,.2f} "
                    f"to complete your order — work will begin once the full "
                    f"amount is received."
                )
                user_msg += " Auto-nudge sent to customer once; flagging for you to follow up if needed."
            else:
                user_msg += " Already nudged once before — flagging for you to follow up directly."
            return PaymentCheckResult(
                order_id=order_id,
                outcome="underpaid",
                expected_rupees=expected,
                received_rupees=received_rupees,
                message_for_user=user_msg,
                message_for_customer=customer_msg,
            )

        # received_rupees > expected
        order["status"] = "flagged_mismatch"
        orders[order_id] = order
        _save_orders(orders)
        overage = received_rupees - expected
        return PaymentCheckResult(
            order_id=order_id,
            outcome="overpaid",
            expected_rupees=expected,
            received_rupees=received_rupees,
            message_for_user=(
                f"Order {order_id} ({order['customer_name']}) — received "
                f"₹{received_rupees:,.2f}, expected ₹{expected:,}. Overpaid by "
                f"₹{overage:,.2f}. Flagging for manual review — do not assume "
                f"this is a tip, confirm with the customer before proceeding."
            ),
        )


def get_order(order_id: str) -> Optional[dict]:
    return db_storage.load_order(order_id)


if __name__ == "__main__":
    # Quick manual smoke test — not a real CLI yet, just proves the
    # exact-match logic works before it's wired into the live webhook.
    test_order = create_order(
        customer_name="Test Customer",
        customer_phone="910000000000",
        tier_name="Starter",
        scope_summary="WhatsApp FAQ + booking bot, single location",
        amount_rupees=8000,
        upi_id="example@upi",
    )
    print("Created order:", test_order["order_id"])
    print()
    print(build_contract_message(test_order))
    print()

    for amt in (1, 4000, 8000, 8000, 9000):
        result = check_payment(test_order["order_id"], amt)
        print(f"-- paid ₹{amt} --> outcome={result.outcome}")
        print("  ", result.message_for_user)
        if result.message_for_customer:
            print("   [nudge to customer]:", result.message_for_customer)
