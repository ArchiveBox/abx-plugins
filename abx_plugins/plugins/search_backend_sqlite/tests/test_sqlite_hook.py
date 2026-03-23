import json
import os
import sqlite3
import subprocess
from pathlib import Path


HOOK = Path(__file__).parent.parent / "on_Snapshot__90_index_sqlite.py"


def run_hook(
    tmp_path: Path,
    snapshot_id: str = "snap-001",
) -> subprocess.CompletedProcess[str]:
    output_dir = tmp_path / "search_backend_sqlite"
    output_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "ABX_RUNTIME": "archivebox",
            "DATA_DIR": str(tmp_path),
            "SNAP_DIR": str(tmp_path),
            "SEARCH_BACKEND_ENGINE": "sqlite",
            "USE_INDEXING_BACKEND": "true",
            "EXTRA_CONTEXT": json.dumps({"snapshot_id": snapshot_id}),
        },
    )
    return subprocess.run(
        [str(HOOK), "--url=https://example.com"],
        cwd=str(output_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_hook_indexes_sibling_outputs_and_symlinks_sources(tmp_path: Path) -> None:
    (tmp_path / "readability").mkdir(parents=True)
    (tmp_path / "title").mkdir(parents=True)
    (tmp_path / "readability" / "content.txt").write_text("Body text to index")
    (tmp_path / "title" / "title.txt").write_text("Example Title")

    result = run_hook(tmp_path)

    assert result.returncode == 0, result.stderr
    assert '"output_str": "1kb text indexed"' in result.stdout

    output_dir = tmp_path / "search_backend_sqlite"
    body_link = output_dir / "readability__content.txt"
    title_link = output_dir / "title__title.txt"
    assert body_link.is_symlink()
    assert title_link.is_symlink()
    assert {path.name for path in output_dir.iterdir() if path.is_symlink()} == {
        "readability__content.txt",
        "title__title.txt",
    }
    assert body_link.resolve() == (tmp_path / "readability" / "content.txt").resolve()
    assert title_link.resolve() == (tmp_path / "title" / "title.txt").resolve()

    conn = sqlite3.connect(str(tmp_path / "search.sqlite3"))
    try:
        row = conn.execute(
            "SELECT snapshot_id, url, title, content FROM search_index",
        ).fetchone()
    finally:
        conn.close()

    assert row == (
        "snap-001",
        "https://example.com",
        "Example Title",
        "Body text to index",
    )


def test_hook_without_content_skips_cleanly(tmp_path: Path) -> None:
    result = run_hook(tmp_path, snapshot_id="snap-empty")

    assert result.returncode == 0
    assert "No indexable content found" in result.stderr
    assert '"status": "noresults"' in result.stdout
    assert '"output_str": "No indexable content"' in result.stdout
    assert not (tmp_path / "search.sqlite3").exists()
