# Nextcloud / ownCloud

Downloads public file and folder shares from Nextcloud and ownCloud using the
background export tab in the snapshot's Chrome session. Uses the provider's Download control and the
shared browser download/unpacking helpers. No API credentials or extra packages.

Applicability is checked on the main capture tab. Eligible UI exports open the
source URL in a background tab using the same Chrome persona; the main capture
tab stays untouched. Export tabs close on completion or shutdown signals.
Chrome also cleans abandoned export tabs, with a hard 60-minute lifetime cap.

One snapshot hook checks the settled page's URL and provider markers. Unrelated
pages return `noresults` without requests or selector waits. Password-protected
shares require the existing browser session to have unlocked them. Download
permissions and provider ZIP limits apply.

Outputs follow Google Drive and Dropbox: `downloads.json` records relative paths,
sizes and SHA-256 hashes; originals are in `files/`. Folder ZIPs are verified and
unpacked before replacing prior successful output. Ordinary ZIP files shared as
files remain ZIPs. The embedded-media stack uses the stock file explorer.

Settings (enabled by default): `NEXTCLOUD_ENABLED` and `NEXTCLOUD_TIMEOUT` (120 seconds, with `TIMEOUT`
fallback). Recognizes `/s/TOKEN` and `/index.php/s/TOKEN` on self-hosted domains.

Real public fixtures:

- [Nextcloud ISV press assets](https://cloud.nextcloud.com/s/Ha8J6so77yXNrQ3), linked
  from Nextcloud's ISV partner program announcement.
- [ownCloud teaching data](https://owncloud.gwdg.de/index.php/s/MwST1BwNLxtDon3),
  published by the University of Goettingen's R teaching guide.

Run `uv run pytest -xq abx_plugins/plugins/nextcloud/tests` in this repository.

Real public capture replayed through ArchiveBox's normal file browser:

![nextcloud real capture replay](https://archivebox.io/screenshots/snapshot-view-nextcloud-desktop.png)

[Tablet](https://archivebox.io/screenshots/snapshot-view-nextcloud-tablet.png) · [Mobile](https://archivebox.io/screenshots/snapshot-view-nextcloud-mobile.png).

[Live scheduled screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-nextcloud).
