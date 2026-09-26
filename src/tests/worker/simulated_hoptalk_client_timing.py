"""How long the simulated HopTalk client waits, and the clock it reads.

The defaults are the protocol's own values in real seconds. Tests scale every duration at once
with `ClientTiming().scaled_by(0.01)`: a 20 s retry pause then takes 0.2 s and every ratio stays
as the protocol has it (a retry pause is still four coalescing pauses). The poll interval is how
often the client looks at its node, not a protocol duration, so it is never scaled.

With scaled timing a client sends far more direct messages per real second than per protocol
second, so its MeshCore timestamps (max(now, previous + 1)) run ahead of the real wall clock, and
a reinstalled app, which starts again from the wall clock, could reuse one. `ScaledClock` keeps
the wall clock in step with the scaled durations; share one instance between the clients of a test.
"""

import dataclasses
import time
from dataclasses import dataclass
from typing import Protocol

RECOMMENDED_RETRY_PAUSES_SECONDS = (20.0, 40.0, 80.0, 160.0, 300.0)
MILLISECONDS_PER_SECOND = 1000
MICROSECONDS_PER_SECOND = 1_000_000


class ClientClock(Protocol):
    def monotonic_seconds(self) -> float:
        """Time for pauses and deadlines; it never steps backwards."""
        ...

    def wall_clock_seconds(self) -> float:
        """Unix time, the source of message ids and MeshCore timestamps; it may step backwards."""
        ...


class SystemClock:
    def monotonic_seconds(self) -> float:
        return time.monotonic()

    def wall_clock_seconds(self) -> float:
        return time.time()


class ScaledClock:
    """Real monotonic time, and a wall clock that runs 1 / time_factor times as fast as the real one.

    The client leaves at least two scaled seconds between its direct messages, so on this clock its
    MeshCore timestamps never run ahead of the wall clock, as on a phone.
    """

    def __init__(self, time_factor: float) -> None:
        self.time_factor = time_factor
        self._monotonic_start_seconds = time.monotonic()
        self._wall_clock_start_seconds = time.time()

    def monotonic_seconds(self) -> float:
        return time.monotonic()

    def wall_clock_seconds(self) -> float:
        elapsed_real_seconds = time.monotonic() - self._monotonic_start_seconds
        return self._wall_clock_start_seconds + elapsed_real_seconds / self.time_factor


@dataclass(frozen=True, kw_only=True)
class ClientTiming:
    # A request is retried after these pauses, then every pause after the last one.
    retry_pauses_seconds: tuple[float, ...] = RECOMMENDED_RETRY_PAUSES_SECONDS
    refresh_all_maximum_retries: int = 3
    minimum_gap_between_direct_messages_seconds: float = 2.0
    maximum_direct_messages_awaiting_acknowledgement: int = 4
    # A direct message stops waiting for its firmware ACK after
    # clamp(factor x the node's suggested timeout, minimum, maximum).
    acknowledgement_wait_factor: float = 1.2
    minimum_acknowledgement_wait_seconds: float = 3.0
    maximum_acknowledgement_wait_seconds: float = 60.0
    table_full_wait_seconds: float = 3.0
    incomplete_acknowledgement_coalescing_seconds: float = 5.0
    path_update_window_seconds: float = 30.0
    wrong_password_wait_seconds: float = 10.0
    rate_limited_wait_seconds: float = 900.0
    server_silence_before_refresh_all_seconds: float = 1800.0
    poll_interval_seconds: float = 0.002

    def scaled_by(self, time_factor: float) -> ClientTiming:
        """Every protocol duration multiplied by the factor; counts and the poll interval stay."""
        return dataclasses.replace(
            self,
            retry_pauses_seconds=tuple(pause * time_factor for pause in self.retry_pauses_seconds),
            minimum_gap_between_direct_messages_seconds=self.minimum_gap_between_direct_messages_seconds * time_factor,
            minimum_acknowledgement_wait_seconds=self.minimum_acknowledgement_wait_seconds * time_factor,
            maximum_acknowledgement_wait_seconds=self.maximum_acknowledgement_wait_seconds * time_factor,
            table_full_wait_seconds=self.table_full_wait_seconds * time_factor,
            incomplete_acknowledgement_coalescing_seconds=(
                self.incomplete_acknowledgement_coalescing_seconds * time_factor
            ),
            path_update_window_seconds=self.path_update_window_seconds * time_factor,
            wrong_password_wait_seconds=self.wrong_password_wait_seconds * time_factor,
            rate_limited_wait_seconds=self.rate_limited_wait_seconds * time_factor,
            server_silence_before_refresh_all_seconds=self.server_silence_before_refresh_all_seconds * time_factor,
        )

    def retry_pause_seconds(self, schedule_step: int) -> float:
        last_step = len(self.retry_pauses_seconds) - 1
        return self.retry_pauses_seconds[min(schedule_step, last_step)]

    def acknowledgement_wait_seconds(self, suggested_timeout_milliseconds: int) -> float:
        """How long a direct message waits for its firmware ACK, from the node's suggested timeout.

        The suggested timeout is what the node reports, so it is never scaled; only the bounds are.
        """
        wait_seconds = self.acknowledgement_wait_factor * suggested_timeout_milliseconds / MILLISECONDS_PER_SECOND
        return min(
            max(wait_seconds, self.minimum_acknowledgement_wait_seconds), self.maximum_acknowledgement_wait_seconds
        )
