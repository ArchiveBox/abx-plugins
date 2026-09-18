import { build } from 'esbuild';
import { readFileSync, writeFileSync, copyFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
const root = import.meta.dirname;
const web = path.resolve(root, '../web');
await build({
  entryPoints: [path.join(root, 'scene.mjs')],
  bundle: true, minify: true, format: 'esm', target: 'es2022',
  legalComments: 'inline', outfile: path.join(web, 'scene.mjs'),
});
copyFileSync(path.join(root, 'node_modules/three/LICENSE'), path.join(web, 'THREE-LICENSE.txt'));
const hash = name => createHash('sha256').update(readFileSync(path.join(web, name))).digest('hex').slice(0, 12);
const diagram = path.join(web, 'diagram.mjs');
writeFileSync(diagram, readFileSync(diagram, 'utf8').replace(/scene\.mjs\?v=[a-f0-9]+/, `scene.mjs?v=${hash('scene.mjs')}`));
const index = path.join(web, 'index.html');
const html = readFileSync(index, 'utf8')
  .replace(/style\.css\?v=[a-f0-9]+/, `style.css?v=${hash('style.css')}`)
  .replace(/diagram\.mjs\?v=[a-f0-9]+/, `diagram.mjs?v=${hash('diagram.mjs')}`);
writeFileSync(index, html);
// A standalone server checkout need not contain the plugin's full-output template.
const template = path.resolve(root, '../../templates/full.html');
if (existsSync(template)) writeFileSync(template, html.replace(
  'Keep the verifier markup in sync with templates/full.html.',
  'Keep the verifier markup in sync with server/web/index.html.',
));
