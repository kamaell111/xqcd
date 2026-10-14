# OLT Manager — Dockerfile
FROM python:3.11-slim

WORKDIR /app

# System deps (untuk netmiko, paramiko, pysnmp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy backend + frontend
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# Data dir
RUN mkdir -p /app/backend/data

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/api/health')" || exit 1

EXPOSE 8002
WORKDIR /app/backend

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
