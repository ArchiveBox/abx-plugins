# iCloud Drive

Saves an original publicly shared iCloud Drive file through the attached Chrome session. One snapshot hook resolves Apple public CloudKit share metadata in the current browser context and streams its original download using the shared CDP resource helper. The shared download saver creates `downloads.json` and a local `files/` tree.

No Apple Account setup, browser navigation, extra tabs, or new dependency is required. Configure `ICLOUDDRIVE_ENABLED` (default true) and `ICLOUDDRIVE_TIMEOUT` (seconds, default 120, with `TIMEOUT` fallback).

Verified with [Tammy's Poem FINAL.pdf](https://www.icloud.com/iclouddrive/09bul5IE1NC7UNrQMRSWznvnw#Tammy's_Poem_FINAL), publicly linked by the [author's fundraiser updates](https://givealittle.co.nz/cause/help-brave-tammy-in-her-battle-with-mecfs/updates). The iCloud sign-in shell does not prevent resolving this public original. Expired shares produce an explicit failed result; restricted shares and folders needing an Apple Account are not silently classified as unrelated URLs. Folder downloads and signed-in private Drive captures are unverified.

The hook attaches before checking original/current URL applicability or waiting
for navigation. Ordinary Drive/login homepages return `noresults`; explicit
CloudKit `requireAppleLogin` prerequisites return `skipped`. Identifiable share
navigation, resolution, download and validation errors remain `failed`.

Public metadata endpoint provenance: [icloud-resolver source](https://github.com/xxanqw/icloud-resolver) and [original public resolver example](https://gist.github.com/jpillora/702ded79330043e38e8202b5c73835e5). No third-party resolver package is installed or invoked.

Run `uv run pytest abx_plugins/plugins/iclouddrive/tests -q` with real Chrome prerequisites. See [test evidence](tests/RESULTS.md).

Real public capture replayed through ArchiveBox's normal file browser:

![iclouddrive real capture replay](https://archivebox.io/screenshots/snapshot-view-iclouddrive-desktop.png)

[Tablet](https://archivebox.io/screenshots/snapshot-view-iclouddrive-tablet.png) · [Mobile](https://archivebox.io/screenshots/snapshot-view-iclouddrive-mobile.png).

[Live scheduled screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-iclouddrive).
