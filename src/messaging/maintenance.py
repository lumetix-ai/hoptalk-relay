"""Periodic clean-up of the messaging tables, run by the worker's maintenance task.

Incomplete uploads are dropped 24 hours after their last part. The traffic log (inbox rows and
outbound packets) is kept for RELAY_LOG_RETENTION_DAYS; an inbox row that still waits to be
processed is never pruned. Delivered messages are kept.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from hoptalk_relay.relay_settings import get_relay_settings
from messaging.incoming_messages import delete_expired_incomplete_messages
from messaging.models import InboundDirectMessage, OutboundPacket
from messaging.service_transactions import run_in_service_transaction


@dataclass(frozen=True, kw_only=True)
class MessagingMaintenanceSummary:
    expired_incomplete_messages: int
    pruned_inbox_rows: int
    pruned_outbound_packets: int


def run_messaging_maintenance(now: datetime) -> MessagingMaintenanceSummary:
    retention_days = get_relay_settings().retention.log_retention_days
    pruned_inbox_rows, pruned_outbound_packets = prune_traffic_log(now, retention_days)
    return MessagingMaintenanceSummary(
        expired_incomplete_messages=delete_expired_incomplete_messages(now),
        pruned_inbox_rows=pruned_inbox_rows,
        pruned_outbound_packets=pruned_outbound_packets,
    )


def prune_traffic_log(now: datetime, retention_days: int) -> tuple[int, int]:
    """Delete inbox rows and outbound packets older than the retention; returns how many of each."""
    oldest_kept_time = now - timedelta(days=retention_days)

    def prune_in_transaction() -> tuple[int, int]:
        _, deleted_inbox_counts = (
            InboundDirectMessage.objects.filter(received_at__lt=oldest_kept_time)
            .exclude(processing_state=InboundDirectMessage.ProcessingState.RECEIVED)
            .delete()
        )
        _, deleted_packet_counts = OutboundPacket.objects.filter(prepared_at__lt=oldest_kept_time).delete()
        return (
            deleted_inbox_counts.get(InboundDirectMessage._meta.label, 0),
            deleted_packet_counts.get(OutboundPacket._meta.label, 0),
        )

    return run_in_service_transaction(prune_in_transaction)
