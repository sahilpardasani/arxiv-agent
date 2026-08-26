FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 DATA_DIR=/app/data WEB_CONCURRENCY=2
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt
COPY --chown=app:app arxiv_agent.py main.py mcp_server.py data_store.py jobs.py legacy.py publishing.py simple_dashboard.html ./
COPY --chown=app:app papers.json papers_archive.json ./data/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-2} --limit-concurrency ${UVICORN_LIMIT_CONCURRENCY:-200} --backlog ${UVICORN_BACKLOG:-2048} --timeout-keep-alive ${KEEP_ALIVE_SECONDS:-5} --timeout-graceful-shutdown ${GRACEFUL_SHUTDOWN_SECONDS:-30} --no-server-header --no-proxy-headers"]
