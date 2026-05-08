FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DOWNLOAD_DIR=/downloads \
    CONFIG_DIR=/config \
    DEFAULT_PRESET=best-original \
    MAX_CONCURRENT_DOWNLOADS=1 \
    TZ=America/New_York

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        gosu \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements-web.txt \
    && pip install --no-cache-dir -e . \
    && python -c "import scdl_web.main; assert scdl_web.main.app"

RUN chmod +x /app/docker/entrypoint.sh

EXPOSE 8090

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["uvicorn", "scdl_web.main:app", "--host", "0.0.0.0", "--port", "8090"]
