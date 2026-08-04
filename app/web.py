#!/usr/bin/env python3
"""NetSpy-Web: dashboard server (polling, ring buffer, static frontend).

ROLE=web: serves the web UI (:8090), polls local/remote agents and
optionally exposes an agent API (:8091) when a local sampler is active
(SERVERS contains "X=local").

Configuration (env):
  SERVERS     "Unraid=local;TrueNAS=http://10.10.10.20:8091"  (example IPs)
  UPLINK      "br0"                (comma-separated, empty = auto)
  DOCKER_SOCK "/var/run/docker.sock"  (empty = disabled)
  AGENT_TOKEN ""                   (optional, shared token)
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent import Sampler, start_agent

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def load_version() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, "VERSION"), os.path.join(base, "..", "VERSION")):
        try:
            with open(p) as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return "dev"


VERSION = load_version()


def parse_servers(spec: str) -> list:
    """'Unraid=local;TrueNAS=http://host:8091' -> [{'name','url'|None}, ...]"""
    out = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, url = part.split("=", 1)
            name, url = name.strip(), url.strip()
        else:
            name, url = part, "local"
        out.append({"name": name or url, "url": None if url == "local" else url})
    return out


class Monitor:
    """Pollt alle Server (1/s), haelt History + letzte Snapshots."""

    def __init__(self, servers: list, uplink: str = "", docker_sock: str = "",
                 token: str = ""):
        self.servers = servers
        self.token = token
        self.lock = threading.Lock()
        self.history: dict = {}
        self.snaps: dict = {}
        self.online: dict = {}
        self.errors: dict = {}
        self.sampler: Sampler | None = None

        for s in servers:
            name = s["name"]
            self.history[name] = deque(maxlen=3600)  # 1 h bei 1 s
            self.snaps[name] = None
            self.online[name] = False
            self.errors[name] = ""
            if s["url"] is None:
                self.sampler = Sampler(uplink=uplink, docker_sock=docker_sock)

    def poll_loop(self) -> None:
        """Pollt alle Server PARALLEL (ThreadPool) — skaliert auf N Hosts,
        ein langsamer Agent blockiert die anderen nicht."""
        workers = max(4, len(self.servers))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            while True:
                def poll_one(s):
                    try:
                        if s["url"] is None:
                            snap = self.sampler.snapshot() if self.sampler else None
                        else:
                            snap = self._fetch(s["url"])
                        if snap is None:
                            raise RuntimeError("kein lokaler Sampler")
                        return s["name"], snap, None
                    except Exception as e:
                        return s["name"], None, str(e)

                for name, snap, err in ex.map(poll_one, self.servers):
                    with self.lock:
                        if snap is not None:
                            self.snaps[name] = snap
                            self.online[name] = True
                            self.errors[name] = ""
                            self.history[name].append(
                                (snap["ts"], snap["totals"]["rx"], snap["totals"]["tx"])
                            )
                        else:
                            self.online[name] = False
                            self.errors[name] = err or ""
                time.sleep(1.0)

    def _fetch(self, url: str) -> dict:
        req = urllib.request.Request(url.rstrip("/") + "/api/metrics")
        if self.token:
            req.add_header("X-Agent-Token", self.token)
        with urllib.request.urlopen(req, timeout=2.5) as r:
            return json.loads(r.read().decode())

    # ------------------------------------------------------------------
    def dashboard(self) -> dict:
        with self.lock:
            series = {}
            for name, h in self.history.items():
                series[name] = {
                    "ts": [p[0] for p in h],
                    "rx": [p[1] for p in h],
                    "tx": [p[2] for p in h],
                }

            # Merge process list across all servers (rows = process/container)
            table: dict = {}
            for s in self.servers:
                name = s["name"]
                snap = self.snaps.get(name)
                if not snap:
                    continue
                for p in snap.get("processes", []):
                    row = table.setdefault(p["name"], {"hosts": {}, "kind": "proc"})
                    row["hosts"][name] = {"rx": p["rx"], "tx": p["tx"]}
                    if p.get("container"):
                        row.setdefault("container", p["container"])
                # Container-Zeilen (veth-Summen, z. B. Bridge-Container auf Unraid)
                for c in snap.get("containers", []):
                    row = table.setdefault(c["name"], {"hosts": {}, "kind": "container"})
                    row["hosts"][name] = {"rx": c["rx"], "tx": c["tx"]}
                rest = snap.get("rest") or {"rx": 0.0, "tx": 0.0}
                if rest["rx"] > 0 or rest["tx"] > 0:
                    key = "- not assigned (kernel/UDP) -"
                    row = table.setdefault(key, {"hosts": {}, "kind": "rest"})
                    row["hosts"][name] = {"rx": rest["rx"], "tx": rest["tx"]}

            rows = []
            for pname, v in table.items():
                total = sum(x["rx"] + x["tx"] for x in v["hosts"].values())
                rows.append({"name": pname, "kind": v.get("kind", "proc"),
                             "container": v.get("container"),
                             "total": total, "hosts": v["hosts"]})
            rows.sort(key=lambda r: r["total"], reverse=True)

            return {
                "version": VERSION,
                "servers": [
                    {
                        "name": s["name"],
                        "online": self.online.get(s["name"], False),
                        "error": self.errors.get(s["name"], ""),
                        "hostname": (self.snaps.get(s["name"]) or {}).get("hostname", ""),
                        "version": (self.snaps.get(s["name"]) or {}).get("version", ""),
                        "totals": (self.snaps.get(s["name"]) or {}).get("totals",
                                                                        {"rx": 0.0, "tx": 0.0}),
                    }
                    for s in self.servers
                ],
                "series": series,
                "ifaces": {
                    s["name"]: (self.snaps.get(s["name"]) or {}).get("interfaces", [])
                    for s in self.servers
                },
                "table": rows,
                "ts": time.time(),
            }


class WebHandler(BaseHTTPRequestHandler):
    server_version = "NetMonWeb/1.0"

    def _send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/dashboard":
            body = json.dumps(self.server.monitor.dashboard()).encode()
            self._send_bytes(200, body, "application/json")
            return
        if path in ("/", "/index.html"):
            path = "/index.html"
        if path.startswith("/") and not path.startswith("//"):
            fname = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
            if fname.startswith(STATIC_DIR) and os.path.isfile(fname):
                with open(fname, "rb") as f:
                    body = f.read()
                ctype = MIME.get(os.path.splitext(fname)[1], "application/octet-stream")
                # Cache-Busting: Versionsnummer in index.html einsetzen
                if fname.endswith("index.html"):
                    body = body.decode("utf-8").replace("@@VERSION@@", VERSION).encode("utf-8")
                self._send_bytes(200, body, ctype)
                return
        self._send_bytes(404, b"not found", "text/plain")

    def log_message(self, *args):  # still
        pass


def start_web(monitor: Monitor, port: int = 8090) -> ThreadingHTTPServer:
    threading.Thread(target=monitor.poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), WebHandler)
    server.daemon_threads = True
    server.monitor = monitor
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    servers = parse_servers(os.environ.get("SERVERS", "Unraid=local"))
    token = os.environ.get("AGENT_TOKEN", "")
    mon = Monitor(
        servers,
        uplink=os.environ.get("UPLINK", ""),
        docker_sock=os.environ.get("DOCKER_SOCK", "/var/run/docker.sock"),
        token=token,
    )
    start_web(mon, port=int(os.environ.get("WEB_PORT", "8090")))

    # Web-Rolle stellt zusaetzlich eine Agent-API bereit, wenn lokal gesampelt wird
    if mon.sampler is not None:
        start_agent(mon.sampler, port=int(os.environ.get("AGENT_PORT", "8091")), token=token)

    print(f"NetMon-Web auf :{os.environ.get('WEB_PORT', '8090')} "
          f"-> Server: {[s['name'] for s in servers]}")
    while True:
        time.sleep(3600)
