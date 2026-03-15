/**
 * Shared utilities for abx plugins (JavaScript).
 *
 * Provides common helpers used across multiple plugins:
 * - Environment variable parsing (getEnv, getEnvBool, getEnvInt, getEnvArray)
 * - CLI argument parsing (parseArgs)
 * - JSONL record emission (emitArchiveResult)
 * - Atomic file writing (writeFileAtomic)
 * - Sibling plugin output checking (hasStaticFileOutput)
 */

const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Environment variable helpers
// ---------------------------------------------------------------------------

function getEnv(name, defaultValue = '') {
    return (process.env[name] || defaultValue).trim();
}

function getEnvBool(name, defaultValue = false) {
    const val = getEnv(name, '').toLowerCase();
    if (['true', '1', 'yes', 'on'].includes(val)) return true;
    if (['false', '0', 'no', 'off'].includes(val)) return false;
    return defaultValue;
}

function getEnvInt(name, defaultValue = 0) {
    const val = parseInt(getEnv(name, String(defaultValue)), 10);
    return isNaN(val) ? defaultValue : val;
}

/**
 * Get array environment variable (JSON array or comma-separated string).
 *
 * If value starts with '[', parse as JSON array.
 * Otherwise, parse as comma-separated values.
 */
function getEnvArray(name, defaultValue = []) {
    const val = getEnv(name, '');
    if (!val) return defaultValue;

    if (val.startsWith('[')) {
        try {
            const parsed = JSON.parse(val);
            if (Array.isArray(parsed)) return parsed;
        } catch (e) {}
    }

    return val.split(',').map(s => s.trim()).filter(Boolean);
}

// ---------------------------------------------------------------------------
// CLI argument parsing
// ---------------------------------------------------------------------------

/**
 * Parse --key=value arguments from process.argv.
 * Returns an object with keys (dashes converted to underscores).
 */
function parseArgs() {
    const args = {};
    process.argv.slice(2).forEach((arg) => {
        if (arg.startsWith('--')) {
            const [key, ...valueParts] = arg.slice(2).split('=');
            args[key.replace(/-/g, '_')] = valueParts.join('=') || true;
        }
    });
    return args;
}

// ---------------------------------------------------------------------------
// JSONL record emission
// ---------------------------------------------------------------------------

function emitArchiveResult(status, outputStr) {
    console.log(JSON.stringify({
        type: 'ArchiveResult',
        status,
        output_str: outputStr,
    }));
}

// ---------------------------------------------------------------------------
// Atomic file writing
// ---------------------------------------------------------------------------

function writeFileAtomic(filePath, contents) {
    const dir = path.dirname(filePath);
    const base = path.basename(filePath);
    const tmpPath = path.join(dir, `.${base}.${process.pid}.tmp`);
    fs.writeFileSync(tmpPath, contents, 'utf8');
    fs.renameSync(tmpPath, filePath);
}

// ---------------------------------------------------------------------------
// Sibling plugin output checking
// ---------------------------------------------------------------------------

function hasStaticFileOutput(staticfileDir = '../staticfile') {
    if (!fs.existsSync(staticfileDir)) return false;
    const stdoutPath = path.join(staticfileDir, 'stdout.log');
    if (!fs.existsSync(stdoutPath)) return false;
    const stdout = fs.readFileSync(stdoutPath, 'utf8');
    for (const line of stdout.split('\n')) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('{')) continue;
        try {
            const record = JSON.parse(trimmed);
            if (record.type === 'ArchiveResult' && record.status === 'succeeded') {
                return true;
            }
        } catch (e) {}
    }
    return false;
}

module.exports = {
    getEnv,
    getEnvBool,
    getEnvInt,
    getEnvArray,
    parseArgs,
    emitArchiveResult,
    writeFileAtomic,
    hasStaticFileOutput,
};
