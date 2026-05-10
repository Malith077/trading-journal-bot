"""
Unit tests for cogs/knowledge.py
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
from discord.ext import commands

from cogs.knowledge import Knowledge


class TestKnowledgeCog:

    @pytest.fixture
    def bot(self):
        return MagicMock()

    @pytest.fixture
    def cog(self, bot):
        return Knowledge(bot)

    @pytest.fixture
    def ctx(self):
        ctx = AsyncMock()
        ctx.guild = MagicMock()
        ctx.channel = MagicMock()
        ctx.channel.name = "current_channel"
        return ctx

    @pytest.mark.asyncio
    async def test_channel_not_found(self, cog, ctx):
        with patch.object(commands.TextChannelConverter, "convert", side_effect=commands.ChannelNotFound("bad")):
            ctx.guild.text_channels = []
            await cog.extract_knowledge.callback(cog, ctx, channel_input="nonexistent")
        assert any("couldn't find" in str(c).lower() for c in ctx.send.call_args_list)

    @pytest.mark.asyncio
    async def test_no_content(self, cog, ctx):
        target = MagicMock()
        target.name = "empty_channel"

        async def empty_history(**kwargs):
            return
            yield
        target.history = empty_history

        status_msg = AsyncMock()
        ctx.send = AsyncMock(return_value=status_msg)
        ctx.channel = target

        await cog.extract_knowledge.callback(cog, ctx)
        status_msg.edit.assert_called()
        assert "No content" in status_msg.edit.call_args.kwargs.get("content", "")

    @pytest.mark.asyncio
    async def test_successful_extraction(self, cog, ctx, tmp_path):
        target = MagicMock()
        target.name = "strategy_channel"

        msg = MagicMock()
        msg.content = "Use CISD for entries"
        msg.author.bot = False
        msg.author.name = "trader"
        msg.created_at.strftime.return_value = "2026-01-01"
        msg.attachments = []

        async def mock_history(**kwargs):
            yield msg
        target.history = mock_history

        status_msg = AsyncMock()
        ctx.send = AsyncMock(return_value=status_msg)
        ctx.channel = target

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"response": "# Strategy Article\nContent"})

        with patch("cogs.knowledge.KB_DIR", tmp_path), \
             patch("cogs.knowledge.rag_service") as mock_rag, \
             patch("aiohttp.ClientSession") as mock_cls:

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False)
            ))
            mock_cls.return_value = mock_session

            await cog.extract_knowledge.callback(cog, ctx)

        article = tmp_path / "strategy_channel_article.md"
        assert article.exists()
        assert "Strategy Article" in article.read_text()
        mock_rag.index_markdown_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_ollama_error(self, cog, ctx):
        target = MagicMock()
        target.name = "test_ch"

        msg = MagicMock()
        msg.content = "Some content"
        msg.author.bot = False
        msg.author.name = "user"
        msg.created_at.strftime.return_value = "2026-01-01"
        msg.attachments = []

        async def mock_history(**kwargs):
            yield msg
        target.history = mock_history

        status_msg = AsyncMock()
        ctx.send = AsyncMock(return_value=status_msg)
        ctx.channel = target

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal error")

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False)
            ))
            mock_cls.return_value = mock_session

            await cog.extract_knowledge.callback(cog, ctx)

        assert any("Ollama Error" in str(c) for c in ctx.send.call_args_list)
