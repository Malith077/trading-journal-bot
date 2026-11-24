"""
Tests for the Discord-maintained checklist (cogs/checklist.py,
services/checklist_service.py) and the per-channel snapshots in cogs/trades.py
that stop an edited list from rewriting old trades' checklists.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import cogs.trades as trades_mod
from cogs.checklist import Checklist, STATUS_TITLE, status_embed
from cogs.trades import (
    ChecklistView, _build_checklist_embed, _items_from_embed,
    _read_checklist_state, _load_checklist_items,
)
from services import checklist_service as cl


# ──────────────────────────────────────────────
# Parsing
# ──────────────────────────────────────────────

class TestParseItems:

    def test_one_item_per_line_across_messages(self):
        items, overflow = cl.parse_items(["Bias set\nAsia swept", "CISD on 5m"])
        assert items == ["Bias set", "Asia swept", "CISD on 5m"]
        assert overflow == 0

    @pytest.mark.parametrize("line", ["- Bias set", "* Bias set", "• Bias set", "1. Bias set",
                                      "2) Bias set", "[ ] Bias set", "[x] Bias set",
                                      "⬜ Bias set", "✅  Bias set", "   Bias set   "])
    def test_list_markers_stripped(self, line):
        assert cl.parse_items([line])[0] == ["Bias set"]

    def test_numbers_inside_items_kept(self):
        assert cl.parse_items(["Wait for 2 closes above the FVG"])[0] == \
            ["Wait for 2 closes above the FVG"]

    def test_blank_lines_code_fences_and_duplicates_dropped(self):
        items, _ = cl.parse_items(["```\nA\n\nB\n```", "A", None, ""])
        assert items == ["A", "B"]

    def test_caps_at_discord_button_limit(self):
        items, overflow = cl.parse_items(["\n".join(f"Rule {i}" for i in range(30))])
        assert len(items) == cl.MAX_ITEMS == 25
        assert overflow == 5

    def test_save_live_round_trips(self, tmp_path):
        path = tmp_path / "live.json"
        cl.save_live(["A", "B"], path)
        assert json.loads(path.read_text()) == ["A", "B"]
        assert not list(tmp_path.glob("*.tmp"))


# ──────────────────────────────────────────────
# Which list new trades get
# ──────────────────────────────────────────────

class TestLoadItems:

    def test_live_list_overrides_repo_default(self, tmp_path):
        live, default = tmp_path / "live.json", tmp_path / "default.json"
        live.write_text(json.dumps(["From Discord"]))
        default.write_text(json.dumps(["From repo"]))
        with patch("cogs.trades.LIVE_CHECKLIST_PATH", live), \
             patch("cogs.trades.CHECKLIST_PATH", default):
            assert _load_checklist_items() == ["From Discord"]

    @pytest.mark.parametrize("content", [None, "[]", "not json{{"])
    def test_falls_back_to_repo_default(self, tmp_path, content):
        live, default = tmp_path / "live.json", tmp_path / "default.json"
        if content is not None:
            live.write_text(content)
        default.write_text(json.dumps(["From repo"]))
        with patch("cogs.trades.LIVE_CHECKLIST_PATH", live), \
             patch("cogs.trades.CHECKLIST_PATH", default):
            assert _load_checklist_items() == ["From repo"]


# ──────────────────────────────────────────────
# Per-channel snapshots
# ──────────────────────────────────────────────

def checklist_message(items, states):
    message = MagicMock()
    message.embeds = [_build_checklist_embed(items, states)]
    return message


class TestSnapshots:

    def test_items_recovered_from_the_message(self):
        items, states = _items_from_embed(checklist_message(["A", "B C"], [True, False]))
        assert items == ["A", "B C"]
        assert states == [True, False]

    def test_unreadable_message_gives_nothing(self):
        assert _items_from_embed(MagicMock(embeds=[])) == ([], [])
        assert _items_from_embed(None) == ([], [])

    @pytest.mark.asyncio
    async def test_snapshot_file_wins(self, tmp_path):
        path = tmp_path / "checklist_state.json"
        path.write_text(json.dumps({"items": ["Old 1", "Old 2"], "states": [True]}))
        items, states = await _read_checklist_state(path, checklist_message(["X"], [False]))
        assert items == ["Old 1", "Old 2"]
        assert states == [True, False]  # padded to the item count

    @pytest.mark.asyncio
    async def test_legacy_tick_list_uses_items_from_the_message(self, tmp_path):
        path = tmp_path / "checklist_state.json"
        path.write_text(json.dumps([False, True]))
        msg = checklist_message(["Was 1", "Was 2"], [False, False])
        with patch("cogs.trades._load_checklist_items", return_value=["New list"]):
            items, states = await _read_checklist_state(path, msg)
        assert items == ["Was 1", "Was 2"]
        assert states == [False, True]

    @pytest.mark.asyncio
    async def test_nothing_saved_falls_back_to_current_list(self, tmp_path):
        with patch("cogs.trades._load_checklist_items", return_value=["Current"]):
            items, states = await _read_checklist_state(tmp_path / "missing.json", None)
        assert items == ["Current"] and states == [False]


def button(view, index):
    return next(c for c in view.children if c.custom_id == f"checklist_{index}")


class TestChecklistClicks:

    def test_router_covers_every_possible_index(self):
        view = ChecklistView.persistent()
        assert [c.custom_id for c in view.children] == \
            [f"checklist_{i}" for i in range(cl.MAX_ITEMS)]

    def test_rendered_view_reflects_ticks(self):
        view = ChecklistView(["A", "B"], [True, False])
        assert button(view, 0).style == discord.ButtonStyle.success
        assert button(view, 1).style == discord.ButtonStyle.secondary
        assert button(view, 1).label == "2. B"

    @pytest.mark.asyncio
    async def test_old_channel_keeps_its_own_items_after_list_changes(self, tmp_path):
        folder = tmp_path / "trade_5_gc"
        folder.mkdir()
        (folder / "checklist_state.json").write_text(
            json.dumps({"items": ["Old A", "Old B"], "states": [False, False]}))

        interaction = MagicMock()
        interaction.channel.name = "trade_5_gc"
        interaction.response = AsyncMock()
        with patch("cogs.trades.TRADES_DIR", tmp_path), \
             patch("cogs.trades._load_checklist_items", return_value=["Brand new"]):
            await button(ChecklistView.persistent(), 1).callback(interaction)

        kwargs = interaction.response.edit_message.call_args.kwargs
        assert "Old B" in kwargs["embed"].fields[0].value
        assert "Brand new" not in kwargs["embed"].fields[0].value
        assert button(kwargs["view"], 1).style == discord.ButtonStyle.success
        assert len(kwargs["view"].children) == 2
        saved = json.loads((folder / "checklist_state.json").read_text())
        assert saved == {"items": ["Old A", "Old B"], "states": [False, True]}

    @pytest.mark.asyncio
    async def test_index_beyond_the_channels_list_is_refused(self, tmp_path):
        folder = tmp_path / "trade_5_gc"
        folder.mkdir()
        (folder / "checklist_state.json").write_text(json.dumps({"items": ["Only"], "states": []}))
        interaction = MagicMock()
        interaction.channel.name = "trade_5_gc"
        interaction.response = AsyncMock()
        with patch("cogs.trades.TRADES_DIR", tmp_path):
            await button(ChecklistView.persistent(), 3).callback(interaction)
        interaction.response.edit_message.assert_not_awaited()
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_new_trade_channel_snapshots_the_list(self, tmp_path):
        channel = AsyncMock(spec=discord.TextChannel)
        channel.name = "trade_9_es"
        channel.category = MagicMock()
        channel.category.name = "Fractal_Trades"
        with patch("cogs.trades.TRADES_DIR", tmp_path), \
             patch("cogs.trades._load_checklist_items", return_value=["R1", "R2"]):
            await trades_mod.Trades(MagicMock()).on_guild_channel_create(channel)
        saved = json.loads((tmp_path / "trade_9_es" / "checklist_state.json").read_text())
        assert saved == {"items": ["R1", "R2"], "states": [False, False]}


# ──────────────────────────────────────────────
# The #trade_checklist channel
# ──────────────────────────────────────────────

BOT_ID = 999


def msg(content="", *, bot=False, author_id=1, embeds=None):
    m = MagicMock()
    m.content = content
    m.author.bot = bot
    m.author.id = BOT_ID if bot else author_id
    m.embeds = embeds or []
    m.delete = AsyncMock()
    return m


def channel_with(messages, name=cl.CHECKLIST_CHANNEL_NAME):
    ch = MagicMock(spec=discord.TextChannel)
    ch.name = name
    ch.guild = MagicMock()
    ch.send = AsyncMock()

    def history(**kwargs):
        async def agen():
            for m in messages:
                yield m
        return agen()
    ch.history = MagicMock(side_effect=history)
    return ch


@pytest.fixture
def cog():
    bot = MagicMock()
    bot.user.id = BOT_ID
    return Checklist(bot)


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "live.json"
    with patch("cogs.trades.LIVE_CHECKLIST_PATH", path), \
         patch("cogs.trades.CHECKLIST_PATH", tmp_path / "default.json"):
        (tmp_path / "default.json").write_text(json.dumps(["Default rule"]))
        yield path


class TestChannelSync:

    @pytest.mark.asyncio
    async def test_saves_your_lines_and_confirms(self, cog, live):
        ch = channel_with([msg("- Bias set\n- Asia swept"), msg("CISD on 5m")])
        assert await cog.sync_from_channel(ch) == ["Bias set", "Asia swept", "CISD on 5m"]
        assert json.loads(live.read_text()) == ["Bias set", "Asia swept", "CISD on 5m"]
        embed = ch.send.call_args.kwargs["embed"]
        assert embed.title == STATUS_TITLE and "**3.** CISD on 5m" in embed.description

    @pytest.mark.asyncio
    async def test_bot_messages_are_not_items(self, cog, live):
        ch = channel_with([msg("instructions", bot=True), msg("My rule")])
        assert await cog.sync_from_channel(ch) == ["My rule"]

    @pytest.mark.asyncio
    async def test_previous_confirmation_replaced(self, cog, live):
        old = msg(bot=True, embeds=[status_embed(["x"], 0)])
        other_bot = msg(bot=True, embeds=[discord.Embed(title="📋 Your trade entry checklist lives here")])
        ch = channel_with([other_bot, old, msg("New rule")])
        await cog.sync_from_channel(ch)
        old.delete.assert_awaited_once()
        other_bot.delete.assert_not_awaited()  # the instructions stay

    @pytest.mark.asyncio
    async def test_unchanged_list_is_quiet(self, cog, live):
        ch = channel_with([msg("Default rule")])
        await cog.sync_from_channel(ch)
        ch.send.assert_not_awaited()
        assert not live.exists()

    @pytest.mark.asyncio
    async def test_empty_channel_keeps_current_list(self, cog, live):
        ch = channel_with([msg("instructions", bot=True)])
        assert await cog.sync_from_channel(ch) is None
        assert not live.exists()
        assert _load_checklist_items() == ["Default rule"]

    @pytest.mark.asyncio
    async def test_overflow_is_reported(self, cog, live):
        ch = channel_with([msg("\n".join(f"Rule {i}" for i in range(27)))])
        await cog.sync_from_channel(ch)
        embed = ch.send.call_args.kwargs["embed"]
        assert any("Too many" in f.name for f in embed.fields)


class TestTriggers:

    @pytest.mark.asyncio
    async def test_your_message_in_the_channel_syncs(self, cog):
        m = msg("Rule")
        m.channel = channel_with([])
        with patch.object(cog, "sync_from_channel", new=AsyncMock()) as sync:
            await cog.on_message(m)
        sync.assert_awaited_once_with(m.channel)

    @pytest.mark.asyncio
    async def test_other_channels_ignored(self, cog):
        m = msg("Rule")
        m.channel = channel_with([], name="general")
        with patch.object(cog, "sync_from_channel", new=AsyncMock()) as sync:
            await cog.on_message(m)
        sync.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edits_and_deletes_sync(self, cog):
        cog.bot.get_channel.return_value = channel_with([])
        payload = MagicMock(channel_id=5)
        with patch.object(cog, "sync_from_channel", new=AsyncMock()) as sync:
            await cog.on_raw_message_edit(payload)
            await cog.on_raw_message_delete(payload)
        assert sync.await_count == 2

    @pytest.mark.asyncio
    async def test_locked_guild_ignored(self, cog):
        m = msg("Rule")
        m.channel = channel_with([])
        with patch("cogs.checklist.is_allowed_guild", return_value=False), \
             patch.object(cog, "sync_from_channel", new=AsyncMock()) as sync:
            await cog.on_message(m)
        sync.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_startup_catches_offline_edits_once(self, cog):
        ch = channel_with([])
        guild = MagicMock()
        guild.text_channels = [ch]
        cog.bot.guilds = [guild]
        with patch.object(cog, "sync_from_channel", new=AsyncMock()) as sync:
            await cog.on_ready()
            await cog.on_ready()  # reconnects fire on_ready again
        sync.assert_awaited_once_with(ch)


class TestSetup:

    @pytest.mark.asyncio
    async def test_creates_channel_and_posts_current_list(self, cog, live):
        created = channel_with([])
        interaction = MagicMock()
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        interaction.guild.text_channels = []
        interaction.guild.create_text_channel = AsyncMock(return_value=created)

        await cog.checklist_setup.callback(cog, interaction)

        interaction.guild.create_text_channel.assert_awaited_once_with(cl.CHECKLIST_CHANNEL_NAME)
        embed = created.send.call_args.kwargs["embed"]
        assert "Default rule" in embed.fields[0].value

    @pytest.mark.asyncio
    async def test_reuses_existing_channel(self, cog, live):
        existing = channel_with([])
        interaction = MagicMock()
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        interaction.guild.text_channels = [existing]
        interaction.guild.create_text_channel = AsyncMock()

        await cog.checklist_setup.callback(cog, interaction)

        interaction.guild.create_text_channel.assert_not_awaited()
        existing.send.assert_awaited_once()
