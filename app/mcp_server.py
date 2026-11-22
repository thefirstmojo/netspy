#!/usr/bin/env python3
"""NetSpy MCP server - read-only Model Context Protocol interface.

Transport: JSON-RPC 2.0 over HTTP (Streamable HTTP; plain `application/json`
answers, no SSE stream). Endpoint `POST /mcp`, port `MCP_PORT` (default 8092).

Hard rules, because this port sits next to an unauthenticated LAN dashboard:

* **READ ONLY.** No tool changes settings, starts/stops anything, runs a
  command, or types into an SSH terminal. Those paths stay in the UI only.
* **Token required.** The listener is not even started unless `MCP_TOKEN` is
  set; every request is compared against it in constant time. No CORS headers,
  body capped at 1 MiB.
* **stdlib only** - the image keeps its zero-dependency property.
* Tools read the live `Monitor` snapshots. Nothing is cached or persisted.

The module is imported as `mcp` from the app directory (that is also why it is
named this way: no third-party MCP SDK exists in the image).
"""
from __future__ import annotations

import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from shared import load_version

MCP_PATH = "/mcp"
HEALTH_PATH = "/mcp/health"
MAX_BODY = 1024 * 1024
DEFAULT_PORT = 8092
SERVER_NAME = "netspy"
# Protocol revisions we speak, newest first. The client's requested revision is
# echoed when we know it, otherwise we answer with the newest we support.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
VERSION = load_version()


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _r(value, digits: int = 1) -> float:
    """Round a byte/s or percent value; None/'' become 0.0."""
    try:
        return round(float(value or 0.0), digits)
    except (TypeError, ValueError):
        return 0.0


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _limit(args: dict, default: int = 20, maximum: int = 500) -> int:
    try:
        n = int(args.get("limit") or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


def _pick_hosts(monitor, args: dict) -> list:
    """Resolve the optional `host` filter; unknown names raise a teachable error."""
    known = [s["name"] for s in monitor.servers]
    wanted = str(args.get("host") or "").strip()
    if not wanted:
        return known
    for name in known:
        if name.lower() == wanted.lower():
            return [name]
    raise ValueError(f"unknown host {wanted!r}; known hosts: {', '.join(known) or 'none'}")


def _agents(monitor) -> dict:
    """{host: agent payload} - only hosts that have produced a snapshot."""
    return {s["name"]: (monitor.snaps.get(s["name"]) or {}) for s in monitor.servers}


# --------------------------------------------------------------------------
# tools (all read-only)
# --------------------------------------------------------------------------
def tool_overview(monitor, args: dict) -> dict:
    """Cluster summary: hosts, agent versions, totals, and every warning the UI would show."""
    hosts = _pick_hosts(monitor, args)
    servers, warnings = [], []
    total_rx = total_tx = 0.0
    for name in hosts:
        snap = monitor.snaps.get(name) or {}
        online = bool(monitor.online.get(name))
        err = monitor.errors.get(name, "")
        totals = snap.get("totals") or {}
        rx, tx = _r(totals.get("rx")), _r(totals.get("tx"))
        total_rx += rx
        total_tx += tx
        servers.append({
            "name": name,
            "online": online,
            "hostname": snap.get("hostname", ""),
            "agent_version": snap.get("version", ""),
            "error": err,
            "rx_bps": rx,
            "tx_bps": tx,
        })
        if not online:
            warnings.append(f"{name}: offline ({err or 'no data yet'})")
        if snap and snap.get("ss_ok") is False:
            warnings.append(f"{name}: socket accounting failed ({snap.get('ss_error') or 'ss error'}) "
                            "- per-process rates are unreliable")
        if snap and snap.get("storage_host") is False:
            warnings.append(f"{name}: no host access (proc_count={snap.get('proc_count')}, "
                            f"pid1={snap.get('pid1')}) - the agent container needs pid: host")
    return {
        "version": VERSION,
        "ts": int(time.time()),
        "hosts": servers,
        "total_rx_bps": _r(total_rx),
        "total_tx_bps": _r(total_tx),
        "warnings": warnings,
    }


def tool_agent_health(monitor, args: dict) -> dict:
    """Per-host agent diagnostics: pid1, proc_count, ss status, host access, docker socket."""
    hosts = _pick_hosts(monitor, args)
    rows = []
    for name in hosts:
        snap = monitor.snaps.get(name) or {}
        lat = list(monitor.latency.get(name) or [])
        docker = snap.get("docker") or {}
        row = {
            "host": name,
            "online": bool(monitor.online.get(name)),
            "hostname": snap.get("hostname", ""),
            "agent_version": snap.get("version", ""),
            "web_version": VERSION,
            "pid1": snap.get("pid1", ""),
            "proc_count": _int(snap.get("proc_count")),
            "storage_host": snap.get("storage_host"),
            "ss_ok": snap.get("ss_ok"),
            "ss_error": snap.get("ss_error", ""),
            "docker": {
                "socket": docker.get("socket"),
                "containers": _int(docker.get("containers")),
                "veths_mapped": _int(docker.get("veths_mapped")),
            },
            "poll_latency_ms": _int(lat[-1][1]) if lat else None,
            "error": monitor.errors.get(name, ""),
        }
        if snap.get("version") and snap.get("version") != VERSION:
            row["version_mismatch"] = f"agent {snap.get('version')} != web {VERSION} - update the agent container"
        rows.append(row)
    return {"hosts": rows}


def tool_processes(monitor, args: dict) -> dict:
    """Top processes by network rate, CPU, RAM or disk I/O (merged across hosts)."""
    kind = str(args.get("kind") or "rate").lower()
    if kind not in ("rate", "cpu", "mem", "disk"):
        raise ValueError("kind must be one of: rate, cpu, mem, disk")
    limit = _limit(args, 20, 200)
    min_rate = _r(args.get("min_rate_bps") or 0, 3)
    hosts = _pick_hosts(monitor, args)

    rows: dict = {}
    for name in hosts:
        snap = monitor.snaps.get(name) or {}
        if kind == "rate":
            items = [(p.get("name", "?"), {"rx_bps": _r(p.get("rx")), "tx_bps": _r(p.get("tx")),
                                           "conns": _int(p.get("conns"))}, p.get("container"))
                     for p in snap.get("processes", [])]
        elif kind == "disk":
            items = [(p.get("name", "?"), {"read_bps": _r(p.get("read")), "write_bps": _r(p.get("write")),
                                           "read10_bps": _r(p.get("read10")), "write10_bps": _r(p.get("write10"))},
                      p.get("container"))
                     for p in snap.get("disk", [])]
        else:
            procs = (snap.get("system") or {}).get("procs", [])
            items = [(p.get("name", "?"), {"cpu_pct": _r(p.get("cpu")), "cpu10_pct": _r(p.get("cpu10")),
                                           "mem_bytes": _int(p.get("mem"))}, p.get("container"))
                     for p in procs]

        for pname, vals, container in items:
            row = rows.setdefault(pname, {"name": pname, "container": container, "hosts": {}})
            row["hosts"][name] = vals
            if container and not row.get("container"):
                row["container"] = container

    out = []
    for row in rows.values():
        vals = list(row["hosts"].values())
        if kind == "rate":
            total = sum(v["rx_bps"] + v["tx_bps"] for v in vals)
            if total < min_rate:
                continue
        elif kind == "cpu":
            total = sum(v["cpu_pct"] for v in vals)
        elif kind == "mem":
            total = sum(v["mem_bytes"] for v in vals)
        else:
            total = sum(v["read_bps"] + v["write_bps"] for v in vals)
        row["total"] = _r(total) if kind != "mem" else _int(total)
        out.append(row)

    out.sort(key=lambda r: r["total"], reverse=True)
    units = {"rate": "bytes/s", "cpu": "percent of one core", "mem": "bytes", "disk": "bytes/s"}[kind]
    return {"kind": kind, "units": units, "hosts": hosts, "total_rows": len(out),
            "truncated": len(out) > limit, "rows": out[:limit]}


_PEER_RE = re.compile(r"\[([0-9a-fA-F:.]+)\]")


def tool_peers(monitor, args: dict) -> dict:
    """Who talks to this host: process rows that carry a peer address or open sockets."""
    limit = _limit(args, 25, 200)
    search = str(args.get("search") or "").strip().lower()
    hosts = _pick_hosts(monitor, args)
    rows = []
    for name in hosts:
        snap = monitor.snaps.get(name) or {}
        for p in snap.get("processes", []):
            pname = p.get("name", "")
            match = _PEER_RE.search(pname)
            conns = _int(p.get("conns"))
            if not match and not conns:
                continue
            if search and search not in pname.lower():
                continue
            rows.append({
                "host": name,
                "process": pname,
                "peer": match.group(1) if match else None,
                "conns": conns,
                "rx_bps": _r(p.get("rx")),
                "tx_bps": _r(p.get("tx")),
                "container": p.get("container"),
            })
    rows.sort(key=lambda r: r["rx_bps"] + r["tx_bps"], reverse=True)
    return {
        "rows": rows[:limit],
        "total_rows": len(rows),
        "truncated": len(rows) > limit,
        "note": "peer is parsed from the process name (e.g. smbd[192.168.2.99]); "
                "the kernel caps comm at 15 characters, the full address comes from the socket",
    }


def tool_containers(monitor, args: dict) -> dict:
    """Docker containers with their traffic (veth sums) plus the per-host docker status."""
    limit = _limit(args, 30, 300)
    hosts = _pick_hosts(monitor, args)
    rows, status = [], {}
    for name in hosts:
        snap = monitor.snaps.get(name) or {}
        docker = snap.get("docker") or {}
        if docker:
            status[name] = {"socket": docker.get("socket"),
                            "containers": _int(docker.get("containers")),
                            "veths_mapped": _int(docker.get("veths_mapped"))}
        for c in snap.get("containers", []):
            rows.append({"host": name, "name": c.get("name", "?"),
                         "rx_bps": _r(c.get("rx")), "tx_bps": _r(c.get("tx"))})
    rows.sort(key=lambda r: r["rx_bps"] + r["tx_bps"], reverse=True)
    return {"docker": status, "total_rows": len(rows), "truncated": len(rows) > limit,
            "rows": rows[:limit]}


def tool_interfaces(monitor, args: dict) -> dict:
    """Physical and virtual interfaces per host, uplink flagged (to tell real traffic from veth sums)."""
    limit = _limit(args, 60, 300)
    hosts = _pick_hosts(monitor, args)
    rows = []
    for name in hosts:
        snap = monitor.snaps.get(name) or {}
        for i in snap.get("interfaces", []):
            rows.append({"host": name, "name": i.get("name", "?"),
                         "uplink": bool(i.get("uplink")),
                         "container": i.get("container"),
                         "rx_bps": _r(i.get("rx")), "tx_bps": _r(i.get("tx"))})
    rows.sort(key=lambda r: r["rx_bps"] + r["tx_bps"], reverse=True)
    return {"total_rows": len(rows), "truncated": len(rows) > limit, "rows": rows[:limit]}


def tool_storage(monitor, args: dict) -> dict:
    """Pool/dataset fill levels: current values, optionally with the recorded history."""
    store = getattr(monitor, "storage_store", None)
    if store is None:
        raise ValueError("storage store unavailable - the data directory is not mounted")
    span = str(args.get("span") or "now").lower()
    if span not in ("now", "h24", "d7", "m1", "m"):
        raise ValueError("span must be one of: now, h24, d7, m1, m")
    hosts = _pick_hosts(monitor, args)
    with store.lock:
        enabled = list(store.data.get("enabled", []))
        pools = dict(store.data.get("pools", {}))
    available = store.available_map(monitor.servers, monitor.snaps)

    rows = []
    for key, info in available.items():
        if info["server"] not in hosts:
            continue
        size, used = _int(info.get("size")), _int(info.get("used"))
        entry = pools.get(key) or {}
        row = {
            "key": key,
            "name": info.get("name", key),
            "server": info.get("server", ""),
            "type": info.get("type", ""),
            "size_bytes": size,
            "used_bytes": used,
            "free_bytes": max(0, size - used),
            "used_pct": round(100.0 * used / size, 1) if size else None,
            "recording": key in enabled,
        }
        if span != "now":
            row[span] = [[_int(p[0]), _int(p[1])] for p in (entry.get(span) or [])]
        rows.append(row)
    rows.sort(key=lambda r: (r["used_pct"] or 0), reverse=True)
    limit = _limit(args, 100, 500)
    return {"span": span, "hosts": hosts, "total_rows": len(rows),
            "truncated": len(rows) > limit, "rows": rows[:limit]}


_METRICS = ("rx", "tx", "cpu", "mem", "latency")


def tool_history(monitor, args: dict) -> dict:
    """Time series (network rate, CPU, RAM, poll latency) for the last N samples."""
    metric = str(args.get("metric") or "rx").lower()
    if metric not in _METRICS:
        raise ValueError(f"metric must be one of: {', '.join(_METRICS)}")
    points = args.get("points")
    try:
        points = int(points) if points is not None else 120
    except (TypeError, ValueError):
        points = 120
    points = max(1, min(points, 300))
    hosts = _pick_hosts(monitor, args)
    series = {}
    unit = ""
    for name in hosts:
        if metric in ("rx", "tx"):
            items = list(monitor.history.get(name) or [])
            idx = 1 if metric == "rx" else 2
            pairs = [(p[0], p[idx]) for p in items]
            unit = "bytes/s"
        elif metric == "cpu":
            items = list(monitor.cpu_history.get(name) or [])
            pairs = [(p[0], p[1]) for p in items]
            unit = "percent of one core"
        elif metric == "mem":
            items = list(monitor.mem_history.get(name) or [])
            pairs = [(p[0], p[1]) for p in items]
            unit = "bytes used"
        else:
            items = list(monitor.latency.get(name) or [])
            pairs = [(p[0], p[1]) for p in items]
            unit = "milliseconds"
        pairs = pairs[-points:]
        series[name] = {"ts": [_int(p[0]) for p in pairs],
                        "values": [_r(val, 2) for _, val in pairs]}
    return {"metric": metric, "unit": unit, "points": points, "series": series}


_TOOLS = [
    {
        "name": "netspy_overview",
        "description": ("Cluster summary: every monitored host with online state, agent version and "
                        "current rx/tx, plus all warnings the dashboard would show (offline host, failed "
                        "socket accounting, missing host access). Start here."),
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string", "description": "Optional single host name (default: all)"}}},
        "_fn": tool_overview,
    },
    {
        "name": "netspy_agent_health",
        "description": ("Per-host agent diagnostics: pid1, proc_count, ss status, host-access flag, docker "
                        "socket state, poll latency and agent-vs-web version mismatch. Use this when a host "
                        "shows no processes or suspicious numbers."),
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string", "description": "Optional single host name (default: all)"}}},
        "_fn": tool_agent_health,
    },
    {
        "name": "netspy_processes",
        "description": ("Top processes/containers merged across hosts. kind=rate (network bytes/s, default), "
                        "cpu (percent of one core), mem (bytes) or disk (read/write bytes/s)."),
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["rate", "cpu", "mem", "disk"], "default": "rate"},
            "host": {"type": "string", "description": "Optional host filter"},
            "limit": {"type": "integer", "default": 20, "maximum": 200},
            "min_rate_bps": {"type": "number", "default": 0,
                             "description": "Only for kind=rate: drop rows below this total rate"}}},
        "_fn": tool_processes,
    },
    {
        "name": "netspy_peers",
        "description": ("Which peers/connections are visible: rows whose process name carries a remote "
                        "address (e.g. smbd[192.168.2.99]) or that hold open sockets, with connection counts."),
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string"}, "search": {"type": "string", "description": "Substring filter on the process name"},
            "limit": {"type": "integer", "default": 25, "maximum": 200}}},
        "_fn": tool_peers,
    },
    {
        "name": "netspy_containers",
        "description": "Docker containers with their traffic (veth sums) and the per-host docker socket status.",
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string"}, "limit": {"type": "integer", "default": 30, "maximum": 300}}},
        "_fn": tool_containers,
    },
    {
        "name": "netspy_interfaces",
        "description": ("Interfaces per host with the uplink flag - use it to check whether a rate is real "
                        "traffic (uplink) or a virtual interface sum."),
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string"}, "limit": {"type": "integer", "default": 60, "maximum": 300}}},
        "_fn": tool_interfaces,
    },
    {
        "name": "netspy_storage",
        "description": ("Pool/dataset fill levels. span=now (default) or with recorded history: h24 (hourly, "
                        "24 h), d7 (daily, 7 days), m1 (daily, 32 days), m (month-end, 12 months)."),
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string"},
            "span": {"type": "string", "enum": ["now", "h24", "d7", "m1", "m"], "default": "now"},
            "limit": {"type": "integer", "default": 100, "maximum": 500}}},
        "_fn": tool_storage,
    },
    {
        "name": "netspy_history",
        "description": ("Time series from the in-memory ring buffers: metric=rx|tx (bytes/s), cpu (percent of "
                        "one core), mem (bytes), latency (ms). Returns up to 300 newest samples."),
        "inputSchema": {"type": "object", "properties": {
            "metric": {"type": "string", "enum": list(_METRICS), "default": "rx"},
            "host": {"type": "string"},
            "points": {"type": "integer", "default": 120, "maximum": 300}}},
        "_fn": tool_history,
    },
]
_TOOL_MAP = {t["name"]: t for t in _TOOLS}


def public_tools() -> list:
    """Tool descriptors as the MCP `tools/list` result wants them (no Python callables)."""
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in _TOOLS]


# --------------------------------------------------------------------------
# JSON-RPC dispatch
# --------------------------------------------------------------------------
def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _text_result(payload: dict, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error}


def dispatch(monitor, request: dict):
    """Handle one JSON-RPC request. Returns a response dict, or None for notifications."""
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return _error(request.get("id") if isinstance(request, dict) else None, -32600, "invalid request")

    method = request["method"]
    params = request.get("params") or {}
    req_id = request.get("id")
    is_notification = "id" not in request

    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        wanted = str(params.get("protocolVersion") or "")
        version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            "instructions": ("Read-only NetSpy access: overview, agent health, top processes, peers, "
                             "containers, interfaces, storage fill levels and time series. "
                             "No tool changes anything."),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": public_tools()}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        tool = _TOOL_MAP.get(name)
        if tool is None:
            return _error(req_id, -32602, f"unknown tool {name!r}; available: {', '.join(_TOOL_MAP)}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(req_id, -32602, "arguments must be an object")
        started = time.perf_counter()
        try:
            payload = tool["_fn"](monitor, args)
        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": req_id, "result": _text_result({"error": str(exc)}, True)}
        except Exception as exc:  # noqa: BLE001 - never let a tool kill the listener
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _text_result({"error": f"{type(exc).__name__}: {exc}"}, True)}
        elapsed = (time.perf_counter() - started) * 1000.0
        print(f"[mcp] {name}: {len(json.dumps(payload))} bytes in {elapsed:.0f} ms", flush=True)
        return {"jsonrpc": "2.0", "id": req_id, "result": _text_result(payload)}

    if is_notification:
        return None
    return _error(req_id, -32601, f"method not found: {method}")


def handle_payload(monitor, payload):
    """Dispatch a single request or a batch. Returns (body_or_None, status)."""
    if isinstance(payload, list):
        if not payload:
            return _error(None, -32600, "empty batch"), 200
        responses = [r for r in (dispatch(monitor, item) for item in payload) if r is not None]
        if not responses:
            return None, 202
        return responses, 200
    response = dispatch(monitor, payload)
    if response is None:
        return None, 202
    return response, 200


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
class McpServer(ThreadingHTTPServer):
    """HTTP server carrying the Monitor and the expected token."""

    daemon_threads = True
    monitor: object
    mcp_token: str


class McpHandler(BaseHTTPRequestHandler):
    server_version = "NetSpyMCP/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, format, *args):  # noqa: A002 - silence the default access log
        return

    def _token_from_headers(self) -> str:
        header = self.headers.get("X-MCP-Token") or ""
        if not header:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                header = auth[7:]
        return header.strip()

    def _authorized(self) -> bool:
        expected = getattr(self.server, "mcp_token", "") or ""
        given = self._token_from_headers()
        if not expected or not given:
            return False
        return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))

    def _send(self, status: int, body: bytes | None, content_type: str = "application/json"):
        self.send_response(status)
        if body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload, content_type: str = "application/json"):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), content_type)

    @staticmethod
    def _path(raw: str) -> str:
        return raw.split("?", 1)[0].rstrip("/") or "/"

    # -- verbs ------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = self._path(self.path)
        if path == HEALTH_PATH:
            # Unauthenticated on purpose: no data, just liveness for a healthcheck.
            self._json(200, {"ok": True, "server": SERVER_NAME, "version": VERSION,
                             "tools": len(_TOOLS), "auth": "X-MCP-Token"})
            return
        if path in (MCP_PATH, "/"):
            # No SSE stream offered: the spec allows 405 here.
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"error": "this MCP endpoint accepts POST /mcp (JSON-RPC); no SSE stream"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": "not found"})

    def do_OPTIONS(self):  # noqa: N802
        # No CORS support - keep browsers out of this port.
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        path = self._path(self.path)
        if path not in (MCP_PATH, "/"):
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "missing or invalid MCP token (header X-MCP-Token)"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return
        if length <= 0:
            self._json(400, {"error": "empty body"})
            return
        if length > MAX_BODY:
            self._json(413, {"error": f"body too large (max {MAX_BODY} bytes)"})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": f"parse error: {exc}"}})
            return
        body, status = handle_payload(self.server.monitor, payload)
        if body is None:
            self._send(status, None)
            return
        self._json(status, body)


def start_mcp(monitor, port: int = DEFAULT_PORT, token: str = "") -> ThreadingHTTPServer | None:
    """Start the read-only MCP listener. Returns None when no token is configured."""
    controller = McpController(monitor, port)
    controller.apply(bool(token), token)
    return controller.server


class McpController:
    """Owns the optional MCP listener and can start/stop it at runtime.

    The Settings tab flips `enabled` and the token while the dashboard keeps
    running, so binding the port once at startup is not enough: apply() brings
    the listener in line with the wish (start / stop / restart with a new
    token) and never raises - a busy port is reported, not thrown.
    """

    def __init__(self, monitor, port: int = DEFAULT_PORT):
        self.monitor = monitor
        self.port = port
        self._server: McpServer | None = None
        self._token = ""
        self.lock = threading.Lock()

    @property
    def server(self) -> McpServer | None:
        return self._server

    @property
    def running(self) -> bool:
        return self._server is not None

    def status(self) -> dict:
        return {"running": self.running, "port": self.port,
                "tools": len(_TOOLS), "path": MCP_PATH, "error": ""}

    def apply(self, enabled: bool, token: str) -> dict:
        """Start, stop or restart so that (enabled, token) is in effect."""
        token = (token or "").strip()
        want = bool(enabled and token)
        with self.lock:
            if self._server is not None and (not want or token != self._token):
                self._stop()
            if want and self._server is None:
                try:
                    server = McpServer(("0.0.0.0", self.port), McpHandler)
                except OSError as exc:
                    return {**self.status(),
                            "error": f"cannot bind port {self.port}: {exc}"}
                server.monitor = self.monitor
                server.mcp_token = token
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self._server = server
                self._token = token
                print(f"[mcp] read-only MCP on :{self.port}{MCP_PATH} "
                      f"({len(_TOOLS)} tools, token required)", flush=True)
            elif self._server is None and not want:
                reason = ("no token configured" if not token
                          else "switched off in the settings")
                print(f"[mcp] disabled - {reason}", flush=True)
            return self.status()

    def stop(self) -> dict:
        with self.lock:
            self._stop()
        return self.status()

    def _stop(self) -> None:
        server, self._server, self._token = self._server, None, ""
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass
        print("[mcp] listener stopped", flush=True)
