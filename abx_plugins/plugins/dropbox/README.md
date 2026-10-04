# Dropbox

Saves Dropbox share links using the existing Chrome session and Dropbox's own
Download action. Shared folders are downloaded as the provider's ZIP, including
nested contents. Shared files retain their original format. No child enumeration,
recursive crawling, additional packages, API keys, or OAuth setup are required.

The hook uses the snapshot's settled tab, preserving share tokens, passwords
already entered in the browser, and the active login. It does not log in or submit
passwords. Access restrictions and the provider's folder-download limits remain
in effect; unavailable controls and incomplete downloads fail explicitly.

`downloads.json` records each saved file's relative path, size, and SHA-256.
Provider ZIPs are temporary transport: entries stream to `files/`, preserving
subdirectories, and temporary ZIP copies are removed after successful
extraction and CRC verification. Only unpacked files remain. Failed downloads
leave previously saved output intact. Extraction uses Python's standard library
from the existing abxpkg runtime; no extra package is installed.

The embedded preview uses ArchiveBox’s stock static-file directory index. Images are available
to liteparse OCR, and text files are discovered by Sonic's normal indexing path.
ZIP files supplied as actual folder contents remain ordinary files and can be
browsed using ArchiveBox's generic ZIP preview.

Settings: `DROPBOX_ENABLED` (default true), `DROPBOX_TIMEOUT` (default 120 seconds,
falls back to `TIMEOUT`). Recognizes modern `/scl/fi/` and `/scl/fo/` shares and
legacy `/s/` and `/sh/` links. Archive the preview link (`dl=0`); direct download
links (`dl=1`) are handled by the existing `staticfile` plugin during navigation.

Folder completeness follows Dropbox's ZIP output; the plugin does not separately
verify every remote child. Oversized folders fail rather than falling back to
recursive crawling. Dropbox Paper, Transfer, and non-share account URLs are not
supported.

Public fixture: the Lockups folder linked from SHIFT Nursing's
[public screening kit](https://www.shiftnursing.com/wp-content/uploads/2024/12/SHIFT_EverybodysWork_ScreeningKit_Fnl.pdf).
