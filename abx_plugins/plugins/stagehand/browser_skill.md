For Stagehand v4, write an `.mts` script and run with the same prefix followed by
`-- tsx /absolute/path/task.mts`. The installed version is `shalpha` rc.56:

```ts
const { StagehandClient } = await import(process.env.ARCHIVEBOX_STAGEHAND_MODULE!);
const client = new StagehandClient({
  cdp_url: process.env.ARCHIVEBOX_BROWSER_CDP_URL!, keep_alive: true,
});
await client.connect();
try {
  const pages = await client.browser.pages(process.env.ARCHIVEBOX_BROWSER_TARGET_ID
    ? {targetId: process.env.ARCHIVEBOX_BROWSER_TARGET_ID} : {});
  console.log(pages.map(page => ({ targetId: page.targetId, url: page.url })));
  if (pages.length !== 1) throw new Error('Select an explicit --target from --list');
  const page = pages[0];
  // Run your task on this selected page.
} finally { await client.close(); } // disconnect; preserve the shared browser
```
