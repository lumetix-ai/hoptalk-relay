"""Replies waiting for the sender loop, kept in memory: one per contact and request.

A reply answers a request the client keeps retrying until it gets an answer, so a reply lost
with the worker's memory costs nothing but a retry. The rules:

- A newer reply for the same (contact, reply key) replaces the older one: a success and an
  error for one request, or two incomplete send statuses, never both go out.
- An incomplete send status waits until no newer part of its message arrived for a few seconds,
  so a burst of parts costs one status; everything else is ready at once.
- A reply expires a minute after it was created.
- A text equal to one sent to the same contact a few seconds earlier is dropped: it answers a
  retry of a request whose answer is already on its way. The acknowledgement tracker's single
  resend by flood after a route reset is exempt, since that copy is the point. Nor does that
  resend hold back a later equal text: it goes out when the first copy's firmware ACK wait ends,
  which on a long route is more than 10 s later, so a client's retry 20 s after the first copy
  would fall inside its window and go unanswered whenever the resend was lost as well.

Times are the worker clock's monotonic seconds.
"""

import itertools
from dataclasses import dataclass, replace

from messaging.request_processing import ReplyReadiness
from worker.worker_timing import WorkerTiming


@dataclass(frozen=True, kw_only=True)
class PendingReply:
    contact_id: int
    reply_key: str
    text: str
    ready_at: float
    created_at: float
    expires_at: float
    # Breaks ties between replies that became ready at the same time: first queued, first sent.
    sequence_number: int
    bypasses_identical_reply_suppression: bool = False


class ReplyQueue:
    def __init__(self, timing: WorkerTiming) -> None:
        self._timing = timing
        self._pending_replies: dict[tuple[int, str], PendingReply] = {}
        self._sent_texts: dict[tuple[int, str], float] = {}
        self._sequence_numbers = itertools.count()

    def __len__(self) -> int:
        return len(self._pending_replies)

    def add_reply(
        self, *, contact_id: int, reply_key: str, text: str, readiness: ReplyReadiness, now: float
    ) -> PendingReply:
        """Queue a reply the services produced, replacing any older reply to the same request."""
        is_coalesced = readiness == ReplyReadiness.COALESCED_INCOMPLETE_STATUS
        ready_delay = self._timing.incomplete_status_coalescing_seconds if is_coalesced else 0.0
        pending_reply = PendingReply(
            contact_id=contact_id,
            reply_key=reply_key,
            text=text,
            ready_at=now + ready_delay,
            created_at=now,
            expires_at=now + self._timing.reply_lifetime_seconds,
            sequence_number=next(self._sequence_numbers),
        )
        self._pending_replies[(contact_id, reply_key)] = pending_reply
        return pending_reply

    def add_flood_resend(self, *, contact_id: int, reply_key: str, text: str, now: float) -> PendingReply:
        """The same reply once more, after its route was reset: ready at once and never suppressed as identical."""
        pending_reply = PendingReply(
            contact_id=contact_id,
            reply_key=reply_key,
            text=text,
            ready_at=now,
            created_at=now,
            expires_at=now + self._timing.reply_lifetime_seconds,
            sequence_number=next(self._sequence_numbers),
            bypasses_identical_reply_suppression=True,
        )
        self._pending_replies[(contact_id, reply_key)] = pending_reply
        return pending_reply

    def put_back(self, pending_reply: PendingReply, *, ready_at: float) -> bool:
        """A reply the node could not take (its packet pool was full) waits again, unless a newer one replaced it."""
        reply_identity = (pending_reply.contact_id, pending_reply.reply_key)
        if reply_identity in self._pending_replies or ready_at >= pending_reply.expires_at:
            return False
        self._pending_replies[reply_identity] = replace(pending_reply, ready_at=ready_at)
        return True

    def has_reply_for(self, contact_id: int, reply_key: str) -> bool:
        return (contact_id, reply_key) in self._pending_replies

    def take_next_ready_reply(self, now: float) -> PendingReply | None:
        """The earliest ready reply, removed from the queue; expired and identical replies are dropped on the way."""
        self._drop_expired_replies(now)
        for pending_reply in sorted(self._pending_replies.values(), key=order_by_readiness):
            if pending_reply.ready_at > now:
                return None
            del self._pending_replies[(pending_reply.contact_id, pending_reply.reply_key)]
            if not self._repeats_a_recently_sent_text(pending_reply, now):
                return pending_reply
        return None

    def record_reply_sent(self, sent_reply: PendingReply, now: float) -> None:
        if sent_reply.bypasses_identical_reply_suppression:
            return
        self._forget_old_sent_texts(now)
        self._sent_texts[(sent_reply.contact_id, sent_reply.text)] = now

    def next_ready_time(self, now: float) -> float | None:
        self._drop_expired_replies(now)
        if not self._pending_replies:
            return None
        return min(pending_reply.ready_at for pending_reply in self._pending_replies.values())

    def drop_replies_to_contacts_other_than(self, existing_contact_ids: set[int]) -> int:
        """Replies to deleted contacts could never be sent."""
        stale_identities = [
            reply_identity for reply_identity in self._pending_replies if reply_identity[0] not in existing_contact_ids
        ]
        for reply_identity in stale_identities:
            del self._pending_replies[reply_identity]
        return len(stale_identities)

    def _repeats_a_recently_sent_text(self, pending_reply: PendingReply, now: float) -> bool:
        if pending_reply.bypasses_identical_reply_suppression:
            return False
        sent_at = self._sent_texts.get((pending_reply.contact_id, pending_reply.text))
        return sent_at is not None and now - sent_at < self._timing.identical_reply_suppression_seconds

    def _drop_expired_replies(self, now: float) -> None:
        expired_identities = [
            reply_identity
            for reply_identity, pending_reply in self._pending_replies.items()
            if pending_reply.expires_at <= now
        ]
        for reply_identity in expired_identities:
            del self._pending_replies[reply_identity]

    def _forget_old_sent_texts(self, now: float) -> None:
        old_entries = [
            sent_text_identity
            for sent_text_identity, sent_at in self._sent_texts.items()
            if now - sent_at >= self._timing.identical_reply_suppression_seconds
        ]
        for sent_text_identity in old_entries:
            del self._sent_texts[sent_text_identity]


def order_by_readiness(pending_reply: PendingReply) -> tuple[float, int]:
    return pending_reply.ready_at, pending_reply.sequence_number
