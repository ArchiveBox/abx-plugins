# Live iCloud Drive evidence

## 2026-11-07: standalone Drive share URLs

[CI job 128448575615](https://github.com/ArchiveBox/abx-plugins/actions/runs/42387707280/job/128448575615)
failed because Apple redirected the legacy fixture to
`https://drive.icloud.com/shared/09bul5IE1NC7UNrQMRSWznvnw` with HTTP 302.
The retained Chrome network log in artifact `hook-logs-57` records that redirect;
the hook recognized only `www.icloud.com/iclouddrive/<share>`.

The unchanged live test reproduced locally: **1 failed, 1 passed**. Added direct
Drive URL and Drive homepage cases, then verified **2 failed, 2 passed** before
updating exact hostname/path recognition. A subsequent run exposed Apple's
client-side handoff back to `www.icloud.com/iclouddrive/<share>?redirect=false&sa=true`
while the hook's metadata fetch was running, destroying its execution context.
The hook now waits for the standalone app's resolved share/error view or that
native handoff before resolving once, within the existing deadline.

Final live coverage: **19 passed in 53.60 seconds**, covering both public URL
forms, both homepages and all 15 existing iCloud unrelated-page/navigation
regressions. A separate new expired-Drive-share case **passed in 5.24 seconds**,
requiring explicit CloudKit `NOT_FOUND` rather than a timeout or `noresults`.
Both public URL forms downloaded the original 41723-byte PDF with the unchanged
filename, PDF signature, EOF, manifest size and SHA256 assertions below. Both
homepages returned `noresults` without creating download output. Configured
scoped pre-commit checks, including full Ty/Pyright, passed. No authentication,
retries, timeout changes, or relaxed assertions were used.

## 2026-10-07: original implementation

2026-10-07: missing-hook test failed first, then the same real Chrome lifecycle and executable hook test passed: **1 passed in 8.87s**. No mocks, synthetic fixtures, internal helper calls, or skipped tests.

Source: `https://www.icloud.com/iclouddrive/09bul5IE1NC7UNrQMRSWznvnw#Tammy's_Poem_FINAL`.
Provenance: https://givealittle.co.nz/cause/help-brave-tammy-in-her-battle-with-mecfs/updates .
Saved `Tammy's Poem FINAL.pdf`, 41723 bytes, PDF header and EOF validated, SHA256 `140e13e3de9d0031e5b610d2f0c122e74303eff3c951503bab3526ecd437f60b`; manifest filename, size and hash match the actual file.

Retained output: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/icloud-evidence/test_public_pdf0/snap/test-snapshot/iclouddrive`.

Command (from abx-plugins):
```console
TMPDIR=/tmp ABXPKG_LIB_DIR=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/runtime CHROME_HEADLESS=true uv run --no-sync pytest -xq abx_plugins/plugins/iclouddrive/tests --basetemp=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/icloud-evidence
```

The older research workbook share `0f6fDVQ1pwdx3yckeXzrbAqkq` returned actual CloudKit `NOT_FOUND`, so it was rejected as a success fixture. Folder and authorized private-session support remain unverified.

Final combined Box/OneDrive/iCloud Python live tests: **6 passed in 54.85s**, including this public PDF with the fixed expected SHA256.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered document; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are retained in the external historical acceptance evidence directory. Source run: `replay-final2`, case `1` in the external evidence directory.

Applicability audit: attachment now precedes URL classification and navigation
waiting. Clearly unrelated original/current URLs return `noresults` before the
shared navigation waiter can report an unrelated DNS/TLS error. Identifiable
document navigation and export/content errors still produce `failed`. Provider
homepage cases use anonymous headless Chrome and require no output directory.
CloudKit's explicit `requireAppleLogin` field now reports `skipped`; share
resolution/content/network errors remain failures. This status branch has no
independently verified auth-required fixture and is not claimed as a successful
authenticated capture.

Final combined anonymous headless verification after the attachment-order fix:
**21 passed in 336.12 seconds**, covering all five provider test modules. This
includes ten real original/native captures (Office DOCX, SharePoint PDF,
OneDrive PDF/XLSX, Box PDF/SVG folder, iCloud PDF, and all three iWork PDFs),
ten provider homepage/invalid-viewer cases, and the existing Box unrelated-page
case. Assertions retained original signatures, content/page counts, ZIP CRC,
byte lengths and manifest hashes. The five-module run used the canonical hook
executables and real Chrome lifecycle, with no authenticated storage supplied.
Retained outputs are under
`/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/provider-final-document-errors`.
Scoped Prettier/Ruff/Ty/Pyright checks and all five JS syntax checks passed.
The parent separately owns the full seventeen-provider unrelated/DNS regression.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.

Historical browser screenshots and captures remain in `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/`. Current screenshots are regenerated and published by scheduled CI in the [live screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-iclouddrive); screenshot binaries are not checked in beside this record.
