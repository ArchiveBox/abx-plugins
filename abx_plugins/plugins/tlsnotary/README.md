# TLSNotary

Opt-in main-response authentication through TLSNotary's browser extension. This
plugin uses the active Chrome profile, captures request headers with the upstream
`useHeaders` API, and makes the extension's separate authenticated request. It does
not retroactively authenticate Chrome traffic or other plugins' WACZ/DOM outputs.

The remote verifier receives an authenticated SHA-256 commitment and hostname.
Only a fixed HTTP protocol marker and whitespace are disclosed; URL paths, query
strings, cookies, response headers and page content stay on the client. MPC adds
substantial bandwidth and computation. Only one complete main GET response is
selected; assets and video streams are excluded. Responses over the configured
byte budget fail explicitly. There is no prefix or video-duration claim.

## Install and capture

```bash
uv run abx-dl install tlsnotary
TLSNOTARY_ENABLED=true uv run abx-dl dl --plugins=title,screenshot,tlsnotary \
  --dir=./capture 'https://news.ycombinator.com/'
```

`config.json` declares Chrome and the SHA-256-pinned official extension ZIP.
The setup hook copies the extension into the persona's plugin cache and applies
version-checked changes: return existing commitment openings and the received
transcript to the local caller before cleanup, remove the opening debug log, and
let Chrome position approval/managed windows inside the headless screen. No Rust compiler,
alternative prover, or custom MPC implementation is installed. See
[the upstream API audit](research/EXTENSION_API_AUDIT.md).

The hook uses Chrome's persisted snapshot target, drives the extension's approval
UI, and unloads its extension instance when finished or cancelled. It disconnects
from the shared browser without closing it. Proof failure produces a failed plugin
result; other archiving outputs remain available.

## Output and verification

The snapshot's `tlsnotary/current/` points to an atomically published capture:

- `response.http`: the exact authenticated response bytes, stored once.
- `receipt.json`: signed hash, hostname, range, time and local opening; no response copy.
- `metadata.json`: display metadata, never a substitute for signature verification.
- `index.html` and local viewer assets: verify the receipt against the response.

The viewer checks Ed25519, the blinded SHA-256 commitment, response length,
authenticated hostname and HTTP framing. It displays only the authenticated
hostname, not an unauthenticated claim about the requested URL path. Archived
content appears as text, never executable HTML. Files are not uploaded.

A third party must obtain the verification code and signing key independently.
`TLSNOTARY_TRUSTED_KEY` overrides the bundled key for a self-hosted server.
A key supplied by an untrusted archive is not a trust anchor. The signer and its
clock are trusted; the signature proves neither the truth of the page's claims
nor an independent timestamp. Local archives can still contain sensitive data.

## Run the verifier

```bash
mkdir -p /secure/tlsnotary-state
openssl genpkey -algorithm ED25519 -out /secure/tlsnotary-state/signing.pem
chmod 600 /secure/tlsnotary-state/signing.pem
cd server
TLSNOTARY_STATE_DIR=/secure/tlsnotary-state docker compose up -d --build
curl http://127.0.0.1:7047/key
```

Use that independently obtained `publicKey` as `TLSNOTARY_TRUSTED_KEY`; point
`TLSNOTARY_VERIFIER_URL` at the HTTPS service. Loopback HTTP is allowed for local
testing. Keep signing keys outside source/build contexts and back them up securely.
The bundled key must match the deployed service; a new self-hosted server has its
own key. Check `/key` independently before relying on an instance.

The gateway allows two sessions, enforces request/response allocation caps and
180-second deadlines, and signs only verifier webhooks containing a full-response
HASH commitment. The webhook listener and upstream verifier are isolated on a
private Docker network. Public endpoints require no authentication. The bridge
connects only to public IPv4 addresses on port 443, pinning DNS resolution.

## Acceptance status

The extension replacement is under active end-to-end testing. Previous native
prototype results and artifacts have been removed; they are not acceptance
results for this plugin. Docker, ArchiveBox UI and screenshot validation must be
recorded in `tests/RESULTS.md` before this status is changed.
