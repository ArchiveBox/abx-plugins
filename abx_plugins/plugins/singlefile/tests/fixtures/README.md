`archivebox-test-page.html` is byte-identical to
[`ArchiveBox/publicsite/tests/index.html`](https://github.com/ArchiveBox/ArchiveBox/blob/d3868ae08/publicsite/tests/index.html),
the source of <https://archivebox.io/tests/>.

The loaded-document regression serves this real static file through Python's
standard HTTP server so a GitHub Pages outage cannot replace its input with an
error page. The same test also captures the live Scraping-With-Cookies article.
