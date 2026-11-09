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

COPY watch_and_convert.py ktx.py scene.py ./

# Mount /mnt/input and /mnt/output separately. They're deliberately not declared as
# VOLUMEs: a forgotten mount should fail at startup, not write to an anonymous volume.
ENV PYTHONUNBUFFERED=1 \
    INPUT_DIR=/mnt/input \
    OUTPUT_DIR=/mnt/output
# Optionally pass at runtime, to adjust the Foundry scene built from each map's .json:
#   FOUNDRY_ASSET_DIR  path Foundry uses for /mnt/output (e.g. assets/maps); level backgrounds are pointed under it
#   GRID_COLOR         "grid.color", e.g. #000000
#   GRID_ALPHA         "grid.alpha", e.g. 0.2
#   LEVEL_HEIGHT       height of each floor's level, 10 by default

CMD ["python", "watch_and_convert.py"]
