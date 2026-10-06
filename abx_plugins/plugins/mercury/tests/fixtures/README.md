These compressed HTML inputs are read-only copies of the rendered DOM artifacts
from the existing ArchiveBox collection, included to exercise the real parser
against the failures observed in that capture:

- `docs-sweeting-cookie-dilemma-dom.html.gz`: `https://docs.sweeting.me/s/cookie-dilemma`, snapshot `01a0dba2a09c779fb39efcd5022cac5d`.
- `wikipedia-commitment-scheme-dom.html.gz`: `https://en.wikipedia.org/wiki/Commitment_scheme`, snapshot `01a0dba3b12273689a977cd3dd003353`.

Both snapshots were captured on 2026-09-26 UTC in the existing collection.

The Wikipedia page is from Wikimedia contributors and is available under
Creative Commons Attribution-ShareAlike; the fixture preserves its source URL
and snapshot identity above. Browser-injected and site JavaScript `<script>`
elements, plus inline event-handler attributes, were removed because the
rendered DOM is the parser input and those runtime-only values are not needed
for this regression. The actual MediaWiki `mw-data:TemplateStyles` href remains
intact because it is the malformed value that reproduced the resolver failure.
Neither fixture is a synthetic parser example.
