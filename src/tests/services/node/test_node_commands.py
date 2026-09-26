import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.db import transaction

from node.models import NodeCommand
from node.node_commands import (
    COMMAND_LIFETIME_SECONDS_BY_KIND,
    EXPIRED_COMMAND_ERROR_MESSAGE,
    ProgressStep,
    ProgressStepState,
    cancel_node_command,
    claim_next_node_command,
    create_node_command,
    delete_old_node_commands,
    expire_pending_node_commands,
    finish_node_command,
    interrupt_running_node_commands,
    read_progress_steps,
    record_node_command_progress,
)
from tests.services.node.database_threads import run_concurrently, wait_until_a_transaction_waits_for_a_lock

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
WORKER_INSTANCE_ID = uuid4()
SHORT_LIVED_KINDS = {
    NodeCommand.Kind.FACTORY_RESET,
    NodeCommand.Kind.CONFIGURE_NODE,
    NodeCommand.Kind.REBOOT_NODE,
    NodeCommand.Kind.START_PAIRING,
}


def create_command(kind: NodeCommand.Kind = NodeCommand.Kind.SEND_ADVERT, created_at: datetime = NOW) -> NodeCommand:
    return create_node_command(kind, {"flood": False}, created_at)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", list(NodeCommand.Kind))
def test_a_command_lives_one_minute_when_it_must_not_run_late_and_five_minutes_otherwise(
    kind: NodeCommand.Kind,
) -> None:
    node_command = create_command(kind)

    expected_lifetime_seconds = 60 if kind in SHORT_LIVED_KINDS else 300
    assert COMMAND_LIFETIME_SECONDS_BY_KIND[kind] == expected_lifetime_seconds
    assert node_command.expires_at == NOW + timedelta(seconds=expected_lifetime_seconds)
    assert node_command.state == NodeCommand.State.PENDING
    assert node_command.arguments == {"flood": False}


@pytest.mark.django_db
def test_pending_commands_expire_by_their_kind() -> None:
    factory_reset = create_command(NodeCommand.Kind.FACTORY_RESET)
    send_advert = create_command(NodeCommand.Kind.SEND_ADVERT)

    assert [command.pk for command in expire_pending_node_commands(NOW + timedelta(seconds=59))] == []
    assert [command.pk for command in expire_pending_node_commands(NOW + timedelta(seconds=60))] == [factory_reset.pk]
    assert [command.pk for command in expire_pending_node_commands(NOW + timedelta(seconds=300))] == [send_advert.pk]

    factory_reset.refresh_from_db()
    assert factory_reset.state == NodeCommand.State.EXPIRED
    assert factory_reset.finished_at == NOW + timedelta(seconds=60)
    assert factory_reset.error_message == EXPIRED_COMMAND_ERROR_MESSAGE


@pytest.mark.django_db
def test_a_claim_takes_the_oldest_pending_command_and_never_one_past_its_expiry() -> None:
    expired_reboot = create_command(NodeCommand.Kind.REBOOT_NODE, created_at=NOW - timedelta(seconds=90))
    first_advert = create_command()
    create_command()

    claimed_command = claim_next_node_command(WORKER_INSTANCE_ID, NOW)

    assert claimed_command is not None
    assert claimed_command.pk == first_advert.pk
    assert claimed_command.state == NodeCommand.State.RUNNING
    assert claimed_command.claimed_at == NOW
    assert claimed_command.claimed_by_worker_instance == WORKER_INSTANCE_ID
    expired_reboot.refresh_from_db()
    assert expired_reboot.state == NodeCommand.State.EXPIRED


@pytest.mark.django_db
def test_only_one_command_runs_at_a_time() -> None:
    create_command()
    create_command()
    assert claim_next_node_command(WORKER_INSTANCE_ID, NOW) is not None

    assert claim_next_node_command(WORKER_INSTANCE_ID, NOW) is None
    assert NodeCommand.objects.filter(state=NodeCommand.State.RUNNING).count() == 1


@pytest.mark.django_db
def test_nothing_is_claimed_when_nothing_is_pending() -> None:
    assert claim_next_node_command(WORKER_INSTANCE_ID, NOW) is None


@pytest.mark.django_db(transaction=True)
def test_a_claim_skips_a_command_another_connection_has_locked() -> None:
    locked_command = create_command()
    next_command = create_command()
    command_is_locked = threading.Event()
    claim_is_done = threading.Event()

    def hold_the_lock_of_the_oldest_command() -> None:
        with transaction.atomic():
            list(NodeCommand.objects.select_for_update().filter(id=locked_command.pk))
            command_is_locked.set()
            claim_is_done.wait(timeout=10)

    def claim_while_the_oldest_is_locked() -> int | None:
        command_is_locked.wait(timeout=10)
        claimed_command = claim_next_node_command(WORKER_INSTANCE_ID, NOW)
        claim_is_done.set()
        return claimed_command.pk if claimed_command else None

    _nothing, claimed_command_id = run_concurrently(
        hold_the_lock_of_the_oldest_command, claim_while_the_oldest_is_locked
    )

    assert claimed_command_id == next_command.pk
    locked_command.refresh_from_db()
    assert locked_command.state == NodeCommand.State.PENDING


@pytest.mark.django_db(transaction=True)
def test_a_cancel_that_waits_for_a_claim_finds_the_command_running_and_changes_nothing() -> None:
    node_command = create_command()
    command_is_locked = threading.Event()

    def claim_slowly() -> bool:
        with transaction.atomic():
            list(NodeCommand.objects.select_for_update().filter(id=node_command.pk))
            command_is_locked.set()
            wait_until_a_transaction_waits_for_a_lock()
            NodeCommand.objects.filter(id=node_command.pk).update(state=NodeCommand.State.RUNNING, claimed_at=NOW)
        return True

    def cancel_during_the_claim() -> bool:
        command_is_locked.wait(timeout=10)
        return cancel_node_command(node_command.pk, NOW)

    _claimed, cancelled = run_concurrently(claim_slowly, cancel_during_the_claim)

    assert cancelled is False
    node_command.refresh_from_db()
    assert node_command.state == NodeCommand.State.RUNNING


@pytest.mark.django_db(transaction=True)
def test_exactly_one_of_a_claim_and_a_cancel_racing_for_the_same_command_wins() -> None:
    for _round_number in range(10):
        node_command = create_command()

        claimed, cancelled = race_a_claim_against_a_cancel(node_command.pk)

        assert claimed != cancelled
        NodeCommand.objects.filter(state=NodeCommand.State.RUNNING).update(
            state=NodeCommand.State.SUCCEEDED, finished_at=NOW
        )


def race_a_claim_against_a_cancel(node_command_id: int) -> list[bool]:
    start_together = threading.Barrier(2)

    def claim() -> bool:
        start_together.wait(timeout=10)
        return claim_next_node_command(WORKER_INSTANCE_ID, NOW) is not None

    def cancel() -> bool:
        start_together.wait(timeout=10)
        return cancel_node_command(node_command_id, NOW)

    return run_concurrently(claim, cancel)


@pytest.mark.django_db
def test_a_cancelled_command_is_never_claimed() -> None:
    node_command = create_command()

    assert cancel_node_command(node_command.pk, NOW) is True
    assert cancel_node_command(node_command.pk, NOW) is False
    assert claim_next_node_command(WORKER_INSTANCE_ID, NOW) is None
    node_command.refresh_from_db()
    assert node_command.state == NodeCommand.State.CANCELLED
    assert node_command.finished_at == NOW


@pytest.mark.django_db
def test_progress_keeps_one_entry_per_step_in_the_order_the_steps_started() -> None:
    node_command = create_command()
    later = NOW + timedelta(seconds=2)

    record_node_command_progress(
        node_command.pk, ProgressStep(step="a", state=ProgressStepState.RUNNING, detail="", at=NOW)
    )
    record_node_command_progress(
        node_command.pk, ProgressStep(step="b", state=ProgressStepState.RUNNING, detail="", at=NOW)
    )
    record_node_command_progress(
        node_command.pk, ProgressStep(step="a", state=ProgressStepState.DONE, detail="took 2 s", at=later)
    )

    node_command.refresh_from_db()
    assert read_progress_steps(node_command) == [
        ProgressStep(step="a", state=ProgressStepState.DONE, detail="took 2 s", at=later),
        ProgressStep(step="b", state=ProgressStepState.RUNNING, detail="", at=NOW),
    ]


@pytest.mark.django_db
def test_finishing_is_a_compare_and_set_on_a_running_command() -> None:
    node_command = create_command()
    assert finish_node_command(node_command.pk, NodeCommand.State.SUCCEEDED, NOW) is False

    claim_next_node_command(WORKER_INSTANCE_ID, NOW)
    assert finish_node_command(node_command.pk, NodeCommand.State.SUCCEEDED, NOW, result={"sent": True}) is True
    assert finish_node_command(node_command.pk, NodeCommand.State.FAILED, NOW, error_message="late") is False

    node_command.refresh_from_db()
    assert node_command.state == NodeCommand.State.SUCCEEDED
    assert node_command.result == {"sent": True}
    assert node_command.error_message == ""


@pytest.mark.django_db
def test_a_command_cannot_be_finished_in_a_state_that_is_not_terminal() -> None:
    node_command = create_command()

    with pytest.raises(ValueError, match="not a terminal state"):
        finish_node_command(node_command.pk, NodeCommand.State.RUNNING, NOW)


@pytest.mark.django_db
def test_commands_still_running_at_start_up_become_interrupted() -> None:
    create_command()
    create_command()
    running_command = claim_next_node_command(WORKER_INSTANCE_ID, NOW)
    assert running_command is not None

    interrupted_commands = interrupt_running_node_commands(NOW + timedelta(minutes=1))

    assert [command.pk for command in interrupted_commands] == [running_command.pk]
    running_command.refresh_from_db()
    assert running_command.state == NodeCommand.State.INTERRUPTED
    assert NodeCommand.objects.filter(state=NodeCommand.State.PENDING).count() == 1


@pytest.mark.django_db
def test_commands_older_than_ninety_days_are_deleted() -> None:
    old_command = create_command(created_at=NOW - timedelta(days=91))
    recent_command = create_command(created_at=NOW - timedelta(days=89))

    assert delete_old_node_commands(NOW) == 1
    assert not NodeCommand.objects.filter(id=old_command.pk).exists()
    assert NodeCommand.objects.filter(id=recent_command.pk).exists()
