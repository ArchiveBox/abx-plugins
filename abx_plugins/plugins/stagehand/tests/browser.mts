const {StagehandClient} = await import(process.env.ARCHIVEBOX_STAGEHAND_MODULE);
const {writeFile} = await import('node:fs/promises');
const client = new StagehandClient({cdp_url: process.env.BU_CDP_WS, keep_alive:true, logging:{sentry_enabled:false}});
await client.connect();
try {
  const pages = await client.browser.pages({targetId:process.env.ARCHIVEBOX_BROWSER_TARGET_ID});
  if (pages.length !== 1) throw new Error('Expected the existing capture tab');
  const state = await pages[0].evaluate({expression: "({heap:window.agentHeap, session:sessionStorage.getItem('agent'), local:localStorage.getItem('agent'), cookie:document.cookie, language:navigator.language, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})"});
  console.log('STATE=' + JSON.stringify(state));
  await pages[0].bringToFront();
  const shot = await pages[0].screenshot({options:{type:'png', fullPage:false}});
  if (!shot.screenshot) throw new Error('Screenshot missing');
  await writeFile(process.env.STAGEHAND_SCREENSHOT, Buffer.from(shot.screenshot.replace(/^data:image\/png;base64,/, ''), 'base64'));
} finally { await client.close(); }
