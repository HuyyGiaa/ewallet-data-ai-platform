#!/usr/bin/env bash
# Build kubectl-ate from the pinned Substrate source (the version in kagent-pilot/versions.env),
# offline from its vendored modules, in a digest-pinned Go image. Output: <out-dir>/kubectl-ate.
# usage: build_kubectl_ate.sh <out-dir>
set -euo pipefail
out=${1:?output directory required}
root=$(cd "$(dirname "$0")/../.." && pwd)
# shellcheck source=../../infra/kubernetes/kagent-pilot/versions.env
. "$root/infra/kubernetes/kagent-pilot/versions.env"
go_image=golang:1.26@sha256:978e0a4e3d953662577d784471db27742f543ab09eead06839a838188c73314b
mkdir -p "$out"; out=$(cd "$out" && pwd)
src=$(mktemp -d); trap 'rm -rf "$src"' EXIT
curl -fsSL "https://codeload.github.com/kagent-dev/substrate/tar.gz/refs/tags/v${SUBSTRATE_VERSION}" -o "$src/src.tgz"
tar -xzf "$src/src.tgz" -C "$src"
docker run --rm -v "$src/substrate-${SUBSTRATE_VERSION}:/src:ro" -v "$out:/out" -w /src --user "$(id -u):$(id -g)" \
  -e GOFLAGS=-mod=vendor -e CGO_ENABLED=0 -e GOCACHE=/tmp/gocache -e HOME=/tmp "$go_image" go build -o /out/kubectl-ate ./cmd/kubectl-ate
"$out/kubectl-ate" --help > /dev/null
echo "$out/kubectl-ate (substrate v${SUBSTRATE_VERSION}) sha256 $(sha256sum "$out/kubectl-ate" | cut -d' ' -f1)"
