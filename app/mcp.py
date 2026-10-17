"""Minimal MCP server (Streamable HTTP, JSON responses, no sessions) so any MCP client can shop through AgentTab.

Speaks the stateless 2026-07-28 revision (server/discover, per-request `_meta` protocol version, MCP-Protocol-Version /
Mcp-Method / Mcp-Name header validation) and the handshake revisions 2025-11-25, 2025-06-18 and 2025-03-26
(initialize + notifications/initialized). Tools are thin wrappers around the same code paths as the REST API: the
agent key is the only credential this endpoint accepts, so every purchase still goes through the caps, the vendor
budgets, the AI guard and owner approval. There are deliberately no owner tools (approve, refund, freeze, saved card).
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED = (MODERN, *LEGACY)
PV_KEY = "io.modelcontextprotocol/protocolVersion"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
HEADER_MISMATCH, UNSUPPORTED_PROTOCOL_VERSION = -32020, -32022


class MCPError(Exception):
    def __init__(self, http: int, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.http, self.code, self.message, self.data = http, code, message, data


@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    handler: Callable[..., tuple[dict, bool]]     # (agent, args_model, request) -> (structured, is_error)
    read_only: bool = False
    open_world: bool = False
    title: str = ""

    def spec(self) -> dict:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        for p in schema.get("properties", {}).values():
            p.pop("title", None)
        schema["additionalProperties"] = False
        return {"name": self.name, "title": self.title or self.name, "description": self.description,
                "inputSchema": schema,
                "annotations": {"readOnlyHint": self.read_only, "destructiveHint": False,
                                "idempotentHint": self.read_only, "openWorldHint": self.open_world}}


def _decode_header(v: str) -> str:
    m = re.fullmatch(r"=\?base64\?([A-Za-z0-9+/=]*)\?=", v)
    if not m:
        return v
    try:
        return base64.b64decode(m.group(1), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise MCPError(400, HEADER_MISMATCH, "Mcp-Name header has invalid base64 encoding")


@dataclass
class MCPServer:
    name: str
    version: str
    instructions: str
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    # ---- protocol ----
    def _version(self, msg: dict, headers: dict) -> str:
        """Pick and validate the protocol version for one request (headers are lower-cased)."""
        hv = headers.get("mcp-protocol-version", "").strip()
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        bv = meta.get(PV_KEY)
        if msg.get("method") == "initialize":          # legacy handshake: version is negotiated in the body
            return "initialize"
        if hv and hv not in SUPPORTED or (bv is not None and bv not in SUPPORTED):
            raise MCPError(400, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                           {"supported": list(SUPPORTED), "requested": bv if bv not in SUPPORTED and bv is not None else hv})
        if bv is not None or hv == MODERN:
            if not hv:
                raise MCPError(400, HEADER_MISMATCH, "MCP-Protocol-Version header is required")
            if bv != hv:
                raise MCPError(400, HEADER_MISMATCH,
                               f"Header mismatch: MCP-Protocol-Version {hv!r} does not match body {bv!r}")
            if hv == MODERN and "id" in msg:
                if headers.get("mcp-method") != msg.get("method"):
                    raise MCPError(400, HEADER_MISMATCH, "Header mismatch: Mcp-Method header does not match body method")
                if msg.get("method") == "tools/call":
                    hn = headers.get("mcp-name")
                    if hn is None or _decode_header(hn) != params.get("name"):
                        raise MCPError(400, HEADER_MISMATCH, "Header mismatch: Mcp-Name header does not match params.name")
            return hv
        return hv or "2025-03-26"                     # legacy client that predates the header

    def _result(self, version: str, body: dict, cacheable: bool = False) -> dict:
        if version == MODERN:
            body = {"resultType": "complete", **body,
                    "_meta": {"io.modelcontextprotocol/serverInfo": {"name": self.name, "version": self.version}}}
            if cacheable:
                body |= {"ttlMs": 60_000, "cacheScope": "public"}
        return body

    def handle(self, msg: Any, headers: dict, agent, request) -> tuple[int, dict | None]:
        """One JSON-RPC message -> (http status, response body or None for 202)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            raise MCPError(400, INVALID_REQUEST, "expected a single JSON-RPC 2.0 request or notification object")
        mid, method = msg.get("id"), msg["method"]
        is_note = "id" not in msg
        if not is_note and (mid is None or isinstance(mid, bool) or not isinstance(mid, (str, int))):
            raise MCPError(400, INVALID_REQUEST, "id must be a string or integer")
        version = self._version(msg, headers)
        if is_note:
            return 202, None                          # notifications/initialized, notifications/cancelled, ...
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            raise MCPError(400, INVALID_PARAMS, "params must be an object", None)
        ok = lambda res: (200, {"jsonrpc": "2.0", "id": mid, "result": res})
        caps = {"tools": {"listChanged": False}}
        if method == "initialize":
            want = params.get("protocolVersion")
            pv = want if want in LEGACY else LEGACY[0]
            return ok({"protocolVersion": pv, "capabilities": caps,
                       "serverInfo": {"name": self.name, "version": self.version}, "instructions": self.instructions})
        if method == "server/discover":
            return ok(self._result(version, {"supportedVersions": list(SUPPORTED), "capabilities": caps,
                                             "instructions": self.instructions}, cacheable=True))
        if method == "ping":
            return ok(self._result(version, {}))
        if method == "tools/list":
            return ok(self._result(version, {"tools": [t.spec() for t in self.tools.values()]}, cacheable=True))
        if method == "tools/call":
            tool = self.tools.get(params.get("name"))
            if tool is None:
                raise MCPError(200, INVALID_PARAMS, f"Unknown tool: {str(params.get('name'))[:64]}", None)
            args = params.get("arguments") or {}
            try:
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                parsed = tool.args.model_validate(args)
            except (ValidationError, ValueError) as e:  # input errors are tool errors, so the model can fix them
                errs = e.errors(include_url=False, include_input=False) if isinstance(e, ValidationError) else [str(e)]
                return ok(self._result(version, self._content({"error": "invalid_arguments", "details": errs}, True)))
            structured, is_error = tool.handler(agent, parsed, request)
            return ok(self._result(version, self._content(structured, is_error)))
        raise MCPError(404 if version == MODERN else 200, METHOD_NOT_FOUND, f"Method not found: {method[:64]}")

    @staticmethod
    def _content(structured: dict, is_error: bool) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(structured, default=str)}],
                "structuredContent": structured, "isError": is_error}


def error_body(mid: Any, e: MCPError) -> dict:
    err = {"code": e.code, "message": e.message}
    if e.data is not None:
        err["data"] = e.data
    return {"jsonrpc": "2.0", "id": mid if isinstance(mid, (str, int)) and not isinstance(mid, bool) else None,
            "error": err}
