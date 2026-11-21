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

## TLSN beta.1 matched release candidate

Build beta.1 from the same checked upstream source for both peers, without the
legacy MPZ, prover-wakeup, or WASM binary patches. The extension source patch
retains the local proof export, window placement, injected channel adapters, and
verifier transcript adapter. The lockfiles are part of this patch.

```sh
uv run --no-project python abx_plugins/plugins/tlsnotary/vendor/build.py "$out" \
  --profile beta1 --component extension
docker buildx build --platform linux/amd64 \
  -f abx_plugins/plugins/tlsnotary/vendor/beta1-verifier.Dockerfile "$out/src"
```

The browser build produces `$out/tlsnotary-beta1-extension.zip` and an unpacked
`$out/extension/` with the bundle checksum manifest. The ZIP uses stable entry
metadata for identical inputs. For a separate verifier build job, use a fresh
`$out` and `--profile beta1 --prepare-only` to stage the same verified source
context without a WASM compiler. Use the existing gateway and its proof/resource
policies; don't pair these outputs with the official alpha.15 artifacts.
