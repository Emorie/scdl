FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DOWNLOAD_DIR=/downloads \
    CONFIG_DIR=/config \
    DEFAULT_PRESET=best-original \
    MAX_CONCURRENT_DOWNLOADS=1 \
    DOWNLOAD_DELAY_SECONDS=10 \
    MAX_RATE_LIMIT_BACKOFF_SECONDS=900 \
    MAX_CONSECUTIVE_RATE_LIMITS=8 \
    DEFAULT_PROFILE_DOWNLOAD_TYPE=uploads \
    TZ=America/New_York

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements-web.txt \
    && pip install --no-cache-dir -e .

RUN python -c "import scdl_web.main; assert scdl_web.main.app"
RUN chmod -R a+rX /app

EXPOSE 8090

CMD ["python", "-m", "uvicorn", "scdl_web.main:app", "--host", "0.0.0.0", "--port", "8090"]
