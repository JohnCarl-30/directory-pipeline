FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Pinned: this resolves the lockfile, so a floating tag would put a build tool
# with its own release cadence in the middle of a reproducibility guarantee.
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

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
# --no-deps on the project install is the half that matters: without it, pip
# re-resolves the dependency ranges and undoes the line above.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt \
 && pip install --no-cache-dir --no-deps . \
 && rm /tmp/req.txt

# Never run the worker as root: it executes network-facing scraping code.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Imports the package rather than curling a port: this image is the worker by
# default, and the worker serves no HTTP but the metrics listener.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import directory_pipeline" || exit 1

CMD ["dp-worker"]
