const root = document.getElementById("networkgraph-ui");
const $ = (id) => root.querySelector(`#${id}`);
const maxNodes = Number(root.dataset.maxNodes),
  reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const nodes = new Map(),
  links = new Map(),
  pages = new Map(),
  queued = new Set(),
  adjacency = new Map();
let queue = [],
  catalogCursor = "",
  catalogDone = false,
  paused = false,
  busy = false,
  selected = null,
  hovered = null;
let ticks = 0,
  dimensionality = 3,
  initialFit = false,
  orbit = false,
  lastFrame = 0,
  visible = [],
  visibleLinks = [],
  labelNodes = [];
const number = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});
const bytes = (value) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (value >= 1024 && i < 4) {
    value /= 1024;
    i++;
  }
  return `${Math.round(value).toLocaleString()} ${units[i]}`;
};
const color = (id) =>
  `hsl(${
    [...id].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 180, 0) + 150
  },80%,65%)`;
const end = (value) => value.id || value;
const focused = () => hovered || selected;
const incident = (l) =>
  !focused() ||
  end(l.source) === focused().id ||
  end(l.target) === focused().id;
const graph = new window.ForceGraph3D($("graph"), { controlType: "orbit" })
  .backgroundColor("#050912")
  .showNavInfo(false)
  .nodeRelSize(4)
  .nodeResolution(10)
  .nodeVal((n) => 1 + Math.log2(1 + (n.count || 1)) * 1.6)
  .nodeColor((n) =>
    n === focused()
      ? "#ffffff"
      : !focused() || adjacency.get(focused().id)?.has(n.id)
      ? color(n.id)
      : "#22313d"
  )
  .nodeOpacity(0.95)
  .linkOpacity(0.7)
  .linkWidth((l) =>
    focused() && incident(l) ? 2 : 0.6 + Math.log2(l.value + 1) * 0.3
  )
  .linkColor((l) =>
    !incident(l)
      ? "#111d2a"
      : end(l.target) === focused()?.id
      ? "#ff5fa9"
      : color(end(l.source))
  )
  .linkDirectionalArrowLength((l) => (incident(l) ? 5 : 0))
  .linkDirectionalArrowRelPos(0.85)
  .linkCurvature(0.12)
  .linkDirectionalParticles((l) =>
    reduced || !incident(l)
      ? 0
      : focused()
      ? 3
      : visibleLinks.length < 250
      ? 1
      : 0
  )
  .linkDirectionalParticleSpeed(0.003)
  .linkDirectionalParticleWidth(1.5)
  .nodeLabel((n) => {
    const el = document.createElement("span");
    el.textContent = `${n.id} · ▤ ${n.count ?? "…"} · ↙ ${
      n.incoming || 0
    } · ↗ ${n.outgoing || 0}`;
    return el;
  })
  .linkLabel((l) => {
    const el = document.createElement("span");
    el.textContent = `${end(l.source)} → ${end(l.target)} · ${l.value}`;
    return el;
  })
  .onNodeClick(select)
  .onNodeRightClick((n) => {
    select(n);
    enqueue(n, true);
  })
  .onNodeHover((n) => {
    hovered = n;
    paint();
  })
  .onBackgroundClick(() => {
    selected = null;
    $("selection").hidden = true;
    paint();
    renderList();
  })
  .cooldownTicks(65)
  .d3AlphaDecay(0.045)
  .d3VelocityDecay(0.35);
graph.d3Force("charge").strength(-55).distanceMax(250);
graph.d3Force("link").distance(95);
graph.d3Force("containment", (alpha) => {
  for (const n of graph.graphData().nodes) {
    n.vx -= (n.x || 0) * alpha * 0.04;
    n.vy -= (n.y || 0) * alpha * 0.04;
    n.vz -= (n.z || 0) * alpha * 0.04;
  }
});
function enqueue(n, priority = false) {
  if (queued.has(n.id) || queue.length >= maxNodes || pages.get(n.id) === null)
    return;
  queued.add(n.id);
  priority ? queue.unshift(n) : queue.push(n);
}
function merge(data, source) {
  let changed = false;
  for (const row of data.nodes || []) {
    if (!nodes.has(row.id) && nodes.size >= 15000) continue;
    let n = nodes.get(row.id);
    if (!n) {
      n = { ...row, captures: new Map() };
      nodes.set(row.id, n);
      changed = true;
      if (nodes.size <= maxNodes) enqueue(n);
    } else {
      if (row.count != null && row.count !== n.count) changed = true;
      Object.assign(n, row);
    }
  }
  for (const edge of data.links || []) {
    if (!nodes.has(edge.source) || !nodes.has(edge.target)) continue;
    const id = `${edge.source}\0${edge.target}`;
    if (links.has(id))
      links.get(id).value = Math.max(links.get(id).value, edge.value);
    else if (links.size < maxNodes * 6) {
      links.set(id, { ...edge });
      changed = true;
    }
  }
  for (const capture of data.captures || []) {
    const n = nodes.get(capture.domain);
    if (n && n.captures.size < 64) n.captures.set(capture.id, capture);
  }
  if (source) source.sampled = (source.sampled || 0) + (data.sampled || 0);
  if (changed) layout();
  renderList();
  if (selected) details();
}
function paint() {
  graph
    .nodeColor(graph.nodeColor())
    .linkColor(graph.linkColor())
    .linkWidth(graph.linkWidth())
    .linkDirectionalParticles(graph.linkDirectionalParticles());
}
function layout() {
  const ranked = [...nodes.values()].sort(
    (a, b) => (b.count || 0) - (a.count || 0)
  );
  visible = ranked.slice(0, maxNodes);
  if (selected && !visible.includes(selected))
    visible[visible.length - 1] = selected;
  const ids = new Set(visible.map((n) => n.id));
  visibleLinks = [...links.values()].filter(
    (l) => ids.has(end(l.source)) && ids.has(end(l.target))
  );
  adjacency.clear();
  for (const n of nodes.values()) {
    n.incoming = 0;
    n.outgoing = 0;
    adjacency.set(n.id, new Set());
  }
  for (const l of links.values()) {
    const a = end(l.source),
      b = end(l.target);
    nodes.get(a).outgoing++;
    nodes.get(b).incoming++;
    adjacency.get(a).add(b);
    adjacency.get(b).add(a);
  }
  graph.graphData({ nodes: visible, links: visibleLinks });
  $("node-count").textContent = number.format(visible.length);
  $("link-count").textContent = number.format(visibleLinks.length);
  $("snapshot-count").textContent = number.format(
    ranked.reduce((s, n) => s + (n.count || 0), 0)
  );
  labelNodes = visible.slice(0, 80).map((n) => {
    const el = document.createElement("button");
    el.className = "node-label";
    el.textContent = n.id;
    el.style.color = color(n.id);
    el.onclick = () => select(n);
    return { n, el };
  });
  $("labels").replaceChildren(...labelNodes.map((l) => l.el));
  const bars = ranked.slice(0, 64),
    max = Math.log2(1 + (bars[0]?.count || 1));
  $("distribution").replaceChildren(
    ...bars.map((n, i) => {
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      const h = (Math.log2(1 + (n.count || 0)) / max) * 38;
      r.setAttribute("x", String((i * 220) / bars.length));
      r.setAttribute("y", String(43 - h));
      r.setAttribute("width", String(Math.max(1, 220 / bars.length - 1)));
      r.setAttribute("height", String(h));
      r.setAttribute("fill", color(n.id));
      return r;
    })
  );
  if (!initialFit && visible.length > 4) {
    initialFit = true;
    setTimeout(() => graph.zoomToFit(reduced ? 0 : 900, 90), 1800);
  }
}
function renderList() {
  const term = $("search").value.trim().toLowerCase();
  const list = [...nodes.values()]
    .filter((n) => !term || n.id.includes(term))
    .sort((a, b) => (b.count || 0) - (a.count || 0))
    .slice(0, 100);
  $("domains").replaceChildren(
    ...list.map((n) => {
      const b = document.createElement("button");
      b.className = "domain" + (n === selected ? " selected" : "");
      b.style.setProperty(
        "--weight",
        `${Math.min(100, Math.log2(1 + (n.count || 1)) * 8)}%`
      );
      const name = document.createElement("span");
      name.textContent = n.id;
      const count = document.createElement("output");
      count.textContent = n.count == null ? "·" : number.format(n.count);
      const degree = document.createElement("small");
      degree.textContent = `↙${n.incoming || 0} ↗${n.outgoing || 0}`;
      b.append(name, count, degree);
      b.onclick = () => select(n);
      return b;
    })
  );
}
function details() {
  const n = selected;
  if (!n) return;
  $("selection").hidden = false;
  $("selected-domain").textContent = n.id;
  const captures = [...n.captures.values()],
    size = captures.reduce((sum, c) => sum + c.size, 0);
  $("selected-count").textContent = `▤ ${n.count ?? "…"}   ↙ ${
    n.incoming || 0
  }   ↗ ${n.outgoing || 0}`;
  $("node-metrics").textContent = `◫ ${
    captures.length < (n.count || 0) ? "≥ " : ""
  }${bytes(size)}   ✓ ${captures.filter((c) => c.downloaded).length}   ◷ ${
    captures.length
      ? new Date(captures.at(-1).created).toLocaleDateString()
      : "·"
  }`;
  $("snapshots").href = `../core/snapshot/?domain=${encodeURIComponent(
    n.scope || "*." + n.id
  )}`;
  $("pin").classList.toggle("active", n.fx != null);
  $("expand").disabled = pages.get(n.id) === null;
  const neighborhood = [...links.values()]
    .filter((l) => end(l.source) === n.id || end(l.target) === n.id)
    .sort((a, b) => b.value - a.value)
    .slice(0, 12);
  $("neighbors").replaceChildren(
    ...neighborhood.map((l) => {
      const b = document.createElement("button");
      const incoming = end(l.target) === n.id;
      const target = nodes.get(incoming ? end(l.source) : end(l.target));
      b.textContent = `${incoming ? "↙" : "↗"} ${target.id} · ${l.value}`;
      b.style.color = incoming ? "#ff78b6" : "#61f3de";
      b.onclick = () => select(target);
      return b;
    })
  );
  $("captures").replaceChildren(
    ...captures
      .slice(-8)
      .reverse()
      .map((c) => {
        const a = document.createElement("a");
        a.href = `../core/snapshot/${c.id}/change/`;
        a.title = c.url;
        const icon = c.downloaded
          ? "✓"
          : c.status === "started"
          ? "◌"
          : c.status === "failed"
          ? "×"
          : "○";
        a.textContent = `${icon} ${
          c.title || new URL(c.url).pathname
        } · ${bytes(c.size)}`;
        return a;
      })
  );
}
function select(n) {
  selected = n;
  hovered = null;
  if (!visible.includes(n)) layout();
  if (Number.isFinite(n.x)) {
    const distance = 160 + Math.log2(1 + (n.count || 1)) * 6;
    const norm = Math.hypot(n.x, n.y, n.z) || 1;
    graph.cameraPosition(
      {
        x: n.x + (n.x / norm) * distance,
        y: n.y + (n.y / norm) * distance,
        z: n.z + (n.z / norm || 1) * distance,
      },
      n,
      reduced ? 0 : 800
    );
  }
  if (!pages.has(n.id)) enqueue(n, true);
  paint();
  renderList();
  details();
}
async function step() {
  if (paused || document.hidden || busy || (catalogDone && !queue.length))
    return;
  busy = true;
  $("connection").className = "loading";
  let node = null;
  try {
    const params = new URLSearchParams();
    if (!catalogDone && (ticks++ % 3 === 0 || !queue.length)) {
      if (catalogCursor) params.set("cursor", catalogCursor);
    } else {
      node = queue.shift();
      queued.delete(node.id);
      const cursor = pages.get(node.id);
      if (cursor === null) return;
      params.set("domain", node.scope || "*." + node.id);
      if (cursor) params.set("after", cursor);
    }
    const response = await fetch(`data/?${params}`, {
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    if (node) pages.set(node.id, data.cursor);
    else {
      catalogCursor = data.cursor;
      catalogDone = !data.cursor;
    }
    merge(data, node);
    $("connection").className = "";
    $("connection").ariaLabel = "Connected";
  } catch (error) {
    if (node) enqueue(node, true);
    $("connection").className = "error";
    $("connection").ariaLabel = "Error";
    paused = true;
    $("play").textContent = "▶";
    $("play").ariaLabel = "Resume";
    console.error(error);
  } finally {
    busy = false;
  }
}
function togglePause() {
  paused = !paused;
  $("play").textContent = paused ? "▶" : "Ⅱ";
  $("play").ariaLabel = paused ? "Resume" : "Pause";
  paused ? graph.pauseAnimation() : graph.resumeAnimation();
}
$("play").onclick = togglePause;
$("fit").onclick = () => graph.zoomToFit(reduced ? 0 : 700, 80);
$("dimensions").onclick = () => {
  dimensionality = dimensionality === 3 ? 2 : 3;
  graph.numDimensions(dimensionality);
  $("dimensions").textContent = `${dimensionality}D`;
};
$("orbit").onclick = () => {
  orbit = !orbit;
  $("orbit").classList.toggle("active", orbit);
};
$("reset").onclick = () => location.reload();
$("search").oninput = renderList;
$("search").onkeydown = (e) => {
  if (e.key !== "Enter") return;
  const value = $("search").value.trim().toLowerCase();
  let n =
    nodes.get(value) || [...nodes.values()].find((n) => n.id.includes(value));
  if (!n && /^[a-z0-9.-]+$/.test(value) && value.includes(".")) {
    n = { id: value, scope: "*." + value, captures: new Map() };
    nodes.set(value, n);
    layout();
  }
  if (n) select(n);
};
$("close").onclick = () => {
  selected = null;
  $("selection").hidden = true;
  paint();
  renderList();
};
$("expand").onclick = () => selected && enqueue(selected, true);
$("pin").onclick = () => {
  if (!selected) return;
  const pinned = selected.fx != null;
  for (const axis of ["x", "y", "z"])
    selected[`f${axis}`] = pinned ? undefined : selected[axis];
  details();
};
function resize() {
  graph
    .width(root.clientWidth > 760 ? root.clientWidth - 260 : root.clientWidth)
    .height(root.clientHeight);
}
new ResizeObserver(resize).observe(root);
resize();
document.addEventListener("visibilitychange", () => {
  if (document.hidden) graph.pauseAnimation();
  else if (!paused) graph.resumeAnimation();
});
function frame(t) {
  requestAnimationFrame(frame);
  if (document.hidden || paused || t - lastFrame < 40) return;
  lastFrame = t;
  if (orbit && !reduced) {
    const p = graph.cameraPosition(),
      a = 0.0015;
    graph.cameraPosition({
      x: p.x * Math.cos(a) - p.z * Math.sin(a),
      y: p.y,
      z: p.x * Math.sin(a) + p.z * Math.cos(a),
    });
  }
  for (const { n, el } of labelNodes) {
    const p = graph.graph2ScreenCoords(n.x || 0, n.y || 0, n.z || 0);
    const isNear =
      !focused() || focused() === n || adjacency.get(focused().id)?.has(n.id);
    el.style.opacity = isNear ? "1" : ".15";
    el.style.transform = `translate(${p.x}px,${p.y + 15}px) translateX(-50%)`;
    el.hidden =
      p.x < 0 ||
      p.x > graph.width() ||
      p.y < 65 ||
      p.y > root.clientHeight - 30;
  }
}
setInterval(step, 500);
setInterval(() => {
  if (!busy && catalogDone && !queue.length && !paused && !document.hidden) {
    catalogCursor = "";
    catalogDone = false;
    for (const n of visible.slice(0, 16)) {
      pages.delete(n.id);
      enqueue(n);
    }
  }
}, 30000);
step();
requestAnimationFrame(frame);
