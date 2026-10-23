# Minimal image in which agent-written code and its tests run. No network is attached at runtime
# (docker run --network none), the root filesystem is read-only and the user is unprivileged.
FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir pytest==9.1.1 && useradd --uid 65534 --no-create-home --shell /usr/sbin/nologin sandbox || true
WORKDIR /workspace
USER 65534:65534
