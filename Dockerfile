# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM python:3.12-slim-bookworm AS dependencies
ARG TARGETARCH
ARG BUILD_DEPENDENCY_TIMEOUT=1200
ARG PYTHON_PACKAGE_INDEX=
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,id=dazzy-uv-py312-${TARGETARCH},target=/root/.cache/uv,sharing=locked \
    --mount=type=bind,source=deploy/build_dependencies.py,target=/tmp/build_dependencies.py \
    python /tmp/build_dependencies.py python

FROM python:3.12-slim-bookworm AS runtime
ARG TARGETARCH
ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security
ARG BUILD_DEPENDENCY_TIMEOUT=1200
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.production
# GeoDjango needs GDAL/GEOS/PROJ even though PostgreSQL itself is external.
RUN --mount=type=cache,id=dazzy-apt-bookworm-${TARGETARCH},target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=dazzy-apt-lists-bookworm-${TARGETARCH},target=/var/lib/apt/lists,sharing=locked \
    --mount=type=bind,source=deploy/build_dependencies.py,target=/tmp/build_dependencies.py \
    python /tmp/build_dependencies.py apt
RUN groupadd --gid 10001 dazzy \
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
