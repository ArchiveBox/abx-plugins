"""Real pinned CSS-tree regressions, without browser globals or saved captures."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_extension_install_env,
)
from abx_plugins.plugins.singlefile.tests.test_singlefile_prepare import PREPARE

PLUGIN = Path(__file__).parents[1]


def _assert_token_stream_regression(bundle: Path, output: Path, env: dict):
    original = bundle.read_text()
    assert hashlib.sha256(original.encode()).hexdigest() == PREPARE.ORIGINAL_SHA256
    assert original.count(PREPARE.PARSER_BEFORE) == 1
    candidate = original.replace(PREPARE.PARSER_BEFORE, PREPARE.PARSER_AFTER)
    # Verify every non-parser byte, including the full native validator.
    prefix, suffix = original.split(PREPARE.PARSER_BEFORE)
    assert candidate == prefix + PREPARE.PARSER_AFTER + suffix
    assert {
        name
        for name, value in vars(PREPARE).items()
        if name.endswith("BEFORE") and isinstance(value, str)
    } == {"PARSER_BEFORE"}
    prepared = candidate
    assert hashlib.sha256(prepared.encode()).hexdigest() == PREPARE.PATCHED_SHA256
    native = json.loads(
        (PLUGIN / "tests/fixtures/font_validation_candidates.json").read_text(),
    )["TIMER_BEFORE"]
    assert original.count(native) == prepared.count(native) == 1
    output.mkdir(parents=True, exist_ok=True)
    notices = "\n".join(re.findall(r"/\*.*?\*/", original, re.S)) + "\n"
    notices += "// Modified TokenStream: exact 32-bit offsets, token-count buffers.\n"
    for name, source in (("original", original), ("candidate", prepared)):
        assert source.count("pf=Object.create") == source.count(";const MT=") == 1
        start = source.index("pf=Object.create")
        end = source.index(";const MT=", start)
        (output / f"{name}.mjs").write_text(
            notices + "var " + source[start:end] + ";export default DT;\n",
        )
    (output / "LICENSE").write_bytes((PLUGIN / "patches/css-tree/LICENSE").read_bytes())
    (output / "regression.mjs").write_bytes(
        (PLUGIN / "tests/fixtures/token_stream.mjs").read_bytes(),
    )
    results = []
    for variant in ("original", "candidate"):
        result = subprocess.run(
            [env["NODE_BINARY"], str(output / "regression.mjs"), variant],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if variant == "original":
            assert (
                result.returncode != 0
                and "32-bit token offsets must not wrap" in result.stderr
            ), result.stderr
        else:
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout)
            assert report["apiAssertions"] == 53408
            assert report["reuseCases"] == 3000 and report["reuseTokens"] == 274170
            assert report["boundaryTokens"] == 41 and report["sparseBytes"] < 200_000
            assert report["denseBytes"] == 3_600_012
        results.append(result)
    return results


def test_real_singlefile_token_stream(tmp_path, singlefile_install_state):
    env, _ = chrome_extension_install_env(tmp_path)
    _assert_token_stream_regression(
        singlefile_install_state["unpacked_path"] / "lib/single-file.js",
        tmp_path / "parser",
        env,
    )
