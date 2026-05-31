FROM ghcr.io/astral-sh/uv:debian-slim
WORKDIR /waku

ARG WAKU_VERSION=unknown
ARG WAKU_COMMIT=unknown
ARG WAKU_BUILD_TIME=unknown
ENV WAKU_VERSION=$WAKU_VERSION
ENV WAKU_COMMIT=$WAKU_COMMIT
ENV WAKU_BUILD_TIME=$WAKU_BUILD_TIME
COPY pyproject.toml uv.lock ./
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ make build-essential git graphviz ca-certificates ffmpeg curl && \
    uv sync --frozen --no-dev && \
    uv pip install pip && \
    apt-get purge -y --auto-remove gcc g++ make build-essential git && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
COPY . .

# Expose health check port
EXPOSE 8180

ENTRYPOINT ["uv", "run", "python", "-m", "waku"]
