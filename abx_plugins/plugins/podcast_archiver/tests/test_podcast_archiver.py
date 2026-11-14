"""Exercise the real downloader against the gallery's public podcast feed."""
import json
import os
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.base.testing import install_required_binary_from_config

PLUGIN = Path(__file__).resolve().parent.parent
HOOK = next(PLUGIN.glob('on_Snapshot__*'))


def test_live_podcast_capture(tmp_path):
    env = os.environ.copy()
    env.update(SNAP_DIR=str(tmp_path), ABXPKG_LIB_DIR=str(tmp_path / 'lib'), PODCAST_ARCHIVER_MAX_EPISODES='1', PODCAST_ARCHIVER_TIMEOUT='180')
    binary = install_required_binary_from_config(PLUGIN, 'podcast-archiver', env=env)
    assert binary and binary.loaded_abspath
    env['PODCAST_ARCHIVER_BINARY'] = str(binary.loaded_abspath)
    result = subprocess.run([sys.executable, str(HOOK), '--url=https://feeds.npr.org/510289/podcast.xml'], env=env, capture_output=True, text=True, timeout=200)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == 'Podcast Archiver ready'
    record = json.loads(lines[-1])
    assert record['status'] == 'succeeded'
    audio = tmp_path / record['output_str']
    assert audio.is_file() and audio.stat().st_size > 1000
    assert list(audio.parent.glob('*.info.json'))


def test_disabled(tmp_path):
    env = os.environ.copy()
    env.update(SNAP_DIR=str(tmp_path), PODCAST_ARCHIVER_ENABLED='false')
    result = subprocess.run([sys.executable, str(HOOK), '--url=https://example.com'], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert json.loads(result.stdout.splitlines()[-1])['status'] == 'skipped'
