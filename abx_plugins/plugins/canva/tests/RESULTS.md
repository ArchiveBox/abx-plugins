# Live export evidence — 2026-10-07

Authenticated public-source Chrome hook lifecycle: **1 passed in 21.67s**.
Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/visual_docs/canva-final-positive/`.

The test starts from the public SUNY Oswego certificate template, using its real
View template → Open in Editor → Share → Download UI flow. It creates an account
copy through the publisher's explicit action; no original edits or sharing
changes occur. No private editor URL is committed as a fixture or recipe.

Saved files are editable `.pptx` and `.pdf`. Assertions check manifest sizes and
SHA-256, native ZIP CRCs, actual slide XML containing CERTIFICATE and APPRECIATION,
embedded images, PDF signature/EOF and real extracted text including
“OF APPRECIATION”, “This certificate is awarded to”, and “First Lastname”.
The curved heading uses individually positioned glyphs whose PDF reading order
separates letters; the whole word is checked in the editable slide XML instead.
The real saved PDF was rendered and visually inspected against the author template.

The final implementation followed observed failures: Share is a menuitem;
format labels contain newlines; Escape from the format menu closed the entire
download panel. Controls now wait for actual hit-test eligibility, and the first
format is selected directly from the already-open menu. No retry, fake content,
internal export call or guessed editor URL was used.

[Official university provenance](https://ww1.oswego.edu/marketing-and-communications/Toolkit/Canva).
The public URL is recorded in `screenshot.json` and `test_canva_authenticated.py`.

The older Utah FAFSA template opens but contains two premium images. Its real
PowerPoint download displayed a trial or US$2+tax license choice. No purchase or
trial was accepted. In managed headless Chrome, the use-template iframe was
blocked with “We'll have you designing again soon” and RayID text. The successful
full lifecycle used headed Chrome with an authorized existing persona; the hook
reports provider blocks explicitly rather than trying to bypass them.

Final anonymous sign-in restriction: **1 passed in 8.73s** in headed Chrome,
with inherited AUTH_STORAGE_FILE explicitly cleared for that browser.
Evidence: `workspace-artifacts/provider-plugins-20261007/visual_docs/canva-final-anonymous/`.

The first full CLI acceptance exposed two different Download controls during
menu transitions. The hook now waits for the selected file type in the export
form and targets that form's submit button. It does not reuse the Share menu
entry as the final export action. The fresh real CLI and replay test passed in
**36.19 seconds** (`replay-canva-final.log`), including original-download byte
comparison and the real PDF viewer. The three screenshots beside this record
were copied byte-for-byte from that successful run. Temporary diagnostic logging
was removed before this capture.

After the export-form correction and formatting, the real positive content test
and anonymous restriction test both passed: **2 passed in 27.07 seconds**
(`canva-content-final.log`).

## Quiet unsupported views and prerequisites

New real negative assertions first failed for both anonymous template sign-in
and the ordinary public SUNY certificate `/view` URL: both previously returned
exit code 1. Together with Miro's anonymous case, the red suite had **3 failures
in 24.53 seconds**. After the changes, **3 passed in 28.00 seconds**.
Canva's tests assert exit code 0, `skipped` for the actual sign-in prompt,
`noresults` for the unsupported ordinary view, the specific reason and no manifest.
They use headed Chrome with inherited authentication explicitly cleared.
Evidence: `workspace-artifacts/provider-plugins-20261007/visual_docs/prerequisites-red/`
and `visual_docs/prerequisites-green.log` beneath the same artifact root.

The already observed premium-content prompt now maps only its dedicated export
prerequisite error to `skipped`. It was not recaptured with authentication during
this change. Provider blocks, control timeouts, network errors and invalid
exports retain their failure behavior; there is no general error suppression.

The final combined authenticated captures and prerequisite cases for Figma,
Miro and Canva passed: **7 passed in 103.13 seconds** (`export-status-final2.log`).
Login skips now emit exactly `Persona must be logged in to canva.com` through
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
