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
set -euo pipefail
capture_dir="$(mktemp -d)"
uv run --no-sync --exclude-newer-package abx-dl=2100-01-01 --with-editable . --with abx-dl==1.12.276 abx-dl install tlsnotary
TLSNOTARY_ENABLED=true uv run --no-sync --exclude-newer-package abx-dl=2100-01-01 --with-editable . --with abx-dl==1.12.276 abx-dl dl \
  --plugins=title,screenshot,tlsnotary --dir="$capture_dir" 'https://news.ycombinator.com/'
node abx_plugins/plugins/tlsnotary/tests/check_capture.mjs \
  "$capture_dir/tlsnotary/current" MCowBQYDK2VwAyEA0H35h4fS0zKwPykdHg5ST/w/Byeek4VGQBSsmKBsr+E=
```

Run these source-checkout examples from the abx-plugins repository root. The
temporary directory printed by abx-dl contains the capture; choose a persistent
`--dir` for your archive. With an installed abx-dl environment, the equivalent
capture command is `TLSNOTARY_ENABLED=true abx-dl dl --plugins=title,screenshot,tlsnotary URL`.

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

The self-contained [server directory](server/README.md) includes Compose, example
environment/tunnel configuration, and complete deployment instructions.

```bash
set -euo pipefail
export TLSNOTARY_STATE_DIR="$(mktemp -d)"
export TLSNOTARY_PORT="${TLSNOTARY_PORT:-7047}"
openssl genpkey -algorithm ED25519 -out "$TLSNOTARY_STATE_DIR/signing.pem"
chmod 600 "$TLSNOTARY_STATE_DIR/signing.pem"
cd abx_plugins/plugins/tlsnotary/server
docker compose -p tlsnotary-example up -d --build
deadline=$((SECONDS + 30))
until curl -fsS "http://127.0.0.1:$TLSNOTARY_PORT/health"; do
  (( SECONDS < deadline )) || exit 1
  sleep 0.2
done
curl -fsS "http://127.0.0.1:$TLSNOTARY_PORT/key"
```

Choose a durable `TLSNOTARY_STATE_DIR` for deployment; the example creates a new
key in a temporary directory. Save that path before leaving the shell. The service
continues running until `docker compose -p tlsnotary-example down` is called from
the server directory with the same environment.

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

## Acceptance

Real extension captures passed in abx-dl Docker and ArchiveBox Docker for Hacker
News and Sweeting.me. A real httpbin cookie round trip passed. The ArchiveBox
snapshot detail preview and independent verification UI both verified the saved
bytes; tampering was rejected. Two public Cabbage sessions succeeded concurrently
and a third received HTTP 503. See [measured results](tests/RESULTS.md).

YouTube's watch page did not complete within 180 seconds; ordinary title and
screenshot outputs still succeeded. Large-response hashing remains constrained by
the pinned upstream extension. This plugin does not certify video prefixes.

The working public endpoint and verification UI are https://tlsnotary.zervice.io/.
The `tlsnotary.archivebox.io` and `verify.archivebox.io` aliases still need
Cloudflare DNS changes before they can be added to the ingress and used.

For an offline check with Node 22+, obtain this plugin and its public key from an
independently trusted source, then run (no network access is used):

```bash
set -euo pipefail
node abx_plugins/plugins/tlsnotary/tests/check_capture.mjs \
  abx_plugins/plugins/tlsnotary/tests/fixtures/hacker-news \
  MCowBQYDK2VwAyEA0H35h4fS0zKwPykdHg5ST/w/Byeek4VGQBSsmKBsr+E=
```

Replace the real public Hacker News fixture path with your snapshot's
`tlsnotary/current` directory. Replace the key for a different trusted server.

This checks the real signature and response and also asserts that five altered
versions are rejected. The public key is pinned in `server/web/verify.mjs`; do not trust
code or keys supplied only by the archive you are investigating.
