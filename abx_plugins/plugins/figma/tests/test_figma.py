"""Exercise public provider export restrictions with a real browser session."""

import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    _create_https_test_server,
    chrome_session,
)

URL = "https://www.figma.com/design/0YpAEiii3cM0l3xidTbWPk/Style-Guide-Starter--Copy---Copy-?node-id=0-1&p=f"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_figma.js"


def test_human_verification_with_empty_visible_body_has_noresults(
    tmp_path,
    tmp_path_factory,
    ensure_chrome_test_prereqs,
):
    html = (Path(__file__).parent / "fixtures" / "human-verification.html").read_text()
    server = _create_https_test_server(tmp_path_factory)
    server.expect_request(urlsplit(URL).path).respond_with_data(
        html,
        status=405,
        content_type="text/html",
    )
    server.start()
    try:
        address = urlsplit(server.url_for("/")).netloc
        with chrome_session(
            tmp_path,
            test_url=URL,
            env_overrides={
                "AUTH_STORAGE_FILE": "",
                "CHROME_CHECK_SSL_VALIDITY": "false",
                "CHROME_ARGS_EXTRA": json.dumps(
                    [
                        f"--host-resolver-rules=MAP www.figma.com {address}",
                    ],
                ),
                "FIGMA_TIMEOUT": "5",
            },
        ) as (_, _, chrome, env):
            navigation = json.loads((chrome / "navigation.json").read_text())
            assert navigation["status"] == 405
            probe = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    f"""
                    const {{connectToPage}} = require({json.dumps(str(CHROME_UTILS))});
                    (async () => {{
                        const {{browser,page}} = await connectToPage({{chromeSessionDir:process.argv[1]}});
                        try {{ console.log(JSON.stringify(await page.evaluate(() => ({{
                            title:document.title, text:document.body.innerText,
                            challenge:!!document.querySelector('#captcha-container')
                        }})))); }} finally {{ await browser.disconnect(); }}
                    }})().catch(error => {{console.error(error);process.exitCode=1;}});
                """,
                    str(chrome),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert probe.returncode == 0, probe.stderr
            assert json.loads(probe.stdout) == {
                "title": "Human Verification",
                "text": "",
                "challenge": True,
            }
            result = subprocess.run(
                [str(HOOK), f"--url={URL}"],
                cwd=chrome.parent,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stderr
            record = parse_jsonl_output(result.stdout)
            assert record and record["status"] == "noresults", result.stdout
            assert record["output_str"] == "Figma requires human verification"
            assert result.stderr.strip() == (
                "Checking Figma document: Figma requires human verification\n"
                "Figma requires human verification"
            )
            assert not (chrome.parent / "figma" / "downloads.json").exists()
    finally:
        server.stop()


def test_public_export_requires_login_has_noresults(
    tmp_path,
    ensure_chrome_test_prereqs,
):
    with chrome_session(
        tmp_path,
        test_url=URL,
        timeout=90,
        env_overrides={
            "AUTH_STORAGE_FILE": "",
            "CHROME_HEADLESS": "false",
            "PERSONAS_DIR": str(tmp_path / "personas"),
            "ACTIVE_PERSONA": "Default",
            "CHROME_USER_DATA_DIR": str(
                tmp_path / "personas" / "Default" / "chrome_profile",
            ),
        },
    ) as (_, _, chrome, env):
        result = subprocess.run(
            [str(HOOK), f"--url={URL}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
        )
        if result.returncode:
            # Retain the actual anonymous page before chrome_session tears it
            # down; a missing menu can mean a different provider login screen.
            diagnostic = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    f"""const {{connectToPage}} = require({json.dumps(str(CHROME_UTILS))});
                    (async () => {{
                        const {{browser,page}} = await connectToPage({{chromeSessionDir:process.argv[1],timeoutMs:10000}});
                        try {{ console.log(JSON.stringify(await page.evaluate(() => ({{url:location.href,title:document.title,text:document.body.innerText}})))); }}
                        finally {{ await browser.disconnect(); }}
                    }})().catch(error => {{console.error(error);process.exitCode=1;}});""",
                    str(chrome),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            (tmp_path / "figma-page.log").write_text(
                diagnostic.stdout + diagnostic.stderr,
            )
            assert result.returncode == 0, (
                result.stderr
                + "\nActual page: "
                + diagnostic.stdout
                + diagnostic.stderr
            )
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults", result.stdout
        assert record["output_str"] == "Persona must be logged in to figma.com"
        lines = result.stderr.strip().splitlines()
        assert len(lines) == 2, result.stderr
        assert lines[-1] == record["output_str"]
        assert lines[0].endswith(": " + record["output_str"]), result.stderr
        assert lines[0].startswith(("Clicking ", "Waiting for Figma downloads:")), (
            result.stderr
        )
        assert not (chrome.parent / "figma" / "downloads.json").exists()
