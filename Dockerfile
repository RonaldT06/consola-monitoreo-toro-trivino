# Imagen de NetMon: Python + ping + zona horaria
FROM python:3.12-slim

# iputils-ping: diagnóstico "responde ping pero no SNMP" | tzdata: hora local en alertas
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
COPY netmon ./netmon

ENV PYTHONUNBUFFERED=1 \
    TZ=America/Bogota \
    NETMON_DB=/app/data/netmon.db

# 8090/tcp = tablero web | 162/udp = traps SNMP
EXPOSE 8090 162/udp

# Docker marca el contenedor "healthy" si la API responde
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/api/state', timeout=4)" || exit 1

CMD ["python", "run.py", "--config", "/app/config/config.yaml"]
