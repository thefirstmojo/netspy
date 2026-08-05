# Unraid (Community Applications) template

This folder contains the Unraid CA template for NetSpy — the **web role** with
full GUI configuration (all env vars editable in the Unraid Docker UI).

## Install / test (before CA listing)

Unraid → **Apps** → **Settings** (gear icon) → **Template Repositories** → add:

```
https://github.com/thefirstmojo/netspy
```

Then **Apps → NetSpy → Install**. All settings are editable in the GUI:
ROLE, SERVERS, UPLINK, ports, AGENT_TOKEN (masked field), Docker socket mount.

No GHCR login required — the image is public and pulls anonymously.

## What the template maps

| GUI field | Env / container | Default |
|---|---|---|
| ROLE | `ROLE` | `web` |
| SERVERS | `SERVERS` | `Unraid=local` |
| UPLINK | `UPLINK` | (empty = auto) |
| PORT:8090 | `WEB_PORT` | `8090` |
| PORT:8091 | `AGENT_PORT` | `8091` |
| AGENT_TOKEN | `AGENT_TOKEN` | (required, masked) |
| DOCKER_SOCK | `DOCKER_SOCK` | `/var/run/docker.sock` |
| Docker Socket | `/var/run/docker.sock` (ro mount) | mounted |

## The compose stays

The template is the Unraid/GUI distribution channel. For agents on other hosts
(TrueNAS, Debian, …) the `docker-compose.yml` at the repo root remains the
reference — same image, same env vars.
