Stagehand v4, inside the connected `try` block above after selecting `page`:

```ts
const { mkdir, writeFile } = await import('node:fs/promises');
const { resolve } = await import('node:path');
const screenshot = await page.screenshot({type: 'png', fullPage: false});
if (!screenshot.length) throw new Error('The selected tab returned no screenshot');
const directory = resolve('opencode/screenshots');
await mkdir(directory, {recursive: true});
const path = resolve(directory, 'stagehand-check.png');
await writeFile(path, screenshot);
console.log(path);
```
