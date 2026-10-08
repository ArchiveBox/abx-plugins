# Live export evidence — 2026-10-07

The real authenticated Chrome lifecycle and executable Figma hook saved both native FIG and PDF outputs. The positive test and the isolated anonymous restriction test passed together: **2 passed in 23.80s**, with unchanged deadlines. No mocks, internal extraction calls, fabricated payloads, retries, skips, or weakened assertions.

The final formatted hook and tests, including exact document-title and eight-page assertions, passed again: **2 passed in 22.59s**. Scoped pre-commit checks passed, including Ruff, Ty, Pyright, Prettier, spelling, test naming, and data syntax.

Fixture: https://www.figma.com/design/0YpAEiii3cM0l3xidTbWPk/Style-Guide-Starter--Copy---Copy-?node-id=0-1&p=f .
The starter design is linked from [Figma's design-system article](https://www.figma.com/blog/figma-on-figma-how-we-built-figma-dot-coms-design-system/).

The initial real test failed in 70.60s without a browser download. DOM hit inspection showed the newly opened File submenu had `pointer-events: none` while visible-selector checks already succeeded; a full-window backdrop received the Save local copy click. The hook now waits for the actual submenu's pointer state, then moves and clicks the control through the native mouse.

The next run completed the actual FIG download but timed out on PDF. File > Export frames to PDF first prepares images, then opens a native settings dialog requiring its separate Export button. The hook now activates the menu command with native keyboard Enter and confirms that actual Export control, keeping Figma's default profile/quality.

Verified files:

- `Style Guide Starter (Copy) (Copy).fig`: 23952990 bytes, SHA256 `863000d26cee432f20f99ede00370c0eaaa0b1e138081278fd04c4549c0da165`. ZIP CRC is valid; `canvas.fig` starts `fig-kiwi`; the thumbnail has a PNG signature; metadata contains client information; more than ten real image assets are present.
- `Style Guide Starter (Copy) (Copy).pdf`: 3825492 bytes, SHA256 `22213b04d6dd1fa4a96e6e902350eb010304e200557219a43366093ae0e0edd1`. Signature/EOF, actual byte length and manifest hashes pass. The test also asserts the document title and eight-page PDF count.

Existing `pdfinfo` independently verified 8 pages, PDF 1.7, unencrypted, with no PDF Title field. Chrome's PDF viewer should use the filename. Existing `pdftotext` extracted real first-page content: `Starter`, `Style Guide`, `Colors`, `Typography`, and `Hayes Valley Studio`. Provider exports include generated metadata, so repeat-export hashes can differ.

Retained successful outputs:
`/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/figma-final/test_authenticated_public_desi0/snap/test-snapshot/figma`.
DOM/UI diagnosis is retained alongside it in `figma-menu.png`, `figma-pdf-click.png`, and the `figma-*` scratch directories/scripts.

The authenticated test requires an explicitly supplied, authorized `AUTH_STORAGE_FILE` and uses headful Chromium. Cookie values were not printed. The anonymous test overrides authentication and persona/profile paths; it still returns a failed ArchiveResult and creates no downloads manifest. Figma blocked the tested headless session with HTTP 403. FigJam, Slides, Buzz, Sites, and Make native exports remain unverified; their successful formats are not claimed here. ArchiveBox gallery replay is owned by the parent acceptance workflow.

```console
TMPDIR=/tmp ABXPKG_LIB_DIR=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/runtime AUTH_STORAGE_FILE=/path/to/authorized/auth.json uv run --no-sync pytest -xq abx_plugins/plugins/figma/tests/test_figma_authenticated.py abx_plugins/plugins/figma/tests/test_figma.py
2 passed in 23.80s
```

Real ArchiveBox CLI and replay UI acceptance passed in 35.72 seconds on 2026-10-07 (`replay-figma.log`). The test captured the public source with the authorized browser session, opened the Embedded media card, downloaded an original with byte comparison, and verified the eight-page native PDF viewer. Unmodified desktop/tablet/mobile screenshots are retained in the external historical acceptance evidence directory.

## Quiet applicability and export prerequisites

The real unrelated DNS failure reproduced a false provider failure before URL
classification. All provider hooks now attach first, classify the requested and
current URLs, and wait for successful navigation only for document candidates.
The expanded suite passed 119 real cases, including provider homepages, external
query strings and two real DNS failures; no navigation markers were fabricated.

The headed anonymous Figma test also exposed a real sign-up dialog hidden behind
an earlier invisible dialog in DOM order. The hook now waits for the visible
menu/dialog and recognizes the actual sign-up prerequisite, including when it
appears after Save local copy. It returns skipped with exit 0. HTTP blocks,
missing controls, invalid documents and actual download failures remain failures.

The final combined Figma/Miro/Canva positive and prerequisite tests passed:
**7 passed in 103.13 seconds** (`export-status-final2.log`). Their authenticated
modules now have provider-specific filenames so they collect together normally.

Skip reasons are short hook stderr messages, also emitted as ArchiveResult
output_str for the existing runner. Figma's login reason is exactly
`Persona must be logged in to figma.com`. No shared emitter, log-file writer or
CLI rendering change is required.

The final real prerequisite-message suite passed **4 tests in 47.59 seconds**
(`prerequisite-stderr-final.log`). Assertions require the exact concise reason
in both captured hook stderr and ArchiveResult output_str, with exit code 0.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.

Historical browser screenshots and captures remain in `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/`. Current screenshots are regenerated and published by scheduled CI in the [live screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-figma); screenshot binaries are not checked in beside this record.
