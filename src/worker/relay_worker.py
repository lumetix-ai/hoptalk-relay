"""The relay worker: one process, one event loop, one task group, and the only owner of the node.

Start-up, in order: take the single-instance lock (a second worker waits here and never opens
the node), settle what the previous process left half done, then start the tasks. Every task
but the lock holder runs supervised, so an exception restarts that task alone; losing the lock
ends the task group and the process, and Compose starts it again.

Shutdown, within the thirty seconds Compose grants: no new command is claimed and the loops
finish their current step; the running node command gets a few seconds; every frame already
taken from the node is recorded; the link is closed and the status written as disconnected;
finally the lock and every database connection are released.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from django.db import connections

from hoptalk_relay.relay_settings import RelaySettings, describe_effective_configuration
from messaging.inbound_log import ReceivedDirectMessageFrame
from worker.acknowledgement_tracker import AcknowledgementTracker
from worker.clock import Clock, SystemClock, cancel_and_wait
from worker.connection_supervisor import ConnectionSupervisor
from worker.contact_reconciler import ContactReconciler
from worker.database_access import run_in_database_thread
from worker.database_listener import DatabaseListener
from worker.inbound_processor import InboundProcessor
from worker.inbound_recorder import InboundRecorder
from worker.maintenance_runner import MaintenanceRunner
from worker.message_drainer import MessageDrainer
from worker.node_command_executor import NodeCommandExecutor
from worker.node_connections import NodeClientFactory
from worker.node_event_router import NodeEventRouter
from worker.node_event_subscriptions import NodeEvent, NodeEventSubscriptions
from worker.node_gateway import NodeGateway
from worker.node_identity_backup_keeper import NodeIdentityBackupKeeper
from worker.node_setup_steps import NodeSetupSteps
from worker.pairing_advertiser import PairingAdvertiser
from worker.reply_queue import ReplyQueue
from worker.sender_loop import SenderLoop
from worker.single_instance_lock import SingleInstanceLock
from worker.startup_recovery import run_startup_recovery
from worker.status_reporter import StatusReporter
from worker.task_supervision import run_supervised_task
from worker.worker_queries import read_existing_contact_ids
from worker.worker_state import WorkerRuntimeStatus, WorkerSignals, WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


class RelayWorker:
    def __init__(
        self,
        *,
        client_factory: NodeClientFactory,
        relay_settings: RelaySettings,
        clock: Clock | None = None,
        timing: WorkerTiming | None = None,
        worker_instance_id: UUID | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.timing = timing or WorkerTiming()
        self.worker_instance_id = worker_instance_id or uuid4()
        self.signals = WorkerSignals()
        self.worker_state = WorkerState(
            runtime_status=WorkerRuntimeStatus(
                worker_instance_id=self.worker_instance_id,
                process_started_at=self.clock.now(),
                transport_description=relay_settings.node_connection.describe(),
                effective_configuration=describe_effective_configuration(relay_settings),
            ),
            signals=self.signals,
            clock=self.clock,
        )
        self.inbound_frame_queue: asyncio.Queue[ReceivedDirectMessageFrame] = asyncio.Queue()
        self.node_event_queue: asyncio.Queue[NodeEvent] = asyncio.Queue()
        self.reply_queue = ReplyQueue(self.timing)
        self.single_instance_lock = SingleInstanceLock(
            clock=self.clock, keepalive_seconds=self.timing.single_instance_keepalive_seconds
        )
        self._build_node_side(client_factory)
        self._build_traffic_side(relay_settings)
        self._build_control_side()

    def _build_node_side(self, client_factory: NodeClientFactory) -> None:
        self.gateway = NodeGateway(timing=self.timing, request_reconnect=self._request_reconnect)
        self.acknowledgement_tracker = AcknowledgementTracker(
            gateway=self.gateway,
            reply_queue=self.reply_queue,
            worker_state=self.worker_state,
            clock=self.clock,
            timing=self.timing,
        )
        self.node_event_subscriptions = NodeEventSubscriptions(
            gateway=self.gateway,
            inbound_frame_queue=self.inbound_frame_queue,
            node_event_queue=self.node_event_queue,
            signals=self.signals,
            clock=self.clock,
            report_disconnection=self._report_disconnection,
        )
        self.identity_backup_keeper = NodeIdentityBackupKeeper(
            gateway=self.gateway, worker_state=self.worker_state, clock=self.clock
        )
        self.connection_supervisor = ConnectionSupervisor(
            client_factory=client_factory,
            gateway=self.gateway,
            subscriptions=self.node_event_subscriptions,
            acknowledgement_tracker=self.acknowledgement_tracker,
            identity_backup_keeper=self.identity_backup_keeper,
            worker_state=self.worker_state,
            clock=self.clock,
            timing=self.timing,
        )
        self.contact_reconciler = ContactReconciler(
            gateway=self.gateway, worker_state=self.worker_state, clock=self.clock, timing=self.timing
        )
        self.pairing_advertiser = PairingAdvertiser(
            gateway=self.gateway, worker_state=self.worker_state, clock=self.clock, timing=self.timing
        )

    def _build_traffic_side(self, relay_settings: RelaySettings) -> None:
        self.message_drainer = MessageDrainer(
            gateway=self.gateway,
            inbound_frame_queue=self.inbound_frame_queue,
            worker_state=self.worker_state,
            clock=self.clock,
            timing=self.timing,
        )
        self.inbound_processor = InboundProcessor(
            gateway=self.gateway,
            reply_queue=self.reply_queue,
            worker_state=self.worker_state,
            clock=self.clock,
            timing=self.timing,
            request_reconciliation=self.contact_reconciler.request_reconciliation,
        )
        self.inbound_recorder = InboundRecorder(
            inbound_frame_queue=self.inbound_frame_queue,
            worker_state=self.worker_state,
            clock=self.clock,
            timing=self.timing,
            hand_over_recorded_row=self.inbound_processor.accept_recorded_row,
        )
        self.sender_loop = SenderLoop(
            gateway=self.gateway,
            reply_queue=self.reply_queue,
            acknowledgement_tracker=self.acknowledgement_tracker,
            worker_state=self.worker_state,
            clock=self.clock,
            timing=self.timing,
            pacing=relay_settings.pacing,
            request_reconciliation=self.contact_reconciler.request_reconciliation,
        )
        self.node_event_router = NodeEventRouter(
            node_event_queue=self.node_event_queue,
            acknowledgement_tracker=self.acknowledgement_tracker,
            pairing_advertiser=self.pairing_advertiser,
            worker_state=self.worker_state,
            clock=self.clock,
            request_reconciliation=self.contact_reconciler.request_reconciliation,
        )

    def _build_control_side(self) -> None:
        self.node_setup_steps = NodeSetupSteps(
            gateway=self.gateway,
            connection_supervisor=self.connection_supervisor,
            identity_backup_keeper=self.identity_backup_keeper,
            message_drainer=self.message_drainer,
            contact_reconciler=self.contact_reconciler,
            worker_state=self.worker_state,
            clock=self.clock,
            timing=self.timing,
        )
        self.node_command_executor = NodeCommandExecutor(
            worker_instance_id=self.worker_instance_id,
            gateway=self.gateway,
            connection_supervisor=self.connection_supervisor,
            message_drainer=self.message_drainer,
            contact_reconciler=self.contact_reconciler,
            pairing_advertiser=self.pairing_advertiser,
            node_setup_steps=self.node_setup_steps,
            worker_state=self.worker_state,
            clock=self.clock,
            timing=self.timing,
        )
        self.status_reporter = StatusReporter(
            worker_state=self.worker_state,
            acknowledgement_tracker=self.acknowledgement_tracker,
            reply_queue=self.reply_queue,
            clock=self.clock,
            timing=self.timing,
        )
        self.maintenance_runner = MaintenanceRunner(
            gateway=self.gateway, worker_state=self.worker_state, clock=self.clock, timing=self.timing
        )
        self.database_listener = DatabaseListener(
            worker_state=self.worker_state,
            timing=self.timing,
            handle_contacts_changed=self._drop_replies_to_deleted_contacts,
        )

    def _request_reconnect(self, reason: str) -> None:
        self.connection_supervisor.request_reconnect(reason)

    def _report_disconnection(self, reason: str) -> None:
        self.connection_supervisor.report_disconnection(reason)

    async def _drop_replies_to_deleted_contacts(self) -> None:
        existing_contact_ids = await run_in_database_thread(read_existing_contact_ids)
        dropped_reply_count = self.reply_queue.drop_replies_to_contacts_other_than(existing_contact_ids)
        if dropped_reply_count:
            logger.info("%d replies to deleted contacts were dropped.", dropped_reply_count)

    def request_shutdown(self) -> None:
        self.signals.shutdown_requested.set()

    # ----- running ----------------------------------------------------------------------------

    async def run(self) -> None:
        """Runs until shutdown was requested and everything is released; raises when the lock is lost."""
        try:
            if not await self._acquire_lock_unless_shutdown():
                return
            await run_startup_recovery(self.clock)
            await self._run_tasks_until_shutdown()
        finally:
            await self._release_resources()
        logger.info("Relay worker stopped.")

    async def _acquire_lock_unless_shutdown(self) -> bool:
        logger.info("Taking the relay lock; this waits while another worker holds it.")
        lock_task = asyncio.ensure_future(self.single_instance_lock.acquire())
        shutdown_task = asyncio.ensure_future(self.signals.shutdown_requested.wait())
        try:
            await asyncio.wait({lock_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await cancel_and_wait({shutdown_task})
        if lock_task.done():
            lock_task.result()
            return True
        await cancel_and_wait({lock_task})
        return False

    def _build_supervised_task_functions(self) -> dict[str, Callable[[], Awaitable[None]]]:
        return {
            "database_listener": self.database_listener.run,
            "connection_supervisor": self.connection_supervisor.run,
            "node_event_router": self.node_event_router.run,
            "message_drainer": self.message_drainer.run,
            "inbound_recorder": self.inbound_recorder.run,
            "inbound_processor": self.inbound_processor.run,
            "sender_loop": self.sender_loop.run,
            "acknowledgement_tracker": self.acknowledgement_tracker.run,
            "node_command_executor": self.node_command_executor.run,
            "contact_reconciler": self.contact_reconciler.run,
            "pairing_advertiser": self.pairing_advertiser.run,
            "status_reporter": self.status_reporter.run,
            "maintenance_runner": self.maintenance_runner.run,
        }

    async def _run_tasks_until_shutdown(self) -> None:
        async with asyncio.TaskGroup() as task_group:
            lock_holder = task_group.create_task(self._hold_the_lock(), name="single_instance_lock_holder")
            supervised_tasks = {
                task_name: task_group.create_task(
                    run_supervised_task(
                        task_function,
                        task_name,
                        worker_state=self.worker_state,
                        clock=self.clock,
                        timing=self.timing,
                    ),
                    name=task_name,
                )
                for task_name, task_function in self._build_supervised_task_functions().items()
            }
            logger.info("Relay worker started (instance %s).", self.worker_instance_id)
            await self.signals.shutdown_requested.wait()
            await self._shut_down_in_order(supervised_tasks)
            for task in [lock_holder, *supervised_tasks.values()]:
                task.cancel()

    async def _hold_the_lock(self) -> None:
        try:
            await self.single_instance_lock.hold()
        except Exception:
            logger.critical(
                "The relay lock's database connection failed, so another worker could take the node over; "
                "this worker stops and starts again."
            )
            raise

    async def _shut_down_in_order(self, supervised_tasks: dict[str, asyncio.Task[None]]) -> None:
        logger.info("Relay worker shutting down.")
        step_timeout_seconds = self.timing.shutdown_step_timeout_seconds
        command_finished = await self.node_command_executor.wait_for_running_command(
            self.timing.node_command_shutdown_wait_seconds
        )
        if not command_finished:
            logger.warning("A node command was still running at shutdown; it will show as interrupted.")
        await wait_for_tasks_to_end([supervised_tasks["message_drainer"]], step_timeout_seconds)
        self.signals.inbound_frames_finished.set()
        await wait_for_tasks_to_end([supervised_tasks["inbound_recorder"]], step_timeout_seconds)
        if not self.inbound_frame_queue.empty():
            logger.error(
                "%d received messages could not be recorded before shutdown.", self.inbound_frame_queue.qsize()
            )

        await cancel_and_wait({supervised_tasks["status_reporter"]})
        self.signals.link_close_requested.set()
        await wait_for_tasks_to_end([supervised_tasks["connection_supervisor"]], step_timeout_seconds)
        with contextlib.suppress(Exception):
            await self.status_reporter.write_final_status()

    async def _release_resources(self) -> None:
        with contextlib.suppress(Exception):
            await self.single_instance_lock.release()
        with contextlib.suppress(Exception):
            await run_in_database_thread(connections.close_all)


async def wait_for_tasks_to_end(tasks: list[asyncio.Task[None]], timeout_seconds: float) -> None:
    """Give the tasks time to finish their current step; cancel whatever still runs afterwards."""
    _finished_tasks, unfinished_tasks = await asyncio.wait(tasks, timeout=timeout_seconds)
    if unfinished_tasks:
        await cancel_and_wait(unfinished_tasks)
