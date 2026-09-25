"""Tests for node_setup.py, driven by a fake node instead of hardware."""

from __future__ import annotations

import argparse
import asyncio
import builtins
import sys

import pytest

import mcnode
import node_setup
from conftest import open_node
from fake_node import (
    ALICE_KEY,
    BOB_KEY,
    FakeTTY,
    any_line,
    scripted_input,
)

PRESETS = [
    {
        "title": "Australia (Narrow)",
        "description": "916.575MHz / SF7 / BW62.5 / CR7",
        "frequency": "916.575",
        "spreading_factor": "7",
        "bandwidth": "62.5",
        "coding_rate": "7",
    },
    {
        "title": "New Zealand (Narrow)",
        "description": "917.375MHz / SF7 / BW62.5 / CR5 / 2B",
        "frequency": "917.375",
        "spreading_factor": "7",
        "bandwidth": "62.5",
        "coding_rate": "5",
        "network_settings": {"path_hash_size": 2},
    },
]

TWO_CONTACTS = [(ALICE_KEY, "Alice"), (BOB_KEY, "Bob")]


def setup_args(**overrides) -> argparse.Namespace:
    args = {"name": None, "preset": None, "path_hash_size": None}
    args.update(overrides)
    return argparse.Namespace(**args)


@pytest.fixture
def canned_presets(monkeypatch):
    monkeypatch.setattr(
        node_setup, "fetch_presets", lambda: asyncio.sleep(0, result=PRESETS)
    )


@pytest.fixture
def terminal(monkeypatch):
    """Pretend stdin is a terminal, and answer its prompts from a list."""

    def _install(answers):
        monkeypatch.setattr(sys, "stdin", FakeTTY())
        monkeypatch.setattr(builtins, "input", scripted_input(answers))

    return _install


# ── auto-add ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "manual, config, expected",
    [
        (True, 0x00, True),
        (True, 0x01, True),     # overwrite-oldest is not an auto-add gate
        (True, 0x02, False),    # chat still auto-added
        (False, 0x00, False),   # manual off means everything is auto-added
        (True, None, True),     # firmware without the per-type config
        (False, None, False),
    ],
)
def test_autoadd_is_off(manual, config, expected):
    assert node_setup.autoadd_is_off(manual, config) is expected


def test_disable_autoadd_clears_type_bits_and_keeps_the_rest(run_async):
    async def scenario():
        node, mc = await open_node(manual_add=0, autoadd_config=0x1F, autoadd_max_hops=3)
        ok = await node_setup.disable_autoadd(mc)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.manual_add == 1
    assert node.autoadd_config == 0x01     # overwrite-oldest preserved
    assert node.autoadd_max_hops == 3      # one-byte command leaves it alone


def test_disable_autoadd_writes_nothing_when_already_off(run_async):
    async def scenario():
        node, mc = await open_node(manual_add=1, autoadd_config=0x01)
        ok = await node_setup.disable_autoadd(mc)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.commands(0x3A) == []
    assert node.commands(0x26) == []


def test_disable_autoadd_clears_a_type_bit_left_on_in_manual_mode(run_async):
    async def scenario():
        node, mc = await open_node(manual_add=1, autoadd_config=0x03)
        ok = await node_setup.disable_autoadd(mc)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.autoadd_config == 0x01


def test_autoadd_report_names_the_enabled_types(run_async, capsys):
    async def scenario():
        node, mc = await open_node(manual_add=1, autoadd_config=0x06)
        manual, config, max_hops = await node_setup.read_autoadd(mc)
        node_setup.report_autoadd(manual, config, max_hops)
        await mc.disconnect()

    run_async(scenario())
    err = capsys.readouterr().err
    assert "chat" in err and "repeater" in err


def test_autoadd_report_handles_firmware_without_the_config(run_async, capsys):
    async def scenario():
        node, mc = await open_node(manual_add=1, autoadd_supported=False)
        manual, config, max_hops = await node_setup.read_autoadd(mc)
        node_setup.report_autoadd(manual, config, max_hops)
        await mc.disconnect()
        return config

    config = run_async(scenario())
    assert config is None
    assert any_line(capsys.readouterr().err, "not supported by this firmware")


# ── contact card and wiping ──────────────────────────────────────────────
def test_show_card_prints_a_meshcore_uri(run_async, capsys):
    async def scenario():
        node, mc = await open_node(name="relay")
        ok = await node_setup.show_card(mc)
        await mc.disconnect()
        return ok

    assert run_async(scenario()) is True
    assert "meshcore://" in capsys.readouterr().out


def test_wipe_contacts_deletes_everything(run_async, capsys):
    async def scenario():
        node, mc = await open_node(contacts=list(TWO_CONTACTS))
        ok = await node_setup.wipe_contacts(mc, assume_yes=True)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.contacts == []
    assert any_line(capsys.readouterr().err, "Deleted 2 of 2")


def test_wipe_contacts_on_an_empty_node_is_fine(run_async, capsys):
    async def scenario():
        node, mc = await open_node()
        ok = await node_setup.wipe_contacts(mc, assume_yes=True)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.commands(0x0F) == []
    assert any_line(capsys.readouterr().err, "No contacts stored")


def test_wipe_contacts_refuses_without_a_terminal(run_async, capsys):
    async def scenario():
        node, mc = await open_node(contacts=list(TWO_CONTACTS))
        ok = await node_setup.wipe_contacts(mc, assume_yes=False)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is False
    assert len(node.contacts) == 2
    assert any_line(capsys.readouterr().err, "Refusing to delete")


# ── factory reset ────────────────────────────────────────────────────────
RESET_FRAME = bytes.fromhex("337265736574")  # 0x33 + "reset"


def test_factory_reset_sends_the_payload_the_firmware_expects(run_async, capsys):
    async def scenario():
        node, mc = await open_node(reset_mode="ok")
        ok = await node_setup.factory_reset(mc, assume_yes=True, firmware={"fw ver": 13})
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.commands(0x33) == [RESET_FRAME]
    assert any_line(capsys.readouterr().err, "accepted the reset")


def test_factory_reset_treats_silence_as_success(run_async, capsys):
    """Current firmware closes the serial link before it can answer."""

    async def scenario():
        node, mc = await open_node(reset_mode="silent")
        ok = await node_setup.factory_reset(mc, assume_yes=True, firmware={"fw ver": 13})
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.commands(0x33) == [RESET_FRAME]
    assert any_line(capsys.readouterr().err, "No confirmation frame")


def test_factory_reset_fails_on_an_error_code(run_async, capsys):
    async def scenario():
        node, mc = await open_node(reset_mode="error")
        ok = await node_setup.factory_reset(mc, assume_yes=True, firmware={"fw ver": 13})
        await mc.disconnect()
        return ok

    assert run_async(scenario()) is False
    assert any_line(capsys.readouterr().err, "rejected the reset")


def test_factory_reset_refuses_without_a_terminal(run_async):
    async def scenario():
        node, mc = await open_node()
        ok = await node_setup.factory_reset(mc, assume_yes=False)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is False
    assert node.commands(0x33) == []


def test_factory_reset_is_cancelled_when_the_name_does_not_match(run_async, terminal):
    terminal(["something-else"])

    async def scenario():
        node, mc = await open_node(name="relay")
        ok = await node_setup.factory_reset(mc, assume_yes=False)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is False
    assert node.commands(0x33) == []


def test_factory_reset_proceeds_when_the_name_matches(run_async, terminal):
    terminal(["relay"])

    async def scenario():
        node, mc = await open_node(name="relay")
        ok = await node_setup.factory_reset(mc, assume_yes=False, firmware={"fw ver": 13})
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.commands(0x33) == [RESET_FRAME]


@pytest.mark.parametrize("firmware", [None, {"fw ver": 15}])
def test_factory_reset_warns_on_an_unverified_protocol(run_async, capsys, firmware):
    async def scenario():
        node, mc = await open_node()
        await node_setup.factory_reset(mc, assume_yes=True, firmware=firmware)
        await mc.disconnect()

    run_async(scenario())
    assert any_line(capsys.readouterr().err, "not the one the reset frame was verified")


def test_factory_reset_on_the_tested_protocol_does_not_warn(run_async, capsys):
    async def scenario():
        node, mc = await open_node()
        await node_setup.factory_reset(mc, assume_yes=True, firmware={"fw ver": 13})
        await mc.disconnect()

    run_async(scenario())
    assert not any_line(capsys.readouterr().err, "not the one the reset frame was verified")


# ── setup ────────────────────────────────────────────────────────────────
def test_setup_takes_every_answer_from_flags(run_async, canned_presets):
    async def scenario():
        node, mc = await open_node(name="My Node 5566")
        ok = await node_setup.setup_node(
            mc,
            setup_args(
                name="hoptalk-relay", preset="New Zealand (Narrow)", path_hash_size=3
            ),
        )
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.name == "hoptalk-relay"
    assert (node.freq, node.bw, node.sf, node.cr) == (917.375, 62.5, 7, 5)
    assert node.path_hash_mode == 2          # 3 bytes on the wire is mode 2
    assert node.multi_acks == node_setup.DIRECT_MSG_ACKS


def test_setup_interactive_defaults_path_hash_to_the_preset(run_async, canned_presets, terminal):
    terminal(["relay-1", "2", ""])            # name, preset number, accept default

    async def scenario():
        node, mc = await open_node(name="My Node 5566")
        ok = await node_setup.setup_node(mc, setup_args())
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.name == "relay-1"
    assert node.path_hash_mode == 1           # the preset asks for 2 bytes
    assert node.multi_acks == node_setup.DIRECT_MSG_ACKS


def test_setup_accepts_hand_entered_radio_values(run_async, canned_presets, terminal):
    terminal(["", "0", "868.5", "125", "9", "6", "1"])

    async def scenario():
        node, mc = await open_node(name="My Node 5566")
        ok = await node_setup.setup_node(mc, setup_args())
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert node.name == "My Node 5566"        # empty answer keeps the current name
    assert (node.freq, node.bw, node.sf, node.cr) == (868.5, 125.0, 9, 6)
    assert node.path_hash_mode == 0


def test_setup_stops_on_an_unknown_preset(run_async, canned_presets):
    async def scenario():
        node, mc = await open_node()
        ok = await node_setup.setup_node(mc, setup_args(name="x", preset="Atlantis"))
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is False
    assert node.commands(0x0B) == []


def test_setup_needs_a_terminal_when_flags_are_missing(run_async, canned_presets):
    async def scenario():
        node, mc = await open_node()
        ok = await node_setup.setup_node(mc, setup_args())
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is False
    assert node.commands(0x08) == []


def test_setup_and_disable_autoadd_do_not_undo_each_other(run_async, canned_presets):
    """Both write SET_OTHER_PARAMS, so each has to preserve the other's field."""

    async def scenario():
        node, mc = await open_node(manual_add=0)
        ok_setup = await node_setup.setup_node(
            mc, setup_args(name="relay", preset="Australia (Narrow)", path_hash_size=1)
        )
        ok_autoadd = await node_setup.disable_autoadd(mc)
        await mc.disconnect()
        return node, ok_setup, ok_autoadd

    node, ok_setup, ok_autoadd = run_async(scenario())
    assert (ok_setup, ok_autoadd) == (True, True)
    assert node.multi_acks == node_setup.DIRECT_MSG_ACKS
    assert node.manual_add == 1


def test_fetch_presets_from_the_live_api(run_async):
    """The preset list the MeshCore clients use; skipped when offline."""
    entries = run_async(node_setup.fetch_presets())
    if not entries:
        pytest.skip("preset API not reachable")
    assert all("frequency" in entry for entry in entries)
    assert any("Australia" in entry["title"] for entry in entries)


# ── reboot ───────────────────────────────────────────────────────────────
PORT = "/dev/cu.usbmodem2101"


def test_wait_for_port_returns_when_it_comes_back(run_async, monkeypatch):
    from fake_node import port_schedule

    monkeypatch.setattr(
        node_setup, "candidate_ports", port_schedule([[PORT], [], [], [PORT]])
    )
    assert run_async(node_setup.wait_for_port(PORT, timeout=10)) == PORT


def test_wait_for_port_gives_up(run_async, monkeypatch):
    from fake_node import port_schedule

    monkeypatch.setattr(node_setup, "candidate_ports", port_schedule([[PORT], []]))
    assert run_async(node_setup.wait_for_port(PORT, timeout=3)) is None


def test_wait_for_port_accepts_a_renamed_port(run_async, monkeypatch, capsys):
    from fake_node import port_schedule

    renamed = "/dev/cu.usbmodem1101"
    monkeypatch.setattr(
        node_setup, "candidate_ports", port_schedule([[PORT], [], [renamed]])
    )
    assert run_async(node_setup.wait_for_port(PORT, timeout=10)) == renamed
    assert any_line(capsys.readouterr().err, "came back as")


def test_reboot_node_sends_the_frame_and_reopens(run_async, monkeypatch):
    from fake_node import port_schedule

    monkeypatch.setattr(
        node_setup, "candidate_ports", port_schedule([[PORT], [], [PORT]])
    )

    async def scenario():
        node, mc = await open_node()
        reopened_node, reopened = await open_node()

        async def fake_connect(args, *, port=None, **kwargs):
            return reopened

        monkeypatch.setattr(node_setup, "connect", fake_connect)
        result = await node_setup.reboot_node(mc, None, PORT)
        await reopened.disconnect()
        return node, result is reopened

    node, reconnected = run_async(scenario())
    assert reconnected is True
    assert node.commands(0x13) == [bytes.fromhex("137265626f6f74")]  # 0x13 + "reboot"


def test_reboot_node_reports_a_node_that_never_returns(run_async, monkeypatch):
    from fake_node import port_schedule

    monkeypatch.setattr(node_setup, "candidate_ports", port_schedule([[PORT], []]))

    async def scenario():
        node, mc = await open_node()
        result = await node_setup.reboot_node(mc, None, PORT)
        return result

    assert run_async(scenario()) is None


# ── firmware guard ───────────────────────────────────────────────────────
def test_check_firmware_is_quiet_on_the_tested_protocol(run_async, capsys):
    async def scenario():
        node, mc = await open_node(protocol=mcnode.TESTED_PROTOCOL)
        info = await mcnode.check_firmware(mc)
        await mc.disconnect()
        return info

    info = run_async(scenario())
    err = capsys.readouterr().err
    assert info["fw ver"] == mcnode.TESTED_PROTOCOL
    assert "Firmware: v1.17.1-d929643" in err
    assert "!" not in err


@pytest.mark.parametrize(
    "protocol, missing",
    [
        (9, ["default path hash size"]),
        (6, ["direct message acks", "default path hash size"]),
    ],
)
def test_check_firmware_names_what_an_older_node_lacks(run_async, capsys, protocol, missing):
    async def scenario():
        node, mc = await open_node(protocol=protocol)
        await mcnode.check_firmware(mc)
        await mc.disconnect()

    run_async(scenario())
    err = capsys.readouterr().err
    assert "older than" in err
    for feature in missing:
        assert feature in err


def test_check_firmware_warns_about_a_newer_node(run_async, capsys):
    async def scenario():
        node, mc = await open_node(protocol=15)
        await mcnode.check_firmware(mc)
        await mc.disconnect()

    run_async(scenario())
    err = capsys.readouterr().err
    assert "newer than" in err
    assert "factory-reset payload" in err


def test_check_firmware_handles_a_node_that_does_not_answer(run_async, capsys):
    async def scenario():
        node, mc = await open_node(answers_device_query=False)
        info = await mcnode.check_firmware(mc)
        await mc.disconnect()
        return info

    assert run_async(scenario()) is None
    assert any_line(capsys.readouterr().err, "firmware version is unknown")


# ── path discovery ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "path_hex, hop_bytes, expected",
    [
        ("", 1, "no hops — a direct neighbour"),
        ("1122", 1, "11 -> 22"),
        ("11223344", 2, "1122 -> 3344"),
    ],
)
def test_format_path(path_hex, hop_bytes, expected):
    assert node_setup.format_path(path_hex, hop_bytes) == expected


def test_discover_path_reports_both_directions(run_async, capsys):
    async def scenario():
        node, mc = await open_node(
            contacts=[(ALICE_KEY, "Alice", b"")],
            discovery_out=b"\x11\x22",
            discovery_in=b"\x33",
        )
        ok = await node_setup.discover_path(mc, "Alice", timeout=5)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is True
    assert len(node.commands(0x34)) == 1
    err = capsys.readouterr().err
    assert any_line(err, "us -> them : 2 hop(s)   11 -> 22")
    assert any_line(err, "them -> us : 1 hop(s)   33")
    assert any_line(err, "not routing this symmetrically")


def test_discover_path_reports_a_symmetric_route_without_the_warning(run_async, capsys):
    async def scenario():
        node, mc = await open_node(
            contacts=[(ALICE_KEY, "Alice")], discovery_out=b"\x11", discovery_in=b"\x33"
        )
        ok = await node_setup.discover_path(mc, "Alice", timeout=5)
        await mc.disconnect()
        return ok

    assert run_async(scenario()) is True
    err = capsys.readouterr().err
    assert any_line(err, "no route, so it floods")
    assert not any_line(err, "not routing this symmetrically")


def test_discover_path_reports_silence(run_async, capsys):
    """No answer means one of the two legs failed — that is the measurement."""

    async def scenario():
        node, mc = await open_node(
            contacts=[(ALICE_KEY, "Alice")], discovery_mode="silent"
        )
        ok = await node_setup.discover_path(mc, "Alice", timeout=1.5)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is False
    assert len(node.commands(0x34)) == 1
    assert any_line(capsys.readouterr().err, "did not make it")


def test_discover_path_needs_a_known_contact(run_async, capsys):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        ok = await node_setup.discover_path(mc, "Nobody", timeout=5)
        await mc.disconnect()
        return node, ok

    node, ok = run_async(scenario())
    assert ok is False
    assert node.commands(0x34) == []
    assert any_line(capsys.readouterr().err, "No contact matching 'Nobody'")


@pytest.mark.parametrize("wanted", ["alice", "ali", "1122334455"])
def test_find_contact_by_name_or_key_prefix(run_async, wanted):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice"), (BOB_KEY, "Bob")])
        found = await node_setup.find_contact(mc, wanted)
        await mc.disconnect()
        return found

    found = run_async(scenario())
    assert found is not None and found["adv_name"] == "Alice"


def test_find_contact_rejects_an_ambiguous_name(run_async, capsys):
    async def scenario():
        node, mc = await open_node(
            contacts=[(ALICE_KEY, "Pager one"), (BOB_KEY, "Pager two")]
        )
        found = await node_setup.find_contact(mc, "pager")
        await mc.disconnect()
        return found

    assert run_async(scenario()) is None
    assert any_line(capsys.readouterr().err, "matches several contacts")
