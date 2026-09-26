from django.db import models
from django.db.models import F, Func, Q

from directory.models import Contact, User
from protocol.constants import MAXIMUM_PART_COUNT, MESSAGE_ID_MAXIMUM, MESSAGE_ID_MINIMUM

# One bit per part: bit n-1 stands for part n.
PARTS_MASK_MAXIMUM = (1 << MAXIMUM_PART_COUNT) - 1


class Message(models.Model):
    """A message from its first part on.

    It has no state column: it is incomplete while accepted_at is NULL, accepted once every
    part is held, and delivered and read by the timestamps that only move forward.
    """

    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_messages")
    # The device that sent the first part. A refresh re-arms only the receipts of messages
    # the requesting device sent.
    sender_device = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        default=None,
        related_name="+",
    )
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="received_messages")
    client_message_id = models.BigIntegerField()
    part_count = models.PositiveSmallIntegerField()
    # part_count entries: each part text exactly as received, or null while that part is
    # missing. Forwarded byte for byte.
    part_texts = models.JSONField()
    # The parts concatenated, set when the last part arrives, stored unencrypted. NULL rather
    # than "" marks an incomplete message: a check constraint ties it to accepted_at.
    text = models.TextField(null=True, blank=True, default=None)  # noqa: DJ001
    created_at = models.DateTimeField()
    # The latest valid part; an incomplete message is deleted 24 hours after it.
    last_part_at = models.DateTimeField()
    # Every part held. A refresh sends oldest first in the order (accepted_at, id).
    accepted_at = models.DateTimeField(null=True, blank=True, default=None)
    # The first delivery to any device of the recipient, and the first read on any of them.
    delivered_at = models.DateTimeField(null=True, blank=True, default=None)
    read_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        db_table = "messages"
        constraints = [
            models.UniqueConstraint(
                fields=["sender", "client_message_id"],
                name="message_unique_client_id_per_sender",
            ),
            models.CheckConstraint(
                condition=Q(client_message_id__gte=MESSAGE_ID_MINIMUM, client_message_id__lte=MESSAGE_ID_MAXIMUM),
                name="message_client_id_range",
            ),
            models.CheckConstraint(
                condition=Q(part_count__gte=1, part_count__lte=MAXIMUM_PART_COUNT),
                name="message_part_count_range",
            ),
            # PostgreSQL also raises when part_texts is not an array.
            models.CheckConstraint(
                condition=Q(
                    part_count=Func(
                        F("part_texts"),
                        function="jsonb_array_length",
                        output_field=models.PositiveSmallIntegerField(),
                    )
                ),
                name="message_part_texts_match_part_count",
            ),
            models.CheckConstraint(
                condition=Q(accepted_at__isnull=True, text__isnull=True)
                | Q(accepted_at__isnull=False, text__isnull=False),
                name="message_text_set_when_accepted",
            ),
            models.CheckConstraint(
                condition=Q(delivered_at__isnull=True) | Q(accepted_at__isnull=False),
                name="message_delivered_only_when_accepted",
            ),
            models.CheckConstraint(
                condition=Q(read_at__isnull=True) | Q(delivered_at__isnull=False),
                name="message_read_only_when_delivered",
            ),
        ]
        indexes = [
            models.Index(fields=["recipient", "sender", "accepted_at", "id"], name="message_refresh_scope"),
            models.Index(fields=["recipient", "client_message_id"], name="message_recipient_client_id"),
            models.Index(fields=["accepted_at"], name="message_accepted_at"),
            models.Index(
                fields=["last_part_at"],
                condition=Q(accepted_at__isnull=True),
                name="message_incomplete_last_part",
            ),
        ]

    def __str__(self) -> str:
        return f"message {self.client_message_id} ({self.pk})"


class RefreshSession(models.Model):
    """Re-delivery of a peer's missed messages to one device, one at a time."""

    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        # A head was exhausted.
        STOPPED = "stopped", "Stopped"
        # The device was relinked to another user.
        CANCELLED = "cancelled", "Cancelled"

    device = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="refresh_sessions")
    peer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    state = models.CharField(max_length=16, choices=State.choices, default=State.ACTIVE)
    requested_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True, default=None)
    # Created by "F *".
    requested_for_all_peers = models.BooleanField(default=False)
    # Grows when messages are appended.
    messages_total = models.PositiveIntegerField(default=0)
    requested_by_inbound = models.ForeignKey(
        "InboundDirectMessage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        default=None,
        related_name="+",
    )

    class Meta:
        db_table = "refresh_sessions"
        constraints = [
            models.UniqueConstraint(
                fields=["device", "peer"],
                condition=Q(state="active"),
                name="refresh_single_active_session",
            ),
        ]
        indexes = [
            models.Index(fields=["device", "state"], name="refresh_session_device_state"),
        ]

    def __str__(self) -> str:
        return f"refresh session {self.pk} ({self.state})"


class MessageDelivery(models.Model):
    """One message to one device of its recipient."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED_FOR_REFRESH = "queued_for_refresh", "Queued for a refresh"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"
        # One cause only: the device was relinked to another user.
        CANCELLED = "cancelled", "Cancelled"

    class FailureReason(models.TextChoices):
        ATTEMPTS_EXHAUSTED = "attempts_exhausted", "Attempts exhausted"
        REFRESH_STOPPED = "refresh_stopped", "Refresh stopped"

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="deliveries")
    device = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="deliveries")
    state = models.CharField(max_length=24, choices=State.choices, default=State.PENDING)
    # The owner while queued_for_refresh or the session's head; kept on terminal rows for the
    # record. CASCADE: a SET_NULL would break "queued_for_refresh needs a session" before the
    # row itself is deleted.
    refresh_session = models.ForeignKey(
        RefreshSession,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        default=None,
        related_name="deliveries",
    )
    # Rounds started since the last (re)arm, and the strategy's limit copied at (re)arm.
    attempt_count = models.PositiveSmallIntegerField(default=0)
    maximum_attempts = models.PositiveSmallIntegerField()
    # +1 on every (re)arm; packets copy it, so an outcome of a packet from an earlier arm
    # changes nothing.
    arm_generation = models.PositiveIntegerField(default=1)
    # The due time of the next round, or of the final failure check.
    next_attempt_at = models.DateTimeField(null=True, blank=True, default=None)
    # Bit n-1 is part n still to be sent in the current round.
    round_pending_parts_mask = models.PositiveSmallIntegerField(default=0)
    # The set of the device's latest K, its current truth: never combined with earlier sets.
    parts_received_mask = models.PositiveSmallIntegerField(default=0)
    # Set means "in progress" for the per-device cap on active deliveries.
    round_started_at = models.DateTimeField(null=True, blank=True, default=None)
    last_sent_at = models.DateTimeField(null=True, blank=True, default=None)
    # The last K, evidence for the route-reset decision.
    last_acknowledgement_received_at = models.DateTimeField(null=True, blank=True, default=None)
    delivered_at = models.DateTimeField(null=True, blank=True, default=None)
    read_at = models.DateTimeField(null=True, blank=True, default=None)
    failed_at = models.DateTimeField(null=True, blank=True, default=None)
    cancelled_at = models.DateTimeField(null=True, blank=True, default=None)
    failure_reason = models.CharField(max_length=32, choices=FailureReason.choices, blank=True, default="")
    created_at = models.DateTimeField()

    class Meta:
        db_table = "message_deliveries"
        verbose_name_plural = "message deliveries"
        constraints = [
            models.UniqueConstraint(fields=["message", "device"], name="message_delivery_unique_per_device"),
            models.CheckConstraint(
                condition=Q(round_pending_parts_mask__gte=0, round_pending_parts_mask__lte=PARTS_MASK_MAXIMUM),
                name="message_delivery_round_mask_range",
            ),
            models.CheckConstraint(
                condition=Q(parts_received_mask__gte=0, parts_received_mask__lte=PARTS_MASK_MAXIMUM),
                name="message_delivery_received_mask_range",
            ),
            models.CheckConstraint(
                condition=~Q(state="pending") | Q(next_attempt_at__isnull=False),
                name="message_delivery_pending_has_next_attempt",
            ),
            models.CheckConstraint(
                condition=~Q(state="delivered") | Q(delivered_at__isnull=False),
                name="message_delivery_delivered_has_delivered_at",
            ),
            models.CheckConstraint(
                condition=~Q(state="queued_for_refresh") | Q(refresh_session__isnull=False),
                name="message_delivery_queued_has_refresh_session",
            ),
            models.UniqueConstraint(
                fields=["refresh_session"],
                condition=Q(state="pending"),
                name="refresh_session_single_head",
            ),
        ]
        indexes = [
            models.Index(
                fields=["next_attempt_at"],
                condition=Q(state="pending"),
                name="delivery_pending_next_attempt",
            ),
            models.Index(fields=["device", "state"], name="delivery_device_state"),
            models.Index(fields=["refresh_session", "state"], name="delivery_session_state"),
        ]

    def __str__(self) -> str:
        return f"delivery {self.pk} ({self.state})"


class ReceiptNotification(models.Model):
    """The delivered and read status of one message for one device of its sender."""

    class TargetLevel(models.IntegerChoices):
        DELIVERED = 1, "Delivered"
        READ = 2, "Read"

    class ConfirmedLevel(models.IntegerChoices):
        NOTHING = 0, "Nothing"
        DELIVERED = 1, "Delivered"
        READ = 2, "Read"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"
        # Only by a relink.
        CANCELLED = "cancelled", "Cancelled"

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="receipt_notifications")
    # One of the sender's devices.
    device = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="receipt_notifications")
    # Only grows.
    target_level = models.PositiveSmallIntegerField(choices=TargetLevel.choices)
    # Only grows, and never beyond target_level.
    confirmed_level = models.PositiveSmallIntegerField(choices=ConfirmedLevel.choices, default=ConfirmedLevel.NOTHING)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    maximum_attempts = models.PositiveSmallIntegerField()
    # +1 whenever the receipt is armed again: its target rises to read, or a refresh from the
    # device that sent the message restarts it.
    arm_generation = models.PositiveIntegerField(default=1)
    # The packet of the current attempt is still to be sent.
    round_pending = models.BooleanField(default=False)
    next_attempt_at = models.DateTimeField(null=True, blank=True, default=None)
    round_started_at = models.DateTimeField(null=True, blank=True, default=None)
    last_sent_at = models.DateTimeField(null=True, blank=True, default=None)
    last_confirmation_received_at = models.DateTimeField(null=True, blank=True, default=None)
    failed_at = models.DateTimeField(null=True, blank=True, default=None)
    cancelled_at = models.DateTimeField(null=True, blank=True, default=None)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        db_table = "receipt_notifications"
        constraints = [
            models.UniqueConstraint(fields=["message", "device"], name="receipt_notification_unique_per_device"),
            models.CheckConstraint(
                condition=Q(confirmed_level__lte=F("target_level")),
                name="receipt_confirmed_level_at_most_target",
            ),
            # Unless cancelled: confirmed exactly when the confirmed level reached the target.
            # A cancelled receipt that a read or a refresh revives chooses its state by this rule.
            models.CheckConstraint(
                condition=Q(state="cancelled")
                | Q(state="confirmed", confirmed_level=F("target_level"))
                | (~Q(state__in=["cancelled", "confirmed"]) & ~Q(confirmed_level=F("target_level"))),
                name="receipt_confirmed_state_matches_levels",
            ),
            models.CheckConstraint(
                condition=~Q(state="pending") | Q(next_attempt_at__isnull=False),
                name="receipt_pending_has_next_attempt",
            ),
        ]
        indexes = [
            models.Index(fields=["next_attempt_at"], condition=Q(state="pending"), name="receipt_pending_next_attempt"),
            models.Index(fields=["device", "state"], name="receipt_device_state"),
        ]

    def __str__(self) -> str:
        return f"receipt {self.pk} ({self.state}, level {self.confirmed_level} of {self.target_level})"


class InboundDirectMessage(models.Model):
    """Every direct message the relay node received: the inbox and the inbound traffic log."""

    class Classification(models.TextChoices):
        UNCLASSIFIED = "unclassified", "Unclassified"
        REQUEST = "request", "Request"
        ACKNOWLEDGEMENT = "acknowledgement", "Acknowledgement"
        NOT_PROTOCOL = "not_protocol", "Not protocol"
        UNSUPPORTED_VERSION = "unsupported_version", "Unsupported version"
        SYNTAX_ERROR = "syntax_error", "Syntax error"
        SERVER_TYPE_IGNORED = "server_type_ignored", "Server type ignored"
        UNKNOWN_SENDER = "unknown_sender", "Unknown sender"
        UNSUPPORTED_TEXT_TYPE = "unsupported_text_type", "Unsupported text type"

    class ProcessingState(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    # The id is the processing order.
    received_at = models.DateTimeField()
    sender_public_key_prefix = models.CharField(max_length=12)
    # NULL when the prefix is unknown or the contact was deleted.
    contact = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inbound_direct_messages",
    )
    # A snapshot "name (prefix)", kept when the contact is deleted.
    contact_label = models.CharField(max_length=96, blank=True, default="")
    # The MeshCore timestamp; part of the firmware-repeat key.
    sender_timestamp = models.BigIntegerField()
    # 0 plain, 1 CLI, 2 signed.
    text_type = models.PositiveSmallIntegerField()
    # 255 means it arrived direct, anything else is the flood hop count.
    path_length = models.PositiveSmallIntegerField()
    signal_to_noise_ratio = models.FloatField(null=True, blank=True, default=None)
    # Redacted for a sign-in request: "HT1 A <username> ********".
    text = models.TextField()
    # SHA-256 of the stored, redacted text: a fast hash over a password would be a guessable
    # copy of it. Part of the firmware-repeat key.
    text_sha256 = models.CharField(max_length=64)
    duplicate_count = models.PositiveIntegerField(default=0)
    last_duplicate_at = models.DateTimeField(null=True, blank=True, default=None)
    classification = models.CharField(
        max_length=24,
        choices=Classification.choices,
        default=Classification.UNCLASSIFIED,
    )
    # The letter after "HT1 ".
    request_type = models.CharField(max_length=1, blank=True, default="")
    processing_state = models.CharField(
        max_length=16,
        choices=ProcessingState.choices,
        default=ProcessingState.RECEIVED,
    )
    # An exception summary.
    processing_error = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True, default=None)
    # Such as "part 2/3 stored" or "device relinked from ivan".
    outcome_summary = models.CharField(max_length=200, blank=True, default="")
    # The reply that was queued; replies carry no password.
    reply_summary = models.CharField(max_length=160, blank=True, default="")
    # The route was reset before replying to a flood arrival.
    route_reset_performed = models.BooleanField(default=False)

    class Meta:
        db_table = "inbound_direct_messages"
        indexes = [
            models.Index(fields=["id"], condition=Q(processing_state="received"), name="inbound_received_id"),
            models.Index(fields=["contact", "sender_timestamp"], name="inbound_contact_timestamp"),
            models.Index(fields=["contact", "received_at"], name="inbound_contact_received_at"),
            models.Index(fields=["received_at"], name="inbound_received_at"),
        ]

    def __str__(self) -> str:
        return f"inbound direct message {self.pk} ({self.classification})"


class OutboundPacket(models.Model):
    """Every direct message the relay sends: the outbound traffic log."""

    class Purpose(models.TextChoices):
        REPLY = "reply", "Reply"
        DELIVERY = "delivery", "Delivery"
        RECEIPT = "receipt", "Receipt"

    class State(models.TextChoices):
        PREPARED = "prepared", "Prepared"
        QUEUED_ON_NODE = "queued_on_node", "Queued on the node"
        NODE_ACKNOWLEDGED = "node_acknowledged", "Acknowledged by the node"
        ACKNOWLEDGEMENT_TIMED_OUT = "acknowledgement_timed_out", "Acknowledgement timed out"
        REJECTED_BY_NODE = "rejected_by_node", "Rejected by the node"
        OUTCOME_UNKNOWN = "outcome_unknown", "Outcome unknown"

    class Route(models.TextChoices):
        FLOOD = "flood", "Flood"
        DIRECT = "direct", "Direct"

    class RouteResetState(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "Not applicable"
        PENDING = "pending", "Pending"
        PERFORMED = "performed", "Performed"
        SKIPPED_PATH_UPDATE = "skipped_path_update", "Skipped: the route changed"
        SKIPPED_APPLICATION_EVIDENCE = "skipped_application_evidence", "Skipped: the device answered"
        SKIPPED_LATE_ACKNOWLEDGEMENT = "skipped_late_acknowledgement", "Skipped: a late acknowledgement"
        DROPPED_BY_RESTART = "dropped_by_restart", "Dropped by a restart"

    contact = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="outbound_packets",
    )
    # A snapshot "name (prefix)", kept when the contact is deleted.
    contact_label = models.CharField(max_length=96)
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    message_delivery = models.ForeignKey(
        MessageDelivery,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="outbound_packets",
    )
    part_number = models.PositiveSmallIntegerField(null=True, blank=True)
    receipt_notification = models.ForeignKey(
        ReceiptNotification,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="outbound_packets",
    )
    # The level this packet carried.
    receipt_level = models.PositiveSmallIntegerField(null=True, blank=True)
    # Copied when prepared. A send outcome is recorded only while the delivery or receipt still
    # has these values, so the outcome of an earlier attempt changes nothing.
    arm_generation = models.PositiveIntegerField(null=True, blank=True)
    attempt_number = models.PositiveIntegerField(null=True, blank=True)
    # For replies: the answered request's key, such as "M:bob:1790294400123456".
    reply_key = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField()
    # Unique and increasing across every packet; the MeshCore attempt is always 0.
    sender_timestamp = models.BigIntegerField(unique=True)
    state = models.CharField(max_length=32, choices=State.choices, default=State.PREPARED)
    # From MSG_SENT; the expected acknowledgement code in lower-case hex.
    route = models.CharField(max_length=8, choices=Route.choices, blank=True, default="")
    expected_acknowledgement_code = models.CharField(max_length=8, blank=True, default="")
    suggested_timeout_milliseconds = models.PositiveIntegerField(null=True, blank=True)
    prepared_at = models.DateTimeField(null=True, blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    acknowledgement_deadline_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    # From the acknowledgement push.
    round_trip_milliseconds = models.PositiveIntegerField(null=True, blank=True)
    # 1 to 6, from RESP_CODE_ERR.
    node_error_code = models.PositiveSmallIntegerField(null=True, blank=True)
    route_reset_state = models.CharField(
        max_length=32,
        choices=RouteResetState.choices,
        default=RouteResetState.NOT_APPLICABLE,
    )
    route_reset_decided_at = models.DateTimeField(null=True, blank=True, default=None)
    connection_generation = models.PositiveIntegerField()

    class Meta:
        db_table = "outbound_packets"
        indexes = [
            # Late acknowledgement matching.
            models.Index(
                fields=["expected_acknowledgement_code"],
                condition=Q(state__in=["queued_on_node", "acknowledgement_timed_out"]),
                name="packet_awaiting_ack_code",
            ),
            # The contact removal guard: removing a contact shifts the firmware's contact table,
            # so the reconciler waits until no packet awaits a firmware acknowledgement.
            models.Index(
                fields=["acknowledgement_deadline_at"],
                condition=Q(acknowledged_at__isnull=True),
                name="packet_unacknowledged_deadline",
            ),
            models.Index(fields=["contact", "queued_at"], name="packet_contact_queued_at"),
            models.Index(fields=["prepared_at"], name="packet_prepared_at"),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} packet {self.pk} ({self.state})"
