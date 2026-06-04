import os
import subprocess

from .test_twocaptcha import CONFIG_SCRIPT


def test_config_hook_skips_crawl_setup_when_chrome_is_snapshot_isolated(tmp_path):
    env = os.environ.copy()
    crawl_dir = tmp_path / "crawl"
    crawl_dir.mkdir()
    env.update(
        {
            "CRAWL_DIR": str(crawl_dir),
            "CHROME_ISOLATION": "snapshot",
            "TWOCAPTCHA_ENABLED": "true",
            "TWOCAPTCHA_API_KEY": "real-code-path-skip-before-browser-wait",
        },
    )

    result = subprocess.run(
        [str(CONFIG_SCRIPT), "--url=https://example.com/"],
        env=env,
        timeout=30,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 10, result.stderr or result.stdout
    assert "CHROME_ISOLATION=snapshot" in result.stderr
    assert not (crawl_dir / "chrome" / ".twocaptcha_configured").exists()
