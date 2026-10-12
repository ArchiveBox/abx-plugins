# Matching TLSNotary performance build

This builds the extension and native verifier from SHA-256-checked upstream
source archives and zero-fuzz patches. The MPZ gate-OT change is opt-in on both
peers; mixing a rebuilt client with the released server is unsupported.

Install Rust 1.95.0, a nightly toolchain with `rust-src`, wasm-pack 0.14.0,
Node/npm, jq and the native TLSNotary build dependencies. The WASM build uses
upstream's nightly `build.sh`. From the abx-plugins checkout, run
`uv run abx_plugins/plugins/tlsnotary/vendor/build.py /absolute/fresh/build-directory`.

The default WASM toolchain is `nightly-2026-10-12`; `--wasm-toolchain nightly`
selects an already installed nightly. `--component verifier` or
`--component extension` builds one side for its destination platform. Use a fresh
directory for each invocation; the builder refuses to overwrite extracted sources.
It does not install toolchains or start services.

Outputs are `tlsn-verifier-server`, `extension/` and `extension.zip`. Run the
native binary with the upstream verifier configuration used by the existing
[gateway](../server/README.md). Keep the gateway, signing key, private verifier
network and resource limits. This is an alternative source build, not a command
to replace a running deployment.

Set `TLSNOTARY_EXTENSION_DIR` to the generated `extension/` directory and configure
`TLSNOTARY_VERIFIER_URL` and `TLSNOTARY_TRUSTED_KEY` for that matching verifier.
Crawl setup checks the generated bundle hashes before using its local proof export.
An empty extension-directory setting retains the normal official release.

The patches preserve private-response commitments and local-only transcript
export. They do not increase byte limits, relax signature verification, or add
queues/retries. Upstream submissions are [MPZ #455](https://github.com/tlsnotary/mpz2/pull/455)
and [TLSN #1197](https://github.com/tlsnotary/tlsn/pull/1197). MPZ is pinned to the
revision compatible with this TLSN release; the patch backports the submitted
changes. Performance measurements are not a security audit or a production
concurrency guarantee.
