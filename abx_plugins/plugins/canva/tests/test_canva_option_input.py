"""Trusted option input stays on target across real compact popup geometry."""

import subprocess
from pathlib import Path

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

HELPER = Path(__file__).resolve().parents[1] / "click_option.js"
HTML = """<!doctype html><style>
body{margin:0}.menu{position:absolute;left:392px;top:200px;width:384px}
.menu:hover{transform:translateY(-60px)}
button{display:block;height:60px;width:384px}
#cover{position:fixed;inset:0;z-index:20;display:none}
.wrap{position:absolute;left:300px;top:200px;width:120px;font:20px/40px sans-serif}
#overlay{position:absolute;left:300px;top:200px;width:120px;height:40px;z-index:5}
</style><div class=menu><button role=option>GIF</button>
<button id=target role=option>PPTX</button><button role=option>PDF</button></div>
<div id=cover></div><script>
const mode=new URL(location.href).searchParams.get('mode');
if(mode==='animated')document.querySelector('.menu').style.transition='transform .15s linear';
if(mode==='blocked')document.querySelector('.menu').addEventListener('pointerenter',()=>{
 document.querySelector('#cover').style.display='block';
},{once:true});
if(mode==='point-obstructed')document.body.innerHTML='<div class=wrap><span id=target role=option>PPTX Microsoft PowerPoint</span></div><div id=overlay></div>';
window.events=[];window.moves=0;
document.addEventListener('pointermove',()=>window.moves++);
for(const type of ['pointerdown','click'])document.addEventListener(type,e=>{
 window.events.push({type,text:e.target.textContent,trusted:e.isTrusted});
});
</script>"""

SCRIPT = """const assert=require('node:assert/strict');
const fs=require('node:fs');
const puppeteer=require('puppeteer');
const {clickReadyOption}=require(process.argv[2]);
(async()=>{
 const browser=await puppeteer.connect({browserWSEndpoint:fs.readFileSync(process.argv[3],'utf8').trim(),defaultViewport:null});
 const page=await browser.newPage();
 try{
  await page.setViewport({width:800,height:600});
  for(const mode of ['hover','animated','blocked','point-obstructed']){
   await page.mouse.move(0,0);
   await page.goto(process.argv[4]+'?mode='+mode);
   const element=await page.$('#target');
   try{
    if(mode==='point-obstructed'){
     const point=await element.clickablePoint();
     assert.equal(await element.evaluate((el,p)=>el.contains(document.elementFromPoint(p.x,p.y)),point),false);
     assert.equal(await element.evaluate(el=>{const b=el.getBoundingClientRect();return el.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2))}),true);
    }
    const blocked=['blocked','point-obstructed'].includes(mode);
    const deadline=Date.now()+(blocked?1000:5000);
    const remaining=()=>{const left=deadline-Date.now();if(left<=0)throw Error('fixture deadline exceeded');return left;};
    if(blocked)await assert.rejects(()=>clickReadyOption(page,element,remaining),/Waiting failed|deadline exceeded/);
    else await clickReadyOption(page,element,remaining);
    const state=await page.evaluate(()=>({events:window.events,moves:window.moves}));
    if(blocked){assert.deepEqual(state.events,[]);assert(state.moves<=12,JSON.stringify(state));}
    else assert.deepEqual(state.events,[{type:'pointerdown',text:'PPTX',trusted:true},{type:'click',text:'PPTX',trusted:true}]);
    console.log(JSON.stringify({mode,...state}));
   }finally{await element.dispose();}
  }
 }finally{await page.close();browser.disconnect();}
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


def test_compact_option_input(tmp_path, httpserver, ensure_chrome_test_prereqs):
    httpserver.expect_request("/").respond_with_data(HTML, content_type="text/html")
    url = httpserver.url_for("/")
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=60,
        env_overrides={"AUTH_STORAGE_FILE": "", "CHROME_HEADLESS": "true"},
    ) as (_, _, chrome, env):
        script = tmp_path / "option-input.js"
        script.write_text(SCRIPT)
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                str(script),
                str(HELPER),
                str(chrome / "cdp_url.txt"),
                url,
            ],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
