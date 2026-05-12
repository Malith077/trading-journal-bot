"""
Unit tests for cogs/rag.py
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cogs.rag import RAG, chunk_text


class TestChunkText:
    def test_short_text(self):
        chunks = chunk_text("Hello world", max_length=50)
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_long_text_split_by_newline(self):
        text = "A" * 20 + "\n" + "B" * 20
        chunks = chunk_text(text, max_length=25)
        assert len(chunks) == 2
        assert chunks[0] == "A" * 20
        assert chunks[1] == "B" * 20

    def test_long_text_split_by_space(self):
        text = "A" * 20 + " " + "B" * 20
        chunks = chunk_text(text, max_length=25)
        assert len(chunks) == 2
        assert chunks[0] == "A" * 20
        assert chunks[1] == "B" * 20

    def test_long_text_no_spaces(self):
        text = "A" * 50
        chunks = chunk_text(text, max_length=20)
        assert len(chunks) == 3
        assert chunks[0] == "A" * 20
        assert chunks[1] == "A" * 20
        assert chunks[2] == "A" * 10


class TestRAGCog:

    @pytest.fixture
    def bot(self):
        return MagicMock()

    @pytest.fixture
    def cog(self, bot):
        return RAG(bot)

    @pytest.fixture
    def interaction(self):
        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        return interaction

    @pytest.mark.asyncio
    async def test_ask_no_context(self, cog, interaction):
        with patch("cogs.rag.rag_service") as mock_rag:
            mock_rag.query_knowledge.return_value = ("", [])
            await cog.ask_bot.callback(cog, interaction, question="What is CISD?")
        interaction.edit_original_response.assert_called()
        assert "couldn't find" in interaction.edit_original_response.call_args.kwargs["content"].lower()

    @pytest.mark.asyncio
    async def test_ask_successful(self, cog, interaction):
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
            await cog.ask_bot.callback(cog, interaction, question="What is CISD?")

        embed_calls = [c for c in interaction.edit_original_response.call_args_list if c.kwargs.get("embed")]
        assert len(embed_calls) >= 1
        interaction.followup.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_ask_long_response(self, cog, interaction):
        long_answer = "A" * 5000
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"response": long_answer})

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
            await cog.ask_bot.callback(cog, interaction, question="Long question?")

        # 5000 chars should be split into 2 chunks of <=4000
        assert interaction.edit_original_response.call_count == 2
        interaction.followup.send.assert_called_once()
        
        embed1 = interaction.edit_original_response.call_args_list[-1].kwargs["embed"]
        embed2 = interaction.followup.send.call_args.kwargs["embed"]
        assert len(embed1.description) <= 4000
        assert len(embed2.description) <= 4000
        assert "Part 2" in embed2.title
        
        # Sources should only be on the last embed
        assert not embed1.fields
        assert embed2.fields[0].name == "📚 Sources Used"

    @pytest.mark.asyncio
    async def test_ask_api_error(self, cog, interaction):
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
            await cog.ask_bot.callback(cog, interaction, question="test")

        interaction.edit_original_response.assert_called()
        assert "error" in interaction.edit_original_response.call_args.kwargs["content"].lower()

    @pytest.mark.asyncio
    async def test_reindex_success(self, cog, interaction):
        with patch("cogs.rag.flush_and_reindex"):
            await cog.manual_reindex.callback(cog, interaction)
        assert any("successful" in str(c).lower() for c in interaction.edit_original_response.call_args_list)

    @pytest.mark.asyncio
    async def test_reindex_failure(self, cog, interaction):
        with patch("cogs.rag.flush_and_reindex", side_effect=RuntimeError("DB error")):
            await cog.manual_reindex.callback(cog, interaction)
        assert any("failed" in str(c).lower() for c in interaction.edit_original_response.call_args_list)
