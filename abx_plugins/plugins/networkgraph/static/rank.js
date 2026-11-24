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
function publishLayout(dimensions, nodes, clusters) {
  postMessage({
    dimensions,
    nodes: nodes.map(({ id, x, y, z, rank, radius, community }) => ({
      id, x, y, z, rank, radius, community,
    })),
    communities: clusters.map(({ id, members }) => ({ id, count: members.length })),
  });
}
onmessage = ({ data }) => {
  const { dimensions } = data;
  const spatial = dimensions === 3;
  const allNodes = data.nodes;
  const index = new Map(allNodes.map((n, i) => [n.id, i]));
  const allLinks = data.links
    .filter((l) => index.has(l.source) && index.has(l.target))
    .map((l) => ({
      ...l,
      a: index.get(l.source),
      b: index.get(l.target),
      weight: Math.log1p(l.value),
    }));
  const count = allNodes.length;
  if (!count) {
    postMessage({ dimensions, nodes: [], communities: [] });
    return;
  }
  let rank = new Float64Array(count).fill(1 / count);
  const weight = new Float64Array(count);
  for (const l of allLinks) weight[l.a] += l.weight;
  for (let iteration = 0; iteration < 24; iteration++) {
    let dangling = 0;
    for (let i = 0; i < count; i++) if (!weight[i]) dangling += rank[i];
    const next = new Float64Array(count).fill((0.15 + 0.85 * dangling) / count);
    for (const l of allLinks)
      next[l.b] += (0.85 * rank[l.a] * l.weight) / weight[l.a];
    let error = 0;
    for (let i = 0; i < count; i++) error += Math.abs(next[i] - rank[i]);
    rank = next;
    if (error < 1e-7) break;
  }
  for (let i = 0; i < count; i++) allNodes[i].rank = rank[i];
  const nodes = allNodes;
  const links = allLinks;
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
    const key = network.degree(n.id) ? communities[n.id] : "isolates";
    if (!partitions.has(key)) partitions.set(key, []);
    partitions.get(key).push(i);
  });
  const groups = new Map();
  for (const [partition, members] of partitions) {
    const hub = members.reduce((a, b) =>
      nodes[a].rank > nodes[b].rank ||
      (nodes[a].rank === nodes[b].rank && nodes[a].count > nodes[b].count)
        ? a
        : b
    );
    const name = partition === "isolates" ? "" : nodes[hub].id;
    const group = {
      id: name,
      members,
      rank: members.reduce((sum, v) => sum + nodes[v].rank, 0),
    };
    groups.set(name, group);
    for (const v of members) nodes[v].community = name;
  }
  const maximum = nodes.reduce((max, n) => Math.max(max, n.rank), 0);
  nodes.forEach((n) => {
    n.radius =
      (n.community ? 3 : 1) +
      Math.log2(1 + n.count) * 2.5 +
      Math.sqrt(n.rank / maximum) * 34;
  });
  const unlinked = groups.get("");
  const clusters = [...groups.values()]
    .filter((g) => g.id)
    .sort((a, b) => b.rank - a.rank);
  if (nodes.every((n) => n.position)) {
    for (const n of nodes) {
      n.x = n.position.x;
      n.y = n.position.y;
      n.z = spatial ? n.position.z ?? 0 : 0;
      if (!n.community) n.radius = 1 + Math.log2(1 + n.count) * 1.8;
    }
    publishLayout(dimensions, nodes, clusters);
    return;
  }
  clusters.forEach((g, i) => {
    g.radius =
      Math.sqrt(g.members.reduce((s, v) => s + (nodes[v].radius + 4) ** 2, 0)) *
      1.8;
    const angle = i * 2.3999632297;
    g.x = Math.cos(angle) * Math.sqrt(i) * 150;
    g.y = Math.sin(angle) * Math.sqrt(i) * 150;
    g.z = spatial ? Math.sin(i * 2) * (g.radius + 150) : 0;
    const established = g.members.map((v) => nodes[v].position).filter(Boolean);
    if (established.length) {
      for (const axis of spatial ? ["x", "y", "z"] : ["x", "y"])
        g[axis] = g[`f${axis}`] =
          established.reduce((sum, p) => sum + (p[axis] ?? 0), 0) / established.length;
    }
  });
  if (clusters.some((g) => g.fx == null))
    d3.forceSimulation(clusters, dimensions)
      .stop()
      .force("collide", d3.forceCollide((g) => g.radius + 80).iterations(3))
      .force("x", d3.forceX().strength(0.025))
      .force("y", d3.forceY().strength(0.025))
      .force("z", spatial ? d3.forceZ().strength(0.025) : null)
      .tick(150);
  for (const g of clusters) {
    g.members.sort((a, b) => nodes[b].radius - nodes[a].radius);
    g.members.forEach((v, i) => {
      const n = nodes[v];
      const angle = i * 2.3999632297;
      const fraction = i / g.members.length;
      const radius =
        g.radius * (spatial ? Math.cbrt(fraction) : Math.sqrt(fraction));
      // Spread members across a sphere in 3D and a disc in 2D.
      const height = spatial ? 2 * ((i * 0.754877666) % 1) - 1 : 0;
      const radial = radius * Math.sqrt(1 - height ** 2);
      n.x = n.cx = g.x + Math.cos(angle) * radial;
      n.y = n.cy = g.y + Math.sin(angle) * radial;
      n.z = n.cz = g.z + radius * height;
      n.vx = n.vy = n.vz = 0;
    });
  }
  if (unlinked) {
    const extent = clusters.reduce(
      (max, g) => Math.max(max, Math.hypot(g.x, g.y) + g.radius), 250
    );
    unlinked.members.sort((a, b) => nodes[b].count - nodes[a].count);
    unlinked.members.forEach((v, i) => {
      const n = nodes[v],
        angle = i * 2.3999632297,
        r = extent + 60 + Math.sqrt(i) * 6;
      n.x = n.cx = Math.cos(angle) * r;
      n.y = n.cy = Math.sin(angle) * r;
      n.z = n.cz = spatial ? Math.sin(i * 2) * r * 0.5 : 0;
      n.vx = n.vy = n.vz = 0;
      n.radius = 1 + Math.log2(1 + n.count) * 1.8;
    });
  }
  // Keep published positions while placing newly discovered neighbors.
  for (const n of nodes) {
    if (!n.position) continue;
    n.x = n.fx = n.position.x;
    n.y = n.fy = n.position.y;
    n.z = n.fz = spatial ? n.position.z ?? 0 : 0;
  }
  // Cluster anchors preserve separation while links pull related sites together.
  d3.forceSimulation(nodes, dimensions)
    .stop()
    .force(
      "link",
      d3
        .forceLink(links.filter((l) => !nodes[l.a].position || !nodes[l.b].position))
        .id((n) => n.id)
        .distance((l) => 40 + l.source.radius + l.target.radius)
        .strength(0.015)
    )
    .force("collide", d3.forceCollide((n) => n.radius + 3).iterations(2))
    .force("x", d3.forceX((n) => n.cx).strength(0.08))
    .force("y", d3.forceY((n) => n.cy).strength(0.08))
    .force("z", spatial ? d3.forceZ((n) => n.cz).strength(0.08) : null)
    .tick(35);
  publishLayout(dimensions, nodes, clusters);
};
