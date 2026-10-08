# syntax=docker/dockerfile:1.7
# One image per Python service, built from the uv workspace:
#   docker buildx build -f docker/python.Dockerfile --platform linux/arm64 \
#     --build-arg PACKAGE=copilot-gateway --build-arg COMMAND=copilot-gateway -t gateway .
# PACKAGE is the workspace member to install (copilot-gateway, copilot-api, copilot-worker);
# COMMAND is the default command (ECS task definitions name it explicitly anyway). The worker
# image also runs copilot-scheduler, copilot-migrate and copilot-db-logins, so the migrations
# are in every image under /app/migrations.

ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=python3.12
WORKDIR /app
ARG PACKAGE
COPY . .
# Locked versions only, no dev tools, the workspace packages installed as wheels (not editable),
# so the final stage needs only the virtual environment.
RUN --mount=type=cache,target=/root/.cache/uv \
    test -n "$PACKAGE" && \
    uv sync --frozen --no-dev --no-editable --package "$PACKAGE"

FROM ${PYTHON_IMAGE}
ARG COMMAND
# A fixed non-root uid: on the Stage 0 instance the host's firewall keeps this uid away from
# the instance metadata service (infra/modules/host), so tasks use only their task roles.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY migrations /app/migrations
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COPILOT_MIGRATIONS_DIR=/app/migrations \
    COPILOT_COMMAND=${COMMAND}
USER 10001
CMD ["/bin/sh", "-c", "exec $COPILOT_COMMAND"]
