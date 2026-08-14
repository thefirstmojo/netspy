"""Erzeugt die Repo-Screenshots mit FAKE-Daten (Route-Interception).

WICHTIG: NUR Dummy-Daten verwenden — niemals echte Server-/Prozessnamen,
echte IPs oder echte Messwerte in oeffentliche Repo-Screenshots!

Nutzung: lokale NetSpy-Instanz laufen lassen, URL als Argument (oder env NETSPY_URL,
Default http://127.0.0.1:8090), dann dieses Skript ausfuehren:
  python3 tools/make_fake_shots.py http://192.168.2.101:8090
Die API-Daten werden IMMER durch Dummy-Daten ersetzt (Route-Interception)."""
import json, math, os, sys, time
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NETSPY_URL", "http://127.0.0.1:8090")

# Version aus der VERSION-Datei des Repos (Screenshots zeigen den aktuellen Stand)
try:
    with open(os.path.join(os.path.dirname(__file__), "..", "VERSION")) as f:
        VERSION = f.read().strip()
except OSError:
    VERSION = "0.0.0"

NOW = time.time()
TS = [NOW - (300 - i) * 2.5 for i in range(121)]  # 5 min

def wave(base, amp, i, phase=0.0):
    return max(0.0, base + amp * math.sin(i / 12.0 + phase) + (i % 37) * 3)

SERVERS = [
    {"name": "Main", "url": None, "online": True, "error": "",
     "hostname": "mainhost", "version": VERSION,
     "totals": {"rx": 420.0, "tx": 180.0}},
    {"name": "Remote", "url": "http://10.10.10.20:8091", "online": True, "error": "",
     "hostname": "remotehost", "version": VERSION,
     "totals": {"rx": 95.0, "tx": 40.0}},
]

def proc(name, cont, rx, tx, conns, server="Main"):
    return {"name": name, "container": cont,
            "hosts": {server: {"rx": rx, "tx": tx, "conns": conns}}}

TABLE = [
    proc("nginx", "web", 240.0, 110.0, 34),
    proc("postgres", "db", 60.0, 12.0, 12),
    proc("docker-proxy:5432", "db", 55.0, 10.0, 9),
    proc("redis-server", "cache", 8.0, 4.0, 3),
    proc("smbd[10.0.0.5]", None, 21.0, 9.0, 2, "Remote"),
    proc("node", "worker", 15.0, 7.0, 5),
    proc("python3", None, 4.0, 2.0, 1),
    proc("sshd", None, 1.2, 0.8, 1),
]

TABLE2 = [
    proc("nginx", "web", 18.0, 41.0, 3, "Remote"),
    proc("postgres", "db", 2.0, 1.0, 2, "Remote"),
    proc("smbd[10.0.0.7]", None, 55.0, 12.0, 4, "Remote"),
]

def diskrow(name, cont, r, w, server="Main"):
    return {"name": name, "container": cont,
            "hosts": {server: {"read": r, "write": w}}}

DISK = [
    diskrow("tdarr", "media", 52000.0, 31000.0),
    diskrow("ffmpeg", None, 41000.0, 600.0),
    diskrow("postgres", "db", 3100.0, 2200.0),
    diskrow("smbd[10.0.0.5]", None, 1500.0, 300.0, "Remote"),
    diskrow("paperless", "docs", 800.0, 150.0),
    diskrow("nginx", "web", 60.0, 40.0),
    diskrow("docker-proxy:5432", "db", 2100.0, 1600.0),
]

SYSROWS = [
    {"name": "tdarr", "container": "media", "cpu": 41.2, "mem": 780 * 1024 * 1024},
    {"name": "ffmpeg", "container": None, "cpu": 87.5, "mem": 120 * 1024 * 1024},
    {"name": "postgres", "container": "db", "cpu": 3.4, "mem": 640 * 1024 * 1024},
    {"name": "nginx", "container": "web", "cpu": 1.1, "mem": 85 * 1024 * 1024},
    {"name": "python3", "container": None, "cpu": 0.8, "mem": 60 * 1024 * 1024},
]

FAKE = {
    "version": VERSION,
    "servers": SERVERS,
    "series": {
        s["name"]: {"ts": TS,
                    "rx": [wave(300, 180, i) * 1024 for i in range(121)],
                    "tx": [wave(120, 80, i, 2.0) * 1024 for i in range(121)]}
        for s in SERVERS
    },
    "ifaces": {"Main": ["br0", "eth0"], "Remote": ["bond0"]},
    "table": TABLE + TABLE2,
    "disk": DISK,
    "system": SYSROWS,
    "host_sys": {
        "Main": {"cpu": 12.4, "mem_total": 32 * 1024**3, "mem_used": 18.2 * 1024**3},
        "Remote": {"cpu": 6.1, "mem_total": 16 * 1024**3, "mem_used": 9.5 * 1024**3},
    },
    "latency": {
        "Main": {"ts": TS, "ms": [2 + (i % 5) for i in range(121)]},
        "Remote": {"ts": TS, "ms": [12 + (i % 9) for i in range(121)]},
    },
    "ts": NOW,
}

BODY = json.dumps(FAKE)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 950}, device_scale_factor=1.5)
    pg.route("**/api/dashboard", lambda route: route.fulfill(
        status=200, content_type="application/json", body=BODY))
    pg.route("**/api/prochistory", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"Main":{"ts":[],"rx":[],"tx":[]},"Remote":{"ts":[],"rx":[],"tx":[]}}'))
    pg.goto(BASE, timeout=20000)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(2500)
    pg.screenshot(path="docs/screenshot.png")
    print("Netzwerk-Screenshot OK")
    # Disk-Tab
    pg.click("#tabbtn-disk")
    pg.wait_for_timeout(1200)
    pg.screenshot(path="docs/screenshots/disk-tab.png")
    print("Disk-Screenshot OK")
    b.close()
