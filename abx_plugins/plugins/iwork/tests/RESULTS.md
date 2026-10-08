# Live iWork evidence

2026-10-07: missing-hook test failed first. Real native UI tests exposed overlapping first-launch and collaboration dialogs. The observed fix uses the topmost clickable controls, fills the guest nickname before enabling Join, activates the SproutCore welcome control with native keyboard Enter, and processes each onboarding phase once before selecting PDF.

Public URL: https://www.icloud.com/keynote/066uNOonke1QaqLmsnlMXpEng .
Provenance: https://iframely.com/domains/apple-keynote .

The same real Chrome lifecycle/executable hook test then passed: **1 passed in 76.78s** with `IWORK_TIMEOUT=120` (documented plugin default). No mocks, internal extraction calls, generated fixture payloads, retries, skipped or weakened assertions. Earlier inherited 60-second runs reached the actual native PDF export but expired while Apple's real job status advanced through `SCHEDULED_FOR_DOWNLOAD`, `SENT_TO_ACTIVEMQ`, and `PENDING`. The server generated the file in about 70 seconds.

The final hook, with debugging removed and the PDF title/page-count assertions included, passed again: **1 passed in 74.13s**. Its retained output is `microsoft_apple_box/iwork-final/test_public_keynote_pdf0/snap/test-snapshot/iwork` under the evidence directory below.

Saved `CoT-zuzalu-final.pdf`, 34459089 bytes, SHA256 `05bcc5bd747930c8912de32a3c8c3319e2ecd77bb0956e784efdf629a3d46f93`. The PDF signature, EOF, filename, byte length and manifest hash were checked. The positive test also checks `/Title (CoT-zuzalu-final)` and `/Count 61` in the actual PDF.

Existing real `pdfinfo` independently verified title `CoT-zuzalu-final`, 61 pages, 1920 × 1080 points, PDF 1.5, unencrypted, Quartz PDFContext. Existing `pdftotext` extracted the first page's actual text: `YUDKOWSKY VS. PLATO`, `CAN LANGUAGE MODELS HAVE KNOWLEDGE?`, and `Tarun Chitra`.

Retained successful outputs: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/iwork-120/test_public_keynote_pdf0/snap/test-snapshot/iwork`.
Retained native UI and export job diagnosis: `iwork-progress` and `iwork-export-debug` under the same scratch directory.

Unrelated URL validation passed in the shared seventeen-hook real `https://example.com` session: this hook returned `noresults` in 0.311s and created no iWork artifacts. Native Apple and Office conversions remain unverified and are not claimed by this hook.

```console
TMPDIR=/tmp ABXPKG_LIB_DIR=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/runtime CHROME_HEADLESS=true uv run --no-sync pytest -xq abx_plugins/plugins/iwork/tests --basetemp=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/microsoft_apple_box/iwork-120
1 passed in 76.78s
```

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered saved document; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-iwork`, case `0` in the external evidence directory.

Pages and Numbers were added through the same Tools/export flow after each real public test first failed with `noresults`. The complete three-app suite then passed **3 tests in 155.02 seconds** (`iwork-three-apps.log`). Pages exported the author's real `Protocole Raman.pdf` (5,395,491 bytes, 21 pages); text inspection confirmed “Spectroscopie Raman”, “Travaux pratiques en biophotonique”, and the WinSpec protocol sections. Numbers exported Swift Package Index's `NIO-dependency-check.pdf` (178,559 bytes, one 1283 × 6189-point page) with the real package table and compatibility summary. No new hook, dependency, browser or config field was needed. Public source links are in the README.

Applicability audit: attachment now precedes URL classification and navigation
waiting. Clearly unrelated original/current URLs return `noresults` before the
shared navigation waiter can report an unrelated DNS/TLS error. Identifiable
document navigation and export/content errors still produce `failed`. Provider
homepage cases use anonymous headless Chrome and require no output directory.

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
