const root = document.getElementById("bandwidth-ui");
const $ = (id) => root.querySelector(`#${id}`);
const processes = new Map(),
  previousProcesses = new Map(),
  crawls = new Map(),
  plugins = new Map(),
  interfaces = new Map(),
  sites = new Map();
let range = root.dataset.range;
let rangeSeconds = Number(root.dataset.rangeSeconds);
let rangeLabel = root.dataset.rangeLabel;
const bucketSeconds = 60;
const historyKey = `archivebox-bandwidth-v3:${root.dataset.monitorKey}`;
const columns = { crawls, plugins, interfaces, sites };
const completed = new Set();
const observed = new Set();
const histories = [
  ["downloads", root.dataset.historyEnabled],
  ["hits", root.dataset.hitHistoryEnabled],
  ["groups", root.dataset.groupHistoryEnabled],
].filter(([, enabled]) => enabled === "true").map(([source]) => ({
  source,
  cursor: source === "groups" && rangeSeconds ? null : "",
  request: null,
  loadedRange: 0,
}));
const nearbyCards = new WeakSet();
const cardData = new WeakMap();
const cardObserver = new IntersectionObserver((entries) => {
  for (const { target, isIntersecting } of entries) {
    if (isIntersecting && cardData.has(target)) {
      nearbyCards.add(target);
      renderCard(target, ...cardData.get(target));
    } else {
      nearbyCards.delete(target);
    }
  }
}, { rootMargin: "250px" });
let savedAt = 0;
let oldest = Date.now() / 1000;
let cursor = "",
  previous = null,
  busy = false,
  totalRate = 0;
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
    map.set(id, {
      id,
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
      buckets: new Map(),
      snapshots: new Set(),
      measured: false,
      rate: 0,
      history: [],
    });
  }
  return map.get(id);
}
function emptyBucket() {
  return { bytes: 0, measured: false, hits: 0, observations: 0, rtt: null };
}
function appendBucket(bucket, amount) {
  bucket.bytes += amount.bytes ?? 0;
  bucket.measured ||= amount.measured ?? amount.bytes > 0;
  bucket.hits += amount.hits ?? 0;
  bucket.observations += amount.observations ?? 0;
  if (amount.rtt != null)
    bucket.rtt = Math.min(bucket.rtt ?? Infinity, amount.rtt);
}
function addTraffic(group, now, amount, seconds = 0) {
  group.measured ||= amount.measured ?? amount.bytes > 0;
  if (seconds > 0) group.rate = amount.bytes / seconds;
  group.peak = Math.max(group.peak, group.rate);
  if (!amount.measured && !amount.bytes && !amount.hits && !amount.observations && amount.rtt == null) return;
  oldest = Math.min(oldest, now);
  const age = Date.now() / 1000 - now;
  for (const [buckets, period] of [
    [group.buckets, bucketSeconds],
    ...(age < 3600 ? [[group.minutes, 60]] : []),
    ...(age < 60 ? [[group.seconds, 1]] : []),
  ]) {
    const key = Math.floor(now / period);
    if (!buckets.has(key)) buckets.set(key, emptyBucket());
    appendBucket(buckets.get(key), amount);
  }
}
function trafficWindows(group, now) {
  const recent = emptyBucket(),
    hour = emptyBucket(),
    selected = emptyBucket();
  for (const [second, amount] of group.seconds) {
    if (now - second >= 60) group.seconds.delete(second);
    else appendBucket(recent, amount);
  }
  const minute = Math.floor(now / 60);
  for (const [time, amount] of group.minutes) {
    const age = minute - time;
    if (age >= 60) group.minutes.delete(time);
    else appendBucket(hour, amount);
  }
  for (const [time, amount] of group.buckets) {
    if (!rangeSeconds || (time + 1) * bucketSeconds > now - rangeSeconds)
      appendBucket(selected, amount);
  }
  return [recent, hour, selected];
}
function restoreHistory() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(historyKey) || "{}");
    const now = Date.now() / 1000;
    oldest = saved.oldest ?? oldest;
    for (const id of saved.completed || []) completed.add(id);
    for (const id of saved.observed || []) observed.add(id);
    for (const [kind, map] of Object.entries(columns)) {
      for (const { id, seconds, minutes, buckets, hostnames, ...meta } of saved[kind] || []) {
        const g = group(map, id);
        Object.assign(g, meta, {
          seconds: new Map(seconds),
          minutes: new Map(minutes),
          buckets: new Map(buckets),
          hostnames: new Set(hostnames),
        });
        trafficWindows(g, now);
        if (!g.buckets.size) map.delete(id);
      }
    }
  } catch {
    // The dashboard also works when storage is unavailable.
  }
}
function saveHistory() {
  try {
    const saved = { oldest, completed: [...completed], observed: [...observed] };
    for (const [kind, map] of Object.entries(columns)) {
      saved[kind] = [...map.values()]
        .filter((g) => g.buckets.size)
        .map(({ id, label, href, favicon, seconds, minutes, buckets, hostnames, peak, last, local, wan }) => ({
          id, label, href, favicon, peak, last, local, wan,
          seconds: [...seconds],
          minutes: [...minutes],
          buckets: [...buckets],
          hostnames: [...(hostnames || [])],
        }));
    }
    sessionStorage.setItem(historyKey, JSON.stringify(saved));
  } catch {
    // Storage limits do not affect live measurements.
  }
}
function ifaceFor(flow, process) {
  if (!flow.interface) return null;
  const nic = group(interfaces, flow.interface);
  nic.local = flow.local;
  nic.wan =
    flow.interface === process.interface && flow.local === process.local_ip
      ? process.wan_ip
      : "";
  return nic;
}
function workGroups(process) {
  const groups = [group(plugins, process.name)];
  if (process.crawl) {
    const crawl = group(crawls, process.crawl);
    crawl.label = process.crawl_label;
    crawl.href = `../crawls/crawl/${process.crawl}/change/`;
    groups.push(crawl);
  }
  return groups;
}
function ingestGroups(data) {
  for (const [id, label] of data.crawls) {
    const crawl = group(crawls, id);
    crawl.label ||= label;
    crawl.href = `../crawls/crawl/${id}/change/`;
    crawl.hasHistory = true;
  }
  for (const name of data.plugins) group(plugins, name).hasHistory = true;
  for (const [name, { local, wan }] of Object.entries(data.interfaces)) {
    const nic = group(interfaces, name);
    nic.local ||= local;
    nic.wan ||= wan;
    nic.hasHistory = true;
  }
}
function ingestDownloads(data, owners) {
  for (const report of data.downloads || []) {
    const owner = owners.get(report.process);
    if (!owner || completed.has(report.process)) continue;
    completed.add(report.process);
    const groups = workGroups(owner);
    if (owner.interface) {
      const nic = ifaceFor({ interface: owner.interface, local: owner.local_ip }, owner);
      groups.push(nic);
    }
    for (const entry of report.entries) {
      const site = group(sites, entry.site);
      site.href ||= entry.site_url;
      site.favicon ||= data.favicons?.[entry.site];
      site.hostnames ||= new Set();
      if (site.hostnames.size < 12) site.hostnames.add(entry.hostname);
      const amount = { bytes: entry.bytes, hits: entry.hits, measured: true };
      for (const g of [site, ...groups]) {
        g.last = Math.max(g.last, report.time);
        addTraffic(g, report.time, amount);
      }
    }
  }
}
function ingestObservations(data) {
  for (const row of data.observations) {
    const key = `${row.snapshot}/${row.site}`;
    if (observed.has(key)) continue;
    observed.add(key);
    const site = group(sites, row.site);
    site.href ||= row.site_url;
    site.hostnames ||= new Set();
    if (site.hostnames.size < 12) site.hostnames.add(row.hostname);
    addTraffic(site, row.time, { observations: 1 });
  }
}
function ingest(data) {
  if (previous && data.time <= previous.time) return;
  const now = data.time,
    delta = previous ? now - previous.time : 0,
    present = new Set();
  for (const row of data.processes) {
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
    for (const g of workGroups(p)) {
      g.disk = Math.max(0, g.disk + diskDelta);
      g.cpu += cpuDelta;
      g.fg += fgDelta;
      g.last = Math.max(g.last, p.ended || p.started || now);
      if (p.snapshot && g.snapshots.size < 4096) g.snapshots.add(p.snapshot);
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
  for (const map of [plugins, crawls, interfaces, sites])
    for (const g of map.values()) {
      g.active = 0;
      g.cpuLive = 0;
      g.cpuSamples = 0;
      g.ram = 0;
      g.ramSamples = 0;
    }
  for (const p of processes.values())
    if (p.status === "running") {
      for (const g of workGroups(p)) {
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
    }
  const previousFlows = new Map(previous?.flows.map((flow) => [flow.id, flow]));
  const owners = new Map(
    data.processes.map((process) => [process.id, process])
  );
  const amounts = new Map();
  const liveRates = new Map();
  let liveBytes = 0;
  for (const flow of data.flows) {
    const owner = owners.get(flow.process);
    if (!owner) continue;
    const site = group(sites, flow.site || "");
    site.last = now;
    site.active++;
    site.href ||= flow.site_url;
    site.favicon ||= data.favicons?.[flow.site];
    const nic = ifaceFor(flow, owner);
    if (nic) {
      nic.active++;
      nic.last = now;
    }
    if (delta <= 0) continue;
    const old = previousFlows.get(flow.id);
    const bytes = old ? Math.max(0, flow.rx - old.rx) : 0;
    const amount = {
      bytes: owner.reports_downloads ? 0 : bytes,
      measured: Boolean(old && !owner.reports_downloads),
      hits:
        !owner.reports_downloads && flow.site && (!old || !old.site) ? 1 : 0,
      rtt: Number.isFinite(flow.rtt) && flow.rtt > 0 ? flow.rtt : null,
    };
    const related = [site, ...workGroups(owner)];
    if (nic) related.push(nic);
    for (const g of related) {
      liveRates.set(g, (liveRates.get(g) || 0) + bytes / delta);
      if (!amounts.has(g)) amounts.set(g, emptyBucket());
      appendBucket(amounts.get(g), amount);
    }
    liveBytes += bytes;
  }
  for (const map of [plugins, crawls, sites, interfaces])
    for (const g of map.values()) g.rate = 0;
  for (const [g, amount] of amounts) addTraffic(g, now, amount, delta);
  for (const [g, rate] of liveRates) {
    g.rate = rate;
    g.peak = Math.max(g.peak, rate);
  }
  ingestDownloads(data, owners);
  totalRate = delta > 0 ? liveBytes / delta : 0;

  const counters = new Map(
    data.interfaces.map((counter) => [counter.id, counter])
  );
  const previousCounters = new Map(
    previous?.interfaces.map((counter) => [counter.id, counter])
  );
  for (const g of interfaces.values()) {
    g.tx = 0;
    const current = counters.get(g.id);
    const old = previousCounters.get(g.id);
    if (!current || !old || delta <= 0) continue;
    const tx = Math.max(0, current.tx - old.tx);
    g.tx = tx / delta;
  }
  for (const map of [plugins, crawls, interfaces, sites]) {
    for (const group of map.values()) {
      if (!group.measured) continue;
      group.history.push(group.rate);
      if (group.history.length > 30) group.history.shift();
    }
  }
  previous = data;
  cursor = data.cursor || cursor;
  render();
  if (now - savedAt >= 30) {
    saveHistory();
    savedAt = now;
  }
}
const icons = {
  crawls: "M3 5h7l2 2h9v12H3zM8 13h8m-4-4v8",
  plugins: "M9 3v5m6-5v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4",
  interfaces:
    "M3 13h18v6H3zM6 16h1m4 0h1m4 0h1M8 9a6 6 0 0 1 8 0M5 6a10 10 0 0 1 14 0",
  sites:
    "M12 3v18M3 12h18M12 3a9 9 0 1 0 0 18 9 9 0 1 0 0-18zM12 3c5 5 5 13 0 18C7 16 7 8 12 3z",
  cpu: "M7 7h10v10H7zM10 3v4m4-4v4m-4 10v4m4-4v4M3 10h4m-4 4h4m10-4h4m-4 4h4",
  disk: "M5 4h14l2 12v4H3v-4zM3 16h18m-5 2h2",
  memory: "M3 7h18v10H3zM7 10v3m5-3v3m5-3v3M7 17v3m5-3v3m5-3v3",
  snapshots: "M7 3h12v14H7zM4 7v13h12M10 7h6m-6 4h6",
  pulse: "M2 12h5l3-7 4 14 3-7h5",
  peak: "M3 19 10 7l4 6 3-4 4 10z",
  receive: "M12 4v11m-4-4 4 4 4-4M5 18v2h14v-2",
  send: "M12 15V4m-4 4 4-4 4 4M5 18v2h14v-2",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 1 0 0-18zm0 4v5l4 2",
};
const svg = (tag) =>
  document.createElementNS("http://www.w3.org/2000/svg", tag);
function icon(name) {
  const node = svg("svg");
  node.setAttribute("viewBox", "0 0 24 24");
  node.setAttribute("aria-hidden", "true");
  node.classList.add("metric-icon");
  const path = svg("path");
  path.setAttribute("d", icons[name]);
  node.append(path);
  return node;
}
function metric(label, value) {
  const item = el("div", null, "metric");
  item.append(el("dt", label), el("dd", value));
  item.title = label;
  return item;
}
function chip(symbol, label, value) {
  const item = el("span", null, "metric-chip");
  item.title = label;
  item.append(icon(symbol), el("span", value));
  return item;
}
function sparkline(history) {
  if (history.length < 2) return null;
  const max = Math.max(...history);
  if (!max) return null;
  const chart = svg("svg");
  chart.classList.add("sparkline");
  chart.setAttribute("viewBox", "0 0 72 28");
  chart.setAttribute("role", "img");
  chart.setAttribute("aria-label", "Recent traffic rate");
  const bars = svg("path");
  const step = 72 / history.length;
  bars.setAttribute(
    "d",
    history
      .map((rate, i) => {
        const height = Math.max(1, (27 * rate) / max);
        return `M${i * step} 28v-${height}h${step * 0.7}v${height}z`;
      })
      .join("")
  );
  chart.append(bars);
  return chart;
}
function historyBins(g, now) {
  const bins = new Array(96).fill(0);
  const start = rangeSeconds ? now - rangeSeconds : oldest;
  const width = Math.max(1, (now - start) / bins.length);
  for (const [bucket, amount] of g.buckets) {
    if ((bucket + 1) * bucketSeconds <= start) continue;
    const bin = Math.min(bins.length - 1, Math.max(0, Math.floor((bucket * bucketSeconds - start) / width)));
    bins[bin] += amount.bytes;
  }
  return bins;
}
function historyChart(bins, maximum) {
  const panel = el("div", null, "day-chart");
  const plot = svg("svg");
  plot.setAttribute("viewBox", "0 0 96 28");
  plot.setAttribute("preserveAspectRatio", "none");
  plot.setAttribute("role", "img");
  plot.setAttribute("aria-label", `Downloads · ${rangeLabel}`);
  if (maximum) {
    const bars = svg("path");
    bars.setAttribute(
      "d",
      bins
        .map((amount, i) => {
          if (!amount) return "";
          const height = (26 * amount) / maximum;
          return `M${i} 28v-${height}h.85v${height}z`;
        })
        .join("")
    );
    plot.append(bars);
  }
  panel.append(plot);
  return panel;
}
function renderCard(card, g, kind, chartMaximum) {
  const work = kind === "plugins" || kind === "crawls";
  const nic = kind === "interfaces";
  const site = kind === "sites";
  card.className = `bandwidth-card ${kind}-card`;

  const header = el("header");
  const name = el(
    g.href ? "a" : "strong",
    kind === "crawls"
      ? g.label || `Crawl #${short(g.id)}`
      : g.hostnames?.size === 1
      ? g.hostnames.values().next().value
      : g.id
  );
  if (g.href) name.href = g.href;
  name.title = name.textContent;
  const emblem = el("span", null, "card-icon");
  if (site && g.favicon) {
    const favicon = el("img");
    favicon.alt = "";
    favicon.src = g.favicon;
    favicon.onerror = () => emblem.replaceChildren(icon(kind));
    emblem.append(favicon);
  } else emblem.append(icon(kind));
  header.append(emblem, name);
  card.replaceChildren(header);
  const stats = site ? el("div", null, "site-stats") : header;
  if (g.active) {
    const label = work ? "running" : g.active === 1 ? "socket" : "sockets";
    stats.append(el("span", `${g.active} ${label}`, "status-badge started"));
  }
  if (site) {
    if (g.rate > 0) {
      const badge = chip("receive", "Live download rate", `${bytes(g.rate)}/s`);
      badge.classList.add("site-rate");
      stats.append(badge);
    }
    if (g.peak > 0)
      stats.append(
        chip(
          "peak",
          "Peak download rate",
          `${Math.round((g.peak * 8) / 1e6) || "<1"} Mbps`
        )
      );
    const latency = g.windows[2].rtt;
    if (latency != null)
      stats.append(
        chip(
          "pulse",
          "Minimum observed RTT",
          `${Math.round(latency) || "<1"} ms`
        )
      );
    if (stats.childElementCount) card.append(stats);
  }

  const traffic = el("div", null, "traffic-strip");
  const metrics = el("dl", null, "traffic-values");
  const averaged = !g.rate && g.windows[0].bytes > 0;
  const rate = averaged ? g.windows[0].bytes / 60 : g.rate;
  if (!site && (g.measured || g.windows[2].measured)) {
    metrics.append(
      metric(
        averaged ? "1-minute average" : "Download rate",
        g.measured ? `${bytes(rate)}/s` : "—"
      ),
      metric(
        "Total transferred",
        g.measured || g.windows[2].bytes ? bytes(g.windows[2].bytes) : "—"
      )
    );
    traffic.append(metrics);
    const chart = sparkline(g.history);
    if (chart) traffic.append(chart);
    card.append(traffic);
  }
  if (site && g.windows[2].bytes) card.append(historyChart(g.bins, chartMaximum));

  const details = el("div", null, "detail-strip");
  if (!site && g.peak > 0)
    details.append(
      chip(
        "peak",
        "Peak traffic",
        `${Math.round((g.peak * 8) / 1e6) || "<1"} Mbps`
      )
    );
  if (work) {
    if (g.disk) details.append(chip("disk", "Disk", bytes(g.disk)));
    if (g.fg)
      details.append(
        chip("clock", "Foreground elapsed time", duration(g.fg))
      );
    if (g.cpu)
      details.append(chip("cpu", "Observed foreground CPU", duration(g.cpu)));
    if (g.snapshots.size)
      details.append(
        chip(
          "snapshots",
          "Snapshots",
          g.snapshots.size.toLocaleString()
        )
      );
    if (g.active && g.cpuSamples && g.cpuLive > 0)
      details.append(
        chip(
          "cpu",
          "Average live CPU",
          `${Math.round(g.cpuLive / g.cpuSamples) || "<1"}%`
        )
      );
    if (g.active && g.ramSamples && g.ram > 0)
      details.append(
        chip(
          "memory",
          "Average live RAM",
          bytes(g.ram / g.ramSamples)
        )
      );
  }
  if (nic) {
    if (g.tx) details.append(chip("send", "Upload rate", `${bytes(g.tx)}/s ↑`));
    if (g.local) details.append(chip("interfaces", "Local IP", g.local));
    if (g.wan) details.append(chip("sites", "WAN IP", g.wan));
  }
  if (details.childElementCount) card.append(details);

  const windowsToShow = g.windows
    .map((amount, index) => ({ ...amount, index }))
    .filter(({ bytes, hits, measured, observations, index }) =>
      !(range === "1h" && index === 1) &&
      (site ? measured || hits || observations : index < 2 && (bytes || hits))
    );
  if (windowsToShow.length) {
    const windows = el("div", null, site ? "window-tiles" : "window-strip");
    if (!site) windows.append(icon("clock"));
    for (const { bytes: value, hits, measured, observations, index } of windowsToShow) {
      const item = el("span", null, "rate-badge");
      const label = ["1m", "1h", range === "all" ? "All time" : range][index];
      item.title = [
        "Downloads in the last minute",
        "Downloads in the last hour",
        rangeLabel,
      ][index];
      item.append(el("b", label));
      if (value || (site && measured)) item.append(el("strong", bytes(value)));
      if (site && hits) {
        const estimate = el(
          "span",
          `${hits.toLocaleString()} req`,
          "hit-estimate"
        );
        estimate.title = "Completed downloads and observed connections";
        item.append(estimate);
      }
      if (site && observations) {
        const count = el("span", `${observations.toLocaleString()} ${observations === 1 ? "hit" : "hits"}`, "hit-estimate");
        count.title = "Captures with a browser response";
        item.append(count);
      }
      windows.append(item);
    }
    card.append(windows);
  }
}

function render() {
  const now = Date.now() / 1000;
  for (const [id, map] of Object.entries(columns)) {
    for (const g of map.values()) g.windows = trafficWindows(g, now);
    const sort = $(`${id}-sort`)?.value || "bandwidth";
    const weight = (g) =>
      sort === "cpu" ? g.cpu : sort === "disk" ? g.disk : g.windows[2].bytes;
    const groups = [...map.values()]
      .filter((g) => id !== "sites" || g.id)
      .filter((g) => (!rangeSeconds && g.hasHistory) || g.active || g.windows[2].measured || g.windows[2].hits || g.windows[2].observations || (
        ["crawls", "plugins"].includes(id) && g.last >= (rangeSeconds ? now - rangeSeconds : 0)
      ))
      .sort(
        (a, b) =>
          weight(b) - weight(a) ||
          b.windows[2].bytes - a.windows[2].bytes ||
          b.windows[2].hits + b.windows[2].observations - a.windows[2].hits - a.windows[2].observations ||
          b.disk - a.disk ||
          b.cpu - a.cpu ||
          a.id.localeCompare(b.id)
      );
    let chartMaximum = 0;
    if (id === "sites") {
      for (const site of groups) {
        if (!site.windows[2].bytes) continue;
        site.bins = historyBins(site, now);
        chartMaximum = Math.max(chartMaximum, ...site.bins);
      }
    }
    const parent = $(id),
      existing = new Map([...parent.children].map((n) => [n.dataset.key, n]));
    let next = parent.firstElementChild;
    for (const g of groups) {
      let card = existing.get(g.id);
      const isNew = !card;
      if (isNew) {
        card = el("article");
        card.dataset.key = g.id;
        cardObserver.observe(card);
      }
      cardData.set(card, [g, id, chartMaximum]);
      if (isNew || nearbyCards.has(card)) renderCard(card, g, id, chartMaximum);
      if (card === next) next = next.nextElementSibling;
      else parent.insertBefore(card, next);
      existing.delete(g.id);
    }
    for (const card of existing.values()) {
      cardObserver.unobserve(card);
      card.remove();
    }
    $(`${id}-count`).hidden = !groups.length;
    $(`${id}-count`).textContent = String(groups.length);
  }
  const observedBytes = [...sites.values()].reduce((sum, g) => sum + g.windows[2].bytes, 0);
  $("total-bytes").textContent = bytes(observedBytes);
  $("total-rate").textContent = bytes(totalRate) + "/s";
  $("process-count").textContent = String(
    [...processes.values()].filter((p) => p.status === "running").length
  );
}
async function loadHistory(stream) {
  if (stream.request || stream.cursor === null || document.hidden) return;
  const controller = new AbortController();
  stream.request = controller;
  try {
    const response = await fetch(`data/?${new URLSearchParams({ history: stream.cursor, source: stream.source, range })}`, {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
    });
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    if (stream.request !== controller) return;
    if (stream.source === "hits") ingestObservations(data);
    else if (stream.source === "groups") ingestGroups(data);
    else ingestDownloads(data, new Map(data.processes.map((process) => [process.id, process])));
    stream.cursor = data.cursor;
    if (stream.cursor !== null) setTimeout(() => loadHistory(stream), 0);
    else {
      stream.loadedRange = rangeSeconds || Infinity;
      saveHistory();
    }
  } catch (error) {
    if (!controller.signal.aborted) console.error(error);
  } finally {
    if (stream.request === controller) {
      stream.request = null;
      render();
    }
  }
}
async function poll() {
  if (document.hidden || busy) return;
  histories.forEach(loadHistory);
  busy = true;
  try {
    const params = new URLSearchParams({ range });
    if (cursor) params.set("after", cursor);
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
for (const control of root.querySelectorAll("select"))
  control.onchange = render;
const rangeLinks = [...root.querySelectorAll(".range-bar a")];
function selectRange(key) {
  const link = rangeLinks.find((item) => item.dataset.range === key);
  if (!link || key === range) return;
  range = key;
  rangeSeconds = Number(link.dataset.seconds);
  rangeLabel = link.textContent;
  root.dataset.range = key;
  root.dataset.rangeSeconds = link.dataset.seconds;
  root.dataset.rangeLabel = rangeLabel;
  for (const item of rangeLinks) item.removeAttribute("aria-current");
  link.setAttribute("aria-current", "true");
  for (const stream of histories) {
    stream.request?.abort();
    stream.request = null;
    stream.cursor = (stream.source !== "groups" || !rangeSeconds) &&
      (rangeSeconds || Infinity) > stream.loadedRange ? "" : null;
  }
  render();
  histories.forEach(loadHistory);
}
for (const link of rangeLinks) {
  link.onclick = (event) => {
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
      return;
    event.preventDefault();
    if (link.dataset.range === range) return;
    history.pushState(null, "", link.href);
    selectRange(link.dataset.range);
  };
}
window.addEventListener("popstate", () => {
  selectRange(new URLSearchParams(location.search).get("range") || "24h");
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    previous = null;
    poll();
  }
});
window.addEventListener("pagehide", saveHistory);
restoreHistory();
render();
setInterval(poll, Number(root.dataset.pollMs));
poll();
