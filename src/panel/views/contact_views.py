"""The Contacts page: the relay's card, the contacts on the node, adding by card, pairing and deleting."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from directory.contacts import (
    CONTACT_CAPACITY,
    ContactAdditionCheck,
    ContactAdditionRefusedError,
    add_contact_from_card,
    check_contact_addition,
    summarize_contact_deletion,
)
from directory.contacts import delete_contact as delete_contact_with_its_rows
from directory.models import Contact
from node.contact_cards import (
    ContactCard,
    InvalidContactCardError,
    MeshCoreNodeType,
    describe_node_type,
    parse_contact_card_uri,
)
from node.models import HeardAdvert, NodeCommand, PairingSession
from node.node_settings import IncompleteNodeConfigurationError, NodeConfiguration, load_node_configuration
from node.pairing_sessions import (
    ADDING_ALLOWED_AFTER_SESSION_END_MINUTES,
    get_active_pairing_session,
    get_latest_pairing_session,
    is_adding_allowed,
)
from panel.forms import ContactCardForm, PairingStartForm
from panel.htmx_request import HtmxHttpRequest
from panel.node_action_availability import read_node_action_availability
from panel.presenters import DisplayedProgressStep, build_displayed_progress_steps
from panel.views.command_views import expire_node_command_if_overdue
from panel.views.redirects import redirect_to_next_page

CONTACTS_TEMPLATE = "panel/contacts.html"
# A start_pairing command that failed this recently is still explained on the page.
PAIRING_FAILURE_SHOWN_FOR = timedelta(minutes=10)
CONTACTS_CHANGED_EVENT = "contacts-changed"


@dataclass(frozen=True, kw_only=True)
class DisplayedHeardAdvert:
    heard_advert: HeardAdvert
    node_type_description: str
    was_added_from_this_advert: bool
    reason_it_cannot_be_added: str

    @property
    def can_be_added(self) -> bool:
        return not self.reason_it_cannot_be_added


@dataclass(frozen=True, kw_only=True)
class PairingArea:
    pairing_session: PairingSession | None
    heard_adverts: list[DisplayedHeardAdvert]
    adding_is_allowed: bool
    starting_command: NodeCommand | None
    starting_command_steps: list[DisplayedProgressStep]
    failed_start_command: NodeCommand | None
    server_now: datetime

    @property
    def is_active(self) -> bool:
        return self.pairing_session is not None and self.pairing_session.state == PairingSession.State.ACTIVE

    @property
    def remaining_seconds(self) -> int:
        if self.pairing_session is None:
            return 0
        return max(0, int((self.pairing_session.ends_at - self.server_now).total_seconds()))

    @property
    def duration_seconds(self) -> int:
        if self.pairing_session is None:
            return 0
        return int((self.pairing_session.ends_at - self.pairing_session.started_at).total_seconds())

    @property
    def adding_allowed_until(self) -> datetime | None:
        if self.pairing_session is None or self.is_active:
            return None
        session_end = self.pairing_session.finished_at or self.pairing_session.ends_at
        return session_end + timedelta(minutes=ADDING_ALLOWED_AFTER_SESSION_END_MINUTES)


@dataclass(frozen=True, kw_only=True)
class CardPreview:
    contact_card: ContactCard | None
    error_message: str
    contact_addition_check: ContactAdditionCheck | None

    @property
    def node_type_description(self) -> str:
        return describe_node_type(self.contact_card.node_type) if self.contact_card else ""

    @property
    def advert_time(self) -> datetime | None:
        if self.contact_card is None:
            return None
        return datetime.fromtimestamp(self.contact_card.advert_timestamp, tz=timezone.get_current_timezone())


def read_node_configuration_for_card() -> NodeConfiguration | None:
    try:
        return load_node_configuration()
    except IncompleteNodeConfigurationError:
        return None


def build_contact_list_context() -> dict[str, Any]:
    contacts = list(Contact.objects.select_related("user").order_by("name", "id"))
    return {
        "contacts": contacts,
        "contact_count": len(contacts),
        "contact_capacity": CONTACT_CAPACITY,
        "has_pending_contacts": any(
            contact.node_sync_state == Contact.NodeSyncState.PENDING_ADD for contact in contacts
        ),
    }


def describe_heard_advert(
    heard_advert: HeardAdvert, contact_keys: set[str], adding_is_allowed: bool
) -> DisplayedHeardAdvert:
    was_added_from_this_advert = heard_advert.added_contact_id is not None
    if was_added_from_this_advert or heard_advert.public_key in contact_keys:
        reason = "Already a contact."
    elif heard_advert.node_type != MeshCoreNodeType.CHAT:
        reason = f"A {describe_node_type(heard_advert.node_type)} cannot be a contact."
    elif not adding_is_allowed:
        reason = "The pairing session ended too long ago."
    else:
        reason = find_contact_addition_problem(heard_advert)
    return DisplayedHeardAdvert(
        heard_advert=heard_advert,
        node_type_description=describe_node_type(heard_advert.node_type),
        was_added_from_this_advert=was_added_from_this_advert,
        reason_it_cannot_be_added=reason,
    )


def find_contact_addition_problem(heard_advert: HeardAdvert) -> str:
    """The rest of what adding checks: a key prefix collision, the relay's own key and a full table.

    Asked only for an advert that passed the cheap checks, so the polled panel stays cheap.
    """
    contact_addition_check = check_contact_addition(heard_advert.public_key, heard_advert.node_type)
    if contact_addition_check.is_allowed:
        return ""
    return contact_addition_check.describe_problems()[0]


def read_pairing_area(now: datetime, pairing_session: PairingSession | None = None) -> PairingArea:
    """The active session, or the latest one while its adverts can still be added, and a start in progress."""
    if pairing_session is None:
        pairing_session = get_active_pairing_session() or get_latest_pairing_session()
        if pairing_session is not None and not is_adding_allowed(pairing_session, now):
            pairing_session = None

    adding_is_allowed = pairing_session is not None and is_adding_allowed(pairing_session, now)
    heard_adverts: list[DisplayedHeardAdvert] = []
    if pairing_session is not None:
        contact_keys = set(Contact.objects.values_list("public_key", flat=True))
        heard_adverts = [
            describe_heard_advert(heard_advert, contact_keys, adding_is_allowed)
            for heard_advert in pairing_session.heard_adverts.order_by("-last_heard_at", "-id")
        ]

    latest_start_command = NodeCommand.objects.filter(kind=NodeCommand.Kind.START_PAIRING).order_by("-id").first()
    if latest_start_command is not None:
        expire_node_command_if_overdue(latest_start_command, now)
    starting_command = find_starting_command(latest_start_command)
    return PairingArea(
        pairing_session=pairing_session,
        heard_adverts=heard_adverts,
        adding_is_allowed=adding_is_allowed,
        starting_command=starting_command,
        starting_command_steps=build_displayed_progress_steps(starting_command) if starting_command else [],
        failed_start_command=find_failed_start_command(latest_start_command, pairing_session, now),
        server_now=now,
    )


def find_starting_command(latest_start_command: NodeCommand | None) -> NodeCommand | None:
    if latest_start_command is None or latest_start_command.state not in (
        NodeCommand.State.PENDING,
        NodeCommand.State.RUNNING,
    ):
        return None
    return latest_start_command


def find_failed_start_command(
    latest_start_command: NodeCommand | None, pairing_session: PairingSession | None, now: datetime
) -> NodeCommand | None:
    if latest_start_command is None or latest_start_command.state in (
        NodeCommand.State.PENDING,
        NodeCommand.State.RUNNING,
        NodeCommand.State.SUCCEEDED,
    ):
        return None
    if latest_start_command.finished_at is None or now - latest_start_command.finished_at > PAIRING_FAILURE_SHOWN_FOR:
        return None
    if pairing_session is not None and pairing_session.started_at > latest_start_command.created_at:
        return None
    return latest_start_command


def build_contacts_page_context(now: datetime, **extra_context: Any) -> dict[str, Any]:
    node_configuration = read_node_configuration_for_card()
    context: dict[str, Any] = {
        "node_configuration": node_configuration,
        "node_action_availability": read_node_action_availability(node_is_configured=node_configuration is not None),
        "card_form": ContactCardForm(),
        "pairing_start_form": PairingStartForm.build_with_defaults(),
        "pairing_area": read_pairing_area(now),
        **build_contact_list_context(),
    }
    context.update(extra_context)
    return context


@require_GET
def show_contacts(request: HttpRequest) -> HttpResponse:
    return render(request, CONTACTS_TEMPLATE, build_contacts_page_context(timezone.now()))


@require_GET
def show_contact_list(request: HttpRequest) -> HttpResponse:
    return render(request, f"{CONTACTS_TEMPLATE}#contact_list", build_contact_list_context())


def build_card_preview(card_form: ContactCardForm) -> CardPreview:
    if not card_form.is_valid():
        return CardPreview(contact_card=None, error_message="Paste a contact card first.", contact_addition_check=None)
    try:
        contact_card = parse_contact_card_uri(card_form.cleaned_data["card_uri"])
    except InvalidContactCardError as invalid_card_error:
        return CardPreview(contact_card=None, error_message=str(invalid_card_error), contact_addition_check=None)
    return CardPreview(
        contact_card=contact_card,
        error_message="",
        contact_addition_check=check_contact_addition(contact_card.public_key, contact_card.node_type),
    )


@require_POST
def preview_contact_card(request: HtmxHttpRequest) -> HttpResponse:
    card_form = ContactCardForm(request.POST)
    card_preview = build_card_preview(card_form)
    if request.htmx:
        return render(request, f"{CONTACTS_TEMPLATE}#card_preview", {"card_preview": card_preview})
    return render(
        request,
        CONTACTS_TEMPLATE,
        build_contacts_page_context(timezone.now(), card_form=card_form, card_preview=card_preview),
    )


@require_POST
def add_contact_card(request: HttpRequest) -> HttpResponse:
    card_form = ContactCardForm(request.POST)
    card_preview = build_card_preview(card_form)
    if card_preview.contact_card is None:
        messages.error(request, card_preview.error_message)
        return redirect("panel:contacts")

    try:
        contact = add_contact_from_card(card_preview.contact_card, timezone.now())
    except ContactAdditionRefusedError as refusal:
        messages.error(request, f"The contact was not added. {refusal}")
    else:
        messages.success(request, f"{contact} was added; the relay worker now adds it to the node.")
    return redirect("panel:contacts")


@require_GET
def show_contact_deletion_summary(request: HttpRequest, contact_id: int) -> HttpResponse:
    contact = get_object_or_404(Contact.objects.select_related("user"), id=contact_id)
    return render(
        request,
        "panel/includes/contact_deletion_summary.html",
        {"deletion_summary": summarize_contact_deletion(contact)},
    )


@require_POST
def delete_contact(request: HttpRequest, contact_id: int) -> HttpResponse:
    contact = get_object_or_404(Contact, id=contact_id)
    contact_description = str(contact)
    delete_contact_with_its_rows(contact)
    messages.success(request, f"{contact_description} was deleted; the relay worker removes it from the node.")
    return redirect_to_next_page(request, "panel:contacts")
