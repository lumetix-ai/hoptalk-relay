"""The maintenance task: it prunes as soon as the worker starts, and again at every interval."""

from datetime import timedelta

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone

from panel.models import OperatorLoginAttempt
from tests.worker.relay_worker.worker_harness import RelayWorkerHarness, in_database, wait_for_database

pytestmark = pytest.mark.django_db(transaction=True)


def create_operator_session(expires_in: timedelta) -> str:
    session = SessionStore()
    session["operator_authenticated_at"] = timezone.now().isoformat()
    session.create()
    Session.objects.filter(session_key=session.session_key).update(expire_date=timezone.now() + expires_in)
    return str(session.session_key)


def create_login_attempt(attempted_ago: timedelta) -> int:
    login_attempt = OperatorLoginAttempt.objects.create(
        client_address="192.0.2.10", succeeded=False, attempted_at=timezone.now() - attempted_ago
    )
    return login_attempt.pk


def list_session_keys() -> set[str]:
    return set(Session.objects.values_list("session_key", flat=True))


def list_login_attempt_ids() -> set[int]:
    return set(OperatorLoginAttempt.objects.values_list("id", flat=True))


async def test_expired_operator_sessions_and_old_login_attempts_are_deleted_when_the_worker_starts(
    relay_worker: RelayWorkerHarness,
) -> None:
    expired_session_key = await in_database(create_operator_session, timedelta(minutes=-1))
    live_session_key = await in_database(create_operator_session, timedelta(hours=1))
    old_login_attempt_id = await in_database(create_login_attempt, timedelta(days=2))
    recent_login_attempt_id = await in_database(create_login_attempt, timedelta(minutes=5))

    relay_worker.start()

    await wait_for_database(
        lambda: expired_session_key not in list_session_keys(), description="the expired session to be deleted"
    )
    await wait_for_database(
        lambda: old_login_attempt_id not in list_login_attempt_ids(), description="the old login attempt to be deleted"
    )
    assert await in_database(list_session_keys) == {live_session_key}
    assert await in_database(list_login_attempt_ids) == {recent_login_attempt_id}
