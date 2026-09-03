# Aperture container image.
#
# Two things this image is deliberate about:
#
# 1. It runs as a non-root user with a read-only-friendly layout. A control that
#    governs what agents may do should not itself be the most privileged process
#    on the host.
# 2. The workspace is a volume, not a layer. Policy, catalog, and principals are
#    operational configuration that changes without a rebuild, and baking them in
#    would make a policy fix require a deploy.
#
# Build:  docker build -t aperture:latest .
# Serve the playground:
#   docker run --rm -p 8000:8000 -v "$PWD/workspace:/workspace" aperture:latest
# Serve MCP over stdio (what an agent runtime actually attaches to):
#   docker run --rm -i -v "$PWD/workspace:/workspace" aperture:latest \
#     aperture serve -w /workspace -p svc_support_agent --purpose customer_support

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Install into a virtualenv we can copy wholesale into the runtime stage, so the
# final image carries no build toolchain.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[web,idp]"


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APERTURE_WORKSPACE=/workspace

# curl is here only for the container healthcheck; nothing in the app shells out.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 aperture \
 && mkdir -p /workspace \
 && chown -R aperture:aperture /workspace

COPY --from=build /opt/venv /opt/venv

USER aperture
WORKDIR /home/aperture

EXPOSE 8000

# Liveness answers "is the process up". The orchestrator should point its readiness
# probe at /readyz instead, which additionally checks that a governed request can
# be served and that the audit chain still verifies.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "aperture.playground:app", "--host", "0.0.0.0", "--port", "8000"]
