# Vendored papers-dl fixes

Upstream: https://github.com/benmuth/papers-dl/pull/5
Base PyPI package: papers-dl 0.0.25 (51b265c).
License: MIT; see LICENSE. These Python files are based on the canonical
papers-dl source at `668760286b1bf29befc349f3ba8bf2ba38fb700e`, with the local
PDF identity validation described below.

The papersdl snapshot hook puts this directory first on the installed CLI's
PYTHONPATH. It replaces its CLI entrypoint and parser/fetch/provider modules
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
arXiv PDF URLs omit the citation's `arXiv:` label while retaining subject and version.

Publisher title markup/entities are decoded before naming PDFs, and path separators
are replaced so paper titles remain filenames inside the output directory.

Known DOI queries reuse identifier metadata when naming the downloaded paper,
avoiding repeated PDF extraction and Google searches. Queries without usable DOI
metadata retain the existing PDF title discovery.

Before naming a PDF, a single DOI declared in its Subject metadata must agree
with a single requested DOI. Conflicting documents (such as a corrigendum offered
for the original paper) fail while retaining the unchanged, content-named PDF.
Absent or ambiguous metadata is not proof of a mismatch; body citations are not
used as document identity.

Long title filenames are limited to 255 UTF-8 bytes including the PDF extension,
with a title hash distinguishing truncated prefixes. PDF bytes remain unchanged.
