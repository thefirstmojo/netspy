# NetMon — Netzwerk-Überwachung (Unraid + TrueNAS)
# Ein Image, zwei Rollen (ROLE=web | ROLE=agent). Keine Python-Abhängigkeiten (nur stdlib).

FROM python:3.13-slim

# ss (iproute2) wird für die Per-Prozess-Messung (inet_diag) benötigt
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/

EXPOSE 8090 8091

CMD ["python", "run.py"]
