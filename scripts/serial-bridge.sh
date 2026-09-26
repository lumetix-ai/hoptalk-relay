#!/usr/bin/env bash

# ----------------------------------------------------------------------------------------------------------------------
# Expose the USB MeshCore node on a local TCP port, for the relay container on Docker Desktop for Mac
#
# Docker Desktop cannot pass a USB serial device into a container, so the relay worker
# reaches the node through meshcore's TCP transport instead, via host.docker.internal. The
# companion protocol frames are identical on both transports, so socat only copies bytes.
#
# socat accepts one client, and only then opens the device; when either side goes away it
# exits and the loop starts over. A node that reboots after a firmware update or a settings
# change comes back under a new /dev/cu.usbmodem* name, so the device is looked up again on
# every pass rather than once at start-up.
# ----------------------------------------------------------------------------------------------------------------------

set -uo pipefail

SOCAT_BINARY="${SOCAT_BINARY:-socat}"
SERIAL_DEVICE_GLOB="${SERIAL_DEVICE_GLOB:-/dev/cu.usbmodem*}"
SERIAL_BAUD_RATE="${SERIAL_BAUD_RATE:-115200}"
# Not 5000: the AirPlay Receiver listens on *:5000 on macOS, and a worker that reached it
# instead of the bridge would wait for a reply that never comes.
BRIDGE_PORT="${BRIDGE_PORT:-5055}"
# Loopback only: containers on Docker Desktop still reach it through host.docker.internal,
# and nothing else on the network can talk to the node.
BRIDGE_LISTEN_ADDRESS="${BRIDGE_LISTEN_ADDRESS:-127.0.0.1}"
RETRY_DELAY_SECONDS="${RETRY_DELAY_SECONDS:-2}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') serial-bridge: ${1}" >&2
}

findSerialDevice() {
    local matchingDevices=()

    # A glob that matches nothing stays literal without nullglob.
    shopt -s nullglob
    # shellcheck disable=SC2206
    matchingDevices=(${SERIAL_DEVICE_GLOB})
    shopt -u nullglob

    if [[ ${#matchingDevices[@]} -eq 1 ]]; then
        echo "${matchingDevices[0]}"
        return 0
    fi

    if [[ ${#matchingDevices[@]} -gt 1 ]]; then
        log "Several devices match ${SERIAL_DEVICE_GLOB}: ${matchingDevices[*]}. Set SERIAL_DEVICE_GLOB to exactly one."
    fi

    return 1
}

# While socat is between two clients nothing listens on the port, and whatever else holds
# it would then take the worker's connection instead.
describeListenersOnBridgePort() {
    lsof -nP -iTCP:"${BRIDGE_PORT}" -sTCP:LISTEN 2> /dev/null | awk 'NR > 1 {print $1 " (pid " $2 ") on " $9}'
}

if ! command -v "${SOCAT_BINARY}" > /dev/null; then
    log "socat is not installed. Run: brew install socat"
    exit 1
fi

listenersOnBridgePort=$(describeListenersOnBridgePort)

if [[ -n ${listenersOnBridgePort} ]]; then
    log "Port ${BRIDGE_PORT} is already in use by: ${listenersOnBridgePort//$'\n'/, }."
    log "Stop that process, or choose a free port with BRIDGE_PORT and set MESHCORE_TCP_PORT in docker/.env to match."
    exit 1
fi

trap 'log "Stopped."; exit 0' INT TERM

waitingForDeviceReported=false

while true; do
    if ! serialDevice=$(findSerialDevice); then
        if [[ ${waitingForDeviceReported} == false ]]; then
            log "Waiting for a device matching ${SERIAL_DEVICE_GLOB}."
            waitingForDeviceReported=true
        fi
        sleep "${RETRY_DELAY_SECONDS}"
        continue
    fi
    waitingForDeviceReported=false

    log "Serving ${serialDevice} on ${BRIDGE_LISTEN_ADDRESS}:${BRIDGE_PORT}."

    # macOS socat has no b115200-style options, only ispeed and ospeed. "rawer" also turns
    # echo off, so the node never sees its own frames reflected back at it.
    "${SOCAT_BINARY}" -d -d \
        "TCP-LISTEN:${BRIDGE_PORT},bind=${BRIDGE_LISTEN_ADDRESS},reuseaddr,nodelay" \
        "OPEN:${serialDevice},rawer,ispeed=${SERIAL_BAUD_RATE},ospeed=${SERIAL_BAUD_RATE}"

    log "Connection closed (socat exit status $?)."
    sleep "${RETRY_DELAY_SECONDS}"
done
