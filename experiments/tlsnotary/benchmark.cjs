// Real browser experiment: uses ArchiveBox's Chrome helpers and TLSN's public API.
// No browser credentials, extension internals, or mocked verifier are used.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
process.env.PERSONAS_DIR = path.resolve(process.env.RESULT_DIR || path.join(__dirname, 'results'), 'personas');
process.env.ACTIVE_PERSONA = `tlsnotary-benchmark-${Date.now()}`;
const chrome = require('../../abx_plugins/plugins/chrome/chrome_utils.js');
const puppeteer = require(process.env.PUPPETEER_PATH || path.join(process.env.NODE_MODULES_DIR, 'puppeteer'));
const output = path.resolve(process.env.RESULT_DIR || path.join(__dirname, 'results'));
fs.mkdirSync(output, {recursive: true});
const extensionPath = process.env.TLSN_EXTENSION_PATH;
const verifierUrl = process.env.TLSN_VERIFIER_URL || 'http://127.0.0.1:17147';
const targets = [
  'https://example.com/',
  'https://raw.githubusercontent.com/tlsnotary/tlsn/ceadf458f6f75909eda013aa50108f9f94956188/crates/server-fixture/server/src/data/4kb.json',
];
if(process.env.INCLUDE_LARGE) targets.push('https://raw.githubusercontent.com/tlsnotary/tlsn-extension/d44623434d04ad00d7b1f8da9712cbc10cd0e99c/servers/verifier/src/main.rs');
const rows = [];
function save(row) {
  rows.push(row);
  fs.writeFileSync(path.join(output, 'measurements.json'), JSON.stringify(rows, null, 2));
  const {result,progress,...summary}=row;
  console.log(JSON.stringify(summary));
}
function responseBody(transcript) {
  const split=transcript.indexOf('\r\n\r\n');
  assert(split>0,'HTTP headers missing');
  const headers=transcript.slice(0,split);
  const wire=Buffer.from(transcript.slice(split+4));
  if(!/^transfer-encoding: chunked\r?$/im.test(headers)) return wire;
  const chunks=[];
  let offset=0;
  while(true) {
    const end=wire.indexOf('\r\n',offset);
    assert(end>=offset,'Missing chunk size');
    const sizeText=wire.subarray(offset,end).toString().split(';')[0];
    assert.match(sizeText,/^[0-9a-f]+$/i);
    const size=parseInt(sizeText,16);
    if(size===0) break;
    offset=end+2;
    assert(offset+size+2<=wire.length,'Truncated chunk');
    chunks.push(wire.subarray(offset,offset+size));
    assert.equal(wire.subarray(offset+size,offset+size+2).toString(),'\r\n');
    offset+=size+2;
  }
  return Buffer.concat(chunks);
}
function pluginCode(url, maxRecvData) {
  const target = new URL(url);
  const proxyUrl = verifierUrl.replace(/^http/, 'ws') + '/proxy?token=' + target.hostname;
  const config = {name: 'ArchiveBox public-page experiment', description: 'Prove a public GET response; no cookies or credentials.',
    requests: [{method:'GET', host:target.hostname, pathname:target.pathname, verifierUrl, proxyUrl}], urls:[], timeout:180000};
  const request = {url, method:'GET', headers:{Host:target.hostname, 'Accept-Encoding':'identity', Connection:'close'}};
  const options = {verifierUrl, proxyUrl, maxSentData:4096, maxRecvData,
    handlers:[{type:'SENT',part:'START_LINE',action:'REVEAL'},{type:'RECV',part:'ALL',action:'REVEAL'}]};
  return `const config = ${JSON.stringify(config)};
    function main() { useEffect(() => { run(); }, []); return div({}, ['Proving public response']); }
    async function run() { try { const result = await prove(${JSON.stringify(request)}, ${JSON.stringify(options)}); done(JSON.stringify({ok:true,result})); }
      catch(error) { done(JSON.stringify({ok:false,error:String(error)})); } }
    export default {config,main};`;
}
async function main() {
  const server = http.createServer((req,res) => {res.setHeader('Content-Type','text/html');res.end('<!doctype html><title>ArchiveBox TLSNotary benchmark</title><h1>Public GET proof benchmark</h1>');});
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    const started = performance.now();
    const launch = await chrome.launchChromium({
      binary:process.env.CHROME_BINARY || '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
      outputDir:path.join(output,'chrome'),
      CHROME_HEADLESS:true, CHROME_CHECK_SSL_VALIDITY:true, enableExtensionDebugging:true,
      CHROME_RESOLUTION:'1024,768',
    });
    assert(launch.success, launch.error);
    browser = await chrome.connectToBrowserEndpoint(puppeteer, launch.cdpUrl);
    fs.writeFileSync(path.join(output,'cdp-url.txt'),launch.cdpUrl);
    save({kind:'launch',ms:performance.now()-started,browser:await browser.version()});
    const page = await browser.newPage();
    await page.setCacheEnabled(false);
    async function navigations(kind) {
      for (let trial=0;trial<3;trial++) for(const url of targets) {
        const t=performance.now();
        const response=await page.goto(url,{waitUntil:'load',timeout:30000});
        const body=await response.buffer();
        assert.equal(response.status(),200);
        save({kind,url,trial,ms:performance.now()-t,bytes:body.length,sha256:crypto.createHash('sha256').update(body).digest('hex')});
      }
    }
    await navigations('baseline');
    const t=performance.now();
    await chrome.loadUnpackedExtensionsIntoBrowser(browser,[{name:'tlsnotary',unpacked_path:extensionPath}]);
    save({kind:'extension_install',ms:performance.now()-t});
    await navigations('installed_idle');
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => !!window.tlsn,{timeout:15000});
    save({kind:'extension_version',version:await page.evaluate(() => window.tlsn.version)});
    if(process.env.IDLE_ONLY) return;
    for(const mode of (process.env.MODES || 'Mpc,Proxy').split(',')) for(let trial=0;trial<Number(process.env.TRIALS || 3);trial++) for(const url of targets) {
      const baseline=rows.find(r=>r.kind==='baseline' && r.url===url);
      const maxRecvData=Math.max(16384,2**Math.ceil(Math.log2(baseline.bytes+8192)));
      const code=pluginCode(url,maxRecvData);
      fs.writeFileSync(path.join(output,'last-plugin.js'),code);
      const start=performance.now();
      const targetPromise=browser.waitForTarget(t=>t.url().includes('confirm'),{timeout:20000});
      const resultPromise=page.evaluate(async (code,mode) => {
        window.proofProgress=[];
        const listener=event=> {if(event.data?.type==='TLSN_PROVE_PROGRESS') window.proofProgress.push({at:performance.now(),...event.data});};
        window.addEventListener('message',listener);
        try {return await window.tlsn.execCode(code,{sessionData:{mode}});}
        finally {window.removeEventListener('message',listener);}
      },code,mode);
      // Attach rejection handling while the real approval popup is loading.
      resultPromise.catch(error=>console.error('EXEC_ERROR',error));
      const popup=await (await Promise.race([targetPromise,resultPromise.then(()=>{throw new Error('Proof finished without approval popup');})])).page();
      await popup.waitForSelector('button');
      const buttons=await popup.$$('button');
      let approved=false;
      for(const button of buttons) if((await button.evaluate(el=>el.textContent)).includes('allow all data sharing this session')) {await button.click();approved=true;break;}
      assert(approved,'Expected the public approval UI');
      const approvedAt=performance.now();
      let timer;
      try {
        const result=await Promise.race([resultPromise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Benchmark proof deadline 180s exceeded')),180000);})]);
        const progress=await page.evaluate(()=>window.proofProgress);
        const totalMs=performance.now()-start, afterApprovalMs=performance.now()-approvedAt;
        const parsed=typeof result==='string'?JSON.parse(result):result;
        assert.equal(parsed.ok,true,parsed.error);
        const transcript=parsed.result.results.find(r=>r.type==='RECV' && r.part==='ALL').value;
        assert.match(transcript,/^HTTP\/1\.1 200 /);
        const body=responseBody(transcript);
        const bodySha256=crypto.createHash('sha256').update(body).digest('hex');
        assert.equal(bodySha256,rows.find(r=>r.kind==='baseline' && r.url===url).sha256,'Proven response must match baseline bytes');
        save({kind:'proof',url,mode,trial,maxRecvData,totalMs,afterApprovalMs,bodySha256,result:parsed,progress});
      } catch(error) {save({kind:'proof_error',url,mode,trial,ms:performance.now()-start,error:String(error),progress:await page.evaluate(()=>window.proofProgress)}); throw error;}
      finally {clearTimeout(timer);}
    }
  } finally {
    if(browser) await browser.close();
    server.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
