"""The community radio presets, bundled so that setup works off-grid.

radio_presets.json is a snapshot of `config.suggested_radio_settings.entries` from
api.meshcore.nz, with the fetch time, the SHA-256 of the whole API response, and the SHA-256
of the entries as serialised by json.dumps(entries, indent=2, sort_keys=True,
ensure_ascii=False) plus a newline. Nothing is fetched at run time; `make update-radio-presets`
refreshes the snapshot on request (radio_preset_updates.py). The radio numbers are strings in
the snapshot and are converted exactly: round(Decimal(value) * 1000).
"""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

RADIO_PRESETS_FILE_PATH = Path(__file__).resolve().parent / "radio_presets.json"
DEPRECATED_TITLE_MARKER = "(Deprecated)"

# The bandwidths LoRa radios support, in kHz as the presets and the wizard write them.
BANDWIDTH_CHOICES_KILOHERTZ = ("7.8", "10.4", "15.6", "20.8", "31.25", "41.7", "62.5", "125", "250", "500")

# The ranges the firmware accepts for its radio parameters; it refuses anything else.
MINIMUM_FREQUENCY_MEGAHERTZ = Decimal("150.000")
MAXIMUM_FREQUENCY_MEGAHERTZ = Decimal("2500.000")
FREQUENCY_MAXIMUM_DECIMALS = 3
MINIMUM_SPREADING_FACTOR = 5
MAXIMUM_SPREADING_FACTOR = 12
MINIMUM_CODING_RATE = 5
MAXIMUM_CODING_RATE = 8
PATH_HASH_SIZES = (1, 2, 3)


@dataclass(frozen=True, kw_only=True)
class RadioPreset:
    title: str
    description: str
    frequency_kilohertz: int
    bandwidth_hertz: int
    spreading_factor: int
    coding_rate: int
    # None when the preset makes no path hash suggestion (it has no network_settings).
    suggested_path_hash_size: int | None
    is_deprecated: bool


@dataclass(frozen=True, kw_only=True)
class RadioPresetSnapshot:
    source_url: str
    fetched_at: datetime
    response_sha256: str
    entries_sha256: str
    presets: tuple[RadioPreset, ...]


def convert_to_thousandths(decimal_text: str) -> int:
    """MHz to kHz, or kHz to Hz, without the float error of int(float(value) * 1000)."""
    return round(Decimal(decimal_text) * 1000)


@cache
def load_radio_preset_snapshot() -> RadioPresetSnapshot:
    snapshot_json = json.loads(RADIO_PRESETS_FILE_PATH.read_text(encoding="utf-8"))
    return RadioPresetSnapshot(
        source_url=snapshot_json["source_url"],
        fetched_at=datetime.fromisoformat(snapshot_json["fetched_at"]),
        response_sha256=snapshot_json["response_sha256"],
        entries_sha256=snapshot_json["entries_sha256"],
        presets=tuple(build_radio_preset(entry) for entry in snapshot_json["entries"]),
    )


def build_radio_preset(entry: dict[str, Any]) -> RadioPreset:
    network_settings = entry.get("network_settings") or {}
    suggested_path_hash_size = network_settings.get("path_hash_size")
    return RadioPreset(
        title=entry["title"],
        description=entry["description"],
        frequency_kilohertz=convert_to_thousandths(entry["frequency"]),
        bandwidth_hertz=convert_to_thousandths(entry["bandwidth"]),
        spreading_factor=int(entry["spreading_factor"]),
        coding_rate=int(entry["coding_rate"]),
        suggested_path_hash_size=int(suggested_path_hash_size) if suggested_path_hash_size is not None else None,
        is_deprecated=DEPRECATED_TITLE_MARKER in entry["title"],
    )


def find_radio_preset(title: str) -> RadioPreset | None:
    return next((preset for preset in load_radio_preset_snapshot().presets if preset.title == title), None)


def find_current_radio_preset(
    frequency_kilohertz: int,
    bandwidth_hertz: int,
    spreading_factor: int,
    coding_rate: int,
    path_hash_size: int,
) -> RadioPreset | None:
    """The preset the node runs now; the path hash size separates presets with equal radio values (Canada, USA).

    A preset that suggests the node's path hash size wins over one that suggests none; one that
    suggests another size never matches.
    """
    presets_with_the_same_radio = [
        preset
        for preset in load_radio_preset_snapshot().presets
        if preset.frequency_kilohertz == frequency_kilohertz
        and preset.bandwidth_hertz == bandwidth_hertz
        and preset.spreading_factor == spreading_factor
        and preset.coding_rate == coding_rate
    ]
    for preset in presets_with_the_same_radio:
        if preset.suggested_path_hash_size == path_hash_size:
            return preset
    for preset in presets_with_the_same_radio:
        if preset.suggested_path_hash_size is None:
            return preset
    return None


def format_thousandths(value_in_thousandths: int) -> str:
    """916575 → "916.575", 62500 → "62.5", 125000 → "125": the way the presets write MHz and kHz."""
    decimal_value = (Decimal(value_in_thousandths) / 1000).normalize()
    return format(decimal_value, "f")
