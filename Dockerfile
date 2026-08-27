# doc-translator Dockerfile — Scenario A (single-host, 5-user internal network)
#
# Build:
#   docker build -t doc-translator:0.2.0 .
#
# Run (bind to host loopback only — internal network use):
#   docker run --rm -p 127.0.0.1:8765:8765 \
#     -v doc-translator-data:/app/data \
#     doc-translator:0.2.0
#
# The container's data/ directory is a volume so uploaded documents,
# provider configurations, and translation jobs survive container
# restarts. Binding to 127.0.0.1 on the host prevents LAN access; drop
# the loopback prefix to expose to a wider network (NOT recommended
# without DOC_TRANSLATOR_REQUIRE_PUBLIC=1 + reverse-proxy TLS).

FROM python:3.12-slim AS base

# System dependencies: pymupdf/Pillow need libgl/libmupdf and other runtime libraries;
# build-essential is also installed for the rare case a wheel is missing and a local
# compile is required (commonly seen on CentOS / RHEL hosts).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first to take advantage of Docker layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Then copy the source.
COPY app/ ./app/
COPY static/ ./static/
COPY pyproject.toml ./
COPY CHANGELOG.md ./
COPY README.md ./

# Mount point for the data volume.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Default scenario: loopback bind. Loopback is unreachable from outside the container,
# so use `docker run -p` to map the container's 8765 to the host's 127.0.0.1:8765.
ENV HOST=127.0.0.1 \
    PORT=8765 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8765

# Run uvicorn directly. The SIGTERM from `docker stop` triggers FastAPI's lifespan
# graceful shutdown (waits for active tasks to drain, 30s timeout).
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "127.0.0.1", "--port", "8765", \
     "--no-server-header", "--proxy-headers"]
