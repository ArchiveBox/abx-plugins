// Playwright storageState's encoded value contract:
// https://github.com/microsoft/playwright/blob/main/packages/isomorphic/utilityScriptSerializers.ts
// Run only inside the isolated storage setup document created by persona_storage.js.
async function restorePlaywrightStorage(databases) {
  function decode(value, refs = new Map()) {
    if (!value || typeof value !== 'object') return value;
    if ('ref' in value) return refs.get(value.ref);
    if ('v' in value) return {undefined: undefined, null: null, NaN, Infinity, '-Infinity': -Infinity, '-0': -0}[value.v];
    if ('d' in value) return new Date(value.d);
    if ('u' in value) return new URL(value.u);
    if ('bi' in value) return BigInt(value.bi);
    if ('r' in value) return new RegExp(value.r.p, value.r.f);
    if ('e' in value) return Object.assign(new Error(value.e.m), {name: value.e.n, stack: value.e.s});
    if ('ta' in value || 'ab' in value) {
      const types = {i8:Int8Array,ui8:Uint8Array,ui8c:Uint8ClampedArray,i16:Int16Array,ui16:Uint16Array,i32:Int32Array,ui32:Uint32Array,f32:Float32Array,f64:Float64Array,bi64:BigInt64Array,bui64:BigUint64Array};
      const buffer = Uint8Array.from(atob((value.ta || value.ab).b), char => char.charCodeAt(0)).buffer;
      return value.ab ? buffer : new types[value.ta.k](buffer);
    }
    const result = value.a ? [] : value.m ? new Map() : value.s ? new Set() : {};
    refs.set(value.id, result);
    if (value.a) value.a.forEach(item => result.push(decode(item, refs)));
    else if (value.m) value.m.forEach(({k,v}) => result.set(decode(k, refs), decode(v, refs)));
    else if (value.s) value.s.forEach(item => result.add(decode(item, refs)));
    else if (value.o) value.o.forEach(({k,v}) => { Object.defineProperty(result, k, {value:decode(v, refs),enumerable:true,writable:true,configurable:true}); });
    else throw new Error('Unsupported encoded IndexedDB value');
    return result;
  }
  const keyPath = item => item.keyPathArray ?? item.keyPath ?? null;
  const failures = [];
  for (const definition of databases) {
    let db;
    try {
      if (!definition || typeof definition.name !== 'string' || !Number.isInteger(definition.version) || !Array.isArray(definition.stores)) throw new Error('Invalid IndexedDB schema');
      // Decode before opening a transaction: malformed exports must not partly
      // overwrite records in an existing database.
      const stores = definition.stores.map(store => ({...store, records:store.records.map(record => ({
        value: 'value' in record ? record.value : decode(record.valueEncoded),
        key: 'key' in record ? record.key : decode(record.keyEncoded),
      }))}));
      const existing = (await indexedDB.databases()).find(item => item.name === definition.name);
      if (existing && existing.version !== definition.version) throw new Error('Existing IndexedDB version differs');
      db = await new Promise((resolve, reject) => {
        const request = indexedDB.open(definition.name, definition.version);
        request.onerror = () => reject(request.error);
        request.onblocked = () => reject(new Error('IndexedDB is in use'));
        request.onupgradeneeded = () => {
          try {
            for (const store of stores) {
              const objectStore = request.result.createObjectStore(store.name, {keyPath:keyPath(store),autoIncrement:store.autoIncrement});
              for (const index of store.indexes) objectStore.createIndex(index.name,keyPath(index),{unique:index.unique,multiEntry:index.multiEntry});
            }
          } catch (error) { request.transaction.abort(); reject(error); }
        };
        request.onsuccess = () => resolve(request.result);
      });
      if (!stores.length) continue;
      await new Promise((resolve, reject) => {
        const transaction = db.transaction(stores.map(store => store.name), 'readwrite');
        transaction.oncomplete = resolve;
        transaction.onabort = () => reject(transaction.error || new Error('IndexedDB import aborted'));
        try {
          for (const store of stores) {
            const target = transaction.objectStore(store.name);
            if (JSON.stringify(target.keyPath) !== JSON.stringify(keyPath(store)) || target.autoIncrement !== store.autoIncrement || store.indexes.some(index => {
              if (!target.indexNames.contains(index.name)) return true;
              const saved = target.index(index.name);
              return JSON.stringify(saved.keyPath) !== JSON.stringify(keyPath(index)) || saved.unique !== index.unique || saved.multiEntry !== index.multiEntry;
            })) throw new Error('Existing IndexedDB schema differs');
            for (const record of store.records) target.put(record.value, record.key);
          }
        } catch (error) { transaction.abort(); reject(error); }
      });
    } catch (error) {
      // Values and key names can contain credentials. Return metadata only.
      failures.push('Playwright IndexedDB import failed (' + (error.name || 'Error') + ')');
    } finally { db?.close(); }
  }
  return failures;
}

module.exports = {restorePlaywrightStorage};
