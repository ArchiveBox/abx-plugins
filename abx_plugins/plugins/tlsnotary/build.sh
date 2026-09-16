#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
cargo build --release --locked --manifest-path runtime/Cargo.toml
RUSTFLAGS='--cfg getrandom_backend="wasm_js"' cargo build --release --locked --target wasm32-unknown-unknown --no-default-features --features web --lib --manifest-path runtime/Cargo.toml
wasm-bindgen runtime/target/wasm32-unknown-unknown/release/abx_tlsnotary.wasm --target web --out-dir web/pkg
# Requires wasm-bindgen-cli 0.2.126 and rustup target add wasm32-unknown-unknown.
# Explicit inputs keep private keys, caches and captures out of downloadable bundles.
COPYFILE_DISABLE=1 tar --exclude=__pycache__ --exclude='*.pyc' -czf web/source.tar.gz README.md build.sh config.json on_CrawlSetup__80_tlsnotary_prepare.py on_Snapshot__95_tlsnotary.py runtime/Cargo.toml runtime/Cargo.lock runtime/src server/compose.yaml server/Dockerfile.runtime server/nginx.conf server/cloudflared.yaml web/index.html web/app.js web/worker.js web/style.css web/trust.json web/OFFLINE.md tests
COPYFILE_DISABLE=1 tar -czf web/offline-verifier.tar.gz -C web index.html app.js worker.js style.css trust.json OFFLINE.md pkg source.tar.gz
