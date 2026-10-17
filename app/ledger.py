"""SQLite ledger: agents, funding captures, metered charges, denials, refunds.
Amounts are stored as integer micro-dollars to avoid float drift."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from decimal import Decimal

MICRO = Decimal("1000000")


def to_micro(d: Decimal) -> int:
    return int((d * MICRO).to_integral_value())


def from_micro(m: int) -> Decimal:
    return (Decimal(m) / MICRO).quantize(Decimal("0.0001"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, name TEXT, api_key TEXT UNIQUE, owner_email TEXT,
  policy TEXT, daily_cap_micro INTEGER, per_call_cap_micro INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, ts REAL, kind TEXT,
  amount_micro INTEGER, tool TEXT, target TEXT, purpose TEXT, decision TEXT,
  reason TEXT, paypal_ref TEXT);
CREATE TABLE IF NOT EXISTS orders(
  order_id TEXT PRIMARY KEY, agent_id TEXT, amount_micro INTEGER, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS purchases(
  id TEXT PRIMARY KEY, agent_id TEXT, ts REAL, vendor TEXT, item TEXT, quantity INTEGER,
  amount_micro INTEGER, target TEXT, purpose TEXT, status TEXT, funding TEXT,
  refund_deadline REAL, approval_expires REAL, paypal_order TEXT, paypal_capture TEXT,
  decision_source TEXT, reason TEXT, fulfillment TEXT);
"""
AGENT_MIGRATIONS = [
    ("vendor_budgets", "TEXT"),            # JSON {vendor_id: rolling-30-day cap USD}; NULL = no allowlist
    ("approval_threshold_micro", "INTEGER"),  # above this, a human approves via a PayPal link; NULL = never
    ("refund_window_days", "INTEGER"),
]


class Ledger:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(agents)")}
        for name, typ in AGENT_MIGRATIONS:
            if name not in cols:
                self.db.execute(f"ALTER TABLE agents ADD COLUMN {name} {typ}")
        self.db.commit()
        self.lock = threading.Lock()

    # agents
    def create_agent(self, name: str, owner_email: str, policy: str,
                     daily_cap: Decimal, per_call_cap: Decimal, vendor_budgets: dict | None = None,
                     approval_threshold: Decimal | None = None, refund_window_days: int = 7) -> dict:
        aid = "ag_" + secrets.token_hex(6)
        key = "atk_" + secrets.token_urlsafe(24)
        with self.lock:
            self.db.execute(
                "INSERT INTO agents(id,name,api_key,owner_email,policy,daily_cap_micro,per_call_cap_micro,created,"
                "vendor_budgets,approval_threshold_micro,refund_window_days) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (aid, name, key, owner_email, policy, to_micro(daily_cap), to_micro(per_call_cap), time.time(),
                 json.dumps({k: str(v) for k, v in vendor_budgets.items()}) if vendor_budgets is not None else None,
                 to_micro(approval_threshold) if approval_threshold is not None else None, refund_window_days))
            self.db.commit()
        return {"agent_id": aid, "api_key": key}

    def agent_by_key(self, key: str):
        return self.db.execute("SELECT * FROM agents WHERE api_key=?", (key,)).fetchone()

    def agent(self, aid: str):
        return self.db.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()

    def agents(self):
        return self.db.execute("SELECT * FROM agents ORDER BY created").fetchall()

    def update_policy(self, aid: str, policy: str | None, daily_cap: Decimal | None, per_call_cap: Decimal | None):
        a = self.agent(aid)
        with self.lock:
            self.db.execute("UPDATE agents SET policy=?, daily_cap_micro=?, per_call_cap_micro=? WHERE id=?",
                            (policy if policy is not None else a["policy"],
                             to_micro(daily_cap) if daily_cap is not None else a["daily_cap_micro"],
                             to_micro(per_call_cap) if per_call_cap is not None else a["per_call_cap_micro"], aid))
            self.db.commit()

    # orders
    def add_order(self, order_id: str, aid: str, amount: Decimal):
        with self.lock:
            self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?)",
                            (order_id, aid, to_micro(amount), "CREATED", time.time()))
            self.db.commit()

    def order(self, order_id: str):
        return self.db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()

    def mark_order(self, order_id: str, status: str):
        with self.lock:
            self.db.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
            self.db.commit()

    # entries
    def add_entry(self, aid: str, kind: str, amount: Decimal, tool="", target="", purpose="",
                  decision="", reason="", paypal_ref=""):
        with self.lock:
            self.db.execute(
                "INSERT INTO entries(agent_id,ts,kind,amount_micro,tool,target,purpose,decision,reason,paypal_ref)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, time.time(), kind, to_micro(amount), tool, target, purpose, decision, reason, paypal_ref))
            self.db.commit()

    def balance(self, aid: str) -> Decimal:
        r = self.db.execute(
            "SELECT COALESCE(SUM(CASE kind WHEN 'fund' THEN amount_micro WHEN 'charge' THEN -amount_micro"
            " WHEN 'refund' THEN -amount_micro WHEN 'credit' THEN amount_micro ELSE 0 END),0)"
            " FROM entries WHERE agent_id=?", (aid,)).fetchone()[0]
        return from_micro(r)

    def spent_since(self, aid: str, since: float) -> Decimal:
        r = self.db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM purchases WHERE agent_id=?"
                            " AND status='completed' AND ts>=?", (aid, since)).fetchone()[0]
        return from_micro(r)

    def vendor_spent_since(self, aid: str, vendor: str, since: float) -> Decimal:
        r = self.db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM purchases WHERE agent_id=? AND vendor=?"
                            " AND status='completed' AND ts>=?", (aid, vendor, since)).fetchone()[0]
        return from_micro(r)

    def set_agent_procurement(self, aid: str, vendor_budgets: dict | None, approval_threshold: Decimal | None,
                              refund_window_days: int | None):
        a = self.agent(aid)
        with self.lock:
            self.db.execute("UPDATE agents SET vendor_budgets=?, approval_threshold_micro=?, refund_window_days=? WHERE id=?",
                            (json.dumps({k: str(v) for k, v in vendor_budgets.items()}) if vendor_budgets is not None else a["vendor_budgets"],
                             to_micro(approval_threshold) if approval_threshold is not None else a["approval_threshold_micro"],
                             refund_window_days if refund_window_days is not None else a["refund_window_days"], aid))
            self.db.commit()

    # purchases
    def add_purchase(self, aid: str, vendor: str, item: str, quantity: int, amount: Decimal, target: str,
                     purpose: str, status: str, funding: str, refund_deadline: float | None,
                     approval_expires: float | None, decision_source: str, reason: str,
                     paypal_order: str = "", fulfillment: str = "") -> str:
        pid = "pu_" + secrets.token_hex(6)
        with self.lock:
            self.db.execute("INSERT INTO purchases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (pid, aid, time.time(), vendor, item, quantity, to_micro(amount), target, purpose,
                             status, funding, refund_deadline, approval_expires, paypal_order, "",
                             decision_source, reason, fulfillment))
            self.db.commit()
        return pid

    def purchase(self, pid: str):
        return self.db.execute("SELECT * FROM purchases WHERE id=?", (pid,)).fetchone()

    def purchase_by_order(self, order_id: str):
        return self.db.execute("SELECT * FROM purchases WHERE paypal_order=?", (order_id,)).fetchone()

    def update_purchase(self, pid: str, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE purchases SET {cols} WHERE id=?", (*fields.values(), pid))
            self.db.commit()

    def purchases(self, aid: str, limit: int = 200):
        rows = self.db.execute("SELECT * FROM purchases WHERE agent_id=? ORDER BY ts DESC LIMIT ?", (aid, limit))
        return [dict(r) | {"amount_usd": float(from_micro(r["amount_micro"]))} for r in rows.fetchall()]

    def captures(self, aid: str):
        # Only tab top-ups; captures that paid for a specific approved purchase are refunded per purchase.
        return self.db.execute("SELECT * FROM entries WHERE agent_id=? AND kind='fund' AND tool!='approval'"
                               " ORDER BY ts DESC", (aid,)).fetchall()

    def entries(self, aid: str | None = None, limit: int = 500):
        if aid:
            rows = self.db.execute("SELECT * FROM entries WHERE agent_id=? ORDER BY ts DESC LIMIT ?", (aid, limit))
        else:
            rows = self.db.execute("SELECT * FROM entries ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) | {"amount_usd": float(from_micro(r["amount_micro"]))} for r in rows.fetchall()]
