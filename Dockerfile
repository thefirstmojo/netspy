# NetMon — Netzwerk-Überwachung (Unraid + TrueNAS)
# Ein Image, zwei Rollen (ROLE=web | ROLE=agent). Keine Python-Abhängigkeiten (nur stdlib).

# slim-bookworm (NICHT slim = trixie!): trixie hat zfsutils-linux aus den
# Repos entfernt — OpenZFS gibt es nur noch bis bookworm.
FROM python:3.13-slim-bookworm

# ss (iproute2) für die Per-Prozess-Messung (inet_diag),
# zfsutils-linux für Pool-Füllstände (zpool list; braucht /dev/zfs am Host).
# HINWEIS: zfsutils-linux liegt in bookworm im CONTRIB-Repo — die slim-Images
# haben nur main aktiv, contrib wird hier ergänzt.
RUN sed -i 's/^Components: main/Components: main contrib/' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    echo "deb http://deb.debian.org/debian bookworm contrib" >> /etc/apt/sources.list 2>/dev/null || true; \
    apt-get update \
    && apt-get install -y --no-install-recommends iproute2 zfsutils-linux \
    && rm -rf /var/lib/apt/lists/*

# pyyaml: menscheneditierbare servers.yaml für die Settings-Page
RUN pip install --no-cache-dir pyyaml

WORKDIR /app
COPY app/ /app/
COPY VERSION /app/VERSION

EXPOSE 8090 8091

CMD ["python", "run.py"]
