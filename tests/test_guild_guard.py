"""
Unit tests for services/guild_guard.py and its wiring into the cogs.

The property under test: an instance with DEV_GUILD_ID set must not act on
anything from another guild. This is what makes it safe to run locally on a
token that is also live in production.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services import guild_guard as gg

DEV = 111
PROD = 999


def _guild(gid, name="server"):
    g = MagicMock()
    g.id = gid
    g.name = name
    return g


# ──────────────────────────────────────────────
# The guard itself
# ──────────────────────────────────────────────

class TestIsAllowedGuild:

    def test_unlocked_allows_everything(self):
        """Production behaviour is unchanged when DEV_GUILD_ID is unset."""
        with patch("services.guild_guard.DEV_GUILD_ID", None):
            assert gg.is_allowed_guild(_guild(PROD))
            assert gg.is_allowed_guild(_guild(DEV))
            assert not gg.is_locked()

    def test_locked_allows_only_the_dev_guild(self):
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            assert gg.is_allowed_guild(_guild(DEV))
            assert not gg.is_allowed_guild(_guild(PROD))
            assert gg.is_locked()

    def test_accepts_raw_ids(self):
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            assert gg.is_allowed_guild(DEV)
            assert not gg.is_allowed_guild(PROD)

    def test_locked_rejects_missing_guild(self):
        """No guild context means nothing to scope the action to."""
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            assert not gg.is_allowed_guild(None)

    def test_unlocked_allows_missing_guild(self):
        with patch("services.guild_guard.DEV_GUILD_ID", None):
            assert gg.is_allowed_guild(None)

    def test_blocked_guild_logged_once(self, capsys):
        gg._reported.clear()
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            for _ in range(5):
                gg.is_allowed_guild(_guild(PROD))
        assert capsys.readouterr().out.count("Ignoring guild") == 1

    def test_guild_id_of(self):
        assert gg.guild_id_of(_guild(7)) == 7
        assert gg.guild_id_of(7) == 7
        assert gg.guild_id_of(None) is None


class TestDescribeLock:

    def test_unlocked_summary(self):
        with patch("services.guild_guard.DEV_GUILD_ID", None):
            out = gg.describe_lock([_guild(PROD, "Prod")])
        assert "No guild lock" in out and "ALL guilds" in out

    def test_locked_summary_names_ignored_guilds(self):
        """The line that would have made this obvious immediately."""
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            out = gg.describe_lock([_guild(DEV, "Test"), _guild(PROD, "Production")])
        assert "Guild lock ACTIVE" in out
        assert "Acting in: Test" in out
        assert "Ignoring 1 other guild" in out
        assert "Production" in out
        assert "production token locally" in out

    def test_warns_when_not_in_the_dev_guild(self):
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            out = gg.describe_lock([_guild(PROD, "Production")])
        assert "ignore EVERYTHING" in out


# ──────────────────────────────────────────────
# Listener integration
# ──────────────────────────────────────────────

def _message(guild_id, category="Fractal_Trades", content="x" * 200):
    m = MagicMock()
    m.author.bot = False
    m.guild = _guild(guild_id)
    m.guild.id = guild_id
    m.content = content
    m.channel = MagicMock()
    m.channel.id = 1
    m.channel.name = "trade_1_gc"
    m.channel.send = AsyncMock()
    m.channel.category = MagicMock()
    m.channel.category.name = category
    m.channel.typing = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)))
    return m


class TestTradesListenerRespectsLock:

    @pytest.fixture
    def cog(self):
        from cogs.trades import Trades
        bot = MagicMock()
        bot.loop = MagicMock()
        return Trades(bot)

    @pytest.mark.asyncio
    async def test_ignores_foreign_guild(self, cog):
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            await cog.on_message(_message(PROD))
        cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_acts_in_dev_guild(self, cog):
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            await cog.on_message(_message(DEV))
        cog.bot.loop.create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_channel_create_ignores_foreign_guild(self, cog):
        import discord
        ch = MagicMock(spec=discord.TextChannel)
        ch.guild = _guild(PROD)
        ch.category = MagicMock()
        ch.category.name = "Fractal_Trades"
        ch.send = AsyncMock()
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            await cog.on_guild_channel_create(ch)
        ch.send.assert_not_awaited()


class TestMissedListenerRespectsLock:

    @pytest.fixture
    def cog(self):
        from cogs.missed import Missed
        bot = MagicMock()
        bot.loop = MagicMock()
        return Missed(bot)

    @pytest.mark.asyncio
    async def test_ignores_foreign_guild(self, cog):
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            await cog.on_message(_message(PROD, category="Missed_Trades"))
        cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_acts_in_dev_guild(self, cog):
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            await cog.on_message(_message(DEV, category="Missed_Trades"))
        cog.bot.loop.create_task.assert_called_once()


class TestCoachListenerRespectsLock:
    """The costly one: unguarded, this bills DeepSeek for production entries."""

    @pytest.fixture
    def cog(self):
        from cogs.coach import Coach
        return Coach(MagicMock())

    @pytest.mark.asyncio
    async def test_does_not_coach_foreign_guild(self, cog):
        with patch("services.guild_guard.DEV_GUILD_ID", DEV), \
             patch("cogs.coach.cs.coach", new=AsyncMock()) as coach_mock, \
             patch.object(cog, "_auto_enabled", AsyncMock(return_value=True)):
            await cog.on_message(_message(PROD))
        coach_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_coaches_dev_guild(self, cog):
        feedback = {"reading": "r", "observations": [], "probe": "",
                    "not_enough_signal": False, "warnings": [], "forbidden_hits": []}
        msg = _message(DEV)
        with patch("services.guild_guard.DEV_GUILD_ID", DEV), \
             patch("cogs.coach.cs.coach", new=AsyncMock(return_value=feedback)), \
             patch.object(cog, "_auto_enabled", AsyncMock(return_value=True)):
            await cog.on_message(msg)
        msg.channel.send.assert_awaited_once()


class TestSlashCommandsRespectLock:

    @pytest.mark.asyncio
    async def test_foreign_guild_interaction_refused(self):
        from bot import GuardedCommandTree
        tree = GuardedCommandTree.__new__(GuardedCommandTree)
        interaction = AsyncMock()
        interaction.guild = _guild(PROD)
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            assert await tree.interaction_check(interaction) is False
        assert "locked to a development server" in \
            interaction.response.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_dev_guild_interaction_allowed(self):
        from bot import GuardedCommandTree
        tree = GuardedCommandTree.__new__(GuardedCommandTree)
        interaction = AsyncMock()
        interaction.guild = _guild(DEV)
        with patch("services.guild_guard.DEV_GUILD_ID", DEV):
            assert await tree.interaction_check(interaction) is True
        interaction.response.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unlocked_allows_any_guild(self):
        from bot import GuardedCommandTree
        tree = GuardedCommandTree.__new__(GuardedCommandTree)
        interaction = AsyncMock()
        interaction.guild = _guild(PROD)
        with patch("services.guild_guard.DEV_GUILD_ID", None):
            assert await tree.interaction_check(interaction) is True


# ──────────────────────────────────────────────
# Startup resilience
# ──────────────────────────────────────────────

class TestDevGuildSync:
    """
    A 50001 on guild sync is a setup problem, not a crash. Startup must survive
    it so on_ready can print the guild list, which is what distinguishes
    "bot isn't in that server" from "invited without applications.commands".
    """

    @pytest.fixture
    def bot(self):
        # Client.tree is a read-only property, so borrow the methods onto a
        # stand-in — they only touch self.tree and self.application_id.
        from bot import TradingAssistant

        class Stand_in:
            application_id = None
            _sync_dev_guild = TradingAssistant._sync_dev_guild
            invite_url = TradingAssistant.invite_url

            def __init__(self):
                self.tree = MagicMock()
                self.tree.copy_global_to = MagicMock()

        return Stand_in()

    @pytest.mark.asyncio
    async def test_successful_sync_reports_count(self, bot, capsys):
        bot.tree.sync = AsyncMock(return_value=[1, 2, 3])
        with patch("bot.DEV_GUILD_ID", DEV):
            await bot._sync_dev_guild()
        assert "Synced 3 command(s)" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_forbidden_does_not_raise(self, bot, capsys):
        import discord
        resp = MagicMock()
        resp.status = 403
        bot.tree.sync = AsyncMock(side_effect=discord.Forbidden(resp, "Missing Access"))
        type(bot).application_id = 42

        with patch("bot.DEV_GUILD_ID", DEV):
            await bot._sync_dev_guild()   # must not raise

        out = capsys.readouterr().out
        assert "Missing Access" in out
        assert "applications.commands" in out
        assert "Starting anyway" in out
        # The remedy must be a ready-to-click link with the real app id.
        assert "client_id=42" in out
        assert "scope=bot%20applications.commands" in out

    @pytest.mark.asyncio
    async def test_other_http_error_does_not_raise(self, bot, capsys):
        import discord
        resp = MagicMock()
        resp.status = 500
        bot.tree.sync = AsyncMock(side_effect=discord.HTTPException(resp, "boom"))
        with patch("bot.DEV_GUILD_ID", DEV):
            await bot._sync_dev_guild()
        assert "Command sync failed" in capsys.readouterr().out

    def test_invite_url_carries_both_scopes(self, bot):
        type(bot).application_id = 12345
        url = bot.invite_url()
        assert "client_id=12345" in url
        assert "scope=bot%20applications.commands" in url
        assert "permissions=125968" in url

    def test_invite_url_without_application_id(self, bot):
        type(bot).application_id = None
        url = bot.invite_url()
        assert "YOUR_APP_ID" in url
