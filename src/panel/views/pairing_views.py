"""Pairing mode on the Contacts page: start, watch the heard adverts, add one, stop."""

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_htmx.http import HTMX_STOP_POLLING, trigger_client_event

from directory.contacts import ContactAdditionRefusedError, add_contact_from_heard_advert
from hoptalk_relay.relay_settings import (
    MAXIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
    MAXIMUM_PAIRING_DURATION_SECONDS,
    MINIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
    MINIMUM_PAIRING_DURATION_SECONDS,
)
from node.models import HeardAdvert, NodeCommand, PairingSession
from node.node_commands import create_node_command
from node.pairing_sessions import get_active_pairing_session, stop_pairing_session
from panel.forms import PairingStartForm
from panel.htmx_request import HtmxHttpRequest
from panel.views.contact_views import CONTACTS_CHANGED_EVENT, CONTACTS_TEMPLATE, read_pairing_area

PAIRING_SECTION_URL_FRAGMENT = "#pairing"
INVALID_PAIRING_START_MESSAGE = (
    f"Pairing needs a duration of {MINIMUM_PAIRING_DURATION_SECONDS} to {MAXIMUM_PAIRING_DURATION_SECONDS} seconds"
    f" and an advert every {MINIMUM_PAIRING_ADVERT_INTERVAL_SECONDS} to {MAXIMUM_PAIRING_ADVERT_INTERVAL_SECONDS}"
    " seconds."
)


def redirect_to_pairing_section() -> HttpResponse:
    response = redirect("panel:contacts")
    response["Location"] += PAIRING_SECTION_URL_FRAGMENT
    return response


def render_pairing_panel(
    request: HttpRequest, pairing_session: PairingSession, pairing_notice: str = "", notice_tone: str = "success"
) -> HttpResponse:
    """The panel, and an out-of-band notice outside it, which the panel's next poll then leaves alone."""
    pairing_area = read_pairing_area(timezone.now(), pairing_session=pairing_session)
    polling_should_stop = request.method == "GET" and not pairing_area.is_active
    response_content = render_to_string(
        f"{CONTACTS_TEMPLATE}#pairing_panel", {"pairing_area": pairing_area}, request=request
    )
    if pairing_notice:
        response_content += render_to_string(
            f"{CONTACTS_TEMPLATE}#pairing_notice",
            {"pairing_notice": pairing_notice, "notice_tone": notice_tone, "notice_is_out_of_band": True},
            request=request,
        )
    return HttpResponse(response_content, status=HTMX_STOP_POLLING if polling_should_stop else 200)


@require_GET
def show_pairing_panel(request: HttpRequest, pairing_session_id: int) -> HttpResponse:
    return render_pairing_panel(request, get_object_or_404(PairingSession, id=pairing_session_id))


@require_POST
def start_pairing(request: HttpRequest) -> HttpResponse:
    pairing_start_form = PairingStartForm(request.POST)
    if not pairing_start_form.is_valid():
        messages.error(request, INVALID_PAIRING_START_MESSAGE)
        return redirect_to_pairing_section()
    if get_active_pairing_session() is not None:
        messages.error(request, "A pairing session is already running.")
        return redirect_to_pairing_section()
    if NodeCommand.objects.filter(
        kind=NodeCommand.Kind.START_PAIRING, state__in=[NodeCommand.State.PENDING, NodeCommand.State.RUNNING]
    ).exists():
        messages.error(request, "Pairing is already starting.")
        return redirect_to_pairing_section()

    create_node_command(
        NodeCommand.Kind.START_PAIRING,
        {
            "duration_seconds": pairing_start_form.cleaned_data["duration_seconds"],
            "advert_interval_seconds": pairing_start_form.cleaned_data["advert_interval_seconds"],
            "advert_flood": pairing_start_form.cleaned_data["advert_flood"],
        },
        timezone.now(),
    )
    messages.success(request, "Pairing starts as soon as the relay worker sends the first advert.")
    return redirect_to_pairing_section()


@require_POST
def stop_pairing(request: HtmxHttpRequest, pairing_session_id: int) -> HttpResponse:
    pairing_session = get_object_or_404(PairingSession, id=pairing_session_id)
    now = timezone.now()
    # The session stops at once, whether or not the worker is running; the command only
    # wakes the worker's advertiser, which checks the session before every advert anyway.
    if stop_pairing_session(pairing_session.pk, now):
        create_node_command(NodeCommand.Kind.STOP_PAIRING, {"pairing_session_id": pairing_session.pk}, now)
    pairing_session.refresh_from_db()
    if request.htmx:
        return render_pairing_panel(request, pairing_session, pairing_notice="Pairing was stopped.")
    messages.success(request, "Pairing was stopped.")
    return redirect_to_pairing_section()


@require_POST
def add_heard_advert(request: HtmxHttpRequest, pairing_session_id: int, heard_advert_id: int) -> HttpResponse:
    heard_advert = get_object_or_404(
        HeardAdvert.objects.select_related("pairing_session"),
        id=heard_advert_id,
        pairing_session_id=pairing_session_id,
    )
    try:
        contact = add_contact_from_heard_advert(heard_advert, timezone.now())
    except ContactAdditionRefusedError as refusal:
        pairing_notice, notice_tone = f"{heard_advert.name or 'The node'} was not added. {refusal}", "danger"
    else:
        pairing_notice, notice_tone = f"{contact} was added; the relay worker now adds it to the node.", "success"

    if not request.htmx:
        if notice_tone == "danger":
            messages.error(request, pairing_notice)
        else:
            messages.success(request, pairing_notice)
        return redirect_to_pairing_section()

    response = render_pairing_panel(request, heard_advert.pairing_session, pairing_notice, notice_tone)
    trigger_client_event(response, CONTACTS_CHANGED_EVENT)
    return response
