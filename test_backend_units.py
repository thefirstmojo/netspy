#!/usr/bin/env python3
"""NetSpy backend unit + regression tests (no browser, no external network).

Run directly:
    python3 test_backend_units.py
Or with pytest:
    pytest test_backend_units.py

The Playwright/DOM tests (test_cmdlist.py, test_filter_audit.py,
test_resize_autoscroll.py, test_terminal_suggested.py) are standalone
scripts and must be run separately with the Playwright interpreter.

Regression focus:
  * /api/dashboard must NEVER fail because of a single server: a server
    without a RAM sample (offline, or freshly added) used to make the whole
    dashboard raise TypeError -> every dashboard request returned 500.
  * The dashboard payload keeps the exact field shape the frontend reads.
  * Terminal targets: validation, key file mode, keys never leave the API.
  * _fetch only opens http/https URLs.
"""
import json
import os
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import agent  # noqa: E402
import web  # noqa: E402
from web import (Monitor, StorageStore, TerminalManager, merge_servers,  # noqa: E402
                 parse_servers, _valid_servers)

FAILS: list = []
GIB = 1024 ** 3


def check(name: str, cond: bool, extra="") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _dead_url() -> str:
    """A URL that refuses connections immediately (port 9 = discard)."""
    return "http://127.0.0.1:9/"


def _snapshot(mem_total=16 * GIB, mem_used=4 * GIB) -> dict:
    """Minimal payload with the shape a real agent produces."""
    return {
        "version": "0.0.0-test",
        "hostname": "fake-host",
        "ts": time.time(),
        "totals": {"rx": 1234.5, "tx": 678.9},
        "rest": {"rx": 1.0, "tx": 2.0},
        "interfaces": [{"name": "eth0", "rx": 1234.5, "tx": 678.9,
                        "uplink": True, "container": None}],
        "processes": [{"name": "nginx", "rx": 1000.0, "tx": 500.0,
                       "container": None}],
        "containers": [],
        "disk": [{"name": "sda", "read": 10.0, "write": 20.0,
                  "read10": 1.0, "write10": 2.0}],
        "system": {"cpu": 12.5, "cpu10": 10.0, "mem_total": mem_total,
                   "mem_used": mem_used,
                   "procs": [{"name": "nginx", "cpu": 1.5, "cpu10": 1.0,
                              "mem": 50 * 1024 ** 2}]},
        "ss_error": "",
        "ss_ok": True,
        "pid1": "init",
        "proc_count": 42,
        "docker": {"socket": False, "containers": 0, "macs": 0,
                   "fdb": 0, "veths_mapped": 0},
    }


def _fake_agent(snapshot: dict) -> ThreadingHTTPServer:
    """Serve `snapshot` on /api/metrics on a free local port."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(snapshot).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------------------
# 1) dashboard() — REGRESSION: no RAM sample must not break the dashboard
# ---------------------------------------------------------------------------
def test_dashboard_without_history():
    print("dashboard() with a server that has no history at all")
    m = Monitor([{"name": "A", "url": _dead_url()}])
    try:
        d = m.dashboard()
    except Exception as e:  # the historical bug: TypeError: 'int' not subscriptable
        check("dashboard() does not raise", False, f"{type(e).__name__}: {e}")
        return
    check("dashboard() does not raise", True)
    check("mem total is 0 when unknown", d["mem"]["A"]["total"] == 0,
          d["mem"]["A"]["total"])
    check("mem arrays empty", d["mem"]["A"]["ts"] == [] and d["mem"]["A"]["used"] == [])
    check("cpu arrays empty", d["cpu"]["A"]["cpu"] == [] and d["cpu"]["A"]["cpu10"] == [])
    check("latency arrays empty", d["latency"]["A"]["ms"] == [])
    check("series present but empty", d["series"]["A"]["rx"] == [])
    check("totals fall back to 0/0", d["servers"][0]["totals"] == {"rx": 0.0, "tx": 0.0})
    check("offline flag false on fresh monitor", d["servers"][0]["online"] is False)
    check("version is a string", isinstance(d["version"], str) and d["version"])
    check("ts is a number", isinstance(d["ts"], float))


def test_dashboard_history_shape():
    print("dashboard() payload shape with data")
    m = Monitor([{"name": "A", "url": _dead_url()}])
    now = time.time()
    m.mem_history["A"].append((now, 8 * GIB, 16 * GIB))
    m.mem_history["A"].append((now + 1, 9 * GIB, 16 * GIB))
    m.cpu_history["A"].append((now, 11.0, 9.5))
    m.latency["A"].append((now, 2.5))
    m.history["A"].append((now, 100.0, 200.0))
    d = m.dashboard()
    check("mem total = last sample", d["mem"]["A"]["total"] == 16 * GIB)
    check("mem used array mirrored", d["mem"]["A"]["used"] == [8 * GIB, 9 * GIB])
    check("mem ts array mirrored", len(d["mem"]["A"]["ts"]) == 2)
    check("cpu cpu/cpu10 arrays", d["cpu"]["A"]["cpu"] == [11.0]
          and d["cpu"]["A"]["cpu10"] == [9.5])
    check("latency ms array", d["latency"]["A"]["ms"] == [2.5])
    check("series rx/tx", d["series"]["A"]["rx"] == [100.0] and d["series"]["A"]["tx"] == [200.0])


def test_dashboard_keys_per_server():
    print("dashboard() exposes one entry per server for every series")
    m = Monitor([{"name": "A", "url": _dead_url()},
                 {"name": "B", "url": _dead_url()}])
    d = m.dashboard()
    for section in ("series", "ifaces", "mem", "cpu", "latency"):
        check(f"{section} covers all servers", set(d[section]) == {"A", "B"},
              sorted(d[section]))
    check("servers list ordered", [s["name"] for s in d["servers"]] == ["A", "B"])
    for key in ("name", "online", "error", "hostname", "version", "totals"):
        check(f"server entry has '{key}'", key in d["servers"][0])
    for key in ("table", "disk", "system", "host_sys"):
        check(f"payload has '{key}'", key in d)


def test_dashboard_one_offline_one_online():
    print("poll_loop: one reachable + one unreachable server (e2e)")
    victim_total = 32 * GIB
    srv = _fake_agent(_snapshot(mem_total=victim_total, mem_used=8 * GIB))
    port = srv.server_address[1]
    m = Monitor([{"name": "Up", "url": f"http://127.0.0.1:{port}/"},
                 {"name": "Down", "url": _dead_url()}])
    threading.Thread(target=m.poll_loop, daemon=True).start()
    time.sleep(2.6)
    srv.shutdown()
    try:
        d = m.dashboard()
    except Exception as e:
        check("dashboard() survives an offline server", False, f"{type(e).__name__}: {e}")
        return
    check("dashboard() survives an offline server", True)
    by = {s["name"]: s for s in d["servers"]}
    check("reachable server online", by["Up"]["online"] is True)
    check("hostname from agent", by["Up"]["hostname"] == "fake-host")
    check("unreachable server offline", by["Down"]["online"] is False)
    check("unreachable server has an error text", bool(by["Down"]["error"]))
    check("RAM total from the agent", d["mem"]["Up"]["total"] == victim_total)
    check("offline server RAM total stays 0", d["mem"]["Down"]["total"] == 0)
    check("traffic series collected", len(d["series"]["Up"]["rx"]) >= 1)
    check("latency recorded", len(d["latency"]["Up"]["ms"]) >= 1)
    check("process table populated", any(r["name"] == "nginx" for r in d["table"]),
          [r["name"] for r in d["table"]])
    check("disk table populated", any(r["name"] == "sda" for r in d["disk"]))
    check("system table populated", any(r["name"] == "nginx" for r in d["system"]))
    check("host_sys has mem_total", d["host_sys"]["Up"].get("mem_total") == victim_total)


def test_poll_one_error_tuple():
    print("_poll_one returns (name, snap, err, ms)")
    m = Monitor([{"name": "A", "url": _dead_url()}])
    name, snap, err, ms = m._poll_one(m.servers[0])
    check("name returned", name == "A")
    check("snap is None on failure", snap is None)
    check("err is a string", isinstance(err, str) and err)
    check("ms is None on failure", ms is None)


def test_process_history():
    print("process_history / prochistory")
    m = Monitor([{"name": "A", "url": _dead_url()}])
    check("unknown process -> empty arrays",
          m.process_history("A", "nope") == {"ts": [], "rx": [], "tx": []})
    m.proc_history["A"]["nginx"] = deque([(1.0, 10.0, 20.0)], maxlen=300)
    h = m.process_history("A", "nginx")
    check("process history mirrored", h == {"ts": [1.0], "rx": [10.0], "tx": [20.0]})
    m.proc_snaps["A"].append((1.0, [["nginx", None, 10.0, 20.0]]))
    ph = m.prochistory()
    check("prochistory shape", ph["A"][0]["ts"] == 1.0
          and ph["A"][0]["procs"][0][0] == "nginx")


def test_set_servers_keeps_and_cleans():
    print("set_servers: history preserved for kept, dropped for removed")
    m = Monitor([{"name": "A", "url": _dead_url()}, {"name": "B", "url": _dead_url()}])
    m.mem_history["A"].append((1.0, 1 * GIB, 2 * GIB))
    old = m.mem_history["A"]
    m.set_servers([{"name": "A", "url": None}, {"name": "C", "url": _dead_url()}])
    check("kept server keeps the same deque", m.mem_history["A"] is old)
    check("kept server keeps its data", len(m.mem_history["A"]) == 1)
    check("new server initialised", "C" in m.mem_history and len(m.mem_history["C"]) == 0)
    check("removed server cleaned up", "B" not in m.mem_history and "B" not in m.snaps)
    check("local sampler created on demand", m.sampler is not None)


# ---------------------------------------------------------------------------
# 2) config parsing / merging
# ---------------------------------------------------------------------------
def test_parsing():
    print("server config parsing")
    s = parse_servers("Unraid=local;TrueNAS=http://10.10.10.20:8091")
    check("parse two servers", [x["name"] for x in s] == ["Unraid", "TrueNAS"])
    check("'local' -> url None", s[0]["url"] is None)
    check("remote url kept", s[1]["url"] == "http://10.10.10.20:8091")
    check("bare name defaults to local", parse_servers("Solo")[0]["url"] is None)
    check("empty spec -> []", parse_servers("") == [])
    check("_valid_servers rejects junk", _valid_servers({"a": 1}) is None)
    check("_valid_servers rejects missing name", _valid_servers([{"url": "x"}]) is None)
    check("_valid_servers normalises 'local'",
          _valid_servers([{"name": "X", "url": "LOCAL"}])[0]["url"] is None)
    merged = merge_servers([{"name": "A", "url": None}],
                           [{"name": "A", "url": "http://x:1"}, {"name": "B", "url": None}])
    check("config entry wins over env", merged[0]["url"] == "http://x:1")
    check("no duplicates", [x["name"] for x in merged] == ["A", "B"])
    check("empty merge adds local Main",
          [x["name"] for x in merge_servers([], [])] == ["Main"])


# ---------------------------------------------------------------------------
# 3) StorageStore history writer
# ---------------------------------------------------------------------------
def test_storage_bump():
    print("StorageStore._bump (hourly / daily / monthly)")
    with tempfile.TemporaryDirectory() as td:
        st = StorageStore(td)
        now = time.mktime((2026, 9, 10, 12, 0, 0, 0, 0, -1))
        entry = {"used": 100}
        persist = st._bump(entry, now)
        check("first tick persists", persist is True)
        check("h24 got one point", len(entry["h24"]) == 1)
        check("d7 got one point", len(entry["d7"]) == 1)
        check("d7 holds the day", entry["d7"][0][2] == time.localtime(int(now)).tm_yday)
        check("no monthly value on the first tick", "m" not in entry)

        persist = st._bump(entry, now + 60)
        check("tick within the hour is not persisted", persist is False)
        check("h24 still one point", len(entry["h24"]) == 1)
        check("d7 live value updated in place", len(entry["d7"]) == 1
              and entry["d7"][0][1] == 100)

        entry["used"] = 200
        persist = st._bump(entry, now + 3700)
        check("after an hour a new point is appended", persist is True)
        check("h24 has two points", len(entry["h24"]) == 2)
        check("h24 keeps the new value", entry["h24"][-1][1] == 200)

        for i in range(30):  # cap at 24 points
            st._bump(entry, now + 7200 + i * 3700)
        check("h24 capped at 24", len(entry["h24"]) == 24)
        check("d7 capped at 7", len(entry["d7"]) <= 7)


def test_storage_month_rollover():
    print("StorageStore month rollover")
    with tempfile.TemporaryDirectory() as td:
        st = StorageStore(td)
        aug = time.mktime((2026, 8, 20, 12, 0, 0, 0, 0, -1))
        sep = time.mktime((2026, 9, 2, 12, 0, 0, 0, 0, -1))
        entry = {"used": 111, "last_month": "2026-08",
                 "d7": [[int(aug), 111, time.localtime(int(aug)).tm_yday],
                        [int(sep), 222, time.localtime(int(sep)).tm_yday]]}
        persist = st._bump(entry, sep)
        check("rollover persists", persist is True)
        check("month value fixed from the last daily point",
              entry.get("m") == [[int(sep), 111]], entry.get("m"))
        check("last_month advanced", entry["last_month"] == "2026-09")
        check("no duplicate month entry on the next tick",
              st._bump(entry, sep + 60) is False)


def test_storage_m1_32_days():
    print("StorageStore m1 (1 month = 32 days)")
    with tempfile.TemporaryDirectory() as td:
        st = StorageStore(td)
        day = 86400
        now = time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))
        entry = {"used": 100}
        st._bump(entry, now)
        check("m1 created on the first tick", len(entry["m1"]) == 1)
        check("m1 holds the current day",
              entry["m1"][0][2] == time.localtime(int(now)).tm_yday)
        entry["used"] = 150
        st._bump(entry, now + 600)
        check("m1 live value updated in place",
              len(entry["m1"]) == 1 and entry["m1"][-1][1] == 150)
        for i in range(1, 41):          # 40 Tage -> Kappe bei 32
            entry["used"] = 100 + i
            st._bump(entry, now + i * day)
        check("m1 capped at 32 days", len(entry["m1"]) == 32)
        check("m1 keeps the newest value", entry["m1"][-1][1] == 140)
        check("m1 spans exactly 32 days",
              int(entry["m1"][-1][0] - entry["m1"][0][0]) == 31 * day)
        check("d7 still capped at 7 with m1 active", len(entry["d7"]) == 7)


def test_storage_short_points_survive():
    print("StorageStore tolerates shortened [ts, used] points (hand-edited JSON)")
    with tempfile.TemporaryDirectory() as td:
        st = StorageStore(td)
        now = time.mktime((2026, 3, 7, 12, 0, 0, 0, 0, -1))
        entry = {"used": 42, "h24": [[int(now) - 60, 40]],
                 "d7": [[int(now) - 86400, 30]],          # ohne Tagesmarke
                 "m1": [[int(now) - 86400, 30]]}          # ohne Tagesmarke
        try:
            st._bump(entry, now)
            crashed = None
        except Exception as e:                            # noqa: BLE001
            crashed = e
        check("_bump does not raise on shortened points", crashed is None, repr(crashed))
        check("m1 got a well-formed new point",
              len(entry["m1"]) == 2 and len(entry["m1"][-1]) == 3
              and entry["m1"][-1][1] == 42)
        check("d7 got a well-formed new point",
              len(entry["d7"]) == 2 and len(entry["d7"][-1]) == 3)
        check("old shortened points are kept (no data loss)",
              entry["m1"][0] == [int(now) - 86400, 30])


def test_storage_m1_migration():
    print("StorageStore m1 seeded from existing daily points")
    with tempfile.TemporaryDirectory() as td:
        st = StorageStore(td)
        base = time.mktime((2026, 5, 1, 12, 0, 0, 0, 0, -1))
        d7 = [[int(base + i * 86400), 10 * i,
               time.localtime(int(base + i * 86400)).tm_yday] for i in range(5)]
        entry = {"used": 55, "h24": [[int(base), 50]], "d7": d7}
        st._bump(entry, base + 5 * 86400)
        check("m1 seeded with the existing real daily points",
              len(entry["m1"]) == 6)
        check("m1 keeps the old values (no projection)",
              [p[1] for p in entry["m1"][:5]] == [0, 10, 20, 30, 40])
        check("m1 last point carries the current value", entry["m1"][-1][1] == 55)


def test_storage_toggle_delete_roundtrip():
    print("StorageStore toggle/delete/persistence")
    with tempfile.TemporaryDirectory() as td:
        st = StorageStore(td)
        check("toggle turns recording on", st.toggle("A:/mnt/disk1") is True)
        check("toggle turns recording off", st.toggle("A:/mnt/disk1") is False)
        st.toggle("A:/mnt/disk1")
        st.tick(time.time(), {"A:/mnt/disk1": {"name": "/mnt/disk1", "server": "A",
                                               "type": "xfs", "size": 100, "used": 50}})
        st.delete("A:/mnt/disk1")
        reloaded = StorageStore(td)
        check("deleted key is gone after reload", "A:/mnt/disk1" not in reloaded.data["pools"])


# ---------------------------------------------------------------------------
# 4) _fetch URL guard + storage/host access helpers
# ---------------------------------------------------------------------------
def test_fetch_scheme_guard():
    print("_fetch only opens http/https")
    m = Monitor([{"name": "A", "url": None}])
    for bad in ("file:///etc/passwd", "ftp://x/1", "gopher://x", "/etc/passwd", ""):
        try:
            m._fetch(bad)
            check(f"rejects {bad!r}", False, "no exception")
        except ValueError:
            check(f"rejects {bad!r}", True)
        except Exception as e:
            check(f"rejects {bad!r}", False, f"{type(e).__name__}: {e}")
    try:
        m._fetch(_dead_url())
        check("http:// passes the guard", False, "unexpected success")
    except ValueError as e:
        check("http:// passes the guard", False, f"guard blocked http: {e}")
    except Exception:
        check("http:// passes the guard", True)


# ---------------------------------------------------------------------------
# 5) TerminalManager (targets, keys, validation)
# ---------------------------------------------------------------------------
def test_terminal_manager():
    print("TerminalManager targets/keys")
    with tempfile.TemporaryDirectory() as td:
        tm = TerminalManager(td, ttyd_user="", ttyd_pass="")
        check("disabled without TTYD_USER/TTYD_PASS", tm.enabled() is False)
        check("no ttyd process spawned when disabled", tm.procs == {})
        check("login refused when disabled", tm.check_login("u", "p") is False)

        err = tm.set_targets([{"name": "TRU", "host": "192.168.2.100", "user": "root",
                               "key": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"}])
        check("valid target accepted", err == "", err)
        st = tm.status()
        t0 = st["targets"][0]
        check("target stored", (t0["name"], t0["host"], t0["user"])
              == ("TRU", "192.168.2.100", "root"))
        check("has_key set", t0["has_key"] is True)
        check("key is NEVER returned to the API",
              "key" not in t0 and "BEGIN OPENSSH" not in json.dumps(st))
        check("first target port is base port", t0["port"] == TerminalManager.BASE_PORT)
        keyfile = os.path.join(td, "ssh", "TRU")
        check("key file written", os.path.isfile(keyfile))
        check("key file mode 0600", oct(os.stat(keyfile).st_mode & 0o777) == "0o600")
        check("known_hosts dir exists",
              os.path.isdir(os.path.join(td, "ssh")))

        err = tm.set_targets([{"name": "A", "host": "1.2.3.4; rm -rf /", "user": "root"}])
        check("shell metacharacters in host rejected", err != "")
        err = tm.set_targets([{"name": "A", "host": "1.2.3.4/../x", "user": "root"}])
        check("slash in host rejected", err != "")
        err = tm.set_targets([{"name": "A", "user": "root"}])
        check("missing host rejected", err != "")
        err = tm.set_targets([{"host": "1.2.3.4", "user": "root"}])
        check("missing name rejected", err != "")
        check("rejected input changed nothing",
              tm.targets[0]["name"] == "TRU")

        err = tm.set_targets([{"name": "TRU", "host": "192.168.2.100", "user": "admin"}])
        check("saving without key keeps the old key file",
              err == "" and os.path.isfile(keyfile) and tm.status()["targets"][0]["has_key"])

        err = tm.set_targets([{"name": "TRU", "host": "192.168.2.100", "user": "admin",
                               "delete_key": True}])
        check("delete_key removes the key file", err == "" and not os.path.exists(keyfile))
        check("delete_key clears has_key", tm.status()["targets"][0]["has_key"] is False)

        err = tm.set_targets([{"name": "TRU", "host": "192.168.2.100", "user": "root"}])
        st = tm.status([{"name": "TRU", "url": "http://192.168.2.100:8091"},
                        {"name": "Extra", "url": "http://192.168.2.101:8091"}])
        sug = [t for t in st["targets"] if t.get("suggested")]
        check("only servers without a target are suggested",
              [t["name"] for t in sug] == ["Extra"], [t["name"] for t in st["targets"]])
        check("suggested host derived from the monitor url",
              sug[0]["host"] == "192.168.2.101" and sug[0]["user"] == "")
        check("suggested rows never get a port", sug[0]["port"] == 0)

        check("_suggested_host: local", web._suggested_host("local") == "localhost")
        check("_suggested_host: empty", web._suggested_host("") == "localhost")
        check("_suggested_host: none", web._suggested_host(None) == "localhost")
        check("_suggested_host: strips scheme+port",
              web._suggested_host("https://host.lan:8091/x") == "host.lan")
        check("_suggested_host: ipv6",
              web._suggested_host("http://[fe80::1]:8091") == "fe80::1")
        check("_safe_name sanitises",
              TerminalManager._safe_name("a b/c;d") == "a_b_c_d")


# ---------------------------------------------------------------------------
# 6) config dir detection + template
# ---------------------------------------------------------------------------
def test_config_dir_helpers():
    print("config dir / template helpers")
    with tempfile.TemporaryDirectory() as td:
        check("CONFIG_DIR env wins", web.resolve_config_dir(td) == td)
        check("template written once", web.init_config_template(td) in (True, False))
        p = web.config_path(td)
        if os.path.exists(p):
            first = open(p).read()
            check("second call does not overwrite", web.init_config_template(td) is False
                  and open(p).read() == first)
            check("template is valid-yaml-but-no-servers", _valid_servers(first) is None)
        check("load_servers falls back to local Main",
              [s["name"] for s in web.load_servers("", td)] == ["Main"])


# ---------------------------------------------------------------------------
# 7) agent selftest (ss/route parsers) still passes
# ---------------------------------------------------------------------------
def test_agent_self_test():
    print("agent parsers (fixture self-test)")
    try:
        agent._self_test()
        check("agent._self_test() passes", True)
    except AssertionError as e:
        check("agent._self_test() passes", False, str(e))


# ---------------------------------------------------------------------------
# 8) CPU/RAM per process: same-name PIDs are SUMMED, scale = one core
# ---------------------------------------------------------------------------
def test_agent_sys_cpu_aggregation():
    """Zwei PIDs mit gleichem comm-Namen muessen summiert werden (max() zeigte
    nur den groessten -> halbierte Werte), und die Prozess-Prozente beziehen
    sich auf EINEN Kern (100 % = ein Thread voll, wie top/htop/docker stats)."""
    print("agent CPU per process (sum across same-name PIDs + one-core scale)")
    import subprocess
    burn = "import time\nt=time.time()\nwhile time.time()-t<40: pass"
    procs = [subprocess.Popen([sys.executable, "-c", burn],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for _ in range(2)]
    try:
        s = agent.Sampler()
        s._sys_tick(time.monotonic(), 1.0)          # Baseline (no deltas yet)
        time.sleep(6.0)
        out = s._sys_tick(time.monotonic() + 6.0, 6.0)
        check("host thread count detected", s._ncpu >= 2, s._ncpu)
        rows = [pr for pr in out["procs"] if pr["name"].startswith("python")]
        row = rows[0] if rows else None
        check("row for the burner processes exists", row is not None,
              [pr["name"] for pr in out["procs"][:3]])
        cpu = row["cpu"] if row else 0.0
        # zwei Burner je ~1 Kern -> ~200 %; mit max() waeren es ~100 %
        check("same-name PIDs are summed, not maxed (2 threads ~200 %)", 
              130.0 <= cpu <= 400.0, cpu)
        check("host cpu keeps the all-cores scale (0-100)",
              0.0 <= out["cpu"] <= 100.0, out["cpu"])
    finally:
        for pr in procs:
            pr.kill()
            pr.wait(timeout=5)


def main() -> int:
    for fn in (test_dashboard_without_history,
               test_dashboard_history_shape,
               test_dashboard_keys_per_server,
               test_dashboard_one_offline_one_online,
               test_poll_one_error_tuple,
               test_process_history,
               test_set_servers_keeps_and_cleans,
               test_parsing,
               test_storage_bump,
               test_storage_month_rollover,
               test_storage_m1_32_days,
               test_storage_short_points_survive,
               test_storage_m1_migration,
               test_storage_toggle_delete_roundtrip,
               test_fetch_scheme_guard,
               test_terminal_manager,
               test_config_dir_helpers,
               test_agent_self_test,
               test_agent_sys_cpu_aggregation):
        fn()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("ALL BACKEND CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
