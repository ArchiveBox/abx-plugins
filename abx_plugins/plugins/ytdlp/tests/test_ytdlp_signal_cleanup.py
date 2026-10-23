"""Real ffmpeg children must survive polite cleanup and remain owned at deadlines."""

import json
import os
import signal
import subprocess
import time

import psutil
import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.ytdlp.tests.test_ytdlp import (
    YTDLP_HOOK,
    _build_test_wav_bytes,
    installed_ytdlp_runtime_env as installed_ytdlp_runtime_env,
    ytdlp_runtime_env as ytdlp_runtime_env,
)


@pytest.fixture
def merging_media_url(tmp_path, httpserver, ytdlp_runtime_env):
    """Serve real DASH video/audio streams so yt-dlp invokes its native merger."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "audio.wav").write_bytes(_build_test_wav_bytes(20))
    generated = subprocess.run(
        [
            ytdlp_runtime_env["FFMPEG_BINARY"],
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10",
            "-i",
            str(media / "audio.wav"),
            "-t",
            "20",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "10",
            "-c:a",
            "aac",
            "-f",
            "dash",
            str(media / "manifest.mpd"),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert generated.returncode == 0, generated.stderr
    for path in media.iterdir():
        if path.name != "audio.wav":
            httpserver.expect_request("/" + path.name).respond_with_data(
                path.read_bytes(),
                content_type="application/dash+xml"
                if path.suffix == ".mpd"
                else "video/mp4",
            )
    return httpserver.url_for("/manifest.mpd")


@pytest.mark.parametrize("termination", ["cleanup", "hook-timeout", "hard-cancel"])
def test_ytdlp_owns_ffmpeg_merge_across_cleanup(
    tmp_path,
    merging_media_url,
    ytdlp_runtime_env,
    termination,
):
    output = tmp_path / "snapshot"
    output.mkdir()
    descendants = []
    # Real-time input reading keeps the native merger active long enough to send
    # the normal cleanup signal after ffmpeg has installed its own handlers.
    extra = ["--format", "bv+ba", "--postprocessor-args", "Merger+ffmpeg_i1:-re"]
    env = {
        **os.environ,
        **ytdlp_runtime_env,
        "SNAP_DIR": str(output),
        "YTDLP_TIMEOUT": "10" if termination == "hook-timeout" else "30",
        "YTDLP_ARGS_EXTRA": json.dumps(extra),
    }
    with subprocess.Popen(
        [str(YTDLP_HOOK), "--url", merging_media_url],
        cwd=output,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as hook:
        owner = psutil.Process(hook.pid)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and hook.poll() is None:
                # ffmpeg's version probes can exit between process-list reads.
                # The native merger's opened output identifies the actual phase.
                if list((output / "ytdlp").glob("*.temp.mp4")):
                    descendants = owner.children(recursive=True)
                    mergers = [
                        child
                        for child in descendants
                        if child.name().startswith("ffmpeg")
                    ]
                    assert len(mergers) == 1
                    assert "-re" in mergers[0].cmdline()
                    break
                time.sleep(0.01)
            else:
                stdout, stderr = hook.communicate(timeout=15)
                pytest.fail(
                    f"Real ffmpeg merger never became active: {stdout}\n{stderr}",
                )
            assert os.getpgid(mergers[0].pid) == hook.pid
            if termination != "hook-timeout":
                os.killpg(hook.pid, signal.SIGTERM)
            if termination == "hard-cancel":
                # The scheduler's final deadline must still own the entire group.
                os.killpg(hook.pid, signal.SIGKILL)
            hook.wait(timeout=25)
            _, alive = psutil.wait_procs(descendants, timeout=3)
            assert not alive, [(p.pid, p.name()) for p in alive]
            stdout, stderr = hook.communicate(timeout=5)
            if termination == "cleanup":
                assert hook.returncode == 0, stdout + stderr
                result = parse_jsonl_output(stdout)
                assert result and result["status"] == "succeeded", stdout + stderr
                saved = output / result["output_str"]
                assert saved.suffix == ".mp4" and saved.stat().st_size > 1000
                decoded = subprocess.run(
                    [
                        env["FFMPEG_BINARY"],
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(saved),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0",
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert decoded.returncode == 0, decoded.stderr
                assert not list(saved.parent.glob("*.temp.mp4"))
            elif termination == "hook-timeout":
                assert hook.returncode == 1, stdout + stderr
                result = parse_jsonl_output(stdout)
                assert result and result["status"] == "failed", stdout + stderr
                assert "Timed out after 10 seconds" in result["output_str"]
            else:
                assert hook.returncode == -signal.SIGKILL
        finally:
            if hook.poll() is None:
                os.killpg(hook.pid, signal.SIGKILL)
                hook.wait(timeout=5)
            for child in descendants:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(descendants, timeout=3)
