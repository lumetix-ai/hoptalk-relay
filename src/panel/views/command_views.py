from datetime import datetime

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET
from django_htmx.http import HTMX_STOP_POLLING

from node.models import NodeCommand
from node.node_commands import expire_pending_node_commands, is_node_command_terminal
from panel.presenters import build_displayed_progress_steps

# ?refresh_page_when_finished=1 reloads the page once the command is over, for pages whose
# content depends on its outcome (a pairing session that started, for example).
REFRESH_PAGE_PARAMETER = "refresh_page_when_finished"


def expire_node_command_if_overdue(node_command: NodeCommand, now: datetime) -> None:
    """Only the worker expires commands otherwise, so while it is away an overdue one would stay pending."""
    if node_command.state == NodeCommand.State.PENDING and node_command.expires_at <= now:
        expire_pending_node_commands(now)
        node_command.refresh_from_db()


@require_GET
def show_command_progress(request: HttpRequest, node_command_id: int) -> HttpResponse:
    node_command = get_object_or_404(NodeCommand, id=node_command_id)
    expire_node_command_if_overdue(node_command, timezone.now())
    command_is_finished = is_node_command_terminal(node_command)
    response = render(
        request,
        "panel/command_progress.html",
        {
            "node_command": node_command,
            "displayed_steps": build_displayed_progress_steps(node_command),
            "refresh_page_when_finished": request.GET.get(REFRESH_PAGE_PARAMETER) == "1",
        },
        status=HTMX_STOP_POLLING if command_is_finished else 200,
    )
    if command_is_finished and request.GET.get(REFRESH_PAGE_PARAMETER) == "1":
        response["HX-Refresh"] = "true"
    return response
