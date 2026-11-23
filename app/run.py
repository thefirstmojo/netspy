#!/usr/bin/env python3
"""NetSpy entrypoint. ROLE=web (dashboard + local sampler) or ROLE=agent."""
import os
import time

from shared import load_version, local_ipv4

ROLE = os.environ.get("ROLE", "web").lower().strip()


def log_agent_startup(sampler, port: int, token: str) -> None:
    """Same idea as web.log_startup: one block, everything a log reader needs."""
    ips = local_ipv4()
    host = ips[0] if ips else "127.0.0.1"
    more = f" (+ {', '.join(ips[1:])})" if len(ips) > 1 else ""
    uplink = ", ".join(sampler.uplink_cfg) or "auto (default route)"
    auth = "token required" if token else "NO TOKEN - anyone on the LAN can read it"
    print(f"[netspy] NetSpy agent {load_version()} - read-only host sampler "
          f"(pid {os.getpid()})", flush=True)
    print(f"[netspy] agent api : http://{host}:{port}{more}  - {auth}", flush=True)
    print(f"[netspy] uplink    : {uplink}", flush=True)
    print(f"[netspy] docker    : {sampler.docker_sock or 'off'}", flush=True)
    print("[netspy] ready - waiting for the dashboard to poll it", flush=True)


if ROLE == "agent":
    from agent import Sampler, start_agent

    sampler = Sampler(uplink=os.environ.get("UPLINK", ""),
                      docker_sock=os.environ.get("DOCKER_SOCK", ""))
    agent_port = int(os.environ.get("AGENT_PORT", "8091"))
    agent_token = os.environ.get("AGENT_TOKEN", "")
    start_agent(sampler, port=agent_port, token=agent_token)
    log_agent_startup(sampler, agent_port, agent_token)
    while True:
        time.sleep(3600)
else:
    from web import main

    main()
