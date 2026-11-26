import {
  Sprite,
  SpriteMaterial,
  CanvasTexture,
  BufferAttribute,
  BufferGeometry,
  Color,
  DynamicDrawUsage,
  LineBasicMaterial,
  LineSegments,
  Raycaster,
  TextureLoader,
  SRGBColorSpace,
  TOUCH,
  Vector2,
  Vector3,
} from "./vendor/three.core.js";
const root = document.getElementById("networkgraph-ui");
const $ = (id) => root.querySelector(`#${id}`);
const snapshot = root.dataset.snapshot;
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const sampledCaptures = new Set();
const nodes = new Map(),
  links = new Map(),
  pages = new Map(),
  queued = new Set(),
  hiddenNodes = new Set(),
  adjacency = new Map();
let resumeSnapshot,
  queue = [],
  catalogCursor = "",
  catalogDone = false,
  outlinkCursor = "",
  outlinkSampled = 0,
  preferDomain = true,
  linksDone = false,
  sampled = 0,
  layoutBusy = false,
  layoutDirty = false,
  layoutUrgent = false,
  layoutTimer = null,
  loadingPaused = false,
  busy = false,
  fetchTimer = null,
  selected = null,
  pendingLocate = null,
  pointerDown = false,
  drag = null,
  layoutDeferred = false;
let dimensionality = 2,
  initialFit = true,
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
const workerUrl = new URL("./rank.js", import.meta.url);
workerUrl.search = new URL(import.meta.url).search;
const worker = new Worker(workerUrl);
worker.onmessage = ({ data }) => {
  layoutBusy = false;
  if (data.dimensions !== dimensionality) {
    requestLayout();
    return;
  }
  const columnSizes = new Map(), columnOffsets = new Map();
  if (snapshot)
    for (const n of nodes.values())
      columnSizes.set(n.column, (columnSizes.get(n.column) || 0) + 1);
  for (const row of data.nodes) {
    const n = nodes.get(row.id);
    if (!n) continue;
    if (snapshot) row.radius = n.column === 0 ? 10 : 5;
    Object.assign(n, {
      rank: row.rank,
      radius: row.radius,
      community: row.community,
    });
    if (snapshot) {
      const offset = columnOffsets.get(n.column) || 0;
      columnOffsets.set(n.column, offset + 1);
      row.x = n.column * 185;
      const count = columnSizes.get(n.column);
      const spacing = Math.max(26, Math.min(65, 520 / count));
      row.y = (offset - (count - 1) / 2) * spacing;
      row.z = 0;
    }
    n.target = { ...row, dimensions: data.dimensions };
    if (n.sprite) n.sprite.scale.set(row.radius * 1.5, row.radius * 1.5, 1);
    if ((snapshot && !n.placement) || !Number.isFinite(n.x)) {
      n.x = row.x;
      n.y = row.y;
      n.z = row.z;
    }
  }
  communityLabels = snapshot ? [] : data.communities;
  layout();
  if (snapshot && initialFit && catalogDone && !layoutDirty && visible.length) {
    initialFit = false;
    let halfWidth = 0, halfHeight = 0;
    for (const node of visible) {
      halfWidth = Math.max(halfWidth, Math.abs(node.target.x) + (node.radius || 0));
      halfHeight = Math.max(halfHeight, Math.abs(node.target.y) + (node.radius || 0));
    }
    const camera = graph.camera();
    const height = Math.max(90, halfHeight, halfWidth / camera.aspect);
    const z = 1.35 * height / Math.tan(camera.fov * Math.PI / 360);
    graph.cameraPosition({ x: 0, y: 0, z }, { x: 0, y: 0, z: 0 }, 0);
    wakeRenderer();
  }
  if (pendingLocate === selected && pendingLocate?.target)
    locateNode(pendingLocate);
  scheduleLayout();
};
function scheduleLayout() {
  if (!layoutDirty || layoutBusy || drag || pointerDown || document.hidden)
    return;
  if (layoutUrgent) {
    clearTimeout(layoutTimer);
    layoutTimer = null;
    sendLayout();
  } else if (layoutTimer === null) {
    layoutTimer = setTimeout(() => {
      layoutTimer = null;
      if (!drag && !pointerDown && !document.hidden) sendLayout();
    }, 2000);
  }
}
function requestLayout(urgent = true) {
  layoutDirty = true;
  layoutUrgent ||= urgent;
  scheduleLayout();
}
function sendLayout() {
  layoutBusy = true;
  layoutDirty = false;
  layoutUrgent = false;
  const activeLinks = graphLinks();
  worker.postMessage({
    nodes: drawableNodes(activeLinks).map((n) => {
      const position = n.target?.dimensions === dimensionality
        ? n.placement || n.target
        : undefined;
      return {
        id: n.id,
        count: n.count || 0,
        position: position && { x: position.x, y: position.y, z: position.z },
      };
    }),
    links: activeLinks.map((l) => ({
      source: end(l.source),
      target: end(l.target),
      value: l.value,
    })),
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
const linkColor = (l) =>
  snapshot && !selected
    ? (nodes.get(end(l.target))?.column <= 0 ? incomingColor : outgoingColor)
    : !incident(l)
    ? "#cbd7e3"
    : !focused()
    ? color(end(l.source))
    : end(l.target) === focused().id
    ? incomingColor
    : outgoingColor;
const linkLabel = (l) => `${end(l.source)} → ${end(l.target)} · ${l.value}`;
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
  .linkColor(linkColor)
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
    el.textContent = linkLabel(l);
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
  .d3VelocityDecay(1)
  .onEngineTick(() => advanceFrame(performance.now()));
graph.d3Force("charge", null);
graph.d3Force("center", null);
graph.d3Force("link", null);
graph.renderer().setPixelRatio(Math.min(devicePixelRatio, 1.25));
graph.controls().enableRotate = false;
// A pinch ending with one finger must transition to pan in 2D. Keeping the
// disabled ROTATE gesture leaves OrbitControls in its two-pointer dolly state.
graph.controls().touches.ONE = TOUCH.PAN;
graph.controls().zoomToCursor = true;
// Set our own initial camera so ForceGraph does not rescale it on data updates.
graph.cameraPosition({ z: 600 });
const canvas = graph.renderer().domElement;
// One WebGL draw for ordinary edges. ForceGraph retains the styled links
// around the selected node so arrows and its existing hover work as usual.
const edgeLines = new LineSegments(
  new BufferGeometry(),
  new LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.65 })
);
edgeLines.frustumCulled = false;
edgeLines.visible = false;
graph.scene().add(edgeLines);
const edgeColor = new Color();
let batchedLinks = [];
let batchedCapacity = 0;
function updateEdgePositions() {
  if (!batchedLinks.length) return;
  const positions = edgeLines.geometry.attributes.position;
  for (let i = 0; i < batchedLinks.length; i++) {
    const { source, target } = batchedLinks[i];
    positions.setXYZ(2 * i, source.x, source.y, source.z);
    positions.setXYZ(2 * i + 1, target.x, target.y, target.z);
  }
  positions.clearUpdateRanges();
  positions.addUpdateRange(0, batchedLinks.length * 6);
  positions.needsUpdate = true;
  edgeLines.geometry.boundingSphere = null;
}
function batchEdges(ordinaryLinks) {
  batchedLinks = ordinaryLinks;
  if (batchedLinks.length > batchedCapacity) {
    batchedCapacity = Math.max(batchedCapacity * 2, batchedLinks.length);
    edgeLines.geometry.dispose();
    edgeLines.geometry = new BufferGeometry();
    for (const name of ["position", "color"])
      edgeLines.geometry.setAttribute(
        name,
        new BufferAttribute(new Float32Array(batchedCapacity * 6), 3)
          .setUsage(DynamicDrawUsage)
      );
  }
  edgeLines.visible = Boolean(batchedLinks.length);
  if (!batchedLinks.length) return;
  edgeLines.geometry.setDrawRange(0, batchedLinks.length * 2);
  const colors = edgeLines.geometry.attributes.color;
  for (let i = 0; i < batchedLinks.length; i++) {
    edgeColor.set(linkColor(batchedLinks[i]));
    colors.setXYZ(2 * i, edgeColor.r, edgeColor.g, edgeColor.b);
    colors.setXYZ(2 * i + 1, edgeColor.r, edgeColor.g, edgeColor.b);
  }
  colors.needsUpdate = true;
  updateEdgePositions();
}
const edgeTooltip = document.createElement("span");
edgeTooltip.className = "scene-tooltip batched-edge-tooltip";
edgeTooltip.hidden = true;
$("graph").append(edgeTooltip);
const edgeRay = new Raycaster();
const pickPoint = new Vector2();
let hoveredGraphObject = false;
let edgePointer = null;
let edgePickDirty = false;
function pickEdge() {
  edgePickDirty = false;
  if (!edgePointer || pointerDown || drag || hoveredGraphObject || !batchedLinks.length) {
    edgeTooltip.hidden = true;
    return;
  }
  const { x, y } = edgePointer;
  pickPoint.set((x / graph.width()) * 2 - 1, 1 - (y / graph.height()) * 2);
  edgeRay.params.Line.threshold = 1;
  edgeRay.setFromCamera(pickPoint, graph.camera());
  const hit = edgeRay.intersectObject(edgeLines, false)[0];
  const link = hit && batchedLinks[Math.floor(hit.index / 2)];
  edgeTooltip.hidden = !link;
  if (link) {
    edgeTooltip.textContent = linkLabel(link);
    edgeTooltip.style.transform = `translate(${x + 12}px, ${y + 12}px)`;
  }
}
function graphHover(kind, item) {
  if (item) {
    hoveredGraphObject = kind;
    edgeTooltip.hidden = true;
  } else if (hoveredGraphObject === kind) {
    hoveredGraphObject = false;
    edgePickDirty = true;
  }
}
graph.onNodeHover((node) => graphHover("node", node));
graph.onLinkHover((link) => graphHover("link", link));
let rendering = true;
let renderDeadline = performance.now() + 1300;
function wakeRenderer(ms = 300) {
  if (document.hidden) return;
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
    initialFit = false;
    edgeTooltip.hidden = true;
    wakeRenderer();
  },
  true
);
function releasePointer() {
  if (!pointerDown) return;
  pointerDown = false;
  if (!drag && layoutDeferred) layout();
  scheduleLayout();
  edgePickDirty = true;
}
window.addEventListener("pointerup", releasePointer);
window.addEventListener("pointercancel", releasePointer);
canvas.addEventListener("pointermove", (event) => {
  const bounds = canvas.getBoundingClientRect();
  edgePointer = { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
  edgePickDirty = true;
  wakeRenderer();
}, { passive: true });
canvas.addEventListener("pointerleave", () => {
  edgePointer = null;
  edgeTooltip.hidden = true;
  wakeRenderer();
}, { passive: true });
canvas.addEventListener("wheel", () => {
  initialFit = false;
  wakeRenderer();
}, { passive: true });
graph.controls().addEventListener("change", () => {
  edgePickDirty = true;
  wakeRenderer();
});
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
  const repeated = Math.min(1, Math.log2(Math.max(1, references)) / 5);
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
  wakeRenderer();
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
  scheduleLayout();
}

function scheduleFetch(delay = 0) {
  if (fetchTimer !== null || busy || loadingPaused || document.hidden) return;
  if (catalogDone && linksDone && !queue.length) return;
  fetchTimer = setTimeout(() => {
    fetchTimer = null;
    step();
  }, delay);
}
function enqueue(n, priority = false) {
  if (snapshot) return;
  if (hiddenNodes.has(n.id) || queued.has(n.id) || pages.get(n.id) === null)
    return;
  queued.add(n.id);
  priority ? queue.unshift(n) : queue.push(n);
  scheduleFetch();
}
function merge(data, source, catalog = false) {
  let changed = false;
  for (const row of data.nodes || []) {
    if (!catalog && !nodes.has(row.id)) continue;
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
      if (catalog && !snapshot) Object.assign(n, { count, size });
    }
  }
  for (const edge of (source && linksDone ? [] : data.links) || []) {
    if (
      !catalog &&
      (!nodes.get(edge.source)?.count || !nodes.get(edge.target)?.count)
    )
      continue;
    for (const id of [edge.source, edge.target])
      if (!nodes.has(id)) nodes.set(id, { id, count: 0, captures: new Map() });
    const id = `${edge.source}\0${edge.target}`;
    let link = links.get(id);
    if (!link) {
      link = { ...edge, value: 0, overview: 0 };
      links.set(id, link);
    }
    const previous = link.value;
    if (source) {
      link.previews ??= new Map();
      link.previews.set(
        source.id,
        (link.previews.get(source.id) || 0) + edge.value
      );
    } else if (snapshot) link.overview = edge.value;
    else link.overview += edge.value;
    link.value = Math.max(link.overview, ...(link.previews?.values() || []));
    changed ||= previous !== link.value;
  }
  for (const capture of data.captures || []) {
    const n = nodes.get(capture.domain);
    if (n && n.captures.size < 64) n.captures.set(capture.id, capture);
  }
  if (source) source.sampled = (source.sampled || 0) + (data.sampled || 0);
  if (changed) requestLayout(false);
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
  return [...nodes.values()].filter(
    (n) =>
      !hiddenNodes.has(n.id) &&
      (connected.has(n.id) ||
        n === selected ||
        (snapshot && n.column === 0) ||
        (linksDone && !links.size))
  );
}
function layout() {
  if (drag || pointerDown) {
    layoutDeferred = true;
    return;
  }
  layoutDeferred = false;
  const activeLinks = graphLinks();
  const ranked = drawableNodes(activeLinks).sort(
    (a, b) => (b.rank || 0) - (a.rank || 0) || (b.count || 0) - (a.count || 0)
  );
  visible = ranked.filter((n) => n.target);
  visibleIds = new Set(visible.map((n) => n.id));
  visibleLinks = activeLinks
    .filter(
      (l) => visibleIds.has(end(l.source)) && visibleIds.has(end(l.target))
    )
    .map((l) => {
      l.emphasized = Boolean(snapshot || (selected && incident(l)));
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
  const emphasized = [];
  const ordinary = [];
  for (const link of visibleLinks)
    (link.emphasized ? emphasized : ordinary).push(link);
  batchEdges(ordinary);
  edgePickDirty = true;
  graph.graphData({ nodes: visible, links: emphasized });
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
  const edgeCount = $("link-count");
  const pendingEdges = visibleLinks.length < activeLinks.length;
  edgeCount.textContent = pendingEdges
    ? `${number.format(visibleLinks.length)}/${number.format(activeLinks.length)}`
    : number.format(visibleLinks.length);
  edgeCount.title = pendingEdges
    ? `${visibleLinks.length.toLocaleString()}/${activeLinks.length.toLocaleString()}`
    : visibleLinks.length.toLocaleString();
  $("snapshot-count").textContent = sampled.toLocaleString();
  const labelPriority = (n) => snapshot && n.column !== 1 ? 1 : 0;
  const byDegree = [...visible].sort(
    (a, b) => labelPriority(b) - labelPriority(a) ||
      b.incoming + b.outgoing - a.incoming - a.outgoing
  );
  const labeled = new Map(byDegree.slice(0, 20).map((n) => [n.id, 0]));
  for (const { id, count } of communityLabels
    .filter((g) => visibleIds.has(g.id) && g.count > 2)
    .sort((a, b) => b.count - a.count)
    .slice(0, 25))
    labeled.set(id, count);
  labelNodes = [...labeled].map(([id, count]) => {
    const n = nodes.get(id);
    const el = document.createElement("button");
    el.className = count ? "node-label community-label" : "node-label";
    el.textContent = count ? `${id} · ${count.toLocaleString()}` : id;
    el.style.color = color(id);
    el.hidden = true;
    el.onclick = () => select(n);
    return { n, el };
  });
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
  const snapshots = $("snapshots");
  const capture = captures[0];
  snapshots.hidden = Boolean(snapshot && !capture);
  snapshots.href = snapshot
    ? capture
      ? `../core/snapshot/${capture.id}/change/`
      : "#"
    : `../core/snapshot/?domain=${encodeURIComponent(n.scope || "*." + n.id)}`;
  $("expand").disabled = Boolean(snapshot) || pages.get(n.id) === null;
  const neighborhood = graphLinks()
    .filter((l) => end(l.source) === n.id || end(l.target) === n.id)
    .sort((a, b) => b.value - a.value);
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
  initialFit = false;
  selected = n;
  pendingLocate = locate ? n : null;
  layout();
  if (!n.target) requestLayout();
  else if (locate) locateNode(n);
  if (!pages.has(n.id)) enqueue(n, true);
  renderList();
  details();
}
function locateNode(n) {
  pendingLocate = null;
  const distance = 160 + Math.log2(1 + (n.count || 1)) * 6;
  const position = graph
    .camera()
    .position.clone()
    .sub(graph.controls().target)
    .setLength(distance)
    .add(n);
  wakeRenderer(1100);
  graph.cameraPosition(position, n, reduced ? 0 : 800);
}
async function streamSnapshot() {
  const response = await fetch(`data/?${new URLSearchParams({ snapshot })}`);
  if (!response.ok || !response.body) throw new Error(String(response.status));
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  const accept = (line) => {
    const data = JSON.parse(line);
    for (const capture of data.captures) sampledCaptures.add(capture.id);
    sampled = sampledCaptures.size;
    merge(data, null, true);
    requestLayout(nodes.size === 1);
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += value;
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        if (loadingPaused) await new Promise((resume) => { resumeSnapshot = resume; });
        accept(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
      }
    }
    if (buffer) accept(buffer);
    catalogDone = linksDone = true;
    root.dataset.complete = root.dataset.linksComplete = "true";
    requestLayout();
  } finally {
    reader.releaseLock();
  }
}
async function step() {
  if (
    loadingPaused ||
    document.hidden ||
    busy ||
    (catalogDone && linksDone && !queue.length)
  )
    return;
  busy = true;
  const indicator = $("connection");
  indicator.className = "loading";
  let node = null;
  try {
    if (snapshot) {
      await streamSnapshot();
      indicator.className = "";
      indicator.ariaLabel = indicator.title = "Connected";
      $("play").hidden = true;
      return;
    }
    const params = new URLSearchParams();
    if (!catalogDone) {
      params.set("scan", "1");
      if (catalogCursor) params.set("after", catalogCursor);
    } else if (!linksDone && (!queue.length || !preferDomain)) {
      preferDomain = true;
      params.set("scan", "links");
      if (outlinkCursor) params.set("after", outlinkCursor);
    } else {
      preferDomain = false;
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
      linksDone = !data.cursor;
    } else {
      sampled += data.sampled;
      catalogCursor = data.cursor;
      catalogDone = !data.cursor;
    }
    const scan = params.get("scan");
    merge(data, node, scan === "1");
    if (scan === "links" && linksDone) {
      for (const [id, link] of links) {
        if (!link.overview) links.delete(id);
        else {
          link.value = link.overview;
          delete link.previews;
        }
      }
    }
    if (node && node === selected && !linksDone) enqueue(node);
    if ((scan === "1" && catalogDone) || (scan === "links" && linksDone)) {
      requestLayout();
    } else if (node && !data.cursor && layoutDirty) {
      requestLayout();
    }
    indicator.className = "";
    indicator.ariaLabel = !catalogDone
      ? `Loading catalog · ${sampled.toLocaleString()} captures`
      : !linksDone
      ? `Scanning saved links · ${outlinkSampled.toLocaleString()} / ${sampled.toLocaleString()} captures`
      : "Connected";
    indicator.title = indicator.ariaLabel;
  } catch (error) {
    if (node) enqueue(node, true);
    indicator.className = "error";
    indicator.ariaLabel = "Graph loading stopped. Resume to continue.";
    indicator.title = indicator.ariaLabel;
    loadingPaused = true;
    $("play").hidden = false;
    $("play").textContent = "▶";
    $("play").ariaLabel = "Resume loading";
    $("play").title = "Resume loading";
    console.error(error);
  } finally {
    busy = false;
    scheduleFetch(pointerDown || drag ? 100 : 8);
  }
}
function togglePause() {
  loadingPaused = !loadingPaused;
  $("play").textContent = loadingPaused ? "▶" : "Ⅱ";
  $("play").ariaLabel = loadingPaused ? "Resume loading" : "Pause loading";
  $("play").title = $("play").ariaLabel;
  if (!loadingPaused) {
    resumeSnapshot?.();
    resumeSnapshot = null;
    scheduleFetch();
  }
}
$("play").onclick = togglePause;
function fit(duration) {
  initialFit = false;
  wakeRenderer(1100);
  graph.zoomToFit(duration, Math.min(graph.width(), graph.height()) * 0.12);
}
$("fit").onclick = () => fit(reduced ? 0 : 700);
$("dimensions").onclick = () => {
  if (drag) finishDrag();
  initialFit = false;
  dimensionality = dimensionality === 3 ? 2 : 3;
  const spatial = dimensionality === 3;
  const target = graph.controls().target.clone();
  const distance = graph.camera().position.distanceTo(target);
  wakeRenderer(1300);
  if (!spatial) target.z = 0;
  graph.controls().enableRotate = spatial;
  graph.controls().touches.ONE = spatial ? TOUCH.ROTATE : TOUCH.PAN;
  graph.cameraPosition(
    {
      x: target.x + (spatial ? distance * 0.45 : 0),
      y: target.y + (spatial ? distance * 0.25 : 0),
      z: target.z + (spatial ? distance * 0.86 : distance),
    },
    target,
    reduced ? 0 : 500
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
  } else {
    wakeRenderer(1300);
    scheduleLayout();
    scheduleFetch();
  }
});
function advanceFrame(t) {
  const moving = Boolean(drag);
  if (drag?.time < t) advanceDrag(t);
  if (t - lastFrame < 40) {
    if (moving) updateEdgePositions();
    return;
  }
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
  let changed = moving;
  for (const n of visible) {
    if (!n.target || n.fx != null || drag?.bodies.has(n)) continue;
    for (const axis of ["x", "y", "z"]) {
      const position =
        axis === "z" && dimensionality === 2
          ? 0
          : n.placement?.[axis] ?? n.target[axis];
      const delta = position - n[axis];
      if (!delta) continue;
      n[axis] += reduced || Math.abs(delta) < 0.01 ? delta : delta * 0.16;
      changed = true;
    }
  }
  if (changed) updateEdgePositions();
}
const projected = new Vector3();
function positionLabels() {
  const camera = graph.camera();
  const width = graph.width(),
    height = graph.height();
  for (const { n, el } of labelNodes) {
    projected.set(n.x, n.y, n.z).project(camera);
    const x = ((projected.x + 1) * width) / 2;
    const y = ((1 - projected.y) * height) / 2;
    el.hidden =
      projected.z < -1 ||
      projected.z > 1 ||
      x < 0 ||
      x > width ||
      y < 65 ||
      y > height - 30;
    if (el.hidden) continue;
    const isNear =
      !focused() || focused() === n || adjacency.get(focused().id)?.has(n.id);
    el.style.opacity = isNear ? "1" : ".15";
    el.style.transform = `translate(${x}px,${y + 15}px) translateX(-50%)`;
  }
}
// Project labels with the same camera and positions the renderer just drew.
graph.scene().onAfterRender = () => {
  positionLabels();
  if (edgePickDirty) pickEdge();
  if (
    !snapshot &&
    initialFit &&
    catalogDone &&
    visible.length &&
    !layoutDirty &&
    !layoutBusy &&
    graph.getGraphBbox()
  ) {
    fit(0);
  } else if (
    !drag &&
    !pointerDown &&
    !(dimensionality === 3 && orbit) &&
    performance.now() >= renderDeadline
  ) {
    // ForceGraph schedules its next frame after this render callback returns.
    queueMicrotask(stopRenderer);
  }
};
if (snapshot) {
  $("expand").hidden = true;
}
scheduleFetch();
