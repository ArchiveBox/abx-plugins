# Figma

Saves Figma Design's editable `.fig` copy through File → Save local copy, then
exports the current page's top-level frames to PDF when that command is available.

Applicability is checked on the main capture tab. Eligible UI exports open the
source URL in a background tab using the same Chrome persona; the main capture
tab stays untouched. Export tabs close on completion or shutdown signals.
Chrome also cleans abandoned export tabs, with a hard 60-minute lifetime cap.

Provider documentation: [Figma exports](https://help.figma.com/hc/en-us/articles/8403626871063-Save-a-local-copy-of-files).

Native `.fig` and eight-page PDF exports are verified with the linked starter design in an authorized authenticated headful Chromium session. The hook waits for the File submenu to accept pointer events, saves the native copy, then confirms Figma's PDF export settings dialog. The public viewer gates local copies behind sign-in, and the tested headless browser returned HTTP 403. Use an authenticated session with copying/export allowed.

The hook attaches to the snapshot's Chrome target, then checks both the requested
and current URLs before waiting for document navigation. Unrelated pages return
`noresults` even when their navigation failed. It uses existing browser authentication without a new browser or API keys. A visible sign-in requirement or explicitly disabled copy control
returns `skipped` with exit code 0. A recognized document's HTTP block, missing
expected controls, timeout or invalid export still returns `failed`.

`FIGMA_ENABLED` defaults to true; set it false to disable extraction.
`FIGMA_TIMEOUT` bounds the whole operation (120 seconds, with `TIMEOUT` fallback).
Outputs are ordinary files under
`figma/files/`; `downloads.json` records relative paths, formats, sizes, and
SHA-256 hashes. ArchiveBox offers format buttons and direct previews.

Run through the normal capture lifecycle:

```bash
uv run abx-dl dl --plugins=figma 'https://www.figma.com/design/0YpAEiii3cM0l3xidTbWPk/Style-Guide-Starter--Copy---Copy-?node-id=0-1&p=f'
```

Tests call the real Chrome crawl, tab, navigation and snapshot hooks against
public URLs. The explicit authenticated acceptance test requires an authorized
`AUTH_STORAGE_FILE`; it asserts real FIG archive contents, PDF page count and
manifest hashes. The anonymous restriction test uses an isolated unauthenticated
profile. See `tests/RESULTS.md` for retained evidence and commands.

The screenshot recipe uses the verified design and requires that authenticated
session. Native exports from FigJam, Slides, Buzz, Sites and Make remain
unverified. ArchiveBox gallery replay is checked separately.

Real public capture replayed through ArchiveBox:

![figma real capture replay](https://archivebox.io/screenshots/snapshot-view-figma-desktop.png)

[Tablet](https://archivebox.io/screenshots/snapshot-view-figma-tablet.png) · [Mobile](https://archivebox.io/screenshots/snapshot-view-figma-mobile.png).

ArchiveBox offers FIG and PDF format buttons. FIG uses its saved PDF companion for preview while Download retrieves the editable FIG original. Nested collections or duplicate formats use the file explorer.

[Live scheduled screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-figma).
