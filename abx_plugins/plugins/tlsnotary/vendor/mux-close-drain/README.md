# Removable alpha.15 mux close-drain correction

The official TLSNotary extension 0.1.0.1501 contains the npm `tlsn-wasm`
0.1.0-alpha.15 artifact. Its multiplexer can silently discard a final frame
accepted through `Handle::new_stream` when the connection closes before the
next driver poll. This can leave a verifier waiting after normal mux shutdown.

This directory backports only the close/close_no_wait receiver drain from
[tlsn-utils #122](https://github.com/tlsnotary/tlsn-utils/commit/67aab0bced6950530f960d89d866405e4557bd02)
onto the released dependency. `mux-close-drain.patch` is the complete source
change. The protocol, verifier, proof checks, deadlines and retry policy are
unchanged. This is not the newer TLSN lifecycle or MPZ performance upgrade.

## Runtime application

The normal official ZIP URL and SHA-256 in `config.json` remain unchanged.
Crawl setup copies that download into a separate `0.1.0.1501-export2-mux1`
cache under the existing preparation lock. `../mux_overlay.py` verifies each
input, the delta, and each output against `manifest.json`, then applies the
24,974-byte standard BSDIFF40 delta to the two identical WASM files.

The rebuilt module uses the original compiler versions and has exactly the
same imports and exports. The original JavaScript glue is retained and
checksum-verified; no generated binding is guessed or rewritten. The other
existing local-proof/window patches still run normally. The installed download
and older prepared caches are not modified. Changed inputs or cached outputs
fail closed instead of silently falling back or repairing them.

The ZIP is an upstream unpacked-extension distribution, not a signed CRX that
this patch claims to preserve. Chrome already loads the plugin's locally
prepared extension. No browser security warning, permission, signature check,
or proof verification is bypassed. Normal capture needs only Python's standard
library; no Rust compiler, Docker, or binary patch executable is added to it.

## Source provenance and regeneration

- TLSN: `47aee45b53e06648c1b2ad3689b367b8c923fdec`.
- tlsn-utils: `64722f7de999cbd41c0cab7312dade306d50ea5f`.
- Official extension source: `68bbd380d6e1a89d79748e5294661c752f5d1205`.
- rustup bootstrap: `1.29.3`, exact archive SHA-256 checked before execution.
- rustc: `1.97.0-nightly (b954122bb 2026-05-20)`.
- Ubuntu clang: `18.1.3 (1ubuntu1)`.
- wasm-bindgen: `0.2.120 (3c5043f94)`; wasm-opt: `117`.
- BSDIFF generator: `bsdiff4==1.4.1`, maintainer-only.

`Dockerfile` pins the Linux amd64 base, compiler date and build-tool archives.
Source/tool hashes are checked before use, Rust dependencies remain locked,
and fixed container paths avoid embedding the maintainer's home directory.
The compiler assertion prevents the upstream mutable `nightly` override from
silently selecting another compiler. `regenerate.py` verifies exact output
and delta hashes; drift is a failed regeneration, not permission to update pins.

From the repository, run with your chosen Docker endpoint/context:

`uv run abx_plugins/plugins/tlsnotary/vendor/mux-close-drain/regenerate.py /absolute/fresh/scratch-directory`

The command builds locally and checks a real TCP regression: the unpatched
source loses the accepted frame, and the backport delivers its exact bytes.
It produces a verified delta in scratch space and never edits this checkout,
changes Docker's global context, publishes artifacts, or accesses credentials.
Build images/cache remain available for inspection. Source builds need Docker,
network access to the pinned public dependencies, and several minutes; runtime
installs do not. The ordinary wheel/sdist include this small patch resource
through the existing package rules and release gates.

Run `tests/test_mux_overlay.py` for the real setup/corruption/cache checks and
`tests/test_navigation_headers.py` for actual browser proofs against the pinned
verifier. The real proof suite retains signature and tamper-rejection checks.

## Removal and upstream status

Remove the overlay import/call, `mux_patch` metadata and mux cache suffix from
`on_CrawlSetup__80_tlsnotary_prepare.py`, then remove this directory and
`../mux_overlay.py`, only after pinning an official extension whose actual WASM
contains #122 or an equivalent fix. Review its glue/version guards and run both
setup and real-proof tests. Keep older caches intact. A closed PR or newer
version string alone does not establish that its published binary has the fix.

As checked on 2026-11-07, newer upstream source has additional fixes: TLSN
[#1202](https://github.com/tlsnotary/tlsn/pull/1202) supersedes #1197, #1207
changes lifecycle/close handling, and MPZ #457/#465 supersede portions of #455.
They are not included in the downloadable extension/npm alpha.15 artifact.
Some newer changes alter peer messages, so they are not mixed into this
same-verifier correction. The optional neighboring performance builder is a
separate explicitly configured path.

## Attribution

TLSN WASM and tlsn-utils mux declare `MIT OR Apache-2.0` in their published
package/source metadata. This modified mux contribution is redistributed under
the Apache-2.0 option. Both upstream mux license texts are included verbatim,
including the original Parity Technologies copyright. Original source and the
upstream correction are linked above. The extension itself declares MIT and
continues to be downloaded from upstream unchanged. This directory distributes
source changes and a binary difference, not an independent extension release.
The existing source metadata contains no additional root NOTICE to reproduce.
