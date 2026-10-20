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

`github-archivebox-releases-dom.html.xz` is the rendered DOM of
`https://github.com/ArchiveBox/ArchiveBox/releases`, snapshot
`06ac89f27dd5719c8000a2f536cb5988`, captured on 2026-10-20 UTC.
It contains ArchiveBox's own release announcements within GitHub's saved page.
Runtime scripts, styles, forms, inputs, inline event handlers, and nonce attributes
were removed; the remaining document structure and article content are retained.
The large real text/whitespace input exposes quadratic normalization in Postlight
2.2.3. The stable maintained Mercury 3.1.1 parser preserves the release article,
including its migration command and code/pre formatting, within the same deadline.
XZ compression keeps the real input below the repository's fixture size limit.
