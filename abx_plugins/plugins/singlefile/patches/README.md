# Temporary SingleFile large-CSS parser fix

SingleFile 1.37.0 includes core 1.12.0 and the upstream
[font-source caching fix](https://github.com/gildas-lormeau/single-file-core/pull/12).
The local font patch has been removed. Native font validation is unchanged.

## Why the remaining patch exists

The bundled CSS-tree parser still packs token offsets into 27 bits. CSS reaching
134,217,728 UTF-16 code units wraps offsets and can corrupt parsing or prevent
progress. [Upstream issue #174](https://github.com/eslint/csstree/issues/174)
tracks the original 24-bit limitation; SingleFile's 27-bit workaround only raises
that boundary. Buffers also allocate one entry per source character even for a
single large data URL.

The preparation hook replaces only the exact TokenStream class in a private,
immutable extension copy. Every other bundle byte stays unchanged. It checks
version 1.37.0, the input hash, unique class anchor and output hash:

- Original: `66e04a0203fcaf633d84e7758004df490300fd13c6d074bd2c2b045ffd1371ab`
- Prepared: `a9379319cbcf018ceff47365d36b91f56af0140b607bcc5d7abece8e9db54050`

Set `SINGLEFILE_PATCH_LARGE_CSS=false` to use the unmodified installed extension.
This explicitly accepts the upstream large-CSS limitation; failures never silently
fall back. Disabling removes only the crawl-local selection metadata, leaving the
provider installation and existing prepared copies intact.

Remove this hook, its capture guard, setting and patch materials once an upstream
extension fixes token storage and passes the large-CSS and full-fidelity gallery
regressions. A font-caching release alone does not fix token storage.

## Source and validation

`css-tree/TokenStream.js` and `TokenStream.patch` are based on official
@eslint/css-tree 4.1.1 TokenStream.js, SHA256
`9d1b0b5e157a984b2700b75a2a44b63dae4ab426186f4d94d3aff2436967d0ec`.
They retain the MIT notice for Roman Dvornov and OpenJS Foundation contributors
in `css-tree/LICENSE`. `css-tree/build.js` and `build.patch` describe the
corresponding single-file-core build overlay and retain its AGPL license in
`AGPL-3.0.txt`. Preparation performs no source download or rebuild.

Token words use exact Float64 arithmetic for a type and full Uint32 offset;
buffers grow by token count, including EOF. Dense storage costs 12 rather than
8 bytes per capacity entry, and growth temporarily retains both buffers.
Sparse large styles avoid source-length allocation. Grammar, URL handling and
AST output are unchanged. Tests exercise the real bundled parser across 128 MiB,
token APIs, buffer reuse and generated CSS. These checks do not replace native
capture and full gallery acceptance.

The separate timer, primary-cache and lazy-validation patches and fixtures are
experimental tests only. They are not applied by preparation; their staged hashes
live in `tests/fixtures/font_validation_candidates.json`. Do not ship them based
on a metadata benchmark: font fallback behavior and timing need native validation.
