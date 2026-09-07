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
      try { localStorage.removeItem('netspy.cmdFavs'); } catch (e) {}
      try { localStorage.removeItem('netspy.cmdPanel'); } catch (e) {}
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
                   "filesystems & mounts", "journalctl -xe",
                   "fstrim -av", "mount -o remount,rw /", "docker ps -a",
                   "systemctl restart <service>", "ping -c 4 <host>", "ip route show",
                   "system, services & processes", "network",
                   "mover start", "mover stop", "mdcmd status", "nvidia-smi",
                   "zpool status", "zfs list -t snapshot", "zpool scrub <pool>",
                   "smbstatus", "zpool clear <pool>", "zpool online <pool> <device>",
                   "zpool replace <pool> <old> <new>"]:
        check(f"enthält: {needle}", needle in txt)
    check("7 Gruppen", pg.locator("#cmdlist .cmdgtitle").count() == 7)
    n_items = pg.locator("#cmdlist .cmditem").count()
    check(f"63 Einträge ({n_items})", n_items == 63)
    check("keine Favoriten-Gruppe ohne Favs", "favorites" not in txt)
    check("cmdpanel sichtbar (default)",
          pg.eval_on_selector("#cmdpanel", "el => el.style.display !== 'none'"))
    # Tooltip initial versteckt — VOR jeder Mausbewegung pruefen
    check("Tooltip initial versteckt",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))

    # ---- Favoriten: ☆ -> ⭐ Favorites-Gruppe oben ----
    fav_btn = pg.locator(".cmditem", has=pg.locator("code", has_text="mount -a")).locator(".cmdfav")
    check("Stern anfangs leer (☆)", fav_btn.inner_text() == "☆")
    fav_btn.click()
    pg.wait_for_timeout(250)
    txt2 = pg.locator("#cmdlist").inner_text().lower()
    check("Favoriten-Gruppe erscheint", "⭐ favorites" in txt2)
    favs = pg.evaluate("JSON.parse(localStorage.getItem('netspy.cmdFavs') || '[]')")
    check(f"localStorage enthält mount -a ({favs})", favs == ["mount -a"])
    check("mount -a jetzt doppelt (Fav + Gruppe)",
          pg.locator(".cmditem", has=pg.locator("code", has_text="mount -a")).count() == 2)
    fav_rows = pg.locator(".cmdgroup", has=pg.locator(".cmdgtitle", has_text="Favorites"))
    check("Favoriten-Gruppe steht ganz oben",
          pg.eval_on_selector("#cmdlist .cmdgroup:first-child .cmdgtitle",
                              "el => el.textContent").lower() == "⭐ favorites".lower())
    # Entfernen per Stern in der Favoriten-Zeile
    fav_rows.locator(".cmdfav").first.click()
    pg.wait_for_timeout(250)
    check("Stern entfernt Favorit wieder",
          pg.locator(".cmdgroup", has=pg.locator(".cmdgtitle", has_text="Favorites")).count() == 0)
    check("mount -a wieder einfach vorhanden",
          pg.locator(".cmditem", has=pg.locator("code", has_text="mount -a")).count() == 1)

    # ---- Copy: Klick auf ⧉ setzt den Befehl ins Clipboard ----
    row = pg.locator(".cmditem", has=pg.locator("code", has_text="mount -a"))
    row.locator(".cmdcopy").click()
    pg.wait_for_timeout(200)
    copied = pg.evaluate("window.__copied")
    check(f"Copy liefert exakten Text ({copied!r})", copied == "mount -a")
    pg.mouse.move(5, 5)   # Maus von der Zeile weg -> Tooltip zu
    pg.wait_for_timeout(150)
    check("Tooltip verschwindet nach Wegfahren",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))

    # ---- Tooltip beim Hover mit 1-s-Delay ----
    row.hover()
    pg.wait_for_timeout(300)
    check("Tooltip poppt NICHT sofort auf (Delay)",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))
    pg.wait_for_timeout(1000)
    tip_txt = pg.locator("#cmdtiptip").inner_text()
    check("Tooltip zeigt Beschreibung nach Delay", "fstab" in tip_txt)
    # In die Gruppen-Titel-Leiste fahren (ausserhalb der Zeilen) -> sofort weg
    box = pg.locator("#cmdlist")
    bb = box.bounding_box()
    pg.mouse.move(bb["x"] + 5, bb["y"] + 2)
    pg.wait_for_timeout(150)
    check("Tooltip weg ausserhalb der Zeilen",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))
    # Zeilenwechsel: alte Zeile verlassen = weg; neue Zeile erst nach Delay
    # (df -h liegt direkt unter mount -a -> sichtbar, kein Scroll noetig)
    row2 = pg.locator(".cmditem", has=pg.locator("code", has_text="df -h"))
    row2.hover()
    pg.wait_for_timeout(250)
    check("Zeilenwechsel: erst weg (Delay laeuft)",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))
    pg.wait_for_timeout(1000)
    tip2 = pg.locator("#cmdtiptip").inner_text()
    check("Zeilenwechsel: neuer Text nach Delay", "disk usage" in tip2)
    # Scrollen versteckt sofort
    pg.eval_on_selector("#cmdlist", "el => { el.scrollTop = el.scrollHeight; }")
    pg.wait_for_timeout(150)
    check("Scrollen versteckt den Tooltip",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))
    pg.mouse.move(5, 5)
    pg.wait_for_timeout(150)
    check("Tooltip nach Zeilenwechsel + Wegfahren zu",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))
    # LEBENSDAUER: Tooltip blendet sich nach 5 s OHNE Mausbewegung selbst aus
    # (deckt Fenster-Verlassen ohne Events ab - z. B. noVNC: die letzte
    # Mausposition bleibt auf der Zeile stehen, kein Signal erreicht die Seite)
    row.scroll_into_view_if_needed()
    pg.wait_for_timeout(300)   # Scroll abschliessen BEVOR die Maus faehrt
    row.hover()
    pg.wait_for_timeout(1500)
    check("Stale: nach Hover sichtbar",
          not pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))
    pg.wait_for_timeout(3200)
    check("Stale: nach 3 s ohne Bewegung noch sichtbar (Lesen moeglich)",
          not pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))
    pg.wait_for_timeout(2600)
    check("Stale: nach 5,8 s ohne Bewegung selbst ausgeblendet",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))
    pg.mouse.move(5, 5)
    pg.wait_for_timeout(150)

    # ---- Flags in den Docker-Beschreibungen erklaert ----
    # Tiefe Zeilen: erst in den sichtbaren Bereich scrollen, DANN hover
    # (Scrollen beim Hover wuerde den Timer abbrechen -> Tooltip bliebe aus)
    drow = pg.locator(".cmditem", has=pg.locator("code", has_text="docker image prune -a -f"))
    drow.scroll_into_view_if_needed()
    pg.wait_for_timeout(200)
    drow.hover()
    pg.wait_for_timeout(1300)
    dtip = pg.locator("#cmdtiptip").inner_text()
    check("Flags erklaert (-a/--all)", "dangling" in dtip and "-a" in dtip)
    check("Flag -f erklaert", "-f" in dtip)
    pg.mouse.move(5, 5)
    pg.wait_for_timeout(150)
    srow = pg.locator(".cmditem", has=pg.locator("code", has_text="docker system prune -f"))
    srow.scroll_into_view_if_needed()
    pg.wait_for_timeout(200)
    srow.hover()
    pg.wait_for_timeout(1300)
    stip = pg.locator("#cmdtiptip").inner_text()
    check("system prune -f: warum kein -a (tagged bleiben)", "tagged" in stip)
    pg.mouse.move(5, 5)
    pg.wait_for_timeout(150)
    check("Tooltip nach Docker-Hover weg",
          pg.eval_on_selector("#cmdtiptip", "el => el.classList.contains('hidden')"))

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
