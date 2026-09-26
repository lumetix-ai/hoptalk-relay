#!/usr/bin/env bash

# ----------------------------------------------------------------------------------------------------------------------
# Prepare the container for its role and hand over to the command
#
# The web and the relay services run this same image. APP_ROLE=web renders the nginx
# configuration, checks the TLS material, verifies the application configuration, waits
# for the database and migrates it before supervisord starts nginx and gunicorn.
# APP_ROLE=relay only verifies the configuration and waits for the database: the web
# container owns the migrations, and the worker waits for them itself.
# ----------------------------------------------------------------------------------------------------------------------

set -eo pipefail

renderNginxConfiguration() {
    if [[ ${HTTPS_PORT:-443} == "443" ]]; then
        export PUBLIC_HTTPS_SUFFIX=""
    else
        export PUBLIC_HTTPS_SUFFIX=":${HTTPS_PORT}"
    fi

    envsubst '${PUBLIC_HTTPS_SUFFIX}' \
        < /etc/nginx/templates/app.conf.template \
        > /etc/nginx/conf.d/app.conf
}

# nginx serves under this image's own account, so it has to be able to read the key. When
# the image is built with an APP_UID other than the account that issued the certificate,
# OpenSSL reports a BIO error that names neither the cause nor the remedy.
checkTlsMaterial() {
    for material in /etc/nginx/tls/fullchain.crt /etc/nginx/tls/server.key; do
        if [[ ! -e ${material} ]]; then
            echo "${material} is missing. Run scripts/generate-tls-certificate.sh." >&2
            exit 1
        fi

        if [[ ! -r ${material} ]]; then
            echo "${material} cannot be read as $(id -un) ($(id -u):$(id -g))." >&2
            echo "It belongs to $(stat -c '%u:%g' "${material}") with mode $(stat -c '%a' "${material}")." >&2
            echo "Build the image with APP_UID and APP_GID set to the account that issued the certificate." >&2
            exit 1
        fi
    done
}

case "${APP_ROLE:-web}" in
    web)
        renderNginxConfiguration
        checkTlsMaterial

        # Refuses to continue when src/.env is incomplete or invalid, so a misconfigured
        # deployment stops here, naming the variable, rather than serving errors.
        python manage.py verify_configuration
        python manage.py wait_for_database
        python manage.py migrate --no-input

        nginx -t
        ;;
    relay)
        python manage.py verify_configuration
        python manage.py wait_for_database
        ;;
    *)
        echo "APP_ROLE must be \"web\" or \"relay\", and it is \"${APP_ROLE}\"." >&2
        exit 1
        ;;
esac

exec "$@"
