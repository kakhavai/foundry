# 24.04, not 22.04, and the Python version is the whole reason.
#
# Every pyproject.toml in this repo declares `requires-python = ">=3.12"` and
# every runtime image is `python:3.12-slim`. 22.04's distro python3 is 3.10,
# which made this image the single place in the system where 3.10 existed.
#
# That only bites `uv run --no-project`, which resolves against the ambient
# interpreter rather than the workspace's — so a step that pulls in a
# workspace package fails at RESOLUTION with "the current Python version
# (3.10.12) does not satisfy Python>=3.12", naming the package rather than
# the command that needs a `--python` flag. It cost a red REQUIRED check and
# a wrong first diagnosis. 24.04's python3 is 3.12.3, so the ambient
# interpreter now agrees with what the repo declares.
#
# Every tool below is a static binary curl'd from its own upstream release,
# so none of them depends on the base image's OS version; only `curl`, `git`,
# `python3`, `python3-pip` and `ca-certificates` come from apt, and all exist
# in 24.04. Nothing in this repo invokes pip (it uses uv), so 24.04's
# PEP 668 externally-managed restriction on pip is not reachable.
FROM ubuntu:24.04

ARG KIND_VERSION=0.24.0
ARG KUBECTL_VERSION=1.31.0
ARG HELM_VERSION=3.16.4
ARG HELMFILE_VERSION=0.168.0
ARG HELM_DIFF_VERSION=3.9.9
ARG DOCKER_CLI_VERSION=27.3.1
ARG BUILDX_VERSION=0.17.1

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl \
    git \
    python3 \
    python3-pip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# kind
RUN curl -Lo /usr/local/bin/kind \
    "https://kind.sigs.k8s.io/dl/v${KIND_VERSION}/kind-linux-amd64" \
    && chmod +x /usr/local/bin/kind

# kubectl
RUN curl -Lo /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# helm
RUN curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    | DESIRED_VERSION="v${HELM_VERSION}" bash

# helmfile
RUN curl -Lo /tmp/helmfile.tar.gz \
    "https://github.com/helmfile/helmfile/releases/download/v${HELMFILE_VERSION}/helmfile_${HELMFILE_VERSION}_linux_amd64.tar.gz" \
    && tar xzf /tmp/helmfile.tar.gz -C /usr/local/bin helmfile \
    && rm /tmp/helmfile.tar.gz \
    && chmod +x /usr/local/bin/helmfile

# helm-diff plugin (required by helmfile)
# Install directly from release tarball — bypasses helm plugin install's git clone
# which can pull a plugin.yaml with fields the current Helm version doesn't parse.
# HELM_PLUGINS is set to an absolute path so GitHub Actions' HOME override
# (/github/home) doesn't hide the plugin from Helm at runtime.
ENV HELM_PLUGINS=/usr/local/share/helm/plugins
RUN mkdir -p /usr/local/share/helm/plugins && \
    curl -fsSL "https://github.com/databus23/helm-diff/releases/download/v${HELM_DIFF_VERSION}/helm-diff-linux-amd64.tgz" \
      | tar xz -C /usr/local/share/helm/plugins/

# uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# docker CLI only (no daemon) — needed to inspect Kind cluster node IPs when running in a container job
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar xz -C /usr/local/bin --strip-components=1 docker/docker

# buildx plugin — required by Docker CLI 23+ when DOCKER_BUILDKIT=1 (or by default)
# and needed to build images using --mount=type=cache in Dockerfiles
RUN mkdir -p /usr/local/lib/docker/cli-plugins && \
    curl -fsSL "https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.linux-amd64" \
      -o /usr/local/lib/docker/cli-plugins/docker-buildx && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
