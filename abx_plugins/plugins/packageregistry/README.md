# Package Registry

Captures package pages from PyPI (`/project/name[/version]/`), npm
(`/package/name[/v/version]`, including scoped names), crates.io
(`/crates/name[/version]`), and RubyGems (`/gems/name[/versions/version]`).
When no version is specified, the registry's current stable/latest release is
selected. It saves native registry API metadata in `registry.json`, a compact
manifest in `metadata.json`, and that release's distribution files. The manifest
includes published checksums and download URLs; checksums and npm integrity
hashes are checked before publication. Registry-provided README text is shown
when present in the metadata. License and other source files remain in the
original distribution archives.

`PACKAGEREGISTRY_MAX_BYTES` limits the combined distribution size (100 MiB by
default); exceeding it fails the capture and preserves the previous output.
`PACKAGEREGISTRY_TIMEOUT` sets the per-request timeout in seconds. Set
`PACKAGEREGISTRY_ENABLED=false` to disable the plugin. Captures require public
registry API and artifact access; private registries are not supported.
