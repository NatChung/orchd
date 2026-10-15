"""Durable notification outbox: tells the owning Orch that intake receipts are waiting.

One row per (receipt, owner), written in the same transaction as the submit or bind that created the duty, so a
crash never loses it. Delivery happens after commit and only wakes; the Orch reads content with the
non-consuming intake queries and an inbox read or a wake never counts as taking responsibility (only a decision
does). Wake text names counts and receipt ids, never the third-party text.
"""
import time

from . import store

URGENT = "urgent"
NORMAL = "normal"
DEFAULT_WINDOW = 1800.0


def enqueue(con, receipt_id, owner, urgency=NORMAL, *, now=None, window=DEFAULT_WINDOW):
    """Inside the caller's transaction. Idempotent per (receipt, owner): a replay never adds a row."""
    now = time.time() if now is None else now
    due = now if urgency == URGENT else now + window
    con.execute("INSERT INTO intake_notifications(receipt_id,owner_orch,urgency,due_at,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(receipt_id,owner_orch) DO UPDATE SET state='pending',urgency=excluded.urgency,"
                "due_at=excluded.due_at,error=NULL WHERE state='withdrawn'", (receipt_id, owner, urgency, due, now))


def upgrade(con, receipt_id, urgency, *, now=None):
    """A later suggestion marks it urgent: a pending notification becomes due now. A delivered one is never
    re-sent for an upgrade, and urgency never goes back down."""
    if urgency != URGENT:
        return
    now = time.time() if now is None else now
    con.execute("UPDATE intake_notifications SET urgency='urgent',due_at=MIN(due_at,?) "
                "WHERE receipt_id=? AND state IN ('pending','failed')", (now, receipt_id))


def rebind(con, receipt_id, old_owner, new_owner, *, now=None):
    """Owner changed while received: the old owner's undelivered duty is withdrawn and the new owner is told."""
    now = time.time() if now is None else now
    if old_owner:
        con.execute("UPDATE intake_notifications SET state='withdrawn' WHERE receipt_id=? AND owner_orch=? "
                    "AND state IN ('pending','failed')", (receipt_id, old_owner))
    row = con.execute("SELECT urgency FROM intake_notifications WHERE receipt_id=? ORDER BY id DESC LIMIT 1",
                      (receipt_id,)).fetchone()
    latest = con.execute("SELECT urgency FROM intake_suggestions WHERE receipt_id=? ORDER BY revision DESC LIMIT 1",
                         (receipt_id,)).fetchone()
    urgency = URGENT if URGENT in (latest and latest["urgency"], row and row["urgency"]) else NORMAL
    enqueue(con, receipt_id, new_owner, urgency, now=now)
