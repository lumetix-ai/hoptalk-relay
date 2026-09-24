"""Tests for listen_dm.py, driven by a fake node instead of hardware."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

import listen_dm
from conftest import open_node
from fake_node import (
    ALICE_KEY,
    BOB_KEY,
    CARD_KEY,
    PAGER_KEY,
    PUSH_MSG_WAITING,
    STRANGER_KEY,
    advert_push,
    any_line,
    channel_msg_frame,
    dm_frame,
    dm_v3_frame,
    pending_contact_push,
)

EMPTY_SLOT = ("", bytes(16))
TAKEN_SLOT = ("general", b"\x01" * 16)


@contextlib.asynccontextmanager
async def listening(mc, **kwargs):
    """Run a listener for the body of the block, then stop it."""
    listener = listen_dm.DirectMessageListener(mc, **kwargs)
    listener.subscribe()
    task = asyncio.create_task(listener.run())
    await asyncio.sleep(0.2)
    try:
        yield listener
    finally:
        listener.stop()
        await asyncio.wait_for(task, 5)


async def deliver(node, frames, settle: float = 0.6) -> None:
    """Queue messages on the node and tell the script they are waiting."""
    node.pending.extend(frames)
    await node.push(bytearray([PUSH_MSG_WAITING]))
    await asyncio.sleep(settle)


# ── pure helpers ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "msg, expected",
    [
        ({"path_len": 255}, "direct"),
        ({"path_len": 2}, "flood, 2 hops"),
        ({"path_len": 1}, "flood, 1 hop"),
        ({"path_len": 255, "SNR": -7.5}, "SNR -7.5"),
        ({"path_len": 255, "txt_type": 2}, "signed"),
        ({"path_len": 255, "txt_type": 1}, "cli"),
    ],
)
def test_route_info(msg, expected):
    assert expected in listen_dm.route_info(msg)


@pytest.mark.parametrize(
    "contact, patterns, expected",
    [
        ({"adv_name": "Pager2", "public_key": PAGER_KEY.hex()}, ["pager*"], True),
        ({"adv_name": "Randomer", "public_key": STRANGER_KEY.hex()}, ["pager*"], False),
        ({"adv_name": "Pager2", "public_key": PAGER_KEY.hex()}, ["99887766"], True),
        ({"adv_name": "Randomer", "public_key": STRANGER_KEY.hex()}, ["99887766"], False),
        ({"adv_name": "Pager2", "public_key": PAGER_KEY.hex()}, [], False),
        ({"adv_name": "Pager2", "public_key": PAGER_KEY.hex()}, ["Pager2"], True),
    ],
)
def test_contact_matches(contact, patterns, expected):
    assert listen_dm.contact_matches(contact, patterns) is expected


# ── receiving messages ───────────────────────────────────────────────────
def test_dm_from_known_contact_is_printed(run_async, capsys):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        async with listening(mc, reply_text=None):
            await deliver(node, [dm_frame(ALICE_KEY[:6], "hello from the mesh", 1758730000)])
        await mc.disconnect()

    run_async(scenario())
    out = capsys.readouterr().out
    assert "Alice: hello from the mesh" in out
    assert "direct" in out


def test_dm_v3_reports_snr_and_hop_count(run_async, capsys):
    async def scenario():
        node, mc = await open_node(contacts=[(BOB_KEY, "Bob")])
        async with listening(mc, reply_text=None):
            await deliver(node, [dm_v3_frame(BOB_KEY[:6], "hello from Bob", 1758730100)])
        await mc.disconnect()

    run_async(scenario())
    out = capsys.readouterr().out
    assert "Bob: hello from Bob" in out
    assert "flood, 2 hops" in out
    assert "SNR -7.5" in out


def test_signed_message_is_labelled(run_async, capsys):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        async with listening(mc, reply_text=None):
            await deliver(node, [dm_frame(ALICE_KEY[:6], "signed text", 1758730101, txt_type=2)])
        await mc.disconnect()

    run_async(scenario())
    out = capsys.readouterr().out
    assert "Alice: signed text" in out
    assert "signed" in out


def test_unknown_sender_shows_its_key_prefix(run_async, capsys):
    async def scenario():
        node, mc = await open_node()
        async with listening(mc, reply_text=None):
            await deliver(node, [dm_frame(bytes.fromhex("f0f0f0f0f0f0"), "who am i", 0)])
        await mc.disconnect()

    run_async(scenario())
    assert "<f0f0f0f0f0f0>: who am i" in capsys.readouterr().out


def test_contact_added_after_start_is_resolved(run_async, capsys):
    """A first message from a contact the script has not seen re-syncs the list."""

    async def scenario():
        node, mc = await open_node()
        async with listening(mc, reply_text=None):
            node.contacts.append((PAGER_KEY, "Pager2"))
            await deliver(node, [dm_frame(PAGER_KEY[:6], "first contact", 1758730102)])
        await mc.disconnect()

    run_async(scenario())
    assert "Pager2: first contact" in capsys.readouterr().out


# ── gating new contacts ──────────────────────────────────────────────────
def test_pending_contact_is_not_added_without_a_gate(run_async, capsys):
    async def scenario():
        node, mc = await open_node()
        async with listening(mc, show_adverts=True, reply_text=None):
            await node.push(pending_contact_push(PAGER_KEY, "Pager2"))
            await asyncio.sleep(0.5)
        await mc.disconnect()
        return node

    node = run_async(scenario())
    assert node.commands(0x09) == []
    assert any_line(capsys.readouterr().err, "pending, not in contacts")


def test_accept_all_adds_a_pending_contact(run_async):
    async def scenario():
        node, mc = await open_node()
        async with listening(mc, accept_all=True, reply_text=None):
            await node.push(pending_contact_push(PAGER_KEY, "Pager2"))
            await asyncio.sleep(0.6)
        await mc.disconnect()
        return node

    node = run_async(scenario())
    adds = node.commands(0x09)
    assert len(adds) == 1
    assert adds[0][1:33] == PAGER_KEY


def test_accept_pattern_adds_only_the_match(run_async):
    async def scenario():
        node, mc = await open_node()
        async with listening(mc, accept_patterns=["Pager*"], reply_text=None):
            await node.push(pending_contact_push(PAGER_KEY, "Pager2"))
            await asyncio.sleep(0.6)
            await node.push(pending_contact_push(STRANGER_KEY, "Randomer"))
            await asyncio.sleep(0.6)
        await mc.disconnect()
        return node

    node = run_async(scenario())
    adds = node.commands(0x09)
    assert [a[1:33] for a in adds] == [PAGER_KEY]


def test_advert_from_known_node_is_reported(run_async, capsys):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        async with listening(mc, show_adverts=True, reply_text=None):
            await node.push(advert_push(ALICE_KEY))
            await asyncio.sleep(0.5)
        await mc.disconnect()

    run_async(scenario())
    assert any_line(capsys.readouterr().err, "advert heard from Alice")


# ── pairing ──────────────────────────────────────────────────────────────
def test_pairing_adds_only_the_paired_key_then_closes(run_async):
    async def scenario():
        node, mc = await open_node()
        async with listening(
            mc, pair=True, pair_key="99887766", pair_timeout=30, reply_text=None
        ) as listener:
            listener.open_pairing_window()
            await node.push(pending_contact_push(STRANGER_KEY, "Randomer"))
            await asyncio.sleep(0.5)
            await node.push(pending_contact_push(PAGER_KEY, "Pager2"))
            await asyncio.sleep(0.6)
            # the window is closed now, so a later advert must not be added
            await node.push(pending_contact_push(STRANGER_KEY, "Randomer"))
            await asyncio.sleep(0.5)
        await mc.disconnect()
        return node

    node = run_async(scenario())
    adds = node.commands(0x09)
    assert [a[1:33] for a in adds] == [PAGER_KEY]


def test_pairing_window_expires(run_async, capsys):
    async def scenario():
        node, mc = await open_node()
        async with listening(
            mc, pair=True, pair_key="99887766", pair_timeout=1.5, reply_text=None
        ) as listener:
            listener.open_pairing_window()
            await asyncio.sleep(2.5)
            await node.push(pending_contact_push(PAGER_KEY, "Pager2"))
            await asyncio.sleep(0.5)
        await mc.disconnect()
        return node

    node = run_async(scenario())
    assert node.commands(0x09) == []
    assert any_line(capsys.readouterr().err, "Pairing window closed")


def test_import_card_adds_the_contact(run_async, capsys):
    async def scenario():
        node, mc = await open_node()
        card = "meshcore://" + (b"\x01" + CARD_KEY + b"CardPager").hex()
        accepted = await listen_dm.import_card(mc, card)
        await mc.disconnect()
        return node, accepted

    node, accepted = run_async(scenario())
    assert accepted is True
    assert len(node.commands(0x12)) == 1
    assert any_line(capsys.readouterr().err, "imported CardPager")


def test_import_card_rejects_garbage(run_async):
    async def scenario():
        node, mc = await open_node()
        accepted = await listen_dm.import_card(mc, "not-a-card")
        await mc.disconnect()
        return node, accepted

    node, accepted = run_async(scenario())
    assert accepted is False
    assert node.commands(0x12) == []


# ── channels ─────────────────────────────────────────────────────────────
def test_channel_message_is_labelled_with_the_channel_name(run_async, capsys):
    async def scenario():
        node, mc = await open_node()
        async with listening(mc, channels={1: "hoptalk"}, reply_text=None):
            await deliver(node, [channel_msg_frame(1, "Ivan: hi all", 1758730200)])
        await mc.disconnect()

    run_async(scenario())
    assert "#hoptalk: Ivan: hi all" in capsys.readouterr().out


def test_create_channel_uses_the_first_free_slot(run_async):
    async def scenario():
        node, mc = await open_node(
            channels={0: TAKEN_SLOT, 1: EMPTY_SLOT, 2: EMPTY_SLOT}
        )
        slot = await listen_dm.create_channel(mc, "hoptalk")
        await mc.disconnect()
        return node, slot

    node, slot = run_async(scenario())
    assert slot == 1
    assert node.channels[1][0] == "hoptalk"
    assert node.channels[1][1] != bytes(16)      # a real random key
    assert node.channels[0] == TAKEN_SLOT        # untouched


def test_create_channel_refuses_a_hash_derived_name(run_async, capsys):
    async def scenario():
        node, mc = await open_node(channels={0: EMPTY_SLOT})
        slot = await listen_dm.create_channel(mc, "#public")
        await mc.disconnect()
        return node, slot

    node, slot = run_async(scenario())
    assert slot is None
    assert node.commands(0x20) == []
    assert any_line(capsys.readouterr().err, "hash of the name")


def test_channel_table_reads_configured_slots(run_async):
    async def scenario():
        node, mc = await open_node(channels={0: TAKEN_SLOT, 1: EMPTY_SLOT})
        table = await listen_dm.channel_table(mc)
        await mc.disconnect()
        return table

    table = run_async(scenario())
    assert table[0]["channel_name"] == "general"
    assert listen_dm.channel_is_empty(table[1])
    assert not listen_dm.channel_is_empty(table[0])


# ── replying ─────────────────────────────────────────────────────────────
def test_reply_is_sent_and_acked(run_async, capsys):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        async with listening(mc):
            await deliver(node, [dm_frame(ALICE_KEY[:6], "ping", 1758730300)], settle=1.2)
        await mc.disconnect()
        return node

    node = run_async(scenario())
    assert node.sent_texts == ["RECEIVED"]
    assert node.commands(0x02)[0][7:13] == ALICE_KEY[:6]
    assert any_line(capsys.readouterr().err, "-> Alice: RECEIVED (direct, acked)")


def test_reply_without_an_ack_says_so(run_async, capsys):
    async def scenario():
        node, mc = await open_node(
            contacts=[(ALICE_KEY, "Alice")], send_msg_mode="silent"
        )
        async with listening(mc):
            await deliver(node, [dm_frame(ALICE_KEY[:6], "ping", 1758730301)], settle=3.2)
        await mc.disconnect()
        return node

    node = run_async(scenario())
    assert node.sent_texts == ["RECEIVED"]
    assert any_line(capsys.readouterr().err, "no ack yet")


def test_reply_failure_is_reported(run_async, capsys):
    async def scenario():
        node, mc = await open_node(
            contacts=[(ALICE_KEY, "Alice")], send_msg_mode="error"
        )
        async with listening(mc):
            await deliver(node, [dm_frame(ALICE_KEY[:6], "ping", 1758730302)], settle=1.2)
        await mc.disconnect()

    run_async(scenario())
    assert any_line(capsys.readouterr().err, "reply to Alice not sent")


def test_own_reply_text_is_not_answered(run_async):
    """Two nodes running this must not answer each other forever."""

    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        async with listening(mc):
            await deliver(node, [dm_frame(ALICE_KEY[:6], "RECEIVED", 1758730303)], settle=1.0)
        await mc.disconnect()
        return node

    assert run_async(scenario()).sent_texts == []


def test_cli_messages_are_not_answered(run_async):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        async with listening(mc):
            await deliver(
                node, [dm_frame(ALICE_KEY[:6], "ver", 1758730304, txt_type=1)], settle=1.0
            )
        await mc.disconnect()
        return node

    assert run_async(scenario()).sent_texts == []


def test_channel_messages_are_not_answered(run_async):
    async def scenario():
        node, mc = await open_node()
        async with listening(mc, channels={0: "general"}):
            await deliver(node, [channel_msg_frame(0, "Ivan: hi", 1758730305)], settle=1.0)
        await mc.disconnect()
        return node

    assert run_async(scenario()).sent_texts == []


def test_replying_can_be_turned_off(run_async):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        async with listening(mc, reply_text=None):
            await deliver(node, [dm_frame(ALICE_KEY[:6], "ping", 1758730306)], settle=1.0)
        await mc.disconnect()
        return node

    assert run_async(scenario()).sent_texts == []


def test_every_message_gets_its_own_reply(run_async):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        async with listening(mc):
            await deliver(
                node,
                [
                    dm_frame(ALICE_KEY[:6], "one", 1758730307),
                    dm_frame(ALICE_KEY[:6], "two", 1758730308),
                ],
                settle=2.0,
            )
        await mc.disconnect()
        return node

    assert run_async(scenario()).sent_texts == ["RECEIVED", "RECEIVED"]


def test_custom_reply_text(run_async):
    async def scenario():
        node, mc = await open_node(contacts=[(ALICE_KEY, "Alice")])
        async with listening(mc, reply_text="ok"):
            await deliver(node, [dm_frame(ALICE_KEY[:6], "ping", 1758730309)], settle=1.2)
        await mc.disconnect()
        return node

    assert run_async(scenario()).sent_texts == ["ok"]
