# Google Docs

Exports Google Docs, Sheets, Slides and Drawings through the snapshot's existing
Chrome tab. No Google API client, API key, OAuth consent flow, rclone, office
converter, or additional runtime dependency is needed.

The Chrome persona must already have access to private documents. Use the same
signed-in persona/session that opens the document in ArchiveBox. Public documents
work without signing in. This plugin does not import an unrelated desktop browser
profile or bypass the owner's download restrictions.

## Usage

Run `abx-dl dl --plugins=googledocs 'https://docs.google.com/document/d/FILE_ID/edit'`.
The Chrome dependency is included automatically. Existing ArchiveBox captures
discover this plugin through the normal plugin catalog.

With `parse_txt_urls` enabled, `archivebox add --depth=1 '<document URL>'`
discovers and archives links from the saved documents. The default DOCX/XLSX
exports preserve labeled hyperlinks as well as visible URLs. Discovery covers
all workbook sheets and deduplicates links across exports. See
[Parse Text URLs](../parse_txt_urls/README.md) for supported formats and the
generic output discovery contract used by other document/download plugins.

| Document | Default exports | Additional formats |
| --- | --- | --- |
| Docs | DOCX, PDF | ODT, RTF, TXT, Markdown, HTML ZIP, EPUB |
| Sheets | XLSX, CSV, PDF | ODS, TSV, HTML ZIP |
| Slides | PPTX, PDF | ODP, TXT |
| Drawings | SVG, PDF | PNG, JPG |

`GOOGLEDOCS_FORMATS='["docx","xlsx","pptx","csv","pdf","svg"]'` controls the requested formats.
Only formats supported by the current document type are requested; duplicates
are removed. Use `GOOGLEDOCS_FORMATS='["pdf"]'` for one export per document, or
`GOOGLEDOCS_FORMATS='["docx","odt","rtf","txt","md","zip","epub","pdf"]'` for all Docs formats.
`GOOGLEDOCS_ENABLED=false` disables the hook. `GOOGLEDOCS_TIMEOUT` bounds the
export operation (120 seconds by default, with the shared `TIMEOUT` fallback).

XLSX/ODS and PDF export the workbook. CSV/TSV save every sheet as a separate
`sheet-GID.csv` / `sheet-GID.tsv` file, with names and IDs in the manifest. The
viewer has format buttons in its header and a sheet selector for CSV/TSV; the
source URL's `gid` selects the initial sheet. Sheet names and IDs come from
metadata already embedded in the loaded editor, without another request. If
Google changes that metadata, CSV/TSV fail explicitly instead of silently saving
only one sheet; whole-workbook formats can still succeed. `zip` requests Google's HTML-with-assets export.
The plugin preserves multi-account `/u/N`, `authuser`, and link `resourcekey`
context. Drive links work when Chrome redirects them to a supported editor URL.
Published `/d/e/...` URLs, Forms, Apps Script, Vids, folders and arbitrary Drive
files are outside this plugin's export surface.

## Session and request reuse

The hook waits for the Chrome plugin's completed navigation and attaches to its
persisted `target_id.txt`. It does not launch another browser, create a tab,
navigate the shared tab, enumerate Drive, or request document metadata from an
API. Chrome's `Network.loadNetworkResource` streams each export using the page's
session credentials and HTTP cache, including cross-origin download redirects.
Cookies stay inside Chrome.

If the optional `responses` plugin already saved the exact export URL in this
snapshot, its successful GET body is validated and reused. The response recorder
preserves the original request URL through Google's download redirects.
Missing, malformed,
out-of-directory, corrupt or unrelated captures are ignored. Reuse is exact
apart from query ordering and fragments: a different sheet, account, revision,
or PDF layout must never be substituted. Each remaining format needs its own
export request; the editor HTML is not an office document and cannot replace one.

Files are streamed to temporary paths, checked for their expected content type
and signature, and atomically replaced. Login/error HTML is rejected. Successful
exports remain available if another format fails; the hook reports `failed` for
any requested export failure, with details in `exports.json`. Failed attempts do
not remove previous successful files. Re-running replaces outputs rather than
treating existing `googledocs` files as a cache. The manifest lists only the
current attempt's successful exports, their sizes, hashes, and reuse status.

## Verification

Run `uv run pytest -xq abx_plugins/plugins/googledocs/tests` from this repository.
Tests exercise the real Chrome lifecycle, cookie-bearing requests, preserved
tabs, hook statuses, and live exports of Google's public API quickstart samples.
The live tests require access to Google and do not skip network/export failures.

Google's browser export URLs are a web UI interface and may change. See Google's
[supported export formats](https://developers.google.com/workspace/drive/api/guides/ref-export-formats).
The [rclone Drive backend](https://rclone.org/drive/) uses OAuth authorization;
adding it would not satisfy reuse of an existing browser session.
