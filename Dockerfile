# NetSpy — Netzwerk-Überwachung (Unraid + TrueNAS)
# Ein Image, zwei Rollen (ROLE=web | ROLE=agent). Keine Python-Abhängigkeiten (nur stdlib).

# slim (trixie) ist wieder ok: kein zfsutils mehr nötig (siehe RUN-Kommentar)
FROM python:3.13-slim

# ss (iproute2) wird für die Per-Prozess-Messung (inet_diag) benötigt.
# openssh-client: ssh für das ttyd-Web-Terminal (Zugriff auf die Hosts).
# Kein zfsutils nötig: Füllstände liest der Agent über /proc/1/root
# (pid: host + SYS_PTRACE, im Compose schon aktiv) — siehe _storage_snapshot.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 openssh-client curl \
    && curl -fsSL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 -o /usr/local/bin/ttyd \
    && chmod +x /usr/local/bin/ttyd \
    && rm -rf /var/lib/apt/lists/*

# pyyaml: menscheneditierbare servers.yaml für die Settings-Page
RUN pip install --no-cache-dir pyyaml

WORKDIR /app
COPY app/ /app/
COPY VERSION /app/VERSION

EXPOSE 8090 8091

CMD ["python", "run.py"]
