import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const root = process.argv[2];
let input = '';
process.stdin.setEncoding('utf8');
for await (const chunk of process.stdin) input += chunk;
const { operation, texts, records, database, ids, limit } = JSON.parse(input);
const load = (path) => import(pathToFileURL(join(root, 'node_modules', path)).href);
const { env, pipeline } = await load('@huggingface/transformers/dist/transformers.node.mjs');

// The install step may download model files, but search must never contact a model host.
env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = root + '/';
env.useFSCache = false;

const model = await pipeline('feature-extraction', 'model', {
    device: 'cpu',
    dtype: 'q8',
    session_options: { intraOpNumThreads: 1, interOpNumThreads: 1 },
});
try {
    const embed = async (text) => (await model(text, { pooling: 'mean', normalize: true })).data;
    const encode = (v) => {
        const data = Buffer.alloc(384 * 4);
        for (let i = 0; i < 384; i++) data.writeFloatLE(v[i], i * 4);
        return data.toString('base64');
    };
    const decode = (s) => {
        const data = Buffer.from(s, 'base64');
        return Float32Array.from({ length: 384 }, (_, i) => data.readFloatLE(i * 4));
    };
    if (operation === 'embed') {
        process.stdout.write(JSON.stringify(encode(await embed(texts[0]))));
    } else if (operation === 'rank') {
        const { BrowserVec } = await load('browservec/dist/browservec.js');
        const db = await BrowserVec.create({ dimension: 384, metric: 'cosine', fallback: 'wasm' });
        try {
            await db.addBatch(records.map(([id, vector]) => ({ id, vector: decode(vector) })));
            const output = [];
            for (const text of texts) {
                output.push(await db.query(await embed(text), { k: records.length }));
            }
            process.stdout.write(JSON.stringify(output));
        } finally {
            db.destroy();
        }
    } else if (operation === 'scan') {
        const { BrowserVec } = await load('browservec/dist/browservec.js');
        const { DatabaseSync } = await import('node:sqlite');
        const key = (id) => /^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$/i.test(id) || /^[a-f\d]{32}$/i.test(id)
            ? id.toLowerCase().replaceAll('-', '')
            : id;
        const scope = ids === null ? null : new Map(ids.map((id) => [key(id), id]));
        const aliases = new Set();
        for (const canonical of scope?.keys() ?? []) {
            aliases.add(canonical);
            if (/^[a-f\d]{32}$/.test(canonical)) {
                aliases.add(canonical.replace(/^(........)(....)(....)(....)(............)$/, '$1-$2-$3-$4-$5'));
            }
        }
        const query = await embed(texts[0]);
        const connection = new DatabaseSync(database, { readOnly: true });
        let batch = [];
        let best = [];
        const compare = (a, b) => (b.score - a.score) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
        const flush = async () => {
            if (!batch.length) return;
            const db = await BrowserVec.create({ dimension: 384, metric: 'cosine', fallback: 'wasm' });
            try {
                await db.addBatch(batch);
                // Ask for the whole chunk so score ties remain stable across chunk boundaries.
                const hits = await db.query(query, { k: batch.length });
                best = [...best, ...hits.filter(({ score }) => Number.isFinite(score))].sort(compare).slice(0, limit);
            } finally {
                db.destroy();
                batch = [];
            }
        };
        function* rows() {
            if (scope === null) {
                yield* connection.prepare('SELECT id, vector FROM embeddings ORDER BY id').iterate();
                return;
            }
            const keys = [...aliases];
            for (let start = 0; start < keys.length; start += 512) {
                const part = keys.slice(start, start + 512);
                const placeholders = part.map(() => '?').join(',');
                yield* connection.prepare(`SELECT id, vector FROM embeddings WHERE id IN (${placeholders})`).iterate(...part);
            }
        }
        try {
            for (const { id, vector } of rows()) {
                const selectedId = scope === null ? id : scope.get(key(id));
                if (!selectedId || vector.byteLength !== 384 * 4) continue;
                const bytes = Buffer.from(vector.buffer, vector.byteOffset, vector.byteLength);
                batch.push({
                    id: selectedId,
                    vector: Float32Array.from({ length: 384 }, (_, i) => bytes.readFloatLE(i * 4)),
                });
                if (batch.length >= 512) await flush();
            }
            await flush();
            process.stdout.write(JSON.stringify(best));
        } finally {
            connection.close();
        }
    } else {
        throw new Error('Unknown operation');
    }
} finally {
    await model.dispose();
}
