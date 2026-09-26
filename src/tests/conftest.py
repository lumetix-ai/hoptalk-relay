import os
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from asgiref.sync import sync_to_async
from django.conf import settings as django_settings
from django.contrib.auth.hashers import make_password
from django.db import connections
from pytest_django import Settings

from hoptalk_relay.relay_settings import OperatorCredentials, RelaySettings
from tests.manual_clock import ManualClock
from tests.panel_operator import PanelOperator

# Worker tests and end-to-end scenarios live in sibling directories and share these fixtures;
# pytest only accepts plugin registrations in this top-level conftest.
pytest_plugins = ["tests.worker.fake_node.fixtures"]

TEST_DATABASE_SUFFIX_PATTERN = re.compile(r"[A-Za-z0-9_]{1,30}")
MANUAL_CLOCK_START = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def pytest_configure() -> None:
    # Checked before the session starts: a usage error raised by the database fixture would be
    # reported again for every test that needs the database.
    test_database_suffix = read_test_database_suffix()
    if test_database_suffix and not TEST_DATABASE_SUFFIX_PATTERN.fullmatch(test_database_suffix):
        raise pytest.UsageError("TEST_DATABASE_SUFFIX may only hold up to 30 letters, digits and underscores.")


def read_test_database_suffix() -> str:
    return os.environ.get("TEST_DATABASE_SUFFIX", "")


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings_parallel_suffix: None) -> None:
    """Give this run its own test database when TEST_DATABASE_SUFFIX is set.

    Several runs can then share one PostgreSQL at the same time: with TEST_DATABASE_SUFFIX=alice
    the test database is test_hoptalk_relay_alice.
    """
    append_test_database_suffix(read_test_database_suffix())


def append_test_database_suffix(test_database_suffix: str) -> None:
    if not test_database_suffix:
        return

    for database_settings in django_settings.DATABASES.values():
        test_settings = cast(dict[str, Any], database_settings.setdefault("TEST", {}))
        test_database_name = test_settings.get("NAME") or f"test_{database_settings['NAME']}"
        test_settings["NAME"] = f"{test_database_name}_{test_database_suffix}"


@pytest.fixture(autouse=True)
def use_plain_static_files_storage(settings: Settings) -> None:
    # ManifestStaticFilesStorage needs the manifest that only collectstatic writes.
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture(autouse=True)
def use_fast_password_hasher(settings: Settings) -> None:
    # Argon2 stays in the list, so the real hashes in src/.env and in the code still verify.
    settings.PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.MD5PasswordHasher",
        "django.contrib.auth.hashers.Argon2PasswordHasher",
    ]


@pytest.fixture
async def close_sync_to_async_thread_connections() -> AsyncIterator[None]:
    """Use it in every async test that reaches the ORM through sync_to_async.

    Such tests also need @pytest.mark.django_db(transaction=True): the executor thread has a
    connection of its own, outside pytest-django's rollback.
    """
    yield
    # pytest-django never closes the executor thread's connection; left checked out of the
    # pool, it makes DROP DATABASE fail at the end of the run.
    await sync_to_async(connections.close_all)()


@pytest.fixture
def manual_clock() -> ManualClock:
    return ManualClock(current_time=MANUAL_CLOCK_START)


@pytest.fixture
def panel_operator(settings: Settings) -> PanelOperator:
    """Configure a known operator for the panel, whatever src/.env holds."""
    panel_operator = PanelOperator(username="operator", password="correct horse battery staple")
    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    settings.RELAY_SETTINGS = replace(
        relay_settings,
        operator_credentials=OperatorCredentials(
            username=panel_operator.username,
            password_hash=make_password(panel_operator.password),
        ),
    )
    return panel_operator
