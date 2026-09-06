#!/usr/bin/env python3
"""Backend-Test: TerminalManager.status() mergt Monitoring-Server als
Auto-Vorschlaege (suggested) — nur fuer Server OHNE echtes SSH-Ziel."""
import importlib.util, os, sys, tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "app"))
spec = importlib.util.spec_from_file_location("web", os.path.join(_HERE, "app/web.py"))
web = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(web)

ok = True
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond: ok = False

check("http-URL -> Host", web._suggested_host("http://192.168.2.100:8091") == "192.168.2.100")
check("https-URL -> Host", web._suggested_host("https://debian.example.com:8443") == "debian.example.com")
check("local -> localhost", web._suggested_host(None) == "localhost")
check("local-Marker -> localhost", web._suggested_host("local") == "localhost")
check("nackte IP", web._suggested_host("10.0.0.5") == "10.0.0.5")

d = tempfile.mkdtemp()
tm = web.TerminalManager(d)
srv = [{"name": "Unraid", "url": None},
       {"name": "TrueNAS", "url": "http://192.168.2.100:8091"},
       {"name": "Debian", "url": "http://192.168.2.30:8091"}]
st = tm.status(srv)
check("3 suggested ohne echte Ziele", len(st["targets"]) == 3
      and all(t["suggested"] for t in st["targets"]))
by = {t["name"]: t for t in st["targets"]}
check("TrueNAS-Host extrahiert", by["TrueNAS"]["host"] == "192.168.2.100")
check("Unraid local -> localhost", by["Unraid"]["host"] == "localhost")
check("suggested user leer/port 0",
      all(t["user"] == "" and t["port"] == 0 for t in st["targets"]))

err = tm.set_targets([{"name": "TrueNAS", "host": "192.168.2.100", "user": "root"}])
check("set_targets ok", err == "")
st2 = tm.status(srv)
names2 = {t["name"]: t for t in st2["targets"]}
check("TrueNAS jetzt echtes Ziel", "TrueNAS" in names2 and not names2["TrueNAS"].get("suggested"))
check("Unraid + Debian bleiben suggested",
      names2["Unraid"].get("suggested") and names2["Debian"].get("suggested"))
check("kein Doppel-TrueNAS", len([t for t in st2["targets"] if t["name"] == "TrueNAS"]) == 1)
check("echte Ziele vor suggested", st2["targets"][0]["name"] == "TrueNAS"
      and not st2["targets"][0].get("suggested"))
tm.shutdown()
print("GESAMT:", "OK" if ok else "FEHLER")
sys.exit(0 if ok else 1)
