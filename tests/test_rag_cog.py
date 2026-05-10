"""
Unit tests for cogs/rag.py
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cogs.rag import RAG


class TestRAGCog:

    @pytest.fixture
    def bot(self):
        return MagicMock()

    @pytest.fixture
    def cog(self, bot):
        return RAG(bot)

    @pytest.fixture
    def ctx(self):
        ctx = AsyncMock()
        status_msg = AsyncMock()
        ctx.send = AsyncMock(return_value=status_msg)
        return ctx

    @pytest.mark.asyncio
    async def test_ask_no_context(self, cog, ctx):
        with patch("cogs.rag.rag_service") as mock_rag:
            mock_rag.query_knowledge.return_value = ("", [])
            await cog.ask_bot.callback(cog, ctx, question="What is CISD?")
        status_msg = ctx.send.return_value
        status_msg.edit.assert_called()
        assert "couldn't find" in status_msg.edit.call_args.kwargs["content"].lower()

    @pytest.mark.asyncio
    async def test_ask_successful(self, cog, ctx):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"response": "CISD means Change in State."})

        with patch("cogs.rag.rag_service") as mock_rag, \
             patch("aiohttp.ClientSession") as mock_cls:
            mock_rag.query_knowledge.return_value = ("context chunk", ["article.md"])
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False)
            ))
            mock_cls.return_value = mock_session
            await cog.ask_bot.callback(cog, ctx, question="What is CISD?")

        embed_calls = [c for c in ctx.send.call_args_list if c.kwargs.get("embed")]
        assert len(embed_calls) >= 1

    @pytest.mark.asyncio
    async def test_ask_api_error(self, cog, ctx):
        mock_resp = AsyncMock()
        mock_resp.status = 500

        with patch("cogs.rag.rag_service") as mock_rag, \
             patch("aiohttp.ClientSession") as mock_cls:
            mock_rag.query_knowledge.return_value = ("some context", ["file.md"])
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False)
            ))
            mock_cls.return_value = mock_session
            await cog.ask_bot.callback(cog, ctx, question="test")

        status_msg = ctx.send.return_value
        status_msg.edit.assert_called()
        assert "error" in status_msg.edit.call_args.kwargs["content"].lower()

    @pytest.mark.asyncio
    async def test_reindex_success(self, cog, ctx):
        with patch("cogs.rag.flush_and_reindex"):
            await cog.manual_reindex.callback(cog, ctx)
        assert any("successful" in str(c).lower() for c in ctx.send.call_args_list)

    @pytest.mark.asyncio
    async def test_reindex_failure(self, cog, ctx):
        with patch("cogs.rag.flush_and_reindex", side_effect=RuntimeError("DB error")):
            await cog.manual_reindex.callback(cog, ctx)
        assert any("failed" in str(c).lower() for c in ctx.send.call_args_list)
