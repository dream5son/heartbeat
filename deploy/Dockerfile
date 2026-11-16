FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 保留源码目录结构：模板路径为 heartbeat 包同级的 ../templates
COPY pyproject.toml README.md ./
COPY app.py ./
COPY heartbeat ./heartbeat
COPY templates ./templates
COPY deploy/gunicorn.conf.py ./deploy/gunicorn.conf.py

# editable 安装保证 Flask 能解析到 /app/templates；gunicorn 仅用于运行时
RUN pip install --no-cache-dir -e . gunicorn \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data /app/logs \
    && chown -R app:app /data /app/logs

ENV HEARTBEAT_DB=sqlite:////data/heartbeat.db \
    PORT=5000 \
    START_SCHEDULER=1 \
    LOG_DIR=/app/logs \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=4 \
    GUNICORN_LOG_LEVEL=info

USER app

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('PORT', '5000')).read()"

CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "heartbeat.app:app"]
