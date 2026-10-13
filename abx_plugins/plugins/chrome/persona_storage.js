const fs = require("fs");

function readStorageState(filename) {
  if (!filename) return { origins: [], tabs: [] };
  const auth = JSON.parse(fs.readFileSync(filename, "utf8"));
  const origins = new Map((auth.origins || []).map(entry => [entry.origin, entry]));
  // Native persona exports before the portable origins format used origin maps.
  for (const key of ["localStorage", "sessionStorage"]) {
    for (const [origin, values] of Object.entries(auth[key] || {})) {
      const entry = origins.get(origin) || { origin };
      if (entry[key] === undefined && !(key === "sessionStorage" && auth.tabs?.some(tab => new URL(tab.url).origin === origin))) {
        entry[key] = Object.entries(values).map(([name, value]) => ({ name, value }));
      }
      origins.set(origin, entry);
    }
  }
  const tabs = auth.tabs || [];
  if (!Array.isArray(tabs)) throw new Error("Auth storage tabs must be an array");
  for (const entry of origins.values()) {
    const url = new URL(entry.origin);
    if (!["http:", "https:"].includes(url.protocol) || url.origin !== entry.origin) throw new Error("Invalid auth storage origin");
    for (const key of ["localStorage", "sessionStorage"]) {
      if (entry[key] !== undefined && (!Array.isArray(entry[key]) || entry[key].some(item =>
        typeof item.name !== "string" || typeof item.value !== "string"))) throw new Error(`Invalid auth ${key}`);
    }
    if (entry.indexedDB !== undefined && (!Array.isArray(entry.indexedDB) || entry.indexedDB.some(value =>
      typeof value.data !== "string" || typeof value.name !== "string" || !Number.isInteger(value.version) || !Array.isArray(value.stores)))) {
      throw new Error("IndexedDB state must contain Dexie JSON exports");
    }
  }
  for (const tab of tabs) {
    if (!/^https?:\/\//.test(tab.url) || !Array.isArray(tab.sessionStorage) || tab.sessionStorage.some(item =>
      typeof item.name !== "string" || typeof item.value !== "string")) throw new Error("Invalid saved tab sessionStorage");
  }
  return { origins: [...origins.values()], tabs };
}

function indexedDBScripts() {
  return ["dexie", "dexie-export-import"].map(name => fs.readFileSync(require.resolve(name), "utf8")).join("\n");
}

async function restoreOriginStorage(browser, origins) {
  // Saved site storage is optional login state synced from a user's browser.
  // One site's export that IndexedDB rejects (e.g. Dexie BulkError on a record)
  // must only skip that database: failing here aborted Chrome launch and
  // stopped every crawl that uses the persona.
  for (const entry of origins) {
    if (!entry.localStorage && !entry.indexedDB?.length) continue;
    const page = await browser.newPage();
    try {
      // A dedicated storage setup document, like Playwright storageState: no site
      // scripts or requests run before the database transaction has committed.
      // This never intercepts a user's tab or an archiving request.
      await page.setRequestInterception(true);
      page.on("request", request => request.respond({ status: 200, contentType: "text/html", body: "<!doctype html><title>Persona storage setup</title>" }));
      await page.goto(entry.origin);
      if (entry.indexedDB?.length) await page.evaluate(indexedDBScripts());
      const skipped = await page.evaluate(async (state) => {
        const skipped = [];
        for (const item of state.localStorage || []) localStorage.setItem(item.name, item.value);
        for (const exported of state.indexedDB || []) {
          try {
            const metadata = JSON.parse(exported.data);
            if (metadata.formatName !== "dexie" || metadata.formatVersion !== 1) throw new Error("Invalid IndexedDB export");
            const name = metadata.data.databaseName;
            if (name !== exported.name) throw new Error("IndexedDB name does not match its schema");
            // Validate the portable schema with Dexie's parser before replacing
            // any working data. The native schema preserves real index names.
            const schema = new Dexie(name);
            schema.version(metadata.data.databaseVersion).stores(Object.fromEntries(metadata.data.tables.map(table => [table.name, table.schema])));
            if (metadata.data.databaseVersion * 10 !== exported.version || schema.tables.length !== exported.stores.length || exported.stores.some(definition => {
              const table = schema.tables.find(table => table.name === definition.name);
              return !table || JSON.stringify(table.schema.primKey.keyPath) !== JSON.stringify(definition.keyPath) || table.schema.primKey.auto !== definition.autoIncrement ||
                table.schema.indexes.length !== definition.indexes.length || definition.indexes.some(index => !table.schema.indexes.some(parsed =>
                  JSON.stringify(parsed.keyPath) === JSON.stringify(index.keyPath) && parsed.unique === index.unique && parsed.multi === index.multiEntry));
            })) throw new Error("IndexedDB export does not match its native schema");
            schema.close();
            // A previous persona can have a different schema at the same native
            // version. onupgradeneeded will not run for it; increasing the version
            // would break the site's own indexedDB.open(name, expectedVersion).
            // Replace only this supplied database, at the saved native version.
            if ((await indexedDB.databases()).some(database => database.name === name)) {
              const matches = await new Promise((resolve, reject) => {
                const request = indexedDB.open(name);
                request.onerror = () => reject(request.error);
                request.onsuccess = () => {
                  const db = request.result;
                  try {
                    const transaction = db.objectStoreNames.length ? db.transaction([...db.objectStoreNames]) : null;
                    const matches = db.version === exported.version && exported.stores.every(definition => {
                      if (!db.objectStoreNames.contains(definition.name)) return false;
                      const store = transaction.objectStore(definition.name);
                      return JSON.stringify(store.keyPath) === JSON.stringify(definition.keyPath) && store.autoIncrement === definition.autoIncrement &&
                        store.indexNames.length === definition.indexes.length && definition.indexes.every(index => {
                          if (!store.indexNames.contains(index.name)) return false;
                          const existing = store.index(index.name);
                          return JSON.stringify(existing.keyPath) === JSON.stringify(index.keyPath) && existing.unique === index.unique && existing.multiEntry === index.multiEntry;
                        });
                    });
                    if (!matches && [...db.objectStoreNames].some(store => !exported.stores.some(definition => definition.name === store))) {
                      throw new Error("Cannot replace IndexedDB schema while preserving stores omitted from the export");
                    }
                    // close() is deferred while this schema transaction is
                    // active. Wait for it before deleting/reopening the database,
                    // otherwise our own connection can block the replacement.
                    if (transaction) {
                      transaction.oncomplete = () => { db.close(); resolve(matches); };
                      transaction.onabort = () => { db.close(); reject(transaction.error); };
                    } else { db.close(); resolve(matches); }
                  } catch (error) { db.close(); reject(error); }
                };
              });
              if (!matches) await new Promise((resolve, reject) => {
                const request = indexedDB.deleteDatabase(name);
                request.onsuccess = () => resolve();
                request.onerror = () => reject(request.error);
                request.onblocked = () => reject(new Error(`IndexedDB ${name} is in use`));
              });
            }
            // Dexie's schema notation uses key paths as index names. Recreate the
            // real native names first so sites using store.index(name) keep working.
            await new Promise((resolve, reject) => {
              const request = indexedDB.open(name, exported.version);
              request.onerror = () => reject(request.error);
              request.onblocked = () => reject(new Error(`IndexedDB ${name} is in use`));
              request.onupgradeneeded = () => {
                const db = request.result;
                for (const definition of exported.stores) {
                  const store = db.objectStoreNames.contains(definition.name)
                    ? request.transaction.objectStore(definition.name)
                    : db.createObjectStore(definition.name, { keyPath: definition.keyPath, autoIncrement: definition.autoIncrement });
                  for (const index of definition.indexes) {
                    if (!store.indexNames.contains(index.name)) store.createIndex(index.name, index.keyPath, { unique: index.unique, multiEntry: index.multiEntry });
                  }
                }
              };
              request.onsuccess = () => { request.result.close(); resolve(); };
            });
            let db;
            try {
              db = new Dexie(name);
              await db.open();
              // The export is the site's complete saved state. Merging it into the
              // profile's previous copy broke unique indexes (ConstraintError).
              const tables = metadata.data.tables.map(table => db.table(table.name));
              await db.transaction("rw", tables, async () => {
                for (const table of tables) await table.clear();
                // The JSON is already parsed. Dexie's completed JsonStream
                // input avoids asynchronous Blob reads inside this transaction.
                await db.import({ result: metadata, done: () => true, eof: () => true }, { overwriteValues: true, noTransaction: true });
              });
            } finally { db?.close(); }
          } catch (error) {
            const first = Object.values(error?.failuresByPos || {})[0] || error?.failures?.[0];
            skipped.push(`${exported.name}: ${error?.name || "Error"} ${error?.message || error}${first ? ` (${first.name}: ${first.message})` : ""}`);
          }
        }
        return skipped;
      }, entry);
      for (const reason of skipped) console.error(`[!] Skipped saved IndexedDB for ${entry.origin}: ${reason}`);
    } catch (error) {
      console.error(`[!] Skipped saved storage for ${entry.origin}: ${error?.message || error}`);
    } finally { await page.close().catch(() => {}); }
  }
}

async function captureOriginStorage(page) {
  const session = await page.createCDPSession();
  try {
    const { frameTree } = await session.send("Page.getFrameTree");
    const { executionContextId } = await session.send("Page.createIsolatedWorld", {
      frameId: frameTree.frame.id, worldName: "archivebox-persona-storage",
    });
    const result = await session.send("Runtime.evaluate", {
      contextId: executionContextId, awaitPromise: true, returnByValue: true,
      expression: `${indexedDBScripts()}\n(async () => {
        const exports = [];
        for (const database of await indexedDB.databases()) {
          const db = new Dexie(database.name);
          try {
            await db.open();
            const native = db.backendDB();
            const transaction = native.objectStoreNames.length ? native.transaction([...native.objectStoreNames]) : null;
            const stores = [...native.objectStoreNames].map(name => {
              const store = transaction.objectStore(name);
              return {name,keyPath:store.keyPath,autoIncrement:store.autoIncrement,
                indexes:[...store.indexNames].map(name => {const index=store.index(name);
                  return {name,keyPath:index.keyPath,unique:index.unique,multiEntry:index.multiEntry};})};
            });
            exports.push({name:native.name,version:native.version,stores,data:await (await db.export()).text()});
          }
          finally { db.close(); }
        }
        const entries = storage => Object.entries(storage).map(([name,value]) => ({name,value}));
        return {origin: location.origin, url: location.href, localStorage: entries(localStorage),
          sessionStorage: entries(sessionStorage), indexedDB: exports};
      })()`,
    });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || "Browser storage export failed");
    return result.result.value;
  } finally { await session.detach(); }
}

module.exports = { readStorageState, restoreOriginStorage, captureOriginStorage };
