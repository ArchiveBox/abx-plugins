# Dropbox acceptance results

Verified on 2026-10-04 with real Chrome and provider downloads on macOS.

- Public SHIFT Nursing Lockups folder: provider ZIP contained its root directory
  and eight PNG files. All PNG signatures, ZIP CRCs, byte size, and SHA-256 passed.
  Chrome target IDs and page URLs remained unchanged.
- An individual PNG shared through the folder preview downloaded in its original
  format. Dropbox's preview-to-child redirect was accepted without losing the
  shared root or selected filename. File signature, size, and SHA-256 passed.
- Actual `archivebox add --plugins=gdrive,dropbox` captured the public folder.
  The card appeared under **Embedded media**, with the ZIP button beside
  **View all files**. The saved ZIP served by ArchiveBox HTTP retained all eight
  PNG files and passed CRC validation.

The shared browser download tests cover cookie authentication, tab isolation,
and preservation of previous output after an incomplete archive. Login-required
and password-protected Dropbox shares have not been acceptance-tested; no suitable
private Dropbox fixture was available. Large folders and non-English controls
remain unverified. The provider performs the ZIP operation; the plugin never
walks child folders or files.
