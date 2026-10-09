# Canva

Save PDF from Canva's actual download menu. When that menu offers PPTX, DOCX or
XLSX, also save its editable export. The real SUNY Oswego public certificate
fixture passed with editable PowerPoint and PDF. Other document types have not
been independently verified; Canva has no universal native editable file type.

For public template previews, the hook follows the publisher's View template →
Open in Editor action in the background export tab. **This creates a copy in the signed-in
Canva account on each capture.** It does not edit the original design or change
sharing. Ordinary public views without this author-provided action return
`noresults` with exit code 0. Sign-in and premium-content prerequisites return
`noresults` with exit code 0. Purchases, trials and premium-content licences are
never accepted. Provider blocks, loading/network failures, missing export
controls and invalid exported documents remain failures.

Applicability is checked on the main capture tab. Eligible UI exports open the
source URL in a background tab using the same Chrome persona; the main capture
tab stays untouched. Export tabs close on completion or shutdown signals.
Chrome also cleans abandoned export tabs, with a hard 60-minute lifetime cap.

Public fixture: [SUNY Oswego certificate](https://www.canva.com/design/DAGudZAYlEE/VzrUrqpV2RhkVHCg43lvKQ/view?mode=preview&utm_campaign=designshare&utm_content=DAGudZAYlEE&utm_medium=link&utm_source=publishsharelink),
published in the [university's official Canva toolkit](https://ww1.oswego.edu/marketing-and-communications/Toolkit/Canva).
[Provider file-type documentation](https://www.canva.com/help/download-file-types/).
[Anonymous guests cannot download designs](https://www.canva.com/en_au/help/collaborate-with-anyone-variantb/).

The hook attaches to the Chrome snapshot target and checks the requested and
attached URLs before waiting for navigation. Unrelated pages return `noresults`
even when their navigation failed; recognizable designs still require successful
navigation. It uses existing browser authentication without API keys or a new
browser. Canva's use-template control may navigate the export tab into the copy.

`CANVA_ENABLED` defaults to true; set it false to disable extraction.
`CANVA_TIMEOUT` bounds the operation (120 seconds, with `TIMEOUT` fallback).
Files live under `canva/files/`;
`downloads.json` records relative paths, formats, sizes and SHA-256 hashes.
ArchiveBox offers format buttons and direct previews.

```bash
uv run abx-dl dl --dir="$(mktemp -d)" --plugins=canva 'https://www.canva.com/design/DAGudZAYlEE/VzrUrqpV2RhkVHCg43lvKQ/view?mode=preview'
```

The command uses the configured persona. Authenticated exports require that
persona to be logged in to Canva; an anonymous capture reports `noresults`.
Use `CHROME_HEADLESS=false` for authenticated exports of this fixture: the tested managed headless browser was
blocked inside Canva's template frame. The hook reports that provider block and
does not bypass it. The gallery recipe needs an accepted authenticated session.

Authenticated live tests require `AUTH_STORAGE_FILE` for an authorized persona and Poppler's
`pdftotext` for the saved PDF content assertions. These are test prerequisites;
the plugin has no additional runtime dependency. The anonymous test clears
inherited authentication explicitly. With the authenticated prerequisites
configured, run both:

```bash
uv run pytest abx_plugins/plugins/canva/tests -q
```


See `tests/RESULTS.md` for actual evidence and limitations.

## Real ArchiveBox replay

The public certificate was captured through the ArchiveBox CLI, then opened through
the Embedded media card and native PDF viewer. The original download bytes were
compared with the saved file. These screenshots are unmodified browser captures.

![Desktop replay](https://archivebox.io/screenshots/snapshot-view-canva-desktop.png)

![Tablet replay](https://archivebox.io/screenshots/snapshot-view-canva-tablet.png)

![Mobile replay](https://archivebox.io/screenshots/snapshot-view-canva-mobile.png)

ArchiveBox previews the saved PDF directly. Selecting an editable Office format keeps Download linked to that original and uses the saved PDF companion for preview. Nested collections or duplicate formats use the file explorer.

[Live scheduled screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-canva).
