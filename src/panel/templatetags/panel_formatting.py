from datetime import datetime, timedelta

import segno
from django import template
from django.utils.safestring import SafeString, mark_safe

from node.radio_presets import format_thousandths
from panel import presenters
from panel.traffic_decoding import shorten_message_id

register = template.Library()

QR_CODE_SCALE = 4
QR_CODE_DARK_COLOUR = "#0f172a"
QR_CODE_LIGHT_COLOUR = "#ffffff"


@register.filter
def parts_mask(parts_mask_value: int, part_count: int) -> str:
    return presenters.format_parts_mask(parts_mask_value, part_count)


@register.filter
def receipt_level(receipt_level_value: int) -> str:
    return presenters.format_receipt_level(receipt_level_value)


@register.filter
def message_id_time(client_message_id: int) -> datetime | None:
    return presenters.convert_message_id_to_time(client_message_id)


@register.filter
def short_message_id(client_message_id: int) -> str:
    return shorten_message_id(client_message_id)


@register.filter
def key_prefix(public_key: str) -> str:
    return presenters.shorten_public_key(public_key)


@register.filter
def contact_route(node_out_path_length: int | None) -> str:
    return presenters.describe_contact_route(node_out_path_length)


@register.filter
def inbound_route(path_length: int) -> str:
    return presenters.describe_inbound_route(path_length)


@register.filter
def badge_tone(state: str) -> str:
    return presenters.find_badge_tone(str(state))


@register.filter
def relay_mode_tone(relay_mode: str) -> str:
    return presenters.find_relay_mode_tone(str(relay_mode))


@register.filter
def humanize_state(state: str) -> str:
    return str(state).replace("_", " ")


@register.filter
def qr_code_svg(contact_card_uri: str) -> SafeString:
    """The card as an inline SVG QR code: no image request, and nothing the content security policy blocks."""
    qr_code = segno.make(contact_card_uri, error="m")
    svg_markup: str = qr_code.svg_inline(
        scale=QR_CODE_SCALE,
        dark=QR_CODE_DARK_COLOUR,
        light=QR_CODE_LIGHT_COLOUR,
        svgclass="size-full",
        omitsize=True,
    )
    # segno writes only path geometry and the colours above; the card text is not part of the markup.
    return mark_safe(svg_markup)


@register.filter
def thousandths(value_in_thousandths: int) -> str:
    return format_thousandths(value_in_thousandths)


@register.filter
def is_in_section(request_path: str, section_path: str) -> bool:
    return request_path == section_path or request_path.startswith(f"{section_path}/")


ALERT_TONES_BY_MESSAGE_LEVEL = {"error": "danger", "warning": "warning", "success": "success", "info": "information"}


@register.filter
def alert_tone(message_tags: str) -> str:
    return next(
        (ALERT_TONES_BY_MESSAGE_LEVEL[tag] for tag in message_tags.split() if tag in ALERT_TONES_BY_MESSAGE_LEVEL),
        "information",
    )


@register.filter
def minutes_and_seconds(total_seconds: int) -> str:
    minutes, seconds = divmod(max(0, int(total_seconds)), 60)
    return f"{minutes}:{seconds:02d}"


@register.filter
def duration(elapsed_time: timedelta) -> str:
    return presenters.format_duration(elapsed_time)
