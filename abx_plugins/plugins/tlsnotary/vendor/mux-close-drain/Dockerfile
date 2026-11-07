# Maintainer-only deterministic-path build; never executed during capture.
FROM ubuntu:24.04@sha256:78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl ca-certificates build-essential clang-18=1:18.1.3-1ubuntu1 libssl-dev pkg-config git patch xz-utils && rm -rf /var/lib/apt/lists/*
ENV HOME=/home/runner CARGO_HOME=/home/runner/.cargo RUSTUP_HOME=/home/runner/.rustup
ENV PATH=/home/runner/.cargo/bin:/usr/local/bin:/usr/bin:/bin
COPY rustup-init /tmp/rustup-init
RUN echo 'b1c8cb14e30889e7e2999e6b69e62d9e9b9ffc0f4df8180564d306d9680e6c55  /tmp/rustup-init' | sha256sum -c && chmod +x /tmp/rustup-init && /tmp/rustup-init -y --profile minimal --default-toolchain nightly-2026-05-21 --component rust-src --target wasm32-unknown-unknown
COPY wasm-bindgen.tar.gz binaryen.tar.gz /tmp/
RUN echo 'ceb6f0b9fea9d2868f258215a1d7f86a4c28330a0d64432cc7d53674618772d6  /tmp/wasm-bindgen.tar.gz' | sha256sum -c && echo '3dc677006555b355ea2da5e82602065a161d5e83eaefd3f759afa00b96e83212  /tmp/binaryen.tar.gz' | sha256sum -c && tar xzf /tmp/wasm-bindgen.tar.gz -C /tmp && cp /tmp/wasm-bindgen-0.2.120-x86_64-unknown-linux-musl/wasm-bindgen /usr/local/bin/ && tar xzf /tmp/binaryen.tar.gz -C /tmp && cp /tmp/binaryen-version_117/bin/wasm-opt /usr/local/bin/
COPY tlsn.tar.gz tlsn-utils.tar.gz mux-close-drain.patch /tmp/
RUN printf '%s\n' 'f119bebd606782842d906068b171a0d73c492f03d8f2ef1dc3016c352729607c  /tmp/tlsn.tar.gz' 'db1a058dd27bcaa879ec6ad0f7867cd9d07192ce0e6493ab096488dbcf57eede  /tmp/tlsn-utils.tar.gz' '5128e2c747bd4656526bed16aae71cbdf1f95f545d0111439b2e643ba8c11bde  /tmp/mux-close-drain.patch' | sha256sum -c && mkdir -p /home/runner/work/tlsn/tlsn && tar xzf /tmp/tlsn.tar.gz -C /home/runner/work/tlsn/tlsn --strip-components=1 && mkdir -p /home/runner/work/tlsn/tlsn/vendor/tlsn-utils && tar xzf /tmp/tlsn-utils.tar.gz -C /home/runner/work/tlsn/tlsn/vendor/tlsn-utils --strip-components=1
WORKDIR /home/runner/work/tlsn/tlsn
RUN printf '\n[patch."https://github.com/tlsnotary/tlsn-utils"]\ntlsn-mux = { path = "vendor/tlsn-utils/mux/mux" }\n' >> Cargo.toml && sed -i '/^name = "tlsn-mux"$/,/^$/ { /^source = /d; }' Cargo.lock && cd vendor/tlsn-utils && patch --batch --fuzz=0 -p1 -i /tmp/mux-close-drain.patch
ENV CC=clang-18 CARGO_BUILD_JOBS=4 RUSTUP_TOOLCHAIN=nightly-2026-05-21
WORKDIR /home/runner/work/tlsn/tlsn/crates/wasm
RUN test "$(rustc --version)" = "rustc 1.97.0-nightly (b954122bb 2026-05-20)" && cargo build --locked --lib --profile wasm --target wasm32-unknown-unknown && wasm-bindgen ../../target/wasm32-unknown-unknown/wasm/tlsn_wasm.wasm --out-dir pkg --typescript --target web && wasm-opt pkg/tlsn_wasm_bg.wasm -o pkg/tlsn_wasm_bg.opt.wasm -O && mv pkg/tlsn_wasm_bg.opt.wasm pkg/tlsn_wasm_bg.wasm
RUN rustc --version && clang-18 --version && wasm-bindgen --version && wasm-opt --version && find pkg -maxdepth 1 -type f -exec sha256sum {} \;
CMD ["/bin/true"]
