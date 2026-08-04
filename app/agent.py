#!/usr/bin/env python3
"""NetMon-Agent: per-interface und per-process Netzwerkraten.

Datenquellen:
  - /proc/net/dev          -> Interface-Zaehler (kumulativ, 64-bit)
  - `ss -tinp` (inet_diag) -> kumulative Byte-Zaehler pro TCP-Socket inkl. PID
  - /proc/<pid>/comm       -> Prozessname
  - /proc/<pid>/ns/net     -> Zuordnung Prozess -> Container (via Docker-Socket)

Voraussetzungen: network_mode: host, pid: host, laeuft als root (uid 0).
Optional: /var/run/docker.sock (read-only) fuer Container-Namen.

Reine Standardbibliothek, keine Dependencies.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROC = "/proc"

# Fallback-Reihenfolge fuer "Uplink"-Interfaces (wenn UPLINK nicht gesetzt)
UPLINK_FALLBACK = ["br0", "bond0", "eth0", "enp", "ens", "eno", "eth"]

# ss -tinp Parsing
_SS_USERS_RE = re.compile(r'users:\(\(("?)([^,)]+),pid=(\d+),fd=\d+')
_SS_ACKED_RE = re.compile(r'bytes_acked:(\d+)')
_SS_RECV_RE = re.compile(r'bytes_received:(\d+)')

# Beispiel-Ausgabe von `ss -tinp` fuer den Parser-Selbsttest
_SS_FIXTURE = (
    "ESTAB  0      0       10.10.10.101:445     10.10.10.50:50423  "
    'users:(("smbd",pid=1234,fd=22))\n'
    "\t cubic wscale:7,7 rto:204 rtt:0.2/0.05 ato:40 mss:1460 pmtu:1500 "
    "cwnd:10 ssthresh:7 bytes_sent:123456 bytes_acked:120000 "
    "bytes_received:987654 segs_out:1034 segs_in:987\n"
    "ESTAB  0      0       10.10.10.101:443     10.10.10.60:40000  "
    'users:(("nginx",pid=777,fd=9))\n'
    "\t cubic wscale:7,7 rto:10 rtt:1/0.5 ato:40 mss:1460 cwnd:10 "
    "bytes_acked:5000 bytes_received:10000\n"
    "LISTEN 0      128     0.0.0.0:8091          0.0.0.0:*  "
    'users:(("python",pid=42,fd=3))\n'
)


def parse_ss(output: str) -> dict:
    """Parsed `ss -tinp` Ausgabe -> {pid: [rx_bytes, tx_bytes]} (kumulativ).

    Nur TCP-Sockets mit zugeordnetem PID. rx = bytes_received,
    tx = bytes_acked (vom Peer bestaetigt = tatsaechlich raus).
    """
    res: dict = {}
    cur: int | None = None
    for line in output.splitlines():
        m = _SS_USERS_RE.search(line)
        if m:
            cur = int(m.group(3))
            res.setdefault(cur, [0, 0])
            continue
        if cur is None:
            continue
        a = _SS_ACKED_RE.search(line)
        r = _SS_RECV_RE.search(line)
        if a:
            res[cur][1] = int(a.group(1))
        if r:
            res[cur][0] = int(r.group(1))
    return res


def read_net_dev() -> dict:
    """/proc/net/dev -> {iface: (rx_bytes, tx_bytes)} kumulativ."""
    dev = {}
    with open(f"{PROC}/net/dev") as f:
        f.readline()
        f.readline()
        for line in f:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            fields = rest.split()
            dev[iface.strip()] = (int(fields[0]), int(fields[8]))
    return dev


class Sampler:
    """Sampled 1x/Sekunde Interface- und Prozessraten."""

    def __init__(self, uplink: str = "", docker_sock: str = ""):
        self.uplink_cfg = [u.strip() for u in uplink.split(",") if u.strip()]
        self.docker_sock = docker_sock or ""
        self.lock = threading.Lock()

        self._prev_iface: dict = {}
        self._prev_ss: dict = {}
        self._comm: dict = {}
        self._ns: dict = {}
        self._containers: dict = {}
        self._ns_ts = 0.0
        self._last_mono = 0.0
        self._ss_error: str | None = None
        self._last: dict | None = None

    # ------------------------------------------------------------------
    # Hilfen
    # ------------------------------------------------------------------
    def comm_for(self, pid: int) -> str:
        if pid not in self._comm:
            try:
                with open(f"{PROC}/{pid}/comm") as f:
                    self._comm[pid] = f.read().strip()[:40] or f"pid{pid}"
            except OSError:
                self._comm[pid] = f"pid{pid}"
        return self._comm[pid]

    def netns_of(self, pid: int) -> int:
        if pid not in self._ns:
            try:
                self._ns[pid] = os.stat(f"{PROC}/{pid}/ns/net").st_ino
            except OSError:
                self._ns[pid] = 0
        return self._ns[pid]

    def _is_uplink(self, name: str, ifaces: dict) -> bool:
        if self.uplink_cfg:
            return any(name == u or name.startswith(u) for u in self.uplink_cfg)
        # Auto: br0 (LAN-Bridge mit allem) -> bond0 -> erstes eth*
        if "br0" in ifaces:
            return name == "br0"
        if "bond0" in ifaces:
            return name == "bond0"
        return bool(re.match(r"^(eth|enp|ens|eno)\d", name))

    # ------------------------------------------------------------------
    # Docker-Socket (optional): Prozess -> Container-Name
    # ------------------------------------------------------------------
    def _docker_get(self, path: str):
        if not self.docker_sock or not os.path.exists(self.docker_sock):
            return None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(self.docker_sock)
            s.sendall(
                f"GET {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n".encode()
            )
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            s.close()
            body = buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""
            return json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return None

    def refresh_containers(self) -> None:
        """Netzwerk-Namespace des Hauptprozesses je Container -> Container-Name."""
        if not self.docker_sock or not os.path.exists(self.docker_sock):
            return
        if time.time() - self._ns_ts < 15:
            return
        self._ns_ts = time.time()
        try:
            containers = self._docker_get("/containers/json") or []
            ns_map = {}
            for c in containers:
                cid = c.get("Id", "")
                name = (c.get("Names") or ["?"])[0].lstrip("/")
                info = self._docker_get(f"/containers/{cid}/json") if cid else None
                if not info:
                    continue
                pid = (info.get("State") or {}).get("Pid") or 0
                if pid <= 0:
                    continue
                try:
                    st = os.stat(f"{PROC}/{pid}/ns/net")
                    ns_map[st.st_ino] = name
                except OSError:
                    continue
            if ns_map:
                self._containers = ns_map
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------
    def _run_ss(self):
        try:
            out = subprocess.run(
                ["ss", "-tinp"], capture_output=True, text=True, timeout=3
            )
            if out.returncode != 0:
                self._ss_error = (out.stderr or f"ss exit {out.returncode}").strip()
                return None
            self._ss_error = None
            return parse_ss(out.stdout)
        except FileNotFoundError:
            self._ss_error = "ss/iproute2 nicht installiert"
            return None
        except Exception as e:  # pragma: no cover
            self._ss_error = str(e)
            return None

    def tick(self) -> None:
        mono = time.monotonic()
        dt = mono - self._last_mono if self._last_mono else 0.0
        self._last_mono = mono

        ifaces = read_net_dev()
        ss = self._run_ss()
        self.refresh_containers()

        # --- Interface-Raten ---
        iface_rates = {}
        for name, (rx, tx) in ifaces.items():
            if name == "lo":
                continue
            prev = self._prev_iface.get(name)
            if prev is not None and dt > 0:
                iface_rates[name] = {
                    "rx": max(0.0, (rx - prev[0]) / dt),
                    "tx": max(0.0, (tx - prev[1]) / dt),
                    "uplink": self._is_uplink(name, ifaces),
                }
            else:
                iface_rates[name] = {"rx": 0.0, "tx": 0.0, "uplink": self._is_uplink(name, ifaces)}
        self._prev_iface = ifaces

        # --- Per-Prozess-Raten (TCP) ---
        procs: dict = {}
        if ss is not None:
            for pid, (rx, tx) in ss.items():
                prev = self._prev_ss.get(pid)
                if prev is None or dt <= 0:
                    continue
                drx = max(0.0, (rx - prev[0]) / dt)
                dtx = max(0.0, (tx - prev[1]) / dt)
                if drx <= 0 and dtx <= 0:
                    continue
                name = self.comm_for(pid)
                entry = procs.setdefault(name, {"rx": 0.0, "tx": 0.0})
                entry["rx"] += drx
                entry["tx"] += dtx
                ns = self.netns_of(pid)
                if ns:
                    cont = self._containers.get(ns)
                    if cont:
                        entry["container"] = cont
            self._prev_ss = ss

            # Caches auf lebende PIDs begrenzen
            alive = set(self._prev_ss.keys())
            self._comm = {k: v for k, v in self._comm.items() if k in alive}
            self._ns = {k: v for k, v in self._ns.items() if k in alive}

        # --- Summen + Rest (Kernel/UDP/ungenau) ---
        totals = {"rx": 0.0, "tx": 0.0}
        for name, rate in iface_rates.items():
            if rate["uplink"]:
                totals["rx"] += rate["rx"]
                totals["tx"] += rate["tx"]

        proc_rx = sum(p["rx"] for p in procs.values())
        proc_tx = sum(p["tx"] for p in procs.values())
        rest = {
            "rx": max(0.0, totals["rx"] - proc_rx),
            "tx": max(0.0, totals["tx"] - proc_tx),
        }

        procs_list = [
            {"name": n, "rx": round(v["rx"], 1), "tx": round(v["tx"], 1),
             "container": v.get("container")}
            for n, v in procs.items()
        ]
        procs_list.sort(key=lambda p: p["rx"] + p["tx"], reverse=True)

        iface_list = [
            {"name": n, "rx": round(v["rx"], 1), "tx": round(v["tx"], 1),
             "uplink": v["uplink"]}
            for n, v in sorted(iface_rates.items(),
                               key=lambda kv: kv[1]["rx"] + kv[1]["tx"], reverse=True)
        ]

        with self.lock:
            self._last = {
                "hostname": socket.gethostname(),
                "ts": time.time(),
                "totals": {k: round(v, 1) for k, v in totals.items()},
                "rest": {k: round(v, 1) for k, v in rest.items()},
                "interfaces": iface_list,
                "processes": procs_list,
                "ss_error": self._ss_error,
                "ss_ok": ss is not None,
            }

    def snapshot(self) -> dict:
        with self.lock:
            if self._last is None:
                return {
                    "hostname": socket.gethostname(),
                    "ts": time.time(),
                    "totals": {"rx": 0.0, "tx": 0.0},
                    "rest": {"rx": 0.0, "tx": 0.0},
                    "interfaces": [],
                    "processes": [],
                    "ss_error": self._ss_error,
                    "ss_ok": False,
                }
            return dict(self._last)


def sampler_loop(sampler: Sampler, interval: float = 1.0) -> None:
    while True:
        t0 = time.monotonic()
        try:
            sampler.tick()
        except Exception:
            pass
        time.sleep(max(0.05, interval - (time.monotonic() - t0)))


# ----------------------------------------------------------------------
# Agent-HTTP-API
# ----------------------------------------------------------------------
class AgentHandler(BaseHTTPRequestHandler):
    server_version = "NetMonAgent/1.0"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        token = getattr(self.server, "token", "")
        if token and self.headers.get("X-Agent-Token") != token:
            self._send(403, {"error": "forbidden"})
            return
        if self.path.split("?")[0] == "/api/metrics":
            self._send(200, self.server.sampler.snapshot())
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args):  # still
        pass


def start_agent(sampler: Sampler, port: int = 8091, token: str = "") -> ThreadingHTTPServer:
    threading.Thread(target=sampler_loop, args=(sampler,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), AgentHandler)
    server.daemon_threads = True
    server.sampler = sampler
    server.token = token
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _self_test() -> None:
    """Parser-Selbsttest mit Fixture."""
    parsed = parse_ss(_SS_FIXTURE)
    assert parsed.get(1234) == [987654, 120000], parsed
    assert parsed.get(777) == [10000, 5000], parsed
    assert parsed.get(42) == [0, 0], parsed  # LISTEN ohne Zaehler
    print("parse_ss Selbsttest OK:", parsed)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _self_test()
        sys.exit(0)
    s = Sampler(uplink=os.environ.get("UPLINK", ""),
                docker_sock=os.environ.get("DOCKER_SOCK", ""))
    print("Agent startet auf :8091 (Ctrl-C zum Beenden)")
    start_agent(s, port=8091, token=os.environ.get("AGENT_TOKEN", ""))
    while True:
        time.sleep(3600)
