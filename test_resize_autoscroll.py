#!/usr/bin/env python3
"""DOM-Test: Terminal-Resize mit Auto-Scroll + Reset-Sizes-Button."""
import json, threading, sys
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from playwright.sync_api import sync_playwright

STATIC = "/opt/data/netspy/app/static"
TARGETS = [
    {"name": "Unraid", "host": "192.168.2.101", "user": "root", "port": 7681,
     "running": True, "has_key": False},
    {"name": "TrueNAS", "host": "192.168.2.100", "user": "root", "port": 7682,
     "running": True, "has_key": False},
]

class H(SimpleHTTPRequestHandler):
    directory = STATIC
    def do_GET(self):
        if self.path.startswith("/api/"):
            body = json.dumps({"enabled": True, "error": "", "targets": TARGETS}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()
    def log_message(self, *a):
        pass

srv = ThreadingHTTPServer(("127.0.0.1", 8095), partial(H, directory=STATIC))
threading.Thread(target=srv.serve_forever, daemon=True).start()

ok = True
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond: ok = False

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 900, "height": 500})
    msgs = []
    pg.on("console", lambda m: msgs.append(m.text) if m.type == "error" else None)
    pg.add_init_script("sessionStorage.setItem('netspy.termLogin','1');"
                       "localStorage.setItem('netspy.termHeights','{}');")
    pg.goto("http://127.0.0.1:8095/index.html")
    pg.click("#tabbtn-term")
    pg.wait_for_selector(".termcard")
    pg.wait_for_timeout(200)

    # Grundzustand: 2 Karten, Standardhöhe 340
    check("2 Karten gerendert", pg.locator(".termcard").count() == 2)
    check("Reset-Button vorhanden", pg.locator("#tresetsizes").count() == 1)

    # --- Reset-Button: vorher Höhe ändern, dann Reset ---
    pg.eval_on_selector(".termframe", "f => f.style.height = '777px'")
    pg.click("#tresetsizes")
    hs = pg.eval_on_selector_all(".termframe", "fs => fs.map(f => f.style.height)")
    check("Reset setzt alle Frames auf 340px", all(h == "340px" for h in hs))
    stored = pg.evaluate("localStorage.getItem('netspy.termHeights')")
    check("Reset entfernt localStorage-Eintrag", stored is None)

    # --- Auto-Scroll: unterste Karte unten, Zug bis in die Randzone ---
    pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    pg.wait_for_timeout(100)
    bars = pg.locator(".termresize")
    n = bars.count()
    bb = bars.nth(n - 1).bounding_box()
    check("Resize-Balken der letzten Karte sichtbar", bb is not None and bb["y"] < 500)
    # Drag starten (Maus auf Balkenmitte drücken)
    pg.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    pg.mouse.down()
    pg.wait_for_timeout(80)
    # Cursor bis in die untere Randzone (unterhalb von innerHeight-90=410) ziehen
    pg.mouse.move(bb["x"] + bb["width"] / 2, 470, steps=8)
    pg.wait_for_timeout(600)   # mehrere rAF-Frames -> Auto-Scroll muss laufen
    sy = pg.evaluate("window.scrollY")
    check(f"Auto-Scroll hat gescrollt (scrollY={sy} > 0)", sy > 0)
    # Frame der GEZOGENEN Karte muss durch den Auto-Scroll wachsen
    # (Balken bleibt unter dem Cursor, auch ohne weitere Mausbewegung)
    sel = '.termcard[data-name="TrueNAS"] .termframe'
    h1 = pg.eval_on_selector(sel, "f => parseFloat(f.style.height)")
    pg.wait_for_timeout(400)
    h2 = pg.eval_on_selector(sel, "f => parseFloat(f.style.height)")
    check(f"Frame wächst durch Auto-Scroll ({h1} -> {h2})", h2 > h1)
    pg.mouse.up()
    pg.wait_for_timeout(150)
    # nach mouseup: kein weiteres Scrollen
    sy2 = pg.evaluate("window.scrollY")
    pg.wait_for_timeout(250)
    sy3 = pg.evaluate("window.scrollY")
    check("Scroll stoppt nach mouseup", abs(sy3 - sy2) < 2)

    errs = [m for m in msgs if "Error" in m or "error" in m]
    check("Keine JS-Fehler", len(errs) == 0)
    b.close()

srv.shutdown()
print("GESAMT:", "OK" if ok else "FEHLER")
sys.exit(0 if ok else 1)
