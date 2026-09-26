import re
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.management import call_command

import hoptalk_relay.settings as project_settings
from hoptalk_relay.relay_settings import RelaySettings

APPLICATION_TABLES = {
    "node_setting",
    "node_commands",
    "node_setup_runs",
    "worker_status",
    "pairing_sessions",
    "heard_adverts",
    "users",
    "contacts",
    "messages",
    "message_deliveries",
    "receipt_notifications",
    "refresh_sessions",
    "inbound_direct_messages",
    "outbound_packets",
    "operator_login_attempts",
}


def test_settings_load_the_validated_relay_configuration() -> None:
    assert isinstance(settings.RELAY_SETTINGS, RelaySettings)
    assert settings.USE_TZ is True


def test_passwords_are_hashed_with_argon2() -> None:
    # The live settings hold the fast hasher every test uses; the project's own list is read here.
    assert project_settings.PASSWORD_HASHERS[0] == "django.contrib.auth.hashers.Argon2PasswordHasher"


APPLICATIONS_THAT_MUST_STAY_OUT = ("django.contrib.auth", "django.contrib.contenttypes", "django.contrib.admin")


def test_the_panel_installs_no_second_users_table() -> None:
    for application_that_must_stay_out in APPLICATIONS_THAT_MUST_STAY_OUT:
        assert application_that_must_stay_out not in settings.INSTALLED_APPS


def test_the_data_model_has_the_fifteen_application_tables() -> None:
    application_labels = {"node", "directory", "messaging", "panel"}
    table_names = {model._meta.db_table for model in apps.get_models() if model._meta.app_label in application_labels}
    assert table_names == APPLICATION_TABLES


@pytest.mark.django_db
def test_every_model_change_has_a_migration() -> None:
    # Exits with status 1, which fails the test, when a model differs from its migrations.
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


PANEL_TEMPLATES_DIRECTORY = Path(settings.BASE_DIRECTORY) / "panel" / "templates"
STATIC_TAG_PATTERN = re.compile(r"""{%\s*static\s+['"](?P<static_path>[^'"]+)['"]\s*%}""")
# Produced by "npm run build", which a checkout that only runs the Python checks never runs.
TAILWIND_BUILD_OUTPUT = "app.css"


def find_static_paths_the_templates_reference() -> set[str]:
    referenced_static_paths: set[str] = set()
    for template_path in PANEL_TEMPLATES_DIRECTORY.rglob("*.html"):
        template_text = template_path.read_text(encoding="utf-8")
        referenced_static_paths.update(match["static_path"] for match in STATIC_TAG_PATTERN.finditer(template_text))
    return referenced_static_paths


def test_every_static_file_the_templates_reference_is_found_without_a_build_step() -> None:
    referenced_static_paths = find_static_paths_the_templates_reference() - {TAILWIND_BUILD_OUTPUT}

    missing_static_paths = sorted(path for path in referenced_static_paths if finders.find(path) is None)

    assert referenced_static_paths
    assert missing_static_paths == []
