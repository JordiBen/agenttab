"""SQLite ledger: agents, funding captures, metered charges, denials, refunds.
Amounts are stored as integer micro-dollars to avoid float drift."""
from __future__ import annotations

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
"""


class Ledger:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # agents
    def create_agent(self, name: str, owner_email: str, policy: str,
                     daily_cap: Decimal, per_call_cap: Decimal) -> dict:
        aid = "ag_" + secrets.token_hex(6)
        key = "atk_" + secrets.token_urlsafe(24)
        with self.lock:
            self.db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?)",
                            (aid, name, key, owner_email, policy, to_micro(daily_cap),
                             to_micro(per_call_cap), time.time()))
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
            " WHEN 'refund' THEN -amount_micro ELSE 0 END),0) FROM entries WHERE agent_id=?", (aid,)).fetchone()[0]
        return from_micro(r)

    def spent_since(self, aid: str, since: float) -> Decimal:
        r = self.db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM entries WHERE agent_id=? AND kind='charge'"
                            " AND ts>=?", (aid, since)).fetchone()[0]
        return from_micro(r)

    def captures(self, aid: str):
        return self.db.execute("SELECT * FROM entries WHERE agent_id=? AND kind='fund' ORDER BY ts DESC",
                               (aid,)).fetchall()

    def entries(self, aid: str | None = None, limit: int = 500):
        if aid:
            rows = self.db.execute("SELECT * FROM entries WHERE agent_id=? ORDER BY ts DESC LIMIT ?", (aid, limit))
        else:
            rows = self.db.execute("SELECT * FROM entries ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) | {"amount_usd": float(from_micro(r["amount_micro"]))} for r in rows.fetchall()]
