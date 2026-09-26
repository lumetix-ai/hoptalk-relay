"""Pairing mode against the fake node: adverts at the interval, adverts heard, contacts added, end and resume."""

import pytest
from django.utils import timezone

from directory.contacts import add_contact_from_heard_advert
from directory.models import Contact
from node.models import HeardAdvert, NodeCommand, PairingSession
from node.node_commands import create_node_command
from node.pairing_sessions import stop_pairing_session
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.radio_packets import AdvertPacket
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    RelayWorkerHarness,
    configure_relay_node,
    in_database,
    wait_for_database,
)
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

ADVERT_INTERVAL_SECONDS = 10
PAIRING_DURATION_SECONDS = 60


def count_adverts(firmware: FakeCompanionFirmware) -> int:
    return sum(1 for packet in firmware.transmitted_packets if isinstance(packet, AdvertPacket))


def read_pairing_session(pairing_session_id: int) -> PairingSession:
    return PairingSession.objects.get(id=pairing_session_id)


async def start_pairing(relay_worker: RelayWorkerHarness, *, advert_flood: bool = False) -> int:
    start_command = await in_database(
        create_node_command,
        NodeCommand.Kind.START_PAIRING,
        {
            "duration_seconds": PAIRING_DURATION_SECONDS,
            "advert_interval_seconds": ADVERT_INTERVAL_SECONDS,
            "advert_flood": advert_flood,
        },
        relay_worker.clock.now(),
    )
    await wait_for_database(
        lambda: NodeCommand.objects.get(id=start_command.pk).state == NodeCommand.State.SUCCEEDED,
        description="pairing to start",
    )
    finished_command = await in_database(NodeCommand.objects.get, id=start_command.pk)
    assert finished_command.result is not None
    pairing_session_id: int = finished_command.result["pairing_session_id"]
    return pairing_session_id


async def wait_for_advert_count(
    firmware: FakeCompanionFirmware, pairing_session_id: int, expected_advert_count: int
) -> None:
    """Wait until the node sent the advert and the worker recorded it.

    The worker takes the time the next advert counts from after the node has sent this one, and
    records the advert after that: a clock moved forward before then would push the next one away.
    """
    await wait_until(
        lambda: count_adverts(firmware) == expected_advert_count, description=f"advert {expected_advert_count}"
    )
    await wait_for_database(
        lambda: read_pairing_session(pairing_session_id).adverts_sent == expected_advert_count,
        description=f"the worker to record advert {expected_advert_count}",
    )


async def start_running_relay(relay_worker: RelayWorkerHarness, firmware: FakeCompanionFirmware) -> None:
    await configure_relay_node(firmware)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)


async def test_adverts_go_out_at_the_interval_until_the_session_ends(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)

    pairing_session_id = await start_pairing(relay_worker)
    await wait_for_advert_count(fake_companion_firmware, pairing_session_id, 1)

    for expected_advert_count in (2, 3):
        relay_worker.clock.advance(seconds=ADVERT_INTERVAL_SECONDS)
        await wait_for_advert_count(fake_companion_firmware, pairing_session_id, expected_advert_count)

    relay_worker.clock.advance(seconds=PAIRING_DURATION_SECONDS)
    await wait_for_database(
        lambda: read_pairing_session(pairing_session_id).state == PairingSession.State.ENDED,
        description="the session to end",
    )
    ended_session = await in_database(read_pairing_session, pairing_session_id)
    assert ended_session.adverts_sent == 3
    assert not any(
        packet.route.is_flood
        for packet in fake_companion_firmware.transmitted_packets
        if isinstance(packet, AdvertPacket)
    )
    relay_worker.clock.advance(seconds=ADVERT_INTERVAL_SECONDS)
    await relay_worker.clock.sleep(0.2)
    assert count_adverts(fake_companion_firmware) == 3


async def test_a_heard_advert_is_captured_and_once_added_the_node_holds_the_contact(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)
    newcomer = simulated_mesh.add_device("newcomer", relay_knows_device=False)
    pairing_session_id = await start_pairing(relay_worker)

    newcomer.send_advert()

    await wait_for_database(
        lambda: HeardAdvert.objects.filter(pairing_session_id=pairing_session_id).exists(),
        description="the heard advert",
    )
    heard_advert = await in_database(HeardAdvert.objects.get, pairing_session_id=pairing_session_id)
    assert heard_advert.public_key == newcomer.public_key.hex()
    assert heard_advert.name == "newcomer"
    assert heard_advert.contact_record["public_key"] == newcomer.public_key.hex()

    contact = await in_database(add_contact_from_heard_advert, heard_advert, timezone.now())

    await wait_for_database(
        lambda: Contact.objects.get(id=contact.pk).node_sync_state == Contact.NodeSyncState.ON_NODE,
        description="the new contact on the node",
    )
    stored_record = fake_companion_firmware.find_contact(newcomer.public_key)
    assert stored_record is not None
    assert not stored_record.has_known_route


async def test_the_relays_own_advert_is_never_captured(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)
    await start_pairing(relay_worker)

    await relay_worker.worker.pairing_advertiser.capture_heard_advert(
        {"public_key": fake_companion_firmware.public_key.hex(), "type": 1, "adv_name": "itself"}
    )

    assert not await in_database(HeardAdvert.objects.exists)


async def test_a_session_resumes_after_a_worker_restart(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)
    pairing_session_id = await start_pairing(relay_worker)

    await relay_worker.restart()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    assert (await in_database(read_pairing_session, pairing_session_id)).state == PairingSession.State.ACTIVE

    relay_worker.clock.advance(seconds=ADVERT_INTERVAL_SECONDS)

    await wait_for_database(
        lambda: read_pairing_session(pairing_session_id).adverts_sent == 2,
        description="the resumed session's next advert",
    )


async def test_a_stopped_session_sends_no_more_adverts(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)
    pairing_session_id = await start_pairing(relay_worker)
    now = relay_worker.clock.now()

    assert await in_database(stop_pairing_session, pairing_session_id, now)
    await in_database(
        create_node_command, NodeCommand.Kind.STOP_PAIRING, {"pairing_session_id": pairing_session_id}, now
    )
    relay_worker.clock.advance(seconds=ADVERT_INTERVAL_SECONDS)
    await relay_worker.clock.sleep(0.3)

    assert count_adverts(fake_companion_firmware) == 1


async def test_leaving_relay_mode_running_stops_the_session(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)
    pairing_session_id = await start_pairing(relay_worker)

    fake_companion_firmware.power_off()

    await wait_for_database(
        lambda: read_pairing_session(pairing_session_id).state == PairingSession.State.STOPPED,
        description="the session stopped with the node gone",
    )
