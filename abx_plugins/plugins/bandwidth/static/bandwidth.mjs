const root = document.getElementById("bandwidth-ui");
const $ = (id) => root.querySelector(`#${id}`);
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const responseOwners = new Map();
const processes = new Map(),
  previousProcesses = new Map(),
  crawls = new Map(),
  plugins = new Map(),
  interfaces = new Map(),
  domains = new Map(),
  seen = new Map();
let cursor = "",
  previous = null,
  busy = false,
  paused = false,
  totalBytes = 0,
  totalRequests = 0;
const compact = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});
const bytes = (value) => {
  let i = 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  while (value >= 1000 && i < 4) {
    value /= 1000;
    i++;
  }
  return `${Math.round(value).toLocaleString()} ${units[i]}`;
};
const duration = (seconds) =>
  seconds >= 3600
    ? `${(seconds / 3600).toFixed(1)} hr`
    : seconds >= 60
    ? `${(seconds / 60).toFixed(1)} min`
    : `${seconds.toFixed(1)} s`;
const color = (id) =>
  `hsl(${
    [...id].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 300, 0) + 150
  },78%,67%)`;
const short = (id) => id.slice(-8);
const background = (p) => /\.bg(?:\.|$)/.test(p.hook);
function el(tag, text, className) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (className) n.className = className;
  return n;
}
function group(map, id) {
  if (!map.has(id)) {
    if (map.size >= 128) {
      const cold = [...map.values()]
        .filter((g) => !g.active)
        .sort((a, b) => a.last - b.last)[0];
      if (cold) map.delete(cold.id);
    }
    map.set(id, {
      id,
      requests: 0,
      bytes: 0,
      disk: 0,
      cpu: 0,
      fg: 0,
      active: 0,
      cpuLive: 0,
      cpuSamples: 0,
      ram: 0,
      ramSamples: 0,
      peak: 0,
      last: 0,
      buckets: new Map(),
      history: [],
      hosts: new Set(),
      snapshots: new Set(),
      measured: false,
    });
  }
  return map.get(id);
}
function rates(g, now = Date.now() / 1000) {
  let requests = 0,
    size = 0;
  for (const [second, b] of g.buckets) {
    if (now - second > 60) {
      g.buckets.delete(second);
      continue;
    }
    requests += b.requests;
    size += b.bytes;
  }
  const span = 60;
  return { requests: requests / span, bytes: size / span };
}
function recordTraffic(g, event) {
  const size = event.bytes || 0;
  g.measured = true;
  g.requests++;
  g.bytes += size;
  g.last = Math.max(g.last, event.time);
  const second = Math.floor(event.time),
    bucket = g.buckets.get(second) || { requests: 0, bytes: 0 };
  bucket.requests++;
  bucket.bytes += size;
  g.buckets.set(second, bucket);
}
function ifaceFor(p) {
  if (!p.interface) return null;
  const key = `${p.interface}|${p.local_ip}|${p.wan_ip}`,
    g = group(interfaces, key);
  Object.assign(g, {
    name: p.interface,
    local: p.local_ip,
    wan: p.wan_ip,
    updated: p.network_updated,
  });
  return g;
}
function ingest(data) {
  const now = data.time,
    delta = previous ? now - previous.time : 0,
    present = new Set();
  for (const row of data.processes) {
    if (!row.snapshot) continue;
    if (row.name === "responses") responseOwners.set(row.snapshot, row);
    while (responseOwners.size > 512)
      responseOwners.delete(responseOwners.keys().next().value);
    const old = previousProcesses.get(row.id),
      fg = !background(row);
    const p = { ...processes.get(row.id), ...row };
    // Keep the last observed cumulative CPU value after the OS process exits.
    if (row.cpu == null && old?.cpu != null) p.cpu = old.cpu;
    const elapsed = row.started
      ? Math.max(0, (row.ended || now) - row.started)
      : 0;
    const cpuDelta = fg ? Math.max(0, (p.cpu || 0) - (old?.cpu || 0)) : 0;
    const diskDelta = (row.disk || 0) - (old?.disk || 0),
      fgDelta = fg ? Math.max(0, elapsed - (old?.elapsed || 0)) : 0;
    p.cpuPercent =
      fg && old?.cpu != null && row.cpu != null && delta > 0
        ? Math.max(0, ((row.cpu - old.cpu) / delta) * 100)
        : null;
    for (const [map, id] of [
      [plugins, p.name],
      [crawls, p.crawl],
    ]) {
      const g = group(map, id);
      g.disk = Math.max(0, g.disk + diskDelta);
      g.cpu += cpuDelta;
      g.fg += fgDelta;
      g.last = Math.max(g.last, p.ended || p.started || now);
      if (g.snapshots.size < 4096) g.snapshots.add(p.snapshot);
      if (map === crawls) {
        g.label = p.crawl_label;
        g.href = `../crawls/crawl/${p.crawl}/change/`;
      }
    }
    previousProcesses.set(row.id, { cpu: p.cpu, disk: row.disk || 0, elapsed });
    if (p.status === "running" || now - (p.ended || 0) < 45) {
      processes.set(p.id, p);
      present.add(p.id);
    }
  }
  for (const [id, p] of processes) {
    if (!present.has(id) && p.status === "running") {
      p.status = "exited";
      p.ended = now;
    }
    if (p.status === "exited" && now - (p.ended || 0) > 45)
      processes.delete(id);
  }
  while (processes.size > 256) processes.delete(processes.keys().next().value);
  while (previousProcesses.size > 8192)
    previousProcesses.delete(previousProcesses.keys().next().value);
  for (const map of [plugins, crawls, interfaces])
    for (const g of map.values()) {
      g.active = 0;
      g.cpuLive = 0;
      g.cpuSamples = 0;
      g.ram = 0;
      g.ramSamples = 0;
    }
  for (const p of processes.values())
    if (p.status === "running") {
      for (const [map, id] of [
        [plugins, p.name],
        [crawls, p.crawl],
      ]) {
        const g = group(map, id);
        g.active++;
        g.last = now;
        if (p.cpuPercent != null) {
          g.cpuLive += p.cpuPercent;
          g.cpuSamples++;
        }
        if (p.rss != null) {
          g.ram += p.rss;
          g.ramSamples++;
        }
      }
      const nic = ifaceFor(p);
      if (nic) {
        nic.active++;
        nic.last = now;
      }
    }
  for (const g of interfaces.values()) {
    const nic = data.interfaces.find((n) => n.id === g.name),
      old = previous?.interfaces.find((n) => n.id === g.name);
    g.rx = nic && old && delta > 0 ? Math.max(0, nic.rx - old.rx) / delta : 0;
    g.tx = nic && old && delta > 0 ? Math.max(0, nic.tx - old.tx) / delta : 0;
    if (nic) {
      const transferred = old
        ? Math.max(0, nic.rx - old.rx) + Math.max(0, nic.tx - old.tx)
        : 0;
      g.nicBytes = (g.nicBytes || 0) + transferred;
      g.nicPeak = Math.max(g.nicPeak || 0, g.rx + g.tx);
      g.nicBuckets ||= new Map();
      g.nicBuckets.set(Math.floor(now), transferred);
      for (const t of g.nicBuckets.keys())
        if (now - t > 60) g.nicBuckets.delete(t);
      g.nicMinute = [...g.nicBuckets.values()].reduce((sum, b) => sum + b, 0);
    }
  }
  const before = new Map(
    [
      ...crawls.values(),
      ...plugins.values(),
      ...interfaces.values(),
      ...domains.values(),
    ].map((g) => [g, g.bytes])
  );
  for (const event of data.events) {
    if (seen.has(event.id)) continue;
    seen.set(event.id, now);
    if (!Number.isFinite(event.time)) continue;
    totalRequests++;
    totalBytes += event.bytes || 0;
    const site = group(domains, event.domain);
    recordTraffic(site, event);
    if (site.hosts.size < 64) site.hosts.add(event.host);
    if (Number.isFinite(event.latency_ms)) site.latency = event.latency_ms;
    const owner = responseOwners.get(event.snapshot);
    if (owner) {
      recordTraffic(group(plugins, owner.name), event);
      recordTraffic(group(crawls, owner.crawl), event);
      const nic = ifaceFor(owner);
      if (nic) recordTraffic(nic, event);
    }
  }
  for (const map of [crawls, plugins, interfaces, domains])
    for (const g of map.values()) {
      const r = rates(g, now);
      if (previous && delta > 0)
        g.peak = Math.max(g.peak, (g.bytes - (before.get(g) || 0)) / delta);
      g.history.push(map === interfaces ? g.rx + g.tx : r.bytes);
      if (g.history.length > 90) g.history.shift();
    }
  for (const [id, t] of seen) if (now - t > 180) seen.delete(id);
  while (seen.size > 16384) seen.delete(seen.keys().next().value);
  previous = data;
  cursor = data.cursor || cursor;
  render();
}
function spark(g) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("spark");
  svg.setAttribute("viewBox", "0 0 240 32");
  svg.setAttribute("preserveAspectRatio", "none");
  const path = document.createElementNS(svg.namespaceURI, "path"),
    max = Math.max(1, ...g.history);
  path.setAttribute(
    "d",
    g.history
      .map(
        (v, i) =>
          `${i ? "L" : "M"}${(i * 240) / Math.max(1, g.history.length - 1)},${
            31 - (v / max) * 28
          }`
      )
      .join(" ")
  );
  svg.append(path);
  return svg;
}
function metric(label, value) {
  const box = el("div", null, "metric");
  box.append(el("output", value), el("small", label));
  return box;
}
function renderCard(card, g, kind, rank, now) {
  const r = rates(g, now),
    work = kind === "plugins" || kind === "crawls",
    nic = kind === "interfaces";
  card.style.setProperty("--color", color(g.id));
  card.style.setProperty(
    "--heat",
    String(Math.max(0.05, 1 - (now - g.last) / 60))
  );
  card.className = `card${g.active ? " live" : ""}${
    !work && !nic && now - g.last > 60 ? " cold" : ""
  }`;
  const title = el("div", null, "card-title");
  title.append(el("small", String(rank + 1).padStart(2, "0"), "rank"));
  const name = el(
    g.href ? "a" : "b",
    kind === "crawls"
      ? `#${short(g.id)}`
      : nic
      ? g.name
      : kind === "domains" && !g.id.includes(":")
      ? `*.${g.id}`
      : g.id
  );
  if (g.href) name.href = g.href;
  name.title = g.label || g.id;
  title.append(name);
  if (work)
    title.append(
      el(
        "span",
        `▤ ${compact.format(g.snapshots.size)} · ● ${g.active}`,
        "live-count"
      )
    );
  card.replaceChildren(title);
  if (nic) {
    const address = el("div", null, "addresses");
    for (const [label, value] of [
      ["Local", g.local],
      ["WAN", g.wan],
    ])
      address.append(metric(label, value || "—"));
    card.append(address);
  }
  if (g.measured || nic) {
    const stats = el("div", null, "stat-strip");
    stats.append(
      metric(nic ? "↓" : "body", bytes(nic ? g.rx : r.bytes) + "/s")
    );
    if (nic) stats.append(metric("↑", bytes(g.tx) + "/s"));
    stats.append(
      metric(nic ? "Σ NIC" : "Σ body", bytes(nic ? g.nicBytes || 0 : g.bytes)),
      metric("Σ resp", g.measured ? compact.format(g.requests) : "—")
    );
    card.append(stats);
  }
  if (work) {
    const stats = el("div", null, "work-stats");
    stats.append(
      metric("disk", bytes(g.disk)),
      metric("CPU fg", g.cpu ? `≥ ${duration(g.cpu)}` : "—")
    );
    if (g.active)
      stats.append(
        metric(
          "Ø CPU fg",
          g.cpuSamples ? `${Math.round(g.cpuLive / g.cpuSamples)}%` : "—"
        ),
        metric("Ø RAM", g.ramSamples ? bytes(g.ram / g.ramSamples) : "—")
      );
    stats.title = `FG ${duration(g.fg)}`;
    card.append(stats);
  }
  if (g.measured || nic) {
    const peak = el("div", null, "peak");
    peak.append(
      el(
        "span",
        `${Math.round(
          ((nic ? g.nicPeak || 0 : g.peak) * 8) / 1e6
        ).toLocaleString()} Mb/s peak`
      )
    );
    if (kind === "domains")
      peak.append(
        el("span", g.latency == null ? "RTT —" : `${Math.round(g.latency)} ms`)
      );
    card.append(peak);
    const table = el("div", null, "rate-table");
    for (const [unit, seconds] of [
      ["min", 60],
      ["hr", 3600],
      ["day", 86400],
    ]) {
      const row = el("div", null, "rate-row");
      row.append(
        el("small", unit),
        el(
          "span",
          g.measured
            ? `${unit === "min" ? "" : "~"}${compact.format(
                Math.round(r.requests * seconds)
              )} resp`
            : "—"
        ),
        el(
          "span",
          `${unit === "min" ? "" : "~"}${bytes(
            (nic ? (g.nicMinute || 0) / 60 : r.bytes) * seconds
          )}`
        )
      );
      table.append(row);
    }
    card.append(table, spark(g));
  }
  if (g.hosts.size) card.title = [...g.hosts].join(" · ");
  card.append(el("i", null, "heat"));
}

function render() {
  const now = Date.now() / 1000;
  for (const [id, map, limit] of [
    ["crawls", crawls, 10],
    ["plugins", plugins, 10],
    ["interfaces", interfaces, 8],
    ["domains", domains, 30],
  ]) {
    const sort = $(`${id}-sort`)?.value || "bandwidth";
    const weight = (g) =>
      sort === "cpu"
        ? g.cpu
        : sort === "disk"
        ? g.disk
        : id === "interfaces"
        ? g.rx + g.tx
        : rates(g, now).bytes;
    const groups = [...map.values()]
      .filter((g) => id !== "interfaces" || g.active || now - g.last < 120)
      .sort(
        (a, b) =>
          weight(b) - weight(a) ||
          b.bytes - a.bytes ||
          b.disk - a.disk ||
          b.cpu - a.cpu ||
          a.id.localeCompare(b.id)
      )
      .slice(0, limit);
    const parent = $(id),
      existing = new Map([...parent.children].map((n) => [n.dataset.key, n]));
    const positions = new Map(
      [...parent.children].map((n) => [
        n.dataset.key,
        n.getBoundingClientRect().top,
      ])
    );
    groups.forEach((g, i) => {
      const card = existing.get(g.id) || el("article");
      card.dataset.key = g.id;
      renderCard(card, g, id, i, now);
      parent.append(card);
      existing.delete(g.id);
    });
    for (const card of existing.values()) card.remove();
    if (!reduced)
      for (const card of parent.children) {
        const before = positions.get(card.dataset.key);
        if (before == null) continue;
        const shift = before - card.getBoundingClientRect().top;
        if (Math.abs(shift) > 2)
          card.animate(
            [
              { transform: `translateY(${shift}px)` },
              { transform: "translateY(0)" },
            ],
            { duration: 450, easing: "cubic-bezier(.2,.8,.2,1)" }
          );
      }
    $(`${id}-count`).textContent = String(
      id === "interfaces" ? groups.length : map.size
    );
  }
  $("total-requests").textContent = compact.format(totalRequests);
  $("total-bytes").textContent = bytes(totalBytes);
  $("total-rate").textContent =
    bytes([...domains.values()].reduce((s, g) => s + rates(g, now).bytes, 0)) +
    "/s";
  $("process-count").textContent = String(
    [...processes.values()].filter((p) => p.status === "running").length
  );
}
async function poll() {
  if (paused || document.hidden || busy) return;
  busy = true;
  try {
    const params = new URLSearchParams(cursor ? { after: cursor } : {});
    const tracked = [...processes.values()]
      .sort((a, b) => (b.status === "running") - (a.status === "running"))
      .slice(0, 128)
      .map((p) => p.id);
    if (tracked.length) params.set("ids", tracked.join(","));
    const response = await fetch(`data/?${params}`, {
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error(String(response.status));
    ingest(await response.json());
    $("connection").className = "";
    $("connection").ariaLabel = "Connected";
  } catch (error) {
    $("connection").className = "error";
    $("connection").ariaLabel = "Error";
    console.error(error);
  } finally {
    busy = false;
  }
}
$("play").onclick = () => {
  paused = !paused;
  $("play").textContent = paused ? "▶" : "Ⅱ";
  $("play").ariaLabel = paused ? "Resume" : "Pause";
  if (!paused) {
    previous = null;
    poll();
  }
};
$("reset").onclick = () => location.reload();
for (const control of root.querySelectorAll("select"))
  control.onchange = render;
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    previous = null;
    poll();
  }
});
setInterval(() => {
  if (document.hidden || paused) return;
  $("clock").textContent = new Date().toLocaleTimeString();
  render();
}, 1000);
setInterval(poll, Number(root.dataset.pollMs));
poll();
