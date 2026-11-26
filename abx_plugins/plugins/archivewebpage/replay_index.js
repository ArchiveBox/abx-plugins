// Inline ZIP metadata avoids serial startup requests. Archived response bodies
// use wabac's persistent payload cache and normal versioned HTTP caching.
// The viewer supplies original compressed index bytes; wabac still parses and
// verifies them. Keep this worker-local, bounded, and separate from payload IDB.
(() => {
  const indexes = new Map();
  const blocks = new Map();
  const blockSize = 256 * 1024;
  const maxBlocks = 32;
  const networkFetch = self.fetch.bind(self);
  // WARC records are often only a few KiB. A bounded read-ahead shares nearby
  // records across parallel requests instead of paying a network trip for each.
  // Large records still stream directly, and memory never grows with WACZ size.
  function loadBlock(input, init, url, index, offset) {
    const key = `${url}\n${offset}`;
    const existing = blocks.get(key);
    if (existing) {
      blocks.delete(key);
      blocks.set(key, existing);
      return existing.promise;
    }
    if (blocks.size >= maxBlocks) {
      const oldest = [...blocks].find(([, block]) => block.complete);
      if (!oldest) return null;
      blocks.delete(oldest[0]);
    }
    const end = Math.min(offset + blockSize, index.size) - 1;
    const headers = new Headers(init?.headers || input.headers);
    headers.set('Range', `bytes=${offset}-${end}`);
    const block = {complete: false};
    // A record's cancellation must not abort other records sharing this read.
    // Retain at most maxBlocks bounded reads, including those still in flight.
    block.promise = networkFetch(input, {...init, headers, signal: null}).then(async response => {
      if (response.status !== 206 || response.headers.get('Content-Range') !== `bytes ${offset}-${end}/${index.size}`) {
        await response.body?.cancel();
        throw new Error('Invalid WACZ byte range');
      }
      const etag = response.headers.get('ETag');
      const modified = response.headers.get('Last-Modified');
      if ((index.etag && etag !== index.etag) || (index.modified && modified && modified !== index.modified)) {
        index.invalid = true;
        await response.body?.cancel();
        throw new Error('WACZ changed during replay');
      }
      if (etag) index.etag = etag;
      if (modified) index.modified = modified;
      const data = new Uint8Array(end - offset + 1);
      const reader = response.body.getReader();
      let length = 0;
      try {
        for (;;) {
          const {done, value} = await reader.read();
          if (done) break;
          if (length + value.length > data.length) throw new Error('Oversized WACZ byte range');
          data.set(value, length);
          length += value.length;
        }
        if (length !== data.length) throw new Error('Incomplete WACZ byte range');
      } catch (error) {
        await reader.cancel();
        throw error;
      } finally {
        reader.releaseLock();
      }
      if (index.invalid) throw new Error('WACZ changed during replay');
      block.complete = true;
      if (!indexes.has(url)) blocks.delete(key);
      return data;
    }).catch(error => {
      // Failed reads cannot become cached successes or evict a newer attempt.
      if (blocks.get(key) === block) blocks.delete(key);
      throw error;
    });
    blocks.set(key, block);
    return block.promise;
  }
  self.addEventListener('message', (event) => {
    if (event.data?.type !== 'archivebox-replay-index') return;
    const client = new URL(event.source.url);
    const url = new URL(event.data.url, client);
    if (client.origin !== self.location.origin || url.origin !== client.origin ||
        url.pathname !== client.pathname || client.searchParams.get('preview') !== '1') return;
    const {size, ranges, version} = event.data.index;
    if (!Number.isSafeInteger(size) || size <= 0 || !Array.isArray(ranges)) return;
    let total = 0;
    const decoded = [];
    for (const [offset, encoded] of ranges) {
      const data = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
      total += data.length;
      if (!Number.isSafeInteger(offset) || offset < 0 || offset + data.length > size || total > 512 * 1024) return;
      decoded.push({offset, data});
    }
    const previous = indexes.get(url.href);
    if (previous && (previous.size !== size || previous.version !== version)) previous.invalid = true;
    indexes.delete(url.href);
    // The ZIP was inspected before rendering the viewer. Check its modification
    // time on the first network block too, even when hashes are disabled.
    const modified = /^\d+-/.test(version || '') ? new Date(Number(version.split('-')[0]) / 1e6).toUTCString() : null;
    indexes.set(url.href, previous || {size, ranges: decoded, version, modified});
    if (indexes.size > 4) indexes.delete(indexes.keys().next().value);
    for (const [key, block] of blocks) {
      // Evicted sources can still have pending reads. Keep counting those in
      // the memory bound until they finish, even across repeated navigations.
      if (block.complete && !indexes.has(key.split('\n')[0])) blocks.delete(key);
    }
    event.ports[0]?.postMessage({ready: true});
  });
  self.fetch = (input, init) => {
    const url = typeof input === 'string' ? new URL(input, self.location.href).href : input.url || input.href;
    const index = indexes.get(url);
    if (!index) return networkFetch(input, init);
    if (index.invalid) return Promise.reject(new Error('WACZ changed during replay'));
    // Wabac otherwise forces no-store even for immutable, revisioned sources.
    // Respect origin Cache-Control; never force-cache private/mutable responses.
    if (new URL(url).searchParams.has('replay-cache')) init = {...init, cache: 'default'};
    const method = (init?.method || input.method || 'GET').toUpperCase();
    const headers = new Headers(init?.headers || input.headers);
    const responseHeaders = {'Accept-Ranges': 'bytes', 'Content-Length': String(index.size), 'Content-Type': 'application/wacz'};
    if (method === 'HEAD') return Promise.resolve(new Response(null, {headers: responseHeaders}));
    const match = /^bytes=(\d*)-(\d*)$/.exec(headers.get('Range') || '');
    if (method !== 'GET' || !match || (!match[1] && !match[2])) return networkFetch(input, init);
    const start = match[1] ? Number(match[1]) : Math.max(0, index.size - Number(match[2]));
    const end = match[1] && match[2] ? Math.min(Number(match[2]), index.size - 1) : index.size - 1;
    const range = index.ranges.find(({offset, data}) => start >= offset && end < offset + data.length);
    if (end < start || !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start >= index.size) return networkFetch(input, init);
    const respond = body => {
      responseHeaders['Content-Length'] = String(body.length);
      responseHeaders['Content-Range'] = `bytes ${start}-${end}/${index.size}`;
      return new Response(body, {status: 206, headers: responseHeaders});
    };
    if (range) return Promise.resolve(respond(range.data.slice(start - range.offset, end - range.offset + 1)));
    // Only revisioned WACZ sources can reuse response bytes. Plain WARC and
    // mutable sources keep the original fetch/streaming behavior.
    if (!new URL(url).searchParams.has('replay-cache') || end - start + 1 > blockSize) return networkFetch(input, init);
    const signal = init?.signal || input.signal;
    if (signal?.aborted) return Promise.reject(signal.reason);
    const first = Math.floor(start / blockSize) * blockSize;
    const offsets = end < first + blockSize ? [first] : [first, first + blockSize];
    const reads = offsets.map(offset => loadBlock(input, init, url, index, offset));
    if (reads.some(read => !read)) {
      // The bounded pool is busy; don't queue unrelated large-page requests.
      for (const read of reads) read?.catch(() => {});
      return networkFetch(input, init);
    }
    const loaded = Promise.all(reads).then(data => {
      if (signal?.aborted) throw signal.reason;
      const body = new Uint8Array(end - start + 1);
      for (let i = 0; i < offsets.length; i++) {
        const begin = Math.max(start, offsets[i]);
        const stop = Math.min(end + 1, offsets[i] + data[i].length);
        body.set(data[i].subarray(begin - offsets[i], stop - offsets[i]), begin - start);
      }
      return respond(body);
    });
    if (!signal) return loaded;
    return new Promise((resolve, reject) => {
      const abort = () => reject(signal.reason);
      signal.addEventListener('abort', abort, {once: true});
      loaded.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
    });
  };
})();
