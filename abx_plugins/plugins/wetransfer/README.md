# WeTransfer

Uses the transfer page's Download control in a background tab in the existing Chrome session and the shared browser-download and manifest helpers. A delivery ZIP is retained unchanged and can be browsed with ArchiveBox's existing ZIP viewer. No WeTransfer API, credentials, or new dependencies.

Applicability is checked on the main capture tab. Eligible UI exports open the
source URL in a background tab using the same Chrome persona; the main capture
tab stays untouched. Export tabs close on completion or shutdown signals.
Chrome also cleans abandoned export tabs, with a hard 60-minute lifetime cap.

One snapshot hook attaches to the existing tab and returns `noresults` on unrelated
pages before waiting for navigation. Only `WETRANSFER_ENABLED` (default true) and
`WETRANSFER_TIMEOUT` (seconds, default 120, with `TIMEOUT` fallback) are configurable.
Disabled capture returns `skipped`; identifiable transfer navigation/download
errors remain `failed`. The hook automatically accepts WeTransfer's Terms of
Service prompt using its normal I agree button before downloading. Expired or
revoked shares fail explicitly. No account is created.

The live positive test downloads the public [Dr Katherine Hunt media kit](https://we.tl/t-vRtsYmlUVM), published on her [official media-kit page](https://www.drkatherinehunt.com/media-kit). It verifies the unchanged 12,086,255-byte ZIP, manifest hash, all six filenames and CRCs, and actual JPEG decoding in Chrome. The screenshot recipe waits for the real ZIP filename; replay screenshots are verified separately.

The [SAMSA training share](https://we.tl/t-JU6cMxNNFJMrW72K) is expired. A separate regression verifies automatic Terms acceptance followed by the actual expired-transfer failure, without publishing a manifest.

Real public capture replayed through ArchiveBox's normal file browser:

![wetransfer real capture replay](https://archivebox.io/screenshots/snapshot-view-wetransfer-desktop.png)

[Tablet](https://archivebox.io/screenshots/snapshot-view-wetransfer-tablet.png) · [Mobile](https://archivebox.io/screenshots/snapshot-view-wetransfer-mobile.png).

[Live scheduled screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-wetransfer).
