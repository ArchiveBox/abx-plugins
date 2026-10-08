# Box live verification

2026-10-07, macOS arm64, real Chrome lifecycle (crawl launch, snapshot tab,
navigation, provider hook subprocess). Initial red test failed because the Box
hook was absent. After implementation:

```console
TMPDIR=/tmp ABXPKG_LIB_DIR=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/runtime CHROME_HEADLESS=true uv run --no-sync pytest -xq abx_plugins/plugins/box/tests/test_box.py
1 passed in 8.38s
```

Public fixture: https://app.box.com/s/7o5sbghsq70vxcz8f8bylhemnngi7rrz

Chrome clicked the real Box Download control. Saved `files/registration.pdf`,
507937 bytes, validated `%PDF-` signature and SHA-256 against downloads.json.
The preview may report a protection error while its original Download remains
available; capture correctly uses the original file. No API token or login.
The initial folder test reproduced saved ZIP transport instead of files.
After recognizing folder metadata and passing requireZip to saveDownloads, both
public file and folder tests pass (2 passed in 18.88s). Folder fixture
https://app.box.com/s/9h9dfu6nbfskj4p64gal20xuf7d7f635 produced 30 SVG logos,
all lengths and SHA-256 hashes verified. Authenticated shares remain unverified.
Retained output: /Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/box-evidence-green

Final combined Box/OneDrive/iCloud Python live tests: **6 passed in 54.85s**; Box public PDF, 30-file folder, unrelated page, OneDrive public PDF and workbook, and iCloud public PDF all validated.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered document; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-final11b`, case `4` in the external evidence directory.

Applicability audit: attachment now precedes URL classification and navigation
waiting. Clearly unrelated original/current URLs return `noresults` before the
shared navigation waiter can report an unrelated DNS/TLS error. Identifiable
document navigation and export/content errors still produce `failed`. Provider
homepage cases use anonymous headless Chrome and require no output directory.
A share redirected to Box's explicit `/login` page reports `skipped`; visiting
that login page directly returns `noresults`.

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
