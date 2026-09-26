# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.14.7
ARG NODE_VERSION=24
ARG UV_VERSION=0.12.19

# ----------------------------------------------------------------------------------------------------------------------
# COPY --from cannot expand a build argument, so the pinned uv image is given a stage name.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-binary

# ----------------------------------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie AS runtime-base

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG APP_UID=1000
ARG APP_GID=1000

# The virtual environment lives outside the application directory, so the src/ bind mount
# of the development target cannot hide it.
ENV APP_ROOT=/app \
    VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    DJANGO_STATIC_ROOT=/data/static

# Debian moves point releases within a suite, so an exact apt pin would break the next
# rebuild; reproducibility comes from the pinned base image instead. supervisor is not
# installed from apt: the Debian package pulls in a second Python, so it comes from PyPI
# into the virtual environment instead.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install --assume-yes --no-install-recommends \
        curl \
        gettext-base \
        nginx \
    && rm -rf /var/lib/apt/lists/*

# The runtime user takes the uid and gid of the host account that owns the mounted
# directories, so bind-mounted files stay writable without a chown at start-up. macOS gives
# every account the primary group 20, which Debian already has as "dialout".
RUN if ! getent group "${APP_GID}" > /dev/null; then groupadd --gid "${APP_GID}" app; fi \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /bin/bash app \
    && install --directory --mode 0755 --owner app --group "${APP_GID}" \
        "${APP_ROOT}" \
        /data/static \
        /etc/nginx/conf.d \
        /etc/nginx/tls \
        /opt/venv \
        /run/app \
        /var/cache/nginx/client_body \
        /var/cache/nginx/fastcgi \
        /var/cache/nginx/proxy \
        /var/cache/nginx/scgi \
        /var/cache/nginx/uwsgi \
        /var/lib/nginx \
        /var/log/nginx \
    # The packaged snippets and virtual hosts are all replaced below.
    && rm -rf /etc/nginx/sites-enabled /etc/nginx/sites-available /etc/nginx/snippets/*

COPY docker/images/app/nginx/nginx.conf            /etc/nginx/nginx.conf
COPY docker/images/app/nginx/snippets/             /etc/nginx/snippets/
COPY docker/images/app/nginx/templates/            /etc/nginx/templates/
COPY docker/images/app/supervisor/supervisord.conf /etc/supervisor/supervisord.conf
COPY --chmod=0755 docker/images/app/entrypoint.sh  /usr/local/bin/docker-entrypoint

WORKDIR ${APP_ROOT}

EXPOSE 8080 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["curl", "--fail", "--silent", "--show-error", "--insecure", "--output", "/dev/null", "https://127.0.0.1:8443/login"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["supervisord", "--configuration", "/etc/supervisor/supervisord.conf"]

# ----------------------------------------------------------------------------------------------------------------------
FROM runtime-base AS python-dependencies

COPY --from=uv-binary /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1

# --locked fails the build when uv.lock no longer matches pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=src/uv.lock,target=uv.lock \
    --mount=type=bind,source=src/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# ----------------------------------------------------------------------------------------------------------------------
FROM node:${NODE_VERSION}-trixie-slim AS assets

WORKDIR /build

COPY src/package.json src/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY src/ ./
RUN npm run build

# ----------------------------------------------------------------------------------------------------------------------
FROM runtime-base AS app-development

ARG APP_UID=1000
ARG APP_GID=1000

# uv stays in this image: "make uv" runs it here throughout development.
COPY --from=uv-binary /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_CACHE_DIR=/tmp/uv-cache

# hadolint ignore=DL3066
USER app

# The dev group is synced too, which uv does by default: the checks and the tests run in
# this image.
RUN --mount=type=cache,target=/tmp/uv-cache,uid=${APP_UID},gid=${APP_GID} \
    --mount=type=bind,source=src/uv.lock,target=uv.lock \
    --mount=type=bind,source=src/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# ----------------------------------------------------------------------------------------------------------------------
FROM runtime-base AS app-production

ARG APP_UID=1000
ARG APP_GID=1000

COPY --from=python-dependencies /opt/venv /opt/venv
COPY --chown=${APP_UID}:${APP_GID} src/                             ./
COPY --chown=${APP_UID}:${APP_GID} --from=assets /build/build/static ./build/static

# hadolint ignore=DL3066
USER app

# collectstatic loads the settings, which refuse to start without the values src/.env
# provides at run time. These placeholders exist only for this one command; nothing reads
# them afterwards and they are not kept in the image.
RUN SECRET_KEY="placeholder-for-collectstatic-during-the-image-build-only-000000" \
    ADMIN_USERNAME="placeholder" \
    ADMIN_PASSWORD_HASH="argon2\$argon2id\$placeholder" \
    python manage.py collectstatic --no-input --verbosity 0
