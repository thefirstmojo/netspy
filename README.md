# NetSpy — Live network monitoring for Unraid + TrueNAS

Web dashboard showing **interface and per-process network throughput** of Unraid
and TrueNAS in real time (1 s sampling). **One image, two roles, one compose.**

| Role | Purpose | Where |
|---|---|---|
| `ROLE=web` | Web UI (:8090) + local sampler + agent API (:8091) | Unraid |
| `ROLE=agent` | Sampler + agent API only (:8091) | TrueNAS |

## Architecture

```
Unraid 10.10.10.10                        TrueNAS 10.10.10.20
┌──────────────────────────────┐        ┌─────────────────────────┐
│ netspy (ROLE=web)            │        │ netspy (ROLE=agent)     │
│  :8090 Web UI                │◄──────►│  :8091 /api/metrics      │
│  :8091 /api/metrics          │  HTTP  │  /proc + ss (inet_diag)  │
│  /proc + ss (inet_diag)      │        └─────────────────────────┘
└──────────────────────────────┘
```

Everything is driven by **one `docker-compose.yml`** — all configuration lives
directly in the file, **no `.env` file required**. `ROLE` decides what the
container does, `SERVERS` tells the dashboard where the connections go. Copy
the compose to each host and adjust the values there.

## Deploy on Unraid

```bash
git clone https://github.com/thefirstmojo/netspy.git && cd netspy
# edit docker-compose.yml: ROLE=web, SERVERS, UPLINK=br0, AGENT_TOKEN,
# uncomment the Docker-socket volume for per-container rows
docker compose up -d                # pulls ghcr.io/thefirstmojo/netspy:latest (public)
# UI: http://10.10.10.10:8090
```

## Deploy on another host as agent (TrueNAS via Portainer, Debian, …)

1. **Portainer** → Stacks → **Add stack** (or use docker compose directly)
2. Paste/edit `docker-compose.yml`:
   - `ROLE: agent`
   - `SERVERS: ""` (empty — the agent is polled by the web instance)
   - `UPLINK: "eth0"` (or whatever the host's uplink is)
   - `AGENT_TOKEN: "<same token as the web instance>"`
   - **TrueNAS:** uncomment the `security_opt: [apparmor:unconfined]` block —
     TrueNAS enforces the `docker-default` AppArmor profile on all containers,
     which blocks the `/proc/<pid>/fd` reads that `ss` needs for process
     attribution. This is the minimal relaxation (no `privileged` needed).
   - Debian: only needed if AppArmor is active on that host.
3. Deploy → test: `curl http://10.10.10.20:8091/api/metrics` (with the
   `X-Agent-Token` header)

> **Version pinning:** Portainer caches images. Pin the tag
> (`image: ghcr.io/thefirstmojo/netspy:v0.3.11`) for deterministic updates —
> a new tag always forces a fresh pull. With `:latest`, tick **"Pull latest
> image"** when updating the stack, otherwise the old image keeps running.

## Local build (development only)

The **prebuilt GHCR image** is the default (`ghcr.io/thefirstmojo/netspy:latest`,
built automatically on every `git tag vX.Y && git push --tags`). To build
locally:

```bash
docker build -t netspy:latest .
# change the image line in docker-compose.yml to: image: netspy:latest
docker compose up -d
```

## How it works

- **Interface rates:** `/proc/net/dev` (cumulative counters, delta per second).
  `UPLINK` selects which interfaces count as "total traffic" (default `br0`;
  auto-fallback: br0 → bond0 → first eth*/en*).
- **Per-process rates (TCP):** `ss -tinpe` (inet_diag) yields cumulative byte
  counters per socket incl. PID and socket inode. Deltas are tracked **per
  socket inode** (immune to fork/handover spikes, e.g. smbd parent/child) and
  aggregated by process name. Rates are capped at the host's physical link
  rate, so impossible artifacts are filtered out.
- **Per-container rates:** with the read-only Docker socket mounted, bridge
  containers get their own rows (veth sums → container name, e.g. `stash`).
- **Unattributed:** UDP, kernel threads (nfsd/kworker), short-lived connections
  → row "- not assigned (kernel/UDP) -".
- **History:** 1 h ring buffer in the web container's memory (no DB needed).
- **Note on bridge containers:** processes *inside* a bridge container live in
  their own network namespace and are not visible from the host — their
  traffic appears as a per-container row instead. Kernel-level SMB/NFS mounts
  (cifs client) are likewise kernel-driven and land in the "not assigned" row.

## Configuration

All values are set **directly in `docker-compose.yml`** (no `.env` file):

| Key (environment) | Default | Description |
|---|---|---|
| `ROLE` | `web` | `web` or `agent` |
| `SERVERS` | `Main=local` | `Name=local;Name=http://host:8091` |
| `UPLINK` | `br0` | Comma-separated, e.g. `br0,bond0` or `eth0` |
| `WEB_PORT` | `8090` | Web UI |
| `AGENT_PORT` | `8091` | Agent API |
| `AGENT_TOKEN` | empty | Header `X-Agent-Token` (must match on all hosts) |
| `DOCKER_SOCK` | `/var/run/docker.sock` (web) | Docker socket for container rows; `""` disables |

Plus two commented option blocks in the compose, enabled per host:
- **AppArmor** (`security_opt: [apparmor:unconfined]`) — hosts that enforce an
  AppArmor container profile (TrueNAS, Debian with AppArmor)
- **Docker socket volume** — hosts with a Docker socket, for per-container rows

## Security

- `pid: host` + root is required to read foreign PIDs.
- Mitigated by: `cap_drop: [ALL]`, only `SYS_PTRACE` added, Docker socket
  read-only and mounted only on demand (off by default).
- Set `AGENT_TOKEN` so the agent isn't openly reachable on your LAN.
- On TrueNAS, `security_opt: [apparmor:unconfined]` is the minimal relaxation
  (AppArmor profile only) instead of `privileged: true`.

## Known limits (by design)

- **TCP only per process:** UDP (DNS, QUIC/streaming) and kernel traffic
  (nfsd/kworker) land in the "not assigned" row.
- **1 s sampling:** short bursts are averaged.
- **Avoid double counting:** set `UPLINK` to the top-level interface (`br0`),
  don't sum bond slaves individually.

## Self-test

```bash
python3 app/agent.py --selftest   # parse_ss parser vs fixture
```

## Roadmap

- SQLite/InfluxDB instead of ring buffer (history across reboots)
- Per-container view (veth → container) as its own section
- Threshold alerts (email/Telegram)
