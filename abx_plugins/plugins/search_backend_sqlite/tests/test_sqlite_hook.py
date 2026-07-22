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
            "SEARCH_BACKEND_SQLITE_ENABLED": "true",
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


def test_hook_indexes_sibling_outputs_and_symlinks_sources(
    tmp_path: Path,
    real_html_snapshot,
) -> None:
    snapshot_dir = real_html_snapshot(tmp_path, "https://example.com", "snap-001")
    result = run_hook(snapshot_dir)

    assert result.returncode == 0, result.stderr
    assert '"status": "succeeded"' in result.stdout

    output_dir = snapshot_dir / "search_backend_sqlite"
    body_link = output_dir / "dom__output.html"
    title_link = output_dir / "title__title.txt"
    assert body_link.is_symlink()
    assert title_link.is_symlink()
    assert {path.name for path in output_dir.iterdir() if path.is_symlink()} == {
        "dom__output.html",
        "title__title.txt",
    }
    assert body_link.resolve() == (snapshot_dir / "dom" / "output.html").resolve()
    assert title_link.resolve() == (snapshot_dir / "title" / "title.txt").resolve()

    conn = sqlite3.connect(str(snapshot_dir / "search.sqlite3"))
    try:
        row = conn.execute(
            "SELECT snapshot_id, url, title, content FROM search_index",
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[:3] == ("snap-001", "https://example.com", "Example Domain")
    assert "Example Domain" in row[3]


def test_hook_without_content_skips_cleanly(tmp_path: Path) -> None:
    result = run_hook(tmp_path, snapshot_id="snap-empty")

    assert result.returncode == 0
    assert "No indexable content found" in result.stderr
    assert '"status": "noresults"' in result.stdout
    assert '"output_str": "No indexable content"' in result.stdout
    assert not (tmp_path / "search.sqlite3").exists()


def test_hook_cold_start_avoids_typed_schema_imports(
    tmp_path: Path,
    real_html_snapshot,
) -> None:
    """The per-snapshot hot path must not rebuild Jambo/Pydantic models."""
    snapshot_dir = real_html_snapshot(
        tmp_path,
        "https://example.com",
        "snap-cold-start",
    )
    (snapshot_dir / "search_backend_sqlite").mkdir(parents=True)

    env = os.environ.copy()
    env.update(
        {
            "ABX_RUNTIME": "archivebox",
            "DATA_DIR": str(snapshot_dir),
            "SNAP_DIR": str(snapshot_dir),
            "SEARCH_BACKEND_SQLITE_ENABLED": "true",
            "EXTRA_CONTEXT": json.dumps({"snapshot_id": "snap-cold-start"}),
        },
    )
    env["PYTHONPROFILEIMPORTTIME"] = "1"
    result = subprocess.run(
        [str(HOOK), "--url=https://example.com"],
        cwd=str(snapshot_dir / "search_backend_sqlite"),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    interpreter_profile_header = (
        "import time: self [us] | cumulative | imported package"
    )
    import_profiles = result.stderr.split(interpreter_profile_header)
    assert len(import_profiles) >= 2, result.stderr
    hook_import_profile = import_profiles[-1]
    assert "pydantic" not in hook_import_profile
    assert "jambo" not in hook_import_profile

    conn = sqlite3.connect(str(snapshot_dir / "search.sqlite3"))
    try:
        row = conn.execute(
            "SELECT snapshot_id, content FROM search_index",
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "snap-cold-start"
    assert "Example Domain" in row[1]
