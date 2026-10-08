# Microsoft365 live verification

2026-10-07, macOS arm64, real Chrome lifecycle and standalone hook subprocess.
Initial red test failed because the hook was absent. After implementation:

```console
TMPDIR=/tmp ABXPKG_LIB_DIR=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/runtime CHROME_HEADLESS=true uv run --no-sync pytest -xq abx_plugins/plugins/microsoft365/tests/test_microsoft365.py
1 passed in 7.52s
```

Real source https://docs.cpuc.ca.gov/PublishedDocs/Published/G000/M551/K722/551722326.docx
viewed through https://view.officeapps.live.com/op/view.aspx?src=https%3A%2F%2Fdocs.cpuc.ca.gov%2FPublishedDocs%2FPublished%2FG000%2FM551%2FK722%2F551722326.docx

The downloaded original is ZIP/CRC-valid, contains [Content_Types].xml and
word/document.xml, and asserts actual PUBLIC UTILITIES COMMISSION and
VEGETATION MANAGEMENT WORK content, length, and SHA-256. Retained evidence:
/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/office-evidence

The viewer renders the 55-page document, but its More menu remained Loading
in diagnostic attempts. Native Office editor conversions and other viewer
formats remain unverified.


Native SharePoint PDF preview test initially failed with `noresults`, then passed
**1 passed in 7.41s** after implementing its actual `#downloadCommand` control.
Source: https://pennstateoffice365.sharepoint.com/:b:/s/Research-to-PolicyCollaboration/EdtX1PTKSn5Apst-a_IKjIYBiP156uATgWctqeH0DK0v1A?e=mCHpWq
(from the Cambridge paper linked in the README).
Saved `Gay (2018) Network Engagement Survey Report_FINAL.pdf`, 1254865 bytes,
PDF header and EOF checked; SHA256
`92b0400cee14ed189694ff1ef518cd5acdc210e3edf3b0ed301723c706bd7ff0`.
Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/sharepoint-green/test_public_sharepoint_pdf0/snap/test-snapshot/microsoft365`.
The test uses the real Chrome lifecycle and executable hook subprocess.

Final combined real test run: **4 passed in 24.75s**, covering the Office DOCX original, native SharePoint PDF, ordinary SharePoint tenant root, and Office homepage. Both ordinary homepages return `noresults` without provider selector waits or provider download artifacts.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered document; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-final2`, case `0` in the external evidence directory.

Applicability audit: attachment now precedes URL classification and navigation
waiting. Clearly unrelated original/current URLs return `noresults` before the
shared navigation waiter can report an unrelated DNS/TLS error. Identifiable
document navigation and export/content errors still produce `failed`. Provider
homepage cases use anonymous headless Chrome and require no output directory.
Missing, empty, malformed and non-HTTP Office viewer sources are excluded.
Six real negative lifecycle cases already passed before the parser change
(46.38 seconds): missing/empty sources redirected to Microsoft's marketing
site, and invalid/non-HTTP sources did not expose a valid viewer target. Thus
the parser bug was identified in source; no live failing reproduction is claimed.
Known Microsoft login redirects from document shares now report `skipped`.

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
