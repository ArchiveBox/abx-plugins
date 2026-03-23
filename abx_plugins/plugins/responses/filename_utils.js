function getExtensionFromMimeType(mimeType) {
    const mimeMap = {
        'text/html': 'html',
        'text/css': 'css',
        'text/javascript': 'js',
        'application/javascript': 'js',
        'application/x-javascript': 'js',
        'application/json': 'json',
        'application/xml': 'xml',
        'text/xml': 'xml',
        'image/png': 'png',
        'image/jpeg': 'jpg',
        'image/gif': 'gif',
        'image/svg+xml': 'svg',
        'image/webp': 'webp',
        'font/woff': 'woff',
        'font/woff2': 'woff2',
        'font/ttf': 'ttf',
        'font/otf': 'otf',
        'application/font-woff': 'woff',
        'application/font-woff2': 'woff2',
        'video/mp4': 'mp4',
        'video/webm': 'webm',
        'audio/mpeg': 'mp3',
        'audio/ogg': 'ogg',
    };

    const mimeBase = (mimeType || '').split(';')[0].trim().toLowerCase();
    return mimeMap[mimeBase] || '';
}

function getExtensionFromUrl(url) {
    try {
        const pathname = new URL(url).pathname;
        const match = pathname.match(/\.([a-z0-9]+)$/i);
        return match ? match[1].toLowerCase() : '';
    } catch (e) {
        return '';
    }
}

function sanitizeFilename(str, maxLen = 200) {
    return str
        .replace(/[^a-zA-Z0-9._-]/g, '_')
        .slice(0, maxLen);
}

function appendExtensionIfMissing(filename, extension) {
    const normalizedExtension = (extension || '').replace(/^\./, '').toLowerCase();
    if (!normalizedExtension) {
        return filename;
    }

    const expectedSuffix = `.${normalizedExtension}`;
    if (filename.toLowerCase().endsWith(expectedSuffix)) {
        return filename;
    }

    return `${filename}${expectedSuffix}`;
}

function buildUniqueFilename({ timestamp, method, url, extension }) {
    const urlHash = sanitizeFilename(encodeURIComponent(url).slice(0, 64));
    return `${timestamp}__${method}__${appendExtensionIfMissing(urlHash, extension)}`;
}

module.exports = {
    appendExtensionIfMissing,
    buildUniqueFilename,
    getExtensionFromMimeType,
    getExtensionFromUrl,
    sanitizeFilename,
};
