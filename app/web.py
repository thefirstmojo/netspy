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
  CONFIG_DIR  "/data"              (writable volume; servers.json lives here)

Server list: if <CONFIG_DIR>/servers.json exists (and is valid) it wins.
Otherwise the SERVERS env var is used (fallback — old composes keep
working without a volume). The settings UI writes servers.json and
shows a hint when the directory is not writable.
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
from shared import load_version

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


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


def config_path(config_dir: str) -> str:
    return os.path.join(config_dir, "servers.yaml")


def config_legacy_path(config_dir: str) -> str:
    return os.path.join(config_dir, "servers.json")


def config_writable(config_dir: str) -> bool:
    """Nur True, wenn <config_dir> ein GEMOUNTETES Volume ist UND wirklich
    beschrieben werden kann (Schreib-Test + Dateisystem-Vergleich).

    Ein Verzeichnis, das der Container selbst im Image-Layer anlegt, wuerde
    ein Container-Recreate/Update nicht ueberleben — Speichern dorthin waere
    nutzlos. Erkennung: gemountete Verzeichnisse liegen auf einem anderen
    Dateisystem (st_dev) als der Container-Root. Zusaetzlich wird ein echter
    Schreib-Test gemacht (faengt z. B. fehlende Rechte bei cap_drop: [ALL]
    ohne CAP_DAC_OVERRIDE auf fremden 755-Verzeichnissen)."""
    try:
        os.makedirs(config_dir, exist_ok=True, mode=0o777)
        if not os.access(config_dir, os.W_OK):
            return False
        root_dev = os.stat("/").st_dev
        if os.stat(config_dir).st_dev == root_dev:
            return False
        # Echter Schreib-Test (Datei anlegen + loeschen)
        probe = os.path.join(config_dir, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _valid_servers(data) -> list | None:
    """Normalisiert eine Server-Liste (dict-Liste) oder None wenn ungueltig.

    'local' wird zu url=None normalisiert (lokaler Sampler)."""
    if not (isinstance(data, list) and data):
        return None
    out = []
    for s in data:
        if not (isinstance(s, dict) and s.get("name")):
            return None
        url = s.get("url")
        if url is not None and str(url).strip().lower() == "local":
            url = None
        out.append({"name": str(s["name"]), "url": url})
    return out


def _read_file(path: str):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _config_servers_list(config_dir: str) -> list:
    """Valide Server aus der Config-Datei (yaml -> json Legacy) oder [].
    Ohne Env-Fallback — die Env-Server werden separat gehalten."""
    txt = _read_file(config_path(config_dir))
    if txt is not None:
        try:
            import yaml
            v = _valid_servers(yaml.safe_load(txt))
            if v:
                return v
        except Exception:
            pass
    txt = _read_file(config_legacy_path(config_dir))
    if txt is not None:
        try:
            v = _valid_servers(json.loads(txt))
            if v:
                return v
        except Exception:
            pass
    return []


def merge_servers(env_servers: list, config_servers: list) -> list:
    """Env-Server (Basis) + Config-Server (ergaenzen/ueberschreiben).

    Bei Namensgleichheit gewinnt der Config-Eintrag. Ist beides leer,
    wird ein lokaler Server ('Main=local') ergaenzt, damit das Dashboard
    immer starten kann."""
    merged: list = []
    names: set = set()
    for s in list(config_servers) + list(env_servers):
        if s["name"] in names:
            continue
        names.add(s["name"])
        merged.append(s)
    if not merged:
        merged = [{"name": "Main", "url": None}]
    return merged


def load_servers(env_servers: str, config_dir: str) -> list:
    """Kombinierte Server-Liste: Config-Datei gewinnt, Env ergaenzt.
    Ohne jede Angabe wird ein lokaler Server ('Main=local') ergaenzt."""
    return merge_servers(parse_servers(env_servers), _config_servers_list(config_dir))


def save_servers(servers: list, config_dir: str) -> str:
    """Speichert die Server-Liste atomar als menscheneditierbares
    servers.yaml. Wirft OSError, wenn der Pfad nicht schreibbar ist."""
    os.makedirs(config_dir, exist_ok=True, mode=0o777)
    path = config_path(config_dir)
    tmp = path + ".tmp"
    header = (
        "# NetSpy - Server configuration (written by the Settings tab, "
        "also human-editable)\n"
        "# url: 'local' = sample this host directly, "
        "or http://host:8091 = agent API of another host\n"
    )
    dumpable = [{"name": s["name"], "url": s.get("url") or "local"}
                for s in servers]
    try:
        import yaml
        body = yaml.safe_dump(dumpable, allow_unicode=True, sort_keys=False)
    except ImportError:
        body = json.dumps(dumpable, indent=2)
    with open(tmp, "w") as f:
        f.write(header + body)
    # Auch auf dem Host editierbar machen (Unraid: uid 99/users)
    os.chmod(tmp, 0o666)
    os.replace(tmp, path)
    return path


class Monitor:
    """Pollt alle Server (1/s), haelt History + letzte Snapshots."""

    def __init__(self, servers: list, uplink: str = "", docker_sock: str = "",
                 token: str = "", config_dir: str = "/data",
                 env_servers: list | None = None,
                 config_servers: list | None = None):
        self.servers = servers
        self.token = token
        self.config_dir = config_dir
        # Herkunft getrennt halten: Env-Server (Basis) + Config-Datei-Server
        self.env_servers: list = env_servers if env_servers is not None else list(servers)
        self.config_servers: list = config_servers if config_servers is not None else []
        self._uplink = uplink
        self._docker_sock = docker_sock
        self.lock = threading.Lock()
        self.history: dict = {}
        self.proc_history: dict = {}   # server -> {proc_name -> deque[(ts,rx,tx)]}
        self.proc_snaps: dict = {}     # server -> deque[(ts, [[name,cont,rx,tx],...])]
        self.latency: dict = {}        # server -> deque[(ts, ms)] (Poll-Antwortzeit)
        self.snaps: dict = {}
        self.online: dict = {}
        self.errors: dict = {}
        self.sampler: Sampler | None = None

        for s in servers:
            name = s["name"]
            self.history[name] = deque(maxlen=3600)  # 1 h bei 1 s
            self.proc_history[name] = {}
            self.proc_snaps[name] = deque(maxlen=300)  # Zeit-Snapshots fuer Hover-Tooltip
            self.latency[name] = deque(maxlen=300)
            self.snaps[name] = None
            self.online[name] = False
            self.errors[name] = ""
            if s["url"] is None:
                self.sampler = Sampler(uplink=uplink, docker_sock=docker_sock)

    def set_servers(self, servers: list) -> None:
        """Server-Liste zur Laufzeit ersetzen (Settings-UI).

        Neue Server werden initialisiert, entfernte aufgeraeumt — bestehende
        Strukturen (History etc.) bleiben erhalten. Falsche/offline Server
        fuehren nur zu leeren Feldern, nie zu einem Crash."""
        with self.lock:
            old = {s["name"] for s in self.servers}
            new = {s["name"] for s in servers}
            for s in servers:
                name = s["name"]
                if name not in self.history:
                    self.history[name] = deque(maxlen=3600)
                    self.proc_history[name] = {}
                    self.proc_snaps[name] = deque(maxlen=300)
                    self.latency[name] = deque(maxlen=300)
                self.snaps.setdefault(name, None)
                self.online.setdefault(name, False)
                self.errors.setdefault(name, "")
            for name in old - new:
                self.history.pop(name, None)
                self.proc_history.pop(name, None)
                self.proc_snaps.pop(name, None)
                self.latency.pop(name, None)
                self.snaps.pop(name, None)
                self.online.pop(name, None)
                self.errors.pop(name, None)
            self.servers = servers
            # Lokaler Sampler nachruesten, wenn ein lokaler Server hinzukam
            has_local = any(s.get("url") is None for s in servers)
            if has_local and self.sampler is None:
                self.sampler = Sampler(uplink=self._uplink,
                                       docker_sock=self._docker_sock)

    def _poll_one(self, s):
        """Fetch one server (used by poll_loop in parallel). Returns
        (name, snap, err, latency_ms)."""
        t0 = time.monotonic()
        try:
            if s["url"] is None:
                snap = self.sampler.snapshot() if self.sampler else None
            else:
                snap = self._fetch(s["url"])
            if snap is None:
                raise RuntimeError("no local sampler")
            return s["name"], snap, None, (time.monotonic() - t0) * 1000.0
        except Exception as e:
            return s["name"], None, str(e), None

    def poll_loop(self) -> None:
        """Pollt alle Server PARALLEL — skaliert auf N Hosts,
        ein langsamer Agent blockiert die anderen nicht."""
        workers = max(4, len(self.servers))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            while True:
                for name, snap, err, ms in ex.map(self._poll_one, list(self.servers)):
                    with self.lock:
                        if name not in self.latency:
                            # Server wurde inzwischen ueber die Settings
                            # entfernt (set_servers) — Rennbedingung abfangen
                            continue
                        if snap is not None:
                            self.snaps[name] = snap
                            self.online[name] = True
                            self.errors[name] = ""
                            if ms is not None:
                                self.latency[name].append((time.time(), ms))
                            self.history[name].append(
                                (snap["ts"], snap["totals"]["rx"], snap["totals"]["tx"])
                            )
                            # Per-Prozess-History (5 min Ring-Buffer pro Prozess)
                            ph = self.proc_history[name]
                            for p in snap.get("processes", []):
                                ph.setdefault(p["name"], deque(maxlen=300)).append(
                                    (snap["ts"], p["rx"], p["tx"])
                                )
                            # Zeit-Snapshots (alle Prozesse zu einem ts) fuer den
                            # eingefrorenen Hover-Tooltip
                            self.proc_snaps[name].append((
                                snap["ts"],
                                [(p.get("name", "?"), p.get("container"),
                                  p.get("rx", 0.0), p.get("tx", 0.0))
                                 for p in snap.get("processes", [])],
                            ))
                        else:
                            self.online[name] = False
                            self.errors[name] = err or ""
                            self.latency[name].append((time.time(), -1.0))
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
                items = list(h)[-300:]  # Frontend uses last 300 points
                series[name] = {
                    "ts": [p[0] for p in items],
                    "rx": [p[1] for p in items],
                    "tx": [p[2] for p in items],
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

            # Disk-I/O pro Prozess über alle Server (name x server, read/write)
            dtable: dict = {}
            for s in self.servers:
                name = s["name"]
                snap = self.snaps.get(name)
                if not snap:
                    continue
                for p in snap.get("disk", []):
                    row = dtable.setdefault(p["name"], {"hosts": {}})
                    row["hosts"][name] = {"read": p["read"], "write": p["write"]}
                    if p.get("container"):
                        row.setdefault("container", p["container"])

            drows = []
            for pname, v in dtable.items():
                total = sum(x["read"] + x["write"] for x in v["hosts"].values())
                drows.append({"name": pname, "container": v.get("container"),
                              "total": total, "hosts": v["hosts"]})
            drows.sort(key=lambda r: r["total"], reverse=True)

            # CPU/RAM pro Prozess (name x server)
            stable: dict = {}
            for s in self.servers:
                name = s["name"]
                snap = self.snaps.get(name)
                if not snap:
                    continue
                sysd = snap.get("system") or {}
                for p in sysd.get("procs", []):
                    row = stable.setdefault(p["name"], {"hosts": {}})
                    row["hosts"][name] = {"cpu": p["cpu"],
                                          "cpu10": p.get("cpu10", 0.0),
                                          "mem": p["mem"]}
                    if p.get("container"):
                        row.setdefault("container", p["container"])
            srows = []
            for pname, v in stable.items():
                total = sum(x["cpu"] for x in v["hosts"].values())
                srows.append({"name": pname, "container": v.get("container"),
                              "total": total, "hosts": v["hosts"]})
            srows.sort(key=lambda r: r["total"], reverse=True)

            host_sys = {
                s["name"]: (self.snaps.get(s["name"]) or {}).get("system", {})
                for s in self.servers
            }

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
                "disk": drows,
                "system": srows,
                "host_sys": host_sys,
                "latency": {
                    s["name"]: {
                        "ts": [p[0] for p in list(self.latency.get(s["name"], []))],
                        "ms": [p[1] for p in list(self.latency.get(s["name"], []))],
                    }
                    for s in self.servers
                },
                "ts": time.time(),
            }

    def process_history(self, server: str, proc: str) -> dict:
        """History eines Prozesses: {ts, rx, tx} (letzte 300 Punkte)."""
        with self.lock:
            ph = self.proc_history.get(server, {}).get(proc)
            if not ph:
                return {"ts": [], "rx": [], "tx": []}
            items = list(ph)
            return {
                "ts": [p[0] for p in items],
                "rx": [p[1] for p in items],
                "tx": [p[2] for p in items],
            }

    def prochistory(self) -> dict:
        """Zeit-Snapshots aller Prozesse pro Server (letzte 300 Punkte).

        Struktur: {server: [{ts, procs: [[name, container, rx, tx], ...]}, ...]}
        Fuer den eingefrorenen Hover-Tooltip, synchron zur Totals-History.
        """
        with self.lock:
            return {
                name: [
                    {"ts": ts, "procs": procs}
                    for (ts, procs) in list(self.proc_snaps.get(name, []))[-300:]
                ]
                for name in self.proc_snaps
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
        raw_path = self.path
        path = raw_path.split("?")[0]
        if path == "/api/dashboard":
            body = json.dumps(self.server.monitor.dashboard()).encode()
            self._send_bytes(200, body, "application/json")
            return
        if path == "/api/prochistory":
            body = json.dumps(self.server.monitor.prochistory()).encode()
            self._send_bytes(200, body, "application/json")
            return
        if path == "/api/settings":
            mon = self.server.monitor
            writable = config_writable(mon.config_dir)
            src = _config_source(mon.config_dir)
            cfg_path = config_path(mon.config_dir)
            has_template = (src == "env"
                            and os.path.exists(cfg_path))
            # Diagnose (fuer Fehlersuche): Dateisystem-IDs + Schreib-Test
            dev_root = dev_cfg = None
            try:
                dev_root = os.stat("/").st_dev
            except OSError:
                pass
            try:
                os.makedirs(mon.config_dir, exist_ok=True, mode=0o777)
                dev_cfg = os.stat(mon.config_dir).st_dev
            except OSError:
                pass
            # Herkunft pro Server: config-Zeilen zuerst, dann env (Dedupe)
            cfg_names = {s["name"] for s in mon.config_servers}
            servers_out = (
                [{"name": s["name"], "url": s.get("url"), "origin": "config"}
                 for s in mon.config_servers]
                + [{"name": s["name"], "url": s.get("url"), "origin": "env"}
                   for s in mon.env_servers if s["name"] not in cfg_names]
            )
            body = json.dumps({
                "path": cfg_path,
                "writable": writable,
                "source": src,
                "has_template": has_template,
                "diag": {"dev_root": dev_root, "dev_cfg": dev_cfg,
                         "mounted": dev_cfg is not None and dev_cfg != dev_root},
                "servers": servers_out,
            }).encode()
            self._send_bytes(200, body, "application/json")
            return
        if path == "/api/process_history":
            from urllib.parse import parse_qs, unquote
            q = parse_qs(raw_path.split("?", 1)[1]) if "?" in raw_path else {}
            server = unquote((q.get("server") or [""])[0])
            proc = unquote((q.get("proc") or [""])[0])
            body = json.dumps(self.server.monitor.process_history(server, proc)).encode()
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

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/settings":
            mon = self.server.monitor
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length).decode() or "{}")
            except (ValueError, OSError):
                self._send_bytes(400, b'{"error":"invalid body"}', "application/json")
                return
            servers = data.get("servers")
            if not isinstance(servers, list):
                self._send_bytes(400, b'{"error":"servers must be a list"}',
                                 "application/json")
                return
            cleaned = []
            for s in servers:
                if not isinstance(s, dict):
                    continue
                name = str(s.get("name") or "").strip()
                if not name:
                    continue
                url = s.get("url")
                cleaned.append({"name": name, "url": (url or None)})
            # Eine leere Liste ist erlaubt: die Config wird geleert, die
            # env-Server bleiben aktiv (merge_servers faellt auf sie zurueck).
            if not config_writable(mon.config_dir):
                hint = (
                    f"Config folder {mon.config_dir} is not a mounted volume "
                    "(saving there would not survive an update). Mount a "
                    "volume — in the docker-compose.yml of the NetSpy web "
                    "container add (Unraid example, create the directory first):\n\n"
                    "    volumes:\n"
                    "      - /mnt/user/appdata/netspy:/netspy\n\n"
                    "The /netspy/config subfolder (and later e.g. /netspy/data) "
                    "is created automatically by the container.\n"
                    "For other systems e.g.:\n"
                    "      - /opt/netspy:/netspy\n\n"
                    "Then recreate the container (docker compose up -d / "
                    "redeploy the stack). The server list is stored as "
                    "human-editable servers.yaml. Without a volume the "
                    "SERVERS environment variable keeps working (fallback)."
                )
                self._send_bytes(409, json.dumps({
                    "error": "config dir is not a mounted volume", "hint": hint
                }).encode(), "application/json")
                return
            try:
                path = save_servers(cleaned, mon.config_dir)
                # Env-Server bleiben unveraendert; die Datei enthaelt nur die
                # uebermittelten (config-)Zeilen
                mon.config_servers = cleaned
                mon.set_servers(merge_servers(mon.env_servers, cleaned))
            except OSError as e:
                self._send_bytes(500, json.dumps({
                    "error": f"save failed: {e}",
                    "hint": "Volume nicht schreibbar? Siehe /api/settings.",
                }).encode(), "application/json")
                return
            except Exception as e:  # noqa: BLE001 - defensiv: nie den Handler-Thread killen
                self._send_bytes(500, json.dumps({
                    "error": f"apply failed: {e}",
                }).encode(), "application/json")
                return
            self._send_bytes(200, json.dumps({"ok": True, "path": path}).encode(),
                             "application/json")
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


def resolve_config_dir(env_dir: str) -> str:
    """CONFIG_DIR-Env, sonst automatisch erkennen:
    - /netspy gemountet (neu, Unterordner-Struktur)  -> /netspy/config
    - /config gemountet (alte Composes)               -> /config
    - sonst Default /netspy/config (Warnung via UI).
    Gibt immer einen Pfad zurueck; das Verzeichnis wird bei Bedarf
    (config_writable/save_servers) automatisch angelegt."""
    if env_dir:
        return env_dir
    root_dev = 0
    try:
        root_dev = os.stat("/").st_dev
    except OSError:
        pass
    for cand in ("/netspy", "/config"):
        try:
            if os.path.isdir(cand) and os.stat(cand).st_dev != root_dev:
                return cand + "/config" if cand == "/netspy" else cand
        except OSError:
            continue
    return "/netspy/config"


TEMPLATE = """# NetSpy - Server configuration
# Managed by the NetSpy web container (Settings tab); also human-editable.
# Servers listed here win over the SERVERS environment variable.
#
# Format: a list of servers the dashboard monitors:
#   url: 'local'          = sample this host directly (web role)
#   url: http://host:8091 = agent API of another host (e.g. TrueNAS)
#
# Examples (commented out - add here or via the settings UI):
# - name: Main
#   url: local
# - name: Remote
#   url: http://10.10.10.20:8091
"""


def init_config_template(config_dir: str) -> bool:
    """Legt beim Start eine leere servers.yaml-Vorlage an — NUR wenn ein
    Volume gemountet ist (sonst wuerde sie im Container-Layer landen und
    ein Update nicht ueberleben). Rueckgabe: True wenn geschrieben.

    Eine reine Kommentar-Datei ist fuer load_servers ungueltig -> die
    SERVERS-Umgebungsvariable bleibt aktiv, bis echte Eintraege folgen."""
    if not config_writable(config_dir):
        return False
    path = config_path(config_dir)
    if os.path.exists(path):
        return False
    try:
        os.makedirs(config_dir, exist_ok=True, mode=0o777)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(TEMPLATE)
        os.chmod(tmp, 0o666)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _config_source(config_dir: str) -> str:
    """'file' wenn eine valide Server-Liste in yaml/json liegt, sonst 'env'."""
    txt = _read_file(config_path(config_dir))
    if txt is not None:
        try:
            import yaml
            if _valid_servers(yaml.safe_load(txt)):
                return "file"
        except Exception:
            pass
    txt = _read_file(config_legacy_path(config_dir))
    if txt is not None:
        try:
            if _valid_servers(json.loads(txt)):
                return "file"
        except Exception:
            pass
    return "env"


def main() -> None:
    config_dir = resolve_config_dir(os.environ.get("CONFIG_DIR", ""))
    env_servers = parse_servers(os.environ.get("SERVERS", ""))
    config_servers = _config_servers_list(config_dir)
    servers = merge_servers(env_servers, config_servers)
    token = os.environ.get("AGENT_TOKEN", "")
    mon = Monitor(
        servers,
        uplink=os.environ.get("UPLINK", ""),
        docker_sock=os.environ.get("DOCKER_SOCK", "/var/run/docker.sock"),
        token=token,
        config_dir=config_dir,
        env_servers=env_servers,
        config_servers=config_servers,
    )
    # Beim Start: leere servers.yaml-Vorlage anlegen (nur bei gemountetem
    # Volume) — sichtbarer Beweis auf dem Host, dass der Pfad korrekt ist.
    if init_config_template(config_dir):
        print(f"[config] Vorlage angelegt: {config_path(config_dir)}")
    start_web(mon, port=int(os.environ.get("WEB_PORT", "8090")))

    # Web-Rolle stellt zusaetzlich eine Agent-API bereit, wenn lokal gesampelt wird
    if mon.sampler is not None:
        start_agent(mon.sampler, port=int(os.environ.get("AGENT_PORT", "8091")), token=token)

    src = "file" if os.path.exists(config_path(config_dir)) else "env"
    print(f"NetMon-Web auf :{os.environ.get('WEB_PORT', '8090')} "
          f"-> Server: {[s['name'] for s in servers]} "
          f"(Config-Quelle: {src}, Pfad: {config_path(config_dir)})")
    while True:
        time.sleep(3600)
