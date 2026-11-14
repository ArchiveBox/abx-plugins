Stagehand v4, inside the connected `try` block above after selecting `page`:

```ts
const { mkdir, writeFile } = await import('node:fs/promises');
const { resolve } = await import('node:path');
await page.bringToFront();
const result = await page.screenshot({options: {type: 'png', fullPage: false}});
if (!result.screenshot) throw new Error('The selected tab returned no screenshot');
const directory = resolve('opencode/screenshots');
await mkdir(directory, {recursive: true});
const path = resolve(directory, 'stagehand-check.png');
await writeFile(path, Buffer.from(result.screenshot.replace(/^data:image\/png;base64,/, ''), 'base64'));
console.log(path);
```
