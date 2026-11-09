# The practice site (our own test forms). Build from the repo root:
#   docker build -f practice_site/Dockerfile -t askfirst-practice .
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PRACTICE_HOST=0.0.0.0 \
    PRACTICE_PORT=8800 \
    PRACTICE_DATA_DIR=/srv/data

WORKDIR /srv
# The same pins as pyproject.toml's "practice" extra, plus what the site imports.
RUN pip install fastapi==0.162.0 uvicorn==0.54.0 jinja2==3.1.6 python-multipart==0.0.33 \
    pydantic==2.14.2 pydantic-settings==2.16.2
COPY practice_site ./practice_site
RUN useradd --create-home --uid 10001 site && mkdir -p /srv/data && chown site:site /srv/data
USER site
EXPOSE 8800
# Render sets PORT; locally it is PRACTICE_PORT (8800).
CMD ["sh", "-c", "PRACTICE_PORT=${PORT:-$PRACTICE_PORT} exec python -m practice_site"]
