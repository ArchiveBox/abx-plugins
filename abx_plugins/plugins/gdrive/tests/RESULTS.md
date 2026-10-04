# Google Drive acceptance results

Verified on 2026-10-04 with real Chrome and provider downloads on macOS.

- Public gdown test folder: provider ZIP contained all six files, including both
  nested directories. ZIP CRCs, byte size, SHA-256, and unchanged Chrome target
  IDs and page URLs passed.
- Created a private folder through the user's signed-in Brave session with two
  small test files. The actual `abx-dl dl --plugins=gdrive` flow, using Google-only
  cookies imported into an isolated Chrome persona, saved a 709-byte ZIP. Both
  files matched the originals byte for byte and ZIP CRCs passed.
- The same private folder without cookies failed and saved no archive files.
  Temporary cookie exports and imported browser profiles were removed. Private
  folder IDs and credentials are excluded from this repository.
- Actual `archivebox add --plugins=gdrive,dropbox` captured the public folder.
  The card appeared under **Embedded media**, and the viewer showed the ZIP button
  next to **View all files**. Downloading the saved ZIP over ArchiveBox HTTP
  returned a valid archive containing all six files.

The shared download test uses real Chromium and a cookie-gated HTTP endpoint to
verify HttpOnly authentication, isolation from another tab downloading the same
filename, unchanged document lifetime, and preservation after a truncated ZIP.
Two real browser launches verify native Chromium user agents replace only the
built-in generic runner identities. The existing custom-user-agent test is also
included in regression checks.

Large split archives, non-English provider controls, and download-disabled folders
have not been acceptance-tested. ZIP completeness follows Google's own output;
there is no recursive enumeration or independent remote inventory.
