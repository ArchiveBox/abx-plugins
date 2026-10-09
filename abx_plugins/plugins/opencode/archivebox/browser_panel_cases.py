"""Real Chrome, HTTP, UI, crash isolation, and rough preview cost measurements."""

import json
import os
import re
from pathlib import Path
import statistics
import subprocess
import threading
import time

import psutil
import pytest
import requests

from abx_plugins import get_plugins_dir
from abx_plugins.plugins.opencode.archivebox.conftest import _login_cookie

pytestmark = pytest.mark.django_db(transaction=True)


def test_agent_context_uses_real_queue_paths_and_shared_metrics(
    crawl,
    snapshot,
    admin_user,
):
    from archivebox.core.models import Snapshot
    from archivebox.crawls.models import Crawl
    from archivebox.progressmonitor.metrics import progress_metrics
    from .context import server_context

    Crawl.objects.filter(pk=crawl.pk).update(status="paused")
    Snapshot.objects.filter(pk=snapshot.pk).update(status="paused")
    deadline = time.monotonic() + 5
    while (
        metrics := progress_metrics(admin_user)
    ) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert metrics is not None
    context = server_context(crawl_id=str(crawl.pk), limit=1)
    assert context["performance"] == metrics["system"]
    assert context["crawls"] == [
        {
            "id": str(crawl.pk),
            "status": "paused",
            "label": crawl.label,
            "persona": crawl.persona.name if crawl.persona else None,
            "output_dir": str(crawl.output_dir),
            "snapshots": {"paused": 1},
        },
    ]
    assert Path(context["paths"]["collection"]).is_dir()
    assert Path(context["paths"]["system_metrics"]).is_file()
    assert not context["browsers"]
    assert "urls" not in context["crawls"][0]
    assert len(context["running_processes"]) <= 1


def test_agent_browser_panel_cost_and_isolation(
    agent_server,
    browser_runtime,
    httpserver,
    tmp_path,
):
    from archivebox.tests.conftest import run_python_cwd

    url, data_dir, server = agent_server
    httpserver.expect_request("/").respond_with_data(
        "<title>Live clock</title><body style='background:#f4f7fa;font:32px sans-serif'>"
        "<h1>Persona workspace</h1><p id='clock'></p><script>setInterval(()=>{"
        "document.querySelector('#clock').textContent=Date.now()},50)</script>",
        content_type="text/html",
    )
    httpserver.expect_request("/next").respond_with_data(
        "<title>After navigation</title><h1>Same tab, new page</h1>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("")
    out, err, code = run_python_cwd(
        """
import json
from archivebox.config.django import setup_django
setup_django(check_db=True)
from archivebox.config.common import get_config
from archivebox.personas.models import Persona
result=[]
for name in ('Preview A', 'Preview B'):
    persona=Persona.objects.create(name=name)
    persona.ensure_dirs()
    config=get_config(persona=persona).model_dump(mode='json')
    config['CHROME_HEADLESS']=True
    # Persona launch belongs to ArchiveBox even when extraction-only config
    # does not include the optional OpenCode plugin.
    config['OPENCODE_ENABLED']=False
    config['ACTIVE_PERSONA']=persona.name
    config['CHROME_USER_DATA_DIR']=str(persona.CHROME_USER_DATA_DIR)
    result.append(config)
print(json.dumps(result))
""",
        cwd=data_dir,
        timeout=60,
    )
    assert code == 0, err
    configs = json.loads(out.splitlines()[-1])
    env = {
        **os.environ,
        "ARCHIVEBOX_ABX_PLUGINS_DIR": str(get_plugins_dir()),
        "NODE_PATH": str(browser_runtime["node_path"]),
        "NODE_MODULES_DIR": str(browser_runtime["node_modules_dir"]),
        "NODE_BINARY": str(browser_runtime["node_binary"]),
        "CHROME_BINARY": str(browser_runtime["chrome_binary"]),
        "ABXPKG_LIB_DIR": str(browser_runtime["lib_dir"]),
    }
    archivebox_file = __import__("archivebox").__file__
    assert archivebox_file is not None
    open_browser = Path(archivebox_file).parent / "personas" / "open_browser.js"
    owners = []
    logs = []
    cookie = _login_cookie(url)
    headers = {"Cookie": cookie, "Origin": url}
    session = requests.Session()
    session.headers.update(headers)
    agent_html = session.get(url + "/admin/agent/", timeout=10)
    assert agent_html.status_code == 200
    csrf = re.search(r"'X-CSRFToken': '([^']+)'", agent_html.text)
    assert csrf is not None, agent_html.text
    session.headers["X-CSRFToken"] = csrf[1]
    api = url + "/admin/agent/browser/"
    timings = {
        "list_ms": [],
        "select_ms": [],
        "stream_first_byte_ms": [],
        "frame_age_ms": [],
    }
    metrics = {}

    def node(script, *args):
        result = subprocess.run(
            [
                str(browser_runtime["node_binary"]),
                "-e",
                script,
                str(Path(get_plugins_dir()) / "chrome/chrome_utils.js"),
                *map(str, args),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        return result.stdout

    def inventory():
        started = time.perf_counter()
        response = session.get(api + "list", timeout=5)
        timings["list_ms"].append(1000 * (time.perf_counter() - started))
        assert response.status_code == 200, response.text
        return response.json()

    def measure(name, seconds=5):
        # CPU seconds / wall second: 100% is one fully occupied logical core.
        roots = {
            "server": [server.pid],
            "chrome_and_owner": [owner.pid for owner in owners],
        }
        snapshots = {}

        def sample():
            values = {}
            for group, pids in roots.items():
                processes = {}
                for pid in pids:
                    try:
                        parent = psutil.Process(pid)
                        processes.update(
                            {
                                p.pid: p
                                for p in [parent, *parent.children(recursive=True)]
                            },
                        )
                    except psutil.NoSuchProcess:
                        pass
                cpu = rss = 0
                for process in processes.values():
                    try:
                        t = process.cpu_times()
                        cpu += t.user + t.system
                        rss += process.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
                values[group] = (cpu, rss / 1024**2)
            return values

        before = sample()
        start = time.perf_counter()
        deadline = start + seconds
        while time.perf_counter() < deadline:
            if name != "closed":
                inventory()
            time.sleep(0.25 if name == "closed" else 1)
        elapsed = time.perf_counter() - start
        after = sample()
        for group in roots:
            snapshots[group] = {
                "cpu_percent_one_core": round(
                    (after[group][0] - before[group][0]) / elapsed * 100,
                    2,
                ),
                "rss_mib": round(after[group][1], 1),
            }
        metrics[name] = snapshots

    stream_stop = threading.Event()
    stream_errors = []
    received = threading.Event()
    stream_response = None
    try:
        for config in configs:
            config.update(
                CHROME_BINARY=str(browser_runtime["chrome_binary"]),
                CHROME_SANDBOX=False,
            )
            log = (tmp_path / f"owner-{len(owners)}.log").open("w+")
            logs.append(log)
            owner = subprocess.Popen(
                [str(browser_runtime["node_binary"]), str(open_browser)],
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                text=True,
                env=env,
                cwd=data_dir,
            )
            assert owner.stdin is not None
            owner.stdin.write(json.dumps(config))
            owner.stdin.close()
            owners.append(owner)
        deadline = time.monotonic() + 25
        live = []
        while time.monotonic() < deadline:
            live = inventory()["browsers"]
            if len(live) == 2 and all(item["tabs"] for item in live):
                break
            assert all(owner.poll() is None for owner in owners), [
                Path(log.name).read_text() for log in logs
            ]
            time.sleep(0.1)
        assert len(live) == 2, [Path(log.name).read_text() for log in logs]
        # Use the existing unified connection helper for every client.
        output = node(
            r"""
const utils=require(process.argv[1]);
(async()=>{ const result=[];
for(const endpoint of JSON.parse(process.argv[2])) {
 const browser=await utils.connectToBrowserEndpoint(utils.resolvePuppeteerModule(),endpoint,{defaultViewport:null});
 const pages=await browser.pages(); const page=pages[0]; await page.goto(process.argv[3]);
 const second=await browser.newPage(); await second.goto(process.argv[3]+'next');
 result.push({id:utils.getTargetIdFromPage(page),second:utils.getTargetIdFromPage(second)});
 await browser.disconnect();
}console.log(JSON.stringify(result)); })().catch(e=>{console.error(e);process.exit(1)});
""",
            json.dumps([item["cdp_url"] for item in live]),
            httpserver.url_for("/"),
        )
        targets = json.loads(output)
        browser = live[0]
        target = targets[0]["id"]
        measure("closed")
        measure("inventory_only")
        for _ in range(10):
            start = time.perf_counter()
            response = session.post(
                api + "select",
                json={"id": browser["id"], "target_id": target},
                timeout=5,
            )
            timings["select_ms"].append(1000 * (time.perf_counter() - start))
            assert response.status_code == 200, response.text

        def read_stream():
            nonlocal stream_response
            try:
                start = time.perf_counter()
                with session.get(
                    api + "stream",
                    params={"id": browser["id"], "target": target},
                    stream=True,
                    timeout=5,
                ) as response:
                    stream_response = response
                    assert response.status_code == 200, response.text
                    for line in response.iter_lines(chunk_size=1):
                        if stream_stop.is_set():
                            break
                        if not line:
                            continue
                        event = json.loads(line)
                        if event["type"] == "connecting":
                            timings["stream_first_byte_ms"].append(
                                1000 * (time.perf_counter() - start),
                            )
                        if event["type"] == "unavailable":
                            raise AssertionError(event)
                        if event["type"] == "frame":
                            timings["frame_age_ms"].append(
                                time.time() * 1000 - event["captured_at"],
                            )
                            received.set()
            except Exception as exc:
                if not stream_stop.is_set():
                    stream_errors.append(str(exc))

        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()
        assert received.wait(5), {
            "errors": stream_errors,
            "timings": timings,
            "inventory": inventory(),
            "artifacts": {
                str(file): file.read_text()[:800]
                for config in configs
                for file in (
                    Path(config["CHROME_USER_DATA_DIR"]).parent / ".browser"
                ).rglob("*.json")
            },
        }
        measure("streaming")
        stream_stop.set()
        reader.join(5)
        assert not reader.is_alive()
        assert not stream_errors
        # Viewer cleanup stops frames without touching Chrome or its tabs.
        time.sleep(0.1)
        measure("disconnected")
        assert all(owner.poll() is None for owner in owners)
        assert not list(
            (
                Path(configs[0]["CHROME_USER_DATA_DIR"]).parent / ".browser" / "frames"
            ).glob("*.json"),
        )
        # Real UI plus real tab navigation and crash, with another browser alive.
        screenshot = tmp_path / "agent-browser-panel.png"
        node(
            r"""
const assert=require('node:assert/strict'),utils=require(process.argv[1]);
(async()=>{
 const data=JSON.parse(process.argv[2]);
 const browser=await utils.connectToBrowserEndpoint(utils.resolvePuppeteerModule(),data.endpoint,{defaultViewport:null});
 const {puppeteer,binary}=utils.getChromeLaunchPrerequisites();
 const ui=await utils.ensureChromeSession({...utils.getChromeSessionOptionsFromConfig({CHROME_HEADLESS:true,CHROME_SANDBOX:false,
  CHROME_USER_DATA_DIR:data.viewer+'/profile',CHROME_DOWNLOADS_DIR:data.viewer+'/downloads',CHROME_ARGS:data.chromeArgs,CHROME_RESOLUTION:'1440,900'}),
  outputDir:data.viewer,puppeteer,binary});
 const viewer=await utils.connectToBrowserEndpoint(puppeteer,ui.cdpUrl,{defaultViewport:null});
 const page=(await viewer.pages())[0];
 page.on('pageerror',error=>console.error('UI error:',String(error)));
 page.on('console',message=>{if(message.type()==='error') console.error('UI console:',message.text())});
 try {
  await page.setCookie(...data.cookie.split('; ').map(s=>{const i=s.indexOf('=');return {name:s.slice(0,i),value:s.slice(i+1),url:data.url}}));
  await page.goto(data.url+'/admin/agent/',{waitUntil:'domcontentloaded'});
  await page.bringToFront();
  console.log('UI_STATE',await page.evaluate(()=>({hidden:document.hidden,status:document.querySelector('.agent-browsers')?.innerText})));
  await page.waitForSelector('[data-browser-id="'+data.browser+'"] [data-target-id="'+data.target+'"]');
  // Remove the welcome through its real UI without needing a model provider.
  const buttons=await page.$$('button');
  for(const button of buttons) if((await button.evaluate(n=>n.textContent)).includes('Start using Agent')) await button.click();
  await page.locator('[data-browser-id="'+data.browser+'"] [data-target-id="'+data.target+'"]').click();
  await page.waitForFunction(()=>{const img=document.querySelector('.agent-browsers img');return img&&!img.hidden&&img.naturalWidth>0});
  assert.equal(await page.$$eval('[data-browser-id]',nodes=>nodes.length),2);
  await page.screenshot({path:data.screenshot});
  const exact=await browser.targets().find(target=>utils.getTargetIdFromTarget(target)===data.target).page();
  await exact.goto(data.next);
  await page.waitForFunction(()=>document.querySelector('[data-current-url]')?.textContent.endsWith('/next'));
  await page.waitForFunction(()=>{const img=document.querySelector('.agent-browsers img');return img&&!img.hidden&&img.naturalWidth>0});
  const session=await exact.createCDPSession(); session.send('Page.crash').catch(()=>{});
  await page.waitForFunction(()=>{const img=document.querySelector('.agent-browsers img');return !img||img.hidden});
  assert.equal((await page.goto(data.url+'/health/')).status(),200);
 }finally{await viewer.disconnect();await utils.closeBrowserInChromeSession({outputDir:data.viewer,puppeteer,cdpUrl:ui.cdpUrl,pid:ui.pid,processIsLocal:true});await browser.disconnect()}
})().catch(e=>{console.error(e);process.exit(1)});
""",
            json.dumps(
                {
                    "endpoint": browser["cdp_url"],
                    "cookie": cookie,
                    "url": url,
                    "browser": browser["id"],
                    "target": target,
                    "next": httpserver.url_for("/next"),
                    "screenshot": str(screenshot),
                    "viewer": str(tmp_path / "viewer"),
                    "chromeArgs": browser_runtime["chrome_args"],
                },
            ),
        )
        latest = inventory()["browsers"]
        assert len(latest) == 2
        assert any(
            tab["available"]
            for item in latest
            if item["id"] != browser["id"]
            for tab in item["tabs"]
        )
        assert not next(
            tab
            for item in latest
            if item["id"] == browser["id"]
            for tab in item["tabs"]
            if tab["target_id"] == target
        )["available"]
        assert len(timings["frame_age_ms"]) >= 10
        for key in ("list_ms", "select_ms", "stream_first_byte_ms"):
            assert max(timings[key]) < 100, (key, timings[key])
        report = {
            "metrics": metrics,
            "timings": {
                key: {
                    "count": len(values),
                    "median": round(statistics.median(values), 2),
                    "p95": round(sorted(values)[int((len(values) - 1) * 0.95)], 2),
                    "max": round(max(values), 2),
                }
                for key, values in timings.items()
            },
            "screenshot": str(screenshot),
        }
        (tmp_path / "preview-cost.json").write_text(json.dumps(report, indent=2))
        print("PREVIEW_COST=" + json.dumps(report))
    finally:
        stream_stop.set()
        if stream_response is not None:
            stream_response.close()
        for owner in owners:
            if owner.poll() is None:
                owner.terminate()
        for owner in owners:
            owner.wait(timeout=20)
        for log in logs:
            log.close()
