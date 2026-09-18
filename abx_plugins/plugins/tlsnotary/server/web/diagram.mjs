// Teaching animation only. It never contacts a service or reads selected capture files.
const lab = document.querySelector('.exchange');
if (lab && !new URLSearchParams(location.search).has('compact')) {
  const $ = selector => lab.querySelector(selector);
  const request = 'GET /private/report?key=example HTTP/1.1\nHost: example.com\nCookie: session=example-secret';
  const body = 'Private report: budget is $100.';
  const response = `HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: ${body.length}\r\n\r\n${body}`;
  // Repeated rounds are grouped. Routes represent the three protocol participants.
  const events = [
    { phase:'Prepare', route:['client'], kind:'local', label:"Private request", detail:"URL + cookies · kept locally", title:'Prepare the private request', copy:'The extension uses request headers from Chrome for a separate TLSNotary request. The URL path and session cookies are private inputs on the client.', payload:request, visibility:'This animation starts after the ordinary browser load. That earlier load is not the notarized exchange.' },
    { phase:'Setup', route:['client','verifier'], kind:'control', label:"Request limits", detail:"Byte limits + capture ID", title:'Ask the verifier to participate', copy:'The client registers a session with byte limits and an opaque identifier. It does not send the private URL path, cookies or page content as registration metadata.', payload:'register\nmode: MPC\nmax received: 262144 bytes\nmax sent: 16384 bytes\nreceipt ID: <random opaque ID>', visibility:'The verifier can read these session settings. They are not the private HTTP request.' },
    { phase:'Setup', route:['verifier','client'], kind:'control', label:"Session ID", detail:"ID for this capture", title:'The verifier accepts the session', copy:'The verifier returns a session identifier. The client uses it to associate the following cryptographic exchanges with this capture.', payload:'session_registered\nsession ID: <assigned ID>', visibility:'The session ID is protocol metadata, not a credential for the website.' },
    { phase:'Setup', route:['client','verifier'], roundTrip:true, kind:'mpc', label:"Privacy setup", detail:"Cryptographic setup messages", title:'Prepare the joint computation', copy:'The client and verifier set up secure multi-party computation (MPC): a way to compute together while keeping their inputs private. They will use it to encrypt and check TLS traffic without giving the verifier the cookies, page contents or complete TLS keys.', payload:'Cryptographic setup messages for joint computation\nCookies, page content and secret keys are not sent', visibility:'The verifier participates without receiving the cookies or the response.' },
    { phase:'TLS handshake', route:['client','server'], kind:'handshake', label:"TLS ClientHello", detail:"Hostname + TLS options", title:'Start the website TLS handshake', copy:'The client sends a TLS ClientHello to the server. The website sees a normal TLS connection; it does not need TLSNotary-specific software.', payload:'ClientHello\nserver name: example.com\nsupported TLS parameters', visibility:'The destination name is public here. There is no HTTP request or cookie in this message.' },
    { phase:'TLS handshake', route:['server','client'], kind:'handshake', label:"Server certificate", detail:"Certificate + TLS options", title:'Receive the server’s identity information', copy:'The server returns its handshake messages and certificate information. This identifies the server key; it is not a server signature on the later page contents.', payload:'ServerHello and related messages\ncertificate chain\nkey-exchange information', visibility:'Server identity is checked in the TLSNotary protocol. Keeping a certificate alone would not authenticate an archived response.' },
    { phase:'TLS handshake', route:['client','verifier'], roundTrip:true, kind:'mpc', label:"TLS key setup", detail:"Compute with private key shares", title:'Perform the key operations jointly', copy:'The client and verifier jointly perform the TLS cryptographic operations, keeping their key shares private. These MPC rounds interleave with the server handshake in a real exchange.', payload:'Cryptographic messages for computing TLS keys together\nEach participant keeps its own key share private', visibility:'The client cannot unilaterally construct an authenticated false exchange and convince an honest verifier.' },
    { phase:'TLS handshake', route:['client','server'], roundTrip:true, kind:'handshake', label:"TLS Finished", detail:"Handshake integrity checks", title:'Finish the authenticated handshake', copy:'The participants complete the TLS handshake. The resulting connection can now carry the private HTTP request.', payload:'Authenticated Finished messages\nWebsite TLS session established', visibility:'The certificate and joint handshake establish the connection; later response bytes still need to be bound to the receipt.' },
    { phase:'Request', route:['client','verifier'], roundTrip:true, kind:'mpc', label:"Request encryption", detail:"Compute without revealing cookies", title:'Encrypt the request without exposing it', copy:'The client and verifier compute the encrypted request together. The verifier receives cryptographic computation messages, while the URL path and cookies stay private. The resulting encrypted GET request goes to the website in the next step.', payload:'Exchanged: cryptographic computation messages\nKept on Client: GET path, query and cookies\nResult: encrypted request for Server', visibility:'The verifier receives cryptographic messages, not the readable HTTP request.' },
    { phase:'Request', route:['client','server'], kind:'request', label:"Encrypted GET", detail:"URL + cookies · encrypted", title:'Send the encrypted GET request', copy:'The client sends the authenticated TLS record to the server. The server decrypts it and uses the session cookies to select the logged-in response.', payload:'TLS application record\n  [encrypted GET path and query]\n  [encrypted Cookie header]\n  [authentication data]', visibility:'The label describes the encrypted contents. Only the website receives the readable request at the destination.' },
    { phase:'Response', route:['server'], kind:'response', label:"Response prepared", detail:"HTTP headers + page content", title:'The server prepares its response', copy:'The server constructs the response for this logged-in session and encrypts it into TLS records.', payload:response, visibility:'The response is readable at the server. It has not yet arrived at the client.' },
    { phase:'Response', route:['server','client'], kind:'response', label:"Response chunk 1", detail:"Encrypted bytes + integrity tag", title:'Receive the first encrypted response chunk', copy:'The server’s response begins arriving at the client. The panel records the arrival of ciphertext; it does not reveal a body before decryption.', payload:'TLS response record group 1\n[encrypted response bytes]\n[authentication data]', visibility:'Record grouping is illustrative. Real responses may use many more records.' },
    { phase:'Response', route:['server','client'], kind:'response', label:"Response chunk 2", detail:"Encrypted bytes + integrity tag", title:'Receive the rest of the encrypted response', copy:'More encrypted bytes arrive. The response data is collected locally, ready for the authenticated decryption process.', payload:'TLS response record group 2\n[remaining encrypted bytes]\n[authentication data]', visibility:'Page contents are still hidden from the verifier. Only the ciphertext and allowed metadata are visible.' },
    { phase:'Response', route:['client','verifier'], roundTrip:true, kind:'mpc', label:"Response decryption", detail:"Readable result only on Client", title:'Authenticate and decrypt the response', copy:'The parties process the TLS records together. The readable result is delivered only to the client. In a real implementation, this work can interleave with receiving records.', payload:'Exchanged: cryptographic computation messages\nChecked: encrypted response + integrity tags\nResult on Client only: readable HTTP response', visibility:'The verifier checks the cryptography without learning the response plaintext.' },
    { phase:'Response', route:['client'], kind:'response', label:"response.http", detail:"Original headers + page content", title:'Assemble the local response transcript', copy:'The complete readable response is now on the client. Watch it appear in the package panel. These are the bytes later saved as response.http.', payload:response, visibility:'This content stays with the client. It is not sent to the verifier to obtain the receipt.' },
    { phase:'Proof', route:['client','verifier'], kind:'control', label:"Disclosure rules", detail:"Which bytes may be revealed", title:'Specify the allowed disclosures', copy:'The client requests a SHA-256 commitment to the full response. Only fixed HTTP syntax is disclosed; the private request and readable response are withheld.', payload:'sent: []\nrecv:\n  PROTOCOL → fixed syntax only\n  ALL → SHA256 commitment', visibility:'Byte ranges and disclosure rules are visible. The response opening remains private.' },
    { phase:'Proof', route:['client','verifier'], roundTrip:true, kind:'proof', label:"Response hash proof", detail:"Link hash to server response", title:'Prove that the commitment matches the response', copy:'The client binds the authenticated response to a blinded hash. The verifier checks the proof without receiving the response or the blinder.', payload:'H = SHA256(response || blinder)\nZK verification messages\nServer identity + permitted syntax\nResponse and blinder remain local', visibility:'The hash alone would not prove origin. Its binding to the authenticated TLS exchange is what the verifier checks here.' },
    { phase:'Proof', route:['verifier'], kind:'proof', label:"Hash verified", detail:"Server identity + response hash", title:'Accept the authenticated commitment', copy:'The verifier has checked the protocol result, server identity and response commitment. It can now attest to the result without possessing the page contents.', payload:'Authenticated server identity\nVerified response commitment\nFull response byte range\nPermitted disclosures only', visibility:'A later reviewer trusts the verifier’s attestation; the compact receipt does not replay the interactive proof.' },
    { phase:'Receipt', route:['verifier'], kind:'receipt', label:"Sign receipt", detail:"Hash + hostname + time", title:'Sign the compact receipt', copy:'The verifier service signs the response commitment, server name, byte range and its own issuance time. The signature is the verifier operator’s, not the website’s.', payload:'Signed fields:\nserver_name, time, algorithm\nhash, start, end\nEd25519 signature over the payload', visibility:'The receipt binds the response hash to the server name, byte range and issuance time.' },
    { phase:'Receipt', route:['verifier','client'], kind:'receipt', label:"Signed receipt", detail:"Signed statement + signature", title:'Deliver the receipt to the client', copy:'The client receives the verifier’s signed statement identifying the server, response hash, byte range and issuance time. It checks the signature, then saves the receipt alongside the response and the random value used to calculate its hash.', payload:'From verifier: signed statement + signature\nAlready on client: response + random value\nTogether: the package for later verification', visibility:'The response bytes have not been uploaded. The receipt contains no duplicate copy of the response.' },
    { phase:'Save', route:['client'], kind:'local', label:"Save package", detail:"response.http + receipt.json", title:'Save the two artifacts', copy:'The client stores response.http once and saves receipt.json beside it. The intermediate handshake data and protocol messages are not extra proof files required by this integration.', payload:'response.http: original response bytes\nreceipt.json: signed payload\n              signature + blinder', visibility:'The saved response is sensitive. Someone given response.http can read its contents.' },
    { phase:'Verify later', route:['client'], kind:'local', label:"Check saved files", detail:"Check signature + response hash", title:'Verify the saved evidence later', copy:'A reviewer checks the receipt’s signature with an independently trusted verifier key, then recomputes the blinded hash and checks the byte range and HTTP completeness. Neither original server needs to be online.', payload:'1. Check Ed25519 signature\n2. Hash response + saved blinder\n3. Match commitment and byte range\n4. Check complete HTTP response', visibility:'Trust remains in the verifier operator, its signing key and clock, the server identity checks and the checking software.' },
  ];
  const colors = {control:'#e7be79',handshake:'#70dcca',request:'#7dbfff',response:'#ffa7bd',mpc:'#c5a6ff',proof:'#dfadff',receipt:'#f5d974',local:'#89e5bd'};
  const names = {client:'Client',server:'Server',verifier:'Verifier'};
  const slider = $('#exchange-time'), play = $('#exchange-play');
  let time=0, playing=true, visible=false, frame=0, last=0, previousEvent=-1;
  const blinder = new Uint8Array(16); // Fixed example opening, not a production blinder.
  let exampleHash='SHA256(response || example blinder)';
  const responseBytes = new TextEncoder().encode(response);
  const hashInput = new Uint8Array(responseBytes.length+blinder.length);
  hashInput.set(responseBytes); hashInput.set(blinder,responseBytes.length);
  // Real hash for the illustrative bytes; this is not an MPC proof or notarization.
  if (crypto.subtle) crypto.subtle.digest('SHA-256',hashInput).then(buffer=>{
    exampleHash=Array.from(new Uint8Array(buffer),b=>b.toString(16).padStart(2,'0')).join('');
    render();
  }).catch(()=>{});
  const receiptText = () => `server_name: example.com\ntime: <verifier issuance time>\nalgorithm: SHA256\nhash: ${exampleHash}\nstart: 0\nend: ${responseBytes.length}\nsignature: Ed25519(verifier key, payload)`;
  const entries = [
    {at:0,source:'client',label:events[0].label,text:()=> "The client keeps the URL path and cookies locally. They select the private page to request. They are not sent to Verifier."},
    {at:1,source:'client',label:events[1].label,text:()=> "Client sends Verifier a capture ID and maximum request/response sizes. This reserves a verification session; it contains no page content."},
    {at:2,source:'verifier',label:events[2].label,text:()=> "Verifier returns a session ID. Client uses it to connect the following exchanges to this capture."},
    {at:3,source:'joint',label:events[3].label,text:()=> "Client and Verifier exchange cryptographic setup messages. This lets them compute together while keeping their inputs private. No page content is collected yet."},
    {at:4,source:'client',label:events[4].label,text:()=> "Client sends example.com the hostname and supported TLS options. This starts the encrypted connection."},
    {at:5,source:'server',label:events[5].label,text:()=> "Server returns its certificate chain and TLS handshake data. These identify example.com; they do not sign the page contents."},
    {at:6,source:'joint',label:events[6].label,text:()=> "Client and Verifier compute TLS keys together. Each keeps its own secret key share. Client cannot invent a valid exchange alone."},
    {at:7,source:'joint',label:events[7].label,text:()=> "Handshake checks complete. The connection to example.com is authenticated and ready for the private request."},
    {at:8,source:'joint',label:events[8].label,text:()=> "Client supplies the URL path and cookies privately. Verifier helps compute their encrypted form without seeing them. The GET request goes to Server in the next step."},
    {at:9,source:'client',label:events[9].label,text:()=> "Client sends Server the encrypted GET request, including its private URL path and cookies. Server can decrypt it; Verifier cannot read it."},
    {at:10,source:'server',label:events[10].label,text:()=> "Server prepares the HTTP status, headers and private page content. These bytes have not reached Client yet."},
    {at:11,source:'server',label:events[11].label,text:()=> "Client collects the first group of encrypted response bytes and integrity tags from Server. The readable page is not available yet."},
    {at:12,source:'server',label:events[12].label,text:()=> "Client collects the remaining encrypted response bytes and integrity tags. These will be checked and decrypted together."},
    {at:13,source:'joint',label:events[13].label,text:()=> "Client and Verifier check the response integrity and compute its decryption. Only Client receives the readable headers and page content."},
    {at:14,source:'server',label:events[14].label,text:()=> response.replaceAll('\r','')},
    {at:15,source:'client',label:events[15].label,text:()=> "Client asks Verifier to authenticate a hash of the full response. Only fixed HTTP syntax may be revealed; the private URL, cookies and page content stay hidden."},
    {at:16,source:'joint',label:events[16].label,text:()=> "Client hashes the response with a fresh random value (the “blinder”). A zero-knowledge proof lets Verifier check that this hash matches the authenticated response without seeing either input. Client keeps both for later verification."},
    {at:17,source:'verifier',label:events[17].label,text:()=> "Verifier has checked the server identity and the proof linking the hash to the response. It can now sign that result without possessing the page contents."},
    {at:18,source:'verifier',label:events[18].label,text:receiptText},
    {at:19,source:'verifier',label:events[19].label,text:()=> "Client receives the signed response hash, server name, byte range and time. It checks the signature, then adds its saved random value beside the signed statement so a reader can recompute the same hash later."},
    {at:20,source:'client',label:events[20].label,text:()=> "response.http — original server response.\nreceipt.json — verifier’s signed statement and signature, plus the client’s saved random value.\nThe response is stored once; the receipt contains its hash, not another copy."},
    {at:21,source:'client',label:events[21].label,text:()=> "Check the signature using a trusted verifier key.\nHash response.http with the saved random value and compare with the signed hash.\nCheck the signed byte range and complete HTTP response."},
  ];
  const paper = $('#evidence-lines');
  const rows=entries.map(entry=>{
    const row=document.createElement('div'); row.className='evidence-entry'; row.dataset.source=entry.source;
    const heading=document.createElement('div'); heading.className='evidence-entry-heading';
    const tag=document.createElement('span'); tag.className='evidence-source'; tag.textContent=entry.source==='joint'?'Client + Verifier':names[entry.source];
    const title=document.createElement('strong'); title.textContent=entry.label;
    const text=document.createElement('pre');
    heading.append(tag,title); row.append(heading,text); paper.append(row);
    return {row,text};
  });
  slider.max=events.length*100-1;
  let lastTyped='';
  function renderEvidence(index,progress) {
    // Arrivals add entries only after the message reaches its destination.
    let active=-1;
    entries.forEach((entry,i)=>{
      const age=time-entry.at;
      const start=events[entry.at].route.length>1 ? 0.86 : 0.08;
      const amount=Math.max(0,Math.min(1,(age-start)/(0.98-start)));
      rows[i].row.hidden=amount===0;
      if (amount>0) active=i;
      const value=entry.text();
      const typed=value.slice(0,Math.floor(value.length*amount));
      if(rows[i].text.textContent!==typed) rows[i].text.textContent=typed;
      rows[i].row.classList.toggle('is-typing',amount>0&&amount<1);
    });
    const proving=index===3||index===6||index===8||index===13||index===16;
    $('#proof-machine').hidden=!proving;
    $('#proof-machine-label').textContent=events[index].label;
    const glyphs='░▒▓█';
    $('#proof-scramble').textContent=Array.from({length:28},(_,i)=>glyphs[(i*7+Math.floor(time*38))%4]).join('');
    $('#proof-machine-progress').value=progress;
    // Follow the writing while playing or scrubbing; this never scrolls the main page.
    const key=`${active}:${Math.floor(time*100)}`;
    if(key!==lastTyped){paper.scrollTop=paper.scrollHeight;lastTyped=key;}
  }
  function render(){
    const index=Math.min(Math.floor(time),events.length-1), event=events[index], progress=time-index;
    const travel=Math.min(1,Math.max(0,(progress-0.1)/0.75));
    const isLocal=event.route.length===1, isServer=event.route.includes('server');
    const returning=event.roundTrip?travel>=0.5:event.route[0]!=='client';
    const leg=event.roundTrip?(travel<0.5?travel*2:2-travel*2):returning?1-travel:travel;
    const a=isServer?[330,420]:[470,420], b=isServer?[150,215]:[650,215];
    const amount=0.2+leg*0.6;
    let position=[a[0]+(b[0]-a[0])*amount,a[1]+(b[1]-a[1])*amount];
    if(isLocal)position=event.route[0]==='client'?[400,569]:event.route[0]==='server'?[150,172]:[650,172];
    $('#exchange-packet').setAttribute('transform',`translate(${position.join(' ')})`);
    $('#exchange-route').setAttribute('d',isLocal?'':`M ${a.join(' ')} L ${b.join(' ')}`);
    lab.style.setProperty('--message-color',colors[event.kind]);
    $('#packet-label').textContent=event.label;
    $('#packet-detail').textContent=event.detail;
    const route=returning&&event.roundTrip?[...event.route].reverse():event.route;
    const direction=isLocal?`${names[route[0]]} · local operation`:`${names[route[0]]} → ${names[route[1]]}`;
    $('#exchange-hop').textContent=direction;
    slider.value=Math.round(time*100);
    slider.setAttribute('aria-valuetext',`Message ${index+1} of ${events.length}: ${event.title}. ${direction}`);
    if(index!==previousEvent){
      previousEvent=index;
      $('#exchange-position').textContent=`${String(index+1).padStart(2,'0')} / ${events.length}`;
      $('#exchange-phase').textContent=event.phase;
      $('#exchange-heading').textContent=event.title;
      $('#exchange-copy').textContent=event.copy;
      $('#exchange-payload').textContent=event.payload;
      $('#exchange-payload-label').textContent=isLocal?'Inside this participant':'Message contents · illustrative';
      $('#exchange-visibility').textContent=event.visibility;
      $('#exchange-hop-detail').textContent=isLocal?'No network transfer at this step.':isServer?'Website connection: TLS handshake or encrypted HTTP records.':'Verification connection: protocol messages and explicitly permitted disclosures.';
      $('#browser-memory').textContent=index<14?'Private URL + cookies':index<16?'Authenticated response':'Response + private blinder';
      $('#verifier-memory').textContent=index<2?'Awaiting a session':index<17?'No readable page contents':'Verified hash · no plaintext';
      lab.querySelectorAll('[data-node]').forEach(node=>node.classList.toggle('node-active',event.route.includes(node.dataset.node)));
    }
    renderEvidence(index,progress);
  }
  function label(){play.textContent=playing?'Pause':time>=events.length-0.01?'Replay':'Play';play.setAttribute('aria-label',playing?'Pause exchange':'Play exchange');}
  function tick(now){
    frame=0;if(!playing||!visible||document.hidden)return;
    time=Math.min(events.length-0.001,time+Math.min((now-last)/1000,0.1)/(events[Math.floor(time)].route.length===1?1.5:4));last=now;render();
    if(time>=events.length-0.001)playing=false;label();if(playing)frame=requestAnimationFrame(tick);
  }
  function schedule(){cancelAnimationFrame(frame);last=performance.now();if(playing&&visible&&!document.hidden)frame=requestAnimationFrame(tick);label();}
  play.addEventListener('click',()=>{if(time>=events.length-0.01)time=0;playing=!playing;render();schedule();});
  slider.addEventListener('input',()=>{playing=false;time=Number(slider.value)/100;render();schedule();});
  document.addEventListener('visibilitychange',schedule);
  const observer=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;schedule();},{threshold:0});observer.observe(lab);
  addEventListener('pagehide',()=>cancelAnimationFrame(frame));addEventListener('pageshow',schedule);
  render();label();
}
