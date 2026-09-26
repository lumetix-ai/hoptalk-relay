from datetime import UTC, datetime, timedelta

import pytest

from node.models import HeardAdvert, PairingSession
from node.pairing_sessions import (
    delete_old_heard_adverts,
    end_expired_pairing_sessions,
    get_active_pairing_session,
    is_adding_allowed,
    record_heard_advert,
    record_pairing_advert_sent,
    start_pairing_session,
    stop_pairing_session,
)

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def build_contact_record(
    public_key: str = "AB" * 32, name: str = "Bob's tracker", node_type: int = 1
) -> dict[str, object]:
    return {
        "public_key": public_key,
        "type": node_type,
        "flags": 0,
        "out_path_len": -1,
        "out_path_hash_mode": -1,
        "out_path": "",
        "adv_name": name,
        "last_advert": 1_790_000_000,
        "adv_lat": -37.8136,
        "adv_lon": 144.9631,
        "lastmod": 1_790_000_005,
    }


def start_session(now: datetime = NOW) -> PairingSession:
    return start_pairing_session(
        duration_seconds=120, advert_interval_seconds=30, advert_flood=False, start_command=None, now=now
    )


def test_a_session_ends_after_its_duration_from_the_first_advert() -> None:
    pairing_session = start_session()

    assert pairing_session.state == PairingSession.State.ACTIVE
    assert pairing_session.ends_at == NOW + timedelta(seconds=120)
    assert get_active_pairing_session() == pairing_session


def test_only_one_session_can_be_active() -> None:
    start_session()

    with pytest.raises(ValueError, match="already active"):
        start_session()


def test_every_advert_sent_is_counted() -> None:
    pairing_session = start_session()

    record_pairing_advert_sent(pairing_session.pk, NOW)
    record_pairing_advert_sent(pairing_session.pk, NOW + timedelta(seconds=30))

    pairing_session.refresh_from_db()
    assert pairing_session.adverts_sent == 2
    assert pairing_session.last_advert_at == NOW + timedelta(seconds=30)


def test_a_heard_advert_is_stored_once_per_key_and_its_repeats_are_counted() -> None:
    pairing_session = start_session()
    later = NOW + timedelta(seconds=40)

    record_heard_advert(pairing_session.pk, build_contact_record(), NOW)
    heard_advert = record_heard_advert(pairing_session.pk, build_contact_record(name="Bob's new name"), later)

    assert HeardAdvert.objects.count() == 1
    assert heard_advert.public_key == "ab" * 32
    assert heard_advert.name == "Bob's new name"
    assert heard_advert.heard_count == 2
    assert heard_advert.first_heard_at == NOW
    assert heard_advert.last_heard_at == later
    assert heard_advert.latitude_microdegrees == -37_813_600
    assert heard_advert.longitude_microdegrees == 144_963_100
    assert heard_advert.contact_record["adv_name"] == "Bob's new name"


def test_the_operator_stops_a_session_once() -> None:
    pairing_session = start_session()

    assert stop_pairing_session(pairing_session.pk, NOW) is True
    assert stop_pairing_session(pairing_session.pk, NOW) is False
    pairing_session.refresh_from_db()
    assert pairing_session.state == PairingSession.State.STOPPED
    assert pairing_session.finished_at == NOW


def test_a_session_whose_time_is_up_ends() -> None:
    pairing_session = start_session()

    assert end_expired_pairing_sessions(NOW + timedelta(seconds=119)) == []
    assert end_expired_pairing_sessions(NOW + timedelta(seconds=120)) == [pairing_session]
    pairing_session.refresh_from_db()
    assert pairing_session.state == PairingSession.State.ENDED


def test_heard_adverts_can_be_added_until_fifteen_minutes_after_the_session_ended() -> None:
    pairing_session = start_session()
    assert is_adding_allowed(pairing_session, NOW)

    stop_pairing_session(pairing_session.pk, NOW + timedelta(seconds=60))
    pairing_session.refresh_from_db()

    assert is_adding_allowed(pairing_session, NOW + timedelta(minutes=16))
    assert not is_adding_allowed(pairing_session, NOW + timedelta(minutes=16, seconds=1))


def test_heard_adverts_are_deleted_seven_days_after_their_session_ended() -> None:
    old_session = start_session(NOW - timedelta(days=9))
    stop_pairing_session(old_session.pk, NOW - timedelta(days=8))
    recent_session = start_session(NOW - timedelta(days=2))
    stop_pairing_session(recent_session.pk, NOW - timedelta(days=2))
    record_heard_advert(old_session.pk, build_contact_record(public_key="01" * 32), NOW - timedelta(days=9))
    record_heard_advert(recent_session.pk, build_contact_record(public_key="02" * 32), NOW - timedelta(days=2))

    assert delete_old_heard_adverts(NOW) == 1
    assert list(HeardAdvert.objects.values_list("public_key", flat=True)) == ["02" * 32]
