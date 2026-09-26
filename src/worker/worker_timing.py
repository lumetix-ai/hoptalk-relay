"""Every delay, timeout and interval of the relay worker, in seconds.

The defaults are the production values. They are not read from src/.env: they follow from the
protocol, the firmware, the serial link and the mesh rather than from an operator's choice. Tests build a
WorkerTiming with tiny values, so that the whole worker runs against the fake node in
milliseconds.
"""

from dataclasses import dataclass

from protocol.constants import IDENTICAL_REPLY_SUPPRESSION_SECONDS, INCOMPLETE_SEND_STATUS_COALESCING_SECONDS


@dataclass(frozen=True, kw_only=True)
class WorkerTiming:
    # Node gateway. After a lost reply the command lock is kept a little longer, so that a late
    # reply cannot be taken for the answer to the next command.
    node_command_timeout_seconds: float = 10.0
    late_reply_grace_seconds: float = 1.5
    lost_replies_before_reconnect: int = 3
    get_next_message_timeout_seconds: float = 5.0
    # A contact listing streams one contact per main-loop pass of the firmware.
    contact_listing_activity_seconds: float = 5.0
    contact_listing_overall_seconds: float = 60.0
    contact_listing_busy_retry_seconds: float = 2.0
    factory_reset_reply_seconds: float = 3.0

    # Connection supervisor.
    tcp_connect_timeout_seconds: float = 15.0
    connect_backoff_initial_seconds: float = 1.0
    connect_backoff_maximum_seconds: float = 30.0
    # A connection that lasted this long counts as healthy: the backoff starts again at the initial delay.
    stable_connection_seconds: float = 60.0
    watchdog_interval_seconds: float = 5.0
    idle_health_check_seconds: float = 60.0
    failed_watchdog_checks_before_reconnect: int = 2
    client_disconnect_timeout_seconds: float = 5.0
    # The node refuses to move its clock backwards, so a clock ahead of the server is only reported.
    node_clock_behind_tolerance_seconds: int = 5
    node_clock_ahead_warning_seconds: int = 60

    # Single-instance lock and database notifications.
    single_instance_keepalive_seconds: float = 10.0
    database_sweep_seconds: float = 5.0

    # Inbound traffic.
    drain_poll_seconds: float = 30.0
    maximum_unrecorded_frames: int = 10
    lost_drain_replies_before_stop: int = 3
    inbound_recording_attempts: int = 3
    inbound_recording_retry_seconds: float = 1.0

    # Replies: an incomplete send status waits for more parts; every reply expires; an identical
    # text to the same contact is not sent twice within a short window.
    incomplete_status_coalescing_seconds: float = INCOMPLETE_SEND_STATUS_COALESCING_SECONDS
    reply_lifetime_seconds: float = 60.0
    identical_reply_suppression_seconds: float = IDENTICAL_REPLY_SUPPRESSION_SECONDS

    # Sender loop.
    minimum_sender_sleep_seconds: float = 1.0
    maximum_sender_sleep_seconds: float = 30.0
    table_full_backoff_initial_seconds: float = 5.0
    table_full_backoff_maximum_seconds: float = 60.0
    table_full_streak_before_error: int = 10

    # Acknowledgement tracker: an ACK that matched nothing may belong to a MSG_SENT not recorded yet.
    unmatched_acknowledgement_lifetime_seconds: float = 60.0

    # Waiting until no packet awaits a firmware ACK, before contacts are removed or the node reboots.
    # A removal waits for the latest ACK deadline recorded, but never gives up sooner than this.
    minimum_removal_quiet_wait_seconds: float = 30.0
    node_restart_quiet_wait_seconds: float = 10.0
    quiet_poll_seconds: float = 0.5

    # Contact reconciliation.
    reconciliation_interval_seconds: float = 600.0
    postponed_removal_retry_seconds: float = 60.0

    # Node commands.
    node_command_shutdown_wait_seconds: float = 10.0
    reboot_disconnect_wait_seconds: float = 15.0
    reconnect_after_restart_wait_seconds: float = 90.0
    advert_retry_seconds: float = 3.0

    # Status, maintenance and task supervision.
    status_interval_seconds: float = 5.0
    maintenance_interval_seconds: float = 600.0
    task_restart_initial_seconds: float = 1.0
    task_restart_maximum_seconds: float = 30.0
    task_healthy_after_seconds: float = 60.0
    shutdown_step_timeout_seconds: float = 5.0
