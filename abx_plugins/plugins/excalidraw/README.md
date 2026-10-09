# Excalidraw

Export the loaded scene as editable `.excalidraw` JSON and an SVG with its scene embedded. The public encrypted share is loaded and decrypted by the provider before export; the hook uses the real Save to file and Export to SVG controls.

Provider documentation: [Excalidraw exports](https://docs.excalidraw.com/docs/codebase/json-schema/).

ArchiveBox-managed Chrome disables `FileSystemAccessLocal` by default so
Excalidraw uses its official browser-download fallback. Chrome's native save
picker cannot complete an unattended headless export or emit a browser download
event. No additional configuration is needed for managed Chrome. An externally
attached browser must also launch with
`--disable-blink-features=FileSystemAccessLocal`; the hook reports an actionable
error if native save pickers remain enabled. The hook does not modify browser
APIs or the application.

The hook checks the snapshot's existing Chrome target and immediately returns
`noresults` for unrelated pages. Saved `#json=` shares open in a separate
background tab in the same browser with temporary storage. Excalidraw loads and
decrypts the original share there; the persona's existing canvas and any overwrite
dialog in the main tab remain untouched. No API keys are needed.

The export tab closes on completion or termination. Chrome's existing daemons
clean up crashed exporters and cap tab lifetime at 60 minutes. A successful
provider request must confirm the scene import before export; import errors fail
the capture. Live `#room=` sessions return `noresults` because their URL alone
does not prove collaboration has finished synchronizing.

`EXCALIDRAW_ENABLED` defaults to true; set it false to disable extraction.
`EXCALIDRAW_TIMEOUT` bounds the operation (120 seconds, with `TIMEOUT` fallback). Outputs are ordinary files under
`excalidraw/files/`; `downloads.json` records relative paths, formats, sizes, and
SHA-256 hashes. ArchiveBox offers format buttons and direct previews.

Run through the normal capture lifecycle:

```bash
uv run abx-dl dl --dir="$(mktemp -d)" --plugins=excalidraw 'https://excalidraw.com/#json=pJK6JcJMr7LGOuy1NbCKP,YneEARvxllEU6vlDQfz81A'
```

Tests call the real Chrome crawl, tab, navigation and snapshot hooks against
public URLs. See `tests/RESULTS.md` for the actual evidence and limitations.

Real public capture replayed through ArchiveBox's normal file browser:

![excalidraw real capture replay](https://archivebox.io/screenshots/snapshot-view-excalidraw-desktop.png)

[Tablet](https://archivebox.io/screenshots/snapshot-view-excalidraw-tablet.png) · [Mobile](https://archivebox.io/screenshots/snapshot-view-excalidraw-mobile.png).

ArchiveBox offers native scene and SVG format buttons. The saved SVG previews the drawing; Download retains the selected editable scene or SVG original. Nested collections or duplicate formats use the file explorer.

[Live scheduled screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-excalidraw).
