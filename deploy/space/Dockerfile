# Base image for the DSH space and runtime.
#
# The space code itself is mounted at /opt/dsh-space (see compose example),
# so this image only provides Node, Python and the pinned DeepSeek Harness CLI.
#
# Build:
#   docker build -t dsh-space:local .
#
# Pin the DSH version: it is a developer preview and breaks compatibility often.
FROM node:24-bookworm-slim

ARG DSH_VERSION=0.2.0-rc.2
ARG NPM_REGISTRY=https://registry.npmjs.org

ENV DSH_VERSION=${DSH_VERSION} \
    DSH_HOME=/home/node/.dsh \
    DSH_PORT=19992 \
    TZ=Asia/Shanghai

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git curl ca-certificates procps make g++ \
 && rm -rf /var/lib/apt/lists/*

# Some DSH plugins spawn a bare `python`; Debian only ships `python3`.
RUN ln -sf /usr/bin/python3 /usr/local/bin/python

# npm >= 11 blocks install scripts by default; without --allow-scripts the native
# helpers (koffi, node-pty, dsh-subprocess-local) are never built and dsh dies
# at runtime with "Cannot find package ...-native".
RUN npm install -g --registry="${NPM_REGISTRY}" \
      --allow-scripts=@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs \
      "@deepseek-ai/dsh@${DSH_VERSION}" \
 && npm cache clean --force

RUN corepack enable pnpm \
 && (corepack prepare pnpm@latest --activate || true) \
 && (pnpm --version || true)

RUN mkdir -p /opt/dsh-space /workspace /home/node/.dsh \
 && chown -R node:node /opt/dsh-space /workspace /home/node/.dsh

USER node
WORKDIR /workspace
EXPOSE 19991 19992 19993
