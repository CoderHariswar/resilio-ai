# vhs + ResilioAI, used only to record docs/assets/demo.gif (see `make demo`).
FROM ghcr.io/charmbracelet/vhs:v0.10.0
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md /src/
COPY resilio /src/resilio
RUN python3 -m venv /opt/resilio && /opt/resilio/bin/pip install -q /src
ENV PATH=/opt/resilio/bin:$PATH
