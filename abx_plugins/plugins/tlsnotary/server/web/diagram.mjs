// Deterministic playback of the deployed protocol. No network calls or capture-file access.
const lab = document.querySelector('.exchange');
if (lab && !new URLSearchParams(location.search).has('compact')) {
  const $ = selector => lab.querySelector(selector);
  const toGateway = ['browser', 'edge', 'tunnel', 'gateway'];
  const toVerifier = [...toGateway, 'verifier'];
  const toWebsite = [...toGateway, 'website'];
  const reverse = route => [...route].reverse();
  const request = 'GET /private/report?key=example HTTP/1.1\nHost: documents.example\nCookie: session=example-secret';
  // Each entry describes semantic messages, not fabricated packet bytes or measured timing.
  const events = [
    { phase: 'Browser load', title: 'The browser loads the page normally', route: ['browser', 'direct', 'directTop', 'directEnd', 'website'], kind: 'tls', label: 'HTTPS request', payload: request, copy: 'The plugin opens the target URL in Chrome to obtain the request headers for the logged-in session. This initial navigation is ordinary HTTPS and is not the notarized request.', visibility: 'Readable at the browser and website. Encrypted on the network.' },
    { phase: 'Browser load', title: 'Chrome receives the initial page', route: ['website', 'directEnd', 'directTop', 'direct', 'browser'], kind: 'tls', label: 'HTTPS response', payload: 'HTTP/1.1 200 OK\n… response headers …\n\nPrivate report: budget is $100.', copy: 'Chrome receives and renders the ordinary response. The verifier has not witnessed this exchange. The extension will make a separate request using the captured browser headers.', visibility: 'The browser and website see the content. This first response has no TLSNotary receipt.' },
    { phase: 'Session setup', title: 'Prepare the request locally', route: ['browser'], kind: 'local', label: 'Prepare request', payload: request, copy: 'The hook passes the URL and captured headers to the extension. Cookies remain on the device until they are encrypted for the website. An opaque random receipt ID associates this capture with the eventual receipt.', visibility: 'URL path, query and cookies are local inputs. They are not session-registration metadata.' },
    { phase: 'Session setup', title: 'Register an MPC session', route: toVerifier, kind: 'control', label: 'register', endpoint: '/session', payload: '{ type: "register",\n  maxRecvData: 262144, maxSentData: 16384,\n  sessionData: { mode: "Mpc",\n    receiptId: "<random opaque ID>" } }', copy: 'A control WebSocket crosses the public edge and tunnel. The gateway limits admission and forwards only the byte limits, mode and opaque receipt ID to the verifier.', visibility: 'This control message is readable by the edge and service. It contains no target URL path, cookies or page contents.' },
    { phase: 'Session setup', title: 'Return the session identifier', route: reverse(toVerifier), kind: 'control', label: 'session ID', endpoint: '/session', payload: '{ type: "session_registered",\n  sessionId: "<assigned session ID>" }', copy: 'The verifier allocates the session. Its identifier travels back along the same control channel to the extension.', visibility: 'The session identifier is routing metadata, not a credential for the target website.' },
    { phase: 'Session setup', title: 'Open the cryptographic channel', route: toVerifier, kind: 'control', label: 'MPC channel', endpoint: '/verifier?sessionId=<ID>', payload: 'WebSocket upgrade\n/verifier?sessionId=<assigned ID>\n\nStart MPC protocol setup', copy: 'A second WebSocket carries the binary cryptographic protocol. The gateway routes it to the verifier container, separate from the website traffic proxy.', visibility: 'The service sees the session ID and protocol messages. Private inputs are protected by MPC, not merely by the outer WebSocket encryption.' },
    { phase: 'Session setup', title: 'Exchange MPC setup messages', route: toVerifier, kind: 'mpc', label: 'MPC setup →', roundTrip: true, endpoint: '/verifier', payload: 'MPC setup messages\nCryptographic encodings / protocol data\n\nNot raw cookies, HTTP text or key shares', copy: 'The extension and verifier prepare the joint computation. The animation groups many bidirectional protocol messages into one exchange; it does not represent a single round or a transfer of the secret key shares.', visibility: 'Each party keeps its private inputs. An observer of these messages does not thereby learn the website plaintext.' },
    { phase: 'Session setup', title: 'Open a transport route to the website', route: toGateway, kind: 'control', label: 'Open proxy', endpoint: '/proxy?token=documents.example&capture=<ID>', payload: 'WebSocket upgrade\n/proxy?token=documents.example\n       &capture=<opaque receipt ID>', copy: 'The proxy endpoint receives the destination hostname. It checks and resolves the address, then opens a TCP connection to the website on port 443.', visibility: 'The hostname and destination IP are visible. The private request path, query and cookies are not in this proxy URL.' },
    { phase: 'TLS handshake', title: 'The proxy opens the website TCP connection', route: ['gateway', 'website'], kind: 'control', label: 'TCP connect', payload: 'Destination: documents.example\nResolved public IPv4 address\nTCP port: 443', copy: 'The gateway is now a byte bridge between the extension’s WebSocket and the website’s TCP socket. It is not terminating the inner website TLS connection.', visibility: 'The website sees a connection from the proxy’s network address, rather than a direct connection from your device.' },
    { phase: 'TLS handshake', title: 'Send the website TLS ClientHello', route: toWebsite, kind: 'tls', label: 'ClientHello', payload: 'TLS handshake: ClientHello\nServer name: documents.example\nSupported cryptographic parameters', copy: 'The extension’s TLS handshake travels inside WebSocket frames until the gateway forwards the same inner TLS bytes over TCP. The outer WSS connection and the website TLS session are distinct.', visibility: 'The destination and public handshake information are not private page content. There is still no HTTP request on the wire.' },
    { phase: 'TLS handshake', title: 'The website returns its handshake messages', route: reverse(toWebsite), kind: 'tls', label: 'Server handshake', payload: 'TLS handshake messages\nServer certificate chain\nKey-exchange / authentication data', copy: 'The server’s handshake and certificate information travel back through the TCP bridge and tunnel. Server identity is checked as part of the TLSNotary protocol.', visibility: 'A certificate binds a server key to a name. It is not a signature on the page that will be returned later.' },
    { phase: 'TLS handshake', title: 'Derive and use TLS keys jointly', route: toVerifier, kind: 'mpc', label: 'Joint handshake', roundTrip: true, endpoint: '/verifier', payload: 'MPC key-exchange / handshake messages\nBrowser private input: its key share\nVerifier private input: its key share\n\nShares are not sent in plaintext', copy: 'The browser and verifier perform the cryptographic handshake operations together. The browser does not obtain unilateral control of the authentication keys needed to fabricate the exchange. These rounds interleave with the website handshake.', visibility: 'The private key shares remain with their owners. The website participates in an ordinary TLS connection.' },
    { phase: 'TLS handshake', title: 'Complete the authenticated TLS handshake', route: toWebsite, kind: 'tls', label: 'Handshake finish', roundTrip: true, payload: 'TLS handshake completion\nAuthenticated Finished messages\n\nWebsite TLS session established', copy: 'Handshake completion messages make the round trip over the proxy path. The HTTP request can now be protected using the joint TLS computation.', visibility: 'The transport intermediaries carry the inner TLS records. They do not acquire the website session keys.' },
    { phase: 'HTTP request', title: 'Encrypt the private HTTP request with MPC', route: toVerifier, kind: 'mpc', label: 'Encrypt request', roundTrip: true, endpoint: '/verifier', payload: 'MPC encryption + authentication messages\nPrivate browser input: HTTP request\nOutput: authenticated TLS ciphertext\n\nCookie header is not a public input', copy: 'The browser supplies the request as a private input. The parties jointly produce authenticated ciphertext without revealing the request plaintext to the verifier.', visibility: 'The verifier participates in the cryptography but does not read the URL path, query, cookies or authorization headers.' },
    { phase: 'HTTP request', title: 'Send the encrypted HTTP request, hop by hop', route: toWebsite, kind: 'tls', label: 'Encrypted GET', payload: 'Inner website TLS application record\n  [encrypted HTTP request bytes]\n  [authentication data]\n\nContains GET path + Cookie, encrypted', copy: 'Follow the same website TLS record through the edge, tunnel connector and gateway. The gateway removes WebSocket framing and writes the TLS bytes to the website socket; it does not decrypt them.', visibility: 'Only the website obtains the readable request at the destination. Cloudflare can terminate outer WSS without decrypting this inner TLS record.' },
    { phase: 'HTTP request', title: 'The website reads the authenticated request', route: ['website'], kind: 'local', label: 'Read request', payload: request, copy: 'The website decrypts the request and receives the cookies needed to serve logged-in content. It does not need a TLSNotary extension, signing API or special server modification.', visibility: 'The website necessarily sees the URL and credentials addressed to it. The independent verifier does not.' },
    { phase: 'HTTP response', title: 'The website constructs the private response', route: ['website'], kind: 'local', label: 'Create response', payload: 'HTTP/1.1 200 OK\n… response headers …\n\nPrivate report: budget is $100.', copy: 'The website selects the response for this session and encrypts it into TLS records. This is the response the extension will authenticate, not the earlier browser-rendered page.', visibility: 'Plaintext exists at the website. It will travel back as encrypted application data.' },
    { phase: 'HTTP response', title: 'Return the encrypted response through the proxy', route: reverse(toWebsite), kind: 'tls', label: 'Encrypted response', payload: 'Inner website TLS application records\n  [encrypted headers and response body]\n  [authentication data]\n\nNo readable HTML sent to the verifier', copy: 'The website’s TCP bytes enter the gateway, which places them in WebSocket frames for the return path. The edge, tunnel and proxy relay ciphertext; the inner record contents remain encrypted at every intermediary.', visibility: 'Intermediaries can observe size and timing. The response headers, page text and embedded secrets remain encrypted.' },
    { phase: 'HTTP response', title: 'Authenticate and decrypt with the verifier', route: toVerifier, kind: 'mpc', label: 'Check + decrypt', roundTrip: true, endpoint: '/verifier', payload: 'MPC authentication / decryption messages\nInputs include authenticated ciphertext\nPrivate inputs include key shares\n\nReadable output: browser only', copy: 'The parties check the server records and process their decryption. The readable response becomes available only to the prover. A real implementation can interleave this work with receiving records.', visibility: 'The verifier checks the exchange without learning the response plaintext.' },
    { phase: 'HTTP response', title: 'The extension holds the authenticated bytes', route: ['browser'], kind: 'local', label: 'Local transcript', payload: 'HTTP/1.1 200 OK\n… response headers …\n\nPrivate report: budget is $100.', copy: 'The extension now holds the request and response transcript locally. The next step binds the complete received response to a commitment that the gateway can attest to.', visibility: 'Readable content is on the user’s device. It is not uploaded to obtain the receipt.' },
    { phase: 'Commitment proof', title: 'Specify what may be disclosed', route: toVerifier, kind: 'control', label: 'reveal_config', endpoint: '/session', payload: 'type: "reveal_config"\nsent: []\nrecv: [\n  PROTOCOL → REVEAL fixed HTTP syntax,\n  ALL → HASH with SHA256\n]', copy: 'The extension sends the range and handler configuration before the proof. The gateway rejects disclosure of request bytes or arbitrary response text; only fixed HTTP syntax and the whole-response hash are allowed.', visibility: 'The configuration and byte ranges are visible. The response itself and its headers are not part of this control message.' },
    { phase: 'Commitment proof', title: 'Prove the commitment matches the TLS response', route: toVerifier, kind: 'mpc', label: 'Commitment proof', roundTrip: true, endpoint: '/verifier', payload: 'Commitment: SHA256(response || blinder)\nZero-knowledge / verification messages\nDisclosed: server identity + fixed syntax\n\nResponse + random blinder stay local', copy: 'The verifier checks that the commitment is bound to the authenticated response. It learns the commitment and allowed disclosures, without receiving the response opening. Many proof messages are grouped here.', visibility: 'The commitment is not a copy of the page. Its random blinder prevents guessing the response by simply hashing likely plaintexts.' },
    { phase: 'Receipt', title: 'Send the verified result to the gateway', route: ['verifier', 'gateway'], kind: 'control', label: 'Verified webhook', endpoint: 'POST /webhook (private)', payload: 'Authenticated server: documents.example\nTranscript lengths + permitted syntax\nVerified SHA256 commitment\nSession / receipt ID', copy: 'The verifier reports the successful result over the private container network. The gateway checks the full-response range and allowed disclosure before issuing anything.', visibility: 'This is an internal result message. It contains no readable response or request secrets.' },
    { phase: 'Receipt', title: 'Sign the compact receipt', route: ['gateway'], kind: 'receipt', label: 'Ed25519 signing', payload: 'Signed payload:\n  server_name, time, algorithm: SHA256\n  hash, start: 0, end: <response length>\nSignature: Ed25519 over payload', copy: 'The gateway signs the commitment, hostname, byte range and its own issuance time. This signature is from the verifier operator, not from the website.', visibility: 'Later reviewers must trust this signing key and the operator’s protocol execution and clock.' },
    { phase: 'Receipt', title: 'The archiver requests its receipt', route: toGateway, kind: 'control', label: 'GET receipt', endpoint: 'GET /receipts/<opaque ID>', payload: 'GET /receipts/<opaque receipt ID>\n\nNo response.http upload', copy: 'After the extension finishes, the local hook fetches the compact receipt. This HTTP request identifies the capture with an opaque random ID, not the private website URL.', visibility: 'The lookup and receipt travel through the public service. The captured response stays local.' },
    { phase: 'Receipt', title: 'Return the signed receipt to the device', route: reverse(toGateway), kind: 'receipt', label: 'Signed receipt', endpoint: '/receipts/<opaque ID>', payload: '{ payload: "<base64 signed fields>",\n  signature: "<Ed25519 signature>" }\n\nNo page contents. No blinder from server.', copy: 'The gateway returns the signature and signed payload. The hook adds the locally held blinder, verifies the result, and prepares the two files together.', visibility: 'The service does not need a copy of the response to deliver the receipt.' },
    { phase: 'Local archive', title: 'Save the response and receipt; verify later locally', route: ['browser', 'archive'], kind: 'local', label: 'Save locally', payload: 'response.http → original response bytes\nreceipt.json → payload + signature\n               + local blinder\n\nLater: check signature, hash and HTTP bounds', copy: 'The response is stored once. A later reviewer checks the signature using an independently trusted verifier key, then opens the commitment with the saved response and blinder. The original servers need not be online.', visibility: 'Anyone given response.http can read it. This receipt covers that response, not the entire browser tab or other snapshot outputs.' },
  ];
  const positions = { browser: [95,280], direct: [95,70], directTop: [805,70], directEnd: [805,150], edge: [300,280], tunnel: [495,280], gateway: [700,280], website: [900,150], verifier: [900,300], archive: [95,440] };
  const names = { browser: 'Your browser', edge: 'Cloudflare edge', tunnel: 'cloudflared', gateway: 'Gateway', website: 'Website', verifier: 'TLSNotary verifier', archive: 'Snapshot directory', direct: 'Direct website connection' };
  const colors = { control:'#e7be79', tls:'#7dbfff', mpc:'#c5a6ff', receipt:'#f5d974', local:'#89e5bd' };
  const slider = $('#exchange-time');
  const play = $('#exchange-play');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const duration = 7; // Pedagogical seconds per message group, never a performance claim.
  let time = 0, playing = !reduced.matches, visible = false, frame = 0, last = 0, previousEvent = -1, previousHop = '';
  slider.max = events.length * 100 - 1;
  function render() {
    const index = Math.min(Math.floor(time), events.length - 1);
    const event = events[index];
    const progress = time - index;
    const points = event.route.map(name => positions[name]);
    const journey = event.roundTrip ? [...event.route, ...reverse(event.route).slice(1)] : event.route;
    const travel = Math.min(1, Math.max(0, (progress - 0.13) / 0.74));
    const distance = travel * Math.max(1, journey.length - 1);
    const segment = Math.min(Math.floor(distance), Math.max(0, journey.length - 2));
    const from = journey[segment], to = journey[Math.min(segment + 1, journey.length - 1)];
    const a = positions[from], b = positions[to];
    const fraction = Math.min(1, distance - segment);
    const x = a[0] + (b[0] - a[0]) * fraction, y = a[1] + (b[1] - a[1]) * fraction;
    $('#exchange-packet').setAttribute('transform', `translate(${x} ${y})`);
    $('#exchange-route').setAttribute('d', points.map((p,i) => `${i ? 'L' : 'M'} ${p.join(' ')}`).join(' '));
    lab.style.setProperty('--message-color', colors[event.kind]);
    slider.value = Math.round(time * 100);
    const hopKey = `${index}:${from}:${to}`;
    if (hopKey !== previousHop) {
      previousHop = hopKey;
      $('#exchange-hop').textContent = index < 2 ? (index === 0 ? 'Your browser → Website (direct HTTPS)' : 'Website → Your browser (direct HTTPS)') : from === to ? `${names[from]} · local operation` : `${names[from]} → ${names[to]}`;
      let transport;
      if (from === to) transport = 'No network hop. These bytes are processed at this endpoint.';
      else if (from.startsWith('direct') || to.startsWith('direct')) transport = 'Direct browser-to-website HTTPS. This initial navigation does not use the TLSNotary proxy.';
      else if (to === 'archive') transport = 'Local filesystem write. Neither file is uploaded to the verifier.';
      else if ([from,to].includes('website')) transport = 'TCP bridge carries inner website TLS bytes unchanged. No WebSocket framing on the website socket.';
      else if ([from,to].includes('browser')) transport = 'Outer HTTPS/WSS to the Cloudflare edge. Website TLS records inside it remain separately encrypted.';
      else if ([from,to].includes('edge')) transport = 'Encrypted Cloudflare Tunnel over HTTP/2 to the connector on Cabbage.';
      else if ([from,to].includes('tunnel')) transport = 'Local HTTP/WS from cloudflared to the gateway. Inner website TLS, when present, remains encrypted.';
      else transport = event.endpoint?.includes('webhook') ? 'Internal HTTP webhook on the private container network.' : 'Private-network WebSocket between gateway and verifier. This carries MPC/proof or control messages, not a plaintext website transcript.';
      $('#exchange-hop-detail').textContent = transport;
      slider.setAttribute('aria-valuetext', `Message ${index+1} of ${events.length}: ${event.title}. ${$('#exchange-hop').textContent}`);
    }
    if (index !== previousEvent) {
      previousEvent = index;
      $('#exchange-position').textContent = `${String(index+1).padStart(2,'0')} / ${events.length}`;
      $('#exchange-phase').textContent = event.phase;
      $('#exchange-heading').textContent = event.title;
      $('#exchange-copy').textContent = event.copy;
      $('#exchange-payload').textContent = event.payload;
      $('#exchange-payload-label').textContent = event.endpoint || (index < 2 ? 'Inside ordinary HTTPS · example plaintext' : event.kind === 'local' ? 'Inside this endpoint · example values' : 'Message contents · example values');
      $('#exchange-visibility').textContent = event.visibility;
      $('#packet-label').textContent = event.label;
      $('#browser-memory').textContent = index < 19 ? 'Browser: private URL + session cookies' : index < 21 ? 'Browser: authenticated response bytes' : index < 26 ? 'Browser: response + secret blinder' : 'Saved: response.http + receipt.json';
      $('#verifier-memory').textContent = index < 3 ? 'Verifier: not involved yet' : index < 11 ? 'Verifier: session + protocol setup' : index < 21 ? 'Verifier: key share; no page plaintext' : 'Verifier: identity + commitment; no page plaintext';
      lab.querySelectorAll('[data-node]').forEach(node => node.classList.toggle('node-active', event.route.includes(node.dataset.node)));
    }
  }
  function label() { play.textContent = playing ? 'Pause' : time >= events.length - 0.01 ? 'Replay' : 'Play'; play.setAttribute('aria-label', playing ? 'Pause exchange' : 'Play exchange'); }
  function tick(now) {
    frame = 0;
    if (!playing || !visible || document.hidden) return;
    time = Math.min(events.length - 0.001, time + Math.min((now-last)/1000,0.1)/duration);
    last = now;
    render();
    if (time >= events.length - 0.001) playing = false;
    label();
    if (playing) frame = requestAnimationFrame(tick);
  }
  function schedule() {
    cancelAnimationFrame(frame);
    last = performance.now();
    if (playing && visible && !document.hidden) frame = requestAnimationFrame(tick);
    label();
  }
  play.addEventListener('click', () => {
    if (time >= events.length - 0.01) time = 0;
    playing = !playing;
    render(); schedule();
  });
  slider.addEventListener('input', () => { playing = false; time = Number(slider.value)/100; render(); schedule(); });
  reduced.addEventListener('change', () => { if (reduced.matches) playing = false; schedule(); });
  document.addEventListener('visibilitychange', schedule);
  const observer = new IntersectionObserver(entries => { visible = entries[0].isIntersecting; schedule(); }, {threshold:0});
  observer.observe(lab);
  addEventListener('pagehide', () => cancelAnimationFrame(frame));
  addEventListener('pageshow', schedule);
  render(); label();
}
