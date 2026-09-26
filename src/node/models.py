from django.db import models
from django.db.models import F, Q


class NodeSetting(models.Model):
    """One key of the configured node. The table holds none or all of the required keys, and optional ones.

    Typed access and the only writes go through node.node_settings and node.node_identity_backups.
    """

    key = models.CharField(max_length=64, primary_key=True)
    value = models.TextField(blank=True)

    class Meta:
        db_table = "node_setting"

    def __str__(self) -> str:
        return self.key


class NodeSetupRun(models.Model):
    """One pass of the setup wizard: read the node, factory reset, configure."""

    class Purpose(models.TextChoices):
        INITIAL = "initial", "Initial setup"
        RECONFIGURE = "reconfigure", "Reconfiguration"

    class State(models.TextChoices):
        READING_NODE = "reading_node", "Reading the node"
        AWAITING_RESET_CONFIRMATION = "awaiting_reset_confirmation", "Awaiting the reset confirmation"
        RESETTING = "resetting", "Resetting"
        AWAITING_CONFIGURATION = "awaiting_configuration", "Awaiting the configuration"
        CONFIGURING = "configuring", "Configuring"
        COMPLETED = "completed", "Completed"
        ABANDONED = "abandoned", "Abandoned"

    FINAL_STATES = (State.COMPLETED, State.ABANDONED)

    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    state = models.CharField(max_length=40, choices=State.choices, default=State.READING_NODE)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True, default=None)
    original_node_information = models.JSONField(null=True, blank=True, default=None)
    original_public_key = models.CharField(max_length=64, blank=True, default="")
    new_public_key = models.CharField(max_length=64, blank=True, default="")
    # The configured key configure_node began to import into the reset node. It is set before the
    # import frame is sent, so from then on the node may hold this key instead of new_public_key.
    restored_public_key = models.CharField(max_length=64, blank=True, default="")
    requested_configuration = models.JSONField(null=True, blank=True, default=None)
    last_error = models.TextField(blank=True, default="")
    # Maintained in the same update as `state`, so that a partial unique constraint can
    # allow only one active run.
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "node_setup_runs"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(is_active=True) & ~Q(state__in=["completed", "abandoned"])
                    | Q(is_active=False, state__in=["completed", "abandoned"])
                ),
                name="node_setup_run_active_matches_state",
            ),
            models.UniqueConstraint(
                fields=["is_active"],
                condition=Q(is_active=True),
                name="node_setup_single_active_run",
            ),
        ]

    def __str__(self) -> str:
        return f"setup run {self.pk} ({self.state})"


class NodeCommand(models.Model):
    """A request from the web to the worker, which alone talks to the node."""

    class Kind(models.TextChoices):
        READ_NODE_INFORMATION = "read_node_information", "Read node information"
        FACTORY_RESET = "factory_reset", "Factory reset"
        CONFIGURE_NODE = "configure_node", "Configure node"
        APPLY_CONFIGURED_SETTINGS = "apply_configured_settings", "Apply configured settings"
        REBOOT_NODE = "reboot_node", "Reboot node"
        SEND_ADVERT = "send_advert", "Send advert"
        EXPORT_CONTACT_CARD = "export_contact_card", "Export contact card"
        START_PAIRING = "start_pairing", "Start pairing"
        STOP_PAIRING = "stop_pairing", "Stop pairing"
        RECONCILE_CONTACTS = "reconcile_contacts", "Reconcile contacts"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        INTERRUPTED = "interrupted", "Interrupted"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    TERMINAL_STATES = (State.SUCCEEDED, State.FAILED, State.INTERRUPTED, State.EXPIRED, State.CANCELLED)

    kind = models.CharField(max_length=40, choices=Kind.choices)
    arguments = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    # The command history outlives an abandoned setup run.
    setup_run = models.ForeignKey(
        NodeSetupRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        default=None,
        related_name="node_commands",
    )
    created_at = models.DateTimeField()
    # The worker never starts a command after this.
    expires_at = models.DateTimeField()
    claimed_at = models.DateTimeField(null=True, blank=True, default=None)
    finished_at = models.DateTimeField(null=True, blank=True, default=None)
    claimed_by_worker_instance = models.UUIDField(null=True, blank=True, default=None)
    progress = models.JSONField(default=list)
    result = models.JSONField(null=True, blank=True, default=None)
    error_message = models.TextField(blank=True, default="")

    class Meta:
        db_table = "node_commands"
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~Q(state__in=["succeeded", "failed", "interrupted", "expired", "cancelled"])
                    | Q(finished_at__isnull=False)
                ),
                name="node_command_terminal_state_has_finished_at",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__gt=F("created_at")),
                name="node_command_expires_after_creation",
            ),
            models.UniqueConstraint(
                fields=["state"],
                condition=Q(state="running"),
                name="node_command_single_running",
            ),
        ]
        indexes = [
            models.Index(fields=["created_at"], condition=Q(state="pending"), name="node_command_pending_created"),
            models.Index(fields=["kind", "created_at"], name="node_command_kind_created"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} command {self.pk} ({self.state})"


class WorkerStatus(models.Model):
    """The worker's live status, one row written only by the worker and read by the panel."""

    class RelayMode(models.TextChoices):
        DISCONNECTED = "disconnected", "Disconnected"
        NOT_CONFIGURED = "not_configured", "Not configured"
        SETUP_IN_PROGRESS = "setup_in_progress", "Setup in progress"
        IDENTITY_MISMATCH = "identity_mismatch", "Identity mismatch"
        RUNNING = "running", "Running"

    class ConnectionState(models.TextChoices):
        DISCONNECTED = "disconnected", "Disconnected"
        CONNECTING = "connecting", "Connecting"
        HANDSHAKING = "handshaking", "Handshaking"
        CONNECTED = "connected", "Connected"

    class NodeIdentityBackupState(models.TextChoices):
        """What the worker last found or did about the backup of the configured node's private key."""

        NOT_CHECKED = "not_checked", "Not checked"
        STORED = "stored", "Stored"
        MISSING = "missing", "Missing"
        UNREADABLE = "unreadable", "Unreadable"
        EXPORT_DISABLED = "export_disabled", "Export disabled in the firmware"
        FAILED = "failed", "Failed"

    SINGLE_ROW_ID = 1

    id = models.SmallIntegerField(primary_key=True, default=SINGLE_ROW_ID)
    worker_instance_id = models.UUIDField(null=True, blank=True, default=None)
    process_started_at = models.DateTimeField(null=True, blank=True, default=None)
    heartbeat_at = models.DateTimeField(null=True, blank=True, default=None)
    relay_mode = models.CharField(max_length=24, choices=RelayMode.choices, default=RelayMode.DISCONNECTED)
    connection_state = models.CharField(
        max_length=16,
        choices=ConnectionState.choices,
        default=ConnectionState.DISCONNECTED,
    )
    # +1 on every successful handshake.
    connection_generation = models.IntegerField(default=0)
    connected_since = models.DateTimeField(null=True, blank=True, default=None)
    transport_description = models.CharField(max_length=128, blank=True, default="")
    node_public_key = models.CharField(max_length=64, blank=True, default="")
    node_name = models.CharField(max_length=64, blank=True, default="")
    node_firmware_version = models.CharField(max_length=64, blank=True, default="")
    node_model = models.CharField(max_length=64, blank=True, default="")
    node_protocol_version = models.IntegerField(null=True, blank=True, default=None)
    node_contact_count = models.IntegerField(null=True, blank=True, default=None)
    # The node's clock minus the server's.
    node_clock_offset_seconds = models.IntegerField(null=True, blank=True, default=None)
    node_radio_summary = models.CharField(max_length=128, blank=True, default="")
    # [{"key", "expected", "actual", "corrected"}]
    settings_drift = models.JSONField(default=list, blank=True)
    # The retry, pacing and retention values the worker runs with.
    effective_configuration = models.JSONField(default=dict, blank=True)
    node_identity_backup_state = models.CharField(
        max_length=16,
        choices=NodeIdentityBackupState.choices,
        default=NodeIdentityBackupState.NOT_CHECKED,
    )
    packets_awaiting_node_acknowledgement = models.IntegerField(default=0)
    replies_queued = models.IntegerField(default=0)
    deliveries_due = models.IntegerField(default=0)
    receipts_due = models.IntegerField(default=0)
    pending_route_resets = models.IntegerField(default=0)
    consecutive_connect_failures = models.IntegerField(default=0)
    last_error_message = models.TextField(blank=True, default="")
    last_error_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        db_table = "worker_status"
        verbose_name_plural = "worker status"
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="worker_status_single_row"),
        ]

    def __str__(self) -> str:
        return f"worker status ({self.relay_mode}, {self.connection_state})"


class PairingSession(models.Model):
    """A period of periodic adverts while the operator watches for a new user's node."""

    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        ENDED = "ended", "Ended"
        STOPPED = "stopped", "Stopped"

    state = models.CharField(max_length=16, choices=State.choices, default=State.ACTIVE)
    # Set by the worker when it sends the first advert, so the countdown matches reality.
    started_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    advert_interval_seconds = models.PositiveSmallIntegerField(default=30)
    adverts_sent = models.PositiveIntegerField(default=0)
    advert_flood = models.BooleanField(default=False)
    last_advert_at = models.DateTimeField(null=True, blank=True, default=None)
    finished_at = models.DateTimeField(null=True, blank=True, default=None)
    start_command = models.ForeignKey(
        NodeCommand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        default=None,
        related_name="+",
    )

    class Meta:
        db_table = "pairing_sessions"
        constraints = [
            models.UniqueConstraint(
                fields=["state"],
                condition=Q(state="active"),
                name="pairing_single_active_session",
            ),
        ]

    def __str__(self) -> str:
        return f"pairing session {self.pk} ({self.state})"


class HeardAdvert(models.Model):
    """An advert the node heard from a node it does not store, during a pairing session."""

    # Heard adverts mean nothing outside their session.
    pairing_session = models.ForeignKey(PairingSession, on_delete=models.CASCADE, related_name="heard_adverts")
    # Lower-case hex.
    public_key = models.CharField(max_length=64)
    name = models.CharField(max_length=64, blank=True, default="")
    # node.contact_cards.MeshCoreNodeType; only a chat node can be added as a contact.
    node_type = models.PositiveSmallIntegerField()
    # The peer's clock.
    advert_timestamp = models.BigIntegerField()
    latitude_microdegrees = models.IntegerField(default=0)
    longitude_microdegrees = models.IntegerField(default=0)
    # The full NEW_CONTACT (0x8A) payload, used as it is for add_contact.
    contact_record = models.JSONField()
    first_heard_at = models.DateTimeField()
    last_heard_at = models.DateTimeField()
    heard_count = models.PositiveIntegerField(default=1)
    added_contact = models.ForeignKey(
        "directory.Contact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        default=None,
        related_name="+",
    )

    class Meta:
        db_table = "heard_adverts"
        constraints = [
            models.UniqueConstraint(
                fields=["pairing_session", "public_key"],
                name="heard_advert_unique_key_per_session",
            ),
        ]

    def __str__(self) -> str:
        return f"advert of {self.name or self.public_key[:12]}"
