#!/usr/bin/env python3
"""NetSpy settings + MCP toggle tests (no browser, no external network).

Run directly:
    python3 test_settings_units.py

Covers app/settings_store.py (settings.yaml) and McpController (app/mcp_server.py):
  * resolution order: MCP_TOKEN env wins over mcp.token in settings.yaml
  * enabled default: a token switches the listener on (composes from before
    v0.7.47 keep working untouched), an explicit mcp.enabled: false wins
  * settings.yaml: roundtrip, merge keeps other keys, broken file is ignored,
    the file is written 0666 (editable on the host, like servers.yaml)
  * log output: the start-up block, the "first connection" line, a rejected
    token (logged once per address, not per request)
  * runtime toggle: start / stop / restart with a new token; the old token is
    rejected afterwards and the port is free again after stop
"""
import json
import os
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import mcp_server as M  # noqa: E402
import settings_store as S  # noqa: E402

FAILS: list = []
CHECKS = [0]
PORT = 18093


def check(label: str, cond: bool) -> None:
    CHECKS[0] += 1
    if not cond:
        FAILS.append(label)
        print(f"    FAIL: {label}")


class StubMonitor:
    """Only the handshake is exercised here - no tool touches this object."""

    snaps: dict = {}
    online: dict = {}
    errors: dict = {}


def rpc(path: str, token: str, body: dict) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-MCP-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def port_open(port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}}


def test_resolution(tmp: str) -> None:
    print("  settings_store.resolve_mcp (env vor Datei)")
    env = {"MCP_TOKEN": "", "MCP_PORT": ""}
    cfg = S.resolve_mcp(tmp, env)
    check("ohne alles: aus", cfg["enabled"] is False and cfg["token"] == ""
          and cfg["token_source"] == "" and cfg["port"] == M.DEFAULT_PORT)

    cfg = S.resolve_mcp(tmp, {"MCP_TOKEN": "envtok"})
    check("env-Token: an, Quelle env, Default-Port",
          cfg["enabled"] is True and cfg["token"] == "envtok"
          and cfg["token_source"] == "env" and cfg["port"] == M.DEFAULT_PORT)

    S.update_settings(tmp, {"mcp": {"token": "filetok"}})
    cfg = S.resolve_mcp(tmp, {"MCP_TOKEN": "", "MCP_PORT": "9999"})
    check("Datei-Token: an, Quelle file", cfg["enabled"] is True
          and cfg["token"] == "filetok" and cfg["token_source"] == "file")
    check("MCP_PORT env gewinnt", cfg["port"] == 9999)

    cfg = S.resolve_mcp(tmp, {"MCP_TOKEN": "envtok"})
    check("env schlaegt Datei", cfg["token"] == "envtok" and cfg["token_source"] == "env")

    S.update_settings(tmp, {"mcp": {"enabled": False}})
    cfg = S.resolve_mcp(tmp, {"MCP_TOKEN": "envtok"})
    check("Toggle aus schlaegt env-Token", cfg["enabled"] is False
          and cfg["token"] == "envtok")

    S.update_settings(tmp, {"mcp": {"enabled": True, "token": ""}})
    cfg = S.resolve_mcp(tmp, {"MCP_TOKEN": ""})
    check("an ohne Token startet nicht", cfg["enabled"] is True and cfg["token"] == "")

    cfg = S.resolve_mcp(tmp, {"MCP_TOKEN": "", "MCP_PORT": "kaputt"})
    check("ungueltiger Port faellt auf Default", cfg["port"] == M.DEFAULT_PORT)


def test_file(tmp: str) -> None:
    print("  settings.yaml: Roundtrip, Merge, Rechte, defekte Datei")
    path = S.update_settings(tmp, {"mcp": {"enabled": True, "token": "abc"}})
    check("Pfad ist settings.yaml", path == S.settings_path(tmp) and path.endswith("settings.yaml"))
    raw = open(path, encoding="utf-8").read()
    check("Kopfkommentar enthalten", raw.startswith("# NetSpy - application settings"))
    check("Datei ist am Host editierbar (0666)", stat.S_IMODE(os.stat(path).st_mode) == 0o666)
    check("Wert gespeichert", S.load_settings(tmp)["mcp"]["token"] == "abc")

    S.update_settings(tmp, {"other": {"keep": 1, "x": 2}})
    S.update_settings(tmp, {"other": {"keep": 9}})
    data = S.load_settings(tmp)
    check("Merge erhaelt unbekannte Keys", data["other"] == {"keep": 9, "x": 2})
    check("Merge erhaelt mcp-Block", data["mcp"]["token"] == "abc")

    open(path, "w", encoding="utf-8").write("mcp: [kaputt\n  - kein dict\n")
    check("defekte Datei -> {}", S.load_settings(tmp) == {})
    check("defekte Datei blockiert Aufloesung nicht",
          S.resolve_mcp(tmp, {"MCP_TOKEN": "t"})["token"] == "t")
    check("fehlende Datei -> {}", S.load_settings(os.path.join(tmp, "nope")) == {})


def test_controller() -> None:
    print("  McpController: Start/Stop/Tokenwechsel zur Laufzeit")
    ctl = M.McpController(StubMonitor(), PORT)
    check("startet nicht ohne Token", ctl.apply(True, "")["running"] is False)
    check("startet nicht wenn aus", ctl.apply(False, "tok-a")["running"] is False)
    check("Port bleibt zu", port_open() is False)

    st = ctl.apply(True, "tok-a")
    check("laeuft mit Toggle an + Token", st["running"] is True and st["tools"] == 8)
    time.sleep(0.15)
    check("Port offen", port_open() is True)
    code, body = rpc(M.MCP_PATH, "tok-a", INIT)
    check("initialize mit richtigem Token -> 200", code == 200 and body["result"]["serverInfo"]["name"] == "netspy")
    code, _ = rpc(M.MCP_PATH, "", INIT)
    check("ohne Token -> 401", code == 401)
    code, _ = rpc(M.MCP_PATH, "falsch", INIT)
    check("falscher Token -> 401", code == 401)

    st = ctl.apply(True, "tok-b")
    check("Tokenwechsel bleibt an", st["running"] is True)
    time.sleep(0.15)
    code, _ = rpc(M.MCP_PATH, "tok-a", INIT)
    check("alter Token wird abgelehnt", code == 401)
    code, body = rpc(M.MCP_PATH, "tok-b", INIT)
    check("neuer Token funktioniert", code == 200)

    st = ctl.apply(False, "tok-b")
    check("Toggle aus stoppt", st["running"] is False)
    time.sleep(0.35)
    check("Port nach Stop geschlossen", port_open() is False)
    check("Status meldet stopped", ctl.status()["running"] is False)

    # Belegter Port: kein Crash, Fehler landet in status() und im Log
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", PORT))
    blocker.listen(1)
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            st = ctl.apply(True, "tok-c")
    finally:
        blocker.close()
    check("belegter Port laesst die Anwendung weiterlaufen",
          st["running"] is False and "cannot bind port" in st["error"])
    check("Bind-Fehler wird geloggt", "[mcp] ERROR" in buf.getvalue()
          and "cannot bind port" in buf.getvalue())
    check("status() behaelt den Fehler", "cannot bind port" in ctl.status()["error"])

    st = ctl.apply(True, "tok-d")
    check("nach freiem Port startet er wieder und der Fehler ist weg",
          st["running"] is True and st["error"] == "")
    ctl.stop()


def test_logging() -> None:
    print("  Logmeldungen: Start, erste Verbindung, abgewiesener Token")
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ctl = M.McpController(StubMonitor(), PORT)
        ctl.apply(True, "")                      # kein Token
        ctl.apply(False, "tok-a")                # Toggle aus
        ctl.apply(True, "log-tok")               # an
        time.sleep(0.2)
        rpc(M.MCP_PATH, "log-tok", INIT)         # 1. Verbindung
        rpc(M.MCP_PATH, "log-tok", INIT)         # 2. Anfrage, keine 2. Meldung
        rpc(M.MCP_PATH, "falsch", INIT)          # 401
        rpc(M.MCP_PATH, "falsch", INIT)          # 401, nicht doppelt
        ctl.apply(False, "log-tok")              # stop
    out = buf.getvalue()
    check("Startmeldung nennt Bindung, Port und Toolzahl",
          f"listening on 0.0.0.0:{PORT}/mcp" in out and "8 read-only tools" in out)
    check("Aus-Meldung ohne Token",
          "off - no token configured" in out and f"port {PORT} free" in out)
    check("Aus-Meldung durch den Toggle",
          "off - switched off in the Settings tab" in out)
    check("erste Verbindung wird einmal gemeldet",
          out.count("first connection from") == 1 and "token accepted" in out)
    check("abgewiesener Token wird einmal pro Adresse gemeldet",
          out.count("rejected 127.0.0.1") == 1)
    check("Stop-Meldung nennt den freigegebenen Port",
          f"listener stopped - port {PORT} released" in out)
    check("kein Fehler bei sauberem Lauf", ctl.status()["error"] == "")
    check("Token steht in keiner Logzeile", "log-tok" not in out)


def test_startup_block(tmp: str) -> None:
    print("  log_startup: ein Block mit Adresse, MCP-Status und Servern")
    import contextlib
    import io

    import web as W

    class Mon:
        sampler = None

    servers = [{"name": "Unraid", "url": "local"},
               {"name": "TrueNAS", "url": "http://192.168.2.100:8091"}]

    def run(cfg, state):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            W.log_startup(Mon(), servers, tmp, 18090, 18091, cfg, state)
        return buf.getvalue()

    on = run({"port": 18092, "token": "x", "token_source": "env", "enabled": True},
             {"running": True, "tools": 8, "error": ""})
    check("Kopfzeile mit Version", f"NetSpy {W.VERSION}" in on)
    check("Dashboard-Adresse", ":18090" in on)
    check("MCP an, Quelle env, Adresse", "on  -> http://" in on and "env MCP_TOKEN" in on
          and ":18092/mcp" in on)
    check("Serverliste mit beiden Eintraegen", "Unraid=local" in on and "TrueNAS=http://" in on)
    check("lokaler Agent nicht gestartet", "not started" in on)
    check("Config-Ordner und Rechte", tmp in on and ("writable" in on or "READ-ONLY" in on))
    check("Token steht nicht im Startblock", "token from env MCP_TOKEN" in on and "'x'" not in on)

    off = run({"port": 18092, "token": "", "token_source": "", "enabled": False},
              {"running": False, "tools": 0, "error": ""})
    check("MCP aus bei fehlendem Token", "off - no token configured" in off
          and "port 18092 free" in off)

    err = run({"port": 18092, "token": "x", "token_source": "file", "enabled": True},
              {"running": False, "tools": 0, "error": "cannot bind port 18092"})
    check("Bind-Fehler wird im Startblock gezeigt", "ERROR" in err and "cannot bind" in err)


def main() -> int:
    print("NetSpy settings + MCP toggle tests")
    with tempfile.TemporaryDirectory() as tmp:
        test_resolution(tmp)
        test_file(tmp)
    test_controller()
    test_logging()
    with tempfile.TemporaryDirectory() as tmp2:
        test_startup_block(tmp2)
    ok = CHECKS[0] - len(FAILS)
    print(f"\n{ok}/{CHECKS[0]} checks passed")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    print("Alle Checks gruen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
