"""Reporting a node command's steps to the panel, which shows them while the command runs."""

from node.node_commands import ProgressStep, ProgressStepState, record_node_command_progress
from worker.clock import Clock
from worker.database_access import run_in_database_thread


class NodeCommandFailedError(Exception):
    """The command cannot succeed; the message is shown to the operator as it is."""


class NodeCommandProgress:
    def __init__(self, *, node_command_id: int, clock: Clock) -> None:
        self._node_command_id = node_command_id
        self._clock = clock

    async def start(self, step: str, detail: str = "") -> None:
        await self._record(step, ProgressStepState.RUNNING, detail)

    async def finish(self, step: str, detail: str = "") -> None:
        await self._record(step, ProgressStepState.DONE, detail)

    async def skip(self, step: str, detail: str) -> None:
        await self._record(step, ProgressStepState.SKIPPED, detail)

    async def fail(self, step: str, error_message: str) -> NodeCommandFailedError:
        """Record the failed step and return the error for the caller to raise."""
        await self._record(step, ProgressStepState.FAILED, error_message)
        return NodeCommandFailedError(error_message)

    async def _record(self, step: str, state: ProgressStepState, detail: str) -> None:
        progress_step = ProgressStep(step=step, state=state, detail=detail, at=self._clock.now())
        await run_in_database_thread(record_node_command_progress, self._node_command_id, progress_step)
