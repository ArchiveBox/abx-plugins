"""Documentation runner placement preserves every code block's coverage."""

import json
import os
import subprocess
import textwrap
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_documentation_paths_follow_provider_runner_requirements():
    workflow = (REPO_ROOT / ".github/workflows/docs.yml").read_text()
    selection = textwrap.dedent(
        workflow.split("mapfile -t", 1)[1]
        .split("<<'PY'\n", 1)[1]
        .split("          PY", 1)[0],
    )
    partitions = {}
    for ugnas in ("true", "false"):
        result = subprocess.run(
            ["uv", "run", "--no-project", "python", "-c", selection],
            cwd=REPO_ROOT,
            env={**os.environ, "DOCS_UGNAS": ugnas},
            capture_output=True,
            text=True,
            check=True,
        )
        partitions[ugnas] = result.stdout.splitlines()
    expected = ["README.md", "AGENTS.md", "skills", "docs"] + [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "abx_plugins/plugins").glob("*/README.md")
    ]
    assert Counter(partitions["true"] + partitions["false"]) == Counter(expected)

    result = subprocess.run(
        ["uv", "run", "--no-project", "python", ".github/ci_test_matrix.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    matrices = dict(line.split("=", 1) for line in result.stdout.splitlines())
    required_readmes = {
        str(Path(item["path"]).parents[1] / "README.md")
        for item in json.loads(matrices["provider-matrix"])
        if item["ugnas"]
        and (REPO_ROOT / Path(item["path"]).parents[1] / "README.md").is_file()
    }
    assert set(partitions["true"]) == required_readmes
    assert "abx_plugins/plugins/tlsnotary/README.md" in partitions["false"]
