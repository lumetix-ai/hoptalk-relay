import hashlib
import json
from decimal import Decimal

import pytest

from node.radio_presets import (
    RADIO_PRESETS_FILE_PATH,
    convert_to_thousandths,
    find_current_radio_preset,
    find_radio_preset,
    format_thousandths,
    load_radio_preset_snapshot,
)

CANADA_AND_USA_RADIO = {
    "frequency_kilohertz": 910525,
    "bandwidth_hertz": 62500,
    "spreading_factor": 7,
    "coding_rate": 5,
}
HUNGARY_NETHERLANDS_AND_SLOVAKIA_RADIO = {
    "frequency_kilohertz": 869618,
    "bandwidth_hertz": 62500,
    "spreading_factor": 7,
    "coding_rate": 5,
}


def test_the_bundled_presets_load_with_unique_titles_and_entries_matching_their_recorded_hash() -> None:
    radio_preset_snapshot = load_radio_preset_snapshot()
    snapshot_entries = json.loads(RADIO_PRESETS_FILE_PATH.read_text(encoding="utf-8"))["entries"]
    serialized_entries = json.dumps(snapshot_entries, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    titles = [preset.title for preset in radio_preset_snapshot.presets]
    assert titles
    assert len(set(titles)) == len(titles)
    assert radio_preset_snapshot.source_url == "https://api.meshcore.nz/api/v1/config"
    assert radio_preset_snapshot.entries_sha256 == hashlib.sha256(serialized_entries.encode("utf-8")).hexdigest()


def test_every_radio_value_of_the_snapshot_converts_exactly() -> None:
    snapshot_entries = json.loads(RADIO_PRESETS_FILE_PATH.read_text(encoding="utf-8"))["entries"]

    for snapshot_entry in snapshot_entries:
        for field_name in ("frequency", "bandwidth"):
            exact_thousandths = Decimal(snapshot_entry[field_name]) * 1000
            assert exact_thousandths == exact_thousandths.to_integral_value(), snapshot_entry["title"]
            assert convert_to_thousandths(snapshot_entry[field_name]) == int(exact_thousandths)


def test_values_that_float_arithmetic_would_truncate_convert_exactly() -> None:
    assert int(float("256.001") * 1000) == 256000
    assert convert_to_thousandths("256.001") == 256001
    assert convert_to_thousandths("62.5") == 62500


def test_a_preset_is_found_by_its_title_with_its_values_in_kilohertz_and_hertz() -> None:
    australia_narrow = find_radio_preset("Australia (Narrow)")

    assert australia_narrow is not None
    assert australia_narrow.frequency_kilohertz == 916575
    assert australia_narrow.bandwidth_hertz == 62500
    assert (australia_narrow.spreading_factor, australia_narrow.coding_rate) == (7, 7)
    assert australia_narrow.suggested_path_hash_size is None
    assert find_radio_preset("Atlantis") is None


def test_only_the_titles_marked_deprecated_are_deprecated() -> None:
    deprecated_titles = {preset.title for preset in load_radio_preset_snapshot().presets if preset.is_deprecated}

    assert deprecated_titles == {"EU/UK (Deprecated)", "Vietnam (Deprecated)"}


@pytest.mark.parametrize(
    ("radio_values", "path_hash_size", "expected_title"),
    [
        pytest.param(CANADA_AND_USA_RADIO, 3, "Canada", id="canada by its path hash size"),
        pytest.param(CANADA_AND_USA_RADIO, 1, "USA", id="usa suggests no path hash size"),
        pytest.param(HUNGARY_NETHERLANDS_AND_SLOVAKIA_RADIO, 2, "Hungary", id="first of two exact matches"),
        pytest.param(HUNGARY_NETHERLANDS_AND_SLOVAKIA_RADIO, 1, "Netherlands", id="netherlands suggests none"),
    ],
)
def test_the_current_preset_is_separated_by_the_path_hash_size(
    radio_values: dict[str, int], path_hash_size: int, expected_title: str
) -> None:
    current_preset = find_current_radio_preset(**radio_values, path_hash_size=path_hash_size)

    assert current_preset is not None
    assert current_preset.title == expected_title


def test_a_radio_no_preset_uses_has_no_current_preset() -> None:
    assert (
        find_current_radio_preset(
            frequency_kilohertz=868000, bandwidth_hertz=125000, spreading_factor=9, coding_rate=5, path_hash_size=1
        )
        is None
    )


def test_thousandths_are_written_the_way_the_presets_write_them() -> None:
    assert format_thousandths(916575) == "916.575"
    assert format_thousandths(62500) == "62.5"
    assert format_thousandths(125000) == "125"
    assert format_thousandths(7800) == "7.8"
