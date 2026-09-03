#!/usr/bin/env python3
"""
create_test_order.py — creates one real dummy order in the live database
so you can test the full PAID <order_id> <amount> flow against the actual
deployed bot, without needing a real customer or a real payment.

WHY THIS EXISTS: order_contract.py's own __main__ block already proves
the exact-match logic works, but it runs against an in-memory/local
test — it never touches the real Postgres database chat_agent.py reads
from on Render. This script creates an order the SAME way
_create_and_send_contract() in chat_agent.py does, so you can then send
a real PAID command to your real bot (over WhatsApp or email, as the
owner) and see it respond for real, exactly as it would for a genuine
customer order.

HOW TO USE:
    1. Run this ON RENDER, not locally — it needs the same DATABASE_URL
       your live chat_agent.py uses, so the order it creates is visible
       to the real, deployed bot. Easiest way: Render dashboard -> your
       service -> Shell tab -> run:
           python create_test_order.py
    2. It prints an order_id and the exact PAID command to send.
    3. Send that PAID command to the bot as the owner (WhatsApp or
       email, whichever OWNER_NOTIFY_CHANNEL you're using) and confirm
       you get the "confirmed — safe to proceed" reply back.
    4. This test order is real in the database (status starts as
       "awaiting_payment") — nothing auto-expires it, so it's safe to
       leave there, or note the order_id if you ever want to identify
       and ignore it later. No real money or real customer is involved
       at any point.

Optional flags let you test underpayment/overpayment too:
    python create_test_order.py --amount 5000
    (then try sending PAID <order_id> 1   -> should say "underpaid"
     and    PAID <order_id> 5000 -> should say "paid")
"""

from __future__ import annotations

import argparse
import os

import order_contract


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a dummy test order to try the PAID command against the real bot.")
    parser.add_argument("--amount", type=int, default=100,
                         help="Amount in rupees to lock in for this test order (default: 100 — small on purpose, so a real test payment is cheap if you want to actually pay it)")
    parser.add_argument("--customer-name", default="TEST ORDER (safe to ignore)")
    parser.add_argument("--customer-phone", default="test:dummy-order",
                         help="Not a real phone number or email — using a value that "
                              "won't match any real lead, so this test order never gets "
                              "confused with a genuine customer conversation.")
    args = parser.parse_args()

    upi_id = os.environ.get("OWNER_UPI_ID", "")
    if not upi_id:
        print("[warning] OWNER_UPI_ID is not set — the test order's payment link/QR will be broken, "
              "but the PAID command flow itself can still be tested (it only checks amounts, "
              "not whether the UPI link works).")

    order = order_contract.create_order(
        customer_name=args.customer_name,
        customer_phone=args.customer_phone,
        tier_name="TEST",
        scope_summary="Dummy order created by create_test_order.py — not a real customer.",
        amount_rupees=args.amount,
        upi_id=upi_id,
    )

    print(f"Created test order: {order['order_id']}")
    print(f"Locked amount: ₹{args.amount:,}")
    print()
    print("Send this to the bot as the owner (same chat you send APPROVE/SETPRICE to) to test a correct payment:")
    print(f"  PAID {order['order_id']} {args.amount}")
    print()
    print("To test underpayment instead, send a smaller amount first, e.g.:")
    print(f"  PAID {order['order_id']} 1")
    print("(should reply 'underpaid', and only THEN try the full amount above to confirm 'paid' works after)")
    print()
    print("This test order is not linked to any real WhatsApp/email conversation — "
          "sending PAID for it will never message a real customer.")


if __name__ == "__main__":
    main()
