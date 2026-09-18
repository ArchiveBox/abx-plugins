// The teaching diagram is independent of app.mjs and never reads capture files.
const lab = document.querySelector('.protocol-lab');
if (lab && !new URLSearchParams(location.search).has('compact')) {
  const state = { phase: 0, privateView: false, altered: false };
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let paused = reducedMotion.matches, visible = false, scene, loading = false;
  const $ = selector => lab.querySelector(selector);
  const stages = [
    ['Step 01 / During capture', 'The browser cannot fabricate the TLS exchange on its own.', 'The browser and verifier jointly perform the TLS operations. The website serves an ordinary HTTPS response. The browser gets the readable bytes; the verifier participates without receiving the page or your cookies.'],
    ['Step 02 / During capture', 'Prove that hidden response bytes match a commitment.', 'A zero-knowledge proof binds a commitment to the authenticated response without disclosing its contents. After successful verification, our gateway signs the commitment, server name and its own issuance time. The response and the blinder stay on your device.'],
    ['Step 03 / After capture', 'Check the saved response against the signed commitment.', 'A later reviewer checks the receipt’s signature with a trusted public key, then recomputes the response commitment locally. The website and verifier need not be online. This delegates trust to the verifier that checked the original connection; it does not replay that proof.'],
  ];
  function motion() {
    $('.lab-motion').textContent = paused ? 'Play animation' : 'Pause animation';
    $('.lab-motion').setAttribute('aria-pressed', String(paused));
    scene?.setRunning(visible && !document.hidden && !paused);
  }
  function update() {
    lab.dataset.phase = state.phase;
    lab.dataset.view = state.privateView ? 'verifier' : 'device';
    lab.querySelectorAll('[data-phase]').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.phase) === state.phase)));
    lab.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-pressed', String((button.dataset.view === 'verifier') === state.privateView)));
    const [label, title, copy] = stages[state.phase];
    $('.lab-step-label').textContent = label;
    $('.lab-phase-title').textContent = title;
    $('.lab-phase-copy').textContent = copy;
    lab.querySelectorAll('[data-secret]').forEach(field => { field.textContent = state.privateView ? '••••••••  not disclosed' : field.dataset.secret; });
    $('.lab-visibility-note').textContent = state.privateView
      ? 'The service sees the destination, connection metadata, commitment and fixed HTTP markers. It does not receive the readable path, cookies or page contents.'
      : 'Illustrative data only. Your browser can read the response and save it locally.';
    $('.lab-path-labels').hidden = state.phase === 2;
    scene?.update(state);
  }
  lab.querySelectorAll('[data-phase]').forEach(button => button.addEventListener('click', () => {
    state.phase = Number(button.dataset.phase);
    update();
  }));
  lab.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    state.privateView = button.dataset.view === 'verifier';
    update();
  }));
  $('.lab-motion').addEventListener('click', () => { paused = !paused; motion(); });
  reducedMotion.addEventListener('change', () => { paused = reducedMotion.matches; motion(); });
  document.addEventListener('visibilitychange', motion);
  // The heavy WebGL module is fetched only when the diagram enters the viewport.
  const observer = new IntersectionObserver(async entries => {
    visible = entries[0].isIntersecting;
    motion();
    if (!visible || loading) return;
    loading = true;
    try {
      const { createScene } = await import('./scene.mjs?v=a0f082b9810e');
      scene = createScene($('.lab-canvas'));
      lab.classList.add('has-webgl');
      $('.lab-motion').hidden = false;
      scene.update(state);
      motion();
    } catch {
      // The static diagram, controls and explanatory text remain usable without WebGL.
      lab.classList.add('no-webgl');
    }
  }, { threshold: 0 });
  observer.observe($('.lab-stage'));
  addEventListener('pagehide', event => {
    if (!event.persisted) { observer.disconnect(); scene?.dispose(); }
  });
  update();

  // A genuine local commitment calculation over an explicitly illustrative response.
  const example = altered => `HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 30\r\n\r\nMeeting notes: budget is $${altered ? '900' : '100'}.`;
  const button = $('.lab-tamper');
  button.disabled = true;
  try {
    const blinder = crypto.getRandomValues(new Uint8Array(16));
    async function digest(altered) {
      const response = new TextEncoder().encode(example(altered));
      const bytes = new Uint8Array(response.length + blinder.length);
      bytes.set(response);
      bytes.set(blinder, response.length);
      return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), byte => byte.toString(16).padStart(2, '0')).join('');
    }
    const original = await digest(false);
    $('.lab-original').textContent = original;
    async function check() {
      button.disabled = true;
      const hash = await digest(state.altered);
      $('.lab-recomputed').textContent = hash;
      $('.lab-example-text').textContent = `Meeting notes: budget is $${state.altered ? '900' : '100'}.`;
      $('.lab-hash-result').textContent = hash === original
        ? 'MATCH — the example response opens the original commitment.'
        : 'MISMATCH — changing one digit produces a different commitment.';
      $('.lab-commitment').classList.toggle('is-altered', hash !== original);
      button.textContent = state.altered ? 'Restore original response' : 'Alter the example response';
      button.setAttribute('aria-pressed', String(state.altered));
      button.disabled = false;
    }
    button.addEventListener('click', async () => {
      state.altered = !state.altered;
      state.phase = 2;
      update();
      await check();
    });
    await check();
  } catch {
    $('.lab-hash-result').textContent = 'The interactive hash calculation needs Web Crypto in a secure browser context. Changing saved bytes would cause the commitment check to fail.';
  }
}
