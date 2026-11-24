"""Exercise the registered command boundary against real archive and index data."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest


PLUGINS = Path(__file__).resolve().parents[1] / "abx_plugins/plugins"


def run_candidates(
    plugin,
    tmp_path,
    candidates,
    terms,
    *,
    archive_root=None,
    streaming=False,
    **limits,
):
    manifest = json.loads((PLUGINS / plugin / "config.json").read_text())
    script, *args = manifest["commands"]["search_candidates"]
    request = {
        "terms": terms,
        "candidates": candidates,
        "limits": limits,
        "streaming": streaming,
    }
    if archive_root:
        request["archive_root"] = str(archive_root)
    env = {**os.environ, "DATA_DIR": str(tmp_path)}
    proc = subprocess.run(
        [str(PLUGINS / plugin / script), *args],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=12,
        env=env,
    )
    return {row["id"]: row for row in map(json.loads, proc.stdout.splitlines())}


def term(key, value, mode="literal"):
    return {"key": key, "value": value, "mode": mode}


@pytest.mark.parametrize("has_lookup", [True, False], ids=["current", "fts-only"])
def test_sqlite_preserves_unknowns_original_text_and_existing_rank(
    tmp_path,
    has_lookup,
):
    from abx_plugins.plugins.search_backend_sqlite.search import ensure_snapshot_lookup

    db_path = tmp_path / "search.sqlite3"
    uid = "88bf679e94374396adf703805b7a1d4e"
    dashed = "88bf679e-9437-4396-adf7-03805b7a1d4e"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE VIRTUAL TABLE search_index USING fts5(snapshot_id,url,title,content,tokenize='porter unicode61')",
        )
        db.execute(
            "INSERT INTO search_index VALUES (?, 'https://example.org/Mixed:Path', 'OnlyInTitle', 'Searching archives. Solar panel [1]')",
            (uid,),
        )
        if has_lookup:
            ensure_snapshot_lookup(db)
    before = db_path.read_bytes()
    terms = [
        term("word", "search", "word"),
        term("exact", "Solar panel"),
        term("id", uid[:12]),
        term("url", "/Mixed:Path"),
        term("title", "OnlyInTitle"),
        term("title_word", "onlyintitle", "word"),
        term("wrong", "sOLAR", "word"),
        term("lower", "solar panel [1]"),
        term("deep", "Solar", "deep"),
    ]
    rows = run_candidates(
        "search_backend_sqlite",
        tmp_path,
        [{"id": dashed}, {"id": "unindexed"}],
        terms,
    )
    assert rows[dashed]["matches"] == {
        "word": True,
        "exact": True,
        "id": True,
        "url": True,
        "title": True,
        "title_word": True,
        "wrong": False,
        "lower": True,
    }
    assert rows[dashed]["scores"]["word"] > 0
    assert rows["unindexed"]["matches"] == {}
    assert db_path.read_bytes() == before
    short = run_candidates(
        "search_backend_sqlite",
        tmp_path,
        [{"id": dashed}],
        [term("x", "Solar")],
        bytes=3,
    )
    assert short[dashed]["matches"] == {}


def test_sonic_cannot_certify_character_case_or_exact_substrings(tmp_path):
    # Unsupported operands must not require a running Sonic service.
    rows = run_candidates(
        "search_backend_sonic",
        tmp_path,
        [{"id": "one"}],
        [term("x", "Exact", "word"), term("y", "a phrase")],
    )
    assert rows["one"]["matches"] == {}


@pytest.mark.parametrize("archive_root", ["relative/archive", "/archive/../other"])
def test_invalid_archive_root_is_rejected(tmp_path, archive_root):
    with pytest.raises(subprocess.CalledProcessError, match="non-zero") as err:
        run_candidates(
            "search_backend_sqlite",
            tmp_path,
            [],
            [],
            archive_root=archive_root,
        )
    assert "absolute archive root without parent references" in err.value.stderr


def test_conflicting_operand_keys_are_rejected(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as err:
        run_candidates(
            "search_backend_sqlite",
            tmp_path,
            [{"id": "one"}],
            [term("x", "first"), term("x", "different")],
        )
    assert "unique operand keys" in err.value.stderr


def test_locked_sqlite_index_leaves_candidates_unknown(tmp_path):
    with sqlite3.connect(tmp_path / "search.sqlite3") as db:
        db.execute("CREATE TABLE existing_fixture (id INTEGER)")
        db.execute("BEGIN EXCLUSIVE")
        rows = run_candidates(
            "search_backend_sqlite",
            tmp_path,
            [{"id": "one"}],
            [term("x", "word")],
        )
        assert not rows.get("one", {}).get("matches")


def test_unreadable_sqlite_document_does_not_hide_later_candidates(tmp_path):
    from abx_plugins.plugins.search_backend_sqlite.search import ensure_snapshot_lookup

    with sqlite3.connect(tmp_path / "search.sqlite3") as db:
        db.execute(
            "CREATE VIRTUAL TABLE search_index USING fts5(snapshot_id,url,title,content)",
        )
        # FTS accepts integer storage; SQLite cannot open this field as a BLOB.
        # Keep its valid posting and still inspect the next candidate.
        db.execute("INSERT INTO search_index VALUES ('old', '', 'indexedword', 123)")
        db.execute("INSERT INTO search_index VALUES ('next', '', '', 'a:phrase')")
        ensure_snapshot_lookup(db)
    rows = run_candidates(
        "search_backend_sqlite",
        tmp_path,
        [{"id": "old"}, {"id": "next"}],
        [term("posting", "indexedword", "word"), term("literal", "a:phrase")],
    )
    assert rows["old"]["matches"] == {"posting": True}
    assert rows["next"]["matches"] == {"posting": False, "literal": True}


def test_index_scan_progresses_across_windows_and_utf8_boundaries(tmp_path):
    content = "x" * 63 + "Éclipse"
    with sqlite3.connect(tmp_path / "search.sqlite3") as db:
        db.execute(
            "CREATE VIRTUAL TABLE search_index USING fts5(snapshot_id,url,title,content)"
        )
        db.execute("INSERT INTO search_index VALUES ('snap', '', '', ?)", (content,))
    found = run_candidates(
        "search_backend_sqlite",
        tmp_path,
        [{"id": "snap"}],
        [term("a", "éclipse"), term("b", "absent")],
        streaming=True,
        bytes=64,
        per_file=32,
    )
    assert found["snap"]["matches"] == {"a": True, "b": False}
    assert found["snap"]["usage"]["bytes"] >= len(content.encode())
