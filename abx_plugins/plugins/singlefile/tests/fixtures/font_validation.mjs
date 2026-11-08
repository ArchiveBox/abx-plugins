// Real browser regression inputs. Algorithms and CSS-tree are extracted from
// the hash-verified installed extension by test_font_validation.py.
import css from './vendor.mjs';
import Metadata, {processFontDetails} from './metadata.mjs';
import Original from './original.mjs';
import Timer from './timer.mjs';
import Eager from './eager.mjs';
import Lazy from './lazy.mjs';

function check(condition, message) {
    if (!condition) throw new Error(message);
}

function comparable(result) {
    return JSON.stringify([result.statuses, result.css, result.stats]);
}

async function evaluate(Helper, text, fallback = () => 'bad', delayed = false) {
    const sheet = css.parse(text);
    const metadata = new Metadata();
    const details = metadata.createFontsDetailsInfo();
    metadata.getFontsDetails(null, sheet.children, 0, details);
    processFontDetails(details);
    const cache = new Map();
    const stats = {fonts: {processed: 0, discarded: 0}};
    const statuses = [];
    const records = [];
    let ruleIndex = 0;
    const helper = new Helper();
    async function visit(scope) {
        for (const [rule, info] of scope.fonts) {
            const mode = fallback(ruleIndex++);
            const nodes = css.findAll(rule, node => node.type === 'Url');
            const declarations = new Map();
            for (const node of nodes) {
                if (mode === 'missing' || (delayed && !node.value.startsWith('data:'))) continue;
                const values = mode === 'empty' ? [] : mode === 'falsey' ? [''] : [
                    location.origin + (delayed ? '/fallback-invalid' : `/fallback-${mode}`)
                    + (delayed ? `?run=${crypto.randomUUID()}` : '')
                ];
                declarations.set(node, values);
            }
            records.push(...info);
            statuses.push(await helper.processFontFaceRule(rule, info, declarations, cache, stats));
        }
        for (const map of [scope.medias, scope.supports, scope.layers]) { // codespell:ignore medias
            for (const child of map.values()) await visit(child);
        }
    }
    await visit(details);
    return {statuses, css: css.generate(sheet), stats, cache, records};
}

export async function run() {
    const [url, urlTwo] = await (await fetch('./fonts.json')).json();
    const good = (format, second = false) => `url(${second ? urlTwo : url}) format("${format}")`;
    const bad = format => `url("data:font/woff2;base64,AAAA") format("${format}")`;
    const face = src => `@font-face{font-family:test;src:${src}}`;
    const formats = ['woff2-variations', 'woff2', 'woff', 'truetype-variations', 'truetype', 'opentype', 'embedded-opentype'];
    const matrix = [];
    for (const first of formats) for (const second of formats) {
        matrix.push({src: `${good(first)},${good(second, true)}`, fallback: 'bad'});
    }
    for (const format of formats) {
        matrix.push({src: `${good(format)},${good(format, true)}`, fallback: 'bad'});
        matrix.push({src: `${good(format, true)},${good(format)}`, fallback: 'bad'});
    }
    for (const fallback of ['missing', 'empty', 'falsey', 'good', 'bad']) {
        for (const tail of ['', ',' + good('woff')]) {
            matrix.push({src: `${bad('woff2')} format("woff2")${tail}`, fallback});
        }
    }
    for (const src of [
        `${good('woff2')} tech(variations),${good('woff')}`,
        `${bad('woff2')} format("woff2") tech(variations),${good('woff')}`,
        `local("Arial"),local("Helvetica"),${good('woff2')}`,
        `${good('unknown')},${good('other', true)}`,
        `url(${url}),url(${urlTwo})`,
        `url("data:font/woff2;base64,") tech(variations),${good('woff')}`,
        'url("data:font/woff2;base64,") format("woff2")',
        'local("Arial")', 'local("Name format(woff2)")'
    ]) matrix.push({src, fallback: 'bad'});
    check(matrix.length === 82, 'Matrix coverage changed');
    for (const [index, spec] of matrix.entries()) {
        const eager = await evaluate(Eager, face(spec.src), () => spec.fallback);
        const lazy = await evaluate(Lazy, face(spec.src), () => spec.fallback);
        check(comparable(eager) === comparable(lazy), `Matrix ${index} differs`);
    }

    const expanded = [
        good('woff2'), `${good('woff')},${good('woff2')}`, `${good('woff2')},${good('woff')}`,
        `${bad('woff2')},${good('woff')}`, `${good('truetype')},${good('opentype')}`,
        `local("Arial"),${good('woff2')}`, `${good('woff2')} tech(variations),${good('woff')}`,
        `${bad('woff2')} tech(variations),${good('woff')}`, `${good('woff2')},${good('woff2')}`,
        bad('woff2'), good('opentype'), `${good('woff2')},${bad('opentype')}`,
        `${good('woff2')},${good('woff2')} tech(variations)`,
        `${bad('woff2')} format("woff2"),${good('woff')}`,
        `${good('woff2')},${bad('woff')} format("woff")`, `${good('unknown')},${good('other')}`,
        `local("Arial"),local("Helvetica"),${good('woff2')}`,
        `${good('woff2-variations')},${good('woff2')}`, `${good('truetype-variations')},${good('truetype')}`,
        `url("data:font/woff2;base64,") format("woff2"),${good('woff')}`
    ].map(face);
    expanded.push(`@font-face{font-family:test;src:${good('woff')};src:${good('woff2')}}`);
    expanded.push(`@media screen{${face(good('woff2') + ',' + good('woff'))}}@supports(display:grid){${face(good('woff'))}}@layer test{${face(good('woff2'))}}`);
    expanded.push(`@font-face{font-family:a;src:${good('woff2')},${good('woff')}}@font-face{font-family:b;src:${good('woff')}}`);
    check(expanded.length === 23, 'Expanded coverage changed');
    for (const [index, text] of expanded.entries()) {
        const mode = () => index % 2 ? 'good' : 'bad';
        const eager = await evaluate(Eager, text, mode);
        const lazy = await evaluate(Lazy, text, mode);
        check(comparable(eager) === comparable(lazy), `Expanded ${index} differs`);
    }

    // Separately demonstrate the contextual-cache defect: a discarded source
    // with working fallback A must not grant validity to another rule's bad B.
    const repeated = `${bad('woff')} format("woff")`;
    const contextual = `@font-face{font-family:a;src:${good('woff2')},${repeated}}@font-face{font-family:b;src:${repeated}}`;
    const fallbacks = index => index === 0 ? 'good' : 'bad';
    const contaminated = await evaluate(Timer, contextual, fallbacks);
    const corrected = await evaluate(Eager, contextual, fallbacks);
    const selected = await evaluate(Lazy, contextual, fallbacks);
    check(JSON.stringify(contaminated.statuses) === '[true,true]', 'Cache red control did not reproduce');
    check(JSON.stringify(corrected.statuses) === '[true,false]', 'Contextual cache repair failed');
    check(comparable(corrected) === comparable(selected), 'Lazy contextual selection differs');

    // The upstream rejection timer incorrectly reaccepts malformed WOFF2 while
    // another real request remains pending. Timer-only repair must select WOFF.
    const slow = () => face(`url("/slow-font?run=${crypto.randomUUID()}") format("woff"),${bad('woff2')} format("woff2")`);
    const original = await evaluate(Original, slow(), () => 'bad', true);
    const repaired = await evaluate(Timer, slow(), () => 'bad', true);
    check(original.css.includes('AAAA'), 'Timer red control did not reproduce');
    check(!repaired.css.includes('AAAA') && repaired.css.includes('slow-font'), 'Timer cleanup failed');
    const rejected = repaired.records.find(source => source.src.includes('AAAA'));
    check(!rejected.valid, 'Rejected font mutated back to valid');

    // Characterize (do not tighten) the existing genuine five-second acceptance
    // policy. A later successful native load cannot rewrite the cached outcome.
    const timeoutResults = [];
    for (const Helper of [Eager, Lazy]) {
        const result = await evaluate(Helper, slow(), () => 'bad', true);
        check(!result.css.includes('AAAA'), 'Malformed source selected at timeout');
        const before = [...result.cache.values()].sort();
        check(JSON.stringify(before) === '["other-rejection","timeout-policy"]', 'Expected genuine timeout policy');
        await new Promise(resolve => setTimeout(resolve, 400));
        check(JSON.stringify([...result.cache.values()].sort()) === JSON.stringify(before), 'Late native load rewrote cached policy');
        result.css = result.css.replace(/slow-font\?run=[^"')]+/g, 'slow-font');
        timeoutResults.push(comparable(result));
    }
    check(timeoutResults[0] === timeoutResults[1], 'Timeout policy selection differs');
    return {matrix: 82, expanded: 23, timerRepair: true, contextRepair: true, timeoutPolicy: true};
}
