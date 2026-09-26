.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

# Every recipe below is one shell script, which needs .ONESHELL and therefore GNU Make
# 3.82 or newer. macOS still ships 3.81, where .ONESHELL is silently ignored and each
# line runs in its own shell, so an if block fails with an unexplained syntax error.
requiredMakeVersion = 3.82
ifneq ($(firstword $(sort $(MAKE_VERSION) $(requiredMakeVersion))),$(requiredMakeVersion))
$(error GNU Make $(requiredMakeVersion) or newer is required, and this is $(MAKE_VERSION). On macOS: brew install make, then put /opt/homebrew/opt/make/libexec/gnubin first on the PATH.)
endif

projectName = HopTalk Relay
serviceName = app
relayServiceName = relay
composeProjectName = $(or $(HOPTALK_COMPOSE_PROJECT_NAME),hoptalk-relay)
containerName = $(composeProjectName)-$(serviceName)
# No TTY when make itself runs without one (continuous integration, a script, an agent):
# docker compose exec refuses to allocate a TTY there. TEST_DATABASE_SUFFIX is passed on
# so that several test runs can share the database without sharing a test database. Given
# by name only, Compose copies its value from the environment, so the value never becomes
# part of a shell command; $(value) keeps Make from expanding it. tests/conftest.py checks it.
composeExecOptions = $(if $(or $(CI),$(shell [ -t 0 ] || echo no-terminal)),--no-TTY,) \
	$(if $(value TEST_DATABASE_SUFFIX),--env TEST_DATABASE_SUFFIX,)
executor ?= container
.ONESHELL:
.SILENT:
SHELL = /bin/bash

# tput needs a terminal type. Without one (a script, an agent) the banners stay plain rather
# than printing an error for every colour change.
ifeq ($(TERM),)
export TERM := dumb
endif

# A file or directory named like a recipe would otherwise count as that target, already up
# to date, and Make would run nothing without saying so because of .SILENT.
.PHONY: check-all check-claude check-dockerfile check-lock check-security check-style check-templates \
	check-tests check-types container-build container-down container-execute container-logs container-ps \
	container-restart container-up fix-all fix-style fix-templates generate-admin-password generate-secret-key \
	generate-tls-certificate help manage npm relay-logs relay-restart serial-bridge setup update-radio-presets uv

# The image runs as the account that owns the mounted directories, so bind-mounted files
# stay writable on Linux without a chown at start-up.
export APP_UID := $(shell id -u)
export APP_GID := $(shell id -g)

define handleError
	echo ''
	echo '$(shell tput setab 1)$(shell echo $@ | sed 's/./ /g')                                                                              $(shell tput sgr0)'
	echo '$(shell tput setab 1) $(shell tput setaf 7)[ERROR] The recipe $(shell tput bold)$@$(shell tput sgr0)$(shell tput setab 1)$(shell tput setaf 7) was executed with errors. Please check the output above. $(shell tput sgr0)'
	echo '$(shell tput setab 1)$(shell echo $@ | sed 's/./ /g')                                                                              $(shell tput sgr0)'
	echo ''

	# Exit with non zero exit code to allow handling the error
	exit 1
endef

define handleSuccess
	echo ''
	echo '$(shell tput setab 2)$(shell echo $@ | sed 's/./ /g')                                                     $(shell tput sgr0)'
	echo '$(shell tput setab 2) $(shell tput setaf 0)[OK] The recipe $(shell tput bold)$@$(shell tput sgr0)$(shell tput setab 2)$(shell tput setaf 0) was executed successfully. Cheers! $(shell tput sgr0)'
	echo '$(shell tput setab 2)$(shell echo $@ | sed 's/./ /g')                                                     $(shell tput sgr0)'
	echo ''
endef

define runCommand
	if $1; then
		$(call handleSuccess)
	else
		$(call handleError)
	fi
endef

define runCommandInContainer
    if [ ! `docker ps -q -f "status=running" -f "name=$(containerName)"` ]; then
        $(MAKE) container-up
    fi

    if ../scripts/docker-compose.sh exec $(composeExecOptions) $(serviceName) $1; then
        $(call handleSuccess)
    else
        $(call handleError)
    fi
endef

define runManageCommand
	$(call runCommandInContainer,python manage.py $1)
endef

# Skips the entrypoint, and with it the checks that stop the application from serving a
# broken configuration. Whatever a first run needs before the container can boot has to
# be produced this way, because until it exists the container is restarting in a loop
# and nothing can be executed inside it.
define runCommandInThrowawayContainer
	if ../scripts/docker-compose.sh run --rm --no-deps --no-TTY --entrypoint $1 $(serviceName) $2; then
		$(call handleSuccess)
	else
		$(call handleError)
	fi
endef

# Neither Python nor uv is installed on the development machine, so everything runs in
# the container by default. Continuous integration sets executor=host to reuse the
# runner's own Python rather than building an image for every check.
define runPythonCommand
	if [[ "$(executor)" == "host" ]]; then
		$(call runCommand, $1)
	else
		$(call runCommandInContainer, $1)
	fi
endef

check-all: ## Check codebase with all checkers
	if [[ "$(executor)" == "container" ]]; then
		$(MAKE) container-up
	fi

	$(call runCommand, \
		$(MAKE) --jobs=4 --keep-going --output-sync \
			check-claude \
			check-dockerfile \
			check-lock \
			check-security \
			check-style \
			check-templates \
			check-tests \
			check-types \
	)

check-claude: ## Check the [#yellow].claude[#black] configuration with agnix
	if [ ! -x bin/agnix ]; then
		echo 'The agnix binary is missing. Run ../scripts/install-agnix.sh to install it.'
		$(call handleError)
	fi

	$(call runCommand, bin/agnix ../.claude)

check-dockerfile: ## Validate the Dockerfile
	$(call runCommand, docker run --rm -i hadolint/hadolint < ../docker/images/app/Dockerfile)

check-lock: ## Check that [#yellow]uv.lock[#black] matches pyproject.toml
	$(call runPythonCommand, uv lock --check)

check-security: ## Check the locked packages for known vulnerabilities with [#yellow]pip-audit[#black]
	$(call runPythonCommand, bash -c "uv export --frozen --format requirements-txt --no-emit-project --quiet \
		| pip-audit --disable-pip --requirement /dev/stdin")

check-style: ## Check the coding style with [#yellow]ruff[#black]
	$(call runPythonCommand, ruff check)
	$(call runPythonCommand, ruff format --check)

check-templates: ## Check the templates with [#yellow]djlint[#black]
	$(call runPythonCommand, djlint . --lint)
	$(call runPythonCommand, djlint . --check)

check-tests: ## Run the [#yellow]pytest[#black] test suite, like [#yellow]make check-tests command="tests/protocol -q"[#black]
	$(call runPythonCommand, pytest $(command))

check-types: ## Analyse the types with [#yellow]mypy[#black]
	$(call runPythonCommand, mypy)

container-build: ## Build App images
	$(call runCommand, ../scripts/docker-compose.sh build --pull)

container-down: ## Stop App containers
	$(call runCommand, ../scripts/docker-compose.sh --progress quiet down)

container-execute: ## Execute a command in a running container ([#yellow]bash[#black] is the default command)
	if [[ "$(command)" == "" ]]; then
		$(call runCommandInContainer, "/bin/bash")
	else
		@$(call runCommandInContainer, $(command))
	fi

container-logs: ## Follow the App container logs
	$(call runCommand, ../scripts/docker-compose.sh logs --follow --tail 100)

container-ps: ## Show App containers state
	$(call runCommand, ../scripts/docker-compose.sh ps)

container-restart: ## Restart App containers
	$(call runCommand, $(MAKE) container-down && $(MAKE) container-up)

# Every service in the selected Compose files: app, relay and database. The
# development-only node service stays out of it, because it is behind a Compose profile.
container-up: ## Start App containers
	$(call runCommand, ../scripts/docker-compose.sh --progress quiet up -d)
	$(MAKE) container-ps

fix-all: ## Fix everything that can be fixed automatically
	$(call runCommand, $(MAKE) fix-style && $(MAKE) fix-templates)

fix-style: ## Fix the coding style with [#yellow]ruff[#black]
	$(call runPythonCommand, ruff check --fix)
	$(call runPythonCommand, ruff format)

# djlint exits with 1 whenever it changed a file, so the check that follows decides whether
# anything is still wrong.
fix-templates: ## Reformat the templates with [#yellow]djlint[#black]
	$(call runPythonCommand, bash -c "djlint . --reformat || djlint . --check")

generate-admin-password: ## Generate the Argon2 hash of the operator password
	read -rsp 'Operator password: ' operatorPassword
	echo ''
	read -rsp 'Repeat the password: ' repeatedPassword
	echo ''

	if [[ "$$operatorPassword" != "$$repeatedPassword" ]]; then
		echo 'The passwords do not match.'
		$(call handleError)
	fi

	if [[ $${#operatorPassword} -lt 12 ]]; then
		echo 'The password is shorter than 12 characters.'
		$(call handleError)
	fi

	echo 'Put the line below in src/.env as it is: the application reads the file itself,'
	echo 'so the dollar signs need no quotes.'
	echo ''

	# Only the hasher's own settings: the application settings would refuse to load without
	# the very line this produces.
	if printf '%s' "$$operatorPassword" | ../scripts/docker-compose.sh run --rm --no-deps --no-TTY --entrypoint python $(serviceName) \
		-c 'import sys; from django.conf import settings; settings.configure(PASSWORD_HASHERS=["django.contrib.auth.hashers.Argon2PasswordHasher"]); from django.contrib.auth.hashers import make_password; print("ADMIN_PASSWORD_HASH=" + make_password(sys.stdin.read()))'; then
		$(call handleSuccess)
	else
		$(call handleError)
	fi

generate-secret-key: ## Generate the Django secret key
	echo 'Put the line below in src/.env, and keep it: a key that changes on every deployment'
	echo 'signs the operator out on every deployment.'
	echo ''

	if ../scripts/docker-compose.sh run --rm --no-deps --no-TTY --entrypoint python $(serviceName) \
		-c 'import secrets; print("SECRET_KEY=" + secrets.token_urlsafe(50))'; then
		$(call handleSuccess)
	else
		$(call handleError)
	fi

generate-tls-certificate: ## Issue the self-signed TLS certificate for the LAN address
	$(call runCommand, ../scripts/generate-tls-certificate.sh $(command))

manage: ## Run a Django management command, like [#yellow]make manage "migrate"[#black]
	if [[ "$(command)" == "" ]]; then
		$(call runManageCommand, help)
	else
		$(call runManageCommand, $(command))
	fi

npm: ## Run an npm command, like [#yellow]make npm "run build"[#black]
	if [[ "$(command)" == "" ]]; then
		$(call runCommand, ../scripts/docker-compose.sh run --rm --no-deps node npm)
	else
		$(call runCommand, ../scripts/docker-compose.sh run --rm --no-deps node npm $(command))
	fi

relay-logs: ## Follow the relay worker's log
	$(call runCommand, ../scripts/docker-compose.sh logs --follow --tail 100 $(relayServiceName))

# In development the worker's code is bind-mounted but not reloaded automatically.
relay-restart: ## Restart the relay worker
	$(call runCommand, ../scripts/docker-compose.sh restart $(relayServiceName))

# macOS development only: Docker Desktop cannot pass the USB node into a container.
serial-bridge: ## Bridge the USB node to local port [#yellow]5055[#black] for the relay container (macOS)
	$(call runCommand, ../scripts/serial-bridge.sh)

setup: ## Prepare the project for its first run
	# Every step depends on the one before it, and a failure that is stepped over is
	# reported by a later step in terms that no longer name the cause.
	set -e

	# Run bare rather than through runCommand: its banner names the recipe it runs
	# inside, and a green "setup was executed successfully" three steps in is a lie.
	../scripts/install-agnix.sh

	if [ ! -f ../docker/.env ]; then
		cp ../docker/.env.example ../docker/.env
	fi

	if [ ! -f .env ]; then
		cp .env.example .env
	fi

	# Compose refuses to read its files while the database password is empty, so it is
	# produced on the host, before anything runs in a container.
	if grep --quiet '^POSTGRES_PASSWORD=$$' ../docker/.env; then
		databasePassword=$$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)

		sed -i.backup "s|^POSTGRES_PASSWORD=$$|POSTGRES_PASSWORD=$${databasePassword}|" ../docker/.env
		rm -f ../docker/.env.backup

		echo 'Wrote a new POSTGRES_PASSWORD into docker/.env.'
	fi

	$(MAKE) generate-tls-certificate
	$(MAKE) container-build

	# The development image serves the stylesheet from src/build, so it has to exist
	# before the container can render a page.
	$(MAKE) npm command="ci"
	$(MAKE) npm command="run build"

	# The environment file is mounted read-only, so nothing in a container can write it;
	# the value is produced there and written here.
	if grep --quiet '^SECRET_KEY=$$' .env; then
		secretKey=$$(../scripts/docker-compose.sh run --rm --no-deps --no-TTY --entrypoint python $(serviceName) \
			-c 'import secrets; print(secrets.token_urlsafe(50), end="")')

		sed -i.backup "s|^SECRET_KEY=$$|SECRET_KEY=$${secretKey}|" .env
		rm -f .env.backup

		echo 'Wrote a new SECRET_KEY into src/.env.'
	fi

	if grep --quiet '^ADMIN_USERNAME=.' .env && grep --quiet '^ADMIN_PASSWORD_HASH=.' .env; then
		$(MAKE) container-up
		$(call handleSuccess)
	else
		$(call handleSuccess)

		echo 'One thing is left: the application refuses to start without an operator.'
		echo 'Set ADMIN_USERNAME in src/.env, add the line "make generate-admin-password" prints,'
		echo 'then run "make container-up".'
		echo ''
	fi

# The one recipe that needs the Internet; the snapshot it writes is committed and bundled, so
# the relay itself never fetches anything. The node directory is mounted explicitly because
# the production image carries its own copy of the code, which the file must not land in.
update-radio-presets: ## Download the current MeshCore radio presets into [#yellow]node/radio_presets.json[#black]
	$(call runCommand, ../scripts/docker-compose.sh run --rm --no-deps --no-TTY \
		--volume "$(CURDIR)/node:/app/node" --entrypoint python $(serviceName) -m node.radio_preset_updates)

uv: ## Run a uv command in a throwaway container, like [#yellow]make uv "add segno"[#black], then rebuild
	if [[ "$(command)" == "" ]]; then
		$(call runCommandInThrowawayContainer, uv,)
	else
		$(call runCommandInThrowawayContainer, uv, $(command))
	fi

# The recipes that hand the words after them on as their command, like "make m migrate".
# Spelled out in full, as in "make manage migrate", the recipe exists, so Make runs it
# directly rather than through .DEFAULT and would then take "migrate" for a recipe of its
# own. The words after it become its command here, and .DEFAULT skips them.
commandPassingRecipes = manage uv npm container-execute generate-tls-certificate
ifneq ($(filter $(firstword $(MAKECMDGOALS)),$(commandPassingRecipes)),)
ifneq ($(word 2,$(MAKECMDGOALS)),)
command ?= $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
stopExecution = yes
endif
endif

help:
	echo '  It is possible to use shortcuts for a recipe, like $(shell tput setaf 3)make c-u$(shell tput sgr0) or $(shell tput setaf 3)make c:u$(shell tput sgr0).'
	echo "  Escape commands with options using double or single quotes, like $(shell tput setaf 3)make m 'migrate --plan'$(shell tput sgr0) or $(shell tput setaf 3)make m \"migrate --plan\"$(shell tput sgr0)."
	echo ''
	grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	| sed -n 's/^\(.*\):.*##\(.*\)/$(shell tput setaf 2)  \1$(shell tput sgr0)  :::\2/p' \
	| sed 's/\[\#yellow\]/$(shell tput setaf 3)/g' \
	| sed 's/\[\#black\]/$(shell tput sgr0)/g' \
	| sort \
	| column -t -s  ':::'

	# Exit with non zero exit code to allow handling the error
	exit 1
.DEFAULT:
	if [[ "$(stopExecution)" == "" ]]; then
		$(eval normalizedShortcut := $(shell echo $@ | sed 's/:/-/g' | sed 's/-/[a-zA-Z0-9_]*-/g' | sed "s/\(.*\)/'  \1.*'/"))
		$(eval foundRecipes := $(shell grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sed -n 's/^\(.*\):.*##\(.*\)/  \1/p' | grep -E $(normalizedShortcut)))
		$(eval foundRecipesCount := $(shell echo $(foundRecipes) | tr ' ' '\n' | wc -l))
		if [[ "$(foundRecipesCount)" == "1" ]]; then
			if [[ "$(foundRecipes)" == "" ]]; then
				echo '$(shell tput setaf 3)Recipe$(shell tput sgr0) $(shell tput setaf 2)$@$(shell tput sgr0) $(shell tput setaf 3)is not found.$(shell tput sgr0)'
				echo ''
				echo 'Showing help.'
				echo ''
				$(MAKE) help
			else
				echo 'Executing recipe $(shell tput setaf 2)$(foundRecipes)$(shell tput sgr0)'
				echo ''

				$(eval parsedCommand := $(shell echo $(MAKECMDGOALS) | sed 's/[a-zA-Z0-9_:-]* //'))

				if [[ "$(filter $(foundRecipes),$(commandPassingRecipes))" != "" ]]; then
					$(eval stopExecution='yes')

					if [[ "$(parsedCommand)" == "$(MAKECMDGOALS)" ]]; then
						$(MAKE) $(foundRecipes)
					else
						$(MAKE) $(foundRecipes) command="$(parsedCommand)"
					fi
				else
					$(MAKE) $(foundRecipes)

					if [[ "$(parsedCommand)" != "$(MAKECMDGOALS)" ]]; then
						$(MAKE) $(parsedCommand)
					fi
				fi
			fi
		else
			echo '$(shell tput setaf 3)Multiple recipes are found.$(shell tput sgr0)'
			echo ''
			echo 'Please use one of the following recipes:'
			grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
				| sed -n 's/^\(.*\):.*##\(.*\)/$(shell tput setaf 2)  \1  :::$(shell tput sgr0)\2/p' \
				| sed 's/\[\#yellow\]/$(shell tput setaf 3)/g' \
				| sed 's/\[\#black\]/$(shell tput sgr0)/g' \
				| grep -E $(normalizedShortcut) \
				| sort \
				| column -t -s ':::'

			# Exit with non zero exit code to allow handling the error
			exit 1
		fi
	fi
