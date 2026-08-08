#!/usr/bin/env python3
"""NetSpy-Agent: per-interface und per-process Netzwerkraten.

Datenquellen:
  - /proc/net/dev          -> Interface-Zaehler (kumulativ, 64-bit)
  - `ss -tinpe` (inet_diag) -> kumulative Byte-Zaehler pro TCP-Socket inkl.
    PID und Socket-Inode (stabiles Pro-Socket-Tracking, keine Fork-Spikes)
  - /proc/<pid>/comm       -> Prozessname
  - /proc/<pid>/ns/net     -> Zuordnung Prozess -> Container (via Docker-Socket)
  - `bridge fdb show`      -> Zuordnung veth-Interface -> Container (via MAC)

Voraussetzungen: network_mode: host, pid: host, laeuft als root (uid 0).
Optional: /var/run/docker.sock (read-only) fuer Container-Namen.

Glättung: Prozess-/Container-Raten laufen durch einen EMA (0.5) und werden
erst nach 6 s Inaktivität aus der Liste entfernt -> stabile, ruhige Anzeige.

Reine Standardbibliothek, keine Dependencies.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from shared import load_version

PROC = "/proc"


def parse_route_table(text: str) -> set:
    """Inhalt von /proc/net/route -> Interfaces mit aktiver IPv4-Default-Route.

    Nur Eintraege mit Destination 00000000 UND Flags UP|GATEWAY (0x3) zaehlen
    (lo hat z. B. nur UP -> wird ausgeschlossen).
    """
    out = set()
    for line in text.splitlines()[1:]:  # Kopfzeile ueberspringen
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "00000000" and (int(parts[3], 16) & 0x3) == 0x3:
            out.add(parts[0])
    return out

# ss -tinpe Parsing (e = extended: Socket-Inode für stabiles Pro-Socket-Tracking)
_SS_USERS_RE = re.compile(r'users:\(\(("?)([^,)]+),pid=(\d+),fd=\d+')
_SS_INO_RE = re.compile(r'\bino:(\d+)')
_SS_ACKED_RE = re.compile(r'bytes_acked:(\d+)')
_SS_RECV_RE = re.compile(r'bytes_received:(\d+)')

# Physikalische Obergrenze: nichts über 50 GB/s pro Socket ist real
# (10G-Link = 1,25 GB/s) -> schluckt Zähler-Artefakte
RATE_CAP = 50e9

# bridge fdb show: "00:11:22:33:44:55 dev vethXXX ..."
_FDB_RE = re.compile(r"^([0-9a-f:]{17})\s+dev\s+(\S+)")

# EMA-Gewicht (0.5 = neue Messung zur Haelfte)
EMA = 0.5
# Prozess bleibt nach letzter Aktivitaet noch N Sekunden in der Liste
DECAY_S = 6.0

# Beispiel-Ausgabe von `ss -tinpe` fuer den Parser-Selbsttest
_SS_FIXTURE = (
    "ESTAB  0      0       10.10.10.10:445     10.10.10.50:50423  "
    'users:(("smbd",pid=1234,fd=22)) uid:0 ino:11111 sk:1\n'
    "\t cubic wscale:7,7 rto:204 rtt:0.2/0.05 ato:40 mss:1460 pmtu:1500 "
    "cwnd:10 ssthresh:7 bytes_sent:123456 bytes_acked:120000 "
    "bytes_received:987654 segs_out:1034 segs_in:987\n"
    "ESTAB  0      0       10.10.10.10:443     10.10.10.60:40000  "
    'users:(("nginx",pid=777,fd=9)) uid:0 ino:22222 sk:2\n'
    "\t cubic wscale:7,7 rto:10 rtt:1/0.5 ato:40 mss:1460 cwnd:10 "
    "bytes_acked:5000 bytes_received:10000\n"
    "LISTEN 0      128     0.0.0.0:8091          0.0.0.0:*  "
    'users:(("python",pid=42,fd=3)) uid:0 ino:33333 sk:3\n'
)


def parse_ss(output: str) -> dict:
    """Parsed `ss -tinpe` -> {inode: {"pid", "rx", "tx"}} (kumulativ).

    Pro-Socket-Tracking über die Socket-Inode: Zähler bleiben stabil, auch
    wenn ein Socket zwischen Prozessen wandert (z. B. smbd Parent/Kind bei
    Fork) oder PIDs wiederverwendet werden. rx = bytes_received,
    tx = bytes_acked (vom Peer bestaetigt = tatsaechlich raus).
    """
    res: dict = {}
    cur: int | None = None
    for line in output.splitlines():
        m = _SS_USERS_RE.search(line)
        if m:
            ino_m = _SS_INO_RE.search(line)
            if not ino_m:
                cur = None
                continue
            cur = int(ino_m.group(1))
            # Lokalen Port extrahieren (4. Feld, z. B. "192.168.2.101:6379"
            # oder "[::1]:6379"): noetig, um beim docker-proxy die eingehende
            # Seite (lokaler Port == host-port) von der ausgehenden
            # Weiterleitung an den Container zu unterscheiden.
            lport = 0
            try:
                parts = line.split()
                if len(parts) >= 4:
                    lport = int(parts[3].rsplit(":", 1)[-1].rstrip("]"))
            except ValueError:
                lport = 0
            res.setdefault(cur, {"pid": int(m.group(3)), "rx": 0, "tx": 0, "lport": lport})
            continue
        if cur is None:
            continue
        a = _SS_ACKED_RE.search(line)
        r = _SS_RECV_RE.search(line)
        if a:
            res[cur]["tx"] = int(a.group(1))
        if r:
            res[cur]["rx"] = int(r.group(1))
    return res


def _decode_chunked(data: bytes) -> bytes:
    """Dekodiert HTTP Transfer-Encoding: chunked (Docker-Daemon streamt so)."""
    out = b""
    i = 0
    while i < len(data):
        j = data.find(b"\r\n", i)
        if j == -1:
            break
        size = int((data[i:j].split(b";")[0].strip() or b"0"), 16)
        if size == 0:
            break
        out += data[j + 2:j + 2 + size]
        i = j + 2 + size + 2  # Chunk-Daten + abschließendes CRLF überspringen
    return out


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
            if len(fields) < 9:
                continue
            dev[iface.strip()] = (int(fields[0]), int(fields[8]))
    return dev


class Sampler:
    """Sampled 1x/Sekunde Interface-, Container- und Prozessraten."""

    def __init__(self, uplink: str = "", docker_sock: str = ""):
        self.uplink_cfg = [u.strip() for u in uplink.split(",") if u.strip()]
        self.docker_sock = docker_sock or ""
        self.lock = threading.Lock()
        self._routes: set = set()
        self._route_ts = 0.0

        self._prev_iface: dict = {}
        self._prev_ss: dict = {}
        self._comm: dict = {}
        self._ns: dict = {}
        self._containers: dict = {}       # netns-inode -> container-name
        self._mac_containers: dict = {}   # mac -> container-name
        self._veth_containers: dict = {}  # veth-iface -> container-name
        self._port_containers: dict = {}  # host-port -> container-name (docker-proxy)
        self._proxy_port: dict = {}       # pid -> host-port (docker-proxy cmdline)
        self._proc_cont: dict = {}        # name -> container (persistent, kein Flackern)
        self._fdb_size = 0                # Anzahl gelesener FDB-Einträge
        self._ns_ts = 0.0
        self._last_mono = 0.0
        self._ss_error: str | None = None
        self._last: dict | None = None

        # Glättung / Stabilität
        self._ema: dict = {}      # name -> {"rx": float, "tx": float}
        self._active: dict = {}   # name -> monotonic timestamp
        self._ema_rest: dict | None = None

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

    def proxy_port_of(self, pid: int) -> int:
        """docker-proxy: Host-Port aus der cmdline (-host-port N).

        Damit laesst sich der Proxy exakt dem Container mit diesem
        Port-Mapping zuordnen (statt des Host-IP-Fallbacks)."""
        if pid not in self._proxy_port:
            port = 0
            try:
                with open(f"{PROC}/{pid}/cmdline", "rb") as f:
                    args = f.read().decode("utf-8", "replace").split("\0")
                for i, a in enumerate(args):
                    if a == "-host-port" and i + 1 < len(args):
                        port = int(args[i + 1])
                        break
            except (OSError, ValueError):
                port = 0
            self._proxy_port[pid] = port
        return self._proxy_port[pid]

    def _default_route_ifaces(self) -> set:
        """Interfaces mit IPv4-Default-Route (30 s gecacht)."""
        now = time.time()
        if self._route_ts and now - self._route_ts < 30:
            return self._routes
        routes = set()
        try:
            with open(f"{PROC}/net/route") as f:
                routes = parse_route_table(f.read())
        except OSError:
            pass
        self._routes, self._route_ts = routes, now
        return routes

    def _is_uplink(self, name: str, ifaces: dict) -> bool:
        if self.uplink_cfg:
            return any(name == u or name.startswith(u) for u in self.uplink_cfg)
        # Auto: Interfaces mit Default-Route (funktioniert ohne Konfiguration
        # auf Unraid/br0, Debian/eth0-enp*, Bonding/bond0, ...)
        routes = self._default_route_ifaces()
        if routes:
            return name in routes
        # Fallback: br0 (LAN-Bridge mit allem) -> bond0 -> erstes eth*
        if "br0" in ifaces:
            return name == "br0"
        if "bond0" in ifaces:
            return name == "bond0"
        return bool(re.match(r"^(eth|enp|ens|eno)\d", name))

    def _pid1_comm(self) -> str:
        try:
            with open(f"{PROC}/1/comm") as f:
                return f.read().strip()[:40]
        except OSError:
            return "?"

    def _proc_count(self) -> int:
        try:
            return sum(1 for e in os.listdir(PROC) if e.isdigit())
        except OSError:
            return 0

    # ------------------------------------------------------------------
    # Docker-Socket (optional): Prozess/Container-Zuordnung
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
            head, _, body = buf.partition(b"\r\n\r\n")
            # Docker-Daemon liefert grosse Antworten chunked -> dekodieren
            if b"transfer-encoding: chunked" in head.lower():
                body = _decode_chunked(body)
            return json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return None

    def _run_fdb(self) -> dict:
        """bridge fdb show -> {mac: veth-interface}. Leer wenn nicht verfügbar."""
        try:
            out = subprocess.run(
                ["bridge", "fdb", "show"], capture_output=True, text=True, timeout=3
            )
            if out.returncode != 0:
                return {}
            res = {}
            for line in out.stdout.splitlines():
                m = _FDB_RE.match(line.strip())
                if m:
                    res.setdefault(m.group(1).lower(), m.group(2))
            return res
        except (FileNotFoundError, Exception):
            return {}

    def refresh_containers(self) -> None:
        """Container-Zuordnungen (max. alle 15 s):
        - netns-inode des Hauptprozesses -> Container (Prozess-Zuordnung)
        - Container-MAC (Docker-API) + bridge fdb -> veth-Interface -> Container
        """
        if not self.docker_sock or not os.path.exists(self.docker_sock):
            self._containers = {}
            self._mac_containers = {}
            self._veth_containers = {}
            self._port_containers = {}
            return
        if time.time() - self._ns_ts < 15:
            return
        self._ns_ts = time.time()
        try:
            containers = self._docker_get("/containers/json") or []
            ns_map = {}
            mac_map = {}
            port_map = {}
            for c in containers:
                cid = c.get("Id", "")
                name = (c.get("Names") or ["?"])[0].lstrip("/")
                # Host-Port -> Container (fuer docker-proxy-Zuordnung)
                for p in c.get("Ports") or []:
                    pub = p.get("PublicPort")
                    if pub:
                        port_map.setdefault(pub, name)
                info = self._docker_get(f"/containers/{cid}/json") if cid else None
                if not info:
                    continue
                pid = (info.get("State") or {}).get("Pid") or 0
                if pid > 0:
                    try:
                        st = os.stat(f"{PROC}/{pid}/ns/net")
                        ns_map[st.st_ino] = name
                    except OSError:
                        pass
                nets = (info.get("NetworkSettings") or {}).get("Networks") or {}
                for net in nets.values():
                    mac = (net or {}).get("MacAddress")
                    if mac:
                        mac_map[mac.lower()] = name
            if ns_map:
                self._containers = ns_map
            if mac_map:
                self._mac_containers = mac_map
            if port_map:
                self._port_containers = port_map
            # veth -> Container über FDB (MAC ist auf dem Bridge-Port gelernt)
            fdb = self._run_fdb()
            self._fdb_size = len(fdb)
            vc = {}
            for mac, veth in fdb.items():
                cname = mac_map.get(mac)
                if cname:
                    vc[veth] = cname
            self._veth_containers = vc
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------
    def _run_ss(self):
        try:
            out = subprocess.run(
                ["ss", "-tinpe"], capture_output=True, text=True, timeout=3
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

        # --- Interface-Raten (inkl. veth->Container) ---
        iface_rates = {}
        for name, (rx, tx) in ifaces.items():
            if name == "lo":
                continue
            prev = self._prev_iface.get(name)
            if prev is not None and dt > 0:
                rate = {
                    "rx": max(0.0, (rx - prev[0]) / dt),
                    "tx": max(0.0, (tx - prev[1]) / dt),
                    "uplink": self._is_uplink(name, ifaces),
                    "container": self._veth_containers.get(name),
                }
            else:
                rate = {"rx": 0.0, "tx": 0.0, "uplink": self._is_uplink(name, ifaces),
                        "container": self._veth_containers.get(name)}
            iface_rates[name] = rate
        self._prev_iface = ifaces

        # --- Container-Raten = Summe ihrer veth-Interfaces ---
        containers_raw = {}
        for name, rate in iface_rates.items():
            cname = rate.get("container")
            if cname:
                e = containers_raw.setdefault(cname, [0.0, 0.0])
                e[0] += rate["rx"]
                e[1] += rate["tx"]

        # --- Summen (Uplink) — vor den Prozessen, für den physikalischen Cap ---
        totals = {"rx": 0.0, "tx": 0.0}
        for name, rate in iface_rates.items():
            if rate["uplink"]:
                totals["rx"] += rate["rx"]
                totals["tx"] += rate["tx"]

        # --- Per-Prozess-Raten (TCP, roh) ---
        # Pro-Socket-Tracking über Inodes: verhindert Spikes, wenn ein Socket
        # zwischen Prozessen wandert (z. B. smbd Parent/Kind bei Fork).
        # Zusätzlich dynamischer physikalischer Cap: kein Prozess kann schneller
        # sein als die Gesamt-Uplink-Rate des Hosts in derselben Sekunde.
        link_cap = totals["rx"] + totals["tx"]
        raw: dict = {}
        # Persistente Zuordnungen uebernehmen: ein einmal zugeordneter
        # docker-proxy behaelt sein Badge, auch wenn der Socket kurz pausiert.
        proc_cont: dict = dict(self._proc_cont)
        if ss is not None:
            for ino, s in ss.items():
                prev = self._prev_ss.get(ino)
                if prev is None or dt <= 0:
                    continue
                drx = max(0.0, (s["rx"] - prev["rx"]) / dt)
                dtx = max(0.0, (s["tx"] - prev["tx"]) / dt)
                # Zähler-Artefakt-Guard: physikalisch unmögliche Raten
                if drx > RATE_CAP:
                    drx = 0.0
                if dtx > RATE_CAP:
                    dtx = 0.0
                if link_cap > 0:
                    drx = min(drx, link_cap)
                    dtx = min(dtx, link_cap)
                # Pro-Socket-Klemme auf die Einzelrichtung: ein Prozess kann nie
                # mehr empfangen als der Host insgesamt empfängt (bzw. senden
                # als der Host sendet). Faengt Zaehler-Spruenge bei rotierenden
                # Verbindungen (z. B. docker-proxy-Verbindungspools).
                if totals["rx"] > 0:
                    drx = min(drx, totals["rx"])
                if totals["tx"] > 0:
                    dtx = min(dtx, totals["tx"])
                if drx <= 0 and dtx <= 0:
                    continue
                pid = s["pid"]
                name = self.comm_for(pid)
                # docker-proxy: exakte Zuordnung ueber den gemappten Host-Port.
                # Jeder Port bekommt eine eigene Zeile (docker-proxy:<port>) mit
                # dem echten Container statt des Host-IP-Fallbacks (host networking).
                if name == "docker-proxy":
                    port = self.proxy_port_of(pid)
                    cname = self._port_containers.get(port) if port else None
                    if cname:
                        name = f"docker-proxy:{port}"
                        self._proc_cont[name] = cname
                        proc_cont[name] = cname
                    # Doppelzaehlung vermeiden: Der docker-proxy hat pro Verbindung
                    # ZWEI Sockets (eingehend am host-port + ausgehende Weiterleitung
                    # an die Container-IP). Beide tragen dieselben Bytes — nur die
                    # eingehende Seite (lokaler Port == host-port) zaehlen.
                    if port and s.get("lport") and s["lport"] != port:
                        continue
                e = raw.setdefault(name, [0.0, 0.0])
                e[0] += drx
                e[1] += dtx
                ns = self.netns_of(pid)
                if ns:
                    cname = self._containers.get(ns)
                    if cname:
                        proc_cont.setdefault(name, cname)
            self._prev_ss = ss

            # Caches auf lebende PIDs begrenzen
            alive = set(s["pid"] for s in ss.values())
            self._comm = {k: v for k, v in self._comm.items() if k in alive}
            self._ns = {k: v for k, v in self._ns.items() if k in alive}
            self._proxy_port = {k: v for k, v in self._proxy_port.items() if k in alive}

        # Artefakt-Schutz: Die Summe aller Prozess-Raten darf die physikalische
        # Interface-Rate nicht uebersteigen. Falls doch (Socket-Zaehler-Spruenge
        # bei rotierenden Verbindungen o. a.), proportional klemmen — der
        # Ueberschuss bleibt dann korrekt im "Rest" (kernel/CIFS) statt einem
        # Prozess zugeschlagen zu werden.
        if raw:
            prx = sum(e[0] for e in raw.values())
            ptx = sum(e[1] for e in raw.values())
            if prx > totals["rx"] > 0:
                sc = totals["rx"] / prx
                for e in raw.values():
                    e[0] *= sc
            if ptx > totals["tx"] > 0:
                sc = totals["tx"] / ptx
                for e in raw.values():
                    e[1] *= sc

        # --- EMA-Glättung + Aktivitäts-Decay für Prozesse ---
        for name, (r, t) in raw.items():
            old = self._ema.get(name)
            if old:
                self._ema[name] = {
                    "rx": EMA * r + (1 - EMA) * old["rx"],
                    "tx": EMA * t + (1 - EMA) * old["tx"],
                }
            else:
                self._ema[name] = {"rx": r, "tx": t}
            self._active[name] = mono

        emitted = {}
        for name, ema in self._ema.items():
            last = self._active.get(name, 0.0)
            # Laenger als DECAY_S ohne neue Messwerte: Zeile entfernen.
            # (Ohne diese Grenze bliebe die letzte Rate fuer immer stehen,
            # weil die EMA ohne neue Daten nie aktualisiert wird.)
            if (mono - last) >= DECAY_S:
                continue
            # Keine Messung in diesem Tick: Rate klingt exponentiell Richtung 0
            # ab (faellt sichtbar, statt mit der letzten Rate stehen zu bleiben).
            if name not in raw:
                ema = {"rx": ema["rx"] * (1 - EMA), "tx": ema["tx"] * (1 - EMA)}
            # EMA-Nachlauf an aktuelle Host-Rate klemmen: ein Prozess wird
            # nie schneller angezeigt als der Host insgesamt empfängt/sendet
            if link_cap > 0:
                emitted[name] = {
                    "rx": min(ema["rx"], link_cap),
                    "tx": min(ema["tx"], link_cap),
                }
            else:
                emitted[name] = ema
        self._ema = {k: v for k, v in self._ema.items() if k in emitted}
        self._active = {k: v for k, v in self._active.items() if k in emitted}
        # Zuordnungs-Cache an lebende Zeilen klemmen (neu geprueft beim Wiederauftauchen)
        self._proc_cont = {k: v for k, v in self._proc_cont.items() if k in emitted}

        procs_list = [
            {"name": n, "rx": round(v["rx"], 1), "tx": round(v["tx"], 1),
             "container": proc_cont.get(n)}
            for n, v in emitted.items()
        ]
        procs_list.sort(key=lambda p: p["rx"] + p["tx"], reverse=True)

        containers_list = [
            {"name": n, "rx": round(v[0], 1), "tx": round(v[1], 1)}
            for n, v in containers_raw.items()
        ]
        containers_list.sort(key=lambda c: c["rx"] + c["tx"], reverse=True)

        # --- Rest (Kernel/UDP/ungenau), ebenfalls geglättet ---
        proc_rx = sum(p["rx"] for p in procs_list)
        proc_tx = sum(p["tx"] for p in procs_list)
        cont_rx = sum(c["rx"] for c in containers_list)
        cont_tx = sum(c["tx"] for c in containers_list)
        rest_raw = {
            "rx": max(0.0, totals["rx"] - proc_rx - cont_rx),
            "tx": max(0.0, totals["tx"] - proc_tx - cont_tx),
        }
        if self._ema_rest is None:
            self._ema_rest = rest_raw
        else:
            self._ema_rest = {
                "rx": EMA * rest_raw["rx"] + (1 - EMA) * self._ema_rest["rx"],
                "tx": EMA * rest_raw["tx"] + (1 - EMA) * self._ema_rest["tx"],
            }
        rest = {k: round(min(v, totals[k]), 1) for k, v in self._ema_rest.items()}

        iface_list = [
            {"name": n, "rx": round(v["rx"], 1), "tx": round(v["tx"], 1),
             "uplink": v["uplink"], "container": v["container"]}
            for n, v in sorted(iface_rates.items(),
                               key=lambda kv: kv[1]["rx"] + kv[1]["tx"], reverse=True)
        ]

        with self.lock:
            self._last = {
                "version": load_version(),
                "hostname": socket.gethostname(),
                "ts": time.time(),
                "totals": {k: round(v, 1) for k, v in totals.items()},
                "rest": rest,
                "interfaces": iface_list,
                "processes": procs_list,
                "containers": containers_list,
                "ss_error": self._ss_error,
                "ss_ok": ss is not None,
                # Diagnose: pid1 != Container-Init -> pid:host aktiv
                "pid1": self._pid1_comm(),
                "proc_count": self._proc_count(),
                # Diagnose Docker-Zuordnung (Container-Zeilen)
                "docker": {
                    "socket": bool(self.docker_sock)
                    and os.path.exists(self.docker_sock),
                    "containers": len(self._containers),
                    "macs": len(self._mac_containers),
                    "fdb": self._fdb_size,
                    "veths_mapped": len(self._veth_containers),
                },
            }

    def snapshot(self) -> dict:
        with self.lock:
            if self._last is None:
                return {
                    "version": load_version(),
                    "hostname": socket.gethostname(),
                    "ts": time.time(),
                    "totals": {"rx": 0.0, "tx": 0.0},
                    "rest": {"rx": 0.0, "tx": 0.0},
                    "interfaces": [],
                    "processes": [],
                    "containers": [],
                    "ss_error": self._ss_error,
                    "ss_ok": False,
                    "pid1": self._pid1_comm(),
                    "proc_count": self._proc_count(),
                    "docker": {"socket": False, "containers": 0, "macs": 0,
                               "fdb": 0, "veths_mapped": 0},
                }
            return dict(self._last)


def sampler_loop(sampler: Sampler, interval: float = 1.0) -> None:
    while True:
        t0 = time.monotonic()
        try:
            sampler.tick()
        except Exception as e:
            print(f"[sampler] tick error: {e}", file=sys.stderr)
        time.sleep(max(0.05, interval - (time.monotonic() - t0)))


# ----------------------------------------------------------------------
# Agent-HTTP-API
# ----------------------------------------------------------------------
class AgentHandler(BaseHTTPRequestHandler):
    server_version = "NetSpyAgent/1.0"

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
    assert parsed.get(11111) == {"pid": 1234, "rx": 987654, "tx": 120000}, parsed
    assert parsed.get(22222) == {"pid": 777, "rx": 10000, "tx": 5000}, parsed
    assert parsed.get(33333) == {"pid": 42, "rx": 0, "tx": 0}, parsed

    # Default-Route-Erkennung: br0+bond0 (Default, UP|GATEWAY) erkannt,
    # lo (nur UP) und eth0 (Subnetz-Route) ausgeschlossen
    route_fix = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "br0\t00000000\t0102A8C0\t0003\t0\t0\t0\t00000000\t1500\t0\t0\n"
        "eth0\t0102A8C0\t00000000\t0007\t0\t0\t0\t00FFFFFF\t1500\t0\t0\n"
        "lo\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t65536\t0\t0\n"
        "bond0\t00000000\t0102A8C0\t0003\t0\t0\t0\t00000000\t1500\t0\t0\n"
    )
    assert parse_route_table(route_fix) == {"br0", "bond0"}, parse_route_table(route_fix)
    print("parse_ss self-test OK (per-socket inodes):", parsed)


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
