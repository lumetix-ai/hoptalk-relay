from django.db import models
from django.db.models import Q
from django.db.models.functions import Left, Lower


class User(models.Model):
    """A HopTalk user, created only by a sign-in request (A) of a new username."""

    # The spelling used when the account was created; every reply carries it.
    username = models.CharField(max_length=16)
    # Usernames are unique case-insensitively, and every lookup uses this column.
    username_lookup = models.GeneratedField(
        expression=Lower("username"),
        output_field=models.CharField(max_length=16),
        db_persist=True,
        unique=True,
    )
    # make_password() of the NFC-normalised password, Argon2 first in PASSWORD_HASHERS.
    password_hash = models.CharField(max_length=255)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "users"
        constraints = [
            models.CheckConstraint(
                condition=Q(username__regex=r"^[A-Za-z0-9]{3,16}$"),
                name="user_username_format",
            ),
        ]

    def __str__(self) -> str:
        return self.username


class Contact(models.Model):
    """A MeshCore node the relay node must hold. A contact with a user is that user's device."""

    class Source(models.TextChoices):
        CARD = "card", "Contact card"
        PAIRING = "pairing", "Pairing"

    class NodeSyncState(models.TextChoices):
        PENDING_ADD = "pending_add", "Waiting to be added to the node"
        ON_NODE = "on_node", "On the node"
        ADD_FAILED = "add_failed", "Adding to the node failed"

    public_key = models.CharField(max_length=64, unique=True)
    # Inbound frames name the sender by six bytes, so two contacts sharing them could not be
    # told apart; an add that collides is refused.
    public_key_prefix = models.GeneratedField(
        expression=Left("public_key", 12),
        output_field=models.CharField(max_length=12),
        db_persist=True,
        unique=True,
    )
    # The name at add time, at most 31 bytes of UTF-8, sent with add_contact.
    name = models.CharField(max_length=64, blank=True, default="")
    # A type-0 record would land in a transient slot of the node's contact table.
    node_type = models.PositiveSmallIntegerField(default=1)
    # From the card or the advert; never in the future.
    advert_timestamp = models.BigIntegerField(default=0)
    latitude_microdegrees = models.IntegerField(default=0)
    longitude_microdegrees = models.IntegerField(default=0)
    source = models.CharField(max_length=16, choices=Source.choices)
    card_uri = models.TextField(blank=True, default="")
    added_at = models.DateTimeField()
    # Deleting a user deletes its devices, and the reconciler then removes them from the node.
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        default=None,
        related_name="devices",
    )
    # The device's "date added" on the Users page.
    linked_at = models.DateTimeField(null=True, blank=True, default=None)
    # Wrong passwords from this device in the current window, which lasts 15 minutes from its
    # first failure.
    failed_sign_in_count = models.PositiveSmallIntegerField(default=0)
    failed_sign_in_window_started_at = models.DateTimeField(null=True, blank=True, default=None)
    # Written by the reconciler; a successful factory reset sets every contact to pending_add.
    node_sync_state = models.CharField(
        max_length=16,
        choices=NodeSyncState.choices,
        default=NodeSyncState.PENDING_ADD,
    )
    node_sync_error = models.TextField(blank=True, default="")
    node_synced_at = models.DateTimeField(null=True, blank=True, default=None)
    # Display-only mirrors of the node's adv_name and route (-1 flood, 0 or more hops).
    node_name = models.CharField(max_length=64, blank=True, default="")
    node_out_path_length = models.SmallIntegerField(null=True, blank=True, default=None)
    # Any inbound direct message from this contact, firmware-level repeats included.
    last_heard_at = models.DateTimeField(null=True, blank=True, default=None)
    # The last PATH_UPDATE push for this contact: a route that changed after a packet was
    # queued needs no reset.
    last_path_update_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        db_table = "contacts"
        constraints = [
            models.CheckConstraint(
                condition=Q(public_key__regex=r"^[0-9a-f]{64}$"),
                name="contact_public_key_format",
            ),
            models.CheckConstraint(condition=Q(node_type=1), name="contact_node_type_chat"),
            models.CheckConstraint(
                condition=Q(user__isnull=True, linked_at__isnull=True) | Q(user__isnull=False, linked_at__isnull=False),
                name="contact_linked_at_matches_user",
            ),
        ]
        indexes = [
            models.Index(fields=["node_sync_state"], name="contact_node_sync_state"),
        ]

    def __str__(self) -> str:
        return f"{self.name or 'unnamed'} ({self.public_key[:12]})"
