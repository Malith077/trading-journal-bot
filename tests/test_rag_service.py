"""
Unit tests for services/rag_service.py

Covers:
  - chunk_markdown(): heading splits, paragraph splits, hard splits, overlap, edge cases
  - RAGService.index_markdown_file(): chunking + upsert + old-chunk cleanup
  - RAGService.query_knowledge(): result formatting, dedup, empty results
"""
import pytest
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path

# Import the pure function directly — no ChromaDB needed
from services.rag_service import chunk_markdown


# ──────────────────────────────────────────────
# chunk_markdown() tests
# ──────────────────────────────────────────────

class TestChunkMarkdown:
    """Tests for the markdown chunking utility function."""

    def test_small_content_single_chunk(self):
        """Content under max_chars stays as one chunk."""
        content = "# Title\n\nShort content here."
        chunks = chunk_markdown(content, max_chars=1500)
        assert len(chunks) == 1
        assert "Title" in chunks[0]

    def test_splits_on_headings(self):
        """Each markdown heading starts a new chunk."""
        content = "# Heading 1\nParagraph one.\n\n## Heading 2\nParagraph two.\n\n### Heading 3\nParagraph three."
        chunks = chunk_markdown(content, max_chars=1500)
        assert len(chunks) == 3
        assert chunks[0].startswith("# Heading 1")
        assert chunks[1].startswith("## Heading 2")
        assert chunks[2].startswith("### Heading 3")

    def test_large_section_splits_on_paragraphs(self):
        """A section exceeding max_chars gets split on paragraph boundaries."""
        para1 = "A" * 400
        para2 = "B" * 400
        para3 = "C" * 400
        content = f"# Big Section\n\n{para1}\n\n{para2}\n\n{para3}"
        # max_chars=500 forces paragraph-level splitting
        chunks = chunk_markdown(content, max_chars=500, overlap=50)
        assert len(chunks) >= 3
        assert any("A" * 100 in c for c in chunks)
        assert any("B" * 100 in c for c in chunks)
        assert any("C" * 100 in c for c in chunks)

    def test_hard_split_with_overlap(self):
        """A single paragraph exceeding max_chars gets hard-split with overlap."""
        long_para = "X" * 1000
        content = f"# Section\n\n{long_para}"
        chunks = chunk_markdown(content, max_chars=300, overlap=50)
        # The heading is one chunk, then the long para gets hard-split
        long_chunks = [c for c in chunks if "X" in c]
        assert len(long_chunks) >= 3
        # Verify overlap: end of chunk N overlaps with start of chunk N+1
        for i in range(len(long_chunks) - 1):
            tail = long_chunks[i][-50:]
            head = long_chunks[i + 1][:50]
            assert tail == head

    def test_empty_content(self):
        """Empty string returns empty list."""
        assert chunk_markdown("") == []

    def test_whitespace_only_content(self):
        """Whitespace-only content returns empty list."""
        assert chunk_markdown("   \n\n  ") == []

    def test_no_headings(self):
        """Content without headings stays as one chunk if under limit."""
        content = "Just a plain paragraph with no markdown headings."
        chunks = chunk_markdown(content, max_chars=1500)
        assert len(chunks) == 1
        assert chunks[0] == content

    def test_preserves_heading_with_body(self):
        """Heading text stays attached to its body content."""
        content = "## Key Concepts\n*   **CISD:** A signal.\n*   **EQ:** The midpoint."
        chunks = chunk_markdown(content, max_chars=1500)
        assert len(chunks) == 1
        assert "## Key Concepts" in chunks[0]
        assert "CISD" in chunks[0]

    def test_multiple_heading_levels(self):
        """H1, H2, H3 all trigger splits."""
        content = "# H1\nBody1\n\n## H2\nBody2\n\n### H3\nBody3"
        chunks = chunk_markdown(content, max_chars=1500)
        assert len(chunks) == 3

    def test_h4_does_not_split(self):
        """H4+ headings do NOT trigger splits (only H1-H3)."""
        content = "# Main\nIntro\n\n#### Sub-detail\nDetail text"
        chunks = chunk_markdown(content, max_chars=1500)
        assert len(chunks) == 1
        assert "#### Sub-detail" in chunks[0]


# ──────────────────────────────────────────────
# RAGService tests (mocked ChromaDB)
# ──────────────────────────────────────────────

class TestRAGService:
    """Tests for RAGService methods with mocked ChromaDB."""

    @pytest.fixture
    def mock_rag_service(self):
        """Create a RAGService with mocked ChromaDB internals."""
        with patch("services.rag_service.chromadb") as mock_chroma, \
             patch("services.rag_service.embedding_functions"):
            mock_collection = MagicMock()
            mock_client = MagicMock()
            mock_client.get_or_create_collection.return_value = mock_collection
            mock_chroma.PersistentClient.return_value = mock_client

            from services.rag_service import RAGService
            service = RAGService()
            service.collection = mock_collection
            service.client = mock_client
            yield service

    def test_index_markdown_file_chunks_and_upserts(self, mock_rag_service, tmp_path):
        """index_markdown_file reads the file, chunks it, and upserts into ChromaDB."""
        md_file = tmp_path / "test_article.md"
        md_file.write_text("# Title\nIntro\n\n## Section\nContent here.")

        mock_rag_service.collection.get.return_value = {"ids": []}

        mock_rag_service.index_markdown_file(md_file)

        mock_rag_service.collection.upsert.assert_called_once()
        call_kwargs = mock_rag_service.collection.upsert.call_args
        ids = call_kwargs.kwargs.get("ids") or call_kwargs[1].get("ids")
        assert all("test_article.md::chunk_" in id for id in ids)

    def test_index_removes_old_chunks_first(self, mock_rag_service, tmp_path):
        """Old chunks for the same file are deleted before re-indexing."""
        md_file = tmp_path / "old_article.md"
        md_file.write_text("# Old\nOld content.")

        mock_rag_service.collection.get.return_value = {
            "ids": ["old_article.md::chunk_0", "old_article.md::chunk_1"]
        }

        mock_rag_service.index_markdown_file(md_file)

        mock_rag_service.collection.delete.assert_called_once_with(
            ids=["old_article.md::chunk_0", "old_article.md::chunk_1"]
        )

    def test_index_no_old_chunks_skips_delete(self, mock_rag_service, tmp_path):
        """If no old chunks exist, delete is not called."""
        md_file = tmp_path / "new_article.md"
        md_file.write_text("# New\nFresh content.")

        mock_rag_service.collection.get.return_value = {"ids": []}

        mock_rag_service.index_markdown_file(md_file)

        mock_rag_service.collection.delete.assert_not_called()

    def test_query_knowledge_returns_context_and_sources(self, mock_rag_service):
        """query_knowledge returns combined context and deduplicated source list."""
        mock_rag_service.collection.query.return_value = {
            "documents": [["Chunk A text", "Chunk B text", "Chunk C text"]],
            "metadatas": [[
                {"source": "article1.md"},
                {"source": "article1.md"},  # duplicate
                {"source": "article2.md"}
            ]]
        }

        context, sources = mock_rag_service.query_knowledge("test query")

        assert "Chunk A text" in context
        assert "Chunk B text" in context
        assert "---" in context  # separator
        assert sources == ["article1.md", "article2.md"]  # deduplicated

    def test_query_knowledge_empty_results(self, mock_rag_service):
        """Empty ChromaDB results return empty string and empty list."""
        mock_rag_service.collection.query.return_value = {
            "documents": [[]],
            "metadatas": [[]]
        }

        context, sources = mock_rag_service.query_knowledge("unknown topic")

        assert context == ""
        assert sources == []

    def test_query_knowledge_no_documents_key(self, mock_rag_service):
        """Handles case where documents list is completely empty."""
        mock_rag_service.collection.query.return_value = {
            "documents": [],
            "metadatas": []
        }

        context, sources = mock_rag_service.query_knowledge("anything")

        assert context == ""
        assert sources == []
