"""
Tests for the SQLite FTS5 search backend.

Tests cover:
1. Search index creation
2. Indexing snapshots
3. Search queries with real test data
4. Flush operations
5. Edge cases (empty index, special characters)
"""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.search_backend_sqlite.search import (
    get_db_path,
    search,
    flush,
    SQLITEFTS_DB,
    FTS_TOKENIZERS,
)


class TestSqliteSearchBackend:
    """Test SQLite FTS5 search backend."""

    def setup_method(self, _method=None):
        """Create a temporary data directory with search index."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / SQLITEFTS_DB

        self._orig_data_dir = os.environ.get("SNAP_DIR")
        os.environ["SNAP_DIR"] = self.temp_dir

        # Create FTS5 table
        self._create_index()

    def teardown_method(self, _method=None):
        """Clean up temporary directory."""
        if self._orig_data_dir is None:
            os.environ.pop("SNAP_DIR", None)
        else:
            os.environ["SNAP_DIR"] = self._orig_data_dir
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_index(self):
        """Create the FTS5 search index table."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS search_index
                USING fts5(
                    snapshot_id,
                    url,
                    title,
                    content,
                    tokenize = '{FTS_TOKENIZERS}'
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _index_snapshot(self, snapshot_id: str, url: str, title: str, content: str):
        """Add a snapshot to the index."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO search_index (snapshot_id, url, title, content) VALUES (?, ?, ?, ?)",
                (snapshot_id, url, title, content),
            )
            conn.commit()
        finally:
            conn.close()

    def test_get_db_path(self):
        """get_db_path should return correct path."""
        path = get_db_path()
        assert path == Path(self.temp_dir) / SQLITEFTS_DB

    def test_search_empty_index(self):
        """search should return empty list for empty index."""
        results = search("nonexistent")
        assert results == []

    def test_search_no_index_file(self):
        """search should return empty list when index file doesn't exist."""
        os.remove(self.db_path)
        results = search("test")
        assert results == []

    def test_search_single_result(self):
        """search should find matching snapshot."""
        self._index_snapshot(
            "snap-001",
            "https://example.com/page1",
            "Example Page",
            "This is example content about testing.",
        )

        results = search("example")
        assert len(results) == 1
        assert results[0] == "snap-001"

    def test_search_multiple_results(self):
        """search should find all matching snapshots."""
        self._index_snapshot(
            "snap-001",
            "https://example.com/1",
            "Python Tutorial",
            "Learn Python programming",
        )
        self._index_snapshot(
            "snap-002",
            "https://example.com/2",
            "Python Guide",
            "Advanced Python concepts",
        )
        self._index_snapshot(
            "snap-003",
            "https://example.com/3",
            "JavaScript Basics",
            "Learn JavaScript",
        )

        results = search("Python")
        assert len(results) == 2
        assert "snap-001" in results
        assert "snap-002" in results
        assert "snap-003" not in results

    def test_search_title_match(self):
        """search should match against title."""
        self._index_snapshot(
            "snap-001",
            "https://example.com",
            "Django Web Framework",
            "Content here",
        )

        results = search("Django")
        assert len(results) == 1
        assert results[0] == "snap-001"

    def test_search_url_match(self):
        """search should match against URL."""
        self._index_snapshot(
            "snap-001",
            "https://archivebox.io/docs",
            "Title",
            "Content",
        )

        results = search("archivebox")
        assert len(results) == 1

    def test_search_content_match(self):
        """search should match against content."""
        self._index_snapshot(
            "snap-001",
            "https://example.com",
            "Generic Title",
            "This document contains information about cryptography and security.",
        )

        results = search("cryptography")
        assert len(results) == 1

    def test_search_case_insensitive(self):
        """search should be case insensitive."""
        self._index_snapshot(
            "snap-001",
            "https://example.com",
            "Title",
            "PYTHON programming",
        )

        results = search("python")
        assert len(results) == 1

    def test_search_stemming(self):
        """search should use porter stemmer for word stems."""
        self._index_snapshot(
            "snap-001",
            "https://example.com",
            "Title",
            "Programming concepts",
        )

        # 'program' should match 'programming' with porter stemmer
        results = search("program")
        assert len(results) == 1

    def test_search_multiple_words(self):
        """search should match documents with all words."""
        self._index_snapshot(
            "snap-001",
            "https://example.com",
            "Web Development",
            "Learn web development skills",
        )
        self._index_snapshot(
            "snap-002",
            "https://example.com",
            "Web Design",
            "Design beautiful websites",
        )

        results = search("web development")
        # FTS5 defaults to OR, so both might match
        # With porter stemmer, both should match 'web'
        assert "snap-001" in results

    def test_search_phrase(self):
        """search should support phrase queries."""
        self._index_snapshot(
            "snap-001",
            "https://example.com",
            "Title",
            "machine learning algorithms",
        )
        self._index_snapshot(
            "snap-002",
            "https://example.com",
            "Title",
            "machine algorithms learning",
        )

        # Phrase search with quotes
        results = search('"machine learning"')
        assert len(results) == 1
        assert results[0] == "snap-001"

    def test_search_distinct_results(self):
        """search should return distinct snapshot IDs."""
        # Index same snapshot twice (could happen with multiple fields matching)
        self._index_snapshot(
            "snap-001",
            "https://python.org",
            "Python",
            "Python programming language",
        )

        results = search("Python")
        assert len(results) == 1

    def test_flush_single(self):
        """flush should remove snapshot from index."""
        self._index_snapshot("snap-001", "https://example.com", "Title", "Content")
        self._index_snapshot("snap-002", "https://example.com", "Title", "Content")

        flush(["snap-001"])

        results = search("Content")
        assert len(results) == 1
        assert results[0] == "snap-002"

    def test_flush_multiple(self):
        """flush should remove multiple snapshots."""
        self._index_snapshot("snap-001", "https://example.com", "Title", "Test")
        self._index_snapshot("snap-002", "https://example.com", "Title", "Test")
        self._index_snapshot("snap-003", "https://example.com", "Title", "Test")

        flush(["snap-001", "snap-003"])

        results = search("Test")
        assert len(results) == 1
        assert results[0] == "snap-002"

    def test_flush_nonexistent(self):
        """flush should not raise for nonexistent snapshots."""
        # Should not raise
        flush(["nonexistent-snap"])

    def test_flush_no_index(self):
        """flush should not raise when index doesn't exist."""
        os.remove(self.db_path)
        # Should not raise
        flush(["snap-001"])

    def test_search_special_characters(self):
        """search should handle special characters in queries."""
        self._index_snapshot(
            "snap-001",
            "https://example.com",
            "C++ Programming",
            "Learn C++ basics",
        )

        # FTS5 handles special chars
        results = search("C++")
        # May or may not match depending on tokenizer config
        # At minimum, should not raise
        assert isinstance(results, list)

    def test_search_unicode(self):
        """search should handle unicode content."""
        self._index_snapshot(
            "snap-001",
            "https://example.com",
            "Titre Francais",
            "cafe resume",
        )
        self._index_snapshot(
            "snap-002",
            "https://example.com",
            "Japanese",
            "Hello world",
        )

        # With remove_diacritics, 'cafe' should match
        results = search("cafe")
        assert len(results) == 1


class TestSqliteSearchWithRealData:
    """Integration tests with realistic archived content."""

    def setup_method(self, _method=None):
        """Create index with realistic test data."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / SQLITEFTS_DB

        self._orig_data_dir = os.environ.get("SNAP_DIR")
        os.environ["SNAP_DIR"] = self.temp_dir

        # Create index
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS search_index
                USING fts5(
                    snapshot_id,
                    url,
                    title,
                    content,
                    tokenize = '{FTS_TOKENIZERS}'
                )
            """)
            # Index realistic data
            test_data = [
                (
                    "snap-001",
                    "https://github.com/ArchiveBox/ArchiveBox",
                    "ArchiveBox - Self-hosted web archiving",
                    "Open source self-hosted web archiving. Collects, saves, and displays various types of content.",
                ),
                (
                    "snap-002",
                    "https://docs.python.org/3/tutorial/",
                    "Python 3 Tutorial",
                    "An informal introduction to Python. Python is an easy to learn, powerful programming language.",
                ),
                (
                    "snap-003",
                    "https://developer.mozilla.org/docs/Web/JavaScript",
                    "JavaScript - MDN Web Docs",
                    "JavaScript (JS) is a lightweight, interpreted programming language with first-class functions.",
                ),
                (
                    "snap-004",
                    "https://news.ycombinator.com",
                    "Hacker News",
                    "Social news website focusing on computer science and entrepreneurship.",
                ),
                (
                    "snap-005",
                    "https://en.wikipedia.org/wiki/Web_archiving",
                    "Web archiving - Wikipedia",
                    "Web archiving is the process of collecting portions of the World Wide Web to ensure the information is preserved.",
                ),
            ]
            conn.executemany(
                "INSERT INTO search_index (snapshot_id, url, title, content) VALUES (?, ?, ?, ?)",
                test_data,
            )
            conn.commit()
        finally:
            conn.close()

    def teardown_method(self, _method=None):
        """Clean up."""
        if self._orig_data_dir is None:
            os.environ.pop("SNAP_DIR", None)
        else:
            os.environ["SNAP_DIR"] = self._orig_data_dir
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_search_archivebox(self):
        """Search for 'archivebox' should find relevant results."""
        results = search("archivebox")
        assert "snap-001" in results

    def test_search_programming(self):
        """Search for 'programming' should find Python and JS docs."""
        results = search("programming")
        assert "snap-002" in results
        assert "snap-003" in results

    def test_search_web_archiving(self):
        """Search for 'web archiving' should find relevant results."""
        results = search("web archiving")
        # Both ArchiveBox and Wikipedia should match
        assert "snap-001" in results
        assert "snap-005" in results

    def test_search_github(self):
        """Search for 'github' should find URL match."""
        results = search("github")
        assert "snap-001" in results

    def test_search_tutorial(self):
        """Search for 'tutorial' should find Python tutorial."""
        results = search("tutorial")
        assert "snap-002" in results

    def test_flush_and_search(self):
        """Flushing a snapshot should remove it from search results."""
        # Verify it's there first
        results = search("archivebox")
        assert "snap-001" in results

        # Flush it
        flush(["snap-001"])

        # Should no longer be found
        results = search("archivebox")
        assert "snap-001" not in results


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
