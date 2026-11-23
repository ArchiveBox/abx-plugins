import {
  Sprite,
  SpriteMaterial,
  CanvasTexture,
  TextureLoader,
  SRGBColorSpace,
} from "./vendor/three.core.js";
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
  outlinkCursor = "",
  linksDone = false,
  sampled = 0,
  layoutBusy = false,
  layoutDirty = false,
  paused = false,
  busy = false,
  selected = null,
  hovered = null;
let dimensionality = 2,
  initialFit = false,
  completeFit = false,
  linksFit = false,
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
const palette = [
  "#62a9ff",
  "#f6b768",
  "#b397fa",
  "#62d4bc",
  "#f083a7",
  "#c5d97c",
  "#75cbe8",
  "#ed9979",
];
const color = (id) => {
  const n = nodes.get(id);
  if (n && !n.community) return "#526679";
  const name = n?.community || id;
  return palette[
    [...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 0) %
      palette.length
  ];
};
const icons = new Map(),
  iconQueue = [];
let iconLoading = 0;
const fallbackCanvas = document.createElement("canvas");
fallbackCanvas.width = fallbackCanvas.height = 48;
const ctx = fallbackCanvas.getContext("2d");
ctx.fillStyle = "#24374b";
ctx.beginPath();
ctx.roundRect(0, 0, 48, 48, 10);
ctx.fill();
ctx.strokeStyle = "#b6cce1";
ctx.lineWidth = 2;
ctx.beginPath();
ctx.arc(24, 24, 15, 0, Math.PI * 2);
ctx.moveTo(9, 24);
ctx.lineTo(39, 24);
ctx.ellipse(24, 24, 7, 15, 0, 0, Math.PI * 2);
ctx.stroke();
const fallback = new CanvasTexture(fallbackCanvas);
fallback.colorSpace = SRGBColorSpace;
function loadIcons() {
  while (iconLoading < 6 && iconQueue.length) {
    const { url, material } = iconQueue.shift();
    iconLoading++;
    new TextureLoader().load(
      url,
      (texture) => {
        texture.colorSpace = SRGBColorSpace;
        material.map = texture;
        material.needsUpdate = true;
        iconLoading--;
        loadIcons();
      },
      undefined,
      () => {
        iconLoading--;
        loadIcons();
      }
    );
  }
}
function nodeIcon(n) {
  // Small low-ranked sites use inexpensive geometry; prominent sites use saved icons.
  if (!n.favicon) return undefined;
  const key = n.favicon;
  if (!icons.has(key)) {
    const material = new SpriteMaterial({
      map: fallback,
      transparent: true,
      depthWrite: false,
      depthTest: false,
    });
    icons.set(key, material);
    iconQueue.push({ url: key, material });
  }
  const sprite = new Sprite(icons.get(key));
  sprite.renderOrder = 10;
  const size = (n.radius || 9) * 1.5;
  sprite.scale.set(size, size, 1);
  n.sprite = sprite;
  loadIcons();
  return sprite;
}
let communityLabels = [];
const worker = new Worker(new URL("./rank.js", import.meta.url));
worker.onmessage = ({ data }) => {
  for (const row of data.nodes) {
    const n = nodes.get(row.id);
    if (!n) continue;
    Object.assign(n, {
      rank: row.rank,
      radius: row.radius,
      community: row.community,
    });
    // Existing nodes animate toward new worker positions without rerunning physics.
    n.target = row;
    if (n.sprite) n.sprite.scale.set(row.radius * 1.5, row.radius * 1.5, 1);
    if (!Number.isFinite(n.x)) {
      n.x = row.x;
      n.y = row.y;
      n.z = row.z;
    }
  }
  layoutBusy = false;
  communityLabels = data.communities;
  layout();
  if (layoutDirty) requestLayout();
};
function requestLayout() {
  layoutDirty = true;
  if (layoutBusy) return;
  layoutBusy = true;
  layoutDirty = false;
  worker.postMessage({
    nodes: [...nodes.values()].slice(0, maxNodes).map((n) => ({
      id: n.id,
      count: n.count || 0,
      fx: n.fx,
      fy: n.fy,
      fz: n.fz,
    })),
    links: [...links.values()].map((l) => ({
      source: end(l.source),
      target: end(l.target),
      value: l.value,
    })),
    dimensions: dimensionality,
  });
}
const end = (value) => value.id || value;
const focused = () => hovered || selected;
const incident = (l) =>
  !focused() ||
  end(l.source) === focused().id ||
  end(l.target) === focused().id;
const graph = new window.ForceGraph3D($("graph"), {
  controlType: "orbit",
  rendererConfig: { antialias: false, powerPreference: "high-performance" },
})
  .backgroundColor("#050912")
  .numDimensions(2)
  .showNavInfo(false)
  .nodeThreeObject(nodeIcon)
  .nodeThreeObjectExtend(true)
  .nodeRelSize(4)
  .nodeResolution(8)
  .nodeVal((n) => ((n.radius || 3) / 4) ** 3)
  .nodeColor((n) =>
    n === focused()
      ? "#ffffff"
      : !focused() || adjacency.get(focused().id)?.has(n.id)
      ? color(n.id)
      : "#22313d"
  )
  .nodeOpacity(0.95)
  .linkOpacity(0.65)
  .linkWidth((l) =>
    focused() && incident(l)
      ? 2
      : l.value >= 4
      ? Math.min(2, Math.log2(l.value) * 0.3)
      : 0
  )
  .linkResolution(3)
  .linkColor((l) =>
    !incident(l)
      ? "#111d2a"
      : end(l.target) === focused()?.id
      ? "#ff5fa9"
      : color(end(l.source))
  )
  .linkDirectionalArrowLength((l) => (focused() && incident(l) ? 5 : 0))
  .linkDirectionalArrowRelPos(0.85)
  .linkCurvature(0)
  .linkDirectionalParticles((l) =>
    !reduced && focused() && incident(l) ? 2 : 0
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
  .cooldownTicks(Infinity)
  .cooldownTime(Infinity)
  .d3VelocityDecay(1);
graph.d3Force("charge", null);
graph.d3Force("center", null);
graph.d3Force("link", null);
graph.renderer().setPixelRatio(Math.min(devicePixelRatio, 1.5));
function enqueue(n, priority = false) {
  if (queued.has(n.id) || queue.length >= maxNodes || pages.get(n.id) === null)
    return;
  queued.add(n.id);
  priority ? queue.unshift(n) : queue.push(n);
}
function merge(data, source, scan = false) {
  let changed = false;
  for (const row of data.nodes || []) {
    if (!nodes.has(row.id) && nodes.size >= 100000) continue;
    let n = nodes.get(row.id);
    if (!n) {
      n = { ...row, captures: new Map() };
      nodes.set(row.id, n);
      changed = true;
    } else {
      if (row.count != null && row.count !== n.count) changed = true;
      const count = (n.count || 0) + (row.count || 0),
        size = (n.size || 0) + (row.size || 0);
      Object.assign(n, { ...row, favicon: row.favicon || n.favicon });
      if (scan) Object.assign(n, { count, size });
    }
  }
  for (const edge of data.links || []) {
    for (const id of [edge.source, edge.target])
      if (!nodes.has(id)) nodes.set(id, { id, count: 0, captures: new Map() });
    const id = `${edge.source}\0${edge.target}`;
    if (links.has(id))
      links.get(id).value = scan
        ? links.get(id).value + edge.value
        : Math.max(links.get(id).value, edge.value);
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
  if (changed || scan) layoutDirty = true;
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
    (a, b) => (b.rank || 0) - (a.rank || 0) || (b.count || 0) - (a.count || 0)
  );
  visible = ranked.slice(0, maxNodes);
  if (selected && !visible.includes(selected))
    visible[visible.length - 1] = selected;
  const ids = new Set(visible.map((n) => n.id));
  visibleLinks = [...links.values()]
    .filter((l) => ids.has(end(l.source)) && ids.has(end(l.target)))
    .map((l) => ({
      ...l,
      source: nodes.get(end(l.source)),
      target: nodes.get(end(l.target)),
    }));
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
  graph
    .nodeVal(graph.nodeVal())
    .nodeColor(graph.nodeColor())
    .nodeThreeObject(graph.nodeThreeObject());
  renderList();
  root.dataset.loaded = String(sampled);
  root.dataset.complete = String(catalogDone);
  root.dataset.linksComplete = String(linksDone);
  $("node-count").textContent = visible.length.toLocaleString();
  $("link-count").textContent = number.format(visibleLinks.length);
  $("snapshot-count").textContent = sampled.toLocaleString();
  labelNodes = visible.slice(0, 20).map((n) => {
    const el = document.createElement("button");
    el.className = "node-label";
    el.textContent = n.id;
    el.style.color = color(n.id);
    el.onclick = () => select(n);
    return { n, el };
  });
  for (const group of communityLabels
    .filter((g) => g.id && g.count > 2)
    .sort((a, b) => b.count - a.count)
    .slice(0, 25)) {
    const el = document.createElement("button");
    el.className = "community-label";
    el.textContent = `${group.id} · ${group.count.toLocaleString()}`;
    el.style.color = color(group.id);
    el.onclick = () => {
      const n = nodes.get(group.id);
      if (n) select(n);
    };
    labelNodes.push({
      n: { x: group.x, y: group.y - group.radius - 25, z: 0 },
      el,
    });
  }
  $("labels").replaceChildren(...labelNodes.map((l) => l.el));
  const bars = [...ranked]
      .sort((a, b) => (b.count || 0) - (a.count || 0))
      .slice(0, 64),
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
  if (
    (!initialFit ||
      (catalogDone && !completeFit) ||
      (linksDone && !linksFit)) &&
    visible.length > 4 &&
    !selected
  ) {
    initialFit = true;
    if (catalogDone) completeFit = true;
    if (linksDone) linksFit = true;
    setTimeout(
      () =>
        graph.zoomToFit(reduced ? 0 : 900, 100, (n) => Boolean(n.community)),
      1800
    );
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
      if (n.favicon) {
        const img = document.createElement("img");
        img.src = n.favicon;
        img.loading = "lazy";
        img.width = 18;
        img.height = 18;
        img.alt = "";
        img.onerror = () => {
          img.hidden = true;
        };
        b.append(img);
      }
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
    size = n.size || captures.reduce((sum, c) => sum + c.size, 0);
  $("selected-count").textContent = `▤ ${n.count ?? "…"}   ↙ ${
    n.incoming || 0
  }   ↗ ${n.outgoing || 0}`;
  $("node-metrics").textContent = `PageRank ${((n.rank || 0) * 100).toFixed(
    2
  )}% · ◫ ${captures.length < (n.count || 0) ? "≥ " : ""}${bytes(size)}   ✓ ${
    captures.filter((c) => c.downloaded).length
  }   ◷ ${
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
  if (
    paused ||
    document.hidden ||
    busy ||
    (catalogDone && linksDone && !queue.length)
  )
    return;
  busy = true;
  $("connection").className = "loading";
  let node = null;
  try {
    const params = new URLSearchParams();
    if (!catalogDone) {
      params.set("scan", "1");
      if (catalogCursor) params.set("after", catalogCursor);
    } else if (!linksDone && !queue.length) {
      params.set("scan", "links");
      if (outlinkCursor) params.set("after", outlinkCursor);
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
    else if (params.get("scan") === "links") {
      outlinkCursor = data.cursor;
      linksDone = !data.cursor;
    } else {
      sampled += data.sampled;
      catalogCursor = data.cursor;
      catalogDone = !data.cursor;
    }
    merge(data, node, !node);
    if (catalogDone && !node && params.get("scan") === "1") requestLayout();
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
    if (!paused && (!catalogDone || !linksDone || queue.length))
      setTimeout(step, 0);
  }
}
function togglePause() {
  paused = !paused;
  $("play").textContent = paused ? "▶" : "Ⅱ";
  $("play").ariaLabel = paused ? "Resume" : "Pause";
  paused ? graph.pauseAnimation() : graph.resumeAnimation();
  if (!paused) step();
}
$("play").onclick = togglePause;
$("fit").onclick = () =>
  graph.zoomToFit(reduced ? 0 : 700, 80, (n) => Boolean(n.community));
$("dimensions").onclick = () => {
  dimensionality = dimensionality === 3 ? 2 : 3;
  graph.numDimensions(dimensionality);
  requestLayout();
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
  requestLayout();
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
  for (const n of visible) {
    if (!n.target || n.fx != null) continue;
    for (const axis of ["x", "y", "z"])
      n[axis] += (n.target[axis] - n[axis]) * (reduced ? 1 : 0.16);
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
setInterval(() => {
  if (!paused && !document.hidden && layoutDirty && !layoutBusy)
    requestLayout();
  if (
    !busy &&
    !paused &&
    !document.hidden &&
    (!catalogDone || !linksDone || queue.length)
  )
    step();
}, 1000);
step();
requestAnimationFrame(frame);
