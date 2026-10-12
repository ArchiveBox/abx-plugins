// Transport cache for ZIP metadata only. WARC payloads always use real fetch.
// The viewer supplies original compressed index bytes; wabac still parses and
// verifies them. Keep this worker-local, bounded, and separate from payload IDB.
(() => {
  const indexes = new Map();
  const networkFetch = self.fetch.bind(self);
  self.addEventListener('message', (event) => {
    if (event.data?.type !== 'archivebox-replay-index') return;
    const client = new URL(event.source.url);
    const url = new URL(event.data.url, client);
    if (client.origin !== self.location.origin || url.origin !== client.origin ||
        url.pathname !== client.pathname || client.searchParams.get('preview') !== '1') return;
    const {size, ranges} = event.data.index;
    if (!Number.isSafeInteger(size) || size <= 0 || !Array.isArray(ranges)) return;
    let total = 0;
    const decoded = [];
    for (const [offset, encoded] of ranges) {
      const data = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
      total += data.length;
      if (!Number.isSafeInteger(offset) || offset < 0 || offset + data.length > size || total > 512 * 1024) return;
      decoded.push({offset, data});
    }
    indexes.delete(url.href);
    indexes.set(url.href, {size, ranges: decoded});
    if (indexes.size > 4) indexes.delete(indexes.keys().next().value);
    event.ports[0]?.postMessage({ready: true});
  });
  self.fetch = (input, init) => {
    const url = typeof input === 'string' ? new URL(input, self.location.href).href : input.url || input.href;
    const index = indexes.get(url);
    if (!index) return networkFetch(input, init);
    const method = (init?.method || input.method || 'GET').toUpperCase();
    const headers = new Headers(init?.headers || input.headers);
    const responseHeaders = {'Accept-Ranges': 'bytes', 'Content-Length': String(index.size), 'Content-Type': 'application/wacz'};
    if (method === 'HEAD') return Promise.resolve(new Response(null, {headers: responseHeaders}));
    const match = /^bytes=(\d*)-(\d*)$/.exec(headers.get('Range') || '');
    if (method !== 'GET' || !match || (!match[1] && !match[2])) return networkFetch(input, init);
    const start = match[1] ? Number(match[1]) : Math.max(0, index.size - Number(match[2]));
    const end = match[1] && match[2] ? Math.min(Number(match[2]), index.size - 1) : index.size - 1;
    const range = index.ranges.find(({offset, data}) => start >= offset && end < offset + data.length);
    if (!range || end < start) return networkFetch(input, init);
    const body = range.data.slice(start - range.offset, end - range.offset + 1);
    responseHeaders['Content-Length'] = String(body.length);
    responseHeaders['Content-Range'] = `bytes ${start}-${end}/${index.size}`;
    return Promise.resolve(new Response(body, {status: 206, headers: responseHeaders}));
  };
})();
