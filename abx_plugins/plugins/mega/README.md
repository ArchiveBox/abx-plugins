# MEGA

**Native capture is UNVERIFIED and acceptance remains incomplete.**

This hook attaches to the existing Chrome target for modern `mega.nz/file/…#…`
or `mega.nz/folder/…#…` public shares. It requests the real webclient Download
control for files and the ZIP download menu for folders. The webclient owns
decryption and delivery. The hook adds no crypto library, API key, OAuth flow,
browser launch, tab creation, navigation, or internal application calls.

The implementation uses MEGA's official source controls:

- [Download page components](https://github.com/meganz/webclient/blob/a3900cd4c19be52ef6e0daad423acf89edaa2e02/html/js/downloadUI.js): the file footer/header Download buttons call `startDownload`.
- [File manager toolbar](https://github.com/meganz/webclient/blob/a3900cd4c19be52ef6e0daad423acf89edaa2e02/html/fm.html): `fm-download` is the public-folder download control.
- [Toolbar download menu](https://github.com/meganz/webclient/blob/a3900cd4c19be52ef6e0daad423acf89edaa2e02/js/ui/components/meganz/fm-secondary-nav.js#L353-L383): its native ZIP button contains `icon-download-zip` and requests a browser ZIP of the current folder.

`MEGA_ENABLED` defaults to true. `MEGA_TIMEOUT` (default 120 seconds) bounds provider preparation,
decryption, download and extraction, inheriting `TIMEOUT`. Browser download
events must complete before publication. A public-folder ZIP is CRC-checked and
extracted into `mega/files/`; an individual file retains its original bytes.
`mega/downloads.json` records file paths, sizes, formats and SHA-256 hashes.
The embedded-media templates display the saved file tree.

The browser's site-safety policy blocked third-party fixtures and the small
promotional GIF folder published in the [official MEGAcmd guide](https://github.com/meganz/MEGAcmd/blob/master/UserGuide.md#exporting-and-importing).
No alternative route was used to reach those resources. Static source review
corrected the folder hook to use the toolbar's ZIP button; its former selector
belonged to a separate context menu. This correction is not live-verified.
No fabricated success test or provider screenshot recipe is included. Live
file/folder downloads, decrypted-content assertions and hosted replay screenshots
remain outstanding. See `tests/RESULTS.md`.
