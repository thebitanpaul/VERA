# Optional: containerised deploy. Render can build from this instead of the
# native Python runtime (set `runtime: docker` in render.yaml), and it also
# works on Fly.io, Cloud Run, or any container host.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vera/ ./vera/
COPY web/ ./web/
COPY data/protocols/ ./data/protocols/
COPY wsgi.py ./

# Bake the manifest into the image: the first request must not pay to parse the
# PDF corpus, and the runtime filesystem may be read-only.
RUN python -m vera.ingest --check

# Escalations are the only runtime write; give them a writable home.
ENV VERA_ESCALATION_DIR=/tmp/vera-escalations
RUN useradd --create-home --uid 10001 vera && chown -R vera:vera /app
USER vera

EXPOSE 8000
ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "gunicorn wsgi:app --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 120 --access-logfile - --error-logfile -"]
