FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Użytkownik bez uprawnień roota; UID/GID dopasuj do właściciela folderu na NAS (PUID/PGID w .env).
ARG PUID=1000
ARG PGID=10
RUN (getent group ${PGID} || groupadd -g ${PGID} bsk) && useradd -u ${PUID} -g ${PGID} -M -s /usr/sbin/nologin bsk
USER ${PUID}:${PGID}

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
