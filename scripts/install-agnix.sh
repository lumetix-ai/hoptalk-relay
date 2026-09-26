#!/usr/bin/env bash

# ----------------------------------------------------------------------------------------------------------------------
# Install the agnix binary that validates the .claude configuration
#
# The release and the checksum of every supported platform are pinned below, so a download that does not match byte for
# byte is never installed. To try another release, export AGNIX_VERSION together with AGNIX_CHECKSUM, or fill both
# variables in docker/.env and run the script again.
# ----------------------------------------------------------------------------------------------------------------------

set -eo pipefail

cd "$(dirname "${0}")"

AGNIX_PINNED_VERSION="0.48.0"

# Checksums of the release archives of AGNIX_PINNED_VERSION, replace every one of them when the version changes
AGNIX_CHECKSUM_AARCH64_APPLE_DARWIN="d7cc221ffcd040373abb7c3ae82c74c8ef5922a5c9f7aec1236a7f1aeb1f164c"
AGNIX_CHECKSUM_X86_64_UNKNOWN_LINUX_GNU="da8a0fd2389f2fa442721ca1ecf447bc0de64bf629014f11336eaccbfe8aa2e8"
AGNIX_CHECKSUM_AARCH64_UNKNOWN_LINUX_GNU="c8ddfa48d6bc3d35ef0625a7292c9be1d7a0a7f2fbced769fac284a5fdf005f5"

AGNIX_REPOSITORY="agent-sh/agnix"
AGNIX_BINARY_PATH="../src/bin/agnix"

GREEN_COLOR=$(tput setaf 2 2> /dev/null || true)
RED_COLOR=$(tput setaf 1 2> /dev/null || true)
DEFAULT_COLOR=$(tput sgr0 2> /dev/null || true)

fail() {
    echo "${RED_COLOR}${1}${DEFAULT_COLOR}" >&2
    exit 1
}

command -v curl > /dev/null 2>&1 || fail "curl is required to download agnix."

operating_system=$(uname -s)
architecture=$(uname -m)

case "${operating_system} ${architecture}" in
    "Darwin arm64")
        release_target="aarch64-apple-darwin"
        pinned_checksum="${AGNIX_CHECKSUM_AARCH64_APPLE_DARWIN}"
        ;;
    "Linux x86_64")
        release_target="x86_64-unknown-linux-gnu"
        pinned_checksum="${AGNIX_CHECKSUM_X86_64_UNKNOWN_LINUX_GNU}"
        ;;
    "Linux aarch64" | "Linux arm64")
        release_target="aarch64-unknown-linux-gnu"
        pinned_checksum="${AGNIX_CHECKSUM_AARCH64_UNKNOWN_LINUX_GNU}"
        ;;
    "Darwin x86_64")
        fail "agnix publishes no macOS build for x86_64. On Apple Silicon, run this script outside of Rosetta."
        ;;
    *)
        fail "Unsupported platform: ${operating_system} ${architecture}. agnix supports macOS on arm64 and Linux on x86_64 or aarch64."
        ;;
esac

case "${1}" in
    "")
        ;;
    "--print-env")
        echo "AGNIX_VERSION=${AGNIX_PINNED_VERSION}"
        echo "AGNIX_CHECKSUM=${pinned_checksum}"
        exit 0
        ;;
    *)
        fail "Unknown argument: ${1}. The only supported argument is --print-env."
        ;;
esac

AGNIX_VERSION="${AGNIX_VERSION:-${AGNIX_PINNED_VERSION}}"

if [[ -z ${AGNIX_CHECKSUM} && ${AGNIX_VERSION} != "${AGNIX_PINNED_VERSION}" ]]; then
    fail "AGNIX_VERSION is set to ${AGNIX_VERSION} while AGNIX_CHECKSUM is empty. Take the checksum of agnix-${release_target}.tar.gz from https://github.com/${AGNIX_REPOSITORY}/releases/tag/v${AGNIX_VERSION} and export it as AGNIX_CHECKSUM."
fi

AGNIX_CHECKSUM="${AGNIX_CHECKSUM:-${pinned_checksum}}"

archive_name="agnix-${release_target}.tar.gz"
download_url="https://github.com/${AGNIX_REPOSITORY}/releases/download/v${AGNIX_VERSION}/${archive_name}"

temporary_directory=$(mktemp -d)
trap 'rm -rf "${temporary_directory}"' EXIT

echo "Downloading agnix ${AGNIX_VERSION} for ${release_target}..."

curl --fail --location --silent --show-error --output "${temporary_directory}/${archive_name}" "${download_url}" \
    || fail "Unable to download ${download_url}. Check that the release exists."

if command -v sha256sum > /dev/null 2>&1; then
    actual_checksum=$(sha256sum "${temporary_directory}/${archive_name}" | cut -d ' ' -f 1)
else
    actual_checksum=$(shasum --algorithm 256 "${temporary_directory}/${archive_name}" | cut -d ' ' -f 1)
fi

if [[ ${actual_checksum} != "${AGNIX_CHECKSUM}" ]]; then
    fail "Checksum mismatch for ${archive_name}: expected ${AGNIX_CHECKSUM}, got ${actual_checksum}. The download was not installed."
fi

tar -xzf "${temporary_directory}/${archive_name}" -C "${temporary_directory}"

mkdir -p "$(dirname "${AGNIX_BINARY_PATH}")"
install -m 0755 "${temporary_directory}/agnix" "${AGNIX_BINARY_PATH}"

# Gatekeeper refuses to execute a binary marked with com.apple.quarantine
if [[ ${operating_system} == "Darwin" ]]; then
    xattr -d com.apple.quarantine "${AGNIX_BINARY_PATH}" 2> /dev/null || true
fi

echo "${GREEN_COLOR}Installed $("${AGNIX_BINARY_PATH}" --version) into src/bin/agnix${DEFAULT_COLOR}"
