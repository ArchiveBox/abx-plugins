import {
  WebGLRenderer, Scene, OrthographicCamera, Group, Mesh, MeshBasicMaterial,
  BoxGeometry, EdgesGeometry, LineSegments, LineBasicMaterial, BufferGeometry,
  Float32BufferAttribute, Points, PointsMaterial, TorusGeometry, IcosahedronGeometry,
  CatmullRomCurve3, Vector3, TubeGeometry, AdditiveBlending,
} from 'three';

// An explanatory model, not a visualization of a real TLS session or proof.
export function createScene(host) {
  const renderer = new WebGLRenderer({ alpha: true, antialias: true, powerPreference: 'low-power' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
  renderer.domElement.setAttribute('aria-hidden', 'true');
  host.append(renderer.domElement);
  const scene = new Scene();
  const camera = new OrthographicCamera(-7.5, 7.5, 3.3, -3.3, 0.1, 100);
  camera.position.set(0, 0, 20);
  const world = new Group();
  scene.add(world);
  const mint = 0x8bf3c5, purple = 0xc0acff, blue = 0x7dbfff;
  function wire(geometry, color, opacity = 0.7) {
    return new LineSegments(new EdgesGeometry(geometry), new LineBasicMaterial({ color, transparent: true, opacity }));
  }
  function box(w, h, d, color) {
    const geometry = new BoxGeometry(w, h, d);
    const group = new Group();
    group.add(new Mesh(geometry, new MeshBasicMaterial({ color, transparent: true, opacity: 0.075, depthWrite: false })));
    group.add(wire(geometry, color));
    return group;
  }
  const website = new Group();
  website.position.x = -5;
  website.rotation.set(0.15, -0.3, 0);
  for (let i = 0; i < 3; i++) {
    const slab = box(1.7, 0.55, 0.8, blue);
    slab.position.y = (i - 1) * 0.78;
    website.add(slab);
    for (let j = 0; j < 3; j++) {
      const led = new Mesh(new BoxGeometry(0.07, 0.07, 0.02), new MeshBasicMaterial({ color: blue }));
      led.position.set(-0.55 + j * 0.2, slab.position.y, 0.42);
      website.add(led);
    }
  }
  world.add(website);
  const browser = new Group();
  browser.rotation.set(0.12, -0.12, 0);
  const pages = [];
  for (let i = 0; i < 4; i++) {
    const sheet = box(2.15, 2.65, 0.04, mint);
    sheet.position.set(i * 0.13, i * 0.08, -i * 0.25);
    browser.add(sheet);
    pages.push(sheet);
  }
  const textBars = new Group();
  for (let i = 0; i < 9; i++) {
    const width = i % 3 === 0 ? 1.25 : 1.6;
    const bar = new Mesh(new BoxGeometry(width, i === 0 ? 0.13 : 0.04, 0.01), new MeshBasicMaterial({ color: mint, transparent: true, opacity: i === 0 ? 0.9 : 0.5 }));
    bar.position.set(-0.1, 0.95 - i * 0.23, 0.04);
    textBars.add(bar);
  }
  browser.add(textBars);
  world.add(browser);
  const verifier = new Group();
  verifier.position.x = 5;
  const core = wire(new IcosahedronGeometry(0.63, 1), purple, 0.9);
  verifier.add(core);
  const rings = [];
  for (let i = 0; i < 3; i++) {
    const ring = new Mesh(new TorusGeometry(0.94 + i * 0.23, 0.013, 5, 90), new MeshBasicMaterial({ color: purple, transparent: true, opacity: 0.75 }));
    ring.rotation.set(i * 0.75, i * 0.65, 0.3);
    rings.push(ring);
    verifier.add(ring);
  }
  world.add(verifier);
  const curves = [
    new CatmullRomCurve3([new Vector3(-4, 0.35, 0), new Vector3(-2.8, 1.3, 0.5), new Vector3(-1.2, 0.55, 0)]),
    new CatmullRomCurve3([new Vector3(1.4, 0.5, 0), new Vector3(2.8, 1.25, 0.5), new Vector3(3.9, 0.4, 0)]),
    new CatmullRomCurve3([new Vector3(3.9, -0.5, 0), new Vector3(2.8, -1.2, 0.5), new Vector3(1.3, -0.6, 0)]),
  ];
  const paths = curves.map((curve, i) => {
    const mesh = new Mesh(new TubeGeometry(curve, 48, 0.012, 4, false), new MeshBasicMaterial({ color: i === 0 ? blue : purple, transparent: true, opacity: 0.25 }));
    world.add(mesh);
    return mesh;
  });
  const packets = curves.map((curve, i) => Array.from({ length: 10 }, (_, j) => {
    const mesh = new Mesh(new IcosahedronGeometry(0.045, 0), new MeshBasicMaterial({ color: i === 0 ? blue : purple }));
    world.add(mesh);
    return { mesh, curve, offset: j / 10, route: i };
  })).flat();
  const dustGeometry = new BufferGeometry();
  // Deterministic geometry; these points represent no actual protocol data.
  dustGeometry.setAttribute('position', new Float32BufferAttribute(Array.from({ length: 480 }, (_, i) => Math.sin(i * 73.13) * (i % 3 === 0 ? 7 : 3)), 3));
  const dust = new Points(dustGeometry, new PointsMaterial({ color: mint, size: 0.022, transparent: true, opacity: 0.35, blending: AdditiveBlending, depthWrite: false }));
  world.add(dust);
  let phase = 0, privateView = false, altered = false, running = false, disposed = false;
  let frame = 0, time = 0, last = 0, targetX = 0, targetY = 0;
  function draw(now = 0) {
    if (disposed) return;
    cancelAnimationFrame(frame);
    if (running) time += Math.min((now - last) / 1000, 0.06);
    last = now;
    world.rotation.x += (targetY - world.rotation.x) * 0.06;
    world.rotation.y += (targetX - world.rotation.y) * 0.06;
    core.rotation.set(time * 0.13, time * 0.2, time * 0.09);
    rings.forEach((ring, i) => { ring.rotation.z = time * (0.1 + i * 0.04); });
    pages.forEach((page, i) => { page.position.z = -i * (phase === 1 ? 0.35 : 0.25); });
    textBars.visible = !privateView;
    browser.position.y = Math.sin(time * 0.7) * 0.06;
    packets.forEach(({ mesh, curve, offset, route }) => {
      mesh.position.copy(curve.getPointAt((time * 0.13 + offset) % 1));
      mesh.visible = phase < 2 && (phase === 1 || route !== 2);
    });
    paths.forEach((path, i) => { path.material.opacity = phase === 2 ? 0.08 : (phase === 1 && i > 0 ? 0.65 : 0.25); });
    core.material.color.setHex(altered && phase === 2 ? 0xff8f99 : purple);
    renderer.render(scene, camera);
    if (running) frame = requestAnimationFrame(draw);
  }
  const resize = new ResizeObserver(() => {
    camera.top = 7.5 * host.clientHeight / host.clientWidth;
    camera.bottom = -camera.top;
    camera.updateProjectionMatrix();
    renderer.setSize(host.clientWidth, host.clientHeight, false);
    draw();
  });
  resize.observe(host);
  function pointer(event) {
    const bounds = host.getBoundingClientRect();
    targetX = ((event.clientX - bounds.left) / bounds.width - 0.5) * 0.18;
    targetY = ((event.clientY - bounds.top) / bounds.height - 0.5) * 0.12;
  }
  function leave() { targetX = targetY = 0; }
  host.addEventListener('pointermove', pointer);
  host.addEventListener('pointerleave', leave);
  function setRunning(value) {
    cancelAnimationFrame(frame);
    running = value && !disposed;
    last = performance.now();
    draw(last);
  }
  return {
    update(state) {
      phase = state.phase;
      privateView = state.privateView;
      altered = state.altered;
      if (!running) draw();
    },
    setRunning,
    dispose() {
      disposed = true;
      setRunning(false);
      resize.disconnect();
      host.removeEventListener('pointermove', pointer);
      host.removeEventListener('pointerleave', leave);
      const geometries = new Set(), materials = new Set();
      scene.traverse(object => {
        if (object.geometry) geometries.add(object.geometry);
        if (object.material) materials.add(object.material);
      });
      geometries.forEach(geometry => geometry.dispose());
      materials.forEach(material => material.dispose());
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
