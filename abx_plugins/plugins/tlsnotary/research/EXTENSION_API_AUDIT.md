# Official extension integration audit (2026-09-17)

This audit supersedes claims that TLSNotary lacks portable attestations or that a
new native prover is required. The official extension is the integration baseline.
The existing native prototype is not acceptance evidence for authenticated browser
capture. The implementation now applies a narrow local-export and window-placement patch
to the pinned extension bundle; its cryptographic protocol and WASM are unchanged.

## Sources and versions

- https://tlsnotary.org/docs/extension/
- https://tlsnotary.org/docs/faq/#how-can-i-verify-the-data-origin-in-a-tlsnotary-proof
- https://tlsnotary.org/docs/faq/#what-is-the-role-of-a-notary
- Official release `0.1.0.1501`, tag commit `68bbd380d6e1a89d79748e5294661c752f5d1205`.
- Also inspected upstream main `d44623434d04ad00d7b1f8da9712cbc10cd0e99c`.
- Chrome Web Store ID: `gcfkkledipjbgdbimfpijgbkhajiaaph`.
- Official release ZIP: https://github.com/tlsnotary/tlsn-extension/releases/download/0.1.0.1501/extension-0.1.0.1501.zip
- GitHub asset SHA-256: `1c312ad4305653b8d9351cf3eac46de4222a43be513260bf53053cfa1de45155`.

## Installation verified

The unmodified release installed successfully through the same `chromewebstore`
provider used by ArchiveWebPage. The generated `tlsnotary.extension.json` points
to an unpacked extension whose manifest reports version `0.1.0.1501`. No Rust
compiler or custom prover was used for this install. From the workspace root:

```bash
uv run --project abx-dl abxpkg install tlsnotary \
  --binproviders chromewebstore --lib /tmp/tlsnotary-official-provider \
  --install-args '["gcfkkledipjbgdbimfpijgbkhajiaaph","--name=tlsnotary","--url=https://github.com/tlsnotary/tlsn-extension/releases/download/0.1.0.1501/extension-0.1.0.1501.zip","--sha256=1c312ad4305653b8d9351cf3eac46de4222a43be513260bf53053cfa1de45155"]'
```

This verifies extension packaging only, not an end-to-end archiving flow. The
tested provider is the dependency installed in the abx-dl environment. The current abxpkg provider supports the pinned release URL and hash arguments.

## Existing functionality to reuse

The FAQ explicitly describes an online notary producing an attestation for later,
offline verification. It also describes selective disclosure via presentations.
These are TLSNotary capabilities, not something ArchiveBox needs to invent.

The extension uses the existing browser authentication session. Its background
`webRequest` listeners capture request bodies and headers with `extraHeaders`.
The Twitter example uses Cookie, authorization and CSRF headers. Managed windows
and `useHeaders` are the supported plugin integration surface; a successful public
GET does not demonstrate this authentication flow.

`packages/plugins/src/swissbank_hash.plugin.ts` demonstrates authenticated,
blinded response commitments. Its handler uses:

```javascript
{ type: 'RECV', part: 'BODY', action: { kind: 'HASH', algorithm: 'SHA256' } }
```

The current source API calls this action HASH; some website documentation calls
it PEDERSEN. Use the pinned release types/examples when constructing plugin code.
Do not copy the example's REVEAL handlers for request start lines or account IDs:
those deliberately reveal fields and do not meet ArchiveBox's privacy requirements.

The underlying WASM `Prover.reveal()` returns `HashOpening` values: a digest and a
16-byte blinder for each committed range. This supports checking existing local
bytes later without embedding those bytes in the receipt. Range ordering and the
hash algorithm must be retained. This is not a measurement of final receipt size.
With SHA-256, those two fields account for 48 raw bytes per range, before the
signature, identity, range metadata and serialization overhead.

`packages/eas-webhook` demonstrates the upstream-supported integration pattern:
a verifier webhook is consumed by an application that issues a durable attestation.
The bundled example is a Spotify/Sepolia demo, not a drop-in private archive
verifier: it uses disclosed fields and hashes the redacted transcript. Merely
copying that transcript hash would not authenticate hidden response content.
ArchiveBox does not need blockchain transactions to use the webhook pattern.

## Export boundary and implemented integration

In release `0.1.0.1501`, `packages/extension/src/offscreen/SessionManager.ts`
obtains `openings` from `proveManager.reveal()`, logs them at debug level, then
returns only `proveManager.getResponse()`. The prover is cleaned up in `finally`.
`ProveManager/index.ts` stores the session-completed response as `{results}`.
The verifier's `VerificationResult` contains results/error, without a signature.

Pinned source references:

- [Authenticated HASH example](https://github.com/tlsnotary/tlsn-extension/blob/68bbd380d6e1a89d79748e5294661c752f5d1205/packages/plugins/src/swissbank_hash.plugin.ts#L43-L86)
- [Openings computed but not returned](https://github.com/tlsnotary/tlsn-extension/blob/68bbd380d6e1a89d79748e5294661c752f5d1205/packages/extension/src/offscreen/SessionManager.ts#L354-L381)
- [Application attestation webhook example](https://github.com/tlsnotary/tlsn-extension/tree/68bbd380d6e1a89d79748e5294661c752f5d1205/packages/eas-webhook)

Thus the inspected `window.tlsn.execCode()` -> `prove()` path does not directly
export the openings or a signed receipt. This is a specific public-API boundary,
not a claim that TLSNotary's protocol cannot authenticate origin or that its
extension is broken. Debug-log scraping is not a supported export mechanism.

The plugin returns these existing values to the local caller before cleanup.
Signing uses the existing verifier webhook integration;
the signer must only accept actual verified commitments from its trusted verifier,
not arbitrary client-submitted hashes. The offline trust anchor must be obtained
independently of the artifact being checked.

The narrow integration work is to expose the existing local openings/ranges to
the caller, attest the verifier-authenticated commitments through the existing
webhook pattern, and check them against matching local response bytes. This does
not require a replacement TLS client, MPC protocol, or response-sized proof blob.
Exposing the opening is distinct from disclosing it to the remote verifier: it
belongs in the local output only. It must not be sent in `sessionData`, which the
extension forwards to the verifier. Request paths, query strings and credentials
must likewise stay out of session metadata and REVEAL handlers.

## Required acceptance

- Install the official extension via `chromewebstore`, load with the existing
  Chrome helpers, and drive its supported flow in the active persona.
- Prove a real authenticated main response with HASH handlers; no request secrets
  or response plaintext may be disclosed to the verifier or written to logs.
- Save a compact receipt/openings and file references, without a response copy.
- Compare the bytes actually proved with the archived response. A replay can
  differ; unrelated DOM, screenshots and WACZ entries are not thereby certified.
- Verify offline against local bytes and a pinned key; changed bytes, receipt,
  openings or key must fail. Missing data and unmatched outputs remain explicit.
- Bound concurrency and duration; test a refused/unreachable verifier alongside
  ordinary archiving plugins and confirm their outputs still succeed.
- Record latency, resource use and receipt size from that actual extension flow.

The prior native benchmarks do not satisfy these extension acceptance checks.

## Measured implementation constraints

The stock verifier requires a nonempty redacted transcript. We reveal only the
fixed HTTP/1.1 protocol marker and whitespace; the gateway rejects other
disclosures. The full response uses ALL/SHA256. The local opening is 16 bytes.

Uncompressed Hacker News exceeded the upstream MPC mux stream limit while hashing.
Requesting normal gzip encoding reduced its authenticated HTTP response to about
6 KB and completed successfully. No mux limit or cryptographic implementation was
modified. Larger pages can still fail; see [acceptance results](../tests/RESULTS.md).

## Direct extension approval

The pinned release's `entries/ConfirmPopup/index.tsx` sends
`{type: 'PLUGIN_CONFIRM_RESPONSE', requestId, mode: 'all-session'}` to the background
worker. `entries/Background/index.ts` forwards it to
`ConfirmationManager.handleConfirmationResponse`, which resolves the pending
execution and closes the popup. The hook reads the request ID from that extension
target's URL and sends this exact RPC from its existing `offscreen.html` target
through CDP `Runtime.evaluate`. The offscreen context remains alive when approval
closes the popup. No DOM selectors, button text, pointer events, or React internals
are used. `window.tlsn.execCode`, `useHeaders` and `prove` remain the execution APIs.
