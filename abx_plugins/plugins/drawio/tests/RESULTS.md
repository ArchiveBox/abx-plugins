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
