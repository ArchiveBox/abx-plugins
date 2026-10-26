# Memento archive download fixture

`iframetest.wacz` is Webrecorder's real, small public capture fixture, copied
unchanged from [ReplayWeb.page at 38305d7](https://github.com/webrecorder/replayweb.page/blob/38305d70c9155caed8f58c84255fac9397155e27/mkdocs/site/docs/examples/iframetest.wacz).
The upstream project is [AGPL-3.0](https://github.com/webrecorder/replayweb.page/blob/38305d70c9155caed8f58c84255fac9397155e27/LICENSE).
Tests also extract its existing WARC to verify compressed and uncompressed
downloads without running a new capture or depending on external services.

# Color-scheme replay fixtures

`color-scheme-{auto,light,dark}.wacz` were recorded on October 26, 2026 with the
real ArchiveWeb.page 0.17.0 extension through `uv run --no-sync abx-dl dl
--plugins=archivewebpage --timeout=60 --dir=<output> <local HTTP URL>`. Each is an
unchanged exported archive of one small local HTML page; no archive records were
fabricated. The source pages set `:root { color-scheme: light dark }`, `light`, or
`dark`, respectively. Their bodies have no background and use `color: CanvasText`.
A 32px square at `(32px, 32px)` uses `background: currentColor`, and a paragraph
contains “Readable archived text”. The fixture tests compare actual composited
foreground/background screenshot pixels in both browser color preferences.

| Fixture | SHA-256 |
| --- | --- |
| auto | `43b44601e37a608784ac8cdceaec2ec84fa9281b6ed60df3ea38172e2256895f` |
| light | `647e7a6d4423232c37da7a35bc3b7c313073ad573e43669f48308d9b6a578c67` |
| dark | `afbd3b1ad2f9e64fcc8c72cca87e3f58fc2993263f291975f0e2226978ed1a41` |
