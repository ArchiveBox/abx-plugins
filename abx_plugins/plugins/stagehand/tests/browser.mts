const {Stagehand, localBrowser} = await import(process.env.ARCHIVEBOX_STAGEHAND_MODULE);
const {writeFile} = await import('node:fs/promises');
const browser = await localBrowser.connect({cdpUrl: process.env.BU_CDP_WS});
try {
  await Stagehand.create({browser});
  const pages = (await browser.context.pages()).filter(page => page.pageId === process.env.ARCHIVEBOX_BROWSER_TARGET_ID);
  if (pages.length !== 1) throw new Error('Expected the existing capture tab');
  const state = await pages[0].evaluate(() => ({heap:window.agentHeap, session:sessionStorage.getItem('agent'), local:localStorage.getItem('agent'), cookie:document.cookie, language:navigator.language, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone}));
  console.log('STATE=' + JSON.stringify(state));
  const shot = await pages[0].screenshot({type:'png', fullPage:false});
  if (!shot.length) throw new Error('Screenshot missing');
  await writeFile(process.env.STAGEHAND_SCREENSHOT, shot);
} finally { await browser.disconnect(); }
