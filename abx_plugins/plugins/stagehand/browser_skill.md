For official Stagehand v4, write an `.mts` script and run with the same prefix followed by
`-- tsx /absolute/path/task.mts`. The installed package is `@browserbasehq/stagehand@4.4.0`:

```ts
const { Stagehand, localBrowser } = await import(process.env.ARCHIVEBOX_STAGEHAND_MODULE!);
const browser = await localBrowser.connect({cdpUrl: process.env.ARCHIVEBOX_BROWSER_CDP_URL!});
try {
  const client = await Stagehand.create({browser});
  const pages = (await browser.context.pages()).filter(page =>
    !process.env.ARCHIVEBOX_BROWSER_TARGET_ID || page.pageId === process.env.ARCHIVEBOX_BROWSER_TARGET_ID);
  console.log(await Promise.all(pages.map(async page => ({targetId: page.pageId, url: await page.url()}))));
  if (pages.length !== 1) throw new Error('Select an explicit --target from --list');
  const page = pages[0];
  // Native APIs: page.evaluate('document.title'), page.locator('button').click(), page.keyPress('Enter').
  // Run your task on this selected page.
} finally { await browser.disconnect(); } // preserve the shared browser
```
