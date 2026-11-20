FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM jenkins/jenkins:2.588-jdk21@sha256:4c03015d601fe7a761220979d2dd7bf465091e2d13de775eb1980627811b3aaa

LABEL org.opencontainers.image.source="ewallet-data-ai-platform" \
      org.opencontainers.image.title="EDAI-2 bounded Jenkins controller"

# The root filesystem is read-only at runtime (readOnlyRootFilesystem), so every interpreter a catalog
# project's requires-python needs is installed here at build time and uv never downloads one at runtime
# (it would write into /opt/uv-python): 3.10 (most projects), 3.11 (rag-ingestion), 3.12 (agents, vLLM).
# CI installs with uv only; the 3.11 interpreter's bundled pip (vendored msgpack/urllib3/setuptools, listed in
# its pip/_vendor/bom.cdx.json) is removed so it adds no unused packages to the image scan.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_CACHE_DIR=/var/jenkins_home/.cache/uv \
    UV_PYTHON_DOWNLOADS=manual

USER root
COPY --from=uv /uv /uvx /usr/local/bin/
# kubectl and envsubst (gettext-base) are what the frozen streaming operator
# (scripts/streaming/block_a_cloud.sh) and feast_online_job.sh call. kubectl comes from the
# signed Kubernetes apt repository at an exact version (within one minor of GKE 1.36/1.37).
ARG KUBECTL_MINOR=v1.37
ARG KUBECTL_VERSION=1.37.1-1.1
# helm runs the Agent staging release (scripts/ci/agent_staging.py: helm template/install/uninstall with
# HELM_DRIVER=configmap) on this controller. Exact release tarball, verified against its published SHA-256
# before it is unpacked. The image is amd64-only (the uv Python path below is x86_64).
ARG HELM_VERSION=3.22.0
ARG HELM_SHA256_AMD64=1e4ab49e429626cf6c6958d914248b78c9730803c2751b87627e171dc800e7bb
RUN apt-get update \
 && apt-get install --yes --no-install-recommends ca-certificates curl gnupg git python3 gettext-base \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL "https://pkgs.k8s.io/core:/stable:/${KUBECTL_MINOR}/deb/Release.key" \
    | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/${KUBECTL_MINOR}/deb/ /" \
    > /etc/apt/sources.list.d/kubernetes.list \
 && apt-get update \
 && apt-get install --yes --no-install-recommends "kubectl=${KUBECTL_VERSION}" \
 && test "$(dpkg --print-architecture)" = amd64 \
 && curl -fsSLo /tmp/helm.tar.gz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" \
 && echo "${HELM_SHA256_AMD64}  /tmp/helm.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/helm.tar.gz -C /tmp linux-amd64/helm \
 && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm \
 && rm -rf /tmp/helm.tar.gz /tmp/linux-amd64 \
 && helm version --short | grep -qx "v${HELM_VERSION}+g[0-9a-f]*" \
 && apt-get purge --yes --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/* \
 && uv python install 3.10.19 3.11.17 3.12.12 \
 && uv pip uninstall \
      --python /opt/uv-python/cpython-3.11.17-linux-x86_64-gnu/bin/python \
      --break-system-packages \
      pip \
 && uv pip install \
      --python /opt/uv-python/cpython-3.10.19-linux-x86_64-gnu/bin/python \
      --break-system-packages \
      setuptools==84.2.1

COPY infra/docker/jenkins-controller/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt \
 && rm -f /opt/jenkins-plugin-manager.jar /usr/local/bin/git-lfs

USER jenkins
