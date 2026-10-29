# DOM outlinks regression fixture

`expanded-dashboard.html.xz` is reduced from the SingleFile output of the public
https://lungfish-science.github.io/wastewater-dashboard/ capture made on
2026-10-29 with the real Chrome, Infiniscroll and SingleFile hooks. It redirects
to `wastewater-variant-explorer.html` and contains 491,931 HTML elements,
including 452,632 table cells. The upstream project includes its LICENSE link.

To fit the repository fixture budget, text-only table cell values are replaced
with equal-length runs of `x`, and embedded image payloads with `data:,`.
Tags, attributes, styles, embedded fonts and anchor destinations are retained.
These reductions affect only this test fixture, not captured output or runtime
extraction. Both the original and reduced page reproduce the protocol timeout.

The fixture retains the actual page structure and styles: a synthetic table
with the same element count did not reproduce the failure. The original DOM
outlinks hook exceeded its normal 30-second Chrome protocol deadline on this
saved page while calculating asset categories it never emitted. Collecting
only the existing anchor output finished in under a second, with all 15 expected
URLs, order, deduplication and depth fields retained. The regression runs the
real hook and Chrome against these local bytes without network dependence on
the original site, altered timeouts, or site-specific extraction logic.
