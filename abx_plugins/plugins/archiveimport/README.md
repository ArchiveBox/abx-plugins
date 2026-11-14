# Archive Import

The hook downloads item metadata and publicly available item files for
`archive.org/details/<identifier>` and `archive.org/download/<identifier>` URLs.
Files are saved under `archiveimport/files/` with their original names. The
metadata API response and the list of downloaded files are saved in
`archiveimport/metadata.json`. Private files are omitted. Existing files are
replaced only after each download completes and checksums supplied by the
Internet Archive are verified. The default 100 MiB total limit protects against
unexpectedly large items; set `ARCHIVEIMPORT_MAX_BYTES=0` to remove it. An item
exceeding the limit is reported as failed, not as a complete capture.

For `perma.cc/<GUID>` URLs, set `ARCHIVEIMPORT_PERMA_API_KEY` to an API key
permitted to download that archive. The plugin downloads the actual WACZ export
from [Perma's documented API](https://perma.cc/docs/developer), and reports
unavailable or unauthorized exports as no results.

Archive replay pages are not item downloads and replayed responses are not
original WARC or WACZ files. Ghostarchive links are downloaded as the WARC advertised by their replay page.
Ghostarchive may reject the browserless default HTTP user agent; configure
`USER_AGENT` or use a persona with a recorded Chrome user agent if it returns 403.
Direct export access for the remaining services has not been established; those
URLs return no results.

For timestamped `web.archive.org/web/<timestamp>/<url>` links, the hook saves
the original response served by the Wayback Machine's `id_` replay mode as
`archiveimport/response`, alongside capture time, content type and available
source reference in metadata. This is a replayed response, not the original WARC,
and it does not include a page's other resources.
