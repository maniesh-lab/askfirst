# The browser agent: `docker run ... askfirst-agent fill site:practice/job_apply`.
# python:3.14-slim plus Playwright's Chromium (the official Playwright image ships an older Python).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HEADLESS=true \
    DATA_DIR=/app/data \
    LOG_FORMAT=json

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml LICENSE ./
COPY app/__init__.py app/__init__.py
RUN pip install . && pip uninstall -y askfirst

# Chromium and the system libraries it needs (needs root, so before switching user).
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY config ./config
COPY profiles ./profiles
RUN pip install --no-deps .

RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /app/data && chown -R agent:agent /app/data
USER agent
VOLUME ["/app/data"]

ENTRYPOINT ["python", "-m", "app.cli"]
CMD ["--help"]
