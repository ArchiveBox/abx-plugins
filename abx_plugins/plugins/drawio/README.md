# draw.io

Exports a shared draw.io / diagrams.net editor document as SVG with the editable
diagram embedded. The hook uses File → Export as → SVG in an isolated background
export tab, keeps “Include a copy of my diagram” enabled, and downloads locally.
It does not edit or save the source document remotely.

Applicability is checked on the main capture tab. Eligible UI exports open the
source URL in a background tab using the same Chrome persona; the main capture
tab stays untouched. Export tabs close on completion or shutdown signals.
Chrome also cleans abandoned export tabs, with a hard 60-minute lifetime cap.

Uses the existing Chrome download helper and `saveDownloads` without additional
dependencies. `downloads.json` records the saved SVG's path, size and SHA-256.
The embedded-media stack opens the saved SVG preview.
One snapshot hook checks applicability and returns `noresults` immediately for
unrelated settled pages.

Settings (enabled by default): `DRAWIO_ENABLED` and `DRAWIO_TIMEOUT` (120 seconds, with `TIMEOUT`
fallback). The initial export surface is shared editor URLs on app.diagrams.net
and app.draw.io with a document location hash (`G`, `W`, `T`, `D`, `A`, `H`,
`R`, or a valid HTTP(S) `U` source). Arbitrary anchors, browser-local files,
editor configuration links, and lightbox-only viewers are not covered.

The live fixtures are draw.io's official public schema.xml and empty.xml examples.
Tests check the schema content and preserve the empty file's embedded native data.
Run `uv run pytest -xq abx_plugins/plugins/drawio/tests` in this repository.

Real public capture replayed through ArchiveBox's normal file browser:

![drawio real capture replay](https://archivebox.io/screenshots/snapshot-view-drawio-desktop.png)

[Tablet](https://archivebox.io/screenshots/snapshot-view-drawio-tablet.png) · [Mobile](https://archivebox.io/screenshots/snapshot-view-drawio-mobile.png).

ArchiveBox previews the saved SVG directly and keeps the editable SVG available through Download. Nested collections or duplicate formats use the file explorer.

[Live scheduled screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-drawio).
