# Google Drive

Saves a shared folder using Google Drive's own ZIP download. Requires the existing
Chrome plugin session, with no additional binaries, API keys, or OAuth setup.

The hook reuses the snapshot's settled tab. Signed-in Drive uses the folder menu's
Download action; public, signed-out Drive uses its Select all / Download action.
Google prepares the archive, including subfolders and its default conversions of
native Workspace files. The plugin does not enumerate children, follow folder
links, or export documents individually. Use `googledocs` on individual document
URLs to select additional export formats.

`downloads.json` records the provider filenames, saved paths, sizes, and SHA-256
hashes. The ZIP files remain intact; they are not extracted. The embedded viewer
shows downloads in its header beside View all files. Failed preparation/downloads
are reported as failed and do not replace previously saved output.

Settings: `GDRIVE_ENABLED` (default true), `GDRIVE_TIMEOUT` (default 120 seconds,
falls back to `TIMEOUT`). Recognizes `/drive/folders/ID`, `/drive/u/N/folders/ID`
and legacy `/folderview?id=ID` links. Account selection and resource keys are
preserved because the hook uses the existing page rather than reconstructing a
ZIP URL. Private folders require the Chrome persona's existing Google login.

The provider controls ZIP limits, excluded files, and document conversion. This
is a capture of the provider's download, not an independently audited recursive
backup. Download-disabled, inaccessible, empty, or oversized folders can fail.
Google may change its web UI; the hook fails explicitly when its controls are
unavailable. It never falls back to a recursive crawl.

Public fixture: [gdown's nested test folder](https://drive.google.com/drive/folders/1KpLl_1tcK0eeehzN980zbG-3M2nhbVks),
maintained in [gdown tests](https://github.com/wkentaro/gdown/blob/main/tests/test_download_folder.py).
