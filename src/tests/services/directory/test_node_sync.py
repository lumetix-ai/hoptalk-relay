"""What the reconciler records about the node's copy of the contacts table."""

from datetime import timedelta

import pytest

from directory.models import Contact
from directory.node_sync import (
    list_contacts_for_node,
    mark_contact_add_failed,
    mark_contact_on_node,
    return_contact_to_pending_add,
)
from messaging.models import OutboundPacket
from messaging.outbound_packets import count_pending_route_resets
from tests.services.directory.row_builders import (
    ROW_CREATION_TIME,
    build_public_key,
    create_contact,
    create_outbound_packet,
)

pytestmark = pytest.mark.django_db

SYNCED_AT = ROW_CREATION_TIME + timedelta(minutes=5)


def test_contacts_are_listed_in_id_order_with_what_their_node_record_needs() -> None:
    later_contact = create_contact(2, node_sync_state=Contact.NodeSyncState.PENDING_ADD)
    earlier_contact = create_contact(1)
    Contact.objects.filter(id=later_contact.pk).update(
        latitude_microdegrees=-37_000_000, advert_timestamp=1_790_000_000
    )

    contacts_for_node = list_contacts_for_node()

    assert [contact.contact_id for contact in contacts_for_node] == sorted([earlier_contact.pk, later_contact.pk])
    listed_later_contact = next(contact for contact in contacts_for_node if contact.contact_id == later_contact.pk)
    assert listed_later_contact.public_key == build_public_key(2)
    assert listed_later_contact.latitude_microdegrees == -37_000_000
    assert listed_later_contact.advert_timestamp == 1_790_000_000
    assert listed_later_contact.node_sync_state == Contact.NodeSyncState.PENDING_ADD


def test_a_contact_found_on_the_node_is_on_node_with_its_mirrors_and_the_time_it_got_there() -> None:
    contact = create_contact(1, node_sync_state=Contact.NodeSyncState.ADD_FAILED)
    Contact.objects.filter(id=contact.pk).update(node_sync_error="the node's contact table is full")

    assert mark_contact_on_node(
        contact.pk, contact.public_key, node_name="Tracker", node_out_path_length=2, now=SYNCED_AT
    )

    contact.refresh_from_db()
    assert contact.node_sync_state == Contact.NodeSyncState.ON_NODE
    assert contact.node_sync_error == ""
    assert contact.node_synced_at == SYNCED_AT
    assert (contact.node_name, contact.node_out_path_length) == ("Tracker", 2)


def test_a_contact_already_on_node_keeps_the_time_it_got_there_while_its_mirrors_follow_the_node() -> None:
    contact = create_contact(1)
    mark_contact_on_node(contact.pk, contact.public_key, node_name="Tracker", node_out_path_length=-1, now=SYNCED_AT)

    later = SYNCED_AT + timedelta(minutes=10)
    mark_contact_on_node(contact.pk, contact.public_key, node_name="Renamed", node_out_path_length=1, now=later)

    contact.refresh_from_db()
    assert contact.node_synced_at == SYNCED_AT
    assert (contact.node_name, contact.node_out_path_length) == ("Renamed", 1)


def test_a_contact_deleted_meanwhile_is_not_written() -> None:
    contact = create_contact(1)
    contact_id, public_key = contact.pk, contact.public_key
    contact.delete()

    assert not mark_contact_on_node(contact_id, public_key, node_name="", node_out_path_length=-1, now=SYNCED_AT)
    assert not mark_contact_add_failed(contact_id, public_key, "refused", SYNCED_AT)


def test_a_contact_added_again_under_a_new_id_is_not_mistaken_for_the_old_one() -> None:
    contact = create_contact(1, node_sync_state=Contact.NodeSyncState.PENDING_ADD)
    old_contact_id = contact.pk
    contact.delete()
    new_contact = create_contact(1, node_sync_state=Contact.NodeSyncState.PENDING_ADD)

    assert not mark_contact_add_failed(old_contact_id, new_contact.public_key, "refused", SYNCED_AT)

    new_contact.refresh_from_db()
    assert new_contact.node_sync_state == Contact.NodeSyncState.PENDING_ADD


def test_a_refused_contact_records_why() -> None:
    contact = create_contact(1, node_sync_state=Contact.NodeSyncState.PENDING_ADD)

    assert mark_contact_add_failed(contact.pk, contact.public_key, "The node's contact table is full.", SYNCED_AT)

    contact.refresh_from_db()
    assert contact.node_sync_state == Contact.NodeSyncState.ADD_FAILED
    assert contact.node_sync_error == "The node's contact table is full."


def test_a_contact_the_node_deleted_by_itself_waits_to_be_added_again() -> None:
    contact = create_contact(1)

    assert return_contact_to_pending_add(contact.public_key, "deleted by the node") == contact.pk
    assert return_contact_to_pending_add(build_public_key(99), "deleted by the node") is None

    contact.refresh_from_db()
    assert contact.node_sync_state == Contact.NodeSyncState.PENDING_ADD
    assert contact.node_sync_error == "deleted by the node"


def test_pending_route_resets_are_counted_only_for_contacts_that_still_exist() -> None:
    contact = create_contact(1)
    for sender_timestamp, packet_contact in ((1_790_000_000, contact), (1_790_000_001, contact), (1_790_000_002, None)):
        packet = create_outbound_packet(
            packet_contact, "HT1 q bob 1", sender_timestamp, state=OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT
        )
        OutboundPacket.objects.filter(id=packet.pk).update(route_reset_state=OutboundPacket.RouteResetState.PENDING)
    create_outbound_packet(contact, "HT1 q bob 1", 1_790_000_003)

    assert count_pending_route_resets() == 2
