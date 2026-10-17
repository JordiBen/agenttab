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
CREATE TABLE IF NOT EXISTS webhook_events(
  id TEXT PRIMARY KEY, ts REAL, type TEXT, resource_id TEXT, agent_id TEXT, purchase_id TEXT,
  verified INTEGER, effect TEXT);
CREATE TABLE IF NOT EXISTS suggestions(
  id TEXT PRIMARY KEY, agent_id TEXT, purchase_id TEXT, kind TEXT, reason TEXT, created REAL,
  due REAL, status TEXT, decided REAL);
"""
AGENT_MIGRATIONS = [
    ("vendor_budgets", "TEXT"),            # JSON {vendor_id: rolling-30-day cap USD}; NULL = no allowlist
    ("approval_threshold_micro", "INTEGER"),  # above this, a human approves via a PayPal link; NULL = never
    ("refund_window_days", "INTEGER"),
    ("is_demo", "INTEGER DEFAULT 0"),      # set only by /api/demo/session; unlocks demo-owner actions
    ("frozen_reason", "TEXT"),             # set = agent frozen (reconciliation problem); no new spending
    ("frozen_ts", "REAL"),
    ("vault_id", "TEXT"),                  # PayPal Vault payment token of the owner's saved (sandbox) card
    ("vault_card", "TEXT"),                # display only, e.g. "VISA 1111"
    ("auto_topup_micro", "INTEGER"),       # saved-card auto top-up size; NULL = off
    ("auto_topup_cap_micro", "INTEGER"),   # max saved-card top-ups per rolling 30 days
]
ORDER_MIGRATIONS = [
    ("intent", "TEXT DEFAULT 'CAPTURE'"),  # AUTHORIZE for approval links (hold, then capture on delivery)
]
ENTRY_MIGRATIONS = [
    ("capture_ref", "TEXT"),        # refunds: which PayPal capture the money came back from
]
PURCHASE_MIGRATIONS = [
    ("used_ts", "REAL"),            # agent reported it actually used the item
    ("paypal_confirmed", "REAL"),   # PayPal webhook confirmed the capture
    ("refunded_ts", "REAL"),
    ("flag", "TEXT"),               # e.g. capture reversed / denied (from webhooks)
    ("paypal_authorization", "TEXT"),  # approval purchases: the PayPal hold, captured after delivery
]


class Ledger:
    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    def __init__(self, path: str):
        # one connection per thread: a single shared sqlite3 connection is not safe under concurrent requests
        self.path = path
        self._local = threading.local()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(agents)")}
        for name, typ in AGENT_MIGRATIONS:
            if name not in cols:
                self.db.execute(f"ALTER TABLE agents ADD COLUMN {name} {typ}")
        pcols = {r[1] for r in self.db.execute("PRAGMA table_info(purchases)")}
        for name, typ in PURCHASE_MIGRATIONS:
            if name not in pcols:
                self.db.execute(f"ALTER TABLE purchases ADD COLUMN {name} {typ}")
        ocols = {r[1] for r in self.db.execute("PRAGMA table_info(orders)")}
        for name, typ in ORDER_MIGRATIONS:
            if name not in ocols:
                self.db.execute(f"ALTER TABLE orders ADD COLUMN {name} {typ}")
        ecols = {r[1] for r in self.db.execute("PRAGMA table_info(entries)")}
        for name, typ in ENTRY_MIGRATIONS:
            if name not in ecols:
                self.db.execute(f"ALTER TABLE entries ADD COLUMN {name} {typ}")
        self.db.commit()
        self.lock = threading.Lock()

    # agents
    def create_agent(self, name: str, owner_email: str, policy: str,
                     daily_cap: Decimal, per_call_cap: Decimal, vendor_budgets: dict | None = None,
                     approval_threshold: Decimal | None = None, refund_window_days: int = 7,
                     is_demo: bool = False) -> dict:
        aid = "ag_" + secrets.token_hex(6)
        key = "atk_" + secrets.token_urlsafe(24)
        with self.lock:
            self.db.execute(
                "INSERT INTO agents(id,name,api_key,owner_email,policy,daily_cap_micro,per_call_cap_micro,created,"
                "vendor_budgets,approval_threshold_micro,refund_window_days,is_demo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, name, key, owner_email, policy, to_micro(daily_cap), to_micro(per_call_cap), time.time(),
                 json.dumps({k: str(v) for k, v in vendor_budgets.items()}) if vendor_budgets is not None else None,
                 to_micro(approval_threshold) if approval_threshold is not None else None, refund_window_days,
                 int(bool(is_demo))))
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

    # reconciliation freeze
    def freeze(self, aid: str, reason: str) -> bool:
        """Freeze an agent (first reason wins). True if this call froze it."""
        with self.lock:
            n = self.db.execute("UPDATE agents SET frozen_reason=?, frozen_ts=? WHERE id=? AND frozen_reason IS NULL",
                                (reason[:300], time.time(), aid)).rowcount
            self.db.commit()
        return n == 1

    def unfreeze(self, aid: str) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET frozen_reason=NULL, frozen_ts=NULL WHERE id=?", (aid,))
            self.db.commit()

    # saved card (Vault) + auto top-up
    def set_vault(self, aid: str, vault_id: str | None, card: str | None) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET vault_id=?, vault_card=? WHERE id=?", (vault_id, card, aid))
            if vault_id is None:
                self.db.execute("UPDATE agents SET auto_topup_micro=NULL, auto_topup_cap_micro=NULL WHERE id=?", (aid,))
            self.db.commit()

    def set_auto_topup(self, aid: str, amount: Decimal | None, monthly_cap: Decimal | None) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET auto_topup_micro=?, auto_topup_cap_micro=? WHERE id=?",
                            (to_micro(amount) if amount is not None else None,
                             to_micro(monthly_cap) if monthly_cap is not None else None, aid))
            self.db.commit()

    def vault_topups_since(self, aid: str, since: float) -> Decimal:
        r = self.db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM entries WHERE agent_id=? AND kind='fund'"
                            " AND tool IN ('vault','vault-auto') AND ts>=?", (aid, since)).fetchone()[0]
        return from_micro(r)

    def unsettled_auto_topups(self, aid: str, older_than: float):
        """Auto top-up credits whose saved-card hold was never captured (paypal_ref still empty; e.g. the process
        died between the purchase commit and the capture) and that were not already reversed. capture_ref holds
        the PayPal authorization id."""
        return self.db.execute(
            "SELECT * FROM entries e WHERE agent_id=? AND kind='fund' AND tool='vault-auto' AND paypal_ref=''"
            " AND ts<? AND NOT EXISTS (SELECT 1 FROM entries r WHERE r.kind='fund_reversal'"
            " AND r.capture_ref='entry:' || e.id) ORDER BY ts", (aid, older_than)).fetchall()

    def fund_entries(self, aid: str, limit: int = 50):
        """Every PayPal capture credited to this agent (tab top-ups and approval-funded purchases)."""
        return self.db.execute("SELECT * FROM entries WHERE agent_id=? AND kind='fund' AND paypal_ref!=''"
                               " ORDER BY ts DESC LIMIT ?", (aid, limit)).fetchall()

    # orders
    def add_order(self, order_id: str, aid: str, amount: Decimal, intent: str = "CAPTURE"):
        with self.lock:
            self.db.execute("INSERT INTO orders(order_id,agent_id,amount_micro,status,created,intent) VALUES(?,?,?,?,?,?)",
                            (order_id, aid, to_micro(amount), "CREATED", time.time(), intent))
            self.db.commit()

    def order(self, order_id: str):
        return self.db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()

    def claim_order(self, order_id: str, frm: tuple[str, ...], to: str) -> bool:
        """Atomic state transition: True only for the one caller that moved the order out of `frm`."""
        with self.lock:
            q = f"UPDATE orders SET status=? WHERE order_id=? AND status IN ({','.join('?' * len(frm))})"
            n = self.db.execute(q, (to, order_id, *frm)).rowcount
            self.db.commit()
        return n == 1

    def mark_order(self, order_id: str, status: str):
        with self.lock:
            self.db.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
            self.db.commit()

    # entries
    def add_entry(self, aid: str, kind: str, amount: Decimal, tool="", target="", purpose="",
                  decision="", reason="", paypal_ref="", capture_ref=""):
        with self.lock:
            self._insert_entry(aid, kind, amount, tool, target, purpose, decision, reason, paypal_ref, capture_ref)
            self.db.commit()

    def _insert_entry(self, aid, kind, amount, tool="", target="", purpose="", decision="", reason="",
                      paypal_ref="", capture_ref="") -> int:
        return self.db.execute(
            "INSERT INTO entries(agent_id,ts,kind,amount_micro,tool,target,purpose,decision,reason,paypal_ref,capture_ref)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (aid, time.time(), kind, to_micro(amount), tool, target, purpose, decision, reason, paypal_ref, capture_ref)).lastrowid

    def set_entry_paypal_ref(self, entry_id: int, paypal_ref: str, reason: str | None = None) -> None:
        with self.lock:
            if reason is None:
                self.db.execute("UPDATE entries SET paypal_ref=? WHERE id=?", (paypal_ref, entry_id))
            else:
                self.db.execute("UPDATE entries SET paypal_ref=?, reason=? WHERE id=?", (paypal_ref, reason, entry_id))
            self.db.commit()

    def refunded_on_capture(self, capture_id: str) -> Decimal:
        r = self.db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM entries WHERE kind='refund' AND capture_ref=?",
                            (capture_id,)).fetchone()[0]
        return from_micro(r)

    def balance(self, aid: str) -> Decimal:
        r = self.db.execute(
            "SELECT COALESCE(SUM(CASE kind WHEN 'fund' THEN amount_micro WHEN 'charge' THEN -amount_micro"
            " WHEN 'refund' THEN -amount_micro WHEN 'credit' THEN amount_micro WHEN 'fund_reversal' THEN -amount_micro"
            " ELSE 0 END),0)"
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
            self.db.execute("INSERT INTO purchases(id,agent_id,ts,vendor,item,quantity,amount_micro,target,purpose,"
                            "status,funding,refund_deadline,approval_expires,paypal_order,paypal_capture,"
                            "decision_source,reason,fulfillment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (pid, aid, time.time(), vendor, item, quantity, to_micro(amount), target, purpose,
                             status, funding, refund_deadline, approval_expires, paypal_order, "",
                             decision_source, reason, fulfillment))
            self.db.commit()
        return pid

    def commit_tab_purchase(self, aid: str, vendor: str, item: str, quantity: int, amount: Decimal, target: str,
                            purpose: str, refund_deadline: float | None, decision_source: str, reason: str,
                            fulfillment: str, tag: str, limits: dict, prefund: dict | None = None) -> tuple[str | None, str]:
        """Re-check balance, daily cap and vendor budget and write purchase + charge in ONE write transaction
        (BEGIN IMMEDIATE), so concurrent purchases can never overspend, even across processes.
        limits = {"daily_cap": Decimal, "vendor_budget": Decimal | None}. Returns (purchase_id, "") or (None, why).
        prefund = {"amount", "tool", "reason"}: an auto top-up held (authorized, not yet captured) on the saved card.
        Its fund entry is written in the same transaction, so it exists only if the purchase commits; the caller
        captures the hold afterwards and sets the entry's PayPal ref (prefund["entry_id"] is filled in here)."""
        now = time.time()
        with self.lock:
            db = self.db
            db.execute("BEGIN IMMEDIATE")
            try:
                if prefund:
                    prefund["entry_id"] = self._insert_entry(aid, "fund", prefund["amount"], prefund["tool"],
                                                             reason=prefund["reason"],
                                                             capture_ref=prefund.get("authorization_id", ""))
                bal = self.balance(aid)
                spent = self.spent_since(aid, now - 86400)
                why = ""
                if amount > bal:
                    why = "insufficient balance"
                elif spent + amount > limits["daily_cap"]:
                    why = f"daily cap ${limits['daily_cap']} would be exceeded (spent ${spent} in 24h)"
                elif limits.get("vendor_budget") is not None and \
                        self.vendor_spent_since(aid, vendor, now - 30 * 86400) + amount > limits["vendor_budget"]:
                    why = f"30-day budget ${limits['vendor_budget']} for {vendor} would be exceeded"
                if why:
                    db.execute("ROLLBACK")
                    return None, why
                pid = "pu_" + secrets.token_hex(6)
                db.execute("INSERT INTO purchases(id,agent_id,ts,vendor,item,quantity,amount_micro,target,purpose,"
                           "status,funding,refund_deadline,approval_expires,paypal_order,paypal_capture,"
                           "decision_source,reason,fulfillment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (pid, aid, now, vendor, item, quantity, to_micro(amount), target, purpose, "completed",
                            "tab", refund_deadline, None, "", "", decision_source, reason, fulfillment))
                self._insert_entry(aid, "charge", amount, f"{vendor}:{item}", target, purpose, "allow", tag, pid)
                db.execute("COMMIT")
                return pid, ""
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def claim_purchase(self, pid: str, frm: str, to: str) -> bool:
        """Atomic status transition (e.g. pending_approval -> completing): exactly one caller wins."""
        with self.lock:
            n = self.db.execute("UPDATE purchases SET status=? WHERE id=? AND status=?", (to, pid, frm)).rowcount
            self.db.commit()
        return n == 1

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

    def purchase_by_capture(self, capture_id: str):
        return self.db.execute("SELECT * FROM purchases WHERE paypal_capture=?", (capture_id,)).fetchone()

    def fund_entry_by_capture(self, capture_id: str):
        return self.db.execute("SELECT * FROM entries WHERE kind='fund' AND paypal_ref=?", (capture_id,)).fetchone()

    def refund_entry_by_ref(self, refund_id: str):
        return self.db.execute("SELECT * FROM entries WHERE kind='refund' AND paypal_ref=?", (refund_id,)).fetchone()

    def update_entries_ts(self, aid: str, delta: float):
        with self.lock:
            self.db.execute("UPDATE entries SET ts=ts+? WHERE agent_id=?", (delta, aid))
            self.db.execute("UPDATE purchases SET ts=ts+?, refund_deadline=refund_deadline+?, "
                            "approval_expires=approval_expires+?, used_ts=used_ts+?, refunded_ts=refunded_ts+? "
                            "WHERE agent_id=?", (delta, delta, delta, delta, delta, aid))
            self.db.execute("UPDATE suggestions SET created=created+?, due=due+? WHERE agent_id=?", (delta, delta, aid))
            self.db.execute("UPDATE webhook_events SET ts=ts+? WHERE agent_id=?", (delta, aid))
            self.db.execute("UPDATE agents SET created=created+? WHERE id=?", (delta, aid))
            self.db.commit()

    # webhooks
    def webhook_seen(self, eid: str) -> bool:
        return self.db.execute("SELECT 1 FROM webhook_events WHERE id=?", (eid,)).fetchone() is not None

    def claim_webhook(self, eid: str, typ: str, resource_id: str, verified: bool) -> bool:
        """INSERT OR IGNORE before processing: a redelivered event id is handled exactly once."""
        with self.lock:
            n = self.db.execute("INSERT OR IGNORE INTO webhook_events VALUES(?,?,?,?,?,?,?,?)",
                                (eid, time.time(), typ, resource_id, "", "", int(verified), "processing")).rowcount
            self.db.commit()
        return n == 1

    def finish_webhook(self, eid: str, aid: str, pid: str, effect: str):
        with self.lock:
            self.db.execute("UPDATE webhook_events SET agent_id=?, purchase_id=?, effect=? WHERE id=?",
                            (aid, pid, effect, eid))
            self.db.commit()

    def release_webhook(self, eid: str):
        with self.lock:
            self.db.execute("DELETE FROM webhook_events WHERE id=? AND effect='processing'", (eid,))
            self.db.commit()

    def add_webhook(self, eid: str, typ: str, resource_id: str, aid: str, pid: str, verified: bool, effect: str):
        if self.claim_webhook(eid, typ, resource_id, verified):
            self.finish_webhook(eid, aid, pid, effect)

    def webhooks(self, aid: str | None = None, limit: int = 200):
        q, a = ("SELECT * FROM webhook_events WHERE agent_id=? ORDER BY ts DESC LIMIT ?", (aid, limit)) if aid else \
            ("SELECT * FROM webhook_events ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in self.db.execute(q, a).fetchall()]

    # refund suggestions (made by the scheduling agent, confirmed by a human)
    def add_suggestion(self, aid: str, pid: str, kind: str, reason: str, due: float | None) -> str | None:
        # one open suggestion per purchase; a kind the owner already decided on is never re-proposed
        if self.db.execute("SELECT 1 FROM suggestions WHERE purchase_id=? AND (kind=? OR status='open')",
                           (pid, kind)).fetchone():
            return None
        sid = "sg_" + secrets.token_hex(5)
        with self.lock:
            self.db.execute("INSERT INTO suggestions VALUES(?,?,?,?,?,?,?,?,?)",
                            (sid, aid, pid, kind, reason, time.time(), due, "open", None))
            self.db.commit()
        return sid

    def suggestion(self, sid: str):
        return self.db.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()

    def suggestions(self, aid: str, status: str | None = None):
        q = "SELECT * FROM suggestions WHERE agent_id=?" + (" AND status=?" if status else "") + " ORDER BY created DESC"
        return [dict(r) for r in self.db.execute(q, (aid, status) if status else (aid,)).fetchall()]

    def set_suggestion(self, sid: str, status: str):
        with self.lock:
            self.db.execute("UPDATE suggestions SET status=?, decided=? WHERE id=?", (status, time.time(), sid))
            self.db.commit()
