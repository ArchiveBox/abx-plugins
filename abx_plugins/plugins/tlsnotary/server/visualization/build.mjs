// Refresh cache keys and synchronize the archived full-output template. No dependencies.
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
const web = path.resolve(import.meta.dirname, '../web');
const hash = name => createHash('sha256').update(readFileSync(path.join(web, name))).digest('hex').slice(0, 12);
const index = path.join(web, 'index.html');
const html = readFileSync(index, 'utf8')
  .replace(/style\.css\?v=[a-f0-9]+/, `style.css?v=${hash('style.css')}`)
  .replace(/diagram\.mjs\?v=[a-f0-9]+/, `diagram.mjs?v=${hash('diagram.mjs')}`);
writeFileSync(index, html);
const template = path.resolve(import.meta.dirname, '../../templates/full.html');
if (existsSync(template)) writeFileSync(template, html.replace(
  'Keep the verifier markup in sync with templates/full.html.',
  'Keep the verifier markup in sync with server/web/index.html.',
));
