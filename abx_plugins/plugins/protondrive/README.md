# Proton Drive

Downloads a public Proton Drive share using its native Download control in the existing Chrome tab. Proton's own application decrypts the files; this plugin does not implement encryption or use credentials outside Chrome. Folder ZIPs are unpacked with the existing safe download helper, preserving subdirectories. Single original files remain unchanged. `downloads.json` records file sizes and SHA-256 hashes and the standard ArchiveBox explorer replays `files/`.

One snapshot hook attaches to Chrome, checks the requested/current share URLs,
then returns `noresults` on unrelated pages before waiting for navigation.
`PROTONDRIVE_ENABLED` (default true) and `PROTONDRIVE_TIMEOUT` (seconds, default
120, with `TIMEOUT` fallback) are the only options. Disabled capture returns
`skipped`; identifiable share navigation/download errors remain `failed`.
Password-protected shares must already be unlocked in Chrome. Password flows
and signed-in private Drive items have not been verified.

The real public folder in `screenshot.json` is published in the [pdown upstream README](https://github.com/Bergbok/pdown). It currently contains a text file, JPEG, and MP4 in nested directories (about 14 MB). Third-party test contents can change.

Real public capture replayed through ArchiveBox's normal file browser:

![protondrive real capture replay](tests/replay-desktop.jpg)

[Tablet](tests/replay-tablet.jpg) · [Mobile](tests/replay-mobile.jpg).
