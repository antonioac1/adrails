"""MCP over stdio (JSON-RPC 2.0, one JSON object per line).

Dispatch is pure (:meth:`Server.handle`); the only I/O lives in :meth:`Server.serve`.
"""
import io
import json
import math
import sys
import time

from .errors import ToolError, scrub

DEFAULT_PROTOCOL_VERSION = "2025-06-18"


def _matches(value, typ):
    """True when *value* satisfies a JSON-Schema scalar *typ* (bool is never integer/number)."""
    if typ == "string":
        return isinstance(value, str)
    if typ == "boolean":
        return isinstance(value, bool)
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "object":
        return isinstance(value, dict)
    if typ == "array":
        return isinstance(value, list)
    return True  # unknown/absent type: nothing to enforce


def validate_arguments(schema, args):
    """Return a list of human-readable problems; an empty list means the arguments are valid."""
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    schema = schema or {}
    props = schema.get("properties") or {}
    problems = []
    for name in schema.get("required") or []:
        if name not in args:
            problems.append("missing required property: %s" % name)
    for name, value in args.items():
        spec = props.get(name)
        if spec is None:
            if schema.get("additionalProperties") is False:
                problems.append("unexpected property: %s" % name)
            continue
        typ = (spec or {}).get("type")
        if typ is not None and not _matches(value, typ):
            problems.append("property %s must be of type %s" % (name, typ))
    return problems


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _text_result(mid, text, is_error):
    return _result(mid, {"content": [{"type": "text", "text": text}], "isError": bool(is_error)})


class Server:
    """A minimal MCP server: register tools with :meth:`tool`, dispatch with :meth:`handle`."""

    def __init__(self, name, version, on_call=None):
        self.name = name
        self.version = version
        self.on_call = on_call
        self._tools = {}
        self._order = []

    def tool(self, name, description, input_schema):
        """Decorator registering ``fn(args: dict) -> dict`` under *name*."""

        def register(fn):
            if name not in self._tools:
                self._order.append(name)
            self._tools[name] = {
                "name": name,
                "description": description,
                "inputSchema": input_schema,
                "fn": fn,
            }
            return fn

        return register

    # -- dispatch ---------------------------------------------------------
    def handle(self, msg):
        if not isinstance(msg, dict):
            return _error(None, -32600, "Invalid Request")
        method = msg.get("method")
        mid = msg.get("id")
        valid_id = _valid_id(mid)
        if not isinstance(method, str):
            if "id" not in msg:
                return None
            return _error(mid if valid_id else None, -32600, "Invalid Request")
        if "id" not in msg or method.startswith("notifications/"):
            return None  # notifications are never answered
        if not valid_id:
            return _error(None, -32600, "Invalid Request")
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            version = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
            return _result(mid, {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": self.version},
            })
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in (self._tools[n] for n in self._order)
            ]})
        if method == "tools/call":
            return self._tools_call(mid, params)
        return _error(mid, -32601, "Method not found: %s" % method)

    def _tools_call(self, mid, params):
        name = params.get("name")
        started = time.perf_counter()
        entry = self._tools.get(name) if isinstance(name, str) else None
        if entry is None:
            return _error(mid, -32602, "Unknown tool: %s" % name)
        args = params.get("arguments")
        if args is None:
            args = {}
        problems = validate_arguments(entry["inputSchema"], args)
        if problems:
            message = "invalid arguments: " + "; ".join(problems)
            self._meter(name, False, started, None, message)
            return _text_result(mid, message, True)
        try:
            result = entry["fn"](args)
        except ToolError as exc:
            message = scrub(str(exc))
            self._meter(name, False, started, None, message)
            return _text_result(mid, message, True)
        except Exception as exc:  # every other failure still becomes a tool error
            message = "internal error: %s: %s" % (type(exc).__name__, scrub(str(exc), 300))
            self._meter(name, False, started, None, message)
            return _text_result(mid, message, True)
        structured = result if isinstance(result, dict) else None
        self._meter(name, True, started, structured, None)
        return _result(mid, {
            "content": [{"type": "text", "text": json.dumps(result, default=str)}],
            "structuredContent": result,
            "isError": False,
        })

    def _meter(self, tool, ok, started, result, error):
        if self.on_call is None:
            return
        ms = (time.perf_counter() - started) * 1000.0
        try:
            self.on_call(tool, ok, ms, result, error)
        except Exception:  # metering must never break the response
            pass

    # -- transport --------------------------------------------------------
    def serve(self, stdin=sys.stdin, stdout=sys.stdout):
        """Read JSON-RPC lines from *stdin*, write responses to *stdout*; return on EOF."""
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                resp = _error(None, -32700, "Parse error")
            else:
                try:
                    resp = self.handle(msg)
                except Exception:  # a malformed request must never end the session
                    resp = _error(None, -32603, "Internal error")
            if resp is not None:
                try:
                    out = json.dumps(resp, default=str, allow_nan=False)
                except ValueError:
                    out = json.dumps(_error(resp.get("id"), -32603, "Internal error"))
                stdout.write(out + "\n")
                stdout.flush()


def _valid_id(mid):
    """JSON-RPC 2.0: a request id is a string, a finite number or null."""
    if mid is None or isinstance(mid, str):
        return True
    if isinstance(mid, bool) or not isinstance(mid, (int, float)):
        return False
    return math.isfinite(mid)


def _reject_constant(name):
    raise ValueError("non-JSON constant %s" % name)


def selftest():
    total = 0

    def ok(cond, detail="assertion failed"):
        nonlocal total
        assert cond, detail
        total += 1

    seen = []

    def on_call(tool, is_ok, ms, result, error):
        seen.append((tool, is_ok, ms, result, error))

    srv = Server("adsops-test", "0.0.1", on_call=on_call)

    @srv.tool("echo", "Echo back input.", {
        "type": "object",
        "properties": {"text": {"type": "string"}, "count": {"type": "integer"},
                       "flag": {"type": "boolean"}, "items": {"type": "array"},
                       "payload": {"type": "object"}},
        "required": ["text"], "additionalProperties": False})
    def _echo(args):
        return {"text": args["text"], "ops_count": 2}

    @srv.tool("plain", "No required properties.", {"type": "object", "properties": {}})
    def _plain(args):
        return {"ok": True}

    @srv.tool("boom", "Raises ToolError carrying a token.", {"type": "object", "properties": {}})
    def _boom(args):
        raise ToolError("bad credential ya29.secretValue123 for you")

    @srv.tool("crash", "Raises an unexpected error.", {"type": "object", "properties": {}})
    def _crash(args):
        raise ValueError("kaboom")

    # initialize: echo client version, else default
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"}})
    ok(r["id"] == 1 and r["jsonrpc"] == "2.0")
    ok(r["result"]["protocolVersion"] == "2024-11-05")
    ok(r["result"]["capabilities"] == {"tools": {}})
    ok(r["result"]["serverInfo"] == {"name": "adsops-test", "version": "0.0.1"})
    r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
    ok(r["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION)

    # notifications -> None ; explicit null id is still a request
    ok(srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)
    ok(srv.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}) is None)
    r = srv.handle({"jsonrpc": "2.0", "id": None, "method": "ping"})
    ok(r is not None and r["id"] is None and r["result"] == {})

    # ping
    ok(srv.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"] == {})

    # tools/list in registration order
    r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    ok([t["name"] for t in r["result"]["tools"]] == ["echo", "plain", "boom", "crash"])
    ok(r["result"]["tools"][0]["description"] == "Echo back input.")
    ok(r["result"]["tools"][0]["inputSchema"]["required"] == ["text"])

    # unknown tool -> -32602
    r = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}})
    ok(r["error"]["code"] == -32602 and "nope" in r["error"]["message"])

    # unknown method -> -32601
    r = srv.handle({"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
    ok(r["error"]["code"] == -32601 and "resources/list" in r["error"]["message"])

    # argument validation: required / type / bool-not-integer / additionalProperties
    call = lambda mid, arguments: srv.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                                              "params": {"name": "echo", "arguments": arguments}})
    r = call(7, {})
    text7 = r["result"]["content"][0]["text"]
    ok(r["result"]["isError"] is True and text7.startswith("invalid arguments:")
       and "text" in text7)
    ok(seen[-1][0] == "echo" and seen[-1][1] is False)
    r = call(8, {"text": 5})
    ok(r["result"]["isError"] is True and "must be of type string" in r["result"]["content"][0]["text"])
    r = call(9, {"text": "x", "count": True})
    ok(r["result"]["isError"] is True and "count" in r["result"]["content"][0]["text"])
    r = call(10, {"text": "x", "extra": 1})
    ok(r["result"]["isError"] is True and "extra" in r["result"]["content"][0]["text"])
    r = call(11, {"text": "x", "count": 3, "flag": False, "items": [], "payload": {}})
    ok(r["result"]["isError"] is False)
    r = call(12, {"text": "x", "items": "nope"})
    ok(r["result"]["isError"] is True and "items" in r["result"]["content"][0]["text"])

    # success shape + on_call timing
    r = srv.handle({"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "hi"}}})
    res = r["result"]
    ok(res["isError"] is False)
    ok(res["structuredContent"] == {"text": "hi", "ops_count": 2})
    ok(res["content"][0]["type"] == "text"
       and json.loads(res["content"][0]["text"]) == {"text": "hi", "ops_count": 2})
    ok(seen[-1][0] == "echo" and seen[-1][1] is True and seen[-1][2] >= 0.0)
    ok(seen[-1][3] == {"text": "hi", "ops_count": 2} and seen[-1][4] is None)

    # ToolError -> isError with scrubbed text
    r = srv.handle({"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                    "params": {"name": "boom", "arguments": {}}})
    boom_text = r["result"]["content"][0]["text"]
    ok(r["result"]["isError"] is True and "ya29" not in boom_text and "bad credential" in boom_text)
    ok(seen[-1][1] is False and seen[-1][4] == boom_text and seen[-1][3] is None)

    # unexpected exception -> internal error text
    r = srv.handle({"jsonrpc": "2.0", "id": 15, "method": "tools/call",
                    "params": {"name": "crash", "arguments": {}}})
    crash_text = r["result"]["content"][0]["text"]
    ok(r["result"]["isError"] is True
       and crash_text.startswith("internal error: ValueError:") and "kaboom" in crash_text)

    # an on_call that raises never breaks the response
    def angry(*_a, **_k):
        raise RuntimeError("meter down")

    srv2 = Server("x", "1", on_call=angry)

    @srv2.tool("t", "d", {"type": "object", "properties": {}})
    def _t(args):
        return {"ops_count": 0}

    r = srv2.handle({"jsonrpc": "2.0", "id": 16, "method": "tools/call",
                     "params": {"name": "t", "arguments": {}}})
    ok(r["result"]["isError"] is False and r["result"]["structuredContent"] == {"ops_count": 0})

    # serve() over StringIO: blank lines skipped, parse error reported, notifications silent
    payload = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 21, "method": "ping"}),
        "{not json",
        "",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 22, "method": "tools/list"}),
    ]) + "\n"
    out = io.StringIO()
    srv.serve(stdin=io.StringIO(payload), stdout=out)
    lines = out.getvalue().splitlines()
    ok(len(lines) == 3, lines)
    parsed = [json.loads(x) for x in lines]
    ok(parsed[0]["id"] == 21 and parsed[0]["result"] == {})
    ok(parsed[1]["id"] is None and parsed[1]["error"]["code"] == -32700)
    ok(parsed[2]["id"] == 22 and len(parsed[2]["result"]["tools"]) == 4)

    return total
