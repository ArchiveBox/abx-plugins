# OneDrive live verification

2026-10-07, macOS arm64, real Chrome lifecycle plus standalone hook subprocess.
Initial red test failed because the hook was absent. Public PDF passed in
10.37s; native public Excel passed in 8.04s. Run:

```console
TMPDIR=/tmp ABXPKG_LIB_DIR=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/runtime CHROME_HEADLESS=true uv run --no-sync pytest -xq abx_plugins/plugins/onedrive/tests
```

PDF source https://1drv.ms/b/s!Ag5FKK35oL7BgoVMG0S2okA5Z7fwMQ?e=NycsI4
saved 2024.06.16 Bulletin.pdf, 4501713 bytes, %PDF- signature,
SHA-256 ebb431ecff5d9f16f2cc16d8c766027af4a48dbc9c88995eafb9ef4713bd50a2.

Excel source https://1drv.ms/x/s!AtWnsymKn5hRiD8rrKYuHTlatez2?e=7F03hJ
saved the original workbook, verified byte count/hash, ZIP CRC, xl/workbook.xml
and xl/worksheets/sheet1.xml.

The migrated OneDrive page loads but its UI never presents Download in this
headless browser. The current share-scoped /_api/v2.0/shares/u!.../driveItem/content
endpoint succeeds through the existing browser session. Legacy api.onedrive.com
returns HTTP401; download.aspx returns404. These are diagnostic findings, not
fallback paths in the hook. It uses only the endpoint proven with both files.

Folder ZIP capture, business SharePoint, and protected shares are unverified.
Retained evidence under
/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/onedrive-evidence
and onedrive-excel-evidence.

Final combined Box/OneDrive/iCloud Python live tests: **6 passed in 54.85s**, including both OneDrive PDF and original Excel content checks.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered document; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are retained in the external historical acceptance evidence directory. Source run: `replay-final4`, case `1` in the external evidence directory.

Applicability audit: attachment now precedes URL classification and navigation
waiting. Clearly unrelated original/current URLs return `noresults` before the
shared navigation waiter can report an unrelated DNS/TLS error. Identifiable
document navigation and export/content errors still produce `failed`. Provider
homepage cases use anonymous headless Chrome and require no output directory.
The candidate check recognizes both OneDrive shares and 1drv.ms share routes.
If an arbitrary original URL redirects to OneDrive, its final share URL supplies
the download token. Explicit known Microsoft account-login redirects report
`skipped`; ordinary account homepages remain unrelated.

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

Historical browser screenshots and captures remain in `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/`. Current screenshots are regenerated and published by scheduled CI in the [live screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-onedrive); screenshot binaries are not checked in beside this record.
