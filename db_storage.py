#!/usr/bin/env python3
"""
db_storage.py — shared Postgres-backed storage for chat_agent.py and
order_contract.py, replacing conversations.json / orders.json.

WHY THIS EXISTS: the original JSON-file storage (load whole file, mutate
in Python, write whole file, guarded by a threading.Lock()) only works
correctly on a single, always-running process on one machine with a real
persistent disk. That ruled out free/ephemeral hosting (e.g. Render's free
tier wipes local files on every restart/redeploy) and made "run this
forever automatically" impossible without either a paid persistent disk or
external storage. This module keeps the exact same data shape — each
conversation/order is still one JSON-shaped Python dict — but stores it as
one row per phone/order_id in Postgres, with the dict itself living in a
JSONB column. That means almost none of chat_agent.py's or
order_contract.py's calling code has to change: only the load/save
functions do, since the data going in and coming out is still the same
dict shape as before.

SETUP: requires a Postgres connection string in the DATABASE_URL
environment variable (e.g. from Neon's free tier — see console.neon.tech,
"Connect" button on the project dashboard). Locally/in dev, DATABASE_URL
can point at any Postgres instance. init_db() must be called once (e.g.
at process startup) to create the tables if they don't already exist —
safe to call every startup, it's idempotent (CREATE TABLE IF NOT EXISTS).

CONCURRENCY: this module does NOT implement cross-process locking (e.g.
`SELECT ... FOR UPDATE`) — the read-modify-write pattern in
order_contract.py's check_payment() and chat_agent.py's set_stage() is
still only protected by their existing in-process threading.Lock()s,
exactly as it was with the old JSON files. That means the safety
guarantee is unchanged from before, not worse — but it also means this
still assumes a SINGLE process. If chat_agent.py is ever run with more
than one worker process (e.g. `gunicorn -w 4`), two workers could race on
the same order/conversation row with no lock protecting them, since each
process has its own separate threading.Lock(). Run with a single worker
(`gunicorn -w 1`) unless/until real row-level locking is added here.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _get_connection():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it to your Neon (or other "
            "Postgres) connection string as an environment variable — "
            "see console.neon.tech, 'Connect' button, for the string."
        )
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def _cursor(commit: bool = False):
    """Small helper so every function below doesn't repeat the same
    connect/cursor/commit/close boilerplate. Always closes the connection,
    even on error, so a failed call never leaks a connection."""
    conn = _get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Creates the two tables if they don't exist yet. Safe to call on
    every process startup — CREATE TABLE IF NOT EXISTS is a no-op if the
    table's already there, so this never wipes existing data."""
    with _cursor(commit=True) as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                phone TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS incoming_log (
                id SERIAL PRIMARY KEY,
                entry JSONB NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


# ---------------------------------------------------------------------------
# Conversations — same shape/semantics as the old _load_conversations() /
# _save_conversations() in chat_agent.py, but per-row instead of whole-file.
# ---------------------------------------------------------------------------

def load_all_conversations() -> dict:
    """Equivalent of the old _load_conversations(): returns the full
    {phone: convo_dict} mapping. Used sparingly — most call sites should
    prefer load_conversation(phone) for a single lead, which is cheaper
    and doesn't require pulling every conversation to look at one."""
    with _cursor() as cur:
        cur.execute("SELECT phone, data FROM conversations")
        rows = cur.fetchall()
        return {row["phone"]: row["data"] for row in rows}


def load_conversation(phone: str) -> Optional[dict]:
    with _cursor() as cur:
        cur.execute("SELECT data FROM conversations WHERE phone = %s", (phone,))
        row = cur.fetchone()
        return row["data"] if row else None


def save_conversation(phone: str, convo: dict) -> None:
    """Upsert — same as the old pattern of loading the whole file,
    setting conversations[phone] = convo, and saving the whole file, but
    now scoped to a single row so two different leads' saves can never
    collide with each other the way two near-simultaneous whole-file
    writes could before."""
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO conversations (phone, data, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (phone) DO UPDATE SET data = EXCLUDED.data, updated_at = now()
            """,
            (phone, json.dumps(convo)),
        )


# ---------------------------------------------------------------------------
# Orders — same shape/semantics as the old _load_orders() / _save_orders()
# in order_contract.py.
# ---------------------------------------------------------------------------

def load_all_orders() -> dict:
    with _cursor() as cur:
        cur.execute("SELECT order_id, data FROM orders")
        rows = cur.fetchall()
        return {row["order_id"]: row["data"] for row in rows}


def load_order(order_id: str) -> Optional[dict]:
    with _cursor() as cur:
        cur.execute("SELECT data FROM orders WHERE order_id = %s", (order_id,))
        row = cur.fetchone()
        return row["data"] if row else None


def save_order(order_id: str, order: dict) -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO orders (order_id, data, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (order_id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()
            """,
            (order_id, json.dumps(order)),
        )


# ---------------------------------------------------------------------------
# Incoming webhook log — was append_to_log() writing to incoming_log.json
# ---------------------------------------------------------------------------

def append_log_entry(entry: dict) -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO incoming_log (entry) VALUES (%s)",
            (json.dumps(entry),),
        )
