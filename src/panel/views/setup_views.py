"""The first-run setup wizard: read the node, factory reset it, configure it, show its card."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_htmx.http import HTMX_STOP_POLLING

from node.models import NodeCommand, NodeSetupRun, WorkerStatus
from node.node_commands import expire_pending_node_commands
from node.node_identity_backups import ConfiguredNodeIdentityBackup, read_configured_node_identity_backup
from node.node_information import InvalidNodeInformationError, NodeInformation
from node.node_settings import (
    MANUAL_RADIO_PRESET_TITLE,
    IncompleteNodeConfigurationError,
    NodeConfiguration,
    is_node_configured,
    load_node_configuration,
)
from node.radio_presets import RadioPreset, find_current_radio_preset, find_radio_preset, format_thousandths
from node.setup_runs import (
    DEFAULT_NODE_NAME,
    WORKER_STATES,
    RequestedNodeConfiguration,
    SetupRunTransitionError,
    cancel_setup_run,
    confirm_factory_reset,
    find_expected_factory_reset_confirmation,
    find_latest_setup_run_command,
    get_active_setup_run,
    get_latest_completed_setup_run,
    read_original_node_information,
    read_requested_configuration,
    retry_reading_node,
    start_setup_run,
    submit_node_configuration,
)
from node.worker_status import is_node_connected, is_worker_offline, read_worker_status
from panel.forms import FactoryResetConfirmationForm, NodeConfigurationForm
from panel.presenters import DisplayedProgressStep, build_displayed_progress_steps

SETUP_TEMPLATE = "panel/setup.html"
# When the node was never read, the configuration form still needs an upper bound.
FALLBACK_MAXIMUM_TRANSMIT_POWER_DBM = 22
FALLBACK_PATH_HASH_SIZE = 1


class SetupStep:
    START = "start"
    READING = "reading"
    CONFIRM_RESET = "confirm_reset"
    RESETTING = "resetting"
    CONFIGURE = "configure"
    REVIEW = "review"
    CONFIGURING = "configuring"
    DONE = "done"


# The wizard's four stages as the progress indicator shows them.
SETUP_STAGE_LABELS = ("Read the node", "Factory reset", "Configure", "Done")
STAGE_NUMBERS_BY_STEP = {
    SetupStep.START: 1,
    SetupStep.READING: 1,
    SetupStep.CONFIRM_RESET: 2,
    SetupStep.RESETTING: 2,
    SetupStep.CONFIGURE: 3,
    SetupStep.REVIEW: 3,
    SetupStep.CONFIGURING: 3,
    SetupStep.DONE: 4,
}
STEPS_BY_RUN_STATE = {
    NodeSetupRun.State.READING_NODE: SetupStep.READING,
    NodeSetupRun.State.AWAITING_RESET_CONFIRMATION: SetupStep.CONFIRM_RESET,
    NodeSetupRun.State.RESETTING: SetupStep.RESETTING,
    NodeSetupRun.State.AWAITING_CONFIGURATION: SetupStep.CONFIGURE,
    NodeSetupRun.State.CONFIGURING: SetupStep.CONFIGURING,
}


@dataclass(frozen=True, kw_only=True)
class SetupSituation:
    """Everything the wizard shows, read once per request."""

    setup_step: str
    setup_run: NodeSetupRun | None
    latest_command: NodeCommand | None
    displayed_steps: list[DisplayedProgressStep]
    worker_status: WorkerStatus | None
    worker_is_online: bool
    node_is_connected: bool
    node_information: NodeInformation | None
    node_information_error: str
    node_configuration: NodeConfiguration | None
    node_configuration_error: str
    configured_identity_backup: ConfiguredNodeIdentityBackup
    completed_run: NodeSetupRun | None

    @property
    def is_waiting_for_the_worker(self) -> bool:
        """Step 0: the node is to be read, but the worker or the node is not there."""
        return self.setup_step == SetupStep.READING and not self.node_is_connected and not self.read_has_failed

    @property
    def completed_run_kept_the_identity(self) -> bool:
        """The run restored the identity it began to restore, in whichever attempt."""
        if self.completed_run is None or self.node_configuration is None:
            return False
        restored_public_key = self.completed_run.restored_public_key
        return bool(restored_public_key) and restored_public_key == self.node_configuration.node_public_key

    @property
    def node_holds_restored_identity(self) -> bool:
        """An earlier attempt of the active run gave the attached node the relay's identity; setup goes on with it."""
        if self.setup_run is None or not self.setup_run.restored_public_key or self.worker_status is None:
            return False
        return self.node_is_connected and self.worker_status.node_public_key == self.setup_run.restored_public_key

    @property
    def offers_identity_restore(self) -> bool:
        return self.configured_identity_backup.is_restorable and not self.node_holds_restored_identity

    @property
    def read_has_failed(self) -> bool:
        return (
            self.setup_step == SetupStep.READING
            and self.latest_command is not None
            and self.latest_command.state in NodeCommand.TERMINAL_STATES
            and self.latest_command.state != NodeCommand.State.SUCCEEDED
        )

    @property
    def should_poll(self) -> bool:
        if self.setup_run is None:
            return False
        if self.read_has_failed:
            return not self.worker_is_online
        return self.setup_run.state in WORKER_STATES


def read_setup_situation(now: datetime) -> SetupSituation:
    setup_run = get_active_setup_run()
    if (
        setup_run is not None
        and setup_run.node_commands.filter(state=NodeCommand.State.PENDING, expires_at__lte=now).exists()
    ):
        expire_pending_node_commands(now)
        setup_run.refresh_from_db()
        if not setup_run.is_active:
            setup_run = None

    worker_status = read_worker_status()
    latest_command = find_latest_setup_run_command(setup_run) if setup_run else None
    node_information, node_information_error = read_node_information_safely(setup_run)
    node_configuration, node_configuration_error = load_node_configuration_safely()
    return SetupSituation(
        setup_step=choose_setup_step(setup_run),
        setup_run=setup_run,
        latest_command=latest_command,
        displayed_steps=build_displayed_progress_steps(latest_command) if latest_command else [],
        worker_status=worker_status,
        worker_is_online=not is_worker_offline(worker_status, now),
        node_is_connected=is_node_connected(worker_status, now),
        node_information=node_information,
        node_information_error=node_information_error,
        node_configuration=node_configuration,
        node_configuration_error=node_configuration_error,
        configured_identity_backup=read_configured_node_identity_backup(),
        completed_run=get_latest_completed_setup_run() if setup_run is None else None,
    )


def choose_setup_step(setup_run: NodeSetupRun | None) -> str:
    if setup_run is not None:
        return STEPS_BY_RUN_STATE[NodeSetupRun.State(setup_run.state)]
    return SetupStep.DONE if is_node_configured() else SetupStep.START


def read_node_information_safely(setup_run: NodeSetupRun | None) -> tuple[NodeInformation | None, str]:
    if setup_run is None:
        return None, ""
    try:
        return read_original_node_information(setup_run), ""
    except InvalidNodeInformationError as invalid_information_error:
        return None, str(invalid_information_error)


def load_node_configuration_safely() -> tuple[NodeConfiguration | None, str]:
    try:
        return load_node_configuration(), ""
    except IncompleteNodeConfigurationError as incomplete_configuration_error:
        return None, str(incomplete_configuration_error)


def find_node_current_preset(node_information: NodeInformation | None) -> RadioPreset | None:
    if node_information is None:
        return None
    return find_current_radio_preset(
        frequency_kilohertz=node_information.radio_frequency_kilohertz,
        bandwidth_hertz=node_information.radio_bandwidth_hertz,
        spreading_factor=node_information.radio_spreading_factor,
        coding_rate=node_information.radio_coding_rate,
        path_hash_size=node_information.path_hash_size,
    )


def build_configuration_form_initial_values(
    setup_run: NodeSetupRun, node_information: NodeInformation | None, *, offers_identity_restore: bool
) -> dict[str, Any]:
    """The previous request after a failed attempt; otherwise defaults from the node as it was read.

    Keeping the relay's identity is the default whenever it is offered.
    """
    requested_configuration = read_requested_configuration(setup_run)
    if requested_configuration is not None:
        return {
            "node_name": requested_configuration.node_name,
            "radio_preset": requested_configuration.radio_preset_title,
            "frequency_megahertz": format_thousandths(requested_configuration.radio_frequency_kilohertz),
            "bandwidth_kilohertz": format_thousandths(requested_configuration.radio_bandwidth_hertz),
            "spreading_factor": requested_configuration.radio_spreading_factor,
            "coding_rate": requested_configuration.radio_coding_rate,
            "path_hash_size": requested_configuration.path_hash_size,
            "transmit_power_dbm": requested_configuration.transmit_power_dbm,
            "replace_public_channel": requested_configuration.replace_public_channel,
            "restore_identity": offers_identity_restore and requested_configuration.restore_identity,
        }

    current_preset = find_node_current_preset(node_information)
    initial_values: dict[str, Any] = {
        "node_name": DEFAULT_NODE_NAME,
        "radio_preset": current_preset.title if current_preset else MANUAL_RADIO_PRESET_TITLE,
        "transmit_power_dbm": find_maximum_transmit_power(node_information),
        "replace_public_channel": False,
        "restore_identity": offers_identity_restore,
    }
    if node_information is not None:
        initial_values.update(
            {
                "frequency_megahertz": format_thousandths(node_information.radio_frequency_kilohertz),
                "bandwidth_kilohertz": format_thousandths(node_information.radio_bandwidth_hertz),
                "spreading_factor": node_information.radio_spreading_factor,
                "coding_rate": node_information.radio_coding_rate,
            }
        )
    initial_values["path_hash_size"] = choose_default_path_hash_size(current_preset, node_information)
    return initial_values


def choose_default_path_hash_size(radio_preset: RadioPreset | None, node_information: NodeInformation | None) -> int:
    """The preset's suggestion, else the node's current size, else 1."""
    if radio_preset is not None and radio_preset.suggested_path_hash_size is not None:
        return radio_preset.suggested_path_hash_size
    if node_information is not None:
        return node_information.path_hash_size
    return FALLBACK_PATH_HASH_SIZE


def find_maximum_transmit_power(node_information: NodeInformation | None) -> int:
    if node_information is None:
        return FALLBACK_MAXIMUM_TRANSMIT_POWER_DBM
    return node_information.maximum_transmit_power_dbm


def build_configuration_form(
    situation: SetupSituation, form_data: dict[str, Any] | None = None
) -> NodeConfigurationForm:
    current_preset = find_node_current_preset(situation.node_information)
    assert situation.setup_run is not None
    offers_identity_restore = situation.offers_identity_restore
    return NodeConfigurationForm(
        form_data,
        initial=build_configuration_form_initial_values(
            situation.setup_run, situation.node_information, offers_identity_restore=offers_identity_restore
        ),
        maximum_transmit_power_dbm=find_maximum_transmit_power(situation.node_information),
        current_preset_title=current_preset.title if current_preset else None,
        offers_identity_restore=offers_identity_restore,
    )


def build_setup_context(situation: SetupSituation, **extra_context: Any) -> dict[str, Any]:
    setup_step = extra_context.pop("setup_step", situation.setup_step)
    maximum_transmit_power_dbm = find_maximum_transmit_power(situation.node_information)
    context: dict[str, Any] = {
        "situation": situation,
        "setup_step": setup_step,
        "setup_stages": SETUP_STAGE_LABELS,
        "stage_number": STAGE_NUMBERS_BY_STEP[setup_step],
        "transmit_power_help": f"From -9 to {maximum_transmit_power_dbm} dBm, the most this node allows.",
        "setup_run": situation.setup_run,
        "node_information": situation.node_information,
        "current_preset": find_node_current_preset(situation.node_information),
        "configured_identity_backup": situation.configured_identity_backup,
    }
    if situation.setup_run is not None:
        context["expected_confirmation"] = find_expected_factory_reset_confirmation(situation.setup_run)
        context["requested_configuration"] = read_requested_configuration(situation.setup_run)
    if setup_step == SetupStep.CONFIRM_RESET:
        context.setdefault("reset_form", FactoryResetConfirmationForm())
    if setup_step == SetupStep.CONFIGURE and "configuration_form" not in extra_context:
        context["configuration_form"] = build_configuration_form(situation)
    context.update(extra_context)
    return context


def redirect_from_root(request: HttpRequest) -> HttpResponse:
    """The setup wizard while node_setting is empty or a setup run is active, the dashboard otherwise."""
    setup_is_needed = not is_node_configured() or get_active_setup_run() is not None
    return redirect("panel:setup" if setup_is_needed else "panel:node_dashboard")


@require_GET
def show_setup_wizard(request: HttpRequest) -> HttpResponse:
    situation = read_setup_situation(timezone.now())
    return render(request, SETUP_TEMPLATE, build_setup_context(situation))


@require_GET
def show_setup_step(request: HttpRequest) -> HttpResponse:
    situation = read_setup_situation(timezone.now())
    status = 200 if situation.should_poll else HTMX_STOP_POLLING
    return render(request, f"{SETUP_TEMPLATE}#setup_step", build_setup_context(situation), status=status)


@require_GET
def show_radio_fields(request: HttpRequest) -> HttpResponse:
    """The radio fieldset for the chosen preset; manual entry keeps the values the operator already had."""
    situation = read_setup_situation(timezone.now())
    if situation.setup_run is None or situation.setup_step != SetupStep.CONFIGURE:
        return HttpResponse(status=204)

    configuration_form = build_configuration_form(situation)
    preset_title = request.GET.get("radio_preset", MANUAL_RADIO_PRESET_TITLE)
    radio_values = build_radio_values_for_preset(preset_title, request.GET, situation.node_information)
    for field_name, field_value in radio_values.items():
        configuration_form.initial[field_name] = field_value
    return render(
        request,
        f"{SETUP_TEMPLATE}#radio_fields",
        build_setup_context(situation, configuration_form=configuration_form),
    )


def build_radio_values_for_preset(
    preset_title: str, submitted_values: Any, node_information: NodeInformation | None
) -> dict[str, Any]:
    radio_preset = find_radio_preset(preset_title)
    if radio_preset is None:
        return {
            "radio_preset": MANUAL_RADIO_PRESET_TITLE,
            "frequency_megahertz": submitted_values.get("frequency_megahertz", ""),
            "bandwidth_kilohertz": submitted_values.get("bandwidth_kilohertz", ""),
            "spreading_factor": submitted_values.get("spreading_factor", ""),
            "coding_rate": submitted_values.get("coding_rate", ""),
            "path_hash_size": submitted_values.get("path_hash_size", FALLBACK_PATH_HASH_SIZE),
        }
    return {
        "radio_preset": radio_preset.title,
        "frequency_megahertz": format_thousandths(radio_preset.frequency_kilohertz),
        "bandwidth_kilohertz": format_thousandths(radio_preset.bandwidth_hertz),
        "spreading_factor": radio_preset.spreading_factor,
        "coding_rate": radio_preset.coding_rate,
        "path_hash_size": choose_default_path_hash_size(radio_preset, node_information),
    }


@require_POST
def start_setup(request: HttpRequest) -> HttpResponse:
    try:
        start_setup_run(timezone.now())
    except SetupRunTransitionError as transition_error:
        messages.error(request, str(transition_error))
    return redirect("panel:setup")


@require_POST
def retry_reading(request: HttpRequest) -> HttpResponse:
    setup_run = get_active_setup_run()
    if setup_run is None:
        return redirect("panel:setup")
    try:
        retry_reading_node(setup_run.pk, timezone.now())
    except SetupRunTransitionError as transition_error:
        messages.error(request, str(transition_error))
    return redirect("panel:setup")


@require_POST
def confirm_reset(request: HttpRequest) -> HttpResponse:
    situation = read_setup_situation(timezone.now())
    if situation.setup_run is None:
        return redirect("panel:setup")

    reset_form = FactoryResetConfirmationForm(request.POST)
    if reset_form.is_valid():
        try:
            confirm_factory_reset(situation.setup_run.pk, reset_form.cleaned_data["typed_confirmation"], timezone.now())
        except SetupRunTransitionError as transition_error:
            messages.error(request, str(transition_error))
        else:
            messages.success(request, "The factory reset has been sent to the relay worker.")
    else:
        messages.error(request, "Type the node's current name to confirm the factory reset.")
    return redirect("panel:setup")


class ConfigurationStage:
    REVIEW = "review"
    EDIT = "edit"
    APPLY = "apply"


@require_POST
def configure(request: HttpRequest) -> HttpResponse:
    """Review, go back to edit, or apply; only applying changes anything."""
    situation = read_setup_situation(timezone.now())
    if situation.setup_run is None or situation.setup_step != SetupStep.CONFIGURE:
        messages.error(request, "Setup is no longer waiting for the configuration.")
        return redirect("panel:setup")

    configuration_form = build_configuration_form(situation, request.POST)
    configuration_stage = request.POST.get("stage", ConfigurationStage.REVIEW)
    if configuration_stage == ConfigurationStage.EDIT or not configuration_form.is_valid():
        return render(request, SETUP_TEMPLATE, build_setup_context(situation, configuration_form=configuration_form))

    requested_configuration = configuration_form.build_requested_configuration()
    if configuration_stage != ConfigurationStage.APPLY:
        return render_configuration_review(request, situation, configuration_form, requested_configuration)

    try:
        submit_node_configuration(situation.setup_run.pk, requested_configuration, timezone.now())
    except SetupRunTransitionError as transition_error:
        messages.error(request, str(transition_error))
    return redirect("panel:setup")


def render_configuration_review(
    request: HttpRequest,
    situation: SetupSituation,
    configuration_form: NodeConfigurationForm,
    requested_configuration: RequestedNodeConfiguration,
) -> HttpResponse:
    return render(
        request,
        SETUP_TEMPLATE,
        build_setup_context(
            situation,
            setup_step=SetupStep.REVIEW,
            configuration_form=configuration_form,
            reviewed_configuration=requested_configuration,
        ),
    )


@require_POST
def cancel_setup(request: HttpRequest) -> HttpResponse:
    setup_run = get_active_setup_run()
    if setup_run is not None:
        try:
            cancel_setup_run(setup_run.pk, timezone.now())
        except SetupRunTransitionError as transition_error:
            messages.error(request, str(transition_error))
        else:
            messages.success(request, "Setup was cancelled.")
    return redirect("panel:setup")
