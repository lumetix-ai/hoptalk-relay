import pytest

from node.models import NodeSetting
from node.node_settings import (
    IncompleteNodeConfigurationError,
    NodeSettingKey,
    load_node_configuration,
    read_node_setting_value,
    replace_node_configuration,
    update_node_setting,
)
from tests.services.node.node_builders import build_node_configuration

pytestmark = pytest.mark.django_db


def test_an_empty_table_means_the_node_needs_its_initial_setup() -> None:
    assert load_node_configuration() is None
    assert read_node_setting_value(NodeSettingKey.NODE_PUBLIC_KEY) == ""


def test_a_replaced_configuration_is_stored_under_every_key_and_loads_back_typed() -> None:
    node_configuration = build_node_configuration()

    replace_node_configuration(node_configuration)

    assert set(NodeSetting.objects.values_list("key", flat=True)) == {key.value for key in NodeSettingKey}
    assert NodeSetting.objects.get(key="radio.client_repeat").value == "0"
    assert NodeSetting.objects.get(key="contacts.manual_add").value == "1"
    assert load_node_configuration() == node_configuration


def test_a_new_configuration_replaces_the_old_one_entirely() -> None:
    replace_node_configuration(build_node_configuration(public_key="aa" * 32, setup_run_id=1))
    replace_node_configuration(build_node_configuration(public_key="bb" * 32, setup_run_id=2))

    loaded_configuration = load_node_configuration()
    assert loaded_configuration is not None
    assert loaded_configuration.node_public_key == "bb" * 32
    assert loaded_configuration.setup_run_id == 2
    assert NodeSetting.objects.count() == len(NodeSettingKey)


def test_a_failed_replacement_leaves_the_old_configuration_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    old_configuration = build_node_configuration(public_key="aa" * 32)
    replace_node_configuration(old_configuration)

    def fail_to_insert(*arguments: object, **keyword_arguments: object) -> None:
        raise RuntimeError("the insert failed")

    monkeypatch.setattr(NodeSetting.objects, "bulk_create", fail_to_insert)
    with pytest.raises(RuntimeError):
        replace_node_configuration(build_node_configuration(public_key="bb" * 32))

    assert load_node_configuration() == old_configuration


def test_a_partial_table_is_reported_with_the_missing_keys() -> None:
    replace_node_configuration(build_node_configuration())
    NodeSetting.objects.filter(key__in=["radio.coding_rate", "setup.run_id"]).delete()

    with pytest.raises(IncompleteNodeConfigurationError, match=r"radio\.coding_rate, setup\.run_id missing"):
        load_node_configuration()


def test_a_value_that_does_not_convert_is_reported_as_unusable() -> None:
    replace_node_configuration(build_node_configuration())
    NodeSetting.objects.filter(key="radio.spreading_factor").update(value="seven")

    with pytest.raises(IncompleteNodeConfigurationError, match=r"radio\.spreading_factor must be a whole number"):
        load_node_configuration()


def test_the_contact_card_and_firmware_details_may_change_after_setup() -> None:
    replace_node_configuration(build_node_configuration())

    update_node_setting(NodeSettingKey.NODE_CONTACT_CARD_URI, "meshcore://11ab")
    update_node_setting(NodeSettingKey.NODE_FIRMWARE_VERSION, "v1.18.0")

    loaded_configuration = load_node_configuration()
    assert loaded_configuration is not None
    assert loaded_configuration.node_contact_card_uri == "meshcore://11ab"
    assert loaded_configuration.node_firmware_version == "v1.18.0"


def test_other_keys_change_only_through_a_new_setup_run() -> None:
    replace_node_configuration(build_node_configuration())

    with pytest.raises(ValueError, match="only through a new setup run"):
        update_node_setting(NodeSettingKey.NODE_PUBLIC_KEY, "cc" * 32)


def test_an_update_before_the_first_setup_is_refused_so_the_table_never_becomes_partial() -> None:
    with pytest.raises(ValueError, match="before the node has been set up"):
        update_node_setting(NodeSettingKey.NODE_CONTACT_CARD_URI, "meshcore://11ab")

    assert not NodeSetting.objects.exists()
