# Live export evidence — 2026-10-07

Authenticated full Chrome hook lifecycle: **1 passed in 14.90s**.
Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/visual_docs/miro-final-positive/`.

The public board `https://miro.com/app/board/uXjVK4c-_uU=/` has title
“Master template for student activity frame”. The actual Main menu → Board →
Export → Save as PDF → Export flow saved its PDF with the default free quality.
Tests verify manifest title, file size/SHA-256, PDF signature/page/image structure,
and the exact exported JPEG SHA-256
`95eb1816613a615be25fdb47a9c3bd2c1300c278824997b76bfe825d4e5db01e`.
That 1377×774 image was visually inspected: the public frame contains a yellow
sticky note with “Hello”. This excludes blank/unrelated PDFs and ignores the
PDF's changing creation timestamp.

The first authenticated run failed on a text selector that matched the wrong
Board control. Subsequent real failures exposed menu toggles closing on click
and controls covered before layout settled. The hook uses the actual provider
test IDs, waits for the board canvas and hit-test eligibility, then hovers the
two submenus. No retry, synthetic application data or internal export call is used.

Native `.rtb` export is **unverified**: this public board's signed-in export menu
does not offer Download board backup. [Miro's documented backup requirements](https://help.miro.com/hc/en-us/articles/360017572774-How-to-save-board-backup)
remain necessary. The anonymous real test independently checks the actionable
sign-in failure and absence of an export manifest.

Real ArchiveBox CLI and replay UI acceptance passed in 32.65 seconds on 2026-10-07 (`replay-miro.log`). The test captured the public board with the authorized browser session, opened the Embedded media card, downloaded the original with byte comparison, and verified the native PDF viewer rendering the real Hello sticky note. Unmodified desktop/tablet/mobile screenshots are stored beside this record.

Final anonymous restriction test (with authorized cookies present in the outer
process but explicitly removed from this browser): **1 passed in 11.35s**.
Evidence: `workspace-artifacts/provider-plugins-20261007/visual_docs/miro-final-anonymous/`.

## Quiet export prerequisites

The new real anonymous test first failed because the hook returned exit code 1
and `failed` for the visible sign-in prerequisite. After the change, it passed
in the combined three-case negative suite: **3 passed in 28.00 seconds**.
The test verifies `skipped`, exit code 0, the actual sign-in reason and no manifest.
Evidence: `workspace-artifacts/provider-plugins-20261007/visual_docs/prerequisites-red/`
and `visual_docs/prerequisites-green.log` beneath the same artifact root.

An explicitly disabled PDF control now skips when no native backup was saved,
or omits PDF while preserving a captured backup. That permission-specific branch
is not independently live-verified; no suitable denied authenticated fixture was
available. Missing controls, timeouts and malformed downloads still fail.

The final combined authenticated captures and prerequisite cases for Figma,
Miro and Canva passed: **7 passed in 103.13 seconds** (`export-status-final2.log`).
Login skips now emit exactly `Persona must be logged in to miro.com` through
normal hook stderr and ArchiveResult output_str. The existing runner persists
and displays it; no custom log-file or CLI code was added.

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
