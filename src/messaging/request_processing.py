"""Processing one recorded inbox row: classify it, run the service it asks for, and return the replies to queue.

Classification, first match wins: an unknown sender prefix; a MeshCore text type other than
plain text; text that is not protocol traffic; another protocol version; a lower-case (server)
type; an acknowledgement (K, C); a request (A Q M R F); another upper-case letter; no letter
after "HT1 ". Only requests are answered, so the server never answers acknowledgements,
server types, errors or other text, and two relays cannot keep each other busy.

The service and the processed mark of the row commit together; the worker queues the replies
only after that. A row is processed once: the mark is a compare-and-set, and a row that was
processed meanwhile rolls the whole transaction back.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from directory.accounts import register_or_sign_in, user_exists
from directory.models import Contact
from hoptalk_relay.logging_configuration import redact_account_request_passwords
from messaging.inbound_log import DIRECT_ARRIVAL_PATH_LENGTH, carries_account_request_password, redact_stored_text
from messaging.incoming_messages import accept_message_part
from messaging.models import InboundDirectMessage
from messaging.reads_and_acknowledgements import (
    record_delivery_acknowledgement,
    record_read,
    record_receipt_acknowledgement,
)
from messaging.refresh_sessions import start_refresh
from messaging.reply_keys import build_reply_key_for_direct_message
from messaging.route_reset_evidence import needs_flood_arrival_route_reset
from messaging.service_transactions import run_in_service_transaction
from protocol.constants import ACKNOWLEDGEMENT_TYPES, PROTOCOL_PREFIX, UNKNOWN_REQUEST_TYPE
from protocol.error_replies import build_grammar_error_reply
from protocol.formatting import format_server_message
from protocol.message_types import (
    AccountReply,
    AccountRequest,
    DeliveryAcknowledgement,
    DeliveryPart,
    ErrorReply,
    MessagePartRequest,
    OtherText,
    ParsedDirectMessage,
    ProtocolSyntaxError,
    QueryReply,
    QueryRequest,
    ReadReply,
    ReadRequest,
    ReceiptAcknowledgement,
    ReceiptPush,
    RefreshReply,
    RefreshRequest,
    SendStatusReply,
    ServerMessage,
    UnknownMessageType,
    UnsupportedVersion,
)
from protocol.parsing import parse_direct_message_text

logger = logging.getLogger(__name__)

PLAIN_TEXT_TYPE = 0
OUTCOME_SUMMARY_MAXIMUM_LENGTH = 200
REPLY_SUMMARY_MAXIMUM_LENGTH = 160
Classification = InboundDirectMessage.Classification


class ReplyReadiness(StrEnum):
    IMMEDIATE = "immediate"
    # A send status with zeros: sent once 5 s pass without a new part of that message, however
    # long the burst lasts; a newer reply for the same key replaces it.
    COALESCED_INCOMPLETE_STATUS = "coalesced_incomplete_status"


@dataclass(frozen=True, kw_only=True)
class QueuedReply:
    """A reply to queue for the sender loop, once the transaction that produced it committed."""

    contact_id: int
    # Lower-case, such as "M:bob:1790294400123456"; a newer reply for the same key replaces this one.
    reply_key: str
    text: str
    server_message: ServerMessage
    readiness: ReplyReadiness


@dataclass(frozen=True, kw_only=True)
class InboundProcessingResult:
    inbox_row_id: int
    contact_id: int | None
    processing_state: InboundDirectMessage.ProcessingState
    classification: InboundDirectMessage.Classification
    replies: tuple[QueuedReply, ...] = ()
    # A new DM that arrived by flood: in relay mode running, reset the route to the contact
    # before queueing the replies, then call record_flood_arrival_route_reset.
    flood_arrival_route_reset_needed: bool = False
    # A DM from a prefix the database does not know: the node holds a contact the database does not.
    reconciliation_needed: bool = False
    # Another call processed the row first; nothing was done.
    was_already_processed: bool = False


@dataclass(frozen=True, kw_only=True)
class RowProcessing:
    """What classifying and serving one row decided, before the reply is formatted."""

    classification: InboundDirectMessage.Classification
    outcome_summary: str
    request_type: str = ""
    reply: ServerMessage | None = None
    reply_readiness: ReplyReadiness = ReplyReadiness.IMMEDIATE
    reconciliation_needed: bool = False


class InboxRowAlreadyProcessedError(Exception):
    """Raised inside the transaction to roll back a service whose row another call processed first."""


def process_inbound_direct_message(
    inbox_row_id: int,
    now: datetime,
    original_text: str | None = None,
) -> InboundProcessingResult:
    """Process one inbox row "received": classify it, run its service and mark it processed, in one transaction.

    A sign-in request is stored with its password redacted, so its password comes from
    `original_text`, the frame's text as the node delivered it; a sign-in row processed without
    it (after a restart) gets no answer, and the client's retry is answered instead. An
    exception rolls everything back and marks the row failed, with no reply.
    """
    try:
        return run_in_service_transaction(lambda: process_in_transaction(inbox_row_id, now, original_text))
    except InboxRowAlreadyProcessedError:
        return build_already_processed_result(inbox_row_id)
    except Exception as processing_error:
        logger.exception("Processing inbox row %s failed; it is marked failed and gets no reply.", inbox_row_id)
        mark_inbox_row_failed(inbox_row_id, processing_error, now)
        return InboundProcessingResult(
            inbox_row_id=inbox_row_id,
            contact_id=read_inbox_row_contact_id(inbox_row_id),
            processing_state=InboundDirectMessage.ProcessingState.FAILED,
            classification=Classification.UNCLASSIFIED,
        )


def process_in_transaction(inbox_row_id: int, now: datetime, original_text: str | None) -> InboundProcessingResult:
    inbox_row = InboundDirectMessage.objects.filter(id=inbox_row_id).first()
    if inbox_row is None or inbox_row.processing_state != InboundDirectMessage.ProcessingState.RECEIVED:
        raise InboxRowAlreadyProcessedError(f"Inbox row {inbox_row_id} is not waiting to be processed.")
    contact = Contact.objects.filter(id=inbox_row.contact_id).first() if inbox_row.contact_id is not None else None

    text_to_process, is_password_available = choose_text_to_process(inbox_row.text, original_text)
    parsed_direct_message = parse_direct_message_text(text_to_process)
    row_processing = classify_and_serve(inbox_row, contact, parsed_direct_message, is_password_available, now)

    reply_key = build_reply_key_for_direct_message(parsed_direct_message, inbox_row_id)
    replies = build_queued_replies(row_processing, contact, reply_key)
    mark_inbox_row_processed(inbox_row_id, row_processing, replies, now)

    return InboundProcessingResult(
        inbox_row_id=inbox_row_id,
        contact_id=contact.pk if contact is not None else None,
        processing_state=InboundDirectMessage.ProcessingState.PROCESSED,
        classification=row_processing.classification,
        replies=replies,
        flood_arrival_route_reset_needed=decide_flood_arrival_route_reset(inbox_row, contact, now),
        reconciliation_needed=row_processing.reconciliation_needed,
    )


def decide_flood_arrival_route_reset(inbox_row: InboundDirectMessage, contact: Contact | None, now: datetime) -> bool:
    if contact is None:
        return False
    return needs_flood_arrival_route_reset(
        arrived_by_flood=inbox_row.path_length != DIRECT_ARRIVAL_PATH_LENGTH,
        received_at=inbox_row.received_at,
        last_path_update_at=contact.last_path_update_at,
        now=now,
    )


def choose_text_to_process(stored_text: str, original_text: str | None) -> tuple[str, bool]:
    """The text to parse, and whether a sign-in request's password is in it."""
    if not carries_account_request_password(stored_text):
        return stored_text, True
    if original_text is not None and redact_stored_text(original_text) == stored_text:
        return original_text, True
    return stored_text, False


def classify_and_serve(
    inbox_row: InboundDirectMessage,
    contact: Contact | None,
    parsed_direct_message: ParsedDirectMessage,
    is_password_available: bool,
    now: datetime,
) -> RowProcessing:
    if contact is None:
        return RowProcessing(
            classification=Classification.UNKNOWN_SENDER,
            outcome_summary="dropped: the sender is not a contact; a reconciliation is requested",
            reconciliation_needed=True,
        )
    if inbox_row.text_type != PLAIN_TEXT_TYPE:
        return RowProcessing(
            classification=Classification.UNSUPPORTED_TEXT_TYPE,
            outcome_summary=f"dropped: MeshCore text type {inbox_row.text_type}",
        )

    request_type = read_version_one_type_letter(inbox_row.text)
    match parsed_direct_message:
        case OtherText():
            return RowProcessing(
                classification=Classification.NOT_PROTOCOL, outcome_summary="not protocol; never answered"
            )
        case UnsupportedVersion():
            return classify_unsupported_version(parsed_direct_message)
        case (
            AccountReply()
            | QueryReply()
            | SendStatusReply()
            | DeliveryPart()
            | ReadReply()
            | ReceiptPush()
            | RefreshReply()
            | ErrorReply()
        ):
            return build_server_type_ignored(request_type)
        case UnknownMessageType():
            return classify_unknown_message_type(parsed_direct_message)
        case ProtocolSyntaxError():
            return classify_syntax_error(parsed_direct_message)
        case DeliveryAcknowledgement():
            acknowledgement_outcome = record_delivery_acknowledgement(contact, parsed_direct_message, now)
            return build_acknowledgement_processing(request_type, acknowledgement_outcome.outcome_summary)
        case ReceiptAcknowledgement():
            acknowledgement_outcome = record_receipt_acknowledgement(contact, parsed_direct_message, now)
            return build_acknowledgement_processing(request_type, acknowledgement_outcome.outcome_summary)
        case _:
            return serve_request(inbox_row, contact, parsed_direct_message, is_password_available, now)


def serve_request(
    inbox_row: InboundDirectMessage,
    contact: Contact,
    request: AccountRequest | QueryRequest | MessagePartRequest | ReadRequest | RefreshRequest,
    is_password_available: bool,
    now: datetime,
) -> RowProcessing:
    match request:
        case AccountRequest():
            if not is_password_available:
                return RowProcessing(
                    classification=Classification.REQUEST,
                    request_type=request.message_type,
                    outcome_summary="not answered: its password is not kept across a restart; a retry is answered",
                )
            account_outcome = register_or_sign_in(contact, request, now)
            return build_request_processing(request, account_outcome.reply, account_outcome.outcome_summary)
        case QueryRequest():
            query_outcome = user_exists(contact, request)
            return build_request_processing(request, query_outcome.reply, query_outcome.outcome_summary)
        case MessagePartRequest():
            part_outcome = accept_message_part(contact, request, now)
            reply_readiness = ReplyReadiness.IMMEDIATE
            if part_outcome.is_incomplete_status:
                reply_readiness = ReplyReadiness.COALESCED_INCOMPLETE_STATUS
            return build_request_processing(
                request, part_outcome.reply, part_outcome.outcome_summary, reply_readiness=reply_readiness
            )
        case ReadRequest():
            read_outcome = record_read(contact, request, now)
            return build_request_processing(request, read_outcome.reply, read_outcome.outcome_summary)
        case RefreshRequest():
            refresh_outcome = start_refresh(contact, request, now, requested_by_inbound_id=inbox_row.pk)
            return build_request_processing(request, refresh_outcome.reply, refresh_outcome.outcome_summary)


def build_request_processing(
    request: AccountRequest | QueryRequest | MessagePartRequest | ReadRequest | RefreshRequest,
    reply: ServerMessage,
    outcome_summary: str,
    reply_readiness: ReplyReadiness = ReplyReadiness.IMMEDIATE,
) -> RowProcessing:
    return RowProcessing(
        classification=Classification.REQUEST,
        request_type=request.message_type,
        outcome_summary=outcome_summary,
        reply=reply,
        reply_readiness=reply_readiness,
    )


def build_acknowledgement_processing(request_type: str, outcome_summary: str) -> RowProcessing:
    return RowProcessing(
        classification=Classification.ACKNOWLEDGEMENT,
        request_type=request_type,
        outcome_summary=outcome_summary,
    )


def build_server_type_ignored(request_type: str) -> RowProcessing:
    return RowProcessing(
        classification=Classification.SERVER_TYPE_IGNORED,
        request_type=request_type,
        outcome_summary="a server type from a contact; never answered",
    )


def classify_unsupported_version(unsupported_version: UnsupportedVersion) -> RowProcessing:
    """Only a request in another version is answered, so two servers of different versions never answer each other."""
    version_error_reply = build_grammar_error_reply(unsupported_version)
    outcome_summary = f"protocol version {unsupported_version.version}"
    if version_error_reply is None:
        outcome_summary += "; not a request, never answered"
    return RowProcessing(
        classification=Classification.UNSUPPORTED_VERSION,
        outcome_summary=outcome_summary,
        reply=version_error_reply,
    )


def classify_unknown_message_type(unknown_message_type: UnknownMessageType) -> RowProcessing:
    letter = unknown_message_type.message_type_letter
    unsupported_type_reply = build_grammar_error_reply(unknown_message_type)
    if unsupported_type_reply is None:
        return build_server_type_ignored(letter)
    return RowProcessing(
        classification=Classification.REQUEST,
        request_type=letter,
        outcome_summary=f"unsupported request type {letter}",
        reply=unsupported_type_reply,
    )


def classify_syntax_error(syntax_error: ProtocolSyntaxError) -> RowProcessing:
    request_type = syntax_error.request_type
    if request_type in ACKNOWLEDGEMENT_TYPES:
        return build_acknowledgement_processing(request_type, "dropped: a malformed acknowledgement")
    syntax_error_reply = build_grammar_error_reply(syntax_error)
    if syntax_error_reply is None:
        return build_server_type_ignored(request_type)
    return RowProcessing(
        classification=Classification.SYNTAX_ERROR,
        request_type="" if request_type == UNKNOWN_REQUEST_TYPE else request_type,
        outcome_summary="the request breaks the grammar",
        reply=syntax_error_reply,
    )


def read_version_one_type_letter(text: str) -> str:
    """The letter after "HT1 ", or "" when the text has none there."""
    if not text.startswith(PROTOCOL_PREFIX):
        return ""
    type_letter = text[len(PROTOCOL_PREFIX) : len(PROTOCOL_PREFIX) + 1]
    return type_letter if type_letter.isascii() and type_letter.isalpha() else ""


def build_queued_replies(
    row_processing: RowProcessing,
    contact: Contact | None,
    reply_key: str | None,
) -> tuple[QueuedReply, ...]:
    """Formatting raises before anything is committed if a reply would break the byte budget or the grammar."""
    if row_processing.reply is None or contact is None or reply_key is None:
        return ()
    return (
        QueuedReply(
            contact_id=contact.pk,
            reply_key=reply_key,
            text=format_server_message(row_processing.reply),
            server_message=row_processing.reply,
            readiness=row_processing.reply_readiness,
        ),
    )


def mark_inbox_row_processed(
    inbox_row_id: int,
    row_processing: RowProcessing,
    replies: tuple[QueuedReply, ...],
    now: datetime,
) -> None:
    """The compare-and-set that makes processing happen once; inbox rows are locked last, after the service's rows."""
    updated_row_count = InboundDirectMessage.objects.filter(
        id=inbox_row_id,
        processing_state=InboundDirectMessage.ProcessingState.RECEIVED,
    ).update(
        processing_state=InboundDirectMessage.ProcessingState.PROCESSED,
        processed_at=now,
        classification=row_processing.classification,
        request_type=row_processing.request_type,
        outcome_summary=row_processing.outcome_summary[:OUTCOME_SUMMARY_MAXIMUM_LENGTH],
        reply_summary=" / ".join(reply.text for reply in replies)[:REPLY_SUMMARY_MAXIMUM_LENGTH],
    )
    if updated_row_count == 0:
        raise InboxRowAlreadyProcessedError(f"Inbox row {inbox_row_id} was processed meanwhile.")


def build_already_processed_result(inbox_row_id: int) -> InboundProcessingResult:
    inbox_row_values = (
        InboundDirectMessage.objects.filter(id=inbox_row_id)
        .values("contact_id", "processing_state", "classification")
        .first()
    )
    if inbox_row_values is None:
        return InboundProcessingResult(
            inbox_row_id=inbox_row_id,
            contact_id=None,
            processing_state=InboundDirectMessage.ProcessingState.PROCESSED,
            classification=Classification.UNCLASSIFIED,
            was_already_processed=True,
        )
    return InboundProcessingResult(
        inbox_row_id=inbox_row_id,
        contact_id=inbox_row_values["contact_id"],
        processing_state=InboundDirectMessage.ProcessingState(inbox_row_values["processing_state"]),
        classification=Classification(inbox_row_values["classification"]),
        was_already_processed=True,
    )


def read_inbox_row_contact_id(inbox_row_id: int) -> int | None:
    return InboundDirectMessage.objects.filter(id=inbox_row_id).values_list("contact_id", flat=True).first()


def mark_inbox_row_failed(inbox_row_id: int, processing_error: Exception, now: datetime) -> None:
    """The error summary goes through the password redaction too: an exception message may quote the text."""
    error_summary = redact_account_request_passwords(f"{type(processing_error).__name__}: {processing_error}")
    InboundDirectMessage.objects.filter(
        id=inbox_row_id,
        processing_state=InboundDirectMessage.ProcessingState.RECEIVED,
    ).update(
        processing_state=InboundDirectMessage.ProcessingState.FAILED,
        processing_error=error_summary,
        processed_at=now,
    )
