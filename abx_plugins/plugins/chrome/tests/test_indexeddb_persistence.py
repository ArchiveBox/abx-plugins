"""Native site database schemas must survive a complete managed-browser restart."""

import json
import subprocess

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("existing_sqlite", [False, True])
def test_native_keypaths_and_rows_survive_browser_restart(
    tmp_path,
    httpserver,
    existing_sqlite,
):
    httpserver.expect_request("/").respond_with_data(
        "<title>Persistent site state</title>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(
        "",
        content_type="image/x-icon",
    )
    profile = tmp_path / "persistent-persona"
    # Existing SQLite state must stay readable when changing rollout policy.
    # Use scalar keys in that case: SQLite already loses compound key metadata.
    key_path = "type" if existing_sqlite else ["type"]
    index_key_path = "theme" if existing_sqlite else ["theme"]
    for stage in ("create", "reopen"):
        # No portable-state import on reopening: this must read the schema
        # persisted by Chrome, rather than replacing it from a saved export.
        overrides = {"PERSONAS_DIR": str(profile), "AUTH_STORAGE_FILE": ""}
        if existing_sqlite and stage == "create":
            overrides["CHROME_ARGS"] = json.dumps(
                ["--no-first-run", "--enable-features=IdbSqliteBackingStore"],
            )
        with chrome_session(
            tmp_path / stage,
            test_url=httpserver.url_for("/"),
            env_overrides=overrides,
        ) as (_, _, chrome_dir, env):
            result = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    r"""
const utils = require(process.argv[1]);
(async () => {
  const {browser,page} = await utils.connectToPage({chromeSessionDir:process.argv[2]});
  try {
    console.log(JSON.stringify(await page.evaluate(async ({stage,keyPath,indexKeyPath}) => {
      const db = await new Promise((resolve,reject) => {
        const request = indexedDB.open('native-site-state', 1);
        request.onerror = () => reject(request.error);
        request.onupgradeneeded = () => {
          if (stage !== 'create') {request.transaction.abort(); return;}
          const store = request.result.createObjectStore('accountData', {keyPath});
          store.createIndex('byTheme', indexKeyPath);
        };
        request.onsuccess = () => resolve(request.result);
      });
      if (stage === 'create') await new Promise((resolve,reject) => {
        const tx = db.transaction('accountData','readwrite');
        tx.objectStore('accountData').put({type:'settings',theme:'dark'});
        tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
      });
      const store = db.transaction('accountData').objectStore('accountData');
      const index = store.index('byTheme');
      const observedKeyPath = store.keyPath, observedIndexKeyPath = index.keyPath;
      const requestValue = request => new Promise((resolve,reject) => {
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      const [row,indexedRow,keys] = await Promise.all([
        requestValue(store.get(Array.isArray(keyPath) ? ['settings'] : 'settings')),
        requestValue(index.get(Array.isArray(indexKeyPath) ? ['dark'] : 'dark')),
        requestValue(store.getAllKeys()),
      ]);
      db.close(); return {keyPath:observedKeyPath,indexKeyPath:observedIndexKeyPath,row,indexedRow,keys};
    }, JSON.parse(process.argv[3]))));
  } finally {await browser.disconnect();}
})().catch(error => {console.error(error);process.exit(1)});
""",
                    str(CHROME_UTILS),
                    str(chrome_dir),
                    json.dumps(
                        {
                            "stage": stage,
                            "keyPath": key_path,
                            "indexKeyPath": index_key_path,
                        },
                    ),
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {
                "keyPath": key_path,
                "indexKeyPath": index_key_path,
                "row": {"type": "settings", "theme": "dark"},
                "indexedRow": {"type": "settings", "theme": "dark"},
                "keys": ["settings"] if existing_sqlite else [["settings"]],
            }, stage
        # chrome_session waits for the actual Chrome PID to exit on cleanup.
        if existing_sqlite and stage == "create":
            databases = profile / "Default" / "chrome_profile" / "Default" / "IndexedDB"
            sqlite_files = []
            for path in databases.rglob("*"):
                if path.is_file():
                    with path.open("rb") as handle:
                        if handle.read(16) == b"SQLite format 3\x00":
                            sqlite_files.append(path)
            assert sqlite_files, (
                "The existing-state fixture must use real SQLite storage"
            )
