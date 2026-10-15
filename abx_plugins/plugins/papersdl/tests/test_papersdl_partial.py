"""A real downloader write failure must not become a successful paper capture."""

import hashlib
import json
import os
import resource
import subprocess

from abx_plugins.plugins.papersdl.tests.test_papersdl import (
    PAPERSDL_HOOK,
    parse_jsonl_output,
    require_papersdl_binary,
)


def test_partial_pdf_write_is_failed_and_preserved(tmp_path):
    binary = require_papersdl_binary()
    output = tmp_path / "papersdl"
    output.mkdir()
    previous = output / "previous.txt"
    previous.write_text("existing output must survive")
    before = hashlib.sha256(previous.read_bytes()).hexdigest()

    def limit_file_size():
        resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))

    result = subprocess.run(
        [str(PAPERSDL_HOOK), "--url=https://arxiv.org/abs/1706.03762"],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "PAPERSDL_BINARY": binary,
            "PAPERSDL_ARGS_EXTRA": json.dumps(["--providers", "arxiv"]),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        preexec_fn=limit_file_size,
        capture_output=True,
        text=True,
        timeout=120,
    )
    partial = list(output.glob("*.pdf"))
    assert len(partial) == 1, (result.stdout, result.stderr)
    data = partial[0].read_bytes()
    assert data.startswith(b"%PDF-")
    assert len(data) == 65536
    assert result.returncode == 1, (result.stdout, result.stderr)
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "failed", record
    assert hashlib.sha256(previous.read_bytes()).hexdigest() == before
