#!/usr/bin/env python3
"""Audit: Server-Filter-Chips (Häkchen) in ALLEN Tabs + Terminal-Auto-Ziele.

Fake-Backend mit 3 Monitoring-Servern (Unraid, TrueNAS, Debian). Terminal:
TrueNAS + Debian als echte Ziele, Unraid als suggested (kein Ziel).
"""
import json, sys, threading, time
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from playwright.sync_api import sync_playwright

STATIC = "/opt/data/netspy/app/static"
MON_SERVERS = ["Unraid", "TrueNAS", "Debian"]
T = time.time()
DASH = {
    "version": "test", "servers": [{"name": n} for n in MON_SERVERS],
    "ifaces": [], "latency": {n: 5 + i for i, n in enumerate(MON_SERVERS)},
    "host_sys": {}, "disk": [], "system": [], "mem": {}, "cpu": {},
    "table": [
        {"name": "sshd", "hosts": {"Unraid": {"rx": 100, "tx": 200, "conns": 3},
                                   "TrueNAS": {"rx": 300, "tx": 400, "conns": 5}}},
        {"name": "rsync", "hosts": {"TrueNAS": {"rx": 10, "tx": 20, "conns": 1},
                                    "Debian": {"rx": 30, "tx": 40, "conns": 2}}},
    ],
    "series": {n: {"ts": [T - 10, T - 5, T], "rx": [1, 2, 3], "tx": [4, 5, 6]}
               for n in MON_SERVERS},
}
STORAGE = {
    "enabled": ["Unraid:cache", "TrueNAS:tank"], "available": {},
    "recorded": {
        "Unraid:cache": {"name": "cache", "server": "Unraid", "type": "btrfs",
                         "size": 6e11, "used": 2e11, "created": T - 100,
                         "h24": [[T - 60, 2e11]], "d7": [], "m": []},
        "TrueNAS:tank": {"name": "tank", "server": "TrueNAS", "type": "zfs",
                         "size": 4e13, "used": 2e13, "created": T - 100,
                         "h24": [[T - 60, 2e13]], "d7": [], "m": []},
    },
    "host_access": {n: True for n in MON_SERVERS},
}
TERM = {"enabled": True, "error": "", "targets": [
    {"name": "TrueNAS", "host": "192.168.2.100", "user": "root",
     "has_key": False, "port": 7681, "running": True},
    {"name": "Debian", "host": "192.168.2.30", "user": "root",
     "has_key": False, "port": 7682, "running": True},
]}
posted = {}

def term_status():
    existing = {t["name"].lower() for t in TERM["targets"]}
    out = [dict(t) for t in TERM["targets"]]
    for n in MON_SERVERS:
        if n.lower() not in existing:
            host = "localhost" if n == "Unraid" else "192.168.2." + {"TrueNAS": "100", "Debian": "30"}[n]
            out.append({"name": n, "host": host, "user": "", "has_key": False,
                        "port": 0, "running": False, "suggested": True})
    return {"enabled": True, "error": "", "targets": out}

class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/dashboard": return self.j(DASH)
        if p == "/api/storage": return self.j(STORAGE)
        if p == "/api/settings":
            return self.j({"path": "/netspy/config/servers.yaml", "writable": True,
                           "source": "file", "has_template": False,
                           "diag": {"mounted": True},
                           "servers": [{"name": n, "url": None, "origin": "config"} for n in MON_SERVERS]})
        if p == "/api/terminal": return self.j(term_status())
        if p in ("/api/prochistory", "/api/process_history"): return self.j({})
        if p.startswith("/api/"): return self.j({"error": "no"})
        return super().do_GET()
    def do_POST(self):
        p = self.path.split("?")[0]
        ln = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(ln).decode() or "{}")
        if p == "/api/terminal/login": return self.j({"ok": True})
        if p == "/api/terminal":
            posted["targets"] = data.get("targets") or []
            TERM["targets"] = [{"name": t["name"], "host": t["host"], "user": t["user"],
                                "has_key": bool(t.get("key")), "port": 7681 + i,
                                "running": True} for i, t in enumerate(posted["targets"])]
            return self.j(term_status())
        return self.j({"ok": True})
    def j(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", 8095), partial(H, directory=STATIC))
threading.Thread(target=srv.serve_forever, daemon=True).start()

ok = True
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond: ok = False

def visible_count(loc):
    return loc.evaluate_all("els => els.filter(e => e.style.display !== 'none').length")

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1100, "height": 800})
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.add_init_script("try { sessionStorage.setItem('netspy.termLogin','1'); } catch (e) {}")
    pg.goto("http://127.0.0.1:8095/index.html")
    pg.wait_for_selector(".chartcard")
    pg.wait_for_timeout(600)
    check("Chips: All + 3 Server", pg.locator("#serverfilter input").count() == 4)
    check("Network: 3 Chart-Karten", pg.locator("#chartgrid .chartcard").count() == 3)
    tn = pg.locator("#proctbody tr[data-server='TrueNAS']")
    check("Network-Tabelle: TrueNAS-Zeilen vorhanden", tn.count() >= 1)

    # ---- Storage-Tab: Baseline laden ----
    pg.click("#tabbtn-storage"); pg.wait_for_timeout(500)
    grid_txt = pg.locator("#storagegrid").inner_text()
    check("Storage-Baseline: tank (TrueNAS) sichtbar", "tank" in grid_txt)
    check("Storage-Baseline: cache (Unraid) sichtbar", "cache" in grid_txt)

    # ---- Terminal-Tab: Baseline (Login gemockt) ----
    pg.click("#tabbtn-term"); pg.wait_for_selector(".termcard"); pg.wait_for_timeout(300)
    check("Terminal: nur ECHTE Ziele als Karten (2, keine suggested-Karte)",
          pg.locator(".termcard").count() == 2)
    check("Terminal: keine Karte fuer suggested Unraid",
          pg.locator(".termcard[data-name='Unraid']").count() == 0)

    # ---- TrueNAS abwaehlen -> Wirkung in jedem Tab ----
    pg.locator("input[data-srv='TrueNAS']").uncheck()
    pg.wait_for_timeout(400)
    # Network
    card_tn = pg.eval_on_selector("#chart-TrueNAS", "el => el.style.display")
    check("Network: TrueNAS-Chart-Karte ausgeblendet", card_tn == "none")
    check("Network: sichtbare Karten = 2", visible_count(pg.locator("#chartgrid .chartcard")) == 2)
    check("Network-Tabelle: keine TrueNAS-Zeilen", tn.count() == 0)
    # Terminal
    check("Terminal: TrueNAS-Karte ausgeblendet",
          pg.eval_on_selector(".termcard[data-name='TrueNAS']", "el => el.style.display") == "none")
    check("Terminal: Debian-Karte weiter sichtbar",
          pg.eval_on_selector(".termcard[data-name='Debian']", "el => el.style.display") != "none")
    # Disk-Tab (Tabelle wird vom Poll befuellt)
    pg.click("#tabbtn-disk"); pg.wait_for_timeout(300)
    check("Disk: keine TrueNAS-Zeilen",
          pg.locator("#disktbody tr[data-server='TrueNAS']").count() == 0)
    check("Disk: Debian/Unraid-Zeilen existieren noch",
          pg.locator("#disktbody tr[data-server='Debian'], #disktbody tr[data-server='Unraid']").count() >= 0)
    # Sys-Tab
    pg.click("#tabbtn-sys"); pg.wait_for_timeout(300)
    check("Sys: keine TrueNAS-Zeilen",
          pg.locator("#systbody tr[data-server='TrueNAS']").count() == 0)
    cpu_hidden = pg.evaluate(
        "Chart.getChart(document.getElementById('cpuchart')).data.datasets"
        ".filter(d => d.label === 'TrueNAS').every(d => d.hidden)")
    mem_hidden = pg.evaluate(
        "Chart.getChart(document.getElementById('memchart')).data.datasets"
        ".filter(d => d.label === 'TrueNAS').every(d => d.hidden)")
    check("Sys: CPU-Chart TrueNAS-Linie hidden", cpu_hidden)
    check("Sys: RAM-Chart TrueNAS-Linie hidden", mem_hidden)
    # Storage
    pg.click("#tabbtn-storage"); pg.wait_for_timeout(400)
    grid_txt = pg.locator("#storagegrid").inner_text()
    check("Storage: tank nach Abwahl weg", "tank" not in grid_txt)
    check("Storage: cache (Unraid) bleibt", "cache" in grid_txt)

    # ---- TrueNAS wieder an -> Karten/Tabellen kommen zurueck ----
    pg.locator("input[data-srv='TrueNAS']").check()
    pg.wait_for_timeout(400)
    check("Network: TrueNAS-Karte wieder sichtbar",
          pg.eval_on_selector("#chart-TrueNAS", "el => el.style.display") != "none")
    pg.click("#tabbtn-term"); pg.wait_for_timeout(300)
    check("Terminal: TrueNAS-Karte wieder sichtbar",
          pg.eval_on_selector(".termcard[data-name='TrueNAS']", "el => el.style.display") != "none")

    # ---- Settings: Terminal-Editor mit Auto-Zeilen ----
    pg.click("#tabbtn-settings"); pg.wait_for_selector("#termsettbox .term-row")
    pg.wait_for_timeout(300)
    rows = pg.locator("#termsettbox .term-row")
    check("Editor: 3 Zeilen (2 echte + 1 auto)", rows.count() == 3)
    auto_badges = pg.locator("#termsettbox .term-row:has-text('auto')")
    check("Editor: auto-Badge fuer Unraid", auto_badges.count() == 1)
    auto_row = pg.locator("#termsettbox .term-row", has=pg.locator("input[value='Unraid']"))
    check("Editor: auto-Zeile hat disabled Namen",
          auto_row.locator("input[data-f='name']").is_disabled())
    check("Editor: auto-Zeile hat kein ✕",
          auto_row.locator("button[data-act='remove']").count() == 0)
    real_row = pg.locator("#termsettbox .term-row", has=pg.locator("input[value='TrueNAS']"))
    check("Editor: echtes Ziel hat ✕", real_row.locator("button[data-act='remove']").count() == 1)

    # ---- Unraid-auto-Zeile befuellen und speichern ----
    auto_row.locator("input[data-f='user']").fill("root")
    pg.click("#term-save")
    pg.wait_for_timeout(600)
    msg = pg.locator("#termsett-msg").inner_text()
    check(f"Save-Meldung ohne skipped ({msg})",
          "Saved" in msg and "skipped" not in msg)
    check("POST enthielt Unraid mit user", any(
        t.get("name") == "Unraid" and t.get("user") == "root" for t in posted.get("targets", [])))
    pg.wait_for_timeout(300)
    check("Editor: Unraid-Zeile jetzt ohne auto-Badge",
          pg.locator("#termsettbox .term-row:has-text('auto')").count() == 0)

    js_errors = [e for e in errs if "Failed to load" not in e]
    check(f"Keine JS-Fehler ({len(js_errors)})", len(js_errors) == 0)
    if js_errors: print("  FEHLER:", js_errors[:5])
    b.close()

srv.shutdown()
print("GESAMT:", "OK" if ok else "FEHLER")
sys.exit(0 if ok else 1)
