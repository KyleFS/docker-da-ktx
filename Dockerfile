FROM python:3.12-slim

ARG KTX_VERSION=4.4.2

# Install KTX-Software (provides the `toktx` CLI used to produce .ktx2 files).
# The upstream .deb declares no apt Depends, so pull in the runtime libs it
# needs explicitly and sanity-check the binary at build time.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl libstdc++6 libgomp1; \
    curl -fsSL -o /tmp/ktx-software.deb \
        "https://github.com/KhronosGroup/KTX-Software/releases/download/v${KTX_VERSION}/KTX-Software-${KTX_VERSION}-Linux-x86_64.deb"; \
    dpkg -i /tmp/ktx-software.deb; \
    rm -f /tmp/ktx-software.deb; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    toktx --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watch_and_convert.py .

ENV PYTHONUNBUFFERED=1 \
    WATCH_DIR=/data

VOLUME ["/data"]

CMD ["python", "watch_and_convert.py"]
