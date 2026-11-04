FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM jenkins/jenkins:2.588-jdk21@sha256:4c03015d601fe7a761220979d2dd7bf465091e2d13de775eb1980627811b3aaa

LABEL org.opencontainers.image.source="ewallet-data-ai-platform" \
      org.opencontainers.image.title="EDAI-2 bounded Jenkins controller"

ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_CACHE_DIR=/var/jenkins_home/.cache/uv

USER root
COPY --from=uv /uv /uvx /usr/local/bin/
RUN apt-get update \
 && apt-get install --yes --no-install-recommends ca-certificates git python3 \
 && rm -rf /var/lib/apt/lists/* \
 && uv python install 3.10.19 3.12.12 \
 && uv pip install \
      --python /opt/uv-python/cpython-3.10.19-linux-x86_64-gnu/bin/python \
      --break-system-packages \
      setuptools==84.2.1

COPY infra/docker/jenkins-controller/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt \
 && rm -f /opt/jenkins-plugin-manager.jar /usr/local/bin/git-lfs

USER jenkins
