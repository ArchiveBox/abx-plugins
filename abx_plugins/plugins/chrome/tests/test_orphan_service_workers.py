"""Unpacked extension churn must not retain obsolete service worker scripts."""

import json
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    get_extensions_dir,
    kill_chrome,
)
from abx_plugins.plugins.chrome.tests.test_chrome import (
    _install_test_extension,
    _isolated_test_env,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("profile_directory", ["Default", "Profile 2"])
def test_startup_removes_orphan_extension_workers_and_preserves_offline_sites(
    tmp_path,
    httpserver,
    profile_directory,
):
    httpserver.expect_request("/").respond_with_data(
        "<title>Offline site</title>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(
        "",
        content_type="image/x-icon",
    )
    httpserver.expect_request("/sw.js").respond_with_data(
        "self.addEventListener('install', event => event.waitUntil("
        "caches.open('saved').then(cache => cache.put('/offline', new Response('retained offline bytes')))));"
        "self.addEventListener('activate', event => event.waitUntil(clients.claim()));"
        "self.addEventListener('fetch', event => event.respondWith(caches.match(event.request)));",
        content_type="application/javascript",
    )
    env = _isolated_test_env(
        tmp_path,
        PLUGINS="chrome,ublock",
        CHROME_ARGS_EXTRA=json.dumps([f"--profile-directory={profile_directory}"]),
    )
    extension = _install_test_extension(Path(get_extensions_dir(env=env)), env)
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const utils = require(process.argv[1]);
const extension = JSON.parse(process.argv[2]);
const root = process.argv[3];
const origin = process.argv[4];
const profile = path.join(process.env.PERSONAS_DIR, 'Default', 'chrome_profile', process.argv[5]);
async function setEnabled(browser, id, enabled) {
  const page = await browser.newPage();
  try {
    await page.goto('chrome://extensions');
    await utils.withTimeout(() => page.evaluate(async (id, enabled) => await chrome.management.setEnabled(id, enabled), id, enabled), 10000, `management.setEnabled(${enabled}) did not complete`);
  } finally { await page.close(); }
}
async function registrations(browser) {
  const page = (await browser.pages())[0] || await browser.newPage();
  const session = await page.createCDPSession();
  const initial = new Promise(resolve => session.once('ServiceWorker.workerRegistrationUpdated', resolve));
  try {
    await session.send('ServiceWorker.enable');
    return (await utils.withTimeout(() => initial, 10000, 'Missing native registration inventory')).registrations;
  } finally { await session.detach(); }
}
(async () => {
  const retiredPath = path.join(root, 'retired-extension');
  const disabledPath = path.join(root, 'disabled-extension');
  await fs.promises.cp(extension.unpacked_path, retiredPath, {recursive: true});
  await fs.promises.cp(extension.unpacked_path, disabledPath, {recursive: true});
  let orphanId, retainedId, disabledId;
  let staleCacheBytes, cleanedCacheBytes;
  const cacheBytes = () => fs.readdirSync(path.join(profile, 'Service Worker', 'ScriptCache'), {withFileTypes: true})
    .filter(entry => entry.isFile()).reduce((sum, entry) => sum + fs.statSync(path.join(entry.parentPath, entry.name)).size, 0);
  for (let index = 0; index < 4; index++) {
    const outputDir = path.join(root, `browser-${index}`);
    const launch = index >= 2
      ? await utils.ensureChromeSession({binary: process.env.CHROME_BINARY, outputDir})
      : await utils.launchChromium({binary: process.env.CHROME_BINARY, outputDir, enableExtensionDebugging: true});
    assert.notEqual(launch.success, false, launch.error);
    const browser = await utils.connectToBrowserEndpoint(utils.resolvePuppeteerModule(), launch.cdpUrl, {defaultViewport: null});
    try {
      if (!index) {
        const session = await browser.target().createCDPSession();
        orphanId = (await session.send('Extensions.loadUnpacked', {path: retiredPath})).id;
        await session.detach();
        await browser.waitForTarget(target => target.url().startsWith(`chrome-extension://${orphanId}/`) && target.type() === 'service_worker');
        const retained = [{...extension}];
        await utils.loadUnpackedExtensionsIntoBrowser(browser, retained, 30000);
        retainedId = retained[0].id;
        assert.notEqual(orphanId, retainedId);
        const target = await browser.waitForTarget(target => target.url().startsWith(`chrome-extension://${retainedId}/`) && target.type() === 'service_worker');
        await (await target.worker()).evaluate(() => chrome.storage.local.set({retained: 'extension state'}));
        const page = await browser.newPage();
        await page.goto(origin);
        await page.evaluate(async () => {
          await navigator.serviceWorker.register('/sw.js');
          await navigator.serviceWorker.ready;
        });
        await page.close();
        const before = await registrations(browser);
        assert(before.some(item => item.scopeURL === `chrome-extension://${orphanId}/` && !item.isDeleted));
        assert(before.some(item => item.scopeURL === origin && !item.isDeleted));
      } else if (index === 1) {
        // A native restart retires the missing unpacked path from Preferences,
        // but leaves its service worker and script bytes in the profile.
        await utils.loadUnpackedExtensionsIntoBrowser(browser, [{...extension}], 30000);
        const stale = await registrations(browser);
        assert(stale.some(item => item.scopeURL === `chrome-extension://${orphanId}/` && !item.isDeleted));
        assert(!(await browser.extensions()).has(orphanId));
        for (const filename of ['Preferences', 'Secure Preferences']) {
          const preferences = path.join(profile, filename);
          const original = fs.readFileSync(preferences);
          try {
            fs.writeFileSync(preferences, '{');
            const installed = utils.readInstalledProfileExtensionIds(path.dirname(profile), [`--profile-directory=${process.argv[5]}`]);
            assert.equal(installed, null);
            assert.equal(await utils.removeOrphanExtensionServiceWorkers(browser, installed, 10000), 0);
            assert((await registrations(browser)).some(item => item.scopeURL === `chrome-extension://${orphanId}/` && !item.isDeleted));
          } finally { fs.writeFileSync(preferences, original); }
        }
      } else {
        const after = await registrations(browser);
        assert(!after.some(item => item.scopeURL === `chrome-extension://${orphanId}/` && !item.isDeleted),
          'Startup retained the service worker of an extension Chrome no longer has installed');
        assert(after.some(item => item.scopeURL === `chrome-extension://${retainedId}/` && !item.isDeleted));
        assert(after.some(item => item.scopeURL === origin && !item.isDeleted));
        const target = await utils.waitForExtensionTargetHandle(browser, retainedId, 10000);
        assert.deepEqual(await (await target.worker()).evaluate(() => chrome.storage.local.get('retained')), {retained: 'extension state'});
        const page = await browser.newPage();
        await page.setOfflineMode(true);
        await page.goto(new URL('/offline', origin).href);
        assert.equal(await page.evaluate(() => document.body.textContent), 'retained offline bytes');
        await page.close();
        if (index === 3) {
          // A fresh native install is not in the previously read preferences.
          // The live inventory must preserve it, including when disabled.
          const installed = utils.readInstalledProfileExtensionIds(path.dirname(profile), [`--profile-directory=${process.argv[5]}`]);
          const session = await browser.target().createCDPSession();
          disabledId = (await session.send('Extensions.loadUnpacked', {path: disabledPath})).id;
          const freshTarget = await browser.waitForTarget(target => target.url().startsWith(`chrome-extension://${disabledId}/`) && target.type() === 'service_worker');
          await (await freshTarget.worker()).evaluate(() => chrome.storage.local.set({retained: 'disabled extension state'}));
          assert.equal(await utils.removeOrphanExtensionServiceWorkers(browser, installed, 10000), 0);
          assert((await registrations(browser)).some(item => item.scopeURL === `chrome-extension://${disabledId}/` && !item.isDeleted));
          await setEnabled(browser, disabledId, false);
          const {extensions} = await session.send('Extensions.getExtensions');
          await session.detach();
          assert.equal(extensions.find(extension => extension.id === disabledId)?.enabled, false);
          const settingsPath = path.join(profile, 'Local Extension Settings', disabledId);
          const settingsBytes = () => Buffer.concat(fs.readdirSync(settingsPath).sort()
            .filter(name => fs.statSync(path.join(settingsPath, name)).isFile())
            .map(name => fs.readFileSync(path.join(settingsPath, name))));
          const stored = settingsBytes();
          assert(stored.includes(Buffer.from('disabled extension state')));
          assert.equal(await utils.removeOrphanExtensionServiceWorkers(browser, installed, 10000), 0);
          assert.equal(crypto.createHash('sha256').update(settingsBytes()).digest('hex'),
            crypto.createHash('sha256').update(stored).digest('hex'));
          const finalInventory = await browser.extensions();
          assert(finalInventory.has(disabledId));
        }
      }
    } finally {
      await utils.closeBrowserInChromeSession({outputDir, cdpUrl: launch.cdpUrl, pid: launch.pid, processIsLocal: true});
      await browser.disconnect();
    }
    if (index === 1) staleCacheBytes = cacheBytes();
    if (index === 2) {
      cleanedCacheBytes = cacheBytes();
      assert(cleanedCacheBytes < staleCacheBytes, 'Chrome did not reclaim the unregistered worker scripts');
    }
    if (index === 3) {
      console.log(JSON.stringify({orphanId, retainedId, staleCacheBytes, cleanedCacheBytes}));
    }
    if (!index) await fs.promises.rm(retiredPath, {recursive: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    try:
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                script,
                str(CHROME_UTILS),
                json.dumps(extension),
                str(tmp_path),
                httpserver.url_for("/"),
                profile_directory,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        for pid_file in tmp_path.glob("browser-*/chrome.pid"):
            kill_chrome(int(pid_file.read_text().strip()))
    assert result.returncode == 0, result.stderr
    assert (
        json.loads(result.stdout)["orphanId"] != json.loads(result.stdout)["retainedId"]
    )
