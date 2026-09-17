Source (formatted with cargo fmt) from https://github.com/tlsnotary/tlsn-utils/tree/ed192916290f1a718596b0b01ffa4b33982232ac/mux/mux .
Cargo.toml resolves workspace rand to 0.10 and omits development-only dependencies.
Vendored because Cargo does not permit a same-source git revision patch.
This pins upstream bounded stream admission without changing cryptographic algorithms.
Two comment typos corrected for repository spelling checks.
