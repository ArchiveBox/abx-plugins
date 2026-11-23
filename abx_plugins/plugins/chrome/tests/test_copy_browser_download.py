import os
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    get_test_env,
)


@pytest.fixture(scope="module")
def copy_command():
    env = get_test_env()
    return [
        env["NODE_BINARY"],
        "-e",
        "require(process.argv[1]).copyBrowserDownload(process.argv[2], process.argv[3])",
        str(CHROME_UTILS),
    ], env


@pytest.mark.parametrize("replacing", [False, True])
def test_copy_browser_download_publishes_bytes_with_fresh_mtime(
    tmp_path,
    replacing,
    copy_command,
):
    source = tmp_path / "download"
    artifact = tmp_path / "archivewebpage.wacz"
    fixture = (
        Path(__file__).resolve().parents[4] / "tests/fixtures/replay/iframetest.wacz"
    )
    source.write_bytes(fixture.read_bytes())
    source.chmod(0o600)
    os.utime(source, (946684800, 946684800))
    if replacing:
        artifact.write_bytes(b"previous capture")
    cmd, env = copy_command
    subprocess.run(
        [*cmd, str(source), artifact.name],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    assert artifact.read_bytes() == source.read_bytes()
    assert artifact.stat().st_mtime > source.stat().st_mtime
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert set(tmp_path.iterdir()) == {source, artifact}


@pytest.mark.parametrize("replacing", [False, True])
def test_write_failure_preserves_download_and_previous_capture(
    tmp_path,
    replacing,
    copy_command,
):
    source = tmp_path / "download"
    source.write_bytes(bytes(range(256)) * 1024)
    artifact = tmp_path / "archivewebpage.wacz"
    if replacing:
        artifact.write_bytes(b"previous capture")
    cmd, env = copy_command
    # Force a real mid-copy EFBIG without filling the filesystem.
    result = subprocess.run(
        [
            "sh",
            "-c",
            'trap "" XFSZ; ulimit -f 1; exec "$@"',
            "sh",
            *cmd,
            str(source),
            artifact.name,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "EFBIG" in result.stderr
    assert source.read_bytes() == bytes(range(256)) * 1024
    if replacing:
        assert artifact.read_bytes() == b"previous capture"
    assert set(tmp_path.iterdir()) == ({source, artifact} if replacing else {source})
