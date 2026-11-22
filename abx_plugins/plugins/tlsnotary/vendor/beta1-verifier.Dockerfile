# Build context: the src/ directory from vendor/build.py --profile beta1 --prepare-only.
FROM docker.io/library/rust:1.95.0-bookworm@sha256:6258907abe69656e41cd992e0b705cdcfabcbbe3db374f92ed2d47121282d4a1 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git libssl-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY tlsnotary-tlsn-40a0695 ./tlsnotary-tlsn-40a0695
COPY tlsnotary-tlsn-extension-d446234/servers ./tlsnotary-tlsn-extension-d446234/servers
ENV CARGO_TARGET_DIR=/out
RUN cargo build --locked --release \
    --manifest-path tlsnotary-tlsn-extension-d446234/servers/Cargo.toml \
    --bin tlsn-verifier-server

FROM docker.io/library/debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV GIT_HASH=d44623434d04ad00d7b1f8da9712cbc10cd0e99c
COPY --from=builder /out/release/tlsn-verifier-server /app/tlsn-verifier-server
# The Compose stack mounts its private webhook config; no upstream demo webhook.
EXPOSE 7047
CMD ["/bin/sh", "-ec", "test -r /app/config.yaml; exec /app/tlsn-verifier-server"]
