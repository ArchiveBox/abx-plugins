/* Weighted PageRank, link communities and d3 layout run off the UI thread. */
importScripts(
  ...[
    "d3-binarytree",
    "d3-quadtree",
    "d3-octree",
    "d3-dispatch",
    "d3-timer",
    "d3-force-3d",
  ].map((name) => `vendor/${name}.min.js`)
);
importScripts(
  "vendor/graphology.umd.min.js",
  "vendor/graphology-library.min.js"
);
let previous = new Map();
onmessage = ({ data }) => {
  const nodes = data.nodes.map((n) => ({ ...previous.get(n.id), ...n }));
  const index = new Map(nodes.map((n, i) => [n.id, i]));
  const links = data.links
    .filter((l) => index.has(l.source) && index.has(l.target))
    .map((l) => ({ ...l }));
  const count = nodes.length;
  if (!count) return;
  let rank = new Float64Array(count).fill(1 / count);
  const weight = new Float64Array(count),
    neighbors = nodes.map(() => []);
  for (const l of links) {
    const a = index.get(l.source),
      b = index.get(l.target);
    l.weight = Math.log1p(l.value);
    weight[a] += l.weight;
    neighbors[a].push([b, l.weight]);
    neighbors[b].push([a, l.weight]);
  }
  for (let iteration = 0; iteration < 24; iteration++) {
    let dangling = 0;
    for (let i = 0; i < count; i++) if (!weight[i]) dangling += rank[i];
    const next = new Float64Array(count).fill((0.15 + 0.85 * dangling) / count);
    for (const l of links) {
      const a = index.get(l.source),
        b = index.get(l.target);
      next[b] += (0.85 * rank[a] * l.weight) / weight[a];
    }
    let error = 0;
    for (let i = 0; i < count; i++) error += Math.abs(next[i] - rank[i]);
    rank = next;
    if (error < 1e-7) break;
  }
  const network = new graphology.DirectedGraph();
  for (const n of nodes) network.addNode(n.id);
  for (const l of links)
    network.mergeDirectedEdge(l.source, l.target, { weight: l.weight });
  const communities = graphologyLibrary.communitiesLouvain(network, {
    getEdgeWeight: "weight",
    resolution: 1.3,
    randomWalk: false,
  });
  const partitions = new Map();
  nodes.forEach((n, i) => {
    const key = neighbors[i].length ? communities[n.id] : "isolates";
    if (!partitions.has(key)) partitions.set(key, []);
    partitions.get(key).push(i);
  });
  const groups = new Map();
  for (const [partition, members] of partitions) {
    const hub = members.reduce((a, b) =>
      rank[a] > rank[b] ||
      (rank[a] === rank[b] && nodes[a].count > nodes[b].count)
        ? a
        : b
    );
    const name = partition === "isolates" ? "" : nodes[hub].id;
    const group = {
      id: name,
      members,
      rank: members.reduce((sum, v) => sum + rank[v], 0),
    };
    groups.set(name, group);
    for (const v of members) nodes[v].community = name;
  }
  const maximum = Math.max(...rank);
  nodes.forEach((n, i) => {
    n.rank = rank[i];
    n.radius =
      (n.community ? 3 : 1) +
      Math.log2(1 + n.count) * 2.5 +
      Math.sqrt(n.rank / maximum) * 34;
  });
  const unlinked = groups.get("");
  const clusters = [...groups.values()]
    .filter((g) => g.id)
    .sort((a, b) => b.rank - a.rank);
  clusters.forEach((g, i) => {
    g.radius =
      Math.sqrt(g.members.reduce((s, v) => s + (nodes[v].radius + 4) ** 2, 0)) *
      1.8;
    const angle = i * 2.3999632297;
    g.x = Math.cos(angle) * Math.sqrt(i) * 150;
    g.y = Math.sin(angle) * Math.sqrt(i) * 150;
    g.z = 0;
  });
  d3.forceSimulation(clusters, 2)
    .stop()
    .force("collide", d3.forceCollide((g) => g.radius + 80).iterations(3))
    .force("x", d3.forceX().strength(0.025))
    .force("y", d3.forceY().strength(0.025))
    .tick(150);
  for (const g of clusters) {
    g.members.sort((a, b) => nodes[b].radius - nodes[a].radius);
    g.members.forEach((v, i) => {
      const n = nodes[v],
        angle = i * 2.3999632297,
        r = Math.sqrt(i / Math.max(1, g.members.length)) * g.radius;
      n.x = g.x + Math.cos(angle) * r;
      n.y = g.y + Math.sin(angle) * r;
      n.cx = n.x;
      n.cy = n.y;
      n.z =
        data.dimensions === 3
          ? Math.sin(i * 1.7) * Math.min(50, g.radius * 0.12)
          : 0;
      n.vx = n.vy = n.vz = 0;
    });
  }
  if (unlinked) {
    const extent = Math.max(
      250,
      ...clusters.map((g) => Math.hypot(g.x, g.y) + g.radius)
    );
    unlinked.members.sort((a, b) => nodes[b].count - nodes[a].count);
    unlinked.members.forEach((v, i) => {
      const n = nodes[v],
        angle = i * 2.3999632297,
        r = extent + 60 + Math.sqrt(i) * 6;
      n.x = n.cx = Math.cos(angle) * r;
      n.y = n.cy = Math.sin(angle) * r;
      n.z = 0;
      n.vx = n.vy = n.vz = 0;
      n.radius = 1 + Math.log2(1 + n.count) * 1.8;
    });
  }
  // Cluster anchors preserve separation while links pull related sites together.
  d3.forceSimulation(nodes, data.dimensions)
    .stop()
    .force(
      "link",
      d3
        .forceLink(links)
        .id((n) => n.id)
        .distance((l) => 40 + l.source.radius + l.target.radius)
        .strength(0.015)
    )
    .force("collide", d3.forceCollide((n) => n.radius + 3).iterations(2))
    .force("x", d3.forceX((n) => n.cx).strength(0.08))
    .force("y", d3.forceY((n) => n.cy).strength(0.08))
    .tick(35);
  previous = new Map(nodes.map((n) => [n.id, n]));
  postMessage({
    nodes: nodes.map(({ id, x, y, z, rank, radius, community }) => ({
      id,
      x,
      y,
      z,
      rank,
      radius,
      community,
    })),
    communities: clusters.map(({ id, x, y, radius, members }) => ({
      id,
      x,
      y,
      radius,
      count: members.length,
    })),
  });
};
