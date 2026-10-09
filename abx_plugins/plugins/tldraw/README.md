# tldraw

Download the full native `.tldr` file, preserving its schema, pages, shapes and asset records. Export the current page to SVG with the provider's Export menu. Both exports work for the published read-only example.

Applicability is checked on the main capture tab. Eligible UI exports open the
source URL in a background tab using the same Chrome persona; the main capture
tab stays untouched. Export tabs close on completion or shutdown signals.
Chrome also cleans abandoned export tabs, with a hard 60-minute lifetime cap.

Provider documentation: [tldraw exports](https://tldraw.dev/docs/persistence).

The hook attaches to the snapshot's existing Chrome target to check applicability.
Unrelated pages return `noresults` immediately after attachment. No new browser
or API keys are required.

`TLDRAW_ENABLED` defaults to true; set it false to disable extraction.
`TLDRAW_TIMEOUT` bounds the operation (120 seconds, with `TIMEOUT` fallback). Outputs are ordinary files under
`tldraw/files/`; `downloads.json` records relative paths, formats, sizes, and
SHA-256 hashes. ArchiveBox offers format buttons and direct previews.

Run through the normal capture lifecycle:

```bash
uv run abx-dl dl --dir="$(mktemp -d)" --plugins=tldraw 'https://www.tldraw.com/r/learn_with_jason'
```

Tests call the real Chrome crawl, tab, navigation and snapshot hooks against
public URLs. See `tests/RESULTS.md` for the actual evidence and limitations.

Real public capture replayed through ArchiveBox's normal file browser:

![tldraw real capture replay](https://archivebox.io/screenshots/snapshot-view-tldraw-desktop.png)

[Tablet](https://archivebox.io/screenshots/snapshot-view-tldraw-tablet.png) · [Mobile](https://archivebox.io/screenshots/snapshot-view-tldraw-mobile.png).

ArchiveBox offers TLDR and SVG format buttons. The native TLDR uses its saved SVG companion for preview while Download retrieves the editable TLDR original. Nested collections or duplicate formats use the file explorer.

[Live scheduled screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-tldraw).
