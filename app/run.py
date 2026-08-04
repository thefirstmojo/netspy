#!/usr/bin/env python3
"""NetMon Entrypoint. ROLE=web (Dashboard + lokaler Sampler) oder ROLE=agent."""
import os
import time

ROLE = os.environ.get("ROLE", "web").lower().strip()

if ROLE == "agent":
    from agent import Sampler, start_agent
    s = Sampler(uplink=os.environ.get("UPLINK", ""),
                docker_sock=os.environ.get("DOCKER_SOCK", ""))
    port = int(os.environ.get("AGENT_PORT", "8091"))
    start_agent(s, port=port, token=os.environ.get("AGENT_TOKEN", ""))
    print(f"NetMon-Agent auf :{port}")
    while True:
        time.sleep(3600)
else:
    from web import main
    main()
