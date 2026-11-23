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
      seconds: new Map(),
      minutes: new Map(),
      hosts: new Set(),
      snapshots: new Set(),
      measured: false,
    });
  }
  return map.get(id);
}
const emptyTraffic = () => ({ requests: 0, bytes: 0, wireBytes: 0 });
function addTraffic(g, time, { requests = 0, bytes = 0, wireBytes = 0 }) {
  for (const [buckets, period] of [
    [g.seconds, 1],
    [g.minutes, 60],
  ]) {
    const timeKey = Math.floor(time / period);
    const bucket = buckets.get(timeKey) || emptyTraffic();
    bucket.requests += requests;
    bucket.bytes += bytes;
    bucket.wireBytes += wireBytes;
    buckets.set(timeKey, bucket);
    if (period === 1) g.peak = Math.max(g.peak, bucket.bytes);
  }
}
function trafficWindows(g, now) {
  const recent = emptyTraffic(),
    hour = emptyTraffic(),
    day = emptyTraffic();
  function add(total, bucket) {
    total.requests += bucket.requests;
    total.bytes += bucket.bytes;
    total.wireBytes += bucket.wireBytes;
  }
  for (const [second, bucket] of g.seconds) {
    if (now - second >= 60) g.seconds.delete(second);
    else add(recent, bucket);
  }
  const minute = Math.floor(now / 60);
  for (const [time, bucket] of g.minutes) {
    const age = minute - time;
    if (age >= 1440) g.minutes.delete(time);
    else {
      add(day, bucket);
      if (age < 60) add(hour, bucket);
    }
  }
  return [recent, hour, day];
}
function recordTraffic(g, event) {
  const size = event.bytes || 0;
  g.measured = true;
  g.requests++;
  g.bytes += size;
  g.last = Math.max(g.last, event.time);
  addTraffic(g, event.time, { requests: 1, bytes: size });
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
      if (transferred) addTraffic(g, now, { wireBytes: transferred });
    }
  }
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
  while (seen.size > 16384) seen.delete(seen.keys().next().value);
  previous = data;
  cursor = data.cursor || cursor;
  $("clock").textContent = new Date(data.time * 1000).toLocaleTimeString();
  render();
}
function badge(text, status = "skipped") {
  return el("span", text, `status-badge ${status}`);
}
function metric(label, value) {
  const item = el("div", null, "metric");
  item.append(el("dt", label), el("dd", value));
  return item;
}
function renderCard(card, g, kind, rank) {
  const work = kind === "plugins" || kind === "crawls",
    nic = kind === "interfaces";
  card.className = "bandwidth-card";
  const header = el("header");
  const name = el(
    g.href ? "a" : "strong",
    kind === "crawls"
      ? g.label || `Crawl #${short(g.id)}`
      : nic
      ? g.name
      : kind === "domains"
      ? `*.${g.id}`
      : g.id
  );
  if (g.href) name.href = g.href;
  name.title = g.id;
  header.append(el("span", String(rank + 1), "rank"), name);
  if (g.active) header.append(badge(`${g.active} running`, "started"));
  else if (work) header.append(badge("Finished", "sealed"));
  card.replaceChildren(header);
  if (work) {
    const badges = el("div", null, "badges");
    if (g.snapshots.size)
      badges.append(badge(`${g.snapshots.size.toLocaleString()} snapshots`));
    if (g.active && g.cpuSamples && g.cpuLive > 0)
      badges.append(
        badge(`${Math.round(g.cpuLive / g.cpuSamples) || "<1"}% CPU avg`)
      );
    if (g.active && g.ramSamples && g.ram > 0)
      badges.append(badge(`${bytes(g.ram / g.ramSamples)} RAM avg`));
    card.append(badges);
  }
  if (nic) {
    const addresses = el("dl", null, "addresses");
    addresses.append(
      metric("Local IP", g.local || "—"),
      metric("WAN IP", g.wan || "—")
    );
    card.append(addresses);
  }
  const metrics = el("dl", null, "metrics");
  metrics.append(
    metric(
      nic ? "↓ Receive" : "Traffic / sec",
      g.measured || nic
        ? `${bytes(nic ? g.rx : g.windows[0].bytes / 60)}/s`
        : "—"
    )
  );
  if (nic) metrics.append(metric("↑ Send", `${bytes(g.tx)}/s`));
  metrics.append(
    metric(
      nic ? "NIC total" : "Traffic total",
      g.measured || nic ? bytes(nic ? g.nicBytes || 0 : g.bytes) : "—"
    )
  );
  if (work)
    metrics.append(
      metric("Disk", bytes(g.disk)),
      metric("CPU · foreground", g.cpu ? `≥ ${duration(g.cpu)}` : "—")
    );
  card.append(metrics);
  if (g.measured || nic) {
    const summary = el("div", null, "badges");
    if (g.requests)
      summary.append(
        badge(`${g.requests.toLocaleString()} responses`, "started")
      );
    const peak = nic ? g.nicPeak || 0 : g.peak;
    if (peak > 0)
      summary.append(
        badge(`${Math.round((peak * 8) / 1e6) || "<1"} Mbps peak`)
      );
    if (g.latency != null)
      summary.append(badge(`${Math.round(g.latency) || "<1"} ms`));
    card.append(summary);
    const ratesRow = el("div", null, "rate-badges");
    g.windows.forEach((window, index) => {
      const traffic = nic ? window.wireBytes : window.bytes;
      const values = [];
      if (window.requests)
        values.push(`${window.requests.toLocaleString()} resp`);
      if (traffic) values.push(bytes(traffic));
      if (!values.length) return;
      const item = el("span", null, "rate-badge");
      item.title = ["Last minute", "Last hour", "Last 24 hours"][index];
      item.append(
        el("b", ["1m", "1h", "24h"][index]),
        el("span", values.join(" · "))
      );
      ratesRow.append(item);
    });
    if (ratesRow.childElementCount) card.append(ratesRow);
  }
}

function render() {
  const now = Date.now() / 1000;
  for (const [id, map, limit] of [
    ["crawls", crawls, 10],
    ["plugins", plugins, 10],
    ["interfaces", interfaces, 8],
    ["domains", domains, 30],
  ]) {
    for (const g of map.values()) g.windows = trafficWindows(g, now);
    const sort = $(`${id}-sort`)?.value || "bandwidth";
    const weight = (g) =>
      sort === "cpu"
        ? g.cpu
        : sort === "disk"
        ? g.disk
        : id === "interfaces"
        ? g.rx + g.tx
        : g.windows[0].bytes;
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
      renderCard(card, g, id, i);
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
    $(`${id}-count`).hidden = !groups.length;
    $(`${id}-count`).textContent = String(
      id === "interfaces" ? groups.length : map.size
    );
  }
  $("total-requests").textContent = compact.format(totalRequests);
  $("total-bytes").textContent = bytes(totalBytes);
  $("total-rate").textContent =
    bytes(
      [...domains.values()].reduce((s, g) => s + g.windows[0].bytes, 0) / 60
    ) + "/s";
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
setInterval(poll, Number(root.dataset.pollMs));
poll();
