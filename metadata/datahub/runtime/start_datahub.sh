#!/usr/bin/env bash

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CACHE_DIR="${DATAHUB_RUNTIME_CACHE_DIR:-${SCRIPT_DIR}/.cache}"
readonly QUICKSTART_SERIES="v1.7.0"
readonly QUICKSTART_GIT_REF="v1.7.0.1"
readonly DATAHUB_SERVER_VERSION="v1.7.0.1"
readonly DATAHUB_CLI_VERSION="1.7.0.5"
readonly QUICKSTART_URL="https://raw.githubusercontent.com/datahub-project/datahub/${QUICKSTART_GIT_REF}/docker/quickstart/docker-compose.quickstart-profile.yml"
readonly SOURCE_SHA256="316e39d4c09690752de4a549da20df37f6ca6eaa7ce5613008a2b6ff49211b1b"
readonly RUNTIME_SHA256="46cee4630ea51581a032b8c95c1401a918517ebc164cbf15ae57509d7f238358"
readonly SOURCE_COMPOSE="${CACHE_DIR}/docker-compose.${QUICKSTART_GIT_REF}.yml"
readonly RUNTIME_COMPOSE="${CACHE_DIR}/docker-compose.${QUICKSTART_GIT_REF}.kafka-9093.yml"
readonly SECRETS_FILE="${CACHE_DIR}/.local-secrets.env"
readonly LEGACY_SECRETS_FILE="${HOME}/.datahub/quickstart/.local-secrets.env"
readonly DATAHUB_PYTHON="${DATAHUB_PYTHON:-python3}"
readonly DATAHUB_PULL_IMAGES="${DATAHUB_PULL_IMAGES:-true}"

usage() {
    cat <<'USAGE'
Usage: metadata/datahub/runtime/start_datahub.sh [start|stop|check|prepare|config]

Commands:
  start    Prepare and start the pinned DataHub Quickstart (default).
  stop     Stop Quickstart containers without deleting volumes.
  check    Run the DataHub CLI health check.
  prepare  Download, verify, and patch the compose file without starting it.
  config   Validate the generated Compose configuration.

Environment:
  DATAHUB_PYTHON             Python executable containing acryl-datahub.
  DATAHUB_PULL_IMAGES        Use false to reuse already-present pinned images.
  DATAHUB_RUNTIME_CACHE_DIR  Override the generated compose cache directory.
USAGE
}

sha256_of() {
    sha256sum "$1" | awk '{print $1}'
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR | required command not found: $1" >&2
        exit 1
    fi
}

verify_checksum() {
    local file_path="$1"
    local expected="$2"
    local actual
    actual="$(sha256_of "${file_path}")"

    if [[ "${actual}" != "${expected}" ]]; then
        echo "ERROR | checksum mismatch | file=${file_path} actual=${actual} expected=${expected}" >&2
        exit 1
    fi
}

prepare_compose() {
    require_command curl
    require_command sha256sum
    mkdir -p "${CACHE_DIR}"

    if [[ ! -f "${SOURCE_COMPOSE}" ]]; then
        echo "Downloading pinned DataHub Quickstart ${QUICKSTART_GIT_REF}..."
        curl --fail --location --retry 3 --output "${SOURCE_COMPOSE}" "${QUICKSTART_URL}"
    fi

    verify_checksum "${SOURCE_COMPOSE}" "${SOURCE_SHA256}"

    "${DATAHUB_PYTHON}" - "${SOURCE_COMPOSE}" "${RUNTIME_COMPOSE}" <<'PY'
from pathlib import Path
import sys

source_path = Path(sys.argv[1])
runtime_path = Path(sys.argv[2])
text = source_path.read_text(encoding="utf-8")

replacements = {
    "BROKER://broker:29092,EXTERNAL://localhost:9092":
        "BROKER://broker:29092,EXTERNAL://localhost:9093",
    "published: '9092'": "published: '9093'",
}

for old, new in replacements.items():
    count = text.count(old)
    if count != 1:
        raise SystemExit(
            f"ERROR | expected exactly one Quickstart value {old!r}; found {count}"
        )
    text = text.replace(old, new)

runtime_path.write_text(text, encoding="utf-8")
PY

    verify_checksum "${RUNTIME_COMPOSE}" "${RUNTIME_SHA256}"
    echo "Prepared DataHub ${QUICKSTART_SERIES} / ${DATAHUB_SERVER_VERSION} compose: ${RUNTIME_COMPOSE}"
}

prepare_secrets() {
    mkdir -p "${CACHE_DIR}"

    if [[ -f "${SECRETS_FILE}" ]]; then
        return
    fi

    if [[ -f "${LEGACY_SECRETS_FILE}" ]]; then
        cp "${LEGACY_SECRETS_FILE}" "${SECRETS_FILE}"
        chmod 600 "${SECRETS_FILE}"
        return
    fi

    umask 077
    "${DATAHUB_PYTHON}" - "${SECRETS_FILE}" <<'PY'
import base64
from pathlib import Path
import secrets
import sys

path = Path(sys.argv[1])
signing_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
salt = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
path.write_text(
    "# Auto-generated local development secrets; do not commit.\n"
    f"DATAHUB_TOKEN_SERVICE_SIGNING_KEY={signing_key}\n"
    f"DATAHUB_TOKEN_SERVICE_SALT={salt}\n",
    encoding="utf-8",
)
PY
}

load_compose_environment() {
    prepare_secrets
    set -a
    # shellcheck disable=SC1090
    source "${SECRETS_FILE}"
    set +a
    export DATAHUB_VERSION="${DATAHUB_SERVER_VERSION}"
    export UI_INGESTION_DEFAULT_CLI_VERSION="${DATAHUB_CLI_VERSION}"
    export DATAHUB_TELEMETRY_ENABLED="false"
}

compose() {
    docker compose \
        --profile quickstart \
        --file "${RUNTIME_COMPOSE}" \
        --project-name datahub \
        "$@"
}

start_quickstart() {
    prepare_compose
    load_compose_environment

    if [[ "${DATAHUB_PULL_IMAGES}" != "false" ]]; then
        compose pull
    fi

    compose up -d --remove-orphans
    echo "DataHub start submitted. Run '$0 check' after services become healthy."
}

stop_quickstart() {
    prepare_compose
    load_compose_environment
    compose stop
    echo "DataHub containers stopped; named volumes were preserved."
}

validate_config() {
    prepare_compose
    load_compose_environment
    compose config --quiet
    echo "PASS | DataHub Compose config"
}

main() {
    local action="${1:-start}"

    case "${action}" in
        start)
            start_quickstart
            ;;
        stop)
            stop_quickstart
            ;;
        check)
            DATAHUB_TELEMETRY_ENABLED=false "${DATAHUB_PYTHON}" -m datahub docker check
            ;;
        prepare)
            prepare_compose
            ;;
        config)
            validate_config
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
