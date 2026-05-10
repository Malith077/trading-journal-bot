"""
Unit tests for scripts/reindex_all.py

Covers:
  - flush_and_reindex: deletes collection, re-creates, indexes all .md files
  - flush_and_reindex: handles missing KB directory
  - flush_and_reindex: handles collection that doesn't exist yet
"""
import pytest
from unittest.mock import MagicMock, patch, call
from pathlib import Path


class TestFlushAndReindex:

    @pytest.fixture
    def mock_rag(self):
        mock = MagicMock()
        mock.client = MagicMock()
        mock.embed_fn = MagicMock()
        mock.collection = MagicMock()
        mock.client.get_or_create_collection.return_value = mock.collection
        return mock

    def test_full_reindex(self, mock_rag, tmp_path):
        """Deletes collection, re-creates it, and indexes all .md files."""
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "article1.md").write_text("# Article 1")
        (kb_dir / "article2.md").write_text("# Article 2")
        (kb_dir / "notes.txt").write_text("Not markdown")  # should be ignored

        with patch("scripts.reindex_all.rag_service", mock_rag), \
             patch("scripts.reindex_all.KB_DIR", kb_dir), \
             patch("scripts.reindex_all.RAG_COLLECTION_NAME", "test_col"):
            from scripts.reindex_all import flush_and_reindex
            flush_and_reindex()

        mock_rag.client.delete_collection.assert_called_once_with(name="test_col")
        mock_rag.client.get_or_create_collection.assert_called_once()
        assert mock_rag.index_markdown_file.call_count == 2

    def test_missing_kb_directory(self, mock_rag, tmp_path):
        """Handles missing knowledge base directory gracefully."""
        missing_dir = tmp_path / "nonexistent"

        with patch("scripts.reindex_all.rag_service", mock_rag), \
             patch("scripts.reindex_all.KB_DIR", missing_dir), \
             patch("scripts.reindex_all.RAG_COLLECTION_NAME", "test_col"):
            from scripts.reindex_all import flush_and_reindex
            flush_and_reindex()

        mock_rag.index_markdown_file.assert_not_called()

    def test_collection_doesnt_exist_yet(self, mock_rag, tmp_path):
        """Handles case where collection doesn't exist (first run)."""
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "article.md").write_text("# Test")

        mock_rag.client.delete_collection.side_effect = Exception("Not found")

        with patch("scripts.reindex_all.rag_service", mock_rag), \
             patch("scripts.reindex_all.KB_DIR", kb_dir), \
             patch("scripts.reindex_all.RAG_COLLECTION_NAME", "test_col"):
            from scripts.reindex_all import flush_and_reindex
            flush_and_reindex()

        # Should continue despite the exception
        mock_rag.client.get_or_create_collection.assert_called_once()
        mock_rag.index_markdown_file.assert_called_once()
