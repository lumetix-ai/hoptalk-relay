import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from node.radio_preset_updates import (
    InvalidRadioPresetResponseError,
    RadioPresetChanges,
    RadioPresetUpdateOutcome,
    build_radio_preset_snapshot_file_content,
    describe_update_outcome,
    extract_radio_preset_entries,
    main,
    update_radio_preset_snapshot,
)
from node.radio_presets import RADIO_PRESETS_FILE_PATH

UPDATE_TIME = datetime(2026, 10, 1, 8, 30, 15, tzinfo=UTC)


def read_bundled_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = json.loads(RADIO_PRESETS_FILE_PATH.read_text(encoding="utf-8"))
    return snapshot


def build_api_response(entries: list[dict[str, Any]]) -> bytes:
    configuration = {
        "config": {
            "connect_screen": {"info_message": "The default pin is 123456."},
            "suggested_radio_settings": {"info_message": "Suggested by the community.", "entries": entries},
        }
    }
    return json.dumps(configuration).encode("utf-8")


def build_valid_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "title": "Australia (Narrow)",
        "description": "916.575MHz / SF7 / BW62.5 / CR7",
        "frequency": "916.575",
        "bandwidth": "62.5",
        "spreading_factor": "7",
        "coding_rate": "7",
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def snapshot_file_path(tmp_path: Path) -> Path:
    copied_snapshot_file_path = tmp_path / "radio_presets.json"
    copied_snapshot_file_path.write_text(RADIO_PRESETS_FILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return copied_snapshot_file_path


def test_rebuilding_the_bundled_snapshot_from_its_own_entries_reproduces_the_file_byte_for_byte() -> None:
    bundled_snapshot = read_bundled_snapshot()

    rebuilt_file_content = build_radio_preset_snapshot_file_content(
        bundled_snapshot["entries"],
        bundled_snapshot["response_sha256"],
        datetime.fromisoformat(bundled_snapshot["fetched_at"]),
    )

    assert rebuilt_file_content == RADIO_PRESETS_FILE_PATH.read_text(encoding="utf-8")


def test_every_bundled_preset_passes_the_validation_of_a_download() -> None:
    bundled_entries = read_bundled_snapshot()["entries"]

    assert extract_radio_preset_entries(build_api_response(bundled_entries)) == bundled_entries


def test_the_same_presets_leave_the_snapshot_untouched(snapshot_file_path: Path) -> None:
    content_before = snapshot_file_path.read_text(encoding="utf-8")
    bundled_entries = read_bundled_snapshot()["entries"]
    reordered_keys_entries = [dict(reversed(list(entry.items()))) for entry in bundled_entries]

    update_outcome = update_radio_preset_snapshot(
        now=UPDATE_TIME,
        download_configuration=lambda: build_api_response(reordered_keys_entries),
        snapshot_file_path=snapshot_file_path,
    )

    assert update_outcome.snapshot_was_rewritten is False
    assert update_outcome.preset_count == len(bundled_entries)
    assert snapshot_file_path.read_text(encoding="utf-8") == content_before
    assert "up to date" in describe_update_outcome(update_outcome)


def test_new_changed_and_removed_presets_rewrite_the_snapshot_with_new_provenance(snapshot_file_path: Path) -> None:
    new_entries = copy.deepcopy(read_bundled_snapshot()["entries"])
    removed_entry = new_entries.pop()
    new_entries[1]["coding_rate"] = "5"
    new_entries.append(
        build_valid_entry(title="Mars (Narrow)", frequency="433.125", network_settings={"path_hash_size": 2})
    )
    response_body = build_api_response(new_entries)

    update_outcome = update_radio_preset_snapshot(
        now=UPDATE_TIME, download_configuration=lambda: response_body, snapshot_file_path=snapshot_file_path
    )

    rewritten_snapshot = json.loads(snapshot_file_path.read_text(encoding="utf-8"))
    assert update_outcome.snapshot_was_rewritten is True
    assert update_outcome.changes.added_titles == ("Mars (Narrow)",)
    assert update_outcome.changes.removed_titles == (removed_entry["title"],)
    assert update_outcome.changes.changed_titles == (new_entries[1]["title"],)
    assert rewritten_snapshot["fetched_at"] == "2026-10-01T08:30:15Z"
    assert rewritten_snapshot["entries"] == new_entries
    assert rewritten_snapshot["entries_sha256"] != read_bundled_snapshot()["entries_sha256"]
    assert snapshot_file_path.read_text(encoding="utf-8") == build_radio_preset_snapshot_file_content(
        new_entries, rewritten_snapshot["response_sha256"], UPDATE_TIME
    )
    assert not snapshot_file_path.with_name("radio_presets.json.download").exists()


def test_the_outcome_description_lists_what_changed() -> None:
    update_outcome = RadioPresetUpdateOutcome(
        snapshot_was_rewritten=True,
        preset_count=27,
        changes=RadioPresetChanges(added_titles=("Mars",), removed_titles=(), changed_titles=("USA", "Canada")),
    )

    description = describe_update_outcome(update_outcome)

    assert "27 presets" in description
    assert "Added: Mars" in description
    assert "Changed: USA, Canada" in description
    assert "Removed" not in description


@pytest.mark.parametrize(
    ("response_body", "expected_message"),
    [
        (b"<html>maintenance</html>", "not JSON"),
        (json.dumps({"config": {}}).encode(), "no config.suggested_radio_settings.entries"),
        (build_api_response([]), "not a list of presets"),
        (build_api_response(["Australia"]), "not a JSON object"),  # type: ignore[list-item]
    ],
)
def test_a_response_of_another_shape_is_refused(response_body: bytes, expected_message: str) -> None:
    with pytest.raises(InvalidRadioPresetResponseError, match=expected_message):
        extract_radio_preset_entries(response_body)


@pytest.mark.parametrize(
    ("entry_overrides", "expected_message"),
    [
        ({"description": ""}, "no text field 'description'"),
        ({"frequency": 916.575}, "no text field 'frequency'"),
        ({"spreading_factor": 7}, "no text field 'spreading_factor'"),
        ({"frequency": "about 916"}, "not a number"),
        ({"frequency": "916.5751"}, "at most 3 decimals"),
        ({"frequency": "100.000"}, "150.000 to 2500.000 MHz"),
        ({"frequency": "NaN"}, "150.000 to 2500.000 MHz"),
        ({"bandwidth": "200"}, "does not offer"),
        ({"spreading_factor": "13"}, "spreading factor '13'"),
        ({"spreading_factor": "SF7"}, "spreading factor 'SF7'"),
        ({"coding_rate": "4"}, "coding rate '4'"),
        ({"network_settings": [2]}, "not a JSON object"),
        ({"network_settings": {"path_hash_size": 4}}, "path hash size 4"),
        ({"network_settings": {"path_hash_size": True}}, "path hash size True"),
        ({"network_settings": {"path_hash_size": "2"}}, "path hash size '2'"),
    ],
)
def test_a_preset_the_firmware_or_the_wizard_cannot_use_is_refused(
    entry_overrides: dict[str, Any], expected_message: str
) -> None:
    response_body = build_api_response([build_valid_entry(**entry_overrides)])

    with pytest.raises(InvalidRadioPresetResponseError, match=expected_message):
        extract_radio_preset_entries(response_body)


def test_network_settings_without_a_path_hash_size_are_accepted() -> None:
    entry = build_valid_entry(network_settings={"region": "AU"})

    assert extract_radio_preset_entries(build_api_response([entry])) == [entry]


def test_two_presets_with_the_same_title_are_refused() -> None:
    response_body = build_api_response([build_valid_entry(), build_valid_entry(frequency="917.375")])

    with pytest.raises(InvalidRadioPresetResponseError, match="appears twice"):
        extract_radio_preset_entries(response_body)


def test_a_refused_download_leaves_the_snapshot_untouched(snapshot_file_path: Path) -> None:
    content_before = snapshot_file_path.read_text(encoding="utf-8")

    with pytest.raises(InvalidRadioPresetResponseError):
        update_radio_preset_snapshot(
            now=UPDATE_TIME,
            download_configuration=lambda: build_api_response([build_valid_entry(bandwidth="200")]),
            snapshot_file_path=snapshot_file_path,
        )

    assert snapshot_file_path.read_text(encoding="utf-8") == content_before


def test_the_command_reports_an_unreachable_api_and_exits_with_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse_the_connection(*, now: datetime) -> RadioPresetUpdateOutcome:
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr("node.radio_preset_updates.update_radio_preset_snapshot", refuse_the_connection)

    exit_status = main()

    assert exit_status == 1
    assert "Could not download the radio presets from https://api.meshcore.nz" in capsys.readouterr().err


def test_the_command_reports_a_refused_response_and_exits_with_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse_the_response(*, now: datetime) -> RadioPresetUpdateOutcome:
        raise InvalidRadioPresetResponseError("The response is not JSON.")

    monkeypatch.setattr("node.radio_preset_updates.update_radio_preset_snapshot", refuse_the_response)

    exit_status = main()

    assert exit_status == 1
    assert "The radio presets were not updated: The response is not JSON." in capsys.readouterr().err
