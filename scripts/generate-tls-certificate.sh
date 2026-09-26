#!/usr/bin/env bash

# ----------------------------------------------------------------------------------------------------------------------
# Issue the local certificate authority and the certificate the application is served with
#
# The application is reached over a LAN IP address, so the address goes into a
# subjectAltName entry: every current browser ignores the common name outright. A local
# authority is issued alongside the certificate rather than signing it directly, because
# Firefox and Android only accept an imported authority that is marked as one, and
# because reissuing the certificate then costs nothing on the clients.
#
# The authority lives in var/authority, which no container mounts: its key can sign a
# certificate for any name that every client trusts, so it must stay out of reach of the
# web server that var/tls is mounted into. An authority found in var/tls is moved to
# var/authority, so the clients that already trust it keep working.
#
# OpenSSL runs inside a container: the host is not expected to provide it.
# ----------------------------------------------------------------------------------------------------------------------

set -eo pipefail

cd "$(dirname "${0}")"

OPENSSL_IMAGE="python:3.14.7-slim-trixie"
TLS_DIRECTORY="../var/tls"
AUTHORITY_DIRECTORY="../var/authority"
AUTHORITY_FILES=(authority.crt authority.key authority.srl)
AUTHORITY_DAYS="3650"
CERTIFICATE_DAYS="398"

GREEN_COLOR=$(tput setaf 2 2> /dev/null || true)
RED_COLOR=$(tput setaf 1 2> /dev/null || true)
DEFAULT_COLOR=$(tput sgr0 2> /dev/null || true)

fail() {
    echo "${RED_COLOR}${1}${DEFAULT_COLOR}" >&2
    exit 1
}

moveAuthorityOutOfTlsDirectory() {
    if [[ ! -f ${TLS_DIRECTORY}/authority.key || -f ${AUTHORITY_DIRECTORY}/authority.key ]]; then
        return
    fi

    for authorityFile in "${AUTHORITY_FILES[@]}"; do
        if [[ -f ${TLS_DIRECTORY}/${authorityFile} ]]; then
            mv "${TLS_DIRECTORY}/${authorityFile}" "${AUTHORITY_DIRECTORY}/${authorityFile}"
        fi
    done

    echo "Moved the local authority from var/tls to var/authority; the clients keep trusting it."
}

if [[ -f ../docker/.env ]]; then
    SERVER_ADDRESS=${SERVER_ADDRESS:-$(grep --extended-regexp '^SERVER_ADDRESS=' ../docker/.env | cut -d '=' -f 2- || true)}
fi

serverAddress="${1:-${SERVER_ADDRESS}}"

if [[ -z ${serverAddress} ]]; then
    if [[ $(uname -s) == "Darwin" ]]; then
        serverAddress=$(ipconfig getifaddr en0 2> /dev/null || ipconfig getifaddr en1 2> /dev/null || true)
    else
        serverAddress=$(ip -4 route get 1.1.1.1 2> /dev/null | awk '{print $7; exit}' || true)
    fi
fi

[[ ${serverAddress} =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
    || fail "Unable to determine the address to issue for. Pass it as the first argument, like ./generate-tls-certificate.sh 192.168.1.10"

mkdir -p "${TLS_DIRECTORY}" "${AUTHORITY_DIRECTORY}"
chmod 0700 "${AUTHORITY_DIRECTORY}"
moveAuthorityOutOfTlsDirectory

docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "$(cd "${TLS_DIRECTORY}" && pwd)":/tls \
    --volume "$(cd "${AUTHORITY_DIRECTORY}" && pwd)":/authority \
    --env SERVER_ADDRESS="${serverAddress}" \
    --env AUTHORITY_DAYS="${AUTHORITY_DAYS}" \
    --env CERTIFICATE_DAYS="${CERTIFICATE_DAYS}" \
    "${OPENSSL_IMAGE}" \
    bash -eo pipefail -c '
cat > /tmp/authority.cnf <<CONFIGURATION
[req]
default_bits       = 4096
default_md         = sha256
prompt             = no
distinguished_name = distinguished_name
x509_extensions    = authority

[distinguished_name]
O  = HopTalk Relay
CN = HopTalk Relay Local Authority

[authority]
basicConstraints     = critical, CA:TRUE, pathlen:0
keyUsage             = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
CONFIGURATION

cat > /tmp/server.cnf <<CONFIGURATION
[req]
default_bits       = 2048
default_md         = sha256
prompt             = no
distinguished_name = distinguished_name

[distinguished_name]
O  = HopTalk Relay
CN = ${SERVER_ADDRESS}

[server]
basicConstraints       = critical, CA:FALSE
keyUsage               = critical, digitalSignature, keyEncipherment
extendedKeyUsage       = serverAuth
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid, issuer
subjectAltName         = @alternative_names

[alternative_names]
IP.1  = ${SERVER_ADDRESS}
IP.2  = 127.0.0.1
DNS.1 = localhost
CONFIGURATION

if [ ! -f /authority/authority.crt ]; then
    openssl req -x509 -newkey rsa:4096 -sha256 -nodes \
        -days "${AUTHORITY_DAYS}" \
        -keyout /authority/authority.key -out /authority/authority.crt \
        -config /tmp/authority.cnf
fi

openssl req -new -newkey rsa:2048 -nodes \
    -keyout /tls/server.key -out /tls/server.csr \
    -config /tmp/server.cnf

openssl x509 -req -in /tls/server.csr \
    -CA /authority/authority.crt -CAkey /authority/authority.key \
    -CAserial /authority/authority.srl -CAcreateserial \
    -days "${CERTIFICATE_DAYS}" -sha256 \
    -extfile /tmp/server.cnf -extensions server \
    -out /tls/server.crt

cat /tls/server.crt /authority/authority.crt > /tls/fullchain.crt
rm -f /tls/server.csr

chmod 0644 /authority/authority.crt /tls/server.crt /tls/fullchain.crt
chmod 0600 /authority/authority.key /tls/server.key
'

echo ""
echo "${GREEN_COLOR}Issued a certificate for ${serverAddress}, valid for ${CERTIFICATE_DAYS} days.${DEFAULT_COLOR}"
echo "Install var/authority/authority.crt as a trusted authority on every client that will reach the application."
