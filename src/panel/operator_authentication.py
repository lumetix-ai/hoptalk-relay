"""The single operator of the admin panel, configured in src/.env.

There is no django.contrib.auth: the credentials are ADMIN_USERNAME and an Argon2
ADMIN_PASSWORD_HASH, a signed-in session carries the time of the sign-in and a fingerprint of
those credentials, and failed attempts are throttled per client address.
"""

import hashlib
import hmac
import ipaddress
import math
from datetime import datetime, timedelta

from django.contrib.auth.hashers import check_password
from django.contrib.sessions.backends.base import SessionBase
from django.contrib.sessions.models import Session
from django.http import HttpRequest

from hoptalk_relay.relay_settings import OperatorCredentials, get_relay_settings
from panel.models import OperatorLoginAttempt

MAXIMUM_RECENT_FAILED_ATTEMPTS = 5
FAILED_ATTEMPT_WINDOW = timedelta(minutes=15)
ABSOLUTE_SESSION_LIFETIME = timedelta(hours=12)
LOGIN_ATTEMPT_RETENTION = timedelta(days=1)

SESSION_AUTHENTICATED_AT_KEY = "operator_authenticated_at"
SESSION_CREDENTIALS_FINGERPRINT_KEY = "operator_credentials_fingerprint"

# Checked instead of the real hash when the username is wrong, so that a wrong username costs
# the same Argon2 work as a wrong password and the response time does not reveal which one it
# was. The password behind it is unknown and irrelevant.
DUMMY_PASSWORD_HASH = (
    "argon2$argon2id$v=19$m=102400,t=2,p=8$eTA5UHVBZTRkRmVoSVlUV0c0Q1ZIWQ$SvbmdQQyV5ibpyM6KINR2nJkNRoMUtLGq5gGkPEYLRQ"
)


def verify_operator_credentials(username: str, password: str) -> bool:
    operator_credentials = get_relay_settings().operator_credentials
    username_matches = hmac.compare_digest(username.encode(), operator_credentials.username.encode())
    password_hash = operator_credentials.password_hash if username_matches else DUMMY_PASSWORD_HASH
    password_matches = check_password(password, password_hash)
    return username_matches and password_matches


def calculate_credentials_fingerprint(operator_credentials: OperatorCredentials) -> str:
    """Changes whenever the username or the password hash changes, which signs every browser out."""
    credentials_text = f"{operator_credentials.username}\n{operator_credentials.password_hash}"
    return hashlib.sha256(credentials_text.encode()).hexdigest()[:16]


def sign_operator_in(session: SessionBase, now: datetime) -> None:
    # A new session key, so a key planted before the sign-in is worthless afterwards.
    session.cycle_key()
    session[SESSION_AUTHENTICATED_AT_KEY] = now.isoformat()
    session[SESSION_CREDENTIALS_FINGERPRINT_KEY] = calculate_credentials_fingerprint(
        get_relay_settings().operator_credentials
    )


def sign_operator_out(session: SessionBase) -> None:
    session.flush()


def has_valid_operator_session(session: SessionBase, now: datetime) -> bool:
    authenticated_at_text = session.get(SESSION_AUTHENTICATED_AT_KEY)
    credentials_fingerprint = session.get(SESSION_CREDENTIALS_FINGERPRINT_KEY)
    if not isinstance(authenticated_at_text, str) or not isinstance(credentials_fingerprint, str):
        return False

    current_fingerprint = calculate_credentials_fingerprint(get_relay_settings().operator_credentials)
    if not hmac.compare_digest(credentials_fingerprint, current_fingerprint):
        return False

    try:
        authenticated_at = datetime.fromisoformat(authenticated_at_text)
    except ValueError:
        return False

    return now - authenticated_at < ABSOLUTE_SESSION_LIFETIME


def find_client_address(request: HttpRequest) -> str | None:
    """The address nginx put in X-Forwarded-For; gunicorn's Unix socket leaves REMOTE_ADDR empty."""
    forwarded_address = request.headers.get("X-Forwarded-For", "").strip()
    try:
        return str(ipaddress.ip_address(forwarded_address))
    except ValueError:
        return None


def calculate_throttle_minutes_remaining(client_address: str | None, now: datetime) -> int:
    """0 when the address may try again; otherwise the whole minutes until enough failures age out."""
    recent_failure_times = list(
        OperatorLoginAttempt.objects.filter(
            client_address=client_address,
            succeeded=False,
            attempted_at__gt=now - FAILED_ATTEMPT_WINDOW,
        )
        .order_by("attempted_at")
        .values_list("attempted_at", flat=True)
    )
    failures_over_the_limit = len(recent_failure_times) - MAXIMUM_RECENT_FAILED_ATTEMPTS
    if failures_over_the_limit <= 0:
        return 0

    throttle_ends_at = recent_failure_times[failures_over_the_limit - 1] + FAILED_ATTEMPT_WINDOW
    return max(1, math.ceil((throttle_ends_at - now).total_seconds() / 60))


def record_login_attempt(client_address: str | None, succeeded: bool, now: datetime) -> None:
    OperatorLoginAttempt.objects.create(client_address=client_address, succeeded=succeeded, attempted_at=now)


def delete_old_login_attempts(now: datetime) -> int:
    deleted_count, _deleted_by_model = OperatorLoginAttempt.objects.filter(
        attempted_at__lt=now - LOGIN_ATTEMPT_RETENTION
    ).delete()
    return deleted_count


def delete_expired_operator_sessions(now: datetime) -> int:
    """Django never deletes an expired session row by itself; without this the table only grows."""
    deleted_count, _deleted_by_model = Session.objects.filter(expire_date__lt=now).delete()
    return deleted_count
