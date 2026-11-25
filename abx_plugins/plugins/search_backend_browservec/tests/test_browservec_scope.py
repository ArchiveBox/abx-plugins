import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from abx_plugins.plugins.search_backend_browservec import search as backend


@pytest.fixture()
def native(monkeypatch, tmp_path):
    runtime = os.environ.get("BROWSERVEC_TEST_RUNTIME")
    if not runtime or not shutil.which(os.environ.get("NODE_BINARY", "node")):
        pytest.skip("Optional native BrowserVec fixture not provided")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEARCH_BACKEND_BROWSERVEC_ENABLED", "true")
    monkeypatch.setenv("SEARCH_BACKEND_BROWSERVEC_RUNTIME", runtime)
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

    cmd = [sys.executable, str(Path(backend.__file__)), "search_scope"]
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
    monkeypatch.delenv("SEARCH_BACKEND_BROWSERVEC_ENABLED", raising=False)
    assert backend.rank("sample", ["archive-id"]) == []
    assert list(tmp_path.iterdir()) == []


def test_busy_model_waits_only_within_budget(monkeypatch, tmp_path):
    import fcntl

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    model = backend.runtime_dir() / "model/onnx/model_quantized.onnx"
    model.parent.mkdir(parents=True)
    model.touch()
    invoked = []
    monkeypatch.setattr(
        backend.subprocess, "run", lambda *_a, **_kw: invoked.append(True)
    )
    with (backend.index_dir() / "model.lock").open("wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(backend.RuntimeUnavailable):
            backend.invoke({"operation": "embed", "texts": ["a"]}, 0.015)
    assert not invoked


def test_model_failure_releases_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    model = backend.runtime_dir() / "model/onnx/model_quantized.onnx"
    model.parent.mkdir(parents=True)
    model.touch()
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
