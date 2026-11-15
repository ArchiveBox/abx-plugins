# Temporary alpha.15 mux close-drain fix

**Why:** the official extension 0.1.0.1501 embeds tlsn-wasm alpha.15, whose mux
can discard an accepted final proof frame during shutdown, leaving the verifier
waiting. `mux-close-drain.patch` backports only the receiver drain from
[tlsn-utils #122](https://github.com/tlsnotary/tlsn-utils/commit/67aab0bced6950530f960d89d866405e4557bd02).
Protocol, verifier, proof checks, deadlines and retries are unchanged.

**Optional:** TLSNotary defaults off. With it enabled, set
`TLSNOTARY_MUX_PATCH_ENABLED=false` to use the official WASM, accepting the
upstream shutdown bug. Local-proof export and window-position fixes remain;
without export ArchiveBox cannot save its proof. Explicit source builds via
`TLSNOTARY_EXTENSION_DIR` bypass this overlay and need their matching verifier.

**Runtime:** `../mux_overlay.py` applies a 24,974-byte BSDIFF40 delta with Python's
standard library to both WASM copies in a separate `0.1.0.1501-export2-mux1`
cache. Opt-out uses `0.1.0.1501-export2`; neither modifies the official download
or older caches. Input, delta, output and original JS-glue hashes are checked.
Corruption fails closed, never silently disables the fix. No Rust/Docker/build
step, changed WASM ABI, or security/proof-verification bypass is needed at runtime.

**Reproduce:** `manifest.json` records artifact hashes and the upstream fix.
`Dockerfile` and `regenerate.py` pin source/tool downloads, exact revisions,
compiler versions and build paths. From the repository run:

`uv run abx_plugins/plugins/tlsnotary/vendor/mux-close-drain/regenerate.py /absolute/fresh/scratch-directory`

This maintainer-only Docker build verifies exact output/delta hashes and a real
TCP regression (unpatched loses the final frame; patched preserves it), writing
only to scratch. Run `tests/test_mux_overlay.py` for setup/opt-out/corruption/cache
checks and `tests/test_navigation_headers.py` for real browser proofs, including
signature and tamper rejection. The separate performance builder is not this fix.

**Remove when:** the pinned official extension's published WASM contains #122
or equivalent, and setup plus real-proof tests pass. A merged PR or newer version
alone is insufficient. Remove the conditional import/call, config flag,
`mux_patch` metadata and cache suffix from the prepare hook/config; delete this
directory, `../mux_overlay.py` and overlay-specific tests. Retain old caches.

**Attribution:** TLSN WASM and tlsn-utils declare `MIT OR Apache-2.0`; this mux
backport uses Apache-2.0. Both upstream mux licenses are included verbatim with
Parity Technologies attribution. No additional upstream root NOTICE exists.
The extension (MIT) is still downloaded unchanged from upstream; this directory
ships source changes and a binary delta, not an independent extension release.
