# Live export evidence — 2026-10-07

Red test: the real public-scene test failed because the hook was missing.
After implementation: **1 passed in 6.64s** with actual Chrome lifecycle and
`CHROME_ARGS_EXTRA=["--disable-blink-features=FileSystemAccessLocal"]`.
No mock, fixture server, internal extraction function or browser API patch.

The provider loaded/decrypted the published scene linked from
[excalidraw-decrypt](https://github.com/loveholidays/excalidraw-decrypt).
The test downloaded `.excalidraw` (48,823 bytes) and embedded SVG (47,138 bytes).
It parsed the native JSON, verified more than ten real scene elements and
`Excalidraw` text, and checked SVG content, sizes and SHA-256 hashes against
`downloads.json`.

Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/visual_docs/excalidraw-test/test_public_encrypted_scene0/snap/test-snapshot/excalidraw/`.

Without the Chrome feature setting, the actual native Save to file and SVG
controls invoke `showSaveFilePicker`, which aborts unattended headless saving.
ArchiveBox-managed Chrome now disables `FileSystemAccessLocal` by default;
externally attached browsers retaining native save pickers receive an actionable
browser-setting error before export.
ArchiveBox replay screenshots remain a separate host acceptance step.

Final combined verification: **5 passed in 30.93s** (two successful native+SVG exports and three explicit access restrictions).
Latest retained outputs: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/visual_docs/verified-five/test_public_encrypted_scene0/snap/test-snapshot/excalidraw/`.

Default-browser follow-up: the real test failed without its per-test Chrome flag
(**1 failed in 5.99s**). Adding `FileSystemAccessLocal` to the existing default
`--disable-blink-features` switch made the same test pass with no override:
**1 passed in 6.55s**. Native JSON and the SVG's compressed editable payload
contain matching element IDs; all saved bytes match the manifest's sizes/hashes.
Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/visual_docs/excalidraw-default-green/test_public_encrypted_scene0/snap/test-snapshot/excalidraw/`.

Empty homepage regression: **1 passed in 5.85s** through the actual Chrome
lifecycle. `https://excalidraw.com/` returns `noresults` within the test's ten-second
hook bound and creates no download manifest.

Persistent-persona regression: **2 failed in 28.31s** before the fix. The test used
the real Text tool to edit the published scene, reloaded its actual share, and
observed the provider's overwrite confirmation. Canceling that dialog made the
old hook report success while saving the edited local marker in its native file.
That incorrect output remains under `visual_docs/excalidraw-persona-red/` for
inspection. No local-storage writes, simulated dialogs, or application internals
were used to create the scene.

After fixing import readiness and provenance: **4 passed in 32.38s**, including
the clean-profile public export, homepage, pending-overwrite and canceled-import
cases. Both denied imports fail within eight seconds, emit no download manifest,
and preserve the settled local scene exactly; the pending dialog remains open.
The SVG export waits for both the checked input and the provider's rendered
`Switch.toggled` state before clicking Export to SVG.
Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/visual_docs/excalidraw-persona-green2/`.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered saved content; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-images-green2`, case `4` in the external evidence directory.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.
