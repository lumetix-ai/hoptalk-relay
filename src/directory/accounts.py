"""Sign-in, user lookup and relinking: the services behind the A and Q requests.

The request processor runs them through sync_to_async, one transaction each, and queues the
reply they return only after that transaction committed.

Sign-in has no sequence numbers: a retry after a lost "a" checks the password again, finds the
device already linked and changes nothing. Wrong passwords are throttled per device, in a
15-minute window that starts at the first failure. The throttle comes before the password
check even for a device that is already linked, and account creation does not clear it: either
exception would let a guesser reset the count or use a linked device as an unlimited oracle.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, transaction
from django.db.models import F
from django.db.models.functions import Coalesce

from directory.contacts import LockedRowChangedError
from directory.models import Contact, User
from messaging.deliveries import cancel_device_deliveries_for_relink
from messaging.receipts import cancel_device_receipts_for_relink
from messaging.refresh_sessions import cancel_device_refresh_sessions_for_relink
from messaging.service_transactions import run_in_service_transaction
from protocol.constants import SIGN_IN_FAILURE_WINDOW_MINUTES, SIGN_IN_FAILURES_BEFORE_RATE_LIMIT, ErrorCode
from protocol.error_replies import build_request_error_reply
from protocol.message_types import AccountReply, AccountRequest, ErrorReply, QueryReply, QueryRequest
from protocol.passwords import is_valid_password, normalize_password
from protocol.usernames import normalize_username_for_lookup

logger = logging.getLogger(__name__)

SIGN_IN_FAILURE_WINDOW = timedelta(minutes=SIGN_IN_FAILURE_WINDOW_MINUTES)


@dataclass(frozen=True, kw_only=True)
class AccountRequestOutcome:
    reply: AccountReply | ErrorReply
    # For inbound_direct_messages.outcome_summary, such as "device relinked from ivan".
    outcome_summary: str


@dataclass(frozen=True, kw_only=True)
class QueryRequestOutcome:
    reply: QueryReply | ErrorReply
    outcome_summary: str


def register_or_sign_in(device: Contact, request: AccountRequest, now: datetime) -> AccountRequestOutcome:
    """Validate the password, then create the user or verify the password, and link, relink or change nothing.

    Answers "a <canonical>", or "e PASSWORD_INVALID / WRONG_PASSWORD / RATE_LIMITED A <username>".
    """
    normalized_password = normalize_password(request.password)
    if not is_valid_password(normalized_password):
        return AccountRequestOutcome(
            reply=build_request_error_reply(ErrorCode.PASSWORD_INVALID, request),
            outcome_summary="the password breaks the password rules",
        )
    return run_in_service_transaction(
        lambda: register_or_sign_in_in_transaction(device.pk, request, normalized_password, now)
    )


def register_or_sign_in_in_transaction(
    device_id: int,
    request: AccountRequest,
    normalized_password: str,
    now: datetime,
) -> AccountRequestOutcome:
    existing_user_id = (
        User.objects.filter(username_lookup=normalize_username_for_lookup(request.username))
        .values_list("id", flat=True)
        .first()
    )
    device_values = Contact.objects.filter(id=device_id).values("user_id").first()
    if device_values is None:
        raise LockedRowChangedError(f"Device {device_id} was deleted before it could sign in.")
    device_user_id = device_values["user_id"]
    locked_users_by_id = lock_users(existing_user_id, device_user_id)
    device = Contact.objects.select_for_update().filter(id=device_id).first()
    if device is None:
        raise LockedRowChangedError(f"Device {device_id} was deleted while it was signing in.")
    if device.user_id != device_user_id:
        raise LockedRowChangedError(f"Device {device_id} was relinked while it was signing in.")

    if existing_user_id is None:
        return create_account(device, request, normalized_password, now)
    existing_user = locked_users_by_id.get(existing_user_id)
    if existing_user is None:
        raise LockedRowChangedError(f"User {existing_user_id} was deleted while device {device_id} was signing in.")
    return sign_in(device, existing_user, request, normalized_password, now)


def lock_users(*user_ids: int | None) -> dict[int, User]:
    """Lock the given users in ascending id, the order every transaction that locks several users follows."""
    wanted_user_ids = {user_id for user_id in user_ids if user_id is not None}
    return {user.pk: user for user in User.objects.select_for_update().filter(id__in=wanted_user_ids).order_by("id")}


def create_account(
    device: Contact,
    request: AccountRequest,
    normalized_password: str,
    now: datetime,
) -> AccountRequestOutcome:
    """A new username: the account is created with this password and the device is linked to it."""
    try:
        with transaction.atomic():
            user = User.objects.create(
                username=request.username,
                password_hash=make_password(normalized_password),
                created_at=now,
            )
    except IntegrityError as integrity_error:
        # Another device registered the same name at the same moment. Starting over locks that
        # user before the device, as the lock order wants, and verifies the password against it.
        raise LockedRowChangedError(f"The username {request.username} was registered meanwhile.") from integrity_error

    link_summary = link_device(device, user, now)
    return AccountRequestOutcome(
        reply=AccountReply(username=user.username),
        outcome_summary=f"account created; {link_summary}",
    )


def sign_in(
    device: Contact,
    user: User,
    request: AccountRequest,
    normalized_password: str,
    now: datetime,
) -> AccountRequestOutcome:
    restart_expired_failure_window(device, now)
    if device.failed_sign_in_count >= SIGN_IN_FAILURES_BEFORE_RATE_LIMIT:
        return AccountRequestOutcome(
            reply=build_request_error_reply(ErrorCode.RATE_LIMITED, request),
            outcome_summary="rate limited: too many wrong passwords from this device",
        )

    if not check_password(normalized_password, user.password_hash):
        record_failed_sign_in(device, now)
        return AccountRequestOutcome(
            reply=build_request_error_reply(ErrorCode.WRONG_PASSWORD, request),
            outcome_summary=f"wrong password ({device.failed_sign_in_count} in the current window)",
        )

    clear_failed_sign_ins(device)
    link_summary = link_device(device, user, now)
    return AccountRequestOutcome(
        reply=AccountReply(username=user.username), outcome_summary=f"signed in; {link_summary}"
    )


def restart_expired_failure_window(device: Contact, now: datetime) -> None:
    window_started_at = device.failed_sign_in_window_started_at
    if window_started_at is not None and window_started_at + SIGN_IN_FAILURE_WINDOW <= now:
        clear_failed_sign_ins(device)


def record_failed_sign_in(device: Contact, now: datetime) -> None:
    """The first failure starts the window."""
    Contact.objects.filter(id=device.pk).update(
        failed_sign_in_count=F("failed_sign_in_count") + 1,
        failed_sign_in_window_started_at=Coalesce(F("failed_sign_in_window_started_at"), now),
    )
    device.refresh_from_db(fields=["failed_sign_in_count", "failed_sign_in_window_started_at"])


def clear_failed_sign_ins(device: Contact) -> None:
    if device.failed_sign_in_count == 0 and device.failed_sign_in_window_started_at is None:
        return
    Contact.objects.filter(id=device.pk).update(failed_sign_in_count=0, failed_sign_in_window_started_at=None)
    device.failed_sign_in_count = 0
    device.failed_sign_in_window_started_at = None


def link_device(device: Contact, user: User, now: datetime) -> str:
    """Link, leave alone or relink the device; returns what happened, for the traffic log."""
    if device.user_id == user.pk:
        return "the device was already linked"
    if device.user_id is None:
        Contact.objects.filter(id=device.pk).update(user_id=user.pk, linked_at=now)
        device.user_id = user.pk
        device.linked_at = now
        return "device linked"

    previous_username = User.objects.values_list("username", flat=True).get(id=device.user_id)
    relink_device(device, user, now)
    return f"device relinked from {previous_username}"


def user_exists(device: Contact, request: QueryRequest) -> QueryRequestOutcome:
    """Answer "q <canonical> 1", "q <as sent> 0", or "e NOT_SIGNED_IN Q <username>" for an unlinked device."""
    device_user_id = Contact.objects.filter(id=device.pk).values_list("user_id", flat=True).first()
    if device_user_id is None:
        return QueryRequestOutcome(
            reply=build_request_error_reply(ErrorCode.NOT_SIGNED_IN, request),
            outcome_summary="the device is not signed in",
        )

    user = User.objects.filter(username_lookup=normalize_username_for_lookup(request.username)).first()
    if user is None:
        return QueryRequestOutcome(
            reply=QueryReply(username=request.username, user_exists=False),
            outcome_summary="no such user",
        )
    return QueryRequestOutcome(
        reply=QueryReply(username=user.username, user_exists=True), outcome_summary="user exists"
    )


def relink_device(device: Contact, new_user: User, now: datetime) -> None:
    """Move the device to another user inside the caller's transaction.

    Cancels the device's non-terminal deliveries and receipts and its active refresh
    sessions, sets user and linked_at, and logs the relink. The caller has locked both users
    and then the device. The previous user's incomplete uploads from this device stay theirs
    and expire.
    """
    previous_user_id = device.user_id
    cancelled_delivery_count = cancel_device_deliveries_for_relink(device.pk, now)
    cancelled_receipt_count = cancel_device_receipts_for_relink(device.pk, now)
    cancelled_session_count = cancel_device_refresh_sessions_for_relink(device.pk, now)
    Contact.objects.filter(id=device.pk).update(user_id=new_user.pk, linked_at=now)
    device.user_id = new_user.pk
    device.linked_at = now
    logger.info(
        "Device %s relinked from user %s to user %s; cancelled %s deliveries, %s receipts and %s refresh sessions.",
        device,
        previous_user_id,
        new_user.pk,
        cancelled_delivery_count,
        cancelled_receipt_count,
        cancelled_session_count,
    )
