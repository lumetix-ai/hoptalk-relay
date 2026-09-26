#!/usr/bin/env bash

# ----------------------------------------------------------------------------------------------------------------------
# Run docker compose with the project name and the Compose files this checkout needs
#
# HOPTALK_COMPOSE_PROJECT_NAME (default "hoptalk-relay") names the project, so isolated development stacks can run side
# by side, each with its own containers, image and database volume.
# ----------------------------------------------------------------------------------------------------------------------

set -eo pipefail

cd "$(dirname "${0}")"

export HOPTALK_COMPOSE_PROJECT_NAME="${HOPTALK_COMPOSE_PROJECT_NAME:-hoptalk-relay}"

composeFiles=(--file "../docker/docker-compose.yml")

if [[ -f ../docker/.env ]]; then
    buildTarget=$(grep --extended-regexp '^APP_BUILD_TARGET=' ../docker/.env | cut -d '=' -f 2- || true)
    meshcoreTransport=$(grep --extended-regexp '^MESHCORE_TRANSPORT=' ../docker/.env | cut -d '=' -f 2- || true)
    SERVER_ADDRESS=${SERVER_ADDRESS:-$(grep --extended-regexp '^SERVER_ADDRESS=' ../docker/.env | cut -d '=' -f 2- || true)}
fi

buildTarget=${buildTarget:-app-development}

if [[ ${buildTarget} == "app-development" ]]; then
    composeFiles+=(--file "../docker/docker-compose.development.yml")
fi

# Only the development target gives each project a database volume of its own. Every other
# target keeps the database in this checkout's var/database, and a second PostgreSQL server
# on that data directory would corrupt it.
if [[ ${buildTarget} != "app-development" && ${HOPTALK_COMPOSE_PROJECT_NAME} != "hoptalk-relay" ]]; then
    echo "An isolated stack (HOPTALK_COMPOSE_PROJECT_NAME=${HOPTALK_COMPOSE_PROJECT_NAME}) needs APP_BUILD_TARGET=app-development in docker/.env: the ${buildTarget} target keeps its database in var/database, which only the hoptalk-relay project may use." >&2
    exit 1
fi

if [[ ${MESHCORE_TRANSPORT:-${meshcoreTransport:-tcp}} == "serial" ]]; then
    composeFiles+=(--file "../docker/docker-compose.serial-device.yml")
fi

# The panel answers only for the address its certificate is issued for, so an empty
# SERVER_ADDRESS is detected exactly as generate-tls-certificate.sh detects it.
if [[ -z ${SERVER_ADDRESS} ]]; then
    if [[ $(uname -s) == "Darwin" ]]; then
        SERVER_ADDRESS=$(ipconfig getifaddr en0 2> /dev/null || ipconfig getifaddr en1 2> /dev/null || true)
    else
        SERVER_ADDRESS=$(ip -4 route get 1.1.1.1 2> /dev/null | awk '{print $7; exit}' || true)
    fi
fi

export SERVER_ADDRESS

# The daemon creates a missing bind-mount source as root, and the container runs as
# the invoking account, which then cannot write into it. Creating the mount sources
# here keeps whichever compose invocation comes first on a fresh checkout safe.
mkdir -p ../var/database ../var/tls

docker compose \
    --project-name "${HOPTALK_COMPOSE_PROJECT_NAME}" \
    "${composeFiles[@]}" \
    "${@}"
