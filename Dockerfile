# MikroTik dashboard — tiny single-container build (works on Pi Zero 2 W and up).
# All mutable state (config.json, auth.json, history*.db) lives under $DASH_BASE,
# so the container is stateless as long as /data is a volume.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 DASH_BASE=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# app code only — never bake config.json/auth.json/databases into the image
COPY server.py .
COPY static/ static/

RUN useradd -r -u 1000 dash && mkdir -p /data
USER 1000
EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8899/login', timeout=4)" || exit 1

CMD ["python", "server.py", "--port", "8899"]
