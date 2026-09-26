"""Pairing mode: periodic adverts and the adverts the node hears meanwhile."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F

from node.models import HeardAdvert, NodeCommand, PairingSession

# A heard advert can still be added this long after its session ended.
ADDING_ALLOWED_AFTER_SESSION_END_MINUTES = 15
HEARD_ADVERT_RETENTION_DAYS = 7

MICRODEGREES_PER_DEGREE = 1_000_000


def get_active_pairing_session() -> PairingSession | None:
    return PairingSession.objects.filter(state=PairingSession.State.ACTIVE).first()


def get_latest_pairing_session() -> PairingSession | None:
    return PairingSession.objects.order_by("-started_at", "-id").first()


def is_adding_allowed(pairing_session: PairingSession, now: datetime) -> bool:
    """While the session is active, and up to ADDING_ALLOWED_AFTER_SESSION_END_MINUTES after it ended."""
    if pairing_session.state == PairingSession.State.ACTIVE:
        return True
    session_end = pairing_session.finished_at or pairing_session.ends_at
    return now <= session_end + timedelta(minutes=ADDING_ALLOWED_AFTER_SESSION_END_MINUTES)


def start_pairing_session(
    duration_seconds: int,
    advert_interval_seconds: int,
    advert_flood: bool,
    start_command: NodeCommand | None,
    now: datetime,
) -> PairingSession:
    """Create the active session, ending at now + duration (the worker, when it sends the first advert).

    Raises ValueError when a session is already active (pairing_single_active_session).
    """
    try:
        with transaction.atomic():
            return PairingSession.objects.create(
                state=PairingSession.State.ACTIVE,
                started_at=now,
                ends_at=now + timedelta(seconds=duration_seconds),
                advert_interval_seconds=advert_interval_seconds,
                advert_flood=advert_flood,
                start_command=start_command,
            )
    except IntegrityError as integrity_error:
        raise ValueError("A pairing session is already active.") from integrity_error


def record_pairing_advert_sent(pairing_session_id: int, now: datetime) -> None:
    PairingSession.objects.filter(id=pairing_session_id).update(adverts_sent=F("adverts_sent") + 1, last_advert_at=now)


def stop_pairing_session(pairing_session_id: int, now: datetime) -> bool:
    """Compare-and-set active to stopped (the operator, or leaving relay mode running); False if it was not active."""
    updated_row_count = PairingSession.objects.filter(id=pairing_session_id, state=PairingSession.State.ACTIVE).update(
        state=PairingSession.State.STOPPED, finished_at=now
    )
    return updated_row_count == 1


def end_expired_pairing_sessions(now: datetime) -> list[PairingSession]:
    """Set every active session whose ends_at has passed to ended."""
    with transaction.atomic():
        expired_sessions = list(
            PairingSession.objects.select_for_update().filter(state=PairingSession.State.ACTIVE, ends_at__lte=now)
        )
        for expired_session in expired_sessions:
            expired_session.state = PairingSession.State.ENDED
            expired_session.finished_at = now
            expired_session.save(update_fields=["state", "finished_at"])
    return expired_sessions


def record_heard_advert(pairing_session_id: int, contact_record: Mapping[str, Any], now: datetime) -> HeardAdvert:
    """Insert or update the session's row for the record's public key (a repeat raises heard_count).

    contact_record is the library's NEW_CONTACT payload: public_key, type, adv_name, last_advert,
    adv_lat and adv_lon (degrees) among others; it is stored whole for add_contact.
    """
    public_key = str(contact_record["public_key"]).lower()
    advert_fields = {
        "name": str(contact_record.get("adv_name", "")),
        "node_type": int(contact_record["type"]),
        "advert_timestamp": int(contact_record.get("last_advert", 0)),
        "latitude_microdegrees": convert_degrees_to_microdegrees(contact_record.get("adv_lat", 0)),
        "longitude_microdegrees": convert_degrees_to_microdegrees(contact_record.get("adv_lon", 0)),
        "contact_record": dict(contact_record),
    }
    with transaction.atomic():
        heard_advert = (
            HeardAdvert.objects.select_for_update()
            .filter(pairing_session_id=pairing_session_id, public_key=public_key)
            .first()
        )
        if heard_advert is None:
            return HeardAdvert.objects.create(
                pairing_session_id=pairing_session_id,
                public_key=public_key,
                first_heard_at=now,
                last_heard_at=now,
                heard_count=1,
                **advert_fields,
            )

        HeardAdvert.objects.filter(id=heard_advert.pk).update(
            last_heard_at=now, heard_count=F("heard_count") + 1, **advert_fields
        )
        heard_advert.refresh_from_db()
        return heard_advert


def convert_degrees_to_microdegrees(degrees: object) -> int:
    return round(float(str(degrees)) * MICRODEGREES_PER_DEGREE)


def delete_old_heard_adverts(now: datetime) -> int:
    """Delete the adverts of sessions that ended more than HEARD_ADVERT_RETENTION_DAYS ago; returns how many."""
    deleted_count, _deleted_by_model = HeardAdvert.objects.filter(
        pairing_session__finished_at__lt=now - timedelta(days=HEARD_ADVERT_RETENTION_DAYS)
    ).delete()
    return deleted_count
