# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM python:3.12-slim-bookworm AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.production
# GeoDjango needs GDAL/GEOS/PROJ even though PostgreSQL itself is external.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgdal32 libgeos-c1v5 libproj25 ca-certificates fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 dazzy \
    && useradd --uid 10001 --gid dazzy --no-create-home dazzy
WORKDIR /app
COPY --from=dependencies /app/.venv /app/.venv
COPY . .
RUN chmod 755 /app/deploy/entrypoint.sh \
    && mkdir -p /app/staticfiles /app/run \
    && chown dazzy:dazzy /app/staticfiles /app/run
USER dazzy
EXPOSE 8000
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["api"]
