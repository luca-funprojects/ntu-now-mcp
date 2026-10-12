# Playwright's image ships a matching Chromium, so headless re-login works on a server.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY now_mcp ./now_mcp
RUN pip install --no-cache-dir . && mkdir -p /app/state /app/data

ENV NOW_STATE_DIR=/app/state \
    NOW_DATA_DIR=/app/data \
    NOW_HTTP_HOST=0.0.0.0 \
    NOW_HTTP_PORT=8765 \
    PYTHONUNBUFFERED=1

EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz').status==200 else 1)"
CMD ["python", "-m", "now_mcp.server", "--http"]
