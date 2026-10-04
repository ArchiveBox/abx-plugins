# Google Docs acceptance results

Verified on 2026-10-04 with real Google endpoints and Chrome on macOS.

- Public Docs: DOCX, PDF, ODT, RTF, TXT, Markdown, HTML ZIP, EPUB.
- Public Sheets: XLSX, CSV, PDF, ODS, TSV, HTML ZIP.
- Public Slides: PPTX, PDF, ODP, TXT.
- Public Drawings: SVG, PDF, PNG, JPG.
- Existing private Google Doc: DOCX, PDF, TXT succeeded through the real
  `abx-dl dl --plugins=googledocs` flow with Google-only cookies imported from
  the user's signed-in Brave session into an isolated Chrome persona.
  DOCX archive integrity, file hashes, and matching text in DOCX/PDF/TXT passed.
- The same private URL in a fresh persona without cookies failed with HTTP 401
  for every format, exit code 1 from the plugin, and no exported document files.
  Private document identifiers and credentials are not checked into this repo.
  The temporary cookie export and test personas were removed after verification.
- Saved `responses` exports were reused byte-for-byte. Missing, interrupted,
  and corrupted captures were exercised through the real recorder and hook.
- A real cookie-gated HTTP endpoint verified HttpOnly cookies, binary streaming,
  cache reuse without a second HTTP request, and HTTP 403 output preservation.
- Live exports preserved Chrome's target IDs and document lifetime. An invalid
  Sheets `gid` failed without replacing the previous valid CSV.
- A real ArchiveBox capture displayed the plugin card under **Embedded media**.
  CSV selection rendered the saved spreadsheet, and native PDF rendering was
  visually checked in Chromium. The standalone CLI also saved all six Sheets
  formats successfully.

Validation command:

```sh
uv run --no-sync pytest \
  abx_plugins/plugins/googledocs/tests \
  abx_plugins/plugins/responses/tests \
  tests/test_dependency_boundaries.py \
  tests/test_hook_input_contract.py \
  tests/test_plugin_executables_have_shebang.py \
  tests/test_plugin_config_metadata.py \
  -k 'not pnpm' -q
```

Result: **33 passed**, one unrelated package-install test deselected. All
pre-commit checks passed, including repository-wide type checking. Private
authentication was a manual acceptance test; CI uses public documents and the
real local cookie-gated endpoint without requiring account credentials.
