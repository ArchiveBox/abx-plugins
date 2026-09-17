const status = document.querySelector('#status');
const result = document.querySelector('#result');
const trust = await fetch('./trust.json').then(r => {if(!r.ok)throw Error('Trusted key unavailable');return r.json();});
document.querySelector('#key').textContent = trust.public_key;
document.querySelector('#command').textContent = `cargo build --release --locked --manifest-path runtime/Cargo.toml\n./runtime/target/release/abx-tlsnotary verify capture.tlsn \\\n  --trusted-key ${trust.public_key}`;
let worker, ready, downloadUrl, busy = false;
function newWorker() {
  worker?.terminate();
  worker = new Worker('./worker.js',{type:'module'});
  ready = new Promise((resolve,reject) => {
    worker.onmessage = event => {if(event.data.ready)resolve();};
    worker.onerror = () => reject(Error('The local WASM verifier could not start.'));
  });
  return ready;
}
await newWorker();
status.textContent = 'Ready. Files are verified on your device.';
async function verify(file) {
  if(!file || busy)return;
  result.hidden = true; status.className='';
  if(file.size>32*1024*1024) {status.textContent='File exceeds the 32 MiB limit.';status.className='invalid';return;}
  busy=true;document.querySelector('#file').disabled=true;
  status.textContent='Checking signature, certificate, timestamp and response coverage…';
  try {
    await newWorker();
    const bytes = await file.arrayBuffer();
    const checked = await new Promise((resolve,reject) => {
      const timer=setTimeout(()=>{worker.terminate();reject(Error('Verification exceeded 30 seconds.'));},30000);
      worker.onmessage=({data})=>{clearTimeout(timer);data.ok?resolve(data.result):reject(Error(data.error));};
      worker.onerror=()=>{clearTimeout(timer);reject(Error('Verification worker failed.'));};
      worker.postMessage({bytes,key:trust.public_key},[bytes]);
    });
    status.textContent=checked.response_complete?'Valid signature and authenticated complete response.':'Valid signature: authenticated PREFIX ONLY; the remainder is not certified.';status.className='valid';
    const facts=document.querySelector('#facts');facts.replaceChildren();
    const body=Uint8Array.from(atob(checked.body_base64),c=>c.charCodeAt(0));
    for(const [label,value] of Object.entries({URL:checked.url,'TLS connection':new Date(checked.connection_time_unix*1000).toISOString(),'HTTP status':checked.status,'Coverage':checked.response_complete?'Complete HTTP response':'Partial response — authenticated prefix only','Content type':checked.content_type,'Body bytes':body.length,'SHA-256':checked.body_sha256})) {
      const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=String(value);facts.append(dt,dd);
    }
    document.querySelector('#preview').textContent=new TextDecoder().decode(body.slice(0,200000))+(body.length>200000?'\n[Preview truncated; download contains all bytes.]':'');
    if(downloadUrl)URL.revokeObjectURL(downloadUrl);
    downloadUrl=URL.createObjectURL(new Blob([body],{type:'application/octet-stream'}));
    const download=document.querySelector('#download');download.href=downloadUrl;download.download='response.body';
    result.hidden=false;
  } catch(error) {status.textContent=`Not verified: ${error.message}`;status.className='invalid';}
  finally {busy=false;document.querySelector('#file').disabled=false;}
}
document.querySelector('#file').addEventListener('change',event=>verify(event.target.files[0]));
const drop=document.querySelector('#drop');drop.addEventListener('dragover',event=>event.preventDefault());drop.addEventListener('drop',event=>{event.preventDefault();verify(event.dataTransfer.files[0]);});
