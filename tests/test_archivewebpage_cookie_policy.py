"""Cookie policy must affect stored WARC headers, not live authentication."""

import gzip
import json
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor

from tests.test_archivewebpage_concurrent_collections import (
    archivewebpage_crawl as archivewebpage_crawl,
    _run_start_hook,
    _run_navigate_hook,
    _run_stop_hook,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    launch_snapshot_tab,
)


def test_exported_wacz_cookie_policy_is_snapshot_local(
    request,
    httpserver_ipv4,
    tmp_path,
):
    env, crawl_chrome_dir, tab_processes = request.getfixturevalue(
        "archivewebpage_crawl",
    )
    cookie = "awp_ephemeral_auth=local-test-only"
    url = httpserver_ipv4.url_for("/authenticated")
    httpserver_ipv4.expect_request(
        "/authenticated",
        headers={"Cookie": cookie},
    ).respond_with_data(
        "<!doctype html><title>Authenticated capture</title><p>authenticated response</p>",
        content_type="text/html",
    )
    httpserver_ipv4.expect_request("/favicon.ico").respond_with_data("", status=404)
    script = r"""
const c = require(process.argv[1]);
(async () => {
  const browser = await c.resolvePuppeteerModule().connect({browserWSEndpoint: require('fs').readFileSync(process.argv[2] + '/cdp_url.txt', 'utf8').trim()});
  try {
    if (process.argv[3] === 'set') await browser.setCookie({name:'awp_ephemeral_auth',value:'local-test-only',url:process.argv[4],httpOnly:true});
    const cookies = await browser.cookies();
    process.stdout.write(JSON.stringify(cookies.filter(c => c.name === 'awp_ephemeral_auth')));
  } finally { await browser.disconnect(); }
})().catch(e => { console.error(e); process.exitCode=1; });
"""

    def cookies(action):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                script,
                str(CHROME_UTILS),
                str(crawl_chrome_dir),
                action,
                url,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    before = cookies("set")
    assert len(before) == 1
    runs = []
    for name, policy in [("default", None), ("retain", "false"), ("strip", "true")]:
        snapshot = tmp_path / name
        (snapshot / "chrome").mkdir(parents=True)
        snapshot_env = env | {"SNAP_DIR": str(snapshot)}
        if policy is not None:
            snapshot_env["ARCHIVEWEBPAGE_STRIP_COOKIES"] = policy
        tab_processes.append(
            launch_snapshot_tab(
                snapshot_chrome_dir=snapshot / "chrome",
                tab_env=snapshot_env,
                test_url=url,
                snapshot_id=name,
                crawl_id="test-cookie-policy",
            ),
        )
        runs.append((snapshot, snapshot_env))
    with ThreadPoolExecutor(max_workers=3) as executor:
        starts = list(
            executor.map(lambda run: _run_start_hook(run[0], run[1], url), runs),
        )
    for result in starts:
        assert result.returncode == 0, (result.stdout, result.stderr)
    for run in runs:
        result = _run_navigate_hook(run[0], run[1], url)
        assert result.returncode == 0, (result.stdout, result.stderr)
    for snapshot, snapshot_env in runs:
        result = _run_stop_hook(snapshot, snapshot_env, url)
        assert result.returncode == 0, (result.stdout, result.stderr)
        with zipfile.ZipFile(
            snapshot / "archivewebpage/archivewebpage.wacz",
        ) as archive:
            warc = b"".join(
                gzip.decompress(archive.read(name))
                for name in archive.namelist()
                if name.endswith(".warc.gz")
            )
        assert b"authenticated response" in warc, "Live request lost authentication"
        if snapshot.name == "retain":
            assert cookie.encode() in warc, (
                "Explicit opt-in did not retain request cookies"
            )
            assert b"\r\nx-wabac-preset-cookie:" in warc.lower()
        else:
            assert cookie.encode() not in warc, (
                "Authentication Cookie leaked into exported WACZ"
            )
            assert b"\r\ncookie:" not in warc.lower()
            assert b"\r\nx-wabac-preset-cookie:" not in warc.lower()
    assert cookies("get") == before, "Capture changed live browser cookies"
    httpserver_ipv4.check_assertions()
