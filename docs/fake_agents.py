#!/usr/bin/env python3
"""Throwaway fake agents for the README screenshot — serves synthetic metrics
with generic names (no real hosts, IPs or containers)."""
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAMES = {19091: "Main", 19092: "Backup"}
PROCS = ["nginx", "postgres", "plex", "smbd", "node", "python", "java", "redis", "rsync", "sshd"]
CONTS = ["app-web", "database", "media", "cache"]


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        t = time.time()
        port = self.server.server_address[1]
        base = 1.4e6 if port == 19091 else 0.45e6
        procs = []
        for i, p in enumerate(PROCS):
            r = max(0.0, base * (0.55 + 0.35 * math.sin(t / 30 + i * 1.3)) * (1.0 if i % 3 else 0.45))
            tx = r * (0.25 + 0.1 * math.sin(t / 45 + i * 0.7))
            if r > 2e3:
                procs.append({"name": p, "rx": round(r, 1), "tx": round(tx, 1),
                              "container": CONTS[i % 4] if i % 2 == 0 else None})
        procs.sort(key=lambda p: p["rx"] + p["tx"], reverse=True)
        totals = {"rx": base, "tx": base * 0.28}
        body = json.dumps({
            "version": "0.3.22", "hostname": NAMES[port], "ts": t,
            "totals": totals,
            "rest": {"rx": totals["rx"] * 0.14, "tx": totals["tx"] * 0.3},
            "interfaces": [
                {"name": "br0", "rx": base, "tx": base * 0.28, "uplink": True, "container": None},
                {"name": "veth8f2a", "rx": procs[0]["rx"] if procs else 0, "tx": procs[0]["tx"] if procs else 0,
                 "uplink": False, "container": "app-web"},
                {"name": "docker0", "rx": base * 0.05, "tx": base * 0.02, "uplink": False, "container": None},
            ],
            "processes": procs,
            "containers": [{"name": c, "rx": base * 0.12, "tx": base * 0.05} for c in CONTS],
            "ss_error": None, "ss_ok": True, "pid1": "systemd", "proc_count": 184,
            "docker": {"socket": True, "containers": 12, "macs": 12, "fdb": 25, "veths_mapped": 4},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    for port in (19091, 19092):
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"fake agent {NAMES[port]} auf :{port}")
    print("Fake-Agents laufen (Ctrl-C zum Beenden)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
