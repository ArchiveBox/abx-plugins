# Live verification, 2026-10-07

The real Chrome export test passed using draw.io's official public `schema.xml`
example. It is included in the combined provider run: **5 passed in 90.60
seconds**.

The native SVG export contains `UserRole`, `AccountName`, and embedded `mxfile`
data for editing. Every saved file's size and SHA-256 match `downloads.json`.

Run from this repository:

```console
uv run pytest -xq abx_plugins/plugins/drawio/tests
```

The ArchiveBox replay test passed real CLI capture, Embedded media card, stock
file explorer, a byte-identical download, and rendering the saved SVG. This pass
is among the first five results in `replay-ten.log`; that run later failed on
another provider. The tested surface is the shared editor; lightbox-only
viewers are not covered.

Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/parent-plugins-final.log`
and `replay-ten.log`. The final replay and screenshot evidence follows below.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered saved content; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-images-green2`, case `1` in the external evidence directory.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.

## Empty diagram regression

The official mxgraph `empty.xml` fixture failed before the fix: the hook waited
for drawing shapes which a valid empty file never contains. A filename header
wait passed isolated plugin tests but failed ArchiveBox capture because that
header is absent in some real editor layouts. The hook now waits for the visible
diagram container: the provider's `EditorUi.fileLoaded` opens the native file,
then `setGraphEnabled(true)` reveals this container. The same readiness state
applies to empty and populated diagrams without depending on optional headers.

The complete real-browser draw.io test file passed: **4 passed in 38.81 seconds**.
It checks both the schema and empty diagram exports, embedded native data,
manifest sizes and hashes, and unrelated editor fragments. The empty SVG retains
its `MyWorkflow` and `Default Layer` native data without vertex or edge objects.

Evidence: `visual_docs/drawio-empty-red.log` and
`visual_docs/drawio-container-green.log` under the external evidence directory above.

The corrected readiness also passed the full real ArchiveBox CLI capture and
browser replay: **1 passed in 39.89 seconds**. This run followed the public
snapshot index, Embedded media card, format viewer, file explorer, and original
downloads, with byte comparisons against saved artifacts. Evidence:
`drawio-container-replay2.log` and retained collection
`drawio-container-replay2/` under the external evidence directory.
