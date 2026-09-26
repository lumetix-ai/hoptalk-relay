"""Refreshing the bundled radio presets from the MeshCore API, on request only.

`make update-radio-presets` runs this module. It is the only code in the project that goes to
the Internet, and the snapshot it writes is committed and bundled, so the relay itself stays
off-grid. A response that does not look like what the MeshCore clients read is refused rather
than guessed at, and the snapshot is rewritten only when the presets themselves changed, so a
run without news leaves nothing to commit.
"""

import hashlib
import json
import os
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

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
    RADIO_PRESETS_FILE_PATH,
)

RADIO_PRESETS_SOURCE_URL = "https://api.meshcore.nz/api/v1/config"
RADIO_PRESETS_SOURCE_PATH = ("config", "suggested_radio_settings", "entries")
DOWNLOAD_TIMEOUT_SECONDS = 20
# The fields the MeshCore clients read from every preset, all of them JSON strings.
REQUIRED_TEXT_FIELD_NAMES = ("title", "description", "frequency", "bandwidth", "spreading_factor", "coding_rate")


class InvalidRadioPresetResponseError(ValueError):
    pass


@dataclass(frozen=True, kw_only=True)
class RadioPresetChanges:
    added_titles: tuple[str, ...]
    removed_titles: tuple[str, ...]
    changed_titles: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class RadioPresetUpdateOutcome:
    snapshot_was_rewritten: bool
    preset_count: int
    changes: RadioPresetChanges


def download_radio_preset_configuration() -> bytes:
    request = urllib.request.Request(
        RADIO_PRESETS_SOURCE_URL,
        headers={"User-Agent": "hoptalk-relay", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        response_body: bytes = response.read()
        return response_body


def extract_radio_preset_entries(response_body: bytes) -> list[dict[str, Any]]:
    try:
        configuration = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as decoding_error:
        raise InvalidRadioPresetResponseError("The response is not JSON.") from decoding_error

    source_path_text = ".".join(RADIO_PRESETS_SOURCE_PATH)
    entries = configuration
    for key in RADIO_PRESETS_SOURCE_PATH:
        if not isinstance(entries, dict) or key not in entries:
            raise InvalidRadioPresetResponseError(f"The response has no {source_path_text}.")
        entries = entries[key]
    if not isinstance(entries, list) or not entries:
        raise InvalidRadioPresetResponseError(f"{source_path_text} is not a list of presets.")

    for entry in entries:
        validate_radio_preset_entry(entry)
    ensure_preset_titles_are_unique(entries)
    return entries


def validate_radio_preset_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise InvalidRadioPresetResponseError("A preset is not a JSON object.")
    preset_label = describe_preset(entry)
    for field_name in REQUIRED_TEXT_FIELD_NAMES:
        field_value = entry.get(field_name)
        if not isinstance(field_value, str) or not field_value.strip():
            raise InvalidRadioPresetResponseError(f"{preset_label} has no text field {field_name!r}.")

    validate_frequency(entry["frequency"], preset_label)
    if entry["bandwidth"] not in BANDWIDTH_CHOICES_KILOHERTZ:
        raise InvalidRadioPresetResponseError(
            f"{preset_label} has the bandwidth {entry['bandwidth']!r} kHz, which the setup wizard does not offer."
        )
    validate_integer_text(
        entry["spreading_factor"], "spreading factor", MINIMUM_SPREADING_FACTOR, MAXIMUM_SPREADING_FACTOR, preset_label
    )
    validate_integer_text(entry["coding_rate"], "coding rate", MINIMUM_CODING_RATE, MAXIMUM_CODING_RATE, preset_label)
    validate_network_settings(entry.get("network_settings"), preset_label)


def describe_preset(entry: dict[str, Any]) -> str:
    title = entry.get("title")
    return f"The preset {title!r}" if isinstance(title, str) else "A preset without a title"


def validate_frequency(frequency_text: str, preset_label: str) -> None:
    try:
        frequency_megahertz = Decimal(frequency_text)
    except InvalidOperation as parsing_error:
        raise InvalidRadioPresetResponseError(
            f"{preset_label} has the frequency {frequency_text!r}, which is not a number."
        ) from parsing_error
    exponent = frequency_megahertz.as_tuple().exponent
    has_too_many_decimals = isinstance(exponent, int) and exponent < -FREQUENCY_MAXIMUM_DECIMALS
    is_outside_the_firmware_range = not (
        frequency_megahertz.is_finite()
        and MINIMUM_FREQUENCY_MEGAHERTZ <= frequency_megahertz <= MAXIMUM_FREQUENCY_MEGAHERTZ
    )
    if has_too_many_decimals or is_outside_the_firmware_range:
        raise InvalidRadioPresetResponseError(
            f"{preset_label} has the frequency {frequency_text!r} MHz; the firmware takes "
            f"{MINIMUM_FREQUENCY_MEGAHERTZ} to {MAXIMUM_FREQUENCY_MEGAHERTZ} MHz with at most "
            f"{FREQUENCY_MAXIMUM_DECIMALS} decimals."
        )


def validate_integer_text(
    value_text: str, value_name: str, minimum_value: int, maximum_value: int, preset_label: str
) -> None:
    if not value_text.isdecimal() or not minimum_value <= int(value_text) <= maximum_value:
        raise InvalidRadioPresetResponseError(
            f"{preset_label} has the {value_name} {value_text!r}; "
            f"the firmware takes {minimum_value} to {maximum_value}."
        )


def validate_network_settings(network_settings: Any, preset_label: str) -> None:
    if network_settings is None:
        return
    if not isinstance(network_settings, dict):
        raise InvalidRadioPresetResponseError(f"{preset_label} has network settings that are not a JSON object.")
    if "path_hash_size" not in network_settings:
        return
    path_hash_size = network_settings["path_hash_size"]
    # bool is a subclass of int, and JSON true must not pass for a size of 1.
    is_integer = isinstance(path_hash_size, int) and not isinstance(path_hash_size, bool)
    if not is_integer or path_hash_size not in PATH_HASH_SIZES:
        raise InvalidRadioPresetResponseError(
            f"{preset_label} suggests the path hash size {path_hash_size!r}; the firmware takes "
            f"{', '.join(str(size) for size in PATH_HASH_SIZES)} bytes."
        )


def ensure_preset_titles_are_unique(entries: list[dict[str, Any]]) -> None:
    seen_titles: set[str] = set()
    for entry in entries:
        if entry["title"] in seen_titles:
            raise InvalidRadioPresetResponseError(
                f"The title {entry['title']!r} appears twice; presets are found by title."
            )
        seen_titles.add(entry["title"])


def serialize_radio_preset_entries(entries: list[dict[str, Any]]) -> str:
    return json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def calculate_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def build_radio_preset_snapshot_file_content(
    entries: list[dict[str, Any]], response_sha256: str, fetched_at: datetime
) -> str:
    serialized_entries = serialize_radio_preset_entries(entries)
    snapshot = {
        "source_url": RADIO_PRESETS_SOURCE_URL,
        "source_path": ".".join(RADIO_PRESETS_SOURCE_PATH),
        "fetched_at": fetched_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "response_sha256": response_sha256,
        "entries_sha256": calculate_sha256(serialized_entries.encode("utf-8")),
        # Read back from the serialised text, so the keys of every preset are stored sorted too.
        "entries": json.loads(serialized_entries),
    }
    return json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"


def describe_radio_preset_changes(
    previous_entries: list[dict[str, Any]], new_entries: list[dict[str, Any]]
) -> RadioPresetChanges:
    previous_entries_by_title = {entry["title"]: entry for entry in previous_entries}
    new_entries_by_title = {entry["title"]: entry for entry in new_entries}
    common_titles = previous_entries_by_title.keys() & new_entries_by_title.keys()
    return RadioPresetChanges(
        added_titles=tuple(title for title in new_entries_by_title if title not in previous_entries_by_title),
        removed_titles=tuple(title for title in previous_entries_by_title if title not in new_entries_by_title),
        changed_titles=tuple(
            title
            for title in new_entries_by_title
            if title in common_titles and new_entries_by_title[title] != previous_entries_by_title[title]
        ),
    )


def update_radio_preset_snapshot(
    *,
    now: datetime,
    download_configuration: Callable[[], bytes] = download_radio_preset_configuration,
    snapshot_file_path: Path = RADIO_PRESETS_FILE_PATH,
) -> RadioPresetUpdateOutcome:
    """Download, validate and rewrite the snapshot; nothing is written when a step fails or nothing changed."""
    response_body = download_configuration()
    new_entries = extract_radio_preset_entries(response_body)
    previous_snapshot = json.loads(snapshot_file_path.read_text(encoding="utf-8"))
    changes = describe_radio_preset_changes(previous_snapshot["entries"], new_entries)

    new_entries_sha256 = calculate_sha256(serialize_radio_preset_entries(new_entries).encode("utf-8"))
    snapshot_is_current = new_entries_sha256 == previous_snapshot["entries_sha256"]
    if not snapshot_is_current:
        snapshot_file_content = build_radio_preset_snapshot_file_content(
            new_entries, calculate_sha256(response_body), now
        )
        replace_file_content(snapshot_file_path, snapshot_file_content)

    return RadioPresetUpdateOutcome(
        snapshot_was_rewritten=not snapshot_is_current,
        preset_count=len(new_entries),
        changes=changes,
    )


def replace_file_content(file_path: Path, file_content: str) -> None:
    """Write next to the file and rename over it, so an interrupted run never leaves half a snapshot."""
    temporary_file_path = file_path.with_name(file_path.name + ".download")
    temporary_file_path.write_text(file_content, encoding="utf-8")
    os.replace(temporary_file_path, file_path)


def describe_update_outcome(update_outcome: RadioPresetUpdateOutcome) -> str:
    if not update_outcome.snapshot_was_rewritten:
        return f"The bundled radio presets are up to date ({update_outcome.preset_count} presets); nothing was changed."

    changes = update_outcome.changes
    lines = [f"Updated src/node/radio_presets.json: {update_outcome.preset_count} presets."]
    for change_label, titles in (
        ("Added", changes.added_titles),
        ("Removed", changes.removed_titles),
        ("Changed", changes.changed_titles),
    ):
        if titles:
            lines.append(f"  {change_label}: {', '.join(titles)}")
    lines.append(
        "Review it with git diff and commit it. Restart the application (make container-restart) to use it in "
        "development, and rebuild the image (make container-build) for production."
    )
    return "\n".join(lines)


def main() -> int:
    try:
        update_outcome = update_radio_preset_snapshot(now=datetime.now(UTC))
    except OSError as download_error:
        sys.stderr.write(f"Could not download the radio presets from {RADIO_PRESETS_SOURCE_URL}: {download_error}\n")
        return 1
    except InvalidRadioPresetResponseError as validation_error:
        sys.stderr.write(f"The radio presets were not updated: {validation_error}\n")
        return 1
    sys.stdout.write(describe_update_outcome(update_outcome) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
