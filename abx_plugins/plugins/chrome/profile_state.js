// Modify saved profile state through Chrome, never through its live databases.
const fs = require('fs');
const path = require('path');
const {openBrowser} = require('./export_browser_state.js');
const {withTimeout, importCookiesFromFile} = require('./chrome_utils.js');
const {readStorageState, restoreOriginStorage} = require('./persona_storage.js');

function normalizeDomain(value) {
    if (typeof value !== 'string' || !value || /[\s/\\?#@*]/.test(value)) {
        throw new Error('domain must be an exact hostname');
    }
    const domain = value.replace(/^\./, '').replace(/\.$/, '').toLowerCase();
    const parsed = new URL(`https://${domain.includes(':') && !domain.startsWith('[') ? `[${domain}]` : domain}`);
    if (parsed.port || !domain) throw new Error('domain must be an exact hostname');
    return parsed.hostname.replace(/^\[|\]$/g, '');
}

async function profileSites(browser) {
    const page = await browser.newPage();
    try {
        await page.goto('chrome://settings/content/all');
        // Chrome's native inventory includes closed-tab localStorage, IndexedDB
        // and partitioned storage; CDP itself cannot enumerate saved origins.
        // https://chromium.googlesource.com/chromium/src/+/refs/heads/main/chrome/browser/ui/webui/settings/site_settings_handler.cc
        return await withTimeout(() => page.evaluate(async () => {
            const {addWebUiListener, sendWithPromise} = await import('chrome://resources/js/cr.js');
            return new Promise((resolve, reject) => {
                addWebUiListener('onStorageListFetched', groups => resolve(groups.flatMap(group => group.origins.map(entry => ({origin: entry.origin, usage: entry.usage || 0})))));
                sendWithPromise('getAllSites').catch(reject);
            });
        }), 30000, 'Timed out reading Chrome site storage');
    } finally {
        await page.close();
    }
}

async function removeDomain(browser, request) {
    const domain = normalizeDomain(request.domain);
    const suppliedOrigins = request.origins || [];
    if (!Array.isArray(suppliedOrigins) || suppliedOrigins.some(value => typeof value !== 'string')) {
        throw new Error('origins must be an array of URLs');
    }
    const matches = value => {
        const url = new URL(value);
        return ['http:', 'https:'].includes(url.protocol) && normalizeDomain(url.hostname) === domain;
    };
    // Validate supplied origins before opening or modifying the profile.
    const origins = new Set(suppliedOrigins.filter(matches).map(value => new URL(value).origin));
    for (const {origin} of await profileSites(browser)) {
        if (matches(origin)) origins.add(new URL(origin).origin);
    }
    const pages = await browser.pages();
    for (const page of pages) {
        for (const frame of page.frames()) {
            if (!/^https?:\/\//.test(frame.url()) || !matches(frame.url())) continue;
            origins.add(new URL(frame.url()).origin);
            await frame.evaluate(() => sessionStorage.clear());
        }
    }
    const controlPage = await browser.newPage();
    const session = await controlPage.createCDPSession();
    try {
        const {cookies} = await session.send('Storage.getCookies');
        const removed = cookies.filter(cookie => normalizeDomain(cookie.domain) === domain);
        for (const cookie of removed) {
            await session.send('Network.deleteCookies', {
                name: cookie.name, domain: cookie.domain, path: cookie.path,
                ...(cookie.partitionKey ? {partitionKey: cookie.partitionKey} : {}),
            });
        }
        for (const origin of origins) {
            // Cookies are deleted individually above: origin-wide cookie
            // deletion can also remove a parent domain's shared cookies.
            await session.send('Storage.clearDataForOrigin', {
                origin,
                storageTypes: 'file_systems,indexeddb,local_storage,shader_cache,websql,service_workers,cache_storage,interest_groups,shared_storage,storage_buckets',
            });
        }
        const remaining = (await session.send('Storage.getCookies')).cookies;
        if (remaining.some(cookie => normalizeDomain(cookie.domain) === domain)) {
            throw new Error('Chrome retained cookies for the selected domain');
        }
        return {domain, cookies_removed: removed.length, origins_removed: [...origins].sort()};
    } finally {
        await session.detach();
        await controlPage.close();
    }
}

async function inspectDomains(browser) {
    const domains = new Map();
    const entryFor = domain => {
        if (!domains.has(domain)) domains.set(domain, {domain, cookies: 0, origins: [], storage_bytes: 0, session_entries: 0});
        return domains.get(domain);
    };
    for (const cookie of await browser.cookies()) entryFor(normalizeDomain(cookie.domain)).cookies++;
    for (const site of await profileSites(browser)) {
        const url = new URL(site.origin);
        if (!['http:', 'https:'].includes(url.protocol) || !site.usage) continue;
        const entry = entryFor(normalizeDomain(url.hostname));
        if (!entry.origins.includes(url.origin)) entry.origins.push(url.origin);
        entry.storage_bytes += site.usage;
    }
    for (const page of await browser.pages()) {
        const sessionOrigins = new Set();
        for (const frame of page.frames()) {
            if (!/^https?:\/\//.test(frame.url())) continue;
            const url = new URL(frame.url());
            if (sessionOrigins.has(url.origin)) continue;
            const count = await frame.evaluate(() => {
                try { return sessionStorage.length; }
                catch (error) { if (error.name === 'SecurityError') return 0; throw error; }
            });
            if (!count) continue;
            sessionOrigins.add(url.origin);
            const entry = entryFor(normalizeDomain(url.hostname));
            if (!entry.origins.includes(url.origin)) entry.origins.push(url.origin);
            entry.session_entries += count;
        }
    }
    return {domains: [...domains.values()].sort((a, b) => a.domain.localeCompare(b.domain))};
}

async function importState(browser, request) {
    let storage;
    try { storage = readStorageState(request.auth_file); }
    catch { throw new Error('Invalid auth storage export'); }
    const {origins, tabs} = storage;
    const cookiesImported = await importCookiesFromFile(browser, [request.cookies_file, request.auth_file], process.env.CHROME_USER_DATA_DIR, {reconcileExtension: false});
    const failures = await restoreOriginStorage(browser, origins, {merge: true});
    if (failures.length) throw new Error('Saved storage could not be imported into the browser profile');
    const states = [...origins.filter(entry => entry.sessionStorage), ...tabs];
    let sessionTabs = 0;
    for (const page of await browser.pages()) {
        for (const frame of page.frames()) {
            if (!/^https?:\/\//.test(frame.url())) continue;
            const origin = new URL(frame.url()).origin;
            const candidates = states.filter(state => (state.origin || new URL(state.url).origin) === origin);
            const unambiguous = new Set(candidates.map(state => JSON.stringify([...state.sessionStorage].sort((a, b) => a.name.localeCompare(b.name))))).size === 1;
            const state = candidates.find(state => state.url === frame.url()) || candidates.find(state => !state.url) || (unambiguous ? candidates[0] : null);
            if (!state) continue;
            await frame.evaluate(entries => { for (const {name, value} of entries) sessionStorage.setItem(name, value); }, state.sessionStorage);
            sessionTabs++;
        }
    }
    return {cookies_imported: cookiesImported, origins_imported: origins.length, session_tabs_imported: sessionTabs};
}

async function main() {
    const request = JSON.parse(fs.readFileSync(0, 'utf8'));
    if (request.action === 'remove-domain') normalizeDomain(request.domain);
    if (request.action === 'import-state' && !request.cookies_file && !request.auth_file) throw new Error('cookies_file or auth_file is required');
    for (const file of [request.cookies_file, request.auth_file].filter(Boolean)) {
        if (!path.isAbsolute(file) || !fs.statSync(file).isFile()) throw new Error('Import files must be existing absolute file paths');
    }
    const {browser, cleanup} = await openBrowser({reuseProfileSession: true});
    try {
        const result = request.action === 'remove-domain' ? await removeDomain(browser, request)
            : request.action === 'import-state' ? await importState(browser, request)
            : await inspectDomains(browser);
        if (request.action !== 'inspect-domains') result.domains = (await inspectDomains(browser)).domains;
        console.log(JSON.stringify(result));
    } finally { await cleanup(); }
}

main().catch(error => {
    console.error(`ERROR: ${error.message}`);
    process.exit(1);
});
