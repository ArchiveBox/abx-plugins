"""Exercise the registered command boundary against real archive and index data."""

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[1] / "abx_plugins/plugins"


def run_candidates(plugin, tmp_path, candidates, terms, **limits):
    manifest = json.loads((PLUGINS / plugin / "config.json").read_text())
    script, *args = manifest["commands"]["search_candidates"]
    request = {"terms": terms, "candidates": candidates, "limits": limits}
    proc = subprocess.run(
        [str(PLUGINS / plugin / script), *args],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=12,
        env={
            **os.environ,
            "DATA_DIR": str(tmp_path),
            "RIPGREP_BINARY": shutil.which("rg") or "rg",
        },
    )
    return {row["id"]: row for row in map(json.loads, proc.stdout.splitlines())}


def term(key, value, mode="literal"):
    return {"key": key, "value": value, "mode": mode}


def test_ripgrep_scans_only_supplied_snapshots_and_respects_literal_case_and_caps(
    tmp_path,
):
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "article.txt").write_text("Solar panel 12%_é\nAlpha:Beta")
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "leak").write_text("secretcontrol")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "filelink").symlink_to(outside / "leak")
    (tmp_path / "empty").mkdir()
    candidates = [
        {"id": "one", "path": str(root)},
        {"id": "missing", "path": str(tmp_path / "missing")},
        {"id": "empty", "path": str(tmp_path / "empty")},
    ]
    terms = [
        term("x", "solar PANEL", "deep"),
        term("y", "solar panel", "deep"),
        term("z", "12%_é", "deep"),
        term("other", "secretcontrol", "deep"),
        term("colon", "alpha:beta", "word"),
    ]
    rows = run_candidates("search_backend_ripgrep", tmp_path, candidates, terms)
    assert rows["one"]["matches"] == {
        "x": False,
        "y": True,
        "z": True,
        "other": False,
        "colon": True,
    }
    assert rows["one"]["usage"]["bytes"] == (root / "article.txt").stat().st_size
    assert rows["missing"]["matches"] == {}
    assert rows["empty"]["matches"] == {}
    short = run_candidates(
        "search_backend_ripgrep",
        tmp_path,
        candidates[:1],
        [term("a", "SOLAR"), term("b", "Sol", "deep")],
        bytes=3,
    )
    assert short["one"]["matches"] == {"b": True}
    assert short["one"]["usage"]["bytes"] == 3


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
            "INSERT INTO search_index VALUES (?, '', '', 'Searching archives. Solar panel [1]')",
            (uid,),
        )
        if has_lookup:
            ensure_snapshot_lookup(db)
    before = db_path.read_bytes()
    terms = [
        term("word", "search", "word"),
        term("exact", "Solar panel"),
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
