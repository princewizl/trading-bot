FROM python:3.11-slim

WORKDIR /app

# git: requirements.txt installs iqoptionapi from a pinned GitHub commit
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --shell /bin/bash botuser \
    && mkdir -p /data \
    && chown -R botuser:botuser /app /data

USER botuser

ENV BOT_DB_PATH=/data/bot.db \
    TZ=UTC \
    PYTHONUNBUFFERED=1

# Heartbeat is touched every minute by the main loop.
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if time.time() - os.path.getmtime('/data/heartbeat') < 180 else 1)"

CMD ["python", "bot_service.py"]
