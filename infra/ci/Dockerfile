FROM ubuntu:22.04

# Pinned tool versions — keep in sync with .github/workflows/integration-test.yml
ARG KIND_VERSION=0.24.0
ARG KUBECTL_VERSION=1.31.0
ARG HELM_VERSION=3.16.0
ARG HELMFILE_VERSION=0.168.0

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
RUN helm plugin install https://github.com/databus23/helm-diff

# uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"
