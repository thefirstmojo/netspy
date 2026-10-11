#!/usr/bin/env python3
"""DOM-Test: Render-/Poll-Drosselung im Hintergrund-Tab.

Regression v0.7.40: Ein versteckter Tab zeichnete weiter jede Sekunde neu. Das
erzeugt ~2-3k DOM-Knoten Muell pro Sekunde, den Chrome im Hintergrund nur
traege abholt - gemessen 37.752 tote Knoten / ~77 MB bis zur naechsten GC.
Der Test zaehlt die /api/dashboard-Aufrufe des Fake-Servers:

  * versteckt -> deutlich weniger Abrufe (Intervall 5 s)
  * sichtbar  -> jeden Sekunde
  * visibilitychange zurueck auf sichtbar -> sofort ein Abruf
"""
import json, sys, threading, time
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from playwright.sync_api import sync_playwright

STATIC = "/opt/data/netspy/app/static"
T = time.time()
hits = {"n": 0}
DASH = {
    "version": "test", "servers": [{"name": "Unraid"}], "ifaces": [], "latency": {"Unraid": 3},
    "host_sys": {"Unraid": {"cpu": 10.0, "mem_total": 8e9, "mem_used": 4e9}}, "disk": [], "system": [],
    "mem": {"Unraid": {"ts": [T - 2, T - 1], "used": [1e9, 2e9]}},
    "cpu": {"Unraid": {"ts": [T - 2, T - 1], "cpu": [5.0, 6.0]}},
    "table": [{"name": "sshd", "hosts": {"Unraid": {"rx": 10, "tx": 20, "conns": 1}}}],
    "series": {"Unraid": {"ts": [T - 2, T - 1], "rx": [1, 2], "tx": [3, 4]}},
}


class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/dashboard":
            hits["n"] += 1
            return self.j(DASH)
        if p in ("/api/storage", "/api/settings", "/api/terminal"):
            return self.j({"enabled": [], "targets": [], "servers": [], "recorded": {}, "available": {}})
        if p.startswith("/api/"):
            return self.j({})
        return super().do_GET()

    def j(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 8095), partial(H, directory=STATIC))
threading.Thread(target=srv.serve_forever, daemon=True).start()

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS" if cond else "FAIL"), "-", name, ("" if cond else f"  <-- {extra}"))
    if not cond:
        ok = False


HIDE = "Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});"
SHOW = ("Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});"
        "document.dispatchEvent(new Event('visibilitychange'));")

with sync_playwright() as pw:
    b = pw.chromium.launch()

    # --- versteckter Tab: gedrosselt ---
    p1 = b.new_page(viewport={"width": 1100, "height": 800})
    errs = []
    p1.on("pageerror", lambda e: errs.append(str(e)))
    p1.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    p1.add_init_script(HIDE)
    p1.goto("http://127.0.0.1:8095/index.html")
    p1.wait_for_timeout(1500)
    hits["n"] = 0
    p1.wait_for_timeout(11000)
    hidden_hits = hits["n"]
    check(f"versteckter Tab: gedrosselt ({hidden_hits} Abrufe in 11 s, erwartet <= 4)", hidden_hits <= 4, hidden_hits)

    # --- sichtbar werden: sofortiger Abruf ---
    hits["n"] = 0
    p1.evaluate(SHOW)
    p1.wait_for_timeout(700)
    check(f"wieder sichtbar: sofort gerendert ({hits['n']} Abrufe)", hits["n"] >= 1, hits["n"])
    p1.wait_for_timeout(3000)
    hits["n"] = 0
    p1.wait_for_timeout(5000)
    vis_hits = hits["n"]
    check(f"sichtbar: normale Sekundenrate ({vis_hits} Abrufe in 5 s, erwartet >= 4)", vis_hits >= 4, vis_hits)
    check("keine JS-Fehler", not errs, errs[:3])
    b.close()

print("GESAMT:", "OK" if ok else "FEHLER")
sys.exit(0 if ok else 1)
