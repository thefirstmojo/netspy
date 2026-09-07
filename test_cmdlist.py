#!/usr/bin/env python3
"""DOM-Test: Linux-Befehlsliste im Terminal-Tab (Inhalt, Copy, Toggle, Tooltip)."""
import json, sys, threading
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from playwright.sync_api import sync_playwright

STATIC = "/opt/data/netspy/app/static"
TERM = {"enabled": True, "error": "", "targets": [
    {"name": "TrueNAS", "host": "192.168.2.100", "user": "root",
     "has_key": False, "port": 7681, "running": True},
]}
DASH = {"version": "t", "servers": [{"name": "Main"}], "ifaces": [], "latency": {},
        "host_sys": {}, "disk": [], "system": [], "mem": {}, "cpu": {},
        "table": [], "series": {"Main": {"ts": [], "rx": [], "tx": []}}}

class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/dashboard": return self.j(DASH)
        if p == "/api/terminal": return self.j(TERM)
        if p.startswith("/api/"): return self.j({})
        return super().do_GET()
    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(ln)
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

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1200, "height": 800})
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.add_init_script("""
      try { sessionStorage.setItem('netspy.termLogin','1'); } catch (e) {}
      // Clipboard mocken (navigator.clipboard ist read-only -> defineProperty)
      try {
        window.__copied = null;
        Object.defineProperty(navigator, 'clipboard', {
          value: { writeText: t => { window.__copied = t; return Promise.resolve(); } },
          configurable: true,
        });
      } catch (e) {}
    """)
    pg.goto("http://127.0.0.1:8095/index.html")
    pg.wait_for_selector("#tabbtn-term")
    pg.click("#tabbtn-term")
    pg.wait_for_selector(".cmditem")
    pg.wait_for_timeout(400)

    # ---- Inhalt: Pflicht-Befehle + Gruppierung (inner_text = gerendert,
    # Titel sind per CSS uppercase) ----
    txt = pg.locator("#cmdlist").inner_text().lower()
    for needle in ["mount -a", "apt-get update", "apt-get upgrade -y",
                   "docker system prune -f", "docker system prune -a -f --volumes",
                   "filesystems & mounts", "docker cleanup", "journalctl -xe"]:
        check(f"enthält: {needle}", needle in txt)
    check("4 Gruppen", pg.locator("#cmdlist .cmdgtitle").count() == 4)
    n_items = pg.locator("#cmdlist .cmditem").count()
    check(f"26 Einträge ({n_items})", n_items == 26)
    check("cmdpanel sichtbar (default)",
          pg.eval_on_selector("#cmdpanel", "el => el.style.display !== 'none'"))

    # ---- Tooltip initial versteckt (VOR jeder Mausbewegung) ----
    check("Tooltip initial versteckt",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))

    # ---- Copy: Klick auf ⧉ setzt den Befehl ins Clipboard ----
    row = pg.locator(".cmditem", has=pg.locator("code", has_text="mount -a"))
    row.locator(".cmdcopy").click()
    pg.wait_for_timeout(200)
    copied = pg.evaluate("window.__copied")
    check(f"Copy liefert exakten Text ({copied!r})", copied == "mount -a")
    pg.mouse.move(5, 5)   # Maus von der Zeile weg -> Tooltip zu

    # ---- Tooltip beim Hover ----
    row.hover()
    pg.wait_for_timeout(250)
    tip_txt = pg.locator("#cmdtiptip").inner_text()
    check("Tooltip zeigt Beschreibung", "fstab" in tip_txt)

    # ---- Toggle: 📋 klappt die Liste ein/aus ----
    pg.click("#cmdopen")
    pg.wait_for_timeout(150)
    check("nach 📋: Panel ausgeblendet",
          pg.eval_on_selector("#cmdpanel", "el => el.style.display === 'none'"))
    st = pg.evaluate("localStorage.getItem('netspy.cmdPanel')")
    check("Zustand persistiert (0)", st == "0")
    pg.click("#cmdopen")
    pg.wait_for_timeout(150)
    check("nochmal 📋: Panel wieder da",
          pg.eval_on_selector("#cmdpanel", "el => el.style.display !== 'none'"))
    # ✕ schließt ebenfalls
    pg.click("#cmdclose")
    pg.wait_for_timeout(150)
    check("✕ schließt Panel",
          pg.eval_on_selector("#cmdpanel", "el => el.style.display === 'none'"))

    # ---- Grid-Layout: Karten UND Panel nebeneinander (flex) ----
    check("Terminal-Karte gerendert", pg.locator(".termcard").count() == 1)
    lay = pg.eval_on_selector(".termlayout", "el => getComputedStyle(el).display")
    check("termlayout ist flex", lay == "flex")

    js_errors = [e for e in errs if "Failed to load resource" not in e]
    check(f"Keine JS-Fehler ({len(js_errors)})", len(js_errors) == 0)
    if js_errors: print("  FEHLER:", js_errors[:4])
    b.close()

srv.shutdown()
print("GESAMT:", "OK" if ok else "FEHLER")
sys.exit(0 if ok else 1)
