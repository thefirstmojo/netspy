#!/usr/bin/env python3
"""DOM-Test: Zeitstempel-Labels der Charts (Format + Tempo).

Regression v0.7.39: fmtTs() formatierte JEDEN Datenpunkt mit
Date#toLocaleTimeString("de-DE", {hour12: false}) - also ICU mit einem frischen
Options-Objekt pro Aufruf. Gemessen: 111 us pro Aufruf, bei ~3.600 Aufrufen pro
Sekunde (15 Charts x 300 Punkte x 3 Server) rund 40 % eines CPU-Kerns
Dauerlast - plus Allokationswachstum, das den Renderer im Dauerbetrieb auf
mehrere GB brachte (siehe 86-h-Messung: 3,2 GB / 45 h CPU).

Der Test haelt beide Eigenschaften fest:
  * Ausgabe identisch zur alten ICU-Variante
  * 20.000 Aufrufe weit unter deren Laufzeit (~2,2 s)
"""
import json, sys, threading, time
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from playwright.sync_api import sync_playwright

STATIC = "/opt/data/netspy/app/static"
T = time.time()
SERVERS = ["Unraid", "TrueNAS"]
N = 300
# wanderndes Zeitfenster wie im echten Backend: pro Abruf ein Punkt mehr
_tick = [0]


def payload():
    _tick[0] += 1
    ts = [T - N + i + _tick[0] for i in range(N)]
    return ts
def dashboard():
    ts = payload()
    return {
        "version": "test", "servers": [{"name": n} for n in SERVERS],
        "ifaces": [], "latency": {n: 5 for n in SERVERS}, "host_sys": {},
        "disk": [], "system": [], "table": [],
        "series": {n: {"ts": ts, "rx": [i * 1.0 for i in range(N)],
                       "tx": [i * 2.0 for i in range(N)]} for n in SERVERS},
        "mem": {n: {"ts": ts, "used": [1000 + i for i in range(N)]} for n in SERVERS},
        "cpu": {n: {"ts": ts, "cpu": [10.0] * N} for n in SERVERS},
    }


class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/dashboard":
            return self.j(dashboard())
        if p == "/api/storage":
            return self.j({"enabled": [], "available": {}, "recorded": {}, "host_access": {}})
        if p == "/api/settings":
            return self.j({"path": "", "writable": False, "source": "none",
                           "has_template": False, "diag": {}, "servers": []})
        if p == "/api/terminal":
            return self.j({"enabled": False, "error": "", "targets": []})
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


with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1100, "height": 800})
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8095/index.html")
    pg.wait_for_timeout(2500)

    # 1) Ausgabe identisch zur alten Implementierung
    same = pg.evaluate("""() => {
      const t = [Math.floor(Date.now()/1000), 1791681740, 1000000000, 1791000000, 1791648000];
      return t.map(x => [new Date(x*1000).toLocaleTimeString('de-DE', {hour12:false}), fmtTs(x)]);
    }""")
    check("Labels identisch zum alten ICU-Format", all(a == b for a, b in same), same)
    check("Format HH:MM:SS", all(len(v) == 8 and v[2] == ":" and v[5] == ":" for _, v in same), same)

    # 2) Tempo: 20.000 Aufrufe (echte Zeitstempel -> Cache greift)
    ms = pg.evaluate("""() => { const t0 = performance.now();
      for (let i = 0; i < 20000; i++) fmtTs(1791681740 + (i % 600));
      return performance.now() - t0; }""")
    check(f"20.000 Aufrufe unter 300 ms (alte Variante: ~2.200 ms) - {ms:.0f} ms", ms < 300)

    # 3) Inkrementelles Fuettern: die Array-OBJEKTE muessen identisch bleiben.
    #    (Werden sie jede Sekunde ersetzt, parst Chart.js alle 300 Punkte neu -
    #    genau das liess den Renderer-Speicher wachsen.)
    pg.wait_for_timeout(2500)          # mindestens zwei Refresh-Ticks
    ident = pg.evaluate("""() => {
      const c = Chart.getChart(document.querySelector('canvas'));
      if (!c) return null;
      const ref = c.data.datasets[0].data;
      const lbl = c.data.labels;
      const before = {n: ref.length, last: lbl[lbl.length - 1], tail: ref[ref.length - 1],
                      top: lbl[0], ptr: window.__ref};
      window.__ref = ref;
      return {same: window.__ref === ref, n: ref.length, last: lbl[lbl.length - 1],
              head: lbl[0], tail: ref[ref.length - 1]};
    }""")
    pg.wait_for_timeout(2500)
    ident2 = pg.evaluate("""() => {
      const c = Chart.getChart(document.querySelector('canvas'));
      const ref = c.data.datasets[0].data;
      const lbl = c.data.labels;
      return {same: ref === window.__ref, n: ref.length, last: lbl[lbl.length - 1],
              head: lbl[0], tail: ref[ref.length - 1]};
    }""")
    check("Daten-Array bleibt dasselbe Objekt (inkrementell statt Neuaufbau)",
          bool(ident2 and ident2["same"]), ident2)
    check("Fenster wandert (neuester Punkt vorne/hinten mitgewandert)",
          bool(ident2 and ident2["last"] != ident) and ident2["head"] != ident.get("head"),
          {"vorher": ident, "nachher": ident2})
    check("Fenster bleibt 300 Punkte lang", bool(ident2 and ident2["n"] == 300), ident2)

    # 4) Die Charts nutzen die Labels wirklich (Ende-zu-Ende)
    labels = pg.evaluate("""() => { const c = Chart.getChart(document.querySelector('canvas'));
      return c && c.data.labels ? c.data.labels.slice(-3) : null; }""")
    check("Chart-Achse traegt HH:MM:SS-Labels",
          bool(labels) and all(len(v) == 8 and v[2] == ":" for v in labels), labels)

    # 5) Keine JS-Fehler
    check("Keine JS-Fehler", not errs, errs[:3])
    b.close()

print("GESAMT:", "OK" if ok else "FEHLER")
sys.exit(0 if ok else 1)
