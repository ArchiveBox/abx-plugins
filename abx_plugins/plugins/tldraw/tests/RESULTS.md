# Live export evidence — 2026-10-07

Red test: the real public-board test failed because the hook was missing.
After implementation: **1 passed in 9.45s** using actual Chrome crawl, tab,
navigation and snapshot hooks. No mocks, fixture server or internal extraction
function.

Public source: `https://www.tldraw.com/r/learn_with_jason`, published with the
[Learn with Jason collaborative-app episode](https://codetv.dev/series/learn-with-jason/s6/collaborative-real-time-apps-with-partykit).
The read-only board exports a full native `.tldr` (217,253 bytes) and a current-page
SVG (7,515 bytes). Assertions parse native schema/records, require pages and more
than ten shapes, verify SVG paths, and check every file size and SHA-256 hash.

Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/visual_docs/tldraw-test3/test_public_readonly_board0/snap/test-snapshot/tldraw/`.

A page-ready wait for real shapes and bringing the tab to the foreground are
required before interacting with menus; background renderer geometry checks
previously stalled. Exports use genuine visible controls and mouse events.
ArchiveBox replay screenshots remain a separate host acceptance step.

Final combined verification: **5 passed in 30.93s** (two successful native+SVG exports and three explicit access restrictions).
Latest retained outputs: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/visual_docs/verified-five/test_public_readonly_board0/snap/test-snapshot/tldraw/`.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered saved content; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-images-green2`, case `3` in the external evidence directory.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.
