import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from abx_plugins.plugins.search_backend_word2vec import search as backend


@pytest.fixture()
def native(monkeypatch, tmp_path):
    runtime = os.environ.get("WORD2VEC_TEST_RUNTIME")
    if not runtime or not shutil.which(os.environ.get("NODE_BINARY", "node")):
        pytest.skip("Optional native Word2Vec fixture not provided")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEARCH_BACKEND_WORD2VEC_ENABLED", "true")
    monkeypatch.setenv("SEARCH_BACKEND_WORD2VEC_MODEL", "MiniLM")
    target = backend.runtime_dir()
    target.mkdir(parents=True)
    for name in ("node_modules", "model"):
        (target / name).symlink_to(Path(runtime) / name, target_is_directory=True)
    (target / "ready").touch()
    return tmp_path


def test_scoped_model_persists_ranks_and_flushes(native):
    related = "000000ee-0202-4242-8000-000000000001"
    unrelated = "unrelated"
    outside = "outside"
    backend.index_text(related, "A physician treats an illness with medication.")
    backend.index_text(unrelated, "A mountain bike rolled downhill.")
    backend.index_text(outside, "The doctor prescribed medicine for a disease.")
    query = "The doctor prescribed medicine for a disease."
    scope = [unrelated, related.replace("-", ""), "not-indexed"]
    import json

    cmd = [sys.executable, "-m", backend.__name__, "search_scope"]
    payload = json.dumps({"query": query, "ids": scope, "limit": 1}) + "\n"
    result = subprocess.run(
        cmd, input=payload, text=True, capture_output=True, check=True
    )
    hits = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(hits) == 1
    assert hits[0]["id"] == related.replace("-", "")
    assert math.isfinite(hits[0]["score"]) and hits[0]["score"] >= 0.7
    assert backend.rank(query, [outside], 1)[0]["score"] > hits[0]["score"]
    backend.flush([related])
    assert backend.rank(query, [related.replace("-", "")], 1) == []


def test_no_model_invocation_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SEARCH_BACKEND_WORD2VEC_ENABLED", raising=False)
    assert backend.rank("sample", ["archive-id"]) == []
    assert list(tmp_path.iterdir()) == []


def test_busy_model_waits_only_within_budget(monkeypatch, tmp_path):
    import fcntl

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runtime = backend.runtime_dir()
    (runtime / "model").mkdir(parents=True)
    (runtime / "model/config.json").write_text('{"hidden_size":384}')
    (runtime / "ready").touch()
    invoked = []
    monkeypatch.setattr(
        backend.subprocess, "run", lambda *_a, **_kw: invoked.append(True)
    )
    with (backend.index_root() / "runtime.lock").open("wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(backend.RuntimeUnavailable):
            backend.invoke({"operation": "embed", "texts": ["a"]}, 0.015)
    assert not invoked


def test_model_failure_releases_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runtime = backend.runtime_dir()
    (runtime / "model").mkdir(parents=True)
    (runtime / "model/config.json").write_text('{"hidden_size":384}')
    (runtime / "ready").touch()
    calls = iter(
        [
            subprocess.TimeoutExpired("node", 0.01),
            subprocess.CompletedProcess([], 0, stdout="[]"),
        ]
    )

    def run(*_a, **_kw):
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(backend.subprocess, "run", run)
    with pytest.raises(backend.RuntimeUnavailable):
        backend.invoke({"operation": "embed", "texts": ["a"]}, 0.1)
    assert backend.invoke({"operation": "embed", "texts": ["b"]}, 0.1) == []


def test_model_switch_keeps_indexes_separate(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEARCH_BACKEND_WORD2VEC_MODEL", "MiniLM")
    minilm = backend.index_dir()
    monkeypatch.setenv("SEARCH_BACKEND_WORD2VEC_MODEL", "BrowserVec built-in")
    assert backend.index_dir() != minilm
    monkeypatch.setenv("SEARCH_BACKEND_WORD2VEC_MODEL", "Custom Hugging Face")
    monkeypatch.setenv(
        "SEARCH_BACKEND_WORD2VEC_HUGGINGFACE_MODEL", "Xenova/bge-small-en-v1.5"
    )
    custom = backend.index_dir()
    monkeypatch.setenv(
        "SEARCH_BACKEND_WORD2VEC_HUGGINGFACE_MODEL", "Xenova/all-MiniLM-L6-v2"
    )
    assert backend.index_dir() not in {minilm, custom}
    monkeypatch.setenv("SEARCH_BACKEND_WORD2VEC_HUGGINGFACE_MODEL", "../escape")
    with pytest.raises(backend.RuntimeUnavailable):
        backend.index_dir()
