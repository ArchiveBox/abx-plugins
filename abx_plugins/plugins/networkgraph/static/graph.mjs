import {
  Sprite,
  SpriteMaterial,
  CanvasTexture,
  TextureLoader,
  SRGBColorSpace,
  Vector3,
} from "./vendor/three.core.js";
const root = document.getElementById("networkgraph-ui");
const $ = (id) => root.querySelector(`#${id}`);
const maxNodes = Number(root.dataset.maxNodes),
  renderLimit = Math.min(maxNodes, 850),
  layoutLimit = Math.min(maxNodes, 1800),
  outlinkScanLimit = 50000,
  reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const nodes = new Map(),
  links = new Map(),
  pages = new Map(),
  queued = new Set(),
  hiddenNodes = new Set(),
  adjacency = new Map();
let queue = [],
  catalogCursor = "",
  catalogDone = false,
  outlinkCursor = "",
  outlinkSampled = 0,
  linksDone = false,
  sampled = 0,
  layoutBusy = false,
  layoutDirty = false,
  paused = false,
  busy = false,
  selected = null,
  pointerDown = false,
  drag = null,
  layoutDeferred = false,
  rearranged = false;
let dimensionality = 2,
  initialFit = false,
  completeFit = false,
  linksFit = false,
  orbit = false,
  lastFrame = 0,
  visible = [],
  visibleIds = new Set(),
  visibleLinks = [],
  catalogOrder = [],
  labelNodes = [];
const number = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});
const pageRank = new Intl.NumberFormat(undefined, {
  style: "percent",
  maximumSignificantDigits: 3,
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
  "#245eaa",
  "#9a4d0a",
  "#6749a9",
  "#087764",
  "#a33469",
  "#566b18",
  "#1c6b84",
  "#a34025",
];
const color = (id) => {
  const n = nodes.get(id);
  if (n && !n.community) return "#61758b";
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
ctx.fillStyle = "#e6eef6";
ctx.beginPath();
ctx.roundRect(0, 0, 48, 48, 10);
ctx.fill();
ctx.strokeStyle = "#45617c";
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
  if (!n.favicon || !n.showIcon) return undefined;
  if (n.sprite && n.spriteUrl === n.favicon) return n.sprite;
  n.spriteUrl = n.favicon;
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
  layoutBusy = false;
  if (data.dimensions !== dimensionality) {
    requestLayout();
    return;
  }
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
  for (const [id, rank] of data.ranks) {
    const n = nodes.get(id);
    if (n) n.rank = rank;
  }
  communityLabels = data.communities;
  layout();
};
function requestLayout() {
  layoutDirty = true;
  if (layoutBusy || drag || pointerDown) return;
  layoutBusy = true;
  layoutDirty = false;
  const activeLinks = graphLinks();
  worker.postMessage({
    nodes: drawableNodes(activeLinks).map((n) => ({
      id: n.id,
      count: n.count || 0,
    })),
    links: activeLinks.map((l) => ({
      source: end(l.source),
      target: end(l.target),
      value: l.value,
    })),
    focus: [...focusNeighborhood(activeLinks)],
    layoutLimit,
    dimensions: dimensionality,
  });
}
const end = (value) => value.id || value;
const focused = () => selected;
const incomingColor = "#a22d66";
const outgoingColor = "#087764";
const incident = (l) =>
  !focused() ||
  end(l.source) === focused().id ||
  end(l.target) === focused().id;
const graph = new window.ForceGraph3D($("graph"), {
  controlType: "orbit",
  rendererConfig: { antialias: false, powerPreference: "high-performance" },
})
  .backgroundColor(
    getComputedStyle(root).getPropertyValue("--graph-canvas").trim()
  )
  .numDimensions(2)
  .showNavInfo(false)
  .nodeThreeObject(nodeIcon)
  .nodeThreeObjectExtend(true)
  .nodeRelSize(4)
  .nodeResolution(6)
  .nodeVal((n) => ((n.radius || 3) / 4) ** 3)
  .nodeColor((n) =>
    n === focused()
      ? "#132338"
      : !focused() || adjacency.get(focused().id)?.has(n.id)
      ? color(n.id)
      : "#beccd9"
  )
  .nodeOpacity(0.95)
  .linkOpacity(0.65)
  .linkWidth((l) =>
    l.emphasized ? Math.min(2, 0.75 + Math.log2(l.value + 1) * 0.4) : 0
  )
  .linkResolution(3)
  .linkColor((l) =>
    !incident(l)
      ? "#cbd7e3"
      : !focused()
      ? color(end(l.source))
      : end(l.target) === focused().id
      ? incomingColor
      : outgoingColor
  )
  .linkDirectionalArrowLength((l) => (l.emphasized ? 5 : 0))
  .linkDirectionalArrowRelPos(0.85)
  .linkCurvature((l) =>
    l.emphasized && links.has(`${end(l.target)}\0${end(l.source)}`) ? 0.14 : 0
  )
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
  .onNodeClick((n) => select(n))
  .onNodeRightClick((n) => {
    select(n);
    enqueue(n, true);
  })
  .onNodeDrag(dragNode)
  .onNodeDragEnd(dropNode)
  .onBackgroundClick(() => {
    selected = null;
    $("selection").hidden = true;
    layout();
  })
  .cooldownTicks(Infinity)
  .cooldownTime(Infinity)
  .d3VelocityDecay(1);
graph.d3Force("charge", null);
graph.d3Force("center", null);
graph.d3Force("link", null);
graph.renderer().setPixelRatio(Math.min(devicePixelRatio, 1.25));
graph.controls().enableRotate = false;
graph.controls().zoomToCursor = true;
const canvas = graph.renderer().domElement;
let rendering = true;
let renderDeadline = performance.now() + 1300;
function wakeRenderer(ms = 300) {
  if (paused || document.hidden) return;
  renderDeadline = Math.max(renderDeadline, performance.now() + ms);
  if (!rendering) {
    rendering = true;
    graph.resumeAnimation();
  }
}
function stopRenderer() {
  rendering = false;
  graph.pauseAnimation();
}
canvas.addEventListener(
  "pointerdown",
  () => {
    pointerDown = true;
    wakeRenderer();
  },
  true
);
function releasePointer() {
  if (!pointerDown) return;
  pointerDown = false;
  if (!drag && layoutDeferred) layout();
}
window.addEventListener("pointerup", releasePointer);
window.addEventListener("pointercancel", releasePointer);
for (const type of ["pointermove", "pointerleave", "wheel"])
  canvas.addEventListener(type, () => wakeRenderer(), { passive: true });
graph.controls().addEventListener("change", () => wakeRenderer());
$("labels").addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    canvas.dispatchEvent(new WheelEvent("wheel", event));
  },
  { passive: false }
);

function linkAffinity(a, b) {
  const references =
    (links.get(`${a}\0${b}`)?.value || 0) +
    (links.get(`${b}\0${a}`)?.value || 0);
  const repeated = Math.min(1, Math.log2(references) / 5);
  const alternatives = Math.min(adjacency.get(a).size, adjacency.get(b).size);
  // A leaf belongs to its only neighbor. Between hubs, repeated links matter more.
  return Math.max(0.93 / alternatives ** 0.85, 0.12 + 0.87 * repeated ** 1.4);
}

function dragInfluence(rootId) {
  const influence = new Map([[rootId, 1]]);
  let wave = new Map(influence);
  while (wave.size) {
    const next = new Map();
    for (const [id, strength] of wave) {
      const transmitted = strength * (id === rootId ? 1 : 0.82);
      for (const peer of adjacency.get(id) || []) {
        if (!visibleIds.has(peer)) continue;
        const received = transmitted * linkAffinity(id, peer);
        // Stop following a branch only when its remaining influence is negligible.
        if (received <= (influence.get(peer) ?? 0.008)) continue;
        influence.set(peer, received);
        next.set(peer, received);
      }
    }
    wave = next;
  }
  return influence;
}

const nodePosition = (n) =>
  new Vector3(n.x, n.y, dimensionality === 3 ? n.z : 0);
const springOffset = new Vector3();
const springStep = 1000 / 60;

function dragNode(n, delta) {
  if (drag?.node === n && drag.remaining === null) return;
  if (drag) finishDrag();
  const bodies = new Map();
  const springs = [];
  const add = (node, origin, influence) => {
    const body = {
      node,
      origin,
      position: origin.clone(),
      velocity: new Vector3(),
      force: new Vector3(),
      tether: 0.18 * (1 - influence) ** 2,
      weight: 0,
    };
    bodies.set(node, body);
    return body;
  };
  const origin = nodePosition(n);
  origin.x -= delta.x;
  origin.y -= delta.y;
  if (dimensionality === 3) origin.z -= delta.z;
  const grabbed = add(n, origin, 1);
  for (const [id, influence] of dragInfluence(n.id)) {
    if (id === n.id) continue;
    const peer = nodes.get(id);
    add(peer, nodePosition(peer), influence);
  }
  for (const a of bodies.values()) {
    for (const id of adjacency.get(a.node.id) || []) {
      if (a.node.id >= id) continue;
      const b = bodies.get(nodes.get(id));
      if (!b) continue;
      const weight = linkAffinity(a.node.id, id) ** 1.7;
      a.weight += weight;
      b.weight += weight;
      const offset = new Vector3().subVectors(b.origin, a.origin);
      springs.push({ a, b, weight, offset, length: offset.length() });
    }
  }
  drag = {
    node: n,
    grabbed,
    bodies,
    springs,
    dropTarget: null,
    remaining: null,
    time: performance.now(),
    elapsed: 0,
  };
  rearranged = true;
}

function tickSprings() {
  const held = drag.dropTarget === null;
  for (const body of drag.bodies.values()) {
    const { node, position, origin, tether, force } = body;
    position.set(node.x, node.y, dimensionality === 3 ? node.z : 0);
    // Weakly connected sites stay near their own groups while strong ties travel with the hub.
    force.subVectors(origin, position).multiplyScalar(tether);
  }
  if (!held) {
    const { position, force } = drag.grabbed;
    force.subVectors(drag.dropTarget, position).multiplyScalar(0.32);
  }
  for (const { a, b, offset, length, weight } of drag.springs) {
    springOffset.subVectors(b.position, a.position);
    const distance = springOffset.length();
    const radial =
      distance > 0.0001 ? (0.16 * (distance - length)) / distance : 0;
    // Restore the local offset as well as the length, so peers fan back out around a moved hub.
    springOffset.multiplyScalar(radial + 0.16).addScaledVector(offset, -0.16);
    a.force.addScaledVector(springOffset, weight / Math.max(1, a.weight));
    b.force.addScaledVector(springOffset, -weight / Math.max(1, b.weight));
  }
  let motion = 0;
  for (const { node, position, velocity, force } of drag.bodies.values()) {
    if (node.fx != null || (held && node === drag.node)) continue;
    velocity.add(force).multiplyScalar(reduced ? 0.84 : 0.91);
    position.add(velocity);
    node.x = position.x;
    node.y = position.y;
    node.z = position.z;
    motion = Math.max(motion, velocity.lengthSq(), force.lengthSq());
  }
  if (!held && (--drag.remaining === 0 || motion < 0.01)) finishDrag();
}

function advanceDrag(time) {
  drag.elapsed += Math.min(time - drag.time, 3 * springStep);
  drag.time = time;
  while (drag && drag.elapsed >= springStep) {
    drag.elapsed -= springStep;
    tickSprings();
  }
}

function dropNode(n) {
  if (drag?.node !== n) return;
  drag.dropTarget = nodePosition(n);
  drag.remaining = reduced ? 75 : 150;
}

function finishDrag() {
  for (const { node: peer, origin } of drag.bodies.values()) {
    if (
      peer !== drag.node &&
      (peer.fx != null || nodePosition(peer).distanceToSquared(origin) < 0.01)
    )
      continue;
    peer.placement = {
      x: peer.x,
      y: peer.y,
      z: dimensionality === 3 ? peer.z : peer.placement?.z,
    };
  }
  drag = null;
  if (layoutDeferred) layout();
  else wakeRenderer();
}

function enqueue(n, priority = false) {
  if (
    hiddenNodes.has(n.id) ||
    queued.has(n.id) ||
    queue.length >= maxNodes ||
    pages.get(n.id) === null
  )
    return;
  queued.add(n.id);
  priority ? queue.unshift(n) : queue.push(n);
}
function merge(data, source, catalog = false) {
  let changed = false;
  for (const row of data.nodes || []) {
    if (!nodes.has(row.id) && (!catalog || nodes.size >= 100000)) continue;
    let n = nodes.get(row.id);
    if (!n) {
      n = { ...row, captures: new Map() };
      nodes.set(row.id, n);
      changed = true;
    } else {
      if (row.count != null || (row.favicon && row.favicon !== n.favicon))
        changed = true;
      const count = (n.count || 0) + (row.count || 0),
        size = (n.size || 0) + (row.size || 0);
      Object.assign(n, { ...row, favicon: row.favicon || n.favicon });
      if (catalog) Object.assign(n, { count, size });
    }
  }
  for (const edge of data.links || []) {
    if (
      !catalog &&
      (!nodes.get(edge.source)?.count || !nodes.get(edge.target)?.count)
    )
      continue;
    for (const id of [edge.source, edge.target])
      if (!nodes.has(id)) nodes.set(id, { id, count: 0, captures: new Map() });
    const id = `${edge.source}\0${edge.target}`;
    if (links.has(id)) {
      const link = links.get(id);
      const value = !source
        ? link.value + edge.value
        : Math.max(link.value, edge.value);
      changed ||= value !== link.value;
      link.value = value;
    } else if (links.size < maxNodes * 6) {
      links.set(id, { ...edge });
      changed = true;
    }
  }
  for (const capture of data.captures || []) {
    const n = nodes.get(capture.domain);
    if (n && n.captures.size < 64) n.captures.set(capture.id, capture);
  }
  if (source) source.sampled = (source.sampled || 0) + (data.sampled || 0);
  if (changed) layoutDirty = true;
  root.dataset.loaded = String(sampled);
  root.dataset.complete = String(catalogDone);
  root.dataset.linksComplete = String(linksDone);
  $("snapshot-count").textContent = sampled.toLocaleString();
  if (source && source === selected) details();
}
function graphLinks() {
  const all = [...links.values()];
  if (!hiddenNodes.size) return all;
  return all.filter(
    (l) => !hiddenNodes.has(end(l.source)) && !hiddenNodes.has(end(l.target))
  );
}
function drawableNodes(activeLinks) {
  const connected = new Set();
  for (const link of activeLinks) {
    connected.add(end(link.source));
    connected.add(end(link.target));
  }
  const hideIsolates = nodes.size - hiddenNodes.size - connected.size > 200;
  if (hideIsolates && !connected.size && !linksDone)
    return [...nodes.values()]
      .filter((n) => !hiddenNodes.has(n.id))
      .sort((a, b) => b.count - a.count)
      .slice(0, 64);
  return [...nodes.values()].filter(
    (n) =>
      !hiddenNodes.has(n.id) &&
      (!hideIsolates || connected.has(n.id) || n === selected)
  );
}
function focusNeighborhood(activeLinks) {
  if (!selected) return new Set();
  const ids = new Set([selected.id]);
  for (const edge of activeLinks
    .filter(incident)
    .sort((a, b) => b.value - a.value)
    .slice(0, Math.min(160, renderLimit / 3))) {
    ids.add(end(edge.source));
    ids.add(end(edge.target));
  }
  return ids;
}
function layout() {
  if (drag || pointerDown) {
    layoutDeferred = true;
    return;
  }
  layoutDeferred = false;
  const activeLinks = graphLinks();
  const focus = focusNeighborhood(activeLinks);
  const ranked = drawableNodes(activeLinks).sort(
    (a, b) => (b.rank || 0) - (a.rank || 0) || (b.count || 0) - (a.count || 0)
  );
  visible = ranked.filter((n) => n.target && focus.has(n.id));
  visible.push(
    ...ranked
      .filter((n) => n.target && !focus.has(n.id))
      .slice(0, renderLimit - visible.length)
  );
  if (selected && !visible.includes(selected)) {
    if (visible.length === renderLimit) visible.pop();
    visible.push(selected);
  }
  visibleIds = new Set(visible.map((n) => n.id));
  visibleLinks = activeLinks
    .filter(
      (l) => visibleIds.has(end(l.source)) && visibleIds.has(end(l.target))
    )
    .sort(
      (a, b) =>
        Number(Boolean(selected) && incident(b)) -
          Number(Boolean(selected) && incident(a)) || b.value - a.value
    )
    .slice(0, renderLimit * 2)
    .map((l) => {
      l.emphasized = Boolean(
        selected &&
          incident(l) &&
          focus.has(end(l.source)) &&
          focus.has(end(l.target))
      );
      l.source = nodes.get(end(l.source));
      l.target = nodes.get(end(l.target));
      return l;
    });
  adjacency.clear();
  for (const n of nodes.values()) {
    n.incoming = 0;
    n.outgoing = 0;
  }
  for (const l of activeLinks) {
    const a = end(l.source),
      b = end(l.target);
    nodes.get(a).outgoing++;
    nodes.get(b).incoming++;
    if (!adjacency.has(a)) adjacency.set(a, new Set());
    if (!adjacency.has(b)) adjacency.set(b, new Set());
    adjacency.get(a).add(b);
    adjacency.get(b).add(a);
  }
  for (const n of visible) n.showIcon = n === selected;
  for (const n of ranked.filter((n) => visibleIds.has(n.id)).slice(0, 96))
    n.showIcon = true;
  graph.graphData({ nodes: visible, links: visibleLinks });
  graph
    .nodeVal(graph.nodeVal())
    .nodeColor(graph.nodeColor())
    .nodeThreeObject(graph.nodeThreeObject())
    .linkColor(graph.linkColor())
    .linkWidth(graph.linkWidth())
    .linkCurvature(graph.linkCurvature())
    .linkDirectionalArrowLength(graph.linkDirectionalArrowLength());
  wakeRenderer(1300);
  catalogOrder = [...nodes.values()].sort(
    (a, b) => (b.count || 0) - (a.count || 0)
  );
  renderList();
  if (selected) details();
  root.dataset.loaded = String(sampled);
  root.dataset.complete = String(catalogDone);
  root.dataset.linksComplete = String(linksDone);
  root.dataset.rendered = String(visible.length);
  $("node-count").textContent = `${visible.length.toLocaleString()} / ${(
    nodes.size - hiddenNodes.size
  ).toLocaleString()}`;
  $("link-count").textContent =
    visibleLinks.length < activeLinks.length
      ? `${number.format(visibleLinks.length)}/${number.format(
          activeLinks.length
        )}`
      : number.format(visibleLinks.length);
  $("snapshot-count").textContent = sampled.toLocaleString();
  labelNodes = ranked
    .filter((n) => visibleIds.has(n.id))
    .slice(0, 20)
    .map((n) => {
      const el = document.createElement("button");
      el.className = "node-label";
      el.textContent = n.id;
      el.style.color = color(n.id);
      el.onclick = () => select(n);
      return { n, el };
    });
  for (const group of communityLabels
    .filter((g) => g.id && !hiddenNodes.has(g.id) && g.count > 2)
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
      n: { x: group.x, y: group.y - group.radius - 25, z: group.z },
      el,
    });
  }
  $("labels").replaceChildren(...labelNodes.map((l) => l.el));
  positionLabels();
  if (paused)
    requestAnimationFrame(() => {
      if (paused) graph.resumeAnimation().pauseAnimation();
    });
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
    !selected &&
    !rearranged
  ) {
    initialFit = true;
    if (catalogDone) completeFit = true;
    if (linksDone) linksFit = true;
    setTimeout(() => {
      if (!rearranged && !selected) {
        wakeRenderer(1200);
        graph.zoomToFit(reduced ? 0 : 900, 100, (n) => Boolean(n.community));
      }
    }, 1800);
  }
}
function renderList() {
  const term = $("search").value.trim().toLowerCase();
  const list = [];
  for (const n of catalogOrder) {
    if (hiddenNodes.has(n.id) || (term && !n.id.includes(term))) continue;
    list.push(n);
    if (list.length === 100) break;
  }
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
      b.onclick = () => select(n, true);
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
  const rank =
    n.rank && (n.incoming || n.outgoing) ? pageRank.format(n.rank) : "—";
  $("node-metrics").textContent = `PageRank ${rank} · ◫ ${
    captures.length < (n.count || 0) ? "≥ " : ""
  }${bytes(size)}   ✓ ${captures.filter((c) => c.downloaded).length}   ◷ ${
    captures.length
      ? new Date(captures.at(-1).created).toLocaleDateString()
      : "·"
  }`;
  $("snapshots").href = `../core/snapshot/?domain=${encodeURIComponent(
    n.scope || "*." + n.id
  )}`;
  $("expand").disabled = pages.get(n.id) === null;
  const neighborhood = graphLinks()
    .filter((l) => end(l.source) === n.id || end(l.target) === n.id)
    .sort((a, b) => b.value - a.value)
    .slice(0, 12);
  $("neighbors").replaceChildren(
    ...neighborhood.map((l) => {
      const b = document.createElement("button");
      const incoming = end(l.target) === n.id;
      const target = nodes.get(incoming ? end(l.source) : end(l.target));
      b.textContent = `${incoming ? "↙" : "↗"} ${target.id} · ${l.value}`;
      b.style.color = incoming ? incomingColor : outgoingColor;
      b.onclick = () => select(target, true);
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
function select(n, locate = false) {
  if (hiddenNodes.has(n.id)) return;
  if (selected === n && !locate) return;
  selected = n;
  layout();
  if ([...focusNeighborhood(graphLinks())].some((id) => !nodes.get(id)?.target))
    requestLayout();
  if (locate && Number.isFinite(n.x)) {
    const distance = 160 + Math.log2(1 + (n.count || 1)) * 6;
    const position = graph
      .camera()
      .position.clone()
      .sub(graph.controls().target)
      .setLength(distance)
      .add(n);
    graph.cameraPosition(position, n, reduced ? 0 : 800);
  }
  if (!pages.has(n.id)) enqueue(n, true);
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
      outlinkSampled += data.sampled;
      linksDone = !data.cursor || outlinkSampled >= outlinkScanLimit;
    } else {
      sampled += data.sampled;
      catalogCursor = data.cursor;
      catalogDone = !data.cursor;
    }
    const scan = params.get("scan");
    merge(data, node, scan === "1");
    if ((scan === "1" && catalogDone) || (scan === "links" && linksDone))
      requestLayout();
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
      setTimeout(step, pointerDown || drag ? 100 : 8);
  }
}
function togglePause() {
  if (drag) finishDrag();
  paused = !paused;
  $("play").textContent = paused ? "▶" : "Ⅱ";
  $("play").ariaLabel = paused ? "Resume" : "Pause";
  paused ? stopRenderer() : wakeRenderer(1300);
  if (!paused) step();
}
$("play").onclick = togglePause;
$("fit").onclick = () => {
  wakeRenderer(1100);
  graph.zoomToFit(reduced ? 0 : 700, 80, (n) => Boolean(n.community));
};
$("dimensions").onclick = () => {
  if (drag) finishDrag();
  dimensionality = dimensionality === 3 ? 2 : 3;
  const spatial = dimensionality === 3;
  const target = graph.controls().target.clone();
  const distance = graph.camera().position.distanceTo(target);
  wakeRenderer(1300);
  if (!spatial) target.z = 0;
  graph.controls().enableRotate = spatial;
  graph.cameraPosition(
    {
      x: target.x + (spatial ? distance * 0.45 : 0),
      y: target.y + (spatial ? distance * 0.25 : 0),
      z: target.z + (spatial ? distance * 0.86 : distance),
    },
    target,
    reduced || paused ? 0 : 500
  );
  graph.numDimensions(dimensionality);
  requestLayout();
  $("dimensions").textContent = `${dimensionality}D`;
  $("dimensions").setAttribute("aria-pressed", String(spatial));
  $("orbit").disabled = !spatial;
};
$("orbit").onclick = () => {
  orbit = !orbit;
  wakeRenderer();
  $("orbit").classList.toggle("active", orbit);
};
$("reset").onclick = () => location.reload();
$("search").oninput = renderList;
$("search").onkeydown = (e) => {
  if (e.key !== "Enter") return;
  const value = $("search").value.trim().toLowerCase();
  const exact = nodes.get(value);
  const n =
    exact && !hiddenNodes.has(value)
      ? exact
      : [...nodes.values()].find(
          (n) => !hiddenNodes.has(n.id) && n.id.includes(value)
        );
  if (n) select(n, true);
};
$("close").onclick = () => {
  selected = null;
  $("selection").hidden = true;
  layout();
};
$("expand").onclick = () => selected && enqueue(selected, true);
$("hide").onclick = () => {
  if (!selected) return;
  if (drag) finishDrag();
  hiddenNodes.add(selected.id);
  queue = queue.filter((n) => n !== selected);
  queued.delete(selected.id);
  selected = null;
  $("selection").hidden = true;
  rearranged = true;
  layout();
  requestLayout();
};
function resize() {
  graph
    .width(root.clientWidth > 760 ? root.clientWidth - 260 : root.clientWidth)
    .height(root.clientHeight);
  wakeRenderer();
}
new ResizeObserver(resize).observe(root);
resize();
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    releasePointer();
    stopRenderer();
  } else wakeRenderer(1300);
});
function frame(t) {
  requestAnimationFrame(frame);
  if (document.hidden || paused || !rendering) return;
  if (drag?.time < t) advanceDrag(t);
  if (
    !drag &&
    !pointerDown &&
    !(dimensionality === 3 && orbit) &&
    t >= renderDeadline
  ) {
    stopRenderer();
    return;
  }
  if (t - lastFrame < 40) return;
  lastFrame = t;
  if (dimensionality === 3 && orbit && !drag) {
    const target = graph.controls().target;
    const p = graph.camera().position.clone().sub(target);
    const a = 0.0015;
    graph.cameraPosition(
      {
        x: target.x + p.x * Math.cos(a) - p.z * Math.sin(a),
        y: target.y + p.y,
        z: target.z + p.x * Math.sin(a) + p.z * Math.cos(a),
      },
      target
    );
  }
  for (const n of visible) {
    if (!n.target || n.fx != null || drag?.bodies.has(n)) continue;
    for (const axis of ["x", "y", "z"]) {
      const position =
        axis === "z" && dimensionality === 2
          ? 0
          : n.placement?.[axis] ?? n.target[axis];
      n[axis] += (position - n[axis]) * (reduced ? 1 : 0.16);
    }
  }
  positionLabels();
}
function positionLabels() {
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
}, 2000);
step();
requestAnimationFrame(frame);
