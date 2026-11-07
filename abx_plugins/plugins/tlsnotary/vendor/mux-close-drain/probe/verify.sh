#!/bin/bash
set -euo pipefail
source_dir=/home/runner/work/tlsn/tlsn/vendor/tlsn-utils
patch --batch --fuzz=0 --reverse -d "$source_dir" -p1 -i /tmp/mux-close-drain.patch
if cargo run --locked --manifest-path /probe/Cargo.toml --target-dir /tmp/probe-target > /tmp/mux-original.log 2>&1; then
    echo 'Unpatched mux unexpectedly preserved the frame; recheck the regression.' >&2
    exit 1
fi
grep -F 'real TCP mux closed without delivering its accepted final frame' /tmp/mux-original.log
patch --batch --fuzz=0 -d "$source_dir" -p1 -i /tmp/mux-close-drain.patch
cargo run --locked --manifest-path /probe/Cargo.toml --target-dir /tmp/probe-target
