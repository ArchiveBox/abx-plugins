// Real browser UI test against a deployed verifier, with a genuine signed artifact.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const puppeteer = require(process.env.PUPPETEER_PATH || path.join(process.env.HOME,'.config/abx/lib/npm/node_modules/puppeteer'));
(async()=>{
 const [url,artifact] = process.argv.slice(2);
 assert(url && artifact,'Usage: node browser.cjs URL /path/to/capture.tlsn');
 const browser=await puppeteer.launch({headless:true,executablePath:process.env.CHROME_BINARY || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'tlsnotary-browser-'));
 try {
  const page=await browser.newPage();
  const requests=[],errors=[];
  page.on('request',r=>requests.push({method:r.method(),url:r.url(),body:r.postData()}));
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url,{waitUntil:'networkidle0'});
  await page.waitForFunction(()=>document.querySelector('#status').textContent.startsWith('Ready.'),{timeout:30000});
  const count=requests.length;
  await (await page.$('#file')).uploadFile(artifact);
  await page.waitForSelector('#status.valid',{timeout:30000});
  const facts=await page.$eval('#facts',e=>e.innerText);
  assert(facts.includes('https://'));
  assert(!await page.$eval('#result',e=>e.hidden));
  const bad=path.join(dir,'bad.tlsn');const bytes=fs.readFileSync(artifact);bytes[Math.floor(bytes.length/2)]^=1;fs.writeFileSync(bad,bytes);
  await (await page.$('#file')).uploadFile(bad);
  await page.waitForSelector('#status.invalid',{timeout:30000});
  assert(await page.$eval('#result',e=>e.hidden));
  assert.equal(errors.length,0,errors.join('\n'));
  for(const r of requests.slice(count)) {
   assert.equal(r.method,'GET');assert.equal(r.body,undefined);
   assert(/\/(worker\.js|pkg\/abx_tlsnotary(?:_bg\.wasm|\.js))$/.test(new URL(r.url).pathname),r.url);
  }
  await page.screenshot({path:path.join(dir,'verification.png'),fullPage:true});
  console.log(JSON.stringify({facts,uploadRequests:0,tamperRejected:true,screenshot:path.join(dir,'verification.png')},null,2));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
