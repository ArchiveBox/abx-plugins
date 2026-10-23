import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.search_backend_sqlite.search import flush


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
        [
            str(HOOK),
            "--url=https://archivebox.io/tests/",
            f"--snapshot-id={snapshot_id}",
        ],
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
    snapshot_dir = real_html_snapshot(
        tmp_path,
        "https://archivebox.io/tests/",
        "snap-001",
    )
    responses = snapshot_dir / "responses"
    responses.mkdir()
    (responses / "body.txt").write_text("sharedresponseuniqueneedle")
    (snapshot_dir / "dom/response.txt").symlink_to("../responses/body.txt")
    outside = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    outside.write_text("excludedsecretuniqueneedle")
    (snapshot_dir / "dom/leak.txt").symlink_to(outside)
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
        "responses__body.txt",
        "dom__response.txt",
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
    assert row[:3] == (
        "snap-001",
        "https://archivebox.io/tests/",
        "ArchiveBox Test Page",
    )
    assert "ArchiveBox Test Page" in row[3]
    assert "excludedsecretuniqueneedle" not in row[3]
    assert "sharedresponseuniqueneedle" in row[3]
    assert (
        output_dir / "dom__response.txt"
    ).read_text() == "sharedresponseuniqueneedle"
    assert not (output_dir / "dom__leak.txt").is_symlink()


def test_hook_without_content_skips_cleanly(tmp_path: Path) -> None:
    result = run_hook(tmp_path, snapshot_id="snap-empty")

    assert result.returncode == 0
    assert "No indexable content found" in result.stderr
    assert '"status": "noresults"' in result.stdout
    assert '"output_str": "No indexable content"' in result.stdout
    assert not (tmp_path / "search.sqlite3").exists()


def test_hook_indexes_pruned_tree_and_preserves_file_and_root_aliases(
    tmp_path: Path,
) -> None:
    from abx_plugins.plugins.search_backend_sonic.tests.test_sonic_inputs import (
        write_discovery_files,
    )

    expected = write_discovery_files(tmp_path)
    result = run_hook(tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["status"] == "succeeded"
    with sqlite3.connect(tmp_path / "search.sqlite3") as conn:
        for term in set(expected.values()):
            assert conn.execute(
                "SELECT snapshot_id FROM search_index WHERE search_index MATCH ?",
                (term,),
            ).fetchall() == [("snap-001",)], term
        assert (
            conn.execute(
                "SELECT snapshot_id FROM search_index WHERE search_index MATCH 'excludedruntimeuniqueneedle'",
            ).fetchall()
            == []
        )
    links = {
        path.name: path
        for path in (tmp_path / "search_backend_sqlite").iterdir()
        if path.is_symlink()
    }
    assert set(links) == {name.replace("/", "__") for name in expected}
    for name in expected:
        assert (
            links[name.replace("/", "__")].read_bytes()
            == (tmp_path / name).read_bytes()
        )


def test_hook_upgrades_existing_index_and_replaces_exact_snapshot(
    tmp_path: Path,
) -> None:
    """Replacement/flush use a B-tree, including IDs with no searchable tokens."""
    source = tmp_path / "title" / "title.txt"
    source.parent.mkdir()
    source.write_text("Preserved article title")
    db_path = tmp_path / "search.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE search_index USING fts5(snapshot_id,url,title,content)",
        )
        conn.executemany(
            "INSERT INTO search_index VALUES (?,?,?,?)",
            [
                (sid, "https://archivebox.io/tests/", "old", body)
                for sid, body in [
                    ("---", "old duplicate one"),
                    ("---", "old duplicate two"),
                    ("snap-001", "untouched sentinel"),
                    ("SNAP 001", "another sentinel"),
                ]
            ],
        )
    for text in ("first replacement", "second replacement"):
        source.write_text(text)
        result = run_hook(tmp_path, snapshot_id="---")
        assert result.returncode == 0, result.stderr
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT title FROM search_index WHERE snapshot_id='---'",
            ).fetchall() == [(text,)]
            assert conn.execute(
                "SELECT count(*) FROM search_index_lookup",
            ).fetchone() == (3,)
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT document_rowid FROM search_index_lookup WHERE snapshot_id=?",
                ("---",),
            ).fetchall()
            assert any("SEARCH" in row[3] and "INDEX" in row[3] for row in plan), plan
    flush(["---"], environ={"DATA_DIR": str(tmp_path)})
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT snapshot_id,content FROM search_index ORDER BY snapshot_id",
        ).fetchall() == [
            ("SNAP 001", "another sentinel"),
            ("snap-001", "untouched sentinel"),
        ]
        assert conn.execute(
            "SELECT snapshot_id FROM search_index_lookup ORDER BY snapshot_id",
        ).fetchall() == [
            ("SNAP 001",),
            ("snap-001",),
        ]


@pytest.mark.parametrize("component", ["a" * 115, "a" * 120, "界" * 50])
def test_hook_indexes_long_source_paths_without_losing_links(
    tmp_path: Path,
    component: str,
) -> None:
    source_dir = tmp_path / "wget" / component / component
    source_dir.mkdir(parents=True)
    sources = {
        source_dir / "first.html": b"<p>needlealpha archived article</p>",
        source_dir / "second.html": b"<p>needlebeta another article</p>",
    }
    for source, content in sources.items():
        source.write_bytes(content)
        flattened = source.relative_to(tmp_path).as_posix().replace("/", "__")
        assert len(os.fsencode(flattened + ".rclonelink")) > 255

    previous_names = None
    for _ in range(2):
        result = run_hook(tmp_path, snapshot_id="snap-long-paths")
        assert result.returncode == 0, result.stderr
        assert '"status": "succeeded"' in result.stdout
        output_dir = tmp_path / "search_backend_sqlite"
        links = list(output_dir.iterdir())
        assert len(links) == len(sources)
        assert all(link.is_symlink() for link in links)
        assert {link.resolve() for link in links} == set(sources)
        assert all(len(os.fsencode(link.name + ".rclonelink")) <= 255 for link in links)
        names = {link.name for link in links}
        if previous_names is not None:
            assert names == previous_names
        previous_names = names
        assert {source: source.read_bytes() for source in sources} == sources

        with sqlite3.connect(tmp_path / "search.sqlite3") as conn:
            assert conn.execute("SELECT count(*) FROM search_index").fetchone() == (1,)
            for term in ("needlealpha", "needlebeta"):
                assert conn.execute(
                    "SELECT snapshot_id FROM search_index WHERE search_index MATCH ?",
                    (term,),
                ).fetchall() == [("snap-long-paths",)]


def test_hook_cold_start_avoids_typed_schema_imports(
    tmp_path: Path,
    real_html_snapshot,
) -> None:
    """The per-snapshot hot path must not rebuild Jambo/Pydantic models."""
    snapshot_dir = real_html_snapshot(
        tmp_path,
        "https://archivebox.io/tests/",
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
        [
            str(HOOK),
            "--url=https://archivebox.io/tests/",
            "--snapshot-id=snap-cold-start",
        ],
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
    assert "ArchiveBox Test Page" in row[1]
