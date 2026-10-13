# Vendored papers-dl fixes

Upstream: https://github.com/benmuth/papers-dl/pull/5
Base PyPI package: papers-dl 0.0.25 (51b265c).
License: MIT; see LICENSE. These Python files are copied unchanged from the
canonical papers-dl source at `b20aa49c6db49ef70f52c878eac647429d179729`.

The papersdl snapshot hook puts this directory first on the installed CLI's
PYTHONPATH. It replaces its parser/fetch/provider modules
without changing the shared installed package or importing papers-dl into the
hook's interpreter. Runtime dependencies come from
the pinned PyPI package. Remove this overlay and update the package pin once
the upstream release contains these changes.

The default fallback order starts with Sci-Hub directory discovery, Unpaywall,
PMC, Europe PMC, bioRxiv, and medRxiv. Existing publisher and repository providers
follow. A successful PDF ends discovery. Responses must pass HTTP status and
PDF-signature checks.

PAPERSDL_UNPAYWALL_EMAIL defaults to unpaywall-papersdl@archivebox.io. Setting it
to an empty string disables Unpaywall; UNPAYWALL_EMAIL is an alias. The hook
passes this value to the CLI as UNPAYWALL_EMAIL. Explicit --providers values in
PAPERSDL_ARGS_EXTRA still select providers.

Mirror discovery supports both links and redirects from sci-hub.now.sh. At
verification time its advertised mirror sci-hub.41610.org did not resolve.
Europe PMC/medRxiv PDF endpoints may require a browser challenge; metadata
lookup alone is not reported as a downloaded paper. bioRxiv may rate-limit
requests. PMC's public automatic SHA-256 download proof is handled with bounded work,
without executing downloaded JavaScript. Manuscript PDF discovery reads the
article self-link from official dataset XML, avoiding challenged HTML landing
pages. No CAPTCHA solving or rate-limit retries are added.

DOI prefixes require a literal period, so archived-link timestamps do not become
false paper identifiers. The same overlay is used for both CLI parsing and fetching.

Publisher title markup/entities are decoded before naming PDFs, and path separators
are replaced so paper titles remain filenames inside the output directory.
