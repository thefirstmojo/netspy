# NetSpy — Live-Netzwerk-Überwachung für Unraid + TrueNAS

Web-Dashboard, das **Interface- und Pro-Prozess-Netzwerkraten** von Unraid und
TrueNAS live anzeigt (1 s Sampling). **Ein Image, zwei Rollen, eine Compose.**

| Rolle | Aufgabe | Wo |
|---|---|---|
| `ROLE=web` | Web-UI (:8090) + lokaler Sampler + Agent-API (:8091) | Unraid |
| `ROLE=agent` | Nur Sampler + Agent-API (:8091) | TrueNAS |

## Architektur

```
Unraid 10.10.10.101                    TrueNAS 10.10.10.100
┌──────────────────────────────┐        ┌─────────────────────────┐
│ netspy (ROLE=web)            │        │ netspy (ROLE=agent)     │
│  :8090 Web-UI                │◄──────►│  :8091 /api/metrics      │
│  :8091 /api/metrics          │  HTTP  │  /proc + ss (inet_diag)  │
│  /proc + ss (inet_diag)      │        └─────────────────────────┘
└──────────────────────────────┘
```

Alles ist über **eine `docker-compose.yml`** und Umgebungsvariablen gesteuert
(`.env.example`): `ROLE` entscheidet, was der Container tut, `SERVERS` sagt dem
Dashboard, wo die Verbindungen hingehen.

## Deploy Unraid

```bash
git clone <repo-url> && cd netspy   # oder Compose-Datei direkt nutzen
cp .env.example .env                # ROLE=web, SERVERS anpassen
docker compose up -d --build
# UI: http://10.10.10.101:8090
```

## Deploy TrueNAS (Portainer)

1. **Portainer** → Stacks → **Add stack**
2. **Repository**: Git-URL dieses Repos + Pfad `docker-compose.yml`
3. **Environment variables** setzen:
   - `ROLE=agent`
   - `AGENT_PORT=8091`
   - `UPLINK=br0`
   - `AGENT_TOKEN=<gleiches Token wie Unraid>`
   - `DOCKER_SOCK`-Mount wird automatisch übersprungen (`optional: true`)
4. Deploy → Test: `curl http://10.10.10.100:8091/api/metrics`

> Falls die Compose-Version `optional: true` nicht kennt: Volumes-Block im
> Portainer-Stack-Editor entfernen (Container-Namen gibt's auf TrueNAS eh nicht).

## Image statt Build (optional)

Nach `git tag v0.1 && git push --tags` baut GitHub Actions das Image und
publiziert es auf `ghcr.io/<user>/netspy:latest`. Dann nur noch
`NETSPY_IMAGE=ghcr.io/<user>/netspy:latest` setzen — kein `--build` nötig.

## Funktionsweise

- **Interface-Raten:** `/proc/net/dev` (kumulative Zähler, Delta pro Sekunde).
  `UPLINK` bestimmt, welche Interfaces als „Gesamt-Traffic" zählen (Standard
  `br0`; Auto-Fallback: br0 → bond0 → erstes eth*/en*).
- **Pro-Prozess-Raten (TCP):** `ss -tinp` (inet_diag) liefert kumulative
  Byte-Zähler pro Socket inkl. PID. Delta pro Sekunde, aggregiert nach
  Prozessname. Zuordnung PID → Container (optional) über den read-only
  Docker-Socket (Netzwerk-Namespace des Container-Hauptprozesses).
- **Nicht zugeordnet:** UDP, Kernel-Threads (nfsd/kworker), kurze Verbindungen
  → Zeile „- nicht zugeordnet (Kernel/UDP) -".
- **History:** Ringbuffer 1 h im Speicher des Web-Containers (kein DB-Bedarf).
- **Hinweis Bridge-Container:** Container im Docker-Bridge-Netz sind aus dem
  Host-Namespace nicht per Prozess sichtbar — deren Traffic erscheint stattdessen
  als eigenes `veth`-Interface in der Interface-Liste (per-Container-Rate).

## Konfiguration (Env)

| Variable | Default | Beschreibung |
|---|---|---|
| `ROLE` | `web` | `web` oder `agent` |
| `SERVERS` | `Unraid=local` | `Name=local;Name=http://host:8091` |
| `UPLINK` | `br0` (Auto) | Kommagetrennt, z. B. `br0,bond0` |
| `WEB_PORT` | `8090` | Web-UI |
| `AGENT_PORT` | `8091` | Agent-API |
| `AGENT_TOKEN` | leer | Header `X-Agent-Token` (muss auf beiden Hosts gleich sein) |
| `NETSPY_IMAGE` | `netspy:latest` | Vorgebautes Image (z. B. GHCR) |
| `NETSPY_CONTAINER` | `netspy` | Container-Name |

## Sicherheit

- `pid: host` + Root ist nötig, um fremde PIDs zu lesen.
- Abgemildert: `cap_drop: [ALL]`, nur `SYS_PTRACE` zusätzlich, Docker-Socket
  read-only und optional gemountet.
- `AGENT_TOKEN` setzen, damit der Agent nicht offen im LAN hängt.

## Bekannte Grenzen (bewusst)

- **Nur TCP pro Prozess:** UDP (DNS, QUIC/Streaming) und Kernel-Traffic
  (nfsd/kworker) landen in der Rest-Zeile.
- **1 s Raster:** kurze Bursts werden gemittelt.
- **Doppelt-Zählung vermeiden:** `UPLINK` auf das Top-Level-Interface setzen
  (`br0`), nicht Bond-Slaves einzeln summieren.

## Selbsttest

```bash
python3 app/agent.py --selftest   # parse_ss-Parser gegen Fixture
```

## Ausbau-Ideen

- SQLite/InfluxDB statt Ringbuffer (History über Reboots)
- Per-Container-Sicht (veth → Container) als eigene Ansicht
- E-Mail/Telegram-Alert bei Schwellwerten
