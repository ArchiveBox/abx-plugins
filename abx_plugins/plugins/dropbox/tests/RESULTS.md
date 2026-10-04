# Dropbox acceptance results

Verified 2026-10-04 with real Chrome and provider downloads on macOS.

- Public SHIFT Nursing Lockups folder: eight unpacked PNG files, with valid
  signatures, matching sizes and SHA-256 hashes. The transport ZIP and owned
  browser download copy are deleted after CRC-checked extraction succeeds.
- An individual shared PNG retains its original format and filename. The
  preview-to-child redirect preserves the selected shared file.
- The original ArchiveBox capture URL was rerun with the full configured plugin
  set. Modalcloser pauses only while the provider download UI is active, then
  resumes; no plugins were filtered out. The raw files appear in the ordinary
  file browser and the compact embedded explorer.
- LiteParse OCR generated text from the exported PNGs. The live Sonic service
  returns the original Dropbox capture for `healing` from the image text.
- Shared tests verify real cookie authentication, cross-tab download isolation,
  preservation of previous output after invalid ZIPs, path/symlink rejection,
  nested ZIP preservation and deletion of temporary transport copies.

Private/password-protected Dropbox shares have not been tested because no
fixture was available. Large folders and non-English controls remain unverified.
The provider performs the folder ZIP operation; the plugin never crawls children.
