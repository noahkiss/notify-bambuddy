# ---------------------------------------------------------------------------
# notify-bambuddy container image.
#
# Two stages, so the wheel-building toolchain never ships in the final image.
# The result is python:3.12-slim plus one dependency, running as a non-root
# user, with no compiler and no build cache in it.
#
# There is no EXPOSE for a service port because there is no service port:
# every connection this makes is OUTBOUND, to BambuBuddy and to Notify (or to
# BambuBuddy never has to reach back into it. The
# only listener is the health endpoint. Inside the image it binds to all
# interfaces, because that is what lets the container runtime probe it; the
# container network is the boundary, and nothing is published to the host
# unless you map the port yourself.
# ---------------------------------------------------------------------------

# --- Build stage -----------------------------------------------------------
FROM python:3.12-slim AS build

WORKDIR /build

# Dependencies first, as their own layer: the source changes on every commit,
# the dependency list almost never does, so this keeps rebuilds fast.
# LICENSE is not optional here. This is AGPL-3.0 software and the licence text
# has to travel with the binary; without it the published image is missing the
# one file the licence requires it to carry. `license-files` in pyproject puts
# it into the wheel, so it reaches the runtime stage automatically.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Build the wheel into /wheels. There are no dependencies to collect alongside
# it, which is the whole reason the runtime stage is as small as it is.
RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /wheels

# --- Runtime stage ---------------------------------------------------------
FROM python:3.12-slim

# A fixed uid/gid so a bind-mounted state directory has predictable ownership
# on the host. 10001 is comfortably outside the range any distro assigns.
RUN groupadd --gid 10001 notify \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin notify

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels notify-bambuddy \
    && rm -rf /wheels

# The SQLite state lives here. Declared a volume so that forgetting to mount
# one does not silently produce a service that loses every tile on restart.
RUN mkdir -p /data && chown notify:notify /data
VOLUME ["/data"]
ENV STATE_DB=/data/notify-bambuddy.db

# The health endpoint binds to localhost by default, which is invisible from
# outside the container. Binding to all interfaces here is what makes
# `docker compose ps` health checks and Kubernetes probes work; the container
# network is the boundary, and the endpoint exposes no credentials.
ENV HEALTH_HOST=0.0.0.0
ENV HEALTH_PORT=9337

# Unbuffered, so logs appear in `docker logs` as they happen rather than in
# 4 KB bursts.
# The commit this image was built from, logged at startup. Without it, a
# rolling `latest` makes "which build am I running" unanswerable: two images
# months apart both report the same version number.
ARG BUILD_SHA=unknown
ENV NOTIFY_BAMBUDDY_BUILD=$BUILD_SHA

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

USER notify

# urllib rather than curl: the slim image has no curl, and adding one for a
# health check would mean shipping a network tool in a container that has no
# business making arbitrary requests.
# Reads HEALTH_PORT and HEALTH_ENABLED from the container environment, so an
# override in compose does not silently turn the check into a permanent
# failure, and disabling the endpoint passes rather than fails.
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,sys,urllib.request; e=os.environ.get('HEALTH_ENABLED','true').lower(); sys.exit(0) if e in ('0','false','no','off') else sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('HEALTH_PORT','9337')+'/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["notify-bambuddy"]
