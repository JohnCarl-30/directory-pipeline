# One image for every app service: the API, the workers and both mock upstreams
# run the same code with a different command (see docker-compose.yml).
#
# Two stages so the final image holds only the installed packages. uv (~50MB),
# pip and the build-time lockfile export stay in the builder and are discarded.

FROM python:3.12-slim AS builder

# Pinned: this resolves the lockfile, so a floating tag would put a build tool
# with its own release cadence in the middle of a reproducibility guarantee.
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# The image installs *the lockfile*, not the version ranges in pyproject.toml.
#
# `pip install .` used to do the latter, which meant the deployable's
# dependencies were whatever resolved on build day while CI tested what
# uv.lock pinned. selectolax 1.0 shipped on 2026-10-03 and removed the Modest
# backend that `extraction/cascade.py` imports, so the integration job went red
# with no commit between it and the last green run -- the container could not
# import its own code, and the lockfile that would have prevented it was
# sitting in the build context unread.
#
# --no-deps on the project install is the half that matters: without it, the
# installer re-resolves the dependency ranges and undoes the line above.
#
# The venv lives at the same path in both stages, so the console scripts'
# shebangs (dp-worker, dp-api) still resolve after the copy.
RUN uv venv /opt/venv \
 && uv export --frozen --no-dev --no-emit-project --format requirements-txt > /tmp/req.txt \
 && uv pip install --python /opt/venv/bin/python --no-cache -r /tmp/req.txt \
 && uv pip install --python /opt/venv/bin/python --no-cache --no-deps .

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

COPY --from=builder /opt/venv /opt/venv

# Never run the worker as root: it executes network-facing scraping code.
RUN useradd --create-home --uid 10001 appuser
USER appuser
WORKDIR /app

# Imports the package rather than curling a port: this image is the worker by
# default, and the worker serves no HTTP but the metrics listener.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import directory_pipeline" || exit 1

CMD ["dp-worker"]
