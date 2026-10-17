#!/usr/bin/env python3
"""stdio <-> AgentTab MCP bridge, for MCP clients that launch local servers (Claude Desktop, Cursor, ...).

    AGENTTAB_URL=https://agenttab.onrender.com AGENTTAB_AGENT_KEY=atk_... python clients/agenttab_mcp_stdio.py

Reads one JSON-RPC message per line on stdin, POSTs it to <AGENTTAB_URL>/mcp with the agent key, writes the response
line to stdout. The bridge holds only the *agent* key: every purchase is still checked by AgentTab's caps, vendor
budgets, AI guard and owner approval on the server. Logs go to stderr only (stdout is the protocol channel).
"""
from __future__ import annotations

import base64
import json
import os
import sys

import httpx

PV_KEY = "io.modelcontextprotocol/protocolVersion"
SAFE = set(map(chr, range(0x21, 0x7F))) | {" "}


def _hdr(v: str) -> str:
    plain = v and all(ch in SAFE for ch in v) and v == v.strip() and not (v.startswith("=?base64?") and v.endswith("?="))
    return v if plain else "=?base64?" + base64.b64encode(v.encode()).decode() + "?="


class Bridge:
    def __init__(self, url: str, key: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/") + "/mcp"
        self.key = key
        self.client = client or httpx.Client(timeout=120)
        self.version = ""            # set by a legacy initialize handshake

    def headers_for(self, msg: dict) -> dict:
        h = {"X-Agent-Key": self.key, "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        pv = meta.get(PV_KEY) or self.version
        if pv:
            h["MCP-Protocol-Version"] = pv
        if isinstance(msg.get("method"), str):
            h["Mcp-Method"] = msg["method"]
        if isinstance(params.get("name"), str):
            h["Mcp-Name"] = _hdr(params["name"])
        return h

    def send(self, line: str) -> str | None:
        try:
            msg = json.loads(line)
        except ValueError:
            return json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        mid = msg.get("id") if isinstance(msg, dict) else None
        try:
            r = self.client.post(self.url, content=line.encode(), headers=self.headers_for(msg if isinstance(msg, dict) else {}))
        except httpx.HTTPError as e:
            return None if mid is None else json.dumps(
                {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"AgentTab unreachable: {type(e).__name__}"}})
        if r.status_code == 202 or not r.content:
            return None
        try:
            body = r.json()
        except ValueError:
            body = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"HTTP {r.status_code}"}}
        if isinstance(msg, dict) and msg.get("method") == "initialize" and isinstance(body.get("result"), dict):
            self.version = body["result"].get("protocolVersion", "")
        if mid is None:              # never answer a notification
            return None
        if body.get("id") is None and "error" in body:
            body["id"] = mid
        return json.dumps(body)


def main() -> int:
    url, key = os.getenv("AGENTTAB_URL", "https://agenttab.onrender.com"), os.getenv("AGENTTAB_AGENT_KEY", "")
    if not key:
        print("set AGENTTAB_AGENT_KEY (an agent key, never the admin token)", file=sys.stderr)
        return 2
    b = Bridge(url, key)
    for line in sys.stdin:
        if line.strip():
            out = b.send(line.strip())
            if out is not None:
                sys.stdout.write(out + "\n")
                sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
