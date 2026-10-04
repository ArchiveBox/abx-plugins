# Dropbox

Saves Dropbox share links using the existing Chrome session and Dropbox's own
Download action. Shared folders are downloaded as the provider's ZIP, including
nested contents. Shared files retain their original format. No child enumeration,
recursive crawling, additional packages, API keys, or OAuth setup are required.

The hook uses the snapshot's settled tab, preserving share tokens, passwords
already entered in the browser, and the active login. It does not log in or submit
passwords. Access restrictions and the provider's folder-download limits remain
in effect; unavailable controls and incomplete downloads fail explicitly.

`downloads.json` records filenames, saved paths, sizes, and SHA-256 hashes. ZIP
files stay intact. The embedded viewer places download buttons in the header
beside View all files. Failed downloads leave previously saved files intact.

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
