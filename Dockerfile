FROM python:3.14.8-slim-bookworm

ENV PYTHONUNBUFFERED=1 
ENV XDG_CACHE_HOME=/app/.cache
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libxss1 \
    libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock \
    && pip check
RUN python -m playwright install --no-shell chromium \
    && chmod -R a+rX /opt/ms-playwright
ENV XDG_CONFIG_HOME=/tmp/browser-config
COPY main.py analytics.py metrics.py parser.py browser_runtime.py janitor_proxy.py download_flow.py download_status.py download_queue.py download_worker.py safe_http.py security_policy.py ./
COPY tools/preflight.py tools/db_backup.py tools/browser_smoke.py tools/database_smoke.py ./tools/
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/.cache \
    && chown -R bot:bot /app/.cache /home/bot
USER bot

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+str(int(os.getenv('METRICS_PORT','8000')))+'/health',timeout=3).close()"]

CMD ["python", "main.py"]
