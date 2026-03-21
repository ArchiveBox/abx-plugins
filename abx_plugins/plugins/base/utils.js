/**
 * Shared utilities for abx plugins (JavaScript).
 *
 * Provides common helpers used across multiple plugins:
 * - Environment variable parsing (getEnv, getEnvBool, getEnvInt, getEnvArray)
 * - CLI argument parsing (parseArgs)
 * - JSONL record emission (emitArchiveResultRecord, emitSnapshotRecord)
 * - Atomic file writing (writeFileAtomic)
 * - Sibling plugin output checking (hasStaticFileOutput)
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

function fsyncIfRegularFile(fd) {
    try {
        const stats = fs.fstatSync(fd);
        if (stats.isFile()) {
            fs.fsyncSync(fd);
        }
    } catch (error) {
        return;
    }
}

function writeFdFully(fd, text) {
    const buffer = Buffer.from(text, 'utf8');
    let offset = 0;
    while (offset < buffer.length) {
        offset += fs.writeSync(fd, buffer, offset, buffer.length - offset);
    }
    fsyncIfRegularFile(fd);
}

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
        } catch (e) {
            // Warn when a value looks like JSON but fails to parse, then
            // fall through to comma-separated parsing below.
            writeFdFully(2, `[base/utils.js] Warning: ${name} looks like JSON but failed to parse: ${e.message}\n`);
        }
    }

    return val.split(',').map(s => s.trim()).filter(Boolean);
}

function getLibDir() {
    const configured = getEnv('LIB_DIR');
    if (configured) return path.resolve(configured);
    return path.resolve(path.join(os.homedir(), '.config', 'abx', 'lib'));
}

function getNodeModulesDir() {
    const configured = getEnv('NODE_MODULES_DIR') || getEnv('NODE_MODULE_DIR');
    if (configured) return path.resolve(configured);
    return path.resolve(path.join(getLibDir(), 'npm', 'node_modules'));
}

function ensureNodeModuleResolution(moduleRef = module) {
    const nodeModulesDir = getNodeModulesDir();

    if (!process.env.NODE_MODULES_DIR && process.env.NODE_MODULE_DIR) {
        process.env.NODE_MODULES_DIR = process.env.NODE_MODULE_DIR;
    }
    if (!process.env.NODE_MODULE_DIR && process.env.NODE_MODULES_DIR) {
        process.env.NODE_MODULE_DIR = process.env.NODE_MODULES_DIR;
    }
    if (!process.env.NODE_PATH) {
        process.env.NODE_PATH = nodeModulesDir;
    }

    if (!moduleRef.paths.includes(nodeModulesDir)) {
        moduleRef.paths.unshift(nodeModulesDir);
    }

    return nodeModulesDir;
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

function parseExtraContext(raw, source) {
    try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            return parsed;
        }
        writeFdFully(2, `[base/utils.js] Warning: ignoring non-object extra context from ${source}\n`);
    } catch (error) {
        writeFdFully(2, `[base/utils.js] Warning: ignoring invalid extra context from ${source}: ${error.message}\n`);
    }
    return {};
}

function getExtraContext() {
    const context = {};
    const envRaw = getEnv('EXTRA_CONTEXT');
    if (envRaw) {
        Object.assign(context, parseExtraContext(envRaw, 'EXTRA_CONTEXT'));
    }

    const argv = process.argv.slice(2);
    for (let i = 0; i < argv.length; i += 1) {
        const arg = argv[i];
        if (arg === '--extra-context') {
            const value = argv[i + 1];
            if (value === undefined) {
                writeFdFully(2, '[base/utils.js] Warning: ignoring missing value for --extra-context\n');
                return context;
            }
            Object.assign(context, parseExtraContext(value, '--extra-context'));
            return context;
        }
        if (arg.startsWith('--extra-context=')) {
            Object.assign(context, parseExtraContext(arg.slice('--extra-context='.length), '--extra-context'));
            return context;
        }
    }

    return context;
}

function mergeExtraContext(record) {
    const extraContext = getExtraContext();
    if (!Object.keys(extraContext).length) {
        return record;
    }
    return { ...extraContext, ...record };
}

function emitArchiveResultRecord(status, outputStr, extra = {}) {
    writeFdFully(1, `${JSON.stringify(mergeExtraContext({
        type: 'ArchiveResult',
        status,
        output_str: outputStr,
        ...extra,
    }))}\n`);
}

function emitSnapshotRecord(record) {
    writeFdFully(1, `${JSON.stringify(mergeExtraContext({
        type: 'Snapshot',
        ...record,
    }))}\n`);
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
    getLibDir,
    getNodeModulesDir,
    ensureNodeModuleResolution,
    parseArgs,
    emitArchiveResultRecord,
    emitSnapshotRecord,
    writeFileAtomic,
    hasStaticFileOutput,
};
