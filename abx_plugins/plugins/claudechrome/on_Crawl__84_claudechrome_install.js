#!/usr/bin/env node
/**
 * Claude for Chrome Extension - Install
 *
 * Installs the official Claude for Chrome extension from the Chrome Web Store.
 *
 * Extension: https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn
 *
 * Priority: 84 - Must install before Chrome session starts at 90
 * Hook: on_Crawl (runs once per crawl, not per snapshot)
 *
 * Requirements:
 * - CLAUDECHROME_ENABLED must be true
 * - ANTHROPIC_API_KEY environment variable should be set for the extension to authenticate
 */

const fs = require('fs');
const path = require('path');
const { getEnvBool } = require('../base/utils.js');

const PLUGIN_DIR = path.basename(__dirname);
const CRAWL_DIR = path.resolve((process.env.CRAWL_DIR || '.').trim());
const OUTPUT_DIR = path.join(CRAWL_DIR, PLUGIN_DIR);
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}
process.chdir(OUTPUT_DIR);

// Import extension utilities
const { installExtensionWithCache } = require('../chrome/chrome_utils.js');

// Check if enabled
if (!getEnvBool('CLAUDECHROME_ENABLED', false)) {
    console.log('SKIPPED: CLAUDECHROME_ENABLED=False');
    process.exit(0);
}

// Extension metadata - official Claude for Chrome
const EXTENSION = {
    webstore_id: 'fcoeoabgfenejglbffodgkkbkcdhcgfn',
    name: 'claudechrome',
};

async function main() {
    const extension = await installExtensionWithCache(EXTENSION);

    if (extension) {
        const apiKey = process.env.ANTHROPIC_API_KEY;
        if (!apiKey) {
            console.warn('[!] Claude for Chrome installed but ANTHROPIC_API_KEY not set');
            console.warn('[!] The extension may require manual login or API key configuration');
        } else {
            console.log('[+] Claude for Chrome extension installed');
        }
    }

    return extension;
}

// Export for use by config hook
module.exports = {
    EXTENSION,
};

if (require.main === module) {
    main().then(() => {
        process.exit(0);
    }).catch(err => {
        console.error(`[!] Claude for Chrome install failed: ${err.message}`);
        process.exit(1);
    });
}
