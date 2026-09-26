import unicodedata
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from django import forms
from django.core.validators import MaxValueValidator, MinValueValidator

from hoptalk_relay.relay_settings import (
    MAXIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
    MAXIMUM_PAIRING_DURATION_SECONDS,
    MINIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
    MINIMUM_PAIRING_DURATION_SECONDS,
    get_relay_settings,
)
from messaging.models import InboundDirectMessage
from node.contact_cards import CONTACT_NAME_MAXIMUM_BYTES
from node.node_settings import MANUAL_RADIO_PRESET_TITLE
from node.radio_presets import (
    BANDWIDTH_CHOICES_KILOHERTZ,
    FREQUENCY_MAXIMUM_DECIMALS,
    MAXIMUM_CODING_RATE,
    MAXIMUM_FREQUENCY_MEGAHERTZ,
    MAXIMUM_SPREADING_FACTOR,
    MINIMUM_CODING_RATE,
    MINIMUM_FREQUENCY_MEGAHERTZ,
    MINIMUM_SPREADING_FACTOR,
    PATH_HASH_SIZES,
    RadioPreset,
    convert_to_thousandths,
    find_radio_preset,
    load_radio_preset_snapshot,
)
from node.setup_runs import RequestedNodeConfiguration

# The firmware splits names on these characters in some of its commands.
FORBIDDEN_NODE_NAME_CHARACTERS = frozenset("[]\\:,?*")
PATH_HASH_SIZE_CHOICES = [(size, f"{size} byte" if size == 1 else f"{size} bytes") for size in PATH_HASH_SIZES]
MINIMUM_TRANSMIT_POWER_DBM = -9
MANUAL_ENTRY_LABEL = "Manual entry"
MANUAL_RADIO_FIELD_NAMES = ("frequency_megahertz", "bandwidth_kilohertz", "spreading_factor", "coding_rate")
CURRENT_PRESET_MARKER = " (current)"
DEPRECATED_PRESET_MARKER = " — deprecated"


class StyledForm(forms.Form):
    """Gives every widget the panel's input classes, so templates can render fields uniformly."""

    def __init__(self, *arguments: Any, **keyword_arguments: Any) -> None:
        super().__init__(*arguments, **keyword_arguments)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "checkbox")
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "input font-mono text-xs")
            elif not isinstance(field.widget, forms.HiddenInput):
                field.widget.attrs.setdefault("class", "input")


class OperatorLoginForm(forms.Form):
    username = forms.CharField(max_length=150, strip=False, widget=forms.TextInput(attrs={"autocomplete": "username"}))
    password = forms.CharField(
        max_length=1024,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


def build_radio_preset_choices(current_preset_title: str | None) -> list[tuple[str, str]]:
    """Every bundled preset, deprecated ones and the node's current one marked, then manual entry."""
    preset_choices = []
    for radio_preset in load_radio_preset_snapshot().presets:
        label = f"{radio_preset.title} — {radio_preset.description}"
        if radio_preset.is_deprecated:
            label += DEPRECATED_PRESET_MARKER
        if radio_preset.title == current_preset_title:
            label += CURRENT_PRESET_MARKER
        preset_choices.append((radio_preset.title, label))
    preset_choices.append((MANUAL_RADIO_PRESET_TITLE, MANUAL_ENTRY_LABEL))
    return preset_choices


def validate_node_name(node_name: str) -> None:
    encoded_length = len(node_name.encode("utf-8"))
    if not 1 <= encoded_length <= CONTACT_NAME_MAXIMUM_BYTES:
        raise forms.ValidationError(
            f"The name must be 1 to {CONTACT_NAME_MAXIMUM_BYTES} bytes of UTF-8; this one is {encoded_length}."
        )
    if any(unicodedata.category(character) == "Cc" for character in node_name):
        raise forms.ValidationError("The name must not contain control characters.")
    forbidden_characters = sorted(FORBIDDEN_NODE_NAME_CHARACTERS.intersection(node_name))
    if forbidden_characters:
        raise forms.ValidationError(f"The name must not contain {' '.join(forbidden_characters)}.")


def parse_frequency_megahertz(frequency_text: str) -> Decimal:
    try:
        frequency_megahertz = Decimal(frequency_text.strip())
    except InvalidOperation as parsing_error:
        raise forms.ValidationError("Enter the frequency in MHz, such as 916.575.") from parsing_error
    if not frequency_megahertz.is_finite():
        raise forms.ValidationError("Enter the frequency in MHz, such as 916.575.")
    exponent = frequency_megahertz.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -FREQUENCY_MAXIMUM_DECIMALS:
        raise forms.ValidationError(f"Use at most {FREQUENCY_MAXIMUM_DECIMALS} decimals.")
    if not MINIMUM_FREQUENCY_MEGAHERTZ <= frequency_megahertz <= MAXIMUM_FREQUENCY_MEGAHERTZ:
        raise forms.ValidationError(
            f"The frequency must be from {MINIMUM_FREQUENCY_MEGAHERTZ} to {MAXIMUM_FREQUENCY_MEGAHERTZ} MHz."
        )
    return frequency_megahertz


class NodeConfigurationForm(StyledForm):
    """The configuration step of the setup wizard; a bundled preset's radio values replace the typed ones.

    It offers to keep the relay's identity only when a readable backup of it exists; a request to
    keep it without one is an error, never quietly a new identity.
    """

    node_name = forms.CharField(max_length=64, strip=False, label="Node name")
    radio_preset = forms.ChoiceField(label="Radio preset")
    # Required for manual entry only; a preset brings its own radio values.
    frequency_megahertz = forms.CharField(max_length=16, required=False, label="Frequency (MHz)")
    bandwidth_kilohertz = forms.ChoiceField(
        choices=[(bandwidth, f"{bandwidth} kHz") for bandwidth in BANDWIDTH_CHOICES_KILOHERTZ],
        required=False,
        label="Bandwidth",
    )
    spreading_factor = forms.IntegerField(
        min_value=MINIMUM_SPREADING_FACTOR,
        max_value=MAXIMUM_SPREADING_FACTOR,
        required=False,
        label="Spreading factor",
    )
    coding_rate = forms.IntegerField(
        min_value=MINIMUM_CODING_RATE, max_value=MAXIMUM_CODING_RATE, required=False, label="Coding rate"
    )
    path_hash_size = forms.TypedChoiceField(choices=PATH_HASH_SIZE_CHOICES, coerce=int, label="Path hash size")
    transmit_power_dbm = forms.IntegerField(label="Transmit power (dBm)")
    replace_public_channel = forms.BooleanField(required=False, label="Replace the Public channel with a private one")
    restore_identity = forms.BooleanField(required=False, label="Keep the relay's identity")

    def __init__(
        self,
        *arguments: Any,
        maximum_transmit_power_dbm: int,
        current_preset_title: str | None = None,
        offers_identity_restore: bool = False,
        **keyword_arguments: Any,
    ) -> None:
        super().__init__(*arguments, **keyword_arguments)
        self.offers_identity_restore = offers_identity_restore
        self.maximum_transmit_power_dbm = maximum_transmit_power_dbm
        radio_preset_field = self.fields["radio_preset"]
        assert isinstance(radio_preset_field, forms.ChoiceField)
        radio_preset_field.choices = build_radio_preset_choices(current_preset_title)
        # The node's maximum is known only once it has been read, so the range is set here.
        transmit_power_field = self.fields["transmit_power_dbm"]
        transmit_power_field.validators.extend(
            [MinValueValidator(MINIMUM_TRANSMIT_POWER_DBM), MaxValueValidator(maximum_transmit_power_dbm)]
        )
        transmit_power_field.widget.attrs.update({"min": MINIMUM_TRANSMIT_POWER_DBM, "max": maximum_transmit_power_dbm})

    def clean_node_name(self) -> str:
        node_name: str = self.cleaned_data["node_name"]
        validate_node_name(node_name)
        return node_name

    def clean_frequency_megahertz(self) -> Decimal | None:
        if self.find_selected_preset() is not None:
            return None
        return parse_frequency_megahertz(self.cleaned_data["frequency_megahertz"])

    def clean(self) -> dict[str, Any] | None:
        cleaned_data = super().clean()
        if self.cleaned_data.get("restore_identity") and not self.offers_identity_restore:
            self.add_error(None, "No readable backup of the relay's identity exists any more, so it cannot be kept.")
        if "radio_preset" not in self.cleaned_data:
            return cleaned_data

        if self.find_selected_preset() is not None:
            # The preset's own values replace whatever the hidden radio fields held.
            for field_name in MANUAL_RADIO_FIELD_NAMES:
                self.errors.pop(field_name, None)
            return cleaned_data

        for field_name in MANUAL_RADIO_FIELD_NAMES:
            if self.cleaned_data.get(field_name) in (None, "") and field_name not in self.errors:
                self.add_error(field_name, "Required for manual entry.")
        return cleaned_data

    def find_selected_preset(self) -> RadioPreset | None:
        preset_title = self.cleaned_data.get("radio_preset", MANUAL_RADIO_PRESET_TITLE)
        if preset_title == MANUAL_RADIO_PRESET_TITLE:
            return None
        return find_radio_preset(preset_title)

    def build_requested_configuration(self) -> RequestedNodeConfiguration:
        """Only after is_valid(); a preset's values win over whatever the radio fields held."""
        selected_preset = self.find_selected_preset()
        if selected_preset is not None:
            frequency_kilohertz = selected_preset.frequency_kilohertz
            bandwidth_hertz = selected_preset.bandwidth_hertz
            spreading_factor = selected_preset.spreading_factor
            coding_rate = selected_preset.coding_rate
        else:
            frequency_kilohertz = round(self.cleaned_data["frequency_megahertz"] * 1000)
            bandwidth_hertz = convert_to_thousandths(self.cleaned_data["bandwidth_kilohertz"])
            spreading_factor = self.cleaned_data["spreading_factor"]
            coding_rate = self.cleaned_data["coding_rate"]

        return RequestedNodeConfiguration(
            node_name=self.cleaned_data["node_name"],
            radio_preset_title=selected_preset.title if selected_preset else MANUAL_RADIO_PRESET_TITLE,
            radio_frequency_kilohertz=frequency_kilohertz,
            radio_bandwidth_hertz=bandwidth_hertz,
            radio_spreading_factor=spreading_factor,
            radio_coding_rate=coding_rate,
            path_hash_size=self.cleaned_data["path_hash_size"],
            transmit_power_dbm=self.cleaned_data["transmit_power_dbm"],
            replace_public_channel=self.cleaned_data["replace_public_channel"],
            restore_identity=self.cleaned_data["restore_identity"],
        )


class FactoryResetConfirmationForm(StyledForm):
    typed_confirmation = forms.CharField(max_length=64, strip=False, label="Type the node's current name to confirm")


class ContactCardForm(StyledForm):
    card_uri = forms.CharField(
        max_length=2048,
        label="Contact card",
        widget=forms.Textarea(attrs={"rows": 4, "spellcheck": "false", "placeholder": "meshcore://11…"}),
    )


class PairingStartForm(StyledForm):
    duration_seconds = forms.IntegerField(
        min_value=MINIMUM_PAIRING_DURATION_SECONDS,
        max_value=MAXIMUM_PAIRING_DURATION_SECONDS,
        label="Duration (seconds)",
    )
    advert_interval_seconds = forms.IntegerField(
        min_value=MINIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
        max_value=MAXIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
        label="Advert every (seconds)",
    )
    advert_flood = forms.BooleanField(required=False, label="Flood adverts")

    @classmethod
    def build_with_defaults(cls) -> PairingStartForm:
        pairing_settings = get_relay_settings().pairing
        return cls(
            initial={
                "duration_seconds": pairing_settings.default_duration_seconds,
                "advert_interval_seconds": pairing_settings.default_advert_interval_seconds,
                "advert_flood": False,
            }
        )


class UserDeletionForm(StyledForm):
    typed_username = forms.CharField(max_length=16, label="Type the username to confirm")


class MessageStatusFilter(StrEnum):
    ALL = ""
    INCOMPLETE = "incomplete"
    UNDELIVERED = "undelivered"
    DELIVERED = "delivered"
    READ = "read"
    WITH_FAILED_DELIVERY = "failed"


MESSAGE_STATUS_CHOICES = (
    (MessageStatusFilter.ALL, "Any status"),
    (MessageStatusFilter.INCOMPLETE, "Receiving parts"),
    (MessageStatusFilter.UNDELIVERED, "Not delivered yet"),
    (MessageStatusFilter.DELIVERED, "Delivered, not read"),
    (MessageStatusFilter.READ, "Read"),
    (MessageStatusFilter.WITH_FAILED_DELIVERY, "With a failed delivery"),
)


class MessageFilterForm(StyledForm):
    user = forms.CharField(max_length=16, required=False, label="User")
    status = forms.ChoiceField(choices=MESSAGE_STATUS_CHOICES, required=False, label="Status")
    live = forms.BooleanField(required=False, label="Auto-refresh")
    before = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)


class TrafficDirectionFilter(StrEnum):
    BOTH = ""
    INBOUND = "inbound"
    OUTBOUND = "outbound"


TRAFFIC_DIRECTION_CHOICES = (
    (TrafficDirectionFilter.BOTH, "Both directions"),
    (TrafficDirectionFilter.INBOUND, "Received"),
    (TrafficDirectionFilter.OUTBOUND, "Sent"),
)


class TrafficFilterForm(StyledForm):
    contact = forms.TypedChoiceField(coerce=int, required=False, empty_value=None, label="Contact")
    direction = forms.ChoiceField(choices=TRAFFIC_DIRECTION_CHOICES, required=False, label="Direction")
    classification = forms.ChoiceField(required=False, label="Classification")
    only_problems = forms.BooleanField(required=False, label="Only problems")
    live = forms.BooleanField(required=False, label="Live")
    before = forms.CharField(max_length=80, required=False, widget=forms.HiddenInput)

    def __init__(self, *arguments: Any, contact_choices: list[tuple[int, str]], **keyword_arguments: Any) -> None:
        super().__init__(*arguments, **keyword_arguments)
        contact_field = self.fields["contact"]
        assert isinstance(contact_field, forms.TypedChoiceField)
        contact_field.choices = [("", "Every contact"), *contact_choices]
        classification_field = self.fields["classification"]
        assert isinstance(classification_field, forms.ChoiceField)
        classification_field.choices = [("", "Any classification"), *InboundDirectMessage.Classification.choices]
