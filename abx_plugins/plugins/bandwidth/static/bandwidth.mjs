const root = document.getElementById("bandwidth-ui");
const $ = (id) => root.querySelector(`#${id}`);
const processes = new Map(),
  previousProcesses = new Map(),
  crawls = new Map(),
  plugins = new Map(),
  interfaces = new Map(),
  sites = new Map();
const historyKey = `archivebox-bandwidth-v2:${root.dataset.monitorKey}`;
const completed = new Map();
const renderedReports = new Set();
let savedAt = 0;
let restoredWork = false;
let cursor = "",
  previous = null,
  busy = false,
  paused = false,
  totalBytes = 0,
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
    if (map.size >= 128) {
      const cold = [...map.values()]
        .filter((g) => !g.active)
        .sort((a, b) => a.last - b.last)[0];
      if (cold) map.delete(cold.id);
    }
    map.set(id, {
      id,
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
      snapshots: new Set(),
      measured: false,
      rate: 0,
      history: [],
    });
  }
  return map.get(id);
}
function emptyBucket() {
  return { bytes: 0, hits: 0, rtt: null };
}
function appendBucket(bucket, amount) {
  bucket.bytes += amount.bytes;
  bucket.hits += amount.hits;
  if (amount.rtt != null)
    bucket.rtt = Math.min(bucket.rtt ?? Infinity, amount.rtt);
}
function addTraffic(group, now, amount, seconds = 0) {
  group.measured = true;
  if (seconds > 0) group.rate = amount.bytes / seconds;
  group.bytes += amount.bytes;
  group.peak = Math.max(group.peak, group.rate);
  if (!amount.bytes && !amount.hits && amount.rtt == null) return;
  for (const [buckets, period] of [
    [group.seconds, 1],
    [group.minutes, 60],
  ]) {
    const key = Math.floor(now / period);
    if (!buckets.has(key)) buckets.set(key, emptyBucket());
    appendBucket(buckets.get(key), amount);
  }
}
function trafficWindows(group, now) {
  const recent = emptyBucket(),
    hour = emptyBucket(),
    day = emptyBucket();
  for (const [second, amount] of group.seconds) {
    if (now - second >= 60) group.seconds.delete(second);
    else appendBucket(recent, amount);
  }
  const minute = Math.floor(now / 60);
  for (const [time, amount] of group.minutes) {
    const age = minute - time;
    if (age >= 1440) group.minutes.delete(time);
    else {
      appendBucket(day, amount);
      if (age < 60) appendBucket(hour, amount);
    }
  }
  return [recent, hour, day];
}
function restoreHistory() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(historyKey) || "{}");
    totalBytes = saved.totalBytes || 0;
    for (const [id, time] of saved.completed || [])
      if (Date.now() / 1000 - time < 86400) completed.set(id, time);
    for (const {
      id,
      href,
      favicon,
      seconds,
      minutes,
      peak,
      hostnames,
    } of saved.sites || []) {
      const site = group(sites, id);
      Object.assign(site, {
        href,
        favicon,
        seconds: new Map(seconds),
        minutes: new Map(minutes),
        hostnames: new Set(hostnames),
        peak,
      });
      site.bytes = trafficWindows(site, Date.now() / 1000)[2].bytes;
      site.measured = site.minutes.size > 0;
      if (!site.measured) sites.delete(id);
    }
    for (const [key, map] of [
      ["plugins", plugins],
      ["crawls", crawls],
    ]) {
      for (const { id, label, href, seconds, minutes, peak, last } of saved
        .work?.[key] || []) {
        const work = group(map, id);
        Object.assign(work, {
          label,
          href,
          seconds: new Map(seconds),
          minutes: new Map(minutes),
          peak,
          last,
        });
        work.bytes = trafficWindows(work, Date.now() / 1000)[2].bytes;
        work.measured = work.minutes.size > 0;
        if (!work.measured) map.delete(id);
      }
    }
    restoredWork = Boolean(saved.work);
  } catch {
    // The dashboard also works when storage is unavailable.
  }
}
function saveHistory() {
  try {
    const history = [...sites.values()]
      .filter((s) => s.minutes.size)
      .map((s) => ({
        id: s.id,
        href: s.href,
        favicon: s.favicon,
        seconds: [...s.seconds],
        minutes: [...s.minutes],
        hostnames: [...(s.hostnames || [])],
        peak: s.peak,
      }));
    const now = Date.now() / 1000;
    for (const [id, time] of completed)
      if (now - time >= 86400) completed.delete(id);
    const work = {};
    for (const [key, map] of [
      ["plugins", plugins],
      ["crawls", crawls],
    ]) {
      work[key] = [...map.values()]
        .filter((g) => g.minutes.size)
        .map(({ id, label, href, seconds, minutes, peak, last }) => ({
          id,
          label,
          href,
          seconds: [...seconds],
          minutes: [...minutes],
          peak,
          last,
        }));
    }
    sessionStorage.setItem(
      historyKey,
      JSON.stringify({
        sites: history,
        work,
        completed: [...completed],
        totalBytes,
      })
    );
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
  if (process.crawl) groups.push(group(crawls, process.crawl));
  return groups;
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
      if (p.crawl && g === crawls.get(p.crawl)) {
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
  let transferred = 0,
    liveBytes = 0;
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
      hits:
        !owner.reports_downloads && flow.site && (!old || !old.site) ? 1 : 0,
      rtt: Number.isFinite(flow.rtt) && flow.rtt > 0 ? flow.rtt : null,
    };
    for (const g of [site, ...workGroups(owner)]) {
      liveRates.set(g, (liveRates.get(g) || 0) + bytes / delta);
      if (!amounts.has(g)) amounts.set(g, emptyBucket());
      appendBucket(amounts.get(g), amount);
    }
    liveBytes += bytes;
    transferred += amount.bytes;
  }
  for (const map of [plugins, crawls, sites, interfaces])
    for (const g of map.values()) g.rate = 0;
  for (const [g, amount] of amounts) addTraffic(g, now, amount, delta);
  for (const [g, rate] of liveRates) {
    g.rate = rate;
    g.peak = Math.max(g.peak, rate);
  }
  for (const report of data.downloads || []) {
    const owner = owners.get(report.process);
    if (!owner || renderedReports.has(report.process)) continue;
    renderedReports.add(report.process);
    const alreadyCounted = completed.has(report.process);
    completed.set(report.process, report.time);
    for (const entry of report.entries) {
      const site = group(sites, entry.site);
      site.href ||= entry.site_url;
      site.favicon ||= data.favicons?.[entry.site];
      site.hostnames ||= new Set();
      if (site.hostnames.size < 12) site.hostnames.add(entry.hostname);
      site.last = Math.max(site.last, report.time);
      const amount = { bytes: entry.bytes, hits: entry.hits, rtt: null };
      if (!alreadyCounted || !restoredWork)
        for (const g of workGroups(owner)) addTraffic(g, report.time, amount);
      if (!alreadyCounted) {
        addTraffic(site, report.time, amount);
        transferred += entry.bytes;
      }
    }
  }
  totalBytes += transferred;
  totalRate = delta > 0 ? liveBytes / delta : 0;

  const counters = new Map(
    data.interfaces.map((counter) => [counter.id, counter])
  );
  const previousCounters = new Map(
    previous?.interfaces.map((counter) => [counter.id, counter])
  );
  for (const g of interfaces.values()) {
    g.rx = g.tx = 0;
    const current = counters.get(g.id);
    const old = previousCounters.get(g.id);
    if (!current || !old || delta <= 0) continue;
    const rx = Math.max(0, current.rx - old.rx);
    const tx = Math.max(0, current.tx - old.tx);
    g.rx = rx / delta;
    g.tx = tx / delta;
    addTraffic(g, now, { ...emptyBucket(), bytes: rx }, delta);
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
  $("clock").textContent = new Date(data.time * 1000).toLocaleTimeString();
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
  sum: "M18 4H6l7 8-7 8h12",
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
function metric(symbol, label, value) {
  const item = el("div", null, "metric");
  const title = el("dt");
  title.append(icon(symbol), el("span", label));
  item.append(title, el("dd", value));
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
function dayBins(site, now) {
  const bins = new Array(96).fill(0);
  const lastMinute = Math.floor(now / 60);
  for (const [minute, amount] of site.minutes) {
    const bin = 95 - Math.floor((lastMinute - minute) / 15);
    if (bin >= 0 && bin < 96) bins[bin] += amount.bytes;
  }
  return bins;
}
function dayChart(bins, maximum, peak, latency) {
  const panel = el("div", null, "day-chart");
  const plot = svg("svg");
  plot.setAttribute("viewBox", "0 0 96 28");
  plot.setAttribute("preserveAspectRatio", "none");
  plot.setAttribute("role", "img");
  plot.setAttribute("aria-label", "24-hour download history");
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
  const axis = el("div", null, "chart-axis");
  const summary = el("span", null, "site-summary");
  if (maximum) summary.append(el("span", `${bytes(maximum / 900)}/s`));
  if (peak > 0)
    summary.append(
      chip(
        "peak",
        "Peak download rate",
        `${Math.round((peak * 8) / 1e6) || "<1"} Mbps`
      )
    );
  if (latency != null)
    summary.append(
      chip("pulse", "Minimum observed RTT", `${Math.round(latency) || "<1"} ms`)
    );
  axis.append(el("span", "−24h"), summary, el("span", "Now"));
  panel.append(plot, axis);
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
      : g.id || "Unattributed"
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
  if (g.active) {
    const label = work ? "running" : g.active === 1 ? "socket" : "sockets";
    header.append(el("span", `${g.active} ${label}`, "status-badge started"));
  } else if (work) header.append(el("span", "Finished", "status-badge sealed"));
  if (site) {
    const badge = chip(
      "receive",
      "Live download rate",
      g.rate ? `${bytes(g.rate)}/s` : "0B/s"
    );
    badge.classList.add("site-rate");
    header.append(badge);
  }
  card.replaceChildren(header);

  const traffic = el("div", null, "traffic-strip");
  const metrics = el("dl", null, "traffic-values");
  const averaged = !g.rate && g.windows[0].bytes > 0;
  const rate = averaged ? g.windows[0].bytes / 60 : g.rate;
  if (!site) {
    metrics.append(
      metric(
        "receive",
        averaged ? "1m avg" : "Download / sec",
        g.measured ? `${bytes(rate)}/s` : "—"
      ),
      metric(
        "sum",
        nic ? "Interface downloads" : "Observed downloads",
        g.measured ? bytes(g.bytes) : "—"
      )
    );
    traffic.append(metrics);
    const chart = sparkline(g.history);
    if (chart) traffic.append(chart);
    card.append(traffic);
  }
  if (site)
    card.append(dayChart(g.bins, chartMaximum, g.peak, g.windows[2].rtt));

  const details = el("div", null, "detail-strip");
  if (!site && g.peak > 0)
    details.append(
      chip(
        "peak",
        "Peak traffic",
        `${Math.round((g.peak * 8) / 1e6) || "<1"} Mbps peak`
      )
    );
  if (work) {
    if (g.disk) details.append(chip("disk", "Disk", bytes(g.disk)));
    if (g.fg)
      details.append(
        chip("clock", "Foreground elapsed time", `${duration(g.fg)} fg`)
      );
    details.append(
      chip(
        "cpu",
        "Foreground CPU",
        g.cpu ? `≥ ${duration(g.cpu)} CPU` : "— CPU"
      )
    );
    if (g.snapshots.size)
      details.append(
        chip(
          "snapshots",
          "Snapshots",
          `${g.snapshots.size.toLocaleString()} snapshots`
        )
      );
    if (g.active && g.cpuSamples && g.cpuLive > 0)
      details.append(
        chip(
          "cpu",
          "Average live CPU",
          `${Math.round(g.cpuLive / g.cpuSamples) || "<1"}% CPU avg`
        )
      );
    if (g.active && g.ramSamples && g.ram > 0)
      details.append(
        chip(
          "memory",
          "Average live RAM",
          `${bytes(g.ram / g.ramSamples)} RAM avg`
        )
      );
  }
  if (nic) {
    if (g.tx) details.append(chip("send", "Upload rate", `${bytes(g.tx)}/s ↑`));
    details.append(
      chip("interfaces", "Local IP", `LAN ${g.local || "—"}`),
      chip("sites", "WAN IP", `WAN ${g.wan || "—"}`)
    );
  }
  if (details.childElementCount) card.append(details);

  if (site || g.windows.some(({ bytes, hits }) => bytes || hits)) {
    const windows = el("div", null, site ? "window-tiles" : "window-strip");
    if (!site) windows.append(icon("clock"));
    g.windows.forEach(({ bytes: value, hits }, index) => {
      if (!site && !value && !hits) return;
      const item = el("span", null, "rate-badge");
      const label = ["1m", "1h", "24h"][index];
      item.title = [
        "Downloads in the last minute",
        "Downloads in the last hour",
        "Downloads in the last 24 hours",
      ][index];
      item.append(el("b", label));
      if (site || value) item.append(el("strong", bytes(value)));
      if (site) {
        const estimate = el(
          "span",
          `≈ ${hits.toLocaleString()} req`,
          "hit-estimate"
        );
        estimate.title = "Completed downloads and observed connections";
        item.append(estimate);
      }
      windows.append(item);
    });
    card.append(windows);
  }
}

function render() {
  const now = Date.now() / 1000;
  for (const [id, map, limit] of [
    ["crawls", crawls, 10],
    ["plugins", plugins, 10],
    ["interfaces", interfaces, 8],
    ["sites", sites, 30],
  ]) {
    for (const g of map.values()) g.windows = trafficWindows(g, now);
    const sort = $(`${id}-sort`)?.value || "bandwidth";
    const weight = (g) =>
      sort === "cpu" ? g.cpu : sort === "disk" ? g.disk : g.rate;
    const groups = [...map.values()]
      .filter((g) => {
        if (id === "interfaces") return g.active || now - g.last < 120;
        if (id === "sites")
          return g.active || g.windows[2].bytes || g.windows[2].hits;
        return true;
      })
      .sort(
        (a, b) =>
          weight(b) - weight(a) ||
          b.bytes - a.bytes ||
          b.disk - a.disk ||
          b.cpu - a.cpu ||
          a.id.localeCompare(b.id)
      )
      .slice(0, limit);
    let chartMaximum = 0;
    if (id === "sites") {
      for (const site of groups) {
        site.bins = dayBins(site, now);
        chartMaximum = Math.max(chartMaximum, ...site.bins);
      }
    }
    const parent = $(id),
      existing = new Map([...parent.children].map((n) => [n.dataset.key, n]));
    groups.forEach((g) => {
      const card = existing.get(g.id) || el("article");
      card.dataset.key = g.id;
      renderCard(card, g, id, chartMaximum);
      parent.append(card);
      existing.delete(g.id);
    });
    for (const card of existing.values()) card.remove();
    $(`${id}-count`).hidden = !groups.length;
    $(`${id}-count`).textContent = String(
      id === "interfaces" ? groups.length : map.size
    );
  }
  $("total-bytes").textContent = bytes(totalBytes);
  $("total-rate").textContent = bytes(totalRate) + "/s";
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
$("reset").onclick = () => {
  window.removeEventListener("pagehide", saveHistory);
  try {
    sessionStorage.removeItem(historyKey);
  } catch {}
  location.reload();
};
for (const control of root.querySelectorAll("select"))
  control.onchange = render;
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
