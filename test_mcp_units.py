#!/usr/bin/env python3
"""NetSpy MCP unit + protocol tests (no browser, no external network).

Run directly:
    python3 test_mcp_units.py

Covers the read-only MCP interface (app/mcp_server.py):
  * the listener stays off unless MCP_TOKEN is set
  * every request needs the token (401 without, 401 with a wrong one)
  * JSON-RPC basics: initialize handshake, tools/list, unknown method, batch,
    notification (202), GET on the endpoint (405), health probe (200)
  * the tools themselves: overview warnings, process merging/sorting/limits,
    peer parsing, containers, interfaces, storage spans, time series, and
    teachable errors for unknown hosts / bad arguments
  * a hard guarantee: no tool name can write, execute or delete anything
"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import mcp_server as M  # noqa: E402
from web import Monitor, StorageStore  # noqa: E402

FAILS: list = []
CHECKS = [0]


def check(label: str, cond: bool) -> None:
    CHECKS[0] += 1
    if not cond:
        FAILS.append(label)
        print(f"    FAIL: {label}")


def snap(host: str = "Tower", **over) -> dict:
    """A snapshot with the exact shape the agent sends."""
    base = {
        "version": M.VERSION,
        "hostname": host,
        "ts": 1795347985.2,
        "totals": {"rx": 1000.0, "tx": 200.0},
        "rest": {"rx": 0.0, "tx": 0.0},
        "interfaces": [
            {"name": "bond0", "rx": 900.0, "tx": 100.0, "uplink": True, "container": None},
            {"name": "veth1234", "rx": 50.0, "tx": 50.0, "uplink": False, "container": "sabnzbd"},
        ],
        "processes": [
            {"name": "smbd[192.168.2.99]", "rx": 1200.0, "tx": 340.0, "container": None, "conns": 4},
            {"name": "nginx", "rx": 4160.0, "tx": 28513.7, "container": None, "conns": 12},
            {"name": "python3", "rx": 10.0, "tx": 20.0, "container": "sabnzbd", "conns": 0},
        ],
        "containers": [{"name": "sabnzbd", "rx": 318449.8, "tx": 69707645.3},
                       {"name": "nginx", "rx": 4160.0, "tx": 1000.0}],
        "disk": [{"name": "shfs", "read": 16977074.8, "write": 154392467.3,
                  "read10": 25529075.7, "write10": 125496621.1, "container": None}],
        "system": {"cpu": 39.3, "cpu10": 30.0, "mem_total": 50414718976, "mem_used": 8 * 1024 ** 3,
                   "procs": [{"name": "nginx", "cpu": 12.0, "cpu10": 10.0, "mem": 4096},
                             {"name": "shfs", "cpu": 200.0, "cpu10": 150.0, "mem": 8192}]},
        "storage": [{"name": "/mnt/cache", "path": "/mnt/cache", "size": 1000, "used": 400,
                     "free": 600, "type": "zfs", "parent": None, "level": 0}],
        "storage_host": True,
        "ss_ok": True,
        "ss_error": "",
        "pid1": "init",
        "proc_count": 1204,
        "docker": {"socket": True, "containers": 38, "macs": 37, "fdb": 183, "veths_mapped": 35},
    }
    base.update(over)
    return base


def make_monitor(with_store: bool = True) -> Monitor:
    m = Monitor([{"name": "Unraid", "url": "http://127.0.0.1:1"},
                 {"name": "TNAS", "url": "http://127.0.0.1:2"}])
    now = 1795347985.0
    m.snaps["Unraid"] = snap()
    m.snaps["TNAS"] = snap("truenas", totals={"rx": 500.0, "tx": 50.0},
                           processes=[{"name": "smbd[192.168.2.50]", "rx": 500.0, "tx": 50.0,
                                       "container": None, "conns": 2},
                                      {"name": "nginx", "rx": 1.0, "tx": 2.0,
                                       "container": None, "conns": 1}],
                           containers=[{"name": "plex", "rx": 100.0, "tx": 100.0}],
                           storage=[{"name": "tank", "path": "/mnt/tank", "size": 2000,
                                     "used": 1900, "free": 100, "type": "zfs",
                                     "parent": None, "level": 0}])
    m.online["Unraid"] = True
    m.online["TNAS"] = False
    m.errors["Unraid"] = ""
    m.errors["TNAS"] = "timeout"
    for i in range(300):
        m.history["Unraid"].append((now + i, float(i), float(i) * 2))
    m.cpu_history["Unraid"].append((now, 39.3, 30.0))
    m.mem_history["Unraid"].append((now, 8 * 1024 ** 3, 16 * 1024 ** 3))
    m.latency["Unraid"].append((now, 12))
    if with_store:
        store = StorageStore(tempfile.mkdtemp(prefix="netspy-mcp-"))
        store.data = {
            "enabled": ["Unraid:/mnt/cache"],
            "pools": {"Unraid:/mnt/cache": {
                "name": "/mnt/cache", "server": "Unraid", "type": "zfs",
                "size": 1000, "used": 400,
                "h24": [[now, 400], [now - 3600, 390]],
                "d7": [[now, 400]], "m1": [[now, 400]], "m": [[now, 400]],
            }},
        }
        m.storage_store = store
    return m


def call(mon, name: str, **args):
    """Run a tool through the real dispatcher; returns (payload, is_error)."""
    resp = M.dispatch(mon, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})
    result = resp["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


# --------------------------------------------------------------------------
def test_no_write_tools() -> None:
    print("tool registry is read-only and complete")
    names = [t["name"] for t in M.public_tools()]
    check("eight tools exposed", len(names) == 8)
    check("all names are netspy_*", all(n.startswith("netspy_") for n in names))
    forbidden = ("set", "write", "delete", "remove", "run", "exec", "start", "stop",
                 "restart", "terminal", "command", "shell", "kill")
    bad = [n for n in names if any(word in n for word in forbidden)]
    check(f"no mutating tool names (found {bad})", not bad)
    check("every tool has a description", all(t.get("description") for t in M.public_tools()))
    check("every tool has an input schema",
          all(isinstance(t.get("inputSchema"), dict) for t in M.public_tools()))
    check("no callables leak into tools/list",
          not any(k.startswith("_") for t in M.public_tools() for k in t))


def test_initialize() -> None:
    print("initialize handshake")
    mon = make_monitor()
    resp = M.dispatch(mon, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18",
                                       "capabilities": {}, "clientInfo": {"name": "t"}}})
    res = resp["result"]
    check("echoes the known protocol version", res["protocolVersion"] == "2025-06-18")
    check("server name is netspy", res["serverInfo"]["name"] == "netspy")
    check("advertises the tools capability", "tools" in res["capabilities"])
    check("server version matches VERSION", res["serverInfo"]["version"] == M.VERSION)
    old = M.dispatch(mon, {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                           "params": {"protocolVersion": "1999-01-01"}})["result"]
    check("unknown protocol version falls back to ours",
          old["protocolVersion"] == M.PROTOCOL_VERSIONS[0])
    check("ping answers", M.dispatch(mon, {"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"] == {})
    check("notification produces no response",
          M.dispatch(mon, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)
    err = M.dispatch(mon, {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
    check("unknown method is -32601", err["error"]["code"] == -32601)


def test_overview() -> None:
    print("overview: totals, versions and warnings")
    mon = make_monitor()
    payload, is_err = call(mon, "netspy_overview")
    check("overview succeeds", not is_err)
    check("both hosts listed", len(payload["hosts"]) == 2)
    check("totals are summed", payload["total_rx_bps"] == 1500.0 and payload["total_tx_bps"] == 250.0)
    check("offline host reported", any("TNAS: offline (timeout)" in w for w in payload["warnings"]))
    check("agent version surfaced", payload["hosts"][0]["agent_version"] == M.VERSION)

    mon.snaps["Unraid"].update({"ss_ok": False, "ss_error": "ss failed", "storage_host": False,
                                "proc_count": 1, "pid1": "python"})
    payload, _ = call(mon, "netspy_overview")
    check("failed socket accounting is a warning",
          any("socket accounting failed (ss failed)" in w for w in payload["warnings"]))
    check("missing host access is a warning with the fix",
          any("pid: host" in w and "proc_count=1" in w for w in payload["warnings"]))
    one, _ = call(mon, "netspy_overview", host="unraid")
    check("host filter is case-insensitive", len(one["hosts"]) == 1 and one["hosts"][0]["name"] == "Unraid")


def test_agent_health() -> None:
    print("agent health: diagnostics + version mismatch")
    mon = make_monitor()
    mon.snaps["Unraid"]["version"] = "0.7.44"
    payload, is_err = call(mon, "netspy_agent_health")
    check("agent health succeeds", not is_err)
    row = payload["hosts"][0]
    check("pid1 is reported", row["pid1"] == "init")
    check("proc_count is reported", row["proc_count"] == 1204)
    check("docker status is reported", row["docker"]["containers"] == 38)
    check("latency of the last poll is reported", row["poll_latency_ms"] == 12)
    check("version mismatch is flagged",
          "version_mismatch" in row and "0.7.44" in row["version_mismatch"])
    check("no mismatch flag when versions match", "version_mismatch" not in payload["hosts"][1])


def test_processes() -> None:
    print("processes: merge, sort, limit, min_rate")
    mon = make_monitor()
    payload, is_err = call(mon, "netspy_processes")
    check("rate kind succeeds", not is_err)
    check("units are bytes/s", payload["units"] == "bytes/s")
    top = payload["rows"][0]
    check("highest total first", top["name"] == "nginx")
    merged = next(r for r in payload["rows"] if r["name"] == "nginx")
    check("same process name merged across hosts", len(merged["hosts"]) == 2)
    check("per-host values kept",
          merged["hosts"]["Unraid"]["conns"] == 12 and merged["hosts"]["TNAS"]["conns"] == 1)
    peers = [r for r in payload["rows"] if r["name"].startswith("smbd[")]
    check("different peer addresses stay separate rows", len(peers) == 2)
    check("container is carried over", next(r for r in payload["rows"] if r["name"] == "python3")["container"] == "sabnzbd")

    limited, _ = call(mon, "netspy_processes", limit=1)
    check("limit truncates and reports it", len(limited["rows"]) == 1 and limited["truncated"])
    filtered, _ = call(mon, "netspy_processes", min_rate_bps=1000)
    check("min_rate drops small rows", all(r["total"] >= 1000 for r in filtered["rows"]))

    cpu, _ = call(mon, "netspy_processes", kind="cpu")
    check("cpu kind sorts by percent and merges hosts",
          cpu["rows"][0]["name"] == "shfs" and cpu["rows"][0]["total"] == 400.0
          and cpu["units"] == "percent of one core")
    mem, _ = call(mon, "netspy_processes", kind="mem")
    check("mem kind sums bytes across hosts", mem["rows"][0]["total"] == 16384 and mem["units"] == "bytes")
    disk, _ = call(mon, "netspy_processes", kind="disk")
    check("disk kind exposes read/write", disk["rows"][0]["hosts"]["Unraid"]["read_bps"] == 16977074.8)

    bad, is_err = call(mon, "netspy_processes", kind="gpu")
    check("bad kind is a tool error, not a crash", is_err and "kind must be" in bad["error"])


def test_peers() -> None:
    print("peers: parse addresses out of process names")
    mon = make_monitor()
    payload, is_err = call(mon, "netspy_peers")
    check("peers succeed", not is_err)
    peers = [r["peer"] for r in payload["rows"] if r["peer"]]
    check("smbd peers parsed", "192.168.2.99" in peers and "192.168.2.50" in peers)
    check("rows sorted by rate", payload["rows"][0]["process"] == "nginx")
    check("note explains the comm limit", "15 characters" in payload["note"])
    one, _ = call(mon, "netspy_peers", host="TNAS")
    check("host filter works for peers", all(r["host"] == "TNAS" for r in one["rows"]))
    searched, _ = call(mon, "netspy_peers", search="192.168.2.99")
    check("search filters by name", len(searched["rows"]) == 1
          and searched["rows"][0]["peer"] == "192.168.2.99")


def test_containers_interfaces() -> None:
    print("containers + interfaces")
    mon = make_monitor()
    payload, is_err = call(mon, "netspy_containers")
    check("containers succeed", not is_err)
    check("densest container first", payload["rows"][0]["name"] == "sabnzbd")
    check("docker status per host", payload["docker"]["Unraid"]["containers"] == 38)
    ifaces, _ = call(mon, "netspy_interfaces")
    check("uplink flag present", any(r["uplink"] for r in ifaces["rows"]))
    check("veth marked as non-uplink",
          all(not r["uplink"] for r in ifaces["rows"] if r["name"].startswith("veth")))


def test_storage() -> None:
    print("storage: current values and history spans")
    mon = make_monitor()
    payload, is_err = call(mon, "netspy_storage")
    check("storage succeeds", not is_err)
    keys = [r["key"] for r in payload["rows"]]
    check("both mounts visible", "Unraid:/mnt/cache" in keys and "TNAS:tank" in keys)
    check("sorted by fill level", payload["rows"][0]["key"] == "TNAS:tank")
    cache = next(r for r in payload["rows"] if r["key"] == "Unraid:/mnt/cache")
    check("used_pct computed", cache["used_pct"] == 40.0)
    check("free bytes computed", cache["free_bytes"] == 600)
    check("recording flag from the store", cache["recording"] is True)
    check("span=now has no series", "h24" not in cache)

    hist, _ = call(mon, "netspy_storage", span="h24")
    h = next(r for r in hist["rows"] if r["key"] == "Unraid:/mnt/cache")
    check("h24 series attached", h["h24"] == [[1795347985, 400], [1795344385, 390]])
    bad, is_err = call(mon, "netspy_storage", span="yearly")
    check("bad span is a tool error", is_err and "span must be" in bad["error"])

    mon.storage_store = None
    no_store, is_err = call(mon, "netspy_storage")
    check("missing store is a clear error", is_err and "storage store unavailable" in no_store["error"])


def test_history() -> None:
    print("history: series, units and point caps")
    mon = make_monitor()
    payload, is_err = call(mon, "netspy_history", metric="rx", points=50)
    check("history succeeds", not is_err)
    series = payload["series"]["Unraid"]
    check("point cap respected", len(series["values"]) == 50)
    check("newest sample kept", series["values"][-1] == 299.0)
    check("timestamps are ints", all(isinstance(t, int) for t in series["ts"]))
    check("unit for rx", payload["unit"] == "bytes/s")
    tx, _ = call(mon, "netspy_history", metric="tx", points=5)
    check("tx picks the third column", tx["series"]["Unraid"]["values"][-1] == 598.0)
    cpu, _ = call(mon, "netspy_history", metric="cpu")
    check("cpu series", cpu["series"]["Unraid"]["values"] == [39.3])
    lat, _ = call(mon, "netspy_history", metric="latency")
    check("latency series in ms", lat["unit"] == "milliseconds" and lat["series"]["Unraid"]["values"] == [12.0])
    bad, is_err = call(mon, "netspy_history", metric="watts")
    check("bad metric is a tool error", is_err and "metric must be" in bad["error"])


def test_errors() -> None:
    print("error handling: unknown host/tool")
    mon = make_monitor()
    payload, is_err = call(mon, "netspy_overview", host="Nope")
    check("unknown host is a tool error", is_err)
    check("error lists the known hosts", "Unraid" in payload["error"] and "Nope" in payload["error"])
    resp = M.dispatch(mon, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                            "params": {"name": "netspy_nope", "arguments": {}}})
    check("unknown tool is -32602", resp["error"]["code"] == -32602)
    bad_args = M.dispatch(mon, {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                "params": {"name": "netspy_overview", "arguments": "nope"}})
    check("non-object arguments rejected", bad_args["error"]["code"] == -32602)
    mon.storage_store = None
    broken = M.dispatch(mon, {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                              "params": {"name": "netspy_storage", "arguments": {"host": "Unraid"}}})
    check("a failing tool answers isError instead of breaking the listener",
          broken["result"]["isError"] is True
          and "storage store unavailable" in json.loads(broken["result"]["content"][0]["text"])["error"])


def test_http() -> None:
    print("HTTP layer: auth, methods, health, batch, body cap")
    mon = make_monitor()
    check("no listener without a token", M.start_mcp(mon, port=0, token="") is None)

    srv = M.start_mcp(mon, port=0, token="secret-token")
    check("listener started with a token", srv is not None)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def post(body, token="secret-token", raw_headers=None):
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-MCP-Token"] = token
        if raw_headers:
            headers.update(raw_headers)
        req = urllib.request.Request(base + M.MCP_PATH, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    status, body = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)
    check("missing token is 401", status == 401)
    status, body = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="wrong")
    check("wrong token is 401", status == 401)
    check("401 body does not leak the expected token", b"secret" not in body)

    status, body = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    listed = json.loads(body)["result"]["tools"]
    check("authorized tools/list works", status == 200 and len(listed) == 8)

    status, body = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "netspy_overview", "arguments": {}}})
    payload = json.loads(json.loads(body)["result"]["content"][0]["text"])
    check("tools/call over HTTP", status == 200 and len(payload["hosts"]) == 2)

    status, body = post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token="secret-token")
    check("notification is 202 with an empty body", status == 202 and body == b"")

    status, body = post([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    check("batch answered with a list", status == 200 and len(json.loads(body)) == 2)

    req = urllib.request.Request(base + M.MCP_PATH, method="GET")
    try:
        urllib.request.urlopen(req, timeout=5)
        code = 200
    except urllib.error.HTTPError as exc:
        code = exc.code
    check("GET on the endpoint is 405 (no SSE)", code == 405)

    with urllib.request.urlopen(base + M.HEALTH_PATH, timeout=5) as r:
        health = json.loads(r.read())
    check("health probe needs no token", r.status == 200 and health["ok"] is True)
    check("health probe reports the tool count", health["tools"] == 8)

    req = urllib.request.Request(base + "/elsewhere", method="GET")
    try:
        urllib.request.urlopen(req, timeout=5)
        code = 200
    except urllib.error.HTTPError as exc:
        code = exc.code
    check("unknown path is 404", code == 404)

    # Body cap: shrink the limit instead of pushing a real megabyte around.
    original = M.MAX_BODY
    M.MAX_BODY = 64
    try:
        status, _ = post({"jsonrpc": "2.0", "id": 1, "method": "ping",
                          "params": {"pad": "x" * 200}})
        check("oversized body is 413", status == 413)
    finally:
        M.MAX_BODY = original

    data = b"{not json"
    req = urllib.request.Request(base + M.MCP_PATH, data=data, method="POST",
                                 headers={"X-MCP-Token": "secret-token"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            code, parsed = r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        code, parsed = exc.code, json.loads(exc.read())
    check("parse error is -32700", code == 400 and parsed["error"]["code"] == -32700)

    bearer = {"Authorization": "Bearer secret-token"}
    status, _ = post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None, raw_headers=bearer)
    check("Authorization: Bearer is accepted", status == 200)

    srv.shutdown()


def main() -> int:
    for fn in (test_no_write_tools, test_initialize, test_overview, test_agent_health,
               test_processes, test_peers, test_containers_interfaces, test_storage,
               test_history, test_errors, test_http):
        fn()
    print()
    print(f"{CHECKS[0]} checks")
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("ALL MCP CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
