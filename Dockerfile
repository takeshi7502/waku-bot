FROM ghcr.io/astral-sh/uv:debian-slim

WORKDIR /waku

ARG WAKU_VERSION=unknown
ARG WAKU_COMMIT=unknown
ARG WAKU_BUILD_TIME=unknown

ENV WAKU_VERSION=$WAKU_VERSION
ENV WAKU_COMMIT=$WAKU_COMMIT
ENV WAKU_BUILD_TIME=$WAKU_BUILD_TIME
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        gcc \
        g++ \
        git \
        graphviz \
        make \
        build-essential && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev && \
    uv pip install pip && \
    apt-get purge -y --auto-remove gcc g++ make build-essential git

COPY . .

# Expose health check port
EXPOSE 8180

ENTRYPOINT ["uv", "run", "python", "-m", "waku"]
