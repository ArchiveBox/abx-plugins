Source from privacy-ethereum/mpz v0.1.0-alpha.6 (6ebfe619490c3155a589fc6a3be83b0976de19dc). Workspace dependency versions resolved in Cargo.toml.
Experimental map concurrency bound backported from upstream 7a5d41c2358dbefaa76c3d07e41fd39ac74b2b09, retaining the alpha.6 executor API. No cryptographic algorithm changes.

The local map() patch processes 32 tasks per batch and exchanges a completed-count barrier on the parent channel after each batch. Local completion alone allowed the prover to outrun peer consumption, reproducing a 512-stream PeerProtocolError on Cabbage. Both peers must use this scheduling variant (WebSocket subprotocol abx-tlsnotary-batch-v1). Offline attestation verification and cryptographic algorithms are unchanged.

Upstream README.md licenses all crates under MIT OR Apache-2.0. This vendored copy includes the Apache-2.0 license text. See https://github.com/privacy-ethereum/mpz/tree/6ebfe619490c3155a589fc6a3be83b0976de19dc for attribution and the upstream license notice.
