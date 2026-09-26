"""How fast end-to-end scenarios run: the production settings, scaled by SCENARIO_TIME_FACTOR.

Every duration a client can observe is the production one multiplied by SCENARIO_TIME_FACTOR, on the
clients and on the server alike, as the fake node's own delays are: the server's retry rounds and
receipt hold-back, its identical-reply window and status coalescing, the route windows, and the
clients' retry pauses. So the ratios the protocol relies on stay as they are in production, such
as a client's retry pause against the server's identical-reply window, or a device away for a few
retry rounds against the server's give-up time. The firmware ACK waits are among them: a reply
whose ACK wait ends unanswered is sent again by flood, which a client sees against its own retry
pause. Delays internal to the worker, such as command timeouts, keep the worker tests' fast values.
"""

from dataclasses import replace

from hoptalk_relay.relay_settings import EngineTimingSettings, RetryStrategy
from protocol.constants import IDENTICAL_REPLY_SUPPRESSION_SECONDS, INCOMPLETE_SEND_STATUS_COALESCING_SECONDS
from tests.worker.relay_worker.worker_harness import FAST_PACING, FAST_WORKER_TIMING
from tests.worker.simulated_hoptalk_client_timing import ClientTiming

SCENARIO_TIME_FACTOR = 0.01
SCENARIO_CLIENT_TIMING = ClientTiming().scaled_by(SCENARIO_TIME_FACTOR)
SCENARIO_WORKER_TIMING = replace(
    FAST_WORKER_TIMING,
    incomplete_status_coalescing_seconds=INCOMPLETE_SEND_STATUS_COALESCING_SECONDS * SCENARIO_TIME_FACTOR,
    identical_reply_suppression_seconds=IDENTICAL_REPLY_SUPPRESSION_SECONDS * SCENARIO_TIME_FACTOR,
)
# The defaults of src/.env.example, which docs/protocol.md section 13.2 lists for clients.
SCENARIO_RETRY_STRATEGY = RetryStrategy(
    maximum_attempts=6,
    initial_pause_seconds=30 * SCENARIO_TIME_FACTOR,
    backoff_multiplier=2.0,
    maximum_pause_seconds=600 * SCENARIO_TIME_FACTOR,
    delivered_receipt_delay_seconds=15 * SCENARIO_TIME_FACTOR,
)
SCENARIO_PACING = replace(FAST_PACING, minimum_seconds_between_sends=2.0 * SCENARIO_TIME_FACTOR)
PRODUCTION_ENGINE_TIMING = EngineTimingSettings()
SCENARIO_ENGINE_TIMING = EngineTimingSettings(
    minimum_acknowledgement_wait_seconds=(
        PRODUCTION_ENGINE_TIMING.minimum_acknowledgement_wait_seconds * SCENARIO_TIME_FACTOR
    ),
    maximum_acknowledgement_wait_seconds=(
        PRODUCTION_ENGINE_TIMING.maximum_acknowledgement_wait_seconds * SCENARIO_TIME_FACTOR
    ),
    unknown_acknowledgement_wait_seconds=(
        PRODUCTION_ENGINE_TIMING.unknown_acknowledgement_wait_seconds * SCENARIO_TIME_FACTOR
    ),
    missing_parts_round_delay_seconds=PRODUCTION_ENGINE_TIMING.missing_parts_round_delay_seconds * SCENARIO_TIME_FACTOR,
    recent_path_update_seconds=PRODUCTION_ENGINE_TIMING.recent_path_update_seconds * SCENARIO_TIME_FACTOR,
    flood_arrival_reset_maximum_age_seconds=(
        PRODUCTION_ENGINE_TIMING.flood_arrival_reset_maximum_age_seconds * SCENARIO_TIME_FACTOR
    ),
    reply_resend_maximum_age_seconds=PRODUCTION_ENGINE_TIMING.reply_resend_maximum_age_seconds * SCENARIO_TIME_FACTOR,
)
